"""JSON 文字清理工具。

從 main_window.py 提取的獨立模組，負責清理非標準 JSON 文字。
"""


def clean_json_text(text):
    """清理非標準 JSON 文字，依序處理：
    1. 移除 // 及 /* */ 注解（只處理字串外，避免誤刪 https://）
    2. 移除物件/陣列尾逗號
    3. 逐字元掃描：將 JSON 字串值內的所有控制字元（含未跳脫的換行）
       轉為合法的 \\uXXXX 跳脫序列，結構層的空白不受影響
    """
    if not isinstance(text, str):
        text = str(text)
    text = text.lstrip('\ufeff')

    def strip_comments(src):
        out = []
        in_string = False
        escape = False
        i = 0
        while i < len(src):
            c = src[i]
            n = src[i + 1] if i + 1 < len(src) else ''
            if in_string:
                out.append(c)
                if escape:
                    escape = False
                elif c == '\\':
                    escape = True
                elif c == '"':
                    in_string = False
                i += 1
                continue
            if c == '"':
                in_string = True
                out.append(c)
                i += 1
                continue
            if c == '/' and n == '/':
                i += 2
                while i < len(src) and src[i] not in '\r\n':
                    i += 1
                continue
            if c == '/' and n == '*':
                i += 2
                while i + 1 < len(src) and not (src[i] == '*' and src[i + 1] == '/'):
                    i += 1
                i += 2 if i + 1 < len(src) else 0
                continue
            out.append(c)
            i += 1
        return ''.join(out)

    def strip_trailing_commas(src):
        out = []
        in_string = False
        escape = False
        i = 0
        while i < len(src):
            c = src[i]
            if in_string:
                out.append(c)
                if escape:
                    escape = False
                elif c == '\\':
                    escape = True
                elif c == '"':
                    in_string = False
                i += 1
                continue
            if c == '"':
                in_string = True
                out.append(c)
                i += 1
                continue
            if c == ',':
                j = i + 1
                while j < len(src) and src[j] in ' \t\r\n':
                    j += 1
                if j < len(src) and src[j] in '}]':
                    i += 1
                    continue
            out.append(c)
            i += 1
        return ''.join(out)

    text = strip_trailing_commas(strip_comments(text))
    result = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            if escape:
                result.append(c)
                escape = False
            elif c == '\\':
                result.append(c)
                escape = True
            elif c == '"':
                in_string = False
                result.append(c)
            elif ord(c) < 0x20:
                result.append('\\u{:04x}'.format(ord(c)))
            else:
                result.append(c)
        else:
            if c == '"':
                in_string = True
                result.append(c)
            else:
                result.append(c)
        i += 1
    return ''.join(result)
