"""Vector-store parity tests — ChromaStore vs SqliteVecStore.

green→green characterization: the same suite runs against both backends
(parametrized) and asserts identical behaviour, plus a direct cross-backend
equivalence test. Uses tmp_path only (no real data) and a deterministic MockEF
(no network). See docs/pgvector-migration-plan.md §11.
"""

from __future__ import annotations

import threading
import time

import pytest
from _vec_helpers import MockEF, build_vector_store

BACKENDS = ["chroma", "sqlite-vec", "pg"]

# node_id, text, metadata (pack_id + space present so where-filters are testable)
CORPUS = [
    ("n1", "apple fruit red sweet", {"pack_id": "A", "space": "s1"}),
    ("n2", "banana fruit yellow soft", {"pack_id": "A", "space": "s1"}),
    ("n3", "car vehicle fast road", {"pack_id": "B", "space": "s2"}),
    ("n4", "train vehicle rail steel", {"pack_id": "B", "space": "s1"}),
    ("n5", "python snake reptile", {"pack_id": "C", "space": "s2"}),
    ("n6", "java coffee bean code", {"pack_id": "C", "space": "s1"}),
]


def _load(store):
    texts = [t for _, t, _ in CORPUS]
    ids = [i for i, _, _ in CORPUS]
    metas = [m for _, _, m in CORPUS]
    store.upsert_texts(texts=texts, metadatas=metas, ids=ids)


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    s = build_vector_store(request.param, tmp_path)
    assert s.available
    yield s
    if request.param == "pg":
        # 공유 PG 테스트 DB에 테이블이 누적되지 않도록 teardown에서 drop
        # (다른 백엔드는 tmp_path 격리라 파일 삭제만으로 충분).
        try:
            from sqlalchemy import text

            with s._engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {s._table}"))
        except Exception:
            pass
    if hasattr(s, "close"):
        s.close()


# ---------------------------------------------------------------------------
# Per-backend contract (both backends must satisfy identically)
# ---------------------------------------------------------------------------


class TestVectorStoreContract:
    def test_upsert_and_count(self, store):
        _load(store)
        assert store.count() == len(CORPUS)

    def test_upsert_idempotent(self, store):
        store.upsert_texts(texts=["hello"], metadatas=[{"pack_id": "A"}], ids=["x"])
        store.upsert_texts(texts=["hello"], metadatas=[{"pack_id": "A"}], ids=["x"])
        assert store.count() == 1
        # same id, new content → document updated, still one row
        store.upsert_texts(texts=["world"], metadatas=[{"pack_id": "A"}], ids=["x"])
        assert store.count() == 1
        assert store.get_by_id("x")["document"] == "world"

    def test_upsert_replaces_metadata_not_merges(self, store):
        """[#175] upsert_texts on an existing id must fully REPLACE
        document+metadata, not merge the new metadata into the old — a stale
        key dropped by the caller's canonical transform must not survive.
        (chromadb's native upsert()/update() merge; this store's contract is
        replace, matching sqlite-vec's DELETE+INSERT and pgvector's
        ON CONFLICT DO UPDATE SET metadata = EXCLUDED.metadata.)"""
        store.upsert_texts(texts=["v1"], metadatas=[{"old": "1", "keep": "k"}], ids=["r"])
        store.upsert_texts(texts=["v2"], metadatas=[{"new": "2"}], ids=["r"])
        hit = store.get_by_id("r")
        assert hit["document"] == "v2"
        assert hit["metadata"] == {"new": "2"}

    def test_get_by_id(self, store):
        _load(store)
        hit = store.get_by_id("n3")
        assert hit is not None
        assert hit["id"] == "n3"
        assert hit["document"] == "car vehicle fast road"
        assert hit["metadata"]["pack_id"] == "B"
        assert store.get_by_id("nonexistent") is None

    def test_delete(self, store):
        _load(store)
        store.delete(["n1", "n2"])
        assert store.count() == len(CORPUS) - 2
        assert store.get_by_id("n1") is None

    def test_reset(self, store):
        _load(store)
        store.reset_collection()
        assert store.count() == 0
        # store still usable after reset
        store.upsert_texts(texts=["again"], metadatas=[{"pack_id": "Z"}], ids=["z"])
        assert store.count() == 1

    def test_empty_inputs(self, store):
        # sqlite-vec returns [] gracefully; Chroma rejects empty batches with
        # ValueError. Either way, no rows are created and no corruption occurs.
        for call in (lambda: store.upsert_texts(texts=[]), lambda: store.add_texts(texts=[])):
            try:
                assert call() == []
            except ValueError:
                pass
        try:
            store.delete([])  # sqlite-vec: no-op; Chroma: rejects empty
        except ValueError:
            pass
        assert store.count() == 0

    def test_length_mismatch_raises(self, store):
        # both backends must reject mismatched texts/metadatas/ids lengths
        # (never silently truncate via zip)
        with pytest.raises(Exception):
            store.upsert_texts(
                texts=["a", "b"], metadatas=[{"pack_id": "p"}], ids=["x"]
            )
        assert store.count() == 0

    def test_query_topk_ordering(self, store):
        _load(store)
        hits = store.query("fruit sweet banana", n_results=3)
        assert len(hits) == 3
        # distances ascending (nearest first)
        dists = [h["distance"] for h in hits]
        assert dists == sorted(dists)
        # keys present
        for h in hits:
            assert set(h.keys()) == {"id", "document", "metadata", "distance"}

    def test_query_n_results_cap(self, store):
        _load(store)
        hits = store.query("anything", n_results=2)
        assert len(hits) == 2

    def test_where_single_pack(self, store):
        _load(store)
        hits = store.query("fruit", n_results=10, where={"pack_id": "A"})
        assert {h["id"] for h in hits} == {"n1", "n2"}
        assert all(h["metadata"]["pack_id"] == "A" for h in hits)

    def test_where_in(self, store):
        _load(store)
        hits = store.query(
            "anything", n_results=10, where={"pack_id": {"$in": ["A", "C"]}}
        )
        assert {h["id"] for h in hits} == {"n1", "n2", "n5", "n6"}

    def test_where_and(self, store):
        _load(store)
        where = {"$and": [{"space": "s1"}, {"pack_id": {"$in": ["A", "B", "C"]}}]}
        hits = store.query("anything", n_results=10, where=where)
        assert {h["id"] for h in hits} == {"n1", "n2", "n4", "n6"}

    def test_where_missing_key_no_match(self, store):
        _load(store)
        hits = store.query("anything", n_results=10, where={"pack_id": "DOES_NOT_EXIST"})
        assert hits == []

    def test_distance_to_score_range(self, store):
        _load(store)
        hits = store.query("fruit", n_results=5)
        for h in hits:
            score = max(0.0, 1.0 - float(h["distance"]))
            assert 0.0 <= score <= 1.0

    def test_unavailable_raises(self, store):
        store._available = False
        with pytest.raises(RuntimeError):
            store.upsert_texts(texts=["x"], metadatas=[{"pack_id": "A"}], ids=["x"])
        # count() must NOT raise when unavailable (returns 0)
        assert store.count() == 0


