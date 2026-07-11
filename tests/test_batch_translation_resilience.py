import re

import pytest

import core.batch_translation as batch_module


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Root:
    def after(self, _delay, callback):
        callback()


class Cache(dict):
    def bulk_update(self, items, sync=True):
        pairs = list(items)
        self.update(pairs)
        return len(pairs)


class SelectiveCache(Cache):
    def bulk_update(self, items, sync=True):
        pairs = list(items)
        accepted = [pair for pair in pairs if pair[0] == "source-0"]
        self.update(accepted)
        return len(accepted)


class FakeApp:
    _RE_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")

    def __init__(self, workers=2, engine="non_ai_chain"):
        self.stop_requested = False
        self.root = Root()
        self.engine_var = Value(engine)
        self.workers_var = Value(workers)
        self.api_key_var = Value("")
        self.deepl_key_var = Value("")
        self.azure_key_var = Value("")
        self.azure_region_var = Value("eastasia")
        self.azure_endpoint_var = Value("")
        self.claude_key_var = Value("")
        self.claude_model_var = Value("")
        self.openai_key_var = Value("test-key" if engine == "openai" else "")
        self.openai_model_var = Value("")
        self.local_url_var = Value("")
        self.ai_auth_mode_var = Value("api")
        self.ai_model_var = Value("fake-model")
        self.ai_base_url_var = Value("")
        self.auto_normalize_endpoint_var = Value(False)
        self.translation_cache = Cache()
        self.progress = []
        self.translated = 0
        self.skipped = 0
        self.errors = 0
        self.logs = []

    @staticmethod
    def _normalize_engine_route_value(engine):
        return engine

    @staticmethod
    def _ai_provider_key():
        return "custom"

    @staticmethod
    def _ai_provider_config():
        return {"models": ("fake-model",), "requires_key": False}

    @staticmethod
    def _ai_api_key_list():
        return []

    @staticmethod
    def _make_translation_chunks(strings, _engine, _primary, _model, force_size):
        size = max(1, int(force_size or 1))
        return [strings[index:index + size]
                for index in range(0, len(strings), size)]

    @staticmethod
    def _mask_format(text):
        return text, {}

    @staticmethod
    def _unmask_format(text, _mapping):
        return text

    @staticmethod
    def validate_translation(_source, translated):
        return translated

    @staticmethod
    def _apply_dictionary_fixes(_source, translated):
        return translated

    @staticmethod
    def fix_placeholders(text):
        return text

    @staticmethod
    def _maybe_save_cache():
        return None

    @staticmethod
    def _shutdown_executor_now(executor):
        executor.shutdown(wait=False, cancel_futures=True)

    def log(self, message):
        self.logs.append(message)

    def update_progress(self, current, total, text_mode=False):
        self.progress.append((current, total, text_mode))

    @staticmethod
    def set_current_item(_message):
        return None

    def _add_progress_counts(self, translated=0, skipped=0, errors=0):
        self.translated += translated
        self.skipped += skipped
        self.errors += errors


def test_failed_first_wave_still_drains_all_chunks(monkeypatch):
    app = FakeApp(workers=2)

    def disabled(_chunk):
        return None, "DISABLED:test provider unavailable"

    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "bing": disabled,
            "azure": disabled,
            "gtx": disabled,
        },
    )

    batch_module.batch_translate_missing(
        app, [f"source-{index}" for index in range(5)], _force_chunk_size=1)

    assert app.progress[-1][:2] == (5, 5)
    assert app.translated == 0
    assert app.skipped == 5
    assert app.errors == 5


def test_exceptional_first_wave_still_drains_all_chunks(monkeypatch):
    app = FakeApp(workers=2)
    calls = []

    def crashing(chunk):
        calls.append(list(chunk))
        raise RuntimeError("provider crashed")

    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {"bing": crashing},
    )

    batch_module.batch_translate_missing(
        app, [f"source-{index}" for index in range(5)], _force_chunk_size=1)

    assert len(calls) == 5
    assert app.progress[-1][:2] == (5, 5)
    assert app.skipped == 5
    assert app.errors == 5


def test_incomplete_primary_result_fails_over(monkeypatch):
    app = FakeApp(workers=1)
    calls = []

    def incomplete(chunk):
        calls.append(("bing", list(chunk)))
        return [None] * len(chunk), None

    def backup(chunk):
        calls.append(("azure", list(chunk)))
        return [f"譯文:{item}" for item in chunk], None

    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "bing": incomplete,
            "azure": backup,
            "gtx": backup,
        },
    )

    batch_module.batch_translate_missing(app, ["source"], _force_chunk_size=1)

    assert calls == [("bing", ["source"]), ("azure", ["source"])]
    assert app.translation_cache == {"source": "譯文:source"}
    assert app.translated == 1
    assert app.skipped == 0
    assert app.errors == 0


@pytest.mark.parametrize(
    "result",
    [None, [], [None], [""], ["   "], [42], ["one", "two"]],
)
def test_translation_result_completeness_rejects_invalid_shapes(result):
    assert not batch_module._translation_result_is_complete(["source"], result)


def test_incomplete_split_retry_activates_paid_backup(monkeypatch):
    app = FakeApp(workers=1, engine="openai")
    calls = []

    def incomplete_after_timeout(chunk):
        calls.append(("openai", list(chunk)))
        if len(chunk) > 1:
            return None, "ERR:timeout"
        return [None], None

    def backup(chunk):
        calls.append(("bing", list(chunk)))
        return [f"譯文:{item}" for item in chunk], None

    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "openai": incomplete_after_timeout,
            "bing": backup,
        },
    )

    batch_module.batch_translate_missing(
        app, ["source-0", "source-1"], _force_chunk_size=2)

    assert calls == [
        ("openai", ["source-0", "source-1"]),
        ("openai", ["source-0"]),
        ("bing", ["source-0", "source-1"]),
    ]
    assert app.translation_cache == {
        "source-0": "譯文:source-0",
        "source-1": "譯文:source-1",
    }


def test_session_keys_include_only_pairs_accepted_by_cache(monkeypatch):
    app = FakeApp(workers=1)
    app.translation_cache = SelectiveCache()

    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "bing": lambda chunk: ([f"translated:{item}" for item in chunk], None),
        },
    )

    batch_module.batch_translate_missing(
        app, ["source-0", "source-1"], _force_chunk_size=2)

    assert app.translation_cache == {"source-0": "translated:source-0"}
    assert app._session_translated_keys == {"source-0"}
    assert app.translated == 1
    assert app.skipped == 1
