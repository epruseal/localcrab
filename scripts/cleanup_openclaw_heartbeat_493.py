#!/usr/bin/env python3
"""승인된 후보 레코드를 네 SQLite 저장소에서 정리한다.

기본 동작은 읽기 전용 dry-run이다. 이 도구는 라이브 서비스 제어를 하지 않는다.
후보는 두 방식 중 하나로 고른다. 기본 방식은 #493 폴링 레코드 접두 규칙이다.
`--approved-ids <파일>`을 주면 승인된 ID 집합만 후보로 삼는다(#513, #520).
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from opencrab.stores.backup import backup_sqlite, verify_sqlite

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_GUARD = 3
EXIT_INCOMPLETE = 4
DB_FILES = ("graph.db", "doc_store.db", "vectors.db", "opencrab.db")
PREFIXES = ("Q: [OpenClaw heartbeat poll]", "Q: Read HEARTBEAT.md if it exists")
EVIDENCE_SPACE = "evidence"
MARKERS = ("rollback-tombstone.json", "rollback-complete.json")
ATTEMPT_PREFIX = "rollback-attempt-"
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class CleanupRefusedError(RuntimeError):
    """도구가 쓰기 전에 중단해야 할 조건을 나타낸다."""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


def _sha(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _json_value(value: Any) -> Any:
    """원시 SQLite BLOB를 manifest에 기록하지 않고 안정적으로 식별한다."""
    if isinstance(value, bytes):
        return {"bytes_sha256": _sha(value), "bytes_length": len(value)}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _canon(value: Any) -> bytes:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_object(raw: Any, label: str) -> dict[str, Any]:
    def reject_dupe(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in pairs:
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            out[key] = item
        return out

    try:
        value = json.loads(raw, object_pairs_hook=reject_dupe)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CleanupRefusedError(f"{label} JSON is invalid") from exc
    if not isinstance(value, dict):
        raise CleanupRefusedError(f"{label} JSON is not an object")
    return value


def _strict_pack(raw: Any, label: str) -> str:
    value = _json_object(raw, label).get("pack_id")
    if not isinstance(value, str):
        raise CleanupRefusedError(f"{label} has no string metadata.pack_id")
    return value


def _props_digest(raw: Any) -> str:
    return _sha(_canon(_json_object(raw, "properties")))


@dataclasses.dataclass(frozen=True)
class _Selection:
    """후보를 어떻게 고르고 어떤 노드를 후보로 보는지 정한다.

    pack_id와 node_type은 후보 검증, 보호 집합, Claim 근거 판정, CAS 삭제가 함께 쓴다.
    ids가 None이면 #493 접두 규칙이고 집합이면 승인된 ID 집합이다.
    """

    pack_id: str
    node_type: str
    ids: frozenset[str] | None


_PREFIX_SELECTION = _Selection("openclaw", "Evidence", None)
_SELECTION: _Selection = _PREFIX_SELECTION
# 보호 섹션 분기가 이미 다루는 타입은 후보 타입이 될 수 없다.
_RESERVED_NODE_TYPES = frozenset({"Document", "Claim"})
_IDS_FILE_KEYS = {"pack_id", "node_type", "ids"}


@contextlib.contextmanager
def _selection_scope(selection: _Selection) -> Iterator[None]:
    """선택을 설정하고 나올 때 이전 값을 복원한다. 모듈 전역 _SCOPE와 같은 방식의 전역 상태이다."""
    global _SELECTION
    previous = _SELECTION
    _SELECTION = selection
    try:
        yield
    finally:
        _SELECTION = previous


def _claim_source_types() -> set[str]:
    """생존 Claim 근거는 기본 Evidence와 선택 타입을 받는다. 삭제 후보 타입과 생존 근거 타입은 다를 수 있다."""
    return {"Evidence", _SELECTION.node_type}


def _ids_digest(ids: Any) -> str:
    return _sha(_canon(sorted(ids)))


def _check_selection_fields(pack_id: Any, node_type: Any) -> None:
    if not isinstance(pack_id, str) or not pack_id:
        raise CleanupRefusedError("selection pack_id must be a non-empty string")
    if not isinstance(node_type, str) or not node_type or node_type in _RESERVED_NODE_TYPES:
        raise CleanupRefusedError("selection node_type must be a non-empty string other than Document and Claim")


def _load_approved_ids(path: Path | None) -> _Selection:
    """승인 ID 파일을 읽어 _parse_approved_ids로 검증한다."""
    if path is None or not path.is_file():
        raise CleanupRefusedError("--approved-ids file is missing")
    return _parse_approved_ids(_json_object(path.read_text(encoding="utf-8"), "approved ids"))


def _parse_approved_ids(data: dict[str, Any]) -> _Selection:
    """승인 ID 본문을 검증한다. 키는 pack_id, node_type, ids뿐이고 ids는 비어 있지 않은 중복 없는 문자열 목록이다.

    선택 스크립트도 파일을 쓰기 전에 이 함수로 본문을 검증한다. 입력 계약의 정본은 이 함수이다.
    """
    if not isinstance(data, dict) or set(data) != _IDS_FILE_KEYS:
        raise CleanupRefusedError("approved ids file has unexpected or missing fields")
    _check_selection_fields(data["pack_id"], data["node_type"])
    ids = data["ids"]
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not item for item in ids):
        raise CleanupRefusedError("approved ids must be a non-empty list of non-empty strings")
    if len(set(ids)) != len(ids):
        raise CleanupRefusedError("approved ids contain duplicates")
    return _Selection(data["pack_id"], data["node_type"], frozenset(ids))


def _selection_record(selection: _Selection) -> dict[str, Any]:
    """manifest에 동결할 선택 기록이다. 승인 파일이 manifest sha256에 묶이므로 선택도 승인에 묶인다."""
    if selection.ids is None:
        return {"mode": "prefix"}
    return {
        "mode": "id-set",
        "pack_id": selection.pack_id,
        "node_type": selection.node_type,
        "approved_count": len(selection.ids),
        "approved_ids_sha256": _ids_digest(selection.ids),
    }


def _selection_from_manifest(manifest: dict[str, Any], candidate_ids: list[str]) -> _Selection:
    """manifest의 선택 기록과 검증된 후보 파일의 ID로 선택을 복원한다.

    선택 기록이 없는 manifest는 접두 규칙이다. id-set이면 후보 ID의 개수와 sha256이 기록과 같아야 한다.
    """
    record = manifest.get("selection")
    if record is None:
        return _PREFIX_SELECTION
    if not isinstance(record, dict):
        raise CleanupRefusedError("manifest selection is invalid")
    if record.get("mode") == "prefix":
        if set(record) != {"mode"}:
            raise CleanupRefusedError("manifest selection is invalid")
        return _PREFIX_SELECTION
    if record.get("mode") != "id-set" or set(record) != {"mode", "pack_id", "node_type", "approved_count", "approved_ids_sha256"}:
        raise CleanupRefusedError("manifest selection is invalid")
    _check_selection_fields(record["pack_id"], record["node_type"])
    if record["approved_count"] != len(candidate_ids) or record["approved_ids_sha256"] != _ids_digest(candidate_ids):
        raise CleanupRefusedError("manifest selection does not match the candidate file")
    return _Selection(record["pack_id"], record["node_type"], frozenset(candidate_ids))


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    _fsync_file(path)


def _write_fsync(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path: Path, data: bytes) -> None:
    """일반 run 산출물을 원자적으로 발행한다. marker에는 쓰지 않는다."""
    tmp = path.with_name(path.name + ".tmp")
    _write_fsync(tmp, data)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _load_vec(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _ro(path: Path, immutable: bool) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return sqlite3.connect(f"{path.resolve().as_uri()}?{query}", uri=True)


def _rw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)


def _commit(conn: sqlite3.Connection) -> None:
    """Write transaction의 commit seam이다."""
    conn.commit()


def _rollback(conn: sqlite3.Connection) -> None:
    """Write transaction의 rollback seam이다."""
    conn.rollback()


def require_empty_wal(data_dir: Path) -> None:
    for name in DB_FILES:
        wal = data_dir / f"{name}-wal"
        if wal.exists() and wal.stat().st_size:
            raise CleanupRefusedError(f"{wal.name} is not empty; stop writers and checkpoint first", EXIT_GUARD)


def _table_names(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(str(row[0]), str(row[1] or "")) for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index','trigger','view') ORDER BY type,name"
    )]


def _schema_digest(data_dir: Path, vector_table: str, *, immutable: bool) -> str:
    result: dict[str, Any] = {"vector_table": vector_table}
    for name in DB_FILES:
        conn = _ro(data_dir / name, immutable)
        try:
            if name == "vectors.db":
                _load_vec(conn)
            result[name] = _table_names(conn)
        finally:
            conn.close()
    return _sha(_canon(result))


def _data_dir_identity(data_dir: Path) -> dict[str, Any]:
    """심볼릭 링크가 아닌 실제 data directory 정체성을 고정한다."""
    resolved = data_dir.resolve(strict=True)
    stat = resolved.stat()
    if not resolved.is_dir():
        raise CleanupRefusedError("local data directory is not a directory", EXIT_GUARD)
    return {"path": str(resolved), "device": stat.st_dev, "inode": stat.st_ino}


def _store_set(data_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"directory": _data_dir_identity(data_dir), "files": {}}
    for name in DB_FILES:
        path = data_dir / name
        if not path.is_file():
            raise CleanupRefusedError(f"required database is missing: {name}", EXIT_GUARD)
        stat = path.stat()
        result["files"][name] = {"device": stat.st_dev, "inode": stat.st_ino}
    return result


def _store_set_digest(data_dir: Path) -> str:
    return _sha(_canon(_store_set(data_dir)))


def _ensure_vector_table(conn: sqlite3.Connection, vector_table: str) -> list[str]:
    if not vector_table.isidentifier():
        raise CleanupRefusedError("vector table name is unsafe")
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (vector_table,)).fetchone()
    if row is None or "vec0" not in str(row[0]).lower():
        raise CleanupRefusedError("required sqlite-vec table is absent", EXIT_GUARD)
    columns = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({vector_table})")]
    required = {"node_id", "pack_id", "document", "metadata", "embedding"}
    if not required <= set(columns):
        raise CleanupRefusedError("vector table schema is incompatible", EXIT_GUARD)
    return columns


def _row_digest(row: tuple[Any, ...]) -> str:
    return _sha(_canon(list(row)))


def _registry_node_record(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"id": row[0], "space": row[1], "node_type": row[2], "node_id": row[3], "created_at": row[4], "updated_at": row[5]}


def _registry_edge_record(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"id": row[0], "from_space": row[1], "from_id": row[2], "relation": row[3], "to_space": row[4], "to_id": row[5], "created_at": row[6]}


def _edge_record(row: tuple[Any, ...]) -> dict[str, Any]:
    frm_type, frm_id, relation, to_type, to_id, props = row
    return {
        "from_type": frm_type,
        "from_id": frm_id,
        "relation": relation,
        "to_type": to_type,
        "to_id": to_id,
        "properties": _props_digest(props),
    }


def _edge_digest(edges: list[dict[str, Any]]) -> str:
    return _sha(_canon(sorted(edges, key=_canon)))



class _Readers:
    """후보 반복이 공유하는 리더 연결과 위치 색인이다.

    위치 색인은 한 번의 스캔으로 FTS rowid와 registry 행 id를 모은다. 이후 조회는
    rowid와 PK 조회로 하고 행의 식별 값을 다시 대조한다. 색인은 스캔 시점의 스냅샷이므로
    이 도구 밖의 commit이 보이면 reload()로 다시 만든다.
    """

    def __init__(self, data_dir: Path, vector_table: str, *, immutable: bool, ids: set[str] | None = None) -> None:
        self.doc = _ro(data_dir / "doc_store.db", immutable)
        self.graph = _ro(data_dir / "graph.db", immutable)
        self.vectors = _ro(data_dir / "vectors.db", immutable)
        self.registry = _ro(data_dir / "opencrab.db", immutable)
        self.vector_table = vector_table
        self._ids = ids
        try:
            _load_vec(self.vectors)
            self.columns = _ensure_vector_table(self.vectors, vector_table)
            self.reload()
        except Exception:
            self.close()
            raise

    def reload(self) -> None:
        ids = self._ids
        self.fts: dict[Any, list[int]] = {}
        for rowid, source_id in self.doc.execute("SELECT rowid,source_id FROM doc_sources_fts"):
            if ids is None or source_id in ids:
                self.fts.setdefault(source_id, []).append(rowid)
        self.reg_nodes: dict[Any, list[int]] = {}
        for row_id, node_id in self.registry.execute("SELECT id,node_id FROM ontology_nodes ORDER BY id"):
            if ids is None or node_id in ids:
                self.reg_nodes.setdefault(node_id, []).append(row_id)
        self.reg_edges: dict[Any, list[int]] = {}
        for row_id, from_id, to_id in self.registry.execute("SELECT id,from_id,to_id FROM ontology_edges ORDER BY id"):
            if ids is None or from_id in ids:
                self.reg_edges.setdefault(from_id, []).append(row_id)
            if (ids is None or to_id in ids) and to_id != from_id:
                self.reg_edges.setdefault(to_id, []).append(row_id)

    def close(self) -> None:
        for conn in (self.doc, self.graph, self.vectors, self.registry):
            conn.close()

    def fts_rows(self, node_id: str) -> list[tuple[Any, ...]]:
        rows = []
        for rowid in self.fts.get(node_id, ()):
            row = self.doc.execute("SELECT source_id,text FROM doc_sources_fts WHERE rowid=?", (rowid,)).fetchone()
            if row is not None and row[0] == node_id:
                rows.append(row)
        return rows

    def registry_nodes(self, node_id: str) -> list[tuple[Any, ...]]:
        rows = []
        for row_id in self.reg_nodes.get(node_id, ()):
            row = self.registry.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE id=?", (row_id,)).fetchone()
            if row is not None and row[3] == node_id:
                rows.append(row)
        return rows

    def registry_edges(self, node_id: str) -> list[tuple[Any, ...]]:
        rows = []
        for row_id in self.reg_edges.get(node_id, ()):
            row = self.registry.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE id=?", (row_id,)).fetchone()
            if row is not None and node_id in (row[2], row[5]):
                rows.append(row)
        return rows


class _Borrowed:
    """apply 동안 유지하는 쓰기 연결을 삭제 함수가 빌려 쓰게 한다. close()는 동작하지 않는다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def close(self) -> None:
        return None


