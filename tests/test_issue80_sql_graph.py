"""Behavioral issue #80 graph-store qualification tests.

SQLite tests use real temporary databases.  PostgreSQL and Neo4j tests are
enabled only by the disposable service environment prepared by CI.  Kùzu is
tested as a capability-negative facade and never imports its optional driver.
"""

from __future__ import annotations

import ast
import hashlib
import ipaddress
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from opencrab.common.graph_identity import (
    DryRunMigrationRequest,
    EdgeIdentityConflict,
    GraphQueryWriteRejected,
    GraphReadCapabilityUnavailable,
    GraphSchemaMigrationRequired,
    GraphWriteCapabilityUnavailable,
    NodeIdentityConflict,
    canonical_edge_digest,
    canonical_node_digest,
)
from opencrab.stores.kuzu_graph_store import KuzuGraphStore, KuzuUnavailableGraphStore
from opencrab.stores.local_graph_store import LocalGraphStore
from tests.issue80_migration import FixtureHandle


def _expect(exc_type: type[BaseException], callback: Callable[[], Any]) -> None:
    with pytest.raises(exc_type):
        callback()


def test_sqlite_identity_and_edge_cas_actual() -> None:
    with tempfile.TemporaryDirectory(prefix="issue80-sqlite-") as tmp:
        store = LocalGraphStore(str(Path(tmp) / "graph.db"))
        try:
            first = store.upsert_node("Person", "n1", {"name": "one"})
            assert first["id"] == "n1"
            assert store.upsert_node("Person", "n1", {"name": "one"}) == first
            _expect(NodeIdentityConflict, lambda: store.upsert_node("Person", "n1", {"name": "two"}))
            _expect(NodeIdentityConflict, lambda: store.upsert_node("Agent", "n1", {"name": "one"}))
            store.upsert_node("Person", "n2", {"name": "two"})
            assert store.upsert_edge("Person", "n1", "knows", "Person", "n2", {"weight": 1})
            old_digest = store.get_node_digest("n1", node_type="Person")
            assert old_digest
            changed = store.reclassify_node(
                "n1", expected_current_digest=old_digest, new_type="Agent",
                new_space_id=None, new_properties={"name": "changed"},
                return_receipt=True,
            )
            assert changed.node_type == "Agent"
            assert store.get_node("Person", "n1") is None
            assert store.get_node("Agent", "n1")["name"] == "changed"
            assert store.get_edge("Agent", "n1", "knows", "Person", "n2") is not None
            _expect(NodeIdentityConflict, lambda: store.reclassify_node(
                "n1", expected_current_digest=old_digest, new_type="Entity",
                new_space_id=None, new_properties={"name": "stale"},
            ))
        finally:
            store.close()


def test_sqlite_schema_classifier_and_public_gate_actual() -> None:
    with tempfile.TemporaryDirectory(prefix="issue80-schema-") as tmp:
        root = Path(tmp)
        fresh = LocalGraphStore(str(root / "fresh.db"))
        try:
            assert fresh.schema_state == "target"
            assert fresh.graph_fingerprint()
        finally:
            fresh.close()
    with FixtureHandle.create() as fixture:
        fixture.create_legacy()
        fixture.seed(nodes=(("Person", "legacy", None, {"name": "legacy"}),))
        legacy = LocalGraphStore(str(fixture.db_path))
        try:
            assert legacy.schema_state == "legacy_migration_required"
            _expect(GraphSchemaMigrationRequired, lambda: legacy.upsert_node("Person", "blocked", {}))
        finally:
            legacy.close()


def test_sqlite_graph_transaction_boundary_actual() -> None:
    with tempfile.TemporaryDirectory(prefix="issue80-graphtx-") as tmp:
        store = LocalGraphStore(str(Path(tmp) / "graph.db"))
        try:
            observed = store._run_graph_tx(
                lambda tx: (id(store._conn), store._conn.in_transaction, tx.fetchone("SELECT 1")[0]),
                exclusive=True,
            )
            assert observed == (id(store._conn), True, 1)
            with pytest.raises(RuntimeError, match="nested"):
                store._run_graph_tx(lambda _tx: store._run_graph_tx(lambda __tx: None))
            before = store.graph_fingerprint()
            def fail(tx: Any) -> None:
                tx.execute("CREATE TABLE issue80_rollback (value TEXT)")
                raise RuntimeError("injected")
            with pytest.raises(RuntimeError, match="injected"):
                store._run_graph_tx(fail, immediate=True)
            assert store.graph_fingerprint() == before
        finally:
            store.close()


