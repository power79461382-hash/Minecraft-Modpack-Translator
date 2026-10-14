import base64
import json
import sys
from types import SimpleNamespace

import pytest

from core import config_store


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_windows_encrypts_without_optional_win32crypt(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32crypt", None)
    secret = "synthetic-key-for-local-roundtrip"
    encoded = config_store.obfuscate(secret)
    assert encoded.startswith("DPAPI:")
    assert config_store.deobfuscate(encoded) == secret


def test_windows_encryption_failure_never_downgrades_to_base64(monkeypatch):
    monkeypatch.setattr(config_store, "_is_windows", lambda: True)

    def fail(*_args, **_kwargs):
        raise OSError("synthetic DPAPI failure")

    monkeypatch.setitem(sys.modules, "win32crypt", SimpleNamespace(CryptProtectData=fail))
    # Both installed pywin32 and native Windows protection must fail closed.
    monkeypatch.setattr(config_store, "_native_dpapi", fail, raising=False)
    with pytest.raises(OSError) as error:
        config_store.obfuscate("synthetic-secret-do-not-print")
    assert "synthetic-secret-do-not-print" not in str(error.value)


def test_legacy_base64_still_decodes():
    encoded = base64.b64encode(b"synthetic-legacy-key").decode("ascii")
    assert config_store.deobfuscate(encoded) == "synthetic-legacy-key"


@pytest.mark.parametrize("encoded", ["not base64!", "DPAPI:invalid!", 17, None, {}, []])
def test_corrupt_secret_is_not_used_as_an_api_key(encoded):
    assert config_store.deobfuscate(encoded) == ""


def test_atomic_config_replace_failure_preserves_prior_document(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    target.write_text('{"prior": true}', encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(config_store.os, "replace", fail_replace)
    with pytest.raises(OSError):
        config_store.write_json_atomic(target, {"new": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"prior": True}
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_config_success_writes_complete_document(tmp_path):
    target = tmp_path / "config.json"
    config_store.write_json_atomic(target, {"setting": "value", "nested": [1, 2]})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "setting": "value", "nested": [1, 2]
    }
    assert list(tmp_path.iterdir()) == [target]
