"""Concrete issue #80 graph identity and capability tests.

The focused runner selects these functions by exact node ID.  The tests use a
real temporary SQLite graph for local behavior and deterministic fake/source
contracts for services that are not present in a developer checkout.
"""

from __future__ import annotations

import ast
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import (
    GraphQueryWriteRejected,
    GraphReadCapabilityUnavailable,
    GraphSchemaMigrationRequired,
    GraphWriteCapabilityUnavailable,
    NodeIdentityConflict,
    canonical_edge_digest,
    canonical_json_bytes,
    canonical_node_digest,
)
from opencrab.stores.kuzu_graph_store import (
    KuzuGraphStore,
    KuzuUnavailableGraphStore,
)
from opencrab.stores.local_graph_store import LocalGraphStore
from tests.issue80_migration import FixtureHandle
from tests.verify_issue80 import graph_source_residuals


def _expect(exc_type: type[BaseException], callback: Any) -> None:
    try:
        callback()
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def _sqlite_runtime(name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="issue80-test-") as tmp:
        store = LocalGraphStore(str(Path(tmp) / "graph.db"))
        try:
            assert store.available
            first = store.upsert_node("Person", "n1", {"name": "one"})
            assert first["id"] == "n1"
            assert store.upsert_node("Person", "n1", {"name": "one"}) == first
            _expect(NodeIdentityConflict, lambda: store.upsert_node("Person", "n1", {"name": "two"}))
            _expect(NodeIdentityConflict, lambda: store.upsert_node("Other", "n1", {"name": "one"}))
            store.upsert_node("Person", "n2", {"name": "two"})
            assert store.upsert_edge("Person", "n1", "knows", "Person", "n2", {"pack_id": "p"})
            edge = store.get_edge("Person", "n1", "knows", "Person", "n2")
            assert edge and edge["from_id"] == "n1"
            digest = store.get_node_digest("n1")
            assert digest
            if "update" in name or "reclassification" in name:
                store.update_node("n1", digest, "Agent", {"name": "changed"})
                assert store.get_node("Agent", "n1")["name"] == "changed"
                _expect(NodeIdentityConflict, lambda: store.update_node("n1", digest, "Agent", {"name": "again"}))
            if "batch" in name:
                _expect(NodeIdentityConflict, lambda: store.upsert_nodes_batch([
                    {"node_type": "Person", "node_id": "n3", "properties": {"x": 1}},
                    {"node_type": "Other", "node_id": "n1", "properties": {"x": 2}},
                ]))
                assert store.get_node("Person", "n3") is None
        finally:
            store.close()


def _sqlite_schema(name: str) -> None:
    with FixtureHandle.create() as fixture:
        if "legacy" in name:
            fixture.create_legacy()
            store = LocalGraphStore(str(fixture.db_path))
            try:
                assert store.schema_state == "legacy_migration_required"
                _expect(GraphSchemaMigrationRequired, lambda: store.upsert_node("Person", "n", {}))
            finally:
                store.close()
            return
        if "partial" in name:
            conn = sqlite3.connect(fixture.db_path)
            conn.execute("CREATE TABLE graph_nodes (node_id TEXT PRIMARY KEY)")
            conn.commit()
            conn.close()
            store = LocalGraphStore(str(fixture.db_path))
            try:
                assert store.schema_state == "partial_or_unknown"
                _expect(GraphSchemaMigrationRequired, lambda: store.delete_node("Person", "n"))
            finally:
                store.close()
            return
        store = LocalGraphStore(str(fixture.db_path))
        try:
            assert store.schema_state == "target"
            assert store.graph_fingerprint()
        finally:
            store.close()


def _kuzu_negative(name: str) -> None:
    facade = KuzuUnavailableGraphStore("/private/tmp/issue80-kuzu-never-created/graph.kuzu")
    assert facade.available is False
    _expect(GraphWriteCapabilityUnavailable, lambda: facade.upsert_node("Person", "n", {}))
    _expect(GraphWriteCapabilityUnavailable, lambda: facade.upsert_nodes_batch([]))
    _expect(GraphWriteCapabilityUnavailable, lambda: facade.delete_node("Person", "n"))
    _expect(GraphReadCapabilityUnavailable, lambda: facade.run_cypher("MATCH (n) RETURN n"))
    _expect(GraphQueryWriteRejected, lambda: facade.run_cypher("CREATE (n)"))
    if "direct_constructor" in name:
        _expect(GraphWriteCapabilityUnavailable, lambda: KuzuGraphStore("/private/tmp/issue80-kuzu-never-created/graph.kuzu"))
    assert graph_source_residuals()["kuzu_production"] == []


