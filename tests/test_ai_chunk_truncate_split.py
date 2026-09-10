# -*- coding: utf-8 -*-
import json
import translator_providers as providers


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json)
        if not self.responses:
            raise AssertionError("unexpected extra POST")
        return self.responses.pop(0)


def _body(chunk_dict):
    return {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": str(chunk_dict)}]}


def _parse(payload):
    return payload["choices"][0]["message"]["content"]


def test_ai_chunk_splits_on_length_finish_reason():
    # First call: 4 items truncated; then two successful halves.
    trunc = FakeResp(200, {
        "choices": [{
            "finish_reason": "length",
            "message": {"content": "{\"0\": \"a\""},
        }]
    })
    ok_left = FakeResp(200, {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps({"0": "甲", "1": "乙"}, ensure_ascii=False)},
        }]
    })
    ok_right = FakeResp(200, {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps({"0": "丙", "1": "丁"}, ensure_ascii=False)},
        }]
    })
    session = FakeSession([trunc, ok_left, ok_right])
    out, err = providers.ai_chunk(
        session, ["a", "b", "c", "d"], "https://example/v1/chat/completions",
        {"Authorization": "Bearer x"}, _body, _parse, "DeepSeek",
        request_timeout=(5, 30), max_batch=20)
    assert err is None
    assert out == ["甲", "乙", "丙", "丁"]
    assert len(session.calls) == 3


def test_model_batch_limit_deepseek_is_small():
    assert providers.model_batch_limit("deepseek-v4-flash") == 16
