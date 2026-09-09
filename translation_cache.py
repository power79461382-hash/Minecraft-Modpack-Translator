import json
import contextlib
import os
import pickle
import re
import shutil
import shelve
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import MutableMapping
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from translation_packager import sanitize_text, sanitize_value
from core.format_mask import fix_placeholders


_RE_ROMAN_TOKEN = re.compile(
    r'^(?:C|XC|XL|L?X{1,3}(?:IX|IV|V?I{0,3})?|II|III|IV|V|VI|VII|VIII|IX)$',
    re.IGNORECASE,
)


def _critical_formats(tokens: Iterable[str]) -> List[str]:
    """保留會影響 Minecraft 執行期的格式碼。"""
    return [token for token in tokens if not _RE_ROMAN_TOKEN.fullmatch(token)]


def _format_tokens_match(source: str, translated: str, format_re) -> bool:
    """格式碼/佔位符比對：用多重集合比較內容與數量。"""
    orig = Counter(_critical_formats(format_re.findall(source)))
    trans = Counter(_critical_formats(format_re.findall(translated)))
    return orig == trans


def _entry_valid(source: str, translated: str, format_re,
                 is_valid_translation: Callable[[str, str], bool]) -> bool:
    if not is_valid_translation(source, translated):
        return False
    return _format_tokens_match(source, translated, format_re)


