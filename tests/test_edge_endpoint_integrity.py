"""Edge endpoint referential integrity at the SQL graph-store layer (issue #84).

WHY THIS FILE EXISTS
--------------------
Issue #84 reported that ``upsert_edge``/``upsert_edges_batch`` wrote edges
without checking that the endpoints exist, and that ``GRAPH_STORE_SCHEMA``
declares no FK -- so a dangling edge was accepted at the store level.

The *enforcement* half of that report was fixed meanwhile (PR #241 added an
in-transaction endpoint lookup to every SQL edge writer). What was NEVER
added is a test that holds it there: the only endpoint-guard regression file
is ``test_add_edge_endpoint_guard.py``, which exercises
``OntologyBuilder.add_edge`` -- the builder layer, i.e. exactly the layer
issue #84 says is insufficient because callers can bypass it. Deleting the
store-level check left the whole suite green. This file closes that hole.

It also pins the diagnostic added for issue #84's third remedy,
``count_dangling_edges()``. The DB still does not enforce the invariant (no
FK -- see the module note on ``count_dangling_edges``), so a raw SQL path can
still seed a dangling row; the point of the counter is that such a row is at
least observable. Every "raw INSERT" below stands in for that path.

CONTRACT PINNED HERE (deliberately asymmetric, see _graph_protocol.py):
  - single ``upsert_edge``  -> returns False, writes nothing
  - batch  ``upsert_edges_batch`` -> raises ValueError, and a batch mixing
    good rows with one bad row writes NONE of them (all-or-none; whether the
    implementation refuses before the first insert or rolls back after is
    not observable from a test, so it is not claimed here)

DETECTION POWER
---------------
Guard tests here pass on arrival (the guard already ships). Their value is
proven by reverse mutation, not by a red-before-green transition:
  - drop ``upsert_edge``'s endpoint lookup      -> the single-* tests fail
  - drop the batch pre-validation loop          -> the batch-* tests fail
  - flip the counter's OR to AND                -> the one-sided tests fail
  - drop the counter's from_type comparison     -> from_type drift fails
  - drop the counter's to_type comparison       -> to_type drift fails
  - drop the missing-object degradation         -> the dropped-table tests fail
  - widen degradation back to a bare "does not exist" substring
                                                -> the PG malformed test fails
The one-sided cases exist FOR that third mutation: a test that only ever
removes BOTH endpoints still passes with AND in place of OR.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from typing import Any

import pytest

from opencrab.stores._sql_graph_base import _is_missing_object_error

# --------------------------------------------------------------------------
# Backends. Both SQL dialects run the identical contract -- FK/constraint
# behaviour is exactly where dialects drift, so a SQLite-only proof would not
# say anything about PG (mirrors tests/test_graph_protocol_contract.py).
# --------------------------------------------------------------------------


class _LocalBackend:
    """LocalGraphStore + a second, plain sqlite3 handle for raw seeding."""

    name = "local"

    def __init__(self, tmp_path):
        from opencrab.stores.local_graph_store import LocalGraphStore

        self._path = str(tmp_path / "graph.db")
        self.store = LocalGraphStore(self._path)

    def raw(self, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(self._path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def table(self, name: str) -> str:
        return name

    def drop(self, name: str) -> None:
        self.raw(f"DROP TABLE {name}")  # noqa: S608

    def close(self) -> None:
        self.store.close()


class _PgBackend:
    """PGGraphStore in a throwaway schema + a raw SQLAlchemy engine."""

    name = "pg"

    def __init__(self, url: str):
        import sqlalchemy as sa

        from opencrab.stores.pg_graph_store import PGGraphStore

        self._sa = sa
        self._schema = f"o84_{uuid.uuid4().hex[:8]}"
        self._engine = sa.create_engine(url)
        self.store = PGGraphStore(url, schema=self._schema)

    def raw(self, sql: str, params: tuple = ()) -> None:
        # Positional -> named, so one test body serves both dialects.
        for index, _ in enumerate(params):
            sql = sql.replace("?", f":p{index}", 1)
        with self._engine.begin() as conn:
            conn.execute(self._sa.text(sql), {f"p{i}": v for i, v in enumerate(params)})

    def table(self, name: str) -> str:
        return f'"{self._schema}".{name}'

    def drop(self, name: str) -> None:
        self.raw(f"DROP TABLE {self.table(name)}")

    def close(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(self._sa.text(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE'))
        self._engine.dispose()


@pytest.fixture(params=["local", "pg"])
def backend(request, tmp_path):
    if request.param == "local":
        made = _LocalBackend(tmp_path)
    else:
        url = os.environ.get("OPENCRAB_PG_TEST_URL")
        if not url:
            pytest.skip("OPENCRAB_PG_TEST_URL not set — PG dialect leg skipped")
        made = _PgBackend(url)
    assert made.store.available
    try:
        yield made
    finally:
        made.close()


@pytest.fixture
def seeded(backend):
    """Two real nodes. Everything below is relative to these."""
    backend.store.upsert_node("Person", "alice", {"name": "alice"}, "subject")
    backend.store.upsert_node("Person", "bob", {"name": "bob"}, "subject")
    return backend


def _edge_count(backend) -> int:
    row = backend.store._fetch_one(f"SELECT COUNT(*) FROM {backend.table('graph_edges')}", {})  # noqa: S608
    return int(row[0])


def _edge(from_id: str, to_id: str, *, from_type: str = "Person", to_type: str = "Person",
          relation: str = "knows") -> dict[str, Any]:
    return {
        "from_type": from_type, "from_id": from_id, "relation": relation,
        "to_type": to_type, "to_id": to_id, "properties": {},
    }


def _seed_raw_edge(backend, from_id: str, to_id: str, *,
                   from_type: str = "Person", to_type: str = "Person",
                   relation: str = "knows") -> None:
    """Write an edge straight to the table, bypassing every store guard.

    This is the F-8-style "raw sqlite script" issue #84 names as the way the
    invariant can still be broken while the schema carries no FK.
    """
    backend.raw(
        f"INSERT INTO {backend.table('graph_edges')} "  # noqa: S608
        "(from_type, from_id, relation, to_type, to_id, properties) VALUES (?,?,?,?,?,?)",
        (from_type, from_id, relation, to_type, to_id, "{}"),
    )


# --------------------------------------------------------------------------
# Normal — a well-formed edge is written, and a clean graph counts zero.
# --------------------------------------------------------------------------


class TestNormal:
    def test_single_edge_between_existing_nodes_is_written(self, seeded):
        assert seeded.store.upsert_edge("Person", "alice", "knows", "Person", "bob") is True
        assert _edge_count(seeded) == 1
        assert seeded.store.get_edge("Person", "alice", "knows", "Person", "bob") is not None

    def test_batch_between_existing_nodes_is_written(self, seeded):
        seeded.store.upsert_node("Person", "carol", {"name": "carol"}, "subject")
        written = seeded.store.upsert_edges_batch([
            _edge("alice", "bob"), _edge("alice", "carol"),
        ])
        assert written == 2
        assert _edge_count(seeded) == 2

    def test_self_referential_edge_is_written(self, seeded):
        """from_id == to_id binds the same id to both endpoint predicates."""
        assert seeded.store.upsert_edge("Person", "alice", "knows", "Person", "alice") is True
        assert _edge_count(seeded) == 1

    def test_self_referential_edge_is_written_by_the_batch_path(self, seeded):
        """The batch is a SEPARATE implementation from the single writer."""
        assert seeded.store.upsert_edges_batch([_edge("alice", "alice")]) == 1
        assert _edge_count(seeded) == 1

    def test_clean_graph_counts_zero_dangling(self, seeded):
        seeded.store.upsert_edge("Person", "alice", "knows", "Person", "bob")
        assert seeded.store.count_dangling_edges() == 0


# --------------------------------------------------------------------------
# Error — the store refuses an edge whose endpoint snapshot does not resolve.
# --------------------------------------------------------------------------


class TestSingleWriterRefuses:
    @pytest.mark.parametrize(
        ("from_id", "to_id"),
        [("alice", "ghost"), ("ghost", "bob"), ("ghost-a", "ghost-b")],
        ids=["to-missing", "from-missing", "both-missing"],
    )
    def test_missing_endpoint_is_refused_and_writes_nothing(self, seeded, from_id, to_id):
        assert seeded.store.upsert_edge("Person", from_id, "knows", "Person", to_id) is False
        assert _edge_count(seeded) == 0

    @pytest.mark.parametrize(
        ("from_type", "to_type"),
        [("Robot", "Person"), ("Person", "Robot")],
        ids=["from-type-drift", "to-type-drift"],
    )
    def test_endpoint_type_mismatch_is_refused(self, seeded, from_type, to_type):
        """The node exists but under a different type: the edge's endpoint
        snapshot still does not resolve, so it must not be written."""
        assert seeded.store.upsert_edge(from_type, "alice", "knows", to_type, "bob") is False
        assert _edge_count(seeded) == 0


class TestBatchWriterRefuses:
    @pytest.mark.parametrize(
        ("from_id", "to_id"),
        [("alice", "ghost"), ("ghost", "bob")],
        ids=["to-missing", "from-missing"],
    )
    def test_missing_endpoint_raises(self, seeded, from_id, to_id):
        with pytest.raises(ValueError, match="edge endpoint does not exist"):
            seeded.store.upsert_edges_batch([_edge(from_id, to_id)])
        assert _edge_count(seeded) == 0

    @pytest.mark.parametrize(
        ("from_type", "to_type"),
        [("Robot", "Person"), ("Person", "Robot")],
        ids=["from-type-drift", "to-type-drift"],
    )
    def test_endpoint_type_mismatch_raises(self, seeded, from_type, to_type):
        with pytest.raises(ValueError, match="edge endpoint type mismatch"):
            seeded.store.upsert_edges_batch([_edge("alice", "bob", from_type=from_type, to_type=to_type)])
        assert _edge_count(seeded) == 0

    def test_one_bad_row_rejects_the_whole_batch(self, seeded):
        """All-or-none: one bad row and the good rows are not written either.

        That is the property a caller can rely on, and the one this assertion
        actually distinguishes. WHETHER the implementation refuses before the
        first insert or rolls back after is not observable from here (both
        leave zero rows), so it is not claimed as contract."""
        seeded.store.upsert_node("Person", "carol", {"name": "carol"}, "subject")
        before = _edge_count(seeded)
        with pytest.raises(ValueError, match="edge endpoint does not exist"):
            seeded.store.upsert_edges_batch([
                _edge("alice", "bob"), _edge("alice", "carol"), _edge("alice", "ghost"),
            ])
        assert _edge_count(seeded) == before == 0


# --------------------------------------------------------------------------
# Edge cases — ordering, empty input, and the observability the DB lacks.
# --------------------------------------------------------------------------


class TestOrderingAndEmpty:
    def test_an_edge_written_before_its_nodes_is_refused(self, backend):
        """Every production loader writes nodes first (pack load runs
        load_nodes before load_edges; both direct batch callers write the
        node batch first). This pins that ordering as a contract rather than
        a coincidence."""
        assert backend.store.upsert_edge("Person", "alice", "knows", "Person", "bob") is False
        with pytest.raises(ValueError, match="edge endpoint does not exist"):
            backend.store.upsert_edges_batch([_edge("alice", "bob")])
        assert _edge_count(backend) == 0

        backend.store.upsert_node("Person", "alice", {}, "subject")
        backend.store.upsert_node("Person", "bob", {}, "subject")
        assert backend.store.upsert_edge("Person", "alice", "knows", "Person", "bob") is True

    def test_empty_batch_is_a_no_op(self, seeded):
        assert seeded.store.upsert_edges_batch([]) == 0
        assert _edge_count(seeded) == 0
        assert seeded.store.count_dangling_edges() == 0


class TestDanglingIsObservable:
    """The schema carries no FK, so these rows CAN exist. Counting them is
    the whole point: before this, nothing in the store could report them."""

    def test_a_one_sided_dangling_edge_is_counted(self, seeded):
        """One endpoint real, one missing. This case is what separates the
        counter's OR from an AND -- with AND it would report zero."""
        _seed_raw_edge(seeded, "alice", "ghost")
        assert seeded.store.count_dangling_edges() == 1

    def test_a_dangling_edge_on_the_other_side_is_counted(self, seeded):
        _seed_raw_edge(seeded, "ghost", "bob")
        assert seeded.store.count_dangling_edges() == 1

    def test_from_type_drift_is_counted(self, seeded):
        """Both node rows exist; the edge records the wrong type for `from`."""
        _seed_raw_edge(seeded, "alice", "bob", from_type="Robot")
        assert seeded.store.count_dangling_edges() == 1

    def test_to_type_drift_is_counted(self, seeded):
        """Same on the other endpoint -- the counter must compare BOTH."""
        _seed_raw_edge(seeded, "alice", "bob", to_type="Robot")
        assert seeded.store.count_dangling_edges() == 1

    def test_self_referential_dangling_is_counted(self, seeded):
        _seed_raw_edge(seeded, "ghost", "ghost")
        assert seeded.store.count_dangling_edges() == 1

    def test_self_referential_type_drift_is_counted(self, seeded):
        _seed_raw_edge(seeded, "alice", "alice", from_type="Robot", to_type="Robot")
        assert seeded.store.count_dangling_edges() == 1

    def test_only_the_dangling_rows_are_counted(self, seeded):
        seeded.store.upsert_edge("Person", "alice", "knows", "Person", "bob")
        _seed_raw_edge(seeded, "alice", "ghost", relation="saw")
        _seed_raw_edge(seeded, "ghost2", "bob", relation="saw")
        assert _edge_count(seeded) == 3
        assert seeded.store.count_dangling_edges() == 2


