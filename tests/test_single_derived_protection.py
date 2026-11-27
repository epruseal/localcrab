"""단일 SQLite 증분 쓰기의 문서와 파생 행 보호를 확인한다."""

from __future__ import annotations

import contextlib
import json
import sqlite3

import pytest

from opencrab.stores import _node_bm25_sqlite as nbm
from opencrab.stores import _source_fts_sqlite as sfts
from opencrab.stores._single_file import SharedSqliteFile
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore


@pytest.fixture
def guarded_docs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    shared = SharedSqliteFile(str(tmp_path / "derived.db"))
    docs = LocalSQLDocStore(shared.db_path, shared=shared)
    conn = shared.connection()
    conn.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, incarnation TEXT NOT NULL)")
    conn.execute("CREATE TABLE pack_delete_operations (operation_id TEXT, pack_id TEXT, incarnation TEXT, state TEXT)")
    conn.executemany("INSERT INTO packs VALUES (?, ?)", [("p", "first"), ("q", "second")])
    conn.commit()
    assert docs.supports_keyword and docs.supports_node_bm25
    yield docs, shared, conn
    docs.close()


@contextlib.contextmanager
def _unit(shared, *, requested="p", validated=None):
    with shared.unit_of_work() as conn:
        shared.validate_pack_writes(validated or {"p": "first"})
        shared._uow.requested_pack = requested
        yield conn


def _snapshot(conn):
    tables = (
        "doc_nodes", nbm.DOCS, nbm.STATS, nbm.PENDING, f"{nbm.FTS}_docsize",
        nbm.VINST, "doc_sources", "doc_source_integrity", sfts.CONTENT,
        f"{sfts.FTS}_docsize",
    )
    return {table: sorted(tuple(row) for row in conn.execute(f"SELECT * FROM main.{table}"))
            for table in tables}


def _mapping(conn, owner="q", key=1, node="target"):
    conn.execute(
        f"INSERT INTO {nbm.DOCS} VALUES (?, 's', ?, ?, 1, 'old')", (key, node, owner)
    )
    conn.execute(f"INSERT INTO {nbm.FTS}(rowid, tokens) VALUES (?, 'foreign')", (key,))
    conn.execute(f"UPDATE {nbm.STATS} SET doc_count=doc_count+1, total_len=total_len+1")
    conn.commit()


@pytest.mark.parametrize("owner", ["q", ""])
@pytest.mark.parametrize("operation", ["delete", "upsert", "create"])
def test_document_mapping_owner_rejects_without_base(guarded_docs, caplog, owner, operation):
    docs, shared, conn = guarded_docs
    _mapping(conn, owner)
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            if operation == "delete":
                docs.delete_node_doc("s", "target")
            elif operation == "create":
                docs.create_node_doc_if_absent("s", "Document", "target", {"pack_id": "p"})
            else:
                docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p"})
    assert _snapshot(conn) == before
    assert "target" in caplog.text and "s" in caplog.text


@pytest.mark.parametrize("mapping", [False, True])
def test_missing_document_preserves_owned_mapping_and_pending(guarded_docs, mapping):
    docs, shared, conn = guarded_docs
    if mapping:
        _mapping(conn, "p")
    conn.execute(f"INSERT INTO {nbm.PENDING} VALUES ('s', 'target')")
    conn.commit()
    before = _snapshot(conn)
    with _unit(shared):
        assert docs.delete_node_doc("s", "target") is False
    assert _snapshot(conn) == before


@pytest.mark.parametrize("operation", ["node", "source", "metadata", "delete_source"])
def test_request_pack_does_not_use_validation_union(guarded_docs, operation, caplog):
    docs, shared, conn = guarded_docs
    with _unit(shared, requested="q", validated={"q": "second"}):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "q"})
        docs.upsert_source("source-target", "old", {"pack_id": "q"})
    state = docs.read_source_state_exact("source-target")
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared, validated={"p": "first", "q": "second"}):
            if operation == "node":
                docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p"})
            elif operation == "source":
                docs.upsert_source("source-target", "new", {"pack_id": "p"})
            elif operation == "metadata":
                docs.rewrite_source_metadata("source-target", {"pack_id": "p"}, state)
            else:
                docs.delete_sources_with_state(["source-target"])
    assert _snapshot(conn) == before
    assert "target" in caplog.text


