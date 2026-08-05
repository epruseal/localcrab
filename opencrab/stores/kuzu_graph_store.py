"""
KuzuGraphStore — KùzuDB 기반 그래프 스토어. 런타임 패키지는 ladybug(KùzuDB가
리브랜딩된 이름, https://github.com/LadybugDB/ladybug)이며 Database/Connection
API는 kuzu와 동일하다. 클래스명·STORAGE_MODE="kuzu" 값은 공개 인터페이스
하위호환을 위해 그대로 유지한다.

LocalGraphStore(SQLite + Python BFS)와 동일한 인터페이스를 구현한다.

요구 버전: ladybug>=0.18. RPi5 aarch64 (CONFIG_PAGE_SIZE_16KB=y) 환경에서
구버전(kuzu 0.11.3)은 buffer manager가 4KB 단위 madvise를 호출해 EINVAL로
조용히 죽었다(LD_PRELOAD=madv_noop.so 우회 필요). 이 버그는
LadybugDB/ladybug#526으로 보고되어 #527("Handle larger OS page sizes in VM
eviction")로 수정되었고 v0.18.0(2026-07-01)에 포함되어, LD_PRELOAD 우회 없이
동작한다.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from typing import Any

from opencrab.stores._graph_common import (
    _edge_passes,
    _merge_space,
    _node_passes,
    _space_passes,
)
from opencrab.stores._json import parse_props as _parse

logger = logging.getLogger(__name__)

_NODE_DDL = (
    "CREATE NODE TABLE OntologyNode("
    "node_id STRING, node_type STRING, space_id STRING, props STRING, "
    "PRIMARY KEY(node_id))"
)
_EDGE_DDL = (
    "CREATE REL TABLE OntologyEdge("
    "FROM OntologyNode TO OntologyNode, "
    "relation STRING, properties STRING)"
)


class KuzuGraphStore:
    """KùzuDB-backed graph store with the same interface as LocalGraphStore.

    Runtime package is ``ladybug`` (>=0.18) — the rebranded KùzuDB — but the
    class name and STORAGE_MODE="kuzu" value stay unchanged for backward
    compatibility.
    """

    def __init__(
        self,
        db_path: str,
        buffer_pool_size: int = 256 * 1024 * 1024,
    ) -> None:
        self._db_path = db_path
        self._available = False
        try:
            import ladybug  # ladybug>=0.18 (rebranded KùzuDB, Database/Connection API unchanged)

            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
            self._db = ladybug.Database(db_path, buffer_pool_size=buffer_pool_size)
            self._conn = ladybug.Connection(self._db)
            self._ensure_schema()
            self._available = True
            logger.info("KuzuGraphStore initialised at %s", db_path)
        except Exception as exc:
            logger.warning("KuzuGraphStore init failed: %s", exc)

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        for ddl in (_NODE_DDL, _EDGE_DDL):
            try:
                self._conn.execute(ddl)
            except Exception:
                pass  # table already exists

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    def close(self) -> None:
        if self._available:
            try:
                self._db.close()
            except Exception:
                pass
            self._available = False

    def ping(self) -> bool:
        try:
            self._conn.execute("MATCH (n:OntologyNode) RETURN count(n) LIMIT 1")
            return True
        except Exception:
            return False

    def ensure_constraints(self) -> None:
        pass  # PRIMARY KEY constraint in schema covers uniqueness

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")

    # ------------------------------------------------------------------
    # Node operations
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
        space_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_available()
        props = {**properties, "id": node_id}
        props_json = json.dumps(props)
        self._conn.execute(
            "MERGE (n:OntologyNode {node_id: $id}) "
            "SET n.node_type = $nt, n.space_id = $sid, n.props = $p",
            {"id": node_id, "nt": node_type, "sid": space_id or "", "p": props_json},
        )
        return props

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        self._require_available()
        # n.space_id is folded in for the same reason as export_nodes (see
        # _merge_space); this is also the funnel for _batch_node_props.
        r = self._conn.execute(
            "MATCH (n:OntologyNode {node_id: $id, node_type: $nt}) "
            "RETURN n.props, n.space_id LIMIT 1",
            {"id": node_id, "nt": node_type},
        )
        if r.has_next():
            row = r.get_next()
            return _merge_space(_parse(row[0]), row[1])
        return None

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        self._require_available()
        r = self._conn.execute(
            "MATCH (n:OntologyNode {node_id: $id}) "
            "RETURN n.node_type, n.props, n.space_id LIMIT 1",
            {"id": node_id},
        )
        if not r.has_next():
            return None
        row = r.get_next()
        props = dict(_merge_space(_parse(row[1]), row[2]))
        props["node_type"] = row[0]
        props.setdefault("id", node_id)
        return props

    def lookup_node_type(self, node_id: str) -> str | None:
        """builder.add_edge duck-typing 인터페이스 — LocalGraphStore·Neo4jStore와 동일 시그니처."""
        info = self.get_node_by_id(node_id)
        return info.get("node_type") if info else None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """True iff the node itself was deleted (unified B2 contract) — a
        DETACH DELETE matching zero nodes does not raise, so existence must
        be checked before the delete rather than inferred from "no exception".
        The MATCH is on the (node_type, node_id) PAIR, matching every other
        backend's delete_node contract — a wrong node_type must be a no-op."""
        self._require_available()
        try:
            existed = self.get_node(node_type, node_id) is not None
            self._conn.execute(
                "MATCH (n:OntologyNode {node_id: $id, node_type: $nt}) DETACH DELETE n",
                {"id": node_id, "nt": node_type},
            )
            return existed
        except Exception as exc:
            logger.warning("KuzuGraphStore delete_node error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    def upsert_edge(
        self,
        from_type: str,
        from_id: str,
        relation: str,
        to_type: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
    ) -> bool:
        self._require_available()
        props_json = json.dumps(properties or {})
        try:
            self._conn.execute(
                "MATCH (a:OntologyNode {node_id: $fid}), (b:OntologyNode {node_id: $tid}) "
                "MERGE (a)-[e:OntologyEdge {relation: $rel}]->(b) "
                "SET e.properties = $props",
                {"fid": from_id, "tid": to_id, "rel": relation, "props": props_json},
            )
            return True
        except Exception as exc:
            logger.warning("KuzuGraphStore upsert_edge error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if not self._available:
            return []
        try:
            r = self._conn.execute(cypher, params or {})
            cols = r.get_column_names()
            rows = r.get_all()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:
            logger.warning("KuzuGraphStore run_cypher error: %s", exc)
            return []

    def find_neighbors(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
        spaces: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """``spaces`` (issue #52): strict space-membership filter. Unlike
        ``pack_id`` (opaque JSON blob here, see #62's note in
        ``_find_neighbors_1hop`` on why that filter is NOT pushed into
        Cypher), ``space_id`` is a real top-level column on ``OntologyNode``
        (see ``_NODE_DDL``), so this one CAN be pushed into the Cypher WHERE
        ahead of LIMIT."""
        self._require_available()

        pack_set: set[str] | None = set(pack_ids) if pack_ids else None
        space_set: set[str] | None = set(spaces) if spaces else None

        if pack_set is not None:
            anchor = self.get_node_by_id(node_id)
            if not _node_passes(anchor or {}, pack_set, include_unpackaged):
                return []
        if space_set is not None:
            anchor = self.get_node_by_id(node_id)
            if not _space_passes(anchor or {}, space_set):
                return []

        if depth == 1:
            return self._find_neighbors_1hop(
                node_id, direction, limit, pack_set, include_unpackaged, space_set
            )

        # depth > 1: Python BFS using 1-hop queries
        visited: set[str] = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        results: list[dict[str, Any]] = []

        while queue and len(results) < limit:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            hops = self._find_neighbors_1hop(
                current_id, direction, limit - len(results),
                pack_set, include_unpackaged, space_set,
            )
            for nb in hops:
                nid = nb.get("properties", {}).get("id")
                if not nid or nid in visited:
                    continue
                visited.add(nid)
                nb["depth"] = current_depth + 1
                results.append(nb)
                if len(results) >= limit:
                    break
                queue.append((nid, current_depth + 1))

        return results[:limit]

    def _find_neighbors_1hop(
        self,
        node_id: str,
        direction: str,
        limit: int,
        pack_set: set[str] | None,
        include_unpackaged: bool,
        space_set: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        # ISSUE #62 (NOT fixed here — left as a clear note, not a half-fix):
        # this method has the same LIMIT-before-pack-filter defect as the SQL
        # backends had (fixed in _sql_graph_base.py's _fetch_edges_for_node /
        # pg_graph_store.py's _batch_frontier_edges via a shared _pack_where
        # SQL-predicate generator) — `LIMIT {limit}` below runs before
        # `_collect_1hop` applies `_node_passes`/`_edge_passes`, so a hub
        # whose first `limit` edges are all out-of-pack can starve in-pack
        # neighbours that exist further down the scan.
        #
        # Pushing the filter into this Cypher WHERE the way neo4j_store.py
        # does is NOT a safe drop-in here: OntologyNode.props / OntologyEdge
        # .properties are declared STRING (a serialized JSON blob — see the
        # CREATE TABLE DDL above and `_parse()`), not per-field typed
        # columns, so there is no `m.pack_id`/`e.pack_id` to put in a WHERE
        # clause. Doing this safely would need either a real `pack_id`
        # column populated at write time (schema + backfill, out of this
        # fix's scope) or Kuzu's JSON extension (`INSTALL json; LOAD json;`)
        # to extract from the blob in Cypher — an added runtime dependency
        # this fix should not silently introduce. Left unfixed; a real
        # `pack_id` column is the safe follow-up, using this method's SQL
        # counterpart as the template for what the WHERE clause should say.
        #
        # `space_set` (issue #52) does NOT have this problem: `space_id` is
        # already a real top-level column (see `_NODE_DDL`), so it IS pushed
        # into the Cypher WHERE below, ahead of LIMIT — no JSON extension
        # needed.
        #
        # direction="both" is issued as two *directed* passes rather than one
        # undirected MATCH: the undirected form cannot say which side of the
        # edge the anchor was on, and find_neighbors' from_id/to_id contract
        # needs that. Total results are still capped at `limit`.
        arrows = {
            "out": ("-[e:OntologyEdge]->", True),
            "in": ("<-[e:OntologyEdge]-", False),
        }
        passes = [arrows[direction]] if direction in arrows else [arrows["out"], arrows["in"]]

        seen: set[str] = set()
        buckets: list[list[dict[str, Any]]] = []
        params: dict[str, Any] = {"id": node_id}
        where_sql = ""
        if space_set is not None:
            params["spaces"] = sorted(space_set)
            where_sql = " WHERE m.space_id IN $spaces"
        for arrow, is_out in passes:
            q = (
                f"MATCH (n:OntologyNode {{node_id: $id}}){arrow}(m:OntologyNode)"
                f"{where_sql} "
                "RETURN m.node_id, m.node_type, m.props, e.relation, e.properties, m.space_id"
            )
            r = self._conn.execute(q + f" LIMIT {int(limit)}", params)
            bucket: list[dict[str, Any]] = []
            self._collect_1hop(
                r, node_id, is_out, bucket, seen, pack_set, include_unpackaged, space_set
            )
            buckets.append(bucket)

        if len(buckets) == 1:
            return buckets[0][:limit]

        # Round-robin between directions rather than draining "out" first: a
        # hub with more than `limit` out-edges would otherwise starve its
        # in-edges completely, which the single undirected MATCH never did.
        results: list[dict[str, Any]] = []
        for i in range(max((len(b) for b in buckets), default=0)):
            for bucket in buckets:
                if i < len(bucket):
                    results.append(bucket[i])
                    if len(results) >= limit:
                        return results
        return results

    def _collect_1hop(
        self,
        r: Any,
        node_id: str,
        is_out: bool,
        results: list[dict[str, Any]],
        seen: set[str],
        pack_set: set[str] | None,
        include_unpackaged: bool,
        space_set: set[str] | None = None,
    ) -> None:
        """Drain one directed 1-hop result set into ``results`` (dedup by node)."""
        while r.has_next():
            row = r.get_next()
            nid, ntype, props_raw, rel, edge_props_raw, m_space = (
                row[0], row[1], row[2], row[3], row[4], row[5]
            )
            # m.space_id folded in so neighbour props match get_node's shape.
            props = dict(_merge_space(_parse(props_raw), m_space))
            props.setdefault("id", nid)
            if space_set is not None and not _space_passes(props, space_set):
                # Redundant for the common case (already pushed into the
                # Cypher WHERE above, ahead of LIMIT) — kept as
                # defense-in-depth for the same _merge_space precedence edge
                # case documented in _sql_graph_base.py's _expand.
                continue
            if pack_set is not None:
                # `node_id` is always the anchor side of this 1-hop query (the
                # caller already verified it passes — once up front in
                # find_neighbors for depth==1, or transitively for depth>1
                # since every queued node already passed this same check when
                # first discovered), so only the far side ("m", here) needs a
                # fresh _node_passes check; pass True for the already-known
                # side into _edge_passes (AND with True is a no-op either way).
                m_passes = _node_passes(props, pack_set, include_unpackaged)
                if not m_passes:
                    continue
                edge_props = _parse(edge_props_raw)
                if not _edge_passes(edge_props, True, m_passes, pack_set):
                    continue
            # Deduplicate: same destination via multiple edges → return once
            if nid in seen:
                continue
            seen.add(nid)
            results.append({
                "properties": props,
                "labels": [ntype],
                "relation_type": rel,
                "relationship_types": [rel],
                "depth": 1,
                # Canonical edge endpoints (see _sql_graph_base._expand).
                "from_id": node_id if is_out else nid,
                "to_id": nid if is_out else node_id,
            })

    def find_by_relations(
        self,
        node_id: str,
        relations: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self._require_available()
        if not relations:
            return []
        limit = int(limit)

        results: list[dict[str, Any]] = []

        # m.space_id is folded in (see _merge_space). The SQL backends get this
        # for free because their find_by_relations/find_path go through
        # get_node; Kuzu issues its own Cypher, so each query needs the column.
        def _fetch(q: str) -> None:
            r = self._conn.execute(q, {"id": node_id, "rels": relations})
            while r.has_next() and len(results) < limit:
                row = r.get_next()
                nid, ntype, props_raw, rel, m_space = row[0], row[1], row[2], row[3], row[4]
                props = dict(_merge_space(_parse(props_raw), m_space))
                props.setdefault("id", nid)
                results.append({
                    "properties": props,
                    "labels": [ntype],
                    "relation_type": rel,
                })

        if direction in ("out", "both"):
            _fetch(
                "MATCH (n:OntologyNode {node_id: $id})-[e:OntologyEdge]->(m:OntologyNode) "
                "WHERE e.relation IN $rels "
                f"RETURN m.node_id, m.node_type, m.props, e.relation, m.space_id LIMIT {limit}"
            )
        if direction in ("in", "both") and len(results) < limit:
            _fetch(
                "MATCH (n:OntologyNode {node_id: $id})<-[e:OntologyEdge]-(m:OntologyNode) "
                "WHERE e.relation IN $rels "
                f"RETURN m.node_id, m.node_type, m.props, e.relation, m.space_id "
                f"LIMIT {limit - len(results)}"
            )
        return results

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        self._require_available()

        visited: set[str] = {from_id}
        queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(from_id, [])])

        while queue:
            current_id, path = queue.popleft()
            if len(path) >= max_depth:
                continue
            # m.space_id folded in for the same reason as find_by_relations.
            r = self._conn.execute(
                "MATCH (n:OntologyNode {node_id: $id})-[e:OntologyEdge]->(m:OntologyNode) "
                "RETURN m.node_id, m.node_type, m.props, e.relation, m.space_id",
                {"id": current_id},
            )
            while r.has_next():
                row = r.get_next()
                nid, ntype, props_raw, rel, m_space = row[0], row[1], row[2], row[3], row[4]
                props = dict(_merge_space(_parse(props_raw), m_space))
                props.setdefault("id", nid)
                props.setdefault("node_type", ntype)
                new_path = path + [{"node": props, "relation": rel}]
                if nid == to_id:
                    return new_path
                if nid not in visited:
                    visited.add(nid)
                    queue.append((nid, new_path))
        return []

    def count_nodes(self, node_type: str | None = None) -> int:
        self._require_available()
        if node_type:
            r = self._conn.execute(
                "MATCH (n:OntologyNode {node_type: $nt}) RETURN count(n)",
                {"nt": node_type},
            )
        else:
            r = self._conn.execute("MATCH (n:OntologyNode) RETURN count(n)")
        return int(r.get_next()[0])

    # ------------------------------------------------------------------
    # Extended operations (LocalGraphStore interface parity)
    # ------------------------------------------------------------------

    def list_packs(self, min_nodes: int = 1) -> list[dict[str, Any]]:
        self._require_available()
        r = self._conn.execute(
            "MATCH (n:OntologyNode) RETURN n.node_id, n.props"
        )
        counts: dict[str, int] = {}
        anchor_titles: dict[str, str] = {}
        anchor_descs: dict[str, str] = {}
        pkg_titles: dict[str, str] = {}
        while r.has_next():
            row = r.get_next()
            node_id, props = row[0], _parse(row[1])
            pid = props.get("pack_id")
            if not pid:
                continue
            pid = str(pid)
            counts[pid] = counts.get(pid, 0) + 1
            # 1순위: pack_create anchor (node_id == "dataset:{pack_id}")
            if node_id == f"dataset:{pid}":
                t = props.get("title") or ""
                if t:
                    anchor_titles[pid] = t
                d = props.get("description") or ""
                if d:
                    anchor_descs[pid] = d
            # 2순위: source_package_title (외부 pack 로더)
            if pid not in pkg_titles:
                t = props.get("source_package_title") or ""
                if t:
                    pkg_titles[pid] = t
        return [
            {
                "pack_id": pid,
                "node_count": cnt,
                "sample_title": anchor_titles.get(pid) or pkg_titles.get(pid) or "",
                # description은 anchor에만 존재한다(source_package_title 같은
                # 노드 단위 폴백이 없다) — 없으면 빈 문자열.
                "sample_description": anchor_descs.get(pid, ""),
            }
            for pid, cnt in sorted(counts.items(), key=lambda x: -x[1])
            if cnt >= min_nodes
        ]

    def export_nodes(
        self,
        pack_id: str | None = None,
        limit: int = 500_000,
        space: str | None = None,
    ) -> list[dict[str, Any]]:
        """``space``, when given, is pushed into the Cypher WHERE clause
        ahead of LIMIT via the ``space_id`` node property -- a plain
        equality check, since Kuzu keeps space in its own column (see
        _NODE_DDL) just like the SQL backends (issue #54). ``pack_id``
        stays a Python post-filter: it lives inside the JSON-serialized
        ``props`` blob, which this Cypher query has no native way to
        index into (pre-existing, separate from #54's space bug)."""
        self._require_available()
        # space_id is returned so _merge_space can restore it into props: this
        # backend keeps space in its own column (see _NODE_DDL), but the
        # protocol's export shape carries it inside props.
        # WHERE clause is only added when space is given -- a bound $space
        # parameter of None makes Kuzu's query planner unable to infer the
        # parameter's type ("Value is not a valid boolean"), since unlike
        # the SQL backends it has no untyped-NULL literal to fall back on.
        where_clause = "WHERE n.space_id = $space " if space is not None else ""
        params = {"space": space} if space is not None else {}
        r = self._conn.execute(
            f"MATCH (n:OntologyNode) {where_clause}"
            f"RETURN n.node_type, n.space_id, n.props LIMIT {int(limit)}",
            params,
        )
        results: list[dict[str, Any]] = []
        while r.has_next():
            row = r.get_next()
            ntype, space_id, props_raw = row[0], row[1], row[2]
            props = _merge_space(_parse(props_raw), space_id)
            if pack_id is not None:
                if (
                    props.get("pack_id") != pack_id
                    and props.get("source") != pack_id
                    and props.get("source_id") != pack_id
                ):
                    continue
            results.append({"props": props, "labels": [ntype]})
        return results

    def export_edges(
        self,
        pack_id: str | None = None,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        self._require_available()
        # a.space_id / b.space_id: same reason as export_nodes -- both
        # endpoints' props must carry their space.
        r = self._conn.execute(
            f"MATCH (a:OntologyNode)-[e:OntologyEdge]->(b:OntologyNode) "
            f"RETURN a.node_type, a.props, b.node_type, b.props, e.relation, e.properties, "
            f"a.space_id, b.space_id "
            f"LIMIT {int(limit)}"
        )
        results: list[dict[str, Any]] = []
        while r.has_next():
            row = r.get_next()
            at, ap, bt, bp, rel, ep, asp, bsp = (
                row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
            )
            sp = _merge_space(_parse(ap), asp)
            tp = _merge_space(_parse(bp), bsp)
            rp = _parse(ep)
            if pack_id is not None:
                if (
                    sp.get("pack_id") != pack_id
                    and sp.get("source") != pack_id
                    and tp.get("pack_id") != pack_id
                    and tp.get("source") != pack_id
                    and rp.get("pack_id") != pack_id
                ):
                    continue
            results.append({
                "source_props":  sp,
                "source_labels": [at],
                "target_props":  tp,
                "target_labels": [bt],
                "rel_props":     rp,
                "relation":      rel,
            })
        return results

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]]) -> int:
        self._require_available()
        for n in nodes:
            self.upsert_node(
                n["node_type"],
                n["node_id"],
                n.get("properties", {}),
                n.get("space_id"),
            )
        return len(nodes)

    def upsert_edges_batch(self, edges: list[dict[str, Any]]) -> int:
        self._require_available()
        count = 0
        for e in edges:
            ok = self.upsert_edge(
                e["from_type"],
                e["from_id"],
                e["relation"],
                e["to_type"],
                e["to_id"],
                e.get("properties"),
            )
            if ok:
                count += 1
        return count
