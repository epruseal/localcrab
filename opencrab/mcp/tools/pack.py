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

from ._registry import tool

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
                "min_nodes": {"type": "integer", "description": "Only return packs with at least this many nodes (default 1).", "default": 1},
                "query": {"type": "string", "description": "Optional search text. When given, only packs scoring above zero are returned, ordered by relevance. Only packs actually loaded in the graph are candidates."},
                "limit": {"type": "integer", "description": "Maximum packs to return. Defaults to 10 when `query` is given, unlimited otherwise."},
            },
            "required": [],
        },
    },
    order=9,
)
def content_pack_list(
    min_nodes: int = 1,
    query: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    List content packs loaded into the localcrab ontology stores.

    Returns each pack_id with node count and a representative title
    derived from node properties (source_package_title / title / name).

    Parameters
    ----------
    min_nodes:
        Only return packs with at least this many nodes (default 1).
    query:
        Optional relevance filter. Candidates are exactly the packs present in
        the graph (a manifest with no ingested nodes is never surfaced); each
        is scored with the same deterministic scorer auto_pack uses, and packs
        scoring zero are dropped. Ordering is
        ``(score desc, node_count desc, pack_id asc)`` — fully tie-broken, so
        repeated calls return the identical order.
    limit:
        Cap on returned packs. Defaults to 10 when ``query`` is given.

    NOTE: pack_create/pack_ingest call this with NO arguments on purpose —
    their pack_id existence check is an exact membership test against the FULL
    list. Passing a query there would shrink the candidate set and reject
    packs that really exist.
    """
    from opencrab.mcp.tools import _clean_str, _get_context

    ctx = _get_context()
    graph = ctx["neo4j"]
    if not graph.available:
        return {"error": "graph store unavailable"}

    # All four backends implement list_packs() natively (Local/PG: SQL GROUP
    # BY; Kuzu/Neo4j: Cypher aggregation) — see opencrab/stores/_graph_protocol.py.
    rows = graph.list_packs(min_nodes)
    # list_packs() 반환 형식:
    # [{"pack_id": str, "node_count": int, "sample_title": str, "sample_description": str}]
    packs = []
    for r in rows:
        pid = r.get("pack_id") or ""
        title = r.get("sample_title") or ""
        display = title.replace(" ontology pack", "").replace(" ontology Pack", "").strip()
        packs.append({
            "pack_id":    pid,
            "node_count": r["node_count"],
            "title":      display or pid or "(no pack_id)",
        })

    # Whitespace-only query == no query (an empty filter must not return an
    # empty pack list). This is input normalisation, not term correction.
    query = _clean_str(query).strip() if query else ""
    if not query:
        if limit is not None and limit >= 0 and len(packs) > limit:
            return {"total": limit, "packs": packs[:limit], "truncated": True}
        return {"total": len(packs), "packs": packs}

    scanned = len(packs)
    ranked = _rank_packs(query, rows, packs)
    effective_limit = _QUERY_DEFAULT_LIMIT if limit is None else limit
    truncated = effective_limit >= 0 and len(ranked) > effective_limit
    if truncated:
        ranked = ranked[:effective_limit]
    response: dict[str, Any] = {
        "total": len(ranked),
        "query": query,
        "scanned": scanned,
        "packs": ranked,
    }
    if truncated:
        response["truncated"] = True
    return response


def _rank_packs(
    query: str,
    rows: list[dict[str, Any]],
    packs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Score graph-loaded packs against ``query``; deterministic ordering.

    ``rows`` are the raw ``list_packs()`` rows (they carry the anchor
    description that the display shape drops); ``packs`` is the parallel
    display shape built above.
    """
    from opencrab.ontology.pack_registry import PackInfo, score_pack

    extras = _manifest_extras()
    scored: list[tuple[float, int, str, dict[str, Any]]] = []
    for row, pack in zip(rows, packs, strict=True):
        pack_id = pack["pack_id"]
        keywords, tags = extras.get(pack_id, ([], []))
        info = PackInfo(
            pack_id=pack_id,
            title=row.get("sample_title") or "",
            description=row.get("sample_description") or "",
            keywords=keywords,
            tags=tags,
        )
        score, matched = score_pack(query, info)
        if score <= 0.0:
            continue
        scored.append((score, pack["node_count"], pack_id, {**pack, "score": score, "matched": matched}))

    # (score desc, node_count desc, pack_id asc) — the third key makes the
    # order total, so the same input always yields the same output.
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in scored]


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
                "tenant_id": {
                    "type": "string",
                    "description": "Tenant identifier for multi-tenant isolation (default: 'default').",
                },
                "subject_id": {
                    "type": "string",
                    "description": "Optional subject performing the write (for billing/audit).",
                },
            },
            "required": ["title"],
        },
    },
    order=13,
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
    tenant_id: str = "default",
    subject_id: str | None = None,
) -> dict[str, Any]:
    """
    Create a new localcrab ontology pack and ingest content into it.

    Caller supplies pre-extracted nodes/edges; the server does NOT call any LLM.
    pack_id is auto-slugged from title unless explicitly provided.
    Optional text is materialised as a 9-space evidence/TextUnit graph node
    (text_as_node=True, default) or embedded as a vector blob only (False).
    tenant_id/subject_id are passed through to the ``ingest`` billing event.
    """
    from opencrab.mcp.tools import _clean_str, _get_context, content_pack_list
    from opencrab.ontology.builder import store_write_failures

    slug = _clean_str(pack_id) if pack_id else _slugify(title)
    if not slug:
        return {"error": "Could not derive a valid pack_id from title."}

    # NO arguments: the duplicate check is an exact membership test and must
    # see the FULL pack list (see content_pack_list's docstring).
    existing = content_pack_list()
    existing_ids = {p["pack_id"] for p in existing.get("packs", [])}
    if slug in existing_ids:
        return {
            "error": "pack already exists",
            "pack_id": slug,
            "hint": "use pack_ingest to add more content",
        }

    ctx = _get_context()
    anchor_node_id = f"dataset:{slug}"
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
        return {"error": f"anchor node failed: {exc}"}

    # Same per-store inspection as _ingest_into_pack: add_node doesn't raise
    # for a per-store failure, it reports "error: ..."/"unavailable" (graph)
    # inside the returned stores map. store_write_failures already applies
    # the one place-to-judge rule (graph is system of record, docs/sql/vector
    # are optional) — reuse that split here instead of re-deciding it:
    #   - graph itself failed -> the pack doesn't exist in the graph, hard
    #     error (matches the "anchor missing = no pack" contract below).
    #   - only optional stores failed -> the pack DOES exist (graph write
    #     went through), so report success and surface which stores lagged,
    #     same as _ingest_into_pack's node/edge loops do via node_errors.
    anchor_stores = anchor_result.get("stores") if isinstance(anchor_result, dict) else None
    anchor_failures = store_write_failures(anchor_stores or {})
    anchor_graph_failures = [f for f in anchor_failures if f.startswith("graph:")]
    if anchor_graph_failures:
        return {"error": "anchor node failed: " + "; ".join(anchor_graph_failures)}
    # Anything left here is optional-store-only (graph already ruled out above).
    anchor_optional_failures = anchor_failures

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
                "tenant_id": {
                    "type": "string",
                    "description": "Tenant identifier for multi-tenant isolation (default: 'default').",
                },
                "subject_id": {
                    "type": "string",
                    "description": "Optional subject performing the write (for billing/audit).",
                },
            },
            "required": ["pack_id"],
        },
    },
    order=14,
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
    tenant_id: str = "default",
    subject_id: str | None = None,
) -> dict[str, Any]:
    """
    Add content into an EXISTING localcrab ontology pack.

    Caller supplies pre-extracted nodes/edges; the server does NOT call any LLM.
    Optional text is materialised as a 9-space evidence/TextUnit graph node
    (text_as_node=True, default) so it becomes a grammar-compliant first-class
    node. Set text_as_node=False for legacy vector-only embedding.
    Fails if the pack does not exist — use pack_create first.
    tenant_id/subject_id are passed through to the ``ingest`` billing event.
    """
    from opencrab.mcp.tools import _clean_str, content_pack_list

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