def test_sqlite_writer_and_protocol_behavior_actual() -> None:
    with tempfile.TemporaryDirectory(prefix="issue80-writer-") as tmp:
        store = LocalGraphStore(str(Path(tmp) / "graph.db"))
        try:
            store.upsert_node("Person", "a", {"name": "a"})
            store.upsert_node("Person", "b", {"name": "b"})
            assert store.upsert_edge("Person", "a", "knows", "Person", "b", {"weight": 1})
            assert store.get_edge_digest("a", "knows", "b") == canonical_edge_digest(
                "a", "knows", "b", "Person", "Person",
                {"from_id": "a", "relation": "knows", "to_id": "b", "weight": 1},
            )
            _expect(EdgeIdentityConflict, lambda: store.upsert_edge(
                "Person", "a", "knows", "Person", "b", {"weight": 2}
            ))
            assert store.run_cypher("SELECT 1") == []
        finally:
            store.close()


def test_kuzu_capability_negative_actual() -> None:
    path = "/private/tmp/issue80-kuzu-never-created/graph.kuzu"
    facade = KuzuUnavailableGraphStore(path)
    assert facade.available is False
    assert facade.schema_state == "disabled"
    _expect(GraphWriteCapabilityUnavailable, lambda: facade.upsert_node("Person", "n", {}))
    _expect(GraphWriteCapabilityUnavailable, lambda: facade.upsert_nodes_batch([]))
    _expect(GraphWriteCapabilityUnavailable, lambda: facade.delete_node("Person", "n"))
    _expect(GraphReadCapabilityUnavailable, lambda: facade.inspect_graph_identity())
    _expect(GraphReadCapabilityUnavailable, lambda: facade.run_cypher("MATCH (n) RETURN n"))
    _expect(GraphQueryWriteRejected, lambda: facade.run_cypher("CREATE (n)"))
    _expect(GraphWriteCapabilityUnavailable, lambda: KuzuGraphStore(path))


def test_kuzu_capability_and_writer_ast_inventory_actual() -> None:
    source_path = Path(__file__).resolve().parents[1] / "opencrab/stores/kuzu_graph_store.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert classes == {"KuzuGraphStore", "KuzuUnavailableGraphStore"}
    optional_imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not optional_imports.intersection({"ladybug", "kuzu"})
    sinks = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany", "commit", "rollback"}
    ]
    assert sinks == []
    assert canonical_node_digest("Person", None, {"id": "n"})


def _required_service_env(*names: str) -> dict[str, str]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.fail(f"required issue80 service environment is missing: {', '.join(missing)}")
    return {name: os.environ[name] for name in names}


def _pg_service_guard() -> tuple[Any, str]:
    env = _required_service_env("OPENCRAB_PG_TEST_URL", "OPENCRAB_ISSUE80_SERVICE_APPLY")
    if env["OPENCRAB_ISSUE80_SERVICE_APPLY"] != "1":
        pytest.fail("OPENCRAB_ISSUE80_SERVICE_APPLY=1 is required for issue80 service tests")
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    url = make_url(env["OPENCRAB_PG_TEST_URL"])
    if not url.host or not url.database or not url.database.endswith("_test"):
        pytest.fail("issue80 PG service requires a loopback *_test database")
    try:
        addresses = {ipaddress.ip_address(url.host)}
    except ValueError:
        import socket
        try:
            addresses = {ipaddress.ip_address(socket.gethostbyname(url.host))}
        except (OSError, ValueError):
            pytest.fail("issue80 PG service host is not loopback")
    if not all(address.is_loopback for address in addresses):
        pytest.fail("issue80 PG service host is not loopback")
    engine = create_engine(env["OPENCRAB_PG_TEST_URL"])
    with engine.connect() as conn:
        if conn.execute(text("SELECT current_database()")).scalar_one() != url.database:
            engine.dispose()
            pytest.fail("issue80 PG connection database identity differs from DSN")
    return engine, f"issue80_{uuid.uuid4().hex}"


