#!/usr/bin/env python3
"""Search baseline harness for issue #542.

Design and run guide: docs/issue-542-search-baseline.md.

Subcommands:
    load     --corpus DIR --work DIR
    run      --corpus DIR --work DIR --run-dir DIR --out REPORT [--config default|scaled-cap]
    compare  BASE_REPORT HEAD_REPORT
    selftest

The module top level uses the standard library only. ``run`` sets the BM25
environment values before it imports ``opencrab.ontology.query``, because that
module reads ``OPENCRAB_BM25_NODE_LIMIT`` at import time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
GIB = 1 << 30

# Immediate stop conditions (design section 9.2).
RSS_ABORT_BYTES = int(1.6 * GIB)
MEM_AVAILABLE_MIN = GIB
SWAP_FREE_MIN = GIB
QUERY_MAX_SECONDS = 60.0
DISK_FREE_MIN = 10 * GIB
BUDGET_GENERATE_LOAD = {"S0": 300, "S1": 1800, "S2": 3600, "S3": 7200, "S4": 14400}
BUDGET_RUN = {"S0": 300, "S1": 1800, "S2": 3600, "S3": 5400, "S4": 10800}

# Requirements (design section 4). They were fixed before any measurement.
REQ_LATENCY_P95_MS = {"explicit_single": 800.0, "explicit_multi": 2000.0,
                      "whole_scope": 2000.0, "auto": 2500.0}
REQ_LEG_P95_MS = 500.0
REQ_CONCURRENT_FACTOR = 2.0
REQ_COLD_S = 15.0
REQ_FRESH_S = {"bm25": 10.0, "fts": 1.0, "vector": 1.0}
REQ_PEAK_RSS = GIB
REQ_INDEX_RATIO = 2.0
REQ_WAL_BYTES = 512 * (1 << 20)
REQ_WRITE_P95_MS = 500.0
REQ_LOCK_WAIT_P95_MS = 2000.0

BM25_DEFAULTS = {
    "OPENCRAB_BM25_NODE_LIMIT": 50000,
    "OPENCRAB_BM25_CHAR_BUDGET": 200_000_000,
    "OPENCRAB_BM25_MAX_NODE_CHARS": 65536,
}
SCALED_CAP_FRACTION = 0.25
EMBED_DIM = 64
VECTOR_TABLE = "vectors_kure"
DISPLAY_NAMES = {"user_a": "search-baseline A", "user_b": "search-baseline B"}
COVERAGE_PREFIX = "BM25 coverage"
OUT_OF_SCOPE = "pack_ids_out_of_scope"


# ---------------------------------------------------------------------------
# Safety: paths and watchdog
# ---------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# RAM-backed filesystems (design 9.1). This device's /tmp is tmpfs; a CI
# runner's /tmp is usually a disk directory, so the guard judges the medium,
# not the path name.
RAM_FS_TYPES = frozenset({"tmpfs", "ramfs"})
# Smoke scale (repository tests). Measured S0: docs.jsonl about 1.7MB and
# 1170 lines, load dir about 9.5MB. S1 docs.jsonl is about 22MB.
SMOKE_MAX_CORPUS_BYTES = 4 * (1 << 20)
SMOKE_MAX_LINES = 2000
DISK_FREE_MIN_SMOKE = 512 * (1 << 20)


def _unescape_mount(field: str) -> str:
    """Undo the octal escapes (for example ``\\040`` for a space) of mountinfo."""
    out: list[str] = []
    i = 0
    while i < len(field):
        code = field[i + 1:i + 4]
        if field[i] == "\\" and len(code) == 3 and all(c in "01234567" for c in code):
            out.append(chr(int(code, 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def _resolve_for_mount(path: Path) -> str:
    """Return the real path the way Path.resolve() computes it.

    The function never folds ".." as text before it resolves symlinks.
    os.path.realpath walks the components in order, follows each symlink
    (also a dangling one), and folds ".." against the resolved prefix.
    """
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raw = os.path.join(os.getcwd(), raw)
    return os.path.realpath(raw)


def fs_type_for(path: Path, mountinfo_text: str) -> str | None:
    """Return the filesystem type of the mount that holds ``path``.

    The function picks the longest mount point that matches ``path`` at a path
    component boundary. It returns None when no mount point matches.
    """
    target = _resolve_for_mount(path)
    best: tuple[int, str] | None = None
    for line in mountinfo_text.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        rest = right.split()
        if not sep or len(fields) < 5 or not rest:
            continue
        mount = _unescape_mount(fields[4])
        if mount != "/" and target != mount and not target.startswith(mount.rstrip("/") + "/"):
            continue
        if best is None or len(mount) > best[0]:
            best = (len(mount), rest[0])
    return best[1] if best else None


def _read_proc_mountinfo() -> str:
    return Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")


def default_fs_type(path: Path, read_mountinfo: Any = None) -> str | None:
    """Return the filesystem type of ``path`` from /proc/self/mountinfo, or None."""
    try:
        text = (read_mountinfo or _read_proc_mountinfo)()
    except OSError:
        return None
    return fs_type_for(path, text)


def corpus_is_smoke(corpus: Path) -> bool:
    """Judge smoke scale from the real corpus file, never from the manifest preset."""
    docs = corpus / "docs.jsonl"
    try:
        if docs.stat().st_size > SMOKE_MAX_CORPUS_BYTES:
            return False
        with docs.open("rb") as fh:
            lines = sum(1 for _ in fh)
    except OSError:
        return False
    return lines <= SMOKE_MAX_LINES


def preset_is_smoke(preset: str) -> bool:
    """Judge smoke scale from the generator's own preset table (code constants)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from search_corpus import PRESETS

    spec = PRESETS[preset]
    total = (sum(spec["large"]) + sum(spec["small"]) + spec["other_private"] + spec["other_public"]
             + spec["creating"] + spec["measure"] + spec["unpackaged"])
    return total <= SMOKE_MAX_LINES


def disk_condition(free_bytes: int, smoke: bool) -> bool:
    """Return True when the free space on a write path must stop the run (design 9.2)."""
    return free_bytes < (DISK_FREE_MIN_SMOKE if smoke else DISK_FREE_MIN)


def check_write_paths(paths: list[Path], sources: list[Path] | None = None, *, smoke: bool,
                      fs_type_of: Any = None) -> None:
    """Refuse write paths inside the repository, the live data dir or a read source.

    ``sources`` are the read-only inputs (corpus and load directories). The
    function refuses a write path inside a source or a write path that
    contains a source. The caller sets ``TMPDIR``, and that path gets the same
    checks. When the run is not smoke scale, the function resolves each path
    to its real path and refuses it when that real path is on a RAM-backed
    filesystem. It treats an unknown filesystem type as RAM-backed (design 9.1).
    """
    fs_type_of = fs_type_of or default_fs_type
    try:
        from opencrab.config import get_settings

        live = Path(get_settings().local_data_dir).expanduser()
    except Exception as exc:  # noqa: BLE001 -- refuse when the live dir is unknown
        raise SystemExit(f"cannot resolve the live data dir, refusing to start: {type(exc).__name__}")
    tmpdir = os.environ.get("TMPDIR")
    if not tmpdir:
        raise SystemExit("TMPDIR is not set; set it to a disk path outside the repository")
    tmp_path = Path(tmpdir)
    sources = sources or []
    for path in [*paths, tmp_path]:
        if _inside(path, REPO_ROOT):
            raise SystemExit(f"write path is inside the repository: {path}")
        if _inside(path, live) or _inside(live, path):
            raise SystemExit(f"write path overlaps the live data dir: {path}")
        for source in sources:
            if _inside(path, source) or _inside(source, path):
                raise SystemExit(f"write path overlaps a read source: {path}")
        if not smoke:
            # Judge the real path, so direct callers and the CLI (which writes
            # to Path.resolve() of its arguments) get the same answer.
            real = Path(_resolve_for_mount(path))
            fs_type = fs_type_of(real)
            if fs_type is None or fs_type in RAM_FS_TYPES:
                raise SystemExit(f"write path is on a RAM-backed or unknown filesystem "
                                 f"({fs_type}): {path} (real path {real})")


