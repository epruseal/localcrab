#!/usr/bin/env python3
"""Fault injection harness for the node BM25 DB index (issue #542).

Design and run guide: docs/issue-542-fault-injection.md.

Subcommands:
    run       --corpus DIR --work DIR --run-root DIR --out REPORT [--scenarios IDS]
    actor     ROLE --dir DIR [options]      (child process role, started by run)
    selftest

The input corpus comes from ``scripts/search_corpus.py generate`` and the load
directory from ``scripts/search_baseline.py load``. Every scenario works on its
own copy of the load directory. The load directory is read only.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import random
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import search_baseline as sb  # noqa: E402

SCRIPT = Path(__file__).resolve()
REPO_ROOT = SCRIPT.parents[1]

# Run and child time limits in seconds (design section 6). Operational values.
RUN_BUDGET = {True: 900.0, False: 7200.0}  # key: smoke scale
CHILD_TIMEOUT = {True: 120.0, False: 900.0}
POLL = 0.02

PROBE_COVERAGE = "auto_pack_probe_coverage"
HARD_CLASSES = ("leak", "dup", "scope_error", "warning_dropped")
# Worst first. "unavailable" is outside the order (D1b only, design 3.6).
VERDICT_RANK = {"not_injected": -1, "inconsistent": 0, "silent_partial": 1,
                "explicit_error": 2, "verified_fallback": 3}
MARK = "qzflt"  # marker token prefix of documents the harness writes

FAULTS: dict[str, list[str]] = {
    "timeout": ["T1", "T2", "T3", "T4"],
    "cancel": ["C1", "C2", "C3"],
    "disk_full": ["D1a", "D1b", "D2", "D3"],
    "index_error": ["E1", "E2a", "E2b", "E3"],
    "resume": ["K1", "K2", "K3"],
    "restart": ["R1", "R2", "R3"],
}
# Scenarios that change the source rows (design section 4 table).
CHANGES_ROWS = {"T4", "D2", "K2", "R1", "R3"}


# ---------------------------------------------------------------------------
# Pure judgement (design 3.2, 3.6). No store, no process.
# ---------------------------------------------------------------------------


def surface_violations(reference: dict[str, dict[str, Any]], observed: dict[str, dict[str, Any]],
                       oracle_ids: dict[str, list[str]], *, rows_unchanged: bool) -> list[str]:
    """Return the query surface violations of one query pass (design 3.2).

    ``reference[qid]`` has ``classes``, ``missing`` (per leg), ``wrong_pack`` and
    ``combined_ids``. ``observed[qid]`` has the same keys plus
    ``selection_warnings``, ``bm25_ids``, ``bm25_cov`` and ``combined_cov``.
    ``oracle_ids[qid]`` is the oracle BM25 id list for the observed request.
    """
    out: list[str] = []
    for qid, ref in reference.items():
        obs = observed.get(qid)
        if obs is None:
            out.append(f"{qid}: not judged")
            continue
        for leg, cls in obs["classes"].items():
            rcls = ref["classes"].get(leg)
            if cls in HARD_CLASSES:
                out.append(f"{qid}:{leg}: {cls}")
                continue
            if cls == "partial_silent":
                same = sorted(obs["missing"].get(leg, [])) == sorted(ref["missing"].get(leg, []))
                if rcls != "partial_silent" or not same:
                    out.append(f"{qid}:{leg}: {rcls} -> partial_silent")
                continue
            if cls == rcls or cls == "partial_flagged":
                continue
            if leg == "combined" and cls == "miss" and obs["combined_cov"]:
                continue
            out.append(f"{qid}:{leg}: {rcls} -> {cls}")
        if not obs["bm25_cov"] and obs["bm25_ids"] != oracle_ids.get(qid):
            out.append(f"{qid}:bm25: unflagged result differs from the oracle")
        if rows_unchanged and not obs["combined_cov"] and obs["combined_ids"] != ref["combined_ids"]:
            out.append(f"{qid}:combined: unflagged result differs from the reference")
        if obs["wrong_pack"] and not ref["wrong_pack"] and PROBE_COVERAGE not in obs["selection_warnings"]:
            out.append(f"{qid}: wrong_pack without {PROBE_COVERAGE}")
    return out


def step_verdict(checks: list[dict[str, Any]], mode: str) -> str:
    """Return the value of one step from its failed check categories (design 3.6)."""
    failed = {c["cat"] for c in checks if not c["ok"]}
    if "injection" in failed:
        return "not_injected"
    if failed & {"invariant", "recovery"}:
        return "inconsistent"
    if failed & {"surface", "stage"}:
        return "silent_partial"
    return mode


def worst(values: list[str]) -> str:
    vals = [v for v in values if v != "unavailable"]
    if not vals:
        return "unavailable"
    return min(vals, key=lambda v: VERDICT_RANK[v])


def selftest_cases() -> list[tuple[str, Any, Any]]:
    """Planted inputs for the judgement functions: (name, expected, actual)."""

    def ref(**over: Any) -> dict[str, Any]:
        base = {"classes": {"bm25": "pass", "fts": "pass", "vector": "pass", "combined": "pass"},
                "missing": {"bm25": [], "combined": []}, "wrong_pack": False,
                "combined_ids": ["a", "b"]}
        base.update(over)
        return base

    def obs(**over: Any) -> dict[str, Any]:
        base = {**ref(), "selection_warnings": [], "bm25_ids": ["a", "b"], "bm25_cov": False,
                "combined_cov": False}
        base.update(over)
        return base

    def run(r: dict[str, Any], o: dict[str, Any], oracle: list[str] | None = None,
            unchanged: bool = True) -> bool:
        return bool(surface_violations({"q": r}, {"q": o}, {"q": oracle or ["a", "b"]},
                                       rows_unchanged=unchanged))

    silent = {"bm25": "partial_silent", "fts": "pass", "vector": "pass", "combined": "partial_silent"}
    flagged = {"bm25": "partial_flagged", "fts": "pass", "vector": "pass", "combined": "partial_flagged"}
    cases: list[tuple[str, Any, Any]] = [
        ("clean pass", False, run(ref(), obs())),
        ("leak", True, run(ref(), obs(classes={**ref()["classes"], "combined": "leak"}))),
        ("dup", True, run(ref(), obs(classes={**ref()["classes"], "bm25": "dup"}))),
        ("warning_dropped", True, run(ref(), obs(classes={**ref()["classes"], "combined": "warning_dropped"}))),
        ("new partial_silent", True, run(ref(), obs(classes=silent, missing={"bm25": ["g"], "combined": ["g"]},
                                                    bm25_ids=["a", "b"]))),
        ("known partial_silent, same miss", False,
         run(ref(classes=silent, missing={"bm25": ["g"], "combined": ["g"]}),
             obs(classes=silent, missing={"bm25": ["g"], "combined": ["g"]}))),
        ("known partial_silent, other miss", True,
         run(ref(classes=silent, missing={"bm25": ["g"], "combined": ["g"]}),
             obs(classes=silent, missing={"bm25": ["g", "h"], "combined": ["g"]}))),
        ("known partial_silent, silent empty result", True,
         run(ref(classes=silent, missing={"bm25": ["g"], "combined": ["g"]}),
             obs(classes=silent, missing={"bm25": ["g"], "combined": ["g"]}, bm25_ids=[]))),
        ("flagged partial", False, run(ref(), obs(classes=flagged, bm25_cov=True, combined_cov=True,
                                                  bm25_ids=[], combined_ids=["b"]))),
        ("combined miss with coverage warning", False,
         run(ref(), obs(classes={**ref()["classes"], "combined": "miss"}, combined_cov=True,
                        bm25_cov=True, bm25_ids=[]))),
        ("combined miss without warning", True,
         run(ref(), obs(classes={**ref()["classes"], "combined": "miss"}))),
        ("unflagged bm25 differs from oracle", True, run(ref(), obs(bm25_ids=["b", "a"]))),
        ("unflagged combined differs, rows unchanged", True, run(ref(), obs(combined_ids=["b", "a"]))),
        ("unflagged combined differs, rows changed", False,
         run(ref(), obs(combined_ids=["b", "a"]), unchanged=False)),
        ("wrong_pack without probe warning", True, run(ref(), obs(wrong_pack=True))),
        ("wrong_pack with probe warning", False,
         run(ref(), obs(wrong_pack=True, selection_warnings=[PROBE_COVERAGE]))),
        ("missing query", True, bool(surface_violations({"q": ref()}, {}, {}, rows_unchanged=True))),
    ]

    def chk(cat: str, ok: bool) -> dict[str, Any]:
        return {"cat": cat, "ok": ok}

    cases += [
        ("verdict clean explicit", "explicit_error", step_verdict([chk("stage", True)], "explicit_error")),
        ("verdict surface", "silent_partial", step_verdict([chk("surface", False)], "verified_fallback")),
        ("verdict stage", "silent_partial", step_verdict([chk("stage", False)], "explicit_error")),
        ("verdict invariant", "inconsistent",
         step_verdict([chk("surface", False), chk("invariant", False)], "explicit_error")),
        ("verdict recovery", "inconsistent", step_verdict([chk("recovery", False)], "explicit_error")),
        ("verdict injection", "not_injected",
         step_verdict([chk("injection", False), chk("invariant", False)], "explicit_error")),
        ("worst order", "inconsistent", worst(["verified_fallback", "inconsistent", "silent_partial"])),
        ("worst not injected", "not_injected", worst(["inconsistent", "not_injected"])),
        ("worst unavailable only", "unavailable", worst(["unavailable"])),
        ("worst skips unavailable", "explicit_error", worst(["unavailable", "explicit_error"])),
    ]
    return cases


def cmd_selftest() -> int:
    bad = [(name, exp, act) for name, exp, act in selftest_cases() if exp != act]
    for name, exp, act in bad:
        print(f"FAIL {name}: expected {exp!r}, got {act!r}")
    print("SELFTEST: PASS" if not bad else f"SELFTEST: {len(bad)} FAILED")
    return 0 if not bad else 1


# ---------------------------------------------------------------------------
# Read-only observation of a scenario copy (design 3.4)
# ---------------------------------------------------------------------------


def _ro(directory: Path) -> sqlite3.Connection:
    conn = sb._ro(directory / "doc.db")
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_node() -> Callable[[Any], dict[str, Any]]:
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    holder = SimpleNamespace(_row_get=lambda row, name: row[name])
    return lambda row: LocalSQLDocStore._row_to_node(holder, row)


def ro_status(directory: Path) -> dict[str, Any]:
    from opencrab.stores import _node_bm25_sqlite as nbm

    conn = _ro(directory)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (nbm.META,)).fetchone() is None:
            return {"state": None, "objects": False}
        status = nbm.status(conn)
        status["objects"] = True
        status["triggers"] = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
            (nbm.PREFIX + "%",)).fetchone()[0]
        status["rows"] = conn.execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0]
        return status
    finally:
        conn.close()


def ro_pending(directory: Path) -> set[tuple[str, str]]:
    from opencrab.stores import _node_bm25_sqlite as nbm

    conn = _ro(directory)
    try:
        return {(r[0], r[1]) for r in conn.execute(f"SELECT space, node_id FROM {nbm.PENDING}")}
    finally:
        conn.close()


def ro_verify(directory: Path) -> dict[str, Any]:
    from opencrab.stores import _node_bm25_sqlite as nbm

    conn = _ro(directory)
    try:
        conn.execute("BEGIN")
        return nbm.verify(conn, _row_to_node())
    finally:
        conn.close()


def ro_node(directory: Path, space: str, node_id: str) -> dict[str, Any] | None:
    conn = _ro(directory)
    try:
        row = conn.execute("SELECT properties FROM doc_nodes WHERE space=? AND node_id=?",
                           (space, node_id)).fetchone()
        return None if row is None else json.loads(row[0])
    finally:
        conn.close()


def backfill_invariant(directory: Path, *, writes: bool = False) -> list[str]:
    """Check a backfill-state index against design 3.4. Returns the problems."""
    from opencrab.ontology.pack_provenance import scope_pack_id
    from opencrab.stores import _node_bm25_sqlite as nbm

    to_node = _row_to_node()
    problems: list[str] = []
    conn = _ro(directory)
    try:
        conn.execute("BEGIN")
        meta = nbm._meta(conn)
        if meta.get("state") != nbm.STATE_BACKFILL:
            return [f"state is {meta.get('state')!r}, not backfill"]
        max_chars = nbm._decode_chars(meta.get("max_text_chars"))
        cursor = (meta.get("cursor_space"), meta.get("cursor_node_id"))
        pending = {(r[0], r[1]) for r in conn.execute(f"SELECT space, node_id FROM {nbm.PENDING}")}
        if pending and not writes:
            problems.append(f"pending rows without writes: {len(pending)}")
        indexed = {(r[1], r[2]): (r[0], r[3], int(r[4]), r[5]) for r in conn.execute(
            f"SELECT doc_key, space, node_id, pack_id, dl, updated_at FROM {nbm.DOCS}")}
        prints: dict[int, int] = {}
        for term, doc in conn.execute(f"SELECT term, doc FROM {nbm.VINST}"):
            prints[int(doc)] = (prints.get(int(doc), 0) + nbm._term_hash(term)) & nbm._MASK
        rows = {(r["space"], r["node_id"]): r for r in conn.execute(
            "SELECT space, node_id, node_type, properties, updated_at FROM doc_nodes")}
        below = set() if cursor[0] is None else {k for k in rows if k <= cursor}
        if writes:
            lost = (below - pending) - set(indexed)
            if lost:
                problems.append(f"keys at or below the cursor missing from the index: {len(lost)}")
        elif set(indexed) != below:
            problems.append(f"index keys differ from keys at or below the cursor: "
                            f"extra={len(set(indexed) - below)} missing={len(below - set(indexed))}")
        bad = 0
        for key, (doc_key, pid, dl, upd) in indexed.items():
            if key in pending:
                continue
            row = rows.get(key)
            if row is None:
                bad += 1
                continue
            node = to_node(row)
            terms = nbm.node_terms(node, max_chars)
            fp = 0
            for term in terms:
                fp = (fp + nbm._term_hash(term)) & nbm._MASK
            updated = "" if row["updated_at"] is None else str(row["updated_at"])
            if (pid != (scope_pack_id(node) or "") or dl != len(terms) or upd != updated
                    or fp != prints.get(doc_key, 0)):
                bad += 1
        if bad:
            problems.append(f"indexed documents that differ from their rows: {bad}")
        stats = conn.execute(f"SELECT doc_count, total_len FROM {nbm.STATS} WHERE id=1").fetchone()
        if stats is None or int(stats[0]) != len(indexed) or int(stats[1]) != sum(
                v[2] for v in indexed.values()):
            problems.append("stats differ from the indexed documents")
        return problems
    finally:
        conn.close()


def _varint(blob: bytes) -> int:
    value = 0
    for byte in blob[:9]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    return value


def keys_invariant(directory: Path, keys: list[tuple[str, str]]) -> list[str]:
    """Per-key check of a ready index for C1 (design 3.4).

    The drain changes only pending keys, and C1 makes only the gold keys
    pending, so the check covers stats, the pending set and each gold key.
    """
    from collections import Counter

    from opencrab.ontology.pack_provenance import scope_pack_id
    from opencrab.stores import _node_bm25_sqlite as nbm

    to_node = _row_to_node()
    conn = _ro(directory)
    try:
        conn.execute("BEGIN")
        problems = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (nbm.META,)).fetchone() is None:
            return ["index objects missing"]
        max_chars = nbm._decode_chars(nbm._meta(conn).get("max_text_chars"))
        stats = conn.execute(f"SELECT doc_count, total_len FROM {nbm.STATS} WHERE id=1").fetchone()
        agg = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(dl), 0) FROM {nbm.DOCS}").fetchone()
        if tuple(stats) != tuple(agg):
            problems.append(f"stats {tuple(stats)} differ from docs {tuple(agg)}")
        pending = {(r[0], r[1]) for r in conn.execute(f"SELECT space, node_id FROM {nbm.PENDING}")}
        if pending - set(keys):
            problems.append(f"unexpected pending keys: {len(pending - set(keys))}")
        for key in keys:
            if key in pending:
                continue
            row = conn.execute("SELECT space, node_id, node_type, properties, updated_at FROM doc_nodes "
                               "WHERE space=? AND node_id=?", key).fetchone()
            doc = conn.execute(f"SELECT doc_key, pack_id, dl, updated_at FROM {nbm.DOCS} "
                               "WHERE space=? AND node_id=?", key).fetchone()
            if (row is None) != (doc is None):
                problems.append(f"{key}: row and index entry disagree")
                continue
            if row is None:
                continue
            node = to_node(row)
            terms = nbm.node_terms(node, max_chars)
            updated = "" if row["updated_at"] is None else str(row["updated_at"])
            if (doc["pack_id"], int(doc["dl"]), doc["updated_at"]) != (scope_pack_id(node) or "", len(terms),
                                                                       updated):
                problems.append(f"{key}: index metadata differs")
                continue
            size = conn.execute(f"SELECT sz FROM {nbm.FTS}_docsize WHERE id=?", (doc["doc_key"],)).fetchone()
            if size is None or _varint(bytes(size[0])) != len(terms):
                problems.append(f"{key}: FTS5 docsize differs from dl")
                continue
            for term, tf in Counter(terms).items():
                got = conn.execute(f"SELECT COUNT(*) FROM {nbm.VINST} WHERE term=? AND doc=?",
                                   (term, doc["doc_key"])).fetchone()[0]
                if got != tf:
                    problems.append(f"{key}: term count differs")
                    break
        return problems
    finally:
        conn.close()


def rows_digest(directory: Path) -> str:
    h = hashlib.sha256()
    conn = _ro(directory)
    try:
        for row in conn.execute("SELECT space, node_id, node_type, properties, updated_at "
                                "FROM doc_nodes ORDER BY space, node_id"):
            h.update(json.dumps(list(row), ensure_ascii=False).encode("utf-8"))
            h.update(b"\n")
    finally:
        conn.close()
    return h.hexdigest()


def _plain(directory: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(directory / "doc.db", timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def checkpoint_truncate(directory: Path) -> None:
    conn = _plain(directory)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row[0] != 0:
            raise RuntimeError(f"checkpoint busy: {tuple(row)}")
    finally:
        conn.close()


def make_pending(directory: Path, keys: list[tuple[str, str]]) -> int:
    """Put keys into pending with a same-value UPDATE (design 4.0)."""
    conn = _plain(directory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("UPDATE doc_nodes SET properties=properties WHERE space=? AND node_id=?", keys)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return len(ro_pending(directory) & set(keys))


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------


def _tag(name: str, data: Any) -> None:
    sys.stdout.write(f"@@ {name} {json.dumps(data, ensure_ascii=False, default=str)}\n")
    sys.stdout.flush()


class Child:
    """One child process with line readers on stdout and stderr."""

    def __init__(self, popen: subprocess.Popen, log: Path, role: str) -> None:
        self.p = popen
        self.log = log
        self.role = role
        self.out: list[str] = []
        self.err: list[str] = []
        self.cv = threading.Condition()
        self.started = time.monotonic()
        self.ended: float | None = None
        self.rc: int | None = None
        self._readers = [threading.Thread(target=self._read, args=(popen.stdout, self.out), daemon=True),
                         threading.Thread(target=self._read, args=(popen.stderr, self.err), daemon=True)]
        for t in self._readers:
            t.start()

    def _read(self, stream: Any, sink: list[str]) -> None:
        for line in stream:
            with self.cv:
                sink.append(line.rstrip("\n"))
                self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def tags(self, name: str) -> list[Any]:
        prefix = f"@@ {name} "
        with self.cv:
            return [json.loads(line[len(prefix):]) for line in self.out if line.startswith(prefix)]

    def wait_tag(self, name: str, timeout: float, count: int = 1) -> bool:
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                if sum(1 for line in self.out if line.startswith(f"@@ {name} ")) >= count:
                    return True
                if self.p.poll() is not None and not any(t.is_alive() for t in self._readers):
                    return False
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cv.wait(min(left, 0.2))

    def send(self, text: str) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(text)
        self.p.stdin.flush()

    def signal(self, sig: int) -> bool:
        """Send ``sig`` when the child is alive. Returns whether it was alive."""
        alive = self.p.poll() is None
        if alive:
            self.p.send_signal(sig)
        return alive

    def finish(self, timeout: float) -> int | None:
        try:
            self.rc = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
            self.rc = None
        self.ended = time.monotonic()
        for t in self._readers:
            t.join(timeout=10)
        self.log.write_text(
            json.dumps({"role": self.role, "pid": self.p.pid, "args": self.p.args, "rc": self.rc}, default=str)
            + "\n--- stdout\n" + "\n".join(self.out) + "\n--- stderr\n" + "\n".join(self.err) + "\n",
            encoding="utf-8")
        return self.rc

    def json_out(self) -> dict[str, Any] | None:
        """Parse the one JSON object a CLI command printed (non-tag stdout lines)."""
        text = "\n".join(line for line in self.out if not line.startswith("@@ "))
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None

    def stderr_text(self) -> str:
        return "\n".join(self.err)


# ---------------------------------------------------------------------------
# Run context and scenario helpers
# ---------------------------------------------------------------------------


class NotInjectedError(Exception):
    """The harness could not confirm that the fault happened."""


class Ctx:
    def __init__(self, corpus: Path, work: Path, root: Path, smoke: bool, watchdog: Any) -> None:
        self.corpus, self.work, self.root, self.smoke = corpus, work, root, smoke
        self.watchdog = watchdog
        self.manifest, self.packs, self.queries = sb._read_corpus(corpus)
        self.expected = sb.expected_state(corpus)
        self.truth: dict[str, tuple[str, str]] = {}
        for doc in sb._iter_docs(corpus):
            pack = self.packs.get(doc["pack"]) if doc["pack"] else None
            space = doc["space"] if doc["form"] == "N" else "evidence"
            self.truth[doc["id"]] = (pack["pack_id"] if pack else "", space)
        self.node_count = int(self.manifest["node_count"])
        self.batch = max(10, self.node_count // 100)
        self.child_timeout = CHILD_TIMEOUT[smoke]
        self.empty_env = root / "empty.env"
        self.reference: dict[str, dict[str, Any]] = {}
        self.ref_effective: dict[str, list[str]] = {}
        self.index_bytes = 0
        self._oracle: tuple[str, Any] | None = None
        self.reference_oracle: Any = None
        self.reference_digest: str | None = None
        self.measure_pack = self.packs["measure"]["pack_id"]

    def gold_keys(self) -> list[tuple[str, str]]:
        ids = sorted({g for q in self.queries for g in q["gold"]})
        return [(self.truth[g][1], g) for g in ids]

    def register(self, ids: list[str]) -> None:
        for node_id in ids:
            self.truth[node_id] = (self.measure_pack, "evidence")

    def new_doc(self, node_id: str, body: str) -> tuple[str, str, str, dict[str, Any]]:
        doc = {"id": node_id, "pack": "measure", "form": "N", "space": "evidence",
               "node_type": "Note", "title": "fault", "text": body}
        return sb.doc_rows(doc, self.packs)["node"]

    def oracle_index(self, directory: Path, docs: Any) -> Any:
        """Return a BM25Index over every current row; reuse it while the rows do not change."""
        from opencrab.ontology import bm25 as bm25_mod

        digest = rows_digest(directory)
        if self.reference_oracle is not None and digest == self.reference_digest:
            return self.reference_oracle
        if self._oracle is not None and self._oracle[0] == digest:
            return self._oracle[1]
        self._oracle = None
        gc.collect()
        self.watchdog.op_start("oracle_build")
        nodes = docs.list_nodes(limit=10**9)
        index = bm25_mod.BM25Index.build(nodes, max_text_chars=bm25_mod.max_text_chars_setting())
        self.watchdog.op_end()
        del nodes
        self._oracle = (digest, index)
        return index


def _profile(q: dict[str, Any]) -> Any:
    import opencrab.ontology.query as qmod

    return qmod._profile_for_query(q["text"], q["limit"], 1)


def _shape(hits: list[dict[str, Any]]) -> list[tuple]:
    return [(h["node_id"], h["space"], h["node_type"], h["score"], h["pack_id"], h["text"],
             json.dumps(h["properties"], sort_keys=True, ensure_ascii=False)) for h in hits]


class Svc:
    """The harness's own service-role stores and HybridQuery for one copy."""

    def __init__(self, directory: Path) -> None:
        from opencrab.ontology.query import HybridQuery
        from opencrab.pack.read_scope import read_scope

        self.dir = directory
        self.sql, self.docs, self.vec = sb._open_stores(directory)
        self.principal = sb._principal("user_a")
        self.scope = read_scope(self.sql, self.principal)
        self.readable = set(self.scope)
        self.hq = HybridQuery(self.vec, sb.NoGraph())
        self.hq._doc_store = self.docs
        self.memory_entries = 0
        original = self.hq._memory_path_entered

        def entered() -> None:
            self.memory_entries += 1
            original()

        self.hq._memory_path_entered = entered

    def path(self) -> str:
        return "db" if self.hq._node_index_store() is not None else "memory"

    def close(self) -> None:
        try:
            self.hq.shutdown_bm25()
        except Exception:  # noqa: BLE001 -- best effort on teardown
            pass
        sb._close(self.docs, self.vec, self.sql)


