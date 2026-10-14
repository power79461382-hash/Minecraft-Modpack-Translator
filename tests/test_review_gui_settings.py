import json
import os
import pickle
import sqlite3
from types import SimpleNamespace

import pytest

import core.config_store as config_store
import gui.main_window as main_window
from core.translation_flow import usable_cache_translation_keys
from gui.main_window import ModTranslatorApp


class Value:
    def get(self):
        return ""

    def set(self, value):
        pass


def test_gui_settings_replace_failure_preserves_previous_document(tmp_path, monkeypatch):
    app = object.__new__(ModTranslatorApp)
    for name in ModTranslatorApp.save_config.__code__.co_names:
        if name.endswith("_var"):
            setattr(app, name, Value())
    app.config_file = str(tmp_path / "app_config.json")
    previous = b'{"ai_provider":"Xiaomi MiMo","ai_model":"mimo-v2.6-flash"}'
    (tmp_path / "app_config.json").write_bytes(previous)
    logs = []
    app.log = logs.append

    def fail_replace(*args):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(config_store.os, "replace", fail_replace)

    app.save_config()

    assert (tmp_path / "app_config.json").read_bytes() == previous
    assert any("儲存失敗" in line for line in logs)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["app_config.json"]


@pytest.mark.parametrize("document", [[], None, "wrong-type", 42])
def test_nonobject_config_reports_failure_without_crashing(tmp_path, document):
    path = tmp_path / "app_config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    logs = []
    app = SimpleNamespace(config_file=str(path), log=logs.append, mod_dir_var=Value())

    ModTranslatorApp.load_config(app)

    assert any("讀取失敗" in line for line in logs)


@pytest.mark.parametrize("cached", ["Stone", "石頭 %s", 42])
def test_invalid_cache_does_not_hide_valid_memory_translation(cached):
    app = object.__new__(ModTranslatorApp)
    app.global_memory_var = SimpleNamespace(get=lambda: True)
    app.translation_memory = {"Stone": "石頭"}
    app.translation_cache = {"Stone": cached}

    assert app.get_translation("Stone") == "石頭"


def test_gui_memory_pickle_cannot_execute_serialized_callable(tmp_path):
    marker = tmp_path / "unwanted-side-effect"
    class Unsafe:
        def __reduce__(self):
            return os.mkdir, (str(marker),)
    path = tmp_path / "translation_memory_pool.json"
    path.with_suffix(".json.pkl").write_bytes(pickle.dumps({"Stone": Unsafe()}))
    app = object.__new__(ModTranslatorApp)
    app.global_memory_file = str(path)
    app.translation_memory = {}

    assert app._load_translation_memory() == {}
    assert not marker.exists()


def test_gui_translation_rejects_invented_format_codes():
    app = object.__new__(ModTranslatorApp)
    assert app.validate_translation("Stone", "§a石頭") == "Stone"


@pytest.mark.parametrize("source", [
    {"text": "Stone", "item": "minecraft:stone", "pages": ["Dirt", "Iron"]},
    ["Stone", {"text": "Dirt"}, "Iron"],
])
def test_stopped_json_translation_retains_all_unprocessed_structure(source):
    app = object.__new__(ModTranslatorApp)
    app.stop_requested = True
    assert app.process_json_data(source) == source


def test_cancel_during_json_translation_keeps_remaining_keys():
    app = object.__new__(ModTranslatorApp)
    app.stop_requested = False
    def translate_and_stop(source):
        app.stop_requested = True
        return "石頭"
    app.get_translation = translate_and_stop
    source = {"text": "Stone", "item": "minecraft:stone", "pages": ["Dirt", "Iron"]}
    assert app.process_json_data(source) == {
        "text": "石頭", "item": "minecraft:stone", "pages": ["Dirt", "Iron"]}


@pytest.mark.parametrize("memory", [{"Stone": "Stone"}, {}])
def test_analysis_counter_rejects_invalid_memory_and_closes_temporary_cache(tmp_path, monkeypatch, memory):
    app = object.__new__(ModTranslatorApp)
    app.set_current_item = lambda *args, **kwargs: None
    app.extract_all_unique_strings = lambda: {"Stone"}
    app._current_cache_file = lambda: str(tmp_path / "translation_cache.json")
    app.global_memory_var = SimpleNamespace(get=lambda: True)
    app._load_translation_memory = lambda: memory
    app.log = lambda *args: None
    loaded = []
    real_load = main_window.load_translation_cache
    def track_cache(*args, **kwargs):
        cache, messages = real_load(*args, **kwargs)
        loaded.append(cache)
        return cache, messages
    monkeypatch.setattr(main_window, "load_translation_cache", track_cache)

    assert app._count_analysis_strings() == (1, 0, 1)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        loaded[0]._db.execute("SELECT 1")


def test_overlong_cache_value_does_not_skip_translation_work():
    app = object.__new__(ModTranslatorApp)
    app.translation_cache = {"Stone": "石頭" * 20}
    app.global_memory_var = SimpleNamespace(get=lambda: False)
    assert app.get_translation("Stone") == "Stone"
    assert usable_cache_translation_keys(app, {"Stone"}) == set()
