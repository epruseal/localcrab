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

import pytest
from sqlalchemy import text as _sql_text

from opencrab.auth import Principal, principal_scope
from opencrab.config import Settings
from opencrab.ontology.builder import OntologyBuilder
from opencrab.ontology.query import HybridQuery
from opencrab.pack.ownership import create_pack
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
    assert receipt["stores"]["documents"].startswith("skipped"), receipt["stores"]
    assert receipt["stores"]["chromadb"].startswith("skipped"), receipt["stores"]
    assert stack["docs"].get_source("src-nograph") is None
    assert stack["vector"].get_by_id("src-nograph") is None


# ---------------------------------------------------------------------------
# 설계 §9 테스트 5·6·6b — 신원 가드가 그래프 축까지 본다
# ---------------------------------------------------------------------------


def test_a_graph_only_foreign_node_is_refused_before_any_write(stack):
    """그래프에만 다른 팩의 노드가 있는 경우.

    doc_sources 나 벡터에 foreign 행을 심으면 기존 `source_identity_conflict`
    가 이미 거절하므로 검출력이 없다. 그래프에만 심는 것이 이 테스트의 요점이다.
    """
    with principal_scope(BOB):
        # write_vector=False 로 심어야 "그래프에만 있는" 상태가 된다. 벡터까지
        # 쓰면 기존 `source_identity_conflict` 의 벡터 축이 먼저 거절해 새
        # 그래프 축의 검출력이 사라진다.
        stack["builder"].add_node(
            space="evidence", node_type="TextUnit", node_id="src-foreign",
            properties={"pack_id": "pack-b", "text": "남의 것"}, pack_id="pack-b",
            write_vector=False,
        )
    assert stack["vector"].get_by_id("src-foreign") is None, "심기 자체가 그래프 전용이어야 한다"

    with pytest.raises(ValueError, match="already attributed"):
        _write(stack, source_id="src-foreign")

    # 부분 저장 없음: 어느 스토어에도 행이 생기지 않는다.
    assert stack["docs"].get_source("src-foreign") is None
    assert stack["vector"].get_by_id("src-foreign") is None


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
    구현이 통과한다. 임의 키까지 세 개를 함께 본다.
    """
    _write(stack, text="첫 본문", source_id="src-replace",
           metadata={"title": "제목", "source": "출처", "reviewer_note": "메모"})
    before = _node(stack, "src-replace")
    assert before["title"] == "제목" and before["source"] == "출처"

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

    assert "skipped" in receipt["stores"]["graph"], receipt["stores"]
    assert receipt["stores"]["documents"].startswith("ok"), receipt["stores"]
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


# ---------------------------------------------------------------------------
# 설계 §9 테스트 13 — fork 옵트아웃
# ---------------------------------------------------------------------------


def test_write_graph_false_skips_the_graph_leg(stack):
    """fork 는 노드를 별도로 복사하므로 소스 단계가 노드를 덮어쓰면 안 된다."""
    receipt = _write(stack, source_id="src-fork", write_graph=False,
                     write_vector=False)
    assert receipt["stores"]["graph"] == "skipped (raw copy)", receipt["stores"]
    assert stack["graph"].get_nodes_by_id("src-fork") == []
    assert receipt["stores"]["documents"].startswith("ok")
