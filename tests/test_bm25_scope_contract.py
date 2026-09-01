"""Issue #256 contract: ``BM25Index.search``'s ``pack_ids`` is keyword-only and required -- an unscoped call must raise ``TypeError`` instead of silently returning an empty result."""

from __future__ import annotations

import pytest

from opencrab.ontology.bm25 import BM25Index


def _node(node_id: str, *, pack_id: str | None = None, text: str = "alpha") -> dict:
    props: dict = {"name": text}
    if pack_id is not None:
        props["pack_id"] = pack_id
    return {
        "node_id": node_id,
        "space": "claim",
        "node_type": "Claim",
        "properties": props,
    }


def _build_index() -> BM25Index:
    return BM25Index.build([
        _node("a", pack_id="A", text="alpha"),
        _node("b", pack_id="B", text="alpha"),
    ])


def test_missing_pack_ids_raises_type_error() -> None:
    index = _build_index()
    with pytest.raises(TypeError):
        index.search("alpha", limit=5)


def test_empty_pack_ids_returns_empty_scope() -> None:
    index = _build_index()
    hits = index.search("alpha", pack_ids=[], limit=5)
    assert hits == []


def test_pack_ids_scopes_to_matching_pack_only() -> None:
    index = _build_index()
    hits = index.search("alpha", pack_ids=["A"], limit=5)
    ids = [h["node_id"] for h in hits]
    assert ids == ["a"]
    assert "b" not in ids


def test_positional_pack_ids_rejected() -> None:
    index = _build_index()
    with pytest.raises(TypeError):
        index.search("alpha", None, 5, ["A"])


def test_positional_spaces_and_limit_still_work_with_keyword_pack_ids() -> None:
    index = _build_index()
    hits = index.search("alpha", None, 5, pack_ids=["A"])
    ids = [h["node_id"] for h in hits]
    assert ids == ["a"]
