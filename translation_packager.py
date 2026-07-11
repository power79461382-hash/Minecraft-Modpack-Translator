import json
import re

from core.json_loader import load_json_lenient


_RE_SURROGATE = re.compile(r'[\ud800-\udfff]')
_RE_FTBQ_LANG_SNBT = re.compile(r'/quests/lang/([a-z]{2,3}_[a-z]{2,3})\.snbt$',
                                re.IGNORECASE)
_RE_LOCALE_SEGMENT = re.compile(r'(^|/)([a-z]{2,3}_[a-z]{2,3})(?=/)',
                                re.IGNORECASE)

_LOCALIZED_MANUAL_MARKERS = (
    '/manual/', '/manuals/', '/guide/', '/guides/', '/guidebook/',
    '/guidebooks/', '/lexicon/', '/research/', '/researches/',
    '/ae2guide/', '/journal/',
)
_LOCALIZED_MANUAL_EXTENSIONS = ('.json', '.txt')


def is_safe_archive_path(path):
    """Return whether path is a canonical relative POSIX archive member."""
    if not isinstance(path, str) or not path or '\x00' in path or '\\' in path:
        return False
    if path.startswith('/') or re.match(r'^[A-Za-z]:', path):
        return False

    member_path = path[:-1] if path.endswith('/') else path
    if not member_path:
        return False
    return all(segment not in ('', '.', '..')
               for segment in member_path.split('/'))


def sanitize_text(text):
    """Remove Unicode surrogate code points that cannot be encoded as UTF-8."""
    if not isinstance(text, str):
        return text
    if not _RE_SURROGATE.search(text):
        return text
    return _RE_SURROGATE.sub('', text)


def sanitize_value(value):
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_text(k) if isinstance(k, str) else k: sanitize_value(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_value(v) for v in value)
    return value


def safe_utf8_bytes(text):
    return sanitize_text(str(text)).encode('utf-8')


def ftbq_lang_snbt_role(rel_path):
    """判斷 FTB Quests lang snbt 的角色（lang-based 任務包專用）。

    回傳 (是否為 lang snbt, 語言碼, zh_tw 輸出路徑)。
    - quests/lang/en_us.snbt 之類 → 這是「語言檔模式」的任務文字來源，
      翻譯必須輸出到同目錄的 zh_tw.snbt，**絕不就地覆寫來源語言檔**。
    - 非 lang snbt（chapters/、data.snbt 等內嵌式）→ (False, None, None)。
    """
    norm = rel_path.replace('\\', '/')
    m = _RE_FTBQ_LANG_SNBT.search(norm)
    if not m:
        return False, None, None
    lang = m.group(1).lower()
    zh_path = norm.rsplit('/', 1)[0] + '/zh_tw.snbt'
    return True, lang, zh_path


def locale_segment(path):
    """Return a directory locale segment such as en_us/zh_cn from a resource path."""
    norm = (path or '').replace('\\', '/')
    match = _RE_LOCALE_SEGMENT.search(norm)
    return match.group(2).lower() if match else None


def replace_locale_segment(path, target_locale='zh_tw'):
    norm = (path or '').replace('\\', '/')
    return _RE_LOCALE_SEGMENT.sub(
        lambda m: m.group(1) + target_locale,
        norm,
        count=1)


def manual_source_priority(path):
    """Return priority for localized manual sources sharing the same target.

    Lower numbers are better. English is preferred because most machine
    translation engines are tuned for en -> zh_tw; zh_cn is still useful when
    no English source exists, but should not compete with en_us for the same
    output path.
    """
    lang = locale_segment(path)
    if not lang:
        return 0
    if lang == 'en_us':
        return 0
    if lang.startswith('en_'):
        return 1
    if lang == 'zh_cn':
        return 2
    if lang == 'zh_tw':
        return 99
    return 50


def manual_source_group_key(path):
    """Return a stable grouping key for localized manual variants.

    assets/foo/book/en_us/page.txt and assets/foo/book/zh_cn/page.txt both
    produce assets/foo/book/zh_tw/page.txt, so only the best source should be
    scanned for that group.
    """
    lang = locale_segment(path)
    if not lang:
        return None
    return replace_locale_segment(path, '__locale__').lower()


