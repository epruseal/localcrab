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


def test_harness_invalidates_bm25_while_holding_write_lock(tmp_path):
    """The production ingest tools invalidate BM25 inside write.lock (design 8.3)."""
    import opencrab.locking as locking

    packs = {"measure": {"pack_id": "qpk-measure", "owner": "user_a", "status": "ready",
                         "visibility": "private", "title": "m one", "description": "m two"}}
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sql, docs, vec = harness._open_stores(run_dir)
    harness._create_users(sql)
    harness._register_packs(sql, packs)
    lock_path = locking._lock_path("write.lock", str(run_dir))
    seen: list[bool] = []

    class RecordingHybrid:
        def invalidate_bm25_cache(self) -> None:
            seen.append(lock_path in (getattr(locking._held, "locks", None) or {}))

    doc = {"id": "measure-000001", "pack": "measure", "form": "S", "space": "evidence",
           "node_type": "TextUnit", "title": "t", "text": "body qxlockword"}
    hybrid = RecordingHybrid()
    harness.locked_write(run_dir, docs, vec, hybrid, harness.doc_rows(doc, packs))
    harness.locked_delete(run_dir, docs, vec, hybrid, doc["id"])
    harness._close(docs, vec, sql)
    assert seen == [True, True]
    assert lock_path not in (getattr(locking._held, "locks", None) or {})


def _run(args: list[str], tmp_path: Path, script: str = "search_baseline.py") -> subprocess.CompletedProcess:
    # TMPDIR is a sibling of corpus, work and run, so the source-overlap check
    # accepts the normal layout (design 9.1).
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(tmp))
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )


def _run_args(corpus: Path, work: Path, run_dir: Path, report: Path) -> list[str]:
    return ["run", "--corpus", str(corpus), "--work", str(work), "--run-dir", str(run_dir),
            "--out", str(report), "--repeats", "2", "--fresh-timeout", "15",
            "--concurrent-seconds", "2"]


def test_s0_end_to_end_and_tampered_state(tmp_path):
    corpus = tmp_path / "corpus"
    generated = _run(["generate", "--preset", "S0", "--seed", "542", "--out", str(corpus)],
                     tmp_path, script="search_corpus.py")
    assert generated.returncode == 0, generated.stdout + generated.stderr
    assert (corpus / corpus_gen.TIMING_FILE).exists()
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
    assert all(count == 0 for count in report["summary"]["repeat_defects"].values()), report["summary"]
    assert report["environment"]["bm25_defaults_match"] is True

    # A report path that already exists is refused, so a rerun cannot
    # overwrite evidence.
    again = _run(_run_args(corpus, work, tmp_path / "run-again", report_path), tmp_path)
    assert again.returncode != 0 and "report already exists" in again.stderr

    # The report is a valid compare input against itself.
    compared = _run(["compare", str(report_path), str(report_path)], tmp_path)
    assert "COMPARE: INVALID" not in compared.stdout, compared.stdout

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


def _report(**over) -> dict:
    base = {
        "verdict": "COMPLETE",
        "corpus": {"manifest_digest": "d", "preset": "S0", "seed": 1},
        "environment": {"config": "default", "bm25_env": {"OPENCRAB_BM25_NODE_LIMIT": 1}},
        "summary": {"repeat_defects": {"leak": 0, "dup": 0}},
        "queries": [{"qid": "q1", "legs": {leg: {"class": "pass"}
                                            for leg in ("bm25", "fts", "vector", "combined")}}],
        "requirements": {"r": {"met": False}},
    }
    base.update(over)
    return base


def test_compare_refuses_reports_it_cannot_compare():
    base = _report()
    assert harness.compare_preconditions(base, _report()) == []
    assert harness.compare_preconditions(base, _report(queries=[]))
    one_leg_less = _report(queries=[{"qid": "q1", "legs": {"bm25": {"class": "pass"}}}])
    assert harness.compare_preconditions(base, one_leg_less)
    other_corpus = _report(corpus={"manifest_digest": "e", "preset": "S0", "seed": 1})
    assert harness.compare_preconditions(base, other_corpus)
    assert harness.compare_preconditions(base, _report(verdict="INCOMPLETE"))


def test_compare_flags_a_defect_from_any_repeat():
    base = _report()
    head = _report(summary={"repeat_defects": {"leak": 0, "dup": 1}})
    assert harness.compare_preconditions(base, head) == []
    # The final-repeat classes are all pass and the requirement is unmet on
    # both sides, yet an earlier repeat's duplicate is still a regression.
    assert any("dup" in line for line in harness.compare_reports(base, head))
    assert harness.compare_reports(base, _report()) == []


@pytest.mark.parametrize("bad", ["repo", "live", "inside_source", "contains_source"])
def test_write_paths_are_refused(bad, tmp_path, monkeypatch):
    from opencrab.config import get_settings

    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    source = tmp_path / "corpus"
    paths = {
        "repo": REPO_ROOT / "scratch-run",
        "live": Path(get_settings().local_data_dir) / "x",
        "inside_source": source / "out.json",
        "contains_source": tmp_path,
    }
    with pytest.raises(SystemExit):
        harness.check_write_paths([paths[bad]], sources=[source])


def test_tmpdir_must_be_set_and_off_the_system_tmp(tmp_path, monkeypatch):
    target = tmp_path / "run"
    monkeypatch.delenv("TMPDIR", raising=False)
    with pytest.raises(SystemExit):
        harness.check_write_paths([target])
    monkeypatch.setenv("TMPDIR", "/tmp")
    with pytest.raises(SystemExit):
        harness.check_write_paths([target])
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    harness.check_write_paths([target])
