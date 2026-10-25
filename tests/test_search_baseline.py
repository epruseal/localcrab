"""#542 search baseline: generator, judge, row contract and S0 end to end.

Design: docs/issue-542-search-baseline.md, section 11.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import search_baseline as harness
from scripts import search_corpus as corpus_gen

REPO_ROOT = Path(__file__).resolve().parents[1]


def _docs(corpus: Path) -> list[dict]:
    return [json.loads(line) for line in (corpus / "docs.jsonl").read_text(encoding="utf-8").splitlines()]


def test_generator_is_deterministic_and_keeps_its_invariants(tmp_path):
    first = corpus_gen.generate("S0", 542, tmp_path / "a")
    second = corpus_gen.generate("S0", 542, tmp_path / "b")
    other = corpus_gen.generate("S0", 543, tmp_path / "c")
    assert first["files"] == second["files"]
    assert first["files"]["docs.jsonl"] != other["files"]["docs.jsonl"]

    docs = _docs(tmp_path / "a")
    queries = json.loads((tmp_path / "a" / "queries.json").read_text(encoding="utf-8"))
    assert queries[0]["shape"] == "explicit_single"
    assert {q["kind"] for q in queries} == set(corpus_gen.KINDS)
    texts = {d["id"]: d["text"] for d in docs}
    for q in queries:
        assert not set(q["gold"]) & set(q["forbidden"]), q["qid"]
        assert set(q["must_hit"]["combined"]) == set(q["gold"]), q["qid"]
        if q["kind"] in ("auto-title", "korean-variant", "top-k-pressure"):
            continue
        holders = {doc_id for doc_id, text in texts.items() if q["text"] in text.split()}
        assert holders == set(q["gold"]) | set(q["forbidden"]), q["qid"]
    n = len(docs)
    ranks = sorted(d["rank"] for d in docs if d["old"])
    assert ranks == list(range(len(ranks))) and len(ranks) == int(n * corpus_gen.OLD_FRACTION)
    assert all(d["rank"] >= n - n * corpus_gen.RECENT_FRACTION for d in docs if d["recent"])
    assert all(d["form"] == "N" for d in docs if d["pack"] is None)


def test_judge_detects_every_planted_defect():
    results = harness.selftest_cases()
    assert results, "selftest produced no cases"
    assert [(name, actual) for name, expected, actual in results if expected != actual] == []


def _read_rows(directory: Path, node_id: str) -> dict:
    import sqlite_vec

    out = {}
    with sqlite3.connect(directory / "doc.db") as conn:
        row = conn.execute(
            "SELECT space, node_type, properties FROM doc_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        out["node"] = (row[0], row[1], json.loads(row[2]))
        row = conn.execute(
            "SELECT text, metadata FROM doc_sources WHERE source_id=?", (node_id,)
        ).fetchone()
        out["source"] = None if row is None else (row[0], json.loads(row[1]))
    conn = sqlite3.connect(directory / "vectors.db")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        row = conn.execute(
            f"SELECT document, metadata FROM {harness.VECTOR_TABLE} WHERE node_id=?", (node_id,)
        ).fetchone()
        out["vector"] = None if row is None else (row[0], json.loads(row[1]))
    finally:
        conn.close()
    return out


def test_row_contract_matches_the_production_writers(tmp_path):
    from opencrab.auth import principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.source_writer import write_source
    from opencrab.stores.local_graph_store import LocalGraphStore

    packs = {"l1": {"pack_id": "qpk-l1", "owner": "user_a", "status": "ready",
                    "visibility": "private", "title": "t one", "description": "d one"}}
    node_doc = {"id": "l1-000001", "pack": "l1", "form": "N", "space": "concept",
                "node_type": "Concept", "title": "node title", "text": "node body qxnodeword"}
    source_doc = {"id": "l1-000002", "pack": "l1", "form": "S", "space": "evidence",
                  "node_type": "TextUnit", "title": "source title", "text": "source body qxsrcword"}

    contract_dir = tmp_path / "contract"
    contract_dir.mkdir()
    sql, docs, vec = harness._open_stores(contract_dir)
    harness._create_users(sql)
    harness._register_packs(sql, packs)
    for doc in (node_doc, source_doc):
        harness.write_doc(docs, vec, harness.doc_rows(doc, packs))
    harness._close(docs, vec, sql)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    sql, docs, vec = harness._open_stores(real_dir)
    harness._create_users(sql)
    harness._register_packs(sql, packs)
    graph = LocalGraphStore(str(real_dir / "graph.db"))
    hybrid = HybridQuery(vec, harness.NoGraph())
    with principal_scope(harness._principal("user_a")):
        receipt = OntologyBuilder(graph, docs, sql, vec=vec).add_node(
            node_doc["space"], node_doc["node_type"], node_doc["id"],
            {"title": node_doc["title"], "text": node_doc["text"]}, pack_id="qpk-l1")
        assert receipt["stores"]["vector"] == "ok", receipt
        receipt = write_source(
            sql, hybrid, docs, vec, graph=graph, text=source_doc["text"],
            source_id=source_doc["id"], metadata={"title": source_doc["title"]}, pack_id="qpk-l1")
        assert receipt["stores"]["documents"].startswith("ok"), receipt
    graph.close()
    harness._close(docs, vec, sql)

    for doc in (node_doc, source_doc):
        assert _read_rows(contract_dir, doc["id"]) == _read_rows(real_dir, doc["id"]), doc["id"]


def _run(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(tmp_path))
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "search_baseline.py"), *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )


def _run_args(corpus: Path, work: Path, run_dir: Path, report: Path) -> list[str]:
    return ["run", "--corpus", str(corpus), "--work", str(work), "--run-dir", str(run_dir),
            "--out", str(report), "--repeats", "1", "--fresh-timeout", "15",
            "--concurrent-seconds", "2"]


def test_s0_end_to_end_and_tampered_state(tmp_path):
    corpus = tmp_path / "corpus"
    corpus_gen.generate("S0", 542, corpus)
    work = tmp_path / "work"
    loaded = _run(["load", "--corpus", str(corpus), "--work", str(work)], tmp_path)
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    assert (work / "load-complete.json").exists()

    report_path = tmp_path / "report.json"
    ran = _run(_run_args(corpus, work, tmp_path / "run", report_path), tmp_path)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verdict"] == "COMPLETE"
    queries = json.loads((corpus / "queries.json").read_text(encoding="utf-8"))
    assert [q["qid"] for q in report["queries"]] == [q["qid"] for q in queries]
    classes = report["summary"]["classes"]
    for leg in classes.values():
        for bad in ("leak", "dup", "scope_error", "warning_dropped", "partial_silent"):
            assert leg.get(bad, 0) == 0, classes
    assert report["environment"]["bm25_defaults_match"] is True

    tampers = {
        "node_type": ("doc.db", "UPDATE doc_nodes SET node_type='Other' WHERE rowid=1"),
        "vector_document": ("vectors.db", None),
        "visibility": ("sql.db", "UPDATE packs SET visibility='private' WHERE pack_id='qpk-bpub'"),
    }
    for name, (db, statement) in tampers.items():
        copy = tmp_path / f"work-{name}"
        shutil.copytree(work, copy)
        if statement is None:
            import sqlite_vec

            conn = sqlite3.connect(copy / db)
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            node_id = conn.execute(f"SELECT node_id FROM {harness.VECTOR_TABLE} LIMIT 1").fetchone()[0]
            conn.execute(f"UPDATE {harness.VECTOR_TABLE} SET document='changed' WHERE node_id=?", (node_id,))
        else:
            conn = sqlite3.connect(copy / db)
            conn.execute(statement)
        conn.commit()
        conn.close()
        out = tmp_path / f"report-{name}.json"
        ran = _run(_run_args(corpus, copy, tmp_path / f"run-{name}", out), tmp_path)
        assert ran.returncode == 2, name + ran.stdout + ran.stderr
        tampered = json.loads(out.read_text(encoding="utf-8"))
        assert tampered["verdict"] == "INCOMPLETE", name
        assert any("initial state mismatch" in r for r in tampered["reasons"]), name


@pytest.mark.parametrize("bad", ["repo", "live"])
def test_write_paths_inside_the_repo_or_live_dir_are_refused(bad, tmp_path):
    from opencrab.config import get_settings

    path = REPO_ROOT / "scratch-run" if bad == "repo" else Path(get_settings().local_data_dir) / "x"
    with pytest.raises(SystemExit):
        harness.check_write_paths([path])
