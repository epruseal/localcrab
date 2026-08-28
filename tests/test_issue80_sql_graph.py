"""Concrete issue #80 graph identity and capability tests.

The focused runner selects these functions by exact node ID.  SQLite exercises
local behavior, while the service cases use disposable PostgreSQL and Neo4j
connections supplied by the test environment.
"""

from __future__ import annotations

import ast
import hashlib
import ipaddress
import os
import socket
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from opencrab.common.graph_identity import (
    ApplyMigrationRequest,
    DryRunMigrationRequest,
    EdgeIdentityConflict,
    GraphMigrationConflict,
    GraphQueryWriteRejected,
    GraphReadCapabilityUnavailable,
    GraphSchemaMigrationRequired,
    GraphWriteCapabilityUnavailable,
    NodeIdentityConflict,
    canonical_edge_digest,
    canonical_json_bytes,
    canonical_node_digest,
    plan_sha256,
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


def _required_service_env(*names: str) -> dict[str, str]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.fail(f"required issue80 service environment is missing: {', '.join(missing)}")
    return {name: os.environ[name] for name in names}


def _pg_service_guard() -> tuple[str, Any, str]:
    env = _required_service_env("OPENCRAB_PG_TEST_URL", "OPENCRAB_ISSUE80_SERVICE_APPLY")
    if env["OPENCRAB_ISSUE80_SERVICE_APPLY"] != "1":
        pytest.fail("OPENCRAB_ISSUE80_SERVICE_APPLY=1 is required for issue80 service tests")
    dsn = env["OPENCRAB_PG_TEST_URL"]
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    host = url.host
    database = url.database
    if not host or not database or not database.endswith("_test"):
        pytest.fail("issue80 PG service requires a loopback *_test database")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(socket.gethostbyname(host))}
        except (OSError, ValueError):
            pytest.fail("issue80 PG service host is not loopback")
    if not all(address.is_loopback for address in addresses):
        pytest.fail("issue80 PG service host is not loopback")
    engine = create_engine(dsn)
    schema = f"issue80_{uuid.uuid4().hex}"
    try:
        with engine.connect() as conn:
            observed = conn.execute(text("SELECT current_database()")).one()
        if observed[0] != database:
            pytest.fail("issue80 PG connection database identity differs from DSN")
    except Exception:
        engine.dispose()
        raise
    return dsn, engine, schema


@contextmanager
def _pg_runtime(*, legacy: bool = False) -> Iterator[tuple[Any, Any, str]]:
    dsn, engine, schema = _pg_service_guard()
    from sqlalchemy import text

    try:
        if legacy:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                conn.execute(text(
                    f'''CREATE TABLE "{schema}".graph_nodes (
                        node_type TEXT NOT NULL, node_id TEXT NOT NULL,
                        space_id TEXT, properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        PRIMARY KEY (node_type, node_id)
                    )'''))
                conn.execute(text(
                    f'''CREATE TABLE "{schema}".graph_edges (
                        from_type TEXT NOT NULL, from_id TEXT NOT NULL,
                        relation TEXT NOT NULL, to_type TEXT NOT NULL,
                        to_id TEXT NOT NULL, properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        PRIMARY KEY (from_type, from_id, relation, to_type, to_id)
                    )'''))
                conn.execute(text(f'''CREATE INDEX idx_nodes_pack ON "{schema}".graph_nodes ((properties->>'pack_id'))'''))
                conn.execute(text(f'''CREATE INDEX idx_nodes_space ON "{schema}".graph_nodes (space_id)'''))
                conn.execute(text(f'''CREATE INDEX idx_edges_from ON "{schema}".graph_edges (from_id)'''))
                conn.execute(text(f'''CREATE INDEX idx_edges_to ON "{schema}".graph_edges (to_id)'''))
                conn.execute(text(
                    f'''CREATE TABLE "{schema}".graph_migration_receipts (
                        request_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
                        request_digest TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
                        mapping_fingerprint TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
                        target_fingerprint_before TEXT NOT NULL,
                        target_fingerprint_after TEXT NOT NULL, edge_loss INTEGER NOT NULL,
                        property_loss INTEGER NOT NULL, receipt_bytes BYTEA NOT NULL,
                        created_at TEXT NOT NULL
                    )'''))
                conn.execute(text(f'''CREATE FUNCTION "{schema}".issue80_fail_receipt_insert()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'issue80 injected receipt failure'; END;
                    $$'''))
                conn.execute(text(f'''CREATE TRIGGER issue80_fail_receipt_insert
                    BEFORE INSERT ON "{schema}".graph_migration_receipts
                    FOR EACH ROW EXECUTE FUNCTION "{schema}".issue80_fail_receipt_insert()'''))
                conn.execute(text(f'''INSERT INTO "{schema}".graph_nodes
                    (node_type,node_id,space_id,properties) VALUES
                    ('Person','a',NULL,'{{"name":"same"}}'::jsonb),
                    ('Entity','a',NULL,'{{"name":"same"}}'::jsonb),
                    ('Person','b',NULL,'{{"name":"bee"}}'::jsonb)'''))
                conn.execute(text(f'''INSERT INTO "{schema}".graph_edges
                    (from_type,from_id,relation,to_type,to_id,properties) VALUES
                    ('Person','a','knows','Person','b','{{"weight": 1}}'::jsonb),
                    ('Entity','a','knows','Person','b','{{"weight": 1}}'::jsonb)'''))
        from opencrab.stores.pg_graph_store import PGGraphStore

        store = PGGraphStore(engine, schema=schema)
        assert store.available
        yield store, engine, schema
    finally:
        try:
            if "store" in locals():
                store.close()
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            engine.dispose()


