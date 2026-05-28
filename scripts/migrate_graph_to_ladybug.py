"""
graph.db (SQLite) → KùzuDB (.kuzu) 마이그레이션 스크립트

배경:
  LocalGraphStore(SQLite BFS)를 KùzuDB 기반 그래프 스토어로 이전한다.
  이 시스템(RPi5 aarch64)은 CONFIG_PAGE_SIZE_16KB=y 커널을 사용하므로
  KùzuDB의 buffer manager가 4KB madvise를 호출할 때 EINVAL이 발생한다.
  LD_PRELOAD=madv_noop.so 로 워크어라운드한다 (madv_noop.so는 미정렬
  madvise를 noop으로 대체).

사용법:
    LD_PRELOAD=/path/to/madv_noop.so python scripts/migrate_graph_to_ladybug.py
    python scripts/migrate_graph_to_ladybug.py [--src SRC] [--dst DST] [--dry-run]

기본값:
    SRC = /home/asdf/.openclaw/workspace/data/localcrab/graph.db
    DST = /home/asdf/.openclaw/workspace/data/localcrab/graph.kuzu
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import tempfile
import time

BATCH_NODE = 50000   # COPY FROM CSV 한 번에 처리할 노드 수
BATCH_EDGE = 50000   # COPY FROM CSV 한 번에 처리할 엣지 수
BUFFER_POOL = 256 * 1024 * 1024  # 256MB

DEFAULT_SRC = "/home/asdf/.openclaw/workspace/data/localcrab/graph.db"
DEFAULT_DST = "/home/asdf/.openclaw/workspace/data/localcrab/graph.kuzu"


def _check_madv_noop() -> None:
    """LD_PRELOAD madv_noop 없이 실행하면 실패할 가능성을 경고."""
    ld_preload = os.environ.get("LD_PRELOAD", "")
    if "madv_noop" not in ld_preload:
        print(
            "⚠️  경고: LD_PRELOAD에 madv_noop.so가 없습니다.\n"
            "   이 시스템(RPi5 aarch64)은 16KB 페이지 커널을 사용합니다.\n"
            "   실행 전 madv_noop.so를 빌드하고 LD_PRELOAD를 설정하세요:\n"
            "     gcc -shared -fPIC -o madv_noop.so scripts/madv_noop.c -ldl\n"
            "     LD_PRELOAD=$(pwd)/madv_noop.so python scripts/migrate_graph_to_ladybug.py\n"
        )


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


def _migrate_nodes(src: sqlite3.Connection, conn, dry_run: bool) -> int:
    rows = src.execute(
        "SELECT node_id, node_type, space_id, properties FROM graph_nodes"
    ).fetchall()
    total = len(rows)
    print(f"  노드: {total:,}개")
    if dry_run:
        return total

    t0 = time.perf_counter()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline=""
    ) as f:
        csv_path = f.name
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(["node_id", "node_type", "space_id", "props"])
        for node_id, node_type, space_id, props in rows:
            writer.writerow(
                [
                    node_id or "",
                    node_type or "",
                    space_id or "",
                    props or "{}",
                ]
            )

    conn.execute(f"COPY OntologyNode FROM '{csv_path}' (HEADER=true)")
    os.unlink(csv_path)

    elapsed = time.perf_counter() - t0
    n = conn.execute("MATCH (n:OntologyNode) RETURN count(n)").get_next()[0]
    print(f"  노드 완료: {n:,}개 / {elapsed:.1f}s → {n/elapsed:.0f} nodes/s")
    return total


def _migrate_edges(src: sqlite3.Connection, conn, dry_run: bool) -> int:
    rows = src.execute(
        "SELECT from_id, to_id, relation, properties FROM graph_edges"
    ).fetchall()
    total = len(rows)
    print(f"  엣지: {total:,}개")
    if dry_run:
        return total

    # 유효 노드 ID 집합 (orphan edge 방지)
    valid_ids: set[str] = set()
    r = conn.execute("MATCH (n:OntologyNode) RETURN n.node_id")
    while r.has_next():
        valid_ids.add(r.get_next()[0])

    t0 = time.perf_counter()
    skipped = 0
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline=""
    ) as f:
        csv_path = f.name
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(["from", "to", "relation", "properties"])
        for from_id, to_id, relation, props in rows:
            if from_id not in valid_ids or to_id not in valid_ids:
                skipped += 1
                continue
            writer.writerow(
                [
                    from_id,
                    to_id,
                    relation or "",
                    props or "{}",
                ]
            )

    conn.execute(f"COPY OntologyEdge FROM '{csv_path}' (HEADER=true)")
    os.unlink(csv_path)

    elapsed = time.perf_counter() - t0
    e = conn.execute("MATCH ()-[r:OntologyEdge]->() RETURN count(r)").get_next()[0]
    print(
        f"  엣지 완료: {e:,}개 (스킵 {skipped}) / {elapsed:.1f}s → {e/elapsed:.0f} edges/s"
    )
    return total - skipped


def run(src_path: str, dst_path: str, dry_run: bool) -> None:
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"소스 DB 없음: {src_path}")

    src = sqlite3.connect(src_path)
    n_sq = src.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    e_sq = src.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]

    if dry_run:
        print(f"[dry-run] SRC={src_path}  DST={dst_path}")
        print(f"  노드: {n_sq:,}  엣지: {e_sq:,}")
        return

    _check_madv_noop()

    if os.path.exists(dst_path):
        backup = dst_path + ".bak"
        os.rename(dst_path, backup)
        print(f"기존 DST 백업: {backup}")

    import kuzu  # kuzu==0.11.3 (LadybugDB 0.16.1은 16KB 페이지 커널 버그 있음)

    db = kuzu.Database(dst_path, buffer_pool_size=BUFFER_POOL)
    conn = kuzu.Connection(db)

    print("스키마 생성 중...")
    _create_schema(conn)

    _migrate_nodes(src, conn, dry_run=False)
    _migrate_edges(src, conn, dry_run=False)

    # 카운트 검증
    n_kz = conn.execute("MATCH (n:OntologyNode) RETURN count(n)").get_next()[0]
    e_kz = conn.execute("MATCH ()-[r:OntologyEdge]->() RETURN count(r)").get_next()[0]

    print(f"\n검증:")
    print(f"  노드 SQLite={n_sq:,}  KùzuDB={n_kz:,}  {'✓' if n_kz == n_sq else '✗'}")
    print(f"  엣지 SQLite={e_sq:,}  KùzuDB={e_kz:,}  {'✓' if e_kz == e_sq else '✗'}")

    if n_kz != n_sq:
        raise AssertionError(f"노드 불일치: KùzuDB={n_kz} vs SQLite={n_sq}")

    print(f"\n✓ 마이그레이션 완료: {dst_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="graph.db → KùzuDB 마이그레이션")
    parser.add_argument("--src", default=DEFAULT_SRC)
    parser.add_argument("--dst", default=DEFAULT_DST)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.src, args.dst, args.dry_run)


if __name__ == "__main__":
    main()
