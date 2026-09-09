"""格式碼遮罩與還原工具。

從 main_window.py 提取的獨立模組，負責：
- 將 Minecraft 格式符號（§a %s {var} 等）替換成唯一佔位符
- 翻譯後將佔位符還原為原始格式符號

改進：佔位符使用 UUID 前綴防碰撞（優化 3）。
"""
import re

# 佔位符前綴：使用固定且不可能出現在自然語言中的格式，
# 同時加入隨機後綴避免多段文字拼接時碰撞。
# 格式：[@F<seq>@<token_id>@]  — seq 是序號，token_id 是防碰撞唯一標記
_MASK_PREFIX = "[@F"
_MASK_SUFFIX = "@]"


def mask_format(text, format_re):
    """將格式符號替換成唯一佔位符，回傳 (masked_text, mapping)。

    Args:
        text: 原始文字
        format_re: 已編譯的格式碼正則（ModTranslatorApp._RE_FORMAT）

    Returns:
        (masked_text, mapping): masked_text 是遮罩後的文字，
        mapping 是 {佔位符: 原始格式碼} 字典

    改進點（優化 3）：
    - 舊格式 [#N#] 在 AI 翻譯多段文字拼接時可能碰撞（如 [#0#] 出現在譯文裡）
    - 新格式 [@FN@<id>@] 加入隨機 id，碰撞概率極低
    - 同時 [@F 前綴在自然語言中幾乎不可能出現
    """
    if not text:
        return text, {}
    mapping = {}
    counter = [0]

    def _replace(m):
        marker = f"{_MASK_PREFIX}{counter[0]}@{_gen_token_id()}{_MASK_SUFFIX}"
        mapping[marker] = m.group(0)
        counter[0] += 1
        return marker

    masked = format_re.sub(_replace, text)
    return masked, mapping


def unmask_format(text, mapping):
    """將翻譯結果中的佔位符還原為原始格式符號（容錯空白）。

    Args:
        text: 含佔位符的翻譯文字
        mapping: mask_format 回傳的 mapping

    Returns:
        還原後的文字
    """
    if not mapping or not text:
        return text
    for marker, original in mapping.items():
        # 翻譯引擎可能在 marker 的任意字元間插入空白；每個字元仍需
        # 完整匹配，避免只憑序號誤替換模型自行產生的相似文字。
        pattern = r'\s*'.join(re.escape(char) for char in marker)
        text = re.sub(pattern, lambda _, o=original: o, text)
    return text


def _gen_token_id():
    """生成簡短唯一 ID 用於防碰撞。

    使用計數器 + 時間戳的低 位元，足夠區分同一批次內的佔位符，
    同時避免引入 uuid 的額外依賴和長度。
    """
    import time
    _gen_token_id._counter = getattr(_gen_token_id, '_counter', 0) + 1
    # 8 位 hex = 32 bit，碰撞概率 < 1/4B
    return f"{_gen_token_id._counter:04x}{int(time.time() * 1000) & 0xFFFF:04x}"


def fix_placeholders(text):
    """修復 AI／機器翻譯中常見的佔位符與格式碼損壞。

    在翻譯寫入時與快取載入時都會用到，用來自動修：
    `% s`、`§ a`、`] (`、括號內被插空白的 `%s` 等。
    """
    if not isinstance(text, str):
        return text
    text = re.sub(r'％((?:\d+\$)?[a-zA-Z])', r'%\1', text)
    text = re.sub(r'%\s*(\d+)\s*\$\s*([a-zA-Z])', r'%\1$\2', text)
    text = re.sub(r'%\s+([a-zA-Z])', r'%\1', text)
    text = re.sub(r'%\s*\.\s*(\d+)\s*([fd])', r'%.\1\2', text)
    text = re.sub(r'\\\s+n', r'\\n', text)
    # § / & 色碼：修「符號與代碼之間」的空白
    text = re.sub(r'([&§])\s+([0-9a-fk-or])', r'\1\2', text, flags=re.IGNORECASE)
    # 括號／引號內被插空白的 printf
    text = re.sub(r'\[\s+(%(?:\d+\$)?[a-zA-Z])\s+\]', r'[\1]', text)
    text = re.sub(r'\(\s+(%(?:\d+\$)?[a-zA-Z])\s+\)', r'(\1)', text)
    text = re.sub(r'"\s+(%(?:\d+\$)?[a-zA-Z])\s+"', r'"\1"', text)
    text = re.sub(r'\]\s+\(', '](', text)
    text = re.sub(r'!\s+\[', '![', text)
    text = repair_patchouli_macros(text)
    text = re.sub(r'\[\s+', r'[', text)
    text = re.sub(r'\s+\]', r']', text)
    return text


def repair_patchouli_macros(text):
    """修復翻譯過程弄壞的 Patchouli 巨集。"""
    if not isinstance(text, str):
        return text
    if '$' not in text and '~' not in text and '～' not in text:
        return text
    text = re.sub(r'\$\s*（', '$(', text)
    text = re.sub(r'\$\(([^()（）]{0,240})）', r'$(\1)', text)
    text = re.sub(r'\$\s*\(\s*\)', '$()', text)
    text = re.sub(r'\$\s+\(', '$(', text)
    text = re.sub(r'[|｜!！][~～]\s*[（(]\s*[）)]', '$()', text)
    return text