@pytest.mark.parametrize("validated, allowed", [({"p": "first"}, True), ({"p": "first", "q": "second"}, False)])
def test_request_fallback_only_uses_single_validated_pack(guarded_docs, validated, allowed):
    docs, shared, conn = guarded_docs
    if allowed:
        with _unit(shared, requested=None, validated=validated):
            docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p"})
        assert conn.execute("SELECT count(*) FROM doc_nodes").fetchone()[0] == 1
    else:
        with pytest.raises(sfts.DerivedRowProtectionError):
            with _unit(shared, requested=None, validated=validated):
                docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p"})
        assert conn.execute("SELECT count(*) FROM doc_nodes").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["docsize", "vocab"])
def test_bm25_new_allocated_rowid_rejects_residue(guarded_docs, kind):
    docs, shared, conn = guarded_docs
    conn.execute(f"INSERT INTO {nbm.FTS}(rowid, tokens) VALUES (1, 'orphan')")
    if kind == "vocab":
        conn.execute(f"DELETE FROM {nbm.FTS}_docsize WHERE id=1")
    conn.commit()
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    assert _snapshot(conn) == before


def test_bm25_clean_old_rowid_reuse_and_unrelated_orphan(guarded_docs):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "old"})
    old = conn.execute(f"SELECT doc_key FROM {nbm.DOCS}").fetchone()[0]
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    assert conn.execute(f"SELECT doc_key FROM {nbm.DOCS}").fetchone()[0] == old
    conn.execute(f"INSERT INTO {nbm.FTS}(rowid, tokens) VALUES (99, 'orphan')")
    conn.commit()
    with _unit(shared):
        assert docs.delete_node_doc("s", "target") is True
    assert conn.execute(f"SELECT doc FROM {nbm.VINST} WHERE doc=99").fetchone() is not None
    assert tuple(conn.execute(f"SELECT doc_count, total_len FROM {nbm.STATS}").fetchone()) == (0, 0)


@pytest.mark.parametrize("operation", ["delete", "upsert"])
def test_bm25_old_fts_residue_rolls_back_whole_unit(guarded_docs, operation, monkeypatch):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "old"})
    before = _snapshot(conn)
    original = nbm._require_empty_rowid

    def leave_residue(connection, rowid):
        connection.execute(f"INSERT INTO {nbm.FTS}(rowid, tokens) VALUES (?, 'residue')", (rowid,))
        original(connection, rowid)

    monkeypatch.setattr(nbm, "_require_empty_rowid", leave_residue)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            docs.upsert_source("earlier", "must roll back", {"pack_id": "p"})
            if operation == "delete":
                docs.delete_node_doc("s", "target")
            else:
                docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    assert _snapshot(conn) == before


def test_bm25_replacement_checks_different_actual_allocated_rowid(guarded_docs):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "old"})
        docs.upsert_node_doc("s", "Document", "keeper", {"pack_id": "p", "text": "keep"})
    conn.execute(f"INSERT INTO {nbm.FTS}(rowid, tokens) VALUES (3, 'orphan')")
    conn.execute(f"DELETE FROM {nbm.FTS}_docsize WHERE id=3")
    conn.commit()
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    assert _snapshot(conn) == before


@pytest.mark.parametrize("operation", ["delete", "upsert"])
def test_bm25_actual_missing_docsize_preserves_old_vocab(guarded_docs, operation):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "oldword"})
    conn.execute(f"DELETE FROM {nbm.FTS}_docsize")
    conn.commit()
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            if operation == "delete":
                docs.delete_node_doc("s", "target")
            else:
                docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    assert _snapshot(conn) == before


def test_nonprotection_reindex_failure_keeps_document_and_pending(guarded_docs, monkeypatch):
    docs, shared, conn = guarded_docs

    def fail(*args, **kwargs):
        raise ValueError("ordinary tokenization failure")

    monkeypatch.setattr(nbm, "node_terms", fail)
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p"})
    assert conn.execute("SELECT count(*) FROM doc_nodes").fetchone()[0] == 1
    assert conn.execute(f"SELECT count(*) FROM {nbm.PENDING}").fetchone()[0] == 1
    assert conn.execute(f"SELECT count(*) FROM {nbm.DOCS}").fetchone()[0] == 0


@pytest.mark.parametrize("derived", ["integrity", "fts", "both"])
@pytest.mark.parametrize("operation", ["delete", "upsert", "metadata"])
def test_missing_source_preserves_derived_rows(guarded_docs, derived, operation, caplog):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "old", {"pack_id": "p"})
    state = docs.read_source_state_exact("source-target")
    conn.execute("DELETE FROM doc_sources WHERE source_id='source-target'")
    if derived == "integrity":
        conn.execute(sfts.delete_sql("= ?"), ("source-target",))
    if derived == "fts":
        conn.execute("DELETE FROM doc_source_integrity WHERE source_id='source-target'")
    conn.commit()
    before = _snapshot(conn)
    if operation == "delete":
        with _unit(shared):
            assert docs.delete_sources_with_state(["source-target"]) == 0
    else:
        with pytest.raises(sfts.DerivedRowProtectionError):
            with _unit(shared):
                if operation == "upsert":
                    docs.upsert_source("source-target", "new", {"pack_id": "p"})
                else:
                    docs.rewrite_source_metadata("source-target", {"pack_id": "p"}, state)
    assert _snapshot(conn) == before
    assert "source-target" in caplog.text
    assert ("derived_protection_rejected" if operation != "delete" else "source_derived_rows_preserved") in caplog.text


