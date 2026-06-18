"""Shared helpers for embedding store backends."""

from __future__ import annotations

import math

# ChromaDB persists the embedding-function name with each collection. Both the
# LM Studio (OpenAI-compatible) and local GGUF backends report the SAME name so
# a collection stays reusable across a fallback switch. Do NOT change this value
# without a collection migration.
EMBEDDING_FUNCTION_NAME = "kure_v1"


def l2_normalize(v: list[float]) -> list[float]:
    """Return ``v`` scaled to unit L2 norm; pass through near-zero vectors."""
    norm = math.sqrt(sum(x * x for x in v))
    if norm < 1e-9:
        return v
    return [x / norm for x in v]
