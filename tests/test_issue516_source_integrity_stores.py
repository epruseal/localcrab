"""Focused store controls for issue #516 source integrity."""

from __future__ import annotations

import hashlib
import json
import math
import threading

import pytest


@pytest.fixture
def local_store(tmp_path):
    from opencrab.stores.local_doc_store import LocalDocStore

    return LocalDocStore(str(tmp_path / "local"))


@pytest.fixture
def sql_store(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "docs.db"))


def test_local_new_write_persists_full_text_and_hides_integrity(local_store):
    text = "x" * 4096
    local_store.upsert_source("source", text, {"pack_id": "pack"})

    assert local_store.get_source("source")["text"] == text
    assert "source_integrity" not in local_store.get_source("source")
    state = local_store.read_source_state_exact("source")
    assert state is not None and state.complete
    assert state.text_digest == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_local_legacy_exact_slice_is_unknown_and_short_text_is_complete(local_store):
    data = {
        "slice": {"source_id": "slice", "text": "x" * 4096, "metadata": {}},
        "short": {"source_id": "short", "text": "short", "metadata": {}},
    }
    local_store._save("sources", data)

    assert local_store.read_source_state_exact("slice").complete is False
    assert local_store.read_source_state_exact("short").complete is True


def test_local_metadata_cannot_forge_integrity_and_exact_scope_is_strict(local_store):
    forged = {"version": 1, "state": "complete", "text_digest": "0" * 64}
    local_store.upsert_source("source", "text", {"pack_id": "pack", "source_integrity": forged})

    assert local_store.read_source_state_exact("source").complete is True
    scoped = local_store.get_source_scoped_exact("source", ["pack"])
    assert scoped["source_id"] == "source"
    assert scoped["source_integrity"]["state"] == "complete"
    assert local_store.get_source_scoped_exact("source", ["other"]) is None
    local_store.upsert_source("fallback", "text", {"source": "pack"})
    assert local_store.get_source_scoped_exact("fallback", ["pack"]) is None


def test_local_explicit_unknown_stays_unknown_through_metadata_rewrite(local_store):
    local_store._save(
        "sources",
        {
            "source": {
                "source_id": "source",
                "text": "text",
                "metadata": {},
                "source_integrity": {"version": 1, "state": "unknown"},
            }
        },
    )
    state = local_store.read_source_state_exact("source")
    local_store.rewrite_source_metadata("source", {"pack_id": "pack"}, state)

    assert local_store.read_source_state_exact("source").complete is False


def test_local_json_rewrite_rejects_state_change_inside_lock(local_store):
    local_store.upsert_source("source", "old", {"pack_id": "pack"})
    state = local_store.read_source_state_exact("source")
    assert state is not None
    local_store.upsert_source("source", "new", {"pack_id": "pack"})

    with pytest.raises(ValueError, match="source state changed"):
        local_store.rewrite_source_metadata("source", {"pack_id": "changed"}, state)

    assert local_store.get_source("source")["text"] == "new"


def test_local_json_rewrite_serializes_normal_writer(local_store, monkeypatch):
    local_store.upsert_source("source", "old", {"pack_id": "pack"})
    state = local_store.read_source_state_exact("source")
    assert state is not None
    entered = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    original_save = local_store._save

    def hold_rewrite(name, data):
        if name == "sources" and data["source"]["metadata"].get("pack_id") == "rewrite":
            entered.set()
            assert release.wait(2)
        original_save(name, data)

    monkeypatch.setattr(local_store, "_save", hold_rewrite)
    rewrite = threading.Thread(
        target=local_store.rewrite_source_metadata,
        args=("source", {"pack_id": "rewrite"}, state),
    )
    writer = threading.Thread(
        target=lambda: (local_store.upsert_source("source", "latest", {"pack_id": "pack"}), writer_done.set()),
    )
    rewrite.start()
    assert entered.wait(2)
    writer.start()
    assert not writer_done.wait(0.05)
    release.set()
    rewrite.join(2)
    writer.join(2)
    assert not rewrite.is_alive() and not writer.is_alive()
    assert writer_done.is_set()
    assert local_store.get_source("source")["text"] == "latest"


