#!/usr/bin/env python3
"""시제품: local 모드의 SQLite 파일 여럿을 한 파일로 합친다 (#671).

대상은 graph.db, doc_store.db, vectors.db, opencrab.db 네 파일이다. billing.db는 대상이
아니다. billing_events와 idx_billing_tenant가 opencrab.db와 이름이 겹쳐 합칠 수 없고, 이름을
바꿔야 하는 병합은 전환 설계 4단계의 항목이다. 두 번째 파일이 같은 이름의 테이블이나 인덱스를
가지면 합치지 않고 중단한다.

원본은 읽기 전용으로만 연다. 결과는 ``<out>.partial``에 만들고 덮어쓰기 없이
``<out>``으로 게시한다. ``--verify``를 주면 검증을 통과해야 게시한다. ``--verify`` 없이 ``--apply``만 주면
검증 없이 게시하므로 운영에서는 ``--verify``를 함께 준다. 중단되면 ``.partial``이 남는다.
다음 실행은 그것을 거부한다. 중단된 산출물은 보존하고 새 경로에서 다시 실행한다.

WAL 모드 원본을 읽기 전용으로 열어도 SQLite가 원본 옆에 빈 ``-wal``과 ``-shm`` 곁파일을
만들 수 있다. 원본 데이터는 바뀌지 않는다. ``--immutable-source``를 주면 곁파일도 만들지
않지만 WAL이 남은 원본에서는 오래된 내용을 읽으므로 WAL이 없는 정지된 스냅숏에만 쓴다.

사용법(계획만 출력한다. 출력 파일을 만들지 않는다):
    python merge_sqlite_stores.py --source-dir <디렉터리> --out <새 파일>

실제 병합:
    python merge_sqlite_stores.py --source-dir <디렉터리> --out <새 파일> --apply --verify

복사 방식: 원본을 ATTACH하고 테이블마다 INSERT ... SELECT를 한다. fts5와 vec0
가상 테이블은 원본의 CREATE VIRTUAL TABLE 문으로 만든 뒤 그림자 테이블의
내용을 덮어쓴다. 인덱스와 트리거는 데이터를 옮긴 뒤 만든다. 대량 복사 중에는
출력에 journal_mode=OFF와 synchronous=OFF를 건다. 복사가 끝나면 WAL 모드로 바꾸고
(``--verify``를 줬다면 검증을 통과한 뒤) 파일 fsync, 새 이름 연결, 디렉터리 fsync 순서로 게시한다.

안전 점검(운영 장치에서 돌리는 도구이므로 시작 전과 복사 중에 건다):

- 임시 디렉터리와 출력 경로가 tmpfs나 ramfs이면 거부한다(메모리를 쓴다).
- 필요량 추정(원본 합계 x 1.3)을 빼고도 여유가 하한(``--min-free-gib``, 기본 30)보다 작으면 시작하지 않는다.
- 테이블 하나를 복사할 때마다 여유를 보고, 하한 아래로 내려가거나 디스크가 차면 중단한다.

종료 코드: 0 성공, 1 병합이나 검증 실패, 2 사용법과 사전 점검 실패(이름 충돌 포함),
3 디스크 여유 하한 위반이나 디스크 가득 참이다. 중단된 부분 파일은 보존한다.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import resource
import shutil
import sqlite3
import stat
import sys
import time
import threading
import urllib.parse
from pathlib import Path
from typing import Any

FILES = ("graph.db", "doc_store.db", "vectors.db", "opencrab.db")
GIB = 1024 ** 3
MERGE_SPACE_FACTOR = 1.3  # 원본 합계에 곱한다. 인덱스 구축 여유를 포함한다.
RAM_FS_TYPES = {"tmpfs", "ramfs"}

# 그림자 테이블 접미사. 인식하지 못한 접미사를 가진 테이블은 일반 테이블로 취급하지 않고
# 중단한다(그림자인지 일반 테이블인지 모르는 채 덮어쓰면 데이터가 어긋난다).
_SHADOW_SUFFIXES = {
    "fts5": {"data", "idx", "content", "docsize", "config"},
    "vec0": {"chunks", "rowids", "info", "auxiliary"},
}
_SHADOW_PATTERNS = {
    "vec0": re.compile(r"^(vector_chunks|metadatachunks|metadatatext)\d+$"),
}
_MODULE_RE = re.compile(r"USING\s+(\w+)", re.IGNORECASE)


class MergeError(RuntimeError):
    """병합이나 사전 점검의 실패. 메시지에 원인을 담는다."""


class DiskFloorError(MergeError):
    """디스크 여유가 하한 아래이거나 디스크가 가득 찼다. 종료 코드 3."""


_publication_owners: dict[str, Any] = {}
_publication_owners_lock = threading.RLock()
_publication_current = threading.local()

# fd 역할의 분류. 분류는 생성 지점이 역할 이름과 함께 고정하고, 어긋난 명시 지정은 부작용 전에 거부한다.
_FD_EXACT = {
    "source-lock": "source-lock", "source-parent": "source-parent", "root-lock": "root",
    "input-root": "consumer", "manifest-parent": "consumer", "evidence-parent": "consumer",
    "manifest-file": "consumer", "evidence-file": "consumer",
    "mounts": "other", "intent": "other", "intent-parent": "other", "staging-parent": "other",
    "staging-dir": "other", "merge-partial": "other", "checkpoint-fsync": "other",
    "billing-write": "other", "billing-read": "other",
    "probe-dir": "other", "manifest-write": "other", "marker-write": "other", "ledger-write": "other",
    "lock-create": "other", "staging-seal": "other", "intent-tmp": "other", "publish-parent": "other",
    "root-dir": "other", "root-file": "other", "ledger-tmp": "other",
}
_FD_PREFIX = (("source:", "consumer"), ("directory:", "consumer"))
_FD_CATEGORIES = {"source-lock", "source-parent", "root", "consumer", "other", "independent"}


def _classify_fd_role(role: str) -> str | None:
    base = role.split("#", 1)[0]
    if base in _FD_EXACT:
        return _FD_EXACT[base]
    for prefix, category in _FD_PREFIX:
        if base.startswith(prefix):
            return category
    return None


class _PublicationFlockBeforeTest(Exception):
    pass


class _PublicationCloseBeforeTest(Exception):
    def __init__(self, retry: Any) -> None:
        super().__init__("test close before SQLite")
        self.retry = retry


class _PublicationWork:
    """게시 context body가 명시 인자로 전달하는 사용 권한. context 종료 뒤에는 무효다."""

    __slots__ = ("owner", "active")

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.active = False


class _PublicationToken:
    """owner가 mutex 아래 발급하는 단계 권한. kind마다 한 번에 하나만 활성이다."""

    __slots__ = ("kind", "owner", "active", "key", "stream")

    def __init__(self, kind: str, owner: Any) -> None:
        self.kind = kind
        self.owner = owner
        self.active = True
        self.key: str | None = None
        self.stream: Any = None


class _PublicationOwner:
    def __init__(self, root: Path) -> None:
        self.root = os.path.abspath(root)
        self.connections: list[Any] = []
        self.fd_slots: dict[str, int | None] = {}
        self.fd_roles: dict[str, list[str]] = {}
        self.fd_category: dict[str, str | None] = {}
        self.uncertain = False
        self.root_lock: Any = None
        self.root_lock_state = "absent"
        self.root_registry_state = "absent"
        self.root_raw_state = "absent"
        self.root_raw_key: str | None = None
        self.source_state = "absent"
        self.deferred_fds: list[str] = []
        self.cleanup_started = False
        self.source_locked = False
        self.defer_root_release = False
        self.cleanup_error: BaseException | None = None
        self.streams: dict[str, Any] = {}
        self.stream_roles: dict[str, list[str]] = {}
        self.connection_cleanup_started = False
        self.effect_observations: list[dict[str, bool | None]] = []
        self.error_log: list[tuple[str, Any, BaseException, str]] = []
        # 짧은 상태 mutex. 판정과 기록만 감싸고 os, SQLite, stream, flock 호출 동안에는 잡지 않는다.
        self.state_lock = threading.Lock()
        self.work: _PublicationWork | None = None
        self.work_active = False
        self.tokens: dict[str, _PublicationToken] = {}
        self.inflight = 0
        self.pending = False

    # ----- 진입 규칙 -----

    def _reject(self, reason: str) -> Any:
        raise MergeError(f"publication owner rejects the call: {reason}")

    def _check_work_locked(self, work: _PublicationWork | None) -> None:
        if work is not None and (work is not self.work or not work.active):
            self._reject("work capability is not valid")
        if self.work_active and work is None:
            self._reject("work capability is missing")

    def _new_token_locked(self, kind: str) -> _PublicationToken:
        token = _PublicationToken(kind, self)
        self.tokens[kind] = token
        return token

    def _end(self, token: _PublicationToken) -> None:
        with self.state_lock:
            token.active = False
            if self.tokens.get(token.kind) is token:
                del self.tokens[token.kind]

    def _check_token(self, token: _PublicationToken | None, kinds: tuple[str, ...]) -> _PublicationToken:
        with self.state_lock:
            if (token is None or token.owner is not self or not token.active
                    or self.tokens.get(token.kind) is not token or token.kind not in kinds):
                self._reject("token is not valid")
        return token

    @contextlib.contextmanager
    def _use(self, work: _PublicationWork | None, token: _PublicationToken | None = None,
             kinds: tuple[str, ...] = ("acquire", "q1")):
        """자원 획득과 정상 사용의 호출 하나를 in-flight로 예약한다. 단계 권한이 있으면 상위 예약을 따른다."""
        if token is not None:
            with self.state_lock:
                if (token.owner is not self or not token.active or token.kind not in kinds
                        or (token.kind != "acquire" and self.tokens.get(token.kind) is not token)):
                    self._reject("token is not valid")
            yield token
            return
        with self.state_lock:
            self._check_work_locked(work)
            if self.pending:
                self._reject("termination is pending")
            if self.tokens:
                self._reject(f"{next(iter(self.tokens))} is in progress")
            self.inflight += 1
            reservation = _PublicationToken("acquire", self)
        try:
            yield reservation
        finally:
            with self.state_lock:
                reservation.active = False
                self.inflight -= 1

    def _begin_direct(self, kind: str) -> _PublicationToken | None:
        """work 없는 IDLE owner의 직접 정리 권한. 금지 장벽이면 None을 돌려주고 부작용이 없다."""
        with self.state_lock:
            if self.work_active:
                self._reject("work is active")
            if self.tokens:
                self._reject(f"{next(iter(self.tokens))} is in progress")
            if self.inflight:
                self._reject("an acquisition is in flight")
            if self.pending or self.uncertain or self.connections:
                return None
            return self._new_token_locked(kind)

    def record(self, stage: str, instance: Any, error: BaseException, kind: str) -> None:
        self.error_log.append((stage, instance, error, kind))

    # ----- fd -----

    def _resolve_category(self, role: str, category: str | None) -> str | None:
        if category is not None and category not in _FD_CATEGORIES:
            raise MergeError("publication fd category is unknown")
        fixed = _classify_fd_role(role)
        if fixed is not None and category is not None and category != fixed:
            raise MergeError("publication fd category differs from the fixed classification")
        return category if category is not None else fixed

    def open_fd(self, role: str, path: Any, flags: int, mode: int = 0o777,
                *, dir_fd: int | None = None, work: _PublicationWork | None = None,
                category: str | None = None, token: _PublicationToken | None = None) -> int:
        resolved = self._resolve_category(role, category)
        with self._use(work, token):
            instances = self.fd_roles.setdefault(role, [])
            key = role if not instances else f"{role}#{len(instances)}"
            instances.append(key)
            self.fd_slots[key] = None
            self.fd_category[key] = resolved
            try:
                fd = os.open(path, flags, mode, dir_fd=dir_fd)
            except BaseException:
                self.uncertain = True
                if flags & os.O_CREAT and dir_fd is None:
                    try:
                        exists = _publication_exists(Path(path))
                    except OSError:
                        exists = None
                    self.effect_observations.append({str(path): exists})
                raise
            self.fd_slots[key] = fd
            self.deferred_fds.append(key)
            if resolved == "source-lock":
                self.source_state = "unattempted"
            return fd

    def adopt_fd(self, role: str, fd: int, *, category: str | None = None,
                 work: _PublicationWork | None = None) -> None:
        if category is None:
            raise MergeError("publication fd adoption requires an explicit category")
        resolved = self._resolve_category(role, category)
        with self._use(work):
            if role in self.fd_slots:
                raise MergeError("publication fd role already exists")
            self.fd_slots[role] = fd
            self.fd_category[role] = resolved
            self.deferred_fds.append(role)

    def _others_closed(self) -> bool:
        for key, slot in self.fd_slots.items():
            if self.fd_category.get(key) in ("source-lock", "source-parent", "root"):
                continue
            if slot is not None:
                return False
        return not any(stream is not None for stream in self.streams.values())

    def _root_complete(self) -> bool:
        return (self.root_raw_state in ("absent", "closed", "transferred") and self.root_lock is None
                and self.root_lock_state in ("absent", "closed")
                and self.root_registry_state in ("absent", "removed"))

    def _check_fd_prerequisites(self, key: str, internal: bool) -> None:
        category = self.fd_category.get(key)
        if category is None and not internal:
            raise MergeError("publication fd role is not classified")
        if category == "root":
            raise MergeError("publication root fd is released through the root lock")
        if not self._root_complete():
            raise MergeError("publication root release is incomplete")
        if category in ("source-lock", "source-parent"):
            if not self._others_closed():
                raise MergeError("publication fds are still held")
            if category == "source-lock" and self.source_state not in ("unlocked", "not-acquired", "unattempted"):
                raise MergeError("publication source lock is not released")
            if category == "source-parent" and self.source_state not in ("closed", "absent"):
                raise MergeError("publication source lock is not closed")
        elif category == "independent" and self.source_state not in ("absent", "not-acquired", "closed"):
            raise MergeError("publication source lock is held")

    def _close_fd_impl(self, key: str, fd: int, internal: bool) -> None:
        if self.uncertain or self.connections:
            return
        self._check_fd_prerequisites(key, internal)
        try:
            os.close(fd)
        except BaseException as exc:
            self.uncertain = True
            if self.fd_category.get(key) == "source-lock":
                self.source_state = "uncertain"
            self.record("fd-close", key, exc, "first")
            raise
        self.fd_slots[key] = None
        if self.fd_category.get(key) == "source-lock":
            self.source_state = "closed"

    def close_fd(self, role: str, fd: int | None = None, *, work: _PublicationWork | None = None,
                 token: _PublicationToken | None = None) -> None:
        if work is not None:
            with self.state_lock:
                self._check_work_locked(work)
        keys = self.fd_roles.get(role, [role])
        if fd is None:
            key = keys[-1]
        else:
            key = next((key for key in keys if self.fd_slots.get(key) == fd), None)
        fd = self.fd_slots.get(key)
        if fd is None:
            return
        if token is not None:
            self._check_token(token, ("cleanup",))
            self._close_fd_impl(key, fd, True)
            return
        if not self.cleanup_started:
            with self._use(work):
                if key not in self.deferred_fds:
                    self.deferred_fds.append(key)
            return
        direct = self._begin_direct("release")
        if direct is None:
            return
        try:
            self._close_fd_impl(key, fd, False)
        finally:
            self._end(direct)

    def flock(self, fd: int, operation: int, *, work: _PublicationWork | None = None,
              token: _PublicationToken | None = None) -> None:
        source_fd = self.fd_slots.get("source-lock")
        is_source = source_fd is not None and fd == source_fd
        if operation & fcntl.LOCK_UN:
            if not is_source:
                raise MergeError("publication unlock is limited to the source lock")
            if token is not None:
                self._check_token(token, ("cleanup",))
                self._unlock_source_impl(fd)
                return
            direct = self._begin_direct("release")
            if direct is None:
                return
            try:
                self._unlock_source_impl(fd)
            finally:
                self._end(direct)
            return
        with self._use(work, token):
            try:
                fcntl.flock(fd, operation)
            except _PublicationFlockBeforeTest:
                if is_source:
                    self.source_state = "not-acquired"
                raise
            except BaseException:
                self.uncertain = True
                if is_source:
                    self.source_state = "uncertain"
                raise
            if is_source and operation & fcntl.LOCK_SH:
                self.source_state = "locked"
                self.source_locked = True

    def _unlock_source_impl(self, fd: int) -> None:
        if self.uncertain or self.connections:
            return
        if not self._root_complete() or not self._others_closed():
            raise MergeError("publication source unlock prerequisites are not met")
        if self.source_state != "locked":
            if self.source_state in ("not-acquired", "unattempted"):
                return
            raise MergeError("publication source lock is not held")
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException as exc:
            self.uncertain = True
            self.source_state = "uncertain"
            self.record("source-unlock", fd, exc, "first")
            raise
        self.source_state = "unlocked"
        self.source_locked = False

    # ----- 연결 -----

    def connect(self, *args: Any, work: _PublicationWork | None = None, **kwargs: Any) -> Any:
        with self._use(work):
            slot = _PublicationConnection(self, None)
            self.connections.append(slot)
            try:
                conn = sqlite3.connect(*args, **kwargs)
            except BaseException:
                slot.failed = True
                self.uncertain = True
                raise
            slot.conn = conn
            return slot

    def register(self, conn: sqlite3.Connection, *, work: _PublicationWork | None = None) -> Any:
        with self._use(work):
            wrapped = _PublicationConnection(self, conn)
            self.connections.append(wrapped)
            return wrapped

    def _collect(self, token: _PublicationToken) -> None:
        """남은 연결을 전량 수집한다. 연결 하나가 실패해도 나머지를 계속 수집한다."""
        failure = None
        for conn in list(self.connections):
            if conn.failed:
                continue
            try:
                conn.close(token=token)
            except _PublicationCloseBeforeTest as exc:
                self.record("connection-close", conn, exc, "test-first")
                try:
                    exc.retry()
                    conn.close(token=token)
                except BaseException as retry_error:
                    self.uncertain = True
                    self.record("connection-close", conn, retry_error, "test-retry")
                    if failure is None:
                        failure = retry_error
            except BaseException as exc:
                self.record("connection-close", conn, exc, "first")
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def close_remaining(self) -> None:
        """work 없는 IDLE owner의 직접 최초 수집. 연결만 수집하고 fd와 잠금은 해제하지 않는다."""
        with self.state_lock:
            if self.connection_cleanup_started:
                raise MergeError("publication cleanup already started")
            if self.work_active:
                self._reject("work is active")
            if self.pending:
                self._reject("termination is pending")
            if self.tokens:
                self._reject(f"{next(iter(self.tokens))} is in progress")
            if self.inflight:
                self._reject("an acquisition is in flight")
            self.connection_cleanup_started = True
            token = self._new_token_locked("collect")
        try:
            self._collect(token)
        finally:
            self._end(token)

    # ----- root 잠금 -----

    def acquire_root_lock(self, root: Path, *, work: _PublicationWork | None = None) -> Any:
        from opencrab import locking

        with self._use(work) as acquire:
            locking.assert_lifetime_lock_order()
            path = str(root.parent / (root.name + ".lifetime.lock"))
            fd = self.open_fd("root-lock", path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666,
                              token=acquire)
            key = self.fd_roles["root-lock"][-1]
            self.root_raw_key = key
            self.root_raw_state = "held-pre-fdopen"
            # 신원 검사 실패는 fdopen 전이므로 raw 보유가 확정이다. 첫 cleanup이 그 숫자를 한 번 닫는다.
            opened = os.fstat(fd)
            current = os.lstat(path)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise MergeError("publication lifetime lock identity differs")
            self.root_lock_state = "fdopen-pending"
            self.root_raw_state = "fdopen-entered"
            try:
                handle = os.fdopen(fd, "r+b")
            except BaseException:
                self.uncertain = True
                self.root_lock_state = "uncertain"
                self.root_raw_state = "uncertain"
                raise
            self.root_lock = handle
            self.fd_slots[key] = None
            self.root_lock_state = "open"
            self.root_raw_state = "transferred"
            try:
                self.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, token=acquire)
            except _PublicationFlockBeforeTest:
                raise
            except BaseException:
                self.root_lock_state = "uncertain"
                raise
            self.root_lock_state = "locked"
            try:
                with locking._lifetime_handles_lock:
                    locking._lifetime_handles[handle] = (path, threading.get_ident(), False)
            except BaseException:
                self.root_registry_state = "uncertain"
                self.uncertain = True
                raise
            self.root_registry_state = "registered"
            return handle

    def _release_root_impl(self) -> None:
        from opencrab import locking

        if self.uncertain or self.connections:
            return
        if self.root_raw_state == "held-pre-fdopen":
            key = self.root_raw_key
            fd = self.fd_slots[key]
            try:
                os.close(fd)
            except BaseException as exc:
                self.root_raw_state = "uncertain"
                self.uncertain = True
                self.record("root-raw-close", key, exc, "first")
                raise
            self.fd_slots[key] = None
            self.root_raw_state = "closed"
            return
        if self.root_lock is None:
            return
        if self.root_lock_state == "locked":
            try:
                fcntl.flock(self.root_lock.fileno(), fcntl.LOCK_UN)
            except BaseException:
                self.root_lock_state = "uncertain"
                self.uncertain = True
                raise
            self.root_lock_state = "unlocked"
        try:
            self.root_lock.close()
        except BaseException:
            self.root_lock_state = "uncertain"
            self.uncertain = True
            raise
        self.root_lock_state = "closed"
        if self.root_registry_state == "registered":
            try:
                with locking._lifetime_handles_lock:
                    del locking._lifetime_handles[self.root_lock]
            except BaseException:
                self.root_registry_state = "uncertain"
                self.uncertain = True
                raise
            self.root_registry_state = "removed"
        self.root_lock = None

    def release_root_lock(self, *, token: _PublicationToken | None = None) -> None:
        if token is not None:
            self._check_token(token, ("cleanup",))
            self._release_root_impl()
            return
        if self.root_lock is None and self.root_raw_state != "held-pre-fdopen":
            return
        direct = self._begin_direct("release")
        if direct is None:
            return
        try:
            self._release_root_impl()
        finally:
            self._end(direct)

    # ----- stream -----

    def _fdopen_impl(self, role: str, fd: int, mode: str) -> tuple[str, Any]:
        instances = self.stream_roles.setdefault(role, [])
        key = role if not instances else f"{role}#{len(instances)}"
        instances.append(key)
        self.streams[key] = None
        try:
            stream = os.fdopen(fd, mode)
        except BaseException:
            self.uncertain = True
            raise
        self.streams[key] = stream
        for fd_key in self.fd_roles[role]:
            if self.fd_slots[fd_key] == fd:
                self.fd_slots[fd_key] = None
                break
        return key, stream

    def fdopen(self, role: str, fd: int, mode: str, *, work: _PublicationWork | None = None) -> Any:
        with self._use(work):
            return self._fdopen_impl(role, fd, mode)[1]

    def fdopen_pair(self, role: str, fd: int, mode: str, *, work: _PublicationWork | None = None,
                    token: _PublicationToken | None = None) -> tuple[str, Any]:
        """준비한 key와 반환 stream을 결속해 돌려준다. Q1이 정확한 key만 닫게 한다."""
        with self._use(work, token):
            return self._fdopen_impl(role, fd, mode)

    def close_stream(self, role: str, *, token: _PublicationToken | None = None) -> None:
        token = self._check_token(token, ("cleanup", "q1"))
        stream = self.streams[role]
        if token.kind == "q1":
            if token.key != role:
                self._reject("Q1 closes only its own stream key")
            if stream is None or stream is not token.stream:
                self.uncertain = True
                raise MergeError("publication Q1 stream identity differs")
        if stream is None or self.uncertain or self.connections:
            return
        try:
            stream.close()
        except BaseException as exc:
            self.uncertain = True
            self.record("stream-close", role, exc, "first")
            raise
        self.streams[role] = None

    def read_mounts(self, work: _PublicationWork | None) -> list[str]:
        """D0의 Q1. /proc/mounts stream을 이 호출 안에서 열고 읽고 닫는다."""
        with self.state_lock:
            self._check_work_locked(work)
            if self.pending or self.uncertain or self.connections or self.inflight or self.tokens:
                self._reject("Q1 preconditions are not met")
            token = self._new_token_locked("q1")
        primary = None
        key = None
        lines: list[str] = []
        try:
            try:
                fd = self.open_fd("mounts", "/proc/mounts", os.O_RDONLY, token=token)
                key, stream = self.fdopen_pair("mounts", fd, "r", token=token)
                token.key, token.stream = key, stream
                lines = stream.read().splitlines()
            except BaseException as exc:
                primary = exc
            close_error = None
            if key is not None:
                try:
                    self.close_stream(key, token=token)
                except BaseException as exc:
                    close_error = exc
            if primary is not None:
                if close_error is not None and close_error is not primary:
                    raise primary from close_error
                raise primary
            if close_error is not None:
                raise close_error
            return lines
        finally:
            self._end(token)

    # ----- 종료 -----

    def _finish_work(self) -> _PublicationToken | None:
        """body 종료와 cleanup 진입을 한 mutex 아래에서 원자적으로 한다."""
        with self.state_lock:
            if self.work is not None:
                self.work.active = False
            self.work_active = False
            if self.tokens or self.inflight or self.connection_cleanup_started or self.pending:
                self.pending = True
                return None
            self.cleanup_started = True
            self.connection_cleanup_started = True
            return self._new_token_locked("cleanup")

    def _release_all(self, token: _PublicationToken) -> None:
        """모든 연결이 닫힌 뒤에만 F1 순서로 자원을 해제한다. 첫 오류나 불명에서 멈춘다."""
        if self.uncertain or self.connections:
            return
        if self.defer_root_release or self.root_lock is not None or self.root_raw_state == "held-pre-fdopen":
            self.release_root_lock(token=token)
        if self.uncertain or self.connections or not self._root_complete():
            return
        for role in list(self.streams):
            self.close_stream(role, token=token)
        for role in list(self.deferred_fds):
            if self.fd_category.get(role) in ("source-lock", "source-parent"):
                continue
            if self.fd_slots[role] is not None:
                self.close_fd(role, self.fd_slots[role], token=token)
        if self.uncertain or self.connections:
            return
        source_lock = self.fd_slots.get("source-lock")
        if source_lock is not None:
            if self.source_state == "locked":
                self.flock(source_lock, fcntl.LOCK_UN, token=token)
            self.close_fd("source-lock", token=token)
        if "source-parent" in self.fd_slots:
            self.close_fd("source-parent", token=token)


class _PublicationConnection:
    def __init__(self, owner: _PublicationOwner, conn: sqlite3.Connection) -> None:
        self.owner = owner
        self.conn = conn
        self.failed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)

    def _close_inner(self) -> None:
        if self.failed:
            raise MergeError("publication connection close is uncertain")
        try:
            self.conn.close()
        except _PublicationCloseBeforeTest:
            raise
        except BaseException:
            self.failed = True
            self.owner.uncertain = True
            raise
        self.owner.connections.remove(self)

    def close(self, *, work: _PublicationWork | None = None, token: _PublicationToken | None = None) -> None:
        if token is not None:
            self.owner._check_token(token, ("cleanup", "collect"))
            self._close_inner()
            return
        with self.owner._use(work):
            self._close_inner()


@contextlib.contextmanager
def _publication_owner(root: Path):
    owner = _PublicationOwner(root)
    work = _PublicationWork(owner)
    with _publication_owners_lock:
        if owner.root in _publication_owners:
            raise MergeError("publication owner already exists")
        _publication_owners[owner.root] = owner
    with owner.state_lock:
        owner.work = work
        owner.work_active = True
        work.active = True
    _publication_current.owner = owner
    primary = None
    try:
        yield owner, work
    except BaseException as exc:
        primary = exc
        raise
    finally:
        token = owner._finish_work()
        cleanup_error = None
        if token is None:
            cleanup_error = MergeError("publication cleanup is rejected while another operation is active")
            owner.record("cleanup", owner, cleanup_error, "rejected")
        else:
            try:
                owner._collect(token)
                owner._release_all(token)
            except BaseException as exc:
                cleanup_error = exc
            finally:
                owner._end(token)
        if cleanup_error is not None and owner.cleanup_error is None:
            owner.cleanup_error = cleanup_error
        _publication_current.owner = None
        if (not owner.uncertain and not owner.connections and owner.root_lock is None
                and not owner.pending and not owner.tokens and not owner.inflight
                and not any(owner.streams.values())
                and not any(fd is not None for fd in owner.fd_slots.values())):
            with _publication_owners_lock:
                _publication_owners.pop(owner.root, None)
        if cleanup_error is not None:
            if primary is None:
                raise cleanup_error
            if token is None or primary.__cause__ is not None or primary.__suppress_context__:
                raise primary  # 거부 객체와 기존 명시적 cause는 primary 위에 덮어쓰지 않고 순서 기록에 둔다
            raise primary from cleanup_error


def fs_type(path: Path, *, work: _PublicationWork | None = None) -> str:
    """``/proc/mounts``에서 경로를 덮는 가장 긴 마운트 지점의 파일시스템 종류를 찾는다."""
    target = str(path.resolve())
    best_len, best_type = -1, "unknown"
    owner = getattr(_publication_current, "owner", None)
    if owner is None:
        try:
            lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
        except OSError:
            return "unknown"
    else:
        lines = owner.read_mounts(work)
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) > best_len:
            best_len, best_type = len(mount), parts[2]
    return best_type


def refuse_ram_fs(label: str, path: Path, *, work: _PublicationWork | None = None) -> None:
    kind = fs_type(path, work=work) if work is not None else fs_type(path)
    if kind == "unknown" and getattr(_publication_current, "owner", None) is not None:
        raise MergeError(f"{label} filesystem is unknown")
    if kind in RAM_FS_TYPES:
        raise MergeError(f"{label} is on {kind} (memory); refusing")


def check_free(path: Path, floor_gib: float, need_bytes: int = 0) -> int:
    """필요량을 빼고도 여유가 하한 이상인지 본다. 여유 바이트를 돌려준다."""
    free = shutil.disk_usage(path).free
    if free - need_bytes < floor_gib * GIB:
        raise DiskFloorError(
            f"free space would fall below the {floor_gib} GiB floor "
            f"(free {free / GIB:.1f} GiB, need {need_bytes / GIB:.1f} GiB)")
    return free


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _file_uri(path: Path, *, mode: str, immutable: bool = False) -> str:
    query = f"mode={mode}" + ("&immutable=1" if immutable else "")
    return f"file:{urllib.parse.quote(str(path.resolve()))}?{query}"


def _source_uri(path: Path, source_fds: dict[str, int] | None, *, immutable: bool) -> str:
    if source_fds is None:
        return _file_uri(path, mode="ro", immutable=immutable)
    fd = source_fds[path.name]
    os.fstat(fd)
    return f"file:/proc/self/fd/{fd}?mode=ro&immutable=1"


def _connect(path: Path, *, mode: str, immutable: bool = False,
             source_fds: dict[str, int] | None = None,
             work: _PublicationWork | None = None) -> sqlite3.Connection:
    uri = (_source_uri(path, source_fds, immutable=immutable) if source_fds is not None
           else _file_uri(path, mode=mode, immutable=immutable))
    owner = getattr(_publication_current, "owner", None)
    if owner is None:
        return sqlite3.connect(uri, uri=True, isolation_level=None)
    return owner.connect(uri, uri=True, isolation_level=None, work=work)


def _close_conn(conn: Any, work: _PublicationWork | None) -> None:
    """게시 분기의 소유 연결은 work 권한으로 닫는다. owner 없는 연결은 기존 close를 쓴다."""
    if work is None:
        conn.close()
    else:
        conn.close(work=work)


def _close_deferred(owner: _PublicationOwner, role: str, fd: int, work: _PublicationWork | None) -> None:
    """finally 안의 지연 close 등록. 거부는 순서 기록에 남기고, 원래 primary가 있으면 primary를 유지한다."""
    outer = sys.exception()
    try:
        owner.close_fd(role, fd, work=work)
    except MergeError as rejection:
        owner.record("deferred-close", role, rejection, "rejected")
        if outer is None:
            raise


def _load_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ImportError as exc:  # vec0 가상 테이블이 없는 원본이면 필요 없다
        raise MergeError("sqlite_vec is not importable") from exc
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _module_of(sql: str) -> str | None:
    match = _MODULE_RE.search(sql or "")
    return match.group(1).lower() if match else None


def inspect_source(path: Path, *, immutable: bool,
                   source_fds: dict[str, int] | None = None,
                   work: _PublicationWork | None = None) -> dict[str, Any]:
    """원본 하나의 객체 목록을 분류한다. 읽기 전용 연결만 연다."""
    conn = _connect(path, mode="ro", immutable=immutable, source_fds=source_fds, work=work)
    try:
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid"
        ).fetchall()
        if any(typ == "table" and name == "pack_delete_operations" for typ, name, _tbl, _sql in rows):
            raise MergeError("single SQLite source requires authorized access")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        _close_conn(conn, work)

    virtual = {
        name: _module_of(sql)
        for typ, name, _tbl, sql in rows
        if typ == "table" and sql and sql.upper().startswith("CREATE VIRTUAL")
    }
    shadow_of: dict[str, str] = {}
    for typ, name, _tbl, sql in rows:
        if typ != "table" or name in virtual or name.startswith("sqlite_"):
            continue
        # 이름이 겹치는 가상 테이블이 둘 이상일 수 있다(예: notes와 notes_tags). 모든 가상 테이블을
        # 본 뒤에 판정해야 만든 순서와 무관하다. 알려진 접미사로 맞는 주인이 있으면 그림자다.
        # 접두어만 맞고 주인이 없으면 추측하지 않고 중단한다. 접두어가 맞지 않으면 일반 테이블이다.
        prefixed = []
        for vname, module in virtual.items():
            prefix = vname + "_"
            if not name.startswith(prefix):
                continue
            prefixed.append((vname, module))
            suffix = name[len(prefix):]
            known = suffix in _SHADOW_SUFFIXES.get(module or "", set())
            pattern = _SHADOW_PATTERNS.get(module or "")
            if known or (pattern is not None and pattern.match(suffix)):
                shadow_of[name] = vname
                break
        if prefixed and name not in shadow_of:
            vname, module = prefixed[0]
            raise MergeError(
                f"{path.name}: table {name!r} starts with virtual table {vname!r} but its "
                f"suffix {name[len(vname) + 1:]!r} is not a known {module} shadow suffix; "
                "refusing to guess")
    regular = [
        name for typ, name, _tbl, sql in rows
        if typ == "table" and name not in virtual and name not in shadow_of
        and not name.startswith("sqlite_")
    ]
    objects = {
        "virtual": virtual,
        "shadow": shadow_of,
        "regular": regular,
        "ddl": {name: sql for typ, name, _tbl, sql in rows if typ == "table"},
        "indexes": [(name, sql) for typ, name, _tbl, sql in rows
                    if typ == "index" and sql and not name.startswith("sqlite_autoindex")],
        "triggers": [(name, sql) for typ, name, _tbl, sql in rows if typ == "trigger"],
        "has_sequence": any(name == "sqlite_sequence" for typ, name, _t, _s in rows),
        "has_stat1": any(name == "sqlite_stat1" for typ, name, _t, _s in rows),
        "page_size": page_size,
        "bytes": page_size * page_count,
        "free_bytes": page_size * freelist,
    }
    return objects


def plan(source_dir: Path, files: list[str], *, immutable: bool,
         source_fds: dict[str, int] | None = None,
         work: _PublicationWork | None = None) -> dict[str, Any]:
    """파일별 분류와 이름 충돌을 계산한다. 출력 파일을 쓰지 않는다."""
    sources: dict[str, dict[str, Any]] = {}
    for name in files:
        path = source_dir / name
        if path.is_symlink() or not path.is_file():
            raise MergeError(f"source must be a regular file, not a symlink: {name}")
        if path.resolve().parent != source_dir.resolve():
            raise MergeError(f"source file escapes source directory: {name}")
        sources[name] = inspect_source(path, immutable=immutable, source_fds=source_fds, work=work)

    seen: dict[str, str] = {}
    collisions: list[dict[str, str]] = []
    for fname, info in sources.items():
        names = (
            list(info["virtual"]) + list(info["shadow"]) + info["regular"]
            + [n for n, _ in info["indexes"]] + [n for n, _ in info["triggers"]]
        )
        for obj in names:
            if obj in seen:
                collisions.append({"object": obj, "first": seen[obj], "second": fname})
            else:
                seen[obj] = fname
    reserved = {
        "pack_delete_operations", "pack_delete_active", "single_file_metadata",
        "idx_pack_delete_pending", "idx_pack_delete_successor", "idx_packs_incarnation",
        "pack_delete_active_valid_insert", "pack_delete_active_valid_update",
        "pack_delete_predecessor_valid", "pack_delete_identity_immutable",
        "pack_delete_committed_immutable", "pack_delete_committed_no_delete",
        "pack_incarnation_new", "pack_incarnation_valid", "pack_incarnation_immutable",
    }
    for obj in sorted(reserved.intersection(seen)):
        collisions.append({"object": obj, "first": seen[obj], "second": "single-db schema"})
    page_sizes = {info["page_size"] for info in sources.values()}
    return {
        "files": files,
        "sources": sources,
        "collisions": collisions,
        "page_sizes": sorted(page_sizes),
        "source_bytes": sum(info["bytes"] for info in sources.values()),
    }


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA {schema}.table_info({_quote(table)})")]


def _needs_physical_rowid(conn: sqlite3.Connection, table: str, ddl: str) -> bool:
    """INTEGER PRIMARY KEY 별칭이 없는 rowid 테이블은 물리 rowid를 따로 옮겨야 한다.

    vec0 그림자 테이블 ``vector_chunks00(rowid PRIMARY KEY, ...)``은 열 이름이 rowid여도 별칭이 아니다.
    sqlite-vec은 청크 번호를 물리 rowid로 blob을 연다. 열만 옮기면 물리 rowid가 1부터 새로 매겨져
    청크가 삭제된 적 있는 원본에서 조회가 깨진다. 실제 스냅숏에서 발견했다.
    """
    if re.search(r"\bWITHOUT\s+ROWID\b", ddl or "", re.IGNORECASE):
        return False
    pks = [row for row in conn.execute(f"PRAGMA main.table_info({_quote(table)})") if row[5]]
    alias = len(pks) == 1 and (pks[0][2] or "").upper() == "INTEGER"
    return not alias


def _copy_table(conn: sqlite3.Connection, table: str, *, clear_first: bool, ddl: str = "") -> int:
    cols = _columns(conn, "main", table)
    if not cols:
        raise MergeError(f"table has no columns in output: {table}")
    column_list = ", ".join(_quote(c) for c in cols)
    select_list = column_list
    if _needs_physical_rowid(conn, table, ddl):
        # 열 이름이 rowid인 표에서도 _rowid_는 물리 rowid를 가리킨다.
        column_list = "_rowid_, " + column_list
        select_list = "_rowid_, " + select_list
    conn.execute("BEGIN")
    try:
        if clear_first:
            conn.execute(f"DELETE FROM main.{_quote(table)}")
        cur = conn.execute(
            f"INSERT INTO main.{_quote(table)} ({column_list}) "
            f"SELECT {select_list} FROM src.{_quote(table)}"
        )
        copied = cur.rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return copied


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def merge(
    source_dir: Path,
    out: Path,
    files: list[str],
    pl: dict[str, Any],
    *,
    immutable: bool,
    cache_kib: int,
    min_free_gib: float,
    log: Any,
    source_fds: dict[str, int] | None = None,
    work: _PublicationWork | None = None,
) -> dict[str, Any]:
    for fname in files:
        inspect_source(source_dir / fname, immutable=immutable, source_fds=source_fds, work=work)
    partial = out.with_name(out.name + ".partial")
    if os.path.lexists(out):
        raise MergeError("output exists; refusing to overwrite")
    if os.path.lexists(partial):
        raise MergeError("a .partial output exists; preserve it and use a new output path")
    page_size = pl["page_sizes"][0]
    report: dict[str, Any] = {"phases": [], "tables": {}}
    free_min = shutil.disk_usage(out.parent).free

    def mark(phase: str, started: float) -> None:
        nonlocal free_min
        size = os.path.getsize(partial) if partial.exists() else 0
        free_min = min(free_min, shutil.disk_usage(out.parent).free)
        entry = {"phase": phase, "seconds": round(time.monotonic() - started, 3),
                 "out_bytes": size, "rss_peak_mib": round(_rss_mib(), 1)}
        report["phases"].append(entry)
        log(json.dumps(entry))

    needs_vec = any(
        module == "vec0" for info in pl["sources"].values() for module in info["virtual"].values()
    )
    owner = getattr(_publication_current, "owner", None)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    fd = (owner.open_fd("merge-partial", partial, flags, 0o600, work=work) if owner
          else os.open(partial, flags, 0o600))
    if owner:
        owner.close_fd("merge-partial", fd, work=work)
    else:
        os.close(fd)
    conn = _connect(partial, mode="rw", work=work)
    try:
        if needs_vec:
            _load_vec(conn)
        conn.execute(f"PRAGMA page_size={page_size}")
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
        conn.execute("PRAGMA mmap_size=0")
        if any(info["has_stat1"] for info in pl["sources"].values()):
            # sqlite_stat1은 예약 이름이라 CREATE TABLE로 만들 수 없다. 빈 출력에서 ANALYZE를 돌리면 빈 표가 생긴다.
            conn.execute("ANALYZE main")
        post_indexes: list[tuple[str, str]] = []
        post_triggers: list[tuple[str, str]] = []
        for fname in files:
            info = pl["sources"][fname]
            started = time.monotonic()
            conn.execute(
                "ATTACH DATABASE ? AS src",
                (_source_uri(source_dir / fname, source_fds, immutable=immutable),),
            )
            try:
                for vname in info["virtual"]:
                    conn.execute(info["ddl"][vname])
                for tname in info["regular"]:
                    conn.execute(info["ddl"][tname])
                order = info["regular"] + list(info["shadow"])
                for tname in order:
                    t0 = time.monotonic()
                    copied = _copy_table(conn, tname, clear_first=tname in info["shadow"],
                                         ddl=info["ddl"].get(tname, ""))
                    report["tables"][tname] = {
                        "file": fname, "rows": copied,
                        "seconds": round(time.monotonic() - t0, 3),
                    }
                    check_free(out.parent, min_free_gib)
                if info["has_sequence"]:
                    for name, seq in conn.execute("SELECT name, seq FROM src.sqlite_sequence").fetchall():
                        cur = conn.execute(
                            "UPDATE main.sqlite_sequence SET seq=max(seq, ?) WHERE name=?", (seq, name))
                        if cur.rowcount == 0:
                            conn.execute("INSERT INTO main.sqlite_sequence(name, seq) VALUES (?, ?)",
                                         (name, seq))
                if info["has_stat1"]:
                    conn.execute("INSERT INTO main.sqlite_stat1 SELECT tbl, idx, stat FROM src.sqlite_stat1")
            finally:
                conn.execute("DETACH DATABASE src")
            post_indexes += info["indexes"]
            post_triggers += info["triggers"]
            mark(f"copy:{fname}", started)
        started = time.monotonic()
        if "packs" not in report["tables"]:
            raise MergeError("pack registry is missing")
        if "incarnation" in _columns(conn, "main", "packs"):
            raise MergeError("pack incarnation already exists")
        conn.execute("""
            CREATE TABLE single_file_metadata (
                singleton INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
                root TEXT NOT NULL,
                database_path TEXT NOT NULL,
                db_uuid TEXT NOT NULL,
                generation TEXT NOT NULL
            )
        """)
        conn.execute("ALTER TABLE packs ADD COLUMN incarnation TEXT")
        conn.execute("UPDATE packs SET incarnation = lower(hex(randomblob(16)))")
        conn.execute("CREATE UNIQUE INDEX idx_packs_incarnation ON packs(incarnation)")
        conn.execute("""
            CREATE TRIGGER pack_incarnation_new AFTER INSERT ON packs
            WHEN NEW.incarnation IS NULL
            BEGIN
                UPDATE packs SET incarnation = lower(hex(randomblob(16))) WHERE pack_id = NEW.pack_id;
            END
        """)
        conn.execute("""
            CREATE TRIGGER pack_incarnation_valid BEFORE INSERT ON packs
            WHEN NEW.incarnation IS NOT NULL AND
                 (length(NEW.incarnation) != 32 OR NEW.incarnation GLOB '*[^0-9a-f]*')
            BEGIN
                SELECT RAISE(ABORT, 'invalid pack incarnation');
            END
        """)
        conn.execute("""
            CREATE TRIGGER pack_incarnation_immutable BEFORE UPDATE OF incarnation ON packs
            WHEN OLD.incarnation IS NOT NULL AND NEW.incarnation IS NOT OLD.incarnation
            BEGIN
                SELECT RAISE(ABORT, 'pack incarnation is immutable');
            END
        """)
        conn.execute("""
            CREATE TABLE pack_delete_operations (
                operation_id TEXT PRIMARY KEY,
                pack_id TEXT NOT NULL,
                incarnation TEXT NOT NULL,
                predecessor TEXT REFERENCES pack_delete_operations(operation_id),
                state TEXT NOT NULL CHECK (state IN ('pending', 'committed')),
                result_json TEXT,
                evidence_json TEXT,
                CHECK ((state = 'pending' AND result_json IS NULL AND evidence_json IS NULL) OR
                       (state = 'committed' AND result_json IS NOT NULL AND evidence_json IS NOT NULL))
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX idx_pack_delete_pending ON pack_delete_operations(pack_id)
            WHERE state = 'pending'
        """)
        conn.execute("""
            CREATE UNIQUE INDEX idx_pack_delete_successor ON pack_delete_operations(predecessor)
            WHERE predecessor IS NOT NULL
        """)
        conn.execute("""
            CREATE TABLE pack_delete_active (
                pack_id TEXT PRIMARY KEY,
                incarnation TEXT NOT NULL,
                operation_id TEXT NOT NULL UNIQUE REFERENCES pack_delete_operations(operation_id),
                state TEXT NOT NULL CHECK (state IN ('stable', 'handoff', 'clear_intent')),
                expected_pointer TEXT,
                CHECK ((state = 'handoff' AND expected_pointer IS NOT NULL) OR
                       (state != 'handoff' AND expected_pointer IS NULL))
            )
        """)
        conn.execute("""
            CREATE TRIGGER pack_delete_active_valid_insert BEFORE INSERT ON pack_delete_active
            WHEN NOT EXISTS (
                SELECT 1 FROM pack_delete_operations o JOIN packs p ON p.pack_id = o.pack_id
                WHERE o.operation_id = NEW.operation_id AND o.pack_id = NEW.pack_id
                  AND o.incarnation = NEW.incarnation AND p.incarnation = NEW.incarnation
                  AND (NEW.state != 'handoff' OR
                       (o.state = 'pending' AND o.predecessor = NEW.expected_pointer))
                  AND (NEW.state != 'clear_intent' OR o.state = 'committed')
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid active delete operation');
            END
        """)
        conn.execute("""
            CREATE TRIGGER pack_delete_active_valid_update BEFORE UPDATE ON pack_delete_active
            WHEN NOT EXISTS (
                SELECT 1 FROM pack_delete_operations o JOIN packs p ON p.pack_id = o.pack_id
                WHERE o.operation_id = NEW.operation_id AND o.pack_id = NEW.pack_id
                  AND o.incarnation = NEW.incarnation AND p.incarnation = NEW.incarnation
                  AND (NEW.state != 'handoff' OR
                       (o.state = 'pending' AND o.predecessor = NEW.expected_pointer))
                  AND (NEW.state != 'clear_intent' OR o.state = 'committed')
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid active delete operation');
            END
        """)
        conn.execute("""
            CREATE TRIGGER pack_delete_predecessor_valid BEFORE INSERT ON pack_delete_operations
            WHEN NEW.predecessor IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pack_delete_operations
                WHERE operation_id = NEW.predecessor AND pack_id = NEW.pack_id
                  AND incarnation = NEW.incarnation AND state = 'committed'
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid delete predecessor');
            END
        """)
        conn.execute("""
            CREATE TRIGGER pack_delete_identity_immutable BEFORE UPDATE OF operation_id, pack_id, incarnation, predecessor
            ON pack_delete_operations
            WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.pack_id IS NOT OLD.pack_id
              OR NEW.incarnation IS NOT OLD.incarnation OR NEW.predecessor IS NOT OLD.predecessor
            BEGIN
                SELECT RAISE(ABORT, 'delete operation identity is immutable');
            END
        """)
        conn.execute("""
            CREATE TRIGGER pack_delete_committed_immutable BEFORE UPDATE ON pack_delete_operations
            WHEN OLD.state = 'committed'
            BEGIN
                SELECT RAISE(ABORT, 'committed delete operation is immutable');
            END
        """)
        conn.execute("""
            CREATE TRIGGER pack_delete_committed_no_delete BEFORE DELETE ON pack_delete_operations
            WHEN OLD.state = 'committed'
            BEGIN
                SELECT RAISE(ABORT, 'committed delete operation is immutable');
            END
        """)
        mark("delete-state", started)
        started = time.monotonic()
        for _name, sql in post_indexes:
            conn.execute(sql)
            check_free(out.parent, min_free_gib)
        mark("indexes", started)
        started = time.monotonic()
        for _name, sql in post_triggers:
            conn.execute(sql)
        mark("triggers", started)
    except BaseException:
        _close_conn(conn, work)
        raise
    _close_conn(conn, work)
    report["free_bytes_min"] = free_min
    report["partial"] = str(partial)
    return report


VEC_SAMPLES = 20


def _vec_samples(conn: sqlite3.Connection, vname: str, count: int) -> list[dict[str, Any]]:
    """vec0 표본: ``<이름>_rowids`` 그림자 테이블의 rowid를 같은 간격으로 골라 질의 벡터를 만든다."""
    rowids = _quote(vname + "_rowids")
    top = conn.execute(f"SELECT max(rowid) FROM main.{rowids}").fetchone()[0] or 0
    cols = {row[1] for row in conn.execute(f"PRAGMA main.table_info({_quote(vname)})")}
    pack_col = "pack_id" in cols
    picked: list[dict[str, Any]] = []
    for i in range(count):
        start = (top * i) // count + 1
        row = conn.execute(
            f"SELECT id FROM main.{rowids} WHERE rowid >= ? ORDER BY rowid LIMIT 1", (start,)).fetchone()
        if row is None:
            continue
        sel = "embedding, pack_id" if pack_col else "embedding"
        got = conn.execute(f"SELECT {sel} FROM main.{_quote(vname)} WHERE node_id = ?", (row[0],)).fetchone()
        if got is None:
            continue
        picked.append({"node_id": row[0], "embedding": got[0], "pack": got[1] if pack_col else None})
    return picked


def verify(
    source_dir: Path,
    out_path: Path,
    files: list[str],
    pl: dict[str, Any],
    report: dict[str, Any],
    *,
    immutable: bool,
    cache_kib: int,
    log: Any,
    source_fds: dict[str, int] | None = None,
    work: _PublicationWork | None = None,
) -> dict[str, Any]:
    """출력의 행 수를 원본과 대조하고 무결성 검사를 한다. 출력은 쓰지 않는다."""
    for fname in files:
        inspect_source(source_dir / fname, immutable=immutable, source_fds=source_fds, work=work)
    result: dict[str, Any] = {"count_mismatch": [], "fts_errors": [], "vec_mismatch": []}
    started = time.monotonic()
    conn = _connect(out_path, mode="ro", work=work)
    try:
        needs_vec = any(m == "vec0" for i in pl["sources"].values() for m in i["virtual"].values())
        if needs_vec:
            _load_vec(conn)
        conn.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
        for tname, entry in report["tables"].items():
            src = _connect(source_dir / entry["file"], mode="ro", immutable=immutable,
                           source_fds=source_fds, work=work)
            try:
                src_n = src.execute(f"SELECT count(*) FROM {_quote(tname)}").fetchone()[0]
            finally:
                _close_conn(src, work)
            out_n = conn.execute(f"SELECT count(*) FROM {_quote(tname)}").fetchone()[0]
            if not (src_n == out_n == entry["rows"]):
                result["count_mismatch"].append(
                    {"table": tname, "source": src_n, "out": out_n, "copied": entry["rows"]})
        invalid_tokens = conn.execute(
            "SELECT count(*) FROM packs WHERE incarnation IS NULL OR length(incarnation) != 32 "
            "OR incarnation GLOB '*[^0-9a-f]*'"
        ).fetchone()[0]
        unique_tokens = conn.execute("SELECT count(DISTINCT incarnation) FROM packs").fetchone()[0]
        pack_count = conn.execute("SELECT count(*) FROM packs").fetchone()[0]
        operation_count = conn.execute("SELECT count(*) FROM pack_delete_operations").fetchone()[0]
        result["delete_state_ok"] = invalid_tokens == 0 and unique_tokens == pack_count and operation_count == 0
        result["count_seconds"] = round(time.monotonic() - started, 3)
        log(json.dumps({"verify": "counts", "mismatch": len(result["count_mismatch"])}))

        started = time.monotonic()
        quick = conn.execute("PRAGMA quick_check").fetchall()
        result["quick_check"] = [row[0] for row in quick]
        result["quick_check_seconds"] = round(time.monotonic() - started, 3)
        log(json.dumps({"verify": "quick_check", "rows": result["quick_check"][:3]}))
    finally:
        _close_conn(conn, work)

    # fts5 integrity-check는 쓰기 연결이 필요하다. 게시 전 사본이므로 출력 파일이 대상이다.
    rw = _connect(out_path, mode="rw", work=work)
    try:
        if needs_vec:
            _load_vec(rw)
        started = time.monotonic()
        for info in pl["sources"].values():
            for vname, module in info["virtual"].items():
                if module != "fts5":
                    continue
                try:
                    rw.execute(f"INSERT INTO {_quote(vname)}({_quote(vname)}) VALUES('integrity-check')")
                except sqlite3.DatabaseError as exc:
                    result["fts_errors"].append({"table": vname, "error": type(exc).__name__})
        result["fts_integrity_seconds"] = round(time.monotonic() - started, 3)
        for fname in files:
            info = pl["sources"][fname]
            for vname, module in info["virtual"].items():
                if module != "vec0":
                    continue
                rw.execute("ATTACH DATABASE ? AS src",
                           (_source_uri(source_dir / fname, source_fds, immutable=immutable),))
                try:
                    samples = _vec_samples(rw, vname, VEC_SAMPLES)
                except sqlite3.OperationalError as exc:
                    # 출력의 벡터 조회가 깨졌다는 뜻이다. 비정상 종료가 아니라 검증 실패로 남긴다.
                    result["vec_mismatch"].append({"table": vname, "error": str(exc)[:160]})
                    samples = []
                try:
                    for sample in samples:
                        for scoped in (False, True):
                            if scoped and sample["pack"] is None:
                                continue
                            where = " AND pack_id = ?" if scoped else ""
                            args = (sample["embedding"],) + ((sample["pack"],) if scoped else ())
                            sql = (f"SELECT node_id, distance FROM {{schema}}.{_quote(vname)} "
                                   f"WHERE embedding MATCH ? AND k=10{where}")
                            got_out = rw.execute(sql.format(schema="main"), args).fetchall()
                            got_src = rw.execute(sql.format(schema="src"), args).fetchall()
                            # id 순서와 거리가 정확히 같아야 한다(같은 바이트에서 계산한다).
                            if got_out != got_src or not got_out:
                                result["vec_mismatch"].append(
                                    {"table": vname, "node": sample["node_id"], "scoped": scoped})
                finally:
                    rw.execute("DETACH DATABASE src")
        log(json.dumps({"verify": "fts_vec", "fts_errors": len(result["fts_errors"]),
                        "vec_mismatch": len(result["vec_mismatch"])}))
    finally:
        _close_conn(rw, work)
    result["ok"] = (
        not result["count_mismatch"] and not result["fts_errors"] and not result["vec_mismatch"]
        and result["quick_check"] == ["ok"] and result["delete_state_ok"]
    )
    return result


def finalize(partial: Path, out: Path) -> dict[str, Any]:
    """WAL 모드로 바꾸고 체크포인트한 뒤 덮어쓰기 없이 게시한다."""
    started = time.monotonic()
    if os.path.lexists(out):
        raise MergeError("output exists; refusing to overwrite")
    initial = os.lstat(partial)
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise MergeError("partial must be a regular file with one link")
    conn = _connect(partial, mode="rw")
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    current_path = os.lstat(partial)
    if (not stat.S_ISREG(current_path.st_mode) or current_path.st_nlink != 1
            or (initial.st_dev, initial.st_ino) != (current_path.st_dev, current_path.st_ino)):
        raise MergeError("partial identity changed")
    for suffix in ("-wal", "-shm"):
        side = Path(str(partial) + suffix)
        if side.exists():
            side.unlink()
    # 게시 전에 파일을 fsync한다. 새 이름을 연결한 뒤 디렉터리를 fsync한다.
    fd = os.open(partial, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        current = os.fstat(fd)
        path_stat = os.lstat(partial)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino)
                or (path_stat.st_dev, path_stat.st_ino) != (current.st_dev, current.st_ino)):
            raise MergeError("partial identity changed")
        os.fsync(fd)
        os.link(partial, out, follow_symlinks=False)
        partial.unlink()
    finally:
        os.close(fd)
    dir_fd = os.open(out.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return {"journal_mode": mode, "seconds": round(time.monotonic() - started, 3)}


def _publication_checkpoint(path: Path, metadata: tuple[str, str, str, str] | None = None,
                            *, work: _PublicationWork | None = None) -> None:
    initial = os.lstat(path)
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise MergeError("publication database identity is invalid")
    conn = _connect(path, mode="rw", work=work)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise MergeError("publication WAL mode differs")
        conn.execute("PRAGMA synchronous=FULL")
        if conn.execute("PRAGMA synchronous").fetchone() != (2,):
            raise MergeError("publication synchronous mode differs")
        if metadata is not None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO single_file_metadata (root, database_path, db_uuid, generation) VALUES (?, ?, ?, ?)",
                         metadata)
            conn.execute("COMMIT")
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if (not isinstance(result, tuple) or len(result) != 3
                or any(type(value) is not int or value < 0 for value in result)
                or result[0] != 0 or result[1] != result[2]):
            raise MergeError("publication WAL checkpoint is incomplete")
    except BaseException as primary:
        try:
            _close_conn(conn, work)
        except BaseException as close_error:
            raise MergeError(f"{primary}; publication close failed: {close_error}") from close_error
        raise
    else:
        _close_conn(conn, work)
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(str(path) + suffix):
            raise MergeError("publication database sidecar remains")
    owner = getattr(_publication_current, "owner", None)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = owner.open_fd("checkpoint-fsync", path, flags, work=work) if owner else os.open(path, flags)
    try:
        opened = os.fstat(fd)
        current = os.lstat(path)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
            raise MergeError("publication database identity changed")
        os.fsync(fd)
    finally:
        if owner:
            _close_deferred(owner, "checkpoint-fsync", fd, work)
        else:
            os.close(fd)


def _prepare_publication(root: Path, db_path: Path, lifetime_lock: Any,
                         *, work: _PublicationWork | None = None) -> dict[str, str]:
    """독점 수명 잠금 아래 생성 의도를 독점 기록한다."""
    import uuid

    from opencrab.locking import assert_exclusive_lifetime_lock

    if not lifetime_lock or lifetime_lock.closed:
        raise MergeError("publication lifetime lock is missing")
    lock_path = root.parent / (root.name + ".lifetime.lock")
    opened = os.fstat(lifetime_lock.fileno())
    current = os.lstat(lock_path)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
        raise MergeError("publication lifetime lock identity differs")
    try:
        assert_exclusive_lifetime_lock(lifetime_lock, str(root.parent / (root.name + ".lifetime.lock")))
    except RuntimeError as exc:
        raise MergeError("publication lifetime lock is not exclusive") from exc

    if (not root.is_absolute() or os.path.realpath(root.parent) != str(root.parent)
            or not root.name or root.name in (".", "..")):
        raise MergeError("publication parent is invalid")
    if os.path.lexists(root) or os.path.lexists(db_path):
        raise MergeError("publication target exists")
    if db_path.parent != root:
        raise MergeError("publication database must be inside its root")
    ledger_path = root.parent / (root.name + ".intent.json")
    payload = {"state": "preparing", "root": str(root), "database": str(db_path),
               "db_uuid": uuid.uuid4().hex, "generation": uuid.uuid4().hex}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    owner = getattr(_publication_current, "owner", None)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = (owner.open_fd("intent", ledger_path, flags, 0o600, work=work) if owner
          else os.open(ledger_path, flags, 0o600))
    if owner:
        stream = owner.fdopen("intent", fd, "wb", work=work)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = (owner.open_fd("intent-parent", root.parent, flags, work=work) if owner
                 else os.open(root.parent, flags))
    try:
        os.fsync(parent_fd)
    finally:
        if owner:
            _close_deferred(owner, "intent-parent", parent_fd, work)
        else:
            os.close(parent_fd)
    return payload


@contextlib.contextmanager
def _publication_source_lock(root: Path, *, work: _PublicationWork | None = None):
    """기존 원본 수명 잠금을 생성하지 않고 공유 획득한다."""
    root = Path(os.path.abspath(root))
    if os.path.realpath(root) != str(root) or not root.is_dir():
        raise MergeError("source root is invalid")
    owner = getattr(_publication_current, "owner", None)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = (owner.open_fd("source-parent", root.parent, flags, work=work) if owner
                 else os.open(root.parent, flags))
    fd = None
    try:
        name = root.name + ".source.lifetime.lock"
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = (owner.open_fd("source-lock", name, flags, dir_fd=parent_fd, work=work) if owner
              else os.open(name, flags, dir_fd=parent_fd))
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise MergeError("source lifetime lock identity differs")
        if owner:
            owner.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, work=work)
        else:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield fd
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise MergeError("source lifetime lock changed")
    finally:
        if owner:
            _close_deferred(owner, "source-lock", fd, work) if fd is not None else None
            _close_deferred(owner, "source-parent", parent_fd, work)
        else:
            if fd is not None:
                os.close(fd)
            os.close(parent_fd)


def _publication_evidence(manifest_raw: bytes, evidence_raw: bytes) -> dict[str, Any]:
    """백업 세트와 동결 증거의 식별 및 시간을 대조한다."""
    from datetime import datetime, timedelta, timezone

    from opencrab.stores._single_file import decode_binding_json

    manifest = decode_binding_json(manifest_raw)
    evidence = decode_binding_json(evidence_raw)
    keys = {"version", "snapshot_id", "source_manifest_sha256", "preparer_id",
            "frozen_from", "frozen_until", "writer_stopped", "billing_same_snapshot",
            "packs_observation", "backup_at", "packs_checked_at", "capture_from", "capture_until"}
    if set(evidence) != keys or evidence["version"] != 1:
        raise MergeError("publication evidence schema differs")
    if (not isinstance(evidence["preparer_id"], str) or not evidence["preparer_id"]
            or not isinstance(manifest.get("set_id"), str) or not manifest["set_id"]
            or evidence["snapshot_id"] != manifest["set_id"]
            or evidence["source_manifest_sha256"] != hashlib.sha256(manifest_raw).hexdigest()
            or evidence["writer_stopped"] is not True
            or evidence["billing_same_snapshot"] is not True
            or evidence["packs_observation"] != "absent"):
        raise MergeError("publication evidence binding differs")

    def stamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise MergeError("publication evidence timestamp is invalid")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MergeError("publication evidence timestamp is invalid") from exc
        if result.utcoffset() != timedelta(0):
            raise MergeError("publication evidence timestamp is not UTC")
        return result.astimezone(timezone.utc)

    start, end = stamp(evidence["frozen_from"]), stamp(evidence["frozen_until"])
    first, last = stamp(evidence["capture_from"]), stamp(evidence["capture_until"])
    checked = stamp(evidence["packs_checked_at"])
    created = stamp(manifest.get("created_at"))
    if (stamp(evidence["backup_at"]) != created or not start <= first <= last <= end
            or not start <= checked <= end or last >= created + timedelta(seconds=1)):
        raise MergeError("publication evidence time differs")
    return manifest


@contextlib.contextmanager
def _publication_inputs(root: Path, manifest: dict[str, Any], files: list[str],
                        *, work: _PublicationWork | None = None):
    """고정된 백업 원본과 manifest의 필수 파일을 대조한다."""
    owner = getattr(_publication_current, "owner", None)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = owner.open_fd("input-root", root, flags, work=work) if owner else os.open(root, flags)
    opened: dict[str, int] = {}
    snapshots: dict[str, tuple[int, int, int, int, int]] = {}
    try:
        expected = set(files) | {"billing.db"}
        if type(manifest.get("format")) is not int or manifest["format"] != 1:
            raise MergeError("source manifest format is unsupported")
        entries = manifest.get("entries")
        metadata = manifest.get("files")
        if not isinstance(entries, list) or not isinstance(metadata, dict):
            raise MergeError("source manifest structure is invalid")
        if set(metadata) != expected:
            raise MergeError("source manifest file set differs")
        roles = expected | {"chroma", "docs", "delete-pack-journals", "graph.kuzu", "packs"}
        if any(not isinstance(entry, dict) or entry.get("label") not in roles for entry in entries):
            raise MergeError("source manifest role is invalid")
        for name in roles - expected:
            matches = [entry for entry in entries if entry.get("label") == name]
            if len(matches) != 1:
                raise MergeError("source manifest role count differs")
            status = matches[0].get("status")
            if name in ("graph.kuzu", "packs"):
                if status != "skipped" or "path" in matches[0] or os.path.lexists(root / name):
                    raise MergeError("source excluded role is not permitted")
            elif status == "skipped":
                if "path" in matches[0] or os.path.lexists(root / name):
                    raise MergeError("source skipped directory differs")
            elif status != "unverified" or matches[0].get("path") != name:
                raise MergeError("source directory role differs")
            elif not os.path.isdir(root / name):
                raise MergeError("source directory is missing")
        if set(os.listdir(root_fd)) - {"MANIFEST.json", *expected,
                                      "chroma", "docs", "delete-pack-journals"}:
            raise MergeError("source contains unlisted files")
        for name in ("chroma", "docs", "delete-pack-journals"):
            entry = next(entry for entry in entries if entry.get("label") == name)
            if entry["status"] == "skipped":
                continue
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = (owner.open_fd("directory:" + name, name, flags, dir_fd=root_fd, work=work) if owner
                         else os.open(name, flags, dir_fd=root_fd))
            try:
                if os.listdir(directory):
                    raise MergeError("source directory is not empty")
            finally:
                if owner:
                    _close_deferred(owner, "directory:" + name, directory, work)
                else:
                    os.close(directory)
        identities = set()
        for name in sorted(expected):
            matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("label") == name]
            if (len(matches) != 1 or matches[0].get("status") != "verified"
                    or matches[0].get("path") != name
                    or matches[0].get("capture") not in ("copied_under_lock", "wal_snapshot")):
                raise MergeError("source manifest entry differs")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = (owner.open_fd("source:" + name, name, flags, dir_fd=root_fd, work=work) if owner
                  else os.open(name, flags, dir_fd=root_fd))
            opened[name] = fd
            st = os.fstat(fd)
            identity = (st.st_dev, st.st_ino)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or identity in identities:
                raise MergeError("source file identity is invalid")
            identities.add(identity)
            snapshots[name] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            digest = hashlib.sha256()
            offset = 0
            while True:
                chunk = os.pread(fd, 1024 * 1024, offset)
                if not chunk:
                    break
                offset += len(chunk)
                digest.update(chunk)
            item = metadata[name]
            if not isinstance(item, dict) or item.get("size") != st.st_size or item.get("sha256") != digest.hexdigest():
                raise MergeError("source file digest differs")
            conn = _connect(root / name, mode="ro", immutable=True, source_fds=opened, work=work)
            try:
                if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise MergeError("source database integrity differs")
                owner = getattr(_publication_current, "owner", None)
                attach = (owner.connect(":memory:", work=work) if owner is not None
                          else sqlite3.connect(":memory:"))
                try:
                    attach.execute("ATTACH DATABASE ? AS source", (_source_uri(root / name, opened, immutable=True),))
                    attach.execute("SELECT count(*) FROM source.sqlite_master").fetchone()
                    attach.execute("DETACH DATABASE source")
                finally:
                    _close_conn(attach, work)
            finally:
                _close_conn(conn, work)
        def check_inputs() -> None:
            for name, fd in opened.items():
                current = os.fstat(fd)
                path_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                observed = (current.st_dev, current.st_ino, current.st_size,
                            current.st_mtime_ns, current.st_ctime_ns)
                path_identity = (path_stat.st_dev, path_stat.st_ino, path_stat.st_size,
                                 path_stat.st_mtime_ns, path_stat.st_ctime_ns)
                if (observed != snapshots[name] or path_identity != snapshots[name]
                        or not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1):
                    raise MergeError("source file changed during use")

        check_inputs()
        yield opened, check_inputs
        check_inputs()
    finally:
        if owner:
            for name in opened:
                _close_deferred(owner, "source:" + name, opened[name], work)
            _close_deferred(owner, "input-root", root_fd, work)
        else:
            for fd in opened.values():
                os.close(fd)
            os.close(root_fd)


def _publication_effect(operation: Any, paths: tuple[Path, ...]) -> None:
    try:
        operation()
    except BaseException as primary:
        owner = getattr(_publication_current, "owner", None)
        observations = {}
        for path in paths:
            try:
                observations[str(path)] = _publication_exists(path)
            except OSError:
                observations[str(path)] = None
        if owner is not None:
            owner.effect_observations.append(observations)
        raise primary


_RENAME_NOREPLACE = 1  # linux/fs.h의 RENAME_NOREPLACE
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _rename_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """renameat2(RENAME_NOREPLACE)로 이름을 바꾼다. 실패하면 커널 errno를 담은 OSError를 낸다.

    heartbeat 정리 스크립트의 같은 이름 함수는 AT_FDCWD 경로를 받고 그 스크립트의 예외를 내므로 쓰지 않는다.
    libc에 renameat2가 없으면 ENOSYS를 낸다. 일반 rename으로 되돌아가지 않는다(D3).
    """
    import ctypes

    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        raise OSError(errno.ENOSYS, "renameat2 is not available") from None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    if function(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), _RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _publication_rename_probe(staging: Path, owner: _PublicationOwner, work: _PublicationWork) -> None:
    """R0a. staging 안에서 NOREPLACE rename 지원을 확인한다. 부작용은 staging 안에 갇힌다."""
    probe_fd = owner.open_fd("probe-dir", staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, work=work)
    try:
        try:
            os.mkdir(".rp-a", 0o700, dir_fd=probe_fd)
        except OSError as exc:
            raise MergeError(f"publication rename probe failed: {exc}") from exc
        try:
            _rename_noreplace(probe_fd, ".rp-a", probe_fd, ".rp-b")
        except OSError as exc:
            if exc.errno in (errno.ENOSYS, errno.EINVAL):
                raise MergeError("publication rename is unsupported") from exc
            raise MergeError(f"publication rename probe failed: {exc}") from exc
        try:
            os.rmdir(".rp-b", dir_fd=probe_fd)
        except OSError as exc:
            raise MergeError(f"publication rename probe failed: {exc}") from exc
    finally:
        _close_deferred(owner, "probe-dir", probe_fd, work)


def _publication_identity(path: Path) -> tuple[int, int, int]:
    info = os.lstat(path)
    return info.st_dev, info.st_ino, info.st_size


def _publication_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _publication_write_file(owner: _PublicationOwner, work: _PublicationWork, role: str, path: Path | str,
                            data: bytes, *, dir_fd: int | None = None) -> None:
    """새 파일을 O_EXCL로 만들어 쓰고 fsync한다. 쓰기와 fsync는 owner 밖 직접 호출이다."""
    fd = owner.open_fd(role, path, _CREATE_FLAGS, 0o600, dir_fd=dir_fd, work=work)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("publication write made no progress")
            view = view[count:]
        os.fsync(fd)
    finally:
        _close_deferred(owner, role, fd, work)


def _publication_table_counts(conn: Any) -> dict[str, int]:
    """일반 테이블의 행 수. 가상 테이블은 확장이 필요해 센 값이 환경에 의존하므로 뺀다(기존 verify의 판정과 같다)."""
    names = [name for typ, name, sql in conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
             if typ == "table" and not (sql and sql.upper().startswith("CREATE VIRTUAL"))
             and not name.startswith("sqlite_")]
    return {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for name in sorted(names)}


def _publication_write_binding(owner: _PublicationOwner, work: _PublicationWork, staging: Path,
                               intent: dict[str, str], source: dict[str, Any], manifest_raw: bytes,
                               evidence_raw: bytes) -> str:
    """R2~R6. staging에 수명 잠금 파일과 결속 파일을 쓰고 manifest_digest를 돌려준다."""
    lock_fd = owner.open_fd("lock-create", staging / "single.db.lifetime.lock", _CREATE_FLAGS, 0o600, work=work)
    _close_deferred(owner, "lock-create", lock_fd, work)
    conn = _connect(staging / "single.db", mode="ro", immutable=True, work=work)
    try:
        tables = _publication_table_counts(conn)
    finally:
        _close_conn(conn, work)
    billing = source["files"]["billing.db"]
    manifest = {"version": 1, "root": intent["root"], "database": intent["database"],
                "db_uuid": intent["db_uuid"], "generation": intent["generation"],
                "source_set_id": source["set_id"],
                "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
                "billing": {"size": billing["size"], "sha256": billing["sha256"]}, "tables": tables}
    manifest_bytes = _publication_json(manifest)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    _publication_write_file(owner, work, "manifest-write", staging / "manifest.json", manifest_bytes)
    _publication_write_file(owner, work, "marker-write", staging / "marker.json",
                            _publication_json({"db_uuid": intent["db_uuid"], "generation": intent["generation"],
                                               "manifest_digest": digest}))
    _publication_write_file(owner, work, "ledger-write", staging / "ledger.json",
                            _publication_json({"state": "staged", "root": intent["root"],
                                               "database": intent["database"], "db_uuid": intent["db_uuid"],
                                               "generation": intent["generation"], "manifest_digest": digest}))
    seal_fd = owner.open_fd("staging-seal", staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, work=work)
    try:
        os.fsync(seal_fd)
    finally:
        _close_deferred(owner, "staging-seal", seal_fd, work)
    return digest


def _publication_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-dir", required=True, type=Path)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", type=Path)
    output.add_argument("--publication-root", type=Path)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--vector-file", default="vectors.db", help="원본 디렉터리의 벡터 DB 파일 이름")
    parser.add_argument("--apply", action="store_true", help="실제로 병합한다(기본은 계획만 출력)")
    parser.add_argument("--verify", action="store_true",
                        help="게시 전에 행 수, quick_check, fts5와 vec0 실조회를 대조한다(운영에서는 필수)")
    parser.add_argument("--immutable-source", action="store_true",
                        help="원본이 변하지 않는다고 선언한다(WAL이 없는 정지된 스냅숏 전용. 기본은 끔)")
    parser.add_argument("--cache-kib", type=int, default=65536)
    parser.add_argument("--min-free-gib", type=float, default=30.0,
                        help="이 여유 아래로 내려가면 시작하지 않거나 복사를 멈춘다")
    parser.add_argument("--report", type=Path, help="결과 JSON을 쓸 경로")
    args = parser.parse_args(argv)
    if args.publication_root is not None:
        if args.evidence_file is None:
            parser.error("--publication-root requires --evidence-file")
        if args.apply and (not args.verify or not args.immutable_source):
            parser.error("--publication-root --apply requires --verify and --immutable-source")
        if not args.apply and args.report:
            parser.error("--publication-root plan does not accept --report")
        try:
            from opencrab.stores._single_file import read_binding_fd

            with (_publication_owner(args.publication_root) as (owner, work),
                  _publication_source_lock(args.source_dir, work=work)):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                root_fd = owner.open_fd("manifest-parent", args.source_dir, flags, work=work)
                evidence_parent_fd = owner.open_fd("evidence-parent", args.evidence_file.parent, flags, work=work)
                try:
                    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                    manifest_fd = owner.open_fd("manifest-file", "MANIFEST.json", flags, dir_fd=root_fd,
                                                work=work)
                    try:
                        evidence_fd = owner.open_fd("evidence-file", args.evidence_file.name, flags,
                                                    dir_fd=evidence_parent_fd, work=work)
                        try:
                            manifest_raw = read_binding_fd(root_fd, "MANIFEST.json", manifest_fd)
                            evidence_raw = read_binding_fd(evidence_parent_fd, args.evidence_file.name, evidence_fd)
                        finally:
                            _close_deferred(owner, "evidence-file", evidence_fd, work)
                    finally:
                        _close_deferred(owner, "manifest-file", manifest_fd, work)
                    manifest = _publication_evidence(manifest_raw, evidence_raw)
                finally:
                    _close_deferred(owner, "evidence-parent", evidence_parent_fd, work)
                    _close_deferred(owner, "manifest-parent", root_fd, work)
                vector_file = Path(args.vector_file)
                if vector_file.name != args.vector_file or args.vector_file in {".", ".."}:
                    raise MergeError("source vector name is invalid")
                files = [FILES[0], FILES[1], args.vector_file, FILES[3]]
                if len(set(files)) != len(files):
                    raise MergeError("source names are not distinct")
                with _publication_inputs(args.source_dir, manifest, files, work=work) as (source_fds, check_inputs):
                    pl = plan(args.source_dir, files, immutable=True, source_fds=source_fds, work=work)
                    check_inputs()
                    if pl["collisions"] or len(pl["page_sizes"]) != 1:
                        raise MergeError("source schemas are incompatible")
                    if not args.apply:
                        print("publication mode is not ready", file=sys.stderr)
                        return 2
                    if args.report is not None:
                        raise MergeError("publication report is not supported yet")
                    root = Path(os.path.abspath(args.publication_root))
                    if os.path.realpath(root.parent) != str(root.parent):
                        raise MergeError("publication parent is invalid")
                    if (os.path.commonpath((str(root), os.path.abspath(args.source_dir)))
                            in (str(root), os.path.abspath(args.source_dir))):
                        raise MergeError("publication and source overlap")
                    staging = root.with_name(root.name + ".staging")
                    ledger = root.with_name(root.name + ".intent.json")
                    intent_tmp = root.with_name(root.name + ".intent.json.tmp")
                    if any(_publication_exists(path) for path in (root, staging, ledger, intent_tmp)):
                        raise MergeError("publication artifacts already exist")
                    refuse_ram_fs("publication directory", root.parent, work=work)
                    check_free(root.parent, args.min_free_gib,
                               int(pl["source_bytes"] * MERGE_SPACE_FACTOR))
                    lock = owner.acquire_root_lock(root, work=work)
                    try:
                        if any(_publication_exists(path) for path in (root, staging, ledger, intent_tmp)):
                            raise MergeError("publication artifacts already exist")
                        check_inputs()
                        check_free(root.parent, args.min_free_gib,
                                   int(pl["source_bytes"] * MERGE_SPACE_FACTOR))
                        intent = _prepare_publication(root, root / "single.db", lock, work=work)
                        check_free(root.parent, args.min_free_gib)
                        _publication_effect(lambda: os.mkdir(staging, mode=0o700), (staging,))
                        parent_fd = owner.open_fd("staging-parent", root.parent,
                                                  os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                  work=work)
                        try:
                            os.fsync(parent_fd)
                        finally:
                            _close_deferred(owner, "staging-parent", parent_fd, work)
                        try:
                            _publication_rename_probe(staging, owner, work)
                            check_inputs()
                            check_free(staging, args.min_free_gib)
                            merged = merge(args.source_dir, staging / "single.db", files, pl,
                                           immutable=True, cache_kib=args.cache_kib,
                                           min_free_gib=args.min_free_gib,
                                           log=lambda line: print(line, file=sys.stderr, flush=True),
                                           source_fds=source_fds, work=work)
                            verified = verify(args.source_dir, Path(merged["partial"]), files, pl, merged,
                                              immutable=True, cache_kib=args.cache_kib,
                                              log=lambda line: print(line, file=sys.stderr, flush=True),
                                              source_fds=source_fds, work=work)
                            if not verified["ok"]:
                                raise MergeError("publication verification failed")
                            check_inputs()
                            partial = Path(merged["partial"])
                            database = staging / "single.db"
                            _publication_checkpoint(partial, work=work)
                            _publication_effect(lambda: os.link(partial, database, follow_symlinks=False),
                                                (partial, database))
                            _publication_effect(partial.unlink, (partial, database))
                            staging_fd = owner.open_fd("staging-dir", staging,
                                                       os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                       work=work)
                            try:
                                os.fsync(staging_fd)
                            finally:
                                _close_deferred(owner, "staging-dir", staging_fd, work)
                            _publication_checkpoint(database, (str(root), str(root / "single.db"),
                                                               intent["db_uuid"], intent["generation"]),
                                                    work=work)
                            single_identity = _publication_identity(database)
                            check_free(staging, args.min_free_gib, os.fstat(source_fds["billing.db"]).st_size)
                            billing_fd = owner.open_fd("billing-write", staging / "billing.db",
                                                       os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                                       work=work)
                            try:
                                source_fd = source_fds["billing.db"]
                                offset = 0
                                digest = hashlib.sha256()
                                while chunk := os.pread(source_fd, 1024 * 1024, offset):
                                    digest.update(chunk)
                                    view = memoryview(chunk)
                                    while view:
                                        count = os.write(billing_fd, view)
                                        if count <= 0:
                                            raise OSError("billing copy made no progress")
                                        view = view[count:]
                                    offset += len(chunk)
                                os.fsync(billing_fd)
                            finally:
                                _close_deferred(owner, "billing-write", billing_fd, work)
                            expected = manifest["files"]["billing.db"]
                            if offset != expected["size"] or digest.hexdigest() != expected["sha256"]:
                                raise MergeError("publication billing copy differs")
                            copied_fd = owner.open_fd("billing-read", staging / "billing.db",
                                                      os.O_RDONLY | os.O_NOFOLLOW, work=work)
                            try:
                                copied_stat = os.fstat(copied_fd)
                                path_stat = os.stat(staging / "billing.db", follow_symlinks=False)
                                copied_digest = hashlib.sha256()
                                copied_offset = 0
                                while chunk := os.pread(copied_fd, 1024 * 1024, copied_offset):
                                    copied_digest.update(chunk)
                                    copied_offset += len(chunk)
                                if (not stat.S_ISREG(copied_stat.st_mode) or copied_stat.st_nlink != 1
                                        or (copied_stat.st_dev, copied_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                                        or copied_offset != expected["size"]
                                        or copied_digest.hexdigest() != expected["sha256"]):
                                    raise MergeError("publication billing identity or digest differs")
                                billing_identity = (copied_stat.st_dev, copied_stat.st_ino, copied_stat.st_size)
                            finally:
                                _close_deferred(owner, "billing-read", copied_fd, work)
                            copied = _connect(staging / "billing.db", mode="ro", immutable=True, work=work)
                            try:
                                if copied.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                                    raise MergeError("publication billing integrity differs")
                            finally:
                                _close_conn(copied, work)
                            check_inputs()
                            if owner.uncertain:  # RG. 예외를 삼키는 이후 변경이 uncertain을 세운 채 쓰기로 들어가지 않게 한다.
                                raise MergeError("publication owner is uncertain")
                            if (_publication_identity(database) != single_identity  # R1
                                    or _publication_identity(staging / "billing.db") != billing_identity):
                                raise MergeError("publication staging identity differs")
                            _publication_write_binding(owner, work, staging, intent, manifest, manifest_raw,
                                                       evidence_raw)  # R2~R6
                        except (MergeError, OSError, RuntimeError, sqlite3.DatabaseError) as exc:
                            print(f"publication staging failed: {exc}", file=sys.stderr)
                            return 1
                    finally:
                        owner.defer_root_release = True
                    print("publication staging is not ready", file=sys.stderr)
                    return 1
        except (MergeError, OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as exc:
            print(f"publication preflight failed: {exc}", file=sys.stderr)
            if exc.__cause__ is not None:
                print(f"publication error cause: {type(exc.__cause__).__name__}", file=sys.stderr)
            return 2
    if args.evidence_file is not None:
        parser.error("--evidence-file requires --publication-root")

    # SQLite의 정렬 임시 파일이 시스템 임시 디렉터리(메모리 tmpfs일 수 있다)로 가지 않게 한다.
    os.environ.setdefault("SQLITE_TMPDIR", str(args.out.resolve().parent))

    vector_file = Path(args.vector_file)
    if vector_file.name != args.vector_file or args.vector_file in {".", ".."}:
        parser.error("--vector-file must be a file name inside source-dir")
    files = [FILES[0], FILES[1], args.vector_file, FILES[3]]
    if len(set(files)) != len(files):
        parser.error("source file names must be distinct")

    def log(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        journal_dir = args.source_dir / "delete-pack-journals"
        if os.path.lexists(journal_dir):
            if journal_dir.is_symlink() or not journal_dir.is_dir():
                raise MergeError("delete journal path is not a regular directory")
            if any(journal_dir.iterdir()):
                raise MergeError("delete journals must be resolved before merge")
        refuse_ram_fs("output directory", args.out.resolve().parent)
        refuse_ram_fs("temp directory", Path(os.environ["SQLITE_TMPDIR"]))
        pl = plan(args.source_dir, files, immutable=args.immutable_source)
    except (MergeError, sqlite3.DatabaseError) as exc:
        log(f"plan failed: {exc}")
        return 2
    summary = {
        "files": files,
        "collisions": pl["collisions"],
        "page_sizes": pl["page_sizes"],
        "source_bytes": pl["source_bytes"],
        "objects": {
            name: {"regular": len(i["regular"]), "virtual": len(i["virtual"]),
                   "shadow": len(i["shadow"]), "indexes": len(i["indexes"]),
                   "triggers": len(i["triggers"]), "free_bytes": i["free_bytes"]}
            for name, i in pl["sources"].items()
        },
    }
    print(json.dumps(summary, indent=2))
    if pl["collisions"] or len(pl["page_sizes"]) != 1:
        log("refusing to merge: name collisions or mixed page sizes")
        return 2
    if not args.apply:
        return 0

    started_all = time.monotonic()
    out_dir = args.out.resolve().parent
    need = int(pl["source_bytes"] * MERGE_SPACE_FACTOR)
    try:
        free_before = check_free(out_dir, args.min_free_gib, need)
    except DiskFloorError as exc:
        log(f"disk preflight failed: {exc}")
        return 3
    try:
        report = merge(args.source_dir, args.out, files, pl, immutable=args.immutable_source,
                       cache_kib=args.cache_kib, min_free_gib=args.min_free_gib, log=log)
    except (DiskFloorError, sqlite3.OperationalError, OSError) as exc:
        full = isinstance(exc, DiskFloorError) or "disk is full" in str(exc).lower() or (
            isinstance(exc, OSError) and getattr(exc, "errno", None) == 28)
        if full:
            log(f"merge stopped on disk space: {type(exc).__name__}: {exc}")
            return 3
        log(f"merge failed: {type(exc).__name__}: {exc}")
        return 1
    except (MergeError, sqlite3.DatabaseError) as exc:
        log(f"merge failed: {type(exc).__name__}: {exc}")
        return 1
    partial = Path(report["partial"])
    report["free_bytes_before"] = free_before
    if args.verify:
        vr = verify(args.source_dir, partial, files, pl, report,
                    immutable=args.immutable_source, cache_kib=args.cache_kib, log=log)
        report["verify"] = vr
        if not vr["ok"]:
            log("verify failed; output stays as .partial")
            if args.report:
                args.report.write_text(json.dumps(report, indent=2))
            return 1
    try:
        from opencrab.locking import (
            acquire_file_lock,
            assert_lifetime_lock_order,
            release_file_lock,
        )
        from opencrab.stores._single_file import SharedSqliteFile

        assert_lifetime_lock_order()
        output = Path(os.path.abspath(args.out))
        if os.path.realpath(output.parent) != str(output.parent):
            raise MergeError("output parent contains a symlink")
        lock_name = output.name + ".lifetime.lock"
        lock_path = output.parent / lock_name
        lock_initial = os.lstat(lock_path) if os.path.lexists(lock_path) else None
        if lock_initial is not None:
            SharedSqliteFile._validate_file_stat(lock_initial)
        lock = acquire_file_lock(lock_name, str(output.parent), timeout=0)
        try:
            lock_opened, lock_current = os.fstat(lock.fileno()), os.lstat(lock_path)
            SharedSqliteFile._validate_file_stat(lock_opened)
            SharedSqliteFile._validate_file_stat(lock_current)
            identity = (lock_opened.st_dev, lock_opened.st_ino)
            if (lock_current.st_dev, lock_current.st_ino) != identity:
                raise MergeError("lifetime lock identity changed")
            if lock_initial is not None and (lock_initial.st_dev, lock_initial.st_ino) != identity:
                raise MergeError("lifetime lock identity changed")
            report["finalize"] = finalize(partial, output)
        finally:
            release_file_lock(lock)
    except (MergeError, OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        log(f"publish failed: {type(exc).__name__}: {exc}")
        return 1
    report["total_seconds"] = round(time.monotonic() - started_all, 3)
    report["out_bytes"] = os.path.getsize(args.out)
    report["rss_peak_mib"] = round(_rss_mib(), 1)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "tables"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
