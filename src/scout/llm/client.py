"""LLMClient: cache -> budget guard -> rate limit -> provider, with usage accounting."""

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from scout.config import LLMConfig, Settings
from scout.llm.anthropic import AnthropicProvider
from scout.llm.base import BudgetExceeded, LLMError, LLMProvider, LLMResult, estimate_tokens
from scout.llm.fake import FakeProvider
from scout.llm.gemini import GeminiProvider
from scout.llm.ratelimit import RateLimiter
from scout.log import get_logger

log = get_logger("llm")


def cache_key(prompt_version: str, model: str, content_hash: str) -> str:
    return hashlib.sha256(f"{prompt_version}|{model}|{content_hash}".encode()).hexdigest()


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class LLMClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: LLMProvider,
        config: LLMConfig,
        *,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._conn = conn
        self._provider = provider
        self._config = config
        # the offline provider has no quota to respect
        rpm = 0 if provider.name == "fake" else config.requests_per_minute
        self._limiter = limiter or RateLimiter(rpm)
        self.tokens_used = 0
        self.cost_usd = 0.0
        self.calls = 0
        self.cache_hits = 0
        self.budget_hit = False

    @property
    def model(self) -> str:
        return self._provider.model

    def spent_today(self) -> tuple[int, float]:
        row = self._conn.execute(
            """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t,
                      COALESCE(SUM(cost_usd), 0) AS c FROM llm_usage WHERE day = ?""",
            (_today(),),
        ).fetchone()
        return int(row["t"]), float(row["c"])

    def is_cached(self, prompt_version: str, content_hash: str) -> bool:
        key = cache_key(prompt_version, self._provider.model, content_hash)
        row = self._conn.execute("SELECT 1 FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()
        return row is not None

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        c = self._config
        return (input_tokens * c.price_in_per_mtok + output_tokens * c.price_out_per_mtok) / 1e6

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        prompt_version: str,
        content_hash: str,
        validate: Callable[[str], object] | None = None,
    ) -> LLMResult:
        """Return a cached response if present; otherwise call the provider within budget.

        If `validate` is given, a reply is cached only after it passes (it may raise
        ValueError), so a malformed reply is paid for once but never replayed from cache."""
        key = cache_key(prompt_version, self._provider.model, content_hash)
        row = self._conn.execute("SELECT * FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()
        if row is not None:
            self.cache_hits += 1
            return LLMResult(
                text=row["response"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                model=row["model"],
                cached=True,
            )

        max_out = self._config.max_output_tokens
        estimate = estimate_tokens(system + prompt) + max_out
        tokens_today, cost_today = self.spent_today()
        if (
            tokens_today + estimate > self._config.daily_token_budget
            or cost_today + self._cost(estimate, 0) > self._config.daily_cost_budget_usd
        ):
            self.budget_hit = True
            log.warning("daily budget reached", extra={"tokens_today": tokens_today})
            raise BudgetExceeded(f"daily budget reached ({tokens_today} tokens used)")

        self._limiter.wait()
        result = self._provider.complete(system=system, prompt=prompt, max_output_tokens=max_out)
        cost = self._cost(result.input_tokens, result.output_tokens)
        valid = True
        if validate is not None:
            try:
                validate(result.text)
            except ValueError:
                valid = False
        with self._conn:
            if valid:
                self._conn.execute(
                    """INSERT OR REPLACE INTO llm_cache (cache_key, prompt_version, model,
                           content_hash, response, input_tokens, output_tokens, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        prompt_version,
                        result.model,
                        content_hash,
                        result.text,
                        result.input_tokens,
                        result.output_tokens,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            self._conn.execute(
                """INSERT INTO llm_usage (day, model, input_tokens, output_tokens, cost_usd)
                   VALUES (?, ?, ?, ?, ?)""",
                (_today(), result.model, result.input_tokens, result.output_tokens, cost),
            )
        self.calls += 1
        self.tokens_used += result.input_tokens + result.output_tokens
        self.cost_usd += cost
        return result


def make_provider(settings: Settings, config: LLMConfig, *, strong: bool = False) -> LLMProvider:
    """Build the configured provider; raises LLMError if its API key is missing."""
    if settings.llm_provider == "fake":
        return FakeProvider()
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        model = config.gemini_strong_model if strong else config.gemini_model
        return GeminiProvider(api_key=settings.gemini_api_key, model=model)
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    model = config.anthropic_strong_model if strong else config.anthropic_model
    return AnthropicProvider(api_key=settings.anthropic_api_key, model=model)
