#!/usr/bin/env python3
"""OpenClaw heartbeat 레코드를 네 SQLite 저장소에서 정리한다.

기본 동작은 읽기 전용 dry-run이다. 이 도구는 라이브 서비스 제어를 하지 않는다.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
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


def _candidate_snapshot(data_dir: Path, vector_table: str, node_id: str, *, immutable: bool) -> dict[str, Any]:
    doc = _ro(data_dir / "doc_store.db", immutable)
    graph = _ro(data_dir / "graph.db", immutable)
    vectors = _ro(data_dir / "vectors.db", immutable)
    try:
        _load_vec(vectors)
        cols = _ensure_vector_table(vectors, vector_table)
        source_rows = doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (node_id,)).fetchall()
        fts_rows = doc.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (node_id,)).fetchall()
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
        }
    finally:
        doc.close()
        graph.close()
        vectors.close()


def _select_candidate(data_dir: Path, vector_table: str, source: tuple[Any, ...]) -> dict[str, Any]:
    node_id, text, metadata = source
    if not isinstance(node_id, str) or not node_id.startswith("evidence:turn:"):
        raise CleanupRefusedError("internal selection received an invalid source id")
    if not isinstance(text, str) or not text.startswith(PREFIXES):
        raise CleanupRefusedError("internal selection received a non-heartbeat source")
    if _strict_pack(metadata, f"source {node_id}") != "openclaw":
        raise CleanupRefusedError("internal selection received another pack")
    doc = _ro(data_dir / "doc_store.db", True)
    graph = _ro(data_dir / "graph.db", True)
    vectors = _ro(data_dir / "vectors.db", True)
    try:
        _load_vec(vectors)
        columns = _ensure_vector_table(vectors, vector_table)
        fts = doc.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (node_id,)).fetchall()
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
        elif docs[0][0] != EVIDENCE_SPACE or docs[0][2] != "Evidence" or _strict_pack(docs[0][3], f"doc {node_id}") != "openclaw":
            errors.append("evidence doc ownership")
        if len(nodes) != 1:
            errors.append("Evidence graph multiplicity")
        elif nodes[0][0] != "Evidence" or nodes[0][2] != EVIDENCE_SPACE or _strict_pack(nodes[0][3], f"graph {node_id}") != "openclaw":
            errors.append("Evidence graph ownership")
        if len(vectors_rows) != 1:
            errors.append("vector multiplicity")
        else:
            row = vectors_rows[0]
            if row[1] != "openclaw" or _strict_pack(row[3], f"vector {node_id}") != "openclaw":
                errors.append("vector ownership")
            elif _json_object(row[3], f"vector {node_id}").get("node_id") != node_id:
                errors.append("vector identity")
        if errors:
            raise CleanupRefusedError(f"{node_id}: " + ", ".join(errors))
        edges = graph.execute(
            "SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? OR to_id=? ORDER BY from_id,relation,to_id",
            (node_id, node_id),
        ).fetchall()
        snap = _candidate_snapshot(data_dir, vector_table, node_id, immutable=True)
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
        doc.close()
        graph.close()
        vectors.close()


def _protected(data_dir: Path, vector_table: str, candidate_ids: set[str]) -> dict[str, Any]:
    """보호해야 할 Document, Claim, 비후보 Evidence의 정체성을 기록한다."""
    doc = _ro(data_dir / "doc_store.db", True)
    graph = _ro(data_dir / "graph.db", True)
    vectors = _ro(data_dir / "vectors.db", True)
    try:
        _load_vec(vectors)
        _ensure_vector_table(vectors, vector_table)
        nodes = graph.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes ORDER BY node_id").fetchall()
        protected_nodes: list[dict[str, Any]] = []
        noncandidate_evidence: list[dict[str, Any]] = []
        protected_edges: list[dict[str, Any]] = []
        for row in nodes:
            node_type, node_id, space, raw = row
            item = {"id": node_id, "type": node_type, "space": space, "properties": _props_digest(raw)}
            if node_type in {"Document", "Claim"}:
                protected_nodes.append(item)
            if node_type == "Evidence" and node_id not in candidate_ids:
                doc_rows = doc.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (node_id,)).fetchall()
                vec_rows = vectors.execute(f"SELECT node_id,pack_id,document,metadata,embedding FROM {vector_table} WHERE node_id=?", (node_id,)).fetchall()
                noncandidate_evidence.append({"graph": item, "doc": [_row_digest(x) for x in doc_rows], "vector": [_row_digest(x) for x in vec_rows]})
        for row in graph.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges ORDER BY from_id,relation,to_id"):
            edge = _edge_record(row)
            if edge["from_id"] not in candidate_ids and edge["to_id"] not in candidate_ids:
                protected_edges.append(edge)
        return {"nodes": protected_nodes, "evidence": noncandidate_evidence, "edges": protected_edges}
    finally:
        doc.close()
        graph.close()
        vectors.close()


def _claim_guard(data_dir: Path, candidates: list[dict[str, Any]]) -> list[str]:
    ids = {record["id"] for record in candidates}
    graph = _ro(data_dir / "graph.db", True)
    try:
        claims = {row[0] for row in graph.execute("SELECT node_id FROM graph_nodes WHERE node_type='Claim'")}
        bad: list[str] = []
        for claim in claims:
            supports = {row[0] for row in graph.execute("SELECT from_id FROM graph_edges WHERE relation='supports' AND to_id=?", (claim,))}
            if supports & ids and supports <= ids:
                bad.append(claim)
        return sorted(bad)
    finally:
        graph.close()


def _registry_report(data_dir: Path, candidate_ids: set[str]) -> dict[str, Any]:
    """registry는 읽기 전용으로 before와 candidate graph target 뒤 orphan을 분리한다."""
    conn = _ro(data_dir / "opencrab.db", True)
    graph = _ro(data_dir / "graph.db", True)
    try:
        rows = conn.execute("SELECT space,node_type,node_id FROM ontology_nodes ORDER BY space,node_id").fetchall()
        edges = conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges ORDER BY id").fetchall()
        graph_ids = {row[0] for row in graph.execute("SELECT node_id FROM graph_nodes")}
        before_orphans = [list(edge) for edge in edges if edge[1] not in graph_ids or edge[4] not in graph_ids]
        expected_ids = graph_ids - candidate_ids
        expected_orphans = [list(edge) for edge in edges if edge[1] not in expected_ids or edge[4] not in expected_ids]
        selected = [list(row) for row in rows if row[2] in candidate_ids]
        return {
            "candidate_rows": selected,
            "candidate_edges": [list(edge) for edge in edges if edge[1] in candidate_ids or edge[4] in candidate_ids],
            "preexisting_orphans": before_orphans,
            "expected_after_orphans": expected_orphans,
            "candidate_count": len(selected),
            "total_rows": len(rows),
            "mode": "report-only",
        }
    finally:
        graph.close()
        conn.close()


def select(data_dir: Path, vector_table: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    require_empty_wal(data_dir)
    doc = _ro(data_dir / "doc_store.db", True)
    try:
        rows = doc.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id LIKE 'evidence:turn:%' ORDER BY source_id").fetchall()
    finally:
        doc.close()
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        try:
            if _strict_pack(row[2], f"source {row[0]}") == "openclaw" and isinstance(row[1], str) and row[1].startswith(PREFIXES):
                candidates.append(_select_candidate(data_dir, vector_table, row))
        except CleanupRefusedError as exc:
            errors.append(str(exc))
    candidate_ids = {candidate["id"] for candidate in candidates}
    last_claims = _claim_guard(data_dir, candidates)
    if last_claims:
        errors.append("candidate set removes final Claim support: " + ", ".join(last_claims))
    protected = _protected(data_dir, vector_table, candidate_ids)
    report = {"errors": errors, "registry": _registry_report(data_dir, candidate_ids), "last_support_claims": last_claims}
    return candidates, {"protected": protected, "report": report}


def _candidate_bytes(candidates: list[dict[str, Any]]) -> bytes:
    return b"".join(_canon(record) + b"\n" for record in candidates)


def _ordered_digest(candidates: list[dict[str, Any]]) -> str:
    return _sha(_canon([record["id"] for record in candidates]))


def dry_run(data_dir: Path, run_dir: Path, vector_table: str) -> dict[str, Any]:
    """후보와 보호 집합을 기록한다. 오류가 있으면 manifest를 만들지 않는다."""
    if any((run_dir / name).exists() for name in ("candidates.jsonl", "manifest.json", "receipt.json")):
        raise CleanupRefusedError("run directory already has published data")
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    candidates, detail = select(data_dir, vector_table)
    body = _candidate_bytes(candidates)
    _publish(run_dir / "candidates.jsonl", body)
    protected_digest = _sha(_canon(detail["protected"]))
    manifest = {
        "candidate_sha256": _sha(body),
        "ordered_ids_sha256": _ordered_digest(candidates),
        "schema_sha256": _schema_digest(data_dir, vector_table, immutable=True),
        "snapshot_sha256": _store_set_digest(data_dir),
        "store_set_sha256": _store_set_digest(data_dir),
        "vector_table": vector_table,
        "protected": detail["protected"],
        "protected_sha256": protected_digest,
    }
    _publish(run_dir / "report.json", _canon({**detail["report"], "protected_sha256": protected_digest}))
    if detail["report"]["errors"]:
        return {"eligible": False, "candidates": len(candidates), "errors": detail["report"]["errors"]}
    _publish(run_dir / "manifest.json", _canon(manifest))
    return {"eligible": True, "candidates": len(candidates), "manifest": manifest}


def _load_run(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates_path, manifest_path = run_dir / "candidates.jsonl", run_dir / "manifest.json"
    if not candidates_path.is_file() or not manifest_path.is_file():
        raise CleanupRefusedError("run has no applyable manifest")
    body = candidates_path.read_bytes()
    manifest = _json_object(manifest_path.read_text(encoding="utf-8"), "manifest")
    candidates = [_json_object(line, "candidate") for line in body.decode("utf-8").splitlines() if line]
    if manifest.get("candidate_sha256") != _sha(body) or manifest.get("ordered_ids_sha256") != _ordered_digest(candidates):
        raise CleanupRefusedError("candidate file does not match manifest")
    return candidates, manifest


def _load_approval(path: Path | None, manifest: dict[str, Any]) -> None:
    if path is None or not path.is_file():
        raise CleanupRefusedError("--approved-manifest is required for apply")
    approval = _json_object(path.read_text(encoding="utf-8"), "approval")
    if set(approval) != {"manifest_sha256", "ordered_ids_sha256"}:
        raise CleanupRefusedError("approval has unexpected or missing fields")
    if approval["manifest_sha256"] != _sha(_canon(manifest)) or approval["ordered_ids_sha256"] != manifest["ordered_ids_sha256"]:
        raise CleanupRefusedError("approval does not bind this exact manifest")


def _component_intents(candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
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
        "manifest": _sha(_canon(manifest)),
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
        _fsync_dir(backup_dir)
        _fsync_dir(backup_dir.parent)
    except Exception:
        raise
    receipt = {
        "backup_set_id": _sha(_canon(files)),
        "files": files,
        "manifest_sha256": _sha(_canon(manifest)),
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
    if receipt.get("manifest_sha256") != _sha(_canon(manifest)) or receipt.get("store_set_sha256") != manifest.get("store_set_sha256"):
        raise CleanupRefusedError("receipt does not bind manifest and store set")
    if data_dir is not None and receipt.get("data_dir") != _data_dir_identity(data_dir):
        raise CleanupRefusedError("receipt does not bind this data directory")
    _validate_intent(receipt, candidates, manifest)
    files = receipt.get("files")
    if not isinstance(files, dict) or set(files) != set(DB_FILES):
        raise CleanupRefusedError("receipt does not hold four backups")
    for name in DB_FILES:
        copy = backup_dir / name
        if not copy.is_file() or _file_sha(copy) != files[name].get("sha256") or (copy.stat().st_mode & 0o777) != 0o600:
            raise CleanupRefusedError(f"backup receipt mismatch: {name}")
        verify_sqlite(copy)
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
    targets = {"vector": [], "graph": [], "edges": _edge_digest([]), "source": [], "fts": [], "doc": []}
    for key, value in current.items():
        if value == recorded.get(key):
            continue
        if key == "edges" and shared_edges_absent and value == targets[key]:
            continue
        if key in axes_done and value == targets[key]:
            continue
        return False
    return True


def _full_snapshot_matches(data_dir: Path, vector_table: str, candidates: list[dict[str, Any]], protected_digest: str) -> bool:
    """첫 apply는 frozen 원본 전체와 정확히 같을 때만 backup을 만든다."""
    if _current_protection_digest(data_dir, vector_table, candidates) != protected_digest:
        return False
    return all(_candidate_snapshot(data_dir, vector_table, record["id"], immutable=False) == record["snapshot"] for record in candidates)


def _component_edges(candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Frozen candidates와 immutable receipt에서 authoritative R/D를 재계산한다."""
    intent = _validate_intent(receipt, candidates, manifest)
    ids = {record["id"] for record in candidates}
    recorded = {
        _sha(_canon(edge)): edge
        for record in candidates
        for edge in record["edges"]
        if edge["from_id"] in ids and edge["to_id"] in ids
    }
    approved = {_sha(_canon(item["edge"])) for item in intent["component"]}
    return recorded, approved


