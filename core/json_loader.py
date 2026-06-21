import json
from typing import Any, Callable, Union


JsonInput = Union[str, bytes, bytearray]


def load_json_lenient(raw: JsonInput, clean_fn: Callable[[str], str], *,
                      encoding: str = "utf-8") -> Any:
    """容忍 BOM、註解與尾逗號的 JSON 載入。

    先用標準 ``json.loads`` 解析；失敗時才套用既有清理函式重試。
    這能把專案內重複的 JSON fallback 邏輯集中到單一位置。
    """
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode(encoding, errors="replace")
    else:
        text = str(raw)
    text = text.lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(clean_fn(text))

