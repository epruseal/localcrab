"""폐기된 `properties.pack` 별칭 제거와 소유 태그 불변식 (#159, #171).

`pack` 은 `pack_id` 의 사본이었고 읽는 자리는 노드 벡터 메타의 `source` 하나뿐이었다.
`pack_id` 만 덮고 `pack` 은 보존하는 writer 들 때문에 한 행이 서로 다른 두 소유 태그를
가질 수 있었고, 그 행이 builder 를 지나면 벡터 `source` 가 옛 팩 이름으로 찍혔다.

이 모듈은 세 가지를 건다.

1. 축 제거 — 생산자와 소비자 양쪽(T1~T3b).
2. 소유 태그를 쓰는 모든 경로가 정규화를 지난다(T4~T6, T14~T15).
3. 그 상태를 **만들 수 없다**는 불변식이 범용 진입점에 걸려 있다(T11~T13).

여기에 회수 술어가 `pack_id` 단일 키라는 사실을 #171 의 과삭제/누락 반례로 고정하고
(T8), 라이브 잔여 별칭이 증분을 매번 재기록시키지 않는다는 것을 확인한다(T7).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.common.pack_tags import (
    RETIRED_KEYS,
    apply_pack_tag,
    canonicalize_pack_alias,
    strip_retired_keys,
)
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack import load as pack_load
from opencrab.pack import normalize as pack_normalize
from opencrab.pack.ownership import create_pack
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _owned_principal(sql, pack_id: str) -> Principal:
    """A real registered user who owns exactly ``pack_id`` (#148: every
    ``builder.add_node``/``add_edge`` call now runs through the write gate,
    which requires a bound principal AND real ownership of the target pack
    -- see ``opencrab.pack.write_gate.authorize``)."""
    user_id = create_user(sql, "tester")
    assigned = create_pack(sql, user_id, pack_id)
    assert assigned == pack_id, "collided with a pre-existing pack of the same id"
    return Principal(user_id=user_id, is_local=True, disabled=False)


# ---------------------------------------------------------------------------
# fixtures / stubs
# ---------------------------------------------------------------------------


class _CapturingVec:
    """벡터 축만 기록하는 스텁. `available` 은 True 라 builder 가 실제로 쓴다."""

    available = True

    def __init__(self) -> None:
        self.metadatas: list[dict] = []

    def upsert_texts(self, texts, ids, metadatas):   # noqa: ARG002
        self.metadatas.extend(metadatas)
        return list(ids)

    def get_by_id(self, doc_id):  # noqa: ARG002
        # #148: the identity guard probes the vector slot before writing.
        # No method at all reads as "cannot verify" (fail-closed) and the
        # write never happens, which would make this stub silently useless.
        return None


class _NoVec:
    available = False

    def delete(self, ids):                            # noqa: ARG002
        return None


@pytest.fixture
def live(tmp_path, monkeypatch):
    """진짜 SQLite 3스토어 + LOCAL_DATA_DIR 실재. 외부 의존 0."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
    assert graph.available
    yield OntologyBuilder(graph, docs, sql), graph, docs
    graph.close()
    docs.close()


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _node(**kw) -> dict:
    row = {"id": "n1", "label": "노드 하나", "node_type": "Document", "space": "resource"}
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# T1 / T2 — 생산자
# ---------------------------------------------------------------------------


def test_t1_transform_node_drops_the_retired_alias():
    """입력이 별칭을 실어 와도 산출 props 에 남지 않는다."""
    _s, _t, _i, props = pack_normalize.transform_node(
        "내팩", _node(properties={"pack": "남의팩", "pack_id": "남의팩"}))
    assert props["pack_id"] == "내팩"
    assert "pack" not in props, (
        "생산자가 폐기 별칭을 다시 쓴다 — 이 값이 stale 이면 벡터 source 가 옛 팩 이름이 된다")


