import concurrent.futures
import json
import os
import time
import tkinter as tk
import traceback
import zipfile

from core.json_loader import load_json_lenient
from translation_packager import (
    ftbq_lang_snbt_role,
    is_localized_manual_resource,
    is_preferred_manual_source,
    is_safe_archive_path,
    locale_segment,
    load_lang_content,
    manual_source_group_key,
    manual_source_priority,
    merge_zh_base_fallback,
    replace_locale_segment,
    structured_json_needs_update,
    translated_lang_path,
)


GENERATED_TRANSLATOR_DIR_MARKERS = (
    '自動翻譯覆蓋',
    'mc_modpack_translator',
)

SINGLE_JAR_SCAN_BUDGET_SECONDS = 120.0

TOP_LEVEL_SCAN_IGNORE = {'libraries', 'bin', 'jre', 'versions'}
RUNTIME_CACHE_DIRS = {
    '.fabric',
    '.mixin.out',
    'downloads',
    'dynamic-resource-pack-cache',
    'moddata',
    'pcl',
    'xaero',
    'logs',
    'crash-reports',
    'saves',
    'shaderpacks',
    'shader_packs',
    'screenshots',
    'backups',
    'backup',
    'out',
    'output',
    'outputs',
    'failed items',
    'dynmap',
    'webcache',
    'texturepacks',
    'world',
    'worlds',
    'local',
    'natives',
}
ASSETS_CACHE_DIRS = {'indexes', 'objects', 'skins', 'log_configs'}
ZIP_RESOURCE_ROOTS = {
    'resourcepacks',
    'global_packs',
    'globalpacks',
}
ZIP_DATA_ROOTS = {
    'datapacks',
    'global_packs',
    'globalpacks',
    'moonlight-global-datapacks',
}


def _rel_parts(base_dir, path):
    try:
        rel = os.path.relpath(path, base_dir).replace('\\', '/')
    except ValueError:
        rel = os.path.basename(path)
    return [part for part in rel.split('/') if part and part != '.']


def _rel_lower(base_dir, path):
    return '/'.join(part.lower() for part in _rel_parts(base_dir, path))


def _has_runtime_cache_segment(parts):
    for part in parts:
        lower = part.lower()
        if lower in RUNTIME_CACHE_DIRS:
            return True
        if lower.endswith('-natives') or lower.endswith('_natives'):
            return True
    return False


def should_descend_scan_dir(mod_dir, root_dir, dirname):
    """Return whether os.walk should descend into a directory.

    The scanner must avoid launcher/runtime caches such as .fabric processed
    jars. Those files look like valid mod jars, but they are not authoritative
    sources and do not get loaded from the final translated package.
    """
    lower = dirname.lower()
    if lower in RUNTIME_CACHE_DIRS:
        return False
    if lower.endswith('-natives') or lower.endswith('_natives'):
        return False
    if is_translator_generated_dir_name(dirname):
        return False
    is_top = os.path.normcase(os.path.normpath(root_dir)) == os.path.normcase(os.path.normpath(mod_dir))
    if is_top and lower in TOP_LEVEL_SCAN_IGNORE:
        return False
    rel_root = _rel_lower(mod_dir, root_dir)
    if rel_root == 'assets' and lower in ASSETS_CACHE_DIRS:
        return False
    return True


def should_scan_translation_archive(mod_dir, path):
    """Return whether a jar/zip is a real translation source for this modpack."""
    parts = _rel_parts(mod_dir, path)
    if not parts:
        return False
    lower_parts = [part.lower() for part in parts]
    if _has_runtime_cache_segment(lower_parts):
        return False
    if any(is_translator_generated_dir_name(part) for part in parts):
        return False
    ext = os.path.splitext(parts[-1])[1].lower()
    top = lower_parts[0]
    rel = '/'.join(lower_parts)
    is_pack_archive = (
        top in ZIP_RESOURCE_ROOTS
        or top in ZIP_DATA_ROOTS
        or '/resourcepacks/' in rel
        or '/datapacks/' in rel
        or '/openloader/resources/' in rel
        or '/paxi/resourcepacks/' in rel
        or '/paxi/datapacks/' in rel
    )
    if ext == '.jar':
        return len(parts) == 1 or top == 'mods' or is_pack_archive
    if ext == '.zip':
        return is_pack_archive
    return False


def is_translator_generated_dir_name(name: str) -> bool:
    """Return True for directories produced by this translator itself.

    These directories must not be scanned as source input. If an old Paxi
    overlay is analyzed again, stale translated files are mixed into the next
    output package and can duplicate/override fresh Patchouli book data.
    """
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    if lowered in {'_backups', 'backups', 'backup'}:
        return True
    return any(marker.lower() in lowered for marker in GENERATED_TRANSLATOR_DIR_MARKERS)


