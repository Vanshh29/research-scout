"""Build the configured sources. Adding a source means one file plus one line here."""

from scout.config import Settings, SourcesConfig
from scout.ingest.arxiv import ArxivSource
from scout.ingest.base import Source
from scout.ingest.github import GithubSource


def build_sources(cfg: SourcesConfig, settings: Settings) -> list[Source]:
    retries = cfg.http.max_retries
    return [
        ArxivSource(cfg.arxiv, max_retries=retries),
        GithubSource(cfg.github, token=settings.github_token, max_retries=retries),
    ]
