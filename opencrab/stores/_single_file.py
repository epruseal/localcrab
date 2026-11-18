"""단일 SQLite 파일 위에서 여러 저장소가 연결 하나와 트랜잭션 하나를 나눠 쓰게 한다 (#671 시제품).

기본은 꺼져 있다. 호출자가 ``SharedSqliteFile``을 하나 만들어 세 저장소(``LocalGraphStore``,
``LocalSQLDocStore``, ``SqliteVecStore``)에 ``shared=``로 넘길 때만 쓰인다. 넘기지 않으면
저장소는 종전처럼 자기 파일과 자기 연결을 쓴다. factory와 설정은 이 시제품에 연결하지 않았다
(전환 설계 3단계 항목).

이 모듈이 맡는 것은 둘뿐이다.

1. 한 파일 경로마다 스레드별 연결 하나와 쓰기 락 하나를 그 파일을 쓰는 모든 저장소에
   나눠 준다(``SharedSqliteFile``).
2. 한 논리 쓰기를 한 트랜잭션으로 묶는 구간을 준다(``unit_of_work``). 구간 안에서
   저장소의 ``_tx()``는 자기 BEGIN과 commit을 하지 않고 SAVEPOINT로 합류한다. 안쪽
   쓰기가 실패하면 그 구간만 되돌리고 예외를 올린다. 예외가 구간 밖으로 나가면 구간
   전체를 롤백한다.

한계(시제품):

- ``SQLStore``(registry, SQLAlchemy)는 이 연결을 쓰지 않는다. registry 쓰기는 구간에
  들어가지 않는다.
- 구간은 ``BEGIN IMMEDIATE``로 쓰기 락을 먼저 잡는다. 구간 안에서 임베딩을 계산하면
  그 시간 동안 락을 쥔다. 임베딩을 구간 밖으로 빼는 일은 3단계 설계에 있다
  (``docs/issue-671-single-db-conversion.md``).
- 락은 같은 스레드에서 재진입한다. 프로세스 사이 직렬화는 종전처럼 ``write.lock``이 맡는다.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from typing import Any

logger = logging.getLogger(__name__)

ConnHook = Callable[[sqlite3.Connection], None]


class SharedSqliteFile:
    """한 SQLite 파일의 연결과 쓰기 락을 나눠 쓰는 저장소 묶음.

    호출자가 파일마다 객체 하나를 만들어 그 파일을 쓰는 저장소들에 넘긴다. 저장소는 생성할 때
    ``acquire``하고 닫을 때 ``release``한다. 마지막 저장소가 ``release``하면 연결을 닫는다.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        self.local = threading.local()
        self.lock = threading.RLock()
        self.conns_lock = threading.Lock()
        self.all_conns: list[sqlite3.Connection] = []
        self._hooks: list[ConnHook] = []
        self._uow = threading.local()
        self._users: list[Any] = []
        self._checkpointer: Any | None = None

    # ------------------------------------------------------------------
    # 저장소 등록
    # ------------------------------------------------------------------

    def acquire(self, owner: Any, hook: ConnHook | None) -> None:
        """저장소를 등록한다. ``hook``은 그 저장소가 연결에 거는 추가 설정이다.

        이미 열린 연결에도 즉시 적용한다. 먼저 만든 저장소의 ``_init_db``가 연결을 열고
        나중에 만든 저장소(예: vec0 확장이 필요한 벡터 저장소)가 같은 연결을 써야 하기 때문이다.
        """
        with self.conns_lock:
            self._users.append(owner)
            if self._checkpointer is None:
                self._checkpointer = owner
            apply_to: list[sqlite3.Connection] = []
            if hook is not None and hook not in self._hooks:
                self._hooks.append(hook)
                apply_to = list(self.all_conns)
        for conn in apply_to:
            hook(conn)

    def release(self, owner: Any) -> None:
        """저장소를 뺀다. 마지막 저장소면 연결을 닫는다."""
        with self.conns_lock:
            if owner in self._users:
                self._users.remove(owner)
            if self._checkpointer is owner:
                self._checkpointer = self._users[0] if self._users else None
            last = not self._users
        if not last:
            return
        with self.conns_lock:
            for conn in self.all_conns:
                try:
                    conn.close()
                except Exception as exc:  # noqa: BLE001 -- 닫기 실패는 기록만 한다
                    logger.debug("SharedSqliteFile.release: close error: %s", exc)
            self.all_conns.clear()

    # ------------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------------

    def new_conn(self) -> sqlite3.Connection:
        """연결을 새로 만든다. 모든 등록 저장소의 설정 훅이 한 연결에 합쳐서 걸린다."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self.conns_lock:
            hooks = list(self._hooks)
            self.all_conns.append(conn)
        for hook in hooks:
            hook(conn)
        return conn

    def connection(self) -> sqlite3.Connection:
        """현재 스레드의 연결(없으면 생성)."""
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = self.new_conn()
            self.local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # 트랜잭션 구간
    # ------------------------------------------------------------------

    @property
    def in_unit_of_work(self) -> bool:
        return getattr(self._uow, "depth", 0) > 0

    @contextlib.contextmanager
    def unit_of_work(self) -> Iterator[sqlite3.Connection]:
        """한 논리 쓰기를 한 트랜잭션으로 묶는다.

        바깥 구간이 ``BEGIN IMMEDIATE``와 commit(또는 rollback)을 맡는다. 같은 스레드에서
        중첩해 부르면 바깥 구간에 합류하고 자기 commit은 하지 않는다. 예외는 삼키지 않는다.
        """
        with self.lock:
            conn = self.connection()
            depth = getattr(self._uow, "depth", 0)
            if depth == 0:
                if conn.in_transaction:
                    raise RuntimeError("unit_of_work cannot start inside an open transaction")
                conn.execute("BEGIN IMMEDIATE")
            self._uow.depth = depth + 1
            try:
                yield conn
            except BaseException:
                self._uow.depth = depth
                if depth == 0:
                    try:
                        conn.rollback()
                    except BaseException as rollback_exc:
                        logger.error(
                            "SharedSqliteFile.unit_of_work: rollback failed "
                            "(original exception still raised): %s", rollback_exc,
                        )
                raise
            self._uow.depth = depth
            if depth == 0:
                try:
                    conn.commit()
                except BaseException:
                    # commit이 실패하면 열린 트랜잭션이 연결에 남지 않게 되돌린다.
                    try:
                        conn.rollback()
                    except BaseException as rollback_exc:
                        logger.error(
                            "SharedSqliteFile.unit_of_work: rollback after failed commit: %s",
                            rollback_exc,
                        )
                    raise
        if depth == 0 and self._checkpointer is not None:
            # commit 뒤의 보조 작업. 실패해도 이미 확정된 커밋에 영향을 주지 않는다.
            self._checkpointer._checkpoint_if_wal_large(conn)

    @contextlib.contextmanager
    def joined_tx(self) -> Iterator[sqlite3.Connection]:
        """구간 안에서 저장소의 ``_tx()``가 쓰는 합류 트랜잭션(SAVEPOINT)."""
        conn = self.connection()
        conn.execute("SAVEPOINT tx_join")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT tx_join")
                conn.execute("RELEASE SAVEPOINT tx_join")
            except BaseException as rollback_exc:
                logger.error(
                    "SharedSqliteFile.joined_tx: savepoint rollback failed "
                    "(original exception still raised): %s", rollback_exc,
                )
            raise
        conn.execute("RELEASE SAVEPOINT tx_join")


def _common_shared_file(stores: tuple[Any, ...]) -> SharedSqliteFile | None:
    first = getattr(stores[0], "shared_file", None) if stores else None
    if not isinstance(first, SharedSqliteFile):
        return None
    for store in stores[1:]:
        if getattr(store, "shared_file", None) is not first:
            return None
    return first  # type: ignore[no-any-return]


def is_single_file(*stores: Any) -> bool:
    """저장소들이 모두 같은 SharedSqliteFile을 나눠 쓰는가.

    공유하지 않는 저장소(PG, 파일이 따로인 SQLite, 시험용 대역)는 False이며 호출자는
    종전 경로를 탄다.
    """
    return _common_shared_file(stores) is not None


def unit_of_work_for(*stores: Any) -> contextlib.AbstractContextManager[Any]:
    """저장소들이 모두 같은 SharedSqliteFile을 쓰면 그 구간을, 아니면 빈 구간을 준다."""
    shared = _common_shared_file(stores)
    if shared is None:
        return contextlib.nullcontext()
    return shared.unit_of_work()