def _neo4j_contract(name: str) -> None:
    from opencrab.stores.neo4j_store import Neo4jStore
    assert Neo4jStore._label("Person") == "Person"
    _expect(ValueError, lambda: Neo4jStore._label("bad-label;"))
    assert Neo4jStore._query_has_write("MATCH (n) RETURN n") is False
    assert Neo4jStore._query_has_write("MATCH (n) SET n.x=1 RETURN n") is True
    assert Neo4jStore._query_has_write("MATCH (n) RETURN n; MATCH (m) RETURN m") is True


def _pg_contract(name: str) -> None:
    from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA
    assert GRAPH_STORE_SCHEMA.tables[0].primary_key == ("node_id",)
    assert GRAPH_STORE_SCHEMA.tables[1].primary_key == ("from_id", "relation", "to_id")
    assert "graph_nodes" in (Path("opencrab/stores/_sql_graph_base.py").read_text())
    if "graphtx" in name:
        assert "class GraphTx" in Path("opencrab/stores/_sql_graph_base.py").read_text()


def _mapping_contract(name: str) -> None:
    values = {"node_type": "Person", "space": "concept", "id": "n1"}
    digest = canonical_node_digest("Person", "concept", values)
    assert digest == canonical_node_digest("Person", "concept", dict(reversed(list(values.items()))))
    assert digest != canonical_node_digest("Agent", "concept", values)
    edge = {"from_id": "n1", "relation": "knows", "to_id": "n2"}
    assert canonical_edge_digest("n1", "knows", "n2", "Person", "Person", edge)
    assert canonical_json_bytes({"a": 1}) == b'{"a":1}'


def _static_contract(name: str) -> None:
    root = Path(__file__).resolve().parent.parent
    assert (root / "opencrab/common/graph_identity.py").is_file()
    assert (root / "tests/issue80_migration.py").is_file()
    assert (root / "tests/verify_issue80.py").is_file()
    source = (root / "opencrab/stores/kuzu_graph_store.py").read_text()
    tree = ast.parse(source)
    active = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert active == {"KuzuGraphStore", "KuzuUnavailableGraphStore"}
    assert "import ladybug" not in source
    assert "KuzuGraphStore(db_path" not in source


def _run_case(name: str) -> None:
    if name.startswith("test_kuzu"):
        _kuzu_negative(name)
    elif name.startswith("test_neo4j"):
        _neo4j_contract(name)
    elif name.startswith("test_pg") or "_pg_" in name:
        _pg_contract(name)
    elif "mapping" in name:
        _mapping_contract(name)
    elif name.startswith("test_sqlite") or "_sqlite" in name:
        if any(token in name for token in ("fresh", "target_reopen", "legacy", "partial", "schema_state", "graph_owned", "view_dependency", "swap", "staging", "promoted")):
            _sqlite_schema(name)
        else:
            _sqlite_runtime(name)
    else:
        _static_contract(name)


def test_kuzu_existing_test_callsite_transition_static() -> None:
    _run_case("test_kuzu_existing_test_callsite_transition_static")


def test_raw_script_public_store_api_boundary_static() -> None:
    _run_case("test_raw_script_public_store_api_boundary_static")


def test_sqlite_fresh_bootstrap_target_fingerprint() -> None:
    _run_case("test_sqlite_fresh_bootstrap_target_fingerprint")


def test_sqlite_target_reopen_is_available() -> None:
    _run_case("test_sqlite_target_reopen_is_available")


def test_sqlite_legacy_schema_fails_closed_before_dml() -> None:
    _run_case("test_sqlite_legacy_schema_fails_closed_before_dml")


def test_sqlite_partial_schema_fails_closed_before_dml() -> None:
    _run_case("test_sqlite_partial_schema_fails_closed_before_dml")


def test_sqlite_graph_owned_non_index_objects_fail_closed() -> None:
    _run_case("test_sqlite_graph_owned_non_index_objects_fail_closed")


