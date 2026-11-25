"""PG 공유 작업 단위의 연결과 저장점 계약을 확인한다."""

import threading
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, text

from opencrab.pack import load
from opencrab.pack.load import _delete_pack_vectors, _live_vec_ids, _vec_backend, _vec_meta_update
from opencrab.pack.ownership import get_pack
from opencrab.stores._pg_uow import PgUnitOfWork, common_pg_uow
from opencrab.stores._single_file import RollbackOnlyError
from opencrab.stores.pg_doc_store import PgDocStore
from opencrab.stores.pg_graph_store import PGGraphStore
from opencrab.stores.pg_vector_store import PgVectorStore
from opencrab.stores.sql_store import SQLStore


@pytest.mark.parametrize("store_type", [PGGraphStore, PgDocStore, PgVectorStore, SQLStore])
def test_pg_store_refuses_initialization_in_active_uow(store_type) -> None:
    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    with uow.unit_of_work():
        with pytest.raises(RuntimeError, match="cannot initialize"):
            if store_type is PgVectorStore:
                store_type(engine, lambda texts: [], pg_uow=uow)
            elif store_type is SQLStore:
                store_type("postgresql://example/test", engine=engine, pg_uow=uow)
            else:
                store_type(engine, pg_uow=uow)
    engine.connect.assert_not_called()
    engine.begin.assert_called_once()


def test_common_pg_uow_rejects_mixed_stores() -> None:
    uow = PgUnitOfWork(MagicMock())
    store = MagicMock()
    store._pg_uow = uow
    other = MagicMock()
    other._pg_uow = None
    assert common_pg_uow(store, store) is uow
    assert common_pg_uow(other) is None
    with pytest.raises(RuntimeError, match="do not share"):
        common_pg_uow(store, other)


@pytest.mark.parametrize("failure", ["begin", "rollback", "commit"])
def test_chunk_pg_transaction_uncertain_is_not_retried(failure) -> None:
    engine = MagicMock()
    manager = engine.begin.return_value
    if failure == "begin":
        manager.__enter__.side_effect = RuntimeError("begin failure")
    else:
        manager.__exit__.side_effect = RuntimeError(f"{failure} failure")
    body = []
    expected = load._ChunkTransactionStartError if failure == "begin" else load.ChunkTransactionUncertainError
    with pytest.raises(expected) as caught:
        with load._chunk_transaction(PgUnitOfWork(engine), None):
            body.append(True)
            if failure == "rollback":
                raise ValueError("body failure")
    if failure == "begin":
        assert caught.value.original is manager.__enter__.side_effect
    assert body == ([] if failure == "begin" else [True])
    engine.begin.assert_called_once()


def test_chunk_pg_confirmed_rollback_preserves_body_exception() -> None:
    engine = MagicMock()
    engine.begin.return_value.__exit__.return_value = False
    with pytest.raises(ValueError, match="body failure"):
        with load._chunk_transaction(PgUnitOfWork(engine), None):
            raise ValueError("body failure")


def test_pg_uow_reuses_connection_and_savepoints() -> None:
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    uow = PgUnitOfWork(engine)
    with uow.unit_of_work() as outer:
        with uow.connection() as reader:
            assert reader is outer
        with uow.connection(write=True) as writer:
            assert writer is outer
        with uow.unit_of_work() as inner:
            assert inner is outer
    assert outer is conn
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()
    conn.begin_nested.assert_called_once()
    assert not uow.active


def test_pg_uow_rollback_only_propagates_to_outer_transaction() -> None:
    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    with pytest.raises(RollbackOnlyError):
        with uow.unit_of_work():
            uow.mark_rollback_only()
    assert engine.begin.return_value.__exit__.call_args.args[0] is RollbackOnlyError
    assert not uow.active


def test_pg_uow_nested_exception_marks_outer_rollback_only() -> None:
    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    with pytest.raises(RollbackOnlyError):
        with uow.unit_of_work():
            with pytest.raises(ValueError):
                with uow.unit_of_work():
                    raise ValueError("failure")
    assert not uow.active


