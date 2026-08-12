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

from opencrab.common.text import slugify

from ._registry import AccessTier, tool

logger = logging.getLogger(__name__)


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
            props = dict(_clean_meta(item.get("properties") or {}))
            props["pack_id"] = pack_id
            node_result = ctx["builder"].add_node(
                space=_clean_str(item.get("space", "")),
                node_type=_clean_str(item.get("node_type", "")),
                node_id=_clean_str(item.get("node_id", "")),
                properties=props,
                subject_id=subject_id,
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
            props = dict(_clean_meta(item.get("properties") or {}))
            props["pack_id"] = pack_id
            edge_result = ctx["builder"].add_edge(
                from_space=_clean_str(item.get("from_space", "")),
                from_id=_clean_str(item.get("from_id", "")),
                relation=_clean_str(item.get("relation", "")),
                to_space=_clean_str(item.get("to_space", "")),
                to_id=_clean_str(item.get("to_id", "")),
                properties=props,
                subject_id=subject_id,
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
        meta["pack_id"] = pack_id
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
                    subject_id=subject_id,
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
        else:
            # Legacy path: vector-only embedding + doc_sources record.
            try:
                vector_result = ctx["hybrid"].ingest(
                    text=text, source_id=source_id, metadata=meta
                )
                stores.update(vector_result.get("stores", {}))
            except Exception as exc:
                stores["chromadb"] = f"error: {exc}"
            if ctx["mongo"].available:
                try:
                    ctx["mongo"].upsert_source(source_id, text, meta)
                    stores["mongodb"] = "ok"
                except Exception as exc:
                    stores["mongodb"] = f"error: {exc}"
            else:
                stores["mongodb"] = "unavailable"

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
    # chromadb/mongodb actually came back a recognized "ok"-prefixed status
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
    # mongodb). pack_create/pack_ingest both build their response as
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

    #146: the registry (``opencrab.packs.registry.list_packs_for`` — the
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
    from opencrab.packs.registry import list_packs_for

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
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_str, _get_context
    from opencrab.ontology.builder import store_write_failures, store_write_succeeded
    from opencrab.packs.registry import create_pack as _register_pack
    from opencrab.packs.registry import delete_pack_row

    principal = current_principal()
    tenant_id = "default"
    subject_id = principal.user_id
    slug = _clean_str(pack_id) if pack_id else _slugify(title)
    if not slug:
        return {"error": "Could not derive a valid pack_id from title."}

    ctx = _get_context()

    # #146: the registry (packs table), not a full content_pack_list() scan,
    # is now the single authority for "is this slug taken". A collision is
    # NEVER reported as an error (#143 invariant 7 — that would tell the
    # caller someone else already owns the exact slug they guessed): the
    # registry quietly appends a random suffix instead, so `slug` below may
    # differ from the caller's requested pack_id. The actually-assigned
    # pack_id is always in the response.
    try:
        slug = _register_pack(
            ctx["sql"],
            owner_id=subject_id,
            pack_id=slug,
            title=_clean_str(title),
            description=_clean_str(description or ""),
        )
    except Exception as exc:
        return {"error": f"pack registration failed: {exc}"}

    anchor_node_id = f"dataset:{slug}"
    anchor_result: dict[str, Any] | None = None
    anchor_exc: Exception | None = None
    try:
        anchor_result = ctx["builder"].add_node(
            space="resource",
            node_type="Dataset",
            node_id=anchor_node_id,
            properties={
                "pack_id": slug,
                "title": _clean_str(title),
                "description": _clean_str(description or ""),
                "created_by": "localcrab-mcp",
            },
            subject_id=subject_id,
        )
    except Exception as exc:
        anchor_exc = exc

    # Same per-store inspection as _ingest_into_pack: add_node doesn't raise
    # for a per-store failure, it reports "error: ..."/"unavailable" (graph)
    # inside the returned stores map. graph is the system of record (the
    # "anchor missing = no pack" contract), so the positive
    # store_write_succeeded() check is the single source of truth for
    # whether the anchor actually landed -- not just "no error reported".
    anchor_stores = anchor_result.get("stores") if isinstance(anchor_result, dict) else None
    graph_landed = anchor_exc is None and store_write_succeeded(anchor_stores or {}, "graph")

    if not graph_landed:
        # The registry row created above now points at an anchor that
        # doesn't exist -- a phantom pack. Compensate by deleting that row
        # (#146 follow-up #170: this undoes ONLY the registry insert, never
        # any store the anchor write itself may have partially landed in;
        # once graph.add_node has actually succeeded this branch is
        # unreachable, so there is nothing to undo past this point).
        if anchor_exc is not None:
            error_msg = f"anchor node failed: {anchor_exc}"
        else:
            graph_failures = [
                f for f in store_write_failures(anchor_stores or {}) if f.startswith("graph:")
            ]
            error_msg = "anchor node failed: " + (
                "; ".join(graph_failures) or "graph write did not succeed"
            )
        try:
            deleted = delete_pack_row(ctx["sql"], slug, subject_id)
            if not deleted:
                logger.warning(
                    "pack_create compensating delete found no matching row "
                    "(pack_id=%s owner=%s)",
                    slug, subject_id,
                )
        except Exception as del_exc:
            logger.warning(
                "pack_create compensating delete failed (pack_id=%s owner=%s): %s",
                slug, subject_id, del_exc,
            )
        return {"error": error_msg}

    # Graph landed -> the pack really exists. Anything left here is
    # optional-store-only (graph already confirmed above).
    anchor_optional_failures = store_write_failures(anchor_stores or {})

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
        "anchor_node": anchor_node_id,
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
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_str, content_pack_list

    principal = current_principal()
    tenant_id = "default"
    subject_id = principal.user_id
    pack_id = _clean_str(pack_id)

    # NO arguments: pack_id must match an existing pack EXACTLY. Passing a
    # query/limit here would narrow the candidate set and reject packs that
    # really exist — the contract is exact match, never fuzzy resolution.
    existing = content_pack_list()
    existing_ids = {p["pack_id"] for p in existing.get("packs", [])}
    if pack_id not in existing_ids:
        return {
            "error": "pack not found; use pack_create first",
            "pack_id": pack_id,
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
            "public-fork (anyone can read/query it and fork it into their own pack)."
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
    """
    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _clean_str, _get_context
    from opencrab.packs.registry import PackForbiddenError, PackNotFoundError
    from opencrab.packs.registry import set_visibility as _set_visibility

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
            "hint": "use pack_fork to copy this pack into your own",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"pack_publish failed: {exc}"}

    return {"status": "ok", "pack_id": pack["pack_id"], "visibility": pack["visibility"]}