def quality_pass(ctx: Ctx, svc: Svc) -> tuple[dict[str, dict[str, Any]], list[float]]:
    """Run every query once and judge it (same call contract as search_baseline.cmd_run)."""
    from opencrab.services.pack_selection import resolve_packs

    observed: dict[str, dict[str, Any]] = {}
    bm25_ms: list[float] = []
    for q in ctx.queries:
        ctx.watchdog.op_start(f"pass:{q['qid']}")
        sel = resolve_packs(q["text"], q["pack_ids"], q["auto_pack"], False, str(svc.dir),
                            scope=svc.scope, raise_on_error=False, sql=svc.sql,
                            principal=svc.principal, hybrid=svc.hq, spaces=q["spaces"])
        # Design 3.2: the BM25 leg is the BM25 call inside the combined call.
        calls: list[dict[str, Any]] = []
        original = svc.hq._bm25_search_with_warnings

        def record(question: str, spaces: Any, limit: int, *, pack_ids: list[str],
                   include_unpackaged: bool = False, _orig: Any = original,
                   _calls: list[dict[str, Any]] = calls) -> Any:
            started = time.perf_counter()
            hits, warns = _orig(question, spaces, limit, pack_ids=pack_ids,
                                include_unpackaged=include_unpackaged)
            _calls.append({"ids": [h.get("node_id") for h in hits], "warnings": list(warns),
                           "spaces": spaces, "limit": limit, "pack_ids": list(pack_ids),
                           "ms": (time.perf_counter() - started) * 1000.0})
            return hits, warns

        svc.hq._bm25_search_with_warnings = record
        try:
            outcome = svc.hq.query(q["text"], pack_ids=sel.effective_pack_ids, full_scope=svc.scope,
                                   spaces=q["spaces"], limit=q["limit"])
        finally:
            del svc.hq._bm25_search_with_warnings
        profile = _profile(q)
        separate = not calls
        if separate:
            record(q["text"], q["spaces"], profile.bm25_limit, pack_ids=list(sel.effective_pack_ids))
        call = calls[0]
        bm25_ms.append(call["ms"])
        fts = svc.hq._fts_search(q["text"], q["spaces"], profile.bm25_limit, pack_ids=sel.effective_pack_ids)
        vec = svc.hq._vector_search(q["text"], q["spaces"], profile.vector_limit,
                                    pack_ids=sel.effective_pack_ids)
        ctx.watchdog.op_end()
        warns = call["warnings"]
        legs = {"bm25": {"ids": call["ids"], "warnings": list(warns)},
                "fts": {"ids": [h.get("node_id") for h in fts], "warnings": []},
                "vector": {"ids": [r.node_id for r in vec], "warnings": []}}
        selection = {"effective": list(sel.effective_pack_ids), "warnings": [w.code for w in sel.warnings]}
        combined = {"items": sb._items_from_results(outcome.results), "warnings": list(outcome.warnings)}
        judged = sb.judge_bundle(q, ctx.truth, svc.readable, selection, legs, combined)
        classes = {name: part["class"] for name, part in judged["legs"].items()}
        classes["combined"] = judged["combined"]["class"]
        missing = {name: list(part["missing"]) for name, part in judged["legs"].items()}
        missing["combined"] = list(judged["combined"]["missing"])
        observed[q["qid"]] = {
            "classes": classes, "missing": missing, "wrong_pack": judged["wrong_pack"],
            "selection_warnings": selection["warnings"], "effective": selection["effective"],
            "bm25_call": {"spaces": call["spaces"], "limit": call["limit"], "pack_ids": call["pack_ids"],
                          "separate": separate, "calls_in_query": len(calls) - int(separate)},
            "bm25_ids": legs["bm25"]["ids"], "bm25_warnings": list(warns),
            "bm25_cov": bool(sb._coverage(list(warns))),
            "combined_cov": bool(judged["combined"]["coverage_warnings"]),
            "combined_ids": [item["id"] for item in combined["items"]],
        }
    return observed, bm25_ms


