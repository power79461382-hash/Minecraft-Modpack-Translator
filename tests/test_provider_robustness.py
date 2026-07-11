import json
from types import SimpleNamespace

import translator_providers as providers


class FakeAIResponse:
    status_code = 200
    text = ""

    def __init__(self, content):
        self._content = content

    def json(self):
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": self._content},
            }]
        }


class FakeAISession:
    def __init__(self, content):
        self._content = content

    def post(self, *args, **kwargs):
        return FakeAIResponse(self._content)


class FakeGTXResponse:
    text = ""

    def __init__(self, status_code=200, translated_text=None, json_error=None):
        self.status_code = status_code
        self._translated_text = translated_text
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return [[[self._translated_text]]]


class FakeGTXSession:
    def __init__(self, singleton_response):
        self.headers = {}
        self.singleton_response = singleton_response
        self.singleton_queries = []

    def mount(self, *args, **kwargs):
        pass

    def get(self, url, params=None, timeout=None):
        query = params["q"]
        if "[[MCT" in query:
            return FakeGTXResponse(translated_text="numbered markers removed")
        self.singleton_queries.append(query)
        return self.singleton_response(query)


def call_ai_chunk(response_object, inputs):
    content = json.dumps(response_object)
    return providers.ai_chunk(
        FakeAISession(content),
        inputs,
        "https://example.invalid/v1/chat/completions",
        {},
        lambda chunk: chunk,
        lambda payload: payload["choices"][0]["message"]["content"],
        "FakeAI",
        max_batch=20,
    )


def make_gtx(monkeypatch, singleton_response):
    fake_session = FakeGTXSession(singleton_response)
    monkeypatch.setattr(providers, "GTX_GATE_INTERVAL", 0)
    monkeypatch.setattr(providers.requests, "Session", lambda: fake_session)
    registry = providers.build_provider_registry(
        SimpleNamespace(headers={}),
        {"should_stop": lambda: False},
    )
    return registry["gtx"], fake_session


def test_ai_chunk_rejects_missing_expected_numeric_key():
    result, error = call_ai_chunk({"0": "translated-0"}, ["source-0", "source-1"])

    assert result is None
    assert error.startswith("ERR:FakeAI")
    assert "1" in error


def test_ai_chunk_rejects_non_string_expected_value():
    result, error = call_ai_chunk({"0": 42}, ["source-0"])

    assert result is None
    assert error.startswith("ERR:FakeAI")
    assert "0" in error


def test_gtx_depth_cap_translates_every_item_in_order(monkeypatch):
    gtx, fake_session = make_gtx(
        monkeypatch,
        lambda query: FakeGTXResponse(translated_text=f"zh:{query}"),
    )
    sources = [f"item-{index}" for index in range(17)]

    result, error = gtx(sources)

    assert error is None
    assert result == [f"zh:{source}" for source in sources]
    assert fake_session.singleton_queries == sources


def test_gtx_depth_cap_preserves_none_slot_for_parse_failure(monkeypatch):
    def singleton_response(query):
        if query == "item-16":
            return FakeGTXResponse(json_error=ValueError("malformed response"))
        return FakeGTXResponse(translated_text=f"zh:{query}")

    gtx, fake_session = make_gtx(monkeypatch, singleton_response)
    sources = [f"item-{index}" for index in range(17)]

    result, error = gtx(sources)

    assert error is None
    assert result == [f"zh:{source}" for source in sources[:-1]] + [None]
    assert fake_session.singleton_queries == sources


def test_gtx_depth_cap_propagates_single_item_rate_limit(monkeypatch):
    def singleton_response(query):
        if query == "item-16":
            return FakeGTXResponse(status_code=429)
        return FakeGTXResponse(translated_text=f"zh:{query}")

    gtx, fake_session = make_gtx(monkeypatch, singleton_response)
    sources = [f"item-{index}" for index in range(17)]

    result, error = gtx(sources)

    assert result == [None] * len(sources)
    assert error == "429:30"
    assert fake_session.singleton_queries == sources
