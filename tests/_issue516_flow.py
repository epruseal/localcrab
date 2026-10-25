"""Real-flow fixtures for the #516 G5-G7 surface cells.

The cells control only the storage legs: the vector store's ``query`` result and
the rows of a real doc store. ``HybridQuery.query``, the real ``Reranker``, the
real fold, ``QueryResult`` conversion and every surface run unchanged. No cell
sets ``text_transformed`` or builds a ``QueryResult``.
"""

from __future__ import annotations

from typing import Any

NEEDLE = "needle"
LIMIT = 4000
RESERVED_KEYS = (
    "_query_provenance",
    "_query_provenance_invalidated",
    "_query_text_transformed",
    "_query_text_source_chars",
    "source_integrity",
    "_source_classification",
)
CELLS = [f"{kind}-{boundary}" for kind in ("concat", "strip", "mix") for boundary in ("below", "at")]


def cell_spec(cell: str) -> dict[str, Any]:
    """Return independent leg inputs and the body the real merge must produce."""
    kind, boundary = cell.split("-")
    total = LIMIT - 1 if boundary == "below" else LIMIT
    if kind == "concat":
        old = "a" * (total - 1 - len(NEEDLE))
        return {
            "vector": old, "source": NEEDLE, "expected": f"{old}\n{NEEDLE}",
            "total": total, "transformed": True,
        }
    if kind == "strip":
        count = total - 1 - len(NEEDLE)
        return {
            "vector": " " + "a" * count,
            "source": NEEDLE + " ",
            "expected": "a" * count + "\n" + NEEDLE,
            "total": total,
            "transformed": True,
        }
    # mix: both legs carry the same body, so the merge leaves the text as it is.
    body = NEEDLE + " " + "b" * (total - len(NEEDLE) - 1)
    return {
        "vector": body, "source": body, "expected": body,
        "total": total, "transformed": False,
    }


def install_vector_leg(vector_store: Any, monkeypatch: Any, pack: str, node_ids: list[str], text: str) -> None:
    """Replace only the vector store's search result with controlled leg hits."""
    hits = [
        {
            "id": node_id,
            "document": text,
            "metadata": {"pack_id": pack, "space": "evidence", "node_id": node_id},
            "distance": 0.1 + index * 0.01,
        }
        for index, node_id in enumerate(node_ids)
    ]

    def controlled_query(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{**hit, "metadata": dict(hit["metadata"])} for hit in hits]

    monkeypatch.setattr(vector_store, "query", controlled_query)


def seed_source_rows(docs: Any, pack: str, node_ids: list[str], text: str) -> None:
    """Write real source rows through the real doc store."""
    for node_id in node_ids:
        docs.upsert_source(
            node_id, text, {"pack_id": pack, "space": "evidence", "node_id": node_id}
        )


def assert_pre_surface(outcome: Any, cell: str, *, folded: bool) -> None:
    """Assert the real rerank/fold output before any surface is inspected."""
    spec = cell_spec(cell)
    assert len(outcome.results) == 1, cell
    result = outcome.results[0]
    assert result.text == spec["expected"], cell
    assert len(result.text) == spec["total"], cell
    assert (len(result.text) < LIMIT) == cell.endswith("below"), cell
    if cell.startswith("strip"):
        assert result.text == result.text.strip()
        assert spec["vector"] != result.text and spec["source"] != result.text
    assert result.text_transformed is spec["transformed"], cell
    if folded:
        assert result.folded_count == 1, cell
        member = result.folded_members[0]
        assert member.text == spec["expected"], cell
        assert member.text_transformed is spec["transformed"], cell
        assert member.node_id != result.node_id
    else:
        assert result.folded_count == 0, cell


