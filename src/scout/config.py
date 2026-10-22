"""Runtime settings (env) and YAML config (sources, interest profile)."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Secrets and paths, read from the environment / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_path: Path = Field(default=Path("data/scout.db"), alias="SCOUT_DB_PATH")
    log_level: str = Field(default="INFO", alias="SCOUT_LOG_LEVEL")
    config_dir: Path = Field(default=Path("config"), alias="SCOUT_CONFIG_DIR")
    prompts_dir: Path = Field(default=Path("prompts"), alias="SCOUT_PROMPTS_DIR")
    llm_provider: Literal["gemini", "anthropic", "fake"] = Field(
        default="gemini", alias="SCOUT_LLM_PROVIDER"
    )
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    n8n_webhook_url: str | None = Field(default=None, alias="SCOUT_N8N_WEBHOOK_URL")


class HttpConfig(BaseModel):
    user_agent: str = "research-scout/0.1"
    timeout_s: float = 30.0
    max_retries: int = Field(default=4, ge=0, le=10)


class ArxivConfig(BaseModel):
    categories: list[str]
    keywords: list[str]
    max_results: int = Field(default=200, ge=1, le=2000)


class GithubConfig(BaseModel):
    topics: list[str] = []
    min_stars: int = 20
    created_within_days: int = 30


class RssFeed(BaseModel):
    name: str
    url: HttpUrl


class RssConfig(BaseModel):
    feeds: list[RssFeed] = []
    max_age_days: int = Field(default=30, ge=1)
    max_items_per_feed: int = Field(default=50, ge=1)


class SourcesConfig(BaseModel):
    http: HttpConfig = HttpConfig()
    arxiv: ArxivConfig
    github: GithubConfig = GithubConfig()
    rss: RssConfig = RssConfig()


class Weights(BaseModel):
    embedding: float = Field(ge=0, le=1)
    llm: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> "Weights":
        if abs(self.embedding + self.llm - 1.0) > 1e-6:
            raise ValueError("ranking weights must sum to 1")
        return self


class Profile(BaseModel):
    topics: list[str]
    seed_papers: list[str] = []
    seed_repos: list[str] = []
    weights: Weights
    rubric: dict[str, float] = {}
    top_k: int = Field(default=25, ge=1)
    top_n: int = Field(default=8, ge=1)


class LLMConfig(BaseModel):
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_strong_model: str = "gemini-3.8-flash"
    anthropic_model: str = "claude-haiku-5-5"
    anthropic_strong_model: str = "claude-sonnet-5-5"
    requests_per_minute: float = Field(default=10, ge=0)
    max_output_tokens: int = Field(default=1000, ge=16)
    daily_token_budget: int = Field(default=200_000, ge=1)
    daily_cost_budget_usd: float = Field(default=0.50, ge=0)
    price_in_per_mtok: float = Field(default=0.0, ge=0)
    price_out_per_mtok: float = Field(default=0.0, ge=0)


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_sources(path: Path) -> SourcesConfig:
    return SourcesConfig.model_validate(_read_yaml(path))


def load_profile(path: Path) -> Profile:
    return Profile.model_validate(_read_yaml(path))


def load_llm_config(path: Path) -> LLMConfig:
    return LLMConfig.model_validate(_read_yaml(path))