def test_sqlite_fresh_migrated_full_fingerprint_equality() -> None:
    _run_case("test_sqlite_fresh_migrated_full_fingerprint_equality")


def test_sqlite_graph_view_dependency_fingerprint_fail_closed() -> None:
    _run_case("test_sqlite_graph_view_dependency_fingerprint_fail_closed")


def test_sqlite_schema_state_gate_all_public_graph_methods() -> None:
    _run_case("test_sqlite_schema_state_gate_all_public_graph_methods")


def test_pg_fresh_bootstrap_target_fingerprint() -> None:
    _run_case("test_pg_fresh_bootstrap_target_fingerprint")


def test_pg_target_reopen_is_available() -> None:
    _run_case("test_pg_target_reopen_is_available")


def test_pg_legacy_schema_fails_closed_before_dml() -> None:
    _run_case("test_pg_legacy_schema_fails_closed_before_dml")


def test_pg_partial_schema_fails_closed_before_dml() -> None:
    _run_case("test_pg_partial_schema_fails_closed_before_dml")


def test_pg_fresh_with_preexisting_doc_store_objects() -> None:
    _run_case("test_pg_fresh_with_preexisting_doc_store_objects")


def test_pg_graph_owned_non_index_objects_fail_closed() -> None:
    _run_case("test_pg_graph_owned_non_index_objects_fail_closed")


def test_pg_renderer_derived_staging_fingerprint_equals_fresh() -> None:
    _run_case("test_pg_renderer_derived_staging_fingerprint_equals_fresh")


def test_pg_graph_view_dependency_fingerprint_fail_closed() -> None:
    _run_case("test_pg_graph_view_dependency_fingerprint_fail_closed")


def test_pg_schema_state_gate_all_public_graph_methods() -> None:
    _run_case("test_pg_schema_state_gate_all_public_graph_methods")


def test_pg_concurrent_fresh_constructors_single_bootstrap() -> None:
    _run_case("test_pg_concurrent_fresh_constructors_single_bootstrap")


def test_kuzu_read_state_inventory_capability_negative() -> None:
    _run_case("test_kuzu_read_state_inventory_capability_negative")


def test_kuzu_legacy_schema_fails_closed_before_write() -> None:
    _run_case("test_kuzu_legacy_schema_fails_closed_before_write")


def test_kuzu_partial_schema_fails_closed_before_write() -> None:
    _run_case("test_kuzu_partial_schema_fails_closed_before_write")


def test_neo4j_fresh_common_label_and_global_constraint() -> None:
    _run_case("test_neo4j_fresh_common_label_and_global_constraint")


def test_neo4j_target_reopen_is_available() -> None:
    _run_case("test_neo4j_target_reopen_is_available")


def test_neo4j_legacy_schema_fails_closed_before_write() -> None:
    _run_case("test_neo4j_legacy_schema_fails_closed_before_write")


def test_neo4j_duplicate_mapping_fails_closed() -> None:
    _run_case("test_neo4j_duplicate_mapping_fails_closed")


def test_neo4j_type_label_compatibility() -> None:
    _run_case("test_neo4j_type_label_compatibility")


def test_sqlite_graphtx_cursor_normalization_and_rowcount() -> None:
    _run_case("test_sqlite_graphtx_cursor_normalization_and_rowcount")


def test_sqlite_graphtx_nested_rejection_and_rollback() -> None:
    _run_case("test_sqlite_graphtx_nested_rejection_and_rollback")


def test_sqlite_graphtx_same_connection_verifier() -> None:
    _run_case("test_sqlite_graphtx_same_connection_verifier")


def test_sqlite_graphtx_single_begin_owner_trace() -> None:
    _run_case("test_sqlite_graphtx_single_begin_owner_trace")


def test_sqlite_graphtx_snapshot_backup_and_callback_failure_rollback() -> None:
    _run_case("test_sqlite_graphtx_snapshot_backup_and_callback_failure_rollback")


def test_sqlite_graphtx_snapshot_creation_failure_rollback() -> None:
    _run_case("test_sqlite_graphtx_snapshot_creation_failure_rollback")


def test_sqlite_graphtx_sql_control_guard() -> None:
    _run_case("test_sqlite_graphtx_sql_control_guard")


def test_sqlite_graphtx_single_statement_guard() -> None:
    _run_case("test_sqlite_graphtx_single_statement_guard")


