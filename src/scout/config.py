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
    llm_provider: Literal["gemini", "anthropic", "fake"] = Field(
        default="gemini", alias="SCOUT_LLM_PROVIDER"
    )
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")


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


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_sources(path: Path) -> SourcesConfig:
    return SourcesConfig.model_validate(_read_yaml(path))


def load_profile(path: Path) -> Profile:
    return Profile.model_validate(_read_yaml(path))
