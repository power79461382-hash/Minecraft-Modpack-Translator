"""format_mask 模組單元測試。

測試 mask_format / unmask_format 的遮罩與還原，
以及 fix_placeholders 的佔位符修復和 repair_patchouli_macros。
"""
import re
import unittest

from core.format_mask import (
    mask_format,
    unmask_format,
    fix_placeholders,
    repair_patchouli_macros,
)

# 與 main_window.py._RE_FORMAT 一致的測試用正則
FORMAT_RE = re.compile(
    r'§[0-9a-fk-or]'
    r'|&[0-9a-fk-or]'
    r'|%(?:\d+\$)?[-+]?[\d.]*[a-zA-Z]'
    r'|\$\([^)\r\n]{0,240}\)'
)


class MaskFormatTests(unittest.TestCase):
    def test_basic_mask_and_unmask(self):
        text = "Hello §aWorld§r %s"
        masked, mapping = mask_format(text, FORMAT_RE)
        # 遮罩後不含原始格式碼
        self.assertNotIn("§a", masked)
        self.assertNotIn("§r", masked)
        self.assertNotIn("%s", masked)
        # 佔位符數量正確
        self.assertEqual(len(mapping), 3)
        # 還原後與原文一致
        restored = unmask_format(masked, mapping)
        self.assertEqual(restored, text)

    def test_empty_text(self):
        masked, mapping = mask_format("", FORMAT_RE)
        self.assertEqual(masked, "")
        self.assertEqual(mapping, {})

    def test_none_text(self):
        masked, mapping = mask_format(None, FORMAT_RE)
        self.assertIsNone(masked)
        self.assertEqual(mapping, {})

    def test_no_format_codes(self):
        text = "Just plain text"
        masked, mapping = mask_format(text, FORMAT_RE)
        self.assertEqual(masked, text)
        self.assertEqual(mapping, {})

    def test_unique_markers(self):
        """同一格式碼出現多次時，每個佔位符應唯一。"""
        text = "§aA§aB§aC"
        masked, mapping = mask_format(text, FORMAT_RE)
        self.assertEqual(len(mapping), 3)
        # 所有佔位符 key 不重複
        self.assertEqual(len(set(mapping.keys())), 3)

    def test_unmask_tolerates_whitespace(self):
        """AI 翻譯可能在佔位符內插入空白，unmask 應容錯。"""
        text = "Hello §aWorld"
        masked, mapping = mask_format(text, FORMAT_RE)
        # 翻譯引擎可能在每個符號與 token 字元間插入空白。
        for marker in mapping:
            tampered = " ".join(marker)
            tampered_text = masked.replace(marker, tampered)
            restored = unmask_format(tampered_text, mapping)
            self.assertEqual(restored, text)

    def test_marker_uses_canonical_single_terminator(self):
        masked, mapping = mask_format("Hello §aWorld", FORMAT_RE)

        marker = next(iter(mapping))
        self.assertRegex(marker, r"^\[@F\d+@[0-9a-f]+@\]$")
        self.assertIn(marker, masked)


class FixPlaceholdersTests(unittest.TestCase):
    def test_fullwidth_percent(self):
        text = "％s 和 ％d"
        fixed = fix_placeholders(text)
        self.assertEqual(fixed, "%s 和 %d")

    def test_space_in_printf(self):
        text = "% 1 $ s 個"
        fixed = fix_placeholders(text)
        self.assertEqual(fixed, "%1$s 個")

    def test_space_in_format_letter(self):
        text = "使用 % s 個"
        fixed = fix_placeholders(text)
        self.assertEqual(fixed, "使用 %s 個")

    def test_space_in_color_code(self):
        text = "§ a 文字"
        fixed = fix_placeholders(text)
        self.assertEqual(fixed, "§a 文字")

    def test_newline_fix(self):
        text = "行一\\ n行二"
        fixed = fix_placeholders(text)
        self.assertIn("\\n", fixed)

    def test_non_string_passthrough(self):
        self.assertIsNone(fix_placeholders(None))
        self.assertEqual(fix_placeholders(42), 42)


class RepairPatchouliTests(unittest.TestCase):
    def test_fullwidth_paren(self):
        text = "文字$（arg）"
        fixed = repair_patchouli_macros(text)
        self.assertEqual(fixed, "文字$(arg)")

    def test_mixed_normal_parentheses_do_not_become_macro(self):
        text = "Price $5, use (example）"

        fixed = repair_patchouli_macros(text)

        self.assertEqual(fixed, text)

    def test_fullwidth_tilde_empty_macro_is_repaired(self):
        self.assertEqual(repair_patchouli_macros("文字！～（）"), "文字$()")

    def test_spaced_tilde_empty_macro_is_repaired(self):
        self.assertEqual(repair_patchouli_macros("文字|~ （）"), "文字$()")

    def test_no_macro_passthrough(self):
        text = "沒有任何巨集的文字"
        self.assertEqual(repair_patchouli_macros(text), text)

    def test_empty_macro(self):
        text = "文字$ （）"
        fixed = repair_patchouli_macros(text)
        self.assertIn("$()", fixed)


if __name__ == "__main__":
    unittest.main()