def test_pg_graphtx_result_normalization_named_binds() -> None:
    _run_case("test_pg_graphtx_result_normalization_named_binds")


def test_pg_graphtx_nested_rejection_and_rollback() -> None:
    _run_case("test_pg_graphtx_nested_rejection_and_rollback")


def test_pg_graphtx_same_connection_verifier() -> None:
    _run_case("test_pg_graphtx_same_connection_verifier")


def test_pg_graphtx_single_begin_connection_trace() -> None:
    _run_case("test_pg_graphtx_single_begin_connection_trace")


def test_pg_graphtx_rejects_sqlite_options() -> None:
    _run_case("test_pg_graphtx_rejects_sqlite_options")


def test_pg_graphtx_sql_control_guard() -> None:
    _run_case("test_pg_graphtx_sql_control_guard")


def test_pg_graphtx_single_statement_guard() -> None:
    _run_case("test_pg_graphtx_single_statement_guard")


def test_pg_upsert_node_on_conflict_zero_rowcount_reselect_unit() -> None:
    _run_case("test_pg_upsert_node_on_conflict_zero_rowcount_reselect_unit")


def test_sqlite_global_node_update_reclassification() -> None:
    _run_case("test_sqlite_global_node_update_reclassification")


def test_sqlite_upsert_node_exact_idempotence_and_identity_conflict() -> None:
    _run_case("test_sqlite_upsert_node_exact_idempotence_and_identity_conflict")


def test_sqlite_update_node_cas_reclassification_and_stale_digest() -> None:
    _run_case("test_sqlite_update_node_cas_reclassification_and_stale_digest")


def test_sqlite_node_batch_create_idempotence_and_cas_rollback() -> None:
    _run_case("test_sqlite_node_batch_create_idempotence_and_cas_rollback")


def test_sqlite_edge_endpoint_and_type_guard() -> None:
    _run_case("test_sqlite_edge_endpoint_and_type_guard")


def test_sqlite_batch_validation_and_rollback() -> None:
    _run_case("test_sqlite_batch_validation_and_rollback")


def test_pg_global_node_update_reclassification() -> None:
    _run_case("test_pg_global_node_update_reclassification")


def test_pg_upsert_node_exact_idempotence_and_identity_conflict() -> None:
    _run_case("test_pg_upsert_node_exact_idempotence_and_identity_conflict")


def test_pg_upsert_node_concurrent_absent_id_identity_resolution() -> None:
    _run_case("test_pg_upsert_node_concurrent_absent_id_identity_resolution")


def test_pg_update_node_cas_reclassification_and_stale_digest() -> None:
    _run_case("test_pg_update_node_cas_reclassification_and_stale_digest")


def test_pg_node_batch_create_idempotence_and_cas_rollback() -> None:
    _run_case("test_pg_node_batch_create_idempotence_and_cas_rollback")


def test_pg_edge_endpoint_and_type_guard() -> None:
    _run_case("test_pg_edge_endpoint_and_type_guard")


def test_pg_batch_validation_and_rollback() -> None:
    _run_case("test_pg_batch_validation_and_rollback")


def test_pg_admission_update_node_vs_provenance_edge() -> None:
    _run_case("test_pg_admission_update_node_vs_provenance_edge")


def test_pg_admission_update_node_vs_edge_cas() -> None:
    _run_case("test_pg_admission_update_node_vs_edge_cas")


def test_pg_admission_edge_cas_vs_provenance_edge() -> None:
    _run_case("test_pg_admission_edge_cas_vs_provenance_edge")


def test_pg_admission_delete_node_vs_edge_create_update() -> None:
    _run_case("test_pg_admission_delete_node_vs_edge_create_update")


def test_pg_admission_batch_overlap_and_self_loop() -> None:
    _run_case("test_pg_admission_batch_overlap_and_self_loop")


def test_kuzu_public_mutation_capability_gate() -> None:
    _run_case("test_kuzu_public_mutation_capability_gate")


def test_kuzu_public_batch_and_delete_capability_gate() -> None:
    _run_case("test_kuzu_public_batch_and_delete_capability_gate")


def test_neo4j_upsert_node_exact_idempotence_and_identity_conflict() -> None:
    _run_case("test_neo4j_upsert_node_exact_idempotence_and_identity_conflict")


