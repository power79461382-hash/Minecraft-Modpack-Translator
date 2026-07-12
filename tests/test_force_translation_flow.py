import re

import pytest

from core.translation_flow import (
    POST_TRANSLATION_RESCUE_LIMIT,
    run_translate_task,
)
from gui.main_window import ModTranslatorApp


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class Root:
    def after(self, _delay, callback):
        callback()


class FlowApp:
    C_SUCCESS = "success"
    C_WARN = "warn"
    C_ERROR = "error"
    C_DANGER = "danger"
    _RE_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")
    _RE_FORMAT = re.compile(r"(?!x)x")

    def __init__(self, work_dir, mode, unique_strings, cache=None, memory=None,
                 retry_count=0, mark_session=False):
        self.analyzed_mc_dir = str(work_dir)
        self._analysis_unique_strings = set(unique_strings)
        self._initial_cache = dict(cache or {})
        self._memory = dict(memory or {})
        self.process_mode_var = Value(mode)
        self.global_memory_var = Value(True)
        self.retry_count_var = Value(retry_count)
        self.engine_var = Value("openai")
        self.stop_requested = False
        self.pause_requested = False
        self.is_processing = True
        self.root = Root()
        self.btn_analyze = None
        self.btn_translate = None
        self.btn_stop = None
        self.btn_pause = None
        self.batch_calls = []
        self.package_calls = []
        self.class_patch_calls = []
        self.mark_session = mark_session

    def load_cache(self):
        return dict(self._initial_cache)

    def _load_translation_memory(self):
        return dict(self._memory)

    def batch_translate_missing(self, strings, _force_chunk_size=None,
                                _preferred_engine=None):
        items = list(strings)
        self.batch_calls.append(items)
        if self.mark_session:
            self._session_translated_keys.update(items)

    @staticmethod
    def save_cache(*_args, **_kwargs):
        return None

    @staticmethod
    def _normalize_engine_route_value(value):
        return value

    def _cache_has_usable_translation(self, source):
        value = self.translation_cache.get(source)
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def should_translate(_source):
        return True

    @staticmethod
    def _find_mixed_translations(_strings):
        return []

    @staticmethod
    def _review_and_fix_cache():
        return None

    @staticmethod
    def _verify_translations(_strings):
        return True

    def _generate_jar_patches(self, rp_dir, rp_name, mc_dir):
        self.package_calls.append((rp_dir, rp_name, mc_dir))
        return str(rp_dir / "stub-output.zip")

    def _generate_class_patch_jars(self, rp_dir, rp_name, mc_dir):
        self.class_patch_calls.append((rp_dir, rp_name, mc_dir))
        return str(rp_dir / "stub-class-patch.zip")

    @staticmethod
    def set_current_item(*_args, **_kwargs):
        return None

    @staticmethod
    def log(*_args, **_kwargs):
        return None

    @staticmethod
    def _set_summary_card(*_args, **_kwargs):
        return None

    @staticmethod
    def _refresh_api_summary(*_args, **_kwargs):
        return None

    @staticmethod
    def _set_btn_state(*_args, **_kwargs):
        return None

    @staticmethod
    def _note_progress_error():
        return None

    @staticmethod
    def _format_eta(seconds):
        return f"{seconds:.1f}s"


def run_flow(app, tmp_path):
    run_translate_task(
        app,
        tmp_path,
        "translations.zip",
        15,
        output_mode="jar_patch",
    )


def run_flow_for_output_mode(app, tmp_path, output_mode):
    app._server_mode = False
    app.analyzed_jars = {}
    app.analyzed_loose = []
    app.analyzed_book_texts = {}
    app.analyzed_book_text_repairs = {}
    app.analyzed_extra = []
    app.analyzed_zip_json = []
    app.datapack_name_var = Value("")
    app.datapack_output_var = Value(False)
    app.scope_mod_lang_var = Value(False)
    app._scope_allows_analyzed_path = lambda *_args: True
    app._scope_allows_extra = lambda *_args: True
    app.update_progress = lambda *_args, **_kwargs: None
    app._output_mode_summary = lambda: ("safe", "")
    app._safe_zip_filename = lambda name, default: (
        name if str(name).lower().endswith(".zip")
        else f"{name or default}.zip")
    app._maybe_install_resource_pack_to_instance = (
        lambda *_args, **_kwargs: None)
    run_translate_task(
        app,
        tmp_path,
        "translations.zip",
        15,
        output_mode=output_mode,
    )