def test_t2_transform_chunk_meta_drops_the_retired_alias():
    """청크 축도 같다. `source`(doc 축 레거시 폴백 소유 태그)는 건드리지 않는다."""
    meta = pack_normalize.transform_chunk_meta("내팩", {"id": "c1", "metadata": {"pack": "old"}})
    assert meta["pack_id"] == "내팩"
    assert meta["source"] == "내팩", "doc 축 폴백 소유 태그까지 지웠다 — 범위 밖 변경이다"
    assert "pack" not in meta


# ---------------------------------------------------------------------------
# T3 / T3b — 소비자(벡터 메타의 source)
# ---------------------------------------------------------------------------


def _vector_meta_for(props: dict, live_stores) -> dict:
    """불변식 chokepoint 를 **우회해** 벡터 메타 생성 자리만 겨냥한다.

    chokepoint 가 생긴 뒤로는 `add_node` 로 불일치 props 를 넣을 수 없다. 여기서
    검증하려는 것은 그 게이트가 아니라 "게이트를 통과한 props 로 메타를 만들 때
    어느 키를 보는가" 이므로, 게이트만 무력화하고 나머지 경로는 실물로 돌린다.

    #148: `add_node` 이제 그 chokepoint 앞에 두 단이 더 있다 -- `authorize`
    (실 레지스트리 소유권 판정, 이 헬퍼의 `sql` 은 `MagicMock(available=False)`
    라 무조건 거부한다) 와 `stamp` (payload 에 없던 `pack_id` 도 무조건 채워
    넣는다, T3b 가 겨냥하는 "pack_id 자체가 없는 행"을 이 헬퍼 안에서 원천적으로
    지워버린다). 이 두 단도 이 테스트의 대상이 아니므로 같은 방식으로 무력화한다
    -- 실물로 두면 T3b 의 "pack_id 없음"이라는 전제 자체가 stamp 단계에서
    사라진다.
    """
    _builder, graph, docs = live_stores
    vec = _CapturingVec()
    builder = OntologyBuilder(graph, docs, MagicMock(available=False), vec=vec)
    principal = Principal(user_id="test-user", is_local=True, disabled=False)
    with (
        patch("opencrab.ontology.builder.canonicalize_pack_alias", lambda tags: None),
        patch("opencrab.ontology.builder.authorize", lambda *a, **kw: None),
        patch("opencrab.ontology.builder.stamp", lambda payload, **kw: dict(payload or {})),
        principal_scope(principal),
    ):
        builder.add_node("resource", "Document", "n1", dict(props), pack_id="unused-gate-is-nulled")
    assert vec.metadatas, "벡터 메타가 안 만들어졌다 — 이 테스트의 전제가 깨졌다"
    return vec.metadatas[0]


def test_t3_vector_source_comes_from_pack_id_not_the_alias(live):
    meta = _vector_meta_for(
        {"pack": "old-name", "pack_id": "new-name", "title": "t"}, live)
    assert meta["source"] == "new-name", (
        "벡터 source 가 폐기 별칭에서 나왔다 — 팩 이름을 바꾸면 옛 이름이 그대로 찍힌다")
    assert meta["pack_id"] == "new-name"


def test_t3b_alias_only_row_gets_an_empty_vector_source(live):
    """의도된 동작 변경: `pack_id` 없는 행의 source 는 빈 문자열이다.

    그 행에 source 를 주던 유일한 근거가 지금 없애는 별칭이었다.
    """
    meta = _vector_meta_for({"pack": "only-alias", "title": "t"}, live)
    assert meta["source"] == "", (
        "별칭만 있는 행이 여전히 벡터 source 를 만든다 — 축이 살아 있다")


# ---------------------------------------------------------------------------
# T4 — mcp 팩 스탬핑
# ---------------------------------------------------------------------------


def _mcp_ctx(graph):
    builder = MagicMock()
    builder.add_node.return_value = {"stores": {"graph": "ok"}}
    builder.add_edge.return_value = {"stores": {"graph": "ok"}}
    mongo = MagicMock()
    mongo.available = True
    mongo.get_node_doc.return_value = None
    mongo.get_source.return_value = None
    chroma = MagicMock()
    chroma.available = True
    chroma.get_by_id.return_value = None
    return {
        "neo4j": graph, "mongo": mongo, "chroma": chroma, "sql": MagicMock(),
        "builder": builder, "hybrid": MagicMock(), "billing": MagicMock(),
    }


