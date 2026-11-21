"""``scripts/merge_sqlite_stores.py``: 여러 SQLite 파일을 한 파일로 합친다 (#671 시제품).

합성 원본(일반 테이블, fts5, vec0, 인덱스, 트리거, AUTOINCREMENT)으로 거는 것:
- 합친 파일의 테이블별 행 수가 원본과 같다(그림자 테이블 포함 전량).
- 합친 파일에서 vec0 KNN과 fts5 MATCH가 원본과 같은 결과를 낸다.
- 이름 충돌은 병합하지 않고 거부한다. 출력이 이미 있으면 거부한다.
- 계획 모드(``--apply`` 없음)는 출력 파일을 만들지 않는다.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "merge_sqlite_stores.py"
_spec = importlib.util.spec_from_file_location("merge_sqlite_stores", SCRIPT)
merge_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_mod)


@pytest.mark.parametrize("kind", ["directory", "symlink", "file"])
def test_merge_refuses_untrusted_journal_path(source_dir, tmp_path, kind):
    journal = source_dir / "delete-pack-journals"
    if kind == "directory":
        journal.mkdir()
        (journal / "pending.json").write_text("{}", encoding="utf-8")
    elif kind == "symlink":
        journal.symlink_to(tmp_path / "missing")
    else:
        journal.write_text("{}", encoding="utf-8")
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out)]) != 0
    assert not out.exists()


def test_apply_refuses_existing_deletion_journal(source_dir, tmp_path):
    journal_dir = source_dir / "delete-pack-journals"
    journal_dir.mkdir()
    (journal_dir / "pending.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "merged.db"
    rc = merge_mod.main(["--source-dir", str(source_dir), "--out", str(out),
                            "--apply", "--verify", "--min-free-gib", "0"])
    assert rc != 0
    assert not out.exists()
    assert not out.with_name(out.name + ".partial").exists()


def test_publish_refuses_lifetime_lock_held(source_dir, tmp_path):
    from opencrab.locking import acquire_file_lock, release_file_lock

    out = tmp_path / "merged.db"
    lock = acquire_file_lock("merged.db.lifetime.lock", str(tmp_path), shared=True)
    try:
        assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                               "--min-free-gib", "0"]) == 1
        assert not out.exists()
        assert out.with_name("merged.db.partial").exists()
    finally:
        release_file_lock(lock)


def test_configured_vector_symlink_outside_source_is_rejected(source_dir, tmp_path):
    outside = tmp_path / "outside.db"
    (source_dir / "vectors.db").rename(outside)
    (source_dir / "custom.db").symlink_to(outside)
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out),
                           "--vector-file", "custom.db"]) == 2
    assert not out.exists()


def test_configured_vector_file_is_used(source_dir, tmp_path):
    (source_dir / "vectors.db").rename(source_dir / "custom.db")
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                           "--verify", "--min-free-gib", "0", "--vector-file", "custom.db"]) == 0
    with _conn(out) as conn:
        assert conn.execute("SELECT count(*) FROM vectors_kure").fetchone()[0] == 40


@pytest.mark.parametrize("name", ["../vectors.db", "graph.db", "sub/vectors.db"])
def test_invalid_vector_file_is_rejected_before_output(source_dir, tmp_path, name):
    out = tmp_path / "merged.db"
    with pytest.raises(SystemExit):
        merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--vector-file", name])
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_finalize_refuses_existing_output(tmp_path):
    partial = tmp_path / "out.db.partial"
    output = tmp_path / "out.db"
    sqlite3.connect(partial).close()
    output.write_bytes(b"existing")
    with pytest.raises((FileExistsError, merge_mod.MergeError)):
        merge_mod.finalize(partial, output)
    assert output.read_bytes() == b"existing"
    assert partial.exists()


def test_finalize_refuses_dangling_output_symlink(tmp_path):
    partial = tmp_path / "out.db.partial"
    output = tmp_path / "out.db"
    sqlite3.connect(partial).close()
    output.symlink_to(tmp_path / "missing")
    with pytest.raises(merge_mod.MergeError):
        merge_mod.finalize(partial, output)
    assert output.is_symlink()
    assert partial.exists()


def test_finalize_refuses_hardlinked_partial(tmp_path):
    partial = tmp_path / "out.db.partial"
    output = tmp_path / "out.db"
    sqlite3.connect(partial).close()
    os.link(partial, tmp_path / "alias")
    with pytest.raises((ValueError, merge_mod.MergeError)):
        merge_mod.finalize(partial, output)
    assert not output.exists()
    assert partial.exists()


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.fixture
def source_dir(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    g = _conn(d / "graph.db")
    g.execute("CREATE TABLE graph_nodes (node_id TEXT PRIMARY KEY, props TEXT)")
    g.execute("CREATE INDEX idx_nodes_props ON graph_nodes(props)")
    g.executemany("INSERT INTO graph_nodes VALUES (?, ?)", [(f"n{i}", f"p{i % 3}") for i in range(30)])
    g.commit()
    g.execute("ANALYZE")  # 실제 스냅숏에 sqlite_stat1이 있었다: 예약 이름이라 DDL로 만들 수 없다
    g.close()

    s = _conn(d / "doc_store.db")
    s.execute("CREATE TABLE doc_sources (source_id TEXT PRIMARY KEY, text TEXT)")
    s.execute("CREATE TABLE seq_t (id INTEGER PRIMARY KEY AUTOINCREMENT, x)")
    s.execute("CREATE VIRTUAL TABLE doc_sources_fts USING fts5(source_id UNINDEXED, text, "
              "tokenize='unicode61 remove_diacritics 0')")
    s.execute("CREATE TRIGGER doc_sources_ai AFTER INSERT ON doc_sources BEGIN "
              "INSERT INTO seq_t(x) VALUES (new.source_id); END")
    for i in range(40):
        s.execute("INSERT INTO doc_sources VALUES (?, ?)", (f"s{i}", f"alpha beta 문서 {i}"))
        s.execute("INSERT INTO doc_sources_fts(source_id, text) VALUES (?, ?)",
                  (f"s{i}", f"alpha beta 문서 {i}"))
    s.execute("DELETE FROM seq_t WHERE id > 35")  # sqlite_sequence가 최댓값보다 크게 남는다
    s.commit()
    s.close()

    v = _conn(d / "vectors.db")
    v.execute("CREATE VIRTUAL TABLE vectors_kure USING vec0(node_id TEXT PRIMARY KEY, "
              "pack_id TEXT partition key, embedding float[4] distance_metric=cosine, +document TEXT)")
    for i in range(40):
        v.execute("INSERT INTO vectors_kure(node_id, pack_id, embedding, document) VALUES (?,?,?,?)",
                  (f"s{i}", "p1" if i % 2 else "p2", sqlite_vec.serialize_float32([i, 1, 0, 0.5]), f"d{i}"))
    v.commit()
    v.close()

    r = _conn(d / "opencrab.db")
    r.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY)")
    r.execute("INSERT INTO packs VALUES ('p1')")
    r.commit()
    r.close()
    return d


def test_plan_only_writes_nothing(source_dir, tmp_path, capsys):
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out)]) == 0
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()
    plan = json.loads(capsys.readouterr().out)
    assert plan["collisions"] == []


@pytest.mark.parametrize("name", ["single_file_metadata", "pack_delete_active"])
def test_plan_rejects_reserved_single_schema(source_dir, tmp_path, name):
    with sqlite3.connect(source_dir / "graph.db") as conn:
        conn.execute(f"CREATE TABLE {name} (id TEXT)")
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--min-free-gib", "0"]) == 2
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_source_fd_uri_binds_source_after_path_replacement(source_dir, tmp_path):
    path = source_dir / "graph.db"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    moved = tmp_path / "original.db"
    try:
        path.rename(moved)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE replacement (id INTEGER)")
        info = merge_mod.inspect_source(path, immutable=True, source_fds={"graph.db": fd})
        assert "graph_nodes" in info["regular"]
        assert "replacement" not in info["regular"]
    finally:
        os.close(fd)


def test_publication_evidence_binds_manifest_and_capture_times():
    manifest = json.dumps({"set_id": "set1", "created_at": "2026-01-01T00:00:03+00:00"}).encode()
    evidence = {
        "version": 1, "snapshot_id": "set1", "source_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "preparer_id": "test", "frozen_from": "2026-01-01T00:00:00Z",
        "frozen_until": "2026-01-01T00:00:02Z", "writer_stopped": True,
        "billing_same_snapshot": True, "packs_observation": "absent",
        "backup_at": "2026-01-01T00:00:03Z", "packs_checked_at": "2026-01-01T00:00:01Z",
        "capture_from": "2026-01-01T00:00:00Z", "capture_until": "2026-01-01T00:00:02Z",
    }
    assert merge_mod._publication_evidence(manifest, json.dumps(evidence).encode())["set_id"] == "set1"
    evidence["snapshot_id"] = "other"
    with pytest.raises(merge_mod.MergeError):
        merge_mod._publication_evidence(manifest, json.dumps(evidence).encode())


def test_publication_inputs_verify_all_required_files(source_dir):
    with sqlite3.connect(source_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE billing_events (id INTEGER)")
    names = [*merge_mod.FILES, "billing.db"]
    entries = [{"label": name, "path": name, "status": "verified", "capture": "copied_under_lock"}
               for name in names]
    entries += [{"label": name, "status": "skipped"}
                for name in ("chroma", "docs", "delete-pack-journals", "graph.kuzu", "packs")]
    metadata = {name: {"size": (source_dir / name).stat().st_size,
                       "sha256": hashlib.sha256((source_dir / name).read_bytes()).hexdigest()}
                for name in names}
    manifest = {"format": 1, "entries": entries, "files": metadata}
    manifest["format"] = 2
    with pytest.raises(merge_mod.MergeError, match="format"):
        with merge_mod._publication_inputs(source_dir, manifest, list(merge_mod.FILES)):
            pass
    manifest["format"] = 1
    with merge_mod._publication_inputs(source_dir, manifest, list(merge_mod.FILES)) as (fds, check_inputs):
        check_inputs()
        assert set(fds) == set(names)
        assert merge_mod.plan(source_dir, list(merge_mod.FILES), immutable=True, source_fds=fds)["collisions"] == []
    original = source_dir / "graph.db"
    with pytest.raises(merge_mod.MergeError, match="changed"):
        with merge_mod._publication_inputs(source_dir, manifest, list(merge_mod.FILES)) as (_, check_inputs):
            original.rename(source_dir / "moved.db")
            original.write_bytes(b"replacement")
            check_inputs()
    original.unlink()
    (source_dir / "moved.db").rename(original)
    metadata["billing.db"]["sha256"] = "invalid"
    with pytest.raises(merge_mod.MergeError, match="digest"):
        with merge_mod._publication_inputs(source_dir, manifest, list(merge_mod.FILES)):
            pass


def test_publication_inputs_rejects_unknown_manifest_format(source_dir):
    with pytest.raises(merge_mod.MergeError, match="format"):
        with merge_mod._publication_inputs(source_dir, {"format": 2}, list(merge_mod.FILES)):
            pass


def test_publication_main_prepares_before_staging(source_dir, tmp_path, monkeypatch):
    with sqlite3.connect(source_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE billing_events (id INTEGER)")
    names = [*merge_mod.FILES, "billing.db"]
    entries = [{"label": name, "path": name, "status": "verified", "capture": "copied_under_lock"}
               for name in names]
    entries += [{"label": name, "status": "skipped"}
                for name in ("chroma", "docs", "delete-pack-journals", "graph.kuzu", "packs")]
    metadata = {name: {"size": (source_dir / name).stat().st_size,
                       "sha256": hashlib.sha256((source_dir / name).read_bytes()).hexdigest()}
                for name in names}
    raw = json.dumps({"format": 1, "entries": entries, "files": metadata, "set_id": "set1",
                      "created_at": "2026-01-01T00:00:03Z"}).encode()
    (source_dir / "MANIFEST.json").write_bytes(raw)
    (tmp_path / "src.source.lifetime.lock").touch()
    evidence = {"version": 1, "snapshot_id": "set1", "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "preparer_id": "test", "frozen_from": "2026-01-01T00:00:00Z",
                "frozen_until": "2026-01-01T00:00:02Z", "writer_stopped": True,
                "billing_same_snapshot": True, "packs_observation": "absent",
                "backup_at": "2026-01-01T00:00:03Z", "packs_checked_at": "2026-01-01T00:00:01Z",
                "capture_from": "2026-01-01T00:00:00Z", "capture_until": "2026-01-01T00:00:02Z"}
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence))
    root = tmp_path / "published"
    staging = tmp_path / "published.staging"
    ledger = tmp_path / "published.intent.json"
    args = ["--source-dir", str(source_dir), "--publication-root", str(root), "--evidence-file",
            str(evidence_path), "--apply", "--verify", "--immutable-source", "--min-free-gib", "0"]
    original_mkdir = os.mkdir

    def check_order(path, *args, **kwargs):
        if Path(path) == staging:
            assert json.loads(ledger.read_text())["state"] == "preparing"
            assert not root.exists()
        return original_mkdir(path, *args, **kwargs)

    original_prepare = merge_mod._prepare_publication

    def fail_prepare(*args, **kwargs):
        raise OSError("injected ledger failure")

    monkeypatch.setattr(merge_mod, "_prepare_publication", fail_prepare)
    assert merge_mod.main(args) != 0
    assert not staging.exists()
    assert not ledger.exists()
    monkeypatch.setattr(merge_mod, "_prepare_publication", original_prepare)
    monkeypatch.setattr(os, "mkdir", check_order)
    assert merge_mod.main(args) == 0
    assert json.loads(ledger.read_text())["state"] == "ready"
    assert root.is_dir()
    assert not staging.exists()
    assert merge_mod.main(args) != 0


@pytest.mark.parametrize("failure", [None, "verify", "checkpoint", "sidecar", "wal", "sync", "busy", "frames", "close", "busy_close", "owner_close", "owner_close_systemexit", "owner_close_exception", "open:staging-parent", "open:merge-partial", "open:checkpoint-fsync", "open:staging-dir", "open:billing-write", "open:billing-read", "open:checkpoint-fsync#2", "connect:billing.db", "connect:single.db.partial", "close:billing.db", "close:single.db.partial", "attach:merge", "detach:merge", "mkdir:before", "mkdir:after", "link:before", "link:after", "unlink:before", "unlink:after", "fsync:intent-parent", "fsync:staging-parent", "fsync:staging-dir", "fsync:checkpoint-fsync", "fsync:billing-write", "fsync-after:intent-parent", "fsync-after:staging-parent", "fsync-after:staging-dir", "fsync-after:checkpoint-fsync", "fsync-after:billing-write", "fsync:intent-stream", "fsync-after:intent-stream", "fsync:checkpoint-fsync#2", "fsync-after:checkpoint-fsync#2", "checkpoint-close:0:before", "checkpoint-close:0:after", "checkpoint-close:1:before", "checkpoint-close:1:after", "observe:root", "observe:staging", "observe:intent", "observe-after:mkdir", "observe-after:link", "observe-after:unlink", "create-after:intent", "create-after:merge-partial", "create-after:billing-write", "create-observe:intent", "create-observe:merge-partial", "create-observe:billing-write", "multi-close", "attach-after-close-fail", "detach-after-close-fail", "close-instance:0", "intent-write", "intent-flush", "intent-write-after", "intent-flush-after", "input-open:input-root", "input-open:source:billing.db", "input-open:source:graph.db", "input-open:source:doc_store.db", "input-open:source:vectors.db", "input-open:source:opencrab.db", "input-open:directory:chroma", "input-open:directory:docs", "input-open:directory:delete-pack-journals", "input-connect:integrity", "input-connect:memory", "input-attach:before", "input-attach:after", "input-detach:before", "input-detach:after", "flock-before:source", "flock-before:root", "flock-after:source", "flock-after:root", "vec:attach-before", "vec:attach-after", "vec:detach-before", "vec:detach-after", "release:source-unlock-before", "release:source-unlock-after", "release:source-close-before", "release:source-close-after", "release:root-unlock-before", "release:root-unlock-after", "release:root-close-before", "release:root-close-after", "release:registry-before", "release:registry-after", "root-fdopen-before", "root-fdopen-after", "intent-fdopen-before", "intent-fdopen-after", "registry-add-before", "registry-add-after", "mounts:open-before", "mounts:open-after", "mounts:fdopen-before", "mounts:fdopen-after", "mounts:read-before", "mounts:read-after", "mounts:close-before", "mounts:close-after"])
def test_publication_main_merges_generated_backup(source_dir, tmp_path, monkeypatch, failure, capsys):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from opencrab.stores.backup import backup_data_dir

    if os.environ.get("PUBLICATION_MULTI_VEC"):
        with _conn(source_dir / "vectors.db") as conn:
            conn.execute("CREATE VIRTUAL TABLE vectors_second USING vec0(node_id TEXT PRIMARY KEY, "
                         "pack_id TEXT partition key, embedding float[4] distance_metric=cosine, +document TEXT)")
            conn.execute("INSERT INTO vectors_second(node_id, pack_id, embedding, document) VALUES (?,?,?,?)",
                         ("second", "p1", sqlite_vec.serialize_float32([1, 1, 0, 0.5]), "second"))
    with sqlite3.connect(source_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE billing_events (id INTEGER)")
        conn.execute("INSERT INTO billing_events VALUES (42)")
    if failure is not None and failure.startswith("input-open:directory:"):
        (source_dir / failure.rsplit(":", 1)[1]).mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    begin = datetime.now(timezone.utc) - timedelta(seconds=2)
    backup = backup_data_dir(source_dir, backups, settings=SimpleNamespace(vector_db_file="vectors.db"))
    end = datetime.now(timezone.utc) + timedelta(seconds=1)
    backup_root = backup.set_dir
    (backups / (backup_root.name + ".source.lifetime.lock")).touch()
    raw = (backup_root / "MANIFEST.json").read_bytes()
    evidence = {"version": 1, "snapshot_id": backup.manifest["set_id"],
                "source_manifest_sha256": hashlib.sha256(raw).hexdigest(), "preparer_id": "test",
                "frozen_from": begin.isoformat(), "frozen_until": end.isoformat(), "writer_stopped": True,
                "billing_same_snapshot": True, "packs_observation": "absent",
                "backup_at": backup.manifest["created_at"], "packs_checked_at": begin.isoformat(),
                "capture_from": begin.isoformat(), "capture_until": begin.isoformat()}
    evidence_path = tmp_path / "evidence-generated.json"
    evidence_path.write_text(json.dumps(evidence))
    root = tmp_path / "published-generated"
    args = ["--source-dir", str(backup_root), "--publication-root", str(root), "--evidence-file",
            str(evidence_path), "--apply", "--verify", "--immutable-source", "--min-free-gib", "0"]
    failure = os.environ.get("PUBLICATION_TEST_TARGET", failure)
    calls = []
    close_events = []
    peer_events = []
    actual_connections = []
    connection_instances = []
    target_instance = None
    attach_events = []
    attach_target = None
    if failure is not None and failure.startswith("attach-key:"):
        attach_target = tuple(json.loads(failure.removeprefix("attach-key:")))
    if failure is not None and failure.startswith(("close-key:", "close-after-key:", "connect-after-key:")):
        target_instance = json.loads(failure.split(":", 1)[1])
        target_instance = (tuple(target_instance[0]), target_instance[1], target_instance[2])
    fd_roles = []
    owners = []
    original_init = merge_mod._PublicationOwner.__init__

    def owner_init(owner, *args, **kwargs):
        original_init(owner, *args, **kwargs)
        owners.append(owner)

    monkeypatch.setattr(merge_mod._PublicationOwner, "__init__", owner_init)
    original_open_fd = merge_mod._PublicationOwner.open_fd

    def open_fd_spy(owner, role, *args, **kwargs):
        fd_roles.append(role)
        if role == "mounts" and failure in {"mounts:open-before", "mounts:open-after"}:
            original_open = merge_mod.os.open

            def fail_mount_open(*args, **kwargs):
                if failure.endswith("after"):
                    original_close = original_open(*args, **kwargs)
                    os.close(original_close)
                raise OSError("injected mounts open uncertainty")

            merge_mod.os.open = fail_mount_open
            try:
                return original_open_fd(owner, role, *args, **kwargs)
            finally:
                merge_mod.os.open = original_open
        if (failure in {"open:" + role, "input-open:" + role, "create-after:" + role, "create-observe:" + role}
                or (failure == "open:" + role + "#2" and fd_roles.count(role) == 2)):
            original_open = merge_mod.os.open

            def fail_open(*args, **kwargs):
                if failure.startswith(("create-after:", "create-observe:")):
                    leaked_fd = original_open(*args, **kwargs)
                    os.close(leaked_fd)
                    if failure.startswith("create-observe:"):
                        fail_observation.append(Path(args[0]))
                raise OSError("injected main open uncertain")

            merge_mod.os.open = fail_open
            try:
                return original_open_fd(owner, role, *args, **kwargs)
            finally:
                merge_mod.os.open = original_open
        return original_open_fd(owner, role, *args, **kwargs)

    monkeypatch.setattr(merge_mod._PublicationOwner, "open_fd", open_fd_spy)
    original_flock = merge_mod.fcntl.flock
    release_events = []

    def flock_spy(fd, operation):
        if failure and failure.startswith("release:") and operation == merge_mod.fcntl.LOCK_UN:
            release_events.append(("unlock", fd))
        owner = getattr(merge_mod._publication_current, "owner", None)
        if owner and failure and failure.startswith("release:") and operation == merge_mod.fcntl.LOCK_UN:
            source_fd = owner.fd_slots.get("source-lock")
            kind = "source" if fd == source_fd else "root"
            if failure in {f"release:{kind}-unlock-before", f"release:{kind}-unlock-after"}:
                if failure.endswith("after"):
                    original_flock(fd, operation)
                raise OSError("injected unlock uncertainty")
        if (failure == "flock-after:source" and operation == merge_mod.fcntl.LOCK_SH | merge_mod.fcntl.LOCK_NB) or (
                failure == "flock-after:root" and operation == merge_mod.fcntl.LOCK_EX | merge_mod.fcntl.LOCK_NB):
            original_flock(fd, operation)
            raise OSError("injected flock after effect")
        if (failure == "flock-before:source" and operation == merge_mod.fcntl.LOCK_SH | merge_mod.fcntl.LOCK_NB) or (
                failure == "flock-before:root" and operation == merge_mod.fcntl.LOCK_EX | merge_mod.fcntl.LOCK_NB):
            raise merge_mod._PublicationFlockBeforeTest("injected before flock")
        return original_flock(fd, operation)

    monkeypatch.setattr(merge_mod.fcntl, "flock", flock_spy)
    real_os_fdopen = os.fdopen
    orphan_streams = []

    def os_fdopen_spy(fd, mode):
        if mode == "r" and failure in {"mounts:fdopen-before", "mounts:fdopen-after"}:
            if failure.endswith("after"):
                orphan_streams.append(real_os_fdopen(fd, mode))
            raise OSError("injected mounts fdopen uncertainty")
        point = "root" if mode == "r+b" else "intent" if mode == "wb" else None
        if failure in {f"{point}-fdopen-before", f"{point}-fdopen-after"}:
            if failure.endswith("after"):
                orphan_streams.append(real_os_fdopen(fd, mode))
            raise OSError("injected fdopen uncertain")
        return real_os_fdopen(fd, mode)

    monkeypatch.setattr(os, "fdopen", os_fdopen_spy)
    original_fdopen = merge_mod._PublicationOwner.fdopen

    class IntentStream:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def write(self, data):
            if failure == "intent-write":
                raise OSError("injected intent write")
            result = self.inner.write(data)
            if failure == "intent-write-after":
                raise OSError("injected intent write after")
            return result

        def flush(self):
            if failure == "intent-flush":
                raise OSError("injected intent flush")
            result = self.inner.flush()
            if failure == "intent-flush-after":
                raise OSError("injected intent flush after")
            return result

        def close(self):
            return self.inner.close()

    def fdopen_spy(owner, role, fd, mode, **kwargs):
        stream = original_fdopen(owner, role, fd, mode, **kwargs)
        if role == "intent" and failure in {"intent-write", "intent-flush", "intent-write-after", "intent-flush-after"}:
            stream = IntentStream(stream)
            owner.streams[role] = stream
        return stream

    monkeypatch.setattr(merge_mod._PublicationOwner, "fdopen", fdopen_spy)
    original_fdopen_pair = merge_mod._PublicationOwner.fdopen_pair

    def fdopen_pair_spy(owner, role, fd, mode, **kwargs):
        assert role == "mounts"
        assert not owner.connections
        assert owner.fd_slots.get("source-lock") is not None and owner.source_locked
        assert owner.root_lock is None
        key, stream = original_fdopen_pair(owner, role, fd, mode, **kwargs)
        if failure in {"mounts:read-before", "mounts:read-after", "mounts:close-before", "mounts:close-after"}:
            class MountStream:
                def read(self):
                    if failure == "mounts:read-before":
                        raise OSError("injected mounts read failure")
                    result = stream.read()
                    if failure == "mounts:read-after":
                        raise OSError("injected mounts read failure")
                    return result

                def close(self):
                    if failure == "mounts:close-before":
                        raise OSError("injected mounts close uncertainty")
                    stream.close()
                    if failure == "mounts:close-after":
                        raise OSError("injected mounts close uncertainty")

            owner.streams[key] = MountStream()
            owner.test_mount_stream = stream
            return key, owner.streams[key]
        return key, stream

    monkeypatch.setattr(merge_mod._PublicationOwner, "fdopen_pair", fdopen_pair_spy)
    original_release_root = merge_mod._PublicationOwner.release_root_lock

    def release_root_spy(owner, **kwargs):
        if failure in {"release:root-close-before", "release:root-close-after"} and owner.root_lock:
            original_handle = owner.root_lock
            owner.test_original_root = original_handle

            class RootCloseProxy:
                def fileno(self):
                    return original_handle.fileno()

                def close(self):
                    release_events.append(("root-close", None))
                    if failure.endswith("after"):
                        original_handle.close()
                    raise OSError("injected root close uncertainty")

            owner.root_lock = RootCloseProxy()
        return original_release_root(owner, **kwargs)

    monkeypatch.setattr(merge_mod._PublicationOwner, "release_root_lock", release_root_spy)
    if failure in {"release:registry-before", "release:registry-after", "registry-add-before", "registry-add-after"}:
        from opencrab import locking

        class RegistryFailure(dict):
            def __setitem__(self, key, value):
                if failure.startswith("registry-add"):
                    if failure.endswith("after"):
                        super().__setitem__(key, value)
                    raise OSError("injected registry uncertainty")
                return super().__setitem__(key, value)

            def __delitem__(self, key):
                release_events.append(("registry-remove", None))
                if failure.endswith("after"):
                    super().__delitem__(key)
                raise OSError("injected registry uncertainty")

        monkeypatch.setattr(locking, "_lifetime_handles", RegistryFailure(locking._lifetime_handles))
    original_close = os.close

    fd_close_events = []

    def close_spy(fd):
        owner = getattr(merge_mod._publication_current, "owner", None)
        if owner and failure and failure.startswith("checkpoint-close:"):
            _, ordinal, timing = failure.split(":")
            keys = owner.fd_roles.get("checkpoint-fsync", [])
            index = int(ordinal)
            if len(keys) > index and owner.fd_slots[keys[index]] == fd:
                fd_close_events.append(fd)
                if timing == "after":
                    original_close(fd)
                raise OSError("injected checkpoint fd close uncertainty")
        if owner and failure and failure.startswith("release:") and fd == owner.fd_slots.get("source-lock"):
            release_events.append(("source-close", fd))
        if owner and fd == owner.fd_slots.get("source-lock") and failure in {
                "release:source-close-before", "release:source-close-after"}:
            if failure.endswith("after"):
                original_close(fd)
            raise OSError("injected source close uncertainty")
        return original_close(fd)

    monkeypatch.setattr(os, "close", close_spy)
    original_lstat = os.lstat
    fail_observation = []

    def lstat_spy(path, *args, **kwargs):
        targets = {"observe:root": root, "observe:staging": root.with_name(root.name + ".staging"),
                   "observe:intent": root.with_name(root.name + ".intent.json")}
        if (failure in targets and path == targets[failure]) or (fail_observation and path == fail_observation[0]):
            raise PermissionError("injected observation failure")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat_spy)
    original_mkdir = os.mkdir
    original_link = os.link
    original_unlink = Path.unlink
    original_fsync = os.fsync

    def fsync_spy(fd):
        owner = getattr(merge_mod._publication_current, "owner", None)
        if owner and failure is not None and failure.startswith(("fsync:", "fsync-after:")):
            after = failure.startswith("fsync-after:")
            role = failure.split(":", 1)[1]
            if role == "intent-stream":
                stream = owner.streams.get("intent")
                matches = stream is not None and fd == stream.fileno()
            elif role.endswith("#2"):
                keys = owner.fd_roles.get(role.removesuffix("#2"), [])
                matches = len(keys) >= 2 and fd == owner.fd_slots[keys[1]]
            else:
                matches = fd in [owner.fd_slots[key] for key in owner.fd_roles.get(role, [])]
            if matches:
                if after:
                    original_fsync(fd)
                raise OSError("injected fsync uncertain")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_spy)

    def mkdir_spy(path, *args, **kwargs):
        if failure == "mkdir:before" and Path(path).name == root.name + ".staging":
            raise OSError("injected mkdir before")
        result = original_mkdir(path, *args, **kwargs)
        if failure in {"mkdir:after", "observe-after:mkdir"} and Path(path).name == root.name + ".staging":
            if failure == "observe-after:mkdir":
                fail_observation.append(Path(path))
            raise OSError("injected mkdir after")
        return result

    def link_spy(src, dst, *args, **kwargs):
        if failure == "link:before" and Path(dst).name == "single.db":
            raise OSError("injected link before")
        result = original_link(src, dst, *args, **kwargs)
        if failure in {"link:after", "observe-after:link"} and Path(dst).name == "single.db":
            if failure == "observe-after:link":
                fail_observation.append(Path(dst))
            raise OSError("injected link after")
        return result

    def unlink_spy(path, *args, **kwargs):
        if failure == "unlink:before" and path.name == "single.db.partial":
            raise OSError("injected unlink before")
        result = original_unlink(path, *args, **kwargs)
        if failure in {"unlink:after", "observe-after:unlink"} and path.name == "single.db.partial":
            if failure == "observe-after:unlink":
                fail_observation.append(path)
            raise OSError("injected unlink after")
        return result

    monkeypatch.setattr(os, "mkdir", mkdir_spy)
    monkeypatch.setattr(os, "link", link_spy)
    monkeypatch.setattr(Path, "unlink", unlink_spy)
    real_sqlite_connect = sqlite3.connect

    class InjectedConnection:
        def __init__(self, inner, phase=None):
            self.inner = inner
            self.phase = phase

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            if sql.startswith(("ATTACH DATABASE", "DETACH DATABASE")):
                verb = sql.split()[0]
                phase = self.phase
                ordinal = sum(event[:2] == (phase, verb) for event in attach_events)
                event = (phase, verb, ordinal)
                attach_events.append(event)
                if attach_target is not None and event == attach_target[:3]:
                    if attach_target[3] == "after":
                        self.inner.execute(sql, *args)
                    raise sqlite3.OperationalError("injected attachment uncertainty")
            if failure is not None and failure.startswith("vec:"):
                _, point = failure.split(":", 1)
                verb, timing = point.split("-")
                if sql.startswith(verb.upper() + " DATABASE"):
                    if timing == "after":
                        self.inner.execute(sql, *args)
                    raise sqlite3.OperationalError("injected vec attachment failure")
            if failure in {"input-attach:before", "input-attach:after", "input-detach:before", "input-detach:after"}:
                operation, timing = failure.split(":")
                verb = "ATTACH" if operation == "input-attach" else "DETACH"
                if sql.startswith(verb + " DATABASE"):
                    if timing == "after":
                        self.inner.execute(sql, *args)
                    raise sqlite3.OperationalError("injected input attachment failure")
            if failure in {"attach-after-close-fail", "detach-after-close-fail"} and sql.startswith("ATTACH DATABASE"):
                self.inner.execute(sql, *args)
                close_events.append(("attached", self.inner.execute("PRAGMA database_list").fetchall()))
                if failure == "attach-after-close-fail":
                    raise sqlite3.OperationalError("injected attach after effect")
                return None
            if failure == "detach-after-close-fail" and sql.startswith("DETACH DATABASE"):
                self.inner.execute(sql, *args)
                close_events.append(("detached", self.inner.execute("PRAGMA database_list").fetchall()))
                raise sqlite3.OperationalError("injected detach after effect")
            if failure == "attach:merge" and sql.startswith("ATTACH DATABASE"):
                raise sqlite3.OperationalError("injected attach failure")
            if failure == "detach:merge" and sql.startswith("DETACH DATABASE"):
                raise sqlite3.OperationalError("injected detach failure")
            return self.inner.execute(sql, *args)

        def close(self):
            if attach_target is not None and len(attach_target) == 5 and attach_target[4] == "close-fail" and attach_target[:3] in attach_events:
                close_events.append(self.inner)
                raise sqlite3.OperationalError("injected attachment close failure")
            if failure is not None and failure.startswith("close-after-key:"):
                close_events.append("close-after")
                self.inner.close()
                raise sqlite3.OperationalError("injected close uncertain")
            if failure is not None and failure.startswith(("close-instance:", "close-key:")):
                raise sqlite3.OperationalError("injected close uncertain")
            if failure in {"attach-after-close-fail", "detach-after-close-fail"}:
                close_events.append(("failed-close", self.inner))
                raise sqlite3.OperationalError("injected close uncertain")
            if failure is not None and failure.startswith("close:"):
                raise sqlite3.OperationalError("injected close uncertain")
            return self.inner.close()

    def sqlite_connect_spy(database, *args, **kwargs):
        import inspect

        if getattr(merge_mod._publication_current, "owner", None) is not None:
            actual_connections.append(str(database))
            frames = inspect.stack()
            phases = tuple(frame.function for frame in frames if frame.filename == str(merge_mod.__file__)
                           and frame.function in {"_publication_inputs", "plan", "merge", "verify",
                                                  "inspect_source", "_publication_checkpoint", "main"})
            role = ("memory" if str(database) == ":memory:" else
                    "source" if "/proc/self/fd/" in str(database) else
                    Path(str(database).split("?", 1)[0]).name)
            base = (phases, role)
            ordinal = sum(item[:2] == base for item in connection_instances)
            connection_instances.append((phases, role, ordinal))
        if failure in {"input-connect:integrity", "input-connect:memory"}:
            if (failure.endswith("memory") and str(database) == ":memory:") or (
                    failure.endswith("integrity") and "/proc/self/fd/" in str(database)):
                raise sqlite3.OperationalError("injected input connect failure")
        if (failure is not None and failure.startswith("connect:")
                and str(database).startswith("file:")
                and failure.removeprefix("connect:") in str(database)):
            raise sqlite3.OperationalError("injected connect uncertain")
        conn = real_sqlite_connect(database, *args, **kwargs)
        if failure is not None and failure.startswith("connect-after-key:") and connection_instances[-1] == target_instance:
            conn.close()
            raise sqlite3.OperationalError("injected connect after effect")
        if (os.environ.get("PUBLICATION_ATTACH_INVENTORY") or attach_target is not None) and connection_instances:
            phases, role_name, ordinal = connection_instances[-1]
            if phases[:1] == ("_publication_inputs",) and role_name == "memory":
                return InjectedConnection(conn, "inputs")
            if phases[:1] == ("merge",) and role_name == "single.db.partial":
                return InjectedConnection(conn, "merge")
            if phases[:1] == ("verify",) and role_name == "single.db.partial" and ordinal == 1:
                return InjectedConnection(conn, "verify")
        if (failure is not None and failure.startswith("vec:")
                and connection_instances[-1][0][:1] == ("verify",)
                and connection_instances[-1][1:] == ("single.db.partial", 1)):
            return InjectedConnection(conn)
        if failure in {"input-attach:before", "input-attach:after", "input-detach:before", "input-detach:after"} and str(database) == ":memory:":
            return InjectedConnection(conn)
        if target_instance is not None and connection_instances[-1] == target_instance:
            return InjectedConnection(conn)
        if failure is not None and failure.startswith("close-instance:"):
            if len(actual_connections) - 1 == int(failure.split(":", 1)[1]):
                return InjectedConnection(conn)
        if failure is not None and str(database).startswith("file:"):
            if (failure.startswith("close:") and failure.removeprefix("close:") in str(database)) or (
                    failure in {"attach:merge", "detach:merge", "attach-after-close-fail", "detach-after-close-fail"} and "single.db.partial" in str(database)):
                return InjectedConnection(conn)
        return conn

    monkeypatch.setattr(merge_mod.sqlite3, "connect", sqlite_connect_spy)
    original_verify = merge_mod.verify

    def verify_spy(*args, **kwargs):
        calls.append(kwargs["source_fds"])
        work = kwargs["work"]
        if failure == "multi-close":
            owner = merge_mod._publication_current.owner
            actual = [sqlite3.connect(":memory:") for _ in range(3)]

            class CloseProbe:
                def __init__(self, index):
                    self.index = index
                    self.actual = actual[index]

                def close(self):
                    close_events.append(self.index)
                    if self.index == 1:
                        raise RuntimeError("injected close uncertain")
                    self.actual.close()

            for index in range(3):
                owner.register(CloseProbe(index), work=work)
            raise KeyboardInterrupt("multi primary")
        if failure in {"owner_close", "owner_close_systemexit", "owner_close_exception"}:
            owner = merge_mod._publication_current.owner
            actual = sqlite3.connect(":memory:")

            class CloseBefore:
                def close(self):
                    raise RuntimeError("injected cleanup close")

            proxy = CloseBefore()
            proxy.actual = actual
            owner.register(proxy, work=work)
            primary = {"owner_close": KeyboardInterrupt, "owner_close_systemexit": SystemExit,
                       "owner_close_exception": ValueError}[failure]
            raise primary("injected primary")
        if failure == "verify":
            raise merge_mod.MergeError("injected verify failure")
        return original_verify(*args, **kwargs)

    original_checkpoint = merge_mod._publication_checkpoint
    original_connect = merge_mod._connect
    checkpoint_active = []

    def checkpoint_spy(path, metadata=None, **kwargs):
        if failure == "checkpoint":
            raise merge_mod.MergeError("injected checkpoint failure")
        checkpoint_active.append(True)
        try:
            return original_checkpoint(path, metadata, **kwargs)
        finally:
            checkpoint_active.pop()

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class SidecarConnection:
        def __init__(self, conn, path):
            self.conn = conn
            self.path = path

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if failure == "wal" and sql == "PRAGMA journal_mode=WAL":
                return Result(("delete",))
            if failure == "sync" and sql == "PRAGMA synchronous":
                return Result((1,))
            if sql == "PRAGMA wal_checkpoint(TRUNCATE)":
                if failure in {"busy", "busy_close"}:
                    return Result((1, 1, 0))
                if failure == "frames":
                    return Result((0, 2, 1))
            return result

        def close(self, **kwargs):
            self.conn.close(**kwargs)
            if failure == "sidecar":
                Path(str(self.path) + "-journal").write_bytes(b"residue")
            if failure in {"close", "busy_close"}:
                raise sqlite3.OperationalError("injected close failure")

    def connect_spy(path, **kwargs):
        conn = original_connect(path, **kwargs)
        if failure in {"sidecar", "wal", "sync", "busy", "frames", "close", "busy_close"} and checkpoint_active and path.name == "single.db.partial" and kwargs.get("mode") == "rw":
            return SidecarConnection(conn, path)
        if failure in {"attach-after-close-fail", "detach-after-close-fail"} and path.name == "single.db.partial" and kwargs.get("mode") == "rw" and not peer_events:
            owner = owners[-1]
            assert owner.connections.count(conn) == 1 and not conn.failed
            peer = real_sqlite_connect(":memory:")

            class Peer:
                def close(self):
                    close_events.append(("peer-closed", None))
                    peer.close()

            owner.register(Peer(), work=kwargs["work"])
            assert owner.connections[-1].conn.__class__ is Peer and owner.connections[-2] is conn
            peer_events.append(("registered", [wrapper.failed for wrapper in owner.connections]))
        return conn

    monkeypatch.setattr(merge_mod, "verify", verify_spy)
    monkeypatch.setattr(merge_mod, "_publication_checkpoint", checkpoint_spy)
    if failure in {"sidecar", "wal", "sync", "busy", "frames", "close", "busy_close",
                   "attach-after-close-fail", "detach-after-close-fail"}:
        monkeypatch.setattr(merge_mod, "_connect", connect_spy)
    if failure in {"attach-after-close-fail", "detach-after-close-fail"}:
        import gc
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        gc.collect()
        owner = merge_mod._publication_owners[str(root)]
        assert owner.uncertain and len(owner.connections) == 1
        assert owner.connections[0].failed
        attached = close_events[0][1]
        assert any(row[1] == "src" for row in attached)
        index = 1
        if failure == "detach-after-close-fail":
            assert close_events[index][0] == "detached"
            assert all(row[1] != "src" for row in close_events[index][1])
            index += 1
        assert close_events[index][0] == "failed-close"
        assert close_events[index + 1][0] == "peer-closed"
        assert owner.connections[0].conn.inner is close_events[index][1]
        assert peer_events == [("registered", [False, False])]
        assert owner.fd_slots["source-lock"] is not None and owner.root_lock is not None
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in (backups / (backup_root.name + ".source.lifetime.lock"),
                     tmp_path / (root.name + ".lifetime.lock")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr
        owner.connections[0].conn.inner.close()
        from opencrab.locking import release_file_lock
        release_file_lock(owner.root_lock)
        for stream in owner.streams.values():
            if stream is not None:
                stream.close()
        for fd in owner.fd_slots.values():
            if fd is not None:
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure == "multi-close":
        import gc
        import subprocess
        import sys

        with pytest.raises(KeyboardInterrupt, match="multi primary") as error:
            merge_mod.main(args)
        assert str(error.value.__cause__) == "injected close uncertain"
        assert close_events == [0, 1, 2]
        gc.collect()
        owner = merge_mod._publication_owners[str(root)]
        assert owner.uncertain and len(owner.connections) == 1 and owner.connections[0].failed
        assert owner.fd_slots["source-lock"] is not None and owner.root_lock is not None
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in (backups / (backup_root.name + ".source.lifetime.lock"),
                     tmp_path / (root.name + ".lifetime.lock")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr
        owner.connections[0].conn.actual.close()
        from opencrab.locking import release_file_lock
        release_file_lock(owner.root_lock)
        for stream in owner.streams.values():
            if stream is not None:
                stream.close()
        for fd in owner.fd_slots.values():
            if fd is not None:
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure in {"owner_close", "owner_close_systemexit", "owner_close_exception"}:
        import gc
        import subprocess
        import sys

        if failure == "owner_close_exception":
            assert merge_mod.main(args) == 2
            stderr = capsys.readouterr().err
            assert "injected primary" in stderr
            assert "publication error cause: RuntimeError" in stderr
            assert "injected cleanup close" not in stderr
        else:
            primary = SystemExit if failure == "owner_close_systemexit" else KeyboardInterrupt
            with pytest.raises(primary, match="injected primary") as error:
                merge_mod.main(args)
            assert isinstance(error.value.__cause__, RuntimeError)
            assert str(error.value.__cause__) == "injected cleanup close"
        gc.collect()
        owner = merge_mod._publication_owners[str(root)]
        assert owner.uncertain and owner.connections and owner.root_lock
        assert str(owner.cleanup_error) == "injected cleanup close"
        paths = [backups / (backup_root.name + ".source.lifetime.lock"),
                 tmp_path / (root.name + ".lifetime.lock")]
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in paths:
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr
        assert merge_mod.main(args) != 0
        owner.connections[0].conn.actual.close()
        from opencrab.locking import release_file_lock
        release_file_lock(owner.root_lock)
        for fd in owner.fd_slots.values():
            if fd is not None:
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure is not None and failure.startswith("mounts:"):
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        owner = owners[-1]
        assert not root.exists()
        assert not root.with_name(root.name + ".intent.json").exists()
        assert not root.with_name(root.name + ".staging").exists()
        assert owner.root_lock is None and not owner.connections
        if failure.startswith("mounts:read-"):
            assert not owner.uncertain and str(root) not in merge_mod._publication_owners
            assert owner.test_mount_stream.closed
        else:
            assert owner.uncertain and merge_mod._publication_owners[str(root)] is owner
            command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
            result = subprocess.run([*command, str(backups / (backup_root.name + ".source.lifetime.lock"))], capture_output=True, text=True, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr
            assert merge_mod.main(args) != 0
            if failure == "mounts:close-before":
                owner.test_mount_stream.close()
            if failure.startswith("mounts:fdopen"):
                if orphan_streams:
                    orphan_streams[0].close()
                else:
                    os.close(owner.fd_slots["mounts"])
            for key, fd in owner.fd_slots.items():
                if fd is not None and not (key == "mounts" and failure.startswith("mounts:fdopen")):
                    os.close(fd)
            del merge_mod._publication_owners[str(root)]
        return
    if failure is not None and failure.startswith("checkpoint-close:"):
        import subprocess
        import sys

        assert merge_mod.main(args) == 2
        _assert_ready_cleanup_failed(capsys.readouterr().err)
        owner = merge_mod._publication_owners[str(root)]
        _, ordinal, timing = failure.split(":")
        key = owner.fd_roles["checkpoint-fsync"][int(ordinal)]
        failed_fd = owner.fd_slots[key]
        assert owner.uncertain and fd_close_events == [failed_fd]
        assert owner.fd_slots["source-lock"] is not None
        _assert_ready_published(root)
        assert merge_mod.main(args) != 0
        assert fd_close_events == [failed_fd]
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        result = subprocess.run([*command, str(backups / (backup_root.name + ".source.lifetime.lock"))], capture_output=True, text=True, check=False)
        assert result.returncode != 0 and "BlockingIOError" in result.stderr
        for slot_key, fd in owner.fd_slots.items():
            if fd is not None and not (slot_key == key and timing == "after"):
                original_close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure is not None and failure.startswith(("open:", "connect:", "connect-after-key:", "close:", "close-instance:", "close-key:", "close-after-key:", "create-after:", "create-observe:")):
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        owner = merge_mod._publication_owners[str(root)]
        role = failure.split(":", 1)[1].split("#", 1)[0]
        assert owner.uncertain
        if failure.startswith(("open:", "create-after:", "create-observe:")):
            assert owner.root_lock is not None
            assert owner.fd_slots[owner.fd_roles[role][-1]] is None
            if failure.startswith("create-after:"):
                assert list(owner.effect_observations[-1].values()) == [True]
            if failure.startswith("create-observe:"):
                assert list(owner.effect_observations[-1].values()) == [None]
                monkeypatch.setattr(os, "lstat", original_lstat)
        elif failure.startswith(("connect:", "connect-after-key:")):
            assert any(conn.failed and conn.conn is None for conn in owner.connections)
        else:
            assert any(conn.failed and conn.conn is not None for conn in owner.connections)
        if failure.startswith(("close-instance:", "close-key:", "close-after-key:")):
            if failure.startswith("close-after-key:"):
                import gc

                gc.collect()
                assert owner.connections[0].conn.inner is not None
                assert close_events == ["close-after"]
                assert merge_mod.main(args) != 0
                assert close_events == ["close-after"]
            if target_instance is not None:
                assert target_instance in connection_instances
            failed_connections = [conn for conn in owner.connections if conn.failed]
            assert len(failed_connections) == 1
            assert owner.fd_slots.get("source-lock") is not None
            command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
            lock_paths = [backups / (backup_root.name + ".source.lifetime.lock")]
            if owner.root_lock is not None:
                lock_paths.append(tmp_path / (root.name + ".lifetime.lock"))
            for path in lock_paths:
                result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
                assert result.returncode != 0 and "BlockingIOError" in result.stderr
            assert not root.exists()
            assert all(conn.failed for conn in owner.connections)
            staging_path = root.with_name(root.name + ".staging")
            if owner.root_lock is None:
                assert not root.with_name(root.name + ".intent.json").exists()
                assert not staging_path.exists()
            else:
                assert root.with_name(root.name + ".intent.json").exists()
                assert staging_path.is_dir()
                phases, role_name, _ordinal = target_instance
                partial_exists = (staging_path / "single.db.partial").exists()
                single_exists = (staging_path / "single.db").exists()
                billing_exists = (staging_path / "billing.db").exists()
                if "merge" in phases and "inspect_source" in phases:
                    assert not partial_exists and not single_exists and not billing_exists
                elif role_name == "single.db" or role_name == "billing.db":
                    assert single_exists and not partial_exists
                    assert billing_exists is (role_name == "billing.db")
                else:
                    assert partial_exists and not single_exists and not billing_exists
            failed_connections[0].conn.inner.close()
            if owner.root_lock is not None:
                from opencrab.locking import release_file_lock
                release_file_lock(owner.root_lock)
            for stream in owner.streams.values():
                if stream is not None:
                    stream.close()
            for fd in owner.fd_slots.values():
                if fd is not None:
                    os.close(fd)
            del merge_mod._publication_owners[str(root)]
            return
        if failure.startswith("connect-after-key:"):
            import gc

            gc.collect()
            assert target_instance in connection_instances
            assert len(owner.connections) == 1
            assert owner.connections[0].failed and owner.connections[0].conn is None
            assert owner.fd_slots.get("source-lock") is not None
            assert not root.exists()
            staging_path = root.with_name(root.name + ".staging")
            if owner.root_lock is None:
                assert not root.with_name(root.name + ".intent.json").exists()
                assert not staging_path.exists()
            else:
                assert root.with_name(root.name + ".intent.json").exists()
                assert staging_path.is_dir()
                phases, role_name, _ordinal = target_instance
                partial_exists = (staging_path / "single.db.partial").exists()
                single_exists = (staging_path / "single.db").exists()
                billing_exists = (staging_path / "billing.db").exists()
                if "merge" in phases and "inspect_source" in phases:
                    assert not partial_exists and not single_exists and not billing_exists
                elif role_name == "single.db" or role_name == "billing.db":
                    assert single_exists and not partial_exists
                    assert billing_exists is (role_name == "billing.db")
                else:
                    assert partial_exists and not single_exists and not billing_exists
            before_retry = list(connection_instances)
            assert merge_mod.main(args) != 0
            assert connection_instances == before_retry
            command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
            lock_paths = [backups / (backup_root.name + ".source.lifetime.lock")]
            if owner.root_lock is not None:
                lock_paths.append(tmp_path / (root.name + ".lifetime.lock"))
            for path in lock_paths:
                result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
                assert result.returncode != 0 and "BlockingIOError" in result.stderr
            if owner.root_lock is not None:
                from opencrab.locking import release_file_lock
                release_file_lock(owner.root_lock)
            for stream in owner.streams.values():
                if stream is not None:
                    stream.close()
            for fd in owner.fd_slots.values():
                if fd is not None:
                    os.close(fd)
            del merge_mod._publication_owners[str(root)]
            return
        assert (tmp_path / "published-generated.intent.json").exists()
        staging_dir = tmp_path / "published-generated.staging"
        assert staging_dir.is_dir() is (role != "intent")
        assert not root.exists()
        if role in {"staging-parent", "merge-partial"}:
            assert (staging_dir / "single.db.partial").exists() is (failure in {"create-after:merge-partial", "create-observe:merge-partial"})
        if role in {"staging-dir", "checkpoint-fsync"} and failure != "open:checkpoint-fsync#2":
            assert not (staging_dir / "billing.db").exists()
        if role == "billing-write":
            assert (staging_dir / "billing.db").exists() is (failure in {"create-after:billing-write", "create-observe:billing-write"})
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in (backups / (backup_root.name + ".source.lifetime.lock"),
                     tmp_path / (root.name + ".lifetime.lock")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr
        from opencrab.locking import release_file_lock
        for conn in owner.connections:
            if isinstance(conn.conn, InjectedConnection):
                conn.conn.inner.close()
        release_file_lock(owner.root_lock)
        for stream in owner.streams.values():
            if stream is not None:
                stream.close()
        for fd in owner.fd_slots.values():
            if fd is not None:
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure in {"registry-add-before", "registry-add-after"}:
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        owner = merge_mod._publication_owners[str(root)]
        assert owner.uncertain and owner.root_lock is not None
        assert owner.root_registry_state == "uncertain" and owner.fd_slots["source-lock"] is not None
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in (backups / (backup_root.name + ".source.lifetime.lock"),
                     tmp_path / (root.name + ".lifetime.lock")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr
        assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
        owner.root_lock.close()
        for stream in owner.streams.values():
            if stream is not None:
                stream.close()
        for fd in owner.fd_slots.values():
            if fd is not None:
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure in {"root-fdopen-before", "root-fdopen-after", "intent-fdopen-before", "intent-fdopen-after"}:
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        owner = merge_mod._publication_owners[str(root)]
        role = "root-lock" if failure.startswith("root") else "intent"
        assert owner.uncertain and owner.fd_slots[role] is not None
        assert owner.fd_slots["source-lock"] is not None
        assert (owner.root_lock is not None) is (role == "intent")
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        result = subprocess.run([*command, str(backups / (backup_root.name + ".source.lifetime.lock"))],
                                capture_output=True, text=True, check=False)
        assert result.returncode != 0 and "BlockingIOError" in result.stderr
        assert not root.exists() and not root.with_name(root.name + ".staging").exists()
        for stream in orphan_streams:
            stream.close()
        if owner.root_lock is not None:
            from opencrab.locking import release_file_lock
            release_file_lock(owner.root_lock)
        for stream in owner.streams.values():
            if stream is not None:
                stream.close()
        for key, fd in owner.fd_slots.items():
            if fd is not None and not (key == role and failure.endswith("after")):
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure is not None and failure.startswith("release:"):
        import subprocess
        import sys

        assert merge_mod.main(args) == 2
        _assert_ready_cleanup_failed(capsys.readouterr().err)
        _assert_ready_published(root)
        owner = merge_mod._publication_owners[str(root)]
        assert owner.uncertain and not owner.connections
        assert owner.fd_slots["source-lock"] is not None
        source_path = backups / (backup_root.name + ".source.lifetime.lock")
        root_path = tmp_path / (root.name + ".lifetime.lock")
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path, locked in ((source_path, failure.startswith("release:root-") or failure.startswith("release:registry") or failure == "release:source-unlock-before"),
                             (root_path, failure == "release:root-unlock-before")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert (result.returncode != 0) is locked
        before_retry = list(release_events)
        assert merge_mod.main(args) != 0
        assert release_events == before_retry
        source_fd = owner.fd_slots["source-lock"]
        if failure.startswith(("release:root-", "release:registry")):
            assert ("unlock", source_fd) not in release_events
            assert ("source-close", source_fd) not in release_events
        if failure.startswith("release:source-unlock"):
            assert ("source-close", source_fd) not in release_events
        if failure.startswith("release:source-close"):
            assert release_events.count(("source-close", source_fd)) == 1
        if failure.startswith("release:root-unlock"):
            assert owner.root_lock is not None and not owner.root_lock.closed
            assert owner.root_registry_state == "registered"
            assert owner.root_lock_state == "uncertain"
            assert ("registry-remove", None) not in release_events
        if failure.startswith("release:root-close"):
            assert release_events.count(("root-close", None)) == 1
            assert ("registry-remove", None) not in release_events
        if failure.startswith("release:registry"):
            assert release_events.count(("registry-remove", None)) == 1
        if hasattr(owner, "test_original_root"):
            handle = owner.test_original_root
            if not handle.closed:
                handle.close()
            from opencrab import locking
            locking._lifetime_handles.pop(handle, None)
        elif owner.root_lock is not None and not owner.root_lock.closed:
            from opencrab.locking import release_file_lock
            monkeypatch.setattr(merge_mod.fcntl, "flock", original_flock)
            release_file_lock(owner.root_lock)
        for stream in owner.streams.values():
            if stream is not None:
                stream.close()
        for key, fd in owner.fd_slots.items():
            if fd is not None and not (key == "source-lock" and failure == "release:source-close-after"):
                original_close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure is not None and failure.startswith("vec:"):
        assert merge_mod.main(args) != 0
        assert "injected vec attachment failure" in capsys.readouterr().err
        owner = owners[-1]
        assert not owner.connections and not owner.uncertain and owner.root_lock is None
        assert all(fd is None for fd in owner.fd_slots.values())
        assert root.with_name(root.name + ".intent.json").exists()
        assert (root.with_name(root.name + ".staging") / "single.db.partial").exists()
        assert not root.exists()
        return
    if failure is not None and failure.startswith("flock-after:"):
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        owner = merge_mod._publication_owners[str(root)]
        assert owner.uncertain and owner.fd_slots["source-lock"] is not None
        assert (owner.root_lock is not None) is (failure == "flock-after:root")
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        paths = [backups / (backup_root.name + ".source.lifetime.lock")]
        if owner.root_lock is not None:
            paths.append(tmp_path / (root.name + ".lifetime.lock"))
        for path in paths:
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode != 0 and "BlockingIOError" in result.stderr
        assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
        if owner.root_lock is not None:
            owner.root_lock.close()
        for stream in owner.streams.values():
            if stream is not None:
                stream.close()
        for fd in owner.fd_slots.values():
            if fd is not None:
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure is not None and failure.startswith("flock-before:"):
        with pytest.raises(merge_mod._PublicationFlockBeforeTest):
            merge_mod.main(args)
        owner = owners[-1]
        assert not owner.uncertain and not owner.connections and owner.root_lock is None
        assert all(fd is None for fd in owner.fd_slots.values())
        assert str(root) not in merge_mod._publication_owners
        assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
        assert not root.with_name(root.name + ".staging").exists()
        return
    if failure is not None and failure.startswith(("input-connect:", "input-attach:", "input-detach:")):
        assert merge_mod.main(args) != 0
        assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
        assert not root.with_name(root.name + ".staging").exists()
        if failure.startswith("input-connect:"):
            owner = merge_mod._publication_owners[str(root)]
            assert owner.uncertain and owner.root_lock is None
            assert any(conn.failed and conn.conn is None for conn in owner.connections)
            for fd in owner.fd_slots.values():
                if fd is not None:
                    os.close(fd)
            del merge_mod._publication_owners[str(root)]
        else:
            assert str(root) not in merge_mod._publication_owners
        return
    if failure is not None and failure.startswith("input-open:"):
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        owner = merge_mod._publication_owners[str(root)]
        role = failure.removeprefix("input-open:")
        assert owner.uncertain and owner.fd_slots[owner.fd_roles[role][-1]] is None
        assert owner.root_lock is None and owner.fd_slots["source-lock"] is not None
        assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
        assert not root.with_name(root.name + ".staging").exists()
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        result = subprocess.run([*command, str(backups / (backup_root.name + ".source.lifetime.lock"))],
                                capture_output=True, text=True, check=False)
        assert result.returncode != 0 and "BlockingIOError" in result.stderr
        for fd in owner.fd_slots.values():
            if fd is not None:
                os.close(fd)
        del merge_mod._publication_owners[str(root)]
        return
    if failure in {"intent-write", "intent-flush", "intent-write-after", "intent-flush-after"}:
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        assert failure.replace("-", " ") in capsys.readouterr().err
        assert root.with_name(root.name + ".intent.json").exists()
        assert not root.with_name(root.name + ".staging").exists()
        assert not root.exists()
        owner = owners[-1]
        assert not owner.connections and not owner.uncertain and owner.root_lock is None
        assert all(fd is None for fd in owner.fd_slots.values())
        assert all(stream is None for stream in owner.streams.values())
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in (backups / (backup_root.name + ".source.lifetime.lock"),
                     tmp_path / (root.name + ".lifetime.lock")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode == 0, result.stderr
        return
    if failure is not None and failure.startswith(("fsync:", "fsync-after:")):
        import subprocess
        import sys

        assert merge_mod.main(args) != 0
        assert not root.exists()
        assert (tmp_path / "published-generated.intent.json").exists()
        assert "injected fsync uncertain" in capsys.readouterr().err
        owner = owners[-1]
        assert not owner.connections and not owner.uncertain and owner.root_lock is None
        assert all(fd is None for fd in owner.fd_slots.values())
        assert all(stream is None for stream in owner.streams.values())
        staging_dir = tmp_path / "published-generated.staging"
        if failure.endswith("intent-stream"):
            assert not staging_dir.exists()
        if failure.endswith("checkpoint-fsync#2"):
            assert (staging_dir / "single.db").exists()
            assert not (staging_dir / "single.db.partial").exists()
            assert not (staging_dir / "billing.db").exists()
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in (backups / (backup_root.name + ".source.lifetime.lock"),
                     tmp_path / (root.name + ".lifetime.lock")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode == 0, result.stderr
        return
    if failure is not None and failure.startswith("observe:"):
        assert merge_mod.main(args) != 0
        owner = owners[-1]
        assert not owner.connections and not owner.uncertain and owner.root_lock is None
        assert all(fd is None for fd in owner.fd_slots.values())
        assert "injected observation failure" in capsys.readouterr().err
        monkeypatch.setattr(os, "lstat", original_lstat)
        assert not root.exists()
        assert not root.with_name(root.name + ".intent.json").exists()
        assert not root.with_name(root.name + ".staging").exists()
        return
    if failure is not None and failure.startswith("observe-after:"):
        assert merge_mod.main(args) != 0
        owner = owners[-1]
        assert owner.effect_observations[-1][str(fail_observation[0])] is None
        assert not owner.connections and not owner.uncertain and owner.root_lock is None
        assert all(fd is None for fd in owner.fd_slots.values())
        monkeypatch.setattr(os, "lstat", original_lstat)
        assert not root.exists()
        assert root.with_name(root.name + ".intent.json").exists()
        assert root.with_name(root.name + ".staging").exists()
        assert not (root.with_name(root.name + ".staging") / "billing.db").exists()
        return
    if failure is not None and failure.startswith(("mkdir:", "link:", "unlink:")):
        assert merge_mod.main(args) != 0
        staging_dir = tmp_path / "published-generated.staging"
        assert not root.exists()
        assert (tmp_path / "published-generated.intent.json").exists()
        assert staging_dir.exists() is (failure != "mkdir:before")
        if failure.startswith("link:") or failure.startswith("unlink:"):
            assert (staging_dir / "single.db.partial").exists() is (failure != "unlink:after")
            assert (staging_dir / "single.db").exists() is (failure != "link:before")
        assert not (staging_dir / "billing.db").exists()
        return
    if attach_target is not None:
        assert merge_mod.main(args) != 0
        assert attach_target[:3] in attach_events
        owner = owners[-1]
        if len(attach_target) == 5:
            import gc
            import subprocess
            import sys

            gc.collect()
            assert owner.uncertain and len(owner.connections) == 1
            assert owner.connections[0].failed
            assert owner.connections[0].conn.inner is close_events[0]
            assert len(close_events) == 1
            assert owner.fd_slots["source-lock"] is not None
            assert merge_mod.main(args) != 0
            assert len(close_events) == 1
            command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
            lock_paths = [backups / (backup_root.name + ".source.lifetime.lock")]
            if owner.root_lock is not None:
                lock_paths.append(tmp_path / (root.name + ".lifetime.lock"))
            for path in lock_paths:
                result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
                assert result.returncode != 0 and "BlockingIOError" in result.stderr
            close_events[0].close()
            if owner.root_lock is not None:
                from opencrab.locking import release_file_lock
                release_file_lock(owner.root_lock)
            for stream in owner.streams.values():
                if stream is not None:
                    stream.close()
            for fd in owner.fd_slots.values():
                if fd is not None:
                    os.close(fd)
            del merge_mod._publication_owners[str(root)]
        else:
            assert not owner.connections and not owner.uncertain and owner.root_lock is None
            assert all(fd is None for fd in owner.fd_slots.values())
            assert str(root) not in merge_mod._publication_owners
        assert not root.exists()
        intent = root.with_name(root.name + ".intent.json")
        staging = root.with_name(root.name + ".staging")
        assert intent.exists() is (attach_target[0] != "inputs")
        assert staging.exists() is (attach_target[0] != "inputs")
        return
    if failure is None:
        assert merge_mod.main(args) == 0
    else:
        assert merge_mod.main(args) != 0
    stderr = capsys.readouterr().err
    if failure is None:
        import subprocess
        import sys

        attach_inventory = os.environ.get("PUBLICATION_ATTACH_INVENTORY")
        if attach_inventory:
            Path(attach_inventory).write_text(json.dumps(attach_events), encoding="utf-8")
        inventory_path = os.environ.get("PUBLICATION_TEST_INVENTORY")
        if inventory_path:
            Path(inventory_path).write_text(json.dumps(connection_instances), encoding="utf-8")
        assert {"merge-partial", "checkpoint-fsync", "staging-parent", "staging-dir",
                "billing-write", "billing-read"} <= set(fd_roles)
        assert fd_roles.count("checkpoint-fsync") == 2
        assert ":memory:" in actual_connections
        assert any("/proc/self/fd/" in value for value in actual_connections)
        assert any("single.db.partial" in value for value in actual_connections)
        assert any("single.db?" in value for value in actual_connections)
        assert any("billing.db?" in value for value in actual_connections)
        assert len(set(connection_instances)) == len(connection_instances)
        command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
        for path in (backups / (backup_root.name + ".source.lifetime.lock"),
                     tmp_path / (root.name + ".lifetime.lock")):
            result = subprocess.run([*command, str(path)], capture_output=True, text=True, check=False)
            assert result.returncode == 0, result.stderr
    expected_errors = {"wal": "WAL mode differs", "sync": "synchronous mode differs",
                       "busy": "checkpoint is incomplete", "frames": "checkpoint is incomplete",
                       "close": "injected close failure", "sidecar": "sidecar remains",
                       "busy_close": "checkpoint is incomplete; publication close failed"}
    if failure in expected_errors:
        assert expected_errors[failure] in stderr
    if failure in {"attach:merge", "detach:merge"}:
        assert "injected" in stderr
        assert str(root) not in merge_mod._publication_owners
    staging = tmp_path / "published-generated.staging"
    assert json.loads((tmp_path / "published-generated.intent.json").read_text())["state"] == (
        "ready" if failure is None else "preparing")
    if failure in {"attach:merge", "detach:merge"}:
        assert not calls
    else:
        assert calls and set(calls[0]) == {*merge_mod.FILES, "billing.db"}
    if failure:
        database = staging / "single.db.partial"
        assert not (staging / "billing.db").exists()
    else:
        database = root / "single.db"
        assert not staging.exists()
        _assert_ready_published(root)
        assert (root / "billing.db").read_bytes() == (backup_root / "billing.db").read_bytes()
    assert database.is_file()
    assert failure is None or not root.exists()
    if failure in {"attach:merge", "detach:merge"}:
        return
    with (_conn(database) if failure else _immutable_conn(database)) as conn:
        assert conn.execute("SELECT count(*) FROM vectors_kure").fetchone()[0] == 40
        assert conn.execute("SELECT count(*) FROM graph_nodes").fetchone()[0] == 30
        if not failure:
            ledger = json.loads((tmp_path / "published-generated.intent.json").read_text())
            row = conn.execute("SELECT root, database_path, db_uuid, generation FROM single_file_metadata").fetchone()
            assert row == (str(root), str(root / "single.db"), ledger["db_uuid"], ledger["generation"])
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_publication_binding_fds_remain_owned_during_main(source_dir, tmp_path, monkeypatch):
    from opencrab.stores import _single_file

    seen = []
    original = _single_file.read_binding_fd

    def read_spy(directory_fd, name, fd):
        owner = merge_mod._publication_current.owner
        role = "manifest-file" if name == "MANIFEST.json" else "evidence-file"
        assert fd in [owner.fd_slots[key] for key in owner.fd_roles[role]]
        seen.append(role)
        return original(directory_fd, name, fd)

    monkeypatch.setattr(_single_file, "read_binding_fd", read_spy)
    (tmp_path / "src.source.lifetime.lock").touch()
    (source_dir / "MANIFEST.json").write_bytes(b"{}")
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b"{}")
    root = tmp_path / "publication-binding"
    assert merge_mod.main(["--source-dir", str(source_dir), "--publication-root", str(root),
                           "--evidence-file", str(evidence)]) != 0
    assert seen == ["manifest-file", "evidence-file"]
    assert str(root) not in merge_mod._publication_owners


@pytest.mark.parametrize("name", ["MANIFEST.json", "evidence.json"])
def test_publication_binding_open_failure_retains_owner(source_dir, tmp_path, monkeypatch, name):
    (tmp_path / "src.source.lifetime.lock").touch()
    (source_dir / "MANIFEST.json").write_bytes(b"{}")
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b"{}")
    root = tmp_path / "publication-binding-open"
    original_open = merge_mod.os.open

    def fail_open(path, *args, **kwargs):
        if path == name:
            raise OSError("binding open uncertain")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(merge_mod.os, "open", fail_open)
    assert merge_mod.main(["--source-dir", str(source_dir), "--publication-root", str(root),
                           "--evidence-file", str(evidence)]) != 0
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain
    role = "manifest-file" if name == "MANIFEST.json" else "evidence-file"
    assert owner.fd_slots[owner.fd_roles[role][0]] is None
    for fd in owner.fd_slots.values():
        if fd is not None:
            os.close(fd)
    del merge_mod._publication_owners[str(root)]


@pytest.mark.parametrize("role", ["manifest-file", "evidence-file"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_publication_binding_close_failure_retains_owner(source_dir, tmp_path, monkeypatch, role, timing):
    import subprocess
    import sys

    (tmp_path / "src.source.lifetime.lock").touch()
    (source_dir / "MANIFEST.json").write_bytes(b"{}")
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b"{}")
    root = tmp_path / "publication-binding-close"
    original_close = merge_mod.os.close
    failed = []

    def fail_close(fd):
        owner = getattr(merge_mod._publication_current, "owner", None)
        if owner and owner.fd_slots.get(role) == fd:
            failed.append(fd)
            if timing == "after":
                original_close(fd)
            raise OSError("binding close uncertain")
        return original_close(fd)

    monkeypatch.setattr(merge_mod.os, "close", fail_close)
    assert merge_mod.main(["--source-dir", str(source_dir), "--publication-root", str(root),
                           "--evidence-file", str(evidence)]) != 0
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain and failed == [owner.fd_slots[role]]
    assert owner.root_lock is None and owner.fd_slots["source-lock"] is not None
    assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
    assert not root.with_name(root.name + ".staging").exists()
    command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
    result = subprocess.run([*command, str(tmp_path / "src.source.lifetime.lock")], capture_output=True, text=True)
    assert result.returncode != 0 and "BlockingIOError" in result.stderr
    for key, fd in owner.fd_slots.items():
        if fd is not None and not (key == role and timing == "after"):
            original_close(fd)
    del merge_mod._publication_owners[str(root)]


def test_publication_fs_type_uses_owned_stream(tmp_path, monkeypatch):
    root = tmp_path / "publication-mounts"
    created = []
    real_fdopen = os.fdopen

    def capture(fd, mode):
        stream = real_fdopen(fd, mode)
        created.append(stream)
        return stream

    monkeypatch.setattr(merge_mod.os, "fdopen", capture)
    with merge_mod._publication_owner(root) as (owner, work):
        assert merge_mod.fs_type(tmp_path, work=work) != "unknown"
        # Q1은 D0에서 자기 stream을 닫는다. 정리 단계까지 열어 두지 않는다.
        assert owner.streams["mounts"] is None and owner.fd_slots["mounts"] is None
        assert len(created) == 1 and created[0].closed
        assert not owner.tokens
    assert str(root) not in merge_mod._publication_owners


def test_publication_fs_type_preserves_each_stream(tmp_path, monkeypatch):
    root = tmp_path / "publication-mounts-repeat"
    created = []
    real_fdopen = os.fdopen

    def capture(fd, mode):
        stream = real_fdopen(fd, mode)
        created.append(stream)
        return stream

    monkeypatch.setattr(merge_mod.os, "fdopen", capture)
    with merge_mod._publication_owner(root) as (owner, work):
        merge_mod.fs_type(tmp_path, work=work)
        merge_mod.fs_type(tmp_path, work=work)
        keys = owner.stream_roles["mounts"]
        assert len(keys) == 2 and len(set(keys)) == 2
        assert all(owner.streams[key] is None for key in keys)
        assert len(created) == 2 and all(stream.closed for stream in created)
    assert str(root) not in merge_mod._publication_owners


def test_publication_refuses_unknown_fs_without_changing_out(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_mod, "fs_type", lambda path, **kwargs: "unknown")
    merge_mod.refuse_ram_fs("out", tmp_path)
    with merge_mod._publication_owner(tmp_path / "publication-unknown") as (_owner, work):
        with pytest.raises(merge_mod.MergeError, match="filesystem is unknown"):
            merge_mod.refuse_ram_fs("publication", tmp_path, work=work)


def test_publication_cleanup_retries_only_close_before_test(tmp_path):
    import threading

    root = tmp_path / "publication-test-retry"
    thread = threading.get_ident()
    events = []

    class CloseBefore:
        ready = False

        def unblock(self):
            events.append(("unblock", threading.get_ident()))
            self.ready = True

        def close(self):
            events.append(("close", threading.get_ident()))
            if not self.ready:
                raise merge_mod._PublicationCloseBeforeTest(self.unblock)

    with merge_mod._publication_owner(root) as (owner, work):
        owner.register(CloseBefore(), work=work)
    assert events == [("close", thread), ("unblock", thread), ("close", thread)]
    assert str(root) not in merge_mod._publication_owners


def test_publication_close_retry_refuses_concurrent_cleanup(tmp_path):
    import threading

    owner = merge_mod._PublicationOwner(tmp_path / "publication-concurrent")
    entered = threading.Event()
    release = threading.Event()

    class CloseBefore:
        ready = False

        def unblock(self):
            entered.set()
            assert release.wait(5)
            self.ready = True

        def close(self):
            if not self.ready:
                raise merge_mod._PublicationCloseBeforeTest(self.unblock)

    owner.register(CloseBefore())
    worker = threading.Thread(target=owner.close_remaining)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(merge_mod.MergeError, match="already started"):
            owner.close_remaining()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not owner.connections


def test_publication_close_retry_failure_refuses_later_cleanup(tmp_path):
    root = tmp_path / "publication-retry-fail"
    calls = []

    class CloseBefore:
        def close(self):
            calls.append("close")
            raise merge_mod._PublicationCloseBeforeTest(lambda: None)

    with pytest.raises(merge_mod._PublicationCloseBeforeTest):
        with merge_mod._publication_owner(root) as (owner, work):
            owner.register(CloseBefore(), work=work)
    assert calls == ["close", "close"]
    assert owner.uncertain and owner.connections
    with pytest.raises(merge_mod.MergeError, match="already started"):
        owner.close_remaining()
    assert calls == ["close", "close"]
    del merge_mod._publication_owners[str(root)]


def test_publication_fs_type_open_failure_retains_owner(tmp_path, monkeypatch):
    root = tmp_path / "publication-mounts-open"
    original = os.open

    def fail_open(path, *args, **kwargs):
        if path == "/proc/mounts":
            raise OSError("mounts open uncertain")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(merge_mod.os, "open", fail_open)
    with pytest.raises(OSError, match="mounts open uncertain"):
        with merge_mod._publication_owner(root) as (_owner, work):
            merge_mod.fs_type(tmp_path, work=work)
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain and owner.fd_slots["mounts"] is None
    del merge_mod._publication_owners[str(root)]


@pytest.mark.parametrize("stage", ["fdopen", "read", "close"])
def test_publication_fs_type_stream_failures(tmp_path, monkeypatch, stage):
    root = tmp_path / ("publication-mounts-" + stage)
    original = os.fdopen
    captured = []

    class StreamProxy:
        def __init__(self, inner):
            self.inner = inner

        def read(self):
            if stage == "read":
                raise OSError("mounts read failure")
            return self.inner.read()

        def close(self):
            if stage == "close":
                raise OSError("mounts close uncertain")
            self.inner.close()

    def fdopen(fd, mode):
        if mode == "r":
            if stage == "fdopen":
                raise OSError("mounts fdopen uncertain")
            proxy = StreamProxy(original(fd, mode))
            captured.append(proxy)
            return proxy
        return original(fd, mode)

    monkeypatch.setattr(merge_mod.os, "fdopen", fdopen)
    with pytest.raises(OSError, match="mounts"):
        with merge_mod._publication_owner(root) as (_owner, work):
            merge_mod.fs_type(tmp_path, work=work)
    if stage == "read":
        assert captured[0].inner.closed
        assert str(root) not in merge_mod._publication_owners
    else:
        owner = merge_mod._publication_owners[str(root)]
        assert owner.uncertain
        if stage == "close":
            captured[0].inner.close()
        else:
            os.close(owner.fd_slots["mounts"])
        del merge_mod._publication_owners[str(root)]


@pytest.mark.parametrize("role", ["manifest-parent", "evidence-parent"])
def test_publication_binding_parent_open_failure(source_dir, tmp_path, monkeypatch, role):
    (tmp_path / "src.source.lifetime.lock").touch()
    root = tmp_path / "publication-parent-failure"
    original = merge_mod._PublicationOwner.open_fd

    def inject(owner, current, path, flags, mode=0o777, **kwargs):
        if current != role:
            return original(owner, current, path, flags, mode, **kwargs)
        real_open = merge_mod.os.open

        def fail(*args, **kwargs):
            raise OSError("injected parent open uncertain")

        monkeypatch.setattr(merge_mod.os, "open", fail)
        try:
            return original(owner, current, path, flags, mode, **kwargs)
        finally:
            monkeypatch.setattr(merge_mod.os, "open", real_open)

    monkeypatch.setattr(merge_mod._PublicationOwner, "open_fd", inject)
    assert merge_mod.main(["--source-dir", str(source_dir), "--publication-root", str(root),
                           "--evidence-file", str(tmp_path / "evidence.json")]) != 0
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain and owner.fd_slots[owner.fd_roles[role][-1]] is None
    assert owner.fd_slots["source-lock"] is not None and owner.root_lock is None
    assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
    for fd in owner.fd_slots.values():
        if fd is not None:
            os.close(fd)
    del merge_mod._publication_owners[str(root)]


@pytest.mark.parametrize("role", ["manifest-parent", "evidence-parent", "manifest-file", "evidence-file"])
def test_publication_binding_open_effect_after_failure(source_dir, tmp_path, monkeypatch, role):
    import subprocess
    import sys

    (tmp_path / "src.source.lifetime.lock").touch()
    (source_dir / "MANIFEST.json").write_bytes(b"{}")
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b"{}")
    root = tmp_path / "publication-binding-effect"
    original = merge_mod._PublicationOwner.open_fd

    def inject(owner, current, path, flags, mode=0o777, **kwargs):
        if current != role:
            return original(owner, current, path, flags, mode, **kwargs)
        real_open = merge_mod.os.open

        def fail(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            os.close(fd)
            raise OSError("injected binding open after effect")

        monkeypatch.setattr(merge_mod.os, "open", fail)
        try:
            return original(owner, current, path, flags, mode, **kwargs)
        finally:
            monkeypatch.setattr(merge_mod.os, "open", real_open)

    monkeypatch.setattr(merge_mod._PublicationOwner, "open_fd", inject)
    assert merge_mod.main(["--source-dir", str(source_dir), "--publication-root", str(root),
                           "--evidence-file", str(evidence)]) != 0
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain and owner.fd_slots[owner.fd_roles[role][-1]] is None
    assert owner.root_lock is None and owner.fd_slots["source-lock"] is not None
    command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
    result = subprocess.run([*command, str(tmp_path / "src.source.lifetime.lock")], capture_output=True, text=True)
    assert result.returncode != 0 and "BlockingIOError" in result.stderr
    assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
    for fd in owner.fd_slots.values():
        if fd is not None:
            os.close(fd)
    del merge_mod._publication_owners[str(root)]


@pytest.mark.parametrize("role", ["manifest-parent", "evidence-parent"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_publication_binding_parent_close_failure(source_dir, tmp_path, monkeypatch, role, timing):
    import subprocess
    import sys

    (tmp_path / "src.source.lifetime.lock").touch()
    (source_dir / "MANIFEST.json").write_bytes(b"{}")
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b"{}")
    root = tmp_path / "publication-parent-close"
    original = os.close
    attempts = []

    def fail(fd):
        owner = getattr(merge_mod._publication_current, "owner", None)
        if owner is not None and owner.fd_slots.get(role) == fd:
            attempts.append(fd)
            if timing == "after":
                original(fd)
            raise OSError("injected parent close uncertain")
        return original(fd)

    monkeypatch.setattr(merge_mod.os, "close", fail)
    assert merge_mod.main(["--source-dir", str(source_dir), "--publication-root", str(root),
                           "--evidence-file", str(evidence)]) != 0
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain and attempts == [owner.fd_slots[role]]
    assert owner.root_lock is None and owner.fd_slots["source-lock"] is not None
    command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"]
    result = subprocess.run([*command, str(tmp_path / "src.source.lifetime.lock")], capture_output=True, text=True)
    assert result.returncode != 0 and "BlockingIOError" in result.stderr
    assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
    for key, fd in owner.fd_slots.items():
        if fd is not None and not (key == role and timing == "after"):
            original(fd)
    del merge_mod._publication_owners[str(root)]


def test_publication_owner_closes_acquired_fd_without_explicit_defer(tmp_path):
    root = tmp_path / "publication-early-failure"
    with pytest.raises(ValueError, match="before next open"):
        with merge_mod._publication_owner(root) as (owner, work):
            fd = owner.open_fd("parent", tmp_path, os.O_RDONLY | os.O_DIRECTORY, work=work)
            raise ValueError("before next open")
    with pytest.raises(OSError):
        os.fstat(fd)
    assert str(root) not in merge_mod._publication_owners


def test_publication_intent_is_owned(tmp_path):
    root = tmp_path / "publication-intent-owner"
    with merge_mod._publication_owner(root) as (owner, work):
        handle = owner.acquire_root_lock(root, work=work)
        merge_mod._prepare_publication(root, root / "single.db", handle, work=work)
        assert owner.streams["intent"] is not None
        assert not owner.streams["intent"].closed
        assert owner.fd_slots["intent"] is None
        assert owner.fd_slots["intent-parent"] is not None
    assert str(root) not in merge_mod._publication_owners


def test_publication_intent_fdopen_failure_retains_owner(tmp_path, monkeypatch):
    root = tmp_path / "publication-intent-fdopen"
    original = os.fdopen
    with pytest.raises(OSError, match="intent fdopen uncertain"):
        with merge_mod._publication_owner(root) as (owner, work):
            handle = owner.acquire_root_lock(root, work=work)

            def fail_fdopen(fd, mode):
                if mode == "wb":
                    raise OSError("intent fdopen uncertain")
                return original(fd, mode)

            monkeypatch.setattr(merge_mod.os, "fdopen", fail_fdopen)
            merge_mod._prepare_publication(root, root / "single.db", handle, work=work)
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain and owner.streams["intent"] is None
    assert owner.fd_slots["intent"] is not None
    from opencrab.locking import release_file_lock
    release_file_lock(owner.root_lock)
    for fd in owner.fd_slots.values():
        if fd is not None:
            os.close(fd)
    del merge_mod._publication_owners[str(root)]


def test_binding_file_existing_reader_preserves_validation(tmp_path):
    from opencrab.stores._single_file import read_binding_file

    path = tmp_path / "binding.json"
    path.write_bytes(b"binding")
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert read_binding_file(parent, path.name) == b"binding"
        with pytest.raises(ValueError):
            read_binding_file(parent, "../binding.json")
    finally:
        os.close(parent)


def test_publication_intent_close_failure_retains_owner(tmp_path):
    root = tmp_path / "publication-intent-close"
    actual = None
    with pytest.raises(OSError, match="intent close uncertain"):
        with merge_mod._publication_owner(root) as (owner, work):
            handle = owner.acquire_root_lock(root, work=work)
            merge_mod._prepare_publication(root, root / "single.db", handle, work=work)
            actual = owner.streams["intent"]

            class CloseFailure:
                def close(self):
                    raise OSError("intent close uncertain")

            owner.streams["intent"] = CloseFailure()
    owner = merge_mod._publication_owners[str(root)]
    assert owner.uncertain and owner.streams["intent"] is not None
    assert owner.fd_slots["intent-parent"] is not None
    actual.close()
    for fd in owner.fd_slots.values():
        if fd is not None:
            os.close(fd)
    del merge_mod._publication_owners[str(root)]


@pytest.mark.parametrize("role", ["staging-parent", "staging-dir", "merge-partial", "checkpoint-fsync",
                                  "billing-write", "billing-read"])
def test_publication_fd_acquisition_failure_retains_prior_locks(tmp_path, role):
    root = tmp_path / ("publication-fd-" + role)
    source = tmp_path / ("source-" + role)
    source.write_bytes(b"source")
    owner = None
    with pytest.raises(OSError, match="injected open uncertain"):
        with merge_mod._publication_owner(root) as (owner, work):
            owner.acquire_root_lock(root, work=work)
            owner.open_fd("source", source, os.O_RDONLY, work=work)
            original = merge_mod.os.open

            def fail_open(path, *args, **kwargs):
                if path == "failure-target":
                    raise OSError("injected open uncertain")
                return original(path, *args, **kwargs)

            merge_mod.os.open = fail_open
            try:
                owner.open_fd(role, "failure-target", os.O_RDONLY, work=work)
            finally:
                merge_mod.os.open = original
    assert owner.uncertain and owner.root_lock is not None
    assert owner.fd_slots[role] is None and owner.fd_slots["source"] is not None
    from opencrab.locking import release_file_lock
    release_file_lock(owner.root_lock)
    os.close(owner.fd_slots["source"])
    del merge_mod._publication_owners[str(root)]


def test_publication_exists_distinguishes_absence_and_observation_error(tmp_path):
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "absent")
    assert merge_mod._publication_exists(dangling)
    assert not merge_mod._publication_exists(tmp_path / "absent")
    regular = tmp_path / "regular"
    regular.write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        merge_mod._publication_exists(regular / "child")


def test_publication_root_lock_contends_across_processes(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "publication-root"
    owner = merge_mod._PublicationOwner(root)
    owner.acquire_root_lock(root)
    lock_path = root.parent / (root.name + ".lifetime.lock")
    command = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)", str(lock_path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode != 0
        assert "BlockingIOError" in result.stderr
    finally:
        owner.release_root_lock()
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_publication_root_fdopen_failure_is_uncertain(tmp_path, monkeypatch):
    root = tmp_path / "publication-fdopen"
    owner = merge_mod._PublicationOwner(root)
    real_fdopen = os.fdopen

    def fail_fdopen(fd, mode):
        raise OSError("fdopen uncertain")

    monkeypatch.setattr(merge_mod.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen uncertain"):
        owner.acquire_root_lock(root)
    assert owner.uncertain
    assert owner.root_lock_state == "uncertain"
    fd = owner.fd_slots["root-lock"]
    assert fd is not None
    monkeypatch.setattr(merge_mod.os, "fdopen", real_fdopen)
    os.close(fd)


def test_publication_root_flock_failure_retains_handle(tmp_path, monkeypatch):
    import gc

    root = tmp_path / "publication-flock"
    real_flock = merge_mod.fcntl.flock

    def fail_after_flock(fd, operation):
        real_flock(fd, operation)
        raise OSError("flock uncertain")

    with pytest.raises(OSError, match="flock uncertain"):
        with merge_mod._publication_owner(root) as (owner, work):
            monkeypatch.setattr(merge_mod.fcntl, "flock", fail_after_flock)
            owner.acquire_root_lock(root, work=work)
    monkeypatch.setattr(merge_mod.fcntl, "flock", real_flock)
    del owner
    gc.collect()
    retained = merge_mod._publication_owners[str(root)]
    assert retained.uncertain and not retained.root_lock.closed
    contender = os.open(root.parent / (root.name + ".lifetime.lock"), os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            real_flock(contender, merge_mod.fcntl.LOCK_EX | merge_mod.fcntl.LOCK_NB)
    finally:
        os.close(contender)
    retained.root_lock.close()
    del merge_mod._publication_owners[str(root)]


def test_publication_root_close_failure_retains_registry(tmp_path, monkeypatch):
    from opencrab import locking

    root = tmp_path / "publication-close"
    owner = merge_mod._PublicationOwner(root)
    handle = owner.acquire_root_lock(root)

    class CloseFailure:
        def fileno(self):
            return handle.fileno()

        def close(self):
            raise OSError("close uncertain")

    owner.root_lock = CloseFailure()
    with pytest.raises(OSError, match="close uncertain"):
        owner.release_root_lock()
    assert owner.uncertain and owner.root_lock_state == "uncertain"
    assert handle in locking._lifetime_handles
    locking.release_file_lock(handle)


def test_publication_root_registry_failure_retains_source(tmp_path):
    from opencrab import locking

    root = tmp_path / "publication-registry"
    owner = merge_mod._PublicationOwner(root)
    handle = owner.acquire_root_lock(root)
    source = owner.open_fd("source", root.parent, os.O_RDONLY | os.O_DIRECTORY)
    with locking._lifetime_handles_lock:
        del locking._lifetime_handles[handle]
    with pytest.raises(KeyError):
        owner.release_root_lock()
    assert owner.uncertain and owner.root_registry_state == "uncertain"
    assert owner.fd_slots["source"] == source
    os.close(source)


def test_publication_root_unlock_failure_retains_handle(tmp_path, monkeypatch):
    root = tmp_path / "publication-unlock"
    owner = merge_mod._PublicationOwner(root)
    handle = owner.acquire_root_lock(root)
    real_flock = merge_mod.fcntl.flock

    def fail_unlock(fd, operation):
        if operation == merge_mod.fcntl.LOCK_UN:
            raise OSError("unlock uncertain")
        return real_flock(fd, operation)

    monkeypatch.setattr(merge_mod.fcntl, "flock", fail_unlock)
    with pytest.raises(OSError, match="unlock uncertain"):
        owner.release_root_lock()
    assert owner.uncertain and owner.root_lock is handle and not handle.closed
    monkeypatch.setattr(merge_mod.fcntl, "flock", real_flock)
    from opencrab.locking import release_file_lock
    release_file_lock(handle)


def test_publication_cleanup_collects_connections_before_root(tmp_path):
    events = []
    root = tmp_path / "publication-cleanup"
    with merge_mod._publication_owner(root) as (owner, work):
        handle = owner.acquire_root_lock(root, work=work)

        class Connection:
            def close(self):
                events.append("connection")

        owner.register(Connection(), work=work)
        owner.defer_root_release = True
        assert not handle.closed
    events.append("root-closed" if handle.closed else "root-open")
    assert events == ["connection", "root-closed"]


def test_publication_owner_tracks_repeated_fd_roles(tmp_path):
    owner = merge_mod._PublicationOwner(tmp_path / "publication-repeat")
    first = owner.open_fd("source", tmp_path, os.O_RDONLY | os.O_DIRECTORY, category="independent")
    second = owner.open_fd("source", tmp_path, os.O_RDONLY | os.O_DIRECTORY, category="independent")
    assert first != second
    assert [owner.fd_slots[key] for key in owner.fd_roles["source"]] == [first, second]
    owner.close_fd("source", first)
    owner.close_fd("source", second)
    owner.cleanup_started = True
    for key in owner.deferred_fds:
        owner.close_fd(key, owner.fd_slots[key])
    assert all(owner.fd_slots[key] is None for key in owner.fd_roles["source"])


def test_publication_owner_preserves_uncertain_acquisition(tmp_path, monkeypatch):
    root = tmp_path / "publication-acquisition"
    owner = merge_mod._PublicationOwner(root)

    def fail_open(*args, **kwargs):
        raise OSError("open result unknown")

    monkeypatch.setattr(merge_mod.os, "open", fail_open)
    with pytest.raises(OSError, match="open result unknown"):
        owner.open_fd("source", root, os.O_RDONLY)
    assert "source" in owner.fd_slots
    assert owner.uncertain


def test_publication_owner_preserves_uncertain_connect(tmp_path, monkeypatch):
    owner = merge_mod._PublicationOwner(tmp_path / "publication-connect")

    def fail_connect(*args, **kwargs):
        assert len(owner.connections) == 1
        assert owner.connections[0].conn is None
        raise sqlite3.OperationalError("connect result unknown")

    monkeypatch.setattr(merge_mod.sqlite3, "connect", fail_connect)
    with pytest.raises(sqlite3.OperationalError, match="connect result unknown"):
        owner.connect(":memory:")
    assert owner.uncertain
    assert owner.connections[0].failed
    assert owner.connections[0].conn is None


def test_publication_owner_retains_failed_connection_and_source_lock(source_dir, tmp_path):
    import gc

    lock_path = tmp_path / "src.source.lifetime.lock"
    lock_path.touch()
    target = tmp_path / "published-owner"
    connection = None
    with pytest.raises(RuntimeError, match="close before SQLite"):
        with (merge_mod._publication_owner(target) as (owner, work),
              merge_mod._publication_source_lock(source_dir, work=work)):
            actual = sqlite3.connect(source_dir / "graph.db")

            class FailingClose:
                def __init__(self, inner):
                    self.inner = inner

                def close(self):
                    raise RuntimeError("close before SQLite")

            connection = owner.register(FailingClose(actual), work=work)
            connection.close(work=work)
    del connection
    gc.collect()
    retained = merge_mod._publication_owners[str(target)]
    assert retained.uncertain and retained.connections
    assert retained.connections[0].conn.inner.execute("SELECT count(*) FROM graph_nodes").fetchone() == (30,)
    with pytest.raises(BlockingIOError):
        contender = os.open(lock_path, os.O_RDONLY)
        try:
            merge_mod.fcntl.flock(contender, merge_mod.fcntl.LOCK_EX | merge_mod.fcntl.LOCK_NB)
        finally:
            os.close(contender)
    retained.connections[0].conn.inner.close()
    os.close(retained.fd_slots["source-lock"])
    os.close(retained.fd_slots["source-parent"])
    del merge_mod._publication_owners[str(target)]


def test_publication_source_lock_requires_existing_regular_file(source_dir, tmp_path):
    lock = tmp_path / "src.source.lifetime.lock"
    with pytest.raises(FileNotFoundError):
        with merge_mod._publication_source_lock(source_dir):
            pass
    assert not lock.exists()
    lock.touch()
    with merge_mod._publication_source_lock(source_dir) as fd:
        assert os.fstat(fd).st_ino == lock.stat().st_ino


def test_publication_mode_refuses_before_creating_output(source_dir, tmp_path):
    root = tmp_path / "published"
    evidence = tmp_path / "evidence.json"
    assert merge_mod.main(["--source-dir", str(source_dir), "--publication-root", str(root),
                           "--evidence-file", str(evidence), "--apply", "--verify",
                           "--immutable-source"]) == 2
    assert not root.exists()
    assert not (tmp_path / "published.intent.json").exists()


def test_publication_intent_precedes_any_target_creation(tmp_path):
    root = tmp_path / "published"
    db = root / "single.db"
    from opencrab.locking import acquire_file_lock, release_file_lock

    shared_lock = acquire_file_lock("published.lifetime.lock", str(tmp_path), shared=True, timeout=0)
    try:
        with pytest.raises(merge_mod.MergeError, match="not exclusive"):
            merge_mod._prepare_publication(root, db, shared_lock)
        assert not (tmp_path / "published.intent.json").exists()
    finally:
        release_file_lock(shared_lock)

    lock = acquire_file_lock("published.lifetime.lock", str(tmp_path), timeout=0)
    try:
        intent = merge_mod._prepare_publication(root, db, lock)
        ledger = tmp_path / "published.intent.json"
        assert json.loads(ledger.read_text()) == intent
        assert intent["state"] == "preparing"
        assert not root.exists()
        assert not db.exists()
        with pytest.raises(FileExistsError):
            merge_mod._prepare_publication(root, db, lock)
        assert not root.exists()
    finally:
        release_file_lock(lock)


def test_plan_rejects_single_database_source(source_dir, tmp_path):
    path = source_dir / "graph.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--min-free-gib", "0"]) == 2
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_direct_merge_rechecks_single_database_source(source_dir, tmp_path):
    files = list(merge_mod.FILES)
    plan = merge_mod.plan(source_dir, files, immutable=False)
    with sqlite3.connect(source_dir / "graph.db") as conn:
        conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    out = tmp_path / "merged.db"
    with pytest.raises(merge_mod.MergeError, match="single SQLite source"):
        merge_mod.merge(source_dir, out, files, plan, immutable=False, cache_kib=4096, min_free_gib=0, log=lambda _: None)
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_direct_verify_rechecks_single_database_source(source_dir, tmp_path):
    files = list(merge_mod.FILES)
    plan = merge_mod.plan(source_dir, files, immutable=False)
    with sqlite3.connect(source_dir / "graph.db") as conn:
        conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT PRIMARY KEY)")
    with pytest.raises(merge_mod.MergeError, match="single SQLite source"):
        merge_mod.verify(source_dir, tmp_path / "absent.db", files, plan, {"tables": {}},
                         immutable=False, cache_kib=4096, log=lambda _: None)
    assert not (tmp_path / "absent.db").exists()


def test_merge_copies_every_table_and_queries_agree(source_dir, tmp_path):
    out = tmp_path / "merged.db"
    report_path = tmp_path / "report.json"
    rc = merge_mod.main([
        "--source-dir", str(source_dir), "--out", str(out), "--apply", "--verify", "--min-free-gib", "0",
        "--report", str(report_path), "--cache-kib", "4096",
    ])
    assert rc == 0
    report = json.loads(report_path.read_text())
    assert report["verify"]["ok"] is True
    assert report["finalize"]["journal_mode"] == "wal"
    assert not out.with_name("merged.db.partial").exists()

    merged = _conn(out)
    assert merged.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    token = merged.execute("SELECT incarnation FROM packs WHERE pack_id = 'p1'").fetchone()[0]
    assert len(token) == 32
    assert merged.execute("SELECT count(*) FROM pack_delete_operations").fetchone()[0] == 0
    assert merged.execute("SELECT count(*) FROM pack_delete_active").fetchone()[0] == 0
    assert merged.execute("SELECT count(*) FROM single_file_metadata").fetchone()[0] == 0
    merged.execute("INSERT INTO single_file_metadata(root, database_path, db_uuid, generation) VALUES (?, ?, ?, ?)", ('root', 'db', 'a' * 32, 'b' * 32))
    with pytest.raises(sqlite3.IntegrityError):
        merged.execute("INSERT INTO single_file_metadata(root, database_path, db_uuid, generation) VALUES (?, ?, ?, ?)", ('root', 'db', 'c' * 32, 'd' * 32))
    merged.execute(
        "INSERT INTO pack_delete_operations(operation_id, pack_id, incarnation, state) VALUES (?, 'p1', ?, 'pending')",
        ('a' * 32, token),
    )
    with pytest.raises(sqlite3.IntegrityError, match="active"):
        merged.execute(
            "INSERT INTO pack_delete_active(pack_id, incarnation, operation_id, state) VALUES ('p1', ?, ?, 'stable')",
            ('wrong', 'a' * 32),
        )
    merged.execute(
        "INSERT INTO pack_delete_active(pack_id, incarnation, operation_id, state) VALUES ('p1', ?, ?, 'stable')",
        (token, 'a' * 32),
    )
    with pytest.raises(sqlite3.IntegrityError):
        merged.execute(
            "INSERT INTO pack_delete_operations(operation_id, pack_id, incarnation, state) VALUES (?, 'p1', ?, 'pending')",
            ('b' * 32, token),
        )
    merged.execute("UPDATE pack_delete_operations SET state='committed', result_json='[]', evidence_json='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        merged.execute("UPDATE pack_delete_operations SET evidence_json='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        merged.execute("DELETE FROM pack_delete_operations")
    with pytest.raises(sqlite3.IntegrityError, match="predecessor"):
        merged.execute(
            "INSERT INTO pack_delete_operations(operation_id, pack_id, incarnation, predecessor, state) "
            "VALUES (?, 'p1', ?, ?, 'pending')", ('b' * 32, token, 'missing'),
        )
    merged.execute(
        "INSERT INTO pack_delete_operations(operation_id, pack_id, incarnation, predecessor, state) "
        "VALUES (?, 'p1', ?, ?, 'pending')", ('b' * 32, token, 'a' * 32),
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        merged.execute("UPDATE pack_delete_operations SET predecessor=NULL WHERE operation_id=?", ('b' * 32,))
    with pytest.raises(sqlite3.IntegrityError, match="active"):
        merged.execute("UPDATE pack_delete_active SET operation_id=?, state='handoff', expected_pointer=?", ('b' * 32, 'wrong'))
    merged.execute("UPDATE pack_delete_active SET operation_id=?, state='handoff', expected_pointer=?", ('b' * 32, 'a' * 32))
    merged.execute("UPDATE pack_delete_active SET state='stable', expected_pointer=NULL")
    with pytest.raises(sqlite3.IntegrityError, match="active"):
        merged.execute("UPDATE pack_delete_active SET state='clear_intent'")
    merged.execute("UPDATE pack_delete_operations SET state='committed', result_json='[]', evidence_json='{}' WHERE operation_id=?", ('b' * 32,))
    merged.execute("UPDATE pack_delete_active SET state='clear_intent'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        merged.execute("UPDATE packs SET incarnation = 'replacement' WHERE pack_id = 'p1'")
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        merged.execute("INSERT INTO packs(pack_id, incarnation) VALUES ('bad', '')")
    merged.execute("INSERT INTO packs(pack_id) VALUES ('p2')")
    next_token = merged.execute("SELECT incarnation FROM packs WHERE pack_id = 'p2'").fetchone()[0]
    assert len(next_token) == 32 and next_token != token
    merged.execute("DELETE FROM packs WHERE pack_id = 'p2'")
    merged.execute("INSERT INTO packs(pack_id) VALUES ('p2')")
    assert merged.execute("SELECT incarnation FROM packs WHERE pack_id = 'p2'").fetchone()[0] != next_token
    merged.rollback()
    for fname, tables in (("graph.db", ["graph_nodes"]),
                          ("doc_store.db", ["doc_sources", "seq_t", "doc_sources_fts"]),
                          ("vectors.db", ["vectors_kure"])):
        src = _conn(source_dir / fname)
        for t in tables:
            assert (merged.execute(f"SELECT count(*) FROM {t}").fetchone()
                    == src.execute(f"SELECT count(*) FROM {t}").fetchone()), t
        src.close()
    q = sqlite_vec.serialize_float32([3, 1, 0, 0.5])
    src_v = _conn(source_dir / "vectors.db")
    sql = "SELECT node_id FROM vectors_kure WHERE embedding MATCH ? AND k=5 AND pack_id='p1'"
    assert merged.execute(sql, (q,)).fetchall() == src_v.execute(sql, (q,)).fetchall()
    src_v.close()
    assert [r[0] for r in merged.execute(
        "SELECT source_id FROM doc_sources_fts WHERE doc_sources_fts MATCH 'alpha' ORDER BY rowid LIMIT 3")
    ] == ["s0", "s1", "s2"]
    # 트리거와 인덱스는 데이터 복사 뒤 만들어졌고 동작한다(복사 중 발화하지 않았다).
    assert merged.execute("SELECT count(*) FROM seq_t").fetchone()[0] == 35
    merged.execute("INSERT INTO doc_sources VALUES ('new', 'x')")
    assert merged.execute("SELECT count(*) FROM seq_t").fetchone()[0] == 36
    # 삭제된 행 때문에 시퀀스가 행 수보다 크다. 병합이 원본의 시퀀스를 유지해야 새 id가 겹치지 않는다.
    assert merged.execute("SELECT seq FROM sqlite_sequence WHERE name='seq_t'").fetchone()[0] == 41
    assert merged.execute("SELECT 1 FROM sqlite_master WHERE name='idx_nodes_props'").fetchone()
    src_g = _conn(source_dir / "graph.db")
    stat = "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx"
    assert merged.execute(stat).fetchall() == src_g.execute(stat).fetchall() != []
    src_g.close()
    merged.close()


def test_reserved_delete_schema_collision_is_refused(source_dir, tmp_path):
    with _conn(source_dir / "opencrab.db") as conn:
        conn.execute("CREATE TABLE pack_delete_operations (id TEXT)")
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out),
                           "--apply", "--min-free-gib", "0"]) == 2
    assert not out.exists()
    assert not out.with_name(out.name + ".partial").exists()


def test_name_collision_is_refused(source_dir, tmp_path):
    extra = _conn(source_dir / "opencrab.db")
    extra.execute("CREATE TABLE graph_nodes (x)")
    extra.commit()
    extra.close()
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply"]) == 2
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_existing_output_is_refused(source_dir, tmp_path):
    out = tmp_path / "merged.db"
    out.write_bytes(b"keep")
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--min-free-gib", "0"]) == 1
    assert out.read_bytes() == b"keep"


def test_free_space_floor_refuses_before_writing(source_dir, tmp_path):
    out = tmp_path / "merged.db"
    rc = merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                         "--min-free-gib", "1000000"])
    assert rc == 3
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_memory_backed_output_directory_is_refused(source_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(merge_mod, "fs_type", lambda _path: "tmpfs")
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply"]) == 2
    assert not out.with_name("merged.db.partial").exists()


def test_floor_reached_during_the_copy_preserves_the_partial_output(source_dir, tmp_path, monkeypatch):
    calls = {"n": 0}
    real = merge_mod.check_free

    def flaky(path, floor, need=0):
        calls["n"] += 1
        if calls["n"] > 1:  # 첫 호출은 시작 전 점검이다. 복사 중 점검에서 실패시킨다.
            raise merge_mod.DiskFloorError("simulated floor")
        return real(path, floor, need)

    monkeypatch.setattr(merge_mod, "check_free", flaky)
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--min-free-gib", "0"]) == 3
    assert not out.exists()
    assert out.with_name("merged.db.partial").exists()


def test_physical_rowids_survive_when_rows_and_chunks_were_deleted(source_dir, tmp_path):
    """열만 옮기면 물리 rowid가 1부터 다시 매겨진다. 실제 스냅숏에서 vec0 조회가 깨졌다."""
    v = _conn(source_dir / "vectors.db")
    for i in range(2000):  # 기본 청크 크기(1024)를 넘겨 청크를 둘 이상 만든다
        v.execute("INSERT INTO vectors_kure(node_id, pack_id, embedding, document) VALUES (?,?,?,?)",
                  (f"x{i}", "p1", sqlite_vec.serialize_float32([i, 1, 0, 0.5]), f"d{i}"))
    v.commit()
    # 첫 청크가 비도록 앞의 1024행(s0~s39, x0~x983)을 지운다.
    for node_id in [f"s{i}" for i in range(40)] + [f"x{i}" for i in range(984)]:
        v.execute("DELETE FROM vectors_kure WHERE node_id = ?", (node_id,))
    v.commit()
    first_chunk = v.execute("SELECT min(chunk_id) FROM vectors_kure_chunks").fetchone()[0]
    v.close()
    assert first_chunk > 1, "첫 청크가 비어 사라지지 않았다: 시험이 공허하다"
    s = _conn(source_dir / "doc_store.db")
    s.execute("DELETE FROM doc_sources WHERE source_id = 's3'")  # TEXT 기본키 표의 rowid에 틈을 만든다
    s.commit()
    s.close()

    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--verify",
                           "--min-free-gib", "0", "--cache-kib", "4096"]) == 0
    merged, src = _conn(out), _conn(source_dir / "doc_store.db")
    sql = "SELECT source_id, rowid FROM doc_sources ORDER BY source_id"
    assert merged.execute(sql).fetchall() == src.execute(sql).fetchall()
    got = merged.execute("SELECT length(embedding) FROM vectors_kure WHERE node_id = 'x1500'").fetchone()
    assert got == (16,)
    merged.close()
    src.close()


def test_unknown_shadow_suffix_is_refused_instead_of_copied_as_a_regular_table(source_dir, tmp_path):
    v = _conn(source_dir / "vectors.db")
    v.execute("CREATE TABLE vectors_kure_extra (x)")
    v.commit()
    v.close()
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply"]) == 2
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_publish_syncs_the_file_before_link_and_directory_after(source_dir, tmp_path, monkeypatch):
    events = []
    real_fsync, real_link = merge_mod.os.fsync, merge_mod.os.link

    def fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def link(src, dst, **kwargs):
        events.append("link")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(merge_mod.os, "fsync", fsync)
    monkeypatch.setattr(merge_mod.os, "link", link)
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                           "--min-free-gib", "0", "--cache-kib", "4096"]) == 0
    assert events[-3:] == ["fsync", "link", "fsync"]


@pytest.mark.parametrize("order", [("notes", "notes_tags"), ("notes_tags", "notes")])
def test_nested_virtual_table_names_are_accepted_in_either_creation_order(source_dir, tmp_path, order):
    s = _conn(source_dir / "doc_store.db")
    for name in order:
        s.execute(f"CREATE VIRTUAL TABLE {name} USING fts5(body)")
    s.commit()
    s.close()
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                           "--min-free-gib", "0", "--cache-kib", "4096"]) == 0
    merged = _conn(out)
    names = {r[0] for r in merged.execute("SELECT name FROM sqlite_master WHERE name LIKE 'notes%'")}
    assert {"notes", "notes_tags", "notes_data", "notes_tags_data"} <= names
    merged.close()


# ---------------------------------------------------------------------------
# Q1 11차 설계: D0에서 mounts stream을 닫고 owner 진입과 정리 권한을 mutex 아래에서 가른다.
# ---------------------------------------------------------------------------


def _flock_contended(path) -> bool:
    """별도 프로세스가 EX를 얻지 못하면 True. 실제 flock 경합으로 보유 여부를 관측한다."""
    import subprocess
    import sys

    command = [sys.executable, "-c",
               "import fcntl,sys; f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)",
               str(path)]
    recorder = _Syscalls.active
    if recorder is not None:
        recorder.paused += 1
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    finally:
        if recorder is not None:
            recorder.paused -= 1
    if result.returncode == 0:
        return False
    assert "BlockingIOError" in result.stderr
    return True


def _run_thread(function):
    """다른 스레드에서 호출 하나를 실행하고 값이나 예외를 돌려준다."""
    import threading

    box = {}

    def target():
        try:
            box["value"] = function()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    return box


class _Gate:
    """다른 스레드를 외부 호출 안에서 멈춰 세우는 장치."""

    def __init__(self):
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()

    def block(self):
        self.entered.set()
        assert self.release.wait(10)


class _Syscalls:
    """os.close, fcntl.flock과 os.open 호출을 순서대로 기록하고, 선택한 숫자에 오류를 주입한다."""

    active = None

    def __init__(self, monkeypatch):
        self.events = []
        self.paused = 0
        _Syscalls.active = self
        self.close_fault = {}
        self.unlock_fault = {}
        self.on_event = None
        self.real_close = os.close
        self.real_flock = merge_mod.fcntl.flock
        monkeypatch.setattr(os, "close", self._close)
        monkeypatch.setattr(merge_mod.fcntl, "flock", self._flock)

    def _close(self, fd):
        if self.paused:
            return self.real_close(fd)  # 별도 프로세스 경쟁 관측이 만드는 닫기는 기록하지 않는다
        self.events.append(("close", fd))
        if self.on_event:
            self.on_event("close", fd)
        fault = self.close_fault.get(fd)
        if fault == "before":
            raise OSError("injected close before effect")
        result = self.real_close(fd)
        if fault == "after":
            raise OSError("injected close after effect")
        return result

    def _flock(self, fd, operation):
        kind = "unlock" if operation == merge_mod.fcntl.LOCK_UN else "lock"
        self.events.append((kind, fd))
        if self.on_event:
            self.on_event(kind, fd)
        fault = self.unlock_fault.get(fd)
        if kind == "unlock" and fault == "before":
            raise OSError("injected unlock before effect")
        result = self.real_flock(fd, operation)
        if kind == "unlock" and fault == "after":
            raise OSError("injected unlock after effect")
        return result

    def count(self, kind, fd=None):
        return sum(1 for event in self.events if event[0] == kind and (fd is None or event[1] == fd))

    def mark(self):
        return len(self.events)

    def since(self, mark):
        return self.events[mark:]


@pytest.fixture
def syscalls(monkeypatch):
    return _Syscalls(monkeypatch)


def _hook_mounts_read(monkeypatch, callback, created=None):
    """mounts stream의 read 안에서 callback을 실행한다. 같은 스레드 callback 경계를 만든다."""
    real = os.fdopen
    created = created if created is not None else []

    class Proxy:
        def __init__(self, inner):
            self.inner = inner

        def read(self):
            callback()
            return self.inner.read()

        def close(self):
            self.inner.close()

        @property
        def closed(self):
            return self.inner.closed

    def fdopen(fd, mode):
        stream = real(fd, mode)
        if mode != "r":
            return stream
        proxy = Proxy(stream)
        created.append(proxy)
        return proxy

    monkeypatch.setattr(merge_mod.os, "fdopen", fdopen)
    return created


def _reset_syscalls():
    """준비 단계의 기록을 지운다. 이후 숫자별 횟수는 시험 본 단계만 센다."""
    if _Syscalls.active is not None:
        _Syscalls.active.events.clear()


def _discard_owner(owner):
    """시험 끝에 불명으로 남은 owner의 자원을 시험 쪽에서 회수한다. 제품 호출이 아니다."""
    from opencrab import locking

    for conn in list(owner.connections):
        inner = conn.conn
        for target in (getattr(inner, "inner", None), getattr(inner, "actual", None), inner):
            if hasattr(target, "execute") and hasattr(target, "close"):
                try:
                    target.close()
                except Exception:
                    pass
                break
    if owner.root_lock is not None:
        try:
            locking.release_file_lock(owner.root_lock)
        except Exception:
            pass
    for stream in owner.streams.values():
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    for fd in owner.fd_slots.values():
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    with merge_mod._publication_owners_lock:
        merge_mod._publication_owners.pop(owner.root, None)


class _ConnectionDouble:
    def __init__(self, label, events, error=None):
        self.label = label
        self.events = events
        self.error = error

    def close(self):
        self.events.append(self.label)
        if self.error is not None:
            raise self.error


def _start_blocked_q1(monkeypatch, owner, work):
    """다른 스레드에서 Q1을 시작하고 stream read 안에서 멈춰 세운다."""
    import threading

    gate = _Gate()
    _hook_mounts_read(monkeypatch, gate.block)
    box = {}

    def target():
        try:
            box["lines"] = owner.read_mounts(work)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    assert gate.entered.wait(10)
    return thread, gate, box


def _finish_blocked_q1(thread, gate, box):
    gate.release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert "error" not in box, box.get("error")


def _rejected(call):
    with pytest.raises(merge_mod.MergeError, match="rejects the call|already started"):
        call()


# ---- Q1 stream close ----


@pytest.mark.parametrize("scenario", ["own-key", "foreign-key-reject", "callback-reject"])
def test_q1_stream_close(tmp_path, monkeypatch, scenario):
    root = tmp_path / "q1-stream-close"
    records = []
    original = merge_mod._PublicationOwner.close_stream

    def spy(owner, role, *, token=None):
        records.append((role, token.kind if token else None, token.key if token else None))
        return original(owner, role, token=token)

    monkeypatch.setattr(merge_mod._PublicationOwner, "close_stream", spy)
    created = []
    with merge_mod._publication_owner(root) as (owner, work):
        foreign_fd = owner.open_fd("intent", tmp_path / "foreign", os.O_WRONLY | os.O_CREAT, 0o600, work=work)
        foreign_key, foreign = owner.fdopen_pair("intent", foreign_fd, "wb", work=work)

        def callback():
            token = owner.tokens["q1"]
            if scenario == "foreign-key-reject":
                with pytest.raises(merge_mod.MergeError, match="own stream key"):
                    owner.close_stream(foreign_key, token=token)
            if scenario == "callback-reject":
                with pytest.raises(merge_mod.MergeError, match="token is not valid"):
                    owner.close_stream(token.key)
                with pytest.raises(merge_mod.MergeError, match="token is not valid"):
                    owner.close_stream(foreign_key)
            assert not foreign.closed and owner.streams[foreign_key] is foreign

        _hook_mounts_read(monkeypatch, callback, created)
        assert merge_mod.fs_type(tmp_path, work=work) != "unknown"
        key = owner.stream_roles["mounts"][0]
        assert owner.streams[key] is None and owner.fd_slots[key] is None
        assert created[0].closed and not owner.uncertain and not owner.tokens
        assert not foreign.closed
        q1_closes = [record for record in records if record[0] == key]
        expected_q1 = [(key, None, None)] if scenario == "callback-reject" else []
        assert q1_closes == expected_q1 + [(key, "q1", key)]
    assert foreign.closed
    expected_foreign = {
        "own-key": [],
        "foreign-key-reject": [(foreign_key, "q1", key)],
        "callback-reject": [(foreign_key, None, None)],
    }[scenario] + [(foreign_key, "cleanup", None)]
    assert [record for record in records if record[0] == foreign_key] == expected_foreign
    assert str(root) not in merge_mod._publication_owners


@pytest.mark.parametrize("scenario", ["sequential", "reentrant"])
def test_q1_stream_identity(tmp_path, monkeypatch, scenario):
    root = tmp_path / "q1-stream-identity"
    created = []
    calls = {"count": 0}
    with merge_mod._publication_owner(root) as (owner, work):
        def callback():
            calls["count"] += 1
            if scenario == "reentrant" and calls["count"] == 1:
                before = list(owner.stream_roles.get("mounts", []))
                with pytest.raises(merge_mod.MergeError, match="rejects the call"):
                    merge_mod.fs_type(tmp_path, work=work)
                assert owner.stream_roles["mounts"] == before == ["mounts"]

        _hook_mounts_read(monkeypatch, callback, created)
        merge_mod.fs_type(tmp_path, work=work)
        merge_mod.fs_type(tmp_path, work=work)
        keys = owner.stream_roles["mounts"]
        assert keys == ["mounts", "mounts#1"]
        assert len(created) == 2 and created[0] is not created[1]
        assert all(proxy.closed for proxy in created)
        assert all(owner.streams[key] is None for key in keys)
        assert not owner.uncertain and not owner.tokens
    assert str(root) not in merge_mod._publication_owners


def test_q1_out_unknown(tmp_path, monkeypatch):
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == "/proc/mounts":
            raise OSError("mounts unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert merge_mod._publication_current.__dict__.get("owner") is None
    assert merge_mod.fs_type(tmp_path) == "unknown"
    merge_mod.refuse_ram_fs("out", tmp_path)
    monkeypatch.setattr(merge_mod, "fs_type", lambda path: "tmpfs")
    with pytest.raises(merge_mod.MergeError, match="is on tmpfs"):
        merge_mod.refuse_ram_fs("out", tmp_path)


# ---- 진입 규칙 ----


def _count_connects(monkeypatch):
    real = sqlite3.connect
    box = {"count": 0}

    def connect(*args, **kwargs):
        box["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(merge_mod.sqlite3, "connect", connect)
    return box


def _entry_q1(tmp_path, monkeypatch, entry):
    root = tmp_path / "q1-entry"
    connects = _count_connects(monkeypatch)
    spare = sqlite3.connect(":memory:")
    connects["count"] = 0
    actions = {
        "connect-other-thread": lambda owner, work: owner.connect(":memory:", work=work),
        "register-other-thread": lambda owner, work: owner.register(spare, work=work),
        "cleanup-other-thread": lambda owner, work: owner.close_remaining(),
        "cleanup-callback": lambda owner, work: owner.close_remaining(),
        "connect-callback": lambda owner, work: owner.connect(":memory:", work=work),
        "q1-callback": lambda owner, work: owner.read_mounts(work),
    }
    with merge_mod._publication_owner(root) as (owner, work):
        action = actions[entry]
        if entry.endswith("callback"):
            seen = {}

            def callback():
                assert "q1" in owner.tokens
                try:
                    action(owner, work)
                except merge_mod.MergeError as exc:
                    seen["error"] = exc
                assert owner.connections == [] and not owner.uncertain

            _hook_mounts_read(monkeypatch, callback)
            merge_mod.fs_type(tmp_path, work=work)
            assert isinstance(seen["error"], merge_mod.MergeError) and "rejects the call" in str(seen["error"])
            assert owner.stream_roles["mounts"] == ["mounts"]
        else:
            thread, gate, box = _start_blocked_q1(monkeypatch, owner, work)
            try:
                result = _run_thread(lambda: action(owner, work))
                assert isinstance(result.get("error"), merge_mod.MergeError)
                assert "rejects the call" in str(result["error"])
                assert owner.connections == [] and connects["count"] == 0 and not owner.uncertain
                assert "q1" in owner.tokens
            finally:
                _finish_blocked_q1(thread, gate, box)
        assert connects["count"] == 0 and owner.connections == [] and not owner.tokens
        assert owner.streams[owner.stream_roles["mounts"][0]] is None
    spare.close()
    assert str(root) not in merge_mod._publication_owners


def _entry_inflight(tmp_path, monkeypatch, entry):
    root = tmp_path / "q1-inflight"
    target = tmp_path / "inflight-target"
    target.write_bytes(b"x")
    gate = _Gate()
    real_open, real_fdopen, real_flock, real_lstat = os.open, os.fdopen, merge_mod.fcntl.flock, os.lstat
    lock_path = str(tmp_path / (root.name + ".lifetime.lock"))

    def open_spy(path, *args, **kwargs):
        if entry == "open-inflight" and str(path) == str(target):
            gate.block()
        return real_open(path, *args, **kwargs)

    def fdopen_spy(fd, mode, *args, **kwargs):
        if entry == "fdopen-inflight" and mode == "wb":
            gate.block()
        return real_fdopen(fd, mode, *args, **kwargs)

    def flock_spy(fd, operation):
        if entry == "flock-inflight" and operation & merge_mod.fcntl.LOCK_SH:
            gate.block()
        return real_flock(fd, operation)

    def lstat_spy(path, *args, **kwargs):
        if entry == "root-acquire-inflight" and str(path) == lock_path:
            gate.block()
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_spy)
    monkeypatch.setattr(os, "fdopen", fdopen_spy)
    monkeypatch.setattr(merge_mod.fcntl, "flock", flock_spy)
    monkeypatch.setattr(os, "lstat", lstat_spy)
    with merge_mod._publication_owner(root) as (owner, work):
        if entry == "fdopen-inflight":
            fd = owner.open_fd("intent", tmp_path / "inflight-intent", os.O_WRONLY | os.O_CREAT, 0o600, work=work)
        if entry == "flock-inflight":
            fd = owner.open_fd("source-lock", target, os.O_RDONLY, work=work)
        actions = {
            "open-inflight": lambda: owner.open_fd("probe", target, os.O_RDONLY, work=work),
            "fdopen-inflight": lambda: owner.fdopen("intent", fd, "wb", work=work),
            "flock-inflight": lambda: owner.flock(fd, merge_mod.fcntl.LOCK_SH | merge_mod.fcntl.LOCK_NB, work=work),
            "root-acquire-inflight": lambda: owner.acquire_root_lock(root, work=work),
        }
        import threading

        box = {}

        def runner():
            try:
                box["value"] = actions[entry]()
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=runner)
        thread.start()
        assert gate.entered.wait(10)
        try:
            assert owner.inflight == 1
            mark_slots = dict(owner.fd_slots)
            for call in (lambda: owner.read_mounts(work), owner.close_remaining):
                _rejected(call)
            assert owner.stream_roles.get("mounts") is None and not owner.tokens
            assert {key: value for key, value in owner.fd_slots.items() if key in mark_slots} == mark_slots
        finally:
            gate.release.set()
            thread.join(10)
        assert not thread.is_alive() and "error" not in box, box.get("error")
        assert owner.inflight == 0
        if entry == "open-inflight":
            assert owner.fd_slots["probe"] is not None
        if entry == "fdopen-inflight":
            assert owner.streams["intent"] is not None and owner.fd_slots["intent"] is None
        if entry == "flock-inflight":
            assert owner.source_state == "locked" and owner.source_locked
        if entry == "root-acquire-inflight":
            assert owner.root_lock_state == "locked" and owner.root_registry_state == "registered"
        # 예약이 끝난 뒤에는 Q1이 정상 진입한다.
        monkeypatch.setattr(os, "open", real_open)
        monkeypatch.setattr(os, "fdopen", real_fdopen)
        monkeypatch.setattr(os, "lstat", real_lstat)
        owner.read_mounts(work)
    monkeypatch.setattr(merge_mod.fcntl, "flock", real_flock)
    assert str(root) not in merge_mod._publication_owners


def _entry_idle(tmp_path):
    events = []
    owner = merge_mod._PublicationOwner(tmp_path / "q1-idle")
    owner.register(_ConnectionDouble("a", events))
    result = _run_thread(owner.close_remaining)
    assert "error" not in result and events == ["a"] and not owner.connections
    with pytest.raises(merge_mod.MergeError, match="already started"):
        owner.close_remaining()
    assert events == ["a"]


def _entry_rejected_finally(tmp_path, monkeypatch):
    root = tmp_path / "q1-rejected-finally"
    fd_holder = {}
    with pytest.raises(merge_mod.MergeError, match="cleanup is rejected") as rejected:
        with merge_mod._publication_owner(root) as (owner, work):
            fd_holder["fd"] = owner.open_fd("probe", tmp_path, os.O_RDONLY | os.O_DIRECTORY, work=work)
            thread, gate, box = _start_blocked_q1(monkeypatch, owner, work)
    assert rejected.value is owner.error_log[-1][2]
    # body 종료가 Q1과 겹치면 정상 종료가 아니다. 자원은 그대로 두고 종료 pending을 기록한다.
    retained = merge_mod._publication_owners[str(root)]
    assert retained is owner and owner.pending and not owner.cleanup_started
    assert owner.error_log[-1][0] == "cleanup" and owner.error_log[-1][3] == "rejected"
    assert owner.cleanup_error is owner.error_log[-1][2]
    assert os.fstat(fd_holder["fd"]) and owner.fd_slots["probe"] == fd_holder["fd"]
    _finish_blocked_q1(thread, gate, box)
    # Q1이 끝나도 정리를 자동 재시도하지 않는다.
    assert owner.pending and not owner.cleanup_started and owner.fd_slots["probe"] == fd_holder["fd"]
    with pytest.raises(merge_mod.MergeError, match="already exists"):
        with merge_mod._publication_owner(root):
            pass
    _discard_owner(owner)


@pytest.mark.parametrize("entry", ["connect-other-thread", "register-other-thread", "cleanup-other-thread",
                                   "cleanup-callback", "connect-callback", "q1-callback", "open-inflight",
                                   "fdopen-inflight", "flock-inflight", "root-acquire-inflight",
                                   "cleanup-idle-other-thread", "cleanup-rejected-finally"])
def test_q1_entry(tmp_path, monkeypatch, entry):
    if entry in {"open-inflight", "fdopen-inflight", "flock-inflight", "root-acquire-inflight"}:
        _entry_inflight(tmp_path, monkeypatch, entry)
    elif entry == "cleanup-idle-other-thread":
        _entry_idle(tmp_path)
    elif entry == "cleanup-rejected-finally":
        _entry_rejected_finally(tmp_path, monkeypatch)
    else:
        _entry_q1(tmp_path, monkeypatch, entry)


# ---- work 사용과 cleanup 배제 ----


def _lifetime_lock(tmp_path, root):
    return tmp_path / (root.name + ".lifetime.lock")


@pytest.mark.parametrize("scenario", ["binding-fd-other-thread", "sql-other-thread", "binding-fd-callback",
                                      "sql-callback", "body-finally-race", "pending-primary"])
def test_q1_work_cleanup(tmp_path, monkeypatch, syscalls, scenario):
    root = tmp_path / "q1-work-cleanup"
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_bytes(b"{}")
    lock_path = _lifetime_lock(tmp_path, root)

    def attempts(owner, fd):
        return [owner.close_remaining, owner.release_root_lock,
                lambda: owner.close_fd("manifest-file", fd)]

    if scenario == "binding-fd-other-thread":
        with merge_mod._publication_owner(root) as (owner, work):
            handle = owner.acquire_root_lock(root, work=work)
            fd = owner.open_fd("manifest-file", manifest, os.O_RDONLY, work=work)
            mark = syscalls.mark()
            for call in attempts(owner, fd):
                result = _run_thread(call)
                assert isinstance(result.get("error"), merge_mod.MergeError)
                assert "rejects the call" in str(result["error"])
            assert syscalls.since(mark) == []
            assert os.pread(fd, 2, 0) == b"{}" and not handle.closed and owner.fd_slots["manifest-file"] == fd
            assert _flock_contended(lock_path) and not owner.connection_cleanup_started
    elif scenario == "sql-other-thread":
        with merge_mod._publication_owner(root) as (owner, work):
            conn = owner.connect(":memory:", work=work)
            conn.execute("CREATE TABLE t (x)")
            conn.execute("INSERT INTO t VALUES (7)")
            for call in (owner.close_remaining, conn.close, lambda: conn.close(work=None)):
                result = _run_thread(call)
                assert isinstance(result.get("error"), merge_mod.MergeError)
                assert "rejects the call" in str(result["error"])
            assert conn.execute("SELECT x FROM t").fetchone() == (7,)
            assert owner.connections == [conn] and not conn.failed and not owner.connection_cleanup_started
            conn.close(work=work)
            assert owner.connections == []
    elif scenario == "binding-fd-callback":
        real_pread = os.pread
        with merge_mod._publication_owner(root) as (owner, work):
            handle = owner.acquire_root_lock(root, work=work)
            fd = owner.open_fd("manifest-file", manifest, os.O_RDONLY, work=work)
            mark = syscalls.mark()
            seen = []

            def pread(target, size, offset):
                for call in attempts(owner, fd):
                    with pytest.raises(merge_mod.MergeError, match="rejects the call"):
                        call()
                    seen.append(call)
                return real_pread(target, size, offset)

            monkeypatch.setattr(os, "pread", pread)
            assert os.pread(fd, 2, 0) == b"{}"
            assert len(seen) == 3 and syscalls.since(mark) == [] and not handle.closed
            monkeypatch.setattr(os, "pread", real_pread)
    elif scenario == "sql-callback":
        with merge_mod._publication_owner(root) as (owner, work):
            handle = owner.acquire_root_lock(root, work=work)
            conn = owner.connect(":memory:", work=work)
            seen = []

            def callback():
                for call in (owner.close_remaining, owner.release_root_lock, lambda: conn.close()):
                    try:
                        call()
                    except merge_mod.MergeError as exc:
                        seen.append(exc)
                return len(seen)

            conn.create_function("cb", 0, callback)
            assert conn.execute("SELECT cb()").fetchone() == (3,)
            assert all("rejects the call" in str(exc) for exc in seen)
            assert owner.connections == [conn] and not owner.connection_cleanup_started and not handle.closed
            conn.close(work=work)
    elif scenario == "body-finally-race":
        events = []
        record = []
        original = merge_mod._PublicationOwner._finish_work

        def finish(self):
            record.append(("before", _run_thread(self.close_remaining)))
            token = original(self)
            record.append(("after", _run_thread(self.close_remaining)))
            record.append(("release", _run_thread(self.release_root_lock)))
            return token

        monkeypatch.setattr(merge_mod._PublicationOwner, "_finish_work", finish)
        with merge_mod._publication_owner(root) as (owner, work):
            owner.register(_ConnectionDouble("c", events), work=work)
            handle = owner.acquire_root_lock(root, work=work)
        # 한쪽만 진입한다. body 종료 전에는 work 때문에, 종료 뒤에는 cleanup 토큰 때문에 외부가 거부된다.
        assert [label for label, _ in record] == ["before", "after", "release"]
        assert all(isinstance(result.get("error"), merge_mod.MergeError) for _, result in record)
        assert "work is active" in str(record[0][1]["error"])
        assert "already started" in str(record[1][1]["error"])
        assert "cleanup is in progress" in str(record[2][1]["error"])
        assert events == ["c"] and handle.closed and not _flock_contended(lock_path)
    else:
        primary = ValueError("body primary")
        cause = RuntimeError("explicit cause")
        primary.__cause__ = cause
        with pytest.raises(ValueError) as caught:
            with merge_mod._publication_owner(root) as (owner, work):
                fd = owner.open_fd("manifest-file", manifest, os.O_RDONLY, work=work)
                thread, gate, box = _start_blocked_q1(monkeypatch, owner, work)
                raise primary
        assert caught.value is primary and primary.__cause__ is cause
        assert merge_mod._publication_owners[str(root)] is owner
        assert owner.pending and not owner.cleanup_started and not owner.work_active
        stage, instance, rejection, kind = owner.error_log[-1]
        assert (stage, kind) == ("cleanup", "rejected") and isinstance(rejection, merge_mod.MergeError)
        assert owner.fd_slots["manifest-file"] == fd and syscalls.count("close", fd) == 0
        _finish_blocked_q1(thread, gate, box)
        assert owner.pending and owner.fd_slots["manifest-file"] == fd and syscalls.count("close", fd) == 0
        assert primary.__cause__ is cause
        _discard_owner(owner)
        return
    assert str(root) not in merge_mod._publication_owners


# ---- 오류 객체와 순서 ----


def _failing_stream(monkeypatch, read_error, close_error):
    real = os.fdopen

    def fdopen(fd, mode):
        stream = real(fd, mode)
        if mode != "r":
            return stream

        class Proxy:
            def read(self):
                raise read_error

            def close(self):
                stream.close()
                raise close_error

        return Proxy()

    monkeypatch.setattr(merge_mod.os, "fdopen", fdopen)


@pytest.mark.parametrize("scenario", ["read-close-cleanup", "multiple-connection-close", "test-retry-failure",
                                      "nested-finally"])
def test_q1_error_objects(source_dir, tmp_path, monkeypatch, syscalls, scenario):
    root = tmp_path / "q1-error-objects"
    lock_path = _lifetime_lock(tmp_path, root)
    if scenario == "read-close-cleanup":
        read_error, close_error = OSError("mounts read failure"), OSError("mounts close failure")
        cleanup_error = RuntimeError("cleanup close failure")
        _failing_stream(monkeypatch, read_error, close_error)
        events = []
        with pytest.raises(OSError, match="mounts read failure") as caught:
            with merge_mod._publication_owner(root) as (owner, work):
                try:
                    merge_mod.fs_type(tmp_path, work=work)
                except OSError as exc:
                    q1_error = exc
                owner.register(_ConnectionDouble("c", events, cleanup_error), work=work)
                raise q1_error
        # read가 primary이고 최초 close 오류가 명시적 cause다. 뒤의 cleanup 오류는 그 둘을 덮지 않는다.
        assert caught.value is read_error and read_error.__cause__ is close_error
        assert owner.cleanup_error is cleanup_error and events == ["c"]
        assert [(stage, error) for stage, _, error, _ in owner.error_log] == [
            ("stream-close", close_error), ("connection-close", cleanup_error)]
        assert merge_mod._publication_owners[str(root)] is owner and owner.uncertain
        _discard_owner(owner)
    elif scenario == "multiple-connection-close":
        first, second = RuntimeError("close 1"), RuntimeError("close 2")
        primary = KeyboardInterrupt("primary")
        events = []
        with pytest.raises(KeyboardInterrupt) as caught:
            with merge_mod._publication_owner(root) as (owner, work):
                handle = owner.acquire_root_lock(root, work=work)
                fd = owner.open_fd("manifest-file", tmp_path, os.O_RDONLY | os.O_DIRECTORY, work=work)
                for label, error in (("0", None), ("1", first), ("2", second), ("3", None)):
                    owner.register(_ConnectionDouble(label, events, error), work=work)
                mark = syscalls.mark()
                raise primary
        assert caught.value is primary and primary.__cause__ is first
        assert events == ["0", "1", "2", "3"]
        assert [error for stage, _, error, _ in owner.error_log if stage == "connection-close"] == [first, second]
        assert len(owner.connections) == 2 and all(conn.failed for conn in owner.connections)
        assert owner.cleanup_error is first and owner.uncertain
        # 연결이 남았으므로 자원 해제 호출이 없다.
        assert syscalls.since(mark) == []
        assert owner.root_lock is handle and not handle.closed and owner.fd_slots["manifest-file"] == fd
        assert _flock_contended(lock_path)
        _discard_owner(owner)
    elif scenario == "test-retry-failure":
        retry_error = RuntimeError("retry failed")
        retried = []

        class RetryFails:
            calls = 0

            def close(self):
                RetryFails.calls += 1
                if RetryFails.calls == 1:
                    raise merge_mod._PublicationCloseBeforeTest(lambda: retried.append("unblocked"))
                raise retry_error

        primary = ValueError("primary")
        with pytest.raises(ValueError) as caught:
            with merge_mod._publication_owner(root) as (owner, work):
                handle = owner.acquire_root_lock(root, work=work)
                owner.register(RetryFails(), work=work)
                mark = syscalls.mark()
                raise primary
        assert caught.value is primary and primary.__cause__ is retry_error
        assert retried == ["unblocked"] and RetryFails.calls == 2
        log = [(stage, kind) for stage, _, _, kind in owner.error_log]
        assert log == [("connection-close", "test-first"), ("connection-close", "test-retry")]
        assert isinstance(owner.error_log[0][2], merge_mod._PublicationCloseBeforeTest)
        assert owner.error_log[1][2] is retry_error
        assert len(owner.connections) == 1 and owner.uncertain
        assert syscalls.since(mark) == [] and not handle.closed and _flock_contended(lock_path)
        _discard_owner(owner)
    else:
        (tmp_path / "src.source.lifetime.lock").touch()
        # 원래 primary가 있으면 중첩 finally의 거부 객체는 순서 기록에만 남고 primary가 전파된다.
        primary = ValueError("nested primary")
        with pytest.raises(ValueError) as caught:
            with (merge_mod._publication_owner(root) as (owner, work),
                  merge_mod._publication_source_lock(source_dir, work=work)):
                thread, gate, box = _start_blocked_q1(monkeypatch, owner, work)
                raise primary
        assert caught.value is primary
        assert [(stage, kind) for stage, _, _, kind in owner.error_log] == [
            ("deferred-close", "rejected"), ("deferred-close", "rejected"), ("cleanup", "rejected")]
        assert len({id(error) for _, _, error, _ in owner.error_log}) == 3
        assert owner.pending and owner.fd_slots["source-lock"] is not None
        assert _flock_contended(tmp_path / "src.source.lifetime.lock")
        _finish_blocked_q1(thread, gate, box)
        _discard_owner(owner)
        # primary가 없으면 첫 거부 객체가 전파되고 남은 정리는 실행되지 않는다.
        other = tmp_path / "q1-error-objects-second"
        with pytest.raises(merge_mod.MergeError, match="rejects the call") as rejected:
            with (merge_mod._publication_owner(other) as (owner, work),
                  merge_mod._publication_source_lock(source_dir, work=work)):
                thread, gate, box = _start_blocked_q1(monkeypatch, owner, work)
        assert rejected.value is owner.error_log[0][2] and owner.error_log[0][0] == "deferred-close"
        assert owner.pending
        _finish_blocked_q1(thread, gate, box)
        _discard_owner(owner)
    if scenario != "nested-finally":
        assert not owner.tokens


# ---- work capability ----


@pytest.mark.parametrize("scenario", ["valid", "missing", "other-owner", "expired"])
def test_q1_work_capability(source_dir, tmp_path, monkeypatch, syscalls, scenario):
    root = tmp_path / "q1-work-capability"
    flags = os.O_RDONLY | os.O_DIRECTORY
    opens, connects = [], _count_connects(monkeypatch)
    real_open = os.open

    def open_spy(path, *args, **kwargs):
        opens.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_spy)
    spare = sqlite3.connect(":memory:")
    connects["count"] = 0
    if scenario == "valid":
        with merge_mod._publication_owner(root) as (owner, work):
            fd = owner.open_fd("probe", tmp_path, flags, work=work)
            conn = merge_mod._connect(source_dir / "graph.db", mode="ro", work=work)
            assert conn.execute("SELECT count(*) FROM graph_nodes").fetchone() == (30,)
            assert owner.connections == [conn]
            merge_mod._close_conn(conn, work)
            assert owner.connections == []
            owner.close_fd("probe", fd, work=work)
            assert owner.fd_slots["probe"] == fd and "probe" in owner.deferred_fds
        with pytest.raises(OSError):
            os.fstat(fd)
        assert str(root) not in merge_mod._publication_owners
        spare.close()
        return
    if scenario == "other-owner":
        stranger = merge_mod._PublicationOwner(tmp_path / "q1-stranger")
        foreign = merge_mod._PublicationWork(stranger)
        foreign.active = True
        stranger.work, stranger.work_active = foreign, True
        credentials = foreign
        pattern = "not valid"
    ctx = merge_mod._publication_owner(root)
    owner, work = ctx.__enter__()
    fd = owner.open_fd("probe", tmp_path, flags, work=work)
    conn = owner.connect(":memory:", work=work)
    mark = syscalls.mark()
    snapshot = (dict(owner.fd_slots), {key: list(value) for key, value in owner.fd_roles.items()},
                list(owner.connections), len(opens), connects["count"])
    if scenario == "expired":
        conn.close(work=work)
        owner.uncertain = True  # 종료 뒤에도 owner가 남게 한다
        ctx.__exit__(None, None, None)
        credentials, pattern = work, "not valid"
        snapshot = (dict(owner.fd_slots), {key: list(value) for key, value in owner.fd_roles.items()},
                    list(owner.connections), len(opens), connects["count"])
        mark = syscalls.mark()
    elif scenario == "missing":
        credentials, pattern = None, "capability is missing"
    wrong = {"missing": {}, "other-owner": {"work": credentials}, "expired": {"work": credentials}}[scenario]
    calls = [lambda: owner.open_fd("x", tmp_path, flags, **wrong),
             lambda: owner.connect(":memory:", **wrong),
             lambda: owner.register(spare, **wrong),
             lambda: owner.close_fd("probe", fd, **wrong),
             lambda: owner.fdopen("intent", fd, "wb", **wrong),
             lambda: owner.acquire_root_lock(root, **wrong),
             lambda: owner.flock(fd, merge_mod.fcntl.LOCK_SH | merge_mod.fcntl.LOCK_NB, **wrong)]
    if scenario != "expired":
        calls.append(lambda: conn.close(**wrong))
    for call in calls:
        with pytest.raises(merge_mod.MergeError, match=pattern):
            call()
    after = (dict(owner.fd_slots), {key: list(value) for key, value in owner.fd_roles.items()},
             list(owner.connections), len(opens), connects["count"])
    assert after == snapshot and "x" not in owner.fd_slots and "intent" not in owner.stream_roles
    assert syscalls.since(mark) == []
    if scenario != "expired":
        assert conn.execute("SELECT 1").fetchone() == (1,) and not conn.failed
        conn.close(work=work)
        ctx.__exit__(None, None, None)
        assert str(root) not in merge_mod._publication_owners
    else:
        assert merge_mod._publication_owners[str(root)] is owner and owner.uncertain and not owner.pending
        _discard_owner(owner)
    spare.close()


@pytest.mark.parametrize("scenario", ["execute-register"])
def test_q1_callback_without_capability(tmp_path, monkeypatch, scenario):
    root = tmp_path / "q1-callback-capability"
    connects = _count_connects(monkeypatch)
    spare = sqlite3.connect(":memory:")
    connects["count"] = 0
    with merge_mod._publication_owner(root) as (owner, work):
        conn = owner.connect(":memory:", work=work)
        seen = []
        before = connects["count"]

        def callback():
            for call in (lambda: owner.register(spare), lambda: owner.connect(":memory:")):
                try:
                    call()
                except merge_mod.MergeError as exc:
                    seen.append(exc)
            return 1

        conn.create_function("cb", 0, callback)
        assert conn.execute("SELECT cb()").fetchone() == (1,)
        assert len(seen) == 2 and all("capability is missing" in str(exc) for exc in seen)
        assert owner.connections == [conn] and connects["count"] == before
        conn.close(work=work)
    spare.close()
    assert str(root) not in merge_mod._publication_owners


# ---- 직접 최초 cleanup ----


@pytest.mark.parametrize("scenario", ["idle-other-thread", "connections-only", "concurrent-reject"])
def test_q1_direct_cleanup(tmp_path, syscalls, scenario):
    events = []
    root = tmp_path / "q1-direct-cleanup"
    owner = merge_mod._PublicationOwner(root)
    if scenario == "idle-other-thread":
        owner.register(_ConnectionDouble("a", events))
        owner.register(_ConnectionDouble("b", events))
        result = _run_thread(owner.close_remaining)
        assert "error" not in result and events == ["a", "b"] and owner.connections == []
        assert owner.connection_cleanup_started and not owner.tokens
        with pytest.raises(merge_mod.MergeError, match="already started"):
            owner.close_remaining()
        assert events == ["a", "b"]
    elif scenario == "connections-only":
        handle = owner.acquire_root_lock(root)
        fd = owner.open_fd("probe", tmp_path, os.O_RDONLY | os.O_DIRECTORY, category="independent")
        intent_fd = owner.open_fd("intent", tmp_path / "direct-intent", os.O_WRONLY | os.O_CREAT, 0o600)
        stream = owner.fdopen("intent", intent_fd, "wb")
        owner.register(_ConnectionDouble("a", events))
        mark = syscalls.mark()
        owner.close_remaining()
        # 직접 수집은 연결만 닫는다. fd, 잠금, stream은 해제하지 않는다.
        assert events == ["a"] and owner.connections == []
        assert syscalls.since(mark) == []
        assert owner.root_lock is handle and not handle.closed and not stream.closed
        assert owner.fd_slots["probe"] == fd and owner.root_lock_state == "locked"
        assert _flock_contended(_lifetime_lock(tmp_path, root))
        owner.release_root_lock()
        stream.close()
        os.close(fd)
    else:
        gate = _Gate()
        retry_events = []

        class CloseBefore:
            ready = False

            def unblock(self):
                retry_events.append("unblock")
                gate.block()
                self.ready = True

            def close(self):
                retry_events.append("close")
                if not self.ready:
                    raise merge_mod._PublicationCloseBeforeTest(self.unblock)

        owner.register(CloseBefore())
        handle = owner.acquire_root_lock(root)
        probe = owner.open_fd("probe", tmp_path, os.O_RDONLY | os.O_DIRECTORY, category="independent")
        import threading

        worker = threading.Thread(target=owner.close_remaining)
        worker.start()
        try:
            assert gate.entered.wait(10)
            mark = syscalls.mark()
            for call in (owner.close_remaining, owner.release_root_lock,
                         lambda: owner.close_fd("probe")):
                with pytest.raises(merge_mod.MergeError, match="rejects the call|already started"):
                    call()
            assert syscalls.since(mark) == [] and not handle.closed
            assert retry_events == ["close", "unblock"]
        finally:
            gate.release.set()
            worker.join(10)
        assert not worker.is_alive() and retry_events == ["close", "unblock", "close"]
        assert owner.connections == [] and not owner.tokens
        owner.release_root_lock()
        os.close(probe)


# ---- 직접 root release와 fd close ----


def _direct_root_owner(tmp_path, name):
    root = tmp_path / name
    owner = merge_mod._PublicationOwner(root)
    handle = owner.acquire_root_lock(root)
    _reset_syscalls()
    return owner, handle, handle.fileno(), _lifetime_lock(tmp_path, root)


@pytest.mark.parametrize("scenario", ["root-idle-success", "fd-idle-success", "root-uncertain", "fd-uncertain",
                                      "q1-conflict", "work-conflict", "inflight-conflict", "cleanup-conflict",
                                      "callback-conflict", "consumer-open"])
def test_q1_direct_release(tmp_path, monkeypatch, syscalls, scenario):
    from opencrab import locking

    flags = os.O_RDONLY | os.O_DIRECTORY
    if scenario == "root-idle-success":
        owner, handle, fileno, lock = _direct_root_owner(tmp_path, "release-root")
        assert _flock_contended(lock)
        owner.release_root_lock()
        assert syscalls.count("unlock", fileno) == 1 and handle.closed and owner.root_lock is None
        assert (owner.root_lock_state, owner.root_registry_state) == ("closed", "removed")
        assert handle not in locking._lifetime_handles and not owner.tokens
        assert not _flock_contended(lock)
        owner.release_root_lock()
        assert syscalls.count("unlock", fileno) == 1
    elif scenario == "fd-idle-success":
        owner = merge_mod._PublicationOwner(tmp_path / "release-fd")
        fd = owner.open_fd("probe", tmp_path, flags, category="independent")
        owner.close_fd("probe", fd)
        assert syscalls.count("close", fd) == 0 and owner.fd_slots["probe"] == fd  # 지연 등록만 한다
        owner.cleanup_started = True
        owner.close_fd("probe", fd)
        assert syscalls.count("close", fd) == 1 and owner.fd_slots["probe"] is None and not owner.tokens
        owner.close_fd("probe", fd)
        assert syscalls.count("close", fd) == 1
    elif scenario == "root-uncertain":
        for timing in ("before", "after"):
            owner, handle, fileno, lock = _direct_root_owner(tmp_path, "release-root-" + timing)
            syscalls.unlock_fault[fileno] = timing
            with pytest.raises(OSError, match="injected unlock"):
                owner.release_root_lock()
            assert owner.uncertain and owner.root_lock_state == "uncertain" and owner.root_lock is handle
            assert owner.root_registry_state == "registered" and not owner.tokens
            owner.release_root_lock()
            assert syscalls.count("unlock", fileno) == 1 and not handle.closed
            assert _flock_contended(lock) is (timing == "before")
            syscalls.unlock_fault.pop(fileno)
            locking.release_file_lock(handle)
    elif scenario == "fd-uncertain":
        for timing in ("before", "after"):
            owner = merge_mod._PublicationOwner(tmp_path / ("release-fd-" + timing))
            fd = owner.open_fd("probe", tmp_path, flags, category="independent")
            _reset_syscalls()
            owner.cleanup_started = True
            syscalls.close_fault[fd] = timing
            with pytest.raises(OSError, match="injected close"):
                owner.close_fd("probe", fd)
            assert owner.uncertain and owner.fd_slots["probe"] == fd and not owner.tokens
            owner.close_fd("probe", fd)
            assert syscalls.count("close", fd) == 1
            if timing == "before":
                syscalls.real_close(fd)
    elif scenario == "consumer-open":
        owner, handle, fileno, lock = _direct_root_owner(tmp_path, "release-consumer")
        fd = owner.open_fd("probe", tmp_path, flags, category="independent")
        owner.register(_ConnectionDouble("c", []))
        owner.cleanup_started = True
        mark = syscalls.mark()
        owner.release_root_lock()
        owner.close_fd("probe", fd)
        assert syscalls.since(mark) == [] and not handle.closed and owner.fd_slots["probe"] == fd
        assert _flock_contended(lock) and not owner.tokens
        owner.close_remaining()
        owner.release_root_lock()
        owner.close_fd("probe", fd)
        assert handle.closed and owner.fd_slots["probe"] is None
    else:
        _direct_release_conflict(tmp_path, monkeypatch, syscalls, scenario)


def _direct_release_conflict(tmp_path, monkeypatch, syscalls, scenario):
    flags = os.O_RDONLY | os.O_DIRECTORY
    rejects = []

    def attempt(owner, fd):
        for call in (owner.release_root_lock, lambda: owner.close_fd("probe", fd)):
            try:
                call()
            except merge_mod.MergeError as exc:
                rejects.append(exc)

    if scenario in {"q1-conflict", "inflight-conflict", "callback-conflict"}:
        owner, handle, fileno, lock = _direct_root_owner(tmp_path, "conflict-" + scenario)
        fd = owner.open_fd("probe", tmp_path, flags, category="independent")
        owner.cleanup_started = True
        mark = syscalls.mark()
        if scenario == "q1-conflict":
            _hook_mounts_read(monkeypatch, lambda: attempt(owner, fd))
            owner.read_mounts(None)
        elif scenario == "inflight-conflict":
            gate = _Gate()
            target = tmp_path / "conflict-target"
            target.write_bytes(b"x")
            real_open = os.open

            def open_spy(path, *args, **kwargs):
                if str(path) == str(target):
                    gate.block()
                return real_open(path, *args, **kwargs)

            monkeypatch.setattr(os, "open", open_spy)
            import threading

            worker = threading.Thread(target=lambda: owner.open_fd("blocked", target, os.O_RDONLY))
            worker.start()
            assert gate.entered.wait(10)
            try:
                attempt(owner, fd)
            finally:
                gate.release.set()
                worker.join(10)
            monkeypatch.setattr(os, "open", real_open)
            assert owner.fd_slots["blocked"] is not None
            syscalls.real_close(owner.fd_slots["blocked"])
        else:
            fired = []

            def on_event(kind, number):
                if kind == "unlock" and number == fileno and not fired:
                    fired.append(number)
                    attempt(owner, fd)

            syscalls.on_event = on_event
            mark = syscalls.mark()
            owner.release_root_lock()
            syscalls.on_event = None
            # 바깥 호출은 성공하고 중첩 호출만 거부된다.
            assert handle.closed and owner.root_lock is None
            assert [event for event in syscalls.since(mark) if event[0] == "unlock"] == [("unlock", fileno)]
            assert syscalls.count("close", fd) == 0
            assert len(rejects) == 2 and all("rejects the call" in str(exc) for exc in rejects)
            owner.close_fd("probe", fd)
            assert owner.fd_slots["probe"] is None
            return
        assert len(rejects) == 2 and all("rejects the call" in str(exc) for exc in rejects)
        assert syscalls.since(mark) == [] and not handle.closed and owner.fd_slots["probe"] == fd
        assert _flock_contended(lock)
        owner.release_root_lock()
        owner.close_fd("probe", fd)
        return
    root = tmp_path / ("conflict-" + scenario)
    if scenario == "work-conflict":
        with merge_mod._publication_owner(root) as (owner, work):
            handle = owner.acquire_root_lock(root, work=work)
            fd = owner.open_fd("probe", tmp_path, flags, category="independent", work=work)
            owner.cleanup_started = True  # 실제 close가 허용되는 상태를 흉내 내도 work가 있으면 거부한다.
            mark = syscalls.mark()
            attempt(owner, fd)
            assert len(rejects) == 2 and all("work is active" in str(exc) for exc in rejects)
            assert syscalls.since(mark) == [] and not handle.closed and owner.fd_slots["probe"] == fd
            owner.cleanup_started = False
        assert handle.closed and str(root) not in merge_mod._publication_owners
        return
    # cleanup-conflict: 최초 cleanup이 연결을 수집하는 동안 바깥 호출이 들어온다.
    seen = {}

    class Probe:
        def close(self):
            seen["mark"] = syscalls.mark()
            attempt(owner, seen["fd"])
            seen["after"] = syscalls.since(seen["mark"])

    with merge_mod._publication_owner(root) as (owner, work):
        handle = owner.acquire_root_lock(root, work=work)
        fileno = handle.fileno()
        seen["fd"] = owner.open_fd("probe", tmp_path, flags, category="independent", work=work)
        owner.register(Probe(), work=work)
    assert len(rejects) == 2 and all("rejects the call" in str(exc) for exc in rejects)
    assert seen["after"] == [] and handle.closed and str(root) not in merge_mod._publication_owners
    assert syscalls.count("unlock", fileno) == 1 and syscalls.count("close", seen["fd"]) == 1


# ---- F1 순서 ----


def _source_owner(tmp_path, name, *, root=False):
    owner = merge_mod._PublicationOwner(tmp_path / name)
    lock = tmp_path / (name + ".source.lifetime.lock")
    lock.touch()
    parent = owner.open_fd("source-parent", tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source = owner.open_fd("source-lock", lock.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    owner.flock(source, merge_mod.fcntl.LOCK_SH | merge_mod.fcntl.LOCK_NB)
    handle = owner.acquire_root_lock(tmp_path / name) if root else None
    _reset_syscalls()
    return owner, lock, parent, source, handle


def _release_source(owner, source, parent):
    owner.cleanup_started = True
    owner.flock(source, merge_mod.fcntl.LOCK_UN)
    owner.close_fd("source-lock")
    owner.close_fd("source-parent")


@pytest.mark.parametrize("scenario", ["root-locked-source-close", "root-unlock-incomplete", "root-close-incomplete",
                                      "root-registry-incomplete", "source-fd-held-unlock",
                                      "source-unlock-incomplete", "all-prior-complete", "known-absent",
                                      "unknown-role", "adopted-protected", "repeat-instances", "internal-cleanup",
                                      "close-uncertain-no-retry"])
def test_q1_fd_order(source_dir, tmp_path, monkeypatch, syscalls, scenario):
    from opencrab import locking

    flags = os.O_RDONLY | os.O_DIRECTORY
    name = "fd-order"
    root = tmp_path / name
    root_lock = _lifetime_lock(tmp_path, root)
    if scenario == "root-locked-source-close":
        owner, lock, parent, source, handle = _source_owner(tmp_path, name, root=True)
        owner.cleanup_started = True
        mark = syscalls.mark()
        for role in ("source-lock", "source-parent"):
            with pytest.raises(merge_mod.MergeError, match="root release is incomplete"):
                owner.close_fd(role)
        assert syscalls.since(mark) == [] and _flock_contended(lock) and _flock_contended(root_lock)
        assert owner.fd_slots["source-lock"] == source and owner.fd_slots["source-parent"] == parent
        owner.release_root_lock()
        _release_source(owner, source, parent)
    elif scenario == "root-unlock-incomplete":
        owner, lock, parent, source, handle = _source_owner(tmp_path, name, root=True)
        fileno = handle.fileno()
        extra = owner.open_fd("source:graph.db", lock, os.O_RDONLY)
        owner.cleanup_started = True
        syscalls.unlock_fault[fileno] = "before"
        with pytest.raises(OSError, match="injected unlock"):
            owner.release_root_lock()
        mark = syscalls.mark()
        owner.close_fd("source:graph.db", extra)
        owner.flock(source, merge_mod.fcntl.LOCK_UN)
        owner.close_fd("source-lock")
        owner.close_fd("source-parent")
        assert syscalls.since(mark) == [] and owner.uncertain and owner.root_lock is handle
        assert _flock_contended(lock) and _flock_contended(root_lock)
        locking.release_file_lock(handle)
        _discard_owner(owner)
    elif scenario in {"root-close-incomplete", "root-registry-incomplete"}:
        owner, lock, parent, source, handle = _source_owner(tmp_path, name, root=True)
        extra = owner.open_fd("source:graph.db", lock, os.O_RDONLY)
        owner.cleanup_started = True
        if scenario == "root-close-incomplete":
            class CloseFailure:
                def fileno(self):
                    return handle.fileno()

                def close(self):
                    raise OSError("injected root close")

            owner.root_lock = CloseFailure()
            with pytest.raises(OSError, match="injected root close"):
                owner.release_root_lock()
            assert owner.root_lock_state == "uncertain" and not _flock_contended(root_lock)
            owner.root_lock = handle
        else:
            with locking._lifetime_handles_lock:
                del locking._lifetime_handles[handle]
            with pytest.raises(KeyError):
                owner.release_root_lock()
            assert owner.root_registry_state == "uncertain" and handle.closed
        mark = syscalls.mark()
        owner.close_fd("source:graph.db", extra)
        owner.flock(source, merge_mod.fcntl.LOCK_UN)
        owner.close_fd("source-lock")
        owner.close_fd("source-parent")
        assert syscalls.since(mark) == [] and owner.uncertain
        assert owner.fd_slots["source:graph.db"] == extra and _flock_contended(lock)
        if scenario == "root-close-incomplete":
            handle.close()
        _discard_owner(owner)
    elif scenario == "source-fd-held-unlock":
        owner, lock, parent, source, handle = _source_owner(tmp_path, name)
        held = owner.open_fd("source:graph.db", lock, os.O_RDONLY)
        owner.cleanup_started = True
        mark = syscalls.mark()
        with pytest.raises(merge_mod.MergeError, match="prerequisites"):
            owner.flock(source, merge_mod.fcntl.LOCK_UN)
        with pytest.raises(merge_mod.MergeError, match="still held"):
            owner.close_fd("source-lock")
        assert syscalls.since(mark) == [] and _flock_contended(lock) and owner.source_state == "locked"
        owner.close_fd("source:graph.db", held)
        _release_source(owner, source, parent)
    elif scenario == "source-unlock-incomplete":
        owner, lock, parent, source, handle = _source_owner(tmp_path, name)
        owner.cleanup_started = True
        mark = syscalls.mark()
        with pytest.raises(merge_mod.MergeError, match="not released"):
            owner.close_fd("source-lock")
        assert syscalls.since(mark) == [] and owner.source_state == "locked" and _flock_contended(lock)
        syscalls.unlock_fault[source] = "before"
        with pytest.raises(OSError, match="injected unlock"):
            owner.flock(source, merge_mod.fcntl.LOCK_UN)
        assert owner.source_state == "uncertain" and owner.uncertain
        owner.close_fd("source-lock")
        owner.close_fd("source-parent")
        assert syscalls.count("close", source) == 0 and syscalls.count("close", parent) == 0
        assert _flock_contended(lock)
        _discard_owner(owner)
    elif scenario == "all-prior-complete":
        owner, lock, parent, source, handle = _source_owner(tmp_path, name, root=True)
        fileno = handle.fileno()
        consumer = owner.open_fd("source:graph.db", lock, os.O_RDONLY)
        other = owner.open_fd("intent-parent", tmp_path, flags)
        owner.cleanup_started = True
        mark = syscalls.mark()
        owner.release_root_lock()
        owner.close_fd("source:graph.db", consumer)
        owner.close_fd("intent-parent", other)
        owner.flock(source, merge_mod.fcntl.LOCK_UN)
        owner.close_fd("source-lock")
        assert owner.source_state == "closed" and not _flock_contended(lock)
        owner.close_fd("source-parent")
        assert syscalls.since(mark) == [("unlock", fileno), ("close", consumer), ("close", other),
                                        ("unlock", source), ("close", source), ("close", parent)]
        assert all(slot is None for slot in owner.fd_slots.values()) and not owner.uncertain
        assert owner.source_state == "closed" and handle.closed and not _flock_contended(root_lock)
    elif scenario == "known-absent":
        owner = merge_mod._PublicationOwner(root)
        first = owner.open_fd("source", tmp_path, flags, category="independent")
        second = owner.open_fd("source", tmp_path, flags, category="independent")
        owner.cleanup_started = True
        assert (owner.root_lock_state, owner.source_state) == ("absent", "absent")
        owner.close_fd("source", first)
        owner.close_fd("source", second)
        assert [syscalls.count("close", first), syscalls.count("close", second)] == [1, 1]
        assert all(slot is None for slot in owner.fd_slots.values())
    elif scenario == "unknown-role":
        owner = merge_mod._PublicationOwner(root)
        mystery = owner.open_fd("mystery", tmp_path, flags)
        owner.cleanup_started = True
        with pytest.raises(merge_mod.MergeError, match="not classified"):
            owner.close_fd("mystery", mystery)
        assert syscalls.count("close", mystery) == 0 and owner.fd_slots["mystery"] == mystery
        for role, category, pattern in (("source-lock", "independent", "differs from the fixed"),
                                        ("source:graph.db", "independent", "differs from the fixed"),
                                        ("root-lock", "independent", "differs from the fixed"),
                                        ("mystery-2", "bogus", "category is unknown")):
            with pytest.raises(merge_mod.MergeError, match=pattern):
                owner.open_fd(role, tmp_path, flags, category=category)
            assert role not in owner.fd_slots and role not in owner.fd_roles
        syscalls.real_close(mystery)
    elif scenario == "adopted-protected":
        owner, lock, parent, source, handle = _source_owner(tmp_path, name, root=True)
        raw = os.open(tmp_path, flags)
        with pytest.raises(merge_mod.MergeError, match="explicit category"):
            owner.adopt_fd("adopted", raw)
        with pytest.raises(merge_mod.MergeError, match="differs from the fixed"):
            owner.adopt_fd("source-lock", raw, category="independent")
        owner.adopt_fd("adopted", raw, category="consumer")
        with pytest.raises(merge_mod.MergeError, match="already exists"):
            owner.adopt_fd("adopted", raw, category="consumer")
        owner.cleanup_started = True
        mark = syscalls.mark()
        with pytest.raises(merge_mod.MergeError, match="root release is incomplete"):
            owner.close_fd("adopted", raw)
        assert syscalls.since(mark) == [] and owner.fd_slots["adopted"] == raw
        owner.release_root_lock()
        owner.close_fd("adopted", raw)
        assert syscalls.count("close", raw) == 1 and owner.fd_slots["adopted"] is None
        _release_source(owner, source, parent)
    elif scenario == "repeat-instances":
        owner, lock, parent, source, handle = _source_owner(tmp_path, name)
        first = owner.open_fd("source:graph.db", lock, os.O_RDONLY)
        second = owner.open_fd("source:graph.db", lock, os.O_RDONLY)
        assert owner.fd_roles["source:graph.db"] == ["source:graph.db", "source:graph.db#1"]
        owner.cleanup_started = True
        owner.close_fd("source:graph.db", first)
        mark = syscalls.mark()
        with pytest.raises(merge_mod.MergeError, match="prerequisites"):
            owner.flock(source, merge_mod.fcntl.LOCK_UN)
        with pytest.raises(merge_mod.MergeError, match="still held"):
            owner.close_fd("source-lock")
        assert syscalls.since(mark) == [] and _flock_contended(lock)
        syscalls.close_fault[second] = "before"
        with pytest.raises(OSError, match="injected close"):
            owner.close_fd("source:graph.db", second)
        owner.flock(source, merge_mod.fcntl.LOCK_UN)
        owner.close_fd("source-lock")
        owner.close_fd("source:graph.db", second)
        assert syscalls.count("close", second) == 1 and syscalls.count("close", source) == 0
        assert syscalls.count("unlock", source) == 0 and _flock_contended(lock)
        syscalls.real_close(second)
        owner.fd_slots["source:graph.db#1"] = None
        _discard_owner(owner)
    elif scenario == "internal-cleanup":
        (tmp_path / "src.source.lifetime.lock").touch()
        marks = []

        def on_event(kind, number):
            marks.append((kind, number, owner._root_complete(), owner._others_closed(), owner.source_state))

        with merge_mod._publication_owner(root) as (owner, work):
            handle = owner.acquire_root_lock(root, work=work)
            fileno = handle.fileno()
            with merge_mod._publication_source_lock(source_dir, work=work) as source:
                consumer = owner.open_fd("source:graph.db", source_dir / "graph.db", os.O_RDONLY, work=work)
                other = owner.open_fd("intent-parent", tmp_path, flags, work=work)
                parent = owner.fd_slots["source-parent"]
                syscalls.on_event = on_event
        syscalls.on_event = None
        order = [(kind, number) for kind, number, *_ in marks]
        assert order == [("unlock", fileno), ("close", consumer), ("close", other), ("unlock", source),
                         ("close", source), ("close", parent)]
        by_number = {(kind, number): rest for kind, number, *rest in marks}
        assert by_number[("close", consumer)][0] and by_number[("close", other)][0]  # root 완료 뒤
        assert by_number[("unlock", source)][1]  # 원본 fd 전량 닫힌 뒤
        assert by_number[("close", source)][2] == "unlocked" and by_number[("close", parent)][2] == "closed"
        assert str(root) not in merge_mod._publication_owners
        # 내부 token도 연결 장벽을 우회하지 못한다. 연결이 남은 동안에는 어떤 해제도 일어나지 않는다.
        second = tmp_path / "fd-order-barrier"
        seen = {}

        class Probe:
            def close(self):
                token = owner.tokens["cleanup"]
                mark = syscalls.mark()
                owner.close_fd("source-lock", token=token)
                owner.release_root_lock(token=token)
                owner.flock(owner.fd_slots["source-lock"], merge_mod.fcntl.LOCK_UN, token=token)
                seen["events"] = syscalls.since(mark)
                raise RuntimeError("probe close failure")

        with pytest.raises(ValueError):
            with (merge_mod._publication_owner(second) as (owner, work),
                  merge_mod._publication_source_lock(source_dir, work=work)):
                handle = owner.acquire_root_lock(second, work=work)
                owner.register(Probe(), work=work)
                mark = syscalls.mark()
                raise ValueError("primary")
        assert seen["events"] == [] and syscalls.since(mark) == [] and not handle.closed
        assert owner.uncertain and owner.fd_slots["source-lock"] is not None
        assert _flock_contended(tmp_path / "src.source.lifetime.lock")
        _discard_owner(owner)
    else:
        for timing in ("before", "after"):
            owner = merge_mod._PublicationOwner(tmp_path / ("fd-order-uncertain-" + timing))
            fd = owner.open_fd("source:graph.db", tmp_path, flags)
            other = owner.open_fd("intent-parent", tmp_path, flags)
            _reset_syscalls()
            owner.cleanup_started = True
            syscalls.close_fault[fd] = timing
            with pytest.raises(OSError, match="injected close"):
                owner.close_fd("source:graph.db", fd)
            mark = syscalls.mark()
            owner.close_fd("source:graph.db", fd)
            owner.close_fd("intent-parent", other)
            assert syscalls.since(mark) == [] and syscalls.count("close", fd) == 1
            assert owner.uncertain and owner.fd_slots["source:graph.db"] == fd
            if timing == "before":
                syscalls.real_close(fd)
            syscalls.real_close(other)


# ---- raw root, source 정리 ----


def _fstat_fault(monkeypatch, holder, role, message, on_hit=None):
    """owner 슬롯 role이 가리키는 숫자에 대한 os.fstat 호출 하나를 실패시킨다."""
    real = os.fstat

    def fstat(target, *args, **kwargs):
        owner = holder.get("owner")
        if owner is not None and isinstance(target, int) and target == owner.fd_slots.get(role):
            if on_hit is not None:
                on_hit(target)
            raise OSError(message)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(os, "fstat", fstat)
    return real


def _track_owner(monkeypatch, holder):
    """새로 만든 owner를 holder에 기록한다. 컨텍스트 진입 중에도 시험 대역이 owner를 찾게 한다."""
    original_init = merge_mod._PublicationOwner.__init__

    def owner_init(owner, *args, **kwargs):
        original_init(owner, *args, **kwargs)
        holder["owner"] = owner

    monkeypatch.setattr(merge_mod._PublicationOwner, "__init__", owner_init)


def _spy_opens(monkeypatch, wanted):
    """os.open이 돌려준 숫자 중 경로가 wanted인 것을 기록한다."""
    real = os.open
    opened = []

    def spy(path, *args, **kwargs):
        fd = real(path, *args, **kwargs)
        if str(path) in wanted:
            opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", spy)
    return opened


def _spy_fdopen(monkeypatch):
    real = os.fdopen
    calls = []

    def spy(fd, mode, *args, **kwargs):
        calls.append((fd, mode))
        return real(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", spy)
    return calls


def _raw_root_failure(tmp_path, source_dir, monkeypatch, syscalls, kind, direct):
    name = "raw-root-" + kind + ("-direct" if direct else "")
    root = tmp_path / name
    lock_path = _lifetime_lock(tmp_path, root)
    real_lstat = os.lstat
    holder = {}
    _track_owner(monkeypatch, holder)

    def lstat(path, *args, **kwargs):
        if str(path) == str(lock_path):
            if kind == "root-lstat-before-fdopen":
                raise OSError("injected root lstat")
            if kind == "root-identity-before-fdopen":
                return real_lstat(tmp_path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    if kind == "root-fstat-before-fdopen":
        _fstat_fault(monkeypatch, holder, "root-lock", "injected root fstat")
    opened = _spy_opens(monkeypatch, {str(lock_path)})
    fdopen_calls = _spy_fdopen(monkeypatch)
    expected = merge_mod.MergeError if kind == "root-identity-before-fdopen" else OSError
    _reset_syscalls()
    if direct:
        owner, source_lock, parent, source, _ = _source_owner(tmp_path, name)
        with pytest.raises(expected):
            owner.acquire_root_lock(root)
        assert owner.root_raw_state == "held-pre-fdopen" and not owner.uncertain
        raw = opened[0]
        mark = syscalls.mark()
        owner.release_root_lock()
        assert syscalls.since(mark) == [("close", raw)]
        owner.release_root_lock()
        assert syscalls.count("close", raw) == 1
        assert _flock_contended(source_lock)  # 원본 SH는 root 정리가 풀지 않는다.
        _release_source(owner, source, parent)
        assert not _flock_contended(source_lock)
    else:
        source_lock = tmp_path / "src.source.lifetime.lock"
        source_lock.touch()
        with pytest.raises(expected):
            with (merge_mod._publication_owner(root) as (owner, work),
                  merge_mod._publication_source_lock(source_dir, work=work) as source):
                assert _flock_contended(source_lock)
                owner.acquire_root_lock(root, work=work)
        raw = opened[0]
        order = [event for event in syscalls.events if event[1] in (raw, source)]
        assert order == [("lock", source), ("close", raw), ("unlock", source), ("close", source)]
        assert not _flock_contended(source_lock) and str(root) not in merge_mod._publication_owners
    assert syscalls.count("close", raw) == 1 and syscalls.count("lock", raw) == 0
    assert syscalls.count("unlock", raw) == 0 and fdopen_calls == []
    assert owner.root_raw_state == "closed" and owner.fd_slots["root-lock"] is None
    assert owner.root_lock is None and owner.root_lock_state == "absent" and owner.root_registry_state == "absent"
    assert not _flock_contended(lock_path) and not owner.uncertain


@pytest.mark.parametrize("scenario", ["root-fstat-before-fdopen", "root-lstat-before-fdopen",
                                      "root-identity-before-fdopen", "root-raw-close-failure",
                                      "root-fdopen-unknown", "source-parent-early", "source-parent-after",
                                      "source-flock-unattempted", "source-flock-test-before",
                                      "source-flock-uncertain"])
def test_q1_raw_cleanup(source_dir, tmp_path, monkeypatch, syscalls, scenario):
    flags = os.O_RDONLY | os.O_DIRECTORY
    src_lock = tmp_path / "src.source.lifetime.lock"
    if scenario in {"root-fstat-before-fdopen", "root-lstat-before-fdopen", "root-identity-before-fdopen"}:
        _raw_root_failure(tmp_path, source_dir, monkeypatch, syscalls, scenario, direct=False)
        _raw_root_failure(tmp_path, source_dir, monkeypatch, syscalls, scenario, direct=True)
    elif scenario == "root-raw-close-failure":
        for timing in ("before", "after"):
            root = tmp_path / ("raw-close-" + timing)
            lock_path = _lifetime_lock(tmp_path, root)
            holder = {}
            _track_owner(monkeypatch, holder)
            src_lock.touch()

            def arm(fd, timing=timing):
                syscalls.close_fault[fd] = timing

            _fstat_fault(monkeypatch, holder, "root-lock", "injected root fstat", on_hit=arm)
            opened = _spy_opens(monkeypatch, {str(lock_path)})
            _reset_syscalls()
            with pytest.raises(OSError) as caught:
                with (merge_mod._publication_owner(root) as (owner, work),
                      merge_mod._publication_source_lock(source_dir, work=work)):
                    dependent = owner.open_fd("intent-parent", tmp_path, flags, work=work)
                    owner.acquire_root_lock(root, work=work)
            raw = opened[0]
            assert str(caught.value) == "injected root fstat"
            assert syscalls.count("close", raw) == 1 and syscalls.count("close", dependent) == 0
            assert owner.uncertain and owner.root_raw_state == "uncertain"
            assert merge_mod._publication_owners[str(root)] is owner
            assert [(stage, kind) for stage, _, _, kind in owner.error_log] == [("root-raw-close", "first")]
            assert owner.fd_slots["intent-parent"] == dependent and _flock_contended(src_lock)
            owner.release_root_lock()
            with pytest.raises(merge_mod.MergeError, match="already started"):
                owner.close_remaining()
            assert syscalls.count("close", raw) == 1 and syscalls.count("close", dependent) == 0
            assert _flock_contended(src_lock)
            if timing == "after":
                owner.fd_slots["root-lock"] = None
            _discard_owner(owner)
            holder["owner"] = None
    elif scenario == "root-fdopen-unknown":
        root = tmp_path / "raw-fdopen-unknown"
        lock_path = _lifetime_lock(tmp_path, root)
        src_lock.touch()
        opened = _spy_opens(monkeypatch, {str(lock_path)})
        real_fdopen = os.fdopen

        def fdopen(fd, mode, *args, **kwargs):
            if mode == "r+b":
                raise OSError("injected root fdopen")
            return real_fdopen(fd, mode, *args, **kwargs)

        monkeypatch.setattr(os, "fdopen", fdopen)
        with pytest.raises(OSError, match="injected root fdopen"):
            with (merge_mod._publication_owner(root) as (owner, work),
                  merge_mod._publication_source_lock(source_dir, work=work)):
                owner.acquire_root_lock(root, work=work)
        raw = opened[0]
        assert owner.uncertain and owner.root_lock_state == "uncertain" and owner.root_raw_state == "uncertain"
        assert syscalls.count("close", raw) == 0 and syscalls.count("unlock", raw) == 0
        assert owner.root_registry_state == "absent" and owner.root_lock is None
        assert merge_mod._publication_owners[str(root)] is owner and _flock_contended(src_lock)
        owner.release_root_lock()
        assert syscalls.count("close", raw) == 0 and syscalls.count("unlock", raw) == 0
        _discard_owner(owner)
    elif scenario == "source-parent-early":
        name = "raw-parent-early"
        root = tmp_path / name
        holder = {}
        owner, lock, parent, source, _ = _source_owner(tmp_path, name)
        holder["owner"] = owner
        _fstat_fault(monkeypatch, holder, "root-lock", "injected root fstat")
        with pytest.raises(OSError, match="injected root fstat"):
            owner.acquire_root_lock(root)
        owner.cleanup_started = True
        mark = syscalls.mark()
        with pytest.raises(merge_mod.MergeError, match="root release is incomplete"):
            owner.close_fd("source-parent")
        assert syscalls.since(mark) == [] and owner.fd_slots["source-parent"] == parent
        assert _flock_contended(lock) and owner.root_raw_state == "held-pre-fdopen"
        owner.release_root_lock()
        mark = syscalls.mark()
        with pytest.raises(merge_mod.MergeError, match="source lock is not closed"):
            owner.close_fd("source-parent")
        assert syscalls.since(mark) == [] and owner.fd_slots["source-parent"] == parent
        assert _flock_contended(lock) and owner.source_state == "locked"
        _release_source(owner, source, parent)
    elif scenario == "source-parent-after":
        owner, lock, parent, source, _ = _source_owner(tmp_path, "raw-parent-after")
        owner.cleanup_started = True
        mark = syscalls.mark()
        owner.flock(source, merge_mod.fcntl.LOCK_UN)
        assert not _flock_contended(lock)
        owner.close_fd("source-lock")
        owner.close_fd("source-parent")
        assert syscalls.since(mark) == [("unlock", source), ("close", source), ("close", parent)]
        assert owner.fd_slots["source-lock"] is None and owner.fd_slots["source-parent"] is None
        assert owner.source_state == "closed" and not owner.uncertain
    else:
        root = tmp_path / "raw-source-flock"
        src_lock.touch()
        opened = _spy_opens(monkeypatch, {src_lock.name})
        holder = {}
        _track_owner(monkeypatch, holder)
        if scenario == "source-flock-unattempted":
            _fstat_fault(monkeypatch, holder, "source-lock", "injected source fstat")
            expected = OSError
        else:
            original = syscalls.real_flock

            def faulty(fd, operation):
                if operation & merge_mod.fcntl.LOCK_SH:
                    if scenario == "source-flock-test-before":
                        raise merge_mod._PublicationFlockBeforeTest()
                    if holder["timing"] == "after":
                        original(fd, operation)
                    raise OSError("injected source flock")
                return original(fd, operation)

            syscalls.real_flock = faulty
            expected = (merge_mod._PublicationFlockBeforeTest if scenario == "source-flock-test-before"
                        else OSError)
        for timing in (("before", "after") if scenario == "source-flock-uncertain" else ("before",)):
            holder["timing"] = timing
            opened.clear()
            mark = syscalls.mark()
            with pytest.raises(expected):
                with (merge_mod._publication_owner(root) as (owner, work),
                      merge_mod._publication_source_lock(source_dir, work=work)):
                    pass
            events = syscalls.since(mark)
            kinds = [kind for kind, _ in events]
            if scenario == "source-flock-uncertain":
                assert owner.uncertain and owner.source_state == "uncertain"
                assert "unlock" not in kinds and "close" not in kinds
                assert merge_mod._publication_owners[str(root)] is owner
                assert _flock_contended(src_lock) is (timing == "after")
                _discard_owner(owner)
                holder["owner"] = None
            else:
                closes = [fd for kind, fd in events if kind == "close"]
                assert owner.source_state == "closed" and not owner.uncertain
                assert len(closes) == 2 and closes[0] == opened[0] and "unlock" not in kinds
                assert not _flock_contended(src_lock) and str(root) not in merge_mod._publication_owners
                assert owner.fd_slots["source-lock"] is None and owner.fd_slots["source-parent"] is None


# ---- main 전체 경로의 Q1 조합 ----


def _publication_case(source_dir, tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from opencrab.stores.backup import backup_data_dir

    with sqlite3.connect(source_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE billing_events (id INTEGER)")
        conn.execute("INSERT INTO billing_events VALUES (42)")
    backups = tmp_path / "backups"
    backups.mkdir()
    begin = datetime.now(UTC) - timedelta(seconds=2)
    backup = backup_data_dir(source_dir, backups, settings=SimpleNamespace(vector_db_file="vectors.db"))
    end = datetime.now(UTC) + timedelta(seconds=1)
    backup_root = backup.set_dir
    source_lock = backups / (backup_root.name + ".source.lifetime.lock")
    source_lock.touch()
    raw = (backup_root / "MANIFEST.json").read_bytes()
    evidence = {"version": 1, "snapshot_id": backup.manifest["set_id"],
                "source_manifest_sha256": hashlib.sha256(raw).hexdigest(), "preparer_id": "test",
                "frozen_from": begin.isoformat(), "frozen_until": end.isoformat(), "writer_stopped": True,
                "billing_same_snapshot": True, "packs_observation": "absent",
                "backup_at": backup.manifest["created_at"], "packs_checked_at": begin.isoformat(),
                "capture_from": begin.isoformat(), "capture_until": begin.isoformat()}
    evidence_path = tmp_path / "evidence-generated.json"
    evidence_path.write_text(json.dumps(evidence))
    root = tmp_path / "published-q1"
    args = ["--source-dir", str(backup_root), "--publication-root", str(root), "--evidence-file",
            str(evidence_path), "--apply", "--verify", "--immutable-source", "--min-free-gib", "0"]
    return args, root, source_lock


@pytest.mark.parametrize("scenario", ["read-close", "keyboard-close", "systemexit-close", "active-connection",
                                      "uncertain"])
def test_q1_main_combined(source_dir, tmp_path, monkeypatch, capsys, syscalls, scenario):
    args, root, source_lock = _publication_case(source_dir, tmp_path)
    owners = []
    original_init = merge_mod._PublicationOwner.__init__

    def owner_init(owner, *init_args, **kwargs):
        original_init(owner, *init_args, **kwargs)
        owners.append(owner)

    monkeypatch.setattr(merge_mod._PublicationOwner, "__init__", owner_init)
    seen = {}
    original_read = merge_mod._PublicationOwner.read_mounts

    def read_spy(owner, work):
        if scenario == "active-connection":
            seen["conn"] = owner.register(sqlite3.connect(":memory:"), work=work)
        if scenario == "uncertain":
            owner.uncertain = True
        try:
            return original_read(owner, work)
        except BaseException as exc:
            seen["raised"] = exc
            raise

    monkeypatch.setattr(merge_mod._PublicationOwner, "read_mounts", read_spy)
    real_fdopen = os.fdopen
    proxies = []
    primary = {"read-close": OSError("injected mounts read"), "keyboard-close": KeyboardInterrupt(),
               "systemexit-close": SystemExit(3)}.get(scenario)

    class MountsProxy:
        def __init__(self, inner):
            self.inner = inner
            self.closes = 0

        def read(self):
            raise primary

        def close(self):
            self.closes += 1
            self.inner.close()
            raise OSError("injected mounts close")

    def fdopen(fd, mode, *fd_args, **kwargs):
        stream = real_fdopen(fd, mode, *fd_args, **kwargs)
        if mode != "r":
            return stream
        proxies.append(MountsProxy(stream))
        return proxies[-1]

    monkeypatch.setattr(os, "fdopen", fdopen)
    if primary is None:
        assert merge_mod.main(args) == 2
    elif isinstance(primary, OSError):
        assert merge_mod.main(args) == 2
    else:
        with pytest.raises(type(primary)) as caught:
            merge_mod.main(args)
        assert caught.value is primary
    owner = owners[-1]
    err = capsys.readouterr().err
    # D0: intent, staging과 root는 만들어지지 않는다.
    assert not root.exists() and not root.with_name(root.name + ".intent.json").exists()
    assert not root.with_name(root.name + ".staging").exists()
    if primary is not None:
        assert len(proxies) == 1 and proxies[0].closes == 1
        close_error = owner.error_log[0][2]
        assert [(stage, kind) for stage, _, _, kind in owner.error_log[:1]] == [("stream-close", "first")]
        assert str(close_error) == "injected mounts close"
        assert seen["raised"] is primary and primary.__cause__ is close_error
        assert owner.uncertain and merge_mod._publication_owners[str(root)] is owner
        assert _flock_contended(source_lock) and owner.root_lock is None
        if isinstance(primary, OSError):
            assert "publication preflight failed: injected mounts read" in err
            assert "publication error cause: OSError" in err
        before = proxies[0].closes
        assert merge_mod.main(args) != 0
        assert proxies[0].closes == before == 1
        _discard_owner(owner)
        return
    assert proxies == [] and isinstance(seen["raised"], merge_mod.MergeError)
    assert "Q1 preconditions are not met" in str(seen["raised"]) and "rejects the call" in err
    assert owner.streams.get("mounts") is None and "mounts" not in owner.fd_slots
    if scenario == "active-connection":
        # 정상 연결은 거부 뒤 cleanup이 닫고 owner를 정리한다.
        assert not owner.uncertain and owner.connections == []
        assert str(root) not in merge_mod._publication_owners and not _flock_contended(source_lock)
    else:
        assert owner.uncertain and merge_mod._publication_owners[str(root)] is owner
        assert _flock_contended(source_lock)
        _discard_owner(owner)


def test_q1_direct_pending(tmp_path, syscalls):
    """종료가 pending이면 직접 root 해제, 원본 unlock, fd close는 부작용 없이 장벽에서 멈춘다."""
    from opencrab import locking

    root = tmp_path / "q1-direct-pending"
    owner = merge_mod._PublicationOwner(root)
    work = merge_mod._PublicationWork(owner)
    with owner.state_lock:
        owner.work, owner.work_active, work.active = work, True, True
    handle = owner.acquire_root_lock(root, work=work)
    fileno = handle.fileno()
    root_lock = _lifetime_lock(tmp_path, root)
    source_lock = tmp_path / "src.source.lifetime.lock"
    source_lock.touch()
    parent = owner.open_fd("source-parent", tmp_path, os.O_RDONLY | os.O_DIRECTORY, work=work)
    source = owner.open_fd("source-lock", source_lock.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=parent, work=work)
    owner.flock(source, merge_mod.fcntl.LOCK_SH | merge_mod.fcntl.LOCK_NB, work=work)
    probe = owner.open_fd("probe", tmp_path, os.O_RDONLY | os.O_DIRECTORY, work=work, category="independent")
    # q1 token이 살아 있는 동안 실제 body 종료 전이를 부르면 정리 권한을 얻지 못하고 pending이 된다.
    with owner.state_lock:
        q1 = owner._new_token_locked("q1")
    assert owner._finish_work() is None
    owner._end(q1)
    assert owner.pending and not owner.cleanup_started and not owner.work_active and not owner.tokens
    assert owner.inflight == 0 and owner.connections == [] and not owner.uncertain
    mark = syscalls.mark()
    owner.release_root_lock()
    owner.flock(source, merge_mod.fcntl.LOCK_UN)
    assert syscalls.since(mark) == []
    with pytest.raises(merge_mod.MergeError, match="termination is pending"):
        owner.close_fd("probe", probe)
    assert syscalls.since(mark) == []
    assert not handle.closed and owner.root_lock is handle and owner.root_lock_state == "locked"
    assert owner.root_registry_state == "registered" and handle in locking._lifetime_handles
    assert owner.source_state == "locked" and owner.source_locked and not owner.tokens
    assert owner.fd_slots["probe"] == probe and owner.fd_slots["source-lock"] == source
    assert _flock_contended(root_lock) and _flock_contended(source_lock)
    assert syscalls.count("unlock", fileno) == 0
    _discard_owner(owner)


# ---- 게시 ready 경로 (#671 P1) ----


def _ready_setup(source_dir, tmp_path):
    """합성 백업 세트와 증거를 만들고 main 인자와 경로를 돌려준다. 기존 prepares_before_staging와 같은 입력이다."""
    from types import SimpleNamespace

    with sqlite3.connect(source_dir / "billing.db") as conn:
        conn.execute("CREATE TABLE billing_events (id INTEGER)")
    names = [*merge_mod.FILES, "billing.db"]
    entries = [{"label": name, "path": name, "status": "verified", "capture": "copied_under_lock"}
               for name in names]
    entries += [{"label": name, "status": "skipped"}
                for name in ("chroma", "docs", "delete-pack-journals", "graph.kuzu", "packs")]
    metadata = {name: {"size": (source_dir / name).stat().st_size,
                       "sha256": hashlib.sha256((source_dir / name).read_bytes()).hexdigest()}
                for name in names}
    raw = json.dumps({"format": 1, "entries": entries, "files": metadata, "set_id": "set1",
                      "created_at": "2026-01-01T00:00:03Z"}).encode()
    (source_dir / "MANIFEST.json").write_bytes(raw)
    (tmp_path / "src.source.lifetime.lock").touch()
    evidence = {"version": 1, "snapshot_id": "set1", "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "preparer_id": "test", "frozen_from": "2026-01-01T00:00:00Z",
                "frozen_until": "2026-01-01T00:00:02Z", "writer_stopped": True,
                "billing_same_snapshot": True, "packs_observation": "absent",
                "backup_at": "2026-01-01T00:00:03Z", "packs_checked_at": "2026-01-01T00:00:01Z",
                "capture_from": "2026-01-01T00:00:00Z", "capture_until": "2026-01-01T00:00:02Z"}
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence))
    root = tmp_path / "published"
    args = ["--source-dir", str(source_dir), "--publication-root", str(root), "--evidence-file",
            str(evidence_path), "--apply", "--verify", "--immutable-source", "--min-free-gib", "0"]
    return SimpleNamespace(
        args=args, root=root, src=source_dir, staging=tmp_path / "published.staging",
        intent=tmp_path / "published.intent.json", intent_tmp=tmp_path / "published.intent.json.tmp",
        lock_src=tmp_path / "src.source.lifetime.lock", lock_root=tmp_path / "published.lifetime.lock")


def _source_tree(path):
    """원본 트리의 (경로, 크기, sha256, mtime_ns, ctime_ns). 원본 불변 단언에 쓴다."""
    tree = {}
    for item in sorted(path.rglob("*")):
        info = item.lstat()
        digest = hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None
        tree[str(item.relative_to(path))] = (info.st_size, digest, info.st_mtime_ns, info.st_ctime_ns)
    return tree


def _source_parent_names(source_dir):
    """원본 상위 디렉터리의 항목 이름(.source.lifetime.lock 포함). 게시 산출물(published로 시작)과
    시험 변형이 만드는 outside-link는 뺀다. 시험 배치에서는 원본 상위와 게시 상위가 같은 디렉터리다."""
    return sorted(item.name for item in source_dir.parent.iterdir()
                  if not item.name.startswith("published") and item.name != "outside-link")


def _ready_disk(ctx):
    """root, staging, intent, tmp, root ledger의 디스크 상태. 파일이 없으면 None이다."""
    def state_of(path):
        try:
            return json.loads(path.read_text())["state"]
        except FileNotFoundError:
            return None
        except (ValueError, KeyError):
            return "partial"  # 쓰기가 실패해 O_EXCL로 만든 파일이 부분 상태로 남은 경우다

    return {"root": ctx.root.exists(), "stg": ctx.staging.exists(), "intent": state_of(ctx.intent),
            "intent_tmp": ctx.intent_tmp.exists(), "ledger_tmp": (ctx.root / "ledger.json.tmp").exists(),
            "ledger": state_of(ctx.root / "ledger.json") if ctx.root.exists()
            else state_of(ctx.staging / "ledger.json")}


class _ConnProxy:
    """sqlite 연결 대역. close 실패나 quick_check 결과를 바꾼다. 나머지는 원 연결로 넘긴다."""

    def __init__(self, inner, *, fail_close=False, bad_quick_check=False):
        self.inner = inner
        self.fail_close = fail_close
        self.bad_quick_check = bad_quick_check

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self, *args, **kwargs):
        if self.fail_close:
            raise sqlite3.OperationalError("injected close failure")
        return self.inner.close()

    def execute(self, sql, *args):
        if self.bad_quick_check and sql.strip().upper().startswith("PRAGMA QUICK_CHECK"):
            return sqlite3.connect(":memory:").execute("SELECT 'bad'")
        return self.inner.execute(sql, *args)


class _ReadyHarness:
    """한 시험의 owner 수집과 주입 설치를 맡는다."""

    def __init__(self, monkeypatch, ctx):
        self.monkeypatch = monkeypatch
        self.ctx = ctx
        self.owners = []
        self.proxies = []
        original_init = merge_mod._PublicationOwner.__init__
        owners = self.owners

        def owner_init(owner, *init_args, **kwargs):
            original_init(owner, *init_args, **kwargs)
            owners.append(owner)

        monkeypatch.setattr(merge_mod._PublicationOwner, "__init__", owner_init)
        # 역할별 순번의 fd를 열린 시점에 기록한다. 닫기 주입은 정리 단계에서 슬롯이 비워진 뒤에도 fd를 찾아야 한다.
        self.fds = {}
        self.by_fd = {}
        self.events = []
        original_open_fd = merge_mod._PublicationOwner.open_fd
        harness = self

        def recording_open_fd(owner, role, *args, **kwargs):
            index = len(owner.fd_roles.get(role, []))
            harness.events.append(("open", role))
            fd = original_open_fd(owner, role, *args, **kwargs)
            harness.fds[(role, index)] = fd
            harness.by_fd[fd] = role
            return fd

        monkeypatch.setattr(merge_mod._PublicationOwner, "open_fd", recording_open_fd)
        self.r8_calls = 0
        self.r8_before = []
        self.r8_after = []
        self.r8_installed = False

    @staticmethod
    def current_owner():
        return getattr(merge_mod._publication_current, "owner", None)

    def slot_fd(self, role, n):
        return self.fds.get((role, n))

    def install_r8(self):
        """5절 규칙. 원본 이름이 staging 이름인 R8 호출에서만 동작하고 R0a 프로브 호출은 그대로 넘긴다."""
        if self.r8_installed:
            return
        self.r8_installed = True
        real = merge_mod._rename_noreplace
        staging_name = self.ctx.staging.name
        harness = self

        def wrapped(src_fd, src, dst_fd, dst):
            if src != staging_name:
                return real(src_fd, src, dst_fd, dst)
            harness.r8_calls += 1
            for hook in harness.r8_before:
                hook()
            real(src_fd, src, dst_fd, dst)
            for hook in harness.r8_after:
                hook()

        self.monkeypatch.setattr(merge_mod, "_rename_noreplace", wrapped)

    def inject_rename(self, name, n, *, after=False):
        """os.rename이 대상 마지막 이름이 name인 n번째 호출(0부터)에서 실패한다."""
        real = os.rename
        counter = {"n": 0}

        def wrapped(src, dst, *args, **kwargs):
            if Path(dst).name == name:
                index = counter["n"]
                counter["n"] += 1
                if index == n:
                    if after:
                        real(src, dst, *args, **kwargs)
                    raise OSError(errno.EIO, "injected rename failure")
            return real(src, dst, *args, **kwargs)

        self.monkeypatch.setattr(os, "rename", wrapped)

    def inject_open(self, role, n=0):
        """role의 n번째 open_fd에서 os.open이 실패한다(open_fd가 uncertain을 세운다)."""
        original = merge_mod._PublicationOwner.open_fd
        real_open = os.open
        monkeypatch = self.monkeypatch

        def wrapped(owner, name, *args, **kwargs):
            if name == role and len(owner.fd_roles.get(role, [])) == n:
                def boom(*a, **k):
                    raise OSError(errno.EIO, "injected open failure")

                monkeypatch.setattr(os, "open", boom)
                try:
                    return original(owner, name, *args, **kwargs)
                finally:
                    monkeypatch.setattr(os, "open", real_open)
            return original(owner, name, *args, **kwargs)

        monkeypatch.setattr(merge_mod._PublicationOwner, "open_fd", wrapped)

    def inject_fd_call(self, function, role, n=0, *, after=False, then=None):
        """os.<function>(fd, ...)이 role의 n번째 fd에서 실패한다. after이면 실제 호출 뒤에 실패한다.

        then이 있으면 실패 대신 실제 호출 뒤에 then()을 한 번 부르고 결과를 돌려준다. close 주입은 한 번만 동작한다.
        """
        original = getattr(os, function)
        harness = self
        done = []

        def wrapped(fd, *args, **kwargs):
            target = harness.slot_fd(role, n)
            if target is not None and fd == target and not (done and (then is not None or function == "close")):
                done.append(True)
                if then is not None:
                    result = original(fd, *args, **kwargs)
                    then()
                    return result
                if after:
                    original(fd, *args, **kwargs)
                raise OSError(errno.EIO, f"injected {function} failure")
            return original(fd, *args, **kwargs)

        self.monkeypatch.setattr(os, function, wrapped)

    def inject_connect(self, marker, *, fail_open=False, fail_close=False, bad_quick_check=False):
        """URI에 marker가 든 sqlite 연결 열기나 닫기가 실패한다."""
        real_connect = sqlite3.connect
        harness = self

        def wrapped(database, *args, **kwargs):
            if marker in str(database):
                if fail_open:
                    raise sqlite3.OperationalError("injected connect failure")
                proxy = _ConnProxy(real_connect(database, *args, **kwargs), fail_close=fail_close,
                                   bad_quick_check=bad_quick_check)
                harness.proxies.append(proxy)
                return proxy
            return real_connect(database, *args, **kwargs)

        self.monkeypatch.setattr(sqlite3, "connect", wrapped)

    def inject_probe(self, mode):
        """R0a 프로브만 실패시킨다. 5절 규칙: 원본 이름이 .rp-a인 호출에서만 동작한다."""
        real_rename = merge_mod._rename_noreplace
        monkeypatch = self.monkeypatch
        if mode in ("rename-unsupported", "probe-unexpected-errno"):
            code = errno.ENOSYS if mode == "rename-unsupported" else errno.EACCES

            def wrapped(src_fd, src, dst_fd, dst):
                if src == ".rp-a":
                    raise OSError(code, "injected probe failure")
                return real_rename(src_fd, src, dst_fd, dst)

            monkeypatch.setattr(merge_mod, "_rename_noreplace", wrapped)
        elif mode == "probe-mkdir":
            real = os.mkdir

            def wrapped_mkdir(path, *args, **kwargs):
                if str(path) == ".rp-a":
                    raise OSError(errno.EIO, "injected probe mkdir failure")
                return real(path, *args, **kwargs)

            monkeypatch.setattr(os, "mkdir", wrapped_mkdir)
        else:
            real = os.rmdir

            def wrapped_rmdir(path, *args, **kwargs):
                if str(path) == ".rp-b":
                    raise OSError(errno.EIO, "injected probe rmdir failure")
                return real(path, *args, **kwargs)

            monkeypatch.setattr(os, "rmdir", wrapped_rmdir)

    def inject_hook_h(self, action):
        """주입 방법 H. staging billing.db가 처음 존재한 뒤 첫 check_inputs() 호출(staging 마지막 호출) 뒤에 action을 한 번 실행한다."""
        original = merge_mod._publication_inputs
        staging = self.ctx.staging

        @contextlib.contextmanager
        def wrapped(*args, **kwargs):
            with original(*args, **kwargs) as (fds, check):
                done = []

                def hooked():
                    check()
                    if not done and (staging / "billing.db").exists():
                        done.append(True)
                        action()

                yield fds, hooked

        self.monkeypatch.setattr(merge_mod, "_publication_inputs", wrapped)

    def discard(self):
        for proxy in self.proxies:
            try:
                proxy.inner.close()
            except Exception:
                pass
        for owner in self.owners:
            if str(owner.root) in {str(key) for key in merge_mod._publication_owners}:
                _discard_owner(owner)


def _install_ready_failure(harness, failure):
    """6절 failure id의 주입을 설치한다."""
    ctx = harness.ctx
    if failure in ("rename-unsupported", "probe-mkdir", "probe-rmdir", "probe-unexpected-errno"):
        harness.inject_probe(failure)
    elif failure == "uncertain-guard":
        harness.inject_hook_h(lambda: setattr(harness.current_owner(), "uncertain", True))
    elif failure == "identity-mismatch":
        def swap():
            copy = ctx.staging / "billing.copy"
            shutil.copyfile(ctx.staging / "billing.db", copy)
            os.replace(copy, ctx.staging / "billing.db")

        harness.inject_hook_h(swap)
    elif failure == "probe-dir-open":
        harness.inject_open("probe-dir")
    elif failure == "lock-create":
        harness.inject_open("lock-create")
    elif failure == "tables-connect":
        harness.inject_connect("published.staging/single.db?mode=ro&immutable=1", fail_open=True)
    elif failure == "tables-connect-close":
        # 앞선 staging 연결이 같은 경로를 쓰므로 R3의 immutable 연결만 고른다.
        harness.inject_connect("published.staging/single.db?mode=ro&immutable=1", fail_close=True)
    elif failure.endswith("-open") and failure.rsplit("-open", 1)[0] in ("manifest", "marker", "ledger", "staging-seal"):
        stem = failure.rsplit("-open", 1)[0]
        harness.inject_open(stem if stem == "staging-seal" else stem + "-write")
    elif failure.endswith(("-write", "-fsync")) and failure.split("-")[0] in ("manifest", "marker", "ledger"):
        stem, call = failure.rsplit("-", 1)
        harness.inject_fd_call(call, stem + "-write")
    elif failure == "staging-seal-fsync":
        harness.inject_fd_call("fsync", "staging-seal")
    else:
        raise AssertionError(f"unknown failure id {failure}")


# (rc, uncertain, 잠금 보유, 메시지 접두어, 디스크 상태). 6절 표를 그대로 옮긴 것이다.
_STAGING_ROW = {"root": False, "stg": True, "intent": "preparing"}
_READY_ROWS = {
    "rename-unsupported": (1, False, False, "publication staging failed: publication rename is unsupported", _STAGING_ROW),
    "probe-mkdir": (1, False, False, "publication staging failed: publication rename probe failed", _STAGING_ROW),
    "probe-rmdir": (1, False, False, "publication staging failed: publication rename probe failed", _STAGING_ROW),
    "probe-unexpected-errno": (1, False, False, "publication staging failed: publication rename probe failed", _STAGING_ROW),
    "probe-dir-open": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "uncertain-guard": (1, True, True, "publication staging failed: publication owner is uncertain", _STAGING_ROW),
    "identity-mismatch": (1, False, False, "publication staging failed: publication staging identity differs", _STAGING_ROW),
    "lock-create": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "tables-connect": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "tables-connect-close": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "manifest-open": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "manifest-write": (1, False, False, "publication staging failed:", _STAGING_ROW),
    "manifest-fsync": (1, False, False, "publication staging failed:", _STAGING_ROW),
    "marker-open": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "marker-write": (1, False, False, "publication staging failed:", _STAGING_ROW),
    "marker-fsync": (1, False, False, "publication staging failed:", _STAGING_ROW),
    "ledger-open": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "ledger-write": (1, False, False, "publication staging failed:", _STAGING_ROW),
    "ledger-fsync": (1, False, False, "publication staging failed:", _STAGING_ROW),
    "staging-seal-open": (1, True, True, "publication staging failed:", _STAGING_ROW),
    "staging-seal-fsync": (1, False, False, "publication staging failed:", _STAGING_ROW),
}
_R0A_IDS = ["rename-unsupported", "probe-mkdir", "probe-rmdir", "probe-unexpected-errno", "probe-dir-open"]


def _run_ready_failure(source_dir, tmp_path, monkeypatch, capsys, failure):
    rc, uncertain, held, message, disk = _READY_ROWS[failure]
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    tree = _source_tree(source_dir)
    parent_names = _source_parent_names(source_dir)
    lock_before = ctx.lock_src.stat()
    _install_ready_failure(harness, failure)
    try:
        assert merge_mod.main(ctx.args) == rc, capsys.readouterr().err
        err = capsys.readouterr().err
        assert message in err
        assert "publication ready" not in err
        owner = harness.owners[-1]
        assert owner.uncertain is uncertain
        assert _flock_contended(ctx.lock_src) is held
        assert _flock_contended(ctx.lock_root) is held
        actual = _ready_disk(ctx)
        assert {key: actual[key] for key in disk} == disk
        assert _source_tree(source_dir) == tree
        assert _source_parent_names(source_dir) == parent_names
        assert ctx.lock_src.stat().st_mtime_ns == lock_before.st_mtime_ns
        assert not any(source_dir.glob("*-wal")) and not any(source_dir.glob("*-shm"))
        if held:
            assert str(owner.root) in {str(key) for key in merge_mod._publication_owners}
        else:
            assert all(fd is None for fd in owner.fd_slots.values()) and not owner.connections
    finally:
        harness.discard()


@pytest.mark.parametrize("failure", _R0A_IDS)
def test_publication_r0a_probe_classification(source_dir, tmp_path, monkeypatch, capsys, failure):
    _run_ready_failure(source_dir, tmp_path, monkeypatch, capsys, failure)


@pytest.mark.parametrize("failure", list(_READY_ROWS))
def test_publication_ready_failure_states(source_dir, tmp_path, monkeypatch, capsys, failure):
    _run_ready_failure(source_dir, tmp_path, monkeypatch, capsys, failure)


def test_publication_ready_skips_when_uncertain(source_dir, tmp_path, monkeypatch, capsys):
    """RG. owner가 uncertain이면 R1 이후 쓰기 호출이 0건이다. 세는 기준은 RG 지점 이후다."""
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    counts = {"armed": False, "write": 0, "create": 0, "rename": 0}
    real_write, real_open, real_rename = os.write, os.open, merge_mod._rename_noreplace

    def write(*args, **kwargs):
        counts["write"] += counts["armed"]
        return real_write(*args, **kwargs)

    def open_spy(path, flags, *args, **kwargs):
        counts["create"] += bool(counts["armed"] and flags & os.O_CREAT)
        return real_open(path, flags, *args, **kwargs)

    def rename(*args):
        counts["rename"] += counts["armed"]
        return real_rename(*args)

    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "open", open_spy)
    monkeypatch.setattr(merge_mod, "_rename_noreplace", rename)

    def hook():
        counts["armed"] = True
        harness.current_owner().uncertain = True

    harness.inject_hook_h(hook)
    try:
        assert merge_mod.main(ctx.args) == 1
        assert "publication owner is uncertain" in capsys.readouterr().err
        assert counts["armed"] and counts["write"] == 0 and counts["create"] == 0 and counts["rename"] == 0
        assert not (ctx.staging / "manifest.json").exists()
        assert not (ctx.staging / "single.db.lifetime.lock").exists()
    finally:
        harness.discard()


def test_publication_rename_noreplace_supported_on_tmp_path(tmp_path):
    """이 파일시스템이 NOREPLACE를 지원하는지 확인한다. 미지원이면 skip이 아니라 실패다."""
    (tmp_path / "a").mkdir()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        merge_mod._rename_noreplace(fd, "a", fd, "b")
        assert not (tmp_path / "a").exists() and (tmp_path / "b").is_dir()
        (tmp_path / "c").mkdir()
        with pytest.raises(OSError) as existing:
            merge_mod._rename_noreplace(fd, "b", fd, "c")
        assert existing.value.errno == errno.EEXIST and (tmp_path / "b").is_dir()
        with pytest.raises(OSError) as missing:
            merge_mod._rename_noreplace(fd, "missing", fd, "d")
        assert missing.value.errno == errno.ENOENT
    finally:
        os.close(fd)


def test_publication_main_refuses_intent_tmp(source_dir, tmp_path, capsys):
    ctx = _ready_setup(source_dir, tmp_path)
    ctx.intent_tmp.write_bytes(b"leftover")
    assert merge_mod.main(ctx.args) == 2
    assert "publication preflight failed: publication artifacts already exist" in capsys.readouterr().err
    assert ctx.intent_tmp.read_bytes() == b"leftover"
    assert not ctx.root.exists() and not ctx.staging.exists() and not ctx.intent.exists()


def _immutable_conn(path):
    """게시된 root를 건드리지 않는 읽기 전용 immutable 연결(vec0 확장 포함)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _assert_ready_published(root):
    """게시가 ready까지 끝난 디스크 상태: root 6개 파일, staging 없음, intent와 root ledger가 ready."""
    intent = root.with_name(root.name + ".intent.json")
    staging = root.with_name(root.name + ".staging")
    assert root.is_dir() and not staging.exists()
    assert sorted(item.name for item in root.iterdir()) == [
        "billing.db", "ledger.json", "manifest.json", "marker.json", "single.db", "single.db.lifetime.lock"]
    assert json.loads(intent.read_text())["state"] == "ready"
    assert json.loads((root / "ledger.json").read_text())["state"] == "ready"
    assert not root.with_name(root.name + ".intent.json.tmp").exists()