def scan_single_jar(self, path):
    """掃描 JAR 內語言檔及 Patchouli 手冊頁面。
    優先使用 en_us.json；若不存在則 fallback 至其他語言檔（en_gb、zh_cn 等）。
    若 JAR 已有 zh_tw.json：只收集 zh_tw 缺少的 key，並記錄既有 zh_tw 基底供後續合併。"""
    lang_files    = {}
    zh_base_local = {}   # {src_path: zh_tw_dict}：既有翻譯基底，輸出時合併
    self.set_current_item(f"掃描中：{os.path.basename(path)}")
    process_mode = getattr(self, "_scan_process_mode", None)
    if process_mode is None:
        process_mode = self.process_mode_var.get() if hasattr(self, "process_mode_var") else "append"
    scope_mod_lang = getattr(self, "_scan_scope_mod_lang", None)
    if scope_mod_lang is None:
        scope_mod_lang = self.scope_mod_lang_var.get()
    scope_books = getattr(self, "_scan_scope_books", None)
    if scope_books is None:
        scope_books = self.scope_books_var.get()
    scope_quests = getattr(self, "_scan_scope_quests", None)
    if scope_quests is None:
        scope_quests = self.scope_quests_var.get()
    # 伺服器模式：dedicated server 只認 en_us，lang/手冊/書本翻了沒人看得到；
    # class 修補在伺服器端是「壞一個 = 全服崩潰」，一律跳過。
    # 保留 advancement（顯示文字由 server 下發給所有玩家）。
    server_mode = getattr(self, "_server_mode", False)
    if server_mode:
        scope_mod_lang = False
        scope_books = False
    try:
        with zipfile.ZipFile(path, 'r') as jar:
            scan_started = time.monotonic()
            scan_budget = float(getattr(
                self, "_single_jar_scan_budget_seconds",
                SINGLE_JAR_SCAN_BUDGET_SECONDS))
            scan_deadline = scan_started + scan_budget
            jar_name = os.path.basename(path)

            def scan_expired(stage):
                if time.monotonic() <= scan_deadline:
                    return False
                self.log(
                    f"⚠️ {jar_name}: {stage} 掃描超過 {scan_budget:.0f}s，"
                    "已略過剩餘內容以避免分析卡死")
                return True

            infos = [
                info for info in jar.infolist()
                if is_safe_archive_path(info.filename)
            ]
            # 建立 小寫名稱 → 原始名稱 對照表（比 set 多一步但只掃一次）
            all_lower_to_orig = {i.filename.lower(): i.filename for i in infos}
            all_names_lower   = frozenset(all_lower_to_orig)

            selected_manual_sources_lower = set()
            if scope_books:
                best_manual_sources = {}
                for idx, info in enumerate(infos):
                    if idx % 1000 == 0 and scan_expired("手冊來源選擇"):
                        break
                    fn = info.filename
                    fn_lower = fn.lower()
                    if not fn_lower.endswith(('.json', '.txt')):
                        continue
                    if locale_segment(fn) == 'zh_tw':
                        continue
                    if not translated_lang_path(fn):
                        continue
                    if not is_preferred_manual_source(fn):
                        continue
                    is_manual_candidate = (
                        '/patchouli_books/' in fn_lower
                        or (fn_lower.startswith('assets/') and '/book/' in fn_lower)
                        or is_localized_manual_resource(fn)
                    )
                    if not is_manual_candidate:
                        continue
                    group_key = manual_source_group_key(fn)
                    if not group_key:
                        continue
                    priority = manual_source_priority(fn)
                    existing = best_manual_sources.get(group_key)
                    if existing is None or priority < existing[0]:
                        best_manual_sources[group_key] = (priority, fn_lower)
                selected_manual_sources_lower = {
                    fn_lower for _priority, fn_lower in best_manual_sources.values()
                }

            # ── 收集所有 lang/ 目錄，每個目錄只選一個來源語言檔 ──
            # 支援一般 assets/*/lang，也支援 JAR 內建 resource pack:
            # packs/i18n/assets/*/lang。
            lang_dirs_seen = set()
            if scope_mod_lang:
                for fn_lower in all_names_lower:
                    if self._is_jar_lang_path(fn_lower):
                        lang_dir = fn_lower[:fn_lower.rfind('/lang/') + 6]  # 含尾斜線
                        lang_dirs_seen.add(lang_dir)

            for lang_dir in lang_dirs_seen:
                src_fn, lang_name = self._pick_lang_file(lang_dir, all_lower_to_orig)
                if src_fn is None:
                    continue
                fn_lower = src_fn.lower()
                fn       = src_fn
                lang_suffix = '.lang' if fn_lower.endswith('.lang') else '.json'

                zh_tw_lower = lang_dir + 'zh_tw' + lang_suffix
                if zh_tw_lower in all_names_lower:
                    # zh_tw.json 已存在 → 讀兩個檔，只收集缺少的 key
                    try:
                        with jar.open(fn) as f:
                            src_str = self.safe_decode_bytes(f.read())
                        src_data = load_lang_content(src_str, fn, self._clean_json_text)
                        zh_tw_fn = all_lower_to_orig[zh_tw_lower]
                        with jar.open(zh_tw_fn) as f:
                            zh_tw_str = self.safe_decode_bytes(f.read())
                        try:
                            zh_tw_data = load_lang_content(zh_tw_str, zh_tw_fn, self._clean_json_text)
                        except (json.JSONDecodeError, ValueError):
                            zh_tw_data = {}
                        zh_base_data = zh_tw_data
                        zh_cn_lower = lang_dir + 'zh_cn' + lang_suffix
                        if lang_name != 'zh_cn' and zh_cn_lower in all_names_lower:
                            try:
                                zh_cn_fn = all_lower_to_orig[zh_cn_lower]
                                with jar.open(zh_cn_fn) as f:
                                    zh_cn_str = self.safe_decode_bytes(f.read())
                                zh_cn_data = load_lang_content(
                                    zh_cn_str, zh_cn_fn, self._clean_json_text)
                                if isinstance(zh_cn_data, dict):
                                    zh_base_data = merge_zh_base_fallback(zh_cn_data, zh_tw_data)
                            except Exception:
                                pass
                        if process_mode == "force":
                            if lang_name != 'en_us':
                                self.log(f"ℹ️ {os.path.basename(path)}: 使用 {os.path.basename(fn)} 作為翻譯來源（無 en_us）")
                            lang_files[fn] = src_data
                            # 即使在 force 模式下，也要保留 zh_base（含 zh_cn fallback），
                            # 否則 zh_cn 的翻譯會被浪費，輸出時無法合併
                            if zh_base_data:
                                zh_base_local[fn] = zh_base_data
                            continue
                        # 保留 zh_tw 未覆蓋、空值、值等同原文（未翻譯複製）、
                        # 或混英值（已含中文但殘留英文單詞）的條目
                        missing = {k: v for k, v in src_data.items()
                                   if isinstance(v, str) and (
                                       k not in zh_tw_data
                                       or not isinstance(zh_tw_data[k], str)
                                       or not zh_tw_data[k].strip()
                                       or zh_tw_data[k] == v
                                       or zh_tw_data[k] == k
                                       or self._lang_value_needs_update(v, zh_tw_data[k])
                                   )}
                        total_text = sum(1 for v in src_data.values() if isinstance(v, str) and v.strip())
                        if process_mode == "skip90" and total_text and len(missing) <= total_text * 0.1:
                            continue
                        if missing:
                            if lang_name != 'en_us':
                                self.log(f"ℹ️ {os.path.basename(path)}: 使用 {os.path.basename(fn)} 作為翻譯來源（無 en_us）")
                            lang_files[fn]    = missing
                            zh_base_local[fn] = zh_base_data
                    except Exception as e:
                        # 不能靜默丟棄整個語言目錄，否則該模組「整包沒被翻」卻無從追查
                        self.log(f"⚠️ {os.path.basename(path)}: 解析 {fn} 失敗，已跳過此語言檔（{e}）")
                    continue

                # zh_tw.json 不存在 → 整個語言檔都需要翻譯
                try:
                    with jar.open(fn) as f:
                        decoded = self.safe_decode_bytes(f.read())
                    if not decoded.strip():
                        continue
                    data = load_lang_content(decoded, fn, self._clean_json_text)
                    zh_cn_lower = lang_dir + 'zh_cn' + lang_suffix
                    if zh_cn_lower in all_names_lower and lang_name != 'zh_cn':
                        try:
                            zh_cn_fn = all_lower_to_orig[zh_cn_lower]
                            with jar.open(zh_cn_fn) as f:
                                zh_cn_str = self.safe_decode_bytes(f.read())
                            zh_cn_data = load_lang_content(zh_cn_str, zh_cn_fn, self._clean_json_text)
                            if isinstance(zh_cn_data, dict):
                                zh_base_local[fn] = zh_cn_data
                        except Exception:
                            pass
                    if lang_name != 'en_us':
                        self.log(f"ℹ️ {os.path.basename(path)}: 使用 {os.path.basename(fn)} 作為翻譯來源（無 en_us）")
                    lang_files[fn] = data
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    self.log(f"⚠️ 無法解析 {fn}: {e}")

            # ── 書本/指南：Patchouli JSON + 自訂手冊 JSON/TXT（如 Alex's Mobs Animal Dictionary） ──
            book_texts = {}
            book_text_repairs = {}
            for idx, info in enumerate(infos):
                if not (scope_books or scope_quests):
                    break
                if idx % 1000 == 0 and scan_expired("手冊/任務"):
                    break
                fn_lower = info.filename.lower()
                fn       = info.filename
                source_locale = locale_segment(fn)
                if source_locale == 'zh_tw':
                    # zh_tw 是輸出目標；由對應來源檔決定是否修補，避免把目標檔再當來源掃一次。
                    continue
                zh_path_for_source = translated_lang_path(fn)
                preferred_manual_source = (
                    bool(zh_path_for_source)
                    and is_preferred_manual_source(fn)
                )
                is_localized_manual_candidate = (
                    source_locale
                    and scope_books
                    and preferred_manual_source
                    and (
                        '/patchouli_books/' in fn_lower
                        or (fn_lower.startswith('assets/') and '/book/' in fn_lower)
                        or is_localized_manual_resource(fn)
                    )
                )
                if (is_localized_manual_candidate
                        and selected_manual_sources_lower
                        and fn_lower not in selected_manual_sources_lower):
                    continue
                is_patchouli = (scope_books
                                and '/patchouli_books/' in fn_lower
                                and fn_lower.endswith('.json')
                                and preferred_manual_source)
                is_patchouli_book_json = (scope_books
                                          and '/patchouli_books/' in fn_lower
                                          and fn_lower.endswith('/book.json')
                                          and preferred_manual_source)
                is_advancement_json = (scope_quests
                                       and fn.startswith('data/')
                                       and '/advancements/' in fn_lower
                                       and fn_lower.endswith('.json'))
                is_book_json = (scope_books
                                and fn_lower.startswith('assets/')
                                and '/book/' in fn_lower
                                and fn_lower.endswith('.json')
                                and preferred_manual_source)
                is_book_txt = (scope_books
                               and fn_lower.startswith('assets/')
                               and '/book/' in fn_lower
                               and fn_lower.endswith('.txt')
                               and preferred_manual_source)
                is_localized_manual_json = (
                    scope_books
                    and is_localized_manual_resource(fn)
                    and fn_lower.endswith('.json')
                    and preferred_manual_source)
                is_localized_manual_txt = (
                    scope_books
                    and is_localized_manual_resource(fn)
                    and fn_lower.endswith('.txt')
                    and preferred_manual_source)
                if not (is_patchouli or is_patchouli_book_json or is_book_json
                        or is_book_txt or is_localized_manual_json
                        or is_localized_manual_txt or is_advancement_json):
                    continue
                if zh_path_for_source and source_locale:
                    zh_tw_lower_p = zh_path_for_source.lower()
                    if zh_tw_lower_p in all_names_lower and process_mode != "force":
                        if is_book_txt or is_localized_manual_txt:
                            zh_tw_fn = all_lower_to_orig.get(zh_tw_lower_p)
                            if zh_tw_fn:
                                try:
                                    with jar.open(zh_tw_fn) as f:
                                        zh_decoded = self.safe_decode_bytes(f.read())
                                    # 按「段落」判斷：既有 zh_tw 只要還有可翻的英文段落
                                    # （含部分翻譯的檔），就要重新走翻譯流程；
                                    # 只有「整檔已無英文段落」才走純排版修正。
                                    # 舊版用「檔內有沒有中文」二分 → 半本英文的檔永遠卡死
                                    if (zh_decoded.strip()
                                            and not self._book_text_translatable_paragraphs(zh_decoded)):
                                        book_text_repairs[zh_tw_fn] = zh_decoded
                                        continue
                                except (UnicodeDecodeError, OSError):
                                    pass
                                # 落到這裡 = zh_tw 是英文殘留或讀取失敗 → 走完整翻譯流程
                            else:
                                continue
                        else:
                            zh_tw_fn = all_lower_to_orig.get(zh_tw_lower_p)
                            if not zh_tw_fn:
                                continue
                            try:
                                with jar.open(fn) as f:
                                    src_decoded = self.safe_decode_bytes(f.read())
                                with jar.open(zh_tw_fn) as f:
                                    zh_decoded = self.safe_decode_bytes(f.read())
                                src_data = load_json_lenient(src_decoded, self._clean_json_text)
                                zh_tw_data = load_json_lenient(zh_decoded, self._clean_json_text)
                                if not structured_json_needs_update(
                                        src_data, zh_tw_data, self._lang_value_needs_update):
                                    continue
                                zh_base_data = zh_tw_data
                                zh_cn_lower_p = replace_locale_segment(fn_lower, 'zh_cn')
                                if source_locale != 'zh_cn' and zh_cn_lower_p in all_names_lower:
                                    try:
                                        zh_cn_fn = all_lower_to_orig[zh_cn_lower_p]
                                        with jar.open(zh_cn_fn) as f:
                                            zh_cn_decoded = self.safe_decode_bytes(f.read())
                                        zh_cn_data = load_json_lenient(
                                            zh_cn_decoded, self._clean_json_text)
                                        if isinstance(zh_cn_data, dict):
                                            zh_base_data = merge_zh_base_fallback(
                                                zh_cn_data, zh_tw_data)
                                    except Exception:
                                        pass
                                lang_files[fn] = src_data
                                zh_base_local[fn] = zh_base_data
                                continue
                            except Exception:
                                # 既有 zh_tw 讀不出或格式不穩，改用來源重新產生安全覆蓋。
                                pass
                    zh_cn_lower_p = replace_locale_segment(fn_lower, 'zh_cn')
                    if source_locale != 'zh_cn' and zh_cn_lower_p in all_names_lower:
                        try:
                            zh_cn_fn = all_lower_to_orig[zh_cn_lower_p]
                            with jar.open(zh_cn_fn) as f:
                                zh_cn_decoded = self.safe_decode_bytes(f.read())
                            if is_book_txt or is_localized_manual_txt:
                                if not (source_locale and source_locale.startswith('en_')):
                                    zh_tw_fn = all_lower_to_orig.get(
                                        zh_tw_lower_p,
                                        replace_locale_segment(fn, 'zh_tw'))
                                    book_text_repairs[zh_tw_fn] = self._to_traditional(zh_cn_decoded)
                                    continue
                            else:
                                zh_cn_data = load_json_lenient(zh_cn_decoded, self._clean_json_text)
                                if isinstance(zh_cn_data, dict):
                                    zh_base_local[fn] = zh_cn_data
                        except Exception:
                            pass
                try:
                    with jar.open(info) as f:
                        decoded = self.safe_decode_bytes(f.read())
                    if not decoded.strip():
                        continue
                    if is_book_txt or is_localized_manual_txt:
                        book_texts[fn] = decoded
                        continue
                    lang_files[fn] = load_json_lenient(decoded, self._clean_json_text)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    self.log(f"⚠️ 無法解析 {fn}: {e}")
            if book_texts:
                with self._jar_lock:
                    self.analyzed_book_texts[path] = book_texts
            if book_text_repairs:
                with self._jar_lock:
                    self.analyzed_book_text_repairs[path] = book_text_repairs

            class_texts = {}
            # 用 _analyze_task 開頭快照的值，不在掃描執行緒裡碰 tkinter 變數。
            # class 硬編碼只掃 item/block tooltip，輸出階段仍會避開高風險 JAR。
            scan_classes = (
                scope_mod_lang
                and not server_mode
                and getattr(self, "_scan_class_tooltip_patch", True)
                and getattr(self, "_scan_output_mode", "resource_pack") in ("hybrid", "jar_patch")
            )
            if scan_classes:
                for idx, info in enumerate(infos):
                    if idx % 250 == 0 and scan_expired("class tooltip"):
                        break
                    fn = info.filename
                    fn_lower = fn.lower()
                    if not fn_lower.endswith('.class'):
                        continue
                    # Only item classes are patched (tooltip 文字主要所在處)。
                    # Block/config/datagen/recipe/world classes often contain JVM
                    # invokedynamic string recipes (\x01 placeholders);
                    # translating those can make the mod fail before Minecraft starts.
                    if '/item/' not in fn_lower:
                        continue
                    if any(skip in fn_lower for skip in (
                            '/api/', '/hooks/', '/natives/', '/event/',
                            '/action/', '/capabilities/', '/example/',
                            '/test/', '/tests/')):
                        continue
                    try:
                        with jar.open(info) as f:
                            class_data = f.read()
                        strings = []
                        for entry in self._class_utf8_entries(class_data):
                            text = entry["text"]
                            if self._is_hardcoded_lore_string(text):
                                strings.append(text)
                        if strings:
                            class_texts[fn] = sorted(set(strings))
                    except (OSError, zipfile.BadZipFile):
                        continue
            if class_texts:
                with self._jar_lock:
                    self.analyzed_class_texts[path] = class_texts
    except (zipfile.BadZipFile, OSError) as e:
        self.log(f"⚠️ 無法開啟 JAR {os.path.basename(path)}: {e}")

    if lang_files:
        with self._jar_lock:
            self.analyzed_jars[path] = lang_files
            if zh_base_local:
                self.analyzed_jars_zh_base[path] = zh_base_local


