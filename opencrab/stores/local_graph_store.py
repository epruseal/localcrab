"""
Local graph store — SQLite-backed graph for local-only mode.

Implements the same interface as Neo4jStore so store consumers are
agnostic of the backend. Nodes and edges are stored in SQLite tables;
BFS traversal is done in Python.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


def _node_pack_id(props: dict[str, Any]) -> str | None:
    """Top-level lookup mirroring the unified provenance helper.

    Imported lazily to avoid an import cycle (pack_provenance imports nothing
    from opencrab.stores, but keep this file dependency-light).
    """
    pid = props.get("pack_id") if isinstance(props, dict) else None
    if pid:
        return str(pid)
    return None


def _node_passes(
    props: dict[str, Any],
    pack_set: set[str] | None,
    include_unpackaged: bool,
) -> bool:
    if not pack_set:
        return True
    pid = _node_pack_id(props)
    if pid is None:
        return include_unpackaged
    return pid in pack_set


def _edge_passes(
    edge_props: dict[str, Any],
    src_passes: bool,
    dst_passes: bool,
    pack_set: set[str] | None,
) -> bool:
    """Apply the agreed edge filter rules.

    Rules (see plan §4):
      1. edge.pack_id in pack_set        -> pass (endpoints still must pass)
      2. edge.pack_id not in pack_set    -> always exclude
      3. edge has no pack_id             -> only pass when both endpoints pass
    """
    if not pack_set:
        return True
    edge_pid = _node_pack_id(edge_props) if isinstance(edge_props, dict) else None
    if edge_pid is not None:
        if edge_pid not in pack_set:
            return False
        return src_passes and dst_passes
    return src_passes and dst_passes

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS graph_nodes (
        node_type   TEXT NOT NULL,
        node_id     TEXT NOT NULL,
        space_id    TEXT,
        properties  TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (node_type, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_edges (
        from_type   TEXT NOT NULL,
        from_id     TEXT NOT NULL,
        relation    TEXT NOT NULL,
        to_type     TEXT NOT NULL,
        to_id       TEXT NOT NULL,
        properties  TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (from_type, from_id, relation, to_type, to_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edges_from ON graph_edges(from_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_to   ON graph_edges(to_id)",
    # pack_id 인덱스: content_pack_list의 GROUP BY와 pack 필터 쿼리(find_neighbors의
    # _node_passes 등)가 properties JSON에서 pack_id를 반복 추출한다. 표현식 인덱스를
    # 걸어두면 O(N) 전체 스캔이 O(log N)으로 줄어든다.
    # json_extract는 SQLite 3.9.0+(2015)부터 지원 — pyproject.toml/README 참고.
    "CREATE INDEX IF NOT EXISTS idx_nodes_pack"
    " ON graph_nodes(json_extract(properties, '$.pack_id'))",
]


class LocalGraphStore:
    """SQLite-backed graph store with the same interface as Neo4jStore."""

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._db_path = db_path
        self._available = False
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        try:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

            # WAL(Write-Ahead Logging) 모드 활성화:
            #   기본 DELETE 모드는 쓰기 시 DB 파일 전체에 배타 잠금을 걸어
            #   MCP 서버처럼 다중 스레드가 동시 접근하는 환경에서 읽기 지연을 유발한다.
            #   WAL 모드는 reader-writer를 격리시켜 쓰기 중에도 읽기를 허용한다.
            #
            # synchronous=NORMAL:
            #   기본 FULL은 매 트랜잭션마다 fsync를 두 번 호출한다. NORMAL은 WAL
            #   체크포인트 시에만 fsync를 수행해 쓰기 처리량을 높인다.
            #   NVMe SSD + 단일 머신 환경에서 전원 장애로 인한 WAL 손상 위험은
            #   수용 가능한 수준이다 (OS가 WAL 파일을 fsync하지 않더라도 DB 파일
            #   자체는 손상되지 않는다).
            #
            # 주의: WAL 모드 전환은 기존 DB에도 안전하게 적용된다. 단, DB 파일
            #   옆에 <db>-wal, <db>-shm 파일이 생성되므로 백업 시 세 파일을 함께
            #   복사해야 한다.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

            cur = self._conn.cursor()
            for ddl in _DDL:
                cur.execute(ddl)
            self._conn.commit()
            self._available = True
            logger.info("LocalGraphStore initialised at %s", self._db_path)
        except Exception as exc:
            logger.warning("LocalGraphStore init failed: %s", exc)

    @property
    def available(self) -> bool:
        return self._available

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass

    def ping(self) -> bool:
        try:
            assert self._conn
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Schema (no-op for local mode)
    # ------------------------------------------------------------------

    def ensure_constraints(self) -> None:
        pass  # PRIMARY KEY constraints cover uniqueness

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
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        props = {**properties, "id": node_id}
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO graph_nodes(node_type, node_id, space_id, properties)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(node_type, node_id) DO UPDATE SET
                space_id   = excluded.space_id,
                properties = excluded.properties
            """,
            (node_type, node_id, space_id, json.dumps(props)),
        )
        self._conn.commit()
        return props

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        cur.execute(
            "SELECT properties FROM graph_nodes WHERE node_type=? AND node_id=?",
            (node_type, node_id),
        )
        row = cur.fetchone()
        return json.loads(row["properties"]) if row else None

    def lookup_node_type(self, node_id: str) -> str | None:
        """Return the node_type for a node_id, or None if not found.

        Used by OntologyBuilder to resolve real node types when writing edges,
        so that edges preserve typed labels instead of falling back to a single
        per-space default.
        """
        if not self._available or not self._conn:
            return None
        cur = self._conn.cursor()
        cur.execute(
            "SELECT node_type FROM graph_nodes WHERE node_id=? LIMIT 1",
            (node_id,),
        )
        row = cur.fetchone()
        return row["node_type"] if row else None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM graph_nodes WHERE node_type=? AND node_id=?",
            (node_type, node_id),
        )
        cur.execute(
            "DELETE FROM graph_edges WHERE (from_type=? AND from_id=?) OR (to_type=? AND to_id=?)",
            (node_type, node_id, node_type, node_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

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
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO graph_edges(from_type, from_id, relation, to_type, to_id, properties)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_type, from_id, relation, to_type, to_id) DO UPDATE SET
                properties = excluded.properties
            """,
            (from_type, from_id, relation, to_type, to_id, json.dumps(properties or {})),
        )
        self._conn.commit()
        return True

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Not supported in local mode — returns empty list with a warning."""
        logger.warning("run_cypher() is not supported in local mode; returning [].")
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
        """BFS neighbour traversal in Python.

        ``pack_ids``/``include_unpackaged`` enforce the agreed graph filter:
          - Nodes outside ``pack_ids`` (or with no pack_id when
            ``include_unpackaged=False``) are dropped from the result and
            do not contribute to traversal expansion.
          - Edges follow the 3-rule policy: an edge whose own ``pack_id`` is
            foreign is always dropped; an edge without a ``pack_id`` only
            survives when both endpoints satisfy the node filter.
          - Anchor must also satisfy the node filter; otherwise the function
            returns an empty list. Source nodes added during traversal are
            guaranteed to pass (they were enqueued only after passing), so
            edge passes reduce to "edge own pack_id ok AND dst passes".
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")

        pack_set: set[str] | None = set(pack_ids) if pack_ids else None
        cur = self._conn.cursor()

        if pack_set is not None:
            anchor_props = self._fetch_node_props_by_id(cur, node_id)
            if not _node_passes(anchor_props or {}, pack_set, include_unpackaged):
                return []

        visited: set[str] = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        results: list[dict[str, Any]] = []

        while queue and len(results) < limit:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            # Outgoing edges
            if direction in ("out", "both"):
                # [BFS 허브 노드 성능 문제와 2단계 수정]
                #
                # 문제: 외부 while 루프의 `len(results) < limit` 조건은 노드 한 개를
                # 완전히 처리한 뒤에야 평가된다. 기존 코드는 cur.fetchall()로 해당
                # 노드의 모든 엣지를 한꺼번에 메모리에 올린 뒤 내부 for 루프를 끝까지
                # 돌았고, 그 동안 limit 체크가 전혀 일어나지 않았다.
                #
                # 실측 영향 (bench_graph_backends.py, 2026-05-27):
                #   - 20k 노드 (최고차수  98): d1 p50 =  0.37ms
                #   - 43k 노드 (최고차수 615): d1 p50 = 11.86ms  ← 32× 급등
                # "engineer", "persona", "pack" 같은 온톨로지 허브 개념이 43k 시점에서
                # 차수 615에 도달하면서, 내부 루프가 ~1230회 _fetch_node_props SQL을
                # 실행한 뒤에야 results가 50개를 채웠다. 데이터가 10× 늘면 허브 차수도
                # 수천으로 커지므로 방치하면 선형 이상으로 열화된다.
                #
                # 수정 1 — SQL LIMIT (fetchall 행 수 자체를 줄임):
                #   남은 슬롯(remaining = limit - len(results))만큼만 DB에서 가져온다.
                #   차수 615 허브라도 아직 result가 0개면 615행이 아닌 최대 50행을 로드.
                #   outgoing과 incoming 두 방향을 합산하므로 각 방향마다 remaining을
                #   독립적으로 계산해 두 번째 방향에서도 과도한 로드를 막는다.
                #
                # 수정 2 — 내부 루프 break (pack 필터 통과율이 낮을 때 보완):
                #   pack 필터가 엄격하면 fetchall로 가져온 remaining개 중 실제로
                #   results에 추가되는 비율이 낮아질 수 있다. SQL LIMIT은 filtered-out
                #   행 수를 예측할 수 없으므로, for 루프 안에서도 limit 도달 시 즉시 break.
                #   두 guard의 역할:
                #     SQL LIMIT → fetchall I/O·Python 객체 생성 비용 직접 절감
                #     break    → 필터 손실분을 상쇄하는 추가 property 조회 방지
                #
                # 결과 집합의 결정론성:
                #   Neo4j는 내부 인덱스 순서, SQLite는 엣지 삽입(upsert) 순서로
                #   첫 N개를 반환한다. 두 모드 간 Jaccard ≈ 96.5%의 차이는 이 순서
                #   차이에서 기인하며, reranker(RRF+BM25)가 최종 순위를 결정하므로
                #   실검색 품질에 미치는 영향은 제한적이다.
                remaining = limit - len(results)
                if remaining > 0:
                    cur.execute(
                        "SELECT to_type, to_id, relation, properties"
                        " FROM graph_edges WHERE from_id=? LIMIT ?",
                        (current_id, remaining),
                    )
                    for row in cur.fetchall():
                        if len(results) >= limit:
                            break
                        nid = row["to_id"]
                        if nid in visited:
                            continue
                        dst_props = self._fetch_node_props(cur, row["to_type"], nid)
                        if not dst_props:
                            continue
                        if pack_set is not None:
                            dst_pass = _node_passes(dst_props, pack_set, include_unpackaged)
                            if not dst_pass:
                                continue
                            edge_props = self._parse_props(row["properties"])
                            if not _edge_passes(edge_props, True, dst_pass, pack_set):
                                continue
                        visited.add(nid)
                        results.append({
                            "properties": dst_props,
                            "labels": [row["to_type"]],
                            "relation_type": row["relation"],
                            "relationship_types": [row["relation"]],
                            "depth": current_depth + 1,
                        })
                        queue.append((nid, current_depth + 1))

            # Incoming edges
            if direction in ("in", "both"):
                # outgoing과 동일한 2단계 수정. direction="both"일 때 outgoing이
                # limit을 채운 경우에도 외부 while은 다음 반복 시작 시에야 확인하므로,
                # incoming 블록 진입 전에 멈추지 않는다. remaining을 재계산해서 이미
                # 슬롯이 소진된 경우에는 DB 쿼리 자체를 건너뛴다.
                remaining = limit - len(results)
                if remaining > 0:
                    cur.execute(
                        "SELECT from_type, from_id, relation, properties"
                        " FROM graph_edges WHERE to_id=? LIMIT ?",
                        (current_id, remaining),
                    )
                    for row in cur.fetchall():
                        if len(results) >= limit:
                            break
                        nid = row["from_id"]
                        if nid in visited:
                            continue
                        src_props = self._fetch_node_props(cur, row["from_type"], nid)
                        if not src_props:
                            continue
                        if pack_set is not None:
                            src_pass = _node_passes(src_props, pack_set, include_unpackaged)
                            if not src_pass:
                                continue
                            edge_props = self._parse_props(row["properties"])
                            if not _edge_passes(edge_props, src_pass, True, pack_set):
                                continue
                        visited.add(nid)
                        results.append({
                            "properties": src_props,
                            "labels": [row["from_type"]],
                            "relation_type": row["relation"],
                            "relationship_types": [row["relation"]],
                            "depth": current_depth + 1,
                        })
                        queue.append((nid, current_depth + 1))

        return results[:limit]

    def _parse_props(self, raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _fetch_node_props_by_id(
        self, cur: sqlite3.Cursor, node_id: str
    ) -> dict[str, Any] | None:
        cur.execute(
            "SELECT properties FROM graph_nodes WHERE node_id=? LIMIT 1",
            (node_id,),
        )
        row = cur.fetchone()
        return self._parse_props(row["properties"]) if row else None

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        """BFS shortest path between two nodes."""
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")

        cur = self._conn.cursor()
        visited: set[str] = {from_id}
        # Each queue item: (current_id, path_so_far)
        queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(from_id, [])])

        while queue:
            current_id, path = queue.popleft()
            if len(path) >= max_depth * 2:
                continue

            cur.execute(
                "SELECT to_type, to_id, relation FROM graph_edges WHERE from_id=?",
                (current_id,),
            )
            for row in cur.fetchall():
                nid = row["to_id"]
                rel = row["relation"]
                node = self._fetch_node_props(cur, row["to_type"], nid) or {"id": nid}
                new_path = path + [{"node": node, "relation": rel}]

                if nid == to_id:
                    return new_path

                if nid not in visited:
                    visited.add(nid)
                    queue.append((nid, new_path))

        return []

    def count_nodes(self, node_type: str | None = None) -> int:
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        if node_type:
            cur.execute("SELECT COUNT(*) FROM graph_nodes WHERE node_type=?", (node_type,))
        else:
            cur.execute("SELECT COUNT(*) FROM graph_nodes")
        row = cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Extended operations — SQLite-native replacements for Cypher paths
    #
    # Neo4j 모드에서는 Cypher 쿼리로 처리하던 기능들이 LocalGraphStore의
    # run_cypher() no-op 때문에 빈 결과를 반환했다. 아래 메서드들은 각 Cypher
    # 쿼리에 해당하는 SQLite 등가 구현을 제공한다.
    # ------------------------------------------------------------------

    def list_packs(self, min_nodes: int = 1) -> list[dict[str, Any]]:
        """팩 목록 집계 — content_pack_list 도구의 SQLite 네이티브 구현.

        Neo4j 모드에서 실행하던 Cypher:
            MATCH (n:OpenCrabNode)
            WITH n.pack_id AS pack_id, count(n) AS node_count,
                 collect(DISTINCT coalesce(n.source_package_title, n.title, n.name, ''))[0]
            WHERE node_count >= $min_nodes
            RETURN pack_id, node_count, sample_title ORDER BY node_count DESC

        SQLite 접근:
            properties 컬럼의 JSON에서 json_extract로 pack_id를 추출해 GROUP BY.
            sample_title 우선순위:
              1) pack_create anchor 노드 (node_id = 'dataset:{pack_id}') 의 title
              2) 외부 pack 로더가 부여한 source_package_title
              anchor도 source_package_title도 없으면 빈 문자열 — content_pack_list가
              display or pid 로직으로 pack_id를 표시한다.
            idx_nodes_pack 인덱스(DDL에서 생성)가 json_extract 추출 결과를 캐싱해
            전체 스캔 없이 GROUP BY를 수행한다.

        SQLite >= 3.9.0 필요 (json_extract 지원).
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT
                json_extract(properties, '$.pack_id') AS pack_id,
                COUNT(*) AS node_count,
                COALESCE(
                    MAX(CASE
                        WHEN node_id = 'dataset:' || json_extract(properties, '$.pack_id')
                        THEN json_extract(properties, '$.title')
                    END),
                    MAX(json_extract(properties, '$.source_package_title')),
                    ''
                ) AS sample_title
            FROM graph_nodes
            WHERE json_extract(properties, '$.pack_id') IS NOT NULL
            GROUP BY json_extract(properties, '$.pack_id')
            HAVING COUNT(*) >= ?
            ORDER BY COUNT(*) DESC
            """,
            (min_nodes,),
        )
        return [dict(row) for row in cur.fetchall()]

    def find_by_relations(
        self,
        node_id: str,
        relations: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """relation 타입 필터를 포함한 단순 1-홉 이웃 탐색.

        lever_simulate()의 두 Cypher 쿼리를 대체한다:
            MATCH (l {id: $lid})-[r:raises|lowers|stabilizes|optimizes]->(o)
            RETURN properties(o), type(r), labels(o)[0]  LIMIT 20

            MATCH (l {id: $lid})-[:affects]->(c)
            RETURN properties(c), labels(c)[0]  LIMIT 10

        또한 analyse()의 노드 타입 조회(get_node_by_id)와 달리 "엣지 타입이 특정
        집합에 속하는 이웃만" 반환하는 용도로 사용한다.

        find_neighbors()와 달리 BFS/깊이 없이 단순 1-홉만 수행하므로,
        relation 집합이 좁을 때 훨씬 가볍다.

        IN (?, ...) placeholders는 relations 길이에 따라 동적으로 생성한다.
        SQL 인젝션 위험 없음 — 모두 바인딩 변수(?) 사용.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        if not relations:
            return []

        cur = self._conn.cursor()
        placeholders = ",".join("?" * len(relations))
        results: list[dict[str, Any]] = []

        if direction in ("out", "both"):
            cur.execute(
                f"SELECT to_type, to_id, relation FROM graph_edges"
                f" WHERE from_id=? AND relation IN ({placeholders}) LIMIT ?",
                (node_id, *relations, limit),
            )
            for row in cur.fetchall():
                props = self._fetch_node_props(cur, row["to_type"], row["to_id"])
                if props:
                    results.append({
                        "properties": props,
                        "labels": [row["to_type"]],
                        "relation_type": row["relation"],
                    })

        if direction in ("in", "both"):
            remaining = limit - len(results)
            if remaining > 0:
                cur.execute(
                    f"SELECT from_type, from_id, relation FROM graph_edges"
                    f" WHERE to_id=? AND relation IN ({placeholders}) LIMIT ?",
                    (node_id, *relations, remaining),
                )
                for row in cur.fetchall():
                    props = self._fetch_node_props(cur, row["from_type"], row["from_id"])
                    if props:
                        results.append({
                            "properties": props,
                            "labels": [row["from_type"]],
                            "relation_type": row["relation"],
                        })

        return results

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        """id 프로퍼티로 노드를 찾는다 (PRIMARY KEY node_id와 동일, type 불문).

        analyse()의 Cypher 노드 조회를 대체한다:
            MATCH (n {id: $id}) RETURN labels(n)[0] AS lbl, n.space AS space LIMIT 1

        PRIMARY KEY는 (node_type, node_id) 쌍이지만, node_id 컬럼 자체도
        idx_edges_from/to 인덱스와 관계없이 직접 LIMIT 1 조회가 빠르다.
        타입을 모르는 상태에서 id만으로 찾아야 할 때 사용한다.

        반환값: properties dict에 'node_type' 키가 추가된 형태
            {"node_type": "Lever", "space": "...", "id": "...", ...}
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        cur.execute(
            "SELECT node_type, properties FROM graph_nodes WHERE node_id=? LIMIT 1",
            (node_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        props = json.loads(row["properties"])
        props["node_type"] = row["node_type"]
        return props

    def export_nodes(
        self,
        pack_id: str | None = None,
        limit: int = 500_000,
    ) -> list[dict[str, Any]]:
        """노드 전체를 export_neo4j_opencrab_ingest()가 기대하는 row 형식으로 반환.

        Neo4j 모드에서 실행하던 Cypher:
            MATCH (n)
            WHERE $pack_id IS NULL OR n.pack_id = $pack_id OR ...
            RETURN properties(n) AS props, labels(n) AS labels
            LIMIT {node_limit}

        SQLite 접근:
            properties 컬럼(JSON 문자열)을 Python json.loads()로 파싱해 dict로 반환.
            SQL json()/json_array() 함수(SQLite 3.38+ 필요)를 의도적으로 사용하지
            않고 Python-side 파싱으로 대체해 버전 요구사항을 3.9.0+로 유지한다.

            pack_id 필터는 json_extract로 처리한다. idx_nodes_pack 인덱스가 있으므로
            pack_id가 지정된 경우 전체 스캔 없이 처리된다.

        반환 형식: [{"props": dict, "labels": ["NodeType"]}, ...]
            (_normalise_node()이 소비하는 형식과 동일)
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        if pack_id:
            cur.execute(
                """
                SELECT node_type, properties FROM graph_nodes
                WHERE json_extract(properties, '$.pack_id') = ?
                   OR json_extract(properties, '$.source')   = ?
                   OR json_extract(properties, '$.source_id') = ?
                LIMIT ?
                """,
                (pack_id, pack_id, pack_id, limit),
            )
        else:
            cur.execute("SELECT node_type, properties FROM graph_nodes LIMIT ?", (limit,))
        return [
            {"props": json.loads(row["properties"]), "labels": [row["node_type"]]}
            for row in cur.fetchall()
        ]

    def export_edges(
        self,
        pack_id: str | None = None,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        """엣지 전체를 export_neo4j_opencrab_ingest()가 기대하는 row 형식으로 반환.

        Neo4j 모드에서 실행하던 Cypher:
            MATCH (a)-[r]->(b)
            WHERE ($pack_id IS NULL OR a.pack_id=... OR b.pack_id=... OR r.pack_id=...)
            RETURN properties(a), labels(a), properties(b), labels(b), properties(r), type(r)
            LIMIT {edge_limit}

        SQLite 접근:
            graph_edges와 graph_nodes를 JOIN해 양 끝점의 properties까지 함께 가져온다.
            properties 파싱은 Python json.loads()로 수행 — SQL json() 함수 미사용.

            pack_id 필터 조건은 엣지 양쪽 끝점과 엣지 자체를 모두 검사한다(Neo4j
            Cypher와 동일 의미). OR 연결이 많아 인덱스 활용이 어려울 수 있으므로
            pack_id가 없는 전체 내보내기는 스캔으로 처리한다.

        반환 형식: [{"source_props": dict, "source_labels": [...], ...}, ...]
            (_normalise_edge()이 소비하는 형식과 동일)
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        cur = self._conn.cursor()
        if pack_id:
            cur.execute(
                """
                SELECT
                    a.node_type AS _from_type, a.properties AS source_props_json,
                    b.node_type AS _to_type,   b.properties AS target_props_json,
                    e.properties AS rel_props_json, e.relation
                FROM graph_edges e
                JOIN graph_nodes a ON e.from_type=a.node_type AND e.from_id=a.node_id
                JOIN graph_nodes b ON e.to_type=b.node_type   AND e.to_id=b.node_id
                WHERE json_extract(a.properties, '$.pack_id') = ?
                   OR json_extract(a.properties, '$.source')   = ?
                   OR json_extract(b.properties, '$.pack_id') = ?
                   OR json_extract(b.properties, '$.source')   = ?
                   OR json_extract(e.properties, '$.pack_id') = ?
                LIMIT ?
                """,
                (pack_id, pack_id, pack_id, pack_id, pack_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT
                    a.node_type AS _from_type, a.properties AS source_props_json,
                    b.node_type AS _to_type,   b.properties AS target_props_json,
                    e.properties AS rel_props_json, e.relation
                FROM graph_edges e
                JOIN graph_nodes a ON e.from_type=a.node_type AND e.from_id=a.node_id
                JOIN graph_nodes b ON e.to_type=b.node_type   AND e.to_id=b.node_id
                LIMIT ?
                """,
                (limit,),
            )
        return [
            {
                "source_props":  json.loads(row["source_props_json"]),
                "source_labels": [row["_from_type"]],
                "target_props":  json.loads(row["target_props_json"]),
                "target_labels": [row["_to_type"]],
                "rel_props":     json.loads(row["rel_props_json"]),
                "relation":      row["relation"],
            }
            for row in cur.fetchall()
        ]

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]]) -> int:
        """대량 노드 적재 — executemany + 단일 commit으로 per-op 대비 ~3× 빠름.

        per-op upsert_node()는 매 호출마다 commit()을 수행해 NVMe SSD에서도
        호출 수에 비례하는 fsync 오버헤드가 발생한다. 팩 적재처럼 수천 개를 한번에
        넣을 때는 이 메서드로 executemany + 단일 commit을 사용한다.

        기존 upsert_node()는 무변경 — 단건 API 호환성 유지.

        각 node dict: {"node_type": str, "node_id": str, "properties": dict,
                       "space_id": str | None}
        반환: 처리된 노드 수.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        params = [
            (
                n["node_type"],
                n["node_id"],
                n.get("space_id"),
                json.dumps({**n.get("properties", {}), "id": n["node_id"]}),
            )
            for n in nodes
        ]
        cur = self._conn.cursor()
        cur.executemany(
            """
            INSERT INTO graph_nodes(node_type, node_id, space_id, properties)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(node_type, node_id) DO UPDATE SET
                space_id   = excluded.space_id,
                properties = excluded.properties
            """,
            params,
        )
        self._conn.commit()
        return len(params)

    def upsert_edges_batch(self, edges: list[dict[str, Any]]) -> int:
        """대량 엣지 적재 — upsert_nodes_batch()와 동일한 이유로 단일 commit 사용.

        각 edge dict: {"from_type": str, "from_id": str, "relation": str,
                       "to_type": str, "to_id": str, "properties": dict | None}
        반환: 처리된 엣지 수.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalGraphStore is not available.")
        params = [
            (
                e["from_type"],
                e["from_id"],
                e["relation"],
                e["to_type"],
                e["to_id"],
                json.dumps(e.get("properties") or {}),
            )
            for e in edges
        ]
        cur = self._conn.cursor()
        cur.executemany(
            """
            INSERT INTO graph_edges(from_type, from_id, relation, to_type, to_id, properties)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_type, from_id, relation, to_type, to_id) DO UPDATE SET
                properties = excluded.properties
            """,
            params,
        )
        self._conn.commit()
        return len(params)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_node_props(
        self, cur: sqlite3.Cursor, node_type: str, node_id: str
    ) -> dict[str, Any] | None:
        cur.execute(
            "SELECT properties FROM graph_nodes WHERE node_type=? AND node_id=?",
            (node_type, node_id),
        )
        row = cur.fetchone()
        return json.loads(row["properties"]) if row else None