class TestPartialSchema:
    """A partial schema stays inspectable -- the same stance
    ``inspect_graph_identity`` already takes ("expose the rows that still
    exist so the operator can see recovery residue"). A diagnostic is most
    needed exactly when the schema is damaged, so it must answer rather
    than surface a raw driver error."""

    def test_missing_edge_table_counts_zero(self, seeded):
        seeded.drop("graph_edges")
        assert seeded.store.count_dangling_edges() == 0

    def test_missing_node_table_counts_every_edge(self, seeded):
        """With no node table, no endpoint resolves -- every edge dangles."""
        _seed_raw_edge(seeded, "alice", "bob")
        _seed_raw_edge(seeded, "alice", "bob", relation="saw")
        seeded.drop("graph_nodes")
        assert seeded.store.count_dangling_edges() == 2

    def test_both_tables_missing_counts_zero(self, seeded):
        seeded.drop("graph_edges")
        seeded.drop("graph_nodes")
        assert seeded.store.count_dangling_edges() == 0

    def test_missing_endpoint_column_counts_every_edge(self, backend):
        """Column-level damage, not table-level: ``graph_edges`` is there but
        has lost ``to_type``, so no edge's endpoint snapshot can resolve. The
        conservative answer is "all of them", and it must be an answer rather
        than a raw driver error.

        The damaged table is REBUILT rather than altered: ``ALTER TABLE ...
        DROP COLUMN`` needs SQLite 3.35+, and this project supports 3.24+
        (see the runtime note in pyproject.toml). CREATE/INSERT is core
        syntax on every supported version.
        """
        if backend.name != "local":
            pytest.skip("column-level damage is exercised on the SQLite leg; "
                        "the PG 42703 classification is pinned by the unit tests below")
        backend.store.upsert_node("Person", "alice", {}, "subject")
        backend.store.upsert_node("Person", "bob", {}, "subject")
        backend.raw("DROP TABLE graph_edges")
        backend.raw(
            "CREATE TABLE graph_edges ("
            " from_type TEXT NOT NULL, from_id TEXT NOT NULL, relation TEXT NOT NULL,"
            " to_id TEXT NOT NULL, properties TEXT NOT NULL DEFAULT '{}',"
            " PRIMARY KEY (from_id, relation, to_id))"
        )
        for relation in ("knows", "saw"):
            backend.raw(
                "INSERT INTO graph_edges (from_type, from_id, relation, to_id, properties)"
                " VALUES (?,?,?,?,?)",
                ("Person", "alice", relation, "bob", "{}"),
            )
        assert backend.store.count_dangling_edges() == 2