def run_analyze_task(self, mod_dir):
    try:
        self._analyze_task_impl(mod_dir)
    except Exception as e:
        details = traceback.format_exc()
        try:
            os.makedirs(self.failed_items_dir, exist_ok=True)
            report = os.path.join(
                self.failed_items_dir,
                time.strftime("%Y%m%d_%H%M%S_analysis_error.txt"))
            with open(report, "w", encoding="utf-8") as f:
                f.write(details)
            self.log(f"❌ 分析流程中斷：{e}")
            self.log(f"   詳細錯誤已寫入：{report}")
        except Exception:
            self.log(f"❌ 分析流程中斷：{e}")

        def finish_failed():
            self.is_processing = False
            self._set_btn_state(self.btn_analyze,   tk.NORMAL)
            self._set_btn_state(self.btn_translate, tk.NORMAL)
            self._set_btn_state(self.btn_stop,      tk.DISABLED)
            self._set_btn_state(self.btn_pause,     tk.DISABLED)
            self._set_summary_card("cache", "分析失敗", "請查看執行記錄", self.C_DANGER)
            self._set_summary_card("pending", "分析失敗", "請查看執行記錄", self.C_DANGER)
        self.root.after(0, finish_failed)


def run_analyze_task_impl(self, mod_dir):
    self.analyzed_mc_dir = mod_dir          # 記錄本次分析的根目錄
    # 伺服器模式偵測：根目錄有 server.properties 即視為 dedicated server
    self._server_mode = os.path.exists(os.path.join(mod_dir, 'server.properties'))
    if self._server_mode:
        self.log("\n🖥️ 偵測到 server.properties → 進入「伺服器模式」")
        self.log("   翻譯範圍：FTB 任務書(.snbt)、advancement 顯示文字、Apotheosis 命名表")
        self.log("   （這些由伺服器同步給所有玩家，不需要玩家裝任何補丁）")
        self.log("   自動跳過：mod 語言檔/手冊/書本（dedicated server 只認 en_us，翻了無效）、")
        self.log("   class 硬編碼修補（伺服器端壞一個 class = 全服崩潰，風險過高）")
    self._scan_process_mode = self.process_mode_var.get() if hasattr(self, "process_mode_var") else "append"
    self._scan_output_mode = self.output_mode_var.get() if hasattr(self, "output_mode_var") else "hybrid"
    self._scan_class_tooltip_patch = bool(
        getattr(getattr(self, "class_tooltip_patch_var", None), "get", lambda: True)())
    self._scan_scope_mod_lang = self.scope_mod_lang_var.get()
    self._scan_scope_books = self.scope_books_var.get()
    self._scan_scope_quests = self.scope_quests_var.get()
    process_mode = self._scan_process_mode
    scope_mod_lang = self._scan_scope_mod_lang
    scope_books = self._scan_scope_books
    scope_quests = self._scan_scope_quests
    self.log("\n--- 開始全域掃描分析（JAR 並發掃描已啟用）---")
    self.log("掃描對象：語言檔 / Patchouli 手冊 / FTB 任務書(.snbt) / 任務 JSON / Markdown")
    jar_paths = []
    self._last_scan_jar_paths = jar_paths

    # 任務書相關目錄關鍵字（snbt / json 過濾用）
    # puffish_skills = Pufferfish's Skills 天賦樹（Prominence II 等包的
    # config/puffish_skills/**/category.json、definitions.json 含技能標題/說明）
    QUEST_KW = ('ftbquests', 'ftb_quests', 'ftb-quests', 'quests',
                'heracles', 'odyssey', 'betterquesting', 'customnpcs',
                'questbook', 'patchouli_books', 'puffish_skills')

    # config/ 路徑中不需要翻譯的設定檔目錄
    SKIP_CONFIG_KW = ('crash_assistant', 'nochatreports', 'shader',
                      'optifine', 'iris', 'complementary', 'euphoria',
                      'local/crash', 'gpu-detect')

    _seen_lang_dirs = set()   # 避免同一個 lang/ 目錄被多個檔案觸發重複掃描

    for root_dir, dirs, files in os.walk(mod_dir):
        if self.stop_requested:
            break
        dirs[:] = [d for d in dirs if should_descend_scan_dir(mod_dir, root_dir, d)]

        for file in files:
            if self.stop_requested:
                break
            path      = os.path.join(root_dir, file)
            path_norm = path.replace('\\', '/').lower()
            rel_norm  = _rel_lower(mod_dir, path)
            ext       = file.lower().rsplit('.', 1)[-1] if '.' in file else ''

            if ext == 'jar':
                if should_scan_translation_archive(mod_dir, path):
                    jar_paths.append(path)

            elif ext == 'zip':
                if not should_scan_translation_archive(mod_dir, path):
                    continue
                in_dp = (
                    rel_norm.startswith('datapacks/')
                    or rel_norm.startswith('moonlight-global-datapacks/')
                    or rel_norm.startswith('global_packs/')
                    or rel_norm.startswith('globalpacks/')
                    or '/datapacks/' in rel_norm
                    or '/paxi/datapacks/' in rel_norm
                )
                in_rp = (
                    rel_norm.startswith('resourcepacks/')
                    or rel_norm.startswith('global_packs/')
                    or rel_norm.startswith('globalpacks/')
                    or '/resourcepacks/' in rel_norm
                    or '/paxi/resourcepacks/' in rel_norm
                )
                in_openloader_resources = self._is_openloader_resources_zip_path(path_norm)
                try:
                    with zipfile.ZipFile(path, 'r') as z:
                        has_en_lang = False
                        for info in z.infolist():
                            if (info.is_dir()
                                    or not is_safe_archive_path(info.filename)):
                                continue
                            fn_lower = info.filename.lower()
                            # 資源包帶 en_us 語言覆寫 → 整包當 JAR 掃描/修補
                            if ((in_rp or in_openloader_resources)
                                    and fn_lower.startswith('assets/')
                                    and fn_lower.endswith('/lang/en_us.json')):
                                has_en_lang = True
                            # Origins 類 datapack JSON（原有功能）
                            if ((scope_quests or scope_books)
                                    and fn_lower.endswith('.json')
                                    and fn_lower.startswith('data/')
                                    and ('/origins/' in fn_lower
                                         or '/powers/' in fn_lower
                                         or '/origin_layers/' in fn_lower
                                         or '/classes/' in fn_lower)):
                                self.analyzed_zip_json.append((path, info.filename))
                        if has_en_lang and scope_mod_lang:
                            jar_paths.append(path)
                except (zipfile.BadZipFile, OSError) as e:
                    self.log(f"⚠️ 無法開啟 ZIP {os.path.basename(path)}: {e}")

            elif ext == 'snbt':
                # 只收集任務書相關的 snbt（排除 ftblibrary 設定等）
                if scope_quests and any(kw in path_norm for kw in QUEST_KW):
                    if not any(sk in path_norm for sk in SKIP_CONFIG_KW):
                        # 語言檔模式（quests/lang/<lang>.snbt）：只拿 en_us 當來源，
                        # 其他語言檔（zh_cn/es_es/ja_jp…）一律跳過——絕不就地覆寫，
                        # 翻譯結果會另存為 zh_tw.snbt（見輸出階段）
                        is_lang, lang_code, _ = ftbq_lang_snbt_role(path_norm)
                        if is_lang and lang_code != 'en_us':
                            pass
                        else:
                            self.analyzed_extra.append(('snbt', path))

            elif ext == 'cfg':
                # Apotheosis 隨機 Boss / 武器命名表（純顯示用，官方提供的覆寫機制）
                # 涵蓋「Jesus the Pig Thief」「Rock Spade」這類遊戲內隨機名稱。
                # 伺服器模式必收（boss 名由 server 同步全員），不受模組語言勾選影響
                if ((scope_mod_lang or getattr(self, "_server_mode", False))
                        and path_norm.endswith('/apotheosis/names.cfg')):
                    self.analyzed_extra.append(('apoth_names', path))

            elif ext == 'md':
                # Markdown 說明文件（位於任務書相關路徑）
                if (scope_books or scope_quests) and any(kw in path_norm for kw in QUEST_KW):
                    if not any(sk in path_norm for sk in SKIP_CONFIG_KW):
                        self.analyzed_extra.append(('md', path))

            elif ext in ('json', 'lang'):
                base = os.path.basename(path).lower()
                if '/lang/' in path_norm and (base.endswith('.json') or base.endswith('.lang')) and (
                        scope_mod_lang or
                        (scope_quests and self._is_quest_path(path_norm))):
                    lang_dir = os.path.dirname(path)
                    lang_dir_norm = lang_dir.replace('\\', '/').lower() + '/'
                    # 只在第一次遇到這個 lang/ 目錄時處理（避免同目錄重複掃描）
                    if lang_dir_norm not in _seen_lang_dirs:
                        _seen_lang_dirs.add(lang_dir_norm)
                        # 依 fallback 順序挑選來源語言檔
                        src_path = None
                        src_lang = None
                        for candidate in self._LANG_FALLBACK_ORDER:
                            cand_path = os.path.join(lang_dir, candidate)
                            if os.path.exists(cand_path):
                                src_path = cand_path
                                src_lang = os.path.splitext(candidate)[0]
                                break
                        # 找不到英文系或 zh_cn 來源 → 跳過，不從俄/葡/日/韓等
                        # 非英文語言翻譯（引擎 en→zh 取向，拿那些當來源會出亂碼）
                        if src_path is None:
                            pass  # 此 lang/ 目錄無英文系來源，跳過
                        else:
                            zh_tw_path = os.path.join(
                                lang_dir,
                                'zh_tw' + ('.lang' if src_path.lower().endswith('.lang') else '.json'))
                            if os.path.exists(zh_tw_path):
                                # zh_tw.json 已存在 → 讀取並找出缺少的 key
                                try:
                                    src_content = self.safe_read_file(src_path)
                                    zh_content  = self.safe_read_file(zh_tw_path)
                                    if src_content.strip():
                                        src_data = load_lang_content(
                                            src_content, src_path, self._clean_json_text)
                                        if zh_content.strip():
                                            try:
                                                zh_data = load_lang_content(
                                                    zh_content, zh_tw_path, self._clean_json_text)
                                            except (json.JSONDecodeError, ValueError):
                                                zh_data = {}
                                        else:
                                            zh_data = {}
                                        if process_mode == "force":
                                            if src_lang != 'en_us':
                                                self.log(f"ℹ️ 使用 {src_lang} 作為翻譯來源（無 en_us）：{src_path}")
                                            self.analyzed_loose.append(src_path)
                                            continue
                                        missing = {k: v for k, v in src_data.items()
                                                   if isinstance(v, str) and (
                                                       k not in zh_data
                                                       or not isinstance(zh_data[k], str)
                                                       or not zh_data[k].strip()
                                                       or zh_data[k] == v
                                                       or zh_data[k] == k
                                                       or self._lang_value_needs_update(v, zh_data[k])
                                                   )}
                                        total_text = sum(1 for v in src_data.values() if isinstance(v, str) and v.strip())
                                        if (process_mode == "skip90"
                                                and total_text and len(missing) <= total_text * 0.1):
                                            continue
                                        if missing:
                                            if src_lang != 'en_us':
                                                self.log(f"ℹ️ 使用 {src_lang} 作為翻譯來源（無 en_us）：{src_path}")
                                            self.analyzed_loose.append(src_path)
                                            self.analyzed_loose_base[src_path] = zh_data
                                except Exception as e:
                                    self.log(f"⚠️ 解析散裝語言檔失敗，已跳過：{src_path}（{e}）")
                            else:
                                if src_lang != 'en_us':
                                    self.log(f"ℹ️ 使用 {src_lang} 作為翻譯來源（無 en_us）：{src_path}")
                                self.analyzed_loose.append(src_path)
                                if len(self.analyzed_loose) % 100 == 0:
                                    self.log(f"🔍 已掃描 {len(self.analyzed_loose)} 個語言檔...")
                elif ext == 'json' and scope_quests and any(kw in path_norm for kw in QUEST_KW):
                    # 任務 / 手冊相關 JSON
                    if '/lang/' not in path_norm:
                        if not any(sk in path_norm for sk in SKIP_CONFIG_KW):
                            self.analyzed_extra.append(('json', path))
                elif ext == 'json' and scope_mod_lang and self._is_mmorpg_data_json_path(path_norm):
                    if not any(sk in path_norm for sk in SKIP_CONFIG_KW):
                        try:
                            preview = self.safe_read_file(path)
                            if '"loc_name"' in preview or '"flavor_text"' in preview:
                                self.analyzed_extra.append(('mns_json', path))
                        except OSError:
                            pass
                elif ext == 'lang' and scope_quests and any(kw in path_norm for kw in QUEST_KW):
                    if '/lang/' not in path_norm and not any(sk in path_norm for sk in SKIP_CONFIG_KW):
                        self.analyzed_extra.append(('lang', path))

    if jar_paths and not self.stop_requested:
        self.log(f"📦 發現 {len(jar_paths)} 個 JAR，開始並發解析語言檔與手冊頁面...")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        futures = {executor.submit(self._scan_single_jar, p): p for p in jar_paths}
        pending = set(futures)
        done_count = 0
        last_status = time.time()
        start_times = {f: time.time() for f in futures}  # 記錄每個 future 的開始時間
        try:
            while pending and not self.stop_requested:
                done, pending = concurrent.futures.wait(
                    pending, timeout=1.0,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    now = time.time()
                    if now - last_status >= 10:
                        self.log(
                            f"⏳ JAR 掃描中：{done_count}/{len(futures)} 完成，"
                            f"剩餘 {len(pending)} 個...")
                        # 檢查是否有處理超過 90 秒的 JAR（可能卡住）
                        for future in pending:
                            elapsed = now - start_times.get(future, now)
                            if elapsed > 90:
                                jar_path = futures[future]
                                self.log(f"⚠️ {os.path.basename(jar_path)} 處理時間過長（{int(elapsed)}秒），可能卡住")
                        self.set_current_item(
                            f"JAR 掃描中：{done_count}/{len(futures)} 完成",
                            force=True)
                        last_status = now
                    continue
                for future in done:
                    done_count += 1
                    try:
                        future.result()
                    except Exception as e:
                        jar_path = futures.get(future, "未知")
                        self.log(f"⚠️ 掃描 {os.path.basename(str(jar_path))} 失敗: {e}")
                    if done_count == len(futures) or done_count % 25 == 0:
                        self.set_current_item(
                            f"JAR 掃描中：{done_count}/{len(futures)} 完成",
                            force=True)
            if self.stop_requested:
                self._shutdown_executor_now(executor)
            else:
                executor.shutdown(wait=True)
        finally:
            if self.stop_requested:
                self._shutdown_executor_now(executor)


    if not self.stop_requested:
        self.log("✅ JAR/資料夾掃描完成，正在整理分析結果...")
        try:
            self._record_update_detection(mod_dir)
        except Exception as e:
            self.log(f"⚠️ 模組更新偵測失敗，已跳過：{e}")
        try:
            self._build_global_memory_pool(mod_dir)
        except Exception as e:
            self.log(f"⚠️ 全域翻譯記憶池建立失敗，已跳過：{e}")

    if not self.stop_requested:
        try:
            self.set_current_item("統計待翻譯詞彙...", force=True)
            self._count_analysis_strings()
            self.log(
                f"📊 待翻譯統計完成：總計 {getattr(self, '_analysis_total_strings', 0):,}，"
                f"已命中 {getattr(self, '_analysis_cache_hits', 0):,}，"
                f"待翻譯 {getattr(self, '_analysis_missing_strings', 0):,}")
        except Exception as e:
            self.log(f"⚠️ 分析統計失敗：{e}")
            self._analysis_total_strings = 0
            self._analysis_cache_hits = 0
            self._analysis_memory_hits = 0
            self._analysis_missing_strings = 0

    def finish_analysis():
        try:
            if self.stop_requested:
                msg = "⏸️ 掃描已暫停。" if self.pause_requested else "🛑 掃描中止。"
                self.log(msg)
                return

            snbt_n = sum(1 for t, _ in self.analyzed_extra if t == 'snbt')
            json_n = sum(1 for t, _ in self.analyzed_extra if t == 'json')
            mns_json_n = sum(1 for t, _ in self.analyzed_extra if t == 'mns_json')
            md_n   = sum(1 for t, _ in self.analyzed_extra if t == 'md')
            lang_n = sum(1 for t, _ in self.analyzed_extra if t == 'lang')
            zip_json_n = len(self.analyzed_zip_json)
            book_txt_n = sum(len(files) for files in self.analyzed_book_texts.values())
            class_text_n = sum(len(strings)
                               for files in self.analyzed_class_texts.values()
                               for strings in files.values())
            self.log(f"✅ 結果：{len(self.analyzed_jars)} 個 JAR、"
                     f"{len(self.analyzed_loose)} 個語言檔、"
                     f"{snbt_n} 個任務書(.snbt)、"
                     f"{json_n} 個任務JSON、{mns_json_n} 個 MineSlash JSON、"
                     f"{lang_n} 個.lang、{md_n} 個MD、"
                     f"{book_txt_n} 個書本TXT、"
                     f"{zip_json_n} 個 ZIP 內 Origins JSON、"
                     f"{class_text_n} 個 class 硬編碼敘述")
            if snbt_n == 0:
                self.log("⚠️  未偵測到 .snbt 任務書！請確認「Minecraft 資料夾」"
                         "已選擇遊戲實例根目錄（含 config/ 資料夾的那層）。")
            try:
                self._refresh_right_summary()
            except Exception as e:
                self.log(f"⚠️ 右側摘要更新失敗，但分析結果可用：{e}")
            self.set_current_item("分析完成", force=True)
        except Exception as e:
            self.log(f"⚠️ 分析完成 UI 更新失敗，但已解鎖開始翻譯：{e}")
            self.set_current_item("分析完成（摘要更新失敗）", force=True)
        finally:
            self.is_processing = False
            self._set_btn_state(self.btn_analyze, tk.NORMAL)
            self._set_btn_state(self.btn_stop,    tk.DISABLED)
            self._set_btn_state(self.btn_pause,   tk.DISABLED)
            if not self.stop_requested:
                self._set_btn_state(self.btn_translate, tk.NORMAL)
                if getattr(self, "_auto_translate_after_analysis", False):
                    self._auto_translate_after_analysis = False
                    self.log("INFO  自動啟動：分析完成，開始翻譯。")
                    self.root.after(300, self.start_translation)

    self.root.after(0, finish_analysis)
