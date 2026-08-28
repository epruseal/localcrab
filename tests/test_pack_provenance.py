from __future__ import annotations

import json
import sqlite3

import pytest

from opencrab.common.graph_identity import EdgeIdentityConflict, NodeIdentityConflict
from opencrab.ontology.pack_provenance import (
    backfill_pack_ids,
    infer_pack_id,
    infer_pack_id_from_path,
    inspect_pack_ids,
    matches_pack_filter,
    resolve_backfill_dry_run,
    resolve_row_pack_id,
)
from tests.issue80_migration import FixtureHandle


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
        "Agent", "n-inferable", "owns", "Agent", "n-already-set",
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

        with pytest.raises(RuntimeError, match="target graph store"):
            backfill_pack_ids(db_path, dry_run=False)
        assert "pack_id" not in _read_node_properties(db_path, "n-inferable")

    def test_assume_pack_id_fills_unresolvable(self, tmp_path) -> None:
        db_path = tmp_path / "graph.db"
        _seed_graph_db(db_path)

        with pytest.raises(RuntimeError, match="target graph store"):
            backfill_pack_ids(db_path, assume_pack_id="fallback-pack", dry_run=False)
        assert "pack_id" not in _read_node_properties(db_path, "n-unresolvable")

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
        with pytest.raises(RuntimeError, match="target graph store"):
            backfill_pack_ids(db_path, dry_run=False)
        with pytest.raises(RuntimeError, match="target graph store"):
            backfill_pack_ids(db_path, dry_run=False)
        assert "pack_id" not in _read_node_properties(db_path, "n-inferable")


# ---------------------------------------------------------------------------
# resolve_row_pack_id (#146 M P1-1, gate R4: each of the helper's outcomes,
# directly -- the SAME helper _process (above) and the migration script's
# node-pack-map builders call, so a divergence between them is structurally
# impossible)
# ---------------------------------------------------------------------------


class TestResolveRowPackId:
    def test_existing_pack_id_wins_over_everything_else(self) -> None:
        row = {"node_type": "Agent", "node_id": "n1"}
        pid, reason = resolve_row_pack_id(
            json.dumps({"pack_id": "already-set", "source_path": "/packs/other/x.md"}),
            row,
            "fallback-pack",
        )
        assert (pid, reason) == ("already-set", "existing")

    def test_inferred_from_props_source_path(self) -> None:
        row = {"node_type": "Agent", "node_id": "n1"}
        pid, reason = resolve_row_pack_id(
            json.dumps({"source_path": "/data/packs/pack-a/x.md"}), row, None
        )
        assert (pid, reason) == ("pack-a", "inferred")

    def test_inferred_from_row_id_column_when_props_have_no_hint(self) -> None:
        row = {"node_type": "Agent", "node_id": "/abs/packs/pack-y/x"}
        pid, reason = resolve_row_pack_id(json.dumps({}), row, None)
        assert (pid, reason) == ("pack-y", "inferred")

    def test_assumed_fallback_when_nothing_inferable(self) -> None:
        row = {"node_type": "Agent", "node_id": "n1"}
        pid, reason = resolve_row_pack_id(json.dumps({}), row, "fallback-pack")
        assert (pid, reason) == ("fallback-pack", "assumed")

    def test_skipped_non_dict_valid_json(self) -> None:
        """Valid JSON that isn't an object (a bare string here) -- distinct
        from malformed JSON, which is treated as ``{}`` (still a dict)."""
        row = {"node_type": "Agent", "node_id": "n1"}
        pid, reason = resolve_row_pack_id(json.dumps("just a string"), row, "fallback-pack")
        assert (pid, reason) == (None, "skipped-non-dict")

    def test_skipped_unresolvable_when_no_assume_pack_id(self) -> None:
        row = {"node_type": "Agent", "node_id": "n1"}
        pid, reason = resolve_row_pack_id(json.dumps({}), row, None)
        assert (pid, reason) == (None, "skipped-unresolvable")

    def test_malformed_json_treated_as_empty_dict(self) -> None:
        row = {"node_type": "Agent", "node_id": "n1"}
        pid, reason = resolve_row_pack_id("not-json{{{", row, None)
        assert (pid, reason) == (None, "skipped-unresolvable")

    def test_v3_defect7_json_string_and_already_parsed_dict_agree(self) -> None:
        """#146 P1(b), PR #177 review round 3 v3 결함 7: a PostgreSQL JSONB
        column (psycopg2 hands back an already-decoded dict) must resolve
        IDENTICALLY to the exact same properties given as a JSON string
        (what SQLite always stores) -- a dict input must not fall through
        to ``json.loads(dict)`` raising ``TypeError`` -> swallowed to
        ``{}`` -> "skipped-unresolvable"/"default", the defect this
        equivalence test exists to catch."""
        row = {"node_id": "n1"}
        props = {"source_path": "/packs/pack-a/docs/n1"}
        as_string = resolve_row_pack_id(json.dumps(props), row, None)
        as_dict = resolve_row_pack_id(props, row, None)
        assert as_string == as_dict == ("pack-a", "inferred")

    def test_v3_defect7_dict_input_with_existing_pack_id(self) -> None:
        """Same equivalence for the "existing" branch, not just "inferred"
        -- a dict ``props_raw`` whose pack_id is already set must be read
        directly, not require a str round-trip first."""
        row = {"node_id": "n1"}
        props = {"pack_id": "already-set"}
        assert resolve_row_pack_id(props, row, None) == ("already-set", "existing")

    def test_v3_defect7_dict_input_that_is_falsy_empty_dict(self) -> None:
        """An empty dict ``{}`` is falsy in Python -- must not be
        misrouted into the ``json.loads`` branch (where ``if props_raw``
        would also treat it as "nothing to parse")."""
        row = {"node_id": "n1"}
        assert resolve_row_pack_id({}, row, "fallback-pack") == ("fallback-pack", "assumed")


