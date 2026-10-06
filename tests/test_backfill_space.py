"""scripts/backfill_space.py (#110 part A).

Every store is built under a pytest tmp_path with real SqliteVecStore,
LocalSQLDocStore and a SQLite graph.db. The live data directory is never opened.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import backfill_space as bf  # noqa: E402

from tests._vec_helpers import MockEF  # noqa: E402

COLL = "vectors_kure"
PACK = "pk"
_EMB = struct.pack("32f", *([0.1] * 32))


class World:
    """A local data directory with graph.db, doc_store.db and vectors.db."""

    def __init__(self, root: Path, legacy_graph: bool = False) -> None:
        self.root = root
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        self.docs = LocalSQLDocStore(str(root / "doc_store.db"))
        self.vec = SqliteVecStore(
            db_path=str(root / "vectors.db"), embedding_function=MockEF(32), dim=32,
            collection_name=COLL)
        g = sqlite3.connect(root / "graph.db")
        pk = "PRIMARY KEY (node_type, node_id)" if legacy_graph else "PRIMARY KEY (node_id)"
        g.execute(
            "CREATE TABLE graph_nodes (node_type TEXT NOT NULL, node_id TEXT NOT NULL, "
            f"space_id TEXT, properties TEXT NOT NULL DEFAULT '{{}}', {pk})")
        g.commit()
        g.close()

    def graph(self, nid, space_id, props, node_type="Entity"):
        g = sqlite3.connect(self.root / "graph.db")
        text = props if isinstance(props, str) else json.dumps(props)
        g.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", (node_type, nid, space_id, text))
        g.commit()
        g.close()

    def doc(self, sid, meta, text="본문"):
        """Insert one doc row; a str meta is stored as the exact JSON text."""
        self.docs.upsert_source(sid, text, meta if isinstance(meta, dict) else {})
        if isinstance(meta, str):
            d = sqlite3.connect(self.root / "doc_store.db")
            d.execute("UPDATE doc_sources SET metadata=? WHERE source_id=?", (meta, sid))
            d.commit()
            d.close()

    def vector(self, vid, meta, partition="__same__", document="doc text"):
        """Insert one vector row; metadata is written as the exact JSON given."""
        if partition == "__same__":
            partition = meta.get("pack_id", PACK) if isinstance(meta, dict) else PACK
        conn = bf._open(self.root / "vectors.db", "rw", True)
        text = meta if isinstance(meta, str) or meta is None else json.dumps(meta, ensure_ascii=False)
        conn.execute(
            f"INSERT INTO {COLL}(node_id, pack_id, embedding, document, metadata) VALUES (?,?,?,?,?)",
            (vid, partition, _EMB, document, text))
        conn.close()

    def dump(self):
        out = {}
        d = sqlite3.connect(self.root / "doc_store.db")
        out["doc"] = dict(d.execute("SELECT source_id, metadata FROM doc_sources"))
        d.close()
        v = bf._open(self.root / "vectors.db", "ro", True)
        out["vec"] = {r[0]: (r[1], r[2], r[3], r[4]) for r in v.execute(
            f"SELECT node_id, pack_id, metadata, document, embedding FROM {COLL}")}
        v.close()
        g = sqlite3.connect(self.root / "graph.db")
        out["graph"] = g.execute("SELECT node_type, node_id, space_id, properties FROM graph_nodes").fetchall()
        g.close()
        return out

    def stat(self):
        """Size and mtime of the three main files (the dry run must not change them)."""
        return {n: (p.stat().st_size, p.stat().st_mtime_ns)
                for n in ("graph.db", "doc_store.db", "vectors.db")
                for p in [self.root / n]}

    def space(self, store, rid):
        rows = self.dump()["doc" if store == "doc" else "vec"]
        raw = rows[rid] if store == "doc" else rows[rid][1]
        return json.loads(raw).get("space") if raw else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("LOCAL_DATA_DIR", str(root))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def world(env):
    return World(env)


def _run(capsys, *argv, **kw):
    code = bf.main(list(argv), **kw)
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip().startswith("{") else out)


def _apply(capsys, world, tmp_path, *extra, **kw):
    dest = tmp_path / "bk"
    dest.mkdir(parents=True, exist_ok=True)
    return _run(capsys, "--apply", "--backup-to", str(dest), *extra, **kw)


def _mixed(world):
    """One record of most classes."""
    world.graph("n1", "concept", {"pack_id": PACK})
    world.graph("n2", "resource", {"pack_id": PACK, "space": "resource"})
    world.graph("n-other", "concept", {"pack_id": "other"})
    world.graph("n-nopack", "concept", {})
    world.doc("n1", {"pack_id": PACK})
    world.doc("d-unmapped", {"pack_id": PACK})
    world.doc("d-valid", {"pack_id": PACK, "space": "claim"})
    world.vector("n1", {"pack_id": PACK})
    world.vector("n2", {"pack_id": PACK, "space": ""})
    world.vector("v-orphan", {"pack_id": PACK})
    world.vector("v-src", {"pack_id": PACK, "source_id": "x"})
    world.vector("d-unmapped", {"pack_id": PACK})
    world.vector("n-other", {"pack_id": PACK})


# ---------------------------------------------------------------------------
# pure functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, kind", [
    (None, "null"), ("", "empty"), (5, "non_string"), (True, "non_string"),
    ([], "non_string"), ({"a": 1}, "non_string"), ("[]", "container"),
    (" {}", "container"), ("[\"x\"]", "container"), ("concept", "valid"),
])
def test_value_kind(value, kind):
    space, k = bf.value_kind(value)
    assert k == kind
    assert (space is not None) == (kind == "valid")


def _g(space="concept", pack=PACK, ambiguous=False, props_bad=False):
    return {"space": space, "pack": pack, "ambiguous": ambiguous, "props_bad": props_bad}


@pytest.mark.parametrize("store, meta, part, graph, docs, allow, expect", [
    ("doc", None, None, {}, {}, False, ("hold_bad_meta", None)),
    ("doc", {"space": "claim"}, None, {"i": _g("concept")}, {}, False, ("skip_valid", "claim")),
    ("doc", {"pack_id": PACK}, None, {"i": _g()}, {}, False, ("apply_graph", "concept")),
    ("doc", {"pack_id": PACK}, None, {"i": _g(pack="o")}, {}, False, ("hold_pack_disagrees", None)),
    ("doc", {}, None, {"i": _g()}, {}, False, ("hold_pack_missing", None)),
    ("doc", {}, None, {"i": _g()}, {}, True, ("apply_graph", "concept")),
    ("doc", {"pack_id": PACK}, None, {"i": _g(pack=None)}, {}, False, ("hold_pack_missing", None)),
    ("doc", {"pack_id": PACK}, None, {"i": _g(ambiguous=True)}, {}, True, ("hold_graph_ambiguous", None)),
    ("doc", {"pack_id": PACK}, None, {"i": _g(props_bad=True)}, {}, True, ("hold_graph_props_bad", None)),
    ("doc", {"pack_id": PACK}, None, {"i": _g(space=None)}, {}, True, ("hold_graph_space_missing", None)),
    ("doc", {"pack_id": PACK}, None, {}, {}, False, ("apply_source_default", "evidence")),
    ("vector", {"pack_id": "p"}, "q", {}, {}, False, ("hold_pack_partition_mismatch", None)),
    ("vector", {}, None, {}, {}, False, ("hold_orphan", None)),
    ("vector", {"source_id": "s"}, None, {}, {}, False, ("apply_source_default", "evidence")),
    ("vector", {"pack_id": PACK}, PACK, {}, {"i": (PACK, "concept")}, False, ("apply_source_default", "concept")),
    ("vector", {"pack_id": PACK}, PACK, {}, {"i": ("x", "concept")}, False, ("hold_doc_pair_pack_differs", None)),
    ("vector", {"pack_id": PACK}, PACK, {}, {"i": (PACK, None)}, False, ("hold_doc_pair_held", None)),
    ("vector", {"pack_id": PACK}, PACK, {"i": _g("resource")}, {"i": (PACK, "concept")}, False,
     ("apply_graph", "resource")),
])
def test_decide(store, meta, part, graph, docs, allow, expect):
    assert bf.decide(store, "i", meta, part, graph, docs, allow) == expect


# ---------------------------------------------------------------------------
# dry run and CLI contract
# ---------------------------------------------------------------------------


def test_dry_run_changes_no_content_and_reports(world, capsys):
    _mixed(world)
    before, stat_before = world.dump(), world.stat()
    code, rep = _run(capsys)
    assert code == 0 and rep["mode"] == "dry-run"
    assert world.dump() == before  # includes the graph rows
    assert world.stat() == stat_before
    assert rep["doc"]["classes"]["apply_graph"] == 1
    assert rep["vector"]["classes"]["hold_pack_disagrees"] == 1
    assert rep["vector"]["classes"]["hold_orphan"] == 1
    assert rep["vector"]["planned_space_distribution"]


def test_dry_run_does_not_wait_for_write_lock(world, env, capsys):
    _mixed(world)
    holder = _hold_lock(env)
    try:
        code, _ = _run(capsys)
    finally:
        holder.kill()
        holder.wait()
    assert code == 0


def test_apply_requires_backup(world, capsys):
    _mixed(world)
    before = world.dump()
    assert bf.main(["--apply"]) == 2
    assert bf.main(["--backup-to", "x"]) == 2
    capsys.readouterr()
    assert world.dump() == before


def test_unsupported_mode_exits_2(world, monkeypatch, capsys):
    monkeypatch.setenv("STORAGE_MODE", "docker")
    assert bf.main([]) == 2


def _hold_lock(data_dir: Path):
    code = textwrap.dedent(f"""
        import sys, time
        from opencrab.locking import write_lock
        with write_lock({str(data_dir)!r}):
            print("held", flush=True)
            time.sleep(120)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_apply_exits_3_when_lock_busy_and_writes_nothing(world, env, tmp_path, capsys):
    _mixed(world)
    before = world.dump()
    holder = _hold_lock(env)
    try:
        code, out = _apply(capsys, world, tmp_path, "--lock-timeout", "0.5")
    finally:
        holder.kill()
        holder.wait()
    assert code == 3
    assert world.dump() == before
    assert not list((tmp_path / "bk").iterdir())


