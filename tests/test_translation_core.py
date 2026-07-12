import io
import json
import os
import re
import tempfile
import time
import unittest
import zipfile

from core.adaptive_concurrency import AdaptiveConcurrency
from core.batch_translation import (
    engine_rate_limit_cooldown,
    likely_bing_passthrough,
    select_ready_engine,
    should_split_timeout_batch,
    soft_throttle_after_success,
    interruptible_sleep,
    stoppable_executor,
    translation_fallback_order,
    translation_worker_limit,
)
from core.analysis_scan import (
    is_translator_generated_dir_name,
    scan_single_jar,
    should_descend_scan_dir,
    should_scan_translation_archive,
)
from core.jar_patcher import (
    generate_jar_patches,
    has_paxi,
    jar_launch_risk_reasons,
    jar_rewrite_is_high_risk,
    mixin_targets_client_renderer,
    paxi_load_order_bytes,
    rebuild_jar_with_inject,
    split_paxi_safe_inject,
)
from core.json_loader import load_json_lenient
from core.translation_flow import save_post_batch_checkpoint
from repair_advancements import reflow_book_txt
from translation_cache import (
    _format_tokens_match,
    load_translation_cache,
    save_translation_cache,
)
from translation_packager import (
    is_localized_manual_resource,
    is_preferred_manual_source,
    locale_segment,
    merge_zh_base_fallback,
    merge_structured_json_with_existing_zh,
    parse_legacy_lang_content,
    structured_json_needs_update,
    translated_fallback_paths,
    translated_lang_path,
    translated_repair_fallback_paths,
)
from translator_providers import (
    BING_BATCH_SIZE,
    GTX_BATCH_SIZE,
    GTX_GATE_INTERVAL,
    _extract_json_object_text,
    _strip_code_fence,
    chat_completions_url,
    model_batch_limit,
    normalize_base_url,
    parse_gtx_numbered_batch,
)
from gui.main_window import ModTranslatorApp


FORMAT_RE = re.compile(r'%\d*\$?[sd]|§[0-9a-fk-or]|[IVXLCDM]+')


class TranslationCacheTests(unittest.TestCase):
    def test_format_tokens_compare_kind_not_only_count(self):
        self.assertFalse(_format_tokens_match("HP %s", "生命 %d", FORMAT_RE))
        self.assertTrue(_format_tokens_match("%1$s uses %2$s", "%2$s 使用 %1$s", FORMAT_RE))
        self.assertTrue(_format_tokens_match("Tier IV", "等級四", FORMAT_RE))

    def test_shelve_cache_filters_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translation_cache.json")
            cache, _messages = load_translation_cache(path, FORMAT_RE, lambda _s, t: bool(t))
            cache["Hello %s"] = "你好 %s"
            cache["Bad %s"] = "壞 %d"
            save_translation_cache(path, cache)
            self.assertEqual(cache.get("Hello %s"), "你好 %s")
            self.assertIsNone(cache.get("Bad %s"))
            cache.close()

    def test_shelve_empty_dir_repairs_from_bak(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translation_cache.json")
            cache, _messages = load_translation_cache(path, FORMAT_RE, lambda _s, t: bool(t))
            cache["Hello"] = "你好"
            cache.close()

            dir_path = path + ".shelve.dir"
            bak_path = path + ".shelve.bak"
            if os.path.exists(dir_path) and os.path.exists(bak_path):
                with open(dir_path, "wb"):
                    pass
                repaired, messages = load_translation_cache(
                    path, FORMAT_RE, lambda _s, t: bool(t))
                try:
                    self.assertEqual(repaired.get("Hello"), "你好")
                    self.assertTrue(any("已修復 shelve 快取索引檔" in m for m in messages))
                finally:
                    if hasattr(repaired, "close"):
                        repaired.close()

    def test_shelve_bulk_update_can_defer_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translation_cache.json")
            cache, _messages = load_translation_cache(path, FORMAT_RE, lambda _s, t: bool(t))
            try:
                written = cache.bulk_update(
                    [("Apple", "蘋果"), ("Bad %s", "壞 %d")],
                    sync=False,
                )
                self.assertEqual(written, 1)
                self.assertEqual(cache.get("Apple"), "蘋果")
                self.assertIsNone(cache.get("Bad %s"))
                cache.sync()
            finally:
                if hasattr(cache, "close"):
                    cache.close()

    def test_delete_removes_pending_update_and_persisted_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translation_cache.json")
            cache, _messages = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t))
            try:
                cache["Hello"] = "你好"
                cache.sync()
                cache["Hello"] = "您好"

                del cache["Hello"]

                self.assertNotIn("Hello", cache)
                self.assertEqual(len(cache), 0)
                cache.sync()
            finally:
                cache.close()

            reopened, _messages = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t))
            try:
                self.assertNotIn("Hello", reopened)
            finally:
                reopened.close()

    def test_pop_removes_pending_update_and_persisted_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translation_cache.json")
            cache, _messages = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t))
            try:
                cache["Hello"] = "你好"
                cache.sync()
                cache["Hello"] = "您好"

                self.assertEqual(cache.pop("Hello"), "您好")

                self.assertNotIn("Hello", cache)
                self.assertEqual(len(cache), 0)
                cache.sync()
            finally:
                cache.close()

            reopened, _messages = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t))
            try:
                self.assertNotIn("Hello", reopened)
            finally:
                reopened.close()

    def test_sqlite_snapshot_merges_persisted_and_pending_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translation_cache.json")
            cache, _messages = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t))
            try:
                cache.bulk_update([
                    ("Persisted", "舊值"),
                    ("Stored", "資料庫"),
                ])
                cache["Persisted"] = "新值"
                cache["Pending"] = "待寫入"

                snapshot = cache.snapshot(
                    {"Persisted", "Stored", "Pending", "Missing"})

                self.assertEqual(snapshot, {
                    "Persisted": "新值",
                    "Stored": "資料庫",
                    "Pending": "待寫入",
                })
            finally:
                cache.close()

    def test_sqlite_snapshot_preserves_original_keys_after_sanitized_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translation_cache.json")
            cache, _messages = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t))
            try:
                canonical = "BrokenKey"
                malformed = "Broken\ud800Key"
                cache[canonical] = "資料庫值"
                cache.sync()
                cache[canonical] = "待寫入值"

                snapshot = cache.snapshot({canonical, malformed})

                self.assertEqual(snapshot, {
                    canonical: "待寫入值",
                    malformed: "待寫入值",
                })
            finally:
                cache.close()

    def test_pause_checkpoint_uses_light_save_only(self):
        class App:
            stop_requested = True
            pause_requested = True

            def __init__(self):
                self.calls = []

            def save_cache(self, light=False):
                self.calls.append(light)

        app = App()
        self.assertFalse(save_post_batch_checkpoint(app))
        self.assertEqual(app.calls, [True])

    def test_normal_post_batch_checkpoint_stays_light(self):
        class App:
            stop_requested = False
            pause_requested = False

            def __init__(self):
                self.calls = []

            def save_cache(self, light=False):
                self.calls.append(light)

        app = App()
        self.assertTrue(save_post_batch_checkpoint(app))
        self.assertEqual(app.calls, [True])


