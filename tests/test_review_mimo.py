import json
from types import SimpleNamespace

import pytest

import translator_providers as providers
from gui.main_window import ModTranslatorApp


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def configure(self, **kwargs):
        self.values = kwargs.get("values")

    def config(self, **kwargs):
        pass


def headless_app():
    app = object.__new__(ModTranslatorApp)
    for name in ModTranslatorApp.load_config.__code__.co_names:
        if name.endswith("_var"):
            setattr(app, name, Value())
    app.ai_provider_var = Value("Xiaomi MiMo")
    app.ai_provider_menu_var = Value("paid")
    app.ai_model_var = Value()
    app.ai_base_url_var = Value()
    app.ai_login_url_var = Value()
    app.ai_model_cb = Widget()
    app.ai_provider_hint = Widget()
    for name in ("log", "_update_libretranslate_service_controls",
                 "_refresh_api_summary", "_on_mc_version_change",
                 "_on_engine_change", "_on_output_mode_change"):
        setattr(app, name, lambda *args, **kwargs: None)
    return app


@pytest.mark.parametrize("old,new", [
    ("mimo-v2.5-pro", "mimo-v2.6-pro"),
    ("mimo-v2.5", "mimo-v2.6-flash"),
])
def test_persisted_mimo_model_migrates_without_changing_endpoint(tmp_path, old, new):
    app = headless_app()
    app.config_file = str(tmp_path / "app_config.json")
    custom_url = "https://my-proxy.example/v1"
    with open(app.config_file, "w", encoding="utf-8") as file:
        json.dump({"ai_provider": "Xiaomi MiMo", "ai_model": old,
                   "ai_base_url": custom_url}, file)

    app.load_config()

    assert app.ai_model_var.get() == new
    assert app.ai_base_url_var.get() == custom_url
    assert app.ai_api_key_var.get() == ""


def test_provider_change_preserves_flash_tier():
    app = headless_app()
    app.ai_model_var.set("mimo-v2.5")

    app._on_ai_provider_change()

    assert app.ai_model_var.get() == "mimo-v2.6-flash"
    assert "mimo-v2.6-flash" in app.ai_model_cb.values
    assert "mimo-v2.5" not in app.ai_model_cb.values


@pytest.mark.parametrize("old,new", [
    ("mimo-v2.5-pro", "mimo-v2.6-pro"),
    ("mimo-v2.5", "mimo-v2.6-flash"),
])
def test_mimo_request_migrates_legacy_model_with_bounded_nonthinking_output(old, new):
    class Session:
        def post(self, url, **kwargs):
            assert url == "https://api.xiaomimimo.com/v1/chat/completions"
            self.body = kwargs["json"]
            return SimpleNamespace(status_code=200, text="", json=lambda: {
                "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"0":"石頭"}'}}]})

    session = Session()
    registry = providers.build_provider_registry(session, {
        "ai_provider_key": "xiaomi_mimo",
        "ai_provider_cfg": ModTranslatorApp.AI_PROVIDER_PRESETS["xiaomi_mimo"],
        "ai_model": old, "ai_base_url": "https://api.xiaomimimo.com/v1",
        "ai_api_key": "synthetic-test-key",
    })

    assert registry["market_ai"](["Stone"]) == (["石頭"], None)
    assert session.body["model"] == new
    assert session.body["max_completion_tokens"] == 4096
    assert "max_tokens" not in session.body
    assert session.body["thinking"] == {"type": "disabled"}
    assert session.body["temperature"] == 0.1


def test_other_provider_legacy_named_model_is_unchanged():
    class Session:
        def post(self, url, **kwargs):
            self.body = kwargs["json"]
            return SimpleNamespace(status_code=200, text="", json=lambda: {
                "choices": [{"finish_reason": "stop", "message": {
                    "content": '{"0":"石頭"}'}}]})
    session = Session()
    provider = providers.build_provider_registry(session, {
        "ai_provider_key": "custom", "ai_model": "mimo-v2.5",
        "ai_base_url": "http://localhost:1234/v1",
    })["market_ai"]
    assert provider(["Stone"]) == (["石頭"], None)
    assert session.body["model"] == "mimo-v2.5"


@pytest.mark.parametrize("saved_model", [[], {}, None])
def test_malformed_saved_mimo_model_uses_safe_default(tmp_path, saved_model):
    app = headless_app()
    app.config_file = str(tmp_path / "app_config.json")
    with open(app.config_file, "w", encoding="utf-8") as file:
        json.dump({"ai_provider": "Xiaomi MiMo", "ai_model": saved_model}, file)
    app.load_config()
    assert app.ai_model_var.get() == "mimo-v2.6-pro"