@pytest.mark.parametrize("operation", ["delete", "upsert", "metadata"])
def test_source_duplicate_content_rowids_are_all_checked(guarded_docs, operation, monkeypatch):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "old", {"pack_id": "p"})
    state = docs.read_source_state_exact("source-target")
    conn.execute(f"INSERT INTO {sfts.FTS}(rowid, source_id, text) VALUES (2, 'source-target', 'duplicate')")
    conn.commit()
    before = _snapshot(conn)
    original = sfts._rowid_occupied
    checked = []

    def check(connection, vocab, rowid):
        checked.append(rowid)
        if rowid == 2:
            connection.execute(f"INSERT INTO {sfts.FTS}_docsize(id, sz) VALUES (2, X'01')")
        return original(connection, vocab, rowid)

    monkeypatch.setattr(sfts, "_rowid_occupied", check)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            if operation == "delete":
                docs.delete_sources_with_state(["source-target"])
            elif operation == "upsert":
                docs.upsert_source("source-target", "new", {"pack_id": "p"})
            else:
                docs.rewrite_source_metadata("source-target", {"pack_id": "p"}, state)
    assert checked == [1, 2]
    assert _snapshot(conn) == before


@pytest.mark.parametrize("operation", ["upsert", "metadata"])
def test_source_rowid_avoids_vocab_only_residue(guarded_docs, operation):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "old", {"pack_id": "p"})
    state = docs.read_source_state_exact("source-target")
    conn.execute(f"INSERT INTO {sfts.FTS}(rowid, source_id, text) VALUES (2, 'foreign', 'foreignword')")
    conn.execute(f"DELETE FROM {sfts.CONTENT} WHERE id=2")
    conn.execute(f"DELETE FROM {sfts.FTS}_docsize WHERE id=2")
    conn.commit()
    with _unit(shared):
        if operation == "upsert":
            docs.upsert_source("source-target", "new", {"pack_id": "p"})
        else:
            docs.rewrite_source_metadata("source-target", {"pack_id": "p", "tag": "changed"}, state)
    vocab = sfts._protected_vocab(conn)
    assert conn.execute(f"SELECT doc FROM {vocab} WHERE term='foreignword'").fetchone()[0] == 2
    rowid = conn.execute(f"SELECT id FROM {sfts.CONTENT} WHERE c0='source-target'").fetchone()[0]
    assert rowid > 0 and rowid != 2
    assert conn.execute(f"SELECT count(*) FROM {sfts.CONTENT} WHERE c0='source-target'").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["delete", "upsert", "metadata"])
def test_source_missing_docsize_with_clean_delete_remains_supported(guarded_docs, operation):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "oldword", {"pack_id": "p"})
    state = docs.read_source_state_exact("source-target")
    conn.execute(f"DELETE FROM {sfts.FTS}_docsize")
    conn.commit()
    with _unit(shared):
        if operation == "delete":
            assert docs.delete_sources_with_state(["source-target"]) == 1
        elif operation == "upsert":
            docs.upsert_source("source-target", "new", {"pack_id": "p"})
        else:
            docs.rewrite_source_metadata("source-target", {"pack_id": "p"}, state)
    vocab = sfts._protected_vocab(conn)
    if operation == "delete":
        assert conn.execute(f"SELECT doc FROM {vocab}").fetchone() is None
    else:
        assert conn.execute(f"SELECT count(*) FROM {sfts.CONTENT}").fetchone()[0] == 1
        assert docs.read_source_state_exact("source-target") is not None


@pytest.mark.parametrize("operation", ["upsert", "metadata"])
def test_source_rowid_preserves_unrelated_docsize_residue(guarded_docs, operation):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "old", {"pack_id": "p"})
    state = docs.read_source_state_exact("source-target")
    conn.execute(f"INSERT INTO {sfts.FTS}_docsize(id, sz) VALUES (2, X'01')")
    conn.commit()
    with _unit(shared):
        if operation == "upsert":
            docs.upsert_source("source-target", "new", {"pack_id": "p"})
        else:
            docs.rewrite_source_metadata("source-target", {"pack_id": "p"}, state)
    assert conn.execute(f"SELECT sz FROM {sfts.FTS}_docsize WHERE id=2").fetchone()[0] == b"\x01"
    assert conn.execute(f"SELECT id FROM {sfts.CONTENT} WHERE c0='source-target'").fetchone()[0] == 3


