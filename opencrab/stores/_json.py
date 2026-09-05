"""Shared JSON helpers for graph store backends."""

from __future__ import annotations

import json
from typing import Any


def dump_props(obj: Any, *, ensure_ascii: bool = True) -> str:
    """Serialize a properties/metadata/details dict for a SQL JSON column.

    ``allow_nan=False`` rejects NaN/Infinity with a ``ValueError`` at write
    time instead of letting the non-standard token reach the column: SQLite's
    ``json_extract()`` treats such a row as malformed JSON, and PostgreSQL's
    ``::jsonb`` cast raises a raw driver error on INSERT (localcrab#82).

    Single choke point for every store's properties/metadata/details
    serialization -- callers that already validate finiteness upstream (the
    graph store's ``graph_identity._validate_json``) route through this too,
    so there is one place, not several, doing the ``json.dumps`` call.
    """
    return json.dumps(obj, ensure_ascii=ensure_ascii, allow_nan=False)


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