def _assert_ready_cleanup_failed(err):
    assert "publication ready; cleanup failed" in err
    assert "publication preflight failed" not in err and "post-ready source check failed" not in err


# ---- 게시 ready 경로: R7~R11 (#671 P1 단계 3) ----

_R9_BASE = {"root": True, "stg": False, "intent": "publishing", "intent_tmp": False, "ledger": "staged"}
_PUBLISH = "publication publish failed:"


def _pub(rc, unc, held, prefix, **disk):
    return (rc, unc, held, prefix, disk)


def _r7(**disk):
    return {"root": False, "stg": True, "ledger_tmp": False, **disk}


_PUB_ROWS = {
    "intent-tmp-create": _pub(1, True, True, _PUBLISH, **_r7(intent="preparing", intent_tmp=False)),
    "intent-tmp-write": _pub(1, False, False, _PUBLISH, **_r7(intent="preparing", intent_tmp=True)),
    "intent-tmp-fsync": _pub(1, False, False, _PUBLISH, **_r7(intent="preparing", intent_tmp=True)),
    "intent-rename-before": _pub(1, False, False, _PUBLISH, **_r7(intent="preparing", intent_tmp=True)),
    "intent-rename-after": _pub(1, False, False, _PUBLISH, **_r7(intent="publishing", intent_tmp=False)),
    "intent-parent-fsync": _pub(1, False, False, _PUBLISH, **_r7(intent="publishing", intent_tmp=False)),
    "intent-parent-open": _pub(1, True, True, _PUBLISH, **_r7(intent="publishing", intent_tmp=False)),
    "publish-parent-open": _pub(1, True, True, _PUBLISH, **_r7(intent="publishing", intent_tmp=False)),
    "rename-enosys-einval": _pub(1, False, False, _PUBLISH + " publication rename is unsupported",
                                 **_r7(intent="publishing", intent_tmp=False)),
    "rename-exdev": _pub(1, False, False, _PUBLISH, **_r7(intent="publishing", intent_tmp=False)),
    "rename-error-effect-absent": _pub(1, False, False, _PUBLISH,
                                       **_r7(intent="publishing", intent_tmp=False)),
    "rename-eexist": _pub(1, False, False, _PUBLISH, root=True, stg=True, intent="publishing", intent_tmp=False),
    "rename-error-effect-present": _pub(1, False, False, _PUBLISH, root=True, stg=False, intent="publishing",
                                        intent_tmp=False, ledger="staged"),
    "rename-error-both-absent": _pub(1, True, True, _PUBLISH, root=False, stg=False, intent="publishing",
                                     intent_tmp=False),
    "rename-error-observe-fails": _pub(1, True, True, _PUBLISH, intent="publishing", intent_tmp=False),
    "publish-parent-fsync-before": _pub(1, False, False, _PUBLISH, **_R9_BASE),
    "publish-parent-fsync-after": _pub(1, False, False, _PUBLISH, **_R9_BASE),
    "root-dir-open": _pub(1, True, True, _PUBLISH, **_R9_BASE),
    "root-file-open": _pub(1, True, True, _PUBLISH, **_R9_BASE),
    "verify-fileset": _pub(1, False, False, _PUBLISH + " publication root file set differs", **_R9_BASE),
    "verify-fileform": _pub(1, False, False, _PUBLISH + " publication root file form differs", **_R9_BASE),
    "verify-binding": _pub(1, False, False, _PUBLISH + " single SQLite generation binding differs", **_R9_BASE),
    "verify-metadata-rows": _pub(1, False, False, _PUBLISH + " publication metadata row count differs",
                                 **_R9_BASE),
    "verify-vec-load": _pub(1, False, False, _PUBLISH + " sqlite_vec is not importable", **_R9_BASE),
    "verify-quick-check": _pub(1, False, False, _PUBLISH + " publication quick_check differs", **_R9_BASE),
    "verify-tables": _pub(1, False, False, _PUBLISH + " publication table counts differ", **_R9_BASE),
    "verify-billing": _pub(1, False, False, _PUBLISH + " publication billing digest differs", **_R9_BASE),
    "verify-identity": _pub(1, False, False, _PUBLISH + " publication published identity differs", **_R9_BASE),
    "verify-sidecar": _pub(1, False, False, _PUBLISH + " publication database sidecar remains", **_R9_BASE),
    "verify-source-changed": _pub(2, False, False, _PUBLISH + " source file changed during use", **_R9_BASE),
    "verify-connect": _pub(1, True, True, _PUBLISH, **_R9_BASE),
    "verify-connect-close": _pub(1, True, True, _PUBLISH, **_R9_BASE),
    "ledger-tmp-create": _pub(1, True, True, _PUBLISH, **{**_R9_BASE, "ledger_tmp": False}),
    "ledger-tmp-write": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger_tmp": True}),
    "ledger-tmp-fsync": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger_tmp": True}),
    "ledger-rename-before": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger_tmp": True}),
    "ledger-rename-after": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger": "ready", "ledger_tmp": False}),
    "root-dir-sync-open": _pub(1, True, True, _PUBLISH, **{**_R9_BASE, "ledger": "ready"}),
    "root-dir-fsync": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger": "ready"}),
    "readback-open": _pub(1, True, True, _PUBLISH, **{**_R9_BASE, "ledger": "ready"}),
    "readback-mismatch": _pub(1, False, False, _PUBLISH + " single SQLite generation binding differs",
                              **{**_R9_BASE, "ledger": "ready"}),
    "intent-ready-tmp-create": _pub(1, True, True, _PUBLISH, **{**_R9_BASE, "ledger": "ready"}),
    "intent-ready-tmp-write": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger": "ready", "intent_tmp": True}),
    "intent-ready-tmp-fsync": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger": "ready", "intent_tmp": True}),
    "intent-ready-rename-before": _pub(1, False, False, _PUBLISH,
                                       **{**_R9_BASE, "ledger": "ready", "intent_tmp": True}),
    "intent-ready-rename-after": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger": "ready", "intent": "ready"}),
    "intent-ready-parent-fsync": _pub(1, False, False, _PUBLISH, **{**_R9_BASE, "ledger": "ready", "intent": "ready"}),
    "intent-ready-parent-open": _pub(1, True, True, _PUBLISH, **{**_R9_BASE, "ledger": "ready", "intent": "ready"}),
}

