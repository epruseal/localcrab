"""Unit tests for usage counter helpers in apps/api/main.py.

Covers:
  - unavailable store -> CountResult(status="unavailable")
  - Mongo timeout (ExecutionTimeout-shaped exc) -> status="timeout"
  - Generic exception -> status="error" with detail
  - top-level owner_id only -> matched
  - nested properties.owner_id only -> matched (backward compatible)
  - documents iter (non-Mongo) fallback path
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.api.main import (  # noqa: E402
    CountResult,
    _count_user_nodes,
    _count_user_queries,
    _count_user_sources,
    _safe_count,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeExecutionTimeout(Exception):
    """Imitates pymongo.errors.ExecutionTimeout by class name."""

    pass


_FakeExecutionTimeout.__name__ = "ExecutionTimeout"


class _FakeCollection:
    def __init__(self, *, match_count: int | None, raises: BaseException | None = None) -> None:
        self._match_count = match_count
        self._raises = raises

    def count_documents(self, _query: dict[str, Any]) -> int:
        if self._raises is not None:
            raise self._raises
        assert self._match_count is not None
        return self._match_count


class _FakeMongoDocs:
    """Mongo-backed doc store double: exposes ._db dict of collections."""

    available = True

    def __init__(self, collections: dict[str, _FakeCollection]) -> None:
        self._db = collections


class _FakeListDocs:
    """Non-Mongo doc store double: exposes list_nodes / list_sources / get_audit_log."""

    available = True

    def __init__(
        self,
        *,
        nodes: list[dict[str, Any]] | None = None,
        sources: list[dict[str, Any]] | None = None,
        audit: list[dict[str, Any]] | None = None,
    ) -> None:
        self._nodes = nodes or []
        self._sources = sources or []
        self._audit = audit or []

    def list_nodes(self) -> list[dict[str, Any]]:
        return self._nodes

    def list_sources(self) -> list[dict[str, Any]]:
        return self._sources

    def get_audit_log(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._audit[:limit]


class _UnavailableDocs:
    available = False


# ---------------------------------------------------------------------------
# _count_user_nodes
# ---------------------------------------------------------------------------


def test_count_user_nodes_unavailable():
    result = _count_user_nodes(_UnavailableDocs(), "u1")
    assert result == CountResult(value=0, status="unavailable")


def test_count_user_nodes_timeout():
    docs = _FakeMongoDocs(
        {"nodes": _FakeCollection(match_count=None, raises=_FakeExecutionTimeout("too slow"))}
    )
    result = _count_user_nodes(docs, "u1")
    assert result.status == "timeout"
    assert result.value == 0
    assert result.detail == "too slow"


def test_count_user_nodes_generic_error():
    docs = _FakeMongoDocs(
        {"nodes": _FakeCollection(match_count=None, raises=RuntimeError("boom"))}
    )
    result = _count_user_nodes(docs, "u1")
    assert result.status == "error"
    assert result.detail == "boom"


def test_count_user_nodes_ok_via_mongo():
    docs = _FakeMongoDocs({"nodes": _FakeCollection(match_count=42)})
    result = _count_user_nodes(docs, "u1")
    assert result == CountResult(value=42, status="ok")


def test_count_user_nodes_top_level_only_iter():
    docs = _FakeListDocs(
        nodes=[
            {"owner_id": "u1", "properties": {}},
            {"owner_id": "u2", "properties": {}},
            {"properties": {}},
        ]
    )
    result = _count_user_nodes(docs, "u1")
    assert result == CountResult(value=1, status="ok")


def test_count_user_nodes_nested_only_iter():
    docs = _FakeListDocs(
        nodes=[
            {"properties": {"owner_id": "u1"}},
            {"properties": {"owner_id": "u2"}},
            {"properties": {"owner_id": "u1"}},
        ]
    )
    result = _count_user_nodes(docs, "u1")
    assert result == CountResult(value=2, status="ok")


# ---------------------------------------------------------------------------
# _count_user_sources
# ---------------------------------------------------------------------------


def test_count_user_sources_top_level_or_nested_iter():
    docs = _FakeListDocs(
        sources=[
            {"user_id": "u1"},                       # top-level only
            {"metadata": {"user_id": "u1"}},          # nested only
            {"user_id": "u2"},
            {"metadata": {"user_id": "u3"}},
            {"user_id": "u1", "metadata": {"user_id": "u1"}},  # both
        ]
    )
    result = _count_user_sources(docs, "u1")
    assert result == CountResult(value=3, status="ok")


def test_count_user_sources_timeout():
    docs = _FakeMongoDocs(
        {"sources": _FakeCollection(match_count=None, raises=_FakeExecutionTimeout("slow"))}
    )
    result = _count_user_sources(docs, "u1")
    assert result.status == "timeout"


# ---------------------------------------------------------------------------
# _count_user_queries
# ---------------------------------------------------------------------------


def test_count_user_queries_iter():
    docs = _FakeListDocs(
        audit=[
            {"actor": "u1", "event_type": "query"},
            {"actor": "u1", "event_type": "ingest"},
            {"actor": "u2", "event_type": "query"},
            {"actor": "u1", "event_type": "query"},
        ]
    )
    result = _count_user_queries(docs, "u1")
    assert result == CountResult(value=2, status="ok")


# ---------------------------------------------------------------------------
# _safe_count
# ---------------------------------------------------------------------------


def test_safe_count_ok():
    assert _safe_count(lambda: 7) == CountResult(value=7, status="ok")


def test_safe_count_exception_classified_as_error():
    def boom() -> int:
        raise RuntimeError("nope")

    result = _safe_count(boom)
    assert result.status == "error"
    assert result.detail == "nope"


def test_safe_count_timeout_classified():
    def slow() -> int:
        raise _FakeExecutionTimeout("deadline")

    result = _safe_count(slow)
    assert result.status == "timeout"


# ---------------------------------------------------------------------------
# CountResult.to_dict
# ---------------------------------------------------------------------------


def test_count_result_to_dict_omits_detail_when_none():
    assert CountResult(value=3, status="ok").to_dict() == {"value": 3, "status": "ok"}


def test_count_result_to_dict_includes_detail():
    out = CountResult(value=0, status="error", detail="bad").to_dict()
    assert out == {"value": 0, "status": "error", "detail": "bad"}
