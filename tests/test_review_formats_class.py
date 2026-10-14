import pytest

from core.class_patcher import class_utf8_entries, patch_class_hardcoded_strings


def class_header(count):
    return bytes.fromhex("cafebabe00000034") + count.to_bytes(2, "big")


@pytest.mark.parametrize("data", [
    class_header(2) + b"\x01\x00\x06Hello",
    class_header(3) + b"\x01\x00\x05Hello\x03",
])
def test_truncated_constant_pool_is_not_patched(data):
    assert class_utf8_entries(data) == []
    assert patch_class_hardcoded_strings(data, {"Hello": "Translated"}) == (data, 0)


def test_class_patch_preserves_nonconstant_body_bytes():
    data = class_header(2) + b"\x01\x00\x05Hello" + b"unchanged-body"
    patched, changed = patch_class_hardcoded_strings(data, {"Hello": "Translated"})

    assert changed == 1
    assert patched == class_header(2) + b"\x01\x00\x0aTranslated" + b"unchanged-body"
