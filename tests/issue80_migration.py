"""Disposable fixture and migration contracts for issue #80.

Production graph migration entry points are intentionally fail-closed.  This
module is the only test-side authority that can create legacy graph rows or a
temporary graph target.  The capability is scoped to a marked child below an
OS temporary directory and is consumed once per mutating helper call.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import (
    GraphMigrationFixtureOnlyError,
    canonical_edge_digest,
    canonical_json_bytes,
    canonical_node_digest,
)
from tests.helpers.issue80_graph_mutation import (
    FixtureMutationCapability,
    create_legacy_schema,
    seed_graph_rows,
)

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS graph_migration_receipts (
    request_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    mapping_fingerprint TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    target_fingerprint_before TEXT NOT NULL,
    target_fingerprint_after TEXT NOT NULL,
    edge_loss INTEGER NOT NULL,
    property_loss INTEGER NOT NULL,
    receipt_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL
)
"""

_LEDGER_COLUMNS = (
    # SQLite reports NOT NULL=0 for a column declared ``TEXT PRIMARY KEY``;
    # the primary-key flag remains the identity constraint.
    ("request_id", "TEXT", 0, 1),
    ("phase", "TEXT", 1, 0),
    ("request_digest", "TEXT", 1, 0),
    ("source_fingerprint", "TEXT", 1, 0),
    ("mapping_fingerprint", "TEXT", 1, 0),
    ("plan_sha256", "TEXT", 1, 0),
    ("target_fingerprint_before", "TEXT", 1, 0),
    ("target_fingerprint_after", "TEXT", 1, 0),
    ("edge_loss", "INTEGER", 1, 0),
    ("property_loss", "INTEGER", 1, 0),
    ("receipt_bytes", "BLOB", 1, 0),
    ("created_at", "TEXT", 1, 0),
)