def _validate_component_edges(conn: sqlite3.Connection, candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """같은 R/D/S helper를 caller와 graph transaction에서 사용한다."""
    recorded, approved = _component_edges(candidates, receipt, manifest)
    ids = {record["id"] for record in candidates}
    placeholders = ",".join("?" for _ in ids)
    actual_rows = [_edge_record(row) for row in conn.execute(f"SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id IN ({placeholders}) AND to_id IN ({placeholders}) ORDER BY from_id,relation,to_id", (*ids, *ids))]
    actual = {_sha(_canon(edge)): edge for edge in actual_rows}
    if not set(recorded) - approved <= set(actual) <= set(recorded):
        raise CleanupRefusedError("component edge R/D/S conflict")
    return actual_rows


def _boundary_edges(candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Frozen boundary rows와 immutable deletion target을 반환한다."""
    intent = _validate_intent(receipt, candidates, manifest)
    recorded = {_sha(_canon(item["edge"])): item["edge"] for item in intent["boundary"]}
    approved = {_sha(_canon(item["edge"])) for item in intent["boundary"] if item["deletion_target"] is True}
    return recorded, approved


def _validate_boundary_edges(conn: sqlite3.Connection, candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidate boundary rows의 exact identity와 approved absence를 확인한다."""
    recorded, approved = _boundary_edges(candidates, receipt, manifest)
    ids = {record["id"] for record in candidates}
    placeholders = ",".join("?" for _ in ids)
    actual_rows = [_edge_record(row) for row in conn.execute(f"SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE (from_id IN ({placeholders}) AND to_id NOT IN ({placeholders})) OR (from_id NOT IN ({placeholders}) AND to_id IN ({placeholders})) ORDER BY from_id,relation,to_id", (*ids, *ids, *ids, *ids))]
    actual = {_sha(_canon(edge)): edge for edge in actual_rows}
    if not set(recorded) - approved <= set(actual) <= set(recorded):
        raise CleanupRefusedError("boundary edge R/D/S conflict")
    return actual_rows


def _validate_graph_intent(conn: sqlite3.Connection, candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Component와 boundary의 immutable graph intent를 함께 확인한다."""
    _validate_component_edges(conn, candidates, receipt, manifest)
    _validate_boundary_edges(conn, candidates, receipt, manifest)


def _protected_graph(conn: sqlite3.Connection, candidate_ids: set[str], excluded_edges: set[str] | None = None, excluded_nodes: set[str] | None = None) -> dict[str, Any]:
    """같은 graph transaction에서 protected graph projection을 만든다."""
    excluded_edges = excluded_edges or set()
    excluded_nodes = excluded_nodes or set()
    nodes: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for node_type, node_id, space, raw in conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes ORDER BY node_id"):
        if node_id in excluded_nodes:
            continue
        item = {"id": node_id, "type": node_type, "space": space, "properties": _props_digest(raw)}
        if node_type in {"Document", "Claim"}:
            nodes.append(item)
        elif node_type == "Evidence" and node_id not in candidate_ids:
            evidence.append(item)
    edges = []
    for row in conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges ORDER BY from_id,relation,to_id"):
        edge = _edge_record(row)
        if _sha(_canon(edge)) in excluded_edges:
            continue
        if edge["from_id"] not in candidate_ids and edge["to_id"] not in candidate_ids:
            edges.append(edge)
    return {"nodes": nodes, "evidence": evidence, "edges": edges}


def _validate_projected_protection(conn: sqlite3.Connection, candidates: list[dict[str, Any]], manifest: dict[str, Any], planned_edges: set[str], planned_nodes: set[str]) -> None:
    """Planned graph write 전 protected projection과 affected Claim support를 검사한다."""
    expected = manifest.get("protected")
    if not isinstance(expected, dict):
        raise CleanupRefusedError("manifest has no protected graph state")
    candidate_ids = {record["id"] for record in candidates}
    current = _protected_graph(conn, candidate_ids, planned_edges, planned_nodes)
    expected_graph = {
        "nodes": expected.get("nodes"),
        "evidence": [item.get("graph") for item in expected.get("evidence", [])],
        "edges": expected.get("edges"),
    }
    if not _strict_equal(current, expected_graph):
        raise CleanupRefusedError("projected protected graph state changed")
    claim_rows = {
        node_id: (space, _props_digest(raw))
        for node_type, node_id, space, raw in conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_type='Claim'")
    }
    frozen_claims = {
        item["id"]: (item["space"], item["properties"])
        for item in expected_graph["nodes"]
        if item["type"] == "Claim"
    }
    affected_claims = {
        edge["to_id"]
        for record in candidates
        for edge in record["edges"]
        if _sha(_canon(edge)) in planned_edges and edge["relation"] == "supports" and edge["to_id"] in claim_rows and claim_rows[edge["to_id"]] == frozen_claims.get(edge["to_id"])
    }
    expected_edges = expected_graph["edges"]
    for claim in affected_claims:
        expected_supports = {
            _sha(_canon(edge))
            for edge in expected_edges
            if edge["relation"] == "supports" and edge["to_id"] == claim
        }
        current_supports = set()
        projected_supports = set()
        for row in conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE relation='supports' AND to_id=?", (claim,)):
            edge = _edge_record(row)
            key = _sha(_canon(edge))
            if edge["from_id"] not in candidate_ids:
                current_supports.add(key)
                if key not in planned_edges:
                    projected_supports.add(key)
        if current_supports != expected_supports or not projected_supports:
            raise CleanupRefusedError("planned graph delete removes final Claim support")


def _validate_all_candidate_claims(data_dir: Path, candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """모든 candidate graph target의 Claim support 유실을 첫 axis write 전에 막는다."""
    conn = _ro(data_dir / "graph.db", True)
    try:
        planned_edges = {
            _sha(_canon(edge))
            for record in candidates
            for edge in record["edges"]
        }
        planned_nodes = {record["id"] for record in candidates}
        _validate_projected_protection(conn, candidates, manifest, planned_edges, planned_nodes)
    finally:
        conn.close()


def _current_protection_digest(data_dir: Path, vector_table: str, candidates: list[dict[str, Any]]) -> str:
    return _sha(_canon(_protected(data_dir, vector_table, {item["id"] for item in candidates})))


def _delete_vector(data_dir: Path, vector_table: str, record: dict[str, Any]) -> None:
    conn = _rw(data_dir / "vectors.db")
    try:
        _load_vec(conn)
        columns = _ensure_vector_table(conn, vector_table)
        selected = ["node_id", "pack_id", "document", "metadata", "embedding"]
        if "embedding_bit" in columns:
            selected.append("embedding_bit")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT {','.join(selected)} FROM {vector_table} WHERE node_id=?", (record["id"],)).fetchone()
        if row is None or _row_digest(row) != record["vector"]["digest"]:
            raise CleanupRefusedError(f"{record['id']}: vector CAS conflict")
        deleted = conn.execute(f"DELETE FROM {vector_table} WHERE node_id=?", (record["id"],)).rowcount
        if deleted != 1:
            raise CleanupRefusedError(f"{record['id']}: vector delete rowcount")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _delete_graph(data_dir: Path, record: dict[str, Any], candidates: list[dict[str, Any]], receipt: dict[str, Any], manifest: dict[str, Any], after_commit: Callable[[], None] | None = None) -> None:
    conn = _rw(data_dir / "graph.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        node = conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (record["id"],)).fetchone()
        if node is None or node[0] != "Evidence" or node[2] != EVIDENCE_SPACE or _props_digest(node[3]) != record["graph"]["properties"]:
            raise CleanupRefusedError(f"{record['id']}: graph CAS conflict")
        component_before = {_sha(_canon(edge)) for edge in _validate_component_edges(conn, candidates, receipt, manifest)}
        boundary_before = {_sha(_canon(edge)) for edge in _validate_boundary_edges(conn, candidates, receipt, manifest)}
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
        _validate_projected_protection(conn, candidates, manifest, deleted_component | deleted_boundary, {record["id"]})
        for edge, raw in planned:
            result = conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation=? AND to_id=? AND from_type=? AND to_type=? AND properties IS ?", (edge["from_id"], edge["relation"], edge["to_id"], edge["from_type"], edge["to_type"], raw)).rowcount
            if result != 1:
                raise CleanupRefusedError(f"{record['id']}: graph edge delete rowcount")
        deleted = conn.execute("DELETE FROM graph_nodes WHERE node_id=? AND node_type='Evidence' AND space_id=? AND properties IS ?", (record["id"], EVIDENCE_SPACE, node[3])).rowcount
        if deleted != 1:
            raise CleanupRefusedError(f"{record['id']}: graph delete rowcount")
        component_after = {_sha(_canon(edge)) for edge in _validate_component_edges(conn, candidates, receipt, manifest)}
        boundary_after = {_sha(_canon(edge)) for edge in _validate_boundary_edges(conn, candidates, receipt, manifest)}
        if component_after != component_before - deleted_component or boundary_after != boundary_before - deleted_boundary:
            raise CleanupRefusedError(f"{record['id']}: graph planned post-state conflict")
        conn.commit()
        if after_commit is not None:
            after_commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _edge_raw_for_digest(conn: sqlite3.Connection, edge: dict[str, Any]) -> Any:
    row = conn.execute("SELECT properties FROM graph_edges WHERE from_id=? AND relation=? AND to_id=?", (edge["from_id"], edge["relation"], edge["to_id"])).fetchone()
    if row is None or _props_digest(row[0]) != edge["properties"]:
        raise CleanupRefusedError("graph edge properties changed")
    return row[0]


def _delete_document(data_dir: Path, record: dict[str, Any]) -> None:
    conn = _rw(data_dir / "doc_store.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (record["id"],)).fetchone()
        fts = conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (record["id"],)).fetchall()
        doc = conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (record["id"],)).fetchone()
        if source is not None and _row_digest(source) != record["source"]:
            raise CleanupRefusedError(f"{record['id']}: source CAS conflict")
        if fts and (len(fts) != 1 or _row_digest(fts[0]) != record["snapshot"]["fts"][0]):
            raise CleanupRefusedError(f"{record['id']}: FTS CAS conflict")
        if doc is not None and (doc[0] != EVIDENCE_SPACE or doc[2] != "Evidence" or _props_digest(doc[3]) != record["doc"]["properties"]):
            raise CleanupRefusedError(f"{record['id']}: doc CAS conflict")
        if source is not None and conn.execute("DELETE FROM doc_sources WHERE source_id=?", (record["id"],)).rowcount != 1:
            raise CleanupRefusedError(f"{record['id']}: source delete rowcount")
        if conn.execute("DELETE FROM doc_sources_fts WHERE source_id=?", (record["id"],)).rowcount != len(fts):
            raise CleanupRefusedError(f"{record['id']}: FTS delete rowcount")
        if doc is not None and conn.execute("DELETE FROM doc_nodes WHERE space=? AND node_id=? AND node_type=? AND properties IS ?", (doc[0], record["id"], doc[2], doc[3])).rowcount != 1:
            raise CleanupRefusedError(f"{record['id']}: doc delete rowcount")
        conn.commit()
    except Exception:
        conn.rollback()
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
    for axis in ("source", "fts", "doc"):
        if snapshot[axis] == []:
            done.add(axis)
    return done


def _apply_records(data_dir: Path, run_dir: Path, vector_table: str, candidates: list[dict[str, Any]], protected_digest: str, receipt: dict[str, Any], manifest: dict[str, Any]) -> None:
    for record in candidates:
        for axis, action in (("vector", _delete_vector), ("graph", _delete_graph), ("document", _delete_document)):
            current = _candidate_snapshot(data_dir, vector_table, record["id"], immutable=False)
            done = _done_axes(current)
            graph_conn = _rw(data_dir / "graph.db")
            try:
                _validate_graph_intent(graph_conn, candidates, receipt, manifest)
            finally:
                graph_conn.close()
            # The R/D/S helper owns component edges. Generic axis CAS compares every other component.
            current["edges"] = record["snapshot"]["edges"]
            if not _allowed(current, record["snapshot"], done):
                raise CleanupRefusedError(f"{record['id']}: snapshot conflict before {axis}")
            target_axes = {"vector"} if axis == "vector" else {"graph", "edges"} if axis == "graph" else {"source", "fts", "doc"}
            if not target_axes <= done:
                if axis == "vector":
                    action(data_dir, vector_table, record)
                elif axis == "graph":
                    action(data_dir, record, candidates, receipt, manifest)
                else:
                    action(data_dir, record)
            after = _candidate_snapshot(data_dir, vector_table, record["id"], immutable=False)
            done = _done_axes(after)
            graph_conn = _rw(data_dir / "graph.db")
            try:
                _validate_graph_intent(graph_conn, candidates, receipt, manifest)
            finally:
                graph_conn.close()
            after["edges"] = record["snapshot"]["edges"]
            if not target_axes <= done or not _allowed(after, record["snapshot"], done):
                raise CleanupRefusedError(f"{record['id']}: snapshot conflict after {axis}")
            if _current_protection_digest(data_dir, vector_table, candidates) != protected_digest:
                raise CleanupRefusedError(f"{record['id']}: protected set changed")
            _progress(run_dir, record, axis, "done")


def apply(data_dir: Path, run_dir: Path, vector_table: str, approved_manifest: Path | None, backup_dir: Path, *, scan: Callable[[Path], tuple[list[int], int]] | None = None) -> None:
    """승인, 백업, CAS가 모두 맞을 때만 frozen 후보를 삭제한다."""
    from repair_store_mismatch_499 import check_holders, holders

    from opencrab.locking import write_lock

    candidates, manifest = _load_run(run_dir)
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
        receipt_path = run_dir / "receipt.json"
        protected_digest = manifest["protected_sha256"]
        if not receipt_path.exists() and not _full_snapshot_matches(data_dir, vector_table, candidates, protected_digest):
            raise CleanupRefusedError("first apply requires the full frozen snapshot")
        receipt = _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir) if receipt_path.exists() else _backup_receipt(data_dir, run_dir, backup_dir, manifest, candidates)
        _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir)
        if _current_protection_digest(data_dir, vector_table, candidates) != protected_digest:
            raise CleanupRefusedError("protected set changed after frozen manifest")
        if _claim_guard(data_dir, candidates):
            raise CleanupRefusedError("Claim support changed after frozen manifest")
        receipt = _load_receipt(run_dir, backup_dir, manifest, candidates, data_dir)
        _validate_all_candidate_claims(data_dir, candidates, manifest)
        _apply_records(data_dir, run_dir, vector_table, candidates, protected_digest, receipt, manifest)
        registry_after = _registry_report(data_dir, {candidate["id"] for candidate in candidates})
        _publish(run_dir / "registry-after.json", _canon(registry_after))
        _publish(run_dir / "apply-complete.json", _canon({"manifest_sha256": _sha(_canon(manifest)), "backup_set_id": receipt["backup_set_id"], "registry_after": registry_after}))


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
    parser.add_argument("--backup-dir")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir, run_dir = Path(args.local_data_dir).resolve(), Path(args.run_dir).resolve()
    try:
        if sum((args.apply, args.rollback, args.prepare_rollback)) > 1:
            raise CleanupRefusedError("choose only one of --apply, --rollback, --prepare-rollback")
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
            result = dry_run(data_dir, run_dir, args.vector_table)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return EXIT_OK
    except CleanupRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
