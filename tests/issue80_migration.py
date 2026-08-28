"""Disposable fixture and migration contracts for issue #80.

Production graph migration entry points are intentionally fail-closed.  This
module is the only test-side authority that can create legacy graph rows or a
temporary graph target.  The capability is scoped to a marked child below an
OS temporary directory and is consumed once per mutating helper call.
"""

from __future__ import annotations

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

    def close(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.exists():
                path.unlink()
        marker = self.root / ".issue80-fixture"
        if marker.exists():
            marker.unlink()
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
        nodes = [tuple(row) for row in conn.execute(
            "SELECT node_type,node_id,space_id,properties FROM graph_nodes ORDER BY node_id"
        )]
        edges = [tuple(row) for row in conn.execute(
            "SELECT from_type,from_id,relation,to_type,to_id,properties "
            "FROM graph_edges ORDER BY from_id,relation,to_id"
        )]
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
    if not path.exists():
        return {"tables": [], "indexes": [], "digest": hashlib.sha256(b"missing").hexdigest()}
    conn = sqlite3.connect(path)
    try:
        tables = [tuple(row) for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name LIKE 'graph_%' ORDER BY name"
        )]
        indexes = [tuple(row) for row in conn.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='index' AND tbl_name LIKE 'graph_%' ORDER BY name"
        )]
    finally:
        conn.close()
    payload = {"tables": tables, "indexes": indexes}
    return {**payload, "digest": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}