def test_source_temp_vocab_always_reads_main_not_temp_fts(guarded_docs):
    docs, shared, conn = guarded_docs
    conn.execute(
        f"CREATE VIRTUAL TABLE temp.{sfts.FTS} USING fts5(source_id UNINDEXED, text)"
    )
    conn.execute(f"INSERT INTO temp.{sfts.FTS}(rowid, source_id, text) VALUES (77, 'decoy', 'decoyword')")
    conn.execute(f"INSERT INTO main.{sfts.FTS}(rowid, source_id, text) VALUES (1, 'foreign', 'mainword')")
    conn.execute(f"DELETE FROM main.{sfts.CONTENT} WHERE id=1")
    conn.execute(f"DELETE FROM main.{sfts.FTS}_docsize WHERE id=1")
    conn.commit()
    with shared.unit_of_work() as tx:
        sfts.insert_protected(tx, "source-target", "newword")
    assert conn.execute(f"SELECT id FROM main.{sfts.CONTENT} WHERE c0='source-target'").fetchone()[0] == 2
    vocab = sfts._protected_vocab(conn)
    assert conn.execute(f"SELECT doc FROM {vocab} WHERE term='mainword'").fetchone()[0] == 1
    assert conn.execute(f"SELECT doc FROM {vocab} WHERE term='decoyword'").fetchone() is None


def test_source_temp_vocab_definition_checked_every_call(guarded_docs):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "old", {"pack_id": "p"})
    conn.execute(f"DROP TABLE temp.{sfts._PROTECTED_VOCAB}")
    conn.execute(f"CREATE TEMP TABLE {sfts._PROTECTED_VOCAB} (doc INTEGER)")
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError, match="derived_protection_rejected") as caught:
        with _unit(shared):
            docs.upsert_source("source-target", "new", {"pack_id": "p"})
    assert "definition" in str(caught.value.__cause__)
    assert _snapshot(conn) == before


@pytest.mark.parametrize("raw", ['{"pack_id":"q","pack_id":"p"}', '{"pack_id":' + '[' * 2000 + '0' + ']' * 2000 + '}'])
@pytest.mark.parametrize("target", ["node", "source"])
def test_owner_guards_reject_duplicate_and_deep_json(guarded_docs, caplog, raw, target):
    docs, shared, conn = guarded_docs
    if target == "node":
        conn.execute("INSERT INTO doc_nodes VALUES ('s', 'target', 'Document', ?, 'old')", (raw,))
    else:
        conn.execute("INSERT INTO doc_sources VALUES ('source-target', 'old', ?, 'old')", (raw,))
    conn.commit()
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            if target == "node":
                docs.delete_node_doc("s", "target")
            else:
                docs.delete_sources_with_state(["source-target"])
    assert _snapshot(conn) == before
    assert "target" in caplog.text


@pytest.mark.parametrize("pack_id, allowed", [(None, True), (False, True), (0, True), ("", True), ([], False), ({}, False), (True, False), (1, False)])
def test_source_owner_fallback_matches_loader_strings(guarded_docs, pack_id, allowed):
    docs, shared, conn = guarded_docs
    conn.execute(
        "INSERT INTO doc_sources VALUES ('source-target', 'old', ?, 'old')",
        (json.dumps({"pack_id": pack_id, "source": "p"}),),
    )
    conn.commit()
    if allowed:
        with _unit(shared):
            assert docs.delete_sources_with_state(["source-target"]) == 1
    else:
        with pytest.raises(sfts.DerivedRowProtectionError):
            with _unit(shared):
                docs.delete_sources_with_state(["source-target"])
        assert conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0] == 1


def test_bm25_verification_failure_bypasses_index_savepoint(guarded_docs, monkeypatch):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "old"})
    before = _snapshot(conn)
    conn.execute(f"DROP TABLE {nbm.VINST}")
    with pytest.raises(sfts.DerivedRowProtectionError):
        with _unit(shared):
            docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    conn.execute(f"CREATE VIRTUAL TABLE {nbm.VINST} USING fts5vocab({nbm.FTS}, 'instance')")
    assert _snapshot(conn) == before


