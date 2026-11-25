"""단일 파일 모드의 벡터 질의 경로: 한 스냅숏, ANN 캐시, commit 뒤 무효화 (#671).

한 질의가 한 읽기 스냅숏을 보고(I1), 쓰기 구간 밖에서는 다섯 파일 모드와 같은 ANN 경로를 쓰며(I2),
열린 트랜잭션 안에서는 ANN을 쓰지 않는다(I3). 무효화는 commit 뒤에 일어나고 `epoch`가 빌드 중
commit이 남긴 낡은 캐시의 설치를 막는다. 설계: `docs/issue-671-single-db-conversion.md`.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from _vec_helpers import MockEF, build_vector_store

from opencrab.stores._single_file import SharedSqliteFile
from opencrab.stores.sqlite_vec_store import SqliteVecStore

DIM = 32
N = 40
IDS = [f"n{i:02d}" for i in range(N)]
PACKS = ["A", "B", "C"]


def _load(store, ids=IDS):
    store.upsert_texts(
        texts=[f"synthetic document {i} topic {i % 7}" for i in range(len(ids))],
        metadatas=[{"pack_id": PACKS[i % 3], "space": "s1"} for i in range(len(ids))],
        ids=list(ids),
    )


def _open(path, shared=None):
    return SqliteVecStore(path, embedding_function=MockEF(DIM), dim=DIM, ann="binary", shared=shared)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / "single.db")
    opened = []

    def make():
        shared = SharedSqliteFile(path)
        store = _open(path, shared)
        opened.append(store)
        return shared, store

    shared, vec = make()
    _load(vec)
    yield path, shared, vec, make
    for store in opened:
        store.close()


def _counter(monkeypatch, obj, name):
    original = getattr(obj, name)
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapper)
    return calls


def _in_thread(fn):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def _ids(hits):
    return [hit["id"] for hit in hits]


# ---------------------------------------------------------------------------
# I1 한 스냅숏, I2 알고리즘 동등, I3 열린 트랜잭션 안
# ---------------------------------------------------------------------------


def test_snapshot_pins_candidate_rows_across_writer_commit(env, monkeypatch):
    """질의 도중 다른 연결이 후보를 삭제해도 그 질의의 결과는 질의 시작 시점의 행이다."""
    path, shared, vec, make = env
    _other_shared, writer = make()
    victim = _ids(vec.query("synthetic document 3 topic 3", n_results=3))[0]
    original = vec._ann_refine_exact

    def delete_then_refine(qvec, ids):
        writer.delete([victim])
        return original(qvec, ids)

    monkeypatch.setattr(vec, "_ann_refine_exact", delete_then_refine)
    hits = vec.query("synthetic document 3 topic 3", n_results=3)
    assert victim in _ids(hits)
    monkeypatch.undo()
    assert victim not in _ids(vec.query("synthetic document 3 topic 3", n_results=3))


def test_negative_control_without_snapshot_sees_the_deletion(env, monkeypatch):
    path, shared, vec, make = env
    _other_shared, writer = make()
    victim = _ids(vec.query("synthetic document 3 topic 3", n_results=3))[0]
    original = vec._ann_refine_exact

    def delete_then_refine(qvec, ids):
        writer.delete([victim])
        return original(qvec, ids)

    monkeypatch.setattr(vec, "_ann_refine_exact", delete_then_refine)
    monkeypatch.setattr(vec, "_open_snapshot", lambda: None)
    assert victim not in _ids(vec.query("synthetic document 3 topic 3", n_results=3))


def test_query_leaves_no_open_transaction_even_after_an_exception(env, monkeypatch):
    path, shared, vec, make = env
    conn = shared.connection()
    vec.query("synthetic document 1 topic 1")
    assert not conn.in_transaction

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(vec, "_select_query_mode", boom)
    with pytest.raises(RuntimeError, match="injected"):
        vec.query("synthetic document 1 topic 1")
    assert not conn.in_transaction
    monkeypatch.undo()
    with shared.unit_of_work():
        pass
    assert vec.query("synthetic document 1 topic 1")


def test_exception_after_begin_in_open_snapshot_leaves_no_open_transaction(env, monkeypatch):
    """BEGIN가 끝난 뒤 `_open_snapshot`이 실패해도 롤백이 돌아 다음 쓰기 구간이 막히지 않는다."""
    path, shared, vec, make = env
    conn = shared.connection()

    def begin_then_fail():
        conn.execute("BEGIN")
        raise RuntimeError("injected after BEGIN")

    monkeypatch.setattr(vec, "_open_snapshot", begin_then_fail)
    with pytest.raises(RuntimeError, match="injected after BEGIN"):
        vec.query("synthetic document 1 topic 1")
    assert not conn.in_transaction
    monkeypatch.undo()
    with shared.unit_of_work():
        pass
    assert vec.query("synthetic document 1 topic 1")


def test_large_scope_uses_one_global_knn_inside_the_snapshot(tmp_path, monkeypatch):
    from opencrab.stores import sqlite_vec_store

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    shared = SharedSqliteFile(str(tmp_path / "s.db"))
    vec = SqliteVecStore(
        str(tmp_path / "s.db"), embedding_function=MockEF(DIM), dim=DIM, shared=shared
    )
    try:
        _load(vec, IDS[:6])
        monkeypatch.setattr(sqlite_vec_store, "_PACK_KNN_MAX", 1)
        calls = []
        original = vec._knn

        def recorded(qvec, k, pack):
            calls.append((pack, vec._conn.in_transaction))
            return original(qvec, k, pack)

        monkeypatch.setattr(vec, "_knn", recorded)
        hits = vec.query("x", n_results=6, where={"pack_id": {"$in": ["A", "B"]}})
        assert hits
        assert calls == [(None, True)]
        assert not shared.connection().in_transaction
    finally:
        vec.close()


def test_query_uses_ann_outside_a_transaction_and_never_inside(env, monkeypatch):
    path, shared, vec, make = env
    rerank = _counter(monkeypatch, vec, "_knn_bit_rerank")
    build = _counter(monkeypatch, vec, "_build_ann_cache")
    assert vec.query("synthetic document 1 topic 1")
    assert len(rerank) == 1 and len(build) == 1
    with shared.unit_of_work():
        vec.upsert_texts(["temporary"], [{"pack_id": "A", "space": "s1"}], ["tmp1"])
        assert "tmp1" in _ids(vec.query("temporary", n_results=N + 1))
        assert len(rerank) == 1 and len(build) == 1
    assert "tmp1" in _ids(vec.query("temporary", n_results=N + 1))


def test_in_transaction_query_sees_temporary_vector_then_rolls_back(env):
    path, shared, vec, make = env
    with pytest.raises(RuntimeError, match="abort"):
        with shared.unit_of_work():
            vec.upsert_texts(["temporary"], [{"pack_id": "A", "space": "s1"}], ["tmp1"])
            assert "tmp1" in _ids(vec.query("temporary", n_results=N + 1))
            raise RuntimeError("abort")
    assert "tmp1" not in _ids(vec.query("temporary", n_results=N + 1))


def test_single_mode_matches_five_file_mode_for_a_large_pack_scope(tmp_path, monkeypatch):
    from opencrab.stores import sqlite_vec_store

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    five = build_vector_store("sqlite-vec", tmp_path / "five", ann="binary")
    shared = SharedSqliteFile(str(tmp_path / "single.db"))
    single = _open(str(tmp_path / "single.db"), shared)
    try:
        _load(five)
        _load(single)
        monkeypatch.setattr(sqlite_vec_store, "_PACK_KNN_MAX", 2)
        counts = {}
        for name, store in (("five", five), ("single", single)):
            counts[name] = _counter(monkeypatch, store, "_knn_bit_rerank")
        where = {"pack_id": {"$in": PACKS}}
        got_five = five.query("synthetic document 5 topic 5", n_results=10, where=where)
        got_single = single.query("synthetic document 5 topic 5", n_results=10, where=where)
        assert counts["five"] and counts["single"]
        assert [(h["id"], round(h["distance"], 6)) for h in got_single] == [
            (h["id"], round(h["distance"], 6)) for h in got_five
        ]
    finally:
        five.close()
        single.close()


# ---------------------------------------------------------------------------
# I4 접근 제어, D 신선도 규칙, 제자리 메타 갱신
# ---------------------------------------------------------------------------


def test_stale_candidate_never_reaches_the_result(env, monkeypatch):
    path, shared, vec, make = env
    _other_shared, writer = make()
    victim = _ids(vec.query("synthetic document 3 topic 3", n_results=3))[0]
    monkeypatch.setattr(vec, "_ann_cache_fresh", lambda cache: True)
    writer.delete([victim])
    assert victim not in _ids(vec.query("synthetic document 3 topic 3", n_results=N))


def test_final_predicate_filters_candidates_outside_the_requested_space(env):
    path, shared, vec, make = env
    vec.upsert_texts(
        ["other space doc"], [{"pack_id": "A", "space": "other"}], ["foreign"]
    )
    hits = vec.query("other space doc", n_results=N + 1, where={"space": "s1"})
    assert "foreign" not in _ids(hits)


def test_top_row_delete_and_external_insert_rebuild_but_non_vector_commit_does_not(env, monkeypatch):
    path, shared, vec, make = env
    _other_shared, writer = make()
    build = _counter(monkeypatch, vec, "_build_ann_cache")
    rerank = _counter(monkeypatch, vec, "_knn_bit_rerank")
    vec.query("synthetic document 1 topic 1")
    assert len(build) == 1
    side = sqlite3.connect(path)
    side.execute("CREATE TABLE IF NOT EXISTS unrelated (x INTEGER)")
    side.execute("INSERT INTO unrelated VALUES (1)")
    side.commit()
    side.close()
    vec.query("synthetic document 1 topic 1")
    assert len(build) == 1
    writer.upsert_texts(["new doc"], [{"pack_id": "A", "space": "s1"}], ["added"])
    assert "added" in _ids(vec.query("new doc", n_results=N + 2))
    assert len(build) == 2
    writer.delete(["added"])  # the largest rowid: max(rowid) drops, so the next query rebuilds
    hits = vec.query("synthetic document 1 topic 1", n_results=N + 2)
    assert "added" not in _ids(hits)
    assert len(build) == 3 and len(rerank) == 4


def test_in_place_metadata_update_is_visible_without_rebuilding(env, monkeypatch):
    from opencrab.pack.load import _vec_meta_update

    path, shared, vec, make = env
    build = _counter(monkeypatch, vec, "_build_ann_cache")
    vec.query("synthetic document 1 topic 1")
    _vec_meta_update(vec, "n01", {"pack_id": "B", "space": "moved"}, "B")
    hits = vec.query("synthetic document 1 topic 1", n_results=N)
    assert len(build) == 1
    assert {h["id"]: h["metadata"]["space"] for h in hits}["n01"] == "moved"


# ---------------------------------------------------------------------------
# E commit 뒤 무효화와 epoch
# ---------------------------------------------------------------------------


def test_invalidation_runs_after_commit_and_not_on_rollback(env):
    path, shared, vec, make = env
    vec.query("synthetic document 1 topic 1")
    assert vec._ann_cache is not None
    epoch = vec._ann_epoch
    with pytest.raises(RuntimeError, match="abort"):
        with shared.unit_of_work():
            vec.delete(["n05"])
            raise RuntimeError("abort")
    assert vec._ann_cache is not None and vec._ann_epoch == epoch
    with shared.unit_of_work():
        vec.delete(["n05"])
        assert vec._ann_cache is not None
    assert vec._ann_cache is None and vec._ann_epoch == epoch + 1


def _pause_once(monkeypatch, obj, name):
    """`obj.name`의 첫 호출에서 멈추는 래퍼. (멈춤 시작 이벤트, 풀기 이벤트)를 돌려준다."""
    original = getattr(obj, name)
    paused, resume = threading.Event(), threading.Event()
    state = {"done": False}

    def wrapper(*args, **kwargs):
        if not state["done"]:
            state["done"] = True
            paused.set()
            assert resume.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapper)
    return paused, resume


def test_build_that_crosses_a_commit_does_not_install_a_stale_cache(env, monkeypatch):
    path, shared, vec, make = env
    paused, resume = _pause_once(monkeypatch, vec, "_quantize_rows")
    thread, box = _in_thread(lambda: vec.query("synthetic document 1 topic 1", n_results=N))
    assert paused.wait(30)
    with shared.unit_of_work():
        vec.delete(["n05"])
    resume.set()
    thread.join(30)
    assert "error" not in box, box
    assert vec._ann_cache is None
    monkeypatch.undo()
    assert "n05" not in _ids(vec.query("synthetic document 1 topic 1", n_results=N))


class _PausingDict(dict):
    """`get`의 첫 호출(PRAGMA data_version 직후, 스냅숏이 이미 고정됨)에서 멈춘다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paused, self.resume = threading.Event(), threading.Event()
        self._armed = True

    def get(self, key, default=None):
        if self._armed:
            self._armed = False
            self.paused.set()
            assert self.resume.wait(30)
        return super().get(key, default)