_CLOSE_ROLES = ["probe-dir", "manifest-write", "marker-write", "ledger-write", "lock-create", "staging-seal",
                "intent-tmp", "ledger-tmp", "publish-parent", "root-dir", "root-file", "intent-parent"]
_POST_READY_IDS = [f"{role}-close" for role in _CLOSE_ROLES] + [
    "inputs-exit-check", "source-lock-exit-identity", "post-ready-source-and-cleanup",
    "step-failure-then-cleanup-failure"]
# 6.3의 기존 시험 행(release 10건, checkpoint-close 4건). 새 시험에는 id 집합 단언에만 포함한다.
_EXISTING_POST_READY_IDS = [
    "release:source-unlock-before", "release:source-unlock-after", "release:source-close-before",
    "release:source-close-after", "release:root-unlock-before", "release:root-unlock-after",
    "release:root-close-before", "release:root-close-after", "release:registry-before",
    "release:registry-after", "checkpoint-close:0:before", "checkpoint-close:0:after",
    "checkpoint-close:1:before", "checkpoint-close:1:after"]
# 6절 표의 failure id 전량. 표를 옮겨 적은 목록이며 시험 인자 집합과 일치해야 한다.
_DESIGN_FAILURE_IDS = {
    "rename-unsupported", "probe-mkdir", "probe-rmdir", "probe-unexpected-errno", "probe-dir-open",
    "uncertain-guard", "identity-mismatch", "lock-create", "tables-connect", "tables-connect-close",
    "manifest-open", "manifest-write", "manifest-fsync", "marker-open", "marker-write", "marker-fsync",
    "ledger-open", "ledger-write", "ledger-fsync", "staging-seal-open", "staging-seal-fsync",
    "intent-tmp-create", "intent-tmp-write", "intent-tmp-fsync", "intent-rename-before",
    "intent-rename-after", "intent-parent-fsync", "intent-parent-open", "publish-parent-open",
    "rename-enosys-einval", "rename-exdev", "rename-error-effect-absent", "rename-eexist",
    "rename-error-effect-present", "rename-error-both-absent", "rename-error-observe-fails",
    "publish-parent-fsync-before", "publish-parent-fsync-after", "root-dir-open", "root-file-open",
    "verify-fileset", "verify-fileform", "verify-binding", "verify-metadata-rows", "verify-vec-load",
    "verify-quick-check", "verify-tables", "verify-billing", "verify-identity", "verify-sidecar",
    "verify-source-changed", "verify-connect", "verify-connect-close", "ledger-tmp-create",
    "ledger-tmp-write", "ledger-tmp-fsync", "ledger-rename-before", "ledger-rename-after",
    "root-dir-sync-open", "root-dir-fsync", "readback-open", "readback-mismatch",
    "intent-ready-tmp-create", "intent-ready-tmp-write", "intent-ready-tmp-fsync",
    "intent-ready-rename-before", "intent-ready-rename-after", "intent-ready-parent-fsync",
    "intent-ready-parent-open",
    "probe-dir-close", "manifest-write-close", "marker-write-close", "ledger-write-close", "lock-create-close",
    "staging-seal-close", "intent-tmp-close", "ledger-tmp-close", "publish-parent-close", "root-dir-close",
    "root-file-close", "intent-parent-close", "inputs-exit-check", "source-lock-exit-identity",
    "post-ready-source-and-cleanup", "step-failure-then-cleanup-failure",
    *_EXISTING_POST_READY_IDS}