# ---------------------------------------------------------------------------
# [#175 v2] Chroma-only: ids that already carry a chromadb ``uri`` are routed
# through native upsert() (merge) instead of delete()+add() (replace) — see
# ChromaStore.upsert_texts docstring. Real chromadb only (no mocks/doubles):
# the merge-vs-replace split under test IS chromadb's own upsert()/add()
# behavior, so a double would just assert against itself.
# ---------------------------------------------------------------------------


class TestChromaUriPreservation:
    def test_mismatched_batch_on_nonempty_collection_destroys_nothing(self, tmp_path):
        """리뷰 P1: 길이 불일치 배치가 delete 를 먼저 수행한 뒤 add 검증에서
        죽으면 기존 레코드가 소실된다 — 종전 native upsert 는 무파괴로
        거부했다. 검증은 어떤 변이보다도 먼저여야 한다. 기존 mismatch
        테스트는 빈 컬렉션이라 이 파괴성을 못 봤다."""
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["keep me"], metadatas=[{"pack_id": "p"}], ids=["survivor"])
        assert store.count() == 1
        with pytest.raises(ValueError):
            store.upsert_texts(
                texts=["a", "b"], metadatas=[{"pack_id": "p"}], ids=["survivor", "x"]
            )
        # 기존 레코드가 그대로 남아 있어야 한다 (delete 가 선행되지 않았음)
        assert store.count() == 1
        got = store.get_by_id("survivor")
        assert got is not None and got["document"] == "keep me"

    def _seed_uri_record(self, store, doc_id, document, metadata, uri, embedding=None):
        """Seed a uri-bearing record directly on the raw collection.
        chromadb's add(uris=...) raises ValueError without an explicit
        embedding on a collection with no data_loader (verified against
        chromadb 1.5.9), so this bypasses store.upsert_texts()/add_texts()."""
        emb = embedding or [0.1] * 32
        store._collection.add(
            ids=[doc_id], embeddings=[emb], documents=[document],
            metadatas=[metadata], uris=[uri],
        )

    def test_u1_uri_record_upsert_preserves_uri_and_updates_values(self, tmp_path):
        # U1: uri record → upsert_texts → uri kept, document/meta updated,
        # stale meta keys survive (merge semantics via native upsert()).
        store = build_vector_store("chroma", tmp_path)
        self._seed_uri_record(
            store, "r1", "doc v1", {"old": "1", "keep": "k"}, "http://example.com/r1"
        )

        store.upsert_texts(texts=["doc v2"], metadatas=[{"new": "2"}], ids=["r1"])

        got = store._collection.get(ids=["r1"], include=["documents", "metadatas", "uris"])
        assert got["uris"][0] == "http://example.com/r1", "uri 가 사라졌다"
        assert got["documents"][0] == "doc v2", "document 이 갱신되지 않았다"
        assert got["metadatas"][0] == {"old": "1", "keep": "k", "new": "2"}, (
            f"메타 병합 결과가 다르다(스테일 키 존속 + 새 키 반영): {got['metadatas'][0]}"
        )

    def test_u1b_mixed_batch_routes_each_id_through_the_right_path(self, tmp_path):
        # U1b: uri record + plain existing record + brand-new id in ONE
        # upsert_texts call → each must land on its correct path, and the
        # underlying collection calls must show it (uri id via upsert() only,
        # the other two via delete()+add()).
        store = build_vector_store("chroma", tmp_path)
        self._seed_uri_record(
            store, "uri1", "u-doc-v1", {"stale": "old", "keep": "k"},
            "http://example.com/uri1",
        )
        store.upsert_texts(texts=["plain-doc-v1"], metadatas=[{"stale": "old2"}], ids=["plain1"])
        # "new1" intentionally never seeded — brand-new id in the mixed batch.

        calls: dict[str, list[list[str]]] = {"upsert": [], "delete": [], "add": []}
        real_upsert = store._collection.upsert
        real_delete = store._collection.delete
        real_add = store._collection.add

        def wrap_upsert(**kw):
            calls["upsert"].append(list(kw.get("ids", [])))
            return real_upsert(**kw)

        def wrap_delete(**kw):
            calls["delete"].append(list(kw.get("ids", [])))
            return real_delete(**kw)

        def wrap_add(**kw):
            calls["add"].append(list(kw.get("ids", [])))
            return real_add(**kw)

        store._collection.upsert = wrap_upsert
        store._collection.delete = wrap_delete
        store._collection.add = wrap_add

        store.upsert_texts(
            texts=["u-doc-v2", "plain-doc-v2", "new-doc-v1"],
            metadatas=[{"new": "u2"}, {"new": "p2"}, {"new": "n1"}],
            ids=["uri1", "plain1", "new1"],
        )

        # uri1 은 uri 를 들고 있어서, new1 은 기존 키가 없어서(따라서 스테일이
        # 생길 수 없어서) 원자적 upsert 로 간다. plain1 만 키를 버리므로 치환이다.
        assert calls["upsert"] and set(calls["upsert"][0]) == {"uri1", "new1"}, (
            f"스테일 키가 없는 id 가 원자적 upsert 경로를 안 탔다: {calls}"
        )
        assert calls["delete"] and set(calls["delete"][0]) == {"plain1"}, (
            f"키를 버리는 id 가 delete+add(치환) 경로를 안 탔다: {calls}"
        )
        assert calls["add"] and set(calls["add"][0]) == {"plain1"}, calls

        got = store._collection.get(
            ids=["uri1", "plain1", "new1"], include=["documents", "metadatas", "uris"]
        )
        by_id = dict(zip(got["ids"], zip(got["documents"], got["metadatas"], got["uris"])))

        uri_doc, uri_meta, uri_uri = by_id["uri1"]
        assert uri_uri == "http://example.com/uri1", "uri 레코드의 uri 가 사라졌다"
        assert uri_doc == "u-doc-v2"
        assert uri_meta == {"stale": "old", "keep": "k", "new": "u2"}, (
            f"uri 레코드는 병합이어야 한다(스테일 키 존속): {uri_meta}"
        )

        plain_doc, plain_meta, plain_uri = by_id["plain1"]
        assert plain_uri is None
        assert plain_doc == "plain-doc-v2"
        assert plain_meta == {"new": "p2"}, (
            f"일반 레코드는 치환이어야 한다(스테일 키 소멸): {plain_meta}"
        )

        new_doc, new_meta, new_uri = by_id["new1"]
        assert new_uri is None
        assert new_doc == "new-doc-v1"
        assert new_meta == {"new": "n1"}

    def test_u2_plain_record_upsert_replaces_document_and_drops_stale_keys(self, tmp_path):
        # U2: no uri anywhere → unchanged replace contract (delete+add):
        # document replaced, stale meta keys dropped. Strengthens the
        # existing cross-backend parity test with an explicit uris=None check.
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["v1"], metadatas=[{"old": "1", "keep": "k"}], ids=["p1"])
        store.upsert_texts(texts=["v2"], metadatas=[{"new": "2"}], ids=["p1"])

        got = store._collection.get(ids=["p1"], include=["documents", "metadatas", "uris"])
        assert got["documents"][0] == "v2"
        assert got["metadatas"][0] == {"new": "2"}, "스테일 키가 살아남았다 — 치환이 아니다"
        assert got["uris"][0] is None


