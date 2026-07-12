import os
import os
import re
import zipfile

import pytest

import core.jar_patcher as jar_patcher
from core.jar_patcher import atomic_zip_output_group
from core.translation_flow import run_translate_task


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class Root:
    def after(self, _delay, callback):
        callback()


class ResourcePackApp:
    C_SUCCESS = "success"
    C_WARN = "warn"
    C_ERROR = "error"
    C_DANGER = "danger"
    _RE_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")
    _RE_FORMAT = re.compile(r"(?!x)x")

    def __init__(self, instance_dir, failure_mode):
        self.analyzed_mc_dir = str(instance_dir)
        source_jar = instance_dir / "mods" / "example.jar"
        self.analyzed_jars = {
            str(source_jar): {
                "data/example/advancements/root.json": {
                    "display": {"title": "Root"},
                },
            },
        }
        self.analyzed_jars_zh_base = {}
        self.analyzed_loose = []
        self.analyzed_loose_base = {}
        self.analyzed_book_texts = {}
        self.analyzed_book_text_repairs = {}
        self.analyzed_extra = []
        self.analyzed_zip_json = []
        self.analyzed_class_texts = {}
        self._analysis_unique_strings = {"Root"}
        self._initial_cache = {"Root": "根節點"}
        self._server_mode = False
        self.failure_mode = failure_mode

        self.process_mode_var = Value("append")
        self.global_memory_var = Value(False)
        self.retry_count_var = Value(0)
        self.engine_var = Value("openai")
        self.datapack_name_var = Value("translations_Datapack.zip")
        self.datapack_output_var = Value(True)
        self.datapack_format_var = Value(10)
        self.scope_mod_lang_var = Value(False)
        self.strict_whitelist_var = Value(False)

        self.stop_requested = False
        self.pause_requested = False
        self.is_processing = True
        self.root = Root()
        self.btn_analyze = None
        self.btn_translate = None
        self.btn_stop = None
        self.btn_pause = None
        self.logs = []

    def load_cache(self):
        return dict(self._initial_cache)

    @staticmethod
    def save_cache(*_args, **_kwargs):
        return None

    def _cache_has_usable_translation(self, source):
        value = self.translation_cache.get(source)
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _normalize_engine_route_value(value):
        return value

    @staticmethod
    def should_translate(_source):
        return True

    @staticmethod
    def _review_and_fix_cache():
        return None

    @staticmethod
    def _verify_translations(_strings):
        return True

    @staticmethod
    def _scope_allows_analyzed_path(*_args):
        return True

    @staticmethod
    def _scope_allows_extra(*_args):
        return True

    @staticmethod
    def _load_official_minecraft_zh_base(*_args):
        return {}

    @staticmethod
    def _is_advancement_json_path(path):
        return "/advancements/" in path

    def _process_advancement_json(self, _data):
        if self.failure_mode == "cancel":
            self.stop_requested = True
            return {"display": {"title": "根節點"}}
        raise RuntimeError("injected resource-pack packaging failure")

    @staticmethod
    def _is_structured_book_json_path(_path):
        return False

    @staticmethod
    def _safe_zip_filename(name, default):
        candidate = name or default
        return candidate if candidate.lower().endswith(".zip") else candidate + ".zip"

    @staticmethod
    def _output_mode_summary():
        return "resource pack", ""

    @staticmethod
    def _maybe_install_resource_pack_to_instance(*_args):
        return None

    def log(self, message):
        self.logs.append(message)

    @staticmethod
    def set_current_item(*_args, **_kwargs):
        return None

    @staticmethod
    def update_progress(*_args, **_kwargs):
        return None

    @staticmethod
    def _set_summary_card(*_args, **_kwargs):
        return None

    @staticmethod
    def _refresh_api_summary(*_args, **_kwargs):
        return None

    @staticmethod
    def _set_btn_state(*_args, **_kwargs):
        return None

    @staticmethod
    def _note_progress_error():
        return None

    @staticmethod
    def _format_eta(seconds):
        return f"{seconds:.1f}s"


def write_sentinel_zip(path, marker):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("previous-output.txt", marker)
    return path.read_bytes()


def run_resource_pack_flow(tmp_path, failure_mode):
    instance_dir = tmp_path / "instance"
    (instance_dir / "mods").mkdir(parents=True)
    app = ResourcePackApp(instance_dir, failure_mode)
    run_translate_task(
        app,
        str(tmp_path),
        "translations.zip",
        9,
        output_mode="resource_pack",
    )
    return app


