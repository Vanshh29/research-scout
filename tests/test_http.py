import httpx
import pytest

from scout.ingest.http import FetchError, backoff_delay, get_with_retry, make_client


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_backoff_delay_is_bounded_and_grows() -> None:
    caps = [backoff_delay(a, base=1.0, cap=30.0, rand=lambda: 1.0) for a in range(6)]
    assert caps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert backoff_delay(3, base=1.0, cap=30.0, rand=lambda: 0.0) == 0.0


def test_retries_on_503_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, text="ok")

    sleeps: list[float] = []
    resp = get_with_retry(
        _client(httpx.MockTransport(handler)), "https://x.test", max_retries=4, sleep=sleeps.append
    )
    assert resp.text == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_honors_retry_after_header() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200)

    sleeps: list[float] = []
    get_with_retry(_client(httpx.MockTransport(handler)), "https://x.test", sleep=sleeps.append)
    assert sleeps == [7.0]


def test_does_not_retry_client_errors() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(FetchError):
        get_with_retry(
            _client(httpx.MockTransport(handler)), "https://x.test", sleep=lambda s: None
        )
    assert calls["n"] == 1


def test_gives_up_after_max_retries_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(FetchError):
        get_with_retry(
            _client(httpx.MockTransport(handler)),
            "https://x.test",
            max_retries=2,
            sleep=lambda s: None,
        )


def test_make_client_sets_user_agent() -> None:
    client = make_client(user_agent="research-scout/test", timeout_s=5)
    assert client.headers["User-Agent"] == "research-scout/test"


def test_per_request_headers_are_sent() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Test"))
        return httpx.Response(200)

    get_with_retry(_client(httpx.MockTransport(handler)), "https://x.test", headers={"X-Test": "1"})
    assert seen == ["1"]
