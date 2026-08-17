"""Two-user read isolation for #147 (execution 4 of #143's authorization design).

Every other test file in this suite asks "does this feature work". This one
asks "can user B see user A's data", and it is the file that has to fail if
someone later loosens a pack predicate. It therefore builds REAL stores
(SQLite-backed graph, doc and registry) rather than mocking them: the
predicates under test are SQL, and a MagicMock graph would happily "pass"
a query whose WHERE clause was deleted.

Layout of the fixture world, used by every test below:

    alice  owns  pack-a       (private)
    bob    owns  pack-b       (private)
    alice  owns  pack-public  (public-read)
    carol  owns  nothing at all

so alice's readable scope is {pack-a, pack-public}, bob's is
{pack-b, pack-public}, and carol's is {pack-public} -- owning nothing is
not the same as reading nothing. Anything that returns a pack-b row to
alice is a leak; anything that returns zero pack-public rows to bob is
over-scoping (a test that only asserted "0 rows" would pass on a build
that returned nothing at all, which is why both directions are checked).
The genuinely-empty scope is constructed where it is needed, by making the
public pack private first.

The store-level tests at the bottom are deliberately NOT routed through an
entry point. Entry points short-circuit an empty scope before the store is
reached, so an entry-point-only suite would keep passing even if every
store helper went back to reading an empty pack set as "no filter" -- which
is exactly the bug #147 had to fix.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.ontology.pack_provenance import in_pack_scope, scope_pack_id
from opencrab.pack.ownership import create_pack, set_visibility
from opencrab.pack.read_scope import (
    RegistryGraphMismatchError,
    assert_registry_covers_graph,
    narrow,
    read_scope,
)

PACK_A = "pack-a"
PACK_B = "pack-b"
PACK_PUBLIC = "pack-public"


# ---------------------------------------------------------------------------
# Fixtures: real stores, three users, four packs
# ---------------------------------------------------------------------------


@pytest.fixture
def sql(tmp_path):
    from opencrab.stores.sql_store import SQLStore

    return SQLStore(f"sqlite:///{tmp_path / 'registry.db'}")


@pytest.fixture
def graph(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    return LocalGraphStore(str(tmp_path / "graph.db"))


@pytest.fixture
def docs(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "docs.db"))


@pytest.fixture
def users(sql):
    """alice/bob/carol as Principals, with the pack registry populated."""
    alice = Principal(user_id=create_user(sql, "Alice"), is_local=False, disabled=False)
    bob = Principal(user_id=create_user(sql, "Bob"), is_local=False, disabled=False)
    carol = Principal(user_id=create_user(sql, "Carol"), is_local=False, disabled=False)

    create_pack(sql, alice.user_id, PACK_A, title="Alice private")
    create_pack(sql, bob.user_id, PACK_B, title="Bob private")
    create_pack(sql, alice.user_id, PACK_PUBLIC, title="Shared")
    set_visibility(sql, alice, PACK_PUBLIC, "public-read")

    return {"alice": alice, "bob": bob, "carol": carol}


def _node(graph, docs, pack_id: str, node_id: str, *, extra: dict[str, Any] | None = None):
    """Write one node into BOTH stores, tagged with pack_id.

    Both, because the read paths disagree about which store answers:
    ``ontology_list_nodes`` uses the graph store when a pack is named and
    the doc store when one is not, so a fixture that only populated one of
    them would leave half the entry points untested.
    """
    props = {"pack_id": pack_id, "node_id": node_id, "title": f"title of {node_id}"}
    props.update(extra or {})
    graph.upsert_node("Document", node_id, dict(props), space_id="resource")
    docs.upsert_node_doc("resource", "Document", node_id, dict(props))


@pytest.fixture
def seeded(graph, docs, users):
    _node(graph, docs, PACK_A, "a-secret")
    _node(graph, docs, PACK_B, "b-secret")
    _node(graph, docs, PACK_PUBLIC, "shared-doc")
    return users


@pytest.fixture
def ctx(graph, docs, sql):
    """A tool context wired to the real stores.

    ``hybrid``/``builder``/``impact`` are left as mocks except where a test
    needs them: this file is about the pack predicates, and the retrieval
    pipeline has its own suites.
    """
    from opencrab.ontology.impact import ImpactEngine

    return {
        "neo4j": graph,
        "mongo": docs,
        "sql": sql,
        "chroma": MagicMock(available=False),
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": ImpactEngine(graph, sql),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }


def _as(ctx, principal):
    """Run a tool as ``principal`` against ``ctx``."""

    class _Ctx:
        def __enter__(self):
            self._patch = patch("opencrab.mcp.tools._get_context", return_value=ctx)
            self._patch.start()
            self._scope = principal_scope(principal)
            self._scope.__enter__()
            return self

        def __exit__(self, *exc):
            self._scope.__exit__(*exc)
            self._patch.stop()
            return False

    return _Ctx()


def _ids(nodes: list[dict[str, Any]]) -> set[str]:
    """node_ids out of either shape a read path returns.

    The tools normalise to ``{"node_id", "properties"}`` while the store
    export methods return ``{"props", "labels"}``. Reading only one of them
    would make every "the foreign id is absent" assertion vacuously true
    against the other -- an empty set contains nothing, including what the
    test was supposed to catch.
    """
    out = set()
    for n in nodes:
        bag = n.get("properties") or n.get("props") or {}
        nid = n.get("node_id") or bag.get("node_id")
        if nid:
            out.add(nid)
    assert not nodes or out, "extracted no ids -- the assertion below would be vacuous"
    return out


# ---------------------------------------------------------------------------
# 1. Cross-user isolation, and the other direction
# ---------------------------------------------------------------------------


class TestCrossUserIsolation:
    def test_scope_is_exactly_owned_plus_public(self, sql, seeded):
        assert read_scope(sql, seeded["alice"]) == frozenset({PACK_A, PACK_PUBLIC})
        assert read_scope(sql, seeded["bob"]) == frozenset({PACK_B, PACK_PUBLIC})
        # Carol owns nothing, so she sees exactly the public pack and no
        # more -- "owns nothing" is not the same as "reads nothing".
        assert read_scope(sql, seeded["carol"]) == frozenset({PACK_PUBLIC})

    def test_list_nodes_without_pack_id_shows_own_and_public_only(self, ctx, seeded):
        from opencrab.mcp.tools import ontology_list_nodes

        with _as(ctx, seeded["alice"]):
            got = _ids(ontology_list_nodes()["nodes"])
        # Both directions in one assertion: bob's row absent AND the shared
        # row present. Asserting only the absence would pass on a build that
        # returned nothing at all.
        assert got == {"a-secret", "shared-doc"}

    def test_list_nodes_without_pack_id_is_symmetric_for_bob(self, ctx, seeded):
        from opencrab.mcp.tools import ontology_list_nodes

        with _as(ctx, seeded["bob"]):
            got = _ids(ontology_list_nodes()["nodes"])
        assert got == {"b-secret", "shared-doc"}

    def test_get_node_cannot_reach_another_users_node(self, ctx, seeded):
        from opencrab.mcp.tools import ontology_get_node

        with _as(ctx, seeded["alice"]):
            own = ontology_get_node("a-secret")
            shared = ontology_get_node("shared-doc")
            foreign = ontology_get_node("b-secret")
        assert own["found"] is True
        assert shared["found"] is True
        assert foreign["found"] is False

    def test_list_edges_only_returns_edges_whose_endpoints_are_readable(
        self, ctx, graph, docs, seeded
    ):
        from opencrab.mcp.tools import ontology_list_edges

        _node(graph, docs, PACK_B, "b-other")
        graph.upsert_edge("Document", "b-secret", "relates_to", "Document", "b-other", {})
        graph.upsert_edge(
            "Document", "shared-doc", "relates_to", "Document", "b-secret", {}
        )

        with _as(ctx, seeded["alice"]):
            edges = ontology_list_edges()["edges"]

        # Neither edge may appear: the first has both endpoints in pack-b,
        # the second has one. An edge row carries BOTH endpoints' full
        # properties, so a single unreadable endpoint discloses that node.
        for e in edges:
            for side in ("source_props", "target_props"):
                assert (e.get(side) or {}).get("pack_id") != PACK_B


# ---------------------------------------------------------------------------
# 2. Existence must not leak (#143 invariant 7)
# ---------------------------------------------------------------------------


class TestNoExistenceLeak:
    def test_foreign_private_pack_reads_identically_to_a_nonexistent_one(
        self, ctx, seeded
    ):
        from opencrab.mcp.tools import ontology_list_edges, ontology_list_nodes

        with _as(ctx, seeded["alice"]):
            foreign_nodes = ontology_list_nodes(pack_id=PACK_B)
            absent_nodes = ontology_list_nodes(pack_id="no-such-pack-at-all")
            foreign_edges = ontology_list_edges(pack_id=PACK_B)
            absent_edges = ontology_list_edges(pack_id="no-such-pack-at-all")

        # Identical apart from the pack_id echoed back, which is the
        # caller's own input.
        assert foreign_nodes["nodes"] == absent_nodes["nodes"] == []
        assert foreign_nodes["total"] == absent_nodes["total"] == 0
        assert foreign_edges["edges"] == absent_edges["edges"] == []
        assert foreign_edges["total"] == absent_edges["total"] == 0

    def test_get_node_foreign_id_reads_identically_to_a_nonexistent_id(
        self, ctx, seeded
    ):
        from opencrab.mcp.tools import ontology_get_node

        with _as(ctx, seeded["alice"]):
            foreign = ontology_get_node("b-secret")
            absent = ontology_get_node("no-such-node")
        assert foreign == {"found": False, "node_id": "b-secret"}
        assert absent == {"found": False, "node_id": "no-such-node"}
        assert set(foreign) == set(absent)

    def test_impact_on_foreign_node_matches_a_nonexistent_node(
        self, ctx, graph, docs, seeded
    ):
        """The foreign anchor must have REAL neighbours.

        Without them the traversal returns empty regardless of the pack
        filter, and `find_neighbors(pack_ids=...)` can be reverted to
        unscoped with this test still green (found by mutation).
        """
        from opencrab.mcp.tools import ontology_impact

        _node(graph, docs, PACK_B, "b-child")
        graph.upsert_edge("Document", "b-secret", "relates_to", "Document", "b-child", {})

        with _as(ctx, seeded["alice"]):
            foreign = ontology_impact("b-secret")
            absent = ontology_impact("no-such-node")
        assert foreign["affected_nodes"] == []

        # Bob reaches the same neighbour, so the empty list above is the
        # scope and not an empty graph.
        with _as(ctx, seeded["bob"]):
            owned = ontology_impact("b-secret")
        assert owned["affected_nodes"] != []

        def _shape(r):
            # summary embeds the node_id the CALLER supplied, so normalise
            # that one token out; everything else must match exactly.
            out = {k: v for k, v in r.items() if k != "node_id"}
            out["summary"] = out["summary"].replace(r["node_id"], "<id>")
            return out

        assert _shape(foreign) == _shape(absent)

    def test_lever_simulate_on_foreign_lever_matches_an_unknown_lever(
        self, ctx, graph, docs, seeded
    ):
        """The foreign lever must have REAL relations for this to prove anything.

        A lever with no out-edges answers with empty lists whether or not
        any scoping exists, so a fixture without them lets the entire
        anchor gate and post-filter be deleted with this test still green
        (found by mutation).
        """
        from opencrab.mcp.tools import ontology_lever_simulate

        _node(graph, docs, PACK_B, "b-lever")
        _node(graph, docs, PACK_B, "b-outcome")
        _node(graph, docs, PACK_B, "b-concept")
        graph.upsert_edge("Document", "b-lever", "raises", "Document", "b-outcome", {})
        graph.upsert_edge("Document", "b-lever", "affects", "Document", "b-concept", {})

        with _as(ctx, seeded["alice"]):
            foreign = ontology_lever_simulate("b-lever", "raises", 0.5)
            absent = ontology_lever_simulate("no-such-lever", "raises", 0.5)
        assert foreign["predicted_outcome_changes"] == absent["predicted_outcome_changes"] == []
        assert foreign["affected_concepts"] == absent["affected_concepts"] == []

        # The other direction: bob DOES see them, so the emptiness above is
        # the scope talking and not an inert fixture.
        with _as(ctx, seeded["bob"]):
            owned = ontology_lever_simulate("b-lever", "raises", 0.5)
        assert [o["node_id"] for o in owned["predicted_outcome_changes"]] == ["b-outcome"]
        assert [c["node_id"] for c in owned["affected_concepts"]] == ["b-concept"]


# ---------------------------------------------------------------------------
# 3. Forgeable predicates (the two the design names)
# ---------------------------------------------------------------------------


class TestForgedPredicates:
    def test_source_fields_naming_another_pack_do_not_join_that_pack(
        self, ctx, graph, docs, seeded
    ):
        """Regression for the 3-way OR (`pack_id = X OR source = X OR source_id = X`).

        Bob writes his own node with alice's pack_id in the caller-controlled
        `source`/`source_id` properties. Under the old predicate that node
        joined pack-a's result set for anyone querying pack-a.
        """
        _node(
            graph,
            docs,
            PACK_B,
            "b-forged",
            extra={"source": PACK_A, "source_id": PACK_A},
        )
        from opencrab.mcp.tools import ontology_list_nodes

        with _as(ctx, seeded["alice"]):
            got = _ids(ontology_list_nodes(pack_id=PACK_A)["nodes"])
        assert "b-forged" not in got
        assert got == {"a-secret"}

    def test_path_shaped_source_id_does_not_confer_membership(self):
        """Regression for `infer_pack_id`'s `/packs/<id>/` path inference.

        `in_pack_scope` must read pack_id only. A row whose pack_id is
        absent but whose source_id spells a readable pack must not pass.
        """
        forged = {"metadata": {"source_id": f"/data/packs/{PACK_A}/stage/x.md"}}
        assert scope_pack_id(forged) is None
        assert in_pack_scope(forged, {PACK_A}) is False

    def test_unattributed_row_never_passes(self):
        assert in_pack_scope({"metadata": {}}, {PACK_A}) is False
        assert in_pack_scope({"properties": {"pack_id": ""}}, {PACK_A}) is False


# ---------------------------------------------------------------------------
# 4. The empty scope: fail-closed everywhere
# ---------------------------------------------------------------------------


class TestEmptyScopeFailsClosed:
    def test_user_with_no_packs_sees_nothing_through_the_read_tools(
        self, ctx, sql, seeded
    ):
        from opencrab.mcp.tools import ontology_get_node, ontology_list_edges, ontology_list_nodes

        # A genuinely empty scope needs no public pack to exist either --
        # otherwise carol still reads that one, correctly.
        set_visibility(sql, seeded["alice"], PACK_PUBLIC, "private")
        assert read_scope(sql, seeded["carol"]) == frozenset()

        with _as(ctx, seeded["carol"]):
            assert ontology_list_nodes()["nodes"] == []
            assert ontology_list_nodes()["total"] == 0
            assert ontology_list_edges()["edges"] == []
            assert ontology_get_node("a-secret")["found"] is False
            assert ontology_get_node("shared-doc")["found"] is False

    def test_in_pack_scope_rejects_on_an_empty_allow_set(self):
        assert in_pack_scope({"properties": {"pack_id": PACK_A}}, set()) is False

    def test_narrow_never_widens(self):
        assert narrow(frozenset(), None) == ([], False)
        assert narrow(frozenset(), [PACK_A]) == ([], True)
        assert narrow(frozenset({PACK_A}), [PACK_A, PACK_B]) == ([PACK_A], True)
        assert narrow(frozenset({PACK_A}), None) == ([PACK_A], False)


class TestStoreLevelEmptyScope:
    """The store helpers themselves, with no entry point in front of them.

    An entry point returns early on an empty scope, so these would keep
    passing through the tools even if the helpers reverted to reading an
    empty pack set as "no filter". Calling the stores directly is the only
    way to pin it.
    """

    def test_scoped_graph_methods_return_nothing_for_an_empty_scope(self, graph, seeded):
        assert graph.export_nodes_scoped([], limit=100) == []
        assert graph.count_exported_nodes_scoped([]) == 0
        assert graph.export_edges_scoped([], limit=100) == []
        assert graph.get_node_by_id_scoped("a-secret", []) is None
        assert graph.search_nodes("title", pack_ids=[], limit=10) == []

    def test_doc_list_nodes_scoped_returns_nothing_for_an_empty_scope(self, docs, seeded):
        assert docs.list_nodes_scoped([], limit=100) == []

    def test_find_neighbors_returns_nothing_for_an_empty_scope(self, graph, seeded):
        # An edge has to exist, or this passes against the pre-#147
        # fail-open too: a node with no neighbours returns [] either way.
        graph.upsert_edge("Document", "a-secret", "relates_to", "Document", "shared-doc", {})
        assert graph.find_neighbors("a-secret", pack_ids=[]) == []
        assert graph.find_neighbors("a-secret", pack_ids=[PACK_A, PACK_PUBLIC]) != []

    def test_node_and_edge_pass_helpers_distinguish_none_from_empty(self):
        from opencrab.stores._graph_common import _edge_passes, _node_passes

        props = {"pack_id": PACK_A}
        # None = "no filter", for the legacy non-authorization callers.
        assert _node_passes(props, None, False) is True
        # Empty = "nothing is readable". The flip this issue exists for.
        assert _node_passes(props, set(), False) is False
        assert _edge_passes({}, True, True, set()) is False


# ---------------------------------------------------------------------------
# 5. Scoped predicates: correctness beyond the empty case
# ---------------------------------------------------------------------------


class TestScopedStorePredicates:
    def test_get_node_by_id_scoped_is_homonym_safe(self, graph, docs, seeded):
        """Same node_id in two packs under two node_types.

        The unscoped lookup matches on node_id alone (the PK is
        (node_type, node_id)), so each caller must get THEIR row -- not a
        coin flip, and not a false "not found".
        """
        graph.upsert_node("Document", "dup", {"pack_id": PACK_A, "node_id": "dup"}, space_id="resource")
        graph.upsert_node("Concept", "dup", {"pack_id": PACK_B, "node_id": "dup"}, space_id="concept")

        assert graph.get_node_by_id_scoped("dup", [PACK_A])["pack_id"] == PACK_A
        assert graph.get_node_by_id_scoped("dup", [PACK_B])["pack_id"] == PACK_B
        assert graph.get_node_by_id_scoped("dup", [PACK_PUBLIC]) is None

    def test_edge_predicate_requires_both_endpoints(self, graph, docs, seeded):
        _node(graph, docs, PACK_A, "a-other")
        graph.upsert_edge("Document", "a-secret", "relates_to", "Document", "a-other", {})
        graph.upsert_edge("Document", "a-secret", "relates_to", "Document", "b-secret", {})

        edges = graph.export_edges_scoped([PACK_A], limit=100)
        pairs = {
            ((e["source_props"] or {}).get("node_id"), (e["target_props"] or {}).get("node_id"))
            for e in edges
        }
        assert ("a-secret", "a-other") in pairs
        assert ("a-secret", "b-secret") not in pairs

    def test_edge_spanning_two_readable_packs_is_returned(self, graph, docs, seeded):
        """A cross-pack edge inside one scope must survive.

        This is the case a chunked IN-list would have dropped: the two
        endpoints live in different packs, so any scheme that filters each
        endpoint against a different subset of the scope loses the edge
        entirely, not just past a limit.
        """
        graph.upsert_edge("Document", "a-secret", "relates_to", "Document", "shared-doc", {})
        edges = graph.export_edges_scoped(sorted({PACK_A, PACK_PUBLIC}), limit=100)
        pairs = {
            ((e["source_props"] or {}).get("node_id"), (e["target_props"] or {}).get("node_id"))
            for e in edges
        }
        assert ("a-secret", "shared-doc") in pairs

    def test_unattributed_rows_are_excluded_by_the_scoped_predicate(self, graph, docs, seeded):
        graph.upsert_node("Document", "orphan", {"node_id": "orphan"}, space_id="resource")
        docs.upsert_node_doc("resource", "Document", "orphan", {"node_id": "orphan"})

        got = _ids(graph.export_nodes_scoped(sorted({PACK_A, PACK_PUBLIC}), limit=100))
        assert "orphan" not in got
        assert _ids(docs.list_nodes_scoped(sorted({PACK_A, PACK_PUBLIC}), limit=100)) == {
            "a-secret",
            "shared-doc",
        }

    def test_large_scope_matches_small_scope(self, graph, docs, seeded):
        """1200 pack_ids must behave exactly like 2.

        Well past SQLite's lowest ``SQLITE_MAX_VARIABLE_NUMBER`` (999), so a
        per-value bind expansion raises "too many SQL variables" here. Result
        equality is asserted, not just absence of an exception: several
        callers swallow store exceptions, which would turn the blow-up into a
        silent zero-row answer.
        """
        big = sorted({PACK_A, PACK_PUBLIC} | {f"filler-{i}" for i in range(1200)})

        assert _ids(graph.export_nodes_scoped(big, limit=500)) == _ids(
            graph.export_nodes_scoped(sorted({PACK_A, PACK_PUBLIC}), limit=500)
        )
        assert graph.count_exported_nodes_scoped(big) == graph.count_exported_nodes_scoped(
            sorted({PACK_A, PACK_PUBLIC})
        )
        assert _ids(docs.list_nodes_scoped(big, limit=500)) == _ids(
            docs.list_nodes_scoped(sorted({PACK_A, PACK_PUBLIC}), limit=500)
        )
        assert _ids(graph.search_nodes("title", pack_ids=big, limit=50)) == _ids(
            graph.search_nodes("title", pack_ids=sorted({PACK_A, PACK_PUBLIC}), limit=50)
        )
        assert graph.find_neighbors("a-secret", pack_ids=big) == graph.find_neighbors(
            "a-secret", pack_ids=sorted({PACK_A, PACK_PUBLIC})
        )

    def test_pack_id_type_parity_with_the_python_rule(self, graph, seeded):
        """Falsy pack_ids are excluded by SQL exactly as Python excludes them.

        `""` / `0` / `false` are "no pack_id" to ``_node_pack_id`` and
        ``scope_pack_id``; the SQL predicate must not let them match a scope
        entry that happens to spell the same text.
        """
        for node_id, pid in (("empty", ""), ("zero", 0), ("false", False)):
            graph.upsert_node(
                "Document", node_id, {"pack_id": pid, "node_id": node_id}, space_id="resource"
            )
        got = _ids(graph.export_nodes_scoped(sorted({PACK_A, "", "0", "false", "False"}), limit=100))
        assert got == {"a-secret"}


class TestRetrievalPipelineScoping:
    """HybridQuery itself, against real stores.

    The list tools and the pipeline reach the data through different code,
    and only the pipeline is what ``ontology_query`` / ``POST /api/query``
    actually run. Mutation testing showed the rest of this file never called
    ``query()`` or any leg with an empty scope, so every empty-scope guard in
    ``opencrab/ontology/query.py`` could be deleted with the suite green.
    """

    @pytest.fixture
    def hybrid(self, graph, docs):
        from opencrab.ontology.query import HybridQuery

        h = HybridQuery(MagicMock(available=False), graph)
        h._doc_store = docs
        return h

    def test_query_returns_nothing_for_an_empty_scope(self, hybrid, seeded):
        # Data exists; only the scope is empty. Asserting on an empty store
        # would prove nothing.
        assert hybrid.query("title", pack_ids=[]).results == []

    def test_query_returns_own_and_public_only(self, hybrid, sql, seeded):
        alice = sorted(read_scope(sql, seeded["alice"]))
        got = {r.node_id for r in hybrid.query("title", pack_ids=alice).results}
        assert "b-secret" not in got
        assert got & {"a-secret", "shared-doc"}

    @pytest.mark.parametrize(
        "leg", ["_bm25_search", "_fts_search", "_vector_search", "_graph_expand"]
    )
    def test_every_leg_is_empty_for_an_empty_scope(self, hybrid, graph, seeded, leg):
        graph.upsert_edge("Document", "a-secret", "relates_to", "Document", "shared-doc", {})
        fn = getattr(hybrid, leg)
        if leg == "_graph_expand":
            assert fn(["a-secret"], 1, 10, pack_ids=[]) == []
        else:
            assert fn("title", None, 10, pack_ids=[]) == []

    def test_vector_leg_is_empty_for_an_empty_scope_even_with_hits_available(
        self, graph, docs, seeded
    ):
        """The vector leg is the one that genuinely needs query.py's guard.

        `_build_chroma_where` cannot express "match nothing" -- with no
        clauses it returns None, which the vector store reads as "no
        filter". So unlike the SQL-backed legs, this one has no second line
        of defence underneath it: remove the empty-scope short-circuit and
        an empty scope becomes an unfiltered similarity search. A store
        double that is `available` and returns hits is required to show it;
        an unavailable one short-circuits earlier and proves nothing.
        """
        from opencrab.ontology.query import HybridQuery

        chroma = MagicMock(available=True)
        chroma.query.return_value = [
            {"id": "b-secret", "document": "leak", "distance": 0.1,
             "metadata": {"node_id": "b-secret", "pack_id": PACK_B}},
        ]
        h = HybridQuery(chroma, graph)
        h._doc_store = docs

        assert h._vector_search("q", None, 10, pack_ids=[]) == []
        # And the store was never even asked -- the scope is answered before
        # a where-clause that cannot express "nothing" is ever built.
        chroma.query.assert_not_called()

        # Same double, non-empty scope: hits do flow, so the emptiness above
        # is the scope and not a broken double.
        assert h._vector_search("q", None, 10, pack_ids=[PACK_B]) != []

    def test_graph_expand_is_not_empty_when_the_scope_allows_it(
        self, hybrid, graph, sql, seeded
    ):
        """Pins that the empty-scope assertions above are about the scope."""
        graph.upsert_edge("Document", "a-secret", "relates_to", "Document", "shared-doc", {})
        alice = sorted(read_scope(sql, seeded["alice"]))
        assert hybrid._graph_expand(["a-secret"], 1, 10, pack_ids=alice) != []

    def test_keyword_search_fallback_is_scoped(self, hybrid, sql, seeded):
        """The REST zero-result fallback. Its store method had no pack
        parameter at all before #147."""
        alice = sorted(read_scope(sql, seeded["alice"]))
        got = {
            (r.get("node") or {}).get("node_id")
            for r in hybrid.keyword_search("title", pack_ids=alice, limit=50)
        }
        assert "b-secret" not in got
        assert got == {"a-secret", "shared-doc"}
        assert hybrid.keyword_search("title", pack_ids=[], limit=50) == []


