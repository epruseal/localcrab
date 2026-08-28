"""Capability-negative Kùzu graph-filter contract tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from opencrab.common.graph_identity import (
    GraphQueryWriteRejected,
    GraphReadCapabilityUnavailable,
    GraphWriteCapabilityUnavailable,
)
from opencrab.stores.kuzu_graph_store import KuzuUnavailableGraphStore


def _store() -> KuzuUnavailableGraphStore:
    return KuzuUnavailableGraphStore(str(Path(tempfile.gettempdir()) / "issue80-kuzu-filter-never-created"))


def test_kuzu_filter_backend_is_capability_negative() -> None:
    store = _store()
    assert store.available is False
    try:
        store.find_neighbors("n")
    except GraphReadCapabilityUnavailable:
        return
    raise AssertionError("Kùzu read path was enabled without qualification")


def test_kuzu_filter_query_write_is_rejected() -> None:
    try:
        _store().run_cypher("MATCH (n) SET n.x=1 RETURN n")
    except GraphQueryWriteRejected:
        return
    raise AssertionError("Kùzu query mutation was not rejected")


def test_kuzu_filter_mutation_is_rejected() -> None:
    try:
        _store().upsert_node("Claim", "a", {})
    except GraphWriteCapabilityUnavailable:
        return
    raise AssertionError("Kùzu mutation was not capability-negative")