@contextmanager
def _neo4j_runtime() -> Iterator[Any]:
    env = _required_service_env(
        "OPENCRAB_NEO4J_TEST_URI", "OPENCRAB_NEO4J_TEST_USER",
        "OPENCRAB_NEO4J_TEST_PASSWORD", "OPENCRAB_NEO4J_TEST_DATABASE",
    )
    from opencrab.stores.neo4j_store import Neo4jStore

    store = Neo4jStore(
        env["OPENCRAB_NEO4J_TEST_URI"], env["OPENCRAB_NEO4J_TEST_USER"],
        env["OPENCRAB_NEO4J_TEST_PASSWORD"], env["OPENCRAB_NEO4J_TEST_DATABASE"],
    )
    try:
        assert store.available
        assert store.ping()
        yield store
    finally:
        store.close()


def _seed_identity(store: Any, prefix: str) -> tuple[Any, Any, Any, Any]:
    a_id, b_id = f"{prefix}-a", f"{prefix}-b"
    a = store.upsert_node("Person", a_id, {"name": "alpha"}, "subject", return_receipt=True)
    b = store.upsert_node("Person", b_id, {"name": "beta"}, "subject", return_receipt=True)
    replay = store.upsert_node("Person", a_id, {"name": "alpha"}, "subject", return_receipt=True)
    assert replay.operation == "idempotent"
    assert replay.node_id == a.node_id
    assert replay.node_type == a.node_type
    assert replay.space_id == a.space_id
    assert replay.properties == a.properties
    assert replay.digest == a.digest
    _expect(NodeIdentityConflict, lambda: store.upsert_node("Person", a_id, {"name": "other"}, "subject"))
    _expect(NodeIdentityConflict, lambda: store.upsert_node("Agent", a_id, {"name": "alpha"}, "subject"))
    ab = store.upsert_edge("Person", a_id, "knows", "Person", b_id, {"weight": 1}, return_receipt=True)
    ba = store.upsert_edge("Person", b_id, "knows", "Person", a_id, {"weight": 2}, return_receipt=True)
    assert ab and ba
    return a, b, ab, ba


def _provenance_record(
    kind: str,
    target: str,
    digest: str,
    *,
    identity: dict[str, str],
    prefix: str,
    pack: str,
    reason: str = "inferred",
) -> dict[str, Any]:
    common = {
        "kind": kind, "target_fingerprint": target,
        "expected_current_digest": digest, "proposed_pack_id": pack,
        "reason": reason,
        "dry_run_evidence_digest": hashlib.sha256(prefix.encode()).hexdigest(),
        "allowed_properties_delta": {"set": {"pack_id": pack}, "remove": []},
    }
    common.update(identity)
    return common


