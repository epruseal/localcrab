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
    text = "x" * 5000
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
        pytest.param("version", "version", '{"version": 0, "version": 1, "state": "complete", "text_digest": "DIGEST"}', id="version"),
        pytest.param("state", "state", '{"version": 1, "state": "unknown", "state": "complete", "text_digest": "DIGEST"}', id="state"),
        pytest.param("text_digest", "text_digest", '{"version": 1, "state": "complete", "text_digest": "0", "text_digest": "DIGEST"}', id="text_digest"),
        pytest.param("same-value", "version", '{"version": 1, "version": 1, "state": "complete", "text_digest": "DIGEST"}', id="same-value"),
        pytest.param("helper-key", "source_integrity", '{"source_integrity": {"version": 1, "state": "unknown"}, "source_integrity": {"version": 1, "state": "complete", "text_digest": "DIGEST"}}', id="helper-key"),
        pytest.param("nested", "key", '{"version": 1, "state": "complete", "text_digest": "DIGEST", "extra": {"key": 1, "key": 2}}', id="nested"),
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


_VALID_HELPER = '{"version": 1, "state": "complete", "text_digest": "DIGEST"}'


@pytest.mark.parametrize(
    ("backend", "helper", "text_length", "state_kind", "complete"),
    [
        pytest.param("local", _VALID_HELPER, 100, "complete", True, id="local-complete-valid"),
        pytest.param("local", '{"version": 1, "state": "unknown"}', 100, "unknown", False, id="local-unknown-valid"),
        pytest.param("local", "null", 100, "present_null", False, id="local-present-null"),
        pytest.param("local", _VALID_HELPER.replace('"version": 1', '"version": true'), 100, "malformed", False, id="local-version-bool"),
        pytest.param("local", _VALID_HELPER.replace('"version": 1', '"version": 1.0'), 100, "malformed", False, id="local-version-float"),
        pytest.param("local", _VALID_HELPER.replace('"version": 1', '"version": 1e0'), 100, "malformed", False, id="local-version-1e0"),
        pytest.param("local", _VALID_HELPER.replace('"version": 1', '"version": 2'), 100, "unsupported_version", False, id="local-version-unsupported"),
        pytest.param("local", _VALID_HELPER.replace("}", ', "extra": 1}'), 100, "malformed", False, id="local-keyset-complete-extra"),
        pytest.param("local", '{"version": 1, "state": "unknown", "extra": 1}', 100, "malformed", False, id="local-keyset-unknown-extra"),
        pytest.param("local", '{"version": 1, "state": "complete"}', 100, "malformed", False, id="local-keyset-missing-digest"),
        pytest.param("local", _VALID_HELPER.replace("DIGEST", "DIGESTUPPER"), 100, "complete", False, id="local-digest-uppercase"),
        pytest.param("local", _VALID_HELPER.replace("DIGEST", "DIGESTPREFIX"), 100, "complete", False, id="local-digest-prefix"),
        pytest.param("local", _VALID_HELPER.replace("DIGEST", "DIGESTOTHER"), 100, "complete", False, id="local-digest-mismatch"),
        pytest.param("local", '{"version": 1, "state": "complete", "text_digest": NaN}', 100, "unknown", False, id="local-helper-nonfinite"),
        pytest.param("local", None, 100, "legacy_absent", True, id="local-legacy-short"),
        pytest.param("local", None, 4095, "legacy_absent", True, id="local-legacy-4095"),
        pytest.param("local", None, 4096, "absent", False, id="local-legacy-4096"),
        pytest.param("local", None, 4097, "absent", False, id="local-legacy-4097"),
        pytest.param("sql", _VALID_HELPER, 100, "complete", True, id="sql-complete-valid"),
        pytest.param("sql", '{"version":0,"version":1,"state":"complete","text_digest":"DIGEST"}', 100, "unknown", False, id="sql-duplicate-version"),
        pytest.param("sql", "{not json", 100, "malformed", False, id="sql-bad-json"),
        pytest.param("sql", '"complete"', 100, "malformed", False, id="sql-json-string-scalar"),
        pytest.param("sql", '{"version":1,"state":"complete","text_digest":NaN}', 100, "unknown", False, id="sql-nonfinite"),
        pytest.param("sql", None, 100, "legacy_absent", True, id="sql-helper-absent-legacy"),
    ],
)
def test_strict_integrity_decoder_matrix(tmp_path, backend, helper, text_length, state_kind, complete):
    from opencrab.stores._source_integrity import strict_utf8_digest

    text = "x" * text_length
    digest = strict_utf8_digest(text)
    other_digest = strict_utf8_digest("other text")
    assert digest is not None and other_digest is not None
    if helper is not None:
        helper = (
            helper.replace("DIGESTUPPER", digest.upper())
            .replace("DIGESTPREFIX", digest[:8])
            .replace("DIGESTOTHER", other_digest)
            .replace("DIGEST", digest)
        )
    normal_digest = strict_utf8_digest("normal")

    if backend == "local":
        from opencrab.stores.local_doc_store import LocalDocStore

        data_dir = tmp_path / "local"
        data_dir.mkdir()
        target = (
            '{"source_id": "target", "text": ' + json.dumps(text)
            + ', "metadata": {"pack_id": "pack"}'
            + ("" if helper is None else ', "source_integrity": ' + helper)
            + "}"
        )
        normal = {
            "source_id": "normal",
            "text": "normal",
            "metadata": {"pack_id": "pack"},
            "source_integrity": {"version": 1, "state": "complete", "text_digest": normal_digest},
        }
        raw = '{"target": ' + target + ', "normal": ' + json.dumps(normal) + "}"
        source_path = data_dir / "sources.json"
        source_path.write_text(raw, encoding="utf-8")
        before = source_path.read_bytes()
        store = LocalDocStore(str(data_dir))
    else:
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        store = LocalSQLDocStore(str(tmp_path / "docs.db"))
        for source_id, source_text, source_helper in (
            ("target", text, helper),
            ("normal", "normal", json.dumps(
                {"version": 1, "state": "complete", "text_digest": normal_digest}
            )),
        ):
            store._exec_write(
                "INSERT INTO doc_sources(source_id, text, metadata, ingested_at) "
                "VALUES (:source_id, :text, :metadata, :ingested_at)",
                {
                    "source_id": source_id,
                    "text": source_text,
                    "metadata": json.dumps({"pack_id": "pack"}),
                    "ingested_at": "2026-01-01T00:00:00+00:00",
                },
            )
            if source_helper is not None:
                store._exec_write(
                    "INSERT INTO doc_source_integrity(source_id, source_integrity) "
                    "VALUES (:source_id, :source_integrity)",
                    {"source_id": source_id, "source_integrity": source_helper},
                )

    state = store.read_source_state_exact("target")
    assert state is not None
    assert state.complete is complete
    assert state.text_digest == digest
    scoped = store.get_source_scoped_exact("target", ["pack"])
    assert scoped is not None
    classification = scoped["_source_classification"]
    assert classification["state_kind"] == state_kind
    assert classification["complete"] is complete
    assert classification["text_digest"] == digest
    assert store.read_source_state_exact("normal").complete is True
    if backend == "local":
        assert source_path.read_bytes() == before


