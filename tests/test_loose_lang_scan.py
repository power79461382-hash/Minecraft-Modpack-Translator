import json
import zipfile

import pytest

from core.analysis_scan import run_analyze_task_impl


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class Root:
    def after(self, _delay, callback):
        callback()


class ScanApp:
    _LANG_FALLBACK_ORDER = ["en_us.json", "zh_cn.json"]

    def __init__(self, mode):
        self.process_mode_var = Value(mode)
        self.output_mode_var = Value("resource_pack")
        self.class_tooltip_patch_var = Value(False)
        self.scope_mod_lang_var = Value(True)
        self.scope_books_var = Value(False)
        self.scope_quests_var = Value(False)
        self.stop_requested = False
        self.pause_requested = False
        self.root = Root()
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

    @staticmethod
    def log(*_args):
        return None

    @staticmethod
    def set_current_item(*_args, **_kwargs):
        return None

    @staticmethod
    def safe_read_file(path):
        with open(path, "r", encoding="utf-8") as file:
            return file.read()

    @staticmethod
    def _clean_json_text(value):
        return value

    @staticmethod
    def _lang_value_needs_update(_source, _target):
        return False

    @staticmethod
    def _is_quest_path(_path):
        return False

    @staticmethod
    def _is_mmorpg_data_json_path(_path):
        return False

    @staticmethod
    def _is_openloader_resources_zip_path(_path):
        return False

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


def write_loose_lang_files(tmp_path, source_content, target_content):
    lang_dir = tmp_path / "assets" / "example" / "lang"
    lang_dir.mkdir(parents=True)
    source_path = lang_dir / "en_us.json"
    target_path = lang_dir / "zh_tw.json"
    source_path.write_text(source_content, encoding="utf-8")
    target_path.write_text(target_content, encoding="utf-8")
    return str(source_path)


@pytest.mark.parametrize("mode", ["append", "force"])
@pytest.mark.parametrize("target_content", ["", "  \n\t"])
def test_empty_existing_zh_tw_still_schedules_source_once(
        tmp_path, mode, target_content):
    source_path = write_loose_lang_files(
        tmp_path,
        json.dumps({"item.example": "Example Item"}),
        target_content,
    )
    app = ScanApp(mode)

    run_analyze_task_impl(app, str(tmp_path))

    assert app.analyzed_loose.count(source_path) == 1
    if mode == "append":
        assert app.analyzed_loose_base[source_path] == {}
    else:
        assert source_path not in app.analyzed_loose_base


def test_empty_source_is_not_scheduled(tmp_path):
    source_path = write_loose_lang_files(tmp_path, "  \n", "")
    app = ScanApp("append")

    run_analyze_task_impl(app, str(tmp_path))

    assert source_path not in app.analyzed_loose


def test_nonempty_partial_zh_tw_keeps_existing_base(tmp_path):
    source_path = write_loose_lang_files(
        tmp_path,
        json.dumps({"item.one": "One", "item.two": "Two"}),
        json.dumps({"item.one": "Translated One"}),
    )
    app = ScanApp("append")

    run_analyze_task_impl(app, str(tmp_path))

    assert app.analyzed_loose.count(source_path) == 1
    assert app.analyzed_loose_base[source_path] == {
        "item.one": "Translated One",
    }


def test_nonempty_complete_zh_tw_is_not_scheduled(tmp_path):
    source_path = write_loose_lang_files(
        tmp_path,
        json.dumps({"item.one": "One"}),
        json.dumps({"item.one": "Translated One"}),
    )
    app = ScanApp("append")

    run_analyze_task_impl(app, str(tmp_path))

    assert source_path not in app.analyzed_loose


def test_standalone_zip_scan_ignores_unsafe_origins_members(tmp_path):
    datapacks_dir = tmp_path / "datapacks"
    datapacks_dir.mkdir()
    zip_path = datapacks_dir / "paths.zip"
    safe_member = "data/example/origins/powers/safe.json"
    unsafe_member = "data/../../evil/origins/powers/pwn.json"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(safe_member, "{}")
        archive.writestr(unsafe_member, "{}")

    app = ScanApp("append")
    app.scope_books_var = Value(True)

    run_analyze_task_impl(app, str(tmp_path))

    assert app.analyzed_zip_json == [(str(zip_path), safe_member)]