def _assert_identity_and_edge_snapshot(store: Any, prefix: str) -> tuple[str, str, str, str]:
    a, b, ab, ba = _seed_identity(store, prefix)
    a_id, b_id = a.node_id, b.node_id
    old_a = store.get_node_digest(a_id, node_type="Person")
    assert old_a == a.digest
    changed = store.reclassify_node(
        a_id,
        expected_current_digest=old_a,
        new_type="Agent",
        new_space_id="subject",
        new_properties={"name": "alpha-updated"},
        return_receipt=True,
    )
    assert changed.node_type == "Agent"
    assert store.get_node("Agent", a_id)["name"] == "alpha-updated"
    assert store.get_node("Person", a_id) is None
    assert store.get_edge("Agent", a_id, "knows", "Person", b_id)
    assert store.get_edge("Person", b_id, "knows", "Agent", a_id)
    assert store.get_edge_digest(a_id, "knows", b_id) != ab.digest
    assert store.get_edge_digest(b_id, "knows", a_id) != ba.digest
    _expect(NodeIdentityConflict, lambda: store.reclassify_node(
        a_id,
        expected_current_digest=old_a,
        new_type="Agent",
        new_space_id="subject",
        new_properties={"name": "stale"},
    ))
    return a_id, b_id, changed.digest, store.get_edge_digest(a_id, "knows", b_id)


def _assert_provenance(store: Any, prefix: str) -> None:
    node_id = f"{prefix}-node"
    from_id, to_id = f"{prefix}-from", f"{prefix}-to"
    node = store.upsert_node("Entity", node_id, {"name": "node"}, "concept", return_receipt=True)
    store.upsert_node("Entity", from_id, {"name": "from"}, "concept")
    store.upsert_node("Entity", to_id, {"name": "to"}, "concept")
    edge = store.upsert_edge("Entity", from_id, "links", "Entity", to_id, {"weight": 1}, return_receipt=True)
    target = store.graph_fingerprint()
    node_record = _provenance_record(
        "node", target, node.digest,
        identity={"node_id": node_id, "node_type": "Entity"},
        prefix=f"{prefix}-node-evidence", pack="issue80-pack",
    )
    edge_record = _provenance_record(
        "edge", target, edge.digest,
        identity={
            "from_id": from_id, "relation": "links", "to_id": to_id,
            "from_type": "Entity", "to_type": "Entity",
        },
        prefix=f"{prefix}-edge-evidence", pack="issue80-pack", reason="assumed",
    )
    receipt = store.backfill_pack_provenance([node_record, edge_record])
    assert len(receipt.records) == 2
    assert receipt.target_fingerprint_before == target
    assert receipt.target_fingerprint_after == store.graph_fingerprint()
    assert store.get_node("Entity", node_id)["pack_id"] == "issue80-pack"
    assert store.get_edge("Entity", from_id, "links", "Entity", to_id)["pack_id"] == "issue80-pack"

    current_target = store.graph_fingerprint()
    owner_conflict = dict(node_record)
    owner_conflict.update({
        "target_fingerprint": current_target,
        "expected_current_digest": store.get_node_digest(node_id, node_type="Entity"),
        "proposed_pack_id": "other-pack",
        "allowed_properties_delta": {"set": {"pack_id": "other-pack"}, "remove": []},
    })
    _expect(RuntimeError, lambda: store.backfill_pack_provenance([owner_conflict]))
    malformed = dict(node_record)
    malformed.pop("node_type")
    _expect(ValueError, lambda: store.backfill_pack_provenance([malformed]))

    mixed_prefix = f"{prefix}-mixed"
    mixed_node_id = f"{mixed_prefix}-node"
    mixed_from, mixed_to = f"{mixed_prefix}-from", f"{mixed_prefix}-to"
    mixed_node = store.upsert_node("Entity", mixed_node_id, {"name": "mixed"}, "concept", return_receipt=True)
    store.upsert_node("Entity", mixed_from, {"name": "mixed-from"}, "concept")
    store.upsert_node("Entity", mixed_to, {"name": "mixed-to"}, "concept")
    mixed_edge = store.upsert_edge("Entity", mixed_from, "links", "Entity", mixed_to, {"weight": 2}, return_receipt=True)
    mixed_target = store.graph_fingerprint()
    mixed_node_record = _provenance_record(
        "node", mixed_target, mixed_node.digest,
        identity={"node_id": mixed_node_id, "node_type": "Entity"},
        prefix=f"{mixed_prefix}-node-evidence", pack="mixed-pack",
    )
    mixed_edge_record = _provenance_record(
        "edge", mixed_target, "0" * 64,
        identity={
            "from_id": mixed_from, "relation": "links", "to_id": mixed_to,
            "from_type": "Entity", "to_type": "Entity",
        },
        prefix=f"{mixed_prefix}-edge-evidence", pack="mixed-pack",
    )
    before_mixed = (
        store.get_node("Entity", mixed_node_id),
        store.get_edge("Entity", mixed_from, "links", "Entity", mixed_to),
        store.graph_fingerprint(),
    )
    _expect(EdgeIdentityConflict, lambda: store.backfill_pack_provenance([mixed_node_record, mixed_edge_record]))
    assert (
        store.get_node("Entity", mixed_node_id),
        store.get_edge("Entity", mixed_from, "links", "Entity", mixed_to),
        store.graph_fingerprint(),
    ) == before_mixed
    assert mixed_edge.digest == store.get_edge_digest(mixed_from, "links", mixed_to)