class TranslationCacheStore(MutableMapping):
    """以 SQLite 為底的翻譯快取，按需讀寫，不全量載入記憶體。

    執行緒安全：所有資料庫操作都用 _lock 保護。
    """

    def __init__(self, cache_path: str, format_re,
                 is_valid_translation: Callable[[str, str], bool],
                 flag: str = "c") -> None:
        self.cache_file = cache_path
        self._path = cache_path + ".sqlite3"
        self._format_re = format_re
        self._is_valid = is_valid_translation
        self._lock = threading.RLock()  # 可重入鎖，支援同一執行緒巢狀呼叫
        readonly = flag == "r"
        uri = False
        db_path = self._path
        if readonly:
            db_path = f"file:{self._path}?mode=ro"
            uri = True
        self._db = sqlite3.connect(
            db_path,
            timeout=30,
            check_same_thread=False,
            uri=uri,
        )
        if not readonly:
            with contextlib.suppress(sqlite3.DatabaseError):
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "source TEXT PRIMARY KEY, "
                "translated TEXT NOT NULL)"
            )
            self._db.commit()
        # 翻譯熱路徑會在短時間寫入數千筆。即使用 SQLite，
        # 每批都落盤仍會讓 UI 偶發卡頓。
        # 先放在記憶體，定時存檔或結束時再批次落盤。
        self._pending: Dict[str, str] = {}
        # 追蹤 _pending 中「DB 裡不存在」的新 key 數量，
        # 讓 __len__ 不必逐筆查詢 DB。
        self._pending_new_count: int = 0

    @property
    def shelve_path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_pending_locked()
                self._db.commit()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                self._db.close()

    def sync(self) -> None:
        with self._lock:
            self._flush_pending_locked()
            self._db.commit()

    def _flush_pending_locked(self) -> int:
        if not self._pending:
            return 0
        pending = list(self._pending.items())
        self._db.executemany(
            "INSERT OR REPLACE INTO cache(source, translated) VALUES (?, ?)",
            pending,
        )
        self._pending.clear()
        self._pending_new_count = 0
        return len(pending)

    def __getitem__(self, key: str) -> str:
        """純讀取，不做驗證或清理（避免大量迭代時觸發寫入導致極慢）。"""
        with self._lock:
            safe_key = sanitize_text(str(key))
            if safe_key in self._pending:
                return sanitize_text(self._pending[safe_key])
            row = self._db.execute(
                "SELECT translated FROM cache WHERE source = ?",
                (safe_key,),
            ).fetchone()
            if not row or not isinstance(row[0], str):
                raise KeyError(key)
            return sanitize_text(row[0])

    def __setitem__(self, key: str, value: str) -> None:
        with self._lock:
            safe_key = sanitize_text(str(key))
            safe_value = sanitize_text(fix_placeholders(str(value)))
            if _entry_valid(safe_key, safe_value, self._format_re, self._is_valid):
                # 只有「尚未在 _pending 且不在 DB」的 key 才算新增
                if safe_key not in self._pending:
                    row = self._db.execute(
                        "SELECT 1 FROM cache WHERE source = ? LIMIT 1",
                        (safe_key,),
                    ).fetchone()
                    if not row:
                        self._pending_new_count += 1
                self._pending[safe_key] = safe_value

    def __delitem__(self, key: str) -> None:
        with self._lock:
            safe_key = sanitize_text(str(key))
            had_pending = safe_key in self._pending
            cur = self._db.execute(
                "DELETE FROM cache WHERE source = ?",
                (safe_key,),
            )
            if had_pending:
                self._pending.pop(safe_key, None)
                # DB 沒有舊值時，pending 才代表尚未計入 DB 的新 key。
                if cur.rowcount == 0 and self._pending_new_count > 0:
                    self._pending_new_count -= 1
                return
            if cur.rowcount == 0:
                raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            keys = [
                row[0]
                for row in self._db.execute("SELECT source FROM cache")
            ]
            existing = set(keys)
            keys.extend(k for k in self._pending.keys() if k not in existing)
            return iter(keys)

    def snapshot(self, keys=None) -> Dict[str, str]:
        """Return a consistent read-only snapshot with pending values applied."""
        with self._lock:
            if keys is None:
                rows = self._db.execute(
                    "SELECT source, translated FROM cache"
                ).fetchall()
                result = dict(rows)
                result.update(self._pending)
                return result

            requested = {
                str(key): sanitize_text(str(key))
                for key in keys
                if key is not None
            }
            if not requested:
                return {}

            canonical_values: Dict[str, str] = {}
            requested_list = list(set(requested.values()))
            # Stay below SQLite's host-parameter limit on older bundled builds.
            for start in range(0, len(requested_list), 500):
                batch = requested_list[start:start + 500]
                placeholders = ','.join('?' for _ in batch)
                rows = self._db.execute(
                    f"SELECT source, translated FROM cache "
                    f"WHERE source IN ({placeholders})",
                    batch,
                ).fetchall()
                canonical_values.update(rows)
            for canonical_key in requested_list:
                if canonical_key in self._pending:
                    canonical_values[canonical_key] = self._pending[canonical_key]
            return {
                original_key: canonical_values[canonical_key]
                for original_key, canonical_key in requested.items()
                if canonical_key in canonical_values
            }

    def __len__(self) -> int:
        with self._lock:
            db_len = self._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            return int(db_len) + self._pending_new_count

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        with self._lock:
            safe_key = sanitize_text(key)
            if safe_key in self._pending:
                return True
            row = self._db.execute(
                "SELECT 1 FROM cache WHERE source = ? LIMIT 1",
                (safe_key,),
            ).fetchone()
            return bool(row)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """純讀取，不做驗證（避免迭代時觸發寫入）。"""
        try:
            return self[key]
        except KeyError:
            return default


    def polish_placeholders(self) -> int:
        """載入後掃描快取，自動修復損壞的格式碼／佔位符並寫回。

        回傳實際被修正的筆數。只更新「修好後仍通過驗證」的條目，
        不會在此刪除無效條目（那是 validate_and_clean 的工作）。
        """
        with self._lock:
            self._flush_pending_locked()
            rows = list(self._db.execute("SELECT source, translated FROM cache"))
            updates = []
            for source, translated in rows:
                if not isinstance(translated, str) or not translated:
                    continue
                fixed = fix_placeholders(translated)
                if fixed == translated:
                    continue
                safe_key = sanitize_text(str(source))
                safe_value = sanitize_text(fixed)
                if not _entry_valid(
                        safe_key, safe_value, self._format_re, self._is_valid):
                    continue
                updates.append((safe_value, safe_key))
                # pending 也同步，避免讀到舊值
                if safe_key in self._pending:
                    self._pending[safe_key] = safe_value
            if updates:
                self._db.executemany(
                    "UPDATE cache SET translated = ? WHERE source = ?",
                    updates,
                )
                self._db.commit()
            return len(updates)

    def validate_and_clean(self) -> int:
        """掃描整個快取，移除無效條目。回傳移除的數量。

        這個方法會觸發大量寫入，應該在維護時單獨調用，
        不應在正常讀取流程中自動執行。
        """
        with self._lock:
            self._flush_pending_locked()
            removed = 0
            keys_to_remove = []
            rows = list(self._db.execute("SELECT source, translated FROM cache"))
            for key, value in rows:
                try:
                    if not isinstance(value, str):
                        keys_to_remove.append(key)
                        continue
                    safe_key = sanitize_text(str(key))
                    safe_value = sanitize_text(value)
                    if not _entry_valid(safe_key, safe_value, self._format_re, self._is_valid):
                        keys_to_remove.append(key)
                except Exception:
                    keys_to_remove.append(key)

            for key in keys_to_remove:
                try:
                    self._db.execute(
                        "DELETE FROM cache WHERE source = ?",
                        (key,),
                    )
                    removed += 1
                except Exception:
                    pass

            if removed > 0:
                self._db.commit()
            return removed

    def pop(self, key: str, default=None):
        with self._lock:
            safe_key = sanitize_text(str(key))
            if safe_key in self._pending:
                val = self._pending[safe_key]
                cur = self._db.execute(
                    "DELETE FROM cache WHERE source = ?",
                    (safe_key,),
                )
                self._pending.pop(safe_key)
                if cur.rowcount == 0 and self._pending_new_count > 0:
                    self._pending_new_count -= 1
                return val
            row = self._db.execute(
                "SELECT translated FROM cache WHERE source = ?",
                (safe_key,),
            ).fetchone()
            if row:
                self._db.execute(
                    "DELETE FROM cache WHERE source = ?",
                    (safe_key,),
                )
                return row[0]
            return default

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._pending_new_count = 0
            self._db.execute("DELETE FROM cache")
            self._db.commit()

    def bulk_update(self, items: Iterable[Tuple[str, str]], sync: bool = True) -> int:
        """批次寫入。

        sync=False 用於翻譯熱路徑，避免每個批次都強制落盤拖慢速度；
        定時存檔與結束存檔仍會呼叫 sync() 保護進度。
        """
        with self._lock:
            written = 0
            valid_updates = {}
            for key, value in items:
                safe_key = sanitize_text(str(key))
                safe_value = sanitize_text(fix_placeholders(str(value)))
                if _entry_valid(safe_key, safe_value, self._format_re, self._is_valid):
                    valid_updates[safe_key] = safe_value
                    written += 1

            unchecked = [
                key for key in valid_updates
                if key not in self._pending
            ]
            existing = set()
            for start in range(0, len(unchecked), 500):
                chunk = unchecked[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._db.execute(
                    f"SELECT source FROM cache WHERE source IN ({placeholders})",
                    chunk,
                ).fetchall()
                existing.update(row[0] for row in rows)

            self._pending_new_count += len(set(unchecked) - existing)
            self._pending.update(valid_updates)
            if sync:
                self._flush_pending_locked()
                self._db.commit()
            return written


def _read_legacy_cache(candidate: str) -> Dict[str, str]:
    if candidate.endswith(".pkl") or candidate.endswith(".pkl.bak"):
        with open(candidate, "rb") as f:
            raw = pickle.load(f)
    else:
        with open(candidate, "r", encoding="utf-8") as f:
            raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("cache root is not an object")
    return raw


def _rename_migrated(path: str) -> None:
    migrated = path + ".migrated"
    if not os.path.exists(path) or os.path.exists(migrated):
        return
    try:
        os.replace(path, migrated)
    except OSError:
        pass


def _shelve_has_files(cache_file: str) -> bool:
    base = cache_file + ".shelve"
    if os.path.exists(base):
        return True
    dirname = os.path.dirname(base) or "."
    prefix = os.path.basename(base)
    try:
        return any(name == prefix or name.startswith(prefix + ".")
                   for name in os.listdir(dirname))
    except OSError:
        return False


def _sqlite_has_files(cache_file: str) -> bool:
    return os.path.exists(cache_file + ".sqlite3")


def _read_shelve_cache(cache_file: str) -> Dict[str, str]:
    base = cache_file + ".shelve"
    raw: Dict[str, str] = {}
    with shelve.open(base, flag="r") as old:
        for key in old.keys():
            value = old[key]
            if isinstance(key, str) and isinstance(value, str):
                raw[key] = value
    return raw


def _repair_shelve_sidecars(cache_file: str) -> List[str]:
    """修復 Windows dbm.dumb/shelve 中斷後留下的空索引檔。

    dbm.dumb 會使用 .dat/.dir，寫入時可能同時留下 .bak。若程式被強制關閉，
    .dir 有機會成為 0 bytes，導致後續開啟快取極慢或讀取失敗。
    """
    messages: List[str] = []
    base = cache_file + ".shelve"
    dir_path = base + ".dir"
    bak_path = base + ".bak"
    dat_path = base + ".dat"
    try:
        if (os.path.exists(dir_path) and os.path.getsize(dir_path) == 0
                and os.path.exists(bak_path) and os.path.getsize(bak_path) > 0):
            shutil.copy2(bak_path, dir_path)
            messages.append("🩹 已修復 shelve 快取索引檔（.dir 由 .bak 還原）")
        if (os.path.exists(dat_path) and os.path.getsize(dat_path) == 0
                and os.path.exists(cache_file + ".pkl")):
            # 留給 legacy migration 重新建立，不直接刪除避免破壞使用者資料。
            messages.append("⚠️ shelve 資料檔疑似為空；若快取不可用，將嘗試從舊快取遷移。")
    except OSError as e:
        messages.append(f"⚠️ shelve 快取側邊檔檢查失敗：{e}")
    return messages


def _migrate_legacy_cache(cache_file: str, store: TranslationCacheStore,
                          format_re,
                          is_valid_translation: Callable[[str, str], bool]) -> List[str]:
    messages: List[str] = []
    if len(store) > 0:
        return messages

    candidates: List[Tuple[str, Callable[[], Dict[str, str]], bool]] = []
    if _shelve_has_files(cache_file):
        candidates.append((
            cache_file + ".shelve",
            lambda cache_file=cache_file: _read_shelve_cache(cache_file),
            False,
        ))
    for candidate in (
            cache_file + ".pkl",
            cache_file + ".pkl.bak",
            cache_file,
            cache_file + ".bak"):
        if os.path.exists(candidate):
            candidates.append((
                candidate,
                lambda candidate=candidate: _read_legacy_cache(candidate),
                True,
            ))

    for candidate, reader, should_rename in candidates:
        try:
            raw = reader()
        except (json.JSONDecodeError, OSError, pickle.PickleError, EOFError,
                ValueError, UnicodeError, Exception) as e:
            messages.append(f"⚠️ 舊快取讀取失敗（{os.path.basename(candidate)}）: {e}")
            continue

        cleaned = []
        removed = 0
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, str):
                removed += 1
                continue
            safe_key = sanitize_text(k)
            safe_value = sanitize_text(v)
            if _entry_valid(safe_key, safe_value, format_re, is_valid_translation):
                cleaned.append((safe_key, safe_value))
            else:
                removed += 1
        store.bulk_update(cleaned)
        store.sync()
        if should_rename:
            for old in (cache_file + ".pkl", cache_file + ".pkl.bak",
                        cache_file, cache_file + ".bak"):
                _rename_migrated(old)
        messages.append(
            f"✅ 已將舊快取遷移到 SQLite：{len(cleaned):,} 筆"
            + (f"，清理 {removed:,} 筆無效條目" if removed else ""))
        return messages
    return messages


