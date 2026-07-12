import io
import json
import os
import time
import tkinter as tk
import zipfile

from core.analysis_scan import java_runtime_warning_lines
from core.jar_patcher import atomic_zip_output_group
from translation_cache import cache_snapshot
from translation_packager import (
    defaultconfigs_mirror_path,
    ftbq_lang_snbt_role,
    json_bytes,
    lang_bytes,
    load_lang_content,
    load_json_content,
    loose_zh_tw_path,
    merge_jar_lang_data,
    merge_structured_json_with_existing_zh,
    merge_with_existing_zh,
    pack_mcmeta,
    parse_legacy_lang_content,
    safe_utf8_bytes,
    translated_fallback_paths,
    translated_lang_path,
    translated_repair_fallback_paths,
)


POST_TRANSLATION_RESCUE_LIMIT = 500


def confirm_java_runtime_before_translation(app):
    """Require explicit confirmation after an incompatible launcher runtime."""
    report = getattr(app, '_java_runtime_compatibility_report', None)
    if not isinstance(report, dict) or not report.get('is_incompatible'):
        return True

    warning_lines = [
        line.strip() for line in java_runtime_warning_lines(report)
        if line.strip()
    ]
    warning_lines.extend((
        "",
        "請先到 PCL 的此版本設定，將 Java 改為上列 Java 17。",
        "完成設定後才按「是」；按「否」會取消本次翻譯輸出。",
    ))
    ask = getattr(app, '_ask_proceed_from_thread', None)
    if not callable(ask):
        app.log("⛔ Java 版本不相容且無法取得確認，已取消翻譯。")
        return False
    confirmed = bool(ask(
        "Java 版本錯誤：先切換至 Java 17",
        '\n'.join(warning_lines)))
    if confirmed:
        app.log("✅ 已確認啟動器改用 Java 17，繼續翻譯。")
    else:
        app.log("⛔ 尚未確認切換 Java 17，已取消本次翻譯。")
    return confirmed


def save_post_batch_checkpoint(app):
    """Persist translated rows quickly; full review runs once at stage 2.5."""
    app.save_cache(light=True)
    return not (app.stop_requested or app.pause_requested)


def usable_cache_translation_keys(app, strings):
    """Return keys with valid cached translations using one bulk read."""
    snapshot = cache_snapshot(app.translation_cache, strings)
    validator = getattr(app, '_is_valid_trad_translation', None)
    if not callable(validator):
        fallback = getattr(app, '_cache_has_usable_translation')
        return {source for source in snapshot if fallback(source)}
    return {
        source for source, translated in snapshot.items()
        if validator(source, translated)
    }


