"""
graph.db (SQLite) → KùzuDB (.kuzu) 마이그레이션 스크립트

배경:
  Ladybug transaction owner와 node/edge 원자적 CAS qualification 전까지
  production graph apply를 열지 않는다. 이 시스템(RPi5 aarch64)의
  CONFIG_PAGE_SIZE_16KB=y 환경에서 구버전(kuzu 0.11.3)의 buffer manager가
  일으키던 madvise 문제는 LadybugDB/ladybug#526→#527로 수정됐지만, 그
  사실만으로 원자적 writer capability가 증명되지는 않는다. 현재 이
  스크립트는 SQLite source만 읽는 dry-run과 disposable fixture 검증만
  제공한다.

사용법:
    python scripts/migrate_graph_to_ladybug.py [--src SRC] [--dst DST] [--dry-run]

기본값:
    SRC = /home/asdf/.openclaw/workspace/data/localcrab/graph.db
    DST = /home/asdf/.openclaw/workspace/data/localcrab/graph.kuzu
"""

from __future__ import annotations

import argparse
import os
import sqlite3

from opencrab.common.graph_identity import GraphMigrationFixtureOnlyError

DEFAULT_SRC = "/home/asdf/.openclaw/workspace/data/localcrab/graph.db"
DEFAULT_DST = "/home/asdf/.openclaw/workspace/data/localcrab/graph.kuzu"


def _create_schema(conn) -> None:
    raise GraphMigrationFixtureOnlyError("graph apply is fixture-only")


def _migrate_nodes(src: sqlite3.Connection, conn, dry_run: bool) -> int:
    raise GraphMigrationFixtureOnlyError("graph apply is fixture-only")


def _migrate_edges(src: sqlite3.Connection, conn, dry_run: bool) -> int:
    raise GraphMigrationFixtureOnlyError("graph apply is fixture-only")


def run(src_path: str, dst_path: str, dry_run: bool) -> None:
    if not dry_run:
        raise GraphMigrationFixtureOnlyError("graph apply is fixture-only")
    # A dry-run remains useful for operators: inspect only the source and do
    # not import Ladybug or create the destination path.
    print(inspect_graph_source(src_path))


def inspect_graph_source(src_path: str) -> dict[str, object]:
    """Inspect the SQLite source without opening a Kùzu target."""
    if not os.path.exists(src_path):
        return {"nodes": 0, "edges": 0, "duplicates": [], "incomplete_endpoints": []}
    conn = sqlite3.connect(f"file:{os.path.abspath(src_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        nodes = [dict(row) for row in conn.execute(
            "SELECT node_id, node_type FROM graph_nodes"
        )]
        edges = [dict(row) for row in conn.execute(
            "SELECT from_id, relation, to_id FROM graph_edges"
        )]
    finally:
        conn.close()
    types: dict[str, set[str]] = {}
    for row in nodes:
        types.setdefault(str(row["node_id"]), set()).add(str(row["node_type"]))
    ids = set(types)
    incomplete = [
        {"edge_key": (row["from_id"], row["relation"], row["to_id"]),
         "missing": [node_id for node_id in (str(row["from_id"]), str(row["to_id"])) if node_id not in ids]}
        for row in edges
        if str(row["from_id"]) not in ids or str(row["to_id"]) not in ids
    ]
    duplicates = [
        {"node_id": node_id, "node_types": sorted(node_types)}
        for node_id, node_types in sorted(types.items()) if len(node_types) > 1
    ]
    return {
        "nodes": len(nodes), "edges": len(edges), "duplicates": duplicates,
        "mapping_required": duplicates, "incomplete_endpoints": incomplete,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="graph.db → KùzuDB 마이그레이션")
    parser.add_argument("--src", default=DEFAULT_SRC)
    parser.add_argument("--dst", default=DEFAULT_DST)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.src, args.dst, args.dry_run)


if __name__ == "__main__":
    main()