_SECOND_METADATA_ROW = ("INSERT INTO single_file_metadata (singleton, root, database_path, db_uuid, generation) "
                        "VALUES (2, 'a', 'b', 'c', 'd')")


def _variant_hook(ctx, variant, monkeypatch=None):
    """R8 rename 직후에 root를 변형하는 훅. R9 거부 시험과 판정 일치 시험이 같은 변형을 쓴다."""
    root, src = ctx.root, ctx.src
    outside = ctx.root.parent / "outside-link"

    def rewrite_db(statement):
        conn = sqlite3.connect(root / "single.db", isolation_level=None)
        try:
            # singleton 열의 CHECK 제약이 둘째 행을 막으므로 삽입 변형은 제약 검사를 끄고 쓴다.
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(statement)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def fileset():
        (root / "extra.txt").write_text("x")

    def hardlink():
        os.link(root / "single.db", outside)

    def binding():
        path = root / "ledger.json"
        data = json.loads(path.read_text())
        data["generation"] = "0" * 32 if data["generation"] != "0" * 32 else "1" * 32
        path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))

    def billing():
        with open(root / "billing.db", "ab") as handle:
            handle.write(b"\0")

    def identity():
        copy = outside.with_name("copy.db")
        shutil.copyfile(root / "single.db", copy)
        os.replace(copy, root / "single.db")

    def source_changed():
        info = (src / "graph.db").stat()
        os.utime(src / "graph.db", ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000_000))

    def fifo_manifest():
        os.unlink(root / "manifest.json")
        os.mkfifo(root / "manifest.json")

    def symlink_marker():
        os.unlink(root / "marker.json")
        os.symlink("manifest.json", root / "marker.json")

    def symlink_lock():
        os.unlink(root / "single.db.lifetime.lock")
        os.symlink("manifest.json", root / "single.db.lifetime.lock")

    def vec_load():
        def broken(conn):
            raise merge_mod.MergeError("sqlite_vec is not importable")

        monkeypatch.setattr(merge_mod, "_load_vec", broken)

    hooks = {
        "verify-fileset": fileset, "verify-fileform": hardlink, "hardlink-single-db": hardlink,
        "verify-binding": binding,
        "verify-metadata-rows": lambda: rewrite_db(_SECOND_METADATA_ROW),
        "metadata-two-rows": lambda: rewrite_db(_SECOND_METADATA_ROW),
        "metadata-zero-rows": lambda: rewrite_db("DELETE FROM single_file_metadata"),
        "verify-tables": lambda: rewrite_db("INSERT INTO graph_nodes VALUES ('extra-node', 'p')"),
        "verify-billing": billing, "verify-identity": identity, "verify-source-changed": source_changed,
        "fifo-manifest": fifo_manifest, "symlink-marker": symlink_marker, "symlink-lifetime-lock": symlink_lock,
        "verify-vec-load": vec_load,
    }
    return hooks.get(variant)


