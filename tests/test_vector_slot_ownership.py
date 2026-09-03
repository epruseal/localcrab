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

    def test_owned_and_unowned_claims_on_one_id_are_rejected_in_either_order(self, store):
        """엣지: 미소유도 하나의 상태로 판정에 참여한다.

        한 배치가 같은 id 를 팩 A 로 한 번, 소유 없이 한 번 주장하면 거부한다.
        미소유를 판정에서 빼면 받아들임이 **레코드 순서와 백엔드에 따라 갈렸다** —
        빈 SQL 스토어에서 [미소유, A] 는 통과해 A 가 슬롯을 가져갔고 [A, 미소유] 는
        쓰기 게이트까지 가서 배치가 롤백됐으며, chroma 는 두 순서 다 자기 get 에서
        거부했다. 순서가 소유자를 정하게 두지 않는 것이 이 규칙의 목적이다.
        """
        for metas in ([{"pack_id": ""}, {"pack_id": "A"}],
                      [{"pack_id": "A"}, {"pack_id": ""}]):
            with pytest.raises(ValueError, match="different packs"):
                store.upsert_texts(
                    texts=["첫", "둘"], metadatas=metas, ids=["contested", "contested"])
            assert store.get_by_id("contested") is None, (
                f"거부된 배치가 행을 남겼다 (metas={metas})")

    def test_two_unowned_claims_on_one_id_are_not_a_conflict(self, store):
        """엣지: 미소유끼리의 중복은 같은 값이라 충돌이 아니다.

        새 규칙은 서로 다른 소유 상태만 막는다. 같은 상태의 중복은 이 이슈와
        무관하므로 백엔드별 현행 동작을 그대로 둔다(같은 팩 중복과 같은 판단).
        """
        call = lambda: store.upsert_texts(  # noqa: E731
            texts=["첫", "둘"],
            metadatas=[{"space": "s"}, {"space": "s"}],
            ids=["dup2", "dup2"],
        )
        if store.backend_name == "chroma":
            with pytest.raises(Exception) as excinfo:
                call()
            assert type(excinfo.value).__name__ == "DuplicateIDError", (
                f"소유권 게이트가 잘못 걸렸다: {type(excinfo.value).__name__}: {excinfo.value}")
        else:
            call()
            assert store.get_by_id("dup2")["document"] == "둘"

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


