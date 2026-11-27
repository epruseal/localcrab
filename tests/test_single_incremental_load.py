"""단일 SQLite 모드의 팩 증분 적재 (#719).

지원 스키마의 합성 파일 DB에서 노드, 엣지, 청크와 마감을 실행한다.
같은 입력의 다섯 파일 모드와 반환값 및 논리 행을 대조한다.
"""

from __future__ import annotations

import json
import sqlite3
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
        self.paths = {"graph": graph_path, "doc": doc_path, "vec": vec_path}
        if self.shared is not None:
            from opencrab.stores.sql_store import shared_write_unit

            with shared_write_unit(self.sql):
                self.shared.validate_pack_writes({OTHER: self.shared.pack_incarnation(OTHER)})
                self.shared._uow.requested_pack = OTHER
                self._seed_other_pack()
        else:
            self._seed_other_pack()

    def _seed_other_pack(self) -> None:
        """두 번째 팩의 행은 첫 팩의 증분 적재가 바꾸지 못한다."""
        self.graph.upsert_node("Document", "o1", {"pack_id": OTHER, "name": "other"}, "resource")
        self.docs.upsert_node_doc("resource", "Document", "o1", {"pack_id": OTHER, "name": "other"})
        self.docs.upsert_source("oc1", "other chunk", {"pack_id": OTHER})
        self.vec.upsert_texts(["other chunk"], [{"pack_id": OTHER}], ["oc1"])

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
        with principal_scope(self.principal):
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
        return {"node_errors": (n_res.ingest_err, n_res.cleanup_err),
                "nodes": tuple(n_res[:5]) + (n_res[6],), "edges": tuple(e_res),
                "chunks": tuple(c_res[:5]) + (c_res[6],), "finalize": fin}

    def snapshot(self) -> dict:
        """저장소 상태의 비교용 사본. 시각과 행 번호는 뺀다."""
        out: dict = {}
        conns = {}
        for key, path in self.paths.items():
            conns[str(path)] = sqlite3.connect(str(path))
        for conn in conns.values():
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        try:
            gconn = conns[str(self.paths["graph"])]
            dconn = conns[str(self.paths["doc"])]
            vconn = conns[str(self.paths["vec"])]
            out["graph_nodes"] = sorted(
                (r[0], r[1], _props(r[2])) for r in gconn.execute(
                    "SELECT node_id, node_type, properties FROM graph_nodes"))
            out["graph_edges"] = sorted(
                (r[0], r[1], r[2], _props(r[3])) for r in gconn.execute(
                    "SELECT from_id, relation, to_id, properties FROM graph_edges"))
            out["doc_nodes"] = sorted(
                (r[0], r[1], r[2], _props(r[3])) for r in dconn.execute(
                    "SELECT space, node_type, node_id, properties FROM doc_nodes"))
            out["doc_sources"] = sorted(
                (r[0], r[1], _props(r[2])) for r in dconn.execute(
                    "SELECT source_id, text, metadata FROM doc_sources"))
            from sqlalchemy import text

            with self.sql._engine.connect() as registry:
                out["registry_nodes"] = sorted(tuple(row) for row in registry.execute(
                    text("SELECT space, node_type, node_id FROM ontology_nodes")))
            out["vectors"] = sorted(
                (r[0], _props(r[1]), r[2], bytes(r[3])) for r in vconn.execute(
                    "SELECT node_id, metadata, document, embedding FROM vectors_kure"))
        finally:
            for conn in conns.values():
                conn.close()
        return out


_VOLATILE = {"created_at", "updated_at", "ingested_at", "last_seen", "timestamp", "_ts", "owner_id"}


def _props(raw) -> str:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
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


# 한 회차의 입력 묶음이다. 첫 회는 빈 저장소에 넣고, 둘째 회는 변경과 삭제를 모두 담는다.
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
            other_rows = [r for r in single_snap[-1]["graph_nodes"] if r[0] == "o1"]
            assert other_rows, "다른 팩의 행이 남아 있어야 한다"
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
    from sqlalchemy import text

    result = {}
    with env.sql._engine.connect() as registry:
        result["registry"] = [tuple(row) for row in registry.execute(
            text("SELECT * FROM ontology_nodes WHERE node_id='o1'"))]
    for role, tables in (("graph", ("graph_nodes", "graph_edges")),
                         ("doc", ("doc_nodes", "doc_sources", "doc_source_integrity")),
                         ("vec", ("vectors_kure",))):
        with sqlite3.connect(env.paths[role]) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            for table in tables:
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                result[table] = sorted(tuple(row) for row in rows
                                       if any(value in {"o1", "oc1"} for value in row if isinstance(value, str)))
    return result