class _Witness:
    """이 도구 밖의 commit을 네 DB의 쓰기 연결 자신의 data_version으로 감시한다.

    쓰기 연결 자신의 data_version은 자기 commit으로 바뀌지 않고 다른 연결의 commit으로만 바뀐다.
    그래서 이 도구의 commit 뒤에 기준값을 다시 잡을 필요가 없고 외부 commit을 흡수하지 않는다.
    가정: 네 DB에 쓰는 다른 연결은 SQLite 연결이다. 파일 통째 교체는 data_version이 보지 못한다.
    """

    def __init__(self, data_dir: Path) -> None:
        self._writers = {name: _rw(data_dir / name) for name in DB_FILES}
        self._seen: dict[str, Any] = {}
        self.adopt(self.snapshot())

    def version(self, name: str) -> Any:
        return self._writers[name].execute("PRAGMA data_version").fetchone()[0]

    def snapshot(self) -> dict[str, Any]:
        return {name: self.version(name) for name in DB_FILES}

    def adopt(self, versions: dict[str, Any]) -> None:
        self._seen = dict(versions)

    def seen(self) -> dict[str, Any]:
        return dict(self._seen)

    def changed(self) -> list[str]:
        return [name for name in DB_FILES if self.version(name) != self._seen[name]]

    def writer(self, name: str) -> sqlite3.Connection:
        return self._writers[name]

    def close(self) -> None:
        for conn in self._writers.values():
            conn.close()


class _RunScope:
    """한 번의 apply 동안 동결 입력에서 파생한 값과 확인 결과를 보관한다."""

    def __init__(self, data_dir: Path, vector_table: str, candidates: Any, manifest: dict[str, Any]) -> None:
        self.data_dir = data_dir
        self.vector_table = vector_table
        self.candidates = candidates
        self.manifest = manifest
        self.receipt: dict[str, Any] | None = None
        self.protected_digest: str | None = None
        self.memo: dict[Any, Any] = {}
        self.passed: set[Any] = set()
        self.witness: _Witness | None = None
        self.readers: _Readers | None = None
        self.site = "record"
        self.verified_digest: str | None = None
        self.dirty = False
        self.full_snapshot_verified = False
        self.wrote = False


_SCOPE: _RunScope | None = None


def _once(key: Any, compute: Callable[[], Any]) -> Any:
    """apply 동안 순수 계산 결과를 한 번만 만든다. scope가 없으면 매번 계산한다."""
    if _SCOPE is None:
        return compute()
    if key not in _SCOPE.memo:
        _SCOPE.memo[key] = compute()
    return _SCOPE.memo[key]


def _scope_for(candidates: Any, manifest: dict[str, Any]) -> _RunScope | None:
    """호출 인자가 현재 apply의 동결 입력과 같은 객체일 때만 scope를 돌려준다."""
    scope = _SCOPE
    if scope is not None and candidates is scope.candidates and manifest is scope.manifest:
        return scope
    return None


def _manifest_sha(manifest: dict[str, Any]) -> str:
    scope = _SCOPE
    if scope is not None and manifest is scope.manifest:
        return _once(("manifest_sha",), lambda: _sha(_canon(manifest)))
    return _sha(_canon(manifest))


def _writer(data_dir: Path, name: str) -> Any:
    """apply 중에는 유지 중인 쓰기 연결을 빌리고 아니면 새 쓰기 연결을 연다."""
    scope = _SCOPE
    if scope is not None and scope.witness is not None and scope.data_dir == data_dir:
        return _Borrowed(scope.witness.writer(name))
    return _rw(data_dir / name)


def _has_triggers(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0])


def _require_no_triggers(witness: _Witness) -> None:
    """삭제가 다른 행을 바꾸게 만드는 trigger가 있으면 쓰기 전에 거부한다. 기준값을 잡은 뒤 같은 연결로 확인한다."""
    for name in DB_FILES:
        if _has_triggers(witness.writer(name)):
            raise CleanupRefusedError(f"{name} has a trigger; remove it before apply", EXIT_GUARD)


def _change_pending() -> bool:
    scope = _SCOPE
    return scope is not None and scope.witness is not None and bool(scope.witness.changed())


def _handle_change() -> None:
    """이 도구 밖의 commit이 보일 때 예전과 같은 판정 경로로 되돌아간다.

    기준값은 처음 읽은 값만 채택한다. 거부 순서와 메시지는 기존 코드의 순서를 따른다: 보호 digest
    불일치는 여기서 거부하지 않고 인라인 검사와 축 끝 확인이 기존 위치에서 거부한다.
    """
    scope = _SCOPE
    assert scope is not None and scope.witness is not None and scope.readers is not None
    witness = scope.witness
    before = witness.snapshot()
    for name in DB_FILES:
        if _has_triggers(witness.writer(name)):
            raise CleanupRefusedError(f"{name} has a trigger; remove it before apply", EXIT_GUARD)
    if scope.receipt is not None:
        _validate_graph_intent(witness.writer("graph.db"), scope.candidates, scope.receipt, scope.manifest)
    scope.readers.reload()
    scope.passed.clear()
    scope.verified_digest = None
    scope.full_snapshot_verified = False
    if witness.snapshot() != before:
        raise CleanupRefusedError("protected set changed")
    witness.adopt(before)
    scope.dirty = True


def _begin_write(conn: Any) -> None:
    """쓰기 잠금을 얻은 직후 네 DB의 감시값을 확인한다. 다르면 rollback하고 변화를 처리한 뒤 트랜잭션을 다시 시작한다."""
    conn.execute("BEGIN IMMEDIATE")
    while _change_pending():
        _rollback(conn)
        _handle_change()
        conn.execute("BEGIN IMMEDIATE")


class _SectionHash:
    """행 단위 item의 순서 무관 다중집합 해시(행 수와 sha256 합 모듈로 2^256)이다.

    의도적으로 깎은 모서리: 합 해시는 적대적 충돌(일반화된 생일 공격)에 약하다. 위협 모델은 사고와
    비적대적 외부 편집이다. 더 강한 해시가 필요하면 정렬된 연쇄 해시로 바꾼다(vec0는 정렬 스캔이 없다).
    """

    __slots__ = ("count", "acc")
    _MOD = 1 << 256

    def __init__(self) -> None:
        self.count = 0
        self.acc = 0

    def add(self, item: Any) -> None:
        self.count += 1
        self.acc = (self.acc + int.from_bytes(hashlib.sha256(_canon(item)).digest(), "big")) % self._MOD

    def result(self) -> dict[str, Any]:
        return {"count": self.count, "sha256": f"{self.acc:064x}"}


class _CandidateStore:
    """후보 레코드를 canonical JSON 줄로 파일에 두고 순회할 때 파싱한다.

    상주하는 것은 id, 줄 오프셋, 길이, 줄별 sha256이다. 다시 읽을 때마다 읽은 바이트를 줄별 값과
    대조하므로 검증 뒤 파일이 바뀌어도 다른 레코드를 받지 못한다.
    """

    @staticmethod
    def _key(record: dict[str, Any]) -> str:
        return record["id"]

    def __init__(self, path: Path) -> None:
        self.path = path
        self.ids: list[str] = []
        self._index: list[tuple[int, int, bytes]] = []
        self._whole = hashlib.sha256()
        self._fd: int | None = None
        self._writer: Any = None
        self._tmp: Path | None = None

    @classmethod
    def create(cls, path: Path) -> _CandidateStore:
        store = cls(path)
        store._tmp = path.with_name(path.name + ".tmp")
        store._writer = store._tmp.open("wb")
        return store

    @classmethod
    def load(cls, path: Path) -> _CandidateStore:
        """파일을 줄 단위로 읽으며 해시와 id를 모은다. 레코드는 줄마다 검증용으로 한 번만 파싱한다."""
        store = cls(path)
        offset = 0
        with path.open("rb") as handle:
            for line in handle:
                store._whole.update(line)
                body = line.rstrip(b"\n")
                if body:
                    record = _json_object(body.decode("utf-8"), "candidate")
                    store._index.append((offset, len(line), hashlib.sha256(line).digest()))
                    store.ids.append(cls._key(record))
                offset += len(line)
        store._fd = os.open(path, os.O_RDONLY)
        return store

    def append(self, record: dict[str, Any]) -> None:
        line = _canon(record) + b"\n"
        offset = self._writer.tell()
        self._writer.write(line)
        self._whole.update(line)
        self._index.append((offset, len(line), hashlib.sha256(line).digest()))
        self.ids.append(self._key(record))

    def finish(self) -> None:
        """기존 _publish와 같은 순서로 임시 파일을 fsync하고 교체한다."""
        assert self._writer is not None and self._tmp is not None
        self._writer.flush()
        os.fsync(self._writer.fileno())
        self._writer.close()
        self._writer = None
        os.replace(self._tmp, self.path)
        _fsync_dir(self.path.parent)
        self._fd = os.open(self.path, os.O_RDONLY)

    @property
    def sha256(self) -> str:
        return self._whole.hexdigest()

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __len__(self) -> int:
        return len(self._index)

    def __getitem__(self, position: Any) -> Any:
        if isinstance(position, slice):
            return [self[index] for index in range(*position.indices(len(self._index)))]
        offset, length, digest = self._index[position]
        assert self._fd is not None
        line = os.pread(self._fd, length, offset)
        if len(line) != length or hashlib.sha256(line).digest() != digest:
            raise CleanupRefusedError("candidate file does not match manifest")
        return json.loads(line)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for position in range(len(self._index)):
            yield self[position]


class _EvidenceStore(_CandidateStore):
    """Claim 판정이 쓰는 비후보 Evidence 항목의 순차 저장소이다. 키는 graph id이고 get()으로 한 항목만 읽는다."""

    @staticmethod
    def _key(record: dict[str, Any]) -> str:
        return record["graph"]["id"]

    def get(self, key: str) -> dict[str, Any] | None:
        position = self._positions().get(key)
        return None if position is None else self[position]

    def _positions(self) -> dict[str, int]:
        positions = getattr(self, "_pos", None)
        if positions is None or len(positions) != len(self.ids):
            positions = {key: position for position, key in enumerate(self.ids)}
            self._pos = positions
        return positions


class _Manifest(dict):
    """manifest 사전에 run 위치를 붙인다. 직렬화에는 영향이 없다. evidence 항목은 sidecar 파일에서 읽는다."""

    run_dir: Path | None = None
    selection: _Selection = _PREFIX_SELECTION
    _evidence: _EvidenceStore | None = None

    def evidence_store(self) -> _EvidenceStore:
        if self._evidence is None:
            descriptor = self["protected"]["evidence"]
            if self.run_dir is None:
                raise CleanupRefusedError("evidence file location is unknown")
            store = _EvidenceStore.load(self.run_dir / descriptor["file"])
            if store.sha256 != descriptor.get("sha256") or len(store) != descriptor.get("count"):
                store.close()
                raise CleanupRefusedError("evidence file does not match manifest")
            self._evidence = store
        return self._evidence

    def close(self) -> None:
        if self._evidence is not None:
            self._evidence.close()
            self._evidence = None


def _evidence_map(manifest: dict[str, Any]) -> Any:
    """영향 Claim을 지지하는 비후보 Evidence 항목을 graph id로 조회하는 대응을 돌려준다."""
    evidence = manifest["protected"]["evidence"]
    if isinstance(evidence, _EvidenceStore):
        return evidence
    if isinstance(evidence, dict):
        return manifest.evidence_store()  # type: ignore[attr-defined]
    return {item["graph"]["id"]: item for item in evidence}


def _registry_snapshot(data_dir: Path, node_id: str, *, immutable: bool, readers: _Readers | None = None) -> dict[str, Any]:
    if readers is None:
        registry = _ro(data_dir / "opencrab.db", immutable)
        try:
            nodes = registry.execute(
                "SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=? ORDER BY id",
                (node_id,),
            ).fetchall()
            edges = registry.execute(
                "SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges "
                "WHERE from_id=? OR to_id=? ORDER BY id",
                (node_id, node_id),
            ).fetchall()
        finally:
            registry.close()
    else:
        nodes = readers.registry_nodes(node_id)
        edges = readers.registry_edges(node_id)
    target_edges = []
    foreign_edges = []
    for row in edges:
        edge = _registry_edge_record(row)
        endpoints = ((edge["from_space"], edge["from_id"]), (edge["to_space"], edge["to_id"]))
        same_id_foreign = any(endpoint_id == node_id and endpoint_space != EVIDENCE_SPACE for endpoint_space, endpoint_id in endpoints)
        if same_id_foreign:
            raise CleanupRefusedError(f"{node_id}: registry edge foreign tuple")
        if (EVIDENCE_SPACE, node_id) in endpoints:
            target_edges.append(edge)
        else:
            foreign_edges.append(edge)
    return {
        "registry_node": [_registry_node_record(row) for row in nodes],
        "registry_edges": target_edges,
        "registry_foreign": foreign_edges,
    }


