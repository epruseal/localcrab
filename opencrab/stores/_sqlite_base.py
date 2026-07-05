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

import logging
import sqlite3
import threading
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
