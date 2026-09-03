"""[#197] 벡터 슬롯 소유권 게이트 — 소유된 슬롯은 그 소유자만 다시 쓴다.

종전에는 벡터 슬롯의 정체성이 ``node_id`` 하나였고 ``pack_id`` 로 한정되지 않아서,
서로 다른 팩이 같은 ``node_id`` 를 쓰면 마지막에 쓴 쪽이 슬롯을 통째로 가져갔다.
팩 A 의 문서와 임베딩이 조용히 사라지고, 팩 A 로 스코프한 질의가 0건이 됐다.

이 모듈은 세 백엔드(chroma / sqlite-vec / pgvector)에 같은 계약을 건다. 새 계약은
``docs/vector-backends.md`` §8.2 와 ``opencrab/stores/_vector_base.py`` 의 CONTRACT 절에 있다.
"""

from __future__ import annotations

import pytest
from _vec_helpers import build_vector_store

BACKENDS = ["chroma", "sqlite-vec", "pg"]


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    s = build_vector_store(request.param, tmp_path)
    assert s.available
    s.backend_name = request.param
    yield s
    if request.param == "pg":
        try:
            from sqlalchemy import text

            with s._engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {s._table}"))
        except Exception:
            pass
    if hasattr(s, "close"):
        s.close()


def _seed_pack_a(store) -> None:
    store.upsert_texts(texts=["팩 A 원본"], metadatas=[{"pack_id": "A"}], ids=["shared"])


def _assert_pack_a_intact(store) -> None:
    hit = store.get_by_id("shared")
    assert hit is not None, "팩 A 의 슬롯이 사라졌다"
    assert hit["metadata"].get("pack_id") == "A", (
        f"슬롯 소유가 팩 A 가 아니다: {hit['metadata'].get('pack_id')!r}")
    assert hit["document"] == "팩 A 원본", f"팩 A 의 문서가 바뀌었다: {hit['document']!r}"


# ---------------------------------------------------------------------------
# 정상 — 게이트가 통과시켜야 하는 쓰기
# ---------------------------------------------------------------------------


class TestOwnedSlotWritesThatMustPass:
    def test_new_id_is_written(self, store):
        """정상: 기존 행이 없는 id 는 어느 팩이 써도 통과한다."""
        store.upsert_texts(texts=["새 값"], metadatas=[{"pack_id": "B"}], ids=["fresh"])
        assert store.get_by_id("fresh")["metadata"]["pack_id"] == "B"

    def test_same_pack_rewrites_its_own_slot(self, store):
        """정상: 자기 팩 재적재는 문서를 갱신한다(증분 적재의 정상 경로)."""
        _seed_pack_a(store)
        store.upsert_texts(texts=["팩 A 갱신"], metadatas=[{"pack_id": "A"}], ids=["shared"])
        hit = store.get_by_id("shared")
        assert hit["document"] == "팩 A 갱신"
        assert hit["metadata"]["pack_id"] == "A"

    def test_pack_takes_over_an_unowned_slot(self, store):
        """정상: `pack_id` 가 비어 있는 미소유 슬롯은 팩이 인수할 수 있다.

        마이그레이션과 백필이 이 경로를 쓴다
        (`scripts/migrate_pack_ownership.py` 의 `_backfill_vector`).
        """
        store.upsert_texts(texts=["미소유"], metadatas=[{"pack_id": ""}], ids=["orphan"])
        store.upsert_texts(texts=["이제 A 소유"], metadatas=[{"pack_id": "A"}], ids=["orphan"])
        hit = store.get_by_id("orphan")
        assert hit["metadata"]["pack_id"] == "A"
        assert hit["document"] == "이제 A 소유"


# ---------------------------------------------------------------------------
# 오류 — 게이트가 거부해야 하는 쓰기
# ---------------------------------------------------------------------------


