import concurrent.futures
import contextlib
import random
import re
import threading
import time
from collections import deque

import requests

from core.adaptive_concurrency import AdaptiveConcurrency
from translator_providers import build_provider_registry, normalize_base_url


def translation_worker_limit(max_workers, primary_id, engine):
    """Return the real worker cap for the active route.

    Bing can sustain high concurrency with batched requests. Other free
    endpoints are more rate-limit prone, so keep them conservative.
    """
    max_workers = max(1, min(32, int(max_workers or 1)))
    if primary_id == 'bing':
        return min(max_workers, 4)
    if primary_id == 'gtx':
        return min(max_workers, 8)
    if primary_id == 'azure':
        return min(max_workers, 5)
    if primary_id in ('mymemory', 'libretranslate'):
        return min(max_workers, 4)
    if engine in ('claude', 'openai', 'market_ai', 'local'):
        return min(max_workers, 8)
    return max_workers


def engine_concurrency_limit(max_workers, engine_id):
    """Cap provider fan-out independently from the selected primary route."""
    max_workers = max(1, int(max_workers or 1))
    if engine_id == 'azure':
        return min(max_workers, 5)
    if engine_id == 'gtx':
        return min(max_workers, 8)
    if engine_id == 'bing':
        return min(max_workers, 4)
    return max_workers


def translation_wait_budget(engine_route, strict_paid_primary):
    """Return the per-chunk cumulative cooldown budget in seconds."""
    if engine_route == 'non_ai_chain' and not strict_paid_primary:
        return 45
    return 300


def translation_fallback_order(engine, primary_id, ai_provider_key=None,
                               azure_available=None):
    """Return fallback route order after the selected primary engine.

    Non-AI mode uses Bing, Azure, and GTX in priority order. A throttled engine
    is skipped until its cooldown expires without reducing the other engines.
    """
    if primary_id == "market_ai" and ai_provider_key in (
            "deepseek_v4_flash_free", "openrouter_free_router", "openrouter_free_models", "openrouter"):
        order = ['bing', 'azure', 'gtx', 'libretranslate', 'google_api']
    elif engine == "non_ai_chain":
        order = ['bing', 'azure', 'gtx']
    elif engine != "non_ai_chain" and primary_id in (
            "market_ai", "openai", "claude", "google_api", "azure", "deepl"):
        order = ['bing', 'azure', 'gtx']
    else:
        order = ['bing', 'azure', 'gtx', 'libretranslate', 'google_api']
    if azure_available is False:
        order = [engine_id for engine_id in order if engine_id != 'azure']
    return order


def engine_rate_limit_cooldown(engine_route, engine_id, retry_after):
    """Normalize short provider retry hints for route switching.

    Azure sometimes returns a very short Retry-After while immediately limiting
    again. In the non-AI chain, give the limited engine a useful cooldown so the
    next engine can run at full speed instead of bouncing back too quickly.
    """
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = 30.0
    if engine_route == "non_ai_chain":
        minimums = {
            "deepl": 60.0,
            "azure": 90.0,
            "bing": 60.0,
            "gtx": 30.0,
        }
        seconds = max(seconds, minimums.get(engine_id, seconds))
    return max(1.0, seconds)


def select_ready_engine(active, throttle_until, now=None):
    """Pick the first ready engine, preserving configured failover priority."""
    current = time.time() if now is None else now
    return next((engine_id for engine_id in active
                 if throttle_until.get(engine_id, 0.0) <= current), None)


def should_split_timeout_batch(engine_route, engine_id, chunk_len):
    """Decide whether a timed-out batch should retry synchronously.

    The non-AI chain is optimized for failover throughput. If Bing/Azure/GTX
    times out, holding a worker for same-engine split retries collapses total
    speed and makes stop/pause feel stuck. Let the chain switch engines instead.
    """
    if chunk_len <= 1:
        return False
    if engine_route == "non_ai_chain":
        return False
    return True


def soft_throttle_after_success(engine_route, engine_id, chunk_len, elapsed_seconds):
    """Return a short cooldown when a free endpoint is silently slowing down.

    Some free endpoints do not return 429 when they are saturated; they simply
    take far longer per batch. Treat that as a soft rate limit so the route can
    use the next engine instead of leaving every worker stuck on the same slow
    provider.
    """
    if engine_route != "non_ai_chain" or chunk_len < 40:
        return 0.0
    elapsed = max(0.001, float(elapsed_seconds or 0.0))
    rate = chunk_len / elapsed
    if engine_id == "bing" and rate < 45.0:
        return 20.0
    if engine_id == "azure" and rate < 16.0:
        return 18.0
    return 0.0