def test_local_sources_get_list_non_dict_rows(local_store):
    path = local_store._collection_path("sources")
    dict_row = (
        '{"source_id": "dict-row", "text": "t", "metadata": {}, '
        '"source_integrity": {"version": 1, "state": "unknown"}}'
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            '{"list-row": [], "number-row": 1, "string-row": "x", '
            '"null-row": null, "dict-row": ' + dict_row + "}"
        )
    with open(path, "rb") as handle:
        before = handle.read()

    assert local_store.get_source("list-row") == []
    assert local_store.get_source("number-row") == 1
    assert local_store.get_source("string-row") == "x"
    assert local_store.get_source("null-row") is None
    public = local_store.get_source("dict-row")
    assert public == {"source_id": "dict-row", "text": "t", "metadata": {}}
    assert local_store.list_sources() == [[], 1, "x", None, public]
    for source_id in ("list-row", "number-row", "string-row", "null-row"):
        assert local_store.read_source_state_exact(source_id) is None
        assert local_store.get_source_scoped_exact(source_id, ["pack"]) is None
    with open(path, "rb") as handle:
        assert handle.read() == before


def test_source_copy_context_resets_after_error(sql_store):
    """#516 G1 ledger row: a failing write inside the copy context leaves no bound state."""
    from opencrab.stores._source_integrity import (
        SourceCopyState,
        current_source_copy_state,
        source_copy_context,
        strict_utf8_digest,
    )

    state = SourceCopyState("sqlite", "source", "body", strict_utf8_digest("body"), False)
    seen = []
    real_upsert = sql_store.upsert_source

    def failing_upsert(source_id, text, metadata):
        seen.append(current_source_copy_state())
        raise RuntimeError("forced write failure")

    sql_store.upsert_source = failing_upsert  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="forced write failure"):
        with source_copy_context(state):
            sql_store.upsert_source("source", "body", {"pack_id": "pack"})

    assert seen == [state], "the state was bound while the write ran"
    assert current_source_copy_state() is None
    sql_store.upsert_source = real_upsert  # type: ignore[method-assign]
    sql_store.upsert_source("source", "body", {"pack_id": "pack"})
    assert sql_store.read_source_state_exact("source").complete is True, "no state leaked into the next write"


