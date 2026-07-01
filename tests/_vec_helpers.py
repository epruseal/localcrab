"""Shared test helpers for vector-store parity/concurrency tests.

A deterministic embedding function (text -> fixed unit vector) lets Chroma and
sqlite-vec be compared exactly: both backends embed the same text to the same
vector, so any divergence is a backend bug, not embedding noise. No network /
LM Studio / GGUF dependency.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any


class MockEF:
    """Deterministic embedding function with the same interface as the real
    EFs (``__call__(list[str]) -> list[list[float]]``, ``embed_query``,
    ``name``). Vectors are pseudo-random unit vectors seeded by sha256(text)."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return [self._vec(t) for t in input]

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return self.__call__(input)

    def name(self) -> str:
        return "mock_ef"

    def _vec(self, text: str) -> list[float]:
        seed = hashlib.sha256(text.encode()).digest()
        vals: list[float] = []
        i = 0
        while len(vals) < self._dim:
            hh = hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
            for b in range(0, len(hh), 4):
                vals.append(int.from_bytes(hh[b : b + 4], "big") / 2**32 - 0.5)
                if len(vals) >= self._dim:
                    break
            i += 1
        norm = math.sqrt(sum(x * x for x in vals)) or 1.0
        return [x / norm for x in vals]


def build_vector_store(backend: str, tmp_path: Any, dim: int = 32) -> Any:
    """Construct a vector store for the given backend with the shared MockEF."""
    ef = MockEF(dim)
    if backend == "chroma":
        from opencrab.stores.chroma_store import ChromaStore

        return ChromaStore(
            host="localhost",
            port=0,
            collection_name="vtest",
            local_mode=True,
            local_path=str(tmp_path / "chroma"),
            embedding_function=ef,
        )
    if backend == "sqlite-vec":
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        return SqliteVecStore(
            db_path=str(tmp_path / "vectors.db"),
            embedding_function=ef,
            dim=dim,
            collection_name="vtest",
        )
    raise ValueError(f"unknown backend {backend!r}")
