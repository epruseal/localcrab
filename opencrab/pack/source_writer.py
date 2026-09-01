"""The source-text writer: the second of the two write chokepoints (#148, #74).

``OntologyBuilder`` covers graph node/edge writes. Source text used to bypass
it entirely -- ``HybridQuery.ingest`` writes only the vector (its own comment
says so: "Doc-store mutations happen outside ingest() today"), and the
``doc_sources`` row was a separate ``docs.upsert_source`` call at every site
that ingests text: REST ``/api/ingest``, the CLI, ``pack_ingest``'s
``text_as_node=False`` path, and ``pack_fork``.

Four sites, two calls each, and the ownership stamp landing on only one of
them is how the free-tier quota came to depend on a key (``metadata.user_id``)
that no server-side code was responsible for setting. #148 made the pair one
call with one stamp.

#74 added the third leg. Text ingested here was reaching the vector and the
doc store but never the graph, so it was invisible to every graph-based
feature -- ``ontology_get_node`` answered ``found: false`` for a source the
same server had just confirmed it stored. Only ``pack_ingest``'s
``text_as_node=True`` branch materialised the ``evidence/TextUnit`` node, so
the same data was stored differently depending on which surface wrote it.
This module now writes that node for every source, which is what makes the
four sites agree.

Order matters, and it changed with #74. The graph leg runs FIRST and is
REQUIRED: when the node does not land, neither the doc row nor the vector is
written. That is ``OntologyBuilder.add_node``'s own rule ("a doc/vector row
with no graph node is invisible to every pack-scoped read"), and writing the
optional stores anyway is precisely the half-stored state #74 reports. It also
has to lead because the builder's ownership guard raises: behind the doc
write, a rejected node would leave a committed row behind a 422.

Between the two optional legs the doc row still leads. An early #148 draft
wrote the vector first and skipped the doc row when the vector failed, which
silently turned ingest into a no-op on any deployment without a vector store
-- ``ingest()`` returns ``{"chromadb": "unavailable"}`` there rather than
raising.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from opencrab.common.graph_identity import (
    GraphSchemaMigrationRequired,
    GraphWriteUnavailable,
)
from opencrab.common.pack_tags import canonicalize_pack_alias
from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET
from opencrab.pack.write_gate import (
    SOURCE_STAMPED,
    authorize,
    authorize_fork_copy,
    identity_reject_message,
    source_identity_conflict,
    stamp,
)

# Sources carry no space of their own, and the FTS space filter drops anything
# without one. Only the pack ingest path used to fill this in, so REST-ingested
# text was invisible to a space-filtered query (#52/#110). Filled here so every
# source path gets it.
_DEFAULT_SPACE = "evidence"

# The graph shape a source is materialised as (#74). Pinned rather than derived
# from the caller's ``metadata["space"]``: ``TextUnit`` exists only in the
# ``evidence`` space in the grammar manifest, and ``pack_ingest``'s
# ``text_as_node=True`` branch already hardcodes exactly this pair. The vector
# and doc rows keep whatever space the caller asked for -- that is the source's
# own space, and it is a different axis from the node's.
_SOURCE_NODE_SPACE = "evidence"
_SOURCE_NODE_TYPE = "TextUnit"

# Graph statuses this module assigns itself, i.e. not copied from a builder
# receipt. ``store_write_succeeded`` recognises neither as success, which is
# what both of them mean.
_GRAPH_SKIPPED_FORK = "skipped (raw copy)"
_GRAPH_SKIPPED_LONG_ID = "skipped (id exceeds the node id limit after fork remap)"
_SKIPPED_GRAPH_FAILED = "skipped (graph write failed)"


def _docs_available(docs: Any) -> bool:
    return bool(getattr(docs, "available", False))


def _existing_node_rows(graph: Any, source_id: str) -> list[dict[str, Any]] | None:
    """Every graph row for ``source_id``, or ``None`` when that cannot be told.

    ``None`` is "cannot tell", NOT "no node" -- the distinction is the whole
    point. ``get_nodes_by_id`` calls ``_require_available()`` and returns ``[]``
    only on a genuine no-match, so an unavailable store raises rather than
    answering. ``lookup_node_type`` and ``get_node_digest`` are both unusable
    here for the opposite reason: the first returns ``None`` on an unavailable
    store AND swallows transient query errors, the second returns ``None`` for a
    row whose digest cannot be computed. Both collapse "absent" and "cannot say"
    into one value, and treating "cannot say" as "absent" is how the carve-out
    below would silently start writing graph-less sources again.

    The ``isinstance`` check mirrors ``write_gate._check_by_id_axis``, which
    guards the same method the same way: a double returning ``None``/``{}``/
    ``()`` must not read as an empty result.
    """
    if graph is None or not getattr(graph, "available", False):
        return None
    method = getattr(graph, "get_nodes_by_id", None)
    if method is None:
        return None
    try:
        rows = method(source_id)
    except Exception:  # noqa: BLE001 -- any failure means "cannot tell"
        return None
    return rows if isinstance(rows, list) else None


def _node_digest(graph: Any, source_id: str) -> str | None:
    """CAS token for an existing ``TextUnit`` row, or ``None``.

    ``None`` sends ``add_node`` down its plain-insert path, where the store's
    own digest comparison rejects a mismatch. That is the correct disposition
    for every ``None`` case here: no row, a row of another type, a backend
    without the method, or a read that failed.
    """
    getter = getattr(graph, "get_node_digest", None)
    if getter is None:
        return None
    try:
        return getter(source_id, node_type=_SOURCE_NODE_TYPE) or None
    except Exception:  # noqa: BLE001 -- see docstring
        return None


def write_source(
    sql: Any,
    hybrid: Any,
    docs: Any,
    vector: Any,
    *,
    graph: Any,
    text: str,
    source_id: str,
    metadata: Mapping[str, Any] | None = None,
    pack_id: str,
    origin: Literal["client", "server"] = "client",
    fork_copy: bool = False,
    write_vector: bool = True,
    write_graph: bool = True,
) -> dict[str, Any]:
    """Write one source's graph node, doc row and vector under one ownership stamp.

    Returns a receipt shaped like the builder's: ``{"source_id", "metadata",
    "stores": {"documents": ..., "chromadb": ...}}``. *Store* failures are
    reported in ``stores`` rather than raised, matching the #158 contract that
    callers read the receipt.

    Rejections are different and DO raise: a caller who is not the pack owner,
    a source_id already attributed elsewhere, or a payload that violates the
    ownership-tag invariant. Those happen before anything is written, so the
    caller gets an exception and no partial row -- which is why the alias check
    below runs here rather than being left to ``HybridQuery.ingest``. That
    method raises it too, but it runs AFTER the doc row is committed, and an
    earlier version of this function let exactly that through: a 422 to the
    client with the doc row already persisted.

    ``pack_id`` is keyword-REQUIRED. A source with no pack is outside every
    pack-scoped read (#143 invariant 5), so "no pack" must not be expressible.

    ``origin="server"`` (design v7 §4-C-1) is for the same reason ``add_node``
    has it: a copied source carries the ORIGINAL owner's ``user_id`` and the
    SOURCE pack's ``pack_id`` in its metadata, both of which differ from the
    forker's own values -- ``origin="client"`` (the default) would reject
    that as forged identity. ``pack_fork`` is the only intended caller.

    ``fork_copy`` (design v7 §4-C-2) routes authorization through
    ``write_gate.authorize_fork_copy`` instead of ``authorize``, widening the
    allowed status to a pack's own fork-reserved ``creating`` window -- see
    that function's docstring. Everything else, including the identity guard
    and the stamp, is unchanged.

    ``write_vector`` (design v7 §4-C-3), default True, skips the vector leg
    entirely when False and records ``"skipped (raw copy)"`` under
    ``stores["chromadb"]`` -- NOT ``stores["vector"]``; that key differs from
    ``OntologyBuilder.add_node``'s because this function's receipt already
    used ``chromadb`` for the vector leg before this parameter existed. Fork
    sets this False because ``hybrid.ingest`` re-embeds, which issue #200
    forbids for a fork; the original vector is imported raw elsewhere instead.

    A doc-store *error* stops the vector write: a vector row pointing at a
    source that failed to record is an orphan no read path can hydrate. A doc
    store that is merely *unavailable* does not -- that is a deployment shape,
    not a failure, and refusing there would break vector-only deployments the
    same way the earlier vector-first ordering broke doc-only ones.
    """
    principal = _principal()

    # Owner-only (#143 invariant 4). `sql` is a REQUIRED positional, not an
    # optional one: an authorize that runs "when the caller happens to pass a
    # store" is fail-open, which is the pattern this work already had to walk
    # back once (see pack/load.py's _require_bound_principal).
    if fork_copy:
        authorize_fork_copy(sql, principal, pack_id)
    else:
        authorize(sql, principal, pack_id)

    # A source_id is a global slot on both sinks -- the doc store upserts on
    # source_id with no pack predicate, and every vector store keys on the id
    # alone -- so writing someone else's id silently takes their row.
    reason = source_identity_conflict(
        docs, vector, source_id=source_id, pack_id=pack_id
    )
    if reason:
        raise ValueError(identity_reject_message("source", source_id, reason))

    meta = stamp(
        metadata, principal=principal, pack_id=pack_id, keys=SOURCE_STAMPED,
        origin=origin,
    )
    meta.setdefault("space", _DEFAULT_SPACE)

    # #171 ownership-tag invariant, checked BEFORE the first write. `pack` is
    # not a reserved boundary key, so a client can still send it; leaving the
    # check to the vector leg means the doc row is already committed when it
    # fires.
    canonicalize_pack_alias(meta)

    receipt: dict[str, Any] = {"source_id": source_id, "metadata": meta, "stores": {}}

    # --- graph leg (#74), FIRST ---------------------------------------------
    # The graph is the system of record for a node, exactly as
    # `OntologyBuilder.add_node` already states for its own writes: a doc/vector
    # row with no graph node is invisible to every graph-based read, which is
    # the defect this leg closes. So it leads, and when it does not land the
    # doc and vector legs do not run at all.
    #
    # It also has to run before the doc row for a second reason: the builder's
    # ownership guard RAISES. Behind the doc write, a rejected node would leave
    # a committed doc row behind a 422 -- the exact shape this module's
    # docstring already had to walk back once for the alias check.
    if not _graph_leg(
        receipt, graph, docs, sql, vector,
        text=text, source_id=source_id, meta=meta, pack_id=pack_id,
        origin=origin, fork_copy=fork_copy, write_graph=write_graph,
    ):
        receipt["stores"]["documents"] = _SKIPPED_GRAPH_FAILED
        receipt["stores"]["chromadb"] = _SKIPPED_GRAPH_FAILED
        return receipt

    doc_failed = False
    if _docs_available(docs):
        try:
            created = docs.upsert_source(source_id, text, meta)
            receipt["stores"]["documents"] = f"ok (id={created or source_id})"
        except Exception as exc:  # noqa: BLE001 -- reported, not raised (#158)
            receipt["stores"]["documents"] = f"error: {exc}"
            doc_failed = True
    else:
        receipt["stores"]["documents"] = "unavailable"

    if doc_failed:
        receipt["stores"]["chromadb"] = "skipped (source record failed)"
        return receipt

    if not write_vector:
        # design v7 §4-C-3: fork imports the original vector raw elsewhere;
        # re-embedding it here would violate #200. Recorded explicitly so
        # `_fork_leg_ok` (opencrab/pack/fork.py) can require exactly this
        # value rather than treating an untouched "unavailable" as
        # equivalent.
        receipt["stores"]["chromadb"] = "skipped (raw copy)"
        return receipt

    # `ingest` mutates the mapping it is handed (it sets `source_id` on it), so
    # the doc row above and the vector below deliberately share one dict: the
    # two must not drift apart.
    try:
        vector_result = hybrid.ingest(text=text, source_id=source_id, metadata=meta)
    except Exception as exc:  # noqa: BLE001 -- store failure, reported (#158)
        receipt["stores"]["chromadb"] = f"error: {exc}"
        return receipt
    receipt["stores"].update(vector_result.get("stores") or {})
    if "vector_id" in vector_result:
        receipt["vector_id"] = vector_result["vector_id"]
    return receipt


def _graph_leg(
    receipt: dict[str, Any],
    graph: Any,
    docs: Any,
    sql: Any,
    vector: Any,
    *,
    text: str,
    source_id: str,
    meta: dict[str, Any],
    pack_id: str,
    origin: str,
    fork_copy: bool,
    write_graph: bool,
) -> bool:
    """Materialise the source as an ``evidence/TextUnit`` node.

    Returns True when the doc and vector legs may proceed. That is NOT the same
    as "a node was written": the two opt-outs below are deliberate skips, not
    failures, and both let the source through.

    Rejections raise, matching this module's existing contract and running
    before any store is touched:

    - ``ValueError`` (grammar, schema, ownership-tag, foreign identity)
    - ``NodeIdentityConflict`` (the id is a different logical node, or a CAS
      raced) -- REST maps it to 422 exactly as ``/api/nodes`` already does
    - the authorization exceptions, which are NEVER absorbed: the builder
      re-runs ``authorize``, and a registry that went down between the two
      checks raises a bare ``RuntimeError`` from ``write_gate.authorize``.

    Only ``GraphSchemaMigrationRequired`` and ``GraphWriteUnavailable`` are
    turned into a receipt status. Catching by a wider class (``RuntimeError``,
    ``Exception``) would swallow that registry failure and the identity
    conflict along with it. The narrow pair is safe because neither is an
    ancestor of ``PackNotFoundError`` (LookupError), ``PackForbiddenError``
    (PermissionError), ``ValueError`` or ``NodeIdentityConflict`` -- it is the
    inheritance graph that makes the boundary hold, not a claim that the set of
    exceptions escaping ``add_node`` is finite (it is not).
    """
    from opencrab.ontology.builder import OntologyBuilder, store_write_succeeded

    if not write_graph:
        # `pack_fork` copies nodes itself, before the sources (its step 14 vs
        # step 16), and with the ORIGINAL remapped properties. Writing the node
        # again from here would overwrite that copy with a freshly built one.
        # Recorded rather than silently omitted, mirroring `add_node`'s own
        # `write_vector=False` wording.
        receipt["stores"]["graph"] = _GRAPH_SKIPPED_FORK
        return True

    rows = _existing_node_rows(graph, source_id)

    if len(source_id) > SOURCE_NODE_ID_BUDGET and rows == []:
        # An id past the budget cannot survive as a node id through a fork
        # remap, and tests/test_pack_fork.py's T77 pins that a source-only id
        # of that length must NOT make a pack unforkable. So do not CREATE a
        # node for it.
        #
        # The `rows == []` half is not decoration. The carve-out is "do not
        # create", not "do not look": a node for a long id can already exist
        # (pack_ingest's text_as_node=True has no length check, and a fork of a
        # budget-length source produces one). Skipping the update there would
        # leave the graph on the old text while the doc row moved to the new
        # one -- the divergence this whole leg exists to prevent. `_existing_
        # node_rows` returns None for "cannot tell", and `None == []` is False,
        # so every uncertain answer falls through to the normal path and fails
        # closed there.
        receipt["stores"]["graph"] = _GRAPH_SKIPPED_LONG_ID
        return True

    node_props: dict[str, Any] = {"pack_id": pack_id, "text": text}
    for key in ("title", "source"):
        if meta.get(key):
            node_props[key] = meta[key]

    # Properties are REPLACED, not merged (design v13 §4.2). The node is a
    # projection of the source, so it mirrors the latest source write: a
    # re-ingest whose metadata has no `title` leaves a node with no `title`.
    # Merging was tried and withdrawn -- it needs a second read for the base,
    # and a base read separated from the CAS token read is a lost update.
    builder = OntologyBuilder(graph, docs, sql, vec=vector)
    try:
        node_receipt = builder.add_node(
            space=_SOURCE_NODE_SPACE,
            node_type=_SOURCE_NODE_TYPE,
            node_id=source_id,
            properties=node_props,
            pack_id=pack_id,
            origin=origin,
            fork_copy=fork_copy,
            # The vector for this id is written once, by `hybrid.ingest` below,
            # with the SOURCE's metadata (user_id/source_id/space). The
            # builder's own vector leg would overwrite that same id with the
            # node summary text and node-shaped metadata. `pack_ingest`'s
            # text_as_node=True branch makes the mirror-image call for the same
            # reason.
            write_vector=False,
            _expected_current_digest=_node_digest(graph, source_id) if rows else None,
        )
    except (GraphSchemaMigrationRequired, GraphWriteUnavailable) as exc:
        # Operational failure of the system of record -- reported, not raised,
        # so the #158 "callers read the receipt" contract keeps holding. A
        # store that is merely legacy/partial or has lost its write capability
        # is a deployment state, not a client error.
        receipt["stores"]["graph"] = f"error: {exc}"
        return False

    node_stores = node_receipt.get("stores") or {}
    for key, status in node_stores.items():
        # The builder's vector leg is off, and its key would collide with
        # nothing here anyway -- but reporting "skipped (raw copy)" under a
        # name this receipt does not otherwise use would just be noise. Every
        # other key is meaningful: `graph`, `docs` (the doc_nodes row, NOT this
        # module's `documents`, which is the doc_sources row) and `sql`.
        if key != "vector":
            receipt["stores"][key] = status
    return store_write_succeeded(node_stores, "graph")


def _principal() -> Any:
    from opencrab.auth import current_principal

    return current_principal()
