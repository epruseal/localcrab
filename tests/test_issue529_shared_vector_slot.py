from __future__ import annotations

import json
import sqlite3

from opencrab.pack import load as pack_load
from tests.test_pack_load import _node, _write_jsonl, live, pack_sql  # noqa: F401


class CollisionVec:
    """One id slot with full-replace semantics for synthetic collision tests."""

    available = True
    _table = "vectors"

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE TABLE vectors (node_id TEXT, pack_id TEXT, metadata TEXT)")
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def get_by_id(self, ident):
        row = self.rows.get(ident)
        if row is None:
            return None
        return {"document": row["text"], "metadata": dict(row["metadata"])}

    def update_metadata(self, ident, metadata):
        if ident not in self.rows:
            return False
        self.rows[ident]["metadata"] = dict(metadata)
        return True

    def upsert_texts(self, texts, metadatas=None, ids=None):
        for text, meta, ident in zip(texts, metadatas or [], ids or []):
            self.rows[ident] = {
                "text": text,
                "metadata": dict(meta),
                "embedding": f"embed:{text}",
            }
            self.calls.append((ident, text, dict(meta)))
            self._conn.execute("DELETE FROM vectors WHERE node_id=?", (ident,))
            self._conn.execute(
                "INSERT INTO vectors (node_id, pack_id, metadata) VALUES (?, ?, ?)",
                (ident, meta.get("pack_id", ""), json.dumps(meta)),
            )
        self._conn.commit()


def _chunk_row(ident: str):
    return {
        "id": ident,
        "text": "authoritative chunk body",
        "document_id": ident,
        "metadata": {
            "category": "category-a",
            "char_start": 1,
            "char_end": 9,
            "evidence_index": 2,
            "kind": "chunk",
            "source_key": "source-a",
            "text_sha256": "digest-a",
        },
    }


def _seed(live, tmp_path, pack_sql):
    builder0, graph, docs = live
    vec = CollisionVec()
    builder = type(builder0)(graph, docs, builder0._sql, vec)
    nodes = _write_jsonl(
        tmp_path / "nodes.jsonl",
        [_node(id="shared-id", label="node body", properties={"degree": 1})],
    )
    chunks = _write_jsonl(tmp_path / "chunks.jsonl", [_chunk_row("shared-id")])
    assert pack_load.load_nodes("pack-1", nodes, builder, {}) == (1, 0, 0)
    assert pack_load.load_chunks("pack-1", chunks, vec, docs, sql=pack_sql) == (1, 0)
    before = dict(vec.rows["shared-id"])
    return builder, graph, docs, vec, nodes, chunks, before


def _increment_nodes(builder, graph, docs, vec, nodes, pack_sql):
    state = pack_load.live_pack_state("pack-1", graph, docs, vec)
    return pack_load.load_nodes_incremental(
        "pack-1", nodes, builder, {}, state["nodes"], graph, docs,
        state["doc_node_spaces"], vec=vec, sql=pack_sql,
    )


def _increment_chunks(graph, docs, vec, chunks, pack_sql):
    state = pack_load.live_pack_state("pack-1", graph, docs, vec)
    return pack_load.load_chunks_incremental(
        "pack-1", chunks, vec, docs, state["chunks"], sql=pack_sql,
    )


def test_shared_degree_change_preserves_chunk_vector(
        live, tmp_path, pack_sql):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    changed_nodes = _write_jsonl(
        tmp_path / "nodes-changed.jsonl",
        [_node(id="shared-id", label="node body", properties={"degree": 2})],
    )

    node_stats = _increment_nodes(builder, graph, docs, vec, changed_nodes, pack_sql)
    after_node = dict(vec.rows["shared-id"])
    chunk_stats = _increment_chunks(graph, docs, vec, chunks, pack_sql)

    assert node_stats[:5] == (0, 1, 0, 0, 0)
    assert after_node == before
    assert chunk_stats[:5] == (0, 0, 0, 1, 0)
    assert vec.rows["shared-id"] == before


def test_shared_node_text_change_preserves_chunk_vector(live, tmp_path, pack_sql):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    changed_nodes = _write_jsonl(
        tmp_path / "nodes-changed.jsonl",
        [_node(id="shared-id", label="changed node body", properties={"degree": 1})],
    )

    node_stats = _increment_nodes(builder, graph, docs, vec, changed_nodes, pack_sql)
    after_node = dict(vec.rows["shared-id"])
    chunk_stats = _increment_chunks(graph, docs, vec, chunks, pack_sql)

    assert node_stats[:5] == (0, 1, 0, 0, 0)
    assert after_node == before
    assert chunk_stats[:5] == (0, 0, 0, 1, 0)
    assert vec.rows["shared-id"] == before


def test_node_only_id_keeps_vector_update(live, tmp_path, pack_sql):
    builder0, graph, docs = live
    vec = CollisionVec()
    builder = type(builder0)(graph, docs, builder0._sql, vec)
    first = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="node-only", label="old", properties={"degree": 1})])
    assert pack_load.load_nodes("pack-1", first, builder, {}) == (1, 0, 0)
    before = dict(vec.rows["node-only"])
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="node-only", label="new", properties={"degree": 2})])
    res = _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    assert res[:5] == (0, 1, 0, 0, 0)
    assert vec.rows["node-only"] != before
    assert vec.rows["node-only"]["text"] != before["text"]


def test_legacy_source_fallback_protects_the_shared_slot(live, tmp_path, pack_sql):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    docs._conn.execute("UPDATE doc_sources SET metadata=json_remove(metadata, '$.pack_id') WHERE source_id='shared-id'")
    docs._conn.commit()
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="shared-id", properties={"degree": 2})])
    res = _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    assert res[:5] == (0, 1, 0, 0, 0)
    assert vec.rows["shared-id"] == before


