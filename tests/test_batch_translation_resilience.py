import re
import threading
import time

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


def test_backup_route_does_not_stampede_azure(monkeypatch):
    app = FakeApp(workers=16, engine="openai")
    app.azure_key_var = Value("test-key")
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def disabled(_chunk):
        return None, "DISABLED:test provider unavailable"

    def azure(chunk):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.03)
            return [f"譯文:{item}" for item in chunk], None
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "openai": disabled,
            "bing": disabled,
            "azure": azure,
            "gtx": lambda chunk: ([f"GTX:{item}" for item in chunk], None),
        },
    )

    batch_module.batch_translate_missing(
        app, [f"source-{index}" for index in range(16)], _force_chunk_size=1)

    assert max_active == 5
    assert len(app.translation_cache) == 16


def test_non_ai_chain_has_short_tail_wait_budget():
    assert batch_module.translation_wait_budget("non_ai_chain", False) <= 60
    assert batch_module.translation_wait_budget("openai", True) == 300


def test_paid_azure_timeouts_activate_backup_after_three_failures(monkeypatch):
    app = FakeApp(workers=1, engine="azure")
    app.azure_key_var = Value("test-key")
    calls = []
    now = [1000.0]

    def azure_timeout(chunk):
        calls.append(("azure", list(chunk)))
        return None, "ERR:Azure 連線失敗: timed out"

    def bing_backup(chunk):
        calls.append(("bing", list(chunk)))
        return [f"譯文:{item}" for item in chunk], None

    def advance_clock(_should_stop, seconds, quantum=0.2):
        del quantum
        now[0] += seconds
        return True

    monkeypatch.setattr(batch_module.time, "time", lambda: now[0])
    monkeypatch.setattr(batch_module, "interruptible_sleep", advance_clock)
    monkeypatch.setattr(batch_module.random, "uniform", lambda _lo, _hi: 0.0)
    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "azure": azure_timeout,
            "bing": bing_backup,
            "gtx": bing_backup,
        },
    )

    batch_module.batch_translate_missing(app, ["source"], _force_chunk_size=1)

    assert calls == [
        ("azure", ["source"]),
        ("azure", ["source"]),
        ("azure", ["source"]),
        ("bing", ["source"]),
    ]
    assert app.translation_cache == {"source": "譯文:source"}
    assert any("已啟用備援" in message for message in app.logs)


def test_paid_azure_transient_circuit_recovers_after_cooldown(monkeypatch):
    app = FakeApp(workers=1, engine="azure")
    app.azure_key_var = Value("test-key")
    calls = []
    azure_calls = 0
    now = [1000.0]

    def azure(chunk):
        nonlocal azure_calls
        azure_calls += 1
        calls.append(("azure", list(chunk), now[0]))
        if azure_calls <= 3:
            return None, "ERR:Azure 連線失敗: timed out"
        return [f"Azure:{item}" for item in chunk], None

    def bing_backup(chunk):
        calls.append(("bing", list(chunk), now[0]))
        # Move beyond any bounded transient circuit cooldown before next chunk.
        now[0] += 120.0
        return [f"Bing:{item}" for item in chunk], None

    def advance_clock(_should_stop, seconds, quantum=0.2):
        del quantum
        now[0] += seconds
        return True

    monkeypatch.setattr(batch_module.time, "time", lambda: now[0])
    monkeypatch.setattr(batch_module, "interruptible_sleep", advance_clock)
    monkeypatch.setattr(batch_module.random, "uniform", lambda _lo, _hi: 0.0)
    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "azure": azure,
            "bing": bing_backup,
            "gtx": bing_backup,
        },
    )

    batch_module.batch_translate_missing(
        app, ["source-0", "source-1"], _force_chunk_size=1)

    assert [engine for engine, _chunk, _at in calls[:4]] == [
        "azure", "azure", "azure", "bing"]
    assert any(engine == "azure" and chunk == ["source-1"]
               for engine, chunk, _at in calls)
    assert app.translation_cache == {
        "source-0": "Bing:source-0",
        "source-1": "Azure:source-1",
    }
    assert not any("Azure 已停用" in message for message in app.logs)


