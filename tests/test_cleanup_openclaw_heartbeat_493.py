"""#493 heartbeat 정리 도구의 fixture 전용 안전 경계 시험이다."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import cleanup_openclaw_heartbeat_493 as cleanup  # noqa: E402

TABLE = "vectors_test"
HEARTBEAT = "Q: [OpenClaw heartbeat poll] tick"


def _db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _schema(root: Path) -> None:
    with _db(root / "doc_store.db") as conn:
        conn.executescript(
            "CREATE TABLE doc_sources (source_id TEXT PRIMARY KEY, text TEXT, metadata TEXT);"
            "CREATE VIRTUAL TABLE doc_sources_fts USING fts5(source_id UNINDEXED, text);"
            "CREATE TABLE doc_nodes (space TEXT, node_id TEXT, node_type TEXT, properties TEXT, PRIMARY KEY(space,node_id));"
        )
    with _db(root / "graph.db") as conn:
        conn.executescript(
            "CREATE TABLE graph_nodes (node_type TEXT, node_id TEXT PRIMARY KEY, space_id TEXT, properties TEXT);"
            "CREATE TABLE graph_edges (from_type TEXT, from_id TEXT, relation TEXT, to_type TEXT, to_id TEXT, properties TEXT, PRIMARY KEY(from_id,relation,to_id));"
        )
    with _db(root / "opencrab.db") as conn:
        conn.executescript(
            "CREATE TABLE ontology_nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, space TEXT NOT NULL, node_type TEXT NOT NULL, node_id TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')), UNIQUE(space,node_id));"
            "CREATE TABLE ontology_edges (id INTEGER PRIMARY KEY AUTOINCREMENT, from_space TEXT NOT NULL, from_id TEXT NOT NULL, relation TEXT NOT NULL, to_space TEXT NOT NULL, to_id TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')), UNIQUE(from_space,from_id,relation,to_space,to_id));"
        )
    import sqlite_vec

    with _db(root / "vectors.db") as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute(
            f"CREATE VIRTUAL TABLE {TABLE} USING vec0(node_id TEXT PRIMARY KEY, pack_id TEXT partition key, embedding float[2], +document TEXT, +metadata TEXT)"
        )


def _put(conn: sqlite3.Connection, sql: str, values: tuple[object, ...]) -> None:
    conn.execute(sql, values)


def _add(root: Path, node_id: str, text: str, pack: object = "openclaw", *, source: bool = True, fts: bool = True, doc: bool = True, graph: bool = True, vector: bool = True, node_type: str = "Evidence", space: str = "evidence", vector_node_id: bool = True) -> None:
    metadata = json.dumps({"pack_id": pack, "node_id": node_id})
    props = json.dumps({"pack_id": pack})
    if source or fts or doc:
        with _db(root / "doc_store.db") as conn:
            if source:
                _put(conn, "INSERT INTO doc_sources VALUES (?,?,?)", (node_id, text, metadata))
            if fts:
                _put(conn, "INSERT INTO doc_sources_fts VALUES (?,?)", (node_id, text))
            if doc:
                _put(conn, "INSERT INTO doc_nodes VALUES (?,?,?,?)", (space, node_id, node_type, props))
    if graph:
        with _db(root / "graph.db") as conn:
            _put(conn, "INSERT INTO graph_nodes VALUES (?,?,?,?)", (node_type, node_id, space, props))
        with _db(root / "opencrab.db") as conn:
            _put(conn, "INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", (space, node_type, node_id))
    if vector:
        import sqlite_vec

        with _db(root / "vectors.db") as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            _put(conn, f"INSERT INTO {TABLE}(node_id,pack_id,embedding,document,metadata) VALUES (?,?,?,?,?)", (node_id, pack, sqlite_vec.serialize_float32([0.0, 1.0]), text, metadata if vector_node_id else json.dumps({"pack_id": pack})))


@pytest.fixture
def world(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    _schema(root)
    _add(root, "evidence:turn:heartbeat", HEARTBEAT)
    _add(root, "evidence:turn:normal", "discussion includes OpenClaw heartbeat poll later")
    _add(root, "evidence:turn:fallback", HEARTBEAT, pack="other")
    return root


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


_OBSERVER_FIELDS = {"source", "kind", "action", "arg1", "arg2", "raw"}
_OBSERVER_FORBIDDEN = {
    getattr(sqlite3, name)
    for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE", "SQLITE_ALTER_TABLE", "SQLITE_ATTACH", "SQLITE_DETACH",
    )
}


_NODE_DELETE_SQL = (
    "DELETE FROM ontology_nodes WHERE id=? AND space=? AND node_type=? AND node_id=? "
    "AND created_at IS ? AND updated_at IS ?"
)
_EDGE_DELETE_SQL = (
    "DELETE FROM ontology_edges WHERE id=? AND from_space=? AND from_id=? AND relation=? "
    "AND to_space=? AND to_id=? AND created_at IS ?"
)


def _frozen_registry_delete_params(candidates: list[dict]) -> dict[str, set[tuple[object, ...]]]:
    nodes: set[tuple[object, ...]] = set()
    edges: set[tuple[object, ...]] = set()
    for candidate in candidates:
        for node in candidate["snapshot"]["registry_node"]:
            nodes.add((node["id"], node["space"], node["node_type"], node["node_id"], node["created_at"], node["updated_at"]))
        for edge in candidate["snapshot"]["registry_edges"]:
            edges.add((edge["id"], edge["from_space"], edge["from_id"], edge["relation"], edge["to_space"], edge["to_id"], edge["created_at"]))
    return {"node": nodes, "edge": edges}


def _assert_registry_delete_records(records: list[dict], candidates: list[dict]) -> None:
    allowed = _frozen_registry_delete_params(candidates)
    for record in records:
        sql, params = record["sql"], tuple(record["params"])
        if sql == _NODE_DELETE_SQL:
            if len(params) != 6 or params not in allowed["node"]:
                raise AssertionError("registry delete target")
        elif sql == _EDGE_DELETE_SQL:
            if len(params) != 7 or params not in allowed["edge"]:
                raise AssertionError("registry delete target")
        else:
            raise AssertionError("registry delete target")


def _assert_observer_events(events: list[dict], *, production: bool) -> None:
    if not events:
        raise AssertionError("pragma event missing" if not production else "observer event missing")
    if not all(set(event) == _OBSERVER_FIELDS for event in events):
        raise AssertionError("observer event fields")
    if not all(isinstance(event["source"], str) and isinstance(event["kind"], str) for event in events):
        raise AssertionError("observer event types")
    writes = [event for event in events if event["kind"] == "authorizer" and event["action"] in _OBSERVER_FORBIDDEN]
    writes = [event for event in writes if event["action"] != sqlite3.SQLITE_DELETE]
    if writes:
        raise AssertionError("forbidden action")
    if production:
        if any(event["source"] == "observer-unit" for event in events):
            raise AssertionError("observer unit source")
        if not any(event["kind"] == "trace" and event["action"] == "SELECT" for event in events):
            raise AssertionError("registry select missing")
        return
    pragmas = [event for event in events if event["kind"] == "authorizer" and event["action"] == sqlite3.SQLITE_PRAGMA]
    if not pragmas:
        raise AssertionError("pragma event missing")
    if not any(event["kind"] == "trace" and event["action"] == "SELECT" for event in events):
        raise AssertionError("normal read missing")
    for event in pragmas:
        name, arg = event["arg1"], event["arg2"]
        if name == "query_only":
            if arg not in {None, ""}:
                raise AssertionError("query_only argument")
        elif name in {"table_info", "foreign_key_list"}:
            if arg not in {"ontology_nodes", "ontology_edges"}:
                raise AssertionError("pragma table")
        else:
            raise AssertionError("pragma name")


def _all_registry_raw(path: Path) -> tuple[list[tuple], list[tuple]]:
    with _db(path) as conn:
        nodes = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id").fetchall()
        edges = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id").fetchall()
    return nodes, edges


def _all_db_bytes(world: Path) -> dict[str, str]:
    return {name: (world / name).read_bytes().hex() for name in cleanup.DB_FILES}


def _all_db_rows(world: Path, registry_conn: sqlite3.Connection | None = None) -> dict[str, list[tuple]]:
    with _db(world / "doc_store.db") as conn:
        doc = conn.execute("SELECT source_id,text,metadata FROM doc_sources ORDER BY source_id").fetchall()
        fts = conn.execute("SELECT source_id,text FROM doc_sources_fts ORDER BY source_id").fetchall()
        doc_nodes = conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes ORDER BY space,node_id").fetchall()
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        vector = conn.execute(f"SELECT * FROM {TABLE} ORDER BY node_id").fetchall()
    with _db(world / "graph.db") as conn:
        graph_nodes = conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes ORDER BY node_id").fetchall()
        graph_edges = conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges ORDER BY from_id,relation,to_id").fetchall()
    if registry_conn is None:
        registry_nodes, registry_edges = _all_registry_raw(world / "opencrab.db")
    else:
        registry_nodes = registry_conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id").fetchall()
        registry_edges = registry_conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id").fetchall()
    return {"source": doc, "fts": fts, "doc": doc_nodes, "vector": vector, "graph": graph_nodes, "graph_edges": graph_edges, "registry": registry_nodes, "registry_edges": registry_edges}


def _logical_json_rows(rows_by_table: dict[str, list[tuple]]) -> dict[str, list[object]]:
    def value(item: object) -> object:
        if isinstance(item, bytes):
            return {"bytes": item.hex()}
        if isinstance(item, tuple):
            return [value(part) for part in item]
        return item
    return {name: [value(row) for row in rows] for name, rows in rows_by_table.items()}


def _logical_rows_json(world: Path) -> dict[str, list[object]]:
    return _logical_json_rows(_all_db_rows(world))


def _expected_after_prior_axes(rows: dict[str, list[tuple]], candidate: str) -> dict[str, list[tuple]]:
    expected = {name: [*values] for name, values in rows.items()}
    expected["vector"] = [row for row in expected["vector"] if row[0] != candidate]
    expected["graph"] = [row for row in expected["graph"] if row[1] != candidate]
    expected["graph_edges"] = [row for row in expected["graph_edges"] if row[1] != candidate and row[4] != candidate]
    expected["source"] = [row for row in expected["source"] if row[0] != candidate]
    expected["fts"] = [row for row in expected["fts"] if row[0] != candidate]
    expected["doc"] = [row for row in expected["doc"] if row[1] != candidate]
    return expected


def _expected_registry_delete(rows: dict[str, list[tuple]], candidate: str) -> dict[str, list[tuple]]:
    expected = {name: [*values] for name, values in rows.items()}
    expected["registry"] = [row for row in expected["registry"] if row[3] != candidate]
    expected["registry_edges"] = [row for row in expected["registry_edges"] if row[2] != candidate and row[5] != candidate]
    return expected


def _logical_rows_setup(snapshot: Path) -> str:
    return (
        "import sqlite_vec\n"
        f"logical_snapshot_path = {str(snapshot)!r}\n"
        "def logical_value(item):\n"
        "    if isinstance(item, bytes): return {'bytes': item.hex()}\n"
        "    if isinstance(item, tuple): return [logical_value(part) for part in item]\n"
        "    return item\n"
        "def logical_rows(data, registry_conn=None):\n"
        "    with sqlite3.connect(data / 'doc_store.db') as conn:\n"
        "        source = conn.execute('SELECT source_id,text,metadata FROM doc_sources ORDER BY source_id').fetchall(); fts = conn.execute('SELECT source_id,text FROM doc_sources_fts ORDER BY source_id').fetchall(); doc = conn.execute('SELECT space,node_id,node_type,properties FROM doc_nodes ORDER BY space,node_id').fetchall()\n"
        "    with sqlite3.connect(data / 'vectors.db') as conn:\n"
        "        conn.enable_load_extension(True); sqlite_vec.load(conn); vector = conn.execute('SELECT * FROM vectors_test ORDER BY node_id').fetchall()\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        graph = conn.execute('SELECT node_type,node_id,space_id,properties FROM graph_nodes ORDER BY node_id').fetchall(); graph_edges = conn.execute('SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges ORDER BY from_id,relation,to_type,to_id').fetchall()\n"
        "    if registry_conn is None:\n"
        "        with sqlite3.connect(data / 'opencrab.db') as conn:\n"
        "            registry = conn.execute('SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id').fetchall(); registry_edges = conn.execute('SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id').fetchall()\n"
        "    else:\n"
        "        registry = registry_conn.execute('SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id').fetchall(); registry_edges = registry_conn.execute('SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id').fetchall()\n"
        "    return {name: [logical_value(row) for row in rows] for name, rows in {'source': source, 'fts': fts, 'doc': doc, 'vector': vector, 'graph': graph, 'graph_edges': graph_edges, 'registry': registry, 'registry_edges': registry_edges}.items()}\n"
        "def save_logical_rows(data, registry_conn=None):\n"
        "    open(logical_snapshot_path, 'w', encoding='utf-8').write(json.dumps(logical_rows(data, registry_conn), sort_keys=True))\n"
    )



def _without_witness_pragmas(trace_text: str) -> str:
    """apply가 유지하는 쓰기 연결의 `PRAGMA data_version`과 trigger 확인 읽기 줄은 쓰기 추적에서 뺀다."""
    ignored = {"PRAGMA data_version", "SELECT count(*) FROM sqlite_master WHERE type='trigger'"}
    return "".join(line for line in trace_text.splitlines(keepends=True) if line.strip() not in ignored)


# 가드가 섹션 비교로 옮겨 가면서 parametrize 값(old/new 문자열)이 바뀌었다. 수정 전 test ID를 보존하려고
# 바뀐 항목의 ID는 수정 전 문자열로 만든다.
_LEGACY_GUARD_TEXT = {
    "registry-claim": (
        'if _claim_failures(graph, conn, doc, candidates, manifest, axis="registry"):\n' + " " * 16 + 'raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
        "if False:\n" + " " * 16 + 'raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
    ),
    "registry-proof": (
        'if _claim_failures(graph, conn, doc, candidates, manifest, axis="registry"):\n' + " " * 16 + 'raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
        "if False:\n" + " " * 16 + 'raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
    ),
    "doc-raw": (
        'if source != evidence.get("source"):\n' + " " * 20 + 'raise CleanupRefusedError("registry protection conflict")',
        "if False:\n" + " " * 20 + 'raise CleanupRefusedError("registry protection conflict")',
    ),
}


def _legacy_param_ids(rows: list[tuple[str, str, str]]) -> list[str]:
    return [f"{name}-{_LEGACY_GUARD_TEXT.get(name, (old, new))[0]}-{_LEGACY_GUARD_TEXT.get(name, (old, new))[1]}" for name, old, new in rows]


def _catalog_mutation(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1
    mutated = source.replace(old, new)
    assert mutated.count(new) == 1
    return mutated


def _event_action(sql: str) -> str:
    token = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if token not in {"SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "ROLLBACK"}:
        raise AssertionError("observation error")
    return token


def _assert_event_contract(events: list[dict], *, expect_clean: bool) -> None:
    application = [event for event in events if event.get("source", "application") == "application"]
    attempts = {
        (event.get("connection_id"), event.get("call_id"))
        for event in application
        if event.get("phase") == "attempt"
    }
    for event in events:
        source = event.get("source", "application")
        action = event.get("action")
        phase = event.get("phase")
        if source == "sqlite-trace":
            if phase != "trace":
                raise AssertionError("observation error")
            if action in {"INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "ROLLBACK"} and (event.get("connection_id"), event.get("operation_id")) not in attempts:
                raise AssertionError("coverage error")
            continue
        if source != "application" or action not in {"SELECT", "PRAGMA", "INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "ROLLBACK"}:
            raise AssertionError("observation error")
        if phase not in {"attempt", "success", "failure", "transaction"}:
            raise AssertionError("observation error")
        if phase == "transaction" and action not in {"BEGIN", "COMMIT", "ROLLBACK"}:
            raise AssertionError("observation error")
    writes = [event for event in application if event.get("phase") == "attempt" and event.get("action") in {"INSERT", "UPDATE", "DELETE"}]
    commits = [event for event in application if event.get("phase") == "transaction" and event.get("action") == "COMMIT"]
    if expect_clean and (writes or commits):
        raise AssertionError("write event")


def _trace_setup(trace: Path, deletes: Path) -> str:
    return (
        "import json, sqlite3\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        f"deletes = open({str(deletes)!r}, 'w', encoding='utf-8')\n"
        "call_id = 0\n"
        "connection_id = 0\n"
        "def emit(event):\n"
        "    trace.write(json.dumps(event, sort_keys=True) + '\\n'); trace.flush()\n"
        "def action_of(sql):\n"
        "    return sql.lstrip().split(None, 1)[0].upper() if sql and sql.strip() else ''\n"
        "class TracedConnection(sqlite3.Connection):\n"
        "    def _event(self, phase, action, sql=None, params=None, operation_id=None, **extra):\n"
        "        global call_id\n"
        "        call_id += 1\n"
        "        emit({'source': 'application', 'phase': phase, 'db': self._trace_db, 'connection_id': self._connection_id, 'action': action, 'sql': sql, 'params': list(params) if params is not None else None, 'call_id': call_id, 'operation_id': operation_id, **extra})\n"
        "        return call_id\n"
        "    def execute(self, sql, params=()):\n"
        "        action = action_of(sql)\n"
        "        operation_id = self._event('attempt', action, sql, params)\n"
        "        if action not in {'SELECT', 'PRAGMA', 'INSERT', 'UPDATE', 'DELETE', 'BEGIN', 'COMMIT', 'ROLLBACK'}:\n"
        "            self._event('failure', action, sql, params, operation_id, error='observation error')\n"
        "            raise AssertionError('observation error')\n"
        "        self._operation_id = operation_id\n"
        "        try:\n"
        "            cursor = super().execute(sql, params)\n"
        "        except Exception as exc:\n"
        "            self._event('failure', action, sql, params, operation_id, error=type(exc).__name__)\n"
        "            raise\n"
        "        finally:\n"
        "            self._operation_id = None\n"
        "        self._event('success', action, sql, params, operation_id, rowcount=cursor.rowcount)\n"
        "        return cursor\n"
        "    def commit(self):\n"
        "        operation_id = self._event('attempt', 'COMMIT')\n"
        "        self._operation_id = operation_id\n"
        "        try:\n"
        "            result = super().commit()\n"
        "        except Exception as exc:\n"
        "            self._event('failure', 'COMMIT', operation_id=operation_id, error=type(exc).__name__)\n"
        "            raise\n"
        "        finally:\n"
        "            self._operation_id = None\n"
        "        self._event('transaction', 'COMMIT', operation_id=operation_id)\n"
        "        return result\n"
        "    def rollback(self):\n"
        "        operation_id = self._event('attempt', 'ROLLBACK')\n"
        "        self._operation_id = operation_id\n"
        "        try:\n"
        "            result = super().rollback()\n"
        "        except Exception as exc:\n"
        "            self._event('failure', 'ROLLBACK', operation_id=operation_id, error=type(exc).__name__)\n"
        "            raise\n"
        "        finally:\n"
        "            self._operation_id = None\n"
        "        self._event('transaction', 'ROLLBACK', operation_id=operation_id)\n"
        "        return result\n"
        "def connect(path, *, read_only, immutable=False):\n"
        "    global connection_id\n"
        "    query = 'mode=ro&immutable=1' if read_only and immutable else 'mode=ro' if read_only else 'mode=rw'\n"
        "    conn = sqlite3.connect(f'{path.resolve().as_uri()}?{query}', uri=True, factory=TracedConnection)\n"
        "    connection_id += 1\n"
        "    conn._connection_id = connection_id\n"
        "    conn._operation_id = None\n"
        "    conn._trace_db = path.name\n"
        "    def callback(sql):\n"
        "        emit({'source': 'sqlite-trace', 'phase': 'trace', 'db': path.name, 'connection_id': conn._connection_id, 'action': action_of(sql), 'sql': sql, 'params': None, 'operation_id': conn._operation_id})\n"
        "    conn.set_trace_callback(callback)\n"
        "    return conn\n"
        "cleanup._rw = lambda path: connect(path, read_only=False)\n"
        "cleanup._ro = lambda path, immutable: connect(path, read_only=True, immutable=immutable)\n"
        "original_delete = cleanup._registry_delete\n"
        "def observe_delete(conn, sql, params):\n"
        "    action = action_of(sql)\n"
        "    deletes.write(json.dumps({'source': 'application', 'phase': 'attempt', 'db': 'opencrab.db', 'action': action, 'sql': sql, 'params': list(params)}) + '\\n'); deletes.flush()\n"
        "    try:\n"
        "        cursor = original_delete(conn, sql, params)\n"
        "    except Exception as exc:\n"
        "        deletes.write(json.dumps({'source': 'application', 'phase': 'failure', 'db': 'opencrab.db', 'action': action, 'error': type(exc).__name__}) + '\\n'); deletes.flush()\n"
        "        raise\n"
        "    deletes.write(json.dumps({'source': 'application', 'phase': 'success', 'db': 'opencrab.db', 'action': action, 'rowcount': cursor.rowcount}) + '\\n'); deletes.flush()\n"
        "    return cursor\n"
        "cleanup._registry_delete = observe_delete\n"
    )


def _assert_axis_commits(events: list[dict], names: set[str]) -> None:
    committed = {event.get("db") for event in events if event.get("source") == "application" and event.get("phase") == "transaction" and event.get("action") == "COMMIT"}
    assert names <= committed


def _survivor_raw(world: Path, node_id: str, claim_id: str | None = None) -> dict[str, list[tuple]]:
    with _db(world / "doc_store.db") as conn:
        source = conn.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (node_id,)).fetchall()
        fts = conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (node_id,)).fetchall()
        doc = conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (node_id,)).fetchall()
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        vector = conn.execute(f"SELECT * FROM {TABLE} WHERE node_id=?", (node_id,)).fetchall()
    with _db(world / "graph.db") as conn:
        graph = conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (node_id,)).fetchall()
        graph_support = conn.execute(
            "SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges "
            "WHERE (from_type=? AND from_id=? AND to_type=? AND to_id=?) "
            "OR (from_type=? AND from_id=? AND to_type=? AND to_id=?)",
            ("Evidence", node_id, "Claim", claim_id, "Claim", claim_id, "Evidence", node_id),
        ).fetchall() if claim_id else []
    with _db(world / "opencrab.db") as conn:
        registry = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (node_id,)).fetchall()
        registry_support = conn.execute(
            "SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges "
            "WHERE (from_space=? AND from_id=? AND to_space=? AND to_id=?) "
            "OR (from_space=? AND from_id=? AND to_space=? AND to_id=?)",
            ("evidence", node_id, "claim", claim_id, "claim", claim_id, "evidence", node_id),
        ).fetchall() if claim_id else []
    return {"source": source, "fts": fts, "doc": doc, "vector": vector, "graph": graph, "graph_support": graph_support, "registry": registry, "registry_support": registry_support}


def _assert_candidate_absent(world: Path, candidate: str) -> None:
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT * FROM doc_sources WHERE source_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT * FROM doc_sources_fts WHERE source_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT * FROM doc_nodes WHERE node_id=?", (candidate,)).fetchall() == []
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert conn.execute(f"SELECT * FROM {TABLE} WHERE node_id=?", (candidate,)).fetchall() == []
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT * FROM graph_nodes WHERE node_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT * FROM graph_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == []
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT * FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT * FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == []


def _cli_main(tmp_path: Path, name: str, args: list[str], setup: str = "") -> subprocess.CompletedProcess[str]:
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{name}.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        + setup
        + f"raise SystemExit(cleanup.main({args!r}))\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(runner)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _run_copy_apply(world: Path, tmp_path: Path, run: Path, backup: Path, approval: Path, source: str, name: str, injection: str = "") -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    copy = tmp_path / f"{name}.py"
    trace = tmp_path / f"{name}.jsonl"
    deletes = tmp_path / f"{name}.deletes.jsonl"
    copy.write_text(source, encoding="utf-8")
    runner = tmp_path / f"{name}.runner.py"
    runner.write_text(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        f"spec = importlib.util.spec_from_file_location('cleanup_copy', {str(copy)!r})\n"
        "cleanup = importlib.util.module_from_spec(spec)\n"
        "sys.modules['cleanup_copy'] = cleanup\n"
        "spec.loader.exec_module(cleanup)\n"
        "sys.path.insert(0, " + repr(str(Path(__file__).parent.parent / "scripts")) + ")\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        + _trace_setup(trace, deletes)
        + injection
        + f"raise SystemExit(cleanup.main({_registry_apply_args(world, run, backup, approval)!r}))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines()]
    return result, [*events, *delete_events]


def _cli_rollback_state(world: Path, tmp_path: Path) -> tuple[Path, Path, list[str]]:
    run, backup = tmp_path / "run", tmp_path / "backup"
    base = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]
    dry = _cli_main(tmp_path, "dry", base)
    assert dry.returncode == 0, dry.stderr
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    applied = _cli_main(tmp_path, "apply", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)])
    assert applied.returncode == 0, applied.stderr
    prepared = _cli_main(tmp_path, "prepare", [*base, "--prepare-rollback", "--backup-dir", str(backup)])
    assert prepared.returncode == 0, prepared.stderr
    return run, backup, [*base, "--rollback", "--backup-dir", str(backup)]


def test_dry_run_selects_exact_prefix_and_strict_pack_without_writes(world: Path, tmp_path: Path):
    before = {path.name: path.read_bytes() for path in world.iterdir()}
    result = cleanup.dry_run(world, tmp_path / "run", TABLE)
    assert result["eligible"] is True
    lines = (tmp_path / "run" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["evidence:turn:heartbeat"]
    assert {path.name: path.read_bytes() for path in world.iterdir()} == before
    assert _load(tmp_path / "run" / "report.json")["registry"]["mode"] == "report-only"


@pytest.mark.parametrize("mutate", ["missing-fts", "wrong-space", "mixed-vector", "document-id"])
def test_dry_run_refuses_incomplete_or_wrong_ownership_axes(world: Path, tmp_path: Path, mutate: str):
    node_id = "evidence:turn:heartbeat"
    if mutate == "missing-fts":
        with _db(world / "doc_store.db") as conn:
            conn.execute("DELETE FROM doc_sources_fts WHERE source_id=?", (node_id,))
    elif mutate == "wrong-space":
        with _db(world / "doc_store.db") as conn:
            conn.execute("UPDATE doc_nodes SET space='concept' WHERE node_id=?", (node_id,))
    elif mutate == "mixed-vector":
        import sqlite_vec

        with _db(world / "vectors.db") as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            row = conn.execute(f"SELECT node_id,embedding,document,metadata FROM {TABLE} WHERE node_id=?", (node_id,)).fetchone()
            conn.execute(f"DELETE FROM {TABLE} WHERE node_id=?", (node_id,))
            conn.execute(f"INSERT INTO {TABLE}(node_id,pack_id,embedding,document,metadata) VALUES (?,?,?,?,?)", (row[0], "other", row[1], row[2], row[3]))
    else:
        with _db(world / "doc_store.db") as conn:
            conn.execute("UPDATE doc_sources SET source_id='evidence:turn:Document:wrong' WHERE source_id=?", (node_id,))
            conn.execute("UPDATE doc_sources_fts SET source_id='evidence:turn:Document:wrong' WHERE source_id=?", (node_id,))
    result = cleanup.dry_run(world, tmp_path / mutate, TABLE)
    assert result["eligible"] is False
    assert not (tmp_path / mutate / "manifest.json").exists()


def test_apply_requires_exact_approval_and_verified_backup(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    with pytest.raises(cleanup.CleanupRefusedError, match="approved-manifest"):
        cleanup.apply(world, run, TABLE, None, tmp_path / "backups", scan=lambda _: ([], 0))
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": "wrong", "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="approval"):
        cleanup.apply(world, run, TABLE, approval, tmp_path / "backups", scan=lambda _: ([], 0))


def test_apply_deletes_only_candidate_axes_and_preserves_normal_conversation(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backups", scan=lambda _: ([], 0))
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT source_id FROM doc_sources ORDER BY source_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
        assert conn.execute("SELECT source_id FROM doc_sources_fts ORDER BY source_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
        assert conn.execute("SELECT node_id FROM doc_nodes ORDER BY node_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes ORDER BY node_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert conn.execute(f"SELECT node_id FROM {TABLE} ORDER BY node_id").fetchall() == [("evidence:turn:fallback",), ("evidence:turn:normal",)]
    assert (run / "receipt.json").is_file()
    assert (run / "progress.jsonl").is_file()
    assert _load(run / "registry-after.json")["mode"] == "report-only"


def test_last_claim_support_blocks_manifest(world: Path, tmp_path: Path):
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:one", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", "evidence:turn:heartbeat", "supports", "Claim", "claim:one", "{}"))
    result = cleanup.dry_run(world, tmp_path / "run", TABLE)
    assert result["eligible"] is False
    assert "final Claim support" in result["errors"][0]


def test_nonempty_wal_refuses_before_immutable_read(world: Path, tmp_path: Path):
    wal = world / "graph.db-wal"
    wal.write_bytes(b"not empty")
    with pytest.raises(cleanup.CleanupRefusedError, match="not empty"):
        cleanup.dry_run(world, tmp_path / "run", TABLE)


def test_marker_uses_exclusive_temporary_and_no_replace(world: Path, tmp_path: Path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": {"path": "p", "device": 1, "inode": 2}, "files": {}}
    payload = cleanup._marker_payload("tombstone", receipt)
    cleanup.publish_marker(run, "rollback-tombstone.json", payload)
    assert cleanup.marker_state(run)[0] == "complete tombstone"
    with pytest.raises(FileExistsError):
        cleanup.publish_marker(run, "rollback-tombstone.json", payload)
    assert cleanup.marker_state(run)[0] == "complete tombstone"


def test_cli_apply_rechecks_marker_inside_lock_and_mutation_is_red(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    before = cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False)
    trace = tmp_path / "marker-recheck.trace"
    marker = run / ".rollback-tombstone.json.tmp"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    setup = (
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original_marker = cleanup.marker_state\n"
        "original_rw = cleanup._rw\n"
        "calls = 0\n"
        "def rw(path):\n"
        "    conn = original_rw(path)\n"
        "    conn.set_trace_callback(lambda sql: trace.write('SQL:' + path.name + ':' + sql + '\\n'))\n"
        "    return conn\n"
        "def marker_state(path):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    result = original_marker(path)\n"
        "    trace.write('MARKER:' + str(calls) + ':' + result[0] + '\\n')\n"
        "    trace.flush()\n"
        "    if calls == 1:\n"
        "        (path / '.rollback-tombstone.json.tmp').write_bytes(b'fixture-marker')\n"
        "    return result\n"
        "cleanup._rw = rw\n"
        "cleanup.marker_state = marker_state\n"
    )
    result = _cli_main(tmp_path, "marker-recheck", args, setup)
    assert result.returncode != 0
    assert "marker state: incomplete artifact" in result.stderr
    lines = trace.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["MARKER:1:absent", "MARKER:2:incomplete artifact"]
    assert marker.read_bytes() == b"fixture-marker"
    assert cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False) == before
    assert not any(line.startswith("SQL:") and any(word in line.upper() for word in ("INSERT", "UPDATE", "DELETE")) for line in lines)
    assert not (run / "receipt.json").exists()
    assert not backup.exists()


def test_incomplete_marker_fails_closed_for_apply_and_rollback(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    (run / ".rollback-tombstone.json.tmp").write_text("partial", encoding="utf-8")
    assert cleanup.marker_state(run)[0] == "incomplete artifact"
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="marker state"):
        cleanup.apply(world, run, TABLE, approval, tmp_path / "backups", scan=lambda _: ([], 0))
    with pytest.raises(cleanup.CleanupRefusedError, match="backup receipt"):
        cleanup.rollback(world, run, tmp_path / "backups")


def test_publish_marker_preserves_evidence_after_directory_fsync_failure(tmp_path: Path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": {"path": "p", "device": 1, "inode": 2}, "files": {}}
    monkeypatch.setattr(cleanup, "_fsync_dir", lambda _: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError, match="fsync"):
        cleanup.publish_marker(run, "rollback-tombstone.json", cleanup._marker_payload("tombstone", receipt))
    assert (run / "rollback-tombstone.json").is_file()
    assert cleanup.marker_state(run)[0] == "complete tombstone"


def test_restore_attempt_keeps_partial_artifact_and_uses_unique_replacement(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    source = tmp_path / "backup.db"
    destination = data / "graph.db"
    source.write_bytes(b"backup")
    destination.write_bytes(b"old")
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    old_artifact = data / ".graph.db.restore-old"
    old_artifact.write_bytes(b"partial")
    successor = cleanup._publish_attempt_successor(run, receipt)
    attempt_dir = run / "restore-attempts" / successor["directory"]
    cleanup._restore_file(source, destination, attempt_dir, receipt)
    assert old_artifact.read_bytes() == b"partial"
    assert destination.read_bytes() == b"backup"
    metadata = attempt_dir / "graph.db.json"
    assert _load(metadata)["file"] == "graph.db"
    assert _load(metadata)["attempt"] == successor["attempt"]


def test_backup_receipt_fsyncs_files_and_directories_before_receipt(world: Path, tmp_path: Path, monkeypatch):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    calls: list[Path] = []
    original_file, original_dir = cleanup._fsync_file, cleanup._fsync_dir
    monkeypatch.setattr(cleanup, "_fsync_file", lambda path: (calls.append(path), original_file(path))[1])
    monkeypatch.setattr(cleanup, "_fsync_dir", lambda path: (calls.append(path), original_dir(path))[1])
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    assert set(backup / name for name in cleanup.DB_FILES) <= set(calls)
    assert backup in calls
    assert backup.parent in calls
    assert (run / "receipt.json").is_file()


def test_cli_backup_parent_fsync_failure_preserves_evidence(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    before = cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False)
    trace = tmp_path / "backup-fsync.trace"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    setup = (
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original_fsync = cleanup._fsync_file\n"
        "original_rw = cleanup._rw\n"
        "def rw(path):\n"
        "    conn = original_rw(path)\n"
        "    conn.set_trace_callback(lambda sql: trace.write('SQL:' + path.name + ':' + sql + '\\n'))\n"
        "    return conn\n"
        "def fsync(path):\n"
        "    trace.write('FSYNC:' + str(path) + '\\n')\n"
        "    trace.flush()\n"
        "    if path == cleanup.Path(" + repr(str(backup.parent)) + "):\n"
        "        trace.write('PARENT-FAULT\\n')\n"
        "        trace.flush()\n"
        "        raise OSError('backup parent fsync fault')\n"
        "    return original_fsync(path)\n"
        "cleanup._rw = rw\n"
        "cleanup._fsync_file = fsync\n"
    )
    result = _cli_main(tmp_path, "backup-parent-fsync", args, setup)
    assert result.returncode != 0
    assert "backup parent fsync fault" in result.stderr
    lines = trace.read_text(encoding="utf-8").splitlines()
    file_indexes = [lines.index(f"FSYNC:{backup / name}") for name in cleanup.DB_FILES]
    assert max(file_indexes) < lines.index(f"FSYNC:{backup}") < lines.index(f"FSYNC:{backup.parent}") < lines.index("PARENT-FAULT")
    assert backup.exists() and all((backup / name).is_file() for name in cleanup.DB_FILES)
    assert not (run / "receipt.json").exists()
    assert cleanup._candidate_snapshot(world, TABLE, candidates[0]["id"], immutable=False) == before
    assert not any(line.startswith("SQL:") and any(word in line.upper() for word in ("INSERT", "UPDATE", "DELETE")) for line in lines)


def test_first_apply_refuses_changed_snapshot_before_backup(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "doc_store.db") as conn:
        conn.execute("DELETE FROM doc_sources_fts WHERE source_id='evidence:turn:heartbeat'")
    backup = tmp_path / "backup"
    with pytest.raises(cleanup.CleanupRefusedError, match="full frozen snapshot"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=lambda _: ([], 0))
    assert not backup.exists()
    assert not (run / "receipt.json").exists()


def test_receipt_rejects_other_data_directory(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    receipt = cleanup._backup_receipt(world, run, backup, manifest, candidates)
    other = tmp_path / "other"
    other.mkdir()
    _schema(other)
    with pytest.raises(cleanup.CleanupRefusedError, match="data directory"):
        cleanup._load_receipt(run, backup, manifest, candidates, other)
    assert receipt["data_dir"] == cleanup._data_dir_identity(world)


@pytest.mark.parametrize(("field", "value"), [("component_count", True), ("digest", "0" * 64)])
def test_receipt_rejects_strict_intent_tampering(world: Path, tmp_path: Path, field: str, value: object):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt_path = run / "receipt.json"
    receipt = _load(receipt_path)
    receipt["component_intent"][field] = value
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="component intent"):
        cleanup._load_receipt(run, backup, manifest, candidates, world)


@pytest.mark.parametrize("action", ["--apply", "--prepare-rollback", "--rollback"])
@pytest.mark.parametrize("mutation", ["empty", "canonical", "digest", "count", "manifest", "endpoint", "row", "component_ids", "deletion_target", "ordered_ids"])
def test_cli_intent_tampering_refuses_all_receipt_callers(world: Path, tmp_path: Path, action: str, mutation: str):
    first, second, other = "evidence:turn:heartbeat", "evidence:turn:intent-count", "evidence:turn:normal"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists intent count")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "rel", "Evidence", second, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "boundary", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if action == "--rollback":
        import repair_store_mismatch_499 as repair
        original_holders = repair.holders
        repair.holders = lambda _: ([], 0)
        try:
            cleanup.prepare_rollback(world, run, backup)
        finally:
            repair.holders = original_holders
    receipt_path = run / "receipt.json"
    receipt = _load(receipt_path)
    receipt_before = receipt_path.read_bytes()
    intent = receipt["component_intent"]
    ordered_ids_seam = mutation == "ordered_ids"
    if mutation == "empty":
        receipt["component_intent"] = {}
    elif mutation == "canonical":
        intent["canonical"] = "{}"
    elif mutation == "digest":
        intent["digest"] = "0" * 64
    elif mutation == "count":
        assert intent["component_count"] == 1
        intent["component_count"] = True
    elif mutation == "manifest":
        intent["manifest"] = "0" * 64
    elif mutation == "endpoint":
        intent["component"][0]["edge"]["to_id"] = "evidence:turn:tampered-endpoint"
    elif mutation == "row":
        intent["component"][0]["edge"]["properties"] = json.dumps({"tampered": True})
    elif mutation == "component_ids":
        intent["component_ids"] = []
    elif mutation == "deletion_target":
        intent["boundary"][0]["deletion_target"] = False
    if not ordered_ids_seam:
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        receipt_before = receipt_path.read_bytes()
    trace = tmp_path / f"intent-{mutation}-{action[2:]}.trace"
    runner = tmp_path / f"intent-{mutation}-{action[2:]}.py"
    script_dir = Path(__file__).parent.parent / "scripts"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, action, "--backup-dir", str(backup)]
    if action == "--apply":
        args.extend(["--approved-manifest", str(approval)])
    ordered_setup = ""
    if ordered_ids_seam:
        ordered_setup = (
            "def ordered_validate(receipt, candidates, manifest):\n"
            "    trace.write('validate-intent\\n')\n"
            "    trace.flush()\n"
            "    changed = dict(manifest)\n"
            "    changed['ordered_ids_sha256'] = '0' * 64\n"
            "    return original_validate(receipt, candidates, changed)\n"
            "cleanup._validate_intent = ordered_validate\n"
        )
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original_validate = cleanup._validate_intent\n"
        "def traced_validate(receipt, candidates, manifest):\n"
        "    trace.write('validate-intent\\n')\n"
        "    trace.flush()\n"
        "    return original_validate(receipt, candidates, manifest)\n"
        "cleanup._validate_intent = traced_validate\n"
        + ordered_setup
        + f"code = cleanup.main({args!r})\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    before = {name: (world / name).read_bytes() for name in cleanup.DB_FILES}
    tombstone = run / "rollback-tombstone.json"
    tombstone_before = tombstone.read_bytes() if action == "--rollback" else None
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0, mutation + action
    assert "receipt component intent mismatch" in result.stderr
    assert trace.read_text(encoding="utf-8") == "validate-intent\n"
    assert {name: (world / name).read_bytes() for name in cleanup.DB_FILES} == before
    assert receipt_path.read_bytes() == receipt_before
    if action == "--prepare-rollback":
        assert not tombstone.exists()
    if action == "--rollback":
        assert tombstone.read_bytes() == tombstone_before
        assert not (run / "rollback-complete.json").exists()


def test_marker_dangling_symlink_fails_closed(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "rollback-tombstone.json").symlink_to(run / "missing")
    assert cleanup.marker_state(run)[0] == "incomplete artifact"


def test_shared_candidate_edge_is_declared_target_on_second_node(world: Path, tmp_path: Path):
    second = "evidence:turn:second"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists now")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", "evidence:turn:heartbeat", "rel", "Evidence", second, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id=?", (second,)).fetchone() is None
        assert conn.execute("SELECT * FROM graph_edges").fetchall() == []


def test_restore_attempt_binding_mismatch_refuses(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    root = run / "restore-attempts"
    root.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(world), "files": {}}
    (root / "graph.db.bad.json").write_text(json.dumps({"receipt_sha256": "other", "backup_set_id": "b", "data_dir": cleanup._data_dir_identity(world), "file": "graph.db", "attempt": "bad"}), encoding="utf-8")
    with pytest.raises(cleanup.CleanupRefusedError, match="binding mismatch"):
        cleanup._validate_attempts(run, receipt, world)


def test_sidecar_removal_fsyncs_each_namespace_change(tmp_path: Path, monkeypatch):
    db = tmp_path / "graph.db"
    db.write_bytes(b"db")
    for suffix in ("-wal", "-shm", "-journal"):
        db.with_name(db.name + suffix).write_bytes(b"sidecar")
    calls: list[Path] = []
    monkeypatch.setattr(cleanup, "_fsync_dir", lambda path: calls.append(path))
    cleanup._remove_sidecars(db)
    assert calls == [tmp_path, tmp_path, tmp_path]
    assert all(not db.with_name(db.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


def test_marker_missing_binding_fields_is_incomplete(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    body = {"kind": "tombstone", "payload_sha256": ""}
    body["payload_sha256"] = cleanup._sha(cleanup._canon({"kind": "tombstone"}))
    (run / "rollback-tombstone.json").write_text(json.dumps(body), encoding="utf-8")
    assert cleanup.marker_state(run)[0] == "incomplete artifact"


def test_apply_rechecks_claim_support_and_protected_edges(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    other = "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:keep", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Claim", "claim:keep", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", other, "supports", "Claim", "claim:keep", "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='supports'", (other,))
    with pytest.raises(cleanup.CleanupRefusedError, match="full frozen snapshot"):
        cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))


def test_claim_guard_rejects_candidate_only_support(world: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:only", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Claim", "claim:only", "{}"))
    candidates, detail = cleanup.select(world, TABLE)
    assert cleanup._claim_guard(world, candidates, {"protected": detail["protected"]}) == ["claim:only"]


def test_unrelated_unsupported_claim_does_not_block_candidate_apply(world: Path, tmp_path: Path):
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:unrelated", "claim", "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id='claim:unrelated'").fetchall() == [("claim:unrelated",)]


def test_supports_edge_to_evidence_is_not_claim_loss(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Evidence", other, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup.apply(world, run, TABLE, approval, tmp_path / "backup", scan=lambda _: ([], 0))


def test_cli_document_partial_resume_requires_receipt(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    candidate = candidates[0]
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    initial = _cli_main(tmp_path, "initial-document", args)
    assert initial.returncode == 0, initial.stderr
    with _db(world / "doc_store.db") as conn:
        conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", (candidate["id"], HEARTBEAT))
        conn.execute("INSERT INTO doc_nodes VALUES (?,?,?,?)", ("evidence", candidate["id"], "Evidence", json.dumps({"pack_id": "openclaw"})))
    result = _cli_main(tmp_path, "resume-document", args)
    assert result.returncode == 0, result.stderr
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT source_id FROM doc_sources_fts WHERE source_id=?", (candidate["id"],)).fetchall() == []
        assert conn.execute("SELECT node_id FROM doc_nodes WHERE node_id=?", (candidate["id"],)).fetchall() == []


def test_cli_document_partial_first_apply_refuses_without_receipt(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    candidate = candidates[0]
    with _db(world / "doc_store.db") as conn:
        conn.execute("DELETE FROM doc_sources WHERE source_id=?", (candidate["id"],))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "first-document.py"
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main({args!r}))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists()
    assert not (run / "receipt.json").exists()


def test_cli_registry_reports_expected_and_actual_orphans_after_candidate_cleanup(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    registry = world / "opencrab.db"
    with _db(registry) as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "preexisting", "evidence", "missing"))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "expected", "evidence", normal))
    registry_uri = f"{registry.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(registry_uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        edges = {tuple(row) for row in conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges")}
    graph_uri = f"{(world / 'graph.db').resolve().as_uri()}?mode=ro"
    with sqlite3.connect(graph_uri, uri=True) as conn:
        before_ids = {row[0] for row in conn.execute("SELECT node_id FROM graph_nodes")}
    run, backup = tmp_path / "run", tmp_path / "backup"
    trace = tmp_path / "registry.trace"
    base = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]

    def setup(source: str) -> str:
        return (
            "import json\n"
            "import sqlite3\n"
            f"trace = open({str(trace)!r}, 'a', encoding='utf-8')\n"
            "for name in ('_ro', '_rw'):\n"
            "    original = getattr(cleanup, name)\n"
            "    def wrap(factory, connection_source):\n"
            "        def open_connection(path, *args):\n"
            "            conn = factory(path, *args)\n"
            "            if path.name == 'opencrab.db':\n"
            "                forbidden_names = ('SQLITE_INSERT', 'SQLITE_UPDATE', 'SQLITE_DELETE', 'SQLITE_CREATE_INDEX', 'SQLITE_CREATE_TABLE', 'SQLITE_CREATE_TEMP_INDEX', 'SQLITE_CREATE_TEMP_TABLE', 'SQLITE_CREATE_TEMP_TRIGGER', 'SQLITE_CREATE_TEMP_VIEW', 'SQLITE_CREATE_TRIGGER', 'SQLITE_CREATE_VIEW', 'SQLITE_CREATE_VTABLE', 'SQLITE_DROP_INDEX', 'SQLITE_DROP_TABLE', 'SQLITE_DROP_TEMP_INDEX', 'SQLITE_DROP_TEMP_TABLE', 'SQLITE_DROP_TEMP_TRIGGER', 'SQLITE_DROP_TEMP_VIEW', 'SQLITE_DROP_TRIGGER', 'SQLITE_DROP_VIEW', 'SQLITE_DROP_VTABLE', 'SQLITE_ALTER_TABLE', 'SQLITE_ATTACH', 'SQLITE_DETACH')\n"
            "                forbidden = {getattr(sqlite3, item) for item in forbidden_names}\n"
            "                def event(kind, action, arg1=None, arg2=None, raw=None):\n"
            "                    trace.write(json.dumps({'source': connection_source, 'kind': kind, 'action': action, 'arg1': arg1, 'arg2': arg2, 'raw': raw}, sort_keys=True) + '\\n')\n"
            "                    trace.flush()\n"
            "                def authorizer(action, arg1, arg2, database, trigger):\n"
            "                    event('authorizer', action, arg1, arg2)\n"
            "                    return sqlite3.SQLITE_OK\n"
            "                conn.set_authorizer(authorizer)\n"
            "                conn.set_trace_callback(lambda sql: event('trace', sql.lstrip().split(None, 1)[0].upper(), raw=sql))\n"
            "            return conn\n"
            "        return open_connection\n"
            "    setattr(cleanup, name, wrap(original, " + repr(source) + " + ':' + name))\n"
        )

    before_dry = registry.read_bytes()
    dry = _cli_main(tmp_path, "registry-dry", base, setup("dry"))
    assert dry.returncode == 0, dry.stderr
    assert registry.read_bytes() == before_dry
    report = _load(run / "report.json")["registry"]
    expected_ids = before_ids - {candidate}
    independently_expected = {edge for edge in edges if edge[1] not in expected_ids or edge[4] not in expected_ids}
    assert {tuple(edge) for edge in report["expected_after_orphans"]} == independently_expected
    assert {tuple(edge) for edge in report["preexisting_orphans"]} != independently_expected
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    before_apply = registry.read_bytes()
    frozen_candidates = cleanup._load_run(run)[0]
    allowed = _frozen_registry_delete_params(frozen_candidates)
    with _db(registry) as conn:
        all_nodes_before = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id").fetchall()
        all_edges_before = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id").fetchall()
        candidate_before = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall()
    protected_before = [row for row in all_nodes_before if row not in allowed["node"]]
    protected_edges_before = [row for row in all_edges_before if row not in allowed["edge"]]
    assert [(row[1], row[2], row[3]) for row in candidate_before] == [("evidence", "Evidence", candidate)]
    delete_trace = tmp_path / "registry-delete-params.jsonl"
    apply_setup = setup("apply") + (
        f"delete_trace = open({str(delete_trace)!r}, 'w', encoding='utf-8')\n"
        "original_delete = cleanup._registry_delete\n"
        "def observe_delete(conn, sql, params):\n"
        "    delete_trace.write(json.dumps({'sql': sql, 'params': list(params)}, sort_keys=True) + '\\n')\n"
        "    delete_trace.flush()\n"
        "    return original_delete(conn, sql, params)\n"
        "cleanup._registry_delete = observe_delete\n"
    )
    applied = _cli_main(tmp_path, "registry-apply", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)], apply_setup)
    assert applied.returncode == 0, applied.stderr
    assert registry.read_bytes() != before_apply
    delete_events = [json.loads(line) for line in delete_trace.read_text(encoding="utf-8").splitlines()]
    assert delete_events
    _assert_registry_delete_records(delete_events, frozen_candidates)
    with _db(registry) as conn:
        all_nodes_after = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id").fetchall()
        all_edges_after = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id").fetchall()
        assert conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == []
    assert all_nodes_after == protected_before
    assert all_edges_after == protected_edges_before
    actual_report = _load(run / "registry-after.json")
    with sqlite3.connect(graph_uri, uri=True) as conn:
        actual_ids = {row[0] for row in conn.execute("SELECT node_id FROM graph_nodes")}
    with sqlite3.connect(registry_uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        actual_edges = {tuple(row) for row in conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges")}
    independently_actual = {edge for edge in actual_edges if edge[1] not in actual_ids or edge[4] not in actual_ids}
    assert {tuple(edge) for edge in actual_report["preexisting_orphans"]} == independently_actual
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    _assert_observer_events(events, production=True)
    assert any(event["source"] == "dry:_ro" and event["kind"] == "trace" and event["action"] == "SELECT" for event in events)
    assert any(event["source"] == "apply:_ro" and event["kind"] == "trace" and event["action"] == "SELECT" for event in events)


def test_transaction_seams_record_commit_and_rollback(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, tmp_path / "backup", manifest, candidates)
    events: list[str] = []
    original_commit, original_rollback = cleanup._commit, cleanup._rollback

    def commit(conn: sqlite3.Connection) -> None:
        events.append("commit-attempt")
        original_commit(conn)
        events.append("commit-success")

    def rollback(conn: sqlite3.Connection) -> None:
        events.append("rollback-attempt")
        original_rollback(conn)
        events.append("rollback-success")

    cleanup._commit, cleanup._rollback = commit, rollback
    try:
        cleanup._delete_vector(world, TABLE, candidates[0])
        assert events == ["commit-attempt", "commit-success"]
        events.clear()
        with pytest.raises(cleanup.CleanupRefusedError):
            cleanup._delete_vector(world, TABLE, {**candidates[0], "vector": {**candidates[0]["vector"], "digest": "wrong"}})
        assert events == ["rollback-attempt", "rollback-success"]
        events.clear()
        failing_commit = cleanup._commit

        def commit_failure(conn: sqlite3.Connection) -> None:
            events.append("commit-attempt")
            raise OSError("commit failure")

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        _schema(fresh)
        _add(fresh, "evidence:turn:heartbeat", HEARTBEAT)
        fresh_run = tmp_path / "fresh-run"
        cleanup.dry_run(fresh, fresh_run, TABLE)
        fresh_candidates, _ = cleanup._load_run(fresh_run)
        cleanup._commit = commit_failure
        with pytest.raises(OSError, match="commit failure"):
            cleanup._delete_vector(fresh, TABLE, fresh_candidates[0])
        assert events == ["commit-attempt", "rollback-attempt", "rollback-success"]
        cleanup._commit = failing_commit
    finally:
        cleanup._commit, cleanup._rollback = original_commit, original_rollback


def _collector_child(tmp_path: Path, name: str, body: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    trace = tmp_path / f"{name}.jsonl"
    deletes = tmp_path / f"{name}.deletes.jsonl"
    runner = tmp_path / f"{name}.runner.py"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner.write_text(
        "from pathlib import Path\n"
        "import sqlite3, sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        + _trace_setup(trace, deletes)
        + body,
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    return result, events


def test_event_collector_detects_actual_writes(tmp_path: Path):
    db = tmp_path / "events.db"
    bootstrap = (
        f"with sqlite3.connect({str(db)!r}) as raw:\n"
        "    raw.execute('CREATE TABLE rows (id INTEGER PRIMARY KEY, value TEXT)')\n"
        "    raw.execute(\"INSERT INTO rows VALUES (1, 'old')\")\n"
        "    raw.execute(\"CREATE VIRTUAL TABLE rows_fts USING fts5(value)\")\n"
        "    raw.execute(\"INSERT INTO rows_fts VALUES ('old')\")\n"
    )
    select, select_events = _collector_child(tmp_path, "collector-select", bootstrap + f"conn = cleanup._rw(Path({str(db)!r}))\nconn.execute('SELECT value FROM rows WHERE id=1').fetchone()\nconn.close()\n")
    assert select.returncode == 0, select.stderr
    _assert_event_contract(select_events, expect_clean=True)

    delete, delete_events = _collector_child(tmp_path, "collector-delete", f"conn = cleanup._rw(Path({str(db)!r}))\nconn.execute('BEGIN')\nconn.execute('DELETE FROM rows WHERE id=1')\nconn.commit()\nconn.close()\n")
    assert delete.returncode == 0, delete.stderr
    with pytest.raises(AssertionError, match="write event"):
        _assert_event_contract(delete_events, expect_clean=True)

    with sqlite3.connect(db) as raw:
        raw.execute("INSERT INTO rows VALUES (1, 'old')")
    update, update_events = _collector_child(tmp_path, "collector-update", f"conn = cleanup._rw(Path({str(db)!r}))\nconn.execute('BEGIN')\nconn.execute(\"UPDATE rows SET value='new' WHERE id=1\")\nconn.rollback()\nconn.close()\n")
    assert update.returncode == 0, update.stderr
    with pytest.raises(AssertionError, match="write event"):
        _assert_event_contract(update_events, expect_clean=True)
    with sqlite3.connect(db) as raw:
        assert raw.execute("SELECT value FROM rows WHERE id=1").fetchone() == ("old",)

    fts, fts_events = _collector_child(tmp_path, "collector-fts", f"conn = cleanup._rw(Path({str(db)!r}))\nconn.execute('BEGIN')\nrowcount = conn.execute('DELETE FROM rows_fts WHERE rowid=1').rowcount\nassert rowcount == 1\nconn.commit()\nconn.close()\n")
    assert fts.returncode == 0, fts.stderr
    assert any(event.get("source") == "application" and event.get("action") == "DELETE" and event.get("rowcount") == 1 for event in fts_events)
    assert any(event.get("source") == "application" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in fts_events)
    delete_attempt = next(event for event in fts_events if event.get("source") == "application" and event.get("phase") == "attempt" and event.get("action") == "DELETE")
    assert any(
        event.get("source") == "sqlite-trace"
        and event.get("connection_id") == delete_attempt["connection_id"]
        and event.get("operation_id") == delete_attempt["call_id"]
        and event.get("sql", "").startswith("-- DELETE FROM")
        and "rows_fts_" in event["sql"]
        for event in fts_events
    )

    comment, comment_events = _collector_child(tmp_path, "collector-comment", f"conn = cleanup._rw(Path({str(db)!r}))\nconn.execute(\"-- comment\\\\nDELETE FROM rows WHERE id=1\")\n")
    assert comment.returncode == 1
    assert "AssertionError: observation error" in comment.stderr
    assert any(event.get("source") == "application" and event.get("action") == "--" and event.get("phase") == "failure" for event in comment_events)

    for action in ("DELETE", "BEGIN", "COMMIT"):
        callback = next(event for event in delete_events if event.get("source") == "sqlite-trace" and event.get("action") == action)
        with pytest.raises(AssertionError, match="coverage error"):
            _assert_event_contract([event for event in delete_events if not (event.get("source") == "application" and event.get("phase") == "attempt" and event.get("connection_id") == callback["connection_id"] and event.get("call_id") == callback["operation_id"])], expect_clean=False)

    failure, failure_events = _collector_child(tmp_path, "collector-commit-failure", f"with sqlite3.connect({str(db)!r}) as raw:\n    raw.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')\n    raw.execute('CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')\nconn = cleanup._rw(Path({str(db)!r}))\nconn.execute('PRAGMA foreign_keys=ON')\nconn.execute('BEGIN')\nconn.execute('INSERT INTO child VALUES (1)')\ntry:\n    conn.commit()\nexcept sqlite3.IntegrityError:\n    conn.rollback()\nfinally:\n    conn.close()\n")
    assert failure.returncode == 0, failure.stderr
    assert any(event.get("source") == "application" and event.get("phase") == "failure" and event.get("action") == "COMMIT" and event.get("error") == "IntegrityError" for event in failure_events)
    assert any(event.get("source") == "application" and event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in failure_events)


def test_registry_observer_parser_and_virtual_table_controls(tmp_path: Path):
    def event(*, action: int | str, arg1: str | None = None, arg2: str | None = None, kind: str = "authorizer", raw: str | None = None) -> dict:
        return {"source": "observer-unit", "kind": kind, "action": action, "arg1": arg1, "arg2": arg2, "raw": raw}

    baseline = [
        event(action=sqlite3.SQLITE_PRAGMA, arg1="query_only"),
        event(action=sqlite3.SQLITE_PRAGMA, arg1="table_info", arg2="ontology_nodes"),
        event(action=sqlite3.SQLITE_PRAGMA, arg1="foreign_key_list", arg2="ontology_edges"),
        event(action="SELECT", kind="trace"),
    ]
    _assert_observer_events(baseline, production=False)
    query_only_arg = [*baseline]
    query_only_arg[0] = event(action=sqlite3.SQLITE_PRAGMA, arg1="query_only", arg2="ON")
    invalid_name = [*baseline]
    invalid_name[0] = event(action=sqlite3.SQLITE_PRAGMA, arg1="writable_schema")
    invalid_table = [*baseline]
    invalid_table[1] = event(action=sqlite3.SQLITE_PRAGMA, arg1="table_info", arg2="other")
    controls = [
        ("query_only argument", query_only_arg),
        ("pragma name", invalid_name),
        ("pragma table", invalid_table),
        ("pragma event missing", []),
        ("forbidden action", [*baseline, event(action=sqlite3.SQLITE_CREATE_VTABLE)]),
        ("forbidden action", [*baseline, event(action=sqlite3.SQLITE_DROP_VTABLE)]),
    ]
    for reason, invalid in controls:
        with pytest.raises(AssertionError) as excinfo:
            _assert_observer_events(invalid, production=False)
        assert str(excinfo.value) == reason
        _assert_observer_events(baseline, production=False)
    candidates = [{"snapshot": {
        "registry_node": [{"id": 1, "space": "evidence", "node_type": "Evidence", "node_id": "evidence:turn:heartbeat", "created_at": "t", "updated_at": "u"}],
        "registry_edges": [{"id": 2, "from_space": "evidence", "from_id": "evidence:turn:heartbeat", "relation": "supports", "to_space": "claim", "to_id": "claim:keep", "created_at": "t"}],
    }}]
    allowed_node = (1, "evidence", "Evidence", "evidence:turn:heartbeat", "t", "u")
    allowed_edge = (2, "evidence", "evidence:turn:heartbeat", "supports", "claim", "claim:keep", "t")
    _assert_registry_delete_records([{"sql": _NODE_DELETE_SQL, "params": list(allowed_node)}], candidates)
    _assert_registry_delete_records([{"sql": _EDGE_DELETE_SQL, "params": list(allowed_edge)}], candidates)
    invalid_records = [
        {"sql": "DELETE FROM ontology_nodes WHERE node_id=? AND ? IS NOT NULL AND 0", "params": ["evidence:turn:normal", "evidence:turn:heartbeat"]},
        {"sql": _NODE_DELETE_SQL, "params": [1, "evidence", "Evidence", "evidence:turn:normal", "t", "u"]},
        {"sql": _EDGE_DELETE_SQL, "params": [2, "evidence", "evidence:turn:other", "supports", "claim", "claim:keep", "t"]},
        {"sql": _NODE_DELETE_SQL, "params": [1, "evidence", "Evidence", "evidence:turn:heartbeat", "changed", "u"]},
        {"sql": _NODE_DELETE_SQL, "params": [1, "Evidence", "evidence", "evidence:turn:heartbeat", "t", "u"]},
        {"sql": _NODE_DELETE_SQL, "params": [1, "evidence", "Evidence", "evidence:turn:heartbeat", "t"]},
    ]
    for record in invalid_records:
        with pytest.raises(AssertionError, match="registry delete target"):
            _assert_registry_delete_records([record], candidates)
    db = tmp_path / "observer.db"
    with _db(db) as conn:
        conn.execute("CREATE TABLE ontology_nodes (id INTEGER, space TEXT, node_type TEXT, node_id TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE ontology_edges (id INTEGER, from_space TEXT, from_id TEXT, relation TEXT, to_space TEXT, to_id TEXT, created_at TEXT)")
        conn.execute("INSERT INTO ontology_nodes VALUES (?,?,?,?,?,?)", allowed_node)
        conn.execute("INSERT INTO ontology_nodes VALUES (?,?,?,?,?,?)", (3, "evidence", "Evidence", "evidence:turn:normal", "t", "u"))
        conn.execute("INSERT INTO ontology_edges VALUES (?,?,?,?,?,?,?)", allowed_edge)
        before_nodes = conn.execute("SELECT * FROM ontology_nodes ORDER BY id").fetchall()
        before_edges = conn.execute("SELECT * FROM ontology_edges ORDER BY id").fetchall()
        conn.commit()
        captured: list[dict[str, object]] = []
        original_delete = cleanup._registry_delete
        def observe(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
            captured.append({"sql": sql, "params": list(params)})
            return original_delete(conn, sql, params)
        cleanup._registry_delete = observe
        try:
            conn.execute("BEGIN")
            cursor = cleanup._registry_delete(conn, "DELETE FROM ontology_nodes WHERE node_id=? AND ? IS NOT NULL AND 0", ("evidence:turn:normal", "evidence:turn:heartbeat"))
            conn.rollback()
        finally:
            cleanup._registry_delete = original_delete
        assert cursor.rowcount == 0
        assert conn.execute("SELECT * FROM ontology_nodes ORDER BY id").fetchall() == before_nodes
        assert conn.execute("SELECT * FROM ontology_edges ORDER BY id").fetchall() == before_edges
    with pytest.raises(AssertionError, match="registry delete target"):
        _assert_registry_delete_records(captured, candidates)


def test_cli_registry_foreign_space_id_refuses_before_vector_write(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("resource", "Document", candidate))
    run = tmp_path / "run"
    base = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]
    result = _cli_main(tmp_path, "foreign-space", base)
    assert result.returncode == 0, result.stderr
    assert not (run / "manifest.json").exists()
    assert "registry node multiplicity" in _load(run / "report.json")["errors"][0]


def test_cli_registry_rowcount_failure_rolls_back_registry_transaction(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        before_node = conn.execute("SELECT space,node_type,node_id FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall()
        before_edges = conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall()
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    result = _cli_main(tmp_path, "registry-rowcount", args, "cleanup._registry_rowcount = lambda cursor: 0\n")
    assert result.returncode != 0
    assert "registry node delete rowcount" in result.stderr
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT space,node_type,node_id FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == before_node
        assert conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == before_edges


def test_cli_registry_claim_support_refuses_before_every_candidate_write(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("claim", "Claim", "claim:registry-only"))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "supports", "claim", "claim:registry-only"))
    before = {path.name: path.read_bytes() for path in world.iterdir()}
    run = tmp_path / "run"
    result = cleanup.dry_run(world, run, TABLE)
    assert result["eligible"] is False
    assert not (run / "manifest.json").exists()
    assert any("candidate set removes final registry Claim support" in error for error in _load(run / "report.json")["errors"])
    assert {path.name: path.read_bytes() for path in world.iterdir()} == before


def test_cli_registry_target_resume_completes_missing_edge(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "related", "evidence", normal))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_nodes WHERE space=? AND node_id=?", ("evidence", candidate))
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    result = _cli_main(tmp_path, "registry-resume", args)
    assert result.returncode == 0, result.stderr
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT * FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT * FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == []


def test_cli_registry_edge_rowcount_failure_rolls_back_atomic_axis(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "related", "evidence", normal))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "opencrab.db") as conn:
        before_nodes = conn.execute("SELECT space,node_type,node_id FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall()
        before_edges = conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall()
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    result = _cli_main(tmp_path, "registry-edge-rowcount", args, "cleanup._registry_rowcount = lambda cursor: 0\n")
    assert result.returncode != 0
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT space,node_type,node_id FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == before_nodes
        assert conn.execute("SELECT from_space,from_id,relation,to_space,to_id FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == before_edges


def test_cli_registry_claim_guard_mutation_is_red(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("claim", "Claim", "claim:registry-only"))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "supports", "claim", "claim:registry-only"))
    run = tmp_path / "run"
    original = cleanup._registry_claim_guard
    cleanup._registry_claim_guard = lambda data, candidates, manifest: []
    try:
        result = cleanup.dry_run(world, run, TABLE)
    finally:
        cleanup._registry_claim_guard = original
    assert result["eligible"] is True
    assert (run / "manifest.json").exists()


def test_cli_registry_first_partial_refuses_before_backup(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "related", "evidence", normal))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND relation='related'", (candidate,))
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    result = _cli_main(tmp_path, "registry-first-partial", args)
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists()
    assert not (run / "receipt.json").exists()


def test_cli_registry_target_resume_completes_partial_edge_set(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "related-one", "evidence", normal))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "related-two", "evidence", normal))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND relation='related-one'", (candidate,))
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    result = _cli_main(tmp_path, "registry-edge-resume", args)
    assert result.returncode == 0, result.stderr
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT * FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == []
        assert conn.execute("SELECT * FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == []


def test_cli_registry_foreign_edge_endpoint_refuses_before_vector_write(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "mixed", "resource", candidate))
    run = tmp_path / "run"
    result = _cli_main(tmp_path, "foreign-edge", ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE])
    assert result.returncode == 0, result.stderr
    report = _load(run / "report.json")
    assert "registry edge foreign tuple" in report["errors"][0]
    assert not (run / "manifest.json").exists()


def test_cli_registry_protection_prewrite_source_metadata_refuses(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "doc_store.db") as conn:
        conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id=?", (json.dumps({"pack_id": "other", "node_id": "evidence:turn:normal"}), "evidence:turn:normal"))
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    result = _cli_main(tmp_path, "source-prewrite", args)
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists()
    assert not (run / "receipt.json").exists()


def test_cli_registry_edge_success_node_rowcount_rolls_back(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "related", "evidence", normal))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "opencrab.db") as conn:
        before_nodes = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall()
        before_edges = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall()
    setup = (
        "original = cleanup._registry_rowcount\n"
        "calls = 0\n"
        "def rowcount(cursor):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return 0 if calls == 2 else original(cursor)\n"
        "cleanup._registry_rowcount = rowcount\n"
    )
    args = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]
    result = _cli_main(tmp_path, "edge-node-rowcount", args, setup)
    assert result.returncode != 0
    assert "registry node delete rowcount" in result.stderr
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == before_nodes
        assert conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == before_edges


def _registry_claim_setup(world: Path, *, include_graph_candidate: bool = True, include_registry_candidate: bool = True) -> tuple[str, str, str]:
    candidate = "evidence:turn:heartbeat"
    normal = "evidence:turn:normal"
    claim = "claim:registry-proof"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", claim, "claim", "{}"))
        if include_graph_candidate:
            conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Claim", claim, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", normal, "supports", "Claim", claim, "{}"))
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("claim", "Claim", claim))
        if include_registry_candidate:
            conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "supports", "claim", claim))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "supports", "claim", claim))
    return candidate, normal, claim


def test_cli_registry_claim_production_positive(world: Path, tmp_path: Path):
    candidate, normal, claim = _registry_claim_setup(world)
    before_survivor = _survivor_raw(world, normal, claim)
    assert before_survivor["graph_support"]
    assert before_survivor["registry_support"]
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "doc_store.db") as conn:
        before_source = conn.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (normal,)).fetchall()
        before_fts = conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (normal,)).fetchall()
        before_doc = conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (normal,)).fetchall()
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        before_vector = conn.execute(f"SELECT node_id,pack_id,document,metadata FROM {TABLE} WHERE node_id=?", (normal,)).fetchall()
    with _db(world / "graph.db") as conn:
        before_graph = conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (normal,)).fetchall()
        before_graph_support = conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall()
    with _db(world / "opencrab.db") as conn:
        before_registry = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (normal,)).fetchall()
        before_registry_support = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall()
    trace = tmp_path / "registry-claim-positive.trace"
    deletes = tmp_path / "registry-claim-positive.deletes.jsonl"
    result = _cli_main(tmp_path, "registry-claim-positive", ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)], _trace_setup(trace, deletes))
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines() if json.loads(line).get("phase") == "attempt"]
    _assert_event_contract(events, expect_clean=False)
    _assert_registry_delete_records(delete_events, cleanup._load_run(run)[0])
    edge = next(event for event in delete_events if event["sql"] == _EDGE_DELETE_SQL)
    _assert_registry_delete_records([edge], cleanup._load_run(run)[0])
    for params in ([*edge["params"][:2], "evidence:turn:other", *edge["params"][3:]], [edge["params"][0], edge["params"][2], edge["params"][1], *edge["params"][3:]], edge["params"][:-1]):
        with pytest.raises(AssertionError, match="registry delete target"):
            _assert_registry_delete_records([{"sql": edge["sql"], "params": params}], cleanup._load_run(run)[0])
    _assert_axis_commits(events, {"vectors.db", "graph.db", "doc_store.db", "opencrab.db"})
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT * FROM doc_sources WHERE source_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT * FROM doc_sources_fts WHERE source_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT * FROM doc_nodes WHERE node_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (normal,)).fetchall() == before_source
        assert conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (normal,)).fetchall() == before_fts
        assert conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (normal,)).fetchall() == before_doc
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert conn.execute(f"SELECT * FROM {TABLE} WHERE node_id=?", (candidate,)).fetchall() == []
        assert conn.execute(f"SELECT node_id,pack_id,document,metadata FROM {TABLE} WHERE node_id=?", (normal,)).fetchall() == before_vector
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT * FROM graph_nodes WHERE node_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (normal,)).fetchall() == before_graph
        assert conn.execute("SELECT * FROM graph_edges WHERE from_id=? AND to_id=?", (candidate, claim)).fetchall() == []
        assert conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall() == before_graph_support
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT * FROM ontology_nodes WHERE node_id=?", (candidate,)).fetchall() == []
        assert conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (normal,)).fetchall() == before_registry
        assert conn.execute("SELECT * FROM ontology_edges WHERE from_id=? AND to_id=?", (candidate, claim)).fetchall() == []
        assert conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall() == before_registry_support
    _assert_candidate_absent(world, candidate)
    assert _survivor_raw(world, normal, claim) == before_survivor


@pytest.mark.parametrize("fault", ["missing", "pack"])
def test_cli_registry_claim_frozen_source_selection_refuses(world: Path, tmp_path: Path, fault: str):
    _registry_claim_setup(world)
    normal = "evidence:turn:normal"
    with _db(world / "doc_store.db") as conn:
        if fault == "missing":
            conn.execute("DELETE FROM doc_sources WHERE source_id=?", (normal,))
        else:
            conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id=?", (json.dumps({"pack_id": "other", "node_id": normal}), normal))
    before = {path.name: path.read_bytes() for path in world.iterdir()}
    run = tmp_path / "run"
    result = _cli_main(tmp_path, f"frozen-source-{fault}", ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE])
    assert result.returncode == 0, result.stderr
    assert not (run / "manifest.json").exists()
    report = _load(run / "report.json")
    assert report["last_support_claims"] == ["claim:registry-proof"]
    assert report["last_registry_claims"] == ["claim:registry-proof"]
    assert {path.name: path.read_bytes() for path in world.iterdir()} == before


def test_cli_registry_claim_nonclaim_target_control(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("concept", "Concept", "concept:target"))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "supports", "concept", "concept:target"))
        protected_before = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (normal,)).fetchall()
    before_survivor = _survivor_raw(world, normal)
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    trace = tmp_path / "registry-nonclaim.trace"
    deletes = tmp_path / "registry-nonclaim.deletes.jsonl"
    result = _cli_main(tmp_path, "registry-nonclaim", _registry_apply_args(world, run, backup, approval), _trace_setup(trace, deletes))
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines() if json.loads(line).get("phase") == "attempt"]
    _assert_event_contract(events, expect_clean=False)
    _assert_registry_delete_records(delete_events, cleanup._load_run(run)[0])
    _assert_axis_commits(events, {"vectors.db", "graph.db", "doc_store.db", "opencrab.db"})
    _assert_candidate_absent(world, candidate)
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (normal,)).fetchall() == protected_before
    assert _survivor_raw(world, normal) == before_survivor


@pytest.mark.parametrize("old,new", [
    (
        'all(node in snapshot["registry_node"] for node in nodes)',
        'all(any({key: value for key, value in node.items() if key != "updated_at"} == {key: value for key, value in expected.items() if key != "updated_at"} for expected in snapshot["registry_node"]) for node in nodes)',
    ),
    (
        'all(edge in snapshot["registry_edges"] for edge in edges)',
        'all(any({key: value for key, value in edge.items() if key != "created_at"} == {key: value for key, value in expected.items() if key != "created_at"} for expected in snapshot["registry_edges"]) for edge in edges)',
    ),
])
def test_catalog_membership_mutations_compile(tmp_path: Path, old: str, new: str):
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    mutated = _catalog_mutation(source, old, new)
    copy = tmp_path / "membership_mutation.py"
    copy.write_text(mutated, encoding="utf-8")
    compile(mutated, str(copy), "exec")


_BRANCH_ROWS = [
    (
        "source-existence",
        'if len(source) != 1 or graph_node is None or len(registry_nodes) != 1 or not isinstance(frozen_source, list) or not isinstance(frozen_registry, list):\n        return False',
        'if graph_node is None or len(registry_nodes) != 1 or not isinstance(frozen_source, list) or not isinstance(frozen_registry, list):\n        return False',
    ),
    (
        "frozen-pack",
        'and _strict_pack(frozen_source[0][2], f"Frozen Claim source {source_id}") == _SELECTION.pack_id',
        'and True',
    ),
    (
        "current-pack",
        'and _strict_pack(source[0][2], f"Claim source {source_id}") == _SELECTION.pack_id',
        'and True',
    ),
    (
        "projected-claim",
        'if _claim_failures(conn, registry, doc, candidates, manifest, axis="graph", planned_edges=planned_edges, planned_nodes=planned_nodes):\n            raise CleanupRefusedError("planned graph delete removes final Claim support")',
        'if False:\n            raise CleanupRefusedError("planned graph delete removes final Claim support")',
    ),
    (
        "registry-claim",
        'if _claim_failures(graph, conn, doc, candidates, manifest, axis="registry"):\n            raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
        'if False:\n            raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
    ),
    (
        "doc-raw",
        'if _doc_sections(doc, protected.get("evidence_ids", []), doc_nodes=False)["evidence_source"] != sections.get("evidence_source"):\n            raise CleanupRefusedError("registry protection conflict")',
        'if False:\n            raise CleanupRefusedError("registry protection conflict")',
    ),
]


@pytest.mark.parametrize("name,old,new", _BRANCH_ROWS, ids=_legacy_param_ids(_BRANCH_ROWS))
def test_catalog_branch_mutations_compile(tmp_path: Path, name: str, old: str, new: str):
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    mutated = _catalog_mutation(source, old, new)
    copy = tmp_path / f"{name}.py"
    copy.write_text(mutated, encoding="utf-8")
    compile(mutated, str(copy), "exec")


def test_catalog_source_existence_copy_records_empty_source_failure(tmp_path: Path):
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    old = 'if len(source) != 1 or graph_node is None or len(registry_nodes) != 1 or not isinstance(frozen_source, list) or not isinstance(frozen_registry, list):\n        return False'
    new = 'if graph_node is None or len(registry_nodes) != 1 or not isinstance(frozen_source, list) or not isinstance(frozen_registry, list):\n        return False'
    assert source.count(old) == 1
    def setup(root: Path, normal: str, snapshot: Path) -> str:
        return ("import sqlite3\n"
            "original_guard = cleanup._claim_guard\n"
            "def guard(data, candidates, manifest):\n"
            f"    with sqlite3.connect({str(root / 'doc_store.db')!r}) as conn: conn.execute(\"DELETE FROM doc_sources WHERE source_id=?\", ({normal!r},))\n"
            "    state = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
            f"    open({str(snapshot)!r}, 'w', encoding='utf-8').write(json.dumps(state))\n"
            "    return original_guard(data, candidates, manifest)\n"
            "cleanup._claim_guard = guard\n")
    b_root, b_run, b_backup, b_approval, b_candidates, _b_manifest, b_normal = _fresh_registry_claim_fixture(tmp_path, "source-existence-baseline")
    m_root, m_run, m_backup, m_approval, m_candidates, _m_manifest, m_normal = _fresh_registry_claim_fixture(tmp_path, "source-existence-mutant")
    assert [r["snapshot"] for r in b_candidates] == [r["snapshot"] for r in m_candidates]
    b_snapshot, m_snapshot = tmp_path / "source-baseline.json", tmp_path / "source-mutant.json"
    baseline, baseline_events = _run_copy_apply(b_root, tmp_path, b_run, b_backup, b_approval, source, "source-existence-baseline-copy", setup(b_root, b_normal, b_snapshot))
    assert baseline.returncode == 2
    assert "Claim support changed after frozen manifest" in baseline.stderr
    _assert_event_contract(baseline_events, expect_clean=True)
    assert _all_db_bytes(b_root) == _load(b_snapshot)
    mutant = _catalog_mutation(source, old, new)
    result, events = _run_copy_apply(m_root, tmp_path, m_run, m_backup, m_approval, mutant, "source-existence-mutant-copy", setup(m_root, m_normal, m_snapshot))
    assert result.returncode == 1
    assert "IndexError: list index out of range" in result.stderr
    _assert_event_contract(events, expect_clean=True)
    assert _all_db_bytes(m_root) == _load(m_snapshot)


@pytest.mark.parametrize("branch,old,new", [
    ("frozen", 'and _strict_pack(frozen_source[0][2], f"Frozen Claim source {source_id}") == _SELECTION.pack_id', 'and True'),
    ("current", 'and _strict_pack(source[0][2], f"Claim source {source_id}") == _SELECTION.pack_id', 'and True'),
])
def test_catalog_source_pack_copy_reaches_later_guard(tmp_path: Path, branch: str, old: str, new: str):
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    equality = 'and [_json_value(row) for row in source] == frozen_source'
    assert source.count(old) == source.count(equality) == 1
    observed = source.replace(old, 'and observe_pack(lambda: ' + old.removeprefix('and ') + ')').replace(equality, 'and observe_equality(lambda: [_json_value(row) for row in source] == frozen_source)')
    def setup(root: Path, normal: str, marker: Path, snapshot: Path) -> str:
        if branch == "frozen":
            change = ("    store = manifest.evidence_store()\n    real_get = store.get\n"
                "    def mutated_get(key):\n        item = real_get(key)\n"
                "        if item is not None and key == " + repr(normal) + ": item['source_raw'][0][2] = json.dumps({'pack_id': 'other', 'node_id': " + repr(normal) + "})\n"
                "        return item\n    store.get = mutated_get\n")
        else:
            change = f"    with sqlite3.connect({str(root / 'doc_store.db')!r}) as conn: conn.execute(\"UPDATE doc_sources SET metadata=? WHERE source_id=?\", (json.dumps({{'pack_id':'other','node_id':{normal!r}}}), {normal!r}))\n"
        return ("import sqlite3\noriginal_guard = cleanup._claim_guard\n"
            "def record(kind, value):\n"
            f"    open({str(marker)!r}, 'a', encoding='utf-8').write(json.dumps({{'kind': kind, 'value': value}}) + '\\n')\n"
            "def observe_pack(call):\n    value = call(); record('pack', value); return value\n"
            "def observe_equality(call):\n    value = call(); record('equality', value); return value\n"
            "def observe_bypass(call):\n    value = call(); record('bypass', value); return value\n"
            "cleanup.observe_pack = observe_pack\ncleanup.observe_equality = observe_equality\ncleanup.observe_bypass = observe_bypass\n"
            "def guard(data, candidates, manifest):\n" + change +
            "    state = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
            f"    open({str(snapshot)!r}, 'w', encoding='utf-8').write(json.dumps(state))\n"
            "    return original_guard(data, candidates, manifest)\ncleanup._claim_guard = guard\n")
    b_root, b_run, b_backup, b_approval, b_candidates, _b_manifest, b_normal = _fresh_registry_claim_fixture(tmp_path, f"pack-{branch}-baseline")
    m_root, m_run, m_backup, m_approval, m_candidates, _m_manifest, m_normal = _fresh_registry_claim_fixture(tmp_path, f"pack-{branch}-mutant")
    assert [r["snapshot"] for r in b_candidates] == [r["snapshot"] for r in m_candidates]
    b_marker, m_marker = tmp_path / f"pack-{branch}-baseline.marker", tmp_path / f"pack-{branch}-mutant.marker"
    b_snapshot, m_snapshot = tmp_path / f"pack-{branch}-baseline.raw", tmp_path / f"pack-{branch}-mutant.raw"
    baseline, baseline_events = _run_copy_apply(b_root, tmp_path, b_run, b_backup, b_approval, observed, f"pack-{branch}-baseline-copy", setup(b_root, b_normal, b_marker, b_snapshot))
    assert baseline.returncode == 2
    assert "Claim support changed after frozen manifest" in baseline.stderr
    assert json.loads(b_marker.read_text().splitlines()[0]) == {"kind": "pack", "value": False}
    _assert_event_contract(baseline_events, expect_clean=True)
    assert _all_db_bytes(b_root) == _load(b_snapshot)
    mutant = _catalog_mutation(observed, 'and observe_pack(lambda: ' + old.removeprefix('and ') + ')', 'and observe_bypass(lambda: True)')
    result, events = _run_copy_apply(m_root, tmp_path, m_run, m_backup, m_approval, mutant, f"pack-{branch}-mutant-copy", setup(m_root, m_normal, m_marker, m_snapshot))
    assert result.returncode == 2
    assert "Claim support changed after frozen manifest" in result.stderr
    marks = [json.loads(line) for line in m_marker.read_text().splitlines()]
    assert marks == [{"kind": "bypass", "value": True}, {"kind": "equality", "value": False}]
    _assert_event_contract(events, expect_clean=True)
    assert _all_db_bytes(m_root) == _load(m_snapshot)


_PROOF_ROWS = [
    (
        "projected",
        'if _claim_failures(conn, registry, doc, candidates, manifest, axis="graph", planned_edges=planned_edges, planned_nodes=planned_nodes):\n            raise CleanupRefusedError("planned graph delete removes final Claim support")',
        'if False:\n            raise CleanupRefusedError("planned graph delete removes final Claim support")',
    ),
    (
        "registry-proof",
        'if _claim_failures(graph, conn, doc, candidates, manifest, axis="registry"):\n            raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
        'if False:\n            raise CleanupRefusedError("registry Claim support changed after frozen manifest")',
    ),
    (
        "doc-raw",
        'if _doc_sections(doc, protected.get("evidence_ids", []), doc_nodes=False)["evidence_source"] != sections.get("evidence_source"):\n            raise CleanupRefusedError("registry protection conflict")',
        'if False:\n            raise CleanupRefusedError("registry protection conflict")',
    ),
]


@pytest.mark.parametrize("name,old,new", _PROOF_ROWS, ids=_legacy_param_ids(_PROOF_ROWS))
def test_catalog_proof_copy_runs_fresh_main(world: Path, tmp_path: Path, name: str, old: str, new: str):
    _candidate, normal, _claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    mutated = _catalog_mutation(source, old, new)
    result, events = _run_copy_apply(world, tmp_path, run, backup, approval, mutated, f"proof-{name}")
    assert result.returncode == 0, result.stderr
    assert events
    _assert_event_contract(events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in events)


@pytest.mark.parametrize("target,old,new", [
    (
        "node",
        'all(node in snapshot["registry_node"] for node in nodes)',
        'all(any({key: value for key, value in node.items() if key != "updated_at"} == {key: value for key, value in expected.items() if key != "updated_at"} for expected in snapshot["registry_node"]) for node in nodes)',
    ),
    (
        "edge",
        'all(edge in snapshot["registry_edges"] for edge in edges)',
        'all(any({key: value for key, value in edge.items() if key != "created_at"} == {key: value for key, value in expected.items() if key != "created_at"} for expected in snapshot["registry_edges"]) for edge in edges)',
    ),
])
def test_catalog_membership_copy_runs_fresh_main(world: Path, tmp_path: Path, target: str, old: str, new: str):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    if target == "edge":
        with _db(world / "opencrab.db") as conn:
            conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "recorded", "evidence", normal))
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    if target == "node":
        with _db(world / "opencrab.db") as conn:
            conn.execute("UPDATE ontology_nodes SET updated_at='changed' WHERE node_id=?", (candidate,))
    else:
        with _db(world / "opencrab.db") as conn:
            conn.execute("UPDATE ontology_edges SET created_at='changed' WHERE from_id=? AND relation='recorded'", (candidate,))
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    mutated = _catalog_mutation(source, old, new)
    result, events = _run_copy_apply(world, tmp_path, run, backup, approval, mutated, f"membership-{target}")
    assert result.returncode != 0
    assert "registry resume precheck conflict" in result.stderr
    assert not any(event.get("db") == "opencrab.db" and event.get("sql", "").strip().upper() == "BEGIN IMMEDIATE" for event in events)


def test_catalog_frozen_pack_copy_loses_target_guard(world: Path, tmp_path: Path):
    _candidate, normal, _claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    old = 'and _strict_pack(frozen_source[0][2], f"Frozen Claim source {source_id}") == _SELECTION.pack_id'
    mutated = _catalog_mutation(source, old, "and True")
    snapshot = tmp_path / "frozen-pack.snapshot.json"
    injection = (
        "original_guard = cleanup._claim_guard\n"
        "def guard(data, candidates, manifest):\n"
        "    store = manifest.evidence_store()\n    real_get = store.get\n"
        "    def mutated_get(key):\n        item = real_get(key)\n"
        "        if item is not None and key == " + repr(normal) + ": item['source_raw'][0][2] = json.dumps({'pack_id': 'other', 'node_id': " + repr(normal) + "})\n"
        "        return item\n    store.get = mutated_get\n"
        "    state = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
        "    open(" + repr(str(snapshot)) + ", 'w', encoding='utf-8').write(json.dumps(state))\n"
        "    return original_guard(data, candidates, manifest)\n"
        "cleanup._claim_guard = guard\n"
    )
    result, events = _run_copy_apply(world, tmp_path, run, backup, approval, mutated, "frozen-pack-copy", injection)
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr
    assert _all_db_bytes(world) == _load(snapshot)
    assert not any(event.get("source") == "application" and event.get("phase") == "attempt" and event.get("action") in {"INSERT", "UPDATE", "DELETE"} for event in events)


def test_catalog_current_pack_copy_loses_target_guard(world: Path, tmp_path: Path):
    _candidate, normal, _claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    old = 'and _strict_pack(source[0][2], f"Claim source {source_id}") == _SELECTION.pack_id'
    mutated = _catalog_mutation(source, old, "and True")
    snapshot = tmp_path / "current-pack.snapshot.json"
    injection = (
        "import sqlite3\n"
        "original_guard = cleanup._claim_guard\n"
        "def guard(data, candidates, manifest):\n"
        f"    with sqlite3.connect({str(world / 'doc_store.db')!r}) as conn: conn.execute(\"UPDATE doc_sources SET metadata=? WHERE source_id=?\", (json.dumps({{'pack_id':'other','node_id':{normal!r}}}), {normal!r}))\n"
        "    state = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
        f"    open({str(snapshot)!r}, 'w', encoding='utf-8').write(json.dumps(state))\n"
        "    return original_guard(data, candidates, manifest)\n"
        "cleanup._claim_guard = guard\n"
    )
    result, events = _run_copy_apply(world, tmp_path, run, backup, approval, mutated, "current-pack-copy", injection)
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr
    assert _all_db_bytes(world) == _load(snapshot)
    assert not any(event.get("source") == "application" and event.get("phase") == "attempt" and event.get("action") in {"INSERT", "UPDATE", "DELETE"} for event in events)



def test_catalog_projected_claim_copy_reaches_graph_transaction(world: Path, tmp_path: Path):
    candidate, normal, _claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    old = 'if _claim_failures(conn, registry, doc, candidates, manifest, axis="graph", planned_edges=planned_edges, planned_nodes=planned_nodes):\n            raise CleanupRefusedError("planned graph delete removes final Claim support")'
    mutated = _catalog_mutation(source, old, 'if False:\n            raise CleanupRefusedError("planned graph delete removes final Claim support")')
    snapshot = tmp_path / "projected-copy.snapshot.json"
    injection = (
        "import sqlite3\n"
        "original_projected = cleanup._validate_projected_protection\n"
        "def projected(data, conn, candidates, manifest, planned_edges, planned_nodes):\n"
        f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as fault: fault.execute(\"UPDATE ontology_nodes SET node_type='Document' WHERE space='evidence' AND node_id=?\", ({normal!r},))\n"
        "    state = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
        f"    open({str(snapshot)!r}, 'w', encoding='utf-8').write(json.dumps(state))\n"
        "    return original_projected(data, conn, candidates, manifest, planned_edges, planned_nodes)\n"
        "cleanup._validate_projected_protection = projected\n"
    )
    result, events = _run_copy_apply(world, tmp_path, run, backup, approval, mutated, "projected-copy", injection)
    assert result.returncode != 0
    assert "protected set changed" in result.stderr
    assert not any(event.get("db") == "graph.db" and "BEGIN" in event.get("sql", "").upper() for event in events)
    assert _all_db_bytes(world) != _load(snapshot)


def test_catalog_projected_claim_copy_reaches_graph_transaction_seam(world: Path, tmp_path: Path):
    candidate, normal, _claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    old = 'if _claim_failures(conn, registry, doc, candidates, manifest, axis="graph", planned_edges=planned_edges, planned_nodes=planned_nodes):\n            raise CleanupRefusedError("planned graph delete removes final Claim support")'
    mutated = _catalog_mutation(source, old, 'if False:\n            raise CleanupRefusedError("planned graph delete removes final Claim support")')
    snapshot = tmp_path / "projected-fault-post.rows.json"
    setup = (
        "import sqlite3\n"
        + _logical_rows_setup(snapshot)
        + "original = cleanup._delete_graph\n"
        + "def graph_fault(data, record, candidates, receipt, manifest):\n"
        + f"    with sqlite3.connect(data / 'opencrab.db') as conn: conn.execute(\"UPDATE ontology_nodes SET node_type='Document' WHERE space='evidence' AND node_id=?\", ({normal!r},))\n"
        + "    save_logical_rows(data)\n"
        + "    return original(data, record, candidates, receipt, manifest)\n"
        + "cleanup._delete_graph = graph_fault\n"
    )
    baseline, baseline_events = _run_copy_apply(world, tmp_path, run, backup, approval, source, "projected-baseline", setup)
    assert baseline.returncode == 2
    assert "planned graph delete removes final Claim support" in baseline.stderr
    _assert_event_contract(baseline_events, expect_clean=False)
    assert any(event.get("db") == "graph.db" and event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in baseline_events)
    assert _logical_rows_json(world) == _load(snapshot)

    fresh = tmp_path / "mutated-data"
    fresh.mkdir()
    _schema(fresh)
    _add(fresh, candidate, HEARTBEAT)
    _add(fresh, normal, "discussion includes OpenClaw heartbeat poll later")
    _add(fresh, "evidence:turn:fallback", HEARTBEAT, pack="other")
    _registry_claim_setup(fresh)
    _fix_registry_timestamps(fresh)
    fresh_run, fresh_backup = tmp_path / "mutated-run", tmp_path / "mutated-backup"
    cleanup.dry_run(fresh, fresh_run, TABLE)
    fresh_candidates, fresh_manifest = cleanup._load_run(fresh_run)
    fresh_approval = tmp_path / "mutated-approval.json"
    fresh_approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(fresh_manifest)), "ordered_ids_sha256": fresh_manifest["ordered_ids_sha256"]}), encoding="utf-8")
    cleanup._backup_receipt(fresh, fresh_run, fresh_backup, fresh_manifest, fresh_candidates)
    mutated_result, mutated_events = _run_copy_apply(fresh, tmp_path, fresh_run, fresh_backup, fresh_approval, mutated, "projected-mutated", setup.replace(str(world), str(fresh)))
    graph_events = [event for event in mutated_events if event.get("db") == "graph.db"]
    _assert_event_contract(mutated_events, expect_clean=False)
    assert any(event.get("source") == "application" and event.get("phase") == "attempt" and event.get("action") == "DELETE" for event in graph_events)
    assert any(event.get("source") == "application" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in graph_events)
    assert "planned graph delete removes final Claim support" not in mutated_result.stderr
    assert mutated_result.returncode == 2
    assert "protected set changed" in mutated_result.stderr
    fault_rows = _load(snapshot)
    expected = {name: [row for row in rows if not ((name == "graph" and row[1] == candidate) or (name == "graph_edges" and (row[1] == candidate or row[4] == candidate)))] for name, rows in fault_rows.items()}
    assert _logical_rows_json(fresh) == expected


def test_catalog_registry_claim_copy_reaches_transaction_seam(tmp_path: Path):
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    guard = 'if _claim_failures(graph, conn, doc, candidates, manifest, axis="registry"):\n            raise CleanupRefusedError("registry Claim support changed after frozen manifest")'
    head = '    try:\n'
    tail = '        if _claim_failures(graph, conn, doc, candidates, manifest, axis="registry"):\n'
    marker = head + tail
    assert source.count(guard) == source.count(marker) == 1
    def copied(root: Path, normal: str) -> str:
        fault = f'        conn.execute("UPDATE ontology_nodes SET node_type=\'Document\' WHERE space=\'evidence\' AND node_id=?", ({normal!r},))\n'
        result = source.replace(marker, head + "        _test_capture_registry('pre', conn)\n" + fault + "        _test_capture_registry('post', conn)\n" + tail)
        assert result.count("_test_capture_registry('pre', conn)") == result.count("_test_capture_registry('post', conn)") == 1
        compile(result, "registry-copy.py", "exec")
        return result
    def capture_setup(path: Path) -> str:
        return (
            f"registry_capture_path = {str(path)!r}\n"
            "def capture_registry(name, conn):\n"
            "    state = json.loads(open(registry_capture_path, 'r', encoding='utf-8').read()) if Path(registry_capture_path).exists() else {}\n"
            "    state[name] = {'registry': [list(row) for row in conn.execute('SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id').fetchall()], 'registry_edges': [list(row) for row in conn.execute('SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id').fetchall()]}\n"
            "    open(registry_capture_path, 'w', encoding='utf-8').write(json.dumps(state, sort_keys=True))\n"
            "cleanup._test_capture_registry = capture_registry\n"
        )
    def logical_with_registry(rows: dict[str, list[tuple]], capture: dict) -> dict[str, list[object]]:
        expected = {name: [*values] for name, values in rows.items()}
        expected['registry'], expected['registry_edges'] = capture['registry'], capture['registry_edges']
        return _logical_json_rows(expected)
    b_root, b_run, b_backup, b_approval, _b_candidates, _b_manifest, b_normal = _fresh_registry_claim_fixture(tmp_path, "registry-proof-baseline", incident_edges=True)
    m_root, m_run, m_backup, m_approval, m_candidates, _m_manifest, m_normal = _fresh_registry_claim_fixture(tmp_path, "registry-proof-mutant", incident_edges=True)
    from copy import deepcopy
    pre_rows = deepcopy(_all_db_rows(b_root))
    assert pre_rows == _all_db_rows(m_root)
    prior_axes_rows = _expected_after_prior_axes(deepcopy(pre_rows), "evidence:turn:heartbeat")
    b_capture, m_capture = tmp_path / "registry-baseline.capture.json", tmp_path / "registry-mutant.capture.json"
    baseline, baseline_events = _run_copy_apply(b_root, tmp_path, b_run, b_backup, b_approval, copied(b_root, b_normal), "registry-proof-baseline-copy", capture_setup(b_capture))
    assert baseline.returncode == 2
    assert "registry Claim support changed after frozen manifest" in baseline.stderr
    _assert_event_contract(baseline_events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in baseline_events)
    b_states = _load(b_capture)
    prior_axes_expected = _logical_json_rows(prior_axes_rows)
    assert logical_with_registry(prior_axes_rows, b_states['pre']) == prior_axes_expected
    assert _logical_rows_json(b_root) == prior_axes_expected
    assert b_states['post']['registry'] != b_states['pre']['registry']
    mutant = _catalog_mutation(copied(m_root, m_normal), guard, 'if False:\n            raise CleanupRefusedError("registry Claim support changed after frozen manifest")')
    result, events = _run_copy_apply(m_root, tmp_path, m_run, m_backup, m_approval, mutant, "registry-proof-mutant-copy", capture_setup(m_capture))
    assert result.returncode == 2
    assert "protected set changed" in result.stderr
    _assert_event_contract(events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in events)
    records = [{"sql": e["sql"], "params": e["params"]} for e in events if e.get("source") == "application" and e.get("phase") == "attempt" and e.get("action") == "DELETE" and e.get("db") == "opencrab.db"]
    _assert_registry_delete_records(records, m_candidates)
    m_states = _load(m_capture)
    fault_rows = logical_with_registry(prior_axes_rows, m_states['post'])
    expected_rows = _expected_registry_delete({name: [tuple(row) for row in rows] for name, rows in fault_rows.items()}, "evidence:turn:heartbeat")
    assert any(row[3] == "unrelated" for row in expected_rows["registry_edges"])
    assert not any(row[3] in {"incoming", "self"} for row in expected_rows["registry_edges"])
    expected = _logical_json_rows(expected_rows)
    assert _logical_rows_json(m_root) == expected
    before_corruption = _logical_rows_json(m_root)
    normal = "evidence:turn:normal"
    with _db(m_root / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        replacement = sqlite_vec.serialize_float32([1.0, 0.0])
        assert conn.execute(f"UPDATE {TABLE} SET embedding=? WHERE node_id=?", (replacement, normal)).rowcount == 1
        assert conn.execute(f"SELECT embedding FROM {TABLE} WHERE node_id=?", (normal,)).fetchone() == (replacement,)
    after_corruption = _logical_rows_json(m_root)
    assert {name: rows for name, rows in after_corruption.items() if name != "vector"} == {name: rows for name, rows in before_corruption.items() if name != "vector"}
    changed = [(before, after) for before, after in zip(before_corruption["vector"], after_corruption["vector"]) if before != after]
    assert len(changed) == 1
    assert changed[0][0][0] == changed[0][1][0] == normal
    assert changed[0][0][1] == changed[0][1][1]
    assert changed[0][0][2] != changed[0][1][2]
    assert changed[0][0][3:] == changed[0][1][3:]
    with pytest.raises(AssertionError):
        assert _logical_rows_json(m_root) == expected


def test_catalog_doc_raw_copy_reaches_digest_seam(tmp_path: Path):
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    guard = 'if _doc_sections(doc, protected.get("evidence_ids", []), doc_nodes=False)["evidence_source"] != sections.get("evidence_source"):\n            raise CleanupRefusedError("registry protection conflict")'
    marker = '    doc = _ro(data_dir / "doc_store.db", True)\n    try:\n        if _doc_sections(doc, protected.get("evidence_ids", []), doc_nodes=False)["evidence_source"] != sections.get("evidence_source"):\n'
    assert source.count(guard) == source.count(marker) == 1
    def copied(root: Path, normal: str) -> str:
        fault = f'    with sqlite3.connect({str(root / "doc_store.db")!r}) as fault:\n        fault.execute("UPDATE doc_sources SET metadata=? WHERE source_id=?", (json.dumps({{"pack_id": "other", "node_id": {normal!r}}}), {normal!r}))\n    _test_capture_logical(data_dir)\n'
        result = source.replace(marker, fault + marker)
        assert result.count("_test_capture_logical(data_dir)") == 1
        compile(result, "docraw-copy.py", "exec")
        return result
    def capture_setup(path: Path) -> str:
        return (
            "import sqlite_vec\n"
            f"logical_capture_path = {str(path)!r}\n"
            "def capture_value(item):\n"
            "    if isinstance(item, bytes): return {'bytes': item.hex()}\n"
            "    if isinstance(item, tuple): return [capture_value(part) for part in item]\n"
            "    return item\n"
            "def capture_logical(data):\n"
            "    with sqlite3.connect(data / 'doc_store.db') as conn:\n"
            "        source_rows = conn.execute('SELECT source_id,text,metadata FROM doc_sources ORDER BY source_id').fetchall(); fts = conn.execute('SELECT source_id,text FROM doc_sources_fts ORDER BY source_id').fetchall(); doc = conn.execute('SELECT space,node_id,node_type,properties FROM doc_nodes ORDER BY space,node_id').fetchall()\n"
            "    with sqlite3.connect(data / 'vectors.db') as conn:\n"
            "        conn.enable_load_extension(True); sqlite_vec.load(conn); vector = conn.execute('SELECT * FROM vectors_test ORDER BY node_id').fetchall()\n"
            "    with sqlite3.connect(data / 'graph.db') as conn:\n"
            "        graph = conn.execute('SELECT node_type,node_id,space_id,properties FROM graph_nodes ORDER BY node_id').fetchall(); graph_edges = conn.execute('SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges ORDER BY from_id,relation,to_type,to_id').fetchall()\n"
            "    with sqlite3.connect(data / 'opencrab.db') as conn:\n"
            "        registry = conn.execute('SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id').fetchall(); registry_edges = conn.execute('SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id').fetchall()\n"
            "    rows = {'source': source_rows, 'fts': fts, 'doc': doc, 'vector': vector, 'graph': graph, 'graph_edges': graph_edges, 'registry': registry, 'registry_edges': registry_edges}\n"
            "    open(logical_capture_path, 'w', encoding='utf-8').write(json.dumps({name: [capture_value(row) for row in values] for name, values in rows.items()}, sort_keys=True))\n"
            "cleanup._test_capture_logical = capture_logical\n"
        )
    b_root, b_run, b_backup, b_approval, _b_candidates, _b_manifest, b_normal = _fresh_registry_claim_fixture(tmp_path, "doc-raw-baseline")
    m_root, m_run, m_backup, m_approval, m_candidates, _m_manifest, m_normal = _fresh_registry_claim_fixture(tmp_path, "doc-raw-mutant")
    pre_rows = _all_db_rows(b_root)
    assert pre_rows == _all_db_rows(m_root)
    b_capture, m_capture = tmp_path / "docraw-baseline-capture.json", tmp_path / "docraw-mutant-capture.json"
    baseline, baseline_events = _run_copy_apply(b_root, tmp_path, b_run, b_backup, b_approval, copied(b_root, b_normal), "doc-raw-baseline-copy", capture_setup(b_capture))
    assert baseline.returncode == 2
    assert "registry protection conflict" in baseline.stderr
    _assert_event_contract(baseline_events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in baseline_events)
    assert _logical_rows_json(b_root) == _load(b_capture)
    mutant = _catalog_mutation(copied(m_root, m_normal), guard, 'if False:\n            raise CleanupRefusedError("registry protection conflict")')
    result, events = _run_copy_apply(m_root, tmp_path, m_run, m_backup, m_approval, mutant, "doc-raw-mutant-copy", capture_setup(m_capture))
    assert result.returncode == 2
    assert "protected set changed" in result.stderr
    _assert_event_contract(events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in events)
    records = [{"sql": e["sql"], "params": e["params"]} for e in events if e.get("source") == "application" and e.get("phase") == "attempt" and e.get("action") == "DELETE" and e.get("db") == "opencrab.db"]
    _assert_registry_delete_records(records, m_candidates)
    fault_rows = _load(m_capture)
    expected = _expected_registry_delete(fault_rows, "evidence:turn:heartbeat")
    assert _logical_rows_json(m_root) == expected


@pytest.mark.parametrize("target,old,new", [
    (
        "node",
        'all(node in snapshot["registry_node"] for node in nodes)',
        'all(any({key: value for key, value in node.items() if key != "updated_at"} == {key: value for key, value in expected.items() if key != "updated_at"} for expected in snapshot["registry_node"]) for node in nodes)',
    ),
    (
        "edge",
        'all(edge in snapshot["registry_edges"] for edge in edges)',
        'all(any({key: value for key, value in edge.items() if key != "created_at"} == {key: value for key, value in expected.items() if key != "created_at"} for expected in snapshot["registry_edges"]) for edge in edges)',
    ),
])
def test_catalog_membership_copy_reaches_registry_seam(tmp_path: Path, target: str, old: str, new: str):
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    candidate = "evidence:turn:heartbeat"
    precheck = "        _registry_resume_precheck(data_dir, vector_table, candidates)"
    begin = "        _begin_write(conn)\n        scope = _scope_for(candidates, manifest)"
    assert source.count(precheck) == source.count(begin) == 1
    fault = (
        f"conn.execute(\"UPDATE ontology_nodes SET updated_at='changed' WHERE node_id=?\", ({candidate!r},))"
        if target == "node" else f"conn.execute(\"UPDATE ontology_edges SET created_at='changed' WHERE from_id=? AND relation='recorded'\", ({candidate!r},))"
    )
    def injected() -> str:
        return source.replace(begin, "        _begin_write(conn)\n        " + fault + "\n        scope = _scope_for(candidates, manifest)")
    b_root, b_run, b_backup, b_approval, _b_candidates, _b_manifest, _b_normal = _fresh_registry_claim_fixture(tmp_path, f"membership-{target}-baseline", registry_edge=target == "edge")
    m_root, m_run, m_backup, m_approval, m_candidates, _m_manifest, _m_normal = _fresh_registry_claim_fixture(tmp_path, f"membership-{target}-mutant", registry_edge=target == "edge")
    assert [record["snapshot"] for record in _b_candidates] == [record["snapshot"] for record in m_candidates]
    assert _all_registry_raw(b_root / "opencrab.db") == _all_registry_raw(m_root / "opencrab.db")
    baseline_survivor_before = _survivor_raw(b_root, "evidence:turn:normal", "claim:registry-proof")
    mutant_survivor_before = _survivor_raw(m_root, "evidence:turn:normal", "claim:registry-proof")
    assert baseline_survivor_before == mutant_survivor_before
    assert baseline_survivor_before["graph_support"]
    assert baseline_survivor_before["registry_support"]
    baseline_source = injected().replace(precheck, "        # membership baseline bypasses receipt precheck")
    pre_begin = _all_registry_raw(b_root / "opencrab.db")
    baseline, baseline_events = _run_copy_apply(b_root, tmp_path, b_run, b_backup, b_approval, baseline_source, f"membership-{target}-baseline-copy")
    assert baseline.returncode == 2
    assert "registry target membership conflict" in baseline.stderr
    _assert_event_contract(baseline_events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in baseline_events)
    assert _all_registry_raw(b_root / "opencrab.db") == pre_begin
    assert _survivor_raw(b_root, "evidence:turn:normal", "claim:registry-proof") == baseline_survivor_before
    mutant = _catalog_mutation(injected().replace(precheck, "        # membership mutant bypasses receipt precheck"), old, new)
    result, events = _run_copy_apply(m_root, tmp_path, m_run, m_backup, m_approval, mutant, f"membership-{target}-mutant-copy")
    assert result.returncode == 0, result.stderr
    _assert_event_contract(events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in events)
    records = [{"sql": event["sql"], "params": event["params"]} for event in events if event.get("source") == "application" and event.get("phase") == "attempt" and event.get("action") == "DELETE" and event.get("db") == "opencrab.db"]
    with pytest.raises(AssertionError, match="registry delete target"):
        _assert_registry_delete_records(records, m_candidates)
    _assert_candidate_absent(m_root, candidate)
    assert _survivor_raw(m_root, "evidence:turn:normal", "claim:registry-proof") == mutant_survivor_before


def test_valid_claim_source_empty_source_raises_without_fabrication(world: Path):
    _candidate, normal, _claim = _registry_claim_setup(world)
    candidates, detail = cleanup.select(world, TABLE)
    frozen = next(item for item in detail["protected"]["evidence"] if item["graph"]["id"] == normal)
    with _db(world / "doc_store.db") as conn:
        conn.execute("DELETE FROM doc_sources WHERE source_id=?", (normal,))
    graph = cleanup._ro(world / "graph.db", True)
    registry = cleanup._ro(world / "opencrab.db", True)
    doc = cleanup._ro(world / "doc_store.db", True)
    try:
        assert cleanup._valid_claim_source(graph, registry, doc, frozen, normal) is False
    finally:
        graph.close()
        registry.close()
        doc.close()


def test_cli_registry_claim_last_support_refuses(world: Path, tmp_path: Path):
    candidate, normal, claim = _registry_claim_setup(world)
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    setup = (
        "original = cleanup._claim_guard\n"
        "def claim_guard(data, candidates, manifest):\n"
        "    result = original(data, candidates, manifest)\n"
        "    import sqlite3\n"
        f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"DELETE FROM ontology_edges WHERE from_id=? AND to_id=?\", ({normal!r}, {claim!r}))\n"
        "    return result\n"
        "cleanup._claim_guard = claim_guard\n"
    )
    result = _cli_main(tmp_path, "registry-claim-last", ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)], setup)
    assert result.returncode != 0
    assert "registry Claim support changed" in result.stderr


def _registry_apply_args(world: Path, run: Path, backup: Path, approval: Path) -> list[str]:
    return ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]


def _registry_manifest(world: Path, tmp_path: Path) -> tuple[Path, Path, Path, list[dict], dict]:
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    return run, backup, approval, candidates, manifest


def _fix_registry_timestamps(root: Path) -> None:
    with _db(root / "opencrab.db") as conn:
        conn.execute("UPDATE ontology_nodes SET created_at='fixture-time', updated_at='fixture-time'")
        conn.execute("UPDATE ontology_edges SET created_at='fixture-time'")


def _fresh_registry_claim_fixture(tmp_path: Path, name: str, *, registry_edge: bool = False, incident_edges: bool = False) -> tuple[Path, Path, Path, Path, list[dict], dict, str]:
    root = tmp_path / name / "data"
    root.mkdir(parents=True)
    _schema(root)
    candidate = "evidence:turn:heartbeat"
    _add(root, candidate, HEARTBEAT)
    _add(root, "evidence:turn:normal", "discussion includes OpenClaw heartbeat poll later")
    _add(root, "evidence:turn:fallback", HEARTBEAT, pack="other")
    _candidate, normal, _claim = _registry_claim_setup(root)
    if registry_edge or incident_edges:
        with _db(root / "opencrab.db") as conn:
            if registry_edge:
                conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "recorded", "evidence", normal))
            if incident_edges:
                conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "incoming", "evidence", candidate))
                conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "self", "evidence", candidate))
                conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "unrelated", "evidence", "evidence:turn:fallback"))
    _fix_registry_timestamps(root)
    run, backup, approval, candidates, manifest = _registry_manifest(root, tmp_path / name)
    cleanup._backup_receipt(root, run, backup, manifest, candidates)
    return root, run, backup, approval, candidates, manifest, normal


def test_cli_registry_first_partial_node_refuses_before_backup(world: Path, tmp_path: Path):
    run, backup, approval, candidates, _ = _registry_manifest(world, tmp_path)
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_nodes WHERE space=? AND node_id=?", ("evidence", candidates[0]["id"]))
    result = _cli_main(tmp_path, "registry-first-node", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists() and not (run / "receipt.json").exists()


def test_cli_registry_foreign_node_refuses_before_vector_write(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("resource", "Document", candidate))
    run = tmp_path / "run"
    result = _cli_main(tmp_path, "registry-foreign-node", ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE])
    assert result.returncode == 0, result.stderr
    assert "registry node multiplicity" in _load(run / "report.json")["errors"][0]


def test_cli_registry_mixed_tuple_edge_refuses_before_vector_write(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "mixed", "resource", candidate))
    run = tmp_path / "run"
    result = _cli_main(tmp_path, "registry-mixed-tuple", ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE])
    assert result.returncode == 0, result.stderr
    assert "registry edge foreign tuple" in _load(run / "report.json")["errors"][0]


def test_cli_registry_resume_precheck_removal_reaches_allowed_guard(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "approved-partial", "evidence", normal))
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND relation='approved-partial'", (candidate,))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "late", "evidence", normal))
    trace = tmp_path / "resume-baseline.trace"
    deletes = tmp_path / "resume-baseline.deletes.jsonl"
    before = _all_db_bytes(world)
    baseline = _cli_main(tmp_path, "resume-precheck-baseline", _registry_apply_args(world, run, backup, approval), _trace_setup(trace, deletes))
    assert baseline.returncode == 2
    assert "registry resume precheck conflict" in baseline.stderr
    baseline_events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    _assert_event_contract(baseline_events, expect_clean=True)
    assert _all_db_bytes(world) == before
    assert deletes.read_text(encoding="utf-8") == ""
    trace = tmp_path / "resume-precheck.trace"
    deletes = tmp_path / "resume-precheck.deletes.jsonl"
    setup = _trace_setup(trace, deletes) + "cleanup._registry_resume_precheck = lambda *args: None\n"
    result = _cli_main(tmp_path, "resume-precheck-loss", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode == 2
    assert "snapshot conflict before vector" in result.stderr
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    _assert_event_contract(events, expect_clean=True)
    assert _all_db_bytes(world) == before
    assert deletes.read_text(encoding="utf-8") == ""


def test_catalog_resume_joint_copy_preserves_post_allowed(tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    old = 'if not _before_axis_allowed(current, record["snapshot"], done, record, axis):\n                raise CleanupRefusedError(f"{record[\'id\']}: snapshot conflict before {axis}")'
    selected = repr(candidate)
    new = 'if not (record["id"] == ' + selected + ' and axis == "vector") and not _before_axis_allowed(current, record["snapshot"], done, record, axis):\n                raise CleanupRefusedError(f"{record[\'id\']}: snapshot conflict before {axis}")'
    precheck_old = "        _registry_resume_precheck(data_dir, vector_table, candidates)"
    assert source.count(precheck_old) == source.count(old) == 1
    def setup_root(name: str):
        root = tmp_path / name / "data"
        root.mkdir(parents=True)
        _schema(root)
        _add(root, candidate, HEARTBEAT)
        _add(root, normal, "discussion includes OpenClaw heartbeat poll later")
        _add(root, "evidence:turn:fallback", HEARTBEAT, pack="other")
        with _db(root / "opencrab.db") as conn:
            conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "approved-partial", "evidence", normal))
        _fix_registry_timestamps(root)
        run, backup = tmp_path / name / "run", tmp_path / name / "backup"
        cleanup.dry_run(root, run, TABLE)
        candidates, manifest = cleanup._load_run(run)
        approval = tmp_path / name / "approval.json"
        approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
        cleanup._backup_receipt(root, run, backup, manifest, candidates)
        with _db(root / "opencrab.db") as conn:
            conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND relation='approved-partial'", (candidate,))
            conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "joint-added", "evidence", normal))
        return root, run, backup, approval, candidates
    b_root, b_run, b_backup, b_approval, _b_candidates = setup_root("resume-joint-baseline")
    m_root, m_run, m_backup, m_approval, _m_candidates = setup_root("resume-joint-mutant")
    fault_rows_baseline = _all_db_rows(b_root)
    fault_rows_mutant = _all_db_rows(m_root)
    assert _b_candidates[0]["id"] == _m_candidates[0]["id"] == candidate
    assert [row[:4] for row in fault_rows_baseline["registry"]] == [row[:4] for row in fault_rows_mutant["registry"]]
    assert [row[1:6] for row in fault_rows_baseline["registry_edges"]] == [row[1:6] for row in fault_rows_mutant["registry_edges"]]
    baseline, baseline_events = _run_copy_apply(b_root, tmp_path, b_run, b_backup, b_approval, source, "resume-joint-baseline-copy")
    assert baseline.returncode == 2
    assert "registry resume precheck conflict" in baseline.stderr
    _assert_event_contract(baseline_events, expect_clean=True)
    assert _all_db_rows(b_root) == fault_rows_baseline
    mutated = _catalog_mutation(source.replace(precheck_old, "        # catalog joint probe bypasses precheck"), old, new)
    result, events = _run_copy_apply(m_root, tmp_path, m_run, m_backup, m_approval, mutated, "resume-joint-mutant-copy")
    assert result.returncode == 2
    assert "snapshot conflict after vector" in result.stderr
    _assert_event_contract(events, expect_clean=False)
    vector_events = [event for event in events if event.get("db") == "vectors.db"]
    registry_events = [event for event in events if event.get("db") == "opencrab.db"]
    assert any(event.get("source") == "application" and event.get("phase") == "attempt" and event.get("action") == "DELETE" for event in vector_events)
    assert any(event.get("source") == "application" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in vector_events)
    assert not any(event.get("source") == "application" and event.get("phase") == "attempt" and event.get("action") == "DELETE" for event in registry_events)
    expected_rows = {name: [row for row in rows if not (name == "vector" and row[0] == candidate)] for name, rows in fault_rows_mutant.items()}
    assert _all_db_rows(m_root) == expected_rows


def test_cli_registry_resume_added_edge_refuses_before_vector(world: Path, tmp_path: Path):
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    candidate, normal = candidates[0]["id"], "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "added", "evidence", normal))
    result = _cli_main(tmp_path, "registry-resume-added", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "registry resume precheck conflict" in result.stderr


def test_cli_registry_resume_changed_edge_refuses_before_vector(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "recorded", "evidence", normal))
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND relation='recorded'", (candidate,))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "changed", "evidence", normal))
    result = _cli_main(tmp_path, "registry-resume-changed", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "registry resume precheck conflict" in result.stderr


def test_cli_registry_resume_source_owner_refuses_before_vector(world: Path, tmp_path: Path):
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    candidate = candidates[0]["id"]
    with _db(world / "doc_store.db") as conn:
        conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id=?", (json.dumps({"pack_id": "other", "node_id": candidate}), candidate))
    result = _cli_main(tmp_path, "registry-resume-source-owner", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "registry resume precheck conflict" in result.stderr


def test_cli_registry_resume_graph_owner_refuses_before_vector(world: Path, tmp_path: Path):
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    candidate = candidates[0]["id"]
    with _db(world / "graph.db") as conn:
        conn.execute("UPDATE graph_nodes SET properties=? WHERE node_id=?", (json.dumps({"pack_id": "other"}), candidate))
    result = _cli_main(tmp_path, "registry-resume-graph-owner", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "registry resume precheck conflict" in result.stderr


def test_cli_registry_claim_graph_only_control(world: Path, tmp_path: Path):
    candidate, normal, claim = _registry_claim_setup(world, include_registry_candidate=False)
    before_survivor = _survivor_raw(world, normal, claim)
    assert before_survivor["graph_support"]
    assert before_survivor["registry_support"]
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    trace = tmp_path / "registry-graph-only.trace"
    deletes = tmp_path / "registry-graph-only.deletes.jsonl"
    result = _cli_main(tmp_path, "registry-graph-only", _registry_apply_args(world, run, backup, approval), _trace_setup(trace, deletes))
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    _assert_event_contract(events, expect_clean=False)
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines() if json.loads(line).get("phase") == "attempt"]
    _assert_registry_delete_records(delete_events, cleanup._load_run(run)[0])
    _assert_axis_commits(events, {"vectors.db", "graph.db", "doc_store.db", "opencrab.db"})
    _assert_candidate_absent(world, candidate)
    assert _survivor_raw(world, normal, claim) == before_survivor
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT * FROM graph_edges WHERE from_id=? AND to_id=?", (candidate, claim)).fetchall() == []
        assert conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (normal,)).fetchall() == [("Evidence", normal, "evidence", json.dumps({"pack_id": "openclaw"}))]


def test_cli_registry_claim_registry_only_control(world: Path, tmp_path: Path):
    candidate, normal, claim = _registry_claim_setup(world, include_graph_candidate=False)
    before_survivor = _survivor_raw(world, normal, claim)
    assert before_survivor["graph_support"]
    assert before_survivor["registry_support"]
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    trace = tmp_path / "registry-registry-only.trace"
    deletes = tmp_path / "registry-registry-only.deletes.jsonl"
    result = _cli_main(tmp_path, "registry-registry-only", _registry_apply_args(world, run, backup, approval), _trace_setup(trace, deletes))
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines() if json.loads(line).get("phase") == "attempt"]
    _assert_event_contract(events, expect_clean=False)
    _assert_registry_delete_records(delete_events, cleanup._load_run(run)[0])
    _assert_axis_commits(events, {"vectors.db", "graph.db", "doc_store.db", "opencrab.db"})
    _assert_candidate_absent(world, candidate)
    assert _survivor_raw(world, normal, claim) == before_survivor
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT * FROM ontology_edges WHERE from_id=? AND to_id=?", (candidate, claim)).fetchall() == []
        assert conn.execute("SELECT space,node_type,node_id FROM ontology_nodes WHERE node_id=?", (normal,)).fetchall() == [("evidence", "Evidence", normal)]


def _registry_claim_source_fault(world: Path, tmp_path: Path, mutation: str) -> subprocess.CompletedProcess[str]:
    _candidate, normal, _claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    if mutation == "type":
        body = f"with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"UPDATE ontology_nodes SET node_type='Document' WHERE space='evidence' AND node_id=?\", ({normal!r},))"
    elif mutation == "space":
        body = f"with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"UPDATE ontology_nodes SET space='resource' WHERE node_id=?\", ({normal!r},))"
    elif mutation == "source-pack":
        body = f"with sqlite3.connect({str(world / 'doc_store.db')!r}) as conn: conn.execute(\"UPDATE doc_sources SET metadata=? WHERE source_id=?\", (json.dumps({{'pack_id':'other','node_id':{normal!r}}}), {normal!r}))"
    elif mutation == "graph-pack":
        body = f"with sqlite3.connect({str(world / 'graph.db')!r}) as conn: conn.execute(\"UPDATE graph_nodes SET properties=? WHERE node_id=?\", (json.dumps({{'pack_id':'other'}}), {normal!r}))"
    elif mutation == "dangling":
        body = f"with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"DELETE FROM ontology_nodes WHERE space='evidence' AND node_id=?\", ({normal!r},))"
    else:
        raise AssertionError(mutation)
    trace = tmp_path / f"registry-claim-{mutation}.trace"
    deletes = tmp_path / f"registry-claim-{mutation}.delete.jsonl"
    snapshot = tmp_path / f"registry-claim-{mutation}.snapshot.json"
    setup = (
        _trace_setup(trace, deletes)
        + f"snapshot_path = {str(snapshot)!r}\n"
        + "original = cleanup._claim_guard\n"
        "def claim_guard(data, candidates, manifest):\n"
        f"    {body}\n"
        "    snapshot = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
        "    open(snapshot_path, 'w', encoding='utf-8').write(json.dumps(snapshot))\n"
        "    return original(data, candidates, manifest)\n"
        "cleanup._claim_guard = claim_guard\n"
    )
    before_nodes, before_edges = _all_registry_raw(world / "opencrab.db")
    result = _cli_main(tmp_path, f"registry-claim-{mutation}", _registry_apply_args(world, run, backup, approval), setup)
    trace_events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines()]
    assert delete_events == []
    _assert_event_contract(trace_events, expect_clean=True)
    injection_snapshot = _load(snapshot)
    assert _all_db_bytes(world) == injection_snapshot
    result.trace_path = trace  # type: ignore[attr-defined]
    result.delete_path = deletes  # type: ignore[attr-defined]
    result.before_registry = (before_nodes, before_edges)  # type: ignore[attr-defined]
    result.snapshot_path = snapshot  # type: ignore[attr-defined]
    return result


@pytest.mark.parametrize("mutation", ["type", "space", "source-pack", "graph-pack"])
def test_cli_registry_claim_source_faults_refuse(world: Path, tmp_path: Path, mutation: str):
    result = _registry_claim_source_fault(world, tmp_path, mutation)
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr
    delete_events = [json.loads(line) for line in result.delete_path.read_text(encoding="utf-8").splitlines()]  # type: ignore[attr-defined]
    trace_events = [json.loads(line) for line in result.trace_path.read_text(encoding="utf-8").splitlines()]  # type: ignore[attr-defined]
    assert delete_events == []
    _assert_event_contract(trace_events, expect_clean=True)
    snapshot = _load(result.snapshot_path)  # type: ignore[attr-defined]
    assert _all_db_bytes(world) == snapshot


def test_cli_registry_protection_prewrite_node_refuses(world: Path, tmp_path: Path):
    run, backup, approval, _candidates, _ = _registry_manifest(world, tmp_path)
    with _db(world / "opencrab.db") as conn:
        conn.execute("UPDATE ontology_nodes SET updated_at='changed' WHERE node_id=?", ("evidence:turn:normal",))
    result = _cli_main(tmp_path, "registry-protect-node", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists()


def test_cli_registry_protection_prewrite_edge_refuses(world: Path, tmp_path: Path):
    normal = "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "noncandidate", "evidence", "missing"))
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    with _db(world / "opencrab.db") as conn:
        conn.execute("UPDATE ontology_edges SET relation='changed' WHERE from_id=? AND relation='noncandidate'", (normal,))
    result = _cli_main(tmp_path, "registry-protect-edge", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists()


def test_cli_registry_node_timestamp_cas_probe(world: Path, tmp_path: Path):
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    candidate = "evidence:turn:heartbeat"
    setup = (
        "original = cleanup._delete_registry\n"
        "def mutate(data, record, candidates, manifest):\n"
        "    import sqlite3\n"
        f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"UPDATE ontology_nodes SET updated_at='changed' WHERE node_id=?\", ({candidate!r},))\n"
        "    return original(data, record, candidates, manifest)\n"
        "cleanup._delete_registry = mutate\n"
    )
    result = _cli_main(tmp_path, "registry-node-cas", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "registry target membership conflict" in result.stderr


def test_cli_registry_edge_timestamp_cas_probe(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "recorded", "evidence", normal))
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    setup = (
        "original = cleanup._delete_registry\n"
        "def mutate(data, record, candidates, manifest):\n"
        "    import sqlite3\n"
        f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"UPDATE ontology_edges SET created_at='changed' WHERE from_id=? AND relation='recorded'\", ({candidate!r},))\n"
        "    return original(data, record, candidates, manifest)\n"
        "cleanup._delete_registry = mutate\n"
    )
    result = _cli_main(tmp_path, "registry-edge-cas", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "registry target membership conflict" in result.stderr


@pytest.mark.parametrize("target", ["node", "edge"])
def test_timestamp_predicate_copy_reaches_observer_guard(tmp_path: Path, target: str):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    if target == "node":
        sql = "AND created_at IS ? AND updated_at IS ?"
        params = '(node["id"], node["space"], node["node_type"], node["node_id"], node["created_at"], node["updated_at"])'
        mutated_sql = "AND created_at IS ?"
        mutated_params = '(node["id"], node["space"], node["node_type"], node["node_id"], node["created_at"])'
    else:
        sql = "AND to_space=? AND to_id=? AND created_at IS ?"
        params = '(edge["id"], edge["from_space"], edge["from_id"], edge["relation"], edge["to_space"], edge["to_id"], edge["created_at"])'
        mutated_sql = "AND to_space=? AND to_id=?"
        mutated_params = '(edge["id"], edge["from_space"], edge["from_id"], edge["relation"], edge["to_space"], edge["to_id"])'
    source = (Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py").read_text(encoding="utf-8")
    assert source.count(sql) == source.count(params) == 1
    def fresh(name: str):
        root = tmp_path / name / "data"
        root.mkdir(parents=True)
        _schema(root)
        _add(root, candidate, HEARTBEAT)
        _add(root, normal, "discussion includes OpenClaw heartbeat poll later")
        _add(root, "evidence:turn:fallback", HEARTBEAT, pack="other")
        claim = "claim:predicate-proof"
        with _db(root / "graph.db") as conn:
            conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", claim, "claim", "{}"))
            conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", normal, "supports", "Claim", claim, "{}"))
            conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Claim", claim, "supports", "Evidence", normal, "{}"))
        with _db(root / "opencrab.db") as conn:
            conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("claim", "Claim", claim))
            conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "supports", "claim", claim))
            conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("claim", claim, "supports", "evidence", normal))
        if target == "edge":
            with _db(root / "opencrab.db") as conn:
                conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "recorded", "evidence", normal))
        _fix_registry_timestamps(root)
        run, backup = tmp_path / name / "run", tmp_path / name / "backup"
        cleanup.dry_run(root, run, TABLE)
        candidates, manifest = cleanup._load_run(run)
        approval = tmp_path / name / "approval.json"
        approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
        cleanup._backup_receipt(root, run, backup, manifest, candidates)
        return root, run, backup, approval, candidates
    b_root, b_run, b_backup, b_approval, b_candidates = fresh("predicate-baseline")
    m_root, m_run, m_backup, m_approval, m_candidates = fresh("predicate-mutant")
    assert [r["id"] for r in b_candidates] == [r["id"] for r in m_candidates] == [candidate]
    assert [{key: value for key, value in r["snapshot"].items() if key not in {"registry_node", "registry_edges"}} for r in b_candidates] == [{key: value for key, value in r["snapshot"].items() if key not in {"registry_node", "registry_edges"}} for r in m_candidates]
    baseline_survivor_before = _survivor_raw(b_root, normal, "claim:predicate-proof")
    mutant_survivor_before = _survivor_raw(m_root, normal, "claim:predicate-proof")
    assert baseline_survivor_before == mutant_survivor_before
    assert {row[0:5] for row in baseline_survivor_before["graph_support"]} == {("Evidence", normal, "supports", "Claim", "claim:predicate-proof"), ("Claim", "claim:predicate-proof", "supports", "Evidence", normal)}
    assert {(row[1], row[2], row[3], row[4], row[5]) for row in baseline_survivor_before["registry_support"]} == {("evidence", normal, "supports", "claim", "claim:predicate-proof"), ("claim", "claim:predicate-proof", "supports", "evidence", normal)}
    baseline, baseline_events = _run_copy_apply(b_root, tmp_path, b_run, b_backup, b_approval, source, f"predicate-{target}-baseline")
    assert baseline.returncode == 0, baseline.stderr
    _assert_event_contract(baseline_events, expect_clean=False)
    baseline_records = [{"sql": e["sql"], "params": e["params"]} for e in baseline_events if e.get("source") == "application" and e.get("phase") == "attempt" and e.get("action") == "DELETE" and e.get("db") == "opencrab.db"]
    _assert_registry_delete_records(baseline_records, b_candidates)
    assert any(e.get("db") == "opencrab.db" and e.get("phase") == "transaction" and e.get("action") == "COMMIT" for e in baseline_events)
    assert _survivor_raw(b_root, normal, "claim:predicate-proof") == baseline_survivor_before
    _assert_candidate_absent(b_root, candidate)
    mutated = source.replace(sql, mutated_sql).replace(params, mutated_params)
    marker = tmp_path / f"predicate-{target}.marker.jsonl"
    injection = ("original_match = cleanup._registry_targets_match\n"
        "def match(nodes, edges, snapshot):\n"
        "    value = original_match(nodes, edges, snapshot)\n"
        f"    open({str(marker)!r}, 'a', encoding='utf-8').write(json.dumps({{'membership': value}}) + '\\n')\n"
        "    return value\ncleanup._registry_targets_match = match\n")
    result, events = _run_copy_apply(m_root, tmp_path, m_run, m_backup, m_approval, mutated, f"predicate-{target}-mutant", injection)
    assert result.returncode == 0, result.stderr
    assert any(event["membership"] is True for event in (json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()))
    _assert_event_contract(events, expect_clean=False)
    records = [{"sql": e["sql"], "params": e["params"]} for e in events if e.get("source") == "application" and e.get("phase") == "attempt" and e.get("action") == "DELETE" and e.get("db") == "opencrab.db"]
    assert any(record["sql"].endswith(mutated_sql) for record in records)
    with pytest.raises(AssertionError, match="registry delete target"):
        _assert_registry_delete_records(records, m_candidates)
    assert any(e.get("db") == "opencrab.db" and e.get("phase") == "transaction" and e.get("action") == "COMMIT" for e in events)
    _assert_candidate_absent(m_root, candidate)
    assert _survivor_raw(m_root, normal, "claim:predicate-proof") == mutant_survivor_before


def test_cli_registry_claim_resume_positive(world: Path, tmp_path: Path):
    candidate, normal, claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND to_id=?", (candidate, claim))
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND to_id=?", (candidate, claim))
    with _db(world / "doc_store.db") as conn:
        survivor_source = conn.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (normal,)).fetchall()
        survivor_fts = conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (normal,)).fetchall()
        survivor_doc = conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (normal,)).fetchall()
    with _db(world / "graph.db") as conn:
        survivor_graph = conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (normal,)).fetchall()
        survivor_graph_support = conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall()
    with _db(world / "opencrab.db") as conn:
        survivor_registry = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (normal,)).fetchall()
        survivor_registry_support = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall()
    trace = tmp_path / "resume.trace"
    deletes = tmp_path / "resume-delete.jsonl"
    absent_edge = next(
        edge for record in candidates for edge in record["snapshot"]["registry_edges"]
        if edge["from_id"] == candidate and edge["to_id"] == claim
    )
    setup = _trace_setup(trace, deletes)
    result = _cli_main(tmp_path, "registry-claim-resume-positive", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode == 0, result.stderr
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines() if json.loads(line).get("phase") == "attempt"]
    trace_events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    absent_params = (absent_edge["id"], absent_edge["from_space"], absent_edge["from_id"], absent_edge["relation"], absent_edge["to_space"], absent_edge["to_id"], absent_edge["created_at"])
    assert all(tuple(event["params"]) != absent_params for event in delete_events)
    _assert_registry_delete_records(delete_events, candidates)
    canonical = next(event for event in delete_events if event["sql"] == _NODE_DELETE_SQL)
    _assert_registry_delete_records([canonical], candidates)
    field_changed = {**canonical, "params": [*canonical["params"]]}
    field_changed["params"][3] = "evidence:turn:other"
    ordered = {**canonical, "params": [*canonical["params"]]}
    ordered["params"][1], ordered["params"][2] = ordered["params"][2], ordered["params"][1]
    short = {**canonical, "params": canonical["params"][:-1]}
    for record in (field_changed, ordered, short):
        with pytest.raises(AssertionError, match="registry delete target"):
            _assert_registry_delete_records([record], candidates)
    _assert_event_contract(trace_events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in trace_events)
    _assert_candidate_absent(world, candidate)
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT source_id,text,metadata FROM doc_sources WHERE source_id=?", (normal,)).fetchall() == survivor_source
        assert conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (normal,)).fetchall() == survivor_fts
        assert conn.execute("SELECT space,node_id,node_type,properties FROM doc_nodes WHERE node_id=?", (normal,)).fetchall() == survivor_doc
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (normal,)).fetchall() == survivor_graph
        assert conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall() == survivor_graph_support
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT * FROM ontology_edges WHERE from_id=? AND to_id=?", (candidate, claim)).fetchall() == []
        assert conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes WHERE node_id=?", (normal,)).fetchall() == survivor_registry
        assert conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges WHERE from_id=? AND to_id=?", (normal, claim)).fetchall() == survivor_registry_support


def test_cli_registry_claim_resume_negative(world: Path, tmp_path: Path):
    candidate, normal, claim = _registry_claim_setup(world)
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND to_id=?", (candidate, claim))
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND to_id=?", (candidate, claim))
    setup = (
        "original = cleanup._claim_guard\n"
        "def claim_guard(data, candidates, manifest):\n"
        "    result = original(data, candidates, manifest)\n"
        "    import sqlite3\n"
        f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"DELETE FROM ontology_edges WHERE from_id=? AND to_id=?\", ({normal!r}, {claim!r}))\n"
        "    return result\n"
        "cleanup._claim_guard = claim_guard\n"
    )
    result = _cli_main(tmp_path, "registry-claim-resume-negative", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "registry Claim support changed" in result.stderr


def test_cli_registry_claim_dangling_source_refuses(world: Path, tmp_path: Path):
    result = _registry_claim_source_fault(world, tmp_path, "dangling")
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr
    assert result.delete_path.read_text(encoding="utf-8") == ""  # type: ignore[attr-defined]
    assert "BEGIN IMMEDIATE" not in result.trace_path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    snapshot = _load(result.snapshot_path)  # type: ignore[attr-defined]
    assert {name: (world / name).read_bytes().hex() for name in cleanup.DB_FILES} == snapshot


def test_cli_registry_claim_wrong_type_refuses(world: Path, tmp_path: Path):
    result = _registry_claim_source_fault(world, tmp_path, "type")
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr


def test_cli_registry_claim_wrong_space_refuses(world: Path, tmp_path: Path):
    result = _registry_claim_source_fault(world, tmp_path, "space")
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr


def test_cli_registry_claim_source_pack_refuses(world: Path, tmp_path: Path):
    result = _registry_claim_source_fault(world, tmp_path, "source-pack")
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr


def test_cli_registry_claim_graph_pack_refuses(world: Path, tmp_path: Path):
    result = _registry_claim_source_fault(world, tmp_path, "graph-pack")
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr


def test_cli_registry_first_partial_edge_refuses_before_backup(world: Path, tmp_path: Path):
    candidate, normal = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "first-partial", "evidence", normal))
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    with _db(world / "opencrab.db") as conn:
        conn.execute("DELETE FROM ontology_edges WHERE from_id=? AND relation='first-partial'", (candidate,))
    result = _cli_main(tmp_path, "registry-first-edge", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists() and not (run / "receipt.json").exists()


def test_cli_registry_resume_foreign_tuple_refuses_before_vector_write(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "resume-mixed", "resource", candidate))
    result = _cli_main(tmp_path, "registry-resume-foreign", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "registry edge foreign tuple" in result.stderr


def test_cli_registry_source_metadata_first_preflight_refuses(world: Path, tmp_path: Path):
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    with _db(world / "doc_store.db") as conn:
        conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id=?", (json.dumps({"pack_id": "other", "node_id": "evidence:turn:normal"}), "evidence:turn:normal"))
    result = _cli_main(tmp_path, "registry-source-first", _registry_apply_args(world, run, backup, approval))
    assert result.returncode != 0
    assert "first apply requires the full frozen snapshot" in result.stderr
    assert not backup.exists() and not (run / "receipt.json").exists()


def test_cli_registry_protection_transaction_node_refuses(world: Path, tmp_path: Path):
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    normal = "evidence:turn:normal"
    setup = (
        "original = cleanup._delete_registry\n"
        "def mutate(data, record, candidates, manifest):\n"
        "    import sqlite3\n"
        f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"UPDATE ontology_nodes SET updated_at='changed' WHERE node_id=?\", ({normal!r},))\n"
        "    return original(data, record, candidates, manifest)\n"
        "cleanup._delete_registry = mutate\n"
    )
    result = _cli_main(tmp_path, "registry-txn-protect-node", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "registry protection conflict" in result.stderr


def test_cli_registry_protection_transaction_edge_refuses(world: Path, tmp_path: Path):
    normal = "evidence:turn:normal"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", normal, "protected-edge", "evidence", "missing"))
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    setup = (
        "original = cleanup._delete_registry\n"
        "def mutate(data, record, candidates, manifest):\n"
        "    import sqlite3\n"
        f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"UPDATE ontology_edges SET relation='changed' WHERE from_id=? AND relation='protected-edge'\", ({normal!r},))\n"
        "    return original(data, record, candidates, manifest)\n"
        "cleanup._delete_registry = mutate\n"
    )
    result = _cli_main(tmp_path, "registry-txn-protect-edge", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "registry protection conflict" in result.stderr


def test_cli_registry_source_metadata_transaction_refuses(world: Path, tmp_path: Path):
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    normal = "evidence:turn:normal"
    trace = tmp_path / "registry-source-transaction.trace"
    deletes = tmp_path / "registry-source-transaction.delete.jsonl"
    snapshot = tmp_path / "registry-source-transaction.rows.json"
    setup = (
        "import sqlite3\n"
        + _trace_setup(trace, deletes)
        + _logical_rows_setup(snapshot)
        + "original = cleanup._delete_registry\n"
        + "def mutate(data, record, candidates, manifest):\n"
        + f"    with sqlite3.connect({str(world / 'doc_store.db')!r}) as conn: conn.execute(\"UPDATE doc_sources SET metadata=? WHERE source_id=?\", (json.dumps({{'pack_id':'other','node_id':{normal!r}}}), {normal!r}))\n"
        + "    save_logical_rows(data)\n"
        + "    return original(data, record, candidates, manifest)\n"
        + "cleanup._delete_registry = mutate\n"
    )
    with _db(world / "opencrab.db") as conn:
        before_nodes = conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id").fetchall()
        before_edges = conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id").fetchall()
    result = _cli_main(tmp_path, "registry-txn-source", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode == 2
    assert "registry protection conflict" in result.stderr
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines()]
    trace_events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert delete_events == []
    _assert_event_contract(trace_events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("action") == "BEGIN" for event in trace_events)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in trace_events)
    assert not any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in trace_events)
    with _db(world / "opencrab.db") as conn:
        assert conn.execute("SELECT id,space,node_type,node_id,created_at,updated_at FROM ontology_nodes ORDER BY id").fetchall() == before_nodes
        assert conn.execute("SELECT id,from_space,from_id,relation,to_space,to_id,created_at FROM ontology_edges ORDER BY id").fetchall() == before_edges
    assert _logical_rows_json(world) == _load(snapshot)


def test_cli_registry_resume_added_edge_transaction_refuses(world: Path, tmp_path: Path):
    run, backup, approval, candidates, manifest = _registry_manifest(world, tmp_path)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    candidate, normal = candidates[0]["id"], "evidence:turn:normal"
    trace = tmp_path / "registry-added-transaction.trace"
    deletes = tmp_path / "registry-added-transaction.delete.jsonl"
    snapshot = tmp_path / "registry-added-transaction.rows.json"
    setup = (
        "import sqlite3\n"
        + _trace_setup(trace, deletes)
        + _logical_rows_setup(snapshot)
        + "original = cleanup._delete_registry\n"
        + "def mutate(data, record, candidates, manifest):\n"
        + f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)\", ('evidence', {candidate!r}, 'late-added', 'evidence', {normal!r}))\n"
        + "    save_logical_rows(data)\n"
        + "    return original(data, record, candidates, manifest)\n"
        + "cleanup._delete_registry = mutate\n"
    )
    before_nodes, before_edges = _all_registry_raw(world / "opencrab.db")
    result = _cli_main(tmp_path, "registry-added-txn", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "registry target membership conflict" in result.stderr
    delete_events = [json.loads(line) for line in deletes.read_text(encoding="utf-8").splitlines()]
    trace_events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert delete_events == []
    _assert_event_contract(trace_events, expect_clean=False)
    assert any(event.get("db") == "opencrab.db" and event.get("action") == "BEGIN" for event in trace_events)
    assert any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in trace_events)
    assert not any(event.get("db") == "opencrab.db" and event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in trace_events)
    after_nodes, after_edges = _all_registry_raw(world / "opencrab.db")
    assert after_nodes == before_nodes
    assert after_edges == _all_registry_raw(world / "opencrab.db")[1]
    assert _logical_rows_json(world) == _load(snapshot)


def test_cli_registry_claim_post_graph_fault_probe(world: Path, tmp_path: Path):
    candidate, normal, claim = _registry_claim_setup(world)
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    trace = tmp_path / "post-graph.trace"
    deletes = tmp_path / "post-graph.delete.jsonl"
    snapshot = tmp_path / "post-graph.snapshot.json"
    setup = (
        "import sqlite3\n"
        + _trace_setup(trace, deletes)
        + f"snapshot_path = {str(snapshot)!r}\n"
        + "original = cleanup._claim_guard\n"
        + "def guard(data, candidates, manifest):\n"
        + "    result = original(data, candidates, manifest)\n"
        + f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn:\n"
        + f"        conn.execute(\"DELETE FROM ontology_nodes WHERE space='evidence' AND node_id=?\", ({normal!r},))\n"
        + "    snapshot = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
        + "    open(snapshot_path, 'w', encoding='utf-8').write(json.dumps(snapshot))\n"
        + "    return result\n"
        + "cleanup._claim_guard = guard\n"
    )
    result = _cli_main(tmp_path, "registry-post-graph", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "registry Claim support changed" in result.stderr
    assert deletes.read_text(encoding="utf-8") == ""
    assert "BEGIN IMMEDIATE" not in trace.read_text(encoding="utf-8")
    state = _load(snapshot)
    assert _all_db_bytes(world) == state


def test_runbook_states_actual_run_and_successor_contract():
    runbook = (Path(__file__).parent.parent / "docs" / "openclaw-heartbeat-cleanup.md").read_text(encoding="utf-8")
    assert "존재하지 않는 새 run path" in runbook
    assert "비어 있는 기존 directory도 지정하면 안 된다" in runbook
    assert "--run-dir <new-run-dir>" in runbook
    assert "하나의 successor UUID directory" in runbook
    assert "data directory fsync" in runbook
    assert "현재 DB file store-set identity를 다시 계산하지 않는다" in runbook
    assert "승인 후보 registry node와 incident edge" in runbook
    assert "frozen `evidence|Evidence` 후보 endpoint" in runbook
    assert "affected Claim" in runbook
    assert "source와 graph metadata의 `pack_id=openclaw`" in runbook


def test_restore_attempt_names_are_unique(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    source = tmp_path / "backup.db"
    source.write_bytes(b"backup")
    destination = data / "graph.db"
    destination.write_bytes(b"old")
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    first = cleanup._publish_attempt_successor(run, receipt)
    second = cleanup._publish_attempt_successor(run, receipt)
    first_dir = run / "restore-attempts" / first["directory"]
    second_dir = run / "restore-attempts" / second["directory"]
    cleanup._restore_file(source, destination, first_dir, receipt)
    cleanup._restore_file(source, destination, second_dir, receipt)
    assert first["attempt"] != second["attempt"]
    assert _load(first_dir / "graph.db.json")["attempt"] == first["attempt"]
    assert _load(second_dir / "graph.db.json")["attempt"] == second["attempt"]


def test_cli_dry_run_uses_fresh_process(world: Path, tmp_path: Path):
    script = Path(__file__).parent.parent / "scripts" / "cleanup_openclaw_heartbeat_493.py"
    run = tmp_path / "cli-run"
    result = subprocess.run(
        [sys.executable, str(script), "--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert _load(run / "manifest.json")["vector_table"] == TABLE


@pytest.mark.parametrize("row_id,mutation", [
    ("B-FA-ADDED-VAL", "added"),
    ("B-FA-CHANGED-VAL", "changed"),
    ("B-RA-ADDED-VAL", "added"),
    ("B-RA-CHANGED-VAL", "changed"),
])
def test_cli_boundary_mutation_rows_refuse_before_vector_write(world: Path, tmp_path: Path, row_id: str, mutation: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        if mutation == "missing":
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
        elif mutation == "added":
            conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "extra", "Evidence", other, "{}"))
        else:
            conn.execute("UPDATE graph_edges SET properties='{" + '"changed":true' + "}' WHERE from_id=? AND relation='rel'", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    before = (world / "vectors.db").read_bytes()
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0, row_id
    assert (world / "vectors.db").read_bytes() == before


@pytest.mark.parametrize("branch,row_id,mutation", [
    ("FA", "B-FA-ALLOWED-VAL", "allowed"),
    ("FA", "B-FA-ADDED-VAL", "added"),
    ("FA", "B-FA-CHANGED-VAL", "changed"),
    ("RA", "B-RA-ALLOWED-VAL", "allowed"),
    ("RA", "B-RA-ADDED-VAL", "added"),
    ("RA", "B-RA-CHANGED-VAL", "changed"),
])
def test_cli_boundary_validator_rows_use_actual_fa_ra_branches(world: Path, tmp_path: Path, branch: str, row_id: str, mutation: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / f"{branch}-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if branch == "RA":
        cleanup._backup_receipt(world, run, backup, manifest, candidates)
        with _db(world / "graph.db") as conn:
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    assert (run / "receipt.json").exists() is (branch == "RA")
    before = (world / "vectors.db").read_bytes()
    trace = tmp_path / f"{row_id}.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    if mutation == "allowed":
        fa_injection = "        conn.execute(\"DELETE FROM graph_edges WHERE from_id=? AND relation='rel'\", (" + repr(candidate) + ",))\n"
        ra_injection = "    pass\n"
    elif mutation == "added":
        fa_injection = "        conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'extra', 'Evidence', " + repr(other) + ", '{}'))\n"
        ra_injection = "    conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'extra', 'Evidence', " + repr(other) + ", '{}'))\n"
    else:
        fa_injection = "        conn.execute(\"UPDATE graph_edges SET properties=? WHERE from_id=? AND relation='rel'\", (" + repr(json.dumps({"changed": True})) + ", " + repr(candidate) + "))\n"
        ra_injection = "    conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'rel', 'Evidence', " + repr(other) + ", " + repr(json.dumps({"changed": True})) + "))\n"
    if branch == "FA":
        setup = (
            "original_backup = cleanup._backup_receipt\n"
            "def publish_then_inject(data, run, backup, manifest, candidates):\n"
            "    trace.write('branch=first receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    receipt = original_backup(data, run, backup, manifest, candidates)\n"
            "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    with sqlite3.connect(data / 'graph.db') as conn:\n"
            + fa_injection
            + "    return receipt\n"
            "cleanup._backup_receipt = publish_then_inject\n"
        )
    else:
        setup = (
            "trace.write('branch=resume receipt-before=True\\n')\n"
            "def no_republish(*args):\n"
            "    raise AssertionError('unexpected backup receipt publication')\n"
            "cleanup._backup_receipt = no_republish\n"
            "with sqlite3.connect(" + repr(str(world / "graph.db")) + ") as conn:\n"
            + ra_injection
        )
    runner.write_text(
        "import sqlite3\nimport sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\nimport repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        + setup
        + f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\nraise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    statements = _without_witness_pragmas(trace.read_text(encoding="utf-8"))
    assert statements.startswith(f"branch={'first' if branch == 'FA' else 'resume'} receipt-before={str(branch == 'RA')}\n")
    if branch == "FA":
        assert "receipt-after=True" in statements
    if mutation == "allowed":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0, row_id
        assert "boundary edge R/D/S conflict" in result.stderr
        assert (world / "vectors.db").read_bytes() == before


def test_cli_first_apply_publication_then_boundary_validator(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "first-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    trace = tmp_path / "fa.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "fa.py"
    runner.write_text(
        "import sqlite3\nimport sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\nimport repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "original = cleanup._backup_receipt\n"
        "def publish_then_mutate(data, run, backup, manifest, candidates):\n"
        "    trace.write('receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    receipt = original(data, run, backup, manifest, candidates)\n"
        "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"UPDATE graph_edges SET properties='{" + '\\"changed\\":true' + "}' WHERE from_id=? AND relation='rel'\", (" + repr(candidate) + ",))\n"
        "    trace.close()\n"
        "    return receipt\n"
        "cleanup._backup_receipt = publish_then_mutate\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    before = (world / "vectors.db").read_bytes()
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "boundary edge R/D/S conflict" in result.stderr
    assert trace.read_text(encoding="utf-8") == "receipt-before=False\nreceipt-after=True\n"
    assert (world / "vectors.db").read_bytes() == before


@pytest.mark.parametrize("row_id", ["B-FA-ALLOWED-VAL", "B-RA-ALLOWED-VAL"])
def test_cli_boundary_allowed_rows_accept_receipt_bound_absence(world: Path, tmp_path: Path, row_id: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, row_id + result.stderr


def test_cli_boundary_change_refuses_before_vector_write(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("UPDATE graph_edges SET properties='{" + '"changed":true' + "}' WHERE from_id=?", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "boundary.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(tmp_path / 'backup')!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    with _db(world / "vectors.db") as conn:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        assert conn.execute(f"SELECT node_id FROM {TABLE} WHERE node_id=?", (candidate,)).fetchall() == [(candidate,)]


def test_cli_apply_prepare_and_rollback_use_argparse_entrypoints(world: Path, tmp_path: Path):
    script_dir = Path(__file__).parent.parent / "scripts"
    run, backup = tmp_path / "cli-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    runner = tmp_path / "entrypoints.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"base = ['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}]\n"
        f"assert cleanup.main([*base, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]) == 0\n"
        f"assert cleanup.main([*base, '--prepare-rollback', '--backup-dir', {str(backup)!r}]) == 0\n"
        f"assert cleanup.main([*base, '--rollback', '--backup-dir', {str(backup)!r}]) == 0\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


def test_attempt_successor_publishes_directory_after_fsync(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    payload = cleanup._publish_attempt_successor(run, receipt)
    assert (run / f"{cleanup.ATTEMPT_PREFIX}{payload['attempt']}.json").is_file()
    assert (run / "restore-attempts" / payload["directory"]).is_dir()
    assert cleanup._published_attempts(run, receipt)[0]["attempt"] == payload["attempt"]


def test_cli_rollback_metadata_only_preserves_evidence_and_resumes(world: Path, tmp_path: Path):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    candidates, manifest = cleanup._load_run(run)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    successor = cleanup._publish_attempt_successor(run, receipt)
    attempt_dir = run / "restore-attempts" / successor["directory"]
    metadata = cleanup._attempt_metadata(receipt, world, "graph.db", successor["attempt"])
    cleanup._write_fsync(attempt_dir / "graph.db.json", cleanup._canon(metadata), exclusive=True)
    result = _cli_main(tmp_path, "metadata-only", rollback_args)
    assert result.returncode == 0, result.stderr
    assert (attempt_dir / "graph.db.json").read_bytes() == cleanup._canon(metadata)
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


def test_cli_rollback_parent_fsync_failure_preserves_pre_restore_state(world: Path, tmp_path: Path):
    run, _, rollback_args = _cli_rollback_state(world, tmp_path)
    before = {name: (world / name).read_bytes() for name in cleanup.DB_FILES}
    setup = (
        "original = cleanup._fsync_dir\n"
        "def fsync(path):\n"
        "    if path == cleanup.Path(" + repr(str(run)) + ") and (path / 'restore-attempts').exists():\n"
        "        raise OSError('parent fsync fault')\n"
        "    return original(path)\n"
        "cleanup._fsync_dir = fsync\n"
    )
    result = _cli_main(tmp_path, "parent-fsync", rollback_args, setup)
    assert result.returncode != 0
    assert "parent fsync fault" in result.stderr
    assert {name: (world / name).read_bytes() for name in cleanup.DB_FILES} == before
    assert not list(run.glob(f"{cleanup.ATTEMPT_PREFIX}*.json"))
    assert not (run / "rollback-complete.json").exists()


def test_cli_rollback_fault_artifacts_follow_binding_contract(world: Path, tmp_path: Path):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    candidates, manifest = cleanup._load_run(run)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    successor = cleanup._publish_attempt_successor(run, receipt)
    attempt_dir = run / "restore-attempts" / successor["directory"]
    metadata = cleanup._attempt_metadata(receipt, world, "graph.db", successor["attempt"])
    _write = cleanup._write_fsync
    _write(attempt_dir / "graph.db.json", cleanup._canon(metadata), exclusive=True)
    replacement = world / metadata["replacement"]
    replacement.write_bytes(b"partial")
    result = _cli_main(tmp_path, "partial-replacement", rollback_args)
    assert result.returncode == 0, result.stderr
    assert replacement.read_bytes() == b"partial"
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


@pytest.mark.parametrize("fault", ["missing-directory", "unbound-replacement", "unbound-sidecar"])
def test_cli_rollback_rejects_unbound_artifacts(world: Path, tmp_path: Path, fault: str):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    candidates, manifest = cleanup._load_run(run)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    if fault == "missing-directory":
        successor = cleanup._publish_attempt_successor(run, receipt)
        (run / "restore-attempts" / successor["directory"]).rmdir()
    elif fault == "unbound-replacement":
        (world / ".graph.db.restore-unbound").write_bytes(b"partial")
    else:
        (world / "graph.db-wal").write_bytes(b"partial")
    before = {name: (world / name).read_bytes() for name in cleanup.DB_FILES}
    result = _cli_main(tmp_path, fault, rollback_args)
    assert result.returncode != 0
    assert {name: (world / name).read_bytes() for name in cleanup.DB_FILES} == before
    assert not (run / "rollback-complete.json").exists()


def test_cli_completion_verifies_without_restore(world: Path, tmp_path: Path):
    run, backup, rollback_args = _cli_rollback_state(world, tmp_path)
    first = _cli_main(tmp_path, "complete-first", rollback_args)
    assert first.returncode == 0, first.stderr
    trace = tmp_path / "completion-trace.txt"
    setup = (
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "cleanup._restore_file = lambda *args: (_ for _ in ()).throw(AssertionError('restore called'))\n"
        "original = cleanup._fsync_dir\n"
        "def fsync(path):\n"
        "    trace.write(str(path) + '\\n')\n"
        "    trace.flush()\n"
        "    return original(path)\n"
        "cleanup._fsync_dir = fsync\n"
    )
    second = _cli_main(tmp_path, "complete-second", rollback_args, setup)
    assert second.returncode == 0, second.stderr
    assert str(run) in trace.read_text(encoding="utf-8")


def test_published_attempt_missing_directory_fails_closed(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    receipt = {"manifest_sha256": "m", "backup_set_id": "b", "store_set_sha256": "s", "data_dir": cleanup._data_dir_identity(data), "files": {}}
    payload = cleanup._publish_attempt_successor(run, receipt)
    (run / "restore-attempts" / payload["directory"]).rmdir()
    with pytest.raises(cleanup.CleanupRefusedError, match="directory is missing"):
        cleanup._published_attempts(run, receipt)


def test_cli_resume_after_graph_commit_uses_receipt_component_intent(world: Path, tmp_path: Path):
    first, second, third = "evidence:turn:heartbeat", "evidence:turn:second-cli", "evidence:turn:third-cli"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists cli second")
    _add(world, third, "Q: Read HEARTBEAT.md if it exists cli third")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "ab", "Evidence", second, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", second, "bc", "Evidence", third, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    _, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    interrupted = tmp_path / "interrupt.py"
    interrupted.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent / 'scripts')!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        "original = cleanup._delete_graph\n"
        "def stop(data, record, candidates, receipt, manifest):\n"
        "    original(data, record, candidates, receipt, manifest, after_commit=lambda: (_ for _ in ()).throw(SystemExit(77)))\n"
        "cleanup._delete_graph = stop\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    interrupted_result = subprocess.run(
        [sys.executable, str(interrupted)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert interrupted_result.returncode == 77
    runner = tmp_path / "resume.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent / 'scripts')!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT relation FROM graph_edges ORDER BY relation").fetchall() == [("bc",)]
    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id IN (?,?,?)", (first, second, third)).fetchall() == []


@pytest.mark.parametrize("branch,row_id", [("FA", "B-FA-ALLOWED-TX"), ("RA", "B-RA-ALLOWED-TX")])
def test_cli_boundary_allowed_transaction_has_no_redelete_or_dangling(world: Path, tmp_path: Path, branch: str, row_id: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / f"{branch}-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if branch == "RA":
        cleanup._backup_receipt(world, run, backup, manifest, candidates)
        with _db(world / "graph.db") as conn:
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    assert (run / "receipt.json").exists() is (branch == "RA")
    trace = tmp_path / f"{row_id}.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    setup = (
        "original_backup = cleanup._backup_receipt\n"
        "def publish_then_remove(data, run, backup, manifest, candidates):\n"
        "    trace.write('branch=first receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    receipt = original_backup(data, run, backup, manifest, candidates)\n"
        "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"DELETE FROM graph_edges WHERE from_id=? AND relation='rel'\", (" + repr(candidate) + ",))\n"
        "    return receipt\n"
        "cleanup._backup_receipt = publish_then_remove\n"
        if branch == "FA" else
        "trace.write('branch=resume receipt-before=True\\n')\n"
        "def no_republish(*args):\n"
        "    raise AssertionError('unexpected backup receipt publication')\n"
        "cleanup._backup_receipt = no_republish\n"
    )
    runner.write_text(
        "import sqlite3\nimport sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\nimport repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'graph.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        + setup
        + f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\nraise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    statements = _without_witness_pragmas(trace.read_text(encoding="utf-8"))
    assert statements.startswith(f"branch={'first' if branch == 'FA' else 'resume'} receipt-before={str(branch == 'RA')}\n")
    if branch == "FA":
        assert "receipt-after=True" in statements
    assert "DELETE FROM graph_edges" not in statements
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT * FROM graph_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == []


@pytest.mark.parametrize("row_id,mutation", [
    ("B-FA-ADDED-TX", "added"), ("B-FA-CHANGED-TX", "changed"),
    ("B-RA-ADDED-TX", "added"), ("B-RA-CHANGED-TX", "changed"),
])
def test_cli_boundary_transaction_rows_reject_before_graph_delete(world: Path, tmp_path: Path, row_id: str, mutation: str):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    branch = "FA" if "B-FA" in row_id else "RA"
    run, backup = tmp_path / f"{branch}-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    if branch == "RA":
        cleanup._backup_receipt(world, run, backup, manifest, candidates)
        with _db(world / "graph.db") as conn:
            conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    assert (run / "receipt.json").exists() is (branch == "RA")
    trace = tmp_path / f"{row_id}.trace"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / f"{row_id}.py"
    if mutation == "added":
        injection = "        conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'extra', 'Evidence', " + repr(other) + ", '{}'))\n"
    elif branch == "FA":
        injection = "        conn.execute(\"UPDATE graph_edges SET properties=? WHERE from_id=? AND relation='rel'\", (" + repr(json.dumps({"changed": True})) + ", " + repr(candidate) + "))\n"
    else:
        injection = "        conn.execute(\"INSERT INTO graph_edges VALUES (?,?,?,?,?,?)\", ('Evidence', " + repr(candidate) + ", 'rel', 'Evidence', " + repr(other) + ", " + repr(json.dumps({"changed": True})) + "))\n"
    if branch == "FA":
        setup = (
            "original_backup = cleanup._backup_receipt\n"
            "def traced_backup(data, run, backup, manifest, candidates):\n"
            "    trace.write('branch=first receipt-before=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    receipt = original_backup(data, run, backup, manifest, candidates)\n"
            "    trace.write('receipt-after=' + str((run / 'receipt.json').exists()) + '\\n')\n"
            "    return receipt\n"
            "cleanup._backup_receipt = traced_backup\n"
        )
    else:
        setup = (
            "trace.write('branch=resume receipt-before=True\\n')\n"
            "def no_republish(*args):\n"
            "    raise AssertionError('unexpected backup receipt publication')\n"
            "cleanup._backup_receipt = no_republish\n"
        )
    runner.write_text(
        "import sqlite3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'graph.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        + setup
        + "orig = cleanup._delete_graph\n"
        "def mutate(data, record, candidates, receipt, manifest):\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        + injection
        + "    return orig(data, record, candidates, receipt, manifest)\n"
        "cleanup._delete_graph = mutate\n"
        f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0, row_id
    assert "boundary edge R/D/S conflict" in result.stderr
    statements = _without_witness_pragmas(trace.read_text(encoding="utf-8"))
    assert statements.startswith(f"branch={'first' if branch == 'FA' else 'resume'} receipt-before={str(branch == 'RA')}\n")
    if branch == "FA":
        assert "receipt-after=True" in statements
    assert "BEGIN IMMEDIATE" in statements
    assert "DELETE FROM graph_edges" not in statements


def test_cli_projected_claim_proof_refuses_before_graph_delete(world: Path, tmp_path: Path):
    candidate, normal, _claim = _registry_claim_setup(world)
    run, backup, approval, _, _ = _registry_manifest(world, tmp_path)
    with _db(world / "graph.db") as conn:
        before_candidate = conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (candidate,)).fetchall()
        before_edges = conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall()
    trace = tmp_path / "projected-claim.trace"
    deletes = tmp_path / "projected-claim.delete.jsonl"
    snapshot = tmp_path / "projected-claim.snapshot.json"
    setup = (
        "import sqlite3\n"
        + _trace_setup(trace, deletes)
        + f"snapshot_path = {str(snapshot)!r}\n"
        + "original = cleanup._delete_graph\n"
        + "def mutate(data, record, candidates, receipt, manifest):\n"
        + f"    with sqlite3.connect({str(world / 'opencrab.db')!r}) as conn: conn.execute(\"UPDATE ontology_nodes SET node_type='Document' WHERE space='evidence' AND node_id=?\", ({normal!r},))\n"
        + "    state = {name: (data / name).read_bytes().hex() for name in cleanup.DB_FILES}\n"
        + "    open(snapshot_path, 'w', encoding='utf-8').write(json.dumps(state))\n"
        + "    return original(data, record, candidates, receipt, manifest)\n"
        + "cleanup._delete_graph = mutate\n"
    )
    result = _cli_main(tmp_path, "projected-claim", _registry_apply_args(world, run, backup, approval), setup)
    assert result.returncode != 0
    assert "planned graph delete removes final Claim support" in result.stderr
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    graph_events = [event for event in events if event.get("db") == "graph.db"]
    assert not any(event.get("action") == "DELETE" for event in graph_events)
    assert not any(event.get("phase") == "transaction" and event.get("action") == "COMMIT" for event in graph_events)
    assert any(event.get("phase") == "transaction" and event.get("action") == "ROLLBACK" for event in graph_events)
    assert deletes.read_text(encoding="utf-8") == ""
    assert _all_db_bytes(world) == _load(snapshot)
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_type,node_id,space_id,properties FROM graph_nodes WHERE node_id=?", (candidate,)).fetchall() == before_candidate
        assert conn.execute("SELECT from_type,from_id,relation,to_type,to_id,properties FROM graph_edges WHERE from_id=? OR to_id=?", (candidate, candidate)).fetchall() == before_edges


def test_cli_registry_claim_nonclaim_registry_space_control(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("resource", "Claim", "resource:claim"))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", candidate, "supports", "resource", "resource:claim"))
    run = tmp_path / "run"
    result = _cli_main(tmp_path, "resource-claim", ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE])
    assert result.returncode == 0, result.stderr
    assert (run / "manifest.json").is_file()


def test_cli_claim_preflight_refuses_before_vector_delete(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:keep", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "supports", "Claim", "claim:keep", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", other, "supports", "Claim", "claim:keep", "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    trace = tmp_path / "vector-trace.txt"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "claim-preflight.py"
    runner.write_text(
        "import sqlite3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'vectors.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        "orig = cleanup._validate_all_candidate_claims\n"
        "def mutate(data, candidates, manifest):\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"DELETE FROM graph_edges WHERE from_id=? AND relation='supports'\", (" + repr(other) + ",))\n"
        "    return orig(data, candidates, manifest)\n"
        "cleanup._validate_all_candidate_claims = mutate\n"
        f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "Claim support changed after frozen manifest" in result.stderr
    assert "DELETE FROM" not in trace.read_text(encoding="utf-8")


def test_cli_component_transaction_change_rejects_before_graph_delete(world: Path, tmp_path: Path):
    first, second = "evidence:turn:heartbeat", "evidence:turn:component-tx"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists component tx")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "rel", "Evidence", second, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    trace = tmp_path / "graph-trace.txt"
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "component-tx.py"
    runner.write_text(
        "import sqlite3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"trace = open({str(trace)!r}, 'w', encoding='utf-8')\n"
        "orig_rw = cleanup._rw\n"
        "def traced(path):\n"
        "    conn = orig_rw(path)\n"
        "    if path.name == 'graph.db':\n"
        "        conn.set_trace_callback(lambda sql: trace.write(sql + '\\n'))\n"
        "    return conn\n"
        "cleanup._rw = traced\n"
        "orig = cleanup._delete_graph\n"
        "def mutate(data, record, candidates, receipt, manifest):\n"
        "    with sqlite3.connect(data / 'graph.db') as conn:\n"
        "        conn.execute(\"UPDATE graph_edges SET properties='{" + '\\"changed\\":true' + "}' WHERE from_id=? AND to_id=?\", (" + repr(first) + ", " + repr(second) + "))\n"
        "    return orig(data, record, candidates, receipt, manifest)\n"
        "cleanup._delete_graph = mutate\n"
        f"code = cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}])\n"
        "trace.close()\n"
        "raise SystemExit(code)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0
    assert "component edge R/D/S conflict" in result.stderr
    statements = _without_witness_pragmas(trace.read_text(encoding="utf-8"))
    assert "BEGIN IMMEDIATE" in statements
    assert "DELETE FROM graph_edges" not in statements


def test_cli_boundary_allowance_removal_refuses_approved_absence(world: Path, tmp_path: Path):
    candidate, other = "evidence:turn:heartbeat", "evidence:turn:normal"
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", candidate, "rel", "Evidence", other, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_edges WHERE from_id=? AND relation='rel'", (candidate,))
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "allowance-removal.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        "original = cleanup._boundary_edges\n"
        "def no_allowance(candidates, receipt, manifest):\n"
        "    recorded, _ = original(candidates, receipt, manifest)\n"
        "    return recorded, set()\n"
        "cleanup._boundary_edges = no_allowance\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0


def test_cli_initial_intent_publication_removal_refuses(world: Path, tmp_path: Path):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    script_dir = Path(__file__).parent.parent / "scripts"
    runner = tmp_path / "publication-removal.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        "original = cleanup._backup_receipt\n"
        "def incomplete(*args):\n"
        "    receipt = original(*args)\n"
        "    receipt.pop('component_intent')\n"
        "    cleanup._publish(args[1] / 'receipt.json', cleanup._canon(receipt))\n"
        "    return receipt\n"
        "cleanup._backup_receipt = incomplete\n"
        f"raise SystemExit(cleanup.main(['--local-data-dir', {str(world)!r}, '--run-dir', {str(run)!r}, '--vector-table', {TABLE!r}, '--apply', '--approved-manifest', {str(approval)!r}, '--backup-dir', {str(backup)!r}]))\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode != 0


def test_cli_complete_intent_all_callers_accept_identical_receipt(world: Path, tmp_path: Path):
    script_dir = Path(__file__).parent.parent / "scripts"
    run, backup = tmp_path / "complete-run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    base = ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]

    def invoke(name: str, args: list[str]) -> None:
        runner = tmp_path / f"complete-{name}.py"
        runner.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(script_dir)!r})\n"
            "import cleanup_openclaw_heartbeat_493 as cleanup\n"
            "import repair_store_mismatch_499 as repair\n"
            "repair.holders = lambda _: ([], 0)\n"
            f"raise SystemExit(cleanup.main({args!r}))\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert result.returncode == 0, result.stderr

    invoke("apply", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)])
    receipt_path = run / "receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    assert _load(receipt_path)["component_intent"] == cleanup._component_intents(*cleanup._load_run(run))
    for name, args in (
        ("apply-resume", [*base, "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)]),
        ("prepare", [*base, "--prepare-rollback", "--backup-dir", str(backup)]),
        ("rollback", [*base, "--rollback", "--backup-dir", str(backup)]),
    ):
        assert receipt_path.read_bytes() == receipt_bytes
        invoke(name, args)
        assert receipt_path.read_bytes() == receipt_bytes


def test_cli_prepare_then_rollback_entrypoint(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    runner = tmp_path / "rollback.py"
    runner.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent / 'scripts')!r})\n"
        "import cleanup_openclaw_heartbeat_493 as cleanup\n"
        "import repair_store_mismatch_499 as repair\n"
        "repair.holders = lambda _: ([], 0)\n"
        f"cleanup.prepare_rollback(Path({str(world)!r}), Path({str(run)!r}), Path({str(backup)!r}))\n"
        f"cleanup.rollback(Path({str(world)!r}), Path({str(run)!r}), Path({str(backup)!r}))\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    restored = subprocess.run([sys.executable, str(runner)], cwd=tmp_path, text=True, capture_output=True, env=env)
    assert restored.returncode == 0, restored.stderr
    assert cleanup.marker_state(run)[0] == "complete tombstone plus completion"


def test_component_helper_rejects_changed_shared_edge(world: Path, tmp_path: Path):
    first, second = "evidence:turn:heartbeat", "evidence:turn:second-rds"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists rds")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", first, "rel", "Evidence", second, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    backup = tmp_path / "backup"
    receipt = cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    with _db(world / "graph.db") as conn:
        conn.execute("UPDATE graph_edges SET properties='{" + '"changed":true' + "}' WHERE from_id=? AND to_id=?", (first, second))
    conn = sqlite3.connect(world / "graph.db")
    try:
        with pytest.raises(cleanup.CleanupRefusedError, match="component edge R/D/S conflict"):
            cleanup._validate_component_edges(conn, candidates, receipt, manifest)
    finally:
        conn.close()


def test_abc_component_resume_b_transaction_uses_rds(world: Path, tmp_path: Path):
    a, b, c = "evidence:turn:heartbeat", "evidence:turn:abc-b", "evidence:turn:abc-c"
    _add(world, b, "Q: Read HEARTBEAT.md if it exists abc b")
    _add(world, c, "Q: Read HEARTBEAT.md if it exists abc c")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", a, "ab", "Evidence", b, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", b, "bc", "Evidence", c, "{}"))
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    by_id = {item["id"]: item for item in candidates}
    assert {edge["relation"] for edge in by_id[b]["edges"]} == {"ab", "bc"}
    backup = tmp_path / "backup"
    receipt = cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    cleanup._delete_graph(world, by_id[a], candidates, receipt, manifest)
    with _db(world / "graph.db") as conn:
        actual_b = conn.execute("SELECT relation FROM graph_edges WHERE from_id=? OR to_id=?", (b, b)).fetchall()
    assert actual_b == [("bc",)]
    cleanup._delete_graph(world, by_id[b], candidates, receipt, manifest)
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id IN (?,?)", (b, c)).fetchall() == [(c,)]


# ---- #493 후속: 전체 스캔 제거, 보호 집합 스트리밍, vector 신원 ----


def _no_scan(_data: Path) -> tuple[list[int], int]:
    return [], 0


def test_vector_identity_uses_pk_when_metadata_has_no_node_id(tmp_path: Path):
    """운영 writer는 vector metadata에 node_id를 넣지 않는다. 신원은 vec0 PK 열이다."""
    root = tmp_path / "data"
    root.mkdir()
    _schema(root)
    _add(root, "evidence:turn:heartbeat", HEARTBEAT, vector_node_id=False)
    result = cleanup.dry_run(root, tmp_path / "run", TABLE)
    assert result["eligible"] is True
    assert result["candidates"] == 1


def test_vector_identity_refuses_metadata_node_id_that_differs(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    _schema(root)
    _add(root, "evidence:turn:heartbeat", HEARTBEAT, vector_node_id=False)
    _vector_sql(root, f"UPDATE {TABLE} SET metadata=? WHERE node_id=?", (json.dumps({"pack_id": "openclaw", "node_id": "evidence:turn:other"}), "evidence:turn:heartbeat"))
    result = cleanup.dry_run(root, tmp_path / "run", TABLE)
    assert result["eligible"] is False
    assert any("vector identity" in error for error in result["errors"])


def _protection_world(root: Path) -> None:
    root.mkdir()
    _schema(root)
    _add(root, "evidence:turn:heartbeat", HEARTBEAT)
    _add(root, "evidence:turn:normal", "discussion includes OpenClaw heartbeat poll later")
    _add(root, "evidence:turn:extra", "extra", source=False, fts=False, vector=False)
    with _db(root / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Document", "doc:keep", "document", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", "evidence:turn:normal", "cites", "Document", "doc:keep", "{}"))
    with _db(root / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("document", "Document", "doc:keep"))
        conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", "evidence:turn:normal", "cites", "document", "doc:keep"))
    _fix_registry_timestamps(root)


def _vector_sql(root: Path, sql: str, params: tuple) -> None:
    import sqlite_vec

    with _db(root / "vectors.db") as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute(sql, params)


_NORMAL, _EXTRA = "evidence:turn:normal", "evidence:turn:extra"
_VECTOR_INSERT = f"INSERT INTO {TABLE}(node_id,pack_id,embedding,document,metadata) VALUES (?,'openclaw',?,'x','{{}}')"
_PROTECTION_MUTATIONS: dict[tuple[str, str], tuple[str, str, tuple]] = {
    ("nodes", "update"): ("graph.db", "UPDATE graph_nodes SET properties='{\"a\":1}' WHERE node_id='doc:keep'", ()),
    ("nodes", "insert"): ("graph.db", "INSERT INTO graph_nodes VALUES ('Document','doc:new','document','{}')", ()),
    ("nodes", "delete"): ("graph.db", "DELETE FROM graph_nodes WHERE node_id='doc:keep'", ()),
    ("evidence_graph", "update"): ("graph.db", "UPDATE graph_nodes SET properties='{\"pack_id\":\"openclaw\",\"k\":1}' WHERE node_id=?", (_NORMAL,)),
    ("evidence_graph", "insert"): ("graph.db", "INSERT INTO graph_nodes VALUES ('Evidence','evidence:turn:new','evidence','{}')", ()),
    ("evidence_graph", "delete"): ("graph.db", "DELETE FROM graph_nodes WHERE node_id=?", (_NORMAL,)),
    ("edges", "update"): ("graph.db", "UPDATE graph_edges SET properties='{\"w\":1}' WHERE to_id='doc:keep'", ()),
    ("edges", "insert"): ("graph.db", "INSERT INTO graph_edges VALUES ('Evidence',?,'other','Document','doc:keep','{}')", (_NORMAL,)),
    ("edges", "delete"): ("graph.db", "DELETE FROM graph_edges WHERE to_id='doc:keep'", ()),
    ("evidence_doc", "update"): ("doc_store.db", "UPDATE doc_nodes SET properties='{\"pack_id\":\"openclaw\",\"k\":1}' WHERE node_id=?", (_NORMAL,)),
    ("evidence_doc", "insert"): ("doc_store.db", "INSERT INTO doc_nodes VALUES ('concept',?,'Concept','{}')", (_EXTRA,)),
    ("evidence_doc", "delete"): ("doc_store.db", "DELETE FROM doc_nodes WHERE node_id=?", (_NORMAL,)),
    ("evidence_source", "update"): ("doc_store.db", "UPDATE doc_sources SET text='changed' WHERE source_id=?", (_NORMAL,)),
    ("evidence_source", "insert"): ("doc_store.db", "INSERT INTO doc_sources VALUES (?,'extra text','{}')", (_EXTRA,)),
    ("evidence_source", "delete"): ("doc_store.db", "DELETE FROM doc_sources WHERE source_id=?", (_NORMAL,)),
    ("evidence_vector", "update"): ("vectors.db", f"UPDATE {TABLE} SET document='changed' WHERE node_id=?", (_NORMAL,)),
    ("evidence_vector", "insert"): ("vectors.db", _VECTOR_INSERT, (_EXTRA, b"")),
    ("evidence_vector", "delete"): ("vectors.db", f"DELETE FROM {TABLE} WHERE node_id=?", (_NORMAL,)),
    ("registry_nodes", "update"): ("opencrab.db", "UPDATE ontology_nodes SET updated_at='x' WHERE node_id='doc:keep'", ()),
    ("registry_nodes", "insert"): ("opencrab.db", "INSERT INTO ontology_nodes(space,node_type,node_id) VALUES ('document','Document','doc:new')", ()),
    ("registry_nodes", "delete"): ("opencrab.db", "DELETE FROM ontology_nodes WHERE node_id='doc:keep'", ()),
    ("registry_edges", "update"): ("opencrab.db", "UPDATE ontology_edges SET created_at='x' WHERE to_id='doc:keep'", ()),
    ("registry_edges", "insert"): ("opencrab.db", "INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES ('evidence',?,'other','document','doc:keep')", (_NORMAL,)),
    ("registry_edges", "delete"): ("opencrab.db", "DELETE FROM ontology_edges WHERE to_id='doc:keep'", ()),
}


@pytest.mark.parametrize("section,op", sorted(_PROTECTION_MUTATIONS))
def test_protected_row_change_is_refused_before_any_write(tmp_path: Path, section: str, op: str):
    """비후보 보호 행 하나가 바뀌거나 추가되거나 사라지면 해당 섹션이 달라지고 apply는 백업 전에 거부한다."""
    import sqlite_vec

    root = tmp_path / "data"
    _protection_world(root)
    run, backup = tmp_path / "run", tmp_path / "backup"
    assert cleanup.dry_run(root, run, TABLE)["eligible"] is True
    _candidates, manifest = cleanup._load_run(run)
    ids = {"evidence:turn:heartbeat"}
    before, _ = cleanup._protection_sections(root, TABLE, ids)
    assert cleanup._protection_digest(before) == manifest["protected_sha256"]
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    db, sql, params = _PROTECTION_MUTATIONS[(section, op)]
    if db == "vectors.db":
        if op == "insert":
            params = (params[0], sqlite_vec.serialize_float32([2.0, 3.0]))
        _vector_sql(root, sql, params)
    else:
        with _db(root / db) as conn:
            conn.execute(sql, params)
    after, _ = cleanup._protection_sections(root, TABLE, ids)
    assert section in {name for name in before if before[name] != after[name]}
    state = _all_db_bytes(root)
    with pytest.raises(cleanup.CleanupRefusedError):
        cleanup.apply(root, run, TABLE, approval, backup, scan=_no_scan)
    assert not backup.exists()
    assert _all_db_bytes(root) == state


def test_protected_graph_raw_properties_text_is_in_the_full_digest_only(tmp_path: Path):
    """JSON 값이 같고 문자열만 다른 변경은 정규화 섹션을 바꾸지 않고 raw 섹션만 바꾼다."""
    root = tmp_path / "data"
    _protection_world(root)
    ids = {"evidence:turn:heartbeat"}
    before, _ = cleanup._protection_sections(root, TABLE, ids)
    with _db(root / "graph.db") as conn:
        conn.execute("UPDATE graph_nodes SET properties=? WHERE node_id=?", ('{"pack_id":"openclaw"}', _NORMAL))
    after, _ = cleanup._protection_sections(root, TABLE, ids)
    assert {name for name in before if before[name] != after[name]} == {"evidence_graph_raw"}


def test_witness_ignores_own_commits_and_sees_foreign_commits(world: Path):
    witness = cleanup._Witness(world)
    try:
        conn = witness.writer("graph.db")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO graph_nodes VALUES ('Document','doc:own','document','{}')")
        conn.commit()
        assert witness.changed() == []
        with _db(world / "graph.db") as other:
            other.execute("INSERT INTO graph_nodes VALUES ('Document','doc:foreign','document','{}')")
        assert witness.changed() == ["graph.db"]
    finally:
        witness.close()


def _prepared_apply(world: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    run, backup = tmp_path / "run", tmp_path / "backup"
    assert cleanup.dry_run(world, run, TABLE)["eligible"] is True
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    return run, backup, approval


def test_apply_refuses_database_with_trigger_before_any_write(world: Path, tmp_path: Path):
    with _db(world / "doc_store.db") as conn:
        conn.execute("CREATE TRIGGER t_493 AFTER DELETE ON doc_nodes BEGIN SELECT 1; END")
    run, backup, approval = _prepared_apply(world, tmp_path)
    state = _all_db_bytes(world)
    with pytest.raises(cleanup.CleanupRefusedError, match="has a trigger") as info:
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    assert info.value.code == cleanup.EXIT_GUARD
    assert not backup.exists()
    assert _all_db_bytes(world) == state


def test_foreign_trigger_added_between_axes_stops_the_next_write(world: Path, tmp_path: Path, monkeypatch):
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._delete_vector

    def vector_then_trigger(data_dir, vector_table, record):
        original(data_dir, vector_table, record)
        with _db(world / "doc_store.db") as conn:
            conn.execute("CREATE TRIGGER t_493 AFTER DELETE ON doc_nodes BEGIN SELECT 1; END")

    monkeypatch.setattr(cleanup, "_delete_vector", vector_then_trigger)
    with pytest.raises(cleanup.CleanupRefusedError, match="has a trigger"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    with _db(world / "graph.db") as conn:
        assert conn.execute("SELECT node_id FROM graph_nodes WHERE node_id='evidence:turn:heartbeat'").fetchall() == [("evidence:turn:heartbeat",)]


def test_foreign_fts_row_added_between_axes_rebuilds_index_and_refuses(world: Path, tmp_path: Path, monkeypatch):
    """위치 색인은 외부 commit이 보이면 다시 만든다. 새 FTS 행은 기존과 같이 거부된다."""
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._delete_vector

    def vector_then_fts(data_dir, vector_table, record):
        original(data_dir, vector_table, record)
        with _db(world / "doc_store.db") as conn:
            conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", (record["id"], "late duplicate"))

    monkeypatch.setattr(cleanup, "_delete_vector", vector_then_fts)
    with pytest.raises(cleanup.CleanupRefusedError):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT source_id FROM doc_sources WHERE source_id='evidence:turn:heartbeat'").fetchall() == [("evidence:turn:heartbeat",)]


def test_fts_lookup_by_rowid_matches_direct_query_and_delete_checks_text(world: Path, tmp_path: Path):
    candidate = "evidence:turn:heartbeat"
    with _db(world / "doc_store.db") as conn:
        conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", (candidate, "duplicate row"))
        direct = conn.execute("SELECT source_id,text FROM doc_sources_fts WHERE source_id=?", (candidate,)).fetchall()
    readers = cleanup._Readers(world, TABLE, immutable=True, ids={candidate})
    try:
        assert readers.fts_rows(candidate) == direct
        assert len(direct) == 2
    finally:
        readers.close()
    with _db(world / "doc_store.db") as conn:
        conn.execute("DELETE FROM doc_sources_fts WHERE text='duplicate row'")
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, _manifest = cleanup._load_run(run)
    record = candidates[0]
    with _db(world / "doc_store.db") as conn:
        conn.execute("UPDATE doc_sources_fts SET text='edited' WHERE source_id=?", (candidate,))
    with pytest.raises(cleanup.CleanupRefusedError, match="FTS CAS conflict"):
        cleanup._delete_document(world, record)
    with _db(world / "doc_store.db") as conn:
        assert conn.execute("SELECT text FROM doc_sources_fts WHERE source_id=?", (candidate,)).fetchall() == [("edited",)]


def test_candidate_loop_issues_no_full_scan_statements(world: Path, tmp_path: Path, monkeypatch):
    statements: list[str] = []
    original_ro, original_rw = cleanup._ro, cleanup._rw

    def traced_ro(path, immutable):
        conn = original_ro(path, immutable)
        conn.set_trace_callback(statements.append)
        return conn

    def traced_rw(path):
        conn = original_rw(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(cleanup, "_ro", traced_ro)
    monkeypatch.setattr(cleanup, "_rw", traced_rw)
    run, backup, approval = _prepared_apply(world, tmp_path)
    cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    joined = "\n".join(statements)
    assert statements
    for scan in ("FROM doc_sources_fts WHERE source_id", "FROM ontology_nodes WHERE node_id", "FROM ontology_edges WHERE from_id", "FROM ontology_edges WHERE relation='supports' AND to_space"):
        assert scan not in joined, scan


def test_candidate_store_rejects_a_line_changed_after_verification(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    candidates, _manifest = cleanup._load_run(run)
    assert [record["id"] for record in candidates] == ["evidence:turn:heartbeat"]
    path = run / "candidates.jsonl"
    data = path.read_bytes()
    assert data.count(b"heartbeat") >= 1
    path.write_bytes(data.replace(b"heartbeat", b"heartbeaT", 1))
    with pytest.raises(cleanup.CleanupRefusedError, match="candidate file does not match manifest"):
        list(candidates)


def test_scoped_edge_validation_union_equals_global(world: Path, tmp_path: Path):
    a, b, c = "evidence:turn:heartbeat", "evidence:turn:sc-b", "evidence:turn:sc-c"
    _add(world, b, "Q: Read HEARTBEAT.md if it exists sc b")
    _add(world, c, "Q: Read HEARTBEAT.md if it exists sc c")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", a, "ab", "Evidence", b, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", b, "bc", "Evidence", c, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", c, "out", "Evidence", "evidence:turn:normal", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", a, "self", "Evidence", a, "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    conn = cleanup._ro(world / "graph.db", False)
    try:
        for validate in (cleanup._validate_component_edges, cleanup._validate_boundary_edges):
            global_rows = {cleanup._sha(cleanup._canon(edge)) for edge in validate(conn, candidates, receipt, manifest)}
            scoped_rows = {
                cleanup._sha(cleanup._canon(edge))
                for node_id in (a, b, c)
                for edge in validate(conn, candidates, receipt, manifest, node_id)
            }
            assert global_rows == scoped_rows
            assert global_rows
    finally:
        conn.close()


def test_evidence_sidecar_is_bound_to_the_manifest(world: Path, tmp_path: Path):
    """Claim 판정이 쓰는 Evidence 항목은 sidecar 파일에 있고 manifest의 digest가 그 내용을 묶는다."""
    _registry_claim_setup(world)
    run = tmp_path / "run"
    assert cleanup.dry_run(world, run, TABLE)["eligible"] is True
    descriptor = _load(run / "manifest.json")["protected"]["evidence"]
    assert descriptor["file"] == "evidence.jsonl" and descriptor["count"] >= 1
    candidates, manifest = cleanup._load_run(run)
    try:
        assert manifest.evidence_store().get("evidence:turn:normal")["graph"]["id"] == "evidence:turn:normal"
    finally:
        candidates.close()
        manifest.close()
    path = run / "evidence.jsonl"
    path.write_bytes(path.read_bytes().replace(b"normal", b"normaL", 1))
    with pytest.raises(cleanup.CleanupRefusedError, match="evidence file does not match manifest"):
        cleanup._load_run(run)


def test_protection_pass_rejects_invalid_properties_json_on_any_graph_row(world: Path, tmp_path: Path):
    """기존 _protected와 같이 모든 노드와 edge의 properties JSON을 검증한다(후보에 닿는 edge 포함)."""
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES ('Concept','concept:bad','concept','not-json')")
    with pytest.raises(cleanup.CleanupRefusedError, match="properties JSON is invalid"):
        cleanup.dry_run(world, tmp_path / "node-run", TABLE)
    with _db(world / "graph.db") as conn:
        conn.execute("DELETE FROM graph_nodes WHERE node_id='concept:bad'")
        conn.execute("INSERT INTO graph_edges VALUES ('Evidence','evidence:turn:heartbeat','rel','Evidence','evidence:turn:normal','not-json')")
    with pytest.raises(cleanup.CleanupRefusedError):
        cleanup.dry_run(world, tmp_path / "edge-run", TABLE)


def _two_candidate_world(world: Path) -> str:
    second = "evidence:turn:second"
    _add(world, second, "Q: Read HEARTBEAT.md if it exists second")
    _fix_registry_timestamps(world)
    return second


def test_change_during_backup_invalidates_the_full_snapshot_mark(world: Path, tmp_path: Path, monkeypatch):
    """첫 snapshot 검사 뒤 백업 도중의 외부 commit은 사전 검사를 다시 돌려 첫 삭제 전에 거부된다."""
    second = _two_candidate_world(world)
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._backup_receipt

    def backup_then_change(*args):
        receipt = original(*args)
        with _db(world / "opencrab.db") as conn:
            conn.execute("UPDATE ontology_nodes SET updated_at='changed' WHERE node_id=?", (second,))
        return receipt

    monkeypatch.setattr(cleanup, "_backup_receipt", backup_then_change)
    with pytest.raises(cleanup.CleanupRefusedError, match="resume precheck conflict"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    with _db(world / "graph.db") as conn:
        assert {row[0] for row in conn.execute("SELECT node_id FROM graph_nodes WHERE node_id LIKE 'evidence:turn:%'")} >= {"evidence:turn:heartbeat", second}


def test_backup_hash_is_recomputed_even_when_file_metadata_is_unchanged(world: Path, tmp_path: Path, monkeypatch):
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._backup_receipt

    def backup_then_overwrite(*args):
        receipt = original(*args)
        target = backup / "vectors.db"
        stat = target.stat()
        with target.open("r+b") as handle:
            handle.seek(stat.st_size - 1)
            last = handle.read(1)
            handle.seek(stat.st_size - 1)
            handle.write(bytes([last[0] ^ 1]))
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return receipt

    monkeypatch.setattr(cleanup, "_backup_receipt", backup_then_overwrite)
    with pytest.raises(cleanup.CleanupRefusedError, match="backup receipt mismatch"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)


def test_scoped_edge_validation_reads_only_the_candidate_edges(world: Path, tmp_path: Path):
    """scope_id를 주면 그 후보에 닿는 edge만 조회하고 반환한다. 전체 조회로 되돌리면 이 시험이 실패한다."""
    a, b, c = "evidence:turn:heartbeat", "evidence:turn:sc-b", "evidence:turn:sc-c"
    _add(world, b, "Q: Read HEARTBEAT.md if it exists sc b")
    _add(world, c, "Q: Read HEARTBEAT.md if it exists sc c")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", a, "ab", "Evidence", b, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", b, "bc", "Evidence", c, "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("Evidence", c, "out", "Evidence", "evidence:turn:normal", "{}"))
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(world, run, TABLE)
    candidates, manifest = cleanup._load_run(run)
    cleanup._backup_receipt(world, run, backup, manifest, candidates)
    receipt = cleanup._load_receipt(run, backup, manifest, candidates, world)
    conn = cleanup._ro(world / "graph.db", False)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        global_component = cleanup._validate_component_edges(conn, candidates, receipt, manifest)
        assert {(edge["from_id"], edge["to_id"]) for edge in global_component} == {(a, b), (b, c)}
        statements.clear()
        scoped = cleanup._validate_component_edges(conn, candidates, receipt, manifest, a)
        assert {(edge["from_id"], edge["to_id"]) for edge in scoped} == {(a, b)}
        assert statements and all(f"'{a}'" in statement for statement in statements)
        statements.clear()
        boundary = cleanup._validate_boundary_edges(conn, candidates, receipt, manifest, c)
        assert {(edge["from_id"], edge["to_id"]) for edge in boundary} == {(c, "evidence:turn:normal")}
        assert statements and all(f"'{c}'" in statement for statement in statements)
    finally:
        conn.close()


def test_verify_protection_does_not_absorb_a_commit_that_lands_during_the_baseline_read(world: Path, tmp_path: Path, monkeypatch):
    """채택된 기준값과 다른 값이 읽히면 새 기준값으로 흡수하지 않고 거부한다. 읽기 직전에 끼어든 commit을 주입한다."""
    run = tmp_path / "run"
    assert cleanup.dry_run(world, run, TABLE)["eligible"] is True
    candidates, manifest = cleanup._load_run(run)
    scope = cleanup._RunScope(world, TABLE, candidates, manifest)
    scope.witness = cleanup._Witness(world)
    monkeypatch.setattr(cleanup, "_SCOPE", scope)
    real_snapshot = scope.witness.snapshot
    injected: list[bool] = []

    def snapshot_after_foreign_commit():
        if not injected:
            injected.append(True)
            with _db(world / "graph.db") as other:
                other.execute("INSERT INTO graph_nodes VALUES ('Concept','concept:late','concept','{}')")
        return real_snapshot()

    monkeypatch.setattr(scope.witness, "snapshot", snapshot_after_foreign_commit)
    try:
        assert cleanup._verify_protection(world, TABLE, candidates, manifest["protected_sha256"]) is False
        assert scope.witness.changed() == ["graph.db"]
    finally:
        scope.witness.close()
        candidates.close()
        manifest.close()


def test_trigger_added_before_the_baseline_is_still_refused(world: Path, tmp_path: Path, monkeypatch):
    """trigger 확인은 기준값을 잡은 뒤 같은 연결로 한다. 기준값 직전에 추가된 trigger도 거부된다."""
    run, backup, approval = _prepared_apply(world, tmp_path)
    original_init = cleanup._Witness.__init__

    def init_after_trigger(self, data_dir):
        with _db(world / "doc_store.db") as conn:
            conn.execute("CREATE TRIGGER t_493 AFTER DELETE ON doc_nodes BEGIN SELECT 1; END")
        original_init(self, data_dir)

    monkeypatch.setattr(cleanup._Witness, "__init__", init_after_trigger)
    with pytest.raises(cleanup.CleanupRefusedError, match="has a trigger"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    assert not backup.exists()


def test_change_during_the_last_receipt_load_reruns_the_full_precheck_with_a_fresh_index(world: Path, tmp_path: Path, monkeypatch):
    """마지막 영수증 적재 도중 뒤 후보에 FTS 중복 행이 생기면 위치 색인을 다시 만든 뒤 전량 사전 검사가 첫 삭제 전에 거부한다."""
    second = _two_candidate_world(world)
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._load_receipt
    calls: list[int] = []

    def load_then_change(*args, **kwargs):
        receipt = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 2:  # 첫 apply에서 사전 검사 바로 앞의 적재
            with _db(world / "doc_store.db") as conn:
                conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", (second, "late duplicate"))
        return receipt

    monkeypatch.setattr(cleanup, "_load_receipt", load_then_change)
    with pytest.raises(cleanup.CleanupRefusedError, match="resume precheck conflict"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    with _db(world / "doc_store.db") as conn:
        assert len(conn.execute("SELECT source_id FROM doc_sources WHERE source_id LIKE 'evidence:turn:%'").fetchall()) >= 2


def _vector_node_ids(root: Path) -> list[str]:
    import sqlite_vec

    with _db(root / "vectors.db") as conn:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return [row[0] for row in conn.execute(f"SELECT node_id FROM {TABLE} ORDER BY node_id")]


def test_change_after_the_last_axis_snapshot_is_checked_again(world: Path, tmp_path: Path, monkeypatch):
    """마지막 축의 snapshot 확인 뒤 축 끝 확인 직전에 끼어든 commit은 같은 축을 다시 검증하게 한다."""
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._axis_end_check
    calls: list[int] = []

    def commit_then_end_check(*args):
        calls.append(1)
        if len(calls) == 4:  # vector, graph, document, registry 순서의 마지막 축
            with _db(world / "doc_store.db") as conn:
                conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", ("evidence:turn:heartbeat", "late duplicate"))
        return original(*args)

    monkeypatch.setattr(cleanup, "_axis_end_check", commit_then_end_check)
    with pytest.raises(cleanup.CleanupRefusedError, match="snapshot conflict"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)


def test_commit_before_the_axis_snapshot_read_is_seen_by_the_snapshot(world: Path, tmp_path: Path, monkeypatch):
    """snapshot 읽기 직전의 외부 commit은 색인을 다시 만든 뒤 다시 읽어 첫 삭제 전에 거부된다."""
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._snapshot_now
    calls: list[int] = []

    def snapshot_after_commit(*args):
        calls.append(1)
        if len(calls) == 2:  # 첫 apply: 전체 snapshot 검사가 1회, 이어서 첫 축의 snapshot
            with _db(world / "doc_store.db") as conn:
                conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", ("evidence:turn:heartbeat", "late duplicate"))
        return original(*args)

    monkeypatch.setattr(cleanup, "_snapshot_now", snapshot_after_commit)
    with pytest.raises(cleanup.CleanupRefusedError, match="snapshot conflict before vector"):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    assert "evidence:turn:heartbeat" in _vector_node_ids(world)


def test_commit_right_after_the_precheck_reruns_it_before_the_first_write(world: Path, tmp_path: Path, monkeypatch):
    second = _two_candidate_world(world)
    run, backup, approval = _prepared_apply(world, tmp_path)
    original = cleanup._registry_resume_precheck

    def precheck_then_commit(*args):
        original(*args)
        with _db(world / "doc_store.db") as conn:
            conn.execute("INSERT INTO doc_sources_fts VALUES (?,?)", (second, "late duplicate"))

    monkeypatch.setattr(cleanup, "_registry_resume_precheck", precheck_then_commit)
    with pytest.raises(cleanup.CleanupRefusedError):
        cleanup.apply(world, run, TABLE, approval, backup, scan=_no_scan)
    assert {"evidence:turn:heartbeat", second} <= set(_vector_node_ids(world))


def test_scoped_keys_returns_only_the_edges_that_touch_the_candidate():
    recorded = {
        "k1": {"from_id": "a", "to_id": "b"},
        "k2": {"from_id": "b", "to_id": "c"},
        "k3": {"from_id": "c", "to_id": "c"},
    }
    assert cleanup._scoped_keys(recorded, "a") == {"k1"}
    assert cleanup._scoped_keys(recorded, "b") == {"k1", "k2"}
    assert cleanup._scoped_keys(recorded, "c") == {"k2", "k3"}
    assert cleanup._scoped_keys(recorded, "z") == set()
    scope = cleanup._RunScope(Path("."), TABLE, [], {})
    previous = cleanup._SCOPE
    cleanup._SCOPE = scope
    try:
        assert cleanup._scoped_keys(recorded, "b") == {"k1", "k2"}
        assert cleanup._scoped_keys(recorded, "z") == set()
    finally:
        cleanup._SCOPE = previous


# --- 승인된 ID 집합 선택 (#513, #520) ---

_STAGE = "/data-root/staging/run-1/"
_ART_A = _STAGE + "records.jsonl"
_ART_B = _STAGE + "report.md"
_KEEP = _STAGE + "keep.md"
_OTHER_PACK = _STAGE + "other-pack.md"


@pytest.fixture
def id_world(tmp_path: Path) -> Path:
    """파일 경로를 id로 쓰는 TextUnit 노드와 그 주변 대조군이다."""
    root = tmp_path / "id-data"
    root.mkdir()
    _schema(root)
    _add(root, _ART_A, "artifact one", pack="pack-a", node_type="TextUnit")
    _add(root, _ART_B, "artifact two", pack="pack-a", node_type="TextUnit")
    _add(root, _KEEP, "kept text unit", pack="pack-a", node_type="TextUnit")
    _add(root, _OTHER_PACK, "other pack text unit", pack="pack-b", node_type="TextUnit")
    _add(root, "evidence:turn:heartbeat", HEARTBEAT)
    return root


def _ids_file(tmp_path: Path, ids: list[str], *, pack: str = "pack-a", node_type: str = "TextUnit", name: str = "ids.json", extra: dict | None = None) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"pack_id": pack, "node_type": node_type, "ids": ids, **(extra or {})}), encoding="utf-8")
    return path


def _id_base(world: Path, run: Path) -> list[str]:
    return ["--local-data-dir", str(world), "--run-dir", str(run), "--vector-table", TABLE]


def _id_approval(tmp_path: Path, run: Path) -> Path:
    manifest = _load(run / "manifest.json")
    approval = tmp_path / "id-approval.json"
    approval.write_text(json.dumps({"manifest_sha256": cleanup._sha(cleanup._canon(manifest)), "ordered_ids_sha256": manifest["ordered_ids_sha256"]}), encoding="utf-8")
    return approval


def test_approved_ids_dry_run_selects_exactly_the_approved_set_without_writes(id_world: Path, tmp_path: Path):
    before = _all_db_bytes(id_world)
    run = tmp_path / "run"
    result = cleanup.dry_run(id_world, run, TABLE, _ids_file(tmp_path, [_ART_B, _ART_A]))
    assert result["eligible"] is True, result
    lines = (run / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(line)["id"] for line in lines) == sorted([_ART_A, _ART_B])
    selection = _load(run / "manifest.json")["selection"]
    assert selection == {"mode": "id-set", "pack_id": "pack-a", "node_type": "TextUnit", "approved_count": 2, "approved_ids_sha256": cleanup._ids_digest([_ART_A, _ART_B])}
    assert _all_db_bytes(id_world) == before
    # 기본 선택은 종전 그대로이며 선택 기록은 prefix다.
    prefix_run = tmp_path / "prefix-run"
    cleanup.dry_run(id_world, prefix_run, TABLE)
    assert _load(prefix_run / "manifest.json")["selection"] == {"mode": "prefix"}
    assert [json.loads(line)["id"] for line in (prefix_run / "candidates.jsonl").read_text(encoding="utf-8").splitlines()] == ["evidence:turn:heartbeat"]


_ONE_AXIS_PACK_ERRORS = {
    "source-pack-only": "another pack",
    "doc-pack-only": "evidence doc ownership",
    "graph-pack-only": "Evidence graph ownership",
    "vector-pack-only": "vector ownership",
}


def _set_one_axis_pack(world: Path, node_id: str, axis: str, pack: str) -> None:
    """한 축의 pack_id만 바꾼다. 나머지 축은 그대로 둔다."""
    if axis == "source-pack-only":
        with _db(world / "doc_store.db") as conn:
            conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id=?", (json.dumps({"pack_id": pack, "node_id": node_id}), node_id))
    elif axis == "doc-pack-only":
        with _db(world / "doc_store.db") as conn:
            conn.execute("UPDATE doc_nodes SET properties=? WHERE node_id=?", (json.dumps({"pack_id": pack}), node_id))
    elif axis == "graph-pack-only":
        with _db(world / "graph.db") as conn:
            conn.execute("UPDATE graph_nodes SET properties=? WHERE node_id=?", (json.dumps({"pack_id": pack}), node_id))
    else:
        import sqlite_vec

        with _db(world / "vectors.db") as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            row = conn.execute(f"SELECT node_id,embedding,document FROM {TABLE} WHERE node_id=?", (node_id,)).fetchone()
            conn.execute(f"DELETE FROM {TABLE} WHERE node_id=?", (node_id,))
            conn.execute(f"INSERT INTO {TABLE}(node_id,pack_id,embedding,document,metadata) VALUES (?,?,?,?,?)", (row[0], pack, row[1], row[2], json.dumps({"pack_id": pack, "node_id": node_id})))


@pytest.mark.parametrize("fault", ["no-source-row", "other-pack", "other-type", "missing-fts", "wrong-space", *_ONE_AXIS_PACK_ERRORS])
def test_approved_ids_dry_run_refuses_any_unmatched_id(id_world: Path, tmp_path: Path, fault: str):
    ids = [_ART_A]
    node_type = "TextUnit"
    if fault in _ONE_AXIS_PACK_ERRORS:
        # ID 경로는 소스 팩 사전 필터 없이 후보 검증을 부른다. 축 하나의 팩만 달라도 거부해야 한다.
        _set_one_axis_pack(id_world, _ART_A, fault, "pack-b")
    elif fault == "no-source-row":
        ids.append(_STAGE + "absent.md")
    elif fault == "other-pack":
        ids.append(_OTHER_PACK)
    elif fault == "other-type":
        ids.append("evidence:turn:heartbeat")
    elif fault == "missing-fts":
        with _db(id_world / "doc_store.db") as conn:
            conn.execute("DELETE FROM doc_sources_fts WHERE source_id=?", (_ART_A,))
    else:
        with _db(id_world / "graph.db") as conn:
            conn.execute("UPDATE graph_nodes SET space_id='concept' WHERE node_id=?", (_ART_A,))
    run = tmp_path / fault
    result = cleanup.dry_run(id_world, run, TABLE, _ids_file(tmp_path, ids, node_type=node_type))
    assert result["eligible"] is False
    assert result["errors"]
    assert not (run / "manifest.json").exists()
    if fault in _ONE_AXIS_PACK_ERRORS:
        assert _ONE_AXIS_PACK_ERRORS[fault] in " ".join(result["errors"])


@pytest.mark.parametrize("fault", ["extra-key", "missing-key", "duplicate", "empty", "empty-string", "claim-type", "document-type", "not-object", "missing-file"])
def test_approved_ids_file_format_is_strict(id_world: Path, tmp_path: Path, fault: str):
    path = tmp_path / "ids.json"
    if fault == "extra-key":
        _ids_file(tmp_path, [_ART_A], extra={"note": "x"})
    elif fault == "missing-key":
        path.write_text(json.dumps({"pack_id": "pack-a", "ids": [_ART_A]}), encoding="utf-8")
    elif fault == "duplicate":
        _ids_file(tmp_path, [_ART_A, _ART_A])
    elif fault == "empty":
        _ids_file(tmp_path, [])
    elif fault == "empty-string":
        _ids_file(tmp_path, [_ART_A, ""])
    elif fault == "claim-type":
        _ids_file(tmp_path, [_ART_A], node_type="Claim")
    elif fault == "document-type":
        _ids_file(tmp_path, [_ART_A], node_type="Document")
    elif fault == "not-object":
        path.write_text(json.dumps([_ART_A]), encoding="utf-8")
    run = tmp_path / "run"
    with pytest.raises(cleanup.CleanupRefusedError):
        cleanup.dry_run(id_world, run, TABLE, path)
    assert not run.exists()


def test_cli_approved_ids_is_dry_run_only(id_world: Path, tmp_path: Path):
    ids = _ids_file(tmp_path, [_ART_A])
    run = tmp_path / "run"
    for flag in (["--apply", "--backup-dir", str(tmp_path / "b")], ["--rollback", "--backup-dir", str(tmp_path / "b")], ["--prepare-rollback", "--backup-dir", str(tmp_path / "b")]):
        result = _cli_main(tmp_path, "ids-only-dry", [*_id_base(id_world, run), "--approved-ids", str(ids), *flag])
        assert result.returncode == 2, result.stderr
        assert "only for dry-run" in result.stderr
    assert not run.exists()


def test_cli_approved_ids_apply_and_rollback_in_separate_processes(id_world: Path, tmp_path: Path):
    """dry-run, apply, prepare-rollback, rollback을 각각 새 프로세스로 돌린다. apply는 manifest에서 선택을 복원한다."""
    run, backup = tmp_path / "run", tmp_path / "backup"
    base = _id_base(id_world, run)
    dry = _cli_main(tmp_path, "id-dry", [*base, "--approved-ids", str(_ids_file(tmp_path, [_ART_A, _ART_B]))])
    assert dry.returncode == 0, dry.stderr
    before_rows = _all_db_rows(id_world)
    survivors = {node_id: _survivor_raw(id_world, node_id) for node_id in (_KEEP, _OTHER_PACK, "evidence:turn:heartbeat")}
    applied = _cli_main(tmp_path, "id-apply", [*base, "--apply", "--approved-manifest", str(_id_approval(tmp_path, run)), "--backup-dir", str(backup)])
    assert applied.returncode == 0, applied.stderr
    for candidate in (_ART_A, _ART_B):
        _assert_candidate_absent(id_world, candidate)
    for node_id, raw in survivors.items():
        assert _survivor_raw(id_world, node_id) == raw
    prepared = _cli_main(tmp_path, "id-prepare", [*base, "--prepare-rollback", "--backup-dir", str(backup)])
    assert prepared.returncode == 0, prepared.stderr
    rolled = _cli_main(tmp_path, "id-rollback", [*base, "--rollback", "--backup-dir", str(backup)])
    assert rolled.returncode == 0, rolled.stderr
    assert _all_db_rows(id_world) == before_rows


def test_approved_ids_last_claim_support_blocks_manifest_and_other_support_allows(id_world: Path, tmp_path: Path):
    with _db(id_world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", "claim:one", "claim", "{}"))
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("TextUnit", _ART_A, "supports", "Claim", "claim:one", "{}"))
    blocked = cleanup.dry_run(id_world, tmp_path / "blocked", TABLE, _ids_file(tmp_path, [_ART_A]))
    assert blocked["eligible"] is False
    assert "final Claim support" in blocked["errors"][0]
    # 같은 타입의 비후보가 같은 Claim을 지지하면 후보를 지워도 지지가 남는다.
    with _db(id_world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", ("TextUnit", _KEEP, "supports", "Claim", "claim:one", "{}"))
    allowed = cleanup.dry_run(id_world, tmp_path / "allowed", TABLE, _ids_file(tmp_path, [_ART_A], name="ids2.json"))
    assert allowed["eligible"] is True, allowed


def _id_interrupted_apply(id_world: Path, tmp_path: Path, run: Path, approval: Path, backup: Path, at: str = "after-graph-commit") -> None:
    """첫 프로세스가 죽는다. before-graph는 첫 후보의 vector 축 뒤 graph 축 전이고 after-graph-commit은 첫 graph commit 직후이다."""
    if at == "before-graph":
        body = "    raise SystemExit(77)\n"
    else:
        body = "    original(data, record, candidates, receipt, manifest, after_commit=lambda: (_ for _ in ()).throw(SystemExit(77)))\n"
    setup = (
        "original = cleanup._delete_graph\n"
        "def stop(data, record, candidates, receipt, manifest):\n"
        + body
        + "cleanup._delete_graph = stop\n"
    )
    result = _cli_main(tmp_path, "id-interrupted", [*_id_base(id_world, run), "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)], setup)
    assert result.returncode == 77, result.stderr


@pytest.mark.parametrize("at", ["before-graph", "after-graph-commit"])
def test_cli_approved_ids_resume_in_new_process_completes(id_world: Path, tmp_path: Path, at: str):
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(id_world, run, TABLE, _ids_file(tmp_path, [_ART_A, _ART_B]))
    approval = _id_approval(tmp_path, run)
    survivors = {node_id: _survivor_raw(id_world, node_id) for node_id in (_KEEP, _OTHER_PACK, "evidence:turn:heartbeat")}
    _id_interrupted_apply(id_world, tmp_path, run, approval, backup, at)
    first = min(_ART_A, _ART_B)  # 후보는 id 순서로 처리한다
    raw = _survivor_raw(id_world, first)
    if at == "before-graph":
        # 첫 후보의 vector 행만 지워졌고 graph, document, registry 축은 남았다.
        assert raw["vector"] == []
        assert raw["graph"] and raw["source"] and raw["fts"] and raw["doc"] and raw["registry"]
    else:
        assert raw["vector"] == [] and raw["graph"] == []
        assert raw["source"] and raw["registry"]
    assert _survivor_raw(id_world, max(_ART_A, _ART_B))["vector"]  # 둘째 후보는 시작 전이다
    resumed = _cli_main(tmp_path, "id-resume", [*_id_base(id_world, run), "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)])
    assert resumed.returncode == 0, resumed.stderr
    for candidate in (_ART_A, _ART_B):
        _assert_candidate_absent(id_world, candidate)
    for node_id, raw in survivors.items():
        assert _survivor_raw(id_world, node_id) == raw


def test_cli_approved_ids_resume_protects_same_type_non_candidate(id_world: Path, tmp_path: Path):
    """후보와 같은 타입의 비후보는 보호 집합에 든다. 중단 뒤 그 행이 바뀌면 재개가 거부한다."""
    run, backup = tmp_path / "run", tmp_path / "backup"
    cleanup.dry_run(id_world, run, TABLE, _ids_file(tmp_path, [_ART_A, _ART_B]))
    approval = _id_approval(tmp_path, run)
    _id_interrupted_apply(id_world, tmp_path, run, approval, backup)
    with _db(id_world / "doc_store.db") as conn:
        conn.execute("UPDATE doc_sources SET text='changed after interruption' WHERE source_id=?", (_KEEP,))
    resumed = _cli_main(tmp_path, "id-resume-protected", [*_id_base(id_world, run), "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)])
    assert resumed.returncode == 2
    assert "protected set changed" in resumed.stderr


@pytest.mark.parametrize("tamper", ["digest", "count", "mode", "extra-key", "reserved-type"])
def test_load_run_refuses_tampered_selection_record(id_world: Path, tmp_path: Path, tamper: str):
    run = tmp_path / "run"
    cleanup.dry_run(id_world, run, TABLE, _ids_file(tmp_path, [_ART_A, _ART_B]))
    manifest = _load(run / "manifest.json")
    record = manifest["selection"]
    if tamper == "digest":
        record["approved_ids_sha256"] = cleanup._ids_digest([_ART_A])
    elif tamper == "count":
        record["approved_count"] = 1
    elif tamper == "mode":
        record["mode"] = "everything"
    elif tamper == "extra-key":
        record["ids"] = [_ART_A]
    else:
        record["node_type"] = "Claim"
    (run / "manifest.json").write_bytes(cleanup._canon(manifest))
    with pytest.raises(cleanup.CleanupRefusedError, match="selection"):
        cleanup._load_run(run)


def test_load_run_reads_legacy_manifest_without_selection_as_prefix(world: Path, tmp_path: Path):
    run = tmp_path / "run"
    cleanup.dry_run(world, run, TABLE)
    manifest = _load(run / "manifest.json")
    del manifest["selection"]
    (run / "manifest.json").write_bytes(cleanup._canon(manifest))
    candidates, loaded = cleanup._load_run(run)
    try:
        assert loaded.selection == cleanup._PREFIX_SELECTION
    finally:
        candidates.close()


def _mixed_claim_setup(world: Path) -> tuple[str, str]:
    """TextUnit 후보와 Evidence 비후보가 graph와 registry에서 같은 Claim을 지지한다."""
    survivor, claim = "evidence:mixed:keep", "claim:mixed"
    _add(world, survivor, "surviving evidence", pack="pack-a", node_type="Evidence")
    with _db(world / "graph.db") as conn:
        conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", ("Claim", claim, "claim", "{}"))
        for kind, node_id in (("TextUnit", _ART_A), ("Evidence", survivor)):
            conn.execute("INSERT INTO graph_edges VALUES (?,?,?,?,?,?)", (kind, node_id, "supports", "Claim", claim, "{}"))
    with _db(world / "opencrab.db") as conn:
        conn.execute("INSERT INTO ontology_nodes(space,node_type,node_id) VALUES (?,?,?)", ("claim", "Claim", claim))
        for node_id in (_ART_A, survivor):
            conn.execute("INSERT INTO ontology_edges(from_space,from_id,relation,to_space,to_id) VALUES (?,?,?,?,?)", ("evidence", node_id, "supports", "claim", claim))
    return survivor, claim


@pytest.mark.parametrize("interrupt", [False, True])
def test_cli_mixed_claim_support_survives_apply_resume_and_rollback(id_world: Path, tmp_path: Path, interrupt: bool):
    survivor, claim = _mixed_claim_setup(id_world)
    before = _all_db_rows(id_world)
    survivor_before = _survivor_raw(id_world, survivor, claim)
    claim_before = _survivor_raw(id_world, claim)
    run, backup = tmp_path / "run", tmp_path / "backup"
    result = cleanup.dry_run(id_world, run, TABLE, _ids_file(tmp_path, [_ART_A]))
    assert result["eligible"] is True, result
    approval = _id_approval(tmp_path, run)
    if interrupt:
        _id_interrupted_apply(id_world, tmp_path, run, approval, backup, "before-graph")
    applied = _cli_main(tmp_path, "mixed-apply", [*_id_base(id_world, run), "--apply", "--approved-manifest", str(approval), "--backup-dir", str(backup)])
    assert applied.returncode == 0, applied.stderr
    _assert_candidate_absent(id_world, _ART_A)
    assert _survivor_raw(id_world, survivor, claim) == survivor_before
    assert _survivor_raw(id_world, claim) == claim_before
    base = _id_base(id_world, run)
    prepared = _cli_main(tmp_path, "mixed-prepare", [*base, "--prepare-rollback", "--backup-dir", str(backup)])
    assert prepared.returncode == 0, prepared.stderr
    rolled = _cli_main(tmp_path, "mixed-rollback", [*base, "--rollback", "--backup-dir", str(backup)])
    assert rolled.returncode == 0, rolled.stderr
    assert _all_db_rows(id_world) == before


@pytest.mark.parametrize("fault", ["source-pack", "graph-space", "registry-type", "edge-type", "last-support"])
def test_mixed_claim_support_refuses_invalid_survivor(id_world: Path, tmp_path: Path, fault: str):
    survivor, claim = _mixed_claim_setup(id_world)
    if fault == "source-pack":
        _set_one_axis_pack(id_world, survivor, "source-pack-only", "pack-b")
    elif fault == "graph-space":
        with _db(id_world / "graph.db") as conn:
            conn.execute("UPDATE graph_nodes SET space_id='concept' WHERE node_id=?", (survivor,))
    elif fault == "registry-type":
        with _db(id_world / "opencrab.db") as conn:
            conn.execute("UPDATE ontology_nodes SET node_type='TextUnit' WHERE node_id=?", (survivor,))
    elif fault == "edge-type":
        with _db(id_world / "graph.db") as conn:
            conn.execute("UPDATE graph_edges SET from_type='TextUnit' WHERE from_id=?", (survivor,))
    else:
        with _db(id_world / "graph.db") as conn:
            conn.execute("DELETE FROM graph_edges WHERE from_id=?", (survivor,))
        with _db(id_world / "opencrab.db") as conn:
            conn.execute("DELETE FROM ontology_edges WHERE from_id=?", (survivor,))
    run = tmp_path / "run"
    result = cleanup.dry_run(id_world, run, TABLE, _ids_file(tmp_path, [_ART_A]))
    assert result["eligible"] is False, result
    assert "Claim support" in " ".join(result["errors"])
    assert not (run / "manifest.json").exists()
