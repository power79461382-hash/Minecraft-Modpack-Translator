import json

import pytest

from core.analysis_scan import (
    detect_minecraft_pack_version,
    detect_minecraft_server_mode,
    run_analyze_task_impl,
)
from gui.main_window import ModTranslatorApp


PACK_FORMATS = {
    "1.12.2": {"rp": 3, "dp": 1},
    "1.16.5": {"rp": 6, "dp": 6},
    "1.18.2": {"rp": 8, "dp": 9},
    "1.19.2": {"rp": 9, "dp": 10},
    "1.20.1": {"rp": 15, "dp": 15},
    "1.21.1+": {"rp": 34, "dp": 48},
}


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class QueuedRoot:
    def __init__(self):
        self.callbacks = []

    def after(self, _delay, callback):
        self.callbacks.append(callback)

    def update(self):
        while self.callbacks:
            self.callbacks.pop(0)()


class Hint:
    def __init__(self):
        self.text = "unchanged"

    def config(self, **kwargs):
        self.text = kwargs["text"]


class ScanApp:
    MC_PACK_FORMATS = PACK_FORMATS
    _LANG_FALLBACK_ORDER = ["en_us.json", "zh_cn.json"]

    def __init__(self):
        self.process_mode_var = Value("append")
        self.output_mode_var = Value("hybrid")
        self.class_tooltip_patch_var = Value(False)
        self.scope_mod_lang_var = Value(False)
        self.scope_books_var = Value(False)
        self.scope_quests_var = Value(False)
        self.mc_version_var = Value("1.20.1")
        self.pack_format_var = Value(15)
        self.datapack_format_var = Value(15)
        self.pack_format_hint = Hint()
        self.stop_requested = False
        self.pause_requested = False
        self.root = QueuedRoot()
        self.is_processing = True
        self.btn_analyze = None
        self.btn_translate = None
        self.btn_stop = None
        self.btn_pause = None
        self.analyzed_jars = {}
        self.analyzed_jars_zh_base = {}
        self.analyzed_loose = []
        self.analyzed_loose_base = {}
        self.analyzed_extra = []
        self.analyzed_zip_json = []
        self.analyzed_book_texts = {}
        self.analyzed_book_text_repairs = {}
        self.analyzed_class_texts = {}
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    @staticmethod
    def set_current_item(*_args, **_kwargs):
        return None

    @staticmethod
    def _record_update_detection(_mod_dir):
        return None

    @staticmethod
    def _build_global_memory_pool(_mod_dir):
        return None

    @staticmethod
    def _count_analysis_strings():
        return None

    @staticmethod
    def _refresh_right_summary():
        return None

    @staticmethod
    def _set_btn_state(*_args):
        return None


def write_version_json(instance_dir, data):
    instance_dir.mkdir(parents=True)
    path = instance_dir / f"{instance_dir.name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_client_version_is_strongest_signal_for_custom_launcher_id(tmp_path):
    instance_dir = (
        tmp_path / ".minecraft" / "versions"
        / "Cisco's Fantasy Medieval RPG [Ultimate] (1)"
    )
    write_version_json(instance_dir, {
        "id": "Cisco's Fantasy Medieval RPG [Ultimate] (1)",
        "clientVersion": "1.19.2",
        "libraries": [
            {"name": "net.minecraftforge:fmlloader:1.20.1-47.3.0"},
        ],
    })

    result = detect_minecraft_pack_version(str(instance_dir), PACK_FORMATS)

    assert result == {
        "version": "1.19.2",
        "rp": 9,
        "dp": 10,
        "source": "version_json.clientVersion",
    }


def test_forge_fmlloader_detects_version_when_id_is_custom(tmp_path):
    instance_dir = tmp_path / ".minecraft" / "versions" / "Custom Pack"
    write_version_json(instance_dir, {
        "id": "Custom Pack",
        "libraries": [
            {"name": "net.minecraftforge:fmlloader:1.19.2-43.5.1"},
        ],
    })

    result = detect_minecraft_pack_version(str(instance_dir), PACK_FORMATS)

    assert result["version"] == "1.19.2"
    assert result["source"] == "version_json.library"


def test_nested_version_string_in_complete_json_is_supported(tmp_path):
    instance_dir = tmp_path / ".minecraft" / "versions" / "Nested"
    write_version_json(instance_dir, {
        "id": "Nested",
        "launcherMetadata": {"minecraftVersion": "1.18.2"},
    })

    result = detect_minecraft_pack_version(str(instance_dir), PACK_FORMATS)

    assert result["version"] == "1.18.2"
    assert result["source"] == "version_json.content"


