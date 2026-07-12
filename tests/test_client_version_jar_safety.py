import io
import json
import os
import re
import zipfile

import pytest

from core.analysis_scan import (
    detect_minecraft_server_mode,
    detect_minecraft_pack_version,
    inspect_minecraft_language_assets,
    minecraft_language_asset_warning_lines,
    should_scan_translation_archive,
    sync_detected_pack_formats,
)
from core.jar_patcher import (
    generate_class_patch_jars,
    generate_jar_patches,
    rebuild_jar_with_inject,
)


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


@pytest.mark.parametrize(
    ("instance_name", "jar_stem", "write_version_json"),
    [
        ("Otherworld", "Otherworld", False),
        ("Otherworld", "1.20.1", True),
    ],
)
def test_client_scan_rejects_minecraft_version_root_jars(
        tmp_path, instance_name, jar_stem, write_version_json):
    instance_dir = tmp_path / instance_name
    instance_dir.mkdir()
    jar_path = instance_dir / f"{jar_stem}.jar"
    jar_path.write_bytes(b"")
    if write_version_json:
        (instance_dir / f"{jar_stem}.json").write_text("{}", encoding="utf-8")

    assert not should_scan_translation_archive(
        str(instance_dir), str(jar_path), server_mode=False)
    assert not should_scan_translation_archive(
        str(instance_dir), str(jar_path), server_mode=True)
    assert should_scan_translation_archive(
        str(instance_dir), str(jar_path), allow_root_jar=True)


def test_client_scan_still_allows_non_version_root_jar(tmp_path):
    instance_dir = tmp_path / "Otherworld"
    instance_dir.mkdir()
    jar_path = instance_dir / "custom-root-addon.jar"
    jar_path.write_bytes(b"")

    assert should_scan_translation_archive(
        str(instance_dir), str(jar_path), server_mode=False)


def test_client_version_directory_is_not_misdetected_as_server(tmp_path):
    instance_dir = tmp_path / ".minecraft" / "versions" / "Otherworld"
    instance_dir.mkdir(parents=True)
    (instance_dir / "Otherworld.json").write_text("{}", encoding="utf-8")
    (instance_dir / "server.properties").write_text("", encoding="utf-8")

    assert not detect_minecraft_server_mode(str(instance_dir))


def test_dedicated_server_directory_is_detected(tmp_path):
    server_dir = tmp_path / "dedicated-server"
    server_dir.mkdir()
    (server_dir / "server.properties").write_text("", encoding="utf-8")

    assert detect_minecraft_server_mode(
        str(server_dir), explicit_opt_in=True)


def test_detect_minecraft_version_prefers_client_version(tmp_path):
    instance_dir = tmp_path / "Cisco Custom Name"
    instance_dir.mkdir()
    (instance_dir / "Cisco Custom Name.json").write_text(
        json.dumps({
            "clientVersion": "1.19.2",
            "libraries": [{"name": "example:compat:1.20.1"}],
        }),
        encoding="utf-8",
    )

    detected = detect_minecraft_pack_version(str(instance_dir), {
        "1.19.2": {"rp": 9, "dp": 10},
        "1.20.1": {"rp": 15, "dp": 15},
    })

    assert detected["version"] == "1.19.2"
    assert (detected["rp"], detected["dp"]) == (9, 10)


def test_detect_minecraft_version_falls_back_to_forge_library(tmp_path):
    instance_dir = tmp_path / "Custom Pack"
    instance_dir.mkdir()
    (instance_dir / "Custom Pack.json").write_text(
        json.dumps({
            "id": "Custom Pack",
            "libraries": [
                {"name": "net.minecraftforge:fmlloader:1.19.2-43.5.1"},
            ],
        }),
        encoding="utf-8",
    )

    detected = detect_minecraft_pack_version(str(instance_dir), {
        "1.19.2": {"rp": 9, "dp": 10},
        "1.20.1": {"rp": 15, "dp": 15},
    })

    assert detected["version"] == "1.19.2"


