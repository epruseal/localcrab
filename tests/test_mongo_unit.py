"""
Contract tests for MongoStore using a fully mocked pymongo client.

No network I/O and no real connection attempts — everything is patched via
``pymongo.MongoClient`` so these tests are instant. This is a deliberate
contrast to tests/test_stores.py::TestMongoStoreUnit, which connects to a
real (invalid) host and burns ~20s total across the suite in
serverSelectionTimeoutMS waits.
"""

from __future__ import annotations

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
    """Minimal stand-in for Neo4jStore/SQLStore — reports unavailable so
    add_edge's graph/sql branches take the simple 'unavailable' path and
    only the mongo audit branch under test is exercised."""

    available = False


def _make_builder(mongo: MagicMock):
    from opencrab.ontology.builder import OntologyBuilder

    return OntologyBuilder(
        neo4j=_StubGraphOrSqlStore(), mongo=mongo, sql=_StubGraphOrSqlStore()
    )


class TestOntologyBuilderMongoAuditContract:
    def test_add_edge_docs_marker_unchanged_on_success(self):
        # Success-path marker stays the literal "audited" — pinned as a
        # public API shape by test_service_paths_characterization.py's
        # TestNodeEdgeWriteMCP/HTTP::test_add_edge_success_shape. Only the
        # error path changes in this fix.
        mongo = MagicMock(available=True)
        mongo.log_event.return_value = "ev-1"
        builder = _make_builder(mongo)

        result = builder.add_edge("subject", "u1", "owns", "resource", "p1")

        assert result["stores"]["docs"] == "audited"

    def test_add_edge_docs_marker_is_error_when_log_event_raises(self):
        # Red before the fix: this raised RuntimeError out of add_edge
        # entirely instead of degrading gracefully like every other store
        # branch (graph/sql) and like add_node's mongo block.
        mongo = MagicMock(available=True)
        mongo.log_event.side_effect = RuntimeError("insert failed")
        builder = _make_builder(mongo)

        result = builder.add_edge("subject", "u1", "owns", "resource", "p1")

        assert result["stores"]["docs"] == "error: insert failed"

    def test_add_edge_docs_marker_unavailable_when_mongo_unavailable(self):
        mongo = MagicMock(available=False)
        builder = _make_builder(mongo)

        result = builder.add_edge("subject", "u1", "owns", "resource", "p1")

        assert result["stores"]["docs"] == "unavailable"
        mongo.log_event.assert_not_called()

    def test_add_node_docs_marker_symmetric_success_case(self):
        # Symmetric normal-path check: add_node's mongo block (already
        # try/except-protected) continues to report "ok (id=<mongo_id>)"
        # now that log_event returns a str instead of None.
        mongo = MagicMock(available=True)
        mongo.upsert_node_doc.return_value = "node-doc-1"
        mongo.log_event.return_value = "ev-2"
        builder = _make_builder(mongo)

        result = builder.add_node(
            "subject", "User", "u1",
            {"name": "Alice", "email": "a@ex.com", "role": "admin"},
        )

        assert result["stores"]["docs"] == "ok (id=node-doc-1)"
        mongo.log_event.assert_called_once()
