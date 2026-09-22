"""Unit tests for ``opencrab.pack.diagnostics`` -- the axis functions and
the four-way ``classify()`` evidence rule added for issue #407.

Axis-function tests use tiny stub/fake objects (not real stores) so each
one isolates exactly the ``hasattr``/exception-vs-value dispatch the
axis functions add on top of the real ``count_*_scoped`` methods (those
methods are covered against real/mocked stores in
``tests/test_diagnose_residue_count_capabilities.py``).

``classify()``'s test matrix follows the design's four-category
partition directly, with special emphasis on the two must-never-happen
cases the design calls out by name: an unknown axis must never let a row
read ``confirmed_empty``, and a graph-node axis that is not KNOWN
positive must never let a row read ``not_candidate`` on graph grounds
alone (rather than on registry-not-ready grounds).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencrab.pack.diagnostics import (
    classify,
    diagnose_pack,
    doc_nodes_axis,
    doc_sources_axis,
    graph_edges_axis,
    graph_nodes_axis,
    vectors_axis,
)

PACK = "pack-x"


# ---------------------------------------------------------------------------
# Axis functions: hasattr -> not_applicable, exception -> unknown,
# success -> known.
# ---------------------------------------------------------------------------


class _NoMethod:
    """A store with no scoped-count capability at all -- structural
    absence, must read not_applicable."""


class _Raises:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, name: str):
        def _boom(*a, **kw):
            raise self._exc

        return _boom


class _Returns:
    def __init__(self, value: int) -> None:
        self._value = value

    def __getattr__(self, name: str):
        return lambda *a, **kw: self._value


class _RealShaped:
    """A store double with exactly ONE named method -- no blanket
    ``__getattr__``. ``_NoMethod``/``_Raises``/``_Returns`` above all
    respond the same way regardless of WHICH attribute name an axis
    function checks (blanket absence, or a catch-all ``__getattr__``), so
    none of them can tell a correct ``hasattr(store, "count_..._scoped")``
    check apart from a mutated/wrong attribute name. This double only
    defines the one real method, so a hasattr-string mutation (a
    different or empty name) makes it misread as ``not_applicable`` and
    the test that uses it fails -- found via mutation testing (#407)."""

    def __init__(self, method_name: str, value: int) -> None:
        setattr(self, method_name, lambda pack_ids: value)


@pytest.mark.parametrize(
    "axis_fn, method_name",
    [
        (graph_nodes_axis, "count_exported_nodes_scoped"),
        (graph_edges_axis, "count_exported_edges_scoped"),
        (doc_sources_axis, "count_sources_scoped"),
        (doc_nodes_axis, "count_nodes_scoped"),
    ],
)
class TestAxisFunctionsGraphAndDoc:
    def test_missing_method_is_not_applicable(self, axis_fn, method_name):
        assert axis_fn(_NoMethod(), PACK) == {"state": "not_applicable", "count": None}

    def test_kuzu_style_getattr_then_raise_is_unknown(self, axis_fn, method_name):
        """Kuzu's ``KuzuUnavailableGraphStore.__getattr__`` makes hasattr()
        True for every name but raises at call time -- must be caught, not
        misread as not_applicable."""
        from opencrab.common.graph_identity import GraphReadCapabilityUnavailable

        store = _Raises(GraphReadCapabilityUnavailable("capability unavailable"))
        assert axis_fn(store, PACK) == {"state": "unknown", "count": None}

    def test_connection_failure_is_unknown_not_zero(self, axis_fn, method_name):
        store = _Raises(RuntimeError("connection lost"))
        assert axis_fn(store, PACK) == {"state": "unknown", "count": None}

    def test_zero_is_known_not_unknown(self, axis_fn, method_name):
        assert axis_fn(_Returns(0), PACK) == {"state": "known", "count": 0}

    def test_positive_is_known(self, axis_fn, method_name):
        assert axis_fn(_Returns(3), PACK) == {"state": "known", "count": 3}

    def test_real_shaped_double_with_correct_method_name_is_known(self, axis_fn, method_name):
        """Closes a real mutation-testing gap: a hasattr() check mutated
        to the wrong (or empty) attribute name still reads not_applicable
        against every stub above, since none of them discriminate by
        name. This double defines only the real method name, so the
        check must target it exactly."""
        store = _RealShaped(method_name, 9)
        assert axis_fn(store, PACK) == {"state": "known", "count": 9}


class TestVectorsAxis:
    def test_delegates_to_count_pack_vectors_bounded(self, monkeypatch):
        import opencrab.pack.diagnostics as diag

        monkeypatch.setattr(
            diag, "count_pack_vectors_bounded", lambda vec, pack_id, cap: ("known_at_least", 7)
        )
        assert vectors_axis(object(), PACK, cap=5) == {"state": "known_at_least", "count": 7}

    def test_default_cap_is_fork_max_vectors(self, monkeypatch):
        import opencrab.pack.diagnostics as diag
        from opencrab.pack.fork import FORK_MAX_VECTORS

        seen = {}

        def _fake(vec, pack_id, cap):
            seen["cap"] = cap
            return ("known", 0)

        monkeypatch.setattr(diag, "count_pack_vectors_bounded", _fake)
        vectors_axis(object(), PACK)
        assert seen["cap"] == FORK_MAX_VECTORS

    def test_live_chroma_get_failure_is_unknown_not_zero(self):
        """The diagnostic adapter owns observation failure conversion.

        This double follows the initialized Chroma backend shape used by
        ``_vec_backend``. It reaches the real ``.get()`` call, unlike a
        helper mock, then fails as a dropped backend connection would.
        """
        collection = MagicMock()
        collection.get.side_effect = RuntimeError("chroma connection lost")

        class _ChromaVec:
            available = True
            _collection = collection

        assert vectors_axis(_ChromaVec(), PACK) == {"state": "unknown", "count": None}

    def test_live_chroma_get_failure_keeps_diagnosis_incomplete(self):
        collection = MagicMock()
        collection.get.side_effect = RuntimeError("chroma connection lost")

        class _ChromaVec:
            available = True
            _collection = collection

        result = diagnose_pack(PACK, graph=_Returns(0), docs=_Returns(0), vec=_ChromaVec())
        assert result["axes"]["vectors"] == {"state": "unknown", "count": None}
        assert result["classification"] == "incomplete_observation"


# ---------------------------------------------------------------------------
# classify(): four-category partition
# ---------------------------------------------------------------------------


def _axes(
    graph_nodes=("known", 0),
    graph_edges=("known", 0),
    doc_sources=("known", 0),
    doc_nodes=("known", 0),
    vectors=("known", 0),
):
    def _a(pair):
        state, count = pair
        return {"state": state, "count": count}

    return {
        "graph_nodes": _a(graph_nodes),
        "graph_edges": _a(graph_edges),
        "doc_sources": _a(doc_sources),
        "doc_nodes": _a(doc_nodes),
        "vectors": _a(vectors),
    }


class TestClassifyNotCandidate:
    def test_registry_not_ready_short_circuits_before_reading_axes(self):
        """not_candidate on registry grounds must not even need a valid
        axes dict -- callers short-circuiting on a failed readable_pack_ids
        lookup pass {}."""
        assert classify(False, {}) == "not_candidate"

    def test_graph_known_positive_is_not_candidate_regardless_of_other_axes(self):
        axes = _axes(graph_nodes=("known", 5), vectors=("unknown", None))
        assert classify(True, axes) == "not_candidate"

    def test_graph_known_count_of_exactly_one_is_already_positive(self):
        """Boundary case for the ``> 0`` positivity check -- a count of 5
        above does not distinguish ``> 0`` from an off-by-one ``> 1``
        (mutation testing found this gap, #407)."""
        axes = _axes(graph_nodes=("known", 1))
        assert classify(True, axes) == "not_candidate"


class TestClassifyConfirmedEmpty:
    def test_all_known_zero_is_confirmed_empty(self):
        axes = _axes()
        assert classify(True, axes) == "confirmed_empty"

    def test_not_applicable_axes_are_excluded_from_the_all_zero_check(self):
        axes = _axes(vectors=("not_applicable", None))
        assert classify(True, axes) == "confirmed_empty"


class TestClassifyContentResidue:
    @pytest.mark.parametrize(
        "axis_name",
        ["graph_edges", "doc_sources", "doc_nodes", "vectors"],
    )
    def test_any_single_applicable_axis_known_positive_is_residue(self, axis_name):
        axes = _axes(**{axis_name: ("known", 1)})
        assert classify(True, axes) == "content_residue"

    def test_known_at_least_counts_as_residue(self):
        axes = _axes(vectors=("known_at_least", 50001))
        assert classify(True, axes) == "content_residue"

    def test_residue_wins_over_a_simultaneous_unknown_axis(self):
        """Design contract: residue evidence is sufficient on its own --
        an unrelated unknown axis does not downgrade a found residue to
        incomplete_observation."""
        axes = _axes(doc_sources=("known", 4), vectors=("unknown", None))
        assert classify(True, axes) == "content_residue"


class TestClassifyIncompleteObservation:
    def test_graph_unknown_is_incomplete_not_confirmed_empty(self):
        """The design's headline must-never-happen case: an unknown graph
        axis must never let a row read confirmed_empty."""
        axes = _axes(graph_nodes=("unknown", None))
        assert classify(True, axes) == "incomplete_observation"

    def test_graph_not_applicable_is_incomplete(self):
        axes = _axes(graph_nodes=("not_applicable", None))
        assert classify(True, axes) == "incomplete_observation"

    def test_graph_known_zero_but_one_other_axis_unknown_is_incomplete(self):
        """All axes known-zero except ONE unavailable -- must be
        incomplete_observation, never confirmed_empty (explicit MLP fixture
        case from the design)."""
        axes = _axes(vectors=("unknown", None))
        assert classify(True, axes) == "incomplete_observation"

    def test_multiple_unknowns_still_incomplete_not_confirmed_empty(self):
        axes = _axes(doc_sources=("unknown", None), vectors=("unknown", None))
        assert classify(True, axes) == "incomplete_observation"

    @pytest.mark.parametrize("axis_name", ["doc_sources", "doc_nodes", "vectors"])
    def test_graph_unknown_with_a_positive_other_axis_is_still_incomplete(self, axis_name):
        """Round-5 arbitration success gate: incomplete_observation is the
        explicit complement of the other three, with no extra "no axis is
        known positive" side condition -- a real partial-outage shape
        (graph down, docs/vectors up and holding real content) must still
        surface as incomplete_observation, never silently read as
        content_residue or confirmed_empty on the strength of the graph
        axis's absence alone (#407)."""
        axes = _axes(graph_nodes=("unknown", None), **{axis_name: ("known", 3)})
        assert classify(True, axes) == "incomplete_observation"

    def test_every_state_combination_is_covered_by_exactly_one_label(self):
        """Exhaustiveness/non-overlap check across the full state cross
        product for graph_nodes x one other axis (vectors), holding the
        remaining three at known-zero. classify() must never raise and
        must return exactly one of the four labels for every combination.
        See ``test_full_state_space_is_exhaustive_and_non_overlapping``
        below for the genuinely full 5-axis sweep this predates."""
        states = [
            ("known", 0), ("known", 3), ("known_at_least", 5),
            ("unknown", None), ("not_applicable", None),
        ]
        labels = {"not_candidate", "confirmed_empty", "content_residue", "incomplete_observation"}
        for g_state, g_count in states:
            for v_state, v_count in states:
                axes = _axes(graph_nodes=(g_state, g_count), vectors=(v_state, v_count))
                result = classify(True, axes)
                assert result in labels, (g_state, g_count, v_state, v_count, result)

    def test_full_state_space_is_exhaustive_and_non_overlapping(self):
        """Mechanical sweep of the entire registry-ready state space (all
        five axes independently, not a hand-picked slice) demanded by the
        design review's completeness nail-down (#407): the four
        classifications must cover every reachable combination and never
        leave one unmapped. ``classify()`` can only ever return a single
        string from a strictly-ordered if/elif/else chain, so "no
        combination lands in more than one label" is a structural property
        of that control flow rather than a separate runtime check -- this
        sweep instead pins the complementary half: that every combination
        lands in exactly one of the four VALID labels, none escape as an
        unrecognized string or an exception. The full product is
        4 x 5**4 = 2500 combinations, small enough to enumerate outright."""
        import itertools

        graph_states = [
            ("known", 0), ("known", 3), ("unknown", None), ("not_applicable", None),
        ]
        other_states = [
            ("known", 0), ("known", 3), ("known_at_least", 5),
            ("unknown", None), ("not_applicable", None),
        ]
        labels = {"not_candidate", "confirmed_empty", "content_residue", "incomplete_observation"}
        seen = set()
        for g, ge, ds, dn, v in itertools.product(
            graph_states, other_states, other_states, other_states, other_states
        ):
            axes = _axes(graph_nodes=g, graph_edges=ge, doc_sources=ds, doc_nodes=dn, vectors=v)
            result = classify(True, axes)
            assert result in labels, (g, ge, ds, dn, v, result)
            seen.add(result)
        # Sanity check on the sweep itself: a sweep that only ever produced
        # one or two labels would "pass" vacuously without exercising the
        # other branches at all.
        assert seen == labels


# ---------------------------------------------------------------------------
# diagnose_pack(): assembly + classification wiring
# ---------------------------------------------------------------------------


class _DistinctGraph:
    """Each method returns a value distinct from every other axis's
    stub method below, so a swapped call target in ``diagnose_pack``'s
    axes-dict assembly (e.g. ``graph_edges_axis``'s result landing under
    the ``"graph_nodes"`` key) produces a mismatched count instead of
    slipping through unnoticed."""

    def count_exported_nodes_scoped(self, pack_ids):
        return 10

    def count_exported_edges_scoped(self, pack_ids):
        return 20


class _DistinctDocs:
    def count_sources_scoped(self, pack_ids):
        return 30

    def count_nodes_scoped(self, pack_ids):
        return 40


class TestDiagnosePackAxisWiring:
    """Closes a real mutation-testing gap: ``TestDiagnosePack`` below uses
    ``_Returns()``/``_NoMethod()`` doubles that respond identically no
    matter which method name ``diagnose_pack`` calls, so a call-target
    swap between two axis functions in its axes-dict assembly (#407)
    produced an identical result and went undetected. These doubles give
    each axis a distinct value so a swap surfaces as a wrong count under
    the wrong key."""

    def test_each_axis_key_carries_its_own_functions_result(self, monkeypatch):
        import opencrab.pack.diagnostics as diag

        monkeypatch.setattr(
            diag, "count_pack_vectors_bounded", lambda vec, pack_id, cap: ("known", 50)
        )
        result = diagnose_pack(PACK, graph=_DistinctGraph(), docs=_DistinctDocs(), vec=object())
        assert result["axes"]["graph_nodes"] == {"state": "known", "count": 10}
        assert result["axes"]["graph_edges"] == {"state": "known", "count": 20}
        assert result["axes"]["doc_sources"] == {"state": "known", "count": 30}
        assert result["axes"]["doc_nodes"] == {"state": "known", "count": 40}
        assert result["axes"]["vectors"] == {"state": "known", "count": 50}


class TestDiagnosePack:
    def test_assembles_all_five_axes_and_classifies(self):
        result = diagnose_pack(
            PACK, graph=_Returns(0), docs=_Returns(0), vec=_NoMethod()
        )
        assert result["pack_id"] == PACK
        assert set(result["axes"]) == {
            "graph_nodes", "graph_edges", "doc_sources", "doc_nodes", "vectors",
        }
        # vec has no recognizable backend shape -> count_pack_vectors_bounded
        # itself returns ("unknown", None) for it (not not_applicable, since
        # vectors_axis has no hasattr gate of its own -- see its docstring).
        assert result["axes"]["vectors"]["state"] == "unknown"
        assert result["classification"] == "incomplete_observation"

    def test_all_confirmed_zero_classifies_confirmed_empty(self):
        result = diagnose_pack(PACK, graph=_Returns(0), docs=_Returns(0), vec=object())
        # vec has no backend shape either -> unknown -> incomplete_observation,
        # confirming the vectors axis is never silently treated as zero.
        assert result["classification"] == "incomplete_observation"
