"""
ImpactEngine 그래프 연동 경로 테스트.

opencrab/ontology/impact.py의 analyse()/lever_simulate()는 그래프 스토어의
available=True 분기(이웃 탐색, 교차 space 승격, find_by_relations 기반
시뮬레이션)와 SQL persist 실패 시 조용히 degrade하는 계약을 갖는다.
FakeGraphStore/FakeSQLStore로 그 두 계약을 직접 검증한다.
"""
from __future__ import annotations

from opencrab.ontology.impact import ImpactEngine

# #147: pack_ids is now a required keyword on analyse()/lever_simulate() and
# is threaded straight through to get_node_by_id_scoped()/find_neighbors().
# FakeGraphStore doesn't implement scope filtering itself (it's a plain
# id->value stub), so the exact contents don't matter to these tests -- only
# that a scope is supplied, matching the new call shape.
PACK_IDS = ["p1"]


class FakeGraphStore:
    """impact.py가 소비하는 정확한 3개 메서드 형태만 흉내낸다."""

    def __init__(self, available=True, node=None, neighbors=None, relations=None):
        self.available = available
        self._node = node
        self._neighbors = neighbors if neighbors is not None else []
        # relations: dict keyed by tuple(relations-list) -> list to return
        self._relations = relations or {}

        self._raise_on_get_node = False
        self._raise_on_find_neighbors = False
        self._raise_on_find_by_relations = False

    def get_node_by_id_scoped(self, node_id, pack_ids):
        if self._raise_on_get_node:
            raise RuntimeError("node lookup boom")
        return self._node

    def find_neighbors(
        self,
        node_id,
        direction="both",
        depth=1,
        limit=50,
        pack_ids=None,
        include_unpackaged=False,
    ):
        if self._raise_on_find_neighbors:
            raise RuntimeError("neighbor traversal boom")
        return self._neighbors

    def find_by_relations(self, node_id, relations, direction="out", limit=20):
        if self._raise_on_find_by_relations:
            raise RuntimeError("relation query boom")
        return self._relations.get(tuple(relations), [])

    def find_by_relations_scoped(
        self, node_id, relations, pack_ids, direction="out", limit=20
    ):
        """#147: what `lever_simulate` calls now.

        The real implementations constrain anchor, other endpoint AND the
        edge itself in the query, before LIMIT -- the edge half in
        particular cannot be reproduced by a caller, because
        `find_by_relations` never returns edge properties. This double
        applies the endpoint half to its canned rows so that a fixture with
        out-of-scope rows still exercises the scoped path. The edge half is
        each store's own; it is pinned per backend (SQL by the isolation
        suite, Kuzu by tests/test_kuzu_graph_store.py, Neo4j by the
        mock-session tests in tests/test_neo4j_helpers.py) rather than
        here.
        """
        if self._raise_on_find_by_relations:
            raise RuntimeError("relation query boom")
        if not pack_ids or not relations:
            return []
        allowed = set(pack_ids)
        return [
            r
            for r in self._relations.get(tuple(relations), [])
            if (r.get("properties") or {}).get("pack_id") in allowed
        ][:limit]


