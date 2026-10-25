"""#516 G5/G7: folded members through the real query, rerank, fold and cap path.

Only two storage legs are controlled: the vector store ``query`` result and the
rows of a real doc store. ``HybridQuery.query``, the real ``Reranker``, the real
fold, the ``QueryResult`` conversion and the MCP handler run unchanged. No case
sets ``text_transformed`` or builds a ``QueryResult`` by hand.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from opencrab.services import result_caps as caps
from tests import _issue516_flow as flow
from tests import test_query_result_caps as _caps

# A new module has no other ``stack`` fixture, so this assignment collides with nothing.
stack = _caps.stack

pytestmark = pytest.mark.usefixtures("bind_test_principal")

PACK = _caps.PACK
NODES = ["fold:a", "fold:b"]


def _ask_recorded(stack, monkeypatch, question: str) -> tuple[dict[str, Any], list[Any]]:
    """Run the MCP handler and keep the outcome of the very query it used."""
    from opencrab.mcp import tools

    recorded: list[Any] = []
    original = stack["hybrid"].query

    def recording_query(*args: Any, **kwargs: Any):
        outcome = original(*args, **kwargs)
        recorded.append(outcome)
        return outcome

    monkeypatch.setattr(stack["hybrid"], "query", recording_query)
    with patch.object(tools, "_get_context", return_value=stack["ctx"]):
        response = tools.ontology_query(
            question=question, pack_ids=[PACK], use_bm25=False, limit=5
        )
    return response, recorded


@pytest.mark.parametrize("cell", flow.CELLS)
def test_pre_cap_fold_member_real_flow(stack, monkeypatch, cell):
    spec = flow.cell_spec(cell)
    flow.install_vector_leg(stack["vec"], monkeypatch, PACK, NODES, spec["vector"])
    flow.seed_source_rows(stack["docs"], PACK, NODES, spec["source"])

    response, recorded = _ask_recorded(stack, monkeypatch, flow.NEEDLE)

    # The real rerank and fold output is asserted before any surface is inspected.
    assert len(recorded) == 1
    flow.assert_pre_surface(recorded[0], cell, folded=True)
    assert response["total"] == 1
    item = response["results"][0]
    flow.assert_public_cell(item, cell)
    members = item["metadata"]["folded_duplicates"]["members"]
    assert len(members) == 1
    flow.assert_public_cell(members[0], cell)
    assert flow.find_reserved(response) == []
    assert caps._wire_length(response) <= caps.QUERY_RESPONSE_CHAR_BUDGET


def test_folded_member_keeps_its_own_locator(stack, monkeypatch):
    big = _caps.BIG
    flow.install_vector_leg(stack["vec"], monkeypatch, PACK, NODES, big)
    flow.seed_source_rows(stack["docs"], PACK, NODES, big)

    response, recorded = _ask_recorded(stack, monkeypatch, "alpha beta")

    assert len(recorded) == 1 and recorded[0].results[0].folded_count == 1
    assert response["total"] == 1
    representative = response["results"][0]
    members = representative["metadata"]["folded_duplicates"]["members"]
    assert len(members) == 1
    seen = set()
    for item in (representative, members[0]):
        assert "truncation" in item
        assert "source_locator" in item["truncation"], item["truncation"]
        locator = item["truncation"]["source_locator"]
        assert locator["source_id"] == item["node_id"]
        assert item["truncation"]["fields"][0]["original_chars"] == len(big)
        assert _caps._read_source(stack, locator) == big
        seen.add(locator["source_id"])
    assert seen == set(NODES)
    assert flow.find_reserved(response) == []
    assert caps._wire_length(response) <= caps.QUERY_RESPONSE_CHAR_BUDGET