def likely_bing_passthrough(text):
    """Detect short names that Bing commonly returns unchanged.

    These still go through Bing first for throughput. If Bing returns the
    source unchanged, the fallback pass keeps the configured route order so
    a large wave is not dragged down by GTX while Azure is still available.
    """
    if not isinstance(text, str) or not text.strip() or "\n" in text:
        return False
    plain = re.sub(r'(?:§.|%\d*\$?[a-zA-Z]|\{\d+\}|\[#\d+#\])', ' ', text).strip()
    if not plain or len(plain) > 100:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", plain)
    if not words or len(words) > 9:
        return False
    if len(words) == 1:
        return len(words[0]) >= 3
    title_words = sum(1 for word in words if word[0].isupper() or word.isupper())
    return title_words / len(words) >= 0.65


def _translation_result_is_complete(source_items, translated_items):
    """Return whether a provider produced one non-blank string per input."""
    return (
        isinstance(translated_items, (list, tuple))
        and len(translated_items) == len(source_items)
        and all(isinstance(item, str) and item.strip()
                for item in translated_items)
    )


def interruptible_sleep(should_stop, seconds, quantum=0.2):
    """Sleep in short slices so stop/pause requests are observed promptly."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(max(0.01, quantum), remaining))
    return False


@contextlib.contextmanager
def stoppable_executor(max_workers, should_stop):
    """Avoid ThreadPoolExecutor.__exit__ waiting for active requests on stop."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        yield executor
    finally:
        stopping = bool(should_stop())
        try:
            executor.shutdown(wait=not stopping, cancel_futures=stopping)
        except TypeError:
            executor.shutdown(wait=not stopping)


