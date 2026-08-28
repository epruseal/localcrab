"""
Ontology Builder.

High-level API for adding nodes and edges to the multi-store ontology.
Validates against the MetaOntology grammar before writing to any store.
Writes to the graph, document, SQL-registry and (optionally) vector stores in
a best-effort fan-out pattern — individual store failures are logged but do
not abort the operation. The ``stores`` map in the response keys results by
role (``graph``/``docs``/``sql``/``vector``), not by backend product, so the
status is meaningful regardless of the local/docker backend in use.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from opencrab.common.graph_identity import (
    EdgeIdentityConflict,
    GraphSchemaMigrationRequired,
    GraphWriteUnavailable,
    NodeIdentityConflict,
)
from opencrab.common.pack_tags import canonicalize_pack_alias
from opencrab.common.timefmt import now_iso
from opencrab.grammar.validator import validate_edge, validate_node, validate_node_properties
from opencrab.pack.write_gate import (
    EDGE_STAMPED,
    NODE_STAMPED,
    authorize,
    authorize_fork_copy,
    edge_identity_conflict,
    identity_reject_message,
    node_identity_conflict,
    resolved_endpoint_pack_conflict,
    stamp,
)
from opencrab.stores.mongo_store import MongoStore
from opencrab.stores.neo4j_store import Neo4jStore
from opencrab.stores.sql_store import SQLStore

logger = logging.getLogger(__name__)


class OntologyBuilder:
    """Coordinates multi-store writes for ontology nodes and edges."""

    def __init__(
        self,
        neo4j: Neo4jStore,
        mongo: MongoStore,
        sql: SQLStore,
        vec: Any = None,
    ) -> None:
        self._neo4j = neo4j
        self._mongo = mongo
        self._sql = sql
        self._vec = vec  # Optional ChromaStore — if provided, add_node embeds each node

    @staticmethod
    def _raise_graph_gate(exc: RuntimeError) -> None:
        if str(exc) in {
            "graph schema requires issue80 migration before writes",
            "graph schema is unavailable: migration required",
        }:
            raise exc

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def add_node(
        self,
        space: str,
        node_type: str,
        node_id: str,
        properties: dict[str, Any] | None = None,
        *,
        pack_id: str,
        origin: str = "client",
        pack_anchor: bool = False,
        fork_copy: bool = False,
        write_vector: bool = True,
        _allow_ready_anchor: bool = False,
        _expected_current_digest: str | None = None,
    ) -> dict[str, Any]:
        """
        Add or update a node in all stores.

        ``origin="server"`` is for replaying data this server previously wrote
        -- the pack loader restoring its own dump. A dump can carry an
        ``owner_id`` stamped by a past server (or by the same server before a
        re-init under a new user); treating that as a forged client value
        rejects the node and silently drops it from the reload. Server-origin
        overwrites it with the importing principal instead. Every
        client-reachable path keeps the default.

        ``pack_anchor`` (#170, design v4 §3.4) opts into writing ONE
        specific node: the pack's own graph anchor. When True, the call must
        ALSO be shaped exactly like that anchor write -- ``space ==
        "resource"``, ``node_type == "Dataset"``, and ``node_id ==
        ownership.anchor_node_id(pack_id)`` -- or it raises ``ValueError``
        before authorization even runs. This parameter opens nothing else:
        not a second node in the same ``creating`` pack, not any node in a
        ``partial`` pack (those never reach this branch's
        ``allowed_statuses``).

        Three call sites pass it, and they do not all write in the same
        lifecycle window:

        - ``pack_create`` (``opencrab/mcp/tools/pack.py``) -- ``creating``,
          the anchor write that completes its two-phase creation.
        - ``fork_pack`` (``opencrab/pack/fork.py``, #201) -- ``creating``
          too, but FIRST rather than last: a fork lands its anchor before
          copying content.
        - ``ensure_anchor`` (``opencrab/pack/lifecycle.py``, #194) --
          ``ready``, rebuilding an anchor an already-published pack lost.
          It is the only caller that sets ``_allow_ready_anchor``.

        Everything else -- including the pack loader and every ordinary
        ``ontology_add_node``/REST call -- leaves it at the default False.

        ``_allow_ready_anchor`` (#194) is internal, and it does NOT widen
        the gate: it swaps this branch's ``creating``-only status set for
        ``authorize``'s own default of ``ready``. It exists so that an
        anchor write says out loud which lifecycle window it belongs to,
        rather than reading as an ordinary write that happens to land on an
        anchor-shaped node. ``ensure_anchor`` gates on ``ready`` before it
        ever gets here, so no other status is reachable through it.

        ``fork_copy`` (design v7 §4-C-2) is the second, narrower, opt-in:
        when True, authorization goes through
        ``write_gate.authorize_fork_copy`` instead of ``authorize``, which
        widens the allowed status to the pack's own ``creating`` window
        (and only when that row was reserved BY a fork -- see that
        function's docstring). Everything else about the write --
        identity guard, stamping, grammar validation, the "graph
        unavailable => write nothing" contract -- is unchanged. The only
        intended caller is ``opencrab/pack/fork.py``.

        ``write_vector`` (design v7 §4-C-3), default True, is the third:
        when False the Chroma leg is skipped entirely and the receipt
        records ``"skipped (raw copy)"`` under ``stores["vector"]`` instead
        of attempting a write. It exists because this leg unconditionally
        calls ``upsert_texts`` -- which re-embeds -- and issue #200
        forbids re-embedding a fork; fork imports the original vectors raw
        instead and sets this to False only on its own node-copy call
        (never on the anchor write, which needs a fresh embedding for its
        own title/description).

        Parameters
        ----------
        space:
            MetaOntology space identifier (e.g. "subject", "resource").
        node_type:
            Node type label (e.g. "User", "Document").
        node_id:
            Stable unique identifier for the node.
        properties:
            Arbitrary key/value properties for the node.

        Returns
        -------
        dict with operation status and node data.

        Raises
        ------
        ValueError
            If the space/node_type combination is invalid, or (when
            ``pack_anchor=True``) the call does not match the pack's own
            anchor shape.
        """
        from opencrab.auth import current_principal
        from opencrab.pack.ownership import (
            PACK_STATUS_CREATING,
            PACK_STATUS_READY,
            anchor_node_id,
        )

        # The gate, in this order and no other (#148). Stamping must run
        # BEFORE canonicalize_pack_alias: that helper rewrites pack tags in
        # place, so a stamp running after it can no longer see what the caller
        # actually asked for and a forged pack_id would pass silently.
        # `stamp` returns a NEW dict -- unlike the pre-#148 code this no longer
        # mutates the caller's properties.
        principal = current_principal()
        if pack_anchor and fork_copy:
            # These two opt-ins are mutually exclusive, and the combination
            # only became expressible when #194 and #201 landed in the same
            # function -- nothing has ever exercised it. Refusing beats
            # picking a winner, because the dispatch below would let the
            # anchor branch shadow `fork_copy` silently, and the gate it
            # shadows (`authorize_fork_copy`) is the STRICTER of the two:
            # it additionally requires the row to carry `forked_from`. A
            # future caller adding the fork opt-in "for consistency" to
            # fork's own anchor write would drop that requirement with no
            # signal at all. The reverse pairing needs no guard: when
            # `pack_anchor` is False the fork branch wins, and it is the
            # narrower gate, so what gets dropped fails closed.
            #
            # Raised here, before authorization, for the same reason as the
            # shape check below: a request that does not make sense must not
            # learn the pack's authorization state first.
            raise ValueError(
                "pack_anchor and fork_copy are mutually exclusive: a fork's "
                "own anchor write goes through the anchor path (#170), its "
                "content copy through the fork path (#201)"
            )
        if pack_anchor:
            # Shape check BEFORE authorize (#170): a caller whose request
            # doesn't even look like an anchor write must not learn anything
            # about the pack's authorization state first. Checked against
            # this call's OWN pack_id only -- never another pack's -- so the
            # error message cannot leak anything about a different pack.
            expected_node_id = anchor_node_id(pack_id)
            mismatches = []
            if space != "resource":
                mismatches.append(f"space must be 'resource', got {space!r}")
            if node_type != "Dataset":
                mismatches.append(f"node_type must be 'Dataset', got {node_type!r}")
            if node_id != expected_node_id:
                mismatches.append("node_id does not match this pack's anchor id")
            if mismatches:
                raise ValueError(
                    "pack_anchor=True requires this pack's own anchor node "
                    "shape: " + "; ".join(mismatches)
                )
            if _allow_ready_anchor:
                # `ready` ONLY -- deliberately not `ready` plus `creating`.
                # #201 opened `creating` to fork's bulk copy but bolted a
                # `forked_from` requirement onto it precisely so that the
                # opening could not become a general "write into any
                # creating pack of mine" door. Letting this branch accept
                # `creating` would reopen exactly that door for the anchor
                # node, with no equivalent requirement, and nothing needs
                # it: `ensure_anchor` refuses every non-`ready` pack before
                # it reaches this call.
                authorize(
                    self._sql,
                    principal,
                    pack_id,
                    allowed_statuses=(PACK_STATUS_READY,),
                )
            else:
                authorize(self._sql, principal, pack_id, allowed_statuses=(PACK_STATUS_CREATING,))
        elif fork_copy:
            authorize_fork_copy(self._sql, principal, pack_id)
        else:
            authorize(self._sql, principal, pack_id)
        props = stamp(
            properties, principal=principal, pack_id=pack_id, keys=NODE_STAMPED,
            origin=origin,
        )

        # Ownership-tag invariant (#171): a row cannot carry `pack` and a truthy
        # `pack_id` with different values. This is the funnel every graph/doc/vector
        # node write reaches (MCP, REST, pack ingest, pack loader), so the check
        # belongs here rather than in each entry point.
        canonicalize_pack_alias(props)


        # Grammar validation FIRST -- before any probe. `node_type` is
        # caller-controlled on the REST /api/nodes and MCP ontology_add_node
        # paths, and Neo4jStore.get_node interpolates it directly into
        # `MATCH (n:{node_type} ...)`. Probing before validating let an
        # authenticated caller run mutating Cypher (e.g. a label of
        # `X) DETACH DELETE n //`) even though the request was rejected a few
        # lines later. Validation is pure and cheap; it belongs in front.
        result = validate_node(space, node_type)
        result.raise_if_invalid()

        # Schema property validation (raises ValueError on failure)
        prop_result = validate_node_properties(node_type, props)
        prop_result.raise_if_invalid()

        # Identity slot guard (#146, promoted to the gate in #148). Node
        # identity is not qualified by pack on any backend, so writing an id
        # that already lives in another pack takes that pack's slot. Checked
        # here rather than at each entry point because every one of them --
        # MCP, REST, CLI, the loader, crabharness -- lands on this call.
        reason = node_identity_conflict(
            self._neo4j, self._mongo, self._vec,
            space=space, node_type=node_type, node_id=node_id, pack_id=pack_id,
        )
        if reason:
            raise ValueError(identity_reject_message("node", node_id, reason))

        receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
        receipt_ts = now_iso()

        output: dict[str, Any] = {
            "node_id": node_id,
            "space": space,
            "node_type": node_type,
            "properties": props,
            "receipt_id": receipt_id,
            "receipt_ts": receipt_ts,
            "stores": {},
        }

        # Graph is the system of record. When it is down, refuse the whole
        # write instead of fanning out to the optional stores (#146 follow-up):
        # a doc/vector row with no graph node is invisible to every pack-scoped
        # read and has to be reconciled by hand later. Returned as a receipt
        # rather than raised so the #158 contract ("callers read the receipt")
        # keeps holding.
        if self._neo4j is None or not self._neo4j.available:
            output["stores"] = {
                "graph": "unavailable",
                "docs": "skipped (graph unavailable)",
                "sql": "skipped (graph unavailable)",
                "vector": "skipped (graph unavailable)",
            }
            return output

        # --- Neo4j write ---
        if self._neo4j is not None and self._neo4j.available:
            try:
                if _expected_current_digest is None:
                    node_props = self._neo4j.upsert_node(
                        node_type=node_type,
                        node_id=node_id,
                        properties=props,
                        space_id=space,
                    )
                else:
                    # A pack reload can legitimately reclassify a node owned
                    # by that same pack.  Global node identity makes
                    # delete-then-upsert unsafe, so use the graph's CAS
                    # reclassification primitive while retaining the normal
                    # document/registry/vector fan-out below.
                    node_props = self._neo4j.update_node(
                        node_id=node_id,
                        expected_current_digest=_expected_current_digest,
                        new_type=node_type,
                        new_properties=props,
                        new_space_id=space,
                    )
                output["stores"]["graph"] = "ok"
                output["node_data"] = node_props
            except (NodeIdentityConflict, EdgeIdentityConflict, GraphSchemaMigrationRequired, GraphWriteUnavailable, ValueError):
                raise
            except RuntimeError as exc:
                self._raise_graph_gate(exc)
                logger.warning("Neo4j node write failed for %s: %s", node_id, exc)
                output["stores"]["graph"] = f"error: {exc}"
            except Exception as exc:
                logger.warning("Neo4j node write failed for %s: %s", node_id, exc)
                output["stores"]["graph"] = f"error: {exc}"
        else:
            output["stores"]["graph"] = "unavailable"

        # --- MongoDB write ---
        if self._mongo is not None and self._mongo.available:
            try:
                mongo_id = self._mongo.upsert_node_doc(space, node_type, node_id, props)
                # store_write_succeeded() (below in this module) only
                # recognizes exactly "ok" or the "ok (...)" shape (status ==
                # "ok" or status.startswith("ok (")) as success — keep the
                # literal "ok (" (space + open-paren included) if this
                # format ever changes, or this status silently stops being
                # billed.
                output["stores"]["docs"] = f"ok (id={mongo_id})"
                self._mongo.log_event(
                    "node_upsert",
                    subject_id=principal.user_id,
                    details={"space": space, "node_type": node_type, "node_id": node_id},
                )
            except Exception as exc:
                logger.warning("MongoDB node write failed for %s: %s", node_id, exc)
                output["stores"]["docs"] = f"error: {exc}"
        else:
            output["stores"]["docs"] = "unavailable"

        # --- PostgreSQL registry write ---
        if self._sql is not None and self._sql.available:
            try:
                self._sql.register_node(space, node_type, node_id)
                output["stores"]["sql"] = "ok"
            except Exception as exc:
                logger.warning("SQL node registry write failed for %s: %s", node_id, exc)
                output["stores"]["sql"] = f"error: {exc}"
        else:
            output["stores"]["sql"] = "unavailable"

        # --- Chroma vector write ---
        if not write_vector:
            # design v7 §4-C-3: fork's raw node copy imports the original
            # embedding itself later (no re-embedding, #200) -- this leg
            # must not run for that call. Recorded, not silently skipped,
            # so `_fork_leg_ok` (opencrab/pack/fork.py) can require exactly
            # this value rather than treating an untouched "unavailable" as
            # equivalent.
            output["stores"]["vector"] = "skipped (raw copy)"
        elif self._vec is not None and self._vec.available:
            try:
                from opencrab.ontology.bm25 import _node_text
                text = _node_text({"node_id": node_id, "node_type": node_type, "properties": props})
                if text.strip():
                    meta = {
                        "pack_id": str(props.get("pack_id") or ""),
                        # `pack_id` 단일 소유 키. 종전엔 폐기 별칭 `properties.pack` 을
                        # 먼저 봤는데, 그 별칭이 stale 이면 벡터 `source` 가 옛 팩 이름으로
                        # 찍혔다(#159). `pack_id` 없는 행은 빈 문자열이 된다 — 그 행에
                        # source 를 주던 유일한 근거가 지금 없애는 별칭이었다.
                        "source": str(props.get("pack_id") or ""),
                        "node_id": node_id,
                        # #51 루트 픽스: space where-필터(query.py._build_chroma_where)가
                        # 매치할 키가 벡터 메타데이터에 없어 항상 0건이었다. 신규 벡터부터
                        # 기록한다 — 백필 전 기존 벡터는 여전히 없으므로 query.py 쪽에서
                        # 과도기 경고를 노출한다(sqlite_vec_store.py 주석 참조).
                        "space": space,
                    }
                    self._vec.upsert_texts(texts=[text], ids=[node_id], metadatas=[meta])
                    output["stores"]["vector"] = "ok"
                else:
                    output["stores"]["vector"] = "skipped (no text)"
            except Exception as exc:
                logger.warning("Chroma node write failed for %s: %s", node_id, exc)
                output["stores"]["vector"] = f"error: {exc}"
        else:
            output["stores"]["vector"] = "unavailable"

        logger.info("Node added: %s/%s (%s)", space, node_id, node_type)
        return output

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------

    def add_edge(
        self,
        from_space: str,
        from_id: str,
        relation: str,
        to_space: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
        *,
        pack_id: str,
        origin: str = "client",
        fork_copy: bool = False,
    ) -> dict[str, Any]:
        """
        Add a directed edge between two nodes.

        Validates the (from_space, to_space, relation) triple against
        the MetaOntology grammar before writing.

        Parameters
        ----------
        from_space:
            Source node's space.
        from_id:
            Source node's ID.
        relation:
            Relation label (must be valid for the given space pair).
        to_space:
            Target node's space.
        to_id:
            Target node's ID.
        properties:
            Optional edge properties.
        origin:
            See add_node. "server" is for the pack loader replaying its own
            dump. Today it is a no-op on this path -- EDGE_STAMPED is only
            ("pack_id",) and pack/normalize.py's apply_pack_tag has already
            overwritten that value upstream, so the client rule passes anyway.
            Passed for symmetry, and so the meaning does not change silently
            if owner_id is ever added to EDGE_STAMPED.
        fork_copy:
            See ``add_node``'s ``fork_copy`` -- same widened authorization
            (``write_gate.authorize_fork_copy``), same restriction to the
            pack's own fork-reserved ``creating`` window. There is no
            vector leg on an edge, so no ``write_vector`` counterpart here.

        Returns
        -------
        dict with operation status.

        Raises
        ------
        ValueError
            If the edge relation is invalid for the given spaces.
        """
        edge_result = validate_edge(from_space, to_space, relation)
        edge_result.raise_if_invalid()

        from opencrab.auth import current_principal

        # See add_node: gate first, alias normalisation second (#148).
        principal = current_principal()
        if fork_copy:
            authorize_fork_copy(self._sql, principal, pack_id)
        else:
            authorize(self._sql, principal, pack_id)
        props = stamp(
            properties, principal=principal, pack_id=pack_id, keys=EDGE_STAMPED,
            origin=origin,
        )
        canonicalize_pack_alias(props)          # #171 — see add_node
        receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
        receipt_ts = now_iso()

        output: dict[str, Any] = {
            "from": {"space": from_space, "id": from_id},
            "relation": relation,
            "to": {"space": to_space, "id": to_id},
            "receipt_id": receipt_id,
            "receipt_ts": receipt_ts,
            "stores": {},
        }

        # See add_node: graph down means the whole write is refused, not
        # fanned out to the optional stores (#146 follow-up).
        if self._neo4j is None or not self._neo4j.available:
            output["stores"] = {
                "graph": "unavailable",
                "docs": "skipped (graph unavailable)",
                "sql": "skipped (graph unavailable)",
            }
            return output

        # Resolve real node types from whichever graph store is available.
        # All four backends expose lookup_node_type(node_id) (see
        # opencrab/stores/_graph_protocol.py), so local mode no longer flattens
        # edge labels to a per-space default.
        #
        # A None lookup means the endpoint node does not exist. Falling back to
        # the space default here used to write a *wrong-typed* row: the SQL
        # backends' upsert_edge is a plain INSERT with no endpoint check, while
        # Neo4jStore.upsert_edge uses MATCH and writes nothing. Because
        # graph_edges' primary key includes from_type/to_type, such a row could
        # not be corrected by re-running the ingest -- it stayed as a permanent
        # dangling edge typed as the space's first node type (resource ->
        # Project, subject -> User).
        #
        # Both endpoints are now checked, and a missing one yields the same
        # "no match" contract Neo4j already had.
        #
        # The check only runs when the graph store is available and implements
        # lookup_node_type: an unavailable store cannot tell "node absent" from
        # "store down", and it writes nothing anyway, so no wrong-typed row can
        # be created. In that case the space default is kept as before.
        lookup = (
            getattr(self._neo4j, "lookup_node_type", None) if self._neo4j is not None else None
        )
        if lookup is not None and self._neo4j is not None and self._neo4j.available:
            from_type = lookup(from_id)
            to_type = lookup(to_id)
            missing = [
                f"{space}/{nid}"
                for space, nid, ntype in (
                    (from_space, from_id, from_type),
                    (to_space, to_id, to_type),
                )
                if ntype is None
            ]
        else:
            from_type = _space_to_default_type(from_space)
            to_type = _space_to_default_type(to_space)
            missing = []

        # #148: an edge may not straddle packs. export_edges_scoped needs BOTH
        # endpoints in scope, so a cross-pack edge is a row its own pack's
        # readers can never see -- refuse it instead of writing it.
        # Unattributed endpoints pass (legacy data predates pack attribution).
        #
        # Check the RESOLVED typed rows, not a guessed type-agnostic result.
        # Global node identity makes each resolved id the only valid endpoint
        # in a qualified graph target.
        if not missing:
            for endpoint_type, endpoint_id in ((from_type, from_id), (to_type, to_id)):
                reason = resolved_endpoint_pack_conflict(
                    self._neo4j, endpoint_type, endpoint_id, pack_id
                )
                if reason:
                    raise ValueError(
                        identity_reject_message("edge", endpoint_id, reason)
                    )

        # The edge's own slot, keyed by the backend's upsert conflict key --
        # which is (from_type, from_id, relation, to_type, to_id), the REAL
        # node types. This probe must therefore run AFTER the lookup above.
        # An earlier version passed the ontology *spaces* ("resource") where
        # the types ("Document") belong, so `get_edge` matched nothing, the
        # probe passed, and the write below -- which uses the resolved types --
        # could overwrite an existing edge and reattribute it to another pack.
        if not missing:
            reason = edge_identity_conflict(
                self._neo4j, from_type=from_type, from_id=from_id,
                relation=relation, to_type=to_type, to_id=to_id, pack_id=pack_id,
            )
            if reason:
                raise ValueError(identity_reject_message("edge", from_id, reason))

        # --- Neo4j write ---
        if missing:
            # Endpoint node absent -> refuse the write instead of inventing a type.
            logger.warning(
                "edge %s -[%s]-> %s skipped: endpoint node(s) not found: %s",
                from_id, relation, to_id, ", ".join(missing),
            )
            output["stores"]["graph"] = f"no match (missing node: {', '.join(missing)})"
            output["missing_nodes"] = missing
        elif self._neo4j is not None and self._neo4j.available:
            try:
                ok = self._neo4j.upsert_edge(from_type, from_id, relation, to_type, to_id, props)
                output["stores"]["graph"] = "ok" if ok else "no match"
            except (NodeIdentityConflict, EdgeIdentityConflict, GraphSchemaMigrationRequired, GraphWriteUnavailable, ValueError):
                raise
            except RuntimeError as exc:
                self._raise_graph_gate(exc)
                logger.warning("Neo4j edge write failed: %s", exc)
                output["stores"]["graph"] = f"error: {exc}"
            except Exception as exc:
                logger.warning("Neo4j edge write failed: %s", exc)
                output["stores"]["graph"] = f"error: {exc}"
        else:
            output["stores"]["graph"] = "unavailable"

        # --- PostgreSQL registry ---
        # Skipped when the graph write was refused, so the registry cannot end
        # up listing an edge the graph does not hold.
        if missing:
            output["stores"]["sql"] = "skipped (missing node)"
        elif self._sql is not None and self._sql.available:
            try:
                self._sql.register_edge(from_space, from_id, relation, to_space, to_id)
                output["stores"]["sql"] = "ok"
            except Exception as exc:
                logger.warning("SQL edge registry failed: %s", exc)
                output["stores"]["sql"] = f"error: {exc}"
        else:
            output["stores"]["sql"] = "unavailable"

        # --- MongoDB audit ---
        if self._mongo.available:
            try:
                self._mongo.log_event(
                    "edge_upsert",
                    subject_id=principal.user_id,
                    details={
                        "from_space": from_space,
                        "from_id": from_id,
                        "relation": relation,
                        "to_space": to_space,
                        "to_id": to_id,
                    },
                )
                # Deliberately not "ok"-prefixed: store_write_succeeded()
                # does NOT recognize "audited" as success (see its
                # docstring) — an edge's docs status is an audit log entry,
                # not a stored copy of the edge.
                output["stores"]["docs"] = "audited"
            except Exception as exc:
                logger.warning("MongoDB audit log write failed: %s", exc)
                output["stores"]["docs"] = f"error: {exc}"
        else:
            output["stores"]["docs"] = "unavailable"

        logger.info("Edge added: %s/%s -[%s]-> %s/%s", from_space, from_id, relation, to_space, to_id)
        return output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def store_write_failures(stores: dict[str, Any]) -> list[str]:
    """Return ``"{store}: {status}"`` entries for statuses that mean the
    write did NOT actually happen: ``"error: ..."`` (store threw) or
    ``"no match"`` / ``"no match (missing node: ...)"`` (edge endpoint
    missing, or the graph upsert matched nothing). ``"unavailable"`` and
    ``"skipped (...)"`` on the optional stores (docs/sql/vector) are NOT
    failures — those are expected when a store isn't configured or was
    deliberately skipped after a sibling failure.

    ``graph`` is different: it is the system of record, so ``"unavailable"``
    there means the write landed nowhere that counts, even if optional
    stores went through. That combination is rare in a real deployment
    (graph is always configured), but the contract of this function is
    "did the write actually happen", so an unavailable graph store must
    count as a failure too.

    Callers that only check ``add_node``/``add_edge`` for a raised exception
    (see the module docstring: individual store failures are swallowed and
    reported here, not raised) must call this on the returned ``stores`` map
    to know whether the write actually succeeded everywhere it matters.

    DIAGNOSTIC, not authoritative for billing: this is a NEGATIVE list — "no
    recognized failure string" is its pass condition, which is fail-open for
    any status shape it doesn't recognize (missing key, non-string value, a
    status this function was never taught). It exists for building
    human-readable error messages (``node_errors``/``edge_errors``/
    ``anchor_errors`` entries) where under-reporting an unrecognized status
    is an acceptable cost. Money-critical "did this actually get billable"
    decisions must use ``store_write_succeeded()`` below instead — issue #66's
    codex review is exactly the story of a billing gate built on this
    negative-list shape being fail-open (see ``graph_write_failed``'s history
    in git blame, and pack.py's legacy text-ingest gate, both now converted).
    """
    failures = []
    for store, status in stores.items():
        if not isinstance(status, str):
            continue
        if status.startswith("error:") or status.startswith("no match"):
            failures.append(f"{store}: {status}")
        elif store == "graph" and status == "unavailable":
            failures.append(f"{store}: {status}")
    return failures


def store_write_succeeded(stores: dict[str, Any], key: str | None = None) -> bool:
    """The single authoritative "did a write actually happen" check for
    money-critical (billing) decisions in this codebase. POSITIVE
    confirmation only: everything except a recognized success status is
    "not confirmed" — a missing key, a non-string value, a missing/non-dict
    ``stores``, or any status this function doesn't recognize. Never guess
    "probably fine" for a receipt shape this function doesn't recognize
    (issue #66's codex review: a fail-open "no known failure -> bill" check
    let malformed/incomplete receipts and ``"unavailable"`` optional-store
    statuses get billed for writes that landed nowhere).

    A status counts as success iff it is EXACTLY ``"ok"`` or matches the
    decorated shape ``"ok (...)"`` (starts with ``"ok ("``) — the two, and
    only two, real shapes ``OntologyBuilder.add_node``/``add_edge`` (this
    module) and ``HybridQuery.ingest`` (``opencrab/ontology/query.py``)
    actually assign, as of issue #66's 5th codex round:

      recognized as success (this is the whole contract, not a prefix
      guess — a bare ``startswith("ok")`` was tried in the 4th round and
      rejected for being wider than the real values: it would silently
      have billed a future ``"okay"``/``"ok-error: ..."`` status too):
        "ok"                     — graph node/edge write, sql registry write
        f"ok (id={mongo_id})"    — Mongo doc write (add_node)
        f"ok (id={vector_id})"   — Chroma vector write (query.py ingest())

      NOT recognized as success (on purpose):
        "audited"                 — Mongo edge audit log (builder.py
                                     add_edge). This IS a real, positive
                                     confirmation that the edge write
                                     happened (log_event succeeded) — but it
                                     is deliberately excluded here anyway,
                                     because every current edge-billing
                                     caller (graph.py#ontology_add_edge,
                                     harness.py, apply.py) uses
                                     key="graph", which never reads the
                                     "docs" entry at all — "audited"
                                     therefore has zero effect on any real
                                     billing decision today. Recognizing it
                                     would only matter for a future
                                     key=None caller that scans an edge's
                                     stores map, and nobody has decided
                                     whether an edge should be billable off
                                     of its audit log alone (vs. its graph
                                     write) — so it stays unrecognized
                                     until that decision is made on purpose,
                                     not inherited for free.
        "skipped (no text)"       — neither success nor failure.
        "skipped (missing node)"
        "unavailable" / "error: ..." / "no match..." — recognized failures.

    CONTRACT (enforced by convention, not code): any new success status
    added to this codebase MUST be exactly "ok" or "ok (...)" , or every
    caller of this function silently stops billing it — and conversely, a
    new FAILURE status must NOT start with "ok " (with a trailing space)
    or it would be misread as success. See the "ok (id=...)" assignment
    sites in this file and in query.py's ``ingest()`` — each carries a
    one-line pointer back to this contract.

    key:
        ``"graph"`` (or any specific store key) checks only that store —
        this is what a node/edge write's billing decision needs, since the
        graph store is the system of record and an optional-store-only
        success (e.g. only ``docs`` came back ``"ok"``, ``graph`` did not)
        must NOT count as a landed write. ``None`` (default) checks whether
        ANY store in the map succeeded — this is what a write with no
        single system-of-record store needs, e.g. pack.py's legacy
        text-ingest path, which never touches ``graph`` at all and can land
        in either the vector store or the doc store.
    """

    def _is_ok(status: Any) -> bool:
        # Exactly "ok", or the decorated "ok (...)" shape — NOT a bare
        # startswith("ok") prefix, which would also (wrongly) accept a
        # future "okay"/"ok-error: ..." status. isinstance() short-circuits
        # before .startswith() ever runs on a non-string value (None, a
        # dict, ...), so a malformed status can't raise here — a billing
        # decision must never blow up the write it's judging.
        return isinstance(status, str) and (status == "ok" or status.startswith("ok ("))

    if not isinstance(stores, dict):
        return False
    if key is not None:
        return _is_ok(stores.get(key))
    return any(_is_ok(status) for status in stores.values())


def graph_write_failed(stores: dict[str, Any]) -> bool:
    """True unless ``stores`` (an add_node/add_edge result's ``"stores"``
    map) POSITIVELY confirms the write landed in the graph store — the
    system of record. Thin wrapper over ``store_write_succeeded(stores,
    "graph")`` — see that function's docstring for the fail-closed
    contract this relies on.

    Optional-store status (docs/sql/vector) is irrelevant here on purpose —
    an optional-store-only failure still means the write landed (the entity
    exists and is queryable), matching the same "graph failed = hard
    failure, optional-store-only failed = partial success" split
    ``opencrab/mcp/tools/pack.py#pack_create`` already applies to its anchor
    node write.
    """
    return not store_write_succeeded(stores, "graph")


# ---------------------------------------------------------------------------
# Per-operation required stores (issue #163)
# ---------------------------------------------------------------------------

# Which stores a write of each kind must POSITIVELY land in before a caller
# may count it as written. Issue #163: ``store_write_succeeded()`` judges one
# receipt but never declared which store an operation actually requires, so
# every caller re-invented that decision (or skipped it).
#
# CHANGING THIS TABLE IS A CROSS-FILE DECISION: the billing gates in
# opencrab/mcp/tools/graph.py, opencrab/mcp/tools/harness.py,
# opencrab/mcp/tools/pack.py and crabharness/crabharness/apply.py hardcode the
# equivalent "graph" policy via graph_write_failed()/store_write_succeeded(...,
# "graph") and are deliberately NOT routed through here (see #163: "전역 기본
# 의미는 바꾸지 않는다 — 다른 소비자(과금 등)에 영향이 있다"). The two policies
# are pinned as equivalent by tests/test_store_receipt_callers.py; edit this
# table only together with a decision about those four call sites.
REQUIRED_STORES: dict[str, frozenset[str]] = {
    "node": frozenset({"graph"}),
    "edge": frozenset({"graph"}),
    # HybridQuery.ingest() (opencrab/ontology/query.py) ONLY — pack/load.py's
    # chunk loading is a separate (ok, err) contract that requires both the
    # vector and the doc store, so this mapping does not apply there.
    "chunk": frozenset({"chromadb"}),
}

NOT_APPLICABLE_STATUSES: frozenset[str] = frozenset({"audited"})


def is_not_applicable_status(status: Any) -> bool:
    """True for a status that is neither a failure nor a confirmed write.

    Today that is exactly ``"audited"`` — ``add_edge``'s Mongo audit-log
    entry above: the ``log_event`` succeeded, but an audit entry is not a
    stored copy of the edge, so it can never stand in for the edge's own
    write confirmation.

    SCOPE (deliberate, narrow): this classifies a status on a store OUTSIDE
    the operation's required set, for reports that want three buckets
    (landed / not-applicable / failed) instead of two.
    ``store_write_succeeded_for()`` does NOT consult it: an ``"audited"``
    status on a REQUIRED store is not success, and treating it as one is
    exactly the confusion issue #163 reports (``graph_ok=True,
    docs_audited=False, all_required_graph_docs=False``). Non-str input
    returns False.
    """
    return isinstance(status, str) and status in NOT_APPLICABLE_STATUSES


def store_write_succeeded_for(stores: Any, kind: str) -> bool:
    """POSITIVE confirmation that a ``kind`` write landed in EVERY store that
    kind requires (``REQUIRED_STORES``). Thin, fail-closed composition over
    ``store_write_succeeded(stores, key)`` — it inherits that function's
    "exactly 'ok' or 'ok (...)'" success contract, including its safety on
    non-str statuses (None/int/dict all read as not-confirmed).

    kind:
        A key of ``REQUIRED_STORES``. Anything else — an unknown string, or a
        non-str (incl. unhashable list/dict, which would otherwise raise
        TypeError from the dict lookup) — raises ``ValueError``. A typo'd
        kind must never silently read as either success or failure.

    Returns False (never raises) when ``stores`` is not a dict, matching
    ``store_write_succeeded``'s treatment of malformed receipts.

    A kind whose required set is empty returns False (fail-closed).
    Unreachable with today's table; pinned by test so a future empty entry
    cannot become a free "yes".
    """
    if not isinstance(kind, str) or kind not in REQUIRED_STORES:
        raise ValueError(
            f"unknown write kind: {kind!r} (known: {sorted(REQUIRED_STORES)})"
        )
    required = REQUIRED_STORES[kind]
    if not required:
        return False
    if not isinstance(stores, dict):
        return False
    return all(store_write_succeeded(stores, key) for key in required)


def _space_to_default_type(space_id: str) -> str:
    """Return a default node type label for a space when the real type is unknown."""
    from opencrab.grammar.manifest import SPACES

    spec = SPACES.get(space_id, {})
    types = spec.get("node_types", [])
    return types[0] if types else space_id.capitalize()
