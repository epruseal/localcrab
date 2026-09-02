"""
MetaOntology grammar validator.

All node and edge validation goes through this module. It is the single
source of truth for what constitutes a valid ontology operation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from opencrab.grammar.manifest import META_EDGES, SPACES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal lookup tables (built once at import time)
# ---------------------------------------------------------------------------

_SPACE_NODE_TYPES: dict[str, set[str]] = {
    space_id: set(spec["node_types"]) for space_id, spec in SPACES.items()
}

# Property "type" names -> Python type(s), sourced from the actual values used
# across opencrab/schemas/types/*.yaml ("string", "int", "float" — no others
# exist today). "float" also accepts plain ints (5 is a valid float). bool is
# a subclass of int in Python, so it is excluded explicitly below rather than
# relying on isinstance() alone.
_PROPERTY_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "int": int,
    "float": (int, float),
}


def _value_matches_type(value: Any, type_name: str) -> bool:
    """True if *value* satisfies declared *type_name*, else False.

    A *type_name* absent from ``_PROPERTY_TYPE_MAP`` (i.e. unknown to this
    validator) always matches — failing closed on an unrecognised type would
    break ingestion for any schema using it, so unknown types are left
    unenforced rather than rejected.
    """
    py_type = _PROPERTY_TYPE_MAP.get(type_name)
    if py_type is None:
        logger.warning(
            "Property type '%s' is not validated -- add it to "
            "_PROPERTY_TYPE_MAP or remove the declaration.",
            type_name,
        )
        return True
    if isinstance(value, bool) and py_type is not bool:
        return False
    return isinstance(value, py_type)

def _is_nullable(spec: dict[str, Any]) -> bool:
    """True if a property spec allows an explicit ``None`` value.

    ``required`` decides whether the KEY must be present. ``nullable`` decides
    whether the VALUE may be ``None``. When a spec does not declare
    ``nullable`` it is derived as ``not required``: every hand-written schema
    in opencrab/schemas/types/ declares it explicitly and follows exactly that
    pairing, and the schemas that pack_registry generates omit the key with
    that same meaning documented (see ``_build_type_schema``). So the
    derivation changes no schema's declared meaning (#49, #106).
    """
    return bool(spec.get("nullable", not spec.get("required", False)))


# Map (from_space, to_space) -> set[relation]
_EDGE_RELATION_MAP: dict[tuple[str, str], set[str]] = {}
for _edge in META_EDGES:
    _key = (_edge["from_space"], _edge["to_space"])
    _EDGE_RELATION_MAP[_key] = set(_edge["relations"])


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Outcome of a grammar validation check."""

    valid: bool
    error: str | None = None

    def raise_if_invalid(self) -> None:
        if not self.valid:
            raise ValueError(self.error or "Validation failed")

    def __bool__(self) -> bool:
        return self.valid


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_node(space_id: str, node_type: str) -> ValidationResult:
    """
    Check that *node_type* is a valid type within *space_id*.

    Parameters
    ----------
    space_id:
        One of the canonical space identifiers (e.g. "subject", "resource").
    node_type:
        The type label for the node (e.g. "User", "Document").

    Returns
    -------
    ValidationResult
        ``.valid`` is True when the combination is allowed.
    """
    if space_id not in _SPACE_NODE_TYPES:
        known = ", ".join(sorted(_SPACE_NODE_TYPES))
        return ValidationResult(
            valid=False,
            error=f"Unknown space '{space_id}'. Known spaces: {known}.",
        )

    if node_type not in _SPACE_NODE_TYPES[space_id]:
        allowed = ", ".join(sorted(_SPACE_NODE_TYPES[space_id]))
        return ValidationResult(
            valid=False,
            error=(
                f"Node type '{node_type}' is not valid in space '{space_id}'. "
                f"Allowed types: {allowed}."
            ),
        )

    return ValidationResult(valid=True)


def validate_edge(from_space: str, to_space: str, relation: str) -> ValidationResult:
    """
    Check that *relation* is a valid meta-edge from *from_space* to *to_space*.

    Parameters
    ----------
    from_space:
        Source space identifier.
    to_space:
        Target space identifier.
    relation:
        Relation label (e.g. "owns", "supports").

    Returns
    -------
    ValidationResult
    """
    if from_space not in _SPACE_NODE_TYPES:
        return ValidationResult(
            valid=False,
            error=f"Unknown source space '{from_space}'.",
        )

    if to_space not in _SPACE_NODE_TYPES:
        return ValidationResult(
            valid=False,
            error=f"Unknown target space '{to_space}'.",
        )

    key = (from_space, to_space)
    if key not in _EDGE_RELATION_MAP:
        return ValidationResult(
            valid=False,
            error=(
                f"No meta-edge defined from space '{from_space}' to space '{to_space}'. "
                "Check grammar/manifest.py for valid space pairs."
            ),
        )

    allowed = _EDGE_RELATION_MAP[key]
    if relation not in allowed:
        return ValidationResult(
            valid=False,
            error=(
                f"Relation '{relation}' is not valid from '{from_space}' to '{to_space}'. "
                f"Allowed relations: {', '.join(sorted(allowed))}."
            ),
        )

    return ValidationResult(valid=True)