class TestNonePackIdIsStoredAsUnowned:
    """`pack_id` 가 `None` 인 메타는 미소유로 **저장**돼야 한다.

    `str(meta.get("pack_id", ""))` 는 키가 있고 값이 `None` 일 때 기본값을 쓰지
    않아 리터럴 `"None"` 을 만든다. 선검사는 `slot_owner` 로 그 메타를 미소유로
    읽으므로, 저장값이 `"None"` 이면 **같은 메타의 재적재가 거부된다** — 자기 팩
    재적재가 실패하는 정상 경로 파손이다. 소유 태그를 쓰는 자리도 읽는 자리와
    같은 `slot_owner` 를 거쳐야 한다.

    **파손이 관측된 것은 pgvector 한 곳이다.** sqlite-vec 은 삽입 전에
    `_sanitize_metadata` 가 `None` 을 빈 문자열로 접어 우연히 무사했다. 그래서
    저장 정규화를 되돌려도 이 클래스의 sqlite-vec 파라미터는 RED 가 되지 않는다.
    그 축을 남기는 것은 버그를 재기 위해서가 아니라 **계약을 걸기 위해서**다:
    정제 단계가 어떻게 바뀌든 저장된 소유 태그는 미소유여야 한다. 이 파일의 다른
    계약 테스트가 백엔드를 파라미터로 도는 것과 같은 방식이다.
    """

    @pytest.fixture(params=["sqlite-vec", "pg"])
    def sql_store(self, request, tmp_path):
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

    def _stored_owner(self, store):
        if store.backend_name == "pg":
            from sqlalchemy import text

            with store._engine.connect() as conn:
                return conn.execute(
                    text(f"SELECT pack_id FROM {store._table} WHERE node_id = 'n'")
                ).scalar()
        return store._conn.execute(
            f"SELECT pack_id FROM {store._table} WHERE node_id = 'n'").fetchone()[0]

    def test_a_none_pack_id_is_persisted_as_the_unowned_value(self, sql_store):
        sql_store.upsert_texts(texts=["값"], metadatas=[{"pack_id": None}], ids=["n"])
        assert self._stored_owner(sql_store) == "", (
            f"미소유가 아닌 값으로 저장됐다: {self._stored_owner(sql_store)!r}")

    def test_re_ingesting_the_same_none_metadata_is_not_rejected(self, sql_store):
        """정상 경로 파손의 실체: 같은 메타로 다시 적재하면 통과해야 한다."""
        sql_store.upsert_texts(texts=["값"], metadatas=[{"pack_id": None}], ids=["n"])
        sql_store.upsert_texts(texts=["값 v2"], metadatas=[{"pack_id": None}], ids=["n"])
        assert sql_store.get_by_id("n")["document"] == "값 v2"


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

            # 첫 호출(선검사 인자 구성)은 통과시키고 그 뒤 재조회만 실패시킨다.
            real = store._slot_owners
            calls = {"n": 0}

            def flaky(conn, ids):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real(conn, ids)
                raise sqlite3.OperationalError("재조회 자체가 실패했다")

            # `undo()` 를 첫 setattr 직후부터 감싼다. 두 setattr 사이에서 예외가
            # 나면 선검사 무력화가 같은 프로세스의 뒤 테스트로 새어 나가 그것들을
            # 거짓 green 으로 만든다.
            monkey = pytest.MonkeyPatch()
            try:
                monkey.setattr(
                    "opencrab.stores.sqlite_vec_store.reject_foreign_slot_writes",
                    lambda *a, **k: None)
                monkey.setattr(store, "_slot_owners", flaky)
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

    def test_the_propagated_exception_is_the_original_object(self, tmp_path):
        """같은 메시지의 새 예외가 아니라 **최초 예외 객체 그 자체**가 올라온다.

        메시지와 `__cause__` 만 보면 구현이 같은 문구로 새 예외를 지어도 통과한다.
        정체성까지 봐야 `raise exc from None` 이 지켜지는 것을 고정한다. 그래서
        최초 실패를 우리가 만든 표식 객체로 주입한다.
        """
        import sqlite3

        store = build_vector_store("sqlite-vec", tmp_path)
        assert store.available
        try:
            store.upsert_texts(texts=["팩 A 원본"], metadatas=[{"pack_id": "A"}], ids=["s"])

            sentinel = sqlite3.OperationalError("최초 원인 표식")

            def boom(*_a, **_k):
                raise sentinel

            # 첫 호출은 선검사가 쓰는 것이라 통과시켜야 한다. 그것까지 실패시키면
            # 층 2 의 가드에 닿기 전에 선검사 줄에서 죽어 이 테스트가 다른 것을
            # 재는 것이 된다.
            real = store._slot_owners
            calls = {"n": 0}

            def flaky(conn, ids):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real(conn, ids)
                raise sqlite3.OperationalError("재조회 자체가 실패했다")

            monkey = pytest.MonkeyPatch()
            try:
                # `_insert_params` 는 `conn.execute` 의 인자라 try 블록 안에서
                # 평가된다. 여기서 던지면 층 2 의 가드 경로를 그대로 탄다.
                monkey.setattr(store, "_insert_params", boom)
                monkey.setattr(store, "_slot_owners", flaky)
                with pytest.raises(sqlite3.Error) as excinfo:
                    store.upsert_texts(
                        texts=["팩 A 갱신"], metadatas=[{"pack_id": "A"}], ids=["s"])
            finally:
                monkey.undo()

            assert excinfo.value is sentinel, (
                "최초 예외 객체가 아니라 다른 객체가 올라왔다: "
                f"{type(excinfo.value).__name__}: {excinfo.value}")
            assert excinfo.value.__cause__ is None
            assert calls["n"] >= 2, "층 2 의 재조회 가드를 타지 않았다"
        finally:
            if hasattr(store, "close"):
                store.close()

    def test_a_non_sqlite_lookup_failure_also_leaves_the_original_error(self, tmp_path):
        """가드의 포착 범위가 드라이버 예외에 갇히지 않는다.

        재조회가 `sqlite3.Error` 밖의 예외를 던져도 최초 원인이 그대로 올라와야
        한다. 범위를 `sqlite3.Error` 로 좁히면 이 축이 RED 가 된다 — 그 좁힘이
        조용히 돌아오는 것을 막는 자리다.
        """
        import sqlite3

        store = build_vector_store("sqlite-vec", tmp_path)
        assert store.available
        try:
            store.upsert_texts(texts=["팩 A 원본"], metadatas=[{"pack_id": "A"}], ids=["s"])

            sentinel = sqlite3.OperationalError("최초 원인 표식")
            real = store._slot_owners
            calls = {"n": 0}

            def boom(*_a, **_k):
                raise sentinel

            def flaky(conn, ids):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real(conn, ids)
                raise RuntimeError("드라이버 밖 재조회 실패")

            monkey = pytest.MonkeyPatch()
            try:
                monkey.setattr(store, "_insert_params", boom)
                monkey.setattr(store, "_slot_owners", flaky)
                with pytest.raises(sqlite3.Error) as excinfo:
                    store.upsert_texts(
                        texts=["팩 A 갱신"], metadatas=[{"pack_id": "A"}], ids=["s"])
            finally:
                monkey.undo()

            assert excinfo.value is sentinel, (
                "드라이버 밖 재조회 실패가 최초 원인을 가렸다: "
                f"{type(excinfo.value).__name__}: {excinfo.value}")
            assert calls["n"] >= 2, "층 2 의 재조회 가드를 타지 않았다"
        finally:
            if hasattr(store, "close"):
                store.close()


