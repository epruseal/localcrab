"""
Contract tests for opencrab.ontology.identity (IdentityEngine, _fuzzy_similarity).

IdentityEngine manages two tables (node_aliases, duplicate_candidates) across
either SQLite or Postgres (dual DDL paths in _TABLES_SQLITE / _TABLES_PG,
identity.py:23-83). Tests run against SQLite always, and additionally against
Postgres when OPENCRAB_PG_TEST_URL is set (mirrors the pattern already used
in tests/test_execution_workflow.py).
"""

from __future__ import annotations

import os
import uuid

import pytest

from opencrab.ontology.identity import IdentityEngine, _fuzzy_similarity
from opencrab.stores.sql_store import SQLStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _pg_scoped_store(dsn: str, suffix: str):
    """Build a SQLStore whose (unqualified) DDL lands in a fresh, uuid-named
    PG schema rather than the shared `public` schema -- prevents concurrent
    pytest sessions from tripping over each other's CREATE/DROP TABLE.

    Mechanism: pointing every pooled connection's `search_path` at a schema
    that exists (and only that schema) makes IdentityEngine/SQLStore's
    unqualified DDL/DML land there without touching production code.
    psycopg2/libpq honor a `-c search_path=...` passed via the `options`
    connect kwarg, and SQLAlchemy forwards unrecognised URL query params
    straight through to psycopg2.connect().
    """
    from sqlalchemy import create_engine, text

    schema = f"t{uuid.uuid4().hex[:12]}_{suffix}"
    admin_engine = create_engine(dsn)
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    sep = "&" if "?" in dsn else "?"
    scoped_dsn = f"{dsn}{sep}options=-csearch_path%3D{schema}"
    store = SQLStore(scoped_dsn)
    return store, schema, admin_engine


def _drop_pg_schema(admin_engine, schema: str) -> None:
    from sqlalchemy import text

    with admin_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    admin_engine.dispose()


@pytest.fixture(params=["sqlite", "pg"])
def sql_store(request, tmp_path):
    if request.param == "sqlite":
        db_path = tmp_path / "identity.db"
        store = SQLStore(f"sqlite:///{db_path}")
        assert store.available
        yield store
        return

    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL 미설정 - PG identity 테스트 스킵")
    store, schema, admin_engine = _pg_scoped_store(dsn, "id")
    if not store.available:
        _drop_pg_schema(admin_engine, schema)
        pytest.skip(f"PG 테스트 DB 접속 불가: {dsn!r}")
    yield store
    _drop_pg_schema(admin_engine, schema)


@pytest.fixture
def engine(sql_store):
    return IdentityEngine(sql_store)


@pytest.fixture
def space():
    """A namespace unique to this test invocation, so that rows written into
    the shared `ontology_nodes` registry table (which sql_store's own
    teardown does not clean up, since other test files rely on it too)
    never collide with data from another test."""
    return f"test_{uuid.uuid4().hex[:8]}"


# ===========================================================================
# _fuzzy_similarity — 정상 (Normal)
# ===========================================================================

class TestFuzzySimilarityNormal:
    def test_identical_strings_score_one(self):
        assert _fuzzy_similarity("Alice Smith", "Alice Smith") == 1.0

    def test_partial_token_overlap_scores_between_zero_and_one(self):
        # {'alice','smith'} vs {'alice','smyth'} -> overlap=1, union=3
        assert _fuzzy_similarity("Alice Smith", "Alice Smyth") == pytest.approx(1 / 3, abs=1e-3)

    def test_disjoint_tokens_score_zero(self):
        assert _fuzzy_similarity("Alice Smith", "Bob Jones") == 0.0

    def test_case_insensitive(self):
        assert _fuzzy_similarity("ALICE SMITH", "alice smith") == 1.0


# ===========================================================================
# _fuzzy_similarity — 오류 (Error)
# ===========================================================================