# --------------------------------------------------------------------------
# Error classification. A dropped table must degrade to a count; anything
# else must reach the caller. Getting this wrong turns a real error into a
# confidently wrong number, which is worse than no diagnostic at all.
# --------------------------------------------------------------------------


class _FakePgError(Exception):
    def __init__(self, message: str, pgcode: str | None = None, sqlstate: str | None = None):
        super().__init__(message)
        self.pgcode = pgcode
        self.sqlstate = sqlstate


class _FakeSaError(Exception):
    def __init__(self, message: str, orig: Exception):
        super().__init__(message)
        self.orig = orig


class TestMissingObjectClassification:
    @pytest.mark.parametrize("code", ["42P01", "42703"], ids=["undefined-table", "undefined-column"])
    def test_missing_object_sqlstates_degrade(self, code):
        exc = _FakeSaError("(psycopg2.errors.X) something", _FakePgError("something", pgcode=code))
        assert _is_missing_object_error(exc) is True

    def test_undefined_operator_does_not_degrade(self):
        """PG says "operator does not exist" for a type-mismatched comparison.
        A substring match on "does not exist" swallows it and then reports
        every edge as dangling -- measured, not hypothetical."""
        exc = _FakeSaError(
            '(psycopg2.errors.UndefinedFunction) operator does not exist: integer = text',
            _FakePgError("operator does not exist: integer = text", pgcode="42883"),
        )
        assert _is_missing_object_error(exc) is False

    def test_sqlstate_attribute_is_read_too(self):
        """psycopg2 exposes pgcode; psycopg3 exposes sqlstate."""
        exc = _FakeSaError("boom", _FakePgError("boom", sqlstate="42P01"))
        assert _is_missing_object_error(exc) is True

    @pytest.mark.parametrize(
        "message", ["no such table: graph_edges", "no such column: node_type"],
        ids=["table", "column"],
    )
    def test_sqlite_missing_object_messages_degrade(self, message):
        assert _is_missing_object_error(sqlite3.OperationalError(message)) is True

    def test_other_sqlite_operational_errors_do_not_degrade(self):
        assert _is_missing_object_error(sqlite3.OperationalError("database is locked")) is False