def load_translation_cache(cache_file: str, format_re,
                           is_valid_translation: Callable[[str, str], bool]):
    """載入 SQLite 快取，必要時自動從舊 shelve/pkl/json 遷移。

    開啟後會自動 polish 快取中損壞的格式碼／佔位符（如 `% s`、`§ a`），
    並把修正結果寫回，避免舊差譯一直殘留。
    """
    messages: List[str] = []
    try:
        messages.extend(_repair_shelve_sidecars(cache_file))
        store = TranslationCacheStore(cache_file, format_re, is_valid_translation)
        if (not _sqlite_has_files(cache_file) or len(store) == 0):
            messages.extend(_migrate_legacy_cache(
                cache_file, store, format_re, is_valid_translation))
        polished = store.polish_placeholders()
        if polished:
            messages.append(
                f"✅ 已自動修復快取中 {polished:,} 筆損壞的格式碼／佔位符")
        return store, messages
    except Exception as e:
        messages.append(f"⚠️ SQLite 快取開啟失敗，改用記憶體快取：{e}")
        return {}, messages


def save_translation_cache(cache_file: str, cache: Mapping[str, str]) -> float:
    """儲存快取。SQLite-backed 快取只需 sync；一般 mapping 會寫入 SQLite。"""
    if isinstance(cache, TranslationCacheStore):
        cache.sync()
        return time.time()

    snapshot = sanitize_value(dict(cache))
    store, _messages = load_translation_cache(
        cache_file, re.compile(r"$^"), lambda _s, _t: True)
    if isinstance(store, TranslationCacheStore):
        store.clear()
        store.bulk_update(snapshot.items())
        store.sync()
    return time.time()


