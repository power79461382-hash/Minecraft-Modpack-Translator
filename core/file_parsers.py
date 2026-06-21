import json
import re


def process_advancement_json(self, data):
    """Advancement 只能翻譯 display.title / display.description 的顯示文字
    （含元件陣列與 extra 巢狀形態）。criteria、requirements、trigger、conditions
    等全是引擎引用的技術識別字，翻譯任何一個都會讓該進度載入失敗
    （Unknown required criterion）並可能導致遊戲崩潰。"""
    if not isinstance(data, dict):
        return data
    out = json.loads(json.dumps(data, ensure_ascii=False))   # deep copy
    display = out.get("display")
    if isinstance(display, dict):
        for key in ("title", "description"):
            if key in display:
                display[key] = self._translate_text_component(display[key])
    return out


def snbt_escape(text: str) -> str:
    """將「純文字」完整重新跳脫為合法的 SNBT 雙引號字串內容。
    輸入一律是 _decode_snbt_string 解碼後的純文字（\\n 已是真換行、\\" 已是裸引號），
    因此必須完整重編碼——舊版「保留既有跳脫」的假設是錯的：
    結尾孤立反斜線會吃掉收尾引號、\\ 後接中文會產生非法跳脫序列，
    兩者都會讓 FTB Quests 整個章節檔解析失敗。"""
    return (text.replace('\\', '\\\\')
                .replace('"', '\\"')
                .replace('\r\n', '\n')
                .replace('\r', '\n')
                .replace('\n', '\\n')
                .replace('\t', '\\t'))


def process_apoth_names_cfg(self, content):
    """翻譯 names.cfg 的名稱清單條目；只改條目行的文字，
    結構（S:Xxx <、>、註解、屬性行）原樣保留，確保 Forge cfg 解析不受影響。"""
    out_lines = []
    for line, entry in self._iter_apoth_name_lines(content):
        if entry is None or not self.should_translate(entry):
            out_lines.append(line)
            continue
        translated = self.get_translation(entry)
        # 條目不可含換行或 '>'（會截斷清單），有就放棄翻譯保留原文
        translated = translated.replace('\n', ' ').replace('\r', ' ').strip()
        if translated == entry or not translated or '>' in translated:
            out_lines.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        out_lines.append(indent + translated)
    return '\n'.join(out_lines)


def snbt_structure_signature(content):
    """SNBT 結構簽章：跳脫感知地計算「字串外」的括號數與字串數。
    翻譯前後簽章必須一致，否則表示輸出壞檔（缺引號/括號錯位）。"""
    braces = brackets = strings = 0
    in_str = False
    escape = False
    for ch in content:
        if in_str:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            strings += 1
        elif ch == '{':
            braces += 1
        elif ch == '[':
            brackets += 1
    # in_str 殘留 True = 有未閉合字串
    return braces, brackets, strings, in_str


def process_text_file(self, content):
    """處理含有引號字串的純文字檔（如 .snbt），替換所有可翻譯字串。
    輸出前做結構驗證：翻譯後的括號/字串數必須與原檔一致，
    否則整檔退回原文（FTB Quests 的 snbt 解析失敗會讓任務書整章消失）。"""
    def replacer(match):
        if self.stop_requested:
            return match.group(0)
        # 引號字串若是「鍵」（後面跟著冒號）絕不能翻譯——
        # 改了鍵名整個欄位就消失（如 "description": [...] 變 "描述": [...]）
        rest = content[match.end():match.end() + 8]
        if rest.lstrip()[:1] == ':':
            return match.group(0)
        raw_text = match.group(1)
        text = self._decode_snbt_string(raw_text)
        snbt_key = self._snbt_key_for_value(content, match.start())
        if snbt_key and snbt_key.lower() == "type":
            repaired = self._repair_ftbq_type_value(snbt_key, text)
            if repaired != text:
                return f'"{self._snbt_escape(repaired)}"'
        if snbt_key and self._is_technical_data_key(snbt_key):
            return match.group(0)
        if (self.strict_whitelist_var.get() and snbt_key
                and not self._is_strict_text_key(snbt_key)):
            return match.group(0)
        component_translated = self._translate_json_text_component_string(text)
        if component_translated is not None:
            if component_translated == text:
                return match.group(0)
            return f'"{self._snbt_escape(component_translated)}"'
        if not self.should_translate(text):
            return match.group(0)
        translated = self.get_translation(text)
        if translated == text:
            return match.group(0)
        # 跳脫翻譯結果中的特殊字元，確保插回 SNBT 後語法仍合法
        # （防止翻譯文字含有 ASCII 雙引號時破壞 SNBT 結構，導致伺服器 FTBQuests 崩潰）
        return f'"{self._snbt_escape(translated)}"'
    translated_content = self._RE_QUOTED_STR.sub(replacer, content)
    if translated_content != content:
        # 只比對「前後簽章是否一致」：簽章解析器不認得 SNBT 單引號字串，
        # 含奇數個裸雙引號的合法檔案 in_str 會殘留 True——但原文與譯文一樣殘留，
        # 相等即放行；只有翻譯「製造出」差異（吃掉引號/括號）才退回原文
        before = self._snbt_structure_signature(content)
        after = self._snbt_structure_signature(translated_content)
        if before != after:
            self.log("⚠️ SNBT 結構驗證失敗（括號/字串數不一致），此檔保留原文以防壞檔")
            return content
    return translated_content


def snbt_key_for_value(self, content: str, match_start: int):
    line_start = content.rfind('\n', 0, match_start) + 1
    prefix = content[line_start:match_start]
    m = self._RE_SNBT_KEY_CTX.search(prefix)
    if not m:
        return None
    return m.group(1) or m.group(2)


def snbt_skip_by_key(self, content: str, match_start: int) -> bool:
    """判斷 SNBT 中位於 match_start 的引號字串是否屬於技術性欄位（黑名單 key）。
    擷取該字串前方最後一個 key，避免漏掉同一行物件中的 type/id/item 等欄位。"""
    key = self._snbt_key_for_value(content, match_start)
    return bool(key and self._is_technical_data_key(key))


def process_md_file(self, content):
    """逐行翻譯 Markdown 文件"""
    lines = content.split('\n')
    new_lines = []
    in_yaml = False
    for line in lines:
        if self.stop_requested:
            break
        stripped = line.strip()
        if stripped == '---':
            in_yaml = not in_yaml
            new_lines.append(line)
            continue
        if in_yaml:
            m = re.match(r'^(\s*title\s*:\s*[\'"]?)(.*?)([\'"]?\s*)$', line, re.IGNORECASE)
            if m and self.should_translate(m.group(2).strip()):
                title = m.group(2).strip()
                new_lines.append(m.group(1) + self.get_translation(title) + m.group(3))
            else:
                new_lines.append(line)
            continue
        if stripped and self.should_translate(stripped) \
                and not stripped.startswith(('```', '#!')):
            # 以 stripped 為快取 key（與 extract_all_unique_strings 一致）
            # 保留原始前後空白，避免破壞縮排結構
            translated = self.get_translation(stripped)
            leading  = line[:len(line) - len(line.lstrip())]
            trailing = line[len(line.rstrip()):]
            new_lines.append(leading + translated + trailing)
        else:
            new_lines.append(line)
    return '\n'.join(new_lines)
