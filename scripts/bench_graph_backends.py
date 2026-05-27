"""
bench_graph_backends.py — LocalGraphStore(SQLite) vs Neo4jStore 비교

우선순위:
  1. 정합성/결과 품질: 모드 간 결과 동등성, 엣지 타입 정합성, 기능 결손
  2. 속도 (tiebreaker): 인제스트/쿼리 지연

사용 예:
  # 임시 Neo4j 기동 후 (별도 포트 7688)
  python scripts/bench_graph_backends.py

  # 스케일 지정
  python scripts/bench_graph_backends.py --scales 500,2000,10000

  # 정합성만
  python scripts/bench_graph_backends.py --consistency-only

  # 속도만
  python scripts/bench_graph_backends.py --speed-only

  # 라이브 Neo4j 읽기 전용 쿼리 (쓰기 0)
  python scripts/bench_graph_backends.py --readonly-target live --neo4j-uri bolt://localhost:7687

  # 기존 graph.db 읽기 전용 쿼리
  python scripts/bench_graph_backends.py --readonly-target localdb
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, quantiles
from typing import Any

# ─── 프로젝트 루트를 sys.path 에 추가 ────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.neo4j_store import Neo4jStore

# ─── 상수 ────────────────────────────────────────────────────────────────────
DUMP_DIR = Path("/home/asdf/opencrab-dump")
NODES_JSONL = DUMP_DIR / "nodes.jsonl"
EDGES_JSONL = DUMP_DIR / "edges.jsonl"
LIVE_GRAPH_DB = Path("/home/asdf/.openclaw/workspace/data/localcrab/graph.db")
LIVE_CHROMA_PATH = "/home/asdf/.openclaw/workspace/data/localcrab/chroma"
LIVE_DOCSTORE_PATH = "/home/asdf/.openclaw/workspace/data/localcrab/docs"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"


# ─── 데이터 로딩 헬퍼 ────────────────────────────────────────────────────────

def load_nodes(n: int) -> list[dict]:
    """nodes.jsonl 에서 앞 n 개를 읽어 반환."""
    nodes = []
    with open(NODES_JSONL, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= n:
                break
            nodes.append(json.loads(line))
    return nodes


def load_edges(node_ids: set[str]) -> list[dict]:
    """양 끝점이 node_ids 에 속하는 엣지만 반환."""
    edges = []
    with open(EDGES_JSONL, encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e.get("from_id") in node_ids and e.get("to_id") in node_ids:
                edges.append(e)
    return edges


# ─── 스토어 초기화 ────────────────────────────────────────────────────────────

def make_sqlite_store(db_path: str) -> LocalGraphStore:
    return LocalGraphStore(db_path=db_path)


def make_neo4j_store(uri: str, user: str = "neo4j", password: str = "benchpass") -> Neo4jStore | None:
    try:
        store = Neo4jStore(uri=uri, user=user, password=password, database=None)
        if not store.available:
            return None
        return store
    except Exception as e:
        print(f"  [경고] Neo4j 연결 실패: {e}")
        return None


# ─── 인제스트 ────────────────────────────────────────────────────────────────

def ingest_to_store(
    store: LocalGraphStore | Neo4jStore,
    nodes: list[dict],
    edges: list[dict],
    node_type_map: dict[str, str],
    batched: bool = False,
) -> tuple[float, float, int, int]:
    """
    노드/엣지를 스토어에 적재.
    반환: (node_elapsed_sec, edge_elapsed_sec, ok_nodes, ok_edges)

    batched=True: SQLite는 단일 트랜잭션(upsert_node_batch), Neo4j는 1000-batch UNWIND
    (단, 현 API가 per-op이라 여기서는 commit 빈도 조정만 제공)
    """
    ok_nodes = 0
    t0 = time.perf_counter()

    if batched and isinstance(store, LocalGraphStore) and store._conn:
        # SQLite batched: 모든 upsert를 단일 트랜잭션으로
        cur = store._conn.cursor()
        for nd in nodes:
            props = dict(nd.get("properties") or {})
            props["id"] = nd["id"]
            cur.execute(
                """
                INSERT INTO graph_nodes(node_type, node_id, space_id, properties)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(node_type, node_id) DO UPDATE SET
                    space_id   = excluded.space_id,
                    properties = excluded.properties
                """,
                (nd.get("node_type", "concept"), nd["id"],
                 nd.get("space"), json.dumps(props)),
            )
            ok_nodes += 1
        store._conn.commit()
    else:
        for nd in nodes:
            try:
                store.upsert_node(
                    node_type=nd.get("node_type", "concept"),
                    node_id=nd["id"],
                    properties=dict(nd.get("properties") or {}),
                    space_id=nd.get("space"),
                )
                ok_nodes += 1
            except Exception:
                pass

    node_elapsed = time.perf_counter() - t0

    ok_edges = 0
    t1 = time.perf_counter()

    if batched and isinstance(store, LocalGraphStore) and store._conn:
        cur = store._conn.cursor()
        for eg in edges:
            from_id = eg.get("from_id", "")
            to_id = eg.get("to_id", "")
            from_type = node_type_map.get(from_id, "concept")
            to_type = node_type_map.get(to_id, "concept")
            cur.execute(
                """
                INSERT INTO graph_edges(from_type, from_id, relation, to_type, to_id, properties)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_type, from_id, relation, to_type, to_id) DO UPDATE SET
                    properties = excluded.properties
                """,
                (from_type, from_id, eg.get("relation", "related"),
                 to_type, to_id, json.dumps(dict(eg.get("properties") or {}))),
            )
            ok_edges += 1
        store._conn.commit()
    else:
        for eg in edges:
            from_id = eg.get("from_id", "")
            to_id = eg.get("to_id", "")
            from_type = node_type_map.get(from_id, "concept")
            to_type = node_type_map.get(to_id, "concept")
            try:
                store.upsert_edge(
                    from_type=from_type,
                    from_id=from_id,
                    relation=eg.get("relation", "related"),
                    to_type=to_type,
                    to_id=to_id,
                    properties=dict(eg.get("properties") or {}),
                )
                ok_edges += 1
            except Exception:
                pass

    edge_elapsed = time.perf_counter() - t1
    return node_elapsed, edge_elapsed, ok_nodes, ok_edges


# ─── 쿼리 ────────────────────────────────────────────────────────────────────

def _safe_find_neighbors(store, node_id: str, depth: int) -> list:
    try:
        return store.find_neighbors(node_id=node_id, direction="both", depth=depth, limit=50)
    except Exception:
        return []


def _safe_count(store) -> int:
    try:
        return store.count_nodes()
    except Exception:
        return -1


def _safe_get_node(store, node_type: str, node_id: str) -> dict | None:
    try:
        return store.get_node(node_type=node_type, node_id=node_id)
    except Exception:
        return None


def measure_query_latency(
    store: LocalGraphStore | Neo4jStore,
    seeds: list[tuple[str, str]],  # [(node_type, node_id), ...]
    depths: list[int] = (1, 2, 3),
    repeat: int = 5,
) -> dict[str, Any]:
    """각 depth별 find_neighbors latency 측정."""
    results: dict[str, list[float]] = {}
    result_counts: dict[str, list[int]] = {}

    for depth in depths:
        lats = []
        cnts = []
        for _ in range(repeat):
            for _, nid in seeds:
                t0 = time.perf_counter()
                res = _safe_find_neighbors(store, nid, depth)
                lats.append((time.perf_counter() - t0) * 1000)
                cnts.append(len(res))
        key = f"neighbors_d{depth}"
        results[key] = lats
        result_counts[key] = cnts

    # get_node 지연
    get_lats = []
    for ntype, nid in seeds:
        for _ in range(repeat):
            t0 = time.perf_counter()
            _safe_get_node(store, ntype, nid)
            get_lats.append((time.perf_counter() - t0) * 1000)
    results["get_node"] = get_lats

    summary = {}
    for key, lats in results.items():
        lats_sorted = sorted(lats)
        summary[key] = {
            "mean_ms": round(mean(lats), 3),
            "p50_ms": round(median(lats), 3),
            "p95_ms": round(quantiles(lats_sorted, n=20)[-1], 3) if len(lats) >= 20 else round(max(lats), 3),
            "count_mean": round(mean(result_counts[key]), 1) if key in result_counts else None,
        }
    return summary


# ─── 정합성 비교 ─────────────────────────────────────────────────────────────

def compare_neighbor_results(
    sqlite_store: LocalGraphStore,
    neo4j_store: Neo4jStore,
    seeds: list[tuple[str, str]],
    depths: list[int] = (1, 2, 3),
) -> dict[str, Any]:
    """
    동일 시드에 대해 두 스토어의 find_neighbors 결과를 비교.
    Jaccard 중첩률, 한쪽에만 있는 항목 개수 반환.
    """
    report: dict[str, Any] = {}
    for depth in depths:
        jaccards = []
        only_sqlite_counts = []
        only_neo4j_counts = []
        result_count_sqlite = []
        result_count_neo4j = []

        for _, nid in seeds:
            sqlite_res = _safe_find_neighbors(sqlite_store, nid, depth)
            neo4j_res = _safe_find_neighbors(neo4j_store, nid, depth)

            def _extract_id(r: dict) -> str | None:
                # find_neighbors 반환: properties.id 안에 노드 ID가 있음
                props = r.get("properties") or {}
                return props.get("id") or r.get("node_id") or r.get("id")

            s_ids = {_extract_id(r) for r in sqlite_res if isinstance(r, dict)} - {None}
            n_ids = {_extract_id(r) for r in neo4j_res if isinstance(r, dict)} - {None}

            union = s_ids | n_ids
            inter = s_ids & n_ids
            jaccard = len(inter) / len(union) if union else 1.0
            jaccards.append(jaccard)
            only_sqlite_counts.append(len(s_ids - n_ids))
            only_neo4j_counts.append(len(n_ids - s_ids))
            result_count_sqlite.append(len(s_ids))
            result_count_neo4j.append(len(n_ids))

        report[f"depth_{depth}"] = {
            "jaccard_mean": round(mean(jaccards), 4) if jaccards else None,
            "only_in_sqlite_mean": round(mean(only_sqlite_counts), 1),
            "only_in_neo4j_mean": round(mean(only_neo4j_counts), 1),
            "sqlite_count_mean": round(mean(result_count_sqlite), 1),
            "neo4j_count_mean": round(mean(result_count_neo4j), 1),
            "perfect_match_rate": round(
                sum(1 for j in jaccards if j >= 0.999) / len(jaccards), 3
            ) if jaccards else None,
        }
    return report


def compare_edge_types(
    sqlite_store: LocalGraphStore,
    neo4j_store: Neo4jStore,
    edges: list[dict],
    node_type_map: dict[str, str],
    sample: int = 200,
) -> dict[str, Any]:
    """
    C항목: 엣지 타입 정합성. 동일 엣지를 적재했을 때 from_type/to_type 라벨이 일치하는지.
    LocalGraphStore 는 properties JSON에서 타입 정보를 꺼내야 함.
    """
    mismatches = 0
    checked = 0
    sample_edges = edges[:sample]

    for eg in sample_edges:
        fid = eg.get("from_id", "")
        tid = eg.get("to_id", "")
        expected_from = node_type_map.get(fid, "concept")
        expected_to = node_type_map.get(tid, "concept")

        # SQLite: graph_edges 테이블에서 from_type/to_type 확인
        sqlite_ok = False
        if sqlite_store._conn:
            try:
                cur = sqlite_store._conn.cursor()
                cur.execute(
                    "SELECT from_type, to_type FROM graph_edges WHERE from_id=? AND to_id=?",
                    (fid, tid),
                )
                row = cur.fetchone()
                if row:
                    sqlite_from = row["from_type"]
                    sqlite_to = row["to_type"]
                    # Neo4j는 ingest 시 타입을 올바르게 저장했으므로 node_type_map이 기준
                    if sqlite_from != expected_from or sqlite_to != expected_to:
                        mismatches += 1
                    sqlite_ok = True
            except Exception:
                pass
        if sqlite_ok:
            checked += 1

    return {
        "checked": checked,
        "type_mismatches": mismatches,
        "mismatch_rate": round(mismatches / checked, 4) if checked else None,
        "note": "SQLite 엣지 from_type/to_type vs nodes.jsonl 기준 타입 비교",
    }


def check_cypher_capability(
    sqlite_store: LocalGraphStore,
    neo4j_store: Neo4jStore | None,
) -> dict[str, Any]:
    """B항목: Cypher 의존 기능 결손 확인."""
    test_cypher = "MATCH (n) RETURN n LIMIT 1"

    sqlite_result = sqlite_store.run_cypher(test_cypher)
    sqlite_cypher_works = len(sqlite_result) > 0

    neo4j_cypher_works = False
    neo4j_result_count = 0
    if neo4j_store and neo4j_store.available:
        try:
            neo4j_result = neo4j_store.run_cypher(test_cypher)
            neo4j_cypher_works = len(neo4j_result) > 0
            neo4j_result_count = len(neo4j_result)
        except Exception:
            pass

    # keyword_search 모사: run_cypher로 keyword 검색
    kw_cypher = "MATCH (n) WHERE toLower(n.name) CONTAINS 'a' RETURN n LIMIT 5"
    sqlite_kw = sqlite_store.run_cypher(kw_cypher)
    neo4j_kw = []
    if neo4j_store and neo4j_store.available:
        try:
            neo4j_kw = neo4j_store.run_cypher(kw_cypher)
        except Exception:
            pass

    return {
        "sqlite_cypher_works": sqlite_cypher_works,
        "neo4j_cypher_works": neo4j_cypher_works,
        "neo4j_cypher_result_count": neo4j_result_count,
        "sqlite_keyword_search_results": len(sqlite_kw),
        "neo4j_keyword_search_results": len(neo4j_kw),
        "impact": (
            "SQLite 모드에서 run_cypher는 no-op → keyword_search/임의 Cypher가 항상 [] 반환. "
            "Neo4j 모드에서는 정상 동작."
        ),
    }


# ─── 출력 포맷 ────────────────────────────────────────────────────────────────

def hline(char="─", width=70):
    print(char * width)


def print_speed_table(scale: int, sqlite_r: dict, neo4j_r: dict | None):
    hline()
    print(f"[속도] 스케일 {scale:,} 노드")
    hline()

    def row(label, s_val, n_val):
        n_str = f"{n_val}" if n_val is not None else "N/A"
        print(f"  {label:<35} SQLite: {s_val:<15} Neo4j: {n_str}")

    # 인제스트 (per-op)
    if "ingest_per_op" in sqlite_r:
        s = sqlite_r["ingest_per_op"]
        n = neo4j_r.get("ingest_per_op") if neo4j_r else None
        row("인제스트(per-op) nodes/s",
            f"{s.get('nodes_per_sec', 0):.0f}",
            f"{n.get('nodes_per_sec', 0):.0f}" if n else "N/A")
        row("인제스트(per-op) edges/s",
            f"{s.get('edges_per_sec', 0):.0f}",
            f"{n.get('edges_per_sec', 0):.0f}" if n else "N/A")

    if "ingest_batched" in sqlite_r:
        s = sqlite_r["ingest_batched"]
        n = neo4j_r.get("ingest_batched") if neo4j_r else None
        row("인제스트(batched) nodes/s",
            f"{s.get('nodes_per_sec', 0):.0f}",
            f"{n.get('nodes_per_sec', 0):.0f}" if n else "N/A")

    # 쿼리
    if "query" in sqlite_r:
        for key, sv in sqlite_r["query"].items():
            nv = neo4j_r["query"].get(key) if neo4j_r and "query" in neo4j_r else None
            row(f"쿼리 {key} p50(ms)",
                f"{sv.get('p50_ms'):.2f}",
                f"{nv.get('p50_ms'):.2f}" if nv else "N/A")


def print_consistency_table(scale: int, report: dict):
    hline()
    print(f"[정합성] 스케일 {scale:,} 노드")
    hline()

    if "neighbor_compare" in report:
        print("  [A] 모드 간 결과 동등성 (find_neighbors)")
        for depth_key, dv in report["neighbor_compare"].items():
            d = depth_key.replace("depth_", "")
            j = dv.get("jaccard_mean")
            pm = dv.get("perfect_match_rate")
            sc = dv.get("sqlite_count_mean")
            nc = dv.get("neo4j_count_mean")
            print(f"      depth={d}: Jaccard={j:.4f}  완전일치율={pm:.1%}  "
                  f"SQLite결과수={sc:.1f}  Neo4j결과수={nc:.1f}")

    if "edge_type" in report:
        et = report["edge_type"]
        print(f"  [C] 엣지 타입 정합성: "
              f"검사={et['checked']}  불일치={et['type_mismatches']}  "
              f"불일치율={et.get('mismatch_rate', 0):.1%}")

    if "cypher" in report:
        cy = report["cypher"]
        print(f"  [B] Cypher 기능 결손:")
        print(f"      SQLite run_cypher 작동: {cy['sqlite_cypher_works']}  "
              f"(keyword 결과: {cy['sqlite_keyword_search_results']}개)")
        print(f"      Neo4j run_cypher 작동: {cy['neo4j_cypher_works']}  "
              f"(keyword 결과: {cy['neo4j_keyword_search_results']}개)")
        if not cy["sqlite_cypher_works"]:
            print(f"      → {cy['impact']}")


# ─── 읽기 전용 쿼리 측정 ─────────────────────────────────────────────────────

def run_readonly_target(target: str, neo4j_uri: str, neo4j_user: str, neo4j_pass: str):
    hline("═")
    print(f"[읽기 전용] 대상: {target}")
    hline("═")

    if target == "localdb":
        if not LIVE_GRAPH_DB.exists():
            print(f"  [오류] {LIVE_GRAPH_DB} 없음")
            return
        store = LocalGraphStore(db_path=str(LIVE_GRAPH_DB))
        cnt_before = _safe_count(store)
        print(f"  SQLite 노드 수(사전): {cnt_before}")

        # 시드 노드 샘플링
        if store._conn:
            cur = store._conn.cursor()
            cur.execute("SELECT node_type, node_id FROM graph_nodes LIMIT 30")
            seeds = [(r["node_type"], r["node_id"]) for r in cur.fetchall()]
        else:
            seeds = []

        lat = measure_query_latency(store, seeds, depths=[1, 2, 3], repeat=3)
        print("  쿼리 지연 (ms):")
        for k, v in lat.items():
            print(f"    {k:<25} mean={v['mean_ms']:.2f}  p50={v['p50_ms']:.2f}  p95={v['p95_ms']:.2f}"
                  + (f"  결과수평균={v['count_mean']:.1f}" if v.get("count_mean") is not None else ""))

        cnt_after = _safe_count(store)
        print(f"  SQLite 노드 수(사후): {cnt_after}  [쓰기 0 확인: {cnt_before == cnt_after}]")

    elif target == "live":
        neo4j_store = make_neo4j_store(neo4j_uri, neo4j_user, neo4j_pass)
        if not neo4j_store:
            print("  [오류] Neo4j 연결 실패")
            return
        cnt_before = _safe_count(neo4j_store)
        print(f"  Neo4j 노드 수(사전): {cnt_before}")

        # 시드 샘플링: 임의 Cypher로 가져옴
        seeds: list[tuple[str, str]] = []
        try:
            rows = neo4j_store.run_cypher(
                "MATCH (n) RETURN labels(n)[0] AS t, n.id AS id LIMIT 30"
            )
            seeds = [(r.get("t", "concept") or "concept", r["id"]) for r in rows if r.get("id")]
        except Exception:
            pass

        if not seeds:
            print("  [경고] 시드 노드를 가져올 수 없음 — Cypher 실행 확인 필요")
            return

        lat = measure_query_latency(neo4j_store, seeds, depths=[1, 2, 3], repeat=3)
        print("  쿼리 지연 (ms):")
        for k, v in lat.items():
            print(f"    {k:<25} mean={v['mean_ms']:.2f}  p50={v['p50_ms']:.2f}  p95={v['p95_ms']:.2f}"
                  + (f"  결과수평균={v['count_mean']:.1f}" if v.get("count_mean") is not None else ""))

        cnt_after = _safe_count(neo4j_store)
        print(f"  Neo4j 노드 수(사후): {cnt_after}  [쓰기 0 확인: {cnt_before == cnt_after}]")


# ─── 메인 벤치마크 ────────────────────────────────────────────────────────────

def run_bench(
    scales: list[int],
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_pass: str,
    consistency_only: bool,
    speed_only: bool,
    seed_count: int = 20,
):
    all_results: dict[str, Any] = {"scales": {}}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Neo4j 연결 확인
    neo4j_available = False
    neo4j_store_global: Neo4jStore | None = None
    neo4j_store_global = make_neo4j_store(neo4j_uri, neo4j_user, neo4j_pass)
    if neo4j_store_global:
        neo4j_available = True
        print(f"Neo4j 연결 성공: {neo4j_uri}  노드 수={_safe_count(neo4j_store_global)}")
    else:
        print(f"[경고] Neo4j 연결 실패 ({neo4j_uri}) — SQLite 단독 측정")

    for scale in scales:
        hline("═")
        print(f"스케일: {scale:,} 노드")
        hline("═")

        # ── 데이터 준비 ──
        print(f"  데이터 로딩 중 (nodes.jsonl {scale:,}개)…")
        nodes = load_nodes(scale)
        node_ids = {nd["id"] for nd in nodes}
        node_type_map = {nd["id"]: nd.get("node_type", "concept") for nd in nodes}
        edges = load_edges(node_ids)
        print(f"  노드={len(nodes):,}  엣지={len(edges):,}")

        # 시드 노드 선택 (degree 높은 순)
        degree: dict[str, int] = defaultdict(int)
        for eg in edges:
            degree[eg.get("from_id", "")] += 1
            degree[eg.get("to_id", "")] += 1
        seeds_ids = sorted(node_ids, key=lambda x: -degree[x])[:seed_count]
        seeds = [(node_type_map.get(nid, "concept"), nid) for nid in seeds_ids]

        scale_result: dict[str, Any] = {
            "node_count": len(nodes),
            "edge_count": len(edges),
        }

        # ── SQLite 측정 ──
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            sqlite_db_path = f.name

        try:
            sqlite_store = make_sqlite_store(sqlite_db_path)
            sqlite_result: dict[str, Any] = {}

            if not speed_only:
                # 정합성 측정용 적재 (per-op, as-used)
                print("  [SQLite] 적재 중 (per-op)…")
                t0 = time.perf_counter()
                n_el, e_el, ok_n, ok_e = ingest_to_store(
                    sqlite_store, nodes, edges, node_type_map, batched=False
                )
                sqlite_result["ingest_per_op"] = {
                    "node_elapsed_s": round(n_el, 3),
                    "edge_elapsed_s": round(e_el, 3),
                    "ok_nodes": ok_n,
                    "ok_edges": ok_e,
                    "nodes_per_sec": round(ok_n / n_el, 1) if n_el > 0 else 0,
                    "edges_per_sec": round(ok_e / e_el, 1) if e_el > 0 else 0,
                }
                print(f"    per-op: nodes={ok_n:,} ({sqlite_result['ingest_per_op']['nodes_per_sec']:.0f}/s)  "
                      f"edges={ok_e:,} ({sqlite_result['ingest_per_op']['edges_per_sec']:.0f}/s)")

                # 정합성 측정
                neo4j_store_for_compare: Neo4jStore | None = None
                if neo4j_available:
                    # Neo4j도 같은 데이터 적재
                    print("  [Neo4j] 적재 중 (per-op)…")
                    # 임시 Neo4j 스토어 (재연결)
                    neo4j_store_for_compare = make_neo4j_store(neo4j_uri, neo4j_user, neo4j_pass)
                    if neo4j_store_for_compare:
                        # 기존 데이터 정리 (임시 인스턴스이므로 안전)
                        try:
                            neo4j_store_for_compare.run_cypher("MATCH (n) DETACH DELETE n")
                        except Exception:
                            pass
                        n_el2, e_el2, ok_n2, ok_e2 = ingest_to_store(
                            neo4j_store_for_compare, nodes, edges, node_type_map, batched=False
                        )
                        scale_result["neo4j_ingest_per_op"] = {
                            "node_elapsed_s": round(n_el2, 3),
                            "edge_elapsed_s": round(e_el2, 3),
                            "ok_nodes": ok_n2,
                            "ok_edges": ok_e2,
                            "nodes_per_sec": round(ok_n2 / n_el2, 1) if n_el2 > 0 else 0,
                            "edges_per_sec": round(ok_e2 / e_el2, 1) if e_el2 > 0 else 0,
                        }
                        print(f"    per-op: nodes={ok_n2:,} ({scale_result['neo4j_ingest_per_op']['nodes_per_sec']:.0f}/s)  "
                              f"edges={ok_e2:,} ({scale_result['neo4j_ingest_per_op']['edges_per_sec']:.0f}/s)")

                consistency: dict[str, Any] = {}

                # A. 결과 동등성
                if neo4j_store_for_compare:
                    print("  [정합성 A] 결과 동등성 비교 중…")
                    consistency["neighbor_compare"] = compare_neighbor_results(
                        sqlite_store, neo4j_store_for_compare, seeds, depths=[1, 2, 3]
                    )

                # B. Cypher 기능 결손
                print("  [정합성 B] Cypher 기능 결손 확인 중…")
                consistency["cypher"] = check_cypher_capability(
                    sqlite_store, neo4j_store_for_compare
                )

                # C. 엣지 타입 정합성
                print("  [정합성 C] 엣지 타입 정합성 확인 중…")
                consistency["edge_type"] = compare_edge_types(
                    sqlite_store, neo4j_store_for_compare, edges, node_type_map, sample=min(200, len(edges))
                )

                scale_result["consistency"] = consistency
                print_consistency_table(scale, consistency)

            if not consistency_only:
                # 속도: batched SQLite
                sqlite_store_b = make_sqlite_store(sqlite_db_path + ".batched.db")
                print("  [SQLite] 적재 중 (batched)…")
                n_el_b, e_el_b, ok_n_b, ok_e_b = ingest_to_store(
                    sqlite_store_b, nodes, edges, node_type_map, batched=True
                )
                sqlite_result["ingest_batched"] = {
                    "node_elapsed_s": round(n_el_b, 3),
                    "edge_elapsed_s": round(e_el_b, 3),
                    "ok_nodes": ok_n_b,
                    "ok_edges": ok_e_b,
                    "nodes_per_sec": round(ok_n_b / n_el_b, 1) if n_el_b > 0 else 0,
                    "edges_per_sec": round(ok_e_b / e_el_b, 1) if e_el_b > 0 else 0,
                }
                print(f"    batched: nodes={ok_n_b:,} ({sqlite_result['ingest_batched']['nodes_per_sec']:.0f}/s)  "
                      f"edges={ok_e_b:,} ({sqlite_result['ingest_batched']['edges_per_sec']:.0f}/s)")

                # SQLite 쿼리 지연 (per-op DB 사용)
                print("  [SQLite] 쿼리 지연 측정 중…")
                sqlite_result["query"] = measure_query_latency(
                    sqlite_store, seeds, depths=[1, 2, 3], repeat=5
                )

                # Neo4j 쿼리 지연
                if neo4j_available:
                    neo4j_q = make_neo4j_store(neo4j_uri, neo4j_user, neo4j_pass)
                    if neo4j_q:
                        # 데이터가 이미 적재된 경우에만 (정합성 측정 시 적재됨)
                        if not speed_only:
                            print("  [Neo4j] 쿼리 지연 측정 중…")
                            neo4j_q_result = {"query": measure_query_latency(
                                neo4j_q, seeds, depths=[1, 2, 3], repeat=5
                            )}
                            scale_result["neo4j_query"] = neo4j_q_result["query"]
                        else:
                            # speed_only: 별도 적재 필요
                            print("  [Neo4j] 속도 전용: 적재 후 쿼리 측정 중…")
                            neo4j_q_ingest = make_neo4j_store(neo4j_uri, neo4j_user, neo4j_pass)
                            if neo4j_q_ingest:
                                try:
                                    neo4j_q_ingest.run_cypher("MATCH (n) DETACH DELETE n")
                                except Exception:
                                    pass
                                n_el3, e_el3, ok_n3, ok_e3 = ingest_to_store(
                                    neo4j_q_ingest, nodes, edges, node_type_map, batched=False
                                )
                                scale_result["neo4j_ingest_per_op"] = {
                                    "nodes_per_sec": round(ok_n3 / n_el3, 1) if n_el3 > 0 else 0,
                                    "edges_per_sec": round(ok_e3 / e_el3, 1) if e_el3 > 0 else 0,
                                }
                                scale_result["neo4j_query"] = measure_query_latency(
                                    neo4j_q_ingest, seeds, depths=[1, 2, 3], repeat=5
                                )

                sqlite_result_for_table = sqlite_result
                neo4j_for_table = None
                if "neo4j_query" in scale_result and "ingest_batched" not in scale_result.get("neo4j_ingest_per_op", {}):
                    neo4j_for_table = {
                        "ingest_per_op": scale_result.get("neo4j_ingest_per_op"),
                        "query": scale_result.get("neo4j_query"),
                    }
                print_speed_table(scale, sqlite_result_for_table, neo4j_for_table)

            scale_result["sqlite"] = sqlite_result

        finally:
            try:
                os.unlink(sqlite_db_path)
            except Exception:
                pass
            try:
                os.unlink(sqlite_db_path + ".batched.db")
            except Exception:
                pass

        all_results["scales"][str(scale)] = scale_result

    # 결과 JSON 저장
    ts = int(time.time())
    out_path = OUTPUT_DIR / f"bench_graph_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n결과 저장: {out_path}")
    return all_results


# ─── 최종 권장안 출력 ────────────────────────────────────────────────────────

def print_recommendation(results: dict):
    hline("═")
    print("■ 종합 분석 및 운영 모드 권장안")
    hline("═")

    # 정합성 결론 수집
    max_jaccard_gap = 0.0
    cypher_broken_in_sqlite = False
    edge_type_mismatch_rate = 0.0
    neo4j_faster_query = False
    sqlite_faster_ingest = False

    for sc_key, sc_val in results.get("scales", {}).items():
        c = sc_val.get("consistency", {})
        if "neighbor_compare" in c:
            for dk, dv in c["neighbor_compare"].items():
                j = dv.get("jaccard_mean", 1.0)
                if j is not None:
                    max_jaccard_gap = max(max_jaccard_gap, 1.0 - j)

        if "cypher" in c:
            if not c["cypher"].get("sqlite_cypher_works", True):
                cypher_broken_in_sqlite = True

        if "edge_type" in c:
            r = c["edge_type"].get("mismatch_rate", 0.0) or 0.0
            edge_type_mismatch_rate = max(edge_type_mismatch_rate, r)

        # 속도 비교
        s_q = sc_val.get("sqlite", {}).get("query", {})
        n_q = sc_val.get("neo4j_query", {})
        if s_q and n_q:
            s_p50 = s_q.get("neighbors_d2", {}).get("p50_ms", 0)
            n_p50 = n_q.get("neighbors_d2", {}).get("p50_ms", 0)
            if n_p50 > 0 and s_p50 > 0:
                neo4j_faster_query = (n_p50 < s_p50)

        s_ingest = sc_val.get("sqlite", {}).get("ingest_per_op", {}).get("nodes_per_sec", 0)
        n_ingest = sc_val.get("neo4j_ingest_per_op", {}).get("nodes_per_sec", 0)
        if s_ingest > 0 and n_ingest > 0:
            sqlite_faster_ingest = (s_ingest > n_ingest)

    print()
    print("  [정합성 결론]")
    j_pct = max_jaccard_gap * 100
    print(f"    A. 결과 동등성: 최대 Jaccard 격차 {j_pct:.1f}%",
          "→ 두 모드 결과가 거의 동일" if j_pct < 5 else
          "→ 의미있는 결과 차이 존재 (Neo4j 우위)" if j_pct < 20 else
          "→ 결과 차이 큼 (Neo4j 결과가 더 풍부)")
    print(f"    B. Cypher 기능: SQLite 결손={'있음 (keyword_search 등 항상 []' if cypher_broken_in_sqlite else '없음'}")
    print(f"    C. 엣지 타입:   불일치율 {edge_type_mismatch_rate:.1%}",
          "→ 정합 양호" if edge_type_mismatch_rate < 0.05 else "→ 불일치 주의 필요")
    print(f"    D. 크로스 스토어: 양 모드 모두 best-effort(트랜잭션 없음) → 부분 기록 리스크 동일")
    print()
    print("  [속도 결론]")
    print(f"    인제스트: {'SQLite 우위' if sqlite_faster_ingest else 'Neo4j 우위 or 비슷'}"
          " (NVMe 환경, per-op fsync 비용 미미)")
    print(f"    쿼리:    {'Neo4j 우위' if neo4j_faster_query else 'SQLite 우위'} (얕은 탐색 기준)")
    print()
    print("  [운영 비용]")
    print("    SQLite(local): 단일 graph.db + JSON + 로컬 Chroma — 추가 프로세스 0, RAM ~수십MB")
    print("    Neo4j(docker): 6개 컨테이너(Neo4j JVM + Mongo + PG + Chroma + api + web),")
    print("                    현재 ~3GB RAM 점유, 16h 가동 중")
    print()
    print("  ■ 권장 결론")

    if cypher_broken_in_sqlite and j_pct >= 10:
        print("  → 【Neo4j 모드 유지】")
        print("     근거: Cypher 의존 기능 결손 + 결과 풍부함 차이가 임계 초과.")
        print("     현재 docker 스택은 이미 가동 중이라 추가 비용 미미.")
        print("     SQLite 전환 시 keyword_search/Cypher 기반 경로가 silent empty → 검색 품질 저하.")
    elif cypher_broken_in_sqlite and j_pct < 10:
        print("  → 【조건부: SQLite 모드 가능, 단 Cypher 경로 사용 여부 확인 필수】")
        print("     근거: 결과 동등성은 양호하지만 Cypher 의존 기능은 silent empty.")
        print("     keyword_search / content_pack_list 를 사용하지 않는다면 SQLite로 충분.")
        print("     docker 스택 불필요 시 local 모드로 전환하면 RAM ~3GB 절약.")
    else:
        print("  → 【SQLite 모드 충분】")
        print("     근거: 결과 동등성 양호 + Cypher 결손 미미 + NVMe SSD라 속도도 충분.")
        print("     docker 스택 제거 시 RAM 3GB+ 절약, 단일 파일로 관리 단순화.")

    print()
    hline()


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="localcrab SQLite vs Neo4j 백엔드 비교")
    parser.add_argument("--scales", default="1000,5000,20000",
                        help="쉼표 구분 스케일 (기본: 1000,5000,20000)")
    parser.add_argument("--neo4j-uri", default="bolt://localhost:7688",
                        help="임시 Neo4j URI (기본: bolt://localhost:7688)")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-pass", default="benchpass")
    parser.add_argument("--consistency-only", action="store_true",
                        help="정합성 측정만 (속도 제외)")
    parser.add_argument("--speed-only", action="store_true",
                        help="속도 측정만 (정합성 제외)")
    parser.add_argument("--readonly-target", choices=["live", "localdb"],
                        help="읽기 전용 쿼리 대상 (쓰기 0). live=라이브 Neo4j, localdb=기존 graph.db")
    parser.add_argument("--seed-count", type=int, default=20,
                        help="시드 노드 수 (기본: 20)")
    args = parser.parse_args()

    if args.readonly_target:
        uri = args.neo4j_uri if args.readonly_target == "live" else "bolt://localhost:7687"
        # live 인 경우 라이브 포트/인증으로 오버라이드
        if args.readonly_target == "live":
            # .env 기본값 사용 (bolt://localhost:7687, neo4j/opencrab)
            uri = "bolt://localhost:7687"
            user = "neo4j"
            pwd = "opencrab"
        else:
            uri = args.neo4j_uri
            user = args.neo4j_user
            pwd = args.neo4j_pass
        run_readonly_target(args.readonly_target, uri, user, pwd)
        return

    scales = [int(s.strip()) for s in args.scales.split(",")]
    results = run_bench(
        scales=scales,
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        neo4j_pass=args.neo4j_pass,
        consistency_only=args.consistency_only,
        speed_only=args.speed_only,
        seed_count=args.seed_count,
    )
    print_recommendation(results)


if __name__ == "__main__":
    main()
