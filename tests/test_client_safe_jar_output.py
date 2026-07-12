import io
import zipfile

from core.analysis_scan import scan_single_jar
from core.jar_patcher import (
    find_packaged_mod_jars,
    translation_package_path,
)


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _ScanApp:
    def __init__(self, output_mode):
        self._scan_process_mode = "append"
        self._scan_scope_mod_lang = True
        self._scan_scope_books = False
        self._scan_scope_quests = False
        self._scan_class_tooltip_patch = True
        self._scan_output_mode = output_mode
        self._server_mode = False
        self._jar_lock = _Lock()
        self.analyzed_jars = {}
        self.analyzed_jars_zh_base = {}
        self.analyzed_book_texts = {}
        self.analyzed_book_text_repairs = {}
        self.analyzed_class_texts = {}
        self.logs = []

    def set_current_item(self, *_args):
        pass

    def log(self, message):
        self.logs.append(message)

    def safe_decode_bytes(self, payload):
        return payload.decode("utf-8")

    def _clean_json_text(self, text):
        return text

    def _lang_value_needs_update(self, *_args):
        return False

    def _is_jar_lang_path(self, _path):
        return False

    def _class_utf8_entries(self, _payload):
        return [{"text": "Hardcoded tooltip"}]

    def _is_hardcoded_lore_string(self, _text):
        return True


def test_translation_package_suffix_is_idempotent(tmp_path):
    expected = tmp_path / "1_安全版_模組語言包.zip"

    assert translation_package_path(
        str(tmp_path), "1_安全版.zip") == str(expected)
    assert translation_package_path(
        str(tmp_path), "1_安全版_模組語言包.zip") == str(expected)


def test_hybrid_scan_collects_opted_in_low_risk_class_rewrites(tmp_path):
    jar_path = tmp_path / "example.jar"
    with zipfile.ZipFile(jar_path, "w") as jar:
        jar.writestr("example/item/Tooltip.class", b"class-bytes")

    app = _ScanApp("hybrid")
    scan_single_jar(app, str(jar_path))

    assert app.analyzed_class_texts == {
        str(jar_path): {
            "example/item/Tooltip.class": ["Hardcoded tooltip"],
        }
    }


def test_direct_scan_collects_opted_in_low_risk_class_rewrites(tmp_path):
    jar_path = tmp_path / "example.jar"
    with zipfile.ZipFile(jar_path, "w") as jar:
        jar.writestr("example/item/Tooltip.class", b"class-bytes")

    app = _ScanApp("jar_patch")
    scan_single_jar(app, str(jar_path))

    assert app.analyzed_class_texts == {
        str(jar_path): {
            "example/item/Tooltip.class": ["Hardcoded tooltip"],
        }
    }


def test_hybrid_scan_skips_class_rewrites_without_opt_in(tmp_path):
    jar_path = tmp_path / "example.jar"
    with zipfile.ZipFile(jar_path, "w") as jar:
        jar.writestr("example/item/Tooltip.class", b"class-bytes")

    app = _ScanApp("hybrid")
    app._scan_class_tooltip_patch = False
    scan_single_jar(app, str(jar_path))

    assert app.analyzed_class_texts == {}


def test_archive_safety_audit_rejects_every_top_level_mod_jar(tmp_path):
    executable = io.BytesIO()
    with zipfile.ZipFile(executable, "w") as jar:
        jar.writestr("example/Entrypoint.class", b"bytecode")
    resource_only = io.BytesIO()
    with zipfile.ZipFile(resource_only, "w") as jar:
        jar.writestr("assets/example/lang/zh_tw.json", "{}")

    output = tmp_path / "translation.zip"
    with zipfile.ZipFile(output, "w") as pack:
        pack.writestr("mods/executable.jar", executable.getvalue())
        pack.writestr("mods/resources.jar", resource_only.getvalue())
        pack.writestr("resourcepacks/selectable.jar", executable.getvalue())

    assert find_packaged_mod_jars(str(output)) == [
        "mods/executable.jar",
        "mods/resources.jar",
    ]


def test_archive_safety_audit_fails_closed_for_invalid_package(tmp_path):
    output = tmp_path / "broken.zip"
    output.write_bytes(b"not a zip")

    assert find_packaged_mod_jars(str(output)) == [
        "<invalid-translation-package>"
    ]
