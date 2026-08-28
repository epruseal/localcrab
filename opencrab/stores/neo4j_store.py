"""
Neo4j graph store adapter.

Wraps the official neo4j-python-driver and exposes typed methods for
creating nodes and edges, running Cypher queries, and traversing paths.
All methods gracefully handle connection failures.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from opencrab.common.graph_identity import (
    EdgeIdentityConflict,
    EdgeWriteReceipt,
    GraphQueryWriteRejected,
    GraphSchemaMigrationRequired,
    GraphWriteCapabilityUnavailable,
    NodeIdentityConflict,
    NodeWriteReceipt,
    ProvenanceBatchReceipt,
    ProvenanceWriteReceipt,
    canonical_edge_digest,
    canonical_json_bytes,
    canonical_node_digest,
    normalize_edge_properties,
    parse_properties_object,
    prepare_node,
    validate_digest,
)

logger = logging.getLogger(__name__)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Neo4jStore:
    """Thread-safe Neo4j adapter using the official driver."""

    def __init__(self, uri: str, user: str, password: str, database: str | None = None) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver: Any = None
        self._available = False
        self._schema_state = "unconfigured"
        self._writer_mutex = threading.RLock()
        self._tx_context = threading.local()
        self._connect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            from neo4j import GraphDatabase  # type: ignore[import]

            from opencrab.common.neo4j_driver import make_driver

            self._driver = make_driver(
                GraphDatabase, self._uri, self._user, self._password
            )
            # Verify connectivity with a lightweight query
            session_kwargs = {"database": self._database} if self._database else {}
            with self._driver.session(**session_kwargs) as session:
                session.run("RETURN 1")
            self._available = True
            self._initialise_schema_state()
            if self._database:
                logger.info("Neo4j connected at %s (database=%s)", self._uri, self._database)
            else:
                logger.info("Neo4j connected at %s", self._uri)
        except Exception as exc:
            logger.warning("Neo4j unavailable (%s): %s", self._uri, exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def close(self) -> None:
        if self._driver:
            try:
                self._driver.close()
            except Exception:
                pass

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("Neo4j is not available.")

    @property
    def schema_state(self) -> str:
        return self._schema_state

    @property
    def write_available(self) -> bool:
        return self._available and self._schema_state == "target"

    def _require_schema_ready(self) -> None:
        self._require_available()
        if self._schema_state != "target":
            from opencrab.common.graph_identity import GraphSchemaMigrationRequired

            raise GraphSchemaMigrationRequired("graph schema migration required")

    @staticmethod
    def _record_value(record: Any, key: str, default: Any = None) -> Any:
        if record is None:
            return default
        try:
            value = record[key]
        except (KeyError, IndexError, TypeError):
            try:
                value = getattr(record, key)
            except AttributeError:
                return default
        return default if value is None else value

    def _domain_labels(self) -> set[str]:
        try:
            from opencrab.grammar.manifest import all_node_types

            return {str(item) for item in all_node_types()}
        except Exception:
            return set()

    def _schema_rows(self, session, statement: str, **params: Any) -> list[Any]:
        return list(session.run(statement, **params))

    def _initialise_schema_state(self) -> None:
        """Classify graph-owned data before allowing any mutation.

        Neo4j schema DDL is deliberately owned here.  CRUD never guesses that
        a database with old domain labels is safe to write.
        """
        try:
            with self._session() as session:
                constraints = self._schema_rows(session, "SHOW CONSTRAINTS")
                indexes = self._schema_rows(session, "SHOW INDEXES")
                owned = self._schema_rows(
                    session,
                    "MATCH (n:OpenCrabNode) RETURN n.node_id AS node_id, "
                    "n.node_type AS node_type, n.node_digest AS node_digest, "
                    "properties(n) AS props, labels(n) AS labels",
                )
                edges = self._schema_rows(
                    session,
                    "MATCH (a)-[r]->(b) "
                    "WHERE a:OpenCrabNode OR b:OpenCrabNode "
                    "RETURN a.node_id AS from_id, a.node_type AS from_type, "
                    "labels(a) AS from_labels, b.node_id AS to_id, "
                    "b.node_type AS to_type, labels(b) AS to_labels, "
                    "type(r) AS rel_type, r.relation AS relation, "
                    "r.from_id AS stored_from_id, r.to_id AS stored_to_id, "
                    "r.edge_key AS edge_key, r.edge_digest AS edge_digest, "
                    "properties(r) AS props",
                )
                domain = self._schema_rows(
                    session,
                    "MATCH (n) WHERE any(label IN labels(n) WHERE label IN $labels) "
                    "RETURN elementId(n) AS source, properties(n) AS props, labels(n) AS labels",
                    labels=sorted(self._domain_labels()),
                )
                write_locks = self._schema_rows(
                    session,
                    "MATCH (l:OpenCrabWriteLock {name:'graph'}) RETURN count(l) AS count",
                )
                migration_locks = self._schema_rows(
                    session,
                    "MATCH (l:OpenCrabMigrationLock {name:'graph'}) RETURN count(l) AS count",
                )
            names: set[str] = set()
            for row in constraints + indexes:
                name = self._record_value(row, "name")
                if name:
                    names.add(str(name))
            required = {
                "opencrab_node_id_unique",
                "opencrab_write_lock_name_unique",
                "opencrab_migration_lock_name_unique",
            }
            if not owned and not domain:
                self._create_target_schema()
                self._schema_state = "target"
                return
            if not required.issubset(names):
                self._schema_state = "legacy_migration_required" if domain or owned else "partial_or_unknown"
                return
            if (
                not write_locks
                or int(self._record_value(write_locks[0], "count", 0) or 0) != 1
                or not migration_locks
                or int(self._record_value(migration_locks[0], "count", 0) or 0) != 1
            ):
                self._schema_state = "partial_or_unknown"
                return
            for row in owned:
                props = self._record_value(row, "props", {})
                node_id = self._record_value(row, "node_id")
                node_type = self._record_value(row, "node_type")
                digest = self._record_value(row, "node_digest")
                labels = [str(label) for label in (self._record_value(row, "labels", []) or [])]
                domain_labels = [label for label in labels if label != "OpenCrabNode"]
                original_props = props if isinstance(props, dict) else {}
                if (
                    not isinstance(node_id, str)
                    or not isinstance(node_type, str)
                    or not validate_digest(digest or "")
                    or len(domain_labels) != 1
                    or not isinstance(props, dict)
                    or original_props.get("id") != node_id
                    or original_props.get("node_id") != node_id
                    or original_props.get("node_type") != node_type
                    or original_props.get("node_digest") != digest
                    or domain_labels[0] != node_type
                ):
                    self._schema_state = "partial_or_unknown"
                    return
                cleaned = self._clean_node_properties(props, node_id, node_type)
                if canonical_node_digest(node_type, cleaned.get("space"), cleaned) != digest:
                    self._schema_state = "partial_or_unknown"
                    return
            edge_keys: set[tuple[str, str, str]] = set()
            edge_digests: set[str] = set()
            for row in edges:
                from_id = self._record_value(row, "from_id")
                to_id = self._record_value(row, "to_id")
                from_type = self._record_value(row, "from_type")
                to_type = self._record_value(row, "to_type")
                relation = self._record_value(row, "relation")
                rel_type = self._record_value(row, "rel_type")
                from_labels = [str(label) for label in (self._record_value(row, "from_labels", []) or [])]
                to_labels = [str(label) for label in (self._record_value(row, "to_labels", []) or [])]
                raw_props = self._record_value(row, "props", {})
                stored_from_id = self._record_value(row, "stored_from_id")
                stored_to_id = self._record_value(row, "stored_to_id")
                edge_key = self._record_value(row, "edge_key")
                edge_digest = self._record_value(row, "edge_digest")
                if (
                    not isinstance(from_id, str)
                    or not isinstance(to_id, str)
                    or not isinstance(from_type, str)
                    or not isinstance(to_type, str)
                    or not isinstance(relation, str)
                    or rel_type != relation
                    or stored_from_id != from_id
                    or stored_to_id != to_id
                    or from_type not in from_labels
                    or to_type not in to_labels
                    or "OpenCrabNode" not in from_labels
                    or "OpenCrabNode" not in to_labels
                    or not isinstance(raw_props, dict)
                ):
                    self._schema_state = "partial_or_unknown"
                    return
                try:
                    relation = self._label(relation)
                    expected_edge_key = self._edge_key(from_id, relation, to_id)
                    cleaned = normalize_edge_properties(
                        from_id, relation, to_id, self._clean_edge_properties(raw_props)
                    )
                    expected_edge_digest = canonical_edge_digest(
                        from_id, relation, to_id, from_type, to_type, cleaned
                    )
                except (TypeError, ValueError):
                    self._schema_state = "partial_or_unknown"
                    return
                if (
                    edge_key != expected_edge_key
                    or not validate_digest(edge_digest or "", edge=True)
                    or edge_digest != expected_edge_digest
                    or raw_props.get("from_id") != from_id
                    or raw_props.get("relation") != relation
                    or raw_props.get("to_id") != to_id
                    or raw_props.get("edge_key") != expected_edge_key
                    or raw_props.get("edge_digest") != expected_edge_digest
                    or raw_props.get("from_type") != from_type
                    or raw_props.get("to_type") != to_type
                ):
                    self._schema_state = "partial_or_unknown"
                    return
                identity = (from_id, relation, to_id)
                if identity in edge_keys or edge_key in edge_digests:
                    self._schema_state = "partial_or_unknown"
                    return
                edge_keys.add(identity)
                edge_digests.add(edge_key)
            self._schema_state = "target"
        except Exception:
            self._schema_state = "partial_or_unknown"

    def _create_target_schema(self) -> None:
        with self._session() as session:
            statements = (
                "CREATE CONSTRAINT opencrab_node_id_unique IF NOT EXISTS "
                "FOR (n:OpenCrabNode) REQUIRE n.node_id IS UNIQUE",
                "CREATE CONSTRAINT opencrab_write_lock_name_unique IF NOT EXISTS "
                "FOR (l:OpenCrabWriteLock) REQUIRE l.name IS UNIQUE",
                "CREATE CONSTRAINT opencrab_migration_lock_name_unique IF NOT EXISTS "
                "FOR (l:OpenCrabMigrationLock) REQUIRE l.name IS UNIQUE",
                "MERGE (l:OpenCrabWriteLock {name:'graph'}) "
                "ON CREATE SET l.lock_epoch=0, l.owner_token=null",
                "MERGE (l:OpenCrabMigrationLock {name:'graph'}) "
                "ON CREATE SET l.owner_token=null",
            )
            for statement in statements:
                session.run(statement).consume()

    def _run_write(self, callback):
        """Run a mutation under the database-global Neo4j writer lock."""
        self._require_schema_ready()
        active = getattr(self._tx_context, "active", None)
        if active is not None:
            return callback(active)
        with self._writer_mutex:
            with self._session() as session:
                execute_write = getattr(session, "execute_write", None)
                if not callable(execute_write):
                    raise GraphWriteCapabilityUnavailable("graph write capability unavailable")
                token = uuid.uuid4().hex

                def work(tx: Any):
                    claim = tx.run(
                        "MATCH (l:OpenCrabWriteLock {name:$name}) "
                        "SET l.lock_epoch=coalesce(l.lock_epoch,0)+1, l.owner_token=$token "
                        "RETURN l.lock_epoch AS epoch",
                        name="graph", token=token,
                    ).single()
                    if claim is None:
                        raise GraphWriteCapabilityUnavailable("graph write capability unavailable")
                    self._tx_context.active = tx
                    try:
                        result = callback(tx)
                    finally:
                        self._tx_context.active = None
                    tx.run(
                        "MATCH (l:OpenCrabWriteLock {name:$name, owner_token:$token}) "
                        "SET l.owner_token=null",
                        name="graph", token=token,
                    ).consume()
                    return result

                return execute_write(work)

    @contextmanager
    def _session(self) -> Generator[Any, None, None]:
        self._require_available()
        if self._driver is None:
            raise RuntimeError("Neo4j is not available.")
        session_kwargs = {"database": self._database} if self._database else {}
        with self._driver.session(**session_kwargs) as session:
            yield session

    # ------------------------------------------------------------------
    # Schema / constraints
    # ------------------------------------------------------------------

    def ensure_constraints(self) -> None:
        if not self._available:
            logger.warning("Neo4j unavailable; skipping constraint creation.")
            return
        if self._schema_state == "fresh":
            self._create_target_schema()
            self._initialise_schema_state()
        if self._schema_state != "target":
            raise GraphSchemaMigrationRequired("graph schema migration required")
        logger.info("Neo4j constraints ensured.")

    # ------------------------------------------------------------------
    # Node operations
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_node_properties(raw: Any, node_id: str, node_type: str | None = None) -> dict[str, Any]:
        original = parse_properties_object({} if raw is None else raw)
        if "id" in original and original["id"] != node_id:
            raise ValueError("reserved graph property")
        if "node_id" in original and original["node_id"] != node_id:
            raise ValueError("reserved graph property")
        if node_type is not None and "node_type" in original and original["node_type"] != node_type:
            raise ValueError("reserved graph property")
        props = original
        props.pop("node_id", None)
        props.pop("node_digest", None)
        props.pop("node_type", None)
        props.setdefault("id", node_id)
        return props

    @staticmethod
    def _clean_edge_properties(raw: Any) -> dict[str, Any]:
        props = parse_properties_object({} if raw is None else raw)
        for key in ("edge_key", "edge_digest", "from_type", "to_type"):
            props.pop(key, None)
        return props

    @staticmethod
    def _provenance_schema_fingerprint() -> str:
        """Return the backend-neutral schema fingerprint used by SQL stores.

        Neo4j has no SQL catalog, but provenance plans are deliberately
        portable.  Keeping this representation identical to
        ``_SqlGraphStoreBase._provenance_schema_fingerprint`` prevents a plan
        made against a target graph from being accepted merely because the
        backend supplied a different fallback label.
        """
        from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA

        schema = {
            "tables": [
                {
                    "name": table.name,
                    "columns": [
                        [column.name, column.kind, column.not_null, column.default]
                        for column in table.columns
                    ],
                    "primary_key": list(table.primary_key),
                }
                for table in GRAPH_STORE_SCHEMA.tables
            ],
            "indexes": [
                [
                    index.name,
                    index.table,
                    index.expr,
                    list(index.json_key) if index.json_key else None,
                ]
                for index in GRAPH_STORE_SCHEMA.indexes
            ],
        }
        return hashlib.sha256(
            b"opencrab.issue80.graph-schema.v1\0"
            + canonical_json_bytes(schema)
        ).hexdigest()

    @staticmethod
    def _label(node_type: str) -> str:
        if not isinstance(node_type, str) or not _IDENT_RE.fullmatch(node_type):
            raise ValueError("graph identity fields must be non-empty strings")
        return node_type

    @staticmethod
    def _edge_key(from_id: str, relation: str, to_id: str) -> str:
        return hashlib.sha256(canonical_json_bytes([from_id, relation, to_id])).hexdigest()

    @staticmethod
    def _query_has_write(cypher: str) -> bool:
        """Inspect executable Cypher tokens, ignoring quoted data/comments."""
        if not isinstance(cypher, str):
            return True
        code: list[str] = []
        i = 0
        while i < len(cypher):
            if cypher.startswith("//", i):
                end = cypher.find("\n", i + 2)
                i = len(cypher) if end < 0 else end
                continue
            if cypher.startswith("/*", i):
                end = cypher.find("*/", i + 2)
                if end < 0:
                    return True
                i = end + 2
                continue
            char = cypher[i]
            if char in ("'", '"', "`"):
                quote = char
                i += 1
                closed = False
                while i < len(cypher):
                    if cypher[i] == quote:
                        # Cypher escapes a quote by doubling it. Backslash is
                        # accepted too because older drivers permit it in
                        # parameter-like literals.
                        if i + 1 < len(cypher) and cypher[i + 1] == quote:
                            i += 2
                            continue
                        i += 1
                        closed = True
                        break
                    if cypher[i] == "\\" and i + 1 < len(cypher):
                        i += 2
                    else:
                        i += 1
                if not closed:
                    return True
                code.append(" ")
                continue
            code.append(char)
            i += 1
        executable = "".join(code)
        if ";" in executable:
            return True
        return bool(re.search(
            r"\b(?:CREATE|MERGE|SET|REMOVE|DELETE|DETACH|DROP|ALTER|COPY|LOAD|FOREACH|CALL|BEGIN|COMMIT|ROLLBACK|USE)\b",
            executable,
            flags=re.IGNORECASE,
        ))

    @classmethod
    def _validate_read_cypher(cls, cypher: str) -> None:
        """Reject anything outside the deliberately small read/query surface."""
        if not isinstance(cypher, str) or not cypher.strip():
            raise GraphQueryWriteRejected("graph query write rejected")
        code: list[str] = []
        i = 0
        while i < len(cypher):
            if cypher.startswith("//", i):
                end = cypher.find("\n", i + 2)
                i = len(cypher) if end < 0 else end
                continue
            if cypher.startswith("/*", i):
                end = cypher.find("*/", i + 2)
                if end < 0:
                    raise GraphQueryWriteRejected("graph query write rejected")
                i = end + 2
                continue
            char = cypher[i]
            if char in ("'", '"', "`"):
                quote = char
                code.append(" ")
                i += 1
                closed = False
                while i < len(cypher):
                    if cypher[i] == quote:
                        if i + 1 < len(cypher) and cypher[i + 1] == quote:
                            i += 2
                            continue
                        i += 1
                        closed = True
                        break
                    if cypher[i] == "\\" and i + 1 < len(cypher):
                        i += 2
                    else:
                        i += 1
                if not closed:
                    raise GraphQueryWriteRejected("graph query write rejected")
                continue
            code.append(char)
            i += 1
        executable = "".join(code).strip()
        if executable.endswith(";"):
            executable = executable[:-1].rstrip()
        if ";" in executable or cls._query_has_write(executable):
            raise GraphQueryWriteRejected("graph query write rejected")
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", executable.upper())
        if not tokens:
            raise GraphQueryWriteRejected("graph query write rejected")
        if tokens[0] not in {"MATCH", "OPTIONAL", "UNWIND", "WITH", "RETURN", "SHOW", "EXPLAIN", "PROFILE"}:
            raise GraphQueryWriteRejected("graph query write rejected")
        if tokens[0] in {"SHOW", "EXPLAIN", "PROFILE"}:
            if tokens[0] == "SHOW" and len(tokens) > 1 and tokens[1] not in {"CONSTRAINTS", "INDEXES"}:
                raise GraphQueryWriteRejected("graph query write rejected")
        elif "RETURN" not in tokens:
            raise GraphQueryWriteRejected("graph query write rejected")

    def upsert_node(
        self,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
        space_id: str | None = None,
        *,
        return_receipt: bool = False,
    ) -> dict[str, Any] | NodeWriteReceipt:
        """
        Create or update a node. Returns the node's properties.

        Parameters
        ----------
        node_type:
            Label to apply (e.g. "User", "Document").
        node_id:
            Unique identifier for the node.
        properties:
            Additional properties to merge onto the node.
        space_id:
            Optional space tag stored as a property.
        """
        self._require_schema_ready()
        node_type = self._label(node_type)
        node_type, props, _space_id, digest = prepare_node(node_type, node_id, properties, space_id)

        def write(tx: Any):
            cypher = """
            MATCH (n:OpenCrabNode {node_id: $node_id})
            RETURN properties(n) AS props, n.node_type AS node_type,
                   n.node_digest AS node_digest
            """
            result = tx.run(cypher, node_id=node_id)
            record = result.single()
            if record:
                stored = self._clean_node_properties(record["props"], node_id, record["node_type"])
                # The digest column is an observation/cache, not the source
                # of truth.  Recompute it so stale importer metadata cannot
                # make a conflicting payload appear idempotent.
                stored_digest = canonical_node_digest(record["node_type"], stored.get("space"), stored)
                if record["node_type"] != node_type or stored_digest != digest:
                    raise NodeIdentityConflict(f"node identity conflict: {node_id}")
                if return_receipt:
                    return NodeWriteReceipt("idempotent", node_id, record["node_type"], stored.get("space"), stored, stored_digest)
                return stored
            result = tx.run(
                f"""
                MERGE (n:OpenCrabNode {{node_id: $node_id}})
                ON CREATE SET n:`{node_type}`, n.node_type=$node_type,
                    n.node_id=$node_id, n.node_digest=$node_digest, n += $props
                RETURN properties(n) AS props, n.node_type AS node_type,
                       n.node_digest AS node_digest
                """,
                node_id=node_id, node_type=node_type, node_digest=digest, props=props,
            )
            created = result.single()
            if created is None:
                raise RuntimeError("graph node merge did not produce a row")
            stored_type = created["node_type"]
            stored = self._clean_node_properties(created["props"], node_id, stored_type)
            stored_digest = canonical_node_digest(stored_type, stored.get("space"), stored)
            if stored_type != node_type or stored_digest != digest:
                raise NodeIdentityConflict(f"node identity conflict: {node_id}")
            if return_receipt:
                operation = "created" if not record else "idempotent"
                return NodeWriteReceipt(operation, node_id, stored_type, stored.get("space"), stored, stored_digest)
            return stored
        return self._run_write(write)

    def get_node(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        """Retrieve a node by type and id."""
        self._require_available()

        node_type = self._label(node_type)
        cypher = "MATCH (n:OpenCrabNode {node_id: $id}) WHERE n.node_type=$node_type RETURN properties(n) AS props"
        with self._session() as session:
            result = session.run(cypher, id=node_id, node_type=node_type)
            record = result.single()
            return self._clean_node_properties(record["props"], node_id, node_type) if record else None

    def lookup_node_type(self, node_id: str) -> str | None:
        """Return the first label for a node_id, or None if not found.

        Used by OntologyBuilder to resolve real node types when writing edges,
        so that edges preserve typed labels instead of falling back to a single
        per-space default.
        """
        if not self._available:
            return None
        try:
            with self._session() as session:
                result = session.run(
                    "MATCH (n:OpenCrabNode {node_id: $id}) RETURN n.node_type AS lbl LIMIT 1",
                    id=node_id,
                )
                record = result.single()
                return record["lbl"] if record and record["lbl"] else None
        except Exception:
            return None

    def delete_node(self, node_type: str, node_id: str) -> bool:
        """Delete a node and all its relationships."""
        self._require_schema_ready()
        node_type = self._label(node_type)
        cypher = "MATCH (n:OpenCrabNode {node_id: $id}) WHERE n.node_type=$node_type DETACH DELETE n RETURN count(n) AS cnt"
        def write(tx: Any):
            result = tx.run(cypher, id=node_id, node_type=node_type)
            record = result.single()
            return bool(record and record["cnt"] > 0)
        return self._run_write(write)

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    def upsert_edge(
        self,
        from_type: str,
        from_id: str,
        relation: str,
        to_type: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
        *,
        return_receipt: bool = False,
    ) -> bool | EdgeWriteReceipt:
        """
        Create or update a directed relationship between two nodes.

        Returns True on success.
        """
        self._require_schema_ready()
        from_type = self._label(from_type)
        to_type = self._label(to_type)
        relation = self._label(relation)
        props = normalize_edge_properties(from_id, relation, to_id, properties)
        digest = canonical_edge_digest(from_id, relation, to_id, from_type, to_type, props)
        edge_key = self._edge_key(from_id, relation, to_id)
        lookup = """
            MATCH (a:OpenCrabNode {node_id: $from_id})-[r]->(b:OpenCrabNode {node_id: $to_id})
            WHERE (r.edge_key=$edge_key OR r.edge_key IS NULL)
              AND (r.relation=$relation OR r.relation IS NULL AND type(r)=$relation)
            RETURN properties(r) AS props, r.from_type AS from_type,
                   r.to_type AS to_type, r.edge_digest AS edge_digest
            LIMIT 1
        """
        def write(tx: Any):
            endpoint_result = tx.run(
                "MATCH (a:OpenCrabNode {node_id: $from_id}) "
                "MATCH (b:OpenCrabNode {node_id: $to_id}) "
                "RETURN a.node_type AS from_type, b.node_type AS to_type",
                from_id=from_id, to_id=to_id,
            ).single()
            if not endpoint_result:
                return False
            if endpoint_result["from_type"] != from_type or endpoint_result["to_type"] != to_type:
                return False
            record = tx.run(
                lookup, from_id=from_id, to_id=to_id, relation=relation, edge_key=edge_key
            ).single()
            if record:
                stored = normalize_edge_properties(from_id, relation, to_id, self._clean_edge_properties(record["props"]))
                stored_digest = canonical_edge_digest(from_id, relation, to_id, record["from_type"], record["to_type"], stored)
                if record["from_type"] != from_type or record["to_type"] != to_type or stored_digest != digest:
                    raise EdgeIdentityConflict(f"edge identity conflict: ({from_id}, {relation}, {to_id})")
                if return_receipt:
                    return EdgeWriteReceipt("idempotent", from_id, relation, to_id, from_type, to_type, stored, stored_digest)
                return True
            result = tx.run(
                f"""
                MATCH (a:OpenCrabNode {{node_id: $from_id}})
                MATCH (b:OpenCrabNode {{node_id: $to_id}})
                MERGE (a)-[r:`{relation}` {{edge_key: $edge_key}}]->(b)
                ON CREATE SET r.from_id=$from_id, r.relation=$relation, r.to_id=$to_id,
                    r.from_type=$from_type, r.to_type=$to_type,
                    r.edge_digest=$edge_digest, r += $props
                RETURN properties(r) AS props
                """,
                from_id=from_id, relation=relation, to_id=to_id,
                from_type=from_type, to_type=to_type, edge_key=edge_key,
                edge_digest=digest, props=props,
            )
            if result.single() is None:
                return False
            if return_receipt:
                return EdgeWriteReceipt("created", from_id, relation, to_id, from_type, to_type, props, digest)
            return True
        return self._run_write(write)

    def get_edge(
        self,
        from_type: str,
        from_id: str,
        relation: str,
        to_type: str,
        to_id: str,
    ) -> dict[str, Any] | None:
        """Same label/relation-type MATCH shape as ``upsert_edge`` -- see
        ``GraphStore.get_edge``'s docstring for the cross-backend contract."""
        self._require_available()

        from_type = self._label(from_type)
        to_type = self._label(to_type)
        relation = self._label(relation)
        cypher = f"""
            MATCH (a:OpenCrabNode {{node_id: $from_id}})-[r:`{relation}`]->(b:OpenCrabNode {{node_id: $to_id}})
            WHERE a.node_type=$from_type AND b.node_type=$to_type
            RETURN properties(r) AS props
        """
        with self._session() as session:
            result = session.run(cypher, from_id=from_id, to_id=to_id, from_type=from_type, to_type=to_type)
            record = result.single()
            return normalize_edge_properties(from_id, relation, to_id, self._clean_edge_properties(record["props"])) if record else None

    def get_node_digest(self, node_id: str, *, node_type: str | None = None) -> str | None:
        self._require_available()
        clause = " WHERE n.node_type=$node_type" if node_type is not None else ""
        params: dict[str, Any] = {"node_id": node_id}
        if node_type is not None:
            params["node_type"] = self._label(node_type)
        with self._session() as session:
            record = session.run(
                "MATCH (n:OpenCrabNode {node_id: $node_id})" + clause +
                " RETURN properties(n) AS props, n.node_type AS node_type, n.node_digest AS digest LIMIT 1",
                **params,
            ).single()
            if not record:
                return None
            props = self._clean_node_properties(record["props"], node_id, record["node_type"])
            return canonical_node_digest(record["node_type"], props.get("space"), props)

    def get_edge_digest(self, from_id: str, relation: str, to_id: str, *, from_type: str | None = None, to_type: str | None = None) -> str | None:
        self._require_available()
        relation = self._label(relation)
        with self._session() as session:
            record = session.run(
                f"MATCH (a:OpenCrabNode {{node_id:$from_id}})-[r:`{relation}`]->(b:OpenCrabNode {{node_id:$to_id}}) "
                "RETURN properties(r) AS props, r.from_type AS from_type, r.to_type AS to_type, r.edge_digest AS digest LIMIT 1",
                from_id=from_id, to_id=to_id,
            ).single()
            if not record or (from_type is not None and record["from_type"] != from_type) or (to_type is not None and record["to_type"] != to_type):
                return None
            props = normalize_edge_properties(from_id, relation, to_id, self._clean_edge_properties(record["props"]))
            return canonical_edge_digest(from_id, relation, to_id, record["from_type"], record["to_type"], props)

    def update_node(self, node_id: str, expected_current_digest: str, new_type: str, new_properties: dict[str, Any], new_space_id: str | None = None, *, return_receipt: bool = False) -> dict[str, Any] | NodeWriteReceipt:
        self._require_schema_ready()
        validate_digest(expected_current_digest)
        new_type = self._label(new_type)
        new_type, props, space_id, digest = prepare_node(new_type, node_id, new_properties, new_space_id)
        def write(tx: Any):
            record = tx.run(
                "MATCH (n:OpenCrabNode {node_id:$node_id}) RETURN properties(n) AS props, n.node_type AS node_type, n.node_digest AS digest LIMIT 1",
                node_id=node_id,
            ).single()
            if not record:
                raise NodeIdentityConflict(f"stale node update: {node_id}")
            current = self._clean_node_properties(record["props"], node_id, record["node_type"])
            current_digest = canonical_node_digest(record["node_type"], current.get("space"), current)
            if current_digest != expected_current_digest:
                raise NodeIdentityConflict(f"stale node update: {node_id}")
            old_type = self._label(record["node_type"])
            updated = tx.run(
                f"MATCH (n:OpenCrabNode {{node_id:$node_id}}) "
                "WHERE n.node_digest=$expected_digest "
                "SET n=$props, n.node_id=$node_id, n.id=$node_id, n.node_type=$node_type, "
                "n.node_digest=$node_digest "
                f"REMOVE n:`{old_type}` SET n:`{new_type}` RETURN n",
                node_id=node_id, expected_digest=expected_current_digest,
                props=props, node_type=new_type, node_digest=digest,
            ).single()
            if updated is None:
                raise NodeIdentityConflict(f"stale node update: {node_id}")
            tx.run("MATCH (:OpenCrabNode)-[r]->(:OpenCrabNode) WHERE r.from_id=$node_id SET r.from_type=$node_type", node_id=node_id, node_type=new_type).consume()
            tx.run("MATCH (:OpenCrabNode)-[r]->(:OpenCrabNode) WHERE r.to_id=$node_id SET r.to_type=$node_type", node_id=node_id, node_type=new_type).consume()
            # Edge digests include endpoint types.  Refresh every incident
            # snapshot after a node type CAS so a later edge CAS and the graph
            # fingerprint continue to describe the same canonical identity.
            incident = tx.run(
                "MATCH (a:OpenCrabNode)-[r]->(b:OpenCrabNode) "
                "WHERE r.from_id=$node_id OR r.to_id=$node_id "
                "RETURN r.from_id AS from_id, r.relation AS relation, "
                "r.to_id AS to_id, type(r) AS rel_type, r.from_type AS from_type, r.to_type AS to_type, "
                "properties(r) AS props",
                node_id=node_id,
            )
            for edge in incident:
                key = (edge["from_id"], edge["relation"] or edge["rel_type"], edge["to_id"])
                edge_props = normalize_edge_properties(*key, self._clean_edge_properties(edge["props"]))
                edge_from_type = new_type if key[0] == node_id else edge["from_type"]
                edge_to_type = new_type if key[2] == node_id else edge["to_type"]
                edge_digest = canonical_edge_digest(*key, edge_from_type, edge_to_type, edge_props)
                tx.run(
                    "MATCH (a:OpenCrabNode {node_id:$from_id})-"
                    "[r]->(b:OpenCrabNode {node_id:$to_id}) "
                    "WHERE r.relation=$relation OR (r.relation IS NULL AND type(r)=$relation) "
                    "SET r.edge_digest=$edge_digest, r.from_type=$from_type, r.to_type=$to_type",
                    from_id=key[0], relation=key[1], to_id=key[2],
                    from_type=edge_from_type, to_type=edge_to_type,
                    edge_digest=edge_digest,
                ).consume()
            if return_receipt:
                return NodeWriteReceipt("updated", node_id, new_type, space_id, props, digest)
            return props
        return self._run_write(write)

    def delete_edge(self, from_id: str, relation: str, to_id: str, *, owner_pack_id: str) -> bool:
        self._require_schema_ready()
        relation = self._label(relation)
        if not isinstance(owner_pack_id, str) or not owner_pack_id:
            raise ValueError("graph identity fields must be non-empty strings")
        def write(tx: Any):
            record = tx.run(
                f"MATCH (a:OpenCrabNode {{node_id:$from_id}})-[r:`{relation}`]->(b:OpenCrabNode {{node_id:$to_id}}) RETURN r.pack_id AS owner LIMIT 1",
                from_id=from_id, to_id=to_id,
            ).single()
            if not record or record["owner"] != owner_pack_id:
                return False
            tx.run(
                f"MATCH (a:OpenCrabNode {{node_id:$from_id}})-[r:`{relation}`]->(b:OpenCrabNode {{node_id:$to_id}}) DELETE r",
                from_id=from_id, to_id=to_id,
            ).consume()
            return True
        return self._run_write(write)

    def update_edge(self, from_type: str, from_id: str, relation: str, to_type: str, to_id: str, properties: dict[str, Any] | None = None, *, expected_current_digest: str, owner_pack_id: str, return_receipt: bool = False) -> bool | EdgeWriteReceipt:
        self._require_schema_ready()
        validate_digest(expected_current_digest, edge=True)
        from_type, to_type, relation = self._label(from_type), self._label(to_type), self._label(relation)
        if not isinstance(owner_pack_id, str) or not owner_pack_id:
            raise ValueError("graph identity fields must be non-empty strings")
        props = normalize_edge_properties(from_id, relation, to_id, properties)
        if props.get("pack_id") != owner_pack_id:
            raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
        digest = canonical_edge_digest(from_id, relation, to_id, from_type, to_type, props)
        def write(tx: Any):
            record = tx.run(
                f"MATCH (a:OpenCrabNode {{node_id:$from_id}})-[r:`{relation}`]->(b:OpenCrabNode {{node_id:$to_id}}) RETURN properties(r) AS props, r.from_type AS from_type, r.to_type AS to_type, r.edge_digest AS digest LIMIT 1",
                from_id=from_id, to_id=to_id,
            ).single()
            if not record or record["from_type"] != from_type or record["to_type"] != to_type:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            current = normalize_edge_properties(from_id, relation, to_id, self._clean_edge_properties(record["props"]))
            current_digest = canonical_edge_digest(from_id, relation, to_id, record["from_type"], record["to_type"], current)
            if current.get("pack_id") != owner_pack_id or current_digest != expected_current_digest:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            updated = tx.run(
                f"MATCH (a:OpenCrabNode {{node_id:$from_id}})-[r:`{relation}`]->(b:OpenCrabNode {{node_id:$to_id}}) "
                "WHERE r.edge_digest=$expected_digest AND r.pack_id=$owner_pack_id "
                "SET r=$props, r.edge_key=$edge_key, r.from_id=$from_id, r.relation=$relation, r.to_id=$to_id, "
                "r.from_type=$from_type, r.to_type=$to_type, r.edge_digest=$edge_digest RETURN r",
                from_id=from_id, relation=relation, to_id=to_id,
                from_type=from_type, to_type=to_type, edge_digest=digest,
                edge_key=self._edge_key(from_id, relation, to_id),
                expected_digest=expected_current_digest, owner_pack_id=owner_pack_id,
                props=props,
            ).single()
            if updated is None:
                raise EdgeIdentityConflict(f"stale edge update: ({from_id}, {relation}, {to_id})")
            if return_receipt:
                return EdgeWriteReceipt("updated", from_id, relation, to_id, from_type, to_type, props, digest)
            return True
        return self._run_write(write)

    def hydrate_evidence(self, rows: list[dict[str, Any]]) -> int:
        """Merge staged evidence through the node CAS writer."""
        self._require_schema_ready()
        prepared: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            node_id = row.get("node_id")
            if isinstance(node_id, str) and node_id:
                prepared.append((node_id, dict(row.get("properties") or {})))

        def write(tx: Any):
            count = 0
            for node_id, additions in prepared:
                current_row = tx.run(
                    "MATCH (n:OpenCrabNode {node_id:$node_id}) "
                    "RETURN properties(n) AS props, n.node_type AS node_type LIMIT 1",
                    node_id=node_id,
                ).single()
                if current_row is None or current_row["node_type"] != "Evidence":
                    continue
                current = self._clean_node_properties(current_row["props"], node_id, "Evidence")
                merged = dict(current)
                merged.update(additions)
                current_digest = canonical_node_digest("Evidence", current.get("space"), current)
                self.update_node(node_id, current_digest, "Evidence", merged, current.get("space"))
                count += 1
            return count
        return self._run_write(write)

    def validate_import(self, pack_id: str) -> dict[str, Any]:
        """Run the importer validation queries through the read-only surface."""
        self._require_available()
        queries = {
            "nodes_by_label": "MATCH (n:OpenCrabNode) WHERE n.pack_id=$pack_id OR n.source_id=$pack_id RETURN n.node_type AS label, count(n) AS count ORDER BY label",
            "edges_by_type": "MATCH ()-[r]->() WHERE r.pack_id=$pack_id OR r.source_id=$pack_id RETURN type(r) AS type, count(r) AS count ORDER BY type",
            "missing_node_evidence_refs": "MATCH (n:OpenCrabNode) WHERE (n.pack_id=$pack_id OR n.source_id=$pack_id) AND n.node_type IN ['Persona','Evidence'] AND (n.evidence_refs IS NULL OR size(n.evidence_refs)=0) RETURN count(n) AS count",
            "missing_edge_evidence_refs": "MATCH ()-[r]->() WHERE (r.pack_id=$pack_id OR r.source_id=$pack_id) AND (r.evidence_refs IS NULL OR size(r.evidence_refs)=0) RETURN count(r) AS count",
            "unhydrated_evidence_nodes": "MATCH (e:OpenCrabNode) WHERE (e.pack_id=$pack_id OR e.source_id=$pack_id) AND e.node_type='Evidence' AND (e.text IS NULL OR e.hash IS NULL OR e.source_path IS NULL) RETURN count(e) AS count",
        }
        result: dict[str, Any] = {}
        for name, query in queries.items():
            rows = self.run_cypher(query, {"pack_id": pack_id})
            if name.endswith("_by_label") or name.endswith("_by_type"):
                result[name] = rows
            else:
                result[name] = int((rows[0] if rows else {}).get("count", 0))
        result["sample"] = self.run_cypher(
            "MATCH (d:OpenCrabNode)-[c:CONTAINS]->(p:OpenCrabNode)<-[s:SUPPORTS]-(e:OpenCrabNode) "
            "WHERE d.id=$dataset_id AND d.node_type='Document' AND p.node_type='Persona' AND e.node_type='Evidence' "
            "RETURN p.id AS persona_id, p.evidence_refs AS persona_evidence_refs, e.id AS evidence_node_id, "
            "c.evidence_refs AS contains_evidence_refs, s.evidence_refs AS supports_evidence_refs LIMIT 1",
            {"dataset_id": f"dataset:{pack_id}"},
        )
        return result

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def run_cypher(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute arbitrary Cypher and return a list of record dicts."""
        self._require_available()
        self._validate_read_cypher(cypher)

        with self._session() as session:
            result = session.run(cypher, **(params or {}))
            return [dict(record) for record in result]

    def find_neighbors(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 1,
        limit: int = 50,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
        spaces: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Find neighboring nodes up to *depth* hops.

        Parameters
        ----------
        direction:
            "out", "in", or "both".
        pack_ids:
            Optional pack_id allow-list. When set, neighbours whose
            ``n.pack_id`` is not in the list (and not NULL, unless
            ``include_unpackaged`` is True) are filtered out. Relationships
            whose own ``r.pack_id`` is set to a foreign value are also
            excluded; relationships with no ``r.pack_id`` are accepted when
            both endpoints survive the node filter.
        include_unpackaged:
            When ``pack_ids`` is set, also allow nodes/edges with no
            ``pack_id`` property (legacy data).
        spaces:
            Optional space allow-list (issue #52). When set, every node
            along the path (anchor included) must have ``n.space IN
            spaces`` — strict membership, no "include unspaced" escape
            hatch (matches the BM25/vector legs' semantics). ``upsert_node``
            writes ``space`` as a plain node property here, so this pushes
            straight into the WHERE clause, same as ``pack_ids``.
        """
        self._require_available()

        # issue #147 §3.4(a): `pack_ids=[]` must return `[]` WITHOUT
        # querying, not fall through to `_build_neighbors_cypher`'s
        # `if pack_ids:` (which treats `[]` the same as `None` -- "no
        # filter" -- since both are falsy in Python). See
        # _sql_graph_base.py's identical fix for the full rationale;
        # `None` (no filter at all) is left to reach the Cypher builder
        # unchanged -- it is not reachable from an authorized caller.
        if pack_ids is not None and not pack_ids:
            return []

        cypher, params = self._build_neighbors_cypher(
            node_id=node_id,
            direction=direction,
            depth=depth,
            limit=limit,
            pack_ids=pack_ids,
            include_unpackaged=include_unpackaged,
            spaces=spaces,
        )

        with self._session() as session:
            result = session.run(cypher, **params)
            return [
                {
                    "properties": dict(record["props"]),
                    "labels": list(record["labels"]),
                    "relationship_types": list(record["relationship_types"]),
                    "relation_type": list(record["relationship_types"])[-1]
                    if record["relationship_types"] else "",
                    "depth": record["depth"],
                    **({"node_type": record["node_type"]}
                       if self._record_value(record, "node_type") else {}),
                }
                for record in result
            ]

    @staticmethod
    def _build_neighbors_cypher(
        node_id: str,
        direction: str,
        depth: int,
        limit: int,
        pack_ids: list[str] | None,
        include_unpackaged: bool,
        spaces: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Pure helper that builds the Cypher query string and parameters.

        Separated for unit testing — exercising real Neo4j is out of scope
        for this change; we only verify the generated query structure.
        """
        depth = int(depth)
        arrow = {
            "out": "-[rels*1..{depth}]->",
            "in": "<-[rels*1..{depth}]-",
            "both": "-[rels*1..{depth}]-",
        }.get(direction, "-[rels*1..{depth}]-").format(depth=depth)

        where_clauses: list[str] = []
        params: dict[str, Any] = {"id": node_id, "limit": limit}

        if pack_ids:
            params["pack_ids"] = list(pack_ids)
            if include_unpackaged:
                where_clauses.append(
                    "ALL(n IN nodes(path) WHERE n.pack_id IS NULL OR n.pack_id IN $pack_ids)"
                )
                where_clauses.append(
                    "ALL(r IN relationships(path) WHERE r.pack_id IS NULL OR r.pack_id IN $pack_ids)"
                )
            else:
                where_clauses.append(
                    "ALL(n IN nodes(path) WHERE n.pack_id IN $pack_ids)"
                )
                where_clauses.append(
                    "ALL(r IN relationships(path) WHERE r.pack_id IS NULL OR r.pack_id IN $pack_ids)"
                )

        if spaces:
            # Strict membership, no "include unspaced" escape hatch (matches
            # the BM25/vector legs — issue #52). `space` is a plain node
            # property (see upsert_node's `props["space"] = space_id`), so
            # this is a direct WHERE push, same shape as pack_ids above.
            params["spaces"] = list(spaces)
            where_clauses.append(
                "ALL(n IN nodes(path) WHERE n.space IN $spaces)"
            )

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        cypher = f"""
            MATCH path = (start:OpenCrabNode {{node_id: $id}}){arrow}(neighbor:OpenCrabNode)
            {where_sql}
            WITH neighbor, labels(neighbor) AS labels, neighbor.node_type AS node_type,
                 properties(neighbor) AS props,
                 [rel IN relationships(path) | type(rel)] AS relationship_types,
                 length(path) AS depth
            RETURN DISTINCT props, labels, node_type, relationship_types, depth
            LIMIT $limit
        """
        return cypher, params

    def find_path(
        self, from_id: str, to_id: str, max_depth: int = 4
    ) -> list[dict[str, Any]]:
        """Find shortest path between two nodes by id."""
        self._require_available()

        cypher = f"""
            MATCH path = shortestPath(
                (a:OpenCrabNode {{node_id: $from_id}})-[*1..{max_depth}]->(b:OpenCrabNode {{node_id: $to_id}})
            )
            RETURN [node IN nodes(path) | properties(node)] AS node_props,
                   [rel IN relationships(path) | type(rel)] AS rel_types
        """
        with self._session() as session:
            result = session.run(cypher, from_id=from_id, to_id=to_id)
            record = result.single()
            if not record:
                return []
            return [
                {"node": node, "relation": rel}
                for node, rel in zip(record["node_props"], record["rel_types"] + [""])
            ]

    def count_nodes(self, node_type: str | None = None) -> int:
        """Count nodes, optionally filtered by type."""
        self._require_available()

        if node_type:
            node_type = self._label(node_type)
            cypher = "MATCH (n:OpenCrabNode) WHERE n.node_type=$node_type RETURN count(n) AS cnt"
        else:
            cypher = "MATCH (n:OpenCrabNode) RETURN count(n) AS cnt"

        with self._session() as session:
            result = session.run(cypher, node_type=node_type) if node_type else session.run(cypher)
            record = result.single()
            return int(record["cnt"]) if record else 0

    def ping(self) -> bool:
        """Return True if the database is reachable."""
        try:
            with self._session() as session:
                session.run("RETURN 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Extended operations — GraphStoreExtended (opencrab/stores/_graph_protocol.py)
    #
    # These 7 methods mirror what LocalGraphStore/PGGraphStore/KuzuGraphStore
    # already provide. Several were previously inlined as ad-hoc Cypher in
    # consumers (content_pack_list's Neo4j branch, lever_simulate's Neo4j
    # branch, pack/neo4j_export.py's _node_query/_edge_query, ontology_get_node's
    # Neo4j fallback) — the Cypher below is copied from those call sites
    # verbatim (or generalised, for find_by_relations) so switching a consumer
    # from its old inline branch to calling these methods directly produces
    # identical output.
    # ------------------------------------------------------------------

    def get_node_by_id(self, node_id: str) -> dict[str, Any] | None:
        """Type-agnostic node lookup by id alone; None if not found.

        ``n.node_type`` is authoritative, so compatibility-label order never
        affects the returned type.
        """
        self._require_available()
        cypher = "MATCH (n:OpenCrabNode {node_id: $id}) RETURN properties(n) AS props, n.node_type AS lbl LIMIT 1"
        with self._session() as session:
            result = session.run(cypher, id=node_id)
            record = result.single()
            if not record:
                return None
            props = self._clean_node_properties(record["props"], node_id, record["lbl"])
            props["node_type"] = record["lbl"]
            return props

    def get_nodes_by_id(self, node_id: str) -> list[dict[str, Any]]:
        """Plural counterpart to ``get_node_by_id`` -- returns EVERY node for
        ``node_id`` instead of the single arbitrary one ``LIMIT 1`` picks.

        The global node_id constraint normally yields one row. This method
        still returns every row so an ambiguous legacy fixture is not hidden
        by a LIMIT, and keeps deterministic ordering for diagnostics.
        ``ORDER BY lbl`` makes the row order deterministic; ``[]`` when
        nothing matches."""
        self._require_available()
        cypher = (
            "MATCH (n:OpenCrabNode {node_id: $id}) RETURN properties(n) AS props, n.node_type AS lbl "
            "ORDER BY lbl"
        )
        with self._session() as session:
            result = session.run(cypher, id=node_id)
            results = []
            for record in result:
                props = self._clean_node_properties(record["props"], node_id, record["lbl"])
                props["node_type"] = record["lbl"]
                results.append(props)
            return results

    def list_pack_ids(self) -> set[str]:
        """See GraphStore.list_pack_ids.

        ``MATCH (n)`` with NO label, unlike ``list_packs``' ``(n:OpenCrabNode)``.
        ``scripts/import_pack_graph_to_neo4j.py`` MERGEs each node under its
        own domain label, so a label-restricted scan misses whole imported
        packs -- and missing them is precisely the state #147's startup
        guard exists to refuse.
        """
        self._require_available()
        # Nodes and relationships both: an edge may carry a pack_id no node
        # has, and the migration unions the two when it builds the registry.
        node_cypher = "MATCH (n:OpenCrabNode) WHERE n.pack_id IS NOT NULL RETURN DISTINCT n.pack_id AS pid"
        edge_cypher = "MATCH (:OpenCrabNode)-[r]->(:OpenCrabNode) WHERE r.pack_id IS NOT NULL RETURN DISTINCT r.pack_id AS pid"
        out: set[str] = set()
        with self._session() as session:
            for cypher in (node_cypher, edge_cypher):
                out |= {str(rec["pid"]) for rec in session.run(cypher) if rec["pid"]}
        return out

    def list_packs(self, min_nodes: int = 1) -> list[dict[str, Any]]:
        """Aggregate node counts per pack_id; packs below min_nodes omitted.

        Cypher matches content_pack_list()'s former inline Neo4j branch:
        anchor node (``dataset:{pack_id}``) title takes priority, falling
        back to any node's ``source_package_title``.
        """
        self._require_available()
        cypher = """
            MATCH (n:OpenCrabNode)
            WHERE n.pack_id IS NOT NULL
            WITH n.pack_id AS pack_id, count(n) AS node_count,
                 collect(CASE WHEN n.id = 'dataset:' + n.pack_id THEN n.title ELSE null END) AS anchor_titles,
                 collect(CASE WHEN n.id = 'dataset:' + n.pack_id THEN n.description ELSE null END) AS anchor_descs,
                 collect(n.source_package_title) AS pkg_titles
            WHERE node_count >= $min_nodes
            WITH pack_id, node_count,
                 coalesce(
                     [t IN anchor_titles WHERE t IS NOT NULL AND t <> ''][0],
                     [t IN pkg_titles  WHERE t IS NOT NULL AND t <> ''][0],
                     ''
                 ) AS sample_title,
                 coalesce(
                     [d IN anchor_descs WHERE d IS NOT NULL AND d <> ''][0],
                     ''
                 ) AS sample_description
            RETURN pack_id, node_count, sample_title, sample_description
            ORDER BY node_count DESC
        """
        with self._session() as session:
            result = session.run(cypher, min_nodes=min_nodes)
            return [dict(record) for record in result]

    def find_by_relations_scoped(
        self,
        node_id: str,
        relations: list[str],
        pack_ids: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """See GraphStore.find_by_relations_scoped. Anchor, other endpoint
        and relationship are all constrained in the Cypher WHERE, ahead of
        LIMIT."""
        self._require_available()
        if not relations or not pack_ids or limit <= 0:
            return []
        arrow = {
            "out": "(a:OpenCrabNode {node_id: $id})-[r]->(b:OpenCrabNode)",
            "in": "(b:OpenCrabNode)-[r]->(a:OpenCrabNode {node_id: $id})",
        }.get(direction, "(a:OpenCrabNode {node_id: $id})-[r]-(b:OpenCrabNode)")
        cypher = f"""
            MATCH {arrow}
            WHERE type(r) IN $relations
              AND a.pack_id IN $pack_ids
              AND b.pack_id IN $pack_ids
              AND (r.pack_id IS NULL OR r.pack_id IN $pack_ids)
            RETURN properties(b) AS props, labels(b) AS labels,
                   b.node_type AS node_type, type(r) AS relation
            LIMIT {int(limit)}
        """
        with self._session() as session:
            result = session.run(
                cypher, id=node_id, relations=list(relations), pack_ids=list(pack_ids)
            )
            return [
                {
                    "properties": dict(rec["props"]),
                    "labels": list(rec["labels"]),
                    "relation_type": rec["relation"],
                    **({"node_type": rec["node_type"]}
                       if self._record_value(rec, "node_type") else {}),
                }
                for rec in result
            ]

    def find_by_relations(
        self,
        node_id: str,
        relations: list[str],
        direction: str = "out",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Single-hop neighbours filtered to a relation-type allow-list.

        Cypher generalises lever_simulate()'s former inline Neo4j branch
        (``-[r:raises|lowers|stabilizes|optimizes]->``) to an arbitrary
        relation list and direction. Relation types are interpolated
        directly into the pattern (Cypher cannot bind relationship types as
        parameters) — same approach ``upsert_edge`` already uses for a
        single relation type.
        """
        self._require_available()
        if not relations:
            return []
        rel_pattern = "|".join(self._label(relation) for relation in relations)
        arrow = {
            "out": f"-[r:{rel_pattern}]->",
            "in": f"<-[r:{rel_pattern}]-",
            "both": f"-[r:{rel_pattern}]-",
        }.get(direction, f"-[r:{rel_pattern}]->")

        cypher = f"""
            MATCH (n:OpenCrabNode {{node_id: $id}}){arrow}(m:OpenCrabNode)
            RETURN properties(m) AS props, labels(m) AS labels,
                   m.node_type AS node_type, type(r) AS relation_type
            LIMIT $limit
        """
        with self._session() as session:
            result = session.run(cypher, id=node_id, limit=limit)
            return [
                {
                    "properties": dict(record["props"]),
                    "labels": list(record["labels"]),
                    "relation_type": record["relation_type"],
                    **({"node_type": record["node_type"]}
                       if self._record_value(record, "node_type") else {}),
                }
                for record in result
            ]

    def export_nodes(
        self, pack_id: str | None = None, limit: int = 500_000, space: str | None = None
    ) -> list[dict[str, Any]]:
        """Bulk node export for pack ingest/re-export tooling.

        Cypher is identical to pack/neo4j_export.py's former inline
        ``_node_query()`` helper (pack_id matches ``pack_id``, ``source``,
        or ``source_id``). ``space``, when given, is pushed into the same
        WHERE clause ahead of LIMIT via the ``space`` node property --
        Neo4jStore writes ``props["space"]`` natively on upsert (see
        ``upsert_node``), so this is real pushdown, not a Python post-filter
        (issue #54).

        ``limit <= 0`` (issue #120): returns ``[]`` without issuing a query
        -- a raw negative ``LIMIT`` literal is invalid Cypher (Neo4j rejects
        it at runtime), and the SQL/Kuzu backends already never query for
        ``limit <= 0``, so this keeps all four implementations agreeing.
        """
        self._require_available()
        if limit <= 0:
            return []
        cypher = f"""
            MATCH (n:OpenCrabNode)
            WHERE ($pack_id IS NULL
               OR n.pack_id = $pack_id OR n.source = $pack_id OR n.source_id = $pack_id)
              AND ($space IS NULL OR n.space = $space)
            RETURN properties(n) AS props, labels(n) AS labels,
                   n.node_type AS node_type
            LIMIT {int(limit)}
        """
        with self._session() as session:
            result = session.run(cypher, pack_id=pack_id, space=space)
            return [
                {
                    "props": dict(record["props"]),
                    "labels": list(record["labels"]),
                    **({"node_type": record["node_type"]}
                       if self._record_value(record, "node_type") else {}),
                }
                for record in result
            ]

    def count_exported_nodes(
        self, pack_id: str | None = None, space: str | None = None
    ) -> int:
        """Real ``count(n)`` with the exact same predicate ``export_nodes``
        filters on, unbounded by any LIMIT -- issue #54: ``total`` must
        reflect the true match count, not get truncated by a caller's
        display ``limit``."""
        self._require_available()
        cypher = """
            MATCH (n:OpenCrabNode)
            WHERE ($pack_id IS NULL
               OR n.pack_id = $pack_id OR n.source = $pack_id OR n.source_id = $pack_id)
              AND ($space IS NULL OR n.space = $space)
            RETURN count(n) AS total
        """
        with self._session() as session:
            result = session.run(cypher, pack_id=pack_id, space=space)
            record = result.single()
            return int(record["total"]) if record else 0

    # ------------------------------------------------------------------
    # Scoped (authorization) surface — issue #147 §3.4(b). See
    # _graph_protocol.py::GraphStoreExtended's "Scoped (authorization)
    # surface" section for why these are separate from get_node_by_id/
    # export_nodes/count_exported_nodes/export_edges (unchanged, kept for
    # the bulk pack-export/fork use case). Neo4j's properties are native
    # (not a JSON blob like Kuzu's), so every predicate here pushes
    # straight into Cypher -- there is no Python post-filter path needed.
    # ------------------------------------------------------------------

    def export_nodes_scoped(
        self, pack_ids: list[str], limit: int, space: str | None = None
    ) -> list[dict[str, Any]]:
        """Strict pack_id-only counterpart to ``export_nodes`` (never
        ``source``/``source_id``). Empty ``pack_ids`` -> ``[]`` without
        issuing a query. ``limit <= 0`` -> ``[]``, same guard
        ``export_nodes`` uses (issue #120)."""
        self._require_available()
        if not pack_ids or limit <= 0:
            return []
        cypher = f"""
            MATCH (n:OpenCrabNode)
            WHERE n.pack_id IN $pack_ids
              AND ($space IS NULL OR n.space = $space)
            RETURN properties(n) AS props, labels(n) AS labels,
                   n.node_type AS node_type
            LIMIT {int(limit)}
        """
        with self._session() as session:
            result = session.run(cypher, pack_ids=list(pack_ids), space=space)
            return [
                {
                    "props": dict(record["props"]),
                    "labels": list(record["labels"]),
                    **({"node_type": record["node_type"]}
                       if self._record_value(record, "node_type") else {}),
                }
                for record in result
            ]

    def count_exported_nodes_scoped(
        self, pack_ids: list[str], space: str | None = None
    ) -> int:
        """Exact ``count(n)`` counterpart to ``export_nodes_scoped``, same
        predicate, unbounded by any LIMIT. Empty ``pack_ids`` -> ``0``
        without querying."""
        self._require_available()
        if not pack_ids:
            return 0
        cypher = """
            MATCH (n:OpenCrabNode)
            WHERE n.pack_id IN $pack_ids
              AND ($space IS NULL OR n.space = $space)
            RETURN count(n) AS total
        """
        with self._session() as session:
            result = session.run(cypher, pack_ids=list(pack_ids), space=space)
            record = result.single()
            return int(record["total"]) if record else 0

    def export_edges_scoped(self, pack_ids: list[str], limit: int) -> list[dict[str, Any]]:
        """AND rule (issue #147 §3.4(b)) -- BOTH endpoints' ``pack_id``
        must be in ``pack_ids``, AND the edge's own ``pack_id`` (if it has
        one) must also be in ``pack_ids`` -- the opposite of
        ``export_edges``' OR-across-endpoints predicate, for the same
        reason ``_sql_graph_base.py::export_edges_scoped`` gives: the
        response embeds both endpoints' full properties, so an OR would
        expose an out-of-scope node via the OTHER endpoint's membership.

        Empty ``pack_ids`` -> ``[]`` without querying. ``limit <= 0`` ->
        ``[]``."""
        self._require_available()
        if not pack_ids or limit <= 0:
            return []
        cypher = f"""
            MATCH (a:OpenCrabNode)-[r]->(b:OpenCrabNode)
            WHERE a.pack_id IN $pack_ids AND b.pack_id IN $pack_ids
              AND (r.pack_id IS NULL OR r.pack_id IN $pack_ids)
            RETURN properties(a) AS source_props, labels(a) AS source_labels,
                   properties(b) AS target_props, labels(b) AS target_labels,
                   properties(r) AS rel_props, type(r) AS relation
            LIMIT {int(limit)}
        """
        with self._session() as session:
            result = session.run(cypher, pack_ids=list(pack_ids))
            return [
                {
                    "source_props": dict(record["source_props"]),
                    "source_labels": list(record["source_labels"]),
                    "target_props": dict(record["target_props"]),
                    "target_labels": list(record["target_labels"]),
                    "rel_props": dict(record["rel_props"]),
                    "relation": record["relation"],
                }
                for record in result
            ]

    def get_node_by_id_scoped(self, node_id: str, pack_ids: list[str]) -> dict[str, Any] | None:
        """Pack predicate pushed into the Cypher WHERE ahead of ``LIMIT
        1`` -- unlike Kuzu's version of this method, Neo4j's native
        properties let the predicate reach Cypher directly, so (unlike
        Kuzu) a ``LIMIT 1`` here is safe: only already-in-scope rows are
        candidates by the time ``LIMIT`` runs, so it can never pick an
        out-of-scope row the way an unscoped ``LIMIT 1`` followed by a
        Python post-filter could.

        Empty ``pack_ids`` -> ``None`` without querying."""
        self._require_available()
        if not pack_ids:
            return None
        cypher = (
            "MATCH (n:OpenCrabNode {node_id: $id}) WHERE n.pack_id IN $pack_ids "
            "RETURN properties(n) AS props, n.node_type AS lbl LIMIT 1"
        )
        with self._session() as session:
            result = session.run(cypher, id=node_id, pack_ids=list(pack_ids))
            record = result.single()
            if not record:
                return None
            props = self._clean_node_properties(record["props"], node_id, record["lbl"])
            props["node_type"] = record["lbl"]
            return props

    def export_edges(
        self, pack_id: str | None = None, limit: int = 1_000_000
    ) -> list[dict[str, Any]]:
        """Bulk edge export, joined with both endpoints' properties.

        Cypher is identical to pack/neo4j_export.py's former inline
        ``_edge_query()`` helper (pack_id matches either endpoint's
        ``pack_id``/``source``/``source_id``, or the edge's own).
        """
        self._require_available()
        node_filter = (
            "($pack_id IS NULL OR a.pack_id = $pack_id OR a.source = $pack_id OR a.source_id = $pack_id) "
            "OR ($pack_id IS NULL OR b.pack_id = $pack_id OR b.source = $pack_id OR b.source_id = $pack_id) "
            "OR ($pack_id IS NULL OR r.pack_id = $pack_id OR r.source = $pack_id OR r.source_id = $pack_id)"
        )
        cypher = f"""
            MATCH (a:OpenCrabNode)-[r]->(b:OpenCrabNode)
            WHERE {node_filter}
            RETURN properties(a) AS source_props, labels(a) AS source_labels,
                   properties(b) AS target_props, labels(b) AS target_labels,
                   properties(r) AS rel_props, type(r) AS relation
            LIMIT {int(limit)}
        """
        with self._session() as session:
            result = session.run(cypher, pack_id=pack_id)
            return [
                {
                    "source_props": dict(record["source_props"]),
                    "source_labels": list(record["source_labels"]),
                    "target_props": dict(record["target_props"]),
                    "target_labels": list(record["target_labels"]),
                    "rel_props": dict(record["rel_props"]),
                    "relation": record["relation"],
                }
                for record in result
            ]

    def graph_fingerprint(self) -> str:
        """Return the same canonical graph snapshot fingerprint as SQL stores."""
        self._require_available()
        nodes: list[list[str]] = []
        edges: list[list[str]] = []
        with self._session() as session:
            for record in session.run(
                "MATCH (n:OpenCrabNode) RETURN n.node_id AS node_id, n.node_type AS node_type, "
                "n.node_digest AS digest, properties(n) AS props"
            ):
                nid = record["node_id"]
                props = self._clean_node_properties(record["props"], nid, record["node_type"])
                digest = canonical_node_digest(record["node_type"], props.get("space"), props)
                nodes.append([nid, digest])
            for record in session.run(
                "MATCH (a:OpenCrabNode)-[r]->(b:OpenCrabNode) "
                "RETURN r.from_id AS from_id, r.relation AS relation, r.to_id AS to_id, "
                "r.from_type AS from_type, r.to_type AS to_type, properties(r) AS props"
            ):
                fid, rel, tid = record["from_id"], record["relation"], record["to_id"]
                props = normalize_edge_properties(fid, rel, tid, self._clean_edge_properties(record["props"]))
                digest = canonical_edge_digest(fid, rel, tid, record["from_type"], record["to_type"], props)
                edges.append([fid, rel, tid, digest])
        payload = {
            "schema_fingerprint": self._provenance_schema_fingerprint(),
            "nodes": sorted(nodes),
            "edges": sorted(edges),
        }
        return hashlib.sha256(b"opencrab.issue80.provenance-target.v1\0" + canonical_json_bytes(payload)).hexdigest()

    def backfill_pack_provenance(self, records: list[dict[str, Any]]) -> ProvenanceBatchReceipt:
        """Apply a frozen plan through one managed Neo4j transaction."""
        self._require_available()
        if not records:
            raise ValueError("graph provenance records must be a non-empty sequence")
        from opencrab.stores._sql_graph_base import _SqlGraphStoreBase

        validated = [_SqlGraphStoreBase._validate_provenance_record(record)[1] for record in records]
        seen: set[tuple[str, Any]] = set()
        for record in validated:
            key: Any = (
                record["node_id"]
                if record["kind"] == "node"
                else (record["from_id"], record["relation"], record["to_id"])
            )
            marker = (record["kind"], key)
            if marker in seen:
                raise RuntimeError(f"graph pack provenance duplicate key: {record['kind']}:{key}")
            seen.add(marker)
        target = validated[0]["target_fingerprint"]
        if any(record["target_fingerprint"] != target for record in validated):
            raise ValueError("graph provenance target fingerprint mismatch")
        def work(tx: Any) -> ProvenanceBatchReceipt:
                def fingerprint() -> str:
                    nodes: list[list[str]] = []
                    edges: list[list[str]] = []
                    for row in tx.run("MATCH (n:OpenCrabNode) RETURN n.node_id AS node_id, n.node_type AS node_type, n.node_digest AS digest, properties(n) AS props"):
                        props = self._clean_node_properties(row["props"], row["node_id"], row["node_type"])
                        nodes.append([row["node_id"], canonical_node_digest(row["node_type"], props.get("space"), props)])
                    for row in tx.run("MATCH (a:OpenCrabNode)-[r]->(b:OpenCrabNode) RETURN r.from_id AS from_id, r.relation AS relation, r.to_id AS to_id, r.from_type AS from_type, r.to_type AS to_type, properties(r) AS props"):
                        props = normalize_edge_properties(row["from_id"], row["relation"], row["to_id"], self._clean_edge_properties(row["props"]))
                        edges.append([row["from_id"], row["relation"], row["to_id"], canonical_edge_digest(row["from_id"], row["relation"], row["to_id"], row["from_type"], row["to_type"], props)])
                    return hashlib.sha256(
                        b"opencrab.issue80.provenance-target.v1\0"
                        + canonical_json_bytes(
                            {
                                "schema_fingerprint": self._provenance_schema_fingerprint(),
                                "nodes": sorted(nodes),
                                "edges": sorted(edges),
                            }
                        )
                    ).hexdigest()

                before = fingerprint()
                if before != target:
                    raise RuntimeError("graph pack provenance target fingerprint mismatch")
                receipts: list[ProvenanceWriteReceipt] = []
                for record in validated:
                    if record["kind"] == "node":
                        nid = record["node_id"]
                        row = tx.run("MATCH (n:OpenCrabNode {node_id:$node_id}) RETURN properties(n) AS props, n.node_type AS node_type, n.node_digest AS digest LIMIT 1", node_id=nid).single()
                        if row is None:
                            raise RuntimeError(f"graph pack provenance node missing: {nid}")
                        props = self._clean_node_properties(row["props"], nid, row["node_type"])
                        digest = canonical_node_digest(row["node_type"], props.get("space"), props)
                        if row["node_type"] != record["node_type"]:
                            raise RuntimeError(f"graph pack provenance node snapshot mismatch: {nid}")
                        if digest != record["expected_current_digest"]:
                            raise NodeIdentityConflict(f"stale node update: {nid}")
                        owner = props.get("pack_id")
                        if owner not in (None, "") and owner != record["proposed_pack_id"]:
                            raise RuntimeError("graph pack provenance conflict")
                        remove_alias = "pack" in props
                        expected_delta = {
                            "set": {} if owner not in (None, "") else {"pack_id": record["proposed_pack_id"]},
                            "remove": ["pack"] if remove_alias else [],
                        }
                        if record["allowed_properties_delta"] != expected_delta:
                            raise RuntimeError("graph pack provenance delta mismatch")
                        if owner in (None, "") or remove_alias:
                            new_props = dict(props)
                            new_props.pop("pack", None)
                            if owner in (None, ""):
                                new_props["pack_id"] = record["proposed_pack_id"]
                            after_digest = canonical_node_digest(record["node_type"], props.get("space"), new_props)
                            remove_alias_clause = " REMOVE n.pack" if remove_alias else ""
                            changed = tx.run(
                                "MATCH (n:OpenCrabNode {node_id:$node_id}) "
                                "WHERE n.node_digest=$expected_digest "
                                "AND (n.pack_id IS NULL OR n.pack_id='') "
                                "SET n += $props, n.node_digest=$node_digest"
                                + remove_alias_clause + " RETURN n",
                                node_id=nid, props=new_props, node_digest=after_digest,
                                expected_digest=record["expected_current_digest"],
                            ).single()
                            if changed is None:
                                raise NodeIdentityConflict(f"stale node update: {nid}")
                            receipts.append(ProvenanceWriteReceipt("node", nid, "assigned", digest, after_digest, 1))
                        else:
                            receipts.append(ProvenanceWriteReceipt("node", nid, "idempotent", digest, digest, 0))
                    else:
                        key = (record["from_id"], record["relation"], record["to_id"])
                        endpoints = tx.run(
                            "MATCH (n:OpenCrabNode) WHERE n.node_id IN [$from_id, $to_id] "
                            "RETURN n.node_id AS node_id, n.node_type AS node_type",
                            from_id=key[0], to_id=key[2],
                        )
                        endpoint_types = {row["node_id"]: row["node_type"] for row in endpoints}
                        if key[0] not in endpoint_types or key[2] not in endpoint_types:
                            missing = key[0] if key[0] not in endpoint_types else key[2]
                            raise ValueError(f"edge endpoint does not exist: {missing}")
                        if endpoint_types[key[0]] != record["from_type"]:
                            raise ValueError(f"edge endpoint type mismatch: {key[0]}")
                        if endpoint_types[key[2]] != record["to_type"]:
                            raise ValueError(f"edge endpoint type mismatch: {key[2]}")
                        row = tx.run(f"MATCH (a:OpenCrabNode {{node_id:$from_id}})-[r:`{self._label(record['relation'])}`]->(b:OpenCrabNode {{node_id:$to_id}}) RETURN properties(r) AS props, r.from_type AS from_type, r.to_type AS to_type, r.edge_digest AS digest LIMIT 1", from_id=key[0], to_id=key[2]).single()
                        if row is None:
                            raise RuntimeError(f"graph pack provenance edge missing: ({key[0]}, {key[1]}, {key[2]})")
                        props = normalize_edge_properties(*key, self._clean_edge_properties(row["props"]))
                        digest = canonical_edge_digest(*key, row["from_type"], row["to_type"], props)
                        if row["from_type"] != record["from_type"] or row["to_type"] != record["to_type"]:
                            raise ValueError(f"edge endpoint type mismatch: {key[0]}")
                        if digest != record["expected_current_digest"]:
                            raise EdgeIdentityConflict(f"stale edge update: ({key[0]}, {key[1]}, {key[2]})")
                        owner = props.get("pack_id")
                        if owner not in (None, "") and owner != record["proposed_pack_id"]:
                            raise RuntimeError("graph pack provenance conflict")
                        remove_alias = "pack" in props
                        expected_delta = {
                            "set": {} if owner not in (None, "") else {"pack_id": record["proposed_pack_id"]},
                            "remove": ["pack"] if remove_alias else [],
                        }
                        if record["allowed_properties_delta"] != expected_delta:
                            raise RuntimeError("graph pack provenance delta mismatch")
                        if owner in (None, "") or remove_alias:
                            new_props = dict(props)
                            new_props.pop("pack", None)
                            if owner in (None, ""):
                                new_props["pack_id"] = record["proposed_pack_id"]
                            after_digest = canonical_edge_digest(*key, row["from_type"], row["to_type"], new_props)
                            remove_alias_clause = " REMOVE r.pack" if remove_alias else ""
                            changed = tx.run(
                                f"MATCH (a:OpenCrabNode {{node_id:$from_id}})-[r:`{self._label(record['relation'])}`]->(b:OpenCrabNode {{node_id:$to_id}}) "
                                "WHERE r.edge_digest=$expected_digest "
                                "AND (r.pack_id IS NULL OR r.pack_id='') "
                                "SET r += $props, r.edge_digest=$edge_digest"
                                + remove_alias_clause + " RETURN r",
                                from_id=key[0], to_id=key[2], props=new_props,
                                edge_digest=after_digest,
                                expected_digest=record["expected_current_digest"],
                            ).single()
                            if changed is None:
                                raise EdgeIdentityConflict(f"stale edge update: ({key[0]}, {key[1]}, {key[2]})")
                            receipts.append(ProvenanceWriteReceipt("edge", key, "assigned", digest, after_digest, 1))
                        else:
                            receipts.append(ProvenanceWriteReceipt("edge", key, "idempotent", digest, digest, 0))
                return ProvenanceBatchReceipt(before, fingerprint(), tuple(receipts))

        # ``_run_write`` opens the managed transaction and claims the same
        # database-global writer lock as every other mutation. Do not open a
        # separate session here: that would split the provenance CAS across
        # two transactions.
        return self._run_write(work)

    def upsert_nodes_batch(self, nodes: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[NodeWriteReceipt, ...]:
        """Bulk upsert; returns the count processed.

        Per-item loop calling ``upsert_node`` — same approach
        ``KuzuGraphStore.upsert_nodes_batch`` uses, since a node's label is
        fixed at Cypher-compile time and a single ``UNWIND`` can't vary the
        label across a mixed-node_type batch without APOC.
        """
        self._require_schema_ready()
        if not nodes:
            return () if return_receipt else 0
        prepared = []
        seen: set[str] = set()
        for item in nodes:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            node_id = item.get("node_id")
            if not isinstance(node_id, str):
                raise ValueError("malformed graph identity")
            if node_id in seen:
                raise ValueError(f"duplicate global graph key in batch: node_id={node_id}")
            seen.add(node_id)
            node_type, props, space_id, _digest = prepare_node(
                item["node_type"], node_id, item.get("properties", {}), item.get("space_id")
            )
            prepared.append((node_type, node_id, props, space_id))

        def write(tx: Any):
            receipts = []
            for node_type, node_id, props, space_id in prepared:
                result = self.upsert_node(
                    node_type, node_id, props, space_id, return_receipt=return_receipt
                )
                if return_receipt:
                    receipts.append(result)
            return tuple(receipts) if return_receipt else len(prepared)
        return self._run_write(write)

    def upsert_edges_batch(self, edges: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[EdgeWriteReceipt, ...]:
        """Bulk upsert; returns the count of edges that upserted successfully.

        Per-item loop calling ``upsert_edge`` — mirrors
        ``KuzuGraphStore.upsert_edges_batch``.
        """
        self._require_schema_ready()
        if not edges:
            return () if return_receipt else 0
        prepared = []
        seen: set[tuple[str, str, str]] = set()
        for item in edges:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            key = (item.get("from_id"), item.get("relation"), item.get("to_id"))
            if not all(isinstance(value, str) and value for value in key):
                raise ValueError("malformed graph identity")
            if key in seen:
                raise ValueError(f"duplicate global graph key in batch: edge_key={key}")
            seen.add(key)
            from_type = self._label(item["from_type"])
            to_type = self._label(item["to_type"])
            props = normalize_edge_properties(key[0], key[1], key[2], item.get("properties"))
            prepared.append((from_type, key[0], key[1], to_type, key[2], props))

        def write(tx: Any):
            count = 0
            receipts = []
            for from_type, from_id, relation, to_type, to_id, props in prepared:
                result = self.upsert_edge(
                    from_type, from_id, relation, to_type, to_id, props,
                    return_receipt=return_receipt,
                )
                if result:
                    count += 1
                    if return_receipt:
                        receipts.append(result)
            return tuple(receipts) if return_receipt else count
        return self._run_write(write)

    def update_nodes_batch(self, nodes: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[NodeWriteReceipt, ...]:
        self._require_schema_ready()
        if not nodes:
            return () if return_receipt else 0
        seen: set[str] = set()
        prepared = []
        for item in nodes:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            node_id = item.get("node_id")
            if node_id in seen:
                raise ValueError(f"duplicate global graph key in batch: node_id={node_id}")
            seen.add(node_id)
            validate_digest(item.get("expected_current_digest"))
            new_type, new_props, new_space, _digest = prepare_node(
                item.get("new_type", item.get("node_type")), node_id,
                item.get("new_properties", item.get("properties")),
                item.get("new_space_id", item.get("space_id")),
            )
            prepared.append((
                node_id, item["expected_current_digest"],
                new_type, new_props, new_space,
            ))

        def write(tx: Any):
            receipts = []
            for node_id, expected, new_type, props, space_id in prepared:
                result = self.update_node(
                    node_id, expected, new_type, props, space_id,
                    return_receipt=return_receipt,
                )
                if return_receipt:
                    receipts.append(result)
            return tuple(receipts) if return_receipt else len(prepared)
        return self._run_write(write)

    def update_edges_batch(self, edges: list[dict[str, Any]], *, return_receipt: bool = False) -> int | tuple[EdgeWriteReceipt, ...]:
        self._require_schema_ready()
        if not edges:
            return () if return_receipt else 0
        seen: set[tuple[str, str, str]] = set()
        prepared = []
        for item in edges:
            if not isinstance(item, dict):
                raise ValueError("malformed graph batch item")
            key = (item.get("from_id"), item.get("relation"), item.get("to_id"))
            if not all(isinstance(value, str) and value for value in key):
                raise ValueError("malformed graph identity")
            if key in seen:
                raise ValueError(f"duplicate global graph key in batch: edge_key={key}")
            seen.add(key)
            validate_digest(item.get("expected_current_digest"), edge=True)
            owner = item.get("owner_pack_id")
            if not isinstance(owner, str) or not owner:
                raise ValueError("graph identity fields must be non-empty strings")
            from_type = self._label(item["from_type"])
            to_type = self._label(item["to_type"])
            props = normalize_edge_properties(key[0], key[1], key[2], item.get("properties"))
            if props.get("pack_id") != owner:
                raise EdgeIdentityConflict(f"stale edge update: ({key[0]}, {key[1]}, {key[2]})")
            prepared.append((
                from_type, key[0], key[1], to_type, key[2],
                props, item["expected_current_digest"], owner,
            ))

        def write(tx: Any):
            receipts = []
            for from_type, from_id, relation, to_type, to_id, props, expected, owner in prepared:
                result = self.update_edge(
                    from_type, from_id, relation, to_type, to_id, props,
                    expected_current_digest=expected, owner_pack_id=owner,
                    return_receipt=return_receipt,
                )
                if return_receipt:
                    receipts.append(result)
            return tuple(receipts) if return_receipt else len(prepared)
        return self._run_write(write)
