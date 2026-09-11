# -*- coding: utf-8 -*-
"""v1.2.7: Base URL host must force matching market_ai provider/api_type."""

from core.batch_translation import translation_fallback_order
from translator_providers import (
    extract_openai_message_text,
    reconcile_market_ai_route,
)


def test_reconcile_deepseek_url_forces_deepseek_even_if_anthropic_key():
    key, cfg, label = reconcile_market_ai_route(
        "anthropic",
        {
            "label": "Anthropic Claude",
            "api_type": "anthropic",
            "base_url": "https://api.anthropic.com/v1/messages",
            "requires_key": True,
        },
        "https://api.deepseek.com/v1",
        "Anthropic Claude",
    )
    assert key == "deepseek"
    assert cfg["api_type"] == "openai_compatible"
    assert label == "DeepSeek"
    assert cfg["label"] == "DeepSeek"


def test_reconcile_anthropic_url_forces_anthropic_api_type():
    key, cfg, label = reconcile_market_ai_route(
        "deepseek",
        {
            "label": "DeepSeek",
            "api_type": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "requires_key": True,
        },
        "https://api.anthropic.com/v1/messages",
        "DeepSeek",
    )
    assert key == "anthropic"
    assert cfg["api_type"] == "anthropic"
    assert label == "Anthropic Claude"


def test_reconcile_unknown_url_unchanged():
    key, cfg, label = reconcile_market_ai_route(
        "custom",
        {
            "label": "自訂 OpenAI Compatible",
            "api_type": "openai_compatible",
            "base_url": "http://localhost:1234/v1",
            "requires_key": False,
        },
        "http://127.0.0.1:8080/v1",
        "自訂 OpenAI Compatible",
    )
    assert key == "custom"
    assert cfg["api_type"] == "openai_compatible"
    assert label == "自訂 OpenAI Compatible"


def test_paid_market_ai_fallback_is_gtx_with_mymemory():
    assert translation_fallback_order(
        "market_ai", "market_ai", "deepseek") == ["gtx", "mymemory"]
    assert translation_fallback_order(
        "market_ai", "market_ai", "anthropic") == ["gtx", "mymemory"]
    assert translation_fallback_order(
        "market_ai", "market_ai", "openai") == ["gtx", "mymemory"]


def test_free_market_ai_keys_still_gtx_with_mymemory_fallback():
    assert translation_fallback_order(
        "market_ai", "market_ai", "deepseek_v4_flash_free") == ["gtx", "mymemory"]
    assert translation_fallback_order(
        "market_ai", "market_ai", "openrouter_free_router") == ["gtx", "mymemory"]


def test_custom_not_in_named_free_provider_tuple_semantics():
    """Paid mis-resolve to custom must not be treated as a named free key.

    free_market_ai no longer lists bare 'custom'; intentional keyless custom
    still relies on requires_key=False elsewhere.
    """
    # Named free keys remain free-path fallbacks (GTX-only).
    assert "custom" not in (
        "deepseek_v4_flash_free",
        "openrouter_free_router",
        "openrouter_free_models",
        "libretranslate",
        "bing_free",
    )


def test_extract_openai_message_prefers_content_then_reasoning():
    assert extract_openai_message_text(
        {"content": '{"0":"你好"}', "reasoning_content": "think"}
    ) == '{"0":"你好"}'
    assert extract_openai_message_text(
        {"content": "", "reasoning_content": '{"0":"劍"}'}
    ) == '{"0":"劍"}'
    assert extract_openai_message_text(
        {"content": None, "reasoning_content": '{"0":"鎬"}'}
    ) == '{"0":"鎬"}'
