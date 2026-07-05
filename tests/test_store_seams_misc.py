"""Seam tests for the S3-mechanical C3 adopter: pg_graph_store/pg_doc_store
(``_graph_common`` helper adoption), kuzu_graph_store (pack-filter parity +
graceful degrade on a missing ``ladybug``), and the ``_require_available()``
guard-message contracts for neo4j_store/mongo_store/sql_store/kuzu_graph_store.

Green against the PRE-adoption code EXCEPT the two groups explicitly
marked red-first below (kuzu pack-filter equivalence, kuzu missing-ladybug
degrade). The vector-store ``count()`` swallow bug (formerly inherited
judgment ①, xfail(strict)) is now fixed in sqlite_vec_store.py/
pg_vector_store.py; the pinning tests below are plain green.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("ladybug")


# ---------------------------------------------------------------------------
# Guard-message contracts (_require_available dedup) — pin the exact
# RuntimeError text per store so the mechanical swap can't silently drift.
# ---------------------------------------------------------------------------


class TestNeo4jGuardContracts:
    def _store(self):
        from opencrab.stores.neo4j_store import Neo4jStore

        return Neo4jStore("bolt://invalid-host:7687", "neo4j", "password")

    def test_get_node_raises(self):
        with pytest.raises(RuntimeError, match="Neo4j is not available"):
            self._store().get_node("User", "u1")

    def test_delete_node_raises(self):
        with pytest.raises(RuntimeError, match="Neo4j is not available"):
            self._store().delete_node("User", "u1")

    def test_find_neighbors_raises(self):
        with pytest.raises(RuntimeError, match="Neo4j is not available"):
            self._store().find_neighbors("u1")

    def test_find_path_raises(self):
        with pytest.raises(RuntimeError, match="Neo4j is not available"):
            self._store().find_path("a", "b")

    def test_count_nodes_raises(self):
        with pytest.raises(RuntimeError, match="Neo4j is not available"):
            self._store().count_nodes()

    def test_ensure_constraints_soft_returns_without_raising(self):
        """Different pattern (log + return) — must stay untouched by the dedup."""
        self._store().ensure_constraints()

    def test_lookup_node_type_returns_none_without_raising(self):
        """Different pattern (soft None) — must stay untouched by the dedup."""
        assert self._store().lookup_node_type("u1") is None


class TestMongoGuardContracts:
    def _store(self):
        from opencrab.stores.mongo_store import MongoStore

        return MongoStore("mongodb://invalid-host:27017", "testdb")

    def test_list_nodes_raises(self):
        with pytest.raises(RuntimeError, match="MongoDB is not available"):
            self._store().list_nodes()

    def test_delete_node_doc_raises(self):
        with pytest.raises(RuntimeError, match="MongoDB is not available"):
            self._store().delete_node_doc("s", "n1")

    def test_upsert_source_raises(self):
        with pytest.raises(RuntimeError, match="MongoDB is not available"):
            self._store().upsert_source("s1", "text", {})

    def test_get_source_raises(self):
        with pytest.raises(RuntimeError, match="MongoDB is not available"):
            self._store().get_source("s1")

    def test_list_sources_raises(self):
        with pytest.raises(RuntimeError, match="MongoDB is not available"):
            self._store().list_sources()

    def test_get_audit_log_raises(self):
        with pytest.raises(RuntimeError, match="MongoDB is not available"):
            self._store().get_audit_log()

    def test_collection_stats_soft_returns_empty_dict(self):
        """Different pattern (soft {}) — must stay untouched by the dedup."""
        assert self._store().collection_stats() == {}


class TestSQLGuardContracts:
    def _store(self):
        from opencrab.stores.sql_store import SQLStore

        return SQLStore("postgresql://invalid:invalid@invalid-host:5432/invalid")

    def test_register_node_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().register_node("s", "User", "u1")

    def test_register_edge_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().register_edge("s", "u1", "owns", "s", "p1")

    def test_save_impact_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().save_impact("n1", "update", {})

    def test_get_impacts_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().get_impacts("n1")

    def test_save_simulation_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().save_simulation("l1", "raises", 0.5, {})

    def test_set_policy_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().set_policy("u1", "view", "r1")

    def test_check_policy_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().check_policy("u1", "view", "r1")

    def test_list_policies_raises(self):
        with pytest.raises(RuntimeError, match="SQL store is not available"):
            self._store().list_policies("u1")

    def test_table_counts_soft_returns_empty_dict(self):
        """Different pattern (soft {}) — must stay untouched by the dedup."""
        assert self._store().table_counts() == {}


class TestKuzuGuardContracts:
    """Force ``_available=False`` via a live-ladybug init failure (not a
    missing-package failure — that path is covered separately below) so
    these pin the guard contract independent of the judgment-②  fix."""

    def _store(self, tmp_path, monkeypatch):
        import ladybug

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(ladybug, "Database", _boom)
        from opencrab.stores.kuzu_graph_store import KuzuGraphStore

        return KuzuGraphStore(db_path=str(tmp_path / "guard.kuzu"))

    def test_unavailable_after_init_failure(self, tmp_path, monkeypatch):
        assert self._store(tmp_path, monkeypatch).available is False

    def test_upsert_node_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).upsert_node("X", "x1", {})

    def test_get_node_by_id_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).get_node_by_id("x1")

    def test_delete_node_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).delete_node("X", "x1")

    def test_upsert_edge_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).upsert_edge("X", "a", "rel", "X", "b")

    def test_find_neighbors_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).find_neighbors("x1")

    def test_find_by_relations_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).find_by_relations("x1", ["rel"])

    def test_find_path_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).find_path("a", "b")

    def test_count_nodes_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).count_nodes()

    def test_list_packs_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).list_packs()

    def test_export_nodes_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).export_nodes()

    def test_export_edges_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).export_edges()

    def test_upsert_nodes_batch_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).upsert_nodes_batch([])

    def test_upsert_edges_batch_raises(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="KuzuGraphStore is not available"):
            self._store(tmp_path, monkeypatch).upsert_edges_batch([])

    def test_run_cypher_soft_returns_empty_list(self, tmp_path, monkeypatch):
        """Different pattern (soft []) — must stay untouched by the dedup."""
        assert self._store(tmp_path, monkeypatch).run_cypher("RETURN 1") == []


class TestPgGraphDocGuardMessagesUnchanged:
    """PGGraphStore/PgDocStore already own `_require_available`
    (test_pg_stores_direct.py pins the full PG-connected contract); this only
    confirms the `_graph_common` helper-dedup (item 2) leaves the
    schema-identifier validation message intact — no live PG needed."""

    def test_pg_graph_store_invalid_schema_message_unchanged(self):
        from opencrab.stores.pg_graph_store import PGGraphStore

        with pytest.raises(ValueError, match="Invalid schema identifier"):
            PGGraphStore("postgresql://x/y", schema="bad-schema; drop table")

    def test_pg_doc_store_invalid_schema_message_unchanged(self):
        from opencrab.stores.pg_doc_store import PgDocStore

        with pytest.raises(ValueError, match="Invalid schema identifier"):
            PgDocStore("postgresql://x/y", schema="bad-schema; drop table")


# ---------------------------------------------------------------------------
# RED-FIRST: kuzu missing-ladybug degrade (INHERITED JUDGMENT ②)
#
# Every other optional-dependency store (Neo4jStore, MongoStore) wraps its
# driver import inside the try/except that flips `available=False` + logs a
# warning. KuzuGraphStore.__init__ instead does `import ladybug` BEFORE its
# try-guard (kuzu_graph_store.py ~54), so a missing package raises an
# uncaught ImportError instead of degrading gracefully — and nothing in
# factory.make_graph_store()/cli.py/mcp/tools.py catches it, so selecting
# STORAGE_MODE=kuzu without ladybug installed crashes app init instead of
# leaving a store with `available=False` (the pattern every status/health
# check elsewhere in this codebase relies on). Judged accidental, not
# deliberate fail-fast — see stage report judgment ②.
# ---------------------------------------------------------------------------


def test_kuzu_missing_ladybug_degrades_gracefully_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "ladybug", None)
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    store = KuzuGraphStore(db_path=str(tmp_path / "graph.kuzu"))
    assert store.available is False


# ---------------------------------------------------------------------------
# RED-FIRST: kuzu pack-filter policy equivalence vs. _graph_common
#
# kuzu_graph_store.py re-implements the pack-filter 3-rule policy inline
# (find_neighbors ~228-241, _find_neighbors_1hop ~304-315) instead of calling
# opencrab.stores._graph_common._node_passes/_edge_passes (the policy the
# local/pg backends share). Two real divergences found — see stage report
# judgment/finding for kuzu equivalence:
#
#   1. `_node_pack_id` casts a truthy pack_id via `str(pid)` before the
#      `pack_set` membership check; kuzu's inline `pid not in pack_set` does
#      not, so a numeric `pack_id` property (e.g. ingested as JSON int 42)
#      wrongly fails a string `pack_ids=["42"]` filter.
#   2. `_node_pack_id` treats a falsy `pack_id` (e.g. `""`) as "no pack_id"
#      (governed by `include_unpackaged`); kuzu's inline check treats any
#      non-None `pack_id` — including `""` — as a real, foreign pack_id that
#      is unconditionally excluded, ignoring `include_unpackaged=True`.
#
# Fixed by having kuzu call the shared helpers directly (props/edge_props are
# already plain dicts via `_parse`, so no restructuring is needed).
# ---------------------------------------------------------------------------


class TestKuzuPackFilterEquivalence:
    @pytest.fixture
    def store(self, tmp_path):
        from opencrab.stores.kuzu_graph_store import KuzuGraphStore

        s = KuzuGraphStore(db_path=str(tmp_path / "equiv_kuzu"))
        yield s
        s.close()

    def test_int_typed_pack_id_matches_str_filter(self, store):
        store.upsert_node("Item", "anchor", {"pack_id": "42"})
        store.upsert_node("Item", "n1", {"pack_id": 42})  # numeric, not str
        store.upsert_edge("Item", "anchor", "rel", "Item", "n1")

        rows = store.find_neighbors(
            "anchor", direction="out", depth=1, pack_ids=["42"]
        )
        ids = {r["properties"]["id"] for r in rows}
        assert "n1" in ids

    def test_empty_string_pack_id_excluded_by_default(self, store):
        """Both policies agree here — establishes the baseline before the
        include_unpackaged=True case below shows the actual divergence."""
        store.upsert_node("Item", "anchor", {"pack_id": "A"})
        store.upsert_node("Item", "n1", {"pack_id": ""})
        store.upsert_edge("Item", "anchor", "rel", "Item", "n1")

        rows = store.find_neighbors(
            "anchor", direction="out", depth=1,
            pack_ids=["A"], include_unpackaged=False,
        )
        assert all(r["properties"]["id"] != "n1" for r in rows)

    def test_empty_string_pack_id_treated_as_unpackaged_when_included(self, store):
        store.upsert_node("Item", "anchor", {"pack_id": "A"})
        store.upsert_node("Item", "n1", {"pack_id": ""})
        store.upsert_edge("Item", "anchor", "rel", "Item", "n1")

        rows = store.find_neighbors(
            "anchor", direction="out", depth=1,
            pack_ids=["A"], include_unpackaged=True,
        )
        ids = {r["properties"]["id"] for r in rows}
        assert "n1" in ids


# ---------------------------------------------------------------------------
# FIXED (was INHERITED JUDGMENT ①, escalated to C2): sqlite_vec_store.count()
# and pg_vector_store.count() used to wrap the *live query* in
# `except Exception: return 0` — not just the `if not self._available:
# return 0` early return that test_stores.py:97-101 pins as an intentional,
# tested contract. A genuine failure (locked DB, dropped table, connection
# drop) during the count query was therefore indistinguishable from "0 rows",
# unlike graph stores' count_nodes() (raises) or chroma_store.count() (no such
# swallow around its own live `.count()` call — only the availability
# early-return). Both vector stores now let a real query exception propagate;
# these tests pin that contract (red→green).
# ---------------------------------------------------------------------------


def test_sqlite_vec_count_surfaces_real_query_errors_not_just_unavailable(tmp_path):
    from _vec_helpers import build_vector_store

    store = build_vector_store("sqlite-vec", tmp_path, dim=8)
    try:
        store.add_texts(texts=["a"], metadatas=[{}], ids=["n1"])
        # Still `available=True` — this is a genuine query-time failure, not
        # the unavailable-store case test_stores.py:97-101 already pins.
        store._conn.execute(f"DROP TABLE {store._table}")
        with pytest.raises(Exception):
            store.count()
    finally:
        store.close()


def test_pg_vector_count_surfaces_real_query_errors_not_just_unavailable(tmp_path):
    from _vec_helpers import build_vector_store

    store = build_vector_store("pg", tmp_path, dim=8)
    try:
        store.add_texts(texts=["a"], metadatas=[{}], ids=["n1"])
        # Still `available=True` — this is a genuine query-time failure, not
        # the unavailable-store case test_stores.py:97-101 already pins.
        from sqlalchemy import text

        with store._engine.begin() as conn:
            conn.execute(text(f"DROP TABLE {store._table}"))
        with pytest.raises(Exception):
            store.count()
    finally:
        store.close()