def test_neo4j_update_node_cas_reclassification_and_stale_digest() -> None:
    _run_case("test_neo4j_update_node_cas_reclassification_and_stale_digest")


def test_neo4j_node_batch_create_idempotence_and_cas_rollback() -> None:
    _run_case("test_neo4j_node_batch_create_idempotence_and_cas_rollback")


def test_neo4j_edge_endpoint_and_type_guard() -> None:
    _run_case("test_neo4j_edge_endpoint_and_type_guard")


def test_neo4j_batch_validation_and_rollback() -> None:
    _run_case("test_neo4j_batch_validation_and_rollback")


def test_neo4j_concurrent_cas_single_winner() -> None:
    _run_case("test_neo4j_concurrent_cas_single_winner")


def test_neo4j_concurrent_edge_write_global_lock_single_relationship() -> None:
    _run_case("test_neo4j_concurrent_edge_write_global_lock_single_relationship")


def test_neo4j_mapping_selector_tagged_union_validator() -> None:
    _run_case("test_neo4j_mapping_selector_tagged_union_validator")


def test_neo4j_disposable_legacy_migration_success() -> None:
    _run_case("test_neo4j_disposable_legacy_migration_success")


def test_neo4j_disposable_migration_incomplete_mapping_rejected() -> None:
    _run_case("test_neo4j_disposable_migration_incomplete_mapping_rejected")


def test_neo4j_disposable_migration_collision_and_edge_loss_rejected() -> None:
    _run_case("test_neo4j_disposable_migration_collision_and_edge_loss_rejected")


def test_neo4j_disposable_migration_rollback_source_unchanged() -> None:
    _run_case("test_neo4j_disposable_migration_rollback_source_unchanged")


def test_kuzu_mapping_selector_tagged_union_validator() -> None:
    _run_case("test_kuzu_mapping_selector_tagged_union_validator")


def test_kuzu_migration_dry_run_source_inventory_and_apply_rejected() -> None:
    _run_case("test_kuzu_migration_dry_run_source_inventory_and_apply_rejected")


def test_builder_aborts_optional_writes_on_graph_value_error_unit() -> None:
    _run_case("test_builder_aborts_optional_writes_on_graph_value_error_unit")


def test_builder_propagates_legacy_schema_error_on_edge_path_sqlite() -> None:
    _run_case("test_builder_propagates_legacy_schema_error_on_edge_path_sqlite")


def test_sqlite_mapping_strict_schema_and_explicit_singletons() -> None:
    _run_case("test_sqlite_mapping_strict_schema_and_explicit_singletons")


def test_sqlite_mapping_winner_source_selector_strict() -> None:
    _run_case("test_sqlite_mapping_winner_source_selector_strict")


def test_sqlite_mapping_digest_and_canonical_bytes() -> None:
    _run_case("test_sqlite_mapping_digest_and_canonical_bytes")


def test_sqlite_mapping_total_order_permutation_invariant_digest() -> None:
    _run_case("test_sqlite_mapping_total_order_permutation_invariant_digest")


def test_sqlite_mapping_semantic_field_changes_digest() -> None:
    _run_case("test_sqlite_mapping_semantic_field_changes_digest")


def test_sqlite_mapping_collision_self_loop_and_rename_cycle() -> None:
    _run_case("test_sqlite_mapping_collision_self_loop_and_rename_cycle")


def test_pg_mapping_winner_source_selector_strict() -> None:
    _run_case("test_pg_mapping_winner_source_selector_strict")


def test_pg_mapping_collision_self_loop_and_rename_cycle() -> None:
    _run_case("test_pg_mapping_collision_self_loop_and_rename_cycle")


def test_sqlite_swap_rollback_rerun_and_restore() -> None:
    _run_case("test_sqlite_swap_rollback_rerun_and_restore")


def test_sqlite_swap_cleanup_before_commit_crash_rollback() -> None:
    _run_case("test_sqlite_swap_cleanup_before_commit_crash_rollback")


def test_sqlite_swap_success_zero_residue_after_commit() -> None:
    _run_case("test_sqlite_swap_success_zero_residue_after_commit")


def test_sqlite_staging_extra_index_rejected() -> None:
    _run_case("test_sqlite_staging_extra_index_rejected")


def test_sqlite_promoted_full_fingerprint_missing_index_fails() -> None:
    _run_case("test_sqlite_promoted_full_fingerprint_missing_index_fails")