@pytest.mark.skipif(
    not os.environ.get("OPENCRAB_PG_TEST_URL"),
    reason="OPENCRAB_PG_TEST_URL not set — PG malformed-schema case skipped",
)
def test_pg_type_mismatched_columns_raise_rather_than_counting_everything():
    """Both tables present, but ``graph_nodes.node_type`` is an integer.

    PGGraphStore opens this schema with ``available=True`` (it classifies as
    partial, which gates writes, not reads), so the counter is reachable. The
    comparison then fails with 42883. Degrading that to "table missing" would
    fall through to the bare edge count and report every edge as dangling
    while the node table sits there intact.
    """
    import sqlalchemy as sa

    from opencrab.stores.pg_graph_store import PGGraphStore

    url = os.environ["OPENCRAB_PG_TEST_URL"]
    schema = f"o84m_{uuid.uuid4().hex[:8]}"
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(sa.text(
                f'CREATE TABLE "{schema}".graph_nodes (node_type integer NOT NULL, node_id text NOT NULL,'
                " space_id text, properties jsonb NOT NULL DEFAULT '{}', PRIMARY KEY (node_id))"
            ))
            conn.execute(sa.text(
                f'CREATE TABLE "{schema}".graph_edges (from_type text NOT NULL, from_id text NOT NULL,'
                " relation text NOT NULL, to_type text NOT NULL, to_id text NOT NULL,"
                " properties jsonb NOT NULL DEFAULT '{}', PRIMARY KEY (from_id, relation, to_id))"
            ))
            conn.execute(sa.text(
                f'INSERT INTO "{schema}".graph_edges VALUES (\'Person\',\'a\',\'knows\',\'Person\',\'b\',\'{{}}\')'
            ))
        store = PGGraphStore(url, schema=schema)
        assert store.available
        with pytest.raises(Exception) as caught:
            store.count_dangling_edges()
        assert getattr(getattr(caught.value, "orig", None), "pgcode", None) == "42883"
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