class TestSearchNodesScoping:
    """``search_nodes`` directly: the predicate the fallback above depends on."""

    def test_search_nodes_excludes_other_users_rows(self, graph, sql, seeded):
        alice = sorted(read_scope(sql, seeded["alice"]))
        got = _ids(graph.search_nodes("title", pack_ids=alice, limit=50))
        assert got == {"a-secret", "shared-doc"}

        bob = sorted(read_scope(sql, seeded["bob"]))
        assert _ids(graph.search_nodes("title", pack_ids=bob, limit=50)) == {
            "b-secret",
            "shared-doc",
        }


# ---------------------------------------------------------------------------
# 6. Pack selection: auto_pack and include_unpackaged
# ---------------------------------------------------------------------------


class TestPackSelectionScoping:
    def _registry_dir(self, tmp_path, pack_id: str) -> str:
        """A manifest on disk for a pack the caller does NOT own.

        ``load_pack_registry`` scans the data directory with no notion of
        ownership, which is why auto_pack has to intersect before scoring.
        """
        d = tmp_path / "packs" / pack_id / "stage"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "pack_id": pack_id,
                    "title": "quantum widget research",
                    "description": "quantum widget research",
                    "version": "1",
                }
            ),
            encoding="utf-8",
        )
        return str(tmp_path)

    def test_auto_pack_will_not_select_a_pack_outside_the_scope(self, tmp_path):
        from opencrab.services.pack_selection import resolve_packs

        data_dir = self._registry_dir(tmp_path, PACK_B)
        sel = resolve_packs(
            "quantum widget research",
            None,
            True,
            False,
            data_dir,
            scope=frozenset({PACK_A}),
            raise_on_error=False,
        )
        assert sel.selected_packs == []
        assert sel.effective_pack_ids == [PACK_A]

    def test_requested_out_of_scope_pack_does_not_fall_back_to_auto_pack(self, tmp_path):
        """Naming an unreadable pack must not silently answer from another one."""
        from opencrab.services.pack_selection import PACK_IDS_OUT_OF_SCOPE, resolve_packs

        data_dir = self._registry_dir(tmp_path, PACK_A)
        sel = resolve_packs(
            "quantum widget research",
            [PACK_B],
            True,
            False,
            data_dir,
            scope=frozenset({PACK_A}),
            raise_on_error=False,
        )
        assert sel.effective_pack_ids == []
        assert sel.selected_packs == []
        assert PACK_IDS_OUT_OF_SCOPE in [w.code for w in sel.warnings]

    def test_out_of_scope_and_nonexistent_produce_the_same_warning(self, tmp_path):
        from opencrab.services.pack_selection import resolve_packs

        kw = {"scope": frozenset({PACK_A}), "raise_on_error": False}
        foreign = resolve_packs("q", [PACK_B], False, False, str(tmp_path), **kw)
        absent = resolve_packs("q", ["never-created"], False, False, str(tmp_path), **kw)
        assert [w.code for w in foreign.warnings] == [w.code for w in absent.warnings]
        assert foreign.effective_pack_ids == absent.effective_pack_ids == []

    def test_include_unpackaged_is_not_honoured(self, tmp_path):
        from opencrab.services.pack_selection import INCLUDE_UNPACKAGED_NOOP, resolve_packs

        sel = resolve_packs(
            "q",
            [PACK_A],
            False,
            True,
            str(tmp_path),
            scope=frozenset({PACK_A}),
            raise_on_error=False,
        )
        assert sel.include_unpackaged_effective is False
        assert INCLUDE_UNPACKAGED_NOOP in [w.code for w in sel.warnings]

    def test_every_warning_code_is_renderable_by_both_interfaces(self):
        """A missing map entry turns a warning into a KeyError mid-request."""
        from opencrab.services import pack_selection as ps

        codes = [
            ps.PACK_IDS_OVERRIDE_AUTO,
            ps.AUTO_PACK_BELOW_THRESHOLD,
            ps.INCLUDE_UNPACKAGED_NOOP,
            ps.PACK_IDS_OUT_OF_SCOPE,
            ps.AUTO_PACK_FAILED,
        ]
        for code in codes:
            w = ps.PackWarning(code, "detail")
            assert ps.mcp_warning_text(w)
            assert ps.cli_warning_text(w)

    def test_no_warning_text_still_promises_a_full_store_search(self):
        """There is no unfiltered search left to fall back to."""
        from opencrab.services import pack_selection as ps

        for text in list(ps._MCP_WARNINGS.values()) + list(ps._CLI_WARNINGS.values()):
            assert "full-store" not in text

    def test_client_facing_include_unpackaged_descriptions_say_it_is_ignored(self):
        """The warning wording was fixed; the parameter descriptions must be too.

        A caller reads the parameter description, not the warning map. Three
        interfaces expose this flag and all three used to promise it would
        surface unpackaged rows -- a response that describes a filter the
        server never applied.
        """
        from opencrab.mcp.tools import TOOL_SCHEMAS

        mcp_desc = TOOL_SCHEMAS["ontology_query"]["inputSchema"]["properties"][
            "include_unpackaged"
        ]["description"]
        assert "IGNORED" in mcp_desc

        from apps.api.main import QueryRequest

        rest_desc = QueryRequest.model_fields["include_unpackaged"].description
        assert "IGNORED" in rest_desc

        from opencrab.cli import query as cli_query

        cli_help = next(
            p.help for p in cli_query.params if p.name == "include_unpackaged"
        )
        assert "Ignored" in cli_help


