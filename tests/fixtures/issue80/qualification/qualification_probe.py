from __future__ import annotations

import inspect
import json
import multiprocessing as mp
import os
import platform
import queue as queue_module
import shutil
import tempfile
import time
import traceback
from pathlib import Path

import ladybug


def result_rows(result):
    if isinstance(result, list):
        return [result_rows(item) for item in result]
    if hasattr(result, "get_all"):
        try:
            result.rows_as_dict()
            return result.get_all()
        except Exception as exc:
            return {"get_all_error": f"{type(exc).__name__}: {exc}"}
    return {"type": type(result).__name__}


def call(conn, query, parameters=None):
    try:
        result = conn.execute(query, parameters)
        return {"ok": True, "rows": result_rows(result)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def api_inventory():
    names = {}
    for name in ("Database", "Connection", "Transaction"):
        cls = getattr(ladybug, name, None)
        names[name] = {
            "present": cls is not None,
            "module": getattr(cls, "__module__", None),
            "signature": None,
            "public": [x for x in dir(cls) if not x.startswith("_")] if cls else [],
        }
        if cls:
            try:
                names[name]["signature"] = str(inspect.signature(cls))
            except Exception as exc:
                names[name]["signature_error"] = f"{type(exc).__name__}: {exc}"
    return names


def one_connection_probe(root):
    db_path = root / "single"
    db = ladybug.Database(db_path, enable_multi_writes=True)
    conn = ladybug.Connection(db)
    out = {"db_path": str(db_path), "operations": {}}
    try:
        out["operations"]["create_node"] = call(
            conn,
            "CREATE NODE TABLE Node(node_id STRING, node_type STRING, node_digest STRING, props STRING, PRIMARY KEY(node_id))",
        )
        out["operations"]["create_edge"] = call(
            conn,
            "CREATE REL TABLE Edge(FROM Node TO Node, relation STRING, edge_digest STRING, props STRING)",
        )
        out["operations"]["insert_a"] = call(
            conn,
            "CREATE (n:Node {node_id: 'a', node_type: 'Person', node_digest: 'd1', props: '{}'})",
        )
        out["operations"]["insert_b"] = call(
            conn,
            "CREATE (n:Node {node_id: 'b', node_type: 'Asset', node_digest: 'd2', props: '{}'})",
        )
        out["operations"]["duplicate_pk"] = call(
            conn,
            "CREATE (n:Node {node_id: 'a', node_type: 'Other', node_digest: 'd3', props: '{}'})",
        )
        out["operations"]["conditional_cas"] = call(
            conn,
            "MATCH (n:Node {node_id: 'a'}) WHERE n.node_digest = 'd1' SET n.node_type = 'Account', n.node_digest = 'd4' RETURN n.node_id, n.node_type, n.node_digest",
        )
        out["operations"]["stale_cas"] = call(
            conn,
            "MATCH (n:Node {node_id: 'a'}) WHERE n.node_digest = 'd1' SET n.node_type = 'Stale', n.node_digest = 'd5' RETURN n.node_id",
        )
        out["operations"]["create_edge"] = call(
            conn,
            "CREATE (a:Node {node_id: 'c', node_type: 'Person', node_digest: 'd6', props: '{}'}), (b:Node {node_id: 'd', node_type: 'Asset', node_digest: 'd7', props: '{}'}), (a)-[r:Edge {relation: 'owns', edge_digest: 'e1', props: '{}'}]->(b)",
        )
        out["operations"]["edge_conditional_cas"] = call(
            conn,
            "MATCH (a:Node {node_id: 'c'})-[r:Edge]->(b:Node {node_id: 'd'}) WHERE r.edge_digest = 'e1' SET r.edge_digest = 'e2' RETURN r.edge_digest",
        )
        out["operations"]["edge_stale_cas"] = call(
            conn,
            "MATCH (a:Node {node_id: 'c'})-[r:Edge]->(b:Node {node_id: 'd'}) WHERE r.edge_digest = 'e1' SET r.edge_digest = 'e3' RETURN r.edge_digest",
        )
        out["operations"]["rollback_command"] = call(conn, "BEGIN TRANSACTION")
        out["operations"]["insert_before_rollback"] = call(
            conn,
            "CREATE (n:Node {node_id: 'rollback', node_type: 'Probe', node_digest: 'dr', props: '{}'})",
        )
        out["operations"]["rollback"] = call(conn, "ROLLBACK")
        out["operations"]["check_rollback"] = call(
            conn,
            "MATCH (n:Node {node_id: 'rollback'}) RETURN n.node_id",
        )
        out["operations"]["begin_command_2"] = call(conn, "BEGIN TRANSACTION")
        out["operations"]["insert_before_commit"] = call(
            conn,
            "CREATE (n:Node {node_id: 'commit', node_type: 'Probe', node_digest: 'dc', props: '{}'})",
        )
        out["operations"]["commit"] = call(conn, "COMMIT")
        out["operations"]["check_commit"] = call(
            conn,
            "MATCH (n:Node {node_id: 'commit'}) RETURN n.node_id",
        )
        out["operations"]["failed_edge_write_begin"] = call(conn, "BEGIN TRANSACTION")
        out["operations"]["failed_edge_write"] = call(
            conn,
            "CREATE (e:Node {node_id: 'e', node_type: 'Probe', node_digest: 'de', props: '{}'}), (f:Node {node_id: 'f', node_type: 'Probe', node_digest: 'df', props: '{}'}), (e)-[:Edge {relation: 'bad', edge_digest: 'bad', props: '{}'}]->(f)",
        )
        out["operations"]["failed_edge_write_trigger"] = call(
            conn,
            "CREATE (n:Node {node_id: 'a', node_type: 'Duplicate', node_digest: 'duplicate', props: '{}'})",
        )
        out["operations"]["failed_edge_write_rollback"] = call(conn, "ROLLBACK")
        out["operations"]["edge_count_after_failed_write"] = call(
            conn,
            "MATCH ()-[r:Edge]->() RETURN count(r) AS count",
        )
        out["operations"]["failed_edge_absent_after_rollback"] = call(
            conn,
            "MATCH (e:Node {node_id: 'e'})-[r:Edge]->(f:Node {node_id: 'f'}) RETURN e.node_id, r.relation, f.node_id",
        )
        out["operations"]["node_rows_final"] = call(
            conn,
            "MATCH (n:Node) RETURN n.node_id, n.node_type, n.node_digest ORDER BY n.node_id",
        )
        out["operations"]["edge_rows_final"] = call(
            conn,
            "MATCH (a:Node)-[r:Edge]->(b:Node) RETURN a.node_id, r.relation, b.node_id, r.edge_digest ORDER BY a.node_id",
        )
    finally:
        conn.close()
        db.close()
    return out


def concurrent_worker(db_path, barrier, queue, worker_id):
    record = {"worker": worker_id}
    db = None
    conn = None
    try:
        db = ladybug.Database(db_path, enable_multi_writes=True)
        conn = ladybug.Connection(db)
        record["pid"] = os.getpid()
        record["database_options"] = {"enable_multi_writes": True}
        record["begin_api"] = "absent from Database/Connection public API"
        record["before_cas"] = call(
            conn,
            "MATCH (n:Node {node_id: 'concurrent'}) RETURN n.node_digest",
        )
        barrier.wait(timeout=20)
        record["cas"] = call(
            conn,
            "MATCH (n:Node {node_id: 'concurrent'}) WHERE n.node_digest = $expected SET n.node_type = $winner_type, n.node_digest = $winner_digest RETURN n.node_id, n.node_digest",
            {
                "expected": "shared",
                "winner_type": f"Winner{worker_id}",
                "winner_digest": f"claimed-{worker_id}",
            },
        )
        record["after_cas"] = call(
            conn,
            "MATCH (n:Node {node_id: 'concurrent'}) RETURN n.node_type, n.node_digest",
        )
    except Exception as exc:
        record["fatal"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
    finally:
        if conn is not None:
            conn.close()
        if db is not None:
            db.close()
        queue.put(record)


def concurrent_probe(root):
    db_path = root / "concurrent"
    db = ladybug.Database(db_path, enable_multi_writes=True)
    conn = ladybug.Connection(db)
    setup = {
        "node": call(
            conn,
            "CREATE NODE TABLE Node(node_id STRING, node_type STRING, node_digest STRING, props STRING, PRIMARY KEY(node_id))",
        ),
        "seed": call(
            conn,
            "CREATE (n:Node {node_id: 'concurrent', node_type: 'Initial', node_digest: 'shared', props: '{}'})",
        ),
    }
    conn.close()
    db.close()
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    processes = [ctx.Process(target=concurrent_worker, args=(str(db_path), barrier, queue, str(i))) for i in (1, 2)]
    for process in processes:
        process.start()
    records = []
    deadline = time.monotonic() + 25
    while len(records) < len(processes) and time.monotonic() < deadline:
        try:
            records.append(queue.get(timeout=1))
        except queue_module.Empty:
            if not any(process.is_alive() for process in processes):
                break
    for process in processes:
        process.join(timeout=2)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    records.sort(key=lambda item: item.get("worker", ""))
    db = ladybug.Database(db_path, read_only=True)
    conn = ladybug.Connection(db)
    final = call(conn, "MATCH (n:Node {node_id: 'concurrent'}) RETURN n.node_type, n.node_digest")
    conn.close()
    db.close()
    return {"setup": setup, "workers": records, "exitcodes": [p.exitcode for p in processes], "final": final}


def main():
    root = Path(tempfile.mkdtemp(prefix="probe-db-", dir=os.environ.get("ISSUE80_QUAL_ROOT")))
    try:
        output = {
            "platform": {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine()},
            "module": {"file": ladybug.__file__, "version": getattr(ladybug, "__version__", "unknown")},
            "api": api_inventory(),
            "single_connection": one_connection_probe(root),
            "concurrent_independent_process": concurrent_probe(root),
        }
        print(json.dumps(output, indent=2, sort_keys=True, default=str))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