def _install_publish_failure(h, failure):
    ctx, mp = h.ctx, h.monkeypatch
    intent_name = ctx.intent.name

    def r8_raise(code, *, before_action=None):
        def hook():
            if before_action is not None:
                before_action()
            raise OSError(code, "injected rename failure")

        h.install_r8()
        h.r8_before.append(hook)

    simple = {
        "intent-tmp-create": lambda: h.inject_open("intent-tmp", 0),
        "intent-tmp-write": lambda: h.inject_fd_call("write", "intent-tmp", 0),
        "intent-tmp-fsync": lambda: h.inject_fd_call("fsync", "intent-tmp", 0),
        "intent-rename-before": lambda: h.inject_rename(intent_name, 0),
        "intent-rename-after": lambda: h.inject_rename(intent_name, 0, after=True),
        "intent-parent-fsync": lambda: h.inject_fd_call("fsync", "intent-parent", 1),
        "intent-parent-open": lambda: h.inject_open("intent-parent", 1),
        "publish-parent-open": lambda: (h.install_r8(), h.inject_open("publish-parent", 0)),
        "rename-enosys-einval": lambda: r8_raise(errno.ENOSYS),
        "rename-exdev": lambda: r8_raise(errno.EXDEV),
        "rename-error-effect-absent": lambda: r8_raise(errno.EIO),
        "rename-eexist": lambda: (h.install_r8(), h.r8_before.append(lambda: os.mkdir(ctx.root))),
        "rename-error-effect-present": lambda: (h.install_r8(), h.r8_after.append(
            lambda: (_ for _ in ()).throw(OSError(errno.EIO, "injected rename failure")))),
        "rename-error-both-absent": lambda: r8_raise(errno.EIO, before_action=lambda: shutil.rmtree(ctx.staging)),
        "publish-parent-fsync-before": lambda: h.inject_fd_call("fsync", "publish-parent", 0),
        "publish-parent-fsync-after": lambda: h.inject_fd_call("fsync", "publish-parent", 0, after=True),
        "root-dir-open": lambda: h.inject_open("root-dir", 0),
        "root-file-open": lambda: h.inject_open("root-file", 0),
        "ledger-tmp-create": lambda: h.inject_open("ledger-tmp", 0),
        "ledger-tmp-write": lambda: h.inject_fd_call("write", "ledger-tmp", 0),
        "ledger-tmp-fsync": lambda: h.inject_fd_call("fsync", "ledger-tmp", 0),
        "ledger-rename-before": lambda: h.inject_rename("ledger.json", 0),
        "ledger-rename-after": lambda: h.inject_rename("ledger.json", 0, after=True),
        "root-dir-sync-open": lambda: h.inject_open("root-dir", 1),
        "root-dir-fsync": lambda: h.inject_fd_call("fsync", "root-dir", 1),
        "readback-open": lambda: h.inject_open("root-file", 3),
        "intent-ready-tmp-create": lambda: h.inject_open("intent-tmp", 1),
        "intent-ready-tmp-write": lambda: h.inject_fd_call("write", "intent-tmp", 1),
        "intent-ready-tmp-fsync": lambda: h.inject_fd_call("fsync", "intent-tmp", 1),
        "intent-ready-rename-before": lambda: h.inject_rename(intent_name, 1),
        "intent-ready-rename-after": lambda: h.inject_rename(intent_name, 1, after=True),
        "intent-ready-parent-fsync": lambda: h.inject_fd_call("fsync", "intent-parent", 2),
        "intent-ready-parent-open": lambda: h.inject_open("intent-parent", 2),
    }
    root_marker = "published/single.db?mode=ro&immutable=1"
    if failure in simple:
        simple[failure]()
    elif failure == "rename-error-observe-fails":
        armed = []
        real_exists = merge_mod._publication_exists

        def exists(path):
            if armed and Path(path) == ctx.staging:
                raise PermissionError(errno.EACCES, "injected observe failure")
            return real_exists(path)

        mp.setattr(merge_mod, "_publication_exists", exists)
        r8_raise(errno.EIO, before_action=lambda: armed.append(True))
    elif failure == "verify-connect":
        h.inject_connect(root_marker, fail_open=True)
    elif failure == "verify-connect-close":
        h.inject_connect(root_marker, fail_close=True)
    elif failure == "verify-quick-check":
        h.inject_connect(root_marker, bad_quick_check=True)
    elif failure == "verify-sidecar":
        real_lexists = os.path.lexists

        def lexists(path):
            return True if str(path) == str(ctx.root / "single.db-wal") else real_lexists(path)

        mp.setattr(os.path, "lexists", lexists)
    elif failure == "readback-mismatch":
        from opencrab.stores import _single_file

        real_read = _single_file.read_binding_fd
        count = []

        def read(directory_fd, name, fd):
            raw = real_read(directory_fd, name, fd)
            if name == "ledger.json":
                count.append(True)
                if len(count) == 2:
                    data = json.loads(raw)
                    data["generation"] = "f" * 32 if data["generation"] != "f" * 32 else "e" * 32
                    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            return raw

        mp.setattr(_single_file, "read_binding_fd", read)
    else:
        hook = _variant_hook(ctx, failure, mp)
        assert hook is not None, failure
        h.install_r8()
        h.r8_after.append(hook)


