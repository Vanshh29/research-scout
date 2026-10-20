"""Stage 2 of ranking: rubric score from the LLM for the top candidates."""

import json
import re

from pydantic import BaseModel, ValidationError, field_validator

from scout.config import Profile
from scout.process.sanitize import wrap_document

PROMPT_NAME = "score_v1"
DIMENSIONS = ("relevance", "novelty", "evidence_quality", "actionability")
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class Rubric(BaseModel):
    relevance: float
    novelty: float
    evidence_quality: float
    actionability: float
    reason: str = ""

    @field_validator(*DIMENSIONS, mode="before")
    @classmethod
    def _unit_interval(cls, v: object) -> object:
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise ValueError("score must be a number")
        return min(1.0, max(0.0, float(v)))

    @field_validator("reason", mode="before")
    @classmethod
    def _clip_reason(cls, v: object) -> object:
        return v.strip()[:240] if isinstance(v, str) else ""

    def weighted(self, weights: dict[str, float]) -> float:
        scores: dict[str, float] = {d: float(getattr(self, d)) for d in DIMENSIONS}
        total = sum(weights.get(d, 0.0) for d in DIMENSIONS)
        if total <= 0:
            return sum(scores.values()) / len(scores)
        return sum(scores[d] * weights.get(d, 0.0) for d in DIMENSIONS) / total


def parse_rubric(text: str) -> Rubric:
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return Rubric.model_validate(json.loads(cleaned))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid score JSON: {exc}") from exc


def build_score_prompt(
    profile: Profile, *, item_id: str, source: str, title: str, digest: str
) -> str:
    interests = "\n".join(f"- {t}" for t in profile.topics)
    doc = wrap_document(f"{title}\n\n{digest}", doc_id=item_id, source=source)
    return (
        f"Reader's interests:\n{interests}\n\nRate this item as the JSON object described.\n\n{doc}"
    )
