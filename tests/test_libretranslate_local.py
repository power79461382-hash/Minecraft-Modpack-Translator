# -*- coding: utf-8 -*-
from core.batch_translation import translation_worker_limit, engine_concurrency_limit


def test_libretranslate_worker_cap_high_for_local():
    assert translation_worker_limit(32, "libretranslate", "market_ai") == 16
    assert engine_concurrency_limit(32, "libretranslate") == 16


def test_libretranslate_preset_in_gui():
    from gui.main_window import ModTranslatorApp
    cfg = ModTranslatorApp.AI_PROVIDER_PRESETS["libretranslate"]
    assert cfg["api_type"] == "libretranslate"
    assert "127.0.0.1:5000" in cfg["base_url"]
    assert cfg.get("requires_key") is False
    assert ModTranslatorApp._ai_provider_menu_for_key("libretranslate") == "free"
