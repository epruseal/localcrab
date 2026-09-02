"""
ReBAC (Relationship-Based Access Control) Engine.

Determines whether a subject has a given permission over a resource
by traversing the Neo4j graph along MetaOntology subject→resource edges
and consulting stored policy rows in PostgreSQL.

Decision logic (in order of priority):
  1. Explicit DENY policy in SQL → deny.
  2. Explicit GRANT policy in SQL → grant.
  3. Direct graph edge from subject to resource matching the permission → grant.
  4. Transitive membership path (subject ∈ team/org that has permission) → grant.
  5. Default → deny.

Failure contract (#78): ``check()`` is a fail-closed boundary. If the SQL
policy lookup raises, or returns a value outside ``bool | None``, the
decision is DENY and the graph is not consulted, because an explicit DENY
row that could not be read must not be overridden by a graph GRANT. The
WARNING names the exception type and the three identifiers only; the full
traceback is logged at DEBUG.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from opencrab.grammar.validator import validate_rebac_permission
from opencrab.stores.neo4j_store import Neo4jStore
from opencrab.stores.sql_store import SQLStore

logger = logging.getLogger(__name__)

# Relations in the subject→resource space that map to permissions
# Reasons for the fail-closed SQL branches of ``ReBACEngine.check`` (#78).
# Tests assert these strings in full, so callers can tell a store failure
# from the ordinary "no policy, no edge" default deny.
_SQL_LOOKUP_FAILED_REASON = (
    "SQL policy lookup failed; default deny applied (fail-closed)."
)
_SQL_NON_BOOLEAN_REASON = (
    "SQL policy lookup returned a non-boolean value; "
    "default deny applied (fail-closed)."
)

_PERMISSION_RELATIONS: dict[str, list[str]] = {
    "view": ["can_view", "can_edit", "can_approve", "owns", "manages"],
    "edit": ["can_edit", "can_approve", "owns", "manages"],
    "execute": ["can_execute", "owns", "manages"],
    "simulate": ["can_execute", "can_edit", "owns", "manages"],
    "approve": ["can_approve", "owns"],
    "admin": ["owns"],
}


@dataclass
class AccessDecision:
    """Result of a ReBAC access check."""

    granted: bool
    reason: str
    subject_id: str
    permission: str
    resource_id: str
    path: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "reason": self.reason,
            "subject_id": self.subject_id,
            "permission": self.permission,
            "resource_id": self.resource_id,
            "path": self.path,
        }


class ReBACEngine:
    """Relationship-based access control engine."""

    def __init__(self, neo4j: Neo4jStore, sql: SQLStore) -> None:
        self._neo4j = neo4j
        self._sql = sql

    def check(
        self,
        subject_id: str,
        permission: str,
        resource_id: str,
    ) -> AccessDecision:
        """
        Determine whether *subject_id* has *permission* over *resource_id*.

        Parameters
        ----------
        subject_id:
            ID of the subject (User, Team, Org, or Agent node).
        permission:
            One of the REBAC_PERMISSIONS (view, edit, execute, simulate, approve, admin).
        resource_id:
            ID of the resource being accessed.

        Returns
        -------
        AccessDecision
            ``granted=False`` with ``_SQL_LOOKUP_FAILED_REASON`` if the SQL
            store raised, or with ``_SQL_NON_BOOLEAN_REASON`` if it returned
            a value outside ``bool | None``. Neither case consults the graph.
            This method does not raise for store failures.
        """
        # Validate permission label
        perm_result = validate_rebac_permission(permission)
        if not perm_result.valid:
            return AccessDecision(
                granted=False,
                reason=perm_result.error or "Invalid permission",
                subject_id=subject_id,
                permission=permission,
                resource_id=resource_id,
            )

        # 1. Check explicit SQL policy (DENY wins). The availability probe
        # and the lookup share one guard: any failure here is DENY without a
        # graph fall-through, because a DENY row we could not read must not
        # be overridden by a graph GRANT (#78). The graph path can only
        # grant, so its own errors stay in _graph_check.
        try:
            sql_available = self._sql.available
            stored = (
                self._sql.check_policy(subject_id, permission, resource_id)
                if sql_available
                else None
            )
        except Exception as exc:
            logger.warning(
                "ReBAC SQL policy lookup failed (%s) for subject=%s "
                "permission=%s resource=%s; treating as DENY (#78)",
                type(exc).__name__,
                subject_id,
                permission,
                resource_id,
            )
            # The exception text can carry a DSN or a server message, so it
            # is kept out of the WARNING and only reachable at DEBUG.
            logger.debug("ReBAC SQL policy lookup traceback", exc_info=exc)
            return AccessDecision(
                granted=False,
                reason=_SQL_LOOKUP_FAILED_REASON,
                subject_id=subject_id,
                permission=permission,
                resource_id=resource_id,
            )

        if stored is True:
            return AccessDecision(
                granted=True,
                reason="Explicit GRANT policy in rebac_policies table.",
                subject_id=subject_id,
                permission=permission,
                resource_id=resource_id,
            )
        if stored is False:
            return AccessDecision(
                granted=False,
                reason="Explicit DENY policy in rebac_policies table.",
                subject_id=subject_id,
                permission=permission,
                resource_id=resource_id,
            )
        if stored is not None:
            # Contract violation (SQLStore returns bool | None since #152).
            # "No policy" would let the graph grant, so this is DENY.
            logger.warning(
                "ReBAC SQL policy lookup returned a non-boolean (%s) for "
                "subject=%s permission=%s resource=%s; treating as DENY (#78)",
                type(stored).__name__,
                subject_id,
                permission,
                resource_id,
            )
            return AccessDecision(
                granted=False,
                reason=_SQL_NON_BOOLEAN_REASON,
                subject_id=subject_id,
                permission=permission,
                resource_id=resource_id,
            )

        # 2. Graph traversal check
        if self._neo4j.available:
            decision = self._graph_check(subject_id, permission, resource_id)
            if decision is not None:
                return decision

        # 3. Default deny
        return AccessDecision(
            granted=False,
            reason=(
                "No matching policy or graph relationship found. "
                "Default deny applied."
            ),
            subject_id=subject_id,
            permission=permission,
            resource_id=resource_id,
        )

    def _graph_check(
        self, subject_id: str, permission: str, resource_id: str
    ) -> AccessDecision | None:
        """
        Traverse the graph to find a permission-granting path.

        Returns an AccessDecision if a path is found, else None.
        """
        valid_relations = _PERMISSION_RELATIONS.get(permission, [])
        if not valid_relations:
            return None

        # Direct check — find_neighbors() is implemented by all store backends
        try:
            neighbors = self._neo4j.find_neighbors(
                subject_id, direction="out", depth=1, limit=200
            )
            for nb in neighbors:
                rel_type = nb.get("relation_type", "")
                nb_id = nb.get("properties", {}).get("id")
                if rel_type in valid_relations and nb_id == resource_id:
                    return AccessDecision(
                        granted=True,
                        reason=f"Direct graph relationship [{rel_type}] found.",
                        subject_id=subject_id,
                        permission=permission,
                        resource_id=resource_id,
                        path=[subject_id, rel_type, resource_id],
                    )
        except Exception as exc:
            logger.debug("ReBAC direct graph check error: %s", exc)

        # Transitive check: subject → (member_of|manages) → group → permission → resource
        try:
            group_neighbors = self._neo4j.find_neighbors(
                subject_id, direction="out", depth=1, limit=100
            )
            for gnb in group_neighbors:
                if gnb.get("relation_type") not in ("member_of", "manages"):
                    continue
                group_id = gnb.get("properties", {}).get("id")
                if not group_id:
                    continue
                resource_neighbors = self._neo4j.find_neighbors(
                    group_id, direction="out", depth=1, limit=100
                )
                for rnb in resource_neighbors:
                    rel_type = rnb.get("relation_type", "")
                    rnb_id = rnb.get("properties", {}).get("id")
                    if rel_type in valid_relations and rnb_id == resource_id:
                        return AccessDecision(
                            granted=True,
                            reason=(
                                f"Transitive access via group '{group_id}' "
                                f"with relation [{rel_type}]."
                            ),
                            subject_id=subject_id,
                            permission=permission,
                            resource_id=resource_id,
                            path=[subject_id, "member_of", str(group_id), rel_type, resource_id],
                        )
        except Exception as exc:
            logger.debug("ReBAC transitive graph check error: %s", exc)

        return None

    def grant(
        self,
        subject_id: str,
        permission: str,
        resource_id: str,
    ) -> None:
        """Explicitly grant a permission in the SQL policy table."""
        validate_rebac_permission(permission).raise_if_invalid()
        if not self._sql.available:
            raise RuntimeError("SQL store not available for policy storage.")
        self._sql.set_policy(subject_id, permission, resource_id, granted=True)
        logger.info("GRANT %s -> %s -> %s", subject_id, permission, resource_id)

    def deny(
        self,
        subject_id: str,
        permission: str,
        resource_id: str,
    ) -> None:
        """Explicitly deny a permission in the SQL policy table."""
        validate_rebac_permission(permission).raise_if_invalid()
        if not self._sql.available:
            raise RuntimeError("SQL store not available for policy storage.")
        self._sql.set_policy(subject_id, permission, resource_id, granted=False)
        logger.info("DENY %s -> %s -> %s", subject_id, permission, resource_id)

    def list_subject_policies(self, subject_id: str) -> list[dict[str, Any]]:
        """Return all stored policies for a subject."""
        if not self._sql.available:
            return []
        return self._sql.list_policies(subject_id)