def _candidate_snapshot(data_dir: Path, vector_table: str, node_id: str, *, immutable: bool, readers: _Readers | None = None) -> dict[str, Any]:
    own = readers is None
    if own:
        readers = _Readers(data_dir, vector_table, immutable=immutable, ids={node_id})
    try:
        doc, graph, vectors, cols = readers.doc, readers.graph, readers.vectors, readers.columns
        source_rows = doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (node_id,)).fetchall()
        fts_rows = readers.fts_rows(node_id)
        node_rows = doc.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (node_id,)).fetchall()
        graph_rows = graph.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (node_id,)).fetchall()
        edge_rows = graph.execute(
            "SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? OR to_id=? ORDER BY from_id,relation,to_id",
            (node_id, node_id),
        ).fetchall()
        selected = ["node_id", "pack_id", "document", "metadata", "embedding"]
        if "embedding_bit" in cols:
            selected.append("embedding_bit")
        vector_rows = vectors.execute(
            f"SELECT {','.join(selected)} FROM {vector_table} WHERE node_id=?", (node_id,)
        ).fetchall()
        return {
            "source": [_row_digest(r) for r in source_rows],
            "fts": [_row_digest(r) for r in fts_rows],
            "doc": [_row_digest(r) for r in node_rows],
            "graph": [_row_digest(r) for r in graph_rows],
            "edges": _edge_digest([_edge_record(r) for r in edge_rows]),
            "vector": [_row_digest(r) for r in vector_rows],
            **_registry_snapshot(data_dir, node_id, immutable=immutable, readers=readers),
        }
    finally:
        if own:
            readers.close()


def _select_candidate(data_dir: Path, vector_table: str, source: tuple[Any, ...], readers: _Readers | None = None) -> dict[str, Any]:
    node_id, text, metadata = source
    if _SELECTION.ids is None:
        if not isinstance(node_id, str) or not node_id.startswith("evidence:turn:"):
            raise CleanupRefusedError("internal selection received an invalid source id")
        if not isinstance(text, str) or not text.startswith(PREFIXES):
            raise CleanupRefusedError("internal selection received a non-heartbeat source")
    elif not isinstance(node_id, str) or node_id not in _SELECTION.ids:
        raise CleanupRefusedError("internal selection received an unapproved source id")
    if _strict_pack(metadata, f"source {node_id}") != _SELECTION.pack_id:
        raise CleanupRefusedError("internal selection received another pack")
    own = readers is None
    if own:
        readers = _Readers(data_dir, vector_table, immutable=True, ids={node_id})
    try:
        doc, graph, vectors, columns = readers.doc, readers.graph, readers.vectors, readers.columns
        fts = readers.fts_rows(node_id)
        docs = doc.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (node_id,)).fetchall()
        nodes = graph.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (node_id,)).fetchall()
        select = ["node_id", "pack_id", "document", "metadata", "embedding"]
        if "embedding_bit" in columns:
            select.append("embedding_bit")
        vectors_rows = vectors.execute(f"SELECT {','.join(select)} FROM {vector_table} WHERE node_id=?", (node_id,)).fetchall()
        errors: list[str] = []
        if len(fts) != 1 or tuple(fts[0]) != (node_id, text):
            errors.append("source FTS identity")
        if len(docs) != 1:
            errors.append("evidence doc multiplicity")
        elif docs[0][0] != EVIDENCE_SPACE or docs[0][2] != _SELECTION.node_type or _strict_pack(docs[0][3], f"doc {node_id}") != _SELECTION.pack_id:
            errors.append("evidence doc ownership")
        if len(nodes) != 1:
            errors.append("Evidence graph multiplicity")
        elif nodes[0][0] != _SELECTION.node_type or nodes[0][2] != EVIDENCE_SPACE or _strict_pack(nodes[0][3], f"graph {node_id}") != _SELECTION.pack_id:
            errors.append("Evidence graph ownership")
        if len(vectors_rows) != 1:
            errors.append("vector multiplicity")
        else:
            row = vectors_rows[0]
            if row[1] != _SELECTION.pack_id or _strict_pack(row[3], f"vector {node_id}") != _SELECTION.pack_id:
                errors.append("vector ownership")
            elif "node_id" in _json_object(row[3], f"vector {node_id}") and _json_object(row[3], f"vector {node_id}")["node_id"] != node_id:
                # 신원은 vec0 PK 열 node_id이다. 운영 writer는 metadata에 node_id를 넣지 않으므로 키가 있을 때만 대조한다.
                errors.append("vector identity")
        registry_nodes = readers.registry_nodes(node_id)
        registry_records = [_registry_node_record(row) for row in registry_nodes]
        if len(registry_records) != 1:
            errors.append("registry node multiplicity")
        elif (registry_records[0]["space"], registry_records[0]["node_type"], registry_records[0]["node_id"]) != (EVIDENCE_SPACE, _SELECTION.node_type, node_id):
            errors.append("registry node ownership")
        if errors:
            raise CleanupRefusedError(f"{node_id}: " + ", ".join(errors))
        edges = graph.execute(
            "SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? OR to_id=? ORDER BY from_id,relation,to_id",
            (node_id, node_id),
        ).fetchall()
        snap = _candidate_snapshot(data_dir, vector_table, node_id, immutable=True, readers=readers)
        return {
            "id": node_id,
            "source": _row_digest(source),
            "doc": {"space": docs[0][0], "type": docs[0][2], "properties": _props_digest(docs[0][3])},
            "graph": {"space": nodes[0][2], "type": nodes[0][0], "properties": _props_digest(nodes[0][3])},
            "vector": {"table": vector_table, "digest": _row_digest(vectors_rows[0]), "columns": columns},
            "edges": [_edge_record(edge) for edge in edges],
            "snapshot": snap,
        }
    finally:
        if own:
            readers.close()


def _graph_sections(conn: sqlite3.Connection, candidate_ids: set[str], *, raw: bool, excluded_nodes: set[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """graph.db의 보호 섹션을 스트리밍으로 계산한다.

    후보에 닿는 edge는 후보 삭제 대상이므로 보호 집합에서 뺀다. 반환의 두 번째 값은 manifest에 동결할
    파생 값(비후보 Evidence id, Claim 노드 항목)이다.
    """
    excluded_nodes = excluded_nodes or set()
    nodes, evidence, evidence_raw, edges = _SectionHash(), _SectionHash(), _SectionHash(), _SectionHash()
    protected_types = _claim_source_types()  # 후보와 같은 타입의 비후보도 보호한다
    evidence_ids: list[str] = []
    claims: list[dict[str, Any]] = []
    for row in conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes"):
        node_type, node_id, space, raw_props = row
        if node_id in excluded_nodes:
            continue
        properties = _props_digest(raw_props)  # 모든 노드의 properties JSON을 검증한다(기존 _protected와 같은 거부 조건)
        if node_type in {"Document", "Claim"}:
            item = {"id": node_id, "type": node_type, "space": space, "properties": properties}
            nodes.add(item)
            if node_type == "Claim":
                claims.append(item)
        elif node_type in protected_types and node_id not in candidate_ids:
            evidence.add({"id": node_id, "type": node_type, "space": space, "properties": properties})
            if raw:
                evidence_raw.add({"id": node_id, "row": _json_value(row)})
            evidence_ids.append(node_id)
    for row in conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges"):
        edge = _edge_record(row)  # 후보에 닿는 edge도 properties JSON을 검증한다
        if row[1] not in candidate_ids and row[4] not in candidate_ids:
            edges.add(edge)
    sections = {"nodes": nodes.result(), "evidence_graph": evidence.result(), "edges": edges.result()}
    if raw:
        sections["evidence_graph_raw"] = evidence_raw.result()
    claims.sort(key=lambda item: item["id"])
    return sections, {"evidence_ids": sorted(evidence_ids), "claims": claims}


def _doc_sections(doc: sqlite3.Connection, evidence_ids: Any, *, doc_nodes: bool = True) -> dict[str, Any]:
    """동결한 비후보 Evidence id의 doc_nodes와 doc_sources 행을 섹션으로 만든다."""
    nodes, sources = _SectionHash(), _SectionHash()
    for node_id in evidence_ids:
        if doc_nodes:
            for row in doc.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (node_id,)):
                nodes.add({"id": node_id, "row": _row_digest(row)})
        for row in doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (node_id,)):
            sources.add({"id": node_id, "row": _row_digest(row)})
    result = {"evidence_source": sources.result()}
    if doc_nodes:
        result["evidence_doc"] = nodes.result()
    return result


def _vector_section(vectors: sqlite3.Connection, vector_table: str, evidence_ids: set[str]) -> dict[str, Any]:
    """vec0를 자연 순서로 한 번 훑는다. 22만 번의 점 조회보다 훨씬 싸고 합 해시는 순서에 무관하다."""
    section = _SectionHash()
    for row in vectors.execute(f"SELECT node_id,pack_id,document,metadata,embedding FROM {vector_table}"):
        if row[0] in evidence_ids:
            section.add({"id": row[0], "row": _row_digest(row)})
    return {"evidence_vector": section.result()}


def _registry_sections(registry: sqlite3.Connection, candidate_ids: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """후보 target이 아닌 registry node와 후보 target에 닿지 않는 edge를 섹션으로 만든다."""
    targets = {(EVIDENCE_SPACE, candidate_id) for candidate_id in candidate_ids}
    nodes, edges = _SectionHash(), _SectionHash()
    claim_nodes: list[dict[str, Any]] = []
    for row in registry.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id"):
        if (row[1], row[3]) not in targets:
            item = _registry_node_record(row)
            nodes.add(item)
            if item["space"] == "claim" and item["node_type"] == "Claim":
                claim_nodes.append(item)
    for row in registry.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id"):
        if (row[1], row[2]) not in targets and (row[4], row[5]) not in targets:
            edges.add(_registry_edge_record(row))
    return {"registry_nodes": nodes.result(), "registry_edges": edges.result()}, claim_nodes


def _protection_sections(data_dir: Path, vector_table: str, candidate_ids: set[str], *, immutable: bool = True, evidence_ids: Any = None, raw: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """보호 집합 전체를 섹션별 (행 수, 합 해시)로 만든다. 행 전체를 메모리에 올리지 않는다."""
    doc = _ro(data_dir / "doc_store.db", immutable)
    graph = _ro(data_dir / "graph.db", immutable)
    vectors = _ro(data_dir / "vectors.db", immutable)
    registry = _ro(data_dir / "opencrab.db", immutable)
    try:
        _load_vec(vectors)
        _ensure_vector_table(vectors, vector_table)
        sections, aux = _graph_sections(graph, candidate_ids, raw=raw)
        ids = aux["evidence_ids"] if evidence_ids is None else evidence_ids
        sections.update(_doc_sections(doc, ids))
        sections.update(_vector_section(vectors, vector_table, set(ids)))
        registry_part, registry_claims = _registry_sections(registry, candidate_ids)
        sections.update(registry_part)
        aux["registry_claims"] = registry_claims
        return sections, aux
    finally:
        doc.close()
        graph.close()
        vectors.close()
        registry.close()


def _protection_digest(sections: dict[str, Any]) -> str:
    return _sha(_canon(sections))


def _evidence_item(doc: sqlite3.Connection, graph: sqlite3.Connection, vectors: sqlite3.Connection, registry: sqlite3.Connection, vector_table: str, node_id: str, candidate_ids: set[str]) -> dict[str, Any] | None:
    """Claim 근거 판정에 쓸 비후보 항목을 동결한다. 허용 근거 타입이 아니거나 후보이면 None이다."""
    row = graph.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (node_id,)).fetchone()
    if row is None or row[0] not in _claim_source_types() or node_id in candidate_ids:
        return None
    source_rows = doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (node_id,)).fetchall()
    registry_rows = registry.execute(
        "SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE space=? AND node_id=?",
        (EVIDENCE_SPACE, node_id),
    ).fetchall()
    return {
        "graph": {"id": node_id, "type": row[0], "space": row[2], "properties": _props_digest(row[3])},
        "graph_raw": _json_value(row),
        "source_raw": [_json_value(x) for x in source_rows],
        "registry": [_registry_node_record(x) for x in registry_rows],
    }


def _protected_detail(data_dir: Path, vector_table: str, candidates: Any, candidate_ids: set[str], evidence_store: _EvidenceStore | None = None) -> dict[str, Any]:
    """manifest에 동결할 protected 항목을 만든다. 행 전체 대신 섹션 digest와 Claim 판정 입력만 담는다."""
    sections, aux = _protection_sections(data_dir, vector_table, candidate_ids)
    provisional = {"protected": {"nodes": aux["claims"], "registry_nodes": aux["registry_claims"], "evidence": []}}
    graph_claims = _frozen_claim_supports(candidates, provisional, axis="graph")
    registry_claims = _frozen_claim_supports(candidates, provisional, axis="registry")
    doc = _ro(data_dir / "doc_store.db", True)
    graph = _ro(data_dir / "graph.db", True)
    vectors = _ro(data_dir / "vectors.db", True)
    registry = _ro(data_dir / "opencrab.db", True)
    try:
        _load_vec(vectors)
        _ensure_vector_table(vectors, vector_table)
        support_ids: set[str] = set()
        for _space, claim_id in graph_claims:
            for (from_id,) in graph.execute("SELECT from_id FROM graph_edges WHERE relation='supports' AND to_id=?", (claim_id,)):
                support_ids.add(from_id)
        if registry_claims:
            for from_id, to_space, to_id in registry.execute("SELECT from_id,to_space,to_id FROM ontology_edges WHERE relation='supports'"):
                if (to_space, to_id) in registry_claims:
                    support_ids.add(from_id)
        evidence: Any = evidence_store if evidence_store is not None else []
        for node_id in sorted(support_ids):
            item = _evidence_item(doc, graph, vectors, registry, vector_table, node_id, candidate_ids)
            if item is not None:
                evidence.append(item)
        if evidence_store is not None:
            evidence_store.finish()
    finally:
        doc.close()
        graph.close()
        vectors.close()
        registry.close()
    return {
        "sections": sections,
        "nodes": aux["claims"],
        "evidence": evidence,
        "registry_nodes": aux["registry_claims"],
        "evidence_ids": aux["evidence_ids"],
    }


