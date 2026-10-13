"""issue #74 — 소스 텍스트 적재가 그래프에 반영되지 않던 결함의 회귀 테스트.

`write_source`(REST `/api/ingest`, CLI `ingest`, MCP `pack_ingest`
`text_as_node=False`, `pack_fork` 가 모두 지나는 소스 텍스트 초크포인트)는
doc_sources 행과 벡터 두 다리만 쓰고 그래프 다리가 없었다. 그래서 적재한
텍스트가 `ontology_get_node`, 노드 목록, impact, ReBAC 어디에서도 보이지
않았다. MCP `pack_ingest` 의 기본값 `text_as_node=True` 만 `evidence/TextUnit`
그래프 노드를 만들어, 같은 데이터가 표면에 따라 절반만 저장됐다.

이 파일은 설계 v13 §9 의 검출기 가운데 핵심을 구현한다. 수정 전 코드에서는
`write_source` 에 `graph` 키워드 인자가 없어 `TypeError` 로, 진입점 경로
테스트는 그래프 조회가 비어 어서션으로 실패한다.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from sqlalchemy import text as _sql_text

from opencrab.auth import Principal, principal_scope
from opencrab.cli import main as _cli_main
from opencrab.common.graph_identity import (
    GraphSchemaMigrationRequired,
    GraphWriteCapabilityUnavailable,
    GraphWriteUnavailable,
    NodeIdentityConflict,
)
from opencrab.config import Settings
from opencrab.ontology.builder import OntologyBuilder
from opencrab.ontology.query import HybridQuery
from opencrab.pack.fork import fork_pack
from opencrab.pack.fork_remap import derive_text_id
from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError, create_pack
from opencrab.pack.source_writer import write_source

ALICE = Principal(user_id="user_alice_74", is_local=False, disabled=False)
BOB = Principal(user_id="user_bob_74", is_local=False, disabled=False)


@pytest.fixture
def stack(tmp_path):
    """Real local store stack, isolated per test. Mirrors tests/test_pack_fork.py."""
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    settings = Settings(
        STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path), EMBEDDING_BACKEND="local",
    )
    graph = make_graph_store(settings)
    docs = make_doc_store(settings)
    vector = make_vector_store(settings)
    sql = make_sql_store(settings)
    builder = OntologyBuilder(graph, docs, sql, vec=vector)
    hybrid = HybridQuery(vector, graph)
    hybrid._doc_store = docs

    with sql._engine.begin() as conn:
        for p in (ALICE, BOB):
            conn.execute(
                _sql_text(
                    "INSERT INTO users (user_id, display_name, is_local) "
                    "VALUES (:u, :n, 0)"
                ),
                {"u": p.user_id, "n": p.user_id},
            )
    create_pack(sql, ALICE.user_id, "pack-a")
    create_pack(sql, BOB.user_id, "pack-b")

    st = {
        "sql": sql, "graph": graph, "docs": docs, "vector": vector,
        "builder": builder, "hybrid": hybrid,
    }
    try:
        yield st
    finally:
        vector.close()
        graph.close()
        docs.close()
        sql._engine.dispose()


def _write(stack, *, text="본문", source_id="src-1", metadata=None,
           pack_id="pack-a", principal=ALICE, graph=None, **kw) -> dict[str, Any]:
    with principal_scope(principal):
        return write_source(
            stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
            graph=stack["graph"] if graph is None else graph,
            text=text, source_id=source_id, metadata=metadata, pack_id=pack_id,
            **kw,
        )


def _node(stack, node_id: str) -> dict[str, Any] | None:
    return stack["graph"].get_node("TextUnit", node_id)


# ---------------------------------------------------------------------------
# 설계 §9 테스트 1 — 소스가 그래프에 일급 노드로 나타난다
# ---------------------------------------------------------------------------


def test_source_write_materialises_an_evidence_textunit_node(stack):
    """이 이슈의 핵심 어서션. 수정 전에는 그래프에 아무 행도 생기지 않았다."""
    receipt = _write(stack, text="적재한 본문", source_id="src-node",
                     metadata={"title": "제목", "source": "출처"})

    assert receipt["stores"]["graph"] == "ok", receipt["stores"]

    node = _node(stack, "src-node")
    assert node is not None, "그래프에 TextUnit 노드가 없다 — 이 이슈의 결함 그 자체"
    assert node["text"] == "적재한 본문"
    assert node["pack_id"] == "pack-a"
    assert node["owner_id"] == ALICE.user_id
    # metadata 의 선택 키는 노드로 복사된다(복사 미구현 변이를 잡는다).
    assert node["title"] == "제목"
    assert node["source"] == "출처"

    # 소스 축은 그대로 살아 있다.
    assert receipt["stores"]["documents"].startswith("ok")
    assert stack["docs"].get_source("src-node") is not None


def test_the_node_is_visible_through_the_scoped_by_id_read(stack):
    """`ontology_get_node` 가 쓰는 바로 그 읽기 경로에서 보여야 한다."""
    _write(stack, source_id="src-scoped")
    found = stack["graph"].get_node_by_id_scoped("src-scoped", ["pack-a"])
    assert found is not None, "ontology_get_node 가 여전히 found: false 를 낸다"


# ---------------------------------------------------------------------------
# 설계 §9 테스트 1b — graph space 는 호출자 metadata 와 무관하게 evidence
# ---------------------------------------------------------------------------


def test_graph_space_is_pinned_to_evidence_while_the_vector_keeps_the_callers(stack):
    """`space=meta.get("space", "evidence")` 변이를 잡는다.

    TextUnit 은 문법상 evidence 공간에만 존재한다. 반면 벡터 메타데이터는
    호출자가 준 space 를 그대로 유지해야 한다 — 소스 읽기 경로의 근거다.
    """
    _write(stack, source_id="src-space", metadata={"space": "resource"})

    node = _node(stack, "src-space")
    assert node is not None
    assert node["space"] == "evidence", node

    row = stack["vector"].get_by_id("src-space")
    assert row is not None
    assert row["metadata"]["space"] == "resource", row["metadata"]


# ---------------------------------------------------------------------------
# 설계 §9 테스트 2 — 그래프가 없으면 doc/벡터에도 쓰지 않는다 (필수 다리)
# ---------------------------------------------------------------------------


class _UnavailableGraph:
    available = False

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        raise RuntimeError("graph store is not available")


def test_graph_unavailable_writes_nothing_at_all(stack):
    """그래프가 system of record 다. 없으면 소스도 쓰지 않는다.

    그래프 없이 doc/벡터만 쓰는 것이 정확히 이 이슈가 보고한 상태이므로,
    그 조합을 계속 만들어내지 않는 것이 수정의 일부다.
    """
    receipt = _write(stack, source_id="src-nograph", graph=_UnavailableGraph())

    assert receipt["stores"]["graph"] == "unavailable", receipt["stores"]
    assert receipt["stores"]["documents"] == "skipped (graph write failed)", receipt["stores"]
    assert receipt["stores"]["chromadb"] == "skipped (graph write failed)", receipt["stores"]
    assert stack["docs"].get_source("src-nograph") is None
    assert stack["vector"].get_by_id("src-nograph") is None


def test_mcp_pack_ingest_rejects_before_write_source_when_graph_unavailable(stack):
    """설계 §9-2 후반: MCP `pack_ingest` 는 `write_source` 에 도달하지도 않고

    그래프 미가용을 조기 거절해야 한다(현재 동작의 무회귀 고정). 이 조기
    거절이 사라지면(예: 그래프 가용성 검사를 지우거나 뒤로 미루는 변이)
    `write_source` 까지 흘러 들어가 위 `test_graph_unavailable_writes_
    nothing_at_all` 과는 다른 응답 형태(`{"error": ...}` 가 아니라 이 함수의
    실패 receipt)를 내거나, 최악의 경우 부분 저장을 만든다.
    """
    from opencrab.billing.hooks import BillingHooks
    from opencrab.mcp.tools.pack import pack_ingest
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.rebac import ReBACEngine

    double_graph = _UnavailableGraph()
    ctx = {
        "neo4j": double_graph,
        "chroma": stack["vector"],
        "mongo": stack["docs"],
        "sql": stack["sql"],
        "builder": OntologyBuilder(double_graph, stack["docs"], stack["sql"], vec=stack["vector"]),
        "rebac": ReBACEngine(double_graph, stack["sql"]),
        "impact": ImpactEngine(double_graph, stack["sql"]),
        "hybrid": stack["hybrid"],
        "billing": BillingHooks(stack["sql"]),
    }

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(
            "pack-a", text="본문", source_id="src-mcp-early-reject", text_as_node=False,
        )

    assert result == {"error": "graph store unavailable"}, result
    assert stack["docs"].get_source("src-mcp-early-reject") is None
    assert stack["vector"].get_by_id("src-mcp-early-reject") is None
    assert _node(stack, "src-mcp-early-reject") is None


# ---------------------------------------------------------------------------
# 설계 §9 테스트 5·6·6b — 신원 가드가 그래프 축까지 본다
# ---------------------------------------------------------------------------


def test_a_graph_only_foreign_node_is_refused_before_any_write(stack):
    """그래프에만 다른 팩의 노드가 있는 경우.

    doc_sources 나 벡터에 foreign 행을 심으면 기존 `source_identity_conflict`
    가 이미 거절하므로 검출력이 없다. 그래프에만 심는 것이 이 테스트의 요점이다.
    """
    # 그래프에**만** 심는다. `builder.add_node(write_vector=False)` 는 벡터만
    # 끄고 doc_nodes 와 레지스트리에는 그대로 쓰므로, 그것으로 심으면 새
    # 그래프 축을 제거해도 doc_nodes 축이 거절을 일으켜 검출력이 사라진다.
    stack["graph"].upsert_node(
        node_type="TextUnit", node_id="src-foreign",
        properties={"pack_id": "pack-b", "text": "남의 것"}, space_id="evidence",
    )
    assert stack["docs"].get_node_doc("evidence", "src-foreign") is None
    assert stack["vector"].get_by_id("src-foreign") is None

    with pytest.raises(ValueError, match="already attributed"):
        _write(stack, source_id="src-foreign")

    # 부분 저장 없음: 어느 스토어에도 행이 생기지 않는다.
    assert stack["docs"].get_source("src-foreign") is None
    assert stack["vector"].get_by_id("src-foreign") is None
    # 설계 §9-5 는 doc_sources·벡터 두 축만이 아니라 doc_nodes 와 sql
    # 레지스트리까지 무기록을 요구한다. 거절 뒤 builder.add_node 의 팬아웃
    # 루프가 조금이라도 돌면(예: 신원 가드를 우회하는 변이) 이 두 축에서
    # 잔여가 남는다.
    assert stack["docs"].get_node_doc("evidence", "src-foreign") is None
    assert not _sql_registry_has(stack, "src-foreign")


def test_an_unattributed_legacy_node_is_taken_over_by_the_writing_pack(stack):
    """설계 §4.2 의 명시적 판정. 무소유 행은 모든 신원 프로브를 통과한다.

    거절하는 쪽을 택하면 시딩 스크립트가 만든 레거시 노드 위로는 영영
    적재할 수 없다.
    """
    stack["graph"].upsert_node(
        node_type="TextUnit", node_id="src-legacy",
        properties={"text": "레거시"}, space_id="evidence",
    )

    receipt = _write(stack, text="새 본문", source_id="src-legacy")
    assert receipt["stores"]["graph"] == "ok", receipt["stores"]

    node = _node(stack, "src-legacy")
    assert node["pack_id"] == "pack-a"
    assert node["owner_id"] == ALICE.user_id
    assert node["text"] == "새 본문"


# ---------------------------------------------------------------------------
# 설계 §9 테스트 8·10·12g — 재적재 계약
# ---------------------------------------------------------------------------


def test_rewriting_a_source_updates_both_the_node_and_the_doc_row(stack):
    """`test_own_source_id_may_be_rewritten` 이 고정한 계약을 유지해야 한다.

    그래프의 `upsert_node` 는 properties digest 가 다르면 거절하므로, 이
    경로는 CAS 갱신을 태워야 한다. 태우지 않으면 내용을 바꾼 재적재가
    NodeIdentityConflict 로 깨진다.
    """
    _write(stack, text="첫 본문", source_id="src-rewrite")
    receipt = _write(stack, text="둘째 본문", source_id="src-rewrite")

    assert receipt["stores"]["graph"] == "ok", receipt["stores"]
    assert _node(stack, "src-rewrite")["text"] == "둘째 본문"
    assert stack["docs"].get_source("src-rewrite")["text"] == "둘째 본문"


def test_reingesting_identical_content_is_idempotent(stack):
    _write(stack, text="같은 본문", source_id="src-same")
    receipt = _write(stack, text="같은 본문", source_id="src-same")
    assert receipt["stores"]["graph"] == "ok", receipt["stores"]
    assert len(stack["graph"].get_nodes_by_id("src-same")) == 1


def test_node_properties_are_replaced_wholesale_not_merged(stack):
    """설계 §4.2 의 명시적 판정. 노드는 소스의 최신 쓰기를 그대로 반영한다.

    `title` 하나만 보면 `title` 만 특례로 지우고 나머지는 병합 보존하는
    구현이 통과한다. 임의 키까지 세 개를 함께 본다. `reviewer_note` 는
    이 경로가 절대 쓰지 않는 키라서 `_write` 의 `metadata` 로 넣어도 노드에
    닿지 않는다(`_graph_leg` 는 `title`/`source` 두 키만 복사한다) — 그래서
    그래프에 직접 심어야 "before 에 있었는데 after 에 사라졌다"를 실제로
    검출한다. 이전 버전은 `_write` 의 `metadata` 로 넣어 `before` 에도 결코
    나타나지 않았으므로 `after` 의 부재가 무엇을 증명하지도 못했다.
    """
    stack["graph"].upsert_node(
        node_type="TextUnit", node_id="src-replace",
        properties={
            "pack_id": "pack-a", "text": "첫 본문",
            "title": "제목", "source": "출처", "reviewer_note": "메모",
        },
        space_id="evidence",
    )
    before = _node(stack, "src-replace")
    assert before["title"] == "제목"
    assert before["source"] == "출처"
    assert before["reviewer_note"] == "메모"

    _write(stack, text="둘째 본문", source_id="src-replace")
    after = _node(stack, "src-replace")
    assert after["text"] == "둘째 본문"
    assert "title" not in after, after
    assert "source" not in after, after
    assert "reviewer_note" not in after, after


# ---------------------------------------------------------------------------
# 설계 §9 테스트 12 — 노드 id 예산 경계 (양방향)
# ---------------------------------------------------------------------------


def _budget() -> int:
    from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET

    return SOURCE_NODE_ID_BUDGET


def test_an_id_at_the_budget_still_becomes_a_node(stack):
    at_limit = "A" * _budget()
    receipt = _write(stack, source_id=at_limit)
    assert receipt["stores"]["graph"] == "ok", receipt["stores"]
    assert _node(stack, at_limit) is not None


def test_an_id_past_the_budget_is_carved_out_but_still_ingests(stack):
    """T77 보존: 노드 id 로 살아남을 수 없는 소스는 노드를 만들지 않는다.

    그러나 doc/벡터 적재 자체는 계속 성공해야 한다 — 오늘 성공하는 적재를
    막는 것은 이 이슈의 범위가 아니다.
    """
    over = "B" * (_budget() + 1)
    receipt = _write(stack, source_id=over)

    assert receipt["stores"]["graph"] == (
        "skipped (id exceeds the node id limit after fork remap)"
    ), receipt["stores"]
    assert receipt["stores"]["documents"].startswith("ok"), receipt["stores"]
    assert receipt["stores"]["chromadb"].startswith("ok"), receipt["stores"]
    assert stack["vector"].get_by_id(over) is not None
    assert stack["graph"].get_nodes_by_id(over) == []


def test_a_carved_out_id_that_already_has_a_node_is_not_skipped(stack):
    """카브아웃은 "새로 만들지 않는다"이지 "그래프를 보지 않는다"가 아니다.

    이미 노드가 있는데 건너뛰면 그래프는 옛 텍스트, doc/벡터는 새 텍스트인
    발산이 생긴다 — §4.1 이 막으려는 바로 그 상태다.
    """
    over = "C" * (_budget() + 1)
    with principal_scope(ALICE):
        stack["builder"].add_node(
            space="evidence", node_type="TextUnit", node_id=over,
            properties={"pack_id": "pack-a", "text": "옛 본문"}, pack_id="pack-a",
        )

    _write(stack, text="새 본문", source_id=over)
    assert _node(stack, over)["text"] == "새 본문"


def test_a_carved_out_id_with_a_rejected_graph_update_leaves_doc_and_vector_at_old_values(stack):
    """설계 §9-12ex 후반. 카브아웃 대상 id 에 이미 노드가 있고 그래프 갱신

    자체가 실패하는 더블에서는, doc_sources·벡터가 옛값 그대로여야 한다.
    갱신 성공만 보면 그래프 실패 + doc/벡터만 새 값이라는, §4.1 이 막으려는
    바로 그 발산을 검출하지 못한다. 파일에 이미 있는
    `_GraphThatRejectsTheWrite` 더블을 재사용한다.
    """
    over = "C" * (_budget() + 1)
    with principal_scope(ALICE):
        stack["builder"].add_node(
            space="evidence", node_type="TextUnit", node_id=over,
            properties={"pack_id": "pack-a", "text": "옛 본문"}, pack_id="pack-a",
        )
    # 실제 그래프/doc/벡터에 "옛 본문"을 실제로 심어 둔다 — 그래야 그 값이
    # 갱신 실패 뒤에도 "그대로"인지를 의미 있게 검사할 수 있다.
    _write(stack, text="옛 본문", source_id=over)

    existing_rows = stack["graph"].get_nodes_by_id(over)
    digest = stack["graph"].get_node_digest(over, node_type="TextUnit")
    reject_double = _GraphThatRejectsTheWrite(
        NodeIdentityConflict("graph update rejected"),
        existing_rows=existing_rows, digest=digest,
    )

    with pytest.raises(NodeIdentityConflict):
        _write(stack, text="새 본문", source_id=over, graph=reject_double)

    assert _node(stack, over)["text"] == "옛 본문"
    assert stack["docs"].get_source(over)["text"] == "옛 본문"
    assert stack["vector"].get_by_id(over)["document"] == "옛 본문"


class _NonListProbe:
    """"없음"과 "확인 불가"를 뭉개는 반환값. `if not rows:` 구현을 잡는다."""

    available = True

    def __init__(self, value):
        self._value = value

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        return self._value


@pytest.mark.parametrize("bad", [None, {}, ()])
def test_a_probe_that_cannot_say_no_does_not_trigger_the_carve_out(stack, bad):
    """카브아웃을 타면 doc/벡터가 쓰이고 그래프는 비어 결함이 재생된다.

    카브아웃을 타지 않으면 이 더블은 신원 프로브를 답하지 못하므로
    `CONFLICT_UNVERIFIABLE` 로 fail-closed 거절된다. 어느 쪽인지가
    `if not rows:` 구현과 `isinstance(rows, list)` 구현을 가른다.
    """
    over = "D" * (_budget() + 1)
    with pytest.raises(ValueError, match="cannot verify"):
        _write(stack, source_id=over, graph=_NonListProbe(bad))
    assert stack["docs"].get_source(over) is None
    assert stack["vector"].get_by_id(over) is None


class _AvailableFalseProbe:
    """§9-12f(a): 예산 초과 id 인데 그래프 자체가 `available=False` 다."""

    available = False

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        raise RuntimeError("graph store is not available")


class _ProbeRaises:
    """§9-12f(b): `available=True` 인데 `get_nodes_by_id` 가 예외를 던진다."""

    available = True

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        raise RuntimeError("boom during by-id probe")


class _ProbeMissingMethod:
    """§9-12f(c): `available=True` 인데 `get_nodes_by_id` 메서드 자체가 없다."""

    available = True


def test_12f_a_over_budget_id_graph_unavailable_does_not_trigger_the_carve_out(stack):
    """§9-12f(a). 그래프 미가용은 카브아웃(§4.6)이 아니라 §4.1 의 필수 다리

    규칙을 태워야 한다. 실측(격리 스크립트, 프로덕션 미수정): `available=False`
    에서는 `_existing_node_rows` 가 그래프를 아예 들여다보지 않고 `None` 을
    돌려주므로(rows==[] 가 아님) 카브아웃이 적용되지 않고, 뒤이어
    `builder.add_node` 의 그래프-미가용 조기 반환 경로를 타 예외 없이
    `graph: "unavailable"` 리셉트로 끝난다. 카브아웃을 탔다면 `graph` 상태가
    "skipped (id exceeds the node id limit after fork remap)"이었을 것이다.
    """
    over = "L" * (_budget() + 1)
    receipt = _write(stack, source_id=over, graph=_AvailableFalseProbe())

    assert receipt["stores"]["graph"] == "unavailable", receipt["stores"]
    assert receipt["stores"]["documents"] == "skipped (graph write failed)", receipt["stores"]
    assert receipt["stores"]["chromadb"] == "skipped (graph write failed)", receipt["stores"]
    assert stack["docs"].get_source(over) is None
    assert stack["vector"].get_by_id(over) is None


def test_12f_b_over_budget_id_probe_exception_does_not_trigger_the_carve_out(stack):
    """§9-12f(b). `get_nodes_by_id` 가 예외를 던지면 카브아웃 판정이 "확인 불가"로

    fail-closed 된다(§4.6). 실측: 카브아웃을 타지 않고 정상 경로로 진행하면
    `builder.add_node` 내부의 `node_identity_conflict` -> `_check_by_id_axis`
    가 같은 프로브를 다시 불러 동일하게 예외를 만나 `CONFLICT_UNVERIFIABLE`
    로 거절하므로 `ValueError("... cannot verify ...")` 가 그대로 전파된다.
    카브아웃을 탔다면 이 거절 자체가 나지 않고 receipt 로 조용히 끝났을 것이다.
    """
    over = "M" * (_budget() + 1)
    with pytest.raises(ValueError, match="cannot verify"):
        _write(stack, source_id=over, graph=_ProbeRaises())
    assert stack["docs"].get_source(over) is None
    assert stack["vector"].get_by_id(over) is None


def test_12f_c_over_budget_id_probe_method_missing_does_not_trigger_the_carve_out(stack):
    """§9-12f(c). `get_nodes_by_id` 메서드 자체가 없는 경우도 (b)와 동일하게

    fail-closed 거절이다 — `getattr(graph, "get_nodes_by_id", None) is None`
    이 두 곳(§4.6 의 카브아웃 프로브, `_check_by_id_axis`) 모두에서
    `CONFLICT_UNVERIFIABLE`/"cannot tell"로 처리된다.
    """
    over = "N" * (_budget() + 1)
    with pytest.raises(ValueError, match="cannot verify"):
        _write(stack, source_id=over, graph=_ProbeMissingMethod())
    assert stack["docs"].get_source(over) is None
    assert stack["vector"].get_by_id(over) is None


# ---------------------------------------------------------------------------
# 설계 §9 테스트 13 — fork 옵트아웃
# ---------------------------------------------------------------------------


def test_write_graph_false_without_fork_copy_is_refused(stack):
    """옵트아웃이 일반 탈출구가 되면 이 이슈의 결함이 그대로 재생된다.

    호출자 인벤토리 테스트는 저장소 안의 리터럴만 보는 탐지기다. 변수로
    전달되는 값과 저장소 밖 호출까지 막으려면 실행 시 불변식이어야 한다.
    """
    with pytest.raises(ValueError, match="fork_copy=True"):
        _write(stack, source_id="src-escape", write_graph=False)
    assert stack["docs"].get_source("src-escape") is None
    assert stack["vector"].get_by_id("src-escape") is None


def test_write_graph_false_skips_the_graph_leg_on_the_fork_path(stack):
    """fork 는 노드를 별도로 복사하므로 소스 단계가 노드를 덮어쓰면 안 된다."""
    from opencrab.pack.ownership import begin_pack_creation

    with principal_scope(ALICE):
        dst = begin_pack_creation(stack["sql"], ALICE.user_id, "dst-fork",
                                  forked_from="pack-a")
    receipt = _write(stack, source_id="src-fork", pack_id=dst,
                     write_graph=False, write_vector=False,
                     origin="server", fork_copy=True)
    assert receipt["stores"]["graph"] == "skipped (raw copy)", receipt["stores"]
    assert stack["graph"].get_nodes_by_id("src-fork") == []
    assert receipt["stores"]["documents"].startswith("ok")


def test_the_fork_path_is_the_only_production_user_of_write_graph_false():
    """설계 §9-13 후반: fork 가 실제로 그 인자를 넘기는지 별도로 고정한다.

    단위 동작(위 테스트)만 보면 fork 가 인자를 빼도 통과한다 — 실제로
    적대검증자가 그 역변이로 147건이 전부 통과함을 실측했다. 인벤토리는
    프로덕션 호출자 파일 집합을 정확히 고정하므로 그 역변이를 잡는다.
    `fork_copy`/`write_vector` 가 쓰는 것과 같은 패턴이다.
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    files = []
    for path in sorted((repo / "opencrab").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # AST, not a regex over the file text: `write_source` 자신의
            # docstring 과 주석이 이 플래그를 설명하므로 문자열 검색은 정의
            # 파일을 호출자로 오인한다. 실제 호출의 키워드 인자만 센다.
            if isinstance(node, ast.Call) and any(
                kw.arg == "write_graph"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is False
                for kw in node.keywords
            ):
                files.append(path.relative_to(repo).as_posix())
                break
    assert files == ["opencrab/pack/fork.py"], files


# ---------------------------------------------------------------------------
# 잔여 검출기 — 벡터 1회 쓰기 고정
# ---------------------------------------------------------------------------


def test_vector_upsert_texts_is_called_exactly_once_per_source_write(stack, monkeypatch):
    """소스 다리와 노드 다리가 각각 임베딩하면 이중 비용·이중 벡터 행이 생긴다.

    `_graph_leg` 는 `builder.add_node(write_vector=False)` 로 노드 쪽 임베딩을
    꺼야 한다 — 끄지 않으면 이 스파이가 2회를 본다.
    """
    calls: list[Any] = []
    original = type(stack["vector"]).upsert_texts

    def _spy(self, *a, **kw):
        calls.append((a, kw))
        return original(self, *a, **kw)

    monkeypatch.setattr(type(stack["vector"]), "upsert_texts", _spy, raising=True)

    _write(stack, text="본문", source_id="src-vec-once")
    assert len(calls) == 1, calls


def test_vector_metadata_preserves_source_identity_and_carries_no_node_shape(stack):
    """설계 §9-15. `space` 하나가 아니라 `user_id`/`source_id`/`space` 가 모두

    보존되고, 빌더의 노드 형상 키인 `node_id` 는 섞이지 않아야 한다.

    검출력은 스탬프와 메타데이터 자체를 겨냥한 역변이로 증명된다 — `stamp` 의
    키 집합을 줄이거나 `node_id` 를 소스 메타데이터에 넣으면 이 테스트가
    단독으로 죽는다(적대검증자 실측). `write_vector=False` → `True` 역변이는
    **잡지 못한다**: `hybrid.ingest` 가 뒤이어 같은 id 를 소스 메타데이터로
    덮어써 최종 형상이 같아지기 때문이다. 그 역변이는 `upsert_texts` 호출
    횟수 테스트와 호출자 인벤토리가 잡는다.
    """
    _write(stack, text="본문", source_id="src-vecmeta")

    row = stack["vector"].get_by_id("src-vecmeta")
    assert row is not None
    meta = row["metadata"]
    assert meta["user_id"] == ALICE.user_id, meta
    assert meta["source_id"] == "src-vecmeta", meta
    assert meta["space"] == "evidence", meta
    assert "node_id" not in meta, meta


# ---------------------------------------------------------------------------
# 설계 §9 테스트 3·3b — 그래프가 있지만 쓰기 자체를 거절한다
# ---------------------------------------------------------------------------


class _GraphThatRejectsTheWrite:
    """`available=True` 인데 실제 쓰기(`upsert_node`/`reclassify_node`)에서만 거부한다.

    신원 프로브(`get_node`/`get_nodes_by_id`)는 통과시켜 실패 지점을
    `add_node` 의 쓰기 시도 한 곳으로 정확히 좁힌다. `existing_rows`/`digest`
    는 기본값(빈 목록/`None`)이면 "신규 삽입" 형상이고, 값을 채우면 "이미
    노드가 있어 CAS 갱신 경로를 타는" 형상을 흉내낸다(§9-12ex 후반).
    """

    available = True

    def __init__(self, exc: Exception, *, existing_rows=None, digest=None):
        self._exc = exc
        self._existing_rows = [] if existing_rows is None else existing_rows
        self._digest = digest

    def get_node(self, node_type, node_id):  # noqa: ARG002
        return None

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        return self._existing_rows

    def get_node_digest(self, node_id, node_type=None):  # noqa: ARG002
        return self._digest

    def upsert_node(self, **kwargs):  # noqa: ARG002
        raise self._exc

    def reclassify_node(self, *args, **kwargs):  # noqa: ARG002
        raise self._exc


def _sql_registry_has(stack, node_id: str) -> bool:
    with stack["sql"]._engine.begin() as conn:
        row = conn.execute(
            _sql_text("SELECT node_id FROM ontology_nodes WHERE node_id=:n"),
            {"n": node_id},
        ).fetchone()
    return row is not None


@pytest.mark.parametrize(
    "exc",
    [
        GraphSchemaMigrationRequired("schema migration required"),
        GraphWriteUnavailable("graph write unavailable"),
        GraphWriteCapabilityUnavailable("graph write capability unavailable"),
    ],
    ids=["migration-required", "write-unavailable", "capability-unavailable"],
)
def test_declared_graph_write_exceptions_leave_no_registry_residue(stack, exc):
    """`add_node` 의 첫 except 절이 이 세 타입을 즉시 재던진다.

    그래서 `_graph_leg` 의 `node_stores` 병합 루프가 전혀 돌지 않아 doc_nodes/sql
    레지스트리에 아무 키도 생기지 않는다. 이 세 타입까지 두 번째(흡수) except
    절로 새면 mongo/sql 이 계속 쓰여 잔여가 남는다.
    """
    receipt = _write(stack, source_id="src-declared-reject",
                     graph=_GraphThatRejectsTheWrite(exc))

    assert receipt["stores"]["graph"].startswith("error:"), receipt["stores"]
    assert "docs" not in receipt["stores"], receipt["stores"]
    assert "sql" not in receipt["stores"], receipt["stores"]
    assert receipt["stores"]["documents"] == "skipped (graph write failed)"
    assert receipt["stores"]["chromadb"] == "skipped (graph write failed)"
    assert stack["docs"].get_source("src-declared-reject") is None
    assert stack["vector"].get_by_id("src-declared-reject") is None
    assert stack["docs"].get_node_doc("evidence", "src-declared-reject") is None
    assert not _sql_registry_has(stack, "src-declared-reject")


def test_generic_runtime_error_from_graph_write_leaves_no_doc_or_sql_residue(stack):
    """선언되지 않은 `RuntimeError` 는 `add_node` 의 두 번째(흡수) except 절로 빠진다.

    #495 이전에는 흡수된 뒤에도 mongo/sql 을 계속 써서 고아 doc_nodes 행과
    sql 레지스트리 행이 남았다. 이제 그래프가 이 쓰기를 갖지 않으면(digest
    조회 결과 None) 나머지 다리를 건너뛴다. `_graph_leg` 는 그 `docs`/`sql`
    상태를 영수증에 병합한다.
    """
    receipt = _write(stack, source_id="src-generic-reject",
                     graph=_GraphThatRejectsTheWrite(RuntimeError("boom, unrelated failure")))

    assert receipt["stores"]["graph"].startswith("error:"), receipt["stores"]
    assert receipt["stores"]["docs"] == "skipped (graph write failed)", receipt["stores"]
    assert receipt["stores"]["sql"] == "skipped (graph write failed)", receipt["stores"]
    assert receipt["stores"]["documents"] == "skipped (graph write failed)"
    assert receipt["stores"]["chromadb"] == "skipped (graph write failed)"
    assert stack["docs"].get_source("src-generic-reject") is None
    assert stack["vector"].get_by_id("src-generic-reject") is None
    assert stack["docs"].get_node_doc("evidence", "src-generic-reject") is None
    assert not _sql_registry_has(stack, "src-generic-reject")


def test_a_probe_exception_on_a_normal_length_id_leaves_all_four_axes_empty(stack):
    """설계 §9-3. 예산 초과 id 의 프로브 예외(§9-12f)와는 다른 축이다 — 이 id 는

    카브아웃 대상조차 아닌 보통 길이라서, 카브아웃 로직을 전혀 거치지 않고
    바로 `node_identity_conflict` 의 `CONFLICT_UNVERIFIABLE` fail-closed
    거절로 들어가야 한다. 거절 뒤 doc_sources·벡터·doc_nodes·sql 레지스트리
    네 축 모두 비어 있어야 한다.
    """
    with pytest.raises(ValueError, match="cannot verify"):
        _write(stack, source_id="src-normal-probe-raises", graph=_ProbeRaises())

    assert stack["docs"].get_source("src-normal-probe-raises") is None
    assert stack["vector"].get_by_id("src-normal-probe-raises") is None
    assert stack["docs"].get_node_doc("evidence", "src-normal-probe-raises") is None
    assert not _sql_registry_has(stack, "src-normal-probe-raises")


class _UnavailableWithSchemaStateDistractor:
    """`available=False` 지만 `schema_state` 라는 미끼 속성을 함께 들고 있다.

    프로덕션이 `.available` 이 아니라 이 속성을 들여다보는 변이라면 이
    더블은 기존 테스트 2 와 다른 결과를 낸다.
    """

    available = False
    schema_state = "partial_or_unknown"

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        raise RuntimeError("graph store is not available")


def test_schema_state_distractor_does_not_change_the_unavailable_disposition(stack):
    """`.available` 만 본다는 계약을 고정한다 — 미끼 속성이 판정을 바꾸면 안 된다."""
    receipt = _write(stack, source_id="src-schema-distractor",
                     graph=_UnavailableWithSchemaStateDistractor())

    assert receipt["stores"]["graph"] == "unavailable", receipt["stores"]
    assert receipt["stores"]["documents"] == "skipped (graph write failed)", receipt["stores"]
    assert receipt["stores"]["chromadb"] == "skipped (graph write failed)", receipt["stores"]
    assert stack["docs"].get_source("src-schema-distractor") is None
    assert stack["vector"].get_by_id("src-schema-distractor") is None


# ---------------------------------------------------------------------------
# 설계 §9 테스트 5b — doc_nodes 에만 남의 행이 있어도 거절한다
# ---------------------------------------------------------------------------


def test_a_doc_nodes_only_foreign_row_is_refused_before_any_write(stack):
    """그래프에는 아무것도 없고 doc_nodes 에만 남의 팩 행이 있는 경우.

    `node_identity_conflict` 는 `docs.get_node_doc` 도 probe 한다. 그 probe 가
    빠지면 이 케이스가 그냥 통과해 버려 doc_nodes 축의 신원 보호가 무력화된다.
    """
    stack["docs"].upsert_node_doc(
        "evidence", "TextUnit", "src-5b", {"pack_id": "pack-b", "text": "남의 것"},
    )
    assert stack["graph"].get_nodes_by_id("src-5b") == []

    with pytest.raises(ValueError, match="already attributed"):
        _write(stack, source_id="src-5b")

    assert stack["docs"].get_source("src-5b") is None
    assert stack["vector"].get_by_id("src-5b") is None
    assert _node(stack, "src-5b") is None


# ---------------------------------------------------------------------------
# 설계 §9 테스트 6 — 같은 id, 다른 node_type
# ---------------------------------------------------------------------------


def test_same_id_different_node_type_is_a_node_identity_conflict(stack):
    """`classify_by_id_rows` 는 같은 팩 소유 행을 타입과 무관하게 "own" 으로 본다.

    그래서 이 케이스를 막는 것은 `node_identity_conflict` 의 ValueError 경로가
    아니라 저장 계층 자체의 digest 불일치 검사(`upsert_node`)다 — 여기서
    잡히는 예외 타입이 `NodeIdentityConflict` 인지를 고정한다.
    """
    with principal_scope(ALICE):
        stack["builder"].add_node(
            space="evidence", node_type="Evidence", node_id="src-typeconflict",
            properties={"pack_id": "pack-a", "text": "증거"}, pack_id="pack-a",
        )
    # 시딩 자체가 (builder.add_node 경유로) 이 id 의 벡터 행을 이미 만든다 —
    # write_source 와는 무관한 부작용이므로 write_source 시도 전후로 그 행이
    # 달라지지 않았는지만 본다("아무것도 없다"가 아니라 "손대지 않았다").
    seeded_vector = stack["vector"].get_by_id("src-typeconflict")
    assert seeded_vector is not None

    with pytest.raises(NodeIdentityConflict):
        _write(stack, source_id="src-typeconflict")

    assert stack["docs"].get_source("src-typeconflict") is None
    assert stack["vector"].get_by_id("src-typeconflict") == seeded_vector


# ---------------------------------------------------------------------------
# 설계 §9 테스트 7 — 두 번째 authorize 호출의 예외는 흡수되지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raiser",
    [
        lambda *a, **kw: (_ for _ in ()).throw(PackForbiddenError("forbidden")),
        lambda *a, **kw: (_ for _ in ()).throw(PackNotFoundError("not found")),
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("pack registry unavailable; refusing the write "
                         "(ownership cannot be verified)")
        ),
    ],
    ids=["forbidden", "not-found", "registry-unavailable"],
)
def test_builder_internal_authorize_failure_is_never_absorbed(stack, monkeypatch, raiser):
    """`write_source` 자신의 첫 authorize 는 통과시키고, `builder.add_node` 내부의

    두 번째(별도로 바인딩된) `authorize` 만 실패시킨다. 세 예외 모두 어느
    except 절에도 걸리지 않으므로 `_write` 를 그대로 뚫고 나와야 한다 — 걸리면
    권한 실패가 조용히 그래프 오류로 둔갑한다.
    """
    monkeypatch.setattr("opencrab.ontology.builder.authorize", raiser)

    with pytest.raises((PackForbiddenError, PackNotFoundError, RuntimeError)):
        _write(stack, source_id="src-authz-7")

    assert stack["docs"].get_source("src-authz-7") is None
    assert stack["vector"].get_by_id("src-authz-7") is None
    assert _node(stack, "src-authz-7") is None