class TestForeignSlotWritesAreRejected:
    def test_cross_pack_overwrite_raises_and_leaves_the_owner_untouched(self, store):
        """오류: 팩 B 가 팩 A 소유 슬롯에 쓰면 거부되고 팩 A 의 행이 무손상이다.

        이것이 #197 의 재현이다. 종전에는 이 호출이 조용히 성공하고 팩 A 의
        문서와 임베딩이 사라졌다.
        """
        _seed_pack_a(store)
        with pytest.raises(ValueError, match="pack"):
            store.upsert_texts(
                texts=["팩 B 침범"], metadatas=[{"pack_id": "B"}], ids=["shared"])
        _assert_pack_a_intact(store)

    def test_the_owners_vector_still_answers_a_pack_scoped_query(self, store):
        """오류 축의 실동작 확인: 거부 뒤에도 팩 A 스코프 질의가 A 의 슬롯을 낸다.

        종전에는 이 질의가 0건이 됐다(슬롯이 팩 B 로 넘어갔으므로).
        """
        _seed_pack_a(store)
        with pytest.raises(ValueError):
            store.upsert_texts(
                texts=["팩 B 침범"], metadatas=[{"pack_id": "B"}], ids=["shared"])
        hits = store.query("팩 A 원본", n_results=5, where={"pack_id": "A"})
        assert [h["id"] for h in hits] == ["shared"], f"팩 A 질의가 비었다: {hits}"

    def test_unowned_write_over_an_owned_slot_is_rejected(self, store):
        """엣지: 빈 `pack_id` 로 남의 소유 슬롯을 덮는 것도 거부한다.

        "소유된 슬롯은 그 소유자만 다시 쓴다" 가 계약이다. 미소유 쓰기라고
        예외를 주면 팩의 벡터가 같은 방식으로 사라진다.
        """
        _seed_pack_a(store)
        with pytest.raises(ValueError, match="pack"):
            store.upsert_texts(
                texts=["소유 없는 쓰기"], metadatas=[{"pack_id": ""}], ids=["shared"])
        _assert_pack_a_intact(store)

    def test_missing_pack_id_key_over_an_owned_slot_is_rejected(self, store):
        """엣지: `pack_id` 키가 아예 없는 메타도 부재가 아니라 미소유로 읽는다."""
        _seed_pack_a(store)
        with pytest.raises(ValueError, match="pack"):
            store.upsert_texts(
                texts=["키 없음"], metadatas=[{"space": "s1"}], ids=["shared"])
        _assert_pack_a_intact(store)


# ---------------------------------------------------------------------------
# 엣지 — 배치
# ---------------------------------------------------------------------------


class TestBatchBehaviour:
    def test_one_foreign_id_rejects_the_whole_batch_with_no_partial_write(self, store):
        """엣지: 배치 3건 중 1건만 교차 팩이면 나머지 2건도 쓰이지 않는다.

        chroma 는 트랜잭션이 없고 큰 배치를 쪼개므로, 검사가 스토어를 만지기
        전에 끝나야 부분 적용이 생기지 않는다.
        """
        _seed_pack_a(store)
        before = store.count()
        with pytest.raises(ValueError, match="pack"):
            store.upsert_texts(
                texts=["B 하나", "B 침범", "B 셋"],
                metadatas=[{"pack_id": "B"}, {"pack_id": "B"}, {"pack_id": "B"}],
                ids=["b1", "shared", "b3"],
            )
        assert store.count() == before, "거부된 배치의 일부가 쓰였다"
        assert store.get_by_id("b1") is None
        assert store.get_by_id("b3") is None
        _assert_pack_a_intact(store)

    def test_cross_pack_duplicate_inside_one_batch_is_rejected(self, store):
        """엣지: 빈 슬롯이라도 한 배치 안에서 두 팩이 같은 id 를 다투면 거부한다.

        기존 행 대조만으로는 둘 다 "행 없음" 으로 통과하고, 그 뒤 백엔드가
        임의로 승자를 정한다. 그 자리에서 소유자를 정하게 두지 않는다.
        """
        with pytest.raises(ValueError, match="pack"):
            store.upsert_texts(
                texts=["A 값", "B 값"],
                metadatas=[{"pack_id": "A"}, {"pack_id": "B"}],
                ids=["contested", "contested"],
            )
        assert store.get_by_id("contested") is None, "거부된 배치가 행을 남겼다"

    def test_same_pack_duplicate_id_keeps_each_backends_existing_behaviour(self, store):
        """엣지: 같은 팩의 중복 id 에는 새 규칙을 걸지 않는다.

        그 자리의 현행 동작은 백엔드마다 다르고(실측: chroma 는 거부, SQL 두
        백엔드는 마지막 값 통과) 이 이슈와 무관하다. 게이트가 그 갈림을 바꾸지
        않는 것을 백엔드별로 고정한다. 통일은 후속이다.
        """
        call = lambda: store.upsert_texts(  # noqa: E731
            texts=["첫 값", "둘째 값"],
            metadatas=[{"pack_id": "A"}, {"pack_id": "A"}],
            ids=["dup", "dup"],
        )
        if store.backend_name == "chroma":
            with pytest.raises(Exception) as excinfo:
                call()
            assert type(excinfo.value).__name__ == "DuplicateIDError", (
                "chroma 의 중복 id 거부가 아닌 예외가 났다: "
                f"{type(excinfo.value).__name__}: {excinfo.value}")
        else:
            call()
            assert store.get_by_id("dup")["document"] == "둘째 값"
            assert store.count() == 1


# ---------------------------------------------------------------------------
# 엣지 — 층 2 (쓰기문 자신의 강제)
# ---------------------------------------------------------------------------


SQL_BACKENDS = ["sqlite-vec", "pg"]


