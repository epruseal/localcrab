"""#493 heartbeat 정리 도구의 fixture 전용 안전 경계 시험이다."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import cleanup_openclaw_heartbeat_493 as cleanup  # noqa: E402

TABLE = "vectors_test"
HEARTBEAT = "Q: [OpenClaw heartbeat poll] tick"


def _db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _schema(root: Path) -> None:
    with _db(root / "doc_store.db") as conn:
        conn.executescript(
            "CREATE TABLE doc_sources (source_id TEXT PRIMARY KEY, text TEXT, metadata TEXT);"
            "CREATE VIRTUAL TABLE doc_sources_fts USING fts5(source_id UNINDEXED, text);"
            "CREATE TABLE doc_nodes (space TEXT, node_id TEXT, node_type TEXT, properties TEXT, PRIMARY KEY(space,node_id));"
        )
    with _db(root / "graph.db") as conn:
        conn.executescript(
            "CREATE TABLE graph_nodes (node_type TEXT, node_id TEXT PRIMARY KEY, space_id TEXT, properties TEXT);"
            "CREATE TABLE graph_edges (from_type TEXT, from_id TEXT, relation TEXT, to_type TEXT, to_id TEXT, properties TEXT, PRIMARY KEY(from_id,relation,to_id));"
        )
    with _db(root / "opencrab.db") as conn:
        conn.executescript(
            "CREATE TABLE ontology_nodes (id INTEGER PRIMARY KEY, space TEXT, node_type TEXT, node_id TEXT);"
            "CREATE TABLE ontology_edges (id INTEGER PRIMARY KEY, from_space TEXT, from_id TEXT, relation TEXT, to_space TEXT, to_id TEXT);"
        )
    import sqlite_vec

    with _db(root / "vectors.db") as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute(
            f"CREATE VIRTUAL TABLE {TABLE} USING vec0(node_id TEXT PRIMARY KEY, pack_id TEXT partition key, embedding float[2], +document TEXT, +metadata TEXT)"
        )


def _put(conn: sqlite3.Connection, sql: str, values: tuple[object, ...]) -> None:
    conn.execute(sql, values)


def _add(root: Path, node_id: str, text: str, pack: object = "openclaw", *, source: bool = True, fts: bool = True, doc: bool = True, graph: bool = True, vector: bool = True, node_type: str = "Evidence", space: str = "evidence") -> None:
    metadata = json.dumps({"pack_id": pack, "node_id": node_id})
    props = json.dumps({"pack_id": pack})
    if source or fts or doc:
        with _db(root / "doc_store.db") as conn:
            if source:
                _put(conn, "INSERT INTO doc_sources VALUES (?,?,?)", (node_id, text, metadata))
            if fts:
                _put(conn, "INSERT INTO doc_sources_fts VALUES (?,?)", (node_id, text))
            if doc:
                _put(conn, "INSERT INTO doc_nodes VALUES (?,?,?,?)", (space, node_id, node_type, props))
    if graph:
        with _db(root / "graph.db") as conn:
            _put(conn, "INSERT INTO graph_nodes VALUES (?,?,?,?)", (node_type, node_id, space, props))
    if vector:
        import sqlite_vec

        with _db(root / "vectors.db") as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            _put(conn, f"INSERT INTO {TABLE}(node_id,pack_id,embedding,document,metadata) VALUES (?,?,?,?,?)", (node_id, pack, sqlite_vec.serialize_float32([0.0, 1.0]), text, metadata))


@pytest.fixture
def world(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    _schema(root)
    _add(root, "evidence:turn:heartbeat", HEARTBEAT)
    _add(root, "evidence:turn:normal", "discussion includes OpenClaw heartbeat poll later")
    _add(root, "evidence:turn:fallback", HEARTBEAT, pack="other")
    with _db(root / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("evidence", "Evidence", "evidence:turn:heartbeat"))
    return root


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_dry_run_selects_exact_prefix_and_strict_pack_without_writes(world: Path, tmp_path: Path):
    before = {path.name: path.read_bytes() for path in world.iterdir()}
    result = cleanup.dry_run(world, tmp_path / "run", TABLE)
    assert result["eligible"] is True
    lines = (tmp_path / "run" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["evidence:turn:heartbeat"]
    assert {path.name: path.read_bytes() for path in world.iterdir()} == before
    assert _load(tmp_path / "run" / "report.json")["registry"]["mode"] == "report-only"


@pytest.mark.parametrize("mutate", ["missing-fts", "wrong-space", "mixed-vector", "document-id"])
def test_dry_run_refuses_incomplete_or_wrong_ownership_axes(world: Path, tmp_path: Path, mutate: str):
    node_id = "evidence:turn:heartbeat"
    if mutate == "missing-fts":
        with _db(world / "doc_store.db") as conn:
            conn.execute("DELETE FROM doc_sources_fts WHERE source_id=?", (node_id,))
    elif mutate == "wrong-space":
        with _db(world / "doc_store.db") as conn:
            conn.execute("UPDATE doc_nodes SET space='concept' WHERE node_id=?", (node_id,))
    elif mutate == "mixed-vector":
        import sqlite_vec

        with _db(world / "vectors.db") as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            row = conn.execute(f"SELECT node_id,embedding,document,metadata FROM {TABLE} WHERE node_id=?", (node_id,)).fetchone()
            conn.execute(f"DELETE FROM {TABLE} WHERE node_id=?", (node_id,))
            conn.execute(f"INSERT INTO {TABLE}(node_id,pack_id,embedding,document,metadata) VALUES (?,?,?,?,?)", (row[0], "other", row[1], row[2], row[3]))
    else:
        with _db(world / "doc_store.db") as conn:
            conn.execute("UPDATE doc_sources SET source_id='evidence:turn:Document:wrong' WHERE source_id=?", (node_id,))
            conn.execute("UPDATE doc_sources_fts SET source_id='evidence:turn:Document:wrong' WHERE source_id=?", (node_id,))
    result = cleanup.dry_run(world, tmp_path / mutate, TABLE)
    assert result["eligible"] is False
    assert not (tmp_path / mutate / "manifest.json").exists()


def test_apply_requires_exact_approval_and_verified_backup(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    with pytest.raises(cleanup.CleanupRefusedError, match="approved-manifest"):
        cleanup.apply(world, run, TABLE, None, tmp_path / "backups", scan=lambda _: ([], 0))
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": "wrong", "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="approval"):
        cleanup.apply(world, run, TABLE, approval, tmp_path / "backups", scan=lambda _: ([], 0))


def test_apply_deletes_only_candidate_axes_and_preserves_normal_conversation(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backups", scan=lambda _: ([], 0))
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT source_id FROM doc_sources ORDER BY source_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
        assert conn.execute("SELECT source_id FROM doc_sources_fts ORDER BY source_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
        assert conn.execute("SELECT node_id FROM doc_nodes ORDER BY node_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes ORDER BY node_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert conn.execute(f"SELECT node_id FROM {TABLE} ORDER BY node_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
    assert (run / "receipt.json").is_file()
    assert (run / "progress.jsonl").is_file()


def test_last_claim_support_blocks_manifest(world: Path, tmp_path: Path):
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:one", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", "evidence:turn:heartbeat", "supports", "Claim", "claim:one", "{}"))
    result = cleanup.dry_run(world, tmp_path / "run", TABLE)
    assert result["eligible"] is False
    assert "final Claim support" in result["errors"][0]


def test_nonempty_wal_refuses_before_immutable_read(world: Path, tmp_path: Path):
    wal = world / "graph.db-wal"
    wal.write_bytes(b"not empty")
    with pytest.raises(cleanup.CleanupRefusedError, match="not empty"):
        cleanup.dry_run(world, tmp_path / "run", TABLE)


def test_marker_uses_exclusive_temporary_and_no_replace(world: Path, tmp_path: Path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "files": {}}
    payload = cleanup._marker_payload("tombstone", receipt)
    cleanup.publish_marker(run, "rollback-tombstone.json", payload)
    assert cleanup.marker_state(run)[0] == "complete tombstone"
    with pytest.raises(FileExistsError):
        cleanup.publish_marker(run, "rollback-tombstone.json", payload)
    assert cleanup.marker_state(run)[0] == "complete tombstone"


def test_incomplete_marker_fails_closed_for_apply_and_rollback(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    (run / ".rollback-tombstone.json.tmp").write_text("partial", encoding="utf-8")
    assert cleanup.marker_state(run)[0] == "incomplete artifact"
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="marker state"):
        cleanup.apply(world, run, TABLE, approval, tmp_path / "backups", scan=lambda _: ([], 0))
    with pytest.raises(cleanup.CleanupRefusedError, match="backup receipt"):
        cleanup.rollback(world, run, tmp_path / "backups")


def test_publish_marker_preserves_evidence_after_directory_fsync_failure(tmp_path: Path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "files": {}}
    monkeypatch.setattr(cleanup, "_fsync_dir", lambda _: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError, match="fsync"):
        cleanup.publish_marker(run, "rollback-tombstone.json", cleanup._marker_payload("tombstone", receipt))
    assert (run / "rollback-tombstone.json").is_file()
    assert cleanup.marker_state(run)[0] == "complete tombstone"