class _FlakyEF(MockEF):
    """MockEF that starts raising once armed — stands in for an embedding
    backend (remote + local fallback) that is down. Counts its calls so a test
    can prove the rollback path does not depend on it."""

    def __init__(self, dim: int = 32):
        super().__init__(dim)
        self.armed = False
        self.calls = 0

    def __call__(self, input):  # noqa: A002 - chroma's EF protocol names it `input`
        self.calls += 1
        if self.armed:
            raise RuntimeError("embedding backend unavailable")
        return super().__call__(input)


def _chroma_store_with_ef(tmp_path, ef):
    from opencrab.stores.chroma_store import ChromaStore

    return ChromaStore(
        host="localhost", port=0, collection_name="vtest", local_mode=True,
        local_path=str(tmp_path / "chroma"), embedding_function=ef,
    )


class _TrackingLock:
    """Wraps a store's lock and signals every acquire ATTEMPT *before* it can
    block, so a thread waiting on the critical section is observable instead
    of being inferred from elapsed time."""

    def __init__(self, inner):
        self._inner = inner
        self.attempts = threading.Semaphore(0)

    def acquire(self, *args, **kwargs):
        self.attempts.release()
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class _BlockingCollection:
    """Collection double whose add() parks inside the critical section."""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []
        self.inside_add = threading.Event()
        self.release = threading.Event()

    def get(self, ids, include=None):
        self.calls.append(("get", list(ids)))
        # 기존 레코드가 있고 그 메타 키를 새 메타가 버리는 형태로 보고한다 —
        # 그래야 치환(delete+add) 경로가 돌고 add 의 barrier 가 발동한다.
        # 스냅샷 필드도 함께 줘야 스토어의 롤백 준비가 성립한다.
        return {
            "ids": list(ids),
            "uris": [None for _ in ids],
            "documents": ["기존" for _ in ids],
            "metadatas": [{"stale": "1"} for _ in ids],
            "embeddings": [[0.1] * 32 for _ in ids],
        }

    def delete(self, ids):
        self.calls.append(("delete", list(ids)))

    def add(self, documents, metadatas, ids):
        self.calls.append(("add", list(ids)))
        self.inside_add.set()
        assert self.release.wait(timeout=10), "테스트 하네스가 add 를 풀어주지 않았다"
        self.calls.append(("add-end", list(ids)))

    def upsert(self, **kw):
        self.calls.append(("upsert", list(kw["ids"])))