def test_warm_path_pins_epoch_before_the_snapshot(env, monkeypatch):
    path, shared, vec, make = env
    _other_shared, writer = make()
    vec.query("synthetic document 1 topic 1")
    writer.upsert_texts(["new doc"], [{"pack_id": "A", "space": "s1"}], ["added"])
    build = _counter(monkeypatch, vec, "_build_ann_cache")
    pausing = _PausingDict(vec._conn_dv)
    vec._conn_dv = pausing
    thread, box = _in_thread(lambda: vec.query("synthetic document 1 topic 1", n_results=N))
    assert pausing.paused.wait(30)
    with shared.unit_of_work():
        vec.delete(["n05"])
    pausing.resume.set()
    thread.join(30)
    assert "error" not in box, box
    assert build == []
    assert vec._ann_cache is None


def test_compare_and_install_share_one_critical_section(env):
    path, shared, vec, make = env
    real = vec._ann_epoch_lock

    class Hook:
        fired = False

        def __enter__(self):
            real.acquire()

        def __exit__(self, *exc):
            real.release()
            if not Hook.fired:
                Hook.fired = True
                vec._drop_ann_cache()

    epoch = vec._ann_epoch
    vec._ann_epoch_lock = Hook()
    assert vec.query("synthetic document 1 topic 1")
    assert Hook.fired
    assert vec._ann_cache is None and vec._ann_epoch == epoch + 1


