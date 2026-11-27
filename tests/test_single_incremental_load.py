"""단일 SQLite 모드의 팩 증분 적재 (#719).

지원 스키마의 합성 파일 DB에서 노드, 엣지, 청크와 마감을 실행한다.
같은 입력의 다섯 파일 모드와 반환값 및 논리 행을 대조한다.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import struct
from collections import Counter
from pathlib import Path

import pytest
import sqlite_vec

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import create_pack
from opencrab.stores._single_file import SharedSqliteFile
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore
from opencrab.stores.sqlite_vec_store import SqliteVecStore

DIM = 4
PACK = "p1"
OTHER = "p2"
_INCARNATION = {PACK: "a" * 32, OTHER: "b" * 32}


def _embed(texts):
    return [[float(len(t) % 5), 1.0, 0.0, 0.5] for t in texts]


class _NoopLock:
    pass


class _Env:
    """layout이 five면 다섯 파일, single이면 `pack_delete_operations` 표가 있는 한 파일이다."""

    def __init__(self, layout: str, d: Path, monkeypatch) -> None:
        self.layout = layout
        self.d = d
        d.mkdir()
        monkeypatch.setenv("LOCAL_DATA_DIR", str(d))
        if layout == "single":
            from scripts.probe_single_db_crash import _supported_single_fixture

            self.path = _supported_single_fixture(d, dim=DIM)
            self.shared = SharedSqliteFile(str(self.path))
            kw = {"shared": self.shared}
            graph_path = doc_path = vec_path = sql_path = self.path
        else:
            self.shared = None
            kw = {}
            graph_path, doc_path = d / "graph.db", d / "doc_store.db"
            vec_path, sql_path = d / "vectors.db", d / "opencrab.db"
        self.graph = LocalGraphStore(str(graph_path), **kw)
        self.docs = LocalSQLDocStore(str(doc_path), **kw)
        self.vec = SqliteVecStore(str(vec_path), embedding_function=_embed, dim=DIM, **kw)
        self.sql = SQLStore(f"sqlite:///{sql_path}", **kw)
        import uuid

        with monkeypatch.context() as identity:
            identity.setattr("opencrab.auth.uuid.uuid4", lambda: uuid.UUID("12345678-1234-4234-8234-123456789abc"))
            self.user_id = create_user(self.sql, "owner", is_local=True)
        for pack in (PACK, OTHER):
            create_pack(self.sql, self.user_id, pack)
        self.builder = OntologyBuilder(self.graph, self.docs, self.sql, vec=self.vec)
        self.paths = {"graph": graph_path, "doc": doc_path, "vec": vec_path, "registry": sql_path}
        if self.shared is not None:
            from opencrab.stores.sql_store import shared_write_unit

            with shared_write_unit(self.sql):
                self.shared.validate_pack_writes({OTHER: self.shared.pack_incarnation(OTHER)})
                self.shared._uow.requested_pack = OTHER
                self._seed_other_pack()
        else:
            self._seed_other_pack()
        _other_rows(self)

    def _seed_other_pack(self) -> None:
        """두 번째 팩의 행은 첫 팩의 증분 적재가 바꾸지 못한다."""
        for node_id in ("o1", "o2"):
            props = {"pack_id": OTHER, "owner_id": self.user_id, "name": node_id}
            self.graph.upsert_node("Document", node_id, props, "resource")
            self.docs.upsert_node_doc("resource", "Document", node_id, props)
            self.sql.register_node("resource", "Document", node_id)
        self.graph.upsert_edge("Document", "o1", "cites", "Document", "o2", {"pack_id": OTHER})
        self.sql.register_edge("resource", "o1", "cites", "resource", "o2")
        self.docs.upsert_source("oc1", "other chunk", {"pack_id": OTHER})
        self.vec.upsert_texts(["other chunk"], [{"pack_id": OTHER}], ["oc1"])
        assert self.graph.get_edge("Document", "o1", "cites", "Document", "o2") is not None

    def close(self) -> None:
        for store in (self.vec, self.docs, self.graph, self.sql):
            store.close()

    @property
    def principal(self) -> Principal:
        return Principal(user_id=self.user_id, is_local=True, disabled=False)

    def run(self, nodes, edges, chunks, work: Path, *, force_delete=False) -> dict:
        """증분 한 회: 노드, 엣지, 청크, 마감 순서로 적재하고 반환값을 모은다."""
        work.mkdir(exist_ok=True)
        nf = _write_jsonl(work / "nodes.jsonl", nodes)
        ef = _write_jsonl(work / "edges.jsonl", edges)
        cf = _write_jsonl(work / "chunks.jsonl", chunks)
        audit_before = _audit_rows(self)
        other_before = _other_rows(self)
        with _round_logs() as records, principal_scope(self.principal):
            state = pack_load.live_pack_state(PACK, self.graph, self.docs, self.vec)
            id_map: dict = {}
            n_res = pack_load.load_nodes_incremental(
                PACK, nf, self.builder, id_map, state["nodes"], self.graph, self.docs,
                state["doc_node_spaces"], vec=self.vec, sql=self.sql,
            )
            applied: set = set()
            e_res = pack_load.load_edges(PACK, ef, self.builder, id_map, applied)
            c_res = pack_load.load_chunks_incremental(
                PACK, cf, self.vec, self.docs, state["chunks"], sql=self.sql,
            )
            fin = pack_load.incremental_finalize(
                PACK, self.graph, self.docs, self.vec, state,
                set(n_res[5]), set(c_res[5]), applied, force_delete, len(nodes), len(chunks), sql=self.sql,
            )
        assert e_res == (len(edges), 0, 0)
        for row in edges:
            source_type = id_map[row["source_id"]][1]
            target_type = id_map[row["target_id"]][1]
            assert self.graph.get_edge(source_type, row["source_id"], "cites",
                                       target_type, row["target_id"]) is not None
        assert (n_res[3], n_res[4], n_res[6], n_res.ingest_err, n_res.cleanup_err) == (0, 0, 0, 0, 0)
        assert (c_res[4], c_res[6]) == (0, 0)
        assert set(n_res[5]) == {row["id"] for row in nodes}
        assert set(c_res[5]) == {row["id"] for row in chunks}
        _assert_round_logs(self, records)
        assert _other_rows(self) == other_before
        assert set(audit_before).issubset(set(_audit_rows(self)))
        if n_res[0] + n_res[1] + e_res[0]:
            assert len(_audit_rows(self)) > len(audit_before)
        return {"node_errors": (n_res.ingest_err, n_res.cleanup_err),
                "nodes": _result(n_res), "edges": tuple(e_res),
                "chunks": _result(c_res), "finalize": fin}

    def snapshot(self) -> dict:
        """전 열을 비교한다. 생성 시각과 검증한 물리 키만 정규화한다."""
        out = {}
        for role, tables in (
            ("graph", ("graph_nodes", "graph_edges")),
            ("doc", ("doc_nodes", "doc_sources", "doc_source_integrity")),
            ("registry", ("ontology_nodes", "ontology_edges")),
            ("vec", ("vectors_kure",)),
        ):
            with _connection(self.paths[role]) as conn:
                for table in tables:
                    rows = _rows(conn, table)
                    if role == "registry":
                        ids = [row["id"] for row in rows]
                        assert all(isinstance(value, int) and value > 0 for value in ids)
                        assert len(ids) == len(set(ids))
                    out[table] = _canonical_rows(rows, omit={"id"} if role == "registry" else set())
                if role == "doc":
                    out.update(_derived_rows(conn))
        return out


_VOLATILE = {"created_at", "updated_at", "ingested_at", "last_seen", "timestamp", "_ts"}
_JSON_COLUMNS = {"properties", "metadata", "source_integrity"}


@contextlib.contextmanager
def _connection(path):
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn, table):
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def _canonical_rows(rows, *, omit=frozenset(), times=True):
    return sorted((tuple((key, _props(value) if key in _JSON_COLUMNS and times else value)
                         for key, value in row.items()
                         if key not in omit and not (times and key in _VOLATILE))
                   for row in rows), key=repr)


def _result(value):
    if isinstance(value, dict):
        return {key: _result(item) if isinstance(item, (tuple, set, dict)) else item
                for key, item in value.items()}
    if isinstance(value, set):
        return tuple(sorted(value))
    return tuple(_result(item) if isinstance(item, (tuple, set, dict)) else item for item in value)


@contextlib.contextmanager
def _round_logs():
    records = []
    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)
    handler = Capture(logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield records
    finally:
        root.removeHandler(handler)


def _assert_round_logs(env, records):
    for record in records:
        if not record.name.startswith("opencrab."):
            continue
        message = record.getMessage()
        if "reason=confirmed_node_cascade" in message:
            assert env.shared is not None
            assert record.args[-1] == "confirmed_node_cascade"
            _, source, relation, target, _ = record.args
            with _connection(env.paths["graph"]) as conn:
                assert conn.execute("SELECT 1 FROM graph_edges WHERE from_id=? AND relation=? AND to_id=?",
                                    (source, relation, target)).fetchone() is None
                assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id IN (?,?)",
                                    (source, target)).fetchone()[0] < 2
            continue
        # 이 경고는 실패가 아니라 실제 문서 복구를 알린다.
        if message.startswith(("doc 행 유실 회수(", "doc 행 값 어긋남 회수(")):
            _, node_id, space = record.args
            assert env.docs.get_node_doc(space, node_id)
            continue
        pytest.fail(f"정상 회차에서 예상하지 않은 경고: {message}")


def _derived_rows(conn, *, raw=False, other=False):
    from opencrab.stores import _node_bm25_sqlite as bm25

    result = {}
    mappings = _rows(conn, bm25.DOCS)
    keys = {row["doc_key"]: (row["space"], row["node_id"]) for row in mappings}
    assert len(keys) == len(mappings)
    source_content = _rows(conn, "doc_sources_fts_content")
    sources = {row["id"]: row["c0"] for row in source_content}
    assert len(sources) == len(source_content)
    assert len(set(sources.values())) == len(sources)
    base_sources = {row["source_id"]: row["text"] for row in _rows(conn, "doc_sources")}
    assert {row["c0"]: row["c1"] for row in source_content} == base_sources
    conn.execute("CREATE VIRTUAL TABLE temp.comparison_source_vinst "
                 "USING fts5vocab(main, doc_sources_fts, instance)")
    for table, physical, links in (
        (bm25.FTS, "rowid", keys), (bm25.FTS + "_docsize", "id", keys),
        (bm25.VINST, "doc", keys), ("doc_sources_fts", "rowid", sources),
        ("doc_sources_fts_content", "id", sources),
        ("doc_sources_fts_docsize", "id", sources), ("comparison_source_vinst", "doc", sources),
    ):
        if physical == "rowid":
            cursor = conn.execute(f'SELECT rowid, * FROM "{table}"')
            rows = [dict(zip([item[0] for item in cursor.description], row)) for row in cursor]
        else:
            rows = _rows(conn, table)
        assert all(row[physical] in links for row in rows), (table, rows, links)
        if physical != "doc":
            assert {row[physical] for row in rows} == set(links), table
        if other:
            rows = [row for row in rows if links[row[physical]] in {("resource", "o1"),
                                                                   ("resource", "o2"), "oc1"}]
        if not raw:
            rows = [dict(row, **{physical: links[row[physical]]}) for row in rows]
        result[table] = _canonical_rows(rows, times=not raw)
    if other:
        mappings = [row for row in mappings if row["pack_id"] == OTHER]
    if not raw:
        mappings = [dict(row, doc_key=keys[row["doc_key"]]) for row in mappings]
    result[bm25.DOCS] = _canonical_rows(mappings, times=not raw)
    pending = _rows(conn, bm25.PENDING)
    if other:
        pending = [row for row in pending if row["node_id"] in {"o1", "o2"}]
    result[bm25.PENDING] = _canonical_rows(pending, times=not raw)
    if not other:
        for table in (bm25.STATS, bm25.META, bm25.VROW):
            result[table] = _canonical_rows(_rows(conn, table), times=not raw)
        meta = dict(conn.execute(f"SELECT key,value FROM {bm25.META}"))
        max_chars = int(meta["max_text_chars"] or 0) or None
        documents = _rows(conn, "doc_nodes")
        expected = {}
        for row in documents:
            node = dict(row, properties=json.loads(row["properties"]))
            expected[(row["space"], row["node_id"])] = bm25.node_terms(node, max_chars)
        assert set(expected) == set(keys.values())
        assert conn.execute(f"SELECT doc_count,total_len FROM {bm25.STATS} WHERE id=1").fetchone() == (
            len(documents), sum(len(terms) for terms in expected.values()))
        for row in _rows(conn, bm25.DOCS):
            assert row["dl"] == len(expected[keys[row["doc_key"]]])
        postings = [(keys[doc], term, column, offset) for term, doc, column, offset in
                    conn.execute(f"SELECT * FROM {bm25.VINST}")]
        assert Counter(postings) == Counter((key, term, "tokens", offset)
                                           for key, terms in expected.items()
                                           for offset, term in enumerate(terms))
    return result


def _props(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k not in _VOLATILE}
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def _node(nid, **kw):
    row = {"id": nid, "label": f"노드 {nid}", "node_type": "Document", "space": "resource"}
    row.update(kw)
    return row


def _edge(eid, src, tgt):
    return {"id": eid, "source_id": src, "target_id": tgt, "label": "CITES"}


def _chunk(i, text=None):
    return {"id": f"c{i}", "document_id": "n1", "text": text or f"본문 {i}"}


# 요청 팩은 첫 회 입력 전에 비어 있다. 다른 팩의 seed는 먼저 존재한다.
_FIRST = (
    [_node(f"n{i}") for i in range(1, 9)],
    [_edge("e1", "n1", "n2"), _edge("e2", "n2", "n3"), _edge("e3", "n4", "n5")],
    [_chunk(i) for i in range(1, 9)],
)
_SECOND = (
    [
        _node("n1", label="바뀐 이름"),                                  # chg
        _node("n2", node_type="Concept", space="concept"),               # 타입과 space 변경
        _node("n4"),                                                     # same
        _node("n5"), _node("n6"), _node("n7"), _node("n8"),
        _node("n9"),                                                     # new
    ],                                                                    # n3은 입력에서 빠진다
    [_edge("e3", "n4", "n5"), _edge("e4", "n5", "n6")],                  # e1과 e2는 빠지고 e4가 새로 든다
    [_chunk(i) for i in range(2, 9)] + [_chunk(1, "바뀐 본문")],            # c1 텍스트 변경
)


def _drive(layout: str, base: Path, monkeypatch, steps) -> tuple[list[dict], list[dict], _Env]:
    env = _Env(layout, base / layout, monkeypatch)
    results, snaps = [], []
    for index, (nodes, edges, chunks) in enumerate(steps):
        results.append(env.run(nodes, edges, chunks, base / layout / f"work{index}"))
        snaps.append(env.snapshot())
    return results, snaps, env


def test_single_incremental_matches_five_file(tmp_path, monkeypatch):
    """같은 입력의 증분 적재 결과는 다섯 파일 모드와 같고, 다른 팩의 행은 그대로다."""
    steps = [_FIRST, _SECOND, _SECOND]
    five_res, five_snap, five = _drive("five", tmp_path, monkeypatch, steps)
    try:
        single_res, single_snap, single = _drive("single", tmp_path, monkeypatch, steps)
        try:
            assert single.shared is not None
            marker = single.shared.connection().execute(
                "SELECT 1 FROM sqlite_master WHERE name='pack_delete_operations'").fetchone()
            assert marker is not None
            assert single_res == five_res
            for step, (a, b) in enumerate(zip(five_snap, single_snap)):
                assert a == b, f"step {step}: 저장소 상태가 다섯 파일 모드와 다르다"
            assert _other_rows(single)["graph_nodes"], "다른 팩의 행이 남아 있어야 한다"
        finally:
            single.close()
    finally:
        five.close()


def _seed_write(env, operation):
    if env.shared is None:
        return operation()
    from opencrab.stores.sql_store import shared_write_unit

    with shared_write_unit(env.sql):
        env.shared.validate_pack_writes({PACK: env.shared.pack_incarnation(PACK)})
        env.shared._uow.requested_pack = PACK
        return operation()


def _other_rows(env):
    result = {}
    for role, tables in (("graph", ("graph_nodes", "graph_edges")),
                         ("doc", ("doc_nodes", "doc_sources", "doc_source_integrity")),
                         ("registry", ("ontology_nodes", "ontology_edges")),
                         ("vec", ("vectors_kure",))):
        with _connection(env.paths[role]) as conn:
            for table in tables:
                rows = [row for row in _rows(conn, table)
                        if any(value in {"o1", "o2", "oc1"}
                               for value in row.values() if isinstance(value, str))]
                assert rows, f"다른 팩 seed가 없다: {table}"
                for row in rows:
                    for column in ("properties", "metadata"):
                        if column in row:
                            assert json.loads(row[column])["pack_id"] == OTHER
                result[table] = _canonical_rows(rows, times=False)
            if role == "doc":
                result.update(_derived_rows(conn, raw=True, other=True))
    assert len(result["graph_nodes"]) == len(result["ontology_nodes"]) == 2
    assert len(result["graph_edges"]) == len(result["ontology_edges"]) == 1
    assert len(result["doc_nodes_bm25_docs"]) == 2
    assert result["doc_sources_fts_content"]
    assert result["comparison_source_vinst"]
    return result


def test_shared_id_orphans_anchor_and_final_same_rounds_match(tmp_path, monkeypatch):
    outcomes = []
    for layout in ("five", "single"):
        env = _Env(layout, tmp_path / layout, monkeypatch)
        try:
            _seed_write(env, lambda: env.graph.upsert_node(
                "Document", "anchor", {"pack_id": PACK, "created_by": "title-backfill"}, "resource"))
            before_other = _other_rows(env)
            rounds = []
            calls = []
            round_calls = []
            round_states = []
            def embedding(texts):
                calls.append(tuple(texts))
                return _embed(texts)
            env.vec._ef = embedding
            for index, inputs in enumerate((_FIRST, _SECOND, _SECOND, _SECOND)):
                calls.clear()
                nodes, edges, chunks = inputs
                chunks = [*chunks, {"id": "n1", "document_id": "n1", "text": "shared authoritative text"}]
                rounds.append(env.run(nodes, edges, chunks, env.d / f"run{index}"))
                round_calls.append(tuple(calls))
                round_states.append(env.snapshot())
                flattened = [text for batch in calls for text in batch]
                assert flattened.count("shared authoritative text") == (1 if index == 0 else 0)
                from opencrab.ontology.bm25 import _node_text

                embedded_nodes = nodes if index == 0 else [row for row in nodes if row["id"] in {"n2", "n9"}] if index == 1 else []
                embedded_chunks = chunks if index == 0 else [row for row in chunks if row["id"] == "c1"] if index == 1 else []
                expected_texts = [_node_text(env.docs.get_node_doc(row["space"], row["id"]))
                                  for row in embedded_nodes] + [row["text"] for row in embedded_chunks]
                assert Counter(flattened) == Counter(expected_texts)
                with _connection(env.paths["vec"]) as conn:
                    row = conn.execute("SELECT * FROM vectors_kure WHERE node_id='n1'")
                    stored = dict(zip([column[0] for column in row.description], row.fetchone()))
                assert stored["document"] == "shared authoritative text"
                assert stored["pack_id"] == PACK
                expected_metadata = {"pack_id": PACK, "source": PACK, "document_id": "n1", "space": "evidence"}
                assert json.loads(stored["metadata"]) == expected_metadata
                assert env.docs.get_source("n1")["metadata"] == expected_metadata
                assert struct.unpack("4f", stored["embedding"]) == pytest.approx(
                    _embed(["shared authoritative text"])[0])
                assert _other_rows(env) == before_other
                assert env.graph.get_node("Document", "anchor") is not None
                shared = env.vec.get_by_id("n1")
                assert shared["document"] == "shared authoritative text"
                assert shared["metadata"]["pack_id"] == PACK
                if index == 1:
                    assert env.graph.get_node("Document", "n3") is None
                    _seed_write(env, lambda: env.docs.upsert_node_doc(
                        "resource", "Document", "doc-orphan", {"pack_id": PACK}))
                    _seed_write(env, lambda: env.vec.upsert_texts(
                        ["orphan"], [{"pack_id": PACK}], ["vec-orphan"]))
                if index == 2:
                    assert env.vec.get_by_id("vec-orphan") is None
                    assert not env.docs.get_node_doc("resource", "doc-orphan")
                    before_same = env.snapshot()
            assert env.snapshot() == before_same
            assert rounds[-1]["nodes"][2] == len(_SECOND[0])
            assert rounds[-1]["node_errors"] == (0, 0)
            assert rounds[-1]["chunks"][3] == len(_SECOND[2]) + 1
            outcomes.append((rounds, round_states, round_calls))
        finally:
            env.close()
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("failure", ["getter", "validator_recursion", "decoder_depth"])
def test_single_source_errors_preserve_exception_and_full_id(tmp_path, monkeypatch, caplog, failure):
    from opencrab.common.graph_identity import PropertyDecodeDepthError

    env = _Env("single", tmp_path / "single", monkeypatch)
    full_id = "source-with-complete-identifier"
    original = env.docs.get_source
    error = {"getter": RuntimeError("getter failed"),
             "validator_recursion": RecursionError("validation failed"),
             "decoder_depth": PropertyDecodeDepthError("decoder depth")}[failure]
    def getter(source_id):
        if source_id == full_id:
            raise error
        return original(source_id)
    monkeypatch.setattr(env.docs, "get_source", getter)
    path = _write_jsonl(env.d / "nodes.jsonl", [_node(full_id), _node("next")])
    try:
        with principal_scope(env.principal):
            if failure == "decoder_depth":
                result = pack_load.load_nodes_incremental(PACK, path, env.builder, {}, {},
                    env.graph, env.docs, {}, vec=env.vec, sql=env.sql)
                assert result.ingest_err == 1 and result[0] == 1
                assert env.graph.get_node("Document", "next") is not None
            else:
                with pytest.raises(type(error)) as caught:
                    pack_load.load_nodes_incremental(PACK, path, env.builder, {}, {},
                        env.graph, env.docs, {}, vec=env.vec, sql=env.sql)
                assert caught.value is error
                assert env.graph.get_node("Document", "next") is None
        assert full_id in caplog.text
    finally:
        env.close()


@pytest.mark.parametrize("failure", ["vector", "source"])
def test_finalize_retries_only_failed_ids_and_preserves_other_pack(tmp_path, monkeypatch, caplog, failure):
    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        env.run(*_FIRST, env.d / "initial")
        with principal_scope(env.principal):
            live = pack_load.live_pack_state(PACK, env.graph, env.docs, env.vec)
        other = _other_rows(env)
        bad = "c3"
        calls = []
        if failure == "vector":
            original = env.vec.delete
            def delete(ids):
                calls.append(tuple(ids))
                if bad in ids:
                    raise RuntimeError("vector failure")
                return original(ids)
            monkeypatch.setattr(env.vec, "delete", delete)
        else:
            original = env.docs.delete_sources_with_state
            def delete(ids):
                calls.append(tuple(ids))
                if bad in ids:
                    raise RuntimeError("source failure")
                return original(ids)
            monkeypatch.setattr(env.docs, "delete_sources_with_state", delete)
        with principal_scope(env.principal):
            result = pack_load.incremental_finalize(PACK, env.graph, env.docs, env.vec, live,
                set(live["nodes"]), {"c1", "c2", "c4", "c5", "c6"}, live["edges"], True, 8, 5, sql=env.sql)
        assert result["chunk_del"] == 2
        assert env.docs.get_source(bad) is not None
        assert env.docs.get_source("c7") is env.docs.get_source("c8") is None
        assert _other_rows(env) == other
        assert bad in caplog.text
        assert (bad,) in calls
        if failure == "vector":
            assert calls.count((bad,)) == 2
        assert any(len(batch) > 1 for batch in calls)
    finally:
        env.close()


@pytest.mark.parametrize("kind", ["node", "finalize"])
@pytest.mark.parametrize("active", ["uow", "raw"])
def test_single_node_and_finalize_reject_outer_transaction(tmp_path, monkeypatch, kind, active):
    import contextlib

    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        path = _write_jsonl(env.d / "nodes.jsonl", [])
        live = {"nodes": {}, "chunks": {}, "edges": set(), "vec_ids": set(), "doc_node_spaces": {}}
        if active == "raw":
            env.shared.connection().execute("BEGIN")
        manager = env.shared.unit_of_work() if active == "uow" else contextlib.nullcontext()
        with principal_scope(env.principal), manager:
            with pytest.raises(RuntimeError, match="cannot run inside"):
                if kind == "node":
                    pack_load.load_nodes_incremental(PACK, path, env.builder, {}, {}, env.graph,
                                                     env.docs, {}, vec=env.vec, sql=env.sql)
                else:
                    pack_load.incremental_finalize(PACK, env.graph, env.docs, env.vec, live,
                        set(), set(), set(), False, 0, 0, sql=env.sql)
    finally:
        env.shared.connection().rollback()
        env.close()


_DELETE_SITES = {
    "node_space": ("docs", "delete_node_doc"),
    "node_type": ("graph", "delete_node"),
    "node_old_doc": ("docs", "delete_node_doc"),
    "node_sweep": ("graph", "delete_node"),
    "final_doc": ("docs", "delete_node_doc"),
    "final_orphan": ("vec", "delete"),
    "final_chunk_vec": ("vec", "delete"),
    "final_node": ("graph", "delete_node"),
    "final_source": ("docs", "delete_sources_with_state"),
    "final_edge": ("graph", "delete_edge"),
}


def _patch_write_unit(patches, manager):
    from opencrab.ontology import builder
    from opencrab.stores import sql_store

    patches.setattr(sql_store, "shared_write_unit", manager)
    patches.setattr(builder, "shared_write_unit", manager)


def _observe_authorization(env, patches, unit):
    from opencrab.pack import ownership

    original = ownership.assert_writable
    authorized = []
    def observe(sql, principal, pack_id, **kwargs):
        assert sql is env.sql
        result = original(sql, principal, pack_id, **kwargs)
        active = env.shared is not None and env.shared.in_unit_of_work
        authorized.append((unit[0], principal, pack_id, active,
                           env.shared.connection().in_transaction if active else False))
        return result
    patches.setattr(ownership, "assert_writable", observe)
    return authorized


def _assert_authorized(env, authorized, unit):
    assert env.shared.in_unit_of_work
    assert env.shared._uow.validated_packs == {PACK: env.shared.pack_incarnation(PACK)}
    assert env.shared._uow.requested_pack == PACK
    assert (unit[0], env.principal, PACK, True, True) in authorized


def _observe_deletes(env, patches, trace, unit):
    authorized = _observe_authorization(env, patches, unit)
    for store_name, method in set(_DELETE_SITES.values()):
        store = getattr(env, store_name)
        original = getattr(store, method)
        def observe(*args, _original=original, _store=store_name, _method=method, **kwargs):
            identity = (_store, _method, repr(args), repr(sorted(kwargs.items())))
            trace.append((unit[0], identity))
            if env.shared is not None:
                _assert_authorized(env, authorized, unit)
            return _original(*args, **kwargs)
        patches.setattr(store, method, observe)


def _target_delete(trace, site):
    method = _DELETE_SITES[site]
    choices = [(unit, identity) for unit, identity in trace if identity[:2] == method]
    index = 1 if site == "final_chunk_vec" else 0
    assert len(choices) > index, (site, trace)
    return choices[index]


def _deletion_setup(env, monkeypatch, site):
    env.run(*_FIRST, env.d / "initial")
    if site == "node_space":
        _seed_write(env, lambda: env.docs.upsert_node_doc("concept", "Document", "n1", {"pack_id": PACK}))
    with principal_scope(env.principal):
        state = pack_load.live_pack_state(PACK, env.graph, env.docs, env.vec)
    if site.startswith("node_"):
        rows = [_node("n1", node_type="Concept", space="concept")] if site in {
            "node_type", "node_old_doc"} else [_node("n1")]
        path = _write_jsonl(env.d / "nodes.jsonl", rows)
        def run():
            current = pack_load.live_pack_state(PACK, env.graph, env.docs, env.vec)
            with monkeypatch.context() as scoped:
                if site == "node_sweep":
                    scoped.setattr(pack_load, "_dup_type_node_rows",
                                   lambda *_: {"n1": {"Document", "Concept"}})
                return pack_load.load_nodes_incremental(PACK, path, env.builder, {}, current["nodes"],
                    env.graph, env.docs, current["doc_node_spaces"], vec=env.vec, sql=env.sql)
        return run
    node_ids = set(state["nodes"]) - {"n3"}
    chunk_ids = set(state["chunks"]) - {"c3"}
    # The vector stages each have one batch. Their occurrence order identifies them.
    def run():
        return pack_load.incremental_finalize(PACK, env.graph, env.docs, env.vec, state,
            node_ids, chunk_ids, {("n4", "cites", "n5")}, True, len(node_ids), len(chunk_ids), sql=env.sql)
    return run


@pytest.mark.parametrize("site", list(_DELETE_SITES))
@pytest.mark.parametrize("phase", ["rollback", "start", "cleanup"])
def test_each_public_delete_has_independent_authorized_outcome(tmp_path, monkeypatch, site, phase):
    import contextlib

    from opencrab.stores import sql_store

    original = sql_store.shared_write_unit
    control = _Env("single", tmp_path / "control", monkeypatch)
    trace = []
    unit = [0]
    @contextlib.contextmanager
    def counted(sql, *, outcome=None, **kwargs):
        unit[0] += 1
        with original(sql, outcome=outcome, **kwargs) as conn:
            yield conn
    try:
        run = _deletion_setup(control, monkeypatch, site)
        with monkeypatch.context() as spy:
            _patch_write_unit(spy, counted)
            _observe_deletes(control, spy, trace, unit)
            with principal_scope(control.principal):
                run()
        target_unit, target_identity = _target_delete(trace, site)
    finally:
        control.close()
    env = _Env("single", tmp_path / "injected", monkeypatch)
    try:
        run = _deletion_setup(env, monkeypatch, site)
        observed = []
        unit[0] = 0
        failure = RuntimeError(f"injected {phase}")
        other_before = _other_rows(env)
        targeted = []
        @contextlib.contextmanager
        def injected(sql, *, outcome=None, **kwargs):
            unit[0] += 1
            selected = unit[0] == target_unit
            if selected:
                targeted.append(unit[0])
                assert observed == [row for row in trace if row[0] < target_unit]
                if phase == "start":
                    raise failure
            try:
                with original(sql, outcome=outcome, **kwargs) as conn:
                    yield conn
                    if selected:
                        assert observed[-1] == (target_unit, target_identity)
                        if phase == "rollback":
                            raise failure
            finally:
                if selected and phase == "cleanup":
                    outcome["cleanup_error"] = failure
                    raise failure
        with monkeypatch.context() as spy:
            _patch_write_unit(spy, injected)
            _observe_deletes(env, spy, observed, unit)
            with principal_scope(env.principal):
                if phase == "rollback":
                    result = run()
                    if site.startswith("node_"):
                        assert result.cleanup_err >= 1
                else:
                    expected = RuntimeError if phase == "start" else pack_load.LoaderWriteCleanupError
                    with pytest.raises(expected) as caught:
                        run()
                    if phase == "start":
                        assert caught.value is failure
                        assert all(identity != target_identity for _, identity in observed)
                    else:
                        assert caught.value.outcome["state"] == "committed"
                        assert caught.value.cleanup_error is failure
            assert targeted == [target_unit]
            if phase != "start":
                assert (target_unit, target_identity) in observed
            assert _other_rows(env) == other_before
            assert not env.shared.in_unit_of_work
            assert not env.shared.connection().in_transaction
    finally:
        env.close()


@pytest.mark.parametrize("site", list(_DELETE_SITES))
def test_delete_prefix_interrupt_matches_modes_and_reexecution_recovers(tmp_path, monkeypatch, site):
    import contextlib

    from opencrab.stores import sql_store

    prefix_states = []
    final_states = []
    prefixes = []
    rerun_results = []
    final_results = []
    for layout in ("five", "single"):
        env = _Env(layout, tmp_path / layout, monkeypatch)
        try:
            run = _deletion_setup(env, monkeypatch, site)
            audit_before = _audit_rows(env)
            trace = []
            unit = [0]
            original = sql_store.shared_write_unit
            @contextlib.contextmanager
            def counted(sql, *, outcome=None, **kwargs):
                unit[0] += 1
                with original(sql, outcome=outcome, **kwargs) as conn:
                    yield conn
            before = env.snapshot()
            # The dry control uses the same initial state on a separate environment.
            control = _Env(layout, tmp_path / (layout + "-control"), monkeypatch)
            try:
                control_run = _deletion_setup(control, monkeypatch, site)
                with monkeypatch.context() as spy:
                    _patch_write_unit(spy, counted)
                    _observe_deletes(control, spy, trace, unit)
                    with principal_scope(control.principal):
                        control_run()
                target_unit, target_identity = _target_delete(trace, site)
            finally:
                control.close()
            assert env.snapshot() == before
            monkeypatch.setenv("LOCAL_DATA_DIR", str(env.d))
            unit[0] = 0
            fired = []
            store_name, method = _DELETE_SITES[site]
            public = None
            method_calls = 0
            completed = []
            observed = []
            interruption = KeyboardInterrupt("prefix interruption")
            target_index = sum(identity[:2] == (store_name, method) for _, identity in trace[:trace.index((target_unit, target_identity))])
            def interrupted(*args, **kwargs):
                nonlocal method_calls
                identity = (store_name, method, repr(args), repr(sorted(kwargs.items())))
                selected = method_calls == target_index
                method_calls += 1
                if selected and not fired:
                    assert identity == target_identity
                    fired.append(True)
                    raise interruption
                return public(*args, **kwargs)
            with monkeypatch.context() as spy:
                _patch_write_unit(spy, counted)
                _observe_deletes(env, spy, observed, unit)
                for observed_store, observed_method in set(_DELETE_SITES.values()):
                    store = getattr(env, observed_store)
                    original_public = getattr(store, observed_method)
                    def committed(*args, _original=original_public, _store=observed_store,
                                  _method=observed_method, **kwargs):
                        value = _original(*args, **kwargs)
                        completed.append((_store, _method, repr(args), repr(sorted(kwargs.items()))))
                        return value
                    spy.setattr(store, observed_method, committed)
                public = getattr(getattr(env, store_name), method)
                spy.setattr(getattr(env, store_name), method, interrupted)
                with principal_scope(env.principal), pytest.raises(KeyboardInterrupt) as caught:
                    run()
                assert caught.value is interruption
            assert fired == [True]
            expected_prefix = [identity for _, identity in trace[:trace.index((target_unit, target_identity))]]
            assert completed == expected_prefix
            assert [identity for _, identity in observed] == expected_prefix
            assert method_calls == target_index + 1
            prefixes.append(completed)
            prefix_states.append(env.snapshot())
            with principal_scope(env.principal):
                rerun = run()
            rerun_results.append((_result(rerun),
                                  getattr(rerun, "ingest_err", None),
                                  getattr(rerun, "cleanup_err", None)))
            final_results.append(env.run(*_SECOND, env.d / "resume"))
            assert set(audit_before).issubset(set(_audit_rows(env)))
            assert len(_audit_rows(env)) > len(audit_before)
            final_states.append(env.snapshot())
        finally:
            env.close()
    assert prefixes[0] == prefixes[1]
    assert prefix_states[0] == prefix_states[1]
    assert rerun_results[0] == rerun_results[1]
    assert final_results[0] == final_results[1]
    assert final_states[0] == final_states[1]
    uninterrupted = _Env("single", tmp_path / "uninterrupted", monkeypatch)
    try:
        uninterrupted_run = _deletion_setup(uninterrupted, monkeypatch, site)
        with principal_scope(uninterrupted.principal):
            uninterrupted_run()
        uninterrupted.run(*_SECOND, uninterrupted.d / "changed")
        assert final_states[1] == uninterrupted.snapshot()
    finally:
        uninterrupted.close()


@pytest.mark.parametrize("boundary", [*_DELETE_SITES, "builder", "chunk"])
def test_same_generation_owner_change_rejects_next_unit(tmp_path, monkeypatch, boundary):
    from opencrab.pack import ownership
    from opencrab.stores import sql_store

    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        replacement = create_user(env.sql, "replacement", is_local=False)
        original_unit = sql_store.shared_write_unit
        unit = [0]
        trace = []
        if boundary in _DELETE_SITES:
            run = _deletion_setup(env, monkeypatch, boundary)
            @contextlib.contextmanager
            def counted(sql, **kwargs):
                unit[0] += 1
                with original_unit(sql, **kwargs) as conn:
                    yield conn
            # 정상 control은 별도 DB에 같은 입력을 넣는다.
            control = _Env("single", tmp_path / "control", monkeypatch)
            try:
                control_run = _deletion_setup(control, monkeypatch, boundary)
                with monkeypatch.context() as spy:
                    _patch_write_unit(spy, counted)
                    _observe_deletes(control, spy, trace, unit)
                    with principal_scope(control.principal):
                        control_run()
                target_unit, _ = _target_delete(trace, boundary)
            finally:
                control.close()
            monkeypatch.setenv("LOCAL_DATA_DIR", str(env.d))
        else:
            path = _write_jsonl(env.d / "input.jsonl",
                                [_node("new-node")] if boundary == "builder" else [_chunk("new")])
            if boundary == "builder":
                def run():
                    return pack_load.load_nodes_incremental(PACK, path, env.builder, {}, {},
                        env.graph, env.docs, {}, vec=env.vec, sql=env.sql)
            else:
                def run():
                    return pack_load.load_chunks_incremental(PACK, path, env.vec, env.docs, {}, sql=env.sql)
            target_unit = None
        before = env.snapshot()
        audit_before = _audit_rows(env)
        incarnation = env.shared.pack_incarnation(PACK)
        changed = []
        denied = []
        at_revoke = []
        original_authorize = ownership.assert_writable
        def authorize(sql, principal, pack_id, **kwargs):
            try:
                return original_authorize(sql, principal, pack_id, **kwargs)
            except ownership.PackNotFoundError:
                denied.append((principal, pack_id, env.shared.in_unit_of_work,
                               env.shared._uow.requested_pack,
                               dict(env.shared._uow.validated_packs)))
                raise
        def revoke():
            at_revoke.append((env.snapshot(), _audit_rows(env)))
            assert not env.shared.in_unit_of_work
            with sqlite3.connect(env.paths["registry"]) as conn:
                assert conn.execute("UPDATE packs SET owner_id=? WHERE pack_id=?",
                                    (replacement, PACK)).rowcount == 1
            assert env.shared.pack_incarnation(PACK) == incarnation
            changed.append(True)
        @contextlib.contextmanager
        def injected(sql, **kwargs):
            unit[0] += 1
            if unit[0] == target_unit:
                revoke()
            with original_unit(sql, **kwargs) as conn:
                yield conn
        original_embedding = env.vec._ef
        def embedding(texts):
            result = original_embedding(texts)
            if not changed:
                revoke()
            return result
        unit[0] = 0
        with monkeypatch.context() as spy:
            spy.setattr(ownership, "assert_writable", authorize)
            _patch_write_unit(spy, injected)
            if target_unit is None:
                spy.setattr(env.vec, "_ef", embedding)
            with principal_scope(env.principal):
                try:
                    run()
                except ownership.PackNotFoundError:
                    pass
        assert changed == [True]
        assert denied
        assert (env.principal, PACK, True, PACK, {PACK: incarnation}) in denied
        assert all(row[:2] == (env.principal, PACK) for row in denied)
        assert env.shared.pack_incarnation(PACK) == incarnation
        # 삭제 전 선행 단위는 커밋될 수 있다. 거부 뒤에는 기반 행과 감사가 바뀌지 않는다.
        assert (env.snapshot(), _audit_rows(env)) == at_revoke[0]
        if target_unit is None:
            assert env.snapshot() == before
            assert _audit_rows(env) == audit_before
        assert _other_rows(env)
        assert not env.shared.in_unit_of_work
        assert not env.shared.connection().in_transaction
    finally:
        env.close()


def test_builder_and_chunk_writes_follow_real_authorization(tmp_path, monkeypatch):
    from opencrab.stores import sql_store

    env = _Env("single", tmp_path / "single", monkeypatch)
    unit = [0]
    writes = []
    original = sql_store.shared_write_unit
    @contextlib.contextmanager
    def counted(sql, **kwargs):
        unit[0] += 1
        with original(sql, **kwargs) as conn:
            yield conn
    try:
        with monkeypatch.context() as spy:
            _patch_write_unit(spy, counted)
            authorized = _observe_authorization(env, spy, unit)
            for store, method in ((env.graph, "upsert_node"), (env.graph, "upsert_edge"),
                                  (env.docs, "upsert_node_doc"), (env.docs, "upsert_source"),
                                  (env.vec, "upsert_precomputed"), (env.sql, "register_node"),
                                  (env.sql, "register_edge")):
                original_write = getattr(store, method)
                def observe(*args, _original=original_write, _method=method, **kwargs):
                    _assert_authorized(env, authorized, unit)
                    writes.append((_method, unit[0]))
                    return _original(*args, **kwargs)
                spy.setattr(store, method, observe)
            env.run(*_FIRST, env.d / "first")
        assert {name for name, _ in writes} == {"upsert_node", "upsert_edge", "upsert_node_doc",
                                               "upsert_source", "upsert_precomputed",
                                               "register_node", "register_edge"}
    finally:
        env.close()


@pytest.mark.parametrize("table,column,value", [
    ("graph_nodes", "space_id", "changed-space"),
    ("graph_edges", "from_type", "ChangedType"),
    ("graph_edges", "to_type", "ChangedType"),
    ("doc_source_integrity", "source_integrity", '{"state":"changed"}'),
    ("ontology_edges", "from_space", "changed-space"),
    ("vectors_kure", "pack_id", "changed-pack"),
    ("graph_nodes", "owner_id", "changed-owner"),
])
def test_snapshot_detects_semantic_column_change(tmp_path, monkeypatch, table, column, value):
    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        env.run(*_FIRST, env.d / "initial")
        before = env.snapshot()
        with _connection(env.path) as conn:
            if column == "owner_id":
                raw = conn.execute("SELECT properties FROM graph_nodes WHERE node_id='n1'").fetchone()[0]
                props = json.loads(raw)
                assert props["owner_id"] == env.user_id
                props["owner_id"] = value
                conn.execute("UPDATE graph_nodes SET properties=? WHERE node_id='n1'", (json.dumps(props),))
            elif table == "vectors_kure":
                cursor = conn.execute('SELECT * FROM vectors_kure WHERE node_id=?', ("c1",))
                columns = [item[0] for item in cursor.description]
                row = dict(zip(columns, cursor.fetchone()))
                row[column] = value
                assert conn.execute('DELETE FROM vectors_kure WHERE node_id=?', ("c1",)).rowcount == 1
                names = ','.join('"' + name + '"' for name in columns)
                placeholders = ','.join('?' for _ in columns)
                conn.execute(f'INSERT INTO vectors_kure ({names}) VALUES ({placeholders})',
                             tuple(row[name] for name in columns))
            else:
                conn.execute(f'UPDATE "{table}" SET "{column}"=?', (value,))
            conn.commit()
        assert env.snapshot() != before
    finally:
        env.close()


def test_snapshot_rejects_mapping_and_stats_corrupted_together(tmp_path, monkeypatch):
    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        env.run(*_FIRST, env.d / "initial")
        with _connection(env.path) as conn:
            assert conn.execute("UPDATE doc_nodes_bm25_docs SET dl=dl+1 WHERE node_id='n1'").rowcount == 1
            assert conn.execute("UPDATE doc_nodes_bm25_stats SET total_len=total_len+1 WHERE id=1").rowcount == 1
            conn.commit()
        with pytest.raises(AssertionError):
            env.snapshot()
    finally:
        env.close()


def test_result_comparison_preserves_returned_ids_and_named_errors():
    original = pack_load.NodeIncrementalResult((1, 0, 0, 0, 0, {"first"}, 0), ingest_err=0, cleanup_err=0)
    changed = pack_load.NodeIncrementalResult((1, 0, 0, 0, 0, {"other"}, 0), ingest_err=0, cleanup_err=0)
    assert _result(original) != _result(changed)
    changed_errors = pack_load.NodeIncrementalResult(tuple(original), ingest_err=1, cleanup_err=1)
    assert (original.ingest_err, original.cleanup_err) != (changed_errors.ingest_err, changed_errors.cleanup_err)


def _audit_rows(env):
    with sqlite3.connect(env.paths["doc"]) as conn:
        return conn.execute("SELECT * FROM audit_log ORDER BY event_id").fetchall()


def test_existing_audit_and_live_foreign_registry_survive_old_key_registration(tmp_path, monkeypatch):
    from sqlalchemy import text

    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        env.run(*_FIRST, env.d / "initial")
        existing = _audit_rows(env)
        assert existing
        # The legacy registry has no owner column. A graph row supplies the live owner check.
        from opencrab.stores.sql_store import shared_write_unit
        with shared_write_unit(env.sql):
            env.shared.validate_pack_writes({OTHER: env.shared.pack_incarnation(OTHER)})
            env.shared._uow.requested_pack = OTHER
            env.sql.register_node("resource", "Document", "o1")
        foreign = _other_rows(env)
        with env.sql._engine.begin() as conn:
            conn.execute(text("INSERT INTO ontology_nodes(space,node_type,node_id) "
                              "VALUES ('resource','Concept','old-registry-key')"))
        env.run([*_SECOND[0], _node("old-registry-key")], _SECOND[1], _SECOND[2], env.d / "changed")
        after = _audit_rows(env)
        assert set(existing).issubset(set(after))
        assert len(after) > len(existing)
        assert _other_rows(env) == foreign
        with env.sql._engine.connect() as conn:
            row = conn.execute(text("SELECT node_type FROM ontology_nodes "
                                    "WHERE space='resource' AND node_id='old-registry-key'")).fetchone()
        assert row[0] == "Document"
        assert env.graph.get_node("Document", "old-registry-key") is not None
    finally:
        env.close()


@pytest.mark.parametrize("collision", [False, True])
def test_finalize_false_edge_warning_distinguishes_committed_cascade_and_owner_conflict(
        tmp_path, monkeypatch, caplog, collision):
    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        env.run(*_FIRST, env.d / "initial")
        with principal_scope(env.principal):
            live = pack_load.live_pack_state(PACK, env.graph, env.docs, env.vec)
        if collision:
            # A stale snapshot must not turn a live foreign edge into a confirmed cascade.
            with sqlite3.connect(env.paths["graph"]) as conn:
                row = conn.execute("SELECT properties FROM graph_edges WHERE from_id='n2' AND to_id='n3'").fetchone()
                props = json.loads(row[0])
                props["pack_id"] = OTHER
                conn.execute("UPDATE graph_edges SET properties=? WHERE from_id='n2' AND to_id='n3'",
                             (json.dumps(props),))
        with principal_scope(env.principal):
            pack_load.incremental_finalize(PACK, env.graph, env.docs, env.vec, live,
                set(live["nodes"]) - {"n3"}, set(live["chunks"]), {("n4", "cites", "n5")},
                True, 7, 8, sql=env.sql)
        assert "n2-cites->n3" in caplog.text
        if collision:
            assert "missing_or_owner_conflict" in caplog.text
            assert env.graph.get_node("Document", "n3") is not None
            assert env.graph.get_edge("Document", "n2", "cites", "Document", "n3") is not None
        else:
            assert "confirmed_node_cascade" in caplog.text
            assert env.graph.get_node("Document", "n3") is None
            assert env.graph.get_edge("Document", "n2", "cites", "Document", "n3") is None
    finally:
        env.close()


@pytest.mark.parametrize("failure", ["receipt", "exception"])
def test_shared_edge_failure_warning_contains_complete_key(tmp_path, monkeypatch, caplog, failure):
    env = _Env("single", tmp_path / "single", monkeypatch)
    try:
        source, target = "complete-source-identifier", "complete-target-identifier"
        path = _write_jsonl(env.d / "edges.jsonl", [_edge("e", source, target)])
        def fail(*_args, **_kwargs):
            if failure == "exception":
                raise RuntimeError("edge failure")
            return {"stores": {"graph": "error: failed"}}
        monkeypatch.setattr(env.builder, "add_edge", fail)
        with principal_scope(env.principal):
            result = pack_load.load_edges(PACK, path, env.builder,
                {source: ("resource", "Document"), target: ("resource", "Document")})
        assert result == (0, 0, 1)
        assert f"resource/{source}-cites->resource/{target}" in caplog.text
    finally:
        env.close()