# ---------------------------------------------------------------------------
# #516 ledger gap: Local JSON file rows through the actual query and range paths
# ---------------------------------------------------------------------------

_DUP_PACK = "pack"
_DUP_TEXT = "duplicate row text " * 300  # more than the 4000 character query cap


def _helper_variants(digest: str) -> dict[str, str]:
    """Raw JSON for the source_integrity member of a row, keyed by duplicate kind."""
    complete = '"version": 1, "state": "complete", "text_digest": "DIGEST"'
    variants = {
        "version": '"source_integrity": {"version": 9, "version": 1, "state": "complete", "text_digest": "DIGEST"}',
        "state": '"source_integrity": {"version": 1, "state": "unknown", "state": "complete", "text_digest": "DIGEST"}',
        "text_digest": '"source_integrity": {"version": 1, "state": "complete", "text_digest": "ZERO", "text_digest": "DIGEST"}',
        "same-value": '"source_integrity": {"version": 1, "version": 1, "state": "complete", "text_digest": "DIGEST"}',
        "helper-key": '"source_integrity": {"version": 1, "state": "unknown"}, "source_integrity": {' + complete + "}",
        "nested": '"source_integrity": {' + complete + ', "extra": {"a": 1, "a": 2}}',
    }
    return {kind: raw.replace("DIGEST", digest).replace("ZERO", "0" * 64) for kind, raw in variants.items()}


def _row_json(source_id: str, text: str, helper_json: str | None) -> str:
    fields = [
        '"source_id": ' + json.dumps(source_id),
        '"text": ' + json.dumps(text),
        '"metadata": ' + json.dumps({"pack_id": _DUP_PACK, "space": "evidence", "node_id": source_id}),
    ]
    if helper_json is not None:
        fields.append(helper_json)
    return json.dumps(source_id) + ": {" + ", ".join(fields) + "}"


def _complete_helper(text: str) -> str:
    from opencrab.stores._source_integrity import strict_utf8_digest

    return '"source_integrity": ' + json.dumps(
        {"version": 1, "state": "complete", "text_digest": strict_utf8_digest(text)}
    )