def test_pg_uow_savepoint_failure_can_be_handled() -> None:
    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    with uow.unit_of_work():
        with pytest.raises(ValueError):
            with uow.connection(write=True):
                raise ValueError("failure")
        assert uow.active
    assert engine.begin.return_value.__exit__.call_args.args[0] is None


@pytest.mark.parametrize("failure", ["begin", "rollback", "commit"])
def test_pg_savepoint_cleanup_failure_forces_outer_rollback(failure) -> None:
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    savepoint = conn.begin_nested.return_value
    if failure == "begin":
        conn.begin_nested.side_effect = RuntimeError("savepoint failure")
    else:
        getattr(savepoint, failure).side_effect = RuntimeError("savepoint failure")
    uow = PgUnitOfWork(engine)
    with pytest.raises(RollbackOnlyError):
        with uow.unit_of_work():
            with pytest.raises(RuntimeError, match="savepoint failure"):
                with uow.connection(write=True):
                    if failure == "rollback":
                        raise ValueError("write failure")
    assert engine.begin.return_value.__exit__.call_args.args[0] is RollbackOnlyError


def test_pg_graph_transaction_uses_shared_connection_and_advisory_lock() -> None:
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    uow = PgUnitOfWork(engine)
    store = PGGraphStore.__new__(PGGraphStore)
    store._engine = engine
    store._pg_uow = uow
    store._graph_tx_state = threading.local()
    store._text = text
    with uow.unit_of_work():
        store._run_graph_tx(lambda tx: tx.execute("SELECT 1"))
    engine.begin.assert_called_once()
    conn.begin_nested.assert_called_once()
    assert "pg_advisory_xact_lock" in str(conn.execute.call_args_list[0].args[0])
    assert str(conn.execute.call_args_list[1].args[0]) == "SELECT 1"


def test_pg_doc_and_vector_use_same_connection() -> None:
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    conn = engine.begin.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.fetchone.return_value = None
    conn.execute.return_value.rowcount = 1
    uow = PgUnitOfWork(engine)
    docs = PgDocStore.__new__(PgDocStore)
    docs._engine = engine
    docs._pg_uow = uow
    docs._text = text
    vector = PgVectorStore.__new__(PgVectorStore)
    vector._engine = engine
    vector._pg_uow = uow
    vector._available = True
    vector._table = "test_vectors"
    vector._dim = 2
    assert _vec_backend(vector)[0] == "sqlalchemy"
    with uow.unit_of_work():
        docs._exec_write("SELECT 1", {})
        vector.upsert_precomputed(["a"], [{"pack_id": "p"}], ["x"], [[0.0, 1.0]])
        vector.get_by_id("x")
        assert _live_vec_ids(vector, "p") == set()
        assert _vec_meta_update(vector, "x", {"pack_id": "p"}, "p")
        assert _delete_pack_vectors("p", vector, True) == (1, "sqlalchemy", True)
    engine.begin.assert_called_once()
    engine.connect.assert_not_called()
    assert conn.begin_nested.call_count == 4


def test_pg_delete_pack_rejects_outer_uow_before_journal(monkeypatch) -> None:
    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    sql = MagicMock()
    sql._pg_uow = uow
    journal = MagicMock()
    monkeypatch.setattr(load, "require_live_data", lambda _: None)
    monkeypatch.setattr(load, "_require_bound_principal", lambda: object())
    monkeypatch.setattr(load, "authorize_delete", lambda *_: None)
    monkeypatch.setattr(load.delete_journal, "save_journal", journal)
    with uow.unit_of_work():
        with pytest.raises(RuntimeError, match="cannot run inside"):
            load.delete_pack("p", object(), object(), object(), sql=sql)
    journal.assert_not_called()


def test_pack_authorization_read_joins_pg_connection() -> None:
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = None
    uow = PgUnitOfWork(engine)
    store = MagicMock()
    store._engine = engine
    store._shared = None
    store._pg_uow = uow
    with uow.unit_of_work():
        assert get_pack(store, "p") is None
    engine.connect.assert_not_called()
    conn.execute.assert_called_once()


