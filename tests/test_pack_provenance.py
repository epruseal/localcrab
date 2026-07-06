from __future__ import annotations

import json
import sqlite3

import pytest

from opencrab.ontology.pack_provenance import (
    backfill_pack_ids,
    infer_pack_id,
    infer_pack_id_from_path,
    matches_pack_filter,
    resolve_backfill_dry_run,
)


def test_t4_infer_pack_id_from_path_standard_layout() -> None:
    path = "/home/asdf/.openclaw/workspace/data/localcrab/packs/test-pack/stage/README.md"
    assert infer_pack_id_from_path(path) == "test-pack"


def test_t4_infer_pack_id_from_path_packs_only() -> None:
    assert infer_pack_id_from_path("packs/abc/stage/file.md") == "abc"


def test_t4_infer_pack_id_from_path_no_match() -> None:
    assert infer_pack_id_from_path("/tmp/random/file.md") is None
    assert infer_pack_id_from_path("") is None
    assert infer_pack_id_from_path(None) is None  # type: ignore[arg-type]


def test_t5_infer_pack_id_metadata_priority() -> None:
    item = {"metadata": {"pack_id": "from-metadata"}, "properties": {"pack_id": "from-props"}}
    assert infer_pack_id(item) == "from-metadata"


def test_t5_infer_pack_id_properties_fallback() -> None:
    item = {"properties": {"pack_id": "from-props"}}
    assert infer_pack_id(item) == "from-props"


def test_t5_infer_pack_id_from_source_path() -> None:
    item = {"metadata": {"source_path": "/tmp/packs/pack-x/stage/a.md"}}
    assert infer_pack_id(item) == "pack-x"


def test_t5_infer_pack_id_from_node_id() -> None:
    item = {"node_id": "/abs/packs/pack-y/stage/node-1"}
    assert infer_pack_id(item) == "pack-y"


def test_t5_infer_pack_id_none() -> None:
    assert infer_pack_id({}) is None
    assert infer_pack_id(None) is None


def test_t5_matches_pack_filter_pass_when_no_filter() -> None:
    assert matches_pack_filter({"metadata": {}}, pack_ids=None) is True
    assert matches_pack_filter({"metadata": {}}, pack_ids=[]) is True


def test_t5_matches_pack_filter_unpackaged_strict_default() -> None:
    item = {"metadata": {}}
    assert matches_pack_filter(item, pack_ids=["A"]) is False


def test_t5_matches_pack_filter_unpackaged_opt_in() -> None:
    item = {"metadata": {}}
    assert matches_pack_filter(item, pack_ids=["A"], include_unpackaged=True) is True


def test_t5_matches_pack_filter_member_passes() -> None:
    item = {"metadata": {"pack_id": "A"}}
    assert matches_pack_filter(item, pack_ids=["A", "B"]) is True


def test_t5_matches_pack_filter_foreign_rejected() -> None:
    item = {"metadata": {"pack_id": "Z"}}
    assert matches_pack_filter(item, pack_ids=["A"], include_unpackaged=True) is False


def test_t5_infer_pack_id_top_level() -> None:
    """item['pack_id'] is read after metadata/properties but before path inference."""
    item = {"pack_id": "from-top-level"}
    assert infer_pack_id(item) == "from-top-level"


def test_t5_metadata_wins_over_top_level_pack_id() -> None:
    """metadata pack_id takes priority over top-level pack_id."""
    item = {"metadata": {"pack_id": "from-metadata"}, "pack_id": "from-top-level"}
    assert infer_pack_id(item) == "from-metadata"


def test_t5_properties_wins_over_top_level_pack_id() -> None:
    """properties pack_id takes priority over top-level pack_id."""
    item = {"properties": {"pack_id": "from-props"}, "pack_id": "from-top-level"}
    assert infer_pack_id(item) == "from-props"


def test_t5_infer_pack_id_bm25_result_shape() -> None:
    """BM25 results may carry pack_id at top level without a metadata wrapper."""
    item = {"node_id": "n1", "text": "some text", "score": 0.5, "pack_id": "bm25-pack"}
    assert infer_pack_id(item) == "bm25-pack"


# ---------------------------------------------------------------------------
# resolve_backfill_dry_run
# ---------------------------------------------------------------------------


class TestResolveBackfillDryRun:
    # --- Normal / Edge: full reconciliation matrix (R11 extraction target) ---
    @pytest.mark.parametrize(
        "apply_changes,dry_run,expected_dry_run,expects_warning",
        [
            (False, None, True, False),  # neither flag -> default dry-run
            (True, None, False, False),  # --apply alone -> real run
            (False, True, True, False),  # --dry-run alone -> stays dry-run
            (False, False, True, True),  # --no-dry-run w/o --apply -> refuse, warn
            (True, True, True, True),  # contradictory -> honour --dry-run, warn
            (True, False, False, False),  # both explicit "do it" -> real run
        ],
    )
    def test_reconciliation_matrix(
        self, apply_changes, dry_run, expected_dry_run, expects_warning
    ) -> None:
        effective, warning = resolve_backfill_dry_run(apply_changes, dry_run)
        assert effective is expected_dry_run
        assert (warning is not None) is expects_warning