# ---------------------------------------------------------------------------
# 설계 §9 테스트 8b — CAS 토큰 읽기 자체가 실패한다
# ---------------------------------------------------------------------------


def test_get_node_digest_failure_rejects_a_changed_content_reingest(stack, monkeypatch):
    """`get_node_digest` 가 던지면 `_node_digest` 는 `None` 을 돌려주고

    `add_node` 는 CAS 갱신이 아니라 평문 삽입 경로를 탄다. 그 경로 자체의
    digest 비교가 내용이 바뀐 재적재를 `NodeIdentityConflict` 로 거절한다.
    doc_sources/벡터는 옛 텍스트에 머물러야 한다.
    """
    _write(stack, text="원본", source_id="src-8b")

    def _raiser(self, *a, **kw):  # noqa: ARG001
        raise RuntimeError("digest read failed")

    monkeypatch.setattr(type(stack["graph"]), "get_node_digest", _raiser, raising=True)

    with pytest.raises(NodeIdentityConflict):
        _write(stack, text="바뀐 내용", source_id="src-8b")

    assert stack["docs"].get_source("src-8b")["text"] == "원본"
    assert stack["vector"].get_by_id("src-8b")["document"] == "원본"
    assert _node(stack, "src-8b")["text"] == "원본"


def test_get_node_digest_failure_still_allows_same_content_reingest(stack, monkeypatch):
    """같은 내용이면 평문 삽입 경로의 digest 비교가 일치해 조용히 통과해야 한다.

    CAS 실패를 무조건 거절로 바꾸는 변이는 이 케이스에서만 드러난다.
    """
    _write(stack, text="같음", source_id="src-8b-same")

    def _raiser(self, *a, **kw):  # noqa: ARG001
        raise RuntimeError("digest read failed")

    monkeypatch.setattr(type(stack["graph"]), "get_node_digest", _raiser, raising=True)

    receipt = _write(stack, text="같음", source_id="src-8b-same")
    assert receipt["stores"]["graph"] == "ok", receipt["stores"]
    assert _node(stack, "src-8b-same")["text"] == "같음"


