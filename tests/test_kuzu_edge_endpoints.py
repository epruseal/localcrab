"""Kùzu edge endpoint API remains disabled until writer qualification."""

from __future__ import annotations

from opencrab.common.graph_identity import (
    GraphReadCapabilityUnavailable,
    GraphWriteCapabilityUnavailable,
)
from opencrab.stores.kuzu_graph_store import KuzuUnavailableGraphStore


def test_kuzu_edge_endpoint_read_is_capability_negative() -> None:
    try:
        KuzuUnavailableGraphStore().get_edge("Person", "a", "knows", "Person", "b")
    except GraphReadCapabilityUnavailable:
        return
    raise AssertionError("Kùzu edge read was enabled without qualification")


def test_kuzu_edge_endpoint_write_is_capability_negative() -> None:
    try:
        KuzuUnavailableGraphStore().upsert_edge("Person", "a", "knows", "Person", "b")
    except GraphWriteCapabilityUnavailable:
        return
    raise AssertionError("Kùzu edge write was enabled without qualification")
