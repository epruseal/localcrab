#!/usr/bin/env python3
"""Repair pgvector rows whose ``pack_id`` holds the legacy literal ``'None'``.

이슈 #306 (#197의 데이터 후속). #197/PR#303 이전 pgvector 쓰기 경로는
``str(meta.get("pack_id", ""))``를 썼다. 이 표현은 ``metadata`` 키가 "없을 때만"
기본값을 쓰므로, ``metadata={"pack_id": None, ...}``처럼 키는 있고 값이 파이썬
``None``인 경우 그 값을 문자열화해 리터럴 ``"None"``을 ``pack_id`` 컬럼에 그대로
적었다. 현재 게이트(``reject_foreign_slot_writes``)는 이 리터럴을 "None이라는
이름의 팩이 소유한 슬롯"으로 읽으므로, 원래 그 슬롯의 정당한 소유자(실제로는
UNOWNED로 재적재하려는 호출자)가 재적재하면 거부된다. 이 스크립트는 그 과거
버그가 이미 써놓은 **잔존 데이터**를 복구한다 — 현재 코드에는 결함이 없다
(``tests/test_vector_slot_ownership.py::TestNonePackIdIsStoredAsUnowned`` 참고).

대상은 pgvector 뿐이다: sqlite-vec은 ``_sanitize_metadata``가 저장 전에 값을
접어 이 형태가 구조적으로 발생할 수 없고, chroma는 전용 ``pack_id`` 컬럼이 없다.

탐지 조건은 ``pack_id = 'None'`` 과 ``metadata @> '{"pack_id": null}'::jsonb``
(메타 키가 실제로 존재하고 값이 JSON null인 경우만) 둘 다를 요구한다 — 진짜로
"None"이라는 이름의 팩이 소유한 행(그 metadata에는 문자열 ``"None"``이 그대로
들어 있어 이 containment 조건을 만족하지 않는다)까지 오염으로 오인해 파괴하지
않기 위함이다.

SAFETY (Autonomy Contract 매핑):
  - 기본 dry-run. 실제 쓰기는 명시적 ``--apply``가 있어야 한다.
  - ``--apply``는 반드시 백업을 선행한다(``--backup-to <path>``, 또는 명시적
    ``--skip-backup``). 백업은 영향받는 행만 담은 자기완결 JSON 스냅샷이다
    (``pg_dump`` 전체 스냅샷은 이 좁은 단일 컬럼 복구에 과도하다).
  - 백업 파일은 임시 파일에 완전히 쓰고 fsync한 뒤 ``os.link()``로 최종 경로에
    배타 생성(이미 있으면 실패, 덮어쓰지 않음)하고, 성공 후 부모 디렉터리를
    fsync한다. 백업 경로(``--backup-to``)를 지정했고 대상 행이 있으면 DB
    트랜잭션은 이 게시가 전부 성공한 뒤에만 COMMIT한다. 백업 실패는 트랜잭션
    전체를 ROLLBACK시킨다(코드 4). 백업 경로를 지정했어도 대상 행이 0건이면
    백업할 내용이 없으므로 파일을 만들지 않고 그대로 COMMIT한다. 이 두 경로에서는
    "DB는 바뀌었는데 백업이 없는" 상태가 구조적으로 불가능하다. ``--skip-backup``
    으로 백업 자체를 명시적으로 생략한 경로는 이 게이트 대상이 아니며, 대상 행이
    있어도 게시 없이 바로 COMMIT한다(사용자가 직접 선택한 생략이지 유실이 아니다).
  - 탐지-백업-UPDATE는 단일 SQL 문(``WITH ... FOR UPDATE ... UPDATE ... RETURNING``)
    으로 원자화한다 — 별도 SELECT 후 조건부 UPDATE 방식의 TOCTOU 경합이 없다.
  - 실행 전 예상 건수와 실제 rowcount가 다르면 트랜잭션을 중단한다(코드 5).
  - 멱등: 이미 고쳐진 행(``pack_id=''``)은 탐지 조건에 애초에 걸리지 않으므로
    재실행해도 안전하게 0건 처리된다.
  - 라이브 데이터에는 실행하지 않는다. 검증은 격리된 개발/테스트 인스턴스에서만
    한다.

ROLLBACK (``--rollback-from <snapshot.json>``, ``--apply`` 필요):
  스냅샷이 기록한 각 행에 대해 PostgreSQL 시스템 컬럼 ``xmin``(그 행을 마지막으로
  쓴 트랜잭션 id)이 복구 직후 값과 여전히 같을 때만 되돌린다. ``xmin``이 다르면
  그 행은 복구 이후 어떤 형태로든(정당한 소유자의 인수, 삭제 후 재생성, 다른
  도구의 수정) 다시 쓰인 것이므로, metadata 내용이 우연히 같아도 물리적으로
  다른 행 버전이면 정확히 구분해 건너뛴다 — 조용히 덮어쓰지 않고 각 행을
  롤백됨/xmin 불일치로 건너뜀/이미 삭제됨 세 상태로 분류해 전부 보고한다.

  **한계**: PostgreSQL 트랜잭션 id는 32비트로 순환하고, ``VACUUM FREEZE``가
  오래된 행의 ``xmin``을 ``FrozenTransactionId``로 바꿔 원래 값을 지운다. 이
  도구의 수리→롤백 창은 운영자가 즉시 또는 곧 실행하는 짧은 구간을 전제하므로
  실무상 문제가 되지 않지만, **오래된 백업 파일(며칠~몇 주 뒤)에 대한
  ``--rollback-from``은 안전하다고 보장하지 않는다** — 이 한계는 실행 시점마다
  stderr 경고로도 출력된다(문서/docstring만 읽지 않는 운영자도 보게 하기 위함).

  백업 서명(``table``/``database``)이 현재 대상과 다르면 즉시 거부한다(코드 4).

EXIT CODES:
  0 성공, 2 사용법/안전 게이트 실패, 3 연결/사전조건 실패, 4 백업 검증 실패,
  5 건수 불일치(UPDATE/롤백 rowcount != 예상, 또는 롤백 부분 처리).

Usage:
    python scripts/repair_pgvector_legacy_none_owner.py --pg-url ...
    python scripts/repair_pgvector_legacy_none_owner.py --pg-url ... \\
        --apply --backup-to /path/to/backup.json
    python scripts/repair_pgvector_legacy_none_owner.py --pg-url ... \\
        --apply --rollback-from /path/to/backup.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from opencrab.stores._graph_common import IDENT_RE

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_BACKUP = 4
EXIT_COUNT_MISMATCH = 5


class CountMismatchError(RuntimeError):
    """실행 전 예상 건수와 실제 rowcount가 다를 때."""


class SnapshotError(ValueError):
    """백업 스냅샷 파일이 손상됐거나, 서명이 대상과 다르거나, 중복 node_id가 있을 때."""


def _check_ident(name: str, label: str) -> None:
    if not IDENT_RE.fullmatch(name):
        raise ValueError(f"Unsafe {label}: {name!r}")


def _as_dict_metadata(value: Any) -> dict[str, Any]:
    """JSONB 값을 dict로 정규화(드라이버가 str/dict 어느 쪽을 주든 안전)."""
    if isinstance(value, dict):
        return value
    return json.loads(value) if value else {}


# ---------------------------------------------------------------------------
# SQL (테이블명은 IDENT_RE로 검증된 값만 f-string 보간한다)
# ---------------------------------------------------------------------------


def _detect_sql(table: str) -> str:
    return (
        f"SELECT node_id, pack_id, metadata FROM {table} "
        "WHERE pack_id = 'None' AND metadata @> '{\"pack_id\": null}'::jsonb "
        "ORDER BY node_id"
    )


def _count_sql(table: str) -> str:
    return (
        f"SELECT count(*) FROM {table} "
        "WHERE pack_id = 'None' AND metadata @> '{\"pack_id\": null}'::jsonb"
    )


def _repair_sql(table: str) -> str:
    return (
        "WITH to_fix AS ("
        f"SELECT node_id, pack_id, metadata FROM {table} "
        "WHERE pack_id = 'None' AND metadata @> '{\"pack_id\": null}'::jsonb "
        "FOR UPDATE"
        ") "
        f"UPDATE {table} SET pack_id = '' "
        f"FROM to_fix WHERE {table}.node_id = to_fix.node_id "
        f"RETURNING {table}.node_id, to_fix.pack_id AS prior_pack_id, "
        f"to_fix.metadata, {table}.xmin::text AS xmin"
    )


def _audit_sql(table: str) -> str:
    return (
        f"SELECT pack_id, count(*) AS n FROM {table} "
        "WHERE pack_id IS NOT NULL AND pack_id != '' AND pack_id != 'None' "
        "AND (pack_id ~ '^\\s*$' OR lower(pack_id) IN ('null', 'none')) "
        "GROUP BY pack_id ORDER BY pack_id"
    )


def _rollback_row_sql(table: str) -> str:
    return (
        "WITH cand AS ("
        f"SELECT node_id FROM {table} "
        "WHERE node_id = :node_id AND pack_id = '' AND xmin::text = :xmin "
        "FOR UPDATE"
        ") "
        f"UPDATE {table} SET pack_id = 'None' "
        f"FROM cand WHERE {table}.node_id = cand.node_id "
        f"RETURNING {table}.node_id"
    )


# ---------------------------------------------------------------------------
# 연결
# ---------------------------------------------------------------------------


def connect(pg_url: str) -> Any:
    from sqlalchemy import create_engine

    return create_engine(pg_url, pool_pre_ping=True, hide_parameters=True)


# ---------------------------------------------------------------------------
# 탐지 / 감사 (읽기 전용)
# ---------------------------------------------------------------------------


def detect(engine: Any, table: str) -> list[dict[str, Any]]:
    """오염 후보를 잠금 없이 보고한다(dry-run 경로). 아무것도 바꾸지 않는다."""
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text(_detect_sql(table))).mappings().all()
    return [
        {
            "node_id": r["node_id"],
            "pack_id": r["pack_id"],
            "metadata": _as_dict_metadata(r["metadata"]),
        }
        for r in rows
    ]


def audit(engine: Any, table: str) -> list[dict[str, Any]]:
    """자동 복구 대상이 아닌 의심스러운 값(공백 전용, 대소문자 변형)을 정보용으로
    보고한다. 진짜 팩 이름과 구분할 근거가 없으므로 절대 자동 복구하지 않는다."""
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text(_audit_sql(table))).mappings().all()
    return [{"pack_id": r["pack_id"], "n": r["n"]} for r in rows]


# ---------------------------------------------------------------------------
# 백업 파일 (원자적/크래시 안전 게시)
# ---------------------------------------------------------------------------


def write_backup_atomic(backup_to: str, snapshot: dict[str, Any]) -> None:
    """임시 파일에 쓰고 fsync한 뒤 ``os.link()``로 배타 생성, 부모 디렉터리를
    fsync한다. ``os.link``는 대상이 이미 있으면 ``FileExistsError``를 내는
    원자적 단일 syscall이라 "존재하면 실패, 없으면 원자 생성"을 정확히
    보장한다(``os.replace``는 무조건 덮어써 이 성질을 주지 못한다)."""
    tmp = f"{backup_to}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.link(tmp, backup_to)
    except FileExistsError:
        raise FileExistsError(f"backup target already exists: {backup_to}") from None
    finally:
        os.unlink(tmp)
    dir_path = os.path.dirname(os.path.abspath(backup_to)) or "."
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_snapshot(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    node_ids = [r["node_id"] for r in snapshot.get("rows", [])]
    if len(node_ids) != len(set(node_ids)):
        raise SnapshotError(f"backup snapshot has duplicate node_id entries: {path}")
    return snapshot


def validate_snapshot_signature(snapshot: dict[str, Any], table: str, database: str) -> None:
    if snapshot.get("table") != table or snapshot.get("database") != database:
        raise SnapshotError(
            "backup signature mismatch: snapshot is for "
            f"table={snapshot.get('table')!r} database={snapshot.get('database')!r}, "
            f"but current target is table={table!r} database={database!r}"
        )


# ---------------------------------------------------------------------------
# 복구 (--apply, 백업 선행 후 커밋)
# ---------------------------------------------------------------------------


def repair(engine: Any, table: str, backup_to: str | None) -> list[dict[str, Any]]:
    """복구를 원자적으로 실행한다.

    순서: 트랜잭션 시작 -> 예상 건수 계산(잠금 없음) -> 원자 CTE 실행(FOR UPDATE
    + UPDATE + RETURNING, 한 문장) -> rowcount 대사(불일치 시 CountMismatchError,
    트랜잭션은 호출자가 예외를 받아 롤백) -> (backup_to가 있고 대상 행이 있으면)
    백업 파일을 원자적으로 게시(실패 시 예외, 트랜잭션 롤백) -> 커밋.

    즉 "DB 커밋 + 백업 유실" 조합이 구조적으로 불가능하다: 대상 행이 있는 한
    백업 게시 성공이 커밋의 전제조건이고, 대상 행이 0건이면 애초에 백업할
    내용도 없다.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        trans = conn.begin()
        try:
            expected = conn.execute(text(_count_sql(table))).scalar()
            rows = conn.execute(text(_repair_sql(table))).mappings().all()
            if len(rows) != expected:
                raise CountMismatchError(
                    f"expected {expected} rows to repair, UPDATE affected {len(rows)}"
                )
            result_rows = [
                {
                    "node_id": r["node_id"],
                    "prior_pack_id": r["prior_pack_id"],
                    "metadata": _as_dict_metadata(r["metadata"]),
                    "xmin": r["xmin"],
                }
                for r in rows
            ]
            if backup_to and result_rows:
                database = conn.execute(text("SELECT current_database()")).scalar()
                snapshot = {"table": table, "database": database, "rows": result_rows}
                write_backup_atomic(backup_to, snapshot)
            trans.commit()
        except Exception:
            trans.rollback()
            raise
    return result_rows


