"""``pack_fork`` orchestrator (issue #201, design v7 §5) -- the third write
chokepoint's ONLY caller.

Copies one pack's nodes, edges, sources, and vectors into a brand-new pack
under a fresh ``creating`` registry row, remapping every content id with a
per-call salt (``opencrab.pack.fork_remap``) because identity is not
pack-scoped (#197) and the copy must not collide with the original. Vectors
are imported RAW -- never re-embedded (#200) -- via each backend's own
``export_pack_vectors``/``import_vectors`` pair.

Everything here runs in the strict order design §5 lays out and that
ordering is deliberate, not an optimisation target:

  §5-1  preflight (steps 1-8b)   -- NO registry row, NO writes of any kind.
                                     Every rejection here leaves nothing
                                     behind: zero registry rows, zero graph
                                     anchors.
  §5-2  reservation (steps 9-12) -- ``begin_pack_creation`` claims the new
                                     slug. The delete window is exactly
                                     steps 10-12 (design §12-4): a mapping
                                     collision, a non-empty ``dst``, or an
                                     identity conflict all compensate with a
                                     confirmed DELETE here, before any
                                     writer has been called.
  §5-3  writes (steps 13-17)     -- anchor, then nodes, edges, sources,
                                     vectors, all via ``fork_copy``/
                                     ``origin="server"``. NOTHING deletes the
                                     registry row past this point (design
                                     §6-1) -- a write failure demotes to
                                     ``partial`` instead.
  §5-4  verdict (steps 18-20)    -- tally losses, promote or demote, report.

Two-tier error model (design §6-2), threaded through every step:

  Tier 1 -- the ORIGINAL data was already broken (missing ``props["id"]``,
            grammar-invalid, a legacy alias conflict, a dangling vector
            reference). Reported via ``skipped``/``errors``, execution
            CONTINUES, and it does NOT by itself block promotion to
            ``ready`` -- as long as the completeness floor (§5-1 step 8b,
            ``FORK_MAX_LOSS_RATIO``) holds.
  Tier 2 -- OUR OWN write attempt failed (a ``_fork_leg_ok`` check came back
            False, ``import_vectors`` raised, or the post-write
            reference-integrity check found a mapped id that never landed).
            This HALTS the write phase immediately and demotes the new
            pack's registry row to ``partial``.

``_fork_leg_ok`` (design §6-3) is a POSITIVE check per write kind, never a
negative list -- "does not start with 'error:'" already fail-opened once in
this codebase's history (an incident this function's docstring exists to
not repeat): a status this module has never seen (a typo, a new backend's
new string) reads as success under a negative list and as failure here,
which is the only direction that is safe to be wrong in.
"""

from __future__ import annotations

import logging
from typing import Any

from opencrab.auth import Principal
from opencrab.common.pack_tags import canonicalize_pack_alias
from opencrab.grammar.validator import validate_edge, validate_node, validate_node_properties
from opencrab.pack.fork_remap import (
    REFERENCE_KEYS,
    build_mapping,
    new_salt,
    remap_props,
    remap_vector_metadata,
    surviving_source_ids,
)
from opencrab.pack.ownership import (
    anchor_node_id,
    begin_pack_creation,
    delete_pack_row,
    get_pack,
    mark_pack_partial,
    mark_pack_ready,
)
from opencrab.pack.source_writer import write_source
from opencrab.pack.write_gate import (
    identity_reject_message,
    node_identity_conflict,
    source_identity_conflict,
)
from opencrab.stores._vector_base import validate_import_records

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# Design v7 leaves the CAP values themselves unspecified -- only the
# *mechanism* (CAP+1 read, reject on truncation, constant-memory pack-scoped
# vector pre-count) is normative (§5-1 step 4, H9). These four numbers are a
# judgment call made here, not derived from the design doc: high enough that
# no real single-pack fork should ever hit them, low enough that a CAP+1
# preflight read stays comfortably bounded in memory. An operator who needs
# to fork a larger pack raises these constants; nothing else in the design
# ties a contract to their specific values.
FORK_MAX_NODES = 20_000
FORK_MAX_EDGES = 50_000
FORK_MAX_SOURCES = 10_000
FORK_MAX_VECTORS = 50_000

# Design v7 §5-1 step 8b / §6, verbatim value.
FORK_MAX_LOSS_RATIO = 0.10

# Length budget for the new pack_id (design v7 §3): ``begin_pack_creation``'s
# collision suffix is exactly ``f"{pack_id}-{secrets.token_hex(4)}"`` -- 1
# (``-``) + 8 (hex) = 9 extra characters -- against the registry column's
# 256-char limit.
_PACK_ID_COLUMN_LIMIT = 256
_PACK_ID_COLLISION_SUFFIX_LEN = 9
_PACK_ID_BUDGET = _PACK_ID_COLUMN_LIMIT - _PACK_ID_COLLISION_SUFFIX_LEN
_DEFAULT_SLUG_SUFFIX = "-fork"


# ---------------------------------------------------------------------------
# §6-3 positive-leg-success checks
# ---------------------------------------------------------------------------


def _leg_ok(status: Any) -> bool:
    """The shared positive-check primitive for graph/docs/sql legs.

    Mirrors ``opencrab.ontology.builder``'s own internal
    ``store_write_succeeded``/``_is_ok`` reading: exactly ``"ok"`` or a
    string that starts with ``"ok ("`` (the decorated ``"ok (id=...)"``
    shape docs/sql legs use). Anything else -- ``"unavailable"``,
    ``"error: ..."``, ``"no match"``, a raised exception's message, a
    backend's brand-new status string this module has never seen -- is NOT
    success. Not a member of a curated failure list; a member of exactly one
    success shape.
    """
    return isinstance(status, str) and (status == "ok" or status.startswith("ok ("))


def _fork_leg_ok(receipt: Any, kind: str) -> bool:
    """Positively confirm that every leg a fork write *attempted* actually
    landed (design §6-3). Never inferred from the absence of an error
    string -- see this module's docstring for the incident that pattern
    caused.

    ``"unavailable"`` is always a failure here: §5-1-3's preflight already
    required graph/docs/vector to all be available before the write phase
    ever starts, so a leg reporting ``"unavailable"`` at write time means
    something changed underneath this call, not a benign deployment shape.
    """
    stores = receipt.get("stores") if isinstance(receipt, dict) else None
    if not isinstance(stores, dict):
        return False
    if kind == "node":
        # fork_copy node write: graph/docs/sql land normally; the vector leg
        # is explicitly turned off (raw-copy contract, #200) and must read
        # back exactly "skipped (raw copy)" -- not "unavailable", not "ok".
        return (
            stores.get("graph") == "ok"
            and _leg_ok(stores.get("docs"))
            and _leg_ok(stores.get("sql"))
            and stores.get("vector") == "skipped (raw copy)"
        )
    if kind == "anchor":
        # The anchor does NOT turn write_vector off -- the new pack needs a
        # real anchor vector, so all four legs must positively succeed.
        return (
            stores.get("graph") == "ok"
            and _leg_ok(stores.get("docs"))
            and _leg_ok(stores.get("sql"))
            and stores.get("vector") == "ok"
        )
    if kind == "edge":
        # Edges have no vector leg. The docs leg ("audited", a Mongo audit
        # record) is deliberately excluded here -- design §6-3 only names
        # graph+sql for edges, and a missing audit entry is not a reason to
        # demote a pack to partial.
        return stores.get("graph") == "ok" and _leg_ok(stores.get("sql"))
    if kind == "source":
        return (
            _leg_ok(stores.get("documents"))
            and stores.get("chromadb") == "skipped (raw copy)"
        )
    raise ValueError(f"_fork_leg_ok: unknown kind {kind!r}")


# ---------------------------------------------------------------------------
# Constant-memory, pack-scoped vector pre-count (design §5-1 step 4, H9)
# ---------------------------------------------------------------------------
# Structurally mirrors opencrab.pack.load._vec_backend's own three-backend
# dispatch (kind literals "sql"/"chroma"/"sqlalchemy", fail-closed None for
# unavailable/unrecognized stores) -- kept local per design's explicit
# instruction ("이 건수 재기는 세 백엔드 공통 헬퍼로 fork 안에 둔다") rather
# than added to load.py, since load.py's own helper answers a different
# question (live id enumeration, unbounded) that this call must NOT reuse:
# a malicious or merely huge public-fork-enabled pack must never make this
# preflight step materialize an unbounded id list or run a whole-collection
# count() (which isn't even pack-scoped).