@contextmanager
def _pg_runtime(*, legacy: bool = False) -> Iterator[Any]:
    engine, schema = _pg_service_guard()
    from sqlalchemy import text
    try:
        if legacy:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                conn.execute(text(f'''CREATE TABLE "{schema}".graph_nodes (
                    node_type TEXT NOT NULL, node_id TEXT NOT NULL, space_id TEXT,
                    properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    PRIMARY KEY (node_type,node_id))'''))
                conn.execute(text(f'''CREATE TABLE "{schema}".graph_edges (
                    from_type TEXT NOT NULL, from_id TEXT NOT NULL, relation TEXT NOT NULL,
                    to_type TEXT NOT NULL, to_id TEXT NOT NULL,
                    properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    PRIMARY KEY (from_type,from_id,relation,to_type,to_id))'''))
                for statement in (
                    f'''CREATE INDEX idx_nodes_pack ON "{schema}".graph_nodes ((properties->>'pack_id'))''',
                    f'''CREATE INDEX idx_nodes_space ON "{schema}".graph_nodes (space_id)''',
                    f'''CREATE INDEX idx_edges_from ON "{schema}".graph_edges (from_id)''',
                    f'''CREATE INDEX idx_edges_to ON "{schema}".graph_edges (to_id)''',
                ):
                    conn.execute(text(statement))
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".graph_nodes
                            (node_type,node_id,space_id,properties)
                            VALUES (:node_type,:node_id,:space_id,
                                    CAST(:properties AS JSONB))'''
                    ),
                    [
                        {"node_type": "Person", "node_id": "pg-a", "space_id": None, "properties": '{"name":"same"}'},
                        {"node_type": "Entity", "node_id": "pg-a", "space_id": None, "properties": '{"name":"same"}'},
                    ],
                )
        from opencrab.stores.pg_graph_store import PGGraphStore
        store = PGGraphStore(engine, schema=schema)
        yield store
    finally:
        if "store" in locals():
            store.close()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_pg_runtime_identity_and_incident_edge_cas() -> None:
    with _pg_runtime() as store:
        store.upsert_node("Person", "pg-a", {"name": "a"})
        store.upsert_node("Person", "pg-b", {"name": "b"})
        assert store.upsert_edge("Person", "pg-a", "knows", "Person", "pg-b", {"weight": 1})
        digest = store.get_node_digest("pg-a", node_type="Person")
        assert digest
        store.reclassify_node(
            "pg-a", expected_current_digest=digest, new_type="Agent",
            new_space_id=None, new_properties={"name": "updated"},
        )
        assert store.get_node("Agent", "pg-a") is not None
        assert store.get_edge("Agent", "pg-a", "knows", "Person", "pg-b") is not None


def test_pg_runtime_migration_and_replay() -> None:
    from opencrab.common.graph_identity import ApplyMigrationRequest, ExplicitMerge
    with _pg_runtime(legacy=True) as store:
        inventory = store.inspect_graph_identity()
        rows = sorted((row for row in inventory.nodes if row.key.node_id == "pg-a"), key=lambda row: row.key.node_type)
        merge = ExplicitMerge(tuple((row.key, row.digest) for row in rows), "pg-a", "Person", None, None)
        dry = store.migrate_graph_identity(DryRunMigrationRequest(inventory.source_fingerprint, (merge,)))
        with tempfile.NamedTemporaryFile(prefix="issue80-pg-backup-", delete=False) as handle:
            artifact = Path(handle.name)
            handle.write(b"backup")
        try:
            request = ApplyMigrationRequest(
                f"issue80-pg-{uuid.uuid4().hex}", inventory.source_fingerprint,
                dry.plan_bytes, dry.plan_sha256, artifact,
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
            )
            receipt = store.migrate_graph_identity(request)
            assert receipt.phase == "apply"
            assert store.schema_state == "target"
            assert store.migrate_graph_identity(request).canonical_bytes == receipt.canonical_bytes
        finally:
            artifact.unlink(missing_ok=True)


def test_pg_runtime_provenance_cas() -> None:
    with _pg_runtime() as store:
        node = store.upsert_node("Entity", "pg-prov", {"name": "node"}, return_receipt=True)
        target = store.graph_fingerprint()
        record = {
            "kind": "node", "target_fingerprint": target,
            "expected_current_digest": node.digest, "proposed_pack_id": "issue80-pack",
            "node_id": "pg-prov", "node_type": "Entity", "reason": "inferred",
            "dry_run_evidence_digest": hashlib.sha256(b"evidence").hexdigest(),
            "allowed_properties_delta": {"set": {"pack_id": "issue80-pack"}, "remove": []},
        }
        receipt = store.backfill_pack_provenance([record])
        assert receipt.target_fingerprint_before == target
        assert store.get_node("Entity", "pg-prov")["pack_id"] == "issue80-pack"


@dataclass(frozen=True)
class _Neo4jEndpointCapability:
    uri: str
    user: str
    password: str
    database: str
    port: int
    run_nonce: str
    container_name: str

    def preflight_database(self, driver_factory: Callable[..., Any] | None = None) -> None:
        if driver_factory is None:
            from neo4j import GraphDatabase
            driver_factory = GraphDatabase.driver
        driver = driver_factory(self.uri, auth=(self.user, self.password))
        try:
            with driver.session(database=self.database) as session:
                record = session.run(
                    "CALL db.info() YIELD name RETURN name AS database"
                ).single()
                if record is None or record["database"] != "neo4j":
                    raise RuntimeError("Neo4j preflight database mismatch")
        finally:
            driver.close()

    def open(
        self,
        store_factory: Callable[..., Any] | None = None,
        driver_factory: Callable[..., Any] | None = None,
    ) -> Any:
        self.preflight_database(driver_factory=driver_factory)
        if store_factory is None:
            from opencrab.stores.neo4j_store import Neo4jStore
            store_factory = Neo4jStore
        return store_factory(self.uri, self.user, self.password, self.database)


def _capability_from_environment() -> _Neo4jEndpointCapability:
    env = _required_service_env(
        "OPENCRAB_ISSUE80_NEO4J_CAPABILITY_FILE", "OPENCRAB_NEO4J_TEST_URI",
        "OPENCRAB_NEO4J_TEST_USER", "OPENCRAB_NEO4J_TEST_PASSWORD",
        "OPENCRAB_NEO4J_TEST_DATABASE", "OPENCRAB_ISSUE80_NEO4J_DISPOSABLE",
        "OPENCRAB_ISSUE80_SERVICE_APPLY",
    )
    if env["OPENCRAB_ISSUE80_NEO4J_DISPOSABLE"] != "1" or env["OPENCRAB_ISSUE80_SERVICE_APPLY"] != "1":
        pytest.fail("issue80 disposable Neo4j capability is required")
    path = Path(env["OPENCRAB_ISSUE80_NEO4J_CAPABILITY_FILE"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        pytest.fail("Neo4j capability must be an absolute regular file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        pytest.fail("Neo4j capability must be mode 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if set(value) != {"schema", "uri", "port", "user", "password", "database", "run_nonce", "container_name"}:
        pytest.fail("Neo4j capability key set is invalid")
    if value["schema"] != "issue80-neo4j-capability-v1":
        pytest.fail("Neo4j capability schema is invalid")
    split = urlsplit(env["OPENCRAB_NEO4J_TEST_URI"])
    if split.scheme not in {"bolt", "neo4j"} or split.hostname != "127.0.0.1" or split.port is None:
        pytest.fail("Neo4j URI must use literal loopback and a port")
    if value["uri"] != env["OPENCRAB_NEO4J_TEST_URI"] or int(value["port"]) != split.port:
        pytest.fail("Neo4j capability endpoint does not match environment")
    if value["user"] != env["OPENCRAB_NEO4J_TEST_USER"] or value["password"] != env["OPENCRAB_NEO4J_TEST_PASSWORD"]:
        pytest.fail("Neo4j capability credentials do not match environment")
    if value["database"] != "neo4j" or env["OPENCRAB_NEO4J_TEST_DATABASE"] != "neo4j":
        pytest.fail("Neo4j capability database is not neo4j")
    return _Neo4jEndpointCapability(
        env["OPENCRAB_NEO4J_TEST_URI"], value["user"], value["password"],
        value["database"], split.port, value["run_nonce"], value["container_name"],
    )


@contextmanager
def _neo4j_runtime() -> Iterator[Any]:
    capability = _capability_from_environment()
    store = capability.open()
    try:
        assert store.available
        assert store.ping()
        yield store
    finally:
        store.close()


def test_neo4j_runtime_identity_and_provenance() -> None:
    with _neo4j_runtime() as store:
        a, b = (
            store.upsert_node("Person", f"issue80-neo-{uuid.uuid4().hex}-a", {"name": "a"}, "subject", return_receipt=True),
            store.upsert_node("Person", f"issue80-neo-{uuid.uuid4().hex}-b", {"name": "b"}, "subject", return_receipt=True),
        )
        store.upsert_edge("Person", a.node_id, "knows", "Person", b.node_id, {"weight": 1})
        digest = store.get_node_digest(a.node_id, node_type="Person")
        assert digest
        changed = store.reclassify_node(
            a.node_id, expected_current_digest=digest, new_type="Agent",
            new_space_id="subject", new_properties={"name": "updated"}, return_receipt=True,
        )
        assert changed.node_type == "Agent"
        assert store.get_node("Agent", a.node_id) is not None


def test_neo4j_runtime_concurrent_cas_uses_capability_factory() -> None:
    from concurrent.futures import ThreadPoolExecutor
    with _neo4j_runtime() as store:
        capability = _capability_from_environment()
        a, _b = (
            store.upsert_node("Person", f"issue80-neo-cas-{uuid.uuid4().hex}-a", {"name": "a"}, "subject", return_receipt=True),
            store.upsert_node("Person", f"issue80-neo-cas-{uuid.uuid4().hex}-b", {"name": "b"}, "subject", return_receipt=True),
        )
        stores = [capability.open() for _ in range(2)]
        try:
            digest = store.get_node_digest(a.node_id, node_type="Person")
            def attempt(worker: Any) -> Any:
                try:
                    return worker.reclassify_node(
                        a.node_id, expected_current_digest=digest, new_type="Agent",
                        new_space_id="subject", new_properties={"name": "winner"},
                    )
                except NodeIdentityConflict:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, stores))
            assert sum(result is not None for result in results) == 1
        finally:
            for worker in stores:
                worker.close()


def test_neo4j_preflight_mismatch_closes_driver_before_constructor() -> None:
    class FakeResult:
        def single(self) -> dict[str, str]:
            return {"database": "wrong"}
    class FakeSession:
        def __init__(self) -> None:
            self.closed = False
        def __enter__(self) -> FakeSession:
            return self
        def __exit__(self, *_args: Any) -> None:
            self.closed = True
        def run(self, statement: str) -> FakeResult:
            assert statement == "CALL db.info() YIELD name RETURN name AS database"
            return FakeResult()
    class FakeDriver:
        def __init__(self) -> None:
            self.session_obj = FakeSession()
            self.closed = False
        def session(self, **_kwargs: Any) -> FakeSession:
            return self.session_obj
        def close(self) -> None:
            self.closed = True
    driver = FakeDriver()
    capability = _Neo4jEndpointCapability(
        "bolt://127.0.0.1:17687", "neo4j", "pw", "neo4j", 17687, "nonce", "container"
    )
    with pytest.raises(RuntimeError, match="database mismatch"):
        capability.open(
            store_factory=lambda *_args: pytest.fail("constructor must not run"),
            driver_factory=lambda *_args, **_kwargs: driver,
        )
    assert driver.closed is True
    assert driver.session_obj.closed is True


def test_neo4j_migration_capability_negative_unit() -> None:
    from unittest.mock import patch

    from opencrab.stores.neo4j_store import Neo4jStore
    store = object.__new__(Neo4jStore)
    request = DryRunMigrationRequest("0" * 64, mappings=())
    with patch("neo4j.GraphDatabase.driver") as driver:
        _expect(GraphWriteCapabilityUnavailable, lambda: store.migrate_graph_identity(request))
        driver.assert_not_called()