# --------------------------------------------------------------------------
# The migration cutover is the fourth edge writer, and the only one that
# inserts endpoint type snapshots WITHOUT consulting graph_nodes -- it trusts
# the validated plan instead. So the plan validation has to do the checking,
# and it has to check the edge's recorded endpoint types against the TARGET
# NODE's own type, not against another field of the same edge spec.
# --------------------------------------------------------------------------


def _tampered_plan(plan_bytes: bytes, *, bogus_type: str = "GhostType") -> bytes:
    """Rewrite one retained edge's ``from_type`` and re-seal the plan.

    Every integrity field a tamperer can recompute IS recomputed here, because
    every one of them is derived from the plan's own bytes rather than from a
    secret: the edge's digest (covers from_type), the mapping fingerprint
    (covers all canonical_mappings, and the validator recomputes it from those
    same mappings), and the planned-edge fingerprint (covers the digests). The
    canonical_mappings list is re-sorted the way the planner sorts it, so the
    result is byte-shaped exactly like a genuine plan.

    What remains wrong is only the thing this test is about: the edge now
    claims an endpoint type that no node in the plan actually has. Nothing
    inside the plan is inconsistent with anything else inside the plan --
    which is precisely why the check has to reach the target NODE's type.
    """
    import hashlib
    import json

    from opencrab.common.graph_identity import canonical_edge_digest, canonical_json_bytes

    payload = json.loads(plan_bytes)
    mappings = payload["canonical_mappings"]
    edge_index = next(
        index for index, item in enumerate(mappings)
        if item.get("kind") == "edge" and item.get("result", "retained") == "retained"
    )
    edge = mappings[edge_index]
    target = dict(edge["target"])
    target["from_type"] = bogus_type
    digest = canonical_edge_digest(
        target["from_id"], target["relation"], target["to_id"],
        target["from_type"], target["to_type"], edge["properties"],
    )
    mappings[edge_index] = {**edge, "target": target, "digest": digest}
    mappings.sort(key=canonical_json_bytes)

    payload["mapping_fingerprint"] = hashlib.sha256(
        b"opencrab.issue80.mapping.v1\0" + canonical_json_bytes(mappings)
    ).hexdigest()
    retained = sorted(
        [item["target"]["from_id"], item["target"]["relation"], item["target"]["to_id"], item["digest"]]
        for item in mappings
        if item.get("kind") == "edge" and item.get("result", "retained") == "retained"
    )
    payload["planned_target_edge_fingerprint"] = hashlib.sha256(
        b"opencrab.issue80.planned-edges.v1\0" + canonical_json_bytes(retained)
    ).hexdigest()
    return canonical_json_bytes(payload)


