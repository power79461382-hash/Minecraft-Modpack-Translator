import concurrent.futures
import threading
from email.utils import formatdate
from types import SimpleNamespace

import core.batch_translation as batch_module
import translator_providers as providers


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self):
        self.headers = {}
        self.posts = []

    def mount(self, *_args, **_kwargs):
        pass

    def get(self, url, **_kwargs):
        assert url == "https://edge.microsoft.com/translate/auth"
        return FakeResponse(text="edge-token")

    def post(self, url, **kwargs):
        body = kwargs["json"]
        self.posts.append((url, body))
        translated = [
            {"translations": [{"text": f"zh:{item.get('Text', item.get('text'))}"}]}
            for item in body
        ]
        return FakeResponse(payload=translated)


def make_registry(monkeypatch, session, **settings):
    monkeypatch.setattr(providers.requests, "Session", lambda: session)
    return providers.build_provider_registry(
        SimpleNamespace(headers={}),
        {"should_stop": lambda: False, **settings},
    )


def test_bing_edge_token_is_sent_to_edge_translator_host(monkeypatch):
    session = RecordingSession()
    bing = make_registry(monkeypatch, session)["bing"]

    result, error = bing(["hello"])

    assert error is None
    assert result == ["zh:hello"]
    assert session.posts[0][0] == (
        "https://api-edge.cognitive.microsofttranslator.com/translate"
    )


def test_bing_auth_rejection_refreshes_token_and_retries_immediately(monkeypatch):
    class RefreshingSession(RecordingSession):
        def __init__(self):
            super().__init__()
            self.get_calls = 0
            self.authorization = []

        def get(self, url, **kwargs):
            self.get_calls += 1
            return FakeResponse(text=f"edge-token-{self.get_calls}")

        def post(self, url, **kwargs):
            authorization = kwargs["headers"]["Authorization"]
            self.authorization.append(authorization)
            if authorization.endswith("edge-token-1"):
                return FakeResponse(status_code=401)
            body = kwargs["json"]
            return FakeResponse(payload=[
                {"translations": [{"text": f"zh:{item['Text']}"}]}
                for item in body
            ])

    session = RefreshingSession()
    bing = make_registry(monkeypatch, session)["bing"]

    result, error = bing(["hello"])

    assert error is None
    assert result == ["zh:hello"]
    assert session.get_calls == 2
    assert session.authorization == [
        "Bearer edge-token-1",
        "Bearer edge-token-2",
    ]


def test_bing_refresh_transport_failure_does_not_permanently_disable(monkeypatch):
    class RecoveringSession(RecordingSession):
        def __init__(self):
            super().__init__()
            self.get_calls = 0

        def get(self, url, **kwargs):
            self.get_calls += 1
            if self.get_calls == 2:
                raise providers.requests.ConnectionError("temporary refresh failure")
            return FakeResponse(text=f"edge-token-{self.get_calls}")

        def post(self, url, **kwargs):
            authorization = kwargs["headers"]["Authorization"]
            if authorization.endswith("edge-token-1"):
                return FakeResponse(status_code=401)
            body = kwargs["json"]
            return FakeResponse(payload=[
                {"translations": [{"text": f"zh:{item['Text']}"}]}
                for item in body
            ])

    session = RecoveringSession()
    bing = make_registry(monkeypatch, session)["bing"]

    first_result, first_error = bing(["first"])
    second_result, second_error = bing(["second"])

    assert first_result is None
    assert first_error.startswith("ERR:")
    assert second_error is None
    assert second_result == ["zh:second"]
    assert session.get_calls == 3


