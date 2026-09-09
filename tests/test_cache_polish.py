# -*- coding: utf-8 -*-
"""Cache load polish: auto-fix broken placeholders in stored translations."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest

from core.format_mask import fix_placeholders
from translation_cache import TranslationCacheStore, load_translation_cache


FORMAT_RE = re.compile(r"§[0-9a-fk-or]|%[0-9.]*\$?[a-zA-Z]|\{[^}]+\}")


class CachePolishOnLoadTests(unittest.TestCase):
    def test_load_polishes_legacy_broken_rows(self):
        """Simulate old/broken rows already in SQLite, then polish on load."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cache.json")
            # Create schema via store, then inject invalid-looking values raw.
            store, _ = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t and t.strip()))
            store.close()

            db = sqlite3.connect(path + ".sqlite3")
            db.execute(
                "INSERT OR REPLACE INTO cache(source, translated) VALUES (?, ?)",
                ("Need %s apples", "需要 % s 個蘋果"),
            )
            db.execute(
                "INSERT OR REPLACE INTO cache(source, translated) VALUES (?, ?)",
                ("Color §aGreen", "顏色 § a綠色"),
            )
            db.execute(
                "INSERT OR REPLACE INTO cache(source, translated) VALUES (?, ?)",
                ("Value [%s]", "值 [ %s ]"),
            )
            db.commit()
            db.close()

            reopened, messages = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t and t.strip()))
            try:
                self.assertEqual(reopened["Need %s apples"], "需要 %s 個蘋果")
                self.assertEqual(reopened["Color §aGreen"], "顏色 §a綠色")
                self.assertEqual(reopened["Value [%s]"], "值 [%s]")
                self.assertTrue(any("自動修復快取" in m for m in messages))
            finally:
                reopened.close()

    def test_setitem_polishes_before_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cache.json")
            store, _ = load_translation_cache(
                path, FORMAT_RE, lambda _s, t: bool(t and t.strip()))
            try:
                store["Need %s apples"] = "需要 % s 個蘋果"
                self.assertEqual(store["Need %s apples"], "需要 %s 個蘋果")
            finally:
                store.close()

    def test_fix_placeholders_bracketed_and_amp(self):
        self.assertEqual(fix_placeholders("[ %1$s ]"), "[%1$s]")
        self.assertEqual(fix_placeholders("& aOK"), "&aOK")
        self.assertEqual(fix_placeholders("val % . 1 f"), "val %.1f")


if __name__ == "__main__":
    unittest.main()
