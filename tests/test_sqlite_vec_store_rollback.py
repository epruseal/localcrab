"""SqliteVecStore write-path rollback (issue #79).

add_texts/upsert_texts/delete/reset_collection used to commit unconditionally
at the end of a `with self._lock:` block with no except/rollback. A mid-batch
failure (e.g. a duplicate id partway through add_texts, which raises since
vec0 has no INSERT OR IGNORE/UPSERT) left the earlier rows of that same call
executed-but-uncommitted on the thread connection; the next unrelated
successful write's commit() would silently persist them too. `_tx()`
(``opencrab/stores/_sqlite_base.py``) now rolls back on exception.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from _vec_helpers import build_vector_store

DIM = 32


@pytest.fixture
def store(tmp_path):
    s = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
    yield s
    s.close()


def _legacy_owners(store, ids):
    rows = store._conn.execute(
        f"SELECT node_id, pack_id FROM {store._table} "  # noqa: S608
        "WHERE node_id IN (SELECT value FROM json_each(?))",
        (json.dumps(list(ids)),),
    ).fetchall()
    return {row["node_id"]: row["pack_id"] for row in rows}


def _outcome(call):
    try:
        return ("value", call())
    except Exception as exc:  # noqa: BLE001
        return ("exception", type(exc), str(exc))


def test_owner_lookup_uses_point_queries(store):
    store.upsert_texts(["first", "second"], ids=["가", "b"],
                       metadatas=[{"pack_id": "owner"}, {}])
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        assert store._slot_owners(store._conn, ["가", "missing", "b", "가"]) == {
            "가": "owner", "b": ""
        }
    finally:
        store._conn.set_trace_callback(None)
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT NODE_ID, PACK_ID")]
    assert selects
    assert all("json_each" not in sql.lower() for sql in selects)
    assert all("node_id =" in sql.lower() for sql in selects)


@pytest.mark.parametrize("ids", [[], ["가", "missing", "가"], [1, "가"], [None], ["\ud800"]])
def test_owner_lookup_matches_legacy_inputs(store, ids):
    store.upsert_texts(["first", "empty"], ids=["가", "empty"],
                       metadatas=[{"pack_id": "owner"}, {}])
    expected = _outcome(lambda: _legacy_owners(store, ids))
    actual = _outcome(lambda: store._slot_owners(store._conn, ids))
    assert actual == expected


def test_owner_lookup_preserves_legacy_null_owner(store):
    conn = store._conn
    with store._tx() as tx:
        params = list(store._insert_params("null-owner", "text", {}, [1.0] + [0.0] * (DIM - 1)))
        params[1] = None
        tx.execute(store._insert_sql(), params)
    expected = _legacy_owners(store, ["null-owner", "missing"])
    assert expected == {"null-owner": None}
    assert store._slot_owners(conn, ["null-owner", "missing"]) == expected


def test_owner_lookup_respects_variable_limit(store):
    store.upsert_texts(["first"], ids=["가"], metadatas=[{"pack_id": "owner"}])
    conn = store._conn
    old_limit = conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
    try:
        ids = [f"missing-{i}" for i in range(12)] + ["가", "가"]
        assert store._slot_owners(conn, ids) == _legacy_owners(store, ids) == {"가": "owner"}
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)


class TestAddTextsRollback:
    def test_partial_batch_failure_leaves_no_new_rows(self, store):
        store.add_texts(["base"], ids=["n1"])
        assert store.count() == 1

        with pytest.raises(Exception):
            # n2/n3 are new; n1 duplicates the existing PK and fails 3rd in
            # the per-row execute loop — n2/n3 must not survive the rollback.
            store.add_texts(["t2", "t3", "dup"], ids=["n2", "n3", "n1"])

        assert store.count() == 1
        assert store.get_by_id("n2") is None
        assert store.get_by_id("n3") is None

    def test_later_write_does_not_smuggle_in_failed_batch(self, store):
        store.add_texts(["base"], ids=["n1"])

        with pytest.raises(Exception):
            store.add_texts(["t2", "dup"], ids=["n2", "n1"])

        # unrelated, independent successful write on the same thread connection
        store.add_texts(["t9"], ids=["n9"])

        assert store.count() == 2
        assert store.get_by_id("n2") is None
        assert store.get_by_id("n9") is not None