def test_force_initial_batch_includes_old_cache_and_memory_hits(tmp_path):
    app = FlowApp(
        tmp_path,
        "force",
        {"cached source", "memory source"},
        cache={"cached source": "old cache translation"},
        memory={"memory source": "old memory translation"},
    )

    run_flow(app, tmp_path)

    assert app.batch_calls == [["cached source", "memory source"]]
    assert app.translation_cache == {
        "cached source": "old cache translation",
    }
    assert app._memory == {"memory source": "old memory translation"}
    assert app._analysis_cache_hits == 0
    assert app._analysis_memory_hits == 0
    assert app._analysis_missing_strings == 2
    assert len(app.package_calls) == 1


def test_force_retries_ignore_old_cache_until_retry_budget_is_exhausted(tmp_path):
    app = FlowApp(
        tmp_path,
        "force",
        {"cached source", "memory source"},
        cache={"cached source": "old cache translation"},
        memory={"memory source": "old memory translation"},
        retry_count=2,
    )

    run_flow(app, tmp_path)

    assert len(app.batch_calls) == 3
    assert all(
        set(call) == {"cached source", "memory source"}
        for call in app.batch_calls
    )
    assert app.translation_cache["cached source"] == "old cache translation"
    assert app._memory["memory source"] == "old memory translation"
    assert len(app.package_calls) == 1


def test_force_retries_stop_after_session_translation_is_recorded(tmp_path):
    app = FlowApp(
        tmp_path,
        "force",
        {"cached source"},
        cache={"cached source": "old cache translation"},
        retry_count=2,
        mark_session=True,
    )

    run_flow(app, tmp_path)

    assert app.batch_calls == [["cached source"]]
    assert app._session_translated_keys == {"cached source"}


def test_stop_requested_during_review_skips_verification_and_packaging(tmp_path):
    app = FlowApp(
        tmp_path,
        "append",
        {"cached source"},
        cache={"cached source": "快取譯文"},
    )
    verify_calls = []

    def review_and_stop():
        app.stop_requested = True

    def verify(strings):
        verify_calls.append(set(strings))
        return True

    app._review_and_fix_cache = review_and_stop
    app._verify_translations = verify

    run_flow(app, tmp_path)

    assert verify_calls == []
    assert app.package_calls == []


def test_stop_requested_during_verification_skips_packaging(tmp_path):
    app = FlowApp(
        tmp_path,
        "append",
        {"cached source"},
        cache={"cached source": "快取譯文"},
    )

    def verify_and_stop(_strings):
        app.stop_requested = True
        return True

    app._verify_translations = verify_and_stop

    run_flow(app, tmp_path)

    assert app.package_calls == []


def test_stop_requested_after_final_summary_skips_packaging(tmp_path):
    app = FlowApp(
        tmp_path,
        "append",
        {"cached source"},
        cache={"cached source": "快取譯文"},
    )

    def summary(_key, _value=None, sub=None, _color=None):
        if sub and "狀態：輸出中" in sub:
            app.stop_requested = True

    app._set_summary_card = summary

    run_flow(app, tmp_path)

    assert app.package_calls == []


def test_non_force_still_avoids_cache_and_memory_hits(tmp_path):
    app = FlowApp(
        tmp_path,
        "append",
        {"cached source", "memory source", "missing source"},
        cache={"cached source": "old cache translation"},
        memory={"memory source": "old memory translation"},
    )

    run_flow(app, tmp_path)

    assert app.batch_calls == [["missing source"]]
    assert app._analysis_cache_hits == 2
    assert app._analysis_memory_hits == 1
    assert app._analysis_missing_strings == 1


def test_fixed_hybrid_flow_generates_opted_in_class_jar_patch(tmp_path):
    app = FlowApp(
        tmp_path,
        "append",
        {"cached source"},
        cache={"cached source": "快取譯文"},
    )
    app._scan_class_tooltip_patch = True

    run_flow_for_output_mode(app, tmp_path, "hybrid")

    assert len(app.class_patch_calls) == 1


@pytest.mark.parametrize("output_mode", ["jar_patch", "resource_pack"])
def test_every_client_output_mode_removes_stale_class_patch_before_packaging(
        tmp_path, output_mode):
    app = FlowApp(
        tmp_path,
        "append",
        {"cached source"},
        cache={"cached source": "快取譯文"},
    )
    stale_patch = tmp_path / "translations_Class硬編碼補丁.zip"
    stale_patch.write_bytes(b"legacy executable class patch")

    run_flow_for_output_mode(app, tmp_path, output_mode)

    assert not stale_patch.exists()


@pytest.mark.parametrize("output_mode", ["jar_patch", "resource_pack"])
def test_stale_class_patch_removal_failure_blocks_every_client_output_mode(
        tmp_path, output_mode):
    app = FlowApp(
        tmp_path,
        "append",
        {"cached source"},
        cache={"cached source": "快取譯文"},
    )
    stale_patch = tmp_path / "translations_Class硬編碼補丁.zip"
    stale_patch.mkdir()

    try:
        run_flow_for_output_mode(app, tmp_path, output_mode)
    except (OSError, RuntimeError):
        pass

    assert stale_patch.is_dir()
    assert app.package_calls == []
    assert not (tmp_path / "translations.zip").exists()


