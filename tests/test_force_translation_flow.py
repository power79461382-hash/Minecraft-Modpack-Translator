import re

from core.translation_flow import run_translate_task


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
