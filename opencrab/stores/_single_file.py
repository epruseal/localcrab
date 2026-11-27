"""여러 SQLite 저장소가 한 연결과 트랜잭션을 공유한다.

호출자는 같은 파일의 저장소에 ``SharedSqliteFile`` 객체를 전달한다. 공유 작업 단위는
``BEGIN IMMEDIATE``로 시작하며 중첩 실패가 발생하면 바깥 트랜잭션을 rollback한다.
저장소 쓰기는 같은 연결에서 팩 세대와 삭제 예약을 검사한다. 파일 수명 잠금과 작업 단위는
서로 다른 경계다. factory의 단일 파일 경로는 전체 전환 검증을 마칠 때까지 차단한다.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

def _schema_tokens(sql: str) -> tuple[str, ...]:
    import re

    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[a-zA-Z_][a-zA-Z_0-9]*|[^\s]", sql or "")
    result = tuple(token if token.startswith("'") else token.strip('"').lower() for token in tokens)
    return tuple(token for index, token in enumerate(result) if not (
        token == "if" and result[index:index + 3] == ("if", "not", "exists")
        or token == "not" and index and result[index - 1:index + 2] == ("if", "not", "exists")
        or token == "exists" and index > 1 and result[index - 2:index + 1] == ("if", "not", "exists")
    ))


_PACK_WRITE_TRIGGERS = (
    'CREATE TRIGGER pack_incarnation_new AFTER INSERT ON packs\n            WHEN NEW.incarnation IS NULL\n            BEGIN\n                UPDATE packs SET incarnation = lower(hex(randomblob(16))) WHERE pack_id = NEW.pack_id;\n            END',
    "CREATE TRIGGER pack_incarnation_valid BEFORE INSERT ON packs\n            WHEN NEW.incarnation IS NOT NULL AND\n                 (length(NEW.incarnation) != 32 OR NEW.incarnation GLOB '*[^0-9a-f]*')\n            BEGIN\n                SELECT RAISE(ABORT, 'invalid pack incarnation');\n            END",
    "CREATE TRIGGER pack_incarnation_immutable BEFORE UPDATE OF incarnation ON packs\n            WHEN OLD.incarnation IS NOT NULL AND NEW.incarnation IS NOT OLD.incarnation\n            BEGIN\n                SELECT RAISE(ABORT, 'pack incarnation is immutable');\n            END",
    "CREATE TRIGGER pack_delete_active_valid_insert BEFORE INSERT ON pack_delete_active\n            WHEN NOT EXISTS (\n                SELECT 1 FROM pack_delete_operations o JOIN packs p ON p.pack_id = o.pack_id\n                WHERE o.operation_id = NEW.operation_id AND o.pack_id = NEW.pack_id\n                  AND o.incarnation = NEW.incarnation AND p.incarnation = NEW.incarnation\n                  AND (NEW.state != 'handoff' OR\n                       (o.state = 'pending' AND o.predecessor = NEW.expected_pointer))\n                  AND (NEW.state != 'clear_intent' OR o.state = 'committed')\n            )\n            BEGIN\n                SELECT RAISE(ABORT, 'invalid active delete operation');\n            END",
    "CREATE TRIGGER pack_delete_active_valid_update BEFORE UPDATE ON pack_delete_active\n            WHEN NOT EXISTS (\n                SELECT 1 FROM pack_delete_operations o JOIN packs p ON p.pack_id = o.pack_id\n                WHERE o.operation_id = NEW.operation_id AND o.pack_id = NEW.pack_id\n                  AND o.incarnation = NEW.incarnation AND p.incarnation = NEW.incarnation\n                  AND (NEW.state != 'handoff' OR\n                       (o.state = 'pending' AND o.predecessor = NEW.expected_pointer))\n                  AND (NEW.state != 'clear_intent' OR o.state = 'committed')\n            )\n            BEGIN\n                SELECT RAISE(ABORT, 'invalid active delete operation');\n            END",
    "CREATE TRIGGER pack_delete_predecessor_valid BEFORE INSERT ON pack_delete_operations\n            WHEN NEW.predecessor IS NOT NULL AND NOT EXISTS (\n                SELECT 1 FROM pack_delete_operations\n                WHERE operation_id = NEW.predecessor AND pack_id = NEW.pack_id\n                  AND incarnation = NEW.incarnation AND state = 'committed'\n            )\n            BEGIN\n                SELECT RAISE(ABORT, 'invalid delete predecessor');\n            END",
    "CREATE TRIGGER pack_delete_identity_immutable BEFORE UPDATE OF operation_id, pack_id, incarnation, predecessor\n            ON pack_delete_operations\n            WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.pack_id IS NOT OLD.pack_id\n              OR NEW.incarnation IS NOT OLD.incarnation OR NEW.predecessor IS NOT OLD.predecessor\n            BEGIN\n                SELECT RAISE(ABORT, 'delete operation identity is immutable');\n            END",
    "CREATE TRIGGER pack_delete_committed_immutable BEFORE UPDATE ON pack_delete_operations\n            WHEN OLD.state = 'committed'\n            BEGIN\n                SELECT RAISE(ABORT, 'committed delete operation is immutable');\n            END",
    "CREATE TRIGGER pack_delete_committed_no_delete BEFORE DELETE ON pack_delete_operations\n            WHEN OLD.state = 'committed'\n            BEGIN\n                SELECT RAISE(ABORT, 'committed delete operation is immutable');\n            END",
)


_PACK_WRITE_TABLES = ("CREATE TABLE pack_delete_operations (\n                operation_id TEXT PRIMARY KEY,\n                pack_id TEXT NOT NULL,\n                incarnation TEXT NOT NULL,\n                predecessor TEXT REFERENCES pack_delete_operations(operation_id),\n                state TEXT NOT NULL CHECK (state IN ('pending', 'committed')),\n                result_json TEXT,\n                evidence_json TEXT,\n                CHECK ((state = 'pending' AND result_json IS NULL AND evidence_json IS NULL) OR\n                       (state = 'committed' AND result_json IS NOT NULL AND evidence_json IS NOT NULL))\n            )", "CREATE TABLE pack_delete_active (\n                pack_id TEXT PRIMARY KEY,\n                incarnation TEXT NOT NULL,\n                operation_id TEXT NOT NULL UNIQUE REFERENCES pack_delete_operations(operation_id),\n                state TEXT NOT NULL CHECK (state IN ('stable', 'handoff', 'clear_intent')),\n                expected_pointer TEXT,\n                CHECK ((state = 'handoff' AND expected_pointer IS NOT NULL) OR\n                       (state != 'handoff' AND expected_pointer IS NULL))\n            )")


ConnHook = Callable[[sqlite3.Connection], None]
_failed_shared_files: dict[str, set[SharedSqliteFile]] = {}
_failed_shared_files_lock = threading.RLock()
_active_units = threading.local()


def sqlite_unit_active() -> bool:
    """현재 스레드가 공유 SQLite 작업 단위 안에 있는지 반환한다."""
    return getattr(_active_units, "depth", 0) > 0


_BINDING_STATES = ("authorized", "staged", "ready")


def validate_single_file_binding(root: str, db_path: str, ledger: dict, manifest: dict, marker: dict,
                                 metadata: dict, manifest_digest: str, *, state: str = "authorized") -> None:
    """검증한 대장과 세대 결속이 설정 경로와 일치하는지 확인한다.

    state는 대장이 가져야 할 상태다. 읽는 쪽의 기본값은 authorized이며 게시자만 staged와 ready를 넘긴다.
    """

    if state not in _BINDING_STATES:
        raise RuntimeError("single SQLite state argument is invalid")
    if not all(isinstance(value, dict) for value in (ledger, manifest, marker, metadata)):
        raise RuntimeError("single SQLite binding is invalid")
    if not {"state", "root", "database", "db_uuid", "generation", "manifest_digest"}.issubset(ledger):
        raise RuntimeError("single SQLite ledger is invalid")
    if not {"root", "database", "db_uuid", "generation"}.issubset(metadata):
        raise RuntimeError("single SQLite metadata is invalid")
    if not {"db_uuid", "generation", "manifest_digest"}.issubset(marker):
        raise RuntimeError("single SQLite marker is invalid")
    if not isinstance(root, str) or not os.path.isabs(root) or os.path.realpath(root) != root:
        raise RuntimeError("single SQLite root is invalid")
    if not isinstance(db_path, str) or not os.path.isabs(db_path) or os.path.realpath(db_path) != db_path:
        raise RuntimeError("single SQLite path is invalid")
    if os.path.commonpath((root, db_path)) != root or db_path == root:
        raise RuntimeError("single SQLite path is outside root")
    if ledger.get("state") != state:
        raise RuntimeError("single SQLite generation is not authorized" if state == "authorized"
                           else "single SQLite generation state differs")
    for key, expected in (("root", root), ("database", db_path)):
        if any(item.get(key) != expected for item in (ledger, manifest, metadata)):
            raise RuntimeError("single SQLite path binding differs")
    for key in ("db_uuid", "generation"):
        value = ledger.get(key)
        if not isinstance(value, str) or len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
            raise RuntimeError("single SQLite generation identity is invalid")
        if any(item.get(key) != value for item in (manifest, marker, metadata)):
            raise RuntimeError("single SQLite generation binding differs")
    if (not isinstance(manifest_digest, str) or len(manifest_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in manifest_digest)
            or ledger.get("manifest_digest") != manifest_digest or marker.get("manifest_digest") != manifest_digest):
        raise RuntimeError("single SQLite manifest binding differs")


def read_binding_file(directory_fd: int, name: str) -> bytes:
    """결속 파일의 경로와 열린 파일 식별 및 내용을 대조한다."""
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name:
        raise ValueError("binding file name is invalid")
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("binding file requires O_NOFOLLOW")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        return read_binding_fd(directory_fd, name, fd)
    finally:
        os.close(fd)


def read_binding_fd(directory_fd: int, name: str, fd: int) -> bytes:
    """열린 결속 파일의 식별과 내용을 대조한다."""
    opened = os.fstat(fd)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    SharedSqliteFile._validate_file_stat(opened)
    SharedSqliteFile._validate_file_stat(current)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError("binding file identity changed")
    if opened.st_size > 1048576:
        raise RuntimeError("binding file is too large")
    chunks = []
    remaining = opened.st_size
    while remaining:
        block = os.read(fd, min(remaining, 65536))
        if not block:
            raise RuntimeError("binding file changed during read")
        chunks.append(block)
        remaining -= len(block)
    after = os.fstat(fd)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
        opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns
    ) or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise RuntimeError("binding file changed during read")
    SharedSqliteFile._validate_file_stat(after)
    SharedSqliteFile._validate_file_stat(current)
    return b"".join(chunks)


def decode_binding_json(raw: bytes) -> dict:
    """결속 JSON의 중복 필드와 비객체 값을 거부한다."""
    import json

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate binding field")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"invalid binding constant: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise RuntimeError("binding JSON is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("binding JSON is not an object")
    return value


def load_single_file_binding(root: str, db_path: str, ledger_dir_fd: int, target_dir_fd: int,
                             ledger_name: str, manifest_name: str, marker_name: str, metadata: dict,
                             *, state: str = "authorized") -> None:
    """고정 디렉터리의 대장, manifest와 표식을 읽어 결속을 검사한다."""
    import hashlib

    ledger = decode_binding_json(read_binding_file(ledger_dir_fd, ledger_name))
    manifest_raw = read_binding_file(target_dir_fd, manifest_name)
    manifest = decode_binding_json(manifest_raw)
    marker = decode_binding_json(read_binding_file(target_dir_fd, marker_name))
    validate_single_file_binding(root, db_path, ledger, manifest, marker, metadata,
                                 hashlib.sha256(manifest_raw).hexdigest(), state=state)


def failed_shared_file(db_path: str) -> SharedSqliteFile | None:
    """종료에 실패한 파일 객체를 돌려준다."""
    with _failed_shared_files_lock:
        objects = _failed_shared_files.get(os.path.abspath(db_path), set())
        if len(objects) > 1:
            raise RuntimeError("multiple failed SQLite file owners")
        return next(iter(objects), None)


class SingleFileUnavailableError(RuntimeError):
    """단일 SQLite 인증 저장소를 지금 열지 못했다(#671).

    ``reason``은 호출자가 503으로 답할 때 쓰는 이유 코드다. 잠금이 풀리면 열리는 경우는 "store-busy"이고
    그 밖의 열기 실패(미승인, 결속 불일치, 설정 오류, 잠금이 아닌 sqlite3 오류, 비가용 저장소)는
    "store-unavailable"이다. 원 예외는 ``__cause__``에 남는다.
    """

    def __init__(self, message: str, reason: str = "store-unavailable") -> None:
        super().__init__(message)
        self.reason = reason


# 보호 검사가 파일 모드별로 기다리는 시간(초)이다. WAL 파일은 `_new_conn`의 기본 대기와 같다.
_UNMANAGED_CHECK_TIMEOUT = 0.2
_UNMANAGED_WAL_WAIT = 5.0
_MARKER_QUERY = "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pack_delete_operations'"


def sqlite_busy_or_locked(exc: BaseException) -> bool:
    """SQLite 오류가 BUSY나 LOCKED(확장 코드 포함)인지 반환한다."""
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _journal_kind(path: str) -> str:
    """파일 헤더 100바이트를 잠금 없이 읽어 저널 모드를 돌려준다.

    바이트 18과 19가 (2, 2)이면 wal, (1, 1)이면 rollback이다. 매직 불일치, 혼합, 100바이트 미만,
    읽기 오류는 other다(판정 불가).
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(100)
    except OSError:
        return "other"
    if len(head) < 100 or head[:16] != b"SQLite format 3\x00":
        return "other"
    if (head[18], head[19]) == (2, 2):
        return "wal"
    if (head[18], head[19]) == (1, 1):
        return "rollback"
    return "other"


def _single_marker(uri: str, timeout: float) -> bool:
    """URI로 연 연결에서 단일 파일 표식 표가 있는지 반환한다."""
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=timeout)) as conn:
        return conn.execute(_MARKER_QUERY).fetchone() is not None


