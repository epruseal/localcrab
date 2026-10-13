"""scripts/repair_store_mismatch_499.py (#499).

Every store is a real LocalGraphStore, LocalSQLDocStore, SQLStore (sqlite)
and SqliteVecStore under a pytest tmp_path, with a deterministic embedding
function. The live data directory is never opened.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import repair_store_mismatch_499 as rp  # noqa: E402

from tests._vec_helpers import MockEF  # noqa: E402

COLL = "vectors_kure"
DIM = 32
OLD = "2020-01-01T00:00:00+00:00"
DB_FILES = ("graph.db", "doc_store.db", "vectors.db", "opencrab.db")


def _open_stores(root: Path) -> rp.Stores:
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sql_store import SQLStore
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    return rp.Stores(
        LocalGraphStore(str(root / "graph.db")),
        LocalSQLDocStore(str(root / "doc_store.db")),
        SQLStore(f"sqlite:///{root / 'opencrab.db'}"),
        SqliteVecStore(db_path=str(root / "vectors.db"), embedding_function=MockEF(DIM), dim=DIM,
                       collection_name=COLL),
    )


def _close(stores: rp.Stores) -> None:
    for s in (stores.graph, stores.docs, stores.vec):
        s.close()
    stores.sql.close()


def _exec(path: Path, *statements: tuple) -> None:
    """Run statements in one committed transaction and close the connection
    (a leaked connection would delete the -wal and -shm files at a random
    later time)."""
    c = sqlite3.connect(path)
    try:
        with c:
            for st in statements:
                c.execute(*st)
    finally:
        c.close()


def _checkpoint(root: Path) -> None:
    for name in DB_FILES:
        c = sqlite3.connect(root / name)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()


class World:
    """Ids of every class plus controls, in one pack input root.

    Pack ``pk`` has an input file that covers its live graph. Pack ``pk2``
    has an input that misses a live graph node. Pack ``nopack`` has no input.
    """

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "data"
        self.root.mkdir()
        self.inputs = tmp / "inputs"
        s = _open_stores(self.root)
        g, d, q, v = s.graph, s.docs, s.sql, s.vec

        def node(space, gtype, dtype, nid, props, *, graph=True, doc=True, reg=None, vec=True):
            props = {"pack_id": "pk", **props}
            if graph:
                g.upsert_node(gtype, nid, props, space)
            if doc:
                d.upsert_node_doc(space, dtype, nid, props)
            q.register_node(space, reg or dtype, nid)
            if vec:
                v.upsert_texts(texts=[f"{nid} {dtype} text"], ids=[nid],
                               metadatas=[{"pack_id": props["pack_id"], "source": props["pack_id"],
                                           "node_id": nid, "space": space}])

        # controls
        node("concept", "Concept", "Concept", "ctl1", {"name": "control"})
        # P1: graph type invalid in the space, doc type valid
        node("evidence", "TextEvidence", "Evidence", "p1a", {"text": "a", "original_type": "TextEvidence"})
        node("evidence", "TextEvidence", "Evidence", "p1b", {"text": "b"})
        g.upsert_edge("TextEvidence", "p1a", "supports", "Concept", "ctl1", {})
        # P2: graph LogEntry, doc TextUnit with other properties
        for nid in ("p2a", "p2b"):
            g.upsert_node("LogEntry", nid, {"pack_id": "pk", "session_id": nid, "project": "x"}, "evidence")
            d.upsert_node_doc("evidence", "TextUnit", nid, {"pack_id": "pk", "text": "old body"})
            q.register_node("evidence", "TextUnit", nid)
            v.upsert_texts(texts=[f"{nid} TextUnit old body"], ids=[nid],
                           metadatas=[{"pack_id": "pk", "source": "pk", "node_id": nid, "space": "evidence"}])
        # P3: both valid
        node("concept", "Entity", "Concept", "p3a", {"name": "e"})
        # P4 delete proposals (absent from the covering input, older than the reload)
        node("concept", None, "Concept", "d1", {"name": "d1"}, graph=False, vec=False)
        node("concept", None, "Concept", "d2", {"name": "d2"}, graph=False, vec=False)
        q.register_edge("concept", "d1", "rel", "concept", "d2")      # shared by two deletes
        q.register_edge("concept", "d2", "rel", "concept", "h1")      # shared with a hold row
        # P4 hold rows
        node("concept", None, "Concept", "h1", {"pack_id": "nopack", "name": "h1"}, graph=False, vec=False)
        node("concept", None, "Concept", "n1", {"name": "newer"}, graph=False, vec=False)
        node("concept", None, "Concept", "dd", {"name": "two rows"}, graph=False, vec=False)
        d.upsert_node_doc("evidence", "Evidence", "dd", {"pack_id": "pk", "text": "second space"})
        # pack pk2: input misses a live graph node
        g.upsert_node("Concept", "g2", {"pack_id": "pk2"}, "concept")
        d.upsert_node_doc("concept", "Concept", "g2", {"pack_id": "pk2"})
        d.upsert_node_doc("concept", "Concept", "c1", {"pack_id": "pk2", "name": "c1"})
        _close(s)

        _exec(self.root / "doc_store.db",
              ("UPDATE doc_nodes SET updated_at=? WHERE node_id IN ('d1','d2','dd','h1','c1')", (OLD,)))
        _checkpoint(self.root)
        self.write_input("pk", ["ctl1", "p1a", "p1b", "p2a", "p2b", "p3a"])
        self.write_input("pk2", ["other"])

    def write_input(self, pack: str, ids: list[str]) -> None:
        p = self.inputs / pack
        p.mkdir(parents=True, exist_ok=True)
        (p / "nodes.jsonl").write_text(
            "".join(json.dumps({"id": i, "space": "concept", "node_type": "Concept"}) + "\n" for i in ids),
            encoding="utf-8")

    # --- helpers -------------------------------------------------------
    def stores(self) -> rp.Stores:
        return _open_stores(self.root)

    def dry(self, run: str = "dry") -> Path:
        rd = self.root.parent / run
        rp.dry_run(self.root, rd, self.inputs, COLL)
        return rd

    def apply(self, run: str, approved: Path | None = None, **kw) -> object:
        opened: list[rp.Stores] = []

        def factory() -> rp.Stores:
            opened.append(self.stores())
            return opened[-1]

        try:
            return rp.apply(self.root, self.root.parent / run, self.inputs, approved,
                            stores_factory=factory, vector_table=COLL, **kw)
        finally:
            for s in opened:
                _close(s)

    def proposed(self, run: str = "dry") -> Path:
        return self.root.parent / run / "proposed-approval.jsonl"

    def q(self, db: str, sql: str, params: tuple = ()) -> list:
        c = sqlite3.connect(self.root / db)
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()

    def file_state(self) -> dict:
        out = {}
        for name in DB_FILES:
            for suf in ("", "-wal", "-shm"):
                p = self.root / f"{name}{suf}"
                out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        return out

    def dump(self) -> dict:
        out = {}
        for name in DB_FILES:
            c = sqlite3.connect(self.root / name)
            for (t, sql) in c.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"):
                if t.startswith("sqlite_") or (sql or "").upper().startswith("CREATE VIRTUAL"):
                    continue
                out[f"{name}:{t}"] = sorted(map(repr, c.execute(f'SELECT * FROM "{t}"')))
            c.close()
        return out

    def vector_doc(self, nid: str) -> str | None:
        import sqlite_vec

        c = sqlite3.connect(self.root / "vectors.db")
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        try:
            r = c.execute(f"SELECT document FROM {COLL} WHERE node_id=?", (nid,)).fetchone()
            return r[0] if r else None
        finally:
            c.close()


def _progress(run: Path) -> list[dict]:
    return [json.loads(x) for x in (run / "progress.jsonl").read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _no_scan(_d: Path) -> tuple[list[int], int]:
    return [], 0


# --- T1, T9 ----------------------------------------------------------------


def test_t1_dry_run_classes_proposals_and_no_file_change(world, monkeypatch):
    from opencrab.stores import local_graph_store, local_sql_doc_store, sql_store, sqlite_vec_store

    def boom(*a, **k):
        raise AssertionError("dry run must not construct a store class")

    for mod, cls in ((local_graph_store, "LocalGraphStore"), (local_sql_doc_store, "LocalSQLDocStore"),
                     (sql_store, "SQLStore"), (sqlite_vec_store, "SqliteVecStore")):
        monkeypatch.setattr(getattr(mod, cls), "__init__", boom)
    before = world.file_state()
    rd = world.dry()
    assert world.file_state() == before
    cands = {r["node_id"]: r for r in map(json.loads, (rd / "candidates.jsonl").read_text().splitlines())}
    classes = {k: v["class"] for k, v in cands.items()}
    assert classes == {"p1a": "P1", "p1b": "P1", "p2a": "P2", "p2b": "P2", "p3a": "P3",
                       "d1": "P4", "d2": "P4", "h1": "P4", "n1": "P4", "dd": "P4", "c1": "P4"}
    # T9: only covered, absent, older, one-row rows get the delete proposal
    proposed = {json.loads(x)["node_id"] for x in world.proposed().read_text().splitlines()}
    assert proposed == {"d1", "d2"}
    assert cands["dd"]["evidence"]["doc_rows"] == 2 and cands["dd"]["proposal"] is None
    assert cands["c1"]["evidence"]["covers_graph"] is False
    assert cands["h1"]["evidence"]["input"] is None
    assert cands["n1"]["evidence"]["older_than_reload"] is False
    summary = json.loads((rd / "summary.json").read_text())
    assert summary["by_class"] == {"P1": 2, "P2": 2, "P3": 1, "P4": 6}
    assert [p["node_id"] for p in summary["p3"]] == ["p3a"]


def test_t1_non_empty_wal_refuses(world):
    (world.root / "graph.db-wal").write_bytes(b"x" * 64)
    with pytest.raises(rp.RepairRefusedError, match="not empty"):
        world.dry()


# --- T2, T3, T11 -----------------------------------------------------------


def test_t2_t3_t11_apply_reaches_targets_and_rerun_changes_nothing(world):
    world.dry()
    res = world.apply("run", world.proposed(), scan=rp.holders)
    assert all(o == "done" for (_, o) in res), res
    q = world.q
    # P1: graph type follows the doc; properties and edge type column follow
    assert q("graph.db", "SELECT node_type FROM graph_nodes WHERE node_id IN ('p1a','p1b') ORDER BY 1") == [
        ("Evidence",), ("Evidence",)]
    assert q("graph.db", "SELECT from_type FROM graph_edges WHERE from_id='p1a'") == [("Evidence",)]
    assert json.loads(q("graph.db", "SELECT properties FROM graph_nodes WHERE node_id='p1a'")[0][0])[
        "original_type"] == "TextEvidence"
    # P2: doc, registry and vector take the graph type and properties
    dt, dp = q("doc_store.db", "SELECT node_type, properties FROM doc_nodes WHERE node_id='p2a'")[0]
    assert dt == "LogEntry" and json.loads(dp) == {"pack_id": "pk", "session_id": "p2a", "project": "x"}
    assert q("opencrab.db", "SELECT node_type FROM ontology_nodes WHERE node_id='p2a'") == [("LogEntry",)]
    assert "LogEntry" in world.vector_doc("p2a")
    # P4 delete: doc, registry node and registry edge rows gone
    for nid in ("d1", "d2"):
        assert q("doc_store.db", "SELECT 1 FROM doc_nodes WHERE node_id=?", (nid,)) == []
        assert q("opencrab.db", "SELECT 1 FROM ontology_nodes WHERE node_id=?", (nid,)) == []
    assert q("opencrab.db", "SELECT count(*) FROM ontology_edges") == [(0,)]
    # untouched: P3, hold rows, controls
    assert q("graph.db", "SELECT node_type FROM graph_nodes WHERE node_id='p3a'") == [("Entity",)]
    for nid in ("h1", "n1", "dd", "c1", "ctl1"):
        assert q("doc_store.db", "SELECT 1 FROM doc_nodes WHERE node_id=?", (nid,)) != []
    assert q("opencrab.db", "SELECT 1 FROM ontology_nodes WHERE node_id='h1'") == [(1,)]
    # T11: the shared edge row is deleted once; the other id reads "done (already)"
    shared = [p for p in _progress(world.root.parent / "run") if p["leg"].startswith("regedge:")
              and p["node_id"] in ("d1", "d2")]
    by_key: dict[str, list[str]] = {}
    for p in shared:
        by_key.setdefault(p["leg"], []).append(p["outcome"])
    assert sorted(map(sorted, by_key.values())) == [["done"], ["done", "done (already)"]]
    # T3: rerun on the same run dir changes no row
    before = world.dump()
    n_first = len(_progress(world.root.parent / "run"))
    res2 = world.apply("run", world.proposed(), scan=rp.holders)
    assert all(o == "done" for (_, o) in res2), res2
    assert world.dump() == before
    second = _progress(world.root.parent / "run")[n_first:]
    assert second and all(p["outcome"] == "done (already)" for p in second)


# --- T4 --------------------------------------------------------------------


def test_t4_resume_after_crash_and_after_lost_commit(world, monkeypatch):
    world.dry()
    real = rp.execute_leg
    writes: list[tuple[str, str]] = []

    def crashing(stores, reader, node_id, leg, recorded):
        if (node_id, leg["op"]) in (("p2a", "vector_upsert"), ("d1", "reg_delete")):
            raise RuntimeError("injected crash")
        writes.append((node_id, leg["key"]))
        return real(stores, reader, node_id, leg, recorded)

    monkeypatch.setattr(rp, "execute_leg", crashing)
    res = world.apply("run", world.proposed(), scan=_no_scan)
    assert res[("P2", "error")] == 1 and res[("P4", "error")] >= 1
    assert world.q("doc_store.db", "SELECT node_type FROM doc_nodes WHERE node_id='p2a'") == [("LogEntry",)]

    def counting(stores, reader, node_id, leg, recorded):
        writes.append((node_id, leg["key"]))
        return real(stores, reader, node_id, leg, recorded)

    writes.clear()
    monkeypatch.setattr(rp, "execute_leg", counting)
    res = world.apply("run", world.proposed(), scan=_no_scan)
    assert all(o == "done" for (_, o) in res), res
    assert ("p2a", "doc:evidence") not in writes and ("p2a", "vector") in writes
    assert ("d1", "doc:concept") not in writes and ("d1", "reg:concept") in writes
    assert not any(n in ("p1a", "p1b") for n, _ in writes)
    # lost commit: one store goes back to the recorded value, a later leg survives
    _exec(world.root / "opencrab.db", ("UPDATE ontology_nodes SET node_type='TextUnit' WHERE node_id='p2a'",))
    writes.clear()
    res = world.apply("run", world.proposed(), scan=_no_scan)
    assert all(o == "done" for (_, o) in res), res
    assert writes == [("p2a", "reg:evidence")]
    assert world.q("opencrab.db", "SELECT node_type FROM ontology_nodes WHERE node_id='p2a'") == [("LogEntry",)]


# --- T5 --------------------------------------------------------------------


def test_t5_snapshot_check(tmp_path):
    world = World(tmp_path)
    rd = world.dry("run")
    _exec(world.root / "graph.db",
          ("UPDATE graph_nodes SET properties=json_set(properties,'$.text','changed') WHERE node_id='p1b'",),
          ("UPDATE graph_nodes SET properties=json_set(properties,'$.project','y') WHERE node_id='p2b'",))
    _exec(world.root / "doc_store.db",
          ("UPDATE doc_nodes SET updated_at='2021-01-01T00:00:00+00:00' WHERE node_id='d1'",),
          ("INSERT INTO doc_nodes (space,node_id,node_type,properties,updated_at) "
           "VALUES ('concept','p2a','Concept','{}','2021-01-01')",))
    _exec(world.root / "opencrab.db",
          ("UPDATE ontology_edges SET relation='changed' WHERE from_id='d2' AND to_id='h1'",))
    before = world.dump()
    res = world.apply("run", world.proposed("run"), scan=_no_scan)
    outcomes = {p["node_id"]: p["outcome"] for p in _progress(rd) if p["outcome"] == "conflict"}
    assert {"p1b", "p2a", "p2b", "d1", "d2"} <= set(outcomes)
    assert res[("P1", "done")] == 1  # p1a still runs
    after = world.dump()
    for key in ("graph.db:graph_nodes",):
        changed = {r for r in after[key] if "'p1b'" in r or "'p2b'" in r}
        assert changed == {r for r in before[key] if "'p1b'" in r or "'p2b'" in r}
    assert world.q("doc_store.db", "SELECT updated_at FROM doc_nodes WHERE node_id='d1'") == [
        ("2021-01-01T00:00:00+00:00",)]
    assert world.q("opencrab.db", "SELECT relation FROM ontology_edges WHERE from_id='d2'") == [("changed",)]
    assert world.q("doc_store.db", "SELECT node_type FROM doc_nodes WHERE node_id='p2a' ORDER BY space") == [
        ("Concept",), ("TextUnit",)]


# --- T6 --------------------------------------------------------------------


@pytest.mark.parametrize("lines", [
    [{"node_id": "p3a", "action": "delete"}],
    [{"node_id": "h1", "action": "delete"}],
    [{"node_id": "dd", "action": "delete"}],
    [{"node_id": "d1", "action": "promote"}],
    [{"node_id": "d1", "action": "delete"}, {"node_id": "d1", "action": "delete"}],
    [{"node_id": "p1a", "action": "delete"}],
])
def test_t6_approval_refusals_write_nothing(world, lines):
    world.dry()
    path = world.root.parent / "approved.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    before = world.dump()
    with pytest.raises(rp.RepairRefusedError, match="approval line"):
        world.apply("run", path, scan=_no_scan)
    assert world.dump() == before


# --- T7 --------------------------------------------------------------------


def test_t7_guard_refuses_when_another_process_holds_a_file(world):
    proc = subprocess.Popen([sys.executable, "-c",
                             "import sys,time; f=open(sys.argv[1],'rb'); print('ok',flush=True); time.sleep(60)",
                             str(world.root / "graph.db")], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ok"
        before = world.dump()
        with pytest.raises(rp.RepairRefusedError) as exc:
            world.apply("run", None)
        assert exc.value.code == rp.EXIT_GUARD
        assert world.dump() == before
        code = rp.main(["--local-data-dir", str(world.root), "--run-dir", str(world.root.parent / "g"),
                        "--pack-input-root", str(world.inputs), "--check-guard"])
        assert code == rp.EXIT_GUARD
        world.dry()  # a dry run still works
    finally:
        proc.kill()
        proc.wait()
    assert rp.main(["--local-data-dir", str(world.root), "--run-dir", str(world.root.parent / "g"),
                    "--pack-input-root", str(world.inputs), "--check-guard"]) == rp.EXIT_OK


# --- T8 --------------------------------------------------------------------


def test_t8_graph_node_after_selection_keeps_the_doc_row(world):
    world.dry()
    s = world.stores()
    s.graph.upsert_node("Concept", "d1", {"pack_id": "pk", "name": "d1"}, "concept")
    _close(s)
    # resume on the dry-run dir: no new selection, the recorded state applies
    world.apply("dry", world.proposed(), scan=_no_scan)
    assert world.q("doc_store.db", "SELECT 1 FROM doc_nodes WHERE node_id='d1'") == [(1,)]
    assert any(p["node_id"] == "d1" and p["outcome"] == "conflict" for p in _progress(world.root.parent / "dry"))


def test_t8_input_change_after_first_p4_write_stops_later_writes(world, monkeypatch):
    world.dry()
    real = rp.execute_leg
    state = {"done": False}

    def changing(stores, reader, node_id, leg, recorded):
        out = real(stores, reader, node_id, leg, recorded)
        if leg["op"] == "doc_delete" and not state["done"]:
            state["done"] = True
            time.sleep(0.01)
            world.write_input("pk", ["ctl1", "p1a", "p1b", "p2a", "p2b", "p3a", "new"])
        return out

    monkeypatch.setattr(rp, "execute_leg", changing)
    world.apply("run", world.proposed(), scan=_no_scan)
    left = world.q("doc_store.db", "SELECT node_id FROM doc_nodes WHERE node_id IN ('d1','d2')")
    assert len(left) == 1
    reasons = [p["detail"] for p in _progress(world.root.parent / "run") if p["outcome"] == "conflict"]
    assert "pack input file changed" in reasons


# --- T10 -------------------------------------------------------------------


def test_t10_resume_requires_a_matching_manifest(world):
    rd = world.dry("run")
    (rd / "manifest.json").unlink()
    with pytest.raises(rp.RepairRefusedError, match="without a manifest"):
        world.apply("run", None, scan=_no_scan)
    rd2 = world.dry("run2")
    m = json.loads((rd2 / "manifest.json").read_text())
    m["candidates_sha256"] = "0" * 64
    (rd2 / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(rp.RepairRefusedError, match="does not match"):
        world.apply("run2", None, scan=_no_scan)
