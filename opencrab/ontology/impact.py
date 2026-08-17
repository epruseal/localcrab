"""
Impact Analysis Engine (I1–I7).

Given a node ID and a change type, this engine analyses which impact
categories are triggered by traversing the MetaOntology graph and
applying heuristic rules based on the node's space and relationships.

Impact categories:
  I1 — Data impact
  I2 — Relation impact
  I3 — Space impact
  I4 — Permission impact
  I5 — Logic impact
  I6 — Cache/index impact
  I7 — Downstream system impact
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from opencrab.grammar.manifest import IMPACT_CATEGORIES, space_for_node_type
from opencrab.stores.neo4j_store import Neo4jStore
from opencrab.stores.sql_store import SQLStore

logger = logging.getLogger(__name__)


@dataclass
class ImpactResult:
    """Structured result of an impact analysis."""

    node_id: str
    change_type: str
    space: str | None
    node_type: str | None
    triggered: list[dict[str, Any]] = field(default_factory=list)
    affected_nodes: list[dict[str, Any]] = field(default_factory=list)
    affected_spaces: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "change_type": self.change_type,
            "space": self.space,
            "node_type": self.node_type,
            "triggered_impacts": self.triggered,
            "affected_nodes": self.affected_nodes,
            "affected_spaces": self.affected_spaces,
            "summary": self.summary,
        }


# Map: space → set of impact category IDs always triggered
_SPACE_BASELINE_IMPACTS: dict[str, list[str]] = {
    "subject": ["I1", "I4"],          # subjects affect data and permissions
    "resource": ["I1", "I6"],         # resources affect data and caches
    "evidence": ["I1", "I2", "I5"],   # evidence affects data, relations, logic
    "concept": ["I2", "I5", "I6"],    # concepts affect relations and logic
    "claim": ["I2", "I5"],            # claims affect relations and logic
    "community": ["I2", "I6"],        # communities affect relations and caches
    "outcome": ["I1", "I5", "I7"],    # outcomes affect data, logic, and downstream
    "lever": ["I1", "I5", "I7"],      # levers affect all downstream systems
    "policy": ["I4", "I5"],           # policies affect permissions and logic
}

# Map: change_type → additional impact IDs
_CHANGE_TYPE_IMPACTS: dict[str, list[str]] = {
    "create": ["I2", "I6"],
    "update": ["I1", "I5", "I6"],
    "delete": ["I1", "I2", "I3", "I5", "I6"],
    "permission_change": ["I4", "I5"],
    "relationship_add": ["I2", "I5"],
    "relationship_remove": ["I2", "I3", "I5"],
    "bulk_import": ["I1", "I2", "I3", "I6", "I7"],
}


class _LeverOutOfScopeError(Exception):
    """Internal control-flow signal: the lever is outside the read scope.

    Raised and caught inside ``lever_simulate`` only. Its whole job is to
    reach the same empty-result state an unknown lever_id reaches, without a
    parallel branch that could later be given different behaviour.
    """


class ImpactEngine:
    """Analyses the blast radius of ontology changes."""

    def __init__(self, neo4j: Neo4jStore, sql: SQLStore) -> None:
        self._neo4j = neo4j
        self._sql = sql

    def analyse(
        self,
        node_id: str,
        change_type: str = "update",
        depth: int = 2,
        *,
        pack_ids: list[str],
    ) -> ImpactResult:
        """
        Compute the impact of a change to *node_id*.

        Parameters
        ----------
        node_id:
            The ID of the node being changed.
        change_type:
            Nature of the change (create, update, delete, permission_change, etc.).
        depth:
            Graph traversal depth for finding affected neighbours.
        pack_ids:
            REQUIRED (#147). The caller's readable pack scope. The anchor is
            resolved WITHIN it and the traversal is bounded BY it.

            An anchor outside the scope produces a response byte-identical
            to one for a node that does not exist (#143 invariant 7). That
            falls out of the two lookups rather than from a special branch:
            ``get_node_by_id_scoped`` returns None for both, and
            ``find_neighbors`` returns [] for both (its anchor must pass the
            same pack filter). A dedicated "not permitted" branch would be a
            second code path free to drift from the "not found" one -- and
            drift here is exactly how existence leaks back in.

        Returns
        -------
        ImpactResult
        """
        result = ImpactResult(
            node_id=node_id,
            change_type=change_type,
            space=None,
            node_type=None,
        )

        # --- Discover node space and type ---
        # All four backends implement get_node_by_id() natively (Neo4j's
        # Cypher is `MATCH (n {id:$id}) RETURN properties(n), labels(n)[0]`,
        # merging "space" from the node's own properties — identical to the
        # `n.space` this call site used to read via a hand-rolled Cypher
        # fallback). See opencrab/stores/_graph_protocol.py.
        if self._neo4j.available:
            try:
                # #147: the scoped lookup, not get_node_by_id + a Python
                # check afterwards. get_node_by_id matches on node_id alone
                # (the PK is (node_type, node_id)), so a homonym in another
                # pack can be the row it returns -- filtering after the fact
                # would then answer "no such node" for a node the caller
                # really does have. The pack predicate has to run before the
                # LIMIT, not after it.
                node = self._neo4j.get_node_by_id_scoped(node_id, pack_ids)
                if node:
                    result.node_type = node.get("node_type")
                    result.space = (
                        node.get("space")
                        or space_for_node_type(result.node_type or "")
                    )
            except Exception as exc:
                logger.debug("Impact engine: node lookup error: %s", exc)

        # --- Triggered impact categories ---
        triggered_ids: set[str] = set()

        # Baseline from space
        space_impacts = _SPACE_BASELINE_IMPACTS.get(result.space or "", [])
        triggered_ids.update(space_impacts)

        # Additional from change type
        change_impacts = _CHANGE_TYPE_IMPACTS.get(change_type, ["I1", "I2"])
        triggered_ids.update(change_impacts)

        # Always trigger I1 (data) for any change
        triggered_ids.add("I1")

        result.triggered = [
            cat for cat in IMPACT_CATEGORIES if cat["id"] in triggered_ids
        ]

        # --- Affected neighbouring nodes ---
        if self._neo4j.available:
            try:
                neighbours = self._neo4j.find_neighbors(
                    node_id=node_id,
                    direction="both",
                    depth=depth,
                    limit=50,
                    pack_ids=pack_ids,
                    include_unpackaged=False,
                )
                result.affected_nodes = neighbours[:20]  # cap output size

                # Collect affected spaces
                affected_spaces: set[str] = set()
                if result.space:
                    affected_spaces.add(result.space)
                for n in neighbours:
                    ns = n.get("properties", {}).get("space")
                    if ns:
                        affected_spaces.add(ns)
                    # Infer from label
                    for lbl in n.get("labels", []):
                        inferred = space_for_node_type(lbl)
                        if inferred:
                            affected_spaces.add(inferred)
                result.affected_spaces = sorted(affected_spaces)

                # If cross-space effects exist, trigger I3 and I7
                if len(result.affected_spaces) > 1:
                    if not any(t["id"] == "I3" for t in result.triggered):
                        i3 = next((c for c in IMPACT_CATEGORIES if c["id"] == "I3"), None)
                        if i3:
                            result.triggered.append(i3)
                    # Downstream systems if lever or outcome space involved
                    if any(s in result.affected_spaces for s in ("lever", "outcome")):
                        if not any(t["id"] == "I7" for t in result.triggered):
                            i7 = next((c for c in IMPACT_CATEGORIES if c["id"] == "I7"), None)
                            if i7:
                                result.triggered.append(i7)
            except Exception as exc:
                logger.debug("Impact engine: neighbor traversal error: %s", exc)

        # --- Build summary ---
        cat_names = [t["name"] for t in result.triggered]
        result.summary = (
            f"Change '{change_type}' on node '{node_id}' "
            f"(space={result.space or 'unknown'}) triggers: {', '.join(cat_names)}. "
            f"Affected spaces: {', '.join(result.affected_spaces) or 'none detected'}."
        )

        # --- Persist to SQL ---
        if self._sql.available:
            try:
                self._sql.save_impact(node_id, change_type, result.to_dict())
            except Exception as exc:
                logger.debug("Impact engine: SQL persist error: %s", exc)

        return result

    def lever_simulate(
        self,
        lever_id: str,
        direction: str,
        magnitude: float,
        *,
        pack_ids: list[str],
    ) -> dict[str, Any]:
        """
        Simulate the downstream effects of moving a lever.

        Parameters
        ----------
        lever_id:
            ID of the Lever node.
        direction:
            "raises", "lowers", "stabilizes", or "optimizes".
        magnitude:
            Numeric strength of the lever action (0.0–1.0 scale recommended).
        pack_ids:
            REQUIRED (#147). The caller's readable pack scope. A lever
            outside it is treated as absent, producing the same empty
            outcome/concept lists an unknown lever_id produces.

            ``find_by_relations`` has no pack parameter and matches on
            node_id alone, so its results are filtered here in Python. That
            is applied AFTER the store's LIMIT, which costs recall (a lever
            with more than 20 out-edges may lose readable ones to
            unreadable ones ahead of them) but cannot leak: every surviving
            row passed the scope check. #143 classifies this
            limit-then-filter shape as a recall defect, not an
            authorization one.
        """
        valid_directions = {"raises", "lowers", "stabilizes", "optimizes"}
        if direction not in valid_directions:
            raise ValueError(
                f"Invalid direction '{direction}'. "
                f"Valid directions: {', '.join(sorted(valid_directions))}."
            )

        outcomes: list[dict[str, Any]] = []
        concepts: list[dict[str, Any]] = []

        # All four backends implement find_by_relations() natively (Neo4j's
        # Cypher generalises the old hand-rolled
        # `MATCH (l)-[r:raises|lowers|stabilizes|optimizes]->(o)` /
        # `MATCH (l)-[:affects]->(c)` patterns this call site used to run via
        # run_cypher() — same relation-type filter, same unfiltered
        # `labels(m)` list, so indexing `[0]` below matches the old
        # `labels(o)[0]` value exactly). See opencrab/stores/_graph_protocol.py.
        if self._neo4j.available:
            try:
                from opencrab.ontology.pack_provenance import in_pack_scope

                pack_set = set(pack_ids)
                # #147: gate on the anchor first. An unreadable lever must
                # look exactly like an absent one, and an absent one reaches
                # the same empty lists by returning no relations.
                anchor = self._neo4j.get_node_by_id_scoped(lever_id, pack_ids)
                if anchor is None:
                    raise _LeverOutOfScopeError

                # Outcome 탐색: lever → (raises|lowers|stabilizes|optimizes) → outcome
                _lever_relations = ["raises", "lowers", "stabilizes", "optimizes"]
                for r in self._neo4j.find_by_relations(lever_id, _lever_relations, "out", 20):
                    props = r.get("properties") or {}
                    if not in_pack_scope({"properties": props}, pack_set):
                        continue
                    rel = r.get("relation_type", "")
                    outcomes.append({
                        "node_id": props.get("id", "?"),
                        "node_type": (r.get("labels") or ["Outcome"])[0],
                        "relation": rel,
                        "predicted_delta": _predict_delta(direction, rel, magnitude),
                    })

                # Concept 탐색: lever → affects → concept
                for r in self._neo4j.find_by_relations(lever_id, ["affects"], "out", 10):
                    props = r.get("properties") or {}
                    if not in_pack_scope({"properties": props}, pack_set):
                        continue
                    concepts.append({
                        "node_id": props.get("id", "?"),
                        "node_type": (r.get("labels") or ["Concept"])[0],
                    })
            except _LeverOutOfScopeError:
                # Not an error: the lever is not readable, so it has no
                # readable relations. Same state as an unknown lever_id.
                pass
            except Exception as exc:
                logger.debug("Lever simulation graph query error: %s", exc)

        sim_result: dict[str, Any] = {
            "lever_id": lever_id,
            "direction": direction,
            "magnitude": magnitude,
            "predicted_outcome_changes": outcomes,
            "affected_concepts": concepts,
            "impact_categories": ["I5", "I7"],
            "confidence": min(0.95, 0.5 + magnitude * 0.45),
            "note": (
                f"Lever '{lever_id}' moved in direction '{direction}' "
                f"with magnitude {magnitude:.2f}."
            ),
        }

        # Persist
        if self._sql.available:
            try:
                self._sql.save_simulation(lever_id, direction, magnitude, sim_result)
            except Exception as exc:
                logger.debug("Lever simulation SQL persist error: %s", exc)

        return sim_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _predict_delta(action_direction: str, edge_relation: str, magnitude: float) -> float:
    """
    Compute a predicted numeric delta for an outcome node.

    Positive delta = increase, negative = decrease.
    """
    direction_sign = {"raises": +1.0, "lowers": -1.0, "stabilizes": 0.0, "optimizes": +0.8}.get(
        action_direction, 0.0
    )
    edge_sign = {"raises": +1.0, "lowers": -1.0, "stabilizes": 0.0, "optimizes": +0.8}.get(
        edge_relation, +1.0
    )
    return round(direction_sign * edge_sign * magnitude, 4)