# ---------------------------------------------------------------------------
# apply end to end
# ---------------------------------------------------------------------------


def test_apply_end_to_end(world, tmp_path, capsys):
    _mixed(world)
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert rep["reconcile"]["ok"], rep["reconcile"]
    assert world.space("doc", "n1") == "concept"            # mapped to graph
    assert world.space("doc", "d-unmapped") == "evidence"    # source default
    assert world.space("doc", "d-valid") == "claim"          # valid kept
    assert world.space("vec", "n1") == "concept"
    assert world.space("vec", "n2") == "resource"            # empty replaced
    assert world.space("vec", "v-src") == "evidence"
    assert world.space("vec", "d-unmapped") == "evidence"    # follows its doc row
    after = world.dump()
    # held records are byte-identical, text and embeddings untouched everywhere
    for held in ("v-orphan", "n-other"):
        assert after["vec"][held] == before["vec"][held]
    for rid, (part, _meta, document, emb) in before["vec"].items():
        assert emb == _EMB  # the dump really carries the embedding
        assert (after["vec"][rid][0], after["vec"][rid][2], after["vec"][rid][3]) == (part, document, emb)
    # a second dry run has only held records left
    code, rep2 = _run(capsys)
    assert code == 0
    for store in ("doc", "vector"):
        assert not [c for c in rep2[store]["classes"] if c.startswith("apply")]


