"""The single write gate: reject client identity, authorize, stamp (#148).

Every client-reachable write in this codebase now passes through one of two
writers -- ``OntologyBuilder`` (graph node/edge fan-out) and
``opencrab.pack.source_writer`` (doc source + vector) -- and both call the
functions here. There is deliberately one implementation of "whose write is
this and which pack does it land in", because enumerating call sites is the
mistake this work kept repeating: every hand-written list of "places to
guard" missed at least one (headers guarded, body missed; ``cli.py serve``
guarded, ``python -m`` missed; REST guarded, MCP missed).

Two layers, and they are NOT the same rule:

* :func:`reject_boundary_identity` -- **external request boundaries only**
  (the MCP dispatcher, REST handlers). Reserved identity keys are refused
  whatever their value. A client has no business naming who it is.
* :func:`stamp` -- **every writer**, internal callers included. Server-derived
  values are assigned; a *client* payload disagreeing with them is refused.
  Internal callers (the pack loader replaying its own dump) get the server
  value written over theirs instead, because their payload is data this
  server previously wrote, not a claim about identity.

``created_by`` is deliberately NOT stamped. In this codebase it is a
provenance sentinel, not an identity: ``pack_create`` writes
``"localcrab-mcp"`` and ``opencrab/pack/load.py`` decides whether a node is a
pack anchor by testing ``created_by == 'title-backfill'``. Assigning the
principal over it would silently break that decision. The principal stamp is
``owner_id``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from opencrab.auth import Principal

# Keys the gate assigns, per sink. They differ because the sinks store
# ownership in different places: node/edge ownership is read off
# ``properties``, while a doc source's owner is ``metadata.user_id`` -- the
# only key the free-tier quota (`_count_user_sources`) and the source-owner
# check (`_source_owner`) in apps/api/main.py ever look at.
NODE_STAMPED = ("pack_id", "owner_id")
EDGE_STAMPED = ("pack_id",)
SOURCE_STAMPED = ("pack_id", "user_id")

# Identity a request may never carry, at any value. Superset of the stamped
# keys: `tenant_id`/`subject_id`/`created_by` are not stamped by the gate but
# are still refused from outside, because accepting them lets a caller author
# audit provenance.
BOUNDARY_REJECTED = (
    "tenant_id",
    "subject_id",
    "created_by",
    "owner_id",
    "user_id",
)

# Payload dicts whose contents are caller-authored and end up persisted.
_PAYLOAD_KEYS = ("properties", "metadata")


class ClientIdentityFieldError(ValueError):
    """A request carried server-owned identity, or contradicted it."""


def boundary_identity_violations(value: Any, path: str = "") -> list[str]:
    """Find reserved identity keys inside any nested properties/metadata dict.

    Walks the whole argument structure rather than checking the call sites
    that exist today. A walk cannot miss a site it has never heard of; a
    hand-maintained list of sites demonstrably can, and did, repeatedly.
    """
    found: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else str(key)
            if key in _PAYLOAD_KEYS and isinstance(sub, dict):
                found += [f"{here}.{k}" for k in BOUNDARY_REJECTED if k in sub]
            found += boundary_identity_violations(sub, here)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += boundary_identity_violations(item, f"{path}[{i}]")
    return found


def reject_boundary_identity(value: Any) -> None:
    """Raise if an external request carries reserved identity keys.

    Call at request boundaries BEFORE any server default is filled in --
    otherwise the server's own value is what gets rejected.
    """
    violations = boundary_identity_violations(value)
    if violations:
        raise ClientIdentityFieldError(
            "server-derived identity fields cannot be supplied by the caller: "
            + ", ".join(sorted(violations))
        )


def stamp(
    payload: Mapping[str, Any] | None,
    *,
    principal: Principal,
    pack_id: str,
    keys: Iterable[str],
    origin: Literal["client", "server"] = "client",
) -> dict[str, Any]:
    """Return a copy of ``payload`` carrying the server's values for ``keys``.

    ``origin="client"`` (the default) refuses a payload that already carries
    one of ``keys`` with a *different* value -- silently overwriting it would
    let a caller believe its value was accepted. An equal value passes: it is
    indistinguishable from the server's own, so there is nothing to refuse.
    That equality rule is what lets server-side callers which legitimately
    pre-fill ``pack_id`` (``opencrab/pack/normalize.py``, ``pack_create``'s
    anchor) keep working without a bypass door.

    ``origin="server"`` overwrites instead. Used only for payloads this
    server previously wrote and is now replaying -- the pack loader's dump
    round-trip, where a node's ``owner_id`` came from a past REST write and
    refusing it would fail the reload of the server's own data.

    MUST run before any pack-tag normalisation (``apply_pack_tag`` rewrites
    ``pack_id`` to the target pack in place, so a gate running after it can
    no longer see what the caller actually asked for).
    """
    server_values = {
        "pack_id": pack_id,
        "owner_id": principal.user_id,
        "user_id": principal.user_id,
    }
    out = dict(payload or {})
    for key in keys:
        expected = server_values[key]
        if origin == "client" and key in out and out[key] != expected:
            raise ClientIdentityFieldError(
                f"{key} is set by the server and cannot be supplied as "
                f"{out[key]!r}"
            )
        out[key] = expected
    return out


def authorize(sql: Any, principal: Principal, pack_id: str) -> dict[str, Any]:
    """Owner-only write authorization for ``pack_id``.

    Thin pass-through to ``assert_writable`` so writers depend on the gate
    rather than reaching into the registry themselves. Raises
    ``PackNotFoundError`` for a missing pack AND for someone else's private
    pack (#143 invariant 7 -- the two must be indistinguishable), and
    ``PackForbiddenError`` for a visible pack owned by someone else.

    Fails closed when the registry is unreachable: without it there is no
    way to decide ownership, and "cannot check" must never mean "allowed".
    """
    if not getattr(sql, "available", False):
        raise RuntimeError(
            "pack registry unavailable; refusing the write (ownership cannot "
            "be verified)"
        )
    from opencrab.pack.ownership import assert_writable

    return assert_writable(sql, principal, pack_id)


# ---------------------------------------------------------------------------
# Identity slot classification
# ---------------------------------------------------------------------------

ByIdVerdict = Literal["own", "foreign", "unattributed", "absent"]


def classify_by_id_rows(rows: Any, pack_id: str) -> ByIdVerdict:
    """Classify every row sharing a ``node_id`` against the target pack.

    Takes ALL rows (``GraphStoreExtended.get_nodes_by_id``), never one.
    ``get_node_by_id``'s ``LIMIT 1`` has no ``ORDER BY``, so with a node_id
    held under two node_types -- a shape this codebase supports and pins --
    which row it returns is undefined, and an unattributed row winning the
    draw would wave a foreign one through.

    - any row already in ``pack_id`` -> ``own`` (this is the owner updating
      their node; on the backends where node_id alone is the primary key an
      "own plus foreign" state cannot exist, and on the ones where it can,
      the exact ``(node_type, node_id)`` slot probe stays authoritative)
    - else any row attributed elsewhere -> ``foreign``, INCLUDING when
      unattributed rows are mixed in (fail-closed: that mix is exactly the
      case ``LIMIT 1`` used to let through)
    - rows exist but none carry a pack_id -> ``unattributed`` (legacy data,
      not yet migrated)
    - no rows -> ``absent``
    """
    if not isinstance(rows, list):
        raise TypeError(f"expected a list of rows, got {type(rows).__name__}")
    if not rows:
        return "absent"
    seen_foreign = False
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError(f"expected row mappings, got {type(row).__name__}")
        row_pack = row.get("pack_id")
        if row_pack == pack_id:
            return "own"
        if row_pack:
            seen_foreign = True
    return "foreign" if seen_foreign else "unattributed"


def by_id_conflict(rows: Any, pack_id: str) -> bool:
    """True when the by-id axis says this write would take a foreign slot."""
    return classify_by_id_rows(rows, pack_id) == "foreign"


def normalize_tags(tags: MutableMapping[str, Any]) -> None:
    """Apply the retired-alias invariant (#159/#171) after stamping.

    Kept here so the ordering -- stamp first, normalise second -- lives next
    to the reason for it (see :func:`stamp`).
    """
    from opencrab.common.pack_tags import canonicalize_pack_alias

    canonicalize_pack_alias(tags)
