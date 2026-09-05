"""
Shared SQL-dialect plumbing for the execution/billing layer.

workflow.py (workflow_runs/action_log), approvals.py (approval_queue), and
billing/hooks.py (billing_events) each hand-roll the same three things:
  - picking a SQLite-DDL-list vs PG-DDL-list by ``sql_store._is_sqlite``
  - a "run these CREATE TABLE/INDEX statements once" ensure-tables loop
  - a ``datetime('now')`` vs ``NOW()`` branch for hand-written UPDATE SQL

This module centralizes that plumbing on top of the dialect fragments
already defined in ``opencrab.stores._sql_dialect`` (SqlDialect
SQLITE/POSTGRES), without adopting its SchemaSpec/render_ddl system --
each of the three callers still writes its own literal CREATE TABLE
strings per dialect (columns differ table to table); this only stops
each module from re-deriving "is this store sqlite or postgres", "what's
the ensure-tables loop", and "what's the now() fragment" by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from opencrab.stores._sql_dialect import POSTGRES, SQLITE, SqlDialect


def dialect_for(sql_store: Any) -> SqlDialect:
    """Return the ``SqlDialect`` matching *sql_store*'s backend."""
    return SQLITE if sql_store._is_sqlite else POSTGRES


def is_sqlite(sql_store: Any) -> bool:
    """Whether *sql_store* is backed by SQLite (vs PostgreSQL)."""
    return bool(sql_store._is_sqlite)


def now_expr(sql_store: Any) -> str:
    """SQL "current time" fragment for *sql_store*'s dialect: ``datetime('now')``
    for SQLite, ``NOW()`` for PostgreSQL."""
    return dialect_for(sql_store).now_expr()


def ensure_tables(
    sql_store: Any,
    ddl_sqlite: Sequence[str],
    ddl_pg: Sequence[str],
    *,
    own_file_lock: bool = False,
) -> None:
    """Run *ddl_sqlite* or *ddl_pg* (whichever matches *sql_store*'s dialect)
    inside one transaction, in order.

    Each caller supplies its own per-dialect DDL statement list (table
    columns/types differ per module); this only picks the right list for
    the store's backend and executes it. Callers that need to swallow
    setup failures (e.g. BillingHooks, which is fire-and-forget) wrap this
    call in their own try/except -- this function itself always raises.

    issue #141 항목 6: 이 함수가 workflow.py/approvals.py/billing/hooks.py
    가 공유하는 DDL 실행 지점이다 — write.lock 없이 *sql_store*._engine에
    직접 DDL을 실행했다. 여기 한 곳만 잠그면 그 호출자들이 보호된다
    (opencrab.stores.sql_store의 write_lock_for_store와 같은 헬퍼: SQLite
    전용, PG는 no-op).

    ``own_file_lock=True``(codex PR #323 리뷰, R3): billing.db 는 opencrab.db
    와 같은 local_data_dir 에 있어 기본(디렉터리 공유) write.lock 을 쓰면
    BillingHooks 부트스트랩이 무관한 온톨로지 쓰기 뒤에서 대기하게 된다 —
    #105 가 분리한 격리를 되돌린다. BillingHooks._ensure_tables() 는 이
    플래그로 자신의 db 파일에만 스코프된 락을 쓴다.
    """
    from sqlalchemy import text

    from opencrab.stores.sql_store import write_lock_for_store

    ddl = ddl_sqlite if sql_store._is_sqlite else ddl_pg
    lock_ctx = write_lock_for_store(sql_store, own_file=own_file_lock)
    with lock_ctx, sql_store._engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
