"""Local embedders. FastEmbed (ONNX, no torch) for real runs; a hash embedder for offline tests."""

import hashlib
import re
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

Vector = NDArray[np.float32]


class Embedder(Protocol):
    name: str
    # cosine range mapped to 0..1 by the embedding score (model-specific baselines differ)
    score_range: tuple[float, float]

    def embed(self, texts: list[str]) -> list[Vector]: ...


def _normalize(v: NDArray[np.floating]) -> Vector:
    norm = float(np.linalg.norm(v))
    out = v / norm if norm > 0 else v
    return out.astype(np.float32)


class FastEmbedder:
    name = "BAAI/bge-small-en-v1.5"
    score_range = (0.45, 0.80)

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # lazy: model loads on first use

        self._model = TextEmbedding(self.name)

    def embed(self, texts: list[str]) -> list[Vector]:
        return [_normalize(np.asarray(v)) for v in self._model.embed(texts)]


class HashEmbedder:
    """Bag-of-words hashed into 384 dims: deterministic, offline, shares-words => similar."""

    name = "hash-384"
    score_range = (0.0, 0.5)
    dim = 384

    def embed(self, texts: list[str]) -> list[Vector]:
        out = []
        for text in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for word in re.findall(r"[a-z0-9]{3,}", text.lower()):
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
            out.append(_normalize(v))
        return out