def test_detect_minecraft_version_uses_mod_filename_majority(tmp_path):
    instance_dir = tmp_path / "Unnamed Pack"
    mods_dir = instance_dir / "mods"
    mods_dir.mkdir(parents=True)
    (mods_dir / "one-forge-1.19.2.jar").write_bytes(b"")
    (mods_dir / "two-mc1.19.2.jar").write_bytes(b"")
    (mods_dir / "compat-1.20.1.jar").write_bytes(b"")

    detected = detect_minecraft_pack_version(str(instance_dir), {
        "1.19.2": {"rp": 9, "dp": 10},
        "1.20.1": {"rp": 15, "dp": 15},
    })

    assert detected["version"] == "1.19.2"


def test_sync_detected_pack_formats_updates_ui_via_root_after(tmp_path):
    instance_dir = tmp_path / "Cisco"
    instance_dir.mkdir()
    (instance_dir / "Cisco.json").write_text(
        json.dumps({"clientVersion": "1.19.2"}), encoding="utf-8")

    class SettableValue:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class ImmediateRoot:
        @staticmethod
        def after(_delay, callback):
            callback()

    class App:
        MC_PACK_FORMATS = {
            "1.19.2": {"rp": 9, "dp": 10},
            "1.20.1": {"rp": 15, "dp": 15},
        }

        def __init__(self):
            self.root = ImmediateRoot()
            self.mc_version_var = SettableValue("1.20.1")
            self.pack_format_var = SettableValue(15)
            self.datapack_format_var = SettableValue(15)
            self.logs = []

        def log(self, message):
            self.logs.append(message)

    app = App()

    assert sync_detected_pack_formats(app, str(instance_dir)) == "1.19.2"
    assert app.mc_version_var.get() == "1.19.2"
    assert app.pack_format_var.get() == 9
    assert app.datapack_format_var.get() == 10
    assert app._detected_resource_pack_format == 9
    assert app._detected_data_pack_format == 10


def test_language_asset_inspection_reports_missing_objects_without_writes(tmp_path):
    minecraft_dir = tmp_path / ".minecraft"
    instance_dir = minecraft_dir / "versions" / "TestClient"
    index_dir = minecraft_dir / "assets" / "indexes"
    object_dir = minecraft_dir / "assets" / "objects"
    instance_dir.mkdir(parents=True)
    index_dir.mkdir(parents=True)
    object_dir.mkdir(parents=True)

    present_hash = "11" * 20
    missing_pack_hash = "22" * 20
    missing_zh_hash = "33" * 20
    (instance_dir / "TestClient.json").write_text(
        json.dumps({"assetIndex": {"id": "1.20-test"}}), encoding="utf-8")
    (index_dir / "1.20-test.json").write_text(
        json.dumps({
            "objects": {
                "pack.mcmeta": {"hash": missing_pack_hash, "size": 1},
                "minecraft/lang/en_us.json": {"hash": present_hash, "size": 1},
                "minecraft/lang/zh_tw.json": {"hash": missing_zh_hash, "size": 1},
                "minecraft/sounds.json": {"hash": "44" * 20, "size": 1},
            }
        }),
        encoding="utf-8",
    )
    present_path = object_dir / present_hash[:2] / present_hash
    present_path.parent.mkdir()
    present_path.write_bytes(b"{}")
    paths_before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    report = inspect_minecraft_language_assets(str(instance_dir))

    paths_after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert paths_after == paths_before
    assert report["status"] == "missing_objects"
    assert report["asset_index_id"] == "1.20-test"
    assert report["language_asset_count"] == 3
    assert {item["name"] for item in report["missing_objects"]} == {
        "pack.mcmeta",
        "minecraft/lang/zh_tw.json",
    }


def test_language_asset_inspection_follows_inherited_version_json(tmp_path):
    minecraft_dir = tmp_path / ".minecraft"
    instance_dir = minecraft_dir / "versions" / "ForgeClient"
    parent_dir = minecraft_dir / "versions" / "1.20.1"
    index_dir = minecraft_dir / "assets" / "indexes"
    instance_dir.mkdir(parents=True)
    parent_dir.mkdir(parents=True)
    index_dir.mkdir(parents=True)

    (instance_dir / "ForgeClient.json").write_text(
        json.dumps({"inheritsFrom": "1.20.1"}), encoding="utf-8")
    (parent_dir / "1.20.1.json").write_text(
        json.dumps({"assetIndex": {"id": "5"}}), encoding="utf-8")
    (index_dir / "5.json").write_text(
        json.dumps({
            "objects": {
                "pack.mcmeta": {"hash": "22" * 20, "size": 1},
            }
        }),
        encoding="utf-8",
    )

    report = inspect_minecraft_language_assets(str(instance_dir))

    assert report["status"] == "missing_objects"
    assert report["asset_index_id"] == "5"
    assert [item["name"] for item in report["missing_objects"]] == [
        "pack.mcmeta"
    ]


