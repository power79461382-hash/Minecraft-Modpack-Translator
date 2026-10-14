import io
from types import SimpleNamespace

import pytest

import translate_cli


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Root:
    def __init__(self):
        self.destroyed = False

    def withdraw(self):
        pass

    def update(self):
        pass

    def destroy(self):
        self.destroyed = True


class Cache:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def cli_case(tmp_path, monkeypatch):
    args = SimpleNamespace(
        modpack=str(tmp_path), output_dir=str(tmp_path), name="pack.zip",
        output_mode="resource_pack", dry_run=False, skip_mods=False,
        skip_quests=False, max_steps=-1, retry=None, engine=None,
        provider=None, model=None, base_url=None, api_key=None,
    )
    root = Root()
    instances = []

    class App:
        scan_success = True
        translation_success = True
        unexpected_error = None

        def __init__(self, _root):
            instances.append(self)
            for name in ("mod_dir", "rp_dir", "rp_name", "output_mode", "retry_count",
                         "engine", "ai_provider", "ai_model", "ai_base_url", "ai_api_key",
                         "api_key", "deepl_key", "azure_key", "claude_key", "openai_key",
                         "claude_model", "openai_model", "local_url"):
                setattr(self, name + "_var", Value())
            self.pack_format_var = Value(15)
            for name in ("jars", "book_texts", "book_text_repairs", "static_assets",
                         "loose_base", "jars_zh_base"):
                setattr(self, "analyzed_" + name, {})
            self.analyzed_loose = []
            self.analyzed_extra = []
            self.translation_cache = Cache()
            self.translation_called = False

        def _reset_progress_counters(self):
            pass

        def _analyze_task(self, _path):
            self._last_analysis_succeeded = self.scan_success

        def extract_all_unique_strings(self):
            return set()

        def _translate_task(self, *_args):
            self.translation_called = True
            if self.unexpected_error:
                raise self.unexpected_error
            self._last_translation_succeeded = self.translation_success

    monkeypatch.setattr(translate_cli, "parse_args", lambda: args)
    monkeypatch.setattr(translate_cli.tk, "Tk", lambda: root)
    monkeypatch.setattr(translate_cli, "CliModTranslatorApp", App)
    return args, root, App, instances


def test_cli_scan_failure_returns_nonzero_without_translation(cli_case):
    _args, root, app_type, instances = cli_case
    app_type.scan_success = False
    assert translate_cli.main() == 1
    assert not instances[0].translation_called
    assert root.destroyed
    assert instances[0].translation_cache.closed


def test_cli_translation_failure_returns_nonzero(cli_case):
    _args, root, app_type, instances = cli_case
    app_type.translation_success = False
    assert translate_cli.main() == 1
    assert root.destroyed
    assert instances[0].translation_cache.closed


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_success_closes_cache_and_root(cli_case, dry_run):
    args, root, _app_type, instances = cli_case
    args.dry_run = dry_run
    assert translate_cli.main() == 0
    assert root.destroyed
    assert instances[0].translation_cache.closed
    assert instances[0].translation_called is not dry_run


def test_cli_exception_closes_resources_without_printing_secret(cli_case, capsys):
    _args, root, app_type, instances = cli_case
    app_type.unexpected_error = RuntimeError("synthetic-secret-provider-error")
    assert translate_cli.main() == 1
    assert root.destroyed
    assert instances[0].translation_cache.closed
    assert "synthetic-secret-provider-error" not in capsys.readouterr().err


def test_cli_filters_replace_analysis_snapshot_used_by_translation(cli_case, monkeypatch):
    _args, _root, app_type, instances = cli_case
    original_scan = app_type._analyze_task

    def scan_with_unfiltered_snapshot(self, path):
        original_scan(self, path)
        self._analysis_unique_strings = {"selected", "excluded by CLI filter"}

    monkeypatch.setattr(app_type, "_analyze_task", scan_with_unfiltered_snapshot)
    monkeypatch.setattr(app_type, "extract_all_unique_strings", lambda _self: {"selected"})
    assert translate_cli.main() == 0
    assert instances[0]._analysis_unique_strings == {"selected"}