def test_queries_with_spaces_find_backfilled_records(world, tmp_path, capsys):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", {"pack_id": PACK}, text="사과 본문")
    world.vector("n1", {"pack_id": PACK})
    assert world.docs.keyword_search("사과", pack_ids=[PACK], spaces=["concept"]) == []
    assert world.vec.query("x", 5, where={"space": {"$in": ["concept"]}}) == []
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert [h["source_id"] if "source_id" in h else h.get("id")
            for h in world.docs.keyword_search("사과", pack_ids=[PACK], spaces=["concept"])] == ["n1"]
    assert [h["id"] for h in world.vec.query("x", 5, where={"space": {"$in": ["concept"]}})] == ["n1"]


def test_valid_space_is_kept_even_when_graph_differs(world, tmp_path, capsys):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", {"pack_id": PACK, "space": "claim"})
    world.vector("n1", {"pack_id": PACK, "space": "claim"})
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0
    assert rep["before"]["space_differs_from_graph"] == {"doc": 1, "vector": 1}
    assert world.space("doc", "n1") == "claim" and world.space("vec", "n1") == "claim"


@pytest.mark.parametrize("bad", ['"[]"', '"{}"', "5", "null", '""', "true", '["x"]', '{"a":1}'])
def test_invalid_values_are_replaced(world, tmp_path, capsys, bad):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", {"pack_id": PACK})
    world.vector("n1", f'{{"pack_id": "{PACK}", "space": {bad}}}')
    d = sqlite3.connect(world.root / "doc_store.db")
    d.execute("UPDATE doc_sources SET metadata=? WHERE source_id='n1'",
              (f'{{"pack_id": "{PACK}", "space": {bad}}}',))
    d.commit()
    d.close()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert world.space("doc", "n1") == "concept" and world.space("vec", "n1") == "concept"
    assert sum(rep["before"]["vector"]["invalid_kinds"].values()) == 1