class ProviderHelperTests(unittest.TestCase):
    def test_model_batch_limit(self):
        self.assertEqual(model_batch_limit("claude-3-5-sonnet"), 80)
        self.assertEqual(model_batch_limit("gpt-5-mini"), 50)
        self.assertEqual(model_batch_limit("deepseek-v4-flash"), 40)
        self.assertEqual(model_batch_limit("unknown", default=17), 17)

    def test_bing_batch_size_matches_gui_chunk_limit(self):
        self.assertEqual(BING_BATCH_SIZE, 320)
        self.assertEqual(
            ModTranslatorApp._translation_batch_limits(None, "non_ai_chain", "bing", ""),
            (320, 48000),
        )

    def test_gtx_gate_uses_fast_fallback_interval(self):
        self.assertLessEqual(GTX_GATE_INTERVAL, 0.15)
        self.assertGreaterEqual(GTX_BATCH_SIZE, 40)

    def test_gtx_numbered_batch_parser_keeps_every_item(self):
        raw = "[[MCT000]] 救贖之戒\n[[ MCT001 ]] 護甲穿透\n[[MCT002]] 冰霜傷害"
        self.assertEqual(
            parse_gtx_numbered_batch(raw, 3),
            ["救贖之戒", "護甲穿透", "冰霜傷害"],
        )
        self.assertIsNone(parse_gtx_numbered_batch(raw, 4))

    def test_url_helpers(self):
        self.assertEqual(
            normalize_base_url("token-plan-sgp.xiaomimimo.com/v1/chat/completions"),
            "https://token-plan-sgp.xiaomimimo.com/v1",
        )
        self.assertEqual(
            chat_completions_url("https://api.example.com/models"),
            "https://api.example.com/v1/chat/completions",
        )

    def test_response_json_extractors(self):
        self.assertEqual(_strip_code_fence("```json\n{\"a\":1}\n```"), '{"a":1}')
        text, parsed = _extract_json_object_text("prefix {\"a\": 1} suffix {\"b\": 2}")
        self.assertEqual(text, '{"a": 1}')
        self.assertEqual(parsed, {"a": 1})


class PackagerTests(unittest.TestCase):
    def test_legacy_lang_and_translated_path(self):
        parsed = parse_legacy_lang_content("# c\nitem.foo=Foo\nbad line\n")
        self.assertEqual(parsed, {"item.foo": "Foo"})
        self.assertEqual(
            translated_lang_path("assets/example/lang/en_us.json"),
            "assets/example/lang/zh_tw.json",
        )
        self.assertEqual(
            translated_lang_path("assets/example/book/en_us/page.txt"),
            "assets/example/book/zh_tw/page.txt",
        )
        self.assertEqual(
            translated_lang_path("assets/example/book/en_gb/page.txt"),
            "assets/example/book/zh_tw/page.txt",
        )
        self.assertEqual(
            translated_lang_path(
                "assets/alexsmobs/book/animal_dictionary/title_text/pt_br/blue_jay.json"),
            "assets/alexsmobs/book/animal_dictionary/title_text/zh_tw/blue_jay.json",
        )
        self.assertEqual(
            translated_lang_path(
                "data/example/patchouli_books/book/en_gb/entries/gem_socketing.json"),
            "data/example/patchouli_books/book/zh_tw/entries/gem_socketing.json",
        )
        self.assertEqual(
            translated_lang_path(
                "assets/immersiveengineering/manual/en_us/accumulators.txt"),
            "assets/immersiveengineering/manual/zh_tw/accumulators.txt",
        )
        self.assertEqual(
            translated_fallback_paths(
                "assets/immersiveengineering/manual/en_us/accumulators.txt"),
            ("assets/immersiveengineering/manual/en_us/accumulators.txt",),
        )
        self.assertTrue(is_localized_manual_resource(
            "assets/immersiveengineering/manual/en_us/accumulators.txt"))
        self.assertIsNone(translated_lang_path(
            "assets/immersiveengineering/manual/accumulators.json"))
        self.assertIsNone(translated_lang_path(
            "data/blue_skies/blue_skies/journal/entries/azulfo.json"))
        self.assertEqual(locale_segment("assets/example/book/en_us/page.txt"), "en_us")
        self.assertTrue(is_preferred_manual_source("assets/example/book/en_gb/page.txt"))
        self.assertTrue(is_preferred_manual_source("assets/example/book/zh_cn/page.txt"))
        self.assertFalse(is_preferred_manual_source("assets/example/book/pt_br/page.txt"))
        self.assertEqual(
            translated_fallback_paths(
                "data/example/patchouli_books/book/en_us/entry.json"),
            ("data/example/patchouli_books/book/en_us/entry.json",),
        )
        self.assertEqual(
            translated_fallback_paths("assets/example/lang/en_us.json"),
            (),
        )
        self.assertEqual(
            translated_repair_fallback_paths(
                "assets/alexsmobs/book/animal_dictionary/zh_tw/capuchin_monkey.txt"),
            ("assets/alexsmobs/book/animal_dictionary/en_us/capuchin_monkey.txt",),
        )
        self.assertEqual(
            translated_repair_fallback_paths(
                "data/example/patchouli_books/book/zh_tw/categories/root.json"),
            ("data/example/patchouli_books/book/en_us/categories/root.json",),
        )

    def test_lenient_json_loader(self):
        def clean(text):
            return text.replace(",}", "}")
        self.assertEqual(load_json_lenient("\ufeff{\"a\":1,}", clean), {"a": 1})

    def test_structured_json_merge_updates_partial_existing_translation(self):
        source = {
            "name": "Gem Socketing",
            "category": "example:category",
            "pages": [
                {"type": "patchouli:text", "text": "Unwanted Unique weapons can be smelted down."},
                {"type": "patchouli:spotlight", "item": "example:gem"},
            ],
        }
        existing = {
            "name": "Gem Socketing",
            "category": "example:category",
            "pages": [
                {"type": "patchouli:text", "text": "Unwanted Unique weapons can be smelted down."},
                {"type": "patchouli:spotlight", "item": "example:gem"},
            ],
        }

        self.assertTrue(structured_json_needs_update(
            source, existing, ModTranslatorApp._lang_value_needs_update))

        def process(data):
            if isinstance(data, dict):
                return {
                    key: process(value)
                    for key, value in data.items()
                }
            if isinstance(data, list):
                return [process(value) for value in data]
            if data == "Gem Socketing":
                return "寶石鑲嵌"
            if data == "Unwanted Unique weapons can be smelted down.":
                return "不需要的獨特武器可以熔化。"
            return data

        merged = merge_structured_json_with_existing_zh(
            source, existing, process, lambda value: value,
            value_needs_update=ModTranslatorApp._lang_value_needs_update)
        self.assertEqual(merged["name"], "寶石鑲嵌")
        self.assertEqual(merged["pages"][0]["text"], "不需要的獨特武器可以熔化。")
        self.assertEqual(merged["pages"][1]["item"], "example:gem")

    def test_partial_zh_tw_base_uses_zh_cn_fallback(self):
        zh_cn = {
            "origin.origins-classes.warrior.name": "战士",
            "origin.origins-classes.warrior.description": "可敬的战士们更乐意以剑与盾作战。",
            "nested": {"title": "章节", "text": "来自简体中文的底稿。"},
        }
        zh_tw = {
            "_comment": "official zh_tw placeholder",
            "nested": {"title": "章節"},
        }

        merged = merge_zh_base_fallback(zh_cn, zh_tw)

        self.assertEqual(merged["_comment"], "official zh_tw placeholder")
        self.assertEqual(merged["origin.origins-classes.warrior.name"], "战士")
        self.assertEqual(merged["origin.origins-classes.warrior.description"], "可敬的战士们更乐意以剑与盾作战。")
        self.assertEqual(merged["nested"]["title"], "章節")
        self.assertEqual(merged["nested"]["text"], "来自简体中文的底稿。")