def _read_kib(path: str, key: str) -> int | None:
    try:
        with open(path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


class Watchdog(threading.Thread):
    """Stop the process when a design section 9.2 condition holds."""

    def __init__(self, preset: str, phase: str, abort_path: Path, disk_paths: list[Path],
                 *, spent_seconds: float = 0.0, smoke: bool = False,
                 read_kib: Any = None, disk_free_of: Any = None) -> None:
        super().__init__(name="search-baseline-watchdog", daemon=True)
        budgets = BUDGET_RUN if phase == "run" else BUDGET_GENERATE_LOAD
        # generate and load share one budget: load receives what generate left.
        self.budget = float(budgets.get(preset, budgets["S4"])) - spent_seconds
        self.phase = phase
        self.abort_path = abort_path
        self.disk_paths = disk_paths
        self.smoke = smoke
        self._read_kib = read_kib or _read_kib
        self._disk_free_of = disk_free_of or (lambda probe: shutil.disk_usage(probe).free)
        self.started = time.monotonic()
        self.peak_rss = 0
        # One slot per thread that makes store calls (main and writer).
        self._ops: dict[str, tuple[str, float]] = {}
        self._ops_lock = threading.Lock()
        self.extra: dict[str, Any] = {}

    def op_start(self, name: str, slot: str = "main") -> None:
        with self._ops_lock:
            self._ops[slot] = (name, time.monotonic())

    def op_end(self, slot: str = "main") -> None:
        with self._ops_lock:
            self._ops.pop(slot, None)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def _condition(self) -> str | None:
        rss = self._read_kib("/proc/self/status", "VmRSS") or 0
        self.peak_rss = max(self.peak_rss, rss)
        if rss > RSS_ABORT_BYTES:
            return "process_rss"
        available = self._read_kib("/proc/meminfo", "MemAvailable")
        if available is not None and available < MEM_AVAILABLE_MIN:
            return "mem_available"
        swap_total = self._read_kib("/proc/meminfo", "SwapTotal")
        swap_free = self._read_kib("/proc/meminfo", "SwapFree")
        if swap_total and swap_free is not None and swap_free < SWAP_FREE_MIN:
            return "swap_free"
        with self._ops_lock:
            ops = list(self._ops.items())
        now = time.monotonic()
        for slot, (name, started) in ops:
            if now - started > QUERY_MAX_SECONDS:
                return f"operation_timeout:{slot}:{name}"
        for path in self.disk_paths:
            probe = path if path.exists() else path.parent
            if probe.exists() and disk_condition(self._disk_free_of(probe), self.smoke):
                return "disk_free"
        if self.elapsed() > self.budget:
            return "time_budget"
        return None

    def run(self) -> None:
        while True:
            condition = self._condition()
            if condition is not None:
                record = {"verdict": "ABORTED", "condition": condition, "phase": self.phase,
                          "elapsed_s": round(time.monotonic() - self.started, 3),
                          "peak_rss_bytes": self.peak_rss, **self.extra}
                try:
                    self.abort_path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_json(self.abort_path, record)
                finally:
                    sys.stderr.write(f"ABORTED {condition}\n")
                    sys.stderr.flush()
                    os._exit(3)
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Embedding, graph stub and the row contract (design 5.4, 5.6)
# ---------------------------------------------------------------------------


class HashEmbedding:
    """Deterministic token-hash embedding. No network, no model file."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self._dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return [self._vec(text) for text in input]

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return self(input)

    def name(self) -> str:
        return "search_baseline_hash"

    def _vec(self, text: str) -> list[float]:
        from opencrab.ontology.text_cues import tokenize

        vec = [0.0] * self._dim
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self._dim
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]


class NoGraph:
    """Graph store stand-in that turns the graph leg off (design section 2)."""

    available = False


def _principal(user_id: str) -> Any:
    from opencrab.auth import Principal

    return Principal(user_id=user_id, is_local=False, disabled=False)


def doc_rows(doc: dict[str, Any], packs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the rows the production write paths leave for one corpus doc."""
    from opencrab.ontology.bm25 import _node_text
    from opencrab.pack.write_gate import NODE_STAMPED, SOURCE_STAMPED, stamp

    pack = packs.get(doc["pack"]) if doc["pack"] else None
    node_id = doc["id"]
    if doc["form"] == "N":
        base = {"title": doc["title"], "text": doc["text"]}
        if pack is None:
            props = {**base, "pack_id": ""}
        else:
            props = stamp(base, principal=_principal(pack["owner"]), pack_id=pack["pack_id"],
                          keys=NODE_STAMPED)
        node = (doc["space"], doc["node_type"], node_id, props)
        vtext = _node_text({"node_id": node_id, "node_type": doc["node_type"], "properties": props})
        pid = str(props.get("pack_id") or "")
        vector = None
        if vtext.strip():
            vector = (node_id, vtext, {"pack_id": pid, "source": pid, "node_id": node_id,
                                       "space": doc["space"]})
        return {"node": node, "source": None, "vector": vector}
    if pack is None:
        raise ValueError(f"source form needs a pack: {node_id}")
    principal = _principal(pack["owner"])
    meta = stamp({"title": doc["title"]}, principal=principal, pack_id=pack["pack_id"],
                 keys=SOURCE_STAMPED)
    meta.setdefault("space", "evidence")
    node_props: dict[str, Any] = {"pack_id": pack["pack_id"], "text": doc["text"]}
    if meta.get("title"):
        node_props["title"] = meta["title"]
    props = stamp(node_props, principal=principal, pack_id=pack["pack_id"], keys=NODE_STAMPED)
    vmeta = dict(meta)
    vmeta["source_id"] = node_id
    return {
        "node": ("evidence", "TextUnit", node_id, props),
        "source": (node_id, doc["text"], dict(meta)),
        "vector": (node_id, doc["text"], vmeta),
    }


# ---------------------------------------------------------------------------
# Expected and observed state (design 8.2)
# ---------------------------------------------------------------------------


def _canon(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _TableHash:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, row: list[Any]) -> None:
        self.lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))

    def result(self) -> dict[str, Any]:
        h = hashlib.sha256()
        for line in sorted(self.lines):
            h.update(line.encode("utf-8"))
            h.update(b"\n")
        return {"rows": len(self.lines), "sha256": h.hexdigest()}