class TestChromaUpsertSafety:
    """[#175 리뷰 라운드3] 변이 전 검증(P1)과 동일 id 직렬화(P2)."""

    def test_omitted_metadata_on_nonempty_collection_destroys_nothing(self, tmp_path):
        """P1: metadatas 를 생략하면 [{}] 가 되는데 chroma 는 빈 dict 를 거부한다.
        치환 경로는 delete 를 먼저 하므로, 검증이 변이 뒤에 오면 기존 레코드가
        영구 소실된다. 종전 native upsert 는 같은 입력을 무파괴로 거부했다."""
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["keep me"], metadatas=[{"pack_id": "p"}], ids=["survivor"])

        with pytest.raises(ValueError):
            store.upsert_texts(texts=["replacement"], ids=["survivor"])

        assert store.count() == 1
        got = store.get_by_id("survivor")
        assert got is not None and got["document"] == "keep me"
        assert got["metadata"] == {"pack_id": "p"}

    def test_mixed_batch_with_one_empty_metadata_destroys_nothing(self, tmp_path):
        # 배치 중 하나만 빈 dict 여도 마찬가지 — 전량이 변이 전에 거부돼야 한다.
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["v1"], metadatas=[{"pack_id": "p"}], ids=["a"])

        with pytest.raises(ValueError):
            store.upsert_texts(
                texts=["a2", "b2"], metadatas=[{"pack_id": "p"}, {}], ids=["a", "b"]
            )

        assert store.count() == 1
        got = store.get_by_id("a")
        assert got is not None and got["document"] == "v1"

    def test_same_id_upserts_are_serialised(self, tmp_path):
        """P2: 동일 id 동시 upsert 가 get→delete/add 구간에서 교차하면, 두 번째
        add 가 chroma 에서 조용히 무시되어(1.5.9 실측: 예외 없이 먼저 쓴 쪽이 이김)
        나중 기록자의 문서가 성공 보고와 함께 유실된다."""
        store = build_vector_store("chroma", tmp_path)
        col = _BlockingCollection()
        store._collection = col
        lock = _TrackingLock(store._lock)
        store._lock = lock

        errors: list[BaseException] = []

        def upsert(text, val):
            try:
                store.upsert_texts(texts=[text], metadatas=[{"w": val}], ids=["same"])
            except BaseException as exc:  # noqa: BLE001 - 아래에서 다시 단언한다
                errors.append(exc)

        t1 = threading.Thread(target=upsert, args=("a", "1"))
        t1.start()
        assert col.inside_add.wait(timeout=10), "T1 이 임계구역에 진입하지 못했다"
        assert lock.attempts.acquire(timeout=10), "T1 의 락 획득이 기록되지 않았다"

        t2 = threading.Thread(target=upsert, args=("b", "2"))
        t2.start()
        assert lock.attempts.acquire(timeout=10), (
            "T2 가 락 획득을 시도조차 하지 않았다 — 임계구역이 직렬화되지 않았다"
        )
        # 여기서 T2 는 T1 이 쥔 락 위에서 acquire 중임이 관측됐으므로 컬렉션 호출을
        # 냈을 수 없다. 이 단언에는 대기 시간이 개입하지 않는다.
        t1_only = [("get", ["same"]), ("delete", ["same"]), ("add", ["same"])]
        assert col.calls == t1_only, col.calls
        time.sleep(0.5)  # 락이 없다면 T2 는 마이크로초 단위로 세 호출을 끝낸다
        assert col.calls == t1_only, f"T1 이 임계구역을 쥔 채로 T2 가 진행했다: {col.calls}"

        col.release.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()
        assert not errors, errors
        assert [c[0] for c in col.calls] == [
            "get", "delete", "add", "add-end", "get", "delete", "add", "add-end",
        ], col.calls

    def test_embedding_failure_leaves_the_existing_record_intact(self, tmp_path):
        """[리뷰 라운드4] chroma 는 add() 안에서 임베딩한다. 임베딩 백엔드가 죽으면
        add 가 던지는데 그 시점엔 치환 경로의 delete 가 이미 끝나 기존 레코드가
        영구 소실된다. 종전 native upsert() 는 같은 실패에서 레코드를 보존했다."""
        ef = _FlakyEF()
        store = _chroma_store_with_ef(tmp_path, ef)
        store.upsert_texts(
            texts=["원본 문서"], metadatas=[{"pack_id": "p", "버릴키": "1"}], ids=["r1"]
        )

        ef.armed = True
        with pytest.raises(Exception):
            store.upsert_texts(texts=["새 문서"], metadatas=[{"pack_id": "p"}], ids=["r1"])

        ef.armed = False
        assert store.count() == 1, "임베딩 실패가 기존 레코드를 지웠다"
        got = store.get_by_id("r1")
        assert got is not None and got["document"] == "원본 문서"
        assert got["metadata"] == {"pack_id": "p", "버릴키": "1"}

    def test_rollback_after_embedding_failure_does_not_call_the_embedding_function(
        self, tmp_path
    ):
        # 복구가 방금 실패한 그것(EF)에 의존하면 정작 필요할 때 못 돈다.
        # 저장된 임베딩을 명시로 넘기므로 EF 호출이 늘어선 안 된다.
        ef = _FlakyEF()
        store = _chroma_store_with_ef(tmp_path, ef)
        store.upsert_texts(texts=["원본"], metadatas=[{"k": "1", "버릴키": "1"}], ids=["r1"])
        before = store._collection.get(ids=["r1"], include=["embeddings"])

        ef.armed = True
        calls_before = ef.calls
        with pytest.raises(Exception):
            store.upsert_texts(texts=["새"], metadatas=[{"k": "2"}], ids=["r1"])

        assert ef.calls == calls_before + 1, (
            f"복구가 EF 를 다시 호출했다(실패한 백엔드 의존): {ef.calls - calls_before}회"
        )
        ef.armed = False
        after = store._collection.get(ids=["r1"], include=["embeddings"])
        assert [list(map(float, v)) for v in after["embeddings"]] == [
            list(map(float, v)) for v in before["embeddings"]
        ], "복구된 임베딩이 원본과 다르다"

    def test_partially_applied_add_is_rolled_back(self, tmp_path):
        # add 가 일부만 반영하고 죽는 경우. 복구가 delete 를 선행하지 않으면
        # (chroma 의 add 는 기존 id 에 대해 조용한 no-op 이므로) 반쯤 쓰인
        # 새 레코드가 그대로 살아남는다.
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["A 원본"], metadatas=[{"k": "a", "버릴키": "1"}], ids=["a"])
        store.upsert_texts(texts=["B 원본"], metadatas=[{"k": "b", "버릴키": "1"}], ids=["b"])

        real_add = store._collection.add

        def half_then_fail(**kw):
            # 복구용 add 는 embeddings 를 명시로 넘긴다(EF 를 타지 않는다) —
            # 전진 경로만 실패시키고 복구는 통과시킨다.
            if "embeddings" in kw:
                return real_add(**kw)
            real_add(ids=kw["ids"][:1], documents=kw["documents"][:1],
                     metadatas=kw["metadatas"][:1])
            raise RuntimeError("add 가 일부만 반영하고 죽었다")

        store._collection.add = half_then_fail
        with pytest.raises(RuntimeError):
            store.upsert_texts(
                texts=["A 새", "B 새", "C 신규"],
                metadatas=[{"k": "a2"}, {"k": "b2"}, {"k": "c"}],
                ids=["a", "b", "new"],
            )
        store._collection.add = real_add

        assert store.get_by_id("a")["document"] == "A 원본", "부분 반영분이 살아남았다"
        assert store.get_by_id("b")["document"] == "B 원본"
        assert store.get_by_id("new") is None, "호출 전에 없던 id 가 남았다"
        assert store.count() == 2

    def test_uri_merge_is_rolled_back_when_a_later_replace_fails(self, tmp_path):
        # 분기 경로에서 merge 가 먼저 성공한 뒤 replace 가 죽으면 배치가 반만
        # 쓰인 채 남는다 — 롤백은 배치 전체를 호출 전 상태로 되돌려야 한다.
        store = build_vector_store("chroma", tmp_path)
        store._collection.add(
            ids=["uri1"], embeddings=[[0.1] * 32], documents=["uri 원본"],
            metadatas=[{"k": "u"}], uris=["http://example.com/uri1"],
        )
        store.upsert_texts(
            texts=["plain 원본"], metadatas=[{"k": "p", "버릴키": "1"}], ids=["plain1"]
        )

        real_add = store._collection.add

        def fail_forward_only(**kw):
            if "embeddings" in kw:      # 복구 경로는 통과시킨다
                return real_add(**kw)
            raise RuntimeError("replace 단계 실패")

        store._collection.add = fail_forward_only
        with pytest.raises(RuntimeError):
            store.upsert_texts(
                texts=["uri 새", "plain 새"],
                metadatas=[{"k": "u2"}, {"k": "p2"}],
                ids=["uri1", "plain1"],
            )
        store._collection.add = real_add

        got = store._collection.get(
            ids=["uri1", "plain1"], include=["documents", "metadatas", "uris"]
        )
        by_id = dict(zip(got["ids"], zip(got["documents"], got["metadatas"], got["uris"])))
        assert by_id["uri1"] == (
            "uri 원본", {"k": "u"}, "http://example.com/uri1"
        ), f"성공한 merge 가 롤백되지 않았다: {by_id['uri1']}"
        assert by_id["plain1"][0] == "plain 원본"
        assert by_id["plain1"][2] is None

    def test_rollback_restores_records_that_had_no_metadata(self, tmp_path):
        # 스토어에 이미 있는 레코드는 metadata 가 없을 수 있다 — chroma 는 그걸
        # None(메타 없이 add) 또는 {}(외부가 쓴 uri 레코드)로 돌려준다. 스냅샷을
        # 그대로 replay 하면 롤백의 add 가 빈 dict 규칙에 걸려 죽고, 구하려던
        # 레코드까지 잃는다.
        store = build_vector_store("chroma", tmp_path)
        store._collection.add(
            ids=["nometa"], embeddings=[[0.2] * 32], documents=["메타 없는 문서"],
        )
        store._collection.add(
            ids=["urinometa"], embeddings=[[0.3] * 32], documents=["uri 문서"],
            uris=["http://example.com/urinometa"],
        )
        store.upsert_texts(
            texts=["보통 문서"], metadatas=[{"k": "1", "버릴키": "1"}], ids=["plain"]
        )

        real_add = store._collection.add

        def fail_forward_only(**kw):
            if "embeddings" in kw:      # 복구 경로는 통과시킨다
                return real_add(**kw)
            raise RuntimeError("전진 add 실패")

        store._collection.add = fail_forward_only
        with pytest.raises(RuntimeError):
            store.upsert_texts(
                texts=["새1", "새2", "새3"],
                metadatas=[{"k": "n1"}, {"k": "n2"}, {"k": "n3"}],
                ids=["nometa", "urinometa", "plain"],
            )
        store._collection.add = real_add

        assert store.count() == 3, "메타 없는 레코드 때문에 롤백이 실패했다"
        got = store._collection.get(
            ids=["nometa", "urinometa", "plain"],
            include=["documents", "metadatas", "uris"],
        )
        by_id = dict(zip(got["ids"], zip(got["documents"], got["metadatas"], got["uris"])))
        assert by_id["nometa"][0] == "메타 없는 문서"
        assert not by_id["nometa"][1], f"없던 메타가 생겼다: {by_id['nometa'][1]}"
        assert by_id["urinometa"][0] == "uri 문서"
        assert by_id["urinometa"][2] == "http://example.com/urinometa", "uri 가 사라졌다"
        assert by_id["plain"] == ("보통 문서", {"k": "1", "버릴키": "1"}, None)

    def test_rollback_restores_records_that_have_no_document(self, tmp_path):
        """레코드는 document 없이도 존재할 수 있다(임베딩만, 또는 uri 만 들고
        외부가 기록한 경우). 스냅샷의 그 None 을 되돌려 쓰는 것이 chromadb 에서
        거부되면 롤백이 죽고 레코드까지 잃는다 — 현재(1.5.9)는 거부하지 않으며,
        이 테스트가 그 전제를 고정한다."""
        store = build_vector_store("chroma", tmp_path)
        store._collection.add(ids=["nodoc"], embeddings=[[0.2] * 32], metadatas=[{"k": "1"}])
        store._collection.add(
            ids=["urinodoc"], embeddings=[[0.3] * 32],
            uris=["http://example.com/urinodoc"],
        )
        store.upsert_texts(texts=["원본"], metadatas=[{"k": "p", "버릴키": "1"}], ids=["plain"])

        real_add = store._collection.add

        def fail_forward_only(**kw):
            if "embeddings" in kw:      # 복구 경로는 통과시킨다
                return real_add(**kw)
            raise RuntimeError("전진 add 실패")

        store._collection.add = fail_forward_only
        with pytest.raises(RuntimeError):
            store.upsert_texts(
                texts=["새1", "새2", "새3"],
                metadatas=[{"k": "n1"}, {"k": "n2"}, {"k": "n3"}],
                ids=["nodoc", "urinodoc", "plain"],
            )
        store._collection.add = real_add

        assert store.count() == 3, "document 없는 레코드 때문에 롤백이 실패했다"
        got = store._collection.get(
            ids=["nodoc", "urinodoc", "plain"],
            include=["documents", "metadatas", "uris"],
        )
        by_id = dict(zip(got["ids"], zip(got["documents"], got["metadatas"], got["uris"])))
        assert by_id["nodoc"] == (None, {"k": "1"}, None)
        assert by_id["urinodoc"][0] is None
        assert by_id["urinodoc"][2] == "http://example.com/urinodoc", "uri 가 사라졌다"
        assert by_id["plain"][0] == "원본"

    def test_upsert_without_stale_keys_never_deletes(self, tmp_path):
        """[리뷰 라운드5] 버려질 키가 없으면 병합 결과가 곧 치환 결과다. 그때는
        원자적 native upsert 로 가야 한다 — delete 창이 없어야 reader 가 살아
        있는 레코드를 부재로 읽지 않는다."""
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["v1"], metadatas=[{"a": "1"}], ids=["r1"])

        calls: dict[str, list[list[str]]] = {"upsert": [], "delete": [], "add": []}
        for name in calls:
            real = getattr(store._collection, name)

            def wrap(_real=real, _name=name, **kw):
                calls[_name].append(list(kw.get("ids", [])))
                return _real(**kw)

            setattr(store._collection, name, wrap)

        store.upsert_texts(texts=["v2"], metadatas=[{"a": "2", "b": "3"}], ids=["r1"])

        assert calls["delete"] == [], f"버릴 키가 없는데 삭제했다: {calls}"
        assert calls["upsert"] == [["r1"]], f"원자적 경로를 안 탔다: {calls}"
        got = store._collection.get(ids=["r1"], include=["documents", "metadatas"])
        assert got["documents"][0] == "v2"
        assert got["metadatas"][0] == {"a": "2", "b": "3"}

    def test_upsert_dropping_a_key_still_replaces(self, tmp_path):
        # #175 본래 계약: 호출자가 버린 키는 실제로 사라져야 한다. 라우팅이
        # 원래 버그를 삼키지 않는지 지킨다.
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["v1"], metadatas=[{"a": "1", "stale": "x"}], ids=["r1"])
        store.upsert_texts(texts=["v2"], metadatas=[{"a": "2"}], ids=["r1"])

        got = store._collection.get(ids=["r1"], include=["documents", "metadatas"])
        assert got["documents"][0] == "v2"
        assert got["metadatas"][0] == {"a": "2"}, "스테일 키가 살아남았다"

    def test_get_by_id_rechecks_under_the_lock_on_a_miss(self, tmp_path):
        """치환 중(delete~add 사이)에 읽어도 살아 있는 레코드를 부재로 보고하면
        안 된다. pack 정체성 검사가 None 을 '충돌 없음'으로 읽기 때문이다."""
        store = build_vector_store("chroma", tmp_path)
        store.upsert_texts(texts=["원본"], metadatas=[{"k": "1", "버릴키": "1"}], ids=["r1"])

        real_add = store._collection.add
        in_window = threading.Event()
        release = threading.Event()

        def park_then_add(**kw):
            if "embeddings" not in kw:      # 전진 경로의 add 에서만 멈춘다
                in_window.set()
                assert release.wait(timeout=10)
            return real_add(**kw)

        store._collection.add = park_then_add

        # 노출되는 인터리빙은 "핸들을 이미 스냅샷한 reader"다. 스냅샷 직후
        # 선점된 상황을 주입한다 — 그냥 읽으면 _collection_handle() 이 같은
        # 락에 걸려 창을 아예 못 보고, 테스트가 무의미해진다.
        real_handle = store._collection_handle

        def snapshot_then_wait():
            handle = real_handle()
            assert in_window.wait(timeout=10), "치환 창에 진입하지 못했다"
            return handle

        store._collection_handle = snapshot_then_wait

        seen = []
        reader = threading.Thread(target=lambda: seen.append(store.get_by_id("r1")))
        reader.start()
        writer = threading.Thread(
            target=store.upsert_texts,
            kwargs=dict(texts=["새"], metadatas=[{"k": "2"}], ids=["r1"]),
        )
        writer.start()
        assert in_window.wait(timeout=10), "치환 창에 진입하지 못했다"
        # 이 시점에 레코드는 delete 됐고 add 는 아직이다.
        assert store._collection.get(ids=["r1"])["ids"] == [], "창 재현 실패"

        release.set()
        writer.join(timeout=10)
        reader.join(timeout=10)
        store._collection.add = real_add
        store._collection_handle = real_handle
        assert seen and seen[0] is not None, "살아 있는 레코드를 부재로 보고했다"
        assert seen[0]["document"] == "새"

    def test_same_collection_stores_share_one_lock(self, tmp_path):
        # 인스턴스가 하나뿐이라는 보장이 없으므로(_get_context 는 초기화를 잠그지
        # 않는다) 락은 인스턴스가 아니라 컬렉션 단위여야 한다.
        a = build_vector_store("chroma", tmp_path)
        b = build_vector_store("chroma", tmp_path)
        other = build_vector_store("chroma", tmp_path / "elsewhere")

        assert a._lock is b._lock, "같은 컬렉션인데 임계구역이 분리돼 있다"
        assert other._lock is not a._lock, "다른 컬렉션까지 직렬화하고 있다"