def test_mod_filenames_use_unique_file_majority_vote(tmp_path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    for filename in (
        "aether-1.19.2-1.4.2-forge.jar",
        "apotheosis-1.19.2-6.5.2.jar",
        "quests-mc1.19.2-3.0.jar",
        "one-outlier-1.20.1.jar",
    ):
        (mods_dir / filename).touch()

    result = detect_minecraft_pack_version(str(tmp_path), PACK_FORMATS)

    assert result["version"] == "1.19.2"
    assert result["source"] == "mods.filename_majority"


def test_analysis_syncs_detected_formats_via_root_after(tmp_path):
    instance_dir = tmp_path / ".minecraft" / "versions" / "Cisco"
    write_version_json(instance_dir, {
        "id": "Cisco",
        "libraries": [
            {"name": "net.minecraftforge:fmlloader:1.19.2-43.5.1"},
        ],
    })
    app = ScanApp()

    run_analyze_task_impl(app, str(instance_dir))

    assert app._detected_mc_version == "1.19.2"
    assert app._detected_pack_format == 9
    assert app._detected_datapack_format == 10
    assert app.mc_version_var.get() == "1.20.1"
    assert app.pack_format_var.get() == 15

    app.root.update()

    assert app.mc_version_var.get() == "1.19.2"
    assert app.pack_format_var.get() == 9
    assert app.datapack_format_var.get() == 10
    assert "1.19.2" in app.pack_format_hint.text
    assert "Resource Pack：9" in app.pack_format_hint.text
    assert "Data Pack：10" in app.pack_format_hint.text


def test_unknown_version_blocks_instead_of_reusing_stale_selection(tmp_path):
    instance_dir = tmp_path / ".minecraft" / "versions" / "Unknown"
    write_version_json(instance_dir, {"id": "Unknown", "libraries": []})
    app = ScanApp()

    with pytest.raises(RuntimeError, match="無法自動判定 Minecraft 版本"):
        run_analyze_task_impl(app, str(instance_dir))
    app.root.update()

    assert app.mc_version_var.get() == "1.20.1"
    assert app.pack_format_var.get() == 15
    assert app.datapack_format_var.get() == 15
    assert "已停止分析" in app.pack_format_hint.text
    assert not hasattr(app, "_detected_mc_version")


def test_invalid_and_malicious_version_json_are_safe(tmp_path):
    invalid_dir = tmp_path / "versions" / "Invalid"
    invalid_dir.mkdir(parents=True)
    (invalid_dir / "Invalid.json").write_text("{not-json", encoding="utf-8")

    malicious_dir = tmp_path / "versions" / "Malicious"
    write_version_json(malicious_dir, {
        "id": "Malicious",
        "clientVersion": "../../1.19.2",
        "inheritsFrom": "../../1.20.1",
    })

    assert detect_minecraft_pack_version(
        str(invalid_dir), PACK_FORMATS) is None
    assert detect_minecraft_pack_version(
        str(malicious_dir), PACK_FORMATS) is None


def test_server_properties_alone_does_not_enable_server_jar_rewrites(tmp_path):
    server_dir = tmp_path / "mixed-client-pack"
    server_dir.mkdir()
    (server_dir / "server.properties").write_text("", encoding="utf-8")

    assert not detect_minecraft_server_mode(str(server_dir))


def test_server_jar_rewrites_require_explicit_opt_in(tmp_path):
    server_dir = tmp_path / "dedicated-server"
    server_dir.mkdir()
    (server_dir / "server.properties").write_text("", encoding="utf-8")

    assert detect_minecraft_server_mode(
        str(server_dir), explicit_opt_in=True)


def test_server_opt_in_dialog_timeout_fails_closed(monkeypatch):
    class DeferredRoot:
        def __init__(self):
            self.callback = None

        def after(self, _delay, callback):
            self.callback = callback

    class TimedOutEvent:
        def __init__(self):
            self.wait_timeout = None

        @staticmethod
        def set():
            return None

        def wait(self, timeout):
            self.wait_timeout = timeout
            return False

    root = DeferredRoot()
    event = TimedOutEvent()
    app = type("DialogApp", (), {"root": root})()
    monkeypatch.setattr(
        "gui.main_window.threading.Event", lambda: event)

    result = ModTranslatorApp._ask_proceed_from_thread(
        app, "Server mode", "Enable JAR rewrites?")

    assert result is False
    assert event.wait_timeout == 300
    assert root.callback is not None
