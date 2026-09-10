import html
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests


MC_PROMPT = (
    "你是 Minecraft 模組在地化專家。將以下 JSON 的每個值從英文（或簡體中文）翻譯成繁體中文（zh-TW）。\n"
    "規則：\n"
    "1. 只翻譯值，不改動鍵名。\n"
    "2. 保留所有 %s %d %1$s §a 等格式化符號，位置與數量必須與原文完全一致。\n"
    "3. 只回傳合法 JSON，不要 markdown 包裝。\n"
    "4. 所有英文單詞都必須翻譯，包含遊戲專有的虛構名詞（如 Jellyshroom、Withering、Glowshroom 等），"
    "可使用意譯或音譯，不得保留任何英文原文。\n\n"
    "輸入：{}"
)


GTX_MINECRAFT_GLOSSARY = {
    "stone": "石頭",
    "dirt": "泥土",
    "grass block": "草方塊",
    "cobblestone": "鵝卵石",
    "oak planks": "橡木材",
    "oak log": "橡木原木",
    "spruce log": "杉木原木",
    "birch log": "樺木原木",
    "iron ingot": "鐵錠",
    "gold ingot": "金錠",
    "copper ingot": "銅錠",
    "diamond": "鑽石",
    "emerald": "綠寶石",
    "coal": "煤炭",
    "redstone": "紅石",
    "lapis lazuli": "青金石",
}

# Bing/Microsoft Translator v3 accepts much larger JSON arrays. Keep the batch
# below the documented ceiling, but avoid the old 50-item split that doubled
# request count for the app's 100-item chunks.
BING_BATCH_SIZE = 320
AZURE_BATCH_SIZE = 100
AZURE_BATCH_CHARS = 45_000
GTX_GATE_INTERVAL = 0.05
GTX_BATCH_SIZE = 128
GTX_BATCH_CHARS = 9000
GTX_SINGLETON_WORKERS = 12


def parse_retry_after_seconds(value, default=10.0, now=None):
    """Parse Retry-After seconds or an HTTP date without leaking errors."""
    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = 10.0
    if not math.isfinite(fallback) or fallback <= 0:
        fallback = 10.0

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None and math.isfinite(seconds):
        return max(1.0, seconds)

    try:
        retry_at = parsedate_to_datetime(str(value).strip())
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = time.time() if now is None else float(now)
        seconds = retry_at.timestamp() - current
        if math.isfinite(seconds):
            return max(1.0, seconds)
    except (TypeError, ValueError, OverflowError):
        pass
    return fallback


def parse_gtx_numbered_batch(text, expected_count):
    """Parse GTX output framed with stable numbered markers.

    Natural-language separators such as ``|~|`` are sometimes removed or
    merged by Google Translate, which caused recursive batch splitting and a
    severe throughput collapse. Alphanumeric markers survive translation and
    let us restore every item by index.
    """
    matches = list(re.finditer(r'\[\[\s*MCT\s*(\d{3,6})\s*\]\]', text or '', re.I))
    if len(matches) != expected_count:
        return None
    results = [None] * expected_count
    for pos, match in enumerate(matches):
        idx = int(match.group(1))
        if idx < 0 or idx >= expected_count or results[idx] is not None:
            return None
        end = matches[pos + 1].start() if pos + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        if not value:
            return None
        results[idx] = value
    return results if all(results) else None


def apply_gtx_glossary(source, translated):
    fixed = GTX_MINECRAFT_GLOSSARY.get((source or "").strip().lower())
    return fixed if fixed else translated


def normalize_base_url(base_url, api_type="openai_compatible"):
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    url = re.sub(r"\s+", "", url)
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    path = re.sub(r"/+", "/", parts.path or "").rstrip("/")
    lower_path = path.lower()
    for suffix in ("/chat/completions", "/models"):
        if lower_path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            lower_path = path.lower()
    if api_type == "openai_compatible":
        if not lower_path.endswith(("/v1", "/v1beta", "/v1beta/openai", "/api/v1")):
            path = (path + "/v1").rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def chat_completions_url(base_url):
    url = normalize_base_url(base_url, "openai_compatible")
    if not url:
        return ""
    if url.endswith("/chat/completions") or ":generateContent" in url:
        return url
    return url + "/chat/completions"