def test_graph_container_space_falls_back_to_column(world, tmp_path, capsys):
    world.graph("n1", "resource", {"pack_id": PACK, "space": "[]"})
    world.doc("n1", {"pack_id": PACK})
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0 and rep["before"]["graph"]["graph_space_container_like"] == 1
    assert world.space("doc", "n1") == "resource"
    code, rep2 = _run(capsys)
    assert not [c for c in rep2["doc"]["classes"] if c.startswith("apply")]  # no oscillation


def test_graph_without_usable_space_is_held(world, tmp_path, capsys):
    world.graph("n1", None, {"pack_id": PACK, "space": "[]"})
    world.doc("n1", {"pack_id": PACK})
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0 and rep["before"]["doc"]["classes"]["hold_graph_space_missing"] == 1
    assert world.dump() == before


def test_graph_properties_bad_never_relaxed(world, tmp_path, capsys):
    world.graph("n1", "concept", "not json")
    world.doc("n1", {"pack_id": PACK})
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path, "--allow-pack-missing")
    assert code == 0 and rep["before"]["doc"]["classes"]["hold_graph_props_bad"] == 1
    assert world.dump() == before


def test_pack_missing_held_then_allowed(world, tmp_path, capsys):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", {})
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert rep["before"]["doc"]["classes"]["hold_pack_missing"] == 1
    assert world.dump() == before
    code, rep = _apply(capsys, world, tmp_path, "--allow-pack-missing")
    assert code == 0 and world.space("doc", "n1") == "concept"


def test_duplicate_graph_ids_in_legacy_schema(env, tmp_path, capsys):
    w = World(env, legacy_graph=True)
    w.graph("n1", "concept", {"pack_id": PACK}, node_type="A")
    w.graph("n1", "resource", {"pack_id": PACK}, node_type="B")
    w.graph("n2", "concept", {"pack_id": PACK}, node_type="A")
    w.graph("n2", "concept", {"pack_id": PACK}, node_type="B")
    w.doc("n1", {"pack_id": PACK})
    w.doc("n2", {"pack_id": PACK})
    code, rep = _apply(capsys, w, tmp_path)
    assert code == 0
    assert rep["before"]["doc"]["classes"]["hold_graph_ambiguous"] == 1
    assert w.space("doc", "n1") is None and w.space("doc", "n2") == "concept"


def test_vector_partition_mismatch_is_held(world, tmp_path, capsys):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.vector("n1", {"pack_id": PACK}, partition="q")
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert rep["before"]["vector"]["classes"]["hold_pack_partition_mismatch"] == 1
    assert world.dump() == before


def test_unmapped_vector_follows_doc_space_and_pair_rules(world, tmp_path, capsys):
    world.doc("a", {"pack_id": PACK, "space": "concept"})
    world.vector("a", {"pack_id": PACK})
    world.doc("b", {"pack_id": "other", "space": "concept"})
    world.vector("b", {"pack_id": PACK})
    world.graph("h", "concept", {"pack_id": "zzz"})
    world.doc("h", {"pack_id": PACK})            # held doc (graph pack disagrees)
    world.vector("h-twin", {"pack_id": PACK, "source_id": "h"})
    world.doc("c", {"pack_id": PACK})
    world.vector("c", {"pack_id": PACK})
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert world.space("vec", "a") == "concept"
    assert world.space("vec", "b") is None
    assert rep["before"]["vector"]["classes"]["hold_doc_pair_pack_differs"] == 1
    assert world.space("vec", "c") == "evidence"


def test_null_partition_vector_is_updated(world, tmp_path, capsys):
    world.graph("n1", "concept", {})
    world.vector("n1", {}, partition=None)
    code, rep = _apply(capsys, world, tmp_path, "--allow-pack-missing")
    assert code == 0, rep
    assert world.space("vec", "n1") == "concept"
    assert world.dump()["vec"]["n1"][0] is None


# ---------------------------------------------------------------------------
# batches, failure, resume, restore
# ---------------------------------------------------------------------------


