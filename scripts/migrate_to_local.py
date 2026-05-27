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
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

# rich 는 pyproject.toml 의존성에 포함돼 있음
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()
logger = logging.getLogger(__name__)

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
    return p.parse_args()


# ---------------------------------------------------------------------------
# Step 0 — Pre-flight 연결 확인
# ---------------------------------------------------------------------------

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
        driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_pass))
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
        pg_engine = create_engine(args.pg_url, connect_args={"connect_timeout": 5})
        with pg_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        # 테이블 존재 여부에 따라 카운트
        sql_counts: dict[str, int] = {}
        tables = ["ontology_nodes", "ontology_edges", "impact_records",
                  "lever_simulations", "rebac_policies"]
        with pg_engine.connect() as conn:
            for tbl in tables:
                try:
                    row = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).fetchone()  # noqa: S608
                    sql_counts[tbl] = int(row[0]) if row else 0
                except Exception:
                    sql_counts[tbl] = 0
        counts["pg_tables"] = sql_counts
        result["pg_engine"] = pg_engine
        total_pg = sum(sql_counts.values())
        console.print(f"[green]OK[/green] (total rows={total_pg:,})")
    except Exception as exc:
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

def migrate_sql(
    pg_url: str,
    sqlite_path: str,
    log: logging.Logger,
) -> dict[str, Any]:
    """
    목적: PostgreSQL의 모든 테이블 행을 SQLite로 복사한다.
    소스: PostgreSQL — ontology_nodes, ontology_edges, impact_records,
                       lever_simulations, rebac_policies
    대상: SQLite (opencrab.db) — SQLStore._create_tables() 로 스키마 초기화
    주의:
      - SERIAL/TIMESTAMPTZ는 SQLite에서 INTEGER/TEXT로 매핑됨
      - id 컬럼(auto increment)은 제외하고 나머지 컬럼만 INSERT
      - ON CONFLICT DO NOTHING 으로 중복 허용
    반환: {"tables": {table_name: row_count, ...}}
    """
    from sqlalchemy import create_engine, inspect, text  # type: ignore[import]

    # SQLite 스키마 초기화 (SQLStore 생성자가 처리)
    from opencrab.stores.sql_store import SQLStore
    sql_store = SQLStore(url=f"sqlite:///{sqlite_path}")
    if not sql_store.available:
        raise RuntimeError(f"SQLite 초기화 실패: {sqlite_path}")

    pg_engine = create_engine(pg_url, connect_args={"connect_timeout": 5})
    sq_engine = sql_store._engine

    tables = ["ontology_nodes", "ontology_edges", "impact_records",
              "lever_simulations", "rebac_policies"]

    table_counts: dict[str, int] = {}

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  TextColumn("{task.completed} rows"), console=console) as progress:
        for tbl in tables:
            task = progress.add_task(f"  {tbl}", total=None)
            count = 0
            try:
                # PostgreSQL에서 컬럼 이름 조회 (id 제외)
                insp = inspect(pg_engine)
                cols_info = insp.get_columns(tbl)
                col_names = [c["name"] for c in cols_info if c["name"] != "id"]

                if not col_names:
                    log.warning("테이블 '%s' 컬럼 정보 없음, 스킵", tbl)
                    table_counts[tbl] = 0
                    continue

                cols_sql = ", ".join(col_names)
                placeholders = ", ".join(f":{c}" for c in col_names)

                with pg_engine.connect() as pg_conn:
                    rows = pg_conn.execute(text(f"SELECT {cols_sql} FROM {tbl}")).fetchall()  # noqa: S608

                with sq_engine.begin() as sq_conn:
                    for row in rows:
                        row_dict = dict(zip(col_names, row))
                        try:
                            sq_conn.execute(
                                text(f"INSERT OR IGNORE INTO {tbl} ({cols_sql}) VALUES ({placeholders})"),  # noqa: S608
                                row_dict,
                            )
                            count += 1
                        except Exception as row_exc:
                            log.warning("행 삽입 실패 (%s): %s", tbl, row_exc)
                        progress.update(task, completed=count)

                table_counts[tbl] = count
                console.print(f"  [green]{tbl}[/green]: {count:,}행")
            except Exception as exc:
                log.warning("테이블 '%s' 마이그레이션 오류: %s", tbl, exc)
                console.print(f"  [yellow]{tbl}: 오류 — {exc}[/yellow]")
                table_counts[tbl] = 0

    return {"tables": table_counts}


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

    # SQL
    if "sql" in results:
        for tbl, cnt in results["sql"].get("tables", {}).items():
            src_cnt = counts.get("pg_tables", {}).get(tbl, "N/A")
            table.add_row(f"SQL:{tbl}", _fmt(src_cnt), _fmt(cnt),
                          _status(src_cnt, cnt) if isinstance(src_cnt, int) else "-")

    console.print(table)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()

    # 로컬 데이터 디렉토리 결정
    local_data_dir = (
        args.local_data_dir
        or os.environ.get("LOCAL_DATA_DIR")
        or "./opencrab_data"
    )
    local_data_dir = os.path.abspath(local_data_dir)
    os.makedirs(local_data_dir, exist_ok=True)

    console.print(f"\n[bold]OpenCrab docker → local 마이그레이션[/bold]")
    console.print(f"  로컬 데이터 디렉토리: {local_data_dir}")
    console.print(f"  dry-run: {args.dry_run}\n")

    # Step 0 — Pre-flight
    preflight_result = preflight(args)
    source_counts = preflight_result["counts"]

    if args.dry_run:
        console.print("\n[bold yellow]--dry-run 모드: 여기서 종료합니다.[/bold yellow]")
        _print_counts_table(source_counts)
        return

    # Step 1 — 백업
    backup_local_data(local_data_dir)

    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "local_data_dir": local_data_dir,
        "source_counts": source_counts,
        "results": {},
    }

    # Step 2 — 그래프 마이그레이션
    if not args.skip_graph:
        console.rule("[bold blue]Step 2 — 그래프 마이그레이션 (Neo4j → LocalGraphStore)")
        from opencrab.stores.local_graph_store import LocalGraphStore
        graph_db_path = os.path.join(local_data_dir, "graph.db")
        local_graph = LocalGraphStore(db_path=graph_db_path)
        try:
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
        sql_result = migrate_sql(args.pg_url, sqlite_path, logger)
        report["results"]["sql"] = sql_result
        total_rows = sum(sql_result["tables"].values())
        console.print(f"  [green]완료[/green] total rows={total_rows:,}")
    else:
        console.print("[yellow]SQL 마이그레이션 건너뜀[/yellow]")

    # Step 6 — 리포트
    console.rule("[bold blue]Step 6 — 검증 & 요약 리포트")
    report["finished_at"] = datetime.now(UTC).isoformat()
    report_path = write_report(report, local_data_dir)
    console.print(f"  리포트 저장: {report_path}")
    print_summary(report)
    console.print("\n[bold green]마이그레이션 완료![/bold green]")


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
    main()
