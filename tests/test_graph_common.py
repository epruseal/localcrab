"""
Contract tests for opencrab.stores._graph_common (pack-filter predicates,
JSONB coercion, identifier regex).
"""

from __future__ import annotations

from opencrab.stores._graph_common import (
    IDENT_RE,
    _as_dict,
    _edge_passes,
    _node_pack_id,
    _node_passes,
)

# ---------------------------------------------------------------------------
# Normal — pack-filter truth table
# ---------------------------------------------------------------------------


class TestNodePassesNormal:
    def test_no_pack_filter_always_passes(self):
        assert _node_passes({"pack_id": "p1"}, None, False) is True
        assert _node_passes({}, None, False) is True

    def test_pack_match_passes(self):
        assert _node_passes({"pack_id": "p1"}, {"p1", "p2"}, False) is True

    def test_pack_mismatch_excluded(self):
        assert _node_passes({"pack_id": "p3"}, {"p1", "p2"}, False) is False

    def test_unpackaged_included_when_flag_true(self):
        assert _node_passes({}, {"p1"}, True) is True

    def test_unpackaged_excluded_when_flag_false(self):
        assert _node_passes({}, {"p1"}, False) is False


class TestEdgePassesNormal:
    def test_no_pack_filter_always_passes(self):
        assert _edge_passes({"pack_id": "p1"}, False, False, None) is True

    def test_edge_pack_in_set_requires_both_endpoints(self):
        assert _edge_passes({"pack_id": "p1"}, True, True, {"p1"}) is True
        assert _edge_passes({"pack_id": "p1"}, True, False, {"p1"}) is False

    def test_edge_pack_not_in_set_always_excluded(self):
        assert _edge_passes({"pack_id": "p9"}, True, True, {"p1"}) is False

    def test_edge_no_pack_id_requires_both_endpoints(self):
        assert _edge_passes({}, True, True, {"p1"}) is True
        assert _edge_passes({}, True, False, {"p1"}) is False
        assert _edge_passes({}, False, False, {"p1"}) is False


# ---------------------------------------------------------------------------
# Error — malformed inputs
# ---------------------------------------------------------------------------


class TestPackFilterError:
    def test_node_pack_id_non_dict_props_returns_none(self):
        assert _node_pack_id("not-a-dict") is None  # type: ignore[arg-type]
        assert _node_pack_id(None) is None  # type: ignore[arg-type]

    def test_node_pack_id_falsy_pack_id_returns_none(self):
        assert _node_pack_id({"pack_id": ""}) is None
        assert _node_pack_id({"pack_id": None}) is None

    def test_node_passes_malformed_props_treated_as_unpackaged(self):
        # non-dict props -> _node_pack_id returns None -> include_unpackaged governs
        assert _node_passes("garbage", {"p1"}, True) is True  # type: ignore[arg-type]
        assert _node_passes("garbage", {"p1"}, False) is False  # type: ignore[arg-type]

    def test_edge_passes_malformed_edge_props(self):
        assert _edge_passes("garbage", True, True, {"p1"}) is True  # type: ignore[arg-type]
        assert _edge_passes("garbage", True, False, {"p1"}) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestPackFilterEdge:
    def test_empty_pack_ids_set_treated_as_no_filter(self):
        # empty set is falsy -> `if not pack_set` -> always passes
        assert _node_passes({"pack_id": "p1"}, set(), False) is True
        assert _edge_passes({"pack_id": "p1"}, False, False, set()) is True

    def test_none_props_node_passes(self):
        assert _node_passes({}, {"p1"}, True) is True

    def test_edge_endpoints_differ_in_pack(self):
        # src in pack_set, dst not, edge itself has no pack_id -> excluded
        assert _edge_passes({}, True, False, {"p1"}) is False
        # both endpoints pass, edge has no pack_id -> included
        assert _edge_passes({}, True, True, {"p1"}) is True


# ---------------------------------------------------------------------------
# _as_dict
# ---------------------------------------------------------------------------


class TestAsDict:
    def test_dict_passthrough(self):
        d = {"a": 1}
        assert _as_dict(d) is d

    def test_json_string_parsed(self):
        assert _as_dict('{"a": 1}') == {"a": 1}

    def test_none_returns_empty_dict(self):
        assert _as_dict(None) == {}

    def test_invalid_json_returns_empty_dict(self):
        assert _as_dict("not json") == {}

    def test_json_array_string_returns_empty_dict(self):
        assert _as_dict("[1, 2, 3]") == {}

    def test_other_types_return_empty_dict(self):
        assert _as_dict(42) == {}
        assert _as_dict([1, 2]) == {}


# ---------------------------------------------------------------------------
# IDENT_RE
# ---------------------------------------------------------------------------


class TestIdentRe:
    def test_accepts_valid_identifiers(self):
        for ident in ["a", "_a", "abc123", "_", "A_b_C9", "public"]:
            assert IDENT_RE.match(ident), ident

    def test_rejects_invalid_identifiers(self):
        for ident in ["1abc", "a-b", "a.b", "a b", "", "a;drop table x", "a$b"]:
            assert not IDENT_RE.match(ident), ident
