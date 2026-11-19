"""issue #661: 거부된 적재 요청은 어느 저장소의 행도 바꾸지 않는다.

REST `/api/ingest`는 `resolve_write_pack`으로 쓰기 대상 팩을 정한 뒤 `write_source`를
부른다. 기본 팩이 없는 사용자(가입 때 기본 팩을 못 받은 레거시 행)에게는 이 해석이
기본 팩 레지스트리 행을 만든다. 그 뒤 `write_source`가 422로 거부해도 그 행은 남는다.
호출자는 422를 쓰기가 없었다는 뜻으로 읽는다.

이 파일은 로컬 데이터 디렉터리의 모든 sqlite 파일(그래프, 문서, 벡터, SQL 레지스트리,
빌링)을 행 단위로 비교한다. 기대값은 "거부 전후의 행 집합이 같다"이고, 같은 사용자의
정상 요청은 행을 만든다는 대조군도 함께 둔다.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sqlite3
import sys
from typing import Any

import pytest
from click.testing import CliRunner
from sqlalchemy import text as _sql_text

from opencrab.auth import bootstrap_local_user, create_user, issue_token
from opencrab.cli import main as _cli_main
from opencrab.pack.fork_remap import SOURCE_NODE_ID_BUDGET
from opencrab.pack.ownership import create_pack

_NODE_ID_DETAIL = "metadata.node_id must equal the source id"
_ALIAS_DETAIL = "retired alias of pack_id"
_FOREIGN_SOURCE_DETAIL = "source identity is already attributed to a different pack"
_FOREIGN_NODE_DETAIL = "identity is already attributed to a different pack"


def _all_rows(data_dir: pathlib.Path) -> dict[str, dict[str, list[str]]]:
    """데이터 디렉터리 아래 모든 sqlite 파일의 모든 표를 읽기 전용으로 읽는다."""
    out: dict[str, dict[str, list[str]]] = {}
    for path in sorted(data_dir.rglob("*")):
        if path.suffix not in {".db", ".sqlite3"}:
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            out[str(path.relative_to(data_dir))] = {
                t: sorted(repr(row) for row in con.execute(f'SELECT * FROM "{t}"'))  # noqa: S608
                for t in tables
            }
        finally:
            con.close()
    return out


def _changed(before: dict, after: dict) -> list[str]:
    """바뀐 표의 이름. 비어 있으면 행 집합이 같다."""
    return [
        f"{f}:{t}"
        for f in sorted(set(before) | set(after))
        for t in sorted(set(before.get(f, {})) | set(after.get(f, {})))
        if before.get(f, {}).get(t) != after.get(f, {}).get(t)
    ]


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")
    # 무료 티어는 남의 source_id를 403으로 먼저 거절한다. 이 파일은 422 경로를 본다.
    monkeypatch.setenv("OPENCRAB_TIER", "pro")
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def rest(data_dir):
    """(TestClient, 앱 컨텍스트). 앱은 파일 경로로 불러온다(apps는 패키지가 아니다)."""
    from fastapi.testclient import TestClient

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "api_main_issue661", repo_root / "apps" / "api" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        yield client, module.app.state.context


def _legacy_user(sql: Any, name: str) -> tuple[str, dict[str, str]]:
    """기본 팩이 없는 사용자와 그의 토큰 헤더. `create_user` 이전 행의 모양이다."""
    user_id = f"user_{name}_661"
    with sql._engine.begin() as conn:
        conn.execute(
            _sql_text(
                "INSERT INTO users (user_id, display_name, is_local) VALUES (:u, :n, 0)"
            ),
            {"u": user_id, "n": name},
        )
    _token_id, secret = issue_token(sql, user_id)
    return user_id, {"Authorization": f"Bearer {secret}"}


def _user(sql: Any, name: str) -> tuple[str, dict[str, str]]:
    """가입 때 기본 팩을 받은 사용자와 그의 토큰 헤더."""
    user_id = create_user(sql, name)
    _token_id, secret = issue_token(sql, user_id)
    return user_id, {"Authorization": f"Bearer {secret}"}


def _packs_of(sql: Any, user_id: str) -> list[str]:
    with sql._engine.connect() as conn:
        rows = conn.execute(
            _sql_text("SELECT pack_id FROM packs WHERE owner_id = :o"), {"o": user_id}
        ).fetchall()
    return [r[0] for r in rows]


def _ingest(client, headers, *, source_id="src-661", text="본문", metadata=None):
    body: dict[str, Any] = {"text": text, "source_id": source_id}
    if metadata is not None:
        body["metadata"] = metadata
    return client.post("/api/ingest", json=body, headers=headers)


# ---------------------------------------------------------------------------
# 결정적 거부: 요청만으로 정해지는 거부
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata, detail",
    [
        ({"node_id": "no-such-node-661"}, _NODE_ID_DETAIL),
        ({"pack": "some-other-pack"}, _ALIAS_DETAIL),
    ],
    ids=["display-node-id", "retired-pack-alias"],
)
def test_a_rejected_ingest_from_a_user_without_a_default_pack_changes_no_row(
    rest, data_dir, metadata, detail,
):
    client, ctx = rest
    user_id, headers = _legacy_user(ctx.sql, "legacy")
    before = _all_rows(data_dir)

    resp = _ingest(client, headers, metadata=metadata)

    assert resp.status_code == 422, resp.text
    assert detail in resp.json()["detail"], resp.text
    assert _packs_of(ctx.sql, user_id) == []
    assert _changed(before, _all_rows(data_dir)) == []

    # 대조군: 같은 사용자의 정상 요청은 쓰고, 그때 기본 팩이 하나 생긴다.
    ok = _ingest(client, headers)
    assert ok.status_code == 200, ok.text
    assert len(_packs_of(ctx.sql, user_id)) == 1
    assert _changed(before, _all_rows(data_dir)) != []


@pytest.mark.parametrize(
    "raw_body",
    [
        '{"text": "본문", "source_id": "src-661", "metadata": {"title": "\\ud800"}}',
        '{"text": "본문", "source_id": "src-661", "metadata": {"title": NaN}}',
        '{"text": "본문", "source_id": "src-661", "metadata": {"source": Infinity}}',
    ],
    ids=["lone-surrogate-title", "nan-title", "infinite-source"],
)
def test_a_body_the_graph_write_refuses_leaves_no_row_and_gets_the_same_answer(
    rest, data_dir, raw_body,
):
    """`GraphPropertyValidationError`는 그래프 쓰기가 저장소를 읽기 전에 던진다. 사전 검사가 따라야 한다."""
    client, ctx = rest
    member_id, member_headers = _user(ctx.sql, "member")
    legacy_id, legacy_headers = _legacy_user(ctx.sql, "legacy")
    json_headers = {"Content-Type": "application/json"}
    before = _all_rows(data_dir)

    expected = client.post(
        "/api/ingest", content=raw_body.encode("utf-8"),
        headers={**member_headers, **json_headers},
    )
    after_member = _all_rows(data_dir)
    resp = client.post(
        "/api/ingest", content=raw_body.encode("utf-8"),
        headers={**legacy_headers, **json_headers},
    )

    assert expected.status_code == 422, expected.text
    assert _changed(before, after_member) == []
    assert resp.status_code == 422, resp.text
    assert resp.json() == expected.json()
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_the_alias_answer_differs_only_in_the_pack_id_it_names(rest, data_dir):
    """별칭 규칙의 메시지는 pack_id를 담는다. 기본 팩이 없으면 그 id는 아직 없는 임시 id다."""
    client, ctx = rest
    member_id, member_headers = _user(ctx.sql, "member")
    _legacy_id, legacy_headers = _legacy_user(ctx.sql, "legacy")
    bad = {"pack": "some-other-pack"}

    expected = _ingest(client, member_headers, metadata=bad).json()["detail"]
    got = _ingest(client, legacy_headers, metadata=bad).json()["detail"]

    real_id = _packs_of(ctx.sql, member_id)[0]
    assert real_id in expected
    assert re.sub(r"default-[0-9a-f]{16}", real_id, got) == expected


def test_a_request_that_breaks_two_rules_gets_the_source_slot_answer_first(rest, data_dir):
    """`write_source`는 남의 source 슬롯을 먼저 보고 다음에 입력 검사를 한다. 같은 순서여야 한다."""
    client, ctx = rest
    _owner_id, owner_headers = _user(ctx.sql, "owner")
    assert _ingest(client, owner_headers, text="남의 본문").status_code == 200
    _member_id, member_headers = _user(ctx.sql, "member")
    legacy_id, legacy_headers = _legacy_user(ctx.sql, "legacy")
    bad = {"node_id": "no-such-node-661"}

    expected = _ingest(client, member_headers, metadata=bad)
    resp = _ingest(client, legacy_headers, metadata=bad)

    assert expected.status_code == 422, expected.text
    assert _FOREIGN_SOURCE_DETAIL in expected.json()["detail"], expected.text
    assert resp.json() == expected.json()
    assert _packs_of(ctx.sql, legacy_id) == []


def test_a_rejected_ingest_from_a_user_with_a_default_pack_keeps_its_old_answer(rest, data_dir):
    client, ctx = rest
    _user_id, headers = _user(ctx.sql, "member")
    before = _all_rows(data_dir)

    resp = _ingest(client, headers, metadata={"node_id": "no-such-node-661"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == (
        "src-661: metadata.node_id must equal the source id "
        "(the source is stored as the graph node of that id)"
    )
    assert _changed(before, _all_rows(data_dir)) == []


# ---------------------------------------------------------------------------
# 저장 상태에 따라 정해지는 거부: 남의 id
# ---------------------------------------------------------------------------


def test_a_foreign_source_id_is_rejected_without_creating_a_default_pack(rest, data_dir):
    client, ctx = rest
    _owner_id, owner_headers = _user(ctx.sql, "owner")
    assert _ingest(client, owner_headers, text="남의 본문").status_code == 200
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert resp.status_code == 422, resp.text
    assert _FOREIGN_SOURCE_DETAIL in resp.json()["detail"], resp.text
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_a_foreign_graph_node_with_the_same_id_is_rejected_without_creating_a_default_pack(
    rest, data_dir,
):
    """소스 슬롯(doc, vector)은 비어 있고 그래프에만 남의 노드가 있는 경우."""
    client, ctx = rest
    owner_id, _owner_headers = _user(ctx.sql, "owner")
    create_pack(ctx.sql, owner_id, "pack-owner-661")
    ctx.graph.upsert_node(
        node_type="TextUnit", node_id="src-661",
        properties={"pack_id": "pack-owner-661", "text": "남의 노드"}, space_id="evidence",
    )
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert resp.status_code == 422, resp.text
    assert _FOREIGN_NODE_DETAIL in resp.json()["detail"], resp.text
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_a_foreign_doc_nodes_row_alone_rejects_an_id_inside_the_budget(rest, data_dir):
    """노드 슬롯 탐침의 doc 쪽만 걸리는 경우. source 슬롯과 그래프에는 남의 행이 없다."""
    client, ctx = rest
    ctx.docs.upsert_node_doc("evidence", "TextUnit", "src-661", {"pack_id": "pack-b", "text": "남의 것"})
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert resp.status_code == 422, resp.text
    assert _FOREIGN_NODE_DETAIL in resp.json()["detail"], resp.text
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_a_foreign_graph_node_rejects_an_id_past_the_node_budget(rest, data_dir):
    """예산을 넘는 id라도 그래프에 노드가 이미 있으면 쓰기는 노드 단계를 건너뛰지 않는다."""
    client, ctx = rest
    over = "G" * (SOURCE_NODE_ID_BUDGET + 1)
    owner_id, _owner_headers = _user(ctx.sql, "owner")
    create_pack(ctx.sql, owner_id, "pack-owner-661")
    ctx.graph.upsert_node(
        node_type="TextUnit", node_id=over,
        properties={"pack_id": "pack-owner-661", "text": "남의 노드"}, space_id="evidence",
    )
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    before = _all_rows(data_dir)

    resp = _ingest(client, headers, source_id=over)

    assert resp.status_code == 422, resp.text
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_an_untagged_node_of_another_type_rejects_without_creating_a_default_pack(rest, data_dir):
    """같은 id를 다른 타입의 무태그 노드가 가진다. 저장소의 일반 삽입 경로가 거부하는 경우다."""
    client, ctx = rest
    ctx.graph.upsert_node(
        node_type="Entity", node_id="src-661", properties={"text": "무태그 노드"}, space_id="concept",
    )
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    _member_id, member_headers = _user(ctx.sql, "member")
    expected = _ingest(client, member_headers)
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert expected.status_code == 422, expected.text
    assert resp.status_code == 422, resp.text
    assert resp.json() == expected.json()
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_a_text_unit_row_whose_digest_cannot_be_read_rejects_without_creating_a_default_pack(
    rest, data_dir,
):
    """space가 문자열이 아닌 직접 쓴 행. digest를 못 읽으면 쓰기는 일반 삽입 경로로 가서 거부된다."""
    client, ctx = rest
    graph_db = data_dir / "graph.db"
    con = sqlite3.connect(graph_db)
    try:
        table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%graph_nodes'"
        ).fetchone()[0]
        con.execute(
            f'INSERT INTO "{table}" (node_type, node_id, space_id, properties) VALUES (?, ?, NULL, ?)',  # noqa: S608
            ("TextUnit", "src-661", '{"text": "직접 쓴 행", "space": 5}'),
        )
        con.commit()
    finally:
        con.close()
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    _member_id, member_headers = _user(ctx.sql, "member")
    expected = _ingest(client, member_headers)
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert expected.status_code == 422, expected.text
    assert resp.status_code == 422, resp.text
    assert resp.json() == expected.json()
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_a_failing_digest_read_is_judged_like_the_write_does(rest, data_dir, monkeypatch):
    """CAS 토큰을 못 읽으면(읽기 예외) 쓰기는 일반 삽입 경로로 가서 거부된다. 사전 검사도 같다."""
    client, ctx = rest
    ctx.graph.upsert_node(
        node_type="TextUnit", node_id="src-661", properties={"text": "무태그 옛 노드"}, space_id="evidence",
    )

    def _raiser(self, *args, **kwargs):  # noqa: ARG001
        raise RuntimeError("digest read failed")

    monkeypatch.setattr(type(ctx.graph), "get_node_digest", _raiser)
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    _member_id, member_headers = _user(ctx.sql, "member")
    expected = _ingest(client, member_headers)
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert expected.status_code == 422, expected.text
    assert resp.status_code == 422, resp.text
    assert resp.json() == expected.json()
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_the_preflight_does_not_reject_when_no_graph_can_be_read():
    """그래프가 없거나 available이 거짓이면 쓰기는 속성 정규화 없이 영수증으로 답한다. 사전 검사는 이 요청을 거부하지 않는다. `available`이 없는 객체 사례는 사전 검사의 동작만 고정한다."""
    from types import SimpleNamespace

    from opencrab.auth import Principal
    from opencrab.pack.source_writer import preflight_source_write

    principal = Principal(user_id="user_x_661", is_local=False, disabled=False)

    assert preflight_source_write(
        None, None, None, text="본문", source_id="src-661", metadata=None,
        principal=principal, pack_id="default-new-661",
    ) is None
    # 본문이 무효여도 그래프가 없으면 add_node는 그래프 저장소의 속성 정규화(prepare_node)를
    # 거치지 않고 영수증을 돌려준다.
    assert preflight_source_write(
        None, None, None, text="본문", source_id="src-661",
        metadata={"title": float("nan")},
        principal=principal, pack_id="default-new-661",
    ) is None
    # 저장소 객체가 있어도 사용할 수 없으면(available=False) 같은 답이다.
    assert preflight_source_write(
        None, None, SimpleNamespace(available=False), text="본문", source_id="src-661",
        metadata={"title": float("nan")},
        principal=principal, pack_id="default-new-661",
    ) is None
    # available 속성이 없는 객체도 사용할 수 없는 저장소로 본다.
    assert preflight_source_write(
        None, None, SimpleNamespace(), text="본문", source_id="src-661",
        metadata={"title": float("nan")},
        principal=principal, pack_id="default-new-661",
    ) is None


def test_an_id_past_the_node_budget_whose_rows_cannot_be_read_is_rejected():
    """예산을 넘는 id라도 행 목록을 읽지 못하면 쓰기는 우회로를 타지 않고 소유 확인 불가로 거부한다.

    `None`(읽지 못함)은 `[]`(행 없음)와 다르다. 사전 검사도 같은 답을 낸다.
    """
    from types import SimpleNamespace

    from opencrab.auth import Principal
    from opencrab.pack.source_writer import preflight_source_write

    def _raiser(_node_id):
        raise RuntimeError("by-id read failed")

    graph = SimpleNamespace(available=True, get_node=lambda *_a: None, get_nodes_by_id=_raiser)
    principal = Principal(user_id="user_x_661", is_local=False, disabled=False)

    with pytest.raises(ValueError, match="cannot verify existing ownership"):
        preflight_source_write(
            None, None, graph, text="본문", source_id="G" * (SOURCE_NODE_ID_BUDGET + 1),
            metadata=None, principal=principal, pack_id="default-new-661",
        )


def test_a_foreign_vector_row_alone_rejects_an_id_past_the_node_budget():
    """예산을 넘는 id는 노드 단계를 건너뛴다. 남의 벡터 행은 1단계 source 탐침만 잡는다."""
    from types import SimpleNamespace

    from opencrab.auth import Principal
    from opencrab.pack.source_writer import preflight_source_write

    graph = SimpleNamespace(available=True, get_nodes_by_id=lambda _node_id: [])
    vector = SimpleNamespace(
        available=True, get_by_id=lambda _id: {"metadata": {"pack_id": "pack-b"}},
    )
    principal = Principal(user_id="user_x_661", is_local=False, disabled=False)

    with pytest.raises(ValueError, match=_FOREIGN_SOURCE_DETAIL):
        preflight_source_write(
            None, vector, graph, text="본문", source_id="G" * (SOURCE_NODE_ID_BUDGET + 1),
            metadata=None, principal=principal, pack_id="default-new-661",
        )


@pytest.mark.parametrize("probe", ["get_source", "get_node_doc"])
def test_an_unverifiable_ownership_probe_rejects_without_creating_a_default_pack(
    rest, data_dir, monkeypatch, probe,
):
    """소유를 확인할 수 없으면(탐침 예외) 쓰기는 422로 거부한다. 사전 검사도 같은 답을 내고 행을 남기지 않는다."""
    client, ctx = rest

    def _raiser(self, *args, **kwargs):  # noqa: ARG001
        raise RuntimeError("probe failed")

    monkeypatch.setattr(type(ctx.docs), probe, _raiser)
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    _member_id, member_headers = _user(ctx.sql, "member")
    expected = _ingest(client, member_headers)
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert expected.status_code == 422, expected.text
    assert "cannot verify existing ownership" in expected.json()["detail"]
    assert resp.status_code == 422, resp.text
    assert resp.json() == expected.json()
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def test_an_untagged_text_unit_row_with_a_healthy_digest_is_still_accepted(rest, data_dir):
    """대조군: 같은 타입의 무태그 행은 CAS 경로로 이어받는다. 새 거부가 이것을 막으면 안 된다."""
    client, ctx = rest
    ctx.graph.upsert_node(
        node_type="TextUnit", node_id="src-661", properties={"text": "무태그 옛 노드"}, space_id="evidence",
    )
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")

    resp = _ingest(client, headers)

    assert resp.status_code == 200, resp.text
    assert len(_packs_of(ctx.sql, legacy_id)) == 1


def test_an_id_past_the_node_budget_is_not_over_rejected_by_the_admission(rest, data_dir):
    """예산을 넘는 id는 그래프 노드를 만들지 않으므로 doc_nodes의 남의 행은 보지 않는다.

    `write_source`의 이 우회로를 사전 검사가 따르지 않으면, 정상 요청이 거부된다.
    """
    client, ctx = rest
    over = "G" * (SOURCE_NODE_ID_BUDGET + 1)
    ctx.docs.upsert_node_doc("evidence", "TextUnit", over, {"pack_id": "pack-b", "text": "남의 것"})
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")

    resp = _ingest(client, headers, source_id=over)

    assert resp.status_code == 200, resp.text
    assert len(_packs_of(ctx.sql, legacy_id)) == 1


def test_validate_node_input_refuses_a_pack_alias_that_disagrees_with_the_pack_id():
    """`add_node`와 사전 검사가 함께 쓰는 순수 검사. 별칭 불일치는 쓰기 전에 거부된다."""
    from opencrab.auth import Principal
    from opencrab.ontology.builder import validate_node_input

    principal = Principal(user_id="user_x_661", is_local=False, disabled=False)
    with pytest.raises(ValueError, match="retired alias"):
        validate_node_input(
            {"pack_id": "p-1", "pack": "p-2", "text": "t"},
            space="evidence", node_type="TextUnit",
            principal=principal, pack_id="p-1", origin="client",
        )
    props = validate_node_input(
        {"pack_id": "p-1", "text": "t"},
        space="evidence", node_type="TextUnit",
        principal=principal, pack_id="p-1", origin="client",
    )
    assert props["pack_id"] == "p-1"


# ---------------------------------------------------------------------------
# CLI ingest: 같은 훅을 쓴다
# ---------------------------------------------------------------------------


def test_a_rejected_cli_ingest_for_a_local_user_without_a_default_pack_changes_no_row(
    data_dir, src_dir,
):
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_doc_store, make_sql_store, make_vector_store

    sql = make_sql_store(get_settings())
    user_id, _secret = bootstrap_local_user(sql)
    with sql._engine.begin() as conn:
        conn.execute(_sql_text("DELETE FROM packs WHERE owner_id = :o"), {"o": user_id})
    src = src_dir / "doc.txt"
    src.write_text("CLI 본문", encoding="utf-8")
    source_id = str(src.resolve())
    docs = make_doc_store(get_settings())
    vector = make_vector_store(get_settings())
    try:
        docs.upsert_source(source_id, "남의 본문", {"pack_id": "pack-b", "user_id": "someone-else"})
    finally:
        docs.close()
        vector.close()
    # 저장소를 한 번 열어 두는 빈 파일 호출로, 열기만으로 생기는 행을 기준선에 넣는다.
    blank = src_dir / "blank.txt"
    blank.write_text("   ", encoding="utf-8")
    assert CliRunner().invoke(_cli_main, ["ingest", str(blank)]).exit_code == 0
    before = _all_rows(data_dir)

    result = CliRunner().invoke(_cli_main, ["ingest", str(src)])

    assert result.exit_code != 0, result.output
    # rich는 긴 경로 뒤에서 줄을 바꾼다. 공백을 정리한 뒤 비교한다.
    assert _FOREIGN_SOURCE_DETAIL in " ".join(result.output.split()), result.output
    assert _packs_of(sql, user_id) == []
    assert _changed(before, _all_rows(data_dir)) == []

    # 대조군: 같은 사용자의 정상 파일은 적재되고, 그때 기본 팩이 하나 생긴다.
    good = src_dir / "good.txt"
    good.write_text("정상 본문", encoding="utf-8")
    ok = CliRunner().invoke(_cli_main, ["ingest", str(good)])
    assert ok.exit_code == 0, ok.output
    assert len(_packs_of(sql, user_id)) == 1
    sql._engine.dispose()


def _cli_legacy_local_user(data_dir, src_dir):
    """기본 팩이 없는 로컬 사용자와 파일 하나. 저장소를 한 번 열어 기준선 행을 만든다."""
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    sql = make_sql_store(get_settings())
    user_id, _secret = bootstrap_local_user(sql)
    with sql._engine.begin() as conn:
        conn.execute(_sql_text("DELETE FROM packs WHERE owner_id = :o"), {"o": user_id})
    blank = src_dir / "blank.txt"
    blank.write_text("   ", encoding="utf-8")
    assert CliRunner().invoke(_cli_main, ["ingest", str(blank)]).exit_code == 0
    return sql, user_id


@pytest.mark.parametrize("plant", ["vector", "graph"])
def test_a_rejected_cli_ingest_judges_the_vector_and_graph_slots_too(
    data_dir, src_dir, monkeypatch, plant,
):
    """CLI 훅이 doc 저장소만이 아니라 벡터와 그래프도 사전 검사에 넘긴다."""
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_graph_store, make_vector_store

    sql, user_id = _cli_legacy_local_user(data_dir, src_dir)
    src = src_dir / "doc.txt"
    src.write_text("CLI 본문", encoding="utf-8")
    source_id = str(src.resolve())
    if plant == "vector":
        vector = make_vector_store(get_settings())
        try:
            monkeypatch.setattr(
                type(vector), "get_by_id",
                lambda _self, _id: {"metadata": {"pack_id": "pack-b"}},
            )
        finally:
            vector.close()
        detail = _FOREIGN_SOURCE_DETAIL
    else:
        graph = make_graph_store(get_settings())
        graph.upsert_node(
            node_type="TextUnit", node_id=source_id,
            properties={"pack_id": "pack-b", "text": "남의 노드"}, space_id="evidence",
        )
        if hasattr(graph, "close"):
            graph.close()
        detail = _FOREIGN_NODE_DETAIL
    before = _all_rows(data_dir)

    result = CliRunner().invoke(_cli_main, ["ingest", str(src)])

    assert result.exit_code != 0, result.output
    assert detail in " ".join(result.output.split()), result.output
    assert _packs_of(sql, user_id) == []
    assert _changed(before, _all_rows(data_dir)) == []
    sql._engine.dispose()


def test_the_cli_hook_runs_under_the_write_lock_with_the_request_and_a_throwaway_id(
    data_dir, src_dir, monkeypatch,
):
    """검사는 쓰기 잠금 안에서, 파일 본문과 기본 팩 모양의 임시 id로 돈다."""
    import contextlib

    import opencrab.locking as locking
    import opencrab.pack.source_writer as source_writer

    sql, user_id = _cli_legacy_local_user(data_dir, src_dir)
    depth = {"n": 0}
    real_lock = locking.write_lock

    @contextlib.contextmanager
    def _counting_lock(*args, **kwargs):
        with real_lock(*args, **kwargs):
            depth["n"] += 1
            try:
                yield
            finally:
                depth["n"] -= 1

    seen: list[tuple[int, str, str]] = []
    real_preflight = source_writer.preflight_source_write

    def _recording(*args, **kwargs):
        seen.append((depth["n"], kwargs["text"], kwargs["pack_id"]))
        return real_preflight(*args, **kwargs)

    monkeypatch.setattr(locking, "write_lock", _counting_lock)
    monkeypatch.setattr(source_writer, "preflight_source_write", _recording)
    good = src_dir / "good.txt"
    good.write_text("정상 본문", encoding="utf-8")

    ok = CliRunner().invoke(_cli_main, ["ingest", str(good)])

    assert ok.exit_code == 0, ok.output
    assert len(seen) == 1, seen
    held, text, throwaway = seen[0]
    assert held == 1
    assert text == "정상 본문"
    assert re.fullmatch(r"default-[0-9a-f]{16}", throwaway), throwaway
    assert _packs_of(sql, user_id) != [throwaway]
    sql._engine.dispose()


# ---------------------------------------------------------------------------
# 판정 줄 전수 변이 실행(runs/mutation-table.txt)이 찾은 시험 공백
# ---------------------------------------------------------------------------


@pytest.fixture()
def schema_calls(monkeypatch):
    """`validate_node_properties`가 받은 (node_type, 속성) 호출을 기록한다."""
    import opencrab.ontology.builder as builder

    calls: list[tuple[str, dict[str, Any]]] = []
    real = builder.validate_node_properties

    def _record(node_type, props):
        calls.append((node_type, dict(props)))
        return real(node_type, props)

    monkeypatch.setattr(builder, "validate_node_properties", _record)
    return calls


def _principal():
    from opencrab.auth import Principal

    return Principal(user_id="user_x_661", is_local=False, disabled=False)


def test_the_request_body_and_a_throwaway_pack_id_reach_the_node_schema_check(
    rest, data_dir, schema_calls,
):
    """REST 훅은 요청 본문과 기본 팩 모양의 임시 id를 사전 검사에 넘긴다. 쓰기는 저장될 id를 쓴다."""
    client, ctx = rest
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")

    resp = _ingest(client, headers, text="본문 661")

    assert resp.status_code == 200, resp.text
    node_calls = [props for node_type, props in schema_calls if node_type == "TextUnit"]
    assert len(node_calls) == 2, node_calls
    preflight_props, write_props = node_calls
    assert preflight_props["text"] == write_props["text"] == "본문 661"
    assert re.fullmatch(r"default-[0-9a-f]{16}", preflight_props["pack_id"]), preflight_props
    assert preflight_props["pack_id"] != write_props["pack_id"]
    assert _packs_of(ctx.sql, legacy_id) == [write_props["pack_id"]]


def test_the_preflight_runs_the_node_schema_check_on_the_properties_of_the_request(schema_calls):
    """스키마 팩이 TextUnit 스키마를 설치하면 add_node가 거부할 수 있다. 사전 검사도 같은 속성을 검사한다."""
    from opencrab.pack.source_writer import preflight_source_write

    preflight_source_write(
        None, None, None, text="본문 661", source_id="src-661",
        metadata={"title": "제목"}, principal=_principal(), pack_id="default-new-661",
    )

    assert [t for t, _props in schema_calls] == ["TextUnit"]
    props = schema_calls[0][1]
    assert (props["pack_id"], props["text"], props["title"]) == (
        "default-new-661", "본문 661", "제목",
    )


def test_a_display_node_id_equal_to_the_source_id_is_accepted():
    """대조군: metadata.node_id가 source id와 같으면 쓰기는 받아들인다. 사전 검사도 거부하지 않는다."""
    from opencrab.pack.source_writer import preflight_source_write

    assert preflight_source_write(
        None, None, None, text="본문", source_id="src-661",
        metadata={"node_id": "src-661"}, principal=_principal(), pack_id="default-new-661",
    ) is None


def test_a_foreign_vector_row_alone_rejects_the_rest_ingest_without_a_default_pack(
    rest, data_dir, monkeypatch,
):
    """REST 훅이 doc과 그래프만이 아니라 벡터도 사전 검사에 넘긴다."""
    client, ctx = rest
    monkeypatch.setattr(
        type(ctx.vector), "get_by_id", lambda _self, _id: {"metadata": {"pack_id": "pack-b"}},
    )
    legacy_id, headers = _legacy_user(ctx.sql, "legacy")
    _member_id, member_headers = _user(ctx.sql, "member")
    expected = _ingest(client, member_headers)
    before = _all_rows(data_dir)

    resp = _ingest(client, headers)

    assert expected.status_code == 422, expected.text
    assert _FOREIGN_SOURCE_DETAIL in expected.json()["detail"]
    assert resp.status_code == 422, resp.text
    assert resp.json() == expected.json()
    assert _packs_of(ctx.sql, legacy_id) == []
    assert _changed(before, _all_rows(data_dir)) == []


def _readable_graph(rows, digest=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        available=True, get_node=lambda *_a: None, get_nodes_by_id=lambda _id: rows,
        get_node_digest=lambda *_a, **_k: digest,
    )


def _docs_with_a_foreign_node_row():
    from types import SimpleNamespace

    return SimpleNamespace(
        available=True, get_source=lambda _id: None,
        get_node_doc=lambda *_a: {"properties": {"pack_id": "pack-b"}},
    )


@pytest.mark.parametrize(
    "case", ["id-at-the-budget", "unavailable-graph-that-still-answers"],
)
def test_the_long_id_carve_out_needs_a_readable_empty_answer_and_an_id_past_the_budget(case):
    """우회로는 예산을 넘는 id와 읽을 수 있는 빈 답이 둘 다 있을 때만 탄다. 아니면 노드 슬롯을 본다."""
    from types import SimpleNamespace

    from opencrab.pack.source_writer import preflight_source_write

    if case == "id-at-the-budget":
        source_id, graph = "G" * SOURCE_NODE_ID_BUDGET, _readable_graph([])
    else:
        source_id = "G" * (SOURCE_NODE_ID_BUDGET + 1)
        graph = SimpleNamespace(available=False, get_nodes_by_id=lambda _id: [])

    with pytest.raises(ValueError, match=_FOREIGN_NODE_DETAIL):
        preflight_source_write(
            _docs_with_a_foreign_node_row(), None, graph, text="본문", source_id=source_id,
            metadata=None, principal=_principal(), pack_id="default-new-661",
        )


@pytest.mark.parametrize(
    "rows",
    [
        [{"node_type": "TextUnit"}, {"node_type": "Entity"}],
        [{"node_type": "Entity"}],
    ],
    ids=["duplicate-rows", "row-of-another-type"],
)
def test_an_existing_row_that_cannot_take_the_cas_path_raises_node_identity_conflict(rows):
    """쓰기는 이 행들에서 일반 삽입 경로로 가서 NodeIdentityConflict를 낸다. 사전 검사는 같은 예외 종류를 낸다."""
    from opencrab.common.graph_identity import NodeIdentityConflict
    from opencrab.pack.source_writer import preflight_source_write

    # digest를 읽을 수 있어도 행이 둘이거나 다른 타입이면 CAS 토큰을 쓰지 않는다.
    graph = _readable_graph(rows, digest="digest-661")
    with pytest.raises(NodeIdentityConflict, match="node identity conflict: src-661"):
        preflight_source_write(
            None, None, graph, text="본문", source_id="src-661",
            metadata=None, principal=_principal(), pack_id="default-new-661",
        )


def test_the_preflight_probes_the_typed_node_slot_of_the_source_node_type():
    """타입 지정 노드 탐침이 읽기에 실패하면 쓰기와 같이 소유 확인 불가로 거부한다."""
    from types import SimpleNamespace

    from opencrab.pack.source_writer import preflight_source_write

    def _get_node(node_type, _node_id):
        if node_type == "TextUnit":
            raise RuntimeError("typed probe failed")
        return None

    graph = SimpleNamespace(available=True, get_node=_get_node, get_nodes_by_id=lambda _id: [])

    with pytest.raises(ValueError, match="cannot verify existing ownership"):
        preflight_source_write(
            None, None, graph, text="본문", source_id="src-661",
            metadata=None, principal=_principal(), pack_id="default-new-661",
        )


def test_a_single_text_unit_row_with_a_readable_digest_is_not_rejected_by_the_cas_rule():
    """대조군: 같은 타입 행 하나에 읽을 수 있는 digest가 있으면 쓰기는 CAS 경로로 이어받는다."""
    from types import SimpleNamespace

    from opencrab.pack.source_writer import preflight_source_write

    graph = _readable_graph([{"node_type": "TextUnit"}])
    graph = SimpleNamespace(**{**vars(graph), "get_node_digest": lambda *_a, **_k: "digest-661"})

    assert preflight_source_write(
        None, None, graph, text="본문", source_id="src-661",
        metadata=None, principal=_principal(), pack_id="default-new-661",
    ) is None


def test_a_foreign_row_among_duplicate_rows_gets_the_node_slot_answer():
    """같은 id의 행이 둘이고 무태그 행과 남의 행이 섞이면 쓰기는 노드 슬롯 거부로 답한다. 사전 검사도 같은 문구를 낸다."""
    from opencrab.pack.source_writer import preflight_source_write

    graph = _readable_graph([{"node_type": "TextUnit"}, {"node_type": "Entity", "pack_id": "pack-b"}])
    with pytest.raises(ValueError) as excinfo:
        preflight_source_write(
            None, None, graph, text="본문", source_id="src-661",
            metadata=None, principal=_principal(), pack_id="default-new-661",
        )
    assert str(excinfo.value) == "src-661: identity is already attributed to a different pack"
