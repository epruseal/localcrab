"""Shared JSON helpers for graph store backends."""

from __future__ import annotations

import json
from typing import Any


def parse_props(raw: str | None) -> dict[str, Any]:
    """Parse a JSON property blob into a dict, returning ``{}`` on any failure.

    Consolidates ``LocalGraphStore._parse_props`` and ``kuzu_graph_store._parse``
    (identical: empty/None -> {}, non-dict JSON -> {}, malformed JSON -> {}).
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}
