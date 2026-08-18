"""
docker 모드 → local 모드 마이그레이션 스크립트.

소스 (docker 모드):
  - Neo4j    : bolt://localhost:7687  (READ ONLY — 절대 쓰기 금지)
  - MongoDB  : localhost:27017
  - HTTP Chroma: localhost:8000
  - PostgreSQL: localhost:5432

목적지 (local 모드):
  - Graph  : LocalGraphStore  (SQLite, graph.db)
  - Doc    : LocalDocStore 또는 LocalSQLDocStore (SQLite, doc_store.db)
  - Vector : ChromaStore PersistentClient (chroma/ 디렉토리)
  - SQL    : SQLStore SQLite (opencrab.db)

실행 예시:
  uv run python scripts/migrate_to_local.py --dry-run
  uv run python scripts/migrate_to_local.py --batch-size 1000 --local-data-dir /data/localcrab
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from typing import Any

# scripts/ 는 패키지가 아니라 sys.path[0] (스크립트 직접 실행 시) 또는 테스트의
# sys.path.insert(0, scripts/) 로 노출된다 -- #151 단일 정본 모듈.
import _migration_tables as mt

# rich 는 pyproject.toml 의존성에 포함돼 있음
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from opencrab.locking import file_lock

console = Console()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLStore 가 만드는 테이블 중 이 스크립트가 복사하지 않는 것이 소스에 있으면
# fail-closed 로 중단한다 (#144 의 users/api_tokens/packs 가 최초 사례). #151 은
# 그 셋을 실제로 이관하므로 현재 스키마에서는 가드가 발동하지 않지만, 아홉 번째
# SQLStore 테이블이 생기면 즉시 잡아내야 하므로 목록은 _migration_tables.py 에서
# 파생한다(``mt.unmigrated_tables``) — 하드코딩 리터럴을 다시 두지 않는다.
# ---------------------------------------------------------------------------


def _pg_table_names(pg_engine: Any) -> set[str]:
    """All base table names in the schema this connection actually resolves to.

    ``current_schema()``, not a literal ``'public'``: a DSN may pin a different
    schema via ``options=-csearch_path%3D...`` (several of this repo's
    PostgreSQL tests do exactly that to isolate themselves), and ``SQLStore``
    then creates its tables *there* while the migration reads them through
    unqualified names. Inspecting only ``public`` would find no auth tables in
    that setup, so the fail-closed guard below would stay silent and the run
    would report success having dropped every user and every issued token --
    precisely the failure the guard exists to prevent. This matches what
    ``SQLStore.table_counts`` already uses.
    """
    from sqlalchemy import text  # type: ignore[import]

    with pg_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
        ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="docker 모드 DB를 local 모드 SQLite/Chroma로 마이그레이션합니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                   help="연결·카운트만 확인하고 실제 쓰기는 하지 않습니다.")
    p.add_argument("--skip-graph",   action="store_true", help="그래프 마이그레이션 건너뜀")
    p.add_argument("--skip-docs",    action="store_true", help="문서 마이그레이션 건너뜀")
    p.add_argument("--skip-vectors", action="store_true", help="벡터 마이그레이션 건너뜀")
    p.add_argument("--skip-sql",     action="store_true", help="SQL 마이그레이션 건너뜀")
    p.add_argument("--batch-size", type=int, default=2000, metavar="N",
                   help="페이지 크기 (노드/엣지/벡터 배치)")
    p.add_argument("--local-data-dir", default=None, metavar="D",
                   help="로컬 데이터 디렉토리 (기본: LOCAL_DATA_DIR 환경변수 또는 ./opencrab_data)")
    p.add_argument("--neo4j-uri",  default="bolt://localhost:7687", metavar="U")
    p.add_argument("--neo4j-user", default="neo4j",     metavar="USER")
    p.add_argument("--neo4j-pass", default="opencrab",  metavar="PASS")
    p.add_argument("--mongo-uri",  default="mongodb://root:opencrab@localhost:27017", metavar="U")
    p.add_argument("--mongo-db",   default="opencrab",  metavar="NAME")
    p.add_argument("--chroma-host", default="localhost", metavar="H")
    p.add_argument("--chroma-port", type=int, default=8000, metavar="P")
    p.add_argument("--chroma-collection", default="opencrab_vectors", metavar="COL")
    p.add_argument("--pg-url",
                   default="postgresql://opencrab:opencrab@localhost:5432/opencrab", metavar="U")
    p.add_argument("--allow-target-only-auth", action="store_true",
                   help="타깃에만 있는 users/api_tokens 행이 있어도 진행합니다. 이 플래그가 "
                        "없으면 중단합니다 -- 소스가 모르는 자격증명이 조용히 계속 유효해지는 "
                        "것을 막습니다 (로컬 바인딩 사용자와 그 토큰은 원래 면제됩니다).")
    p.add_argument("--allow-unmigrated", action="store_true",
                   help="SQLStore 가 만들지만 이 이관이 복사하지 않는 테이블이 소스에 있어도 "
                        "마이그레이션하지 않고 진행합니다 (요약에 제외 목록 표시). "
                        "이 플래그 없이는 중단됩니다.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Step 0 — Pre-flight 연결 확인
# ---------------------------------------------------------------------------

def _pg_sql_counts(pg_engine: Any) -> tuple[dict[str, int], list[str]]:
    """행 수를 세되, 존재하는 테이블만 센다. 반환: (카운트, 없는 테이블 목록).

    테이블별 try/except 로 감싸면 안 된다. PostgreSQL 은 실패한 문장이 트랜잭션 전체를
    중단시키므로, 없는 테이블 하나 때문에 뒤따르는 모든 카운트가 예외가 되어 0 으로
    보고된다 -- pre-#144 스키마에서 목록 앞쪽의 `users` 가 실패하면 실존하는 ontology
    테이블까지 0 이 된다. ``SQLStore.table_counts`` 가 같은 이유로 같은 방식을 쓴다.

    없는 테이블은 카운트 dict 에 넣지 않는다. 마커 값을 섞으면 호출부의 합계와 천단위
    포맷이 깨지고, 0 행과 부재도 구분되지 않는다.
    """
    from sqlalchemy import text  # type: ignore[import]

    existing = _pg_table_names(pg_engine)
    counts: dict[str, int] = {}
    absent: list[str] = []
    with pg_engine.connect() as conn:
        for spec in mt.SQL_TABLE_SPECS:
            if spec.name not in existing:
                absent.append(spec.name)
                continue
            row = conn.execute(text(f"SELECT COUNT(*) FROM {spec.name}")).fetchone()  # noqa: S608
            counts[spec.name] = int(row[0]) if row else 0
    return counts, absent


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    """
    목적: 모든 소스 서비스에 연결하고 데이터 규모를 보고한다.
    소스: Neo4j(READ ONLY), MongoDB, HTTP Chroma, PostgreSQL
    주의: Neo4j는 절대 쓰기 금지. RETURN 1 쿼리만 사용.
    반환: {"neo4j": driver, "mongo_db": db, "chroma_http": client,
           "pg_engine": engine, "counts": {...}}
    연결 실패 시 SystemExit으로 종료 (재시도 없음).
    """
    console.rule("[bold blue]Step 0 — Pre-flight 연결 확인")
    result: dict[str, Any] = {}
    counts: dict[str, Any] = {}
    errors: list[str] = []

    # Neo4j (READ ONLY)
    console.print("  Neo4j 연결 중...", end=" ")
    try:
        from neo4j import GraphDatabase  # type: ignore[import]

        from opencrab.common.neo4j_driver import make_driver
        driver = make_driver(GraphDatabase, args.neo4j_uri, args.neo4j_user, args.neo4j_pass)
        with driver.session() as sess:
            # READ ONLY: RETURN 1 로만 ping
            sess.run("RETURN 1").consume()
            node_count = sess.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            edge_count = sess.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        counts["neo4j_nodes"] = node_count
        counts["neo4j_edges"] = edge_count
        result["neo4j_driver"] = driver
        console.print(f"[green]OK[/green] (nodes={node_count:,}, edges={edge_count:,})")
    except Exception as exc:
        console.print(f"[red]FAIL[/red]: {exc}")
        errors.append(f"Neo4j: {exc}")

    # MongoDB
    console.print("  MongoDB 연결 중...", end=" ")
    try:
        from pymongo import MongoClient  # type: ignore[import]
        client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client[args.mongo_db]
        counts["mongo_nodes"]  = db["nodes"].count_documents({})
        counts["mongo_sources"] = db["sources"].count_documents({})
        counts["mongo_audit"]  = db["audit_log"].count_documents({})
        result["mongo_db"] = db
        console.print(
            f"[green]OK[/green] (nodes={counts['mongo_nodes']:,}, "
            f"sources={counts['mongo_sources']:,}, audit={counts['mongo_audit']:,})"
        )
    except Exception as exc:
        console.print(f"[red]FAIL[/red]: {exc}")
        errors.append(f"MongoDB: {exc}")

    # HTTP Chroma
    console.print("  Chroma (HTTP) 연결 중...", end=" ")
    try:
        import chromadb  # type: ignore[import]
        http_client = chromadb.HttpClient(host=args.chroma_host, port=args.chroma_port)
        http_client.heartbeat()
        try:
            col = http_client.get_collection(args.chroma_collection)
            counts["chroma_vectors"] = col.count()
        except Exception:
            counts["chroma_vectors"] = 0
        result["chroma_http"] = http_client
        console.print(f"[green]OK[/green] (vectors={counts['chroma_vectors']:,})")
    except Exception as exc:
        console.print(f"[red]FAIL[/red]: {exc}")
        errors.append(f"Chroma HTTP: {exc}")

    # PostgreSQL
    console.print("  PostgreSQL 연결 중...", end=" ")
    try:
        from sqlalchemy import create_engine, text  # type: ignore[import]
        pg_engine = create_engine(
            args.pg_url, connect_args={"connect_timeout": 5}, hide_parameters=True
        )
        with pg_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        sql_counts, absent_tables = _pg_sql_counts(pg_engine)
        counts["pg_tables"] = sql_counts
        counts["pg_absent_tables"] = absent_tables
        result["pg_engine"] = pg_engine
        total_pg = sum(sql_counts.values())
        console.print(f"[green]OK[/green] (total rows={total_pg:,})")
        if absent_tables:
            console.print(f"  소스에 없는 테이블: {absent_tables}")

        if not args.skip_sql:
            excluded_auth_tables = mt.unmigrated_tables(_pg_table_names(pg_engine))
            if excluded_auth_tables and args.allow_unmigrated:
                console.print(
                    f"  [yellow]--allow-unmigrated[/yellow]: 이관 대상이 아닌 SQLStore 테이블 "
                    f"{excluded_auth_tables} 제외하고 진행합니다"
                )
                result["excluded_auth_tables"] = excluded_auth_tables
            elif excluded_auth_tables:
                console.print(
                    f"  [red]FAIL[/red]: 소스에 이 스크립트가 복사하지 않는 SQLStore 테이블 "
                    f"{excluded_auth_tables} 이(가) 있습니다"
                )
                errors.append(
                    f"PostgreSQL: SQLStore table(s) {excluded_auth_tables} present but not "
                    "migrated; pass --allow-unmigrated to proceed anyway — completing without "
                    "them silently drops that data while still reporting success"
                )
    except Exception as exc:
        # 연결 진단은 행 값을 담지 않으므로 default-deny 대상이 아니다 (#151 7절 예외).
        console.print(f"[red]FAIL[/red]: {exc}")
        errors.append(f"PostgreSQL: {exc}")

    if errors:
        console.print(f"\n[bold red]연결 실패 ({len(errors)}건):[/bold red]")
        for e in errors:
            console.print(f"  - {e}")
        console.print("\n[red]마이그레이션을 중단합니다. 소스 서비스를 확인하세요.[/red]")
        sys.exit(1)

    result["counts"] = counts
    return result


# ---------------------------------------------------------------------------
# Step 1 — 기존 로컬 데이터 백업
# ---------------------------------------------------------------------------

def backup_local_data(local_data_dir: str) -> dict[str, str]:
    """
    목적: 기존 로컬 파일을 덮어쓰기 전에 타임스탬프 접미사 백업 파일로 복사한다.
    소스 → 대상:
      graph.db      → graph.db.bak.{ts}
      doc_store.db  → doc_store.db.bak.{ts}  (있으면)
      chroma/       → chroma.bak.{ts}/        (있으면)
      opencrab.db   → opencrab.db.bak.{ts}    (있으면)
      billing.db    → billing.db.bak.{ts}     (있으면 — issue #105: billing_events
                                                 는 opencrab.db 가 아닌 이 파일에 있다)
    주의: shutil.copy2/copytree 사용. 없으면 경고만 출력하고 스킵.
    반환: {원본경로: 백업경로} (백업된 항목만)
    """
    console.rule("[bold blue]Step 1 — 기존 로컬 데이터 백업")
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backed_up: dict[str, str] = {}

    targets = [
        ("graph.db",     "file"),
        ("doc_store.db", "file"),
        ("chroma",       "dir"),
        ("opencrab.db",  "file"),
        # issue #105: billing_events moved out of opencrab.db into its own
        # file so it stops contending with write.lock'd writers for
        # opencrab.db's SQLite file lock. It must be backed up separately or
        # a split that was meant to prevent billing data loss would cause a
        # bigger one (unbacked-up billing history).
        ("billing.db",   "file"),
    ]

    for name, kind in targets:
        src = os.path.join(local_data_dir, name)
        if kind == "file":
            bak_name = f"{name}.bak.{ts}"
        else:
            bak_name = f"{name}.bak.{ts}"
        dst = os.path.join(local_data_dir, bak_name)

        if kind == "file" and os.path.isfile(src):
            shutil.copy2(src, dst)
            # WAL 모드에서 생성되는 -wal, -shm 파일도 함께 백업
            for suffix in ["-wal", "-shm"]:
                extra_src = src + suffix
                if os.path.isfile(extra_src):
                    shutil.copy2(extra_src, dst + suffix)
            console.print(f"  [green]백업[/green] {name} → {bak_name}")
            backed_up[src] = dst
        elif kind == "dir" and os.path.isdir(src):
            shutil.copytree(src, dst)
            console.print(f"  [green]백업[/green] {name}/ → {bak_name}/")
            backed_up[src] = dst
        else:
            console.print(f"  [yellow]없음, 스킵[/yellow] {name}")

    return backed_up


# ---------------------------------------------------------------------------
# Step 2 — 그래프 마이그레이션 (Neo4j → LocalGraphStore)
# ---------------------------------------------------------------------------

def _extract_node_type(labels: list[str]) -> str:
    """
    labels 리스트에서 'OpenCrabNode'를 제거하고 첫 번째 나머지 레이블을 반환한다.
    레이블이 없으면 'Unknown' 반환.
    """
    filtered = [lbl for lbl in labels if lbl != "OpenCrabNode"]
    return filtered[0] if filtered else "Unknown"


def migrate_graph(
    neo4j_driver: Any,
    local_store: Any,
    batch_size: int,
    log: logging.Logger,
) -> dict[str, int]:
    """
    목적: Neo4j의 모든 노드/엣지를 LocalGraphStore(SQLite)로 복사한다.
    소스: Neo4j (READ ONLY — MATCH + RETURN 쿼리만 사용)
    대상: LocalGraphStore.upsert_nodes_batch(), upsert_edges_batch()
    주의:
      - Neo4j에 절대 쓰기 금지 (MATCH … RETURN 쿼리만 실행)
      - id 없는 노드/엣지는 skip + 경고
      - labels에서 'OpenCrabNode' 제거 → 첫 번째 나머지 = node_type
      - SKIP/LIMIT 페이징으로 메모리 상한 유지
    반환: {"nodes": N, "edges": M}
    """
    total_nodes = 0
    total_edges = 0

    # --- 노드 마이그레이션 ---
    console.print("  [cyan]노드 마이그레이션...[/cyan]")
    skip = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed} nodes"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("  노드", total=None)
        while True:
            with neo4j_driver.session() as sess:
                rows = sess.run(
                    "MATCH (n) RETURN properties(n) AS props, labels(n) AS labels"
                    " SKIP $skip LIMIT $batch_size",
                    skip=skip,
                    batch_size=batch_size,
                ).data()

            if not rows:
                break

            batch: list[dict[str, Any]] = []
            for row in rows:
                props = dict(row.get("props") or {})
                labels = list(row.get("labels") or [])
                node_id = props.get("id")
                if not node_id:
                    log.warning("id 없는 노드 스킵 (labels=%s props_keys=%s)", labels, list(props.keys()))
                    continue
                node_type = _extract_node_type(labels)
                space = props.get("space", "")
                batch.append({
                    "node_type": node_type,
                    "node_id": str(node_id),
                    "space_id": str(space) if space else None,
                    "properties": props,
                })

            if batch:
                local_store.upsert_nodes_batch(batch)
                total_nodes += len(batch)
                progress.update(task, completed=total_nodes)

            skip += batch_size
            if len(rows) < batch_size:
                break

    console.print(f"  노드 완료: {total_nodes:,}개")

    # --- 엣지 마이그레이션 ---
    console.print("  [cyan]엣지 마이그레이션...[/cyan]")
    skip = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed} edges"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("  엣지", total=None)
        while True:
            with neo4j_driver.session() as sess:
                rows = sess.run(
                    """
                    MATCH (a)-[r]->(b)
                    RETURN properties(a).id AS from_id, labels(a) AS from_labels,
                           type(r) AS relation, properties(r) AS rel_props,
                           properties(b).id AS to_id, labels(b) AS to_labels
                    SKIP $skip LIMIT $batch_size
                    """,
                    skip=skip,
                    batch_size=batch_size,
                ).data()

            if not rows:
                break

            batch_edges: list[dict[str, Any]] = []
            for row in rows:
                from_id = row.get("from_id")
                to_id   = row.get("to_id")
                if not from_id or not to_id:
                    log.warning(
                        "from_id 또는 to_id 없는 엣지 스킵 (relation=%s)", row.get("relation")
                    )
                    continue
                from_type = _extract_node_type(list(row.get("from_labels") or []))
                to_type   = _extract_node_type(list(row.get("to_labels") or []))
                batch_edges.append({
                    "from_type":  from_type,
                    "from_id":    str(from_id),
                    "relation":   str(row.get("relation", "")),
                    "to_type":    to_type,
                    "to_id":      str(to_id),
                    "properties": dict(row.get("rel_props") or {}),
                })

            if batch_edges:
                local_store.upsert_edges_batch(batch_edges)
                total_edges += len(batch_edges)
                progress.update(task, completed=total_edges)

            skip += batch_size
            if len(rows) < batch_size:
                break

    console.print(f"  엣지 완료: {total_edges:,}개")
    return {"nodes": total_nodes, "edges": total_edges}


# ---------------------------------------------------------------------------
# Step 3 — 문서 마이그레이션 (MongoDB → LocalDocStore / LocalSQLDocStore)
# ---------------------------------------------------------------------------

def migrate_docs(
    mongo_db: Any,
    sql_doc_store: Any,
    batch_size: int,
    log: logging.Logger,
) -> dict[str, int]:
    """
    목적: MongoDB의 nodes / sources / audit_log 컬렉션을 로컬 doc store로 복사한다.
    소스: MongoDB db["nodes"], db["sources"], db["audit_log"]
    대상: LocalDocStore 또는 LocalSQLDocStore
          .upsert_node_doc(space, node_type, node_id, properties)
          .upsert_source(source_id, text, metadata)
          .log_event(event_type, subject_id, details)
    주의:
      - _id 필드는 JSON 직렬화 불가능하므로 제외
      - node_id 없는 문서는 skip + 경고
    반환: {"nodes": N, "sources": M, "audit_events": K}
    """
    total_nodes = 0
    total_sources = 0
    total_audit = 0

    # nodes 컬렉션
    console.print("  [cyan]nodes 컬렉션 마이그레이션...[/cyan]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  TextColumn("{task.completed} docs"), console=console) as progress:
        task = progress.add_task("  nodes", total=None)
        for doc in mongo_db["nodes"].find({}, {"_id": 0}):
            node_id = doc.get("node_id")
            if not node_id:
                log.warning("node_id 없는 MongoDB 문서 스킵: %s", list(doc.keys()))
                continue
            sql_doc_store.upsert_node_doc(
                doc.get("space", ""),
                doc.get("node_type", ""),
                node_id,
                doc.get("properties", {}),
            )
            total_nodes += 1
            progress.update(task, completed=total_nodes)
    console.print(f"  nodes 완료: {total_nodes:,}개")

    # sources 컬렉션
    console.print("  [cyan]sources 컬렉션 마이그레이션...[/cyan]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  TextColumn("{task.completed} docs"), console=console) as progress:
        task = progress.add_task("  sources", total=None)
        for doc in mongo_db["sources"].find({}, {"_id": 0}):
            source_id = doc.get("source_id")
            if not source_id:
                log.warning("source_id 없는 MongoDB 문서 스킵")
                continue
            sql_doc_store.upsert_source(
                source_id,
                doc.get("text", ""),
                doc.get("metadata", {}),
            )
            total_sources += 1
            progress.update(task, completed=total_sources)
    console.print(f"  sources 완료: {total_sources:,}개")

    # audit_log 컬렉션 (타임스탬프 순 정렬)
    console.print("  [cyan]audit_log 컬렉션 마이그레이션...[/cyan]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  TextColumn("{task.completed} events"), console=console) as progress:
        task = progress.add_task("  audit_log", total=None)
        for doc in mongo_db["audit_log"].find({}, {"_id": 0}).sort("timestamp", 1):
            sql_doc_store.log_event(
                doc.get("event_type", "unknown"),
                doc.get("subject_id"),
                doc.get("details", {}),
            )
            total_audit += 1
            progress.update(task, completed=total_audit)
    console.print(f"  audit_log 완료: {total_audit:,}개")

    return {"nodes": total_nodes, "sources": total_sources, "audit_events": total_audit}


# ---------------------------------------------------------------------------
# Step 4 — 벡터 마이그레이션 (HTTP Chroma → local Chroma)
# ---------------------------------------------------------------------------

def migrate_vectors(
    http_client: Any,
    local_client: Any,
    collection_name: str,
    batch_size: int,
    log: logging.Logger,
) -> dict[str, int]:
    """
    목적: HTTP Chroma 컬렉션의 벡터를 임베딩 재계산 없이 로컬 Chroma로 복사한다.
    소스: http_client.get_collection(collection_name) — embeddings/documents/metadatas 포함
    대상: local_client.get_or_create_collection(collection_name)
    주의:
      - include=["embeddings","documents","metadatas"] 로 원본 임베딩 그대로 복사
      - offset/limit 페이징으로 메모리 상한 유지
      - 소스 컬렉션이 없으면 경고 후 0 반환
    반환: {"vectors": N}
    """
    total_vectors = 0

    try:
        http_col = http_client.get_collection(collection_name)
    except Exception as exc:
        log.warning("Chroma 소스 컬렉션 '%s' 없음: %s", collection_name, exc)
        console.print(f"  [yellow]Chroma 소스 컬렉션 없음, 스킵: {exc}[/yellow]")
        return {"vectors": 0}

    local_col = local_client.get_or_create_collection(
        collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    total = http_col.count()
    console.print(f"  소스 벡터 수: {total:,}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} vectors"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("  벡터 복사", total=total)
        for offset in range(0, total, batch_size):
            result = http_col.get(
                limit=batch_size,
                offset=offset,
                include=["embeddings", "documents", "metadatas"],
            )
            ids = result.get("ids", [])
            if not ids:
                break
            local_col.add(
                ids=ids,
                embeddings=result.get("embeddings"),
                documents=result.get("documents"),
                metadatas=result.get("metadatas"),
            )
            total_vectors += len(ids)
            progress.update(task, completed=total_vectors)

    console.print(f"  벡터 완료: {total_vectors:,}개")
    return {"vectors": total_vectors}


# ---------------------------------------------------------------------------
# Step 5 — SQL 마이그레이션 (PostgreSQL → SQLite)
# ---------------------------------------------------------------------------

def _target_only_auth(pg_engine: Any, sqlite_path: str) -> dict[str, list[str]]:
    """로컬 타깃에만 있는 자격증명 -- 이 설치 자신의 신원은 뺀다.

    로컬 모드는 바인딩할 ``is_local`` 사용자가 정확히 하나 있어야 하므로, 그 사용자와
    그가 가진 토큰은 소스가 미처 챙기지 못한 남의 자격증명이 아니라 이 설치의 신원이다.
    ``opencrab init`` 이 둘을 함께 만들기 때문에 토큰도 함께 면제한다.

    정방향에는 이 면제가 없다. 그쪽 타깃은 PG 배포본이지 소스 설치의 신원 보관소가
    아니어서, 거기 놓인 ``is_local`` 사용자야말로 막아야 할 자격증명이다.
    """
    from sqlalchemy import inspect, text  # type: ignore[import]

    conn = sqlite3.connect(sqlite_path)
    try:
        # 무엇이 있는지부터 센다. 가드가 SQLStore 의 ensure-schema 보다 먼저 돌므로
        # pre-#144 로컬 DB 에는 users 도 api_tokens 도 없을 수 있고, 존재를 가정하고
        # SELECT 하면 바로 그 오래된 DB 에서 백업 이전에 죽는다 -- absent 처리가
        # 지원하려던 대상이 정확히 그 DB 다.
        target_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        local_user, local_tokens = mt.local_identity(conn, target_tables)
        exempt = {"users": {local_user} if local_user else set(), "api_tokens": local_tokens}
        insp = inspect(pg_engine)
        found: dict[str, list[str]] = {}
        for table in mt.AUTH_CREDENTIAL_TABLES:
            if table not in target_tables:
                continue
            key = mt.SPEC_BY_NAME[table].conflict_key
            assert key is not None
            col = ", ".join(key)
            src_keys: set[tuple] = set()
            if insp.has_table(table):
                with pg_engine.connect() as pg_conn:
                    src_keys = {
                        tuple(r) for r in pg_conn.execute(text(f"SELECT {col} FROM {table}"))  # noqa: S608
                    }
            dst_keys = {tuple(r) for r in conn.execute(f"SELECT {col} FROM {table}")}  # noqa: S608
            extra = {k for k in dst_keys - src_keys if k[0] not in exempt[table]}
            if extra:
                found[table] = [",".join(str(v) for v in k) for k in extra]
        return found
    finally:
        conn.close()


def migrate_sql(
    pg_url: str,
    sqlite_path: str,
    log: logging.Logger,
    allow_target_only_auth: bool = False,
) -> dict[str, Any]:
    """
    목적: PostgreSQL의 SQL-store 테이블 행을 SQLite로 복사한다.
    소스/순서: ``_migration_tables.SQL_TABLE_SPECS`` (users 가 FK 참조 대상이라 먼저).
               #151 이전에는 5테이블 리터럴이 이 함수와 ``preflight()`` 양쪽에 따로
               하드코딩돼 있었다 -- 이제 단일 정본에서 파생한다.
    대상: SQLite (opencrab.db) — SQLStore 생성자가 스키마만 만들고, INSERT 는 별도
          write engine 을 쓴다(#151 7-4: private ``sql_store._engine`` 접근 제거).
    주의:
      - 복사 컬럼은 테이블당 PG/SQLite 카탈로그에서 1회만 파생한다(``mt.resolve_columns``).
      - boolean/timestamp 는 PG 카탈로그가 정본이며, 값은 행 단위로 변환한다. PG
        BOOLEAN 은 타입이 도메인을 강제하므로(허용값은 psycopg2 가 bool/None 만 반환)
        정방향과 달리 별도 오염 전수 스캔은 두지 않는다 — 스캔이 있어도 절대 걸리지
        않는 죽은 코드가 된다(#151 설계 4절).
      - 스키마 검증(``mt.resolve_columns``)과 값 변환(``mt.to_sqlite_bool`` /
        ``mt.to_sqlite_timestamp``) 실패는 :class:`_migration_tables.MigrationError` 로
        테이블 단위 try/except 를 통과해 전체 실행을 중단시킨다 — 예전처럼 조용히
        ``count=0`` 으로 삼키지 않는다. 개별 행의 INSERT 실패(제약 위반 등)만 경고로
        남기고 계속한다.
    반환: {"tables": {table_name: {"copied": int, "source": int|None, "target": int|None}}}
          ``source`` 는 PG 원본 행 수, ``target`` 은 복사 후 다시 센 SQLite 행 수.
          소스에 테이블이 없으면 ``source``/``target`` 모두 ``None`` (absent, 복사 단계에
          진입하지 않는다).
    """
    from sqlalchemy import create_engine, inspect, text  # type: ignore[import]

    # SQLite 스키마 초기화 (SQLStore 생성자가 처리) — 이 store 는 스키마 생성용으로만
    # 쓰고, 쓰기는 아래 별도 engine 으로 한다.
    from opencrab.stores.sql_store import SQLStore
    sql_store = SQLStore(url=f"sqlite:///{sqlite_path}")
    if not sql_store.available:
        raise mt.MigrationError(f"SQLite 초기화 실패: {sqlite_path}")

    pg_engine = create_engine(pg_url, connect_args={"connect_timeout": 5}, hide_parameters=True)
    # sql_store.py 가 SQLite 커넥션에 두던 timeout 핀을 승계한다 -- write.lock 이
    # 걸린 동안 다른 프로세스의 파일 잠금 대기가 무한정 걸리지 않게 하기 위함.
    sq_engine = create_engine(
        f"sqlite:///{sqlite_path}", hide_parameters=True, connect_args={"timeout": 5.0}
    )

    # 복사 전에 검사한다. 다 쓴 뒤에 중단하면 종료 코드만 바뀌고 타깃은 이미 오염된다.
    if not allow_target_only_auth:
        target_only = _target_only_auth(pg_engine, sqlite_path)
        if target_only:
            raise mt.TargetOnlyAuthError(mt.target_only_report(target_only))

    table_results: dict[str, dict[str, Any]] = {}

    # 스키마 검증은 첫 쓰기 이전에 전 테이블에 대해 한 번에 끝낸다. 테이블마다 검증하고
    # 바로 복사하면, 뒤쪽 테이블의 구스키마(예: owner_id 없는 packs)가 users 와
    # api_tokens 를 이미 커밋한 뒤에야 걸려 인증 데이터가 반쯤 갱신된 채로 남는다.
    # 정방향이 이미 같은 이유로 전수 사전검증을 한다.
    resolved: dict[str, tuple[list[str], set[str], set[str]]] = {}
    present: list[mt.SqlTableSpec] = []
    for spec in mt.SQL_TABLE_SPECS:
        if not inspect(pg_engine).has_table(spec.name):
            continue
        pg_columns, pg_bool_cols, pg_ts_cols = mt.pg_typed_columns(pg_engine, spec.name)
        sq_raw = sq_engine.raw_connection()
        try:
            sqlite_cols = mt.sqlite_columns(sq_raw, spec.name)
        finally:
            sq_raw.close()
        cols = mt.resolve_columns(spec, pg_columns, sqlite_cols)
        resolved[spec.name] = (cols, pg_bool_cols & set(cols), pg_ts_cols & set(cols))
        present.append(spec)

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  TextColumn("{task.completed} rows"), console=console) as progress:
        for spec in mt.SQL_TABLE_SPECS:
            tbl = spec.name
            task = progress.add_task(f"  {tbl}", total=None)

            if spec not in present:
                log.warning("소스 테이블 '%s' 없음, 스킵 (absent)", tbl)
                console.print(f"  [yellow]{tbl}[/yellow]: 소스에 없음 (absent)")
                table_results[tbl] = {"copied": 0, "source": None, "target": None}
                continue

            col_names, bool_cols, ts_cols = resolved[tbl]

            cols_sql = ", ".join(col_names)
            placeholders = ", ".join(f":{c}" for c in col_names)
            # 자연 키가 있으면 업서트한다. INSERT OR IGNORE 는 키가 이미 있을 때 기존 로컬
            # 행을 남기는데, 이 스크립트는 병합이 아니라 이관이므로 소스가 정본이다.
            # 남겨 두면 PG 에서 폐기한 토큰의 로컬 행이 revoked_at NULL 로 살아남아
            # verify_token() 이 계속 통과시킨다(opencrab/auth.py 의 revoked_at IS NULL
            # AND disabled 조건). 정방향도 같은 이유로 업서트한다.
            # 자연 키가 없는 impact_records/lever_simulations 는 갱신할 대상을 지목할 수
            # 없으므로 OR IGNORE 를 유지한다 -- 재실행 중복을 막는 유일한 수단이다.
            if spec.conflict_key:
                updates = ", ".join(
                    f"{c} = excluded.{c}" for c in col_names if c not in spec.conflict_key
                )
                insert_sql = (
                    f"INSERT INTO {tbl} ({cols_sql}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({', '.join(spec.conflict_key)}) DO UPDATE SET {updates}"
                )
            else:
                insert_sql = f"INSERT OR IGNORE INTO {tbl} ({cols_sql}) VALUES ({placeholders})"

            with pg_engine.connect() as pg_conn:
                source_row = pg_conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).fetchone()  # noqa: S608
                source_count = int(source_row[0]) if source_row else 0
                rows = pg_conn.execute(text(f"SELECT {cols_sql} FROM {tbl}")).fetchall()  # noqa: S608

            count = 0
            failed_rows = 0
            source_keys: set[tuple] = set()
            with sq_engine.begin() as sq_conn:
                for row in rows:
                    row_dict = dict(zip(col_names, row))
                    # conflict_key 로 선언된 자연 키만 로그에 남긴다 -- token_hash 같은
                    # 비밀은 어느 테이블의 conflict_key 도 아니므로 여기 오지 않는다.
                    key = (
                        ",".join(str(row_dict.get(k)) for k in spec.conflict_key)
                        if spec.conflict_key
                        else None
                    )
                    for col in bool_cols:
                        row_dict[col] = mt.to_sqlite_bool(row_dict[col], table=tbl, column=col, key=key)
                    for col in ts_cols:
                        row_dict[col] = mt.to_sqlite_timestamp(row_dict[col])
                    if spec.conflict_key:
                        source_keys.add(tuple(row_dict[c] for c in spec.conflict_key))
                    try:
                        result = sq_conn.execute(text(insert_sql), row_dict)  # noqa: S608
                        # 업서트는 갱신에도 rowcount 1 을 준다 -- 쓴 행 수이지 새로 삽입된
                        # 행 수가 아니다. 재실행하면 source 와 같아지는 것이 정상이다.
                        count += result.rowcount
                    except Exception as row_exc:
                        # SQLite 는 문장 실패로 트랜잭션을 중단시키지 않으므로 계속 진행한다.
                        failed_rows += 1
                        log.warning("행 쓰기 실패: %s", mt.safe_error_text(row_exc, table=tbl, key=key))
                    progress.update(task, completed=count)

            with sq_engine.connect() as sq_conn:
                target_row = sq_conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).fetchone()  # noqa: S608
                target_count = int(target_row[0]) if target_row else 0

                # 행 수로는 보존을 판정할 수 없다. 타깃이 자기 행을 하나 갖고 있으면
                # 소스 행이 거부돼도 총계가 같아 통과한다. 키로 판정한다.
                # 문자열 join 이 아니라 튜플인 이유: ('a,b','c') 와 ('a','b,c') 가
                # 충돌한다. conflict_key 컬럼은 PG VARCHAR / SQLite TEXT 라 양쪽 다
                # str 로 돌아와 강제 변환이 필요 없다.
                missing_sample: list[Any] = []
                if spec.conflict_key:
                    key_cols = ", ".join(spec.conflict_key)
                    target_keys = {
                        tuple(r)
                        for r in sq_conn.execute(text(f"SELECT {key_cols} FROM {tbl}"))  # noqa: S608
                    }
                    missing_sample = sorted(source_keys - target_keys)

            table_results[tbl] = {
                "copied": count,
                "source": source_count,
                "target": target_count,
                "missing_keys": len(missing_sample),
                "failed_rows": failed_rows,
            }
            console.print(
                f"  [green]{tbl}[/green]: {count:,}행 복사 "
                f"(source={source_count:,}, target={target_count:,})"
            )
            if missing_sample:
                console.print(
                    f"  [red]{tbl}: 소스 행 {len(missing_sample):,}건이 타깃에 없다[/red] "
                    f"(예: {missing_sample[:3]})"
                )
            if failed_rows:
                console.print(f"  [red]{tbl}: 행 쓰기 실패 {failed_rows:,}건[/red]")

    return {"tables": table_results}


# ---------------------------------------------------------------------------
# Step 6 — 검증 & 요약 리포트
# ---------------------------------------------------------------------------

def write_report(
    report: dict[str, Any],
    local_data_dir: str,
) -> str:
    """migration_report_{timestamp}.json 파일 저장 후 경로 반환."""
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(local_data_dir, f"migration_report_{ts}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    return report_path


def print_summary(report: dict[str, Any]) -> None:
    """콘솔 요약 테이블 출력."""
    console.rule("[bold green]마이그레이션 요약")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("항목", style="cyan")
    table.add_column("소스", justify="right")
    table.add_column("대상(실제)", justify="right")
    table.add_column("상태", justify="center")

    counts = report.get("source_counts", {})
    results = report.get("results", {})

    def _fmt(v: Any) -> str:
        return f"{v:,}" if isinstance(v, int) else str(v)

    def _status(src: int, dst: int) -> str:
        if dst >= src:
            return "[green]OK[/green]"
        return f"[yellow]부분 ({dst}/{src})[/yellow]"

    # Graph
    if "graph" in results:
        g = results["graph"]
        src_n = counts.get("neo4j_nodes", "N/A")
        src_e = counts.get("neo4j_edges", "N/A")
        table.add_row("그래프 노드", _fmt(src_n), _fmt(g.get("nodes", 0)),
                      _status(src_n, g.get("nodes", 0)) if isinstance(src_n, int) else "-")
        table.add_row("그래프 엣지", _fmt(src_e), _fmt(g.get("edges", 0)),
                      _status(src_e, g.get("edges", 0)) if isinstance(src_e, int) else "-")

    # Docs
    if "docs" in results:
        d = results["docs"]
        table.add_row("문서 노드",  _fmt(counts.get("mongo_nodes",  "N/A")),
                      _fmt(d.get("nodes", 0)), "-")
        table.add_row("문서 소스",  _fmt(counts.get("mongo_sources", "N/A")),
                      _fmt(d.get("sources", 0)), "-")
        table.add_row("감사 이벤트", _fmt(counts.get("mongo_audit", "N/A")),
                      _fmt(d.get("audit_events", 0)), "-")

    # Vectors
    if "vectors" in results:
        v = results["vectors"]
        src_v = counts.get("chroma_vectors", "N/A")
        table.add_row("벡터", _fmt(src_v), _fmt(v.get("vectors", 0)),
                      _status(src_v, v.get("vectors", 0)) if isinstance(src_v, int) else "-")

    # SQL — #151: 반환 형태가 {"copied","source","target"} 로 바뀌어 전용 상태
    # 판정을 쓴다. absent 테이블(source is None)은 OK/MISMATCH 판정에서 제외한다.
    if "sql" in results:
        for tbl, t in results["sql"].get("tables", {}).items():
            src_val: Any = "absent" if t["source"] is None else t["source"]
            dst_val: Any = "absent" if t["target"] is None else t["target"]
            table.add_row(f"SQL:{tbl}", _fmt(src_val), _fmt(dst_val), _sql_status(t))

    console.print(table)


def _preservation_failure(t: dict[str, Any]) -> str | None:
    """이 테이블이 보존에 실패한 사유, 성공이면 None.

    요약 표와 종료 코드가 같은 함수를 보게 한다. 둘로 나뉘어 있을 때 카운트만 보는
    쪽이 초록 OK 를 찍고 키를 보는 쪽이 5 로 끝내, 운영자가 실패 원인을 설명하지
    못하는 카운트를 들여다보게 됐다.

    absent 테이블(``source is None``)은 비교 대상이 없으므로 판정에서 뺀다.
    """
    if t["source"] is None:
        return None
    missing = t.get("missing_keys", 0)
    if missing:
        return f"missing_keys={missing}"
    failed = t.get("failed_rows", 0)
    if failed:
        # 갱신 실패는 키가 이미 타깃에 있으므로 missing_keys 로 안 걸린다. 그런데 행은
        # 갱신되지 않은 채 남으므로, 이것을 세지 않으면 stale 인증 상태가 조용히 통과한다.
        return f"failed_rows={failed}"
    if t["target"] < t["source"]:
        return f"{t['target']}/{t['source']}"
    return None


def _sql_status(t: dict[str, Any]) -> str:
    if t["source"] is None:
        return "-"
    failure = _preservation_failure(t)
    return "[green]OK[/green]" if failure is None else f"[red]MISMATCH ({failure})[/red]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _row_preservation_mismatches(sql_result: dict[str, Any] | None) -> list[str]:
    """보존에 실패한 테이블 이름 목록 -- #151 6절 행 보존 판정.

    자연 키가 있는 테이블은 도착하지 않은 키가 판정 기준이다. 행 수만 보면 타깃이
    자기 행을 하나 갖고 있을 때 소스 행이 버려져도 총계가 같아 통과한다.
    자연 키가 없는 ``impact_records``/``lever_simulations`` 는 대조할 키가 없어
    ``target < source`` 로만 판정한다. 그 둘은 재실행 시 ``target > source`` 가
    될 수 있으나 이 조건에 걸리지 않는다(중복 자체는 #151 범위 밖의 기존 결함).

    자연 키 테이블은 업서트하므로 같은 키의 내용 불일치는 남지 않는다. 업서트가 다른 제약
    으로 실패한 경우는 ``failed_rows`` 가 잡는다. 자연 키가 없는 두 테이블은 여전히
    ``INSERT OR IGNORE`` 이므로 같은 키의 내용 불일치가 통과한다.
    """
    if not sql_result:
        return []
    return [
        name for name, t in sql_result["tables"].items() if _preservation_failure(t) is not None
    ]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - 최상위 default-deny 경계 (#151 7-2)
        # traceback.print_exc()/logging.exception()/rich 의 print_exception() 은
        # __cause__ 체인을 렌더링해 MigrationError 뒤에 감춘 드라이버 예외 원문을
        # 그대로 stderr 에 남긴다(실측 확인) -- safe_error_text() 결과만 찍는다.
        console.print(f"\n[bold red]! 마이그레이션 실패[/bold red]: {mt.safe_error_text(exc)}")
        return 1


def _run(args: argparse.Namespace) -> int:
    # 로컬 데이터 디렉토리 결정
    local_data_dir = (
        args.local_data_dir
        or os.environ.get("LOCAL_DATA_DIR")
        or "./opencrab_data"
    )
    local_data_dir = os.path.abspath(local_data_dir)
    os.makedirs(local_data_dir, exist_ok=True)

    console.print("\n[bold]OpenCrab docker → local 마이그레이션[/bold]")
    console.print(f"  로컬 데이터 디렉토리: {local_data_dir}")
    console.print(f"  dry-run: {args.dry_run}\n")

    # Step 0 — Pre-flight
    preflight_result = preflight(args)
    source_counts = preflight_result["counts"]
    excluded_auth_tables = preflight_result.get("excluded_auth_tables", [])

    if args.dry_run:
        console.print("\n[bold yellow]--dry-run 모드: 여기서 종료합니다.[/bold yellow]")
        _print_counts_table(source_counts)
        if excluded_auth_tables:
            console.print(f"[yellow]--allow-unmigrated: 제외된 테이블 {excluded_auth_tables}[/yellow]")
        return 0

    # 자격증명 가드는 어떤 스토어보다 먼저 돈다. Step 2~4 가 그래프·문서·벡터를 먼저
    # 쓰므로, migrate_sql 안에서만 중단하면 이미 세 스토어를 고친 뒤의 종료 코드가 된다.
    if not args.skip_sql and not args.allow_target_only_auth:
        sqlite_path = os.path.join(local_data_dir, "opencrab.db")
        if os.path.exists(sqlite_path):
            target_only = _target_only_auth(preflight_result["pg_engine"], sqlite_path)
            if target_only:
                raise mt.TargetOnlyAuthError(mt.target_only_report(target_only))

    # Step 1 — 백업
    with file_lock("write.lock", local_data_dir):
        backup_local_data(local_data_dir)

    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "local_data_dir": local_data_dir,
        "source_counts": source_counts,
        "excluded_auth_tables": excluded_auth_tables,
        "results": {},
    }

    # Step 2 — 그래프 마이그레이션
    if not args.skip_graph:
        console.rule("[bold blue]Step 2 — 그래프 마이그레이션 (Neo4j → LocalGraphStore)")
        from opencrab.stores.local_graph_store import LocalGraphStore
        graph_db_path = os.path.join(local_data_dir, "graph.db")
        local_graph = LocalGraphStore(db_path=graph_db_path)
        try:
            with file_lock("write.lock", local_data_dir):
                graph_result = migrate_graph(
                    preflight_result["neo4j_driver"], local_graph, args.batch_size, logger
                )
            report["results"]["graph"] = graph_result
            console.print(f"  [green]완료[/green] nodes={graph_result['nodes']:,} edges={graph_result['edges']:,}")
        finally:
            local_graph.close()
    else:
        console.print("[yellow]그래프 마이그레이션 건너뜀[/yellow]")

    # Step 3 — 문서 마이그레이션
    if not args.skip_docs:
        console.rule("[bold blue]Step 3 — 문서 마이그레이션 (MongoDB → LocalDocStore)")
        # LocalSQLDocStore가 구현됐으면 우선 사용, 없으면 LocalDocStore fallback
        try:
            from opencrab.stores.local_sql_doc_store import LocalSQLDocStore  # type: ignore[import]
            doc_db_path = os.path.join(local_data_dir, "doc_store.db")
            doc_store = LocalSQLDocStore(db_path=doc_db_path)
            console.print("  LocalSQLDocStore 사용")
        except ImportError:
            from opencrab.stores.local_doc_store import LocalDocStore
            docs_dir = os.path.join(local_data_dir, "docs")
            doc_store = LocalDocStore(data_dir=docs_dir)
            console.print("  [yellow]LocalSQLDocStore 미구현 → LocalDocStore(JSON) fallback[/yellow]")

        with file_lock("write.lock", local_data_dir):
            docs_result = migrate_docs(
                preflight_result["mongo_db"], doc_store, args.batch_size, logger
            )
        report["results"]["docs"] = docs_result
        console.print(
            f"  [green]완료[/green] nodes={docs_result['nodes']:,} "
            f"sources={docs_result['sources']:,} audit={docs_result['audit_events']:,}"
        )
    else:
        console.print("[yellow]문서 마이그레이션 건너뜀[/yellow]")

    # Step 4 — 벡터 마이그레이션
    if not args.skip_vectors:
        console.rule("[bold blue]Step 4 — 벡터 마이그레이션 (HTTP Chroma → local Chroma)")
        import chromadb  # type: ignore[import]
        chroma_local_path = os.path.join(local_data_dir, "chroma")
        os.makedirs(chroma_local_path, exist_ok=True)
        local_chroma = chromadb.PersistentClient(path=chroma_local_path)
        with file_lock("write.lock", local_data_dir):
            vectors_result = migrate_vectors(
                preflight_result["chroma_http"],
                local_chroma,
                args.chroma_collection,
                args.batch_size,
                logger,
            )
        report["results"]["vectors"] = vectors_result
        console.print(f"  [green]완료[/green] vectors={vectors_result['vectors']:,}")
    else:
        console.print("[yellow]벡터 마이그레이션 건너뜀[/yellow]")

    # Step 5 — SQL 마이그레이션
    if not args.skip_sql:
        console.rule("[bold blue]Step 5 — SQL 마이그레이션 (PostgreSQL → SQLite)")
        sqlite_path = os.path.join(local_data_dir, "opencrab.db")
        with file_lock("write.lock", local_data_dir):
            sql_result = migrate_sql(
                args.pg_url, sqlite_path, logger, args.allow_target_only_auth
            )
        report["results"]["sql"] = sql_result
        total_rows = sum(t["copied"] for t in sql_result["tables"].values())
        console.print(f"  [green]완료[/green] total rows={total_rows:,}")
    else:
        console.print("[yellow]SQL 마이그레이션 건너뜀[/yellow]")

    # Step 6 — 리포트
    console.rule("[bold blue]Step 6 — 검증 & 요약 리포트")
    report["finished_at"] = datetime.now(UTC).isoformat()
    report_path = write_report(report, local_data_dir)
    console.print(f"  리포트 저장: {report_path}")
    print_summary(report)
    if excluded_auth_tables:
        console.print(f"[yellow]제외된 테이블 (--allow-unmigrated): {excluded_auth_tables}[/yellow]")

    # #151 6절: 행 보존 실패는 비영 종료(5, 정방향 --verify 불일치와 동일 코드)
    mismatches = _row_preservation_mismatches(report["results"].get("sql"))
    if mismatches:
        console.print(
            f"\n[bold red]! 행 보존 실패 (MISMATCH)[/bold red]: {mismatches} "
            "(target < source — 위 요약 표 참고)"
        )
        return 5

    console.print("\n[bold green]마이그레이션 완료![/bold green]")
    return 0


def _print_counts_table(counts: dict[str, Any]) -> None:
    """dry-run 모드에서 소스 카운트를 테이블로 출력."""
    table = Table(title="소스 데이터 규모", show_header=True, header_style="bold magenta")
    table.add_column("항목", style="cyan")
    table.add_column("수량", justify="right")
    table.add_row("Neo4j 노드",  f"{counts.get('neo4j_nodes', 'N/A'):,}" if isinstance(counts.get('neo4j_nodes'), int) else "N/A")
    table.add_row("Neo4j 엣지",  f"{counts.get('neo4j_edges', 'N/A'):,}" if isinstance(counts.get('neo4j_edges'), int) else "N/A")
    table.add_row("MongoDB nodes",  f"{counts.get('mongo_nodes', 'N/A'):,}" if isinstance(counts.get('mongo_nodes'), int) else "N/A")
    table.add_row("MongoDB sources", f"{counts.get('mongo_sources', 'N/A'):,}" if isinstance(counts.get('mongo_sources'), int) else "N/A")
    table.add_row("MongoDB audit",  f"{counts.get('mongo_audit', 'N/A'):,}" if isinstance(counts.get('mongo_audit'), int) else "N/A")
    table.add_row("Chroma 벡터",  f"{counts.get('chroma_vectors', 'N/A'):,}" if isinstance(counts.get('chroma_vectors'), int) else "N/A")
    pg_tables = counts.get("pg_tables", {})
    for tbl, cnt in pg_tables.items():
        table.add_row(f"PostgreSQL:{tbl}", f"{cnt:,}")
    console.print(table)


if __name__ == "__main__":
    raise SystemExit(main())
