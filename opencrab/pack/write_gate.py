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

from opencrab.pack.ownership import PACK_STATUS_CREATING, PACK_STATUS_READY

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


def authorize(
    sql: Any,
    principal: Principal,
    pack_id: str,
    *,
    allowed_statuses: tuple[str, ...] = (PACK_STATUS_READY,),
) -> dict[str, Any]:
    """Owner-only write authorization for ``pack_id``.

    Thin pass-through to ``assert_writable`` so writers depend on the gate
    rather than reaching into the registry themselves. Raises
    ``PackNotFoundError`` for a missing pack, for a pack whose ``status`` is
    not in ``allowed_statuses`` (#170), AND for someone else's private pack
    (#143 invariant 7 -- all three must be indistinguishable), and
    ``PackForbiddenError`` for a visible, status-eligible pack owned by
    someone else.

    ``allowed_statuses`` defaults to ready-only. Lifecycle-readiness
    checking lives HERE, at the gate, rather than at each tool boundary
    (``pack_ingest``, ``ontology_add_node``, REST ``/api/nodes``, ...) --
    this module's own docstring already records why: a hand-maintained list
    of "places to guard" has missed one every time this codebase tried it.
    Putting the default here means every writer that goes through
    ``authorize`` is ready-only unless it explicitly widens the set (today,
    only ``OntologyBuilder.add_node``'s ``pack_anchor`` path does, and only
    to ``('creating',)``), instead of depending on every future call site
    remembering to check status itself.

    Fails closed when the registry is unreachable: without it there is no
    way to decide ownership, and "cannot check" must never mean "allowed".
    """
    if not getattr(sql, "available", False):
        raise RuntimeError(
            "pack registry unavailable; refusing the write (ownership cannot "
            "be verified)"
        )
    from opencrab.pack.ownership import assert_writable

    return assert_writable(sql, principal, pack_id, allowed_statuses=allowed_statuses)


def authorize_fork_copy(sql: Any, principal: Principal, pack_id: str) -> dict[str, Any]:
    """``pack_fork``'s bulk copy into a ``creating`` pack -- the ONE widening
    of the write gate this design makes (design v7 §4-C-2).

    Same owner-only rule as :func:`authorize`, with ``allowed_statuses``
    widened to ``('creating',)`` alone: content must land inside the
    reservation window, because "raise to ready first, then write" is wrong
    twice over -- it makes an observably partial copy visible in between,
    and once ``ready`` the row can no longer be demoted (``mark_pack_partial``
    is ``WHERE status='creating'``).

    That widening alone would also let any owner write into ANY ``creating``
    pack of their own -- including one ``pack_create`` still has in flight.
    Requiring ``forked_from`` closes that: ``pack_create``'s ``creating``
    rows never carry it, only rows ``begin_pack_creation(..., forked_from=src)``
    reserved by a fork do. This is what stops the widening from becoming a
    general "write into any creating pack" door.

    Raises ``ValueError``, NOT ``PackNotFoundError``: everyone who reaches
    this line has already passed ``authorize``'s owner check, so there is
    nothing left to hide from them -- answering "pack not found" about the
    caller's own pack they just reserved would mislead debugging instead of
    protecting anything.
    """
    row = authorize(sql, principal, pack_id, allowed_statuses=(PACK_STATUS_CREATING,))
    if not row.get("forked_from"):
        raise ValueError(
            "fork_copy is permitted only on a pack reserved by pack_fork"
        )
    return row


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


# ---------------------------------------------------------------------------
# Identity slot guard
# ---------------------------------------------------------------------------
#
# Promoted here from opencrab/mcp/tools/pack.py, where #146 first built it for
# pack_create/pack_ingest only. #148 gives every write an explicit pack_id --
# including ontology_add_node and REST /api/nodes, which had none before -- and
# that opens a re-attribution path the moment the guard is missing: node
# identity is NOT qualified by pack on any backend (Kuzu's primary key is
# node_id alone; every vector store keys on node_id globally; sqlite-vec's
# upsert deletes by node_id with no pack predicate), so writing a node_id that
# already lives in someone else's pack silently takes their slot.
#
# A probe is (store, method, args, path-to-pack_id-in-result).

_Probe = tuple[Any, str, tuple[Any, ...], tuple[str, ...]]

CONFLICT_FOREIGN = "foreign"
CONFLICT_UNVERIFIABLE = "unverifiable"


def _extract(result: Any, path: tuple[str, ...]) -> Any:
    value: Any = result
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _check_probes(pack_id: str, probes: list[_Probe]) -> str | None:
    """Every probe's slot against ``pack_id``.

    ``None`` when no conflict (including probes skipped because their store is
    absent/unavailable). ``"unverifiable"`` -- fail-closed -- when a probe
    method is missing, raises, or returns a shape we do not recognise: being
    unable to check must never read as "no conflict". ``"foreign"`` when a
    truthy extracted value differs from ``pack_id``; a falsy one is
    unattributed legacy data and passes.
    """
    for store, method_name, args, path in probes:
        if store is None or not getattr(store, "available", False):
            continue
        method = getattr(store, method_name, None)
        if method is None:
            return CONFLICT_UNVERIFIABLE
        try:
            result = method(*args)
        except Exception:  # noqa: BLE001 -- any failure is "cannot verify"
            return CONFLICT_UNVERIFIABLE
        if result is None:
            continue
        if not isinstance(result, Mapping):
            return CONFLICT_UNVERIFIABLE
        value = _extract(result, path)
        if value and value != pack_id:
            return CONFLICT_FOREIGN
    return None