def _run_publish_failure(source_dir, tmp_path, monkeypatch, capsys, failure):
    rc, uncertain, held, message, disk = _PUB_ROWS[failure]
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    tree = _source_tree(source_dir)
    parent_names = _source_parent_names(source_dir)
    _install_publish_failure(harness, failure)
    try:
        assert merge_mod.main(ctx.args) == rc
        err = capsys.readouterr().err
        assert message in err
        assert "publication ready" not in err
        if failure == "verify-source-changed":
            assert "publication preflight failed: source file changed during use" in err
        owner = harness.owners[-1]
        assert owner.uncertain is uncertain
        assert _flock_contended(ctx.lock_src) is held
        assert _flock_contended(ctx.lock_root) is held
        actual = _ready_disk(ctx)
        assert {key: actual[key] for key in disk} == disk
        if failure == "publish-parent-open":
            assert harness.r8_calls == 0
        if failure not in ("verify-source-changed",):
            assert _source_tree(source_dir) == tree
        assert _source_parent_names(source_dir) == parent_names
        assert not any(source_dir.glob("*-wal")) and not any(source_dir.glob("*-shm"))
        if held:
            assert str(owner.root) in {str(key) for key in merge_mod._publication_owners}
        else:
            assert all(fd is None for fd in owner.fd_slots.values()) and not owner.connections
    finally:
        harness.discard()