def _many(world, n=7):
    for i in range(n):
        world.graph(f"n{i}", "concept", {"pack_id": PACK})
        world.doc(f"n{i}", {"pack_id": PACK})
        world.vector(f"n{i}", {"pack_id": PACK})


def test_batch_failure_rolls_back_that_batch_and_rerun_converges(env, tmp_path, capsys):
    ref = World(env)
    _many(ref)
    ref_dump = None
    # single clean run for the reference state
    code, _ = _apply(capsys, ref, tmp_path / "a", "--batch-size", "3")
    assert code == 0
    ref_dump = ref.dump()
    # fresh world, failure at the second batch, then rerun
    shutil.rmtree(env)
    env.mkdir()
    from opencrab.config import get_settings

    get_settings.cache_clear()
    w = World(env)
    _many(w)

    def boom(store, idx):
        if idx == 1:
            raise RuntimeError("injected")

    code, rep = _apply(capsys, w, tmp_path / "b", "--batch-size", "3", before_commit=boom)
    assert code == 1 and "injected" in rep["error"]
    mid = w.dump()
    assert sum(1 for m in mid["doc"].values() if json.loads(m).get("space")) == 3  # first batch kept
    code, rep = _apply(capsys, w, tmp_path / "b", "--batch-size", "3")
    assert code == 0 and rep["reconcile"]["ok"]
    assert w.dump() == ref_dump


def test_max_batches_then_rerun(world, tmp_path, capsys):
    _many(world)
    code, rep = _apply(capsys, world, tmp_path, "--batch-size", "2", "--max-batches", "1")
    assert code == 0 and rep["write"]["batches"] == 1
    assert rep["write"]["remaining"]["doc"] > 0 and rep["reconcile"]["ok"]
    for _ in range(10):
        code, rep = _apply(capsys, world, tmp_path, "--batch-size", "2", "--max-batches", "1")
        if rep["write"]["batches"] == 0:
            break
    dump = world.dump()
    assert all(json.loads(m).get("space") for m in dump["doc"].values())
    assert all(json.loads(v[1]).get("space") for v in dump["vec"].values())


def test_changed_row_is_skipped_by_reread(world, tmp_path, capsys):
    _many(world, 2)

    # A row that gains a valid space between plan and write is not overwritten.
    orig = bf._write_one_batch

    def wrapped(conn, store, batch, paths, res, allow, stats):
        if store == "doc":
            conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id='n0'",
                         (json.dumps({"pack_id": PACK, "space": "claim"}),))
        return orig(conn, store, batch, paths, res, allow, stats)

    bf._write_one_batch = wrapped
    try:
        code, rep = _apply(capsys, world, tmp_path)
    finally:
        bf._write_one_batch = orig
    assert world.space("doc", "n0") == "claim"
    assert rep["write"]["skipped_changed"] == 1
    assert code == 1  # reconcile sees the foreign write as a mismatch


def test_backup_restore_with_documented_procedure(world, env, tmp_path, capsys):
    _many(world)
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0
    assert world.dump() != before
    backup = Path(rep["backup_set"])
    world.docs.close()  # the documented procedure stops every process that opens the files
    world.vec.close()
    for name in ("doc_store.db", "vectors.db"):
        for side in ("-wal", "-shm"):
            (env / (name + side)).unlink(missing_ok=True)
        shutil.copyfile(backup / name, env / name)
    assert world.dump() == before


def test_apply_text_and_fts_untouched(world, tmp_path, capsys):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", {"pack_id": PACK}, text="바나나 본문")
    d = sqlite3.connect(world.root / "doc_store.db")
    fts_before = d.execute("SELECT source_id, text FROM doc_sources_fts").fetchall()
    d.close()
    code, _ = _apply(capsys, world, tmp_path)
    assert code == 0
    d = sqlite3.connect(world.root / "doc_store.db")
    assert d.execute("SELECT text FROM doc_sources WHERE source_id='n1'").fetchone()[0] == "바나나 본문"
    assert d.execute("SELECT source_id, text FROM doc_sources_fts").fetchall() == fts_before
    d.close()