_SINGLE_ROOT_NAMES = ("ledger.json", "single.db")
_SINGLE_ROOT_MESSAGE = "single SQLite root requires SINGLE_SQLITE_FILE"


def reject_single_root(directory: str) -> None:
    """단일 SQLite root(ledger.json이나 single.db가 있는 디렉터리)이면 거부한다.

    SINGLE_SQLITE_FILE 없이 단일 root를 LOCAL_DATA_DIR로 받은 프로세스가 root에 빈 다섯 파일이나 잠금
    파일을 만드는 일을 막는다. 두 이름을 lstat으로만 본다(파일을 열거나 만들지 않으므로 곁파일이 생기지
    않는다). 링크와 디렉터리와 깨진 링크도 있는 것으로 센다. 판정할 수 없으면 거부한다(fail-closed).
    한계: 호출자가 준 디렉터리 바로 아래만 본다. VECTOR_DB_FILE이 상대 하위 경로이면 그 하위 디렉터리를
    보므로 root 자체를 가리켜도 잡지 못한다.
    """
    for name in _SINGLE_ROOT_NAMES:
        try:
            os.lstat(os.path.join(directory, name))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise RuntimeError(f"{_SINGLE_ROOT_MESSAGE}: cannot inspect {directory}: {exc.strerror or exc}") from exc
        raise RuntimeError(f"{_SINGLE_ROOT_MESSAGE}: {directory}")