def reconcile_market_ai_route(provider_key, provider_cfg, base_url, label):
    """Align provider_key/api_type/label to Base URL host when it is a known API.

    UI selection can desync from a manually edited (or previously saved) Base URL
    — e.g. Anthropic protocol + DeepSeek URL/key → DISABLED Anthropic auth, then
    strict_paid backup falls through to GTX. When the URL host clearly belongs to
    a known vendor, force the matching route even if the UI key disagrees.
    Unknown / private / custom URLs are left unchanged.
    """
    cfg = dict(provider_cfg or {})
    key = (provider_key or "").strip() or "custom"
    resolved_label = (label or cfg.get("label") or key or "AI").strip() or "AI"
    raw = (base_url or cfg.get("base_url") or "").strip()
    if not raw:
        return key, cfg, resolved_label

    try:
        host = (urlsplit(raw if "://" in raw else f"https://{raw}").hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return key, cfg, resolved_label

    # (host needle, provider_key, api_type, label)
    rules = (
        ("api.anthropic.com", "anthropic", "anthropic", "Anthropic Claude"),
        ("api.deepseek.com", "deepseek", "openai_compatible", "DeepSeek"),
        ("api.openai.com", "openai", "openai_compatible", "OpenAI"),
        ("api.moonshot.", "kimi", "openai_compatible", "Kimi / Moonshot"),
        ("generativelanguage.googleapis.com", "gemini", "gemini", "Google Gemini"),
        ("openrouter.ai", "openrouter", "openai_compatible", "OpenRouter"),
        ("api.x.ai", "grok", "openai_compatible", "xAI Grok"),
        ("api.groq.com", "groq", "openai_compatible", "Groq"),
        ("api.mistral.ai", "mistral", "openai_compatible", "Mistral"),
        ("dashscope.aliyuncs.com", "qwen", "openai_compatible", "Qwen / DashScope"),
        ("dashscope-intl.aliyuncs.com", "qwen", "openai_compatible", "Qwen / DashScope"),
        ("api.perplexity.ai", "perplexity", "openai_compatible", "Perplexity"),
        ("api.together.xyz", "together", "openai_compatible", "Together AI"),
        ("together.ai", "together", "openai_compatible", "Together AI"),
        ("api.fireworks.ai", "fireworks", "openai_compatible", "Fireworks"),
        ("api.xiaomimimo.com", "xiaomi_mimo", "openai_compatible", "Xiaomi MiMo"),
    )
    for needle, forced_key, api_type, forced_label in rules:
        if needle in host:
            cfg["api_type"] = api_type
            cfg["label"] = forced_label
            # Known vendor hosts always need a key for paid routing.
            if "requires_key" in cfg:
                cfg["requires_key"] = True
            return forced_key, cfg, forced_label
    return key, cfg, resolved_label


def extract_openai_message_text(message):
    """Prefer message.content; fall back to reasoning_content (DeepSeek reasoner-style)."""
    if isinstance(message, dict):
        content = extract_ai_text(message.get("content"))
        if isinstance(content, str) and content.strip():
            return content
        reasoning = message.get("reasoning_content")
        if reasoning:
            return extract_ai_text(reasoning)
        return content if isinstance(content, str) else ""
    return extract_ai_text(message)


def extract_ai_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif item.get("type") == "text" and "content" in item:
                    parts.append(str(item["content"]))
        return "".join(parts)
    return "" if content is None else str(content)


def make_chat_body(model, chunk_dict, max_out=4096, token_param="max_tokens",
                   extra_body=None):
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": MC_PROMPT.format(json.dumps(chunk_dict, ensure_ascii=False)),
        }],
    }
    if token_param == "max_completion_tokens":
        body["max_completion_tokens"] = max_out
    else:
        body["temperature"] = 0.1
        body["max_tokens"] = max_out
    if isinstance(extra_body, dict):
        body.update(extra_body)
    return body


def model_batch_limit(model_name, default=20):
    model = (model_name or "").lower()
    if any(k in model for k in ("claude", "sonnet", "opus")):
        return 80
    if any(k in model for k in ("gpt-5", "gpt-4", "o4", "o3")):
        return 50
    if any(k in model for k in ("deepseek", "kimi", "moonshot", "grok", "qwen")):
        # Smaller batches avoid output max_tokens truncation on long Minecraft strings.
        return 16
    return default


def _strip_code_fence(content):
    text = (content or "").strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text).strip()
    return text


def _extract_json_object_text(content):
    """Return the first JSON object/array text from a model response."""
    text = _strip_code_fence(content)
    if not text:
        raise json.JSONDecodeError("empty model content", text, 0)
    decoder = json.JSONDecoder()
    starts = []
    for marker in ("{", "["):
        pos = text.find(marker)
        if pos >= 0:
            starts.append(pos)
    if not starts:
        raise json.JSONDecodeError("no JSON object found in model content", text, 0)
    start = min(starts)
    obj, end = decoder.raw_decode(text[start:])
    return text[start:start + end], obj