def test_source_lookup_error_stops_before_node_write(live, tmp_path, pack_sql, monkeypatch):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="shared-id", properties={"degree": 2})])
    writes = []
    real = builder.add_node
    def record(*args, **kwargs):
        writes.append(args[2])
        return real(*args, **kwargs)
    def broken(_):
        raise RuntimeError("source lookup failed")
    monkeypatch.setattr(builder, "add_node", record)
    monkeypatch.setattr(docs, "get_source", broken)
    import pytest
    with pytest.raises(RuntimeError, match="source lookup failed"):
        _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    assert writes == []
    assert vec.rows["shared-id"] == before


def test_changed_chunk_text_replaces_after_protected_node(live, tmp_path, pack_sql):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    changed_node = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="shared-id", properties={"degree": 2})])
    _increment_nodes(builder, graph, docs, vec, changed_node, pack_sql)
    changed_chunk = _write_jsonl(tmp_path / "chunks-changed.jsonl", [{**_chunk_row("shared-id"), "text": "changed chunk body"}])
    stats = _increment_chunks(graph, docs, vec, changed_chunk, pack_sql)
    assert stats[:5] == (0, 1, 0, 0, 0)
    assert vec.rows["shared-id"]["text"] == "changed chunk body"
    assert vec.rows["shared-id"]["metadata"]["category"] == "category-a"


def test_source_owner_value_matrix_uses_the_doc_predicate_rules(live):
    builder, graph, docs = live
    cases = [
        ({"pack_id": "pack-1"}, True),
        ({"pack_id": "other", "source": "pack-1"}, False),
        ({"source": "pack-1"}, True),
        ({"pack_id": None, "source": "pack-1"}, True),
        ({"pack_id": False, "source": "pack-1"}, True),
        ({"pack_id": 0, "source": "pack-1"}, True),
        ({"pack_id": 0.0, "source": "pack-1"}, True),
        ({"pack_id": "", "source": "pack-1"}, True),
        ({"pack_id": "0", "source": "pack-1"}, False),
        ({"pack_id": True, "source": "pack-1"}, False),
        ({"pack_id": 1, "source": "pack-1"}, False),
        ({"pack_id": [], "source": "pack-1"}, False),
        ({"pack_id": {}, "source": "pack-1"}, False),
    ]
    for i, (metadata, want) in enumerate(cases):
        docs.upsert_source(f"s{i}", "text", metadata)
        assert pack_load._same_pack_source(docs, f"s{i}", "pack-1") is want


def test_missing_source_api_stops_before_node_write(live, tmp_path, pack_sql, monkeypatch):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="shared-id", properties={"degree": 2})])
    writes = []
    real = builder.add_node
    def record(*args, **kwargs):
        writes.append(args[2])
        return real(*args, **kwargs)
    monkeypatch.setattr(builder, "add_node", record)
    monkeypatch.setattr(docs, "get_source", None)
    import pytest
    with pytest.raises(RuntimeError, match="cannot read a source"):
        _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    assert writes == []


def test_missing_shared_vector_recovers_in_chunk_phase(live, tmp_path, pack_sql):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    vec.rows.pop("shared-id")
    vec._conn.execute("DELETE FROM vectors WHERE node_id='shared-id'")
    vec._conn.commit()
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="shared-id", properties={"degree": 2})])
    _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    assert "shared-id" not in vec.rows
    stats = _increment_chunks(graph, docs, vec, chunks, pack_sql)
    assert stats[:5] == (0, 1, 0, 0, 0)
    assert vec.rows["shared-id"] == before


def test_changed_chunk_metadata_updates_after_protected_node(live, tmp_path, pack_sql):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="shared-id", properties={"degree": 2})])
    _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    row = _chunk_row("shared-id")
    row["metadata"]["theme"] = "new"
    changed_chunk = _write_jsonl(tmp_path / "chunks-meta.jsonl", [row])
    stats = _increment_chunks(graph, docs, vec, changed_chunk, pack_sql)
    assert stats[:5] == (0, 0, 1, 0, 0)
    assert vec.rows["shared-id"]["text"] == before["text"]
    assert vec.rows["shared-id"]["embedding"] == before["embedding"]
    assert vec.rows["shared-id"]["metadata"]["theme"] == "new"


def test_malformed_source_result_stops_before_node_write(live, tmp_path, pack_sql, monkeypatch):
    builder, graph, docs, vec, nodes, chunks, before = _seed(live, tmp_path, pack_sql)
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="shared-id", properties={"degree": 2})])
    calls = []
    monkeypatch.setattr(builder, "add_node", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(docs, "get_source", lambda _id: {"metadata": []})
    import pytest
    with pytest.raises(RuntimeError, match="metadata is malformed"):
        _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    assert calls == []


def test_foreign_source_does_not_suppress_node_vector(live, tmp_path, pack_sql):
    builder0, graph, docs = live
    vec = CollisionVec()
    builder = type(builder0)(graph, docs, builder0._sql, vec)
    nodes = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="node-only", label="old", properties={"degree": 1})])
    assert pack_load.load_nodes("pack-1", nodes, builder, {}) == (1, 0, 0)
    docs.upsert_source("node-only", "foreign source", {"pack_id": "pack-b"})
    before = dict(vec.rows["node-only"])
    changed = _write_jsonl(tmp_path / "nodes-changed.jsonl", [_node(id="node-only", label="new", properties={"degree": 2})])
    res = _increment_nodes(builder, graph, docs, vec, changed, pack_sql)
    assert res[:5] == (0, 1, 0, 0, 0)
    assert vec.rows["node-only"] != before