@pytest.mark.parametrize("occupied", [-1, 9223372036854775807])
def test_source_new_rowid_remains_positive_at_integer_bounds(guarded_docs, occupied):
    docs, shared, conn = guarded_docs
    conn.execute(
        f"INSERT INTO {sfts.FTS}(rowid, source_id, text) VALUES (?, 'foreign', 'foreignword')",
        (occupied,),
    )
    conn.commit()
    with _unit(shared):
        docs.upsert_source("source-target", "newword", {"pack_id": "p"})
    assert conn.execute(f"SELECT id FROM {sfts.CONTENT} WHERE c0='source-target'").fetchone()[0] == 1
    assert conn.execute(f"SELECT id FROM {sfts.CONTENT} WHERE c0='foreign'").fetchone()[0] == occupied


def test_successful_reindex_consumes_only_requested_pending_key(guarded_docs):
    docs, shared, conn = guarded_docs
    _mapping(conn, node="foreign-key", key=50)
    conn.execute(f"INSERT INTO {nbm.PENDING} VALUES ('s', 'foreign-key')")
    conn.commit()
    foreign = tuple(conn.execute(f"SELECT * FROM {nbm.DOCS} WHERE node_id='foreign-key'").fetchone())
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    assert [tuple(row) for row in conn.execute(f"SELECT * FROM {nbm.PENDING}")] == [("s", "foreign-key")]
    assert tuple(conn.execute(f"SELECT * FROM {nbm.DOCS} WHERE node_id='foreign-key'").fetchone()) == foreign
    assert tuple(conn.execute(f"SELECT doc_count, total_len FROM {nbm.STATS}").fetchone()) == tuple(
        conn.execute(f"SELECT count(*), sum(dl) FROM {nbm.DOCS}").fetchone()
    )


@pytest.mark.parametrize("operation", ["delete", "upsert", "metadata"])
def test_clean_duplicate_source_rows_are_all_removed(guarded_docs, operation):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "old", {"pack_id": "p"})
    state = docs.read_source_state_exact("source-target")
    conn.execute(f"INSERT INTO {sfts.FTS}(rowid, source_id, text) VALUES (2, 'source-target', 'duplicate')")
    conn.commit()
    with _unit(shared):
        if operation == "delete":
            assert docs.delete_sources_with_state(["source-target"]) == 1
        elif operation == "upsert":
            docs.upsert_source("source-target", "new", {"pack_id": "p"})
        else:
            docs.rewrite_source_metadata("source-target", {"pack_id": "p"}, state)
    assert conn.execute(f"SELECT count(*) FROM {sfts.CONTENT}").fetchone()[0] == (0 if operation == "delete" else 1)
    vocab = sfts._protected_vocab(conn)
    assert conn.execute(f"SELECT 1 FROM {vocab} WHERE term='duplicate'").fetchone() is None


def test_metadata_state_failure_rolls_back_preceding_shared_mutation(guarded_docs):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_source("source-target", "old", {"pack_id": "p"})
    state = docs.read_source_state_exact("source-target")
    before = _snapshot(conn)
    with pytest.raises(sfts.DerivedRowProtectionError, match="derived_protection_rejected") as caught:
        with _unit(shared):
            docs.upsert_node_doc("s", "Document", "earlier", {"pack_id": "p"})
            docs.upsert_source("source-target", "changed", {"pack_id": "p"})
            docs.rewrite_source_metadata("source-target", {"pack_id": "p"}, state)
    assert "state changed" in str(caught.value.__cause__)
    assert _snapshot(conn) == before


def test_caught_protection_exception_still_rolls_back_outer_unit(guarded_docs):
    docs, shared, conn = guarded_docs
    _mapping(conn)
    before = _snapshot(conn)
    with pytest.raises(RuntimeError, match="rollback-only"):
        with _unit(shared):
            docs.upsert_source("earlier", "text", {"pack_id": "p"})
            with pytest.raises(sfts.DerivedRowProtectionError):
                docs.delete_node_doc("s", "target")
    assert _snapshot(conn) == before


