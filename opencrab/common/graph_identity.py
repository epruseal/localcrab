"""Canonical graph identity and write-intent primitives.

This module deliberately contains no database code.  All graph backends use
the same byte representation before comparing rows or producing a receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any


class NodeIdentityConflict(RuntimeError):  # noqa: N818 - public domain exception name
    """A global node id is already used for another logical node."""


class EdgeIdentityConflict(RuntimeError):  # noqa: N818 - public domain exception name
    """A global edge key is already used for another logical edge."""


class GraphSchemaMigrationRequired(RuntimeError):  # noqa: N818 - public domain exception name
    """The graph database is legacy or cannot be classified safely."""


class GraphWriteUnavailable(RuntimeError):  # noqa: N818 - public domain exception name
    """The graph backend is not available for writes."""


class GraphWriteCapabilityUnavailable(GraphWriteUnavailable):
    """The backend has no qualified atomic write capability."""


class GraphReadCapabilityUnavailable(RuntimeError):  # noqa: N818 - public domain exception name
    """The backend has no qualified read capability."""


class GraphQueryWriteRejected(GraphWriteUnavailable):
    """An arbitrary query was not proven read-only."""


class GraphMigrationFixtureOnlyError(RuntimeError):
    """Graph migration apply is restricted to disposable fixtures."""


def _check_string(value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("graph identity fields must be non-empty strings")
    return value


def _validate_json(value: Any, *, _seen: set[int] | None = None) -> None:
    """Validate the intentionally narrow JSON value grammar.

    ``dict`` and ``list`` values can be cyclic in Python even though JSON
    cannot represent cycles.  Reject those graphs explicitly so an invalid
    caller value produces the same controlled validation error as every other
    unsupported value instead of recursing until the interpreter raises.
    """
    if value is None or isinstance(value, (bool, str, int)):
        if isinstance(value, str):
            value.encode("utf-8")  # rejects lone surrogates
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("graph properties must contain finite JSON values")
        return
    if isinstance(value, (list, dict)):
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            raise ValueError("graph properties must not contain cycles")
        seen.add(marker)
        try:
            if isinstance(value, list):
                for item in value:
                    _validate_json(item, _seen=seen)
            else:
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("graph properties object keys must be strings")
                    key.encode("utf-8")
                    _validate_json(item, _seen=seen)
        finally:
            seen.remove(marker)
        return
    raise ValueError("graph properties must contain JSON values")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable UTF-8 JSON encoding used by graph digests."""
    _validate_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def parse_properties_object(value: Any) -> dict[str, Any]:
    """Decode a stored graph-properties value without accepting duplicate keys.

    SQLite returns JSON text while PostgreSQL and the native graph drivers
    may already return a mapping.  Provenance plans must observe the same
    object on every backend, so malformed JSON, non-objects, and duplicate
    object keys fail closed instead of being coerced to ``{}``.
    """
    if isinstance(value, dict):
        obj = dict(value)
        _validate_json(obj)
        return obj
    if not isinstance(value, str) or not value:
        raise ValueError("malformed graph properties")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in items:
            if key in out:
                raise ValueError("duplicate graph property")
            out[key] = item
        return out

    try:
        parsed = json.loads(value, object_pairs_hook=pairs)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("malformed graph properties") from exc
    if not isinstance(parsed, dict):
        raise ValueError("malformed graph properties")
    _validate_json(parsed)
    return parsed


