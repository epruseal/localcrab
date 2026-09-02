"""Actual SQLite migration qualification for issue #80.

These tests deliberately exercise the public migration API against disposable
files.  Fixture-only catalog writes are confined to ``FixtureHandle`` leases;
the production store is never used to manufacture legacy or corrupt rows.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from opencrab.common.graph_identity import (
    ApplyMigrationRequest,
    DryRunMigrationRequest,
    ExplicitMerge,
    GraphMigrationConflict,
    GraphSchemaMigrationRequired,
    LegacyNodeKey,
    PropertyResolution,
    receipt_sha256,
)
from opencrab.stores.local_graph_store import LocalGraphStore
from tests.issue80_migration import FixtureHandle, graph_snapshot, schema_snapshot


def _seed_duplicate_fixture() -> tuple[FixtureHandle, LocalGraphStore]:
    fixture = FixtureHandle.create()
    fixture.create_legacy()
    fixture.seed(
        nodes=(
            ("Agent", "a", None, {"name": "same"}),
            ("Person", "a", None, {"name": "same"}),
            ("Person", "b", None, {"name": "bee"}),
        ),
        edges=(
            ("Agent", "a", "knows", "Person", "b", {"weight": 1}),
            ("Person", "a", "knows", "Person", "b", {"weight": 1}),
        ),
    )
    return fixture, LocalGraphStore(str(fixture.db_path))


def _merge_request(store: LocalGraphStore) -> tuple[Any, Any, Any]:
    inventory = store.inspect_graph_identity()
    duplicates = sorted(
        (row for row in inventory.nodes if row.key.node_id == "a"),
        key=lambda row: (row.key.node_type, row.key.node_id),
    )
    assert [row.key for row in duplicates] == [
        LegacyNodeKey("Agent", "a"), LegacyNodeKey("Person", "a")
    ]
    merge = ExplicitMerge(
        sources=tuple((row.key, row.digest) for row in duplicates),
        target_node_id="a",
        target_node_type="Person",
        target_space_id=None,
        target_pack_id=None,
    )
    request = DryRunMigrationRequest(inventory.source_fingerprint, mappings=(merge,))
    return inventory, request, store.migrate_graph_identity(request)


def _apply_request(dry: Any, artifact: Path, *, request_id: str | None = None) -> ApplyMigrationRequest:
    return ApplyMigrationRequest(
        request_id=request_id or f"issue80-{uuid.uuid4().hex}",
        expected_source_fingerprint=dry.source_fingerprint,
        plan_bytes=dry.plan_bytes,
        plan_sha256=dry.plan_sha256,
        backup_path=artifact,
        backup_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )


def _ledger_row(path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT request_id,phase,request_digest,source_fingerprint,"
            "mapping_fingerprint,plan_sha256,target_fingerprint_before,"
            "target_fingerprint_after,edge_loss,property_loss,receipt_bytes "
            "FROM graph_migration_receipts ORDER BY request_id"
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_sqlite_inventory_is_read_only_and_duplicate_identity_requires_explicit_mapping() -> None:
    fixture, store = _seed_duplicate_fixture()
    try:
        before = graph_snapshot(fixture.db_path)
        inventory = store.inspect_graph_identity()
        after = graph_snapshot(fixture.db_path)
        assert before == after
        assert inventory.schema_state == "legacy"
        assert len(inventory.nodes) == 3
        assert [row.key.node_id for row in inventory.nodes].count("a") == 2
        with pytest.raises(GraphMigrationConflict, match="duplicate bare node id"):
            store.migrate_graph_identity(
                DryRunMigrationRequest(inventory.source_fingerprint, mappings=())
            )
        assert graph_snapshot(fixture.db_path) == before
    finally:
        store.close()
        fixture.close()


def test_sqlite_malformed_properties_are_lossless_and_rejected_before_dml() -> None:
    fixture = FixtureHandle.create()
    fixture.create_legacy()
    fixture.seed(
        nodes=(
            ("Array", "bad-array", None, "[1,2,3]"),
            ("Scalar", "bad-scalar", None, '"not-an-object"'),
        ),
        edges=(
            ("Array", "bad-array", "corrupt", "Scalar", "bad-scalar", b"\xff\x00\xfe"),
        ),
    )
    store = LocalGraphStore(str(fixture.db_path))
    try:
        before = graph_snapshot(fixture.db_path)
        inventory = store.inspect_graph_identity()
        after = graph_snapshot(fixture.db_path)
        assert before == after
        rows = {row.key.node_type: row for row in inventory.nodes}
        assert rows["Array"].normalized_properties is None
        assert rows["Scalar"].normalized_properties is None
        assert rows["Array"].property_error == "malformed_json"
        assert rows["Scalar"].property_error == "malformed_json"
        edge = inventory.edges[0]
        assert bytes(edge.raw_properties) == b"\xff\x00\xfe"
        assert edge.normalized_properties is None
        assert edge.property_error == "malformed_json"
        with pytest.raises(GraphMigrationConflict):
            store.migrate_graph_identity(
                DryRunMigrationRequest(inventory.source_fingerprint, mappings=())
            )
        assert graph_snapshot(fixture.db_path) == before
    finally:
        store.close()
        fixture.close()


def test_sqlite_property_resolution_is_fail_closed_and_valid_plan_has_no_loss() -> None:
    fixture = FixtureHandle.create()
    fixture.create_legacy()
    fixture.seed(
        nodes=(
            ("Agent", "a", None, {"name": "agent", "city": "Seoul"}),
            ("Person", "a", None, {"name": "person", "city": "Seoul"}),
        )
    )
    store = LocalGraphStore(str(fixture.db_path))
    try:
        inventory = store.inspect_graph_identity()
        rows = {row.key: row for row in inventory.nodes}
        sources = tuple(
            (key, rows[key].digest)
            for key in sorted(rows, key=lambda key: (key.node_type, key.node_id))
        )
        merge = ExplicitMerge(sources, "a", "Person", None, None)
        with pytest.raises(GraphMigrationConflict, match="different source property values"):
            store.migrate_graph_identity(
                DryRunMigrationRequest(inventory.source_fingerprint, mappings=(merge,))
            )
        valid = DryRunMigrationRequest(
            inventory.source_fingerprint,
            mappings=(merge,),
            property_resolutions=(
                PropertyResolution(rows[LegacyNodeKey("Agent", "a")].key, "name", "agent", "agent_name"),
                PropertyResolution(rows[LegacyNodeKey("Person", "a")].key, "name", "person", "person_name"),
            ),
        )
        dry = store.migrate_graph_identity(valid)
        assert dry.property_loss == 0
        assert dry.phase == "dry_run"
        cases = (
            PropertyResolution(LegacyNodeKey("Agent", "a"), "missing", "x", "x"),
            PropertyResolution(LegacyNodeKey("Agent", "a"), "name", "agent", "id"),
            PropertyResolution(LegacyNodeKey("Agent", "a"), "name", "agent", "shared"),
            PropertyResolution(LegacyNodeKey("Person", "a"), "name", "person", "shared"),
        )
        for resolution in cases:
            with pytest.raises(GraphMigrationConflict):
                store.migrate_graph_identity(
                    DryRunMigrationRequest(
                        inventory.source_fingerprint,
                        mappings=(merge,),
                        property_resolutions=valid.property_resolutions + (resolution,),
                    )
                )
    finally:
        store.close()
        fixture.close()


def test_sqlite_dry_run_has_zero_graph_and_ledger_mutation_and_canonical_plan() -> None:
    fixture, store = _seed_duplicate_fixture()
    try:
        before_graph = graph_snapshot(fixture.db_path)
        before_schema = schema_snapshot(fixture.db_path)
        _inventory, request, first = _merge_request(store)
        second = store.migrate_graph_identity(request)
        assert first.phase == "dry_run"
        assert first.plan_bytes == second.plan_bytes
        assert first.plan_sha256 == second.plan_sha256
        assert first.canonical_bytes == second.canonical_bytes
        assert first.property_loss == 0
        assert graph_snapshot(fixture.db_path) == before_graph
        assert schema_snapshot(fixture.db_path) == before_schema
        with sqlite3.connect(fixture.db_path) as conn:
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_migration_receipts'"
            ).fetchone() is None
    finally:
        store.close()
        fixture.close()


def test_sqlite_apply_cas_cutover_and_reopen_readback(tmp_path: Path) -> None:
    fixture, store = _seed_duplicate_fixture()
    artifact = tmp_path / "issue80-backup.bin"
    artifact.write_bytes(b"operator backup\n")
    try:
        _inventory, _request, dry = _merge_request(store)
        request = _apply_request(dry, artifact)
        before_graph = graph_snapshot(fixture.db_path)
        before_schema = schema_snapshot(fixture.db_path)
        with pytest.raises(GraphMigrationConflict, match="plan SHA"):
            store.migrate_graph_identity(
                ApplyMigrationRequest(
                    request.request_id,
                    request.expected_source_fingerprint,
                    request.plan_bytes,
                    "0" * 64,
                    request.backup_path,
                    request.backup_sha256,
                )
            )
        assert graph_snapshot(fixture.db_path) == before_graph
        assert schema_snapshot(fixture.db_path) == before_schema
        with pytest.raises(GraphMigrationConflict, match="backup SHA"):
            store.migrate_graph_identity(
                ApplyMigrationRequest(
                    f"{request.request_id}-bad-backup",
                    request.expected_source_fingerprint,
                    request.plan_bytes,
                    request.plan_sha256,
                    request.backup_path,
                    "0" * 64,
                )
            )
        assert graph_snapshot(fixture.db_path) == before_graph
        receipt = store.migrate_graph_identity(request)
        assert receipt.phase == "apply"
        assert store.schema_state == "target"
        assert store.get_node("Person", "a") is not None
        assert store.get_node("Agent", "a") is None
        assert store.get_node("Person", "b") is not None
        assert store.get_edge("Person", "a", "knows", "Person", "b") is not None
        store.close()
        store = LocalGraphStore(str(fixture.db_path))
        assert store.schema_state == "target"
        assert store.get_node("Person", "a") is not None
        assert store.graph_fingerprint() == receipt.target_fingerprint_after
    finally:
        store.close()
        fixture.close()


def test_sqlite_trigger_rollback_and_unknown_commit_recover_after_close_reopen(tmp_path: Path) -> None:
    fixture, store = _seed_duplicate_fixture()
    artifact = tmp_path / "issue80-trigger-backup.bin"
    artifact.write_bytes(b"operator backup\n")
    try:
        _inventory, _request, dry = _merge_request(store)
        request = _apply_request(dry, artifact)
        fixture.create_receipt_insert_abort_trigger()
        baseline_graph = graph_snapshot(fixture.db_path)
        baseline_schema = schema_snapshot(fixture.db_path)
        with pytest.raises(Exception, match="issue80 injected receipt failure"):
            store.migrate_graph_identity(request)
        assert graph_snapshot(fixture.db_path) == baseline_graph
        assert schema_snapshot(fixture.db_path) == baseline_schema
        store.close()
        fixture.drop_receipt_insert_abort_trigger()

        store = LocalGraphStore(str(fixture.db_path))
        original_run = store._run_graph_tx

        def lose_response(callback: Any, **kwargs: Any) -> Any:
            original_run(callback, **kwargs)
            raise ConnectionError("issue80 response lost after commit")

        store._run_graph_tx = lose_response  # type: ignore[method-assign]
        with pytest.raises(ConnectionError, match="response lost"):
            store.migrate_graph_identity(request)
        store.close()
        store = LocalGraphStore(str(fixture.db_path))
        assert store.schema_state == "target"
        committed = _ledger_row(fixture.db_path)
        replay = store.migrate_graph_identity(request)
        assert replay.phase == "apply"
        assert replay.request_id == committed[0]
        assert replay.request_digest == committed[2]
        assert replay.source_fingerprint == committed[3]
        assert replay.mapping_fingerprint == committed[4]
        assert replay.plan_sha256 == committed[5]
        assert replay.target_fingerprint_before == committed[6]
        assert replay.target_fingerprint_after == committed[7]
        assert replay.edge_loss == committed[8]
        assert replay.property_loss == committed[9]
        # The ledger stores the canonical receipt zlib-compressed; the
        # invariant under test is that the replay equals the stored receipt.
        stored_receipt = store._decode_ledger_receipt(committed[10])
        assert bytes(committed[10]).startswith(b"zlib\0")
        # A truncated or extended stored value must not decode to a receipt.
        with pytest.raises(GraphMigrationConflict, match="ledger receipt is malformed"):
            store._decode_ledger_receipt(bytes(committed[10]) + b"garbage")
        with pytest.raises(GraphMigrationConflict, match="ledger receipt is malformed"):
            store._decode_ledger_receipt(bytes(committed[10])[:-8])
        assert replay.canonical_bytes == stored_receipt
        assert replay.receipt_sha256 == receipt_sha256(stored_receipt)
        with sqlite3.connect(fixture.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_migration_receipts").fetchone()[0] == 1
        post = store.upsert_node("Person", "post-replay", {"name": "ok"})
        assert post["id"] == "post-replay"
        assert store.get_node("Person", "post-replay") is not None
        receipt = store.migrate_graph_identity(request)
        store.close()
        fixture.drop_required_graph_index()
        store = LocalGraphStore(str(fixture.db_path))
        assert receipt.phase == "apply"
        assert store.schema_state == "partial_or_unknown"
        with pytest.raises(GraphSchemaMigrationRequired):
            store.upsert_node("Person", "blocked", {"name": "blocked"})
        with sqlite3.connect(fixture.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM graph_migration_receipts").fetchone()[0] == 1
        assert schema_snapshot(fixture.db_path)["residue"] == []
    finally:
        store.close()
        fixture.close()