class _ConnectionProbe:
    def __init__(self, connection, execute):
        self.connection = connection
        self.execute = execute

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_bm25_second_owner_read_failure_rolls_back_whole_unit(guarded_docs, monkeypatch, caplog):
    docs, shared, conn = guarded_docs
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "old"})
    before = _snapshot(conn)
    failure = sqlite3.OperationalError("owner-read-private-sentinel")
    reads = []

    def execute(sql, parameters=()):
        if sql.startswith(f"SELECT pack_id FROM main.{nbm.DOCS}"):
            reads.append(sql)
            if len(reads) == 2:
                assert json.loads(conn.execute(
                    "SELECT properties FROM doc_nodes WHERE node_id='target'"
                ).fetchone()[0])["text"] == "new"
                raise failure
        return conn.execute(sql, parameters)

    monkeypatch.setattr(shared.local, "conn", _ConnectionProbe(conn, execute))
    with pytest.raises(sfts.DerivedRowProtectionError) as caught:
        with _unit(shared):
            docs.upsert_source("earlier", "roll back", {"pack_id": "p"})
            docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p", "text": "new"})
    assert len(reads) == 2
    causes = []
    cause = caught.value
    while cause is not None:
        causes.append(cause)
        cause = cause.__cause__
    assert failure in causes
    assert "owner-read-private-sentinel" not in caplog.text
    assert "owner-read-private-sentinel" not in str(caught.value)
    assert _snapshot(conn) == before


_QUOTES = [("", ""), ('"', '"'), ("[", "]"), ("`", "`"), ("'", "'")]


@pytest.mark.parametrize("left,right", _QUOTES, ids=["bare", "double", "bracket", "backtick", "single"])
def test_source_vocab_reuses_normal_quoted_declaration(guarded_docs, left, right):
    docs, shared, conn = guarded_docs
    name = sfts._PROTECTED_VOCAB.upper()
    conn.execute(f"CREATE VIRTUAL TABLE temp.{left}{name}{right} "
                 "USING fts5vocab([main], `doc_sources_fts`, 'instance')")
    declared = conn.execute("SELECT sql FROM temp.sqlite_master WHERE name=?", (name,)).fetchone()[0]
    assert name in declared
    conn.execute(f"INSERT INTO main.{sfts.FTS}(rowid, source_id, text) VALUES (41, 'foreign', 'mainword')")
    conn.execute(f"CREATE VIRTUAL TABLE temp.{sfts.FTS} USING fts5(source_id, text)")
    conn.execute(f"INSERT INTO temp.{sfts.FTS}(rowid, source_id, text) VALUES (81, 'decoy', 'decoyword')")
    conn.commit()
    assert conn.execute(f"SELECT doc FROM temp.{name} WHERE term='mainword'").fetchone()[0] == 41
    assert conn.execute(f"SELECT doc FROM temp.{name} WHERE term='decoyword'").fetchone() is None
    conn.execute(f"DROP TABLE temp.{sfts.FTS}")
    conn.commit()
    for text in ("firstword", "secondword"):
        with _unit(shared):
            docs.upsert_source("source-target", text, {"pack_id": "p"})
        assert conn.execute("SELECT sql FROM temp.sqlite_master WHERE name=?", (name,)).fetchone()[0] == declared
        assert sfts.require_protection_vocab(conn) == f"temp.{name}"
        rowid = conn.execute(f"SELECT id FROM main.{sfts.CONTENT} WHERE c0='source-target'").fetchone()[0]
        assert conn.execute(f"SELECT doc FROM temp.{name} WHERE term=?", (text,)).fetchone()[0] == rowid
    assert conn.execute(f"SELECT doc FROM temp.{name} WHERE term='mainword'").fetchone()[0] == 41


@pytest.mark.parametrize("left,right", _QUOTES, ids=["bare", "double", "bracket", "backtick", "single"])
@pytest.mark.parametrize("arguments", [
    "main, 'wrong_target', 'instance'", "main, 'doc_sources_fts', 'row'",
    "temp, 'doc_sources_fts', 'instance'",
])
def test_source_vocab_rejects_wrong_existing_declaration(guarded_docs, left, right, arguments):
    docs, shared, conn = guarded_docs
    name = sfts._PROTECTED_VOCAB.upper()
    conn.execute(f"CREATE VIRTUAL TABLE temp.{left}{name}{right} USING fts5vocab({arguments})")
    assert conn.execute("SELECT type FROM temp.sqlite_master WHERE name=?", (name,)).fetchone()[0] == "table"
    before = _snapshot(conn)
    for _ in range(2):
        with pytest.raises(sfts.DerivedRowProtectionError):
            sfts.require_protection_vocab(conn)
        with pytest.raises(sfts.DerivedRowProtectionError):
            with _unit(shared):
                docs.upsert_source("source-target", "new", {"pack_id": "p"})
    assert _snapshot(conn) == before
    assert [row[0] for row in conn.execute("SELECT name FROM temp.sqlite_master")] == [name]


