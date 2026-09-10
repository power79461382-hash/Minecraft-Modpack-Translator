import concurrent.futures
import threading
import time
from types import SimpleNamespace

import pytest

import translator_providers as providers


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


class GTXRequestTracker:
    def __init__(self, response_for_singleton):
        self.response_for_singleton = response_for_singleton
        self.lock = threading.Lock()
        self.active_singletons = 0
        self.peak_singletons = 0
        self.session_count = 0


class TrackingGTXSession:
    def __init__(self, tracker):
        self.headers = {}
        self.tracker = tracker

    def mount(self, *args, **kwargs):
        pass

    def get(self, url, params=None, timeout=None):
        query = params["q"]
        if "[[MCT" in query:
            return FakeGTXResponse(translated_text="numbered markers removed")

        with self.tracker.lock:
            self.tracker.active_singletons += 1
            self.tracker.peak_singletons = max(
                self.tracker.peak_singletons,
                self.tracker.active_singletons)
        try:
            time.sleep(0.01)
            return self.tracker.response_for_singleton(query)
        finally:
            with self.tracker.lock:
                self.tracker.active_singletons -= 1


def make_gtx(monkeypatch, response_for_singleton, workers=3):
    tracker = GTXRequestTracker(response_for_singleton)

    def session_factory():
        with tracker.lock:
            tracker.session_count += 1
        return TrackingGTXSession(tracker)

    monkeypatch.setattr(providers, "GTX_GATE_INTERVAL", 0)
    monkeypatch.setattr(providers, "GTX_SINGLETON_WORKERS", workers)
    monkeypatch.setattr(providers.requests, "Session", session_factory)
    registry = providers.build_provider_registry(
        SimpleNamespace(headers={}),
        {"should_stop": lambda: False},
    )
    return registry["gtx"], tracker


def test_gtx_depth_cap_runs_singletons_in_bounded_parallel_and_keeps_order(
        monkeypatch):
    def response_for_singleton(query):
        if query == "item-37":
            return FakeGTXResponse(json_error=ValueError("malformed response"))
        return FakeGTXResponse(translated_text=f"zh:{query}")

    gtx, tracker = make_gtx(monkeypatch, response_for_singleton, workers=3)
    sources = [f"item-{index}" for index in range(64)]

    result, error = gtx(sources)

    expected = [f"zh:{source}" for source in sources]
    expected[37] = None
    assert error is None
    assert result == expected
    assert 2 <= tracker.peak_singletons <= 3


def test_gtx_parallel_singletons_keep_rate_limit_error_semantics(monkeypatch):
    def response_for_singleton(query):
        if query == "item-37":
            return FakeGTXResponse(status_code=429)
        return FakeGTXResponse(translated_text=f"zh:{query}")

    gtx, tracker = make_gtx(monkeypatch, response_for_singleton, workers=3)
    sources = [f"item-{index}" for index in range(64)]

    result, error = gtx(sources)

    assert result == [None] * len(sources)
    assert error.startswith("429:")
    assert 2 <= tracker.peak_singletons <= 3


def test_gtx_registry_caps_real_singleton_http_across_outer_calls(monkeypatch):
    workers = 3
    outer_calls = 6
    gtx, tracker = make_gtx(
        monkeypatch,
        lambda query: FakeGTXResponse(translated_text=f"zh:{query}"),
        workers=workers,
    )

    def translate_outer(outer_index):
        return gtx([f"{outer_index}-{index}" for index in range(64)])

    with concurrent.futures.ThreadPoolExecutor(max_workers=outer_calls) as executor:
        outcomes = list(executor.map(translate_outer, range(outer_calls)))

    assert all(error is None for _result, error in outcomes)
    assert 2 <= tracker.peak_singletons <= workers


def test_gtx_registry_reuses_singleton_worker_sessions(monkeypatch):
    workers = 3
    gtx, tracker = make_gtx(
        monkeypatch,
        lambda query: FakeGTXResponse(translated_text=f"zh:{query}"),
        workers=workers,
    )

    result, error = gtx([f"item-{index}" for index in range(64)])

    assert error is None
    assert len(result) == 64
    # One caller session handles marker batches; shared singleton workers reuse
    # at most one session each across every depth-capped leaf.
    assert tracker.session_count <= 1 + workers


def test_gtx_capacity_wait_does_not_release_expired_slots_as_burst(monkeypatch):
    request_times = []
    request_lock = threading.Lock()

    class SlowFirstSession:
        headers = {}

        def mount(self, *args, **kwargs):
            pass

        def get(self, url, params=None, timeout=None):
            with request_lock:
                request_times.append(time.monotonic())
                request_number = len(request_times)
            if request_number == 1:
                time.sleep(0.12)
            return FakeGTXResponse(translated_text=f"zh:{params['q']}")

    monkeypatch.setattr(providers, "GTX_SINGLETON_WORKERS", 1)
    monkeypatch.setattr(providers, "GTX_GATE_INTERVAL", 0.03)
    monkeypatch.setattr(providers.requests, "Session", SlowFirstSession)
    gtx = providers.build_provider_registry(
        SimpleNamespace(headers={}),
        {"should_stop": lambda: False},
    )["gtx"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(lambda value: gtx([value]), ["a", "b", "c"]))

    assert all(error is None for _result, error in outcomes)
    assert len(request_times) == 3
    # The pre-fix implementation released both queued calls almost together.
    # Keep tolerance below one Windows timer tick so loaded CI stays stable.
    assert request_times[2] - request_times[1] >= 0.005


@pytest.mark.parametrize("status_code", [403, 500, 502, 503, 504])
def test_gtx_batch_http_failure_fails_fast_without_recursive_fanout(
        monkeypatch, status_code):
    request_count = 0

    class FailureResponse:
        text = ""

        def __init__(self):
            self.status_code = status_code

        def json(self):
            return {}

    class FailureSession:
        headers = {}

        def mount(self, *args, **kwargs):
            pass

        def get(self, url, params=None, timeout=None):
            nonlocal request_count
            request_count += 1
            return FailureResponse()

    monkeypatch.setattr(providers, "GTX_GATE_INTERVAL", 0)
    monkeypatch.setattr(providers.requests, "Session", FailureSession)
    gtx = providers.build_provider_registry(
        SimpleNamespace(headers={}),
        {"should_stop": lambda: False},
    )["gtx"]

    sources = [f"item-{index}" for index in range(80)]
    result, error = gtx(sources)

    assert result == [None] * len(sources)
    assert error == f"ERR:GTX HTTP {status_code}"
    assert request_count == 1