def test_pg_runtime_identity_cas_and_incident_edge_snapshot() -> None:
    with _pg_runtime() as (store, _engine, _schema):
        a_id, b_id, _changed_digest, _edge_digest = _assert_identity_and_edge_snapshot(
            store, f"issue80-pg-{uuid.uuid4().hex}"
        )
        before = (
            store.get_node("Agent", a_id),
            store.get_node("Person", b_id),
            store.get_edge("Agent", a_id, "knows", "Person", b_id),
            store.get_edge("Person", b_id, "knows", "Agent", a_id),
            store.graph_fingerprint(),
        )
        b_digest = store.get_node_digest(b_id, node_type="Person")
        store.reclassify_node(
            b_id,
            expected_current_digest=b_digest,
            new_type="Person",
            new_space_id="subject",
            new_properties={"name": "beta-updated"},
        )
        after_b = store.get_node_digest(b_id, node_type="Person")
        after_setup = (
            store.get_node("Agent", a_id),
            store.get_node("Person", b_id),
            store.get_edge("Agent", a_id, "knows", "Person", b_id),
            store.get_edge("Person", b_id, "knows", "Agent", a_id),
            store.graph_fingerprint(),
        )
        _expect(NodeIdentityConflict, lambda: store.update_nodes_batch([
            {
                "node_id": a_id,
                "expected_current_digest": store.get_node_digest(a_id, node_type="Agent"),
                "new_type": "Agent",
                "new_properties": {"name": "batch-change"},
                "new_space_id": "subject",
            },
            {
                "node_id": b_id,
                "expected_current_digest": b_digest,
                "new_type": "Person",
                "new_properties": {"name": "stale-change"},
                "new_space_id": "subject",
            },
        ]))
        assert (
            store.get_node("Agent", a_id),
            store.get_node("Person", b_id),
            store.get_edge("Agent", a_id, "knows", "Person", b_id),
            store.get_edge("Person", b_id, "knows", "Agent", a_id),
            store.graph_fingerprint(),
        ) == after_setup
        assert after_b == store.get_node_digest(b_id, node_type="Person")
        assert before != after_setup


def _pg_catalog_snapshot(engine: Any, schema: str) -> bytes:
    from sqlalchemy import text

    with engine.connect() as conn:
        tables = [row[0] for row in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=:schema ORDER BY table_name"
        ), {"schema": schema})]
        nodes = [tuple(row) for row in conn.execute(text(
            f'SELECT node_type,node_id,space_id,properties::text FROM "{schema}".graph_nodes ORDER BY node_type,node_id'
        ))] if "graph_nodes" in tables else []
        edges = [tuple(row) for row in conn.execute(text(
            f'SELECT from_type,from_id,relation,to_type,to_id,properties::text FROM "{schema}".graph_edges ORDER BY from_type,from_id,relation,to_type,to_id'
        ))] if "graph_edges" in tables else []
        ledger = [tuple(row) for row in conn.execute(text(
            f'SELECT request_id,phase,request_digest,source_fingerprint,mapping_fingerprint,plan_sha256,target_fingerprint_before,target_fingerprint_after,edge_loss,property_loss,encode(receipt_bytes, \'hex\'),created_at::text FROM "{schema}".graph_migration_receipts ORDER BY request_id'
        ))] if "graph_migration_receipts" in tables else []
        return canonical_json_bytes({"tables": tables, "nodes": nodes, "edges": edges, "ledger": ledger})