# ---------------------------------------------------------------------------
# Cross-backend equivalence (Chroma vs sqlite-vec, same EF/data → same results)
# ---------------------------------------------------------------------------


def test_cross_backend_parity(tmp_path):
    chroma = build_vector_store("chroma", tmp_path / "c")
    sv = build_vector_store("sqlite-vec", tmp_path / "s")
    _load(chroma)
    _load(sv)

    queries = ["fruit sweet", "vehicle fast", "code snake", "coffee bean"]

    # No-where: identical ordering + close distances.
    for q in queries:
        c_hits = chroma.query(q, n_results=len(CORPUS))
        s_hits = sv.query(q, n_results=len(CORPUS))
        assert [h["id"] for h in c_hits] == [h["id"] for h in s_hits], (
            f"ordering mismatch for {q!r}"
        )
        c_dist = {h["id"]: h["distance"] for h in c_hits}
        s_dist = {h["id"]: h["distance"] for h in s_hits}
        for nid in c_dist:
            assert abs(c_dist[nid] - s_dist[nid]) < 1e-3, (
                f"distance mismatch {nid} for {q!r}: {c_dist[nid]} vs {s_dist[nid]}"
            )

    # With where: identical result sets.
    wheres = [
        {"pack_id": "A"},
        {"pack_id": {"$in": ["A", "C"]}},
        {"$and": [{"space": "s1"}, {"pack_id": {"$in": ["A", "B", "C"]}}]},
    ]
    for w in wheres:
        c_ids = {h["id"] for h in chroma.query("anything", n_results=10, where=w)}
        s_ids = {h["id"] for h in sv.query("anything", n_results=10, where=w)}
        assert c_ids == s_ids, f"where set mismatch for {w}: {c_ids} vs {s_ids}"

    if hasattr(sv, "close"):
        sv.close()