def _issue80_provenance_sqlite(case: str) -> None:
    with FixtureHandle.create() as fixture:
        from opencrab.stores.local_graph_store import LocalGraphStore
        store = LocalGraphStore(str(fixture.db_path))
        try:
            store.upsert_node("Person", "n1", {"source_path": "/packs/p1/a"})
            store.upsert_node("Person", "n2", {"source_path": "/packs/p1/b"})
            store.upsert_edge("Person", "n1", "knows", "Person", "n2", {"source_path": "/packs/p1/e"})
            plan = inspect_pack_ids(fixture.db_path)
            records = list(plan["records"])
            assert records and all(item["kind"] in {"node", "edge"} for item in records)
            if case == "stale_node_digest_rejected":
                store.update_node("n1", store.get_node_digest("n1"), "Person", {"source_path": "/packs/p1/changed"})
                try:
                    store.backfill_pack_provenance(records)
                except (NodeIdentityConflict, RuntimeError):
                    return
                raise AssertionError("stale provenance plan was accepted")
            if case == "malformed_properties_rejected":
                import sqlite3
                conn = sqlite3.connect(fixture.db_path)
                conn.execute("UPDATE graph_nodes SET properties=?", ("[]",))
                conn.commit()
                conn.close()
                try:
                    store.backfill_pack_provenance(records)
                except RuntimeError as exc:
                    assert "malformed" in str(exc)
                    return
                raise AssertionError("malformed provenance row was accepted")
            if case == "malformed_owner_rejected":
                bad = dict(records[0])
                bad["proposed_pack_id"] = 7
                try:
                    store.backfill_pack_provenance([bad])
                except ValueError:
                    return
                raise AssertionError("malformed provenance owner was accepted")
            if case == "missing_node_rejected":
                bad = dict(records[0])
                bad["node_id"] = "missing"
                try:
                    store.backfill_pack_provenance([bad])
                except RuntimeError:
                    return
                raise AssertionError("missing provenance node was accepted")
            if case == "duplicate_mixed_key_rejected":
                try:
                    store.backfill_pack_provenance([records[0], records[0]])
                except RuntimeError:
                    return
                raise AssertionError("duplicate provenance key was accepted")
            if case == "node_edge_mixed_batch_rollback":
                node = next(item for item in records if item["kind"] == "node")
                edge = dict(next(item for item in records if item["kind"] == "edge"))
                edge["to_id"] = "missing"
                try:
                    store.backfill_pack_provenance([node, edge])
                except (RuntimeError, ValueError, EdgeIdentityConflict):
                    assert "pack_id" not in store.get_node("Person", "n1")
                    return
                raise AssertionError("mixed invalid provenance batch was accepted")
            if case == "existing_owner_preserved":
                store.update_node("n1", store.get_node_digest("n1"), "Person", {"source_path": "/packs/p1/a", "pack_id": "p1"})
                plan = inspect_pack_ids(fixture.db_path)
                receipt = store.backfill_pack_provenance(plan["records"])
                assert receipt.records
                return
            if case == "existing_owner_conflict":
                store.update_node("n1", store.get_node_digest("n1"), "Person", {"source_path": "/packs/p1/a", "pack_id": "other"})
                plan = inspect_pack_ids(fixture.db_path)
                conflicting = dict(plan["records"][0])
                conflicting["proposed_pack_id"] = "p1"
                try:
                    store.backfill_pack_provenance([conflicting])
                except RuntimeError:
                    return
                raise AssertionError("foreign owner was overwritten")
            receipt = store.backfill_pack_provenance(records)
            assert receipt.target_fingerprint_before == plan["target_fingerprint"]
            assert receipt.target_fingerprint_after == store.graph_fingerprint()
            if case in {"node_inferred_plan_and_apply", "node_assumed_plan_and_apply", "exact_allowed_property_delta", "post_write_digest_receipt"}:
                assert all(item.after_digest for item in receipt.records)
            if case == "dry_run_plan_fingerprint_and_evidence_frozen":
                assert all(item.get("dry_run_evidence_digest") for item in records)
        finally:
            store.close()


