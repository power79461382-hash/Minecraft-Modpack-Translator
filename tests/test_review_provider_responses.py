import json
from types import SimpleNamespace

import pytest

import translator_providers as providers


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        return self.responses.pop(0)


def response(payload, status=200, text="", headers=None):
    return SimpleNamespace(status_code=status, text=text, headers=headers or {},
                           json=lambda: payload)


def translate(session, sources, parse):
    return providers.ai_chunk(session, sources, "https://example.invalid/chat",
                              {}, lambda data: data, parse, "test")


def test_null_model_content_is_recoverable_error():
    session = Session([response({"choices": [{"message": {"content": None}}]})])
    result, error = translate(session, ["Stone"],
                              lambda payload: payload["choices"][0]["message"]["content"])
    assert result is None
    assert error.startswith("ERR:")


@pytest.mark.parametrize("payload,parse", [
    ({"stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"0":"石頭"}'}]},
     lambda payload: providers.extract_ai_text(payload["content"])),
    ({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [
        {"text": '{"0":"石頭"}'}]}}]},
     lambda payload: providers.extract_ai_text(payload["candidates"][0]["content"]["parts"])),
])
def test_anthropic_and_gemini_truncation_does_not_accept_partial_translation(payload, parse):
    result, error = translate(Session([response(payload)]), ["Stone"], parse)
    assert result is None
    assert error.startswith("ERR:")


def test_ai_respects_retry_after_header():
    result, error = translate(Session([response({}, status=429, headers={"Retry-After": "120"})]),
                              ["Stone"], lambda payload: "")
    assert result is None
    assert error == "429:120.0"


def test_ai_error_response_redacts_auth_key():
    key = "synthetic-only-secret"
    session = Session([response({}, status=400, text=f"bad request token={key}")])
    result, error = providers.ai_chunk(session, ["Stone"], "https://example.invalid/chat",
                                       {"Authorization": f"Bearer {key}"},
                                       lambda data: data, lambda payload: "", "test")
    assert result is None
    assert error.startswith("ERR:")
    assert key not in error
    assert "[REDACTED]" in error


def test_google_transport_error_redacts_url_key(monkeypatch):
    key = "synthetic-only-google-secret"
    class BrokenSession:
        headers = {}
        def mount(self, *args, **kwargs):
            pass
        def post(self, url, **kwargs):
            raise providers.requests.ConnectionError(f"failed URL {url}")
    monkeypatch.setattr(providers.requests, "Session", BrokenSession)
    registry = providers.build_provider_registry(SimpleNamespace(headers={}), {"google_key": key})
    result, error = registry["google_api"](["Stone"])
    assert result is None
    assert error.startswith("ERR:")
    assert key not in error


def test_missing_batch_row_can_recover_with_source_specific_split_requests():
    session = Session([
        response({"choices": [{"message": {"content": '{"0":"石頭"}'}}]}),
        response({"choices": [{"message": {"content": '{"0":"石頭"}'}}]}),
        response({"choices": [{"message": {"content": '{"0":"泥土"}'}}]}),
    ])
    result, error = translate(session, ["Stone", "Dirt"],
                              lambda payload: payload["choices"][0]["message"]["content"])
    assert error is None
    assert result == ["石頭", "泥土"]
    assert session.calls == [{"0": "Stone", "1": "Dirt"}, {"0": "Stone"}, {"0": "Dirt"}]


def test_cancelled_ai_chunk_does_not_send_request():
    session = Session([])
    result, error = providers.ai_chunk(session, ["Stone"], "https://example.invalid/chat",
                                      {}, lambda data: data, lambda payload: "", "test",
                                      should_stop=lambda: True)
    assert result == [None]
    assert error is None
    assert session.calls == []


def test_cancel_during_truncation_prevents_recursive_requests():
    session = Session([response({"choices": [{"finish_reason": "length", "message": {
        "content": '{"0":"partial"}'}}]})])
    result, error = providers.ai_chunk(session, ["Stone", "Dirt"],
                                      "https://example.invalid/chat", {}, lambda data: data,
                                      lambda payload: payload["choices"][0]["message"]["content"],
                                      "test", should_stop=lambda: bool(session.calls))
    assert result == [None, None]
    assert error is None
    assert session.calls == [{"0": "Stone", "1": "Dirt"}]