# ---------------------------------------------------------------------------
# 엣지 — 이관 스크립트도 같은 정규화를 쓴다
# ---------------------------------------------------------------------------


class TestMigrationScriptNormalisesTheOwnershipTag:
    """chroma 에서 sqlite-vec 으로 옮기는 스크립트가 소유 태그를 정규화한다.

    그 스크립트는 소유 컬럼을 컬럼으로 복사하지 않고 **메타에서 값을 꺼내 새
    컬럼을 만든다.** 그래서 스토어와 같은 정규화를 거쳐야 한다. `_sanitize_metadata`
    가 `None` 은 접지만 `0` 과 `False` 는 그대로 두므로, `str()` 을 쓰면 그것들이
    `"0"` 과 `"False"` 로 저장된다. 읽는 쪽 `slot_owner` 는 셋 다 미소유로 접으므로
    이관 직후 그 행의 재적재가 거부된다.

    소유 컬럼을 컬럼으로 복사하는 다른 이관 스크립트 둘은 대상이 아니다. 그쪽은
    원본 보존이 목적이고 dict 정규화가 개입할 자리가 없다.
    """

    def test_falsy_ownership_tags_all_land_as_unowned(self):
        """정상: `None` 과 `0` 과 `False` 가 전부 미소유 컬럼값이 된다."""
        from opencrab.stores._vector_base import slot_owner
        from opencrab.stores.chroma_store import _sanitize_metadata

        for raw in ({"pack_id": None}, {"pack_id": 0}, {"pack_id": False}, {}):
            clean = _sanitize_metadata(dict(raw))
            assert slot_owner(clean) == "", (
                f"{raw} 가 미소유로 저장되지 않는다: {slot_owner(clean)!r}")

    def test_a_real_pack_name_survives(self):
        """정상: 실제 팩 이름은 그대로 남는다(정규화가 소유를 지우지 않는다)."""
        from opencrab.stores._vector_base import slot_owner
        from opencrab.stores.chroma_store import _sanitize_metadata

        assert slot_owner(_sanitize_metadata({"pack_id": "pack-a"})) == "pack-a"

    def test_the_script_binds_the_owner_column_through_the_shared_normaliser(self):
        """실제 삽입 표현식을 **AST 로** 대조한다.

        위 두 축은 공유 함수를 직접 부르므로 스크립트가 `str(...)` 로 돌아가도
        통과한다. 실 호출 지점을 봐야 한다. 문자열 검색은 주석에도 걸리고 같은
        뜻의 다른 표기를 놓치므로 구문 트리를 본다 — 이 저장소가
        `TestVecBackendKindsCoverage` 에서 이미 쓰는 기법이다.

        스크립트를 실제로 태우지 않는 이유: `main()` 이 인자 파싱과 실 chroma
        컬렉션 연결과 스토어 생성을 한 함수에 묶고 있어, 행 생성부만 떼려면
        이 이슈의 범위 밖 리팩터가 필요하다.
        """
        import ast
        import pathlib as _pathlib

        source = _pathlib.Path(__file__).resolve().parents[1] / (
            "scripts/migrate_chroma_to_sqlite_vec.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))

        # `rows.append((_id, <소유 태그>, ...))` 의 두 번째 원소를 찾는다.
        owner_exprs = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "append"):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "rows"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Tuple) and len(arg.elts) >= 2:
                    owner_exprs.append(arg.elts[1])

        assert owner_exprs, "이관 스크립트에서 행 생성 지점을 찾지 못했다"
        for expr in owner_exprs:
            assert isinstance(expr, ast.Call), (
                f"소유 태그 자리가 호출이 아니다: {ast.dump(expr)[:120]}")
            assert isinstance(expr.func, ast.Name) and expr.func.id == "slot_owner", (
                "소유 태그가 공유 정규화를 거치지 않는다: "
                f"{ast.unparse(expr)}")
            # 인수까지 본다. 호출 이름만 보면 `slot_owner({})` 처럼 정제한 메타를
            # 넘기지 않는 형태가 통과한다.
            assert len(expr.args) == 1 and not expr.keywords, (
                f"소유 태그 정규화의 인수 모양이 다르다: {ast.unparse(expr)}")
            assert isinstance(expr.args[0], ast.Name) and expr.args[0].id == "clean", (
                "정제한 메타가 아니라 다른 것을 정규화한다: "
                f"{ast.unparse(expr)}")