def _parse_translation_json(content, expected_count):
    json_text, parsed = _extract_json_object_text(content)
    if not isinstance(parsed, dict):
        raise TypeError("model JSON response must be an object")
    for index in range(expected_count):
        key = str(index)
        if key not in parsed:
            raise TypeError(
                f"model JSON response is missing expected translation key {key!r}")
        if not isinstance(parsed[key], str):
            raise TypeError(
                f"model JSON response value for key {key!r} must be a string")
    return parsed


def ai_chunk(session, chunk_data, post_url, req_headers, make_body,
             parse_resp, eng_name, request_timeout=(10, 150), max_batch=20):
    """Shared JSON-in/JSON-out translator for LLM providers."""
    max_batch = max(1, int(max_batch or 20))
    if len(chunk_data) > max_batch:
        results = []
        for i in range(0, len(chunk_data), max_batch):
            sub = chunk_data[i:i + max_batch]
            sub_res, err = ai_chunk(session, sub, post_url, req_headers,
                                    make_body, parse_resp, eng_name,
                                    request_timeout, max_batch)
            if err:
                return None, err
            results.extend(sub_res)
        return results, None

    def _split_and_retry(reason):
        """On truncation / incomplete JSON, halve the batch instead of failing the wave."""
        if len(chunk_data) <= 1:
            return None, reason
        mid = max(1, len(chunk_data) // 2)
        left, err = ai_chunk(session, chunk_data[:mid], post_url, req_headers,
                             make_body, parse_resp, eng_name,
                             request_timeout, max_batch)
        if err:
            return None, err
        right, err = ai_chunk(session, chunk_data[mid:], post_url, req_headers,
                              make_body, parse_resp, eng_name,
                              request_timeout, max_batch)
        if err:
            return None, err
        return left + right, None

    chunk_dict = {str(i): t for i, t in enumerate(chunk_data)}
    try:
        res = session.post(post_url, headers=req_headers,
                           json=make_body(chunk_dict), timeout=request_timeout)
        if res.status_code == 200:
            payload = res.json()
            finish_reason = ""
            try:
                finish_reason = str(payload.get("choices", [{}])[0].get("finish_reason") or "")
            except Exception:
                finish_reason = ""
            content = parse_resp(payload).strip()
            if not content:
                try:
                    msg = payload.get("choices", [{}])[0].get("message") or {}
                    content = extract_openai_message_text(msg).strip()
                except Exception:
                    content = content or ""
            if finish_reason.lower() in ("length", "max_tokens"):
                return _split_and_retry(
                    f"ERR:{eng_name} 模型輸出被截斷，請降低執行緒/批次或提高 max_tokens")
            try:
                trans_dict = _parse_translation_json(content, len(chunk_data))
            except (json.JSONDecodeError, TypeError) as parse_err:
                detail = str(parse_err)
                if len(chunk_data) > 1 and any(
                        kw in detail for kw in (
                            "Unterminated string", "Expecting value",
                            "no JSON object", "missing expected translation key",
                            "empty model content")):
                    return _split_and_retry(
                        f"ERR:{eng_name} JSON 解析失敗: {detail}")
                raise
            return [trans_dict[str(i)]
                    for i in range(len(chunk_data))], None
        if res.status_code == 429:
            return None, "429:60"
        if res.status_code == 529:
            return None, "429:30"
        if res.status_code == 402:
            return None, f"DISABLED:{eng_name} 帳戶餘額不足，請加值"

        body_lower = res.text.lower()
        if res.status_code in (401, 403):
            if any(kw in body_lower for kw in ('permission', 'license', 'credits')):
                return None, f"DISABLED:{eng_name} 帳號沒有 API credits/licenses，請到供應商 Console 加值或啟用授權"
            return None, f"DISABLED:{eng_name} API Key 無效或無授權"
        if any(kw in body_lower for kw in
               ('insufficient_quota', 'insufficient_credits',
                'credit balance', 'exceeded your', 'out of credits',
                'billing', 'payment required', 'credits or licenses',
                'doesn\\u0027t have any credits', "doesn't have any credits")):
            return None, f"DISABLED:{eng_name} API 額度已用盡"
        return None, f"ERR:{eng_name} HTTP {res.status_code}: {res.text[:100]}"
    except json.JSONDecodeError as e:
        detail = str(e)
        if "Unterminated string" in detail or "Expecting value" in detail:
            return None, f"ERR:{eng_name} 回應不是完整 JSON（可能被截斷或模型未遵守 JSON 輸出）: {detail}"
        return None, f"ERR:{eng_name} JSON 解析失敗: {detail}"
    except (requests.RequestException, KeyError, IndexError, TypeError) as e:
        return None, f"ERR:{eng_name} 錯誤: {e}"


def _openrouter_headers(headers, provider_key, base_url):
    if provider_key == "openrouter" or "openrouter.ai" in (base_url or ""):
        headers["HTTP-Referer"] = "https://localhost/minecraft-translator"
        headers["X-Title"] = "MinecraftTranslatorGUI"
    return headers


def build_provider_registry(session, settings):
    """Build provider callables matching the legacy (chunk_data) -> (translations, err) API."""
    google_key = settings.get("google_key", "")
    deepl_key = settings.get("deepl_key", "")
    azure_key = settings.get("azure_key", "")
    azure_region = settings.get("azure_region", "eastasia")
    azure_url = settings.get("azure_url", "")
    claude_key = settings.get("claude_key", "")
    claude_model = settings.get("claude_model", "claude-3-5-haiku-20241022")
    openai_key = settings.get("openai_key", "")
    openai_model = settings.get("openai_model", "gpt-5-mini")
    local_url = settings.get("local_url", "")
    ai_provider_key = settings.get("ai_provider_key", "")
    ai_provider_cfg = settings.get("ai_provider_cfg", {})
    ai_api_key = settings.get("ai_api_key", "")
    ai_api_keys = settings.get("ai_api_keys", None) or []
    ai_model = settings.get("ai_model", "")
    ai_base_url = settings.get("ai_base_url", "")
    ai_label = settings.get("ai_label", "AI")
    should_stop = settings.get("should_stop", lambda: False)
    ai_api_keys = [str(k).strip() for k in ai_api_keys if str(k).strip()]
    if ai_api_key and ai_api_key not in ai_api_keys:
        ai_api_keys.insert(0, ai_api_key)
    ai_key_lock = threading.Lock()
    ai_key_cursor = [0]
    ai_key_disabled = set()

    def next_ai_key():
        if not ai_api_keys:
            return "", 0
        with ai_key_lock:
            available = [
                i for i, key in enumerate(ai_api_keys)
                if key and i not in ai_key_disabled
            ]
            if not available:
                return "", -1
            pos = ai_key_cursor[0] % len(available)
            idx = available[pos]
            ai_key_cursor[0] = (pos + 1) % max(1, len(available))
            return ai_api_keys[idx], idx

    def disable_ai_key(idx):
        if idx >= 0:
            with ai_key_lock:
                ai_key_disabled.add(idx)

    thread_local = threading.local()

    def http_session():
        """Return a worker-local HTTP session.

        requests.Session is not designed as a high-concurrency shared object.
        A single global session made Bing/Azure workers contend for connection
        state and could drag throughput from 100+ items/s down to 20~30.
        """
        local_session = getattr(thread_local, "session", None)
        if local_session is not None:
            return local_session
        local_session = requests.Session()
        try:
            local_session.headers.update(getattr(session, "headers", {}) or {})
        except Exception:
            local_session.headers.update({'User-Agent': 'Mozilla/5.0'})
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8,
            pool_maxsize=16,
            pool_block=False,
        )
        local_session.mount('https://', adapter)
        local_session.mount('http://', adapter)
        thread_local.session = local_session
        return local_session

    gtx_lock = threading.Lock()
    gtx_last_req = [0.0]
    gtx_interval = GTX_GATE_INTERVAL
    gtx_worker_count = max(1, int(GTX_SINGLETON_WORKERS or 1))
    gtx_request_gate = threading.BoundedSemaphore(gtx_worker_count)
    gtx_singleton_executor = ThreadPoolExecutor(max_workers=gtx_worker_count)

    def gtx_gate():
        # 鎖內只「預約」下一個發射時間槽，sleep 移到鎖外，
        # 避免多個 worker 全部卡在同一把鎖上序列化等待
        with gtx_lock:
            now = time.time()
            slot = max(now, gtx_last_req[0] + gtx_interval)
            gtx_last_req[0] = slot
        gap = slot - time.time()
        if gap > 0:
            time.sleep(gap)

    def _gtx_batches(items, max_items=GTX_BATCH_SIZE, max_chars=GTX_BATCH_CHARS):
        batch = []
        chars = 0
        for item in items:
            item_len = len(item)
            if batch and (len(batch) >= max_items or chars + item_len > max_chars):
                yield batch
                batch = []
                chars = 0
            batch.append(item)
            chars += item_len + 8
        if batch:
            yield batch

    def _gtx_request(params_fn, text, timeout):
        with gtx_request_gate:
            gtx_gate()
            return http_session().get(
                "https://translate.googleapis.com/translate_a/single",
                params=params_fn(text),
                timeout=timeout)

    def _gtx_batch_translate(batch, params_fn, depth=0):
        if should_stop():
            return [None] * len(batch), None
        if not batch:
            return [], None

        try:
            if len(batch) > 1:
                payload = "\n".join(
                    f"[[MCT{idx:03d}]] {value}" for idx, value in enumerate(batch))
                res = _gtx_request(params_fn, payload, (5, 15))
                if res.status_code == 429:
                    return None, "429:30"
                if res.status_code == 200:
                    raw = html.unescape("".join(p[0] for p in res.json()[0] if p[0]))
                    parts = parse_gtx_numbered_batch(raw, len(batch))
                    if parts:
                        return [apply_gtx_glossary(src, dst)
                                for src, dst in zip(batch, parts)], None
                elif res.status_code not in (413, 414):
                    return None, f"ERR:GTX HTTP {res.status_code}"
            if len(batch) > 1 and depth < 4:
                mid = max(1, len(batch) // 2)
                left, err = _gtx_batch_translate(batch[:mid], params_fn, depth + 1)
                if err:
                    return None, err
                right, err = _gtx_batch_translate(batch[mid:], params_fn, depth + 1)
                if err:
                    return None, err
                return left + right, None

            if len(batch) > 1:
                outcomes = list(gtx_singleton_executor.map(
                    lambda item: _gtx_batch_translate(
                        [item], params_fn, depth + 1),
                    batch,
                ))
                results = []
                for item_result, err in outcomes:
                    if err:
                        return None, err
                    results.append(item_result[0] if item_result else None)
                return results, None

            res = _gtx_request(params_fn, batch[0], (5, 12))
            if res.status_code == 429:
                return None, "429:30"
            if res.status_code == 200:
                text = html.unescape("".join(p[0] for p in res.json()[0] if p[0]))
                translated = text.strip() or None
                return [apply_gtx_glossary(batch[0], translated)], None
            return None, f"ERR:GTX HTTP {res.status_code}"
        except requests.RequestException as e:
            return None, f"ERR:GTX 連線失敗: {e}"
        except (ValueError, TypeError, KeyError, IndexError):
            pass
        return [None] * len(batch), None

    def gtx(chunk_data):
        url = "https://translate.googleapis.com/translate_a/single"
        params_fn = lambda q: {"client": "gtx", "sl": "auto",
                               "tl": "zh-TW", "dt": "t", "q": q}
        results = []
        for batch in _gtx_batches(chunk_data):
            if should_stop():
                results.extend([None] * (len(chunk_data) - len(results)))
                break
            translated, err = _gtx_batch_translate(batch, params_fn)
            if err:
                results.extend([None] * (len(chunk_data) - len(results)))
                return results, err
            results.extend(translated)
        return results, None

    def google_api(chunk_data):
        try:
            res = http_session().post(
                f"https://translation.googleapis.com/language/translate/v2?key={google_key}",
                json={"q": chunk_data, "target": "zh-TW", "format": "text"},
                timeout=(5, 15))
            if res.status_code == 200:
                return [html.unescape(i['translatedText'])
                        for i in res.json()['data']['translations']], None
            if res.status_code == 429:
                return None, "429:30"
            if res.status_code == 403:
                body = res.text.lower()
                if any(kw in body for kw in ('quota', 'billing', 'disabled', 'exceeded')):
                    return None, "DISABLED:Google API 額度已用盡或未啟用計費"
                return None, "429:30"
            return None, f"ERR:Google API HTTP {res.status_code}"
        except requests.RequestException as e:
            return None, f"ERR:Google 連線失敗: {e}"

    def deepl(chunk_data):
        base_url = ("https://api-free.deepl.com/v2/translate"
                    if deepl_key.endswith(":fx")
                    else "https://api.deepl.com/v2/translate")
        try:
            res = http_session().post(
                base_url,
                headers={"Authorization": f"DeepL-Auth-Key {deepl_key}",
                         "Content-Type": "application/json"},
                json={"text": chunk_data, "source_lang": "EN", "target_lang": "ZH-HANT"},
                timeout=(5, 20))
            if res.status_code == 200:
                return [item['text'] for item in res.json()['translations']], None
            if res.status_code == 429:
                return None, "429:60"
            if res.status_code == 456:
                return None, "DISABLED:DeepL 免費額度已用盡"
            if res.status_code in (401, 403):
                return None, "DISABLED:DeepL API Key 無效或無授權"
            return None, f"ERR:DeepL HTTP {res.status_code}"
        except requests.RequestException as e:
            return None, f"ERR:DeepL 連線失敗: {e}"

    def azure(chunk_data):
        results = []
        batch = []
        batch_chars = 0
        batches = []
        for text in chunk_data:
            text_chars = len(text)
            if text_chars > AZURE_BATCH_CHARS:
                return None, (
                    f"ERR:Azure 單筆文字超過 {AZURE_BATCH_CHARS:,} 字元")
            if batch and (len(batch) >= AZURE_BATCH_SIZE
                          or batch_chars + text_chars > AZURE_BATCH_CHARS):
                batches.append(batch)
                batch = []
                batch_chars = 0
            batch.append(text)
            batch_chars += text_chars
        if batch:
            batches.append(batch)

        for batch in batches:
            try:
                res = http_session().post(
                    azure_url,
                    headers={"Ocp-Apim-Subscription-Key": azure_key,
                             "Ocp-Apim-Subscription-Region": azure_region,
                             "Content-Type": "application/json"},
                    params={"api-version": "3.0", "to": "zh-Hant"},  # omit from= for auto-detect (zh_cn sources)
                    json=[{"text": t} for t in batch],
                    timeout=(3, 12))
                if res.status_code == 200:
                    try:
                        translated = [
                            item['translations'][0]['text']
                            for item in res.json()
                        ]
                    except (ValueError, TypeError, KeyError, IndexError) as e:
                        return None, (
                            "ERR:Azure incomplete translation response: "
                            f"{e}")
                    if len(translated) != len(batch):
                        return None, (
                            "ERR:Azure incomplete translation response: "
                            f"expected {len(batch)}, got {len(translated)}")
                    results.extend(translated)
                    continue
                if res.status_code == 429:
                    cd = parse_retry_after_seconds(
                        res.headers.get("Retry-After"), default=10.0)
                    return None, f"429:{cd}"
                if res.status_code == 401:
                    return None, "DISABLED:Azure 驗證失敗，請確認 Key 與 Region"
                if res.status_code == 403:
                    return None, "DISABLED:Azure 無授權（Key 無效或地區不符）"
                return None, f"ERR:Azure HTTP {res.status_code}"
            except requests.RequestException as e:
                return None, f"ERR:Azure 連線失敗: {e}"
        return results, None

    def claude(chunk_data):
        return ai_chunk(
            session,
            chunk_data,
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": claude_key, "anthropic-version": "2023-06-01",
             "Content-Type": "application/json"},
            lambda d: {"model": claude_model, "max_tokens": 4096,
                       "messages": [{"role": "user",
                                     "content": MC_PROMPT.format(
                                         json.dumps(d, ensure_ascii=False))}]},
            lambda r: extract_ai_text(r['content']),
            "Claude",
            max_batch=model_batch_limit(claude_model, 60)
        )

    def openai(chunk_data):
        return ai_chunk(
            session,
            chunk_data,
            "https://api.openai.com/v1/chat/completions",
            {"Authorization": f"Bearer {openai_key}",
             "Content-Type": "application/json"},
            lambda d: make_chat_body(openai_model, d, 4096, "max_completion_tokens"),
            lambda r: extract_openai_message_text(r['choices'][0]['message']),
            "OpenAI",
            max_batch=model_batch_limit(openai_model, 40)
        )

    def local(chunk_data):
        return ai_chunk(
            session,
            chunk_data,
            local_url,
            {"Content-Type": "application/json"},
            lambda d: {"messages": [{"role": "user",
                                     "content": MC_PROMPT.format(
                                         json.dumps(d, ensure_ascii=False))}],
                       "temperature": 0.1, "max_tokens": 2048},
            lambda r: extract_openai_message_text(r['choices'][0]['message']),
            "LocalAI",
            max_batch=30
        )

    def libretranslate(chunk_data):
        base = (ai_base_url or "http://127.0.0.1:5000").rstrip("/")
        url = base if base.endswith("/translate") else base + "/translate"
        results = []
        for text in chunk_data:
            if should_stop():
                results.extend([None] * (len(chunk_data) - len(results)))
                break
            payload = {
                "q": text,
                "source": "auto",
                "target": "zh",
                "format": "text",
            }
            if ai_api_key:
                payload["api_key"] = ai_api_key
            try:
                res = http_session().post(url, json=payload, timeout=(5, 30))
                if res.status_code == 200:
                    data = res.json()
                    translated = data.get("translatedText")
                    if isinstance(translated, list):
                        translated = translated[0] if translated else None
                    results.append(str(translated).strip() if translated else None)
                    continue
                if res.status_code == 429:
                    results.extend([None] * (len(chunk_data) - len(results)))
                    return results, "429:30"
                if res.status_code in (401, 403):
                    return None, "DISABLED:LibreTranslate API Key 無效或無授權"
                return None, f"DISABLED:LibreTranslate HTTP {res.status_code}"
            except requests.RequestException as e:
                return None, f"DISABLED:LibreTranslate 連線失敗: {e}"
            except (ValueError, TypeError, KeyError) as e:
                return None, f"ERR:LibreTranslate 回應解析失敗: {e}"
        return results, None

    def mymemory(chunk_data):
        results = []
        for text in chunk_data:
            if should_stop():
                results.extend([None] * (len(chunk_data) - len(results)))
                break
            try:
                # Prefer zh-CN→zh-TW when source already has CJK; else en→zh-TW.
                langpair = (
                    "zh-CN|zh-TW"
                    if any("一" <= ch <= "鿿" for ch in text)
                    else "en|zh-TW"
                )
                res = http_session().get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": text, "langpair": langpair},
                    timeout=(5, 20))
                if res.status_code == 429:
                    results.extend([None] * (len(chunk_data) - len(results)))
                    return results, "429:60"
                if res.status_code != 200:
                    return None, f"DISABLED:MyMemory HTTP {res.status_code}"
                data = res.json()
                if data.get("quotaFinished"):
                    return None, "DISABLED:MyMemory 免費額度已用盡"
                translated = (
                    (data.get("responseData") or {}).get("translatedText")
                    or data.get("translatedText")
                )
                results.append(html.unescape(str(translated).strip()) if translated else None)
            except requests.RequestException as e:
                return None, f"DISABLED:MyMemory 連線失敗: {e}"
            except (ValueError, TypeError, KeyError) as e:
                return None, f"ERR:MyMemory 回應解析失敗: {e}"
        return results, None

    # ── Bing/Microsoft 免費翻譯（免 API Key、免本地模型） ──
    # 走 Edge 瀏覽器的認證端點拿免費 JWT（約 10 分鐘過期，自動刷新），
    # 再呼叫微軟官方 Translator v3 API：陣列批次、回應逐條對齊，
    # 原生支援 zh-Hant。與 plainheart/bing-translate-api 等開源專案同做法。
    bing_lock = threading.Lock()
    bing_token_refresh_lock = threading.Lock()
    bing_state = {
        "token": "",
        "expiry": 0.0,
        "auth_fails": 0,
        "disabled": False,
    }
    bing_gate_state = [0.0]
    BING_GATE_INTERVAL = 0.005

    def _bing_gate():
        with bing_lock:
            now = time.time()
            slot = max(now, bing_gate_state[0] + BING_GATE_INTERVAL)
            bing_gate_state[0] = slot
        gap = slot - time.time()
        if gap > 0:
            time.sleep(gap)

    def _bing_token():
        with bing_lock:
            if bing_state["token"] and time.time() < bing_state["expiry"]:
                return bing_state["token"]
        # Only one worker refreshes the shared token. Re-check after acquiring
        # because another worker may already have completed the refresh.
        with bing_token_refresh_lock:
            with bing_lock:
                if (bing_state["token"]
                        and time.time() < bing_state["expiry"]):
                    return bing_state["token"]
            res = http_session().get(
                "https://edge.microsoft.com/translate/auth", timeout=(2, 6))
            if res.status_code != 200 or not res.text.strip():
                raise RuntimeError(f"auth HTTP {res.status_code}")
            token = res.text.strip()
            with bing_lock:
                bing_state["token"] = token
                # JWT 約 10 分鐘，提早刷新。
                bing_state["expiry"] = time.time() + 8 * 60
            return token

    def bing(chunk_data):
        results = []
        batch_size = BING_BATCH_SIZE
        for i in range(0, len(chunk_data), batch_size):
            if should_stop():
                results.extend([None] * (len(chunk_data) - len(results)))
                break
            batch = chunk_data[i:i + batch_size]
            auth_retries = 0
            while True:
                with bing_lock:
                    if bing_state["disabled"]:
                        return None, (
                            "DISABLED:Bing 端點連續拒絕授權（401/403），本場停用")
                _bing_gate()
                try:
                    token = _bing_token()
                except (requests.RequestException, RuntimeError) as e:
                    action = "刷新" if auth_retries else "取得"
                    return None, f"ERR:Bing token {action}失敗: {e}"
                try:
                    res = http_session().post(
                        "https://api-edge.cognitive.microsofttranslator.com/translate",
                        params={"api-version": "3.0", "to": "zh-Hant"},
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        json=[{"Text": t} for t in batch],
                        timeout=(2, 6))
                except requests.RequestException as e:
                    return None, f"ERR:Bing 連線失敗: {e}"
                if res.status_code not in (401, 403):
                    break

                with bing_lock:
                    # A delayed rejection for an older token must not clear the
                    # fresh token another worker already installed.
                    rejected_current = bing_state["token"] == token
                    if rejected_current:
                        bing_state["token"] = ""
                        bing_state["expiry"] = 0.0
                        bing_state["auth_fails"] += 1
                    fails = bing_state["auth_fails"]
                    if rejected_current and fails >= 2:
                        bing_state["disabled"] = True
                    disabled = bing_state["disabled"]
                if disabled:
                    return None, "DISABLED:Bing 端點連續拒絕授權（401/403），本場停用"
                if rejected_current:
                    auth_retries += 1
                if auth_retries >= 2:
                    with bing_lock:
                        bing_state["disabled"] = True
                    return None, (
                        "DISABLED:Bing 端點連續拒絕授權（401/403），本場停用")
            if res.status_code == 429:
                return None, "429:60"
            if res.status_code != 200:
                return None, f"ERR:Bing HTTP {res.status_code}"
            try:
                translated = [item["translations"][0]["text"]
                              for item in res.json()]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                return None, f"ERR:Bing 回應解析失敗: {e}"
            if len(translated) != len(batch):
                return None, "ERR:Bing 回應數量不符"
            with bing_lock:
                bing_state["auth_fails"] = 0   # 成功 → 清除連續授權失敗計數
            results.extend(
                apply_gtx_glossary(src, (dst or "").strip() or None)
                for src, dst in zip(batch, translated))
        return results, None

    def market_ai(chunk_data):
        api_type = ai_provider_cfg.get("api_type", "openai_compatible")
        request_timeout = ai_provider_cfg.get("request_timeout", (10, 60))

        if api_type == "libretranslate":
            return libretranslate(chunk_data)

        if api_type == "bing":
            return bing(chunk_data)

        if api_type == "anthropic":
            for _ in range(max(1, len(ai_api_keys))):
                key, key_idx = next_ai_key()
                result, err = ai_chunk(
                    session,
                    chunk_data,
                    ai_base_url,
                    {"x-api-key": key,
                     "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
                    lambda d: {"model": ai_model, "max_tokens": 4096,
                               "messages": [{"role": "user",
                                             "content": MC_PROMPT.format(
                                                 json.dumps(d, ensure_ascii=False))}]},
                    lambda r: extract_ai_text(r['content']),
                    ai_label,
                    request_timeout,
                    max_batch=model_batch_limit(ai_model, 60)
                )
                if err and err.startswith("DISABLED:") and key_idx >= 0 and len(ai_api_keys) > 1:
                    disable_ai_key(key_idx)
                    continue
                return result, err
            return None, f"DISABLED:{ai_label} 所有 API Key 均不可用"

        if api_type == "gemini":
            base = ai_base_url.rstrip("/")
            post_url = base if ":generateContent" in base else f"{base}/models/{ai_model}:generateContent"
            for _ in range(max(1, len(ai_api_keys))):
                key, key_idx = next_ai_key()
                result, err = ai_chunk(
                    session,
                    chunk_data,
                    post_url,
                    {"x-goog-api-key": key,
                     "Content-Type": "application/json"},
                    lambda d: {"contents": [{"role": "user",
                                             "parts": [{"text": MC_PROMPT.format(
                                                 json.dumps(d, ensure_ascii=False))}]}],
                               "generationConfig": {"temperature": 0.1,
                                                    "maxOutputTokens": 4096}},
                    lambda r: extract_ai_text(
                        r['candidates'][0]['content'].get('parts', [])),
                    ai_label,
                    request_timeout,
                    max_batch=model_batch_limit(ai_model, 40)
                )
                if err and err.startswith("DISABLED:") and key_idx >= 0 and len(ai_api_keys) > 1:
                    disable_ai_key(key_idx)
                    continue
                return result, err
            return None, f"DISABLED:{ai_label} 所有 API Key 均不可用"

        post_url = chat_completions_url(ai_base_url)
        token_param = ai_provider_cfg.get("token_param", "max_tokens")
        extra_body = dict(ai_provider_cfg.get("extra_body", {}) or {})
        max_out = 16384 if ai_provider_key == "deepseek" else 4096
        if ai_provider_key == "deepseek":
            extra_body.setdefault("response_format", {"type": "json_object"})
        for _ in range(max(1, len(ai_api_keys))):
            key, key_idx = next_ai_key()
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            headers = _openrouter_headers(headers, ai_provider_key, ai_base_url)
            result, err = ai_chunk(
                session,
                chunk_data,
                post_url,
                headers,
                lambda d: make_chat_body(ai_model, d, max_out, token_param, extra_body),
                lambda r: extract_openai_message_text(r['choices'][0]['message']),
                ai_label,
                request_timeout,
                max_batch=model_batch_limit(ai_model, 40)
            )
            if err and err.startswith("DISABLED:") and key_idx >= 0 and len(ai_api_keys) > 1:
                disable_ai_key(key_idx)
                continue
            return result, err
        return None, f"DISABLED:{ai_label} 所有 API Key 均不可用"

    return {
        'gtx': gtx,
        'bing': bing,
        'google_api': google_api if google_key else None,
        'deepl': deepl if deepl_key else None,
        'azure': azure if azure_key else None,
        'claude': claude if claude_key else None,
        'openai': openai if openai_key else None,
        'market_ai': market_ai,
        'local': local,
        'libretranslate': libretranslate,
        'mymemory': mymemory,
    }