# ---------------------------------------------------------------------------
# 7. Startup reconciliation
# ---------------------------------------------------------------------------


class TestStartupCheck:
    def test_refuses_when_the_graph_holds_an_unregistered_pack_id(
        self, sql, graph, docs, seeded
    ):
        _node(graph, docs, "ghost-pack", "ghost-node")
        with pytest.raises(RegistryGraphMismatchError) as exc:
            assert_registry_covers_graph(sql, graph)
        assert "ghost-pack" in str(exc.value)
        assert "migrate_pack_ownership" in str(exc.value)

    def test_passes_when_the_registry_covers_every_graph_pack_id(self, sql, graph, seeded):
        assert_registry_covers_graph(sql, graph) is None

    def test_pack_less_rows_do_not_refuse_startup(self, sql, graph, docs, seeded):
        """The brick regression.

        Rows with no pack_id at all are invisible, but refusing on them
        cannot be repaired on pg/kuzu/docker (the migration's graph backfill
        is local-only) and is reachable through ordinary use until #148
        stamps every writer. Two earlier revisions of this check refused
        here and would have bricked those deployments on first boot.
        """
        graph.upsert_node("Document", "orphan", {"node_id": "orphan"}, space_id="resource")
        assert_registry_covers_graph(sql, graph) is None

    def test_skips_when_the_graph_store_is_unavailable(self, sql):
        assert_registry_covers_graph(sql, MagicMock(available=False)) is None