class TestFuzzySimilarityErrors:
    def test_none_input_raises_attribute_error(self):
        """No caller in this codebase passes None (find_duplicates_by_name
        always supplies DB-backed node_id / caller-supplied name strings) —
        there is no documented graceful-None contract, so the natural
        AttributeError from `.lower()` is the expected, un-guarded behavior."""
        with pytest.raises(AttributeError):
            _fuzzy_similarity(None, "x")


# ===========================================================================
# _fuzzy_similarity — 엣지 (Edge)
# ===========================================================================

class TestFuzzySimilarityEdges:
    def test_empty_string_on_either_side_scores_zero(self):
        assert _fuzzy_similarity("", "Alice") == 0.0
        assert _fuzzy_similarity("Alice", "") == 0.0
        assert _fuzzy_similarity("", "") == 0.0

    def test_korean_tokens_overlap_correctly(self):
        # {'김철수','대리'} vs {'김철수','팀장'} -> overlap=1, union=3
        assert _fuzzy_similarity("김철수 대리", "김철수 팀장") == pytest.approx(1 / 3, abs=1e-3)


# ===========================================================================
# add_alias / resolve_canonical / get_aliases — 정상 (Normal)
# ===========================================================================

class TestAliasNormal:
    def test_add_alias_then_resolve_canonical(self, engine):
        engine.add_alias("canon1", "aliasA")
        assert engine.resolve_canonical("aliasA") == "canon1"

    def test_resolve_canonical_for_non_alias_returns_input_unchanged(self, engine):
        assert engine.resolve_canonical("never_registered") == "never_registered"

    def test_get_aliases_returns_all_records_for_canonical(self, engine):
        engine.add_alias("canon1", "aliasA")
        engine.add_alias("canon1", "aliasB")
        rows = engine.get_aliases("canon1")
        assert {r["alias_id"] for r in rows} == {"aliasA", "aliasB"}


# ===========================================================================
# add_alias — 엣지 (Edge)
# ===========================================================================

class TestAliasEdges:
    def test_duplicate_alias_insert_is_idempotent(self, engine):
        """UNIQUE (canonical_id, alias_id) + INSERT OR IGNORE / ON CONFLICT DO
        NOTHING: re-registering the same pair must not raise and must not
        create a second row."""
        engine.add_alias("canon1", "aliasA")
        engine.add_alias("canon1", "aliasA")
        rows = engine.get_aliases("canon1")
        assert len(rows) == 1


# ===========================================================================
# propose_duplicate / resolve_duplicate — 정상 (Normal)
# ===========================================================================

class TestDuplicateStateMachineNormal:
    def test_propose_creates_pending_candidate(self, engine):
        result = engine.propose_duplicate("nodeB", "nodeA", similarity=0.8)
        assert result["status"] == "pending"
        assert result["node_a_id"] == "nodeA"  # sorted() normalises pair order
        assert result["node_b_id"] == "nodeB"

    def test_propose_same_pair_regardless_of_argument_order_dedups(self, engine):
        first = engine.propose_duplicate("nodeB", "nodeA")
        second = engine.propose_duplicate("nodeA", "nodeB")
        assert second["already_exists"] is True
        assert second["candidate_id"] == first["candidate_id"]

    def test_resolve_accepted_registers_alias_node_b_into_node_a(self, engine):
        prop = engine.propose_duplicate("nodeB", "nodeA")
        result = engine.resolve_duplicate(prop["candidate_id"], "accepted", decided_by="tester")
        assert result["status"] == "accepted"
        aliases = engine.get_aliases("nodeA")
        assert any(a["alias_id"] == "nodeB" and a["alias_type"] == "merge" for a in aliases)

    def test_resolve_rejected_does_not_register_alias(self, engine):
        prop = engine.propose_duplicate("nodeB", "nodeA")
        engine.resolve_duplicate(prop["candidate_id"], "rejected")
        assert engine.get_aliases("nodeA") == []


# ===========================================================================
# propose_duplicate / resolve_duplicate — 오류 (Error)
# ===========================================================================