# ---------------------------------------------------------------------------
# Scale > vec0 k-limit (4096): the store must NOT crash and pushdown stays exact
# (regression guard for the k=4096 cap; small corpora never exercise this).
# ---------------------------------------------------------------------------


def _bruteforce_topk(ef, corpus, query, k, packs=None):
    """Exact top-k node_ids by cosine (MockEF vectors are unit-norm → cos=dot)."""
    qv = ef([query])[0]
    scored = []
    for _id, text, meta in corpus:
        if packs is not None and meta.get("pack_id") not in packs:
            continue
        v = ef([text])[0]
        scored.append((sum(a * b for a, b in zip(qv, v)), _id))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, i in scored[:k]]


def _bruteforce_topk_filter(ef, corpus, query, k, predicate):
    """Exact top-k node_ids by cosine among corpus items matching `predicate`."""
    qv = ef([query])[0]
    scored = []
    for _id, text, meta in corpus:
        if not predicate(meta):
            continue
        v = ef([text])[0]
        scored.append((sum(a * b for a, b in zip(qv, v)), _id))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, i in scored[:k]]


def test_scale_over_4096_no_crash_and_pushdown_exact(tmp_path):
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    ef = MockEF(32)
    store = SqliteVecStore(
        db_path=str(tmp_path / "vbig.db"),
        embedding_function=ef,
        dim=32,
        collection_name="vbig",
    )
    N = 4200  # > vec0's k cap of 4096
    corpus = [
        (f"n{i}", f"text number {i} content", {"pack_id": f"p{i % 3}",
                                               "space": "s1" if i % 4 == 0 else "s2"})
        for i in range(N)
    ]
    store.add_texts(
        texts=[t for _, t, _ in corpus],
        metadatas=[m for _, _, m in corpus],
        ids=[i for i, _, _ in corpus],
    )
    assert store.count() == N
    q = "text number 7 content"

    # 1. Every filter path must NOT raise (the C1 k>4096 crash) and stay bounded.
    for where in [
        None,
        {"pack_id": "p0"},
        {"pack_id": {"$in": ["p0", "p1"]}},
        {"$and": [{"space": "s1"}, {"pack_id": "p0"}]},
        {"$and": [{"space": "s1"}, {"pack_id": {"$in": ["p0", "p1"]}}]},
        {"space": "s1"},
    ]:
        hits = store.query(q, n_results=10, where=where)
        assert len(hits) <= 10

    # 1b. Force fetch_k to vec0's 4096 cap so the clamp is exercised (removing the
    #     clamp makes these raise OperationalError):
    #       n_results=5000, no where  → fetch_k=5000 → clamp 4096
    #       residual (space) filter    → fetch_k = _VEC0_K_MAX (4096)
    big = store.query(q, n_results=5000, where=None)
    assert len(big) <= 5000
    res = store.query(q, n_results=500, where={"space": "s1"})
    assert all(h["metadata"].get("space") == "s1" for h in res)  # residual filter honored
    store.query(q, n_results=600, where={"$and": [{"space": "s1"}, {"pack_id": "p0"}]})

    # 1c. duplicate pack in $in must not yield duplicate result rows
    dup = store.query(q, n_results=10, where={"pack_id": {"$in": ["p0", "p0"]}})
    assert len({h["id"] for h in dup}) == len(dup)

    # 2. pack isolation at scale
    hits = store.query(q, n_results=10, where={"pack_id": "p1"})
    assert hits and all(h["metadata"]["pack_id"] == "p1" for h in hits)

    # 3. pushdown exactness vs brute-force ground truth (single pack + $in)
    assert [h["id"] for h in store.query(q, n_results=10, where={"pack_id": "p0"})] \
        == _bruteforce_topk(ef, corpus, q, 10, packs={"p0"})
    assert [h["id"] for h in store.query(
        q, n_results=10, where={"pack_id": {"$in": ["p0", "p1"]}})] \
        == _bruteforce_topk(ef, corpus, q, 10, packs={"p0", "p1"})

    store.close()