# ---------------------------------------------------------------------------
# 오류 — 거부 메시지가 남의 팩 이름을 흘리지 않는다
# ---------------------------------------------------------------------------


class TestRejectionMessagesDoNotNameTheOtherPack:
    """거부 텍스트에 소유 팩의 id 가 들어가면 안 된다.

    `opencrab/pack/write_gate.py` 의 `identity_reject_message` 가 graph 층에 세운
    불변식(localcrab#143 불변식 7)이 여기에도 그대로 걸린다. 이 텍스트는 쓰기
    영수증에 원문 그대로 실려 나간다 — `OntologyBuilder.add_node` 가 예외를 잡아
    `stores["vector"]` 에 문자열로 넣는다. 담으면 충돌하는 node_id 로 써 보는 것만
    으로 남의 팩 이름을 알아낼 수 있다.

    호출자가 되받는 것은 자기가 낸 것뿐이다: 걸린 id 와 그 개수. 자기 배치를
    고치는 데 필요한 전부다.
    """

    @pytest.fixture(params=BACKENDS)
    def store(self, request, tmp_path):
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

    def test_the_existing_owner_is_not_named(self, store):
        secret = "a-private-pack-name-4f21"
        store.upsert_texts(texts=["소유자 문서"], metadatas=[{"pack_id": secret}], ids=["s"])

        with pytest.raises(ValueError) as excinfo:
            store.upsert_texts(
                texts=["침범"], metadatas=[{"pack_id": "intruder"}], ids=["s"])

        message = str(excinfo.value)
        assert secret not in message, f"소유 팩 이름이 메시지에 샜다: {message}"
        assert "'s'" in message, f"호출자가 낸 id 가 메시지에 없다: {message}"

    @pytest.mark.parametrize("backend", ["sqlite-vec", "pg"])
    def test_the_layer_two_message_does_not_name_the_owner_either(self, backend, tmp_path):
        """선검사를 지나친 경쟁에서 나오는 메시지도 같은 규율을 지킨다.

        SQL 두 백엔드 모두 층 2 를 가지므로 둘 다 건다. 한쪽만 덮으면 다른 쪽이
        조용히 갈라진다.
        """
        store = build_vector_store(backend, tmp_path)
        assert store.available
        try:
            secret = "a-private-pack-name-9c30"
            store.upsert_texts(texts=["소유자"], metadatas=[{"pack_id": secret}], ids=["s"])

            module = type(store).__module__
            monkey = pytest.MonkeyPatch()
            try:
                monkey.setattr(f"{module}.reject_foreign_slot_writes", lambda *a, **k: None)
                with pytest.raises(ValueError) as excinfo:
                    store.upsert_texts(
                        texts=["침범"], metadatas=[{"pack_id": "intruder"}], ids=["s"])
            finally:
                monkey.undo()

            assert secret not in str(excinfo.value), (
                f"층 2 메시지에 소유 팩 이름이 샜다: {excinfo.value}")
        finally:
            if backend == "pg":
                try:
                    from sqlalchemy import text

                    with store._engine.begin() as conn:
                        conn.execute(text(f"DROP TABLE IF EXISTS {store._table}"))
                except Exception:
                    pass
            if hasattr(store, "close"):
                store.close()

    def test_both_sql_backends_word_the_layer_two_rejection_identically(self, tmp_path):
        """두 SQL 백엔드의 층 2 문구가 서로 같다.

        "소유자 이름이 없다" 만 걸면 두 백엔드가 서로 다른 말을 해도 통과한다.
        실제로 한쪽 문구만 바꾸고 다른 쪽을 두어 갈라진 적이 있다. 같은 자리에서
        같은 말을 하는 것 자체를 걸어 그 갈라짐이 다시 조용히 생기지 않게 한다.
        """
        messages = {}
        for backend in ("sqlite-vec", "pg"):
            store = build_vector_store(backend, tmp_path / backend)
            assert store.available
            try:
                store.upsert_texts(texts=["소유자"], metadatas=[{"pack_id": "owner"}], ids=["s"])
                module = type(store).__module__
                monkey = pytest.MonkeyPatch()
                try:
                    monkey.setattr(
                        f"{module}.reject_foreign_slot_writes", lambda *a, **k: None)
                    with pytest.raises(ValueError) as excinfo:
                        store.upsert_texts(
                            texts=["침범"], metadatas=[{"pack_id": "intruder"}], ids=["s"])
                finally:
                    monkey.undo()
                messages[backend] = str(excinfo.value)
            finally:
                if backend == "pg":
                    try:
                        from sqlalchemy import text

                        with store._engine.begin() as conn:
                            conn.execute(text(f"DROP TABLE IF EXISTS {store._table}"))
                    except Exception:
                        pass
                if hasattr(store, "close"):
                    store.close()

        assert messages["sqlite-vec"] == messages["pg"], (
            "두 SQL 백엔드의 층 2 문구가 갈렸다:\n"
            f"  sqlite-vec: {messages['sqlite-vec']}\n"
            f"  pg        : {messages['pg']}")

    def test_a_batch_internal_conflict_may_name_both_since_the_caller_supplied_them(
        self, store
    ):
        """대조군: 한 배치 안의 충돌은 두 이름을 적어도 된다.

        그 둘은 호출자가 방금 자기 손으로 넘긴 값이라 새로 드러나는 것이 없다.
        이 축이 있어야 위 두 단언이 "모든 메시지에서 이름을 지웠다" 가 아니라
        "저장소에서 읽은 이름만 지웠다" 를 재는 것이 된다.
        """
        with pytest.raises(ValueError) as excinfo:
            store.upsert_texts(
                texts=["A", "B"],
                metadatas=[{"pack_id": "mine-a"}, {"pack_id": "mine-b"}],
                ids=["dup3", "dup3"],
            )
        message = str(excinfo.value)
        assert "mine-a" in message and "mine-b" in message, (
            f"호출자가 낸 두 값이 메시지에 없다: {message}")
