"""
KuzuGraphStore — KùzuDB 0.11.3 기반 그래프 스토어.

LocalGraphStore(SQLite + Python BFS)와 동일한 인터페이스를 구현한다.
RPi5 aarch64 (CONFIG_PAGE_SIZE_16KB=y) 환경에서는 LD_PRELOAD=madv_noop.so 필요.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from typing import Any

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
    """KùzuDB-backed graph store with the same interface as LocalGraphStore."""

    def __init__(
        self,
        db_path: str,
        buffer_pool_size: int = 256 * 1024 * 1024,
    ) -> None:
        import kuzu  # kuzu==0.11.3

        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._db_path = db_path
        self._available = False
        try:
            self._db = kuzu.Database(db_path, buffer_pool_size=buffer_pool_size)
            self._conn = kuzu.Connection(self._db)
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
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        props = {**properties, "id": node_id}
        props_json = json.dumps(props)
        self._conn.execute(
            "MERGE (n:OntologyNode {node_id: $id}) "
            "SET n.node_type = $nt, n.space_id = $sid, n.props = $p",
            {"id": node_id, "nt": node_type, "sid": space_id or "", "p": props_json},
        )
        return props

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        r = self._conn.execute(
            "MATCH (n:OntologyNode {node_id: $id, node_type: $nt}) RETURN n.props LIMIT 1",
            {"id": node_id, "nt": node_type},
        )
        if r.has_next():
            return _parse(r.get_next()[0])
        return None

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        r = self._conn.execute(
            "MATCH (n:OntologyNode {node_id: $id}) "
            "RETURN n.node_type, n.props LIMIT 1",
            {"id": node_id},
        )
        if not r.has_next():
            return None
        row = r.get_next()
        props = _parse(row[1])
        props["node_type"] = row[0]
        props.setdefault("id", node_id)
        return props

    def lookup_node_type(self, node_id: str) -> str | None:
        """builder.add_edge duck-typing 인터페이스 — LocalGraphStore·Neo4jStore와 동일 시그니처."""
        info = self.get_node_by_id(node_id)
        return info.get("node_type") if info else None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        try:
            self._conn.execute(
                "MATCH (n:OntologyNode {node_id: $id}) DETACH DELETE n",
                {"id": node_id},
            )
            return True
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
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
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
    ) -> list[dict[str, Any]]:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")

        pack_set: set[str] | None = set(pack_ids) if pack_ids else None

        if pack_set is not None:
            anchor = self.get_node_by_id(node_id)
            anchor_pid = (anchor or {}).get("pack_id")
            if anchor_pid is None:
                if not include_unpackaged:
                    return []
            elif anchor_pid not in pack_set:
                return []

        if depth == 1:
            return self._find_neighbors_1hop(
                node_id, direction, limit, pack_set, include_unpackaged
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
                pack_set, include_unpackaged,
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
    ) -> list[dict[str, Any]]:
        if direction == "out":
            q = (
                "MATCH (n:OntologyNode {node_id: $id})-[e:OntologyEdge]->(m:OntologyNode) "
                "RETURN m.node_id, m.node_type, m.props, e.relation, e.properties"
            )
        elif direction == "in":
            q = (
                "MATCH (n:OntologyNode {node_id: $id})<-[e:OntologyEdge]-(m:OntologyNode) "
                "RETURN m.node_id, m.node_type, m.props, e.relation, e.properties"
            )
        else:
            q = (
                "MATCH (n:OntologyNode {node_id: $id})-[e:OntologyEdge]-(m:OntologyNode) "
                "RETURN m.node_id, m.node_type, m.props, e.relation, e.properties"
            )
        r = self._conn.execute(q + f" LIMIT {int(limit)}", {"id": node_id})
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        while r.has_next():
            row = r.get_next()
            nid, ntype, props_raw, rel, edge_props_raw = row[0], row[1], row[2], row[3], row[4]
            props = _parse(props_raw)
            props.setdefault("id", nid)
            if pack_set is not None:
                pid = props.get("pack_id")
                if pid is None:
                    if not include_unpackaged:
                        continue
                elif pid not in pack_set:
                    continue
                # Edge pack_id rule: foreign-pack edge always excluded
                edge_props = _parse(edge_props_raw)
                edge_pid = edge_props.get("pack_id") if isinstance(edge_props, dict) else None
                if edge_pid is not None and edge_pid not in pack_set:
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
            })
        return results

    def find_by_relations(
        self,
        node_id: str,
        relations: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        if not relations:
            return []

        results: list[dict[str, Any]] = []

        def _fetch(q: str) -> None:
            r = self._conn.execute(q, {"id": node_id, "rels": relations})
            while r.has_next() and len(results) < limit:
                row = r.get_next()
                nid, ntype, props_raw, rel = row[0], row[1], row[2], row[3]
                props = _parse(props_raw)
                props.setdefault("id", nid)
                results.append({
                    "properties": props,
                    "labels": [ntype],
                    "relation_type": rel,
                })

        if direction in ("out", "both"):
            _fetch(
                "MATCH (n:OntologyNode {node_id: $id})-[e:OntologyEdge]->(m:OntologyNode) "
                f"WHERE e.relation IN $rels RETURN m.node_id, m.node_type, m.props, e.relation LIMIT {limit}"
            )
        if direction in ("in", "both") and len(results) < limit:
            _fetch(
                "MATCH (n:OntologyNode {node_id: $id})<-[e:OntologyEdge]-(m:OntologyNode) "
                f"WHERE e.relation IN $rels RETURN m.node_id, m.node_type, m.props, e.relation LIMIT {limit - len(results)}"
            )
        return results

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")

        visited: set[str] = {from_id}
        queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(from_id, [])])

        while queue:
            current_id, path = queue.popleft()
            if len(path) >= max_depth:
                continue
            r = self._conn.execute(
                "MATCH (n:OntologyNode {node_id: $id})-[e:OntologyEdge]->(m:OntologyNode) "
                "RETURN m.node_id, m.node_type, m.props, e.relation",
                {"id": current_id},
            )
            while r.has_next():
                row = r.get_next()
                nid, ntype, props_raw, rel = row[0], row[1], row[2], row[3]
                props = _parse(props_raw)
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
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
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
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        r = self._conn.execute(
            "MATCH (n:OntologyNode) RETURN n.node_id, n.props"
        )
        counts: dict[str, int] = {}
        anchor_titles: dict[str, str] = {}
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
            }
            for pid, cnt in sorted(counts.items(), key=lambda x: -x[1])
            if cnt >= min_nodes
        ]

    def export_nodes(
        self,
        pack_id: str | None = None,
        limit: int = 500_000,
    ) -> list[dict[str, Any]]:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        r = self._conn.execute(
            f"MATCH (n:OntologyNode) RETURN n.node_type, n.props LIMIT {int(limit)}"
        )
        results: list[dict[str, Any]] = []
        while r.has_next():
            row = r.get_next()
            ntype, props_raw = row[0], row[1]
            props = _parse(props_raw)
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
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        r = self._conn.execute(
            f"MATCH (a:OntologyNode)-[e:OntologyEdge]->(b:OntologyNode) "
            f"RETURN a.node_type, a.props, b.node_type, b.props, e.relation, e.properties "
            f"LIMIT {int(limit)}"
        )
        results: list[dict[str, Any]] = []
        while r.has_next():
            row = r.get_next()
            at, ap, bt, bp, rel, ep = (
                row[0], row[1], row[2], row[3], row[4], row[5]
            )
            sp = _parse(ap)
            tp = _parse(bp)
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
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
        for n in nodes:
            self.upsert_node(
                n["node_type"],
                n["node_id"],
                n.get("properties", {}),
                n.get("space_id"),
            )
        return len(nodes)

    def upsert_edges_batch(self, edges: list[dict[str, Any]]) -> int:
        if not self._available:
            raise RuntimeError("KuzuGraphStore is not available.")
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
