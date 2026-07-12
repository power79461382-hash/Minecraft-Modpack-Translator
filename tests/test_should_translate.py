"""should_translate 方法單元測試。

should_translate 依賴大量 class-level 正則和 _looks_like_structural_reference
classmethod，不依賴任何 instance 屬性，因此用 __new__ 繞過 __init__（Tkinter GUI
初始化）即可測試。
"""
import unittest

from gui.main_window import ModTranslatorApp


def _make_app():
    """建立一個繞過 __init__ 的 ModTranslatorApp 實例，僅用於測試 class 方法。"""
    return ModTranslatorApp.__new__(ModTranslatorApp)


class ShouldTranslateTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()

    # ── 應翻譯（回傳 True）──
    def test_plain_english(self):
        self.assertTrue(self.app.should_translate("Hello World"))

    def test_sentence_with_format(self):
        self.assertTrue(self.app.should_translate("Welcome to the %s dimension!"))

    def test_mixed_cjk_and_english(self):
        self.assertTrue(self.app.should_translate("獲得 Brightsteel 板"))

    def test_single_word_long_enough(self):
        self.assertTrue(self.app.should_translate("Diamond"))

    # ── 不應翻譯（回傳 False）──
    def test_empty_string(self):
        self.assertFalse(self.app.should_translate(""))

    def test_single_char(self):
        self.assertFalse(self.app.should_translate("A"))

    def test_non_string(self):
        self.assertFalse(self.app.should_translate(42))
        self.assertFalse(self.app.should_translate(None))

    def test_boolean_keywords(self):
        for kw in ('true', 'false', 'null', 'none', 'default'):
            self.assertFalse(self.app.should_translate(kw), f"should skip: {kw}")

    def test_pure_number(self):
        self.assertFalse(self.app.should_translate("12345"))

    def test_hex_color(self):
        self.assertFalse(self.app.should_translate("#FF5733"))
        self.assertFalse(self.app.should_translate("FF5733"))

    def test_namespace_id(self):
        self.assertFalse(self.app.should_translate("minecraft:diamond"))

    def test_lang_key_ref(self):
        self.assertFalse(self.app.should_translate("advancement.story.mineDiamond"))

    def test_filepath(self):
        self.assertFalse(self.app.should_translate("textures/blocks/diamond.png"))

    def test_snake_key(self):
        self.assertFalse(self.app.should_translate("has_diamond_block_slab"))

    def test_already_chinese(self):
        self.assertFalse(self.app.should_translate("這是一段已經翻好的中文"))

    def test_locale_code(self):
        self.assertFalse(self.app.should_translate("en_us"))
        self.assertFalse(self.app.should_translate("zh_tw"))

    def test_key_chord(self):
        self.assertFalse(self.app.should_translate("Ctrl + A"))
        self.assertFalse(self.app.should_translate("[F4]"))

    def test_all_caps(self):
        self.assertFalse(self.app.should_translate("ASCII_ONLY"))

    def test_mod_condition(self):
        self.assertFalse(self.app.should_translate("or(mod(some_mod))"))

    def test_url(self):
        self.assertFalse(self.app.should_translate("https://example.com/page"))

    def test_pure_format_code(self):
        self.assertFalse(self.app.should_translate("§d§l§9"))

    def test_patchouli_template_control_tokens(self):
        for token in (
                "#recipe", "#heading", "#text", "#image", "#item",
                "#link", "#anchor", "#tier#", "#mana_cost#", "#school#",
                "#output", "#reagent", "#footer", "#page_title"):
            self.assertFalse(
                self.app.should_translate(token),
                f"Patchouli control token must remain structural: {token}")

    def test_patchouli_control_token_ignores_broken_cached_translation(self):
        self.app.stop_requested = False
        self.app.translation_cache = {
            "#recipe": "#食譜",
            "#mana_cost#": "#法力消耗#",
        }

        output = self.app.process_json_data({
            "recipe_name": "#recipe",
            "text": "#mana_cost#",
        })

        self.assertEqual(output, {
            "recipe_name": "#recipe",
            "text": "#mana_cost#",
        })


if __name__ == "__main__":
    unittest.main()
