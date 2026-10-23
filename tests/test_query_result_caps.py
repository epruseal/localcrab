"""#511: size handling of the ``ontology_query`` response.

Design and decision tables: docs/issue-511-query-text-cap.md. Each test class
below maps to a table of that document.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from opencrab.ontology.query import QueryOutcome, QueryResult
from opencrab.services import result_caps as caps
from opencrab.services.result_caps import (
    apply_response_budget,
    cap_query_item,
    cap_selected_pack,
    response_too_large_error,
)

from ._vec_helpers import build_vector_store
from .test_mcp_pack_aware import _real_sql_with_owned_pack, _stub_context

pytestmark = pytest.mark.usefixtures("bind_test_principal")

PACK = "p1"
LIMIT = caps.QUERY_FIELD_CHAR_LIMIT
BUDGET = caps.QUERY_RESPONSE_CHAR_BUDGET


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def _item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "vector",
        "node_id": "n1",
        "score": 0.5,
        "text": "short",
        "metadata": {"pack_id": PACK},
        "graph_context": None,
        "raw_score": 0.5,
    }
    base.update(over)
    return base


def _long_strings(item: dict[str, Any], limit: int, folded: bool = False) -> list[list[Any]]:
    """Paths of strings longer than ``limit`` outside the places the design exempts.

    Exempt: object keys, ``truncation`` (its paths hold keys), identifier places
    (``node_id``, ``canonical``, ``graph_context`` identifier keys).
    """
    found: list[list[Any]] = []

    def walk(value: Any, path: list[Any]) -> None:
        if isinstance(value, str):
            if len(value) > limit:
                found.append(path)
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + [key])
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(child, path + [index])

    walk(item.get("text"), ["text"])
    meta = item.get("metadata")
    if isinstance(meta, dict):
        for key, child in meta.items():
            if folded and key == "folded_duplicates":
                for index, member in enumerate(child["members"]):
                    found.extend(
                        [["metadata", key, "members", index, *p] for p in _long_strings(member, limit)]
                    )
            else:
                walk(child, ["metadata", key])
    else:
        walk(meta, ["metadata"])
    context = item.get("graph_context")
    if isinstance(context, dict):
        for key, child in context.items():
            if key not in ("anchor_id", "edge_endpoints", "edge"):
                walk(child, ["graph_context", key])
    return found


# ---------------------------------------------------------------------------
# Table: string traversal (_cap)
# ---------------------------------------------------------------------------


class TestStringTraversal:
    @pytest.mark.parametrize("ch", ["a", "한", "😀", ""])
    def test_string_at_limit_is_returned_as_same_object(self, ch):
        value = ch * LIMIT
        cuts: list = []
        out, changed = caps._cap(value, ["x"], LIMIT, cuts)
        assert out is value and changed is False and cuts == []

    def test_string_over_limit_is_cut_and_recorded(self):
        value = "한" * (LIMIT + 1)
        cuts: list = []
        out, changed = caps._cap(value, ["a", 3], LIMIT, cuts)
        assert out == "한" * LIMIT and changed is True
        assert cuts == [{"path": ["a", 3], "original_chars": LIMIT + 1}]

    def test_dict_unchanged_is_same_object_and_changed_is_rebuilt(self):
        short = {"a": "x", "b": {"c": [1, 2]}}
        out, changed = caps._cap(short, [], LIMIT, [])
        assert out is short and changed is False

        before = copy.deepcopy(short)
        long = {"k": "y" * (LIMIT + 5), "keep": short}
        snapshot = copy.deepcopy(long)
        cuts: list = []
        out, changed = caps._cap(long, [], LIMIT, cuts)
        assert changed is True and out is not long
        assert list(out) == ["k", "keep"]  # key order
        assert out["keep"] is short  # unchanged child keeps identity
        assert long == snapshot and short == before  # input untouched
        assert cuts == [{"path": ["k"], "original_chars": LIMIT + 5}]

    def test_long_object_key_is_kept_as_is(self):
        key = "k" * (LIMIT * 2)
        out, changed = caps._cap({key: "v" * (LIMIT + 1)}, [], LIMIT, [])
        assert list(out) == [key] and changed is True

    def test_skip_keys_apply_to_the_top_level_only(self):
        big = "z" * (LIMIT + 1)
        value = {"skip": big, "nested": {"skip": big}}
        cuts: list = []
        out, _ = caps._cap(value, [], LIMIT, cuts, ("skip",))
        assert out["skip"] == big
        assert out["nested"]["skip"] == "z" * LIMIT
        assert [c["path"] for c in cuts] == [["nested", "skip"]]

    def test_list_and_tuple_keep_their_kind_and_report_positions(self):
        big = "q" * (LIMIT + 1)
        out, changed = caps._cap(["a", big], [], LIMIT, cuts := [])
        assert changed and out == ["a", "q" * LIMIT] and cuts[0]["path"] == [1]
        out, changed = caps._cap(("a", big), [], LIMIT, [])
        assert isinstance(out, tuple) and out[1] == "q" * LIMIT
        same = ["a", "b"]
        assert caps._cap(same, [], LIMIT, [])[0] is same
        same_t = ("a", "b")
        assert caps._cap(same_t, [], LIMIT, [])[0] is same_t

    @pytest.mark.parametrize("value", [None, 7, 1.5, True])
    def test_other_values_are_returned_as_same_object(self, value):
        out, changed = caps._cap(value, [], LIMIT, [])
        assert out is value and changed is False


# ---------------------------------------------------------------------------
# Table: item handling
# ---------------------------------------------------------------------------


class TestItemHandling:
    @pytest.mark.parametrize(
        "item",
        [
            _item(),
            _item(text=None),
            _item(text=""),
            _item(metadata={"a": {"b": ["x", 1, None]}, "pack_id": PACK}),
            _item(graph_context={"anchor_id": "a", "labels": ["L"], "depth": 1}),
            _item(text="t" * LIMIT, metadata={"m": "m" * LIMIT}),
        ],
    )
    def test_item_without_long_string_is_the_same_object_and_bytes(self, item):
        before = _json(item)
        out = cap_query_item(item, limit=LIMIT)
        assert out is item
        assert _json(out) == before
        assert "truncation" not in out

    def test_long_text_only(self):
        item = _item(text="t" * 380_000)
        out = cap_query_item(item, limit=LIMIT)
        assert out["text"] == "t" * LIMIT
        assert out["truncation"] == {
            "limit_chars": LIMIT,
            "fields": [{"path": ["text"], "original_chars": 380_000}],
            "full_text_tool": "ontology_get_node",
        }
        assert list(out)[-1] == "truncation"
        assert list(out)[:-1] == list(item)
        assert item["text"] == "t" * 380_000  # input untouched

    def test_graph_leg_shape_short_text_long_metadata_text(self):
        item = _item(
            source="graph",
            text="r" * LIMIT,
            metadata={"id": "n2", "text": "b" * 50_000, "pack_id": PACK},
        )
        out = cap_query_item(item, limit=LIMIT)
        assert out["text"] == item["text"]
        assert out["metadata"]["text"] == "b" * LIMIT
        assert out["truncation"]["fields"] == [
            {"path": ["metadata", "text"], "original_chars": 50_000}
        ]

    def test_graph_context_non_identifier_key_is_cut(self):
        item = _item(graph_context={"labels": ["L" * (LIMIT + 3)], "relation_type": "r"})
        out = cap_query_item(item, limit=LIMIT)
        assert out["graph_context"]["labels"] == ["L" * LIMIT]
        assert out["truncation"]["fields"][0]["path"] == ["graph_context", "labels", 0]

    def test_text_metadata_graph_context_report_in_traversal_order(self):
        item = _item(
            text="t" * (LIMIT + 1),
            metadata={"m": "m" * (LIMIT + 2)},
            graph_context={"relation_type": "g" * (LIMIT + 3)},
        )
        fields = cap_query_item(item, limit=LIMIT)["truncation"]["fields"]
        assert [f["path"] for f in fields] == [
            ["text"], ["metadata", "m"], ["graph_context", "relation_type"]
        ]

    def test_identifier_places_are_kept_whole(self):
        big = "i" * (LIMIT * 3)
        item = _item(
            node_id=big,
            canonical={"resolved": True, "node_id": big, "document_id": big},
            graph_context={
                "anchor_id": big,
                "edge_endpoints": {"from_id": big, "to_id": big},
                "edge": {
                    "resolved": True,
                    "source": {"node_id": big},
                    "relation": big,
                    "target": {"node_id": big},
                },
            },
        )
        before = _json(item)
        out = cap_query_item(item, limit=LIMIT)
        assert out is item and _json(out) == before

    # -- fold ---------------------------------------------------------------

    def _folded(self, rep_text: str, member_text: str, count: int = 2) -> dict[str, Any]:
        members = [
            _item(node_id=f"m{i}", text=member_text, canonical={"node_id": f"m{i}" * 2000})
            for i in range(count)
        ]
        return _item(
            text=rep_text,
            metadata={"pack_id": PACK, "folded_duplicates": {"count": count, "members": members}},
        )

    def test_member_only_long_changes_the_block_but_not_the_representative_truncation(self):
        item = self._folded("short", "m" * (LIMIT + 10))
        snapshot = copy.deepcopy(item)
        out = cap_query_item(item, limit=LIMIT, folded=True)
        assert out is not item and item == snapshot
        assert "truncation" not in out
        block = out["metadata"]["folded_duplicates"]
        assert block["count"] == 2 and len(block["members"]) == 2
        assert [m["node_id"] for m in block["members"]] == ["m0", "m1"]
        for member in block["members"]:
            assert member["text"] == "m" * LIMIT
            assert member["truncation"]["fields"] == [
                {"path": ["text"], "original_chars": LIMIT + 10}
            ]
            assert member["canonical"] == {"node_id": member["node_id"] * 2000}
        assert list(out["metadata"]) == list(item["metadata"])

    def test_both_representative_and_members_long(self):
        out = cap_query_item(
            self._folded("r" * (LIMIT + 1), "m" * (LIMIT + 1)), limit=LIMIT, folded=True
        )
        assert out["truncation"]["fields"] == [{"path": ["text"], "original_chars": LIMIT + 1}]
        assert all("truncation" in m for m in out["metadata"]["folded_duplicates"]["members"])

    def test_representative_only_long_leaves_members_as_the_same_objects(self):
        item = self._folded("r" * (LIMIT + 1), "m")
        out = cap_query_item(item, limit=LIMIT, folded=True)
        assert "truncation" in out
        assert out["metadata"]["folded_duplicates"] is item["metadata"]["folded_duplicates"]

    def test_nothing_long_in_a_folded_item_is_the_same_object(self):
        item = self._folded("short", "short")
        before = _json(item)
        assert cap_query_item(item, limit=LIMIT, folded=True) is item
        assert _json(item) == before

    def test_caller_written_folded_duplicates_is_a_plain_value(self):
        item = _item(metadata={"folded_duplicates": {"members": [{"text": "c" * (LIMIT + 1)}]}})
        out = cap_query_item(item, limit=LIMIT, folded=False)
        assert out["metadata"]["folded_duplicates"]["members"][0]["text"] == "c" * LIMIT
        assert out["truncation"]["fields"][0]["path"] == [
            "metadata", "folded_duplicates", "members", 0, "text"
        ]

    # -- selected packs -------------------------------------------------------

    def test_selected_pack_cuts_matched_and_keeps_pack_id(self):
        entry = {"pack_id": "P" * (LIMIT + 5), "score": 1.0, "matched": ["title", "source:" + "s" * 9000]}
        out = cap_selected_pack(entry, limit=LIMIT)
        assert out["pack_id"] == entry["pack_id"]
        assert out["matched"][0] == "title" and len(out["matched"][1]) == LIMIT
        assert out["truncation"] == {
            "limit_chars": LIMIT,
            "fields": [{"path": ["matched", 1], "original_chars": len("source:") + 9000}],
        }

    def test_selected_pack_short_is_the_same_object(self):
        entry = {"pack_id": "p", "score": 1.0, "matched": ["title"]}
        before = _json(entry)
        assert cap_selected_pack(entry, limit=LIMIT) is entry
        assert _json(entry) == before


# ---------------------------------------------------------------------------
# Table: response budget
# ---------------------------------------------------------------------------


def _response(results: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "question": "q",
        "total": len(results),
        "results": results,
        "selected_packs": [],
        **extra,
    }


def _results(n: int, size: int) -> list[dict[str, Any]]:
    return [{"node_id": f"n{i}", "text": "x" * size} for i in range(n)]


def _r(response: dict[str, Any], k: int) -> dict[str, Any]:
    return caps._truncated_response(response, k, len(response["results"]))


class TestResponseBudget:
    # -- row A ----------------------------------------------------------------

    def test_row_a_response_at_budget_is_returned_as_is(self):
        response = _response(_results(5, 100))
        length = caps._wire_length(response)
        before = _json(response)
        assert apply_response_budget(response, budget=length) is response
        assert _json(response) == before
        out = apply_response_budget(response, budget=length - 1)
        assert out is not response and "results_truncated" in out

    def test_row_a_word_for_word_for_default_budget(self):
        response = _response(_results(3, 50))
        before = _json(response)
        assert apply_response_budget(response) is response
        assert _json(response) == before
        assert "results_truncated" not in response and "omitted_results" not in response

    # -- row B ----------------------------------------------------------------

    @pytest.mark.parametrize("k", [1, 3, 7, 11])
    def test_row_b_keeps_the_largest_prefix_that_fits(self, k):
        n = 12
        response = _response(_results(n, 200))
        budget = caps._wire_length(_r(response, k))
        out = apply_response_budget(response, budget=budget)
        assert [r["node_id"] for r in out["results"]] == [f"n{i}" for i in range(k)]
        assert out["total"] == k and out["results_truncated"] is True
        assert out["omitted_results"] == n - k
        assert caps._wire_length(out) <= budget
        assert k == n - 1 or caps._wire_length(_r(response, k + 1)) > budget
        assert list(out) == [
            "question", "total", "results", "results_truncated", "omitted_results", "next_offset", "selected_packs"
        ]
        assert out["question"] == "q" and out["selected_packs"] == []

    def test_row_b_n2_second_result_has_a_huge_identifier(self):
        results = [{"node_id": "a", "text": "ok"}, {"node_id": "i" * 600_000, "text": "ok"}]
        response = _response(results)
        assert caps._wire_length(response) > BUDGET
        out = apply_response_budget(response)
        assert len(out["results"]) == 1 and out["total"] == 1
        assert out["omitted_results"] == 1 and out["results_truncated"] is True

    def test_row_b_sum_of_results_under_the_item_limit(self):
        response = _response([{"node_id": f"n{i}", "text": "한" * LIMIT} for i in range(70)])
        assert caps._wire_length(response) > BUDGET
        out = apply_response_budget(response)
        assert 0 < out["total"] < 70
        assert out["total"] + out["omitted_results"] == 70
        assert caps._wire_length(out) <= BUDGET
        assert caps._wire_length(_r(response, out["total"] + 1)) > BUDGET

    def test_row_b_drops_a_folded_member_with_its_representative(self):
        head = {"node_id": "head-id-7f3a", "text": "z" * 500}
        rep = {"node_id": "rep-id-7f3a", "text": "x" * 500, "metadata": {"folded_duplicates": {
            "count": 1, "members": [{"node_id": "member-id-7f3a", "text": "y" * 500}]}}}
        response = _response([head, rep])
        out = apply_response_budget(response, budget=caps._wire_length(_r(response, 1)))
        assert out["results"] == [head] and out["total"] == 1
        assert out["omitted_results"] == 1
        dumped = _json(out)
        assert "rep-id-7f3a" not in dumped and "member-id-7f3a" not in dumped

    @pytest.mark.parametrize(
        ("n", "k"),
        [
            (2, 1), (9, 8), (10, 1), (10, 9), (11, 9), (11, 10),
            (100, 9), (100, 10), (100, 99), (101, 99), (101, 100),
            (1000, 1), (1000, 8), (1000, 9), (1000, 10), (1000, 11), (1000, 98), (1000, 99),
            (1000, 100), (1000, 101), (1000, 998), (1000, 999), (1001, 1000),
        ],
    )
    def test_row_b_digit_boundaries(self, n, k):
        # Each result is longer than the two added fields, so R(k) is shorter
        # than the full response for every k < n.
        response = _response([{"node_id": f"n{i}", "text": "t" * 100} for i in range(n)])
        budget = caps._wire_length(_r(response, k))
        assert budget < caps._wire_length(response)
        out = apply_response_budget(response, budget=budget)
        assert out["total"] == k and out["omitted_results"] == n - k
        assert caps._wire_length(out) <= budget
        assert k == n - 1 or caps._wire_length(_r(response, k + 1)) > budget

    # -- row C ----------------------------------------------------------------

    def _row_c_cases(self) -> dict[str, dict[str, Any]]:
        parts = {"parts": ["a" * LIMIT] * 156}
        return {
            "n1_identifier": _response([{"node_id": "i" * 600_000}]),
            "n2_first_result_too_big": _response(
                [{"node_id": "a", "metadata": parts}, {"node_id": "b", "text": "ok"}]
            ),
            "n0_warnings": _response([], spaces_filter_warnings=["w" * 600_000]),
            "outside_fields": _response(
                [{"node_id": "a", "text": "ok"}],
                selected_packs=[{"pack_id": "p" * 600_000, "score": 1, "matched": []}],
            ),
        }

    @pytest.mark.parametrize(
        "name", ["n1_identifier", "n2_first_result_too_big", "n0_warnings", "outside_fields"]
    )
    def test_row_c_returns_the_fixed_error(self, name):
        response = self._row_c_cases()[name]
        assert caps._wire_length(response) > BUDGET
        assert apply_response_budget(response) == response_too_large_error()

    def test_row_c_error_is_one_value_that_names_no_count_or_cause(self):
        errors = [apply_response_budget(r) for r in self._row_c_cases().values()]
        assert all(e == errors[0] for e in errors)
        assert set(errors[0]) == {"error", "message"}
        assert errors[0]["error"] == "response_too_large"
        assert "첫 결과" not in errors[0]["message"]
        assert caps._wire_length(errors[0]) < BUDGET

    # -- unit -----------------------------------------------------------------

    @pytest.mark.parametrize(
        ("ch", "increment"),
        [("a", 1), ('"', 4), ("\\", 4), ("\n", 3), ("\x00", 7), ("한", 3), ("😀", 4)],
    )
    def test_wire_length_per_character(self, ch, increment):
        base = caps._wire_length({"a": ""})
        assert caps._wire_length({"a": ch * 10}) - base == 10 * increment

    def test_quote_heavy_response_is_over_budget_by_wire_length_only(self):
        n = 400
        response = _response([{"node_id": "b", "text": "ok"}, {"node_id": "a", "text": '"' * n}])
        inner = len(json.dumps(response, ensure_ascii=True))
        wire = caps._wire_length(response)
        assert inner < wire
        budget = (inner + wire) // 2
        out = apply_response_budget(response, budget=budget)
        assert out is not response and out["omitted_results"] == 1


class TestContinuationAndSurfaceBytes:
    def test_budget_adds_next_offset_before_it_measures(self):
        response = _response(_results(3, 100))

        def only_one_result_fits(value: dict[str, Any]) -> int:
            if "next_offset" not in value:
                return 2
            assert value["next_offset"] == 7 + len(value["results"])
            return 1 if len(value["results"]) == 1 else 2

        out = apply_response_budget(response, budget=1, offset=7, measure=only_one_result_fits)
        assert [item["node_id"] for item in out["results"]] == ["n0"]
        assert out["total"] == 1
        assert out["omitted_results"] == 2
        assert out["next_offset"] == 8

    def test_fallback_cap_keeps_identifiers_and_cuts_body(self):
        fallback = {
            "node": {
                "id": "i" * (LIMIT + 1),
                "node_id": "n" * (LIMIT + 1),
                "pack_id": "p" * (LIMIT + 1),
                "space": "s" * (LIMIT + 1),
                "text": "한" * (LIMIT + 1),
            },
            "label": "L" * (LIMIT + 1),
        }
        out = caps.cap_keyword_fallback(fallback, limit=LIMIT)
        assert out["node"]["id"] == fallback["node"]["id"]
        assert out["node"]["node_id"] == fallback["node"]["node_id"]
        assert out["node"]["pack_id"] == fallback["node"]["pack_id"]
        assert out["node"]["space"] == fallback["node"]["space"]
        assert out["label"] == fallback["label"]
        assert out["node"]["text"] == "한" * LIMIT
        assert out["truncation"]["fields"] == [
            {"path": ["node", "text"], "original_chars": LIMIT + 1}
        ]


# ---------------------------------------------------------------------------
# The unit matches what the server sends
# ---------------------------------------------------------------------------

_PAYLOAD = {
    "text": 'quote " back \\ line \n nul \x00 hangul 한글 emoji 😀 plain abc',
    "nested": [{"k": "한" * 20}],
}


@pytest.fixture
def server():
    from opencrab.mcp.server import MCPServer

    with patch("opencrab.mcp.server.get_settings") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            mcp_server_name="opencrab-test",
            mcp_server_version="0.0.1",
            mcp_protocol_versions=None,
        )
        return MCPServer()


class TestWireUnitMatchesServer:
    def test_legacy_tools_call(self, server):
        with patch("opencrab.mcp.server.dispatch_tool", return_value=_PAYLOAD):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "t1", "arguments": {}}}
            )
        text = response["result"]["content"][0]["text"]
        assert len(json.dumps(text, ensure_ascii=False).encode("utf-8")) == caps._wire_length(_PAYLOAD)

    def test_modern_tools_call(self, server):
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        with patch("opencrab.mcp.server.dispatch_tool", return_value=_PAYLOAD):
            response = server.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "t1", "arguments": {}, "_meta": meta}}
            )
        text = response["result"]["content"][0]["text"]
        assert len(json.dumps(text, ensure_ascii=False).encode("utf-8")) == caps._wire_length(_PAYLOAD)


# ---------------------------------------------------------------------------
# ontology_query with a stubbed hybrid
# ---------------------------------------------------------------------------


def _qr(node_id: str, text: str, **kw: Any) -> QueryResult:
    meta = kw.pop("metadata", None)
    return QueryResult(
        source=kw.pop("source", "vector"),
        node_id=node_id,
        score=kw.pop("score", 0.5),
        text=text,
        metadata=meta if meta is not None else {"node_id": node_id, "pack_id": PACK},
        **kw,
    )


def _query(results: list[QueryResult], **kwargs: Any) -> dict[str, Any]:
    from opencrab.mcp import tools

    hybrid = MagicMock()
    hybrid.query = MagicMock(return_value=QueryOutcome(results=results, warnings=[]))
    ctx = _stub_context(hybrid, sql=_real_sql_with_owned_pack(PACK))
    kwargs.setdefault("include_canonical_ids", False)
    with patch.object(tools, "_get_context", return_value=ctx):
        return tools.ontology_query(question="q", pack_ids=[PACK], **kwargs)


# The response the handler returned before #511 for the fixed input of
# ``test_short_results_serialize_as_before``. It was produced by running the
# pre-change handler (the tree before this change) on that input, not by the
# current code.
_BASELINE_SHORT_RESPONSE = {
    "question": "q",
    "spaces_filter": None,
    "subject_id": "test-user",
    "tenant_id": "default",
    "pipeline": {"bm25": True, "rerank": True, "fts": True},
    "total": 3,
    "results": [
        {
            "source": "vector",
            "node_id": f"n{i}",
            "score": score,
            "text": f"text {i}",
            "metadata": {"node_id": f"n{i}", "pack_id": PACK},
            "graph_context": None,
            "raw_score": None,
        }
        for i, score in enumerate((0.5, 0.4, 0.3))
    ],
    "selected_packs": [],
    "pack_filter": {"pack_ids": [PACK], "auto_pack": False, "include_unpackaged": False},
}


class TestOntologyQueryDescription:
    """The tool description is searched by ``tool_search`` and read by clients."""

    def _description(self) -> str:
        from opencrab.mcp.tools import TOOL_SCHEMAS

        return TOOL_SCHEMAS["ontology_query"]["description"]

    def test_description_does_not_contain_the_substring_pack(self):
        # ``tool_search`` matches descriptions by a case-insensitive substring.
        # A "pack" here moves the tool into the description-match group of
        # the query "pack" (tests/test_tool_catalog_search.py).
        assert "pack" not in self._description().lower()

    @pytest.mark.parametrize(
        "token",
        ["4000", "truncation", "results_truncated", "omitted_results", "response_too_large",
         "ontology_get_node"],
    )
    def test_description_names_the_new_contract(self, token):
        assert token in self._description()


class TestOntologyQueryHandler:
    def test_short_results_serialize_as_before(self):
        results = [
            _qr(f"n{i}", f"text {i}", score=score) for i, score in enumerate((0.5, 0.4, 0.3))
        ]
        response = _query(results)
        assert response == _BASELINE_SHORT_RESPONSE
        assert _json(response) == _json(_BASELINE_SHORT_RESPONSE)

    def test_long_text_and_metadata_are_cut(self):
        big = "한" * 100_000
        result = _qr("n1", big, metadata={"node_id": "n1", "pack_id": PACK, "text": big})
        response = _query([result])
        item = response["results"][0]
        assert item["text"] == "한" * LIMIT and item["metadata"]["text"] == "한" * LIMIT
        assert [f["path"] for f in item["truncation"]["fields"]] == [["text"], ["metadata", "text"]]
        assert caps._wire_length(response) <= BUDGET
        assert result.text == big  # the QueryResult is not touched

    def test_graph_context_is_cut_but_identifiers_stay(self):
        ctx = {"anchor_id": "a" * 9000, "labels": ["L" * 9000], "relation_type": "r"}
        response = _query([_qr("n1", "t", source="graph", graph_context=ctx)])
        out = response["results"][0]["graph_context"]
        assert out["anchor_id"] == "a" * 9000
        assert out["labels"] == ["L" * LIMIT]

    def test_folded_members_are_cut_and_keep_count(self):
        big = "m" * 30_000
        member = _qr("m1", big).to_dict()
        rep = _qr(
            "r1", "short",
            metadata={"node_id": "r1", "pack_id": PACK,
                      "folded_duplicates": {"count": 1, "members": [member]}},
            folded_count=1,
        )
        response = _query([rep])
        block = response["results"][0]["metadata"]["folded_duplicates"]
        assert block["count"] == 1 and len(block["members"]) == 1
        assert block["members"][0]["text"] == "m" * LIMIT
        assert block["members"][0]["truncation"]["fields"][0]["original_chars"] == 30_000
        assert "truncation" not in response["results"][0]

    def test_selected_packs_matched_is_cut(self):
        from opencrab.services.pack_selection import PackSelection

        selection = PackSelection(
            [PACK], [{"pack_id": PACK, "score": 3.0, "matched": ["source:" + "s" * 9000]}], [], False, False
        )
        with patch("opencrab.services.pack_selection.resolve_packs", return_value=selection):
            response = _query([_qr("n1", "t")])
        entry = response["selected_packs"][0]
        assert len(entry["matched"][0]) == LIMIT and "truncation" in entry

    def test_over_budget_drops_tail_results_and_total_counts_returned(self, monkeypatch):
        results = [_qr(f"n{i}", "x" * 3000, score=1.0 - i / 100) for i in range(10)]
        full = _query(results)
        monkeypatch.setattr(
            caps, "QUERY_RESPONSE_CHAR_BUDGET", caps._wire_length(full) - 1
        )
        response = _query(results)
        assert response["total"] == 9 == len(response["results"])
        assert response["results_truncated"] is True and response["omitted_results"] == 1
        assert [r["node_id"] for r in response["results"]] == [f"n{i}" for i in range(9)]

    def test_first_result_over_budget_is_the_fixed_error(self, monkeypatch):
        monkeypatch.setattr(caps, "QUERY_RESPONSE_CHAR_BUDGET", 100)
        assert _query([_qr("n1", "x" * 500)]) == response_too_large_error()

    def test_continuation_uses_fixed_limit_and_next_offset(self, monkeypatch):
        results = [_qr(f"n{i}", "한" * 20, score=1 - i / 10) for i in range(4)]
        full = _query(results, limit=4)
        monkeypatch.setattr(
            caps, "QUERY_RESPONSE_CHAR_BUDGET", caps._wire_length(full) - 1
        )
        first = _query(results, limit=4)
        second = _query(results, limit=4, offset=first["next_offset"])
        assert first["next_offset"] == first["total"]
        assert [item["node_id"] for item in first["results"] + second["results"]] == [
            "n0", "n1", "n2", "n3"
        ]

    @pytest.mark.parametrize("offset", [-1, True, "0"])
    def test_invalid_offset_is_rejected_by_handler(self, offset):
        with pytest.raises(ValueError, match="offset must be an integer >= 0"):
            _query([_qr("n1", "t")], offset=offset)


# ---------------------------------------------------------------------------
# Real stores: every leg, the fold, and the full-body path
# ---------------------------------------------------------------------------

BIG = "alpha beta " + ("한글본문 " * 5000)


@pytest.fixture()
def stack(tmp_path):
    from opencrab.ontology.query import HybridQuery
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    vec = build_vector_store("sqlite-vec", tmp_path)
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    sql = _real_sql_with_owned_pack(PACK)
    hybrid = HybridQuery(vec, graph)
    hybrid._doc_store = docs
    ctx = _stub_context(hybrid, sql=sql)
    ctx["neo4j"] = graph
    try:
        yield {"vec": vec, "docs": docs, "graph": graph, "sql": sql, "hybrid": hybrid, "ctx": ctx}
    finally:
        vec.close()
        graph.close()
        docs.close()
        sql._engine.dispose()


def _write(stack, text: str, source_id: str, **meta: Any) -> dict[str, Any]:
    from opencrab.pack.source_writer import write_source

    return write_source(
        stack["sql"], stack["hybrid"], stack["docs"], stack["vec"], graph=stack["graph"],
        text=text, source_id=source_id, metadata={"space": "evidence", **meta}, pack_id=PACK,
    )


def _node(stack, node_id: str, text: str, store_vector=False, store_doc_node=False, store_source=False):
    props = {"id": node_id, "text": text, "pack_id": PACK, "space": "evidence"}
    stack["graph"].upsert_node("TextUnit", node_id, props, "evidence")
    meta = {"pack_id": PACK, "space": "evidence", "node_id": node_id}
    if store_vector:
        stack["vec"].add_texts([text], [meta], [node_id])
    if store_doc_node:
        stack["docs"].upsert_node_doc("evidence", "TextUnit", node_id, props)
    if store_source:
        stack["docs"].upsert_source(node_id, text, dict(meta))


def _ask(stack, question: str, **kw: Any) -> dict[str, Any]:
    from opencrab.mcp import tools

    kw.setdefault("limit", 5)
    kw.setdefault("use_rerank", False)
    with patch.object(tools, "_get_context", return_value=stack["ctx"]):
        return tools.ontology_query(question=question, pack_ids=[PACK], **kw)


def _get(stack, node_id: str) -> dict[str, Any]:
    from opencrab.mcp import tools

    with patch.object(tools, "_get_context", return_value=stack["ctx"]):
        return tools.ontology_get_node(node_id)


def _by_id(response, node_id):
    return next(r for r in response["results"] if r["node_id"] == node_id)


def _assert_bounded(response: dict[str, Any]) -> None:
    for item in response["results"]:
        assert _long_strings(item, LIMIT, folded="folded_duplicates" in (item.get("metadata") or {})) == []
    assert caps._wire_length(response) <= BUDGET


class TestRealStores:
    def test_vector_leg_is_cut_and_the_full_body_is_read_with_get_node(self, stack):
        _write(stack, BIG, "src:big")
        response = _ask(stack, "alpha beta", limit=1, use_bm25=False, use_fts=False)
        assert response["total"] == 1
        item = response["results"][0]
        assert item["node_id"] == "src:big"
        assert item["truncation"]["fields"][0] == {"path": ["text"], "original_chars": len(BIG)}
        assert item["truncation"]["full_text_tool"] == "ontology_get_node"
        _assert_bounded(response)
        node = _get(stack, item["node_id"])
        assert node["found"] is True and node["properties"]["text"] == BIG

    def test_bm25_leg_is_cut(self, stack):
        _node(stack, "bm:big", "bmtoken " + BIG, store_doc_node=True)
        response = _ask(stack, "bmtoken", use_fts=False)
        item = _by_id(response, "bm:big")
        assert item["source"] == "bm25"
        assert any(f["path"] == ["text"] for f in item["truncation"]["fields"])
        _assert_bounded(response)

    def test_fts_leg_is_cut(self, stack):
        _node(stack, "fts:big", "ftstoken " + BIG, store_source=True)
        response = _ask(stack, "ftstoken", use_bm25=False)
        item = _by_id(response, "fts:big")
        assert item["source"] == "keyword"
        assert item["truncation"]["fields"][0]["path"] == ["text"]
        _assert_bounded(response)

    def test_graph_leg_metadata_text_is_cut(self, stack):
        _write(stack, "gammatoken anchor note", "src:anchor")
        _node(stack, "src:nbr", BIG + " nbr")
        stack["graph"].upsert_edge("TextUnit", "src:anchor", "RELATED_TO", "TextUnit", "src:nbr", {})
        response = _ask(stack, "gammatoken")
        item = _by_id(response, "src:nbr")
        assert item["source"] == "graph"
        assert len(item["text"]) <= LIMIT
        assert item["metadata"]["text"] == (BIG + " nbr")[:LIMIT]
        assert item["truncation"]["fields"] == [
            {"path": ["metadata", "text"], "original_chars": len(BIG + " nbr")}
        ]
        _assert_bounded(response)
        assert _get(stack, "src:nbr")["properties"]["text"] == BIG + " nbr"

    def test_folded_members_from_real_stores(self, stack):
        body = "dupbody " + ("한" * 6000)
        for i in range(3):
            _write(stack, body, f"dup:{i}")
        response = _ask(stack, "dupbody", use_bm25=False, use_fts=False)
        rep = next(r for r in response["results"] if "folded_duplicates" in r["metadata"])
        members = rep["metadata"]["folded_duplicates"]["members"]
        assert rep["metadata"]["folded_duplicates"]["count"] == len(members) == 2
        for member in members:
            assert member["text"] == body[:LIMIT]
            assert member["truncation"]["fields"][0]["original_chars"] == len(body)
            assert member["canonical"]["resolved"] is True
        _assert_bounded(response)

    def test_get_node_after_write_source_boundaries(self, stack):
        # Long id: write_source skips the graph node, the document is still found.
        long_id = "J" * 300
        receipt = _write(stack, "longid zzlong " + BIG, long_id)
        assert receipt["stores"]["graph"].startswith("skipped")
        response = _ask(stack, "zzlong", use_bm25=False)
        assert _by_id(response, long_id)["truncation"]["fields"]
        assert _get(stack, long_id)["found"] is False
        # metadata.node_id naming an id that does not exist.
        _write(stack, "ghost zzghost " + BIG, "src:ghost", node_id="ghost:id")
        response = _ask(stack, "zzghost", use_bm25=False)
        assert _by_id(response, "ghost:id")["truncation"]["fields"]
        assert _get(stack, "ghost:id")["found"] is False
        # metadata.node_id naming another existing node: found, but not this body.
        _write(stack, BIG, "src:real")
        _write(stack, "other zzother " + BIG, "src:other", node_id="src:real")
        response = _ask(stack, "zzother", use_bm25=False)
        item = _by_id(response, "src:real")
        node = _get(stack, item["node_id"])
        assert node["found"] is True and node["properties"]["text"] == BIG
        assert node["properties"]["text"] != "other zzother " + BIG
