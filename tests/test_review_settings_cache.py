import dbm
import copyreg
import io
import os
import pickle
import re
import sqlite3

import pytest

import translation_cache
from translation_cache import (
    TranslationCacheStore,
    load_translation_cache,
    save_translation_cache,
)


FORMAT_RE = re.compile(r"(?!x)x")


class UnsafeLegacyValue:
    def __init__(self, marker):
        self.marker = str(marker)

    def __reduce__(self):
        # Harmless synthetic local side effect demonstrates callable execution.
        return os.mkdir, (self.marker,)


def test_plain_pickle_loader_rejects_cached_extensions(tmp_path):
    marker = tmp_path / "unexpected-extension-side-effect"
    extension_code = 23456
    copyreg.add_extension(os.mkdir.__module__, "mkdir", extension_code)
    try:
        payload = pickle.dumps(UnsafeLegacyValue(marker), protocol=2)
        # Other unpickling in the process can populate this global cache;
        # extension lookup then bypasses Unpickler.find_class.
        copyreg._extension_cache[extension_code] = os.mkdir
        with pytest.raises(pickle.UnpicklingError):
            translation_cache.load_legacy_cache_pickle(io.BytesIO(payload))
        assert not marker.exists()
    finally:
        copyreg.remove_extension(os.mkdir.__module__, "mkdir", extension_code)
        copyreg._extension_cache.pop(extension_code, None)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_plain_pickle_loader_supports_legacy_protocols(protocol):
    payload = pickle.dumps({"source": "translation"}, protocol=protocol)
    assert translation_cache.load_legacy_cache_pickle(io.BytesIO(payload)) == {
        "source": "translation"
    }


@pytest.mark.parametrize("kind", ["pkl", "shelve"])
def test_legacy_cache_migration_rejects_pickle_callables(tmp_path, kind):
    path = str(tmp_path / "cache.json")
    marker = tmp_path / "unexpected-deserialization-side-effect"
    payload = pickle.dumps(UnsafeLegacyValue(marker))
    if kind == "pkl":
        with open(path + ".pkl", "wb") as stream:
            stream.write(payload)
    else:
        with dbm.open(path + ".shelve", "c") as old:
            old[b"source"] = payload

    cache, messages = load_translation_cache(path, FORMAT_RE, lambda _s, _t: True)
    try:
        assert not marker.exists()
        assert len(cache) == 0
        assert messages
    finally:
        if hasattr(cache, "close"):
            cache.close()


@pytest.mark.parametrize("kind", ["pkl", "shelve"])
def test_plain_legacy_cache_still_migrates(tmp_path, kind):
    path = str(tmp_path / "cache.json")
    if kind == "pkl":
        with open(path + ".pkl", "wb") as stream:
            pickle.dump({"source": "translation"}, stream)
    else:
        with dbm.open(path + ".shelve", "c") as old:
            old[b"source"] = pickle.dumps("translation")
    cache, _messages = load_translation_cache(path, FORMAT_RE, lambda _s, _t: True)
    try:
        assert cache["source"] == "translation"
    finally:
        cache.close()


def test_mapping_save_rolls_back_if_insert_fails(tmp_path):
    path = str(tmp_path / "cache.json")
    cache = TranslationCacheStore(path, FORMAT_RE, lambda _s, _t: True)
    cache["prior"] = "old translation"
    cache.close()
    with sqlite3.connect(path + ".sqlite3") as db:
        db.execute("CREATE TRIGGER reject_new BEFORE INSERT ON cache "
                   "WHEN NEW.source = 'bad' "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END")
    with pytest.raises(sqlite3.DatabaseError):
        save_translation_cache(path, {"good": "new value", "bad": "bad value"})
    with sqlite3.connect(path + ".sqlite3") as db:
        assert dict(db.execute("SELECT source, translated FROM cache")) == {
            "prior": "old translation"
        }


def test_mapping_save_reports_unwritable_database(tmp_path):
    path = str(tmp_path / "missing-parent" / "cache.json")
    with pytest.raises((OSError, sqlite3.DatabaseError)):
        save_translation_cache(path, {"source": "translation"})


def test_sync_failure_keeps_pending_values_for_retry(tmp_path):
    path = str(tmp_path / "cache.json")
    cache = TranslationCacheStore(path, FORMAT_RE, lambda _s, _t: True)
    try:
        cache["source"] = "translation"
        cache._db.execute("PRAGMA foreign_keys = ON")
        # A deferred FK lets INSERT finish but makes COMMIT fail.
        cache._db.execute("CREATE TABLE parents(id TEXT PRIMARY KEY)")
        cache._db.execute("CREATE TABLE children(id TEXT REFERENCES parents(id) "
                          "DEFERRABLE INITIALLY DEFERRED)")
        cache._db.execute("CREATE TRIGGER deferred_failure AFTER INSERT ON cache "
                          "BEGIN INSERT INTO children VALUES (NEW.source); END")
        cache._db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            cache.sync()
        cache._db.rollback()
        cache._db.execute("DROP TRIGGER deferred_failure")
        cache._db.commit()
        assert cache["source"] == "translation"
        assert len(cache) == 1
        cache.sync()
    finally:
        cache.close()
    with sqlite3.connect(path + ".sqlite3") as db:
        assert dict(db.execute("SELECT source, translated FROM cache")) == {
            "source": "translation"
        }