# ---------------------------------------------------------------------------
# 설계 §9 테스트 9 — reclassify_node(CAS 갱신)이 거절한다
# ---------------------------------------------------------------------------


def test_reclassify_node_conflict_propagates_and_leaves_old_values(stack, monkeypatch):
    """CAS 갱신 경로 자체가 `NodeIdentityConflict` 를 던지면(레이스 등) 그대로

    전파돼야 한다. 흡수하면 이 이슈가 막으려던 발산(doc/벡터는 새 값, 그래프는
    옛 값)이 그대로 재현된다.
    """
    _write(stack, text="첫 본문", source_id="src-9")

    def _raiser(self, *a, **kw):  # noqa: ARG001
        raise NodeIdentityConflict("race lost")

    monkeypatch.setattr(type(stack["graph"]), "reclassify_node", _raiser, raising=True)

    with pytest.raises(NodeIdentityConflict):
        _write(stack, text="둘째 본문", source_id="src-9")

    assert stack["docs"].get_source("src-9")["text"] == "첫 본문"
    assert stack["vector"].get_by_id("src-9")["document"] == "첫 본문"
    assert _node(stack, "src-9")["text"] == "첫 본문"


# ---------------------------------------------------------------------------
# 설계 §9 테스트 11 — 그래프는 성공, doc_sources 는 실패
# ---------------------------------------------------------------------------


def test_doc_sources_failure_after_graph_success_reports_partial_receipt(stack, monkeypatch):
    """그래프가 새 텍스트로 갱신된 뒤 doc_sources 쓰기가 실패하면

    영수증은 `graph: ok` + `documents: error: ...` + `chromadb: skipped
    (source record failed)` 를 내야 하고, doc_sources/벡터의 실제 값은 옛
    텍스트에 머물러야 한다 — 그래프만 갱신되고 나머지는 멈춘 상태를
    영수증이 정확히 반영하는지 본다.
    """
    _write(stack, text="옛 본문", source_id="src-11")

    def _raiser(self, *a, **kw):  # noqa: ARG001
        raise RuntimeError("doc_sources write failed")

    monkeypatch.setattr(type(stack["docs"]), "upsert_source", _raiser, raising=True)

    receipt = _write(stack, text="새 본문", source_id="src-11")

    assert receipt["stores"]["graph"] == "ok", receipt["stores"]
    assert receipt["stores"]["documents"].startswith("error:"), receipt["stores"]
    assert receipt["stores"]["chromadb"] == "skipped (source record failed)", receipt["stores"]

    assert _node(stack, "src-11")["text"] == "새 본문"
    assert stack["docs"].get_source("src-11")["text"] == "옛 본문"
    assert stack["vector"].get_by_id("src-11")["document"] == "옛 본문"


# ---------------------------------------------------------------------------
# 설계 §9 테스트 12c — 예산 초과 id 에서 신원 가드와 카브아웃의 상호작용
# ---------------------------------------------------------------------------


def test_12c_over_budget_id_graph_only_foreign_node_is_refused(stack):
    """(a) 예산을 넘는 id 라도 그래프에 남의 노드가 이미 있으면 카브아웃보다

    신원 가드가 먼저다: `rows == []` 가 아니므로(그래프 프로브 결과가 실제로
    비어있지 않으므로) 카브아웃이 적용되지 않고 `node_identity_conflict` 의
    그래프 프로브가 거절한다.
    """
    over = "E" * (_budget() + 1)
    stack["graph"].upsert_node(
        node_type="TextUnit", node_id=over,
        properties={"pack_id": "pack-b", "text": "남의 것"}, space_id="evidence",
    )

    with pytest.raises(ValueError, match="already attributed"):
        _write(stack, source_id=over)

    assert stack["docs"].get_source(over) is None
    assert stack["vector"].get_by_id(over) is None


def test_12c_over_budget_id_same_pack_different_type_is_a_node_identity_conflict(stack):
    """(b) 예산을 넘는 id 에 같은 팩 소유의 다른 node_type 행이 있으면

    `node_identity_conflict` 자신의 검사는 전부 통과한다(같은 팩이므로 by-id
    축이 "own"). 저장 계층의 digest 불일치 검사가 대신 거절한다 — (b)는
    카브아웃도 적용되지 않는다는 것까지 함께 고정한다(`rows` 가 빈 리스트가
    아니므로).
    """
    over = "F" * (_budget() + 1)
    with principal_scope(ALICE):
        stack["builder"].add_node(
            space="evidence", node_type="Evidence", node_id=over,
            properties={"pack_id": "pack-a", "text": "증거"}, pack_id="pack-a",
        )

    with pytest.raises(NodeIdentityConflict):
        _write(stack, source_id=over)

    assert stack["docs"].get_source(over) is None


def test_12c_over_budget_id_doc_nodes_only_foreign_row_succeeds_via_carve_out(stack):
    """(c) 예산을 넘는 id 인데 doc_nodes 에만 남의 팩 행이 있고 그래프는 비어있으면,

    카브아웃(`_existing_node_rows` 가 `[]` 를 돌려줌)이 먼저 적용돼 그래프
    노드를 아예 만들지 않는다. `node_identity_conflict`/`builder.add_node` 는
    호출조차 되지 않으므로 doc_nodes 의 이 남의 행은 전혀 프로브되지 않고
    적재가 성공한다 — 카브아웃이 그래프만 보고 doc_nodes 를 보지 않는다는
    것의 직접적 증거다.
    """
    over = "G" * (_budget() + 1)
    stack["docs"].upsert_node_doc(
        "evidence", "TextUnit", over, {"pack_id": "pack-b", "text": "남의 것"},
    )
    assert stack["graph"].get_nodes_by_id(over) == []

    receipt = _write(stack, source_id=over)

    assert receipt["stores"]["graph"] == (
        "skipped (id exceeds the node id limit after fork remap)"
    ), receipt["stores"]
    assert receipt["stores"]["documents"].startswith("ok"), receipt["stores"]
    assert receipt["stores"]["chromadb"].startswith("ok"), receipt["stores"]
    assert stack["vector"].get_by_id(over) is not None
    assert stack["graph"].get_nodes_by_id(over) == []


# ---------------------------------------------------------------------------
# 설계 §9 테스트 12d — 반복 fork 의 비단조성(문서화된 한계, 수정 대상 아님)
# ---------------------------------------------------------------------------


def _fork(stack, *, principal, src_pack_id, **kw) -> dict[str, Any]:
    with principal_scope(principal):
        return fork_pack(
            stack["sql"], stack["graph"], stack["docs"], stack["vector"],
            stack["hybrid"], stack["builder"],
            principal=principal, src_pack_id=src_pack_id, **kw,
        )


def test_12d_budget_exact_source_chained_fork_eventually_rejects(stack):
    """예산과 같은 길이의 소스는 노드가 된다. 그 노드 id 는 첫 fork 에서 리맵

    접미사가 붙어 늘어나고, 두 번째(체이닝된) fork 에서 다시 늘어나 등록부의
    node_id 한계를 넘는다. 이것은 §9 가 명시적으로 문서화하는 한계다 — fork 의
    id 재매핑 형상 자체를 바꿔야 해결되므로 이 이슈의 범위 밖이고, 이
    테스트는 "고쳐졌다"가 아니라 "이 모양대로 계속 동작한다"를 고정한다.
    """
    at_limit = "H" * _budget()
    _write(stack, source_id=at_limit)
    assert _node(stack, at_limit) is not None

    first = _fork(stack, principal=ALICE, src_pack_id="pack-a")
    assert first.get("status") == "ok", first

    second = _fork(stack, principal=ALICE, src_pack_id=first["pack_id"])
    assert "error" in second, second
    assert at_limit in second["error"] or "node_id" in second["error"].lower() \
        or "limit" in second["error"].lower(), second


def test_12d_one_char_over_budget_source_survives_repeated_fork(stack):
    """예산보다 한 글자 많은 소스는 노드가 되지 않고 소스 축에만 머문다.

    노드가 없으니 fork 의 노드 길이 검사에 걸릴 것이 없어 반복 fork 가
    계속 성공해야 한다 — 12d 의 대조군.
    """
    over = "I" * (_budget() + 1)
    _write(stack, source_id=over)
    assert stack["graph"].get_nodes_by_id(over) == []

    first = _fork(stack, principal=ALICE, src_pack_id="pack-a")
    assert first.get("status") == "ok", first

    second = _fork(stack, principal=ALICE, src_pack_id=first["pack_id"])
    assert second.get("status") == "ok", second


# ---------------------------------------------------------------------------
# REST / CLI / MCP 진입점 공용 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture()
def rest_env(tmp_path, monkeypatch):
    """REST/CLI 진입점을 위한 별도 프로세스 환경 — `stack` 과 달리 env var 로

    settings 를 구성해야 REST/CLI 자신의 `get_settings()` 가 이 값을 본다.
    """
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def rest_sql(rest_env):
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    return make_sql_store(get_settings())


@pytest.fixture()
def rest_principal(rest_sql):
    from opencrab.auth import bootstrap_local_user

    return bootstrap_local_user(rest_sql)


@pytest.fixture()
def rest_auth(rest_principal):
    _user_id, secret = rest_principal
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture()
def rest_module(rest_env):
    """apps/api/main.py 를 파일 경로로 로드한다(apps 는 패키지가 아니라 import 불가).

    test_service_paths_characterization.py 의 `api_module` 과 같은 이름 충돌을
    피하려고 모듈명을 다르게 등록한다.
    """
    import importlib.util
    import pathlib
    import sys as _sys

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "api_main_issue74_parity", repo_root / "apps" / "api" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rest_client(rest_env, rest_module, monkeypatch):
    monkeypatch.setenv("OPENCRAB_TIER", "free")
    from fastapi.testclient import TestClient

    with TestClient(rest_module.app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture()
def cli74_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def cli74_bootstrapped(cli74_env):
    from opencrab.auth import bootstrap_local_user
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    user_id, _secret = bootstrap_local_user(make_sql_store(get_settings()))
    return user_id


@pytest.fixture()
def mcp_ctx(stack):
    """`opencrab.mcp.tools._get_context()` 와 같은 모양의 실 로컬 ctx.

    `stack` 을 그대로 재사용해 ALICE/pack-a 시딩과 동일한 데이터 위에서
    MCP 경로를 테스트한다.
    """
    from opencrab.billing.hooks import BillingHooks
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.rebac import ReBACEngine

    g = stack["graph"]
    s = stack["sql"]
    rebac = ReBACEngine(g, s)
    stack["hybrid"]._rebac = rebac
    return {
        "neo4j": g,
        "chroma": stack["vector"],
        "mongo": stack["docs"],
        "sql": s,
        "builder": stack["builder"],
        "rebac": rebac,
        "impact": ImpactEngine(g, s),
        "hybrid": stack["hybrid"],
        "billing": BillingHooks(s),
    }


# ---------------------------------------------------------------------------
# 설계 §9 테스트 1 (계속) — REST / CLI 진입점도 write_source 초크포인트를 지난다
# ---------------------------------------------------------------------------


def test_rest_ingest_materialises_graph_node_and_all_store_rows(
    rest_client, rest_module, rest_auth, rest_principal,
):
    """REST `/api/ingest` 가 write_source 를 우회하는 변이라면 이 이슈의 결함이

    REST 경로에서만 되살아난다 — 그래프 노드 없이 doc/벡터만 쌓인다.
    """
    resp = rest_client.post(
        "/api/ingest",
        json={
            "text": "REST 본문", "source_id": "src-rest-1",
            "metadata": {"title": "REST 제목", "source": "REST 출처"},
        },
        headers=rest_auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stores"]["graph"] == "ok", body["stores"]

    ctx = rest_module.app.state.context
    node = ctx.graph.get_node("TextUnit", "src-rest-1")
    assert node is not None, "REST 로 적재한 소스가 그래프에 노드로 나타나지 않는다"
    assert node["text"] == "REST 본문"
    assert node["title"] == "REST 제목"
    assert node["source"] == "REST 출처"

    assert ctx.docs.get_source("src-rest-1") is not None
    assert ctx.vector.get_by_id("src-rest-1") is not None
    # 설계 §9-1: doc_nodes 축(빌더가 쓰는 슬롯, 이 위의 doc_sources 와는 다른
    # 슬롯)도 확인해야 한다. doc_sources 만 보면 그래프 다리가 doc_nodes 를
    # 남기지 않는 변이를 놓친다.
    assert ctx.docs.get_node_doc("evidence", "src-rest-1") is not None
    with ctx.sql._engine.begin() as conn:
        row = conn.execute(
            _sql_text("SELECT node_id FROM ontology_nodes WHERE node_id=:n"),
            {"n": "src-rest-1"},
        ).fetchone()
    assert row is not None, "sql 레지스트리에 노드 행이 없다"

    # 설계 §9-1(b): `ontology_list_nodes(pack_id=...)` 축에서도 보여야 한다.
    # get_node 단일조회만 통과하고 목록 조회 경로(export_nodes_scoped 등)가
    # 빠진 변이는 위 단언들을 다 통과하고도 여기서 잡힌다.
    from opencrab.mcp.tools.graph import ontology_list_nodes
    from opencrab.pack.ownership import resolve_write_pack

    user_id, _secret = rest_principal
    rest_user = Principal(user_id=user_id, is_local=True, disabled=False)
    default_pack = resolve_write_pack(ctx.sql, rest_user, None)
    list_ctx = {"neo4j": ctx.graph, "mongo": ctx.docs, "sql": ctx.sql}
    with principal_scope(rest_user), patch("opencrab.mcp.tools._get_context", return_value=list_ctx):
        listing = ontology_list_nodes(pack_id=default_pack)
    assert any(n["node_id"] == "src-rest-1" for n in listing["nodes"]), listing


def test_cli_ingest_materialises_graph_node_and_all_store_rows(cli74_bootstrapped, tmp_path):
    """CLI `ingest` 가 write_source 를 우회하는 변이라면 이 이슈의 결함이

    CLI 경로에서만 되살아난다.
    """
    src = tmp_path / "doc.txt"
    src.write_text("CLI 본문", encoding="utf-8")

    result = CliRunner().invoke(_cli_main, ["ingest", str(src)])
    assert result.exit_code == 0, result.output
    assert "Ingested 1/1 files." in result.output, result.output

    from opencrab.config import get_settings
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    cfg = get_settings()
    graph = make_graph_store(cfg)
    docs = make_doc_store(cfg)
    sql = make_sql_store(cfg)
    vector = make_vector_store(cfg)
    try:
        source_id = str(src.resolve())
        node = graph.get_node("TextUnit", source_id)
        assert node is not None, "CLI 로 적재한 소스가 그래프에 노드로 나타나지 않는다"
        assert node["text"] == "CLI 본문"
        assert docs.get_source(source_id) is not None
        assert vector.get_by_id(source_id) is not None
        # 설계 §9-1: doc_nodes 축도 확인한다(doc_sources 와는 다른 슬롯).
        assert docs.get_node_doc("evidence", source_id) is not None
        with sql._engine.begin() as conn:
            row = conn.execute(
                _sql_text("SELECT node_id FROM ontology_nodes WHERE node_id=:n"),
                {"n": source_id},
            ).fetchone()
        assert row is not None

        # 설계 §9-1(b): `ontology_list_nodes(pack_id=...)` 축에서도 보여야 한다.
        from opencrab.mcp.tools.graph import ontology_list_nodes
        from opencrab.pack.ownership import resolve_write_pack

        cli_principal = Principal(user_id=cli74_bootstrapped, is_local=True, disabled=False)
        default_pack = resolve_write_pack(sql, cli_principal, None)
        list_ctx = {"neo4j": graph, "mongo": docs, "sql": sql}
        with principal_scope(cli_principal), \
             patch("opencrab.mcp.tools._get_context", return_value=list_ctx):
            listing = ontology_list_nodes(pack_id=default_pack)
        assert any(n["node_id"] == source_id for n in listing["nodes"]), listing
    finally:
        graph.close()
        docs.close()
        vector.close()
        sql._engine.dispose()


def test_mcp_legacy_ingest_materialises_graph_node(mcp_ctx):
    """MCP `pack_ingest text_as_node=False`(레거시)도 같은 초크포인트를 지난다."""
    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx):
        result = _ingest_into_pack(
            "pack-a", text="MCP 본문", source_id="src-mcp-1",
            metadata={"title": "MCP 제목"}, text_as_node=False,
        )

    assert result["status"] == "ok", result
    node = mcp_ctx["neo4j"].get_node("TextUnit", "src-mcp-1")
    assert node is not None
    assert node["text"] == "MCP 본문"
    assert node["title"] == "MCP 제목"

    # 설계 §9-1: doc_sources·doc_nodes·sql 레지스트리·벡터 네 축 모두 확인한다.
    assert mcp_ctx["mongo"].get_source("src-mcp-1") is not None
    assert mcp_ctx["mongo"].get_node_doc("evidence", "src-mcp-1") is not None
    assert mcp_ctx["chroma"].get_by_id("src-mcp-1") is not None
    assert _sql_registry_has({"sql": mcp_ctx["sql"]}, "src-mcp-1")

    # 설계 §9-1(b): `ontology_list_nodes(pack_id=...)` 축에서도 보여야 한다.
    from opencrab.mcp.tools.graph import ontology_list_nodes

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx):
        listing = ontology_list_nodes(pack_id="pack-a")
    assert any(n["node_id"] == "src-mcp-1" for n in listing["nodes"]), listing


