"""Shared test helpers for vector-store parity/concurrency tests.

A deterministic embedding function (text -> fixed unit vector) lets Chroma and
sqlite-vec be compared exactly: both backends embed the same text to the same
vector, so any divergence is a backend bug, not embedding noise. No network /
LM Studio / GGUF dependency.
"""

from __future__ import annotations

import hashlib
import math
import os
import uuid
from typing import Any

import pytest


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


def build_vector_store(
    backend: str, tmp_path: Any, dim: int = 32, **kwargs: Any
) -> Any:
    """Construct a vector store for the given backend with the shared MockEF.

    Extra kwargs are forwarded to the store constructor (e.g. ``ann="binary"``,
    ``ann_coarse_k=...`` for SqliteVecStore's §3.7 2-stage path)."""
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
            **kwargs,
        )
    if backend == "sqlite-vec":
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        return SqliteVecStore(
            db_path=str(tmp_path / "vectors.db"),
            embedding_function=ef,
            dim=dim,
            collection_name="vtest",
            **kwargs,
        )
    if backend == "pg":
        # 실 PG 없이는 테스트 불가능한 백엔드 — env 미설정/접속 불가 시 깔끔히
        # skip(나머지 스위트는 무영향). 테스트별 격리를 위해 collection_name에
        # uuid 접미사(공유 테스트 DB에서 병렬 실행/재실행 시 테이블 충돌 방지).
        dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
        if not dsn:
            pytest.skip("OPENCRAB_PG_TEST_URL not set - pg backend tests skipped")
        from opencrab.stores.pg_vector_store import PgVectorStore

        collection = kwargs.pop("collection_name", f"vtest_{uuid.uuid4().hex[:12]}")
        store = PgVectorStore(
            dsn_or_engine=dsn,
            embedding_function=ef,
            dim=dim,
            collection_name=collection,
            **kwargs,
        )
        if not store.available:
            pytest.skip(f"Cannot connect to PG test DB at {dsn!r}")
        return store
    raise ValueError(f"unknown backend {backend!r}")