@pytest.mark.parametrize("failure", list(_PUB_ROWS))
def test_publication_ready_publish_failure_states(source_dir, tmp_path, monkeypatch, capsys, failure):
    _run_publish_failure(source_dir, tmp_path, monkeypatch, capsys, failure)


def _install_post_ready(h, failure):
    ctx = h.ctx

    def touch_source():
        info = (ctx.src / "graph.db").stat()
        os.utime(ctx.src / "graph.db", ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000_000))

    def replace_source_lock():
        fresh = ctx.lock_src.with_name("lock.new")
        fresh.touch()
        os.replace(fresh, ctx.lock_src)

    if failure.endswith("-close") and failure[:-len("-close")] in _CLOSE_ROLES:
        h.inject_fd_call("close", failure[:-len("-close")], 0)
    elif failure == "inputs-exit-check":
        h.inject_fd_call("fsync", "intent-parent", 2, then=touch_source)
    elif failure == "source-lock-exit-identity":
        h.inject_fd_call("fsync", "intent-parent", 2, then=replace_source_lock)
    elif failure == "post-ready-source-and-cleanup":
        h.inject_fd_call("fsync", "intent-parent", 2, then=touch_source)
        h.inject_fd_call("close", "root-file", 0)
    elif failure == "step-failure-then-cleanup-failure":
        h.install_r8()
        h.r8_after.append(_variant_hook(ctx, "verify-fileset"))
        h.inject_fd_call("close", "root-dir", 0)
    else:
        raise AssertionError(failure)