def _typed_property(value: Any) -> dict[str, Any]:
    """Make SQLite text and bytes comparable without decoding invalid bytes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "kind": "bytes",
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, str):
        return {
            "kind": "text",
            "base64": base64.b64encode(value.encode("utf-8", errors="surrogatepass")).decode("ascii"),
        }
    return {"kind": "json", "value": value}


@dataclass
class FixtureHandle:
    """A disposable SQLite graph fixture with an explicit mutation lease."""

    root: Path
    db_path: Path

    @classmethod
    def create(cls) -> FixtureHandle:
        root = Path(tempfile.mkdtemp(prefix="issue80-fixture-"))
        capability = FixtureMutationCapability.create(root)
        capability.consume(root / "graph.db")
        return cls(root=root, db_path=root / "graph.db")

    def lease(self) -> FixtureMutationCapability:
        return FixtureMutationCapability.from_root(self.root)

    def create_legacy(self) -> Path:
        return create_legacy_schema(self.db_path, capability=self.lease())

    def seed(self, nodes: Iterable[Any] = (), edges: Iterable[Any] = ()) -> Path:
        return seed_graph_rows(self.db_path, nodes, edges, capability=self.lease())

    def create_receipt_insert_abort_trigger(self) -> Path:
        """Install the production ledger schema and a fixture-only abort seam."""
        self.lease().consume(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_LEDGER_DDL)
            columns = tuple(
                (str(name), str(declared_type).upper(), int(not_null), int(primary_key))
                for _ordinal, name, declared_type, not_null, _default, primary_key in conn.execute(
                    "PRAGMA table_info(graph_migration_receipts)"
                )
            )
            expected = tuple((name, declared_type, not_null, primary_key) for name, declared_type, not_null, primary_key in _LEDGER_COLUMNS)
            if columns != expected:
                raise AssertionError("fixture ledger schema does not match production DDL")
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS issue80_fail_receipt_insert
                BEFORE INSERT ON graph_migration_receipts
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'issue80 injected receipt failure');
                END
                """
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.db_path

    def drop_receipt_insert_abort_trigger(self) -> Path:
        """Remove exactly the fixture trigger through a fresh mutation lease."""
        self.lease().consume(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP TRIGGER IF EXISTS issue80_fail_receipt_insert")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.db_path

    def drop_required_graph_index(self, name: str = "idx_nodes_pack") -> Path:
        """Corrupt one known graph-owned index in this disposable fixture only."""
        if name not in {"idx_nodes_pack", "idx_nodes_space", "idx_edges_from", "idx_edges_to"}:
            raise ValueError("unknown issue80 graph index")
        self.lease().consume(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f'DROP INDEX "{name}"')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.db_path

    def close(self) -> None:
        """Release the fixture's files and remove its directory.

        전제(codex 설계검증, issue #141): 이 정리는 "다른 프로세스가 이
        디렉터리의 write.lock 을 쥐고 있지 않다"는 것을 스스로 증명하지
        않는다. 이 메서드를 부르는 모든 기존 경로가 같은 스레드/프로세스
        안에서 스토어를 먼저 닫은 뒤(close()) 동기적으로 이 메서드를
        호출한다는 호출 관례에 기대어서만 안전하다. 이 관례를 벗어난 새
        사용(다른 프로세스/스레드가 같은 디렉터리에 동시 접근)에는 이
        보장이 미치지 않는다.
        """
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()
        marker = self.root / ".issue80-fixture"
        if marker.exists():
            marker.unlink()
        # issue #141 항목 2(회귀 게이트에서 발견): 이 root 아래 SQLite 스토어를
        # 열면 부트스트랩이 write.lock 을 만든다. flock 파일은 설계상 삭제하지
        # 않으므로(opencrab.locking) 남아 있는 게 정상이고, 지우지 않으면
        # rmdir 이 "Directory not empty" 로 깨진다.
        lock_path = self.root / "write.lock"
        if lock_path.exists():
            lock_path.unlink()
        self.root.rmdir()

    def __enter__(self) -> FixtureHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def graph_snapshot(db_path: str | Path) -> dict[str, Any]:
    """Return a typed, deterministic source snapshot for migration tests."""
    path = Path(db_path)
    if not path.exists():
        return {"nodes": [], "edges": [], "digest": hashlib.sha256(b"empty").hexdigest()}
    conn = sqlite3.connect(path)
    try:
        nodes = [
            {
                "node_type": row[0],
                "node_id": row[1],
                "space_id": row[2],
                "properties": _typed_property(row[3]),
            }
            for row in conn.execute(
                "SELECT node_type,node_id,space_id,properties FROM graph_nodes "
                "ORDER BY node_type,node_id,space_id"
            )
        ]
        edges = [
            {
                "from_type": row[0],
                "from_id": row[1],
                "relation": row[2],
                "to_type": row[3],
                "to_id": row[4],
                "properties": _typed_property(row[5]),
            }
            for row in conn.execute(
                "SELECT from_type,from_id,relation,to_type,to_id,properties "
                "FROM graph_edges ORDER BY from_type,from_id,relation,to_type,to_id"
            )
        ]
    finally:
        conn.close()
    payload = {"nodes": nodes, "edges": edges}
    return {
        **payload,
        "digest": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }


def target_graph_digest(store: Any) -> str:
    """Return the canonical graph fingerprint exposed by an operational store."""
    return store.graph_fingerprint()


def apply_fixture_only(*_args: Any, **_kwargs: Any) -> None:
    """A named test helper whose apply authority is deliberately explicit."""
    raise GraphMigrationFixtureOnlyError("graph apply is fixture-only")


def node_digest(node_type: str, node_id: str, properties: dict[str, Any], space_id: str | None = None) -> str:
    props = dict(properties)
    props["id"] = node_id
    if space_id is not None:
        props["space"] = space_id
    return canonical_node_digest(node_type, space_id, props)


def edge_digest(
    from_id: str,
    relation: str,
    to_id: str,
    from_type: str,
    to_type: str,
    properties: dict[str, Any] | None = None,
) -> str:
    props = dict(properties or {})
    props.update({"from_id": from_id, "relation": relation, "to_id": to_id})
    return canonical_edge_digest(from_id, relation, to_id, from_type, to_type, props)


def read_schema_state(db_path: str | Path) -> dict[str, Any]:
    """Read graph-owned SQLite catalog metadata without opening a writer."""
    path = Path(db_path)
    return schema_snapshot(path)


def schema_snapshot(db_path: str | Path) -> dict[str, Any]:
    """Read graph catalog, ledger rows and residue through a read-only handle."""
    path = Path(db_path)
    if not path.exists():
        payload = {"tables": [], "indexes": [], "triggers": [], "views": [], "ledger": [], "residue": []}
        return {**payload, "digest": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}
    conn = sqlite3.connect(path)
    try:
        objects = [tuple(row) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger','view') ORDER BY type,name"
        )]
        tables = [row for row in objects if row[0] == "table" and (str(row[1]).startswith("graph_") or str(row[1]).startswith("issue80_"))]
        indexes = [row for row in objects if row[0] == "index" and str(row[2]).startswith("graph_")]
        triggers = [row for row in objects if row[0] == "trigger" and (str(row[2]).startswith("graph_") or str(row[1]).startswith("issue80_"))]
        views = [row for row in objects if row[0] == "view" and (str(row[1]).startswith("graph_") or str(row[1]).startswith("issue80_"))]
        residue = [row for row in tables if str(row[1]).startswith(("graph_nodes_", "graph_edges_"))]
        ledger = []
        if any(row[1] == "graph_migration_receipts" for row in tables):
            ledger = [
                [*row[:-1], _typed_property(row[-1])]
                for row in conn.execute(
                    "SELECT request_id,phase,request_digest,source_fingerprint,"
                    "mapping_fingerprint,plan_sha256,target_fingerprint_before,"
                    "target_fingerprint_after,edge_loss,property_loss,receipt_bytes "
                    "FROM graph_migration_receipts ORDER BY request_id"
                )
            ]
    finally:
        conn.close()
    payload = {"tables": tables, "indexes": indexes, "triggers": triggers, "views": views, "ledger": ledger, "residue": residue}
    return {**payload, "digest": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}
