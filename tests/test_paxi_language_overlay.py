import io
import json
import os
import re
import tempfile
import zipfile

import pytest

from core.jar_patcher import (
    generate_jar_patches,
    jar_launch_risk_reasons,
    jar_rewrite_is_high_risk,
    rebuild_jar_with_inject,
)


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class OverlayApp:
    SYNTHETIC_LANG_ZH_TW = {}
    ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
    _RE_JAR_SIG = re.compile(r"^META-INF/.*\.(?:SF|DSA|RSA|EC)$", re.I)
    C_SUCCESS = "success"
    C_WARN = "warn"

    def __init__(self, jar_paths):
        self.stop_requested = False
        self.logs = []
        self.analyzed_jars = {
            jar_path: {
                "assets/example/lang/en_us.json": {
                    f"item.example.{word.lower()}": word
                }
            }
            for jar_path, word in zip(jar_paths, ("One", "Two", "Three"))
        }
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
        self.pack_format_var = Value(15)
        self.datapack_format_var = Value(15)

    def log(self, message):
        self.logs.append(message)

    def update_progress(self, *_args):
        pass

    def _output_mode_summary(self):
        return "JAR 直接套用", ""

    def _set_summary_card(self, *_args):
        pass

    def _refresh_api_summary(self):
        pass

    def _scope_allows_analyzed_path(self, *_args):
        return True

    def _scope_allows_extra(self, *_args):
        return True

    def _has_openloader_resources(self, *_args):
        return False

    def _jar_launch_risk_reasons(self, jar_path):
        return jar_launch_risk_reasons(jar_path)

    def _jar_rewrite_is_high_risk(self, reasons):
        return jar_rewrite_is_high_risk(reasons)

    def _load_official_minecraft_zh_base(self, *_args):
        return None

    def _is_advancement_json_path(self, *_args):
        return False

    def _is_structured_book_json_path(self, *_args):
        return False

    def _to_traditional(self, value):
        return value

    def process_json_data(self, data, *_args, **_kwargs):
        return {key: f"中:{value}" for key, value in data.items()}

    def _filter_lang_output_entries(self, _source, output):
        return output, 0, 0

    def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
        return rebuild_jar_with_inject(
            jar_path, temp_jar, inject, self._RE_JAR_SIG)

    def safe_decode_bytes(self, payload):
        return payload.decode("utf-8-sig")

    def _clean_json_text(self, text):
        return text

    def process_origin_json_display_fields(self, data):
        translated = dict(data)
        translated["display"] = dict(data.get("display", {}))
        translated["display"]["name"] = "已翻譯"
        return translated