# ---------------------------------------------------------------------------
# 롤백 (--rollback-from, xmin 지문 대조)
# ---------------------------------------------------------------------------

ROLLBACK_STATUS_DONE = "rolled_back"
ROLLBACK_STATUS_XMIN_MISMATCH = "skipped_xmin_mismatch"
ROLLBACK_STATUS_DELETED = "skipped_deleted"


def rollback(engine: Any, table: str, snapshot: dict[str, Any]) -> dict[str, str]:
    """스냅샷의 각 행을 ``xmin`` 지문이 일치할 때만 되돌린다. 불일치/삭제된 행은
    조용히 건너뛰지 않고 상태를 개별 보고한다(호출자가 부분 처리를 확인하도록)."""
    from sqlalchemy import text

    statuses: dict[str, str] = {}
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            for row in snapshot["rows"]:
                node_id = row["node_id"]
                res = conn.execute(
                    text(_rollback_row_sql(table)),
                    {"node_id": node_id, "xmin": row["xmin"]},
                ).fetchall()
                if res:
                    statuses[node_id] = ROLLBACK_STATUS_DONE
                    continue
                exists = conn.execute(
                    text(f"SELECT 1 FROM {table} WHERE node_id = :node_id"),
                    {"node_id": node_id},
                ).scalar()
                statuses[node_id] = (
                    ROLLBACK_STATUS_XMIN_MISMATCH if exists else ROLLBACK_STATUS_DELETED
                )
            trans.commit()
        except Exception:
            trans.rollback()
            raise
    return statuses


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pg-url", default=None, help="target PostgreSQL DSN (default: settings.postgres_url)")
    ap.add_argument("--table", default=None, help="pgvector table name (default: settings.embed_collection)")
    ap.add_argument("--apply", action="store_true", help="perform writes; without this the script is dry-run")
    ap.add_argument("--backup-to", default=None, help="write a JSON snapshot of affected rows here before --apply")
    ap.add_argument("--skip-backup", action="store_true", help="explicitly skip the mandatory pre-repair backup")
    ap.add_argument("--rollback-from", default=None, help="restore rows from a prior --backup-to snapshot (requires --apply)")
    args = ap.parse_args(argv)

    from opencrab.config import get_settings

    settings = get_settings()
    pg_url = args.pg_url or settings.postgres_url
    table = args.table or settings.embed_collection

    try:
        _check_ident(table, "--table")
    except ValueError as exc:
        print(f"! {exc}")
        return EXIT_USAGE

    print(f"# pg-url : {pg_url}")
    print(f"# table  : {table}")

    try:
        engine = connect(pg_url)
        with engine.connect():
            pass
    except Exception as exc:
        print(f"! could not connect: {exc}")
        return EXIT_PRECONDITION

    if args.rollback_from:
        if not args.apply:
            print("! --rollback-from requires --apply.")
            return EXIT_USAGE
        try:
            snapshot = load_snapshot(args.rollback_from)
        except (OSError, json.JSONDecodeError, SnapshotError) as exc:
            print(f"! could not load snapshot: {exc}")
            return EXIT_BACKUP
        from sqlalchemy import text

        with engine.connect() as conn:
            database = conn.execute(text("SELECT current_database()")).scalar()
        try:
            validate_snapshot_signature(snapshot, table, database)
        except SnapshotError as exc:
            print(f"! {exc}")
            return EXIT_BACKUP

        print(
            "! WARNING: rollback matches rows via PostgreSQL 'xmin', which "
            "wraps around (32-bit transaction ids) and is rewritten by VACUUM "
            "FREEZE. This tool assumes the repair-to-rollback window is short "
            "(run promptly). Rollback of an OLD snapshot (days/weeks later) is "
            "NOT guaranteed safe.",
            file=sys.stderr,
        )

        statuses = rollback(engine, table, snapshot)
        done = sum(1 for s in statuses.values() if s == ROLLBACK_STATUS_DONE)
        total = len(statuses)
        for node_id, status in statuses.items():
            print(f"#   {node_id}: {status}")
        print(f"# rolled back {done}/{total} rows.")
        if done != total:
            print(
                f"! {total - done} row(s) were NOT rolled back (see per-row "
                "status above) -- do not assume the rollback is complete."
            )
            print("RESULT: FAIL (partial rollback)")
            return EXIT_COUNT_MISMATCH
        print("RESULT: PASS")
        return EXIT_OK

    detected = detect(engine, table)
    audited = audit(engine, table)
    print(f"# contaminated rows detected: {len(detected)}")
    for row in detected[:20]:
        print(f"#   {row['node_id']}")
    if len(detected) > 20:
        print(f"#   ... and {len(detected) - 20} more")
    if audited:
        print("# audit (informational only, NOT auto-repaired):")
        for entry in audited:
            print(f"#   pack_id={entry['pack_id']!r}: {entry['n']} row(s)")

    if not args.apply:
        print("# dry-run: no writes.")
        print("RESULT: PASS (dry-run)")
        return EXIT_OK

    if not args.backup_to and not args.skip_backup:
        print("! --apply requires --backup-to <path> (or explicit --skip-backup).")
        return EXIT_USAGE

    try:
        result_rows = repair(engine, table, args.backup_to)
    except CountMismatchError as exc:
        print(f"! {exc}")
        return EXIT_COUNT_MISMATCH
    except (OSError, FileExistsError) as exc:
        print(f"! backup write failed, repair rolled back: {exc}")
        return EXIT_BACKUP

    print(f"# repaired {len(result_rows)} row(s).")
    if args.backup_to:
        print(f"# backup written -> {args.backup_to}")
    print("RESULT: PASS")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