def test_residual_filter_recall_exact_within_cap(tmp_path):
    """Residual (non-pack) filter recall is EXACT when the corpus fits within
    vec0's k cap. Regression guard for C1: the residual post-filter must scan up
    to _VEC0_K_MAX and must not silently drop matches that rank beyond a small
    inflate (the pre-fix code fetched only n_results*12 and returned []/short)."""
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    ef = MockEF(32)
    store = SqliteVecStore(
        db_path=str(tmp_path / "vres.db"), embedding_function=ef, dim=32,
        collection_name="vres",
    )
    N = 2000  # < 4096 → top-4096 scan covers the whole corpus → exact recall
    corpus = [
        (f"n{i}", f"doc {i} body text", {"pack_id": f"p{i % 3}",
                                         "space": "s1" if i % 7 == 0 else "s2"})
        for i in range(N)
    ]
    store.add_texts(
        texts=[t for _, t, _ in corpus], metadatas=[m for _, _, m in corpus],
        ids=[i for i, _, _ in corpus],
    )
    q = "doc 13 body text"
    # residual-only (space, not pushable) — matches must be recalled exactly
    got = [h["id"] for h in store.query(q, n_results=10, where={"space": "s1"})]
    exact = _bruteforce_topk_filter(ef, corpus, q, 10, lambda m: m.get("space") == "s1")
    assert got == exact
    assert all(h["metadata"]["space"] == "s1"
               for h in store.query(q, n_results=10, where={"space": "s1"}))
    store.close()


def test_add_texts_duplicate_id_raises(tmp_path):
    """Documented divergence from Chroma: vec0 has no INSERT OR IGNORE, so
    add_texts raises on a duplicate primary key (Chroma warns and skips)."""
    import sqlite3

    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    store = SqliteVecStore(
        db_path=str(tmp_path / "vdup.db"), embedding_function=MockEF(16), dim=16,
        collection_name="vdup",
    )
    store.add_texts(texts=["a"], metadatas=[{"pack_id": "p"}], ids=["x"])
    with pytest.raises(sqlite3.Error):
        store.add_texts(texts=["b"], metadatas=[{"pack_id": "p"}], ids=["x"])
    assert store.count() == 1
    store.close()
