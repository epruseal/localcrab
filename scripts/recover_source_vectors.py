#!/usr/bin/env python3
"""Recover approved source vectors from authoritative local source rows.

The tool defaults to dry-run. It reads only IDs in an approved JSONL file.
Use --apply with --backup-to only after the prevention fix is deployed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencrab.auth import Principal, principal_scope, require_local_principal
from opencrab.locking import write_lock
from opencrab.pack.write_gate import authorize
from opencrab.stores._graph_common import decode_properties
from opencrab.stores._vector_base import slot_owner
from opencrab.stores.chroma_store import _sanitize_metadata

MANIFEST_VERSION = 1


class RecoveryError(RuntimeError):
    """The recovery cannot safely process an approved target."""


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    text: str
    metadata: dict[str, Any]
    property_decode_error: bool = False


class SourceReader:
    """Read one current source row through a fresh SQLite mode=ro connection."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "doc_store.db"

    def get_source(self, source_id: str) -> SourceRow | None:
        uri = f"file:{self._path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT source_id, text, metadata FROM doc_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        metadata, corrupted = decode_properties(row[2])
        return SourceRow(row[0], row[1], metadata, corrupted)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _target_digest(source: SourceRow) -> str:
    return _digest({"text": source.text, "metadata": _sanitize_metadata(source.metadata)})


def _vector_digest(row: dict[str, Any]) -> str:
    return _digest({"text": row.get("document"), "metadata": _sanitize_metadata(row.get("metadata") or {})})


