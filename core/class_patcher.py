"""Java class 檔案常量池修補工具。

從 main_window.py 提取的獨立模組，負責：
- 解析 .class 常量池中的 Utf8 條目
- 判斷哪些常量是硬編碼的顯示文字（lore/tooltip）
- 將翻譯後的文字寫回 .class，使用 Modified UTF-8 編碼
"""
import re

# ── 預編譯正則 ──
_RE_LORE_CTRL    = re.compile(r'[\x00-\x1f]')
_RE_LORE_WORD4   = re.compile(r'[A-Za-z]{4,}')
_RE_LORE_WORD3   = re.compile(r'[A-Za-z]{3,}')
_RE_LORE_FMTCODE = re.compile(r'[§""][0-9a-fk-orx]')
_RE_LORE_NSREF   = re.compile(r'\b[a-z0-9_]+:[a-z0-9_/.]+')
_RE_LORE_WORDS   = re.compile(r"[A-Za-z']{2,}")


def has_cjk_text(text):
    return isinstance(text, str) and bool(re.search(r'[\u3400-\u9fff]', text))


def _read_u2(data, offset):
    return int.from_bytes(data[offset:offset + 2], 'big'), offset + 2


def _read_u4(data, offset):
    return int.from_bytes(data[offset:offset + 4], 'big'), offset + 4


def decode_mutf8(raw):
    """Decode Java class constant-pool Modified UTF-8 without leaking surrogates."""
    if not isinstance(raw, (bytes, bytearray)):
        return ""
    units = []
    i = 0
    size = len(raw)
    while i < size:
        b1 = raw[i]
        try:
            if b1 <= 0x7F:
                units.append(b1)
                i += 1
            elif (b1 & 0xE0) == 0xC0 and i + 1 < size:
                b2 = raw[i + 1]
                units.append(((b1 & 0x1F) << 6) | (b2 & 0x3F))
                i += 2
            elif (b1 & 0xF0) == 0xE0 and i + 2 < size:
                b2 = raw[i + 1]
                b3 = raw[i + 2]
                units.append(((b1 & 0x0F) << 12)
                             | ((b2 & 0x3F) << 6)
                             | (b3 & 0x3F))
                i += 3
            else:
                units.append(0xFFFD)
                i += 1
        except Exception:
            units.append(0xFFFD)
            i += 1
    chars = []
    i = 0
    while i < len(units):
        u = units[i]
        if 0xD800 <= u <= 0xDBFF and i + 1 < len(units):
            lo = units[i + 1]
            if 0xDC00 <= lo <= 0xDFFF:
                cp = 0x10000 + ((u - 0xD800) << 10) + (lo - 0xDC00)
                chars.append(chr(cp))
                i += 2
                continue
        if 0xD800 <= u <= 0xDFFF:
            chars.append("\uFFFD")
        else:
            chars.append(chr(u))
        i += 1
    return "".join(chars)


def class_utf8_entries(data):
    """Return class constant-pool Utf8 entries as dicts with byte offsets."""
    if not isinstance(data, (bytes, bytearray)) or data[:4] != b'\xca\xfe\xba\xbe':
        return []
    try:
        offset = 8
        cp_count, offset = _read_u2(data, offset)
        entries = []
        index = 1
        while index < cp_count:
            tag = data[offset]
            offset += 1
            if tag == 1:
                len_offset = offset
                size, offset = _read_u2(data, offset)
                bytes_offset = offset
                raw = bytes(data[bytes_offset:bytes_offset + size])
                offset += size
                text = decode_mutf8(raw)
                entries.append({
                    "len_offset": len_offset,
                    "bytes_offset": bytes_offset,
                    "size": size,
                    "text": text,
                })
            elif tag in (3, 4):
                offset += 4
            elif tag in (5, 6):
                offset += 8
                index += 1
            elif tag in (7, 8, 16, 19, 20):
                offset += 2
            elif tag in (9, 10, 11, 12, 17, 18):
                offset += 4
            elif tag == 15:
                offset += 3
            else:
                return []
            index += 1
        return entries
    except (IndexError, ValueError):
        return []


