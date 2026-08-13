"""pack_ingest의 pack_id exact 일치 + 소유권 계약.

#146: 존재 검사는 content_pack_list()의 필터링되지 않은 목록이 아니라
``packs`` 등록부(``assert_writable``)를 SQL PK로 조회한다 -- 정확히 같은
문자열만 매치하는 SQL WHERE pack_id = :pid 이므로 근접한 pack_id가 절대
보정되어 실존 팩으로 통과되지 않는다는 계약은 그대로 유지된다. 여기 더해
소유자만 쓸 수 있다는 계약(#143 불변식 4)도 함께 검증한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal
from opencrab.mcp.tools import pack_ingest
from opencrab.pack.ownership import create_pack, set_visibility
from opencrab.stores.sql_store import SQLStore

# #145: pack_create/pack_ingest now call current_principal() internally;
# bind a fixed test principal for every test in this module (see
# conftest.py's bind_test_principal).
pytestmark = pytest.mark.usefixtures("bind_test_principal")


@pytest.fixture
def sql():
    store = SQLStore("sqlite:///:memory:")
    create_pack(store, "test-user", "claude", title="Claude")
    return store


@pytest.fixture
def ctx(sql):
    builder = MagicMock()
    builder.add_node.return_value = {"stores": {"graph": "ok"}}
    hybrid = MagicMock()
    mongo = MagicMock()
    mongo.available = False
    graph = MagicMock()
    graph.available = True
    return {
        "builder": builder,
        "hybrid": hybrid,
        "mongo": mongo,
        "neo4j": graph,
        "sql": sql,
        "billing": MagicMock(),
    }


# ---------------------------------------------------------------------------
# fuzzy 금지 -- SQL PK exact match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "near_miss",
    ["clau", "Claude", "claude-", "claude ", " claude", "CLAUDE", "claude/"],
)
def test_near_miss_pack_id_is_rejected(ctx, near_miss):
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(pack_id=near_miss, text="본문")
    assert result == {
        "error": "pack not found; use pack_create first",
        "pack_id": near_miss,
    }
    ctx["builder"].add_node.assert_not_called()


def test_exact_pack_id_passes_and_is_stored_verbatim(ctx):
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(
            pack_id="claude",
            nodes=[{"space": "concept", "node_type": "Concept", "node_id": "c1"}],
        )
    assert result["status"] == "ok"
    assert result["pack_id"] == "claude"
    _, kwargs = ctx["builder"].add_node.call_args
    assert kwargs["properties"]["pack_id"] == "claude"


# ---------------------------------------------------------------------------
# #146 D: 소유자만 쓴다 (#143 불변식 4) -- 존재 검사가 아니라 인가 검사
# ---------------------------------------------------------------------------


def test_unregistered_pack_id_is_rejected(ctx):
    """등록부에 아예 없는 pack_id -- 존재하지 않는 팩."""
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(pack_id="never-created", text="본문")
    assert result == {
        "error": "pack not found; use pack_create first",
        "pack_id": "never-created",
    }


def test_someone_elses_private_pack_is_rejected_same_as_not_found(sql, ctx):
    """#143 불변식 7: 남의 private 팩과 미등록 pack_id는 동일 응답이어야
    존재 자체가 새지 않는다."""
    create_pack(sql, "someone-else", "alice-secret")
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(pack_id="alice-secret", text="본문")
    assert result == {
        "error": "pack not found; use pack_create first",
        "pack_id": "alice-secret",
    }
    ctx["builder"].add_node.assert_not_called()


def test_someone_elses_public_pack_is_rejected_as_not_writable(sql, ctx):
    """행동 변화 (#146 D): 이전에는 readable 목록 멤버십만 봤으므로 남의
    공개 팩에도 ingest가 가능했다. 이제는 소유자만 쓴다 -- 회귀가 아니라
    #143 불변식 4의 이행이다."""
    create_pack(sql, "someone-else", "shared-pack")
    set_visibility(
        sql, Principal(user_id="someone-else", is_local=False, disabled=False),
        "shared-pack", "public-read",
    )
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(pack_id="shared-pack", text="본문")
    assert result == {
        "error": "PACK_NOT_WRITABLE: not the pack owner",
        "pack_id": "shared-pack",
        "hint": "use pack_fork to copy this pack into your own",
    }
    ctx["builder"].add_node.assert_not_called()


def test_response_dict_is_identical_for_unregistered_and_someone_elses_private(sql, ctx):
    """D3: 두 응답 dict 전체가 완전히 동일해야 한다 -- 응답이 pack_id와
    principal의 가시 집합에만 의존한다는 동일성 정의."""
    create_pack(sql, "someone-else", "alice-secret")
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        unregistered = pack_ingest(pack_id="totally-unregistered", text="본문")
        private = pack_ingest(pack_id="alice-secret", text="본문")
    # pack_id를 맞춰서 비교하면 완전히 같은 shape이어야 한다.
    assert unregistered == {
        "error": "pack not found; use pack_create first",
        "pack_id": "totally-unregistered",
    }
    assert private == {
        "error": "pack not found; use pack_create first",
        "pack_id": "alice-secret",
    }
    # docstring 의 "전체 dict 동일" 을 문자 그대로 단언한다: 유일하게
    # 달라도 되는 필드(요청 pack_id 의 에코)를 정규화하면 두 dict 는
    # 완전히 같아야 한다. key set 비교보다 강하다 (값 차이도 잡는다).
    assert {**unregistered, "pack_id": None} == {**private, "pack_id": None}


def test_graph_unavailable_rejects_before_any_store_write(sql, ctx):
    """D1/D2: graph가 불가능하면 assert_writable조차 부르기 전에 거부한다
    -- 등록된 자기 소유 팩이어도 어떤 스토어에도 쓰지 않는다."""
    ctx["neo4j"].available = False
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        result = pack_ingest(pack_id="claude", text="본문")
    assert result == {"error": "graph store unavailable"}
    ctx["builder"].add_node.assert_not_called()
    ctx["billing"].on_ingest.assert_not_called()
