"""pack_ingest/pack_create의 pack_id exact 일치 계약.

content_pack_list에 query/limit이 생기면서 생기는 회귀를 막는다: 존재 검사는
반드시 필터링되지 않은 전체 목록을 봐야 하고, 근접한 pack_id는 절대 보정되어
실존 팩으로 통과되면 안 된다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.mcp.tools import pack_ingest

# #145: pack_create/pack_ingest now call current_principal() internally;
# bind a fixed test principal for every test in this module (see
# conftest.py's bind_test_principal).
pytestmark = pytest.mark.usefixtures("bind_test_principal")

_EXISTING = {"total": 1, "packs": [{"pack_id": "claude", "node_count": 9709, "title": "Claude"}]}


@pytest.fixture
def ctx():
    builder = MagicMock()
    hybrid = MagicMock()
    mongo = MagicMock()
    mongo.available = False
    return {
        "builder": builder,
        "hybrid": hybrid,
        "mongo": mongo,
        "neo4j": MagicMock(),
        "billing": MagicMock(),
    }


# ---------------------------------------------------------------------------
# 전체 목록으로만 존재 검사
# ---------------------------------------------------------------------------


def test_pack_ingest_asks_for_the_unfiltered_pack_list(ctx):
    with (
        patch("opencrab.mcp.tools._get_context", return_value=ctx),
        patch("opencrab.mcp.tools.content_pack_list") as mock_list,
    ):
        mock_list.return_value = _EXISTING
        pack_ingest(pack_id="claude", text="본문", source_id="claude:doc:1")
    # 인자 없이 호출해야 한다 — query를 넘기면 후보가 줄어 실존 팩이 거부된다.
    mock_list.assert_called_once_with()


# pack_create's own duplicate-check used to be exactly this ("ask for the
# unfiltered list, exact-match against it") — #146 replaced it with the
# `packs` registry (owner_id/visibility), which decides slug collisions by
# quietly suffixing rather than erroring (#143 invariant 7). See
# tests/test_packs_registry.py for that contract; pack_ingest's own
# exact-match behaviour below (querying content_pack_list, still the read
# path #146 scoped) is unchanged.


# ---------------------------------------------------------------------------
# fuzzy 금지
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "near_miss",
    ["clau", "Claude", "claude-", "claude ", " claude", "CLAUDE", "claude/"],
)
def test_near_miss_pack_id_is_rejected(ctx, near_miss):
    with (
        patch("opencrab.mcp.tools._get_context", return_value=ctx),
        patch("opencrab.mcp.tools.content_pack_list") as mock_list,
    ):
        mock_list.return_value = _EXISTING
        result = pack_ingest(pack_id=near_miss, text="본문")
    assert result["error"] == "pack not found; use pack_create first"
    assert result["pack_id"] == near_miss
    ctx["builder"].add_node.assert_not_called()


def test_exact_pack_id_passes_and_is_stored_verbatim(ctx):
    with (
        patch("opencrab.mcp.tools._get_context", return_value=ctx),
        patch("opencrab.mcp.tools.content_pack_list") as mock_list,
    ):
        mock_list.return_value = _EXISTING
        result = pack_ingest(
            pack_id="claude",
            nodes=[{"space": "concept", "node_type": "Concept", "node_id": "c1"}],
        )
    assert result["status"] == "ok"
    assert result["pack_id"] == "claude"
    _, kwargs = ctx["builder"].add_node.call_args
    assert kwargs["properties"]["pack_id"] == "claude"


