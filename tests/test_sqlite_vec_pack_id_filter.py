"""sqlite-vec reads an unowned pack_id the same way its partition column stores it (#85).

The partition column holds ``slot_owner(meta)``. An absent, ``None``, or empty
``pack_id`` is the empty owner. The Python post-filter read an absent
``pack_id`` as a missing key. A row without the key matched nothing. The fixture
builds the legacy ``None`` row with direct SQL. The public write paths already
replace ``None``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _vec_helpers import build_vector_store  # noqa: E402

from opencrab.stores.sqlite_vec_store import _eval_where  # noqa: E402

DIM = 8


@pytest.fixture
def store(tmp_path):
    s = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
    s.upsert_texts(texts=["keyless doc"], ids=["keyless"])
    s.upsert_texts(texts=["explicit empty"], metadatas=[{"pack_id": ""}], ids=["empty"])
    s.upsert_texts(texts=["owned a"], metadatas=[{"pack_id": "A", "space": "s1"}], ids=["a"])
    s.upsert_texts(texts=["owned b"], metadatas=[{"pack_id": "B"}], ids=["b"])
    s.upsert_texts(texts=["keyless space"], metadatas=[{"space": "s1"}], ids=["ks"])
    s.upsert_texts(texts=["legacy none"], metadatas=[{"pack_id": ""}], ids=["legacy_none"])
    # A legacy row whose JSON carries pack_id None while the column is empty.
    with s._tx() as conn:
        conn.execute(
            "UPDATE vtest SET metadata = ? WHERE node_id = ?",
            (json.dumps({"pack_id": None}), "legacy_none"),
        )
    yield s
    s.close()


def _ids(store, where):
    return sorted(h["id"] for h in store.query("doc", n_results=20, where=where))


class TestUnownedRowsFollowThePartitionColumn:
    def test_empty_equality_finds_keyless_none_and_empty_rows(self, store):
        assert _ids(store, {"pack_id": ""}) == ["empty", "keyless", "ks", "legacy_none"]

    def test_empty_in_finds_the_same_rows(self, store):
        assert _ids(store, {"pack_id": {"$in": [""]}}) == ["empty", "keyless", "ks", "legacy_none"]

    def test_empty_pack_with_residual_filter(self, store):
        where = {"$and": [{"pack_id": ""}, {"space": "s1"}]}
        assert _ids(store, where) == ["ks"]

    def test_explicit_pack_scope_is_unchanged(self, store):
        assert _ids(store, {"pack_id": "A"}) == ["a"]
        assert _ids(store, {"pack_id": {"$in": ["A", "B"]}}) == ["a", "b"]

    def test_unowned_rows_never_enter_an_owned_scope(self, store):
        ids = _ids(store, {"pack_id": {"$in": ["A"]}})
        assert "keyless" not in ids and "ks" not in ids and "empty" not in ids

    def test_missing_space_still_matches_nothing(self, store):
        assert "keyless" not in _ids(store, {"space": "s1"})
        assert _ids(store, {"space": "s1"}) == ["a", "ks"]


class TestNegativeOperatorsMatchThePgColumnPredicate:
    """pgvector compares the column only, where an unowned row is ''."""

    @pytest.mark.parametrize(
        ("cond", "expected"),
        [
            ({"$ne": "A"}, True),
            ({"$ne": ""}, False),
            ({"$nin": ["A"]}, True),
            ({"$nin": [""]}, False),
            ({"$nin": []}, True),
            ({"$eq": ""}, True),
        ],
    )
    @pytest.mark.parametrize("meta", [{}, {"pack_id": None}, {"pack_id": ""}])
    def test_unowned_metadata_reads_as_the_empty_owner(self, meta, cond, expected):
        assert _eval_where({"pack_id": cond}, meta) is expected

    def test_other_values_keep_their_prior_reading(self):
        assert _eval_where({"pack_id": "None"}, {"pack_id": "None"}) is True
        assert _eval_where({"pack_id": 5}, {"pack_id": 5}) is True
        assert _eval_where({"pack_id": ""}, {"pack_id": 0}) is False
        assert _eval_where({"pack_id": ""}, {"pack_id": False}) is False

    def test_other_missing_keys_still_never_match(self):
        assert _eval_where({"space": {"$ne": "x"}}, {}) is False
        assert _eval_where({"space": "x"}, {}) is False

    def test_the_stored_metadata_is_not_rewritten(self, store):
        hit = next(h for h in store.query("doc", n_results=20) if h["id"] == "keyless")
        assert hit["metadata"] == {}
