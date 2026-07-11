from translation_packager import merge_structured_json_with_existing_zh


def test_append_merge_preserves_existing_localized_list_tail():
    source = {
        "pages": [
            {"text": "English source page"},
        ],
    }
    existing = {
        "pages": [
            {"text": "既有第一頁"},
            {"text": "本地化額外頁"},
        ],
    }

    merged = merge_structured_json_with_existing_zh(
        source,
        existing,
        lambda _data: {"pages": [{"text": "更新後第一頁"}]},
        lambda value: value,
        value_needs_update=lambda _source, _existing: True,
    )

    assert merged["pages"] == [
        {"text": "更新後第一頁"},
        {"text": "本地化額外頁"},
    ]