@pytest.mark.usefixtures("bind_test_principal")
def test_t4_mcp_ingest_normalises_every_axis(tmp_path, caplog):
    """노드·엣지·텍스트 메타 3축 모두 별칭을 버리고, 불일치는 경고로 드러난다."""
    from opencrab.mcp.tools import _ingest_into_pack

    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    # 엣지 축은 끝점 타입이 실제로 조회돼야 스탬핑까지 간다(끝점 미해결이면 그 앞에서
    # 거부된다). builder 가 MagicMock 이라 노드 쓰기가 라이브에 안 남으므로 직접 심는다.
    graph.upsert_node("Entity", "e1", {"pack_id": "my-pack"})
    graph.upsert_node("Entity", "e2", {"pack_id": "my-pack"})
    ctx = _mcp_ctx(graph)
    try:
        with caplog.at_level("WARNING"), patch(
                "opencrab.mcp.tools._get_context", return_value=ctx):
            _ingest_into_pack(
                "my-pack",
                nodes=[{"space": "concept", "node_type": "Entity", "node_id": "e1",
                        "properties": {"pack": "old"}}],
                edges=[{"from_space": "concept", "from_id": "e1", "relation": "part_of",
                        "to_space": "concept", "to_id": "e2",
                        "properties": {"pack": "old"}}],
                # text_as_node=False 로 레거시 텍스트 경로를 태운다 — 호출자 metadata 가
                # 벡터·doc_sources 로 그대로 흘러 들어가는 것이 그 경로다.
                text="본문", source_id="s1", metadata={"pack": "old"},
                text_as_node=False,
            )
    finally:
        graph.close()

    node_props = ctx["builder"].add_node.call_args.kwargs["properties"]
    edge_props = ctx["builder"].add_edge.call_args.kwargs["properties"]
    assert "pack" not in node_props and node_props["pack_id"] == "my-pack"
    assert "pack" not in edge_props and edge_props["pack_id"] == "my-pack"

    ingest_meta = ctx["hybrid"].ingest.call_args.kwargs["metadata"]
    assert "pack" not in ingest_meta and ingest_meta["pack_id"] == "my-pack"

    dropped_warnings = [r for r in caplog.records if "retired 'pack' alias" in r.message]
    assert len(dropped_warnings) == 3, (
        "3축 각각에서 불일치 별칭 폐기가 경고로 드러나야 한다 — 조용히 버리면 "
        f"호출자가 자기 오류를 모른다 (실제 {len(dropped_warnings)}건)")


# ---------------------------------------------------------------------------
# T5 — provenance backfill
# ---------------------------------------------------------------------------


def _scratch_graph_db(path: Path, props: dict) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE graph_nodes (node_type TEXT, node_id TEXT, properties TEXT)")
    conn.execute(
        "CREATE TABLE graph_edges (from_type TEXT, from_id TEXT, relation TEXT, "
        "to_type TEXT, to_id TEXT, properties TEXT)")
    conn.execute("INSERT INTO graph_nodes VALUES (?,?,?)",
                 ("Document", "n1", json.dumps(props)))
    conn.commit()
    conn.close()
    return path


def test_t5_provenance_backfill_drops_the_alias(tmp_path):
    from opencrab.ontology.pack_provenance import backfill_pack_ids

    db = _scratch_graph_db(tmp_path / "graph.db", {"pack": "old"})
    backfill_pack_ids(db, assume_pack_id="assumed-pack", dry_run=False)

    conn = sqlite3.connect(db)
    props = json.loads(conn.execute("SELECT properties FROM graph_nodes").fetchone()[0])
    conn.close()
    assert props["pack_id"] == "assumed-pack"
    assert "pack" not in props, (
        "backfill 이 pack_id 만 붙이고 별칭을 남겼다 — 그 행이 바로 #171 의 혼합 상태다")