@pytest.mark.parametrize("failure", _POST_READY_IDS)
def test_publication_ready_post_ready_failure_states(source_dir, tmp_path, monkeypatch, capsys, failure):
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    tree = _source_tree(source_dir)
    parent_names = _source_parent_names(source_dir)
    _install_post_ready(harness, failure)
    try:
        assert merge_mod.main(ctx.args) == 2
        err = capsys.readouterr().err
        lines = [line for line in err.splitlines() if line.startswith("publication")]
        owner = harness.owners[-1]
        if failure.endswith("-close"):
            assert any(line.startswith("publication ready; cleanup failed: ") for line in lines)
            assert "publication preflight failed" not in err and "post-ready source check failed" not in err
            assert owner.uncertain
            assert _flock_contended(ctx.lock_root) is False and _flock_contended(ctx.lock_src) is True
            _assert_ready_published(ctx.root)
        elif failure in ("inputs-exit-check", "source-lock-exit-identity"):
            cause = ("source file changed during use" if failure == "inputs-exit-check"
                     else "source lifetime lock changed")
            assert f"publication ready; post-ready source check failed: {cause}" in lines
            assert "cleanup failed" not in err and "publication preflight failed" not in err
            assert not owner.uncertain
            assert _flock_contended(ctx.lock_root) is False and _flock_contended(ctx.lock_src) is False
            _assert_ready_published(ctx.root)
        elif failure == "post-ready-source-and-cleanup":
            line = next(item for item in lines if item.startswith("publication ready;"))
            assert line.startswith("publication ready; post-ready source check failed: source file changed "
                                   "during use; cleanup failed: ")
            assert "injected close failure" in line
            assert "publication error cause: OSError" in err
            assert owner.uncertain
            assert _flock_contended(ctx.lock_root) is False and _flock_contended(ctx.lock_src) is True
            _assert_ready_published(ctx.root)
        else:
            assert lines[0] == "publication publish failed: publication root file set differs"
            assert lines[-1].startswith("publication preflight failed: ") and "injected close failure" in lines[-1]
            assert "publication ready" not in err
            assert owner.uncertain
            assert _flock_contended(ctx.lock_root) is False and _flock_contended(ctx.lock_src) is True
            actual = _ready_disk(ctx)
            assert {key: actual[key] for key in _R9_BASE} == _R9_BASE
        if failure not in ("inputs-exit-check", "source-lock-exit-identity", "post-ready-source-and-cleanup"):
            assert _source_tree(source_dir) == tree
        assert _source_parent_names(source_dir) == parent_names
    finally:
        harness.discard()


def test_publication_ready_failure_id_set_is_complete():
    """6절 표의 failure id 전량이 시험 인자 집합에 있다(누락 0, 초과 0)."""
    covered = (set(_READY_ROWS) | set(_PUB_ROWS) | set(_POST_READY_IDS) | set(_EXISTING_POST_READY_IDS))
    assert covered == _DESIGN_FAILURE_IDS
    assert not set(_READY_ROWS) & set(_PUB_ROWS)


def test_publication_ready_main_publishes(source_dir, tmp_path, capsys):
    from opencrab.stores._single_file import load_single_file_binding

    ctx = _ready_setup(source_dir, tmp_path)
    tree = _source_tree(source_dir)
    parent_names = _source_parent_names(source_dir)
    assert merge_mod.main(ctx.args) == 0
    err = capsys.readouterr().err
    assert err.splitlines().count("publication ready") == 1
    _assert_ready_published(ctx.root)
    with _immutable_conn(ctx.root / "single.db") as conn:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        metadata = dict(zip(("root", "database", "db_uuid", "generation"),
                            conn.execute("SELECT root, database_path, db_uuid, generation "
                                         "FROM single_file_metadata").fetchone(), strict=True))
        assert conn.execute("SELECT count(*) FROM vectors_kure").fetchone()[0] == 40
        assert conn.execute("SELECT count(*) FROM doc_sources_fts").fetchone()[0] == 40
    dir_fd = os.open(ctx.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        load_single_file_binding(str(ctx.root), str(ctx.root / "single.db"), dir_fd, dir_fd, "ledger.json",
                                 "manifest.json", "marker.json", metadata, state="ready")
    finally:
        os.close(dir_fd)
    assert _source_tree(source_dir) == tree
    assert _source_parent_names(source_dir) == parent_names
    assert not any(source_dir.glob("*-wal")) and not any(source_dir.glob("*-shm"))
    assert _flock_contended(ctx.lock_src) is False and _flock_contended(ctx.lock_root) is False


def test_publication_ready_reader_default_rejects_ready(source_dir, tmp_path):
    from opencrab.stores._single_file import decode_binding_json, validate_single_file_binding

    ctx = _ready_setup(source_dir, tmp_path)
    assert merge_mod.main(ctx.args) == 0
    raws = {name: (ctx.root / name).read_bytes() for name in ("ledger.json", "manifest.json", "marker.json")}
    ledger, manifest, marker = (decode_binding_json(raws[name]) for name in raws)
    with _immutable_conn(ctx.root / "single.db") as conn:
        metadata = dict(zip(("root", "database", "db_uuid", "generation"),
                            conn.execute("SELECT root, database_path, db_uuid, generation "
                                         "FROM single_file_metadata").fetchone(), strict=True))
    digest = hashlib.sha256(raws["manifest.json"]).hexdigest()
    for state in ("ready", "staged"):
        with pytest.raises(RuntimeError, match="single SQLite generation is not authorized"):
            validate_single_file_binding(str(ctx.root), str(ctx.root / "single.db"), {**ledger, "state": state},
                                         manifest, marker, metadata, digest)


def test_publication_ready_staged_never_ready_before_verify(source_dir, tmp_path, monkeypatch):
    from opencrab.stores import _single_file

    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    harness.install_r8()
    events = []
    armed = []

    def state():
        try:
            return json.loads((ctx.root / "ledger.json").read_text())["state"]
        except FileNotFoundError:
            return None

    harness.r8_after.append(lambda: (armed.append(True), events.append(("rename-noreplace", state()))))
    real_verify = merge_mod._verify_published

    def verify(*args, **kwargs):
        events.append(("verify-before", state()))
        result = real_verify(*args, **kwargs)
        events.append(("verify-after", state()))
        return result

    monkeypatch.setattr(merge_mod, "_verify_published", verify)
    real_read = _single_file.read_binding_fd

    def read(directory_fd, name, fd):
        if armed:
            events.append(("read-binding", state()))
        return real_read(directory_fd, name, fd)

    monkeypatch.setattr(_single_file, "read_binding_fd", read)
    real_rename = os.rename

    def rename(src, dst, *args, **kwargs):
        if Path(dst).name != "ledger.json":
            return real_rename(src, dst, *args, **kwargs)
        events.append(("ledger-rename-before", state()))
        result = real_rename(src, dst, *args, **kwargs)
        events.append(("ledger-rename-after", state()))
        return result

    monkeypatch.setattr(os, "rename", rename)
    assert merge_mod.main(ctx.args) == 0
    tags = [tag for tag, _ in events]
    assert tags.count("read-binding") == 4
    after = tags.index("ledger-rename-after")
    assert all(value == "staged" for _, value in events[:after])
    assert events[after][1] == "ready"
    fourth = [index for index, tag in enumerate(tags) if tag == "read-binding"][3]
    assert fourth > after and events[fourth][1] == "ready"
    assert tags.index("verify-after") < tags.index("ledger-rename-before")


def test_publication_ready_manifest_digest_binds_files(source_dir, tmp_path):
    from opencrab.stores._single_file import load_single_file_binding

    ctx = _ready_setup(source_dir, tmp_path)
    assert merge_mod.main(ctx.args) == 0
    digest = hashlib.sha256((ctx.root / "manifest.json").read_bytes()).hexdigest()
    assert json.loads((ctx.root / "marker.json").read_text())["manifest_digest"] == digest
    assert json.loads((ctx.root / "ledger.json").read_text())["manifest_digest"] == digest
    assert json.loads(ctx.intent.read_text())["manifest_digest"] == digest
    with _immutable_conn(ctx.root / "single.db") as conn:
        metadata = dict(zip(("root", "database", "db_uuid", "generation"),
                            conn.execute("SELECT root, database_path, db_uuid, generation "
                                         "FROM single_file_metadata").fetchone(), strict=True))
    manifest_path = ctx.root / "manifest.json"
    original = manifest_path.read_bytes()
    assert b'"version":1' in original
    manifest_path.write_bytes(original.replace(b'"version":1', b'"version":2'))
    dir_fd = os.open(ctx.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="manifest binding differs"):
            load_single_file_binding(str(ctx.root), str(ctx.root / "single.db"), dir_fd, dir_fd, "ledger.json",
                                     "manifest.json", "marker.json", metadata, state="ready")
    finally:
        os.close(dir_fd)


def test_publication_ready_file_modes(source_dir, tmp_path):
    ctx = _ready_setup(source_dir, tmp_path)
    assert merge_mod.main(ctx.args) == 0
    assert stat_mode(ctx.root) == 0o700
    for name in ("billing.db", "ledger.json", "manifest.json", "marker.json", "single.db",
                 "single.db.lifetime.lock"):
        assert (name, stat_mode(ctx.root / name)) == (name, 0o600)


def stat_mode(path):
    return os.lstat(path).st_mode & 0o777


def test_publication_ready_uses_immutable_connection(source_dir, tmp_path, monkeypatch):
    ctx = _ready_setup(source_dir, tmp_path)
    uris = []
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        if "/single.db?mode=ro" in str(database):  # 게시 흐름의 R3와 R9 연결. 기존 .partial 연결은 제외한다
            uris.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    listings = []
    real_counts = merge_mod._publication_table_counts

    def counts(conn):
        for directory in (ctx.staging, ctx.root):
            if directory.is_dir():
                listings.append(sorted(item.name for item in directory.iterdir()))
        return real_counts(conn)

    monkeypatch.setattr(merge_mod, "_publication_table_counts", counts)
    assert merge_mod.main(ctx.args) == 0
    assert any("published.staging/single.db" in uri for uri in uris)
    assert any("published/single.db" in uri for uri in uris)
    assert all("immutable=1" in uri for uri in uris)
    assert len(uris) == 2 and len(listings) == 2
    assert all(not any(name.endswith(("-wal", "-shm", "-journal")) for name in names) for names in listings)


def test_publication_ready_q1_and_fd_order(source_dir, tmp_path, monkeypatch):
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    events = harness.events
    real_close, real_flock = os.close, merge_mod.fcntl.flock

    def close(fd):
        role = harness.by_fd.get(fd)
        if role is not None:
            events.append(("close", role))
        return real_close(fd)

    def flock(fd, operation):
        if operation == merge_mod.fcntl.LOCK_UN:
            owner = harness.current_owner()
            events.append(("unlock", len(owner.connections) if owner else None))
        return real_flock(fd, operation)

    real_mounts = merge_mod._PublicationOwner.read_mounts

    def mounts(owner, work):
        result = real_mounts(owner, work)  # 이 호출은 stream을 안에서 닫고 돌아온다
        events.append(("mounts-done", None))
        return result

    monkeypatch.setattr(merge_mod._PublicationOwner, "read_mounts", mounts)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(merge_mod.fcntl, "flock", flock)
    assert merge_mod.main(ctx.args) == 0
    kinds = [kind for kind, _ in events]
    first_unlock, last_unlock = kinds.index("unlock"), len(kinds) - 1 - kinds[::-1].index("unlock")
    assert first_unlock < last_unlock
    assert events[first_unlock][1] == 0  # 연결은 root 해제 전에 모두 수집된다
    assert events.index(("mounts-done", None)) < events.index(("open", "intent"))
    new_roles = ("probe-dir", "manifest-write", "marker-write", "ledger-write", "lock-create", "staging-seal",
                 "intent-tmp", "ledger-tmp", "publish-parent", "root-dir", "root-file", "intent-parent")
    for role in new_roles:
        indexes = [index for index, event in enumerate(events) if event == ("close", role)]
        assert indexes, role
        assert all(first_unlock < index < last_unlock for index in indexes), role
    for role in new_roles:
        assert merge_mod._classify_fd_role(role) == "other"


def test_publication_ready_repeat_main_refuses(source_dir, tmp_path, capsys):
    ctx = _ready_setup(source_dir, tmp_path)
    assert merge_mod.main(ctx.args) == 0
    capsys.readouterr()
    before = {path.name: path.read_bytes() for path in ctx.root.iterdir()}
    assert merge_mod.main(ctx.args) == 2
    assert "publication artifacts already exist" in capsys.readouterr().err
    assert {path.name: path.read_bytes() for path in ctx.root.iterdir()} == before


@pytest.mark.parametrize("failure", ["rename-error-effect-absent", "rename-eexist", "rename-error-effect-present",
                                     "rename-error-both-absent"])
def test_publication_rename_error_classification(source_dir, tmp_path, monkeypatch, capsys, failure):
    """R8 관측 조합 네 가지: 미수행, 외부 root, 수행됨, uncertain."""
    _run_publish_failure(source_dir, tmp_path, monkeypatch, capsys, failure)


def test_publication_rename_refuses_existing_empty_root(source_dir, tmp_path, monkeypatch, capsys):
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    _install_publish_failure(harness, "rename-eexist")
    try:
        assert merge_mod.main(ctx.args) == 1
        assert _PUBLISH in capsys.readouterr().err
        assert ctx.root.is_dir() and list(ctx.root.iterdir()) == []
        assert (ctx.staging / "ledger.json").exists()
    finally:
        harness.discard()


def test_publication_rename_uncertain_blocks_reentry(source_dir, tmp_path, monkeypatch, capsys):
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    _install_publish_failure(harness, "rename-error-both-absent")
    try:
        assert merge_mod.main(ctx.args) == 1
        capsys.readouterr()
        assert harness.owners[-1].uncertain
        mark = harness.r8_calls
        assert merge_mod.main(ctx.args) == 2
        assert "publication owner already exists" in capsys.readouterr().err
        with pytest.raises(merge_mod.MergeError, match="publication owner already exists"):
            with merge_mod._publication_owner(ctx.root):
                pass
        assert harness.r8_calls == mark
    finally:
        harness.discard()


@pytest.mark.parametrize("variant", ["control", "fifo-manifest", "hardlink-single-db", "symlink-marker",
                                     "symlink-lifetime-lock", "metadata-two-rows", "metadata-zero-rows"])
def test_publication_ready_verify_matches_reader(source_dir, tmp_path, monkeypatch, capsys, variant):
    """R9("staged")와 읽는 쪽 validate_binding이 같은 변형을 둘 다 거부한다. 대조군은 둘 다 통과한다."""
    from opencrab.stores._single_file import SharedSqliteFile

    causes = {"fifo-manifest": "publication root file form differs",
              "hardlink-single-db": "publication root file form differs",
              "symlink-marker": "publication root file form differs",
              "symlink-lifetime-lock": "publication root file form differs",
              "metadata-two-rows": "publication metadata row count differs",
              "metadata-zero-rows": "publication metadata row count differs"}
    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    hook = _variant_hook(ctx, variant, monkeypatch)
    if hook is not None:
        harness.install_r8()
        harness.r8_after.append(hook)
    try:
        rc = merge_mod.main(ctx.args)
        err = capsys.readouterr().err
        if variant == "control":
            assert rc == 0
        else:
            assert rc == 1 and f"{_PUBLISH} {causes[variant]}" in err
            assert _ready_disk(ctx)["intent"] == "publishing" and _ready_disk(ctx)["ledger"] == "staged"
        ledger_path = ctx.root / "ledger.json"
        ledger_path.write_text(json.dumps({**json.loads(ledger_path.read_text()), "state": "authorized"},
                                          sort_keys=True, separators=(",", ":")))
        shared, dir_fd, rejected = None, None, False
        try:
            shared = SharedSqliteFile(str(ctx.root / "single.db"), checked=True)
            dir_fd = os.open(ctx.root, os.O_RDONLY | os.O_DIRECTORY)
            shared.validate_binding(str(ctx.root), dir_fd, dir_fd, "ledger.json", "manifest.json", "marker.json")
        except Exception:
            rejected = True
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
            if shared is not None:
                shared.close()
        assert rejected is (variant != "control")
    finally:
        harness.discard()


def test_publication_ready_verify_value_error_class(source_dir, tmp_path, monkeypatch, capsys):
    """R9의 읽기 ValueError는 단계 except가 받아 rc 1이다. preflight rc 2가 아니다."""
    from opencrab.stores import _single_file

    ctx = _ready_setup(source_dir, tmp_path)
    harness = _ReadyHarness(monkeypatch, ctx)
    harness.install_r8()
    armed = []
    harness.r8_after.append(lambda: armed.append(True))
    real_read = _single_file.read_binding_fd

    def read(directory_fd, name, fd):
        if armed:
            raise ValueError("file must be regular and have one link")
        return real_read(directory_fd, name, fd)

    monkeypatch.setattr(_single_file, "read_binding_fd", read)
    try:
        assert merge_mod.main(ctx.args) == 1
        err = capsys.readouterr().err
        assert f"{_PUBLISH} file must be regular and have one link" in err
        assert "publication preflight failed" not in err
    finally:
        harness.discard()
