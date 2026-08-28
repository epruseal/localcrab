"""Disposable SQLite graph fixtures for issue #80 qualification tests.

The production migration entry points intentionally have no apply path while
the optional Ladybug writer is unqualified.  Tests that need malformed or
legacy rows therefore use this module, never a production data directory or
an external graph service.

Every mutating helper requires a root below the operating system temporary
directory and the exact ``.issue80-fixture`` marker.  A
``FixtureMutationCapability`` is a one-use lease; callers can issue another
lease from the same marker after a completed operation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER_NAME = ".issue80-fixture"
MARKER_CONTENT = "opencrab.issue80.fixture.v1\n"
GRAPH_DB_NAME = "graph.db"


def _trusted_temp_roots() -> tuple[Path, ...]:
    roots = {Path(tempfile.gettempdir()).resolve()}
    # macOS commonly exposes /var/folders as tempfile.gettempdir() while the
    # workspace runner uses /private/tmp.  Both are OS temporary roots; this
    # explicit second root does not broaden the check to an arbitrary path.
    for raw in ("/tmp", "/private/tmp"):
        path = Path(raw)
        if path.is_dir():
            roots.add(path.resolve())
    return tuple(sorted(roots, key=lambda item: len(item.parts), reverse=True))


def _is_below(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _validate_root(root: str | os.PathLike[str], *, marker: bool = True) -> Path:
    raw = Path(root)
    if not raw.is_absolute():
        raise ValueError("issue80 fixture root must be absolute")
    if raw.is_symlink():
        raise ValueError("issue80 fixture root must not be a symlink")
    path = raw.resolve(strict=True)
    if not path.is_dir() or not any(_is_below(path, base) for base in _trusted_temp_roots()):
        raise ValueError("issue80 fixture root must be an existing temporary directory")
    marker_path = path / MARKER_NAME
    if marker:
        if marker_path.is_symlink() or not marker_path.is_file():
            raise PermissionError("issue80 fixture marker is missing")
        if marker_path.read_text(encoding="utf-8") != MARKER_CONTENT:
            raise PermissionError("issue80 fixture marker is invalid")
    return path


@dataclass
class FixtureMutationCapability:
    """One-use authority for one mutation in a disposable fixture root."""

    root: Path
    _used: bool = False

    def __post_init__(self) -> None:
        self.root = _validate_root(self.root)

    @classmethod
    def create(cls, root: str | os.PathLike[str] | None = None) -> FixtureMutationCapability:
        if root is None:
            root_path = Path(tempfile.mkdtemp(prefix="issue80-graph-fixture-"))
        else:
            root_path = Path(root)
            if not root_path.is_absolute():
                raise ValueError("issue80 fixture root must be absolute")
            if root_path.is_symlink() or not root_path.is_dir():
                raise ValueError("issue80 fixture root must be an existing temporary directory")
            _validate_root(root_path, marker=False)
        marker = root_path / MARKER_NAME
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != MARKER_CONTENT:
                raise PermissionError("issue80 fixture marker is invalid")
        else:
            marker.write_text(MARKER_CONTENT, encoding="utf-8")
        return cls(root_path)

    @classmethod
    def from_root(cls, root: str | os.PathLike[str]) -> FixtureMutationCapability:
        """Issue a fresh one-use lease from an already marked fixture root."""
        return cls(Path(root))

    def consume(self, target: str | os.PathLike[str] | None = None) -> Path:
        """Consume this lease and verify that the target remains in its root."""
        if self._used:
            raise RuntimeError("issue80 fixture mutation capability was already used")
        root = _validate_root(self.root)
        if target is not None:
            target_path = Path(target)
            if not target_path.is_absolute():
                raise ValueError("issue80 fixture target must be absolute")
            resolved = target_path.resolve(strict=False)
            if not _is_below(resolved, root):
                raise PermissionError("issue80 fixture target escapes its root")
        self._used = True
        return root


@dataclass
class FixtureStoreGraph:
    """Delegating facade returned by :func:`setup_fake_store_graph`."""

    root: Path
    db_path: Path
    store: Any
    capability: FixtureMutationCapability

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def close(self) -> None:
        self.store.close()

    def __iter__(self):
        # Accommodates callers that want ``store, capability = setup...``
        # while keeping the normal return value a useful store facade.
        yield self.store
        yield self.capability


def _root_for_target(target: Any, capability: FixtureMutationCapability | None) -> tuple[Path, Path]:
    if isinstance(target, FixtureStoreGraph):
        root, db_path = target.root, target.db_path
    elif hasattr(target, "_db_path"):
        db_path = Path(target._db_path)
        root = db_path.parent
    else:
        path = Path(target)
        if path.suffix == ".db":
            db_path, root = path, path.parent
        else:
            root, db_path = path, path / GRAPH_DB_NAME
    expected_root = capability.root if capability is not None else root
    checked = _validate_root(expected_root)
    db_path = db_path.resolve(strict=False)
    if db_path.parent != checked or db_path.name != GRAPH_DB_NAME:
        raise PermissionError("issue80 fixture graph path is outside its root")
    return checked, db_path


def _consume(target: Any, capability: FixtureMutationCapability | None) -> tuple[Path, Path]:
    root, db_path = _root_for_target(target, capability)
    if capability is None:
        # A valid marker is an explicit fixture authorization.  Use a fresh
        # lease for this one helper call so independent helpers do not share
        # mutable capability state accidentally.
        capability = FixtureMutationCapability.from_root(root)
    capability.consume(db_path)
    return root, db_path


def _remove_graph_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db_path}{suffix}")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise PermissionError("issue80 fixture graph sidecar is not a regular file")
            path.unlink()


def reset_graph(
    target: Any,
    *,
    capability: FixtureMutationCapability | None = None,
) -> Path:
    """Remove only the known graph fixture files and return the DB path."""
    _root, db_path = _consume(target, capability)
    if isinstance(target, FixtureStoreGraph) and target.store is not None:
        target.close()
    _remove_graph_files(db_path)
    return db_path


def create_legacy_schema(
    target: Any,
    *,
    capability: FixtureMutationCapability | None = None,
) -> Path:
    """Create the pre-issue80 composite-key SQLite graph schema."""
    root, db_path = _consume(target, capability)
    del root
    _remove_graph_files(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE graph_nodes (
                node_type TEXT NOT NULL,
                node_id TEXT NOT NULL,
                space_id TEXT,
                properties TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (node_type, node_id)
            );
            CREATE TABLE graph_edges (
                from_type TEXT NOT NULL,
                from_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                to_type TEXT NOT NULL,
                to_id TEXT NOT NULL,
                properties TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (from_type, from_id, relation, to_type, to_id)
            );
            CREATE INDEX idx_nodes_pack ON graph_nodes(json_extract(properties, '$.pack_id'));
            CREATE INDEX idx_nodes_space ON graph_nodes(space_id);
            CREATE INDEX idx_edges_from ON graph_edges(from_id);
            CREATE INDEX idx_edges_to ON graph_edges(to_id);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _node_row(item: Any) -> tuple[Any, ...]:
    if isinstance(item, dict):
        return (
            item["node_type"], item["node_id"], item.get("space_id"),
            item.get("properties", {}),
        )
    if len(item) == 3:
        return item[0], item[1], None, item[2]
    return tuple(item)


def _edge_row(item: Any) -> tuple[Any, ...]:
    if isinstance(item, dict):
        return (
            item["from_type"], item["from_id"], item["relation"],
            item["to_type"], item["to_id"], item.get("properties", {}),
        )
    return tuple(item)


def _stored_properties(value: Any) -> str | bytes:
    """Keep raw SQLite bytes raw while encoding structured fixture values."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def seed_graph_rows(
    target: Any,
    nodes: Iterable[Any] = (),
    edges: Iterable[Any] = (),
    *,
    capability: FixtureMutationCapability | None = None,
) -> Path:
    """Insert raw graph rows into a marked fixture database.

    Raw JSON strings are accepted intentionally so corruption and duplicate
    key cases can be reproduced without weakening production validation.
    """
    _root, db_path = _consume(target, capability)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO graph_nodes (node_type,node_id,space_id,properties) VALUES (?,?,?,?)",
            [
                (node_type, node_id, space_id, _stored_properties(props))
                for node_type, node_id, space_id, props in (_node_row(item) for item in nodes)
            ],
        )
        conn.executemany(
            "INSERT INTO graph_edges (from_type,from_id,relation,to_type,to_id,properties) VALUES (?,?,?,?,?,?)",
            [
                (from_type, from_id, relation, to_type, to_id, _stored_properties(props))
                for from_type, from_id, relation, to_type, to_id, props in (_edge_row(item) for item in edges)
            ],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return db_path


def inject_graph_corruption(
    target: Any,
    kind: str,
    *,
    capability: FixtureMutationCapability | None = None,
    node_id: str = "fixture-node",
    relation: str = "fixture-rel",
) -> Path:
    """Inject one named malformed graph condition into a fixture DB."""
    _root, db_path = _consume(target, capability)
    conn = sqlite3.connect(db_path)
    try:
        if kind == "malformed_node_properties":
            conn.execute("UPDATE graph_nodes SET properties=? WHERE node_id=?", ("{", node_id))
        elif kind == "malformed_edge_properties":
            conn.execute(
                "UPDATE graph_edges SET properties=? WHERE from_id=? AND relation=?",
                ("{", node_id, relation),
            )
        elif kind == "dangling_edge":
            conn.execute(
                "INSERT INTO graph_edges (from_type,from_id,relation,to_type,to_id,properties) VALUES (?,?,?,?,?,?)",
                ("Fixture", node_id, relation, "Fixture", "missing-endpoint", "{}"),
            )
        elif kind == "duplicate_node":
            conn.execute(
                "INSERT INTO graph_nodes (node_type,node_id,space_id,properties) VALUES (?,?,?,?)",
                ("OtherFixture", node_id, None, "{}"),
            )
        else:
            raise ValueError(f"unknown issue80 graph corruption kind: {kind}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return db_path


def setup_fake_store_graph(
    root: str | os.PathLike[str] | None = None,
    *,
    nodes: Iterable[Any] = (),
    edges: Iterable[Any] = (),
    reset: bool = True,
) -> FixtureStoreGraph:
    """Create a marked, disposable LocalGraphStore facade for tests."""
    capability = FixtureMutationCapability.create(root)
    root_path = capability.root
    db_path = root_path / GRAPH_DB_NAME
    if reset:
        reset_graph(root_path, capability=capability)
        # reset consumed the initial lease.  A fresh lease is required for
        # seeding, and the facade stores it for callers that need raw setup.
        capability = FixtureMutationCapability.from_root(root_path)
    from opencrab.stores.local_graph_store import LocalGraphStore

    store = LocalGraphStore(str(db_path))
    facade = FixtureStoreGraph(root_path, db_path, store, capability)
    if nodes or edges:
        seed_graph_rows(facade, nodes, edges, capability=capability)
        facade.capability = FixtureMutationCapability.from_root(root_path)
    return facade