# ---------------------------------------------------------------------------
# 설계 §9 테스트 4 — 그래프 쓰기 거절이 세 진입점에서 어떻게 드러나는가
# ---------------------------------------------------------------------------


def test_rest_ingest_reports_failure_receipt_when_graph_write_is_rejected(
    rest_client, rest_module, rest_auth,
):
    """그래프 다리가 거절돼도 REST 는 200 을 유지하되 실패 영수증을 낸다.

    500 으로 새거나 성공으로 둔갑하면 클라이언트가 실패를 알 방법이 없다.
    """
    ctx = rest_module.app.state.context
    ctx.graph = _GraphThatRejectsTheWrite(GraphWriteUnavailable("down for maintenance"))

    resp = rest_client.post(
        "/api/ingest",
        json={"text": "본문", "source_id": "src-rest-fail"},
        headers=rest_auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stores"]["graph"].startswith("error:"), body["stores"]
    assert body["stores"]["documents"] == "skipped (graph write failed)", body["stores"]
    assert body["stores"]["chromadb"] == "skipped (graph write failed)", body["stores"]
    # 설계 §9-2/§9-4: 리셉트만이 아니라 doc_sources·벡터에 실제로 아무것도
    # 없는지도 확인한다.
    assert ctx.docs.get_source("src-rest-fail") is None
    assert ctx.vector.get_by_id("src-rest-fail") is None


def test_cli_ingest_reports_fail_and_zero_successes_when_graph_write_is_rejected(
    cli74_bootstrapped, tmp_path, monkeypatch,
):
    """CLI 는 doc_status 가 `"skipped (graph write failed)"` 라 doc_ok=False 가

    돼 `RuntimeError` 를 던지고, 그 파일은 FAIL 로 집계돼야 한다 — 조용히
    OK 로 세면 그래프 없는 반쪽짜리 적재가 성공으로 보고된다.
    """
    from opencrab.stores import factory as _factory

    double = _GraphThatRejectsTheWrite(GraphWriteUnavailable("down for maintenance"))
    monkeypatch.setattr(_factory, "make_graph_store", lambda cfg: double)  # noqa: ARG005

    src = tmp_path / "doc.txt"
    src.write_text("CLI 본문", encoding="utf-8")

    result = CliRunner().invoke(_cli_main, ["ingest", str(src)])
    # #189: 그래프 쓰기 거부로 파일이 FAIL 로 집계되므로 exit code 는 3 이어야 한다.
    assert result.exit_code == 3, result.output
    assert "Ingested 0/1 files." in result.output, result.output
    assert "FAIL" in result.output, result.output

    # 설계 §9-2/§9-4: doc_sources·벡터에 실제로 아무것도 없는지 확인한다.
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_doc_store, make_vector_store

    cfg = get_settings()
    docs = make_doc_store(cfg)
    vector = make_vector_store(cfg)
    try:
        source_id = str(src.resolve())
        assert docs.get_source(source_id) is None
        assert vector.get_by_id(source_id) is None
    finally:
        docs.close()
        vector.close()


def test_mcp_legacy_ingest_reports_partial_when_graph_write_is_rejected(stack):
    """MCP 레거시는 이 네 타입을 흡수해 `node_errors` 로 담으므로 `status="partial"` 이어야 한다."""
    from opencrab.billing.hooks import BillingHooks
    from opencrab.mcp.tools import _ingest_into_pack
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.rebac import ReBACEngine

    double_graph = _GraphThatRejectsTheWrite(GraphWriteUnavailable("down for maintenance"))
    ctx = {
        "neo4j": double_graph,
        "chroma": stack["vector"],
        "mongo": stack["docs"],
        "sql": stack["sql"],
        "builder": OntologyBuilder(double_graph, stack["docs"], stack["sql"], vec=stack["vector"]),
        "rebac": ReBACEngine(double_graph, stack["sql"]),
        "impact": ImpactEngine(double_graph, stack["sql"]),
        "hybrid": stack["hybrid"],
        "billing": BillingHooks(stack["sql"]),
    }

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "pack-a", text="본문", source_id="src-mcp-fail", text_as_node=False,
        )

    assert result["status"] == "partial", result
    # 설계 §9-16: 그래프 노드가 안착하지 않았으므로 카운터도 0/None 이어야
    # 한다. `status` 만 보면 그래프 실패에서도 노드가 생겼다고 우기는
    # 카운터 구현이 통과한다.
    assert result.get("added_nodes") == 0, result
    assert result.get("evidence_node") is None, result


# ---------------------------------------------------------------------------
# 설계 §9 테스트 14·16·16b — MCP 레거시의 청구/added_nodes 신호
# ---------------------------------------------------------------------------


def _mcp_ctx_with_billing_spy(stack):
    """`mcp_ctx` 와 같은 모양이되 billing 만 스파이로 바꾼다.

    billing 은 그래프 스토어가 아니므로 MagicMock 사용이 코드베이스 관례와
    합치한다.
    """
    from unittest.mock import MagicMock

    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.rebac import ReBACEngine

    g = stack["graph"]
    s = stack["sql"]
    rebac = ReBACEngine(g, s)
    stack["hybrid"]._rebac = rebac
    billing = MagicMock()
    billing.on_ingest.return_value = {"ok": True}
    return {
        "neo4j": g,
        "chroma": stack["vector"],
        "mongo": stack["docs"],
        "sql": s,
        "builder": stack["builder"],
        "rebac": rebac,
        "impact": ImpactEngine(g, s),
        "hybrid": stack["hybrid"],
        "billing": billing,
    }, billing


def test_14a_normal_legacy_ingest_bills_once(stack):
    """(a) 정상 레거시 적재는 청구가 정확히 1회 발생해야 한다."""
    from opencrab.mcp.tools import _ingest_into_pack

    ctx, billing = _mcp_ctx_with_billing_spy(stack)
    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "pack-a", text="본문", source_id="src-14a", text_as_node=False,
        )
    assert result["status"] == "ok", result
    assert billing.on_ingest.call_count == 1


def test_14b_graph_ok_documents_error_does_not_bill(stack, monkeypatch):
    """(b) 그래프는 성공했지만 doc_sources 가 실패하면 벡터도 skip 되므로

    `legacy_text_landed` 가 거짓이다 — 청구되면 안 된다.
    """
    from opencrab.mcp.tools import _ingest_into_pack

    ctx, billing = _mcp_ctx_with_billing_spy(stack)

    def _raiser(self, *a, **kw):  # noqa: ARG001
        raise RuntimeError("doc_sources write failed")

    monkeypatch.setattr(type(stack["docs"]), "upsert_source", _raiser, raising=True)

    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "pack-a", text="본문", source_id="src-14b", text_as_node=False,
        )
    assert result["status"] == "partial", result
    # 테스트 이름이 전제하는 "그래프는 성공했다"를 직접 단언한다 — 이것 없이는
    # 그래프 키가 아예 없는(수정 전) 상태에서도 통과해 이름이 거짓 전제를
    # 깔게 된다.
    assert result["stores"]["graph"] == "ok", result["stores"]
    assert result["stores"]["documents"].startswith("error:"), result["stores"]
    assert result["stores"]["chromadb"] == "skipped (source record failed)", result["stores"]
    billing.on_ingest.assert_not_called()


def test_14c_long_id_carve_out_but_documents_and_chromadb_succeed_bills(stack):
    """(c) 예산 초과 id 는 카브아웃으로 그래프 노드가 없어도

    doc_sources/벡터가 둘 다 성공하므로 `legacy_text_landed` 가 참 — 청구돼야
    한다.
    """
    from opencrab.mcp.tools import _ingest_into_pack

    ctx, billing = _mcp_ctx_with_billing_spy(stack)
    over = "J" * (_budget() + 1)

    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "pack-a", text="본문", source_id=over, text_as_node=False,
        )
    assert result["stores"]["graph"] == (
        "skipped (id exceeds the node id limit after fork remap)"
    ), result["stores"]
    assert result["stores"]["documents"].startswith("ok"), result["stores"]
    assert result["stores"]["chromadb"].startswith("ok"), result["stores"]
    billing.on_ingest.assert_called_once()


def test_14d_residue_only_ok_under_docs_sql_keys_does_not_bill(stack):
    """(d) 그래프가 흡수형 `RuntimeError` 로 실패하면 청구하지 않는다.

    #495 이전에는 doc_nodes/sql 레지스트리에 잔여(`docs`/`sql` 키의 "ok")가
    남았다. 지금은 그 다리를 건너뛴다. 어느 쪽이든 `documents`/`chromadb`
    키가 성공이 아니므로 `legacy_text_landed` 는 참이 되면 안 된다.
    """
    from unittest.mock import MagicMock

    from opencrab.mcp.tools import _ingest_into_pack
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.rebac import ReBACEngine

    double_graph = _GraphThatRejectsTheWrite(RuntimeError("boom, unrelated failure"))
    billing = MagicMock()
    billing.on_ingest.return_value = {"ok": True}
    ctx = {
        "neo4j": double_graph,
        "chroma": stack["vector"],
        "mongo": stack["docs"],
        "sql": stack["sql"],
        "builder": OntologyBuilder(double_graph, stack["docs"], stack["sql"], vec=stack["vector"]),
        "rebac": ReBACEngine(double_graph, stack["sql"]),
        "impact": ImpactEngine(double_graph, stack["sql"]),
        "hybrid": stack["hybrid"],
        "billing": billing,
    }

    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = _ingest_into_pack(
            "pack-a", text="본문", source_id="src-14d", text_as_node=False,
        )

    assert result["stores"].get("docs") == "skipped (graph write failed)", result["stores"]
    assert result["stores"].get("sql") == "skipped (graph write failed)", result["stores"]
    assert result["stores"]["documents"] == "skipped (graph write failed)", result["stores"]
    assert result["stores"]["chromadb"] == "skipped (graph write failed)", result["stores"]
    billing.on_ingest.assert_not_called()


def test_16_added_nodes_and_evidence_node_match_actual_writes_including_carve_out(stack):
    """정상 케이스와 예산 초과 카브아웃 케이스 모두에서 `added_nodes`/`evidence_node`

    가 실제로 만들어진 노드와 일치해야 한다. 카브아웃은 노드를 만들지 않으므로
    `added_nodes==0`, `evidence_node is None` 이어야 한다.
    """
    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        normal = _ingest_into_pack(
            "pack-a", text="본문", source_id="src-16-normal", text_as_node=False,
        )
    assert normal["added_nodes"] == 1, normal
    assert normal["evidence_node"] == "src-16-normal", normal

    over = "K" * (_budget() + 1)
    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        carved = _ingest_into_pack(
            "pack-a", text="본문", source_id=over, text_as_node=False,
        )
    assert carved["added_nodes"] == 0, carved
    assert carved["evidence_node"] is None, carved


def test_16b_a_single_store_error_zeroes_added_nodes_and_marks_partial(stack, monkeypatch):
    """`graph: ok` 인데 `documents`/`sql`/`documents`/`chromadb` 중 하나라도

    error 면 `added_nodes==0`, `evidence_node is None`, `status=="partial"` 이어야
    한다 — 그래프만 보고 added_nodes 를 올리면 부분 실패가 성공으로 보고된다.
    """
    from opencrab.mcp.tools import _ingest_into_pack

    def _raiser(self, *a, **kw):  # noqa: ARG001
        raise RuntimeError("doc_sources write failed")

    monkeypatch.setattr(type(stack["docs"]), "upsert_source", _raiser, raising=True)

    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        result = _ingest_into_pack(
            "pack-a", text="본문", source_id="src-16b", text_as_node=False,
        )
    # 테스트 이름이 전제하는 "그래프는 ok" 를 직접 단언한다 — 이것 없이는
    # 그래프 키가 아예 없는(수정 전) 상태에서도 통과해 이름이 거짓 전제를
    # 깔게 된다.
    assert result["stores"]["graph"] == "ok", result["stores"]
    assert result["added_nodes"] == 0, result
    assert result["evidence_node"] is None, result
    assert result["status"] == "partial", result


def mcp_ctx_from(stack):
    """`mcp_ctx` 픽스처와 동일한 ctx 를 픽스처 밖에서(같은 테스트 안 두 번 호출용)

    직접 구성한다 — 매 호출 새 ReBACEngine/ImpactEngine 인스턴스라 상태 공유
    걱정이 없다.
    """
    from opencrab.billing.hooks import BillingHooks
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.rebac import ReBACEngine

    g = stack["graph"]
    s = stack["sql"]
    rebac = ReBACEngine(g, s)
    stack["hybrid"]._rebac = rebac
    return {
        "neo4j": g,
        "chroma": stack["vector"],
        "mongo": stack["docs"],
        "sql": s,
        "builder": stack["builder"],
        "rebac": rebac,
        "impact": ImpactEngine(g, s),
        "hybrid": stack["hybrid"],
        "billing": BillingHooks(s),
    }


# ---------------------------------------------------------------------------
# 설계 §9 테스트 17 — MCP 레거시의 예외 흡수도 좁아야 한다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raiser",
    [
        lambda *a, **kw: (_ for _ in ()).throw(PackForbiddenError("forbidden")),
        lambda *a, **kw: (_ for _ in ()).throw(PackNotFoundError("not found")),
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("pack registry unavailable; refusing the write "
                         "(ownership cannot be verified)")
        ),
    ],
    ids=["forbidden", "not-found", "registry-unavailable"],
)
def test_17_mcp_legacy_narrow_catch_does_not_absorb_authorization_failures(
    stack, monkeypatch, raiser,
):
    """MCP 레거시의 except 절은 `(ValueError, NodeIdentityConflict,

    GraphSchemaMigrationRequired, GraphWriteUnavailable)` 로 좁다. 두 번째
    authorize 호출의 권한 예외가 여기 흡수돼 `status="partial"` 로 둔갑하면
    실패한 인가가 부분 성공으로 보고된다.
    """
    from opencrab.mcp.tools import _ingest_into_pack

    monkeypatch.setattr("opencrab.ontology.builder.authorize", raiser)

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)), \
         pytest.raises((PackForbiddenError, PackNotFoundError, RuntimeError)):
        _ingest_into_pack("pack-a", text="본문", source_id="src-17", text_as_node=False)