def test_language_asset_inspection_rejects_inheritance_cycle(tmp_path):
    minecraft_dir = tmp_path / ".minecraft"
    first_dir = minecraft_dir / "versions" / "First"
    second_dir = minecraft_dir / "versions" / "Second"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    (first_dir / "First.json").write_text(
        json.dumps({"inheritsFrom": "Second"}), encoding="utf-8")
    (second_dir / "Second.json").write_text(
        json.dumps({"inheritsFrom": "First"}), encoding="utf-8")

    report = inspect_minecraft_language_assets(str(first_dir))

    assert report["status"] == "invalid_version_json"
    assert "cycle" in report["error"].lower()


@pytest.mark.parametrize("payload", [[], None, "invalid-shape"])
def test_language_asset_inspection_rejects_non_mapping_version_json(
        tmp_path, payload):
    instance_dir = tmp_path / ".minecraft" / "versions" / "Broken"
    instance_dir.mkdir(parents=True)
    (instance_dir / "Broken.json").write_text(
        json.dumps(payload), encoding="utf-8")

    report = inspect_minecraft_language_assets(str(instance_dir))

    assert report["status"] == "invalid_version_json"
    assert report["error"]


def test_missing_asset_index_id_produces_actionable_warning():
    lines = minecraft_language_asset_warning_lines({
        "status": "missing_asset_index_id",
        "missing_objects": [],
    })

    assert lines
    assert "missing_asset_index_id" in "\n".join(lines)


class PackagingApp:
    SYNTHETIC_LANG_ZH_TW = {}
    ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
    _RE_JAR_SIG = re.compile(r"^META-INF/.*\.(?:SF|DSA|RSA|EC)$", re.I)
    C_SUCCESS = "success"
    C_WARN = "warn"

    def __init__(self, jar_path):
        source_path = "assets/minecraft/lang/en_us.json"
        self.stop_requested = False
        self.logs = []
        self._server_mode = False
        self._scan_class_tooltip_patch = True
        self._allow_root_jar = True
        self.analyzed_jars = {jar_path: {source_path: {"menu.quit": "Quit Game"}}}
        self.analyzed_jars_zh_base = {}
        self.analyzed_loose = []
        self.analyzed_loose_base = {}
        self.analyzed_book_texts = {}
        self.analyzed_book_text_repairs = {}
        self.analyzed_class_texts = {}
        self.analyzed_extra = []
        self.analyzed_zip_json = []
        self.process_mode_var = Value("append")
        self.include_large_backups_var = Value(False)
        self.scope_mod_lang_var = Value(False)
        self.pack_format_var = Value(9)

    def log(self, message):
        self.logs.append(message)

    def update_progress(self, *_args):
        pass

    def _set_summary_card(self, *_args):
        pass

    def _output_mode_summary(self):
        return "JAR 直接套用", ""

    def _refresh_api_summary(self):
        pass

    def _scope_allows_analyzed_path(self, *_args):
        return True

    def _scope_allows_extra(self, *_args):
        return True

    def _has_openloader_resources(self, *_args):
        return False

    def _jar_launch_risk_reasons(self, *_args):
        return ()

    def _jar_rewrite_is_high_risk(self, *_args):
        return False

    def _load_official_minecraft_zh_base(self, *_args):
        return None

    def _is_advancement_json_path(self, *_args):
        return False

    def _is_structured_book_json_path(self, *_args):
        return False

    def _to_traditional(self, value):
        return value

    def process_json_data(self, *_args, **_kwargs):
        return {"menu.quit": "離開遊戲"}

    def _filter_lang_output_entries(self, _source, output):
        return output, 0, 0

    def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
        return rebuild_jar_with_inject(
            jar_path, temp_jar, inject, self._RE_JAR_SIG)