def _vec_backend(vec: Any) -> tuple[str | None, Any, str | None]:
    if not getattr(vec, "available", False):
        return (None, None, None)
    conn = getattr(vec, "_conn", None) or getattr(vec, "conn", None)
    if conn is not None:
        return ("sql", conn, getattr(vec, "_table", None) or getattr(vec, "table_name", "vectors_kure"))
    collection_handle = getattr(vec, "_collection_handle", None)
    if callable(collection_handle):
        # design §12-5: chroma_store's own snapshot accessor -- taken under
        # its lock so a concurrent reset_collection() swap is never
        # observed half-applied. Prefer it over the raw `_collection`
        # attribute below, which that lock does not protect.
        return ("chroma", collection_handle(), None)
    if hasattr(vec, "_collection"):
        return ("chroma", vec._collection, None)
    engine = getattr(vec, "_engine", None)
    if engine is not None:
        return ("sqlalchemy", engine, getattr(vec, "_table", None) or "vectors")
    return (None, None, None)


def _count_pack_vectors(vec: Any, pack_id: str, cap: int) -> int | None:
    """``COUNT(*) WHERE pack_id = ...``, capped, in constant memory.

    Returns the count on success. Returns ``None`` when the backend is
    unavailable or of an unrecognized shape -- fail-closed: a caller that
    cannot count must not treat that as "zero vectors" (the same distinction
    ``load.py``'s ``_live_vec_ids`` draws for the same reason).

    sql/sqlalchemy: the database does the counting, so no LIMIT is needed --
    ``COUNT(*)`` never materializes the matched rows regardless of how many
    there are. chroma has no server-side pack-scoped COUNT; ``.get()`` is the
    only way to learn how many ids match, so it is called with
    ``limit=cap + 1`` (never the whole collection) and only the id list's
    length is used -- embeddings/documents/metadatas are never requested
    (``include=[]``).
    """
    kind, handle, table = _vec_backend(vec)
    if kind is None:
        return None
    if kind == "sql":
        row = handle.execute(
            f"SELECT COUNT(*) FROM {table} WHERE pack_id = ?", (pack_id,)  # noqa: S608
        ).fetchone()
        return int(row[0]) if row else 0
    if kind == "chroma":
        result = handle.get(where={"pack_id": pack_id}, limit=cap + 1, include=[])
        ids = result.get("ids") if isinstance(result, dict) else None
        if not isinstance(ids, list):
            return None
        return len(ids)
    if kind == "sqlalchemy":
        import sqlalchemy

        with handle.connect() as conn:
            row = conn.execute(
                sqlalchemy.text(f"SELECT COUNT(*) FROM {table} WHERE pack_id = :p"),  # noqa: S608
                {"p": pack_id},
            ).fetchone()
        return int(row[0]) if row else 0
    return None


# ---------------------------------------------------------------------------
# Small preflight helpers
# ---------------------------------------------------------------------------


def _node_id(record: dict[str, Any]) -> str | None:
    props = record.get("props")
    if not isinstance(props, dict):
        return None
    node_id = props.get("id")
    return node_id if isinstance(node_id, str) and node_id else None


def _source_id(record: dict[str, Any]) -> str | None:
    source_id = record.get("source_id")
    return source_id if isinstance(source_id, str) and source_id else None


# ---------------------------------------------------------------------------
# §5-4 step 18 / §4-A predicates 1-2 (H4): post-write re-read verification
# ---------------------------------------------------------------------------
# Predicate 3 (H5: imported vector ids (new node ids | new source ids)) is
# already guaranteed by construction -- step 17's import_batch only ever
# contains records whose id was already confirmed present in `mapping`'s
# value-space (the §5-1 step 6b classification), and step 17 only runs at
# all once the node/source write loops (14/16) have each completed in full
# without a break, so every id vectors could import was already written as
# a node or source in `dst`. Predicates 1-2 are NOT guaranteed by
# construction in the same way -- they are only as good as remap_props'/
# remap_vector_metadata's own correctness, and the store round-trip
# in between (serialization, a store silently truncating/rewriting a
# value) is exactly the kind of thing a write-time-only check cannot see.
# This is why design names it a SEPARATE re-read step rather than folding
# it into the write loops' own receipt checks.


def _h4_scan(obj: dict[str, Any], mapping_keys: set[str], src_pack: str, *, check_pack_id: bool = False) -> list[str]:
    """Scan ``obj``'s top-level ``REFERENCE_KEYS`` positions (the exact
    domain ``remap_props``/``remap_vector_metadata`` themselves rewrite) for
    a value that is still a source-space id (a key of ``mapping``) or still
    the literal ``src_pack`` string. ``check_pack_id`` additionally checks
    the (non-``REFERENCE_KEYS``) ``pack_id`` position -- design rule 4's own
    verification target, relevant to vector metadata only (node/edge/source
    writers stamp ``pack_id`` fresh from the ``pack_id=dst`` kwarg, never
    from copied data, so they carry no equivalent risk).
    """
    hits: list[str] = []
    for key in REFERENCE_KEYS:
        value = obj.get(key)
        if not isinstance(value, str):
            continue
        if value in mapping_keys:
            hits.append(f"{key}={value!r} (unremapped source-space id)")
        elif value == src_pack:
            hits.append(f"{key}={value!r} (unremapped source pack_id)")
    if check_pack_id:
        pid = obj.get("pack_id")
        if pid == src_pack:
            hits.append(f"pack_id={pid!r} (unremapped source pack_id)")
    return hits


def _h4_verify(
    graph: Any, docs: Any, vector: Any, *,
    dst: str, mapping: dict[str, str],
    written_nodes: int, written_edges: int, written_sources: int, written_vectors: int,
    src_pack_id: str,
) -> list[str]:
    """Re-read the just-written copy and confirm design §4-A predicates 1-2
    against it. Returns a list of hit descriptions (empty = clean). Limits
    are the EXACT counts this call itself just wrote, not the §5-1 CAPs --
    this is re-reading our own bounded output, not untrusted source data.
    """
    mapping_keys = set(mapping.keys())
    hits: list[str] = []

    if written_nodes:
        for row in graph.export_nodes_scoped([dst], written_nodes + 1):
            props = row.get("props") or {}
            hits.extend(_h4_scan(props, mapping_keys, src_pack_id))

    if written_edges:
        for row in graph.export_edges_scoped([dst], written_edges + 1):
            rel_props = row.get("rel_props") or {}
            hits.extend(_h4_scan(rel_props, mapping_keys, src_pack_id))
            for endpoint_key in ("source_props", "target_props"):
                endpoint_id = (row.get(endpoint_key) or {}).get("id")
                if isinstance(endpoint_id, str) and endpoint_id in mapping_keys:
                    hits.append(f"{endpoint_key}.id={endpoint_id!r} (unremapped source-space id)")

    if written_sources:
        for row in docs.list_sources_scoped([dst], written_sources + 1):
            # design §12-3: the row's own STRUCTURAL source_id column, not
            # just metadata -- a source whose id itself was never remapped
            # (unlike a node, whose id lives inside props/REFERENCE_KEYS,
            # a source's id is a top-level column `_h4_scan` never looks
            # at).
            row_source_id = row.get("source_id")
            if isinstance(row_source_id, str) and row_source_id in mapping_keys:
                hits.append(f"source_id={row_source_id!r} (unremapped source-space id)")
            metadata = row.get("metadata") or {}
            hits.extend(_h4_scan(metadata, mapping_keys, src_pack_id))

    if written_vectors:
        for row in vector.export_pack_vectors(dst):
            metadata = row.get("metadata") or {}
            hits.extend(_h4_scan(metadata, mapping_keys, src_pack_id, check_pack_id=True))

    return hits


class _RejectedError(Exception):
    """Internal control-flow signal for a preflight/reservation rejection.

    Carries the exact response dict to return -- raised instead of returned
    up through several nested loops so a rejection anywhere in preflight
    (§5-1) unwinds cleanly to one place, before any registry row exists.
    """

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(response.get("error"))
        self.response = response


def _reject(message: str, *, hint: str | None = None) -> _RejectedError:
    resp: dict[str, Any] = {"error": message}
    if hint:
        resp["hint"] = hint
    return _RejectedError(resp)


def _declared_limit_reject(detail: str) -> _RejectedError:
    """A #197-declared-limit preflight rejection (design §12-3): every shape
    this covers is one the store itself already accepts on a normal write
    path -- it is only FORKING that cannot be made safe for it yet, because
    identity is not pack-scoped (#197) and the copy would either collide
    with the destination anchor or lose data non-deterministically. Says so
    explicitly rather than reading like the source data itself is broken.
    """
    return _reject(
        f"{detail}; this shape is supported by the store but cannot be "
        "forked until issue #197 (identity is not pack-scoped) is resolved "
        "-- fix the source data (rename/remove the conflicting id) or wait "
        "for #197",
    )