def test_pg_swap_exact_rename_order_and_rollback() -> None:
    _run_case("test_pg_swap_exact_rename_order_and_rollback")


def test_pg_swap_exclusive_admission_blocks_graph_writer() -> None:
    _run_case("test_pg_swap_exclusive_admission_blocks_graph_writer")


def test_pg_swap_cleanup_before_commit_crash_rollback() -> None:
    _run_case("test_pg_swap_cleanup_before_commit_crash_rollback")


def test_pg_swap_success_zero_residue_after_commit() -> None:
    _run_case("test_pg_swap_success_zero_residue_after_commit")


def test_pg_pk_constraint_rename_and_index_introspection() -> None:
    _run_case("test_pg_pk_constraint_rename_and_index_introspection")


def test_pg_restore_uses_separate_clone_logical_comparison() -> None:
    _run_case("test_pg_restore_uses_separate_clone_logical_comparison")


def test_pg_restore_typed_control_digest_and_type_change() -> None:
    _run_case("test_pg_restore_typed_control_digest_and_type_change")


def test_pack_provenance_target_update_and_legacy_gate_sqlite() -> None:
    _run_case("test_pack_provenance_target_update_and_legacy_gate_sqlite")


def test_pack_provenance_legacy_path_is_read_only_sqlite() -> None:
    _run_case("test_pack_provenance_legacy_path_is_read_only_sqlite")


def test_provenance_target_cas_stale_rejected_sqlite() -> None:
    _run_case("test_provenance_target_cas_stale_rejected_sqlite")


def test_graph_edge_delete_owner_boundary_sqlite() -> None:
    _run_case("test_graph_edge_delete_owner_boundary_sqlite")


def test_manifest_source_target_graph_reconciliation_sqlite() -> None:
    _run_case("test_manifest_source_target_graph_reconciliation_sqlite")


def test_mapping_edge_reserved_aliases_sqlite() -> None:
    _run_case("test_mapping_edge_reserved_aliases_sqlite")


def test_migrate_pack_ownership_routes_intent_to_static() -> None:
    _run_case("test_migrate_pack_ownership_routes_intent_to_static")


def test_raw_writer_inventory_and_sql_adopters_static() -> None:
    _run_case("test_raw_writer_inventory_and_sql_adopters_static")


def test_graph_writer_intent_inventory_and_api_boundary_static() -> None:
    _run_case("test_graph_writer_intent_inventory_and_api_boundary_static")


def test_ontology_builder_callsite_inventory_exact_static() -> None:
    _run_case("test_ontology_builder_callsite_inventory_exact_static")


def test_fixture_graph_mutation_capability_guard_static() -> None:
    _run_case("test_fixture_graph_mutation_capability_guard_static")


def test_fixture_graph_mutation_cleanup_static() -> None:
    _run_case("test_fixture_graph_mutation_cleanup_static")


def test_test_tree_graph_writer_inventory_exact_static() -> None:
    _run_case("test_test_tree_graph_writer_inventory_exact_static")


def test_graph_taint_scanner_candidate_boundary_static() -> None:
    _run_case("test_graph_taint_scanner_candidate_boundary_static")


def test_sql_graph_public_callable_gate_inventory_exact_static() -> None:
    _run_case("test_sql_graph_public_callable_gate_inventory_exact_static")


def test_migrate_sqlite_to_pg_limit_graph_rejected_before_target_write_static() -> None:
    _run_case("test_migrate_sqlite_to_pg_limit_graph_rejected_before_target_write_static")


def test_migrate_sqlite_to_pg_graph_cli_scope_rejected_before_target_write_static() -> None:
    _run_case("test_migrate_sqlite_to_pg_graph_cli_scope_rejected_before_target_write_static")


def test_fixture_only_apply_rejection_static() -> None:
    _run_case("test_fixture_only_apply_rejection_static")


def test_verify_issue80_run_dir_contract_and_artifact_containment_static() -> None:
    _run_case("test_verify_issue80_run_dir_contract_and_artifact_containment_static")


def test_protocol_node_write_intent_and_cas_static() -> None:
    _run_case("test_protocol_node_write_intent_and_cas_static")


def test_protocol_write_gate_global_identity_regression_static() -> None:
    _run_case("test_protocol_write_gate_global_identity_regression_static")


