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
    """그래프가 없으면 쓰기는 영수증으로 답하고 거부하지 않는다. 사전 검사도 거부하지 않는다."""
    from opencrab.auth import Principal
    from opencrab.pack.source_writer import preflight_source_write

    principal = Principal(user_id="user_x_661", is_local=False, disabled=False)

    assert preflight_source_write(
        None, None, None, text="본문", source_id="src-661", metadata=None,
        principal=principal, pack_id="default-new-661",
    ) is None
    # 본문이 무효여도 그래프가 없으면 add_node는 속성 검사를 하지 않고 영수증을 돌려준다.
    assert preflight_source_write(
        None, None, None, text="본문", source_id="src-661",
        metadata={"title": float("nan")},
        principal=principal, pack_id="default-new-661",
    ) is None


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
