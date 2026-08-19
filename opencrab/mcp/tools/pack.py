"""Pack management tools: list/create/ingest content packs.

See graph.py's module docstring for why ``_get_context`` (and, here,
``content_pack_list`` too) is imported at function scope inside handler
bodies rather than at module level: ``patch("opencrab.mcp.tools.<name>")``
patches the attribute on the ``opencrab.mcp.tools`` package object, and only
a late import resolved at call time observes that patched value. This
matters in particular for ``pack_create``/``pack_ingest``'s internal calls to
``content_pack_list`` — even though all three live in this same module, a
bare same-module reference would resolve to this module's own (unpatchable)
global, not the package attribute the tests patch.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from opencrab.common.pack_tags import apply_pack_tag
from opencrab.common.text import slugify
from opencrab.pack.write_gate import (
    edge_identity_conflict,
    identity_reject_message,
    node_identity_conflict,
    source_identity_conflict,
)

from ._registry import AccessTier, tool

logger = logging.getLogger(__name__)


def _pack_error(ctx: dict[str, Any], slug: str, subject_id: str, message: str,
                *, disclose_status: bool = True) -> dict[str, Any]:
    """A ``pack_create`` failure response that still names the pack (#170).

    Several failure branches deliberately KEEP the registry row (design v4
    §3.0 -- past the first content write, a pack that cannot be finalised is
    demoted, never deleted). The caller needs the id of what was kept, and it
    cannot derive it: ``create_pack``'s slug negotiation may have appended a
    random suffix, so the assigned id can differ from the one requested. That
    is why the success path already returns ``pack_id`` -- a failure that
    leaves something behind owes the same.

    ``registry_status`` is READ BACK rather than assumed. These branches have
    just tried a transition that may not have applied (a demotion whose UPDATE
    matched nothing, a re-registration that lost a PK race), and reporting the
    intended status instead of the actual one would hand the caller a claim
    this function never checked.

    The status is disclosed ONLY for a row this caller owns, and ownership is
    checked against what the read back just returned -- not against what the
    branch believed a moment earlier. Every branch that lands here is a
    concurrent-actor branch, so the row occupying this slug at read time can
    be someone else's: the caller's own row went missing and another subject
    claimed the slug, say. Reporting that row's status would disclose a
    stranger's pack, and a ``creating`` one is invisible to every read path,
    so the field would confirm precisely what slug negotiation exists to hide
    -- that the slug is taken (#143 invariant 7). ``get_pack`` is unscoped by
    design, so this is the check that has to make it scoped.

    ``disclose_status=False`` short-circuits that for the branch that already
    established the row is foreign; it changes nothing about the result, only
    the wasted read.

    The status is ``None`` rather than an absent key whenever it is withheld,
    so every failure response from here has one shape. "We could not read it",
    "it is not yours", and "there is no row" are all simply no information,
    and a caller branching on the key's presence would be reading a
    distinction that must not exist.
    """
    from opencrab.pack.ownership import get_pack

    status: str | None = None
    if disclose_status:
        try:
            row = get_pack(ctx["sql"], slug)
        except Exception:  # noqa: BLE001 -- an error path must still answer
            row = None
        if row is not None and row["owner_id"] == subject_id:
            status = row["status"]
    return {"error": message, "pack_id": slug, "registry_status": status}


def _slugify(text: str) -> str:
    """Generate a URL-safe pack_id slug from a title string.

    Strips MCP surrogate junk first (``_clean_str``) then delegates to the
    shared slugify with ``allow_hangul=True``. Dropping Hangul collapsed every
    all-Korean title onto the same fallback (``pack``), so distinct Korean packs
    would have collided on one id — keeping Hangul makes the slug faithful.
    """
    from opencrab.mcp.tools import _clean_str

    return slugify(_clean_str(text), allow_hangul=True, fallback="pack")


def _nine_space_hint() -> str:
    """Build a concise 9-space grammar summary from manifest.SPACES."""
    try:
        from opencrab.grammar.manifest import SPACES
        lines = [
            "9-Space MetaOntology grammar (`space` + `node_type` values):",
        ]
        for space_id, spec in SPACES.items():
            types = ", ".join(spec.get("node_types", []))
            desc = spec.get("description", "")
            lines.append(f"  {space_id:<10} — {desc}: {types}")
        lines.append(
            "For valid edge relations between spaces, call ontology_manifest."
        )
        return "\n".join(lines)
    except Exception:
        return ""


_NINE_SPACE_HINT: str = _nine_space_hint()


# ---------------------------------------------------------------------------
# Cross-pack identity-ownership guard (#146 P1(a))
#
# _ingest_into_pack/pack_create write into several stores keyed by an
# identity the CALLER supplies (node_id / edge endpoints / source_id), not a
# server-generated key. Without this guard, a caller could silently
# overwrite a slot already attributed to a DIFFERENT pack simply by naming
# the same identity from inside their own (writable) destination pack -- an
# upsert with no ownership check (#143 invariant 4). The functions below
# probe every store slot a write will actually touch, using EACH store's own
# conflict key (see GraphStore.get_edge's docstring for why that key differs
# by backend), and refuse the whole item if any slot already belongs to a
# different pack.
#
# Judged on pack_id ALONE -- no owner/visibility lookup (no 3-way OR), and a
# same-owner different pack is refused too (no implicit re-attribution).
# Unavailable stores are skipped (nothing gets written there either, so
# "checked" == "written"); a missing probe method, a raised exception, or a
# non-dict return is fail-closed ("unverifiable"), never silently treated as
# "no conflict". A falsy/absent pack_id at the extracted path is
# unattributed legacy data and passes through untouched -- blocking it would
# break ordinary ingest into packs that predate this guard.
#
# Rejection messages never include the OTHER pack's id, owner, or visibility
# (#143 invariant 7): the only bit that leaks is "this identity is not your
# destination pack's", the same class of leak `pack_create`'s slug-suffix
# collision handling already accepts (see design doc "노출 표면" section).
#
# RACE-FREEDOM DEPENDS ON THE DISPATCH WRITE LOCK. This is a read-then-write
# pair, so two callers ingesting the same previously-absent identity into
# different packs must not both observe "no row". They cannot: dispatch_tool
# runs every `writes=True` handler inside `_write_lock()` (an exclusive flock
# on <data_dir>/write.lock), so the probe and the builder call sit in one
# critical section and the second caller sees the first caller's row. Both
# halves of that -- the writes=True flag and the lock actually being held
# around the handler -- are pinned by tests in tests/test_pack_identity_guard.py
# (PR #177 review round 5); do not drop either without replacing this with a
# conditional/CAS upsert. Residual, unchanged by this guard and shared with
# every other check-then-write here: a file lock does not span hosts, so a
# multi-host deployment against one remote DB is still racy.
# ---------------------------------------------------------------------------

# #148: the identity guard now lives in opencrab/pack/write_gate.py so the
# builder and the source writer enforce it too -- this module only had it
# because #146 built it here first. These stay as thin adapters because the
# call sites below pass a ctx dict, and because the by-id axis moved from
# `get_node_by_id` (LIMIT 1, no ORDER BY -- undefined which row won) to the
# all-rows classification inside the gate.


def _node_probe_conflict(ctx: dict[str, Any], space: str, node_type: str, node_id: str,
                         pack_id: str) -> str | None:
    return node_identity_conflict(
        ctx.get("neo4j"), ctx.get("mongo"), ctx.get("chroma"),
        space=space, node_type=node_type, node_id=node_id, pack_id=pack_id,
    )


def _source_probe_conflict(ctx: dict[str, Any], source_id: str, pack_id: str) -> str | None:
    return source_identity_conflict(
        ctx.get("mongo"), ctx.get("chroma"), source_id=source_id, pack_id=pack_id
    )


def _edge_probe_conflict(ctx: dict[str, Any], from_type: str, from_id: str, relation: str,
                         to_type: str, to_id: str, pack_id: str) -> str | None:
    return edge_identity_conflict(
        ctx.get("neo4j"), from_type=from_type, from_id=from_id, relation=relation,
        to_type=to_type, to_id=to_id, pack_id=pack_id,
    )


_identity_reject_message = identity_reject_message


def _warn_dropped_alias(dropped: str | None, kind: str, ident: str) -> None:
    """Surface a retired `pack` alias that disagreed with the destination pack.

    Caller-facing boundary, so the drop is not silent (#171). It is not an
    error: pack_id is authoritative here by construction, and refusing the
    whole write over a redundant legacy key would be a worse trade.
    """
    if dropped is None:
        return
    logger.warning(
        "%s %s: dropped retired 'pack' alias %r that disagreed with the "
        "destination pack_id", kind, ident, dropped,
    )


def _ingest_into_pack(
    pack_id: str,
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    text: str | None = None,
    source_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    text_as_node: bool = True,
    tenant_id: str = "default",
    subject_id: str | None = None,
) -> dict[str, Any]:
    """Store caller-supplied nodes/edges and/or embed text, all tagged with pack_id. No server LLM.

    Bills exactly one ``ingest`` billing event per call (issue #66) — shared
    by pack_create/pack_ingest so both go through a single instrumentation
    point rather than each needing its own billing call. Uses ``source_id``
    when text was ingested, else falls back to ``pack_id`` (on_ingest's
    signature requires a non-None string).

    Parameters
    ----------
    text_as_node:
        When True (default), raw ``text`` is materialised as a 9-space
        ``evidence/TextUnit`` graph node via ``builder.add_node`` so it
        becomes a first-class grammar-compliant node (graph + doc + vector,
        all pack_id-tagged).  ``hybrid.ingest`` and ``mongo.upsert_source``
        are skipped to avoid duplicate vector writes under the same id.
        When False, the legacy path is used: vector-only embedding via
        ``hybrid.ingest`` + doc_sources record via ``mongo.upsert_source``.
    """
    from opencrab.grammar.validator import validate_edge, validate_node
    from opencrab.mcp.tools import _clean_meta, _clean_str, _get_context
    from opencrab.ontology.builder import store_write_failures, store_write_succeeded

    ctx = _get_context()
    added_nodes = 0
    added_edges = 0
    node_errors: list[str] = []
    edge_errors: list[str] = []
    stores: dict[str, Any] = {}
    evidence_node: str | None = None
    # Billing-only signal (issue #66 codex review, findings [4]/[6]): kept
    # deliberately SEPARATE from added_nodes/added_edges below.
    # added_nodes/added_edges require store_write_failures() to see zero
    # failures across ALL stores (graph + every optional one) — that is this
    # tool's own, stricter, pre-existing "did I fully write this" accounting
    # for its response body (added_nodes/node_errors), and changing it would
    # ripple into that response contract for no billing-accuracy reason.
    # Billability only needs the graph store (the system of record) to have
    # landed — the same rule graph.py/harness.py/apply.py already use — so
    # it is computed here independently via store_write_succeeded(), proving
    # the billing gate (`wrote_anything` below) is driven by that one
    # function alone, not by store_write_failures()-derived counters.
    billable_write = False

    for item in nodes or []:
        try:
            item_space = _clean_str(item.get("space", ""))
            item_node_type = _clean_str(item.get("node_type", ""))
            item_node_id = _clean_str(item.get("node_id", ""))
            # Grammar validation BEFORE the probes, not just inside
            # builder.add_node afterwards. The probes pass node_type to
            # graph.get_node, and Neo4jStore.get_node interpolates it into
            # Cypher as a label (f"MATCH (n:{node_type} ...)"). Before the
            # identity guard existed, every caller-supplied node_type reached
            # a store only after this whitelist check; keeping that ordering
            # is what stops an unvalidated label from being interpolated.
            # validate_node is a pure membership test against the 9-space
            # manifest, so builder.add_node re-running it costs nothing.
            validate_node(item_space, item_node_type).raise_if_invalid()
            reason = _node_probe_conflict(
                ctx, item_space, item_node_type, item_node_id, pack_id
            )
            if reason:
                node_errors.append(
                    _identity_reject_message("node", item_node_id, reason)
                )
                continue
            props = dict(_clean_meta(item.get("properties") or {}))
            _warn_dropped_alias(apply_pack_tag(props, pack_id), "node", item_node_id)
            node_result = ctx["builder"].add_node(
                space=item_space,
                node_type=item_node_type,
                node_id=item_node_id,
                properties=props,
                pack_id=pack_id,
            )
            # add_node never raises for a per-store failure (see builder.py's
            # module docstring) — it reports "error: ..." inside the returned
            # stores map instead, so a bare try/except here would count a
            # failed write as a success. Inspect the map explicitly.
            node_stores = node_result.get("stores") if isinstance(node_result, dict) else None
            failures = store_write_failures(node_stores or {})
            if failures:
                node_errors.append(
                    f"{item.get('node_id', '?')}: " + "; ".join(failures)
                )
            else:
                added_nodes += 1
            if store_write_succeeded(node_stores or {}, "graph"):
                billable_write = True
        except Exception as exc:
            node_errors.append(f"{item.get('node_id', '?')}: {exc}")

    for item in edges or []:
        try:
            item_from_space = _clean_str(item.get("from_space", ""))
            item_from_id = _clean_str(item.get("from_id", ""))
            item_relation = _clean_str(item.get("relation", ""))
            item_to_space = _clean_str(item.get("to_space", ""))
            item_to_id = _clean_str(item.get("to_id", ""))
            # Same ordering rule as the node loop above: the edge probe passes
            # `relation` to graph.get_edge, and Neo4jStore.get_edge
            # interpolates it into Cypher (-[r:{relation}]->), so it must
            # clear the whitelist before any store sees it.
            validate_edge(item_from_space, item_to_space, item_relation).raise_if_invalid()

            # Endpoint types are resolved the same way builder.add_edge
            # resolves them (graph.lookup_node_type). A `None` result is
            # ambiguous -- it means either "node doesn't exist" or "the
            # lookup query itself failed" (Neo4jStore.lookup_node_type
            # swallows transient query errors and returns None either way).
            # Those two cases cannot be told apart from here, and treating
            # an unresolvable endpoint as "no conflict, skip the probe" is
            # unsafe: if the underlying error is transient and clears
            # before builder.add_edge runs its own lookup, the endpoints
            # resolve there and the write proceeds with no pack_id check at
            # all. So an unresolvable endpoint fails closed (rejected) here,
            # exactly like the other "cannot verify" cases in _foreign_pack.
            graph_store = ctx.get("neo4j")
            lookup = getattr(graph_store, "lookup_node_type", None) if graph_store is not None else None
            from_type = lookup(item_from_id) if lookup is not None else None
            to_type = lookup(item_to_id) if lookup is not None else None
            if from_type is None or to_type is None:
                edge_errors.append(
                    _identity_reject_message(
                        "edge", f"{item_from_id}->{item_to_id}", "unverifiable"
                    )
                )
                continue
            reason = _edge_probe_conflict(
                ctx, from_type, item_from_id, item_relation, to_type, item_to_id, pack_id
            )
            if reason:
                edge_errors.append(
                    _identity_reject_message(
                        "edge", f"{item_from_id}->{item_to_id}", reason
                    )
                )
                continue

            props = dict(_clean_meta(item.get("properties") or {}))
            _warn_dropped_alias(apply_pack_tag(props, pack_id), "edge", item.get("id") or "?")
            edge_result = ctx["builder"].add_edge(
                from_space=item_from_space,
                from_id=item_from_id,
                relation=item_relation,
                to_space=item_to_space,
                to_id=item_to_id,
                properties=props,
                pack_id=pack_id,
            )
            # Same as above: a missing edge endpoint is reported as
            # stores["graph"] = "no match (missing node: ...)" without
            # raising, so it must be read out of the stores map too.
            edge_stores = edge_result.get("stores") if isinstance(edge_result, dict) else None
            failures = store_write_failures(edge_stores or {})
            if failures:
                edge_errors.append(
                    f"{item.get('from_id', '?')}→{item.get('to_id', '?')}: "
                    + "; ".join(failures)
                )
            else:
                added_edges += 1
            if store_write_succeeded(edge_stores or {}, "graph"):
                billable_write = True
        except Exception as exc:
            edge_errors.append(
                f"{item.get('from_id', '?')}→{item.get('to_id', '?')}: {exc}"
            )

    text_ingested = False
    if text and source_id:
        text = _clean_str(text)
        meta = _clean_meta(metadata or {})
        _warn_dropped_alias(apply_pack_tag(meta, pack_id), "source", source_id)
        # issue #52 follow-up: the legacy branch below (text_as_node=False)
        # writes this same `meta` into both the vector store (hybrid.ingest)
        # and doc_sources (mongo.upsert_source) with no `space` tag, so
        # spaces-filtered queries could never match it. The text_as_node=True
        # branch a few lines down already tags its TextUnit node
        # `space="evidence"` — this is the same kind of content (raw
        # ingested text), so default it to the same space here for
        # consistency, while still letting an explicit caller-supplied
        # `metadata["space"]` (e.g. apps/api/main.py's IngestRequest.metadata,
        # which already passes arbitrary caller metadata straight through)
        # win. Existing rows written before this change remain untagged —
        # see the space-filter warning in HybridQuery.query() and #52's
        # backfill note.
        meta.setdefault("space", "evidence")

        if text_as_node:
            # Materialise text as a 9-space evidence/TextUnit graph node so it
            # becomes a grammar-compliant first-class node (graph + doc_nodes +
            # vector), all tagged with pack_id.  builder.add_node handles vector
            # embedding internally, so we skip hybrid.ingest / mongo.upsert_source
            # to avoid duplicate writes under the same source_id.
            try:
                reason = _node_probe_conflict(
                    ctx, "evidence", "TextUnit", source_id, pack_id
                )
                if reason:
                    msg = _identity_reject_message("node", source_id, reason)
                    node_errors.append(msg)
                    stores["evidence_node"] = msg
                else:
                    node_props: dict[str, Any] = {
                        "pack_id": pack_id,
                        "text": text,
                    }
                    if meta.get("title"):
                        node_props["title"] = meta["title"]
                    if meta.get("source"):
                        node_props["source"] = meta["source"]
                    evidence_result = ctx["builder"].add_node(
                        space="evidence",
                        node_type="TextUnit",
                        node_id=source_id,
                        properties=node_props,
                        pack_id=pack_id,
                    )
                    # Same store-map inspection as the node/edge loops above —
                    # a per-store failure here would otherwise still count as a
                    # successful evidence node.
                    evidence_stores = (
                        evidence_result.get("stores")
                        if isinstance(evidence_result, dict)
                        else None
                    )
                    failures = store_write_failures(evidence_stores or {})
                    if failures:
                        node_errors.append(
                            f"{source_id} (evidence/TextUnit): " + "; ".join(failures)
                        )
                        stores["evidence_node"] = "; ".join(failures)
                    else:
                        evidence_node = source_id
                        added_nodes += 1
                        stores["evidence_node"] = "ok"
                    if store_write_succeeded(evidence_stores or {}, "graph"):
                        billable_write = True
            except Exception as exc:
                node_errors.append(f"{source_id} (evidence/TextUnit): {exc}")
                stores["evidence_node"] = f"error: {exc}"
            text_ingested = True
        else:
            # Legacy path: vector-only embedding + doc_sources record, now
            # through the write_source chokepoint (#148) instead of two
            # independent calls -- see opencrab/pack/source_writer.py for why
            # (doc row first, one ownership stamp for both).
            reason = _source_probe_conflict(ctx, source_id, pack_id)
            if reason:
                node_errors.append(_identity_reject_message("source", source_id, reason))
            else:
                from opencrab.pack.source_writer import write_source

                write_result = write_source(
                    ctx["sql"], ctx["hybrid"], ctx["mongo"], ctx["chroma"],
                    text=text, source_id=source_id,
                    metadata=meta, pack_id=pack_id,
                )
                stores.update(write_result.get("stores", {}))

                text_ingested = True

    # #66 codex re-review, findings [4]/[6]: the billing gate must be
    # provably driven by store_write_succeeded() ALONE, not by
    # store_write_failures()-derived counters (added_nodes/added_edges are
    # kept for the tool's own response body — see `billable_write`'s
    # docstring-comment above the node loop — but must not leak into this
    # decision). billable_write already covers every node/edge/evidence-node
    # write via store_write_succeeded(..., "graph"). The text_as_node=False
    # legacy branch never touches `graph` at all (vector-only embedding + a
    # doc_sources record), so its own signal is store_write_succeeded(stores)
    # with no key — positive confirmation that at least one of
    # chromadb/documents actually came back a recognized "ok"-prefixed status
    # (see that function's docstring in builder.py for the full success-value
    # inventory this is based on, and why "unavailable" alone must not bill).
    text_stores_failed = bool(store_write_failures(stores))  # for `status` below only
    legacy_text_landed = text_ingested and not text_as_node and store_write_succeeded(stores)
    wrote_anything = billable_write or legacy_text_landed
    if wrote_anything:
        billing_result = ctx["billing"].on_ingest(tenant_id, subject_id, source_id or pack_id)
        if not billing_result.get("ok"):
            # #105: don't repeat the on_node_write/on_query pattern of
            # discarding emit()'s result — surface a failed persist in this
            # module's own log context too, without failing the
            # (already-succeeded) ingest.
            logger.warning(
                "on_ingest billing event failed to persist (pack_id=%s): %s",
                pack_id, billing_result.get("error"),
            )
    ctx["hybrid"].invalidate_bm25_cache()

    # Partial failure = any node/edge write error, or any leftover "error:"/
    # "no match" status sitting in the legacy text-path `stores` dict (chromadb/
    # documents; the key was renamed from "mongodb" in #148 to match the
    # name the source writer and REST already use). pack_create/pack_ingest
    # both build their response as
    # {"status": "ok", ..., **ingest_result} — since ingest_result is spread
    # last, this "status" wins over their literal "ok" and callers get an
    # accurate top-level signal instead of an unconditional "ok".
    status = "partial" if node_errors or edge_errors or text_stores_failed else "ok"

    return {
        "status": status,
        "pack_id": pack_id,
        "added_nodes": added_nodes,
        "added_edges": added_edges,
        "node_errors": node_errors,
        "edge_errors": edge_errors,
        "stores": stores,
        "text_ingested": text_ingested,
        "evidence_node": evidence_node,
    }


_QUERY_DEFAULT_LIMIT = 10


def _manifest_extras() -> dict[str, tuple[list[str], list[str]]]:
    """``{pack_id: (keywords, tags)}`` from the on-disk manifest registry.

    Loaded ONCE per call (the registry scan walks every pack directory, so a
    per-pack ``get_pack`` would re-read every manifest N times). Joined onto
    the graph-derived candidates by exact pack_id, and the registry can never
    *introduce* a pack: a manifest with no ingested nodes stays invisible.
    ``category`` is folded into tags so the shared scorer needs no new field.
    """
    from opencrab.config import get_settings
    from opencrab.ontology.pack_registry import load_pack_registry

    try:
        registry = load_pack_registry(get_settings().local_data_dir)
    except Exception as exc:  # noqa: BLE001 — registry is optional metadata
        logger.debug("manifest registry load failed: %s", exc)
        return {}

    extras: dict[str, tuple[list[str], list[str]]] = {}
    for info in registry:
        tags = list(info.tags)
        category = info.raw.get("category")
        if isinstance(category, str) and category:
            tags.append(category)
        extras[info.pack_id] = (list(info.keywords), tags)
    return extras


@tool(
    "content_pack_list",
    {
        "description": (
            "List content packs loaded in the localcrab ontology graph. Returns pack_id, node count, "
            "and display title for each pack. Without `query` this is the full list; with `query` the "
            "packs are filtered and ranked by deterministic keyword relevance (pack_id, title, "
            "description, keywords/tags/category)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_nodes": {"type": "integer", "description": "Only return packs with at least this many nodes, when node counts are known (default 0 -- see node_count_known in the response).", "default": 0},
                "query": {"type": "string", "description": "Optional search text. When given, only packs scoring above zero are returned, ordered by relevance."},
                "limit": {"type": "integer", "description": "Maximum packs to return. Defaults to 10 when `query` is given, unlimited otherwise."},
            },
            "required": [],
        },
    },
    order=9,
    access=AccessTier.READ,
)
def content_pack_list(
    min_nodes: int = 0,
    query: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    List content packs the caller can see, per the ``packs`` ownership/
    visibility registry.

    #146: the registry (``opencrab.pack.ownership.list_packs_for`` — the
    caller's own packs plus every non-private one, #143 invariant 3) is now
    the SOURCE of this list, not ``graph.list_packs()``'s node-count
    aggregation (#143 acceptance criteria: "graph.list_packs()를 팩 목록의
    권위로 쓰는 호출부가 0건"). A pack registered but not yet graph-loaded
    (e.g. right after ``pack_create``) still shows up; a graph-loaded
    pack_id with no registry row (shouldn't normally happen, but pre-#146
    legacy data before the backfill migration runs) is dropped rather than
    surfaced with no known owner.

    graph.list_packs(0) is consulted only as an AUXILIARY node-count/title
    source, joined onto the registry rows by pack_id. When the graph store
    is unavailable, or the aggregation call itself raises, node counts are
    simply unknown for every pack in this response (never a partial mix,
    never a top-level error) -- see ``node_count_known``/
    ``min_nodes_applied`` below.

    Response shape
    --------------
    Top-level ``node_count_known`` (bool) and ``min_nodes_applied`` (bool)
    describe the WHOLE response, always both true or both false together:

    - True: graph aggregation succeeded. Each pack's ``node_count`` is a
      real int (0 is a legitimate "no nodes yet" count, not "unknown").
      ``min_nodes`` was applied as a real filter.
    - False: graph unavailable or the aggregation call raised. Each pack's
      ``node_count`` is ``None`` -- "unknown", never guessed at or
      defaulted to 0. ``min_nodes`` is NOT applied (filtering on an unknown
      quantity would be arbitrary) -- every readable pack is returned.

    Parameters
    ----------
    min_nodes:
        Only return packs with at least this many nodes (default 0). Only
        takes effect when node counts are known -- see above.
    query:
        Optional relevance filter. Every readable registered pack is a
        candidate (not just graph-loaded ones); each is scored with the
        same deterministic scorer auto_pack uses, and packs scoring zero
        are dropped. Ordering is ``(score desc, pack_id asc)``.
    limit:
        Cap on returned packs. Defaults to 10 when ``query`` is given.

    NOTE: pack_ingest calls this with NO arguments on purpose — its pack_id
    existence check is an exact membership test against the FULL list a
    caller can see. Passing a query there would shrink the candidate set and
    reject packs that really exist.
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_str, _get_context
    from opencrab.pack.ownership import list_packs_for

    ctx = _get_context()
    graph = ctx["neo4j"]

    registry_rows = list_packs_for(ctx["sql"], current_principal())

    # All four backends implement list_packs() natively (Local/PG: SQL GROUP
    # BY; Kuzu/Neo4j: Cypher aggregation) — see opencrab/stores/_graph_protocol.py.
    # Called unfiltered (0): min_nodes filtering now happens below, against
    # the registry-sourced candidate list, not delegated to the store.
    node_count_known = False
    graph_agg: dict[str, dict[str, Any]] = {}
    if getattr(graph, "available", False):
        try:
            graph_agg = {
                r["pack_id"]: r for r in graph.list_packs(0) if r.get("pack_id")
            }
            node_count_known = True
        except Exception as exc:  # noqa: BLE001 — aggregation is best-effort
            logger.debug("content_pack_list: graph.list_packs() aggregation failed: %s", exc)
            node_count_known = False
    min_nodes_applied = node_count_known

    enriched = []
    for row in registry_rows:
        pid = row["pack_id"]
        agg = graph_agg.get(pid)
        if node_count_known:
            node_count = agg["node_count"] if agg else 0
            if node_count < min_nodes:
                continue
        else:
            node_count = None
        # #146: registry title/description win (they're the authority for
        # this pack's identity); the graph anchor node is only a fallback
        # for a pack the registry has no title/description for yet.
        raw_title = row.get("title") or (agg.get("sample_title") if agg else "") or ""
        description = row.get("description") or (agg.get("sample_description") if agg else "") or ""
        display_title = raw_title.replace(" ontology pack", "").replace(" ontology Pack", "").strip()
        enriched.append({
            "pack_id": pid,
            "node_count": node_count,
            "title": display_title or pid,
            "_raw_title": raw_title,
            "_description": description,
        })

    # Whitespace-only query == no query (an empty filter must not return an
    # empty pack list). This is input normalisation, not term correction.
    query = _clean_str(query).strip() if query else ""
    if not query:
        if node_count_known:
            enriched.sort(key=lambda p: (-p["node_count"], p["pack_id"]))
        else:
            enriched.sort(key=lambda p: p["pack_id"])
        packs = [_display_pack(p) for p in enriched]
        total = len(packs)
        response: dict[str, Any] = {
            "total": total,
            "node_count_known": node_count_known,
            "min_nodes_applied": min_nodes_applied,
            "packs": packs,
        }
        if limit is not None and limit >= 0 and total > limit:
            response["total"] = limit
            response["packs"] = packs[:limit]
            response["truncated"] = True
        return response

    scanned = len(enriched)
    ranked = _rank_packs(query, enriched)
    effective_limit = _QUERY_DEFAULT_LIMIT if limit is None else limit
    truncated = effective_limit >= 0 and len(ranked) > effective_limit
    if truncated:
        ranked = ranked[:effective_limit]
    response = {
        "total": len(ranked),
        "node_count_known": node_count_known,
        "min_nodes_applied": min_nodes_applied,
        "query": query,
        "scanned": scanned,
        "packs": ranked,
    }
    if truncated:
        response["truncated"] = True
    return response


def _display_pack(item: dict[str, Any]) -> dict[str, Any]:
    """Strip the internal scoring-only fields (``_raw_title``/
    ``_description``) an ``enriched`` entry carries, leaving the public
    response shape."""
    return {"pack_id": item["pack_id"], "node_count": item["node_count"], "title": item["title"]}


def _rank_packs(query: str, enriched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score candidate packs against ``query``; deterministic ordering.

    ``enriched`` is content_pack_list's internal per-pack list (registry
    title/description already resolved with graph-anchor fallback, node
    counts already known-or-null) -- the SAME resolution rule scoring uses
    here is what built ``title``/``_raw_title``/``_description`` in the
    first place, so ranking and display never disagree about a pack's
    identity.
    """
    from opencrab.ontology.pack_registry import PackInfo, score_pack

    extras = _manifest_extras()
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for item in enriched:
        pack_id = item["pack_id"]
        keywords, tags = extras.get(pack_id, ([], []))
        info = PackInfo(
            pack_id=pack_id,
            title=item["_raw_title"],
            description=item["_description"],
            keywords=keywords,
            tags=tags,
        )
        score, matched = score_pack(query, info)
        if score <= 0.0:
            continue
        display = _display_pack(item)
        display["score"] = score
        display["matched"] = matched
        scored.append((score, pack_id, display))

    # (score desc, pack_id asc) -- same ordering whether node counts are
    # known or not (#146 C: node_count is no longer part of the tie-break,
    # since it can be null).
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored]


@tool(
    "pack_create",
    {
        "description": (
            "Create a new localcrab ontology pack and ingest content into it. "
            "Caller supplies pre-extracted nodes/edges (same shape as ontology_add_node/ontology_add_edge); "
            "the server does NOT call any LLM. pack_id is auto-slugged from title unless provided. "
            "Optional `text` is embedded locally into the vector/doc store (no external API).\n\n"
            + _NINE_SPACE_HINT
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Human-readable pack title (also used to auto-generate pack_id if not provided).",
                },
                "pack_id": {
                    "type": "string",
                    "description": "Optional explicit pack_id slug. Auto-slugged from title if omitted.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional pack description stored on the anchor node.",
                },
                "nodes": {
                    "type": "array",
                    "description": "Pre-extracted ontology nodes to add to the pack.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "space": {"type": "string", "description": "MetaOntology space (e.g. 'concept', 'resource')."},
                            "node_type": {"type": "string", "description": "Node type within the space (e.g. 'Entity', 'Document')."},
                            "node_id": {"type": "string", "description": "Stable unique identifier."},
                            "properties": {"type": "object", "description": "Arbitrary key/value node properties."},
                        },
                        "required": ["space", "node_type", "node_id"],
                    },
                },
                "edges": {
                    "type": "array",
                    "description": "Pre-extracted ontology edges to add to the pack.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_space": {"type": "string"},
                            "from_id": {"type": "string"},
                            "relation": {"type": "string", "description": "Relation label (call ontology_manifest for valid relations per space pair)."},
                            "to_space": {"type": "string"},
                            "to_id": {"type": "string"},
                            "properties": {"type": "object"},
                        },
                        "required": ["from_space", "from_id", "relation", "to_space", "to_id"],
                    },
                },
                "text": {
                    "type": "string",
                    "description": "Optional raw text. Materialised as a 9-space evidence/TextUnit graph node by default (text_as_node=true).",
                },
                "text_as_node": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true (default), text is stored as an evidence/TextUnit graph node (grammar-compliant, pack_id-tagged). Set false for legacy vector-only embedding.",
                },
            },
            "required": ["title"],
        },
    },
    order=13,
    access=AccessTier.WRITE,
    writes=True,
)
def pack_create(
    title: str,
    pack_id: str | None = None,
    description: str | None = None,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    text: str | None = None,
    text_as_node: bool = True,
) -> dict[str, Any]:
    """
    Create a new localcrab ontology pack and ingest content into it.

    Caller supplies pre-extracted nodes/edges; the server does NOT call any LLM.
    pack_id is auto-slugged from title unless explicitly provided.
    Optional text is materialised as a 9-space evidence/TextUnit graph node
    (text_as_node=True, default) or embedded as a vector blob only (False).
    The ``ingest`` billing event's subject is the caller's server-derived
    ``current_principal()`` (#145) -- never a client argument; tenant_id
    stays fixed at 'default'.

    Registry lifecycle (#170, design v4 §3.5): the pack_id is reserved
    ``creating`` before any content is written and promoted to ``ready``
    once its graph anchor is confirmed. If the anchor cannot be confirmed
    after ``builder.add_node`` has run, the row is demoted to ``partial``
    -- never deleted. The ONE exception -- the ONLY branch anywhere in this
    function that deletes the registry row -- is an anchor identity
    conflict caught BEFORE ``builder.add_node`` is ever called, where "no
    content exists for this pack" is proven by control flow rather than by
    probing a store a slow remote commit could still be about to fill. See
    that branch's own comment, and design v4 §3.0, for why every later
    failure demotes instead.

    Only the ANCHOR's fate moves the registry. Once the anchor is
    confirmed the row stays ``ready`` even if an optional store (docs or
    vector) rejected the anchor, or if individual nodes/edges fail during
    the ingest below; those are reported in the RESPONSE's ``status``
    field as ``"partial"``, which is a different thing from the registry
    status of the same name.
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_str, _get_context
    from opencrab.ontology.builder import store_write_failures, store_write_succeeded
    from opencrab.pack.lifecycle import (
        ANCHOR_ABSENT,
        ANCHOR_GRAPH,
        ANCHOR_OPTIONAL_ONLY,
        PROBE_PRESENT,
        anchor_verdict,
        probe_anchor,
    )
    from opencrab.pack.ownership import (
        PACK_STATUS_CREATING,
        PACK_STATUS_READY,
        _insert_pack,
        anchor_node_id,
        begin_pack_creation,
        delete_pack_row,
        get_pack,
        mark_pack_partial,
        mark_pack_ready,
    )

    principal = current_principal()
    tenant_id = "default"
    subject_id = principal.user_id
    slug = _clean_str(pack_id) if pack_id else _slugify(title)
    if not slug:
        return {"error": "Could not derive a valid pack_id from title."}

    ctx = _get_context()
    graph = ctx["neo4j"]
    docs = ctx["mongo"]
    vector = ctx["chroma"]
    # #146 P1(a): reject before the registry row is created -- if the graph
    # (system of record for pack content, and where the identity-ownership
    # probes below read from) is down, there is nothing safe to register.
    # Same precondition pack_ingest already enforces; doing it here too
    # means graph-unavailable can never leave a registry row pointing at a
    # pack that was never actually written (docs/sql/vector/audit partial
    # state with no compensating action needed, because nothing was
    # registered in the first place).
    if not getattr(graph, "available", False):
        return {"error": "graph store unavailable"}

    # #170: two-phase creation. The slug is reserved status='creating' --
    # NOT immediately ready -- so every read path (readable_pack_ids/
    # list_packs_for filter on status='ready') and every write path but the
    # anchor write itself (write_gate's default allowed_statuses excludes
    # 'creating') treats this pack as if it does not exist yet, until it is
    # promoted or demoted below. Same slug-collision negotiation as
    # create_pack (see begin_pack_creation's docstring): a collision is
    # NEVER reported as an error (#143 invariant 7), so `slug` below may
    # differ from the caller's requested pack_id. The actually-assigned
    # pack_id is always in the response.
    try:
        slug = begin_pack_creation(
            ctx["sql"],
            owner_id=subject_id,
            pack_id=slug,
            title=_clean_str(title),
            description=_clean_str(description or ""),
        )
    except Exception as exc:
        return {"error": f"pack registration failed: {exc}"}

    anchor_id = anchor_node_id(slug)

    # #146 P1(a): the slug is only assigned above (registry may have
    # suffixed it on collision), so the anchor identity probe -- same
    # _node_probes 3-way check the node/edge loops use -- has to run here,
    # AFTER registration but BEFORE builder.add_node, so no store is ever
    # written to when the anchor identity is already someone else's.
    anchor_probe_reason = _node_probe_conflict(ctx, "resource", "Dataset", anchor_id, slug)
    if anchor_probe_reason is not None:
        # THE ONLY DELETE IN THIS FUNCTION (#170, design v4 §3.0):
        # builder.add_node has not been called yet -- not here, not on any
        # earlier iteration, because this is the first and only anchor
        # write attempt this call makes -- so "no content was written for
        # this pack_id" is proven by control flow, no probe needed. The
        # slug was also reserved by begin_pack_creation a few lines above,
        # in THIS call, so no other request can have written under it
        # either. Both of those stop holding the instant builder.add_node
        # is actually called below, which is why no later branch in this
        # function deletes anything.
        error_msg = _identity_reject_message("node", anchor_id, anchor_probe_reason)
        try:
            deleted = delete_pack_row(
                ctx["sql"], slug, subject_id, only_status=(PACK_STATUS_CREATING,)
            )
        except Exception:
            deleted = False
        if not deleted:
            # Delete failed outright, or matched zero rows (something else
            # moved this row out of 'creating' between begin_pack_creation
            # and here -- e.g. an operator's manual intervention, since no
            # other code path touches a 'creating' row before the writer
            # runs). Leaving it behind would strand a phantom 'creating'
            # row forever, so demote it instead: a 'partial' row is at
            # least visible to `packs repair-registry`'s reporting, where a
            # stuck 'creating' row is not (repair only acts on rows older
            # than its threshold, but reports both).
            try:
                mark_pack_partial(ctx["sql"], slug, subject_id)
            except Exception:
                logger.warning(
                    "pack_create: could not demote pack_id=%s after a failed "
                    "compensating delete (identity-conflict branch)", slug,
                )
        return _pack_error(ctx, slug, subject_id, error_msg)

    anchor_result: dict[str, Any] | None = None
    anchor_exc: Exception | None = None
    try:
        anchor_result = ctx["builder"].add_node(
            space="resource",
            node_type="Dataset",
            node_id=anchor_id,
            properties={
                "pack_id": slug,
                "title": _clean_str(title),
                "description": _clean_str(description or ""),
                "created_by": "localcrab-mcp",
            },
            pack_id=slug,
            pack_anchor=True,
        )
    except Exception as exc:
        anchor_exc = exc

    # ------------------------------------------------------------------
    # PAST THIS POINT, NOTHING IN THIS FUNCTION DELETES THE REGISTRY ROW
    # (#170, design v4 §3.0 -- the rule is stated once, at the top of this
    # function, and enforced here by simply never calling delete_pack_row
    # again). Two reasons a probe-based delete would be unsafe from here
    # on, both measured against this codebase rather than assumed:
    #   (1) "the anchor probe came back absent" has never implied "this
    #       pack has zero content". When this was written the standing
    #       counterexample was load.py's chunk loader writing docs/vector
    #       outside write_gate.authorize; #205 closed that one, but the
    #       implication only ever held as long as EVERY writer was known
    #       and gated, which is a property a future writer can quietly
    #       take away without touching this file.
    #   (2) store_write_succeeded() is fail-closed: a commit followed by a
    #       dropped connection reads as "not landed" even when it landed,
    #       and a slow remote graph backend can still be about to commit
    #       after this function has already decided to act. Deleting on
    #       that ambiguity would delete the registry row out from under a
    #       pack whose anchor lands a moment later -- a graph ORPHAN with
    #       no registry row, exactly the state
    #       read_scope.assert_registry_covers_graph refuses to boot with.
    # Every failure from here on is a 'partial' demotion instead.
    # ------------------------------------------------------------------
    anchor_stores = anchor_result.get("stores") if isinstance(anchor_result, dict) else None
    # Same per-store inspection as _ingest_into_pack: add_node doesn't raise
    # for a per-store failure, it reports "error: ..."/"unavailable" (graph)
    # inside the returned stores map. graph is the system of record, so the
    # positive store_write_succeeded() check is the single source of truth
    # for whether the anchor actually landed -- not just "no error
    # reported".
    graph_landed = anchor_exc is None and store_write_succeeded(anchor_stores or {}, "graph")

    if anchor_exc is not None:
        write_detail = f"anchor node write raised: {anchor_exc}"
    else:
        graph_failures = [
            f for f in store_write_failures(anchor_stores or {}) if f.startswith("graph:")
        ]
        write_detail = "anchor node write did not confirm in graph: " + (
            "; ".join(graph_failures) or "graph write did not succeed"
        )

    if not graph_landed:
        # store_write_succeeded()'s fail-closed reading may simply be
        # wrong -- re-probe the stores directly (the #146 follow-up's
        # "애매한 커밋 후 재조회") before believing "not landed".
        verdict = anchor_verdict(probe_anchor(graph, docs, vector, slug))
        if verdict == ANCHOR_GRAPH:
            # The ambiguous commit actually landed. Join the normal success
            # path below exactly as if store_write_succeeded had said so
            # the first time.
            graph_landed = True
        else:
            # optional-only / absent / unverifiable all take the SAME
            # registry action (mark_pack_partial, never delete) but must
            # give the operator a DISTINGUISHABLE reason (design v4 §3.5).
            if verdict == ANCHOR_OPTIONAL_ONLY:
                probe_detail = (
                    "re-probe found the anchor in an optional store (docs/vector) "
                    "but NOT in graph -- by this system's definition the pack does "
                    "not exist yet"
                )
            elif verdict == ANCHOR_ABSENT:
                probe_detail = "re-probe found the anchor in no store at all"
            else:  # ANCHOR_UNVERIFIABLE
                probe_detail = (
                    "the anchor's landing status could not be verified (graph "
                    "unavailable, or the re-probe itself failed)"
                )
            try:
                mark_pack_partial(ctx["sql"], slug, subject_id)
            except Exception:
                logger.warning(
                    "pack_create: could not demote pack_id=%s after an "
                    "unconfirmed anchor write (verdict=%s)", slug, verdict,
                )
            return _pack_error(
                ctx, slug, subject_id,
                f"{write_detail}. {probe_detail}. Pack '{slug}' marked "
                f"partial, not deleted -- see #170 design v4 §3.0.",
            )

    # graph_landed is True here, either from the direct check above or from
    # the re-probe's ANCHOR_GRAPH verdict.
    try:
        finalized = mark_pack_ready(ctx["sql"], slug, subject_id)
    except Exception:
        # The registry can be briefly unreachable (locked SQLite, a dropped
        # connection) with the graph anchor already committed. Letting that
        # propagate would abandon the call OUTSIDE this function's response
        # contract: the caller would get an exception instead of the
        # assigned pack_id -- which after slug suffixing they cannot
        # reconstruct -- while the row sits in 'creating'. Fall into the
        # reconciliation below instead, which re-reads the row and is
        # already written for exactly this question ("the transition's
        # outcome is unknown, what IS the row now?"). If the UPDATE did land
        # before the raise, the re-read sees 'ready' and this call continues
        # normally.
        logger.warning(
            "pack_create: finalizing pack_id=%s raised; reconciling", slug,
            exc_info=True,
        )
        finalized = False

    if not finalized:
        # mark_pack_ready only matches a row that is STILL 'creating' and
        # STILL owned by subject_id (see its own docstring). Reaching here
        # therefore means either that call raised (above) or another actor
        # moved the row -- no branch above this point deletes or reassigns
        # it, so this function's own control flow never produces the
        # sub-cases below (design v4 §3.5).
        try:
            row = get_pack(ctx["sql"], slug)
        except Exception as exc:
            return _pack_error(
                ctx, slug, subject_id,
                f"could not reconcile pack '{slug}' after anchor write: {exc}",
            )

        if row is None:
            # No delete branch in this design reaches this point (see the
            # comment block above) -- only a manual `DELETE FROM packs` can
            # produce it. The anchor write above may still have landed in
            # graph, so re-register (status='ready') to avoid leaving a
            # graph ORPHAN with no registry row -- but ONLY when the graph
            # anchor is POSITIVELY confirmed for THIS slug; an unconfirmed
            # anchor must not be re-registered on a guess.
            try:
                anchor_confirmed = (
                    probe_anchor(graph, docs, vector, slug).get("graph") == PROBE_PRESENT
                )
            except Exception:
                anchor_confirmed = False
            reregistered = False
            if anchor_confirmed:
                try:
                    # PK-safe: if someone else's row already occupies this
                    # exact slug, _insert_pack returns False instead of
                    # overwriting it -- this call never writes into another
                    # subject's pack. That False is kept, not discarded:
                    # "we tried" and "it landed" are different claims and the
                    # message below makes one of them.
                    reregistered = _insert_pack(
                        ctx["sql"], slug, subject_id, _clean_str(title),
                        _clean_str(description or ""), None, status=PACK_STATUS_READY,
                    )
                    if not reregistered:
                        # PK conflict: some other row already holds this slug.
                        # Server-side only -- the response below must not say
                        # so (see its comment).
                        logger.warning(
                            "pack_create: re-registration of orphaned anchor "
                            "pack_id=%s did not land; the slug is already "
                            "held by another row", slug,
                        )
                except Exception:
                    reregistered = False
                    logger.warning(
                        "pack_create: re-registration of orphaned anchor "
                        "pack_id=%s raised", slug, exc_info=True,
                    )
            # Either way: no ingest. This call cannot safely attribute the
            # missing row to its own control flow, so it never proceeds as
            # if it had succeeded.
            return _pack_error(
                ctx, slug, subject_id,
                (
                    f"pack '{slug}'s registry row was missing when finalizing "
                    f"the anchor write (not caused by this call -- see #170 "
                    f"design v4 §3.5); "
                    + (
                        "re-registered as ready since its graph anchor is "
                        "confirmed present, but "
                        if reregistered
                        # "not confirmed to land" rather than "did not
                        # land": one of the two causes is a raised insert,
                        # and an insert that raises after committing DID
                        # land. Same rule the anchor probe and the registry
                        # status already follow here -- not knowing is its
                        # own answer, never the negative one.
                        #
                        # Deliberately silent on WHY, too. The
                        # two causes are a PK conflict and a raised insert,
                        # and naming the first would report that the slug is
                        # occupied -- the one fact slug negotiation exists to
                        # keep out of responses (#143 invariant 7). Saying it
                        # for the raised case would also assert a cause never
                        # checked. Both are distinguished in the server log.
                        else "re-registration was attempted (its graph anchor "
                        "is confirmed present) but was not confirmed to land "
                        "-- see the server log; "
                        if anchor_confirmed
                        else "NOT re-registered because its graph anchor could "
                        "not be confirmed present, and "
                    )
                    + "this call did not ingest -- retry pack_ingest separately."
                ),
            )

        if row["owner_id"] != subject_id:
            # Someone else now owns this pack_id row -- never write into
            # it, never demote it (it may be their perfectly healthy pack),
            # never ingest.
            return _pack_error(
                ctx, slug, subject_id,
                f"pack '{slug}' is no longer owned by this caller after "
                f"the anchor write; refusing to finalize or ingest.",
                disclose_status=False,
            )

        if row["status"] != PACK_STATUS_READY:
            # Still ours but not 'ready', and WHICH not-ready decides the
            # action -- the two get here for opposite reasons.
            #
            # 'creating' and ours is the state mark_pack_ready's own WHERE
            # matches, so it cannot be a rejection: the attempt above raised
            # before landing. Nobody else has said anything about this pack,
            # and the anchor is confirmed in graph, so the repair pass will
            # promote it on its next run. Demoting it here would trade that
            # self-healing row for a 'partial' one, which repair deliberately
            # never touches -- turning a transient registry blip into work
            # that waits for a human. Leave it and say so.
            #
            # 'partial' means some other actor -- a repair-pass demotion, an
            # operator -- reached a conclusion with information this call
            # does not have. Design v4 §9 rejected self-promotion here on
            # purpose, so that conclusion is confirmed rather than overruled.
            if row["status"] != PACK_STATUS_CREATING:
                try:
                    mark_pack_partial(ctx["sql"], slug, subject_id)
                except Exception:
                    pass
                detail = (
                    f"was moved to status={row['status']!r} by another "
                    f"process before this call could finalize it"
                )
            else:
                detail = (
                    "could not be marked ready (the registry write did not "
                    "go through); its graph anchor is confirmed, so "
                    "'packs repair-registry' will promote it"
                )
            return _pack_error(
                ctx, slug, subject_id,
                f"pack '{slug}' {detail}; ingest skipped.",
            )

        # row["status"] == "ready" and it is ours: a repair pass (or an
        # operator's --promote) already promoted it between our anchor
        # write and this check. Join the success path exactly as if
        # mark_pack_ready had returned True itself.

    # The pack is 'ready' here, either because mark_pack_ready just
    # transitioned it or because the reconciliation above found it already
    # there. Anything left in anchor_stores is optional-store-only --
    # filtered to exclude any stale "graph:" entry from an earlier ambiguous
    # attempt the re-probe above has since confirmed actually landed.
    anchor_optional_failures = [
        f for f in store_write_failures(anchor_stores or {}) if not f.startswith("graph:")
    ]

    source_id: str | None = None
    if text:
        digest = hashlib.sha1(
            (_clean_str(title) + _clean_str(text)).encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        source_id = f"{slug}:doc:{digest}"

    ingest_result = _ingest_into_pack(
        slug,
        nodes=nodes,
        edges=edges,
        text=text,
        source_id=source_id,
        metadata={"title": _clean_str(title), "source": "pack_create"},
        text_as_node=text_as_node,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )

    result = {
        "status": "ok",
        "pack_id": slug,
        "title": _clean_str(title),
        "anchor_node": anchor_id,
        **ingest_result,
    }
    if anchor_optional_failures:
        # ingest_result's own "status" (spread above) may already be "ok" —
        # force "partial" and surface the anchor's optional-store failures
        # the same way node_errors/edge_errors do, so callers see the pack
        # was created but not every store has the anchor.
        result["status"] = "partial"
        result["anchor_errors"] = anchor_optional_failures
    return result


@tool(
    "pack_ingest",
    {
        "description": (
            "Add content into an EXISTING localcrab ontology pack. "
            "Caller supplies pre-extracted nodes/edges and/or raw text; the server does NOT call any LLM. "
            "Fails if the pack does not exist — use pack_create first.\n\n"
            + _NINE_SPACE_HINT
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pack_id": {
                    "type": "string",
                    "description": "Existing pack_id to add content into.",
                },
                "nodes": {
                    "type": "array",
                    "description": "Pre-extracted ontology nodes to add.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "space": {"type": "string"},
                            "node_type": {"type": "string"},
                            "node_id": {"type": "string"},
                            "properties": {"type": "object"},
                        },
                        "required": ["space", "node_type", "node_id"],
                    },
                },
                "edges": {
                    "type": "array",
                    "description": "Pre-extracted ontology edges to add.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_space": {"type": "string"},
                            "from_id": {"type": "string"},
                            "relation": {"type": "string"},
                            "to_space": {"type": "string"},
                            "to_id": {"type": "string"},
                            "properties": {"type": "object"},
                        },
                        "required": ["from_space", "from_id", "relation", "to_space", "to_id"],
                    },
                },
                "text": {
                    "type": "string",
                    "description": "Optional raw text. Materialised as a 9-space evidence/TextUnit graph node by default (text_as_node=true). Use to append conversation content to a loaded pack.",
                },
                "text_as_node": {
                    "type": "boolean",
                    "default": True,
                    "description": "When true (default), text is stored as an evidence/TextUnit graph node (grammar-compliant, pack_id-tagged, graph+doc+vector). Set false for legacy vector-only embedding.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional document title (stored as metadata).",
                },
                "source_id": {
                    "type": "string",
                    "description": "Optional stable source identifier for the text document. Auto-generated from title+text hash if omitted.",
                },
            },
            "required": ["pack_id"],
        },
    },
    order=14,
    access=AccessTier.WRITE,
    writes=True,
)
def pack_ingest(
    pack_id: str,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    text: str | None = None,
    title: str | None = None,
    source_id: str | None = None,
    text_as_node: bool = True,
) -> dict[str, Any]:
    """
    Add content into an EXISTING localcrab ontology pack.

    Caller supplies pre-extracted nodes/edges; the server does NOT call any LLM.
    Optional text is materialised as a 9-space evidence/TextUnit graph node
    (text_as_node=True, default) so it becomes a grammar-compliant first-class
    node. Set text_as_node=False for legacy vector-only embedding.
    Fails if the pack does not exist — use pack_create first.
    The ``ingest`` billing event's subject is the caller's server-derived
    ``current_principal()`` (#145) -- never a client argument; tenant_id
    stays fixed at 'default'.

    #146: existence is no longer checked via a ``content_pack_list()`` scan
    (that read path is scoped to what the caller can WRITE-then-READ, but
    graph.list_packs() membership was never ownership -- any reader in the
    readable set could write into a pack they don't own). ``assert_writable``
    is the same registry-authority check ``pack_publish`` already uses:
    owner-only, with the two-exception existence-leak contract (#143
    invariant 7) unchanged.

    **Behaviour change**: an authenticated caller could previously ingest
    into anyone else's PUBLIC pack (only readable-set membership was
    checked, not ownership). Now only the owner can write -- #143 invariant
    4 ("쓰기는 소유자만") enforced here for the first time, not a
    regression.
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_str, _get_context
    from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError, assert_writable

    principal = current_principal()
    tenant_id = "default"
    subject_id = principal.user_id
    pack_id = _clean_str(pack_id)

    ctx = _get_context()
    graph = ctx["neo4j"]
    # Reject before any store write is attempted -- if the graph (system of
    # record for pack content) is down, there is nothing safe to write.
    if not getattr(graph, "available", False):
        return {"error": "graph store unavailable"}

    try:
        assert_writable(ctx["sql"], principal, pack_id)
    except PackNotFoundError:
        # #143 invariant 7: identical response for "doesn't exist at all"
        # and "exists but it's someone else's private pack" -- existence of
        # a private pack must not be observable to non-owners.
        return {
            "error": "pack not found; use pack_create first",
            "pack_id": pack_id,
        }
    except PackForbiddenError:
        # A visible (public) pack owned by someone else -- existence is
        # already observable (content_pack_list), so this can safely say
        # more than "not found".
        return {
            "error": "PACK_NOT_WRITABLE: not the pack owner",
            "pack_id": pack_id,
            # No fork tool exists yet (PR #177 review round 7): naming one
            # here sent callers straight into an unknown-tool error. Point at
            # the workflow that actually works today -- this pack stays
            # READABLE (it is public), so a caller can query it and build
            # their own pack from what they need.
            "hint": (
                "this pack is readable but not writable by you; "
                "create your own with pack_create and ingest into that"
            ),
        }

    if not (nodes or edges or text):
        return {
            "error": "no content provided: supply at least one of nodes, edges, or text"
        }

    sid = source_id
    if text and not sid:
        digest = hashlib.sha1(
            (_clean_str(title or "") + _clean_str(text)).encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:12]
        sid = f"{pack_id}:doc:{digest}"

    ingest_result = _ingest_into_pack(
        pack_id,
        nodes=nodes,
        edges=edges,
        text=text,
        source_id=sid,
        metadata={"title": _clean_str(title or ""), "source": "pack_ingest"},
        text_as_node=text_as_node,
        tenant_id=tenant_id,
        subject_id=subject_id,
    )

    return {"status": "ok", "pack_id": pack_id, **ingest_result}


@tool(
    "pack_publish",
    {
        "description": (
            "Set a content pack's visibility. Owner-only. `visibility` is one of: "
            "private (default — only the owner can see or use it), "
            "public-read (anyone can read/query it), or "
            "public-fork (same read access as public-read today; it additionally "
            "RECORDS that you permit forking. The fork tool itself is not "
            "available yet -- it is planned in issue #148, so until then this "
            "value expresses intent and grants nothing beyond public-read)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pack_id": {
                    "type": "string",
                    "description": "Pack to change visibility for.",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["private", "public-read", "public-fork"],
                    "description": "New visibility.",
                },
            },
            "required": ["pack_id", "visibility"],
        },
    },
    order=16,
    # WRITE, not ADMIN: publishing is an owner-scoped operation on the
    # caller's own pack (assert_writable gates it) -- remote principals
    # must be able to publish what they own (#150's tier semantics).
    access=AccessTier.WRITE,
    writes=True,
)
def pack_publish(pack_id: str, visibility: str) -> dict[str, Any]:
    """
    Set a pack's visibility (#146). Owner-only — the caller's server-derived
    ``current_principal()`` (#145) must own ``pack_id``, never a client
    argument.

    #143 invariant 7 (existence must not leak): a pack_id that doesn't
    exist at all, and a private pack owned by someone else, both return the
    identical "pack not found" error — indistinguishable to the caller. A
    pack that IS visible (public-read/public-fork) but owned by someone
    else returns a distinct "not the pack owner" error instead, since that
    pack's existence is already observable to anyone (e.g. via
    content_pack_list).

    ``public-fork`` vs ``public-read`` (PR #177 review round 7): today these
    grant the SAME access. There is no fork tool -- ``packs.forked_from``
    exists as a column and ``VISIBILITIES`` carries the value, but nothing
    reads it to copy a pack. ``public-fork`` therefore records the owner's
    INTENT to allow forking, to be honoured once the fork tool lands (planned
    in issue #148); it does not currently do anything a caller can observe
    beyond public-read.

    The value STAYS in ``VISIBILITIES`` -- it is part of the parent auth
    design's data model, not a dead enum to be pruned. What round 7 found was
    the WORDING: the old description (and the "not the pack owner" hint)
    advertised a fork tool that does not exist, sending callers into an
    unknown-tool error. Do not reintroduce a fork promise here or in any
    response string until that tool is actually registered.
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_str, _get_context
    from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError
    from opencrab.pack.ownership import set_visibility as _set_visibility

    ctx = _get_context()
    principal = current_principal()
    pack_id = _clean_str(pack_id)
    visibility = _clean_str(visibility)

    try:
        pack = _set_visibility(ctx["sql"], principal, pack_id, visibility)
    except ValueError as exc:
        return {"error": str(exc)}
    except PackNotFoundError:
        return {"error": "pack not found", "pack_id": pack_id}
    except PackForbiddenError:
        return {
            "error": "not the pack owner",
            "pack_id": pack_id,
            # No fork tool exists yet (PR #177 review round 7): naming one
            # here sent callers straight into an unknown-tool error. Point at
            # the workflow that actually works today -- this pack stays
            # READABLE (it is public), so a caller can query it and build
            # their own pack from what they need.
            "hint": (
                "this pack is readable but not writable by you; "
                "create your own with pack_create and ingest into that"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"pack_publish failed: {exc}"}

    return {"status": "ok", "pack_id": pack["pack_id"], "visibility": pack["visibility"]}
