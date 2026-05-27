"""
graph.db (SQLite) → LadybugDB (.lbug) 마이그레이션 스크립트

사용법:
    python scripts/migrate_graph_to_ladybug.py [--src SRC] [--dst DST] [--dry-run]

기본값:
    SRC = /home/asdf/.openclaw/workspace/data/localcrab/graph.db
    DST = /home/asdf/.openclaw/workspace/data/localcrab/graph.lbug
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time

BATCH = 2000

DEFAULT_SRC = "/home/asdf/.openclaw/workspace/data/localcrab/graph.db"
DEFAULT_DST = "/home/asdf/.openclaw/workspace/data/localcrab/graph.lbug"


def _create_schema(conn) -> None:
    conn.execute(
        """CREATE NODE TABLE OntologyNode(
            node_id   STRING,
            node_type STRING,
            space_id  STRING,
            props     STRING,
            PRIMARY KEY (node_id))"""
    )
    conn.execute(
        """CREATE REL TABLE OntologyEdge(
            FROM OntologyNode TO OntologyNode,
            relation   STRING,
            properties STRING)"""
    )


def _migrate_nodes(src: sqlite3.Connection, lb_conn, dry_run: bool) -> int:
    rows = src.execute(
        "SELECT node_id, node_type, space_id, properties FROM graph_nodes"
    ).fetchall()
    total = len(rows)
    print(f"  노드: {total:,}개 이전 시작")
    if dry_run:
        return total

    t0 = time.perf_counter()
    for i in range(0, total, BATCH):
        batch = rows[i : i + BATCH]
        lb_conn.execute("BEGIN TRANSACTION")
        for node_id, node_type, space_id, props in batch:
            lb_conn.execute(
                "MERGE (n:OntologyNode {node_id: $id}) "
                "ON CREATE SET n.node_type=$t, n.space_id=$s, n.props=$p "
                "ON MATCH  SET n.node_type=$t, n.space_id=$s, n.props=$p",
                {
                    "id": node_id or "",
                    "t": node_type or "",
                    "s": space_id or "",
                    "p": props or "{}",
                },
            )
        lb_conn.execute("COMMIT")
        pct = min(100, (i + len(batch)) * 100 // total)
        print(f"    노드 {i + len(batch):,}/{total:,} ({pct}%)", end="\r")
    elapsed = time.perf_counter() - t0
    print(f"\n  노드 이전 완료: {elapsed:.1f}s")
    return total


def _migrate_edges(src: sqlite3.Connection, lb_conn, dry_run: bool) -> int:
    rows = src.execute(
        "SELECT from_id, to_id, relation, properties FROM graph_edges"
    ).fetchall()
    total = len(rows)
    print(f"  엣지: {total:,}개 이전 시작")
    if dry_run:
        return total

    # 존재하는 노드 ID 캐시 (외래 키 오류 방지)
    r = lb_conn.execute("MATCH (n:OntologyNode) RETURN n.node_id")
    valid_ids: set[str] = set()
    while r.has_next():
        valid_ids.add(r.get_next()[0])

    t0 = time.perf_counter()
    skipped = 0
    for i in range(0, total, BATCH):
        batch = rows[i : i + BATCH]
        lb_conn.execute("BEGIN TRANSACTION")
        for from_id, to_id, relation, props in batch:
            if from_id not in valid_ids or to_id not in valid_ids:
                skipped += 1
                continue
            lb_conn.execute(
                "MATCH (a:OntologyNode {node_id: $f}), (b:OntologyNode {node_id: $t}) "
                "MERGE (a)-[:OntologyEdge {relation: $r, properties: $p}]->(b)",
                {
                    "f": from_id,
                    "t": to_id,
                    "r": relation or "",
                    "p": props or "{}",
                },
            )
        lb_conn.execute("COMMIT")
        pct = min(100, (i + len(batch)) * 100 // total)
        print(f"    엣지 {i + len(batch):,}/{total:,} ({pct}%)", end="\r")
    elapsed = time.perf_counter() - t0
    print(f"\n  엣지 이전 완료: {elapsed:.1f}s  (스킵 {skipped}개)")
    return total - skipped


def run(src_path: str, dst_path: str, dry_run: bool) -> None:
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"소스 DB 없음: {src_path}")

    if dry_run:
        print(f"[dry-run] SRC={src_path}  DST={dst_path}")
        src = sqlite3.connect(src_path)
        n = src.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
        e = src.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        print(f"  노드: {n:,}  엣지: {e:,}")
        return

    if os.path.exists(dst_path):
        backup = dst_path + ".bak"
        os.rename(dst_path, backup)
        print(f"기존 DST 백업: {backup}")

    import ladybug as lb

    src = sqlite3.connect(src_path)
    db = lb.Database(dst_path)
    lb_conn = lb.Connection(db)

    print("스키마 생성 중...")
    _create_schema(lb_conn)

    n_total = _migrate_nodes(src, lb_conn, dry_run=False)
    e_total = _migrate_edges(src, lb_conn, dry_run=False)

    # 카운트 검증
    n_lb = lb_conn.execute("MATCH (n:OntologyNode) RETURN count(n)").get_next()[0]
    e_lb = lb_conn.execute("MATCH ()-[r:OntologyEdge]->() RETURN count(r)").get_next()[0]
    n_sq = src.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    e_sq = src.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]

    print(f"\n검증:")
    print(f"  노드 SQLite={n_sq:,}  LadybugDB={n_lb:,}  {'✓' if n_lb == n_sq else '✗'}")
    print(f"  엣지 SQLite={e_sq:,}  LadybugDB={e_lb:,}  {'✓' if e_lb == e_sq else '△ (스킵 있음)'}")

    if n_lb != n_sq:
        raise AssertionError(f"노드 불일치: LadybugDB={n_lb} vs SQLite={n_sq}")

    size_mb = os.path.getsize(dst_path) / 1024 / 1024
    print(f"\n✓ 덤프 완료: {dst_path}  ({size_mb:.1f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="graph.db → LadybugDB 마이그레이션")
    parser.add_argument("--src", default=DEFAULT_SRC)
    parser.add_argument("--dst", default=DEFAULT_DST)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.src, args.dst, args.dry_run)


if __name__ == "__main__":
    main()