def _valid_claim_source(
    graph: sqlite3.Connection,
    registry: sqlite3.Connection,
    doc: sqlite3.Connection,
    frozen: dict[str, Any] | None,
    source_id: str,
) -> bool:
    """현재와 동결 상태의 근거 타입, 팩, 공간과 행이 같은지 확인한다."""
    if frozen is None:
        return False
    source = doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (source_id,)).fetchall()
    graph_node = graph.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (source_id,)).fetchone()
    registry_nodes = registry.execute(
        "SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE space=? AND node_id=?",
        (EVIDENCE_SPACE, source_id),
    ).fetchall()
    frozen_source = frozen.get("source_raw")
    frozen_registry = frozen.get("registry")
    if len(source) != 1 or graph_node is None or len(registry_nodes) != 1 or not isinstance(frozen_source, list) or not isinstance(frozen_registry, list):
        return False
    registry_record = _registry_node_record(registry_nodes[0])
    frozen_graph = frozen.get("graph")
    source_type = frozen_graph.get("type") if isinstance(frozen_graph, dict) else None
    return (
        len(frozen_source) == 1
        and _strict_pack(frozen_source[0][2], f"Frozen Claim source {source_id}") == _SELECTION.pack_id
        and _strict_pack(source[0][2], f"Claim source {source_id}") == _SELECTION.pack_id
        and frozen_graph is not None
        and isinstance(frozen.get("graph_raw"), list)
        and _strict_pack(frozen["graph_raw"][3], f"Frozen Claim graph {source_id}") == _SELECTION.pack_id
        and source_type in _claim_source_types()
        and graph_node[0] == source_type and graph_node[1] == source_id and graph_node[2] == EVIDENCE_SPACE
        and _strict_pack(graph_node[3], f"Claim graph {source_id}") == _SELECTION.pack_id
        and len(frozen_registry) == 1
        and frozen_registry[0]["space"] == EVIDENCE_SPACE
        and frozen_registry[0]["node_type"] == source_type
        and frozen_registry[0]["node_id"] == source_id
        and registry_record == frozen_registry[0]
        and [_json_value(row) for row in source] == frozen_source
        and _strict_equal({"id": source_id, "type": graph_node[0], "space": graph_node[2], "properties": _props_digest(graph_node[3])}, frozen_graph)
    )


def _claim_failures(
    graph: sqlite3.Connection,
    registry: sqlite3.Connection,
    doc: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    axis: str,
    planned_edges: set[str] | None = None,
    planned_nodes: set[str] | None = None,
) -> list[str]:
    """현재 connection projection에서 frozen Claim support를 검사한다."""
    planned_edges = planned_edges or set()
    planned_nodes = planned_nodes or set()
    candidate_ids = {record["id"] for record in candidates}
    affected = _frozen_claim_supports(candidates, manifest, axis=axis)
    protected = _evidence_map(manifest)
    bad: list[str] = []
    supports_by_target: dict[tuple[Any, Any], list[tuple[Any, ...]]] = {}
    if axis == "registry" and affected:
        for row in registry.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE relation='supports'"):
            if (row[4], row[5]) in affected:
                supports_by_target.setdefault((row[4], row[5]), []).append(row)
    for claim_space, claim_id in sorted(affected):
        if axis == "graph":
            claim = graph.execute("SELECT node_type,node_id,space_id FROM graph_nodes WHERE node_id=?", (claim_id,)).fetchone()
            if claim is None or claim != ("Claim", claim_id, claim_space) or claim_id in planned_nodes:
                bad.append(claim_id)
                continue
            rows = graph.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE relation='supports' AND to_id=?", (claim_id,)).fetchall()
            supports = [_edge_record(row) for row in rows]
            valid = any(
                edge["from_type"] in _claim_source_types() and edge["to_type"] == "Claim"
                and edge["from_type"] == ((protected.get(edge["from_id"]) or {}).get("graph") or {}).get("type")
                and edge["from_id"] not in candidate_ids
                and _sha(_canon(edge)) not in planned_edges
                and _valid_claim_source(graph, registry, doc, protected.get(edge["from_id"]), edge["from_id"])
                for edge in supports
            )
        else:
            claim = registry.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE space=? AND node_id=?", (claim_space, claim_id)).fetchone()
            if claim is None or _registry_node_record(claim)["space"] != "claim" or _registry_node_record(claim)["node_type"] != "Claim" or claim_id in planned_nodes:
                bad.append(claim_id)
                continue
            supports = [_registry_edge_record(row) for row in supports_by_target.get((claim_space, claim_id), ())]
            valid = any(
                edge["from_space"] == EVIDENCE_SPACE and edge["from_id"] not in candidate_ids
                and _sha(_canon(edge)) not in planned_edges
                and _valid_claim_source(graph, registry, doc, protected.get(edge["from_id"]), edge["from_id"])
                for edge in supports
            )
        if not valid:
            bad.append(claim_id)
    return sorted(bad)


