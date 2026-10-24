"""
Contract tests for MongoStore using a fully mocked pymongo client.

No network I/O and no real connection attempts — everything is patched via
``pymongo.MongoClient`` so these tests are instant. This is a deliberate
contrast to tests/test_stores.py::TestMongoStoreUnit, which connects to a
real (invalid) host and burns ~20s total across the suite in
serverSelectionTimeoutMS waits.
"""

from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from opencrab.stores.mongo_store import MongoStore


def _make_connected_store() -> tuple[MongoStore, MagicMock, MagicMock]:
    """Build a MongoStore whose ``_connect`` succeeds against a mocked client."""
    mock_client = MagicMock(name="MongoClient")
    mock_client.admin.command.return_value = {"ok": 1.0}
    collections = {
        "nodes": MagicMock(name="nodes_collection"),
        "sources": MagicMock(name="sources_collection"),
        "audit_log": MagicMock(name="audit_log_collection"),
    }
    mock_db = MagicMock(name="db")
    # __getitem__ must dispatch per collection name (nodes/sources/audit_log)
    # rather than returning one shared MagicMock for every key.
    mock_db.__getitem__.side_effect = collections.__getitem__
    mock_client.__getitem__.return_value = mock_db
    with patch("pymongo.MongoClient", return_value=mock_client):
        store = MongoStore("mongodb://mock:27017", "testdb")
    return store, mock_client, mock_db


def _make_unavailable_store() -> MongoStore:
    """Build a MongoStore whose ``_connect`` fails (pymongo raises)."""
    with patch("pymongo.MongoClient", side_effect=RuntimeError("no route to host")):
        store = MongoStore("mongodb://mock:27017", "testdb")
    return store


# ---------------------------------------------------------------------------
# Normal — available store, CRUD translation to collection calls
# ---------------------------------------------------------------------------