def get_allowed_relations(from_space: str, to_space: str) -> list[str]:
    """
    Return the list of valid relation labels between two spaces.

    Returns an empty list if no meta-edge exists between those spaces.
    """
    key = (from_space, to_space)
    return sorted(_EDGE_RELATION_MAP.get(key, set()))


def validate_metadata_layer(layer: str, attribute: str) -> ValidationResult:
    """
    Validate that *attribute* belongs to *layer* in ACTIVE_METADATA_LAYERS.
    """
    from opencrab.grammar.manifest import ACTIVE_METADATA_LAYERS

    if layer not in ACTIVE_METADATA_LAYERS:
        known = ", ".join(sorted(ACTIVE_METADATA_LAYERS))
        return ValidationResult(
            valid=False,
            error=f"Unknown metadata layer '{layer}'. Known layers: {known}.",
        )

    allowed = ACTIVE_METADATA_LAYERS[layer]
    if attribute not in allowed:
        return ValidationResult(
            valid=False,
            error=(
                f"Attribute '{attribute}' not in layer '{layer}'. "
                f"Allowed: {', '.join(allowed)}."
            ),
        )

    return ValidationResult(valid=True)


def validate_rebac_permission(permission: str) -> ValidationResult:
    """Check that *permission* is a known ReBAC permission."""
    from opencrab.grammar.manifest import REBAC_PERMISSIONS

    if permission not in REBAC_PERMISSIONS:
        return ValidationResult(
            valid=False,
            error=(
                f"Unknown permission '{permission}'. "
                f"Allowed: {', '.join(REBAC_PERMISSIONS)}."
            ),
        )
    return ValidationResult(valid=True)


def validate_node_properties(node_type: str, properties: dict[str, Any]) -> ValidationResult:
    """
    Validate node properties against the Type Schema Registry.

    If no schema exists for the node_type, the check always passes
    (schema is optional — not all types need a registered schema).

    Per property the checks are: ``required`` (the key must be present unless
    the spec has a ``default``), ``nullable`` (an explicit ``None`` is
    rejected unless the spec allows it, see ``_is_nullable``), ``enum`` and
    ``type`` (both skipped for a permitted ``None``). Errors keep their
    historical order: missing keys in schema order, then null and enum errors
    in input order, then type errors in input order.

    Parameters
    ----------
    node_type:
        The node type label (e.g. "User", "Document").
    properties:
        The property dict to validate.

    Returns
    -------
    ValidationResult
    """
    try:
        from opencrab.schemas.loader import load_type_schema
    except ImportError:
        return ValidationResult(valid=True)

    schema = load_type_schema(node_type)
    if schema is None:
        return ValidationResult(valid=True)

    schema_props: dict[str, Any] = schema.get("properties", {})
    errors: list[str] = []

    # Required field check: key presence only. An explicit None is judged by
    # the nullable check below, not here.
    for field, spec in schema_props.items():
        if spec.get("required", False) and "default" not in spec:
            if field not in properties:
                errors.append(f"Required field '{field}' is missing.")

    # Null and enum value check. An explicit None on a non-nullable field is
    # exactly one error for that field; the enum and type checks below do not
    # add to it. A None on a nullable field skips both checks (#49, #106).
    for field, value in properties.items():
        if field in schema_props:
            spec = schema_props[field]
            if value is None:
                if not _is_nullable(spec):
                    errors.append(
                        f"Field '{field}' must not be null "
                        "(schema declares nullable: false)."
                    )
                continue
            allowed = spec.get("enum")
            if allowed is not None and value not in allowed:
                errors.append(
                    f"Field '{field}' must be one of {allowed}, got '{value}'."
                )

    # Type check
    for field, value in properties.items():
        if field in schema_props:
            spec = schema_props[field]
            declared_type = spec.get("type")
            if (
                declared_type is not None
                and value is not None
                and not _value_matches_type(value, declared_type)
            ):
                errors.append(
                    f"Field '{field}' must be of type '{declared_type}', "
                    f"got {type(value).__name__} ({value!r})."
                )

    if errors:
        return ValidationResult(valid=False, error="; ".join(errors))
    return ValidationResult(valid=True)


def describe_grammar() -> dict[str, Any]:
    """Return a JSON-serialisable summary of the full MetaOntology grammar."""
    from opencrab.grammar.manifest import (
        ACTIVE_METADATA_LAYERS,
        GRAMMAR_VERSION,
        IMPACT_CATEGORIES,
        REBAC_OBJECT_TYPES,
        REBAC_PERMISSIONS,
        SPACES,
    )

    return {
        "version": GRAMMAR_VERSION,
        "spaces": SPACES,
        "meta_edges": META_EDGES,
        "impact_categories": IMPACT_CATEGORIES,
        "active_metadata_layers": ACTIVE_METADATA_LAYERS,
        "rebac": {
            "object_types": REBAC_OBJECT_TYPES,
            "permissions": REBAC_PERMISSIONS,
        },
    }