def normalize_node_properties(node_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    node_id = _check_string(node_id)
    if not isinstance(properties, dict):
        raise ValueError("malformed graph node properties")
    # ``node_id`` was historically copied into the JSON properties by graph
    # callers.  It is now the dedicated global key, but accepting an equal
    # legacy copy preserves those callers and does not create an ambiguity.
    # A disagreeing copy is still rejected before any database access.
    if "node_id" in properties and properties["node_id"] != node_id:
        raise ValueError("reserved graph property")
    reserved = {"node_type", "node_digest", "space_id"}
    if reserved.intersection(properties):
        raise ValueError("reserved graph property")
    supplied = properties.get("id")
    if "id" in properties and supplied != node_id:
        raise ValueError("reserved graph property")
    out = dict(properties)
    out["id"] = node_id
    _validate_json(out)
    return out


def normalize_space(space_id: Any, properties: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    if space_id is not None and (not isinstance(space_id, str) or not space_id):
        # Existing callers use None for absent space; all other values are
        # malformed rather than silently changing the digest.
        raise ValueError("graph identity fields must be non-empty strings")
    prop_space = properties.get("space")
    if prop_space is not None and not isinstance(prop_space, str):
        raise ValueError("reserved graph property")
    effective = space_id if space_id is not None else (prop_space or None)
    out = dict(properties)
    if effective is None:
        out.pop("space", None)
    else:
        out["space"] = effective
    return effective, out


def canonical_node_digest(node_type: str, space_id: str | None, properties: dict[str, Any]) -> str:
    node_type = _check_string(node_type)
    if space_id is not None:
        _check_string(space_id)
    payload = {"node_type": node_type, "space_id": space_id, "properties": properties}
    return hashlib.sha256(b"opencrab.issue80.node.v1\0" + canonical_json_bytes(payload)).hexdigest()


def prepare_node(node_type: str, node_id: str, properties: dict[str, Any], space_id: str | None = None) -> tuple[str, dict[str, Any], str | None, str]:
    node_type = _check_string(node_type)
    node_id = _check_string(node_id)
    props = normalize_node_properties(node_id, properties)
    effective_space, props = normalize_space(space_id, props)
    digest = canonical_node_digest(node_type, effective_space, props)
    return node_type, props, effective_space, digest


def normalize_edge_properties(from_id: str, relation: str, to_id: str, properties: dict[str, Any] | None) -> dict[str, Any]:
    from_id = _check_string(from_id)
    relation = _check_string(relation)
    to_id = _check_string(to_id)
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        raise ValueError("malformed graph edge properties")
    out = dict(properties)
    for key, expected in (("from_id", from_id), ("relation", relation), ("to_id", to_id)):
        if key in out and out[key] != expected:
            raise ValueError("reserved graph property")
        out[key] = expected
    for key in ("edge_key", "from_type", "to_type", "edge_digest"):
        if key in out:
            raise ValueError("reserved graph property")
    if "pack_id" in out and out["pack_id"] is not None and (not isinstance(out["pack_id"], str) or not out["pack_id"]):
        raise ValueError("malformed graph edge owner")
    _validate_json(out)
    return out


def canonical_edge_digest(from_id: str, relation: str, to_id: str, from_type: str, to_type: str, properties: dict[str, Any]) -> str:
    payload = {
        "from_id": _check_string(from_id), "relation": _check_string(relation), "to_id": _check_string(to_id),
        "from_type": _check_string(from_type), "to_type": _check_string(to_type), "properties": properties,
    }
    return hashlib.sha256(b"opencrab.issue80.edge.v1\0" + canonical_json_bytes(payload)).hexdigest()


def validate_digest(value: str, *, edge: bool = False) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("malformed edge digest" if edge else "malformed node digest")
    return value


@dataclass(frozen=True)
class NodeWriteReceipt:
    operation: str
    node_id: str
    node_type: str
    space_id: str | None
    properties: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class EdgeWriteReceipt:
    operation: str
    from_id: str
    relation: str
    to_id: str
    from_type: str
    to_type: str
    properties: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class ProvenanceWriteReceipt:
    kind: str
    key: str | tuple[str, str, str]
    operation: str
    before_digest: str
    after_digest: str
    rowcount: int


@dataclass(frozen=True)
class ProvenanceBatchReceipt:
    target_fingerprint_before: str
    target_fingerprint_after: str
    records: tuple[ProvenanceWriteReceipt, ...]
