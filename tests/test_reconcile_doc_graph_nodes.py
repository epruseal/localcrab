"""scripts/reconcile_doc_graph_nodes.py (#317).

PR #316(#55)이 ontology_get_node/ontology_list_nodes를 그래프 스토어 하나로만
읽게 고정한 뒤, 문서 스토어에만 있는 노드는 어떤 조회 표면에도 보이지 않게
됐다. 이 모듈은 두 스토어의 노드 키 집합을 진단하고 결정된 방향으로만
치유하는 도구를 검증한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ is not a package (tests/test_repair_pgvector_legacy_none_owner.py's
# identical pattern) -- import it directly off sys.path instead.
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import reconcile_doc_graph_nodes as recon  # noqa: E402

from opencrab.common.graph_identity import (  # noqa: E402
    FrozenDict,
    GraphReadCapabilityUnavailable,
    LegacyNodeKey,
    LegacyNodeRow,
    PropertyNormalizationIssue,
)

# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_store(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    return LocalGraphStore(str(tmp_path / "graph.db"))


@pytest.fixture
def doc_store(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "doc.db"))


class _FakeInventory:
    def __init__(self, schema_state: str) -> None:
        self.schema_state = schema_state
        self.nodes = ()


class _FakeGraphStore:
    """diagnose()/heal() 가 실제로 부르는 메서드만 흉내 내는 가짜.

    schema_state 는 실제 스토어의 내부 마이그레이션 상태 기계에 의존하지
    않고 직접 지정한다 -- 그 상태 기계를 재현하는 것은 이 이슈의 책임
    범위가 아니다(설계 §3/§9, 마이그레이션 자체는 별도 인프라).
    """

    def __init__(self, schema_state: str = "target") -> None:
        self._schema_state = schema_state
        self.upsert_calls: list[tuple] = []

    def inspect_graph_identity(self):
        return _FakeInventory(self._schema_state)

    def graph_schema_state(self):
        return self._schema_state

    def iter_graph_node_identities(self, batch_size: int = 5000):
        return iter(())

    def upsert_node(self, node_type, node_id, properties, space_id):
        self.upsert_calls.append((node_type, node_id, properties, space_id))


class _RejectingGraphStore:
    def inspect_graph_identity(self):
        raise GraphReadCapabilityUnavailable("Neo4j/Kuzu 는 이 진단을 지원하지 않는다")

    def graph_schema_state(self):
        raise GraphReadCapabilityUnavailable("Neo4j/Kuzu 는 이 진단을 지원하지 않는다")


# ---------------------------------------------------------------------------
# 진단: 분류 정확성 (3절)
# ---------------------------------------------------------------------------


class TestDiagnoseClassification:
    def test_control_group_both_present_type_match_is_not_reported(self, graph_store, doc_store):
        graph_store.upsert_node("Concept", "n-both", {"label": "both"}, "space-a")
        doc_store.upsert_node_doc("space-a", "Concept", "n-both", {"label": "both"})

        report = recon.diagnose(graph_store, doc_store)

        assert report.doc_only == []
        assert report.graph_only == []
        assert report.conflicts == []

    def test_graph_only_node_reported_exhaustively(self, graph_store, doc_store):
        graph_store.upsert_node("Concept", "n1", {"a": 1}, "space-a")
        graph_store.upsert_node("Concept", "n2", {"b": 2}, "space-a")

        report = recon.diagnose(graph_store, doc_store)

        reported_ids = {row.node_id for row in report.graph_only}
        assert reported_ids == {"n1", "n2"}

    def test_doc_only_node_reported_and_never_confused_with_graph_only(self, graph_store, doc_store):
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        graph_store.upsert_node("Concept", "g1", {"y": 1}, "space-a")

        report = recon.diagnose(graph_store, doc_store)

        assert [row.node_id for row in report.doc_only] == ["d1"]
        assert [row.node_id for row in report.graph_only] == ["g1"]

    def test_both_present_type_mismatch_is_identity_conflict(self, graph_store, doc_store):
        graph_store.upsert_node("Concept", "n1", {"a": 1}, "space-a")
        doc_store.upsert_node_doc("space-a", "OtherType", "n1", {"a": 1})

        report = recon.diagnose(graph_store, doc_store)

        assert len(report.conflicts) == 1
        assert report.conflicts[0].node_id == "n1"
        assert report.conflicts[0].reason == "node_type_mismatch"
        assert report.doc_only == []
        assert report.graph_only == []

    def test_zero_duplicates_reports_explicit_zero(self, graph_store, doc_store):
        graph_store.upsert_node("Concept", "n1", {"a": 1}, "space-a")
        doc_store.upsert_node_doc("space-a", "Concept", "n1", {"a": 1})

        report = recon.diagnose(graph_store, doc_store)

        assert report.duplicate_space_count == 0
        assert "0건" in recon.format_report(report).split("\n")[5]

    def test_duplicate_space_doc_only_no_graph_match_is_unhealable_and_counted(self, graph_store, doc_store):
        doc_store.upsert_node_doc("space-a", "Concept", "dup1", {"x": 1})
        doc_store.upsert_node_doc("space-b", "Concept", "dup1", {"x": 2})

        report = recon.diagnose(graph_store, doc_store)

        assert report.duplicate_space_count == 1
        assert len(report.doc_only) == 1
        assert report.doc_only[0].healable is False
        assert report.doc_only[0].reason == recon.REASON_DUP_SPACE_NO_GRAPH

    def test_duplicate_space_with_graph_match_is_identity_conflict_and_counted(self, graph_store, doc_store):
        graph_store.upsert_node("Concept", "dup2", {"x": 1}, "space-a")
        doc_store.upsert_node_doc("space-a", "Concept", "dup2", {"x": 1})
        doc_store.upsert_node_doc("space-b", "Concept", "dup2", {"x": 1})

        report = recon.diagnose(graph_store, doc_store)

        assert report.duplicate_space_count == 1
        assert len(report.conflicts) == 1
        assert report.conflicts[0].reason == "space_mismatch"

    def test_diagnose_time_non_dict_properties_is_rejected_not_healable_true(self, graph_store, doc_store):
        """대체 리뷰(BLOCKING) 지적: ``_classify_doc_only`` 가
        ``get_node_doc()`` 을 거치면 ``_row_to_node()``/``_as_dict()`` 가
        비딕셔너리 값을 조용히 ``{}`` 로 치환한다. 그 빈 딕셔너리는
        ``prepare_node()`` 를 그대로 통과해 진단이 ``healable=True`` 를
        잘못 보고한다. ``get_node_docs_by_id()`` (적용 시점 재확인이 이미
        쓰는 헬퍼, #402) 로 바꾸면 원본 리스트 값을 그대로 ``prepare_node()``
        에 넘겨 정상적으로 거부돼야 한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        doc_store._exec_write(
            f"UPDATE {doc_store._table('doc_nodes')} SET properties=:properties"
            " WHERE space=:space AND node_id=:node_id",
            {"properties": "[1, 2]", "space": "space-a", "node_id": "d1"},
        )

        report = recon.diagnose(graph_store, doc_store)

        assert len(report.doc_only) == 1
        row = report.doc_only[0]
        assert row.healable is False
        assert row.reason.startswith(f"{recon.REASON_PROMOTION_REJECTED}:")

    def test_diagnose_time_duplicate_json_keys_do_not_silently_collapse_to_last_value(
        self, graph_store, doc_store
    ):
        """대체 리뷰(BLOCKING) 지적: SQLite ``properties`` 컬럼이 중복 키를
        가진 JSON 문자열로 오염돼 있으면(정상 배관을 거치지 않은 쓰기),
        ``json.loads()`` 기본 동작이 중복 키를 조용히 마지막 값으로
        덮어써 실제 저장값과 다른 딕셔너리를 진단에 넘긴다. 그래프 쪽
        ``parse_properties_object()`` 와 같은 원칙으로, 중복 키를 감지하면
        원본 raw 문자열을 그대로 반환해 ``prepare_node()`` 의 비딕셔너리
        거부에 판정을 맡겨야 한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        doc_store._exec_write(
            f"UPDATE {doc_store._table('doc_nodes')} SET properties=:properties"
            " WHERE space=:space AND node_id=:node_id",
            {"properties": '{"label": "wrong", "x": 1, "label": "right"}', "space": "space-a", "node_id": "d1"},
        )

        report = recon.diagnose(graph_store, doc_store)

        assert len(report.doc_only) == 1
        row = report.doc_only[0]
        assert row.healable is False
        assert row.reason.startswith(f"{recon.REASON_PROMOTION_REJECTED}:")


# ---------------------------------------------------------------------------
# 그래프 전용 역채움 (4-1절, 쟁점2 thaw_json)
# ---------------------------------------------------------------------------


class TestGraphOnlyBackfill:
    def test_backfill_preserves_pack_id_and_thaws_nested_properties(self, graph_store, doc_store):
        graph_store.upsert_node(
            "Concept",
            "g1",
            {"label": "g-only", "pack_id": "pack-x", "nested": {"inner": [1, 2, 3]}},
            "space-a",
        )

        report = recon.diagnose(graph_store, doc_store)
        results = recon.heal(graph_store, doc_store, report, promote_doc_only=False)

        assert len(results) == 1
        assert results[0].outcome == "healed"
        doc = doc_store.get_node_doc("space-a", "g1")
        assert doc is not None
        assert doc["properties"]["pack_id"] == "pack-x"
        # 쟁점2: 중첩 값이 FrozenDict/tuple 이 아니라 평범한 dict/list 여야
        # 한다 -- dict() 얕은 변환이면 최상위만 dict 가 되고 내부는 얼어
        # 있다.
        assert isinstance(doc["properties"]["nested"], dict)
        assert isinstance(doc["properties"]["nested"]["inner"], list)
        assert doc["properties"]["nested"]["inner"] == [1, 2, 3]

    def test_graph_only_with_space_id_none_is_unhealable(self, graph_store, doc_store):
        graph_store.upsert_node("Concept", "g-nospace", {"a": 1}, None)

        report = recon.diagnose(graph_store, doc_store)

        assert len(report.graph_only) == 1
        row = report.graph_only[0]
        assert row.healable is False
        assert row.reason == recon.REASON_SPACE_ID_NONE
        results = recon.heal(graph_store, doc_store, report, promote_doc_only=False)
        assert results == []
        assert doc_store.get_node_doc("space-a", "g-nospace") is None

    def test_backfill_does_not_overwrite_concurrently_created_doc_row(self, graph_store, doc_store):
        """5-2절: 진단과 적용 사이에 다른 프로세스가 이미 문서 행을 만들었으면
        그 값을 잃지 않아야 한다 (create_node_doc_if_absent, DO NOTHING)."""
        graph_store.upsert_node("Concept", "g1", {"label": "from-graph"}, "space-a")
        report = recon.diagnose(graph_store, doc_store)

        # 진단 이후, 적용 이전에 동시 쓰기가 doc 행을 이미 만들었다고 가정.
        doc_store.upsert_node_doc("space-a", "Concept", "g1", {"label": "CONCURRENT_WRITE"})

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=False)

        assert len(results) == 1
        assert results[0].outcome == "skipped_exists"
        doc = doc_store.get_node_doc("space-a", "g1")
        assert doc["properties"]["label"] == "CONCURRENT_WRITE"

    def test_backfill_skips_when_source_vanished_at_apply(self, graph_store, doc_store):
        """#404: _backfill_one() 이 apply 직전 get_node_identity_by_id() 로
        다시 조회한다 -- 진단 이후 그래프 쪽에서 삭제됐으면 되살리지 않고
        건너뛴다."""
        graph_store.upsert_node("Concept", "g1", {"label": "x"}, "space-a")
        report = recon.diagnose(graph_store, doc_store)

        graph_store.delete_node("Concept", "g1")

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=False)

        assert len(results) == 1
        assert results[0].outcome == "skipped_rejected"
        assert results[0].reason == recon.REASON_BACKFILL_VANISHED_AT_APPLY
        assert doc_store.get_node_doc("space-a", "g1") is None

    def test_backfill_skips_when_apply_time_refetch_finds_property_error(self, graph_store, doc_store):
        """진단 시점에는 정상이던 노드의 properties 가 apply 시점 재조회에서
        malformed 로 나오면(동시 오염), 진단 시점 스냅샷을 신뢰하지 않고
        건너뛴다."""
        graph_store.upsert_node("Concept", "g1", {"label": "x"}, "space-a")
        report = recon.diagnose(graph_store, doc_store)

        # idx_nodes_pack 은 json_extract() 표현식 인덱스라 malformed 텍스트로
        # 갱신하려면 먼저 인덱스를 지워야 한다(test_sql_graph_base.py 의
        # 같은 우회와 동일한 이유).
        graph_store._conn.execute("DROP INDEX idx_nodes_pack")
        graph_store._conn.execute(
            "UPDATE graph_nodes SET properties = 'not json' WHERE node_id = 'g1'"
        )
        graph_store._conn.commit()

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=False)

        assert len(results) == 1
        assert results[0].outcome == "skipped_rejected"
        assert results[0].reason == recon.REASON_BACKFILL_REJECTED_AT_APPLY
        assert doc_store.get_node_doc("space-a", "g1") is None

    def test_backfill_skips_when_identity_changed_at_apply(self, graph_store, doc_store):
        """진단 이후 같은 node_id 가 삭제되고 다른 space/type 으로
        재생성됐으면, node_id 재조회가 "같은 노드"를 가리킨다고 가정하지
        않고 건너뛴다 (PK 유일성과 시간에 걸친 동일성의 혼동 방지)."""
        graph_store.upsert_node("Concept", "g1", {"label": "x"}, "space-a")
        report = recon.diagnose(graph_store, doc_store)

        graph_store.delete_node("Concept", "g1")
        graph_store.upsert_node("Document", "g1", {"label": "y"}, "space-b")

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=False)

        assert len(results) == 1
        assert results[0].outcome == "skipped_rejected"
        assert results[0].reason == recon.REASON_BACKFILL_IDENTITY_CHANGED_AT_APPLY
        # additive-only 보장: 재생성된 노드의 문서도 만들어지지 않아야 한다.
        assert doc_store.get_node_doc("space-a", "g1") is None
        assert doc_store.get_node_doc("space-b", "g1") is None


# ---------------------------------------------------------------------------
# _strip_heavy_fields (3절, #404)
# ---------------------------------------------------------------------------


class TestStripHeavyFields:
    def _full_row(self, **overrides: Any) -> LegacyNodeRow:
        defaults: dict[str, Any] = dict(
            key=LegacyNodeKey("Concept", "g1"),
            space_id="space-a",
            pack_id="pack-x",
            raw_properties=b'{"a": 1}',
            normalized_properties=FrozenDict({"a": 1}),
            property_error=None,
            normalization_issues=(),
            digest="deadbeef",
        )
        defaults.update(overrides)
        return LegacyNodeRow(**defaults)

    def test_strips_raw_and_normalized_and_digest(self):
        stripped = recon._strip_heavy_fields(self._full_row())

        assert stripped.raw_properties is None
        assert stripped.normalized_properties is None
        assert stripped.digest == ""
        assert stripped.key == LegacyNodeKey("Concept", "g1")
        assert stripped.space_id == "space-a"

    def test_keeps_normalization_issue_count_but_empties_raw_values(self):
        issue = PropertyNormalizationIssue(
            "node", "Concept:g1", "space_id", FrozenDict({"space": "elsewhere"}), "reserved_value_conflict"
        )
        stripped = recon._strip_heavy_fields(self._full_row(normalization_issues=(issue,)))

        assert len(stripped.normalization_issues) == 1
        assert stripped.normalization_issues[0].raw_values == FrozenDict({})
        assert stripped.normalization_issues[0].reason == "reserved_value_conflict"

    def test_string_and_none_pack_id_are_left_untouched(self):
        assert recon._strip_heavy_fields(self._full_row(pack_id="pack-x")).pack_id == "pack-x"
        assert recon._strip_heavy_fields(self._full_row(pack_id=None)).pack_id is None

    def test_non_string_pack_id_replaced_with_type_named_placeholder(self):
        stripped = recon._strip_heavy_fields(self._full_row(pack_id={"nested": "blob"}))
        assert stripped.pack_id == "<non_string_pack_id:dict>"

        stripped_list = recon._strip_heavy_fields(self._full_row(pack_id=[1, 2, 3]))
        assert stripped_list.pack_id == "<non_string_pack_id:list>"


# ---------------------------------------------------------------------------
# 문서 전용 승격 (4-2절, 쟁점1)
# ---------------------------------------------------------------------------


class TestDocOnlyPromotion:
    def test_not_promoted_without_promote_doc_only_flag(self, graph_store, doc_store):
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})

        report = recon.diagnose(graph_store, doc_store)
        results = recon.heal(graph_store, doc_store, report, promote_doc_only=False)

        assert results == []
        assert graph_store.get_node("Concept", "d1") is None

    def test_promoted_with_promote_doc_only_flag(self, graph_store, doc_store):
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})

        report = recon.diagnose(graph_store, doc_store)
        assert report.doc_only[0].healable is True
        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        assert len(results) == 1
        assert results[0].outcome == "healed"
        node = graph_store.get_node("Concept", "d1")
        assert node is not None
        assert node["x"] == 1

    def test_missing_node_type_is_never_promotable(self, graph_store, doc_store):
        # (space, node_id) 만 채우고 node_type 을 빈 문자열로 남긴 원시 행을
        # 삽입해 "누락/비문자열 node_type" 을 재현한다.
        doc_store.upsert_node_doc("space-a", "", "d-bad-type", {"x": 1})

        report = recon.diagnose(graph_store, doc_store)

        assert len(report.doc_only) == 1
        row = report.doc_only[0]
        assert row.healable is False
        assert row.reason == recon.REASON_NODE_TYPE_MISSING

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)
        assert results == []

    def test_reserved_key_property_is_rejected_via_real_prepare_node(self, graph_store, doc_store):
        """쟁점1 반정규 의도: 예약 키/id 불일치는 손으로 짠 사전검사가 아니라
        실제 prepare_node() 호출이 잡아야 한다 (몽키패치 없이)."""
        doc_store.upsert_node_doc("space-a", "Concept", "d-badprop", {"id": "MISMATCHED_ID"})

        report = recon.diagnose(graph_store, doc_store)

        assert len(report.doc_only) == 1
        row = report.doc_only[0]
        assert row.healable is False
        assert row.reason.startswith(recon.REASON_PROMOTION_REJECTED)

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)
        assert results == []

    def test_non_string_pack_id_rejected_before_prepare_node(self, graph_store, doc_store):
        doc_store.upsert_node_doc("space-a", "Concept", "d-badpack", {"pack_id": 12345})

        report = recon.diagnose(graph_store, doc_store)

        assert len(report.doc_only) == 1
        row = report.doc_only[0]
        assert row.healable is False
        assert row.reason == recon.REASON_PACK_ID_NON_STRING

    def test_prepare_node_non_validation_exception_does_not_crash_tool(self, graph_store, doc_store, monkeypatch):
        """쟁점1 후반부: prepare_node() 가 GraphPropertyValidationError 밖의
        예외(예: ValueError)를 던져도 도구는 죽지 않고 "승격 불가"로 계속
        진행해야 한다 (넓은 except Exception)."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        doc_store.upsert_node_doc("space-a", "Concept", "d2", {"y": 2})

        original_prepare_node = recon.prepare_node

        def _boom(**kwargs):
            if kwargs.get("node_id") == "d1":
                raise ValueError("정수-문자열 변환 자릿수 제한 같은 임의 예외")
            return original_prepare_node(**kwargs)

        monkeypatch.setattr(recon, "prepare_node", _boom)

        report = recon.diagnose(graph_store, doc_store)

        rows_by_id = {row.node_id: row for row in report.doc_only}
        assert rows_by_id["d1"].healable is False
        assert "ValueError" in rows_by_id["d1"].reason
        assert rows_by_id["d2"].healable is True

    def test_promotion_race_reports_conflict_without_crashing(self, graph_store, doc_store):
        """5-2절 반대 방향: 진단과 적용 사이에 다른 프로세스가 같은 node_id
        를 다른 내용으로 그래프에 이미 만들었으면 NodeIdentityConflict 를
        잡아 '동시 생성' 충돌로 보고해야 한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "race1", {"x": 1})
        report = recon.diagnose(graph_store, doc_store)

        # 진단 이후, 적용 이전에 동시 쓰기가 그래프 쪽에 다른 내용으로 먼저
        # 도착했다고 가정.
        graph_store.upsert_node("Concept", "race1", {"x": "DIFFERENT"}, "space-a")

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        assert len(results) == 1
        assert results[0].outcome == "skipped_conflict"
        assert results[0].reason == "concurrent_graph_write"

    def test_property_change_between_diagnosis_and_apply_does_not_crash_tool(self, graph_store, doc_store):
        """결함1 회귀: 진단 통과 후 적용 직전에 문서 속성이 예약키로 바뀌면
        _promote_one() 의 prepare_node() 재호출이 예외를 전파하지 않고
        skipped_rejected 로 격리돼야 하고, 이어지는 다른 행은 계속 승격돼야
        한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        doc_store.upsert_node_doc("space-a", "Concept", "d2", {"y": 2})

        report = recon.diagnose(graph_store, doc_store)
        rows_by_id = {row.node_id: row for row in report.doc_only}
        assert rows_by_id["d1"].healable is True
        assert rows_by_id["d2"].healable is True

        # 진단 통과 후, 적용 직전에 다른 프로세스가 d1 의 문서 속성을 예약키로
        # 덮어썼다고 가정한다.
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"id": "MISMATCHED_ID"})

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        results_by_id = {r.node_id: r for r in results}
        assert results_by_id["d1"].outcome == "skipped_rejected"
        assert results_by_id["d1"].reason.startswith(recon.REASON_PROMOTION_REJECTED_AT_APPLY)
        assert graph_store.get_node("Concept", "d1") is None
        assert results_by_id["d2"].outcome == "healed"
        assert graph_store.get_node("Concept", "d2") is not None

    def test_apply_time_source_deleted_is_not_promoted_as_empty_node(self, graph_store, doc_store):
        """#317 이중검증 지적1 회귀: 진단 통과 후 적용 직전에 원본 문서가
        삭제되면, 재확인 없이 진단 값을 신뢰하던 옛 코드는 properties 를
        {} 로 치환해 삭제된 문서를 빈 속성 그래프 노드로 되살렸다. 이
        도구의 약속("어느 방향도 삭제하지 않는다")을 어기므로, 재확인이
        0건을 감지해 승격을 건너뛰고 그래프에 아무것도 만들지 않아야
        한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        report = recon.diagnose(graph_store, doc_store)
        assert report.doc_only[0].healable is True

        doc_store.delete_node_doc("space-a", "d1")

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        assert len(results) == 1
        assert results[0].outcome == "skipped_rejected"
        assert results[0].reason == recon.REASON_PROMOTION_VANISHED_AT_APPLY
        assert graph_store.get_node("Concept", "d1") is None

    def test_apply_time_new_duplicate_space_prevents_promotion(self, graph_store, doc_store):
        """#317 이중검증 지적2 회귀: 진단 이후 같은 node_id 가 다른 space
        에도 새로 생기면, 어느 space 를 승격해야 하는지 판단할 근거가
        없다(진단의 정체성 충돌 분류와 같은 이유). 재확인이 다건을
        감지해 어느 space 로도 승격하지 않아야 한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        report = recon.diagnose(graph_store, doc_store)
        assert report.doc_only[0].healable is True

        doc_store.upsert_node_doc("space-b", "Concept", "d1", {"x": 2})

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        assert len(results) == 1
        assert results[0].outcome == "skipped_rejected"
        assert results[0].reason == recon.REASON_DUP_SPACE_AT_APPLY
        assert graph_store.get_node("Concept", "d1") is None

    def test_apply_time_pack_id_becomes_non_string_is_rejected(self, graph_store, doc_store):
        """전수 감사로 찾은 세 번째 사례: 진단 시점에는 pack_id 가
        문자열이었어도 적용 직전에 비문자열로 바뀌면, 재확인이 읽은
        현재 값에 대해 diagnosis 와 같은 pack_id 게이트를 다시 거쳐야
        한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"pack_id": "p1"})
        report = recon.diagnose(graph_store, doc_store)
        assert report.doc_only[0].healable is True

        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"pack_id": 12345})

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        assert len(results) == 1
        assert results[0].outcome == "skipped_rejected"
        assert results[0].reason == recon.REASON_PACK_ID_NON_STRING
        assert graph_store.get_node("Concept", "d1") is None

    def test_apply_time_non_dict_properties_is_rejected_not_promoted_empty(self, graph_store, doc_store):
        """#317 이중검증 1/2라운드 FAIL 을 함께 고정하는 회귀 테스트: 원본
        문서는 여전히 존재하지만 properties 컬럼 자체가 비딕셔너리 값(예:
        리스트)으로 오염된 경우, 재확인이 그 값을 {} 로 치환하지 않고
        원형 그대로 prepare_node() 에 넘겨 그 함수의 비딕셔너리 거부에
        맡겨야 한다. LocalSQLDocStore 는 properties 를 JSON TEXT 컬럼에
        저장하므로, 저수준 SQL 로 그 컬럼에 JSON 리스트 문자열을 직접
        써 넣어 "정상 배관을 거치지 않은 오염"을 재현한다."""
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"x": 1})
        report = recon.diagnose(graph_store, doc_store)
        assert report.doc_only[0].healable is True

        doc_store._exec_write(
            f"UPDATE {doc_store._table('doc_nodes')} SET properties=:properties"
            " WHERE space=:space AND node_id=:node_id",
            {"properties": "[1, 2]", "space": "space-a", "node_id": "d1"},
        )

        results = recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        assert len(results) == 1
        assert results[0].outcome == "skipped_rejected"
        assert results[0].reason.startswith(recon.REASON_PROMOTION_REJECTED_AT_APPLY)
        assert graph_store.get_node("Concept", "d1") is None


# ---------------------------------------------------------------------------
# schema_state / 백엔드 거부 (3절/5-3절)
# ---------------------------------------------------------------------------


class TestSchemaStateRejection:
    def test_legacy_schema_state_rejects_diagnosis(self, doc_store):
        fake = _FakeGraphStore(schema_state="legacy")
        with pytest.raises(recon.ReconciliationRejectedError):
            recon.diagnose(fake, doc_store)

    def test_partial_schema_state_rejects_diagnosis(self, doc_store):
        fake = _FakeGraphStore(schema_state="partial")
        with pytest.raises(recon.ReconciliationRejectedError):
            recon.diagnose(fake, doc_store)

    def test_fresh_schema_state_allows_diagnosis(self, doc_store):
        fake = _FakeGraphStore(schema_state="fresh")
        report = recon.diagnose(fake, doc_store)
        assert report.schema_state == "fresh"

    def test_fresh_schema_state_rejects_apply_via_main(self, doc_store, monkeypatch):
        fake_graph = _FakeGraphStore(schema_state="fresh")
        monkeypatch.setattr(recon, "_build_stores", lambda args: (fake_graph, doc_store, "local", "/unused"))

        exit_code = recon.main(["--local-data-dir", "/unused", "--apply", "--skip-backup"])
        assert exit_code == recon.EXIT_REJECTED

    def test_capability_unavailable_backend_is_rejected(self, doc_store):
        with pytest.raises(GraphReadCapabilityUnavailable):
            recon.diagnose(_RejectingGraphStore(), doc_store)


# ---------------------------------------------------------------------------
# CLI / main() (5-4절 백업, 5-5절 실행 기록, 5-6절 출력)
# ---------------------------------------------------------------------------


class TestMainCli:
    def _make_target_dirs(self, tmp_path):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        g = LocalGraphStore(str(data_dir / "graph.db"))
        d = LocalSQLDocStore(str(data_dir / "doc_store.db"))
        g.upsert_node("Concept", "g1", {"a": 1}, "space-a")
        d.upsert_node_doc("space-a", "Concept", "d1", {"b": 2})
        return data_dir

    def test_apply_without_backup_or_skip_backup_flag_is_usage_error(self, tmp_path):
        data_dir = self._make_target_dirs(tmp_path)
        exit_code = recon.main(["--local-data-dir", str(data_dir), "--apply"])
        assert exit_code == recon.EXIT_USAGE

    def test_dry_run_produces_no_execution_record_file(self, tmp_path, capsys):
        data_dir = self._make_target_dirs(tmp_path)
        cwd_before = set(Path.cwd().glob("reconcile_run_*.jsonl"))
        exit_code = recon.main(["--local-data-dir", str(data_dir)])
        assert exit_code == recon.EXIT_OK
        cwd_after = set(Path.cwd().glob("reconcile_run_*.jsonl"))
        assert cwd_after == cwd_before

    def test_apply_backup_failure_aborts_before_any_write(self, tmp_path, monkeypatch):
        data_dir = self._make_target_dirs(tmp_path)

        def _boom(*args, **kwargs):
            from opencrab.stores.backup import BackupError

            raise BackupError("시뮬레이션된 백업 실패")

        monkeypatch.setattr("opencrab.stores.backup.backup_data_dir", _boom)

        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        doc_before = LocalSQLDocStore(str(data_dir / "doc_store.db")).get_node_doc("space-a", "g1")
        assert doc_before is None

        exit_code = recon.main(
            [
                "--local-data-dir",
                str(data_dir),
                "--apply",
                "--backup-to",
                str(tmp_path / "backup-dest"),
            ]
        )
        assert exit_code == recon.EXIT_BACKUP

        doc_after = LocalSQLDocStore(str(data_dir / "doc_store.db")).get_node_doc("space-a", "g1")
        assert doc_after is None

    def test_pg_backup_to_without_skip_backup_is_rejected(self, tmp_path):
        exit_code = recon.main(
            [
                "--pg-url",
                "postgresql://opencrab:opencrab@localhost:5432/opencrab_nonexistent_317",
                "--apply",
                "--backup-to",
                str(tmp_path / "backup-dest"),
            ]
        )
        assert exit_code == recon.EXIT_USAGE

    def test_apply_backup_resolves_local_data_dir_when_flag_omitted(self, tmp_path, monkeypatch):
        """결함2 회귀: --local-data-dir 를 생략하고 LOCAL_DATA_DIR 환경변수로만
        데이터 디렉터리를 지정해도, 백업이 그 해석된 경로를 받아야 한다(원시
        --local-data-dir 인자를 그대로 넘기면 None 이라 TypeError 로 죽는다)."""
        data_dir = self._make_target_dirs(tmp_path)
        monkeypatch.setenv("LOCAL_DATA_DIR", str(data_dir))
        backup_dest = tmp_path / "backup-dest"
        backup_dest.mkdir()

        exit_code = recon.main(
            ["--apply", "--backup-to", str(backup_dest), "--record-to", str(tmp_path / "run.jsonl")]
        )

        assert exit_code == recon.EXIT_OK
        assert backup_dest.exists()
        assert any(backup_dest.iterdir()), "백업 대상 디렉터리에 산출물이 남아야 한다"

    def test_apply_writes_execution_record_with_correct_directions(self, tmp_path, capsys):
        data_dir = self._make_target_dirs(tmp_path)
        record_path = tmp_path / "run.jsonl"

        exit_code = recon.main(
            [
                "--local-data-dir",
                str(data_dir),
                "--apply",
                "--promote-doc-only",
                "--skip-backup",
                "--record-to",
                str(record_path),
            ]
        )
        assert exit_code == recon.EXIT_OK
        assert record_path.exists()

        lines = [json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines()]
        directions = sorted(entry["direction"] for entry in lines)
        assert directions == ["backfill", "promotion"]
        assert all(entry["outcome"] == "healed" for entry in lines)

        out = capsys.readouterr().out
        assert str(record_path) in out

    def test_record_write_failure_does_not_roll_back_completed_writes(self, tmp_path, monkeypatch):
        data_dir = self._make_target_dirs(tmp_path)

        class _BrokenWriter(recon.RecordWriter):
            def append(self, result):
                raise OSError("시뮬레이션된 기록 파일 쓰기 실패")

        monkeypatch.setattr(recon, "RecordWriter", _BrokenWriter)

        exit_code = recon.main(
            [
                "--local-data-dir",
                str(data_dir),
                "--apply",
                "--skip-backup",
                "--record-to",
                str(tmp_path / "run.jsonl"),
            ]
        )
        assert exit_code == recon.EXIT_RECORD_WRITE_FAILED

        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        doc = LocalSQLDocStore(str(data_dir / "doc_store.db")).get_node_doc("space-a", "g1")
        assert doc is not None, "기록 파일 실패 전에 이미 끝난 스토어 쓰기가 롤백되면 안 된다"


class TestFormatReportHealSummary:
    def test_skipped_outcomes_get_distinct_labels_and_per_row_reasons(self):
        """결함3 회귀: skipped_rejected 가 기존 건너뜀(이미 존재/동시 충돌)
        한 줄 합계에 말없이 섞이지 않고, outcome 별로 개별 라벨과 사유가
        보여야 한다."""
        report = recon.DiagnosisReport(schema_state="target")
        heal_results = [
            recon.HealResult("g1", "space-a", "backfill", "healed"),
            recon.HealResult("d1", "space-a", "promotion", "healed"),
            recon.HealResult("g2", "space-a", "backfill", "skipped_exists"),
            recon.HealResult("d2", "space-a", "promotion", "skipped_conflict", "concurrent_graph_write"),
            recon.HealResult(
                "d3",
                "space-a",
                "promotion",
                "skipped_rejected",
                f"{recon.REASON_PROMOTION_REJECTED_AT_APPLY}: ValueError: reserved graph property",
            ),
        ]

        out = recon.format_report(report, heal_results=heal_results)

        assert "건너뜀(이미 존재/동시 충돌)" not in out
        assert "건너뜀(이미 존재): 1건" in out
        assert "건너뜀(동시 충돌): 1건" in out
        assert "건너뜀(적용 거부): 1건" in out
        assert "node_id=d3" in out
        assert "outcome=skipped_rejected" in out
        assert recon.REASON_PROMOTION_REJECTED_AT_APPLY in out


# ---------------------------------------------------------------------------
# 인수 후 스토어 상태 등치 (받아들임 기준: 치유 후 양쪽 노드 키 집합이 같다)
# ---------------------------------------------------------------------------


class TestAcceptanceCriteria:
    def test_key_sets_equal_after_full_heal(self, graph_store, doc_store):
        graph_store.upsert_node("Concept", "g1", {"a": 1}, "space-a")
        graph_store.upsert_node("Concept", "g2", {"a": 2}, "space-a")
        doc_store.upsert_node_doc("space-a", "Concept", "d1", {"b": 1})
        doc_store.upsert_node_doc("space-a", "Concept", "both1", {"c": 1})
        graph_store.upsert_node("Concept", "both1", {"c": 1}, "space-a")

        report = recon.diagnose(graph_store, doc_store)
        recon.heal(graph_store, doc_store, report, promote_doc_only=True)

        graph_ids = {row.key.node_id for row in graph_store.inspect_graph_identity().nodes}
        doc_ids = {node_id for _space, node_id, _type in doc_store.iter_node_identities()}
        assert graph_ids == doc_ids == {"g1", "g2", "d1", "both1"}

    def test_synthetic_single_store_node_is_reported_exhaustively_and_control_group_untouched(
        self, graph_store, doc_store
    ):
        # 대조군: 이미 양쪽에 일치하게 존재.
        for i in range(5):
            graph_store.upsert_node("Concept", f"stable{i}", {"v": i}, "space-a")
            doc_store.upsert_node_doc("space-a", "Concept", f"stable{i}", {"v": i})

        # 합성 단일 스토어 노드.
        graph_store.upsert_node("Concept", "only-in-graph", {"v": 99}, "space-a")

        report = recon.diagnose(graph_store, doc_store)

        assert [row.node_id for row in report.graph_only] == ["only-in-graph"]
        assert report.doc_only == []
        assert report.conflicts == []

        recon.heal(graph_store, doc_store, report, promote_doc_only=False)

        for i in range(5):
            doc = doc_store.get_node_doc("space-a", f"stable{i}")
            assert doc["properties"] == {"v": i}
        healed_doc = doc_store.get_node_doc("space-a", "only-in-graph")
        assert healed_doc is not None