def _legacy_two_node_fixture():
    """One edge between two distinctly-named nodes.

    Deliberately NOT the duplicate-id merge fixture: there, the two source
    edges collapse onto one target key, so changing one spec's digest trips
    the dedup consistency check before anything endpoint-related is reached.
    A plan with a single retained edge isolates the endpoint-type question.
    """
    from tests.issue80_migration import FixtureHandle

    fixture = FixtureHandle.create()
    fixture.create_legacy()
    fixture.seed(
        nodes=(("Person", "alice", None, {"name": "a"}), ("Person", "bob", None, {"name": "b"})),
        edges=(("Person", "alice", "knows", "Person", "bob", {"weight": 1}),),
    )
    return fixture


class TestMigrationPlanEndpointTypes:
    def test_a_valid_plan_still_applies(self, tmp_path):
        """Guard against the check rejecting legitimate plans: the planner
        derives every edge's target types from the mapped node's own type
        (``source_to_target[...]["node_type"]``), so a plan it produced must
        pass unchanged and rebuild a graph with zero dangling edges."""
        from opencrab.common.graph_identity import (
            ApplyMigrationRequest,
            DryRunMigrationRequest,
            plan_sha256,
        )
        from opencrab.stores.local_graph_store import LocalGraphStore

        fixture = _legacy_two_node_fixture()
        store = LocalGraphStore(str(fixture.db_path))
        try:
            inventory = store.inspect_graph_identity()
            dry = store.migrate_graph_identity(DryRunMigrationRequest(inventory.source_fingerprint, mappings=()))
            artifact = tmp_path / "backup.db"
            artifact.write_bytes(fixture.db_path.read_bytes())
            receipt = store.migrate_graph_identity(ApplyMigrationRequest(
                request_id="issue84-valid",
                expected_source_fingerprint=dry.source_fingerprint,
                plan_bytes=dry.plan_bytes,
                plan_sha256=plan_sha256(dry.plan_bytes),
                backup_path=artifact,
                backup_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            ))
            assert receipt.phase == "apply"
            assert store.count_dangling_edges() == 0
        finally:
            store.close()
            fixture.close()

    def test_a_plan_claiming_the_wrong_endpoint_type_is_rejected(self, tmp_path):
        """The migration cutover is the one edge writer that does NOT consult
        ``graph_nodes`` -- it trusts the validated plan and inserts the type
        snapshots verbatim. So the plan validation has to compare each edge's
        recorded endpoint types against the TARGET NODE's own type.

        Measured before the check existed: this tampered plan was accepted and
        cutover wrote ``('GhostType', 'alice', 'knows', 'Person', 'bob')``,
        after which ``count_dangling_edges()`` reported 1 -- a type-drifted
        endpoint in a freshly rebuilt graph."""
        from opencrab.common.graph_identity import (
            ApplyMigrationRequest,
            DryRunMigrationRequest,
            GraphMigrationConflict,
            plan_sha256,
        )
        from opencrab.stores.local_graph_store import LocalGraphStore
        from tests.issue80_migration import graph_snapshot

        fixture = _legacy_two_node_fixture()
        store = LocalGraphStore(str(fixture.db_path))
        try:
            inventory = store.inspect_graph_identity()
            dry = store.migrate_graph_identity(DryRunMigrationRequest(inventory.source_fingerprint, mappings=()))
            artifact = tmp_path / "backup.db"
            artifact.write_bytes(fixture.db_path.read_bytes())
            tampered = _tampered_plan(dry.plan_bytes)
            assert tampered != dry.plan_bytes, "tamper helper did not change the plan"
            before = graph_snapshot(fixture.db_path)
            with pytest.raises(GraphMigrationConflict, match="endpoint type"):
                store.migrate_graph_identity(ApplyMigrationRequest(
                    request_id="issue84-tampered",
                    expected_source_fingerprint=dry.source_fingerprint,
                    plan_bytes=tampered,
                    plan_sha256=plan_sha256(tampered),
                    backup_path=artifact,
                    backup_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                ))
            assert graph_snapshot(fixture.db_path) == before, "cutover ran despite the conflict"
        finally:
            store.close()
            fixture.close()
