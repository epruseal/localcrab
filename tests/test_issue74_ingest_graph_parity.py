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


# ---------------------------------------------------------------------------
# 설계 §9 테스트 3·3b — 그래프가 있지만 쓰기 자체를 거절한다
# ---------------------------------------------------------------------------


class _GraphThatRejectsTheWrite:
    """`available=True` 인데 실제 쓰기(`upsert_node`)에서만 거부한다.

    신원 프로브(`get_node`/`get_nodes_by_id`)는 통과시켜 실패 지점을
    `add_node` 의 쓰기 시도 한 곳으로 정확히 좁힌다.
    """

    available = True

    def __init__(self, exc: Exception):
        self._exc = exc

    def get_node(self, node_type, node_id):  # noqa: ARG002
        return None

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        return []

    def upsert_node(self, **kwargs):  # noqa: ARG002
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


def test_generic_runtime_error_from_graph_write_leaves_doc_and_sql_residue(stack):
    """선언되지 않은 `RuntimeError` 는 `add_node` 의 두 번째(흡수) except 절로 빠진다.

    흡수되면 `add_node` 는 예외 없이 mongo/sql 을 계속 쓰고 dict 를 정상
    반환하므로, `_graph_leg` 가 그 `docs`/`sql` 키를 영수증에 병합한다. 이
    잔여(고아 doc_nodes 행 + sql 레지스트리 행)가 실제로 남는지 확인한다 —
    앞의 세 타입과 대칭되는, "흡수되면 잔여가 남는다"는 반대쪽 판정이다.
    """
    receipt = _write(stack, source_id="src-generic-reject",
                     graph=_GraphThatRejectsTheWrite(RuntimeError("boom, unrelated failure")))

    assert receipt["stores"]["graph"].startswith("error:"), receipt["stores"]
    assert receipt["stores"]["docs"].startswith("ok"), receipt["stores"]
    assert receipt["stores"]["sql"] == "ok", receipt["stores"]
    assert receipt["stores"]["documents"] == "skipped (graph write failed)"
    assert receipt["stores"]["chromadb"] == "skipped (graph write failed)"
    assert stack["docs"].get_source("src-generic-reject") is None
    assert stack["vector"].get_by_id("src-generic-reject") is None
    assert stack["docs"].get_node_doc("evidence", "src-generic-reject") is not None
    assert _sql_registry_has(stack, "src-generic-reject")


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


def test_rest_ingest_materialises_graph_node_and_all_store_rows(rest_client, rest_module, rest_auth):
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
    with ctx.sql._engine.begin() as conn:
        row = conn.execute(
            _sql_text("SELECT node_id FROM ontology_nodes WHERE node_id=:n"),
            {"n": "src-rest-1"},
        ).fetchone()
    assert row is not None, "sql 레지스트리에 노드 행이 없다"


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
        with sql._engine.begin() as conn:
            row = conn.execute(
                _sql_text("SELECT node_id FROM ontology_nodes WHERE node_id=:n"),
                {"n": source_id},
            ).fetchone()
        assert row is not None
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
    assert result.exit_code == 0, result.output
    assert "Ingested 0/1 files." in result.output, result.output
    assert "FAIL" in result.output, result.output


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
    """(d) 그래프가 흡수형 `RuntimeError` 로 실패해도 doc_nodes/sql 레지스트리에

    잔여(`docs`/`sql` 키의 "ok")가 남는다. 그 잔여는 `documents`/`chromadb`
    키가 아니므로 `legacy_text_landed` 를 절대 참으로 만들면 안 된다 — 잔여만
    보고 청구하면 실패한 적재에 과금하는 결함이 된다.
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

    assert result["stores"].get("docs", "").startswith("ok"), result["stores"]
    assert result["stores"].get("sql") == "ok", result["stores"]
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
    resp = rest_client.post(
        "/api/ingest",
        json={"text": "본문", "source_id": "src-6-rest"},
        headers=rest_auth,
    )
    assert resp.status_code >= 400, resp.text
    assert ctx.docs.get_source("src-6-rest") is None


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
