import json
import os
import re
import zipfile

from core.json_loader import load_json_lenient
from translation_packager import (
    load_json_content,
    load_lang_content,
    parse_legacy_lang_content,
)


def extract_all_unique_strings(self):
    """從已分析的 analyzed_jars / analyzed_loose / analyzed_extra 提取所有可翻譯字串。"""
    unique_strings = set()

    # ── JAR 內語言檔 & Patchouli 手冊 ──
    for lang_files in self.analyzed_jars.values():
        for path_in_jar, lang_data in lang_files.items():
            if not self._scope_allows_analyzed_path("jar", path_in_jar):
                continue
            self._collect_strings_json(
                lang_data, unique_strings,
                strict_context=self._is_structured_book_json_path(path_in_jar))

    # ── JAR 內自訂書本 txt（Alex's Mobs Animal Dictionary 等） ──
    for book_files in self.analyzed_book_texts.values():
        for path_in_jar, content in book_files.items():
            if not self._scope_allows_analyzed_path("book_txt", path_in_jar):
                continue
            for segment in self._book_text_translatable_paragraphs(content):
                unique_strings.add(segment)

    for class_files in self.analyzed_class_texts.values():
        for strings in class_files.values():
            for text in strings:
                if self.should_translate(text):
                    unique_strings.add(text)

    # ── 獨立 en_us.json ──
    for path in self.analyzed_loose:
        if not self._scope_allows_analyzed_path("loose", path):
            continue
        try:
            content = self.safe_read_file(path)
            if not content.strip():
                continue
            data = load_lang_content(content, path, self._clean_json_text)
            # 若有既有 zh_tw 基底，僅收集缺少的 key（避免重翻已翻譯的條目）
            zh_base = self.analyzed_loose_base.get(path)
            if zh_base and self.process_mode_var.get() != "force":
                data = {k: v for k, v in data.items()
                        if isinstance(v, str) and (
                            k not in zh_base
                            or not isinstance(zh_base[k], str)
                            or not zh_base[k].strip()
                            or zh_base[k] == v
                            or zh_base[k] == k
                            or self._lang_value_needs_update(v, zh_base[k])
                        )}
            self._collect_strings_json(data, unique_strings)
        except (json.JSONDecodeError, OSError) as e:
            self.log(f"⚠️ 跳過 {os.path.basename(path)}: {e}")

    # ── 附加檔案：snbt / md / json ──
    for file_type, path in self.analyzed_extra:
        if not self._scope_allows_extra(file_type, path):
            continue
        try:
            content = self.safe_read_file(path)
            if not content.strip():
                continue
            if file_type == 'snbt':
                # FTB Quests .snbt：只收集有引號的字串值（跳過技術性欄位）
                for m in self._RE_QUOTED_STR.finditer(content):
                    val = self._decode_snbt_string(m.group(1))
                    if self._snbt_skip_by_key(content, m.start()):
                        continue
                    snbt_key = self._snbt_key_for_value(content, m.start())
                    if (self.strict_whitelist_var.get() and snbt_key
                            and not self._is_strict_text_key(snbt_key)):
                        continue
                    component = self._json_text_component_obj(val)
                    if component is not None:
                        for text_value in self._walk_json_text_values(component):
                            if self.should_translate(text_value):
                                unique_strings.add(text_value)
                        continue
                    if not self.should_translate(val):
                        continue
                    unique_strings.add(val)
            elif file_type == 'apoth_names':
                for _line, entry in self._iter_apoth_name_lines(content):
                    if entry and self.should_translate(entry):
                        unique_strings.add(entry)
            elif file_type == 'md':
                # Markdown：逐行收集
                in_yaml = False
                for line in content.split('\n'):
                    stripped = line.strip()
                    if stripped == '---':
                        in_yaml = not in_yaml
                        continue
                    if in_yaml:
                        m = re.match(r'^\s*title\s*:\s*[\'"]?(.*?)[\'"]?\s*$', line, re.IGNORECASE)
                        if m and self.should_translate(m.group(1).strip()):
                            unique_strings.add(m.group(1).strip())
                        continue
                    if stripped and not stripped.startswith(('```', '#!')):
                        if self.should_translate(stripped):
                            unique_strings.add(stripped)
            elif file_type == 'json':
                data = load_json_lenient(content, self._clean_json_text)
                self._collect_strings_json(
                    data, unique_strings, preserve_technical_keys=True,
                    strict_context=self.strict_whitelist_var.get())
            elif file_type == 'mns_json':
                data = load_json_lenient(content, self._clean_json_text)
                self._collect_mmorpg_json_display_strings(data, unique_strings)
            elif file_type == 'lang':
                data = parse_legacy_lang_content(content)
                self._collect_strings_json(data, unique_strings, preserve_technical_keys=True)
        except Exception as e:
            self.log(f"⚠️ 跳過附加檔 {os.path.basename(path)}: {e}")

    for zip_path, internal_path in self.analyzed_zip_json:
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                content = self.safe_decode_bytes(z.read(internal_path))
            if not content.strip():
                continue
            data = load_json_content(content, self._clean_json_text)
            if isinstance(data, dict):
                for key in ("name", "description"):
                    value = data.get(key)
                    if isinstance(value, str) and self.should_translate(value):
                        unique_strings.add(value)
        except Exception as e:
            self.log(f"⚠️ 跳過 ZIP 內 JSON {os.path.basename(zip_path)}::{internal_path}: {e}")

    return unique_strings