@pytest.fixture(params=SQL_BACKENDS)
def sql_store(request, tmp_path):
    """SQL 두 백엔드만. chroma 는 층 2 를 가질 수 없어 이 축에서 제외한다 —
    조건부 쓰기도 트랜잭션도 없고 프로세스 간 잠금은 MCP 전용 공유 락이다."""
    s = build_vector_store(request.param, tmp_path)
    assert s.available
    s.backend_name = request.param
    yield s
    if request.param == "pg":
        try:
            from sqlalchemy import text

            with s._engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {s._table}"))
        except Exception:
            pass
    if hasattr(s, "close"):
        s.close()


class TestWriteStatementEnforcesOwnershipOnItsOwn:
    """선검사를 무력화해도 쓰기문 자신이 교차 팩 인수를 거부한다.

    이 축이 없으면 층 2 는 선검사에 가려 한 번도 실행되지 않고, 그래서
    누가 지워도 아무 테스트가 깨지지 않는다. 층 2 가 존재하는 이유는
    선검사의 SELECT 와 쓰기 사이에 다른 프로세스가 슬롯 소유자를 바꿀 수
    있기 때문이며, 그 창은 선검사만으로 닫히지 않는다.

    예외 타입을 `ValueError` 로 좁혀 건다. 두 SQL 백엔드가 층 2 에서 같은 형태를
    내므로(vec0 은 기본키 충돌을 소유자 재조회 뒤 바꿔 던진다) 백엔드별로 느슨하게
    둘 이유가 없다. `Exception` 으로 열어 두면 소유권과 무관한 크래시도 통과한다.
    """

    def test_foreign_write_still_fails_with_the_pre_check_disabled(
        self, sql_store, monkeypatch
    ):
        sql_store.upsert_texts(texts=["팩 A 원본"], metadatas=[{"pack_id": "A"}], ids=["s"])

        module = type(sql_store).__module__
        monkeypatch.setattr(
            f"{module}.reject_foreign_slot_writes", lambda *a, **k: None)

        with pytest.raises(ValueError, match="pack"):
            sql_store.upsert_texts(
                texts=["팩 B 침범"], metadatas=[{"pack_id": "B"}], ids=["s"])

        hit = sql_store.get_by_id("s")
        assert hit["metadata"]["pack_id"] == "A", "층 2 가 없어 슬롯이 넘어갔다"
        assert hit["document"] == "팩 A 원본"

    def test_the_rejected_batch_rolls_back_whole_with_the_pre_check_disabled(
        self, sql_store, monkeypatch
    ):
        """층 2 가 걸린 배치는 그 배치의 다른 건도 남기지 않는다(트랜잭션 롤백)."""
        sql_store.upsert_texts(texts=["팩 A 원본"], metadatas=[{"pack_id": "A"}], ids=["s"])

        module = type(sql_store).__module__
        monkeypatch.setattr(
            f"{module}.reject_foreign_slot_writes", lambda *a, **k: None)

        with pytest.raises(ValueError, match="pack"):
            sql_store.upsert_texts(
                texts=["B 하나", "B 침범"],
                metadatas=[{"pack_id": "B"}, {"pack_id": "B"}],
                ids=["b1", "s"],
            )
        assert sql_store.get_by_id("b1") is None, "롤백되지 않고 같은 배치의 다른 건이 남았다"
        assert sql_store.get_by_id("s")["metadata"]["pack_id"] == "A"


# ---------------------------------------------------------------------------
# 엣지 — 저장된 pack_id 가 NULL 인 행 (pgvector 전용)
# ---------------------------------------------------------------------------