def test_paxi_merges_language_overlays_without_rebuilding_source_jars():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        jar_paths = [os.path.join(mods_dir, name) for name in ("one.jar", "two.jar")]
        for jar_path in jar_paths:
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("META-INF/mods.toml", "modLoader='javafml'")

        output = generate_jar_patches(
            OverlayApp(jar_paths), tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            names = set(pack.namelist())
            assert "mods/one.jar" not in names
            assert "mods/two.jar" not in names
            overlays = [
                name for name in names
                if name.startswith("config/paxi/resourcepacks/")
                and name.endswith(".zip")
            ]
            assert len(overlays) == 1
            overlay_name = overlays[0]
            load_order = json.loads(pack.read(
                "config/paxi/resourcepack_load_order.json").decode("utf-8"))
            assert load_order["loadOrder"][-1] == os.path.basename(overlay_name)
            manifest = json.loads(pack.read(
                "_translator/PAXI_OVERLAY_MANIFEST.json").decode("utf-8"))
            assert overlay_name in manifest["active_files"]
            assert any(
                path.endswith("translation_自動翻譯覆蓋")
                for path in manifest["obsolete_directories"]
            )
            overlay_bytes = pack.read(overlay_name)

        with zipfile.ZipFile(io.BytesIO(overlay_bytes)) as overlay:
            assert "pack.mcmeta" in overlay.namelist()
            translated = json.loads(overlay.read(
                "assets/example/lang/zh_tw.json").decode("utf-8"))

        assert translated == {
            "item.example.one": "中:One",
            "item.example.two": "中:Two",
        }


def test_direct_client_mode_rebuilds_mod_jar_without_paxi_overlay():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        jar_path = os.path.join(mods_dir, "example.jar")
        with zipfile.ZipFile(jar_path, "w") as jar:
            jar.writestr("META-INF/mods.toml", "modLoader='javafml'")
            jar.writestr(
                "assets/example/lang/en_us.json",
                json.dumps({"item.example.one": "One"}),
            )
        original_bytes = open(jar_path, "rb").read()

        app = OverlayApp([jar_path])
        app._active_output_mode = "jar_patch"
        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            names = set(pack.namelist())
            assert "mods/example.jar" in names
            assert not any(
                name.startswith("config/paxi/resourcepacks/")
                for name in names
            )
            rebuilt_bytes = pack.read("mods/example.jar")

        with zipfile.ZipFile(io.BytesIO(rebuilt_bytes)) as rebuilt:
            translated = json.loads(rebuilt.read(
                "assets/example/lang/zh_tw.json").decode("utf-8"))

        assert translated == {"item.example.one": "中:One"}
        assert open(jar_path, "rb").read() == original_bytes


def test_direct_client_mode_does_not_emit_synthetic_paxi_pack():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        jar_path = os.path.join(mods_dir, "example.jar")
        with zipfile.ZipFile(jar_path, "w") as jar:
            jar.writestr("META-INF/mods.toml", "modLoader='javafml'")

        app = OverlayApp([jar_path])
        app._active_output_mode = "jar_patch"
        app.scope_mod_lang_var = Value(True)
        app.SYNTHETIC_LANG_ZH_TW = {"translator.test": "測試"}
        app.ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {"attribute.test": "屬性"}
        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            assert not any(
                name.startswith("config/paxi/")
                for name in pack.namelist()
            )


def test_paxi_synthetic_languages_are_inside_overlay_not_instance_assets():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.19.2.jar"), "wb").close()

        app = OverlayApp([])
        app.scope_mod_lang_var = Value(True)
        app.SYNTHETIC_LANG_ZH_TW = {"translator.test": "測試"}
        app.ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {"attribute.test": "屬性"}

        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            names = set(pack.namelist())
            assert not any(name.startswith("assets/") for name in names)
            overlay_name = next(
                name for name in names
                if name.startswith("config/paxi/resourcepacks/")
                and name.endswith(".zip")
            )
            overlay_bytes = pack.read(overlay_name)

        with zipfile.ZipFile(io.BytesIO(overlay_bytes)) as overlay:
            translator = json.loads(overlay.read(
                "assets/mc_modpack_translator/lang/zh_tw.json"))
            attributes = json.loads(overlay.read(
                "assets/additionalentityattributes/lang/zh_tw.json"))

        assert translator == {"translator.test": "測試"}
        assert attributes == {"attribute.test": "屬性"}


def test_paxi_overlay_prefers_detected_formats_over_stale_controls():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.19.2.jar"), "wb").close()
        jar_path = os.path.join(mods_dir, "mixed.jar")
        advancement_path = "data/example/advancements/root.json"
        with zipfile.ZipFile(jar_path, "w") as jar:
            jar.writestr("META-INF/mods.toml", "modLoader='javafml'")

        app = OverlayApp([jar_path])
        app._detected_resource_pack_format = 9
        app._detected_data_pack_format = 10
        app.analyzed_jars[jar_path][advancement_path] = {
            "display": {"title": "Root"}
        }
        app._is_advancement_json_path = lambda path: "/advancements/" in path
        app._process_advancement_json = lambda _data: {
            "display": {"title": "根節點"}
        }

        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            resource_name = next(
                name for name in pack.namelist()
                if name.startswith("config/paxi/resourcepacks/")
                and name.endswith(".zip"))
            data_name = next(
                name for name in pack.namelist()
                if name.startswith("config/paxi/datapacks/")
                and name.endswith(".zip"))
            resource_bytes = pack.read(resource_name)
            data_bytes = pack.read(data_name)

        with zipfile.ZipFile(io.BytesIO(resource_bytes)) as resource_pack:
            resource_meta = json.loads(resource_pack.read("pack.mcmeta"))
        with zipfile.ZipFile(io.BytesIO(data_bytes)) as data_pack:
            data_meta = json.loads(data_pack.read("pack.mcmeta"))

        assert resource_meta["pack"]["pack_format"] == 9
        assert data_meta["pack"]["pack_format"] == 10


def test_paxi_does_not_offload_existing_resourcepack_archives():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        resourcepacks_dir = os.path.join(mc_dir, "resourcepacks")
        os.makedirs(mods_dir)
        os.makedirs(resourcepacks_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        jar_paths = [
            os.path.join(resourcepacks_dir, name)
            for name in ("one.jar", "two.jar")
        ]
        for jar_path in jar_paths:
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("pack.mcmeta", "{}")

        output = generate_jar_patches(
            OverlayApp(jar_paths), tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            names = set(pack.namelist())
            assert "resourcepacks/one.jar" in names
            assert "resourcepacks/two.jar" in names
            assert not any(
                name.startswith("config/paxi/resourcepacks/")
                for name in names
            )


def test_paxi_language_collision_order_is_deterministic():
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        by_name = {
            name: os.path.join(mods_dir, name)
            for name in ("a.jar", "z.jar")
        }
        for jar_path in by_name.values():
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("META-INF/mods.toml", "modLoader='javafml'")

        for run, names in enumerate((("z.jar", "a.jar"), ("a.jar", "z.jar"))):
            jar_paths = [by_name[name] for name in names]
            app = OverlayApp(jar_paths)
            app.analyzed_jars = {
                jar_path: {
                    "assets/example/lang/en_us.json": {
                        "item.example.shared": os.path.basename(jar_path)
                    }
                }
                for jar_path in jar_paths
            }
            output = generate_jar_patches(
                app, tmp, f"translation-{run}.zip", mc_dir)
            with zipfile.ZipFile(output) as pack:
                overlay_name = next(
                    name for name in pack.namelist()
                    if name.startswith("config/paxi/resourcepacks/")
                    and name.endswith(".zip")
                )
                overlay_bytes = pack.read(overlay_name)
            with zipfile.ZipFile(io.BytesIO(overlay_bytes)) as overlay:
                translated = json.loads(overlay.read(
                    "assets/example/lang/zh_tw.json").decode("utf-8"))
                results.append(translated["item.example.shared"])

    assert results == ["中:z.jar", "中:z.jar"]


def test_paxi_offloads_pure_mod_advancement_without_rebuilding_jar():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        jar_path = os.path.join(mods_dir, "advancement.jar")
        advancement_path = "data/example/advancements/root.json"
        with zipfile.ZipFile(jar_path, "w") as jar:
            jar.writestr("META-INF/mods.toml", "modLoader='javafml'")
            jar.writestr(advancement_path, json.dumps({
                "display": {"title": "Root"}
            }))

        app = OverlayApp([jar_path])
        app.analyzed_jars = {
            jar_path: {
                advancement_path: {"display": {"title": "Root"}}
            }
        }
        app._is_advancement_json_path = (
            lambda path: "/advancements/" in path)
        app._process_advancement_json = lambda _data: {
            "display": {"title": "根節點"}
        }

        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            names = set(pack.namelist())
            assert "mods/advancement.jar" not in names
            datapack_name = next(
                name for name in names
                if name.startswith("config/paxi/datapacks/")
                and name.endswith(".zip")
            )
            load_order = json.loads(pack.read(
                "config/paxi/datapack_load_order.json").decode("utf-8"))
            assert load_order["loadOrder"][-1] == os.path.basename(datapack_name)
            datapack_bytes = pack.read(datapack_name)

        with zipfile.ZipFile(io.BytesIO(datapack_bytes)) as datapack:
            translated = json.loads(datapack.read(
                advancement_path).decode("utf-8"))
            assert translated["display"]["title"] == "根節點"


def test_paxi_offloads_advancement_and_never_rebuilds_class_residual():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        jar_path = os.path.join(mods_dir, "mixed.jar")
        advancement_path = "data/example/advancements/root.json"
        class_path = "example/item/Tooltip.class"
        with zipfile.ZipFile(jar_path, "w") as jar:
            jar.writestr("META-INF/mods.toml", "modLoader='javafml'")
            jar.writestr(advancement_path, json.dumps({
                "display": {"title": "Root"}
            }))
            jar.writestr(class_path, b"original")

        app = OverlayApp([jar_path])
        app.analyzed_jars = {
            jar_path: {
                advancement_path: {"display": {"title": "Root"}}
            }
        }
        app.analyzed_class_texts = {
            jar_path: {class_path: ["Tooltip"]}
        }
        app._is_advancement_json_path = (
            lambda path: "/advancements/" in path)
        app._process_advancement_json = lambda _data: {
            "display": {"title": "根節點"}
        }
        app._build_class_inject_for_jar = lambda *_args: {
            class_path: b"patched"
        }

        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            assert "mods/mixed.jar" not in pack.namelist()
            datapack_name = next(
                name for name in pack.namelist()
                if name.startswith("config/paxi/datapacks/")
                and name.endswith(".zip")
            )
            datapack_bytes = pack.read(datapack_name)
        with zipfile.ZipFile(io.BytesIO(datapack_bytes)) as datapack:
            assert advancement_path in datapack.namelist()
        assert any(
            "mixed.jar" in line and "class" in line and "不修改" in line
            for line in app.logs
        )


def test_client_jar_patch_never_emits_class_only_mod_jar_without_paxi():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        os.makedirs(mods_dir)
        jar_path = os.path.join(mods_dir, "class-only.jar")
        class_path = "example/item/Tooltip.class"
        with zipfile.ZipFile(jar_path, "w") as jar:
            jar.writestr("META-INF/mods.toml", "modLoader='javafml'")
            jar.writestr(class_path, b"original")

        app = OverlayApp([jar_path])
        app.analyzed_jars = {}
        app.analyzed_class_texts = {
            jar_path: {class_path: ["Tooltip"]}
        }
        app._build_class_inject_for_jar = lambda *_args: {
            class_path: b"patched"
        }

        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        assert output == ""
        assert not os.path.exists(
            os.path.join(tmp, "translation_模組語言包.zip"))
        assert any(
            "class-only.jar" in line and "class" in line and "不修改" in line
            for line in app.logs
        )


@pytest.mark.parametrize(
    ("paxi_kind", "internal_path"),
    [
        ("resourcepacks", "assets/example/origins/root.json"),
        ("datapacks", "data/example/origins/root.json"),
    ],
)
def test_existing_paxi_archive_json_moves_to_small_generated_overlay(
        paxi_kind, internal_path):
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        source_dir = os.path.join(mc_dir, "config", "paxi", paxi_kind)
        os.makedirs(mods_dir)
        os.makedirs(source_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        source_zip = os.path.join(source_dir, "large-source.zip")
        source_data = {
            "display": {"name": "Original"},
            "technical": {"id": "example:root", "weight": 3},
        }
        with zipfile.ZipFile(source_zip, "w") as source:
            source.writestr("pack.mcmeta", "{}")
            source.writestr(internal_path, json.dumps(source_data))
            source.writestr("large-unused.bin", b"x" * 100_000)

        app = OverlayApp([])
        app.analyzed_zip_json = [(source_zip, internal_path)]
        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            names = set(pack.namelist())
            source_rel = os.path.relpath(source_zip, mc_dir).replace("\\", "/")
            assert source_rel not in names
            generated_name = next(
                name for name in names
                if name.startswith(f"config/paxi/{paxi_kind}/")
                and name.endswith(".zip")
                and not name.endswith("large-source.zip")
            )
            generated_bytes = pack.read(generated_name)

        with zipfile.ZipFile(io.BytesIO(generated_bytes)) as generated:
            translated = json.loads(generated.read(internal_path).decode("utf-8"))
            assert translated["display"]["name"] == "已翻譯"
            assert translated["technical"] == source_data["technical"]


def test_regular_resourcepack_origin_json_keeps_archive_output_semantics():
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        source_dir = os.path.join(mc_dir, "resourcepacks")
        os.makedirs(mods_dir)
        os.makedirs(source_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        source_zip = os.path.join(source_dir, "selectable.zip")
        internal_path = "assets/example/origins/root.json"
        with zipfile.ZipFile(source_zip, "w") as source:
            source.writestr("pack.mcmeta", "{}")
            source.writestr(internal_path, json.dumps({
                "display": {"name": "Original"}
            }))

        app = OverlayApp([])
        app.analyzed_zip_json = [(source_zip, internal_path)]
        output = generate_jar_patches(
            app, tmp, "translation.zip", mc_dir)

        with zipfile.ZipFile(output) as pack:
            rebuilt_bytes = pack.read("resourcepacks/selectable.zip")
        with zipfile.ZipFile(io.BytesIO(rebuilt_bytes)) as rebuilt:
            translated = json.loads(rebuilt.read(internal_path).decode("utf-8"))
            assert translated["display"]["name"] == "已翻譯"


def test_paxi_source_archive_collision_order_is_deterministic():
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        mc_dir = os.path.join(tmp, "instance")
        mods_dir = os.path.join(mc_dir, "mods")
        source_dir = os.path.join(mc_dir, "config", "paxi", "resourcepacks")
        os.makedirs(mods_dir)
        os.makedirs(source_dir)
        open(os.path.join(mods_dir, "Paxi-1.20.1.jar"), "wb").close()
        internal_path = "assets/example/origins/root.json"
        by_name = {}
        for name, value in (("a.zip", "A"), ("z.zip", "Z")):
            source_zip = os.path.join(source_dir, name)
            by_name[name] = source_zip
            with zipfile.ZipFile(source_zip, "w") as source:
                source.writestr("pack.mcmeta", "{}")
                source.writestr(internal_path, json.dumps({
                    "display": {"name": value}
                }))

        for run, names in enumerate((("z.zip", "a.zip"), ("a.zip", "z.zip"))):
            app = OverlayApp([])
            app.analyzed_zip_json = [
                (by_name[name], internal_path) for name in names
            ]
            app.process_origin_json_display_fields = lambda data: {
                "display": {"name": f"中:{data['display']['name']}"}
            }
            output = generate_jar_patches(
                app, tmp, f"translation-{run}.zip", mc_dir)
            with zipfile.ZipFile(output) as pack:
                generated_name = next(
                    name for name in pack.namelist()
                    if name.startswith("config/paxi/resourcepacks/")
                    and name.endswith(".zip")
                )
                generated_bytes = pack.read(generated_name)
            with zipfile.ZipFile(io.BytesIO(generated_bytes)) as generated:
                translated = json.loads(generated.read(
                    internal_path).decode("utf-8"))
                results.append(translated["display"]["name"])

    assert results == ["中:Z", "中:Z"]
