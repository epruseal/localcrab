"""#493 heartbeat 정리 도구의 fixture 전용 안전 경계 시험이다."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
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


_OBSERVER_FIELDS = {"source", "kind", "action", "arg1", "arg2", "raw"}
_OBSERVER_FORBIDDEN = {
    getattr(sqlite3, name)
    for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE", "SQLITE_ALTER_TABLE", "SQLITE_ATTACH", "SQLITE_DETACH",
    )
}


def _assert_observer_events(events: list[dict], *, production: bool) -> None:
    if not events:
        raise AssertionError("pragma event missing" if not production else "observer event missing")
    if not all(set(event) == _OBSERVER_FIELDS for event in events):
        raise AssertionError("observer event fields")
    if not all(isinstance(event["source"], str) and isinstance(event["kind"], str) for event in events):
        raise AssertionError("observer event types")
    writes = [event for event in events if event["kind"] == "authorizer" and event["action"] in _OBSERVER_FORBIDDEN]
    if writes:
        raise AssertionError("forbidden action")
    if production:
        if any(event["source"] == "observer-unit" for event in events):
            raise AssertionError("observer unit source")
        if not any(event["kind"] == "trace" and event["action"] == "SELECT" for event in events):
            raise AssertionError("registry select missing")
        return
    pragmas = [event for event in events if event["kind"] == "authorizer" and event["action"] == sqlite3.SQLITE_PRAGMA]
    if not pragmas:
        raise AssertionError("pragma event missing")
    if not any(event["kind"] == "trace" and event["action"] == "SELECT" for event in events):
        raise AssertionError("normal read missing")
    for event in pragmas:
        name, arg = event["arg1"], event["arg2"]
        if name == "query_only":
            if arg not in {None, ""}:
                raise AssertionError("query_only argument")
        elif name in {"table_info", "foreign_key_list"}:
            if arg not in {"ontology_nodes", "ontology_edges"}:
                raise AssertionError("pragma table")
        else:
            raise AssertionError("pragma name")


def _cli_main(tmp_path: Path, name: str, args: list[str], setup: str = "") -> subprocess.CompletedProcess[str]:
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{name}.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        + setup
        + f"raise SystemExit(cleanup.main({args!r}))\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(runner)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _cli_rollback_state(world: Path, tmp_path: Path) -> tuple[Path, Path, list[str]]:
    run, backup = tmp_path / "run", tmp_path / "backup"
    base = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]
    dry = _cli_main(tmp_path, "dry", base)
    assert dry.returncode == 0, dry.stderr
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    applied = _cli_main(tmp_path, "apply", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)])
    assert applied.returncode == 0, applied.stderr
    prepared = _cli_main(tmp_path, "prepare", [*base, "--prepare-rollback", "--backup-dir", str(backup)])
    assert prepared.returncode == 0, prepared.stderr
    return run, backup, [*base, "--rollback", "--backup-dir", str(backup)]


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
    assert _load(run / "registry-after.json")["mode"] == "report-only"


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
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": {"path": "p", "device": 1, "inode": 2}, "files": {}}
    payload = cleanup._marker_payload("tombstone", receipt)
    cleanup.publish_marker(run, "rollback-tombstone.json", payload)
    assert cleanup.marker_state(run)[0] == "complete tombstone"
    with pytest.raises(FileExistsError):
        cleanup.publish_marker(run, "rollback-tombstone.json", payload)
    assert cleanup.marker_state(run)[0] == "complete tombstone"


def test_cli_apply_rechecks_marker_inside_lock_and_mutation_is_red(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    before = cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False)
    trace = tmp_path / "marker-recheck.trace"
    marker = run / ".rollback-tombstone.json.tmp"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    setup = (
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original_marker = cleanup.marker_state\n"
        "original_rw = cleanup._rw\n"
        "calls = 0\n"
        "def rw(path):\n"
        "    conn = original_rw(path)\n"
        "    conn.set_trace_callback(lambda sql: trace.write('SQL:' + path.name + ':' + sql + '\\n'))\n"
        "    return conn\n"
        "def marker_state(path):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    result = original_marker(path)\n"
        "    trace.write('MARKER:' + str(calls) + ':' + result[0] + '\\n')\n"
        "    trace.flush()\n"
        "    if calls == 1:\n"
        "        (path / '.rollback-tombstone.json.tmp').write_bytes(b'fixture-marker')\n"
        "    return result\n"
        "cleanup._rw = rw\n"
        "cleanup.marker_state = marker_state\n"
    )
    result = _cli_main(tmp_path, "marker-recheck", args, setup)
    assert result.returncode != 0
    assert "marker state: incomplete artifact" in result.stderr
    lines = trace.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["MARKER:1:absent", "MARKER:2:incomplete artifact"]
    assert marker.read_bytes() == b"fixture-marker"
    assert cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False) == before
    assert not any(line.startswith("SQL:") and any(word in line.upper() for word in ("INSERT", "UPDATE", "DELETE")) for line in lines)
    assert not (run / "receipt.json").exists()
    assert not backup.exists()


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
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": {"path": "p", "device": 1, "inode": 2}, "files": {}}
    monkeypatch.setattr(cleanup, "_fsync_dir", lambda _: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError, match="fsync"):
        cleanup.publish_marker(run, "rollback-tombstone.json", cleanup._marker_payload("tombstone", receipt))
    assert (run / "rollback-tombstone.json").is_file()
    assert cleanup.marker_state(run)[0] == "complete tombstone"


def test_restore_attempt_keeps_partial_artifact_and_uses_unique_replacement(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    source = tmp_path / "backup.db"
    destination = data / "graph.db"
    source.write_bytes(b"backup")
    destination.write_bytes(b"old")
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    old_artifact = data / ".graph.db.restore-old"
    old_artifact.write_bytes(b"partial")
    successor = cleanup._publish_attempt_successor(run, receipt)
    attempt_dir = run / "restore-attempts" / successor["directory"]
    cleanup._restore_file(source, destination, attempt_dir, receipt)
    assert old_artifact.read_bytes() == b"partial"
    assert destination.read_bytes() == b"backup"
    metadata = attempt_dir / "graph.db.json"
    assert _load(metadata)["file"] == "graph.db"
    assert _load(metadata)["attempt"] == successor["attempt"]


def test_backup_receipt_fsyncs_files_and_directories_before_receipt(world: Path, tmp_path: Path, monkeypatch):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    calls: list[Path] = []
    original_file, original_dir = cleanup._fsync_file, cleanup._fsync_dir
    monkeypatch.setattr(cleanup, "_fsync_file", lambda path: (calls.append(path), original_file(path))[1])
    monkeypatch.setattr(cleanup, "_fsync_dir", lambda path: (calls.append(path), original_dir(path))[1])
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    assert set(backup / name for name in cleanup.DB_FILES) <= set(calls)
    assert backup in calls
    assert backup.parent in calls
    assert (run / "receipt.json").is_file()


def test_cli_backup_parent_fsync_failure_preserves_evidence(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    before = cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False)
    trace = tmp_path / "backup-fsync.trace"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    setup = (
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original_fsync = cleanup._fsync_file\n"
        "original_rw = cleanup._rw\n"
        "def rw(path):\n"
        "    conn = original_rw(path)\n"
        "    conn.set_trace_callback(lambda sql: trace.write('SQL:' + path.name + ':' + sql + '\\n'))\n"
        "    return conn\n"
        "def fsync(path):\n"
        "    trace.write('FSYNC:' + str(path) + '\\n')\n"
        "    trace.flush()\n"
        "    if path == cleanup.Path(" + repr(str(backup.parent)) + "):\n"
        "        trace.write('PARENT-FAULT\\n')\n"
        "        trace.flush()\n"
        "        raise OSError('backup parent fsync fault')\n"
        "    return original_fsync(path)\n"
        "cleanup._rw = rw\n"
        "cleanup._fsync_file = fsync\n"
    )
    result = _cli_main(tmp_path, "backup-parent-fsync", args, setup)
    assert result.returncode != 0
    assert "backup parent fsync fault" in result.stderr
    lines = trace.read_text(encoding="utf-8").splitlines()
    file_indexes = [lines.index(f"FSYNC:{backup / name}") for name in cleanup.DB_FILES]
    assert max(file_indexes) < lines.index(f"FSYNC:{backup}") < lines.index(f"FSYNC:{backup.parent}") < lines.index("PARENT-FAULT")
    assert backup.exists() and all((backup / name).is_file() for name in cleanup.DB_FILES)
    assert not (run / "receipt.json").exists()
    assert cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False) == before
    assert not any(line.startswith("SQL:") and any(word in line.upper() for word in ("INSERT", "UPDATE", "DELETE")) for line in lines)


def test_first_apply_refuses_changed_snapshot_before_backup(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "doc_store.db") as conn:
        conn.execute("DELETE FROM doc_sources_fts WHERE source_id='evidence:turn:heartbeat'")
    backup = tmp_path / "backup"
    with pytest.raises(cleanup.CleanupRefusedError, match="full frozen snapshot"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=lambda _: ([], 0))
    assert not backup.exists()
    assert not (run / "receipt.json").exists()


def test_receipt_rejects_other_data_directory(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    receipt = cleanup._backup_receipt(world, run, backup, manifest, candidates)
    other = tmp_path / "other"
    other.mkdir()
    _schema(other)
    with pytest.raises(cleanup.CleanupRefusedError, match="data directory"):
        cleanup._load_receipt(run, backup, manifest, candidates, other)
    assert receipt["data_dir"] == cleanup._data_dir_identity(world)


@pytest.mark.parametrize(("field", "value"), [("component_count", True), ("digest", "0" * 64)])
def test_receipt_rejects_strict_intent_tampering(world: Path, tmp_path: Path, field: str, value: object):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt_path = run / "receipt.json"
    receipt = _load(receipt_path)
    receipt["component_intent"][field] = value
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="component intent"):
        cleanup._load_receipt(run, backup, manifest, candidates, world)


@pytest.mark.parametrize("action", ["--apply", "--prepare-rollback", "--rollback"])
@pytest.mark.parametrize("mutation", ["empty", "canonical", "digest", "count", "manifest", "endpoint", "row", "component_ids", "deletion_target", "ordered_ids"])
def test_cli_intent_tampering_refuses_all_receipt_callers(world: Path, tmp_path: Path, action: str, mutation: str):
    first, second, other = "evidence:turn:heartbeat", "evidence:turn:intent-count", "evidence:turn:normal"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists intent count")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "rel", "Evidence", second, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "boundary", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if action == "--rollback":
        import repair_store_mismatch_499 as repair
        original_holders = repair.holders
        repair.holders = lambda _: ([], 0)
        try:
            cleanup.prepare_rollback(world, run, backup)
        finally:
            repair.holders = original_holders
    receipt_path = run / "receipt.json"
    receipt = _load(receipt_path)
    receipt_before = receipt_path.read_bytes()
    intent = receipt["component_intent"]
    ordered_ids_seam = mutation == "ordered_ids"
    if mutation == "empty":
        receipt["component_intent"] = {}
    elif mutation == "canonical":
        intent["canonical"] = "{}"
    elif mutation == "digest":
        intent["digest"] = "0" * 64
    elif mutation == "count":
        assert intent["component_count"] == 1
        intent["component_count"] = True
    elif mutation == "manifest":
        intent["manifest"] = "0" * 64
    elif mutation == "endpoint":
        intent["component"][0]["edge"]["to_id"] = "evidence:turn:tampered-endpoint"
    elif mutation == "row":
        intent["component"][0]["edge"]["properties"] = json.dumps({"tampered": True})
    elif mutation == "component_ids":
        intent["component_ids"] = []
    elif mutation == "deletion_target":
        intent["boundary"][0]["deletion_target"] = False
    if not ordered_ids_seam:
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        receipt_before = receipt_path.read_bytes()
    trace = tmp_path / f"intent-{mutation}-{action[2:]}.trace"
    runner = tmp_path / f"intent-{mutation}-{action[2:]}.py"
    script_dir = Path(__file__).parent.parent / "scripts"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, action, "--backup-dir", str(backup)]
    if action == "--apply":
        args.extend(["--approved-manifest", str(approval)])
    ordered_setup = ""
    if ordered_ids_seam:
        ordered_setup = (
            "def ordered_validate(receipt, candidates, manifest):\n"
            "    trace.write('validate-intent\\n')\n"
            "    trace.flush()\n"
            "    changed = dict(manifest)\n"
            "    changed['ordered_ids_sha256'] = '0' * 64\n"
            "    return original_validate(receipt, candidates, changed)\n"
            "cleanup._validate_intent = ordered_validate\n"
        )
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original_validate = cleanup._validate_intent\n"
        "def traced_validate(receipt, candidates, manifest):\n"
        "    trace.write('validate-intent\\n')\n"
        "    trace.flush()\n"
        "    return original_validate(receipt, candidates, manifest)\n"
        "cleanup._validate_intent = traced_validate\n"
        + ordered_setup
        + f"code = cleanup.main({args!r})\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    before = {name: (world / name).read_bytes() for name in cleanup.DB_FILES}
    tombstone = run / "rollback-tombstone.json"
    tombstone_before = tombstone.read_bytes() if action == "--rollback" else None
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0, mutation + action
    assert "receipt component intent mismatch" in result.stderr
    assert trace.read_text(encoding="utf-8") == "validate-intent\n"
    assert {name: (world / name).read_bytes() for name in cleanup.DB_FILES} == before
    assert receipt_path.read_bytes() == receipt_before
    if action == "--prepare-rollback":
        assert not tombstone.exists()
    if action == "--rollback":
        assert tombstone.read_bytes() == tombstone_before
        assert not (run / "rollback-complete.json").exists()


def test_marker_dangling_symlink_fails_closed(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "rollback-tombstone.json").symlink_to(run / "missing")
    assert cleanup.marker_state(run)[0] == "incomplete artifact"


def test_shared_candidate_edge_is_declared_target_on_second_node(world: Path, tmp_path: Path):
    second = "evidence:turn:second"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists now")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", "evidence:turn:heartbeat", "rel", "Evidence", second, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id=?", (second,)).fetchone() is None
        assert conn.execute("SELECT * FROM graph_edges").fetchall() == []


def test_restore_attempt_binding_mismatch_refuses(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    root = run / "restore-attempts"
    root.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(world), "files": {}}
    (root / "graph.db.bad.json").write_text(json.dumps({"receipt_sha256": "other", "backup_set_id": "b", "data_dir": cleanup._data_dir_identity(world), "file": "graph.db", "attempt": "bad"}), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="binding mismatch"):
        cleanup._validate_attempts(run, receipt, world)


def test_sidecar_removal_fsyncs_each_namespace_change(tmp_path: Path, monkeypatch):
    db = tmp_path / "graph.db"
    db.write_bytes(b"db")
    for suffix in ("-wal", "-shm", "-journal"):
        db.with_name(db.name + suffix).write_bytes(b"sidecar")
    calls: list[Path] = []
    monkeypatch.setattr(cleanup, "_fsync_dir", lambda path: calls.append(path))
    cleanup._remove_sidecars(db)
    assert calls == [tmp_path, tmp_path, tmp_path]
    assert all(not db.with_name(db.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


def test_marker_missing_binding_fields_is_incomplete(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    body = {"kind": "tombstone", "payload_sha256": ""}
    body["payload_sha256"] = cleanup._sha(cleanup._canon({"kind": "tombstone"}))
    (run / "rollback-tombstone.json").write_text(json.dumps(body), encoding="utf-8")
    assert cleanup.marker_state(run)[0] == "incomplete artifact"


def test_apply_rechecks_claim_support_and_protected_edges(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    other = "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:keep", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Claim", "claim:keep", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", other, "supports", "Claim", "claim:keep", "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='supports'", (other,))
    with pytest.raises(cleanup.CleanupRefusedError, match="full frozen snapshot"):
        cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))


def test_claim_guard_rejects_candidate_only_support(world: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:only", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Claim", "claim:only", "{}"))
    candidates, _ = cleanup.select(world, TABLE)
    assert cleanup._claim_guard(world, candidates) == ["claim:only"]


def test_unrelated_unsupported_claim_does_not_block_candidate_apply(world: Path, tmp_path: Path):
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:unrelated", "claim", "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id='claim:unrelated'").fetchall() == [("claim:unrelated",)]


def test_supports_edge_to_evidence_is_not_claim_loss(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Evidence", other, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))


def test_cli_document_partial_resume_requires_receipt(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    candidate = candidates[0]
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    initial = _cli_main(tmp_path, "initial-document", args)
    assert initial.returncode == 0, initial.stderr
    with _db(world / "doc_store.db") as conn:
        conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", (candidate["id"], HEARTBEAT))
        conn.execute("INSERT INTO doc_nodes VALUES (?,?,?,?)", ("evidence", candidate["id"], "Evidence", json.dumps({"pack_id": "openclaw"})))
    result = _cli_main(tmp_path, "resume-document", args)
    assert result.returncode == 0, result.stderr
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT source_id FROM doc_sources_fts WHERE source_id=?", (candidate["id"],)).fetchall() == []
        assert conn.execute("SELECT node_id FROM doc_nodes WHERE node_id=?", (candidate["id"],)).fetchall() == []


def test_cli_document_partial_first_apply_refuses_without_receipt(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    candidate = candidates[0]
    with _db(world / "doc_store.db") as conn:
        conn.execute("DELETE FROM doc_sources WHERE source_id=?", (candidate["id"],))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "first-document.py"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main({args!r}))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists()
    assert not (run / "receipt.json").exists()


def test_cli_registry_reports_expected_and_actual_orphans_without_writes(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    registry = world / "opencrab.db"
    with _db(registry) as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("evidence", "Evidence", normal))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "preexisting", "evidence", "missing"))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "expected", "evidence", normal))
    registry_uri = f"{registry.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(registry_uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        edges = {tuple(row) for row in conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges")}
    graph_uri = f"{(world / 'graph.db').resolve().as_uri()}?mode=ro"
    with sqlite3.connect(graph_uri, uri=True) as conn:
        before_ids = {row[0] for row in conn.execute("SELECT node_id FROM graph_nodes")}
    run, backup = tmp_path / "run", tmp_path / "backup"
    trace = tmp_path / "registry.trace"
    base = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]

    def setup(source: str) -> str:
        return (
            "import json\n"
            "import sqlite3\n"
            f"trace = open({str(trace)!r}, 'a', encoding='utf-8')\n"
            "for name in ('_ro', '_rw'):\n"
            "    original = getattr(cleanup, name)\n"
            "    def wrap(factory, connection_source):\n"
            "        def open_connection(path, *args):\n"
            "            conn = factory(path, *args)\n"
            "            if path.name == 'opencrab.db':\n"
            "                forbidden_names = ('SQLITE_INSERT', 'SQLITE_UPDATE', 'SQLITE_DELETE', 'SQLITE_CREATE_INDEX', 'SQLITE_CREATE_TABLE', 'SQLITE_CREATE_TEMP_INDEX', 'SQLITE_CREATE_TEMP_TABLE', 'SQLITE_CREATE_TEMP_TRIGGER', 'SQLITE_CREATE_TEMP_VIEW', 'SQLITE_CREATE_TRIGGER', 'SQLITE_CREATE_VIEW', 'SQLITE_CREATE_VTABLE', 'SQLITE_DROP_INDEX', 'SQLITE_DROP_TABLE', 'SQLITE_DROP_TEMP_INDEX', 'SQLITE_DROP_TEMP_TABLE', 'SQLITE_DROP_TEMP_TRIGGER', 'SQLITE_DROP_TEMP_VIEW', 'SQLITE_DROP_TRIGGER', 'SQLITE_DROP_VIEW', 'SQLITE_DROP_VTABLE', 'SQLITE_ALTER_TABLE', 'SQLITE_ATTACH', 'SQLITE_DETACH')\n"
            "                forbidden = {getattr(sqlite3, item) for item in forbidden_names}\n"
            "                def event(kind, action, arg1=None, arg2=None, raw=None):\n"
            "                    trace.write(json.dumps({'source': connection_source, 'kind': kind, 'action': action, 'arg1': arg1, 'arg2': arg2, 'raw': raw}, sort_keys=True) + '\\n')\n"
            "                    trace.flush()\n"
            "                def authorizer(action, arg1, arg2, database, trigger):\n"
            "                    event('authorizer', action, arg1, arg2)\n"
            "                    return sqlite3.SQLITE_OK\n"
            "                conn.set_authorizer(authorizer)\n"
            "                conn.set_trace_callback(lambda sql: event('trace', sql.lstrip().split(None, 1)[0].upper(), raw=sql))\n"
            "            return conn\n"
            "        return open_connection\n"
            "    setattr(cleanup, name, wrap(original, " + repr(source) + " + ':' + name))\n"
        )

    before_dry = registry.read_bytes()
    dry = _cli_main(tmp_path, "registry-dry", base, setup("dry"))
    assert dry.returncode == 0, dry.stderr
    assert registry.read_bytes() == before_dry
    report = _load(run / "report.json")["registry"]
    expected_ids = before_ids - {candidate}
    independently_expected = {edge for edge in edges if edge[1] not in expected_ids or edge[4] not in expected_ids}
    assert {tuple(edge) for edge in report["expected_after_orphans"]} == independently_expected
    assert {tuple(edge) for edge in report["preexisting_orphans"]} != independently_expected
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    before_apply = registry.read_bytes()
    applied = _cli_main(tmp_path, "registry-apply", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)], setup("apply"))
    assert applied.returncode == 0, applied.stderr
    assert registry.read_bytes() == before_apply
    actual_report = _load(run / "registry-after.json")
    with sqlite3.connect(graph_uri, uri=True) as conn:
        actual_ids = {row[0] for row in conn.execute("SELECT node_id FROM graph_nodes")}
    independently_actual = {edge for edge in edges if edge[1] not in actual_ids or edge[4] not in actual_ids}
    assert {tuple(edge) for edge in actual_report["preexisting_orphans"]} == independently_actual
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    _assert_observer_events(events, production=True)
    assert any(event["source"] == "dry:_ro" and event["kind"] == "trace" and event["action"] == "SELECT" for event in events)
    assert any(event["source"] == "apply:_ro" and event["kind"] == "trace" and event["action"] == "SELECT" for event in events)


def test_registry_observer_parser_and_virtual_table_controls():
    def event(*, action: int | str, arg1: str | None = None, arg2: str | None = None, kind: str = "authorizer") -> dict:
        return {"source": "observer-unit", "kind": kind, "action": action, "arg1": arg1, "arg2": arg2, "raw": None}

    baseline = [
        event(action=sqlite3.SQLITE_PRAGMA, arg1="query_only"),
        event(action=sqlite3.SQLITE_PRAGMA, arg1="table_info", arg2="ontology_nodes"),
        event(action=sqlite3.SQLITE_PRAGMA, arg1="foreign_key_list", arg2="ontology_edges"),
        event(action="SELECT", kind="trace"),
    ]
    _assert_observer_events(baseline, production=False)
    query_only_arg = [*baseline]
    query_only_arg[0] = event(action=sqlite3.SQLITE_PRAGMA, arg1="query_only", arg2="ON")
    invalid_name = [*baseline]
    invalid_name[0] = event(action=sqlite3.SQLITE_PRAGMA, arg1="writable_schema")
    invalid_table = [*baseline]
    invalid_table[1] = event(action=sqlite3.SQLITE_PRAGMA, arg1="table_info", arg2="other")
    controls = [
        ("query_only argument", query_only_arg),
        ("pragma name", invalid_name),
        ("pragma table", invalid_table),
        ("pragma event missing", []),
        ("forbidden action", [*baseline, event(action=sqlite3.SQLITE_CREATE_VTABLE)]),
        ("forbidden action", [*baseline, event(action=sqlite3.SQLITE_DROP_VTABLE)]),
    ]
    for reason, invalid in controls:
        with pytest.raises(AssertionError) as excinfo:
            _assert_observer_events(invalid, production=False)
        assert str(excinfo.value) == reason
        _assert_observer_events(baseline, production=False)


def test_runbook_states_actual_run_and_successor_contract():
    runbook = (Path(__file__).parent.parent / "docs" / "openclaw-heartbeat-cleanup.md").read_text(encoding="utf-8")
    assert "존재하지 않는 새 run path" in runbook
    assert "비어 있는 기존 directory도 지정하면 안 된다" in runbook
    assert "--run-dir <new-run-dir>" in runbook
    assert "하나의 successor UUID directory" in runbook
    assert "data directory fsync" in runbook
    assert "현재 DB file store-set identity를 다시 계산하지 않는다" in runbook


def test_restore_attempt_names_are_unique(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    source = tmp_path / "backup.db"
    source.write_bytes(b"backup")
    destination = data / "graph.db"
    destination.write_bytes(b"old")
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    first = cleanup._publish_attempt_successor(run, receipt)
    second = cleanup._publish_attempt_successor(run, receipt)
    first_dir = run / "restore-attempts" / first["directory"]
    second_dir = run / "restore-attempts" / second["directory"]
    cleanup._restore_file(source, destination, first_dir, receipt)
    cleanup._restore_file(source, destination, second_dir, receipt)
    assert first["attempt"] != second["attempt"]
    assert _load(first_dir / "graph.db.json")["attempt"] == first["attempt"]
    assert _load(second_dir / "graph.db.json")["attempt"] == second["attempt"]


def test_cli_dry_run_uses_fresh_process(world: Path, tmp_path: Path):
    script = Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py"
    run = tmp_path / "cli-run"
    result = subprocess.run(
        [sys.executable, str(script), "--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert _load(run / "manifest.json")["vector_table"] == TABLE


@pytest.mark.parametrize("row_id,mutation", [
    ("B-FA-ADDED-VAL", "added"),
    ("B-FA-CHANGED-VAL", "changed"),
    ("B-RA-ADDED-VAL", "added"),
    ("B-RA-CHANGED-VAL", "changed"),
])
def test_cli_boundary_mutation_rows_refuse_before_vector_write(world: Path, tmp_path: Path, row_id: str, mutation: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        if mutation == "missing":
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
        elif mutation == "added":
            conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "extra", "Evidence", other, "{}"))
        else:
            conn.execute("UPDATE graph_edges SET properties='{" + '"changed":true' + "}' WHERE from_id=? AND relation='rel'", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    before = (world / "vectors.db").read_bytes()
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0, row_id
    assert (world / "vectors.db").read_bytes() == before


@pytest.mark.parametrize("branch,row_id,mutation", [
    ("FA", "B-FA-ALLOWED-VAL", "allowed"),
    ("FA", "B-FA-ADDED-VAL", "added"),
    ("FA", "B-FA-CHANGED-VAL", "changed"),
    ("RA", "B-RA-ALLOWED-VAL", "allowed"),
    ("RA", "B-RA-ADDED-VAL", "added"),
    ("RA", "B-RA-CHANGED-VAL", "changed"),
])
def test_cli_boundary_validator_rows_use_actual_fa_ra_branches(world: Path, tmp_path: Path, branch: str, row_id: str, mutation: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / f"{branch}-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if branch == "RA":
        cleanup._backup_receipt(world, run, backup, manifest, candidates)
        with _db(world / "graph.db") as conn:
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    assert (run / "receipt.json").exists() is (branch == "RA")
    before = (world / "vectors.db").read_bytes()
    trace = tmp_path / f"{row_id}.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    if mutation == "allowed":
        fa_injection = "        conn.execute(\"DELETE FROM graph_edges WHERE from_id=? AND relation='rel'\", (" + repr(candidate) + ",))\n"
        ra_injection = "    pass\n"
    elif mutation == "added":
        fa_injection = "        conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'extra', 'Evidence', " + repr(other) + ", '{}'))\n"
        ra_injection = "    conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'extra', 'Evidence', " + repr(other) + ", '{}'))\n"
    else:
        fa_injection = "        conn.execute(\"UPDATE graph_edges SET properties=? WHERE from_id=? AND relation='rel'\", (" + repr(json.dumps({"changed": True})) + ", " + repr(candidate) + "))\n"
        ra_injection = "    conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'rel', 'Evidence', " + repr(other) + ", " + repr(json.dumps({"changed": True})) + "))\n"
    if branch == "FA":
        setup = (
            "original_backup = cleanup._backup_receipt\n"
            "def publish_then_inject(data, run, backup, manifest, candidates):\n"
            "    trace.write('branch=first receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    receipt = original_backup(data, run, backup, manifest, candidates)\n"
            "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    with sqlite3.connect(data / 'graph.db') as conn:\n"
            + fa_injection
            + "    return receipt\n"
            "cleanup._backup_receipt = publish_then_inject\n"
        )
    else:
        setup = (
            "trace.write('branch=resume receipt-before=True\\n')\n"
            "def no_republish(*args):\n"
            "    raise AssertionError('unexpected backup receipt publication')\n"
            "cleanup._backup_receipt = no_republish\n"
            "with sqlite3.connect(" + repr(str(world / "graph.db")) + ") as conn:\n"
            + ra_injection
        )
    runner.write_text(
        "import sqlite3\nimport sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\nimport repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        + setup
        + f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\nraise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    statements = trace.read_text(encoding="utf-8")
    assert statements.startswith(f"branch={'first' if branch == 'FA' else 'resume'} receipt-before={str(branch == 'RA')}\n")
    if branch == "FA":
        assert "receipt-after=True" in statements
    if mutation == "allowed":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0, row_id
        assert "boundary edge R/D/S conflict" in result.stderr
        assert (world / "vectors.db").read_bytes() == before


def test_cli_first_apply_publication_then_boundary_validator(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "first-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    trace = tmp_path / "fa.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "fa.py"
    runner.write_text(
        "import sqlite3\nimport sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\nimport repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original = cleanup._backup_receipt\n"
        "def publish_then_mutate(data, run, backup, manifest, candidates):\n"
        "    trace.write('receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    receipt = original(data, run, backup, manifest, candidates)\n"
        "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"UPDATE graph_edges SET properties='{" + '\\"changed\\":true' + "}' WHERE from_id=? AND relation='rel'\", (" + repr(candidate) + ",))\n"
        "    trace.close()\n"
        "    return receipt\n"
        "cleanup._backup_receipt = publish_then_mutate\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    before = (world / "vectors.db").read_bytes()
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "boundary edge R/D/S conflict" in result.stderr
    assert trace.read_text(encoding="utf-8") == "receipt-before=False\nreceipt-after=True\n"
    assert (world / "vectors.db").read_bytes() == before


@pytest.mark.parametrize("row_id", ["B-FA-ALLOWED-VAL", "B-RA-ALLOWED-VAL"])
def test_cli_boundary_allowed_rows_accept_receipt_bound_absence(world: Path, tmp_path: Path, row_id: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, row_id + result.stderr


def test_cli_boundary_change_refuses_before_vector_write(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("UPDATE graph_edges SET properties='{" + '"changed":true' + "}' WHERE from_id=?", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "boundary.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(tmp_path / 'backup')!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert conn.execute(f"SELECT node_id FROM {TABLE} WHERE node_id=?", (candidate,)).fetchall() == [(candidate,)]


def test_cli_apply_prepare_and_rollback_use_argparse_entrypoints(world: Path, tmp_path: Path):
    script_dir = Path(__file__).parent.parent / "scripts"
    run, backup = tmp_path / "cli-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    runner = tmp_path / "entrypoints.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"base = ['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}]\n"
        f"assert cleanup.main([*base, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]) == 0\n"
        f"assert cleanup.main([*base, '--prepare-rollback', '--backup-dir', {str(backup)!r}]) == 0\n"
        f"assert cleanup.main([*base, '--rollback', '--backup-dir', {str(backup)!r}]) == 0\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


def test_attempt_successor_publishes_directory_after_fsync(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    payload = cleanup._publish_attempt_successor(run, receipt)
    assert (run / f"{cleanup.ATTEMPT_PREFIX}{payload['attempt']}.json").is_file()
    assert (run / "restore-attempts" / payload["directory"]).is_dir()
    assert cleanup._published_attempts(run, receipt)[0]["attempt"] == payload["attempt"]


def test_cli_rollback_metadata_only_preserves_evidence_and_resumes(world: Path, tmp_path: Path):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    candidates, manifest = cleanup._load_run(run)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    successor = cleanup._publish_attempt_successor(run, receipt)
    attempt_dir = run / "restore-attempts" / successor["directory"]
    metadata = cleanup._attempt_metadata(receipt, world, "graph.db", successor["attempt"])
    cleanup._write_fsync(attempt_dir / "graph.db.json", cleanup._canon(metadata), exclusive=True)
    result = _cli_main(tmp_path, "metadata-only", rollback_args)
    assert result.returncode == 0, result.stderr
    assert (attempt_dir / "graph.db.json").read_bytes() == cleanup._canon(metadata)
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


def test_cli_rollback_parent_fsync_failure_preserves_pre_restore_state(world: Path, tmp_path: Path):
    run, _, rollback_args = _cli_rollback_state(world, tmp_path)
    before = {name: (world / name).read_bytes() for name in cleanup.DB_FILES}
    setup = (
        "original = cleanup._fsync_dir\n"
        "def fsync(path):\n"
        "    if path == cleanup.Path(" + repr(str(run)) + ") and (path / 'restore-attempts').exists():\n"
        "        raise OSError('parent fsync fault')\n"
        "    return original(path)\n"
        "cleanup._fsync_dir = fsync\n"
    )
    result = _cli_main(tmp_path, "parent-fsync", rollback_args, setup)
    assert result.returncode != 0
    assert "parent fsync fault" in result.stderr
    assert {name: (world / name).read_bytes() for name in cleanup.DB_FILES} == before
    assert not list(run.glob(f"{cleanup.ATTEMPT_PREFIX}*.json"))
    assert not (run / "rollback-complete.json").exists()


def test_cli_rollback_fault_artifacts_follow_binding_contract(world: Path, tmp_path: Path):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    candidates, manifest = cleanup._load_run(run)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    successor = cleanup._publish_attempt_successor(run, receipt)
    attempt_dir = run / "restore-attempts" / successor["directory"]
    metadata = cleanup._attempt_metadata(receipt, world, "graph.db", successor["attempt"])
    _write = cleanup._write_fsync
    _write(attempt_dir / "graph.db.json", cleanup._canon(metadata), exclusive=True)
    replacement = world / metadata["replacement"]
    replacement.write_bytes(b"partial")
    result = _cli_main(tmp_path, "partial-replacement", rollback_args)
    assert result.returncode == 0, result.stderr
    assert replacement.read_bytes() == b"partial"
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


@pytest.mark.parametrize("fault", ["missing-directory", "unbound-replacement", "unbound-sidecar"])
def test_cli_rollback_rejects_unbound_artifacts(world: Path, tmp_path: Path, fault: str):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    candidates, manifest = cleanup._load_run(run)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    if fault == "missing-directory":
        successor = cleanup._publish_attempt_successor(run, receipt)
        (run / "restore-attempts" / successor["directory"]).rmdir()
    elif fault == "unbound-replacement":
        (world / ".graph.db.restore-unbound").write_bytes(b"partial")
    else:
        (world / "graph.db-wal").write_bytes(b"partial")
    before = {name: (world / name).read_bytes() for name in cleanup.DB_FILES}
    result = _cli_main(tmp_path, fault, rollback_args)
    assert result.returncode != 0
    assert {name: (world / name).read_bytes() for name in cleanup.DB_FILES} == before
    assert not (run / "rollback-complete.json").exists()


def test_cli_completion_verifies_without_restore(world: Path, tmp_path: Path):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    first = _cli_main(tmp_path, "complete-first", rollback_args)
    assert first.returncode == 0, first.stderr
    trace = tmp_path / "completion-trace.txt"
    setup = (
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "cleanup._restore_file = lambda *args: (_ for _ in ()).throw(AssertionError('restore called'))\n"
        "original = cleanup._fsync_dir\n"
        "def fsync(path):\n"
        "    trace.write(str(path) + '\\n')\n"
        "    trace.flush()\n"
        "    return original(path)\n"
        "cleanup._fsync_dir = fsync\n"
    )
    second = _cli_main(tmp_path, "complete-second", rollback_args, setup)
    assert second.returncode == 0, second.stderr
    assert str(run) in trace.read_text(encoding="utf-8")


def test_published_attempt_missing_directory_fails_closed(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    payload = cleanup._publish_attempt_successor(run, receipt)
    (run / "restore-attempts" / payload["directory"]).rmdir()
    with pytest.raises(cleanup.CleanupRefusedError, match="directory is missing"):
        cleanup._published_attempts(run, receipt)


def test_cli_resume_after_graph_commit_uses_receipt_component_intent(world: Path, tmp_path: Path):
    first, second, third = "evidence:turn:heartbeat", "evidence:turn:second-cli", "evidence:turn:third-cli"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists cli second")
    _add(world, third, "Q: Read HEARTBEAT.md if it exists cli third")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "ab", "Evidence", second, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", second, "bc", "Evidence", third, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    _, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    interrupted = tmp_path / "interrupt.py"
    interrupted.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent / 'scripts')!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        "original = cleanup._delete_graph\n"
        "def stop(data, record, candidates, receipt, manifest):\n"
        "    original(data, record, candidates, receipt, manifest, after_commit=lambda: (_ for _ in ()).throw(SystemExit(77)))\n"
        "cleanup._delete_graph = stop\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    interrupted_result = subprocess.run(
        [sys.executable, str(interrupted)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert interrupted_result.returncode == 77
    runner = tmp_path / "resume.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent / 'scripts')!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT relation FROM graph_edges ORDER BY relation").fetchall() == [("bc",)]
    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id IN (?,?,?)", (first, second, third)).fetchall() == []


@pytest.mark.parametrize("branch,row_id", [("FA", "B-FA-ALLOWED-TX"), ("RA", "B-RA-ALLOWED-TX")])
def test_cli_boundary_allowed_transaction_has_no_redelete_or_dangling(world: Path, tmp_path: Path, branch: str, row_id: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / f"{branch}-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if branch == "RA":
        cleanup._backup_receipt(world, run, backup, manifest, candidates)
        with _db(world / "graph.db") as conn:
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    assert (run / "receipt.json").exists() is (branch == "RA")
    trace = tmp_path / f"{row_id}.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    setup = (
        "original_backup = cleanup._backup_receipt\n"
        "def publish_then_remove(data, run, backup, manifest, candidates):\n"
        "    trace.write('branch=first receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    receipt = original_backup(data, run, backup, manifest, candidates)\n"
        "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"DELETE FROM graph_edges WHERE from_id=? AND relation='rel'\", (" + repr(candidate) + ",))\n"
        "    return receipt\n"
        "cleanup._backup_receipt = publish_then_remove\n"
        if branch == "FA" else
        "trace.write('branch=resume receipt-before=True\\n')\n"
        "def no_republish(*args):\n"
        "    raise AssertionError('unexpected backup receipt publication')\n"
        "cleanup._backup_receipt = no_republish\n"
    )
    runner.write_text(
        "import sqlite3\nimport sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\nimport repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'graph.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        + setup
        + f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\nraise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    statements = trace.read_text(encoding="utf-8")
    assert statements.startswith(f"branch={'first' if branch == 'FA' else 'resume'} receipt-before={str(branch == 'RA')}\n")
    if branch == "FA":
        assert "receipt-after=True" in statements
    assert "DELETE FROM graph_edges" not in statements
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT * FROM graph_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == []


@pytest.mark.parametrize("row_id,mutation", [
    ("B-FA-ADDED-TX", "added"), ("B-FA-CHANGED-TX", "changed"),
    ("B-RA-ADDED-TX", "added"), ("B-RA-CHANGED-TX", "changed"),
])
def test_cli_boundary_transaction_rows_reject_before_graph_delete(world: Path, tmp_path: Path, row_id: str, mutation: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    branch = "FA" if "B-FA" in row_id else "RA"
    run, backup = tmp_path / f"{branch}-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if branch == "RA":
        cleanup._backup_receipt(world, run, backup, manifest, candidates)
        with _db(world / "graph.db") as conn:
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    assert (run / "receipt.json").exists() is (branch == "RA")
    trace = tmp_path / f"{row_id}.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    if mutation == "added":
        injection = "        conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'extra', 'Evidence', " + repr(other) + ", '{}'))\n"
    elif branch == "FA":
        injection = "        conn.execute(\"UPDATE graph_edges SET properties=? WHERE from_id=? AND relation='rel'\", (" + repr(json.dumps({"changed": True})) + ", " + repr(candidate) + "))\n"
    else:
        injection = "        conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'rel', 'Evidence', " + repr(other) + ", " + repr(json.dumps({"changed": True})) + "))\n"
    if branch == "FA":
        setup = (
            "original_backup = cleanup._backup_receipt\n"
            "def traced_backup(data, run, backup, manifest, candidates):\n"
            "    trace.write('branch=first receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    receipt = original_backup(data, run, backup, manifest, candidates)\n"
            "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    return receipt\n"
            "cleanup._backup_receipt = traced_backup\n"
        )
    else:
        setup = (
            "trace.write('branch=resume receipt-before=True\\n')\n"
            "def no_republish(*args):\n"
            "    raise AssertionError('unexpected backup receipt publication')\n"
            "cleanup._backup_receipt = no_republish\n"
        )
    runner.write_text(
        "import sqlite3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'graph.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        + setup
        + "orig = cleanup._delete_graph\n"
        "def mutate(data, record, candidates, receipt, manifest):\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        + injection
        + "    return orig(data, record, candidates, receipt, manifest)\n"
        "cleanup._delete_graph = mutate\n"
        f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0, row_id
    assert "boundary edge R/D/S conflict" in result.stderr
    statements = trace.read_text(encoding="utf-8")
    assert statements.startswith(f"branch={'first' if branch == 'FA' else 'resume'} receipt-before={str(branch == 'RA')}\n")
    if branch == "FA":
        assert "receipt-after=True" in statements
    assert "BEGIN IMMEDIATE" in statements
    assert "DELETE FROM graph_edges" not in statements


def test_cli_claim_preflight_refuses_before_vector_delete(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:keep", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Claim", "claim:keep", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", other, "supports", "Claim", "claim:keep", "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    trace = tmp_path / "vector-trace.txt"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "claim-preflight.py"
    runner.write_text(
        "import sqlite3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'vectors.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        "orig = cleanup._validate_all_candidate_claims\n"
        "def mutate(data, candidates, manifest):\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"DELETE FROM graph_edges WHERE from_id=? AND relation='supports'\", (" + repr(other) + ",))\n"
        "    return orig(data, candidates, manifest)\n"
        "cleanup._validate_all_candidate_claims = mutate\n"
        f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "projected protected graph state changed" in result.stderr
    assert "DELETE FROM" not in trace.read_text(encoding="utf-8")


def test_cli_component_transaction_change_rejects_before_graph_delete(world: Path, tmp_path: Path):
    first, second = "evidence:turn:heartbeat", "evidence:turn:component-tx"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists component tx")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "rel", "Evidence", second, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    trace = tmp_path / "graph-trace.txt"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "component-tx.py"
    runner.write_text(
        "import sqlite3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'graph.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        "orig = cleanup._delete_graph\n"
        "def mutate(data, record, candidates, receipt, manifest):\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"UPDATE graph_edges SET properties='{" + '\\"changed\\":true' + "}' WHERE from_id=? AND to_id=?\", (" + repr(first) + ", " + repr(second) + "))\n"
        "    return orig(data, record, candidates, receipt, manifest)\n"
        "cleanup._delete_graph = mutate\n"
        f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "component edge R/D/S conflict" in result.stderr
    statements = trace.read_text(encoding="utf-8")
    assert "BEGIN IMMEDIATE" in statements
    assert "DELETE FROM graph_edges" not in statements


def test_cli_boundary_allowance_removal_refuses_approved_absence(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "allowance-removal.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        "original = cleanup._boundary_edges\n"
        "def no_allowance(candidates, receipt, manifest):\n"
        "    recorded, _ = original(candidates, receipt, manifest)\n"
        "    return recorded, set()\n"
        "cleanup._boundary_edges = no_allowance\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0


def test_cli_initial_intent_publication_removal_refuses(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "publication-removal.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        "original = cleanup._backup_receipt\n"
        "def incomplete(*args):\n"
        "    receipt = original(*args)\n"
        "    receipt.pop('component_intent')\n"
        "    cleanup._publish(args[1] / 'receipt.json', cleanup._canon(receipt))\n"
        "    return receipt\n"
        "cleanup._backup_receipt = incomplete\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0


def test_cli_complete_intent_all_callers_accept_identical_receipt(world: Path, tmp_path: Path):
    script_dir = Path(__file__).parent.parent / "scripts"
    run, backup = tmp_path / "complete-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    base = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]

    def invoke(name: str, args: list[str]) -> None:
        runner = tmp_path / f"complete-{name}.py"
        runner.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(script_dir)!r})\n"
            "import cleanup_openclaw_heartbeat_493 as cleanup\n"
            "import repair_store_mismatch_499 as repair\n"
            "repair.holders = lambda _: ([], 0)\n"
            f"raise SystemExit(cleanup.main({args!r}))\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert result.returncode == 0, result.stderr

    invoke("apply", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)])
    receipt_path = run / "receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    assert _load(receipt_path)["component_intent"] == cleanup._component_intents(*cleanup._load_run(run))
    for name, args in (
        ("apply-resume", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]),
        ("prepare", [*base, "--prepare-rollback", "--backup-dir", str(backup)]),
        ("rollback", [*base, "--rollback", "--backup-dir", str(backup)]),
    ):
        assert receipt_path.read_bytes() == receipt_bytes
        invoke(name, args)
        assert receipt_path.read_bytes() == receipt_bytes


def test_cli_prepare_then_rollback_entrypoint(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    runner = tmp_path / "rollback.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent / 'scripts')!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"cleanup.prepare_rollback(Path({str(world)!r}), Path({str(run)!r}), Path({str(backup)!r}))\n"
        f"cleanup.rollback(Path({str(world)!r}), Path({str(run)!r}), Path({str(backup)!r}))\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    restored = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env=env)
    assert restored.returncode == 0, restored.stderr
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


def test_component_helper_rejects_changed_shared_edge(world: Path, tmp_path: Path):
    first, second = "evidence:turn:heartbeat", "evidence:turn:second-rds"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists rds")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "rel", "Evidence", second, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    receipt = cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    with _db(world / "graph.db") as conn:
        conn.execute("UPDATE graph_edges SET properties='{" + '"changed":true' + "}' WHERE from_id=? AND to_id=?", (first, second))
    conn = sqlite3.connect(world / "graph.db")
    try:
        with pytest.raises(cleanup.CleanupRefusedError, match="component edge R/D/S conflict"):
            cleanup._validate_component_edges(conn, candidates, receipt, manifest)
    finally:
        conn.close()


def test_abc_component_resume_b_transaction_uses_rds(world: Path, tmp_path: Path):
    a, b, c = "evidence:turn:heartbeat", "evidence:turn:abc-b", "evidence:turn:abc-c"
    _add(world, b, "Q: Read HEARTBEAT.md if it exists abc b")
    _add(world, c, "Q: Read HEARTBEAT.md if it exists abc c")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", a, "ab", "Evidence", b, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", b, "bc", "Evidence", c, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    by_id = {item["id"]: item for item in candidates}
    assert {edge["relation"] for edge in by_id[b]["edges"]} == {"ab", "bc"}
    backup = tmp_path / "backup"
    receipt = cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    cleanup._delete_graph(world, by_id[a], candidates, receipt, manifest)
    with _db(world / "graph.db") as conn:
        actual_b = conn.execute("SELECT relation FROM graph_edges WHERE from_id=? OR to_id=?", (b, b)).fetchall()
    assert actual_b == [("bc",)]
    cleanup._delete_graph(world, by_id[b], candidates, receipt, manifest)
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id IN (?,?)", (b, c)).fetchall() == [(c,)]