@pytest.mark.parametrize("declaration", [
    "CREATE VIRTUAL TABLE source_fts_protection_vinst USING fts5vocab(main, 'doc_sources_fts', 'instance', 'extra')",
    "CREATE VIRTUAL TABLE source_fts_protection_vinst USING fts5vocab(main, 'doc_sources_fts', 'instance'); SELECT 1",
    "CREATE VIRTUAL TABLE wrong_name USING fts5vocab(main, 'doc_sources_fts', 'instance')",
])
def test_source_vocab_definition_rejects_extra_tokens(guarded_docs, declaration):
    docs, shared, conn = guarded_docs
    name = sfts._PROTECTED_VOCAB
    conn.execute(f"CREATE VIRTUAL TABLE temp.{name} USING fts5vocab(main, '{sfts.FTS}', 'instance')")

    class _Cursor:
        def fetchall(self):
            return [(name, "table", declaration)]

    def execute(sql, parameters=()):
        if "FROM temp.sqlite_master" in sql:
            return _Cursor()
        return conn.execute(sql, parameters)

    with pytest.raises(sfts.DerivedRowProtectionError):
        sfts.require_protection_vocab(_ConnectionProbe(conn, execute))


def test_source_vocab_selection_avoids_main_ascii_collisions_without_creation(guarded_docs):
    docs, shared, conn = guarded_docs
    for name in (sfts._PROTECTED_VOCAB.upper(), sfts._PROTECTED_VOCAB + "_1"):
        conn.execute(f"CREATE TABLE main.{name}(value TEXT)")
        conn.execute(f"INSERT INTO main.{name} VALUES ('keep')")
    conn.commit()
    selected = sfts.require_protection_vocab(conn)
    assert selected == f"temp.{sfts._PROTECTED_VOCAB}_2"
    assert conn.execute("SELECT count(*) FROM temp.sqlite_master").fetchone()[0] == 0
    for text in ("firstword", "secondword"):
        with _unit(shared):
            docs.upsert_source("source-target", text, {"pack_id": "p"})
        assert sfts.require_protection_vocab(conn) == selected
        assert conn.execute(f"SELECT doc FROM {selected} WHERE term=?", (text,)).fetchone() is not None
        assert conn.execute(f"SELECT value FROM {sfts._PROTECTED_VOCAB}").fetchone()[0] == "keep"
    conn.execute(f"DROP TABLE {selected}")
    conn.execute(f"CREATE TEMP TABLE {sfts._PROTECTED_VOCAB}_2(doc INTEGER)")
    with pytest.raises(sfts.DerivedRowProtectionError):
        sfts.require_protection_vocab(conn)
    assert conn.execute("SELECT count(*) FROM temp.sqlite_master").fetchone()[0] == 1


def test_source_vocab_does_not_shadow_actual_vector_collection(guarded_docs):
    import sqlite_vec

    docs, shared, conn = guarded_docs
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    name = sfts._PROTECTED_VOCAB
    conn.execute(f"CREATE VIRTUAL TABLE {name} USING vec0(embedding float[2])")
    conn.execute(f"INSERT INTO {name}(rowid, embedding) VALUES (17, ?)",
                 (sqlite_vec.serialize_float32([0.25, 0.5]),))
    conn.commit()
    before = [tuple(row) for row in conn.execute(f"SELECT rowid, embedding FROM main.{name}")]
    for text in ("firstword", "secondword"):
        with _unit(shared):
            docs.upsert_source("source-target", text, {"pack_id": "p"})
        assert [tuple(row) for row in conn.execute(f"SELECT rowid, embedding FROM {name}")] == before
        assert sfts.require_protection_vocab(conn) == f"temp.{name}_1"
        assert conn.execute(f"SELECT doc FROM temp.{name}_1 WHERE term=?", (text,)).fetchone() is not None


@pytest.mark.parametrize("origin", ["decoder", "validator"])
def test_shared_source_depth_classification_preserves_origin(guarded_docs, monkeypatch, origin):
    from opencrab.common import graph_identity

    docs, shared, conn = guarded_docs
    conn.execute("INSERT INTO doc_sources VALUES ('target', 'body', '{\"pack_id\":\"p\"}', 'old')")
    conn.commit()
    failure = RecursionError("depth-private-sentinel")
    reads = []
    original = graph_identity.json.loads

    def decode(value, *args, **kwargs):
        reads.append(value)
        if origin == "decoder":
            raise failure
        return original(value, *args, **kwargs)

    def validate(*args, **kwargs):
        raise failure

    monkeypatch.setattr(graph_identity.json, "loads", decode)
    if origin == "validator":
        monkeypatch.setattr(graph_identity, "_validate_json", validate)
    expected = graph_identity.PropertyDecodeDepthError if origin == "decoder" else RecursionError
    with pytest.raises(expected) as caught:
        docs.get_source("target")
    if origin == "decoder":
        assert caught.value.__cause__ is failure
    else:
        assert caught.value is failure
    assert reads == ['{"pack_id":"p"}']


