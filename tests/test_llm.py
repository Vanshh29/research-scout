import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from scout.config import LLMConfig, Settings
from scout.llm.anthropic import AnthropicProvider
from scout.llm.base import BudgetExceeded, LLMError
from scout.llm.client import LLMClient, cache_key, make_provider
from scout.llm.fake import FakeProvider
from scout.llm.gemini import GeminiProvider
from scout.llm.ratelimit import RateLimiter
from scout.store.db import connect, migrate


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def make_client(
    conn: sqlite3.Connection, provider: FakeProvider | None = None, **cfg: object
) -> tuple[LLMClient, FakeProvider]:
    fake = provider or FakeProvider()
    config = LLMConfig(requests_per_minute=0, **cfg)  # type: ignore[arg-type]
    return LLMClient(conn, fake, config), fake


def call(client: LLMClient, content_hash: str = "h1", prompt: str = "hello") -> str:
    return client.complete(
        system="sys", prompt=prompt, prompt_version="v1", content_hash=content_hash
    ).text


def test_cache_hit_skips_provider_and_cost(conn: sqlite3.Connection) -> None:
    client, fake = make_client(conn)
    first = call(client)
    second = call(client)
    assert first == second
    assert fake.calls == 1
    assert client.calls == 1 and client.cache_hits == 1


def test_cache_key_depends_on_prompt_version_model_and_hash() -> None:
    base = cache_key("v1", "m", "h")
    assert base != cache_key("v2", "m", "h")
    assert base != cache_key("v1", "m2", "h")
    assert base != cache_key("v1", "m", "h2")


def test_cache_persists_across_clients(conn: sqlite3.Connection) -> None:
    call(make_client(conn)[0])
    client2, fake2 = make_client(conn)
    call(client2)
    assert fake2.calls == 0


def test_budget_guard_blocks_and_flags(conn: sqlite3.Connection) -> None:
    long = FakeProvider(responder=lambda _s, _p: "x" * 2000)  # ~500 output tokens
    client, fake = make_client(conn, long, daily_token_budget=1000, max_output_tokens=700)
    call(client, "h1")  # estimate ~700 < 1000, runs and records ~500 used
    with pytest.raises(BudgetExceeded):
        call(client, "h2")
    assert client.budget_hit
    assert fake.calls == 1


def test_budget_blocks_on_cost(conn: sqlite3.Connection) -> None:
    client, fake = make_client(
        conn, daily_cost_budget_usd=0.0001, price_in_per_mtok=1000.0, max_output_tokens=700
    )
    with pytest.raises(BudgetExceeded):
        call(client)
    assert fake.calls == 0


def test_usage_accumulates_into_run_totals(conn: sqlite3.Connection) -> None:
    client, _ = make_client(conn)
    call(client, "h1")
    call(client, "h2")
    tokens_today, _ = client.spent_today()
    assert tokens_today == client.tokens_used > 0


def test_rate_limiter_spaces_calls() -> None:
    now = [0.0]
    slept: list[float] = []

    def fake_sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(30, clock=lambda: now[0], sleep=fake_sleep)  # one call per 2s
    limiter.wait()
    limiter.wait()
    limiter.wait()
    assert slept == [2.0, 2.0]


def gemini_ok(request: httpx.Request) -> httpx.Response:
    assert request.headers["x-goog-api-key"] == "secret"
    assert "secret" not in str(request.url)
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": "hi "}, {"text": "there"}]}}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3},
        },
    )


def test_gemini_parses_response_and_keeps_key_out_of_url() -> None:
    client = httpx.Client(transport=httpx.MockTransport(gemini_ok))
    p = GeminiProvider(api_key="secret", model="m", client=client)
    r = p.complete(system="s", prompt="p", max_output_tokens=10)
    assert (r.text, r.input_tokens, r.output_tokens) == ("hi there", 11, 3)


def test_gemini_retries_429_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429) if attempts["n"] == 1 else gemini_ok(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = GeminiProvider(api_key="secret", model="m", client=client, sleep=lambda _: None)
    assert p.complete(system="s", prompt="p", max_output_tokens=10).text == "hi there"
    assert attempts["n"] == 2


def test_gemini_400_is_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    p = GeminiProvider(
        api_key="k",
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(LLMError):
        p.complete(system="s", prompt="p", max_output_tokens=10)
    assert attempts["n"] == 1


def test_gemini_malformed_response_raises() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(LLMError):
        GeminiProvider(api_key="k", model="m", client=client).complete(
            system="s", prompt="p", max_output_tokens=10
        )


def test_anthropic_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["system"] == "s"
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    p = AnthropicProvider(
        api_key="k", model="m", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    r = p.complete(system="s", prompt="p", max_output_tokens=10)
    assert (r.text, r.input_tokens, r.output_tokens) == ("ok", 5, 2)


def test_make_provider_requires_key_and_supports_fake() -> None:
    cfg = LLMConfig()
    assert make_provider(Settings(SCOUT_LLM_PROVIDER="fake"), cfg).name == "fake"  # type: ignore[call-arg]
    with pytest.raises(LLMError):
        make_provider(Settings(SCOUT_LLM_PROVIDER="gemini", GEMINI_API_KEY=None), cfg)  # type: ignore[call-arg]