def _read_corpus(corpus: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    packs = json.loads((corpus / "packs.json").read_text(encoding="utf-8"))["packs"]
    queries = json.loads((corpus / "queries.json").read_text(encoding="utf-8"))
    return manifest, packs, queries


def _iter_docs(corpus: Path):
    with (corpus / "docs.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


def expected_state(corpus: Path) -> dict[str, Any]:
    """Compute the five table hashes from the corpus and the row contract only."""
    from opencrab.stores._json import dump_props
    from opencrab.stores.chroma_store import _sanitize_metadata

    _manifest, packs, _queries = _read_corpus(corpus)
    tables = {name: _TableHash() for name in ("doc_nodes", "doc_sources", "vector", "packs", "users")}
    for doc in _iter_docs(corpus):
        rows = doc_rows(doc, packs)
        space, node_type, node_id, props = rows["node"]
        tables["doc_nodes"].add([space, node_id, node_type, _canon(dump_props(props)), doc["updated_at"]])
        if rows["source"] is not None:
            sid, text, meta = rows["source"]
            tables["doc_sources"].add([sid, text, _canon(dump_props(meta))])
        if rows["vector"] is not None:
            vid, vtext, vmeta = rows["vector"]
            tables["vector"].add([vid, vtext, _canon(json.dumps(_sanitize_metadata(vmeta)))])
    for user, display in DISPLAY_NAMES.items():
        tables["users"].add([user, display, 0])
        tables["packs"].add([f"default:{user}", user, "ready", "private", "Default pack", None, 1])
    for pack in packs.values():
        tables["packs"].add([pack["pack_id"], pack["owner"], pack["status"], pack["visibility"],
                             pack["title"], pack["description"], 0])
    return {name: table.result() for name, table in tables.items()}


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def observed_state(directory: Path) -> tuple[dict[str, Any], list[str]]:
    """Read the five tables with read-only connections. Returns (state, problems)."""
    import sqlite_vec

    from opencrab.pack.ownership import _DEFAULT_PACK_ID_PREFIX

    problems: list[str] = []
    tables = {name: _TableHash() for name in ("doc_nodes", "doc_sources", "vector", "packs", "users")}
    default_ids: set[str] = set()
    sql = _ro(directory / "sql.db")
    try:
        for row in sql.execute("SELECT user_id, display_name, is_local FROM users"):
            tables["users"].add([row[0], row[1], int(row[2])])
        for row in sql.execute(
            "SELECT pack_id, owner_id, status, visibility, title, description, is_default FROM packs"
        ):
            pack_id = row[0]
            if int(row[6]) == 1:
                if not pack_id.startswith(_DEFAULT_PACK_ID_PREFIX):
                    problems.append("default pack id without the module prefix")
                default_ids.add(pack_id)
                pack_id = f"default:{row[1]}"
            tables["packs"].add([pack_id, row[1], row[2], row[3], row[4], row[5], int(row[6])])
    finally:
        sql.close()
    doc = _ro(directory / "doc.db")
    try:
        for row in doc.execute("SELECT space, node_id, node_type, properties, updated_at FROM doc_nodes"):
            tables["doc_nodes"].add([row[0], row[1], row[2], _canon(row[3]), row[4]])
            if json.loads(row[3]).get("pack_id") in default_ids:
                problems.append("doc_nodes row in a default pack")
        for row in doc.execute("SELECT source_id, text, metadata FROM doc_sources"):
            tables["doc_sources"].add([row[0], row[1], _canon(row[2])])
            if json.loads(row[2] or "{}").get("pack_id") in default_ids:
                problems.append("doc_sources row in a default pack")
    finally:
        doc.close()
    vec = _ro(directory / "vectors.db")
    try:
        vec.enable_load_extension(True)
        sqlite_vec.load(vec)
        vec.enable_load_extension(False)
        for row in vec.execute(f"SELECT node_id, document, metadata FROM {VECTOR_TABLE}"):
            tables["vector"].add([row[0], row[1], _canon(row[2])])
            if json.loads(row[2] or "{}").get("pack_id") in default_ids:
                problems.append("vector row in a default pack")
    finally:
        vec.close()
    return {name: table.result() for name, table in tables.items()}, sorted(set(problems))


def _state_diff(expected: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    return sorted(name for name in expected if expected[name] != observed.get(name))


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def _create_users(sql: Any) -> None:
    """create_user's one-transaction contract with fixed ids (design 5.3)."""
    from sqlalchemy import text

    from opencrab.pack.ownership import ensure_default_pack

    with sql._engine.begin() as conn:
        for user, display in DISPLAY_NAMES.items():
            conn.execute(
                text("INSERT INTO users (user_id, display_name, is_local) VALUES (:u, :d, :l)"),
                {"u": user, "d": display, "l": False},
            )
            ensure_default_pack(sql, user, conn=conn)


def _register_packs(sql: Any, packs: dict[str, dict[str, Any]]) -> None:
    from opencrab.pack.ownership import begin_pack_creation, create_pack, set_visibility

    for pack in packs.values():
        if pack["status"] == "creating":
            assigned = begin_pack_creation(sql, pack["owner"], pack["pack_id"], pack["title"],
                                           pack["description"])
        else:
            assigned = create_pack(sql, pack["owner"], pack["pack_id"], pack["title"],
                                   pack["description"])
        if assigned != pack["pack_id"]:
            raise SystemExit(f"pack id was suffixed: {pack['pack_id']}")
        if pack["visibility"] != "private":
            set_visibility(sql, _principal(pack["owner"]), pack["pack_id"], pack["visibility"])


def _open_stores(directory: Path) -> tuple[Any, Any, Any]:
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sql_store import SQLStore
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    sql = SQLStore(f"sqlite:///{directory / 'sql.db'}")
    docs = LocalSQLDocStore(str(directory / "doc.db"))
    vec = SqliteVecStore(str(directory / "vectors.db"), embedding_function=HashEmbedding(),
                         dim=EMBED_DIM, collection_name=VECTOR_TABLE)
    if not (docs.available and vec.available and docs.supports_keyword):
        raise SystemExit("isolated stores are not available")
    return sql, docs, vec


def _close(*stores: Any) -> None:
    for store in stores:
        close = getattr(store, "close", None)
        if close is not None:
            close()
        engine = getattr(store, "_engine", None)
        if engine is not None:
            engine.dispose()


def write_doc(docs: Any, vec: Any, rows: dict[str, Any]) -> None:
    space, node_type, node_id, props = rows["node"]
    docs.upsert_node_doc(space, node_type, node_id, props)
    if rows["source"] is not None:
        docs.upsert_source(*rows["source"])
    if rows["vector"] is not None:
        vid, vtext, vmeta = rows["vector"]
        vec.upsert_texts(texts=[vtext], metadatas=[vmeta], ids=[vid])


def locked_write(run_dir: Path, docs: Any, vec: Any, hq: Any, rows: dict[str, Any]) -> float:
    """Write one document and invalidate BM25 inside one write.lock block.

    The production ingest tools run the store writes and the invalidation
    inside the same write.lock (design 8.3). The function returns the lock
    wait in seconds.
    """
    from opencrab.locking import write_lock

    started = time.perf_counter()
    with write_lock(str(run_dir), timeout=30.0, purpose="search-baseline"):
        acquired = time.perf_counter()
        write_doc(docs, vec, rows)
        hq.invalidate_bm25_cache()
    return acquired - started


def locked_delete(run_dir: Path, docs: Any, vec: Any, hq: Any, node_id: str) -> None:
    """Delete one source-form document and invalidate BM25 inside one write.lock block."""
    from opencrab.locking import write_lock

    with write_lock(str(run_dir), timeout=30.0, purpose="search-baseline"):
        docs.delete_node_doc("evidence", node_id)
        docs.delete_sources_with_state([node_id])
        vec.delete([node_id])
        hq.invalidate_bm25_cache()


def cmd_load(corpus: Path, work: Path) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from search_corpus import TIMING_FILE, manifest_digest

    smoke = corpus_is_smoke(corpus)
    check_write_paths([work], sources=[corpus], smoke=smoke)
    if work.exists():
        raise SystemExit(f"load directory already exists: {work}")
    timing = corpus / TIMING_FILE
    if not timing.exists():
        raise SystemExit(f"{TIMING_FILE} missing: generate the corpus with search_corpus.py first")
    spent = float(json.loads(timing.read_text(encoding="utf-8"))["generate_seconds"])
    manifest, packs, _queries = _read_corpus(corpus)
    work.mkdir(parents=True)
    watchdog = Watchdog(manifest["preset"], "load", work / "aborted.json", [work],
                        spent_seconds=spent, smoke=smoke)
    watchdog.start()
    digest = manifest_digest(corpus)
    started = time.monotonic()
    sql, docs, vec = _open_stores(work)
    _create_users(sql)
    _register_packs(sql, packs)
    batch: list[tuple[str, str, dict[str, Any]]] = []
    stamps: list[tuple[str, str, str]] = []
    for doc in _iter_docs(corpus):
        rows = doc_rows(doc, packs)
        space, node_type, node_id, props = rows["node"]
        docs.upsert_node_doc(space, node_type, node_id, props)
        if rows["source"] is not None:
            docs.upsert_source(*rows["source"])
        if rows["vector"] is not None:
            batch.append(rows["vector"])
        stamps.append((doc["updated_at"], space, node_id))
        if len(batch) >= 64:
            vec.upsert_texts(texts=[b[1] for b in batch], metadatas=[b[2] for b in batch],
                             ids=[b[0] for b in batch])
            batch = []
    if batch:
        vec.upsert_texts(texts=[b[1] for b in batch], metadatas=[b[2] for b in batch],
                         ids=[b[0] for b in batch])
    with docs._tx() as conn:
        conn.executemany("UPDATE doc_nodes SET updated_at=? WHERE space=? AND node_id=?", stamps)
    load_seconds = time.monotonic() - started
    _close(docs, vec, sql)
    expected = expected_state(corpus)
    observed, problems = observed_state(work)
    diff = _state_diff(expected, observed)
    if diff or problems:
        print(json.dumps({"load": "MISMATCH", "tables": diff, "problems": problems}))
        return 2
    sizes = {p.name: p.stat().st_size for p in sorted(work.iterdir()) if p.is_file()}
    _atomic_json(work / "load-complete.json", {
        "manifest_digest": digest, "preset": manifest["preset"], "seed": manifest["seed"],
        "expected": expected, "load_seconds": round(load_seconds, 3), "file_bytes": sizes,
        "peak_rss_bytes": watchdog.peak_rss,
    })
    print(json.dumps({"load": "COMPLETE", "load_seconds": round(load_seconds, 3),
                      "rows": {k: v["rows"] for k, v in expected.items()}}))
    return 0


# ---------------------------------------------------------------------------
# Judgment (design section 7). Pure functions: no store, no clock.
# ---------------------------------------------------------------------------


def _coverage(warnings: list[str]) -> list[str]:
    return [w for w in warnings if isinstance(w, str) and w.startswith(COVERAGE_PREFIX)]


def judge_bundle(entry: dict[str, Any], truth: dict[str, tuple[str, str]], readable: set[str],
                 selection: dict[str, Any], legs: dict[str, dict[str, Any]],
                 combined: dict[str, Any]) -> dict[str, Any]:
    """Classify every leg and the combined result of one query bundle.

    ``legs[name]`` is ``{"ids": [...], "warnings": [...]}`` with the leg's whole
    returned list. ``combined`` is ``{"items": [{"id", "members"}], "warnings"}``.
    ``truth`` maps every known node id to ``(pack_id, space)``.
    """
    limit = entry["limit"]
    effective = set(selection["effective"])
    spaces = set(entry["spaces"] or [])
    forbidden = set(entry["forbidden"])
    decoys = set(entry["decoys"])
    gold = list(entry["gold"])

    def leak_ids(ids: list[str]) -> list[str]:
        bad = []
        for node_id in ids:
            known = truth.get(node_id)
            if known is None:
                bad.append(node_id)
                continue
            pack_id, space = known
            if (node_id in forbidden or pack_id not in effective or pack_id not in readable
                    or (spaces and space not in spaces)):
                bad.append(node_id)
        return bad

    def decoy_above(ids: list[str]) -> int:
        first_gold = next((i for i, node_id in enumerate(ids) if node_id in gold), None)
        window = ids[:first_gold] if first_gold is not None else ids[:limit]
        return sum(1 for node_id in window if node_id in decoys)

    scope_error = bool(
        (entry["expect_scope_warning"] and OUT_OF_SCOPE not in selection["warnings"])
        or (entry["expect_empty"] and (combined["items"] or any(leg["ids"] for leg in legs.values())))
    )
    bm25_cov = _coverage(legs.get("bm25", {}).get("warnings", []))
    out: dict[str, Any] = {"legs": {}}
    for name, leg in legs.items():
        ids = list(leg["ids"])
        top = ids[:limit]
        must = entry["must_hit"][name]
        missing = [g for g in must if g not in top]
        cov = _coverage(leg.get("warnings", []))
        result = {
            "returned": len(ids),
            "leak": leak_ids(ids),
            "dup": len(ids) - len(set(ids)),
            "missing": missing,
            "contract_miss": [g for g in gold if g not in must and g not in top],
            "gold_rank": {g: ids.index(g) + 1 for g in missing if g in ids},
            "decoy_above_gold": decoy_above(ids),
            "coverage_warnings": cov,
        }
        silent = name == "bm25" and bool(missing) and not cov
        # Independent defect flags. The class below picks one by priority, so
        # a leaking id returned twice is class "leak" with flags["dup"] true.
        result["flags"] = {"leak": bool(result["leak"]), "dup": result["dup"] > 0,
                           "scope_error": scope_error, "warning_dropped": False,
                           "partial_silent": silent}
        if result["leak"]:
            cls = "leak"
        elif result["dup"]:
            cls = "dup"
        elif scope_error:
            cls = "scope_error"
        elif name == "bm25" and missing:
            cls = "partial_flagged" if cov else "partial_silent"
        elif missing:
            cls = "miss"
        else:
            cls = "pass"
        result["class"] = cls
        out["legs"][name] = result

    # Leak and duplicate checks see every returned representative and member.
    # Gold presence counts only the top ``limit`` representatives and their
    # members (design 7.1).
    flat: list[str] = []
    rep_ids: list[str] = []
    top_flat: list[str] = []
    for rank, item in enumerate(combined["items"]):
        rep_ids.append(item["id"])
        group = [item["id"], *item.get("members", [])]
        flat.extend(group)
        if rank < limit:
            top_flat.extend(group)
    must = entry["must_hit"]["combined"]
    missing = [g for g in must if g not in top_flat]
    cov = _coverage(combined.get("warnings", []))
    dropped = sorted(set(bm25_cov) - set(cov))
    result = {
        "returned": len(rep_ids),
        "leak": leak_ids(flat),
        "dup": len(flat) - len(set(flat)),
        "missing": missing,
        "decoy_above_gold": decoy_above(flat),
        "coverage_warnings": cov,
        "warning_dropped": dropped,
    }
    bm25_missed = [g for g in missing if g in set(entry["must_hit"]["bm25"])]
    result["flags"] = {"leak": bool(result["leak"]), "dup": result["dup"] > 0,
                       "scope_error": scope_error, "warning_dropped": bool(dropped),
                       "partial_silent": bool(bm25_missed) and not cov}
    if result["leak"]:
        cls = "leak"
    elif result["dup"]:
        cls = "dup"
    elif scope_error:
        cls = "scope_error"
    elif dropped:
        cls = "warning_dropped"
    elif bm25_missed:
        cls = "partial_flagged" if cov else "partial_silent"
    elif missing:
        cls = "miss"
    else:
        cls = "pass"
    result["class"] = cls
    out["combined"] = result
    out["wrong_pack"] = bool(entry["auto_pack"] and entry["expect_pack"]
                             and list(selection["effective"]) != [entry["expect_pack"]])
    return out


def selftest_cases() -> list[tuple[str, str, str]]:
    """Return (case, expected class, actual class) for planted defects."""
    truth = {
        "g1": ("p1", "evidence"), "g2": ("p1", "concept"), "d1": ("p1", "evidence"),
        "x1": ("p2", "evidence"), "f1": ("p1", "evidence"), "o1": ("p3", "evidence"),
    }
    readable = {"p1", "p2"}

    def entry(**over: Any) -> dict[str, Any]:
        base = {"limit": 3, "spaces": None, "forbidden": ["f1"], "decoys": ["d1"],
                "gold": ["g1"], "auto_pack": False, "expect_pack": None,
                "expect_scope_warning": False, "expect_empty": False,
                "must_hit": {"bm25": ["g1"], "fts": ["g1"], "vector": [], "combined": ["g1"]}}
        base.update(over)
        return base

    def legs(bm25: list[str] | None = None, bm25_warn: list[str] | None = None,
             fts: list[str] | None = None, vector: list[str] | None = None) -> dict[str, Any]:
        return {"bm25": {"ids": ["g1"] if bm25 is None else bm25, "warnings": bm25_warn or []},
                "fts": {"ids": ["g1"] if fts is None else fts, "warnings": []},
                "vector": {"ids": ["d1"] if vector is None else vector, "warnings": []}}

    def comb(items: list[dict[str, Any]] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
        return {"items": [{"id": "g1", "members": []}] if items is None else items,
                "warnings": warnings or []}

    sel = {"effective": ["p1"], "warnings": []}
    warn = "BM25 coverage partial rows: indexed rows are below the store total"
    cases: list[tuple[str, str, Any]] = [
        ("clean", "pass", (entry(), sel, legs(), comb(), "combined")),
        ("vector leak past limit", "leak",
         (entry(), sel, legs(vector=["d1", "g1", "d1x", "f1"]), comb(), "vector")),
        ("folded member leak", "leak",
         (entry(), sel, legs(), comb([{"id": "g1", "members": ["x1"]}]), "combined")),
        ("unknown id", "leak", (entry(), sel, legs(fts=["g1", "zz"]), comb(), "fts")),
        ("unreadable pack", "leak",
         (entry(), {"effective": ["p1", "p3"], "warnings": []}, legs(fts=["g1", "o1"]), comb(), "fts")),
        ("space outside filter", "leak",
         (entry(spaces=["concept"], gold=["g2"], must_hit={"bm25": ["g2"], "fts": [], "vector": [],
                                                          "combined": ["g2"]}),
          sel, legs(bm25=["g2", "g1"]), comb([{"id": "g2", "members": []}]), "bm25")),
        ("duplicate", "dup", (entry(), sel, legs(), comb([{"id": "g1", "members": ["g1"]}]), "combined")),
        ("fts miss", "miss", (entry(), sel, legs(fts=["d1", "d1", "d1"][:1]), comb(), "fts")),
        ("bm25 silent partial", "partial_silent", (entry(), sel, legs(bm25=[]), comb(), "bm25")),
        ("bm25 flagged partial", "partial_flagged",
         (entry(), sel, legs(bm25=[], bm25_warn=[warn]), comb(warnings=[warn]), "bm25")),
        ("combined silent partial", "partial_silent",
         (entry(), sel, legs(), comb([{"id": "d1", "members": []}]), "combined")),
        ("warning dropped with all gold found", "warning_dropped",
         (entry(), sel, legs(bm25_warn=[warn]), comb(), "combined")),
        ("scope warning missing", "scope_error",
         (entry(expect_scope_warning=True), sel, legs(), comb(), "combined")),
        # An empty effective scope makes every returned id a leak, and leak
        # comes first in the design 7.2 order.
        ("forbidden-only request answered", "leak",
         (entry(expect_scope_warning=True, expect_empty=True, gold=[],
                must_hit={"bm25": [], "fts": [], "vector": [], "combined": []}),
          {"effective": [], "warnings": [OUT_OF_SCOPE]}, legs(bm25=[], fts=[], vector=[]),
          comb([{"id": "d1", "members": []}]), "combined")),
    ]
    results = []
    for name, expected, (ent, selection, leg_map, combined, target) in cases:
        judged = judge_bundle(ent, truth, readable, selection, leg_map, combined)
        actual = judged["combined"]["class"] if target == "combined" else judged["legs"][target]["class"]
        results.append((name, expected, actual))
    judged = judge_bundle(entry(auto_pack=True, expect_pack="p2"), truth, readable, sel, legs(), comb())
    results.append(("wrong pack", "wrong_pack", "wrong_pack" if judged["wrong_pack"] else "no_flag"))
    # Gold past the top limit does not count as found (design 7.1).
    gold_past_limit = [{"id": "d1", "members": []}, {"id": "g1", "members": []}]
    judged = judge_bundle(entry(limit=1), truth, readable, sel, legs(), comb(gold_past_limit))
    results.append(("combined gold past limit, bm25 must-hit", "partial_silent",
                    judged["combined"]["class"]))
    no_bm25 = entry(limit=1, must_hit={"bm25": [], "fts": ["g1"], "vector": [], "combined": ["g1"]})
    judged = judge_bundle(no_bm25, truth, readable, sel, legs(bm25=[]), comb(gold_past_limit))
    results.append(("combined gold past limit, no bm25 must-hit", "miss", judged["combined"]["class"]))
    # A leaking id returned twice: class leak, and the dup flag stays set.
    judged = judge_bundle(entry(), truth, readable, sel, legs(fts=["g1", "f1", "f1"]), comb())
    fts = judged["legs"]["fts"]
    results.append(("leak returned twice keeps the dup flag", "leak+dup",
                    f"{fts['class']}+{'dup' if fts['flags']['dup'] else 'nodup'}"))
    return results


def cmd_selftest() -> int:
    results = selftest_cases()
    failed = [r for r in results if r[1] != r[2]]
    for name, expected, actual in results:
        print(f"{'ok ' if expected == actual else 'BAD'} {name}: expected={expected} actual={actual}")
    print("SELFTEST: PASS" if not failed else f"SELFTEST: FAIL ({len(failed)})")
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _summary(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "p50_ms": _p(values, 0.5), "p95_ms": _p(values, 0.95),
            "max_ms": max(values) if values else None}


def _environment(config: str, env_values: dict[str, int], module_defaults: dict[str, int]) -> dict[str, Any]:
    limits = ""
    try:
        for line in Path("/proc/self/limits").read_text().splitlines():
            if line.startswith("Max address space"):
                limits = " ".join(line.split())
    except OSError:
        pass
    cpu_max = None
    try:
        cg = Path("/proc/self/cgroup").read_text().strip().split("\n")[0].split(":", 2)[2]
        cpu_max = Path(f"/sys/fs/cgroup{cg}/cpu.max").read_text().strip()
    except (OSError, IndexError):
        pass
    return {
        "python": platform.python_version(), "machine": platform.machine(),
        "cpu_count": os.cpu_count(), "address_space_limit": limits, "cgroup_cpu_max": cpu_max,
        "malloc_arena_max": os.environ.get("MALLOC_ARENA_MAX"), "config": config,
        "bm25_env": env_values, "bm25_module_defaults": module_defaults,
        "bm25_defaults_match": all(module_defaults.get(k) == v for k, v in BM25_DEFAULTS.items()),
        "tmpdir_fs": default_fs_type(Path(os.environ.get("TMPDIR", ""))) if os.environ.get("TMPDIR") else None,
    }


def _copy_db_files(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True)
    for base in ("doc.db", "vectors.db", "sql.db"):
        for suffix in ("", "-wal", "-shm"):
            path = src / (base + suffix)
            if path.exists():
                shutil.copy2(path, dst / path.name)


def _invariants(run_dir: Path, corpus: Path, cap25: int, cap: int, char_budget: int) -> list[str]:
    problems: list[str] = []
    old: set[str] = set()
    recent: set[str] = set()
    total = 0
    for doc in _iter_docs(corpus):
        total += 1
        if doc["old"]:
            old.add(doc["id"])
        if doc["recent"]:
            recent.add(doc["id"])
    conn = _ro(run_dir / "doc.db")
    try:
        stored = conn.execute("SELECT COALESCE(SUM(LENGTH(properties)), 0) FROM doc_nodes").fetchone()[0]
    finally:
        conn.close()
    if stored >= char_budget:
        problems.append("stored properties reach the character budget")

    # Read the windows with the API the BM25 build uses
    # (_Bm25CacheWorker._read_window): the budgeted query, and on failure
    # list_nodes cut by the same budget rule.
    from opencrab.ontology.bm25 import trim_to_budget
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    store = LocalSQLDocStore(str(run_dir / "doc.db"))
    try:
        def window_ids(limit: int) -> set[str]:
            try:
                nodes = store.list_nodes_within_budget(limit=limit, max_chars=char_budget)
            except Exception:  # noqa: BLE001 -- same fallback as _read_window
                nodes = trim_to_budget(store.list_nodes(limit=limit), max_chars=char_budget)
            return {n["node_id"] for n in nodes}

        window25 = window_ids(cap25)
        window = window_ids(cap)
    finally:
        store.close()
    if window25 & old:
        problems.append("old documents inside the scaled-cap window")
    if not recent <= window25:
        problems.append("recent documents outside the scaled-cap window")
    if cap >= total and len(window) != total:
        problems.append("default window does not hold every row")
    return problems


def _items_from_results(results: list[Any]) -> list[dict[str, Any]]:
    return [{"id": r.node_id, "members": [m.node_id for m in (r.folded_members or [])]}
            for r in results]


def cmd_run(args: argparse.Namespace) -> int:  # noqa: C901 -- one linear measurement script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from search_corpus import manifest_digest

    corpus, work = Path(args.corpus).resolve(), Path(args.work).resolve()
    run_dir, out = Path(args.run_dir).resolve(), Path(args.out).resolve()
    smoke = corpus_is_smoke(corpus)
    check_write_paths([run_dir, out], sources=[corpus, work], smoke=smoke)
    if run_dir.exists():
        raise SystemExit(f"run directory already exists: {run_dir}")
    if out.exists():
        raise SystemExit(f"report already exists: {out}")
    manifest, packs, queries = _read_corpus(corpus)
    watchdog = Watchdog(manifest["preset"], "run", out, [run_dir, out.parent], smoke=smoke)
    watchdog.start()

    report: dict[str, Any] = {"verdict": "INCOMPLETE", "reasons": []}

    def finish(code: int) -> int:
        report["peak_rss_bytes_sampled"] = watchdog.peak_rss
        _atomic_json(out, report)
        print(json.dumps({"verdict": report["verdict"], "reasons": report.get("reasons", [])}))
        return code

    node_count = manifest["node_count"]
    cap25 = math.ceil(node_count * SCALED_CAP_FRACTION)
    env_values = dict(BM25_DEFAULTS)
    if args.config == "scaled-cap":
        env_values["OPENCRAB_BM25_NODE_LIMIT"] = cap25
    for key, value in env_values.items():
        os.environ[key] = str(value)

    # Initial state (design 8.2).
    marker = work / "load-complete.json"
    if not marker.exists():
        report["reasons"].append("load-complete.json missing")
        return finish(2)
    complete = json.loads(marker.read_text(encoding="utf-8"))
    try:
        digest = manifest_digest(corpus)
    except ValueError as exc:
        report["reasons"].append(str(exc))
        return finish(2)
    expected = expected_state(corpus)
    if complete.get("manifest_digest") != digest or complete.get("expected") != expected:
        report["reasons"].append("load-complete.json does not match the corpus")
        return finish(2)
    _copy_db_files(work, run_dir)
    observed, problems = observed_state(run_dir)
    diff = _state_diff(expected, observed)
    if diff or problems:
        report["reasons"].append(f"initial state mismatch: tables={diff} problems={problems}")
        return finish(2)

    import opencrab.ontology.query as qmod
    from opencrab.ontology.query import HybridQuery
    from opencrab.pack.read_scope import read_scope
    from opencrab.services.pack_selection import resolve_packs

    module_defaults = {
        "OPENCRAB_BM25_NODE_LIMIT": qmod._BM25_NODE_LIMIT,
        "OPENCRAB_BM25_CHAR_BUDGET": qmod._BM25_CHAR_BUDGET_DEFAULT,
        "OPENCRAB_BM25_MAX_NODE_CHARS": qmod._BM25_MAX_NODE_CHARS_DEFAULT,
    }
    report["environment"] = _environment(args.config, env_values, module_defaults)
    report["environment"]["module_node_limit_effective"] = qmod._BM25_NODE_LIMIT
    if qmod._BM25_NODE_LIMIT != env_values["OPENCRAB_BM25_NODE_LIMIT"]:
        report["reasons"].append("BM25 node limit was not applied before import")
        return finish(2)
    report["corpus"] = {"manifest_digest": digest, "preset": manifest["preset"],
                        "seed": manifest["seed"], "node_count": node_count,
                        "source_count": manifest["source_count"], "query_count": len(queries)}
    inv = _invariants(run_dir, corpus, cap25, env_values["OPENCRAB_BM25_NODE_LIMIT"],
                      env_values["OPENCRAB_BM25_CHAR_BUDGET"])
    if inv:
        report["reasons"].extend(inv)
        return finish(2)
    if not queries or queries[0]["shape"] != "explicit_single":
        report["reasons"].append("first planned query is not an explicit single-pack request")
        return finish(2)

    sql, docs, vec = _open_stores(run_dir)
    principal_a = _principal("user_a")
    scope = read_scope(sql, principal_a)
    hq = HybridQuery(vec, NoGraph())
    hq._doc_store = docs
    build_times: list[float] = []
    original_ensure = hq._bm25.ensure_built

    def timed_ensure(doc_store: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_ensure(doc_store)
        finally:
            build_times.append(time.perf_counter() - started)

    hq._bm25.ensure_built = timed_ensure

    truth: dict[str, tuple[str, str]] = {}
    for doc in _iter_docs(corpus):
        pack = packs.get(doc["pack"]) if doc["pack"] else None
        space = doc["space"] if doc["form"] == "N" else "evidence"
        truth[doc["id"]] = (pack["pack_id"] if pack else "", space)
    measure_pack = packs["measure"]["pack_id"]

    def request(q: dict[str, Any]) -> tuple[Any, Any, float]:
        watchdog.op_start(f"request:{q['qid']}")
        started = time.perf_counter()
        selection = resolve_packs(
            q["text"], q["pack_ids"], q["auto_pack"], False, str(run_dir), scope=scope,
            raise_on_error=False, sql=sql, principal=principal_a, hybrid=hq, spaces=q["spaces"])
        outcome = hq.query(q["text"], pack_ids=selection.effective_pack_ids, full_scope=scope,
                           spaces=q["spaces"], limit=q["limit"])
        elapsed = (time.perf_counter() - started) * 1000.0
        watchdog.op_end()
        return selection, outcome, elapsed

    def leg_calls(q: dict[str, Any], effective: list[str]) -> tuple[dict[str, Any], dict[str, float]]:
        profile = qmod._profile_for_query(q["text"], q["limit"], 1)
        timings: dict[str, float] = {}
        legs: dict[str, Any] = {}
        watchdog.op_start(f"legs:{q['qid']}")
        t = time.perf_counter()
        hits, warns = hq._bm25_search_with_warnings(q["text"], q["spaces"], profile.bm25_limit,
                                                    pack_ids=effective)
        timings["bm25"] = (time.perf_counter() - t) * 1000.0
        legs["bm25"] = {"ids": [h.get("node_id") for h in hits], "warnings": list(warns)}
        t = time.perf_counter()
        hits = hq._fts_search(q["text"], q["spaces"], profile.bm25_limit, pack_ids=effective)
        timings["fts"] = (time.perf_counter() - t) * 1000.0
        legs["fts"] = {"ids": [h.get("node_id") for h in hits], "warnings": []}
        t = time.perf_counter()
        results = hq._vector_search(q["text"], q["spaces"], profile.vector_limit, pack_ids=effective)
        timings["vector"] = (time.perf_counter() - t) * 1000.0
        legs["vector"] = {"ids": [r.node_id for r in results], "warnings": []}
        watchdog.op_end()
        return legs, timings

    def generation() -> int | None:
        state = hq._bm25.state
        return state.generation if state is not None else None

    readable = set(scope)
    phase_cpu: dict[str, float] = {}

    # Cold (design 8.3 step 3).
    cpu0 = time.process_time()
    _sel, _out, cold_ms = request(queries[0])
    report["cold"] = {"first_request_ms": cold_ms,
                      "bm25_first_build_ms": build_times[0] * 1000.0 if build_times else None}
    phase_cpu["cold"] = time.process_time() - cpu0

    # Warm and quality (steps 4 and 5).
    cpu0 = time.process_time()
    shape_lat: dict[str, list[float]] = {}
    leg_lat: dict[str, list[float]] = {"bm25": [], "fts": [], "vector": []}
    per_query: list[dict[str, Any]] = []
    flag_names = ("leak", "dup", "scope_error", "warning_dropped", "partial_silent")
    repeat_defects: dict[str, int] = dict.fromkeys(flag_names, 0)
    repeat_classes: dict[str, dict[str, int]] = {}
    discarded_bundles = 0

    def stable_bundle(q: dict[str, Any]) -> tuple[Any, Any, float, dict[str, Any], dict[str, float]] | None:
        """Build one bundle whose BM25 generation did not change (design 7.1).

        The function discards a bundle whose generation changed, together
        with its latency samples, and builds it again. Three changes in a row
        return None.
        """
        nonlocal discarded_bundles
        for _attempt in range(3):
            g0 = generation()
            sel, outcome, ms = request(q)
            legs, timings = leg_calls(q, sel.effective_pack_ids)
            if generation() == g0:
                return sel, outcome, ms, legs, timings
            discarded_bundles += 1
        return None

    for q in queries:
        if stable_bundle(q) is None:  # warm-up bundle, not recorded
            report["reasons"].append(f"unstable BM25 generation: {q['qid']}")
            continue
        last = None
        for _rep in range(args.repeats):
            built = stable_bundle(q)
            if built is None:
                report["reasons"].append(f"unstable BM25 generation: {q['qid']}")
                break
            sel, outcome, ms, legs, timings = built
            shape_lat.setdefault(q["shape"], []).append(ms)
            for name, value in timings.items():
                leg_lat[name].append(value)
            selection = {"effective": list(sel.effective_pack_ids),
                         "warnings": [w.code for w in sel.warnings]}
            combined = {"items": _items_from_results(outcome.results), "warnings": list(outcome.warnings)}
            judged = judge_bundle(q, truth, readable, selection, legs, combined)
            parts = {**judged["legs"], "combined": judged["combined"]}
            for leg_name, part in parts.items():
                for flag in flag_names:
                    repeat_defects[flag] += bool(part["flags"][flag])
                counts = repeat_classes.setdefault(leg_name, {})
                counts[part["class"]] = counts.get(part["class"], 0) + 1
            last = (judged, selection, list(outcome.warnings))
        if last is None:
            continue
        judged, selection, warnings = last
        per_query.append({"qid": q["qid"], "kind": q["kind"], "shape": q["shape"],
                          "selection": selection, "combined_warnings": warnings,
                          "wrong_pack": judged["wrong_pack"],
                          "legs": {**judged["legs"], "combined": judged["combined"]}})
    phase_cpu["warm"] = time.process_time() - cpu0
    report["queries"] = per_query

    # Freshness (step 6).
    cpu0 = time.process_time()
    fresh_marker1 = "qxfreshone"
    fresh_marker2 = "qxfreshtwo"
    body1 = f"{fresh_marker1} " + " ".join(f"qdfa{i}" for i in range(20))
    body2 = f"{fresh_marker2} " + " ".join(f"qdfb{i}" for i in range(20))
    fresh_id = "fresh-000001"
    truth[fresh_id] = (measure_pack, "evidence")

    def fresh_doc(body: str) -> dict[str, Any]:
        return {"id": fresh_id, "pack": "measure", "form": "S", "space": "evidence",
                "node_type": "TextUnit", "title": "fresh", "text": body}

    profile = qmod._profile_for_query(fresh_marker1, 10, 1)

    def leg_state(leg: str, query_text: str) -> tuple[bool, str | None]:
        watchdog.op_start(f"freshness:{leg}")
        try:
            return _leg_state(leg, query_text)
        finally:
            watchdog.op_end()

    def _leg_state(leg: str, query_text: str) -> tuple[bool, str | None]:
        if leg == "bm25":
            hits, _w = hq._bm25_search_with_warnings(query_text, None, profile.bm25_limit,
                                                     pack_ids=[measure_pack])
            return fresh_id in [h.get("node_id") for h in hits][:10], None
        if leg == "fts":
            hits = hq._fts_search(query_text, None, profile.bm25_limit, pack_ids=[measure_pack])
            return fresh_id in [h.get("node_id") for h in hits][:10], None
        results = hq._vector_search(query_text, None, profile.vector_limit, pack_ids=[measure_pack])
        for r in results[:10]:
            if r.node_id == fresh_id:
                return True, r.text
        return False, None

    def poll(conditions: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        pending = dict(conditions)
        done: dict[str, Any] = {}
        while pending and time.perf_counter() - started < args.fresh_timeout:
            for leg in list(pending):
                if pending[leg]():
                    done[leg] = round(time.perf_counter() - started, 3)
                    del pending[leg]
            if pending:
                time.sleep(0.1)
        for leg in pending:
            done[leg] = "not_converged"
        return done

    def write_fresh(body: str) -> None:
        locked_write(run_dir, docs, vec, hq, doc_rows(fresh_doc(body), packs))

    freshness: dict[str, Any] = {}
    write_fresh(body1)
    freshness["insert"] = poll({
        "bm25": lambda: leg_state("bm25", fresh_marker1)[0],
        "fts": lambda: leg_state("fts", fresh_marker1)[0],
        "vector": lambda: leg_state("vector", body1)[0],
    })
    visible = {leg for leg, v in freshness["insert"].items() if v != "not_converged"}
    write_fresh(body2)

    def updated(leg: str) -> bool:
        if leg == "vector":
            found, text = leg_state("vector", body2)
            return found and text == body2
        return leg_state(leg, fresh_marker2)[0] and not leg_state(leg, fresh_marker1)[0]

    freshness["update"] = poll({leg: (lambda leg=leg: updated(leg)) for leg in sorted(visible)})
    for leg in ("bm25", "fts", "vector"):
        if leg not in visible:
            freshness["update"][leg] = "precondition_failed"
    still = {leg for leg, v in freshness["update"].items() if isinstance(v, float)}
    locked_delete(run_dir, docs, vec, hq, fresh_id)
    freshness["delete"] = poll({
        leg: (lambda leg=leg: not leg_state(leg, body2 if leg == "vector" else fresh_marker2)[0])
        for leg in sorted(still)
    })
    for leg in ("bm25", "fts", "vector"):
        if leg not in still:
            freshness["delete"][leg] = "precondition_failed"
    report["freshness"] = freshness
    phase_cpu["freshness"] = time.process_time() - cpu0

    # Concurrent writes (step 7).
    cpu0 = time.process_time()
    total_writes = int(round(args.concurrent_seconds * args.concurrent_rate))
    conc_ids = [f"conc-{i:06d}" for i in range(total_writes)]
    for node_id in conc_ids:
        truth[node_id] = (measure_pack, "evidence")
    write_ms: list[float] = []
    lock_ms: list[float] = []
    write_errors: list[str] = []
    stop_writer = threading.Event()

    def writer() -> None:
        interval = 1.0 / args.concurrent_rate
        start = time.perf_counter()
        for i, node_id in enumerate(conc_ids):
            due = start + i * interval
            delay = due - time.perf_counter()
            if delay > 0 and stop_writer.wait(delay):
                return
            doc = {"id": node_id, "pack": "measure", "form": "N", "space": "evidence",
                   "node_type": "Evidence", "title": "concurrent", "text": f"qdconc{i} filler text"}
            try:
                watchdog.op_start(f"write:{node_id}", slot="writer")
                t0 = time.perf_counter()
                waited = locked_write(run_dir, docs, vec, hq, doc_rows(doc, packs))
                t2 = time.perf_counter()
                lock_ms.append(waited * 1000.0)
                write_ms.append((t2 - t0) * 1000.0)
            except Exception as exc:  # noqa: BLE001 -- recorded in the report
                write_errors.append(type(exc).__name__)
            finally:
                watchdog.op_end(slot="writer")

    thread = threading.Thread(target=writer, name="search-baseline-writer", daemon=True)
    thread.start()
    conc_lat: dict[str, list[float]] = {}
    conc_violations = {"leak": 0, "dup": 0, "requests": 0}
    while thread.is_alive():
        for q in queries:
            if not thread.is_alive():
                break
            sel, outcome, ms = request(q)
            conc_lat.setdefault(q["shape"], []).append(ms)
            selection = {"effective": list(sel.effective_pack_ids),
                         "warnings": [w.code for w in sel.warnings]}
            empty_legs = {name: {"ids": [], "warnings": []} for name in ("bm25", "fts", "vector")}
            combined = {"items": _items_from_results(outcome.results), "warnings": list(outcome.warnings)}
            judged = judge_bundle(q, truth, readable, selection, empty_legs, combined)
            conc_violations["leak"] += bool(judged["combined"]["flags"]["leak"])
            conc_violations["dup"] += bool(judged["combined"]["flags"]["dup"])
            conc_violations["requests"] += 1
    thread.join()
    report["concurrent"] = {
        "seconds": args.concurrent_seconds, "rate_per_s": args.concurrent_rate,
        "writes_planned": total_writes, "writes_done": len(write_ms), "write_errors": write_errors,
        "write_latency": _summary(write_ms), "lock_wait": _summary(lock_ms),
        "request_latency": {shape: _summary(v) for shape, v in conc_lat.items()},
        "violations": conc_violations,
    }
    phase_cpu["concurrent"] = time.process_time() - cpu0
    hq.shutdown_bm25()

    # Resources (step 8).
    checkpoints: dict[str, Any] = {}
    for name in ("doc.db", "vectors.db"):
        conn = sqlite3.connect(str(run_dir / name))
        try:
            if name == "vectors.db":
                import sqlite_vec

                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            busy, log, done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            checkpoints[name] = {"busy": busy, "log_frames": log, "checkpointed": done}
        finally:
            conn.close()
    files = {p.name: p.stat().st_size for p in sorted(run_dir.iterdir()) if p.is_file()}
    index_bytes = raw_bytes = None
    dbstat: dict[str, int] = {}
    conn = _ro(run_dir / "doc.db")
    try:
        try:
            dbstat = {r[0]: int(r[1]) for r in conn.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
            index_bytes = sum(v for k, v in dbstat.items() if k.startswith("doc_sources_fts"))
        except sqlite3.Error:
            dbstat = {}
        raw_bytes = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM doc_sources").fetchone()[0]
    finally:
        conn.close()
    hwm = _read_kib("/proc/self/status", "VmHWM")
    report["resources"] = {
        "vm_hwm_bytes": hwm, "phase_cpu_s": {k: round(v, 3) for k, v in phase_cpu.items()},
        "files_bytes": files, "checkpoints": checkpoints, "dbstat_bytes": dbstat,
        "fts_index_bytes": index_bytes, "source_text_bytes": raw_bytes,
    }
    _close(docs, vec, sql)

    # Summary and requirements (design section 4).
    classes: dict[str, dict[str, int]] = {}
    for entry in per_query:
        for leg, part in entry["legs"].items():
            classes.setdefault(leg, {})
            classes[leg][part["class"]] = classes[leg].get(part["class"], 0) + 1
    latency = {shape: _summary(v) for shape, v in shape_lat.items()}
    legs_summary = {leg: _summary(v) for leg, v in leg_lat.items()}
    defects = dict(repeat_defects)
    defects["concurrent_leak"] = conc_violations["leak"]
    defects["concurrent_dup"] = conc_violations["dup"]
    report["summary"] = {"classes": classes, "latency": latency, "leg_latency": legs_summary,
                         "repeat_defects": defects, "repeat_classes": repeat_classes,
                         "discarded_bundles": discarded_bundles,
                         "wrong_pack": sum(1 for e in per_query if e["wrong_pack"])}

    def met(value: Any, limit: float) -> bool | None:
        return None if value is None else value <= limit

    req: dict[str, Any] = {}
    non_pass = {leg: sum(n for c, n in v.items() if c != "pass") for leg, v in classes.items()}
    # Independent flag counts over every warm repeat (design 4.2, fix of #542
    # verification round 1). Concurrent requests add leak and dup.
    req["quality_leak"] = {"observed": defects["leak"] + defects["concurrent_leak"], "limit": 0}
    req["quality_dup"] = {"observed": defects["dup"] + defects["concurrent_dup"], "limit": 0}
    req["quality_combined_non_pass"] = {"observed": non_pass.get("combined", 0), "limit": 0}
    req["quality_leg_non_pass"] = {"observed": {k: v for k, v in non_pass.items() if k != "combined"},
                                   "limit": 0}
    req["quality_partial_silent"] = {"observed": defects["partial_silent"], "limit": 0}
    req["quality_scope_error"] = {"observed": defects["scope_error"], "limit": 0}
    req["quality_warning_dropped"] = {"observed": defects["warning_dropped"], "limit": 0}
    req["quality_wrong_pack"] = {"observed": report["summary"]["wrong_pack"], "limit": 0}
    for key in ("quality_leak", "quality_dup", "quality_combined_non_pass", "quality_partial_silent",
                "quality_scope_error", "quality_warning_dropped", "quality_wrong_pack"):
        req[key]["met"] = req[key]["observed"] == 0
    req["quality_leg_non_pass"]["met"] = all(v == 0 for v in req["quality_leg_non_pass"]["observed"].values())
    for shape, limit in REQ_LATENCY_P95_MS.items():
        p95 = latency.get(shape, {}).get("p95_ms")
        req[f"latency_{shape}_p95_ms"] = {"observed": p95, "limit": limit, "met": met(p95, limit)}
        cp95 = report["concurrent"]["request_latency"].get(shape, {}).get("p95_ms")
        climit = limit * REQ_CONCURRENT_FACTOR
        req[f"concurrent_{shape}_p95_ms"] = {"observed": cp95, "limit": climit, "met": met(cp95, climit)}
    for leg, summary in legs_summary.items():
        req[f"leg_{leg}_p95_ms"] = {"observed": summary["p95_ms"], "limit": REQ_LEG_P95_MS,
                                    "met": met(summary["p95_ms"], REQ_LEG_P95_MS)}
    cold_s = cold_ms / 1000.0
    req["cold_first_request_s"] = {"observed": cold_s, "limit": REQ_COLD_S, "met": cold_s <= REQ_COLD_S}
    for change, legs_done in freshness.items():
        for leg, value in legs_done.items():
            limit = REQ_FRESH_S[leg]
            ok = isinstance(value, float) and value <= limit
            req[f"fresh_{change}_{leg}_s"] = {"observed": value, "limit": limit, "met": ok}
    req["peak_rss_bytes"] = {"observed": hwm, "limit": REQ_PEAK_RSS, "met": met(hwm, REQ_PEAK_RSS)}
    if index_bytes is None or not raw_bytes:
        req["fts_index_ratio"] = {"observed": "unmeasured", "limit": REQ_INDEX_RATIO, "met": None}
    else:
        ratio = index_bytes / raw_bytes
        req["fts_index_ratio"] = {"observed": ratio, "limit": REQ_INDEX_RATIO, "met": ratio <= REQ_INDEX_RATIO}
    for name, cp in checkpoints.items():
        wal = files.get(name + "-wal", 0)
        req[f"wal_after_checkpoint_{name}"] = {"observed": {"busy": cp["busy"], "wal_bytes": wal},
                                               "limit": REQ_WAL_BYTES,
                                               "met": cp["busy"] == 0 and wal <= REQ_WAL_BYTES}
    wp95 = report["concurrent"]["write_latency"]["p95_ms"]
    lp95 = report["concurrent"]["lock_wait"]["p95_ms"]
    req["write_p95_ms"] = {"observed": wp95, "limit": REQ_WRITE_P95_MS, "met": met(wp95, REQ_WRITE_P95_MS)}
    req["lock_wait_p95_ms"] = {"observed": lp95, "limit": REQ_LOCK_WAIT_P95_MS,
                               "met": met(lp95, REQ_LOCK_WAIT_P95_MS)}
    report["requirements"] = req
    if write_errors:
        report["reasons"].append("concurrent writes raised errors")
    report["verdict"] = "COMPLETE" if not report["reasons"] else "INCOMPLETE"
    if not report["reasons"]:
        del report["reasons"]
    return finish(0 if report["verdict"] == "COMPLETE" else 2)


# ---------------------------------------------------------------------------
# compare (design 7.4)
# ---------------------------------------------------------------------------

_HARD = {"leak", "dup", "scope_error", "warning_dropped", "partial_silent"}
_LEGS = {"bm25", "fts", "vector", "combined"}


def compare_preconditions(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """Return why two reports cannot be compared (design 7.4). Empty means valid."""
    problems: list[str] = []
    for report, name in ((base, "base"), (head, "head")):
        if report.get("verdict") != "COMPLETE":
            problems.append(f"{name} report is not COMPLETE")
    if problems:
        return problems
    for key in ("manifest_digest", "preset", "seed"):
        if base["corpus"].get(key) != head["corpus"].get(key):
            problems.append(f"corpus {key} differs")
    for key in ("config", "bm25_env"):
        if base["environment"].get(key) != head["environment"].get(key):
            problems.append(f"environment {key} differs")
    base_q = {q["qid"]: q for q in base.get("queries", [])}
    head_q = {q["qid"]: q for q in head.get("queries", [])}
    if set(base_q) != set(head_q):
        problems.append("query id sets differ")
    for qid in sorted(set(base_q) & set(head_q)):
        for report_q, name in ((base_q[qid], "base"), (head_q[qid], "head")):
            if set(report_q["legs"]) != _LEGS:
                problems.append(f"{name} {qid} leg set differs")
    if set(base.get("requirements", {})) != set(head.get("requirements", {})):
        problems.append("requirement key sets differ")
    return problems


def compare_reports(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """Return regressions of head against base. Call compare_preconditions first."""
    regressions: list[str] = []
    for flag, count in head.get("summary", {}).get("repeat_defects", {}).items():
        if count:
            regressions.append(f"head repeat defect {flag}: {count}")
    base_q = {q["qid"]: q for q in base.get("queries", [])}
    for q in head.get("queries", []):
        old = base_q[q["qid"]]
        for leg, part in q["legs"].items():
            if part["class"] in _HARD:
                regressions.append(f"{q['qid']}:{leg}: {part['class']}")
            if old["legs"][leg]["class"] == "pass" and part["class"] != "pass":
                regressions.append(f"{q['qid']}:{leg}: pass -> {part['class']}")
    base_req, head_req = base.get("requirements", {}), head.get("requirements", {})
    for key, value in head_req.items():
        if value.get("met") is False and base_req[key].get("met") is not False:
            regressions.append(f"requirement newly unmet: {key}")
    return regressions


def cmd_compare(base_path: str, head_path: str) -> int:
    base = json.loads(Path(base_path).read_text(encoding="utf-8"))
    head = json.loads(Path(head_path).read_text(encoding="utf-8"))
    problems = compare_preconditions(base, head)
    if problems:
        for line in problems:
            print(f"INVALID {line}")
        print("COMPARE: INVALID")
        return 2
    regressions = compare_reports(base, head)
    for line in regressions:
        print(f"REGRESSION {line}")
    print("COMPARE: CLEAN" if not regressions else f"COMPARE: {len(regressions)} REGRESSIONS")
    return 0 if not regressions else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load")
    load.add_argument("--corpus", required=True)
    load.add_argument("--work", required=True)
    run = sub.add_parser("run")
    run.add_argument("--corpus", required=True)
    run.add_argument("--work", required=True)
    run.add_argument("--run-dir", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--config", choices=("default", "scaled-cap"), default="default")
    run.add_argument("--repeats", type=int, default=5)
    run.add_argument("--fresh-timeout", type=float, default=30.0)
    run.add_argument("--concurrent-seconds", type=float, default=60.0)
    run.add_argument("--concurrent-rate", type=float, default=5.0)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("base")
    cmp_.add_argument("head")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.command == "load":
        return cmd_load(Path(args.corpus).resolve(), Path(args.work).resolve())
    if args.command == "run":
        return cmd_run(args)
    if args.command == "compare":
        return cmd_compare(args.base, args.head)
    return cmd_selftest()


if __name__ == "__main__":
    raise SystemExit(main())