class BookReflowTests(unittest.TestCase):
    def test_reflow_book_txt_idempotent_and_keeps_newline_marker(self):
        src = "<NEWLINE>\n這是一段很長的繁體中文內容，需要被重排避免跑出書本範圍。"
        once = reflow_book_txt(src)
        twice = reflow_book_txt(once)
        self.assertEqual(once, twice)
        self.assertIn("<NEWLINE>", once)

    def test_book_txt_output_guard_rejects_untranslated_english(self):
        class FakeBookApp:
            _has_cjk_text = staticmethod(ModTranslatorApp._has_cjk_text)
            _normalize_inline_book_markers = staticmethod(
                ModTranslatorApp._normalize_inline_book_markers)

            def should_translate(self, text):
                return isinstance(text, str) and any(ch.isalpha() for ch in text)

            def _normalize_book_text_block(self, block):
                return ModTranslatorApp._normalize_book_text_block(block)

            def _book_text_translatable_paragraphs(self, content):
                return ModTranslatorApp._book_text_translatable_paragraphs(self, content)

            def _book_text_has_effective_translation(self, source, translated):
                return ModTranslatorApp._book_text_has_effective_translation(
                    self, source, translated)

        app = FakeBookApp()
        source = "Numerous strange creatures\ninhabit the Overworld."
        self.assertFalse(app._book_text_has_effective_translation(source, source))
        self.assertFalse(app._book_text_has_effective_translation(
            source, "Numerous strange creatures inhabit the Overworld."))
        self.assertTrue(app._book_text_has_effective_translation(
            source, "許多奇特的生物棲息在主世界。"))


class PatchouliJsonTranslationTests(unittest.TestCase):
    def test_patchouli_link_macro_text_is_translatable_but_plain_url_is_not(self):
        source = (
            "Iron's Spells 'n Spellbooks is an RPG-inspired spellcasting mod. "
            "You can fight dangerous wizards, raid structures, delve through dungeons, "
            "collect resources, and find powerful magical items. "
            "$(l:https://iron431.github.io/Irons-Spellbooks-Docs)External wiki here$(/l)"
        )
        app = object.__new__(ModTranslatorApp)

        self.assertTrue(app.should_translate(source))
        self.assertFalse(app.should_translate("https://iron431.github.io/Irons-Spellbooks-Docs"))

        collected = set()
        app._collect_strings_json({"landing_text": source}, collected, strict_context=True)
        self.assertIn(source, collected)

    def test_patchouli_book_json_translates_visible_text_only(self):
        source = (
            "Iron's Spells 'n Spellbooks is an RPG-inspired spellcasting mod. "
            "$(l:https://iron431.github.io/Irons-Spellbooks-Docs)External wiki here$(/l)"
        )
        translated = "鐵人法術書是 RPG 風格的施法模組。$(l:https://iron431.github.io/Irons-Spellbooks-Docs)外部 wiki 在這裡$(/l)"
        app = object.__new__(ModTranslatorApp)
        app.stop_requested = False
        app.get_translation = lambda text: translated if text == source else text

        result = app.process_json_data({
            "landing_text": source,
            "category": "irons_spellbooks:root",
            "pages": [{"type": "patchouli:text", "text": "Visible page text."}],
        }, strict_context=True)

        self.assertEqual(result["landing_text"], translated)
        self.assertEqual(result["category"], "irons_spellbooks:root")
        self.assertEqual(result["pages"][0]["type"], "patchouli:text")

    def test_translated_patchouli_macro_ids_do_not_trigger_retranslation(self):
        app = object.__new__(ModTranslatorApp)
        source = (
            "你的致命一擊造成的傷害量取決於你的"
            "$(l:apotheosis:adventure/attributes/crit_damage)暴擊傷害$()。"
        )

        self.assertFalse(app.should_translate(source))

    def test_visible_english_outside_patchouli_macros_still_gets_translated(self):
        app = object.__new__(ModTranslatorApp)

        self.assertTrue(app.should_translate("已翻譯但還有 Frost Ward 可見文字$(p)"))

    def test_untranslated_patchouli_output_detection_keeps_ids_safe(self):
        app = object.__new__(ModTranslatorApp)
        source = {
            "name": "Gem Socketing",
            "category": "apotheosis:adventure/root",
            "pages": [
                {
                    "type": "patchouli:text",
                    "text": "Unwanted Unique weapons can be smelted down.",
                    "item": "example:gem",
                },
                {"type": "patchouli:spotlight", "item": "example:gem"},
            ],
        }
        output = {
            "name": "Gem Socketing",
            "category": "apotheosis:adventure/root",
            "pages": [
                {
                    "type": "patchouli:text",
                    "text": "Unwanted Unique weapons can be smelted down.",
                    "item": "example:gem",
                },
                {"type": "patchouli:spotlight", "item": "example:gem"},
            ],
        }

        missing = set(app._collect_untranslated_visible_json_strings(
            source, output, strict_context=True))

        self.assertIn("Gem Socketing", missing)
        self.assertIn("Unwanted Unique weapons can be smelted down.", missing)
        self.assertNotIn("apotheosis:adventure/root", missing)
        self.assertNotIn("patchouli:text", missing)
        self.assertNotIn("example:gem", missing)

    def test_structured_book_json_packaging_never_calls_translation_network(self):
        app = object.__new__(ModTranslatorApp)
        app.stop_requested = False
        app._to_traditional = lambda value: value
        logs = []
        app.log = logs.append
        translations = {}

        def get_translation(text):
            return translations.get(text, text)

        app.get_translation = get_translation
        app.batch_translate_missing = lambda *_args, **_kwargs: self.fail(
            "packaging must not make translation network requests")

        source = {
            "name": "Gem Socketing",
            "category": "apotheosis:adventure/root",
            "pages": [
                {
                    "type": "patchouli:text",
                    "text": "Unwanted Unique weapons can be smelted down.",
                    "item": "example:gem",
                }
            ],
        }

        def process_book_data(data, preserve=False, strict=False):
            return app.process_json_data(data, preserve, True)

        merged = merge_structured_json_with_existing_zh(
            source, {}, process_book_data, app._to_traditional,
            value_needs_update=ModTranslatorApp._lang_value_needs_update)
        self.assertEqual(merged["name"], "Gem Socketing")
        self.assertEqual(
            merged["pages"][0]["text"],
            "Unwanted Unique weapons can be smelted down.")

        repaired = app._repair_structured_book_json_output(
            source, {}, merged, process_book_data, "test")

        self.assertIs(repaired, merged)
        self.assertEqual(repaired["name"], "Gem Socketing")
        self.assertEqual(
            repaired["pages"][0]["text"],
            "Unwanted Unique weapons can be smelted down.")
        self.assertEqual(repaired["category"], "apotheosis:adventure/root")
        self.assertEqual(repaired["pages"][0]["type"], "patchouli:text")
        self.assertEqual(repaired["pages"][0]["item"], "example:gem")
        self.assertTrue(any(
            "test" in message
            and "2" in message
            and "打包階段不發送網路翻譯" in message
            for message in logs))