def test_pg_registry_writes_join_outer_connection() -> None:
    engine = MagicMock()
    conn = engine.begin.return_value.__enter__.return_value
    uow = PgUnitOfWork(engine)
    store = SQLStore.__new__(SQLStore)
    store._engine = engine
    store._pg_uow = uow
    store._shared = None
    store._available = True
    store._is_sqlite = False
    with uow.unit_of_work():
        store.register_node("s", "type", "n")
        store.register_edge("s", "n", "rel", "s", "m")
    engine.begin.assert_called_once()
    assert conn.begin_nested.call_count == 2
    assert conn.execute.call_count == 2


def test_pg_uow_savepoint_and_rollback_on_sqlalchemy_connection() -> None:
    engine = create_engine("sqlite:///:memory:")
    event.listen(engine, "connect", lambda dbapi, _: setattr(dbapi, "isolation_level", None))
    event.listen(engine, "begin", lambda conn: conn.exec_driver_sql("BEGIN"))
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY)"))
        uow = PgUnitOfWork(engine)
        with uow.unit_of_work():
            with uow.connection(write=True) as conn:
                conn.execute(text("INSERT INTO records VALUES (1)"))
            with pytest.raises(Exception):
                with uow.connection(write=True) as conn:
                    conn.execute(text("INSERT INTO records VALUES (1)"))
            with uow.connection() as conn:
                assert conn.execute(text("SELECT count(*) FROM records")).scalar_one() == 1
        with pytest.raises(RollbackOnlyError):
            with uow.unit_of_work():
                with uow.connection(write=True) as conn:
                    conn.execute(text("INSERT INTO records VALUES (2)"))
                uow.mark_rollback_only()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT id FROM records ORDER BY id")).scalars().all() == [1]
    finally:
        engine.dispose()


def test_pg_hybrid_invalidation_runs_after_commit(monkeypatch) -> None:
    from opencrab.ontology.query import HybridQuery

    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    docs = MagicMock()
    docs.shared_file = None
    docs._pg_uow = uow
    hybrid = HybridQuery(MagicMock(), MagicMock(), doc_store=docs)
    calls = []
    monkeypatch.setattr(hybrid, "_node_index_store", lambda: None)
    monkeypatch.setattr(hybrid._bm25, "invalidate", lambda: calls.append("invalidated"))
    try:
        with uow.unit_of_work():
            hybrid.invalidate_bm25_cache()
            assert calls == []
        assert calls == ["invalidated"]
        with pytest.raises(RuntimeError, match="abort"):
            with uow.unit_of_work():
                hybrid.invalidate_bm25_cache()
                raise RuntimeError("abort")
        assert calls == ["invalidated"]
    finally:
        hybrid.shutdown_bm25()


