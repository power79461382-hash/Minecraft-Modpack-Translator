import json

import pytest

from core.json_utils import clean_json_text
from translation_packager import merge_zh_base_fallback


def test_locale_fallback_preserves_primary_false_and_zero_values():
    fallback = {"show_progress": True, "sortnum": 5, "name": "Fallback title"}
    primary = {"show_progress": False, "sortnum": 0, "name": "Localized title"}

    assert merge_zh_base_fallback(fallback, primary) == {
        "show_progress": False, "sortnum": 0, "name": "Localized title"
    }


@pytest.mark.parametrize("content", ['{"count":1/* comment */2}', '{"value":tr/* comment */ue}'])
def test_json_comment_removal_does_not_join_distinct_tokens(content):
    with pytest.raises(json.JSONDecodeError):
        json.loads(clean_json_text(content))
