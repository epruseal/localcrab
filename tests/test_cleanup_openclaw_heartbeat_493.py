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
    cleanup._restore_file(source, destination, run, receipt)
    assert old_artifact.read_bytes() == b"partial"
    assert destination.read_bytes() == b"backup"
    metadata = list((run / "restore-attempts").glob("graph.db.*.json"))
    assert len(metadata) == 1
    assert _load(metadata[0])["file"] == "graph.db"


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
    manifest = _load(run / "manifest.json")
    backup = tmp_path / "backup"
    receipt = cleanup._backup_receipt(world, run, backup, manifest)
    other = tmp_path / "other"
    other.mkdir()
    _schema(other)
    with pytest.raises(cleanup.CleanupRefusedError, match="data directory"):
        cleanup._load_receipt(run, backup, manifest, other)
    assert receipt["data_dir"] == cleanup._data_dir_identity(world)


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


def test_document_partial_resume_deletes_fts_and_doc_without_source(world: Path):
    record = cleanup._select_candidate(world, TABLE, ("evidence:turn:heartbeat", HEARTBEAT, json.dumps({"pack_id": "openclaw", "node_id": "evidence:turn:heartbeat"})))
    with _db(world / "doc_store.db") as conn:
        conn.execute("DELETE FROM doc_sources WHERE source_id=?", (record["id"],))
    cleanup._delete_document(world, record)
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT source_id FROM doc_sources_fts WHERE source_id=?", (record["id"],)).fetchall() == []
        assert conn.execute("SELECT node_id FROM doc_nodes WHERE node_id=?", (record["id"],)).fetchall() == []


def test_registry_report_separates_existing_and_expected_orphans(world: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "rel", "evidence", "missing"))
    report = cleanup._registry_report(world, {candidate})
    assert report["preexisting_orphans"]
    assert report["expected_after_orphans"]
    assert report["candidate_edges"]


def test_runbook_states_new_run_path_contract():
    runbook = (Path(__file__).parent.parent / "docs" / "openclaw-heartbeat-cleanup.md").read_text(encoding="utf-8")
    assert "새 run directory" in runbook
    assert "--run-dir <new-run-dir>" in runbook


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
    cleanup._restore_file(source, destination, run, receipt)
    cleanup._restore_file(source, destination, run, receipt)
    names = sorted(path.name for path in (run / "restore-attempts").glob("graph.db.*.json"))
    assert len(names) == 2
    assert names[0] != names[1]


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
    first, second = "evidence:turn:heartbeat", "evidence:turn:second-cli"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists cli")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "rel", "Evidence", second, "{}"))
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
        "original = cleanup._delete_graph\n"
        "def stop(data, record, candidates, receipt, manifest):\n"
        "    original(data, record, candidates, receipt, manifest, after_commit=lambda: (_ for _ in ()).throw(SystemExit(77)))\n"
        "cleanup._delete_graph = stop\n"
        f"cleanup.apply(Path({str(world)!r}), Path({str(run)!r}), {TABLE!r}, Path({str(approval)!r}), Path({str(backup)!r}), scan=lambda _: ([], 0))\n",
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
        f"cleanup.apply(Path({str(world)!r}), Path({str(run)!r}), {TABLE!r}, Path({str(approval)!r}), Path({str(backup)!r}), scan=lambda _: ([], 0))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id IN (?,?)", (first, second)).fetchall() == []


def test_cli_prepare_then_rollback_entrypoint(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    _, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    cleanup._backup_receipt(world, run, backup, manifest)
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
    receipt = cleanup._backup_receipt(world, run, backup, manifest)
    cleanup._publish_component_intents(run, receipt, candidates, manifest)
    receipt = cleanup._load_receipt(run, backup, manifest, world)
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
    receipt = cleanup._backup_receipt(world, run, backup, manifest)
    cleanup._publish_component_intents(run, receipt, candidates, manifest)
    receipt = cleanup._load_receipt(run, backup, manifest, world)
    cleanup._delete_graph(world, by_id[a], candidates, receipt, manifest)
    with _db(world / "graph.db") as conn:
        actual_b = conn.execute("SELECT relation FROM graph_edges WHERE from_id=? OR to_id=?", (b, b)).fetchall()
    assert actual_b == [("bc",)]
    cleanup._delete_graph(world, by_id[b], candidates, receipt, manifest)
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id IN (?,?)", (b, c)).fetchall() == [(c,)]