def test_shared_id_orphans_anchor_and_final_same_rounds_match(tmp_path, monkeypatch):
    import struct

    outcomes = []
    for layout in ("five", "single"):
        env = _Env(layout, tmp_path / layout, monkeypatch)
        try:
            _seed_write(env, lambda: env.graph.upsert_node(
                "Document", "anchor", {"pack_id": PACK, "created_by": "title-backfill"}, "resource"))
            before_other = _other_rows(env)
            rounds = []
            calls = []
            def embedding(texts):
                calls.append(tuple(texts))
                return _embed(texts)
            env.vec._ef = embedding
            for index, inputs in enumerate((_FIRST, _SECOND, _SECOND, _SECOND)):
                nodes, edges, chunks = inputs
                chunks = [*chunks, {"id": "n1", "document_id": "n1", "text": "shared authoritative text"}]
                rounds.append(env.run(nodes, edges, chunks, env.d / f"run{index}"))
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
            with sqlite3.connect(env.paths["vec"]) as conn:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                vector = conn.execute("SELECT embedding FROM vectors_kure WHERE node_id='n1'").fetchone()[0]
                assert struct.unpack("4f", vector) == pytest.approx(_embed(["shared authoritative text"])[0])
            outcomes.append((rounds, env.snapshot(), calls))
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


def _observe_deletes(env, patches, trace, unit):
    for store_name, method in set(_DELETE_SITES.values()):
        store = getattr(env, store_name)
        original = getattr(store, method)
        def observe(*args, _original=original, _store=store_name, _method=method, **kwargs):
            identity = (_store, _method, repr(args), repr(sorted(kwargs.items())))
            trace.append((unit[0], identity))
            if env.shared is not None:
                assert env.shared.in_unit_of_work
                assert env.shared._uow.validated_packs == {PACK: env.shared.pack_incarnation(PACK)}
                assert env.shared._uow.requested_pack == PACK
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
    if site == "node_sweep":
        monkeypatch.setattr(pack_load, "_dup_type_node_rows", lambda *_: {"n1": {"Document", "Concept"}})
    with principal_scope(env.principal):
        state = pack_load.live_pack_state(PACK, env.graph, env.docs, env.vec)
    if site.startswith("node_"):
        rows = [_node("n1", node_type="Concept", space="concept")] if site in {
            "node_type", "node_old_doc"} else [_node("n1")]
        path = _write_jsonl(env.d / "nodes.jsonl", rows)
        def run():
            return pack_load.load_nodes_incremental(PACK, path, env.builder, {}, state["nodes"],
                env.graph, env.docs, state["doc_node_spaces"], vec=env.vec, sql=env.sql)
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
    def counted(sql, *, outcome=None):
        unit[0] += 1
        with original(sql, outcome=outcome) as conn:
            yield conn
    try:
        run = _deletion_setup(control, monkeypatch, site)
        with monkeypatch.context() as spy:
            spy.setattr(sql_store, "shared_write_unit", counted)
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
        def injected(sql, *, outcome=None):
            unit[0] += 1
            selected = unit[0] == target_unit
            if selected:
                targeted.append(unit[0])
                assert observed == [row for row in trace if row[0] < target_unit]
                if phase == "start":
                    raise failure
            try:
                with original(sql, outcome=outcome) as conn:
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
            spy.setattr(sql_store, "shared_write_unit", injected)
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
    for layout in ("five", "single"):
        env = _Env(layout, tmp_path / layout, monkeypatch)
        try:
            run = _deletion_setup(env, monkeypatch, site)
            audit_before = _audit_rows(env)
            trace = []
            unit = [0]
            original = sql_store.shared_write_unit
            @contextlib.contextmanager
            def counted(sql, *, outcome=None):
                unit[0] += 1
                with original(sql, outcome=outcome) as conn:
                    yield conn
            before = env.snapshot()
            # The dry control uses the same initial state on a separate environment.
            control = _Env(layout, tmp_path / (layout + "-control"), monkeypatch)
            try:
                control_run = _deletion_setup(control, monkeypatch, site)
                with monkeypatch.context() as spy:
                    spy.setattr(sql_store, "shared_write_unit", counted)
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
            public = getattr(getattr(env, store_name), method)
            method_calls = 0
            target_index = sum(identity[:2] == (store_name, method) for _, identity in trace[:trace.index((target_unit, target_identity))])
            def interrupted(*args, **kwargs):
                nonlocal method_calls
                identity = (store_name, method, repr(args), repr(sorted(kwargs.items())))
                selected = method_calls == target_index
                method_calls += 1
                if selected and not fired:
                    assert identity == target_identity
                    fired.append(True)
                    raise KeyboardInterrupt("prefix interruption")
                return public(*args, **kwargs)
            with monkeypatch.context() as spy:
                spy.setattr(getattr(env, store_name), method, interrupted)
                with principal_scope(env.principal), pytest.raises(KeyboardInterrupt):
                    run()
            assert fired == [True]
            prefix_states.append(env.snapshot())
            env.run(*_SECOND, env.d / "resume")
            assert set(audit_before).issubset(set(_audit_rows(env)))
            assert len(_audit_rows(env)) > len(audit_before)
            final_states.append(env.snapshot())
        finally:
            env.close()
    assert prefix_states[0] == prefix_states[1]
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
        assert f"{source}-cites->{target}" in caplog.text
    finally:
        env.close()
