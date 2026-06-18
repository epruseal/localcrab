"""Timestamp formatting helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string with offset (``...+00:00``).

    Consolidates the identical ``_now_iso`` definitions previously duplicated in
    execution/, ontology/, and billing/ modules.
    """
    return datetime.now(timezone.utc).isoformat()
