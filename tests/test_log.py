import json
import logging

import pytest

from scout.log import JsonFormatter, get_logger


def _format(record_extra: dict[str, object]) -> dict[str, object]:
    record = logging.LogRecord("scout.test", logging.INFO, __file__, 1, "hello", None, None)
    for k, v in record_extra.items():
        setattr(record, k, v)
    parsed: dict[str, object] = json.loads(JsonFormatter().format(record))
    return parsed


def test_json_log_has_core_fields_and_extras() -> None:
    out = _format({"source": "arxiv", "items": 3})
    assert out["msg"] == "hello"
    assert out["level"] == "INFO"
    assert out["logger"] == "scout.test"
    assert out["source"] == "arxiv"
    assert out["items"] == 3
    assert "ts" in out


@pytest.mark.parametrize("key", ["api_key", "github_token", "Authorization", "password"])
def test_secret_like_fields_are_redacted(key: str) -> None:
    out = _format({key: "sk-very-secret"})
    assert out[key] == "***"


def test_get_logger_is_namespaced() -> None:
    assert get_logger("ingest").name == "scout.ingest"
