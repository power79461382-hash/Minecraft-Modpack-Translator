"""translate_cli 專用測試。

測試 parse_args 參數解析和 apply_filters 過濾邏輯，
不涉及實際 Tkinter 啟動或翻譯執行。
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# 確保專案根目錄在 sys.path
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import translate_cli
from translate_cli import parse_args, apply_filters, clear_previous_analysis, CliModTranslatorApp


class ParseArgsTests(unittest.TestCase):
    def test_minimal_args(self):
        with patch.object(sys, 'argv', ['translate_cli.py', '--modpack', '/tmp/mc']):
            args = parse_args()
        self.assertEqual(args.modpack, '/tmp/mc')
        self.assertEqual(args.output_mode, 'jar_patch')
        self.assertEqual(args.name, 'Auto_Translated_Mods_zh_tw.zip')
        self.assertFalse(args.dry_run)

    def test_dry_run_flag(self):
        with patch.object(sys, 'argv', [
            'translate_cli.py', '--modpack', '/tmp/mc', '--dry-run'
        ]):
            args = parse_args()
        self.assertTrue(args.dry_run)

    def test_engine_choice(self):
        with patch.object(sys, 'argv', [
            'translate_cli.py', '--modpack', '/tmp/mc',
            '--engine', 'openai', '--model', 'gpt-4o'
        ]):
            args = parse_args()
        self.assertEqual(args.engine, 'openai')
        self.assertEqual(args.model, 'gpt-4o')

    def test_output_mode_choices(self):
        for mode in ('resource_pack', 'hybrid', 'jar_patch'):
            with patch.object(sys, 'argv', [
                'translate_cli.py', '--modpack', '/tmp/mc',
                '--output-mode', mode
            ]):
                args = parse_args()
                self.assertEqual(args.output_mode, mode)

    def test_invalid_engine_exits(self):
        with patch.object(sys, 'argv', [
            'translate_cli.py', '--modpack', '/tmp/mc',
            '--engine', 'invalid_engine'
        ]):
            with self.assertRaises(SystemExit):
                parse_args()


class ApplyFiltersTests(unittest.TestCase):
    """測試 apply_filters 的過濾邏輯，用 mock app。"""

    def _make_mock_app(self):
        app = MagicMock()
        app.analyzed_jars = {
            "jar1.jar": {"path/a.json": {}, "path/b.json": {}, "path/c.json": {}}
        }
        app.analyzed_loose = ["/tmp/quest1.json", "/tmp/book1.txt", "/tmp/lang1.lang"]
        app.analyzed_extra = [
            ("type", "/tmp/quest2.json", {}),
            ("type", "/tmp/lang2.lang", {}),
        ]
        app.analyzed_book_texts = {}
        app.analyzed_book_text_repairs = {}
        app.analyzed_static_assets = {}
        app.analyzed_loose_base = []
        app.analyzed_jars_zh_base = {}
        app._reset_progress_counters = MagicMock()
        app._is_quest_path = MagicMock(side_effect=lambda p: 'quest' in p)
        app._is_book_path = MagicMock(side_effect=lambda p: 'book' in p)
        return app

    def test_clear_previous_analysis(self):
        app = self._make_mock_app()
        clear_previous_analysis(app)
        self.assertEqual(len(app.analyzed_jars), 0)
        self.assertEqual(len(app.analyzed_loose), 0)
        self.assertEqual(len(app.analyzed_extra), 0)

    def test_skip_mods_keeps_quests_and_books(self):
        app = self._make_mock_app()
        args = MagicMock(skip_mods=True, skip_quests=False, max_steps=-1)
        apply_filters(app, args)
        # skip_mods 清空 analyzed_jars，保留 quest/book
        self.assertEqual(len(app.analyzed_jars), 0)
        # loose 只保留 quest/book
        for p in app.analyzed_loose:
            self.assertTrue('quest' in p or 'book' in p)

    def test_skip_quests_removes_quests(self):
        app = self._make_mock_app()
        args = MagicMock(skip_mods=False, skip_quests=True, max_steps=-1)
        apply_filters(app, args)
        # extra 中不含 quest
        for item in app.analyzed_extra:
            self.assertNotIn('quest', item[1])
        # loose 中不含 quest
        for p in app.analyzed_loose:
            self.assertNotIn('quest', p)

    def test_max_steps_limits_jar_files(self):
        app = self._make_mock_app()
        args = MagicMock(skip_mods=False, skip_quests=False, max_steps=2)
        apply_filters(app, args)
        total_jar_entries = sum(len(v) for v in app.analyzed_jars.values())
        self.assertLessEqual(total_jar_entries, 2)


class CliModTranslatorAppTests(unittest.TestCase):
    """測試 CliModTranslatorApp 的 log 方法輸出到 stdout。"""

    def test_log_prints_to_stdout(self):
        # 不實例化整個 Tkinter app，只測 log 方法邏輯
        # CliModTranslatorApp.log 繼承自 ModTranslatorApp 但 override 了 print
        # 用 __new__ 繞過 __init__
        app = CliModTranslatorApp.__new__(CliModTranslatorApp)
        # 直接呼叫 log，它只用 sys.stdout
        import io
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            app.log("測試訊息")
        finally:
            sys.stdout = old_stdout
        self.assertIn("測試訊息", captured.getvalue())


class MainTests(unittest.TestCase):
    def test_main_sanitizes_output_name_before_configuration_and_translation(self):
        class FakeVar:
            def __init__(self, value=None):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class FakeRoot:
            def withdraw(self):
                pass

            def update(self):
                pass

            def destroy(self):
                pass

        instances = []

        class FakeApp:
            def __init__(self, _root):
                instances.append(self)
                self.mod_dir_var = FakeVar()
                self.rp_dir_var = FakeVar()
                self.rp_name_var = FakeVar()
                self.output_mode_var = FakeVar()
                self.retry_count_var = FakeVar()
                self.engine_var = FakeVar()
                self.ai_provider_var = FakeVar()
                self.ai_model_var = FakeVar()
                self.ai_base_url_var = FakeVar()
                self.ai_api_key_var = FakeVar()
                self.pack_format_var = FakeVar(15)
                self.analyzed_jars = {}
                self.analyzed_book_texts = {}
                self.analyzed_book_text_repairs = {}
                self.analyzed_static_assets = {}
                self.analyzed_loose = []
                self.analyzed_loose_base = {}
                self.analyzed_extra = []
                self.analyzed_jars_zh_base = {}
                self.translated_name = None

            def _reset_progress_counters(self):
                pass

            def _analyze_task(self, _modpack):
                pass

            def extract_all_unique_strings(self):
                return set()

            def _translate_task(self, _output_dir, name, _pack_format, _output_mode):
                self.translated_name = name

        with tempfile.TemporaryDirectory() as temp_dir:
            modpack_dir = os.path.join(temp_dir, "modpack")
            output_dir = os.path.join(temp_dir, "output")
            os.mkdir(modpack_dir)
            os.mkdir(output_dir)
            default_name = "Auto_Translated_Mods_zh_tw.zip"
            for raw_name in (r"..\victim.zip", "custom-pack.zip", default_name):
                with self.subTest(raw_name=raw_name):
                    instances.clear()
                    args = SimpleNamespace(
                        modpack=modpack_dir,
                        output_dir=output_dir,
                        name=raw_name,
                        output_mode="resource_pack",
                        dry_run=False,
                        skip_mods=False,
                        skip_quests=False,
                        max_steps=-1,
                        retry=None,
                        engine=None,
                        provider=None,
                        model=None,
                        base_url=None,
                        api_key=None,
                    )

                    with patch.object(
                            translate_cli.ModTranslatorApp, "_safe_zip_filename",
                            wraps=translate_cli.ModTranslatorApp._safe_zip_filename) as sanitize, \
                            patch.object(translate_cli, "parse_args", return_value=args), \
                            patch.object(translate_cli.tk, "Tk", return_value=FakeRoot()), \
                            patch.object(translate_cli, "CliModTranslatorApp", FakeApp):
                        result = translate_cli.main()

                    sanitize.assert_called_once_with(
                        raw_name, "Auto_Translated_Mods_zh_tw")
                    app = instances[0]
                    expected_name = CliModTranslatorApp._safe_zip_filename(
                        raw_name, "Auto_Translated_Mods_zh_tw")
                    resolved_output = os.path.abspath(
                        os.path.join(output_dir, app.translated_name))

                    self.assertEqual(result, 0)
                    self.assertEqual(app.rp_name_var.get(), expected_name)
                    self.assertEqual(app.translated_name, expected_name)
                    self.assertEqual(os.path.basename(expected_name), expected_name)
                    self.assertTrue(expected_name.endswith(".zip"))
                    self.assertEqual(
                        os.path.commonpath([output_dir, resolved_output]), output_dir)


if __name__ == "__main__":
    unittest.main()