# ---------------------------------------------------------------------------
# 설계 §9 테스트 6 — 같은 id 다른 node_type 충돌의 REST/MCP 축
# ---------------------------------------------------------------------------


def test_6_rest_same_id_different_node_type_conflict_is_rejected(
    rest_client, rest_module, rest_auth, rest_principal,
):
    """REST 경유로도 같은 id·다른 node_type 재적재가 노드 정체성 충돌로

    거부돼야 한다 — REST 계층이 이 충돌을 흡수해 200 을 돌려주면 안 된다.
    시딩은 실제로 REST 호출이 쓰게 될 것과 동일한 principal/기본 팩 위에서
    해야 충돌이 실제로 성립한다 (다른 팩이면 foreign 로 갈려 다른 경로를
    탄다).
    """
    from opencrab.pack.ownership import resolve_write_pack

    user_id, _secret = rest_principal
    rest_user = Principal(user_id=user_id, is_local=True, disabled=False)
    ctx = rest_module.app.state.context
    default_pack = resolve_write_pack(ctx.sql, rest_user, None)
    seed_builder = OntologyBuilder(ctx.graph, ctx.docs, ctx.sql, vec=ctx.vector)
    with principal_scope(rest_user):
        seed_builder.add_node(
            space="evidence",
            node_type="Evidence",
            node_id="src-6-rest",
            properties={"pack_id": default_pack, "text": "증거"},
            pack_id=default_pack,
        )
    # 시딩 자체가 벡터 행을 만든다(add_node 의 write_vector 기본값). 거부된
    # REST 재적재가 이 행을 건드리지 않았는지 보려면 절대적 부재가 아니라
    # 시딩 시점 값과의 불변을 비교해야 한다.
    seeded_vector = ctx.vector.get_by_id("src-6-rest")
    assert seeded_vector is not None
    resp = rest_client.post(
        "/api/ingest",
        json={"text": "본문", "source_id": "src-6-rest"},
        headers=rest_auth,
    )
    # 설계 §9-6 은 422 를 요구한다(수정 전에는 500). `except (ValueError,
    # NodeIdentityConflict)` 를 `except ValueError` 로 되돌리는 역변이는
    # `resp.status_code >= 400` 만으로는 잡히지 않는다 — 500 도 >= 400 이다.
    assert resp.status_code == 422, resp.text
    assert ctx.docs.get_source("src-6-rest") is None
    # 설계 §9-6 은 doc_sources 뿐 아니라 벡터 행도 거부된 재적재로 새로
    # 생기거나 바뀌지 않았는지 요구한다 — 시딩된 값 그대로여야 한다.
    assert ctx.vector.get_by_id("src-6-rest") == seeded_vector


def test_6_mcp_same_id_different_node_type_conflict_is_reported_as_partial(stack):
    """MCP 레거시 경유로도 같은 id·다른 node_type 재적재는 `status="partial"` 로

    보고돼야 한다 — 예외가 흡수돼 `status="ok"` 로 둔갑하면 안 된다.
    """
    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE):
        stack["builder"].add_node(
            space="evidence",
            node_type="Evidence",
            node_id="src-6-mcp",
            properties={"pack_id": "pack-a", "text": "증거"},
            pack_id="pack-a",
        )
    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        result = _ingest_into_pack(
            "pack-a", text="본문", source_id="src-6-mcp", text_as_node=False,
        )
    assert result["status"] == "partial", result
    assert result["added_nodes"] == 0, result
    assert result["evidence_node"] is None, result
    assert stack["docs"].get_source("src-6-mcp") is None


# ---------------------------------------------------------------------------
# 이슈 #468: 같은 id 가 다른 노드를 가리키는 거절은 불투명 오류가 아니라 항목 거절로 보고된다
# ---------------------------------------------------------------------------

_CONFLICT_FIXED = "node id already names a different node; this item was not written"
_CONFLICT_MARKER = "rejected (node identity conflict)"


def _seed_logentry(stack, node_id: str) -> dict[str, Any]:
    """팩 로더가 만드는 것과 같은 모양의 LogEntry 를 같은 팩에 먼저 심는다."""
    with principal_scope(ALICE):
        stack["builder"].add_node(
            space="evidence", node_type="LogEntry", node_id=node_id,
            properties={"pack_id": "pack-a", "session_id": "s-468"},
            pack_id="pack-a",
        )
    return stack["graph"].get_node("LogEntry", node_id)


def _ingest468(stack, **kw):
    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        return _ingest_into_pack("pack-a", **kw)


def _seed_node(stack, node_type: str, node_id: str, *, pack_id="pack-a", space="evidence",
               principal=ALICE, **props) -> dict[str, Any]:
    with principal_scope(principal):
        stack["builder"].add_node(
            space=space, node_type=node_type, node_id=node_id,
            properties={"pack_id": pack_id, **props}, pack_id=pack_id,
        )
    return stack["graph"].get_node(node_type, node_id)


def _snapshot(stack, node_type: str, node_id: str):
    """Graph row, doc row and vector row of one node, for before/after equality."""
    return (
        stack["graph"].get_node(node_type, node_id),
        stack["docs"].get_node_doc("evidence", node_id),
        stack["vector"].get_by_id(node_id),
    )


def _legs(stack, question: str, pack_id: str = "pack-a"):
    """Node ids returned by the BM25 leg and by the vector leg."""
    h = stack["hybrid"]
    bm25 = {hit.get("node_id") for hit in h._bm25_search(question, None, 10, pack_ids=[pack_id])}
    vec = {hit.node_id for hit in h._vector_search(question, None, 10, pack_ids=[pack_id])}
    return bm25, vec


_DERIVED = "#text"


def _slot2(source_id: str, text: str) -> str:
    """The #483 slot 2 id, computed independently of the code under test."""
    import hashlib

    return source_id + "#text-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def test_468_text_over_existing_logentry_is_stored_under_a_derived_id(stack, caplog):
    seeded_row = _seed_logentry(stack, "codex/s/468-a")
    before = _snapshot(stack, "LogEntry", "codex/s/468-a")
    derived = "codex/s/468-a" + _DERIVED

    with caplog.at_level("ERROR"):
        result = _ingest468(
            stack,
            text="zebrafish 대화 원문",
            source_id="codex/s/468-a",
            metadata={"title": "대화 제목", "source": "pack_ingest"},
            text_as_node=True,
        )

    assert result["status"] == "ok", result
    assert result["node_errors"] == [], result
    assert result["evidence_node"] == derived, result
    assert result["added_nodes"] == 1, result
    assert result["text_ingested"] is True, result
    assert result["stores"]["evidence_node"] == "ok (derived id for codex/s/468-a)", result
    # The LogEntry is not touched in any store.
    assert _snapshot(stack, "LogEntry", "codex/s/468-a") == before
    assert stack["graph"].get_node("LogEntry", "codex/s/468-a") == seeded_row
    assert stack["graph"].get_node("TextUnit", "codex/s/468-a") is None
    assert stack["docs"].get_source("codex/s/468-a") is None
    # The text sits in a new TextUnit that points back to the LogEntry.
    node = stack["graph"].get_node("TextUnit", derived)
    assert node["text"] == "zebrafish 대화 원문"
    assert node["source_id"] == "codex/s/468-a"
    assert node["title"] == "대화 제목" and node["source"] == "pack_ingest"
    assert node["pack_id"] == "pack-a" and node["owner_id"] == ALICE.user_id
    # The text is searchable by both legs under the derived id.
    bm25, vec = _legs(stack, "zebrafish")
    assert derived in bm25, bm25
    assert derived in vec, vec
    assert not [r for r in caplog.records if r.exc_info]


def test_468_same_request_twice_is_idempotent(stack):
    _seed_logentry(stack, "codex/s/468-i")
    first = _ingest468(stack, text="같은 본문", source_id="codex/s/468-i")
    derived_row = stack["graph"].get_node("TextUnit", "codex/s/468-i" + _DERIVED)
    digest = stack["graph"].get_node_digest("codex/s/468-i" + _DERIVED)
    second = _ingest468(stack, text="같은 본문", source_id="codex/s/468-i")
    assert first["status"] == second["status"] == "ok", (first, second)
    assert second["evidence_node"] == "codex/s/468-i" + _DERIVED
    assert stack["graph"].get_node("TextUnit", "codex/s/468-i" + _DERIVED) == derived_row
    assert stack["graph"].get_node_digest("codex/s/468-i" + _DERIVED) == digest


def test_483_same_id_with_different_text_is_stored_under_a_digest_id(stack, caplog):
    """#483: a continued session sends a new text for the same id. It is stored."""
    _seed_logentry(stack, "codex/s/468-d")
    _ingest468(stack, text="첫 본문", source_id="codex/s/468-d")
    derived = "codex/s/468-d" + _DERIVED
    kept = stack["graph"].get_node("TextUnit", derived)
    with caplog.at_level("ERROR"):
        result = _ingest468(stack, text="quokka 다른 본문", source_id="codex/s/468-d")
    slot2 = _slot2("codex/s/468-d", "quokka 다른 본문")
    assert result["status"] == "ok", result
    assert result["node_errors"] == [], result
    assert result["evidence_node"] == slot2 and result["added_nodes"] == 1, result
    assert result["text_ingested"] is True, result
    assert result["stores"]["evidence_node"] == "ok (derived id for codex/s/468-d)", result
    node = stack["graph"].get_node("TextUnit", slot2)
    assert node["text"] == "quokka 다른 본문" and node["source_id"] == "codex/s/468-d"
    assert stack["graph"].get_node("TextUnit", derived) == kept
    bm25, vec = _legs(stack, "quokka")
    assert slot2 in bm25 and slot2 in vec, (bm25, vec)
    assert not [r for r in caplog.records if r.exc_info]


# #488: a same-pack evidence TextUnit at the id is now a text source. See the
# test_488 tests below.
@pytest.mark.parametrize("make_existing", [
    pytest.param(lambda st, i: _seed_node(st, "Evidence", i, text="기존"), id="evidence"),
    pytest.param(lambda st, i: _seed_node(st, "Topic", i, space="concept"), id="other-space"),
])
def test_468_other_existing_nodes_keep_the_rejection(stack, make_existing):
    make_existing(stack, "codex/s/468-o")
    gtype = stack["graph"].lookup_node_type("codex/s/468-o")
    doc_before = stack["docs"].get_node_doc("evidence", "codex/s/468-o")
    result = _ingest468(stack, text="새 본문", source_id="codex/s/468-o")
    assert result["status"] == "partial", result
    assert result["node_errors"] == [f"codex/s/468-o: {_CONFLICT_FIXED}"], result
    assert result["stores"]["evidence_node"] == _CONFLICT_MARKER, result
    assert result["text_ingested"] is False and result["evidence_node"] is None, result
    assert stack["graph"].get_node("TextUnit", "codex/s/468-o" + _DERIVED) is None
    assert stack["graph"].lookup_node_type("codex/s/468-o") == gtype
    assert stack["docs"].get_node_doc("evidence", "codex/s/468-o") == doc_before


def test_468_logentry_of_another_pack_is_rejected_by_the_identity_probe(stack):
    _seed_node(stack, "LogEntry", "codex/s/468-p", pack_id="pack-b", principal=BOB)
    result = _ingest468(stack, text="본문", source_id="codex/s/468-p")
    assert result["node_errors"] == [
        "codex/s/468-p: identity is already attributed to a different pack"
    ], result
    assert result["text_ingested"] is False and result["evidence_node"] is None, result
    assert stack["graph"].get_node("TextUnit", "codex/s/468-p" + _DERIVED) is None


def test_468_logentry_without_pack_id_is_not_derived_from(stack):
    """The identity probe lets an unattributed legacy row pass. The gate must not."""
    with principal_scope(ALICE):
        stack["graph"].upsert_node("LogEntry", "codex/s/468-u", {"session_id": "s"},
                                   space_id="evidence")
    result = _ingest468(stack, text="본문", source_id="codex/s/468-u")
    assert result["node_errors"] == [f"codex/s/468-u: {_CONFLICT_FIXED}"], result
    assert result["text_ingested"] is False, result
    assert stack["graph"].get_node("TextUnit", "codex/s/468-u" + _DERIVED) is None


def test_468_logentry_typed_node_in_another_space_is_not_derived_from(stack):
    with principal_scope(ALICE):
        stack["graph"].upsert_node("LogEntry", "codex/s/468-v", {"pack_id": "pack-a"},
                                   space_id="concept")
    result = _ingest468(stack, text="본문", source_id="codex/s/468-v")
    assert result["node_errors"] == [f"codex/s/468-v: {_CONFLICT_FIXED}"], result
    assert result["text_ingested"] is False, result
    assert stack["graph"].get_node("TextUnit", "codex/s/468-v" + _DERIVED) is None


def test_468_derived_id_owned_by_another_pack_is_rejected(stack):
    _seed_logentry(stack, "codex/s/468-x")
    _seed_node(stack, "TextUnit", "codex/s/468-x" + _DERIVED, pack_id="pack-b",
               principal=BOB, text="남의 것")
    foreign = stack["graph"].get_node("TextUnit", "codex/s/468-x" + _DERIVED)
    result = _ingest468(stack, text="본문", source_id="codex/s/468-x")
    expected = "codex/s/468-x: identity is already attributed to a different pack"
    assert result["status"] == "partial" and result["text_ingested"] is False, result
    assert result["node_errors"] == [expected], result
    assert result["stores"]["evidence_node"] == expected, result
    assert result["evidence_node"] is None, result
    assert stack["graph"].get_node("TextUnit", "codex/s/468-x" + _DERIVED) == foreign


def test_468_id_too_long_to_derive_keeps_the_rejection(stack):
    from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET

    long_id = "L" * (SOURCE_NODE_ID_BUDGET - 2)
    _seed_logentry(stack, long_id)
    result = _ingest468(stack, text="본문", source_id=long_id)
    assert result["node_errors"] == [f"{long_id}: {_CONFLICT_FIXED}"], result
    assert result["text_ingested"] is False, result


def test_468_non_owner_is_rejected_and_nothing_is_written(stack):
    from opencrab.mcp.tools import _ingest_into_pack

    _seed_logentry(stack, "codex/s/468-n")
    with principal_scope(BOB), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        result = _ingest_into_pack("pack-a", text="본문", source_id="codex/s/468-n")
    assert result["status"] == "partial", result
    assert result["evidence_node"] is None, result
    assert stack["graph"].get_node("TextUnit", "codex/s/468-n" + _DERIVED) is None


def _ingest_with_billing(stack, **kw):
    """`_ingest468` that also returns the billing mock."""
    from unittest.mock import MagicMock

    from opencrab.mcp.tools import _ingest_into_pack

    ctx = mcp_ctx_from(stack)
    ctx["billing"] = MagicMock()
    ctx["billing"].on_ingest.return_value = {"ok": True}
    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=ctx):
        return _ingest_into_pack("pack-a", **kw), ctx["billing"]


def test_468_derived_write_store_failure_is_reported_like_the_normal_branch(stack):
    _seed_logentry(stack, "codex/s/468-f2")
    with patch.object(stack["docs"], "upsert_node_doc", side_effect=RuntimeError("doc down")):
        derived, derived_billing = _ingest_with_billing(
            stack, text="본문", source_id="codex/s/468-f2")
        normal, normal_billing = _ingest_with_billing(
            stack, text="본문", source_id="codex/s/468-plain")
    for key in ("status", "evidence_node", "added_nodes", "text_ingested"):
        assert derived[key] == normal[key], (key, derived, normal)
    assert derived["text_ingested"] is True and derived["status"] == "partial", derived
    assert derived["node_errors"] and "codex/s/468-f2" + _DERIVED in derived["node_errors"][0], derived
    assert derived["stores"]["evidence_node"] == normal["stores"]["evidence_node"], (derived, normal)
    # The graph write landed in both cases, so both bill once.
    assert derived_billing.on_ingest.call_count == normal_billing.on_ingest.call_count == 1


@pytest.mark.parametrize("exc", [RuntimeError("registry down"), ValueError("bad grammar")])
def test_468_exception_in_the_derivation_stage_is_an_item_error(stack, exc):
    _seed_logentry(stack, "codex/s/468-e")
    builder = stack["builder"]
    real_add_node = builder.add_node

    def add_node(*args, **kw):
        if str(kw.get("node_id", "")).endswith(_DERIVED):
            raise exc
        return real_add_node(*args, **kw)

    with patch.object(builder, "add_node", side_effect=add_node):
        result, billing = _ingest_with_billing(
            stack, text="본문", source_id="codex/s/468-e",
            nodes=[{"space": "concept", "node_type": "Topic", "node_id": "t-468-e",
                    "properties": {}}],
        )
    assert result["status"] == "partial", result
    assert result["evidence_node"] is None and result["text_ingested"] is False, result
    assert result["stores"]["evidence_node"] == f"error: {type(exc).__name__}", result
    assert len(result["node_errors"]) == 1, result
    if isinstance(exc, ValueError):
        assert result["node_errors"][0] == "codex/s/468-e (evidence/TextUnit): bad grammar"
    else:
        assert "registry down" not in result["node_errors"][0]
    # The node of the same request that came first stays written.
    assert stack["graph"].get_node("Topic", "t-468-e") is not None
    assert billing.on_ingest.call_count == 1
    assert stack["graph"].get_node("TextUnit", "codex/s/468-e" + _DERIVED) is None


class _FakeGraph:
    available = True

    def __init__(self, rows=None, error=None):
        self._rows, self._error = rows, error

    def get_nodes_by_id(self, node_id):
        if self._error:
            raise self._error
        return self._rows


_GOOD_ROW = {"node_type": "LogEntry", "space": "evidence", "pack_id": "pack-a", "id": "x"}