def reject_single_root_under(path: str | os.PathLike[str]) -> None:
    """``path``가 단일 root이거나 그 아래이면 거부한다.

    출력 경로를 인자로 받는 스크립트가 root 아래에 디렉터리를 만들기 전에 부른다. 대상 경로와 모든 상위
    디렉터리(abspath와 realpath 둘)에 ``reject_single_root``를 건다. 대상이 아직 없어도 된다. root 밖
    경로는 통과한다. 상위 디렉터리를 끝까지 보므로 호출 수는 경로 깊이에 비례한다(lstat 두 번씩).
    """
    for base in dict.fromkeys((os.path.abspath(path), os.path.realpath(path))):
        directory = base
        while True:
            reject_single_root(directory)
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent


def reject_unmanaged_single_file(db_path: str, *, allow_single_root: bool = False) -> None:
    """공유 연결 없이 단일 파일 스키마를 열지 않는다.

    순서는 ``:memory:`` 통과, 단일 root 검사, 표식 검사다. root 검사가 표식 검사보다 앞서는 이유는 표식
    검사가 DB 파일을 열어 WAL 옆에 ``-wal``과 ``-shm``을 만들거나 hot journal을 회복할 수 있기 때문이다.
    root 검사는 lstat만 쓴다. ``allow_single_root``가 거짓이면 DB 파일 부모 디렉터리(abspath와 realpath
    둘)에 ``reject_single_root``를 건다. 참으로 넘기는 호출은 단일 모드 billing 저장소 하나뿐이다. 그
    저장소는 root 안 billing.db를 공유 객체 없이 연다.

    헤더로 저널 모드를 가른다. WAL 파일은 `_UNMANAGED_WAL_WAIT`만큼 기다리고 읽지 못하면 그 BUSY를 올린다.
    롤백 저널 파일이 잠겨 있으면 잠금 없는 읽기(immutable)로 표식만 판정하고, 헤더를 다시 읽어 여전히
    롤백일 때만 그 결과를 쓴다. 판정할 수 없으면 거부한다(fail-closed). 롤백 DB에 hot journal이 남아
    읽기 전용 연결이 회복하지 못하면(READONLY) rw 연결로 저널을 회복한 뒤 판정한다.

    잔여 위험(전환 문서 7.3절): immutable 읽기는 스키마를 바꾸는 중인 롤백 파일이나 merge 부분 파일이나
    저널 회복 중인 파일에서 틀린 결과를 낼 수 있다. 관리 밖에서 롤백 모드로 바꾼 단일 파일 사본에도
    같은 오판 가능성이 있다. 게시된 단일 파일은 늘 WAL이라 이 경로에 닿지 않는다.
    hot journal 회복 재시도는 저널 회복이라는 쓰기를 한다. finalize 중에 종료해 hot journal이 남은
    `.partial`에 닿으면 merge가 보존하라고 한 그 파일의 바이트를 바꾼다. 회복 재시도가 BUSY와 LOCKED가
    아닌 코드로 실패하면 호출자는 그 예외를 그대로 받는다. 다섯 파일 저장소 생성자는 `unmanaged_open_refusal`로
    부르므로 그 예외를 받지 않고 저장소가 비가용이 된다. 이 함수를 직접 부르는 스크립트와 단일 모드 billing은
    예외를 그대로 받는다.
    """
    if db_path == ":memory:":
        return
    if not allow_single_root:
        for directory in dict.fromkeys(
            (os.path.dirname(os.path.abspath(db_path)), os.path.dirname(os.path.realpath(db_path)))
        ):
            reject_single_root(directory)
    path = Path(db_path)
    if not path.is_file():
        return
    resolved = path.resolve()
    base = resolved.as_uri() + "?mode=ro"
    kind = _journal_kind(str(resolved))
    try:
        found = _single_marker(base, _UNMANAGED_WAL_WAIT if kind == "wal" else _UNMANAGED_CHECK_TIMEOUT)
    except sqlite3.OperationalError as exc:
        if kind == "rollback" and ((getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF) == sqlite3.SQLITE_READONLY:
            # hot journal: 읽기 전용 연결은 저널을 되돌리지 못한다. rw 연결이 회복한다. 실패는 그대로 올린다.
            found = _single_marker(resolved.as_uri() + "?mode=rw", _UNMANAGED_CHECK_TIMEOUT)
        else:
            if not sqlite_busy_or_locked(exc) or kind != "rollback":
                raise
            try:
                found = _single_marker(base + "&immutable=1", 0)
            except sqlite3.Error:
                raise exc from None
            if _journal_kind(str(resolved)) != "rollback":
                raise exc
    if found:
        raise RuntimeError("single SQLite file requires shared connection")


def unmanaged_open_refusal(db_path: str, *, allow_single_root: bool = False) -> BaseException | None:
    """보호 검사가 판정하지 못한 파일을 열지 않게 하려고 그 예외를 올리지 않고 돌려준다.

    다섯 파일 저장소 생성자가 부른다. 판정 불가(sqlite3.Error와 OSError)이면 그 예외 객체를 돌려주고,
    저장소는 그 파일을 열지 않은 채 비가용이 된다. 표식이 있거나 단일 root여서 확정한 거부(RuntimeError)는
    그대로 올린다. 통과하면 None이다. 검사 함수는 모듈 전역 이름으로 부른다(시험이 속성을 바꿔 끼운다).
    """
    try:
        reject_unmanaged_single_file(db_path, allow_single_root=allow_single_root)
    except (sqlite3.Error, OSError) as exc:
        return exc
    return None


class RollbackOnlyError(RuntimeError):
    """공유 작업 단위가 rollback-only 상태로 끝났다."""


class PackWriteRejectedError(RuntimeError):
    """팩 세대 또는 삭제 예약 때문에 쓰기를 거부한다."""


class SharedSqliteFile:
    """한 SQLite 파일의 연결과 쓰기 락을 나눠 쓰는 저장소 묶음.

    호출자가 파일마다 객체 하나를 만들어 그 파일을 쓰는 저장소들에 넘긴다. 저장소는 생성할 때
    ``acquire``하고 닫을 때 ``release``한다. 마지막 저장소가 ``release``하면 연결을 닫는다.
    """

    def __init__(self, db_path: str, *, checked: bool = False) -> None:
        self.db_path = os.path.abspath(db_path)
        if failed_shared_file(self.db_path) is not None:
            raise RuntimeError("failed SQLite file owner requires close retry")
        self._checked = checked
        self._file_handle: int | None = None
        self._lifetime_lock: Any = None
        if checked:
            self._open_checked_file()
        self.local = threading.local()
        self.lock = threading.RLock()
        self.conns_lock = threading.Lock()
        self.all_conns: list[sqlite3.Connection] = []
        self._hooks: list[ConnHook] = []
        self._owner_hooks: list[tuple[Any, ConnHook]] = []
        self._uow = threading.local()
        self._users: list[Any] = []
        self._checkpointer: Any | None = None
        self._closed = False

    def _open_checked_file(self) -> None:
        from opencrab.locking import (
            acquire_file_lock,
            assert_lifetime_lock_order,
            release_file_lock,
        )

        assert_lifetime_lock_order()
        parent = os.path.dirname(self.db_path)
        if not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("checked SQLite file requires O_NOFOLLOW")
        if os.path.realpath(parent) != parent:
            raise ValueError("SQLite path has a symlinked parent")
        initial = os.lstat(self.db_path)
        self._validate_file_stat(initial)
        name = os.path.basename(self.db_path) + ".lifetime.lock"
        lock_path = os.path.join(parent, name)
        try:
            lock_initial = os.lstat(lock_path)
        except FileNotFoundError:
            lock_initial = None
        if lock_initial is not None:
            self._validate_file_stat(lock_initial)
        lock = acquire_file_lock(name, parent, shared=True)
        fd = None
        try:
            lock_path_stat = os.lstat(lock_path)
            lock_fd_stat = os.fstat(lock.fileno())
            self._validate_file_stat(lock_path_stat)
            self._validate_file_stat(lock_fd_stat)
            lock_identity = (lock_fd_stat.st_dev, lock_fd_stat.st_ino)
            if (lock_path_stat.st_dev, lock_path_stat.st_ino) != lock_identity:
                raise ValueError("lifetime lock identity changed")
            if lock_initial is not None and (lock_initial.st_dev, lock_initial.st_ino) != lock_identity:
                raise ValueError("lifetime lock identity changed")
            fd = os.open(self.db_path, os.O_RDONLY | os.O_NOFOLLOW)
            current = os.fstat(fd)
            self._validate_file_stat(current)
            self._validate_file_stat(os.lstat(self.db_path))
            if (initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("SQLite file identity changed")
            self._file_handle = fd
            self._lifetime_lock = lock
            self._check_file_identity()
        except BaseException:
            if fd is not None:
                os.close(fd)
            release_file_lock(lock)
            self._file_handle = None
            self._lifetime_lock = None
            raise

    @staticmethod
    def _validate_file_stat(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("file must be regular and have one link")

    def _check_file_identity(self) -> None:
        if self._file_handle is None:
            raise RuntimeError("checked SQLite file is closed")
        if self._lifetime_lock is None:
            raise RuntimeError("checked SQLite lock is closed")
        lock_opened = os.fstat(self._lifetime_lock.fileno())
        lock_current = os.lstat(self.db_path + ".lifetime.lock")
        self._validate_file_stat(lock_opened)
        self._validate_file_stat(lock_current)
        if (lock_opened.st_dev, lock_opened.st_ino) != (lock_current.st_dev, lock_current.st_ino):
            raise ValueError("lifetime lock identity changed")
        opened = os.fstat(self._file_handle)
        current = os.lstat(self.db_path)
        self._validate_file_stat(opened)
        self._validate_file_stat(current)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("SQLite file identity changed")

    # ------------------------------------------------------------------
    # 저장소 등록
    # ------------------------------------------------------------------

    def validate_binding(self, root: str, ledger_dir_fd: int, target_dir_fd: int,
                         ledger_name: str, manifest_name: str, marker_name: str) -> None:
        """checked 파일의 수명 잠금 안에서 세대 결속을 검사한다."""
        if not self._checked:
            raise RuntimeError("single SQLite binding requires a checked file")
        self._check_file_identity()
        root_stat = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode) or os.path.realpath(root) != root:
            raise RuntimeError("single SQLite root directory is invalid")
        target_stat = os.fstat(target_dir_fd)
        if (target_stat.st_dev, target_stat.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise RuntimeError("single SQLite target directory differs")
        parent = os.path.dirname(self.db_path)
        parent_stat = os.stat(parent, follow_symlinks=False)
        ledger_stat = os.fstat(ledger_dir_fd)
        if (ledger_stat.st_dev, ledger_stat.st_ino) != (parent_stat.st_dev, parent_stat.st_ino):
            raise RuntimeError("single SQLite ledger directory differs")
        # DB를 열기 전에 대장 state만 먼저 거른다. mode=ro 연결도 WAL DB 옆에 -wal과 -shm을 만들므로
        # ready나 staged 게시 root는 열지 않고 거부한다. 권위 있는 전체 검증은 아래 load_single_file_binding이 한다.
        if decode_binding_json(read_binding_file(ledger_dir_fd, ledger_name)).get("state") != "authorized":
            raise RuntimeError("single SQLite generation is not authorized")
        conn = sqlite3.connect(Path(self.db_path).as_uri() + "?mode=ro", uri=True)
        try:
            self._check_file_identity()
            rows = conn.execute(
                "SELECT root, database_path, db_uuid, generation FROM single_file_metadata"
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError("single SQLite metadata row is invalid")
            metadata = dict(zip(("root", "database", "db_uuid", "generation"), rows[0]))
            load_single_file_binding(root, self.db_path, ledger_dir_fd, target_dir_fd,
                                     ledger_name, manifest_name, marker_name, metadata)
        finally:
            conn.close()
        root_after = os.stat(root, follow_symlinks=False)
        parent_after = os.stat(parent, follow_symlinks=False)
        if (root_after.st_dev, root_after.st_ino) != (target_stat.st_dev, target_stat.st_ino):
            raise RuntimeError("single SQLite root directory changed")
        if (parent_after.st_dev, parent_after.st_ino) != (ledger_stat.st_dev, ledger_stat.st_ino):
            raise RuntimeError("single SQLite ledger directory changed")
        self._check_file_identity()

    def acquire(self, owner: Any, hook: ConnHook | None) -> None:
        """저장소를 등록한다. ``hook``은 그 저장소가 연결에 거는 추가 설정이다.

        이미 열린 연결에도 즉시 적용한다. 먼저 만든 저장소의 ``_init_db``가 연결을 열고
        나중에 만든 저장소(예: vec0 확장이 필요한 벡터 저장소)가 같은 연결을 써야 하기 때문이다.
        """
        with self.lock, self.conns_lock:
            if self._closed:
                raise RuntimeError("shared SQLite file is closed")
            self._users.append(owner)
            if self._checkpointer is None and hasattr(owner, "_checkpoint_if_wal_large"):
                self._checkpointer = owner
            if hook is not None and hook not in self._hooks:
                try:
                    if any(conn.in_transaction for conn in self.all_conns):
                        raise RuntimeError("cannot register a connection hook during a transaction")
                    for conn in self.all_conns:
                        hook(conn)
                except BaseException:
                    self._users.remove(owner)
                    if self._checkpointer is owner:
                        self._checkpointer = None
                    raise
                self._hooks.append(hook)
            if hook is not None:
                self._owner_hooks.append((owner, hook))

    def close(self) -> None:
        """등록된 저장소가 없으면 공유 파일을 닫는다."""
        with self.lock, self.conns_lock:
            if self._users:
                raise RuntimeError("shared SQLite file still has owners")
            if not self._closed or self.all_conns:
                self._close_locked()

    def _close_locked(self) -> None:
        self._closed = True
        remaining = []
        interrupt = None
        for conn in self.all_conns:
            try:
                conn.close()
            except BaseException as exc:  # noqa: BLE001
                logger.error("SharedSqliteFile: close error: %s", exc)
                remaining.append(conn)
                if not isinstance(exc, Exception) and interrupt is None:
                    interrupt = exc
        self.all_conns[:] = remaining
        if remaining:
            with _failed_shared_files_lock:
                _failed_shared_files.setdefault(self.db_path, set()).add(self)
            if interrupt is not None:
                raise interrupt
            raise RuntimeError("shared SQLite connection close failed")
        try:
            if self._file_handle is not None:
                os.close(self._file_handle)
        finally:
            self._file_handle = None
            if self._lifetime_lock is not None:
                from opencrab.locking import release_file_lock

                try:
                    release_file_lock(self._lifetime_lock)
                finally:
                    self._lifetime_lock = None
        with _failed_shared_files_lock:
            failed = _failed_shared_files.get(self.db_path)
            if failed is not None:
                failed.discard(self)
                if not failed:
                    del _failed_shared_files[self.db_path]

    def release(self, owner: Any) -> None:
        """저장소를 뺀다. 마지막 저장소면 연결을 닫는다."""
        with self.lock, self.conns_lock:
            if owner not in self._users:
                if not self._users and self._closed and self.all_conns:
                    self._close_locked()
                return
            self._users.remove(owner)
            removed = [hook for registered, hook in self._owner_hooks if registered is owner]
            self._owner_hooks = [(registered, hook) for registered, hook in self._owner_hooks
                                 if registered is not owner]
            for hook in removed:
                if not any(registered_hook == hook for _, registered_hook in self._owner_hooks):
                    self._hooks.remove(hook)
            if self._checkpointer is owner:
                self._checkpointer = next(
                    (user for user in self._users if hasattr(user, "_checkpoint_if_wal_large")),
                    None,
                )
            if self._users:
                return
            self._close_locked()

    # ------------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------------

    def new_conn(self) -> sqlite3.Connection:
        """연결을 새로 만든다. 모든 등록 저장소의 설정 훅이 한 연결에 합쳐서 걸린다."""
        with self.conns_lock:
            if self._closed:
                raise RuntimeError("shared SQLite file is closed")
            if self._checked:
                self._check_file_identity()
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                if self._checked:
                    self._check_file_identity()
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                for hook in self._hooks:
                    hook(conn)
                self.all_conns.append(conn)
            except BaseException:
                conn.close()
                raise
            return conn

    def connection(self) -> sqlite3.Connection:
        """현재 스레드의 연결(없으면 생성)."""
        if self._closed:
            raise RuntimeError("shared SQLite file is closed")
        if self._checked:
            self._check_file_identity()
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

    def after_commit(self, callback: Callable[[], None]) -> None:
        if not self.in_unit_of_work:
            raise RuntimeError("no shared unit of work is active")
        self._uow.after_commit.append(callback)

    def mark_rollback_only(self) -> None:
        if not self.in_unit_of_work:
            raise RuntimeError("no shared unit of work is active")
        self._uow.rollback_only = True

    def pack_incarnation(self, pack_id: str) -> str | None:
        """전환 스키마가 있으면 쓰기 전에 팩 세대를 읽는다."""
        conn = self.connection()
        marker = conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='pack_delete_operations'"
        ).fetchone()
        if marker is None:
            return None
        if "incarnation" not in {row[1] for row in conn.execute("PRAGMA main.table_info(packs)")}:
            raise PackWriteRejectedError("single SQLite pack incarnation schema is missing")
        row = conn.execute("SELECT incarnation FROM main.packs WHERE pack_id = ?", (pack_id,)).fetchone()
        if row is None or not row[0]:
            raise RuntimeError("pack incarnation is missing")
        return str(row[0])

    def _require_unshadowed_storage(self, conn) -> None:
        from opencrab.stores import _node_bm25_sqlite as nbm

        protected = {
            "packs", "pack_delete_operations", "pack_delete_active", "doc_nodes",
            "doc_sources", "doc_source_integrity", "audit_log", "ontology_nodes",
            "ontology_edges", "graph_nodes", "graph_edges", "idx_doc_sources_fts_source_id",
        }
        protected.update(
            row[0].lower() for row in conn.execute("SELECT name FROM main.sqlite_master")
            if row[0].lower().startswith(("doc_nodes_bm25", "doc_sources_fts", "vectors_", "graph_"))
        )
        protected.update(
            row[0].lower() for row in conn.execute("SELECT name FROM main.sqlite_master WHERE type='index'")
            if row[0].lower().startswith(("idx_nodes_", "idx_edges_", "idx_doc_", "idx_audit_", "idx_pack_", "idx_packs_"))
        )
        for name, kind, table in conn.execute("SELECT name, type, tbl_name FROM temp.sqlite_master"):
            if name.lower() in protected or (kind == "trigger" and table.lower() in protected):
                raise PackWriteRejectedError("temporary object shadows protected SQLite storage")
        expected = {ddl.split()[5]: ddl for ddl in nbm.ddl_statements() if ddl.startswith("CREATE TRIGGER")}
        expected.update({ddl.split()[2]: ddl for ddl in _PACK_WRITE_TRIGGERS})
        for name, table, ddl in conn.execute(
            "SELECT name, tbl_name, sql FROM main.sqlite_master WHERE type='trigger'"
        ):
            if table.lower() in protected and (
                name not in expected or _schema_tokens(ddl) != _schema_tokens(expected[name])
            ):
                raise PackWriteRejectedError("unexpected trigger on protected SQLite storage")
        for (table_name,) in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table'").fetchall():
            escaped = table_name.replace('"', '""')
            for key in conn.execute(f'PRAGMA main.foreign_key_list("{escaped}")'):
                # 지원 스키마의 FK는 NO ACTION이다. 연쇄 동작은 별도 자식 표에서도 거부한다.
                if key[2].lower() in protected and any(
                    str(action).upper() in {"CASCADE", "SET NULL", "SET DEFAULT"} for action in (key[5], key[6])
                ):
                    raise PackWriteRejectedError("unexpected foreign key action on protected SQLite storage")

    def require_write_layout(self, *, node_indexes: bool, docs=None, graph=None, vec=None) -> None:
        """현재 쓰기 연결에서 로더가 사용하는 객체와 쓰기 분기를 확인한다."""
        from opencrab.stores import _node_bm25_sqlite as nbm
        from opencrab.stores import _source_fts_sqlite as sfts
        from opencrab.stores._sql_dialect import SQLITE
        from opencrab.stores._sql_doc_base import DOC_STORE_SCHEMA

        conn = self.connection()
        self._require_unshadowed_storage(conn)
        objects = {name: (kind, table, ddl) for kind, name, table, ddl in conn.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_master"
        )}

        def require(name, kind="table", ddl=None):
            found = objects.get(name)
            if found is None or found[0] != kind or (ddl is not None and _schema_tokens(found[2]) != _schema_tokens(ddl)):
                raise PackWriteRejectedError(f"unsupported single SQLite object: {name}")

        require("packs")
        from opencrab.stores.sql_store import _TABLES_SQL_SQLITE

        for ddl in _TABLES_SQL_SQLITE:
            words = ddl.split()
            if len(words) > 5 and words[1:5] == ["TABLE", "IF", "NOT", "EXISTS"]:
                name = words[5]
                if name in {"ontology_nodes", "ontology_edges"}:
                    require(name, ddl=ddl)
                elif name == "packs":
                    require(name, ddl=ddl.rstrip().removesuffix(")") + ", incarnation TEXT)")
        for ddl in _PACK_WRITE_TABLES:
            require(ddl.split()[2], ddl=ddl)
        journal_columns = {row[1] for row in conn.execute("PRAGMA main.table_info(pack_delete_operations)")}
        if journal_columns != {"operation_id", "pack_id", "incarnation", "predecessor", "state", "result_json", "evidence_json"}:
            raise PackWriteRejectedError("unsupported single SQLite deletion journal")
        for ddl in _PACK_WRITE_TRIGGERS:
            require(ddl.split()[2], "trigger", ddl)
        for ddl in SQLITE.render_ddl(DOC_STORE_SCHEMA):
            tokens = ddl.split()
            kind = tokens[1].lower()
            name = tokens[5] if tokens[2:5] == ["IF", "NOT", "EXISTS"] else tokens[2]
            require(name.strip('"'), kind, ddl)
        require(sfts.FTS, ddl="CREATE VIRTUAL TABLE doc_sources_fts USING fts5(source_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 0')")
        require(sfts.INDEX, "index", sfts.INDEX_DDL)
        if docs is None or not docs.available or not docs._fts_ok or not docs._read_only_fts_ok(conn):
            raise PackWriteRejectedError("single SQLite source FTS is unavailable")
        virtuals = [sfts.FTS]
        if node_indexes:
            if graph is None or graph._classify_schema(conn) != "target":
                raise PackWriteRejectedError("single SQLite graph schema is unavailable")
            graph._require_write_available()
            if not docs._node_bm25_ok or not docs._read_only_node_bm25_ok(conn):
                raise PackWriteRejectedError("single SQLite node BM25 is unavailable")
            for ddl in nbm.ddl_statements():
                tokens = ddl.split()
                kind = "table" if tokens[1] == "VIRTUAL" else tokens[1].lower()
                name = tokens[6] if tokens[1] == "VIRTUAL" else tokens[5]
                require(name, kind, ddl)
            virtuals.append(nbm.FTS)
        if vec is None or not vec.available:
            raise PackWriteRejectedError("single SQLite vector store is unavailable")
        vec._check_existing_table(conn)
        require(vec._table, ddl=vec._create_table_sql(with_bit=vec._has_bit_column))
        virtuals.append(vec._table)
        table_types = {row[1]: row[2] for row in conn.execute("PRAGMA main.table_list")}
        for name in virtuals:
            if table_types.get(name) != "virtual":
                raise PackWriteRejectedError(f"single SQLite virtual table is unavailable: {name}")
            if name == vec._table:
                suffixes = ["_info", "_chunks", "_rowids", "_vector_chunks00", "_auxiliary"]
                if vec._has_bit_column:
                    suffixes.append("_vector_chunks01")
            else:
                suffixes = ["_data", "_idx", "_docsize", "_config"]
                if name == sfts.FTS:
                    suffixes.append("_content")
            for suffix in suffixes:
                shadow = name + suffix
                require(shadow)
                if name == vec._table:
                    layouts = {
                        "_info": f'CREATE TABLE "{name}_info" (key text primary key, value any)',
                        "_chunks": f'CREATE TABLE "{name}_chunks"(chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,size INTEGER NOT NULL,sequence_id integer,partition00,validity BLOB NOT NULL, rowids BLOB NOT NULL)',
                        "_rowids": f'CREATE TABLE "{name}_rowids"(rowid INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,chunk_id INTEGER,chunk_offset INTEGER)',
                        "_auxiliary": f'CREATE TABLE "{name}_auxiliary"( rowid integer PRIMARY KEY , value00, value01)',
                    }
                    shadow_ddl = layouts.get(suffix, f'CREATE TABLE "{shadow}"(rowid PRIMARY KEY,vectors BLOB NOT NULL)')
                else:
                    layouts = {
                        "_data": "(id INTEGER PRIMARY KEY, block BLOB)",
                        "_idx": "(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID",
                        "_docsize": "(id INTEGER PRIMARY KEY, sz BLOB" + (", origin INTEGER)" if name == nbm.FTS else ")"),
                        "_config": "(k PRIMARY KEY, v) WITHOUT ROWID",
                        "_content": "(id INTEGER PRIMARY KEY, c0, c1)",
                    }
                    shadow_ddl = f"CREATE TABLE '{shadow}'" + layouts[suffix]
                require(shadow, ddl=shadow_ddl)
                allowed_types = {"table", "shadow"} if name == vec._table else {"shadow"}
                if table_types.get(shadow) not in allowed_types:
                    raise PackWriteRejectedError(f"single SQLite shadow table is unavailable: {shadow}")

    def validate_pack_writes(self, packs: dict[str, str]) -> None:
        """열린 쓰기 트랜잭션에서 팩 세대와 삭제 예약을 확인한다."""
        if not self.in_unit_of_work:
            raise RuntimeError("no shared unit of work is active")
        try:
            if not packs:
                raise RuntimeError("pack write scope is empty")
            if self._uow.delete_operation is not None:
                raise PackWriteRejectedError("general writes are forbidden during deletion")
            conn = self.connection()
            self._require_unshadowed_storage(conn)
            for pack_id, incarnation in sorted(packs.items()):
                if not pack_id or not incarnation:
                    raise RuntimeError("pack identity is missing")
                previous = self._uow.validated_packs.get(pack_id)
                if previous is not None and previous != incarnation:
                    raise PackWriteRejectedError("pack incarnation changed within unit of work")
                row = conn.execute(
                    "SELECT incarnation FROM main.packs WHERE pack_id = ?", (pack_id,)
                ).fetchone()
                if row is None or row[0] != incarnation:
                    raise PackWriteRejectedError("pack incarnation changed or is missing")
                pending = conn.execute(
                    "SELECT 1 FROM main.pack_delete_operations WHERE pack_id = ? AND state = 'pending'",
                    (pack_id,),
                ).fetchone()
                if pending is not None:
                    raise PackWriteRejectedError("pack deletion is pending")
            self._uow.validated_packs.update(packs)
        except BaseException:
            self.mark_rollback_only()
            raise

    def validate_delete_operation(self, pack_id: str, incarnation: str, operation_id: str) -> None:
        """삭제 예약의 팩 세대와 작업 식별자를 같은 트랜잭션에서 확인한다."""
        if not self.in_unit_of_work:
            raise RuntimeError("no shared unit of work is active")
        try:
            if not pack_id or not incarnation or not operation_id:
                raise PackWriteRejectedError("delete operation identity is missing")
            if self._uow.validated_packs:
                raise PackWriteRejectedError("deletion cannot share general write authority")
            identity = (pack_id, incarnation, operation_id)
            if self._uow.delete_operation not in (None, identity):
                raise PackWriteRejectedError("delete operation identity changed")
            conn = self.connection()
            row = conn.execute(
                "SELECT p.incarnation, o.incarnation, o.state "
                "FROM main.packs p JOIN main.pack_delete_operations o ON o.pack_id = p.pack_id "
                "WHERE p.pack_id = ? AND o.operation_id = ?",
                (pack_id, operation_id),
            ).fetchone()
            if row is None or tuple(row) != (incarnation, incarnation, "pending"):
                raise PackWriteRejectedError("delete operation identity does not match pending")
            previous = self._uow.validated_packs.get(pack_id)
            if previous is not None and previous != incarnation:
                raise PackWriteRejectedError("pack incarnation changed within unit of work")
            self._uow.delete_operation = identity
        except BaseException:
            self.mark_rollback_only()
            raise

    @contextlib.contextmanager
    def unit_of_work(self, *, outcome: dict | None = None) -> Iterator[sqlite3.Connection]:
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
                self._uow.rollback_only = False
                self._uow.validated_packs = {}
                self._uow.delete_operation = None
                self._uow.after_commit = []
            prior_validated = self._uow.validated_packs.copy()
            prior_requested = getattr(self._uow, "requested_pack", None)
            if depth == 0:
                self._uow.requested_pack = None
            prior_delete_operation = self._uow.delete_operation
            self._uow.depth = depth + 1
            try:
                _active_units.depth = getattr(_active_units, "depth", 0) + 1
                try:
                    yield conn
                finally:
                    _active_units.depth -= 1
            except BaseException as body_exc:
                self._uow.depth = depth
                self._uow.requested_pack = prior_requested
                if depth == 0 and outcome is not None:
                    outcome["body_error"] = body_exc
                self._uow.validated_packs = prior_validated
                self._uow.delete_operation = prior_delete_operation
                self._uow.rollback_only = True
                if depth == 0:
                    self._uow.after_commit = []
                    try:
                        conn.rollback()
                        if outcome is not None:
                            outcome["state"] = "rolled_back"
                    except BaseException as rollback_exc:
                        if outcome is not None:
                            outcome["rollback_error"] = rollback_exc
                        logger.error(
                            "SharedSqliteFile.unit_of_work: rollback failed "
                            "(original exception still raised): %s", rollback_exc,
                        )
                raise
            self._uow.depth = depth
            self._uow.requested_pack = prior_requested
            self._uow.delete_operation = prior_delete_operation
            if depth == 0:
                if self._uow.rollback_only:
                    self._uow.after_commit = []
                    if outcome is not None:
                        outcome["rollback_only"] = True
                    try:
                        conn.rollback()
                    except BaseException as rollback_exc:
                        if outcome is not None:
                            outcome["rollback_error"] = rollback_exc
                        raise
                    if outcome is not None:
                        outcome["state"] = "rolled_back"
                    raise RollbackOnlyError("shared unit of work is rollback-only")
                try:
                    conn.commit()
                    if outcome is not None:
                        outcome["state"] = "committed"
                except BaseException:
                    self._uow.after_commit = []
                    # commit이 실패하면 열린 트랜잭션이 연결에 남지 않게 되돌린다.
                    try:
                        conn.rollback()
                    except BaseException as rollback_exc:
                        if outcome is not None:
                            outcome["rollback_error"] = rollback_exc
                        logger.error(
                            "SharedSqliteFile.unit_of_work: rollback after failed commit: %s",
                            rollback_exc,
                        )
                    raise
        if depth == 0:
            callbacks = self._uow.after_commit
            self._uow.after_commit = []
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    logger.exception("SharedSqliteFile: post-commit callback failed")
        if depth == 0 and self._checkpointer is not None:
            # 커밋 뒤 보조 작업의 오류를 쓰기 실패로 반환하지 않는다.
            try:
                self._checkpointer._checkpoint_if_wal_large(conn)
            except Exception:
                logger.exception("SharedSqliteFile: post-commit checkpoint failed")

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
    shared_files = [getattr(store, "shared_file", None) for store in stores]
    actual = [shared for shared in shared_files if isinstance(shared, SharedSqliteFile)]
    if not actual:
        return None
    first = actual[0]
    if any(shared is not first for shared in shared_files):
        raise RuntimeError("stores do not share the same shared SQLite file")
    return first


def is_single_file(*stores: Any) -> bool:
    """저장소들이 모두 같은 SharedSqliteFile을 나눠 쓰는가.

    공유 파일을 쓰지 않는 저장소는 False를 반환한다. 일부만 공유하거나 서로 다른 공유
    파일을 쓰는 조합은 거부한다.
    """
    return _common_shared_file(stores) is not None


def unit_of_work_for(*stores: Any) -> contextlib.AbstractContextManager[Any]:
    """저장소들이 모두 같은 SharedSqliteFile을 쓰면 그 구간을, 아니면 빈 구간을 준다."""
    shared = _common_shared_file(stores)
    if shared is None:
        return contextlib.nullcontext()
    return shared.unit_of_work()
