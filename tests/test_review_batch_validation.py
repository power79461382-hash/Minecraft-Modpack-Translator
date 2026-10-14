import re

import pytest

import core.batch_translation as batch_module
from core.translation_flow import run_translate_task
from core.verification import verify_translations
from test_batch_translation_resilience import FakeApp
from test_force_translation_flow import FlowApp, run_flow, run_flow_for_output_mode
from test_resource_pack_atomicity import ResourcePackApp


@pytest.mark.parametrize("malformed", [[42], {"0": "譯文"}, "譯文", ["譯文", "多餘項目"]])
def test_malformed_passthrough_fallback_preserves_valid_primary_rows(monkeypatch, malformed):
    app = FakeApp(workers=1)
    monkeypatch.setattr(
        batch_module, "build_provider_registry",
        lambda _session, _settings: {
            "gtx": lambda _chunk: (["translated:first", "second"], None),
            "mymemory": lambda _chunk: (malformed, None),
        },
    )

    batch_module.batch_translate_missing(app, ["first", "second"], _force_chunk_size=2)

    assert app.translation_cache == {"first": "translated:first"}
    assert app.translated == 1
    assert app.skipped == 1
    assert app.errors == 0


class ValidatedFlowApp(FlowApp):
    @staticmethod
    def _is_valid_trad_translation(source, translated):
        return isinstance(translated, str) and translated != source and bool(
            re.search(r"[\u3400-\u9fff]", translated))


@pytest.mark.parametrize("storage", ["cache", "memory"])
def test_unusable_saved_translation_does_not_skip_initial_batch(tmp_path, storage):
    app = ValidatedFlowApp(
        tmp_path, "append", {"Source"},
        **{storage: {"Source": "Source"}},
    )

    run_flow(app, tmp_path)

    assert app.batch_calls == [["Source"]]
    assert app._analysis_missing_strings == 1
    assert app._analysis_cache_hits == 0
    assert app.package_calls == []


class FormatVerificationApp:
    _RE_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")

    def __init__(self, cache, fallback=None):
        self.translation_cache = cache
        self.fallback = fallback
        self.reports = []

    @staticmethod
    def log(_message):
        pass

    @staticmethod
    def _is_valid_trad_translation(source, translated):
        return isinstance(translated, str) and translated != source and bool(
            re.search(r"[\u3400-\u9fff]", translated))

    @staticmethod
    def should_translate(_source):
        return True

    def get_translation(self, source):
        return self.fallback if self.fallback is not None else source

    @staticmethod
    def _critical_format_tokens(text):
        return re.findall(r"%\d*\$?[sd]|\$\([^)]*\)", text)

    def _write_failed_items_report(self, untranslated, fmt_issues):
        self.reports.append((untranslated, fmt_issues))
        return ""


def test_verification_rejects_format_tokens_added_to_plain_source():
    app = FormatVerificationApp({"Plain": "文字 %s"})

    assert verify_translations(app, ["Plain"]) is False
    assert app.reports == [([], [("Plain", "文字 %s")])]


def test_verification_checks_resolved_fallback_translation_formats():
    app = FormatVerificationApp({}, fallback="文字 $(l:broken)")

    assert verify_translations(app, ["Plain"]) is False
    assert app.reports == [([], [("Plain", "文字 $(l:broken)")])]


def test_successful_packaging_reports_translation_success(tmp_path):
    app = FlowApp(tmp_path, "append", {"Source"}, cache={"Source": "譯文"})

    run_flow(app, tmp_path)

    assert app._last_translation_succeeded is True


@pytest.mark.parametrize("mode", ["cancel", "exception"])
def test_failed_packaging_resets_previous_translation_success(tmp_path, mode):
    instance = tmp_path / "instance"
    (instance / "mods").mkdir(parents=True)
    app = ResourcePackApp(instance, mode)
    app._last_translation_succeeded = True

    run_translate_task(app, str(tmp_path), "translations.zip", 9, "resource_pack")

    assert app._last_translation_succeeded is False
    assert not (tmp_path / "translations.zip").exists()


def test_cancellation_during_hybrid_class_output_is_not_reported_as_success(tmp_path):
    app = FlowApp(tmp_path, "append", {"Source"}, cache={"Source": "譯文"})
    app._scan_class_tooltip_patch = True

    def cancel_class_output(*_args):
        app.stop_requested = True
        app.pause_requested = True
        return ""

    app._generate_class_patch_jars = cancel_class_output
    run_flow_for_output_mode(app, tmp_path, "hybrid")

    assert app._last_translation_succeeded is False
    assert (tmp_path / "translations.zip").exists()


def test_resource_pack_success_reports_translation_success(tmp_path):
    instance = tmp_path / "instance"
    (instance / "mods").mkdir(parents=True)
    app = ResourcePackApp(instance, "success")
    app._process_advancement_json = lambda _data: {"display": {"title": "根節點"}}

    run_translate_task(app, str(tmp_path), "translations.zip", 9, "resource_pack")

    assert app._last_translation_succeeded is True
    assert (tmp_path / "translations.zip").exists()
    assert (tmp_path / "translations_Datapack.zip").exists()