def test_sql_write_hides_integrity_and_exact_scope_is_strict(sql_store):
    sql_store.upsert_source("source", "full text", {"pack_id": "pack"})

    source = sql_store.get_source("source")
    assert source["text"] == "full text"
    assert "source_integrity" not in source
    scoped = sql_store.get_source_scoped_exact("source", ["pack"])
    assert scoped["source_id"] == "source"
    assert scoped["source_integrity"]["state"] == "complete"
    assert sql_store.get_source_scoped_exact("source", ["other"]) is None


def test_sql_explicit_unknown_is_sticky_and_public_shape_omits_it(sql_store):
    sql_store._exec_write(
        "INSERT INTO doc_sources(source_id, text, metadata, ingested_at) "
        "VALUES (:source_id, :text, :metadata, :ingested_at)",
        {
            "source_id": "source",
            "text": "text",
            "metadata": json.dumps({"pack_id": "pack"}),
            "ingested_at": "2026-01-01T00:00:00+00:00",
        },
    )
    sql_store._exec_write(
        "INSERT INTO doc_source_integrity(source_id, source_integrity) "
        "VALUES (:source_id, :source_integrity)",
        {
            "source_id": "source",
            "source_integrity": json.dumps({"version": 1, "state": "unknown"}),
        },
    )

    state = sql_store.read_source_state_exact("source")
    assert state is not None and state.complete is False
    sql_store.rewrite_source_metadata("source", {"pack_id": "pack"}, state)
    assert sql_store.read_source_state_exact("source").complete is False
    assert "source_integrity" not in sql_store.get_source("source")


def test_sql_legacy_positional_row_has_no_helper_state(sql_store):
    sql_store._exec_write(
        "INSERT INTO doc_sources VALUES ('legacy', 'text', '{}', '2026-01-01')", {}
    )

    state = sql_store.read_source_state_exact("legacy")
    assert state is not None and state.complete is True


@pytest.mark.parametrize(
    ("case", "record_change", "scope", "expected_id", "expected_text"),
    [
        ("complete", {}, {"pack"}, "source", "text"),
        ("legacy", {"_source_classification": {"state_kind": "legacy_absent"}}, {"pack"}, "source", "text"),
        ("row-id", {"source_id": "other"}, {"pack"}, "source", "text"),
        ("classification-id", {"_source_classification": {"source_id": "other"}}, {"pack"}, "source", "text"),
        ("expected-id", {}, {"pack"}, "other", "text"),
        ("row-text", {"text": "other"}, {"pack"}, "source", "text"),
        ("classification-text", {"_source_classification": {"text": "other"}}, {"pack"}, "source", "text"),
        ("expected-text", {}, {"pack"}, "source", "other"),
        ("metadata-pack", {"metadata": {"pack_id": "other"}}, {"pack"}, "source", "text"),
        ("classification-pack", {"_source_classification": {"pack_id": "other"}}, {"pack"}, "source", "text"),
        ("scope-out", {}, {"other"}, "source", "text"),
        ("scope-string", {}, "pack", "source", "text"),
        ("scope-mapping", {}, {"pack": True}, "source", "text"),
        ("scope-nonstring", {}, {1}, "source", "text"),
        ("digest", {"_source_classification": {"text_digest": "0" * 64}}, {"pack"}, "source", "text"),
        ("complete-equals", {"_source_classification": {"complete": 1}}, {"pack"}, "source", "text"),
        ("unknown", {"_source_classification": {"complete": True, "state_kind": "unknown"}}, {"pack"}, "source", "text"),
        ("malformed", {"_source_classification": {"complete": True, "state_kind": "malformed"}}, {"pack"}, "source", "text"),
        ("unsupported", {"_source_classification": {"complete": True, "state_kind": "unsupported_version"}}, {"pack"}, "source", "text"),
        ("present-null", {"_source_classification": {"complete": True, "state_kind": "present_null"}}, {"pack"}, "source", "text"),
        ("absent", {"_source_classification": {"complete": True, "state_kind": "absent"}}, {"pack"}, "source", "text"),
        ("state-kind-list", {"_source_classification": {"state_kind": []}}, {"pack"}, "source", "text"),
        ("state-kind-mapping", {"_source_classification": {"state_kind": {}}}, {"pack"}, "source", "text"),
    ],
)
def test_validate_source_row_payload(case, record_change, scope, expected_id, expected_text):
    from opencrab.stores._source_integrity import (
        strict_utf8_digest,
        validate_source_row_payload,
    )

    text = "text"
    digest = strict_utf8_digest(text)
    assert digest is not None
    record = {
        "source_id": "source",
        "text": text,
        "metadata": {"pack_id": "pack"},
        "_source_classification": {
            "source_id": "source",
            "text": text,
            "pack_id": "pack",
            "text_digest": digest,
            "complete": True,
            "state_kind": "complete",
        },
    }
    for key, value in record_change.items():
        if key == "_source_classification":
            record[key].update(value)
        else:
            record[key] = value

    result = validate_source_row_payload(
        record,
        permitted_packs=scope,
        expected_source_id=expected_id,
        expected_text=expected_text,
    )
    if case in {"complete", "legacy"}:
        assert result == ("source", "pack", text, digest)
    else:
        assert result is None, case