# ---------------------------------------------------------------------------
# T6 — 엣지 적재
# ---------------------------------------------------------------------------


def test_t6_load_edges_drops_the_alias(live, tmp_path):
    builder, graph, _docs = live
    # #148: load_nodes/load_edges require a bound principal that actually
    # owns "pack-1" -- both call builder.add_node/add_edge internally,
    # which now runs through the real write gate (see _owned_principal).
    principal = _owned_principal(builder._sql, "pack-1")
    with principal_scope(principal):
        _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        pack_load.load_nodes("pack-1", tmp_path / "n.jsonl", builder, {})
        ef = _write_jsonl(tmp_path / "e.jsonl", [{
            "from_id": "n1", "to_id": "n2", "relation": "cites",
            "properties": {"pack": "old"},
        }])
        pack_load.load_edges("pack-1", ef, builder, {"n1": ("resource", "Document"),
                                                     "n2": ("resource", "Document")})

    row = graph._fetch_one(
        "SELECT properties FROM graph_edges WHERE from_id=:f", {"f": "n1"})
    props = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert props["pack_id"] == "pack-1"
    assert "pack" not in props


# ---------------------------------------------------------------------------
# T7 / T7b — 증분이 폐기 키 때문에 재기록되지 않는다
# ---------------------------------------------------------------------------


def test_t7_live_alias_does_not_make_every_node_changed(live, tmp_path):
    """라이브 행에 남은 폐기 키는 증분 대조에서 빠진다.

    빼지 않으면 그 키를 가진 행이 **매 증분 전량 chg** 로 잡힌다. 재기록으로
    지워지지도 않는다 — neo4j 의 upsert 는 전달된 키만 SET 하므로 그 재기록이
    영구히 반복된다.
    """
    builder, graph, docs = live
    # #148: load_nodes_incremental also requires a bound principal that
    # owns "pack-1" -- see test_t6's comment / _owned_principal.
    principal = _owned_principal(builder._sql, "pack-1")
    f = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1")])
    _s, _t, _i, props = pack_normalize.transform_node("pack-1", _node(id="n1"))
    live_nodes = {"n1": ("Document", "resource", {**props, "pack": "pack-1"})}

    with principal_scope(principal):
        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, live_nodes, graph, docs, {"n1": {"resource"}})
    assert (n_new, n_chg, n_same, skip, err) == (0, 0, 1, 0, 0), (
        "폐기 별칭 하나 때문에 동일한 행이 chg 로 잡혔다 — 매 증분 전량 재기록된다")


def test_t7b_live_alias_does_not_make_every_chunk_meta_changed(live, tmp_path):
    """청크 축은 별도 비교 로직이라 노드축 제외가 자동 적용되지 않는다."""
    _b, _g, docs = live
    row = {"id": "c1", "document_id": "n1", "text": "본문"}
    f = _write_jsonl(tmp_path / "chunks.jsonl", [row])
    meta = pack_normalize.transform_chunk_meta("pack-1", row)
    live_chunks = {"c1": ("본문", {**meta, "pack": "pack-1"})}

    c_new, c_txt, c_meta, c_same, err, _ids = pack_load.load_chunks_incremental(
        "pack-1", f, _NoVec(), docs, live_chunks)
    assert (c_new, c_txt, c_meta, c_same, err) == (0, 0, 0, 1, 0), (
        "폐기 별칭 하나 때문에 청크가 meta 갱신 경로로 흘렀다")


# ---------------------------------------------------------------------------
# T8 — 회수 술어는 pack_id 단일 키 (#171 의 과삭제/누락 반례)
# ---------------------------------------------------------------------------


def test_t8a_mixed_row_owned_by_another_pack_survives(live):
    """과삭제 반례: `pack_id=B` 인 행은 `pack=A` 를 달고 있어도 A 삭제에 안 걸린다."""
    _builder, graph, docs = live
    graph.upsert_node("Document", "mixed", {"pack_id": "pack-b", "pack": "pack-a"})

    pack_load.delete_pack("pack-a", graph, docs, _NoVec())

    assert graph.get_node("Document", "mixed") is not None, (
        "회수 술어가 폐기 별칭을 본다 — 다른 팩 소유 행이 함께 지워졌다(과삭제)")