def test_pg_builder_failure_rolls_back_outer_unit(monkeypatch) -> None:
    from opencrab.auth import Principal, principal_scope
    from opencrab.ontology import builder as builder_module
    from opencrab.ontology.builder import OntologyBuilder

    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    graph, docs, sql = MagicMock(), MagicMock(), MagicMock()
    for store in (graph, docs, sql):
        store._pg_uow = uow
        store.available = True
    sql.shared_file = None
    docs.upsert_node_doc.side_effect = RuntimeError("doc failure")
    monkeypatch.setattr(builder_module, "authorize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder_module, "node_identity_conflict", lambda *_args, **_kwargs: None)
    with principal_scope(Principal(user_id="owner", is_local=True, disabled=False)):
        receipt = OntologyBuilder(graph, docs, sql).add_node(
            "resource", "Document", "n", {"title": "t"}, pack_id="p", write_vector=False,
        )
    assert receipt["stores"]["graph"] == "error: rolled back"
    assert receipt["stores"]["docs"].startswith("error:")
    assert engine.begin.return_value.__exit__.call_args.args[0] is RollbackOnlyError


@pytest.mark.parametrize("incremental", [False, True])
def test_pg_chunk_rejects_existing_uow_before_embedding(tmp_path, monkeypatch, incremental) -> None:
    from opencrab.auth import Principal, principal_scope

    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    vec, docs, sql = MagicMock(), MagicMock(), MagicMock()
    for store in (vec, docs, sql):
        store._pg_uow = uow
    vec.available = True
    monkeypatch.setattr(load, "require_live_data", lambda *_: None)
    monkeypatch.setattr(load, "authorize", lambda *_args, **_kwargs: None)
    with principal_scope(Principal(user_id="owner", is_local=True, disabled=False)):
        with uow.unit_of_work():
            with pytest.raises(RuntimeError, match="cannot run inside"):
                if incremental:
                    load.load_chunks_incremental("p", tmp_path / "missing", vec, docs, {}, sql=sql)
                else:
                    load.load_chunks("p", tmp_path / "missing", vec, docs, sql=sql)
    vec.prepare_upsert_vectors.assert_not_called()
    docs.upsert_source.assert_not_called()


@pytest.mark.parametrize("incremental", [False, True])
def test_pg_chunk_short_embedding_batch_retries_missing_vectors(tmp_path, monkeypatch, incremental) -> None:
    import json

    from opencrab.auth import Principal, principal_scope

    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    vec, docs, sql = MagicMock(), MagicMock(), MagicMock()
    for store in (vec, docs, sql):
        store._pg_uow = uow
    vec.available = True
    calls = []

    def prepare(texts):
        calls.append(list(texts))
        return [[0.0]] if len(texts) == 2 else [[0.0] for _ in texts]

    def write(texts, _metas, _ids, vectors):
        if len(texts) != len(vectors):
            raise ValueError("vector count")

    vec.prepare_upsert_vectors.side_effect = prepare
    vec.upsert_precomputed.side_effect = write
    monkeypatch.setattr(load, "require_live_data", lambda *_: None)
    monkeypatch.setattr(load, "authorize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(load, "_live_vec_ids_or_unconfirmed", lambda *_: (set(), False))
    path = tmp_path / "chunks.jsonl"
    path.write_text("\n".join(json.dumps({"id": str(i), "text": "text", "metadata": {}}) for i in range(2)))
    with principal_scope(Principal(user_id="owner", is_local=True, disabled=False)):
        if incremental:
            result = load.load_chunks_incremental("p", path, vec, docs, {}, sql=sql)
            assert result[0] == 2 and result[4] == 0
        else:
            assert load.load_chunks("p", path, vec, docs, sql=sql) == (2, 0)
    assert [len(call) for call in calls] == [2, 1]


@pytest.mark.parametrize(
    "path_kind,incremental",
    [("batch", False), ("batch", True), ("fallback", False), ("fallback", True), ("metadata", True)],
)
def test_pg_chunk_public_begin_failure_is_not_retried(tmp_path, monkeypatch, path_kind, incremental) -> None:
    import json

    from opencrab.auth import Principal, principal_scope
    engine = MagicMock()
    failure = RuntimeError("begin failure")
    engine.begin.side_effect = failure
    uow = PgUnitOfWork(engine)
    vec, docs, sql = MagicMock(), MagicMock(), MagicMock()
    for store in (vec, docs, sql):
        store._pg_uow = uow
    vec.available = True
    if path_kind == "fallback":
        vec.prepare_upsert_vectors.side_effect = [ValueError("prepare failure"), [[0.0]]]
    else:
        vec.prepare_upsert_vectors.return_value = [[0.0]]
    monkeypatch.setattr(load, "require_live_data", lambda *_: None)
    monkeypatch.setattr(load, "authorize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(load, "_live_vec_ids_or_unconfirmed", lambda *_: ({"s"}, False))
    path = tmp_path / "chunks.jsonl"
    path.write_text(json.dumps({"id": "s", "text": "text", "metadata": {}}))
    live = {"s": ("text", {"different": True})} if path_kind == "metadata" else {}
    with principal_scope(Principal(user_id="owner", is_local=True, disabled=False)):
        with pytest.raises(RuntimeError, match="begin failure") as caught:
            if incremental:
                load.load_chunks_incremental("p", path, vec, docs, live, sql=sql)
            else:
                load.load_chunks("p", path, vec, docs, sql=sql)
    assert caught.value is failure
    engine.begin.assert_called_once()
    assert vec.prepare_upsert_vectors.call_count == (0 if path_kind == "metadata" else 2 if path_kind == "fallback" else 1)
    vec.upsert_precomputed.assert_not_called()
    docs.upsert_source.assert_not_called()
    docs.rewrite_source_metadata.assert_not_called()


@pytest.mark.parametrize("incremental", [False, True])
def test_pg_chunk_fallback_begin_failure_after_confirmed_rollback(tmp_path, monkeypatch, incremental) -> None:
    import json

    from opencrab.auth import Principal, principal_scope

    engine = MagicMock()
    manager = MagicMock()
    manager.__exit__.return_value = False
    failure = RuntimeError("fallback begin failure")
    engine.begin.side_effect = [manager, failure]
    uow = PgUnitOfWork(engine)
    vec, docs, sql = MagicMock(), MagicMock(), MagicMock()
    for store in (vec, docs, sql):
        store._pg_uow = uow
    vec.available = True
    vec.prepare_upsert_vectors.return_value = [[0.0]]
    vec.upsert_precomputed.side_effect = ValueError("body failure")
    monkeypatch.setattr(load, "require_live_data", lambda *_: None)
    monkeypatch.setattr(load, "authorize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(load, "_live_vec_ids_or_unconfirmed", lambda *_: (set(), False))
    path = tmp_path / "chunks.jsonl"
    path.write_text(json.dumps({"id": "s", "text": "text", "metadata": {}}))
    with principal_scope(Principal(user_id="owner", is_local=True, disabled=False)):
        with pytest.raises(RuntimeError, match="fallback begin failure") as caught:
            if incremental:
                load.load_chunks_incremental("p", path, vec, docs, {}, sql=sql)
            else:
                load.load_chunks("p", path, vec, docs, sql=sql)
    assert caught.value is failure
    assert engine.begin.call_count == 2
    assert manager.__exit__.call_args.args[0] is ValueError
    vec.upsert_precomputed.assert_called_once()
    docs.upsert_source.assert_not_called()


@pytest.mark.parametrize("failure", ["rollback", "commit"])
def test_pg_chunk_metadata_uncertain_does_not_return_or_retry(tmp_path, monkeypatch, failure) -> None:
    import json

    from opencrab.auth import Principal, principal_scope

    engine = MagicMock()
    manager = engine.begin.return_value
    manager.__exit__.side_effect = RuntimeError(f"{failure} failure")
    uow = PgUnitOfWork(engine)
    vec, docs, sql = MagicMock(), MagicMock(), MagicMock()
    for store in (vec, docs, sql):
        store._pg_uow = uow
    vec.available = True
    if failure == "rollback":
        docs.rewrite_source_metadata.side_effect = ValueError("body failure")
    monkeypatch.setattr(load, "require_live_data", lambda *_: None)
    monkeypatch.setattr(load, "authorize", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(load, "_live_vec_ids_or_unconfirmed", lambda *_: ({"s"}, False))
    monkeypatch.setattr(load, "_vec_meta_update", lambda *_: True)
    path = tmp_path / "chunks.jsonl"
    path.write_text(json.dumps({"id": "s", "text": "text", "metadata": {}}))
    with principal_scope(Principal(user_id="owner", is_local=True, disabled=False)):
        with pytest.raises(load.ChunkTransactionUncertainError):
            load.load_chunks_incremental("p", path, vec, docs, {"s": ("text", {"old": True})}, sql=sql)
    engine.begin.assert_called_once()
    docs.rewrite_source_metadata.assert_called_once()
    vec.prepare_upsert_vectors.assert_not_called()


def test_pg_callbacks_run_only_after_commit() -> None:
    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    observed = []
    with uow.unit_of_work():
        uow.after_commit(lambda: observed.append(uow.active))
        assert observed == []
    assert observed == [False]
    with pytest.raises(RollbackOnlyError):
        with uow.unit_of_work():
            uow.after_commit(lambda: observed.append("unexpected"))
            uow.mark_rollback_only()
    assert observed == [False]
    engine.begin.return_value.__exit__.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        with uow.unit_of_work():
            uow.after_commit(lambda: observed.append("unexpected"))
    assert observed == [False]


def test_pg_uow_without_outer_transaction_keeps_engine_contract() -> None:
    engine = MagicMock()
    uow = PgUnitOfWork(engine)
    with uow.connection():
        pass
    with uow.connection(write=True):
        pass
    engine.connect.assert_called_once()
    engine.begin.assert_called_once()
    with pytest.raises(RuntimeError):
        uow.mark_rollback_only()
