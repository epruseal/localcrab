"""
MongoDB document store adapter.

Stores rich ontology node documents, ingested source records, and
audit logs. Uses pymongo with a connection pool.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ownership predicate fragments (issue #222)
# ---------------------------------------------------------------------------
# MongoDB's ``$in`` and ``$type`` do NOT test an array value as a whole: if the
# field holds an array they test its ELEMENTS ("If ``field`` has an array, the
# ``$in`` operator selects the documents whose ``field`` has an array that
# contains at least one element that matches a value in the specified array";
# "For documents where ``field`` is an array, ``$type`` returns documents in
# which at least one array element matches a type passed to ``$type``"). A
# dotted path traverses an array of embedded documents the same way. The SQL
# canon treats none of those shapes as owning, so mongo used to match rows SQL
# excluded -- a fork copying beyond its pack and a read scope leaking across
# packs. The array exclusion is built HERE, once, and reused by every leg of
# both predicates: the two implementations diverged in the first place because
# each expressed the same meaning in its own words.


def _array() -> dict[str, Any]:
    """``$type: "array"`` -- a FIELD-level test ("Queries for ``$type:
    'array'`` return documents where the field itself is an array"), so
    wrapping it in ``$not`` excludes arrays without the element-traversal
    ambiguity that makes ``$not`` unreliable with comparison operators.

    Returns a fresh dict every call: these fragments end up aliased several
    times inside one query document, and a shared module-level dict would let
    a caller mutating the returned query corrupt every later query.
    """
    return {"$type": "array"}


# Values ``SqlDialect.json_truthy_text`` folds to SQL NULL -- the SQL canon's
# definition of "this row has no pack_id", which is what opens the legacy
# ``source`` fallback. Note ``0`` and ``0.0`` are mutually redundant under
# BSON's by-value numeric comparison; both are kept because this list is
# transcribed from the SQL side, not re-derived.
_FALSY_PACK_VALUES: list[Any] = [None, "", 0, 0.0, False]


def _scalar_string_in(values: list[str]) -> dict[str, Any]:
    """Membership that matches ONLY a scalar BSON string (issue #222).

    Mirrors the SQL canon's ``_json_str_in`` (``opencrab/stores/_sql_doc_base``),
    which requires ``json_type='text'`` / ``jsonb_typeof='string'`` before
    comparing. ``$type: "string"`` is what carries that requirement across;
    it is redundant for scalars while ``values`` is a list of ``str`` (BSON
    equality is type-strict, so a number never matches a bound string), and it
    is the defence that keeps a contract-violating non-string entry in
    ``pack_ids`` failing the same way SQL would.

    CONTRACT -- STRING-ONLY, AND IT DIVERGES FROM ONE SQL PREDICATE ON
    PURPOSE (issue #222; canon decision tracked in issue #226). A non-string
    ``pack_id`` -- number, boolean, array, object -- is NOT owning here. Do
    not "fix" that by widening this predicate: the SQL side is not
    self-consistent, so there is no single behaviour to converge on.
    ``opencrab/pack/load.py``'s ``_json_str_eq`` (what ``delete_pack`` uses on
    both the doc-node and graph axes) is string-strict and its docstring calls
    a non-string ``pack_id`` defined-unsupported;
    ``_SqlDocStoreBase.list_nodes_scoped``'s ``json_truthy_text`` stringifies
    numbers and booleans and so answers differently on the same row. This
    predicate follows the string-strict one -- the policy the repository
    states, and the one ``scripts/migrate_pack_ownership.py``'s
    ``_classify_pack_id`` enforces by classifying such values ``malformed``.

    CONSEQUENCE, ACCEPTED: ``ontology_list_nodes`` can answer differently on
    a mongo deployment than on a SQL one, for a node whose ``pack_id`` is
    stored as a non-string. The direction is UNDER-inclusion -- mongo sees
    less, never more -- so it is fail-closed and cannot move data across a
    pack boundary, which is the invariant #222 exists to protect. Issue #226
    decides the canon (string-only everywhere, or promote non-strings) and
    adds the write-time enforcement that would stop such values being stored
    at all. ``tests/test_mongo_owner_equivalence.py``'s
    ``TestKnownContractGap`` pins the current difference class by class so
    #226 can see in a diff exactly what it changes.
    """
    return {"$in": list(values), "$type": "string", "$not": _array()}


def _scalar_falsy() -> dict[str, Any]:
    """"This row has no pack_id" -- the same set ``json_truthy_text(...) IS
    NULL`` picks out on the SQL side, with arrays excluded.

    Mongo's ``$in`` already treats a bound ``None`` as matching both "missing"
    and "null", so no separate ``$exists`` clause is needed. The array
    exclusion matters here on its own: without it ``pack_id: [0]`` or
    ``[None]`` reads as absent (element traversal finds a falsy element) and
    wrongly opens the legacy ``source`` fallback, while SQL sees a non-NULL
    raw JSON text and keeps the row out.
    """
    return {"$in": list(_FALSY_PACK_VALUES), "$not": _array()}


def _non_array(field: str) -> dict[str, Any]:
    """Guard for the metadata/properties CONTAINER itself.

    A dotted path traverses an array of embedded documents, so a row shaped
    ``{"metadata": [{"pack_id": "p"}]}`` matches ``metadata.pack_id`` even
    though the container is not a document. SQL extracts NULL from such a row
    and excludes it. Excluding the array container here closes the same class
    of leak the value-level guards close.

    No ``$type: "object"`` companion: it is redundant (a string/number/missing
    container leaves the dotted path unresolved, which already fails both
    legs) and it has no counterpart on the SQL side, where container exclusion
    falls out of JSON path extraction rather than being written down.
    """
    return {field: {"$not": _array()}}


class MongoStore:
    """MongoDB adapter for document-oriented ontology storage."""

    def __init__(self, uri: str, db_name: str) -> None:
        self._uri = uri
        self._db_name = db_name
        self._client: Any = None
        self._db: Any = None
        self._available = False
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            from pymongo import MongoClient  # type: ignore[import]

            self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
            # Force connection check
            self._client.admin.command("ping")
            self._db = self._client[self._db_name]
            self._available = True
            logger.info("MongoDB connected (db=%s)", self._db_name)
            self._ensure_indexes()
        except Exception as exc:
            logger.warning("MongoDB unavailable: %s", exc)
            self._available = False

    def _ensure_indexes(self) -> None:
        """Create indexes for common query patterns."""
        try:
            # nodes collection: unique on (space, node_id)
            self._db["nodes"].create_index(
                [("space", 1), ("node_id", 1)], unique=True
            )
            # owner_id lookups (top-level + legacy nested path)
            self._db["nodes"].create_index("owner_id")
            self._db["nodes"].create_index("properties.owner_id")
            # sources collection
            self._db["sources"].create_index("source_id", unique=True)
            self._db["sources"].create_index("user_id")
            self._db["sources"].create_index("metadata.user_id")
            # audit_log: sorted by timestamp; compound for query counters
            self._db["audit_log"].create_index([("timestamp", -1)])
            self._db["audit_log"].create_index([("subject_id", 1), ("event_type", 1)])
            self._db["audit_log"].create_index([("actor", 1), ("event_type", 1)])
        except Exception as exc:
            logger.debug("MongoDB index creation: %s", exc)

    @property
    def available(self) -> bool:
        return self._available

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("MongoDB is not available.")

    def ping(self) -> bool:
        """Return True if MongoDB is reachable."""
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Node document operations
    # ------------------------------------------------------------------

    def upsert_node_doc(
        self,
        space: str,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
    ) -> str:
        """
        Upsert a node document. Returns the MongoDB _id as string.

        `properties.owner_id` is mirrored to a top-level `owner_id` column so
        ownership counters can use an indexed field instead of a nested path.
        """
        self._require_available()

        doc: dict[str, Any] = {
            "space": space,
            "node_type": node_type,
            "node_id": node_id,
            "properties": properties,
            "updated_at": datetime.now(tz=UTC),
        }
        owner_id = properties.get("owner_id") if isinstance(properties, dict) else None
        if owner_id is not None:
            doc["owner_id"] = owner_id
        result = self._db["nodes"].update_one(
            {"space": space, "node_id": node_id},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(tz=UTC)}},
            upsert=True,
        )
        if result.upserted_id:
            return str(result.upserted_id)
        # Return existing doc id
        existing = self._db["nodes"].find_one(
            {"space": space, "node_id": node_id}, {"_id": 1}
        )
        return str(existing["_id"]) if existing else ""

    def get_node_doc(self, space: str, node_id: str) -> dict[str, Any] | None:
        """Retrieve a node document by space and node_id."""
        self._require_available()

        doc = self._db["nodes"].find_one(
            {"space": space, "node_id": node_id}, {"_id": 0}
        )
        return dict(doc) if doc else None

    def list_nodes(
        self, space: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List node documents, optionally filtered by space.

        ``limit <= 0`` (issue #120 follow-up): returns ``[]`` without
        querying. pymongo's own ``Cursor.limit(0)`` means "no limit" (see
        its docstring), which is the opposite of what a caller passing 0
        rows requested means -- same class of footgun as SQLite mapping a
        bound ``LIMIT -1`` to "no limit" (see ``_sql_doc_base.py``'s
        ``list_nodes``), just triggered by a different value on a different
        engine. This guard makes 0 and negative agree with every other
        backend regardless of which engine is behind the store.
        """
        self._require_available()
        if limit <= 0:
            return []

        query: dict[str, Any] = {}
        if space:
            query["space"] = space
        cursor = self._db["nodes"].find(query, {"_id": 0}).limit(limit)
        return [dict(doc) for doc in cursor]

    def list_nodes_scoped(
        self, pack_ids: list[str], space: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Authorization-scoped counterpart to ``list_nodes`` (issue #147
        §3.5) -- see ``_SqlDocStoreBase.list_nodes_scoped``'s docstring for
        the full rationale (same contract, Mongo query form). No ``.sort()``
        here, matching ``list_nodes``' own lack of one -- this method does
        not add an ordering guarantee ``list_nodes`` never had.

        Empty ``pack_ids`` -> ``[]`` WITHOUT querying. ``limit <= 0`` ->
        ``[]``, same guard ``list_nodes`` uses (issue #120 follow-up).

        SCALAR STRINGS ONLY (issue #222): the bare ``$in`` this used to carry
        matched a row whose ``properties.pack_id`` was an ARRAY containing a
        scoped id, and a row whose whole ``properties`` was an array of
        embedded documents -- SQL excludes both, so the same data landed in
        different scopes depending on the backend. This is a READ scope
        (``ontology_list_nodes``'s pack-unspecified branch), so the leak was
        cross-pack visibility, not only a fork range issue. See
        ``_scalar_string_in``/``_non_array`` for the shared exclusion and for
        the documented contract on non-string pack_ids.

        ``space`` stays a plain equality with no array guard: it is a space
        filter rather than an ownership term, ``upsert_node_doc`` types it
        ``str``, and the SQL twin stores it in a TEXT column where an array
        value cannot exist -- so no cross-backend fixture could exercise a
        guard here and it would be a clause no test could kill."""
        self._require_available()
        if not pack_ids or limit <= 0:
            return []

        query: dict[str, Any] = {
            **_non_array("properties"),
            "properties.pack_id": _scalar_string_in(list(pack_ids)),
        }
        if space:
            query["space"] = space
        cursor = self._db["nodes"].find(query, {"_id": 0}).limit(limit)
        return [dict(doc) for doc in cursor]

    def delete_node_doc(self, space: str, node_id: str) -> bool:
        """Delete a node document. Returns True if deleted."""
        self._require_available()

        result = self._db["nodes"].delete_one({"space": space, "node_id": node_id})
        return result.deleted_count > 0

    # ------------------------------------------------------------------
    # Source / ingestion records
    # ------------------------------------------------------------------

    def upsert_source(
        self,
        source_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> str:
        """Store or update a raw ingestion source document.

        `metadata.user_id` is mirrored to a top-level `user_id` column so
        ownership counters can use an indexed field instead of a nested path.
        """
        self._require_available()

        doc: dict[str, Any] = {
            "source_id": source_id,
            "text": text,
            "metadata": metadata,
            "updated_at": datetime.now(tz=UTC),
        }
        user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
        if user_id is not None:
            doc["user_id"] = user_id
        result = self._db["sources"].update_one(
            {"source_id": source_id},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(tz=UTC)}},
            upsert=True,
        )
        if result.upserted_id:
            return str(result.upserted_id)
        existing = self._db["sources"].find_one({"source_id": source_id}, {"_id": 1})
        return str(existing["_id"]) if existing else ""

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        """Retrieve a source document."""
        self._require_available()

        doc = self._db["sources"].find_one({"source_id": source_id}, {"_id": 0})
        return dict(doc) if doc else None

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        """List all ingested sources.

        ``limit <= 0``: returns ``[]`` without querying -- same contract as
        ``list_nodes`` above."""
        self._require_available()
        if limit <= 0:
            return []

        cursor = self._db["sources"].find({}, {"_id": 0, "text": 0}).limit(limit)
        return [dict(doc) for doc in cursor]

    def list_sources_scoped(self, pack_ids: list[str], limit: int = 100) -> list[dict[str, Any]]:
        """Authorization-scoped counterpart to ``list_sources`` (issue #201
        §4-B) -- see ``_SqlDocStoreBase.list_sources_scoped``'s docstring
        for the full rationale (same contract, Mongo query form) and
        ``_doc_owner_pred_scoped`` (same module) for the ownership
        predicate this replicates.

        No Mongo-native canon exists to reuse or diverge from here:
        ``opencrab/pack/load.py``'s ``delete_pack`` never branches for a
        Mongo ``docs`` store (it calls ``docs._dialect`` directly, which
        MongoStore has no equivalent of) -- Mongo is a `docker`-mode-only
        backend with no existing doc_sources ownership logic anywhere in
        the codebase for this method to drift from. This is that logic's
        first appearance for Mongo, expressed in Mongo's own idiom:
        ``pack_id`` matches the scope list (and is a real BSON string, not
        a same-spelled number/bool -- ``$type`` mirrors the SQL side's
        ``json_type='text'`` strictness) OR ``pack_id`` is absent/falsy AND
        ``source`` matches. Mongo's ``$in`` already treats a bound ``None``
        as matching both "missing" and "null" (unlike a bare equality
        check), so the falsy list needs no separate ``$exists`` clause.

        ARRAYS ARE EXCLUDED AT EVERY LEG (issue #222). ``$in`` and ``$type``
        test array ELEMENTS rather than the array, and a dotted path
        traverses an array of embedded documents, so all three legs plus the
        container used to match rows the SQL predicate rejects -- a
        ``pack_id`` of ``["p"]`` read as owned, a ``pack_id`` of ``[0]`` read
        as absent (opening the ``source`` fallback), and a ``metadata`` that
        is itself an array of documents read through. Since this predicate
        decides ``pack_fork``'s copy range, those were rows copied outside
        the fork's pack. The exclusion is built once in
        ``_scalar_string_in``/``_scalar_falsy``/``_non_array`` (module level)
        so the legs cannot drift apart again.

        Empty ``pack_ids`` -> ``[]`` WITHOUT querying. ``limit <= 0`` ->
        ``[]``, same guard ``list_nodes_scoped`` uses (issue #120 follow-up).

        FAIL-CLOSED ON UNAVAILABLE (design §5-1 step 3, issue #201): raises
        via ``_require_available()`` rather than returning ``[]`` -- see the
        SQL sibling's docstring for why a down store must not look like an
        empty pack to ``pack_fork``'s orphan-vector detection.

        Row shape includes ``text`` (design §15-3), matching the SQL
        sibling's `SELECT source_id, text, metadata, ingested_at`: this
        method's only callers are `pack_fork`'s scoped reads, whose step-16
        copy loop needs each row's `text` as the fork copy payload.
        """
        self._require_available()
        if not pack_ids or limit <= 0:
            return []

        ids = list(pack_ids)
        query: dict[str, Any] = {
            **_non_array("metadata"),
            "$or": [
                {"metadata.pack_id": _scalar_string_in(ids)},
                {
                    "$and": [
                        {"metadata.pack_id": _scalar_falsy()},
                        {"metadata.source": _scalar_string_in(ids)},
                    ]
                },
            ],
        }
        cursor = self._db["sources"].find(query, {"_id": 0}).limit(limit)
        return [dict(doc) for doc in cursor]

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        subject_id: str | None,
        details: dict[str, Any],
    ) -> str:
        """Append an audit log entry. Returns the inserted event id as a string.

        Raises RuntimeError when MongoDB is unavailable, matching the other
        doc store backends (LocalSQLDocStore, PgDocStore), which also return
        an event_id str so callers can correlate audit entries.
        """
        self._require_available()

        result = self._db["audit_log"].insert_one(
            {
                "event_type": event_type,
                "subject_id": subject_id,
                "details": details,
                "timestamp": datetime.now(tz=UTC),
            }
        )
        return str(result.inserted_id)

    def get_audit_log(
        self, limit: int = 100, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve recent audit log entries.

        ``limit <= 0``: returns ``[]`` without querying -- same contract as
        ``list_nodes`` above."""
        self._require_available()
        if limit <= 0:
            return []

        query: dict[str, Any] = {}
        if event_type:
            query["event_type"] = event_type
        cursor = (
            self._db["audit_log"]
            .find(query, {"_id": 0})
            .sort("timestamp", -1)
            .limit(limit)
        )
        return [dict(doc) for doc in cursor]

    # ------------------------------------------------------------------
    # Collection stats
    # ------------------------------------------------------------------

    def collection_stats(self) -> dict[str, int]:
        """Return document counts for all collections."""
        if not self._available:
            return {}

        return {
            "nodes": self._db["nodes"].count_documents({}),
            "sources": self._db["sources"].count_documents({}),
            "audit_log": self._db["audit_log"].count_documents({}),
        }