def read_approved(path: Path) -> tuple[list[str], str, str]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecoveryError("approved file is not UTF-8") from exc
    ids: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line:
            raise RecoveryError(f"approved file has blank line {lineno}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecoveryError(f"approved file has invalid JSON at line {lineno}") from exc
        if not isinstance(value, dict) or set(value) != {"id"}:
            raise RecoveryError(f"approved file has invalid record at line {lineno}")
        ident = value["id"]
        if not isinstance(ident, str) or not ident or not ident.strip():
            raise RecoveryError(f"approved file has invalid id at line {lineno}")
        ids.append(ident)
    if not ids or len(ids) != len(set(ids)):
        raise RecoveryError("approved file is empty or has duplicate IDs")
    return ids, hashlib.sha256(raw).hexdigest(), _digest(sorted(ids))


def checked_source(reader: SourceReader, ident: str) -> tuple[SourceRow, str, str]:
    source = reader.get_source(ident)
    if source is None:
        raise RecoveryError("approved source is absent")
    if (not isinstance(source.text, str) or source.property_decode_error
            or not isinstance(source.metadata, dict)):
        raise RecoveryError("approved source metadata is malformed")
    pack_id = source.metadata.get("pack_id")
    if not isinstance(pack_id, str) or not pack_id:
        raise RecoveryError("approved source has no valid pack ID")
    return source, pack_id, _target_digest(source)


def checked_vector(vector: Any, ident: str, pack_id: str) -> tuple[dict[str, Any], str]:
    row = vector.get_by_id(ident)
    if not isinstance(row, dict) or not isinstance(row.get("metadata"), dict):
        raise RecoveryError("approved vector is absent or malformed")
    if slot_owner(row["metadata"]) != pack_id:
        raise RecoveryError("approved vector owner does not match source")
    return row, _vector_digest(row)


def write_manifest(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as fh:
        fh.write(_canonical(value))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery manifest is unreadable") from exc
    if not isinstance(value, dict) or value.get("version") != MANIFEST_VERSION:
        raise RecoveryError("recovery manifest version is unsupported")
    return value


def vector_identity(vector: Any) -> dict[str, Any]:
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    if not isinstance(vector, SqliteVecStore):
        raise RecoveryError("recovery requires sqlite-vec")
    db_path = Path(vector._db_path)
    if not db_path.is_absolute():
        local_conn = getattr(vector, "_conn", None)
        db_path = Path(local_conn.execute("PRAGMA database_list").fetchone()[2]) if local_conn is not None else db_path
    resolved = db_path.resolve(strict=False)
    return {
        "class": f"{type(vector).__module__}.{type(vector).__name__}",
        "table": vector._table,
        "dimension": vector._dim,
        "has_bit": vector._has_bit_column,
        "path_digest": hashlib.sha256(str(resolved).encode()).hexdigest(),
    }


def _event(callback: Callable[[str], None], name: str) -> None:
    callback(name)


def plan(
    approved: Path,
    run_dir: Path,
    reader: SourceReader,
    vector: Any,
    sql: Any,
    principal: Principal,
    event: Callable[[str], None] = lambda _name: None,
) -> dict[str, Any]:
    if run_dir.exists():
        raise RecoveryError("run directory already exists")
    ids, raw_digest, semantic_digest = read_approved(approved)
    targets = []
    with principal_scope(principal):
        for ident in ids:
            _event(event, "source_read")
            source, pack_id, source_digest = checked_source(reader, ident)
            _event(event, "authorized")
            authorize(sql, principal, pack_id)
            _event(event, "vector_read")
            _row, vector_digest = checked_vector(vector, ident, pack_id)
            targets.append({
                "id": ident,
                "pack_id": pack_id,
                "source_digest": source_digest,
                "vector_digest": vector_digest,
                "state": "planned",
            })
    run_dir.mkdir(mode=0o700, parents=True)
    manifest = {
        "version": MANIFEST_VERSION,
        "approved_raw_digest": raw_digest,
        "approved_semantic_digest": semantic_digest,
        "approved_ids": ids,
        "principal_digest": _digest({"user_id": principal.user_id, "is_local": principal.is_local}),
        "vector_identity": vector_identity(vector),
        "targets": targets,
        "backup": None,
    }
    write_manifest(run_dir / "manifest.json", manifest)
    return manifest


def verify_current_target(vector: Any, ident: str, source: SourceRow, pack_id: str) -> dict[str, Any]:
    row, _digest_value = checked_vector(vector, ident, pack_id)
    metadata = _sanitize_metadata(source.metadata)
    if row.get("document") != source.text or _sanitize_metadata(row["metadata"]) != metadata:
        raise RecoveryError("vector readback does not match source target")
    verifier = getattr(vector, "verify_recovery_row", None)
    if not callable(verifier):
        raise RecoveryError("vector embedding verifier is unavailable")
    if not verifier(ident):
        raise RecoveryError("vector embedding representation is invalid")
    return row


def apply(
    approved: Path,
    run_dir: Path,
    reader: SourceReader,
    vector: Any,
    sql: Any,
    principal: Principal,
    backup_to: Path,
    backup_runner: Callable[[Path], dict[str, Any]],
    lock_factory: Callable[[], contextlib.AbstractContextManager[Any]],
    event: Callable[[str], None] = lambda _name: None,
) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    manifest = load_manifest(manifest_path)
    ids, raw_digest, semantic_digest = read_approved(approved)
    target_ids = [target.get("id") for target in manifest.get("targets", []) if isinstance(target, dict)]
    if (raw_digest != manifest.get("approved_raw_digest")
            or semantic_digest != manifest.get("approved_semantic_digest")
            or ids != manifest.get("approved_ids")
            or target_ids != ids
            or not ids):
        raise RecoveryError("approved file does not match dry-run manifest")
    if manifest.get("principal_digest") != _digest({"user_id": principal.user_id, "is_local": principal.is_local}):
        raise RecoveryError("principal does not match dry-run manifest")
    if manifest.get("vector_identity") != vector_identity(vector):
        raise RecoveryError("vector store does not match dry-run manifest")
    backup = backup_runner(backup_to)
    if not isinstance(backup, dict) or not backup.get("verified"):
        raise RecoveryError("backup did not verify")
    manifest["backup"] = backup
    write_manifest(manifest_path, manifest)
    _event(event, "backup_verified")
    with principal_scope(principal), lock_factory():
        _event(event, "lock_entered")
        if manifest.get("principal_digest") != _digest({"user_id": principal.user_id, "is_local": principal.is_local}):
            raise RecoveryError("principal changed before apply lock")
        if manifest.get("vector_identity") != vector_identity(vector):
            raise RecoveryError("vector store changed before apply lock")
        for target in manifest["targets"]:
            _event(event, "source_read")
            source, pack_id, source_digest = checked_source(reader, target["id"])
            _event(event, "authorized")
            authorize(sql, principal, pack_id)
            _event(event, "vector_read")
            _before, vector_digest = checked_vector(vector, target["id"], pack_id)
            if target["state"] == "verified":
                if source_digest != target["source_digest"]:
                    raise RecoveryError("verified source changed")
                verify_current_target(vector, target["id"], source, pack_id)
                continue
            if source_digest != target["source_digest"] or vector_digest != target["vector_digest"]:
                raise RecoveryError("approved source or vector changed after dry-run")
            target["state"] = "written"
            write_manifest(manifest_path, manifest)
            metadata = _sanitize_metadata(source.metadata)
            _event(event, "upsert")
            vector.upsert_texts(texts=[source.text], ids=[target["id"]], metadatas=[metadata])
            verify_current_target(vector, target["id"], source, pack_id)
            target["state"] = "verified"
            write_manifest(manifest_path, manifest)
    return manifest


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--approved", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-to", type=Path)
    args = parser.parse_args(argv)
    if args.apply and args.backup_to is None:
        parser.error("--apply requires --backup-to")
    from opencrab.config import get_settings
    from opencrab.stores.backup import verify_backup_set
    from opencrab.stores.factory import make_sql_store, make_vector_store

    settings = get_settings()
    principal = require_local_principal()
    reader = SourceReader(Path(settings.local_data_dir))
    sql = make_sql_store(settings)
    vector = make_vector_store(settings)
    try:
        if not args.apply:
            manifest = plan(args.approved, args.run_dir, reader, vector, sql, principal)
            print(json.dumps({"planned": len(manifest["targets"])}))
            return 0

        def backup_runner(dest: Path) -> dict[str, Any]:
            completed = subprocess.run(
                [
                    sys.executable, "-m", "opencrab.stores.backup",
                    "--data-dir", settings.local_data_dir, "--to", str(dest), "--json",
                ],
                check=False, capture_output=True, text=True,
            )
            if completed.returncode != 0:
                raise RecoveryError("online backup command failed")
            try:
                payload = json.loads(completed.stdout)
                set_dir = Path(payload["set_dir"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RecoveryError("online backup command returned invalid output") from exc
            report = verify_backup_set(set_dir)
            return {"verified": True, "set": str(set_dir), "report": report}

        manifest = apply(
            args.approved, args.run_dir, reader, vector, sql, principal, args.backup_to,
            backup_runner, lambda: write_lock(settings.local_data_dir, purpose="recover_source_vectors"),
        )
        print(json.dumps({"verified": sum(t["state"] == "verified" for t in manifest["targets"])}))
        return 0
    except RecoveryError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        for store in (vector, sql):
            close = getattr(store, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    raise SystemExit(_main())
