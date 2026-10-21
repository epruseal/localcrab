from __future__ import annotations

import contextlib
import json

import pytest

from opencrab.auth import Principal
from scripts import recover_source_vectors as recovery


@pytest.fixture(autouse=True)
def fake_identity(monkeypatch):
    monkeypatch.setattr(
        recovery,
        "vector_identity",
        lambda vector: {"class": "fake", "table": vector._table, "dimension": vector._dim, "has_bit": False, "path_digest": "fake"},
    )


class FakeReader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_source(self, ident):
        self.calls.append(ident)
        return self.rows.get(ident)


class FakeVector:
    _db_path = "fake-vectors.db"
    _table = "vectors"
    _dim = 2
    _has_bit_column = False

    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.verify_ok = True

    def get_by_id(self, ident):
        value = self.rows.get(ident)
        return None if value is None else {"document": value["document"], "metadata": dict(value["metadata"])}

    def verify_recovery_row(self, ident):
        return self.verify_ok and ident in self.rows

    def upsert_texts(self, *, texts, ids, metadatas):
        self.calls.append((list(texts), list(ids), [dict(x) for x in metadatas]))
        for text, ident, meta in zip(texts, ids, metadatas):
            self.rows[ident] = {"document": text, "metadata": dict(meta)}


def principal():
    return Principal(user_id="test-user", is_local=True, disabled=False)


def source(ident="id-a", text="new", pack="pack-a", **metadata):
    return recovery.SourceRow(ident, text, {"pack_id": pack, **metadata})


def vector(ident="id-a", text="old", pack="pack-a", **metadata):
    return {ident: {"document": text, "metadata": {"pack_id": pack, **metadata}}}


def approved(tmp_path, *ids):
    path = tmp_path / "approved.jsonl"
    path.write_text("".join(json.dumps({"id": i}) + "\n" for i in ids), encoding="utf-8")
    return path


@pytest.fixture
def auth(monkeypatch):
    calls = []
    monkeypatch.setattr(recovery, "authorize", lambda sql, principal, pack: calls.append((sql, principal, pack)))
    return calls


def test_dry_run_writes_manifest_without_vector_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector())
    manifest = recovery.plan(approved(tmp_path, "id-a"), tmp_path / "run", reader, vec, object(), principal())
    assert manifest["targets"][0]["state"] == "planned"
    assert vec.calls == []
    assert reader.calls == ["id-a"]
    assert len(auth) == 1


def test_apply_requires_matching_digests_and_rewrites_one_target(tmp_path, auth):
    reader = FakeReader({"id-a": source(text="authority", category="x")})
    vec = FakeVector(vector(text="wrong", category="old"))
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    def backup(_dest):
        return {"verified": True, "set": "synthetic"}

    result = recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", backup, contextlib.nullcontext)
    assert result["targets"][0]["state"] == "verified"
    assert vec.rows["id-a"] == {"document": "authority", "metadata": {"pack_id": "pack-a", "category": "x"}}
    assert len(vec.calls) == 1
    assert len(auth) == 2


def test_apply_refuses_source_drift_before_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source(text="first")})
    vec = FakeVector(vector())
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    reader.rows["id-a"] = source(text="changed")
    with pytest.raises(recovery.RecoveryError, match="changed after dry-run"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert vec.calls == []


def test_owner_mismatch_refuses_without_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector(pack="pack-b"))
    with pytest.raises(recovery.RecoveryError, match="owner"):
        recovery.plan(approved(tmp_path, "id-a"), tmp_path / "run", reader, vec, object(), principal())
    assert vec.calls == []


def test_decode_error_refuses_without_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": recovery.SourceRow("id-a", "x", {}, True)})
    vec = FakeVector(vector())
    with pytest.raises(recovery.RecoveryError, match="malformed"):
        recovery.plan(approved(tmp_path, "id-a"), tmp_path / "run", reader, vec, object(), principal())
    assert vec.calls == []