@pytest.mark.parametrize("rows, error, expected", [
    ([_GOOD_ROW], None, True),
    (None, RuntimeError("read failed"), None),
    ("not a list", None, None),
    ([], None, None),
    ([_GOOD_ROW, dict(_GOOD_ROW)], None, None),
    ([{**_GOOD_ROW, "property_decode_error": True}], None, None),
    ([{**_GOOD_ROW, "node_type": "TextUnit"}], None, True),
    ([{**_GOOD_ROW, "node_type": "TextUnit", "pack_id": "pack-b"}], None, False),
    ([{**_GOOD_ROW, "node_type": "TextUnit", "space": "concept"}], None, False),
    ([{**_GOOD_ROW, "node_type": "Evidence"}], None, False),
    ([{**_GOOD_ROW, "space": "concept"}], None, False),
    ([{**_GOOD_ROW, "pack_id": "pack-b"}], None, False),
    ([{k: v for k, v in _GOOD_ROW.items() if k != "pack_id"}], None, False),
])
def test_468_derivation_gate_admits_only_a_readable_same_pack_evidence_source(
    rows, error, expected,
):
    from opencrab.mcp.tools.pack import _derivable_source

    ctx = {"neo4j": _FakeGraph(rows=rows, error=error)}
    assert _derivable_source(ctx, "x", "pack-a") is expected


def test_480_derivation_gate_without_a_read_method_cannot_tell():
    from opencrab.mcp.tools.pack import _derivable_source

    assert _derivable_source({"neo4j": object()}, "x", "pack-a") is None
    assert _derivable_source({}, "x", "pack-a") is None
    assert _derivable_source({"neo4j": object()}, "x", "") is False


def test_468_derived_id_stays_inside_the_node_id_column_after_two_forks():
    from opencrab.pack.fork_remap import NODE_ID_COLUMN_LIMIT, remap_id

    salts = ["0123456789ab", "ba9876543210"]

    def after_two_forks(n):
        return remap_id(remap_id("N" * n, salts[0]), salts[1])

    # The derived id would be 257 characters: refused.
    assert derive_text_id(after_two_forks(226)) is None
    # The derived id is exactly at the column limit: accepted.
    derived = derive_text_id(after_two_forks(225))
    assert derived is not None and len(derived) == NODE_ID_COLUMN_LIMIT


def test_468_accepts_the_exact_base_budget(stack):
    from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET, TEXT_ID_SUFFIX

    source_id = "B" * (SOURCE_NODE_ID_BUDGET - len(TEXT_ID_SUFFIX))
    _seed_logentry(stack, source_id)
    result = _ingest468(stack, text="본문", source_id=source_id)
    assert result["status"] == "ok", result
    assert result["evidence_node"] == source_id + TEXT_ID_SUFFIX, result
    assert len(result["evidence_node"]) == SOURCE_NODE_ID_BUDGET


def test_468_billing_follows_the_graph_write_of_the_derived_node(stack):
    from unittest.mock import MagicMock

    _seed_logentry(stack, "codex/s/468-b1")
    ctx = mcp_ctx_from(stack)
    ctx["billing"] = MagicMock()
    ctx["billing"].on_ingest.return_value = {"ok": True}
    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=ctx):
        _ingest_into_pack("pack-a", text="본문", source_id="codex/s/468-b1")
    assert ctx["billing"].on_ingest.call_count == 1
    # An Evidence-typed conflict is rejected and not billed.
    _seed_node(stack, "Evidence", "codex/s/468-b2", text="기존")
    ctx["billing"].reset_mock()
    with principal_scope(ALICE), patch("opencrab.mcp.tools._get_context", return_value=ctx):
        _ingest_into_pack("pack-a", text="본문", source_id="codex/s/468-b2")
    ctx["billing"].on_ingest.assert_not_called()


def test_468_derived_text_survives_a_fork_and_a_retry_on_the_fork_target(stack):
    _seed_logentry(stack, "codex/s/468-k")
    # Same metadata the real pack_ingest tool passes.
    meta = {"title": "포크 제목", "source": "pack_ingest"}
    _ingest468(stack, text="포크 본문", source_id="codex/s/468-k", metadata=meta)

    forked = _fork(stack, principal=ALICE, src_pack_id="pack-a")
    assert forked.get("status") == "ok", forked
    new_pack = forked["pack_id"]
    ids = {
        r["props"]["id"] for r in stack["graph"].export_nodes(pack_id=new_pack)
        if r["props"]["id"].startswith("codex/s/468-k")
    }
    log_copy = next(i for i in ids if "#text" not in i)
    text_copy = next(i for i in ids if "#text" in i)
    assert text_copy == derive_text_id(log_copy), ids
    before = stack["graph"].get_node("TextUnit", text_copy)
    assert before["source_id"] == log_copy
    assert before["title"] == "포크 제목" and before["source"] == "pack_ingest"

    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        retry = _ingest_into_pack(new_pack, text="포크 본문", source_id=log_copy, metadata=meta)
    assert retry["status"] == "ok", retry
    assert retry["evidence_node"] == text_copy, retry
    assert stack["graph"].get_node("TextUnit", text_copy) == before


@pytest.mark.parametrize("plain", [
    "a", "codex/s/1", "x#y", "k~notsalt", "x\n", "\n",
    "x~0123456789AB", "x~0123456789a_", "x~0123456789ag",
])
def test_468_derived_id_commutes_with_the_fork_remap(plain):
    from opencrab.pack.fork_remap import remap_id

    for salts in ([], ["0123456789ab"], ["0123456789ab", "ba9876543210"]):
        original = plain
        for salt in salts:
            original = remap_id(original, salt)
        expected = plain + "#text"
        for salt in salts:
            expected = remap_id(expected, salt)
        assert derive_text_id(original) == expected
    assert derive_text_id(plain) == plain + "#text"


def test_468_nodes_loop_rejection_is_reported_and_later_items_still_land(stack, caplog):
    seeded = _seed_logentry(stack, "codex/s/468-b")
    doc_before = stack["docs"].get_node_doc("evidence", "codex/s/468-b")
    vec_before = stack["vector"].get_by_id("codex/s/468-b")
    with caplog.at_level("ERROR"):
        result = _ingest468(stack, nodes=[
            {"space": "evidence", "node_type": "TextUnit",
             "node_id": "codex/s/468-b", "properties": {"text": "충돌"}},
            {"space": "evidence", "node_type": "TextUnit",
             "node_id": "codex/s/468-c", "properties": {"text": "정상"}},
        ])
    assert result["status"] == "partial", result
    assert result["added_nodes"] == 1, result
    assert result["node_errors"] == [f"codex/s/468-b: {_CONFLICT_FIXED}"], result
    # #480: the one error is a final rejection, so a resend cannot fix it.
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["node_identity_conflict"], result
    assert stack["graph"].get_node("TextUnit", "codex/s/468-c") is not None
    # 거절된 항목은 기존 행을 건드리지 않고, 재시도마다 ERROR 트레이스백이 쌓이지 않는다.
    assert stack["graph"].get_node("LogEntry", "codex/s/468-b") == seeded
    assert stack["graph"].get_node("TextUnit", "codex/s/468-b") is None
    assert stack["docs"].get_node_doc("evidence", "codex/s/468-b") == doc_before
    assert stack["vector"].get_by_id("codex/s/468-b") == vec_before
    assert not [r for r in caplog.records if r.exc_info]


@pytest.mark.parametrize("source_id", ["error", "no match-468", "codex/s/468-h"])
def test_468_marker_does_not_depend_on_the_caller_id_spelling(stack, source_id):
    """표식이 `store_write_failures` 의 접두사 판정과 섞이지 않아야 한다."""
    _seed_node(stack, "Evidence", source_id, text="기존")
    result = _ingest468(stack, text="본문", source_id=source_id, text_as_node=True)
    assert result["stores"]["evidence_node"] == _CONFLICT_MARKER, result
    assert result["node_errors"] == [f"{source_id}: {_CONFLICT_FIXED}"], result


def test_468_legacy_text_path_keeps_its_own_behaviour(stack):
    _seed_logentry(stack, "codex/s/468-g")
    result = _ingest468(
        stack, text="대화 원문", source_id="codex/s/468-g", text_as_node=False,
    )
    assert result["status"] == "partial", result
    assert result["evidence_node"] is None, result


def test_468_clean_ingest_response_is_unchanged(stack):
    result = _ingest468(stack, text="정상 본문", source_id="codex/s/468-f")
    assert result["status"] == "ok", result
    assert result["stores"]["evidence_node"] == "ok", result
    assert set(result) == {
        "status", "pack_id", "added_nodes", "added_edges", "node_errors",
        "edge_errors", "stores", "text_ingested", "evidence_node",
    }


# ---------------------------------------------------------------------------
# #480: a partial result says whether a resend can fix it
# ---------------------------------------------------------------------------


def _session_payload(ended_at: str, text: str, node_id: str = "codex/s/480-a") -> dict[str, Any]:
    """The shape the Codex session hook sends: a LogEntry node plus its text."""
    return dict(
        nodes=[{"space": "evidence", "node_type": "LogEntry", "node_id": node_id,
                "properties": {"title": "t", "session_id": "s", "type": "codex-session",
                               "ended_at": ended_at}}],
        text=text, source_id=node_id, metadata={"title": "t"},
    )


def test_480_identical_resend_is_ok_and_has_no_retry_fields(stack):
    first = _ingest468(stack, **_session_payload("2026-10-07T03:21:03Z", "본문1"))
    second = _ingest468(stack, **_session_payload("2026-10-07T03:21:03Z", "본문1"))
    for result in (first, second):
        assert result["status"] == "ok", result
        assert "retryable" not in result and "reject_codes" not in result, result


def test_480_continued_session_is_a_final_rejection(stack):
    _ingest468(stack, **_session_payload("2026-10-07T03:21:03Z", "본문1"))
    result = _ingest468(stack, **_session_payload("2026-10-08T03:21:03Z", "본문2"))
    assert result["status"] == "partial", result
    # #483: only the LogEntry is refused. The new text is stored at slot 2.
    assert len(result["node_errors"]) == 1, result
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["node_identity_conflict"], result
    assert result["text_ingested"] is True, result
    assert result["evidence_node"] == _slot2("codex/s/480-a", "본문2"), result


def test_480_store_failure_stays_retryable(stack):
    with patch.object(stack["docs"], "upsert_node_doc", side_effect=RuntimeError("doc down")):
        result = _ingest468(stack, text="본문", source_id="codex/s/480-b")
    assert result["status"] == "partial", result
    assert result["retryable"] is True, result
    assert "reject_codes" not in result, result


def test_480_legacy_store_failure_alone_stays_retryable(stack):
    # The legacy path reports a failed store only in `stores`, with no node_errors entry.
    with patch.object(stack["docs"], "upsert_source", side_effect=RuntimeError("doc down")):
        result = _ingest468(stack, text="본문", source_id="codex/s/480-m", text_as_node=False)
    assert result["status"] == "partial" and result["node_errors"] == [], result
    assert result["retryable"] is True, result
    assert "reject_codes" not in result, result


def test_480_legacy_store_failure_with_a_final_rejection_stays_retryable(stack):
    _seed_node(stack, "TextUnit", "480-p1", pack_id="pack-b", principal=BOB, text="남의 것")
    with patch.object(stack["docs"], "upsert_source", side_effect=RuntimeError("doc down")):
        result = _ingest468(
            stack, text="본문", source_id="codex/s/480-m2", text_as_node=False,
            nodes=[{"space": "evidence", "node_type": "TextUnit", "node_id": "480-p1",
                    "properties": {"text": "충돌"}}],
        )
    assert len(result["node_errors"]) == 1, result
    assert result["retryable"] is True, result


def test_480_reject_codes_list_each_reason_once(stack):
    _seed_node(stack, "TextUnit", "480-q1", pack_id="pack-b", principal=BOB, text="남의 것")
    _seed_node(stack, "TextUnit", "480-q2", pack_id="pack-b", principal=BOB, text="남의 것")
    result = _ingest468(stack, nodes=[
        {"space": "evidence", "node_type": "TextUnit", "node_id": i, "properties": {"text": "x"}}
        for i in ("480-q1", "480-q2")
    ])
    assert len(result["node_errors"]) == 2, result
    assert result["reject_codes"] == ["foreign_pack_identity"], result


def test_480_final_rejection_plus_store_failure_stays_retryable(stack):
    _seed_logentry(stack, "codex/s/480-c")
    with patch.object(stack["docs"], "upsert_node_doc", side_effect=RuntimeError("doc down")):
        result = _ingest468(stack, nodes=[
            {"space": "evidence", "node_type": "TextUnit", "node_id": "codex/s/480-c",
             "properties": {"text": "충돌"}},
            {"space": "evidence", "node_type": "TextUnit", "node_id": "codex/s/480-d",
             "properties": {"text": "정상"}},
        ])
    assert result["status"] == "partial", result
    assert len(result["node_errors"]) == 2, result
    assert result["retryable"] is True, result


@pytest.mark.parametrize("source_id", ["error", "no match-480", "codex/s/480-e"])
def test_480_foreign_pack_text_rejection_is_final_whatever_the_id_spelling(stack, source_id):
    _seed_node(stack, "TextUnit", source_id, pack_id="pack-b", principal=BOB, text="남의 것")
    result = _ingest468(stack, text="본문", source_id=source_id)
    assert result["status"] == "partial", result
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["foreign_pack_identity"], result


@pytest.mark.parametrize("source_id", ["error", "no match-480", "codex/s/480-f"])
def test_480_foreign_derived_id_rejection_is_final_whatever_the_id_spelling(stack, source_id):
    _seed_logentry(stack, source_id)
    _seed_node(stack, "TextUnit", source_id + _DERIVED, pack_id="pack-b", principal=BOB, text="남의 것")
    result = _ingest468(stack, text="본문", source_id=source_id)
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["derived_id_conflict"], result


def test_480_unreadable_logentry_check_stays_retryable(stack):
    _seed_logentry(stack, "codex/s/480-g")
    real = stack["graph"].get_nodes_by_id
    calls = []

    def flaky(node_id):
        # The ownership probe and the builder identity check read first and
        # succeed. The derivation check reads last, after the graph write
        # refused the id, and that read fails.
        calls.append(node_id)
        if len(calls) > 2:
            raise RuntimeError("read down")
        return real(node_id)

    with patch.object(stack["graph"], "get_nodes_by_id", side_effect=flaky):
        result = _ingest468(stack, text="본문", source_id="codex/s/480-g")
    assert len(calls) > 2, calls
    assert result["status"] == "partial" and result["text_ingested"] is False, result
    # The fixed conflict message proves that the derivation check ran and returned None.
    assert result["node_errors"] == [f"codex/s/480-g: {_CONFLICT_FIXED}"], result
    assert result["retryable"] is True, result
    assert "reject_codes" not in result, result


def test_480_non_logentry_conflict_and_too_long_id_are_final(stack):
    from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET

    _seed_node(stack, "Evidence", "codex/s/480-h", text="기존")
    other = _ingest468(stack, text="본문", source_id="codex/s/480-h")
    long_id = "L" * (SOURCE_NODE_ID_BUDGET - 2)
    _seed_logentry(stack, long_id)
    too_long = _ingest468(stack, text="본문", source_id=long_id)
    for result in (other, too_long):
        assert result["retryable"] is False, result
        assert result["reject_codes"] == ["node_identity_conflict"], result


def test_480_legacy_text_conflict_stays_retryable(stack):
    _seed_logentry(stack, "codex/s/480-i")
    result = _ingest468(stack, text="대화 원문", source_id="codex/s/480-i", text_as_node=False)
    assert result["status"] == "partial", result
    assert result["retryable"] is True, result


def test_480_foreign_edge_rejection_is_final(stack):
    _seed_node(stack, "Topic", "480-t1", space="concept")
    _seed_node(stack, "Topic", "480-t2", space="concept")
    with principal_scope(ALICE), patch(
        "opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)
    ):
        from opencrab.mcp.tools import _ingest_into_pack

        edge = {"from_space": "concept", "from_id": "480-t1", "relation": "related_to",
                "to_space": "concept", "to_id": "480-t2"}
        with patch("opencrab.mcp.tools.pack._edge_probe_conflict", return_value="foreign"):
            result = _ingest_into_pack("pack-a", edges=[edge])
    assert result["status"] == "partial", result
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["foreign_pack_identity"], result


def test_480_unverifiable_ownership_probe_stays_retryable(stack):
    with patch("opencrab.mcp.tools.pack._node_probe_conflict", return_value="unverifiable"):
        result = _ingest468(stack, text="본문", source_id="codex/s/480-j")
    assert result["status"] == "partial" and result["text_ingested"] is False, result
    assert result["retryable"] is True, result
    assert "reject_codes" not in result, result


# One test per classification site: a foreign probe is final, an unverifiable probe is not.

def _assert_probe_outcome(result, reason):
    assert result["status"] == "partial", result
    if reason == "foreign":
        assert result["retryable"] is False, result
        assert result["reject_codes"] == ["foreign_pack_identity"], result
    else:
        assert result["retryable"] is True, result
        assert "reject_codes" not in result, result


def test_480_node_loop_foreign_pack_is_final_for_real_state(stack):
    _seed_node(stack, "TextUnit", "480-n1", pack_id="pack-b", principal=BOB, text="남의 것")
    result = _ingest468(stack, nodes=[
        {"space": "evidence", "node_type": "TextUnit", "node_id": "480-n1",
         "properties": {"text": "충돌"}},
    ])
    _assert_probe_outcome(result, "foreign")


@pytest.mark.parametrize("reason", ["foreign", "unverifiable"])
def test_480_node_loop_probe_reason_decides_retryable(stack, reason):
    with patch("opencrab.mcp.tools.pack._node_probe_conflict", return_value=reason):
        result = _ingest468(stack, nodes=[
            {"space": "evidence", "node_type": "TextUnit", "node_id": "480-n2",
             "properties": {"text": "본문"}},
        ])
    assert len(result["node_errors"]) == 1, result
    _assert_probe_outcome(result, reason)


