"""#495: a failed graph write must not fan out to the other stores.

``OntologyBuilder.add_node`` used to log a swallowed graph exception and then
write the doc, audit, registry and vector legs anyway. On the create path that
left a doc/vector row with no graph node. On the reclassify path it left the
doc row on the new type while the graph kept the old one. ``add_edge`` wrote
the SQL edge registry after a graph error or a graph "no match".

After a non-ok graph status the builder asks the graph whether it holds this
exact write. Held (ambiguous commit): the legs run as before. Not held: the
legs are skipped. Unknown: nothing is written and the legs carry an "error:"
status. The add_node docstring is the canonical statement of this contract.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from opencrab.auth import Principal, principal_scope
from opencrab.common.graph_identity import (
    GraphPropertyCorruptionError,
    NodeIdentityConflict,
    prepare_node,
)
from opencrab.ontology.builder import OntologyBuilder, store_write_failures
from opencrab.pack.ownership import PACK_STATUS_READY, create_pack, get_pack
from opencrab.stores.sql_store import SQLStore
from tests._pack_fixtures import ensure_test_user

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reconcile_doc_graph_nodes as reconcile  # noqa: E402

LEGS = ("docs", "audit", "sql", "vector")
# Receipt status contract (literal on purpose: consumers match these strings).
SKIPPED_GRAPH_WRITE_FAILED = "skipped (graph write failed)"
GRAPH_STATE_UNKNOWN = "error: graph state unknown"
NODE_PROPS = {"name": "Issue495 User", "email": "u495@example.com", "role": "admin"}


class _Docs:
    """Doc store double that records writes. ``fail_audit`` makes log_event raise."""

    available = True

    def __init__(self, fail_audit: bool = False):
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.events: list[tuple] = []
        self.fail_audit = fail_audit

    def get_node_doc(self, space, node_id):
        return self.rows.get((space, node_id))

    def get_source(self, source_id):  # noqa: ARG002
        return None

    def upsert_node_doc(self, space, node_type, node_id, props):
        self.rows[(space, node_id)] = {"node_type": node_type, "properties": dict(props)}
        return f"{space}::{node_id}"

    def log_event(self, *args, **kwargs):
        if self.fail_audit:
            raise RuntimeError("audit down")
        self.events.append((args, kwargs))


class _Vec:
    available = True

    def __init__(self):
        self.writes: list[list[str]] = []

    def get_by_id(self, node_id):  # noqa: ARG002
        return None

    def upsert_texts(self, texts, ids, metadatas):  # noqa: ARG002
        self.writes.append(list(ids))


class _Graph:
    """Graph double. ``digest`` is what get_node_digest returns, or an
    exception instance to raise. ``commit`` records the write before raising."""

    available = True

    def __init__(self, *, raise_exc=None, digest=None, commit=False, existing=None,
                 edge_row=None, edge_result=True):
        self.raise_exc = raise_exc
        self.digest = digest
        self.commit = commit
        self.nodes: dict[str, dict[str, Any]] = dict(existing or {})
        self.edge_row = edge_row
        self.edge_result = edge_result
        self.digest_calls = 0

    def get_node(self, node_type, node_id):
        row = self.nodes.get(node_id)
        if row is None or row["node_type"] != node_type:
            return None
        return dict(row["props"])

    def get_nodes_by_id(self, node_id):
        row = self.nodes.get(node_id)
        return [] if row is None else [{**row["props"], "node_type": row["node_type"]}]

    def lookup_node_type(self, node_id):
        row = self.nodes.get(node_id)
        return None if row is None else row["node_type"]

    def get_edge(self, *args):  # noqa: ARG002
        if isinstance(self.edge_row, Exception):
            raise self.edge_row
        return self.edge_row

    def get_node_digest(self, node_id, *, node_type=None):  # noqa: ARG002
        self.digest_calls += 1
        if isinstance(self.digest, Exception):
            raise self.digest
        return self.digest

    def _write(self, node_type, node_id, props, space):
        if self.commit:
            self.nodes[node_id] = {"node_type": node_type, "props": {**props, "space": space}}
        if self.raise_exc is not None:
            raise self.raise_exc
        self.nodes[node_id] = {"node_type": node_type, "props": {**props, "space": space}}
        return dict(props)

    def upsert_node(self, node_type, node_id, properties, space_id):
        return self._write(node_type, node_id, properties, space_id)

    def reclassify_node(self, node_id, *, expected_current_digest, new_type,  # noqa: ARG002
                        new_properties, new_space_id):
        return self._write(new_type, node_id, new_properties, new_space_id)

    def upsert_edge(self, *args):  # noqa: ARG002
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.edge_result


def _sql_and_pack(name: str = "issue495-pack") -> tuple[SQLStore, str]:
    sql = SQLStore("sqlite:///:memory:")
    ensure_test_user(sql, "test-user")
    return sql, create_pack(sql, "test-user", name)


def _registry_rows(sql: SQLStore, table: str) -> int:
    with sql._engine.begin() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _expected_digest(pack_id: str) -> str:
    # The builder stamps owner_id and pack_id before the graph write.
    props = {**NODE_PROPS, "owner_id": "test-user", "pack_id": pack_id}
    return prepare_node("User", "u495", props, "subject")[3]


pytestmark = pytest.mark.usefixtures("bind_test_principal")


# --- add_node -------------------------------------------------------------


def _add(builder, pack_id, **kwargs):
    return builder.add_node("subject", "User", "u495", properties=dict(NODE_PROPS),
                            pack_id=pack_id, **kwargs)


def test_t1_create_failure_not_held_writes_nothing_else():
    sql, pack_id = _sql_and_pack()
    graph, docs, vec = _Graph(raise_exc=sqlite3.OperationalError("database is locked")), _Docs(), _Vec()
    result = _add(OntologyBuilder(graph, docs, sql, vec), pack_id)
    assert result["stores"]["graph"] == "error: OperationalError"
    for leg in LEGS:
        assert result["stores"][leg] == SKIPPED_GRAPH_WRITE_FAILED, (leg, result["stores"])
    assert docs.rows == {} and docs.events == [] and vec.writes == []
    assert _registry_rows(sql, "ontology_nodes") == 0
    assert graph.digest_calls == 1


def test_t2_reclassify_failure_not_held_keeps_doc_on_old_type():
    sql, pack_id = _sql_and_pack()
    old = {"node_type": "User", "props": {"pack_id": pack_id, "owner_id": "test-user"}}
    graph = _Graph(raise_exc=GraphPropertyCorruptionError("corrupt"), digest="old-digest",
                   existing={"u495": old})
    docs, vec = _Docs(), _Vec()
    docs.rows[("subject", "u495")] = {"node_type": "User", "properties": {"pack_id": pack_id}}
    result = _add(OntologyBuilder(graph, docs, sql, vec), pack_id,
                  _expected_current_digest="old-digest")
    assert result["stores"]["graph"] == "error: GraphPropertyCorruptionError"
    for leg in LEGS:
        assert result["stores"][leg] == SKIPPED_GRAPH_WRITE_FAILED
    assert docs.rows[("subject", "u495")]["properties"] == {"pack_id": pack_id}
    assert docs.events == [] and vec.writes == []


def test_t3_probe_raises_is_unknown_and_reported_as_failure():
    sql, pack_id = _sql_and_pack()
    graph = _Graph(raise_exc=sqlite3.OperationalError("disk I/O error"),
                   digest=sqlite3.OperationalError("disk I/O error"))
    docs, vec = _Docs(), _Vec()
    result = _add(OntologyBuilder(graph, docs, sql, vec), pack_id)
    for leg in LEGS:
        assert result["stores"][leg] == GRAPH_STATE_UNKNOWN
    assert docs.rows == {} and docs.events == [] and vec.writes == []
    assert _registry_rows(sql, "ontology_nodes") == 0
    failures = store_write_failures(result["stores"])
    assert any(f.startswith("docs:") for f in failures)
    assert any(f.startswith("vector:") for f in failures)


def test_t4_ambiguous_commit_held_writes_every_leg():
    sql, pack_id = _sql_and_pack()
    graph = _Graph(raise_exc=sqlite3.OperationalError("connection dropped"), commit=True,
                   digest=_expected_digest(pack_id))
    docs, vec = _Docs(), _Vec()
    result = _add(OntologyBuilder(graph, docs, sql, vec), pack_id)
    assert result["stores"]["graph"] == "error: OperationalError"
    assert result["stores"]["docs"].startswith("ok (")
    assert result["stores"]["audit"] == "ok"
    assert result["stores"]["sql"] == "ok"
    assert result["stores"]["vector"] == "ok"
    assert ("subject", "u495") in docs.rows and vec.writes == [["u495"]]


def test_t4b_ambiguous_commit_held_with_real_local_graph(tmp_path):
    """The digest the builder computes matches the digest the real SQL graph
    store reports for the row it committed."""
    from opencrab.stores.local_graph_store import LocalGraphStore

    real = LocalGraphStore(db_path=str(tmp_path / "graph.db"))

    class CommitThenRaise:
        def __getattr__(self, name):
            return getattr(real, name)

        def upsert_node(self, *args, **kwargs):
            real.upsert_node(*args, **kwargs)
            raise sqlite3.OperationalError("connection dropped after commit")

    sql, pack_id = _sql_and_pack()
    docs, vec = _Docs(), _Vec()
    result = _add(OntologyBuilder(CommitThenRaise(), docs, sql, vec), pack_id)
    assert result["stores"]["graph"] == "error: OperationalError"
    assert result["stores"]["docs"].startswith("ok (")
    assert result["stores"]["vector"] == "ok"


def test_t5_control_success_writes_every_leg_once():
    sql, pack_id = _sql_and_pack()
    graph, docs, vec = _Graph(), _Docs(), _Vec()
    result = _add(OntologyBuilder(graph, docs, sql, vec), pack_id)
    assert result["stores"]["graph"] == "ok"
    assert result["stores"]["docs"].startswith("ok (")
    assert result["stores"]["sql"] == "ok" and result["stores"]["vector"] == "ok"
    assert len(docs.events) == 1 and vec.writes == [["u495"]]
    assert _registry_rows(sql, "ontology_nodes") == 1
    assert graph.digest_calls == 0


# --- add_edge -------------------------------------------------------------


def _edge_builder(graph):
    sql, pack_id = _sql_and_pack()
    for nid in ("e-from", "e-to"):
        graph.nodes[nid] = {"node_type": "Document", "props": {"pack_id": pack_id}}
    return OntologyBuilder(graph, _Docs(), sql, _Vec()), sql, pack_id


def _edge(builder, pack_id):
    return builder.add_edge("resource", "e-from", "cites", "resource", "e-to", pack_id=pack_id)


def test_t6_edge_error_not_held_skips_registry():
    builder, sql, pack_id = _edge_builder(_Graph(raise_exc=sqlite3.OperationalError("locked")))
    result = _edge(builder, pack_id)
    assert result["stores"]["sql"] == SKIPPED_GRAPH_WRITE_FAILED
    assert _registry_rows(sql, "ontology_edges") == 0


def test_t7_edge_no_match_skips_registry():
    builder, sql, pack_id = _edge_builder(_Graph(edge_result=False))
    result = _edge(builder, pack_id)
    assert result["stores"]["graph"] == "no match"
    assert result["stores"]["sql"] == SKIPPED_GRAPH_WRITE_FAILED
    assert _registry_rows(sql, "ontology_edges") == 0


def test_t7b_edge_probe_raises_is_unknown():
    builder, sql, pack_id = _edge_builder(_Graph(raise_exc=sqlite3.OperationalError("locked")))
    builder._neo4j.edge_row = None
    real_get_edge = builder._neo4j.get_edge
    calls = {"n": 0}

    def get_edge(*args):
        # The identity probe before the write must see "free"; the probe
        # after the failed write raises.
        calls["n"] += 1
        if calls["n"] > 1:
            raise sqlite3.OperationalError("locked")
        return real_get_edge(*args)

    builder._neo4j.get_edge = get_edge
    result = _edge(builder, pack_id)
    assert result["stores"]["sql"] == GRAPH_STATE_UNKNOWN
    assert _registry_rows(sql, "ontology_edges") == 0


def test_t8_edge_error_but_held_writes_registry():
    graph = _Graph(raise_exc=sqlite3.OperationalError("dropped"))
    builder, sql, pack_id = _edge_builder(graph)
    probes = {"n": 0}

    def get_edge(*args):  # noqa: ARG001
        probes["n"] += 1
        return None if probes["n"] == 1 else {"pack_id": pack_id}

    graph.get_edge = get_edge
    result = _edge(builder, pack_id)
    assert result["stores"]["graph"] == "error: OperationalError"
    assert result["stores"]["sql"] == "ok"
    assert _registry_rows(sql, "ontology_edges") == 1


@pytest.mark.parametrize("commit, sql_status, rows", [
    (True, "ok", 1),
    (False, SKIPPED_GRAPH_WRITE_FAILED, 0),
])
def test_t8b_edge_probe_wiring_with_real_local_graph(tmp_path, commit, sql_status, rows):
    """The edge probe passes the resolved node types and the endpoints in
    order. A real store keys get_edge on (from_type, from_id, relation,
    to_type, to_id), so a swapped endpoint or a space in a type slot reads
    "absent" and drops the registry row of a committed edge."""
    from opencrab.stores.local_graph_store import LocalGraphStore

    real = LocalGraphStore(db_path=str(tmp_path / "graph.db"))

    class EdgeFault:
        def __getattr__(self, name):
            return getattr(real, name)

        def upsert_edge(self, *args, **kwargs):
            if commit:
                real.upsert_edge(*args, **kwargs)
            raise sqlite3.OperationalError("dropped")

    sql, pack_id = _sql_and_pack()
    builder = OntologyBuilder(EdgeFault(), _Docs(), sql, None)
    for nid in ("e-from", "e-to"):
        assert builder.add_node("resource", "Document", nid, properties={"title": nid},
                                pack_id=pack_id)["stores"]["graph"] == "ok"
    result = builder.add_edge("resource", "e-from", "cites", "resource", "e-to", pack_id=pack_id)
    assert result["stores"]["graph"] == "error: OperationalError"
    assert result["stores"]["sql"] == sql_status
    assert _registry_rows(sql, "ontology_edges") == rows


def test_t9_edge_control_success_writes_registry():
    builder, sql, pack_id = _edge_builder(_Graph())
    result = _edge(builder, pack_id)
    assert result["stores"]["graph"] == "ok" and result["stores"]["sql"] == "ok"
    assert _registry_rows(sql, "ontology_edges") == 1


# --- pack_create consumer -------------------------------------------------


def _pack_ctx(sql, graph, docs, vec):
    builder = OntologyBuilder(graph, docs, sql, vec)
    return {
        "neo4j": graph, "chroma": vec, "mongo": docs, "sql": sql, "builder": builder,
        "rebac": MagicMock(), "impact": MagicMock(), "hybrid": MagicMock(), "billing": MagicMock(),
    }


def _pack_create(ctx, slug):
    from opencrab.mcp.tools import pack_create

    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
            return pack_create(title="P", pack_id=slug)


def _anchor_digest(graph_holder: dict) -> Any:
    """get_node_digest stand-in that reports the digest of the row the graph
    double committed, exactly like a real store would."""
    def digest(node_id, *, node_type=None):
        row = graph_holder["graph"].nodes.get(node_id)
        if row is None or row["node_type"] != node_type:
            return None
        props = {k: v for k, v in row["props"].items() if k != "space"}
        return prepare_node(row["node_type"], node_id, props, row["props"].get("space"))[3]
    return digest


@pytest.fixture
def alice_sql():
    sql = SQLStore("sqlite:///:memory:")
    ensure_test_user(sql, "alice")
    return sql


def test_t10_pack_create_held_commit_with_audit_failure_stays_partial(alice_sql):
    graph = _Graph(raise_exc=sqlite3.OperationalError("dropped"), commit=True)
    graph.get_node_digest = _anchor_digest({"graph": graph})
    docs, vec = _Docs(fail_audit=True), _Vec()
    result = _pack_create(_pack_ctx(alice_sql, graph, docs, vec), "p495-held")
    assert result.get("status") == "partial", result
    assert any(e.startswith("audit:") for e in result["anchor_errors"])
    assert get_pack(alice_sql, "p495-held")["status"] == PACK_STATUS_READY


def test_t11_pack_create_commit_then_unknown_reports_missing_legs(alice_sql):
    graph = _Graph(raise_exc=sqlite3.OperationalError("dropped"), commit=True,
                   digest=sqlite3.OperationalError("dropped"))
    docs, vec = _Docs(fail_audit=True), _Vec()
    result = _pack_create(_pack_ctx(alice_sql, graph, docs, vec), "p495-unknown")
    assert result.get("status") == "partial", result
    errs = result["anchor_errors"]
    assert any(e.startswith("docs:") for e in errs) and any(e.startswith("vector:") for e in errs)
    assert docs.rows == {} and vec.writes == []


def test_t12_pack_create_existing_anchor_other_digest_is_ok_and_untouched(alice_sql):
    """Intended verdict change (A2b). Base: docs rewritten, audit fails ->
    partial. Now: the graph does not hold this write, nothing else is written,
    the re-probe finds the existing anchor -> ok."""
    from opencrab.pack.ownership import anchor_node_id

    slug = "p495-existing"
    anchor = anchor_node_id(slug)
    existing = {anchor: {"node_type": "Dataset",
                         "props": {"pack_id": slug, "name": "older", "space": "resource"}}}
    graph = _Graph(raise_exc=sqlite3.OperationalError("locked"), existing=existing)
    graph.get_node_digest = _anchor_digest({"graph": graph})
    docs, vec = _Docs(fail_audit=True), _Vec()
    docs.rows[("resource", anchor)] = {"node_type": "Dataset", "properties": {"pack_id": slug, "name": "older"}}
    result = _pack_create(_pack_ctx(alice_sql, graph, docs, vec), slug)
    assert result.get("status") == "ok", result
    assert "anchor_errors" not in result
    assert docs.rows[("resource", anchor)]["properties"]["name"] == "older"
    assert vec.writes == []


def test_t13_pack_create_commit_then_unknown_with_healthy_stores_is_partial(alice_sql):
    """Intended verdict change (A2b). Base: every optional leg written -> ok.
    Now: the graph state is unknown, nothing else is written -> partial."""
    graph = _Graph(raise_exc=sqlite3.OperationalError("dropped"), commit=True,
                   digest=sqlite3.OperationalError("dropped"))
    docs, vec = _Docs(), _Vec()
    result = _pack_create(_pack_ctx(alice_sql, graph, docs, vec), "p495-unknown-ok")
    assert result.get("status") == "partial", result
    assert docs.rows == {} and vec.writes == []


# --- accepted double-fault window -------------------------------------------


def test_t14_double_fault_state_diagnosis_and_recovery(tmp_path):
    """Reclassify commits, then raises, then the digest read raises. The
    split is visible to the reconcile diagnosis and heals by a resend with
    the CURRENT graph digest; a resend with the old digest is refused."""
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    real = LocalGraphStore(db_path=str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "docs.db"))
    sql, pack_id = _sql_and_pack()
    fault = {"on": False}

    class Faulty:
        def __getattr__(self, name):
            return getattr(real, name)

        def reclassify_node(self, *args, **kwargs):
            out = real.reclassify_node(*args, **kwargs)
            if fault["on"]:
                raise sqlite3.OperationalError("dropped after commit")
            return out

        def get_node_digest(self, *args, **kwargs):
            if fault["on"]:
                raise sqlite3.OperationalError("dropped")
            return real.get_node_digest(*args, **kwargs)

    builder = OntologyBuilder(Faulty(), docs, sql, None)
    first = builder.add_node("evidence", "TextUnit", "s495", properties={"text": "a"}, pack_id=pack_id)
    assert first["stores"]["graph"] == "ok"
    old_digest = real.get_node_digest("s495", node_type="TextUnit")

    fault["on"] = True
    split = builder.add_node("evidence", "LogEntry", "s495", properties={"text": "a"},
                             pack_id=pack_id, _expected_current_digest=old_digest)
    fault["on"] = False
    assert split["stores"]["docs"] == GRAPH_STATE_UNKNOWN
    assert real.lookup_node_type("s495") == "LogEntry"
    assert docs.get_node_doc("evidence", "s495")["node_type"] == "TextUnit"

    report = reconcile.diagnose(real, docs)
    assert [(c.node_id, c.reason) for c in report.conflicts] == [("s495", "node_type_mismatch")]

    with pytest.raises(NodeIdentityConflict):
        builder.add_node("evidence", "LogEntry", "s495", properties={"text": "a"},
                         pack_id=pack_id, _expected_current_digest=old_digest)
    assert docs.get_node_doc("evidence", "s495")["node_type"] == "TextUnit"

    current = real.get_node_digest("s495", node_type="LogEntry")
    healed = builder.add_node("evidence", "LogEntry", "s495", properties={"text": "a"},
                              pack_id=pack_id, _expected_current_digest=current)
    assert healed["stores"]["graph"] == "ok"
    assert docs.get_node_doc("evidence", "s495")["node_type"] == "LogEntry"