def test_t8b_row_owned_by_the_named_pack_is_deleted_despite_a_foreign_alias(live):
    """누락 반례: `pack_id=A` 인 행은 `pack=B` 를 달고 있어도 A 삭제에 걸린다."""
    _builder, graph, docs = live
    graph.upsert_node("Document", "mine", {"pack_id": "pack-a", "pack": "pack-b"})

    pack_load.delete_pack("pack-a", graph, docs, _NoVec())

    assert graph.get_node("Document", "mine") is None, (
        "회수 술어가 별칭 쪽으로 좁혀졌다 — 자기 팩 소유 행이 회수되지 않았다(누락)")


def test_t8c_edge_reclaim_is_cascade_only_guard(live):
    """`delete_pack` 에는 엣지 회수 술어가 **없다** — 노드 삭제의 cascade 뿐이다.

    이것은 가드다(이 PR 의 수정을 원복해도 실패하지 않는다). 엣지 직접 회수 술어를
    신설하는 미래의 변경을 잡는 tripwire 이고, 그 변경은 별건(엣지 소유 축)이다.
    """
    _builder, graph, docs = live
    graph.upsert_node("Document", "b1", {"pack_id": "pack-b"})
    graph.upsert_node("Document", "b2", {"pack_id": "pack-b"})
    graph.upsert_edge("Document", "b1", "relates_to", "Document", "b2",
                      {"pack_id": "pack-a", "pack": "pack-a"})

    pack_load.delete_pack("pack-a", graph, docs, _NoVec())

    assert graph.get_edge("Document", "b1", "relates_to", "Document", "b2") is not None, (
        "양 끝점이 다른 팩인 엣지가 지워졌다 — 엣지 직접 회수 술어가 새로 생겼다. "
        "그것은 이 PR 의 범위가 아니라 엣지 소유 축의 별건이다")


# ---------------------------------------------------------------------------
# T9 / T10 — tripwire
# ---------------------------------------------------------------------------


def test_t9_no_writer_outside_pack_tags_assigns_the_retired_key():
    """보조 방어선이다 — **producer 완전성의 증명이 아니다.**

    dict 복사, `.update()`, 동적 키, `json_set` SQL, Cypher, 벡터 메타 경로는 이
    스캔이 못 잡는다. 진짜 강제는 범용 진입점의 불변식(T11~T13)이다. 여기서 잡는
    것은 "누가 리터럴 대입으로 별칭을 다시 쓰기 시작했다"는 가장 흔한 재발 형태다.
    `tests/` 는 스캔 대상이 아니다 — 반례 테스트가 그 픽스처를 정당하게 만든다.
    쓰기 코드가 사는 나머지 루트는 전부 본다.
    """
    allowed = REPO_ROOT / "opencrab" / "common" / "pack_tags.py"
    offenders = []
    for root in ("opencrab", "apps", "scripts", "server", "crabharness"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            if path == allowed:
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for form in ('["pack"] =', '["pack"]=', "['pack'] =", "['pack']="):
                    if form in stripped:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "폐기 별칭에 리터럴 대입하는 자리가 생겼다 — 소유 태그는 "
        f"opencrab/common/pack_tags.py 를 지나야 한다: {offenders}")


def test_t10_doc_owner_predicate_never_looks_at_the_retired_key():
    """doc 축 소유 술어는 `pack_id` 우선 + `source` 폴백뿐이다."""
    from opencrab.stores._sql_dialect import SQLITE

    pred = pack_load._doc_owner_pred(SQLITE)
    assert "$.pack_id" in pred and "$.source" in pred
    assert "'$.pack'" not in pred and '"$.pack"' not in pred, (
        "doc 축 회수 술어에 폐기 별칭 항이 되살아났다")