@pytest.mark.parametrize("reason", ["foreign", "unverifiable"])
def test_480_edge_loop_probe_reason_decides_retryable(stack, reason):
    _seed_node(stack, "Topic", "480-e1", space="concept")
    _seed_node(stack, "Topic", "480-e2", space="concept")
    edge = {"from_space": "concept", "from_id": "480-e1", "relation": "related_to",
            "to_space": "concept", "to_id": "480-e2"}
    with patch("opencrab.mcp.tools.pack._edge_probe_conflict", return_value=reason):
        result = _ingest468(stack, edges=[edge])
    assert len(result["edge_errors"]) == 1, result
    _assert_probe_outcome(result, reason)


@pytest.mark.parametrize("reason", ["foreign", "unverifiable"])
def test_480_legacy_source_probe_reason_decides_retryable(stack, reason):
    with patch("opencrab.mcp.tools.pack._source_probe_conflict", return_value=reason):
        result = _ingest468(stack, text="본문", source_id="codex/s/480-l1", text_as_node=False)
    assert len(result["node_errors"]) == 1, result
    assert result["text_ingested"] is False, result
    _assert_probe_outcome(result, reason)


@pytest.mark.parametrize("reason", ["foreign", "unverifiable"])
def test_480_derived_id_probe_reason_decides_retryable(stack, reason):
    import opencrab.mcp.tools.pack as pack_module

    _seed_logentry(stack, "codex/s/480-d1")
    real = pack_module._node_probe_conflict

    def probe(ctx, space, node_type, node_id, pack_id):
        # The text probe passes. Only the probe of the derived id answers `reason`.
        if node_id.endswith(_DERIVED):
            return reason
        return real(ctx, space, node_type, node_id, pack_id)

    with patch("opencrab.mcp.tools.pack._node_probe_conflict", side_effect=probe):
        result = _ingest468(stack, text="본문", source_id="codex/s/480-d1")
    assert result["text_ingested"] is False, result
    assert result["stores"]["evidence_node"].startswith("codex/s/480-d1:"), result
    if reason == "foreign":
        assert result["retryable"] is False, result
        assert result["reject_codes"] == ["derived_id_conflict"], result
    else:
        assert result["retryable"] is True, result
        assert "reject_codes" not in result, result


# ---------------------------------------------------------------------------
# #480: node and edge grammar rejections are final
# ---------------------------------------------------------------------------


def _grammar_nodes():
    return {
        "unknown-type": {"space": "evidence", "node_type": "NoSuchType", "node_id": "480-g1",
                         "properties": {}},
        "document-without-title": {"space": "resource", "node_type": "Document",
                                   "node_id": "480-g2", "properties": {"pack_id": "pack-a"}},
        "claim-without-statement": {"space": "claim", "node_type": "Claim",
                                    "node_id": "480-g3", "properties": {"pack_id": "pack-a"}},
    }


@pytest.mark.parametrize("name", ["unknown-type", "document-without-title", "claim-without-statement"])
def test_480_node_grammar_rejection_is_final(stack, name):
    result = _ingest468(stack, nodes=[_grammar_nodes()[name]])
    assert result["status"] == "partial" and len(result["node_errors"]) == 1, result
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["grammar_rejected"], result


def test_480_edge_grammar_rejection_is_final(stack):
    _seed_node(stack, "Topic", "480-ge1", space="concept")
    _seed_node(stack, "Topic", "480-ge2", space="concept")
    edge = {"from_space": "concept", "from_id": "480-ge1", "relation": "no_such_relation",
            "to_space": "concept", "to_id": "480-ge2"}
    result = _ingest468(stack, edges=[edge])
    assert result["status"] == "partial" and len(result["edge_errors"]) == 1, result
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["grammar_rejected"], result


def test_480_grammar_rejections_report_one_code_and_mix_with_other_final_codes(stack):
    _seed_logentry(stack, "codex/s/480-gm")
    result = _ingest468(stack, nodes=[
        _grammar_nodes()["unknown-type"], _grammar_nodes()["document-without-title"],
        {"space": "evidence", "node_type": "TextUnit", "node_id": "codex/s/480-gm",
         "properties": {"text": "충돌"}},
    ])
    assert len(result["node_errors"]) == 3, result
    assert result["retryable"] is False, result
    assert result["reject_codes"] == ["grammar_rejected", "node_identity_conflict"], result


def test_480_grammar_rejection_with_a_store_failure_stays_retryable(stack):
    with patch.object(stack["docs"], "upsert_node_doc", side_effect=RuntimeError("doc down")):
        result = _ingest468(stack, nodes=[
            _grammar_nodes()["unknown-type"],
            {"space": "evidence", "node_type": "TextUnit", "node_id": "480-gs",
             "properties": {"text": "정상"}},
        ])
    assert len(result["node_errors"]) == 2, result
    assert result["retryable"] is True, result
    assert "reject_codes" not in result, result


def test_480_a_failing_validator_stays_retryable(stack):
    with patch("opencrab.grammar.validator.validate_node", side_effect=RuntimeError("manifest gone")):
        result = _ingest468(stack, nodes=[_grammar_nodes()["unknown-type"]])
    assert result["status"] == "partial" and len(result["node_errors"]) == 1, result
    assert result["retryable"] is True, result
    assert "reject_codes" not in result, result


def test_480_a_plain_builder_value_error_stays_retryable(stack):
    with patch.object(stack["builder"], "add_node", side_effect=ValueError("not a grammar verdict")):
        result = _ingest468(stack, nodes=[
            {"space": "evidence", "node_type": "TextUnit", "node_id": "480-gv",
             "properties": {"text": "본문"}},
        ])
    assert len(result["node_errors"]) == 1, result
    assert result["retryable"] is True, result
    assert "reject_codes" not in result, result


# ---------------------------------------------------------------------------
# #483: each text of a continued session is stored as its own TextUnit
# ---------------------------------------------------------------------------


def _node_state(stack, node_type: str, node_id: str):
    """Return the graph digest and the graph row.

    An idempotent resend refreshes ``updated_at`` of the doc row (#470
    behavior), so this helper does not compare the doc row.
    """
    return (stack["graph"].get_node_digest(node_id, node_type=node_type),
            stack["graph"].get_node(node_type, node_id))


def test_483_three_texts_of_one_session_each_land_and_resends_are_idempotent(stack):
    sid = "codex/s/483-a"
    first = _ingest468(stack, **_session_payload("2026-10-07T00:00:00Z", "본문 A", sid))
    assert first["status"] == "ok" and first["evidence_node"] == sid + _DERIVED, first
    log_before = _snapshot(stack, "LogEntry", sid), _node_state(stack, "LogEntry", sid)
    slot1_before = _node_state(stack, "TextUnit", sid + _DERIVED)

    b = _ingest468(stack, **_session_payload("2026-10-08T00:00:00Z", "본문 A 그리고 B", sid))
    c = _ingest468(stack, **_session_payload("2026-10-09T00:00:00Z", "본문 A 그리고 B 그리고 C", sid))
    assert b["evidence_node"] == _slot2(sid, "본문 A 그리고 B"), b
    assert c["evidence_node"] == _slot2(sid, "본문 A 그리고 B 그리고 C"), c
    for r in (b, c):
        assert r["text_ingested"] is True and r["added_nodes"] == 1, r
        assert r["reject_codes"] == ["node_identity_conflict"], r
    ids = {sid + _DERIVED, b["evidence_node"], c["evidence_node"]}
    assert len(ids) == 3
    for i in ids:
        node = stack["graph"].get_node("TextUnit", i)
        assert node["source_id"] == sid and node["pack_id"] == "pack-a", node
        assert stack["vector"].get_by_id(i) is not None, i
    states = {i: _node_state(stack, "TextUnit", i) for i in ids}

    # Resends of each version find the same node and add nothing.
    again = [
        _ingest468(stack, **_session_payload("2026-10-10T00:00:00Z", t, sid))
        for t in ("본문 A", "본문 A 그리고 B", "본문 A 그리고 B 그리고 C")
    ]
    assert [r["evidence_node"] for r in again] == [
        sid + _DERIVED, b["evidence_node"], c["evidence_node"]]
    for r in again:
        assert r["added_nodes"] == 1 and r["text_ingested"] is True, r
    assert {i: _node_state(stack, "TextUnit", i) for i in ids} == states
    ids_now = {
        r["props"]["id"] for r in stack["graph"].export_nodes(pack_id="pack-a")
        if r["props"]["id"].startswith(sid)
    }
    assert ids_now == ids | {sid}, ids_now
    # The first LogEntry and the slot 1 TextUnit are unchanged.
    assert (_snapshot(stack, "LogEntry", sid), _node_state(stack, "LogEntry", sid)) == log_before
    assert _node_state(stack, "TextUnit", sid + _DERIVED) == slot1_before


def test_483_first_text_lands_at_slot_1_even_if_it_is_a_later_version(stack):
    _seed_logentry(stack, "codex/s/483-b")
    result = _ingest468(stack, text="나중 본문", source_id="codex/s/483-b")
    assert result["evidence_node"] == "codex/s/483-b" + _DERIVED, result
    assert stack["graph"].get_node("TextUnit", _slot2("codex/s/483-b", "나중 본문")) is None


def test_483_same_text_other_title_after_slot_1_adds_one_digest_node(stack):
    _seed_logentry(stack, "codex/s/483-c")
    _ingest468(stack, text="본문 C", source_id="codex/s/483-c", metadata={"title": "t1"})
    result = _ingest468(stack, text="본문 C", source_id="codex/s/483-c", metadata={"title": "t2"})
    slot2 = _slot2("codex/s/483-c", "본문 C")
    assert result["status"] == "ok" and result["evidence_node"] == slot2, result
    assert stack["graph"].get_node("TextUnit", slot2)["title"] == "t2"
    assert stack["graph"].get_node("TextUnit", "codex/s/483-c" + _DERIVED)["title"] == "t1"


def test_483_same_text_other_title_after_slot_2_is_a_derived_conflict(stack):
    _seed_logentry(stack, "codex/s/483-d")
    _ingest468(stack, text="첫 본문", source_id="codex/s/483-d")
    # Precondition: text B sits at its digest id.
    first_b = _ingest468(stack, text="본문 B", source_id="codex/s/483-d", metadata={"title": "t1"})
    slot2 = _slot2("codex/s/483-d", "본문 B")
    assert first_b["evidence_node"] == slot2, first_b
    kept = _node_state(stack, "TextUnit", slot2)
    result = _ingest468(stack, text="본문 B", source_id="codex/s/483-d", metadata={"title": "t2"})
    assert result["status"] == "partial" and result["text_ingested"] is False, result
    assert result["node_errors"] == [
        f"codex/s/483-d: derived id {slot2} already names a different node or "
        "different content; this item was not written"
    ], result
    assert result["stores"]["evidence_node"] == "rejected (derived id conflict)", result
    assert result["retryable"] is False and result["reject_codes"] == ["derived_id_conflict"]
    assert _node_state(stack, "TextUnit", slot2) == kept


def test_483_squatters_at_both_slots_are_a_final_derived_conflict(stack, caplog):
    sid = "codex/s/483-e"
    _seed_logentry(stack, sid)
    _seed_node(stack, "TextUnit", sid + _DERIVED, text="다른 것")
    _seed_node(stack, "TextUnit", _slot2(sid, "본문 E"), text="또 다른 것")
    with caplog.at_level("WARNING"):
        result, billing = _ingest_with_billing(stack, text="본문 E", source_id=sid)
    assert result["status"] == "partial" and result["text_ingested"] is False, result
    # One report, for slot 2 only. Slot 1 reports nothing.
    assert result["node_errors"] == [
        f"{sid}: derived id {_slot2(sid, '본문 E')} already names a different node or "
        "different content; this item was not written"
    ], result
    assert result["evidence_node"] is None and result["added_nodes"] == 0, result
    assert result["retryable"] is False and result["reject_codes"] == ["derived_id_conflict"]
    assert sum("derived id conflict" in r.getMessage() for r in caplog.records) == 1
    billing.on_ingest.assert_not_called()


def test_483_non_textunit_at_slot_1_falls_to_slot_2(stack):
    sid = "codex/s/483-f"
    _seed_logentry(stack, sid)
    _seed_node(stack, "Evidence", sid + _DERIVED, text="증거")
    result = _ingest468(stack, text="본문 F", source_id=sid)
    assert result["status"] == "ok" and result["evidence_node"] == _slot2(sid, "본문 F"), result
    assert stack["graph"].lookup_node_type(sid + _DERIVED) == "Evidence"


def test_483_foreign_pack_at_slot_1_does_not_fall_through(stack):
    sid = "codex/s/483-g"
    _seed_logentry(stack, sid)
    _seed_node(stack, "TextUnit", sid + _DERIVED, pack_id="pack-b", principal=BOB, text="남의 것")
    result = _ingest468(stack, text="본문 G", source_id=sid)
    assert result["text_ingested"] is False and result["evidence_node"] is None, result
    assert result["reject_codes"] == ["derived_id_conflict"], result
    assert stack["graph"].get_node("TextUnit", _slot2(sid, "본문 G")) is None


def test_483_foreign_pack_at_slot_2_is_a_final_derived_conflict(stack):
    sid = "codex/s/483-h"
    _seed_logentry(stack, sid)
    _ingest468(stack, text="첫 본문", source_id=sid)
    slot2 = _slot2(sid, "본문 H")
    _seed_node(stack, "TextUnit", slot2, pack_id="pack-b", principal=BOB, text="남의 것")
    result = _ingest468(stack, text="본문 H", source_id=sid)
    assert result["node_errors"] == [
        f"{sid}: identity is already attributed to a different pack"], result
    assert result["text_ingested"] is False and result["evidence_node"] is None, result
    assert result["reject_codes"] == ["derived_id_conflict"], result


def test_483_slot_2_budget_edge(stack):
    from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET

    slot2_suffix = len("#text-") + 16
    fits = "F" * (SOURCE_NODE_ID_BUDGET - slot2_suffix)
    over = "G" * (SOURCE_NODE_ID_BUDGET - slot2_suffix + 1)
    for sid in (fits, over):
        _seed_logentry(stack, sid)
        _ingest468(stack, text="첫 본문", source_id=sid)
    ok = _ingest468(stack, text="둘째 본문", source_id=fits)
    assert ok["status"] == "ok" and ok["evidence_node"] == _slot2(fits, "둘째 본문"), ok
    assert len(ok["evidence_node"]) == SOURCE_NODE_ID_BUDGET
    refused = _ingest468(stack, text="둘째 본문", source_id=over)
    # Slot 1 fits but slot 2 does not: the slot 1 conflict stays visible.
    assert refused["node_errors"] == [
        f"{over}: derived id {over}{_DERIVED} already names a different node or "
        "different content; this item was not written"
    ], refused
    assert refused["text_ingested"] is False, refused
    assert refused["reject_codes"] == ["derived_id_conflict"], refused


def test_483_slot_2_store_failure_stays_retryable_and_bills(stack):
    sid = "codex/s/483-i"
    _seed_logentry(stack, sid)
    _ingest468(stack, text="첫 본문", source_id=sid)
    with patch.object(stack["docs"], "upsert_node_doc", side_effect=RuntimeError("doc down")):
        result, billing = _ingest_with_billing(stack, text="본문 I", source_id=sid)
    assert result["status"] == "partial" and result["text_ingested"] is True, result
    assert result["evidence_node"] is None, result
    assert len(result["node_errors"]) == 1 and _slot2(sid, "본문 I") in result["node_errors"][0]
    assert result["retryable"] is True and "reject_codes" not in result, result
    assert billing.on_ingest.call_count == 1


def test_483_slot_2_exception_stays_retryable(stack):
    sid = "codex/s/483-j"
    _seed_logentry(stack, sid)
    _ingest468(stack, text="첫 본문", source_id=sid)
    builder = stack["builder"]
    real_add_node = builder.add_node

    def add_node(*args, **kw):
        if "#text-" in str(kw.get("node_id", "")):
            raise RuntimeError("registry down")
        return real_add_node(*args, **kw)

    with patch.object(builder, "add_node", side_effect=add_node):
        result, billing = _ingest_with_billing(stack, text="본문 J", source_id=sid)
    assert result["text_ingested"] is False and result["evidence_node"] is None, result
    assert result["stores"]["evidence_node"] == "error: RuntimeError", result
    assert result["retryable"] is True and "reject_codes" not in result, result
    billing.on_ingest.assert_not_called()


def test_483_slot_2_write_bills_once(stack):
    sid = "codex/s/483-k"
    _seed_logentry(stack, sid)
    _ingest468(stack, text="첫 본문", source_id=sid)
    result, billing = _ingest_with_billing(stack, text="본문 K", source_id=sid)
    assert result["evidence_node"] == _slot2(sid, "본문 K"), result
    assert billing.on_ingest.call_count == 1


def test_483_digest_text_survives_a_fork_and_a_retry_on_the_fork_target(stack):
    sid = "codex/s/483-m"
    _seed_logentry(stack, sid)
    meta = {"title": "포크 제목", "source": "pack_ingest"}
    _ingest468(stack, text="첫 본문", source_id=sid, metadata=meta)
    _ingest468(stack, text="둘째 본문", source_id=sid, metadata=meta)

    forked = _fork(stack, principal=ALICE, src_pack_id="pack-a")
    assert forked.get("status") == "ok", forked
    new_pack = forked["pack_id"]
    ids = {
        r["props"]["id"] for r in stack["graph"].export_nodes(pack_id=new_pack)
        if r["props"]["id"].startswith(sid)
    }
    log_copy = next(i for i in ids if "#text" not in i)
    digest = _slot2("", "둘째 본문")[len("#text-"):]
    slot2_copy = derive_text_id(log_copy, digest)
    assert slot2_copy in ids, ids
    before = stack["graph"].get_node("TextUnit", slot2_copy)
    assert before["source_id"] == log_copy

    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        retry = _ingest_into_pack(new_pack, text="둘째 본문", source_id=log_copy, metadata=meta)
    assert retry["status"] == "ok", retry
    assert retry["evidence_node"] == slot2_copy, retry
    assert stack["graph"].get_node("TextUnit", slot2_copy) == before


