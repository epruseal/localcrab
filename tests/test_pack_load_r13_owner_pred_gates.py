"""r13(#142 재리뷰) — doc_sources 소유 술어 조건부 폴백. `design_fix_round13_v2.md`
의 폐쇄 게이트 ⓐ~ⓖ 를 코드로 건다.

핵심 결함(1f97bb3): `build_count_sql` doc_pred·`delete_pack` src_pred·
`live_pack_state` chunk_pred 3자리가 전부 **무조건 OR**
(`pack_id == :pack OR source == :pack`)였다 — 혼합 태그 문서
(`pack_id="B", source="A"`)가 A 쪽 대사·삭제·증분 분류에 오포섭됐다.
수정: `_doc_owner_pred(dialect)` 단일 헬퍼 — `pack_id` 가 소유 정본, `source`
는 `pack_id` 가 `SqlDialect.json_truthy_text` 기준 "없음"일 때만 폴백.

- ⓐ 혼합 형태 e2e: 3자리 동일 집합(A delete→B 보존, B delete→삭제,
  live_pack_state(A) 미분류, COUNT(A) 미계수·COUNT(B) 계수).
- ⓑ falsy pack_id(부재·null·""·false·0) → 폴백 매치(고아 0).
- ⓒ non-falsy 비문자열 pack_id → 비소유(source 일치해도) + `_named_to_qmark`
  lookbehind(`::numeric` 비토큰) + 파생 export 리터럴/ARGC 불변.
- ⓓ 소비자 qmark+tuple 호환(r11 게이트 ⑨ 재확인 + 명시 재현).
- ⓕ PG형 fake 위반 0(신규 술어).
- ⓖ 변형(무조건 OR 복원) → ⓐ red(사본에서 회귀 검출력 증명).

ⓔ(전체 스위트 보존·r11/r12 green·128팩 diff)는 이 파일 밖 — 리드 보고 참고.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import create_pack
from opencrab.stores._sql_dialect import POSTGRES, SQLITE
from tests.test_pack_load import (  # noqa: F401 — 실 스토어 픽스처 재사용
    _LIVE_TEST_USER,
    _NoVec,
    live,
    pack_sql,
)
from tests.test_pack_load_r11_pg_gates import _NoVecFake, _pg_fakes


def _own(pack_sql, *pack_ids):
    """`pack_ids` 를 `_LIVE_TEST_USER` 소유로 등록하고 그 principal 을 돌려준다(#434)."""
    for pack_id in pack_ids:
        create_pack(pack_sql, _LIVE_TEST_USER, pack_id)
    return Principal(user_id=_LIVE_TEST_USER, is_local=True, disabled=False)


# ───────────────────────── 게이트 ⓐ: 혼합 형태 e2e ─────────────────────────

class TestMixedFormOwnershipE2E:
    """`pack_id="B", source="A"` 혼합 태그 문서 — A 삭제/분류/대사 어디서도
    A 소유로 안 잡히고, B 삭제/분류/대사에서는 정상적으로 B 소유로 잡힌다.
    실스토어(sqlite) 왕복이라 세 함수가 실제로 부르는 SQL 을 그대로 태운다.
    """

    def _seed(self, docs):
        docs.upsert_source("mixed", "혼합 태그 본문", {"pack_id": "B", "source": "A"})
        docs.upsert_source("b-only", "B 전용 본문", {"pack_id": "B"})
        docs.upsert_source("a-legacy", "A 레거시(source 만) 본문", {"source": "A"})

    def test_delete_pack_a_spares_the_mixed_and_b_only_docs(self, live, tmp_path, pack_sql):
        _builder, graph, docs = live
        self._seed(docs)

        with principal_scope(_own(pack_sql, "A")):
            pack_load.delete_pack("A", graph, docs, _NoVec(), sql=pack_sql)

        left = {r[0] for r in docs._conn.execute("SELECT source_id FROM doc_sources")}
        assert "mixed" in left, "혼합 태그 문서(pack_id=B)가 A delete_pack 에 오삭제됐다 — P1 재현"
        assert "b-only" in left, "무관한 B 전용 문서가 A delete_pack 에 지워졌다"
        assert "a-legacy" not in left, "레거시 source-only A 문서가 폴백 매치로 안 지워졌다(회귀)"

    def test_delete_pack_b_removes_the_mixed_doc(self, live, tmp_path, pack_sql):
        _builder, graph, docs = live
        self._seed(docs)

        with principal_scope(_own(pack_sql, "B")):
            pack_load.delete_pack("B", graph, docs, _NoVec(), sql=pack_sql)

        left = {r[0] for r in docs._conn.execute("SELECT source_id FROM doc_sources")}
        assert "mixed" not in left, "혼합 태그 문서(pack_id=B)가 B delete_pack 에서 안 지워졌다"
        assert "b-only" not in left
        assert "a-legacy" in left, "무관한 A 레거시 문서가 B delete_pack 에 지워졌다"

    def test_live_pack_state_a_does_not_classify_the_mixed_doc(self, live, tmp_path):
        _builder, graph, docs = live
        self._seed(docs)

        state_a = pack_load.live_pack_state("A", graph, docs, _NoVec())
        assert "mixed" not in state_a["chunks"], (
            "혼합 태그 문서가 A 의 live_pack_state 에 잡혔다 — finalize 삭제 후보로 오분류될 수 있다")
        assert "a-legacy" in state_a["chunks"], "레거시 source-only A 문서가 A 분류에서 빠졌다(회귀)"

        state_b = pack_load.live_pack_state("B", graph, docs, _NoVec())
        assert "mixed" in state_b["chunks"], "혼합 태그 문서가 B 의 live_pack_state 에 안 잡혔다"
        assert "b-only" in state_b["chunks"]

    def test_count_a_excludes_mixed_count_b_includes(self, live, tmp_path):
        _builder, graph, docs = live
        self._seed(docs)

        counts_a = pack_load.pack_live_counts("A", graph, docs, _NoVec())
        counts_b = pack_load.pack_live_counts("B", graph, docs, _NoVec())

        # A: a-legacy 만 소유(1건) — mixed 는 오계수되면 안 된다.
        assert counts_a["docs"] == 1, f"A 대사 카운트가 혼합 문서를 오계수했다: {counts_a}"
        # B: mixed + b-only 2건.
        assert counts_b["docs"] == 2, f"B 대사 카운트가 안 맞는다: {counts_b}"

    def test_three_sites_agree_on_the_same_ownership_set(self, live, tmp_path):
        """3자리(대사·삭제·증분 분류)가 **같은 집합**을 본다 — 대사 카운트로
        본 소유 건수와 live_pack_state 로 본 소유 건수가 갈리면 대사 자체가
        신뢰 불가가 된다(그 축이 원래 존재하는 이유)."""
        _builder, graph, docs = live
        self._seed(docs)

        for pack in ("A", "B"):
            count = pack_load.pack_live_counts(pack, graph, docs, _NoVec())["docs"]
            state_n = len(pack_load.live_pack_state(pack, graph, docs, _NoVec())["chunks"])
            assert count == state_n, f"{pack}: COUNT={count} vs live_pack_state={state_n} 불일치"


# ─────────────────── 게이트 ⓑ·ⓒ: 소유 판정 진리표(직접 SQL) ───────────────────

class TestOwnerPredTruthTable:
    """`_doc_owner_pred` 를 sqlite 인메모리 위에서 직접 실행해 falsy/non-falsy
    비문자열 pack_id 진리표를 확정한다 — e2e 보다 케이스가 세밀해(JSON null·
    false·0·object·array) 실스토어 upsert 경로보다 여기가 낫다."""

    def _match(self, dialect, metadata: dict, target: str = "P") -> bool:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE doc_sources (source_id TEXT, metadata TEXT)")
        conn.execute("INSERT INTO doc_sources VALUES(?,?)", ("c1", json.dumps(metadata)))
        conn.commit()
        pred = pack_load._doc_owner_pred(dialect)
        # PG 방언 산출물은 sqlite 로 직접 실행 불가(jsonb_typeof/->>/::numeric) —
        # 이 클래스는 술어의 **행 판정 로직**만 sqlite 방언으로 확인한다. PG
        # 방언 자체의 실행 가능성·번역은 게이트 ⓕ(TestPgShapedFakeStores 계열)
        # 가 따로 건다.
        got = {r[0] for r in conn.execute(f"SELECT source_id FROM doc_sources WHERE {pred}",
                                           {"pack": target})}
        return "c1" in got

    # ── ⓑ falsy pack_id → 폴백 매치(정본상 "없음") ──
    @pytest.mark.parametrize("metadata", [
        {"source": "P"},                  # pack_id 부재
        {"pack_id": None, "source": "P"},  # JSON null
        {"pack_id": "", "source": "P"},   # falsy 빈 문자열
        {"pack_id": False, "source": "P"},  # falsy false
        {"pack_id": 0, "source": "P"},    # falsy 0
        {"pack_id": 0.0, "source": "P"},  # falsy 0.0
    ], ids=["absent", "null", "empty-str", "false", "zero-int", "zero-real"])
    def test_falsy_pack_id_falls_back_to_source_match(self, metadata):
        assert self._match(SQLITE, metadata) is True, (
            f"falsy pack_id 인데 source 폴백이 매치 안 했다(고아화): {metadata}")

    def test_pack_id_only_still_matches(self):
        assert self._match(SQLITE, {"pack_id": "P"}) is True

    def test_source_and_different_pack_id_does_not_match(self):
        """pack_id 가 **다른** 값으로 존재하면(non-falsy 문자열) source 가
        일치해도 폴백이 안 걸린다 — 이것이 바로 P1 결함의 반증 케이스다."""
        assert self._match(SQLITE, {"pack_id": "Q", "source": "P"}) is False

    def test_unrelated_tag_does_not_match(self):
        assert self._match(SQLITE, {"pack_id": "Q", "tag": "P"}) is False

    # ── ⓒ non-falsy 비문자열 pack_id → 비소유(source 일치해도) ──
    @pytest.mark.parametrize("metadata", [
        {"pack_id": 42, "source": "P"},
        {"pack_id": True, "source": "P"},
        {"pack_id": {"a": "P"}, "source": "P"},
        {"pack_id": ["P"], "source": "P"},
    ], ids=["int", "bool", "object", "array"])
    def test_non_falsy_non_string_pack_id_is_never_owned_even_with_matching_source(
            self, metadata):
        assert self._match(SQLITE, metadata) is False, (
            f"non-falsy 비문자열 pack_id 인데 source 폴백이 걸렸다(정의된 비지원 위반): {metadata}")


# ─────────────────── 게이트 ⓒ(lookbehind)·ⓓ: `_named_to_qmark` ───────────────────

class TestNamedToQmarkCastLookbehind:
    """`(?<!:)` lookbehind — PG `::numeric` 캐스트의 두 번째 `:` 가 named 토큰
    으로 오인돼 파라미터로 치환되면 파생 SQL 이 깨진다(실사거리: `_doc_owner_pred`
    의 PG 산출물이 `json_truthy_text` 를 통해 `::numeric` 을 처음 낸다)."""

    def test_double_colon_cast_is_not_treated_as_a_token(self):
        sql = "SELECT * FROM t WHERE (x->'k')::numeric = 0 AND y = :pack"
        out, argc = pack_load._named_to_qmark(sql)
        assert out == "SELECT * FROM t WHERE (x->'k')::numeric = 0 AND y = ?", out
        assert argc == 1, f"::numeric 의 두 번째 ':' 가 토큰으로 잘못 세어졌다: argc={argc}"

    def test_named_token_still_substitutes_normally(self):
        out, argc = pack_load._named_to_qmark("WHERE a = :pack OR b = :pack")
        assert out == "WHERE a = ? OR b = ?"
        assert argc == 2

    def test_actual_pg_doc_owner_pred_survives_derivation_without_corrupting_the_cast(self):
        """설계 확증 재현: `_doc_owner_pred(POSTGRES)` 산출물을 실제로 파생에
        태워 `::numeric` 이 살아남고 ARGC 가 2(:pack 2 회) 그대로인지 확인한다."""
        pred = pack_load._doc_owner_pred(POSTGRES)
        assert "::numeric" in pred, "전제가 깨졌다 — PG 산출물에 ::numeric 이 없다"
        out, argc = pack_load._named_to_qmark(pred)
        assert "::numeric" in out, f"파생 후 캐스트가 사라졌다(오치환): {out!r}"
        assert argc == 2, f"ARGC 가 2 여야 한다(:pack 2 회 출현): {argc}"

    def test_literal_parsing_guard_unaffected(self):
        """lookbehind 추가가 리터럴 가드(게이트 ⑨ 계열)를 안 깬다 — 여전히
        문자열 리터럴 안의 `:token` 은 잡아낸다."""
        with pytest.raises(AssertionError):
            pack_load._named_to_qmark("SELECT * FROM t WHERE x = ':not_a_param'")


class TestDerivedExportInvariants:
    """ⓒ 파생 산출물 불변: nodes/edges/anchor 는 이번 변경 밖(docs 만 바뀐다) +
    ARGC {'nodes':1,'edges':1,'docs':2} 불변 + 리터럴 가드 통과."""

    def test_argc_shape_unchanged(self):
        assert pack_load.COUNT_SQL_ARGC == {"nodes": 1, "edges": 1, "docs": 2}, (
            pack_load.COUNT_SQL_ARGC)

    def test_nodes_and_edges_sql_untouched_by_docs_change(self):
        """`_json_str_eq` 기반 node/edge 술어는 `_doc_owner_pred` 도입과 무관
        하다 — 소유권 판정은 여전히 `pack_id` 단순 등가 하나뿐이고 docs 축의
        OR 폴백은 새어 들어오지 않는다.

        `CASE`는 `#415`(손상 JSON 행 가드) 이후 두 술어 모두에 있다 — 이건
        r13이 막던 것과 다른 성질(malformed-JSON 방어 전위조건절)이라 여기서
        금지하지 않는다. 대신 등가절 리터럴이 `idx_nodes_pack`의 인덱스
        표현식과 바이트 단위로 같은 채인지를 확인해 소유권 판정 자체는
        여전히 단순 등가뿐임을 고정한다. 인덱스 탐색이 실제로 보존되는지는
        `test_nodes_sql_still_uses_the_pack_id_index_after_the_json_safety_guard`
        가 `EXPLAIN QUERY PLAN` 실측으로 별도 고정한다."""
        sqls = pack_load.build_count_sql(SQLITE)
        assert "OR" not in sqls["nodes"], sqls["nodes"]
        assert "OR" not in sqls["edges"], sqls["edges"]
        assert "json_extract(properties, '$.pack_id') = :pack" in sqls["nodes"], sqls["nodes"]
        assert "json_extract(properties, '$.pack_id') = :pack" in sqls["edges"], sqls["edges"]

    def test_nodes_sql_still_uses_the_pack_id_index_after_the_json_safety_guard(self, live):
        """#415의 CASE 가드가 `idx_nodes_pack` 인덱스 탐색을 깨지 않았는지
        진짜 스토어(운영 스키마 그대로, 손으로 베낀 DDL 아님)로
        `EXPLAIN QUERY PLAN` 실측 고정. `graph_edges`에는 애초에 `pack_id`
        인덱스가 없어(전수 확인, `GRAPH_STORE_SCHEMA`) edges 쪽은 대응하는
        고정 시험이 없다 — 늘 `SCAN`이라 고정할 성질 자체가 없다."""
        _builder, graph, _docs = live
        sqls = pack_load.build_count_sql(SQLITE)
        plan = graph._conn.execute(
            f"EXPLAIN QUERY PLAN {sqls['nodes']}", {"pack": "own-pack"}
        ).fetchall()
        plan_text = " ".join(str(cell) for row in plan for cell in row)
        # `SEARCH ... USING INDEX idx_nodes_pack`를 요구한다 — 이 인덱스를
        # 커버링 스캔(`SCAN ... USING INDEX`)하는 계획도 "idx_nodes_pack"
        # 문자열은 포함하지만 그것은 탐색이 아니다. 지키려는 성질은 탐색이
        # 유지되는 것이다.
        assert "SEARCH graph_nodes USING INDEX idx_nodes_pack" in plan_text, plan_text

    def test_anchor_sql_untouched(self):
        assert pack_load.ANCHOR_SQL == pack_load.build_anchor_sql(SQLITE)
        assert "pack_id" not in pack_load.ANCHOR_SQL

    def test_docs_sql_now_uses_the_conditional_owner_predicate(self):
        sqls = pack_load.build_count_sql(SQLITE)
        assert sqls["docs"] == (
            "SELECT COUNT(*) AS n FROM doc_sources WHERE "
            + pack_load._doc_owner_pred(SQLITE))
        assert "CASE" in sqls["docs"], "조건부 폴백(json_truthy_text CASE)이 안 보인다"

    def test_literal_parsing_guard_passes_the_real_generated_predicates(self):
        for dialect in (SQLITE, POSTGRES):
            pack_load._assert_no_named_token_in_string_literals(
                pack_load._doc_owner_pred(dialect))


# ───────────────────────── 게이트 ⓓ: 소비자 qmark+tuple 호환 ─────────────────────────

class TestLegacyQmarkConsumerCompat:
    """레거시 `COUNT_SQL`/`COUNT_SQL_ARGC` export — qmark+tuple 호출 관례를
    쓰는 외부 소비자(rename_pack_ids.py·run_ingest_workflow, dump 트리 소유·
    이번 범위 밖)가 계속 동작하는지 이 리포 안에서 확인 가능한 만큼 확인한다:
    named 산출물과 legacy qmark 산출물이 실행 결과까지 일치해야 그 소비자들이
    안전하다."""

    def test_docs_qmark_and_named_agree_on_real_mixed_data(self, live, tmp_path):
        _builder, _graph, docs = live
        docs.upsert_source("mixed", "본문", {"pack_id": "B", "source": "A"})
        docs.upsert_source("b-only", "본문2", {"pack_id": "B"})
        docs.upsert_source("a-legacy", "본문3", {"source": "A"})

        named_sql = pack_load.build_count_sql(SQLITE)["docs"]
        named_a = docs._conn.execute(named_sql, {"pack": "A"}).fetchone()[0]
        named_b = docs._conn.execute(named_sql, {"pack": "B"}).fetchone()[0]

        params_a = ("A",) * pack_load.COUNT_SQL_ARGC["docs"]
        params_b = ("B",) * pack_load.COUNT_SQL_ARGC["docs"]
        legacy_a = docs._conn.execute(pack_load.COUNT_SQL["docs"], params_a).fetchone()[0]
        legacy_b = docs._conn.execute(pack_load.COUNT_SQL["docs"], params_b).fetchone()[0]

        assert (named_a, named_b) == (1, 2), (named_a, named_b)  # A: a-legacy만, B: mixed+b-only
        assert (legacy_a, legacy_b) == (named_a, named_b), (
            "legacy qmark export 가 named 산출물과 다른 행 집합을 센다 — "
            "qmark+tuple 소비자가 대사에서 named 와 어긋난 값을 받는다")

    def test_qmark_sql_has_no_named_tokens_left(self):
        assert ":" not in pack_load.COUNT_SQL["docs"].replace("::", ""), (
            "레거시 export 에 named 토큰이 남아있다(파생이 덜 됐다): "
            + pack_load.COUNT_SQL["docs"])


# ───────────────────────── 게이트 ⓕ: PG형 fake 위반 0 ─────────────────────────

class TestPgFakeNewPredicateNoViolation:
    """`_doc_owner_pred(POSTGRES)` 가 `_validate_pg_shape` 를 통과하고(qmark·
    sqlite 전용 함수·bare 테이블명 없음) 실제로 PG형 fake 위에서 올바른 결과를
    낸다 — r11 `TestPgShapedFakeStores` 의 4 시나리오(이미 green, 별도 실행
    확인 완료)에 더해 여기서는 **혼합 형태**를 PG fake 로 재현한다."""

    def test_mixed_form_on_pg_fake_delete_pack(self, pack_sql):
        graph, docs = _pg_fakes()
        graph.seed_node("Document", "n1", "A")
        docs.seed_source("mixed", "본문", pack_id="B", source="A")
        docs.seed_source("b-only", "본문2", pack_id="B")

        with principal_scope(_own(pack_sql, "A")):
            node_del, chunk_sql_del, _ = pack_load.delete_pack(
                "A", graph, docs, _NoVecFake(), sql=pack_sql)

        assert node_del == 1
        assert chunk_sql_del == 0, "PG fake 에서도 혼합 문서가 A delete_pack 에 오삭제됐다"
        left = {r["source_id"] for r in docs._raw.execute("SELECT source_id FROM doc_sources")}
        assert left == {"mixed", "b-only"}

    def test_mixed_form_on_pg_fake_pack_live_counts(self):
        graph, docs = _pg_fakes()
        docs.seed_source("mixed", "본문", pack_id="B", source="A")
        docs.seed_source("a-legacy", "본문2", source="A")

        got_a = pack_load.pack_live_counts("A", graph, docs, _NoVecFake())
        got_b = pack_load.pack_live_counts("B", graph, docs, _NoVecFake())
        assert got_a["docs"] == 1, got_a  # a-legacy 만
        assert got_b["docs"] == 1, got_b  # mixed 만


# ───────────────────────── 게이트 ⓖ: 변형 → red ─────────────────────────

class TestMutantUnconditionalOrIsCaughtByGateA:
    """변형(무조건 OR 로 되돌린 `_doc_owner_pred`) 을 사본에서 주입하면 게이트
    ⓐ 의 핵심 단언이 실패해야 한다 — 이 스위트가 실제로 그 결함 클래스를
    검출한다는 증명(회귀 검출력)."""

    def test_reverting_to_unconditional_or_reproduces_the_p1_bug(self, live, tmp_path,
                                                                    monkeypatch, pack_sql):
        _builder, graph, docs = live
        docs.upsert_source("mixed", "혼합 태그 본문", {"pack_id": "B", "source": "A"})

        def _unconditional_or(dialect):
            pack = pack_load._json_str_eq(dialect, "metadata", "pack_id", "pack")
            source = pack_load._json_str_eq(dialect, "metadata", "source", "pack")
            return f"{pack} OR {source}"

        monkeypatch.setattr(pack_load, "_doc_owner_pred", _unconditional_or)

        with principal_scope(_own(pack_sql, "A")):
            pack_load.delete_pack("A", graph, docs, _NoVec(), sql=pack_sql)

        left = {r[0] for r in docs._conn.execute("SELECT source_id FROM doc_sources")}
        assert "mixed" not in left, (
            "변형이 예상대로 결함을 재현하지 않았다(정변형·게이트 무력) — "
            "무조건 OR 에서는 혼합 문서가 지워져야 이 테스트가 유의미하다")
