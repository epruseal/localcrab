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
import os
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

    # 이슈 #94: SQLite 의 자동 체크포인트(PASSIVE)는 임계를 넘긴 커밋마다 다시
    # 시도되지만, 성공해도 WAL 파일을 0으로 되감지(TRUNCATE) 않는다 — 프레임
    # 위치만 재사용할 뿐 파일 크기는 유지된다. 이 저장소 어디에도 명시적
    # wal_checkpoint 호출이 없어, 장기 실행 프로세스는 WAL 이 한 번 임계를
    # 넘기면 프로세스가 살아있는 한(정확히는 마지막 커넥션이 닫힐 때까지)
    # 절대 줄어들지 않는다. 4MiB 는 SQLite 기본 페이지 크기(4096B) 기준
    # `wal_autocheckpoint` 기본값 1000페이지의 근사치다 — WAL 헤더/프레임
    # 오버헤드 때문에 정확한 페이지 수 일치는 아니며, 그 근방에서 보수적으로
    # (약간 이르게) 발동하는 근사치로 의도했다. 인스턴스 속성으로 오버라이드
    # 가능하도록 클래스 상수로 둔다(테스트에서 monkeypatch).
    _WAL_CHECKPOINT_THRESHOLD_BYTES: int = 4 * 1024 * 1024

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
        WAL은 DB 파일에 영속 설정되며 그 결과로 <db>-wal/<db>-shm가 함께 생긴다.
        백업 경로는 온라인 백업 API로 WAL을 통과해 읽으므로 이 사이드카 파일들을
        백업에 함께 복사해서는 안 된다. 자세한 내용은 opencrab/stores/backup.py 참고.
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
    def _tx(self, *, immediate: bool = False, exclusive: bool = False) -> Iterator[sqlite3.Connection]:
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

        commit 성공 뒤, 위 try/except BaseException 블록을 완전히 벗어난 자리에서
        ``_checkpoint_if_wal_large()`` 를 호출한다(이슈 #94). 그 자리를 고른 이유는
        구조적이다: 블록 안에 두면 체크포인트 보조 로직이 던지는 어떤 예외든
        ``except BaseException`` 이 가로채 이미 성공한 commit 에 대해 잘못된
        rollback 을 시도하고, 호출자에게는 쓰기 자체가 실패한 것처럼 보인다.
        """
        with self._lock:
            conn = self._conn
            try:
                if immediate and exclusive:
                    raise ValueError("immediate and exclusive transactions are mutually exclusive")
                if immediate:
                    if conn.in_transaction:
                        raise RuntimeError("nested graph transaction is not allowed")
                    conn.execute("BEGIN IMMEDIATE")
                elif exclusive:
                    if conn.in_transaction:
                        raise RuntimeError("nested graph transaction is not allowed")
                    conn.execute("BEGIN EXCLUSIVE")
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
            self._checkpoint_if_wal_large(conn)

    def _exec(self, conn: sqlite3.Connection, sql: str) -> sqlite3.Cursor:
        """``conn.execute(sql)`` 의 얇은 위임.

        체크포인트 보조 로직(``_checkpoint_if_wal_large``/``_restore_busy_timeout``)
        만 이 메서드를 거친다. ``sqlite3.Connection`` 은 C 로 구현된 불변
        타입이라 인스턴스·클래스 양쪽 모두 속성 재대입이 막혀 있어(런타임에
        따라 다르며, CPython 3.13 기준 클래스 속성도 재대입 불가) 테스트가
        실행 실패나 busy/인터럽트를 직접 주입할 자리가 없다. 이 메서드는
        일반 커밋 경로(``_tx()`` 본문)에는 쓰지 않는다 — 그쪽은 이 문제와
        무관하다.
        """
        return conn.execute(sql)

    def _checkpoint_if_wal_large(self, conn: sqlite3.Connection) -> None:
        """WAL 사이드카가 임계를 넘겼으면 명시적 TRUNCATE 체크포인트를 시도한다
        (이슈 #94, WAL 체크포인트 정책 부재).

        호출 위치가 중요하다: ``_tx()`` 의 ``try/except BaseException`` 블록을
        완전히 벗어난 뒤(즉 commit 이 이미 성공한 뒤)에만 불린다. 이 함수 안에서
        나는 어떤 예외든 그 블록 밖에 있으므로 ``except BaseException`` 의
        rollback 분기를 절대 타지 않는다 — 이미 확정된 커밋이 체크포인트발
        예외로 실패인 것처럼 보이는 오탐을 구조적으로 없앤다.

        매 커밋마다 무조건 불리지만 비용은 낮다: 임계 미만이면 `os.stat` 1회로
        끝난다(카운터 방식이 필요했던 이유인 "매 커밋마다 무거운 PRAGMA 를
        물지 않기"를 이 사전 검사가 대신한다).
        """
        try:
            wal_size = os.path.getsize(f"{self._db_path}-wal")
        except Exception as exc:
            # WAL 사이드카가 아직 없는 초기 상태(OSError)를 포함해, 이 조회
            # 단계에서 나는 어떤 Exception 도 방금 성공한 커밋을 실패로
            # 보이게 해서는 안 된다(받아들임 기준 4) — 조용히 반환한다.
            logger.debug(
                "%s._checkpoint_if_wal_large: WAL size probe failed, skipping: %s",
                type(self).__name__, exc,
            )
            return
        if wal_size < self._WAL_CHECKPOINT_THRESHOLD_BYTES:
            return

        try:
            old_timeout = self._exec(conn, "PRAGMA busy_timeout").fetchone()[0]
            # 열린 리더가 있으면 체크포인트 호출 자체가 이 커넥션의 busy_timeout
            # 만큼(기본 5000ms) 멈춘다 — 그동안 낮춰 즉시 busy 여부만 받는다.
            self._exec(conn, "PRAGMA busy_timeout=0")
        except Exception as exc:
            logger.debug(
                "%s._checkpoint_if_wal_large: busy_timeout probe failed, skipping: %s",
                type(self).__name__, exc,
            )
            return

        try:
            row = self._exec(conn, "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and row[0]:
                # busy=1: 다른 커넥션이 실제 열린 스냅샷을 쥐고 있다 — WAL 의
                # 근본 제약이지 이 정책의 결함이 아니다. 다음 커밋에서 재시도.
                logger.debug(
                    "%s._checkpoint_if_wal_large: checkpoint busy, will retry on next commit",
                    type(self).__name__,
                )
        except Exception as exc:
            # 체크포인트는 성능 최적화이지 쓰기 계약의 일부가 아니다 — 어떤
            # 실행 실패도 커밋의 성공 여부에 영향을 주지 않는다.
            logger.debug(
                "%s._checkpoint_if_wal_large: checkpoint execution failed: %s",
                type(self).__name__, exc,
            )
        finally:
            self._restore_busy_timeout(conn, old_timeout)

    def _restore_busy_timeout(self, conn: sqlite3.Connection, old_timeout: int) -> None:
        """체크포인트 시도 동안 낮춘 busy_timeout 을 원래 값으로 되돌린다.

        ``except BaseException`` 으로 감싼다(``Exception`` 이 아니라): 복원
        PRAGMA 자체가 실패하면(예: 몽키패치, 극히 드문 이상) 그 커넥션의
        busy_timeout 이 0인 채로 남아 이후 이 스레드의 일반 쓰기가 동시성
        재시도 여유(교차 프로세스 쓰기 포함, SqliteVecStore 의 계약)를 조용히
        잃는다 — "다음 쓰기에서 정상 오류로 드러난다"는 전제는 성립하지
        않는다(복원 실패가 커넥션 손상을 보장하지 않으므로). 그래서 예외
        종류와 무관하게 먼저 커넥션을 폐기·재생성 대상으로 만든 뒤, 삼켜도
        되는 ``Exception`` 이면 경고만 남기고 반환하고, 인터럽트 계열의
        순수 ``BaseException`` 이면 정리를 마친 뒤 그대로 재던진다(삼키지
        않는다는 ``_tx()`` 의 기존 불변식과 같은 원칙).
        """
        try:
            self._exec(conn, f"PRAGMA busy_timeout={int(old_timeout)}")
        except BaseException as exc:
            self._discard_conn(conn)
            if isinstance(exc, Exception):
                logger.warning(
                    "%s._restore_busy_timeout: restore failed, discarded connection: %s",
                    type(self).__name__, exc,
                )
                return
            raise

    def _discard_conn(self, conn: sqlite3.Connection) -> None:
        """오염된(busy_timeout 복원 실패) 스레드-로컬 커넥션을 폐기한다.

        다음에 같은 스레드가 ``self._conn`` 에 접근하면 ``_new_conn()`` 이 새
        커넥션을 만들며, 그 경로가 ``_configure_connection()``(SqliteVecStore
        의 busy_timeout=5000 포함)과 sqlite3 기본 타임아웃을 다시 정상
        적용하므로 계약이 그 시점에 회복된다. ``_all_conns`` 목록 변경은
        ``_new_conn()``/``close()`` 와 같은 ``_conns_lock`` 으로 보호한다 —
        쓰기 직렬화 락(``self._lock``)과는 별개의 불변식이다.
        """
        try:
            conn.close()
        except Exception as exc:
            logger.debug("%s._discard_conn: close error: %s", type(self).__name__, exc)
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
        with self._conns_lock:
            if conn in self._all_conns:
                self._all_conns.remove(conn)

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception as exc:
                    logger.debug("%s.close: connection close error: %s", type(self).__name__, exc)
            self._all_conns.clear()

    @contextlib.contextmanager
    def _bootstrap_lock(self) -> Iterator[None]:
        """스키마 부트스트랩(DDL)을 이 스토어 파일이 있는 디렉터리의
        write.lock 으로 감싼다(issue #141 항목 2).

        스토어 생성자는 Settings 객체가 아니라 파일 경로(``self._db_path``)만
        알므로, 잠금 디렉터리도 프로세스 기본 데이터 디렉터리가 아니라 그
        파일 자신의 부모 디렉터리에서 구한다 — 마이그레이션 스크립트와
        테스트는 흔히 프로세스 기본과 다른 데이터 디렉터리를 대상으로 돈다.
        ``write_lock`` 은 스레드·경로 기준으로 재진입 가능하므로(``opencrab
        .locking.file_lock``), 이미 같은 디렉터리의 write.lock 을 쥔 진입점
        (``ingest``/``extract`` 등) 안에서 스토어가 생성돼도 추가 대기 없이
        곧바로 진행된다.
        """
        from opencrab.locking import write_lock

        data_dir = os.path.dirname(os.path.abspath(self._db_path))
        with write_lock(data_dir):
            yield

    def _require_available(self) -> None:
        if not getattr(self, "_available", False):
            raise RuntimeError(f"{type(self).__name__} is not available.")