def batch_translate_missing(self, missing_strings, _force_chunk_size=None,
                            _preferred_engine=None):
    total       = len(missing_strings)
    engine      = self._normalize_engine_route_value(self.engine_var.get())
    if hasattr(self, "engine_var") and self.engine_var.get() != engine:
        # tkinter 變數只能在主執行緒寫入（本函式跑在翻譯 worker 上）
        self.root.after(0, lambda: self.engine_var.set(engine))
    max_workers = max(1, min(32, self.workers_var.get()))

    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    # 連線池放大到 worker 數以上，避免 16 並行時超額連線被丟棄、重複 TLS 握手
    _adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=40)
    session.mount('https://', _adapter)
    session.mount('http://', _adapter)

    counter_lock  = threading.Lock()
    completed_ref = [0]
    translate_start = time.time()
    speed_samples = deque(maxlen=12)
    self._progress_started_at = translate_start

    def atomic_progress(n, translated=0, skipped=0, errors=0):
        with counter_lock:
            completed_ref[0] += n
            if translated or skipped or errors:
                self._add_progress_counts(translated, skipped, errors)
            val = completed_ref[0]
        self.update_progress(val, total, text_mode=True)
        self.set_current_item(f"翻譯中：{val:,}/{total:,} 筆")
        return val

    # ── 讀取所有引擎設定 ──
    google_key     = self.api_key_var.get().strip()
    deepl_key      = self.deepl_key_var.get().strip()
    azure_key      = self.azure_key_var.get().strip()
    azure_region   = self.azure_region_var.get().strip() or "eastasia"
    azure_endpoint = self.azure_endpoint_var.get().strip().rstrip('/')
    claude_key     = self.claude_key_var.get().strip()
    claude_model   = self.claude_model_var.get().strip() or "claude-haiku-4-5-20251001"
    openai_key     = self.openai_key_var.get().strip()
    openai_model   = self.openai_model_var.get().strip() or "gpt-5.4-mini"
    local_url      = self.local_url_var.get().strip()
    ai_provider_key = self._ai_provider_key()
    ai_provider_cfg = self._ai_provider_config()
    ai_auth_mode   = self.ai_auth_mode_var.get().strip() or "api"
    ai_api_keys    = self._ai_api_key_list()
    ai_api_key     = ai_api_keys[0] if ai_api_keys else ""
    ai_model       = self.ai_model_var.get().strip() or (
        ai_provider_cfg.get("models", ("local-model",))[0])
    ai_base_url    = self.ai_base_url_var.get().strip() or ai_provider_cfg.get("base_url", "")
    ai_label       = ai_provider_cfg.get("label", "AI")
    if (self.auto_normalize_endpoint_var.get()
            and ai_provider_cfg.get("api_type", "openai_compatible") == "openai_compatible"):
        normalized_url = normalize_base_url(ai_base_url, "openai_compatible")
        if normalized_url and normalized_url != ai_base_url:
            ai_base_url = normalized_url
            # tkinter 變數只能在主執行緒寫入（此處跑在翻譯 worker 上）
            self.root.after(0, lambda u=normalized_url: self.ai_base_url_var.set(u))
            self.log(f"INFO  Base URL 已自動整理：{normalized_url}")
    market_ai_blocked = False

    # ── 驗證主引擎必要設定 ──
    if engine == "deepl" and not deepl_key:
        self.log("❌ 請填入 DeepL API Key！"); return
    if engine == "azure" and not azure_key:
        self.log("❌ 請填入 Azure Subscription Key！"); return
    if engine == "claude" and not claude_key:
        self.log("❌ 請填入 Anthropic API Key！"); return
    if engine == "openai" and not openai_key:
        self.log("❌ 請填入 OpenAI API Key！"); return
    if engine == "market_ai":
        api_type = ai_provider_cfg.get("api_type", "openai_compatible")
        if ai_auth_mode == "login":
            self.log("ℹ️  登入頁模式會開啟供應商平台取得/管理 Key；批次翻譯仍透過 API 呼叫。")
        if ai_provider_cfg.get("requires_key", True) and not ai_api_keys:
            self.log(f"⚠️ 未填入 {ai_label} API Key，已跳過此雲端模型並改用免費保底引擎。")
            market_ai_blocked = True
        if not ai_model:
            self.log("❌ 請選擇或輸入 AI 模型 ID！")
            return
        if not market_ai_blocked and api_type != "bing" and not ai_base_url:
            self.log("❌ 請填入 API Base URL！")
            return

    azure_url = (f"{azure_endpoint}/translator/text/v3.0/translate"
                 if azure_endpoint
                 else "https://api.cognitive.microsofttranslator.com/translate")

    ENG_LABEL = {
        'gtx':        '⚡ GTX',
        'bing':       '🪟 Bing免費',
        'google_api': '🚀 Google API',
        'deepl':      '⚡ DeepL',
        'azure':      '🌐 Azure',
        'claude':     '🤖 Claude',
        'openai':     '💬 OpenAI',
        'market_ai':  '🌍 市面AI',
        'local':      '🖥️ 本地AI',
        'libretranslate': '🧩 LibreTranslate',
        'mymemory':   '🧠 MyMemory',
    }

    provider_settings = {
        "google_key": google_key,
        "deepl_key": deepl_key,
        "azure_key": azure_key,
        "azure_region": azure_region,
        "azure_url": azure_url,
        "claude_key": claude_key,
        "claude_model": claude_model,
        "openai_key": openai_key,
        "openai_model": openai_model,
        "local_url": local_url,
        "ai_provider_key": ai_provider_key,
        "ai_provider_cfg": ai_provider_cfg,
        "ai_api_key": ai_api_key,
        "ai_api_keys": ai_api_keys,
        "ai_model": ai_model,
        "ai_base_url": ai_base_url,
        "ai_label": ai_label,
        "should_stop": lambda: self.stop_requested,
    }
    ALL_FN = build_provider_registry(session, provider_settings)

    # 將 UI 引擎選擇對應到引擎 ID
    primary_id = {
        'google': 'google_api' if google_key else 'gtx',
        'deepl':  'deepl',
        'azure':  'azure',
        'claude': 'claude',
        'openai': 'openai',
        'market_ai': 'market_ai',
        'non_ai_chain': 'bing',
        'local':  'local',
    }.get(engine, 'gtx')
    if engine == "market_ai" and ai_provider_key == "libretranslate":
        primary_id = "libretranslate"
    elif engine == "market_ai" and ai_provider_key == "bing_free":
        primary_id = "bing"
    elif engine == "market_ai" and market_ai_blocked:
        primary_id = "gtx"

    # 建立引擎池。
    # 付費 API 模式先鎖定主模型；只有 Key/餘額/服務不可用時才啟用備援，
    # 避免使用者指定 DeepSeek/Kimi/OpenAI 等計費模型時混入 Azure/Bing/GTX 譯文。
    free_market_ai = (
        primary_id in ("market_ai", "bing")
        and (
            ai_provider_key in ("deepseek_v4_flash_free", "openrouter_free_router", "openrouter_free_models", "libretranslate", "bing_free", "custom")
            or ai_model.lower() == "openrouter/free"
            or ai_model.lower().endswith(":free")
            or not ai_provider_cfg.get("requires_key", True)
        )
    )
    strict_paid_primary = (
        engine != "non_ai_chain"
        and
        primary_id in ("market_ai", "openai", "claude", "google_api", "azure", "deepl")
        and not free_market_ai
        and not market_ai_blocked
    )

    pool_ids = [primary_id]
    backup_ids = []
    # Argos（離線庫未隨 EXE 打包，永遠不可用）已移除。
    fallback_order = translation_fallback_order(
        engine, primary_id, ai_provider_key,
        azure_available=bool(azure_key))

    for eid in fallback_order:
        if eid not in pool_ids and ALL_FN.get(eid):
            pool_ids.append(eid)
            if strict_paid_primary:
                backup_ids.append(eid)
    if not strict_paid_primary and 'gtx' not in pool_ids:
        pool_ids.append('gtx')

    dispatch = {eid: ALL_FN[eid] for eid in pool_ids if ALL_FN.get(eid)}

    # 動態分批：同時限制項目數與字元量，避免長書頁/lore 造成模型回應截斷。
    chunks = self._make_translation_chunks(
        missing_strings, engine, primary_id, ai_model, _force_chunk_size)
    routed_chunks = [(chunk, _preferred_engine) for chunk in chunks]
    if engine == "non_ai_chain" and primary_id == "bing":
        chain = " → ".join(ENG_LABEL.get(eid, eid) for eid in pool_ids)
        self.log(f"⚡ 非 AI 翻譯鏈：{chain}；限流自動切換")
    if _preferred_engine:
        self.log(f"↪️ 重試批次優先使用 {ENG_LABEL.get(_preferred_engine, _preferred_engine)}")
    chunk_size = max((len(chunk) for chunk, _preferred in routed_chunks), default=1)

    # ── 共享狀態（執行緒安全） ──
    throttle_until  = {e: 0.0 for e in pool_ids}   # 限流解除時間
    error_counts    = {e: 0 for e in pool_ids}     # 連續暫時性錯誤計數
    slow_success_counts = {e: 0 for e in pool_ids} # 成功但過慢的連續批次
    disabled_engines = set()                         # 額度耗盡，永久停用
    inactive_backups = set(backup_ids)                # 付費主 API 可用時不啟用備援
    backup_activated = [not strict_paid_primary]
    eng_lock         = threading.Lock()
    adaptive_controller = None

    def _active_engines_locked():
        return [
            e for e in pool_ids
            if e not in disabled_engines and e not in inactive_backups
        ]

    def _pick(preferred_engine=None):
        """回傳 (可用引擎, 0) 或 (None, 需等待秒數)。
        若所有引擎均已停用則回傳 (None, -1) 作為終止信號。"""
        with eng_lock:
            active = _active_engines_locked()
            if not active:
                return None, -1   # 所有引擎均停用
            now = time.time()
            if (preferred_engine in active
                    and throttle_until.get(preferred_engine, 0.0) <= now):
                return preferred_engine, 0.0
            ready_engine = select_ready_engine(active, throttle_until, now)
            if ready_engine:
                # Keep every worker on the fastest available engine. The old
                # round-robin sent part of each wave to GTX even while Bing was
                # healthy, so the whole wave waited for its slowest GTX chunks.
                # Order in pool_ids is the configured failover priority.
                return ready_engine, 0.0
            wait = min(throttle_until[e] - now for e in active)
            return None, max(1.0, wait)

    def _retry_candidates(current_engine, tried, retry_idx, chunk_data):
        with eng_lock:
            now = time.time()
            return [
                e for e in pool_ids
                if e not in tried
                and e not in disabled_engines
                and e not in inactive_backups
                and throttle_until.get(e, 0.0) <= now
            ]

    def _throttle(eng, sec, *, rate_limited=False, announce=True):
        try:
            sec = max(1.0, float(sec))
        except (TypeError, ValueError):
            sec = 30.0
        if rate_limited:
            sec = engine_rate_limit_cooldown(engine, eng, sec)
        with eng_lock:
            deadline = time.time() + sec
            throttle_until[eng] = max(
                throttle_until.get(eng, 0.0), deadline)
            locked_to_paid_primary = strict_paid_primary and eng == primary_id and not backup_activated[0]
            now = time.time()
            has_ready_alternative = any(
                e != eng
                and e not in disabled_engines
                and e not in inactive_backups
                and throttle_until.get(e, 0.0) <= now
                for e in pool_ids
            )
        if adaptive_controller is not None:
            if locked_to_paid_primary or not has_ready_alternative:
                new_workers = adaptive_controller.on_rate_limit(sec)
                cause = "限流" if rate_limited else "暫時錯誤"
                self.log(f"🐢 自適應{cause}：有效並發降為 {new_workers}")
            else:
                cause = "限流" if rate_limited else "暫時冷卻"
                self.log(
                    f"🚀 {ENG_LABEL.get(eng, eng)} {cause}，"
                    "但已有下一個引擎可用；維持目前並發")
        if not announce:
            return
        if not rate_limited:
            if locked_to_paid_primary:
                self.log(
                    f"⚠️ {ENG_LABEL.get(eng, eng)} 暫時冷卻 {sec:.0f}s "
                    "→ 付費主 API 等待後重試")
            else:
                self.log(
                    f"↪️ {ENG_LABEL.get(eng, eng)} 暫時冷卻 {sec:.0f}s "
                    "→ 自動切換下一個引擎...")
            return
        if locked_to_paid_primary:
            self.log(f"⚠️ {ENG_LABEL.get(eng, eng)} 限流 {sec:.0f}s → 付費主 API 仍可用，等待後重試，不切換備援")
        elif has_ready_alternative:
            self.log(
                f"⚠️ {ENG_LABEL.get(eng, eng)} HTTP 429；"
                f"僅冷卻此引擎 {sec:.0f}s，立即切換下一個，不中斷翻譯")
        else:
            self.log(
                f"⚠️ {ENG_LABEL.get(eng, eng)} HTTP 429；"
                f"目前無可用備援，冷卻 {sec:.0f}s 後重試")

    def _disable(eng, reason):
        with eng_lock:
            disabled_engines.add(eng)
            active_left = _active_engines_locked()
        self.log(f"🚫 {ENG_LABEL.get(eng, eng)} 已停用（{reason}）"
                 f"  剩餘可用引擎：{' → '.join(ENG_LABEL.get(e, e) for e in active_left) or '無'}")

    def _activate_backups(reason):
        if not strict_paid_primary:
            return
        with eng_lock:
            if backup_activated[0]:
                return
            inactive_backups.clear()
            backup_activated[0] = True
            active_left = _active_engines_locked()
        self.log("🧯 付費主 API 無法繼續使用，已啟用備援："
                 f"{' → '.join(ENG_LABEL.get(e, e) for e in active_left if e != primary_id) or '無'}"
                 f"（原因：{reason}）")

    def _paid_primary_unusable(err):
        if err.startswith("DISABLED:"):
            return True
        lower = err.lower()
        return err.startswith("ERR:") and any(kw in lower for kw in (
            "http 500", "http 502", "http 503", "http 504",
            "service unavailable", "temporarily unavailable",
            "connection", "timeout", "timed out", "連線失敗",
            "network", "max retries", "incomplete translation response",
        ))

    # 顯示引擎池資訊
    if strict_paid_primary and backup_ids:
        self.log(f"🔄 付費主 API 模式：{ENG_LABEL.get(primary_id, primary_id)}")
        self.log("   備援僅在主 API 無法使用或餘額/額度不足時啟用："
                 f"{'  →  '.join(ENG_LABEL.get(e, e) for e in backup_ids)}")
    else:
        self.log(f"🔄 智能引擎池：{'  →  '.join(ENG_LABEL.get(e, e) for e in pool_ids)}")
    if engine == "azure" and azure_key:
        masked = azure_key[:4] + "****" + azure_key[-4:] if len(azure_key) >= 8 else "****"
        self.log(f"🔑 Azure Key: {masked}  端點: {azure_url}")

    workers = translation_worker_limit(max_workers, primary_id, engine)
    if workers != max_workers:
        self.log(f"⚙️ 翻譯並發：{workers}/{max_workers}（主引擎：{ENG_LABEL.get(primary_id, primary_id)}）")
    adaptive_controller = AdaptiveConcurrency(workers, minimum=1, maximum=workers)
    engine_gates = {
        engine_id: threading.BoundedSemaphore(
            engine_concurrency_limit(workers, engine_id))
        for engine_id in dispatch
    }
    _ENGINE_RESELECT = object()

    def _dispatch_with_limit(engine_id, payload):
        gate = engine_gates[engine_id]
        while not self.stop_requested:
            if not gate.acquire(timeout=0.2):
                continue
            try:
                # A queued worker may have selected this provider before another
                # request throttled or disabled it. Re-select instead of sending
                # the rest of the queued burst into the same cooldown window.
                with eng_lock:
                    unavailable = (
                        engine_id in disabled_engines
                        or engine_id in inactive_backups
                        or throttle_until.get(engine_id, 0.0) > time.time()
                    )
                if unavailable:
                    return None, _ENGINE_RESELECT
                return dispatch[engine_id](payload)
            finally:
                gate.release()
        return None, _ENGINE_RESELECT

    def _incomplete_result_error(eng):
        prefix = ("DISABLED:" if strict_paid_primary and eng == primary_id
                  else "ERR:")
        return (f"{prefix}{ENG_LABEL.get(eng, eng)} "
                "incomplete translation response")

    def process_chunk_smart(chunk_data, preferred_engine=None):
        _MAX_CUMULATIVE_WAIT = translation_wait_budget(
            engine, strict_paid_primary)
        _cumulative_wait = 0.0
        for _ in range(60):
            if self.stop_requested:
                return chunk_data, None, None
            eng, wait = _pick(preferred_engine)
            if eng is None:
                if wait == -1:
                    return chunk_data, None, "所有翻譯引擎已停用（額度耗盡或 Key 無效），略過此批次"
                if _cumulative_wait + wait > _MAX_CUMULATIVE_WAIT:
                    self.log(f"⏱️ 累計等待已達 {_cumulative_wait:.0f}s 上限，放棄此批次")
                    return chunk_data, None, f"所有引擎持續限流超過 {_MAX_CUMULATIVE_WAIT}s，略過此批次"
                _cumulative_wait += wait
                self.log(f"⏳ 所有引擎限流，等待 {wait:.0f}s...（累計 {_cumulative_wait:.0f}s/{_MAX_CUMULATIVE_WAIT}s）")
                if not interruptible_sleep(
                        lambda: self.stop_requested,
                        wait + random.uniform(0, 1)):
                    return chunk_data, None, None
                continue
            # ── 格式保護遮罩：翻譯前替換，翻譯後還原 ──
            masked_chunk = []
            mappings     = []
            for t in chunk_data:
                m, mp = self._mask_format(t)
                masked_chunk.append(m)
                mappings.append(mp)
            request_started = time.monotonic()
            trans, err = _dispatch_with_limit(eng, masked_chunk)
            request_elapsed = time.monotonic() - request_started
            if err is _ENGINE_RESELECT:
                continue
            if err is None and not _translation_result_is_complete(
                    chunk_data, trans):
                err = _incomplete_result_error(eng)
            if err is None:
                # 成功 → 清除此引擎的連續錯誤計數
                with eng_lock:
                    error_counts[eng] = 0
                soft_cooldown = soft_throttle_after_success(
                    engine, eng, len(chunk_data), request_elapsed)
                if soft_cooldown:
                    with eng_lock:
                        slow_success_counts[eng] = slow_success_counts.get(eng, 0) + 1
                        slow_count = slow_success_counts[eng]
                    if slow_count >= 2:
                        rate = len(chunk_data) / max(0.001, request_elapsed)
                        self.log(
                            f"🐢 {ENG_LABEL.get(eng, eng)} 回應過慢"
                            f"（約 {rate:.1f} 詞/秒）→ 冷卻 {soft_cooldown:.0f}s，切換下一個引擎")
                        _throttle(eng, soft_cooldown, announce=False)
                        with eng_lock:
                            slow_success_counts[eng] = 0
                else:
                    with eng_lock:
                        slow_success_counts[eng] = 0
                # 還原格式符號
                if trans:
                    trans = [self._unmask_format(t, mp)
                             for t, mp in zip(trans, mappings)]
                    # ── 專有名詞拒翻補救 ──
                    # Bing/Azure 對 "Ascendant Azure Leggings" 這類標題式名稱
                    # 可能會成功地原樣返回。只在小量殘留時即時補救；大量原樣返回
                    # 若整批同步丟給備援，主翻譯波會被慢引擎拖到 20~40 詞/秒。
                    # 大量殘留交給後續驗證重試處理，讓 Bing 主波先維持高吞吐。
                    if not strict_paid_primary:
                        retry_idx = [
                            i for i, t in enumerate(trans)
                            if t and t.strip() == chunk_data[i].strip()
                            and not self._RE_CJK_CHAR.search(chunk_data[i])
                        ]
                        if (engine == "non_ai_chain" and eng == "bing"
                                and preferred_engine is None):
                            # 主波不要在每個 Bing 批次內同步補救專有名詞；
                            # 這會把 100+ 詞/秒的主波拖進慢引擎。未翻項目
                            # 會由重試階段整批切 Azure/GTX 處理。
                            retry_idx = []
                        elif engine == "non_ai_chain" and eng == "bing" and len(retry_idx) > 10:
                            retry_idx = []
                        tried = {eng}
                        hops = 0
                        # 上限要蓋住整條鏈，確保名稱類字串能交給其他引擎補翻。
                        while retry_idx and hops < 4 and not self.stop_requested:
                            candidates = _retry_candidates(
                                eng, tried, retry_idx, chunk_data)
                            if not candidates:
                                break
                            nxt = candidates[0]
                            tried.add(nxt)
                            hops += 1
                            sub_trans, sub_err = _dispatch_with_limit(
                                nxt,
                                [masked_chunk[i] for i in retry_idx])
                            if sub_err is _ENGINE_RESELECT:
                                continue
                            if sub_err or not sub_trans:
                                if sub_err and sub_err.startswith("429:"):
                                    try:
                                        _throttle(
                                            nxt, float(sub_err[4:]),
                                            rate_limited=True)
                                    except ValueError:
                                        _throttle(
                                            nxt, 30.0, rate_limited=True)
                                elif sub_err and sub_err.startswith("DISABLED:"):
                                    _disable(nxt, sub_err[9:])
                                continue
                            for j, i in enumerate(retry_idx):
                                cand = sub_trans[j] if j < len(sub_trans) else None
                                if cand:
                                    cand = self._unmask_format(cand, mappings[i])
                                if cand and cand.strip() and cand.strip() != chunk_data[i].strip():
                                    trans[i] = cand
                            retry_idx = [i for i in retry_idx
                                         if trans[i] and trans[i].strip() == chunk_data[i].strip()]
                return chunk_data, trans, None
            timeout_err = 'timed out' in err.lower() or 'timeout' in err.lower()
            # AI/API 路線保留切批救援；非 AI 鏈路改為立刻 failover，
            # 避免 Bing/Azure/GTX 逾時時把 worker 全部卡在同引擎重試。
            if (timeout_err and len(chunk_data) > 1
                    and should_split_timeout_batch(engine, eng, len(chunk_data))):
                mid = len(chunk_data) // 2
                self.log(f"⏱️ {ENG_LABEL.get(eng, eng)} 批次逾時 → "
                         f"對半切批重試（{len(chunk_data)} → {mid}+{len(chunk_data) - mid}）")
                merged = []
                split_ok = True
                for lo, hi in ((0, mid), (mid, len(chunk_data))):
                    sub_chunk = chunk_data[lo:hi]
                    sub_trans, sub_err = _dispatch_with_limit(
                        eng, masked_chunk[lo:hi])
                    if sub_err is _ENGINE_RESELECT:
                        split_ok = False
                        err = _ENGINE_RESELECT
                        break
                    if sub_err:
                        err = sub_err
                        split_ok = False
                        break
                    if not _translation_result_is_complete(
                            sub_chunk, sub_trans):
                        err = _incomplete_result_error(eng)
                        split_ok = False
                        break
                    merged.extend(sub_trans)
                if split_ok and _translation_result_is_complete(
                        chunk_data, merged):
                    with eng_lock:
                        error_counts[eng] = 0
                    trans = [self._unmask_format(t, mp)
                             for t, mp in zip(merged, mappings)]
                    return chunk_data, trans, None
                if err is _ENGINE_RESELECT:
                    continue
                # 切批仍失敗 → 落回一般錯誤處理（冷卻/計數）
            elif (timeout_err
                  and not (strict_paid_primary and eng == primary_id)):
                wait_seconds = 8.0 if eng == "bing" else 12.0 if eng == "azure" else 20.0
                self.log(f"⏱️ {ENG_LABEL.get(eng, eng)} 批次逾時 → "
                         f"冷卻 {wait_seconds:.0f}s 並切換下一個引擎")
                _throttle(eng, wait_seconds, announce=False)
                continue

            if err.startswith("429:"):
                try:
                    _throttle(
                        eng, float(err[4:]), rate_limited=True)
                except ValueError:
                    _throttle(eng, 30.0, rate_limited=True)
                continue
            if err.startswith("DISABLED:"):
                if strict_paid_primary and eng == primary_id:
                    _activate_backups(err[9:])
                _disable(eng, err[9:])
                # 停用後立刻重試（換下一個引擎）
                continue
            if strict_paid_primary and eng == primary_id:
                if _paid_primary_unusable(err):
                    reason = err.lstrip("ERR:")
                    with eng_lock:
                        # 引擎已在冷卻中 → 這是同一波網路抖動的並行失敗，
                        # 不重複計數（否則 8 個 in-flight 請求一次抖動就湊滿 3 次）
                        if time.time() < throttle_until.get(eng, 0.0):
                            continue
                        error_counts[eng] += 1
                        failures = error_counts[eng]
                    if failures >= 3:
                        _activate_backups(reason)
                        cooldown = min(60.0, 20.0 * failures)
                        self.log(
                            f"⚠️ {ENG_LABEL.get(eng, eng)} 暫時性錯誤"
                            f"（連續 {failures} 次）：{reason} → 已啟用備援，"
                            f"冷卻 {cooldown:.0f}s 後自動恢復探測")
                        _throttle(eng, cooldown, announce=False)
                    else:
                        self.log(f"⚠️ {ENG_LABEL.get(eng, eng)} 暫時性錯誤"
                                 f"（{failures}/3）：{reason} → 冷卻 20s 後重試")
                        _throttle(eng, 20.0, announce=False)
                    continue
                return chunk_data, None, err
            # 暫時性錯誤只開啟有期限的熔斷。只有供應商明確回傳
            # DISABLED 才能在整場任務停用，避免短暫網路故障永久降級。
            with eng_lock:
                # 同一波抖動的並行失敗不重複計數（引擎已在冷卻中）
                if time.time() < throttle_until.get(eng, 0.0):
                    continue
                error_counts[eng] += 1
                failures = error_counts[eng]
            cooldown = min(60.0, 15.0 * max(1, failures))
            self.log(
                f"⚠️ {ENG_LABEL.get(eng, eng)} 暫時性錯誤"
                f"（連續 {failures} 次）：{err.lstrip('ERR:')[:60]} → "
                f"冷卻 {cooldown:.0f}s 後自動恢復探測")
            _throttle(eng, cooldown, announce=False)
            continue
        return chunk_data, None, "所有引擎重試均失敗，略過此批次"

    with stoppable_executor(workers, lambda: self.stop_requested) as executor:
        chunk_iter = iter(routed_chunks)
        futures = {}

        def submit_until_limit():
            while (not self.stop_requested
                   and len(futures) < adaptive_controller.effective_workers()):
                try:
                    c, preferred_engine = next(chunk_iter)
                except StopIteration:
                    break
                futures[executor.submit(
                    process_chunk_smart, c, preferred_engine)] = (c, preferred_engine)

        def cancel_pending_futures():
            for f in list(futures.keys()):
                f.cancel()
            futures.clear()
            self._shutdown_executor_now(executor)

        submit_until_limit()
        while futures:
            if self.stop_requested:
                cancel_pending_futures()
                break
            done_set, _pending = concurrent.futures.wait(
                futures, timeout=0.2,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if self.stop_requested:
                cancel_pending_futures()
                break
            if not done_set:
                continue
            for future in done_set:
                origin_chunk, origin_preferred = futures.pop(future, ([], None))
                if self.stop_requested:
                    cancel_pending_futures()
                    break
                try:
                    chunk_data, translations, err = future.result()
                except Exception as e:
                    adaptive_controller.on_error()
                    self.log(f"⚠️ 執行緒錯誤: {e}")
                    failed_count = len(origin_chunk)
                    atomic_progress(failed_count, skipped=failed_count, errors=1)
                    continue
                if self.stop_requested:
                    cancel_pending_futures()
                    break
                if err:
                    adaptive_controller.on_error()
                    self.log(f"❌ {err}")
                    failed_count = len(chunk_data) if chunk_data else len(origin_chunk)
                    atomic_progress(failed_count, skipped=failed_count, errors=1)
                    continue
                adaptive_controller.on_success()
                changed_pairs = []
                if translations:
                    for orig, trans in zip(chunk_data, translations):
                        if trans and trans.strip():
                            validated = self.validate_translation(
                                orig, self._apply_dictionary_fixes(
                                    orig, self.fix_placeholders(trans)))
                            # 僅快取實際翻譯結果；validate 回傳原文代表格式碼不符或超長，不入快取
                            # 注意：validate 不做繁體驗證，只做格式/長度驗證；
                            # 繁體驗證在 _review_and_fix_cache / load_cache 階段做，
                            # 避免把「純共用字」翻譯（如：石頭、木頭）誤判為無效而不存入快取
                            if validated and validated.strip() and validated != orig:
                                changed_pairs.append((orig, validated))
                            elif validated == orig:
                                # validate 回傳原文：格式代碼數量不符或長度異常
                                # 直接存原文做佔位，讓此批次進度正常推進；
                                # _review_and_fix_cache 會清掉這些「翻譯==原文」條目讓下次重試
                                pass  # 不存入，下次重試
                translated_count = 0
                if changed_pairs:
                    bulk_update = getattr(self.translation_cache, "bulk_update", None)
                    if callable(bulk_update):
                        try:
                            bulk_update(changed_pairs, sync=False)
                        except TypeError:
                            bulk_update(changed_pairs)
                    else:
                        for orig, validated in changed_pairs:
                            self.translation_cache[orig] = validated
                    accepted_pairs = [
                        (orig, validated)
                        for orig, validated in changed_pairs
                        if self.translation_cache.get(orig) == validated
                    ]
                    translated_count = len(accepted_pairs)
                    if accepted_pairs:
                        self._session_translated_keys = getattr(
                            self, "_session_translated_keys", set())
                        self._session_translated_keys.update(
                            orig for orig, _ in accepted_pairs)
                processed_count = len(chunk_data) if chunk_data else 0
                skipped_count = max(0, processed_count - translated_count)
                if self.stop_requested:
                    cancel_pending_futures()
                    break
                done = atomic_progress(
                    processed_count, translated=translated_count,
                    skipped=skipped_count)
                # One line per completed chunk keeps the record panel visibly
                # synchronized without producing one UI event per translated item.
                if done > 0:
                    now = time.time()
                    with eng_lock:
                        active_snapshot = _active_engines_locked()
                    cur = (origin_preferred
                           if origin_preferred in active_snapshot
                           and throttle_until.get(origin_preferred, 0.0) <= now
                           else next((e for e in active_snapshot
                                      if throttle_until[e] <= now),
                                     active_snapshot[-1]
                                     if active_snapshot else primary_id))
                    sample = (str(chunk_data[0])[:20].replace('\n', ' ')
                              if chunk_data else "")
                    elapsed = now - translate_start
                    speed_samples.append((now, done))
                    if len(speed_samples) >= 2:
                        sample_elapsed = speed_samples[-1][0] - speed_samples[0][0]
                        sample_done = speed_samples[-1][1] - speed_samples[0][1]
                        speed = (sample_done / sample_elapsed
                                 if sample_elapsed > 0 and sample_done > 0
                                 else 0.0)
                    else:
                        speed = done / elapsed if elapsed > 0 and done > 0 else 0.0
                    if speed > 0:
                        eta_sec = max(0, (total - done) / speed)
                        eta_mm  = int(eta_sec) // 60
                        eta_ss  = int(eta_sec) % 60
                        eta_str = f"  ⚡{speed:.1f}詞/秒  ⏱剩餘{eta_mm:02d}:{eta_ss:02d}"
                    else:
                        eta_str = ""
                    self.log(f"{ENG_LABEL.get(cur, cur)} [{done}/{total}]{eta_str} (例: {sample}...)")
                self._maybe_save_cache()
                if self.stop_requested:
                    break
            if not self.stop_requested:
                submit_until_limit()