class FakeSQLStore:
    def __init__(self, available=True, raise_on_save_impact=False, raise_on_save_simulation=False):
        self.available = available
        self._raise_impact = raise_on_save_impact
        self._raise_sim = raise_on_save_simulation
        self.saved_impacts = []
        self.saved_simulations = []

    def save_impact(self, node_id, change_type, result_dict):
        if self._raise_impact:
            raise RuntimeError("sql impact persist boom")
        self.saved_impacts.append((node_id, change_type, result_dict))

    def save_simulation(self, lever_id, direction, magnitude, sim_result):
        if self._raise_sim:
            raise RuntimeError("sql simulation persist boom")
        self.saved_simulations.append((lever_id, direction, magnitude, sim_result))


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestAnalyseNormal:
    def test_cross_space_neighbors_escalate_i3_and_i7(self):
        # node has no "space" property -> exercises the space_for_node_type()
        # fallback (line ~131) as well as the cross-space escalation.
        node = {"id": "pol-1", "node_type": "Policy"}
        neighbors = [
            {
                "properties": {"id": "pol-2", "space": "policy"},
                "labels": ["Policy"],
                "relation_type": "affects",
            },
            {
                # no "space" property -> must be inferred from label "Outcome"
                "properties": {"id": "out-1"},
                "labels": ["Outcome"],
                "relation_type": "raises",
            },
        ]
        graph = FakeGraphStore(available=True, node=node, neighbors=neighbors)
        sql = FakeSQLStore(available=True)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.analyse("pol-1", change_type="update", depth=2, pack_ids=PACK_IDS)

        assert result.space == "policy"
        assert result.node_type == "Policy"
        assert result.affected_spaces == ["outcome", "policy"]

        triggered_ids = {t["id"] for t in result.triggered}
        # Policy baseline (I4,I5) + update change (I1,I5,I6) + always I1
        # + cross-space escalation (I3) + outcome-space escalation (I7).
        assert triggered_ids == {"I1", "I3", "I4", "I5", "I6", "I7"}

        assert sql.saved_impacts == [("pol-1", "update", result.to_dict())]

    def test_lever_simulate_builds_outcomes_and_concepts_with_predicted_delta(self):
        # #147: lever_simulate gates on get_node_by_id_scoped(lever_id, ...)
        # and then reads relations through find_by_relations_scoped, which
        # constrains anchor/endpoint/edge in the store. So both the anchor
        # and each related node need a pack_id inside PACK_IDS for this
        # "found and in-scope" test to reach the outcome/concept building
        # code rather than the out-of-scope short-circuit.
        outcomes_raw = [
            {"properties": {"id": "out-1", "pack_id": "p1"}, "labels": ["Outcome"], "relation_type": "raises"},
            {"properties": {"id": "out-2", "pack_id": "p1"}, "labels": ["KPI"], "relation_type": "lowers"},
        ]
        concepts_raw = [
            {"properties": {"id": "c-1", "pack_id": "p1"}, "labels": ["Concept"], "relation_type": "affects"},
        ]
        graph = FakeGraphStore(
            available=True,
            node={"id": "lev-1", "node_type": "Lever", "pack_id": "p1"},
            relations={
                ("raises", "lowers", "stabilizes", "optimizes"): outcomes_raw,
                ("affects",): concepts_raw,
            },
        )
        sql = FakeSQLStore(available=True)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.lever_simulate("lev-1", direction="raises", magnitude=0.5, pack_ids=PACK_IDS)

        assert result["predicted_outcome_changes"] == [
            {"node_id": "out-1", "node_type": "Outcome", "relation": "raises", "predicted_delta": 0.5},
            {"node_id": "out-2", "node_type": "KPI", "relation": "lowers", "predicted_delta": -0.5},
        ]
        assert result["affected_concepts"] == [{"node_id": "c-1", "node_type": "Concept"}]
        assert sql.saved_simulations == [("lev-1", "raises", 0.5, result)]


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestAnalyseAndSimulateErrors:
    def test_analyse_degrades_when_save_impact_raises(self):
        graph = FakeGraphStore(available=False)
        sql = FakeSQLStore(available=True, raise_on_save_impact=True)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.analyse("n-1", change_type="update", pack_ids=PACK_IDS)

        assert result.node_id == "n-1"
        assert sql.saved_impacts == []  # raised before append -> nothing persisted

    def test_lever_simulate_degrades_when_save_simulation_raises(self):
        graph = FakeGraphStore(available=False)
        sql = FakeSQLStore(available=True, raise_on_save_simulation=True)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.lever_simulate("lev-1", direction="lowers", magnitude=0.3, pack_ids=PACK_IDS)

        assert result["lever_id"] == "lev-1"
        assert sql.saved_simulations == []

    # invalid-direction ValueError already covered by
    # tests/test_mcp.py::TestImpactEngine::test_lever_simulate_invalid_direction
    # — not duplicated here.

    def test_analyse_degrades_when_get_node_by_id_raises(self):
        graph = FakeGraphStore(available=True)
        graph._raise_on_get_node = True
        sql = FakeSQLStore(available=False)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.analyse("n-1", pack_ids=PACK_IDS)

        assert result.space is None
        assert result.node_type is None

    def test_analyse_degrades_when_find_neighbors_raises(self):
        graph = FakeGraphStore(available=True, node={"id": "n-1", "node_type": "Lever"})
        graph._raise_on_find_neighbors = True
        sql = FakeSQLStore(available=False)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.analyse("n-1", pack_ids=PACK_IDS)

        assert result.space == "lever"
        assert result.affected_nodes == []
        assert result.affected_spaces == []

    def test_lever_simulate_degrades_when_find_by_relations_raises(self):
        # #147: the anchor must resolve (in-scope) first, otherwise the new
        # _LeverOutOfScopeError short-circuit -- not find_by_relations raising
        # -- would be what actually produces the empty lists below.
        graph = FakeGraphStore(available=True, node={"id": "lev-1", "node_type": "Lever"})
        graph._raise_on_find_by_relations = True
        sql = FakeSQLStore(available=False)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.lever_simulate("lev-1", direction="raises", magnitude=0.5, pack_ids=PACK_IDS)

        assert result["predicted_outcome_changes"] == []
        assert result["affected_concepts"] == []


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestAnalyseAndSimulateEdges:
    def test_analyse_node_not_found_and_zero_neighbors(self):
        graph = FakeGraphStore(available=True, node=None, neighbors=[])
        sql = FakeSQLStore(available=False)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        # unmapped change_type exercises the ["I1", "I2"] default (line ~144)
        result = engine.analyse("missing-node", change_type="unmapped_change", pack_ids=PACK_IDS)

        assert result.space is None
        assert result.node_type is None
        assert result.affected_nodes == []
        assert result.affected_spaces == []
        assert {t["id"] for t in result.triggered} == {"I1", "I2"}

    def test_lever_simulate_no_matching_relations(self):
        # #147: the anchor must resolve (in-scope) so this exercises "no
        # matching relations" rather than the anchor-missing short-circuit.
        graph = FakeGraphStore(
            available=True, node={"id": "lev-1", "node_type": "Lever"}, relations={}
        )
        sql = FakeSQLStore(available=True)
        engine = ImpactEngine(neo4j=graph, sql=sql)

        result = engine.lever_simulate("lev-1", direction="optimizes", magnitude=0.2, pack_ids=PACK_IDS)

        assert result["predicted_outcome_changes"] == []
        assert result["affected_concepts"] == []
        assert sql.saved_simulations == [("lev-1", "optimizes", 0.2, result)]