@pytest.mark.parametrize(
    ("error", "expected_cooldown"),
    [
        ("ERR:Bing token 取得失敗: auth HTTP 503", "冷卻 15s"),
        ("ERR:Bing 連線失敗: timed out", "冷卻 8s"),
    ],
)
def test_bing_temporary_errors_do_not_use_rate_limit_cooldown(
        monkeypatch, error, expected_cooldown):
    app = FakeApp(workers=1)
    calls = []
    normalized = []

    def temporary_failure(chunk):
        calls.append(("bing", list(chunk)))
        return None, error

    def fallback(chunk):
        calls.append(("gtx", list(chunk)))
        return [f"譯文:{item}" for item in chunk], None

    monkeypatch.setattr(
        batch_module,
        "engine_rate_limit_cooldown",
        lambda *args: normalized.append(args) or 60.0,
    )
    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "bing": temporary_failure,
            "gtx": fallback,
        },
    )

    batch_module.batch_translate_missing(app, ["source"], _force_chunk_size=1)

    assert calls == [("bing", ["source"]), ("gtx", ["source"])]
    assert normalized == []
    assert any(expected_cooldown in message for message in app.logs)
    assert not any("Bing免費 限流" in message for message in app.logs)


def test_bing_rate_limit_log_says_fallback_does_not_block(monkeypatch):
    app = FakeApp(workers=1)

    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {
            "bing": lambda _chunk: (None, "429:60"),
            "gtx": lambda chunk: ([f"譯文:{item}" for item in chunk], None),
        },
    )

    batch_module.batch_translate_missing(app, ["source"], _force_chunk_size=1)

    assert app.translation_cache == {"source": "譯文:source"}
    assert any(
        "Bing免費 HTTP 429" in message
        and "僅冷卻此引擎 60s" in message
        and "不中斷翻譯" in message
        for message in app.logs
    )


def test_concurrent_short_cooldown_does_not_shorten_rate_limit(monkeypatch):
    app = FakeApp(workers=2)
    now = [1000.0]
    calls = []
    bing_calls = 0
    calls_lock = threading.Lock()
    first_wave = threading.Barrier(2, timeout=3)
    fallback_wave = threading.Barrier(2, timeout=3)

    def bing(chunk):
        nonlocal bing_calls
        with calls_lock:
            bing_calls += 1
            call_number = bing_calls
            calls.append(("bing", list(chunk), now[0]))
        if call_number <= 2:
            first_wave.wait()
            if call_number == 1:
                return None, "429:60"
            time.sleep(0.05)
            return None, "ERR:Bing 連線失敗: timed out"
        return [f"譯文:{item}" for item in chunk], None

    def gtx(chunk):
        with calls_lock:
            calls.append(("gtx", list(chunk), now[0]))
        if chunk[0] in ("source-0", "source-1"):
            fallback_wave.wait()
            now[0] = 1009.0
        return [f"譯文:{item}" for item in chunk], None

    monkeypatch.setattr(batch_module.time, "time", lambda: now[0])
    monkeypatch.setattr(batch_module.random, "uniform", lambda _lo, _hi: 0.0)
    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {"bing": bing, "gtx": gtx},
    )

    batch_module.batch_translate_missing(
        app, ["source-0", "source-1", "source-2"], _force_chunk_size=1)

    bing_sources = [chunk[0] for engine, chunk, _at in calls if engine == "bing"]
    assert sorted(bing_sources) == ["source-0", "source-1"]
    assert any(engine == "gtx" and chunk == ["source-2"]
               for engine, chunk, _at in calls)


def test_temporary_errors_use_recoverable_circuit(monkeypatch):
    app = FakeApp(workers=1)
    now = [1000.0]
    calls = []
    bing_calls = 0

    def bing(chunk):
        nonlocal bing_calls
        bing_calls += 1
        calls.append(("bing", list(chunk), now[0]))
        if bing_calls <= 3:
            return None, "ERR:Bing token 取得失敗: auth HTTP 503"
        return [f"譯文:{item}" for item in chunk], None

    def gtx(chunk):
        calls.append(("gtx", list(chunk), now[0]))
        now[0] += 120.0
        return [f"譯文:{item}" for item in chunk], None

    monkeypatch.setattr(batch_module.time, "time", lambda: now[0])
    monkeypatch.setattr(batch_module.random, "uniform", lambda _lo, _hi: 0.0)
    monkeypatch.setattr(
        batch_module,
        "build_provider_registry",
        lambda _session, _settings: {"bing": bing, "gtx": gtx},
    )

    batch_module.batch_translate_missing(
        app, [f"source-{index}" for index in range(5)], _force_chunk_size=1)

    assert bing_calls >= 4
    assert any(engine == "bing" and chunk == ["source-3"]
               for engine, chunk, _at in calls)
