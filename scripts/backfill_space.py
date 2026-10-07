#!/usr/bin/env python3
"""Backfill a valid ``space`` into existing vector and doc_sources records (#110 part A).

A vector or doc_sources record without a valid space is a backfill candidate.
A spaces-filtered reader treats malformed values differently per backend, so this tool does not claim one common read rule.
Part B made the pack chunk loaders keep a valid space for new writes. This
tool repairs the records that were stored earlier. Only STORAGE_MODE=local
with a sqlite-vec vector file, ``doc_store.db`` and ``graph.db`` is covered.

Terms
  no valid space   the key is missing, null, an empty string, a non-string
                   value, or a container-like string. A container-like string
                   starts with ``[`` or ``{`` after blanks. The old loader path
                   wrote it as the ``str()`` of a list or dict.
  valid space      any other non-empty string. The tool never overwrites it,
                   even when it differs from the graph space (reported only).
  graph space      a valid, not container-like ``properties.space``, else a
                   valid ``space_id`` column. Duplicate graph rows of one
                   ``node_id`` must agree on (space, pack_id) or the id is held.

The container rule is a deliberate narrowing. The read side accepts a
container-like string as a space name, but it never equals a real space name.
The tool replaces it so a spaces filter can find the record by a real space.
A missing or null space never matches a spaces filter. The report counts valid
spaces that are not in the grammar (space_not_in_grammar). The tool keeps them.

Decision for a record without a valid space (one pure function, used by the
dry run, the plan, the per-batch re-read and the reconcile):
  - metadata text that is not a JSON object  hold_bad_meta (NULL and empty
    text read as an empty object; the tool cannot add a key to any other
    non-object, and it never rewrites a value it cannot read as an object)
  - pack_id of a non-string type             hold_pack_invalid
  - vector pack (partition column, NULL read as "") differs from its
    metadata pack_id                       hold_pack_partition_mismatch
  - id in graph_nodes (mapped): ambiguous duplicate rows hold_graph_ambiguous;
    unparseable properties hold_graph_props_bad (never relaxed); no graph space
    hold_graph_space_missing; record pack or graph pack missing
    hold_pack_missing (apply with --allow-pack-missing); packs differ
    hold_pack_disagrees; otherwise apply the graph space (apply_graph)
  - id not in graph_nodes (unmapped):
      doc_sources row                        evidence (apply_source_default)
      vector with a doc_sources row of the same id: the doc row pack must equal
        the vector pack (else hold_doc_pair_pack_differs); the vector takes the
        doc row effective space; a held doc row holds the vector
        (hold_doc_pair_held)
      vector with a valid metadata.source_id evidence (apply_source_default)
      other vectors                          hold_orphan

Usage
  backfill_space.py --dry-run-scratch DIR            dry run (default)
  backfill_space.py --apply --backup-to DIR          write

The dry run changes no database content and takes no lock. Its scratch root
must already exist and must not be ``/tmp``. Each run creates one UUID child
there. Success removes that child and keeps the root. A failure keeps the child
for inspection. SQLite may create or refresh the ``-wal`` and ``-shm`` sidecars
of a WAL database even when it is opened ``mode=ro``.

Apply sequence:
  1. Take ``write.lock`` for the whole run.
  2. Take a backup set with ``backup_data_dir``.
  3. Plan.
  4. Write per batch. Each batch is one transaction per store. The tool
     re-reads the rows inside the transaction. It writes a row only when the
     same decision still holds. ``UPDATE`` touches metadata only, never text, embedding or the FTS
     index.
  5. Reconcile.
``--max-batches N`` stops early. A rerun continues because written rows no
longer qualify. Each run takes its own backup set.

Stop every other writer first. The scheduled conversation reingest and its
graph import scripts, and these manual scripts, must not run in the window:
reconcile_doc_graph_nodes, repair_pgvector_legacy_none_owner, migrate_to_local,
migrate_sqlite_to_pg, migrate_add_binary_quantization,
migrate_chroma_to_sqlite_vec, import_pack_graph_to_neo4j,
build_nemotron_personas_korea_pack, bench_graph_backends. A writer that skips
write.lock shows up only as a reconcile mismatch.

Restore:
  1. Stop every process that opens the store files.
  2. Delete the ``-wal`` and ``-shm`` files of the target.
  3. Copy the backup file over the target main file.
  4. Start the services.
Copying only the main file leaves the old WAL, which re-applies the later
writes.

Exit codes: 0 done, 1 failure or reconcile mismatch, 2 usage or unsupported
mode, 3 write.lock busy. After a failed write the tool prints a report that
comes from a read-only rescan under the same lock, never from memory counters.
An interrupt (KeyboardInterrupt, SystemExit) prints the same report without an
exit value and re-raises, so the process status is the interrupt's own.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from opencrab.common.hashing import file_sha256
from opencrab.grammar.manifest import SPACES as _GRAMMAR

GRAMMAR_SPACES = frozenset(_GRAMMAR)

EVIDENCE = "evidence"
LIST_CAP = 1000

# ---------------------------------------------------------------------------
# Value and graph helpers
# ---------------------------------------------------------------------------


def value_kind(v: Any) -> tuple[str | None, str]:
    """Return (valid space or None, kind). kind names why a value is invalid."""
    if v is None:
        return None, "null"
    if not isinstance(v, str):
        return None, "non_string"
    if not v:
        return None, "empty"
    if v.lstrip()[:1] in ("[", "{"):
        return None, "container"
    return v, "valid"


def parse_meta(text: Any) -> dict[str, Any] | None:
    """Return the metadata dict. SQL NULL and empty text read as {}. Any other non-object gives None."""
    if text is None or text == "":
        return {}
    return parse_strict_object(text)


def parse_strict_object(text: Any) -> dict[str, Any] | None:
    """Return the read-side JSON object, or None when the text is corrupt."""
    if not isinstance(text, (str, bytes)):
        return None
    from opencrab.common.graph_identity import GraphPropertyValidationError, parse_properties_object

    try:
        return parse_properties_object(text)
    except (GraphPropertyValidationError, RecursionError):
        return None


def parse_graph_properties(text: Any) -> dict[str, Any] | None:
    """Return the graph properties by the read side's corruption rule, or None when corrupt.

    The read side rejects duplicate keys, NaN and Infinity, empty text and any
    non-object. This reuses its parser so both agree.
    """
    return parse_strict_object(text)


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


#: Marks a pack_id of a type the read side keeps as is (number, bool, list, object).
INVALID_PACK = object()


def pack_of(meta: dict[str, Any] | None) -> Any:
    """Return the pack string of a record ("" when absent or null). A non-string value gives INVALID_PACK."""
    if meta is None:
        return INVALID_PACK
    v = meta.get("pack_id")
    if v is None:
        return ""
    return v if isinstance(v, str) else INVALID_PACK


def build_graph_map(gconn: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """node_id -> {"ambiguous", "props_bad", "space", "pack"} and graph stats."""
    stats = collections.Counter()
    seen: dict[str, tuple[Any, ...]] = {}
    out: dict[str, dict[str, Any]] = {}
    for node_id, space_id, props_text in gconn.execute(
        "SELECT node_id, space_id, properties FROM graph_nodes"
    ):
        props = parse_graph_properties(props_text)
        if props is None:
            entry = {"props_bad": True, "space": None, "pack": None}
            stats["graph_props_bad"] += 1
        else:
            p_space, p_kind = value_kind(props.get("space"))
            c_space, _ = value_kind(space_id)
            if p_kind == "container":
                stats["graph_space_container_like"] += 1
            if p_space is not None and c_space is not None and p_space != c_space:
                stats["graph_column_differs_props"] += 1
            entry = {
                "props_bad": False,
                "space": p_space if p_space is not None else c_space,
                "pack": _str_or_none(props.get("pack_id")),
            }
        key = (entry["props_bad"], entry["space"], entry["pack"])
        if node_id in out:
            if seen[node_id] != key:
                out[node_id]["ambiguous"] = True
        else:
            entry["ambiguous"] = False
            out[node_id] = entry
            seen[node_id] = key
    return out, dict(stats)


# ---------------------------------------------------------------------------
# The decision function
# ---------------------------------------------------------------------------


def _mapped_decision(
    g: dict[str, Any], pack: str, allow_pack_missing: bool
) -> tuple[str, str | None]:
    if g["ambiguous"]:
        return "hold_graph_ambiguous", None
    if g["props_bad"]:
        return "hold_graph_props_bad", None
    if g["space"] is None:
        return "hold_graph_space_missing", None
    if not pack or g["pack"] is None:
        if not allow_pack_missing:
            return "hold_pack_missing", None
    elif pack != g["pack"]:
        return "hold_pack_disagrees", None
    return "apply_graph", g["space"]


def decide(
    store: str,
    rid: str,
    meta: dict[str, Any] | None,
    partition_pack: str | None,
    graph: dict[str, dict[str, Any]],
    docs: dict[str, tuple[str, str | None]],
    allow_pack_missing: bool = False,
) -> tuple[str, str | None]:
    """Return (class, space). class "skip_valid" means the record has a valid space.

    ``docs`` maps a doc_sources id to (pack, effective space or None when the
    doc row is held). It is complete before any vector is decided.
    """
    if meta is None:
        return "hold_bad_meta", None
    valid, _ = value_kind(meta.get("space"))
    if valid is not None:
        return "skip_valid", valid
    own = pack_of(meta)
    if own is INVALID_PACK:
        return "hold_pack_invalid", None
    if store == "doc":
        pack = own
    else:
        pack = partition_pack or ""
        if own != pack:
            return "hold_pack_partition_mismatch", None
    g = graph.get(rid)
    if g is not None:
        return _mapped_decision(g, pack, allow_pack_missing)
    if store == "doc":
        return "apply_source_default", EVIDENCE
    pair = docs.get(rid)
    if pair is not None:
        if pair[0] != pack:
            return "hold_doc_pair_pack_differs", None
        if pair[1] is None:
            return "hold_doc_pair_held", None
        return "apply_source_default", pair[1]
    if _str_or_none(meta.get("source_id")) is not None:
        return "apply_source_default", EVIDENCE
    return "hold_orphan", None


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------


def _uri(path: Path, mode: str) -> str:
    return path.resolve().as_uri() + f"?mode={mode}"


def _open(path: Path, mode: str, vec: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(_uri(path, mode), uri=True, timeout=30, isolation_level=None)
    if vec:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


class Paths:
    def __init__(self, data_dir: Path, vector_file: str, collection: str) -> None:
        self.data_dir = data_dir
        self.vector = (
            (data_dir / vector_file) if not os.path.isabs(vector_file) else Path(vector_file)
        )
        self.collection = collection
        self.doc = data_dir / "doc_store.db"
        self.graph = data_dir / "graph.db"

    def missing(self) -> list[str]:
        return [str(p) for p in (self.vector, self.doc, self.graph) if not p.is_file()]


def scan(paths: Paths, allow_pack_missing: bool, mode: str = "ro") -> dict[str, Any]:
    """Classify every record. Returns counters, plan lists and doc info."""
    gconn = _open(paths.graph, mode, False)
    dconn = _open(paths.doc, mode, False)
    vconn = _open(paths.vector, mode, True)
    try:
        graph, gstats = build_graph_map(gconn)
        res: dict[str, Any] = {
            "graph_stats": gstats,
            "total": {"doc": 0, "vector": 0},
            "classes": {"doc": collections.Counter(), "vector": collections.Counter()},
            "invalid_kinds": {"doc": collections.Counter(), "vector": collections.Counter()},
            "distribution": {"doc": collections.Counter(), "vector": collections.Counter()},
            "plan": {"doc": [], "vector": []},
            "held_ids": {
                "doc": collections.defaultdict(list),
                "vector": collections.defaultdict(list),
            },
            "held_map": {"doc": {}, "vector": {}},
            "space_differs_from_graph": {"doc": 0, "vector": 0},
            "corroboration": collections.Counter(),
            "space_not_in_grammar": {"doc": collections.Counter(), "vector": collections.Counter()},
            "vector_vs_doc_space_differs": 0,
        }
        docs: dict[str, tuple[str, str | None]] = {}

        def note(
            store: str, rid: str, cls: str, space: str | None, meta: Any, part: str | None
        ) -> None:
            res["classes"][store][cls] += 1
            if cls == "skip_valid":
                if space not in GRAMMAR_SPACES:
                    res["space_not_in_grammar"][store][space] += 1
                g = graph.get(rid)
                if g is not None and g["space"] is not None and g["space"] != space:
                    res["space_differs_from_graph"][store] += 1
                return
            if isinstance(meta, dict):
                res["invalid_kinds"][store][
                    value_kind(meta.get("space"))[1] if "space" in meta else "missing"
                ] += 1
            if cls.startswith("apply"):
                res["distribution"][store][space] += 1
                res["plan"][store].append((rid, part, space, cls))
            else:
                res["held_map"][store][rid] = cls
                lst = res["held_ids"][store][cls]
                if len(lst) < LIST_CAP:
                    lst.append(rid)

        for rid, text in dconn.execute("SELECT source_id, metadata FROM doc_sources"):
            res["total"]["doc"] += 1
            meta = parse_meta(text)
            cls, space = decide("doc", rid, meta, None, graph, docs, allow_pack_missing)
            pack = pack_of(meta)
            docs[rid] = (pack, space if (cls == "skip_valid" or cls.startswith("apply")) else None)
            note("doc", rid, cls, space, meta, None)
        for rid, part, text in vconn.execute(
            f"SELECT node_id, pack_id, metadata FROM {paths.collection}"
        ):  # noqa: S608
            res["total"]["vector"] += 1
            meta = parse_meta(text)
            cls, space = decide("vector", rid, meta, part, graph, docs, allow_pack_missing)
            note("vector", rid, cls, space, meta, part)
            if rid in graph and isinstance(meta, dict):
                if cls == "apply_graph" or cls == "skip_valid":
                    corro = rid in docs or meta.get("node_id") == rid
                    res["corroboration"]["corroborated" if corro else "pk_only"] += 1
                if cls == "apply_graph" and rid in docs and docs[rid][1] not in (None, space):
                    res["vector_vs_doc_space_differs"] += 1
        res["docs"] = docs
        res["graph"] = graph
        return res
    finally:
        gconn.close()
        dconn.close()
        vconn.close()


def _digest_value(digest: Any, value: Any) -> None:
    """Add one typed SQLite value to a stream digest."""
    if value is None:
        digest.update(b"N")
        return
    if isinstance(value, bytes):
        tag, raw = b"B", value
    elif isinstance(value, str):
        tag, raw = b"S", value.encode("utf-8")
    elif isinstance(value, int):
        tag, raw = b"I", str(value).encode("ascii")
    elif isinstance(value, float):
        tag, raw = b"F", value.hex().encode("ascii")
    else:
        raise TypeError(f"unsupported SQLite value: {type(value).__name__}")
    digest.update(tag + len(raw).to_bytes(8, "big") + raw)


def _digest_rows(cursor: Any) -> str:
    digest = hashlib.sha256()
    for row in cursor:
        for value in row:
            _digest_value(digest, value)
    return digest.hexdigest()


def _record_db(run: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(run / "report.db")
    try:
        conn.executescript("""
            CREATE TABLE graph (id TEXT PRIMARY KEY, props_bad INTEGER NOT NULL, space TEXT,
                                pack TEXT, ambiguous INTEGER NOT NULL);
            CREATE TABLE docs (id TEXT PRIMARY KEY, pack TEXT NOT NULL,
                               invalid_pack INTEGER NOT NULL, space TEXT);
            CREATE TABLE counts (store TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                                 value INTEGER NOT NULL, PRIMARY KEY(store, kind, key));
            CREATE TABLE held (store TEXT NOT NULL, class TEXT NOT NULL, id TEXT NOT NULL,
                               PRIMARY KEY(store, class, id));
        """)
    except BaseException:
        conn.close()
        raise
    return conn


def _inc(report: sqlite3.Connection, store: str, kind: str, key: str, value: int = 1) -> None:
    report.execute(
        """
        INSERT INTO counts(store, kind, key, value) VALUES (?, ?, ?, ?)
        ON CONFLICT(store, kind, key) DO UPDATE SET value=value+excluded.value
    """,
        (store, kind, key, value),
    )


def _graph_get(report: sqlite3.Connection, rid: str) -> dict[str, Any] | None:
    row = report.execute(
        "SELECT props_bad, space, pack, ambiguous FROM graph WHERE id=?", (rid,)
    ).fetchone()
    if row is None:
        return None
    return {"props_bad": bool(row[0]), "space": row[1], "pack": row[2], "ambiguous": bool(row[3])}


def _doc_get(report: sqlite3.Connection, rid: str) -> tuple[Any, str | None] | None:
    row = report.execute("SELECT pack, invalid_pack, space FROM docs WHERE id=?", (rid,)).fetchone()
    if row is None:
        return None
    return (INVALID_PACK if row[1] else row[0], row[2])


def _decide_report(
    store: str,
    rid: str,
    meta: dict[str, Any] | None,
    part: str | None,
    report: sqlite3.Connection,
    allow: bool,
) -> tuple[str, str | None]:
    """Use the shared decision rules with one-record disk-backed maps."""
    graph = _graph_get(report, rid)
    docs = (
        {}
        if store == "doc"
        else ({rid: _doc_get(report, rid)} if _doc_get(report, rid) is not None else {})
    )
    return decide(store, rid, meta, part, {} if graph is None else {rid: graph}, docs, allow)


def _note_report(
    report: sqlite3.Connection,
    store: str,
    rid: str,
    cls: str,
    space: str | None,
    meta: dict[str, Any] | None,
    graph: dict[str, Any] | None,
) -> None:
    _inc(report, store, "total", "all")
    _inc(report, store, "class", cls)
    if cls == "skip_valid":
        if space not in GRAMMAR_SPACES:
            _inc(report, store, "non_grammar", space or "")
        if graph is not None and graph["space"] is not None and graph["space"] != space:
            _inc(report, store, "graph_diff", "all")
        return
    if isinstance(meta, dict):
        kind = value_kind(meta.get("space"))[1] if "space" in meta else "missing"
        _inc(report, store, "invalid", kind)
    if cls.startswith("apply"):
        _inc(report, store, "distribution", space or "")
    else:
        report.execute("INSERT OR IGNORE INTO held VALUES (?, ?, ?)", (store, cls, rid))


def _logical_snapshot(paths: Paths) -> dict[str, str]:
    gconn, dconn, vconn = (
        _open(paths.graph, "ro", False),
        _open(paths.doc, "ro", False),
        _open(paths.vector, "ro", True),
    )
    try:
        vector = hashlib.sha256()
        for _rowid, node_id, _chunk_id, _chunk_offset in vconn.execute(
            f"SELECT rowid, id, chunk_id, chunk_offset FROM {paths.collection}_rowids"
        ):  # noqa: S608
            row = vconn.execute(
                f"SELECT node_id, pack_id, embedding, document, metadata FROM {paths.collection} WHERE node_id=?",  # noqa: S608
                (node_id,),
            ).fetchone()
            if row is None or row[0] != node_id:
                raise RuntimeError("vector shadow id has no matching public row")
            for value in row:
                _digest_value(vector, value)
        return {
            "graph_file": file_sha256(paths.graph),
            "doc_file": file_sha256(paths.doc),
            "vector_file": file_sha256(paths.vector),
            "graph": _digest_rows(
                gconn.execute("SELECT node_type, node_id, space_id, properties FROM graph_nodes")
            ),
            "doc": _digest_rows(
                dconn.execute("SELECT source_id, text, metadata, ingested_at FROM doc_sources")
            ),
            "fts": _digest_rows(
                dconn.execute("SELECT rowid, source_id, text FROM doc_sources_fts ORDER BY rowid")
            ),
            "vector": vector.hexdigest(),
        }
    finally:
        gconn.close()
        dconn.close()
        vconn.close()


def _write_map(out: Any, cursor: Any) -> None:
    """Write a JSON object directly from an ordered cursor."""
    out.write("{")
    first = True
    for key, value in cursor:
        if not first:
            out.write(",")
        json.dump(key, out, ensure_ascii=False)
        out.write(":")
        json.dump(value, out)
        first = False
    out.write("}")


def _write_held(out: Any, report: sqlite3.Connection, store: str) -> None:
    out.write("{")
    current: str | None = None
    count = 0
    first_class = True
    for cls, rid in report.execute(
        "SELECT class, id FROM held WHERE store=? ORDER BY class, id", (store,)
    ):
        if cls != current:
            if current is not None:
                out.write("]")
            if not first_class:
                out.write(",")
            json.dump(cls, out, ensure_ascii=False)
            out.write(":[")
            current, count, first_class = cls, 0, False
        if count < LIST_CAP:
            if count:
                out.write(",")
            json.dump(rid, out, ensure_ascii=False)
            count += 1
    if current is not None:
        out.write("]")
    out.write("}")


def _write_report_json(report: sqlite3.Connection, list_ids: bool, out: Any) -> None:
    """Serialize report rows without an unbounded Python map."""
    out.write('{"mode":"dry-run","total":{')
    for index, store in enumerate(("doc", "vector")):
        if index:
            out.write(",")
        json.dump(store, out)
        out.write(":")
        value = report.execute(
            "SELECT value FROM counts WHERE store=? AND kind='total' AND key='all'", (store,)
        ).fetchone()
        out.write(str(value[0] if value else 0))
    out.write('},"graph":')
    _write_map(
        out,
        report.execute(
            "SELECT key, value FROM counts WHERE store='graph' AND kind='stat' ORDER BY key"
        ),
    )
    out.write(',"space_differs_from_graph":{')
    for index, store in enumerate(("doc", "vector")):
        if index:
            out.write(",")
        json.dump(store, out)
        out.write(":")
        value = report.execute(
            "SELECT value FROM counts WHERE store=? AND kind='graph_diff' AND key='all'", (store,)
        ).fetchone()
        out.write(str(value[0] if value else 0))
    out.write('},"vector_corroboration":')
    _write_map(
        out,
        report.execute(
            "SELECT key, value FROM counts WHERE store='vector' AND kind='corroboration' ORDER BY key"
        ),
    )
    out.write(',"vector_vs_doc_space_differs":')
    value = report.execute(
        "SELECT value FROM counts WHERE store='vector' AND kind='vector_doc_diff' AND key='all'"
    ).fetchone()
    out.write(str(value[0] if value else 0))
    for store in ("doc", "vector"):
        out.write(",")
        json.dump(store, out)
        out.write(':{"no_valid_space":')
        value = report.execute(
            "SELECT COALESCE(SUM(value), 0) FROM counts WHERE store=? AND kind='class' AND key != 'skip_valid'",
            (store,),
        ).fetchone()[0]
        out.write(str(value))
        for field, kind in (
            ("classes", "class"),
            ("invalid_kinds", "invalid"),
            ("planned_space_distribution", "distribution"),
            ("space_not_in_grammar", "non_grammar"),
        ):
            out.write(",")
            json.dump(field, out)
            out.write(":")
            _write_map(
                out,
                report.execute(
                    "SELECT key, value FROM counts WHERE store=? AND kind=? ORDER BY key",
                    (store, kind),
                ),
            )
        if list_ids:
            out.write(',"held_ids":')
            _write_held(out, report, store)
        out.write("}")
    out.write("}")


def _run_dry_run(paths: Paths, args: argparse.Namespace) -> int:
    """Write the report and remove its scratch child after stdout flushes."""
    root = args.dry_run_scratch
    if root is None or not root.is_dir() or root.resolve() == Path("/tmp"):
        print(
            "dry run failed: --dry-run-scratch must name an existing non-/tmp directory",
            file=sys.stderr,
        )
        return 1
    run = root / f"backfill-space-{uuid.uuid4()}"
    report: sqlite3.Connection | None = None
    try:
        run.mkdir(mode=0o700)
        report = _record_db(run)
        before = _logical_snapshot(paths)
        gconn, dconn, vconn = (
            _open(paths.graph, "ro", False),
            _open(paths.doc, "ro", False),
            _open(paths.vector, "ro", True),
        )
        try:
            for rid, space_id, props_text in gconn.execute(
                "SELECT node_id, space_id, properties FROM graph_nodes"
            ):
                if not isinstance(rid, str):
                    raise ValueError("unsupported graph identifier")
                props = parse_graph_properties(props_text)
                if props is None:
                    entry = (1, None, None)
                    _inc(report, "graph", "stat", "graph_props_bad")
                else:
                    pspace, pkind = value_kind(props.get("space"))
                    cspace, _ = value_kind(space_id)
                    if pkind == "container":
                        _inc(report, "graph", "stat", "graph_space_container_like")
                    if pspace is not None and cspace is not None and pspace != cspace:
                        _inc(report, "graph", "stat", "graph_column_differs_props")
                    entry = (
                        0,
                        pspace if pspace is not None else cspace,
                        _str_or_none(props.get("pack_id")),
                    )
                prior = report.execute(
                    "SELECT props_bad, space, pack FROM graph WHERE id=?", (rid,)
                ).fetchone()
                if prior is None:
                    report.execute("INSERT INTO graph VALUES (?, ?, ?, ?, 0)", (rid, *entry))
                elif prior != entry:
                    report.execute("UPDATE graph SET ambiguous=1 WHERE id=?", (rid,))
            for rid, text in dconn.execute("SELECT source_id, metadata FROM doc_sources"):
                if not isinstance(rid, str):
                    raise ValueError("unsupported document identifier")
                meta = parse_meta(text)
                graph = _graph_get(report, rid)
                cls, space = _decide_report("doc", rid, meta, None, report, args.allow_pack_missing)
                pack = pack_of(meta)
                stored_pack = "" if pack is INVALID_PACK else pack
                report.execute(
                    "INSERT INTO docs VALUES (?, ?, ?, ?)",
                    (
                        rid,
                        stored_pack,
                        int(pack is INVALID_PACK),
                        space if cls == "skip_valid" or cls.startswith("apply") else None,
                    ),
                )
                _note_report(report, "doc", rid, cls, space, meta, graph)
            for _rowid, rid, _chunk_id, _chunk_offset in vconn.execute(
                f"SELECT rowid, id, chunk_id, chunk_offset FROM {paths.collection}_rowids"
            ):  # noqa: S608
                row = vconn.execute(
                    f"SELECT node_id, pack_id, metadata FROM {paths.collection} WHERE node_id=?",
                    (rid,),
                ).fetchone()  # noqa: S608
                if row is None or row[0] != rid or not isinstance(rid, str):
                    raise ValueError("unsupported vector identifier")
                _node_id, part, text = row
                meta = parse_meta(text)
                graph = _graph_get(report, rid)
                cls, space = _decide_report(
                    "vector", rid, meta, part, report, args.allow_pack_missing
                )
                _note_report(report, "vector", rid, cls, space, meta, graph)
                if graph is not None and isinstance(meta, dict):
                    if cls == "apply_graph" or cls == "skip_valid":
                        corroborated = (
                            _doc_get(report, rid) is not None or meta.get("node_id") == rid
                        )
                        _inc(
                            report,
                            "vector",
                            "corroboration",
                            "corroborated" if corroborated else "pk_only",
                        )
                    pair = _doc_get(report, rid)
                    if cls == "apply_graph" and pair is not None and pair[1] not in (None, space):
                        _inc(report, "vector", "vector_doc_diff", "all")
            report.commit()
        finally:
            gconn.close()
            dconn.close()
            vconn.close()
        _write_report_json(report, args.list_ids, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
        if before != _logical_snapshot(paths):
            raise RuntimeError("dry run changed a main file or logical row")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        if report is not None:
            report.close()
        try:
            print(f"dry run failed: {exc}; dry-run scratch retained at {run}", file=sys.stderr)
        except OSError:
            pass
        return 1
    report.close()
    shutil.rmtree(run)
    return 0


def no_valid(res: dict[str, Any], store: str) -> int:
    return sum(n for c, n in res["classes"][store].items() if c != "skip_valid")


def hold_classes(res: dict[str, Any], store: str) -> dict[str, int]:
    return {c: n for c, n in res["classes"][store].items() if c.startswith("hold")}


def report_of(res: dict[str, Any], list_ids: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "total": res["total"],
        "graph": res["graph_stats"],
        "space_differs_from_graph": res["space_differs_from_graph"],
        "vector_corroboration": dict(res["corroboration"]),
        "vector_vs_doc_space_differs": res["vector_vs_doc_space_differs"],
    }
    for store in ("doc", "vector"):
        out[store] = {
            "no_valid_space": no_valid(res, store),
            "classes": dict(res["classes"][store]),
            "invalid_kinds": dict(res["invalid_kinds"][store]),
            "planned_space_distribution": dict(res["distribution"][store]),
            "space_not_in_grammar": dict(res["space_not_in_grammar"][store]),
        }
        if list_ids:
            out[store]["held_ids"] = {k: v for k, v in res["held_ids"][store].items()}
    return out


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _new_meta_text(meta: dict[str, Any], space: str) -> str:
    out = dict(meta)
    out["space"] = space
    from opencrab.stores._json import dump_props

    return dump_props(out)


def _write_batches(
    paths: Paths,
    plan: dict[str, list[tuple]],
    res: dict[str, Any],
    batch_size: int,
    max_batches: int | None,
    allow_pack_missing: bool,
    before_commit: Any,
) -> dict[str, Any]:
    stats = {
        "committed": {"doc": [], "vector": []},
        "skipped_changed": {"doc": [], "vector": []},
        "skipped_unserializable": {"doc": [], "vector": []},
        "batches": 0,
        "remaining": {"doc": 0, "vector": 0},
    }
    dconn = _open(paths.doc, "rw", False)
    vconn = _open(paths.vector, "rw", True)
    try:
        for store, conn in (("doc", dconn), ("vector", vconn)):
            items = plan[store]
            for start in range(0, len(items), batch_size):
                if max_batches is not None and stats["batches"] >= max_batches:
                    stats["remaining"][store] = len(items) - start
                    break
                batch = items[start : start + batch_size]
                conn.execute("BEGIN IMMEDIATE")
                try:
                    done = _write_one_batch(
                        conn, store, batch, paths, res, allow_pack_missing, stats
                    )
                    if before_commit is not None:
                        before_commit(store, stats["batches"])
                    conn.execute("COMMIT")
                except BaseException as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except BaseException as rb:
                        exc.rollback_error = f"{type(rb).__name__}: {rb}"
                    raise
                stats["batches"] += 1
                stats["committed"][store].extend(done)
    finally:
        dconn.close()
        vconn.close()
    return stats


def _write_one_batch(conn, store, batch, paths, res, allow_pack_missing, stats):
    ids = [b[0] for b in batch]
    marks = ",".join("?" for _ in ids)
    if store == "doc":
        rows = conn.execute(
            f"SELECT source_id, NULL, metadata FROM doc_sources WHERE source_id IN ({marks})",  # noqa: S608
            ids,
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT node_id, pack_id, metadata FROM {paths.collection} WHERE node_id IN ({marks})",  # noqa: S608
            ids,
        ).fetchall()
    current = {r[0]: r for r in rows}
    done = []
    for rid, part, space, cls in batch:
        row = current.get(rid)
        if row is None:
            stats["skipped_changed"][store].append(rid)
            continue
        meta = parse_meta(row[2])
        now = decide(store, rid, meta, row[1], res["graph"], res["docs"], allow_pack_missing)
        if now != (cls, space) or row[1] != part:
            stats["skipped_changed"][store].append(rid)
            continue
        try:
            text = _new_meta_text(meta, space)
        except (TypeError, ValueError, UnicodeError):
            stats["skipped_unserializable"][store].append(rid)
            continue
        if store == "doc":
            cur = conn.execute(
                "UPDATE doc_sources SET metadata = ? WHERE source_id = ?", (text, rid)
            )
        elif part is None:
            cur = conn.execute(
                f"UPDATE {paths.collection} SET metadata = ? WHERE node_id = ? AND pack_id IS NULL",  # noqa: S608
                (text, rid),
            )
        else:
            cur = conn.execute(
                f"UPDATE {paths.collection} SET metadata = ? WHERE node_id = ? AND pack_id = ?",  # noqa: S608
                (text, rid, part),
            )
        if cur.rowcount != 1:
            # The row was re-read inside BEGIN IMMEDIATE and the key is a primary
            # key, so one row always matches. Fail loudly and roll the batch back.
            raise AssertionError(f"{store} UPDATE changed {cur.rowcount} rows for {rid}")
        done.append((rid, space))
    return done


def reconcile(
    paths: Paths, before: dict[str, Any], stats: dict[str, Any], allow_pack_missing: bool
) -> dict[str, Any]:
    """Rescan and compare with the pre-write scan. Returns {"ok", "problems"}."""
    after = scan(paths, allow_pack_missing)
    problems: list[str] = []
    for store in ("doc", "vector"):
        n = len(stats["committed"][store])
        if after["total"][store] != before["total"][store]:
            problems.append(f"{store}: total rows changed")
        if no_valid(after, store) != no_valid(before, store) - n:
            problems.append(f"{store}: no-valid-space count is not before minus committed")
        if after["held_map"][store] != before["held_map"][store]:
            problems.append(f"{store}: hold class counts changed (a held id changed class)")
    # Every committed id now holds its planned space (full check).
    dconn = _open(paths.doc, "ro", False)
    vconn = _open(paths.vector, "ro", True)
    try:
        for store, conn, sql in (
            ("doc", dconn, "SELECT metadata FROM doc_sources WHERE source_id = ?"),
            ("vector", vconn, f"SELECT metadata FROM {paths.collection} WHERE node_id = ?"),
        ):  # noqa: S608
            for rid, space in stats["committed"][store]:
                row = conn.execute(sql, (rid,)).fetchone()
                meta = parse_meta(row[0]) if row else None
                if not isinstance(meta, dict) or meta.get("space") != space:
                    problems.append(f"{store}: {rid} does not hold {space}")
                    break
    finally:
        dconn.close()
        vconn.close()
    return {"ok": not problems, "problems": problems}


def _failure_report(paths: Paths, args: argparse.Namespace, exc: BaseException) -> dict[str, Any]:
    """Describe a failed write phase from a read-only rescan, never from memory counters."""
    out: dict[str, Any] = {
        "error": f"{type(exc).__name__}: {exc}",
        "error_type": type(exc).__name__,
        "error_text": str(exc),
        "commit_state_unknown": True,
        "note": "The failure may have happened before or after the COMMIT of the open batch. "
        "The rescan below shows the database state under the same lock.",
    }
    if getattr(exc, "rollback_error", None):
        out["rollback_error"] = exc.rollback_error
    try:
        res = scan(paths, args.allow_pack_missing, mode="ro")
        out["rescan"] = {
            "total": res["total"],
            **{
                store: {
                    "valid_space": res["classes"][store].get("skip_valid", 0),
                    "no_valid_space": no_valid(res, store),
                    "classes": {
                        c: n for c, n in res["classes"][store].items() if c != "skip_valid"
                    },
                }
                for store in ("doc", "vector")
            },
        }
    except BaseException as rescan_exc:
        out["rescan_error"] = f"{type(rescan_exc).__name__}: {rescan_exc}"
    return out


def run_apply(
    paths: Paths, args: argparse.Namespace, before_commit: Any = None
) -> tuple[int, dict[str, Any]]:
    from opencrab.locking import write_lock
    from opencrab.stores.backup import backup_data_dir

    try:
        lock = write_lock(str(paths.data_dir), timeout=args.lock_timeout)
        lock.__enter__()
    except TimeoutError as exc:
        return 3, {"error": str(exc)}
    try:
        backup = backup_data_dir(paths.data_dir, args.backup_to, lock_timeout=args.lock_timeout)
        before = scan(paths, args.allow_pack_missing, mode="ro")
        out: dict[str, Any] = {
            "backup_set": str(backup.set_dir),
            "before": report_of(before, args.list_ids),
        }
        try:
            stats = _write_batches(
                paths,
                before["plan"],
                before,
                args.batch_size,
                args.max_batches,
                args.allow_pack_missing,
                before_commit,
            )
        except BaseException as exc:
            failure = _failure_report(paths, args, exc)
            out.pop("before", None)
            out.update(failure)
            if isinstance(exc, Exception):
                return 1, out
            exc.backfill_report = out
            raise
        out["write"] = {
            "batches": stats["batches"],
            "committed": {k: len(v) for k, v in stats["committed"].items()},
            "skipped_changed": sum(len(v) for v in stats["skipped_changed"].values()),
            "skipped_unserializable": {
                k: len(v) for k, v in stats["skipped_unserializable"].items()
            },
            "remaining": stats["remaining"],
        }
        if args.list_ids:
            for kind in ("changed", "unserializable"):
                out[f"skipped_{kind}_ids"] = {
                    k: v[:LIST_CAP] for k, v in stats[f"skipped_{kind}"].items()
                }
        rec = reconcile(paths, before, stats, args.allow_pack_missing)
        out["reconcile"] = rec
        clean = rec["ok"] and not any(stats["skipped_unserializable"].values())
        return (0 if clean else 1), out
    finally:
        lock.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-dir", help="local data directory (default LOCAL_DATA_DIR)")
    p.add_argument("--apply", action="store_true", help="write (needs --backup-to)")
    p.add_argument("--backup-to", help="existing directory that receives a backup set")
    p.add_argument("--max-batches", type=int, help="stop after this many batches")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--list-ids", action="store_true", help="list held ids (capped per class)")
    p.add_argument(
        "--dry-run-scratch",
        type=Path,
        help="existing non-/tmp directory for report-only dry-run state",
    )
    p.add_argument(
        "--allow-pack-missing",
        action="store_true",
        help="apply the graph space when a pack_id is missing on either side",
    )
    p.add_argument("--lock-timeout", type=float, default=30.0)
    return p


def main(argv: list[str] | None = None, before_commit: Any = None) -> int:
    args = _parser().parse_args(argv)
    if args.apply and not args.backup_to:
        print("--apply needs --backup-to", file=sys.stderr)
        return 2
    if args.backup_to and not args.apply:
        print("--backup-to is only valid with --apply", file=sys.stderr)
        return 2
    if args.apply and args.dry_run_scratch:
        print("--dry-run-scratch is only valid without --apply", file=sys.stderr)
        return 2
    if not args.apply and args.dry_run_scratch is None:
        print("dry run needs --dry-run-scratch", file=sys.stderr)
        return 2
    if args.batch_size < 1 or (args.max_batches is not None and args.max_batches < 1):
        print("--batch-size and --max-batches must be at least 1", file=sys.stderr)
        return 2
    if args.data_dir:
        os.environ["LOCAL_DATA_DIR"] = args.data_dir
    from opencrab.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()
    if cfg.storage_mode != "local" or cfg.vector_backend_resolved != "sqlite-vec":
        print(
            "only STORAGE_MODE=local with the sqlite-vec vector backend is supported",
            file=sys.stderr,
        )
        return 2
    paths = Paths(Path(cfg.local_data_dir), cfg.vector_db_file, cfg.vector_collection)
    absent = paths.missing()
    if absent:
        print("missing store files: " + ", ".join(absent), file=sys.stderr)
        return 2
    if not args.apply:
        return _run_dry_run(paths, args)
    try:
        code, out = run_apply(paths, args, before_commit)
    except BaseException as exc:
        report = getattr(exc, "backfill_report", None)
        if report is not None:
            print(
                json.dumps(
                    {"mode": "apply", "reraised": type(exc).__name__, **report},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        raise
    print(json.dumps({"mode": "apply", "exit": code, **out}, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