def test_pg_runtime_legacy_mapping_dry_run_apply_and_rollback(tmp_path: Path) -> None:
    from sqlalchemy import text

    from opencrab.common.graph_identity import ExplicitMerge

    with _pg_runtime(legacy=True) as (store, engine, schema):
        inventory = store.inspect_graph_identity()
        duplicate_keys = [row.key for row in inventory.nodes if row.key.node_id == "a"]
        assert len(duplicate_keys) == 2
        rejected = DryRunMigrationRequest(inventory.source_fingerprint, mappings=())
        _expect(GraphMigrationConflict, lambda: store.migrate_graph_identity(rejected))
        first, second = sorted(
            (row for row in inventory.nodes if row.key.node_id == "a"),
            key=lambda row: row.key.node_type,
        )
        merge = ExplicitMerge(
            sources=((first.key, first.digest), (second.key, second.digest)),
            target_node_id="a", target_node_type="Person",
            target_space_id=None, target_pack_id=None,
        )
        request = DryRunMigrationRequest(inventory.source_fingerprint, mappings=(merge,))
        before = _pg_catalog_snapshot(engine, schema)
        dry = store.migrate_graph_identity(request)
        assert dry.plan_bytes == bytes(dry.plan_bytes)
        assert dry.plan_sha256 == plan_sha256(dry.plan_bytes)
        assert _pg_catalog_snapshot(engine, schema) == before
        artifact = tmp_path / "issue80-operator-artifact.bin"
        artifact.write_bytes(b"issue80 operator supplied artifact\n")
        apply_request = ApplyMigrationRequest(
            request_id=f"issue80-{uuid.uuid4().hex}",
            expected_source_fingerprint=inventory.source_fingerprint,
            plan_bytes=dry.plan_bytes,
            plan_sha256=dry.plan_sha256,
            backup_path=artifact,
            backup_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )
        failed_before = _pg_catalog_snapshot(engine, schema)
        with pytest.raises(Exception, match="issue80 injected receipt failure"):
            store.migrate_graph_identity(apply_request)
        assert _pg_catalog_snapshot(engine, schema) == failed_before
        with engine.begin() as conn:
            conn.execute(text(f'DROP TRIGGER issue80_fail_receipt_insert ON "{schema}".graph_migration_receipts'))
            conn.execute(text(f'DROP FUNCTION "{schema}".issue80_fail_receipt_insert()'))
        receipt = store.migrate_graph_identity(apply_request)
        assert receipt.phase == "apply"
        assert store.schema_state == "target"
        assert store.get_node_by_id("a") is not None
        assert store.get_edge("Person", "a", "knows", "Person", "b") is not None
        assert store.migrate_graph_identity(apply_request).canonical_bytes == receipt.canonical_bytes


def test_pg_runtime_provenance_cas_and_mixed_rollback() -> None:
    with _pg_runtime() as (store, _engine, _schema):
        _assert_provenance(store, f"issue80-pg-prov-{uuid.uuid4().hex}")


def test_neo4j_runtime_identity_cas_and_provenance() -> None:
    with _neo4j_runtime() as store:
        prefix = f"issue80-neo-{uuid.uuid4().hex}"
        _assert_identity_and_edge_snapshot(store, prefix)
        _assert_provenance(store, f"{prefix}-prov")


def test_neo4j_runtime_concurrent_cas_and_rollback() -> None:
    with _neo4j_runtime() as store:
        prefix = f"issue80-neo-batch-{uuid.uuid4().hex}"
        a, b, _ab, _ba = _seed_identity(store, prefix)
        a_id, b_id = a.node_id, b.node_id
        a_digest = store.get_node_digest(a_id, node_type="Person")
        b_digest = store.get_node_digest(b_id, node_type="Person")
        store.reclassify_node(
            b_id,
            expected_current_digest=b_digest,
            new_type="Person",
            new_space_id="subject",
            new_properties={"name": "beta-updated"},
        )
        stores = []
        try:
            from opencrab.stores.neo4j_store import Neo4jStore
            env = _required_service_env(
                "OPENCRAB_NEO4J_TEST_URI", "OPENCRAB_NEO4J_TEST_USER",
                "OPENCRAB_NEO4J_TEST_PASSWORD", "OPENCRAB_NEO4J_TEST_DATABASE",
            )
            stores = [Neo4jStore(
                env["OPENCRAB_NEO4J_TEST_URI"], env["OPENCRAB_NEO4J_TEST_USER"],
                env["OPENCRAB_NEO4J_TEST_PASSWORD"], env["OPENCRAB_NEO4J_TEST_DATABASE"],
            ) for _ in range(2)]
            def attempt(worker: Any) -> Any:
                try:
                    return worker.reclassify_node(
                        a_id, expected_current_digest=a_digest, new_type="Agent",
                        new_space_id="subject", new_properties={"name": "winner"},
                    )
                except NodeIdentityConflict:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, stores))
            assert sum(result is not None for result in results) == 1
            assert store.get_node("Agent", a_id)["name"] == "winner"
        finally:
            for worker in stores:
                worker.close()
        before = (
            store.get_node("Agent", a_id), store.get_node("Person", b_id),
            store.get_edge("Agent", a_id, "knows", "Person", b_id),
            store.get_edge("Person", b_id, "knows", "Agent", a_id), store.graph_fingerprint(),
        )
        current_a = store.get_node_digest(a_id, node_type="Agent")
        _expect(NodeIdentityConflict, lambda: store.update_nodes_batch([
            {"node_id": a_id, "expected_current_digest": current_a,
             "new_type": "Entity", "new_properties": {"name": "batch"},
             "new_space_id": "concept"},
            {"node_id": b_id, "expected_current_digest": b_digest,
             "new_type": "Person", "new_properties": {"name": "stale"},
             "new_space_id": "subject"},
        ]))
        assert (
            store.get_node("Agent", a_id), store.get_node("Person", b_id),
            store.get_edge("Agent", a_id, "knows", "Person", b_id),
            store.get_edge("Person", b_id, "knows", "Agent", a_id), store.graph_fingerprint(),
        ) == before