class StructuralReferenceGuardTests(unittest.TestCase):
    def test_structural_file_references_are_not_translated(self):
        app = object.__new__(ModTranslatorApp)
        app.stop_requested = False
        app._to_traditional = lambda value: value
        app._translate_json_text_component_string = lambda value: None
        app.get_translation = lambda value: {
            "Build this structure.": "建造這個結構。",
            "HouseV2.nbt": "房屋V2.nbt",
            "prefab:schematics/HouseV2.nbt": "prefab:schematics/房屋V2.nbt",
        }.get(value, value)

        result = app.process_json_data({
            "schematic": "HouseV2.nbt",
            "prefab": "prefab:schematics/HouseV2.nbt",
            "text": "Build this structure.",
        })

        self.assertEqual(result["schematic"], "HouseV2.nbt")
        self.assertEqual(result["prefab"], "prefab:schematics/HouseV2.nbt")
        self.assertEqual(result["text"], "建造這個結構。")
        self.assertFalse(app.should_translate("HouseV2.nbt"))
        self.assertEqual(app.validate_translation("HouseV2.nbt", "房屋V2.nbt"), "HouseV2.nbt")

    def test_polluted_existing_structural_reference_is_repaired(self):
        source = {"schematic": "HouseV2.nbt", "name": "Prefab House"}
        existing = {"schematic": "房屋V2.nbt", "name": "預製房屋"}

        def process(data):
            if isinstance(data, dict):
                return {key: process(value) for key, value in data.items()}
            if data == "Prefab House":
                return "預製房屋"
            return data

        merged = merge_structured_json_with_existing_zh(
            source,
            existing,
            process,
            lambda value: value,
            value_needs_update=ModTranslatorApp._lang_value_needs_update,
        )

        self.assertTrue(ModTranslatorApp._lang_value_needs_update("HouseV2.nbt", "房屋V2.nbt"))
        self.assertFalse(ModTranslatorApp._lang_value_needs_update("HouseV2.nbt", "HouseV2.nbt"))
        self.assertEqual(merged["schematic"], "HouseV2.nbt")
        self.assertEqual(merged["name"], "預製房屋")


