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
    _normalize_space,
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
    def test_empty_pack_ids_set_treated_as_nothing_passes(self):
        # issue #147 §3.4(a): None ("no filter") and an empty set ("nothing
        # is visible") no longer collapse to the same "always passes"
        # behaviour -- an empty pack_set now excludes everything.
        assert _node_passes({"pack_id": "p1"}, set(), False) is False
        assert _edge_passes({"pack_id": "p1"}, False, False, set()) is False

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
# _normalize_space (issue #118)
# ---------------------------------------------------------------------------


class TestNormalizeSpace:
    """Precedence (issue #118 codex review [2]): the explicit ``space_id``
    ARGUMENT wins over a conflicting ``properties["space"]`` key -- matches
    what neo4j_store.py's upsert_node already did, and is now shared by all
    three backends via this one function (see its docstring)."""

    def test_truthy_space_id_wins_and_overwrites_props_space(self):
        props, space_id = _normalize_space({"space": "claim"}, "evidence")
        assert props["space"] == "evidence"
        assert space_id == "evidence"

    def test_no_space_id_but_truthy_props_space_is_promoted_to_space_id(self):
        """The column must get populated even when the caller only supplied
        properties["space"] and no explicit space_id -- otherwise the
        SQL/Kuzu space_id predicates could never match this node at all."""
        props, space_id = _normalize_space({"space": "concept"}, None)
        assert props["space"] == "concept"
        assert space_id == "concept"

    def test_falsy_props_space_is_overwritten_by_space_id(self):
        props, space_id = _normalize_space({"space": ""}, "evidence")
        assert props["space"] == "evidence"
        assert space_id == "evidence"

    def test_missing_props_space_is_filled_from_space_id(self):
        props, space_id = _normalize_space({"text": "body"}, "evidence")
        assert props["space"] == "evidence"
        assert space_id == "evidence"

    def test_no_space_id_and_no_props_space_leaves_both_absent(self):
        props, space_id = _normalize_space({"text": "body"}, None)
        assert "space" not in props
        assert space_id is None

    def test_input_dict_not_mutated_when_value_folded_in(self):
        original = {"text": "body"}
        props, _space_id = _normalize_space(original, "evidence")
        assert "space" not in original
        assert props is not original

    def test_input_dict_returned_unchanged_when_nothing_to_fold(self):
        """Nothing to fold means the two ALREADY agree (or space_id wins but
        props["space"] already equals it) -- not merely "props has a
        space", since with this precedence a differing props["space"] is
        always overwritten."""
        original = {"space": "evidence"}
        props, space_id = _normalize_space(original, "evidence")
        assert props is original
        assert space_id == "evidence"

    # -- codex review [3]: type contract must match _merge_space/_valid_space --

    def test_non_string_props_space_is_not_a_valid_value(self):
        """An int/dict truthy properties["space"] is not a usable space
        identifier (see _valid_space) -- ignored, same as if absent."""
        props, space_id = _normalize_space({"space": 5}, None)
        assert space_id is None
        assert props["space"] == 5  # untouched, not promoted into space_id

    def test_non_string_space_id_argument_is_not_promoted(self):
        """A caller passing a non-string space_id (misuse) must never end up
        written into the space_id column -- avoids the PG bind-type-error
        risk codex review [7] flagged."""
        props, space_id = _normalize_space({"text": "body"}, 5)  # type: ignore[arg-type]
        assert space_id is None
        assert "space" not in props

    def test_non_string_space_id_falls_back_to_valid_props_space(self):
        props, space_id = _normalize_space({"space": "concept"}, 5)  # type: ignore[arg-type]
        assert space_id == "concept"
        assert props["space"] == "concept"

    # -- codex review [2]: conflicts are logged, not silently dropped --

    def test_conflicting_values_logs_a_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            _normalize_space({"space": "claim", "id": "n1"}, "evidence")
        assert any(
            "claim" in r.message and "evidence" in r.message and "n1" in r.message
            for r in caplog.records
        )

    def test_no_conflict_does_not_log(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            _normalize_space({"space": "evidence"}, "evidence")
            _normalize_space({}, "evidence")
            _normalize_space({"space": "concept"}, None)
        assert caplog.records == []


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