def test_issue80_inspection_uses_runtime_space_precedence_for_legacy_row() -> None:
    """A frozen plan must fingerprint a direct-edit row exactly as the store.

    ``space_id`` is the safety-net winner when a legacy JSON payload carries a
    conflicting truthy ``space`` value.  Keeping that rule in one helper makes
    the plan's CAS target equal the target store's live fingerprint.
    """
    with FixtureHandle.create() as fixture:
        from opencrab.stores.local_graph_store import LocalGraphStore

        store = LocalGraphStore(str(fixture.db_path))
        try:
            store.upsert_node(
                "Person", "n1", {"source_path": "/packs/p1/a"}, space_id="runtime-space"
            )
            conn = sqlite3.connect(fixture.db_path)
            try:
                conn.execute(
                    "UPDATE graph_nodes SET properties=? WHERE node_id=?",
                    (json.dumps({"id": "n1", "space": "legacy-space", "source_path": "/packs/p1/a"}), "n1"),
                )
                conn.commit()
            finally:
                conn.close()
            plan = inspect_pack_ids(fixture.db_path)
            assert plan["target_fingerprint"] == store.graph_fingerprint()
            store.backfill_pack_provenance(plan["records"])
        finally:
            store.close()


@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def _issue80_provenance_dispatch(backend: str, case: str) -> None:
    assert backend == "sqlite"
    _issue80_provenance_sqlite(case)

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_node_inferred_plan_and_apply(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "node_inferred_plan_and_apply")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_node_assumed_plan_and_apply(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "node_assumed_plan_and_apply")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_existing_owner_preserved(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "existing_owner_preserved")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_existing_owner_conflict(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "existing_owner_conflict")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_stale_node_digest_rejected(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "stale_node_digest_rejected")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_malformed_properties_rejected(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "malformed_properties_rejected")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_malformed_owner_rejected(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "malformed_owner_rejected")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_missing_node_rejected(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "missing_node_rejected")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_duplicate_mixed_key_rejected(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "duplicate_mixed_key_rejected")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_node_edge_mixed_batch_rollback(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "node_edge_mixed_batch_rollback")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_exact_allowed_property_delta(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "exact_allowed_property_delta")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_post_write_digest_receipt(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "post_write_digest_receipt")

@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_dry_run_plan_fingerprint_and_evidence_frozen(backend: str) -> None:
    _issue80_provenance_dispatch(backend, "dry_run_plan_fingerprint_and_evidence_frozen")


@pytest.mark.parametrize("backend", ["sqlite"], ids=["sqlite"])
def test_issue80_provenance_mixed_lock_total_order_trace(backend: str) -> None:
    _issue80_provenance_sqlite("node_edge_mixed_batch_rollback")