@pytest.mark.parametrize("raw", ['{"pack_id":', '{"pack_id":"p","pack_id":"q"}', '[]'])
def test_shared_source_corrupt_metadata_uses_one_decoder_read(guarded_docs, monkeypatch, raw):
    from opencrab.common import graph_identity

    docs, shared, conn = guarded_docs
    conn.execute("INSERT INTO doc_sources VALUES ('target', 'body', ?, 'old')", (raw,))
    conn.commit()
    original = graph_identity.json.loads
    reads = []

    def decode(value, *args, **kwargs):
        reads.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(graph_identity.json, "loads", decode)
    source = docs.get_source("target")
    assert source == {"source_id": "target", "text": "body", "metadata": {},
                      "ingested_at": "old", "property_decode_error": True}
    assert reads == [raw]


@pytest.mark.parametrize("probe", ["_read_only_fts_ok", "_read_only_node_bm25_ok"])
@pytest.mark.parametrize("safe_log", [False, True])
def test_capability_probe_safe_log_preserves_result(guarded_docs, caplog, probe, safe_log):
    docs, shared, conn = guarded_docs
    failure = sqlite3.OperationalError("capability-private-sentinel")

    def fail(*args, **kwargs):
        raise failure

    assert getattr(docs, probe)(conn, safe_log=safe_log) is True
    assert getattr(docs, probe)(_ConnectionProbe(conn, fail), safe_log=safe_log) is False
    assert ("capability-private-sentinel" in caplog.text) is (not safe_log)


def test_guarded_safe_log_keeps_general_bm25_savepoint_contract(guarded_docs, monkeypatch, caplog):
    docs, shared, conn = guarded_docs
    failure = ValueError("bm25-private-sentinel")
    statements = []

    def execute(sql, parameters=()):
        statements.append(sql)
        return conn.execute(sql, parameters)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(shared.local, "conn", _ConnectionProbe(conn, execute))
    monkeypatch.setattr(nbm, "node_terms", fail)
    with shared.unit_of_work(safe_log=True):
        shared.validate_pack_writes({"p": "first"})
        shared._uow.requested_pack = "p"
        docs.upsert_node_doc("s", "Document", "target", {"pack_id": "p"})
    assert conn.execute("SELECT count(*) FROM doc_nodes").fetchone()[0] == 1
    assert conn.execute(f"SELECT count(*) FROM {nbm.PENDING}").fetchone()[0] == 1
    assert conn.execute(f"SELECT count(*) FROM {nbm.DOCS}").fetchone()[0] == 0
    assert "SAVEPOINT node_bm25" in statements
    assert "ROLLBACK TO SAVEPOINT node_bm25" in statements
    assert "RELEASE SAVEPOINT node_bm25" in statements
    assert "bm25-private-sentinel" not in caplog.text
    assert any("bm25_sync_failed" in record.getMessage() for record in caplog.records)


def test_bm25_stats_match_base_documents_not_other_derived_rows(guarded_docs):
    from opencrab.ontology.bm25 import max_text_chars_setting

    docs, shared, conn = guarded_docs
    for owner, incarnation in (("p", "first"), ("q", "second")):
        with _unit(shared, requested=owner, validated={owner: incarnation}):
            docs.upsert_node_doc("s", "Document", owner, {"pack_id": owner, "text": "one two three"})
    with _unit(shared):
        docs.upsert_node_doc("s", "Document", "p", {"pack_id": "p", "text": "replacement with four tokens"})
        docs.upsert_node_doc("s", "Document", "extra", {"pack_id": "p", "label": "extra"})
    rows = conn.execute("SELECT space, node_id, node_type, properties, updated_at FROM doc_nodes").fetchall()
    expected = (len(rows), sum(len(nbm.node_terms(docs._row_to_node(row), max_text_chars_setting())) for row in rows))
    actual = tuple(conn.execute(f"SELECT doc_count, total_len FROM {nbm.STATS}").fetchone())
    assert actual == expected
    conn.execute(f"UPDATE {nbm.DOCS} SET dl=dl+1")
    conn.execute(f"UPDATE {nbm.STATS} SET total_len=total_len+?", (len(rows),))
    assert tuple(conn.execute(f"SELECT doc_count, total_len FROM {nbm.STATS}").fetchone()) == tuple(
        conn.execute(f"SELECT count(*), sum(dl) FROM {nbm.DOCS}").fetchone()
    )
    assert tuple(conn.execute(f"SELECT doc_count, total_len FROM {nbm.STATS}").fetchone()) != expected
    conn.rollback()