# ---------------------------------------------------------------------------
# T11 / T12 / T13 — 범용 진입점의 불변식
# ---------------------------------------------------------------------------


def test_t11_builder_rejects_disagreeing_ownership_tags(live):
    """소유 권위가 없는 진입점에서 두 태그가 다르면 거부한다(#171).

    #148: `add_node`/`add_edge` 는 이제 이 검사 앞에 실 소유권 게이트가 있다
    -- `pack_id` 파라미터로 넘긴 값과 payload 의 `pack_id` 가 (여기서는 "B"로)
    일치해야 `stamp` 를 무사히 지나 이 검사( canonicalize_pack_alias )에 실제로
    도달한다. `_owned_principal` 로 "B" 를 실제 소유해야 하는 이유가 그것이다.
    """
    builder, _graph, _docs = live
    principal = _owned_principal(builder._sql, "B")
    with principal_scope(principal):
        with pytest.raises(ValueError, match="retired alias"):
            builder.add_node("resource", "Document", "n1",
                             {"pack": "A", "pack_id": "B", "title": "t"}, pack_id="B")
        with pytest.raises(ValueError, match="retired alias"):
            builder.add_edge("resource", "n1", "cites", "resource", "n2",
                             properties={"pack": "A", "pack_id": "B"}, pack_id="B")


def test_t12_builder_normalises_the_redundant_alias_and_keeps_a_lone_one(live):
    """(a) 값이 같으면 중복 별칭만 버린다.

    (b) 는 #148 로 전제가 무너졌다: 원래는 "`pack_id` 를 아예 안 준 쓰기는
    `pack` 별칭이 임의 속성으로 그대로 남는다"였다. 그런데 #148 의 `stamp`
    (write_gate.py) 는 `add_node`/`add_edge` 의 이제-필수인 `pack_id` 키워드
    인자를 모든 쓰기에 무조건 못박는다 -- payload 에 `pack_id` 가 없었어도
    `stamp` 가 강제로 채워 넣는다. 그 결과 "쓰기 대상 팩이 없는 채로 `pack`
    별칭만 있는 행"은 이제 `add_node` 진입점을 통해서는 **만들 수 없는 상태**
    다: 채워지는 `pack_id` 가 별칭 값과 같으면 (a)와 동일하게 중복 제거되고,
    다르면 T11 처럼 거부된다 -- 그 사이의 "보존" 경로가 없다. 이 서브케이스는
    같은 파일의 T3b 가 겪은 것과 같은 종류의 계약 변화라 fake 로 우회하지
    않고, 실제로 지금 일어나는 일(= (a)와 합류)을 pin 한다."""
    builder, graph, _docs = live
    principal = _owned_principal(builder._sql, "A")

    with principal_scope(principal):
        props_same = {"pack": "A", "pack_id": "A", "title": "t"}
        builder.add_node("resource", "Document", "same", props_same, pack_id="A")
        stored_same = graph.get_node("Document", "same")
        assert stored_same["pack_id"] == "A" and "pack" not in stored_same

        # (b): no explicit `pack_id` in the payload, but #148 makes the
        # add_node `pack_id=` kwarg mandatory for every write, and `stamp`
        # fills it into props unconditionally. Naming the SAME pack this
        # write already targets ("A") makes it land exactly like (a) once
        # `stamp` has run -- confirming there is no third "preserved,
        # untouched" outcome left.
        props_lone = {"pack": "A", "title": "t"}
        builder.add_node("resource", "Document", "lone", props_lone, pack_id="A")
        stored_lone = graph.get_node("Document", "lone")
        assert stored_lone["pack_id"] == "A" and "pack" not in stored_lone, (
            "#148 이후 pack_id 없는 단독 별칭 쓰기는 stamp 가 채운 pack_id 와 "
            "합쳐져 (a)와 같은 중복-제거 경로로 합류해야 한다")


