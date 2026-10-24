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
        return {"vector": old, "source": NEEDLE, "expected": f"{old}\n{NEEDLE}", "total": total}
    if kind == "strip":
        count = total - 1 - len(NEEDLE)
        return {
            "vector": " " + "a" * count,
            "source": NEEDLE + " ",
            "expected": "a" * count + "\n" + NEEDLE,
            "total": total,
        }
    body = NEEDLE + " " + "b" * (total - len(NEEDLE) - 1)
    return {"vector": body, "source": body, "expected": body, "total": total}


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
    assert result.text_transformed is True, cell
    if folded:
        assert result.folded_count == 1, cell
        member = result.folded_members[0]
        assert member.text == spec["expected"], cell
        assert member.text_transformed is True, cell
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


def assert_public_unavailable(item: dict[str, Any], cell: str) -> None:
    """Assert one public result item is unavailable with no locator or private key."""
    spec = cell_spec(cell)
    assert item["text"] == spec["expected"], cell
    truncation = item["truncation"]
    assert truncation["full_text_unavailable"] is True, cell
    assert "source_locator" not in truncation, cell
    assert "full_text_tool" not in truncation, cell
    assert find_reserved(item) == [], cell