@pytest.mark.parametrize("commit_while_waiting", [True, False])
def test_waiting_query_reuses_or_skips_the_build(env, monkeypatch, commit_while_waiting):
    path, shared, vec, make = env
    build = _counter(monkeypatch, vec, "_build_ann_cache")
    paused, resume = _pause_once(monkeypatch, vec, "_quantize_rows")
    real_lock = vec._ann_cache_lock
    attempts = []
    second = threading.Event()

    class Wrapped:
        def __enter__(self):
            attempts.append(1)
            if len(attempts) == 2:
                second.set()
            real_lock.acquire()

        def __exit__(self, *exc):
            real_lock.release()

    vec._ann_cache_lock = Wrapped()
    first, box_a = _in_thread(lambda: vec.query("synthetic document 1 topic 1", n_results=N))
    assert paused.wait(30)
    waiter, box_w = _in_thread(lambda: vec.query("synthetic document 2 topic 2", n_results=N))
    assert second.wait(30)
    if commit_while_waiting:
        with shared.unit_of_work():
            vec.delete(["n05"])
    resume.set()
    first.join(30)
    waiter.join(30)
    assert "error" not in box_a and "error" not in box_w, (box_a, box_w)
    assert len(build) == 1
    if commit_while_waiting:
        assert vec._ann_cache is None
        assert "n05" not in _ids(box_w["value"])
    else:
        assert vec._ann_cache is not None