@pytest.mark.parametrize("plain", ["a", "codex/s/1", "x#y", "k~notsalt", "x~0123456789AB"])
def test_483_digest_id_commutes_with_the_fork_remap(plain):
    from opencrab.pack.fork_remap import remap_id

    digest = "0123456789abcdef"
    for salts in ([], ["0123456789ab"], ["0123456789ab", "ba9876543210"]):
        original = plain
        expected = plain + "#text-" + digest
        for salt in salts:
            original = remap_id(original, salt)
            expected = remap_id(expected, salt)
        assert derive_text_id(original, digest) == expected


# ---------------------------------------------------------------------------
# #488: a continued Claude session. The first text sits at source_id itself.
# ---------------------------------------------------------------------------


def _claude_payload(sid: str, text: str, turns: int) -> dict[str, Any]:
    """Build the payload that the Claude session hook sends: Document, Claim, Evidence and the text.

    The Document and the Claim change with every delivery, as in the hook.
    """
    nodes = [
        {"space": "resource", "node_type": "Document", "node_id": f"resource:{sid}",
         "properties": {"title": "p", "session_id": "s", "source_id": sid,
                        "turn_count": turns}},
        {"space": "claim", "node_type": "Claim", "node_id": f"claim:{sid}",
         "properties": {"statement": f"{turns} turns", "text": f"{turns} turns"}},
    ]
    nodes += [
        {"space": "evidence", "node_type": "Evidence", "node_id": f"evidence:{sid}:{i}",
         "properties": {"text": f"turn {i}"}}
        for i in range(turns)
    ]
    return dict(nodes=nodes, text=text, source_id=sid,
                metadata={"title": "p", "source": "pack_ingest"})


def _settled_bm25(stack, timeout: float = 30.0) -> None:
    """Wait for the debounced BM25 rebuild after the last write.

    The BM25 leg serves the last built index while a rebuild is pending. A
    query right after a write can miss that write.
    """
    import time

    hybrid = stack["hybrid"]
    deadline = time.monotonic() + timeout
    while hybrid._bm25_dirty and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not hybrid._bm25_dirty, "BM25 rebuild did not settle"


def test_488_claude_session_texts_each_land_and_resends_are_idempotent(stack):
    sid = "claude/p/488-a"
    texts = ("wombat 첫 본문", "wombat 첫 본문 numbat 둘째", "wombat 첫 본문 numbat 둘째 quoll 셋째")
    first = _ingest468(stack, **_claude_payload(sid, texts[0], 1))
    assert first["status"] == "ok" and first["evidence_node"] == sid, first
    first_rows = _snapshot(stack, "TextUnit", sid)
    first_state = _node_state(stack, "TextUnit", sid)

    b = _ingest468(stack, **_claude_payload(sid, texts[1], 2))
    c = _ingest468(stack, **_claude_payload(sid, texts[2], 3))
    expected = [sid, sid + _DERIVED, _slot2(sid, texts[2])]
    assert [b["evidence_node"], c["evidence_node"]] == expected[1:], (b, c)
    for r in (b, c):
        # The Document and the Claim changed, so the graph refuses them. The text lands.
        assert r["text_ingested"] is True, r
        assert len(r["node_errors"]) == 2, r
        assert r["retryable"] is False and r["reject_codes"] == ["node_identity_conflict"], r
    for node_id, text in zip(expected, texts):
        node = stack["graph"].get_node("TextUnit", node_id)
        assert node["text"] == text and node["pack_id"] == "pack-a", node
        assert node.get("source_id") == (None if node_id == sid else sid), node
    _settled_bm25(stack)
    for word, node_id in (("wombat", sid), ("numbat", expected[1]), ("quoll", expected[2])):
        bm25, vec = _legs(stack, word)
        assert node_id in bm25 and node_id in vec, (word, bm25, vec)
    states = {i: _node_state(stack, "TextUnit", i) for i in expected}

    # B and C never write at source_id: all three rows of the first text stay.
    assert _snapshot(stack, "TextUnit", sid) == first_rows
    again = [_ingest468(stack, **_claude_payload(sid, t, 4)) for t in texts[1:]]
    assert [r["evidence_node"] for r in again] == expected[1:], again
    assert _snapshot(stack, "TextUnit", sid) == first_rows
    # A resend of A is the ordinary idempotent write at source_id (#470).
    again.append(_ingest468(stack, **_claude_payload(sid, texts[0], 4)))
    assert again[-1]["evidence_node"] == sid, again[-1]
    for r in again:
        assert r["text_ingested"] is True and r["added_nodes"] >= 1, r
    assert {i: _node_state(stack, "TextUnit", i) for i in expected} == states
    assert _node_state(stack, "TextUnit", sid) == first_state
    ids_now = {
        r["props"]["id"] for r in stack["graph"].export_nodes(pack_id="pack-a")
        if r["props"]["id"].startswith(sid)
    }
    assert ids_now == set(expected), ids_now


def test_488_textunit_source_foreign_slot_1_does_not_fall_through(stack):
    sid = "claude/p/488-b"
    _ingest468(stack, text="첫 본문", source_id=sid)
    _seed_node(stack, "TextUnit", sid + _DERIVED, pack_id="pack-b", principal=BOB, text="남의 것")
    result = _ingest468(stack, text="둘째 본문", source_id=sid)
    assert result["text_ingested"] is False and result["evidence_node"] is None, result
    assert result["retryable"] is False and result["reject_codes"] == ["derived_id_conflict"], result
    assert stack["graph"].get_node("TextUnit", _slot2(sid, "둘째 본문")) is None


def test_488_unreadable_textunit_source_check_stays_retryable(stack):
    sid = "claude/p/488-c"
    _ingest468(stack, text="첫 본문", source_id=sid)
    real = stack["graph"].get_nodes_by_id
    calls = []

    def flaky(node_id):
        # The ownership probe and the builder identity check read first and
        # succeed. The derivation check reads last and fails.
        calls.append(node_id)
        if len(calls) > 2:
            raise RuntimeError("read down")
        return real(node_id)

    with patch.object(stack["graph"], "get_nodes_by_id", side_effect=flaky):
        result = _ingest468(stack, text="둘째 본문", source_id=sid)
    assert len(calls) > 2, calls
    assert result["node_errors"] == [f"{sid}: {_CONFLICT_FIXED}"], result
    assert result["text_ingested"] is False, result
    assert result["retryable"] is True and "reject_codes" not in result, result
    assert stack["graph"].get_node("TextUnit", sid + _DERIVED) is None


def test_488_textunit_source_length_edges(stack):
    from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET

    no_slot_1 = "M" * (SOURCE_NODE_ID_BUDGET - len(_DERIVED) + 1)
    no_slot_2 = "N" * (SOURCE_NODE_ID_BUDGET - len("#text-") - 16 + 1)
    for sid in (no_slot_1, no_slot_2):
        _ingest468(stack, text="첫 본문", source_id=sid)
        _ingest468(stack, text="둘째 본문", source_id=sid)
    refused_1 = _ingest468(stack, text="둘째 본문", source_id=no_slot_1)
    assert refused_1["node_errors"] == [f"{no_slot_1}: {_CONFLICT_FIXED}"], refused_1
    assert refused_1["reject_codes"] == ["node_identity_conflict"], refused_1
    # Slot 1 holds the second text. A third text needs slot 2, which does not fit.
    assert stack["graph"].get_node("TextUnit", no_slot_2 + _DERIVED)["text"] == "둘째 본문"
    refused_2 = _ingest468(stack, text="셋째 본문", source_id=no_slot_2)
    assert refused_2["text_ingested"] is False, refused_2
    assert refused_2["reject_codes"] == ["derived_id_conflict"], refused_2


def test_488_textunit_source_text_survives_a_fork_and_a_retry_on_the_fork_target(stack):
    sid = "claude/p/488-d"
    meta = {"title": "포크 제목", "source": "pack_ingest"}
    _ingest468(stack, text="첫 본문", source_id=sid, metadata=meta)
    _ingest468(stack, text="둘째 본문", source_id=sid, metadata=meta)

    forked = _fork(stack, principal=ALICE, src_pack_id="pack-a")
    assert forked.get("status") == "ok", forked
    new_pack = forked["pack_id"]
    ids = {
        r["props"]["id"] for r in stack["graph"].export_nodes(pack_id=new_pack)
        if r["props"]["id"].startswith(sid)
    }
    source_copy = next(i for i in ids if "#text" not in i)
    slot1_copy = derive_text_id(source_copy)
    assert ids == {source_copy, slot1_copy}, ids
    before = stack["graph"].get_node("TextUnit", slot1_copy)
    assert before["source_id"] == source_copy and before["text"] == "둘째 본문"

    from opencrab.mcp.tools import _ingest_into_pack

    with principal_scope(ALICE), \
         patch("opencrab.mcp.tools._get_context", return_value=mcp_ctx_from(stack)):
        retry = _ingest_into_pack(new_pack, text="둘째 본문", source_id=source_copy, metadata=meta)
    assert retry["status"] == "ok" and retry["evidence_node"] == slot1_copy, retry
    assert stack["graph"].get_node("TextUnit", slot1_copy) == before


# ---------------------------------------------------------------------------
# #484: a node of the same request that a transient error kept out of the graph
# keeps its id. The text waits for a resend and does not take that id.
# ---------------------------------------------------------------------------

_DEFERRED = "deferred (node with this id not written)"


def _flaky_logentry(stack, shape: str, times: int = 1):
    """Make the first ``times`` LogEntry writes fail with a retryable error.

    ``receipt``: the graph upsert raises, and the builder reports
    ``graph: error: ...``. ``raise``: the builder raises GraphWriteUnavailable.
    ``probe``: the ownership probe of the node cannot verify. ``commit``: the
    graph upsert commits and then raises.
    """
    import opencrab.mcp.tools.pack as pack_mod

    calls = {"n": 0}

    def first_logentry(node_type):
        if node_type == "LogEntry" and calls["n"] < times:
            calls["n"] += 1
            return True
        return False

    if shape == "probe":
        real_probe = pack_mod._node_probe_conflict

        def probe(ctx, space, node_type, node_id, pack_id):
            if first_logentry(node_type):
                return "unverifiable"
            return real_probe(ctx, space, node_type, node_id, pack_id)

        return patch.object(pack_mod, "_node_probe_conflict", side_effect=probe)
    graph = stack["graph"]
    real_upsert = graph.upsert_node
    exc = GraphWriteUnavailable("graph down") if shape == "raise" else RuntimeError("graph busy")

    def upsert(*args, **kw):
        if first_logentry(kw.get("node_type")):
            if shape == "commit":
                real_upsert(*args, **kw)
            raise exc
        return real_upsert(*args, **kw)

    return patch.object(graph, "upsert_node", side_effect=upsert)


def _rel(value, sid: str):
    """``value`` with the ``sid`` prefix cut off, so two sessions compare equal."""
    return value[len(sid):] if isinstance(value, str) and value.startswith(sid) else value


def _shape(stack, sid: str) -> dict[str, Any]:
    """Graph nodes whose id starts with ``sid``: id suffix -> (type, property keys, text, source)."""
    out = {}
    for row in stack["graph"].export_nodes(pack_id="pack-a"):
        node_id = row["props"]["id"]
        if not node_id.startswith(sid):
            continue
        for node_type in ("LogEntry", "TextUnit"):
            node = stack["graph"].get_node(node_type, node_id)
            if node is not None:
                out[_rel(node_id, sid)] = (
                    node_type, frozenset(node), node.get("text"), _rel(node.get("source_id"), sid),
                )
    return out


@pytest.mark.parametrize("failure", ["receipt", "raise", "probe", "commit"])
def test_484_transient_node_failure_then_resend_converges(stack, failure):
    sid, clean_sid = "codex/s/484-a", "codex/s/clean-484-a"
    with _flaky_logentry(stack, failure):
        first = _ingest468(stack, **_session_payload("2026-10-13T00:00:00Z", "본문 484", sid))

    assert first["status"] == "partial", first
    assert first["retryable"] is True and "reject_codes" not in first, first
    assert first["text_ingested"] is False and first["evidence_node"] is None, first
    assert first["added_nodes"] == 0, first
    assert first["stores"]["evidence_node"] == _DEFERRED, first
    assert len(first["node_errors"]) == 2, first
    assert first["node_errors"][1].startswith(f"{sid} (evidence/TextUnit): "), first
    # The text took no id: the original id and slot 1 hold no TextUnit anywhere.
    assert stack["graph"].get_node("TextUnit", sid) is None
    if failure != "commit":
        assert stack["graph"].get_node("LogEntry", sid) is None
    assert stack["graph"].get_node("TextUnit", sid + _DERIVED) is None
    doc = stack["docs"].get_node_doc("evidence", sid)
    assert doc is None or doc["node_type"] == "LogEntry", doc
    assert stack["docs"].get_source(sid) is None

    second = _ingest468(stack, **_session_payload("2026-10-13T00:00:00Z", "본문 484", sid))
    assert second["status"] == "ok", second
    assert second["evidence_node"] == sid + _DERIVED, second
    assert stack["graph"].get_node("LogEntry", sid) is not None
    assert stack["docs"].get_node_doc("evidence", sid)["node_type"] == "LogEntry"
    assert stack["vector"].get_by_id(sid + _DERIVED) is not None

    # The state equals the state of a request that never failed.
    clean = _ingest468(stack, **_session_payload("2026-10-13T00:00:00Z", "본문 484", clean_sid))
    assert clean["status"] == "ok", clean
    clean_shape = _shape(stack, clean_sid)
    assert set(clean_shape) == {"", _DERIVED}, clean_shape
    assert _shape(stack, sid) == clean_shape


def test_484_legacy_branch_converges_like_a_clean_request(stack):
    sid, clean_sid = "codex/s/484-l", "codex/s/clean-484-l"
    payload = dict(text_as_node=False)
    with _flaky_logentry(stack, "receipt"):
        first = _ingest468(stack, **_session_payload("2026-10-13T00:00:00Z", "본문 484", sid), **payload)
    assert first["retryable"] is True and first["text_ingested"] is False, first
    assert first["stores"]["evidence_node"] == _DEFERRED, first
    assert stack["graph"].get_node("TextUnit", sid) is None
    assert stack["docs"].get_source(sid) is None

    second = _ingest468(stack, **_session_payload("2026-10-13T00:00:00Z", "본문 484", sid), **payload)
    clean = _ingest468(stack, **_session_payload("2026-10-13T00:00:00Z", "본문 484", clean_sid), **payload)
    for key in ("status", "added_nodes", "text_ingested", "evidence_node", "retryable", "reject_codes"):
        assert _rel(second.get(key), sid) == _rel(clean.get(key), clean_sid), (key, second, clean)
    assert stack["graph"].get_node("LogEntry", sid) is not None
    assert _shape(stack, sid) == _shape(stack, clean_sid)
    assert (stack["docs"].get_source(sid) is None) == (stack["docs"].get_source(clean_sid) is None)


def test_484_graph_landed_with_doc_failure_does_not_defer(stack):
    sid = "codex/s/484-d"
    docs = stack["docs"]
    real = docs.upsert_node_doc

    def upsert_node_doc(space, node_type, node_id, props):
        if node_type == "LogEntry":
            raise RuntimeError("doc down")
        return real(space, node_type, node_id, props)

    with patch.object(docs, "upsert_node_doc", side_effect=upsert_node_doc):
        first = _ingest468(stack, **_session_payload("2026-10-13T00:00:00Z", "본문 484", sid))
    assert first["status"] == "partial" and first["retryable"] is True, first
    assert first["evidence_node"] == sid + _DERIVED, first
    assert first["stores"]["evidence_node"] == f"ok (derived id for {sid})", first
    assert stack["graph"].get_node("LogEntry", sid) is not None


def test_484_failure_of_another_node_does_not_defer(stack):
    sid = "codex/s/484-o"
    graph = stack["graph"]
    real = graph.upsert_node

    def upsert(*args, **kw):
        if kw.get("node_type") == "Topic":
            raise RuntimeError("graph busy")
        return real(*args, **kw)

    with patch.object(graph, "upsert_node", side_effect=upsert):
        result = _ingest468(
            stack, text="본문 484", source_id=sid,
            nodes=[{"space": "concept", "node_type": "Topic", "node_id": "t-484-o",
                    "properties": {}}],
        )
    assert result["status"] == "partial" and result["retryable"] is True, result
    assert result["evidence_node"] == sid and result["text_ingested"] is True, result
    assert stack["graph"].get_node("TextUnit", sid) is not None


def test_484_repeated_failure_keeps_deferring_until_the_node_lands(stack):
    sid = "codex/s/484-r"
    payload = _session_payload("2026-10-13T00:00:00Z", "본문 484", sid)
    with _flaky_logentry(stack, "receipt", times=2):
        first = _ingest468(stack, **payload)
        second = _ingest468(stack, **payload)
    for result in (first, second):
        assert result["retryable"] is True and result["text_ingested"] is False, result
        assert result["stores"]["evidence_node"] == _DEFERRED, result
        assert stack["graph"].get_node("TextUnit", sid) is None
    third = _ingest468(stack, **payload)
    assert third["status"] == "ok" and third["evidence_node"] == sid + _DERIVED, third
    assert stack["graph"].get_node("LogEntry", sid) is not None


def test_484_same_id_failed_then_landed_in_one_request_does_not_defer(stack):
    sid = "codex/s/484-t"
    payload = _session_payload("2026-10-13T00:00:00Z", "본문 484", sid)
    payload["nodes"] = payload["nodes"] * 2
    with _flaky_logentry(stack, "receipt"):
        result = _ingest468(stack, **payload)
    assert result["status"] == "partial" and result["retryable"] is True, result
    assert result["evidence_node"] == sid + _DERIVED and result["text_ingested"] is True, result
    assert stack["graph"].get_node("LogEntry", sid) is not None
