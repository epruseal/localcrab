from __future__ import annotations

import contextlib
import json

import pytest

from scripts import recover_source_vectors as recovery


class FakeReader:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_source(self, ident):
        self.calls.append(ident)
        return self.rows.get(ident)


class FakeVector:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_by_id(self, ident):
        value = self.rows.get(ident)
        return None if value is None else {"document": value["document"], "metadata": dict(value["metadata"])}

    def upsert_texts(self, *, texts, ids, metadatas):
        self.calls.append((list(texts), list(ids), [dict(x) for x in metadatas]))
        for text, ident, meta in zip(texts, ids, metadatas):
            self.rows[ident] = {"document": text, "metadata": dict(meta)}


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
    manifest = recovery.plan(approved(tmp_path, "id-a"), tmp_path / "run", reader, vec, object(), object())
    assert manifest["targets"][0]["state"] == "planned"
    assert vec.calls == []
    assert reader.calls == ["id-a"]
    assert len(auth) == 1


def test_apply_requires_matching_digests_and_rewrites_one_target(tmp_path, auth):
    reader = FakeReader({"id-a": source(text="authority", category="x")})
    vec = FakeVector(vector(text="wrong", category="old"))
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), object())
    def backup(_dest):
        return {"verified": True, "set": "synthetic"}

    result = recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", backup, contextlib.nullcontext)
    assert result["targets"][0]["state"] == "verified"
    assert vec.rows["id-a"] == {"document": "authority", "metadata": {"pack_id": "pack-a", "category": "x"}}
    assert len(vec.calls) == 1
    assert len(auth) == 2


def test_apply_refuses_source_drift_before_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source(text="first")})
    vec = FakeVector(vector())
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), object())
    reader.rows["id-a"] = source(text="changed")
    with pytest.raises(recovery.RecoveryError, match="changed after dry-run"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert vec.calls == []


def test_owner_mismatch_refuses_without_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector(pack="pack-b"))
    with pytest.raises(recovery.RecoveryError, match="owner"):
        recovery.plan(approved(tmp_path, "id-a"), tmp_path / "run", reader, vec, object(), object())
    assert vec.calls == []


def test_decode_error_refuses_without_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": recovery.SourceRow("id-a", "x", {}, True)})
    vec = FakeVector(vector())
    with pytest.raises(recovery.RecoveryError, match="malformed"):
        recovery.plan(approved(tmp_path, "id-a"), tmp_path / "run", reader, vec, object(), object())
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
    recovery.plan(file, tmp_path / "run", reader, vec, object(), object())
    entered = []
    def lock():
        entered.append(True)
        return contextlib.nullcontext()
    with pytest.raises(recovery.RecoveryError, match="backup"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", lambda _: {"verified": False}, lock)
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
    recovery.plan(file, tmp_path / "run", reader, vec, object(), object())
    def backup(_): return {"verified": True}
    recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", backup, contextlib.nullcontext)
    vec.rows["id-a"]["document"] = "bad"
    with pytest.raises(recovery.RecoveryError, match="readback"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", backup, contextlib.nullcontext)


def test_apply_writes_each_approved_target_once_in_order(tmp_path, auth):
    reader = FakeReader({"id-a": source("id-a", "a"), "id-b": source("id-b", "b")})
    vec = FakeVector({**vector("id-a", "old-a"), **vector("id-b", "old-b")})
    file = approved(tmp_path, "id-a", "id-b")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), object())
    def backup(_): return {"verified": True}
    recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", backup, contextlib.nullcontext)
    assert [call[1][0] for call in vec.calls] == ["id-a", "id-b"]
    assert reader.calls == ["id-a", "id-b", "id-a", "id-b"]


def test_apply_refuses_vector_drift_before_upsert(tmp_path, auth):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector(text="old"))
    file = approved(tmp_path, "id-a")
    recovery.plan(file, tmp_path / "run", reader, vec, object(), object())
    vec.rows["id-a"]["document"] = "external-change"
    with pytest.raises(recovery.RecoveryError, match="changed after dry-run"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert vec.calls == []


def test_apply_authorization_failure_has_zero_upserts(tmp_path, monkeypatch):
    reader = FakeReader({"id-a": source()})
    vec = FakeVector(vector())
    file = approved(tmp_path, "id-a")
    monkeypatch.setattr(recovery, "authorize", lambda *_args: None)
    recovery.plan(file, tmp_path / "run", reader, vec, object(), object())
    def denied(*_args):
        raise PermissionError("denied")
    monkeypatch.setattr(recovery, "authorize", denied)
    with pytest.raises(PermissionError, match="denied"):
        recovery.apply(file, tmp_path / "run", reader, vec, object(), object(), tmp_path / "backup", lambda _: {"verified": True}, contextlib.nullcontext)
    assert vec.calls == []