@pytest.mark.usefixtures("bind_test_principal")
def test_local_json_file_query_and_range_controls(tmp_path, monkeypatch):
    """Rows with a duplicate key inside the helper never get a locator.

    sources.json is raw text with the duplicate keys kept, so the actual file read,
    ``HybridQuery.query`` and the range handler all see it. Two controls give a locator that reads back the text: a normal complete row and a helper-free
    legacy row below the Local JSON limit. Legacy rows at and above the limit give no locator."""
    from opencrab.stores._source_integrity import strict_utf8_digest
    from tests import _issue516_flow as flow

    stack = flow.build_stack(tmp_path, _DUP_PACK, "local")
    try:
        rows, texts = [], {}
        for kind in _helper_variants("0"):
            # A distinct body per row: equal bodies would fold into one result.
            source_id, text = f"dup-{kind}", f"{_DUP_TEXT}{kind} "
            rows.append(_row_json(source_id, text, _helper_variants(strict_utf8_digest(text))[kind]))
            texts[source_id] = text
        normal_text = "normal complete row " * 300
        rows.append(_row_json("normal", normal_text, _complete_helper(normal_text)))
        texts["normal"] = normal_text
        for length in (4095, 4096, 4097):
            text = ("legacy " * 700)[:length]
            rows.append(_row_json(f"legacy-{length}", text, None))
            texts[f"legacy-{length}"] = text
        with open(stack["docs"]._collection_path("sources"), "w", encoding="utf-8") as handle:
            handle.write("{" + ", ".join(rows) + "}")

        items = flow.query_rows(stack, monkeypatch, _DUP_PACK, texts)
        for source_id, text in texts.items():
            truncation = items[source_id]["truncation"]
            if source_id in ("normal", "legacy-4095"):
                locator = truncation["source_locator"]
                assert locator["source_id"] == source_id, source_id
                chunk = flow.range_call(stack, locator, 64)
                assert chunk["text"] == text[:64], source_id
                assert chunk["total_bytes"] == len(text.encode("utf-8")), source_id
            else:
                assert truncation["full_text_unavailable"] is True, source_id
                assert "source_locator" not in truncation and "full_text_tool" not in truncation, source_id
                refused = flow.range_call(stack, flow.hand_locator(_DUP_PACK, source_id, text))
                assert refused == {"error": "full_text_unavailable"}, source_id
    finally:
        flow.close_stack(stack)


def test_local_json_integrity_normalization_preserves_rows_and_public_shape(tmp_path):
    """Reading changes no byte. A later write keeps every other row and stores the affected
    helper as an explicit unknown. Public rows are plain data."""
    from opencrab.stores._source_integrity import strict_utf8_digest
    from opencrab.stores.local_doc_store import LocalDocStore

    data_dir = tmp_path / "local"
    data_dir.mkdir()
    dup_text = "dup row text"
    digest = strict_utf8_digest(dup_text)
    normal = {
        "source_id": "normal",
        "text": "normal row",
        "metadata": {"pack_id": _DUP_PACK, "note": "keep"},
        "source_integrity": {"version": 1, "state": "complete", "text_digest": strict_utf8_digest("normal row")},
    }
    raw = "{" + ", ".join([
        _row_json("dup", dup_text, _helper_variants(digest)["helper-key"]),
        json.dumps("normal") + ": " + json.dumps(normal),
    ]) + "}"
    path = data_dir / "sources.json"
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()

    store = LocalDocStore(str(data_dir))
    assert store.read_source_state_exact("dup").complete is False
    assert store.read_source_state_exact("normal").complete is True
    assert store.get_source_scoped_exact("dup", [_DUP_PACK])["_source_classification"]["complete"] is False

    def plain(value):
        if isinstance(value, dict):
            assert type(value) is dict
            return all(plain(child) for child in value.values())
        if isinstance(value, list):
            assert type(value) is list
            return all(plain(child) for child in value)
        return True

    public = [store.get_source("dup"), store.get_source("normal"), *store.list_sources()]
    assert len(public) >= 4
    for row in public:
        assert isinstance(row, dict) and plain(row)
        assert "source_integrity" not in row
        assert not any(key.startswith("_") for key in row), list(row)
    assert path.read_bytes() == before, "reading must not rewrite the file"

    store.upsert_source("other", "other row", {"pack_id": _DUP_PACK})
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["dup"]["text"] == dup_text
    assert stored["dup"]["metadata"] == {"pack_id": _DUP_PACK, "space": "evidence", "node_id": "dup"}
    assert stored["dup"]["source_integrity"] == {"version": 1, "state": "unknown"}
    assert stored["normal"] == normal
    assert stored["other"]["text"] == "other row"
    assert stored["other"]["source_integrity"]["state"] == "complete"
    assert store.read_source_state_exact("dup").complete is False
