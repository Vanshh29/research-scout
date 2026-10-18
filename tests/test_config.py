from pathlib import Path

import pytest
from pydantic import ValidationError

from scout.config import Profile, Settings, SourcesConfig, load_profile, load_sources

CONFIG_DIR = Path(__file__).parent.parent / "config"


def test_repo_sources_config_is_valid() -> None:
    cfg = load_sources(CONFIG_DIR / "sources.yaml")
    assert "cs.CL" in cfg.arxiv.categories
    assert cfg.arxiv.keywords
    assert len(cfg.rss.feeds) >= 1
    assert all(str(f.url).startswith("https://") for f in cfg.rss.feeds)


def test_repo_profile_is_valid() -> None:
    profile = load_profile(CONFIG_DIR / "profile.yaml")
    assert profile.topics
    assert profile.weights.embedding + profile.weights.llm == pytest.approx(1.0)


def test_ranking_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(
            {"topics": ["rag"], "weights": {"embedding": 0.8, "llm": 0.8}, "top_k": 10}
        )


def test_rss_feed_url_must_be_http() -> None:
    with pytest.raises(ValidationError):
        SourcesConfig.model_validate(
            {
                "arxiv": {"categories": ["cs.CL"], "keywords": ["rag"]},
                "rss": {"feeds": [{"name": "bad", "url": "file:///etc/passwd"}]},
            }
        )


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("SCOUT_LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None)
    assert settings.db_path == tmp_path / "x.db"
    assert settings.log_level == "DEBUG"