def oracle_ids(ctx: Ctx, svc: Svc, observed: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Oracle ids for the recorded BM25 call of each observed query (design 3.2)."""
    index = ctx.oracle_index(svc.dir, svc.docs)
    out = {}
    for q in ctx.queries:
        if q["qid"] not in observed:
            continue
        call = observed[q["qid"]]["bm25_call"]
        hits = index.search(q["text"], spaces=call["spaces"], limit=call["limit"], pack_ids=call["pack_ids"])
        out[q["qid"]] = [h["node_id"] for h in hits]
    return out


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {"n": len(values), "p50": round(ordered[len(ordered) // 2], 3), "p95": round(p95, 3),
            "max": round(ordered[-1], 3)}


class Scenario:
    def __init__(self, ctx: Ctx, sid: str) -> None:
        self.ctx, self.sid = ctx, sid
        self.dir = ctx.root / sid
        self.steps: list[dict[str, Any]] = []
        self.recovery_commands: list[str] = []
        self.children: list[dict[str, Any]] = []
        self.svc: Svc | None = None
        self.n_actor = 0

    # -- records ---------------------------------------------------------

    def step(self, name: str, mode: str) -> dict[str, Any]:
        st = {"name": name, "mode": mode, "checks": [], "notes": {}}
        self.steps.append(st)
        return st

    @staticmethod
    def check(st: dict[str, Any], cat: str, name: str, ok: Any, detail: Any = None) -> bool:
        st["checks"].append({"cat": cat, "name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def inject(self, st: dict[str, Any], name: str, ok: Any, detail: Any = None) -> None:
        self.check(st, "injection", name, ok, detail)
        if not ok:
            raise NotInjectedError(f"{self.sid}/{st['name']}: {name}: {detail}")

    # -- copies ----------------------------------------------------------

    def prepare(self, target: Path | None = None) -> None:
        target = target or self.dir
        sb._copy_db_files(self.ctx.work, target)
        observed, problems = sb.observed_state(target)
        diff = sb._state_diff(self.ctx.expected, observed)
        if diff or problems:
            raise NotInjectedError(f"copy does not match the load state: {diff} {problems}")

    def to_backfill(self) -> None:
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        store = LocalSQLDocStore(str(self.dir / "doc.db"))
        store.node_bm25_drop()
        store.close()
        conn = _plain(self.dir)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        store = LocalSQLDocStore(str(self.dir / "doc.db"))
        store.close()
        status = ro_status(self.dir)
        if status.get("state") != "backfill" or status.get("doc_count") != 0:
            raise NotInjectedError(f"backfill copy is not in backfill state: {status}")

    def open_svc(self) -> Svc:
        self.close_svc()
        self.svc = Svc(self.dir)
        return self.svc

    def close_svc(self) -> None:
        if self.svc is not None:
            self.svc.close()
            self.svc = None
            gc.collect()

    # -- children --------------------------------------------------------

    def spawn(self, role: str, *args: str, env: dict[str, str] | None = None, stdin: bool = False,
              directory: Path | None = None, wrap: list[str] | None = None) -> Child:
        self.n_actor += 1
        cmd = [sys.executable, str(SCRIPT), "actor", role, "--dir", str(directory or self.dir),
               "--corpus", str(self.ctx.corpus), "--timeout", str(int(self.ctx.child_timeout)), *args]
        if wrap:
            cmd = [*wrap, *cmd]
        full_env = dict(os.environ, LOCAL_DATA_DIR=str(self.dir), LOCALCRAB_ENV_FILE=str(self.ctx.empty_env),
                        PYTHONDONTWRITEBYTECODE="1")
        full_env.update(env or {})
        popen = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                                 text=True, env=full_env, cwd=str(REPO_ROOT))
        child = Child(popen, self.dir / f"actor-{self.n_actor:02d}-{role}.log", role)
        self.children.append({"n": self.n_actor, "role": role, "pid": popen.pid, "args": args})
        return child

    def run_child(self, role: str, *args: str, **kw: Any) -> Child:
        child = self.spawn(role, *args, **kw)
        child.finish(self.ctx.child_timeout)
        self.children[-1]["rc"] = child.rc
        return child

    def cli(self, role: str, *args: str, record: bool = True) -> Child:
        child = self.run_child(role, *args)
        if record:
            self.recovery_commands.append(f"opencrab packs bm25-index {role} {' '.join(args)}".strip())
        return child

    def wait_progress(self, child: Child, target_docs: int) -> bool:
        """Wait until the index holds ``target_docs`` documents while ``child`` runs."""
        deadline = time.monotonic() + self.ctx.child_timeout
        while time.monotonic() < deadline:
            if child.p.poll() is not None:
                return False
            try:
                status = ro_status(self.dir)
            except sqlite3.Error:
                status = {}
            if (status.get("doc_count") or 0) >= target_docs and status.get("state") == "backfill":
                return True
            time.sleep(POLL)
        return False

    # -- shared judgements -------------------------------------------------

    def judge_pass(self, st: dict[str, Any], observed: dict[str, dict[str, Any]],
                   only: set[str] | None = None, skip: set[str] | None = None) -> list[str]:
        assert self.svc is not None
        ref = {qid: r for qid, r in self.ctx.reference.items()
               if (only is None or qid in only) and (skip is None or qid not in skip)}
        violations = surface_violations(ref, observed, oracle_ids(self.ctx, self.svc, observed),
                                        rows_unchanged=self.sid not in CHANGES_ROWS)
        self.check(st, "surface", "query_surface", not violations, violations[:40])
        st["notes"]["flagged_queries"] = sorted(q for q, o in observed.items() if o["bm25_cov"])
        return violations

    def recover(self, commands: list[tuple[str, tuple[str, ...]]]) -> None:
        """Run documented recovery commands, restart and check design 3.5."""
        st = self.step("recovery", "verified_fallback")
        self.close_svc()
        for role, args in commands:
            child = self.cli(role, *args)
            self.check(st, "recovery", f"{role} exit 0", child.rc == 0,
                       {"rc": child.rc, "out": child.json_out()})
        svc = self.open_svc()  # restart
        if svc.path() != "db":
            self.check(st, "recovery", "db path after restart", False, ro_status(self.dir))
            return
        index = self.ctx.oracle_index(self.dir, svc.docs)
        mismatch = []
        for q in self.ctx.queries:
            eff = self.ctx.ref_effective[q["qid"]]
            limit = _profile(q).bm25_limit
            got = svc.docs.node_bm25_search(q["text"], pack_ids=eff, spaces=q["spaces"], limit=limit)
            want = index.search(q["text"], spaces=q["spaces"], limit=limit, pack_ids=eff)
            if got is None or got[1] or _shape(got[0]) != _shape(want):
                mismatch.append(q["qid"])
        self.check(st, "recovery", "db results equal the oracle", not mismatch, mismatch)
        status = ro_status(self.dir)
        self.check(st, "recovery", "ready and no pending",
                   status.get("state") == "ready" and status.get("pending") == 0, status)
        verify = self.cli("verify", record=False)
        self.check(st, "recovery", "verify CLI exit 0", verify.rc == 0, verify.json_out())
        self.recovery_commands.append("opencrab packs bm25-index verify")
        observed, _ = quality_pass(self.ctx, svc)
        diff = [qid for qid, r in self.ctx.reference.items()
                if observed[qid]["classes"] != r["classes"] or observed[qid]["wrong_pack"] != r["wrong_pack"]]
        self.check(st, "recovery", "classes equal the reference", not diff, diff)
        oids = oracle_ids(self.ctx, svc, observed)
        off = [qid for qid in observed if observed[qid]["bm25_ids"] != oids[qid]]
        self.check(st, "recovery", "bm25 equals the oracle", not off, off)
        if self.sid not in CHANGES_ROWS:
            moved = [qid for qid, r in self.ctx.reference.items()
                     if observed[qid]["combined_ids"] != r["combined_ids"]]
            self.check(st, "recovery", "combined equals the reference", not moved, moved)

    def result(self) -> dict[str, Any]:
        values = [step_verdict(s["checks"], s["mode"]) for s in self.steps]
        for s, v in zip(self.steps, values):
            s["value"] = v
        return {"id": self.sid, "value": worst(values) if values else "not_injected",
                "steps": self.steps, "recovery_commands": self.recovery_commands,
                "children": self.children}


# ---------------------------------------------------------------------------
# Scenarios (design section 4)
# ---------------------------------------------------------------------------


def _hold_write_lock(directory: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(directory / "doc.db", timeout=30.0, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _release(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def _first_query(ctx: Ctx, kind_prefix: str) -> dict[str, Any]:
    for q in ctx.queries:
        if q["kind"].startswith(kind_prefix):
            return q
    raise NotInjectedError(f"no query of kind {kind_prefix}")


def _node_gold_query(ctx: Ctx) -> tuple[dict[str, Any], str]:
    """An explicit single-pack query with a node-form gold that BM25 must hit."""
    for q in ctx.queries:
        if q["shape"] == "explicit_single" and q["must_hit"]["bm25"]:
            for g in q["must_hit"]["bm25"]:
                return q, g
    raise NotInjectedError("no explicit single-pack query with a BM25 must-hit gold")


def _write_search_file(sc: Scenario, name: str, queries: list[dict[str, Any]]) -> Path:
    path = sc.dir / name
    rows = [{"qid": q["qid"], "text": q["text"], "spaces": q["spaces"],
             "limit": _profile(q).bm25_limit, "effective": sc.ctx.ref_effective[q["qid"]]} for q in queries]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def _bm25_results_violations(sc: Scenario, results: list[dict[str, Any]]) -> list[str]:
    """Design 3.2 oracle rule for BM25-only results that a child printed."""
    assert sc.svc is not None
    index = sc.ctx.oracle_index(sc.dir, sc.svc.docs)
    by_qid = {q["qid"]: q for q in sc.ctx.queries}
    out = []
    for res in results:
        q = by_qid[res["qid"]]
        if sb._coverage(res["warnings"]):
            continue
        want = [h["node_id"] for h in index.search(q["text"], spaces=q["spaces"], limit=res["limit"],
                                                   pack_ids=res["effective"])]
        if res["ids"] != want:
            out.append(f"{res['qid']}: unflagged child result differs from the oracle")
    return out


def sc_t1(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    svc = sc.open_svc()
    keys = ctx.gold_keys()
    st = sc.step("sqlite write lock held", "explicit_error")
    before = make_pending(sc.dir, keys)
    holder = _hold_write_lock(sc.dir)
    try:
        observed, lat = quality_pass(ctx, svc)
        after = len(ro_pending(sc.dir) & set(keys))
    finally:
        _release(holder)
    sc.inject(st, "pending kept under the lock", before > 0 and after == before, [before, after])
    sc.judge_pass(st, observed)
    st["notes"]["bm25_latency_ms"] = _summary(lat)
    st["notes"]["path"] = svc.path()

    st = sc.step("in-process lock held", "explicit_error")
    before = make_pending(sc.dir, keys)
    got, release = threading.Event(), threading.Event()

    def hold() -> None:
        with svc.docs._lock:
            got.set()
            release.wait(ctx.child_timeout)

    holder_thread = threading.Thread(target=hold, daemon=True)
    holder_thread.start()
    got.wait(10)
    try:
        observed, lat = quality_pass(ctx, svc)
        after = len(ro_pending(sc.dir) & set(keys))
    finally:
        release.set()
        holder_thread.join(10)
    sc.inject(st, "pending kept under the in-process lock", before > 0 and after == before, [before, after])
    sc.judge_pass(st, observed)
    st["notes"]["bm25_latency_ms"] = _summary(lat)

    st = sc.step("after release", "explicit_error")
    observed, _ = quality_pass(ctx, svc)
    sc.check(st, "surface", "pending drained by the next searches",
             not (ro_pending(sc.dir) & set(keys)), len(ro_pending(sc.dir)))
    sc.judge_pass(st, observed)
    sc.recover([])


def sc_t2(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    node_id = "fault-t2-000001"
    ctx.register([node_id])
    plan = sc.dir / "plan.json"
    plan.write_text(json.dumps([["new", node_id, f"{MARK}tlock body"]]), encoding="utf-8")
    st = sc.step("store write under the lock", "explicit_error")
    holder = _hold_write_lock(sc.dir)
    try:
        child = sc.run_child("writer", "--plan", str(plan), "--max-fail", "1")
    finally:
        _release(holder)
    errs = child.tags("err")
    sc.inject(st, "write raised a lock error", errs and "locked" in errs[0]["error"], errs)
    pending = ro_pending(sc.dir)
    sc.check(st, "stage", "failed write left no row and no pending",
             ro_node(sc.dir, "evidence", node_id) is None and ("evidence", node_id) not in pending,
             {"pending": len(pending)})
    sc.recover([])


def sc_t3(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    sc.to_backfill()
    svc = sc.open_svc()
    st = sc.step("backfill step under the lock", "explicit_error")
    child = sc.spawn("backfill", "--batch", str(ctx.batch), "--wait-go", stdin=True)
    opened = child.wait_tag("opened", ctx.child_timeout)
    if not opened:
        child.finish(ctx.child_timeout)
        sc.inject(st, "child opened the store", False, child.stderr_text()[-2000:])
    before = ro_status(sc.dir)
    holder = _hold_write_lock(sc.dir)
    try:
        child.send("go\n")
        observed, _ = quality_pass(ctx, svc)
        child.finish(ctx.child_timeout)
    finally:
        _release(holder)
    out = child.json_out() or {}
    sc.inject(st, "backfill step hit the lock",
              child.tags("opened") and child.tags("opened")[0].get("supports") is True
              and "locked" in str(out.get("error", "")), out)
    after = ro_status(sc.dir)
    sc.check(st, "stage", "backfill exit is non-zero", child.rc not in (0, None), child.rc)
    sc.check(st, "invariant", "cursor and state unchanged",
             (before["state"], before["cursor"], before["doc_count"])
             == (after["state"], after["cursor"], after["doc_count"]), [before, after])
    sc.check(st, "invariant", "backfill invariant", not backfill_invariant(sc.dir),
             backfill_invariant(sc.dir))
    st2 = sc.step("search during the failed backfill", "verified_fallback")
    sc.check(st2, "surface", "memory path used", svc.memory_entries > 0, svc.memory_entries)
    sc.judge_pass(st2, observed)
    sc.recover([("backfill", ("--batch", str(ctx.batch)))])


def sc_t4(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    node_id = "fault-t4-000001"
    ctx.register([node_id])
    search_file = _write_search_file(sc, "search.json", ctx.queries)
    st = sc.step("store opened under the lock", "verified_fallback")
    holder = _hold_write_lock(sc.dir)
    released = False
    try:
        child = sc.spawn("open", "--wait-go", "--search-file", str(search_file),
                         "--write-id", node_id, stdin=True)
        opened = child.wait_tag("opened", ctx.child_timeout)
        _release(holder)
        released = True
        if opened:
            child.send("go\n")
        child.finish(ctx.child_timeout)
    finally:
        if not released:
            _release(holder)
    tags = child.tags("opened")
    sc.inject(st, "instance is available but its index is unsupported",
              tags and tags[0]["available"] and tags[0]["status"].get("state") == "unsupported", tags)
    results = child.tags("result")
    sc.check(st, "surface", "every query answered", len(results) == len(ctx.queries), len(results))
    sc.check(st, "surface", "memory path in that instance",
             all(r["path"] == "memory" for r in results), sorted({r["path"] for r in results}))
    svc = sc.open_svc()
    # The child searched before its write, so the oracle is the reference rows.
    viol = _bm25_results_violations_at_reference(sc, results)
    sc.check(st, "surface", "unflagged results equal the oracle", not viol, viol[:20])
    writes = child.tags("ack") + child.tags("err")
    pending = ro_pending(sc.dir)
    sc.check(st, "stage", "write kept its key in pending",
             child.tags("ack") and ro_node(sc.dir, "evidence", node_id) is not None
             and ("evidence", node_id) in pending, {"writes": writes, "pending": len(pending)})
    st["notes"]["after_release"] = child.tags("status")
    svc.close()
    sc.svc = None
    sc.recover([])


def _bm25_results_violations_at_reference(sc: Scenario, results: list[dict[str, Any]]) -> list[str]:
    """Oracle check against the reference rows (T4: the child searched before it wrote)."""
    ref_index = sc.ctx.reference_oracle
    by_qid = {q["qid"]: q for q in sc.ctx.queries}
    out = []
    for res in results:
        q = by_qid[res["qid"]]
        if sb._coverage(res["warnings"]):
            continue
        want = [h["node_id"] for h in ref_index.search(q["text"], spaces=q["spaces"], limit=res["limit"],
                                                       pack_ids=res["effective"])]
        if res["ids"] != want:
            out.append(f"{res['qid']}: unflagged child result differs from the oracle")
    return out


class _Cancel:
    """Progress handler that interrupts the k-th callback once (design 4.2)."""

    def __init__(self, k: int) -> None:
        self.k, self.calls, self.fired = k, 0, False

    def __call__(self) -> int:
        self.calls += 1
        if not self.fired and self.calls == self.k:
            self.fired = True
            return 1
        return 0


_OPCODES = 100


def _cancel_points(n: int, head: int, spread: int) -> list[int]:
    points = set(range(1, min(head, n) + 1))
    if n > head:
        step = (n - head) / max(1, spread)
        points |= {head + 1 + int(i * step) for i in range(spread)}
    return sorted(p for p in points if 1 <= p <= n)


def sc_c1(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    svc = sc.open_svc()
    q, _gold = _node_gold_query(ctx)
    eff = ctx.ref_effective[q["qid"]]
    limit = _profile(q).bm25_limit
    keys = ctx.gold_keys()
    want = [h["node_id"] for h in ctx.reference_oracle.search(
        q["text"], spaces=q["spaces"], limit=limit, pack_ids=eff)]
    conn = svc.docs._conn

    def call(handler: Any) -> tuple[list[str], list[str], Exception | None]:
        conn.set_progress_handler(handler, _OPCODES)
        try:
            hits, warns = svc.hq._bm25_search_with_warnings(q["text"], q["spaces"], limit, pack_ids=eff)
            return [h["node_id"] for h in hits], list(warns), None
        except Exception as exc:  # noqa: BLE001 -- recorded as a stage violation
            return [], [], exc
        finally:
            conn.set_progress_handler(None, 0)

    make_pending(sc.dir, keys)
    counter = _Cancel(0)
    call(counter)
    n = counter.calls
    st = sc.step("cancel points", "explicit_error")
    st["notes"]["callbacks"] = n
    fired = 0
    outcomes: dict[str, int] = {}
    bad: list[Any] = []
    for k in _cancel_points(n, 20, 40):
        make_pending(sc.dir, keys)
        entries = svc.memory_entries
        cancel = _Cancel(k)
        ids, warns, exc = call(cancel)
        fired += cancel.fired
        memory = svc.memory_entries > entries
        if exc is not None:
            kind = "raised"
            bad.append({"k": k, "raised": repr(exc)})
        elif sb._coverage(warns):
            kind = "flagged"
        elif ids == want:
            kind = "memory_equal" if memory else "equal"
        else:
            kind = "silent_differs"
            bad.append({"k": k, "ids": ids[:5], "memory": memory})
        outcomes[kind] = outcomes.get(kind, 0) + 1
        free = svc.docs._lock.acquire(blocking=False)
        if free:
            svc.docs._lock.release()
        problems = keys_invariant(sc.dir, keys)
        if conn.in_transaction or not free or problems:
            sc.check(st, "invariant", f"state after cancel point {k}", False,
                     {"in_tx": conn.in_transaction, "lock_free": free, "problems": problems})
    st["notes"]["outcomes"] = outcomes
    sc.inject(st, "at least one cancellation fired", fired > 0, fired)
    sc.check(st, "surface", "each point is equal, flagged or an equal fallback", not bad, bad[:20])
    verify = ro_verify(sc.dir)
    sc.check(st, "invariant", "verify excluding pending", verify["ok"], verify)
    st = sc.step("after cancellation", "explicit_error")
    observed, _ = quality_pass(ctx, svc)
    sc.judge_pass(st, observed)
    sc.recover([])


def sc_c2(sc: Scenario) -> None:
    from opencrab.ontology.query import HybridQuery

    ctx = sc.ctx
    sc.prepare()
    sc.to_backfill()
    svc = sc.open_svc()
    q, _gold = _node_gold_query(ctx)
    eff = ctx.ref_effective[q["qid"]]
    limit = _profile(q).bm25_limit
    want = [h["node_id"] for h in ctx.reference_oracle.search(
        q["text"], spaces=q["spaces"], limit=limit, pack_ids=eff)]
    conn = svc.docs._conn

    def call(handler: Any) -> tuple[list[str], list[str], Exception | None, str]:
        hq = HybridQuery(svc.vec, sb.NoGraph())
        hq._doc_store = svc.docs
        path = "db" if hq._node_index_store() is not None else "memory"
        conn.set_progress_handler(handler, _OPCODES)
        try:
            ctx.watchdog.op_start("c2_call")
            hits, warns = hq._bm25_search_with_warnings(q["text"], q["spaces"], limit, pack_ids=eff)
            return [h["node_id"] for h in hits], list(warns), None, path
        except Exception as exc:  # noqa: BLE001 -- recorded
            return [], [], exc, path
        finally:
            ctx.watchdog.op_end()
            conn.set_progress_handler(None, 0)
            hq.shutdown_bm25()
            del hq
            gc.collect()

    counter = _Cancel(0)
    call(counter)
    n = counter.calls
    st = sc.step("cancel points on the memory path", "verified_fallback")
    st["notes"]["callbacks"] = n
    fired = 0
    outcomes: dict[str, int] = {}
    bad: list[Any] = []
    for k in _cancel_points(n, 0, 10):
        cancel = _Cancel(k)
        ids, warns, exc, path = call(cancel)
        fired += cancel.fired
        if exc is not None:
            kind = "raised"
            bad.append({"k": k, "raised": repr(exc)})
        elif sb._coverage(warns):
            kind = "flagged"
        elif ids == want:
            kind = "equal"
        else:
            kind = "silent_differs"
            bad.append({"k": k, "returned": len(ids), "path": path})
        outcomes[kind] = outcomes.get(kind, 0) + 1
        problems = backfill_invariant(sc.dir)
        if conn.in_transaction or problems:
            sc.check(st, "invariant", f"state after cancel point {k}", False,
                     {"in_tx": conn.in_transaction, "problems": problems})
    st["notes"]["outcomes"] = outcomes
    sc.inject(st, "at least one cancellation fired", fired > 0, fired)
    sc.check(st, "surface", "each point is equal or flagged", not bad, bad[:20])
    problems = backfill_invariant(sc.dir)
    sc.check(st, "invariant", "backfill invariant", not problems, problems)
    sc.recover([("backfill", ("--batch", str(ctx.batch)))])


def sc_c3(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    sc.to_backfill()
    total = ro_status(sc.dir)["rows"]
    for sig, frac in ((signal.SIGINT, 0.25), (signal.SIGTERM, 0.5)):
        name = signal.Signals(sig).name
        st = sc.step(f"{name} at {int(frac * 100)}%", "explicit_error")
        child = sc.spawn("backfill", "--batch", str(ctx.batch))
        reached = sc.wait_progress(child, int(total * frac))
        state_at = ro_status(sc.dir)["state"]
        alive = child.signal(sig)
        child.finish(ctx.child_timeout)
        sc.children[-1]["rc"] = child.rc
        after = ro_status(sc.dir)
        if sig == signal.SIGTERM:
            by_signal = child.rc == -signal.SIGTERM
        else:
            by_signal = child.rc == 1 and "Aborted!" in child.stderr_text()
        sc.inject(st, "signal landed during backfill and ended the child",
                  reached and alive and by_signal and state_at == "backfill" and after["state"] == "backfill",
                  {"reached": reached, "alive": alive, "rc": child.rc, "state_at": state_at, "after": after})
        sc.check(st, "stage", "exit is non-zero or by signal", child.rc not in (0, None),
                 {"rc": child.rc, "stderr_tail": child.stderr_text()[-300:]})
        problems = backfill_invariant(sc.dir)
        sc.check(st, "invariant", "no partial step", not problems, problems)
    st = sc.step("--max-seconds stop", "explicit_error")
    child = sc.run_child("backfill", "--batch", str(ctx.batch), "--max-seconds", "0.2")
    out = child.json_out() or {}
    after = ro_status(sc.dir)
    sc.inject(st, "stopped before ready", after["state"] == "backfill", after)
    sc.check(st, "stage", "exit 0 and honest state",
             child.rc == 0 and out.get("state") == "backfill", {"rc": child.rc, "out": out})
    problems = backfill_invariant(sc.dir)
    sc.check(st, "invariant", "no partial step", not problems, problems)
    svc = sc.open_svc()
    st = sc.step("search while stopped", "verified_fallback")
    observed, _ = quality_pass(ctx, svc)
    sc.check(st, "surface", "memory path used", svc.memory_entries > 0, svc.memory_entries)
    sc.judge_pass(st, observed)
    sc.recover([("backfill", ("--batch", str(ctx.batch)))])


def _disk_limit(ctx: Ctx) -> int:
    return max(64 * 1024, ctx.index_bytes // 3)


def _d1_after(sc: Scenario, st: dict[str, Any], child_rc: int | None, out: dict[str, Any],
              error_words: tuple[str, ...]) -> None:
    after = ro_status(sc.dir)
    err = str(out.get("error", ""))
    sc.inject(st, "backfill failed on a write error after progress",
              child_rc not in (0, None) and any(w in err for w in error_words)
              and after["state"] == "backfill" and after["cursor"][0] is not None,
              {"rc": child_rc, "out": out, "after": after})
    sc.check(st, "stage", "exit is non-zero with an error JSON", child_rc not in (0, None) and err, out)
    problems = backfill_invariant(sc.dir)
    sc.check(st, "invariant", "no partial step", not problems, problems)


def _d1_search_and_recover(sc: Scenario) -> None:
    svc = sc.open_svc()
    st = sc.step("search after the failed backfill", "verified_fallback")
    observed, _ = quality_pass(sc.ctx, svc)
    sc.check(st, "surface", "memory path used", svc.memory_entries > 0, svc.memory_entries)
    sc.judge_pass(st, observed)
    sc.recover([("backfill", ("--batch", str(sc.ctx.batch)))])


def sc_d1a(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    sc.to_backfill()
    checkpoint_truncate(sc.dir)
    st = sc.step("backfill with RLIMIT_FSIZE", "explicit_error")
    limit = _disk_limit(ctx)
    st["notes"]["fsize_limit"] = limit
    child = sc.run_child("backfill", "--batch", str(ctx.batch), "--fsize-limit", str(limit))
    _d1_after(sc, st, child.rc, child.json_out() or {}, ("disk I/O error", "full"))
    _d1_search_and_recover(sc)


def unshare_available() -> bool:
    try:
        return subprocess.run(["unshare", "-Urm", "true"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def sc_d1b(sc: Scenario) -> None:
    ctx = sc.ctx
    if not unshare_available():
        st = sc.step("backfill on a size-limited tmpfs", "unavailable")
        st["notes"]["reason"] = "unshare -Urm failed in this environment"
        return
    sc.prepare()
    sc.to_backfill()
    checkpoint_truncate(sc.dir)
    sc.close_svc()
    st = sc.step("backfill on a size-limited tmpfs", "explicit_error")
    used = sum((sc.dir / n).stat().st_size for n in ("doc.db", "doc.db-wal", "doc.db-shm")
               if (sc.dir / n).exists())
    size = ((used + _disk_limit(ctx)) // 4096 + 1) * 4096
    st["notes"]["tmpfs_size"] = size
    (sc.dir / "enospc-mnt").mkdir()
    child = sc.run_child("enospc", "--size", str(size), "--batch", str(ctx.batch),
                         wrap=["unshare", "-Urm"])
    inner = child.tags("inner")
    copied = child.tags("copyback")
    sc.inject(st, "copy back finished", child.rc not in (None, 92, 93) and inner and copied,
              {"rc": child.rc, "stderr_tail": child.stderr_text()[-500:]})
    after = ro_status(sc.dir)
    keys = ("state", "cursor", "doc_count", "pending")
    sc.inject(st, "copy matches the tmpfs state",
              {k: copied[0].get(k) for k in keys} == {k: after.get(k) for k in keys},
              {"tmpfs": copied[0], "copy": after})
    _d1_after(sc, st, inner[0]["rc"], inner[0]["out"] or {}, ("full",))
    _d1_search_and_recover(sc)


def sc_d2(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    ids = [f"fault-d2-{i:06d}" for i in range(1, 202)]
    ctx.register(ids)
    plan = sc.dir / "plan.json"
    body = " ".join(f"{MARK}fill{j}" for j in range(400))
    plan.write_text(json.dumps([["new", i, f"{MARK}d{n} {body}"] for n, i in enumerate(ids[:-1])]),
                    encoding="utf-8")
    checkpoint_truncate(sc.dir)
    st = sc.step("writes with RLIMIT_FSIZE", "explicit_error")
    child = sc.run_child("writer", "--plan", str(plan), "--max-fail", "4",
                         "--fsize-limit", str(256 * 1024))
    acks = [t["id"] for t in child.tags("ack")]
    errs = child.tags("err")
    sc.inject(st, "a write failed with a write error",
              any("disk I/O error" in e["error"] for e in errs), errs[:4])
    pending = ro_pending(sc.dir)
    lost = [i for i in acks if ro_node(sc.dir, "evidence", i) is None]
    sc.check(st, "stage", "every acknowledged write is in the source", not lost, lost)
    stray = [e["id"] for e in errs
             if ro_node(sc.dir, "evidence", e["id"]) is not None and ("evidence", e["id"]) not in pending]
    sc.check(st, "stage", "a failed write is absent or pending", not stray, stray)
    st["notes"].update(acked=len(acks), failed=len(errs))
    svc = sc.open_svc()
    plan_rows = {r[1]: r[2] for r in json.loads(plan.read_text(encoding="utf-8"))}
    missing = []
    for node_id in acks:
        marker = plan_rows[node_id].split()[0]
        hits, warns = svc.hq._bm25_search_with_warnings(marker, None, 10, pack_ids=[ctx.measure_pack])
        if node_id not in [h["node_id"] for h in hits] and not sb._coverage(warns):
            missing.append(node_id)
    sc.check(st, "surface", "acknowledged writes are found or flagged", not missing, missing)
    sc.close_svc()

    st = sc.step("write with max_page_count", "explicit_error")
    conn = _plain(sc.dir)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    svc = sc.open_svc()
    node_id = ids[-1]
    space, node_type, nid, props = ctx.new_doc(node_id, f"{MARK}pagefull {body}")
    store_conn = svc.docs._conn
    pages = store_conn.execute("PRAGMA page_count").fetchone()[0]
    store_conn.execute(f"PRAGMA max_page_count={int(pages)}")
    error = None
    try:
        svc.docs.upsert_node_doc(space, node_type, nid, props)
    except Exception as exc:  # noqa: BLE001 -- the injected error
        error = repr(exc)
    finally:
        store_conn.execute("PRAGMA max_page_count=1073741823")
    present = ro_node(sc.dir, "evidence", node_id) is not None
    in_pending = ("evidence", node_id) in ro_pending(sc.dir)
    st["notes"].update(error=error, present=present, pending=in_pending)
    sc.inject(st, "page limit hit the write",
              (error is not None and "full" in error) or (present and in_pending),
              {"error": error, "present": present, "pending": in_pending})
    sc.check(st, "stage", "a raised write is absent or pending",
             error is None or not present or in_pending, {"error": error, "present": present})
    sc.check(st, "stage", "a returned write is in the source", error is not None or present, present)
    hits, warns = svc.hq._bm25_search_with_warnings(f"{MARK}pagefull", None, 10, pack_ids=[ctx.measure_pack])
    found = node_id in [h["node_id"] for h in hits]
    sc.check(st, "surface", "a stored write is found or flagged after the limit",
             not present or found or sb._coverage(warns), {"found": found, "warnings": warns})
    sc.recover([])


def sc_d3(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    keys = ctx.gold_keys()
    make_pending(sc.dir, keys)
    checkpoint_truncate(sc.dir)
    search_file = _write_search_file(sc, "search.json", ctx.queries)
    st = sc.step("search-time drain with RLIMIT_FSIZE", "explicit_error")
    child = sc.run_child("search", "--search-file", str(search_file), "--fsize-limit", str(64 * 1024),
                         "--debug-drain")
    err = child.stderr_text()
    sc.inject(st, "drain skipped on a write error",
              "search-time drain skipped" in err and "disk I/O error" in err, err[-800:])
    results = child.tags("result")
    sc.check(st, "surface", "every query answered", len(results) == len(ctx.queries), len(results))
    svc = sc.open_svc()
    viol = _bm25_results_violations(sc, results)
    sc.check(st, "surface", "unflagged results equal the oracle", not viol, viol[:20])
    verify = ro_verify(sc.dir)
    sc.check(st, "invariant", "verify excluding pending", verify["ok"], verify)
    st = sc.step("search without the limit", "explicit_error")
    observed, _ = quality_pass(ctx, svc)
    sc.judge_pass(st, observed)
    sc.recover([])


def sc_e1(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    sc.to_backfill()
    conn = _ro(sc.dir)
    try:
        keys = [(r[0], r[1]) for r in conn.execute("SELECT space, node_id FROM doc_nodes ORDER BY space, node_id")]
    finally:
        conn.close()
    key = keys[len(keys) // 2]
    st = sc.step("reindex failure on one key", "explicit_error")
    child = sc.run_child("backfill", "--batch", str(ctx.batch), "--fail-key", json.dumps(list(key)))
    out = child.json_out() or {}
    sc.inject(st, "the failure was injected", child.tags("inject"), child.stderr_text()[-500:])
    after = ro_status(sc.dir)
    sc.check(st, "stage", "exit non-zero and the error names the key",
             child.rc not in (0, None) and key[1] in str(out.get("error", "")), {"rc": child.rc, "out": out})
    cursor = tuple(after["cursor"]) if after["cursor"][0] is not None else None
    sc.check(st, "invariant", "cursor before the key and not ready",
             after["state"] == "backfill" and (cursor is None or cursor < key), after)
    problems = backfill_invariant(sc.dir)
    sc.check(st, "invariant", "no partial step", not problems, problems)
    _d1_search_and_recover(sc)


def _flip(block: bytes, positions: list[int]) -> bytes:
    data = bytearray(block)
    for pos in positions:
        if 0 <= pos < len(data):
            data[pos] ^= 0xFF
    return bytes(data)


def _corruption_observed(directory: Path) -> dict[str, Any]:
    from opencrab.stores import _node_bm25_sqlite as nbm

    out: dict[str, Any] = {}
    conn = _plain(directory)
    try:
        try:
            conn.execute(f"INSERT INTO {nbm.FTS}({nbm.FTS}, rank) VALUES('integrity-check', 1)")
            out["integrity_check"] = "passed"
        except sqlite3.Error as exc:
            out["integrity_check"] = repr(exc)
        total = int(conn.execute(f"SELECT total_len FROM {nbm.STATS} WHERE id=1").fetchone()[0])
        out["stats_total_len"] = total
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {nbm.VINST}").fetchone()[0]
            out["vinst_scan"] = f"rows={n}"
            out["vinst_rows"] = int(n)
        except sqlite3.Error as exc:
            out["vinst_scan"] = repr(exc)
    finally:
        conn.close()
    # The damage is observable when a check fails, the scan raises, or the
    # scan sees a token count other than the one the stats row recorded.
    out["observed"] = (out["integrity_check"] != "passed" or "vinst_rows" not in out
                       or out["vinst_rows"] != out["stats_total_len"])
    return out


def _corrupt(directory: Path, variant: str, seed: int) -> list[int]:
    from opencrab.stores import _node_bm25_sqlite as nbm

    table = f"{nbm.FTS}_data"
    conn = _plain(directory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if variant == "structure":
            rows = conn.execute(f"SELECT id, block FROM {table} WHERE id=10").fetchall()
        else:
            ids = [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE id > 10 ORDER BY id")]
            rng = random.Random(seed)
            chosen = rng.sample(ids, min(8, len(ids)))
            rows = conn.execute(f"SELECT id, block FROM {table} WHERE id IN ({','.join('?' * len(chosen))})",
                                chosen).fetchall()
        touched = []
        rng = random.Random(seed)
        for row_id, block in rows:
            n = len(block)
            positions = [n // 2, n // 3, (2 * n) // 3, 4] if variant == "structure" else \
                [rng.randrange(4, max(5, n)) for _ in range(4)]
            conn.execute(f"UPDATE {table} SET block=? WHERE id=?", (_flip(block, positions), row_id))
            touched.append(row_id)
        conn.execute("COMMIT")
        return touched
    finally:
        conn.close()


def _index_error_recovery(sc: Scenario, st: dict[str, Any]) -> None:
    verify = sc.cli("verify", record=False)
    sc.check(st, "stage", "verify CLI reports the damage with a non-zero exit", verify.rc not in (0, None),
             {"rc": verify.rc, "out": verify.json_out(), "stderr_tail": verify.stderr_text()[-300:]})
    reopen = sc.run_child("open")
    st["notes"]["reopen_status"] = (reopen.tags("opened") or [{}])[0].get("status")
    sc.close_svc()
    sc.recover([("drop", ("--yes",)), ("status", ()), ("backfill", ("--batch", str(sc.ctx.batch)))])


def _sc_e2(sc: Scenario, variant: str) -> None:
    ctx = sc.ctx
    st = sc.step(f"FTS5 {variant} damage", "explicit_error")
    observed_damage: dict[str, Any] = {}
    for attempt in range(5 if variant == "leaf" else 1):
        if sc.dir.exists():
            shutil.rmtree(sc.dir)
        sc.prepare()
        touched = _corrupt(sc.dir, variant, 542 + attempt)
        observed_damage = {"attempt": attempt, "blocks": touched, **_corruption_observed(sc.dir)}
        if observed_damage["observed"]:
            break
    st["notes"]["damage"] = observed_damage
    sc.inject(st, "damage observable by integrity-check or vocab scan", observed_damage.get("observed"),
              observed_damage)
    svc = sc.open_svc()
    st["notes"]["path_after_reopen"] = svc.path()
    observed, _ = quality_pass(ctx, svc)
    sc.judge_pass(st, observed)
    unavailable = sorted(q for q, o in observed.items()
                         if any("unavailable" in w for w in o["bm25_warnings"]))
    st["notes"]["unavailable_queries"] = unavailable
    _index_error_recovery(sc, st)


def sc_e2a(sc: Scenario) -> None:
    _sc_e2(sc, "structure")


def sc_e2b(sc: Scenario) -> None:
    _sc_e2(sc, "leaf")


def _candidate_keys(directory: Path, term: str, packs: list[str]) -> set[int]:
    from opencrab.stores import _node_bm25_sqlite as nbm

    conn = _ro(directory)
    try:
        sql = (f"SELECT v.doc FROM {nbm.VINST} v JOIN {nbm.DOCS} d ON d.doc_key = v.doc "
               f"WHERE v.term = ? AND d.pack_id IN ({','.join('?' * len(packs))}) GROUP BY v.doc")
        return {int(r[0]) for r in conn.execute(sql, [term, *packs])}
    finally:
        conn.close()


def sc_e3(sc: Scenario) -> None:
    from opencrab.ontology.text_cues import tokenize
    from opencrab.stores import _node_bm25_sqlite as nbm

    ctx = sc.ctx
    sc.prepare()
    q1, gold = _node_gold_query(ctx)
    q2 = next((q for q in ctx.queries if q["kind"] == "scope-filter" and q["forbidden"]), None)
    if q2 is None:
        raise NotInjectedError("no scope-filter query with a forbidden document")
    forbidden = next(f for f in q2["forbidden"] if ctx.truth[f][0])
    conn = _plain(sc.dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        g_key = conn.execute(f"SELECT doc_key FROM {nbm.DOCS} WHERE space=? AND node_id=?",
                             (ctx.truth[gold][1], gold)).fetchone()[0]
        f_key = conn.execute(f"SELECT doc_key FROM {nbm.DOCS} WHERE space=? AND node_id=?",
                             (ctx.truth[forbidden][1], forbidden)).fetchone()[0]
        conn.execute(f"DELETE FROM {nbm.FTS} WHERE rowid=?", (g_key,))
        conn.execute(f"DELETE FROM {nbm.DOCS} WHERE doc_key=?", (g_key,))
        conn.execute(f"UPDATE {nbm.DOCS} SET pack_id=? WHERE doc_key=?", (q2["pack_ids"][0], f_key))
        conn.execute("COMMIT")
    finally:
        conn.close()
    t1 = nbm.fts_term(tokenize(q1["text"])[0])
    t2 = nbm.fts_term(tokenize(q2["text"])[0])
    st1 = sc.step("E3i index entry of a gold document removed", "explicit_error")
    sc.inject(st1, "gold left the candidate set",
              g_key not in _candidate_keys(sc.dir, t1, ctx.ref_effective[q1["qid"]]), g_key)
    st2 = sc.step("E3ii pack of a forbidden document changed in the index", "explicit_error")
    sc.inject(st2, "forbidden document reached the candidate set",
              f_key in _candidate_keys(sc.dir, t2, [q2["pack_ids"][0]]), f_key)
    svc = sc.open_svc()
    observed, _ = quality_pass(ctx, svc)
    sc.judge_pass(st1, observed, only={q1["qid"]})
    sc.judge_pass(st2, observed, skip={q1["qid"]})
    _index_error_recovery(sc, st1)


def sc_k1(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    sc.to_backfill()
    total = ro_status(sc.dir)["rows"]
    prev: tuple | None = None
    for frac in (0.25, 0.5, 0.75):
        st = sc.step(f"SIGKILL at {int(frac * 100)}%", "explicit_error")
        child = sc.spawn("backfill", "--batch", str(ctx.batch))
        reached = sc.wait_progress(child, int(total * frac))
        alive = child.signal(signal.SIGKILL)
        child.finish(ctx.child_timeout)
        sc.children[-1]["rc"] = child.rc
        after = ro_status(sc.dir)
        sc.inject(st, "kill landed during backfill",
                  reached and alive and child.rc == -signal.SIGKILL and after["state"] == "backfill",
                  {"reached": reached, "alive": alive, "rc": child.rc, "after": after})
        cursor = tuple(after["cursor"])
        sc.check(st, "invariant", "cursor does not move back", prev is None or cursor >= prev,
                 [prev, cursor])
        prev = cursor
        problems = backfill_invariant(sc.dir)
        sc.check(st, "invariant", "no partial step", not problems, problems)
    svc = sc.open_svc()
    st = sc.step("search between kills", "verified_fallback")
    observed, _ = quality_pass(ctx, svc)
    sc.judge_pass(st, observed)
    sc.recover([("backfill", ("--batch", str(ctx.batch)))])


def sc_k2(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    ids = [f"fault-k2-{i:06d}" for i in range(1, 401)]
    ctx.register(ids)
    plan: list[list[str]] = []
    for n, node_id in enumerate(ids):
        plan.append(["new", node_id, f"{MARK}k{n} {MARK}knew body"])
        if n >= 2 and n % 3 == 0:
            plan.append(["edit", ids[n - 2], f"{MARK}e{n} {MARK}kedit body"])
        if n >= 5 and n % 5 == 0:
            plan.append(["delete", ids[n - 5], ""])
    path = sc.dir / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    st = sc.step("SIGKILL during writes", "explicit_error")
    pause_at = next(n for n, op in enumerate(plan) if n >= 40 and op[0] == "edit")
    child = sc.spawn("writer", "--plan", str(path), "--pause-at", str(pause_at))
    inside = child.wait_tag("inside", ctx.child_timeout)
    alive = child.signal(signal.SIGKILL)
    child.finish(ctx.child_timeout)
    sc.children[-1]["rc"] = child.rc
    begins = child.tags("begin")
    acks = child.tags("ack")
    sc.inject(st, "kill landed inside a write transaction",
              inside and alive and child.rc == -signal.SIGKILL and len(begins) == len(acks) + 1,
              {"inside": inside, "alive": alive, "begins": len(begins), "acks": len(acks), "rc": child.rc})
    # Design 3.3: acknowledged writes are in the source; the unacknowledged
    # write is in its prior state or fully committed.
    expected: dict[str, str | None] = {}
    for op in acks:
        expected[op["id"]] = None if op["op"] == "delete" else op["body"]
    flight = begins[len(acks)]
    wrong = []
    for node_id in sorted(set(expected) | {flight["id"]}):
        props = ro_node(sc.dir, "evidence", node_id)
        actual = None if props is None else props.get("text")
        allowed = {expected.get(node_id)}
        if node_id == flight["id"]:
            allowed.add(None if flight["op"] == "delete" else flight["body"])
        if actual not in allowed:
            wrong.append({"id": node_id, "allowed": sorted(map(str, allowed)), "actual": actual})
    sc.check(st, "stage", "acknowledged changes present, unacknowledged write prior or committed",
             not wrong, wrong[:10])
    props = ro_node(sc.dir, "evidence", flight["id"])
    st["notes"]["in_flight"] = {"op": flight["op"], "id": flight["id"],
                                "committed": (props or {}).get("text") == flight["body"]}
    verify = ro_verify(sc.dir)
    sc.check(st, "invariant", "verify excluding pending", verify["ok"], verify)
    sc.recover([])


def sc_k3(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    large = ctx.packs["l1"]["pack_id"]
    conn = _ro(sc.dir)
    try:
        keys = [(r[0], r[1]) for r in conn.execute(
            "SELECT space, node_id FROM doc_nodes WHERE json_extract(properties, '$.pack_id') = ?", (large,))]
    finally:
        conn.close()
    p0 = make_pending(sc.dir, keys)
    search_file = _write_search_file(sc, "search.json", ctx.queries[:1])
    st = sc.step("SIGKILL during the search-time drain", "explicit_error")
    child = sc.spawn("search", "--search-file", str(search_file), "--loop", "--slow-drain")
    deadline = time.monotonic() + ctx.child_timeout
    seen = p0
    while time.monotonic() < deadline and child.p.poll() is None:
        seen = len(ro_pending(sc.dir))
        if 0 < seen < p0:
            break
        time.sleep(POLL)
    alive = child.signal(signal.SIGKILL)
    child.finish(ctx.child_timeout)
    sc.children[-1]["rc"] = child.rc
    p1 = len(ro_pending(sc.dir))
    sc.inject(st, "kill landed mid-drain", alive and child.rc == -signal.SIGKILL and 0 < p1 < p0,
              {"alive": alive, "rc": child.rc, "p0": p0, "seen": seen, "p1": p1})
    verify = ro_verify(sc.dir)
    sc.check(st, "invariant", "verify excluding pending", verify["ok"], verify)
    svc = sc.open_svc()
    st = sc.step("search with pending left", "explicit_error")
    observed, _ = quality_pass(ctx, svc)
    sc.judge_pass(st, observed)
    sc.recover([("backfill", ("--batch", str(ctx.batch)))])


def sc_r1(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    sc.to_backfill()
    ids = [f"fault-r1-{i:06d}" for i in range(1, 61)]
    ctx.register(ids)
    measure_ids = sorted(i for i, (p, _s) in ctx.truth.items()
                         if p == ctx.measure_pack and not i.startswith("fault-"))[:20]
    svc = sc.open_svc()
    st = sc.step("service search during backfill", "verified_fallback")
    observed, _ = quality_pass(ctx, svc)
    sc.inject(st, "first pass on the memory path", svc.memory_entries > 0, svc.memory_entries)
    sc.judge_pass(st, observed)
    st = sc.step("writes during backfill", "explicit_error")
    # The child rests between backfill steps so writes land while it runs (design 4.6).
    child = sc.spawn("backfill", "--batch", str(max(1, ctx.node_count // 200)), "--step-delay", "0.02")
    sc.wait_progress(child, 1)
    writes: list[dict[str, Any]] = []
    n = 0
    while child.p.poll() is None and n < len(ids):
        status = ro_status(sc.dir)
        started = time.monotonic()
        if n % 2 == 0:
            space, node_type, nid, props = ctx.new_doc(ids[n], f"{MARK}r{n} body")
            svc.docs.upsert_node_doc(space, node_type, nid, props)
            kind = "store"
        else:
            # Read the index state inside the transaction that commits the edit (design 4.6).
            from opencrab.stores import _node_bm25_sqlite as nbm

            target = measure_ids[n % len(measure_ids)]
            conn = _plain(sc.dir)
            try:
                conn.execute("BEGIN IMMEDIATE")
                meta = nbm._meta(conn)
                conn.execute("UPDATE doc_nodes SET properties=json_set(properties, '$.text', ?) "
                             "WHERE space=? AND node_id=?",
                             (f"{MARK}redit{n} body", ctx.truth[target][1], target))
                conn.execute("COMMIT")
            finally:
                conn.close()
            status = {"state": meta.get("state"), "cursor": [meta.get("cursor_space"), meta.get("cursor_node_id")]}
            kind = "sql_in_tx"
        writes.append({"n": n, "kind": kind, "state": status.get("state"), "cursor": status.get("cursor"),
                       "t": round(started - child.started, 3)})
        n += 1
    child.finish(ctx.child_timeout)
    sc.children[-1]["rc"] = child.rc
    st["notes"]["writes"] = writes
    overlap = [w for w in writes if w["kind"] == "sql_in_tx" and w["state"] == "backfill"
               and w["cursor"] and w["cursor"][0] is not None]
    sc.inject(st, "a write overlapped the running backfill",
              child.rc == 0 and overlap and ro_status(sc.dir)["state"] == "ready",
              {"rc": child.rc, "overlap": len(overlap)})
    st = sc.step("same service after backfill", "explicit_error")
    sc.check(st, "surface", "same instance switched to the DB path", svc.path() == "db", svc.path())
    observed, _ = quality_pass(ctx, svc)
    sc.judge_pass(st, observed)
    search_file = _write_search_file(sc, "search.json", ctx.queries)
    restarted = sc.run_child("search", "--search-file", str(search_file))
    results = {r["qid"]: r for r in restarted.tags("result")}
    differ = [qid for qid, o in observed.items()
              if qid not in results or results[qid]["ids"] != o["bm25_ids"]
              or results[qid]["path"] != "db"]
    sc.check(st, "surface", "a new process returns the same BM25 results", not differ, differ)
    sc.recover([])


def sc_r2(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    svc = sc.open_svc()
    st = sc.step("restart with another MAX_NODE_CHARS", "verified_fallback")
    child = sc.run_child("open", env={"OPENCRAB_BM25_MAX_NODE_CHARS": "4096"})
    status = (child.tags("opened") or [{}])[0].get("status", {})
    sc.inject(st, "index reset to backfill with the new value",
              status.get("state") == "backfill" and status.get("max_text_chars") == 4096, status)
    observed, _ = quality_pass(ctx, svc)
    sc.check(st, "surface", "memory path used", svc.memory_entries > 0, svc.memory_entries)
    sc.judge_pass(st, observed)
    sc.close_svc()
    sc.recover([("status", ()), ("backfill", ("--batch", str(ctx.batch)))])


def sc_r3(sc: Scenario) -> None:
    ctx = sc.ctx
    sc.prepare()
    referenced = {i for q in ctx.queries for i in (*q["gold"], *q["forbidden"], *q["decoys"])}
    conn = _ro(sc.dir)
    try:
        filler = next(r[1] for r in conn.execute("SELECT space, node_id FROM doc_nodes ORDER BY node_id")
                      if r[1] not in referenced and ctx.truth.get(r[1], ("", ""))[0] != ctx.measure_pack)
        table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='doc_nodes'").fetchone()[0]
        index_sql = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='doc_nodes' AND sql IS NOT NULL")]
    finally:
        conn.close()
    sc.close_svc()
    st = sc.step("doc_nodes rebuilt without one row, then restart", "verified_fallback")
    conn = _plain(sc.dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(table_sql.replace("doc_nodes", "doc_nodes_rebuilt", 1))
        conn.execute("INSERT INTO doc_nodes_rebuilt SELECT * FROM doc_nodes WHERE node_id != ?", (filler,))
        conn.execute("DROP TABLE doc_nodes")
        conn.execute("ALTER TABLE doc_nodes_rebuilt RENAME TO doc_nodes")
        for sql in index_sql:
            conn.execute(sql)
        conn.execute("COMMIT")
    finally:
        conn.close()
    st["notes"]["removed"] = filler
    opened = sc.run_child("open")
    status = (opened.tags("opened") or [{}])[0].get("status", {})
    after = ro_status(sc.dir)
    sc.inject(st, "restart reset the index and recreated the triggers",
              status.get("state") == "backfill" and after.get("triggers") == 3, {"status": status, "after": after})
    svc = sc.open_svc()
    observed, _ = quality_pass(ctx, svc)
    sc.check(st, "surface", "memory path used", svc.memory_entries > 0, svc.memory_entries)
    sc.judge_pass(st, observed)
    sc.close_svc()
    sc.recover([("backfill", ("--batch", str(ctx.batch)))])


SCENARIOS: dict[str, Callable[[Scenario], None]] = {
    "T1": sc_t1, "T2": sc_t2, "T3": sc_t3, "T4": sc_t4,
    "C1": sc_c1, "C2": sc_c2, "C3": sc_c3,
    "D1a": sc_d1a, "D1b": sc_d1b, "D2": sc_d2, "D3": sc_d3,
    "E1": sc_e1, "E2a": sc_e2a, "E2b": sc_e2b, "E3": sc_e3,
    "K1": sc_k1, "K2": sc_k2, "K3": sc_k3,
    "R1": sc_r1, "R2": sc_r2, "R3": sc_r3,
}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _environment() -> dict[str, Any]:
    env = sb._environment("fault", dict(sb.BM25_DEFAULTS), {})
    env.pop("bm25_module_defaults", None)
    env.pop("bm25_defaults_match", None)
    env["sqlite"] = sqlite3.sqlite_version
    env["platform"] = platform.platform()
    return env


def _index_bytes(directory: Path) -> int:
    conn = _ro(directory)
    try:
        return int(conn.execute("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name LIKE 'doc_nodes_bm25%'")
                   .fetchone()[0])
    finally:
        conn.close()


def build_reference(ctx: Ctx) -> list[str]:
    """Measure the reference copy (design 3.1). Returns the problems."""
    sc = Scenario(ctx, "reference")
    sc.prepare()
    problems: list[str] = []
    status = ro_status(sc.dir)
    if status.get("state") != "ready" or status.get("pending") != 0:
        problems.append(f"reference index is not ready without pending: {status}")
    verify = ro_verify(sc.dir)
    if not verify["ok"]:
        problems.append(f"reference verify mismatch: {verify}")
    ctx.index_bytes = _index_bytes(sc.dir)
    svc = sc.open_svc()
    try:
        if svc.path() != "db":
            problems.append("reference does not use the DB path")
        observed, _ = quality_pass(ctx, svc)
        ctx.ref_effective = {qid: o["effective"] for qid, o in observed.items()}
        ctx.reference = {qid: {"classes": o["classes"], "missing": o["missing"],
                               "wrong_pack": o["wrong_pack"], "combined_ids": o["combined_ids"]}
                         for qid, o in observed.items()}
        index = ctx.oracle_index(sc.dir, svc.docs)
        ctx.reference_oracle = index
        ctx.reference_digest = rows_digest(sc.dir)
        ctx._oracle = None
        for q in ctx.queries:
            eff = ctx.ref_effective[q["qid"]]
            limit = _profile(q).bm25_limit
            got = svc.docs.node_bm25_search(q["text"], pack_ids=eff, spaces=q["spaces"], limit=limit)
            want = index.search(q["text"], spaces=q["spaces"], limit=limit, pack_ids=eff)
            if got is None or got[1] or _shape(got[0]) != _shape(want):
                problems.append(f"reference DB result differs from the oracle: {q['qid']}")
            if observed[q["qid"]]["bm25_cov"]:
                problems.append(f"reference has a coverage warning: {q['qid']}")
    finally:
        sc.close_svc()
    return problems


def cmd_run(args: argparse.Namespace) -> int:
    corpus, work = Path(args.corpus).resolve(), Path(args.work).resolve()
    root, out = Path(args.run_root).resolve(), Path(args.out).resolve()
    smoke = sb.corpus_is_smoke(corpus)
    sb.check_write_paths([root, out], sources=[corpus, work], smoke=smoke)
    if root.exists():
        raise SystemExit(f"run root already exists: {root}")
    if out.exists():
        raise SystemExit(f"report already exists: {out}")
    selected = list(SCENARIOS) if not args.scenarios else [s.strip() for s in args.scenarios.split(",")]
    unknown = [s for s in selected if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios: {unknown}")
    for key, value in sb.BM25_DEFAULTS.items():
        os.environ[key] = str(value)
    root.mkdir(parents=True)
    manifest, _packs, _queries = sb._read_corpus(corpus)
    watchdog = sb.Watchdog(manifest["preset"], "run", out, [root, out.parent], smoke=smoke)
    watchdog.budget = RUN_BUDGET[smoke]
    watchdog.extra["completed"] = []
    watchdog.start()
    ctx = Ctx(corpus, work, root, smoke, watchdog)
    ctx.empty_env.write_text("", encoding="utf-8")
    report: dict[str, Any] = {"verdict": "INCOMPLETE", "reasons": [], "environment": _environment(),
                              "corpus": {"preset": manifest["preset"], "seed": manifest["seed"],
                                         "node_count": manifest["node_count"],
                                         "query_count": len(ctx.queries)}}
    marker = work / "load-complete.json"
    if not marker.exists() or json.loads(marker.read_text(encoding="utf-8")).get("expected") != ctx.expected:
        report["reasons"].append("load-complete.json is missing or does not match the corpus")
        sb._atomic_json(out, report)
        return 2
    problems = build_reference(ctx)
    report["reference"] = {
        "problems": problems, "index_bytes": ctx.index_bytes,
        "classes": {leg: _count(c["classes"][leg] for c in ctx.reference.values())
                    for leg in ("bm25", "fts", "vector", "combined")},
        "partial_silent": sorted(q for q, r in ctx.reference.items() if "partial_silent" in r["classes"].values()),
    }
    if problems:
        report["reasons"].extend(problems)
        sb._atomic_json(out, report)
        return 2
    results = []
    for sid in selected:
        sc = Scenario(ctx, sid)
        started = time.monotonic()
        try:
            SCENARIOS[sid](sc)
        except NotInjectedError as exc:
            sc.steps.append({"name": "injection", "mode": "explicit_error", "notes": {"error": str(exc)},
                             "checks": [{"cat": "injection", "name": "confirmed", "ok": False,
                                         "detail": str(exc)}]})
        except Exception:  # noqa: BLE001 -- a harness failure makes the run INCOMPLETE
            import traceback

            sc.steps.append({"name": "harness error", "mode": "explicit_error", "notes": {},
                             "checks": [{"cat": "injection", "name": "harness ran", "ok": False,
                                         "detail": traceback.format_exc()[-3000:]}]})
        finally:
            sc.close_svc()
        res = sc.result()
        res["elapsed_s"] = round(time.monotonic() - started, 1)
        sc.dir.mkdir(parents=True, exist_ok=True)
        sb._atomic_json(sc.dir / "result.json", res)
        results.append(res)
        watchdog.extra["completed"] = [r["id"] for r in results]
        print(json.dumps({"scenario": sid, "value": res["value"], "elapsed_s": res["elapsed_s"]}), flush=True)
    report["scenarios"] = results
    report["faults"] = {fault: worst([r["value"] for r in results if r["id"] in ids])
                        for fault, ids in FAULTS.items() if any(r["id"] in ids for r in results)}
    report["unavailable"] = [r["id"] for r in results if r["value"] == "unavailable"]
    not_injected = [r["id"] for r in results if r["value"] == "not_injected"]
    if not_injected:
        report["reasons"].append(f"not injected: {not_injected}")
    report["verdict"] = "COMPLETE" if not report["reasons"] else "INCOMPLETE"
    report["peak_rss_bytes_sampled"] = watchdog.peak_rss
    sb._atomic_json(out, report)
    print(json.dumps({"verdict": report["verdict"], "faults": report["faults"],
                      "reasons": report["reasons"]}, ensure_ascii=False))
    return 0 if report["verdict"] == "COMPLETE" else 2


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ---------------------------------------------------------------------------
# actor (child process roles)
# ---------------------------------------------------------------------------


def _actor_store(directory: Path) -> Any:
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(directory / "doc.db"))


def _wait_go(args: argparse.Namespace) -> None:
    if args.wait_go:
        line = sys.stdin.readline()
        if line.strip() != "go":
            raise SystemExit(95)


def _actor_cli(role: str, directory: Path, args: argparse.Namespace) -> int:
    import opencrab.cli as cli
    from opencrab.stores import _node_bm25_sqlite as nbm

    def make(settings: Any, doc: bool = False, **_kw: Any) -> Any:
        store = _actor_store(directory)
        if args.step_delay > 0:
            step = store.node_bm25_backfill_step

            def slow_step(*a: Any, **kw: Any) -> Any:
                result = step(*a, **kw)
                time.sleep(args.step_delay)
                return result

            store.node_bm25_backfill_step = slow_step
        _tag("opened", {"available": store.available, "supports": store.supports_node_bm25})
        _wait_go(args)
        return SimpleNamespace(doc=store)

    cli._make_stores = make
    if args.fail_key:
        key = tuple(json.loads(args.fail_key))
        original = nbm.reindex_key

        def flaky(conn: Any, row_to_node: Any, space: str, node_id: str, meta: Any = None) -> None:
            if (space, node_id) == key:
                _tag("inject", {"key": list(key)})
                raise RuntimeError("injected reindex failure")
            original(conn, row_to_node, space, node_id, meta)

        nbm.reindex_key = flaky
    commands = {"backfill": cli.packs_bm25_index_backfill, "verify": cli.packs_bm25_index_verify,
                "drop": cli.packs_bm25_index_drop, "status": cli.packs_bm25_index_status}
    argv: list[str] = []
    if role == "backfill":
        argv = ["--batch", str(args.batch)]
        if args.max_seconds is not None:
            argv += ["--max-seconds", str(args.max_seconds)]
    elif role == "drop":
        argv = ["--yes"]
    try:
        commands[role].main(args=argv, prog_name=f"bm25-index {role}", standalone_mode=True)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def _searcher(directory: Path) -> tuple[Any, Any]:
    from opencrab.ontology.query import HybridQuery

    store = _actor_store(directory)
    hq = HybridQuery(sb.NoGraph(), sb.NoGraph())
    hq._doc_store = store
    return store, hq


def _search_rows(hq: Any, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        path = "db" if hq._node_index_store() is not None else "memory"
        hits, warns = hq._bm25_search_with_warnings(row["text"], row["spaces"], row["limit"],
                                                    pack_ids=row["effective"])
        _tag("result", {"qid": row["qid"], "ids": [h["node_id"] for h in hits], "warnings": list(warns),
                        "limit": row["limit"], "effective": row["effective"], "path": path})


def _actor_open(directory: Path, args: argparse.Namespace) -> int:
    store = _actor_store(directory)
    status = store.node_bm25_status() if store.supports_node_bm25 else {"state": "unsupported"}
    _tag("opened", {"status": status, "available": store.available, "supports": store.supports_node_bm25})
    _wait_go(args)
    if args.search_file:
        from opencrab.ontology.query import HybridQuery

        hq = HybridQuery(sb.NoGraph(), sb.NoGraph())
        hq._doc_store = store
        _search_rows(hq, json.loads(Path(args.search_file).read_text(encoding="utf-8")))
    if args.write_id:
        manifest, packs, _q = sb._read_corpus(Path(args.corpus))
        doc = {"id": args.write_id, "pack": "measure", "form": "N", "space": "evidence",
               "node_type": "Note", "title": "fault", "text": f"{MARK}open body"}
        space, node_type, node_id, props = sb.doc_rows(doc, packs)["node"]
        try:
            store.upsert_node_doc(space, node_type, node_id, props)
            _tag("ack", {"id": node_id})
        except Exception as exc:  # noqa: BLE001 -- reported to the parent
            _tag("err", {"id": node_id, "error": repr(exc)})
    _tag("status", {"supports": store.supports_node_bm25})
    store.close()
    return 0


def _actor_writer(directory: Path, args: argparse.Namespace) -> int:
    _manifest, packs, _q = sb._read_corpus(Path(args.corpus))
    store = _actor_store(directory)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    current = {"n": -1}
    if args.pause_at >= 0:
        # K2: stop inside the write transaction, after the doc_nodes write and
        # before the commit, so the parent's SIGKILL lands mid-transaction.
        from opencrab.stores import _node_bm25_sqlite as nbm

        original = nbm.reindex_key

        def paused(*a: Any, **kw: Any) -> None:
            if current["n"] == args.pause_at:
                _tag("inside", {"n": current["n"]})
                time.sleep(args.timeout + 60)
            original(*a, **kw)

        nbm.reindex_key = paused
    fails = 0
    for n, (op, node_id, body) in enumerate(plan):
        current["n"] = n
        _tag("begin", {"op": op, "id": node_id, "body": body})
        try:
            if op == "delete":
                store.delete_node_doc("evidence", node_id)
            else:
                doc = {"id": node_id, "pack": "measure", "form": "N", "space": "evidence",
                       "node_type": "Note", "title": "fault", "text": body}
                space, node_type, nid, props = sb.doc_rows(doc, packs)["node"]
                store.upsert_node_doc(space, node_type, nid, props)
            _tag("ack", {"op": op, "id": node_id, "body": body})
        except Exception as exc:  # noqa: BLE001 -- reported to the parent
            fails += 1
            _tag("err", {"op": op, "id": node_id, "error": repr(exc)})
            if fails >= args.max_fail:
                break
    store.close()
    return 0


def _actor_search(directory: Path, args: argparse.Namespace) -> int:
    import logging

    if args.debug_drain:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        logging.getLogger("opencrab.stores.local_sql_doc_store").setLevel(logging.DEBUG)
    store, hq = _searcher(directory)
    if args.slow_drain:
        # K3: slow the drain so a kill lands between drain transactions.
        store._NODE_BM25_DRAIN_BATCH = 16
        store._NODE_BM25_DRAIN_SECONDS = 0.01
    rows = json.loads(Path(args.search_file).read_text(encoding="utf-8"))
    if not args.loop:
        _search_rows(hq, rows)
    else:
        from opencrab.stores import _node_bm25_sqlite as nbm

        while True:
            _search_rows(hq, rows)
            left = nbm.pending_count(store._conn)
            _tag("pending", {"n": left})
            if left == 0:
                break
            time.sleep(0.1)
    store.close()
    return 0


def _actor_enospc(directory: Path, args: argparse.Namespace) -> int:
    """D1b outer child: run inside ``unshare -Urm`` (design 4.3, 5.2)."""
    mnt = directory / "enospc-mnt"
    files = ("doc.db", "doc.db-wal", "doc.db-shm")
    if subprocess.run(["mount", "-t", "tmpfs", "-o", f"size={args.size}", "tmpfs", str(mnt)]).returncode != 0:
        return 90
    for name in files:
        if (directory / name).exists():
            shutil.copy2(directory / name, mnt / name)
    inner = subprocess.run(
        [sys.executable, str(SCRIPT), "actor", "backfill", "--dir", str(mnt), "--corpus", args.corpus,
         "--timeout", str(args.timeout), "--batch", str(args.batch)],
        capture_output=True, text=True, timeout=args.timeout)
    text = "\n".join(line for line in inner.stdout.splitlines() if not line.startswith("@@ "))
    start, end = text.find("{"), text.rfind("}")
    try:
        out = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
    except json.JSONDecodeError:
        out = None
    _tag("inner", {"rc": inner.returncode, "out": out, "stderr_tail": inner.stderr[-500:]})
    try:
        status = ro_status(mnt)
    except sqlite3.Error as exc:
        _tag("copyback_status_error", {"error": repr(exc)})
        return 93
    try:
        for name in ("doc.db-wal", "doc.db-shm"):
            if (directory / name).exists():
                (directory / name).unlink()
        for name in files:
            if (mnt / name).exists():
                shutil.copy2(mnt / name, directory / name)
    except OSError as exc:
        _tag("copyback_error", {"error": repr(exc)})
        return 92
    _tag("copyback", status)
    return inner.returncode


def cmd_actor(args: argparse.Namespace) -> int:
    # The D1b outer child outlives its inner child by the copy back.
    signal.alarm(max(1, int(args.timeout)) + (60 if args.role == "enospc" else 0))
    if args.fsize_limit:
        resource.setrlimit(resource.RLIMIT_FSIZE, (args.fsize_limit, args.fsize_limit))
    directory = Path(args.dir)
    if args.role in ("backfill", "verify", "drop", "status"):
        return _actor_cli(args.role, directory, args)
    if args.role == "open":
        return _actor_open(directory, args)
    if args.role == "writer":
        return _actor_writer(directory, args)
    if args.role == "search":
        return _actor_search(directory, args)
    if args.role == "enospc":
        return _actor_enospc(directory, args)
    raise SystemExit(f"unknown role {args.role}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--corpus", required=True)
    run.add_argument("--work", required=True)
    run.add_argument("--run-root", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--scenarios", default="")
    actor = sub.add_parser("actor")
    actor.add_argument("role", choices=("backfill", "verify", "drop", "status", "open", "writer",
                                        "search", "enospc"))
    actor.add_argument("--dir", required=True)
    actor.add_argument("--corpus", required=True)
    actor.add_argument("--timeout", type=int, required=True)
    actor.add_argument("--fsize-limit", type=int, default=0)
    actor.add_argument("--batch", type=int, default=500)
    actor.add_argument("--max-seconds", type=float, default=None)
    actor.add_argument("--fail-key", default="")
    actor.add_argument("--wait-go", action="store_true")
    actor.add_argument("--plan", default="")
    actor.add_argument("--max-fail", type=int, default=10**9)
    actor.add_argument("--search-file", default="")
    actor.add_argument("--write-id", default="")
    actor.add_argument("--loop", action="store_true")
    actor.add_argument("--slow-drain", action="store_true")
    actor.add_argument("--debug-drain", action="store_true")
    actor.add_argument("--size", type=int, default=0)
    actor.add_argument("--pause-at", type=int, default=-1)
    actor.add_argument("--step-delay", type=float, default=0.0)
    actor.add_argument("--yes", action="store_true", help="accepted for drop; the actor always confirms")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "actor":
        return cmd_actor(args)
    return cmd_selftest()


if __name__ == "__main__":
    raise SystemExit(main())