def test_cancellation_preserves_previous_resource_pack_and_datapack(tmp_path):
    pack_path = tmp_path / "translations.zip"
    datapack_path = tmp_path / "translations_Datapack.zip"
    previous_pack = write_sentinel_zip(pack_path, "old resource pack")
    previous_datapack = write_sentinel_zip(datapack_path, "old datapack")

    app = run_resource_pack_flow(tmp_path, "cancel")

    assert app.stop_requested
    assert pack_path.read_bytes() == previous_pack
    assert datapack_path.read_bytes() == previous_datapack
    assert not list(tmp_path.glob("*.tmp"))


def test_exception_preserves_previous_resource_pack_and_datapack(tmp_path):
    pack_path = tmp_path / "translations.zip"
    datapack_path = tmp_path / "translations_Datapack.zip"
    previous_pack = write_sentinel_zip(pack_path, "old resource pack")
    previous_datapack = write_sentinel_zip(datapack_path, "old datapack")

    app = run_resource_pack_flow(tmp_path, "exception")

    assert any("injected resource-pack packaging failure" in line for line in app.logs)
    assert pack_path.read_bytes() == previous_pack
    assert datapack_path.read_bytes() == previous_datapack
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_group_rollback_preserves_recovery_backup(monkeypatch, tmp_path):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    previous_first = write_sentinel_zip(first, "old first")
    previous_second = write_sentinel_zip(second, "old second")
    real_replace = os.replace
    real_copy = jar_patcher.shutil.copy2

    def injected_replace(source, destination):
        if str(source).endswith(".tmp") and os.path.abspath(destination) == str(second):
            raise OSError("injected second replace failure")
        if str(source).endswith(".bak") and os.path.abspath(destination) == str(first):
            raise OSError("injected rollback replace failure")
        return real_replace(source, destination)

    def injected_copy(source, destination, *args, **kwargs):
        if str(source).endswith(".bak") and os.path.abspath(destination) == str(first):
            raise OSError("injected rollback copy failure")
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(jar_patcher.os, "replace", injected_replace)
    monkeypatch.setattr(jar_patcher.shutil, "copy2", injected_copy)
    state = {}

    with pytest.raises(RuntimeError, match="回復不完整"):
        with atomic_zip_output_group(
                [str(first), str(second)], lambda: True,
                state=state) as outputs:
            outputs[str(first)].writestr("new.txt", "new first")
            outputs[str(second)].writestr("new.txt", "new second")

    backups = list(tmp_path.glob("*.bak"))
    assert second.read_bytes() == previous_second
    assert first.read_bytes() != previous_first
    assert len(backups) == 1
    assert backups[0].read_bytes() == previous_first
    assert state["recovery_backups"] == [str(backups[0])]


def test_second_output_replace_failure_rolls_back_both_outputs(
        tmp_path, monkeypatch):
    instance_dir = tmp_path / "instance"
    (instance_dir / "mods").mkdir(parents=True)
    app = ResourcePackApp(instance_dir, "success")
    app._process_advancement_json = lambda _data: {
        "display": {"title": "root"},
    }

    pack_path = tmp_path / "translations.zip"
    datapack_path = tmp_path / "translations_Datapack.zip"
    previous_pack = write_sentinel_zip(pack_path, "old resource pack")
    previous_datapack = write_sentinel_zip(datapack_path, "old datapack")
    real_replace = os.replace
    failure_injected = False

    def fail_datapack_replace(source, destination):
        nonlocal failure_injected
        if (not failure_injected
                and os.path.abspath(destination) == os.path.abspath(datapack_path)):
            failure_injected = True
            raise OSError("injected second output replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        "core.jar_patcher.os.replace", fail_datapack_replace)

    run_translate_task(
        app,
        str(tmp_path),
        "translations.zip",
        9,
        output_mode="resource_pack",
    )

    assert any(
        "injected second output replacement failure" in line
        for line in app.logs)
    assert failure_injected
    assert pack_path.read_bytes() == previous_pack
    assert datapack_path.read_bytes() == previous_datapack
    assert not [
        path for path in tmp_path.rglob("*")
        if path.suffix.lower() in {".tmp", ".bak"}
    ]