def remove_legacy_client_class_patch(app, output_dir, output_name):
    """Remove an executable class patch left by unsafe older releases."""
    base_name = output_name.replace('.zip', '')
    legacy_path = os.path.join(
        output_dir, base_name + '_Class硬編碼補丁.zip')
    try:
        os.remove(legacy_path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(
            f"無法移除舊版危險 Class 補丁：{legacy_path}: {exc}") from exc
    app.log(f"🛡️ 已移除舊版危險 Class 補丁：{legacy_path}")
    return True


def run_translate_task(self, rp_dir, rp_name, pack_format, output_mode="jar_patch"):
    output_mode = output_mode if output_mode in ("resource_pack", "hybrid", "jar_patch") else "hybrid"
    self._active_output_mode = output_mode
    pack_format = int(getattr(
        self, '_detected_resource_pack_format', pack_format))
    mc_dir = self.analyzed_mc_dir          # 使用分析時的根目錄，確保 rel_path 正確
    task_started_at = time.time()
    task_completed = False
    task_cancelled = False
    task_error = False
    output_path = ""

    def checkpoint_cancelled():
        nonlocal task_cancelled
        if not (self.stop_requested or self.pause_requested):
            return False
        task_cancelled = True
        self.save_cache(light=True)
        if self.pause_requested:
            self.log("⏸️ 已暫停並儲存進度。下次直接點「開始極速翻譯」即可接續！")
        else:
            self.log("🛑 處理已中斷。")
        return True

    try:
        if not confirm_java_runtime_before_translation(self):
            task_cancelled = True
            self._set_summary_card(
                "pending", "已取消",
                "Java 版本不相容：請先在 PCL 固定 Java 17",
                self.C_WARN)
            return

        previous_cache = getattr(self, "translation_cache", None)
        if hasattr(previous_cache, "close"):
            previous_cache.close()
        self.translation_cache = self.load_cache()
        self._last_light_len = -1
        self.translation_dictionary = {}
        self.last_save_time    = time.time()

        phase1_start = time.time()
        self.log("\n--- 階段一：全域詞彙提取與智能過濾 ---")
        self.log(f"📁 分析基準目錄：{mc_dir}")
        cached_strings = getattr(self, "_analysis_unique_strings", None)
        if cached_strings:
            unique_strings = set(cached_strings)
            self.log(f"♻️ 使用分析階段已提取詞彙：{len(unique_strings):,} 筆")
        else:
            self.set_current_item("階段一：提取全域詞彙...", force=True)
            unique_strings = self.extract_all_unique_strings()
            self._analysis_unique_strings = unique_strings
            self.log(f"🔎 已重新提取詞彙：{len(unique_strings):,} 筆")
        force_mode = self.process_mode_var.get() == "force"
        self._session_translated_keys = set()
        if force_mode:
            # 不逐筆刪除 shelve 快取。4 萬筆以上的隨機刪除會讓 Windows dbm
            # 長時間卡住，甚至留下不完整索引檔。強制重翻改為本輪忽略舊快取，
            # 翻譯成功後自然覆寫新結果。
            self._force_ignore_cache_strings = set(unique_strings)
            self.set_current_item("階段一：強制重翻將忽略舊快取...", force=True)
            self.log(f"♻️ 強制重翻：本輪忽略 {len(unique_strings):,} 筆舊快取，不進行逐筆刪除。")
        else:
            self._force_ignore_cache_strings = set()
        self.set_current_item("階段一：比對記憶池與快取...", force=True)
        # 不要在翻譯開始前把全域記憶池命中逐筆寫入 shelve 快取。
        # 38k 詞彙、1w+ 記憶池命中會變成大量磁碟隨機寫入，導致第一階段長時間卡住。
        # 輸出時 get_translation() 已會讀取記憶池，所以這裡只做集合比對。
        # force 模式仍使用記憶池和快取作為 fallback：翻譯引擎未翻到的字串，
        # 輸出時會從快取/記憶池取回，避免產生未翻譯的輸出。
        cache_keys = set() if force_mode else set(self.translation_cache.keys())
        memory = self._load_translation_memory() if self.global_memory_var.get() else {}
        memory_keys = set(memory.keys()) if memory else set()
        if force_mode:
            cache_hit_strings = set()
            memory_hit_strings = set()
            missing_strings = sorted(unique_strings)
        else:
            cache_hit_strings = unique_strings & cache_keys
            memory_hit_strings = (unique_strings - cache_hit_strings) & memory_keys
            missing_strings = sorted(
                unique_strings - cache_hit_strings - memory_hit_strings)
        cache_hits = len(cache_hit_strings) + len(memory_hit_strings)
        self._analysis_total_strings = len(unique_strings)
        self._analysis_cache_hits = cache_hits
        self._analysis_memory_hits = len(memory_hit_strings)
        self._analysis_missing_strings = len(missing_strings)
        if memory_hit_strings:
            self.log(f"🧠 全域翻譯記憶池命中 {len(memory_hit_strings):,} 筆，輸出時直接套用。")
        hit_rate = (cache_hits / len(unique_strings) * 100) if unique_strings else 0.0
        self._set_summary_card(
            "cache", f"{cache_hits:,}",
            f"總計：{len(unique_strings):,}\n命中率：{hit_rate:.1f}%",
            self.C_SUCCESS)
        self._set_summary_card(
            "pending", f"{len(missing_strings):,}",
            f"總計：{len(unique_strings):,}\n狀態：準備翻譯",
            self.C_WARN if missing_strings else self.C_SUCCESS)
        self.log(f"⏱️ 階段一完成：{time.time() - phase1_start:.1f} 秒")

        self.log("\n--- 階段二：雲端 / 本地 AI 連線翻譯 ---")
        if missing_strings:
            self.set_current_item(
                f"階段二：準備翻譯 {len(missing_strings):,} 筆...",
                force=True)
            self.log(f"🔍 共發現 {len(unique_strings)} 筆有效詞彙。")
            self.log(f"✅ 快取命中 {len(unique_strings) - len(missing_strings)} 筆，"
                     f"剩餘 {len(missing_strings)} 筆需翻譯...")
            self.batch_translate_missing(missing_strings)
            save_post_batch_checkpoint(self)
        else:
            self.log(f"💡 全部 {len(unique_strings)} 筆詞彙已在快取中，直接進入生成階段。")

        # ── 驗證重試：validate 失敗或 API 批次失敗的字串不入快取，依設定重新嘗試 ──
        retry_count = max(0, min(10, int(self.retry_count_var.get())))
        prev_missing = None
        retry_chunk_size = None   # 先吃滿動態批次；無進展才縮小隔離「毒字串」
        retry_preferred_engine = None
        engine_route = self._normalize_engine_route_value(self.engine_var.get())
        for attempt in range(1, retry_count + 1):
            if self.stop_requested:
                break
            usable_keys = (
                set(self._session_translated_keys)
                if force_mode
                else usable_cache_translation_keys(self, unique_strings)
            )
            still_missing = [s for s in unique_strings
                             if s not in usable_keys
                             and (force_mode or s not in memory_keys)
                             and not self._RE_CJK_CHAR.search(s)   # 混中英=已翻過，重試浪費配額
                             and self.should_translate(s)]
            if not still_missing:
                break
            missing_set = set(still_missing)
            if prev_missing is not None and missing_set == prev_missing:
                if engine_route == "non_ai_chain":
                    if retry_preferred_engine is None:
                        retry_preferred_engine = "azure"
                        retry_chunk_size = 160
                        self.log(
                            "ℹ️  重試無進展 → 改用 Azure 批量補救，"
                            "避免 Bing 原樣返回專有名詞拖慢主波")
                    elif retry_preferred_engine == "azure":
                        retry_preferred_engine = "gtx"
                        retry_chunk_size = 80
                        self.log("ℹ️  Azure 補救無進展 → 改用 GTX 批量補救")
                    elif retry_chunk_size is None or retry_chunk_size > 20:
                        retry_chunk_size = 20
                        self.log("ℹ️  GTX 補救仍無進展 → 降為 20 條/批做最後隔離")
                    else:
                        self.log(
                            f"ℹ️  非 AI 鏈路批量補救仍無進展（{len(still_missing)} 筆），"
                            "停止重試以避免長時間卡住。")
                        break
                elif retry_chunk_size is None:
                    retry_chunk_size = 20
                    self.log(f"ℹ️  重試無進展 → 降為 20 條/批，縮小批次避免連坐")
                elif retry_chunk_size > 1:
                    # 批次級失敗會連坐同批好字串：降到單條/批隔離毒字串再試一輪
                    retry_chunk_size = 1
                    self.log(f"ℹ️  重試仍無進展 → 降為單條/批，隔離造成整批失敗的字串")
                else:
                    # 單條也救不回 → 是驗證永遠過不了的字串（純專有名詞等），收手
                    self.log(f"ℹ️  單條重試仍無進展（{len(still_missing)} 筆），"
                             f"停止重試以節省時間與配額。")
                    break
            prev_missing = missing_set
            batch_desc = "動態批次" if retry_chunk_size is None else f"{retry_chunk_size} 條/批"
            self.log(f"\n--- 階段二（驗證重試 {attempt}/{retry_count}）："
                     f"{len(still_missing)} 筆未入快取字串，"
                     f"以 {batch_desc} 重試 ---")
            self.batch_translate_missing(
                still_missing,
                _force_chunk_size=retry_chunk_size,
                _preferred_engine=retry_preferred_engine)
            self.save_cache(light=True)

        # ── 階段二.七：分段翻譯救援 ──
        # 含格式碼的標題/長句（任務書/裝備 tooltip）整句送翻時，引擎常弄壞 [#N#] 遮罩
        # 而被驗證退回。改成按格式碼切段、只翻純文字段、碼原樣保留：
        # 格式碼與 %s 完全不經過引擎，組裝結果結構上不可能壞 → 零崩潰風險。
        rescue_budget = POST_TRANSLATION_RESCUE_LIMIT if retry_count > 0 else 0

        def run_bounded_rescue(callback, candidates):
            nonlocal rescue_budget
            before = rescue_budget
            self._post_translation_rescue_budget = before
            self._post_translation_rescue_budget_managed = False
            try:
                return callback(candidates)
            finally:
                if self._post_translation_rescue_budget_managed:
                    rescue_budget = max(
                        0, min(before, int(
                            self._post_translation_rescue_budget)))
                else:
                    # Test doubles and older integrations may not implement the
                    # engine-item budget. Count their source rows conservatively.
                    rescue_budget = max(0, before - len(candidates))
                del self._post_translation_rescue_budget
                del self._post_translation_rescue_budget_managed

        if retry_count == 0:
            self.log("ℹ️  驗證重試設為 0；略過階段二.七、二.八補翻，直接生成輸出。")
        if not self.stop_requested and rescue_budget > 0:
            usable_keys = usable_cache_translation_keys(self, unique_strings)
            heavy = sorted(
                s for s in unique_strings
                if s not in usable_keys
                and (force_mode or s not in memory_keys)
                and not self._RE_CJK_CHAR.search(s)
                and self.should_translate(s)
                and self._RE_FORMAT.search(s)
            )
            if heavy:
                selected = heavy[:rescue_budget]
                omitted = len(heavy) - len(selected)
                self.log(f"\n--- 階段二.七：格式字串分段補翻（{len(selected)} 筆） ---")
                if omitted:
                    self.log(
                        f"ℹ️  補翻共用上限 {POST_TRANSLATION_RESCUE_LIMIT} 筆；"
                        f"另 {omitted} 筆保留原文，避免完成後長時間卡住。")
                rescued = run_bounded_rescue(
                    self._retry_segment_mode, selected)
                if rescued:
                    self.save_cache(light=True)

        # ── 階段二.八：混英譯文補翻（AI 引擎才有能力翻自創詞） ──
        if not self.stop_requested and rescue_budget > 0:
            mixed = sorted(self._find_mixed_translations(unique_strings))
            if mixed:
                selected = list(mixed[:rescue_budget])
                omitted = len(mixed) - len(selected)
                if omitted:
                    self.log(
                        f"ℹ️  補翻共用上限 {POST_TRANSLATION_RESCUE_LIMIT} 筆；"
                        f"另 {omitted} 筆保留原文，避免完成後長時間卡住。")
                engine_route = self._normalize_engine_route_value(self.engine_var.get())
                if engine_route in ('market_ai', 'claude', 'openai', 'local'):
                    self.log(f"\n--- 階段二.八：混英譯文補翻（{len(selected)} 筆，"
                             f"如「Brightsteel 板」→「亮鋼板」） ---")
                    if run_bounded_rescue(
                            self._retranslate_mixed, selected):
                        self.save_cache(light=True)
                else:
                    self.log(f"\n--- 階段二.八：混英片語升級（{len(selected)} 筆，非 AI 鏈） ---")
                    if run_bounded_rescue(
                            self._upgrade_mixed_phrases, selected):
                        self.save_cache(light=True)
                    self.log(f"💡 非 AI 升級為逐片語翻譯；想要整句更通順的版本，"
                             f"可切「市面 AI 模型」再跑一次。")

        if not self.stop_requested:
            usable_keys = usable_cache_translation_keys(self, unique_strings)
            still_missing_final = [
                s for s in unique_strings
                if s not in usable_keys and s not in memory_keys
            ]
            if still_missing_final:
                self.log(f"ℹ️  仍有 {len(still_missing_final)} 筆字串無法翻譯"
                         f"（可能為專有名詞、格式驗證失敗或 API 持續失敗，輸出時將保留原文）")
            usable_count = len(unique_strings) - len(still_missing_final)
            usable_ratio = (usable_count / len(unique_strings)) if unique_strings else 1.0
            too_few_translations = (
                bool(missing_strings)
                and len(unique_strings) >= 100
                and usable_count < max(10, int(len(unique_strings) * 0.01))
            )
            if usable_count == 0 or too_few_translations:
                task_cancelled = True
                self.log(
                    "❌ 本輪沒有產生足夠可用譯文，已取消輸出，避免產生空 zh_tw "
                    f"或看似完成但未翻譯的語言包（可用 {usable_count:,}/"
                    f"{len(unique_strings):,}，{usable_ratio:.1%}）。")
                self._set_summary_card(
                    "pending", "已取消",
                    f"總計：{len(unique_strings):,}\n狀態：可用譯文不足",
                    self.C_ERROR)
                self.save_cache(light=True)
                return

        if checkpoint_cancelled():
            return

        # ── 階段 2.5：快取繁體複查 + 翻譯品質驗證 ──
        phase25_start = time.time()
        self.set_current_item("階段 2.5：快取複查與品質驗證...", force=True)
        if checkpoint_cancelled():
            return
        self._review_and_fix_cache()
        if checkpoint_cancelled():
            return
        verified = self._verify_translations(unique_strings)
        if checkpoint_cancelled():
            return
        if not verified:
            task_cancelled = True
            self._set_summary_card(
                "pending", "已取消",
                f"總計：{len(unique_strings):,}\n狀態：驗證未通過",
                self.C_WARN)
            return   # 使用者取消輸出
        self.log(f"⏱️ 階段 2.5 完成：{time.time() - phase25_start:.1f} 秒")
        usable_keys = usable_cache_translation_keys(self, unique_strings)
        final_missing = len([
            s for s in unique_strings
            if s not in usable_keys and s not in memory_keys
        ])
        self._set_summary_card(
            "pending", f"{final_missing:,}",
            f"總計：{len(unique_strings):,}\n狀態：輸出中",
            self.C_WARN if final_missing else self.C_SUCCESS)
        if checkpoint_cancelled():
            return

        server_mode = getattr(self, "_server_mode", False)
        class_patch_enabled = bool(getattr(
            self, '_scan_class_tooltip_patch', False))
        if (not server_mode
                and (output_mode != 'hybrid' or not class_patch_enabled)):
            remove_legacy_client_class_patch(self, rp_dir, rp_name)

        if output_mode == "jar_patch" or server_mode:
            if server_mode:
                self.log("\n--- 階段三：開始生成伺服器 JAR 套用包 ---")
                self.set_current_item("階段三：生成伺服器翻譯包...", force=True)
            else:
                self.log("\n--- 階段三：開始重建客戶端翻譯 JAR ---")
                self.set_current_item("階段三：重建翻譯 JAR...", force=True)
            phase3_start = time.time()
            output_path = self._generate_jar_patches(rp_dir, rp_name, mc_dir) or ""
            self.log(f"⏱️ 階段三完成：{time.time() - phase3_start:.1f} 秒")
            if not self.stop_requested and output_path:
                task_completed = True
                self._last_output_path = output_path
            return

        self.log("\n--- 階段三：開始生成安全資源包 ---")
        self.set_current_item("階段三：生成資源包...", force=True)
        pack_path    = os.path.join(rp_dir, rp_name)

        total_tasks  = (
            sum(1 for lf in self.analyzed_jars.values()
                for p in lf if self._scope_allows_analyzed_path("jar", p))
            + sum(1 for p in self.analyzed_loose
                  if self._scope_allows_analyzed_path("loose", p))
            + sum(1 for bf in self.analyzed_book_texts.values()
                  for p in bf if self._scope_allows_analyzed_path("book_txt", p))
            + sum(1 for bf in self.analyzed_book_text_repairs.values()
                  for p in bf if self._scope_allows_analyzed_path("book_txt", p))
            + sum(1 for t, p in self.analyzed_extra
                  if self._scope_allows_extra(t, p))
            + len(self.analyzed_zip_json))
        current_task = 0
        has_cfg      = False   # 是否包含 config / defaultconfigs 檔案
        datapack = None
        datapack_name = self._safe_zip_filename(
            self.datapack_name_var.get(),
            rp_name[:-4] + "_Datapack")
        if datapack_name.lower() == rp_name.lower():
            datapack_name = rp_name[:-4] + "_Datapack.zip"
        datapack_path = os.path.join(rp_dir, datapack_name)

        def validate_generated_zip(path):
            try:
                with zipfile.ZipFile(path, 'r') as generated:
                    return generated.testzip() is None
            except (OSError, zipfile.BadZipFile):
                return False

        output_ready = False
        output_atomic_state = {}
        datapack_requested = bool(self.datapack_output_var.get())
        output_paths = [pack_path]
        if datapack_requested:
            output_paths.append(datapack_path)
        with atomic_zip_output_group(
                output_paths,
                lambda: output_ready and not self.stop_requested,
                validate=validate_generated_zip,
                state=output_atomic_state) as atomic_outputs:
            pack = atomic_outputs[pack_path]
            if datapack_requested:
                datapack = atomic_outputs[datapack_path]
                datapack.writestr(
                    'pack.mcmeta',
                    json_bytes(pack_mcmeta(
                        int(getattr(
                            self, '_detected_data_pack_format',
                            self.datapack_format_var.get())),
                        "§a自動翻譯資料包")))

            pack.writestr('pack.mcmeta', json_bytes(pack_mcmeta(pack_format)))

            def write_output(internal_path, data):
                nonlocal datapack
                if (datapack_requested
                        and internal_path.replace('\\', '/').startswith('data/')):
                    datapack.writestr(internal_path, data)
                    return "datapack"
                pack.writestr(internal_path, data)
                return "resourcepack"

            # ── JAR 內語言檔 & Patchouli 手冊（全在 assets/ → 只進資源包） ──
            for jar_path, lang_files in self.analyzed_jars.items():
                if self.stop_requested:
                    break
                jar_name = os.path.basename(jar_path)
                for path_in_jar, lang_data in lang_files.items():
                    if self.stop_requested:
                        break
                    if not self._scope_allows_analyzed_path("jar", path_in_jar):
                        continue
                    zh_base = (self.analyzed_jars_zh_base
                               .get(jar_path, {})
                               .get(path_in_jar, {}))
                    if self.process_mode_var.get() == "force":
                        # force 模式不應丟棄 zh_cn fallback，
                        # 只是不管 zh_tw 是否已存在都重新翻譯
                        pass
                    else:
                        official_base = self._load_official_minecraft_zh_base(mc_dir, path_in_jar)
                        if official_base:
                            zh_base = self._combine_lang_bases(lang_data, official_base, zh_base)
                    if self._is_advancement_json_path(path_in_jar):
                        merged_data = self._process_advancement_json(lang_data)
                    elif self._is_structured_book_json_path(path_in_jar):
                        def process_book_data(data, preserve=False, strict=False):
                            return self.process_json_data(data, preserve, True)

                        merged_data = merge_structured_json_with_existing_zh(
                            lang_data, zh_base, process_book_data, self._to_traditional,
                            value_needs_update=self._lang_value_needs_update)
                        if hasattr(self, "_repair_structured_book_json_output"):
                            merged_data = self._repair_structured_book_json_output(
                                lang_data, zh_base, merged_data, process_book_data,
                                f"{jar_name}:{path_in_jar}")
                    else:
                        def process_jar_data(data, preserve=False, strict=False, _path=path_in_jar):
                            return self.process_json_data(
                                data, preserve, strict or self._is_structured_book_json_path(_path))

                        merged_data = merge_jar_lang_data(
                            lang_data, zh_base, process_jar_data, self._to_traditional)

                    if '/lang/' in path_in_jar:
                        merged_data, dropped, format_dropped = self._filter_lang_output_entries(lang_data, merged_data)
                        if dropped:
                            extra = f"（含 {format_dropped:,} 個格式碼異常）" if format_dropped else ""
                            self.log(f"  ℹ️ {jar_name}: 略過 {dropped:,} 個尚未翻譯的語言項目{extra}")
                    translated_bytes = lang_bytes(path_in_jar, merged_data)

                    # 只寫 zh_tw 版本，不覆蓋原始 en_us.json
                    zh_path = translated_lang_path(path_in_jar)
                    if zh_path:
                        self.log(f"📄 {jar_name}: {zh_path}")
                        write_output(zh_path, translated_bytes)
                        for fallback_path in translated_fallback_paths(path_in_jar):
                            if fallback_path != zh_path:
                                write_output(fallback_path, translated_bytes)
                                self.log(f"📄 {jar_name}: {fallback_path}（fallback 覆蓋）")
                        if '/assets/' in zh_path and not zh_path.startswith('assets/'):
                            flat_zh_path = zh_path[zh_path.rfind('/assets/') + 1:]
                            if flat_zh_path != zh_path:
                                write_output(flat_zh_path, translated_bytes)
                                self.log(f"📄 {jar_name}: {flat_zh_path}")

                    current_task += 1
                    self.update_progress(current_task, total_tasks, text_mode=False)

            # ── JAR 內自訂書本 txt（例如 Alex's Mobs Animal Dictionary）─ 寫成 zh_tw/*.txt ──
            for jar_path, book_files in self.analyzed_book_texts.items():
                if self.stop_requested:
                    break
                jar_name = os.path.basename(jar_path)
                for path_in_jar, content in book_files.items():
                    if self.stop_requested:
                        break
                    if not self._scope_allows_analyzed_path("book_txt", path_in_jar):
                        continue
                    zh_path = translated_lang_path(path_in_jar)
                    if not zh_path:
                        continue
                    translated_text = self.process_book_text_content(content, path_in_jar)
                    if (hasattr(self, "_book_text_has_effective_translation")
                            and not self._book_text_has_effective_translation(content, translated_text)):
                        self.log(
                            f"⚠️ {jar_name}: 書本 TXT 尚無有效譯文，略過 {path_in_jar}，"
                            "避免輸出英文 zh_tw")
                        current_task += 1
                        self.update_progress(current_task, total_tasks, text_mode=False)
                        continue
                    self.log(f"📘 {jar_name}: {zh_path}")
                    translated_bytes = safe_utf8_bytes(translated_text)
                    write_output(zh_path, translated_bytes)
                    for fallback_path in translated_fallback_paths(path_in_jar):
                        if fallback_path != zh_path:
                            write_output(fallback_path, translated_bytes)
                            self.log(f"📘 {jar_name}: {fallback_path}（fallback 覆蓋）")
                    current_task += 1
                    self.update_progress(current_task, total_tasks, text_mode=False)

            # ── 既有 zh_tw 書本 txt：只修正繁中文字寬換行，不增加 API 成本 ──
            for jar_path, book_files in self.analyzed_book_text_repairs.items():
                if self.stop_requested:
                    break
                jar_name = os.path.basename(jar_path)
                for path_in_jar, content in book_files.items():
                    if self.stop_requested:
                        break
                    if not self._scope_allows_analyzed_path("book_txt", path_in_jar):
                        continue
                    fixed_text = self.process_book_text_content(content, path_in_jar)
                    self.log(f"📘 {jar_name}: 修正書本換行 {path_in_jar}")
                    fixed_bytes = safe_utf8_bytes(fixed_text)
                    write_output(path_in_jar, fixed_bytes)
                    for fallback_path in translated_repair_fallback_paths(path_in_jar):
                        if fallback_path != path_in_jar:
                            write_output(fallback_path, fixed_bytes)
                            self.log(f"📘 {jar_name}: {fallback_path}（fallback 修復覆蓋）")
                    current_task += 1
                    self.update_progress(current_task, total_tasks, text_mode=False)

            # ── 獨立 en_us.json（在 assets/ 路徑 → 資源包） ──
            for path in self.analyzed_loose:
                if self.stop_requested:
                    break
                if not self._scope_allows_analyzed_path("loose", path):
                    continue
                rel_path  = os.path.relpath(path, mc_dir).replace('\\', '/')
                zh_tw_rel = loose_zh_tw_path(rel_path)
                self.log(f"📄 獨立 lang: {zh_tw_rel}")
                try:
                    content = self.safe_read_file(path)
                    if not content.strip():
                        continue
                    data = load_lang_content(content, path, self._clean_json_text)
                    # 若有既有 zh_tw 基底，只翻譯缺少的 key，再合併輸出完整翻譯
                    zh_base = self.analyzed_loose_base.get(path)
                    if self.process_mode_var.get() == "force":
                        zh_base = None
                    merged = merge_with_existing_zh(
                        data, zh_base,
                        self.process_json_data, self._to_traditional,
                        value_needs_update=self._lang_value_needs_update)
                    merged, dropped, format_dropped = self._filter_lang_output_entries(data, merged)
                    if dropped:
                        extra = f"（含 {format_dropped:,} 個格式碼異常）" if format_dropped else ""
                        self.log(f"  ℹ️ {zh_tw_rel}: 略過 {dropped:,} 個尚未翻譯的語言項目{extra}")
                    translated_bytes = lang_bytes(path, merged)
                    # 只寫 zh_tw.json，不覆蓋原始 en_us.json
                    write_output(zh_tw_rel, translated_bytes)
                except (json.JSONDecodeError, OSError) as e:
                    self.log(f"⚠️ 略過 {zh_tw_rel}: {e}")
                current_task += 1
                self.update_progress(current_task, total_tasks, text_mode=False)

            # ── 附加檔案（snbt / json）─ 全部寫入同一個合併翻譯包 ──
            written_cfg_paths = set()   # 追蹤已直接寫入 config/ 的路徑，避免 defaultconfigs/ 鏡像時重複
            for file_type, path in self.analyzed_extra:
                if self.stop_requested:
                    break
                if not self._scope_allows_extra(file_type, path):
                    continue
                rel_path = os.path.relpath(path, mc_dir).replace('\\', '/')
                is_assets = rel_path.startswith('assets/')
                label = "資源包" if is_assets else "Config/任務"
                self.log(f"📄 [{label}/{file_type.upper()}]: {rel_path}")
                try:
                    content = self.safe_read_file(path)
                    if not content.strip():
                        continue
                    if file_type == 'snbt':
                        translated = self.process_text_file(content)
                        # 語言檔模式（quests/lang/en_us.snbt）：輸出到 zh_tw.snbt，
                        # 絕不就地覆寫 en_us（保住英文來源；遊戲語言設 zh_tw 時自動載入）
                        is_lang, _lc, zh_snbt = ftbq_lang_snbt_role(rel_path)
                        out_rel = zh_snbt if is_lang else rel_path
                        write_output(out_rel, safe_utf8_bytes(translated))
                        if not is_assets:
                            has_cfg = True
                            if out_rel.startswith('config/'):
                                written_cfg_paths.add(out_rel)
                        # defaultconfigs/ftbquests/ → 同時寫入 config/ftbquests/
                        # FTB Quests 執行時讀 config/，defaultconfigs/ 只用於新世界初始化
                        config_rel = defaultconfigs_mirror_path(out_rel)
                        if config_rel:
                            if config_rel not in written_cfg_paths:
                                write_output(config_rel, safe_utf8_bytes(translated))
                                has_cfg = True
                                written_cfg_paths.add(config_rel)  # 立即標記，防止後續 config/ 同名檔重複寫入
                    elif file_type == 'json':
                        data = load_json_content(content, self._clean_json_text)
                        translated_bytes = json_bytes(
                            self.process_json_data(
                                data, preserve_technical_keys=True,
                                strict_context=self.strict_whitelist_var.get()))
                        write_output(rel_path, translated_bytes)
                        if is_assets and '/en_us/' in rel_path:
                            zh_path = rel_path.replace('/en_us/', '/zh_tw/')
                            write_output(zh_path, translated_bytes)
                        if not is_assets:
                            has_cfg = True
                    elif file_type == 'mns_json':
                        data = load_json_content(content, self._clean_json_text)
                        translated_bytes = json_bytes(
                            self.process_mmorpg_json_display_fields(data))
                        write_output(rel_path, translated_bytes)
                        has_cfg = True
                    elif file_type == 'apoth_names':
                        translated = self.process_apoth_names_cfg(content)
                        write_output(rel_path, safe_utf8_bytes(translated))
                        has_cfg = True
                        self.log(f"  🏷️ Apotheosis 命名表已翻譯：{rel_path}")
                    elif file_type == 'md':
                        translated = self.process_md_file(content)
                        write_output(rel_path, safe_utf8_bytes(translated))
                        if not is_assets:
                            has_cfg = True
                    elif file_type == 'lang':
                        data = parse_legacy_lang_content(content)
                        translated_bytes = lang_bytes(
                            path, self.process_json_data(
                                data, preserve_technical_keys=True,
                                strict_context=self.strict_whitelist_var.get()))
                        write_output(rel_path, translated_bytes)
                        if not is_assets:
                            has_cfg = True
                except Exception as e:
                    self.log(f"⚠️ 略過 {rel_path}: {e}")
                current_task += 1
                self.update_progress(current_task, total_tasks, text_mode=False)

            zip_groups = {}
            for zip_path, internal_path in self.analyzed_zip_json:
                zip_groups.setdefault(zip_path, []).append(internal_path)

            for zip_path, internal_paths in zip_groups.items():
                if self.stop_requested:
                    break
                rel_path = os.path.relpath(zip_path, mc_dir).replace('\\', '/')
                internal_set = set(internal_paths)
                patched_count = 0
                try:
                    out_zip = io.BytesIO()
                    with zipfile.ZipFile(zip_path, 'r') as src_zip, \
                         zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as dst_zip:
                        for item in src_zip.infolist():
                            data = src_zip.read(item)
                            if item.filename in internal_set and item.filename.lower().endswith('.json'):
                                try:
                                    content = self.safe_decode_bytes(data)
                                    obj = load_json_content(content, self._clean_json_text)
                                    translated = self.process_origin_json_display_fields(obj)
                                    data = json_bytes(translated)
                                    patched_count += 1
                                except Exception as e:
                                    self.log(
                                        f"  ⚠️ ZIP 內 JSON 略過 {os.path.basename(zip_path)}::{item.filename}: {e}")
                            dst_zip.writestr(item, data)
                    if patched_count:
                        write_output(rel_path, out_zip.getvalue())
                        has_cfg = True
                        self.log(
                            f"  ✅ {os.path.basename(zip_path)}（翻譯 ZIP 內 Origins JSON {patched_count} 個）")
                except Exception as e:
                    self.log(f"⚠️ ZIP 處理失敗 {rel_path}: {e}")
                current_task += len(internal_paths)
                self.update_progress(current_task, total_tasks, text_mode=False)

            if self.scope_mod_lang_var.get():
                write_output(
                    'assets/mc_modpack_translator/lang/zh_tw.json',
                    json_bytes(self.SYNTHETIC_LANG_ZH_TW))
                write_output(
                    'assets/additionalentityattributes/lang/zh_tw.json',
                    json_bytes(self.ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW))
                self.log("📄 合成 lang: assets/mc_modpack_translator/lang/zh_tw.json")

            output_ready = not self.stop_requested

        if not self.stop_requested and not output_atomic_state.get('committed'):
            raise RuntimeError("資源包群組驗證或原子替換失敗，舊輸出已保留")

        if not self.stop_requested:
            if output_mode == 'hybrid' and class_patch_enabled:
                self.set_current_item(
                    "階段四：生成低風險 class/JAR 補丁...", force=True)
                class_patch_path = self._generate_class_patch_jars(
                    rp_dir, rp_name, mc_dir) or ""
                if class_patch_path:
                    self.log(f"📦 低風險 class/JAR 補丁：{class_patch_path}")
            self.update_progress(total_tasks, total_tasks, text_mode=False)
            self.log(f"\n🎉 合併翻譯包生成完畢！")
            self.log(f"📦 翻譯包路徑（含 mod 語言 + config + defaultconfigs）：")
            self.log(f"   {pack_path}")
            self._maybe_install_resource_pack_to_instance(pack_path, rp_dir, mc_dir)
            if datapack is not None:
                self.log(f"📦 Data Pack / OpenLoader 資料包：")
                self.log(f"   {datapack_path}")
            self.log(f"\n📋 使用方式：")
            self.log(f"   ① 將此 ZIP 放入 resourcepacks/ 資料夾並在遊戲內啟用 → mod 語言翻譯生效")
            if has_cfg:
                self.log(f"   ② 用 7-Zip / WinRAR 將 config/ 和 defaultconfigs/ 解壓至遊戲根目錄")
                self.log(f"      → FTB Quests 任務書翻譯生效，重啟遊戲後套用")
            mode_title, _ = self._output_mode_summary()
            self._last_output_path = pack_path
            output_path = pack_path
            task_completed = True
            self._set_summary_card(
                "output", mode_title,
                f"語言：zh_tw\n已產生：{os.path.basename(pack_path)}",
                self.C_SUCCESS)
            self._refresh_api_summary()

    except Exception as e:
        task_error = True
        self.log(f"❌ 嚴重錯誤: {e}")
        self._note_progress_error()
        self._set_summary_card("api", "錯誤", "請查看執行記錄", self.C_DANGER)
    finally:
        elapsed = time.time() - task_started_at
        elapsed_text = self._format_eta(elapsed) if hasattr(self, "_format_eta") else f"{elapsed:.1f}s"
        total_strings = getattr(self, "_analysis_total_strings", 0)
        if task_completed:
            self.log(f"✅ 任務完成，用時 {elapsed_text}")
            if output_path:
                self.log(f"✅ 輸出完成：{output_path}")
        def finish():
            self.is_processing = False
            self._set_btn_state(self.btn_analyze,   tk.NORMAL)
            self._set_btn_state(self.btn_translate, tk.NORMAL)
            self._set_btn_state(self.btn_stop,      tk.DISABLED)
            self._set_btn_state(self.btn_pause,     tk.DISABLED)
            if task_error:
                self.set_current_item("發生錯誤，請查看執行記錄", force=True)
                self._refresh_api_summary(state="錯誤", color=self.C_DANGER, sub="請查看執行記錄")
            elif task_cancelled or self.stop_requested or self.pause_requested:
                state = "已暫停" if self.pause_requested else "已停止"
                self.set_current_item(state, force=True)
                self._set_summary_card(
                    "pending", state,
                    f"總計：{total_strings:,}\n狀態：可重新開始",
                    self.C_WARN)
                self._refresh_api_summary(state=state, color=self.C_WARN)
            elif task_completed:
                self.set_current_item("完成：已輸出翻譯包", force=True)
                self._set_summary_card(
                    "pending", "完成",
                    f"總計：{total_strings:,}\n狀態：輸出完成",
                    self.C_SUCCESS)
                self._refresh_api_summary(state="已完成", color=self.C_SUCCESS)
        self.root.after(0, finish)