def test_reconcile_detects_a_held_record_changing_class(world, tmp_path, capsys):
    _many(world, 1)
    world.graph("h", "concept", {"pack_id": "zzz"})
    world.doc("h", {"pack_id": PACK})          # hold_pack_disagrees
    orig = bf._write_one_batch

    def wrapped(conn, store, batch, paths, res, allow, stats):
        done = orig(conn, store, batch, paths, res, allow, stats)
        if store == "doc":
            conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id='h'", ('{"x": 1}',))
        return done

    bf._write_one_batch = wrapped
    try:
        code, rep = _apply(capsys, world, tmp_path)
    finally:
        bf._write_one_batch = orig
    assert code == 1
    assert any("hold class counts changed" in p for p in rep["reconcile"]["problems"])


def test_reconcile_detects_two_held_records_swapping_class(world, tmp_path, capsys):
    _many(world, 1)
    world.graph("h1", "concept", {"pack_id": "zzz"})
    world.graph("h2", "concept", {"pack_id": "zzz"})
    world.doc("h1", {"pack_id": PACK})          # hold_pack_disagrees
    world.doc("h2", {})                          # hold_pack_missing
    orig = bf._write_one_batch

    def wrapped(conn, store, batch, paths, res, allow, stats):
        done = orig(conn, store, batch, paths, res, allow, stats)
        if store == "doc":
            conn.execute("UPDATE doc_sources SET metadata='{}' WHERE source_id='h1'")
            conn.execute("UPDATE doc_sources SET metadata=? WHERE source_id='h2'", ('{"pack_id": "pk"}',))
        return done

    bf._write_one_batch = wrapped
    try:
        code, rep = _apply(capsys, world, tmp_path)
    finally:
        bf._write_one_batch = orig
    assert code == 1
    assert any("held id changed class" in p for p in rep["reconcile"]["problems"])


_NON_OBJECT_TEXTS = ["null", "5", "[]", "true", '"s"', "not json"]


@pytest.mark.parametrize("text", _NON_OBJECT_TEXTS)
def test_non_object_metadata_is_held_unchanged(world, tmp_path, capsys, text):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", {"pack_id": PACK})
    world.vector("n1", text, partition=PACK)
    d = sqlite3.connect(world.root / "doc_store.db")
    d.execute("UPDATE doc_sources SET metadata=? WHERE source_id='n1'", (text,))
    d.commit()
    d.close()
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert rep["before"]["doc"]["classes"]["hold_bad_meta"] == 1
    assert rep["before"]["vector"]["classes"]["hold_bad_meta"] == 1
    assert world.dump() == before


def test_missing_metadata_text_reads_as_empty_object(world, tmp_path, capsys):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.vector("n1", None, partition="")   # SQL NULL metadata
    code, rep = _apply(capsys, world, tmp_path, "--allow-pack-missing")
    assert code == 0, rep
    assert world.space("vec", "n1") == "concept"


@pytest.mark.parametrize("props", ["", "null", "5", "[]", "not json"])
def test_graph_properties_non_object_is_corrupt_and_never_relaxed(world, tmp_path, capsys, props):
    world.graph("n1", "concept", props)
    world.doc("n1", {"pack_id": PACK})
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path, "--allow-pack-missing")
    assert code == 0
    assert rep["before"]["doc"]["classes"]["hold_graph_props_bad"] == 1
    assert world.dump() == before


@pytest.mark.parametrize("bad", ["0", "false", "[]", '{"a": 1}'])
def test_non_string_pack_id_is_held(world, tmp_path, capsys, bad):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", f'{{"pack_id": {bad}}}')
    world.doc("u1", f'{{"pack_id": {bad}}}')                 # unmapped doc
    world.vector("n1", f'{{"pack_id": {bad}}}', partition="")
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path, "--allow-pack-missing")
    assert code == 0, rep
    assert rep["before"]["doc"]["classes"]["hold_pack_invalid"] == 2
    assert rep["before"]["vector"]["classes"]["hold_pack_invalid"] == 1
    assert world.dump() == before


def test_null_pack_id_still_reads_as_empty(world, tmp_path, capsys):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", '{"pack_id": null}')
    code, rep = _apply(capsys, world, tmp_path)
    assert rep["before"]["doc"]["classes"]["hold_pack_missing"] == 1


@pytest.mark.parametrize("bad", ["0", "false", "[]", '{"a": 1}'])
def test_doc_with_valid_space_and_invalid_pack_does_not_pair(world, tmp_path, capsys, bad):
    world.doc("a", f'{{"pack_id": {bad}, "space": "concept"}}')
    world.vector("a", {}, partition="")
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert rep["before"]["vector"]["classes"]["hold_doc_pair_pack_differs"] == 1
    assert world.space("vec", "a") is None


