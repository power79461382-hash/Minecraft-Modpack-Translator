import json
import threading
import zipfile

import pytest

import translation_packager as packager
from core.analysis_scan import scan_single_jar


@pytest.mark.parametrize(
    "path",
    [
        "assets/example/lang/en_us.json",
        "data/example/advancements/root.json",
        "pack.mcmeta",
        "assets/example/",
    ],
)
def test_safe_archive_path_accepts_canonical_posix_members(path):
    assert packager.is_safe_archive_path(path)


@pytest.mark.parametrize(
    "path",
    [
        None,
        b"assets/example/lang/en_us.json",
        42,
        "",
        "assets/example/\x00lang/en_us.json",
        "/assets/example/lang/en_us.json",
        "//server/share/file.json",
        "C:/assets/example/lang/en_us.json",
        "C:assets/example/lang/en_us.json",
        r"assets\example\lang\en_us.json",
        r"assets\..\escape\lang\en_us.json",
        "assets//example/lang/en_us.json",
        "assets/./example/lang/en_us.json",
        "assets/../escape/lang/en_us.json",
        "assets/example//",
        ".",
        "..",
    ],
)
def test_safe_archive_path_rejects_noncanonical_or_unsafe_members(path):
    assert not packager.is_safe_archive_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "assets/../../escape/lang/en_us.json",
        "/assets/example/lang/en_us.json",
        "C:/assets/example/lang/en_us.json",
        r"assets\..\escape\lang\en_us.json",
    ],
)
def test_translated_lang_path_rejects_unsafe_members(path):
    assert packager.translated_lang_path(path) is None


class ScanApp:
    _LANG_FALLBACK_ORDER = ["en_us.json", "zh_cn.json"]

    def __init__(self):
        self._scan_process_mode = "force"
        self._scan_scope_mod_lang = True
        self._scan_scope_books = False
        self._scan_scope_quests = False
        self._scan_class_tooltip_patch = False
        self._scan_output_mode = "resource_pack"
        self._server_mode = False
        self._jar_lock = threading.Lock()
        self.analyzed_jars = {}
        self.analyzed_jars_zh_base = {}
        self.analyzed_book_texts = {}
        self.analyzed_book_text_repairs = {}
        self.analyzed_class_texts = {}

    @staticmethod
    def set_current_item(*_args):
        return None

    @staticmethod
    def log(*_args):
        return None

    @staticmethod
    def safe_decode_bytes(value):
        return value.decode("utf-8")

    @staticmethod
    def _clean_json_text(value):
        return value

    @staticmethod
    def _lang_value_needs_update(_source, _target):
        return False

    @staticmethod
    def _is_jar_lang_path(path):
        return (
            path.startswith("assets/")
            and "/lang/" in path
            and path.endswith((".json", ".lang"))
        )

    @classmethod
    def _pick_lang_file(cls, lang_dir, all_lower_to_orig):
        for candidate in cls._LANG_FALLBACK_ORDER:
            key = lang_dir + candidate
            if key in all_lower_to_orig:
                return all_lower_to_orig[key], candidate.rsplit(".", 1)[0]
        return None, None


def test_scan_single_jar_ignores_unsafe_members_before_classification(tmp_path):
    jar_path = tmp_path / "paths.jar"
    safe_path = "assets/safe/lang/en_us.json"
    unsafe_paths = [
        "assets/../../escape/lang/en_us.json",
        "/assets/absolute/lang/en_us.json",
        "C:/assets/drive/lang/en_us.json",
        r"assets\..\escape\lang\en_us.json",
    ]
    with zipfile.ZipFile(jar_path, "w") as archive:
        archive.writestr(safe_path, json.dumps({"safe.key": "Safe text"}))
        for index, path in enumerate(unsafe_paths):
            archive.writestr(path, json.dumps({f"unsafe.{index}": "Unsafe text"}))

    app = ScanApp()
    scan_single_jar(app, str(jar_path))

    assert app.analyzed_jars[str(jar_path)] == {
        safe_path: {"safe.key": "Safe text"},
    }
    for source_path in app.analyzed_jars[str(jar_path)]:
        translated_path = packager.translated_lang_path(source_path)
        assert translated_path is not None
        assert packager.is_safe_archive_path(translated_path)