def test_t13_hybrid_ingest_rejects_before_the_store_try_and_before_early_return():
    """벡터 스토어가 없어도 검사는 돈다 — 조기 반환·넓은 try 보다 앞이라는 뜻이다."""
    from opencrab.ontology.query import HybridQuery

    hybrid = HybridQuery(MagicMock(available=False), MagicMock(available=False))
    with pytest.raises(ValueError, match="retired alias"):
        hybrid.ingest(text="본문", source_id="s1",
                      metadata={"pack": "A", "pack_id": "B"})


def test_t13b_rest_ingest_maps_the_invariant_violation_to_422(monkeypatch):
    """노드 엔드포인트가 이미 ValueError 에 주는 것과 같은 처분이다."""
    from fastapi import HTTPException

    from apps.api import main as api

    monkeypatch.setattr(api, "_enforce_ingest_limits", lambda *a, **kw: None)
    ctx = MagicMock()
    ctx.hybrid.ingest.side_effect = ValueError("properties.pack is a retired alias")
    auth = MagicMock(user_id="u1", tier="pro")

    with pytest.raises(HTTPException) as exc:
        api.ingest_text(api.IngestRequest(text="본문", source_id="s1"), auth, ctx)
    assert exc.value.status_code == 422, (
        f"불변식 위반이 클라이언트 오류가 아니라 {exc.value.status_code} 로 나갔다")


# ---------------------------------------------------------------------------
# T14 / T14b / T15 — 스크립트 writer
# ---------------------------------------------------------------------------


def test_t14_doc_backfill_sql_removes_the_alias_in_the_same_statement():
    """`_missing_and_set_sql` 의 set 식이 별칭까지 지운다(두 방언 모두).

    이 backfill 은 `pack_id` 가 **없는** 행만 고른다 — 별칭만 달고 있던 행이
    그대로 통과하면 나오는 것이 정확히 #171 의 혼합 상태다.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_mpo", REPO_ROOT / "scripts" / "migrate_pack_ownership.py")
    mpo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mpo)

    _missing, sqlite_set = mpo._missing_and_set_sql("properties", is_pg=False)
    _missing_pg, pg_set = mpo._missing_and_set_sql("properties", is_pg=True)
    assert "json_remove" in sqlite_set and "'$.pack'" in sqlite_set
    assert "- 'pack'" in pg_set

    # 방언 식이 실제로 어떻게 도는지 SQLite 로 확인한다(키 부재는 no-op).
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (properties TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (json.dumps({"pack": "A"}),))
    conn.execute("INSERT INTO t VALUES (?)", (json.dumps({"other": 1}),))
    conn.execute(f"UPDATE t SET properties = {sqlite_set.replace(':pid', '?')}",
                 ("target",))
    rows = [json.loads(r[0]) for r in conn.execute("SELECT properties FROM t")]
    conn.close()
    assert rows[0] == {"pack_id": "target"}, "별칭이 남아 혼합 행이 됐다"
    assert rows[1] == {"other": 1, "pack_id": "target"}, "키가 없을 때 no-op 이 아니다"


def test_t14b_chroma_uri_records_keep_the_alias_documented_limit():
    """알려진 한계를 고정한다 — 이 PR 이 고치는 것이 아니라 **기록**하는 것이다.

    `ChromaStore.upsert_texts` 는 uri 보유 레코드의 metadata 를 **merge** 한다
    (uri 를 살리려는 의도된 예외, #175/#186). 그래서 그런 레코드에서는 벡터
    backfill 이 폐기 별칭을 지우지 못한다. 잔여 별칭은 읽는 코드가 없어 무해하고,
    그 스크립트의 벡터 축은 이미 best-effort 로 선언돼 있다.

    이 단언이 깨지면 스토어의 uri 예외가 사라졌다는 뜻이고, 그때는 위 한계 서술이
    낡은 것이 된다.
    """
    doc = (REPO_ROOT / "opencrab" / "stores" / "chroma_store.py").read_text(
        encoding="utf-8")
    assert "uri-bearing records" in doc and "merge" in doc, (
        "chroma 의 uri merge 예외가 사라졌다 — migrate_pack_ownership 의 벡터 축 "
        "한계 주석과 이 테스트의 서술을 함께 갱신해야 한다")


class _FullReplaceVec:
    """`upsert_texts` 가 메타를 **통째로 교체**하는 벡터 스토어(sqlite-vec/pgvector,
    그리고 uri 없는 chroma 레코드의 동작). uri 보유 chroma 레코드의 merge 예외는
    T14b 가 따로 고정한다 — 두 성질을 한 스텁에 섞으면 어느 쪽이 깨졌는지 못 읽는다."""

    available = True

    def __init__(self, stored: dict[str, dict]) -> None:
        self.stored = stored

    def get_by_id(self, node_id):
        row = self.stored.get(node_id)
        return None if row is None else {"document": "본문", "metadata": dict(row)}

    def upsert_texts(self, documents, metadatas, ids):    # noqa: ARG002
        for node_id, meta in zip(ids, metadatas, strict=True):
            self.stored[node_id] = dict(meta)


def test_t16_vector_backfill_drops_the_alias():
    """벡터 축 backfill 도 별칭을 남기지 않는다.

    이 축은 SQL set 식(T14)과도, chroma 한계(T14b)와도 다른 세 번째 경로다 —
    파이썬 dict 를 읽어 고쳐 되쓴다.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_mpo_vec", REPO_ROOT / "scripts" / "migrate_pack_ownership.py")
    mpo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mpo)

    vec = _FullReplaceVec({"n1": {"pack": "A"}})
    summary = mpo._backfill_vector(vec, ["n1"], {"n1": "B"}, apply=True)

    assert summary == {"checked": 1, "missing": 1, "updated": 1}, (
        f"backfill 이 이 행을 처리하지 않았다 — 단언이 무의미해진다: {summary}")
    assert vec.stored["n1"] == {"pack_id": "B"}, (
        "벡터 메타에 폐기 별칭이 남았다 — pack_id 와 어긋난 혼합 행이다")