def _check_by_id_axis(graph: Any, node_id: str, pack_id: str) -> str | None:
    """The type-agnostic axis, over ALL rows sharing ``node_id``.

    Deliberately NOT ``get_node_by_id``: its query is ``LIMIT 1`` with no
    ``ORDER BY``, and a node_id held under two node_types is a shape this
    codebase supports and pins. Which row that returned was undefined, so an
    unattributed row winning the draw waved a foreign one through.
    """
    if graph is None or not getattr(graph, "available", False):
        return None
    method = getattr(graph, "get_nodes_by_id", None)
    if method is None:
        return CONFLICT_UNVERIFIABLE
    try:
        rows = method(node_id)
    except Exception:  # noqa: BLE001
        return CONFLICT_UNVERIFIABLE
    try:
        verdict = classify_by_id_rows(rows, pack_id)
    except TypeError:
        return CONFLICT_UNVERIFIABLE
    return CONFLICT_FOREIGN if verdict == "foreign" else None


def node_identity_conflict(
    graph: Any, docs: Any, vector: Any, *, space: str, node_type: str,
    node_id: str, pack_id: str,
) -> str | None:
    """Would writing this node take a slot attributed to another pack?

    Four slots, all mandatory. The exact ``(node_type, node_id)`` graph slot
    keeps the strict rule; only the type-agnostic axis uses the all-rows
    classification. Knowing the graph slot is ours proves nothing about the
    doc and vector slots -- the builder overwrites those in the same call,
    keyed by ``(space, node_id)`` and by ``node_id`` alone.
    """
    reason = _check_probes(pack_id, [
        (graph, "get_node", (node_type, node_id), ("pack_id",)),
        (docs, "get_node_doc", (space, node_id), ("properties", "pack_id")),
        (vector, "get_by_id", (node_id,), ("metadata", "pack_id")),
    ])
    return reason or _check_by_id_axis(graph, node_id, pack_id)


def edge_identity_conflict(
    graph: Any, *, from_type: str, from_id: str, relation: str, to_type: str,
    to_id: str, pack_id: str,
) -> str | None:
    """Keyed by the backend's own upsert conflict key (see GraphStore.get_edge).
    Edge sql-registry and audit rows have no pack_id column to guard."""
    return _check_probes(pack_id, [
        (graph, "get_edge", (from_type, from_id, relation, to_type, to_id), ("pack_id",)),
    ])


def source_identity_conflict(
    docs: Any, vector: Any, *, source_id: str, pack_id: str
) -> str | None:
    """The legacy (text_as_node=False) text path: doc_sources + vector, keyed
    by source_id alone -- there is no graph node here."""
    return _check_probes(pack_id, [
        (docs, "get_source", (source_id,), ("metadata", "pack_id")),
        (vector, "get_by_id", (source_id,), ("metadata", "pack_id")),
    ])


def identity_reject_message(kind: str, ident: str, reason: str) -> str:
    """Fixed wording -- #143 invariant 7 means this must NEVER name the other
    pack's id, owner, title, or visibility."""
    if reason == CONFLICT_UNVERIFIABLE:
        return f"{ident}: cannot verify existing ownership on this backend"
    if kind == "edge":
        return f"{ident}: edge identity is already attributed to a different pack"
    if kind == "source":
        return f"{ident}: source identity is already attributed to a different pack"
    return f"{ident}: identity is already attributed to a different pack"


def resolved_endpoint_pack_conflict(
    graph: Any, node_type: str, node_id: str, pack_id: str
) -> str | None:
    """Is the endpoint row the writer will actually attach to foreign?

    Checks the exact ``(node_type, node_id)`` row -- the one the caller's
    ``lookup_node_type`` just resolved -- rather than "any row with this id".
    The by-id form below passes as soon as it sees a row in the target pack,
    but with the same id held under two node_types (a supported shape) the
    unordered lookup can still select the OTHER pack's row, and the edge then
    attaches to an endpoint outside its own pack. Scoped edge export requires
    both endpoints in scope, so that edge is written and immediately invisible.
    """
    return _check_probes(pack_id, [
        (graph, "get_node", (node_type, node_id), ("pack_id",)),
    ])


def endpoint_pack_conflict(graph: Any, node_id: str, pack_id: str) -> str | None:
    """Is this edge endpoint attributed to a pack other than ``pack_id``?

    An edge whose endpoints straddle two packs is invisible to its own pack's
    readers: ``export_edges_scoped`` requires BOTH endpoints to be in scope, so
    the row exists and never appears. Rather than write that, refuse it.

    Unattributed endpoints pass. Legacy nodes carry no pack_id and the seed
    scripts still create them; a rule that rejected those would refuse edges
    over data this repo has not finished migrating (the ``default`` pack in
    scripts/migrate_pack_ownership.py exists precisely because such rows are
    still around).
    """
    if graph is None or not getattr(graph, "available", False):
        return None
    method = getattr(graph, "get_nodes_by_id", None)
    if method is None:
        return CONFLICT_UNVERIFIABLE
    try:
        rows = method(node_id)
    except Exception:  # noqa: BLE001
        return CONFLICT_UNVERIFIABLE
    try:
        verdict = classify_by_id_rows(rows, pack_id)
    except TypeError:
        return CONFLICT_UNVERIFIABLE
    return CONFLICT_FOREIGN if verdict == "foreign" else None