def is_hardcoded_lore_string(text):
    """判斷 class 常量池中的字串是否為硬編碼的顯示文字（tooltip/lore）。

    從 ModTranslatorApp._is_hardcoded_lore_string 提取，
    使用模組級正則而非類別屬性。
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if len(s) < 12 or len(s) > 260:
        return False
    if ' ' not in s:
        return False
    if any(x in s for x in ('\\', ';', ')V', '(L', '.java',
                            'net/', 'Lnet/', 'com/', 'org/',
                            '{', '}', '=', '<', '>')) or '://' in s:
        return False
    if '":' in s or '" :' in s:
        return False
    if _RE_LORE_CTRL.search(s):
        return False
    if not _RE_LORE_WORD4.search(s):
        return False
    lowered = s.lower()
    if lowered.startswith(('item.', 'block.', 'entity.', 'effect.', 'attribute.', 'key.')):
        return False
    if lowered in {
            'armor modifier',
            'armor toughness',
            'armor knockback resistance',
            'attack damage',
            'attack speed',
            'explosion resistance',
            'opening the jack in the box!',
    }:
        return False
    if lowered.startswith((
            'input block is missing',
            'result block is missing',
            'the event was not fired',
            'our container provider is missing',
            'itemlightsources only contains',
    )):
        return False
    if _RE_LORE_NSREF.search(s):
        return False
    if _RE_LORE_FMTCODE.search(s) and _RE_LORE_WORD3.search(s):
        return True
    markers = (
        '[full set bonus]', '[unique effect]', 'armor worn by',
        'legendary weapon', 'grants ', 'reduces ', 'boosts ',
        'inflicts ', 'damage ', 'health', 'armor', 'resistance',
        'chance ', 'when above', 'incoming damage', 'fatal damage',
        'crafting of powerful tools', 'powerful tools and equipment',
        'pristine coin', 'celebration of momentous events',
        '[on key press]', 'sets hp', 'invulnerability',
        'night vision', 'water breathing', 'jump boost',
        'nearby players', 'rewind time',
        '[right click ability]', '[shift right click]', 'on right click',
        'while held', 'when held', 'in main hand', 'in off hand',
    )
    if any(marker in lowered for marker in markers):
        return True
    if '/' in s:
        return False
    if lowered.startswith(('error', 'failed', 'unable', 'cannot',
                           'could not', 'couldn\'t', 'warning', 'exception',
                           'invalid', 'missing', 'unknown', 'unexpected',
                           'saved ', 'loading', 'loaded ', 'registered')):
        return False
    words = _RE_LORE_WORDS.findall(s)
    if len(words) >= 5 and s.rstrip().endswith(('.', '!', '?')):
        head = s.lstrip('§0123456789abcdefklmnorx[] ')
        return bool(head[:1].isupper())
    return False


def mutf8_encode(text):
    """Java class 常量池要求 Modified UTF-8（JVM 規範 §4.4.7）：
    U+0000 編成 0xC0 0x80；非 BMP 字元拆成 surrogate pair 各 3 bytes（共 6 bytes）。
    用標準 UTF-8 寫入 4-byte 序列會讓 JVM 拋 ClassFormatError → 模組拒載。"""
    out = bytearray()
    for ch in text:
        cp = ord(ch)
        if cp == 0:
            out += b'\xc0\x80'
        elif cp <= 0xFFFF:
            out += ch.encode('utf-8')
        else:
            cp -= 0x10000
            for sur in (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)):
                out += bytes([0xE0 | (sur >> 12),
                              0x80 | ((sur >> 6) & 0x3F),
                              0x80 | (sur & 0x3F)])
    return bytes(out)


def patch_class_hardcoded_strings(data, replacements):
    """將 .class 常量池中的硬編碼字串替換為翻譯。

    Args:
        data: .class 檔案的原始位元組
        replacements: {原文: 譯文} 字典

    Returns:
        (patched_bytes, changed_count)
    """
    entries = class_utf8_entries(data)
    if not entries or not replacements:
        return data, 0
    out = bytearray()
    cursor = 0
    changed = 0
    for entry in entries:
        text = entry["text"]
        translated = replacements.get(text)
        if not translated or translated == text:
            continue
        encoded = mutf8_encode(translated)
        if len(encoded) > 65535:
            continue
        len_offset = entry["len_offset"]
        bytes_offset = entry["bytes_offset"]
        end = bytes_offset + entry["size"]
        out.extend(data[cursor:len_offset])
        out.extend(len(encoded).to_bytes(2, 'big'))
        out.extend(encoded)
        cursor = end
        changed += 1
    if changed:
        out.extend(data[cursor:])
        return bytes(out), changed
    return data, 0
