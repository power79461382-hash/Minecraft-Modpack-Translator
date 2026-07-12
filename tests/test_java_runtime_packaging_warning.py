import zipfile

import pytest

from core.jar_patcher import generate_jar_patches


class _Value:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _PackagingApp:
    SYNTHETIC_LANG_ZH_TW = {"translator.test": "測試"}
    ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
    C_SUCCESS = "success"
    C_WARN = "warn"

    def __init__(self, runtime_report):
        self._server_mode = False
        self._java_runtime_compatibility_report = runtime_report
        self.stop_requested = False
        self.logs = []
        self.analyzed_jars = {}
        self.analyzed_jars_zh_base = {}
        self.analyzed_loose = []
        self.analyzed_loose_base = {}
        self.analyzed_book_texts = {}
        self.analyzed_book_text_repairs = {}
        self.analyzed_class_texts = {}
        self.analyzed_extra = []
        self.analyzed_zip_json = []
        self.include_large_backups_var = _Value(False)
        self.scope_mod_lang_var = _Value(True)
        self.pack_format_var = _Value(9)
        self.datapack_format_var = _Value(10)

    def log(self, message):
        self.logs.append(message)

    def update_progress(self, *_args):
        pass

    def _scope_allows_analyzed_path(self, *_args):
        return True

    def _scope_allows_extra(self, *_args):
        return True

    def _has_openloader_resources(self, *_args):
        return False

    def _output_mode_summary(self):
        return "安全覆蓋套用", ""

    def _set_summary_card(self, *_args):
        pass

    def _refresh_api_summary(self):
        pass


def _generate_client_package(tmp_path, runtime_report):
    instance_dir = tmp_path / "instance"
    mods_dir = instance_dir / "mods"
    mods_dir.mkdir(parents=True)
    (mods_dir / "Paxi-1.19.2-Forge.jar").write_bytes(b"")

    app = _PackagingApp(runtime_report)
    output = generate_jar_patches(
        app, str(tmp_path), "client.zip", str(instance_dir))
    assert output
    return output


def test_incompatible_java_report_is_embedded_in_client_package(tmp_path):
    output = _generate_client_package(tmp_path, {
        "status": "incompatible",
        "is_incompatible": True,
        "minecraft_version": "1.19.2",
        "required_java_major": 17,
        "actual_java_major": 25,
    })

    with zipfile.ZipFile(output) as package:
        warning = package.read(
            "TRANSLATOR_RUNTIME_WARNING.txt").decode("utf-8-sig")

    assert "Java 版本不相容" in warning
    assert "Minecraft 1.19.2" in warning
    assert "Java 25" in warning
    assert "Java 17" in warning
    assert "不是翻譯包或 Paxi 錯誤" in warning
    assert "翻譯器不會修改 Java 或遊戲實例" in warning


@pytest.mark.parametrize("runtime_report", [None, {"status": "compatible"}])
def test_runtime_warning_is_omitted_without_incompatibility(
        tmp_path, runtime_report):
    output = _generate_client_package(tmp_path, runtime_report)

    with zipfile.ZipFile(output) as package:
        assert "TRANSLATOR_RUNTIME_WARNING.txt" not in package.namelist()
