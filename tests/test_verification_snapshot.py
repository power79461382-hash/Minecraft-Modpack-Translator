import os
import re
import sqlite3
import tempfile
import threading

import core.translation_flow as translation_flow
from core.verification import verify_translations
from gui.main_window import ModTranslatorApp
from translation_cache import TranslationCacheStore, review_and_fix_cache


class SnapshotOnlyCache:
    def __init__(self, values):
        self.values = values
        self.snapshot_calls = 0

    def snapshot(self, keys=None):
        self.snapshot_calls += 1
        selected = self.values if keys is None else {
            key: self.values[key] for key in keys if key in self.values
        }
        return dict(selected)

    def get(self, *_args, **_kwargs):
        raise AssertionError("verification must use one bulk snapshot")

    def __contains__(self, _key):
        raise AssertionError("verification must use one bulk snapshot")

    def bulk_update(self, _items, sync=True):
        raise AssertionError("unchanged review snapshot must not be rewritten")

    def pop(self, *_args, **_kwargs):
        raise AssertionError("valid review snapshot must not remove entries")


class VerificationApp:
    _RE_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")

    def __init__(self):
        self.translation_cache = SnapshotOnlyCache({
            "Hello %s": "你好 %s",
            "Plain": "純文字",
        })
        self.logs = []

    def log(self, message):
        self.logs.append(message)

    @staticmethod
    def _is_valid_trad_translation(source, translated):
        return bool(translated and translated != source
                    and VerificationApp._RE_CJK_CHAR.search(translated))

    def _cache_has_usable_translation(self, _source):
        raise AssertionError("verification must use one bulk snapshot")

    @staticmethod
    def should_translate(_source):
        return True

    @staticmethod
    def get_translation(_source):
        raise AssertionError("all test strings are present in snapshot")

    @staticmethod
    def _critical_format_tokens(text):
        return re.findall(r"%\d*\$?[sd]", text)

    @staticmethod
    def _write_failed_items_report(_untranslated, _fmt_issues):
        return ""


def test_verification_uses_one_bulk_cache_snapshot():
    app = VerificationApp()

    assert verify_translations(app, {"Hello %s", "Plain"})
    assert app.translation_cache.snapshot_calls == 1


def test_cache_usable_check_uses_single_get_without_contains():
    class GetOnlyCache:
        def __init__(self):
            self.get_calls = 0

        def get(self, key, default=None):
            self.get_calls += 1
            return "譯文" if key == "Source" else default

        def __contains__(self, _key):
            raise AssertionError("contains causes a second SQLite query")

    class App:
        translation_cache = GetOnlyCache()

        @staticmethod
        def _is_valid_trad_translation(source, translated):
            return translated == "譯文" and source == "Source"

    app = App()

    assert ModTranslatorApp._cache_has_usable_translation(app, "Source")
    assert app.translation_cache.get_calls == 1


def test_review_uses_one_bulk_cache_snapshot():
    class App(VerificationApp):
        _RE_FORMAT = re.compile(r"%\d*\$?[sd]")

        def __init__(self):
            super().__init__()
            self._session_translated_keys = {"Hello %s", "Plain"}
            self._memory_pool_dirty = False
            self.save_calls = []

        @staticmethod
        def _to_traditional(text):
            return text

        @staticmethod
        def _repair_patchouli_macros(text):
            return text

        def save_cache(self, light=False):
            self.save_calls.append(light)

    app = App()

    ModTranslatorApp._review_and_fix_cache(app)

    assert app.translation_cache.snapshot_calls == 1
    assert app.save_calls == [True]


def test_usable_key_collection_uses_bulk_snapshot():
    app = VerificationApp()

    usable = translation_flow.usable_cache_translation_keys(
        app, {"Hello %s", "Plain", "Missing"})

    assert usable == {"Hello %s", "Plain"}
    assert app.translation_cache.snapshot_calls == 1


def test_light_save_flushes_pending_overwrite_when_cache_length_is_unchanged():
    class DisabledValue:
        @staticmethod
        def get():
            return False

    class App:
        global_memory_var = DisabledValue()

        def __init__(self, cache):
            self.translation_cache = cache
            self._cache_lock = threading.Lock()
            self._last_light_len = len(cache)
            self.last_save_time = 0

    with tempfile.TemporaryDirectory() as tmp:
        cache_path = os.path.join(tmp, "translation_cache")
        cache = TranslationCacheStore(
            cache_path, re.compile(r"$^"), lambda _source, _translated: True)
        try:
            cache["same key"] = "old value"
            cache.sync()
            cache["same key"] = "new value"

            ModTranslatorApp.save_cache(App(cache), light=True)

            db = sqlite3.connect(cache_path + ".sqlite3")
            try:
                stored = db.execute(
                    "SELECT translated FROM cache WHERE source = ?",
                    ("same key",),
                ).fetchone()[0]
            finally:
                db.close()
            assert stored == "new value"
        finally:
            cache.close()


def test_cache_review_can_cancel_before_scanning_the_full_snapshot():
    source_cache = {
        f"source {index}": f"譯文 {index}"
        for index in range(1_100)
    }
    cancel_checks = 0

    def should_cancel():
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks >= 2

    reviewed, stats = review_and_fix_cache(
        source_cache,
        re.compile(r"$^"),
        lambda text: text,
        lambda _source, _translated: True,
        should_cancel=should_cancel,
    )

    assert stats["cancelled"] is True
    assert 0 < len(reviewed) < len(source_cache)
    assert len(source_cache) == 1_100


def test_bulk_update_prefetches_existing_keys_in_bounded_queries(tmp_path):
    cache_path = str(tmp_path / "translation_cache")
    cache = TranslationCacheStore(
        cache_path, re.compile(r"$^"), lambda _source, _translated: True)
    select_statements = []
    try:
        cache._db.set_trace_callback(
            lambda statement: select_statements.append(statement)
            if statement.lstrip().upper().startswith("SELECT") else None)

        cache.bulk_update(
            ((f"source {index}", f"譯文 {index}") for index in range(1_000)),
            sync=False,
        )

        assert len(select_statements) <= 4
    finally:
        cache._db.set_trace_callback(None)
        cache.close()
