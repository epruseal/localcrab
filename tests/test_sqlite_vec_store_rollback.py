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

import pytest
from _vec_helpers import build_vector_store

DIM = 32


@pytest.fixture
def store(tmp_path):
    s = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
    yield s
    s.close()


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