# ---------------------------------------------------------------------------
# load.py 호출 지점
# ---------------------------------------------------------------------------


def test_load_helper_skips_doubles_without_the_hook_and_drops_after_commit(env):
    from opencrab.pack.load import _invalidate_vec_ann

    path, shared, vec, make = env
    _invalidate_vec_ann(object())
    vec.query("synthetic document 1 topic 1")
    with shared.unit_of_work():
        _invalidate_vec_ann(vec)
        assert vec._ann_cache is not None
    assert vec._ann_cache is None


# ---------------------------------------------------------------------------
# r2: 팩 삭제 무효화, 대규모 범위의 최종 predicate, 트랜잭션 안 대규모 범위, 빈 표
# ---------------------------------------------------------------------------

SCOPE = ["A", "B"]


def _load_mixed(store):
    """팩 A, B, C와 공간 s1, s2가 섞인 행. A와 B에 두 공간이 모두 있다."""
    store.upsert_texts(
        texts=[f"synthetic document {i} topic {i % 7}" for i in range(N)],
        metadatas=[
            {"pack_id": PACKS[i % 3], "space": ("s1", "s2")[(i // 3) % 2]} for i in range(N)
        ],
        ids=list(IDS),
    )


@pytest.fixture(params=["single", "five"])
def mixed(request, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / f"{request.param}.db")
    shared = SharedSqliteFile(path) if request.param == "single" else None
    store = _open(path, shared)
    _load_mixed(store)
    yield request.param, shared, store
    store.close()


def _scope_where(spaces=None):
    from opencrab.ontology.query import _build_chroma_where

    return _build_chroma_where(spaces=spaces, pack_ids=SCOPE)


def test_delete_pack_vectors_drops_the_cache_after_commit(mixed):
    from opencrab.pack.load import _delete_pack_vectors

    kind, shared, vec = mixed
    assert vec.query("synthetic document 1 topic 1")
    assert vec._ann_cache is not None
    epoch = vec._ann_epoch
    if shared is None:
        count, _kind, confirmed = _delete_pack_vectors("A", vec, True)
        assert confirmed and count > 0
        assert vec._ann_cache is None and vec._ann_epoch == epoch + 1
        return
    with shared.unit_of_work():
        count, _kind, confirmed = _delete_pack_vectors("A", vec, True)
        assert confirmed and count > 0
        assert vec._ann_cache is not None and vec._ann_epoch == epoch
    assert vec._ann_cache is None and vec._ann_epoch == epoch + 1
    assert all(hit["metadata"]["pack_id"] != "A" for hit in vec.query("synthetic document 1 topic 1", n_results=N))


@pytest.mark.parametrize("ann", ["", "binary"])
@pytest.mark.parametrize("spaces", [None, ["s1"]])
def test_large_scope_never_returns_rows_outside_the_requested_scope(
    tmp_path, monkeypatch, ann, spaces
):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("opencrab.stores.sqlite_vec_store._PACK_KNN_MAX", 1)
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    vec = SqliteVecStore(path, embedding_function=MockEF(DIM), dim=DIM, ann=ann, shared=shared)
    try:
        _load_mixed(vec)
        assert {h["metadata"]["pack_id"] for h in vec.query("synthetic document 3 topic 3", n_results=N)} == set(PACKS)
        hits = vec.query("synthetic document 3 topic 3", n_results=N, where=_scope_where(spaces))
        assert hits
        assert {h["metadata"]["pack_id"] for h in hits} <= set(SCOPE)
        if spaces:
            assert {h["metadata"]["space"] for h in hits} == set(spaces)
        if ann == "binary":
            assert vec._ann_cache is not None  # the ANN path served the query
    finally:
        vec.close()


def test_identity_candidate_filter_still_returns_only_scoped_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("opencrab.stores.sqlite_vec_store._PACK_KNN_MAX", 1)
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    vec = _open(path, shared)
    called = []

    def identity(cand, cache, pack_scope):
        called.append(1)
        return cand

    try:
        _load_mixed(vec)
        monkeypatch.setattr(vec, "_filter_candidates_by_pack", identity)
        hits = vec.query("synthetic document 3 topic 3", n_results=N, where=_scope_where(["s1"]))
        assert called
        assert hits
        assert {h["metadata"]["pack_id"] for h in hits} <= set(SCOPE)
        assert {h["metadata"]["space"] for h in hits} == {"s1"}
    finally:
        vec.close()


def test_large_scope_inside_a_transaction_queries_each_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("opencrab.stores.sqlite_vec_store._PACK_KNN_MAX", 1)
    path = str(tmp_path / "single.db")
    shared = SharedSqliteFile(path)
    vec = _open(path, shared)
    try:
        _load_mixed(vec)
        packs_seen = []
        original = vec._knn

        def recording(qvec, k, pack):
            packs_seen.append(pack)
            return original(qvec, k, pack)

        monkeypatch.setattr(vec, "_knn", recording)
        build = _counter(monkeypatch, vec, "_build_ann_cache")
        with shared.unit_of_work():
            hits = vec.query("synthetic document 3 topic 3", n_results=N, where=_scope_where())
        assert sorted(packs_seen) == sorted(SCOPE)
        assert not build
        assert hits and {h["metadata"]["pack_id"] for h in hits} <= set(SCOPE)
    finally:
        vec.close()


@pytest.mark.parametrize("single", [True, False])
def test_empty_table_query_returns_nothing_before_and_after_deleting_everything(tmp_path, monkeypatch, single):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    path = str(tmp_path / "empty.db")
    vec = _open(path, SharedSqliteFile(path) if single else None)
    try:
        assert vec.query("synthetic document 1 topic 1") == []
        _load(vec)
        assert vec.query("synthetic document 1 topic 1")
        assert vec._ann_cache is not None
        vec.delete(IDS)
        assert vec.query("synthetic document 1 topic 1") == []
    finally:
        vec.close()