def is_preferred_manual_source(path):
    """Whether a book/manual path is a safe source for zh_tw generation.

    Prefer English or Simplified Chinese sources. Other locale directories in
    real modpacks are often incomplete or even malformed, so translating them
    into zh_tw creates noisy output and parse warnings instead of coverage.
    Paths without an explicit locale are structural book JSON and are safe.
    """
    lang = locale_segment(path)
    if not lang:
        return True
    return lang.startswith('en_') or lang == 'zh_cn'


def is_localized_manual_resource(path):
    """Whether a resource path is a localized in-game manual page.

    Some mods (Immersive Engineering, Engineered Schematics, guidebook-style
    mods) store real page text under assets/<mod>/manual/en_us/*.txt instead
    of Patchouli's patchouli_books layout. Only localized directories are
    treated as translatable here; unlocalized manual/*.json files are usually
    recipe/layout metadata and must stay untouched.
    """
    path_norm = (path or '').replace('\\', '/')
    lower = path_norm.lower()
    return (
        path_norm.lower().startswith('assets/')
        and locale_segment(path_norm) is not None
        and lower.endswith(_LOCALIZED_MANUAL_EXTENSIONS)
        and any(marker in lower for marker in _LOCALIZED_MANUAL_MARKERS)
    )


def translated_lang_path(path_in_jar):
    """Return the zh_tw output path for a jar language/manual path, or None."""
    if not is_safe_archive_path(path_in_jar):
        return None
    path = path_in_jar
    lower = path.lower()
    if '/patchouli_books/' in lower:
        localized = replace_locale_segment(path, 'zh_tw')
        if localized != path:
            return localized
        if lower.endswith('/book.json'):
            return path
    if '/book/' in lower:
        localized = replace_locale_segment(path, 'zh_tw')
        if localized != path:
            return localized
        if lower.endswith(('.json', '.txt')):
            return path
    if is_localized_manual_resource(path):
        localized = replace_locale_segment(path, 'zh_tw')
        if localized != path:
            return localized
    if '/advancements/' in lower and lower.endswith('.json'):
        return path
    if '/lang/' in lower:
        suffix = '.lang' if lower.endswith('.lang') else '.json'
        return path.rsplit('/', 1)[0] + '/zh_tw' + suffix
    return None


def translated_fallback_paths(path_in_jar):
    """Return source-locale paths that should also receive translated book data.

    Patchouli and several in-game manuals load en_us as a fallback even when a
    zh_tw file exists. Writing the translated payload to the original en_us book
    path inside our overlay keeps those fallbacks from replacing translated
    content with English.
    """
    path = (path_in_jar or "").replace('\\', '/')
    lower = path.lower()
    lang = locale_segment(path)
    if not (lang and lang.startswith('en_')):
        return ()
    is_patchouli_json = '/patchouli_books/' in lower and lower.endswith('.json')
    is_book_file = '/book/' in lower and lower.endswith(('.json', '.txt'))
    if is_patchouli_json or is_book_file or is_localized_manual_resource(path):
        return (path,)
    return ()


def translated_repair_fallback_paths(path_in_jar):
    """Return fallback source-locale paths for repaired zh_tw book/manual data.

    Some manual renderers ignore or only partially honor zh_tw resources and
    continue reading en_us. When we only reflow/repair an existing zh_tw or
    zh_cn-derived page, mirror the fixed Traditional Chinese payload to en_us
    in the overlay as well so the in-game fallback path cannot show English.
    """
    path = (path_in_jar or "").replace('\\', '/')
    lower = path.lower()
    lang = locale_segment(path)
    if lang != 'zh_tw':
        return ()
    is_patchouli_json = '/patchouli_books/' in lower and lower.endswith('.json')
    is_book_file = '/book/' in lower and lower.endswith(('.json', '.txt'))
    if is_patchouli_json or is_book_file or is_localized_manual_resource(path):
        fallback = replace_locale_segment(path, 'en_us')
        if fallback != path:
            return (fallback,)
    return ()