def test_packager_omits_client_version_root_jar_defense_in_depth(tmp_path):
    instance_dir = tmp_path / "Otherworld"
    output_dir = tmp_path / "output"
    instance_dir.mkdir()
    output_dir.mkdir()
    jar_path = instance_dir / "Otherworld.jar"
    (instance_dir / "Otherworld.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(jar_path, "w") as archive:
        archive.writestr(
            "assets/minecraft/lang/en_us.json",
            json.dumps({"menu.quit": "Quit Game"}),
        )

    app = PackagingApp(str(jar_path))
    app._server_mode = True
    app._allow_root_jar = False
    output_path = generate_jar_patches(
        app, str(output_dir), "client.zip", str(instance_dir))

    assert output_path == ""
    assert not os.path.exists(output_dir / "client_模組語言包.zip")
    assert any("版本主 JAR" in line for line in app.logs)


def test_hybrid_class_packager_restores_version_root_jar_when_opted_in(
        tmp_path):
    instance_dir = tmp_path / "Otherworld"
    output_dir = tmp_path / "output"
    instance_dir.mkdir()
    output_dir.mkdir()
    jar_path = instance_dir / "Otherworld.jar"
    (instance_dir / "Otherworld.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(jar_path, "w") as archive:
        archive.writestr("example/item/Tooltip.class", b"original")

    app = PackagingApp(str(jar_path))
    app.analyzed_jars = {}
    app.analyzed_class_texts = {
        str(jar_path): {"example/item/Tooltip.class": ["Tooltip"]}
    }
    app._build_class_inject_for_jar = lambda *_args: {
        "example/item/Tooltip.class": b"patched"
    }

    generate_class_patch_jars(
        app, str(output_dir), "client.zip", str(instance_dir))

    patch_path = output_dir / "client_Class硬編碼補丁.zip"
    assert patch_path.exists()
    with zipfile.ZipFile(patch_path) as package:
        assert "Otherworld.jar" in package.namelist()
        assert "_backups/Otherworld.jar" in package.namelist()
        rebuilt = package.read("Otherworld.jar")
    with zipfile.ZipFile(io.BytesIO(rebuilt)) as jar:
        assert jar.read("example/item/Tooltip.class") == b"patched"


def test_hybrid_class_packager_restores_mod_jar_when_opted_in(tmp_path):
    instance_dir = tmp_path / "ClientPack"
    mods_dir = instance_dir / "mods"
    output_dir = tmp_path / "output"
    mods_dir.mkdir(parents=True)
    output_dir.mkdir()
    jar_path = mods_dir / "example.jar"
    with zipfile.ZipFile(jar_path, "w") as archive:
        archive.writestr("example/item/Tooltip.class", b"original")

    app = PackagingApp(str(jar_path))
    app.analyzed_jars = {}
    app.analyzed_class_texts = {
        str(jar_path): {"example/item/Tooltip.class": ["Tooltip"]}
    }
    app._build_class_inject_for_jar = lambda *_args: {
        "example/item/Tooltip.class": b"patched"
    }

    generate_class_patch_jars(
        app, str(output_dir), "client.zip", str(instance_dir))

    patch_path = output_dir / "client_Class硬編碼補丁.zip"
    assert patch_path.exists()
    with zipfile.ZipFile(patch_path) as package:
        assert "mods/example.jar" in package.namelist()
        assert "_backups/mods/example.jar" in package.namelist()
        rebuilt = package.read("mods/example.jar")
    with zipfile.ZipFile(io.BytesIO(rebuilt)) as jar:
        assert jar.read("example/item/Tooltip.class") == b"patched"


def test_client_class_packager_removes_stale_legacy_patch(tmp_path):
    instance_dir = tmp_path / "ClientPack"
    output_dir = tmp_path / "output"
    instance_dir.mkdir()
    output_dir.mkdir()
    stale_patch = output_dir / "client_Class硬編碼補丁.zip"
    stale_patch.write_bytes(b"legacy executable patch")

    app = PackagingApp(str(instance_dir / "unused.jar"))
    app.analyzed_jars = {}
    app.analyzed_class_texts = {}

    generate_class_patch_jars(
        app, str(output_dir), "client.zip", str(instance_dir))

    assert not stale_patch.exists()
    assert any("沒有偵測到" in line for line in app.logs)


def test_client_class_packager_fails_closed_if_stale_patch_cannot_be_removed(
        tmp_path, monkeypatch):
    instance_dir = tmp_path / "ClientPack"
    output_dir = tmp_path / "output"
    instance_dir.mkdir()
    output_dir.mkdir()
    stale_patch = output_dir / "client_Class硬編碼補丁.zip"
    stale_patch.write_bytes(b"legacy executable patch")

    app = PackagingApp(str(instance_dir / "unused.jar"))
    app.analyzed_jars = {}
    app.analyzed_class_texts = {}

    monkeypatch.setattr(
        "core.jar_patcher.os.remove",
        lambda _path: (_ for _ in ()).throw(OSError("locked")))

    with pytest.raises(RuntimeError, match="舊 class 補丁"):
        generate_class_patch_jars(
            app, str(output_dir), "client.zip", str(instance_dir))

    assert stale_patch.exists()


def _prepare_client_overlay_package(tmp_path):
    instance_dir = tmp_path / "ClientPack"
    mods_dir = instance_dir / "mods"
    output_dir = tmp_path / "output"
    mods_dir.mkdir(parents=True)
    output_dir.mkdir()
    jar_path = mods_dir / "example.jar"
    with zipfile.ZipFile(jar_path, "w") as archive:
        archive.writestr(
            "assets/minecraft/lang/en_us.json",
            json.dumps({"menu.quit": "Quit Game"}),
        )
    (mods_dir / "Paxi-1.19.2-Forge.jar").write_bytes(b"")
    return PackagingApp(str(jar_path)), instance_dir, output_dir


def _write_sentinel_package(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("previous-output.txt", "keep this package")
    return path.read_bytes()


def test_client_package_success_atomically_replaces_final_zip(
        tmp_path, monkeypatch):
    app, instance_dir, output_dir = _prepare_client_overlay_package(tmp_path)
    final_path = output_dir / "client_模組語言包.zip"
    previous_bytes = _write_sentinel_package(final_path)
    replace_calls = []
    real_replace = os.replace

    def tracked_replace(source, destination):
        replace_calls.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr("core.jar_patcher.os.replace", tracked_replace)

    output = generate_jar_patches(
        app, str(output_dir), "client.zip", str(instance_dir))

    assert output == str(final_path)
    assert final_path.read_bytes() != previous_bytes
    assert len(replace_calls) == 1
    source, destination = map(os.path.abspath, replace_calls[0])
    assert source != destination
    assert os.path.dirname(source) == os.path.dirname(destination)
    assert destination == os.path.abspath(final_path)


def test_client_package_exception_preserves_existing_final_zip_and_cleans_temp(
        tmp_path, monkeypatch):
    app, instance_dir, output_dir = _prepare_client_overlay_package(tmp_path)
    final_path = output_dir / "client_模組語言包.zip"
    previous_bytes = _write_sentinel_package(final_path)

    def fail_overlay(*_args, **_kwargs):
        raise RuntimeError("injected packaging failure")

    monkeypatch.setattr(
        "core.jar_patcher.build_paxi_overlay_zip", fail_overlay)

    try:
        generate_jar_patches(
            app, str(output_dir), "client.zip", str(instance_dir))
    except RuntimeError as exc:
        assert "injected packaging failure" in str(exc)

    assert final_path.read_bytes() == previous_bytes
    assert not list(output_dir.glob("*.tmp"))


def test_client_package_cancellation_preserves_existing_final_zip_and_cleans_temp(
        tmp_path):
    app, instance_dir, output_dir = _prepare_client_overlay_package(tmp_path)
    final_path = output_dir / "client_模組語言包.zip"
    previous_bytes = _write_sentinel_package(final_path)

    def cancel_after_first_progress(*_args):
        app.stop_requested = True

    app.update_progress = cancel_after_first_progress

    output = generate_jar_patches(
        app, str(output_dir), "client.zip", str(instance_dir))

    assert output == ""
    assert final_path.read_bytes() == previous_bytes
    assert not list(output_dir.glob("*.tmp"))