def test_bing_two_fresh_tokens_rejected_disables_without_rate_limit(monkeypatch):
    class RejectingSession(RecordingSession):
        def __init__(self):
            super().__init__()
            self.get_calls = 0

        def get(self, url, **kwargs):
            self.get_calls += 1
            return FakeResponse(text=f"edge-token-{self.get_calls}")

        def post(self, url, **kwargs):
            return FakeResponse(status_code=403)

    session = RejectingSession()
    bing = make_registry(monkeypatch, session)["bing"]

    result, error = bing(["hello"])

    assert result is None
    assert error.startswith("DISABLED:Bing")
    assert "限流" not in error
    assert session.get_calls == 2


def test_bing_concurrent_auth_rejections_share_one_token_refresh(monkeypatch):
    class ConcurrentRefreshSession(RecordingSession):
        def __init__(self):
            super().__init__()
            self.get_calls = 0
            self.lock = threading.Lock()
            self.first_wave = threading.Barrier(2, timeout=3)

        def get(self, url, **kwargs):
            with self.lock:
                self.get_calls += 1
                token_number = self.get_calls
            return FakeResponse(text=f"edge-token-{token_number}")

        def post(self, url, **kwargs):
            authorization = kwargs["headers"]["Authorization"]
            if authorization.endswith("edge-token-1"):
                self.first_wave.wait()
                return FakeResponse(status_code=401)
            body = kwargs["json"]
            return FakeResponse(payload=[
                {"translations": [{"text": f"zh:{item['Text']}"}]}
                for item in body
            ])

    session = ConcurrentRefreshSession()
    bing = make_registry(monkeypatch, session)["bing"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda text: bing([text]), ["one", "two"]))

    assert outcomes == [(["zh:one"], None), (["zh:two"], None)]
    assert session.get_calls == 2


def test_azure_splits_large_fallback_batches_by_items_and_characters(monkeypatch):
    session = RecordingSession()
    azure = make_registry(
        monkeypatch,
        session,
        azure_key="test-key",
        azure_region="eastasia",
        azure_url="https://api.cognitive.microsofttranslator.com/translate",
    )["azure"]
    sources = [f"{index}:" + ("x" * 500) for index in range(101)]

    result, error = azure(sources)

    assert error is None
    assert result == [f"zh:{source}" for source in sources]
    assert len(session.posts) > 1
    for _url, body in session.posts:
        assert len(body) <= 100
        assert sum(len(item["text"]) for item in body) <= 45_000


def test_azure_malformed_success_is_classified_for_fallback(monkeypatch):
    class MalformedSession(RecordingSession):
        def post(self, url, **kwargs):
            return FakeResponse(payload=[{"translations": []}])

    azure = make_registry(
        monkeypatch,
        MalformedSession(),
        azure_key="test-key",
        azure_region="eastasia",
        azure_url="https://example.test/translate",
    )["azure"]

    result, error = azure(["one"])

    assert result is None
    assert error.startswith("ERR:Azure incomplete translation response")


def test_azure_missing_items_is_classified_for_fallback(monkeypatch):
    class MissingItemSession(RecordingSession):
        def post(self, url, **kwargs):
            return FakeResponse(payload=[
                {"translations": [{"text": "zh:one"}]},
            ])

    azure = make_registry(
        monkeypatch,
        MissingItemSession(),
        azure_key="test-key",
        azure_region="eastasia",
        azure_url="https://example.test/translate",
    )["azure"]

    result, error = azure(["one", "two"])

    assert result is None
    assert error.startswith("ERR:Azure incomplete translation response")


def test_retry_after_supports_seconds_http_date_and_invalid_values():
    now = 1_700_000_000.0
    http_date = formatdate(now + 30, usegmt=True)

    assert providers.parse_retry_after_seconds("12.5", now=now) == 12.5
    assert providers.parse_retry_after_seconds(http_date, now=now) == 30.0
    assert providers.parse_retry_after_seconds("not-a-date", default=10, now=now) == 10.0


def test_non_ai_fallback_order_is_gtx_only():
    assert batch_module.translation_fallback_order(
        "non_ai_chain",
        "gtx",
        azure_available=False,
    ) == ["gtx"]