class TestMongoStoreNormal:
    def test_connect_success_sets_available_and_builds_indexes(self):
        store, mock_client, mock_db = _make_connected_store()
        assert store.available is True
        mock_client.admin.command.assert_called_once_with("ping")
        assert mock_db["nodes"].create_index.called
        assert mock_db["sources"].create_index.called
        assert mock_db["audit_log"].create_index.called

    def test_ping_true_when_available(self):
        store, mock_client, _db = _make_connected_store()
        assert store.ping() is True

    def test_upsert_node_doc_inserts_via_update_one_upsert(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].update_one.return_value = MagicMock(upserted_id="abc123")

        result = store.upsert_node_doc("subject", "User", "u1", {"name": "Alice"})

        assert result == "abc123"
        args, kwargs = mock_db["nodes"].update_one.call_args
        filter_arg, update_arg = args[0], args[1]
        assert filter_arg == {"space": "subject", "node_id": "u1"}
        assert update_arg["$set"]["node_type"] == "User"
        assert update_arg["$set"]["properties"] == {"name": "Alice"}
        assert kwargs["upsert"] is True

    def test_upsert_node_doc_falls_back_to_find_one_when_matched_not_inserted(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].update_one.return_value = MagicMock(upserted_id=None)
        mock_db["nodes"].find_one.return_value = {"_id": "existing123"}

        result = store.upsert_node_doc("subject", "User", "u1", {"name": "Alice"})

        assert result == "existing123"
        mock_db["nodes"].find_one.assert_called_once_with(
            {"space": "subject", "node_id": "u1"}, {"_id": 1}
        )

    def test_upsert_node_doc_mirrors_owner_id_to_top_level(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].update_one.return_value = MagicMock(upserted_id="abc")

        store.upsert_node_doc("subject", "User", "u1", {"owner_id": "owner-1"})

        _args, kwargs = mock_db["nodes"].update_one.call_args
        set_doc = mock_db["nodes"].update_one.call_args[0][1]["$set"]
        assert set_doc["owner_id"] == "owner-1"

    def test_get_node_doc_queries_by_space_and_node_id_excluding_mongo_id(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].find_one.return_value = {"space": "s", "node_id": "n1"}

        result = store.get_node_doc("s", "n1")

        assert result == {"space": "s", "node_id": "n1"}
        mock_db["nodes"].find_one.assert_called_once_with(
            {"space": "s", "node_id": "n1"}, {"_id": 0}
        )

    def test_get_node_doc_returns_none_when_missing(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].find_one.return_value = None

        assert store.get_node_doc("s", "missing") is None

    def test_get_node_docs_by_id_queries_by_node_id_only(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].find.return_value = [
            {"space": "s1", "node_id": "shared", "node_type": "T", "properties": {"x": 1}},
            {"space": "s2", "node_id": "shared", "node_type": "T", "properties": {"x": 2}},
        ]

        result = store.get_node_docs_by_id("shared")

        assert len(result) == 2
        assert {row["space"] for row in result} == {"s1", "s2"}
        mock_db["nodes"].find.assert_called_once_with({"node_id": "shared"}, {"_id": 0})

    def test_get_node_docs_by_id_returns_empty_list_when_missing(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].find.return_value = []

        assert store.get_node_docs_by_id("missing") == []

    def test_create_node_doc_if_absent_inserts_and_reports_created(self):
        store, _client, mock_db = _make_connected_store()

        result = store.create_node_doc_if_absent("s", "T", "n1", {"a": 1})

        assert result == "created"
        mock_db["nodes"].insert_one.assert_called_once()
        (doc,), _kwargs = mock_db["nodes"].insert_one.call_args
        assert doc["space"] == "s"
        assert doc["node_id"] == "n1"
        assert doc["node_type"] == "T"
        assert doc["properties"] == {"a": 1}

    def test_create_node_doc_if_absent_mirrors_owner_id_to_top_level(self):
        store, _client, mock_db = _make_connected_store()

        store.create_node_doc_if_absent("s", "User", "u1", {"owner_id": "owner-1"})

        (doc,), _kwargs = mock_db["nodes"].insert_one.call_args
        assert doc["owner_id"] == "owner-1"

    def test_create_node_doc_if_absent_reports_exists_on_duplicate_key(self):
        """issue #317 5-2절: a concurrent writer winning the unique index on
        (space, node_id) must be reported as "exists", not raised, and must
        NOT retry with an overwrite -- that is exactly the race this method
        exists to avoid (unlike upsert_node_doc's unconditional $set)."""
        from pymongo.errors import DuplicateKeyError

        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].insert_one.side_effect = DuplicateKeyError("E11000 duplicate key")

        result = store.create_node_doc_if_absent("s", "T", "n1", {"a": "SHOULD_NOT_MATTER"})

        assert result == "exists"
        mock_db["nodes"].insert_one.assert_called_once()

    def test_iter_node_identities_yields_triples_from_cursor(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["nodes"].find.return_value
        cursor.batch_size.return_value = [
            {"space": "s1", "node_id": "a", "node_type": "T"},
            {"space": "s1", "node_id": "b", "node_type": "T"},
        ]

        result = list(store.iter_node_identities())

        assert result == [("s1", "a", "T"), ("s1", "b", "T")]
        mock_db["nodes"].find.assert_called_once_with(
            {}, {"_id": 0, "space": 1, "node_id": 1, "node_type": 1}
        )

    def test_iter_node_identities_filters_by_space(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["nodes"].find.return_value
        cursor.batch_size.return_value = []

        list(store.iter_node_identities(space="s1"))

        mock_db["nodes"].find.assert_called_once_with(
            {"space": "s1"}, {"_id": 0, "space": 1, "node_id": 1, "node_type": 1}
        )

    def test_iter_node_identities_never_calls_limit(self):
        """Unlike list_nodes, this must stream the full collection -- calling
        .limit() here would silently reintroduce a cap (issue #317 6절)."""
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["nodes"].find.return_value
        cursor.batch_size.return_value = []

        list(store.iter_node_identities())

        cursor.limit.assert_not_called()

    def test_iter_node_identities_missing_node_type_defaults_empty_string(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["nodes"].find.return_value
        cursor.batch_size.return_value = [{"space": "s1", "node_id": "a"}]

        result = list(store.iter_node_identities())

        assert result == [("s1", "a", "")]

    def test_list_nodes_filters_by_space_when_given(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["nodes"].find.return_value
        cursor.limit.return_value = [{"node_id": "a"}, {"node_id": "b"}]

        result = store.list_nodes(space="subject", limit=10)

        mock_db["nodes"].find.assert_called_once_with({"space": "subject"}, {"_id": 0})
        cursor.limit.assert_called_once_with(10)
        assert result == [{"node_id": "a"}, {"node_id": "b"}]

    def test_list_nodes_limit_zero_returns_empty_without_querying(self):
        """issue #120 follow-up: pymongo's own ``Cursor.limit(0)`` means "no
        limit" (the opposite of this contract), so the guard must stop
        before ``find`` is ever called -- proven here the same way as the
        Neo4j export_nodes fix, by asserting the collection method was
        never invoked."""
        store, _client, mock_db = _make_connected_store()

        assert store.list_nodes(limit=0) == []
        mock_db["nodes"].find.assert_not_called()

    def test_list_nodes_negative_limit_returns_empty_without_querying(self):
        store, _client, mock_db = _make_connected_store()

        assert store.list_nodes(limit=-1) == []
        mock_db["nodes"].find.assert_not_called()

    def test_delete_node_doc_true_when_deleted(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].delete_one.return_value = MagicMock(deleted_count=1)

        assert store.delete_node_doc("s", "n1") is True
        mock_db["nodes"].delete_one.assert_called_once_with(
            {"space": "s", "node_id": "n1"}
        )

    def test_delete_node_doc_false_when_nothing_deleted(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].delete_one.return_value = MagicMock(deleted_count=0)

        assert store.delete_node_doc("s", "n1") is False

    def test_upsert_source_mirrors_user_id_to_top_level(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["sources"].update_one.return_value = MagicMock(upserted_id="src1")

        result = store.upsert_source("src-1", "text body", {"user_id": "u1"})

        assert result == "src1"
        set_doc = mock_db["sources"].update_one.call_args[0][1]["$set"]
        assert set_doc["user_id"] == "u1"
        assert set_doc["text"] == "text body"

    def test_list_sources_excludes_id_and_text_projection(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["sources"].find.return_value
        cursor.limit.return_value = [{"source_id": "s1"}]

        store.list_sources(limit=5)

        mock_db["sources"].find.assert_called_once_with({}, {"_id": 0, "text": 0})
        cursor.limit.assert_called_once_with(5)

    def test_list_sources_limit_zero_returns_empty_without_querying(self):
        store, _client, mock_db = _make_connected_store()

        assert store.list_sources(limit=0) == []
        mock_db["sources"].find.assert_not_called()

    def test_list_sources_negative_limit_returns_empty_without_querying(self):
        store, _client, mock_db = _make_connected_store()

        assert store.list_sources(limit=-1) == []
        mock_db["sources"].find.assert_not_called()

    def test_get_audit_log_sorts_desc_and_limits(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["audit_log"].find.return_value
        cursor.sort.return_value = cursor
        cursor.limit.return_value = [{"event_type": "login"}]

        result = store.get_audit_log(limit=20, event_type="login")

        mock_db["audit_log"].find.assert_called_once_with(
            {"event_type": "login"}, {"_id": 0}
        )
        cursor.sort.assert_called_once_with("timestamp", -1)
        cursor.limit.assert_called_once_with(20)
        assert result == [{"event_type": "login"}]

    def test_get_audit_log_limit_zero_returns_empty_without_querying(self):
        store, _client, mock_db = _make_connected_store()

        assert store.get_audit_log(limit=0) == []
        mock_db["audit_log"].find.assert_not_called()

    def test_get_audit_log_negative_limit_returns_empty_without_querying(self):
        store, _client, mock_db = _make_connected_store()

        assert store.get_audit_log(limit=-1) == []
        mock_db["audit_log"].find.assert_not_called()

    def test_collection_stats_counts_all_collections(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].count_documents.return_value = 3
        mock_db["sources"].count_documents.return_value = 2
        mock_db["audit_log"].count_documents.return_value = 7

        stats = store.collection_stats()

        assert stats == {"nodes": 3, "sources": 2, "audit_log": 7}


# ---------------------------------------------------------------------------
# log_event contract — return-id + raise-when-unavailable
# ---------------------------------------------------------------------------
#
# BUG FOUND: log_event previously returned None unconditionally (fire-and-
# forget, silently swallowing both the "unavailable" case and any insert
# exception). Every sibling doc store (LocalSQLDocStore, PgDocStore) raises
# RuntimeError when unavailable and returns the inserted event_id as a str
# so callers can correlate audit entries — LocalSQLDocStore's own docstring
# says it mints a uuid4 "to match MongoStore's ObjectId semantics", i.e. the
# original design intent was for Mongo to hand back an id too. No production
# caller (opencrab/ontology/builder.py) reads the return value of
# log_event, so aligning the contract does not change any observable
# behavior for existing callers in the success path. Fixed in
# opencrab/stores/mongo_store.py: log_event now raises RuntimeError when
# unavailable and returns str(inserted_id) on success.


class TestMongoStoreLogEventContract:
    def test_log_event_inserts_and_returns_inserted_id(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["audit_log"].insert_one.return_value = MagicMock(inserted_id="oid-1")

        result = store.log_event("node_upsert", "u1", {"node_id": "n1"})

        assert result == "oid-1"
        args = mock_db["audit_log"].insert_one.call_args[0][0]
        assert args["event_type"] == "node_upsert"
        assert args["subject_id"] == "u1"
        assert args["details"] == {"node_id": "n1"}

    def test_log_event_raises_when_unavailable(self):
        store = _make_unavailable_store()
        with pytest.raises(RuntimeError, match="not available"):
            store.log_event("test_event", "u1", {"detail": "value"})


# ---------------------------------------------------------------------------
# Error — unavailable store behavior per method (uniform raise contract)
# ---------------------------------------------------------------------------


class TestMongoStoreUnavailableContract:
    def test_connect_failure_leaves_store_unavailable(self):
        store = _make_unavailable_store()
        assert store.available is False

    def test_ping_false_when_unavailable(self):
        # ping() calls self._client.admin.command, but _client is None when
        # _connect failed before assignment.
        store = _make_unavailable_store()
        assert store.ping() is False

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.upsert_node_doc("sp", "T", "n1", {}),
            lambda s: s.get_node_doc("sp", "n1"),
            lambda s: s.create_node_doc_if_absent("sp", "T", "n1", {}),
            lambda s: list(s.iter_node_identities()),
            lambda s: s.list_nodes(),
            lambda s: s.delete_node_doc("sp", "n1"),
            lambda s: s.upsert_source("src1", "text", {}),
            lambda s: s.get_source("src1"),
            lambda s: s.list_sources(),
            lambda s: s.log_event("ev", None, {}),
            lambda s: s.get_audit_log(),
        ],
    )
    def test_raises_runtime_error_when_unavailable(self, call):
        store = _make_unavailable_store()
        with pytest.raises(RuntimeError, match="not available"):
            call(store)

    def test_collection_stats_returns_empty_dict_when_unavailable(self):
        # collection_stats is a best-effort stats read, documented to
        # degrade to {} rather than raise — distinct from the CRUD/audit
        # raise contract above.
        store = _make_unavailable_store()
        assert store.collection_stats() == {}


# ---------------------------------------------------------------------------
# Edge — empty filters, id fallback handling
# ---------------------------------------------------------------------------


class TestMongoStoreEdgeCases:
    def test_list_nodes_empty_query_when_no_space_given(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["nodes"].find.return_value
        cursor.limit.return_value = []

        store.list_nodes()

        mock_db["nodes"].find.assert_called_once_with({}, {"_id": 0})

    def test_get_audit_log_empty_query_when_no_event_type_given(self):
        store, _client, mock_db = _make_connected_store()
        cursor = mock_db["audit_log"].find.return_value
        cursor.sort.return_value = cursor
        cursor.limit.return_value = []

        store.get_audit_log()

        mock_db["audit_log"].find.assert_called_once_with({}, {"_id": 0})

    def test_upsert_node_doc_returns_empty_string_when_no_existing_doc_found(self):
        # Pathological race: update_one reports no upsert, and a concurrent
        # delete removes the doc before the fallback find_one runs.
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].update_one.return_value = MagicMock(upserted_id=None)
        mock_db["nodes"].find_one.return_value = None

        assert store.upsert_node_doc("sp", "T", "n1", {}) == ""

    def test_upsert_node_doc_returns_empty_string_when_id_lookup_raises(self):
        # #375, same class of bug as builder.py's mongo block: the $set
        # write in update_one already landed (no exception, upserted_id is
        # falsy). The find_one below is only a best-effort lookup for an id
        # to decorate the return value with. If it raises, that must not
        # turn the write we already completed into a reported failure.
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].update_one.return_value = MagicMock(upserted_id=None)
        mock_db["nodes"].find_one.side_effect = RuntimeError("connection reset")

        assert store.upsert_node_doc("sp", "T", "n1", {}) == ""

    def test_upsert_node_doc_no_owner_mirror_when_owner_id_absent(self):
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].update_one.return_value = MagicMock(upserted_id="x")

        store.upsert_node_doc("sp", "T", "n1", {"name": "no-owner"})

        set_doc = mock_db["nodes"].update_one.call_args[0][1]["$set"]
        assert "owner_id" not in set_doc

    def test_upsert_node_doc_non_dict_properties_skips_owner_mirror(self):
        # properties is typed dict[str, Any] but the owner_id mirror guards
        # with isinstance() — verify a non-dict value degrades gracefully
        # instead of raising on properties.get(...).
        store, _client, mock_db = _make_connected_store()
        mock_db["nodes"].update_one.return_value = MagicMock(upserted_id="x")

        store.upsert_node_doc("sp", "T", "n1", properties="not-a-dict")  # type: ignore[arg-type]

        set_doc = mock_db["nodes"].update_one.call_args[0][1]["$set"]
        assert "owner_id" not in set_doc


# ---------------------------------------------------------------------------
# OntologyBuilder <-> mongo audit contract
# ---------------------------------------------------------------------------
#
# BUG FOUND: opencrab/ontology/builder.py's add_edge() calls
# self._mongo.log_event(...) for the "MongoDB audit" step (~line 263)
# *without* a try/except, unlike add_node()'s mongo block (~line 120-131)
# which wraps upsert_node_doc + log_event together. Once log_event was
# changed (this stage) to raise RuntimeError instead of silently
# swallowing insert failures, a transient Mongo write error during
# add_edge's audit step now propagates out of add_edge entirely — even
# though the Neo4j/SQL writes already succeeded. Fixed by wrapping the
# edge audit block in the same try/except + output["stores"]["docs"]
# error-marker pattern already used everywhere else in this file.


class _StubGraphOrSqlStore:
    """Minimal stand-in for Neo4jStore — reports unavailable. NOT used for
    ``sql`` anymore (see ``_make_builder``'s docstring): authorize() needs a
    real, queryable SQL store now."""

    available = False


class _AvailableGraphStub:
    """Minimal stand-in for Neo4jStore that reports available -- so
    add_edge/add_node's #148-point-6 "graph unavailable -> refuse the whole
    write" early exit does not fire before the mongo-specific branch under
    test in this class ever runs. ``upsert_node`` succeeds: since #495 a
    graph write that did not land stops the doc leg, so the docs/mongo
    marker this class tests is only reached after a landed graph write.
    Deliberately has no upsert_edge method: the resulting AttributeError is
    caught by add_edge's own try/except and reported as "graph": "error: ..."
    -- irrelevant to the edge audit marker this class tests.

    #162: ``lookup_node_type`` IS implemented here (returning a fixed type
    for any id), unlike the other write methods above. Since that fix,
    add_edge treats a missing/unavailable ``lookup_node_type`` as "cannot
    resolve either endpoint's type -> refuse the whole write", which would
    short-circuit before ever reaching the mongo branch this class tests --
    the same #148-point-6 early exit this stub exists to dodge.
    """

    # #148: the identity guard probes these before any write. A stub that
    # lacks them is "cannot verify", which is fail-closed and refuses the
    # write -- so the mongo branch under test would never be reached. Empty
    # answers = "this id is free", which is what this class assumes.
    def get_node(self, node_type, node_id):  # noqa: ARG002
        return None

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        return []

    def get_edge(self, *args):  # noqa: ARG002
        return None

    def upsert_node(self, node_type, node_id, properties, space_id):  # noqa: ARG002
        return dict(properties)

    def lookup_node_type(self, node_id):  # noqa: ARG002
        return "StubType"

    available = True


def _make_builder(mongo: MagicMock) -> tuple:
    """Returns ``(builder, pack_id)``.

    #148: add_node/add_edge now call current_principal() and
    authorize(sql, principal, pack_id) internally, and authorize() requires
    a REAL, queryable SQL store (it fails closed -- RuntimeError -- when
    ``sql.available`` is falsy, and assert_writable() issues real queries
    against it) -- the old always-unavailable stub can no longer stand in
    for ``sql`` here. ``neo4j`` is also switched from "always unavailable"
    to ``_AvailableGraphStub`` for the same #148-point-6 reason (see that
    class's docstring): this class's whole point is exercising the mongo
    docs-marker branch, which point 6 never reaches while the graph store
    reports unavailable.
    """
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.pack.ownership import create_pack
    from opencrab.stores.sql_store import SQLStore
    from tests._pack_fixtures import ensure_test_user

    sql = SQLStore("sqlite:///:memory:")
    ensure_test_user(sql, "test-user")
    pack_id = create_pack(sql, "test-user", "mongo-audit-test-pack")
    builder = OntologyBuilder(neo4j=_AvailableGraphStub(), mongo=mongo, sql=sql)
    return builder, pack_id


class TestOntologyBuilderMongoAuditContract:
    # #148: add_node/add_edge now call current_principal() internally --
    # bind a fixed test principal for every test in this class (see
    # conftest.py's bind_test_principal; scoped to this class only, not the
    # whole module, since the rest of this file doesn't need it).
    pytestmark = pytest.mark.usefixtures("bind_test_principal")

    def test_add_edge_docs_marker_unchanged_on_success(self):
        # Success-path marker stays the literal "audited" — pinned as a
        # public API shape by test_service_paths_characterization.py's
        # TestNodeEdgeWriteMCP/HTTP::test_add_edge_success_shape. Only the
        # error path changes in this fix.
        mongo = MagicMock(available=True)
        # #148: the identity guard probes the doc slot; a bare MagicMock
        # answers with another MagicMock, which is fail-closed.
        mongo.get_node_doc.return_value = None
        mongo.log_event.return_value = "ev-1"
        builder, pack_id = _make_builder(mongo)

        result = builder.add_edge("subject", "u1", "owns", "resource", "p1", pack_id=pack_id)

        assert result["stores"]["docs"] == "audited"

    def test_add_edge_docs_marker_is_error_when_log_event_raises(self):
        # Red before the fix: this raised RuntimeError out of add_edge
        # entirely instead of degrading gracefully like every other store
        # branch (graph/sql) and like add_node's mongo block.
        mongo = MagicMock(available=True)
        # #148: the identity guard probes the doc slot; a bare MagicMock
        # answers with another MagicMock, which is fail-closed.
        mongo.get_node_doc.return_value = None
        mongo.log_event.side_effect = RuntimeError("insert failed")
        builder, pack_id = _make_builder(mongo)

        result = builder.add_edge("subject", "u1", "owns", "resource", "p1", pack_id=pack_id)

        # #168: the caller-facing marker now carries only the exception's
        # type name, never the raw backend message ("insert failed").
        assert result["stores"]["docs"] == "error: RuntimeError"

    def test_add_edge_docs_marker_unavailable_when_mongo_unavailable(self):
        mongo = MagicMock(available=False)
        builder, pack_id = _make_builder(mongo)

        result = builder.add_edge("subject", "u1", "owns", "resource", "p1", pack_id=pack_id)

        assert result["stores"]["docs"] == "unavailable"
        mongo.log_event.assert_not_called()

    def test_add_node_docs_marker_symmetric_success_case(self):
        # Symmetric normal-path check: add_node's mongo block (already
        # try/except-protected) continues to report "ok (id=<mongo_id>)"
        # now that log_event returns a str instead of None. #375: also
        # pins that a fully successful write now gets its own "audit": "ok"
        # marker, separate from "docs".
        mongo = MagicMock(available=True)
        # #148: the identity guard probes the doc slot; a bare MagicMock
        # answers with another MagicMock, which is fail-closed.
        mongo.get_node_doc.return_value = None
        mongo.upsert_node_doc.return_value = "node-doc-1"
        mongo.log_event.return_value = "ev-2"
        builder, pack_id = _make_builder(mongo)

        result = builder.add_node(
            "subject", "User", "u1",
            {"name": "Alice", "email": "a@ex.com", "role": "admin"},
            pack_id=pack_id,
        )

        assert result["stores"]["docs"] == "ok (id=node-doc-1)"
        assert result["stores"]["audit"] == "ok"
        mongo.log_event.assert_called_once()

    def test_add_node_audit_marker_is_error_when_log_event_raises_after_doc_write_succeeds(self):
        # #375 RED before the fix: a log_event-only failure used to
        # overwrite the already-successful doc write's status to "error",
        # corrupting downstream err aggregation and same/chg convergence
        # judgments that read stores["docs"]. The doc write itself must
        # keep reporting success; only "audit" carries the failure.
        mongo = MagicMock(available=True)
        mongo.get_node_doc.return_value = None
        mongo.upsert_node_doc.return_value = "node-doc-2"
        mongo.log_event.side_effect = RuntimeError("audit_log insert failed")
        builder, pack_id = _make_builder(mongo)

        result = builder.add_node(
            "subject", "User", "u2",
            {"name": "Bob", "email": "b@ex.com", "role": "admin"},
            pack_id=pack_id,
        )

        assert result["stores"]["docs"] == "ok (id=node-doc-2)"
        assert result["stores"]["audit"] == "error: RuntimeError"

    def test_add_node_audit_marker_skipped_when_doc_write_itself_fails(self):
        # #375, control-flow-unchanged half of the fix (§3-1): when the doc
        # write itself fails, log_event must still not be attempted (same
        # behavior as before this fix), and "audit" must not silently read
        # as "unavailable" or "ok" -- it gets its own skip marker distinct
        # from a genuine audit failure.
        mongo = MagicMock(available=True)
        mongo.get_node_doc.return_value = None
        mongo.upsert_node_doc.side_effect = RuntimeError("nodes collection write failed")
        builder, pack_id = _make_builder(mongo)

        result = builder.add_node(
            "subject", "User", "u3",
            {"name": "Carol", "email": "c@ex.com", "role": "admin"},
            pack_id=pack_id,
        )

        assert result["stores"]["docs"] == "error: RuntimeError"
        assert result["stores"]["audit"] == "skipped (doc write failed)"
        mongo.log_event.assert_not_called()


# ---------------------------------------------------------------------------
# #516 manifest D1: raw-state CAS of MongoStore.rewrite_source_metadata
# ---------------------------------------------------------------------------
#
# These cases run against a mocked pymongo collection. They do not prove what a
# MongoDB server does (the manifest keeps that as a separate server gate). The
# fake ``sources`` collection applies the update filter with an evaluator that
# is written here from the MongoDB aggregation rules. It never calls the
# product's query builder. The evaluator reads only the ``$expr`` document that
# the product hands to ``update_one``.

_ABSENT = object()


class _EvalError(Exception):
    """The expression could not be evaluated (the server would reject it)."""


def _kind(value):
    if value is _ABSENT:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise _EvalError(f"unsupported value {value!r}")


def _mongo_equal(left, right):
    """Aggregation ``$eq``: ints and doubles compare by value, nothing else crosses kinds."""
    lk, rk = _kind(left), _kind(right)
    if {lk, rk} <= {"int", "double"}:
        return left == right
    if lk != rk:
        return False
    if lk == "array":
        return len(left) == len(right) and all(_mongo_equal(a, b) for a, b in zip(left, right))
    if lk == "object":
        return set(left) == set(right) and all(_mongo_equal(left[k], right[k]) for k in left)
    if lk in ("missing", "null"):
        return True
    return left == right


def _strict_same(left, right):
    """Same BSON kind at every depth and equal values (an int is not a double)."""
    if _kind(left) != _kind(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_strict_same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return set(left) == set(right) and all(_strict_same(left[k], right[k]) for k in left)
    return left == right


def _truthy(value):
    if value is _ABSENT or value is None or value is False:
        return False
    return not (isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0)


def _path(value, parts):
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part, _ABSENT)
        elif isinstance(value, list):
            value = [item[part] for item in value if isinstance(item, dict) and part in item]
        else:
            return _ABSENT
    return value


def _eval(expr, doc, variables):
    if isinstance(expr, str):
        if expr.startswith("$$"):
            name, *rest = expr[2:].split(".")
            return _path(variables[name], rest)
        if expr.startswith("$"):
            return _path(doc, expr[1:].split("."))
        return expr
    if isinstance(expr, list):
        return [_eval(item, doc, variables) for item in expr]
    if not isinstance(expr, dict):
        return expr
    if len(expr) != 1:
        raise _EvalError(f"expression object needs one operator: {expr!r}")
    ((op, arg),) = expr.items()
    if op == "$literal":
        return arg
    if op == "$type":
        return _kind(_eval(arg, doc, variables))
    if op == "$eq":
        left, right = (_eval(item, doc, variables) for item in arg)
        return _mongo_equal(left, right)
    if op == "$and":
        # The server does not promise short-circuit evaluation or order, so every
        # argument is evaluated and any error makes the whole expression an error.
        values = [_eval(item, doc, variables) for item in arg]
        return all(_truthy(value) for value in values)
    if op == "$cond":
        condition, then, other = arg
        return _eval(then if _truthy(_eval(condition, doc, variables)) else other, doc, variables)
    if op == "$setEquals":
        left, right = (_eval(item, doc, variables) for item in arg)
        if not isinstance(left, list) or not isinstance(right, list):
            raise _EvalError("$setEquals needs arrays")
        return (all(any(_mongo_equal(a, b) for b in right) for a in left)
                and all(any(_mongo_equal(a, b) for b in left) for a in right))
    if op == "$map":
        source = _eval(arg["input"], doc, variables)
        if source is None or source is _ABSENT:
            return None
        return [_eval(arg["in"], doc, {**variables, arg["as"]: item}) for item in source]
    if op == "$objectToArray":
        value = _eval(arg, doc, variables)
        if value is None or value is _ABSENT:
            return None
        if not isinstance(value, dict):
            raise _EvalError("$objectToArray needs an object")
        return [{"k": key, "v": item} for key, item in value.items()]
    raise _EvalError(f"unsupported operator {op}")


def _match(query, doc):
    """Return ``match``, ``no-match`` or ``error`` for an ``$expr`` filter."""
    if doc is None:
        return "no-match"
    try:
        return "match" if _truthy(_eval(query["$expr"], doc, {})) else "no-match"
    except _EvalError:
        return "error"


def _project(doc, projection):
    included = [k for k, v in (projection or {}).items() if v and k != "_id"]
    if included:
        return {k: copy.deepcopy(doc[k]) for k in included if k in doc}
    excluded = {k for k, v in (projection or {}).items() if not v}
    return {k: copy.deepcopy(v) for k, v in doc.items() if k not in excluded}


class _CasHarness:
    """One document behind a mocked ``sources`` collection and its update log."""

    def __init__(self, document):
        self.doc = copy.deepcopy(document)
        self.race = None
        self.finds = []
        self.recreated = False
        self.updates = []
        self.result = None  # (matched, modified) to force; None applies the filter
        self.store, _client, db = _make_connected_store()
        db["sources"].find_one.side_effect = self._find_one
        db["sources"].update_one.side_effect = self._update_one

    def _find_one(self, query, projection=None):
        self.finds.append(query)
        if self.doc is None or not _query_match(query, self.doc):
            return None
        return _project(self.doc, projection)

    def _update_one(self, query, update, upsert=False):
        before = _match(query, self.doc)
        if self.race is not None:
            self.race(self)
        after = _match(query, self.doc)
        self.updates.append(SimpleNamespace(query=query, update=update, upsert=upsert, before=before, after=after))
        if self.result is not None:
            matched, modified = self.result
        else:
            matched = modified = 1 if after == "match" else 0
        if matched and self.doc is not None:
            self.doc["metadata"] = update["$set"]["metadata"]
        if upsert and not matched and self.doc is None:
            self.recreated = True
        return SimpleNamespace(matched_count=matched, modified_count=modified)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_BODY = "body"


def _doc(state=_ABSENT):
    doc = {"source_id": "source-1", "text": _BODY}
    if state is not _ABSENT:
        doc["source_integrity"] = state
    return doc


def _complete():
    return {"version": 1, "state": "complete", "text_digest": _digest(_BODY)}


def _unknown():
    return {"version": 1, "state": "unknown"}


def _clauses(harness):
    return harness.updates[-1].query["$expr"]["$and"]


def _require_type(clauses, path, kind, name):
    found = [c for c in clauses if isinstance(c.get("$eq"), list) and c["$eq"][0] == {"$type": path}]
    assert len(found) == 1, name
    assert found[0]["$eq"][1] == kind, name


def _require_literal(clauses, path, expected, name):
    found = [
        c for c in clauses
        if isinstance(c.get("$eq"), list) and c["$eq"][0] == path
        and isinstance(c["$eq"][1], dict) and set(c["$eq"][1]) == {"$literal"}
    ]
    assert len(found) == 1, name
    assert _strict_same(expected, found[0]["$eq"][1]["$literal"]), name


def _require_object_guard(clauses, name):
    guarded = [c["$cond"] for c in clauses if "$cond" in c]
    assert len(guarded) == 1, name
    assert guarded[0][0] == {"$eq": [{"$type": "$source_integrity"}, "object"]}, name
    assert guarded[0][2] is False, name
    return guarded[0][1]["$and"]


def _require_keyset(nested, keys, name):
    found = [c["$setEquals"] for c in nested if "$setEquals" in c]
    assert len(found) == 1, name
    expected_map = {"$map": {"input": {"$objectToArray": "$source_integrity"}, "as": "field", "in": "$$field.k"}}
    assert found[0][0] == expected_map and found[0][1] == {"$literal": sorted(keys)}, name


def _require_state_fields(harness, state, label):
    """Assert the nested type and literal clauses for every field of ``state``."""
    nested = _require_object_guard(_clauses(harness), f"require_{label}_object_cond_type")
    _require_keyset(nested, state, f"require_{label}_keyset")
    for key, value in state.items():
        name = "digest" if key == "text_digest" else key
        _require_type(nested, f"$source_integrity.{key}", _kind(value), f"require_{label}_{name}_type")
        _require_literal(nested, f"$source_integrity.{key}", value, f"require_{label}_{name}_literal")


def _top_level_guards(harness):
    clauses = _clauses(harness)
    _require_type(clauses, "$source_id", "string", "require_source_id_type")
    _require_literal(clauses, "$source_id", "source-1", "require_source_id_literal")
    _require_type(clauses, "$text", "string", "require_text_type")
    _require_literal(clauses, "$text", _BODY, "require_text_literal")


def _refused(document, race):
    """Run one rewrite whose document changes after the re-read; return the harness."""
    harness = _CasHarness(document)
    snapshot = harness.store.read_source_state_exact("source-1")
    assert snapshot is not None
    harness.race = race
    with pytest.raises(ValueError, match="source state changed"):
        harness.store.rewrite_source_metadata("source-1", {"pack_id": "new"}, snapshot)
    update = harness.updates[-1]
    assert update.upsert is False
    assert update.before == "match", "the filter must match the document it was built from"
    assert update.after == "no-match", update.after
    if harness.doc is not None:
        assert "metadata" not in harness.doc, "a refused rewrite must not write"
    return harness


def _set(key, value):
    return lambda h: h.doc.__setitem__(key, value)


def _set_state(key, value):
    return lambda h: h.doc["source_integrity"].__setitem__(key, value)


class TestMongoSourceIntegrityRewriteCas:
    def test_evaluator_follows_mongo_equality(self):
        """The evaluator keeps the rules that make the type clauses load-bearing."""
        assert _mongo_equal(1, 1.0) is True
        assert _mongo_equal(True, 1) is False
        assert _mongo_equal(_ABSENT, None) is False
        assert _mongo_equal(["a"], "a") is False
        assert _strict_same(1, 1.0) is False

    @pytest.mark.parametrize("value", [["source-1"], 5, "other"], ids=["array", "int", "other-string"])
    def test_source_id_array_or_non_string_refuses(self, value):
        harness = _refused(_doc(_unknown()), _set("source_id", value))
        _top_level_guards(harness)

    @pytest.mark.parametrize("value", [["body"], 5, "other"], ids=["array", "int", "other-string"])
    def test_text_array_or_non_string_refuses(self, value):
        harness = _refused(_doc(_unknown()), _set("text", value))
        _top_level_guards(harness)

    def test_missing_to_null_race_refuses(self):
        harness = _refused(_doc(), _set("source_integrity", None))
        _require_type(_clauses(harness), "$source_integrity", "missing", "require_missing_top_type")

    def test_null_to_array_race_refuses(self):
        harness = _refused(_doc(None), _set("source_integrity", [None]))
        _require_type(_clauses(harness), "$source_integrity", "null", "require_null_top_type")

    def test_object_to_array_race_refuses(self):
        harness = _refused(_doc(_unknown()), lambda h: h.doc.__setitem__("source_integrity", [h.doc["source_integrity"]]))
        _require_object_guard(_clauses(harness), "require_object_cond_type")

    @pytest.mark.parametrize("value", [1.0, 2], ids=["double", "changed-int"])
    def test_version_int_to_double_race_refuses(self, value):
        harness = _refused(_doc(_complete()), _set_state("version", value))
        _require_state_fields(harness, _complete(), "complete")
        assert _mongo_equal(1, 1.0), "only the $type clause separates int from double"

    @pytest.mark.parametrize("value", [[1], 2], ids=["array", "changed-int"])
    def test_version_int_to_array_race_refuses(self, value):
        harness = _refused(_doc(_unknown()), _set_state("version", value))
        _require_state_fields(harness, _unknown(), "unknown")

    @pytest.mark.parametrize("value", [["complete"], "other"], ids=["array", "other-string"])
    def test_state_string_to_array_race_refuses(self, value):
        harness = _refused(_doc(_complete()), _set_state("state", value))
        _require_state_fields(harness, _complete(), "complete")

    def test_digest_string_to_array_race_refuses(self):
        harness = _refused(_doc(_complete()), _set_state("text_digest", [_digest(_BODY)]))
        _require_state_fields(harness, _complete(), "complete")

    @pytest.mark.parametrize("change", ["extra-key", "missing-digest"])
    def test_complete_extra_or_missing_field_refuses(self, change):
        def race(h):
            if change == "extra-key":
                h.doc["source_integrity"]["extra"] = True
            else:
                del h.doc["source_integrity"]["text_digest"]

        harness = _refused(_doc(_complete()), race)
        _require_state_fields(harness, _complete(), "complete")

    @pytest.mark.parametrize("change", ["extra-key", "digest-added", "state-array", "state-complete"])
    def test_unknown_extra_or_digest_field_refuses(self, change):
        def race(h):
            state = h.doc["source_integrity"]
            if change == "extra-key":
                state["extra"] = True
            elif change == "digest-added":
                state["text_digest"] = _digest(_BODY)
            elif change == "state-array":
                state["state"] = ["unknown"]
            else:
                state["state"] = "complete"

        harness = _refused(_doc(_unknown()), race)
        _require_state_fields(harness, _unknown(), "unknown")

    def test_complete_digest_change_race_refuses(self):
        harness = _refused(_doc(_complete()), _set_state("text_digest", "0" * 64))
        _require_state_fields(harness, _complete(), "complete")

    def test_source_delete_race_does_not_recreate(self):
        harness = _refused(_doc(_complete()), lambda h: setattr(h, "doc", None))
        assert harness.updates[-1].upsert is False
        assert harness.doc is None and harness.recreated is False

    def test_match_zero_raises_and_modified_zero_succeeds(self):
        # matched 1, modified 0 (the stored metadata already equals the new one): success
        harness = _CasHarness(_doc(_complete()))
        snapshot = harness.store.read_source_state_exact("source-1")
        harness.result = (1, 0)
        assert harness.store.rewrite_source_metadata("source-1", {"pack_id": "same"}, snapshot) == "source-1"
        assert harness.updates[-1].upsert is False
        # matched 0 (a change that the filter rejects): conflict
        harness = _CasHarness(_doc(_complete()))
        snapshot = harness.store.read_source_state_exact("source-1")
        harness.result = (0, 0)
        with pytest.raises(ValueError, match="source state changed"):
            harness.store.rewrite_source_metadata("source-1", {"pack_id": "same"}, snapshot)
        # matched 1, modified 1 (an ordinary rewrite) writes the metadata
        harness = _CasHarness(_doc(_complete()))
        snapshot = harness.store.read_source_state_exact("source-1")
        assert harness.store.rewrite_source_metadata("source-1", {"pack_id": "new"}, snapshot) == "source-1"
        assert harness.doc["metadata"] == {"pack_id": "new"}
        assert harness.updates[-1].before == "match" and harness.updates[-1].after == "match"


def _exact_expr_clauses(query):
    """Return the ``$expr`` ``$and`` clauses of an exact-source filter."""
    if "$expr" in query:
        return query["$expr"]["$and"]
    return next(part for part in query["$and"] if "$expr" in part)["$expr"]["$and"]


def test_exact_source_reader_rejects_array_source_id():
    """MID-01. Both exact readers accept a scalar string source_id only.

    A find on ``{"source_id": "source-1"}`` would match a row whose source_id is
    ``["source-1"]``. The readers use an ``$expr`` filter instead. This case
    evaluates that filter on an array-valued row and on a scalar control row.
    """
    row = {"text": _BODY, "source_integrity": _unknown(), "metadata": {"pack_id": "pack"}}
    for bad in (["source-1"], ["source-1", "other"], 5):
        harness = _CasHarness({**row, "source_id": bad})
        assert harness.store.read_source_state_exact("source-1") is None, bad
        assert harness.store.get_source_scoped_exact("source-1", ["pack"]) is None, bad
        assert len(harness.finds) == 2
        for query in harness.finds:
            assert not _query_match(query, harness.doc), bad
            clauses = _exact_expr_clauses(query)
            _require_type(clauses, "$source_id", "string", "require_source_id_type")
            _require_literal(clauses, "$source_id", "source-1", "require_source_id_literal")
    control = _CasHarness({**row, "source_id": "source-1"})
    assert control.store.read_source_state_exact("source-1") is not None
    scoped = control.store.get_source_scoped_exact("source-1", ["pack"])
    assert scoped is not None and scoped["source_id"] == "source-1"


# ---- query matcher for the filters of the Mongo scoped readers (test_mongo_unit.py) ----

def _field_values(doc, path):
    """Candidate values of a dotted path; arrays on the way are walked element by element."""
    current = [doc]
    for part in path.split("."):
        nxt = []
        for value in current:
            if isinstance(value, dict):
                nxt.append(value.get(part, _ABSENT))
            elif isinstance(value, list):
                nxt.extend(item.get(part, _ABSENT) for item in value if isinstance(item, dict))
        current = nxt
    return current


def _type_matches(value, name):
    if name == "array":
        return isinstance(value, list)
    if isinstance(value, list):
        return any(_kind(item) == name for item in value)
    return _kind(value) == name


def _operator_matches(values, operators):
    for op, arg in operators.items():
        if op == "$in":
            hit = False
            for value in values:
                candidates = [value, *(value if isinstance(value, list) else [])]
                for candidate in candidates:
                    if any(_mongo_equal(candidate, item) for item in arg if item is not None):
                        hit = True
                    if None in arg and (candidate is None or candidate is _ABSENT):
                        hit = True
            if not hit:
                return False
        elif op == "$type":
            if not any(_type_matches(value, arg) for value in values):
                return False
        elif op == "$not":
            if _operator_matches(values, arg):
                return False
        else:
            raise AssertionError(f"unsupported query operator {op}")
    return True


def _query_match(query, doc):
    """Evaluate the query documents that MongoStore sends: $and, $expr and field operators."""
    for key, value in query.items():
        if key == "$and":
            if not all(_query_match(part, doc) for part in value):
                return False
        elif key == "$expr":
            if _match({"$expr": value}, doc) != "match":
                return False
        elif key.startswith("$"):
            raise AssertionError(f"unsupported query operator {key}")
        elif isinstance(value, dict) and all(op.startswith("$") for op in value):
            if not _operator_matches(_field_values(doc, key), value):
                return False
        else:
            raise AssertionError(f"unsupported field condition {key}")
    return True


def test_scoped_filter_matcher_follows_the_product_filter():
    """The query matcher evaluates the whole filter that get_source_scoped_exact sends.

    The matcher is the fixture of the Mongo cases above. This case checks it against the filter of
    the product on rows that must match and rows that must not."""
    base = {"source_id": "source-1", "text": _BODY, "metadata": {"pack_id": "pack"}}
    cases = {
        "normal": (base, True),
        "pack-out-of-scope": ({**base, "metadata": {"pack_id": "other"}}, False),
        "metadata-array": ({**base, "metadata": [{"pack_id": "pack"}]}, False),
        "pack-id-array": ({**base, "metadata": {"pack_id": ["pack"]}}, False),
        "source-id-array": ({**base, "source_id": ["source-1"]}, False),
    }
    for name, (document, expected) in cases.items():
        harness = _CasHarness(document)
        row = harness.store.get_source_scoped_exact("source-1", ["pack"])
        assert (row is not None) is expected, name
        query = harness.finds[-1]
        assert _query_match(query, document) is expected, name
    with pytest.raises(AssertionError, match="unsupported query operator"):
        _query_match({"metadata.pack_id": {"$regex": "p"}}, base)