@pytest.mark.parametrize("props", [
    '{"pack_id": "other", "pack_id": "pk", "space": "claim", "space": "concept"}',
    '{"pack_id": "pk", "space": NaN}',
    '{"pack_id": "pk", "x": Infinity}',
])
def test_graph_properties_with_duplicate_keys_or_nan_are_corrupt(world, tmp_path, capsys, props):
    world.graph("n1", "concept", props)
    world.doc("n1", {"pack_id": PACK})
    world.vector("n1", {"pack_id": PACK})
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path, "--allow-pack-missing")
    assert code == 0, rep
    assert rep["before"]["doc"]["classes"]["hold_graph_props_bad"] == 1
    assert rep["before"]["vector"]["classes"]["hold_graph_props_bad"] == 1
    assert world.dump() == before


def test_space_not_in_grammar_is_reported_and_kept(world, tmp_path, capsys):
    world.doc("a", {"pack_id": PACK, "space": "outside_grammar"})
    world.vector("a", {"pack_id": PACK, "space": "outside_grammar"})
    world.doc("b", {"pack_id": PACK, "space": "concept"})
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert rep["before"]["doc"]["space_not_in_grammar"] == {"outside_grammar": 1}
    assert rep["before"]["vector"]["space_not_in_grammar"] == {"outside_grammar": 1}
    assert world.space("doc", "a") == "outside_grammar" and world.space("vec", "a") == "outside_grammar"

_STRICT_METADATA = [
    '{"pack_id": "pk", "x": NaN}',
    '{"pack_id": "pk", "x": Infinity}',
    '{"pack_id": "pk", "x": -Infinity}',
    '{"pack_id": "first", "pack_id": "pk"}',
    '{"pack_id": "pk", "space": "claim", "space": "concept"}',
]


@pytest.mark.parametrize("text", _STRICT_METADATA)
def test_json5_and_duplicate_metadata_is_held(world, tmp_path, capsys, text):
    world.graph("n1", "concept", {"pack_id": PACK})
    world.doc("n1", {"pack_id": PACK})
    world.vector("n1", text, partition=PACK)
    d = sqlite3.connect(world.root / "doc_store.db")
    d.execute("UPDATE doc_sources SET metadata=? WHERE source_id='n1'", (text,))
    d.commit()
    d.close()
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert rep["before"]["doc"]["classes"]["hold_bad_meta"] == 1
    assert rep["before"]["vector"]["classes"]["hold_bad_meta"] == 1
    assert world.dump() == before


def _deep_json_that_recurses():
    from opencrab.common.graph_identity import parse_properties_object

    text = "{}"
    while True:
        text = '{"x":' + text + "}"
        try:
            parse_properties_object(text)
        except RecursionError:
            return text


def test_deep_json_is_held_not_raised(world, tmp_path, capsys):
    text = _deep_json_that_recurses()
    assert sqlite3.connect(":memory:").execute("SELECT json_valid(?, 3)", (text,)).fetchone()[0] == 1
    world.doc("d", text)
    world.vector("v", text, partition=PACK)
    world.graph("g", "concept", text)
    world.doc("g", {"pack_id": PACK})
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert rep["before"]["doc"]["classes"]["hold_bad_meta"] == 1
    assert rep["before"]["vector"]["classes"]["hold_bad_meta"] == 1
    assert rep["before"]["doc"]["classes"]["hold_graph_props_bad"] == 1
    assert world.dump() == before


def test_graph_property_space_wins_over_column(world, tmp_path, capsys):
    world.graph("n", "resource", {"pack_id": PACK, "space": "concept"})
    world.doc("n", {"pack_id": PACK})
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert world.space("doc", "n") == "concept"


def test_legacy_same_space_different_pack_is_ambiguous(env, tmp_path, capsys):
    w = World(env, legacy_graph=True)
    w.graph("n", "concept", {"pack_id": PACK}, node_type="A")
    w.graph("n", "concept", {"pack_id": "other"}, node_type="B")
    w.doc("n", {"pack_id": PACK})
    code, rep = _apply(capsys, w, tmp_path)
    assert code == 0, rep
    assert rep["before"]["doc"]["classes"]["hold_graph_ambiguous"] == 1