def _claim_guard(data_dir: Path, candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    graph = _ro(data_dir / "graph.db", True)
    registry = _ro(data_dir / "opencrab.db", True)
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        return _claim_failures(graph, registry, doc, candidates, manifest, axis="graph")
    finally:
        graph.close()
        registry.close()
        doc.close()


def _registry_claim_guard(data_dir: Path, candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    registry = _ro(data_dir / "opencrab.db", True)
    graph = _ro(data_dir / "graph.db", True)
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        return _claim_failures(graph, registry, doc, candidates, manifest, axis="registry")
    finally:
        registry.close()
        graph.close()
        doc.close()


def _frozen_claim_supports(candidates: list[dict[str, Any]], manifest: dict[str, Any], *, axis: str) -> set[tuple[str, str]]:
    if axis == "graph":
        supports = [
            edge for record in candidates for edge in record["edges"]
            if edge["relation"] == "supports" and edge["from_type"] == _SELECTION.node_type
        ]
        claims = {
            (item["space"], item["id"])
            for item in manifest["protected"]["nodes"]
            if item["space"] == "claim" and item["type"] == "Claim"
        }
        return {(edge["to_type"].lower(), edge["to_id"]) for edge in supports if (edge["to_type"].lower(), edge["to_id"]) in claims}
    if axis == "registry":
        claims = {
            (node["space"], node["node_id"])
            for node in manifest["protected"]["registry_nodes"]
            if node["space"] == "claim" and node["node_type"] == "Claim"
        }
        return {
            (edge["to_space"], edge["to_id"])
            for record in candidates for edge in record["snapshot"]["registry_edges"]
            if edge["relation"] == "supports" and (edge["from_space"], edge["from_id"]) == (EVIDENCE_SPACE, record["id"]) and (edge["to_space"], edge["to_id"]) in claims
        }
    raise CleanupRefusedError("unknown Claim support axis")


def _snapshot_now(data_dir: Path, vector_table: str, node_id: str) -> dict[str, Any]:
    """현재 상태 snapshot이다. apply 중에는 공유 리더와 위치 색인을 쓴다."""
    scope = _SCOPE
    readers = scope.readers if scope is not None else None
    return _candidate_snapshot(data_dir, vector_table, node_id, immutable=False, readers=readers)


def _ids_of(candidates: Any) -> set[str]:
    return set(candidates.ids) if isinstance(candidates, _CandidateStore) else {record["id"] for record in candidates}


def _verify_protection(data_dir: Path, vector_table: str, candidates: Any, protected_digest: str) -> bool:
    """보호 집합 digest가 동결 값과 같은지 확인한다. witness가 변화 없음을 보이면 직전 확인을 재사용한다."""
    scope = _SCOPE
    if scope is None or scope.witness is None:
        return _current_protection_digest(data_dir, vector_table, candidates) == protected_digest
    # 값을 한 번만 읽어 채택된 기준값과 대조한다. 대조와 읽기 사이에 끼어든 commit을 기준값으로 흡수하지 않는다.
    before = scope.witness.snapshot()
    if before != scope.witness.seen():
        return False
    if scope.verified_digest == protected_digest:
        return True
    ok = _current_protection_digest(data_dir, vector_table, candidates, immutable=False) == protected_digest
    if ok and scope.witness.snapshot() == before:
        scope.verified_digest = protected_digest
        return True
    return False


def _fresh_snapshot(data_dir: Path, vector_table: str, node_id: str) -> dict[str, Any]:
    """snapshot을 읽은 뒤 외부 변화가 없었는지 확인한다. 변화가 있었으면 위치 색인을 다시 만들고 다시 읽는다."""
    scope = _SCOPE
    for _attempt in range(4):
        snapshot = _snapshot_now(data_dir, vector_table, node_id)
        if scope is None or scope.witness is None or not _change_pending():
            return snapshot
        _handle_change()
    raise CleanupRefusedError(f"{node_id}: database set kept changing while reading a snapshot")


def _recheck_before_first_write(data_dir: Path, vector_table: str, candidates: Any) -> None:
    _registry_resume_precheck(data_dir, vector_table, candidates)


def _axis_gate(data_dir: Path | None = None, vector_table: str | None = None, candidates: Any = None) -> bool:
    """축을 시작할 때 이 도구 밖의 commit이 보이면 변화를 처리한다. 처리했으면 True를 돌려준다.

    첫 쓰기 전이면 전량 사전 검사도 다시 돌린다.
    """
    if not _change_pending():
        return False
    _handle_change()
    scope = _SCOPE
    if candidates is not None and scope is not None and not scope.wrote:
        _recheck_before_first_write(data_dir, vector_table, candidates)  # type: ignore[arg-type]
    return True


def _axis_end_check(data_dir: Path, vector_table: str, candidates: Any, protected_digest: str, label: str) -> bool:
    """기존의 축 끝 전체 digest 자리이다. 외부 변화가 보였거나 확인 대기 중일 때만 전체 digest를 계산한다."""
    scope = _SCOPE
    if scope is None or scope.witness is None:
        if _current_protection_digest(data_dir, vector_table, candidates) != protected_digest:
            raise CleanupRefusedError(f"{label}: protected set changed")
        return False
    handled = _axis_gate()
    if scope.dirty:
        if _current_protection_digest(data_dir, vector_table, candidates, immutable=False) != protected_digest:
            raise CleanupRefusedError(f"{label}: protected set changed")
        scope.dirty = False
    return handled


def _registry_resume_precheck(data_dir: Path, vector_table: str, candidates: Any) -> None:
    """Receipt-bound target absence만 허용하고 모든 후보 ownership을 첫 vector write 전에 대조한다."""
    scope = _SCOPE
    for _attempt in range(3):
        if scope is not None and scope.witness is not None and _change_pending():
            _handle_change()  # 위치 색인을 다시 만들고 전체 snapshot 일치 표시를 비운다
        if scope is not None and scope.full_snapshot_verified:
            # 첫 apply의 전체 snapshot 일치(더 엄격한 조건)가 같은 상태에서 이미 확인됐다.
            return
        for record in candidates:
            current = _snapshot_now(data_dir, vector_table, record["id"])
            done = _done_axes(current)
            current["edges"] = record["snapshot"]["edges"]
            if not _allowed(current, record["snapshot"], done):
                raise CleanupRefusedError(f"{record['id']}: registry resume precheck conflict")
        if scope is None or scope.witness is None or not _change_pending():
            return
    raise CleanupRefusedError("database set changed during the resume precheck")


def _registry_report(data_dir: Path, candidate_ids: set[str], *, immutable: bool = True) -> dict[str, Any]:
    """registry는 읽기 전용으로 before와 candidate graph target 뒤 orphan을 분리한다. 행은 스트리밍으로 읽는다."""
    conn = _ro(data_dir / "opencrab.db", immutable)
    graph = _ro(data_dir / "graph.db", immutable)
    try:
        graph_ids = {row[0] for row in graph.execute("SELECT node_id FROM graph_nodes")}
        selected: list[list[Any]] = []
        total_rows = 0
        for row in conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY space,node_id"):
            total_rows += 1
            if row[3] in candidate_ids:
                selected.append([row[1], row[2], row[3]])
        candidate_edges: list[list[Any]] = []
        before_orphans: list[list[Any]] = []
        expected_orphans: list[list[Any]] = []
        for edge in conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges ORDER BY id"):
            if edge[1] not in graph_ids or edge[4] not in graph_ids:
                before_orphans.append(list(edge))
            if edge[1] not in graph_ids or edge[1] in candidate_ids or edge[4] not in graph_ids or edge[4] in candidate_ids:
                expected_orphans.append(list(edge))
            if edge[1] in candidate_ids or edge[4] in candidate_ids:
                candidate_edges.append(list(edge))
        return {
            "candidate_rows": selected,
            "candidate_edges": candidate_edges,
            "preexisting_orphans": before_orphans,
            "expected_after_orphans": expected_orphans,
            "candidate_count": len(selected),
            "total_rows": total_rows,
            "mode": "report-only",
        }
    finally:
        graph.close()
        conn.close()


def select(data_dir: Path, vector_table: str, store: _CandidateStore | None = None, evidence_store: _EvidenceStore | None = None) -> tuple[Any, dict[str, Any]]:
    """후보를 고르고 보호 집합을 만든다. store를 주면 후보를 파일에 이어 쓰고 그 저장소를 돌려준다."""
    require_empty_wal(data_dir)
    candidates: Any = store if store is not None else []
    errors: list[str] = []
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        approved = _SELECTION.ids
        if approved is None:
            ids = {row[0] for row in doc.execute("SELECT source_id FROM doc_sources WHERE source_id LIKE 'evidence:turn:%'")}
        else:
            ids = set(approved)
        readers = _Readers(data_dir, vector_table, immutable=True, ids=ids)
        try:
            if approved is None:
                for row in doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id LIKE 'evidence:turn:%' ORDER BY source_id"):
                    try:
                        if _strict_pack(row[2], f"source {row[0]}") == _SELECTION.pack_id and isinstance(row[1], str) and row[1].startswith(PREFIXES):
                            candidates.append(_select_candidate(data_dir, vector_table, row, readers))
                    except CleanupRefusedError as exc:
                        errors.append(str(exc))
            else:
                # 승인된 ID는 모두 후보가 되거나 오류가 된다. 조용히 빠지는 ID는 없다.
                for node_id in sorted(approved):
                    row = doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (node_id,)).fetchone()
                    try:
                        if row is None:
                            raise CleanupRefusedError(f"{node_id}: approved id has no source row")
                        candidates.append(_select_candidate(data_dir, vector_table, row, readers))
                    except CleanupRefusedError as exc:
                        errors.append(str(exc))
        finally:
            readers.close()
    finally:
        doc.close()
    if store is not None:
        store.finish()
    candidate_ids = set(store.ids) if store is not None else {candidate["id"] for candidate in candidates}
    protected = _protected_detail(data_dir, vector_table, candidates, candidate_ids, evidence_store)
    provisional_manifest = {"protected": protected}
    last_claims = _claim_guard(data_dir, candidates, provisional_manifest)
    registry_claims = _registry_claim_guard(data_dir, candidates, provisional_manifest)
    if last_claims:
        errors.append("candidate set removes final Claim support: " + ", ".join(last_claims))
    if registry_claims:
        errors.append("candidate set removes final registry Claim support: " + ", ".join(registry_claims))
    report = {"errors": errors, "registry": _registry_report(data_dir, candidate_ids), "last_support_claims": last_claims, "last_registry_claims": registry_claims}
    return candidates, {"protected": protected, "report": report}


def _ordered_digest(candidates: Any) -> str:
    ids = candidates.ids if isinstance(candidates, _CandidateStore) else [record["id"] for record in candidates]
    return _sha(_canon(ids))


def dry_run(data_dir: Path, run_dir: Path, vector_table: str, approved_ids: Path | None = None) -> dict[str, Any]:
    """후보와 보호 집합을 기록한다. 오류가 있으면 manifest를 만들지 않는다.

    approved_ids가 없으면 #493 접두 규칙으로 고른다. 있으면 그 파일의 ID 집합만 후보로 삼는다.
    승인 파일은 run directory를 만들기 전에 검사한다.
    """
    selection = _PREFIX_SELECTION if approved_ids is None else _load_approved_ids(approved_ids)
    with _selection_scope(selection):
        return _dry_run(data_dir, run_dir, vector_table)


def _dry_run(data_dir: Path, run_dir: Path, vector_table: str) -> dict[str, Any]:
    if any((run_dir / name).exists() for name in ("candidates.jsonl", "manifest.json", "receipt.json")):
        raise CleanupRefusedError("run directory already has published data")
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    store = _CandidateStore.create(run_dir / "candidates.jsonl")
    evidence_store = _EvidenceStore.create(run_dir / "evidence.jsonl")
    try:
        candidates, detail = select(data_dir, vector_table, store, evidence_store)
        protected_digest = _protection_digest(detail["protected"]["sections"])
        frozen = {**detail["protected"], "evidence": {"file": "evidence.jsonl", "count": len(evidence_store), "sha256": evidence_store.sha256}}
        manifest = {
            "candidate_sha256": store.sha256,
            "ordered_ids_sha256": _ordered_digest(candidates),
            "schema_sha256": _schema_digest(data_dir, vector_table, immutable=True),
            "snapshot_sha256": _store_set_digest(data_dir),
            "store_set_sha256": _store_set_digest(data_dir),
            "vector_table": vector_table,
            "selection": _selection_record(_SELECTION),
            "protected": frozen,
            "protected_sha256": protected_digest,
        }
        _publish(run_dir / "report.json", _canon({**detail["report"], "protected_sha256": protected_digest}))
        if detail["report"]["errors"]:
            return {"eligible": False, "candidates": len(candidates), "errors": detail["report"]["errors"]}
        _publish(run_dir / "manifest.json", _canon(manifest))
        return {"eligible": True, "candidates": len(candidates), "manifest": manifest}
    finally:
        store.close()
        evidence_store.close()


def _load_run(run_dir: Path) -> tuple[_CandidateStore, dict[str, Any]]:
    candidates_path, manifest_path = run_dir / "candidates.jsonl", run_dir / "manifest.json"
    if not candidates_path.is_file() or not manifest_path.is_file():
        raise CleanupRefusedError("run has no applyable manifest")
    manifest = _Manifest(_json_object(manifest_path.read_text(encoding="utf-8"), "manifest"))
    manifest.run_dir = run_dir
    candidates = _CandidateStore.load(candidates_path)
    if manifest.get("candidate_sha256") != candidates.sha256 or manifest.get("ordered_ids_sha256") != _ordered_digest(candidates):
        candidates.close()
        raise CleanupRefusedError("candidate file does not match manifest")
    try:
        manifest.selection = _selection_from_manifest(manifest, candidates.ids)
        if isinstance((manifest.get("protected") or {}).get("evidence"), dict):
            manifest.evidence_store()
    except CleanupRefusedError:
        candidates.close()
        raise
    return candidates, manifest


def _load_approval(path: Path | None, manifest: dict[str, Any]) -> None:
    if path is None or not path.is_file():
        raise CleanupRefusedError("--approved-manifest is required for apply")
    approval = _json_object(path.read_text(encoding="utf-8"), "approval")
    if set(approval) != {"manifest_sha256", "ordered_ids_sha256"}:
        raise CleanupRefusedError("approval has unexpected or missing fields")
    if approval["manifest_sha256"] != _manifest_sha(manifest) or approval["ordered_ids_sha256"] != manifest["ordered_ids_sha256"]:
        raise CleanupRefusedError("approval does not bind this exact manifest")


def _component_intents(candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    if _scope_for(candidates, manifest) is not None:
        return _once(("intents",), lambda: _build_component_intents(candidates, manifest))
    return _build_component_intents(candidates, manifest)


def _build_component_intents(candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    """Initial receipt에 넣을 immutable component, boundary intent를 만든다."""
    ids = {record["id"] for record in candidates}
    component: dict[str, dict[str, Any]] = {}
    boundary: dict[str, dict[str, Any]] = {}
    for record in candidates:
        for edge in record["edges"]:
            key = _sha(_canon(edge))
            if edge["from_id"] in ids and edge["to_id"] in ids:
                component[key] = {"edge": edge, "endpoints": sorted((edge["from_id"], edge["to_id"])), "approved_absent": True}
            else:
                boundary[key] = {"edge": edge, "candidate": record["id"], "deletion_target": True}
    body = {
        "manifest": _manifest_sha(manifest),
        "component": list(component.values()),
        "boundary": list(boundary.values()),
        "component_ids": sorted(component),
        "component_count": len(component),
    }
    canonical = _canon(body).decode("utf-8")
    return {**body, "canonical": canonical, "digest": _sha(canonical)}


def _strict_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(_strict_equal(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_strict_equal(item, value) for item, value in zip(actual, expected))
    return actual == expected


def _validate_intent(receipt: dict[str, Any], candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    if _scope_for(candidates, manifest) is not None:
        return _once(("intent_valid", id(receipt)), lambda: (receipt, _validate_intent_uncached(receipt, candidates, manifest)))[1]
    return _validate_intent_uncached(receipt, candidates, manifest)


def _validate_intent_uncached(receipt: dict[str, Any], candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    intent = receipt.get("component_intent")
    expected = _component_intents(candidates, manifest)
    if not isinstance(intent, dict) or not _strict_equal(intent, expected):
        raise CleanupRefusedError("receipt component intent mismatch")
    if intent["digest"] != _sha(intent["canonical"]):
        raise CleanupRefusedError("receipt component intent digest mismatch")
    return intent


def _backup_receipt(data_dir: Path, run_dir: Path, backup_dir: Path, manifest: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if backup_dir.exists() or run_dir in backup_dir.parents or backup_dir in run_dir.parents:
        raise CleanupRefusedError("backup directory must be new and outside run directory")
    backup_dir.mkdir(mode=0o700, parents=True)
    files: dict[str, Any] = {}
    try:
        for name in DB_FILES:
            target = backup_dir / name
            backup_sqlite(data_dir / name, target, deadline=time.monotonic() + 60)
            os.chmod(target, 0o600)
            verify_sqlite(target)
            _fsync_file(target)
            files[name] = {"sha256": _file_sha(target), "mode": target.stat().st_mode & 0o777}
            if _SCOPE is not None:
                _SCOPE.memo[("backup_verified", files[name]["sha256"])] = True
        _fsync_dir(backup_dir)
        _fsync_dir(backup_dir.parent)
    except Exception:
        raise
    receipt = {
        "backup_set_id": _sha(_canon(files)),
        "files": files,
        "manifest_sha256": _manifest_sha(manifest),
        "store_set_sha256": manifest["store_set_sha256"],
        "data_dir": _data_dir_identity(data_dir),
        "component_intent": _component_intents(candidates, manifest),
    }
    _publish(run_dir / "receipt.json", _canon(receipt))
    return receipt


def _load_receipt(run_dir: Path, backup_dir: Path, manifest: dict[str, Any], candidates: list[dict[str, Any]], data_dir: Path | None = None) -> dict[str, Any]:
    path = run_dir / "receipt.json"
    if not path.is_file():
        raise CleanupRefusedError("verified four-store backup receipt is required")
    receipt = _json_object(path.read_text(encoding="utf-8"), "receipt")
    if receipt.get("manifest_sha256") != _manifest_sha(manifest) or receipt.get("store_set_sha256") != manifest.get("store_set_sha256"):
        raise CleanupRefusedError("receipt does not bind manifest and store set")
    if data_dir is not None and receipt.get("data_dir") != _data_dir_identity(data_dir):
        raise CleanupRefusedError("receipt does not bind this data directory")
    _validate_intent(receipt, candidates, manifest)
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != set(DB_FILES):
        raise CleanupRefusedError("receipt does not hold four backups")
    for name in DB_FILES:
        copy = backup_dir / name
        if not copy.is_file():
            raise CleanupRefusedError(f"backup receipt mismatch: {name}")
        digest = _file_sha(copy)
        if digest != files[name].get("sha256") or (copy.stat().st_mode & 0o777) != 0o600:
            raise CleanupRefusedError(f"backup receipt mismatch: {name}")
        # hash는 호출마다 다시 계산한다. integrity_check는 내용(sha256)이 같으면 결과도 같으므로 같은 프로세스에서 한 번만 돈다.
        verified = ("backup_verified", digest)
        if _SCOPE is not None and verified in _SCOPE.memo:
            continue
        verify_sqlite(copy)
        if _SCOPE is not None:
            _SCOPE.memo[verified] = True
    return receipt


def _marker_payload(kind: str, receipt: dict[str, Any]) -> dict[str, Any]:
    base = {
        "kind": kind,
        "manifest_sha256": receipt["manifest_sha256"],
        "backup_set_id": receipt["backup_set_id"],
        "store_set_sha256": receipt["store_set_sha256"],
        "data_dir": receipt["data_dir"],
        "receipt_sha256": _sha(_canon(receipt)),
    }
    return {**base, "payload_sha256": _sha(_canon(base))}


def _read_marker(path: Path, kind: str) -> dict[str, Any]:
    data = _json_object(path.read_text(encoding="utf-8"), path.name)
    required = {"kind", "manifest_sha256", "backup_set_id", "store_set_sha256", "data_dir", "receipt_sha256", "payload_sha256"}
    if kind == "attempt-published":
        required |= {"attempt", "directory"}
    proof = {key: value for key, value in data.items() if key != "payload_sha256"}
    if set(data) != required or data.get("kind") != kind or data.get("payload_sha256") != _sha(_canon(proof)):
        raise CleanupRefusedError(f"{path.name} digest is invalid")
    return data


def marker_state(run_dir: Path) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    temporary = list(run_dir.glob(".rollback-*.tmp"))
    tomb, complete = run_dir / MARKERS[0], run_dir / MARKERS[1]
    tomb_exists, complete_exists = os.path.lexists(tomb), os.path.lexists(complete)
    if temporary:
        return "incomplete artifact", None, None
    if not tomb_exists and not complete_exists:
        return "absent", None, None
    try:
        tomb_data = _read_marker(tomb, "tombstone") if tomb_exists else None
        complete_data = _read_marker(complete, "completion") if complete_exists else None
    except (CleanupRefusedError, OSError):
        return "incomplete artifact", None, None
    if complete_data is not None and tomb_data is None:
        return "incomplete artifact", None, None
    if tomb_data is None:
        return "incomplete artifact", None, None
    keys = ("manifest_sha256", "backup_set_id", "store_set_sha256", "data_dir", "receipt_sha256")
    if complete_data is not None and any(tomb_data[key] != complete_data[key] for key in keys):
        return "incomplete artifact", None, None
    return ("complete tombstone plus completion" if complete_data else "complete tombstone"), tomb_data, complete_data


def _rename_noreplace(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise CleanupRefusedError("Linux renameat2(RENAME_NOREPLACE) is required") from exc
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(target), RENAME_NOREPLACE)
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def publish_marker(run_dir: Path, filename: str, payload: dict[str, Any]) -> None:
    """marker는 임시 O_EXCL 생성과 renameat2 no-replace로만 최종 발행한다."""
    final = run_dir / filename
    temporary = run_dir / f".{filename}.tmp"
    if final.exists():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(final))
    data = _canon(payload)
    _write_fsync(temporary, data, exclusive=True)
    try:
        _rename_noreplace(temporary, final)
    except Exception:
        raise
    _fsync_dir(run_dir)
    if final.read_bytes() != data:
        raise CleanupRefusedError(f"{filename} readback failed")
    _read_marker(final, payload["kind"])


def _allowed(current: dict[str, Any], recorded: dict[str, Any], axes_done: set[str], *, shared_edges_absent: bool = False) -> bool:
    targets = {"vector": [], "graph": [], "edges": _edge_digest([]), "source": [], "fts": [], "doc": [], "registry_node": [], "registry_edges": []}
    for key, value in current.items():
        if value == recorded.get(key):
            continue
        if key == "edges" and shared_edges_absent and value == targets[key]:
            continue
        if key in {"registry_node", "registry_edges"} and all(item in recorded.get(key, []) for item in value):
            continue
        if key in axes_done and value == targets[key]:
            continue
        return False
    return True


def _full_snapshot_matches(data_dir: Path, vector_table: str, candidates: list[dict[str, Any]], protected_digest: str) -> bool:
    """첫 apply는 frozen 원본 전체와 정확히 같을 때만 backup을 만든다."""
    if not _verify_protection(data_dir, vector_table, candidates, protected_digest):
        return False
    matched = all(_snapshot_now(data_dir, vector_table, record["id"]) == record["snapshot"] for record in candidates)
    if matched and _SCOPE is not None:
        _SCOPE.full_snapshot_verified = True
    return matched


def _candidate_id_set(candidates: Any, manifest: dict[str, Any]) -> set[str]:
    if _scope_for(candidates, manifest) is not None:
        return _once(("ids",), lambda: _ids_of(candidates))
    return _ids_of(candidates)


def _component_edges(candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Frozen candidates와 immutable receipt에서 authoritative R/D를 재계산한다."""
    if _scope_for(candidates, manifest) is not None:
        return _once(("component_edges", id(receipt)), lambda: (receipt, _component_edges_uncached(candidates, receipt, manifest)))[1]
    return _component_edges_uncached(candidates, receipt, manifest)


def _component_edges_uncached(candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    intent = _validate_intent(receipt, candidates, manifest)
    ids = _candidate_id_set(candidates, manifest)
    recorded = {
        _sha(_canon(edge)): edge
        for record in candidates
        for edge in record["edges"]
        if edge["from_id"] in ids and edge["to_id"] in ids
    }
    approved = {_sha(_canon(item["edge"])) for item in intent["component"]}
    return recorded, approved


def _scoped_keys(recorded: dict[str, dict[str, Any]], scope_id: str) -> set[str]:
    """scope 후보에 닿는 recorded edge key만 고른다. apply 중에는 후보별 색인을 한 번 만든다."""
    def build() -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        by_node: dict[str, set[str]] = {}
        for key, edge in recorded.items():
            by_node.setdefault(edge["from_id"], set()).add(key)
            by_node.setdefault(edge["to_id"], set()).add(key)
        return recorded, by_node

    if _SCOPE is None:
        return {key for key, edge in recorded.items() if scope_id in (edge["from_id"], edge["to_id"])}
    return set(_once(("keys_by_node", id(recorded)), build)[1].get(scope_id, ()))


def _forget_receipts(keep: int | None) -> None:
    """이전에 적재한 영수증의 검증 메모를 놓는다. 영수증 본문은 후보 수에 비례하므로 한 번에 하나만 쥔다."""
    scope = _SCOPE
    if scope is None:
        return
    for key in [k for k in scope.memo if isinstance(k, tuple) and k and k[0] in {"intent_valid", "component_edges", "boundary_edges"} and k[1] != keep]:
        del scope.memo[key]


def _release_intent(receipt: dict[str, Any], candidates: Any, manifest: dict[str, Any]) -> None:
    """파생값(approved key, 후보별 edge 색인)을 만든 뒤 영수증 사본의 큰 본문과 이전 영수증 메모를 놓는다.

    영수증 파일과 형식은 그대로이다. 이 사본은 apply 안에서만 쓰이고 marker 함수는 apply에서 호출되지 않는다.
    """
    scope = _scope_for(candidates, manifest)
    if scope is None:
        return
    _component_edges(candidates, receipt, manifest)
    _boundary_edges(candidates, receipt, manifest)
    keep = id(receipt)
    _forget_receipts(keep)
    scope.memo.pop(("intents",), None)
    intent = receipt.get("component_intent")
    if isinstance(intent, dict):
        receipt["component_intent"] = {"digest": intent["digest"]}
        scope.memo[("intent_valid", keep)] = (receipt, receipt["component_intent"])


def _validate_component_edges(conn: sqlite3.Connection, candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any], scope_id: str | None = None) -> list[dict[str, Any]]:
    """같은 R/D/S helper를 caller와 graph transaction에서 사용한다.

    scope_id가 있으면 그 후보에 닿는 edge만 대조한다. R/D/S 조건은 edge 단위로 성립하므로
    후보별 부분 집합의 합이 전체 조건과 같다.
    """
    recorded, approved = _component_edges(candidates, receipt, manifest)
    ids = _candidate_id_set(candidates, manifest)
    if scope_id is None:
        actual_rows = [_edge_record(row) for row in conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges") if row[1] in ids and row[4] in ids]
        actual_rows.sort(key=lambda edge: (edge["from_id"], edge["relation"], edge["to_id"]))
    else:
        keys = _scoped_keys(recorded, scope_id)
        recorded = {key: recorded[key] for key in keys}
        approved = approved & keys
        actual_rows = [
            edge
            for edge in (
                _edge_record(row)
                for row in conn.execute(
                    "SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? OR to_id=? ORDER BY from_id,relation,to_id",
                    (scope_id, scope_id),
                )
            )
            if edge["from_id"] in ids and edge["to_id"] in ids
        ]
    actual = {_sha(_canon(edge)): edge for edge in actual_rows}
    if not set(recorded) - approved <= set(actual) <= set(recorded):
        raise CleanupRefusedError("component edge R/D/S conflict")
    return actual_rows


def _boundary_edges(candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Frozen boundary rows와 immutable deletion target을 반환한다."""
    if _scope_for(candidates, manifest) is not None:
        return _once(("boundary_edges", id(receipt)), lambda: (receipt, _boundary_edges_uncached(candidates, receipt, manifest)))[1]
    return _boundary_edges_uncached(candidates, receipt, manifest)


def _boundary_edges_uncached(candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    intent = _validate_intent(receipt, candidates, manifest)
    recorded = {_sha(_canon(item["edge"])): item["edge"] for item in intent["boundary"]}
    approved = {_sha(_canon(item["edge"])) for item in intent["boundary"] if item["deletion_target"] is True}
    return recorded, approved


def _validate_boundary_edges(conn: sqlite3.Connection, candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any], scope_id: str | None = None) -> list[dict[str, Any]]:
    """Candidate boundary rows의 exact identity와 approved absence를 확인한다."""
    recorded, approved = _boundary_edges(candidates, receipt, manifest)
    ids = _candidate_id_set(candidates, manifest)
    if scope_id is None:
        actual_rows = [_edge_record(row) for row in conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges") if (row[1] in ids) != (row[4] in ids)]
        actual_rows.sort(key=lambda edge: (edge["from_id"], edge["relation"], edge["to_id"]))
    else:
        keys = _scoped_keys(recorded, scope_id)
        recorded = {key: recorded[key] for key in keys}
        approved = approved & keys
        actual_rows = [
            edge
            for edge in (
                _edge_record(row)
                for row in conn.execute(
                    "SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? OR to_id=? ORDER BY from_id,relation,to_id",
                    (scope_id, scope_id),
                )
            )
            if (edge["from_id"] in ids) != (edge["to_id"] in ids)
        ]
    actual = {_sha(_canon(edge)): edge for edge in actual_rows}
    if not set(recorded) - approved <= set(actual) <= set(recorded):
        raise CleanupRefusedError("boundary edge R/D/S conflict")
    return actual_rows


def _validate_graph_intent(conn: sqlite3.Connection, candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any], scope_id: str | None = None) -> None:
    """Component와 boundary의 immutable graph intent를 함께 확인한다."""
    _validate_component_edges(conn, candidates, receipt, manifest, scope_id)
    _validate_boundary_edges(conn, candidates, receipt, manifest, scope_id)


def _validate_projected_protection(data_dir: Path, conn: sqlite3.Connection, candidates: list[dict[str, Any]], manifest: dict[str, Any], planned_edges: set[str], planned_nodes: set[str]) -> None:
    """Planned graph write 전 protected projection과 affected Claim support를 검사한다."""
    scope = _scope_for(candidates, manifest)
    key = ("projected", scope.site) if scope is not None else None
    if scope is not None and key in scope.passed:
        # witness가 이 도구 밖의 commit이 없음을 확인하므로 같은 호출 자리의 통과 결과를 재사용한다.
        return
    expected = manifest.get("protected")
    if not isinstance(expected, dict):
        raise CleanupRefusedError("manifest has no protected graph state")
    candidate_ids = _candidate_id_set(candidates, manifest)
    sections = expected.get("sections")
    if not isinstance(sections, dict):
        raise CleanupRefusedError("manifest has no protected graph state")
    current, _ = _graph_sections(conn, candidate_ids, raw=False, excluded_nodes=planned_nodes)
    if any(current[name] != sections.get(name) for name in ("nodes", "evidence_graph", "edges")):
        raise CleanupRefusedError("projected protected graph state changed")
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        if _doc_sections(doc, expected.get("evidence_ids", []), doc_nodes=False)["evidence_source"] != sections.get("evidence_source"):
            raise CleanupRefusedError("projected protected source state changed")
    finally:
        doc.close()
    registry = _ro(data_dir / "opencrab.db", True)
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        if _claim_failures(conn, registry, doc, candidates, manifest, axis="graph", planned_edges=planned_edges, planned_nodes=planned_nodes):
            raise CleanupRefusedError("planned graph delete removes final Claim support")
    finally:
        doc.close()
        registry.close()
    if scope is not None:
        scope.passed.add(key)


def _validate_all_candidate_claims(data_dir: Path, candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """모든 candidate graph target의 Claim support 유실을 첫 axis write 전에 막는다."""
    if _claim_guard(data_dir, candidates, manifest):
        raise CleanupRefusedError("Claim support changed after frozen manifest")
    if _registry_claim_guard(data_dir, candidates, manifest):
        raise CleanupRefusedError("registry Claim support changed after frozen manifest")
    conn = _ro(data_dir / "graph.db", True)
    try:
        planned_edges = {
            _sha(_canon(edge))
            for record in candidates
            for edge in record["edges"]
        }
        planned_nodes = {record["id"] for record in candidates}
        scope = _scope_for(candidates, manifest)
        if scope is not None:
            scope.site = "preflight"
        try:
            _validate_projected_protection(data_dir, conn, candidates, manifest, planned_edges, planned_nodes)
        finally:
            if scope is not None:
                scope.site = "record"
    finally:
        conn.close()


def _current_protection_digest(data_dir: Path, vector_table: str, candidates: Any, *, immutable: bool = True) -> str:
    return _protection_digest(_protection_sections(data_dir, vector_table, _ids_of(candidates), immutable=immutable)[0])


def _delete_vector(data_dir: Path, vector_table: str, record: dict[str, Any]) -> None:
    conn = _writer(data_dir, "vectors.db")
    try:
        _load_vec(conn)
        columns = _ensure_vector_table(conn, vector_table)
        selected = ["node_id", "pack_id", "document", "metadata", "embedding"]
        if "embedding_bit" in columns:
            selected.append("embedding_bit")
        _begin_write(conn)
        row = conn.execute(f"SELECT {','.join(selected)} FROM {vector_table} WHERE node_id=?", (record["id"],)).fetchone()
        if row is None or _row_digest(row) != record["vector"]["digest"]:
            raise CleanupRefusedError(f"{record['id']}: vector CAS conflict")
        deleted = conn.execute(f"DELETE FROM {vector_table} WHERE node_id=?", (record["id"],)).rowcount
        if deleted != 1:
            raise CleanupRefusedError(f"{record['id']}: vector delete rowcount")
        _commit(conn)
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _delete_graph(data_dir: Path, record: dict[str, Any], candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any], after_commit: Callable[[], None] | None = None) -> None:
    conn = _writer(data_dir, "graph.db")
    try:
        _begin_write(conn)
        node = conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (record["id"],)).fetchone()
        if node is None or node[0] != _SELECTION.node_type or node[2] != EVIDENCE_SPACE or _props_digest(node[3]) != record["graph"]["properties"]:
            raise CleanupRefusedError(f"{record['id']}: graph CAS conflict")
        component_before = {_sha(_canon(edge)) for edge in _validate_component_edges(conn, candidates, receipt, manifest, record["id"])}
        boundary_before = {_sha(_canon(edge)) for edge in _validate_boundary_edges(conn, candidates, receipt, manifest, record["id"])}
        _, component_targets = _component_edges(candidates, receipt, manifest)
        _, boundary_targets = _boundary_edges(candidates, receipt, manifest)
        deleted_component: set[str] = set()
        deleted_boundary: set[str] = set()
        planned: list[tuple[dict[str, Any], Any]] = []
        for edge in record["edges"]:
            key = _sha(_canon(edge))
            if key in component_targets:
                deleted_component.add(key)
            elif key in boundary_targets:
                deleted_boundary.add(key)
            else:
                raise CleanupRefusedError(f"{record['id']}: graph edge is not an immutable deletion target")
            row = conn.execute("SELECT properties FROM graph_edges WHERE from_id=? AND relation=? AND to_id=?", (edge["from_id"], edge["relation"], edge["to_id"])).fetchone()
            if row is None:
                continue
            if _props_digest(row[0]) != edge["properties"]:
                raise CleanupRefusedError(f"{record['id']}: graph edge CAS conflict")
            planned.append((edge, row[0]))
        component_lower = component_before - component_targets
        boundary_lower = boundary_before - boundary_targets
        component_projected = component_before - deleted_component
        boundary_projected = boundary_before - deleted_boundary
        if not (component_lower <= component_projected <= component_before) or not (boundary_lower <= boundary_projected <= boundary_before):
            raise CleanupRefusedError(f"{record['id']}: graph planned post-state conflict")
        _validate_projected_protection(data_dir, conn, candidates, manifest, deleted_component | deleted_boundary, {record["id"]})
        for edge, raw in planned:
            result = conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation=? AND to_id=? AND from_type=? AND to_type=? AND properties IS ?", (edge["from_id"], edge["relation"], edge["to_id"], edge["from_type"], edge["to_type"], raw)).rowcount
            if result != 1:
                raise CleanupRefusedError(f"{record['id']}: graph edge delete rowcount")
        deleted = conn.execute("DELETE FROM graph_nodes WHERE node_id=? AND node_type=? AND space_id=? AND properties IS ?", (record["id"], _SELECTION.node_type, EVIDENCE_SPACE, node[3])).rowcount
        if deleted != 1:
            raise CleanupRefusedError(f"{record['id']}: graph delete rowcount")
        component_after = {_sha(_canon(edge)) for edge in _validate_component_edges(conn, candidates, receipt, manifest, record["id"])}
        boundary_after = {_sha(_canon(edge)) for edge in _validate_boundary_edges(conn, candidates, receipt, manifest, record["id"])}
        if component_after != component_before - deleted_component or boundary_after != boundary_before - deleted_boundary:
            raise CleanupRefusedError(f"{record['id']}: graph planned post-state conflict")
        _commit(conn)
        if after_commit is not None:
            after_commit()
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _edge_raw_for_digest(conn: sqlite3.Connection, edge: dict[str, Any]) -> Any:
    row = conn.execute("SELECT properties FROM graph_edges WHERE from_id=? AND relation=? AND to_id=?", (edge["from_id"], edge["relation"], edge["to_id"])).fetchone()
    if row is None or _props_digest(row[0]) != edge["properties"]:
        raise CleanupRefusedError("graph edge properties changed")
    return row[0]


def _registry_rowcount(cursor: sqlite3.Cursor) -> int:
    return cursor.rowcount


def _registry_delete(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> sqlite3.Cursor:
    """Registry DELETE의 SQL과 bound parameter를 하나의 관측 seam으로 모은다."""
    return conn.execute(sql, params)


def _registry_targets_match(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], snapshot: dict[str, Any]) -> bool:
    """Receipt resume에서 승인 target 부재만 허용하는 identity seam이다."""
    return (
        all(node in snapshot["registry_node"] for node in nodes)
        and all(edge in snapshot["registry_edges"] for edge in edges)
    )


def _registry_global_checks(conn: sqlite3.Connection, data_dir: Path, candidates: Any, manifest: dict[str, Any]) -> None:
    """후보 키 밖 registry 보호 상태와 Claim support를 한 transaction 안에서 대조한다."""
    protected = manifest.get("protected")
    if not isinstance(protected, dict) or not isinstance(protected.get("sections"), dict):
        raise CleanupRefusedError("manifest has no protected registry state")
    sections = protected["sections"]
    current, _ = _registry_sections(conn, _candidate_id_set(candidates, manifest))
    if current["registry_nodes"] != sections.get("registry_nodes") or current["registry_edges"] != sections.get("registry_edges"):
        raise CleanupRefusedError("registry protection conflict")
    graph = _ro(data_dir / "graph.db", True)
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        if _claim_failures(graph, conn, doc, candidates, manifest, axis="registry"):
            raise CleanupRefusedError("registry Claim support changed after frozen manifest")
    finally:
        doc.close()
        graph.close()
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        if _doc_sections(doc, protected.get("evidence_ids", []), doc_nodes=False)["evidence_source"] != sections.get("evidence_source"):
            raise CleanupRefusedError("registry protection conflict")
    finally:
        doc.close()


def _delete_registry(data_dir: Path, record: dict[str, Any], candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    conn = _writer(data_dir, "opencrab.db")
    try:
        _begin_write(conn)
        scope = _scope_for(candidates, manifest)
        if scope is None or ("registry-global",) not in scope.passed:
            # 후보 키 밖 상태는 witness가 이 도구 밖의 commit을 감시하는 동안 바뀌지 않는다. 첫 후보에서만 대조한다.
            _registry_global_checks(conn, data_dir, candidates, manifest)
            if scope is not None:
                scope.passed.add(("registry-global",))
        snapshot = record["snapshot"]
        nodes, all_edges = _registry_incident_rows(conn, record["id"])
        edges = []
        foreign = []
        for row in all_edges:
            edge = _registry_edge_record(row)
            if (EVIDENCE_SPACE, record["id"]) in ((edge["from_space"], edge["from_id"]), (edge["to_space"], edge["to_id"])):
                edges.append(edge)
            else:
                foreign.append(edge)
        current_nodes = [_registry_node_record(row) for row in nodes]
        if not _registry_targets_match(current_nodes, edges, snapshot):
            raise CleanupRefusedError(f"{record['id']}: registry target membership conflict")
        if foreign:
            raise CleanupRefusedError(f"{record['id']}: registry edge foreign tuple")
        if foreign != snapshot["registry_foreign"]:
            raise CleanupRefusedError(f"{record['id']}: registry edge CAS conflict")
        for edge in edges:
            cursor = _registry_delete(
                conn,
                "DELETE FROM ontology_edges WHERE id=? AND from_space=? AND from_id=? AND relation=? "
                "AND to_space=? AND to_id=? AND created_at IS ?",
                (edge["id"], edge["from_space"], edge["from_id"], edge["relation"], edge["to_space"], edge["to_id"], edge["created_at"]),
            )
            if _registry_rowcount(cursor) != 1:
                raise CleanupRefusedError(f"{record['id']}: registry edge delete rowcount")
        for node in [_registry_node_record(row) for row in nodes]:
            cursor = _registry_delete(
                conn,
                "DELETE FROM ontology_nodes WHERE id=? AND space=? AND node_type=? AND node_id=? "
                "AND created_at IS ? AND updated_at IS ?",
                (node["id"], node["space"], node["node_type"], node["node_id"], node["created_at"], node["updated_at"]),
            )
            if _registry_rowcount(cursor) != 1:
                raise CleanupRefusedError(f"{record['id']}: registry node delete rowcount")
        _commit(conn)
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _registry_incident_rows(conn: sqlite3.Connection, node_id: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """후보 id의 registry node와 incident edge를 id 순서로 읽는다. apply 중에는 위치 색인의 PK 조회를 쓴다."""
    scope = _SCOPE
    if scope is not None and scope.readers is not None:
        nodes = []
        for row_id in scope.readers.reg_nodes.get(node_id, ()):
            row = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE id=?", (row_id,)).fetchone()
            if row is not None and row[3] == node_id:
                nodes.append(row)
        edges = []
        for row_id in scope.readers.reg_edges.get(node_id, ()):
            row = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE id=?", (row_id,)).fetchone()
            if row is not None and node_id in (row[2], row[5]):
                edges.append(row)
        return nodes, edges
    nodes = conn.execute(
        "SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=? ORDER BY id",
        (node_id,),
    ).fetchall()
    edges = conn.execute(
        "SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges "
        "WHERE from_id=? OR to_id=? ORDER BY id",
        (node_id, node_id),
    ).fetchall()
    return nodes, edges


def _fts_rowids(conn: sqlite3.Connection, node_id: str) -> list[int]:
    """source_id의 FTS rowid를 구한다. apply 중에는 위치 색인을, 아니면 한 번의 스캔을 쓴다."""
    scope = _SCOPE
    if scope is not None and scope.readers is not None:
        return list(scope.readers.fts.get(node_id, ()))
    return [rowid for rowid, source_id in conn.execute("SELECT rowid,source_id FROM doc_sources_fts") if source_id == node_id]


def _delete_document(data_dir: Path, record: dict[str, Any]) -> None:
    conn = _writer(data_dir, "doc_store.db")
    try:
        _begin_write(conn)
        source = conn.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (record["id"],)).fetchone()
        fts = []
        for rowid in _fts_rowids(conn, record["id"]):
            row = conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE rowid=?", (rowid,)).fetchone()
            if row is not None and row[0] == record["id"]:
                fts.append((rowid, row))
        doc = conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (record["id"],)).fetchone()
        if source is not None and _row_digest(source) != record["source"]:
            raise CleanupRefusedError(f"{record['id']}: source CAS conflict")
        if fts and (len(fts) != 1 or _row_digest(fts[0][1]) != record["snapshot"]["fts"][0]):
            raise CleanupRefusedError(f"{record['id']}: FTS CAS conflict")
        if doc is not None and (doc[0] != EVIDENCE_SPACE or doc[2] != _SELECTION.node_type or _props_digest(doc[3]) != record["doc"]["properties"]):
            raise CleanupRefusedError(f"{record['id']}: doc CAS conflict")
        if source is not None and conn.execute("DELETE FROM doc_sources WHERE source_id=?", (record["id"],)).rowcount != 1:
            raise CleanupRefusedError(f"{record['id']}: source delete rowcount")
        deleted_fts = sum(
            conn.execute("DELETE FROM doc_sources_fts WHERE rowid=? AND source_id=? AND text=?", (rowid, row[0], row[1])).rowcount
            for rowid, row in fts
        )
        if deleted_fts != len(fts):
            raise CleanupRefusedError(f"{record['id']}: FTS delete rowcount")
        if doc is not None and conn.execute("DELETE FROM doc_nodes WHERE space=? AND node_id=? AND node_type=? AND properties IS ?", (doc[0], record["id"], doc[2], doc[3])).rowcount != 1:
            raise CleanupRefusedError(f"{record['id']}: doc delete rowcount")
        _commit(conn)
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _progress(run_dir: Path, record: dict[str, Any], axis: str, outcome: str) -> None:
    path = run_dir / "progress.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": record["id"], "axis": axis, "outcome": outcome}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _done_axes(snapshot: dict[str, Any]) -> set[str]:
    done: set[str] = set()
    if snapshot["vector"] == []:
        done.add("vector")
    if snapshot["graph"] == [] and snapshot["edges"] == _edge_digest([]):
        done |= {"graph", "edges"}
    for axis in ("source", "fts", "doc", "registry_node", "registry_edges"):
        if snapshot[axis] == []:
            done.add(axis)
    return done


def _before_axis_allowed(current: dict[str, Any], recorded: dict[str, Any], axes_done: set[str], record: dict[str, Any], axis: str) -> bool:
    """Axis별 resume precheck seam을 제공한다."""
    return _allowed(current, recorded, axes_done)


def _apply_records(data_dir: Path, run_dir: Path, vector_table: str, candidates: Any, protected_digest: str, receipt: dict[str, Any], manifest: dict[str, Any]) -> None:
    scope = _SCOPE
    for record in candidates:
        carried = None
        axes = (("vector", _delete_vector), ("graph", _delete_graph), ("document", _delete_document), ("registry", _delete_registry))
        index = retries = 0
        while index < len(axes):
            axis, action = axes[index]
            if retries > 3:
                raise CleanupRefusedError(f"{record['id']}: database set kept changing during {axis}")
            if _axis_gate(data_dir, vector_table, candidates):
                carried = None
            current = carried if carried is not None else _fresh_snapshot(data_dir, vector_table, record["id"])
            done = _done_axes(current)
            graph_conn = _writer(data_dir, "graph.db")
            try:
                _validate_graph_intent(graph_conn, candidates, receipt, manifest, record["id"])
            finally:
                graph_conn.close()
            # The R/D/S helper owns component edges. Generic axis CAS compares every other component.
            current["edges"] = record["snapshot"]["edges"]
            if not _before_axis_allowed(current, record["snapshot"], done, record, axis):
                raise CleanupRefusedError(f"{record['id']}: snapshot conflict before {axis}")
            target_axes = {"vector"} if axis == "vector" else {"graph", "edges"} if axis == "graph" else {"source", "fts", "doc"} if axis == "document" else {"registry_node", "registry_edges"}
            if not target_axes <= done:
                if axis == "vector":
                    action(data_dir, vector_table, record)
                elif axis == "graph":
                    action(data_dir, record, candidates, receipt, manifest)
                elif axis == "registry":
                    action(data_dir, record, candidates, manifest)
                else:
                    action(data_dir, record)
                if scope is not None:
                    scope.wrote = True
            after = _fresh_snapshot(data_dir, vector_table, record["id"])
            carried = dict(after)
            done = _done_axes(after)
            graph_conn = _writer(data_dir, "graph.db")
            try:
                _validate_graph_intent(graph_conn, candidates, receipt, manifest, record["id"])
            finally:
                graph_conn.close()
            after["edges"] = record["snapshot"]["edges"]
            if not target_axes <= done or not _allowed(after, record["snapshot"], done):
                raise CleanupRefusedError(f"{record['id']}: snapshot conflict after {axis}")
            if _axis_end_check(data_dir, vector_table, candidates, protected_digest, record["id"]):
                # 축 끝에서 외부 변화를 처리했다. 색인이 바뀌었으므로 같은 축을 다시 검증한 뒤 기록한다.
                carried = None
                retries += 1
                continue
            _progress(run_dir, record, axis, "done")
            index += 1
            retries = 0


def apply(data_dir: Path, run_dir: Path, vector_table: str, approved_manifest: Path | None, backup_dir: Path, *, scan: Callable[[Path], tuple[list[int], int]] | None = None) -> None:
    """승인, 백업, CAS가 모두 맞을 때만 frozen 후보를 삭제한다."""
    global _SCOPE

    candidates, manifest = _load_run(run_dir)
    scope = _RunScope(data_dir, vector_table, candidates, manifest)
    _SCOPE = scope
    try:
        with _selection_scope(manifest.selection):
            _apply_run(data_dir, run_dir, vector_table, approved_manifest, backup_dir, scan, candidates, manifest, scope)
    finally:
        _SCOPE = None
        if scope.readers is not None:
            scope.readers.close()
        if scope.witness is not None:
            scope.witness.close()
        candidates.close()
        manifest.close()


def _apply_run(data_dir: Path, run_dir: Path, vector_table: str, approved_manifest: Path | None, backup_dir: Path, scan: Callable[[Path], tuple[list[int], int]] | None, candidates: Any, manifest: dict[str, Any], scope: _RunScope) -> None:
    from repair_store_mismatch_499 import check_holders, holders

    from opencrab.locking import write_lock

    _load_approval(approved_manifest, manifest)
    state, _, _ = marker_state(run_dir)
    if state != "absent":
        raise CleanupRefusedError(f"apply refuses rollback marker state: {state}")
    with write_lock(str(data_dir), timeout=10.0, purpose="cleanup_openclaw_heartbeat_493"):
        check_holders(data_dir, scan or holders)
        require_empty_wal(data_dir)
        if _schema_digest(data_dir, vector_table, immutable=False) != manifest["schema_sha256"] or _store_set_digest(data_dir) != manifest["store_set_sha256"]:
            raise CleanupRefusedError("database set changed after frozen manifest")
        state, _, _ = marker_state(run_dir)
        if state != "absent":
            raise CleanupRefusedError(f"apply refuses rollback marker state: {state}")
        scope.witness = _Witness(data_dir)
        _require_no_triggers(scope.witness)
        scope.readers = _Readers(data_dir, vector_table, immutable=False, ids=set(candidates.ids))
        if scope.witness.changed():
            raise CleanupRefusedError("database set changed after frozen manifest")
        receipt_path = run_dir / "receipt.json"
        protected_digest = manifest["protected_sha256"]
        scope.protected_digest = protected_digest
        if not receipt_path.exists() and not _full_snapshot_matches(data_dir, vector_table, candidates, protected_digest):
            raise CleanupRefusedError("first apply requires the full frozen snapshot")
        receipt = _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir) if receipt_path.exists() else _backup_receipt(data_dir, run_dir, backup_dir, manifest, candidates)
        _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir)
        scope.receipt = receipt
        _forget_receipts(id(receipt))
        if _change_pending():
            _handle_change()
        if not _verify_protection(data_dir, vector_table, candidates, protected_digest):
            raise CleanupRefusedError("protected set changed after frozen manifest")
        if _claim_guard(data_dir, candidates, manifest):
            raise CleanupRefusedError("Claim support changed after frozen manifest")
        if _registry_claim_guard(data_dir, candidates, manifest):
            raise CleanupRefusedError("registry Claim support changed after frozen manifest")
        scope.receipt = receipt = None
        _forget_receipts(None)
        receipt = _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir)
        scope.receipt = receipt
        _registry_resume_precheck(data_dir, vector_table, candidates)
        _validate_all_candidate_claims(data_dir, candidates, manifest)
        _release_intent(receipt, candidates, manifest)
        # 새 프로세스의 기준값은 시작 시점 상태를 채택하므로 첫 쓰기 전에 후보 전체 R/D/S를 한 번 확인한다.
        graph_conn = _writer(data_dir, "graph.db")
        try:
            _validate_graph_intent(graph_conn, candidates, receipt, manifest)
        finally:
            graph_conn.close()
        _apply_records(data_dir, run_dir, vector_table, candidates, protected_digest, receipt, manifest)
        graph_conn = _writer(data_dir, "graph.db")
        try:
            _validate_graph_intent(graph_conn, candidates, receipt, manifest)
        finally:
            graph_conn.close()
        if _current_protection_digest(data_dir, vector_table, candidates, immutable=False) != protected_digest:
            raise CleanupRefusedError("protected set changed after apply")
        registry_after = _registry_report(data_dir, _ids_of(candidates), immutable=False)
        _publish(run_dir / "registry-after.json", _canon(registry_after))
        _publish(run_dir / "apply-complete.json", _canon({"manifest_sha256": _manifest_sha(manifest), "backup_set_id": receipt["backup_set_id"], "registry_after": registry_after}))


def _attempt_dir(run_dir: Path, attempt: str | None = None) -> Path:
    root = run_dir / "restore-attempts"
    if not root.exists():
        root.mkdir(mode=0o700)
        _fsync_dir(run_dir)
    if attempt is None:
        return root
    path = root / attempt
    path.mkdir(mode=0o700)
    _fsync_dir(root)
    _fsync_dir(run_dir)
    return path


def _publish_attempt_successor(run_dir: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """prepared tombstone 뒤 immutable attempt binding을 no-replace로 발행한다."""
    attempt = uuid.uuid4().hex
    directory = _attempt_dir(run_dir, attempt)
    payload = {**_marker_payload("attempt-published", receipt), "attempt": attempt, "directory": directory.name}
    payload["payload_sha256"] = _sha(_canon({key: value for key, value in payload.items() if key != "payload_sha256"}))
    publish_marker(run_dir, f"{ATTEMPT_PREFIX}{attempt}.json", payload)
    return payload


def _attempt_metadata(receipt: dict[str, Any], data_dir: Path, name: str, attempt: str) -> dict[str, Any]:
    return {
        "receipt_sha256": _sha(_canon(receipt)),
        "backup_set_id": receipt["backup_set_id"],
        "data_dir": _data_dir_identity(data_dir),
        "file": name,
        "attempt": attempt,
        "replacement": f".{name}.restore-{attempt}",
    }


def _published_attempts(run_dir: Path, receipt: dict[str, Any]) -> list[dict[str, Any]]:
    expected = _marker_payload("attempt-published", receipt)
    out: list[dict[str, Any]] = []
    for path in run_dir.glob(f"{ATTEMPT_PREFIX}*.json"):
        marker = _read_marker(path, "attempt-published")
        for key in ("manifest_sha256", "backup_set_id", "store_set_sha256", "data_dir", "receipt_sha256"):
            if marker[key] != expected[key]:
                raise CleanupRefusedError("attempt successor does not bind receipt")
        if marker["directory"] != marker["attempt"]:
            raise CleanupRefusedError("attempt successor directory does not match UUID")
        directory = run_dir / "restore-attempts" / marker["directory"]
        if not directory.is_dir():
            raise CleanupRefusedError("published attempt directory is missing")
        out.append(marker)
    return sorted(out, key=lambda marker: marker["attempt"])


def _validate_attempts(run_dir: Path, receipt: dict[str, Any], data_dir: Path) -> None:
    """모든 restore artifact를 발행된 UUID, metadata, replacement 이름으로 함께 검증한다."""
    root = run_dir / "restore-attempts"
    published = {marker["attempt"]: marker for marker in _published_attempts(run_dir, receipt)}
    if not root.exists():
        if published:
            raise CleanupRefusedError("published attempt directory is missing")
        root_entries: list[Path] = []
    else:
        root_entries = list(root.iterdir())
    expected_base = {
        "receipt_sha256": _sha(_canon(receipt)),
        "backup_set_id": receipt["backup_set_id"],
        "data_dir": _data_dir_identity(data_dir),
    }
    metadata_by_attempt: dict[tuple[str, str], dict[str, Any]] = {}
    for directory in root_entries:
        if not directory.is_dir() or directory.name not in published:
            raise CleanupRefusedError("restore artifact binding mismatch")
        for path in directory.iterdir():
            if not path.is_file() or path.suffix != ".json":
                raise CleanupRefusedError("restore artifact binding mismatch")
            metadata = _json_object(path.read_text(encoding="utf-8"), path.name)
            required = {"receipt_sha256", "backup_set_id", "data_dir", "file", "attempt", "replacement"}
            if (
                set(metadata) != required
                or any(metadata[key] != value for key, value in expected_base.items())
                or metadata["file"] not in DB_FILES
                or metadata["attempt"] != directory.name
                or metadata["replacement"] != f".{metadata['file']}.restore-{directory.name}"
                or path.name != f"{metadata['file']}.json"
                or (directory.name, metadata["file"]) in metadata_by_attempt
            ):
                raise CleanupRefusedError("restore artifact binding mismatch")
            metadata_by_attempt[(directory.name, metadata["file"])] = metadata
    for name in DB_FILES:
        destination = data_dir / name
        for sidecar in (destination.with_name(destination.name + suffix) for suffix in ("-wal", "-shm", "-journal")):
            if sidecar.exists():
                raise CleanupRefusedError("restore sidecar artifact is unbound")
        for artifact in data_dir.glob(f".{name}.*"):
            prefix = f".{name}.restore-"
            if not artifact.is_file() or not artifact.name.startswith(prefix):
                raise CleanupRefusedError("restore artifact binding mismatch")
            attempt = artifact.name.removeprefix(prefix)
            metadata = metadata_by_attempt.get((attempt, name))
            if metadata is None or metadata["replacement"] != artifact.name:
                raise CleanupRefusedError("restore artifact binding mismatch")


def _remove_sidecars(destination: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = destination.with_name(destination.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
            _fsync_dir(destination.parent)


def _restore_file(source: Path, destination: Path, attempt_dir: Path, receipt: dict[str, Any]) -> None:
    """발행된 successor UUID 아래 metadata를 쓰고 해당 UUID replacement만 복원한다."""
    attempt = attempt_dir.name
    replacement = destination.with_name(f".{destination.name}.restore-{attempt}")
    metadata = _attempt_metadata(receipt, destination.parent, destination.name, attempt)
    metadata_path = attempt_dir / f"{destination.name}.json"
    _write_fsync(metadata_path, _canon(metadata), exclusive=True)
    _fsync_dir(attempt_dir)
    with source.open("rb") as reader, replacement.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(replacement, destination)
    _fsync_dir(destination.parent)
    _remove_sidecars(destination)


def prepare_rollback(data_dir: Path, run_dir: Path, backup_dir: Path) -> None:
    """복원 전에 exact receipt를 묶은 tombstone만 안전하게 발행한다."""
    from repair_store_mismatch_499 import check_holders, holders

    from opencrab.locking import write_lock

    candidates, manifest = _load_run(run_dir)
    receipt = _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir)
    state, _, _ = marker_state(run_dir)
    if state != "absent":
        raise CleanupRefusedError(f"rollback preparation refuses marker state: {state}")
    with write_lock(str(data_dir), timeout=10.0, purpose="cleanup_openclaw_heartbeat_493 rollback prepare"):
        check_holders(data_dir, holders)
        if marker_state(run_dir)[0] != "absent":
            raise CleanupRefusedError("rollback preparation marker changed before lock")
        if receipt["data_dir"] != _data_dir_identity(data_dir):
            raise CleanupRefusedError("receipt does not bind this data directory")
        publish_marker(run_dir, "rollback-tombstone.json", _marker_payload("tombstone", receipt))


def rollback(data_dir: Path, run_dir: Path, backup_dir: Path) -> None:
    """완전한 tombstone과 exact receipt가 있을 때 네 DB 전체를 복원한다."""
    from repair_store_mismatch_499 import check_holders, holders

    from opencrab.locking import write_lock

    candidates, manifest = _load_run(run_dir)
    receipt = _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir)
    state, tombstone, completion = marker_state(run_dir)
    expected_tombstone = _marker_payload("tombstone", receipt)
    expected_completion = _marker_payload("completion", receipt)
    if state == "absent" or state == "incomplete artifact":
        raise CleanupRefusedError(f"rollback refuses marker state: {state}")
    if tombstone != expected_tombstone or (completion is not None and completion != expected_completion):
        raise CleanupRefusedError("rollback marker does not bind this receipt")
    with write_lock(str(data_dir), timeout=10.0, purpose="cleanup_openclaw_heartbeat_493 rollback"):
        check_holders(data_dir, holders)
        state, tombstone, completion = marker_state(run_dir)
        if receipt["data_dir"] != _data_dir_identity(data_dir) or tombstone != expected_tombstone or (completion is not None and completion != expected_completion):
            raise CleanupRefusedError("rollback marker changed before lock")
        _validate_attempts(run_dir, receipt, data_dir)
        if state == "complete tombstone plus completion":
            for name in DB_FILES:
                _remove_sidecars(data_dir / name)
                verify_sqlite(data_dir / name)
                if _file_sha(data_dir / name) != receipt["files"][name]["sha256"]:
                    raise CleanupRefusedError("completed rollback data differs from receipt")
            _fsync_dir(run_dir)
            return
        successor = _publish_attempt_successor(run_dir, receipt)
        attempt_dir = run_dir / "restore-attempts" / successor["directory"]
        _validate_attempts(run_dir, receipt, data_dir)
        for name in DB_FILES:
            _restore_file(backup_dir / name, data_dir / name, attempt_dir, receipt)
        for name in DB_FILES:
            verify_sqlite(data_dir / name)
            if _file_sha(data_dir / name) != receipt["files"][name]["sha256"]:
                raise CleanupRefusedError(f"restored file differs from receipt: {name}")
        publish_marker(run_dir, "rollback-complete.json", expected_completion)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local-data-dir", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--vector-table", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--prepare-rollback", action="store_true")
    parser.add_argument("--approved-manifest")
    parser.add_argument("--approved-ids", help="dry-run에서만 쓴다. 이 파일의 ID 집합만 후보로 삼는다.")
    parser.add_argument("--backup-dir")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir, run_dir = Path(args.local_data_dir).resolve(), Path(args.run_dir).resolve()
    try:
        if sum((args.apply, args.rollback, args.prepare_rollback)) > 1:
            raise CleanupRefusedError("choose only one of --apply, --rollback, --prepare-rollback")
        if args.approved_ids and (args.apply or args.rollback or args.prepare_rollback):
            raise CleanupRefusedError("--approved-ids is only for dry-run; apply and rollback read the selection from the manifest")
        if args.apply:
            if not args.backup_dir:
                raise CleanupRefusedError("--backup-dir is required for apply")
            apply(data_dir, run_dir, args.vector_table, Path(args.approved_manifest) if args.approved_manifest else None, Path(args.backup_dir).resolve())
            print("apply complete")
        elif args.prepare_rollback:
            if not args.backup_dir:
                raise CleanupRefusedError("--backup-dir is required for rollback preparation")
            prepare_rollback(data_dir, run_dir, Path(args.backup_dir).resolve())
            print("rollback tombstone complete")
        elif args.rollback:
            if not args.backup_dir:
                raise CleanupRefusedError("--backup-dir is required for rollback")
            rollback(data_dir, run_dir, Path(args.backup_dir).resolve())
            print("rollback complete")
        else:
            result = dry_run(data_dir, run_dir, args.vector_table, Path(args.approved_ids) if args.approved_ids else None)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return EXIT_OK
    except CleanupRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
