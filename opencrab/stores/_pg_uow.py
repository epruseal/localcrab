"""PostgreSQL 논리 쓰기의 연결과 저장점을 관리한다."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

from opencrab.stores._single_file import RollbackOnlyError

logger = logging.getLogger(__name__)


def common_pg_uow(*stores: Any) -> PgUnitOfWork | None:
    """모든 저장소가 동일한 명시적 PG 작업 단위를 쓰는지 확인한다."""
    units = [getattr(store, "_pg_uow", None) for store in stores]
    actual = [unit for unit in units if isinstance(unit, PgUnitOfWork)]
    if not actual:
        return None
    if any(unit is not actual[0] for unit in units):
        raise RuntimeError("stores do not share the same PostgreSQL unit of work")
    return actual[0]


class PgUnitOfWork:
    """한 엔진에서 스레드별 외부 트랜잭션을 관리한다."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._state = threading.local()

    @property
    def active(self) -> bool:
        return getattr(self._state, "connection", None) is not None

    def mark_rollback_only(self) -> None:
        if not self.active:
            raise RuntimeError("PostgreSQL unit of work is not active")
        self._state.rollback_only = True

    def after_commit(self, callback: Callable[[], None]) -> None:
        if not self.active:
            raise RuntimeError("PostgreSQL unit of work is not active")
        self._state.after_commit.append(callback)

    @contextlib.contextmanager
    def unit_of_work(self, *, outcome: dict | None = None) -> Iterator[Any]:
        if self.active:
            try:
                yield self._state.connection
            except BaseException:
                self.mark_rollback_only()
                raise
            return
        body_error = None
        try:
            with self.engine.begin() as conn:
                self._state.connection = conn
                self._state.rollback_only = False
                self._state.after_commit = []
                try:
                    yield conn
                    if self._state.rollback_only:
                        raise RollbackOnlyError("PostgreSQL unit of work is rollback-only")
                    callbacks = self._state.after_commit
                except BaseException as exc:
                    body_error = exc
                    raise
                finally:
                    del self._state.connection
                    del self._state.rollback_only
                    del self._state.after_commit
        except BaseException as exc:
            if outcome is not None and body_error is not None and exc is body_error:
                outcome["state"] = "rolled_back"
            raise
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("PgUnitOfWork: post-commit callback failed")

    @contextlib.contextmanager
    def connection(self, *, write: bool = False) -> Iterator[Any]:
        if self.active:
            conn = self._state.connection
            if write:
                try:
                    nested = conn.begin_nested()
                except BaseException:
                    self.mark_rollback_only()
                    raise
                try:
                    yield conn
                except BaseException:
                    try:
                        nested.rollback()
                    except BaseException:
                        self.mark_rollback_only()
                        raise
                    raise
                try:
                    nested.commit()
                except BaseException:
                    self.mark_rollback_only()
                    raise
            else:
                yield conn
        else:
            manager = self.engine.begin() if write else self.engine.connect()
            with manager as conn:
                yield conn