def _store_contract_violation_reject(detail: str) -> _RejectedError:
    """A preflight rejection for a shape that should be UNREACHABLE via the
    store's own write path (design §12-11 contract 5) -- unlike
    ``_declared_limit_reject``, this is NOT "supported by the store but not
    yet forkable pending #197"; it is a sign the store itself violated its
    own contract (e.g. a duplicate primary key the doc store's schema should
    already have prevented from ever being written). Wording says so
    explicitly rather than reading like an ordinary #197 declared limit.
    """
    return _reject(f"{detail}; this indicates a store contract violation (bug signal)")


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def fork_pack(
    sql: Any,
    graph: Any,
    docs: Any,
    vector: Any,
    hybrid: Any,
    builder: Any,
    *,
    principal: Principal,
    src_pack_id: str,
    new_pack_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Fork ``src_pack_id`` into a brand-new pack owned by ``principal``.

    Not idempotent (design §7): every call mints a fresh salt and, on
    success, a fresh ``pack_id`` -- retrying a call that already succeeded
    creates a SECOND pack, it does not resume or dedupe the first. There is
    no ``operation_id`` idempotency key (out of scope, design §9).

    Reference rewriting (``fork_remap.remap_props``/``remap_vector_metadata``)
    covers exact top-level scalar values under ``fork_remap.REFERENCE_KEYS``
    only. A reference nested in a dict/list, a composite/non-standard-key
    string, or a value that already pointed outside the source pack is left
    untouched and counted in the response's ``unverified_refs`` instead of
    being (possibly wrongly) rewritten.

    Returns a dict shaped ``{"status": "ok"|"partial", "pack_id": ...,
    "forked_from": ..., "visibility": "private", "copied": {...}, "skipped":
    {...}, "errors": {...}, "unverified_refs": ..., "registry_status_observed":
    ..., "registry_transition_confirmed": ...}`` on completion (design §3's
    schema names both statuses under this one shape -- "partial" adds an
    "error" key and reports actual progress so far, it does not shrink to a
    bare error string), or ``{"error": ..., "hint"?: ...}`` on a
    preflight/reservation rejection that left nothing behind.

    ``copied`` counts LOGICAL units this call itself confirmed every
    required leg of, not physical rows (design §12-5) -- a writer that left
    a leg behind mid-failure, or a chroma batch that partly landed, can
    leave more actual rows in a store than ``copied`` reports; that residue
    is not tracked here and its recovery path is the operator ``delete_pack``
    (design §6-1), not this response.

    ``registry_status_observed``/``registry_transition_confirmed`` (design
    §12-1) report what a post-write registry re-read OBSERVED at the moment
    it ran -- not a live guarantee, a concurrent actor can still move the
    row after this call returns. ``registry_status_observed`` is one of
    ``"ready"``, ``"partial"``, ``"creating"``, ``"missing"``, or
    ``"unknown"`` (the last when the confirming re-read itself failed).
    ``registry_transition_confirmed`` is ``True`` only when that observed
    value matches what this call was trying to leave behind (``"ready"`` on
    ``status: "ok"``, ``"partial"`` on ``status: "partial"``). ``False``
    does not mean the pack is broken -- it means THIS call could not verify
    the transition landed; the recovery path is the same operator
    ``delete_pack`` / re-inspection design §6-1 already names.
    """
    # ---------------- §5-1 step 2: source pack authorization ----------------
    # #143 invariant 7: nonexistent pack and someone else's private pack
    # return the SAME "pack not found" error. A public-read (not
    # public-fork) pack owned by someone else is already observable via
    # content_pack_list, so it gets its own, more specific message.
    src = get_pack(sql, src_pack_id)
    if (
        src is None
        or src.get("status") != "ready"
        or (src.get("owner_id") != principal.user_id and src.get("visibility") == "private")
    ):
        return {"error": "pack not found", "pack_id": src_pack_id}
    is_owner = src.get("owner_id") == principal.user_id
    if not is_owner and src.get("visibility") != "public-fork":
        return {
            "error": "pack is not fork-enabled",
            "pack_id": src_pack_id,
            "hint": "the owner has not published this pack with visibility=public-fork",
        }

    try:
        return _fork_pack_inner(
            sql, graph, docs, vector, hybrid, builder,
            principal=principal, src=src, src_pack_id=src_pack_id,
            new_pack_id=new_pack_id, title=title, description=description,
        )
    except _RejectedError as rejected:
        return rejected.response


def _fork_pack_inner(
    sql: Any, graph: Any, docs: Any, vector: Any, hybrid: Any, builder: Any,
    *, principal: Principal, src: dict[str, Any], src_pack_id: str,
    new_pack_id: str | None, title: str | None, description: str | None,
) -> dict[str, Any]:
    # ---------------- §5-1 step 3: fail-closed availability ----------------
    if not getattr(graph, "available", False):
        raise _reject("graph store unavailable")
    if not getattr(docs, "available", False):
        raise _reject("document store unavailable")
    if not getattr(vector, "available", False) or not (
        hasattr(vector, "export_pack_vectors") and hasattr(vector, "import_vectors")
    ):
        raise _reject("vector store unavailable")

    # ---------------- §5-1 step 4: CAP+1 reads, vector pre-count ----------------
    nodes = graph.export_nodes_scoped([src_pack_id], FORK_MAX_NODES + 1)
    if len(nodes) > FORK_MAX_NODES:
        raise _reject(f"pack too large to fork: more than {FORK_MAX_NODES} nodes")
    edges = graph.export_edges_scoped([src_pack_id], FORK_MAX_EDGES + 1)
    if len(edges) > FORK_MAX_EDGES:
        raise _reject(f"pack too large to fork: more than {FORK_MAX_EDGES} edges")
    sources = docs.list_sources_scoped([src_pack_id], FORK_MAX_SOURCES + 1)
    if len(sources) > FORK_MAX_SOURCES:
        raise _reject(f"pack too large to fork: more than {FORK_MAX_SOURCES} sources")

    vector_count = _count_pack_vectors(vector, src_pack_id, FORK_MAX_VECTORS)
    if vector_count is None:
        raise _reject("vector store does not support pack-scoped counting")
    if vector_count > FORK_MAX_VECTORS:
        raise _reject(f"pack too large to fork: more than {FORK_MAX_VECTORS} vectors")

    src_anchor = anchor_node_id(src_pack_id)

    # ---------------- §5-1 steps 5-6: id recovery + grammar/property + ----------------
    # ---------------- alias-conflict validation (all Tier 1)          ----------------
    node_errors: list[str] = []
    edge_errors: list[str] = []
    source_errors: list[str] = []
    skipped_alias_nodes = 0
    skipped_alias_sources = 0
    skipped_alias_edges = 0

    surviving_nodes_by_id: dict[str, dict[str, Any]] = {}
    surviving_node_ids: set[str] = set()
    src_anchor_seen = 0
    seen_node_types: dict[str, str] = {}
    for record in nodes:
        node_id = _node_id(record)
        if node_id is None:
            node_errors.append("node missing props['id']; skipped (Tier 1)")
            continue
        props = record["props"]
        labels = record.get("labels") or []
        node_type = labels[0] if labels else None
        space = props.get("space")
        if node_id == src_anchor:
            # The source pack's OWN anchor is never copied as an ordinary
            # node -- the new pack gets its own anchor at step 13. Counted
            # explicitly (not assumed to appear exactly once) so the
            # completeness-floor denominator below stays exact regardless
            # of whether export_nodes_scoped happens to include it. Design
            # §12-3: only a STRUCTURALLY genuine anchor (space="resource",
            # node_type="Dataset") counts as the intentional exclusion --
            # anything else sharing this id is a #197-declared-limit
            # rejection below, not a silent drop, because a wrong-shaped
            # row here would otherwise be counted as "intentionally
            # excluded" and vanish from both the copy AND the loss-ratio
            # denominator.
            if space == "resource" and node_type == "Dataset":
                src_anchor_seen += 1
                continue
            raise _declared_limit_reject(
                f"node {node_id!r} shares the pack anchor's id but is not the "
                "anchor itself (space != 'resource' or node_type != 'Dataset')"
            )
        if not node_type or not isinstance(space, str):
            node_errors.append(f"node {node_id!r} missing space/type; skipped (Tier 1)")
            continue
        if node_id in seen_node_types and seen_node_types[node_id] != node_type:
            # design §12-3: which of the two rows "wins" depends on
            # export_nodes_scoped's row order, which is undeclared (no
            # ORDER BY) -- there is no deterministic, lossless way to pick
            # one, so this is a whole-fork rejection, not a Tier 1 drop of
            # either row.
            raise _declared_limit_reject(
                f"node {node_id!r} appears with more than one node_type "
                f"({seen_node_types[node_id]!r} and {node_type!r})"
            )
        seen_node_types[node_id] = node_type
        grammar = validate_node(space, node_type)
        if grammar.valid:
            prop_result = validate_node_properties(node_type, props)
            grammar = prop_result
        if not grammar.valid:
            node_errors.append(f"node {node_id!r} failed grammar validation; skipped (Tier 1)")
            continue
        try:
            canonicalize_pack_alias(props)
        except ValueError:
            skipped_alias_nodes += 1
            continue
        surviving_nodes_by_id[node_id] = record
        surviving_node_ids.add(node_id)

    surviving_edges: list[dict[str, Any]] = []
    for record in edges:
        from_id = _node_id({"props": record.get("source_props") or {}})
        to_id = _node_id({"props": record.get("target_props") or {}})
        if from_id is None or to_id is None:
            edge_errors.append("edge endpoint missing props['id']; skipped (Tier 1)")
            continue
        # The source pack's own anchor is a permanent, always-surviving
        # endpoint even though it is deliberately excluded from
        # `surviving_node_ids` (it is never copied as an ordinary node --
        # design §4-A rule 2: "매핑의 dataset:{src}→dataset:{dst} 항목은 엣지
        # 끝점 재지정 전용이다"). An edge touching the anchor is not a
        # preflight casualty; it survives here and gets its anchor endpoint
        # repointed to the new anchor at write time via `mapping` (step 10
        # fixes `mapping[src_anchor]` to the real `dst_anchor`). Treating the
        # anchor as absent from survival (as opposed to absent from the
        # node-copy set) would spuriously drop every edge touching it as
        # "endpoint did not survive preflight (Tier 1)" -- exactly the
        # regression T28's reverse-mutation note calls out ("앵커 매핑 제거 →
        # 엣지가 구 앵커를 가리킴").
        from_ok = from_id in surviving_node_ids or from_id == src_anchor
        to_ok = to_id in surviving_node_ids or to_id == src_anchor
        if not from_ok or not to_ok:
            # Dropped because an endpoint node itself did not survive (id
            # missing, grammar-invalid, or alias-conflicted) -- not a defect
            # of the edge record itself, but Tier 1 either way (design §5-1
            # step 6: "그 항목에 종속된 엣지만 Tier 1 로 제외한다").
            edge_errors.append(
                f"edge {from_id!r}->{to_id!r} dropped: endpoint did not survive preflight (Tier 1)"
            )
            continue
        from_space = (record.get("source_props") or {}).get("space")
        to_space = (record.get("target_props") or {}).get("space")
        relation = record.get("relation")
        if not isinstance(from_space, str) or not isinstance(to_space, str) or not relation:
            edge_errors.append(f"edge {from_id!r}->{to_id!r} missing space/relation; skipped (Tier 1)")
            continue
        grammar = validate_edge(from_space, to_space, relation)
        if not grammar.valid:
            edge_errors.append(f"edge {from_id!r}->{to_id!r} failed grammar validation; skipped (Tier 1)")
            continue
        rel_props = dict(record.get("rel_props") or {})
        try:
            canonicalize_pack_alias(rel_props)
        except ValueError:
            skipped_alias_edges += 1
            continue
        record = dict(record)
        record["rel_props"] = rel_props
        surviving_edges.append(record)

    surviving_sources: list[dict[str, Any]] = []
    surviving_source_ids_set: set[str] = set()
    seen_source_ids: set[str] = set()
    for record in sources:
        source_id = _source_id(record)
        if source_id is None:
            source_errors.append("source missing source_id; skipped (Tier 1)")
            continue
        if source_id == src_anchor:
            # design §12-3: dropping this source as Tier 1 would silently
            # lose its vector (excluded by the anchor-id vector rule below)
            # while the copy still reports "ok"; copying it would make the
            # copy's source row impersonate the anchor id. Neither is
            # acceptable, so this rejects the whole fork instead.
            raise _declared_limit_reject(
                f"source {source_id!r} shares the pack anchor's id"
            )
        if source_id in seen_source_ids:
            # design §12-11 contract 5: the doc store's PK is source_id, so
            # this is unreachable via the normal SQL-backed store -- reaching
            # it at all is a store contract violation (a bug signal), not a
            # #197-declared limit the store itself otherwise accepts, hence
            # `_store_contract_violation_reject` rather than
            # `_declared_limit_reject`. The `appears more than once` wording
            # is kept verbatim (T59 asserts this exact substring).
            raise _store_contract_violation_reject(
                f"source {source_id!r} appears more than once in this pack"
            )
        seen_source_ids.add(source_id)
        metadata = dict(record.get("metadata") or {})
        try:
            canonicalize_pack_alias(metadata)
        except ValueError:
            skipped_alias_sources += 1
            continue
        record = dict(record)
        record["metadata"] = metadata
        surviving_sources.append(record)
        surviving_source_ids_set.add(source_id)

    # ---------------- §5-1 step 7 (built here; §6b's mapping-before- ----------------
    # ---------------- classification note applies to VECTOR classification, ----------------
    # ---------------- which needs this mapping and therefore runs after it) ----------------
    salt = new_salt()
    # The anchor entry is a PLACEHOLDER here (self-mapped): the real
    # destination pack_id is not known until begin_pack_creation runs in
    # §5-2 step 9. §6b's classification never actually consults this
    # placeholder -- an anchor-id vector record is intercepted by its OWN
    # first-checked rule (skipped.anchor_vector) before the "is this id in
    # the mapping" check ever runs, so the placeholder cannot leak into a
    # decision. Step 10 overwrites this with the real value once it exists.
    mapping = build_mapping(
        surviving_node_ids, surviving_source_ids_set,
        salt=salt, src_anchor=src_anchor, dst_anchor=src_anchor,
    )

    # ---------------- §5-1 step 6b: vector classification (uses mapping) ----------------
    # Positive-check philosophy per record, mirroring _fork_leg_ok's own
    # rule: each vector record is classified into exactly one bucket, first
    # match wins, in this fixed order --
    #   1. skipped.anchor_vector  -- id == src_anchor (excluded from the
    #      loss ratio: the new anchor's own vector replaces it, design §6).
    #   2. skipped.vector_orphans -- id not in mapping (neither a surviving
    #      node nor a surviving source -- points at something preflight
    #      already dropped, or something list_sources_scoped/
    #      export_nodes_scoped never returned).
    #   3. skipped.vector_mistagged -- metadata.pack_id present and != src
    #      (a legacy row that was never actually scoped to this pack).
    #   4. skipped.vector_invalid -- fails the PER-RECORD validity pre-check
    #      below (this module's own mirror of a subset of
    #      _vector_base.validate_import_records' per-record rules, run
    #      BEFORE ever calling import_vectors so a bad record cannot abort
    #      an otherwise-good batch and turn a Tier 1 loss into a Tier 2
    #      failure).
    #   5. skipped.vector_batch_invalid -- fails the BATCH-level
    #      decomposition pass below (duplicate id / non-uniform dimension --
    #      see the 2-pass block right after this loop).
    #   6. otherwise: survives, remapped, added to the import batch.
    exported_vectors = vector.export_pack_vectors(src_pack_id)
    vector_errors: list[str] = []
    skipped_anchor_vector = 0
    skipped_vector_orphans = 0
    skipped_vector_mistagged = 0
    skipped_vector_invalid = 0
    import_batch: list[dict[str, Any]] = []
    # design §12-2: matches what step 17's real `import_vectors` call will
    # actually pass for THIS backend -- chroma's own `import_vectors` calls
    # `allow_uris=True`; fixing this to False would reject any record
    # carrying a non-None `uris` value that a real fork of a chroma-backed
    # pack must accept.
    allow_uris = _vec_backend(vector)[0] == "chroma"
    for rec in exported_vectors:
        # design §12-2: shape check BEFORE any field access -- pgvector's
        # export can hand back non-dict metadata (see the mistagged check
        # below), and a non-dict RECORD itself, however unlikely, must not
        # crash this loop with an AttributeError on `.get`.
        if not isinstance(rec, dict):
            skipped_vector_invalid += 1
            vector_errors.append(
                f"vector record is not a dict, got {type(rec).__name__}; skipped (Tier 1)"
            )
            continue
        rec_id = rec.get("id")
        if rec_id == src_anchor:
            skipped_anchor_vector += 1
            continue
        if not isinstance(rec_id, str) or rec_id not in mapping:
            skipped_vector_orphans += 1
            continue
        meta = rec.get("metadata")
        # design §12-2: KEY EXISTENCE, not truthiness -- the real validator's
        # own rule is "a present pack_id of None is DIFFERENT, not absent".
        # The `isinstance` guard is load-bearing: a non-dict metadata (str,
        # int -- reachable via pgvector's json.loads of stored jsonb) must
        # not raise from `"pack_id" in meta` / `meta["pack_id"]` here, this
        # loop is still pre-reservation and nothing downstream catches it.
        if isinstance(meta, dict) and "pack_id" in meta and meta["pack_id"] != src_pack_id:
            skipped_vector_mistagged += 1
            continue
        problem = _vector_record_invalid(rec, pack_id=src_pack_id, allow_uris=allow_uris)
        if problem:
            skipped_vector_invalid += 1
            vector_errors.append(f"vector {rec_id!r} invalid, skipped (Tier 1): {problem}")
            continue
        import_batch.append(rec)

    # ---------------- §5-1 step 6b (cont'd): 2-pass batch decomposition ----------------
    # `validate_import_records` (the REAL validator `import_vectors` calls
    # at step 17) has two checks that are BATCH-level, not per-record:
    # duplicate `id` within the batch, and dimensional uniformity across the
    # batch. Design v7 §5-1-6b pins a deterministic 2-pass decomposition so
    # those batch failures become per-record Tier 1 losses HERE, in
    # preflight, instead of surfacing as a step-17 exception that the
    # generic `except Exception` there would misclassify Tier 2 -- demoting
    # an otherwise-successful, already-reserved fork to `partial` over one
    # bad exported record.
    #
    # pass 1 (this loop): walks `import_batch` -- already in
    # `export_pack_vectors`'s own order, since the loop above only ever
    # appends, never reorders or filters out-of-order -- and applies the
    # two batch checks in that fixed order. On a duplicate `id`, KEEP THE
    # FIRST occurrence and drop the later one(s). The reference dimension
    # is established from the FIRST SURVIVING record (the first record that
    # was not itself dropped as a duplicate) -- any later record whose
    # embedding length disagrees with that reference is dropped too. Both
    # drops are Tier 1 and share one counter, `skipped.vector_batch_invalid`
    # (mirrors the shape of `vector_orphans`/`vector_mistagged`/
    # `vector_invalid` above; two backend-real failure modes, one counter,
    # since both are "this record collided with the rest of the batch," not
    # two independently meaningful axes).
    skipped_vector_batch_invalid = 0
    seen_vector_ids: set[str] = set()
    reference_dim: int | None = None
    pass1_survivors: list[dict[str, Any]] = []
    for rec in import_batch:
        rec_id = rec["id"]  # guaranteed present/str -- _vector_record_invalid already checked it.
        if rec_id in seen_vector_ids:
            skipped_vector_batch_invalid += 1
            vector_errors.append(
                f"vector {rec_id!r} duplicate id within export batch, "
                "skipped (Tier 1): keeping the first occurrence"
            )
            continue
        embedding_len = len(rec.get("embedding") or [])
        if reference_dim is None:
            reference_dim = embedding_len
        elif embedding_len != reference_dim:
            skipped_vector_batch_invalid += 1
            vector_errors.append(
                f"vector {rec_id!r} embedding dim {embedding_len} != batch "
                f"reference dim {reference_dim} (from the first surviving "
                "record), skipped (Tier 1)"
            )
            continue
        seen_vector_ids.add(rec_id)
        pass1_survivors.append(rec)
    import_batch = pass1_survivors

    # Intended limit (design §5-1-6b "의도된 한계 (증폭)"): the reference
    # dimension above is always "whatever the first surviving record's
    # embedding length is" -- there is no independently-known correct
    # dimension to check against instead. Chroma has no app-side table dim
    # at all (`dim=None` below, always), so if the FIRST surviving record
    # happens to be the one bad-dimension record, every OTHER, perfectly
    # good record in the batch is dropped here as "the mismatch" against
    # that bad reference -- which can push the vector axis past step 8b's
    # completeness floor and reject the whole fork over a single corrupt
    # export row. This is deliberate, not a bug to "improve": a
    # majority-vote reference dimension was considered and rejected by the
    # design as non-deterministic on a tie, and `validate_import_records`
    # itself uses the very same "record 0" rule -- picking a different rule
    # here would make this module's notion of "the" batch dimension diverge
    # from the real validator's, defeating the whole point of pass 2 below.
    #
    # pass 2: re-run the REAL validator over the decomposed survivors to
    # confirm the decomposition actually worked. `pack_id=src_pack_id` (not
    # `dst`, which does not exist until §5-2 step 9) because every
    # surviving record's metadata `pack_id` is still absent-or-`src_pack_id`
    # here -- anything else was already dropped as `vector_mistagged`
    # above; the rewrite to `dst` happens for real at step 17 via
    # `remap_vector_metadata`. `dim`/`allow_uris` mirror exactly what this
    # backend's own `import_vectors` passes internally (sqlite-vec/pgvector:
    # their fixed `self._dim`; chroma: no such attribute, so `getattr(...,
    # None)` naturally yields `None`, matching chroma's own call site,
    # which never passes `dim` at all) -- so pass 2 checks exactly what
    # step 17 will.
    if import_batch:
        vec_kind, _, _ = _vec_backend(vector)
        try:
            validate_import_records(
                import_batch,
                pack_id=src_pack_id,
                dim=getattr(vector, "_dim", None),
                allow_uris=(vec_kind == "chroma"),
            )
        except Exception as exc:
            # Design §5-1-6b: a pass-2 failure means the DECOMPOSITION
            # itself has a defect -- not an original-data Tier 1 loss -- so
            # this refuses the WHOLE preflight rather than reporting a
            # partial loss. Pre-reservation: zero registry rows, zero graph
            # anchors (same guarantee as every other §5-1 rejection).
            raise _reject(
                f"internal error: vector batch decomposition failed "
                f"re-validation: {exc}"
            ) from exc

    # ---------------- §5-1 step 8: pack_id / slug length budget ----------------
    # An explicitly caller-supplied new_pack_id that is too long is rejected
    # (design §3: silently truncating a name the caller chose on purpose
    # would be surprising -- the caller can just pick a shorter one). The
    # AUTO-GENERATED default is handled differently: design §3 explicitly
    # directs truncating src_pack_id itself so "{src}-fork" always fits,
    # leaving room for both the "-fork" suffix and begin_pack_creation's own
    # collision suffix -- a pack whose only "fault" is a long name must
    # still be forkable via the default path without the caller having to
    # work around it by hand.
    if new_pack_id:
        requested_slug = new_pack_id
        if len(requested_slug) > _PACK_ID_BUDGET:
            raise _reject(
                f"new_pack_id exceeds the {_PACK_ID_BUDGET}-character budget"
            )
    else:
        requested_slug = f"{src_pack_id}{_DEFAULT_SLUG_SUFFIX}"
        if len(requested_slug) > _PACK_ID_BUDGET:
            truncated_len = _PACK_ID_BUDGET - len(_DEFAULT_SLUG_SUFFIX)
            requested_slug = f"{src_pack_id[:truncated_len]}{_DEFAULT_SLUG_SUFFIX}"

    # ---------------- §5-1 step 8b: completeness floor per axis ----------------
    def _check_floor(name: str, total: int, dropped: int) -> None:
        if total == 0:
            return
        if dropped / total > FORK_MAX_LOSS_RATIO:
            raise _reject(
                f"fork rejected: {name} loss ratio {dropped}/{total} exceeds the "
                f"{FORK_MAX_LOSS_RATIO:.0%} completeness floor"
            )

    nodes_dropped = len(nodes) - len(surviving_nodes_by_id) - src_anchor_seen
    nodes_dropped = max(nodes_dropped, 0)
    _check_floor("node", len(nodes), nodes_dropped)
    _check_floor("edge", len(edges), len(edges) - len(surviving_edges))
    _check_floor("source", len(sources), len(sources) - len(surviving_sources))
    vectors_dropped = (
        skipped_vector_orphans + skipped_vector_mistagged
        + skipped_vector_invalid + skipped_vector_batch_invalid
    )
    _check_floor("vector", len(exported_vectors), vectors_dropped)

    # =====================================================================
    # §5-2 reservation (steps 9-12)
    # =====================================================================
    owner_id = principal.user_id
    fork_title = title if title is not None else src.get("title")
    fork_description = description if description is not None else src.get("description")
    try:
        dst = begin_pack_creation(
            sql, owner_id, requested_slug,
            title=fork_title, description=fork_description, forked_from=src_pack_id,
        )
    except Exception as exc:
        raise _reject(f"pack registration failed: {exc}") from exc

    def _compensate_reservation(reason: str) -> _RejectedError:
        """Confirmed (never assumed) compensating delete of the just-reserved
        ``creating`` row (design §12-4). Never raises -- even a failed
        cleanup still returns a well-formed rejection, it just adds an
        operator-inspection note instead of silently claiming success. Never
        promises a later, unguaranteed operator action ("will age-demote").
        """
        try:
            deleted = delete_pack_row(sql, dst, owner_id, only_status=("creating",))
        except Exception:
            deleted = False
        if deleted:
            return _reject(reason)

        requery_ok = True
        row: dict[str, Any] | None = None
        try:
            row = get_pack(sql, dst)
        except Exception:
            requery_ok = False

        if requery_ok and row is None:
            # Already gone -- either the delete above committed and only its
            # return value was lost, or another actor removed it first. The
            # goal (no reserved row left behind) is already achieved.
            return _reject(reason)
        # design §12-11 contract 3: `get_pack` is ownership-unscoped, so a
        # bare `owner_id` match is not enough to trust this row as OUR
        # reservation -- a same-owner row from a DIFFERENT fork (a different
        # `forked_from`) must not leak its status/pack_id into this neutral
        # cleanup-failure message either.
        if (
            requery_ok
            and row is not None
            and row.get("owner_id") == owner_id
            and row.get("forked_from") == src_pack_id
        ):
            return _reject(
                f"{reason}; the reserved pack {dst!r} could not be cleaned up "
                f"(observed status {row.get('status')!r}); operator inspection required"
            )
        # Someone else's row, a same-owner row from a different fork, or the
        # requery itself failed (should not happen for a fresh reservation,
        # but #143 invariant 7 forbids exposing another owner's row state
        # regardless): one neutral message, no status, no pack_id.
        return _reject(
            f"{reason}; reservation cleanup could not be confirmed; operator inspection required"
        )

    # =====================================================================
    # R1 -- steps 10 (cont'd) through 12: reservation exists, no writer has
    # been called yet, so any rejection here -- expected (mapping collision,
    # non-empty dst, identity conflict) or unexpected (a store raising) --
    # compensates with a DELETE, never a demote (design §12-1). The
    # `except _RejectedError: raise` carve-out matters: every expected
    # rejection below already compensates itself via `_compensate_reservation`
    # before raising, so re-catching it in the generic branch would delete
    # twice and re-wrap `identity_reject_message`'s exact wording into
    # "pre-write check failed: ...".
    # =====================================================================
    try:
        # step 10: fix up the mapping's anchor entry now that dst is known
        # (design §12-11 contract 1: kept as the R1 `try`'s first statements
        # so the region boundary matches §12-1's own description of R1 --
        # both lines are, in practice, incapable of raising (an f-string and
        # a dict assignment), but the boundary itself is what this aligns).
        dst_anchor = anchor_node_id(dst)
        mapping[src_anchor] = dst_anchor

        # step 10 (cont'd): the anchor fix-up above must not have collided
        # with any other mapping value -- design §12-3's four preflight
        # rejections already remove every known cause, so a collision here
        # is a bug signal, not an original-data Tier 1 loss.
        if len(set(mapping.values())) != len(mapping):
            raise _compensate_reservation(
                "internal error: id mapping is not injective after anchor fix-up"
            )

        # step 11: dst must be genuinely empty (defense in depth -- a
        # freshly negotiated unique slug should never already have
        # content). Design §5-2 step 11 names all FOUR axes explicitly
        # ("노드·엣지·소스·벡터가 전부 비어 있을 것") -- the vector check
        # uses the same pack-scoped counter as §5-1 step 4 (cap=1 is
        # enough: any count other than exactly 0, or an inability to count
        # at all, is disqualifying here).
        dst_vector_count = _count_pack_vectors(vector, dst, 1)
        if (
            graph.export_nodes_scoped([dst], 1)
            or graph.export_edges_scoped([dst], 1)
            or docs.list_sources_scoped([dst], 1)
            or dst_vector_count != 0
        ):
            raise _compensate_reservation("pack registry state inconsistent after reservation")

        # step 12: bulk identity-conflict probe over every REMAPPED id,
        # before any writer is called -- the last point at which a
        # rejection can still delete the reservation instead of demoting it
        # (design §6-1: the only delete exception is this pre-writer span
        # of §5-2, steps 10-12).
        for old_id in surviving_node_ids:
            new_id = mapping[old_id]
            record = surviving_nodes_by_id[old_id]
            props = record["props"]
            reason = node_identity_conflict(
                graph, docs, vector,
                space=props.get("space"), node_type=record["labels"][0],
                node_id=new_id, pack_id=dst,
            )
            if reason:
                raise _compensate_reservation(identity_reject_message("node", new_id, reason))
        for old_id in surviving_source_ids_set:
            new_id = mapping[old_id]
            reason = source_identity_conflict(docs, vector, source_id=new_id, pack_id=dst)
            if reason:
                raise _compensate_reservation(identity_reject_message("source", new_id, reason))
        anchor_reason = node_identity_conflict(
            graph, docs, vector, space="resource", node_type="Dataset",
            node_id=dst_anchor, pack_id=dst,
        )
        if anchor_reason:
            raise _compensate_reservation(identity_reject_message("node", dst_anchor, anchor_reason))
    except _RejectedError:
        raise
    except Exception as exc:
        raise _compensate_reservation(f"pre-write check failed: {exc!r}") from exc

    # =====================================================================
    # §5-3 writes (steps 13-17) -- NOTHING past this point deletes the
    # registry row (design §6-1). Every Tier 2 failure demotes instead.
    # =====================================================================
    tier2_failure: str | None = None
    unverified_refs_total = 0
    # Declared here (before the anchor write can fail) rather than beside
    # each write-phase loop below: design §3's response schema is ONE shape
    # for both "ok" and "partial" ("status": "ok"|"partial" sharing the same
    # "copied"/"skipped"/"errors" fields), so _demote() below must be able to
    # report actual progress -- e.g. "17 of 40 nodes copied before the edge
    # write that failed" -- not just a bare error string. These need to
    # already be bound (even if still 0) the first moment _demote() could
    # possibly be called, which is right after the anchor write.
    written_nodes = 0
    written_edges = 0
    written_sources = 0
    written_vectors = 0
    imported_source_payload: list[dict[str, Any]] = []
    imported_vector_payload: list[dict[str, Any]] = []

    def _demote(reason: str) -> dict[str, Any]:
        """Demote the reservation to ``partial`` and report what the demotion
        itself could confirm about the registry row (design §12-1-2). Never
        raises -- its own reconfirmation read is wrapped too, so every
        caller (R2, R3, and R1's own compensation path is a SEPARATE
        function, ``_compensate_reservation``) gets back a well-formed dict
        no matter what the registry does underneath it.
        """
        try:
            promoted = mark_pack_partial(sql, dst, owner_id)
        except Exception as exc:
            promoted = False
            logger.warning("pack_fork: mark_pack_partial raised for pack_id=%s: %s", dst, exc)

        if promoted:
            registry_status_observed = "partial"
            registry_transition_confirmed = True
        else:
            logger.warning(
                "pack_fork: mark_pack_partial reported no row updated for pack_id=%s "
                "(row may have moved out of 'creating' concurrently)", dst,
            )
            requery_ok = True
            row: dict[str, Any] | None = None
            try:
                row = get_pack(sql, dst)
            except Exception as exc:
                requery_ok = False
                logger.warning(
                    "pack_fork: requery after mark_pack_partial failure raised for "
                    "pack_id=%s: %s", dst, exc,
                )
            if not requery_ok:
                registry_status_observed, registry_transition_confirmed = "unknown", False
            elif row is None:
                registry_status_observed, registry_transition_confirmed = "missing", False
            elif row.get("owner_id") == owner_id and row.get("forked_from") == src_pack_id:
                # design §12-11 contract 3: only OUR row's status is trusted
                # -- `get_pack` is ownership-unscoped, so without this
                # predicate a same-slug row another actor now owns (or a
                # same-owner row from a DIFFERENT fork) could be read back
                # here and reported as if it were this call's own row.
                if row.get("status") == "partial":
                    # Another actor (e.g. an operator's
                    # repair_incomplete_packs age-demotion) already made the
                    # same transition.
                    registry_status_observed, registry_transition_confirmed = "partial", True
                else:
                    observed = row.get("status")
                    registry_status_observed = observed if isinstance(observed, str) else "unknown"
                    registry_transition_confirmed = False
            else:
                # The row exists but fails the "is this ours" predicate --
                # its status is not ours to report (#143 invariant 7).
                registry_status_observed, registry_transition_confirmed = "unknown", False

        # design §12-11 contract 2: the residual-list computation below reads
        # `imported_source_payload`/`imported_vector_payload`/`mapping` --
        # already-in-scope data, not I/O -- but a corrupt entry in either
        # payload (or a `surviving_source_ids`/`sorted` regression) must not
        # blow up the whole response. Guarded separately from the registry
        # re-query above: a demotion that already committed must still come
        # back as a well-formed partial response, just with this one field
        # empty and the failure recorded instead of silently dropped.
        try:
            sources_without_vectors = sorted(
                surviving_source_ids(imported_source_payload, mapping)
                - {p["id"] for p in imported_vector_payload}
            )
            demote_source_errors = source_errors
        except Exception as exc:
            sources_without_vectors = []
            demote_source_errors = [
                *source_errors,
                f"sources_without_vectors computation failed, reported as empty: {exc!r}",
            ]

        return {
            "status": "partial",
            "pack_id": dst,
            "forked_from": src_pack_id,
            "visibility": "private",
            "error": reason,
            "copied": {
                "nodes": written_nodes,
                "edges": written_edges,
                "sources": written_sources,
                "vectors": written_vectors,
            },
            "skipped": {
                "nodes_alias_conflict": skipped_alias_nodes,
                "edges_alias_conflict": skipped_alias_edges,
                "sources_alias_conflict": skipped_alias_sources,
                "anchor_vector": skipped_anchor_vector,
                "vector_orphans": skipped_vector_orphans,
                "vector_mistagged": skipped_vector_mistagged,
                "vector_invalid": skipped_vector_invalid,
                "vector_batch_invalid": skipped_vector_batch_invalid,
                "sources_without_vectors": sources_without_vectors,
            },
            "errors": {
                "nodes": node_errors,
                "edges": edge_errors,
                "sources": demote_source_errors,
                "vectors": vector_errors,
            },
            "unverified_refs": unverified_refs_total,
            # Design §12-1-2: what the registry was OBSERVED to be at the
            # moment of this call, not a promise about what it is now -- a
            # concurrent actor can still move it after this read. "observed"
            # is deliberately not "actual" in the field name.
            "registry_status_observed": registry_status_observed,
            "registry_transition_confirmed": registry_transition_confirmed,
        }

    # =====================================================================
    # R2 -- steps 13-18: the writer span. `_RejectedError` never originates
    # here (nothing in this span raises it), but the carve-out is kept for
    # the same reason R1 keeps it: a control-flow signal must never be
    # reread as a write failure. Any OTHER exception -- builder/write_source
    # raising, a store going away mid-write -- demotes to `partial` instead
    # of leaking past this function; the already-written legs are reported
    # via `written_*`, which live outside this try (design §12-1 R2).
    # =====================================================================
    try:
        # step 13: anchor.
        anchor_receipt = builder.add_node(
            space="resource", node_type="Dataset", node_id=dst_anchor,
            properties={
                "pack_id": dst,
                "title": fork_title or "",
                "description": fork_description or "",
                "created_by": "localcrab-mcp:pack_fork",
                "forked_from": src_pack_id,
            },
            pack_id=dst, pack_anchor=True,
        )
        if not _fork_leg_ok(anchor_receipt, "anchor"):
            return _demote("anchor write did not confirm across all stores")

        # step 14: nodes.
        for old_id in surviving_node_ids:
            record = surviving_nodes_by_id[old_id]
            new_id = mapping[old_id]
            props, unverified = remap_props(
                record["props"], mapping, src_pack=src_pack_id, dst_pack=dst,
            )
            props["id"] = new_id
            node_type = record["labels"][0]
            receipt = builder.add_node(
                space=props.get("space"), node_type=node_type, node_id=new_id,
                properties=props, pack_id=dst, origin="server", fork_copy=True,
                write_vector=False,
            )
            if not _fork_leg_ok(receipt, "node"):
                tier2_failure = f"node {old_id!r} -> {new_id!r} write failed"
                break
            written_nodes += 1
            unverified_refs_total += unverified

        # step 15: edges (only if node writes fully succeeded).
        if tier2_failure is None:
            for record in surviving_edges:
                from_id = _node_id({"props": record.get("source_props") or {}})
                to_id = _node_id({"props": record.get("target_props") or {}})
                new_from = mapping[from_id]
                new_to = mapping[to_id]
                rel_props, unverified = remap_props(
                    record.get("rel_props") or {}, mapping, src_pack=src_pack_id, dst_pack=dst,
                )
                # add_edge takes (from_space, to_space), NOT node types --
                # it resolves the node type itself via internal endpoint
                # lookup on (space, id). Using source_labels/target_labels
                # here (node types) would send add_edge the wrong kind of
                # string entirely and make every edge write fail as
                # "no match", so this must mirror the same
                # source_props["space"]/target_props["space"] extraction
                # already used for the §5-1 step 6 validate_edge() call above.
                from_space = (record.get("source_props") or {}).get("space")
                to_space = (record.get("target_props") or {}).get("space")
                receipt = builder.add_edge(
                    from_space, new_from, record["relation"],
                    to_space, new_to,
                    properties=rel_props, pack_id=dst, origin="server",
                    fork_copy=True,
                )
                if not _fork_leg_ok(receipt, "edge"):
                    tier2_failure = f"edge {from_id!r}->{to_id!r} write failed"
                    break
                written_edges += 1
                unverified_refs_total += unverified

        # step 16: sources.
        if tier2_failure is None:
            for record in surviving_sources:
                old_id = _source_id(record)
                new_id = mapping[old_id]
                meta, unverified = remap_props(
                    record.get("metadata") or {}, mapping, src_pack=src_pack_id, dst_pack=dst,
                )
                text = record.get("text") or ""
                if not text:
                    # design flags this as a known mongo_store.list_sources_scoped
                    # projection gap: its rows omit `text` where the SQL doc
                    # store's do not. Fall back to a direct fetch before treating
                    # this source as empty.
                    fetched = docs.get_source(old_id) if hasattr(docs, "get_source") else None
                    if isinstance(fetched, dict):
                        text = fetched.get("text") or ""
                receipt = write_source(
                    sql, hybrid, docs, vector,
                    text=text, source_id=new_id, metadata=meta, pack_id=dst,
                    origin="server", fork_copy=True, write_vector=False,
                )
                if not _fork_leg_ok(receipt, "source"):
                    tier2_failure = f"source {old_id!r} -> {new_id!r} write failed"
                    break
                written_sources += 1
                imported_source_payload.append({"source_id": old_id})
                unverified_refs_total += unverified

        # step 17: vectors -- one raw import_vectors batch, no re-embedding.
        if tier2_failure is None and import_batch:
            records = []
            vector_meta_unverified = 0
            for rec in import_batch:
                new_id = mapping[rec["id"]]
                meta, unverified = remap_vector_metadata(
                    rec.get("metadata") or {}, mapping,
                    src_pack=src_pack_id, dst_pack=dst, owner_id=owner_id,
                )
                vector_meta_unverified += unverified
                new_rec = dict(rec)
                new_rec["id"] = new_id
                new_rec["metadata"] = meta
                records.append(new_rec)
            try:
                landed_ids = vector.import_vectors(records, pack_id=dst)
            except Exception as exc:
                tier2_failure = f"vector import failed: {exc}"
            else:
                landed_set = set(landed_ids or [])
                missing = [r["id"] for r in records if r["id"] not in landed_set]
                if missing:
                    # H5 (§4-A predicate 3, checked here rather than by re-read):
                    # import_vectors is documented as non-atomic; a partial
                    # batch is a Tier 2 failure, not a Tier 1 loss (design
                    # §6-2, §6-1: this module does NOT attempt a compensating
                    # delete of what did land).
                    tier2_failure = f"vector import landed only {len(landed_set)}/{len(records)} records"
                else:
                    written_vectors = len(records)
                    imported_vector_payload = [{"id": r["id"]} for r in records]
                    # Only counted once the whole batch actually landed --
                    # matches the node/edge/source loops above, which likewise
                    # fold a record's own `unverified` count into the total
                    # only past its own `_fork_leg_ok` check. A batch that
                    # Tier-2-fails here is reported via `_demote()` instead,
                    # whose `unverified_refs_total` correctly excludes it: an
                    # unwritten record's residual-reference count would
                    # misdescribe content that never made it into `copied`.
                    unverified_refs_total += vector_meta_unverified

        # step 18: H4 post-write verification (design §5-4 step 18, §4-A
        # predicates 1-2) -- re-read the copy just written and confirm no
        # source-space id or literal src_pack string survived in a
        # REFERENCE_KEYS/pack_id position. A hit here is OUR write attempt
        # failing to do what it claimed, not an original-data defect -- Tier 2.
        if tier2_failure is None:
            leaked = _h4_verify(
                graph, docs, vector,
                dst=dst, mapping=mapping,
                written_nodes=written_nodes, written_edges=written_edges,
                written_sources=written_sources, written_vectors=written_vectors,
                src_pack_id=src_pack_id,
            )
            if leaked:
                tier2_failure = f"H4 post-write verification found leaked references: {leaked[:5]}"

        if tier2_failure is not None:
            return _demote(tier2_failure)
    except _RejectedError:
        raise
    except Exception as exc:
        return _demote(f"write phase raised: {exc!r}")

    # =====================================================================
    # R3 -- steps 18b-20: the verdict span. `mark_pack_ready` returning
    # False or raising does NOT immediately demote -- it commits inside its
    # own transaction (ownership.py's `with sql._engine.begin()`), so either
    # signal can mean "committed, only the response was lost" just as easily
    # as "never committed". A fresh registry read decides which one actually
    # happened, and that read is itself wrapped: a raise here means the SAME
    # broken `sql` handle that just made `mark_pack_ready` raise, so it is
    # folded into "could not confirm" rather than left to escape (design
    # §12-1 R3). `_demote` never raises, so every return out of this except
    # clause is well-formed.
    # =====================================================================
    try:
        # step 18b: residual sources-without-vectors report (fork_remap's own
        # helper, comparing the copied-source id set against the imported-vector
        # id set -- both already expressed in dst-space ids).
        sources_survivor_ids = surviving_source_ids(imported_source_payload, mapping)
        vectors_survivor_ids = {p["id"] for p in imported_vector_payload}
        sources_without_vectors = sorted(sources_survivor_ids - vectors_survivor_ids)

        # step 19: promote to ready.
        try:
            finalized = mark_pack_ready(sql, dst, owner_id)
        except Exception as exc:
            finalized = False
            logger.warning("pack_fork: mark_pack_ready raised for pack_id=%s: %s", dst, exc)

        if not finalized:
            logger.warning(
                "pack_fork: mark_pack_ready reported no row updated for pack_id=%s "
                "(row may have moved out of 'creating' concurrently)", dst,
            )
            requery_ok = True
            requery: dict[str, Any] | None = None
            try:
                requery = get_pack(sql, dst)
            except Exception as exc:
                requery_ok = False
                logger.warning(
                    "pack_fork: requery after mark_pack_ready failure raised for "
                    "pack_id=%s: %s", dst, exc,
                )
            # design §12-11 contract 3: `get_pack` is ownership-unscoped, so
            # a re-read landing on `status == "ready"` is only trustworthy
            # when the row is confirmed OURS (same owner, same
            # `forked_from`) -- otherwise a same-slug row another actor now
            # owns, or a same-owner row from a DIFFERENT fork, could be
            # misread as this call's own transition landing.
            if not (
                requery_ok
                and requery is not None
                and requery.get("owner_id") == owner_id
                and requery.get("forked_from") == src_pack_id
                and requery.get("status") == "ready"
            ):
                return _demote("could not promote to ready after a fully successful write phase")
            # else: the write DID commit and only the response was lost --
            # fall through to the normal "ok" response below.

        return {
            "status": "ok",
            "pack_id": dst,
            "forked_from": src_pack_id,
            "visibility": "private",
            "copied": {
                "nodes": written_nodes,
                "edges": written_edges,
                "sources": written_sources,
                "vectors": written_vectors,
            },
            "skipped": {
                "nodes_alias_conflict": skipped_alias_nodes,
                "edges_alias_conflict": skipped_alias_edges,
                "sources_alias_conflict": skipped_alias_sources,
                "anchor_vector": skipped_anchor_vector,
                "vector_orphans": skipped_vector_orphans,
                "vector_mistagged": skipped_vector_mistagged,
                "vector_invalid": skipped_vector_invalid,
                "vector_batch_invalid": skipped_vector_batch_invalid,
                "sources_without_vectors": sources_without_vectors,
            },
            "errors": {
                "nodes": node_errors,
                "edges": edge_errors,
                "sources": source_errors,
                "vectors": vector_errors,
            },
            "unverified_refs": unverified_refs_total,
            "registry_status_observed": "ready",
            "registry_transition_confirmed": True,
        }
    except _RejectedError:
        raise
    except Exception as exc:
        return _demote(f"verdict phase raised: {exc!r}")


def _vector_record_invalid(record: dict[str, Any], *, pack_id: str, allow_uris: bool) -> str | None:
    """Per-record validity pre-check, run BEFORE any record is ever handed
    to ``import_vectors`` -- design §5-1 step 6b's 2-pass batch-decomposition
    algorithm exists so a single already-broken exported record (Tier 1: it
    was broken before this call ever touched it) cannot raise inside
    ``import_vectors`` and abort the WHOLE batch, which would turn every
    other, perfectly good record in that batch into a Tier 2 failure it does
    not deserve.

    A THIN WRAPPER (design §12-2) over the real per-record validator,
    ``opencrab.stores._vector_base.validate_import_records`` -- not a
    hand-written mirror of its rules. A hand mirror re-diverges from the
    real validator every time the real one gains a check (this module's
    previous mirror missed unknown metadata keys and let
    ``struct.pack("f", 1e40)``'s silent saturation to ``inf`` through, both
    of which the real validator catches). ``pack_id=pack_id`` -- the caller
    passes ``src_pack_id``, since at this point in preflight every surviving
    record's metadata ``pack_id`` is still absent-or-source (anything else
    was already dropped as ``vector_mistagged`` upstream of this call).
    ``allow_uris`` mirrors what THIS backend's own ``import_vectors`` will
    actually pass at step 17 (chroma: True; everything else: False) -- see
    the caller.

    The four exceptions caught below are exactly the ones a single record's
    OWN shape can trigger in the real validator: a shape violation is
    reported as ``ValueError``; ``sorted(unknown)`` over mixed-type keys
    raises ``TypeError``; ``len(embedding)`` over a huge ``Sequence`` (e.g.
    ``range(sys.maxsize + 1)``) can raise ``OverflowError``; formatting a
    deeply-nested value into an error message can raise ``RecursionError``.
    All four mean "this record's data is malformed" -- Tier 1.

    Anything ELSE (``MemoryError``, a ``RuntimeError`` from a validator
    regression, ...) is NOT a data defect -- it is the environment or the
    validator itself breaking -- so it is not folded into a per-record Tier
    1 skip. It escalates to a pre-reservation whole-fork rejection instead
    (raised as ``_RejectedError``, caught by ``fork_pack``): the
    completeness floor exists to absorb systematic original-data loss, not
    to quietly swallow a sporadic internal failure one record at a time.

    TWO checks the real validator also performs are deliberately not run
    per-record here, because neither is decidable from a single record:
    duplicate ``id`` WITHIN the batch, and dimensional uniformity ACROSS the
    batch. These are handled by a SEPARATE, explicit 2-pass step (design
    §5-1-6b) that runs immediately after this function's caller filters
    every candidate record through it: pass 1 walks the already-filtered
    batch in ``export_pack_vectors`` order, keeping the first occurrence of
    a duplicate id and dropping the rest, and establishing the reference
    dimension from the first surviving record (dropping any later record
    whose dimension disagrees); pass 2 re-runs the real validator over the
    survivors to confirm the decomposition actually worked. Both pass-1
    drops are Tier 1, counted under ``skipped.vector_batch_invalid`` -- NOT
    deferred to ``import_vectors`` at step 17 the way they used to be, which
    is what let a single duplicate/mismatched record raise inside the real
    batch import and get misclassified Tier 2 by the generic
    ``except Exception`` there, demoting an otherwise-successful,
    already-reserved fork to ``partial`` over an original-data defect that
    was never our own write failing.
    """
    try:
        validate_import_records([record], pack_id=pack_id, allow_uris=allow_uris)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        return str(exc)
    except Exception as exc:
        raise _reject(f"internal error: vector record validation raised {exc!r}") from exc
    return None
