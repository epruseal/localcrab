"""Canonical graph identity and write-intent primitives.

This module deliberately contains no database code.  All graph backends use
the same byte representation before comparing rows or producing a receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias


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


class GraphMigrationConflict(RuntimeError):  # noqa: N818 - public domain exception name
    """A migration source, plan, target, or ledger identity no longer matches."""


class FrozenDict(Mapping[str, Any]):
    """Recursively immutable JSON object used by public graph receipts."""

    __slots__ = ("_items", "_data", "_hash")

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        source = {} if values is None else dict(values)
        if any(not isinstance(key, str) for key in source):
            raise ValueError("graph properties object keys must be strings")
        self._data = {key: freeze_json(value) for key, value in source.items()}
        self._items = tuple(sorted(self._data.items(), key=lambda item: item[0]))
        self._hash = hash(self._items)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._items)!r})"

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)


FrozenValue: TypeAlias = None | bool | int | float | str | FrozenDict | tuple[Any, ...]


def freeze_json(value: Any) -> FrozenValue:
    """Copy and recursively freeze a JSON-compatible value."""
    _validate_json(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise ValueError("graph properties must contain JSON values")


def thaw_json(value: Any) -> Any:
    """Return a recursive defensive copy suitable for a caller or JSON encoder."""
    if isinstance(value, FrozenDict) or isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    if isinstance(value, list):
        return [thaw_json(item) for item in value]
    return value


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
    if isinstance(value, FrozenDict) or isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        seen = _seen if _seen is not None else set()
        marker = id(value)
        if marker in seen:
            raise ValueError("graph properties must not contain cycles")
        seen.add(marker)
        try:
            if isinstance(value, (list, tuple)):
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
        thaw_json(value),
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
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("malformed graph properties") from exc
    if isinstance(value, Mapping):
        obj = thaw_json(value)
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

    def __post_init__(self) -> None:
        frozen = freeze_json(self.properties)
        if not isinstance(frozen, FrozenDict):
            raise ValueError("graph receipt properties must be an object")
        object.__setattr__(self, "properties", frozen)


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

    def __post_init__(self) -> None:
        frozen = freeze_json(self.properties)
        if not isinstance(frozen, FrozenDict):
            raise ValueError("graph receipt properties must be an object")
        object.__setattr__(self, "properties", frozen)


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


@dataclass(frozen=True, order=True)
class LegacyNodeKey:
    node_type: str
    node_id: str


@dataclass(frozen=True)
class PropertyNormalizationIssue:
    record_kind: Literal["node", "edge"]
    source_key: str
    field: str
    raw_values: FrozenDict
    reason: str


@dataclass(frozen=True)
class LegacyNodeRow:
    key: LegacyNodeKey
    space_id: str | None
    pack_id: str | None
    raw_properties: bytes | FrozenValue
    normalized_properties: FrozenDict | None
    property_error: str | None
    normalization_issues: tuple[PropertyNormalizationIssue, ...]
    digest: str

    def __post_init__(self) -> None:
        raw = self.raw_properties
        object.__setattr__(self, "raw_properties", bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else freeze_json(raw))
        if self.normalized_properties is not None:
            object.__setattr__(self, "normalized_properties", FrozenDict(self.normalized_properties))
        object.__setattr__(self, "normalization_issues", tuple(self.normalization_issues))


@dataclass(frozen=True)
class LegacyEdgeRow:
    from_key: LegacyNodeKey
    relation: str
    to_key: LegacyNodeKey
    raw_properties: bytes | FrozenValue
    normalized_properties: FrozenDict | None
    property_error: str | None
    normalization_issues: tuple[PropertyNormalizationIssue, ...]
    digest: str

    def __post_init__(self) -> None:
        raw = self.raw_properties
        object.__setattr__(self, "raw_properties", bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else freeze_json(raw))
        if self.normalized_properties is not None:
            object.__setattr__(self, "normalized_properties", FrozenDict(self.normalized_properties))
        object.__setattr__(self, "normalization_issues", tuple(self.normalization_issues))


@dataclass(frozen=True)
class GraphInventory:
    schema_state: Literal["fresh", "legacy", "target", "partial"]
    nodes: tuple[LegacyNodeRow, ...]
    edges: tuple[LegacyEdgeRow, ...]
    normalization_issues: tuple[PropertyNormalizationIssue, ...]
    source_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "normalization_issues", tuple(self.normalization_issues))


@dataclass(frozen=True)
class ExplicitRename:
    source: LegacyNodeKey
    source_digest: str
    target_node_id: str
    target_node_type: str
    target_space_id: str | None
    target_pack_id: str | None


@dataclass(frozen=True)
class ExplicitMerge:
    sources: tuple[tuple[LegacyNodeKey, str], ...]
    target_node_id: str
    target_node_type: str
    target_space_id: str | None
    target_pack_id: str | None

    def __post_init__(self) -> None:
        if isinstance(self.sources, (list, tuple)):
            object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class PropertyResolution:
    source: LegacyNodeKey
    source_property: str
    source_value: FrozenValue
    target_property: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_value", freeze_json(self.source_value))


@dataclass(frozen=True)
class MigrationPlanPayload:
    source_fingerprint: str
    mapping_fingerprint: str
    canonical_mappings: tuple[FrozenDict, ...]
    planned_target_node_fingerprint: str
    planned_target_edge_fingerprint: str
    collision_results: tuple[FrozenDict, ...]
    dedup_results: tuple[FrozenDict, ...]
    edge_loss: int
    property_loss: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_mappings", tuple(FrozenDict(item) for item in self.canonical_mappings))
        object.__setattr__(self, "collision_results", tuple(FrozenDict(item) for item in self.collision_results))
        object.__setattr__(self, "dedup_results", tuple(FrozenDict(item) for item in self.dedup_results))


@dataclass(frozen=True)
class DryRunMigrationRequest:
    expected_source_fingerprint: str
    mappings: tuple[ExplicitRename | ExplicitMerge, ...]
    property_resolutions: tuple[PropertyResolution, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.mappings, (list, tuple)):
            object.__setattr__(self, "mappings", tuple(self.mappings))
        if isinstance(self.property_resolutions, (list, tuple)):
            object.__setattr__(self, "property_resolutions", tuple(self.property_resolutions))


@dataclass(frozen=True)
class ApplyMigrationRequest:
    request_id: str
    expected_source_fingerprint: str
    plan_bytes: bytes
    plan_sha256: str
    backup_path: Path
    backup_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.plan_bytes, (bytes, bytearray, memoryview)):
            object.__setattr__(self, "plan_bytes", bytes(self.plan_bytes))


MigrationRequest = DryRunMigrationRequest | ApplyMigrationRequest


@dataclass(frozen=True)
class MigrationReceiptPayload:
    request_id: str | None
    phase: Literal["dry_run", "apply"]
    request_digest: str
    source_fingerprint: str
    mapping_fingerprint: str
    plan_sha256: str
    target_fingerprint_before: str
    target_fingerprint_after: str
    mapping_result: tuple[FrozenDict, ...]
    edge_loss: int
    property_loss: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "mapping_result", tuple(FrozenDict(item) for item in self.mapping_result))


@dataclass(frozen=True)
class MigrationReceipt:
    request_id: str | None
    phase: Literal["dry_run", "apply"]
    request_digest: str
    source_fingerprint: str
    mapping_fingerprint: str
    plan_sha256: str
    plan_bytes: bytes
    target_fingerprint_before: str
    target_fingerprint_after: str
    mapping_result: tuple[FrozenDict, ...]
    edge_loss: int
    property_loss: int
    receipt_sha256: str
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_bytes", bytes(self.plan_bytes))
        object.__setattr__(self, "canonical_bytes", bytes(self.canonical_bytes))
        object.__setattr__(self, "mapping_result", tuple(freeze_json(item) for item in self.mapping_result))

    def to_canonical_payload(self) -> MigrationReceiptPayload:
        return MigrationReceiptPayload(
            request_id=self.request_id,
            phase=self.phase,
            request_digest=self.request_digest,
            source_fingerprint=self.source_fingerprint,
            mapping_fingerprint=self.mapping_fingerprint,
            plan_sha256=self.plan_sha256,
            target_fingerprint_before=self.target_fingerprint_before,
            target_fingerprint_after=self.target_fingerprint_after,
            mapping_result=self.mapping_result,
            edge_loss=self.edge_loss,
            property_loss=self.property_loss,
        )


def decode_raw_properties(value: Any, *, jsonb: bool = False) -> tuple[bytes | FrozenValue, FrozenDict | None, str | None]:
    """Decode one stored graph property value while retaining parse failures."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw: bytes | FrozenValue = bytes(value)
        try:
            parsed = parse_properties_object(raw)
        except ValueError:
            return raw, None, "malformed_json"
        return raw, FrozenDict(parsed), None
    if isinstance(value, str) and jsonb:
        # psycopg drivers normally decode JSONB before returning it: an object
        # is a mapping, an array is a list, and a JSON string scalar is a
        # plain Python str.  Do not mistake that scalar for malformed JSON.
        raw = value.encode("utf-8", errors="surrogatepass")

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
            # A driver-level raw object/array that is not valid JSON is
            # malformed data.  A normal unquoted string is the JSONB scalar
            # representation returned by the driver and remains observable as
            # such in the inventory.
            if value.lstrip().startswith(("{", "[")):
                reason = "duplicate_object_key" if "duplicate" in str(exc) else "malformed_json"
            else:
                return value, None, "jsonb_non_object_scalar"
            return raw, None, reason
        if isinstance(parsed, dict):
            return raw, FrozenDict(parsed), None
        if isinstance(parsed, list):
            return freeze_json(parsed), None, "jsonb_non_object_array"
        return freeze_json(parsed), None, "jsonb_non_object_scalar"
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="surrogatepass")
        try:
            parsed = parse_properties_object(value)
        except ValueError as exc:
            message = str(exc)
            if "duplicate" in message:
                return raw, None, "duplicate_object_key"
            return raw, None, "malformed_json"
        return raw, FrozenDict(parsed), None
    if isinstance(value, Mapping):
        try:
            frozen = freeze_json(value)
            return frozen, FrozenDict(value), None
        except ValueError:
            try:
                raw = freeze_json(dict(value))
            except ValueError:
                raw = repr(dict(value))
            return raw, None, "malformed_object"
    if isinstance(value, (list, tuple)):
        return freeze_json(value), None, "jsonb_non_object_array" if jsonb else "json_array"
    try:
        raw = freeze_json(value)
    except ValueError:
        raw = repr(value)
    return raw, None, "jsonb_non_object_scalar" if jsonb else "json_scalar"