def test_approved_input_rejects_duplicates_and_extra_keys(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id":"id-a"}\n{"id":"id-a"}\n', encoding="utf-8")
    with pytest.raises(recovery.RecoveryError, match="duplicate"):
        recovery.read_approved(path)
    path.write_text('{"id":"id-a","other":1}\n', encoding="utf-8")
    with pytest.raises(recovery.RecoveryError, match="invalid record"):
        recovery.read_approved(path)


def test_backup_failure_has_zero_upserts_and_does_not_enter_lock(tmp_path, auth):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector())
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    entered = []
    def lock():
        entered.append(True)
        return contextlib.nullcontext()
    with pytest.raises(recovery.RecoveryError, match="backup"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": False}, lock)
    assert entered == []
    assert vec.calls == []


def test_source_reader_reads_latest_wal_value(tmp_path):
    db = tmp_path / "doc_store.db"
    writer = recovery.sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE doc_sources (source_id TEXT PRIMARY KEY, text TEXT, metadata TEXT)")
    writer.execute("INSERT INTO doc_sources VALUES (?, ?, ?)", ("id-a", "old", '{"pack_id":"pack-a"}'))
    writer.commit()
    assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
    writer.execute("UPDATE doc_sources SET text=? WHERE source_id=?", ("new", "id-a"))
    writer.commit()
    row = recovery.SourceReader(tmp_path).get_source("id-a")
    assert row is not None and row.text == "new"
    writer.close()


def test_verified_resume_checks_current_readback(tmp_path, auth):
    reader = FakeReader({"id-a": source(text="authority")})
    vec = FakeVector(vector(text="wrong"))
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    def backup(_): return {"verified": True}
    recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", backup, contextlib.nullcontext)
    vec.rows["id-a"]["document"] = "bad"
    with pytest.raises(recovery.RecoveryError, match="readback"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", backup, contextlib.nullcontext)


def test_apply_writes_each_approved_target_once_in_order(tmp_path, auth):
    reader = FakeReader({"id-a": source("id-a", "a"), "id-b": source("id-b", "b")})
    vec = FakeVector({**vector("id-a", "old-a"), **vector("id-b", "old-b")})
    file = approved(tmp_path, "id-a", "id-b")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    def backup(_): return {"verified": True}
    recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", backup, contextlib.nullcontext)
    assert [call[1][0] for call in vec.calls] == ["id-a", "id-b"]
    assert reader.calls == ["id-a", "id-b", "id-a", "id-b"]


def test_apply_refuses_vector_drift_before_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector(text="old"))
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    vec.rows["id-a"]["document"] = "external-change"
    with pytest.raises(recovery.RecoveryError, match="changed after dry-run"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert vec.calls == []


def test_apply_authorization_failure_has_zero_upserts(tmp_path, monkeypatch):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector())
    file = approved(tmp_path, "id-a")
    monkeypatch.setattr(recovery, "authorize", lambda *_args: None)
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    def denied(*_args):
        raise PermissionError("denied")
    monkeypatch.setattr(recovery, "authorize", denied)
    with pytest.raises(PermissionError, match="denied"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert vec.calls == []


def test_manifest_target_list_must_match_approved_input(tmp_path, auth):
    reader = FakeReader({"id-a": source(), "id-b": source("id-b")})
    vec = FakeVector({**vector("id-a"), **vector("id-b")})
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    manifest_path = tmp_path / "run" / "manifest.json"
    manifest = recovery.load_manifest(manifest_path)
    manifest["targets"].append({"id": "id-b", "state": "planned"})
    recovery.write_manifest(manifest_path, manifest)
    with pytest.raises(recovery.RecoveryError, match="manifest"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert vec.calls == []


def test_apply_verifier_failure_has_zero_verified_target(tmp_path, auth):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector())
    vec.verify_ok = False
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    with pytest.raises(recovery.RecoveryError, match="embedding"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert recovery.load_manifest(tmp_path / "run" / "manifest.json")["targets"][0]["state"] == "written"


class RecordingLock:
    def __init__(self, events, on_enter=None):
        self.events = events
        self.on_enter = on_enter
        self.held = False

    def __enter__(self):
        self.held = True
        if self.on_enter:
            self.on_enter()
        return self

    def __exit__(self, *_args):
        self.held = False


def test_apply_rechecks_inside_lock_before_upsert(tmp_path, monkeypatch):
    reader = FakeReader({"id-a": source(text="first")})
    vec = FakeVector(vector())
    file = approved(tmp_path, "id-a")
    monkeypatch.setattr(recovery, "authorize", lambda *_args: None)
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    events = []
    held_calls = []
    lock = RecordingLock(events, lambda: reader.rows.__setitem__("id-a", source(text="drift")))
    original_get = reader.get_source
    def guarded_source(ident):
        assert lock.held
        held_calls.append("source_read")
        return original_get(ident)
    reader.get_source = guarded_source
    original_get_vector = vec.get_by_id
    def guarded_vector(ident):
        assert lock.held
        held_calls.append("vector_read")
        return original_get_vector(ident)
    vec.get_by_id = guarded_vector
    def guarded_authorize(*_args):
        assert lock.held
        held_calls.append("authorized")
    monkeypatch.setattr(recovery, "authorize", guarded_authorize)
    with pytest.raises(recovery.RecoveryError, match="changed"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, lambda: lock, events.append)
    assert events == ["backup_verified", "lock_entered", "source_read", "authorized", "vector_read"]
    assert held_calls == ["source_read", "authorized", "vector_read"]
    assert vec.calls == []


def test_apply_orders_backup_lock_rechecks_and_upsert(tmp_path, monkeypatch):
    reader = FakeReader({"id-a": source(text="authority")})
    vec = FakeVector(vector(text="old"))
    file = approved(tmp_path, "id-a")
    monkeypatch.setattr(recovery, "authorize", lambda *_args: None)
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    events = []
    lock = RecordingLock(events)
    original_source = reader.get_source
    original_vector = vec.get_by_id
    def source_under_lock(ident):
        assert lock.held
        return original_source(ident)
    def vector_under_lock(ident):
        assert lock.held
        return original_vector(ident)
    reader.get_source = source_under_lock
    vec.get_by_id = vector_under_lock
    def authorize_under_lock(*_args):
        assert lock.held
    monkeypatch.setattr(recovery, "authorize", authorize_under_lock)
    recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, lambda: lock, events.append)
    assert events == ["backup_verified", "lock_entered", "source_read", "authorized", "vector_read", "upsert"]


def test_missing_representation_verifier_refuses_before_verified(tmp_path, auth):
    class NoVerifierVector:
        _db_path = "fake-vectors.db"
        _table = "vectors"
        _dim = 2
        _has_bit_column = False

        def __init__(self):
            self.rows = vector()
            self.calls = []

        def get_by_id(self, ident):
            row = self.rows.get(ident)
            return None if row is None else {"document": row["document"], "metadata": dict(row["metadata"])}

        def upsert_texts(self, *, texts, ids, metadatas):
            self.calls.append(ids)
            self.rows[ids[0]] = {"document": texts[0], "metadata": dict(metadatas[0])}

    reader = FakeReader({"id-a": source()})
    vec = NoVerifierVector()
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    with pytest.raises(recovery.RecoveryError, match="verifier"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert recovery.load_manifest(tmp_path / "run" / "manifest.json")["targets"][0]["state"] == "written"

@pytest.mark.parametrize("bad_target", [None, {"id": "id-a"}, {"id": "id-a", "pack_id": "pack-a", "source_digest": "s", "vector_digest": "v", "state": "unknown"}])
def test_manifest_malformed_target_refuses_before_backup(tmp_path, auth, bad_target):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector())
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    path = tmp_path / "run" / "manifest.json"
    manifest = recovery.load_manifest(path)
    manifest["targets"] = [bad_target]
    recovery.write_manifest(path, manifest)
    calls = []
    with pytest.raises(recovery.RecoveryError, match="manifest"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: calls.append("backup"), contextlib.nullcontext)
    assert calls == []
    assert vec.calls == []


def test_written_target_resumes_without_second_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source(text="authority")})
    vec = FakeVector(vector(text="old"))
    vec.verify_ok = False
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), principal())
    with pytest.raises(recovery.RecoveryError, match="embedding"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert len(vec.calls) == 1
    vec.verify_ok = True
    recovery.apply(file, tmp_path / "run", reader, vec, object(), principal(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert len(vec.calls) == 1
    assert recovery.load_manifest(tmp_path / "run" / "manifest.json")["targets"][0]["state"] == "verified"

@pytest.mark.parametrize(
    ("mode", "backend", "want"),
    [
        ("local", "sqlite-vec", True),
        ("local", "chroma", False),
        ("local", "pgvector", False),
        ("docker", "sqlite-vec", False),
        ("docker", "chroma", False),
        ("docker", "pgvector", False),
        ("kuzu", "sqlite-vec", False),
        ("kuzu", "chroma", False),
        ("kuzu", "pgvector", False),
        ("pg", "sqlite-vec", False),
        ("pg", "chroma", False),
        ("pg", "pgvector", False),
    ],
)
def test_supported_settings_matrix(mode, backend, want):
    settings = type("Settings", (), {"storage_mode": mode, "vector_backend_resolved": backend})()
    assert recovery._supported_settings(settings) is want


def test_unsupported_settings_refuse_before_dependencies(tmp_path, monkeypatch):
    import opencrab.config

    settings = type("Settings", (), {"storage_mode": "kuzu", "vector_backend_resolved": "sqlite-vec"})()
    monkeypatch.setattr(opencrab.config, "get_settings", lambda: settings)
    calls = []
    monkeypatch.setattr(recovery, "require_local_principal", lambda: calls.append("principal"))
    path = approved(tmp_path, "id-a")
    assert recovery._main(["--approved", str(path), "--run-dir", str(tmp_path / "run")]) == 1
    assert calls == []