def test_reconcile_detects_committed_space_change(world, tmp_path, capsys):
    _many(world, 2)
    orig = bf._write_one_batch

    def wrapped(conn, store, batch, paths, res, allow, stats):
        done = orig(conn, store, batch, paths, res, allow, stats)
        if store == "vector":
            d = sqlite3.connect(world.root / "doc_store.db")
            d.execute("UPDATE doc_sources SET metadata=? WHERE source_id='n0'", ('{"pack_id":"pk","space":"claim"}',))
            d.commit()
            d.close()
        return done

    bf._write_one_batch = wrapped
    try:
        code, rep = _apply(capsys, world, tmp_path, "--batch-size", "1")
    finally:
        bf._write_one_batch = orig
    assert code == 1
    assert any("does not hold concept" in x for x in rep["reconcile"]["problems"])


def test_reconcile_detects_extra_valid_row(world, tmp_path, capsys):
    _many(world, 1)
    orig = bf._write_one_batch

    def wrapped(conn, store, batch, paths, res, allow, stats):
        done = orig(conn, store, batch, paths, res, allow, stats)
        if store == "vector":
            world.doc("extra", {"pack_id": PACK, "space": "concept"})
        return done

    bf._write_one_batch = wrapped
    try:
        code, rep = _apply(capsys, world, tmp_path)
    finally:
        bf._write_one_batch = orig
    assert code == 1
    assert any("total rows changed" in x for x in rep["reconcile"]["problems"])


def test_unserializable_row_does_not_block_its_batch(world, tmp_path, capsys, monkeypatch):
    _many(world, 2)
    real = bf._new_meta_text

    def fail_one(meta, space):
        if meta.get("marker") == "bad":
            raise ValueError("injected serialization failure")
        return real(meta, space)

    d = sqlite3.connect(world.root / "doc_store.db")
    d.execute("UPDATE doc_sources SET metadata=? WHERE source_id='n0'", ('{"pack_id":"pk","marker":"bad"}',))
    d.commit()
    d.close()
    monkeypatch.setattr(bf, "_new_meta_text", fail_one)
    code, rep = _apply(capsys, world, tmp_path, "--batch-size", "2", "--list-ids")
    assert code == 1
    assert rep["write"]["committed"]["doc"] == 1
    assert rep["write"]["skipped_unserializable"] == {"doc": 1, "vector": 0}
    assert rep["skipped_unserializable_ids"]["doc"] == ["n0"]
    assert rep["reconcile"]["ok"]
    assert world.space("doc", "n0") is None and world.space("doc", "n1") == "concept"


def test_lone_surrogate_metadata_is_held(world, tmp_path, capsys):
    world.graph("n", "concept", {"pack_id": PACK})
    world.doc("n", '{"pack_id":"pk","title":"x\\ud800"}')
    before = world.dump()
    code, rep = _apply(capsys, world, tmp_path)
    assert code == 0, rep
    assert rep["before"]["doc"]["classes"]["hold_bad_meta"] == 1
    assert world.dump() == before


def test_apply_holds_lock_during_doc_and_vector_writes(world, env, tmp_path, capsys):
    _many(world, 1)
    attempts = []

    def probe_lock(store, _idx):
        code = textwrap.dedent(f"""
            from opencrab.locking import write_lock
            try:
                with write_lock({str(env)!r}, timeout=0.1):
                    print('acquired')
            except TimeoutError:
                print('busy')
        """)
        got = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
        attempts.append((store, got))

    code, rep = _apply(capsys, world, tmp_path, before_commit=probe_lock)
    assert code == 0, rep
    assert attempts == [("doc", "busy"), ("vector", "busy")]


def test_dry_run_compares_full_doc_content_and_file_bytes(world, capsys, monkeypatch):
    world.graph("n", "concept", {"pack_id": PACK})
    world.doc("n", {"pack_id": PACK}, text="original")
    d = sqlite3.connect(world.root / "doc_store.db")
    d.execute("UPDATE doc_sources SET ingested_at='earlier' WHERE source_id='n'")
    d.commit()
    d.close()
    original = bf.scan

    def corrupt(paths, allow, mode="ro"):
        result = original(paths, allow, mode)
        if mode == "ro":
            d = sqlite3.connect(world.root / "doc_store.db")
            d.execute("UPDATE doc_sources SET text='changed' WHERE source_id='n'")
            d.commit()
            d.close()
        return result

    monkeypatch.setattr(bf, "scan", corrupt)
    code, _ = _run(capsys)
    assert code == 1
    assert sqlite3.connect(world.root / "doc_store.db").execute(
        "SELECT text FROM doc_sources WHERE source_id='n'").fetchone()[0] == "changed"