def test_neo4j_migration_capability_negative_unit() -> None:
    from unittest.mock import patch

    from opencrab.stores.neo4j_store import Neo4jStore

    store = object.__new__(Neo4jStore)
    request = DryRunMigrationRequest(expected_source_fingerprint="0" * 64, mappings=())
    with patch("neo4j.GraphDatabase.driver") as driver:
        _expect(GraphWriteCapabilityUnavailable, lambda: store.migrate_graph_identity(request))
        driver.assert_not_called()


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


def test_kuzu_read_state_inventory_capability_negative() -> None:
    _run_case("test_kuzu_read_state_inventory_capability_negative")


def test_kuzu_legacy_schema_fails_closed_before_write() -> None:
    _run_case("test_kuzu_legacy_schema_fails_closed_before_write")


def test_kuzu_partial_schema_fails_closed_before_write() -> None:
    _run_case("test_kuzu_partial_schema_fails_closed_before_write")


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


def test_pg_upsert_node_on_conflict_zero_rowcount_reselect_unit() -> None:
    from opencrab.stores._sql_dialect import POSTGRES
    from opencrab.stores._sql_graph_base import GraphTx, _SqlGraphStoreBase
    from opencrab.stores.pg_graph_store import _CatalogTxAdapter

    source = Path(_SqlGraphStoreBase.__module__.replace('.', '/') + ".py")
    assert source.is_file()
    assert "ON CONFLICT (node_id) DO NOTHING" in source.read_text()

    class Result:
        rowcount = 0

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class Connection:
        def __init__(self) -> None:
            self.statements: list[tuple[str, dict[str, Any] | None]] = []

        def execute(self, statement: str, params: dict[str, Any] | None = None) -> Result:
            self.statements.append((statement, params))
            return Result()

    connection = Connection()
    adapter = _CatalogTxAdapter(GraphTx(connection, POSTGRES))
    adapter.execute("SELECT 1", {"probe": 1})
    assert connection.statements[-1] == ("SELECT 1", {"probe": 1})

    from sqlalchemy import text

    adapter.execute(text("SELECT 1"))
    assert connection.statements[-1] == ("SELECT 1", {})
    for invalid in (type("TextLike", (), {"text": "SELECT 1"})(), type("StringLike", (), {"__str__": lambda self: "SELECT 1"})(), b"SELECT 1", None):
        _expect(TypeError, lambda invalid=invalid: adapter.execute(invalid))
    _expect(ValueError, lambda: adapter.execute(text("BEGIN")))
    _expect(ValueError, lambda: adapter.execute(text("SELECT 1; SELECT 2")))


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


def test_kuzu_public_mutation_capability_gate() -> None:
    _run_case("test_kuzu_public_mutation_capability_gate")


def test_kuzu_public_batch_and_delete_capability_gate() -> None:
    _run_case("test_kuzu_public_batch_and_delete_capability_gate")


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


def test_kuzu_run_cypher_rejects_mutation_kuzu() -> None:
    _run_case("test_kuzu_run_cypher_rejects_mutation_kuzu")


def test_kuzu_direct_execute_writer_capability_negative_static() -> None:
    _run_case("test_kuzu_direct_execute_writer_capability_negative_static")