class AnalysisScanFilterTests(unittest.TestCase):
    def test_skips_translator_generated_overlay_and_backup_directories(self):
        self.assertTrue(is_translator_generated_dir_name("客戶端_自動翻譯覆蓋"))
        self.assertTrue(is_translator_generated_dir_name("MyPack_自動翻譯覆蓋"))
        self.assertTrue(is_translator_generated_dir_name("_backups"))
        self.assertTrue(is_translator_generated_dir_name("mc_modpack_translator"))
        self.assertFalse(is_translator_generated_dir_name("patchouli_books"))
        self.assertFalse(is_translator_generated_dir_name("datapacks"))

    def test_archive_filter_keeps_real_mods_and_skips_runtime_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_mod = os.path.join(tmp, "mods", "example.jar")
            cache_mod = os.path.join(tmp, ".fabric", "processedMods", "example.jar")
            root_jar = os.path.join(tmp, "Example Pack.jar")
            library_jar = os.path.join(tmp, "libraries", "ignored.jar")
            rp_zip = os.path.join(tmp, "resourcepacks", "ui.zip")
            rp_jar = os.path.join(tmp, "resourcepacks", "data_pack_style.jar")
            global_zip = os.path.join(tmp, "global_packs", "global.zip")
            natives_jar = os.path.join(tmp, "Example Pack-natives", "native-helper.jar")
            for path in (real_mod, cache_mod, root_jar, library_jar, rp_zip, rp_jar, global_zip, natives_jar):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(b"")

            self.assertTrue(should_scan_translation_archive(tmp, real_mod))
            self.assertTrue(should_scan_translation_archive(tmp, root_jar))
            self.assertTrue(should_scan_translation_archive(tmp, rp_zip))
            self.assertTrue(should_scan_translation_archive(tmp, rp_jar))
            self.assertTrue(should_scan_translation_archive(tmp, global_zip))
            self.assertFalse(should_scan_translation_archive(tmp, cache_mod))
            self.assertFalse(should_scan_translation_archive(tmp, library_jar))
            self.assertFalse(should_scan_translation_archive(tmp, natives_jar))

    def test_directory_filter_skips_runtime_caches_but_keeps_pack_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(should_descend_scan_dir(tmp, tmp, ".fabric"))
            self.assertFalse(should_descend_scan_dir(tmp, tmp, ".mixin.out"))
            self.assertFalse(should_descend_scan_dir(tmp, tmp, "libraries"))
            self.assertFalse(should_descend_scan_dir(tmp, tmp, "Example Pack-natives"))
            self.assertTrue(should_descend_scan_dir(tmp, tmp, "assets"))

            assets_dir = os.path.join(tmp, "assets")
            os.makedirs(assets_dir)
            self.assertFalse(should_descend_scan_dir(tmp, assets_dir, "objects"))
            self.assertTrue(should_descend_scan_dir(tmp, assets_dir, "examplemod"))

    def test_patchouli_scan_prefers_en_us_over_zh_cn_for_same_book(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class NullLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeScanApp:
            def __init__(self):
                self.process_mode_var = Var("append")
                self.scope_mod_lang_var = Var(False)
                self.scope_books_var = Var(True)
                self.scope_quests_var = Var(False)
                self._scan_process_mode = "append"
                self._scan_scope_mod_lang = False
                self._scan_scope_books = True
                self._scan_scope_quests = False
                self._scan_class_tooltip_patch = False
                self._scan_output_mode = "jar_patch"
                self._server_mode = False
                self._jar_lock = NullLock()
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

            def safe_decode_bytes(self, value):
                return value.decode("utf-8")

            def _clean_json_text(self, value):
                return value

            def _lang_value_needs_update(self, _source, _target):
                return False

        en_path = "data/irons_spellbooks/patchouli_books/iss_guide_book/en_us/entries/root.json"
        zh_cn_path = "data/irons_spellbooks/patchouli_books/iss_guide_book/zh_cn/entries/root.json"
        with tempfile.TemporaryDirectory() as tmp:
            jar_path = os.path.join(tmp, "manual.jar")
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr(en_path, json.dumps({"name": "Rune Etching", "text": "English page"}))
                jar.writestr(zh_cn_path, json.dumps({"name": "符文蚀刻", "text": "简体页面"}))

            app = FakeScanApp()
            scan_single_jar(app, jar_path)

        self.assertIn(jar_path, app.analyzed_jars)
        self.assertIn(en_path, app.analyzed_jars[jar_path])
        self.assertNotIn(zh_cn_path, app.analyzed_jars[jar_path])

    def test_book_txt_scan_keeps_en_us_when_zh_cn_exists(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class NullLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeScanApp:
            def __init__(self):
                self.process_mode_var = Var("append")
                self.scope_mod_lang_var = Var(False)
                self.scope_books_var = Var(True)
                self.scope_quests_var = Var(False)
                self._scan_process_mode = "append"
                self._scan_scope_mod_lang = False
                self._scan_scope_books = True
                self._scan_scope_quests = False
                self._scan_class_tooltip_patch = False
                self._scan_output_mode = "jar_patch"
                self._server_mode = False
                self._jar_lock = NullLock()
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

            def safe_decode_bytes(self, value):
                return value.decode("utf-8")

            def _clean_json_text(self, value):
                return value

            def _lang_value_needs_update(self, _source, _target):
                return False

            def _to_traditional(self, value):
                return value

            def _book_text_translatable_paragraphs(self, content):
                return ["english"] if "English animal text" in content else []

        en_path = "assets/alexsmobs/book/animal_dictionary/en_us/capuchin_monkey.txt"
        zh_cn_path = "assets/alexsmobs/book/animal_dictionary/zh_cn/capuchin_monkey.txt"
        with tempfile.TemporaryDirectory() as tmp:
            jar_path = os.path.join(tmp, "alexsmobs.jar")
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr(en_path, "<NEWLINE>\n<NEWLINE>\n<NEWLINE>\nEnglish animal text")
                jar.writestr(zh_cn_path, "<NEWLINE>\n<NEWLINE>\n<NEWLINE>\n简体动物文字")

            app = FakeScanApp()
            scan_single_jar(app, jar_path)

        self.assertIn(jar_path, app.analyzed_book_texts)
        self.assertIn(en_path, app.analyzed_book_texts[jar_path])
        self.assertNotIn(jar_path, app.analyzed_book_text_repairs)


class AdaptiveConcurrencyTests(unittest.TestCase):
    def test_non_ai_fallback_order_prioritizes_bing_azure_gtx(self):
        self.assertEqual(
            translation_fallback_order("non_ai_chain", "bing"),
            ["bing", "azure", "gtx"],
        )

    def test_engine_selection_uses_priority_and_skips_throttled_engine(self):
        active = ["bing", "azure", "gtx"]
        self.assertEqual(
            select_ready_engine(active, {"bing": 0, "azure": 0, "gtx": 0}, now=10),
            "bing",
        )
        self.assertEqual(
            select_ready_engine(active, {"bing": 20, "azure": 0, "gtx": 0}, now=10),
            "azure",
        )

    def test_short_title_like_text_can_bypass_bing(self):
        self.assertTrue(likely_bing_passthrough("Rimstone Slab"))
        self.assertTrue(likely_bing_passthrough("Horseweed"))
        self.assertTrue(likely_bing_passthrough("Runic Power: Active"))
        self.assertFalse(likely_bing_passthrough(
            "This sentence should be translated normally by the primary engine."))
        self.assertFalse(likely_bing_passthrough("item.mod.internal_name"))

    def test_free_market_fallback_keeps_extra_backups(self):
        self.assertEqual(
            translation_fallback_order(
                "market_ai", "market_ai", "deepseek_v4_flash_free"),
            ["bing", "azure", "gtx", "libretranslate", "google_api"],
        )

    def test_non_ai_timeout_fails_over_instead_of_splitting(self):
        self.assertFalse(should_split_timeout_batch("non_ai_chain", "bing", 320))
        self.assertFalse(should_split_timeout_batch("non_ai_chain", "azure", 320))
        self.assertFalse(should_split_timeout_batch("non_ai_chain", "gtx", 80))

    def test_non_ai_slow_success_can_soft_throttle(self):
        self.assertEqual(soft_throttle_after_success("non_ai_chain", "bing", 320, 4.0), 0.0)
        self.assertEqual(soft_throttle_after_success("non_ai_chain", "bing", 320, 10.0), 20.0)
        self.assertEqual(soft_throttle_after_success("non_ai_chain", "azure", 160, 12.0), 18.0)
        self.assertEqual(soft_throttle_after_success("market_ai", "bing", 320, 20.0), 0.0)

    def test_api_timeout_can_still_split_large_batches(self):
        self.assertTrue(should_split_timeout_batch("market_ai", "market_ai", 32))
        self.assertFalse(should_split_timeout_batch("market_ai", "market_ai", 1))

    def test_interruptible_sleep_stops_promptly(self):
        started = time.monotonic()
        self.assertFalse(interruptible_sleep(
            lambda: time.monotonic() - started >= 0.05,
            5.0,
            quantum=0.01,
        ))
        self.assertLess(time.monotonic() - started, 0.3)

    def test_stoppable_executor_does_not_wait_for_active_future(self):
        stop = [False]
        started = time.monotonic()
        with stoppable_executor(1, lambda: stop[0]) as executor:
            executor.submit(time.sleep, 0.5)
            stop[0] = True
        self.assertLess(time.monotonic() - started, 0.2)

    def test_non_ai_azure_cooldown_avoids_fast_bounce_back(self):
        self.assertEqual(engine_rate_limit_cooldown("non_ai_chain", "azure", 10), 90.0)
        self.assertEqual(engine_rate_limit_cooldown("non_ai_chain", "bing", 10), 60.0)
        self.assertEqual(engine_rate_limit_cooldown("azure", "azure", 10), 10.0)

    def test_translation_worker_limit_caps_bing_large_batch_burst(self):
        self.assertEqual(translation_worker_limit(16, "bing", "non_ai_chain"), 4)
        self.assertEqual(translation_worker_limit(32, "bing", "non_ai_chain"), 4)
        self.assertEqual(translation_worker_limit(16, "gtx", "non_ai_chain"), 8)
        self.assertEqual(translation_worker_limit(16, "mymemory", "non_ai_chain"), 4)
        self.assertEqual(translation_worker_limit(16, "deepseek", "market_ai"), 8)

    def test_rate_limit_halves_and_success_recovers(self):
        limiter = AdaptiveConcurrency(8, minimum=1, maximum=8)
        self.assertEqual(limiter.on_rate_limit(1), 4)
        self.assertEqual(limiter.effective_workers(), 4)
        time.sleep(1.05)
        for _ in range(8):
            limiter.on_success()
        self.assertGreaterEqual(limiter.effective_workers(), 5)


class JarPatcherTests(unittest.TestCase):
    def test_paxi_detection_and_safe_overlay_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "mods"))
            with open(os.path.join(tmp, "mods", "Paxi-1.19.2-Forge.jar"), "wb"):
                pass
            self.assertTrue(has_paxi(tmp))

        resources, data, residual = split_paxi_safe_inject({
            "assets/example/lang/zh_tw.json": b"{}",
            "assets/example/book/zh_tw/page.txt": b"text",
            "data/example/advancements/root.json": b"{}",
            "data/example/patchouli_books/book/zh_tw/entry.json": b"{}",
            "data/example/recipes/tool.json": b"{}",
            "com/example/Tooltip.class": b"class",
        })
        self.assertEqual(len(resources), 3)
        self.assertEqual(len(data), 2)
        self.assertEqual(len(residual), 2)
        self.assertIn(
            "assets/example/patchouli_books/book/zh_tw/entry.json",
            resources)

    def test_paxi_detection_ignores_generated_config_without_mod_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(
                tmp, "config", "paxi", "resourcepacks", "客戶端_自動翻譯覆蓋"))

            self.assertFalse(has_paxi(tmp))

    def test_paxi_load_order_preserves_existing_and_appends_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            paxi_dir = os.path.join(tmp, "config", "paxi")
            os.makedirs(paxi_dir)
            with open(os.path.join(paxi_dir, "datapack_load_order.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"loadOrder": ["base", "translation"]}, f)
            data = json.loads(paxi_load_order_bytes(
                tmp, "datapack_load_order.json", "translation").decode("utf-8"))
            self.assertEqual(data["loadOrder"], ["base", "translation"])

    def test_renderer_mixin_detection(self):
        self.assertTrue(mixin_targets_client_renderer(
            '{"client":["MixinLevelRenderer"]}'))
        self.assertFalse(mixin_targets_client_renderer(
            '{"mixins":["CommonConfigMixin"]}'))

    def test_jar_risk_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar_path = os.path.join(tmp, "renderer.jar")
            import zipfile
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("example.mixins.json",
                             '{"client":["MixinGameRenderer"]}')
            reasons = jar_launch_risk_reasons(jar_path)
            self.assertIn("renderer-mixin", reasons)
            self.assertTrue(jar_rewrite_is_high_risk(reasons))

    def test_high_risk_jar_without_safe_loader_is_not_rewritten(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeApp:
            SYNTHETIC_LANG_ZH_TW = {}
            ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
            _RE_JAR_SIG = re.compile(r"^META-INF/.*\.(?:SF|DSA|RSA|EC)$", re.I)
            C_SUCCESS = "success"
            C_WARN = "warn"

            def __init__(self, jar_path):
                self.stop_requested = False
                self.logs = []
                self.analyzed_jars = {
                    jar_path: {
                        "assets/origins-classes/lang/en_us.json": {
                            "origin.origins-classes.warrior.name": "Warrior"
                        }
                    }
                }
                self.analyzed_jars_zh_base = {}
                self.analyzed_loose = []
                self.analyzed_loose_base = {}
                self.analyzed_book_texts = {
                    jar_path: {
                        "assets/immersiveengineering/manual/en_us/accumulators.txt":
                            "Accumulator text"
                    }
                }
                self.analyzed_book_text_repairs = {
                    jar_path: {
                        "assets/alexsmobs/book/animal_dictionary/zh_tw/capuchin_monkey.txt":
                            "捲尾猴文字"
                    }
                }
                self.analyzed_class_texts = {}
                self.analyzed_extra = []
                self.analyzed_zip_json = []
                self.process_mode_var = Var("append")
                self.include_large_backups_var = Var(False)
                self.scope_mod_lang_var = Var(False)
                self.pack_format_var = Var(9)

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

            def process_json_data(self, _data, *_args, **_kwargs):
                return {"origin.origins-classes.warrior.name": "戰士"}

            def _filter_lang_output_entries(self, _source_data, output_data):
                return output_data, 0, 0

            def process_book_text_content(self, content, *_args):
                if content == "Accumulator text":
                    return "蓄電器文字"
                if content == "捲尾猴文字":
                    return "捲尾猴文字"
                return content

            def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
                return rebuild_jar_with_inject(
                    jar_path, temp_jar, inject, self._RE_JAR_SIG)

        with tempfile.TemporaryDirectory() as tmp:
            mc_dir = os.path.join(tmp, "mc")
            mods_dir = os.path.join(mc_dir, "mods")
            os.makedirs(mods_dir)
            jar_path = os.path.join(mods_dir, "origins-classes-forge.jar")
            import zipfile
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("META-INF/accesstransformer.cfg", "public net.minecraft.Example")
                jar.writestr("assets/origins-classes/lang/en_us.json", json.dumps({
                    "origin.origins-classes.warrior.name": "Warrior"
                }))

            app = FakeApp(jar_path)
            output_path = generate_jar_patches(app, tmp, "中文.zip", mc_dir)

            self.assertEqual(output_path, "")
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "中文_模組語言包.zip")))
            self.assertTrue(any(
                "origins-classes-forge.jar" in line
                and "禁止重包模組 JAR" in line
                for line in app.logs))

    def test_empty_translated_language_is_not_packaged(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeApp:
            SYNTHETIC_LANG_ZH_TW = {}
            ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
            _RE_JAR_SIG = re.compile(r"^META-INF/.*\.(?:SF|DSA|RSA|EC)$", re.I)
            C_SUCCESS = "success"
            C_WARN = "warn"

            def __init__(self, jar_path):
                self.stop_requested = False
                self.logs = []
                self.analyzed_jars = {
                    jar_path: {
                        "assets/example/lang/en_us.json": {
                            "item.example.blank": "Blank Item"
                        }
                    }
                }
                self.analyzed_jars_zh_base = {}
                self.analyzed_loose = []
                self.analyzed_loose_base = {}
                self.analyzed_book_texts = {}
                self.analyzed_book_text_repairs = {}
                self.analyzed_class_texts = {}
                self.analyzed_extra = []
                self.analyzed_zip_json = []
                self.process_mode_var = Var("force")
                self.include_large_backups_var = Var(False)
                self.scope_mod_lang_var = Var(False)
                self.pack_format_var = Var(9)

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

            def process_json_data(self, _data, *_args, **_kwargs):
                return {}

            def _filter_lang_output_entries(self, _source_data, _output_data):
                return {}, 1, 0

            def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
                return rebuild_jar_with_inject(
                    jar_path, temp_jar, inject, self._RE_JAR_SIG)

        with tempfile.TemporaryDirectory() as tmp:
            mc_dir = os.path.join(tmp, "mc")
            mods_dir = os.path.join(mc_dir, "mods")
            os.makedirs(mods_dir)
            jar_path = os.path.join(mods_dir, "empty-lang.jar")
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("assets/example/lang/en_us.json", json.dumps({
                    "item.example.blank": "Blank Item"
                }))

            app = FakeApp(jar_path)
            output_path = generate_jar_patches(app, tmp, "中文.zip", mc_dir)

            self.assertEqual("", output_path)
            self.assertFalse(os.path.exists(os.path.join(tmp, "中文.zip")))
            self.assertTrue(any("沒有可用繁中譯文" in line for line in app.logs))

    def test_resourcepack_archives_keep_original_output_paths(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeApp:
            SYNTHETIC_LANG_ZH_TW = {}
            ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
            _RE_JAR_SIG = re.compile(r"^META-INF/.*\.(?:SF|DSA|RSA|EC)$", re.I)
            C_SUCCESS = "success"
            C_WARN = "warn"

            def __init__(self, jar_path, zip_path):
                self.stop_requested = False
                self.logs = []
                self.analyzed_jars = {
                    jar_path: {
                        "assets/example/lang/en_us.json": {
                            "item.example.jar": "Jar Item"
                        }
                    },
                    zip_path: {
                        "assets/example/lang/en_us.json": {
                            "item.example.zip": "Zip Item"
                        }
                    },
                }
                self.analyzed_jars_zh_base = {}
                self.analyzed_loose = []
                self.analyzed_loose_base = {}
                self.analyzed_book_texts = {}
                self.analyzed_book_text_repairs = {}
                self.analyzed_class_texts = {}
                self.analyzed_extra = []
                self.analyzed_zip_json = []
                self.process_mode_var = Var("append")
                self.include_large_backups_var = Var(False)
                self.scope_mod_lang_var = Var(False)
                self.pack_format_var = Var(9)

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
                return {
                    key: ("JAR 物品" if value == "Jar Item" else "ZIP 物品")
                    for key, value in data.items()
                }

            def _filter_lang_output_entries(self, _source_data, output_data):
                return output_data, 0, 0

            def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
                return rebuild_jar_with_inject(
                    jar_path, temp_jar, inject, self._RE_JAR_SIG)

        with tempfile.TemporaryDirectory() as tmp:
            mc_dir = os.path.join(tmp, "mc")
            rp_dir = os.path.join(mc_dir, "resourcepacks")
            os.makedirs(rp_dir)
            jar_path = os.path.join(rp_dir, "shared.jar")
            zip_path = os.path.join(rp_dir, "shared.zip")
            for archive_path, key, value in (
                    (jar_path, "item.example.jar", "Jar Item"),
                    (zip_path, "item.example.zip", "Zip Item")):
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("assets/example/lang/en_us.json", json.dumps({
                        key: value
                    }))

            app = FakeApp(jar_path, zip_path)
            output_path = generate_jar_patches(app, tmp, "中文.zip", mc_dir)

            self.assertTrue(os.path.exists(output_path))
            with zipfile.ZipFile(output_path) as pack:
                self.assertIn("resourcepacks/shared.jar", pack.namelist())
                self.assertIn("resourcepacks/shared.zip", pack.namelist())
                self.assertNotIn("mods/shared.jar", pack.namelist())
                self.assertNotIn("mods/shared.zip", pack.namelist())
                jar_bytes = pack.read("resourcepacks/shared.jar")
                zip_bytes = pack.read("resourcepacks/shared.zip")

            for payload, key, expected in (
                    (jar_bytes, "item.example.jar", "JAR 物品"),
                    (zip_bytes, "item.example.zip", "ZIP 物品")):
                nested_path = os.path.join(tmp, f"{key}.zip")
                with open(nested_path, "wb") as f:
                    f.write(payload)
                with zipfile.ZipFile(nested_path) as nested:
                    self.assertIn("assets/example/lang/zh_tw.json", nested.namelist())
                    data = json.loads(nested.read(
                        "assets/example/lang/zh_tw.json").decode("utf-8"))
                self.assertEqual(data[key], expected)

            self.assertTrue(any("resourcepacks/shared.jar" in line for line in app.logs))
            self.assertTrue(any("resourcepacks/shared.zip" in line for line in app.logs))

    def test_high_risk_patchouli_with_paxi_uses_safe_overlays(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeApp:
            SYNTHETIC_LANG_ZH_TW = {}
            ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
            _RE_JAR_SIG = re.compile(r"^META-INF/.*\.(?:SF|DSA|RSA|EC)$", re.I)
            C_SUCCESS = "success"
            C_WARN = "warn"

            def __init__(self, jar_path):
                self.stop_requested = False
                self.logs = []
                self.analyzed_jars = {
                    jar_path: {
                        "data/simplyswords/patchouli_books/runic_grimoire/en_us/categories/category_gem_socketing.json": {
                            "name": "Gem Socketing",
                            "description": "Unwanted Unique weapons can be smelted down.",
                            "icon": "simplyswords:runefused_gem",
                        }
                    }
                }
                self.analyzed_jars_zh_base = {}
                self.analyzed_loose = []
                self.analyzed_loose_base = {}
                self.analyzed_book_texts = {}
                self.analyzed_book_text_repairs = {}
                self.analyzed_class_texts = {}
                self.analyzed_extra = []
                self.analyzed_zip_json = []
                self.process_mode_var = Var("append")
                self.include_large_backups_var = Var(False)
                self.scope_mod_lang_var = Var(False)
                self.pack_format_var = Var(9)
                self.datapack_format_var = Var(10)

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

            def _is_structured_book_json_path(self, path):
                return "/patchouli_books/" in path

            def _to_traditional(self, value):
                return value

            def _lang_value_needs_update(self, source, translated):
                return ModTranslatorApp._lang_value_needs_update(source, translated)

            def process_json_data(self, data, *_args, **_kwargs):
                return {
                    "name": "寶石鑲嵌",
                    "description": "不需要的獨特武器可以熔化。",
                    "icon": "simplyswords:runefused_gem",
                }

            def _filter_lang_output_entries(self, _source_data, output_data):
                return output_data, 0, 0

            def process_book_text_content(self, content, *_args):
                return content

            def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
                return rebuild_jar_with_inject(
                    jar_path, temp_jar, inject, self._RE_JAR_SIG)

        with tempfile.TemporaryDirectory() as tmp:
            mc_dir = os.path.join(tmp, "mc")
            mods_dir = os.path.join(mc_dir, "mods")
            os.makedirs(mods_dir)
            with open(os.path.join(mods_dir, "Paxi-1.19.2-Forge.jar"), "wb"):
                pass
            jar_path = os.path.join(mods_dir, "simplyswords.jar")
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("META-INF/accesstransformer.cfg", "public net.minecraft.Example")
                jar.writestr(
                    "data/simplyswords/patchouli_books/runic_grimoire/en_us/categories/category_gem_socketing.json",
                    json.dumps({
                        "name": "Gem Socketing",
                        "description": "Unwanted Unique weapons can be smelted down.",
                        "icon": "simplyswords:runefused_gem",
                    }),
                )

            app = FakeApp(jar_path)
            output_path = generate_jar_patches(app, tmp, "客戶端.zip", mc_dir)

            self.assertTrue(os.path.exists(output_path))
            with zipfile.ZipFile(output_path) as pack:
                names = set(pack.namelist())
                self.assertNotIn("mods/simplyswords.jar", names)
                self.assertIn("config/paxi/datapack_load_order.json", names)
                self.assertIn("config/paxi/resourcepack_load_order.json", names)
                datapack_name = next(
                    name for name in names
                    if name.startswith("config/paxi/datapacks/")
                    and name.endswith(".zip"))
                datapack_bytes = pack.read(datapack_name)
            with zipfile.ZipFile(io.BytesIO(datapack_bytes)) as datapack:
                patchouli_path = (
                    "data/simplyswords/patchouli_books/runic_grimoire/en_us/"
                    "categories/category_gem_socketing.json")
                self.assertIn(patchouli_path, datapack.namelist())
                data = json.loads(datapack.read(patchouli_path).decode("utf-8"))
            self.assertEqual(data["name"], "寶石鑲嵌")
            self.assertEqual(data["description"], "不需要的獨特武器可以熔化。")
            self.assertTrue(any("原始模組 JAR 不修改" in line for line in app.logs))

    def test_high_risk_patchouli_without_paxi_is_not_rewritten(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        class FakeApp:
            SYNTHETIC_LANG_ZH_TW = {}
            ADDITIONAL_ENTITY_ATTRIBUTES_ZH_TW = {}
            _RE_JAR_SIG = re.compile(r"^META-INF/.*\.(?:SF|DSA|RSA|EC)$", re.I)
            C_SUCCESS = "success"
            C_WARN = "warn"

            def __init__(self, jar_path):
                self.stop_requested = False
                self.logs = []
                self.analyzed_jars = {
                    jar_path: {
                        "data/irons_spellbooks/patchouli_books/iss_guide_book/book.json": {
                            "name": "Iron's Guidebook",
                            "landing_text": "Iron's Spells 'n Spellbooks is an RPG-inspired spellcasting mod.",
                            "version": 1,
                        }
                    }
                }
                self.analyzed_jars_zh_base = {}
                self.analyzed_loose = []
                self.analyzed_loose_base = {}
                self.analyzed_book_texts = {}
                self.analyzed_book_text_repairs = {}
                self.analyzed_class_texts = {}
                self.analyzed_extra = []
                self.analyzed_zip_json = []
                self.process_mode_var = Var("append")
                self.include_large_backups_var = Var(False)
                self.scope_mod_lang_var = Var(False)
                self.pack_format_var = Var(9)

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

            def _is_structured_book_json_path(self, path):
                return "/patchouli_books/" in path

            def _to_traditional(self, value):
                return value

            def _lang_value_needs_update(self, source, translated):
                return ModTranslatorApp._lang_value_needs_update(source, translated)

            def process_json_data(self, data, *_args, **_kwargs):
                return {
                    "name": "鐵人指南",
                    "landing_text": "鐵的咒語與魔法書是一款受角色扮演遊戲啟發的施法模組。",
                    "version": data["version"],
                }

            def _filter_lang_output_entries(self, _source_data, output_data):
                return output_data, 0, 0

            def process_book_text_content(self, content, *_args):
                return content

            def _rebuild_jar_with_inject(self, jar_path, temp_jar, inject):
                return rebuild_jar_with_inject(
                    jar_path, temp_jar, inject, self._RE_JAR_SIG)

        with tempfile.TemporaryDirectory() as tmp:
            mc_dir = os.path.join(tmp, "mc")
            mods_dir = os.path.join(mc_dir, "mods")
            os.makedirs(mods_dir)
            jar_path = os.path.join(mods_dir, "irons_spellbooks.jar")
            with zipfile.ZipFile(jar_path, "w") as jar:
                jar.writestr("META-INF/accesstransformer.cfg", "public net.minecraft.Example")
                jar.writestr(
                    "data/irons_spellbooks/patchouli_books/iss_guide_book/book.json",
                    json.dumps({
                        "name": "Iron's Guidebook",
                        "landing_text": "Iron's Spells 'n Spellbooks is an RPG-inspired spellcasting mod.",
                        "version": 1,
                    }),
                )

            app = FakeApp(jar_path)
            output_path = generate_jar_patches(app, tmp, "客戶端.zip", mc_dir)

            self.assertEqual(output_path, "")
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "客戶端_模組語言包.zip")))
            self.assertTrue(any(
                "irons_spellbooks.jar" in line
                and "禁止重包模組 JAR" in line
                for line in app.logs))


class GuiDelegateTests(unittest.TestCase):
    def test_camel_case_namespaced_component_is_not_literal_text(self):
        self.assertFalse(ModTranslatorApp._component_translate_value_is_literal(
            "advancement.enigmaticlegacy:discoverSpellstone"))
        self.assertTrue(ModTranslatorApp._component_translate_value_is_literal(
            "A New Beginning"))

    def test_extracted_static_helpers_are_still_wired(self):
        self.assertEqual(ModTranslatorApp._snbt_escape('A "B"\\n'), 'A \\"B\\"\\\\n')
        self.assertEqual(
            ModTranslatorApp._snbt_structure_signature('{"text":"Hello"}')[:3],
            (1, 0, 2),
        )

    def test_force_mode_cache_helper_ignores_old_entries(self):
        class Fake:
            def _is_valid_trad_translation(self, orig, trans):
                return ModTranslatorApp._is_valid_trad_translation(orig, trans)

        fake = Fake()
        fake.translation_cache = {"Old": "舊譯", "Fresh": "新譯"}
        fake._force_ignore_cache_strings = {"Old", "Fresh"}
        fake._session_translated_keys = {"Fresh"}

        # force 模式下仍允許快取 fallback：翻譯引擎未翻到的字串，
        # 輸出時會從快取取回，避免產生未翻譯的輸出
        self.assertTrue(ModTranslatorApp._cache_has_usable_translation(fake, "Old"))
        self.assertTrue(ModTranslatorApp._cache_has_usable_translation(fake, "Fresh"))

    def test_cache_helper_rejects_passthrough_english(self):
        class Fake:
            def _is_valid_trad_translation(self, orig, trans):
                return ModTranslatorApp._is_valid_trad_translation(orig, trans)

        fake = Fake()
        fake.translation_cache = {
            "Gem Socketing": "Gem Socketing",
            "Socketing Gems": "Socketing Gems",
            "Animal Dictionary": "動物詞典",
        }
        fake._force_ignore_cache_strings = set()
        fake._session_translated_keys = set()

        self.assertFalse(ModTranslatorApp._cache_has_usable_translation(
            fake, "Gem Socketing"))
        self.assertFalse(ModTranslatorApp._cache_has_usable_translation(
            fake, "Socketing Gems"))
        self.assertTrue(ModTranslatorApp._cache_has_usable_translation(
            fake, "Animal Dictionary"))

    def test_extracted_methods_exist_on_gui_class(self):
        for name in (
                "_scan_single_jar",
                "_analyze_task",
                "_analyze_task_impl",
                "_coverage_task",
                "_coverage_task_server",
                "extract_all_unique_strings",
                "batch_translate_missing",
                "_translate_task",
                "_verify_translations",
                "_write_failed_items_report",
                "_generate_jar_patches",
                "_generate_class_patch_jars",
                "process_text_file",
                "process_md_file"):
            self.assertTrue(callable(getattr(ModTranslatorApp, name)))


if __name__ == "__main__":
    unittest.main()