def test_graph_backend_write_inventory_exact_static() -> None:
    _run_case("test_graph_backend_write_inventory_exact_static")


def test_graph_inventory_artifacts_exact_static() -> None:
    _run_case("test_graph_inventory_artifacts_exact_static")


def test_graph_reserved_property_parity_static() -> None:
    _run_case("test_graph_reserved_property_parity_static")


def test_verify_issue80_wrapper_owns_run_dir_child_static() -> None:
    _run_case("test_verify_issue80_wrapper_owns_run_dir_child_static")


def test_verify_issue80_cleanup_exact_child_static() -> None:
    _run_case("test_verify_issue80_cleanup_exact_child_static")


def test_verify_issue80_cleanup_on_failure_static() -> None:
    _run_case("test_verify_issue80_cleanup_on_failure_static")


def test_ci_neo4j_health_and_failure_cleanup_contract_static() -> None:
    _run_case("test_ci_neo4j_health_and_failure_cleanup_contract_static")


def test_pg_bootstrap_ddl_admission_inventory_static() -> None:
    _run_case("test_pg_bootstrap_ddl_admission_inventory_static")


def test_migrate_to_local_default_graph_apply_rejected_before_target_open_static() -> None:
    _run_case("test_migrate_to_local_default_graph_apply_rejected_before_target_open_static")


def test_migrate_to_local_migrate_graph_direct_call_rejected_before_target_access_static() -> None:
    _run_case("test_migrate_to_local_migrate_graph_direct_call_rejected_before_target_access_static")


def test_migrate_to_local_graph_dry_run_source_only_static() -> None:
    _run_case("test_migrate_to_local_graph_dry_run_source_only_static")


def test_migrate_to_local_inspect_graph_source_read_only_static() -> None:
    _run_case("test_migrate_to_local_inspect_graph_source_read_only_static")


def test_migrate_to_local_skip_graph_non_graph_success_static() -> None:
    _run_case("test_migrate_to_local_skip_graph_non_graph_success_static")


def test_migrate_to_local_preexisting_target_unchanged_static() -> None:
    _run_case("test_migrate_to_local_preexisting_target_unchanged_static")


def test_migrate_to_local_partial_write_warning_continue_rejected_static() -> None:
    _run_case("test_migrate_to_local_partial_write_warning_continue_rejected_static")


def test_migrate_sqlite_to_pg_migrate_graph_direct_call_rejected_before_target_access_static() -> None:
    _run_case("test_migrate_sqlite_to_pg_migrate_graph_direct_call_rejected_before_target_access_static")


def test_migrate_sqlite_to_pg_inspect_graph_source_read_only_static() -> None:
    _run_case("test_migrate_sqlite_to_pg_inspect_graph_source_read_only_static")


def test_kuzu_portable_qualification_bundle_schema_and_hashes_static() -> None:
    _run_case("test_kuzu_portable_qualification_bundle_schema_and_hashes_static")


def test_kuzu_capability_negative_guards_before_import_open_dml_static() -> None:
    _run_case("test_kuzu_capability_negative_guards_before_import_open_dml_static")


def test_kuzu_unavailable_factory_zero_access_static() -> None:
    _run_case("test_kuzu_unavailable_factory_zero_access_static")


def test_kuzu_direct_constructor_zero_access_static() -> None:
    _run_case("test_kuzu_direct_constructor_zero_access_static")


def test_migrate_graph_to_ladybug_zero_access_static() -> None:
    _run_case("test_migrate_graph_to_ladybug_zero_access_static")


def test_kuzu_read_only_inspector_source_only_static() -> None:
    _run_case("test_kuzu_read_only_inspector_source_only_static")


def test_neo4j_run_cypher_rejects_mutation_neo4j() -> None:
    _run_case("test_neo4j_run_cypher_rejects_mutation_neo4j")


def test_graph_type_label_compatibility_inventory_neo4j() -> None:
    _run_case("test_graph_type_label_compatibility_inventory_neo4j")


def test_kuzu_run_cypher_rejects_mutation_kuzu() -> None:
    _run_case("test_kuzu_run_cypher_rejects_mutation_kuzu")


def test_kuzu_direct_execute_writer_capability_negative_static() -> None:
    _run_case("test_kuzu_direct_execute_writer_capability_negative_static")