def canonical_plan_bytes(plan: MigrationPlanPayload) -> bytes:
    """Encode a request-independent migration plan payload."""
    value = {
        "source_fingerprint": plan.source_fingerprint,
        "mapping_fingerprint": plan.mapping_fingerprint,
        "canonical_mappings": thaw_json(plan.canonical_mappings),
        "planned_target_node_fingerprint": plan.planned_target_node_fingerprint,
        "planned_target_edge_fingerprint": plan.planned_target_edge_fingerprint,
        "collision_results": thaw_json(plan.collision_results),
        "dedup_results": thaw_json(plan.dedup_results),
        "edge_loss": plan.edge_loss,
        "property_loss": plan.property_loss,
    }
    return canonical_json_bytes(value)


def plan_sha256(plan_bytes: bytes) -> str:
    return hashlib.sha256(b"opencrab.issue80.migration-plan.v1\0" + bytes(plan_bytes)).hexdigest()


def canonical_receipt_bytes(payload: MigrationReceiptPayload) -> bytes:
    value = {
        "request_id": payload.request_id,
        "phase": payload.phase,
        "request_digest": payload.request_digest,
        "source_fingerprint": payload.source_fingerprint,
        "mapping_fingerprint": payload.mapping_fingerprint,
        "plan_sha256": payload.plan_sha256,
        "target_fingerprint_before": payload.target_fingerprint_before,
        "target_fingerprint_after": payload.target_fingerprint_after,
        "mapping_result": thaw_json(payload.mapping_result),
        "edge_loss": payload.edge_loss,
        "property_loss": payload.property_loss,
    }
    return canonical_json_bytes(value)


def receipt_sha256(receipt_bytes: bytes) -> str:
    return hashlib.sha256(b"opencrab.issue80.migration-receipt.v1\0" + bytes(receipt_bytes)).hexdigest()