def loose_zh_tw_path(rel_path):
    suffix = '.lang' if rel_path.lower().endswith('.lang') else '.json'
    return rel_path.rsplit('/', 1)[0] + '/zh_tw' + suffix


def defaultconfigs_mirror_path(rel_path):
    if rel_path.startswith('defaultconfigs/'):
        return 'config/' + rel_path[len('defaultconfigs/'):]
    return None


def json_bytes(data):
    return json.dumps(sanitize_value(data), ensure_ascii=False, indent=4).encode('utf-8')


def parse_legacy_lang_content(content):
    data = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        data[key.strip()] = value
    return data


def legacy_lang_bytes(data):
    # 譯文若含真實換行會把 entry 斷成兩行（後半行無 = 被遊戲丟棄），須轉成字面 \n
    lines = [
        "{}={}".format(
            sanitize_text(str(key)),
            sanitize_text(str(value)).replace('\r', '').replace('\n', '\\n'))
        for key, value in data.items()
    ]
    return safe_utf8_bytes("\n".join(lines) + ("\n" if lines else ""))


def load_json_content(content, clean_json_text):
    return load_json_lenient(content, clean_json_text)


def load_lang_content(content, source_path, clean_json_text):
    if str(source_path).lower().endswith('.lang'):
        return parse_legacy_lang_content(content)
    return load_json_content(content, clean_json_text)


def lang_bytes(source_path, data):
    if str(source_path).lower().endswith('.lang'):
        return legacy_lang_bytes(data)
    return json_bytes(data)


def merge_with_existing_zh(source_data, zh_base, process_json_data, to_traditional,
                           value_needs_update=None):
    if not zh_base:
        translated_data = process_json_data(source_data)
        translated_data, _ = drop_untranslated_lang_entries(source_data, translated_data)
        return translated_data

    missing = {k: v for k, v in source_data.items()
               if isinstance(v, str) and (
                   k not in zh_base
                   or not isinstance(zh_base[k], str)
                   or not zh_base[k].strip()
                   or zh_base[k] == v
                   or zh_base[k] == k
                   or (value_needs_update is not None
                       and value_needs_update(v, zh_base[k]))
               )}
    translated_missing = process_json_data(missing)
    translated_missing, _ = drop_untranslated_lang_entries(missing, translated_missing)
    zh_base_trad = convert_existing_zh_base(zh_base, to_traditional)
    return {**zh_base_trad, **translated_missing}


def merge_jar_lang_data(lang_data, zh_base, process_json_data, to_traditional):
    translated_data = process_json_data(lang_data)
    translated_data, _ = drop_untranslated_lang_entries(lang_data, translated_data)
    if not zh_base:
        return translated_data
    zh_base_trad = convert_existing_zh_base(zh_base, to_traditional)
    return {**zh_base_trad, **translated_data}


def merge_zh_base_fallback(fallback_base, primary_base):
    """Use zh_cn as a fallback for incomplete zh_tw resources.

    Some mods ship a placeholder zh_tw file with only one or two keys while a
    much more complete zh_cn file exists. Treating that partial zh_tw as the
    whole base makes the generated zh_tw drop many visible strings when an
    engine returns a pass-through value. This merge keeps primary zh_tw values,
    but fills missing nested dict/list entries from zh_cn before Traditional
    conversion happens later in the packaging flow.
    """
    if not primary_base:
        return fallback_base
    if not fallback_base:
        return primary_base
    if isinstance(fallback_base, dict) and isinstance(primary_base, dict):
        merged = dict(fallback_base)
        for key, value in primary_base.items():
            merged[key] = merge_zh_base_fallback(fallback_base.get(key), value)
        return merged
    if isinstance(fallback_base, list) and isinstance(primary_base, list):
        merged = list(fallback_base)
        for idx, value in enumerate(primary_base):
            if idx < len(merged):
                merged[idx] = merge_zh_base_fallback(merged[idx], value)
            else:
                merged.append(value)
        return merged
    return primary_base