def test_retry_zero_skips_all_post_translation_engine_rescue(tmp_path):
    app = FlowApp(
        tmp_path,
        "append",
        {"Needs rescue %s", "Mixed source"},
        cache={"Mixed source": "混合 Source"},
        retry_count=0,
    )
    app._RE_FORMAT = re.compile(r"%s")
    rescue_calls = []

    def retry_segment_mode(strings):
        rescue_calls.append(("segment", list(strings)))
        app.batch_translate_missing(["unexpected segment engine call"])
        return 0

    def find_mixed_translations(_strings):
        rescue_calls.append(("mixed-scan", []))
        return ["Mixed source"]

    def retranslate_mixed(strings):
        rescue_calls.append(("mixed", list(strings)))
        app.batch_translate_missing(["unexpected mixed engine call"])
        return 0

    app._retry_segment_mode = retry_segment_mode
    app._find_mixed_translations = find_mixed_translations
    app._retranslate_mixed = retranslate_mixed

    run_flow(app, tmp_path)

    assert rescue_calls == []
    assert app.batch_calls == [["Needs rescue %s"]]


def test_post_translation_rescue_candidates_share_a_bounded_budget(tmp_path):
    rescue_limit = 500
    segment_sources = {f"Segment rescue {index} %s" for index in range(800)}
    mixed_sources = [f"Mixed source {index}" for index in range(800)]
    app = FlowApp(
        tmp_path,
        "append",
        segment_sources,
        retry_count=1,
    )
    app._RE_FORMAT = re.compile(r"%s")
    rescue_candidates = []
    logs = []

    def retry_segment_mode(strings):
        rescue_candidates.extend(("segment", source) for source in strings)
        return 0

    def retranslate_mixed(strings):
        rescue_candidates.extend(("mixed", source) for source in strings)
        return 0

    app._retry_segment_mode = retry_segment_mode
    app._find_mixed_translations = lambda _strings: list(mixed_sources)
    app._retranslate_mixed = retranslate_mixed
    app.log = logs.append

    run_flow(app, tmp_path)

    assert 0 < len(rescue_candidates) <= rescue_limit
    assert any("上限" in message and "保留原文" in message for message in logs)


def test_memory_hit_formatted_strings_do_not_enter_post_translation_rescue(tmp_path):
    source = "Already translated %s"
    app = FlowApp(
        tmp_path,
        "append",
        {source},
        memory={source: "已翻譯 %s"},
        retry_count=1,
    )
    app._RE_FORMAT = re.compile(r"%s")
    rescued_sources = []

    def retry_segment_mode(strings):
        rescued_sources.extend(strings)
        return 0

    app._retry_segment_mode = retry_segment_mode

    run_flow(app, tmp_path)

    assert rescued_sources == []
    assert app.batch_calls == []


def test_mixed_rescue_candidates_are_sorted_before_budget_cap(tmp_path):
    mixed_sources = [
        f"Mixed source {index:04d}"
        for index in range(POST_TRANSLATION_RESCUE_LIMIT + 25)
    ]
    app = FlowApp(
        tmp_path,
        "append",
        set(mixed_sources),
        cache={source: "混合 Source" for source in mixed_sources},
        retry_count=1,
    )
    selected_sources = []
    app._find_mixed_translations = lambda _strings: list(reversed(mixed_sources))

    def retranslate_mixed(strings):
        selected_sources.extend(strings)
        return 0

    app._retranslate_mixed = retranslate_mixed

    run_flow(app, tmp_path)

    assert selected_sources == sorted(mixed_sources)[:POST_TRANSLATION_RESCUE_LIMIT]


def test_segment_rescue_caps_expanded_engine_items(tmp_path):
    app = FlowApp(tmp_path, "append", set())
    app.translation_cache = {}
    app._RE_FORMAT = re.compile(r"%s")
    app._RE_EN_WORD = re.compile(r"[A-Za-z]{4,}")
    app._split_by_format_tokens = ModTranslatorApp._split_by_format_tokens
    app.get_translation = lambda source: source
    engine_items = []

    def batch_translate_missing(strings, *_args, **_kwargs):
        engine_items.extend(strings)

    app.batch_translate_missing = batch_translate_missing
    heavy_sources = [
        f"Alpha{index} %s Beta{index} %s Gamma{index} %s Delta{index}"
        for index in range(200)
    ]

    ModTranslatorApp._retry_segment_mode(app, heavy_sources)

    assert 0 < len(engine_items) <= POST_TRANSLATION_RESCUE_LIMIT