class TestNullPackIdIsUnowned:
    """저장된 `pack_id` 가 `NULL` 인 행도 미소유로 읽어야 한다.

    두 층이 이 행을 다르게 읽으면 계약이 갈린다. 층 1 은 `slot_owner` 를 거쳐
    `None` 과 빈 문자열과 부재를 한 상태로 접어 통과시키는데, SQL 에서 `NULL = ''`
    은 거짓이 아니라 `NULL` 이라 소유권 술어가 NULL 로 평가돼 갱신을 막는다.
    `pack_id` 는 두 SQL 백엔드에서 NOT NULL 이 아니므로 외부에서 쓴 행에 이 값이
    들어올 수 있다.

    NULL 행을 만드는 방법이 백엔드마다 다르다. pgvector 는 보통의 컬럼이라 UPDATE
    로 만들고, sqlite-vec 은 그 컬럼이 vec0 파티션 키라 UPDATE 가 막히므로
    (`UPDATE on partition key columns are not supported yet.`) 직접 INSERT 로
    만든다. 어느 쪽이든 외부 기록이 남길 수 있는 상태를 그대로 재현한다.
    """

    def _assert_pack_can_claim(self, store) -> None:
        store.upsert_texts(texts=["이제 A 소유"], metadatas=[{"pack_id": "A"}], ids=["n1"])
        hit = store.get_by_id("n1")
        assert hit["metadata"]["pack_id"] == "A", (
            f"NULL 소유 태그 행을 인수하지 못했다: {hit['metadata'].get('pack_id')!r}")
        assert hit["document"] == "이제 A 소유"

    def test_pgvector_pack_can_claim_a_row_whose_stored_pack_id_is_null(self, tmp_path):
        from sqlalchemy import text

        store = build_vector_store("pg", tmp_path)
        assert store.available
        try:
            store.upsert_texts(texts=["미소유"], metadatas=[{"pack_id": ""}], ids=["n1"])
            with store._engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {store._table} SET pack_id = NULL WHERE node_id = 'n1'"))
                stored = conn.execute(
                    text(f"SELECT pack_id FROM {store._table} WHERE node_id = 'n1'")
                ).scalar()
            assert stored is None, f"NULL 로 만들지 못했다: {stored!r}"
            self._assert_pack_can_claim(store)
        finally:
            try:
                with store._engine.begin() as conn:
                    conn.execute(text(f"DROP TABLE IF EXISTS {store._table}"))
            except Exception:
                pass
            if hasattr(store, "close"):
                store.close()

    def test_sqlite_vec_pack_can_claim_a_row_whose_stored_pack_id_is_null(self, tmp_path):
        store = build_vector_store("sqlite-vec", tmp_path)
        assert store.available
        try:
            # 파티션 키는 UPDATE 가 막히므로 NULL 을 직접 INSERT 한다.
            vec = store._embed(["미소유"])[0]
            params = list(store._insert_params("n1", "미소유", {"pack_id": ""}, vec))
            params[1] = None
            with store._tx() as conn:
                conn.execute(store._insert_sql(), tuple(params))
                stored = conn.execute(
                    f"SELECT pack_id FROM {store._table} WHERE node_id = 'n1'"
                ).fetchone()[0]
            assert stored is None, f"NULL 로 만들지 못했다: {stored!r}"
            self._assert_pack_can_claim(store)
        finally:
            if hasattr(store, "close"):
                store.close()


class TestTheOriginalErrorSurvivesAFailedOwnerLookup:
    """소유자 재조회가 실패해도 호출자가 받는 예외는 최초 원인 그대로다.

    층 2 가 걸렸을 때 sqlite-vec 은 소유자를 다시 읽어 오류 메시지를 좋게 만든다.
    그 재조회는 **보조 수단**이므로, 그것이 실패했다고 호출자가 보는 실패의 정체가
    바뀌면 안 된다. 바뀌면 진짜 원인(디스크 오류, 스키마 문제)이 재조회 오류에
    가려진다.
    """

    def test_a_failing_owner_lookup_does_not_replace_the_insert_error(self, tmp_path):
        import sqlite3

        store = build_vector_store("sqlite-vec", tmp_path)
        assert store.available
        try:
            store.upsert_texts(texts=["팩 A 원본"], metadatas=[{"pack_id": "A"}], ids=["s"])

            # 선검사를 통과시켜 층 2 까지 내려보낸다.
            monkey = pytest.MonkeyPatch()
            monkey.setattr(
                "opencrab.stores.sqlite_vec_store.reject_foreign_slot_writes",
                lambda *a, **k: None)

            # 첫 호출(선검사 인자 구성)은 통과시키고 그 뒤 재조회만 실패시킨다.
            real = store._slot_owners
            calls = {"n": 0}

            def flaky(conn, ids):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real(conn, ids)
                raise sqlite3.OperationalError("재조회 자체가 실패했다")

            monkey.setattr(store, "_slot_owners", flaky)
            try:
                with pytest.raises(sqlite3.Error) as excinfo:
                    store.upsert_texts(
                        texts=["팩 B 침범"], metadatas=[{"pack_id": "B"}], ids=["s"])
            finally:
                monkey.undo()

            assert calls["n"] >= 2, "재조회 경로를 타지 않았다"
            assert "재조회 자체가 실패했다" not in str(excinfo.value), (
                f"재조회 오류가 최초 원인을 가렸다: {excinfo.value}")
            assert "UNIQUE" in str(excinfo.value), (
                f"최초 원인(기본키 충돌)이 아니다: {type(excinfo.value).__name__}: {excinfo.value}")
            assert excinfo.value.__cause__ is None, (
                "재조회 오류가 __cause__ 로 달렸다 — 진단을 흐린다")

            hit = store.get_by_id("s")
            assert hit["metadata"]["pack_id"] == "A", "롤백되지 않고 슬롯이 넘어갔다"
            assert hit["document"] == "팩 A 원본"
        finally:
            if hasattr(store, "close"):
                store.close()
