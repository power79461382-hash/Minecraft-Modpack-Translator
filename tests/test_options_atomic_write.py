import os

from gui.main_window import ModTranslatorApp


def _app():
    return object.__new__(ModTranslatorApp)


def test_options_update_is_atomic_and_preserves_unknown_lines_and_newlines(
        tmp_path, monkeypatch):
    options_path = tmp_path / "options.txt"
    original = (
        b"music:0.5\r\n"
        b"resourcePacks:[\"vanilla\"]\r\n"
        b"custom:value:with:colons\r\n"
        b"lang:en_us\r\n"
        b"untouched:last"
    )
    options_path.write_bytes(original)
    pack_id = "file/1_\u6a21\u7d44\u8a9e\u8a00\u5305.zip"

    real_fsync = os.fsync
    real_replace = os.replace
    fsynced = []
    replace_args = []

    def tracked_fsync(fd):
        fsynced.append(fd)
        return real_fsync(fd)

    def tracked_replace(source, destination):
        replace_args.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr("gui.main_window.os.fsync", tracked_fsync)
    monkeypatch.setattr("gui.main_window.os.replace", tracked_replace)

    assert _app()._enable_pack_in_options(str(options_path), pack_id) is True

    expected = (
        "music:0.5\r\n"
        f'resourcePacks:["vanilla","mod_resources","{pack_id}"]\r\n'
        "custom:value:with:colons\r\n"
        "lang:zh_tw\r\n"
        "untouched:last"
    ).encode("utf-8")
    assert options_path.read_bytes() == expected
    assert (tmp_path / "options.txt.translator.bak").read_bytes() == original
    assert len(fsynced) >= 2
    assert len(replace_args) == 1
    source, destination = map(os.path.abspath, replace_args[0])
    assert os.path.dirname(source) == os.path.dirname(destination)
    assert destination == os.path.abspath(options_path)
    assert not list(tmp_path.glob("*.tmp"))

    assert _app()._enable_pack_in_options(str(options_path), pack_id) is False
    assert (tmp_path / "options.txt.translator.bak").read_bytes() == original


def test_options_update_does_not_overwrite_existing_backup(tmp_path):
    options_path = tmp_path / "options.txt"
    backup_path = tmp_path / "options.txt.translator.bak"
    options_path.write_text(
        'resourcePacks:["vanilla"]\nlang:en_us\n', encoding="utf-8")
    backup_path.write_bytes(b"existing backup")

    assert _app()._enable_pack_in_options(
        str(options_path), "file/translated.zip") is True
    assert backup_path.read_bytes() == b"existing backup"


def test_replace_failure_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    options_path = tmp_path / "options.txt"
    original = b'resourcePacks:["vanilla"]\r\nlang:en_us\r\n'
    options_path.write_bytes(original)

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr("gui.main_window.os.replace", fail_replace)

    assert _app()._enable_pack_in_options(
        str(options_path), "file/translated.zip") is False
    assert options_path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_temp_write_failure_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    options_path = tmp_path / "options.txt"
    backup_path = tmp_path / "options.txt.translator.bak"
    original = b'resourcePacks:["vanilla"]\nlang:en_us\n'
    options_path.write_bytes(original)
    backup_path.write_bytes(b"existing backup")

    def fail_fsync(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr("gui.main_window.os.fsync", fail_fsync)

    assert _app()._enable_pack_in_options(
        str(options_path), "file/translated.zip") is False
    assert options_path.read_bytes() == original
    assert backup_path.read_bytes() == b"existing backup"
    assert not list(tmp_path.glob("*.tmp"))
