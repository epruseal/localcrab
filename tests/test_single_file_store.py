"""단일 파일 공유 연결과 트랜잭션 구간 (#671 시제품, 기본 꺼짐).

``SharedSqliteFile``을 쓰는 세 저장소(그래프, doc, 벡터)가 연결 하나와 쓰기 락 하나를 나눠 쓴다.
``unit_of_work()`` 안의 쓰기는 일반 테이블, fts5, vec0를 가리지 않고 한 트랜잭션으로 커밋되거나
롤백된다. ``shared`` 인자가 없으면 종전 동작이다(기존 저장소 시험이 그 경로를 이미 건다).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest
import sqlite_vec

from opencrab.pack.ownership import get_pack
from opencrab.stores._single_file import SharedSqliteFile, unit_of_work_for
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore
from opencrab.stores.sqlite_vec_store import SqliteVecStore

DIM = 4


def _embed(texts):
    return [[float(len(t) % 5), 1.0, 0.0, 0.5] for t in texts]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    graph = LocalGraphStore(path, shared=shared)
    docs = LocalSQLDocStore(path, shared=shared)
    vec = SqliteVecStore(path, embedding_function=_embed, dim=DIM, shared=shared)
    yield path, shared, graph, docs, vec
    for store in (graph, docs, vec):
        store.close()


def _counts(path: str) -> tuple[int, int, int]:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        return (
            conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0],
            conn.execute("SELECT count(*) FROM doc_sources_fts").fetchone()[0],
            conn.execute("SELECT count(*) FROM vectors_kure").fetchone()[0],
        )
    finally:
        conn.close()


def _write_pair(docs, vec, sid="s1"):
    vec.upsert_texts(texts=["본문"], ids=[sid], metadatas=[{"pack_id": "p"}])
    docs.upsert_source(sid, "본문", {"pack_id": "p"})


def test_direct_sqlite_stores_reject_unmanaged_unified_database(tmp_path):
    path = tmp_path / "unified.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    for factory in (
        lambda: LocalGraphStore(str(path)),
        lambda: LocalSQLDocStore(str(path)),
        lambda: SqliteVecStore(str(path), embedding_function=_embed, dim=DIM),
        lambda: SQLStore(f"sqlite:///{path}"),
    ):
        with pytest.raises(RuntimeError, match="shared"):
            factory()


def test_pack_write_guard_checks_incarnation_and_pending_in_transaction(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "guard.db"))
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    conn.execute("INSERT INTO packs VALUES ('q', 'second')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="active"):
            shared.validate_pack_writes({"p": "first"})
        with shared.unit_of_work():
            shared.validate_pack_writes({"p": "first"})
        for expected in ({"p": "old"}, {"missing": "first"}, {}):
            with pytest.raises(RuntimeError):
                with shared.unit_of_work():
                    shared.validate_pack_writes(expected)
        conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
        conn.commit()
        with pytest.raises(RuntimeError, match="pending"):
            with shared.unit_of_work():
                shared.validate_pack_writes({"p": "first"})
        with pytest.raises(RuntimeError, match="pending"):
            with shared.unit_of_work():
                shared.validate_pack_writes({"q": "second", "p": "first"})
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work() as tx:
                tx.execute("INSERT INTO packs VALUES ('other', 'second')")
                with pytest.raises(RuntimeError, match="pending"):
                    shared.validate_pack_writes({"p": "first"})
        assert conn.execute("SELECT count(*) FROM packs WHERE pack_id='other'").fetchone()[0] == 0
    finally:
        shared.close()


def test_nested_pack_guard_failure_invalidates_outer_work(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "guard.db"))
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work() as tx:
                tx.execute("INSERT INTO packs VALUES ('q', 'second')")
                with pytest.raises(RuntimeError, match="pending"):
                    with shared.unit_of_work():
                        shared.validate_pack_writes({"p": "first"})
        assert conn.execute("SELECT count(*) FROM packs WHERE pack_id='q'").fetchone()[0] == 0
    finally:
        shared.close()


def test_nested_guard_does_not_authorize_after_savepoint_failure(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "guard.db"))
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work():
                with pytest.raises(ValueError):
                    with shared.unit_of_work():
                        shared.validate_pack_writes({"p": "first"})
                        raise ValueError("abort")
                assert not shared._uow.validated_packs
    finally:
        shared.close()


def test_nested_guard_rejects_same_pack_new_incarnation(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "guard.db"))
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work() as tx:
                shared.validate_pack_writes({"p": "first"})
                tx.execute("UPDATE packs SET incarnation='second' WHERE pack_id='p'")
                with pytest.raises(RuntimeError, match="incarnation"):
                    with shared.unit_of_work():
                        shared.validate_pack_writes({"p": "second"})
        assert conn.execute("SELECT incarnation FROM packs WHERE pack_id='p'").fetchone()[0] == "first"
    finally:
        shared.close()


def test_nested_guard_rejects_changed_incarnation_after_prior_validation(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "guard.db"))
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work() as tx:
                shared.validate_pack_writes({"p": "first"})
                tx.execute("UPDATE packs SET incarnation='second' WHERE pack_id='p'")
                with pytest.raises(RuntimeError, match="incarnation"):
                    shared.validate_pack_writes({"p": "first"})
        assert conn.execute("SELECT incarnation FROM packs WHERE pack_id='p'").fetchone()[0] == "first"
    finally:
        shared.close()


def test_delete_operation_guard_accepts_only_matching_pending_identity(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "guard.db"))
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        for operation, incarnation in (("other", "first"), ("op", "other")):
            with pytest.raises(RuntimeError):
                with shared.unit_of_work():
                    shared.validate_delete_operation("p", incarnation, operation)
        with shared.unit_of_work():
            shared.validate_delete_operation("p", "first", "op")
            assert shared._uow.validated_packs == {}
        conn.execute("UPDATE pack_delete_operations SET state='committed'")
        conn.commit()
        with pytest.raises(RuntimeError, match="pending"):
            with shared.unit_of_work():
                shared.validate_delete_operation("p", "first", "op")
        conn.execute("UPDATE pack_delete_operations SET state='pending'")
        conn.execute("UPDATE packs SET incarnation='new'")
        conn.commit()
        with pytest.raises(RuntimeError, match="pending"):
            with shared.unit_of_work():
                shared.validate_delete_operation("p", "first", "op")
    finally:
        shared.close()


def test_pack_write_guard_serializes_against_reservation(tmp_path):
    path = str(tmp_path / "guard.db")
    shared = SharedSqliteFile(path)
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    conn.commit()
    started = threading.Event()
    committed = threading.Event()
    errors = []

    def reserve():
        try:
            with sqlite3.connect(path, timeout=5) as other:
                started.set()
                other.execute("BEGIN IMMEDIATE")
                other.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
            committed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with shared.unit_of_work():
            shared.validate_pack_writes({"p": "first"})
            thread = threading.Thread(target=reserve)
            thread.start()
            assert started.wait(2)
            assert not committed.wait(0.2)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert committed.is_set()
        with pytest.raises(RuntimeError, match="pending"):
            with shared.unit_of_work():
                shared.validate_pack_writes({"p": "first"})
    finally:
        shared.close()


def test_registry_writes_join_shared_transaction(tmp_path):
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    try:
        assert sql.shared_file is shared
        with pytest.raises(RuntimeError, match="rollback"):
            with shared.unit_of_work() as conn:
                assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                sql.register_node("concept", "Thing", "n1")
                sql.register_edge("concept", "n1", "rel", "concept", "n2")
                assert conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM ontology_edges").fetchone()[0] == 1
                raise RuntimeError("rollback")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM ontology_edges").fetchone()[0] == 0
        with shared.unit_of_work():
            sql.register_node("concept", "Thing", "n1")
            sql.register_edge("concept", "n1", "rel", "concept", "n2")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM ontology_edges").fetchone()[0] == 1
        sql.close()
        sql.close()
    finally:
        sql.close()


def test_shared_connection_creation_is_closed_if_hook_fails(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    owner = object()
    def fail(_conn):
        raise RuntimeError("hook failed")
    shared.acquire(owner, fail)
    try:
        with pytest.raises(RuntimeError, match="hook failed"):
            shared.connection()
        assert shared.all_conns == []
    finally:
        shared.release(owner)


def test_failed_hook_registration_does_not_leave_owner_or_hook(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    first = object()
    second = object()
    shared.acquire(first, None)
    shared.connection()
    def fail(_conn):
        raise RuntimeError("hook failed")
    try:
        with pytest.raises(RuntimeError, match="hook failed"):
            shared.acquire(second, fail)
        assert second not in shared._users
        assert fail not in shared._hooks
    finally:
        shared.release(first)


def test_released_owner_hook_is_not_used_by_new_connections(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    first, second = object(), object()
    calls = []

    def hook(_conn):
        calls.append("first")

    shared.acquire(first, hook)
    shared.acquire(second, None)
    shared.release(first)
    try:
        shared.new_conn()
        assert calls == []
    finally:
        shared.release(second)


def test_identical_hook_remains_until_last_owner_releases(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    first, second = object(), object()
    calls = []

    def hook(_conn):
        calls.append("hook")

    shared.acquire(first, hook)
    shared.acquire(second, hook)
    shared.release(first)
    try:
        shared.new_conn()
        assert calls == ["hook"]
    finally:
        shared.release(second)


def test_last_release_waits_for_active_unit(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    owner = object()
    shared.acquire(owner, None)
    started = threading.Event()
    released = threading.Event()

    def close_owner():
        started.set()
        shared.release(owner)
        released.set()

    with shared.unit_of_work() as conn:
        worker = threading.Thread(target=close_owner)
        worker.start()
        assert started.wait(5)
        assert not released.wait(0.1)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    worker.join(5)
    assert not worker.is_alive()
    assert released.is_set()


def test_exclusive_lifetime_lock_rejects_same_thread_shared_handle(tmp_path):
    from opencrab.locking import acquire_file_lock

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    try:
        with pytest.raises(RuntimeError, match="upgrade"):
            acquire_file_lock("single.db.lifetime.lock", str(tmp_path), timeout=0)
    finally:
        shared.close()


def test_file_lock_rejects_shared_to_exclusive_upgrade(tmp_path):
    from opencrab.locking import file_lock

    with file_lock("single.db.lifetime.lock", str(tmp_path), shared=True):
        with pytest.raises(RuntimeError, match="upgrade"):
            with file_lock("single.db.lifetime.lock", str(tmp_path)):
                pytest.fail("exclusive lock was not acquired")


@pytest.mark.parametrize("filename", ["write.lock", "delete-pack-example.lock"])
def test_checked_shared_file_rejects_reverse_lock_order(tmp_path, filename):
    from opencrab.locking import file_lock

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    with file_lock(filename, str(tmp_path)):
        with pytest.raises(RuntimeError, match="lock order"):
            SharedSqliteFile(str(path), checked=True)
    assert not (tmp_path / "single.db.lifetime.lock").exists()


def test_checked_shared_file_holds_lifetime_lock(tmp_path):
    from opencrab.locking import acquire_file_lock, release_file_lock

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    owner = object()
    shared.acquire(owner, None)
    try:
        with pytest.raises(RuntimeError, match="upgrade"):
            acquire_file_lock("single.db.lifetime.lock", str(tmp_path), timeout=0)
        assert shared.connection().execute("SELECT 1").fetchone()[0] == 1
    finally:
        shared.release(owner)
    lock = acquire_file_lock("single.db.lifetime.lock", str(tmp_path), timeout=0)
    release_file_lock(lock)


def test_lifetime_lock_excludes_other_process(tmp_path):
    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    owner = object()
    shared.acquire(owner, None)
    code = (
        "import sys; from opencrab.locking import acquire_file_lock; "
        "acquire_file_lock('single.db.lifetime.lock', sys.argv[1], timeout=0)"
    )
    try:
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
        assert result.returncode != 0
        assert "TimeoutError" in result.stderr
    finally:
        shared.release(owner)
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_checked_shared_file_detects_path_replacement(tmp_path):
    path = tmp_path / "single.db"
    replacement = tmp_path / "replacement.db"
    sqlite3.connect(path).close()
    sqlite3.connect(replacement).close()
    shared = SharedSqliteFile(str(path), checked=True)
    owner = object()
    shared.acquire(owner, None)
    try:
        os.replace(replacement, path)
        with pytest.raises(ValueError, match="identity|link"):
            shared.connection()
    finally:
        shared.release(owner)


def test_checked_shared_file_releases_lock_before_store_acquisition(tmp_path):
    from opencrab.locking import acquire_file_lock, release_file_lock

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    shared.close()
    lock = acquire_file_lock("single.db.lifetime.lock", str(tmp_path), timeout=0)
    release_file_lock(lock)
    with pytest.raises(RuntimeError, match="closed"):
        shared.acquire(object(), None)


def test_checked_shared_file_rejects_hardlinked_lock(tmp_path):
    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    lock = tmp_path / "single.db.lifetime.lock"
    lock.touch()
    os.link(lock, tmp_path / "lock-alias")
    with pytest.raises((OSError, ValueError)):
        SharedSqliteFile(str(path), checked=True)


def test_checked_shared_file_rejects_hardlinked_db(tmp_path):
    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    alias = tmp_path / "alias.db"
    os.link(path, alias)
    for candidate in (path, alias):
        with pytest.raises(ValueError, match="link"):
            SharedSqliteFile(str(candidate), checked=True)


def test_hardlinked_paths_are_rejected_in_separate_processes(tmp_path):
    path = tmp_path / "single.db"
    alias = tmp_path / "alias.db"
    sqlite3.connect(path).close()
    os.link(path, alias)
    code = "import sys; from opencrab.stores._single_file import SharedSqliteFile; SharedSqliteFile(sys.argv[1], checked=True)"
    for candidate in (path, alias):
        result = subprocess.run([sys.executable, "-c", code, str(candidate)], capture_output=True, text=True)
        assert result.returncode != 0
        assert "one link" in result.stderr


def test_closed_shared_file_cannot_be_reused(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    sql = SQLStore(f"sqlite:///{tmp_path / 'single.db'}", shared=shared)
    with shared.unit_of_work():
        sql.register_node("concept", "Thing", "n1")
    sql.close()
    with pytest.raises(RuntimeError, match="closed"):
        shared.acquire(object(), None)


def test_closed_store_does_not_reuse_shared_thread_connection(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    docs = LocalSQLDocStore(str(tmp_path / "single.db"), shared=shared)
    assert docs._conn is not None
    docs.close()
    with pytest.raises(RuntimeError, match="closed"):
        _ = docs._conn


def test_registry_rejects_mismatched_shared_file(tmp_path):
    shared = SharedSqliteFile(str(tmp_path / "one.db"))
    with pytest.raises(ValueError, match="does not match"):
        SQLStore(f"sqlite:///{tmp_path / 'other.db'}", shared=shared)
    assert not (tmp_path / "other.db").exists()


def test_registry_shared_hook_enables_foreign_keys_on_existing_connection(tmp_path):
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    owner = object()
    shared.acquire(owner, None)
    conn = shared.connection()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        sql.close()
        shared.release(owner)


def test_registry_shared_rejects_registration_during_transaction(tmp_path):
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    owner = object()
    shared.acquire(owner, None)
    try:
        with shared.unit_of_work():
            with pytest.raises(RuntimeError, match="transaction"):
                SQLStore(f"sqlite:///{path}", shared=shared)
    finally:
        shared.release(owner)


def test_registry_shared_connection_enforces_foreign_keys(tmp_path):
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with shared.unit_of_work() as conn:
                conn.execute("INSERT INTO packs (pack_id, owner_id) VALUES (?, ?)", ("pack", "missing"))
    finally:
        sql.close()


def test_registry_pack_read_sees_shared_uncommitted_write(tmp_path):
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    try:
        with shared.unit_of_work() as conn:
            conn.execute("INSERT INTO users (user_id, display_name) VALUES (?, ?)", ("owner", "Owner"))
            conn.execute("INSERT INTO packs (pack_id, owner_id) VALUES (?, ?)", ("pack", "owner"))
            assert get_pack(sql, "pack")["owner_id"] == "owner"
    finally:
        sql.close()


def test_mixed_shared_stores_fail_closed(stores, tmp_path):
    from opencrab.stores._single_file import is_single_file

    _path, shared, graph, docs, vec = stores
    other = SharedSqliteFile(str(tmp_path / "other.db"))
    other_sql = SQLStore(f"sqlite:///{tmp_path / 'other.db'}", shared=other)
    try:
        with pytest.raises(RuntimeError, match="shared"):
            is_single_file(graph, docs, vec, other_sql)
        with pytest.raises(RuntimeError, match="shared"):
            is_single_file(None, graph)
    finally:
        other_sql.close()


def test_stores_share_one_file_and_one_connection(stores):
    path, shared, graph, docs, vec = stores
    assert graph.shared_file is docs.shared_file is vec.shared_file is shared
    assert graph._conn is docs._conn is vec._conn
    assert shared.db_path == path


def test_vec0_extension_is_usable_even_though_graph_store_opened_the_connection_first(stores):
    _path, _shared, _graph, _docs, vec = stores
    assert vec.available
    vec.upsert_texts(texts=["a"], ids=["x"], metadatas=[{"pack_id": "p"}])
    assert vec.count() == 1


def test_checked_sqlstore_releases_lifetime_lock_on_close(tmp_path):
    from opencrab.locking import acquire_file_lock, release_file_lock

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    assert sql.available
    sql.close()
    lock = acquire_file_lock("single.db.lifetime.lock", str(tmp_path), timeout=0)
    release_file_lock(lock)


def test_shared_pack_search_keeps_snapshot_across_writer_commit(stores, monkeypatch):
    path, _shared, _graph, _docs, vec = stores
    vec.upsert_texts(["one", "two"], [{"pack_id": "p"}, {"pack_id": "q"}], ["a", "b"])
    other_shared = SharedSqliteFile(path)
    writer = SqliteVecStore(path, embedding_function=_embed, dim=DIM, shared=other_shared)
    original = vec._knn
    calls = []

    def mutate_after_first(qvec, k, pack):
        rows = original(qvec, k, pack)
        calls.append(pack)
        if len(calls) == 1:
            writer.upsert_texts(["new"], [{"pack_id": "q"}], ["c"])
        return rows

    monkeypatch.setattr(vec, "_knn", mutate_after_first)
    try:
        hits = vec.query("one", n_results=10, where={"pack_id": {"$in": ["p", "q"]}})
        assert {hit["id"] for hit in hits} == {"a", "b"}
        assert {hit["id"] for hit in vec.query("one", n_results=10,
                                               where={"pack_id": "q"})} == {"b", "c"}
    finally:
        writer.close()


def test_shared_query_in_transaction_sees_temporary_vector_then_rolls_back(stores):
    path, shared, _graph, _docs, vec = stores
    with pytest.raises(RuntimeError, match="abort"):
        with shared.unit_of_work():
            vec.upsert_texts(["temporary"], [{"pack_id": "p"}], ["temporary"])
            assert [hit["id"] for hit in vec.query("temporary", where={"pack_id": "p"})] == ["temporary"]
            raise RuntimeError("abort")
    assert vec.query("temporary", where={"pack_id": "p"}) == []


def test_vector_metadata_helper_rejects_owner_change(stores):
    from opencrab.pack.load import _vec_meta_update

    path, shared, _graph, _docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    vec.upsert_texts(["text"], [{"pack_id": "allowed"}], ["s1"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                with pytest.raises(RuntimeError, match="owner"):
                    _vec_meta_update(vec, "s1", {"pack_id": "blocked"}, "allowed")
        assert vec.get_by_id("s1")["metadata"]["pack_id"] == "allowed"
    finally:
        sql.close()


def test_vector_metadata_helper_rejects_unvalidated_scope(stores):
    from opencrab.pack.load import _vec_meta_update

    path, shared, _graph, _docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    vec.upsert_texts(["text"], [{"pack_id": "blocked"}], ["s1"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                with pytest.raises(RuntimeError, match="owner"):
                    _vec_meta_update(vec, "s1", {"pack_id": "blocked", "x": 1}, "blocked")
        assert vec.get_by_id("s1")["metadata"].get("x") is None
    finally:
        sql.close()


def test_pack_vector_delete_helper_rejects_unvalidated_scope(stores):
    from opencrab.pack.load import _delete_pack_vectors

    path, shared, _graph, _docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    vec.upsert_texts(["text"], [{"pack_id": "blocked"}], ["s1"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                _count, _kind, confirmed = _delete_pack_vectors("blocked", vec, True)
                assert not confirmed
        assert vec.get_by_id("s1") is not None
    finally:
        sql.close()


def test_shared_vector_pack_delete_reports_confirmed_count(stores):
    from opencrab.pack.load import _delete_pack_vectors

    _path, shared, _graph, _docs, vec = stores
    vec.upsert_texts(["one", "two"], [{"pack_id": "p"}, {"pack_id": "p"}], ["a", "b"])
    with shared.unit_of_work():
        count, kind, confirmed = _delete_pack_vectors("p", vec, True)
        assert (count, kind, confirmed) == (2, "sql", True)
    assert vec.get_by_id("a") is None
    assert vec.get_by_id("b") is None


def test_default_pack_single_db_creates_user_and_pack_atomically(stores):
    from opencrab.auth import create_user
    from opencrab.pack.ownership import ensure_default_pack

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TRIGGER fill_incarnation AFTER INSERT ON packs BEGIN UPDATE packs SET incarnation='first' WHERE pack_id=NEW.pack_id; END")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        owner = create_user(sql, "owner", is_local=True)
        default = ensure_default_pack(sql, owner)
        assert conn.execute("SELECT owner_id, incarnation FROM packs WHERE pack_id=?", (default,)).fetchone()[:] == (owner, "first")
        assert ensure_default_pack(sql, owner) == default
    finally:
        sql.close()


def test_default_pack_single_db_concurrent_creation(tmp_path, monkeypatch):
    from opencrab.auth import create_user
    from opencrab.pack.ownership import ensure_default_pack

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    conn = shared.connection()
    conn.execute("DELETE FROM packs WHERE owner_id=?", (owner,))
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TRIGGER fill_incarnation AFTER INSERT ON packs BEGIN UPDATE packs SET incarnation=lower(hex(randomblob(16))) WHERE pack_id=NEW.pack_id; END")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def create_default():
        try:
            barrier.wait(timeout=5)
            results.append(ensure_default_pack(sql, owner))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create_default) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
        assert not errors
        assert len(results) == 2 and results[0] == results[1]
        assert conn.execute("SELECT count(*) FROM packs WHERE owner_id=? AND is_default=1", (owner,)).fetchone()[0] == 1
    finally:
        sql.close()


def test_existing_default_pack_pending_marks_outer_rollback(stores):
    from opencrab.auth import create_user
    from opencrab.pack.ownership import ensure_default_pack, find_default_pack

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    default = find_default_pack(sql, owner)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id=?", (default,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', ?, 'first', 'pending')", (default,))
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work() as tx:
                tx.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('temp', 'temp', 0)")
                with pytest.raises(RuntimeError, match="pending"):
                    ensure_default_pack(sql, owner, conn=tx)
        assert conn.execute("SELECT count(*) FROM users WHERE user_id='temp'").fetchone()[0] == 0
    finally:
        sql.close()


def test_default_pack_single_db_retries_existing_candidate(stores, monkeypatch):
    from opencrab.auth import create_user
    from opencrab.pack import ownership

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    old_owner = create_user(sql, "old", is_local=False)
    ownership.create_pack(sql, old_owner, "taken")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='old'")
    conn.execute("CREATE TRIGGER fill_incarnation AFTER INSERT ON packs BEGIN UPDATE packs SET incarnation='new' WHERE pack_id=NEW.pack_id; END")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    candidates = iter(("taken", "free"))
    monkeypatch.setattr(ownership, "new_default_pack_id", lambda: next(candidates))
    try:
        owner = create_user(sql, "new", is_local=False)
        assert ownership.ensure_default_pack(sql, owner) == "free"
    finally:
        sql.close()


def test_default_pack_pending_candidate_marks_outer_rollback(stores, monkeypatch):
    from opencrab.auth import create_user
    from opencrab.pack import ownership

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    conn = shared.connection()
    conn.execute("DELETE FROM packs WHERE owner_id=?", (owner,))
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'default-fixed', 'old', 'pending')")
    conn.commit()
    monkeypatch.setattr(ownership, "new_default_pack_id", lambda: "default-fixed")
    try:
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work() as tx:
                tx.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('temp', 'temp', 0)")
                with pytest.raises(RuntimeError, match="pending"):
                    ownership.ensure_default_pack(sql, owner, conn=tx)
        assert conn.execute("SELECT count(*) FROM users WHERE user_id='temp'").fetchone()[0] == 0
    finally:
        sql.close()


def test_default_pack_creation_rejects_pending_candidate_atomically(stores, monkeypatch):
    from opencrab.auth import create_user
    from opencrab.pack import ownership

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'default-fixed', 'old', 'pending')")
    conn.commit()
    monkeypatch.setattr(ownership, "new_default_pack_id", lambda: "default-fixed")
    try:
        with pytest.raises(RuntimeError, match="pending"):
            create_user(sql, "owner", is_local=True)
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM packs").fetchone()[0] == 0
    finally:
        sql.close()


def test_pack_creation_in_single_db_preserves_collision_negotiation(stores):
    from opencrab.auth import create_user
    from opencrab.pack.ownership import create_pack

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        assert create_pack(sql, owner, "p") == "p"
        second = create_pack(sql, owner, "p")
        assert second.startswith("p-")
        assert conn.execute("SELECT count(*) FROM packs WHERE pack_id IN (?, ?)", ("p", second)).fetchone()[0] == 2
    finally:
        sql.close()


def test_pack_creation_rejects_pending_deleted_name(stores):
    from opencrab.auth import create_user
    from opencrab.pack.ownership import create_pack

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'old', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="pending"):
            create_pack(sql, owner, "p")
        assert conn.execute("SELECT count(*) FROM packs WHERE pack_id='p'").fetchone()[0] == 0
    finally:
        sql.close()


def test_registry_edge_requires_matching_graph_owner(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Document", "n1", {"pack_id": "blocked"})
    graph.upsert_node("Document", "n2", {"pack_id": "blocked"})
    graph.upsert_edge("Document", "n1", "cites", "Document", "n2", {"pack_id": "blocked"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                sql.register_edge("resource", "n1", "cites", "resource", "n2")
        assert conn.execute("SELECT count(*) FROM ontology_edges").fetchone()[0] == 0
    finally:
        sql.close()


def test_registry_node_requires_matching_graph_owner(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Document", "n1", {"pack_id": "blocked"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                sql.register_node("resource", "Document", "n1")
        assert conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0] == 0
    finally:
        sql.close()


def test_direct_registry_write_requires_validated_unit(stores):
    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            sql.register_node("resource", "Document", "n1")
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                sql.register_node("resource", "Document", "n1")
        assert conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0] == 0
    finally:
        sql.close()


def test_registry_lifecycle_missing_pack_returns_false_in_single_db(stores):
    from opencrab.auth import create_user
    from opencrab.pack.ownership import (
        delete_pack_row,
        mark_pack_partial,
        mark_pack_ready,
        promote_partial_pack,
    )

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        for operation in (mark_pack_ready, mark_pack_partial, promote_partial_pack, delete_pack_row):
            assert operation(sql, "missing", owner) is False
    finally:
        sql.close()


def test_registry_lifecycle_rejects_pending_deletion(stores):
    from opencrab.auth import create_user
    from opencrab.pack.ownership import (
        begin_pack_creation,
        delete_pack_row,
        mark_pack_partial,
        mark_pack_ready,
    )

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    begin_pack_creation(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        for operation in (mark_pack_ready, mark_pack_partial, delete_pack_row):
            with pytest.raises(RuntimeError, match="pending"):
                operation(sql, "p", owner)
        assert conn.execute("SELECT status FROM packs WHERE pack_id='p'").fetchone()[0] == "creating"
    finally:
        sql.close()


def test_guarded_pack_cannot_authorize_another_pack(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'blocked', 'second', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                docs.upsert_source("s1", "text", {"pack_id": "blocked"})
        assert conn.execute("SELECT count(*) FROM doc_sources WHERE source_id='s1'").fetchone()[0] == 0
    finally:
        sql.close()


def test_nested_pending_source_rolls_back_outer_work(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(RuntimeError, match="rollback-only"):
                with shared.unit_of_work() as tx:
                    tx.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('temporary', 'temp', 0)")
                    result = write_source(sql, hybrid, docs, None, graph=graph, text="text",
                                          source_id="s1", metadata={}, pack_id="p", write_vector=False)
                    assert result["stores"]["documents"].startswith("error:")
        assert conn.execute("SELECT count(*) FROM users WHERE user_id='temporary'").fetchone()[0] == 0
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_guarded_pack_cannot_rewrite_another_pack_source(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    docs.upsert_source("s1", "text", {"pack_id": "blocked"})
    state = docs.read_source_state_exact("s1")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                docs.rewrite_source_metadata("s1", {"pack_id": "allowed"}, state)
        assert docs.get_source("s1")["metadata"]["pack_id"] == "blocked"
    finally:
        sql.close()


def test_guarded_pack_cannot_delete_another_pack_node_doc(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    docs.upsert_node_doc("resource", "Document", "n1", {"pack_id": "blocked"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                docs.delete_node_doc("resource", "n1")
        assert conn.execute("SELECT count(*) FROM doc_nodes WHERE node_id='n1'").fetchone()[0] == 1
    finally:
        sql.close()


def test_guarded_pack_cannot_delete_another_pack_source(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    docs.upsert_source("s1", "text", {"pack_id": "blocked"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                docs.delete_sources_with_state(["s1"])
        assert conn.execute("SELECT count(*) FROM doc_sources WHERE source_id='s1'").fetchone()[0] == 1
    finally:
        sql.close()


def test_guarded_pack_cannot_delete_another_pack_vector(stores):
    path, shared, _graph, _docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    vec.upsert_texts(["text"], [{"pack_id": "blocked"}], ["s1"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                vec.delete(["s1"])
        assert vec.get_by_id("s1") is not None
    finally:
        sql.close()


def test_guarded_pack_cannot_write_another_pack_node_doc(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                docs.upsert_node_doc("resource", "Document", "n1", {"pack_id": "blocked"})
        assert conn.execute("SELECT count(*) FROM doc_nodes WHERE node_id='n1'").fetchone()[0] == 0
    finally:
        sql.close()


def test_guarded_pack_cannot_update_another_pack_graph_edge(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Document", "n1", {"pack_id": "blocked"})
    graph.upsert_node("Document", "n2", {"pack_id": "blocked"})
    graph.upsert_edge("Document", "n1", "cites", "Document", "n2", {"pack_id": "blocked"})
    digest = graph.get_edge_digest("n1", "cites", "n2", from_type="Document", to_type="Document")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                graph.update_edge("Document", "n1", "cites", "Document", "n2", {"pack_id": "blocked", "x": 1},
                                  expected_current_digest=digest, owner_pack_id="blocked")
        assert graph.get_edge("Document", "n1", "cites", "Document", "n2").get("x") is None
    finally:
        sql.close()


def test_guarded_pack_cannot_delete_another_pack_graph_edge(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Document", "n1", {"pack_id": "blocked"})
    graph.upsert_node("Document", "n2", {"pack_id": "blocked"})
    graph.upsert_edge("Document", "n1", "cites", "Document", "n2", {"pack_id": "blocked"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                graph.delete_edge("n1", "cites", "n2", owner_pack_id="blocked")
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
    finally:
        sql.close()


def test_guarded_pack_cannot_update_another_pack_graph_node(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Document", "n1", {"pack_id": "blocked"})
    digest = graph.get_node_digest("n1")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                graph.update_node("n1", digest, "Document", {"pack_id": "allowed"})
        assert graph.get_node("Document", "n1")["pack_id"] == "blocked"
    finally:
        sql.close()


def test_single_db_graph_batch_rejects_unproven_scope(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                graph.upsert_nodes_batch([{"node_id": "n1", "node_type": "Document",
                                           "properties": {"pack_id": "blocked"}}])
        assert graph.get_node("Document", "n1") is None
    finally:
        sql.close()


def test_guarded_pack_cannot_write_another_pack_graph_edge(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Document", "n1", {"pack_id": "allowed"})
    graph.upsert_node("Document", "n2", {"pack_id": "allowed"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                graph.upsert_edge("Document", "n1", "cites", "Document", "n2", {"pack_id": "blocked"})
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 0
    finally:
        sql.close()


def test_guarded_pack_cannot_delete_another_pack_graph_node(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Document", "n1", {"pack_id": "blocked"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                graph.delete_node("Document", "n1")
        assert graph.get_node("Document", "n1") is not None
    finally:
        sql.close()


def test_guarded_pack_cannot_write_another_pack_node(stores):
    path, shared, graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'blocked', 'second', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                graph.upsert_node("Document", "n1", {"pack_id": "blocked"})
        assert graph.get_node("Document", "n1") is None
    finally:
        sql.close()


def test_guarded_pack_cannot_import_another_pack_vector(stores):
    path, shared, _graph, _docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    record = {"id": "s1", "document": "text", "metadata": {"pack_id": "blocked"}, "embedding": _embed(["text"])[0]}
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                vec.import_vectors([record], pack_id="blocked")
        assert vec.get_by_id("s1") is None
    finally:
        sql.close()


def test_guarded_pack_cannot_write_another_pack_vector(stores):
    path, shared, _graph, _docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'blocked', 'second', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                vec.upsert_precomputed(["text"], [{"pack_id": "blocked"}], ["s1"], [_embed(["text"])[0]])
        assert vec.get_by_id("s1") is None
    finally:
        sql.close()


def test_direct_store_write_rejects_pending_pack(stores):
    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            docs.upsert_source("s1", "text", {"pack_id": "p"})
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                docs.upsert_source("s1", "text", {"pack_id": "p"})
        assert conn.execute("SELECT count(*) FROM doc_sources WHERE source_id='s1'").fetchone()[0] == 0
    finally:
        sql.close()


def test_single_db_fork_refuses_before_destination_reservation(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.fork import fork_pack
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "origin")
    conn = shared.connection()
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    conn.commit()
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    try:
        result = fork_pack(sql, graph, docs, vec, hybrid, OntologyBuilder(graph, docs, sql, vec=vec),
                           principal=Principal(user_id=owner, is_local=True, disabled=False),
                           src_pack_id="origin", new_pack_id="copy")
        assert result["error"] == "single SQLite fork is not yet supported"
        assert conn.execute("SELECT count(*) FROM packs WHERE pack_id='copy'").fetchone()[0] == 0
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_single_db_incremental_nodes_rejects_unprotected_cleanup(stores, tmp_path):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.load import load_nodes_incremental
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    conn.commit()
    source = tmp_path / "nodes.jsonl"
    source.write_text("", encoding="utf-8")
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(RuntimeError, match="single SQLite"):
                load_nodes_incremental("p", source, OntologyBuilder(graph, docs, sql, vec=vec),
                                       {}, {}, graph, docs, {}, vec=vec, sql=sql)
    finally:
        sql.close()


def test_single_db_incremental_finalize_rejects_unprotected_delete(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.pack.load import incremental_finalize
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    conn.commit()
    live = {"nodes": {}, "chunks": {}, "edges": {}, "vec_ids": set(), "doc_node_spaces": {}}
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(RuntimeError, match="single SQLite"):
                incremental_finalize("p", graph, docs, vec, live, set(), set(), set(), False, 0, 0, sql=sql)
    finally:
        sql.close()


def test_single_file_delete_rejects_legacy_journal_path(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.pack.load import delete_pack
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    conn.commit()
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(RuntimeError, match="single.*delet"):
                delete_pack("p", graph, docs, vec, sql=sql)
        assert conn.execute("SELECT count(*) FROM packs WHERE pack_id='p'").fetchone()[0] == 1
    finally:
        sql.close()


def test_single_file_delete_reservation_rechecks_authorization(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.delete_journal import DeletePackJournalPending
    from opencrab.pack.load import _reserve_single_file_delete
    from opencrab.pack.ownership import PackNotFoundError, create_pack

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    try:
        principal = Principal(user_id=owner, is_local=True, disabled=False)
        other = Principal(user_id="other", is_local=True, disabled=False)
        with pytest.raises(PackNotFoundError):
            _reserve_single_file_delete(shared, sql, other, "p")
        assert conn.execute("SELECT count(*) FROM pack_delete_operations").fetchone()[0] == 0
        with pytest.raises(RuntimeError, match="rollback-only"):
            with shared.unit_of_work():
                with pytest.raises(RuntimeError, match="separate transaction"):
                    _reserve_single_file_delete(shared, sql, principal, "p")
        operation, incarnation = _reserve_single_file_delete(shared, sql, principal, "p")
        assert incarnation == "a" * 32
        assert len(operation) == 32
        assert tuple(conn.execute("SELECT operation_id, state FROM pack_delete_operations").fetchone()) == (operation, "pending")
        with pytest.raises(DeletePackJournalPending, match="pending"):
            _reserve_single_file_delete(shared, sql, principal, "p")
        assert conn.execute("SELECT count(*) FROM pack_delete_operations").fetchone()[0] == 1
    finally:
        sql.close()


def test_delete_reservation_survives_journal_failure(stores, monkeypatch, tmp_path):
    from opencrab.auth import Principal, create_user
    from opencrab.pack import delete_journal
    from opencrab.pack.load import _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()

    def fail_journal(*_args, **_kwargs):
        raise OSError("journal failure")

    monkeypatch.setattr(delete_journal, "save_journal", fail_journal)
    try:
        principal = Principal(user_id=owner, is_local=True, disabled=False)
        with pytest.raises(OSError, match="journal failure"):
            _start_single_file_delete(shared, sql, principal, "p", str(tmp_path))
        row = conn.execute("SELECT pack_id, incarnation, state FROM pack_delete_operations").fetchone()
        assert tuple(row) == ("p", "a" * 32, "pending")
    finally:
        sql.close()


@pytest.mark.parametrize("fail", [False, True])
def test_single_file_delete_apply_is_atomic(stores, fail, monkeypatch):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    sql.register_node("concept", "Thing", "n")
    docs.upsert_source("s", "text", {"pack_id": "p"})
    vec.upsert_texts(["text"], [{"pack_id": "p"}], ["s"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    epoch = vec._ann_epoch
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        if fail:
            def fail_delete(*_args, **_kwargs):
                raise RuntimeError("injected graph failure")
            monkeypatch.setattr("opencrab.pack.load._single_file_delete_evidence", fail_delete)
            with pytest.raises(RuntimeError, match="injected"):
                _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
            assert vec._ann_epoch == epoch
            assert _counts(path) == (1, 1, 1)
            assert conn.execute("SELECT count(*) FROM doc_source_integrity").fetchone()[0] == 1
            assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
            assert conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0] == 1
        else:
            assert _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec) == (1, 1, 1)
            assert vec._ann_epoch == epoch + 1  # the ANN cache was dropped after the commit
            assert _counts(path) == (0, 0, 0)
            assert conn.execute("SELECT count(*) FROM doc_source_integrity").fetchone()[0] == 0
            assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "committed"
            assert conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0] == 1
    finally:
        sql.close()


def test_single_file_resume_committed_operation_without_redelete(stores, monkeypatch):
    from opencrab.auth import Principal, create_user
    from opencrab.pack import delete_journal
    from opencrab.pack.load import (
        _apply_single_file_delete,
        _resume_single_file_delete,
        _start_single_file_delete,
    )
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    data_dir = os.path.dirname(path)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", data_dir)
        with pytest.raises(delete_journal.DeletePackJournalConflict, match="identity"):
            _resume_single_file_delete(shared, sql, principal, "p", data_dir, {**journal, "incarnation": "b" * 32}, graph, docs, vec)
        assert _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec) == (0, 0, 0)
        def fail_apply(*_args, **_kwargs):
            raise AssertionError("must not delete twice")
        monkeypatch.setattr("opencrab.pack.load._apply_single_file_delete", fail_apply)
        assert _resume_single_file_delete(shared, sql, principal, "p", data_dir, journal, graph, docs, vec) == (0, 0, 0)
        assert delete_journal.load_journal(data_dir, "p", schema=2)["state"] == "committed"
    finally:
        sql.close()


@pytest.mark.parametrize("table", ["doc_sources", "graph_nodes", "pack_delete_operations"])
def test_single_file_delete_rolls_back_on_sql_axis_failure(stores, table):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    docs.upsert_source("s", "text", {"pack_id": "p"})
    vec.upsert_texts(["text"], [{"pack_id": "p"}], ["s"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    action = "UPDATE" if table == "pack_delete_operations" else "DELETE"
    conn.execute(f"CREATE TRIGGER fail_axis BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'axis failure'); END")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(sqlite3.IntegrityError, match="axis failure"):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert _counts(path) == (1, 1, 1)
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='n'").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        sql.close()


@pytest.mark.parametrize("tag,selected", [(False, True), (0, True), ([], False), ({}, False), (1, False), (True, False)])
def test_single_file_delete_scope_uses_source_owner_predicate(stores, tag, selected):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    docs.upsert_source("s", "text", {"source": "p", "pack_id": tag})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, predecessor TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("CREATE TABLE pack_delete_active (pack_id TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        result = _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert result == (0, int(selected), 0)
        assert conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0] == int(not selected)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "committed"
    finally:
        sql.close()


@pytest.mark.parametrize("kind", ["nodes", "edges", "doc_twins", "doc_extras", "sources", "vectors"])
def test_single_file_delete_rejects_omitted_scope_key(stores, monkeypatch, kind):
    from opencrab.auth import Principal, create_user
    from opencrab.pack import load
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "n", {"pack_id": "p"})
    docs.upsert_node_doc("concept", "Thing", "n", {"pack_id": "p"})
    docs.upsert_node_doc("concept", "Thing", "extra", {"pack_id": "p"})
    docs.upsert_source("s", "text", {"pack_id": "p"})
    vec.upsert_texts(["text"], [{"pack_id": "p"}], ["s"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    original = load._single_file_delete_scope
    def omit(*args):
        scope = original(*args)
        assert scope[kind]
        scope[kind] = []
        return scope
    try:
        journal = load._start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        monkeypatch.setattr(load, "_single_file_delete_scope", omit)
        with pytest.raises(RuntimeError, match="scope"):
            load._apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        sql.close()


@pytest.mark.parametrize("tag", [None, "", "q"])
def test_single_file_delete_independent_source_fallback(stores, tag):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    create_pack(sql, owner, "q")
    docs.upsert_source("s", "text", {"source": "p", "pack_id": tag})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        result = _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert result == (0, 0 if tag == "q" else 1, 0)
        assert conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0] == (1 if tag == "q" else 0)
    finally:
        sql.close()


@pytest.mark.parametrize("space", [None, "other", "", 1, False, [], {}, [1], {"x": 1}])
def test_single_file_delete_independent_space_contract(stores, space):
    import json

    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    conn = shared.connection()
    conn.execute("UPDATE graph_nodes SET properties=? WHERE node_id='n'", (json.dumps({"pack_id": "p", "space": space}),))
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        if space is None or isinstance(space, str) and space:
            assert _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec) == (1, 0, 0)
        else:
            with pytest.raises((RuntimeError, TypeError)):
                _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
            assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        sql.close()


def test_single_file_delete_scope_matches_explicit_keys(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import (
        _apply_single_file_delete,
        _single_file_delete_scope,
        _start_single_file_delete,
    )
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    create_pack(sql, owner, "q")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_node("Thing", "qnode", {"pack_id": "q"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "qnode", {"pack_id": "q"})
    docs.upsert_node_doc("concept", "Thing", "n", {"pack_id": "q"})
    docs.upsert_node_doc("concept", "Thing", "extra", {"pack_id": "p"})
    docs.upsert_source("s", "text", {"pack_id": "p"})
    vec.upsert_texts(["text"], [{"pack_id": "p"}], ["s"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    expected = {
        "nodes": {("Thing", "n", "concept")},
        "edges": {("n", "rel", "qnode")},
        "doc_twins": {("concept", "n")},
        "doc_extras": {("concept", "extra")},
        "sources": {("s",)},
        "vectors": {("s",)},
    }
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with shared.unit_of_work() as tx:
            scope = _single_file_delete_scope(tx, "p", graph, docs, vec)
            assert {kind: set(keys) for kind, keys in scope.items()} == expected
        assert _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec) == (2, 1, 1)
        assert conn.execute("SELECT node_id FROM graph_nodes").fetchone()[0] == "qnode"
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM doc_nodes").fetchone()[0] == 0
    finally:
        sql.close()


def test_single_file_delete_detects_reintroduced_edge_before_commit(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "n", {"pack_id": "p"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("CREATE TRIGGER restore_edge AFTER DELETE ON graph_nodes BEGIN INSERT INTO graph_edges (from_id, relation, to_id, from_type, to_type, properties) VALUES ('n','rel','n','Thing','Thing','{}'); END")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(RuntimeError, match="remaining"):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
        assert conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0] == 1
    finally:
        sql.close()


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_shared_lifetime_lock_survives_close_interrupt(tmp_path, error):
    from opencrab.stores._single_file import failed_shared_file

    class FailingClose:
        fail = True

        def close(self):
            if self.fail:
                raise error("close interrupted")

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    conn = FailingClose()
    shared.all_conns.append(conn)
    with pytest.raises(error, match="close interrupted"):
        shared.close()
    assert failed_shared_file(str(path)) is shared
    assert shared._lifetime_lock is not None and shared.all_conns == [conn]
    conn.fail = False
    shared.close()
    assert failed_shared_file(str(path)) is None


def test_shared_lifetime_lock_survives_connection_close_failure(tmp_path):
    class FailingClose:
        def __init__(self):
            self.fail = True

        def close(self):
            if self.fail:
                raise OSError("injected close failure")

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    conn = FailingClose()
    shared.all_conns.append(conn)
    with pytest.raises(RuntimeError, match="close"):
        shared.close()
    assert shared._lifetime_lock is not None
    assert shared.all_conns == [conn]
    probe = tmp_path / "try_exclusive.py"
    probe.write_text(
        "import sys\n"
        "from opencrab.locking import acquire_file_lock\n"
        "acquire_file_lock('single.db.lifetime.lock', sys.argv[1], timeout=0)\n"
    )
    result = subprocess.run([sys.executable, str(probe), str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "TimeoutError" in result.stderr
    from opencrab.locking import file_lock

    with pytest.raises((TimeoutError, RuntimeError)):
        with file_lock("single.db.lifetime.lock", str(tmp_path), timeout=0):
            pytest.fail("exclusive lock must not be acquired")
    conn.fail = False
    shared.close()
    assert shared._lifetime_lock is None
    with file_lock("single.db.lifetime.lock", str(tmp_path), timeout=0):
        pass


def test_last_owner_can_retry_failed_shared_close(tmp_path):
    class FailingClose:
        def close(self):
            raise OSError("injected close failure")

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    owner = object()
    shared.acquire(owner, None)
    conn = FailingClose()
    shared.all_conns.append(conn)
    with pytest.raises(RuntimeError, match="close"):
        shared.release(owner)
    assert shared.all_conns == [conn]
    assert shared._lifetime_lock is not None
    conn.close = lambda: None
    shared.release(owner)
    assert shared._lifetime_lock is None
    assert shared.all_conns == []


@pytest.mark.parametrize("operation", ["disable", "enable", "issue", "revoke"])
def test_auth_writes_join_shared_rollback(stores, operation):
    from opencrab.auth import create_user, disable_user, enable_user, issue_token, revoke_token

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    user = create_user(sql, "owner", is_local=True)
    token_id, _ = issue_token(sql, user)
    if operation == "enable":
        disable_user(sql, user)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    before_user = conn.execute("SELECT disabled FROM users WHERE user_id=?", (user,)).fetchone()[0]
    before_tokens = [tuple(row) for row in conn.execute("SELECT token_id, revoked_at FROM api_tokens ORDER BY token_id")]
    try:
        with pytest.raises(RuntimeError, match="forced rollback"):
            with shared.unit_of_work():
                if operation == "disable":
                    disable_user(sql, user)
                elif operation == "enable":
                    enable_user(sql, user)
                elif operation == "issue":
                    issue_token(sql, user)
                else:
                    revoke_token(sql, token_id)
                raise RuntimeError("forced rollback")
        assert conn.execute("SELECT disabled FROM users WHERE user_id=?", (user,)).fetchone()[0] == before_user
        assert [tuple(row) for row in conn.execute("SELECT token_id, revoked_at FROM api_tokens ORDER BY token_id")] == before_tokens
    finally:
        sql.close()


def test_auth_reads_newly_issued_token_in_shared_uow(stores):
    from opencrab.auth import create_user, issue_token, list_tokens, verify_token

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    user = create_user(sql, "owner", is_local=True)
    try:
        with pytest.raises(RuntimeError, match="forced rollback"):
            with shared.unit_of_work():
                token_id, secret = issue_token(sql, user)
                assert verify_token(sql, secret).user_id == user
                assert {token["token_id"] for token in list_tokens(sql, user)} == {token_id}
                raise RuntimeError("forced rollback")
        assert verify_token(sql, secret) is None
        assert list_tokens(sql, user) == []
    finally:
        sql.close()


def test_auth_verify_sees_uncommitted_user_disable(stores):
    from opencrab.auth import (
        create_user,
        disable_user,
        get_local_user,
        issue_token,
        list_tokens,
        list_users,
        verify_token,
    )

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    user = create_user(sql, "owner", is_local=True)
    _token_id, secret = issue_token(sql, user)
    try:
        with pytest.raises(RuntimeError, match="forced rollback"):
            with shared.unit_of_work():
                assert disable_user(sql, user)
                assert verify_token(sql, secret) is None
                assert get_local_user(sql).disabled
                assert list_users(sql)[0]["disabled"]
                from opencrab.auth import enable_user, revoke_token

                assert enable_user(sql, user)
                revoke_token(sql, _token_id)
                assert verify_token(sql, secret) is None
                assert list_tokens(sql, user)[0]["revoked_at"] is not None
                raise RuntimeError("forced rollback")
        assert verify_token(sql, secret) is not None
    finally:
        sql.close()


def test_shared_close_retry_keeps_successful_connection_closed(tmp_path):
    class Connection:
        def __init__(self, fail):
            self.fail = fail
            self.calls = 0

        def close(self):
            self.calls += 1
            if self.fail:
                raise OSError("injected close failure")

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    first, second = Connection(False), Connection(True)
    shared.all_conns.extend([first, second])
    with pytest.raises(RuntimeError, match="close"):
        shared.close()
    assert shared.all_conns == [second]
    assert first.calls == 1
    second.fail = False
    shared.close()
    assert first.calls == 1
    assert second.calls == 2
    assert shared._lifetime_lock is None


def test_sqlstore_can_retry_failed_shared_close(tmp_path):
    class FailingClose:
        def close(self):
            raise OSError("injected close failure")

    path = tmp_path / "single.db"
    sqlite3.connect(path).close()
    shared = SharedSqliteFile(str(path), checked=True)
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = FailingClose()
    shared.all_conns.append(conn)
    with pytest.raises(RuntimeError, match="close"):
        sql.close()
    assert sql._shared_registered
    assert shared._lifetime_lock is not None
    conn.close = lambda: None
    sql.close()
    assert not sql._shared_registered
    assert shared._lifetime_lock is None


def test_single_file_uow_waits_for_backup_write_lock(stores):
    from opencrab.auth import create_user
    from opencrab.locking import write_lock

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    started = threading.Event()
    completed = threading.Event()
    errors = []
    users = []

    def writer():
        try:
            started.set()
            users.append(create_user(sql, "writer"))
            completed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=writer)
            thread.start()
            assert started.wait(5)
            assert not completed.wait(0.2)
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert completed.is_set()
        assert conn.execute("SELECT user_id FROM users").fetchone()[0] == users[0]
    finally:
        sql.close()


@pytest.mark.parametrize("lock_name", ["billing.db.lock", "delete-pack-example.lock"])
def test_single_file_independent_writer_rejects_lock_inversion(stores, lock_name):
    from opencrab.auth import create_user
    from opencrab.locking import file_lock

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        with file_lock(lock_name, os.path.dirname(path)):
            with pytest.raises(RuntimeError, match="lock order"):
                create_user(sql, "writer")
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    finally:
        sql.close()


@pytest.mark.parametrize("operation", ["disable", "enable", "issue", "revoke"])
def test_single_file_auth_writer_waits_for_backup_lock(stores, operation):
    from opencrab.auth import create_user, disable_user, enable_user, issue_token, revoke_token
    from opencrab.locking import write_lock

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    user_id = create_user(sql, "writer")
    token_id, _secret = issue_token(sql, user_id)
    if operation == "enable":
        disable_user(sql, user_id)
    started, completed = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            started.set()
            if operation == "disable":
                disable_user(sql, user_id)
            elif operation == "enable":
                enable_user(sql, user_id)
            elif operation == "issue":
                issue_token(sql, user_id)
            else:
                revoke_token(sql, token_id)
            completed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=writer)
            thread.start()
            assert started.wait(5)
            assert not completed.wait(0.2)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert completed.is_set()
        conn = shared.connection()
        if operation in ("disable", "enable"):
            assert conn.execute("SELECT disabled FROM users WHERE user_id=?", (user_id,)).fetchone()[0] == (operation == "disable")
        elif operation == "issue":
            assert conn.execute("SELECT count(*) FROM api_tokens WHERE user_id=?", (user_id,)).fetchone()[0] == 2
        else:
            assert conn.execute("SELECT revoked_at FROM api_tokens WHERE token_id=?", (token_id,)).fetchone()[0] is not None
    finally:
        sql.close()


def test_single_file_writer_reenters_existing_write_lock(stores):
    from opencrab.auth import create_user
    from opencrab.locking import file_lock, write_lock

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        with write_lock(os.path.dirname(path)):
            with file_lock("delete-pack-example.lock", os.path.dirname(path)):
                user_id = create_user(sql, "writer")
            with file_lock("billing.db.lock", os.path.dirname(path)):
                assert conn.execute("SELECT user_id FROM users").fetchone()[0] == user_id
    finally:
        sql.close()


@pytest.mark.parametrize("operation", ["request", "resolve", "create_run", "advance"])
def test_single_file_execution_writer_waits_for_backup_lock(stores, operation):
    from opencrab.execution.approvals import ApprovalEngine
    from opencrab.execution.workflow import WorkflowEngine
    from opencrab.locking import write_lock

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    approvals, workflow = ApprovalEngine(sql), WorkflowEngine(sql)
    approval_id = approvals.request("test", "subject", {})["approval_id"]
    run_id = workflow.create_run("test", {})["run_id"]
    started, completed = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            started.set()
            if operation == "request":
                approvals.request("other", "subject", {})
            elif operation == "resolve":
                approvals.resolve(approval_id, "approved")
            elif operation == "create_run":
                workflow.create_run("other", {})
            else:
                workflow.advance(run_id, "running")
            completed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=writer)
            thread.start()
            assert started.wait(5)
            assert not completed.wait(0.2)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert completed.is_set()
        conn = shared.connection()
        if operation == "request":
            assert conn.execute("SELECT count(*) FROM approval_queue").fetchone()[0] == 2
        elif operation == "resolve":
            assert conn.execute("SELECT status FROM approval_queue WHERE approval_id=?", (approval_id,)).fetchone()[0] == "approved"
        elif operation == "create_run":
            assert conn.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 2
        else:
            assert conn.execute("SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()[0] == "running"
    finally:
        sql.close()


def test_single_file_execution_writes_join_outer_rollback(stores):
    from opencrab.execution.approvals import ApprovalEngine
    from opencrab.execution.workflow import WorkflowEngine

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    approvals, workflow = ApprovalEngine(sql), WorkflowEngine(sql)
    approval_id = approvals.request("test", "subject", {})["approval_id"]
    run_id = workflow.create_run("test", {})["run_id"]
    try:
        with pytest.raises(ValueError, match="abort"):
            with shared.unit_of_work():
                approvals.request("other", "subject", {})
                approvals.resolve(approval_id, "approved")
                workflow.create_run("other", {})
                workflow.advance(run_id, "running")
                raise ValueError("abort")
        conn = shared.connection()
        assert conn.execute("SELECT count(*) FROM approval_queue").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM approval_queue").fetchone()[0] == "pending"
        assert conn.execute("SELECT count(*) FROM workflow_runs").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM workflow_runs").fetchone()[0] == "pending"
        assert conn.execute("SELECT count(*) FROM action_log").fetchone()[0] == 1
    finally:
        sql.close()


@pytest.mark.parametrize("operation", ["alias", "propose", "resolve"])
def test_single_file_identity_writer_waits_for_backup_lock(stores, operation):
    from opencrab.locking import write_lock
    from opencrab.ontology.identity import IdentityEngine

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    identity = IdentityEngine(sql)
    candidate_id = identity.propose_duplicate("a", "b")["candidate_id"]
    started, completed = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            started.set()
            if operation == "alias":
                identity.add_alias("c", "d")
            elif operation == "propose":
                identity.propose_duplicate("c", "d")
            else:
                identity.resolve_duplicate(candidate_id, "accepted")
            completed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=writer)
            thread.start()
            assert started.wait(5)
            assert not completed.wait(0.2)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert completed.is_set()
        conn = shared.connection()
        if operation == "alias":
            assert conn.execute("SELECT count(*) FROM node_aliases WHERE canonical_id='c'").fetchone()[0] == 1
        elif operation == "propose":
            assert conn.execute("SELECT count(*) FROM duplicate_candidates").fetchone()[0] == 2
        else:
            assert conn.execute("SELECT status FROM duplicate_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()[0] == "accepted"
            assert conn.execute("SELECT count(*) FROM node_aliases WHERE alias_type='merge'").fetchone()[0] == 1
    finally:
        sql.close()


def test_single_file_identity_writes_join_outer_rollback(stores):
    from opencrab.ontology.identity import IdentityEngine

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    identity = IdentityEngine(sql)
    candidate_id = identity.propose_duplicate("a", "b")["candidate_id"]
    try:
        with pytest.raises(ValueError, match="abort"):
            with shared.unit_of_work():
                identity.add_alias("c", "d")
                identity.propose_duplicate("e", "f")
                identity.resolve_duplicate(candidate_id, "accepted")
                raise ValueError("abort")
        conn = shared.connection()
        assert conn.execute("SELECT count(*) FROM node_aliases").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM duplicate_candidates").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM duplicate_candidates").fetchone()[0] == "pending"
    finally:
        sql.close()


@pytest.mark.parametrize("kind", ["impact", "simulation", "policy"])
def test_single_file_sql_writer_waits_for_backup_lock(stores, kind):
    from opencrab.locking import write_lock

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    started, completed = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            started.set()
            if kind == "impact":
                sql.save_impact("n", "update", {})
            elif kind == "simulation":
                sql.save_simulation("n", "up", 1.0, {})
            else:
                sql.set_policy("u", "read", "n")
            completed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=writer)
            thread.start()
            assert started.wait(5)
            assert not completed.wait(0.2)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert completed.is_set()
        table = {"impact": "impact_records", "simulation": "lever_simulations", "policy": "rebac_policies"}[kind]
        assert shared.connection().execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    finally:
        sql.close()


@pytest.mark.parametrize("kind", ["create", "default", "ready", "visibility"])
def test_single_file_pack_writer_waits_for_backup_lock(stores, kind):
    from opencrab.auth import Principal, create_user
    from opencrab.locking import write_lock
    from opencrab.pack.ownership import (
        begin_pack_creation,
        create_pack,
        ensure_default_pack,
        mark_pack_ready,
        set_visibility,
    )

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("CREATE TRIGGER new_pack_incarnation AFTER INSERT ON packs WHEN NEW.incarnation IS NULL BEGIN UPDATE packs SET incarnation=NEW.pack_id WHERE pack_id=NEW.pack_id; END")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    if kind == "ready":
        begin_pack_creation(sql, owner, "p")
    elif kind == "visibility":
        create_pack(sql, owner, "p")
    if kind == "default":
        conn.execute("DELETE FROM packs WHERE owner_id=?", (owner,))
        conn.commit()
    started, completed = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            started.set()
            if kind == "create":
                create_pack(sql, owner, "p")
            elif kind == "default":
                ensure_default_pack(sql, owner)
            elif kind == "ready":
                mark_pack_ready(sql, "p", owner)
            else:
                set_visibility(sql, principal, "p", "public-read")
            completed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=writer)
            thread.start()
            assert started.wait(5)
            assert not completed.wait(0.2)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert completed.is_set()
        if kind == "default":
            assert conn.execute("SELECT count(*) FROM packs WHERE owner_id=?", (owner,)).fetchone()[0] == 1
        else:
            row = conn.execute("SELECT status, visibility FROM packs WHERE pack_id='p'").fetchone()
            assert row is not None
            if kind == "ready":
                assert row[0] == "ready"
            if kind == "visibility":
                assert row[1] == "public-read"
    finally:
        sql.close()


@pytest.mark.parametrize("changed", ["ready", "root", "database", "db_uuid", "generation", "digest", "marker", "metadata"])
def test_single_file_binding_rejects_mismatch(tmp_path, changed):
    import hashlib

    from opencrab.stores._single_file import validate_single_file_binding

    root = str(tmp_path.resolve())
    db = str(tmp_path / "single.db")
    identity = {"root": root, "database": db, "db_uuid": "a" * 32, "generation": "b" * 32}
    manifest = dict(identity)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    ledger = {**identity, "state": "authorized", "manifest_digest": digest}
    marker = {"db_uuid": identity["db_uuid"], "generation": identity["generation"], "manifest_digest": digest}
    metadata = dict(identity)
    validate_single_file_binding(root, db, ledger, manifest, marker, metadata, digest)
    if changed == "ready":
        ledger["state"] = "ready"
    elif changed in ("root", "database", "db_uuid", "generation"):
        metadata[changed] = "wrong"
    elif changed == "digest":
        ledger["manifest_digest"] = "wrong"
    elif changed == "marker":
        marker["generation"] = "c" * 32
    else:
        metadata.pop("root")
    with pytest.raises(RuntimeError):
        validate_single_file_binding(root, db, ledger, manifest, marker, metadata, digest)


def test_single_file_binding_loads_directory_files(tmp_path):
    import hashlib

    from opencrab.stores._single_file import load_single_file_binding

    root, db = str(tmp_path.resolve()), str(tmp_path / "single.db")
    metadata = {"root": root, "database": db, "db_uuid": "a" * 32, "generation": "b" * 32}
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    ledger = {**metadata, "state": "authorized", "manifest_digest": digest}
    marker = {"db_uuid": "a" * 32, "generation": "b" * 32, "manifest_digest": digest}
    (tmp_path / "ledger").write_text(json.dumps(ledger))
    (tmp_path / "manifest").write_text(json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    (tmp_path / "marker").write_text(json.dumps(marker))
    directory = os.open(tmp_path, os.O_RDONLY)
    try:
        load_single_file_binding(root, db, directory, directory, "ledger", "manifest", "marker", metadata)
        (tmp_path / "ledger").write_text(json.dumps({**ledger, "state": "ready"}))
        with pytest.raises(RuntimeError, match="not authorized"):
            load_single_file_binding(root, db, directory, directory, "ledger", "manifest", "marker", metadata)
    finally:
        os.close(directory)


def test_checked_single_file_binding_requires_lifetime_lock(tmp_path):
    import hashlib

    path = tmp_path / "single.db"
    root, db = str(tmp_path.resolve()), str(path)
    metadata = {"root": root, "database": db, "db_uuid": "a" * 32, "generation": "b" * 32}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE single_file_metadata (root TEXT, database_path TEXT, db_uuid TEXT, generation TEXT)")
        conn.execute("INSERT INTO single_file_metadata VALUES (?, ?, ?, ?)", tuple(metadata.values()))
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    (tmp_path / "ledger").write_text(json.dumps({**metadata, "state": "authorized", "manifest_digest": digest}))
    (tmp_path / "manifest").write_text(json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    (tmp_path / "marker").write_text(json.dumps({"db_uuid": "a" * 32, "generation": "b" * 32, "manifest_digest": digest}))
    directory = os.open(tmp_path, os.O_RDONLY)
    checked = SharedSqliteFile(db, checked=True)
    unchecked = SharedSqliteFile(db)
    try:
        checked.validate_binding(root, directory, directory, "ledger", "manifest", "marker")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE single_file_metadata SET generation=?", ("c" * 32,))
        with pytest.raises(RuntimeError, match="binding differs"):
            checked.validate_binding(root, directory, directory, "ledger", "manifest", "marker")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE single_file_metadata SET generation=?", ("b" * 32,))
        other_path = tmp_path / "other"
        other_path.mkdir()
        other_fd = os.open(other_path, os.O_RDONLY)
        try:
            with pytest.raises(RuntimeError, match="directory differs"):
                checked.validate_binding(root, other_fd, directory, "ledger", "manifest", "marker")
            with pytest.raises(RuntimeError, match="directory differs"):
                checked.validate_binding(root, directory, other_fd, "ledger", "manifest", "marker")
        finally:
            os.close(other_fd)
        with pytest.raises(RuntimeError, match="checked"):
            unchecked.validate_binding(root, directory, directory, "ledger", "manifest", "marker")
        os.replace(path, tmp_path / "old.db")
        sqlite3.connect(path).close()
        with pytest.raises(ValueError, match="identity"):
            checked.validate_binding(root, directory, directory, "ledger", "manifest", "marker")
    finally:
        unchecked.close()
        checked.close()
        os.close(directory)


@pytest.mark.parametrize("raw", [b'[]', b'{"state":"ready","state":"authorized"}', b'{"n":NaN}', b'{'])
def test_binding_json_rejects_invalid_content(raw):
    from opencrab.stores._single_file import decode_binding_json

    with pytest.raises(RuntimeError):
        decode_binding_json(raw)
    assert decode_binding_json(b'{"state":"authorized"}') == {"state": "authorized"}


def test_binding_file_read_checks_file_type_and_identity(tmp_path, monkeypatch):
    from opencrab.stores._single_file import read_binding_file

    path = tmp_path / "ledger"
    path.write_bytes(b"value")
    directory = os.open(tmp_path, os.O_RDONLY)
    try:
        assert read_binding_file(directory, "ledger") == b"value"
        with pytest.raises(ValueError):
            read_binding_file(directory, "../ledger")
        os.link(path, tmp_path / "hardlink")
        with pytest.raises(ValueError, match="one link"):
            read_binding_file(directory, "ledger")
        (tmp_path / "hardlink").unlink()
        (tmp_path / "symlink").symlink_to(path)
        with pytest.raises(OSError):
            read_binding_file(directory, "symlink")
        real_read = os.read

        def replace_after_read(fd, size):
            value = real_read(fd, size)
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"other")
            os.replace(replacement, path)
            return value

        monkeypatch.setattr(os, "read", replace_after_read)
        with pytest.raises(RuntimeError, match="changed"):
            read_binding_file(directory, "ledger")
    finally:
        os.close(directory)


def test_billing_and_write_lock_reject_active_shared_unit(stores, tmp_path):
    from opencrab.billing.hooks import BillingHooks
    from opencrab.locking import write_lock

    _path, shared, _graph, _docs, _vec = stores
    # 단일 모드 billing과 같은 호출 형태다. root 안 billing.db는 allow_single_root로 연다(B11).
    billing = SQLStore(f"sqlite:///{tmp_path / 'billing.db'}", create_tables=False, allow_single_root=True)
    hooks = BillingHooks(billing)
    try:
        with shared.unit_of_work():
            result = hooks.emit("query")
            assert result["ok"] is False
            assert "active SQLite unit" in result["error"]
            with pytest.raises(RuntimeError, match="active SQLite unit"):
                with write_lock(str(tmp_path)):
                    pass
        assert hooks.emit("query")["ok"] is True
        with billing._engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM billing_events").scalar_one() == 1
    finally:
        billing.close()


def test_single_file_delete_serializes_other_connection(stores, monkeypatch):
    from opencrab.auth import Principal, create_user
    from opencrab.pack import load
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    journal = load._start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
    entered = threading.Event()
    release = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    errors = []
    original_scope = load._single_file_delete_scope

    def pause_scope(*args):
        if not entered.is_set():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("delete test timed out")
        return original_scope(*args)

    def delete():
        try:
            load._apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        except Exception as exc:
            errors.append(exc)

    def write():
        try:
            with sqlite3.connect(path, timeout=5) as other:
                writer_started.set()
                other.execute("BEGIN IMMEDIATE")
                assert other.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "committed"
            writer_finished.set()
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(load, "_single_file_delete_scope", pause_scope)
    deleter = threading.Thread(target=delete)
    writer = threading.Thread(target=write)
    try:
        deleter.start()
        assert entered.wait(5)
        writer.start()
        assert writer_started.wait(5)
        assert not writer_finished.wait(0.2)
    finally:
        release.set()
        deleter.join(5)
        if writer.ident is not None:
            writer.join(5)
        sql.close()
    assert not deleter.is_alive()
    assert not writer.is_alive()
    assert not errors
    assert writer_finished.is_set()


def test_single_file_delete_rolls_back_on_vector_failure(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    docs.upsert_node_doc("concept", "Thing", "n", {"pack_id": "p"})
    docs.upsert_source("s", "text", {"pack_id": "p"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("CREATE TABLE broken_vectors (node_id TEXT, pack_id TEXT)")
    conn.execute("INSERT INTO broken_vectors VALUES ('s', 'p')")
    conn.execute("CREATE TRIGGER fail_vector BEFORE DELETE ON broken_vectors BEGIN SELECT RAISE(ABORT, 'vector failure'); END")
    conn.commit()
    original_table = vec._table
    vec._table = "broken_vectors"
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(sqlite3.IntegrityError, match="vector failure"):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert conn.execute("SELECT count(*) FROM doc_nodes WHERE node_id='n'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='n'").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        vec._table = original_table
        sql.close()


def test_single_file_delete_rolls_back_on_node_index_failure(stores, monkeypatch):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack
    from opencrab.stores import _node_bm25_sqlite

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    docs.upsert_node_doc("concept", "Thing", "n", {"pack_id": "p"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    def fail_index(*_args, **_kwargs):
        raise RuntimeError("index failure")
    monkeypatch.setattr(docs, "_node_bm25_ok", True)
    monkeypatch.setattr(_node_bm25_sqlite, "reindex_key", fail_index)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(RuntimeError, match="index failure"):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert conn.execute("SELECT count(*) FROM doc_nodes WHERE node_id='n'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='n'").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        sql.close()


def test_single_file_delete_rejects_foreign_pending_reference(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    create_pack(sql, owner, "other")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_node("Thing", "o", {"pack_id": "other"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "o", {"pack_id": "other"})
    docs.upsert_source("s", "text", {"pack_id": "p"})
    vec.upsert_texts(["text"], [{"pack_id": "p"}], ["s"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('foreign', 'other', 'other', 'pending', NULL, NULL)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(RuntimeError, match="pending"):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert _counts(path) == (1, 1, 1)
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM pack_delete_operations WHERE pack_id='p'").fetchone()[0] == "pending"
    finally:
        sql.close()


@pytest.mark.parametrize("kind", ["edges", "doc_twins"])
def test_single_file_history_reference_requires_current_authority(tmp_path, kind):
    from opencrab.pack.delete_journal import DeletePackJournalConflict
    from opencrab.pack.load import (
        _single_file_delete_evidence,
        _single_file_delete_history_references,
    )

    conn = sqlite3.connect(tmp_path / "history.db")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, predecessor TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("CREATE TABLE graph_edges (from_id TEXT, relation TEXT, to_id TEXT)")
    conn.execute("CREATE TABLE doc_nodes (space TEXT, node_id TEXT)")
    keys = {key: [] for key in ("nodes", "edges", "doc_twins", "doc_extras", "sources", "vectors")}
    key = ("a", "rel", "b") if kind == "edges" else ("concept", "a")
    keys[kind] = [key]
    evidence = _single_file_delete_evidence("op", "inc", [0, 0, 0], keys)
    conn.execute("INSERT INTO pack_delete_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("op", "p", "inc", None, "committed", "[0,0,0]", json.dumps(evidence)))
    current = {"edges": [], "doc_twins": []}
    try:
        _single_file_delete_history_references(conn, "p", current)
        if kind == "edges":
            conn.execute("INSERT INTO graph_edges VALUES (?, ?, ?)", key)
        else:
            conn.execute("INSERT INTO doc_nodes VALUES (?, ?)", key)
        with pytest.raises(DeletePackJournalConflict, match="authority"):
            _single_file_delete_history_references(conn, "p", current)
        current[kind].append(key)
        _single_file_delete_history_references(conn, "p", current)
    finally:
        conn.close()


def test_single_file_history_reference_query_failure_is_not_absence(tmp_path):
    from opencrab.pack.load import (
        _single_file_delete_evidence,
        _single_file_delete_history_references,
    )

    conn = sqlite3.connect(tmp_path / "history.db")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, predecessor TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    keys = {key: [] for key in ("nodes", "edges", "doc_twins", "doc_extras", "sources", "vectors")}
    keys["edges"] = [("a", "rel", "b")]
    evidence = _single_file_delete_evidence("op", "inc", [0, 0, 0], keys)
    conn.execute("INSERT INTO pack_delete_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("op", "p", "inc", None, "committed", "[0,0,0]", json.dumps(evidence)))
    try:
        with pytest.raises(sqlite3.OperationalError, match="graph_edges"):
            _single_file_delete_history_references(conn, "p", {"edges": [], "doc_twins": []})
    finally:
        conn.close()


def test_single_file_history_deduplicates_references_and_checks_each_evidence(tmp_path):
    from opencrab.pack.delete_journal import DeletePackJournalConflict
    from opencrab.pack.load import (
        _single_file_delete_evidence,
        _single_file_delete_history_references,
    )

    conn = sqlite3.connect(tmp_path / "history.db")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, predecessor TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("CREATE TABLE graph_edges (from_id TEXT, relation TEXT, to_id TEXT)")
    conn.execute("CREATE TABLE doc_nodes (space TEXT, node_id TEXT)")
    keys = {key: [] for key in ("nodes", "edges", "doc_twins", "doc_extras", "sources", "vectors")}
    keys["edges"] = [("a", "rel", "b")]
    for op, predecessor in (("a", None), ("b", "a"), ("c", "b")):
        evidence = _single_file_delete_evidence(op, "inc", [0, 0, 0], keys)
        conn.execute("INSERT INTO pack_delete_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (op, "p", "inc", predecessor, "committed", "[0,0,0]", json.dumps(evidence)))
    conn.execute("INSERT INTO graph_edges VALUES ('a', 'rel', 'b')")
    current = {"edges": [("a", "rel", "b")], "doc_twins": []}
    try:
        _single_file_delete_history_references(conn, "p", current)
        conn.execute("UPDATE pack_delete_operations SET evidence_json='{}' WHERE operation_id='b'")
        with pytest.raises(DeletePackJournalConflict, match="evidence"):
            _single_file_delete_history_references(conn, "p", current)
    finally:
        conn.close()


def test_single_file_pending_delete_rechecks_historical_reference(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.delete_journal import DeletePackJournalConflict
    from opencrab.pack.load import (
        _apply_single_file_delete,
        _single_file_delete_evidence,
        _start_single_file_delete,
    )
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "target", {"pack_id": "p"}, "concept")
    graph.upsert_node("Thing", "old", {"pack_id": "other"}, "concept")
    graph.upsert_node("Thing", "other", {"pack_id": "other"}, "concept")
    graph.upsert_edge("Thing", "old", "rel", "Thing", "other", {})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='inc'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, predecessor TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("CREATE TABLE pack_delete_active (pack_id TEXT)")
    keys = {key: [] for key in ("nodes", "edges", "doc_twins", "doc_extras", "sources", "vectors")}
    keys["edges"] = [("old", "rel", "other")]
    evidence = _single_file_delete_evidence("oldop", "inc", [0, 0, 0], keys)
    conn.execute("INSERT INTO pack_delete_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("oldop", "p", "inc", None, "committed", "[0,0,0]", json.dumps(evidence)))
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(DeletePackJournalConflict, match="authority"):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='target'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM pack_delete_operations WHERE operation_id=?", (journal["operation_id"],)).fetchone()[0] == "pending"
    finally:
        sql.close()


def test_single_file_history_rejects_cycle_and_damaged_evidence(tmp_path):
    from opencrab.pack.delete_journal import DeletePackJournalConflict
    from opencrab.pack.load import (
        _single_file_delete_evidence,
        _single_file_delete_history_references,
    )

    conn = sqlite3.connect(tmp_path / "history.db")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, predecessor TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.execute("CREATE TABLE graph_edges (from_id TEXT, relation TEXT, to_id TEXT)")
    conn.execute("CREATE TABLE doc_nodes (space TEXT, node_id TEXT)")
    keys = {key: [] for key in ("nodes", "edges", "doc_twins", "doc_extras", "sources", "vectors")}
    for op, predecessor in (("a", "b"), ("b", "a")):
        evidence = _single_file_delete_evidence(op, "inc", [0, 0, 0], keys)
        conn.execute("INSERT INTO pack_delete_operations VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (op, "p", "inc", predecessor, "committed", "[0,0,0]", json.dumps(evidence)))
    current = {"edges": [], "doc_twins": []}
    try:
        with pytest.raises(DeletePackJournalConflict, match="cycle"):
            _single_file_delete_history_references(conn, "p", current)
        conn.execute("UPDATE pack_delete_operations SET predecessor=NULL WHERE operation_id='a'")
        conn.execute("UPDATE pack_delete_operations SET evidence_json='{}' WHERE operation_id='a'")
        with pytest.raises(DeletePackJournalConflict, match="evidence"):
            _single_file_delete_history_references(conn, "p", current)
    finally:
        conn.close()


def test_single_file_resume_rejects_damaged_evidence(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.delete_journal import DeletePackJournalConflict
    from opencrab.pack.load import (
        _apply_single_file_delete,
        _resume_single_file_delete,
        _start_single_file_delete,
    )
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    data_dir = os.path.dirname(path)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", data_dir)
        _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        conn.execute("UPDATE pack_delete_operations SET evidence_json='{}'")
        conn.commit()
        with pytest.raises(DeletePackJournalConflict, match="evidence"):
            _resume_single_file_delete(shared, sql, principal, "p", data_dir, journal, graph, docs, vec)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "committed"
    finally:
        sql.close()


def test_single_file_delete_preserves_unrelated_rows_and_cascades_foreign_reference(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    create_pack(sql, owner, "other")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_node("Thing", "o", {"pack_id": "other"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "o", {"pack_id": "other"})
    docs.upsert_node_doc("concept", "Thing", "n", {"pack_id": "other"})
    docs.upsert_node_doc("concept", "Thing", "o", {"pack_id": "other"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        assert _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec) == (1, 0, 0)
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 0
        assert conn.execute("SELECT node_id FROM graph_nodes").fetchone()[0] == "o"
        assert conn.execute("SELECT node_id FROM doc_nodes").fetchone()[0] == "o"
    finally:
        sql.close()


def test_single_file_resume_rejects_reintroduced_reference(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.delete_journal import DeletePackJournalConflict
    from opencrab.pack.load import (
        _apply_single_file_delete,
        _resume_single_file_delete,
        _start_single_file_delete,
    )
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    docs.upsert_node_doc("concept", "Thing", "n", {})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    data_dir = os.path.dirname(path)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", data_dir)
        _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        conn.execute("INSERT INTO doc_nodes (space,node_id,node_type,properties,updated_at) VALUES ('concept','n','Thing','{}','now')")
        conn.commit()
        with pytest.raises(DeletePackJournalConflict, match="remaining"):
            _resume_single_file_delete(shared, sql, principal, "p", data_dir, journal, graph, docs, vec)
        assert conn.execute("SELECT count(*) FROM doc_nodes WHERE node_id='n'").fetchone()[0] == 1
    finally:
        sql.close()


def test_single_file_delete_state_machine_recovers_pending(stores, monkeypatch):
    from opencrab.auth import Principal, create_user
    from opencrab.pack import delete_journal
    from opencrab.pack.load import _single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    import opencrab.pack.load as load
    original = load._apply_single_file_delete
    def fail_apply(*_args, **_kwargs):
        raise RuntimeError("interrupted")
    monkeypatch.setattr(load, "_apply_single_file_delete", fail_apply)
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            _single_file_delete(shared, sql, principal, "p", graph, docs, vec, False, None)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
        monkeypatch.setattr(load, "_apply_single_file_delete", original)
        with pytest.raises(delete_journal.DeletePackJournalPending):
            _single_file_delete(shared, sql, principal, "p", graph, docs, vec, False, None)
        assert _single_file_delete(shared, sql, principal, "p", graph, docs, vec, True, None) == (0, 0, 0)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "committed"
    finally:
        sql.close()


def test_shared_audit_failure_keeps_best_effort_contract(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with shared.unit_of_work():
            shared.validate_pack_writes({"allowed": "first"})
            with pytest.raises(TypeError):
                docs.log_event("test", None, {"bad": object()})
            docs.upsert_source("s", "text", {"pack_id": "allowed"})
        assert docs.get_source("s") is not None
    finally:
        sql.close()


def test_bootstrap_user_joins_single_file_transaction(stores):
    from opencrab.auth import bootstrap_local_user

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            with shared.unit_of_work():
                bootstrap_local_user(sql)
                raise RuntimeError("rollback")
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM packs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM api_tokens").fetchone()[0] == 0
    finally:
        sql.close()


def test_visibility_change_rejects_pending_deletion(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.ownership import create_pack, set_visibility

    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        principal = Principal(user_id=owner, is_local=True, disabled=False)
        with pytest.raises(RuntimeError, match="pending"):
            set_visibility(sql, principal, "p", "public-read")
        assert conn.execute("SELECT visibility FROM packs WHERE pack_id='p'").fetchone()[0] == "private"
        conn.execute("DELETE FROM pack_delete_operations")
        conn.commit()
        assert set_visibility(sql, principal, "p", "public-read")["visibility"] == "public-read"
    finally:
        sql.close()


@pytest.mark.parametrize("raw", ['{"pack_id":"p","pack_id":"other"}', '{"pack_id":"other","pack_id":"p"}'])
def test_delete_scope_rejects_duplicate_target_node_pack_key(stores, raw):
    from opencrab.auth import create_user
    from opencrab.pack.load import _single_file_delete_scope
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    conn = shared.connection()
    conn.execute("UPDATE graph_nodes SET properties=? WHERE node_id='n'", (raw,))
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="invalid"):
            with shared.unit_of_work():
                _single_file_delete_scope(conn, "p", graph, docs, vec)
        assert conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0] == 1
    finally:
        sql.close()


@pytest.mark.parametrize("target", ["edge", "twin"])
@pytest.mark.parametrize("raw", ['{', '[]', '{"pack_id":"p","pack_id":"q"}', '{"x":NaN}', '{"pack_id":false}', '{"pack_id":"missing"}'])
def test_delete_rejects_invalid_reference_atomically(stores, target, raw):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "n", {})
    docs.upsert_node_doc("concept", "Thing", "n", {})
    conn = shared.connection()
    if target == "edge":
        conn.execute("UPDATE graph_edges SET properties=?", (raw,))
    else:
        conn.execute("UPDATE doc_nodes SET properties=?", (raw,))
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=pack_id")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(RuntimeError):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
        assert conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM doc_nodes").fetchone()[0] == 1
    finally:
        sql.close()


def test_delete_scope_separates_references_and_pending_packs(stores):
    from opencrab.auth import create_user
    from opencrab.pack.load import _single_file_delete_scope
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    create_pack(sql, owner, "other")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_node("Thing", "o", {"pack_id": "other"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "o", {"pack_id": "other"})
    docs.upsert_node_doc("concept", "Thing", "n", {})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("UPDATE packs SET incarnation='second' WHERE pack_id='other'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    try:
        with shared.unit_of_work():
            scope = _single_file_delete_scope(conn, "p", graph, docs, vec)
            assert scope["edges"] == [("n", "rel", "o")]
            assert scope["doc_twins"] == [("concept", "n")]
            assert shared._uow.validated_packs == {}
        conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'other', 'second', 'pending')")
        conn.commit()
        with pytest.raises(RuntimeError, match="pending"):
            with shared.unit_of_work():
                _single_file_delete_scope(conn, "p", graph, docs, vec)
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
    finally:
        sql.close()


def test_delete_evidence_includes_all_kinds_and_is_deterministic():
    import json

    from opencrab.pack.delete_journal import DeletePackJournalConflict
    from opencrab.pack.load import _single_file_delete_evidence, _verify_single_file_delete_evidence

    keys = {"nodes": [["Thing", "n", "concept"]], "edges": [], "doc_twins": [],
            "doc_extras": [], "sources": [["s"]], "vectors": [["s"]]}
    evidence = _single_file_delete_evidence("op", "generation", [1, 1, 1], keys)
    assert evidence["counts"] == {kind: len(rows) for kind, rows in keys.items()}
    assert evidence == _single_file_delete_evidence("op", "generation", [1, 1, 1], keys)
    assert _verify_single_file_delete_evidence(json.dumps(evidence), "op", "generation", [1, 1, 1]) == keys
    for changed in ({**evidence, "sha256": "bad"}, {**evidence, "result": [0, 0, 0]},
                    {**evidence, "keys": {"nodes": []}}):
        with pytest.raises(DeletePackJournalConflict, match="evidence"):
            _verify_single_file_delete_evidence(json.dumps(changed), "op", "generation", [1, 1, 1])
    with pytest.raises(DeletePackJournalConflict, match="evidence"):
        _verify_single_file_delete_evidence('{"keys":{},"keys":{}}', "op", "generation", [1, 1, 1])
    with pytest.raises(RuntimeError, match="incomplete"):
        _single_file_delete_evidence("op", "generation", [1, 1, 1], {"nodes": []})


def test_delete_reference_owner_classifies_strictly(tmp_path):
    from opencrab.pack.load import _single_file_delete_owner

    conn = sqlite3.connect(tmp_path / "owners.db")
    conn.execute("CREATE TABLE packs (pack_id TEXT, incarnation TEXT)")
    conn.execute("INSERT INTO packs VALUES ('p', 'first')")
    try:
        for raw in ('{}', '{"pack_id": null}', '{"pack_id": ""}'):
            assert _single_file_delete_owner(conn, raw) is None
        assert _single_file_delete_owner(conn, '{"pack_id":"p","source":"other"}') == ("p", "first")
        for raw in ('{"pack_id": false}', '{"pack_id": 0}', '{"pack_id":"missing"}',
                    '[]', '{"x":NaN}', '{"pack_id":"p","pack_id":"q"}', '{'):
            with pytest.raises(RuntimeError):
                _single_file_delete_owner(conn, raw)
    finally:
        conn.close()


def test_delete_operation_does_not_authorize_general_writes(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    docs.upsert_source("s", "text", {"pack_id": "p"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('p', 'owner', 'first')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="not validated"):
            with shared.unit_of_work():
                shared.validate_delete_operation("p", "first", "op")
                docs.delete_sources_with_state(["s"])
        assert docs.get_source("s") is not None
    finally:
        sql.close()


@pytest.mark.parametrize("owner", ["p", "other"])
@pytest.mark.parametrize("kind", ["edge", "doc"])
def test_delete_operation_rejects_unrelated_general_writes(stores, owner, kind):
    path, shared, graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    graph.upsert_node("Thing", "target", {"pack_id": "p"}, "concept")
    graph.upsert_edge("Thing", "a", "rel", "Thing", "b", {"pack_id": owner})
    docs.upsert_node_doc("concept", "Thing", "n", {"pack_id": owner})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('p', 'owner', 'first')")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('other', 'owner', 'second')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    node_before = conn.execute("SELECT * FROM graph_nodes").fetchall()
    edge_before = conn.execute("SELECT * FROM graph_edges").fetchall()
    doc_before = conn.execute("SELECT * FROM doc_nodes").fetchall()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_delete_operation("p", "first", "op")
                conn.execute("DELETE FROM graph_nodes WHERE node_id='target'")
                if kind == "edge":
                    graph.upsert_edge("Thing", "a", "rel", "Thing", "b", {"pack_id": owner, "changed": True})
                else:
                    docs.upsert_node_doc("concept", "Thing", "n", {"pack_id": owner, "changed": True})
        assert conn.execute("SELECT * FROM graph_nodes").fetchall() == node_before
        assert conn.execute("SELECT * FROM graph_edges").fetchall() == edge_before
        assert conn.execute("SELECT * FROM doc_nodes").fetchall() == doc_before
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        sql.close()


@pytest.mark.parametrize("order", ["write_first", "delete_first"])
def test_delete_operation_cannot_share_general_write_authority(stores, order):
    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('p', 'owner', 'first')")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('other', 'owner', 'second')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                if order == "write_first":
                    shared.validate_pack_writes({"other": "second"})
                    shared.validate_delete_operation("p", "first", "op")
                else:
                    shared.validate_delete_operation("p", "first", "op")
                    shared.validate_pack_writes({"other": "second"})
        with shared.unit_of_work():
            shared.validate_pack_writes({"other": "second"})
    finally:
        sql.close()


def test_nested_delete_authority_expires_on_exit(stores):
    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('p', 'owner', 'first')")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('other', 'owner', 'second')")
    conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
    conn.commit()
    try:
        with shared.unit_of_work():
            with shared.unit_of_work():
                shared.validate_delete_operation("p", "first", "op")
            shared.validate_pack_writes({"other": "second"})
    finally:
        sql.close()


def test_direct_doc_sql_rejects_unvalidated_pack(stores):
    path, shared, _graph, docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    docs.upsert_source("blocked", "text", {"pack_id": "blocked"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.execute("INSERT INTO users (user_id, display_name, is_local) VALUES ('owner', 'owner', 0)")
    conn.execute("INSERT INTO packs (pack_id, owner_id, incarnation) VALUES ('allowed', 'owner', 'first')")
    conn.commit()
    try:
        with pytest.raises(RuntimeError):
            with shared.unit_of_work():
                shared.validate_pack_writes({"allowed": "first"})
                docs._exec_write("DELETE FROM doc_sources WHERE source_id=:sid", {"sid": "blocked"})
        assert docs.get_source("blocked") is not None
    finally:
        sql.close()


def test_single_file_delete_preserves_ambiguous_registry_edges(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "left", {"pack_id": "p"}, "concept")
    graph.upsert_node("Thing", "right", {"pack_id": "p"}, "concept")
    graph.upsert_edge("Thing", "left", "rel", "Thing", "right", {"pack_id": "p"})
    sql.register_edge("concept", "left", "rel", "concept", "right")
    sql.register_edge("other", "left", "rel", "other", "right")
    conn = shared.connection()
    before = [tuple(row) for row in conn.execute(
        "SELECT from_space, from_id, relation, to_space, to_id FROM ontology_edges ORDER BY from_space"
    ).fetchall()]
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        after = [tuple(row) for row in conn.execute(
            "SELECT from_space, from_id, relation, to_space, to_id FROM ontology_edges ORDER BY from_space"
        ).fetchall()]
        assert after == before
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 0
    finally:
        sql.close()


def test_single_file_delete_preserves_unrelated_owned_edge(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "left", {"pack_id": "other"}, "concept")
    graph.upsert_node("Thing", "right", {"pack_id": "other"}, "concept")
    graph.upsert_edge("Thing", "left", "rel", "Thing", "right", {"pack_id": "p"})
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        assert _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec) == (0, 0, 0)
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0] == 2
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "committed"
    finally:
        sql.close()


def test_single_file_delete_rejects_pending_without_external_journal(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack import delete_journal
    from opencrab.pack.load import _reserve_single_file_delete, _single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        _reserve_single_file_delete(shared, sql, principal, "p")
        for resume in (False, True):
            with pytest.raises(delete_journal.DeletePackJournalConflict, match="no journal"):
                _single_file_delete(shared, sql, principal, "p", graph, docs, vec, resume, None)
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        sql.close()


def test_single_file_delete_rejects_orphan_external_journal(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack import delete_journal
    from opencrab.pack.load import _single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    journal = {"schema": 2, "pack_id": "p", "operation_id": "b" * 32,
               "incarnation": "a" * 32, "state": "pending", "result": None}
    delete_journal.save_journal(os.path.dirname(path), "p", journal)
    try:
        principal = Principal(user_id=owner, is_local=True, disabled=False)
        with pytest.raises(delete_journal.DeletePackJournalConflict):
            _single_file_delete(shared, sql, principal, "p", graph, docs, vec, False, None)
        assert conn.execute("SELECT count(*) FROM pack_delete_operations").fetchone()[0] == 0
    finally:
        sql.close()


def test_delete_helper_rejects_unregistered_incident_edge_without_changes(stores):
    from opencrab.auth import Principal, create_user
    from opencrab.pack.load import _apply_single_file_delete, _start_single_file_delete
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Thing", "n", {"pack_id": "p"}, "concept")
    graph.upsert_node("Thing", "other", {"pack_id": "other"}, "concept")
    graph.upsert_edge("Thing", "n", "rel", "Thing", "other", {"pack_id": "other"})
    docs.upsert_source("s", "text", {"pack_id": "p"})
    vec.upsert_texts(["text"], [{"pack_id": "p"}], ["s"])
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        journal = _start_single_file_delete(shared, sql, principal, "p", os.path.dirname(path))
        with pytest.raises(RuntimeError, match="incarnation"):
            _apply_single_file_delete(shared, sql, principal, "p", journal, graph, docs, vec)
        assert _counts(path) == (1, 1, 1)
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM pack_delete_operations").fetchone()[0] == "pending"
    finally:
        sql.close()


@pytest.mark.xfail(
    strict=True,
    raises=RuntimeError,  # 설계가 정한 거부만 흡수한다. 같은 문구는 T-N7(factory 시험)이 정확히 단언한다.
    reason="delete_pack는 pack_delete_operations 표가 있는 단일 SQLite 파일에서 거부된다. "
    "삭제 저널 단위(followup_671_delete_journal_unit)가 그 거부를 풀 때 이 xfail을 지우고 시험을 켠다.",
)
def test_single_file_delete_reserves_before_external_journal(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.pack import delete_journal
    from opencrab.pack.load import delete_pack
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation=? WHERE pack_id='p'", ("a" * 32,))
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY, pack_id TEXT, incarnation TEXT, state TEXT, result_json TEXT, evidence_json TEXT)")
    conn.commit()

    def fail_journal(*_args, **_kwargs):
        raise OSError("journal failure")

    monkeypatch.setattr(delete_journal, "save_journal", fail_journal)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(OSError, match="journal failure"):
                delete_pack("p", graph, docs, vec, sql=sql)
        row = conn.execute("SELECT pack_id, incarnation, state FROM pack_delete_operations").fetchone()
        assert tuple(row) == ("p", "a" * 32, "pending")
    finally:
        sql.close()


def test_source_rejects_recreated_pack_during_embedding(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    prepare = vec.prepare_upsert_vectors

    def recreate(texts):
        vectors = prepare(texts)
        with sqlite3.connect(path) as other:
            other.execute("UPDATE packs SET incarnation='second' WHERE pack_id='p'")
        return vectors

    monkeypatch.setattr(vec, "prepare_upsert_vectors", recreate)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
            assert receipt["stores"]["documents"].startswith("error:")
        assert _counts(path) == (0, 0, 0)
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='s1'").fetchone()[0] == 0
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_source_rejects_deletion_reserved_during_embedding(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    prepare = vec.prepare_upsert_vectors

    def reserve(texts):
        vectors = prepare(texts)
        with sqlite3.connect(path) as other:
            other.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
        return vectors

    monkeypatch.setattr(vec, "prepare_upsert_vectors", reserve)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
            assert receipt["stores"]["documents"].startswith("error:")
            assert receipt["stores"]["chromadb"].startswith("error:")
        assert _counts(path) == (0, 0, 0)
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='s1'").fetchone()[0] == 0
    finally:
        hybrid.shutdown_bm25()
        sql.close()


@pytest.mark.parametrize("kind", ["node", "edge", "source", "chunks"])
def test_single_file_product_writer_waits_for_backup_lock(stores, tmp_path, kind):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.locking import write_lock
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.load import load_chunks
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    graph.upsert_node("Document", "a", {"pack_id": "p"}, "resource")
    graph.upsert_node("Document", "b", {"pack_id": "p"}, "resource")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    builder = OntologyBuilder(graph, docs, sql, vec=vec)
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text(json.dumps({"id": "chunk", "text": "text", "metadata": {}}))
    started, completed = threading.Event(), threading.Event()
    errors, results = [], []

    def writer():
        try:
            with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
                started.set()
                if kind == "node":
                    results.append(builder.add_node("resource", "Document", "new", {"title": "t"}, pack_id="p"))
                elif kind == "edge":
                    results.append(builder.add_edge("resource", "a", "cites", "resource", "b", pack_id="p"))
                elif kind == "source":
                    results.append(write_source(sql, hybrid, docs, vec, graph=graph, text="text", source_id="s", metadata={}, pack_id="p"))
                else:
                    results.append(load_chunks("p", chunks, vec, docs, sql=sql))
                completed.set()
        except Exception as exc:
            errors.append(exc)

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=writer)
            thread.start()
            assert started.wait(5)
            assert not completed.wait(0.3)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert completed.is_set()
        if kind == "chunks":
            assert results[0] == (1, 0)
            assert conn.execute("SELECT count(*) FROM doc_sources WHERE source_id='chunk'").fetchone()[0] == 1
        elif kind == "source":
            assert results[0]["stores"]["documents"].startswith("ok")
            assert conn.execute("SELECT count(*) FROM doc_sources WHERE source_id='s'").fetchone()[0] == 1
        elif kind == "node":
            assert results[0]["stores"]["graph"] == "ok"
            assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='new'").fetchone()[0] == 1
        else:
            assert results[0]["stores"]["graph"] == "ok"
            assert conn.execute("SELECT count(*) FROM graph_edges WHERE from_id='a' AND to_id='b'").fetchone()[0] == 1
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_source_single_db_guard_allows_valid_pack(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
        assert receipt["stores"]["graph"] == "ok"
        assert receipt["stores"]["documents"].startswith("ok")
        assert _counts(path) == (1, 1, 1)
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_source_success_embeds_outside_transaction(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    original = vec.prepare_upsert_vectors
    def prepare(texts):
        assert not shared.in_unit_of_work
        return original(texts)
    vec.prepare_upsert_vectors = prepare
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
        assert receipt["stores"]["graph"] == "ok"
        assert receipt["stores"]["chromadb"].startswith("ok (")
        assert _counts(path) == (1, 1, 1)
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_source_without_optional_vector_commits_other_axes(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import begin_pack_creation
    from opencrab.pack.source_writer import write_source

    path, _shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=graph.shared_file)
    owner = create_user(sql, "owner", is_local=True)
    begin_pack_creation(sql, owner, "p", forked_from="original")
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, None, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p",
                                   fork_copy=True, write_vector=False)
        assert receipt["stores"]["graph"] == "ok"
        assert receipt["stores"]["chromadb"] == "skipped (raw copy)"
        assert _counts(path) == (1, 1, 0)
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_source_rejects_hybrid_with_different_vector(stores, tmp_path):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    (tmp_path / "unrelated").mkdir()  # root 밖 디렉터리여야 B11의 root 검사에 걸리지 않는다
    other = SqliteVecStore(str(tmp_path / "unrelated" / "other.db"), embedding_function=_embed, dim=DIM)
    hybrid = HybridQuery(other, graph, doc_store=docs)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(RuntimeError, match="vector"):
                write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                             source_id="s1", metadata={}, pack_id="p")
        assert _counts(path) == (0, 0, 0)
    finally:
        hybrid.shutdown_bm25()
        other.close()
        sql.close()


def test_source_rejects_hybrid_with_different_doc_store(stores, tmp_path):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    (tmp_path / "unrelated").mkdir()  # root 밖 디렉터리여야 B11의 root 검사에 걸리지 않는다
    other = LocalSQLDocStore(str(tmp_path / "unrelated" / "other-doc.db"))
    hybrid = HybridQuery(vec, graph, doc_store=other)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(RuntimeError, match="doc store"):
                write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                             source_id="s1", metadata={}, pack_id="p")
        assert _counts(path) == (0, 0, 0)
    finally:
        hybrid.shutdown_bm25()
        other.close()
        sql.close()


def test_shared_bm25_callback_failure_keeps_invalidation_marker(stores, monkeypatch):
    from opencrab.ontology.query import HybridQuery

    _path, shared, graph, docs, vec = stores
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    monkeypatch.setattr(hybrid, "_node_index_store", lambda: None)
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("invalidation failure")

    monkeypatch.setattr(hybrid._bm25, "invalidate", fail)
    try:
        with shared.unit_of_work():
            hybrid.invalidate_bm25_cache()
        assert calls == [True]
        with pytest.raises(RuntimeError, match="invalidation failure"):
            hybrid._memory_path_entered()
        assert hybrid._bm25_invalidation_skipped is True
    finally:
        hybrid.shutdown_bm25()


def test_source_vector_failure_rolls_back_graph_and_doc(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    def fail(*_args, **_kwargs):
        raise RuntimeError("vector failure")
    monkeypatch.setattr(vec, "upsert_precomputed", fail)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
        assert receipt["stores"]["chromadb"].startswith("error:")
        assert receipt["stores"]["graph"].startswith("error:")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='s1'").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM doc_sources WHERE source_id='s1'").fetchone()[0] == 0
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_source_node_audit_failure_does_not_rollback_required_writes(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    hybrid = HybridQuery(vec, graph, doc_store=docs)

    def fail(*_args, **_kwargs):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(docs, "log_event", fail)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
        assert receipt["stores"]["audit"].startswith("error:")
        assert receipt["stores"]["graph"] == "ok"
        assert _counts(path) == (1, 1, 1)
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_source_unavailable_vector_rolls_back_required_writes(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    monkeypatch.setattr(vec, "_available", False)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
        assert receipt["stores"]["chromadb"] == "unavailable"
        assert receipt["stores"]["graph"] == "error: rolled back"
        assert _counts(path) == (0, 0, 0)
    finally:
        hybrid.shutdown_bm25()
        sql.close()


@pytest.mark.parametrize("failure", ["documents", "registry"])
def test_source_required_write_failure_rolls_back(stores, monkeypatch, failure):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.ownership import create_pack
    from opencrab.pack.source_writer import write_source

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    hybrid = HybridQuery(vec, graph, doc_store=docs)

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(docs if failure == "documents" else sql,
                        "upsert_source" if failure == "documents" else "register_node", fail)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = write_source(sql, hybrid, docs, vec, graph=graph, text="text",
                                   source_id="s1", metadata={}, pack_id="p")
        assert receipt["stores"]["graph"].startswith("error:")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='s1'").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM doc_sources WHERE source_id='s1'").fetchone()[0] == 0
        assert _counts(path) == (0, 0, 0)
    finally:
        hybrid.shutdown_bm25()
        sql.close()


def test_builder_embedding_failure_returns_failure_receipt(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    def fail(_texts):
        raise RuntimeError("embedding failure")
    vec._ef = fail
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = OntologyBuilder(graph, docs, sql, vec=vec).add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id="p")
        assert receipt["stores"]["vector"].startswith("error:")
        assert receipt["stores"]["graph"].startswith("error:")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='n1'").fetchone()[0] == 0
    finally:
        sql.close()


def test_nested_builder_failure_returns_rollback_receipt(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    def fail(*_args, **_kwargs):
        raise RuntimeError("doc failure")
    monkeypatch.setattr(docs, "upsert_node_doc", fail)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            with pytest.raises(RuntimeError, match="rollback"):
                with shared.unit_of_work():
                    receipt = OntologyBuilder(graph, docs, sql, vec=vec).add_node(
                        "resource", "Document", "n1", {"title": "t"}, pack_id="p", write_vector=False)
                    assert receipt["stores"]["graph"].startswith("error:")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='n1'").fetchone()[0] == 0
    finally:
        sql.close()


def test_builder_rejects_pending_deletion_for_node_and_edge(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    builder = OntologyBuilder(graph, docs, sql, vec=vec)
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    try:
        with principal_scope(principal):
            for node in ("n1", "n2"):
                builder.add_node("resource", "Document", node, {"title": node}, pack_id="p")
            conn = shared.connection()
            conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
            conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
            conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
            conn.execute("INSERT INTO pack_delete_operations VALUES ('op', 'p', 'first', 'pending')")
            conn.commit()
            node_result = builder.add_node("resource", "Document", "n3", {"title": "t"}, pack_id="p")
            edge_result = builder.add_edge("resource", "n1", "cites", "resource", "n2", pack_id="p")
            assert node_result["stores"]["graph"].startswith("error:")
            assert edge_result["stores"]["graph"].startswith("error:")
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='n3'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM ontology_edges").fetchone()[0] == 0
    finally:
        sql.close()


def test_builder_single_db_guard_allows_valid_node_and_edge(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    conn = shared.connection()
    conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
    conn.execute("UPDATE packs SET incarnation='first' WHERE pack_id='p'")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.commit()
    builder = OntologyBuilder(graph, docs, sql, vec=vec)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            for node in ("n1", "n2"):
                result = builder.add_node("resource", "Document", node, {"title": node}, pack_id="p")
                assert result["stores"]["graph"] == "ok"
                assert result["stores"]["sql"] == "ok"
            edge = builder.add_edge("resource", "n1", "cites", "resource", "n2", pack_id="p")
        assert edge["stores"]["graph"] == "ok"
        assert edge["stores"]["sql"] == "ok"
    finally:
        sql.close()


def test_builder_vector_success_embeds_outside_transaction(stores):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    def embed(texts):
        assert not shared.in_unit_of_work
        return _embed(texts)
    vec._ef = embed
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = OntologyBuilder(graph, docs, sql, vec=vec).add_node(
                "resource", "Document", "n1", {"title": "t"}, pack_id="p")
        assert receipt["stores"]["graph"] == "ok"
        assert receipt["stores"]["vector"] == "ok"
        assert vec.get_by_id("n1") is not None
    finally:
        sql.close()


def test_builder_edge_audit_failure_does_not_rollback_required_writes(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    builder = OntologyBuilder(graph, docs, sql, vec=vec)

    def fail(*_args, **_kwargs):
        raise RuntimeError("audit failure")

    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            for node in ("n1", "n2"):
                builder.add_node("resource", "Document", node, {"title": node}, pack_id="p")
            monkeypatch.setattr(docs, "log_event", fail)
            receipt = builder.add_edge("resource", "n1", "cites", "resource", "n2", pack_id="p")
        assert receipt["stores"]["docs"].startswith("error:")
        assert receipt["stores"]["graph"] == "ok"
        assert receipt["stores"]["sql"] == "ok"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM ontology_edges").fetchone()[0] == 1
    finally:
        sql.close()


def test_builder_edge_registry_failure_rolls_back_graph(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    principal = Principal(user_id=owner, is_local=True, disabled=False)
    builder = OntologyBuilder(graph, docs, sql, vec=vec)
    try:
        with principal_scope(principal):
            for node in ("n1", "n2"):
                builder.add_node("resource", "Document", node, {"title": node}, pack_id="p")
            def fail(*_args):
                raise RuntimeError("registry failure")
            monkeypatch.setattr(sql, "register_edge", fail)
            receipt = builder.add_edge("resource", "n1", "cites", "resource", "n2", pack_id="p")
        assert receipt["stores"]["graph"].startswith("error:")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0] == 0
    finally:
        sql.close()


def test_builder_doc_failure_rolls_back_other_stores(stores, monkeypatch):
    from opencrab.auth import Principal, create_user, principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack

    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    owner = create_user(sql, "owner", is_local=True)
    create_pack(sql, owner, "p")
    builder = OntologyBuilder(graph, docs, sql, vec=vec)
    def fail(*_args, **_kwargs):
        raise RuntimeError("doc failure")
    monkeypatch.setattr(docs, "upsert_node_doc", fail)
    try:
        with principal_scope(Principal(user_id=owner, is_local=True, disabled=False)):
            receipt = builder.add_node("resource", "Document", "n1", {"title": "t"}, pack_id="p")
        assert receipt["stores"]["graph"].startswith("error:")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='n1'").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM ontology_nodes WHERE node_id='n1'").fetchone()[0] == 0
        assert vec.get_by_id("n1") is None
    finally:
        sql.close()


def test_bm25_verify_rejected_inside_shared_unit_without_commit(stores):
    path, shared, _graph, docs, vec = stores
    with shared.unit_of_work():
        _write_pair(docs, vec)
        with pytest.raises(RuntimeError, match="BM25"):
            docs.node_bm25_verify()
        assert _counts(path) == (0, 0, 0)
    assert _counts(path) == (1, 1, 1)


def test_vector_delete_helper_joins_outer_rollback(stores):
    from opencrab.pack.load import _delete_pack_vectors

    path, shared, _graph, docs, vec = stores
    _write_pair(docs, vec)
    with pytest.raises(RuntimeError, match="rollback"):
        with shared.unit_of_work():
            _count, _kind, confirmed = _delete_pack_vectors("p", vec, True)
            assert confirmed
            raise RuntimeError("rollback")
    assert _counts(path) == (1, 1, 1)


def test_vector_metadata_update_joins_outer_rollback(stores):
    from opencrab.pack.load import _vec_meta_update

    _path, shared, _graph, docs, vec = stores
    _write_pair(docs, vec)
    before = vec.get_by_id("s1")["metadata"]
    with pytest.raises(RuntimeError, match="rollback"):
        with shared.unit_of_work():
            assert _vec_meta_update(vec, "s1", {"pack_id": "p", "changed": "yes"}, "p")
            raise RuntimeError("rollback")
    assert vec.get_by_id("s1")["metadata"] == before


def test_graph_snapshot_rejected_before_side_effect_in_shared_unit(stores, tmp_path):
    _path, shared, graph, _docs, _vec = stores
    target = tmp_path / "snapshot.db"
    with shared.unit_of_work():
        with pytest.raises(RuntimeError, match="snapshot"):
            graph._run_graph_tx(lambda _tx: None, immediate=True, snapshot_path=target)
    assert not target.exists()
    assert not list(tmp_path.glob(".snapshot.db.issue80-*"))


def test_precomputed_vector_upsert_joins_shared_unit(stores):
    path, shared, _graph, docs, vec = stores
    vectors = vec.prepare_upsert_vectors(["본문"])
    with pytest.raises(RuntimeError, match="rollback"):
        with shared.unit_of_work():
            vec.upsert_precomputed(["본문"], [{"pack_id": "p"}], ["s1"], vectors)
            docs.upsert_source("s1", "본문", {"pack_id": "p"})
            raise RuntimeError("rollback")
    assert _counts(path) == (0, 0, 0)
    with shared.unit_of_work():
        vec.upsert_precomputed(["본문"], [{"pack_id": "p"}], ["s1"], vectors)
        docs.upsert_source("s1", "본문", {"pack_id": "p"})
    assert _counts(path) == (1, 1, 1)


def test_precomputed_vector_rejects_mismatched_count_before_write(stores):
    path, _shared, _graph, _docs, vec = stores
    with pytest.raises(ValueError, match="vectors"):
        vec.upsert_precomputed(["a", "b"], [{"pack_id": "p"}] * 2, ["a", "b"], [[1.0] * DIM])
    assert _counts(path) == (0, 0, 0)


@pytest.mark.parametrize("kind", ["impact", "simulation", "policy"])
def test_operational_sql_writes_join_shared_unit(stores, kind):
    path, shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    writes = {
        "impact": lambda: sql.save_impact("n", "update", {}),
        "simulation": lambda: sql.save_simulation("n", "up", 1.0, {}),
        "policy": lambda: sql.set_policy("subject", "read", "resource"),
    }
    tables = {"impact": "impact_records", "simulation": "lever_simulations", "policy": "rebac_policies"}
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            with shared.unit_of_work():
                writes[kind]()
                assert shared.connection().execute(f"SELECT count(*) FROM {tables[kind]}").fetchone()[0] == 1
                raise RuntimeError("rollback")
        with sqlite3.connect(path) as conn:
            assert conn.execute(f"SELECT count(*) FROM {tables[kind]}").fetchone()[0] == 0
        writes[kind]()
        with sqlite3.connect(path) as conn:
            assert conn.execute(f"SELECT count(*) FROM {tables[kind]}").fetchone()[0] == 1
    finally:
        sql.close()


def test_operational_sql_independent_write_waits_for_backup_lock(stores):
    from opencrab.locking import write_lock

    path, _shared, _graph, _docs, _vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=_shared)
    started = threading.Event()
    finished = threading.Event()
    errors = []

    def write():
        try:
            started.set()
            sql.save_impact("n", "update", {})
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    try:
        with write_lock(os.path.dirname(path)):
            thread = threading.Thread(target=write)
            thread.start()
            assert started.wait(2)
            assert not finished.wait(0.2)
        thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert finished.is_set()
    finally:
        sql.close()


def test_four_stores_commit_and_rollback_together(stores):
    path, shared, graph, docs, vec = stores
    sql = SQLStore(f"sqlite:///{path}", shared=shared)
    try:
        def write():
            graph.upsert_node("Thing", "n1", {"name": "first"}, "concept")
            _write_pair(docs, vec)
            sql.register_node("concept", "Thing", "n1")

        def counts():
            with sqlite3.connect(path) as conn:
                return (conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0],
                        conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0],
                        conn.execute("SELECT count(*) FROM ontology_nodes").fetchone()[0])

        with pytest.raises(RuntimeError, match="rollback"):
            with shared.unit_of_work():
                write()
                assert counts() == (0, 0, 0)
                raise RuntimeError("rollback")
        assert counts() == (0, 0, 0)
        assert _counts(path)[2] == 0
        with shared.unit_of_work():
            write()
        assert counts() == (1, 1, 1)
        assert _counts(path)[2] == 1
    finally:
        sql.close()


def test_commit_makes_all_three_table_kinds_visible(stores):
    path, shared, _graph, docs, vec = stores
    with shared.unit_of_work():
        _write_pair(docs, vec)
        # 구간 안에서는 아직 다른 연결에 보이지 않는다.
        assert _counts(path) == (0, 0, 0)
    assert _counts(path) == (1, 1, 1)


def test_exception_rolls_back_regular_fts5_and_vec0_together(stores):
    path, shared, _graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            _write_pair(docs, vec)
            raise RuntimeError("boom")
    assert _counts(path) == (0, 0, 0)
    # 롤백 뒤에도 같은 연결로 다음 쓰기가 된다.
    _write_pair(docs, vec, "s2")
    assert _counts(path) == (1, 1, 1)


def test_checkpoint_error_after_commit_does_not_report_rollback(stores, monkeypatch):
    path, shared, graph, docs, vec = stores
    def fail(_conn):
        raise RuntimeError("checkpoint failed")
    monkeypatch.setattr(graph, "_checkpoint_if_wal_large", fail)
    with shared.unit_of_work():
        _write_pair(docs, vec)
    assert _counts(path) == (1, 1, 1)


def test_nested_failure_marks_outer_transaction_rollback_only(stores):
    path, shared, _graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="rollback"):
        with shared.unit_of_work():
            _write_pair(docs, vec)
            try:
                with shared.unit_of_work():
                    raise ValueError("inner failure")
            except ValueError:
                pass
    assert _counts(path) == (0, 0, 0)


def test_commit_callbacks_wait_for_outer_commit_and_skip_rollback(stores):
    _path, shared, _graph, _docs, _vec = stores
    called = []
    with shared.unit_of_work():
        shared.after_commit(lambda: called.append("committed"))
        with shared.unit_of_work():
            shared.after_commit(lambda: called.append("nested"))
        assert called == []
    assert called == ["committed", "nested"]
    with pytest.raises(RuntimeError, match="abort"):
        with shared.unit_of_work():
            shared.after_commit(lambda: called.append("rolled back"))
            raise RuntimeError("abort")
    assert called == ["committed", "nested"]


def test_hybrid_invalidation_runs_after_shared_commit(stores, monkeypatch):
    from opencrab.ontology.query import HybridQuery

    _path, shared, graph, docs, vec = stores
    hybrid = HybridQuery(vec, graph, doc_store=docs)
    calls = []
    monkeypatch.setattr(hybrid, "_node_index_store", lambda: None)
    monkeypatch.setattr(hybrid._bm25, "invalidate", lambda: calls.append("invalidated"))
    try:
        with shared.unit_of_work():
            hybrid.invalidate_bm25_cache()
            assert calls == []
        assert calls == ["invalidated"]
        with pytest.raises(RuntimeError, match="abort"):
            with shared.unit_of_work():
                hybrid.invalidate_bm25_cache()
                raise RuntimeError("abort")
        assert calls == ["invalidated"]
    finally:
        hybrid.shutdown_bm25()


def test_callback_failure_does_not_change_committed_result(stores):
    path, shared, _graph, docs, vec = stores
    def fail():
        raise RuntimeError("callback failure")
    with shared.unit_of_work():
        _write_pair(docs, vec)
        shared.after_commit(fail)
    assert _counts(path) == (1, 1, 1)


def test_explicit_rollback_only_can_be_set_from_receipt(stores):
    path, shared, _graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="rollback"):
        with shared.unit_of_work():
            _write_pair(docs, vec)
            shared.mark_rollback_only()
    assert _counts(path) == (0, 0, 0)
    with shared.unit_of_work():
        _write_pair(docs, vec)
    assert _counts(path) == (1, 1, 1)


def test_inner_failure_rolls_back_only_its_savepoint(stores):
    path, shared, _graph, docs, vec = stores
    with shared.unit_of_work():
        _write_pair(docs, vec, "keep")
        with contextlib.suppress(sqlite3.IntegrityError):
            # 같은 구간 안의 _tx()가 자기 SAVEPOINT만 되돌린다.
            docs._exec_write_many([
                ("INSERT INTO doc_sources(source_id, text, metadata, ingested_at) "
                 "VALUES ('dup', 't', '{}', 'now')", {}),
                ("INSERT INTO doc_sources(source_id, text, metadata, ingested_at) "
                 "VALUES ('dup', 't', '{}', 'now')", {}),
            ])
    assert _counts(path)[0] == 1, "안쪽 실패의 부분 쓰기가 남았거나 바깥 쓰기가 사라졌다"


def test_nested_unit_of_work_joins_the_outer_one(stores):
    path, shared, _graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            with shared.unit_of_work():
                _write_pair(docs, vec)
            assert _counts(path) == (0, 0, 0), "안쪽 구간이 따로 커밋했다"
            raise RuntimeError("boom")
    assert _counts(path) == (0, 0, 0)


def test_unit_of_work_refuses_to_start_inside_an_open_transaction(stores):
    _path, shared, _graph, docs, _vec = stores
    conn = docs._conn
    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="open transaction"):
            with shared.unit_of_work():
                pass
    finally:
        conn.rollback()


def test_close_of_one_store_keeps_the_connection_for_the_others(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    docs = LocalSQLDocStore(path, shared=shared)
    vec = SqliteVecStore(path, embedding_function=_embed, dim=DIM, shared=shared)
    docs.close()
    assert vec.ping()
    vec.upsert_texts(texts=["a"], ids=["x"], metadatas=[{"pack_id": "p"}])
    vec.close()
    assert shared.all_conns == []


def test_unit_of_work_for_is_a_noop_without_a_shared_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    vec = SqliteVecStore(str(tmp_path / "vec.db"), embedding_function=_embed, dim=DIM)
    try:
        assert docs.shared_file is None
        assert isinstance(unit_of_work_for(vec, docs), contextlib.nullcontext)
    finally:
        docs.close()
        vec.close()


def test_unit_of_work_for_requires_the_same_shared_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    a = SharedSqliteFile(str(tmp_path / "a.db"))
    b = SharedSqliteFile(str(tmp_path / "b.db"))
    docs = LocalSQLDocStore(str(tmp_path / "a.db"), shared=a)
    vec = SqliteVecStore(str(tmp_path / "b.db"), embedding_function=_embed, dim=DIM, shared=b)
    try:
        with pytest.raises(RuntimeError, match="shared"):
            unit_of_work_for(vec, docs)
    finally:
        docs.close()
        vec.close()


def test_other_thread_sees_only_committed_state_during_the_unit(stores):
    path, shared, _graph, docs, vec = stores
    seen: list[tuple[int, int, int]] = []
    started = threading.Event()
    release = threading.Event()

    def reader():
        started.wait(5)
        seen.append(_counts(path))
        release.set()

    t = threading.Thread(target=reader)
    t.start()
    with shared.unit_of_work():
        _write_pair(docs, vec)
        started.set()
        assert release.wait(10)
    t.join()
    assert seen == [(0, 0, 0)]
    assert _counts(path) == (1, 1, 1)


def test_large_batch_rollback_crosses_vec0_chunk_and_fts_flush_boundaries(stores):
    """vec0 청크 경계와 fts5 pending 해시 flush를 넘는 크기도 구간 롤백이 전부 되돌린다."""
    path, shared, _graph, docs, vec = stores
    n = 1200
    ids = [f"big-{i}" for i in range(n)]
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            vec.upsert_texts(texts=ids, ids=ids, metadatas=[{"pack_id": "p"}] * n)
            for sid in ids:
                docs.upsert_source(sid, f"본문 {sid} alpha", {"pack_id": "p"})
            raise RuntimeError("boom")
    assert _counts(path) == (0, 0, 0)
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    try:
        conn.execute("INSERT INTO doc_sources_fts(doc_sources_fts) VALUES('integrity-check')")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM vectors_kure WHERE embedding MATCH ? AND k=3",
                            (sqlite_vec.serialize_float32([1.0, 1.0, 0.0, 0.5]),)).fetchone()[0] == 0
    finally:
        conn.close()


def test_exclusive_transaction_inside_a_unit_is_refused(stores):
    _path, shared, graph, _docs, _vec = stores
    with shared.unit_of_work():
        with pytest.raises(RuntimeError, match="exclusive"):
            with graph._tx(exclusive=True):
                pass


def test_immediate_transaction_inside_a_unit_joins_it(stores):
    path, shared, graph, docs, vec = stores
    with pytest.raises(RuntimeError, match="boom"):
        with shared.unit_of_work():
            with graph._tx(immediate=True) as conn:
                conn.execute("INSERT INTO graph_nodes(node_type, node_id, space_id, properties) "
                             "VALUES ('t', 'g1', 's', '{}')")
            raise RuntimeError("boom")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM graph_nodes WHERE node_id='g1'").fetchone()[0] == 0
    finally:
        conn.close()


def test_wal_is_checkpointed_after_a_unit_that_grows_it_past_the_threshold(stores, monkeypatch):
    path, shared, graph, docs, vec = stores
    monkeypatch.setattr(type(graph), "_WAL_CHECKPOINT_THRESHOLD_BYTES", 64 * 1024)
    with shared.unit_of_work():
        for i in range(400):
            docs.upsert_source(f"w{i}", "x" * 400, {"pack_id": "p"})
    import os

    assert os.path.getsize(path + "-wal") == 0


def test_lower_udf_is_consistent_on_the_shared_connection(stores):
    """graph 저장소가 등록한 ``lower`` UDF가 공유 연결에서 벡터와 doc 저장소의 질의에 영향을 주지 않는다."""
    _path, _shared, _graph, docs, vec = stores
    vec.upsert_texts(texts=["a"], ids=["x"], metadatas=[{"pack_id": "P"}])
    assert vec.count() == 1
    # SQLite 내장 lower는 ASCII만 바꾼다. 비ASCII 대문자가 바뀌어야 UDF가 이 연결에 걸렸다는 증거다.
    assert vec._conn.execute("SELECT lower('Ä')").fetchone()[0] == "ä"
    assert docs._conn.execute("SELECT lower('Ä')").fetchone()[0] == "ä"


def _binding_parts(tmp_path, state):
    import hashlib

    root, db = str(tmp_path.resolve()), str(tmp_path / "single.db")
    identity = {"root": root, "database": db, "db_uuid": "a" * 32, "generation": "b" * 32}
    manifest = dict(identity)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    ledger = {**identity, "state": state, "manifest_digest": digest}
    marker = {"db_uuid": identity["db_uuid"], "generation": identity["generation"], "manifest_digest": digest}
    return root, db, ledger, manifest, marker, dict(identity), digest


def test_single_file_binding_accepts_explicit_staged_and_ready_state(tmp_path):
    from opencrab.stores._single_file import load_single_file_binding, validate_single_file_binding

    for state in ("staged", "ready", "authorized"):
        root, db, ledger, manifest, marker, metadata, digest = _binding_parts(tmp_path, state)
        validate_single_file_binding(root, db, ledger, manifest, marker, metadata, digest, state=state)
        other = "ready" if state != "ready" else "staged"
        with pytest.raises(RuntimeError, match="state differs"):
            validate_single_file_binding(root, db, ledger, manifest, marker, metadata, digest, state=other)
        (tmp_path / "ledger").write_text(json.dumps(ledger))
        (tmp_path / "manifest").write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        (tmp_path / "marker").write_text(json.dumps(marker))
        directory = os.open(tmp_path, os.O_RDONLY)
        try:
            # 디렉터리 읽기 경로도 같은 state 인자를 그대로 넘긴다.
            load_single_file_binding(root, db, directory, directory, "ledger", "manifest", "marker", metadata,
                                     state=state)
            if state != "authorized":
                with pytest.raises(RuntimeError, match="not authorized"):
                    load_single_file_binding(root, db, directory, directory, "ledger", "manifest", "marker", metadata)
        finally:
            os.close(directory)


def test_single_file_binding_default_state_unchanged(tmp_path):
    from opencrab.stores._single_file import validate_single_file_binding

    for state in ("staged", "ready"):
        root, db, ledger, manifest, marker, metadata, digest = _binding_parts(tmp_path, state)
        with pytest.raises(RuntimeError, match="single SQLite generation is not authorized"):
            validate_single_file_binding(root, db, ledger, manifest, marker, metadata, digest)
    root, db, ledger, manifest, marker, metadata, digest = _binding_parts(tmp_path, "authorized")
    validate_single_file_binding(root, db, ledger, manifest, marker, metadata, digest)


def test_single_file_binding_rejects_unknown_state(tmp_path):
    from opencrab.stores._single_file import validate_single_file_binding

    root, db, ledger, manifest, marker, metadata, digest = _binding_parts(tmp_path, "ready")
    for state in ("weird", "", None, "Ready"):
        with pytest.raises(RuntimeError, match="state argument"):
            validate_single_file_binding(root, db, ledger, manifest, marker, metadata, digest, state=state)
