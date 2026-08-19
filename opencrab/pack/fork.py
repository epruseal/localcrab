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
                                     slug; the ONLY delete this module ever
                                     performs (a pre-anchor identity-conflict
                                     rejection) can still happen here.
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
    {...}, "errors": {...}, "unverified_refs": ...}`` on completion (design
    §3's schema names both statuses under this one shape -- "partial" adds
    an "error" key and reports actual progress so far, it does not shrink
    to a bare error string), or ``{"error": ..., "hint"?: ...}`` on a
    preflight/reservation rejection that left nothing behind.
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
    for record in nodes:
        node_id = _node_id(record)
        if node_id is None:
            node_errors.append("node missing props['id']; skipped (Tier 1)")
            continue
        if node_id == src_anchor:
            # The source pack's OWN anchor is never copied as an ordinary
            # node -- the new pack gets its own anchor at step 13. Counted
            # explicitly (not assumed to appear exactly once) so the
            # completeness-floor denominator below stays exact regardless
            # of whether export_nodes_scoped happens to include it.
            src_anchor_seen += 1
            continue
        props = record["props"]
        labels = record.get("labels") or []
        node_type = labels[0] if labels else None
        space = props.get("space")
        if not node_type or not isinstance(space, str):
            node_errors.append(f"node {node_id!r} missing space/type; skipped (Tier 1)")
            continue
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
    for record in sources:
        source_id = _source_id(record)
        if source_id is None:
            source_errors.append("source missing source_id; skipped (Tier 1)")
            continue
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
    for rec in exported_vectors:
        rec_id = rec.get("id")
        if rec_id == src_anchor:
            skipped_anchor_vector += 1
            continue
        if not isinstance(rec_id, str) or rec_id not in mapping:
            skipped_vector_orphans += 1
            continue
        meta = rec.get("metadata") or {}
        declared = meta.get("pack_id") if isinstance(meta, dict) else None
        if declared is not None and declared != src_pack_id:
            skipped_vector_mistagged += 1
            continue
        problem = _vector_record_invalid(rec)
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

    dst_anchor = anchor_node_id(dst)
    # step 10: fix up the mapping's anchor entry now that dst is known.
    mapping[src_anchor] = dst_anchor

    def _delete_reservation() -> None:
        try:
            delete_pack_row(sql, dst, owner_id, only_status=("creating",))
        except Exception:
            logger.warning("pack_fork: could not delete reserved pack_id=%s after a "
                            "pre-write rejection", dst)

    # step 11: dst must be genuinely empty (defense in depth -- a freshly
    # negotiated unique slug should never already have content). Design §5-2
    # step 11 names all FOUR axes explicitly ("노드·엣지·소스·벡터가 전부
    # 비어 있을 것") -- the vector check uses the same pack-scoped counter
    # as §5-1 step 4 (cap=1 is enough: any count other than exactly 0, or an
    # inability to count at all, is disqualifying here).
    dst_vector_count = _count_pack_vectors(vector, dst, 1)
    if (
        graph.export_nodes_scoped([dst], 1)
        or graph.export_edges_scoped([dst], 1)
        or docs.list_sources_scoped([dst], 1)
        or dst_vector_count != 0
    ):
        _delete_reservation()
        raise _reject("pack registry state inconsistent after reservation")

    # step 12: bulk identity-conflict probe over every REMAPPED id, before
    # any writer is called -- the last point at which a rejection can still
    # delete the reservation instead of demoting it (design §6-1: the only
    # delete exception is this pre-writer span of §5-2).
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
            _delete_reservation()
            raise _reject(identity_reject_message("node", new_id, reason))
    for old_id in surviving_source_ids_set:
        new_id = mapping[old_id]
        reason = source_identity_conflict(docs, vector, source_id=new_id, pack_id=dst)
        if reason:
            _delete_reservation()
            raise _reject(identity_reject_message("source", new_id, reason))
    anchor_reason = node_identity_conflict(
        graph, docs, vector, space="resource", node_type="Dataset",
        node_id=dst_anchor, pack_id=dst,
    )
    if anchor_reason:
        _delete_reservation()
        raise _reject(identity_reject_message("node", dst_anchor, anchor_reason))

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
        try:
            promoted = mark_pack_partial(sql, dst, owner_id)
        except Exception as exc:
            promoted = False
            logger.warning("pack_fork: mark_pack_partial raised for pack_id=%s: %s", dst, exc)
        if not promoted:
            logger.warning(
                "pack_fork: mark_pack_partial reported no row updated for pack_id=%s "
                "(row may have moved out of 'creating' concurrently)", dst,
            )
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
                "sources_without_vectors": sorted(
                    surviving_source_ids(imported_source_payload, mapping)
                    - {p["id"] for p in imported_vector_payload}
                ),
            },
            "errors": {
                "nodes": node_errors,
                "edges": edge_errors,
                "sources": source_errors,
                "vectors": vector_errors,
            },
            "unverified_refs": unverified_refs_total,
        }

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

    # =====================================================================
    # §5-4 verdict (steps 18-20)
    # =====================================================================
    # step 18b: residual sources-without-vectors report (fork_remap's own
    # helper, comparing the copied-source id set against the imported-vector
    # id set -- both already expressed in dst-space ids).
    sources_survivor_ids = surviving_source_ids(imported_source_payload, mapping)
    vectors_survivor_ids = {p["id"] for p in imported_vector_payload}
    sources_without_vectors = sorted(sources_survivor_ids - vectors_survivor_ids)

    finalized = mark_pack_ready(sql, dst, owner_id)
    if not finalized:
        logger.warning(
            "pack_fork: mark_pack_ready reported no row updated for pack_id=%s "
            "(row may have moved out of 'creating' concurrently)", dst,
        )
        return _demote("could not promote to ready after a fully successful write phase")

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
    }


