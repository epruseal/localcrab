"""Kùzu capability-negative tests.

No optional package is imported and no positive constructor is collected until
an independently qualified transaction owner exists.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from opencrab.common.graph_identity import (
    GraphQueryWriteRejected,
    GraphReadCapabilityUnavailable,
    GraphWriteCapabilityUnavailable,
)
from opencrab.stores.kuzu_graph_store import KuzuGraphStore, KuzuUnavailableGraphStore


def test_kuzu_constructor_fails_before_path_access() -> None:
    path = Path(tempfile.gettempdir()) / "issue80-kuzu-no-constructor" / "graph.kuzu"
    try:
        KuzuGraphStore(str(path))
    except GraphWriteCapabilityUnavailable:
        pass
    else:
        raise AssertionError("Kùzu constructor unexpectedly enabled")
    assert not path.parent.exists()


def test_kuzu_unavailable_facade_reports_disabled() -> None:
    store = KuzuUnavailableGraphStore()
    assert store.available is False
    assert store.schema_state == "disabled"
    assert store.ping() is False


def test_kuzu_public_mutations_fail_closed() -> None:
    store = KuzuUnavailableGraphStore()
    for call in (
        lambda: store.upsert_node("Person", "n", {}),
        lambda: store.update_node("n", "0" * 64, "Person", {}),
        lambda: store.upsert_edge("Person", "n", "knows", "Person", "m"),
        lambda: store.delete_node("Person", "n"),
        lambda: store.ensure_constraints(),
    ):
        try:
            call()
        except GraphWriteCapabilityUnavailable:
            continue
        raise AssertionError("Kùzu mutation did not fail closed")


def test_kuzu_query_guard_distinguishes_read_and_write() -> None:
    store = KuzuUnavailableGraphStore()
    try:
        store.run_cypher("MATCH (n) RETURN n")
    except GraphReadCapabilityUnavailable:
        pass
    else:
        raise AssertionError("Kùzu read capability unexpectedly enabled")
    try:
        store.run_cypher("CREATE (n)")
    except GraphQueryWriteRejected:
        pass
    else:
        raise AssertionError("Kùzu write query was not rejected")
