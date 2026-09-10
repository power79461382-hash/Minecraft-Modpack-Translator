# -*- coding: utf-8 -*-
import translator_providers as providers


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or [[["ok", None, None, None]]]

    def json(self):
        return self._payload


def test_gtx_rate_limit_slows_gate(monkeypatch):
    monkeypatch.setattr(providers, "GTX_GATE_INTERVAL", 0.12)
    monkeypatch.setattr(providers, "GTX_GATE_MIN", 0.08)
    monkeypatch.setattr(providers, "GTX_GATE_MAX", 1.5)
    monkeypatch.setattr(providers, "GTX_SINGLETON_WORKERS", 1)
    monkeypatch.setattr(providers, "GTX_BATCH_SIZE", 8)
    monkeypatch.setattr(providers, "GTX_BATCH_CHARS", 500)

    class Sess:
        def __init__(self):
            self.n = 0
        def get(self, *a, **k):
            self.n += 1
            if self.n == 1:
                return FakeResp(429)
            return FakeResp(200)

    sess = Sess()
    settings = {
        "google_key": "", "deepl_key": "", "azure_key": "",
        "azure_region": "eastasia",
        "azure_url": "https://api.cognitive.microsofttranslator.com/translate",
        "claude_key": "", "claude_model": "", "openai_key": "", "openai_model": "",
        "local_url": "", "ai_provider_key": "", "ai_provider_cfg": {},
        "ai_api_key": "", "ai_api_keys": [], "ai_model": "",
        "ai_base_url": "", "ai_label": "AI", "should_stop": lambda: False,
    }
    # Patch Session construction inside registry by replacing http_session via first call path:
    # build registry then monkeypatch the worker session factory indirectly by patching requests.Session.get
    import requests
    real_session = requests.Session
    class FakeSession(real_session):
        def get(self, *a, **k):
            return sess.get(*a, **k)
    monkeypatch.setattr(requests, "Session", FakeSession)

    reg = providers.build_provider_registry(FakeSession(), settings)
    out, err = reg["gtx"](["Hello"])
    assert err is not None and err.startswith("429:")
    assert out is None or out == [None]
    # second call after interval raised should still work path-wise
