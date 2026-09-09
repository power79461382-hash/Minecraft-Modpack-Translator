# -*- coding: utf-8 -*-
"""Regression tests for zh_cn-only packaging and provider locale handling."""

from __future__ import annotations

import inspect

import translation_packager as packager
import translator_providers as providers


def test_drop_untranslated_keeps_cjk_passthrough():
    source = {
        "item.mod.wood": "木",
        "item.mod.ingot": "铁锭",
        "item.mod.english": "Iron Ingot",
    }
    output = {
        "item.mod.wood": "木",  # shared glyph, unchanged after s2t
        "item.mod.ingot": "鐵錠",
        "item.mod.english": "Iron Ingot",  # untranslated English must drop
    }
    cleaned, dropped = packager.drop_untranslated_lang_entries(source, output)
    assert cleaned["item.mod.wood"] == "木"
    assert cleaned["item.mod.ingot"] == "鐵錠"
    assert "item.mod.english" not in cleaned
    assert dropped == 1


def test_drop_untranslated_still_drops_empty():
    source = {"a": "hello"}
    output = {"a": "   "}
    cleaned, dropped = packager.drop_untranslated_lang_entries(source, output)
    assert cleaned == {}
    assert dropped == 1


def test_azure_omits_hardcoded_from_en():
    src = inspect.getsource(providers)
    assert '"from": "en"' not in src or "omit from=" in src
    # Stronger: the Azure params line must not force from=en
    assert 'params={"api-version": "3.0", "from": "en", "to": "zh-Hant"}' not in src
    assert 'params={"api-version": "3.0", "to": "zh-Hant"}' in src


def test_mymemory_uses_zh_cn_pair_for_cjk():
    src = inspect.getsource(providers)
    assert "zh-CN|zh-TW" in src
    assert "en|zh-TW" in src


def test_simp_fallback_covers_common_gear_words():
    # Import app class without launching GUI if possible
    from gui.main_window import ModTranslatorApp

    # Force fallback path by temporarily clearing OpenCC if present
    import gui.main_window as mw
    old = mw._OPENCC_TW
    try:
        mw._OPENCC_TW = None
        out = ModTranslatorApp._to_traditional("装备 防御 伤害 护甲 攻击")
        for needle in ("裝備", "防禦", "傷害", "護甲", "攻擊"):
            assert needle in out, f"missing {needle} in {out!r}"
    finally:
        mw._OPENCC_TW = old