class TestDuplicateStateMachineErrors:
    def test_resolve_unknown_candidate_id_raises(self, engine):
        with pytest.raises(ValueError, match="not found or already resolved"):
            engine.resolve_duplicate("dup_does_not_exist", "accepted")

    def test_resolve_invalid_decision_raises_before_touching_db(self, engine):
        prop = engine.propose_duplicate("nodeB", "nodeA")
        with pytest.raises(ValueError, match="decision must be 'accepted' or 'rejected'"):
            engine.resolve_duplicate(prop["candidate_id"], "maybe")
        # still pending — the invalid decision must not have partially applied
        pending = engine.list_pending_candidates()
        assert any(c["candidate_id"] == prop["candidate_id"] for c in pending)

    def test_double_resolve_raises_on_second_call(self, engine):
        prop = engine.propose_duplicate("nodeB", "nodeA")
        engine.resolve_duplicate(prop["candidate_id"], "accepted")
        with pytest.raises(ValueError, match="not found or already resolved"):
            engine.resolve_duplicate(prop["candidate_id"], "accepted")


# ===========================================================================
# find_duplicates_by_name — 정상 (Normal)
# ===========================================================================

class TestFindDuplicatesByNameNormal:
    def test_finds_and_ranks_candidates_by_similarity_desc(self, sql_store, engine, space):
        sql_store.register_node(space, "person", "Alice Smith")
        sql_store.register_node(space, "person", "Alice Smyth")
        sql_store.register_node(space, "person", "Bob Jones")

        candidates = engine.find_duplicates_by_name(
            "new_id", "Alice Smith", space=space, threshold=0.3
        )
        assert [c["node_id"] for c in candidates] == ["Alice Smith", "Alice Smyth"]
        assert candidates[0]["similarity"] >= candidates[1]["similarity"]

    def test_limit_caps_result_count(self, sql_store, engine, space):
        for i in range(5):
            sql_store.register_node(space, "person", f"same_id extra{i}")
        candidates = engine.find_duplicates_by_name(
            "q", "same_id", space=space, threshold=0.0, limit=2
        )
        assert len(candidates) == 2


# ===========================================================================
# find_duplicates_by_name — 엣지 (Edge)
# ===========================================================================

class TestFindDuplicatesByNameEdges:
    def test_excludes_the_queried_node_id_itself(self, sql_store, engine, space):
        sql_store.register_node(space, "person", "same_id")
        sql_store.register_node(space, "person", "other")
        candidates = engine.find_duplicates_by_name(
            "same_id", "same_id", space=space, threshold=0.0
        )
        assert "same_id" not in [c["node_id"] for c in candidates]

    def test_below_threshold_candidates_are_excluded(self, sql_store, engine, space):
        sql_store.register_node(space, "person", "Bob Jones")
        candidates = engine.find_duplicates_by_name(
            "q", "Alice Smith", space=space, threshold=0.5
        )
        assert candidates == []

    def test_no_registered_nodes_returns_empty_list(self, engine, space):
        assert engine.find_duplicates_by_name("q", "Anything", space=space) == []


# ===========================================================================
# list_pending_candidates — 정상 (Normal)
# ===========================================================================

class TestListPendingCandidatesNormal:
    def test_only_pending_candidates_are_listed_sorted_by_similarity(self, engine):
        engine.propose_duplicate("a1", "a2", similarity=0.9)
        engine.propose_duplicate("b1", "b2", similarity=0.4)
        resolved = engine.propose_duplicate("c1", "c2", similarity=0.99)
        engine.resolve_duplicate(resolved["candidate_id"], "rejected")

        pending = engine.list_pending_candidates()
        sims = [p["similarity"] for p in pending]
        assert sims == sorted(sims, reverse=True)
        assert all(p["status"] == "pending" for p in pending)
        assert "c1" not in [p["node_a_id"] for p in pending] + [p["node_b_id"] for p in pending]