def _vector_record_invalid(record: dict[str, Any]) -> str | None:
    """This module's own mirror of a subset of
    ``opencrab.stores._vector_base.validate_import_records``'s per-record
    checks, run BEFORE any record is ever handed to ``import_vectors`` --
    design §5-1 step 6b's 2-pass batch-decomposition algorithm exists so a
    single already-broken exported record (Tier 1: it was broken before
    this call ever touched it) cannot raise inside ``import_vectors`` and
    abort the WHOLE batch, which would turn every other, perfectly good
    record in that batch into a Tier 2 failure it does not deserve.

    Only the checks that are meaningful for ONE record in isolation are
    reproduced here (id shape, embedding shape/finiteness, document type,
    metadata type/key shape); nothing here allows a record through that
    ``import_vectors`` would reject as a hard error for its OWN independent
    per-record reasons.

    TWO checks ``validate_import_records`` also performs are deliberately
    ABSENT from this function, because neither is decidable from a single
    record: duplicate ``id`` WITHIN the batch, and dimensional uniformity
    ACROSS the batch. These are not skipped -- they are handled by a
    SEPARATE, explicit 2-pass step (design §5-1-6b) that this function's
    caller runs immediately after filtering every candidate record through
    this function: pass 1 walks the already-per-record-filtered batch in
    ``export_pack_vectors`` order, keeping the first occurrence of a
    duplicate id and dropping the rest, and establishing the reference
    dimension from the first surviving record (dropping any later record
    whose dimension disagrees); pass 2 re-runs the REAL
    ``validate_import_records`` over the survivors to confirm the
    decomposition actually worked. Both pass-1 drops are Tier 1, counted
    under ``skipped.vector_batch_invalid`` -- NOT deferred to
    ``import_vectors`` at step 17 the way they used to be, which is what
    let a single duplicate/mismatched record raise inside the real batch
    import and get misclassified Tier 2 by the generic `except Exception`
    there, demoting an otherwise-successful, already-reserved fork to
    `partial` over an original-data defect that was never our own write
    failing.
    """
    if "id" not in record or not isinstance(record.get("id"), str) or not record["id"]:
        return "missing/invalid id"
    embedding = record.get("embedding")
    if not isinstance(embedding, (list, tuple)) or not embedding:
        return "missing/empty embedding"
    for component in embedding:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            return "embedding contains a non-numeric component"
        try:
            import struct

            struct.pack("f", float(component))
        except (OverflowError, ValueError):
            return "embedding contains a non-float32-representable component"
    document = record.get("document")
    if document is not None and not isinstance(document, str):
        return "document must be str or None"
    metadata = record.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return "metadata must be a dict"
    if isinstance(metadata, dict) and any(not isinstance(k, str) for k in metadata):
        return "metadata has a non-str key"
    return None