@pytest.mark.parametrize("engine,key_field", [
    ("google", "api_key_var"), ("deepl", "deepl_key_var"),
    ("azure", "azure_key_var"), ("claude", "claude_key_var"),
    ("openai", "openai_key_var"), ("market_ai", "ai_api_key_var"),
])
def test_cli_key_override_reaches_selected_engine(cli_case, engine, key_field):
    args, _root, _app_type, instances = cli_case
    args.engine = engine
    args.api_key = "synthetic-cli-key"
    assert translate_cli.main() == 0
    assert getattr(instances[0], key_field).get() == "synthetic-cli-key"


@pytest.mark.parametrize("engine,model_field", [
    ("openai", "openai_model_var"), ("claude", "claude_model_var"),
    ("market_ai", "ai_model_var"),
])
def test_cli_model_override_reaches_selected_engine(cli_case, engine, model_field):
    args, _root, _app_type, instances = cli_case
    args.engine = engine
    args.model = "synthetic-cli-model"
    assert translate_cli.main() == 0
    assert getattr(instances[0], model_field).get() == "synthetic-cli-model"


def test_cli_local_endpoint_override_reaches_local_engine(cli_case):
    args, _root, _app_type, instances = cli_case
    args.engine = "local"
    args.base_url = "http://127.0.0.1:8000/v1/chat/completions"
    assert translate_cli.main() == 0
    assert instances[0].local_url_var.get() == "http://127.0.0.1:8000/v1/chat/completions"


def test_cli_missing_unicode_path_is_encoding_safe(tmp_path, monkeypatch):
    args = SimpleNamespace(modpack=str(tmp_path / "\u4e2d\u6587-missing"))
    monkeypatch.setattr(translate_cli, "parse_args", lambda: args)
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="ascii")
    monkeypatch.setattr(translate_cli.sys, "stderr", stream)
    assert translate_cli.main() == 2
    stream.flush()
    assert b"Modpack folder not found" in output.getvalue()


def test_max_steps_limits_books_classes_and_embedded_zip_targets():
    app = SimpleNamespace(
        analyzed_jars={"mod.jar": {"assets/mod/lang/en_us.json": {}}},
        analyzed_jars_zh_base={"mod.jar": {"assets/mod/lang/en_us.json": {}}},
        analyzed_book_texts={"mod.jar": {"book-one.txt": "one", "book-two.txt": "two"}},
        analyzed_book_text_repairs={}, analyzed_static_assets={},
        analyzed_class_texts={"mod.jar": {"Tooltip.class": ["tooltip"]}},
        analyzed_loose=[], analyzed_loose_base={}, analyzed_extra=[],
        analyzed_zip_json=[("embedded.zip", "description.json")],
    )
    args = SimpleNamespace(skip_mods=False, skip_quests=False, max_steps=1)
    translate_cli.apply_filters(app, args)
    assert sum(len(files) for files in app.analyzed_jars.values()) == 1
    assert app.analyzed_book_texts == {}
    assert app.analyzed_class_texts == {}
    assert app.analyzed_zip_json == []


def test_max_steps_can_select_book_target_without_unlimited_repair_outputs():
    app = SimpleNamespace(
        analyzed_jars={}, analyzed_jars_zh_base={},
        analyzed_book_texts={"mod.jar": {"book-one.txt": "one", "book-two.txt": "two"}},
        analyzed_book_text_repairs={"mod.jar": {"zh_tw/book-one.txt": "repair-one",
                                               "zh_tw/book-two.txt": "repair-two"}},
        analyzed_static_assets={}, analyzed_class_texts={},
        analyzed_loose=[], analyzed_loose_base={}, analyzed_extra=[],
        analyzed_zip_json=[],
    )
    args = SimpleNamespace(skip_mods=False, skip_quests=False, max_steps=1)
    translate_cli.apply_filters(app, args)
    assert app.analyzed_book_texts == {"mod.jar": {"book-one.txt": "one"}}
    assert app.analyzed_book_text_repairs == {}