def test_t15_neo4j_import_script_does_not_synthesise_a_mixed_row():
    """`prepare_node`/`prepare_edge` 는 팩 파일 properties 에 `pack_id` 를 합성한다 —
    입력이 별칭을 달고 있으면 두 태그가 어긋난 행이 나간다."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_imp_neo4j", REPO_ROOT / "scripts" / "import_pack_graph_to_neo4j.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    _label, node = mod.prepare_node(
        {"id": "n1", "space": "resource", "node_type": "Document",
         "properties": {"pack": "old"}})
    assert "pack" not in node["props"] and node["props"]["pack_id"]
    assert node["props"]["source_id"], "다른 합성 키까지 지웠다"

    *_head, edge = mod.prepare_edge(
        {"id": "e1", "from_id": "n1", "to_id": "n2", "relation": "CONTAINS",
         "from_space": "resource", "to_space": "evidence",
         "properties": {"pack": "old"}})
    assert "pack" not in edge["props"] and edge["props"]["pack_id"]


# ---------------------------------------------------------------------------
# 헬퍼 자체
# ---------------------------------------------------------------------------


def test_helper_contract():
    assert RETIRED_KEYS == frozenset({"pack"})

    tags = {"pack": "old", "keep": 1}
    assert apply_pack_tag(tags, "new") == "old"
    assert tags == {"pack_id": "new", "keep": 1}
    assert apply_pack_tag({"pack": "same"}, "same") is None
    assert apply_pack_tag({}, "p") is None

    same = {"pack": "A", "pack_id": "A"}
    canonicalize_pack_alias(same)
    assert same == {"pack_id": "A"}

    lone = {"pack": "A"}
    canonicalize_pack_alias(lone)
    assert lone == {"pack": "A"}

    falsy = {"pack": "A", "pack_id": ""}
    canonicalize_pack_alias(falsy)
    assert falsy == {"pack": "A", "pack_id": ""}, (
        "falsy pack_id 는 소유 태그가 아니다 — 그것을 근거로 거부하면 안 된다")

    assert strip_retired_keys({"pack": "A", "b": 2}) == {"b": 2}