def cache_snapshot(cache: Mapping[str, str], keys=None) -> Dict[str, str]:
    """Read cache values once, using a store's optimized snapshot when present."""
    snapshot = getattr(cache, 'snapshot', None)
    if callable(snapshot):
        return snapshot(keys)
    if keys is None:
        return dict(cache)

    missing = object()
    result = {}
    for key in keys:
        value = cache.get(key, missing)
        if value is not missing:
            result[key] = value
    return result


def review_and_fix_cache(
        cache: Mapping[str, str], format_re, to_traditional,
        is_valid_translation: Callable[[str, str], bool],
        should_cancel: Optional[Callable[[], bool]] = None):
    """回傳清理後快取與 before/converted/removed 統計。"""
    before = len(cache)
    converted = 0
    removed = 0
    cancelled = False
    new_cache: Dict[str, str] = {}

    for index, (source, translated) in enumerate(cache.items()):
        if (index % 512 == 0 and callable(should_cancel)
                and should_cancel()):
            cancelled = True
            break
        if not isinstance(translated, str) or not translated.strip() or translated == source:
            removed += 1
            continue

        safe_source = sanitize_text(source)
        fixed = sanitize_text(to_traditional(fix_placeholders(translated)))
        if not is_valid_translation(safe_source, fixed):
            removed += 1
            continue

        if not _format_tokens_match(safe_source, fixed, format_re):
            removed += 1
            continue

        if fixed != translated:
            converted += 1
        new_cache[safe_source] = fixed

    return new_cache, {
        "before": before,
        "converted": converted,
        "removed": removed,
        "kept": len(new_cache),
        "cancelled": cancelled,
    }