@pytest.mark.parametrize("raw", ["[]", "null", "1"])
def test_local_sources_non_object_stays_corrupt_and_blocks_write(local_store, raw):
    from opencrab.stores.local_doc_store import CorruptCollectionError

    path = local_store._collection_path("sources")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(raw)
    before = open(path, "rb").read()

    with pytest.raises(CorruptCollectionError):
        local_store.get_source("source")
    with pytest.raises(CorruptCollectionError):
        local_store.upsert_source("source", "text", {"pack_id": "pack"})

    assert open(path, "rb").read() == before


def test_local_helper_strictness_preserves_metadata_nonfinite(local_store):
    from opencrab.stores._source_integrity import strict_utf8_digest

    text = "text"
    local_store._collection_path("sources")
    path = local_store._collection_path("sources")
    path_data = {
        "normal": {
            "source_id": "normal",
            "text": text,
            "metadata": {"pack_id": "pack", "value": float("nan")},
            "source_integrity": {
                "version": 1,
                "state": "complete",
                "text_digest": strict_utf8_digest(text),
            },
        },
        "bad-helper": {
            "source_id": "bad-helper",
            "text": text,
            "metadata": {"pack_id": "pack"},
            "source_integrity": {"version": 1, "state": "complete", "text_digest": float("nan")},
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(path_data, handle, allow_nan=True)

    store_row = local_store.get_source("normal")
    assert store_row is not None
    assert math.isnan(store_row["metadata"]["value"])
    assert local_store.read_source_state_exact("normal").complete is True
    assert local_store.read_source_state_exact("bad-helper").complete is False


def test_sql_scoped_exact_rejects_decoded_metadata_without_owned_pack(sql_store):
    sql_store.upsert_source("source", "text", {"pack_id": "pack"})
    sql_store._exec_write(
        "UPDATE doc_sources SET metadata=:metadata WHERE source_id=:source_id",
        {"source_id": "source", "metadata": '{"pack_id":"pack","pack_id":"pack"}'},
    )

    assert sql_store.get_source_scoped_exact("source", ["pack"]) is None


def test_local_sql_delete_reuse_has_no_stale_helper(sql_store):
    sql_store.upsert_source("source", "first", {"pack_id": "pack"})
    assert sql_store.delete_sources_with_state(["source"]) == 1
    assert sql_store._fetch_one(
        "SELECT source_id FROM doc_source_integrity WHERE source_id=:source_id",
        {"source_id": "source"},
    ) is None

    sql_store._exec_write(
        "INSERT INTO doc_sources VALUES ('source', 'legacy', '{}', '2026-01-01')", {}
    )
    state = sql_store.read_source_state_exact("source")
    assert state is not None and state.complete is True


def test_sql_integrity_write_rolls_back_with_fts(sql_store):
    if not sql_store._fts_ok:
        pytest.skip("SQLite FTS5 is unavailable")
    real_conn = sql_store._conn

    class FailFts:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            if "INSERT INTO doc_sources_fts" in sql:
                raise RuntimeError("forced FTS failure")
            return self.conn.execute(sql) if params is None else self.conn.execute(sql, params)

        def commit(self):
            return self.conn.commit()

        def rollback(self):
            return self.conn.rollback()

    sql_store._local.conn = FailFts(real_conn)
    try:
        with pytest.raises(RuntimeError, match="forced FTS failure"):
            sql_store.upsert_source("source", "text", {"pack_id": "pack"})
    finally:
        sql_store._local.conn = real_conn
    assert sql_store.get_source("source") is None

@pytest.mark.parametrize(
    ("case", "duplicate_key", "helper"),
    [
        ("version", "version", '{"version": 0, "version": 1, "state": "complete", "text_digest": "DIGEST"}'),
        ("state", "state", '{"version": 1, "state": "unknown", "state": "complete", "text_digest": "DIGEST"}'),
        ("text_digest", "text_digest", '{"version": 1, "state": "complete", "text_digest": "0", "text_digest": "DIGEST"}'),
        ("same-value", "version", '{"version": 1, "version": 1, "state": "complete", "text_digest": "DIGEST"}'),
        ("helper-key", "source_integrity", '{"source_integrity": {"version": 1, "state": "unknown"}, "source_integrity": {"version": 1, "state": "complete", "text_digest": "DIGEST"}}'),
        ("nested", "key", '{"version": 1, "state": "complete", "text_digest": "DIGEST", "extra": {"key": 1, "key": 2}}'),
    ],
)
def test_local_json_duplicate_integrity_file_never_promotes(tmp_path, case, duplicate_key, helper):
    from opencrab.stores._source_integrity import strict_utf8_digest
    from opencrab.stores.local_doc_store import LocalDocStore

    def duplicate_keys(value):
        if not isinstance(value, list):
            return set()
        if all(isinstance(item, tuple) and len(item) == 2 for item in value):
            keys = [key for key, _item in value]
            return {key for key in keys if keys.count(key) > 1} | {
                key
                for _key, item in value
                for key in duplicate_keys(item)
            }
        return {key for item in value for key in duplicate_keys(item)}

    data_dir = tmp_path / "local"
    data_dir.mkdir()
    text = "duplicate target"
    digest = strict_utf8_digest(text)
    normal = {
        "source_id": "normal",
        "text": "normal",
        "metadata": {"pack_id": "pack"},
        "source_integrity": {
            "version": 1,
            "state": "complete",
            "text_digest": strict_utf8_digest("normal"),
        },
    }
    assert digest is not None
    assert normal["source_integrity"]["text_digest"] is not None
    helper_value = helper.replace("DIGEST", digest)
    duplicate_fields = [
        json.dumps("source_id") + ": " + json.dumps("duplicate"),
        json.dumps("text") + ": " + json.dumps(text),
        json.dumps("metadata") + ": " + json.dumps({"pack_id": "pack"}),
    ]
    if case == "helper-key":
        duplicate_fields.extend(
            (
                json.dumps("source_integrity") + ": " + json.dumps({"version": 1, "state": "unknown"}),
                json.dumps("source_integrity") + ": " + json.dumps({
                    "version": 1,
                    "state": "complete",
                    "text_digest": digest,
                }),
            )
        )
    else:
        duplicate_fields.append(json.dumps("source_integrity") + ": " + helper_value)
    duplicate = ", ".join(duplicate_fields)
    raw = "{" + json.dumps("duplicate") + ": {" + duplicate + "}, " + json.dumps("normal") + ": " + json.dumps(normal) + "}"
    assert json.loads(raw)["normal"] == normal
    parsed_pairs = json.loads(raw, object_pairs_hook=lambda pairs: pairs)
    pairs_by_key = dict(parsed_pairs)
    assert duplicate_key in duplicate_keys(pairs_by_key["duplicate"]), case
    assert duplicate_keys(pairs_by_key["normal"]) == set()

    source_path = data_dir / "sources.json"
    source_path.write_text(raw, encoding="utf-8")
    before = source_path.read_bytes()
    store = LocalDocStore(str(data_dir))
    state = store.read_source_state_exact("duplicate")
    assert state is not None and state.complete is False, case
    scoped = store.get_source_scoped_exact("duplicate", ["pack"])
    assert scoped is not None
    assert scoped["source_integrity"] == {"version": 1, "state": "unknown"}
    assert scoped["_source_classification"]["state_kind"] == "unknown"
    assert scoped["_source_classification"]["complete"] is False
    assert store.read_source_state_exact("normal").complete is True
    public_rows = [store.get_source("normal"), *store.list_sources()]
    for row in public_rows:
        assert isinstance(row, dict)
        assert type(row["metadata"]) is dict
    assert source_path.read_bytes() == before
