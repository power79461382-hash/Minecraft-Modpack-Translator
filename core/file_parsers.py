import json
import re


_RE_SNBT_TOKEN = re.compile(
    r'//[^\r\n]*|/\*.*?\*/'
    r'|"((?:[^"\\]|\\.)*)"'
    r"|'((?:[^'\\]|\\.)*)'"
    r'|[{}\[\]:,]|[^\s{}\[\]:,"\'/]+',
    re.DOTALL)


def iter_snbt_quoted_values(self, content):
    """Yield double-quoted values with their field, tracking nested containers.

    Technical fields protect their entire subtree. Keys and comment strings
    never become translation input, and list values inherit the list's field.
    """
    tokens = (match for match in _RE_SNBT_TOKEN.finditer(content)
              if not match.group(0).startswith(('//', '/*')))
    stack = [{'kind': '{', 'key': None, 'owner': None, 'technical': None}]
    current = next(tokens, None)
    while current is not None:
        following = next(tokens, None)
        token = current.group(0)
        frame = stack[-1]
        key = frame['key'] if frame['kind'] == '{' else frame['owner']
        effective_key = frame['technical'] or key
        if following is not None and following.group(0) == ':':
            if current.group(1) is not None:
                frame['key'] = self._decode_snbt_string(current.group(1))
            elif current.group(2) is not None:
                frame['key'] = current.group(2)
            else:
                frame['key'] = token
        elif token in ('{', '['):
            technical = frame['technical']
            if key and self._is_technical_data_key(key):
                technical = key
            if frame['kind'] == '{':
                frame['key'] = None
            stack.append({'kind': token, 'key': None,
                          'owner': key, 'technical': technical})
        elif token in ('}', ']'):
            if len(stack) > 1:
                stack.pop()
        elif token not in (':', ','):
            if current.group(1) is not None:
                yield current, effective_key
            if frame['kind'] == '{':
                frame['key'] = None
        current = following


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
    def replacer(match, snbt_key):
        if self.stop_requested:
            return match.group(0)
        # 引號字串若是「鍵」（後面跟著冒號）絕不能翻譯——
        # 改了鍵名整個欄位就消失（如 "description": [...] 變 "描述": [...]）
        rest = content[match.end():match.end() + 8]
        if rest.lstrip()[:1] == ':':
            return match.group(0)
        raw_text = match.group(1)
        text = self._decode_snbt_string(raw_text)
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
    parts = []
    cursor = 0
    for match, snbt_key in iter_snbt_quoted_values(self, content):
        parts.append(content[cursor:match.start()])
        parts.append(replacer(match, snbt_key))
        cursor = match.end()
    parts.append(content[cursor:])
    translated_content = ''.join(parts)
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
    for match, key in iter_snbt_quoted_values(self, content):
        if match.start() == match_start:
            return key
        if match.start() > match_start:
            break
    return None


def snbt_skip_by_key(self, content: str, match_start: int) -> bool:
    """判斷 SNBT 中位於 match_start 的引號字串是否屬於技術性欄位（黑名單 key）。
    擷取該字串前方最後一個 key，避免漏掉同一行物件中的 type/id/item 等欄位。"""
    key = self._snbt_key_for_value(content, match_start)
    return bool(key and self._is_technical_data_key(key))


def iter_markdown_text_lines(content):
    """Yield original lines and spans of prose, excluding metadata and code."""
    in_yaml = False
    fence = None
    for index, line in enumerate(content.splitlines(keepends=True)):
        stripped = line.strip()
        if index == 0 and stripped.lstrip('\ufeff') == '---':
            in_yaml = True
            yield line, None
            continue
        if in_yaml:
            if stripped in ('---', '...'):
                in_yaml = False
                yield line, None
                continue
            match = re.match(
                r'^(\s*title\s*:\s*)(.*?)(\s*)$',
                line, re.IGNORECASE)
            if match is None:
                yield line, None
                continue
            start, end = match.span(2)
            value = match.group(2)
            quote = value[:1] if value[:1] in ('"', "'") else ''
            if quote:
                if len(value) < 2 or not value.endswith(quote):
                    yield line, None
                    continue
                if quote == "'":
                    if re.fullmatch(r"'(?:[^']|'')*'", value) is None:
                        yield line, None
                        continue
                    decoded = value[1:-1].replace("''", "'")
                else:
                    try:
                        decoded = json.loads(value)
                    except json.JSONDecodeError:
                        # YAML-specific escapes need a full YAML parser.
                        yield line, None
                        continue
                start += 1
                end -= 1
            else:
                if (value[:1] in ('!', '&', '*', '|', '>', '[', '{', '#')
                        or re.search(r'\s#|:\s', value)):
                    yield line, None
                    continue
                decoded = value
            yield line, (start, end, quote, decoded)
            continue
        if fence is not None:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0])
                            + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
            yield line, None
            continue
        match = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if match:
            marker = match.group(1)
            fence = marker[0], len(marker)
            yield line, None
            continue
        if not stripped or stripped.startswith('#!'):
            yield line, None
            continue
        yield line, (len(line) - len(line.lstrip()), len(line.rstrip()))


def markdown_text_for_span(line, span):
    """Return decoded YAML text or the prose used as a translation key."""
    return span[3] if len(span) == 4 else line[span[0]:span[1]].strip()


def process_md_file(self, content):
    """Translate Markdown prose while preserving code and unprocessed lines."""
    new_lines = []
    for line, span in iter_markdown_text_lines(content):
        if self.stop_requested or span is None:
            new_lines.append(line)
            continue
        start, end = span[:2]
        text = markdown_text_for_span(line, span)
        if text and self.should_translate(text):
            translated = self.get_translation(text)
            if translated == text:
                new_lines.append(line)
                continue
            if len(span) == 4:
                if span[2]:
                    start -= 1
                    end += 1
                translated = json.dumps(translated, ensure_ascii=False)
            new_lines.append(line[:start] + translated + line[end:])
        else:
            new_lines.append(line)
    return ''.join(new_lines)