def _value_needs_update(source, existing, value_needs_update=None):
    if not isinstance(source, str):
        return False
    if not isinstance(existing, str):
        return True
    if not existing.strip() or existing == source or existing == source.strip():
        return True
    if existing == source.split(':')[-1].strip():
        return True
    if value_needs_update is not None and value_needs_update(source, existing):
        return True
    return False


def structured_json_needs_update(source_data, zh_base, value_needs_update=None):
    """Return whether an existing structured zh JSON is missing visible text.

    This is intentionally structural rather than key-whitelist based. Book JSON
    formats vary by mod, but gameplay IDs usually survive the value update
    predicate because they look like namespaces/file paths instead of prose.
    """
    if not zh_base:
        return True
    if isinstance(source_data, dict):
        if not isinstance(zh_base, dict):
            return True
        for key, source_value in source_data.items():
            if key not in zh_base:
                return True
            if structured_json_needs_update(
                    source_value, zh_base.get(key), value_needs_update):
                return True
        return False
    if isinstance(source_data, list):
        if not isinstance(zh_base, list) or len(zh_base) < len(source_data):
            return True
        return any(
            structured_json_needs_update(src, zh_base[idx], value_needs_update)
            for idx, src in enumerate(source_data)
        )
    if isinstance(source_data, str):
        return _value_needs_update(source_data, zh_base, value_needs_update)
    return False


def merge_structured_json_with_existing_zh(source_data, zh_base, process_json_data,
                                           to_traditional,
                                           value_needs_update=None):
    """Merge translated structured JSON with an existing zh base recursively.

    Flat lang files can drop untranslated entries, but book/advancement JSON
    must keep their schema. This helper keeps valid existing Traditional Chinese
    strings, translates missing or mixed English strings from the source, and
    preserves technical fields through the caller's strict JSON translator.
    """
    translated_data = process_json_data(source_data)
    if not zh_base:
        return translated_data
    zh_base_trad = convert_existing_zh_base(zh_base, to_traditional)

    def merge_node(source, existing, translated):
        if isinstance(source, dict):
            if not isinstance(translated, dict):
                return translated
            if not isinstance(existing, dict):
                existing = {}
            merged = dict(existing)
            for key, source_value in source.items():
                merged[key] = merge_node(
                    source_value,
                    existing.get(key),
                    translated.get(key) if isinstance(translated, dict) else None,
                )
            return merged
        if isinstance(source, list):
            if not isinstance(translated, list):
                return translated
            existing_list = existing if isinstance(existing, list) else []
            merged = []
            for idx, source_value in enumerate(source):
                existing_value = existing_list[idx] if idx < len(existing_list) else None
                translated_value = translated[idx] if idx < len(translated) else None
                merged.append(merge_node(source_value, existing_value, translated_value))
            merged.extend(existing_list[len(source):])
            return merged
        if isinstance(source, str):
            if isinstance(existing, str) and not _value_needs_update(
                    source, existing, value_needs_update):
                return existing
            return translated if translated is not None else source
        return translated if translated is not None else source

    return merge_node(source_data, zh_base_trad, translated_data)


def convert_existing_zh_base(value, to_traditional):
    """Convert existing zh_cn/zh_tw bases recursively before merging."""
    if isinstance(value, str):
        return to_traditional(value)
    if isinstance(value, dict):
        return {k: convert_existing_zh_base(v, to_traditional)
                for k, v in value.items()}
    if isinstance(value, list):
        return [convert_existing_zh_base(v, to_traditional) for v in value]
    return value


def drop_untranslated_lang_entries(source_data, output_data):
    """Remove flat language entries that still equal their source text."""
    if not isinstance(source_data, dict) or not isinstance(output_data, dict):
        return output_data, 0

    cleaned = {}
    dropped = 0
    for key, value in output_data.items():
        source_value = source_data.get(key)
        if isinstance(source_value, str) and isinstance(value, str):
            if not value.strip() or value == source_value:
                dropped += 1
                continue
        cleaned[key] = value
    return cleaned, dropped


def pack_mcmeta(pack_format, description="§a自動翻譯模組繁體中文包"):
    return {
        "pack": {
            "pack_format": pack_format,
            "description": description,
        }
    }