# ---------------------------------------------------------------------------
# backfill_pack_ids
# ---------------------------------------------------------------------------


def _seed_graph_db(db_path) -> None:
    from opencrab.stores.local_graph_store import LocalGraphStore

    store = LocalGraphStore(db_path=str(db_path))
    store.upsert_node("Agent", "n-inferable", {"source_path": "/data/packs/pack-a/x.md"})
    store.upsert_node("Agent", "n-unresolvable", {"note": "no path hint"})
    store.upsert_node("Agent", "n-already-set", {"pack_id": "existing-pack"})
    store.upsert_edge(
        "Agent", "n-inferable", "owns", "Project", "p-inferable",
        {"source_path": "/data/packs/pack-a/y.md"},
    )
    store.close()


def _read_node_properties(db_path, node_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT properties FROM graph_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        return json.loads(row[0])
    finally:
        conn.close()


class TestBackfillPackIds:
    # --- Normal ---
    def test_dry_run_infers_without_writing(self, tmp_path) -> None:
        db_path = tmp_path / "graph.db"
        _seed_graph_db(db_path)

        summary = backfill_pack_ids(db_path, dry_run=True)

        assert summary["dry_run"] is True
        assert summary["nodes_inferred"] == 1
        assert summary["nodes_skipped"] == 1
        assert summary["edges_inferred"] == 1
        assert "pack_id" not in _read_node_properties(db_path, "n-inferable")

    def test_apply_persists_inferred_and_leaves_existing_untouched(self, tmp_path) -> None:
        db_path = tmp_path / "graph.db"
        _seed_graph_db(db_path)

        summary = backfill_pack_ids(db_path, dry_run=False)

        assert summary["dry_run"] is False
        assert _read_node_properties(db_path, "n-inferable")["pack_id"] == "pack-a"
        assert _read_node_properties(db_path, "n-already-set")["pack_id"] == "existing-pack"

    def test_assume_pack_id_fills_unresolvable(self, tmp_path) -> None:
        db_path = tmp_path / "graph.db"
        _seed_graph_db(db_path)

        summary = backfill_pack_ids(db_path, assume_pack_id="fallback-pack", dry_run=False)

        assert summary["nodes_assumed"] == 1
        assert summary["nodes_skipped"] == 0
        assert _read_node_properties(db_path, "n-unresolvable")["pack_id"] == "fallback-pack"

    # --- Error ---
    def test_missing_table_raises(self, tmp_path) -> None:
        db_path = tmp_path / "empty.db"
        sqlite3.connect(db_path).close()  # valid sqlite file, but no schema at all

        with pytest.raises(sqlite3.OperationalError):
            backfill_pack_ids(db_path, dry_run=True)

    def test_malformed_properties_json_counts_as_skipped(self, tmp_path) -> None:
        """A hand-rolled minimal schema (no expression index on properties,
        unlike LocalGraphStore's real graph.db) so an invalid-JSON string can
        actually be persisted, to exercise the json.loads try/except."""
        db_path = tmp_path / "graph.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE graph_nodes (node_type TEXT, node_id TEXT, properties TEXT)"
        )
        conn.execute(
            "CREATE TABLE graph_edges (from_type TEXT, from_id TEXT, relation TEXT, "
            "to_type TEXT, to_id TEXT, properties TEXT)"
        )
        conn.execute(
            "INSERT INTO graph_nodes VALUES ('Agent', 'n-broken', 'not-json{{{')"
        )
        conn.commit()
        conn.close()

        summary = backfill_pack_ids(db_path, dry_run=True)

        # malformed properties -> treated as {} -> no pack_id -> inferred from
        # node_id (no /packs/ hint) -> skipped, not a crash.
        assert summary["nodes_skipped"] == 1
        assert summary["nodes_inferred"] == 0

    # --- Edge ---
    def test_empty_tables_are_a_no_op(self, tmp_path) -> None:
        from opencrab.stores.local_graph_store import LocalGraphStore

        db_path = tmp_path / "graph.db"
        store = LocalGraphStore(db_path=str(db_path))
        store.close()  # tables created, no rows

        summary = backfill_pack_ids(db_path, dry_run=True)

        assert summary == {
            "dry_run": True,
            "nodes_inferred": 0,
            "nodes_assumed": 0,
            "nodes_skipped": 0,
            "edges_inferred": 0,
            "edges_assumed": 0,
            "edges_skipped": 0,
        }

    def test_apply_is_idempotent_on_second_run(self, tmp_path) -> None:
        db_path = tmp_path / "graph.db"
        _seed_graph_db(db_path)
        backfill_pack_ids(db_path, dry_run=False)

        summary = backfill_pack_ids(db_path, dry_run=False)

        assert summary["nodes_inferred"] == 0
        assert summary["edges_inferred"] == 0
        assert summary["nodes_skipped"] == 1  # n-unresolvable, still no hint