def find_reserved(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Return the paths of reserved private keys at any depth."""
    found: list[tuple[Any, ...]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in RESERVED_KEYS:
                found.append((*path, key))
            found.extend(find_reserved(child, (*path, key)))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(find_reserved(child, (*path, index)))
    return found


def assert_public_unchanged(item: dict[str, Any], cell: str) -> None:
    """Assert one public item of an untransformed cell carries its body and no cut."""
    spec = cell_spec(cell)
    assert item["text"] == spec["expected"], cell
    assert len(item["text"]) == spec["total"], cell
    assert "truncation" not in item, cell
    assert find_reserved(item) == [], cell


def assert_public_cell(item: dict[str, Any], cell: str) -> None:
    """Assert the public item matches the cell: unavailable if transformed, else unchanged."""
    if cell_spec(cell)["transformed"]:
        assert_public_unavailable(item, cell)
    else:
        assert_public_unchanged(item, cell)


def assert_public_unavailable(item: dict[str, Any], cell: str) -> None:
    """Assert one public result item is unavailable with no locator or private key."""
    spec = cell_spec(cell)
    assert item["text"] == spec["expected"], cell
    truncation = item["truncation"]
    assert truncation["full_text_unavailable"] is True, cell
    assert "source_locator" not in truncation, cell
    assert "full_text_tool" not in truncation, cell
    assert find_reserved(item) == [], cell


# ---- #516 ledger-gap helpers (appended to tests/_issue516_flow.py) ----

def build_stack(tmp_path: Any, pack: str, backend: str, document: dict[str, Any] | None = None) -> dict[str, Any]:
    """Real query stack over one doc-store backend: local JSON, local SQL or a mocked Mongo collection."""
    from opencrab.ontology.query import HybridQuery
    from opencrab.stores.local_graph_store import LocalGraphStore
    from tests._vec_helpers import build_vector_store
    from tests.test_mcp_pack_aware import _real_sql_with_owned_pack, _stub_context

    harness = None
    if backend == "local":
        from opencrab.stores.local_doc_store import LocalDocStore

        docs = LocalDocStore(str(tmp_path / "local"))
    elif backend == "sql":
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    else:
        from tests.test_mongo_unit import _CasHarness

        harness = _CasHarness(document)
        docs = harness.store
    vec = build_vector_store("sqlite-vec", tmp_path)
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    sql = _real_sql_with_owned_pack(pack)
    hybrid = HybridQuery(vec, graph)
    hybrid._doc_store = docs
    ctx = _stub_context(hybrid, sql=sql)
    ctx["neo4j"] = graph
    ctx["mongo"] = docs
    return {
        "vec": vec, "docs": docs, "graph": graph, "sql": sql, "hybrid": hybrid,
        "ctx": ctx, "harness": harness, "backend": backend,
    }


def close_stack(stack: dict[str, Any]) -> None:
    stack["vec"].close()
    stack["graph"].close()
    close = getattr(stack["docs"], "close", None)
    if stack["backend"] != "mongo" and callable(close):
        close()
    stack["sql"]._engine.dispose()


def query_rows(stack: dict[str, Any], monkeypatch: Any, pack: str, texts: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Run the real ``HybridQuery.query`` with a vector leg that returns every row of ``texts``."""
    from opencrab.services import result_caps as caps

    hits = [
        {
            "id": source_id,
            "document": text,
            "metadata": {"pack_id": pack, "space": "evidence", "node_id": source_id},
            "distance": 0.1 + index / 100,
        }
        for index, (source_id, text) in enumerate(texts.items())
    ]
    monkeypatch.setattr(
        stack["vec"], "query",
        lambda **_kwargs: [{**hit, "metadata": dict(hit["metadata"])} for hit in hits],
    )
    outcome = stack["hybrid"].query(
        question="alpha", pack_ids=[pack], full_scope={pack},
        use_rerank=False, use_bm25=False, use_fts=False, limit=len(texts) + 2,
    )
    items = {result.node_id: caps.cap_query_item(result.to_cap_item()) for result in outcome.results}
    assert set(items) == set(texts), "every row must reach the public result"
    return items


def range_call(stack: dict[str, Any], locator: Any, length: int = 64) -> dict[str, Any]:
    from unittest.mock import patch

    from opencrab.mcp import tools

    with patch.object(tools, "_get_context", return_value=stack["ctx"]):
        return tools.ontology_get_source_range(locator, 0, length)


def hand_locator(pack: str, source_id: str, text: str) -> dict[str, str]:
    from opencrab.stores._source_integrity import strict_utf8_digest

    return {"version": "1", "source_id": source_id, "pack_id": pack, "source_digest": strict_utf8_digest(text)}


def merge_then_cap(cell: str, pack: str = "p1") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run the real ``_merge_duplicate`` and ``cap_query_item`` on one cell (no marker is set by hand)."""
    from opencrab.ontology.reranker import _merge_duplicate
    from opencrab.services.result_caps import cap_query_item

    spec = cell_spec(cell)
    proof = {"source_id": "s1", "pack_id": pack, "source_digest": "d1"}

    def hit(source: str, text: str) -> dict[str, Any]:
        return {
            "source": source, "node_id": "n1", "score": 1.0, "text": text,
            "metadata": {"pack_id": pack}, "graph_context": None, "raw_score": 1.0,
            "_query_provenance": dict(proof),
        }

    existing = hit("vector", spec["vector"])
    _merge_duplicate(existing, hit("bm25", spec["source"]))
    return spec, existing, cap_query_item(existing)


def assert_merge_cap_cell(cell: str) -> None:
    spec, merged, capped = merge_then_cap(cell)
    assert merged["text"] == spec["expected"], cell
    assert len(merged["text"]) == spec["total"], cell
    assert (len(merged["text"]) < LIMIT) == cell.endswith("below"), cell
    if cell.startswith("strip"):
        assert merged["text"] == merged["text"].strip(), cell
    assert merged.get("_query_text_transformed", False) is spec["transformed"], cell
    assert capped["text"] == spec["expected"], cell
    assert find_reserved(capped) == [], cell
    if spec["transformed"]:
        truncation = capped["truncation"]
        assert truncation["full_text_unavailable"] is True, cell
        assert "source_locator" not in truncation, cell
        assert "full_text_tool" not in truncation, cell
    else:
        # The same body from two legs is not a transformation: nothing is cut, nothing is hidden.
        assert "truncation" not in capped, cell
