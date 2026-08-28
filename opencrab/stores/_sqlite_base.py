"""
Shared thread-local SQLite connection scaffolding.

EXTRACTED VERBATIM (byte-near-identical across all three call sites, diffed
before extraction) FROM:
    - opencrab/stores/local_graph_store.py   (~112-175: __init__ conn-state,
      _new_conn, _conn property, close)
    - opencrab/stores/local_sql_doc_store.py (~98-216: same members)
    - opencrab/stores/sqlite_vec_store.py    (~200-323: same members, plus
      sqlite-vec extension loading and `PRAGMA busy_timeout=5000`)

THREAD SAFETY: each thread gets its own sqlite3 connection (threading.local) —
    sharing one connection across threads corrupts even reads. WAL mode lets
    those per-thread connections read concurrently while a threading.Lock
    (``self._lock``, owned by the mixin's state but serialising writes at the
    call site, not inside this module) serialises writers so only one
    connection writes the file at a time (avoids SQLITE_BUSY). Reads take no
    lock.

INTER-COPY DIFFERENCE: sqlite_vec_store.py's ``_new_conn()`` additionally
    enables extension loading, loads the ``sqlite_vec`` extension, and sets
    ``PRAGMA busy_timeout=5000`` (cross-process writer tolerance for its vec0
    table) — local_graph_store.py / local_sql_doc_store.py do neither. This
    is captured by the ``_configure_connection()`` hook below, which
    ``SqliteVecStore`` overrides; the hook runs AFTER the WAL/synchronous
    pragmas (the original ran the extension load BEFORE them) — this reorder
    is behaviorally inert since extension loading and journal-mode/sync
    pragmas are independent SQLite session settings.

``_require_available()`` mirrors the PG stores' existing pattern (see e.g.
    ``pg_graph_store.PGGraphStore._require_available``): message convention
    is ``"<ClassName> is not available."``, using ``type(self).__name__`` so
    each adopter gets its own class name without overriding the method.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


class _SqliteConnMixin:
    """Thread-local sqlite3 connection scaffolding shared by the SQLite-backed
    stores. Subclasses call ``_init_conn_state(db_path)`` once from
    ``__init__`` before any ``_conn`` access, and may override
    ``_configure_connection()`` to run extra per-connection setup.
    """

    _db_path: str
    _local: threading.local
    _conns_lock: threading.Lock
    _all_conns: list[Any]
    _lock: threading.Lock

    def _init_conn_state(self, db_path: str) -> None:
        """스레드-로컬 커넥션 + 쓰기 직렬화 락 초기화. __init__에서 1회 호출."""
        self._db_path = db_path
        # 쓰기 직렬화 락: 스레드별 커넥션이 같은 WAL 파일에 동시에 쓰면 SQLITE_BUSY가
        # 나므로, 프로세스 내에서는 한 번에 한 writer만 쓰도록 직렬화한다.
        self._lock = threading.Lock()
        # 스레드-로컬 커넥션: 단일 sqlite3 커넥션을 여러 스레드가 공유하면 읽기조차
        # "API misuse"로 깨진다. 스레드마다 자기 커넥션을 쓰면 WAL이 reader/writer를
        # 격리해 읽기는 락 없이 동시 진행된다.
        self._local = threading.local()
        # Transaction ownership is per worker thread.  Keeping this beside
        # the thread-local connection prevents one writer from making every
        # concurrent writer look like a nested transaction.
        self._graph_tx_state = threading.local()
        self._conns_lock = threading.Lock()
        self._all_conns = []

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """Hook for subclasses needing extra per-connection setup (e.g.
        SqliteVecStore's extension load + busy_timeout). No-op by default."""

    def _new_conn(self) -> sqlite3.Connection:
        """이 스레드 전용 커넥션을 생성한다.

        WAL: reader-writer를 격리해 쓰기 중에도 읽기를 허용. synchronous=NORMAL은
        WAL 체크포인트 시에만 fsync해 처리량을 높인다(단일 머신/NVMe에서 수용 가능).
        WAL은 DB 파일에 영속 설정되며 <db>-wal/<db>-shm가 생기므로 백업 시 함께 복사.
        """
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._configure_connection(conn)
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection | None:
        """현재 스레드의 커넥션(없으면 생성). 기존 메서드들이 self._conn.X 형태로
        그대로 쓰도록 property로 노출한다."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    @contextlib.contextmanager
    def _tx(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """쓰기 트랜잭션 경계. 쓰기 락을 쥔 채 커넥션을 내주고, with 블록이 예외 없이
        끝나면 commit, 예외가 나면 rollback 후 재던진다.

        WHY: 파이썬 sqlite3는 DML 앞에 암묵적으로 BEGIN을 걸어, 커밋 전 예외가 나면
        트랜잭션이 열린 채로 스레드 커넥션에 남는다(SQLAlchemy engine.begin()과 달리
        자동 롤백이 없다). 그 상태로 놔두면 같은 스레드의 다음 무관한 쓰기가 호출하는
        commit()에 앞서 실패한 배치의 부분 실행분까지 묻어 들어간다. 배치 쓰기 헬퍼는
        모두 이 컨텍스트 매니저를 거쳐야 그 틈이 막힌다.

        ``BaseException``까지 잡는다(``Exception``이 아니라): KeyboardInterrupt나
        SystemExit이 배치 도중 끼어들어도 항상 재던지므로(swallow하지 않음) 관찰
        가능한 동작은 그대로이고, 그 경로에서도 rollback이 실행되도록 보장만 넓힌다
        — "예외를 삼킨다"는 없이 불변식만 강화하는 변경이라 폭넓은 예외 포착의
        일반적 위험(에러 은폐)이 적용되지 않는다.

        rollback() 자체가 실패할 가능성(디스크 오류 등)은 별개로 대비한다: 그 경우도
        try/except로 감싸 로그만 남기고 원래 예외를 그대로 재던진다 — rollback 실패로
        원래 예외가 가려지면 호출자는 배치가 왜 실패했는지 영영 알 수 없게 된다.
        """
        with self._lock:
            conn = self._conn
            try:
                if immediate:
                    if conn.in_transaction:
                        raise RuntimeError("nested graph transaction is not allowed")
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except BaseException as rollback_exc:
                    logger.error(
                        "%s._tx: rollback failed (original exception still raised): %s",
                        type(self).__name__, rollback_exc,
                    )
                raise

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception as exc:
                    logger.debug("%s.close: connection close error: %s", type(self).__name__, exc)
            self._all_conns.clear()

    def _require_available(self) -> None:
        if not getattr(self, "_available", False):
            raise RuntimeError(f"{type(self).__name__} is not available.")
