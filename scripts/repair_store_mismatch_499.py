#!/usr/bin/env python3
"""One-off repair of the graph / node-document store disagreement (#499).

#495 found ids whose graph type and node-document type differ, and
node-document rows with no graph node. #498 stops new ones. This tool repairs
the existing rows of a local (SQLite) data directory. Run it with the
services stopped; the guard refuses otherwise.

Classes (selection):

- P1: one doc row in the graph space; the graph type fails the grammar in that
  space and the doc type passes. The tool reclassifies the graph node to the
  doc type (properties and space stay). Runs without approval.
- P2: one doc row in the graph space; graph ``LogEntry``, doc ``TextUnit``.
  The graph wins: the tool rewrites the doc row, registry row and vector
  from the graph. Runs without approval.
- P3: every other type mismatch. Report only: the summary lists metadata per
  id for a later decision. The tool has no write path for P3.
- P4: every doc row without a graph node. The dry run compares each row with
  its pack input (``<pack input root>/<pack_id>/nodes.jsonl``) and proposes
  ``delete`` only when all of these hold: the id has one doc row, no graph
  edge and no vector row; the input holds every graph node of the pack except
  its anchor; the id is absent from the input; the row is older than every
  in-input doc row of the pack. The tool does not decide: it deletes only rows
  with that proposal AND a line in the approval file. The other P4 rows
  ("hold") are report only.

State model: the candidate file records, per id, a snapshot of every store
component (as digests, no property values) and the target of each planned
write ("leg"). Before each leg the tool reads the whole snapshot again. Each
component must be at its recorded value or at a target of this action;
otherwise the id is a conflict and nothing more is written for it. A leg
whose component is already at its target is skipped. So a rerun after any
interruption continues from the stores, not from the progress file.

Selection reads the four files with ``immutable=1`` and requires empty
``-wal`` files, so a dry run touches no file of the data directory.

Usage::

    # dry run (default): selection, candidate file, summary, proposal
    python scripts/repair_store_mismatch_499.py --local-data-dir D \\
        --run-dir R1 --pack-input-root P
    # guard only (services stopped?)
    python scripts/repair_store_mismatch_499.py --local-data-dir D \\
        --run-dir R1 --pack-input-root P --check-guard
    # apply into a new run dir (selects again under the lock)
    python scripts/repair_store_mismatch_499.py --local-data-dir D \\
        --run-dir R2 --pack-input-root P --apply --approved approved.jsonl
    # (approved.jsonl: the lines of R1/proposed-approval.jsonl the lead accepts)
    # resume an interrupted apply: the same command with the same R2

Apply builds the vector store from Settings exactly as the service does, so
run it with the service environment (``EMBEDDING_BACKEND=openai``,
``VECTOR_BACKEND=sqlite-vec``); any other vector store is refused.

Exit codes: 0 ok; 2 usage or refused input; 3 guard refused; 4 conflicts or
errors remain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import canonical_node_digest, prepare_node, thaw_json
from opencrab.grammar.validator import validate_node
from opencrab.stores._graph_common import _merge_space, decode_properties
from opencrab.stores._sql_dialect import SQLITE
from opencrab.stores._sql_graph_base import _SqlGraphStoreBase

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GUARD = 3
EXIT_INCOMPLETE = 4

DB_FILES = ("graph.db", "doc_store.db", "vectors.db", "opencrab.db")
SIDECARS = ("", "-wal", "-shm", "-journal")

P1, P2, P3, P4 = "P1", "P2", "P3", "P4"

ABSENT = None


class RepairRefusedError(RuntimeError):
    """The run must stop before any write (usage, input, or guard)."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def _sha(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def json_digest(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def doc_props_digest(raw: Any) -> str:
    """Digest of a stored doc ``properties`` column (parsed JSON, canonical)."""
    try:
        return json_digest(json.loads(raw))
    except (TypeError, ValueError):
        return "raw:" + _sha(str(raw))


def stored_graph_digest(node_type: str, space_id: str | None, raw: Any) -> str:
    """The value ``get_node_digest`` returns for this stored row, and the
    value the CAS in ``update_node`` compares with."""
    props, corrupted = decode_properties(raw)
    if corrupted:
        return "corrupted"
    merged = _merge_space(props, space_id)
    try:
        return canonical_node_digest(node_type, space_id or merged.get("space"), merged)
    except (TypeError, ValueError):
        return ""


class _InventoryReader:
    """Decodes a graph row exactly as ``get_node_identity_by_id`` does
    (``_node_inventory_row``) without a store connection, so the dry run
    opens no store class."""

    _dialect = SQLITE
    _issue = staticmethod(_SqlGraphStoreBase._issue)
    _same_value = staticmethod(_SqlGraphStoreBase._same_value)

    def row(self, node_type: str, node_id: str, space_id: str | None, raw: Any) -> Any:
        return _SqlGraphStoreBase._node_inventory_row(self, (node_type, node_id, space_id, raw))


def doc_props_from_graph(node_type: str, node_id: str, space_id: str | None, raw: Any) -> dict[str, Any] | None:
    """Doc properties built from a graph row the way the reconcile tool's
    ``_backfill_one`` builds them (normalized properties plus ``pack_id``)."""
    row = _InventoryReader().row(node_type, node_id, space_id, raw)
    if row.property_error is not None or row.normalization_issues or row.normalized_properties is None:
        return None
    props = thaw_json(row.normalized_properties)
    if row.pack_id is not None:
        props["pack_id"] = row.pack_id
    return props


def vector_target(node_id: str, node_type: str, space: str, props: dict[str, Any]) -> dict[str, Any]:
    """Text and metadata exactly as ``OntologyBuilder.add_node`` writes them."""
    from opencrab.ontology.bm25 import _node_text
    from opencrab.stores._vector_base import slot_owner
    from opencrab.stores.chroma_store import _sanitize_metadata

    text = _node_text({"node_id": node_id, "node_type": node_type, "properties": props})
    meta = {
        "pack_id": str(props.get("pack_id") or ""),
        "source": str(props.get("pack_id") or ""),
        "node_id": node_id,
        "space": space,
    }
    clean = _sanitize_metadata(meta)
    return {
        "text": text,
        "meta": meta,
        "value": {"pack_id": slot_owner(clean), "doc_sha": _sha(text), "meta_sha": json_digest(clean)},
    }


# ---------------------------------------------------------------------------
# Read connection and snapshot
# ---------------------------------------------------------------------------


def _load_vec(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def require_empty_wal(data_dir: Path) -> None:
    for name in DB_FILES:
        wal = data_dir / f"{name}-wal"
        if wal.exists() and wal.stat().st_size > 0:
            raise RepairRefusedError(
                f"{wal.name} is not empty. Stop the services, then run "
                f"PRAGMA wal_checkpoint(TRUNCATE) on {name}, and retry."
            )


def open_reader(data_dir: Path, *, immutable: bool) -> sqlite3.Connection:
    """One read connection: doc_store with graph, opencrab and vectors attached.

    ``immutable=1`` (selection) reads only the main files and touches no
    -wal or -shm file; the caller has checked that every -wal is empty.
    Without it (apply), the reader sees the commits of the store writers.
    """
    flag = "mode=ro&immutable=1" if immutable else "mode=ro"

    def uri(name: str) -> str:
        return f"{(data_dir / name).resolve().as_uri()}?{flag}"

    conn = sqlite3.connect(uri("doc_store.db"), uri=True)
    _load_vec(conn)
    conn.execute(f"ATTACH DATABASE '{uri('graph.db')}' AS g")
    conn.execute(f"ATTACH DATABASE '{uri('opencrab.db')}' AS o")
    conn.execute(f"ATTACH DATABASE '{uri('vectors.db')}' AS v")
    conn.execute("PRAGMA query_only=ON")
    return conn


class Reader:
    """Snapshot reads for one id, on one read connection."""

    def __init__(self, conn: sqlite3.Connection, vector_table: str) -> None:
        if not vector_table.isidentifier():
            raise RepairRefusedError(f"unsafe vector table name: {vector_table!r}")
        self.conn = conn
        self.vector_table = vector_table
        self.refresh_spaces()

    def _distinct(self, table: str, column: str) -> list[str]:
        """Distinct values of the leading column of a unique index, one index
        probe per value (loose index scan). The doc and registry tables have
        no ``node_id`` index; their unique keys lead with the space, so a
        lookup by (space IN all, node_id) needs the current space list."""
        sql = (
            f"WITH RECURSIVE s(x) AS (SELECT min({column}) FROM {table} "
            f"UNION ALL SELECT (SELECT min({column}) FROM {table} WHERE {column} > s.x) "
            f"FROM s WHERE s.x IS NOT NULL) SELECT x FROM s WHERE x IS NOT NULL"
        )
        return [r[0] for r in self.conn.execute(sql)]

    def refresh_spaces(self) -> None:
        self.doc_spaces = self._distinct("doc_nodes", "space")
        self.reg_spaces = self._distinct("o.ontology_nodes", "space")
        self.reg_edge_spaces = self._distinct("o.ontology_edges", "from_space")

    @staticmethod
    def _in(values: list[str]) -> str:
        return ",".join("?" for _ in values) or "NULL"

    def graph_row(self, node_id: str) -> tuple[str, str | None, Any] | None:
        return self.conn.execute(
            "SELECT node_type, space_id, properties FROM g.graph_nodes WHERE node_id=?", (node_id,)
        ).fetchone()

    def doc_rows(self, node_id: str) -> list[tuple[str, str, Any, str]]:
        return self.conn.execute(
            f"SELECT space, node_type, properties, updated_at FROM doc_nodes "
            f"WHERE space IN ({self._in(self.doc_spaces)}) AND node_id=?",
            (*self.doc_spaces, node_id),
        ).fetchall()

    def snapshot(self, node_id: str, *, with_reg_edges: bool) -> dict[str, Any]:
        """Every component of one id, read in one read transaction with the
        space lists read again, so a row in a new space is seen."""
        self.conn.execute("BEGIN")
        try:
            self.refresh_spaces()
            return self._snapshot(node_id, with_reg_edges=with_reg_edges)
        finally:
            self.conn.execute("COMMIT")

    def _snapshot(self, node_id: str, *, with_reg_edges: bool) -> dict[str, Any]:
        c = self.conn
        snap: dict[str, Any] = {}
        g = self.graph_row(node_id)
        edges = c.execute(
            "SELECT (SELECT count(*) FROM g.graph_edges WHERE from_id=?)"
            " + (SELECT count(*) FROM g.graph_edges WHERE to_id=? AND from_id<>?)",
            (node_id, node_id, node_id),
        ).fetchone()[0]
        snap["graph"] = (
            None if g is None
            else {"type": g[0], "space": g[1], "digest": stored_graph_digest(g[0], g[1], g[2])}
        )
        snap["graph_edges"] = edges
        for space, node_type, raw, updated in self.doc_rows(node_id):
            snap[f"doc:{space}"] = {"type": node_type, "props": doc_props_digest(raw), "updated_at": updated}
        for rowid, space, node_type in c.execute(
            f"SELECT id, space, node_type FROM o.ontology_nodes "
            f"WHERE space IN ({self._in(self.reg_spaces)}) AND node_id=?",
            (*self.reg_spaces, node_id),
        ):
            snap[f"reg:{space}"] = {"rowid": rowid, "type": node_type}
        if with_reg_edges:
            rows = c.execute(
                f"SELECT id, from_space, from_id, relation, to_space, to_id FROM o.ontology_edges "
                f"WHERE (from_space IN ({self._in(self.reg_edge_spaces)}) AND from_id=?) OR to_id=?",
                (*self.reg_edge_spaces, node_id, node_id),
            ).fetchall()
            for r in rows:
                snap[f"regedge:{r[0]}"] = list(r[1:])
        v = c.execute(
            f"SELECT pack_id, document, metadata, embedding FROM v.{self.vector_table} WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if v is not None:
            try:
                meta_sha = json_digest(json.loads(v[2]))
            except (TypeError, ValueError):
                meta_sha = "raw:" + _sha(str(v[2]))
            snap["vector"] = {
                "pack_id": v[0] or "",
                "doc_sha": _sha(v[1] or ""),
                "meta_sha": meta_sha,
                "emb_sha": _sha(bytes(v[3]) if v[3] is not None else b""),
            }
        return snap


# ---------------------------------------------------------------------------
# Component comparison
# ---------------------------------------------------------------------------


def _eq_recorded(cur: Any, rec: Any) -> bool:
    return cur == rec


def _eq_target(key: str, cur: Any, tgt: Any) -> bool:
    """A target names only what the leg sets; other fields are free."""
    if tgt is ABSENT:
        return cur is None
    if cur is None:
        return False
    return all(cur.get(k) == v for k, v in tgt.items())


def check_snapshot(snap: dict[str, Any], recorded: dict[str, Any], targets: dict[str, Any]) -> str | None:
    """None when every component is at its recorded value or a target;
    otherwise the first offending component key."""
    for key in sorted(set(snap) | set(recorded)):
        cur, rec = snap.get(key), recorded.get(key)
        if _eq_recorded(cur, rec):
            continue
        if key in targets and _eq_target(key, cur, targets[key]):
            continue
        return key
    return None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

TYPE_MIS_SQL = (
    "SELECT DISTINCT d.node_id FROM doc_nodes d JOIN g.graph_nodes n ON n.node_id = d.node_id "
    "WHERE n.node_type <> d.node_type"
)
DOC_ONLY_SQL = (
    "SELECT DISTINCT d.node_id FROM doc_nodes d "
    "WHERE NOT EXISTS (SELECT 1 FROM g.graph_nodes n WHERE n.node_id = d.node_id)"
)


def _graph_props_for_update(g: tuple[str, str | None, Any]) -> dict[str, Any]:
    props, corrupted = decode_properties(g[2])
    if corrupted:
        raise ValueError("graph properties corrupted")
    return _merge_space(props, g[1])


def _leg(key: str, op: str, target: Any, **params: Any) -> dict[str, Any]:
    return {"key": key, "op": op, "target": target, **params}


def plan_p1(node_id: str, g: tuple, doc: tuple) -> list[dict[str, Any]]:
    props = _graph_props_for_update(g)
    digest = prepare_node(doc[1], node_id, props, g[1])[3]
    return [_leg("graph", "graph_reclassify", {"type": doc[1], "space": g[1], "digest": digest},
                 source="graph", new_type=doc[1], new_space=g[1])]


def plan_p2(node_id: str, g: tuple) -> list[dict[str, Any]]:
    """Doc row, registry row and vector rewritten from the graph row."""
    props = doc_props_from_graph(g[0], node_id, g[1], g[2])
    if props is None or g[1] is None:
        raise ValueError("graph row cannot be decoded")
    space, node_type = g[1], g[0]
    vt = vector_target(node_id, node_type, space, props)
    return [
        _leg(f"doc:{space}", "doc_upsert", {"type": node_type, "props": json_digest(props)},
             space=space, node_type=node_type),
        _leg(f"reg:{space}", "reg_upsert", {"type": node_type}, space=space, node_type=node_type),
        _leg("vector", "vector_upsert", vt["value"], space=space, node_type=node_type),
    ]


def plan_p4_delete(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Every doc row, registry node row and registry edge row of the id."""
    legs = [_leg(k, "doc_delete", ABSENT, space=k.split(":", 1)[1]) for k in sorted(snap) if k.startswith("doc:")]
    legs += [_leg(k, "reg_delete", ABSENT, space=k.split(":", 1)[1]) for k in sorted(snap) if k.startswith("reg:")]
    legs += [_leg(k, "reg_edge_delete", ABSENT) for k in sorted(snap) if k.startswith("regedge:")]
    return legs


def p3_evidence(node_id: str, g: tuple, snap: dict[str, Any], rows: dict[str, tuple]) -> dict[str, Any]:
    """Metadata only (no property values) for the lead's per-id decision."""
    def pack_of(raw: Any) -> str:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return ""
        return str(value.get("pack_id") or "") if isinstance(value, dict) else ""

    vec = snap.get("vector")
    return {
        "graph": {"space": g[1], "type": g[0], "valid": validate_node(g[1], g[0]).valid, "pack_id": pack_of(g[2])},
        "docs": [{"space": s, "type": r[1], "valid": validate_node(s, r[1]).valid, "pack_id": pack_of(r[2])}
                 for s, r in sorted(rows.items())],
        "registry": [{"space": k.split(":", 1)[1], "type": v["type"]} for k, v in sorted(snap.items()) if k.startswith("reg:")],
        "graph_edges": snap["graph_edges"],
        # The space each registry edge row gives this id (from or to end).
        "registry_edge_spaces": dict(Counter(
            cols[0] if cols[1] == node_id else cols[3] for k, cols in snap.items() if k.startswith("regedge:"))),
        "vector_pack_id": None if vec is None else vec["pack_id"],
    }


def _file_identity(path: Path, root: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    st = path.stat()
    return {"path": str(path.relative_to(root)), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "sha256": _sha(path.read_bytes())}


class PackInputs:
    """Pack input files (``<root>/<pack_id>/nodes.jsonl``) and the evidence
    the proposal uses. The tool never decides on this evidence alone."""

    def __init__(self, root: Path, reader: Reader) -> None:
        self.root = root
        self.reader = reader
        self._cache: dict[str, dict[str, Any]] = {}

    def info(self, pack_id: str) -> dict[str, Any]:
        if pack_id in self._cache:
            return self._cache[pack_id]
        from opencrab.pack.ownership import anchor_node_id

        info: dict[str, Any] = {"input": None, "covers_graph": False, "rows": {}, "in_input_min_updated": None}
        path = self.root / pack_id / "nodes.jsonl"
        if pack_id and "/" not in pack_id and pack_id not in (".", "..") and path.is_file():
            info["input"] = _file_identity(path, self.root)
            rows: dict[str, dict[str, Any]] = {}
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        row = json.loads(line)
                        rows[str(row.get("id"))] = row
            info["rows"] = rows
            c = self.reader.conn
            graph_ids = {r[0] for r in c.execute(
                "SELECT node_id FROM g.graph_nodes WHERE json_extract(properties, '$.pack_id') = ?", (pack_id,))}
            graph_ids.discard(anchor_node_id(pack_id))
            info["covers_graph"] = graph_ids <= set(rows)
            times = [u for nid, u in c.execute(
                "SELECT node_id, updated_at FROM doc_nodes WHERE json_extract(properties, '$.pack_id') = ?",
                (pack_id,)) if nid in rows]
            info["in_input_min_updated"] = min(times) if times else None
        self._cache[pack_id] = info
        return info


def p4_proposal(node_id: str, rows: dict[str, tuple], inputs: PackInputs,
                snap_edges: int = 0, has_vector: bool = False) -> tuple[str | None, dict[str, Any]]:
    from opencrab.pack.normalize import transform_node

    evidence: dict[str, Any] = {"doc_rows": len(rows), "graph_edges": snap_edges, "vector": has_vector}
    if len(rows) != 1:
        return None, evidence
    (space, (_, node_type, raw, updated)), = rows.items()
    try:
        props = json.loads(raw)
    except (TypeError, ValueError):
        props = {}
    pack_id = str(props.get("pack_id") or "") if isinstance(props, dict) else ""
    info = inputs.info(pack_id)
    evidence.update({"pack_id": pack_id, "space": space, "type": node_type, "updated_at": updated,
                     "input": info["input"], "covers_graph": info["covers_graph"],
                     "in_input_min_updated": info["in_input_min_updated"]})
    if info["input"] is None or not info["covers_graph"]:
        return None, evidence
    if snap_edges or has_vector:
        return None, evidence
    in_input = node_id in info["rows"]
    evidence["in_input"] = in_input
    if not in_input:
        older = info["in_input_min_updated"] is not None and updated < info["in_input_min_updated"]
        evidence["older_than_reload"] = older
        return ("delete" if older else None), evidence
    t_space, t_type, _, _ = transform_node(pack_id, info["rows"][node_id])
    evidence["input_space_type_match"] = (t_space, t_type) == (space, node_type)
    return ("promote" if evidence["input_space_type_match"] else None), evidence


def select(reader: Reader, inputs: PackInputs) -> list[dict[str, Any]]:
    """Classify every mismatched id. Pure reads on ``reader``.

    P1 and P2 records carry ``legs``; P4 records carry ``legs`` for the
    delete action and a ``proposal``; P3 records carry evidence only."""
    c = reader.conn
    out: list[dict[str, Any]] = []
    for (node_id,) in c.execute(TYPE_MIS_SQL).fetchall():
        g = reader.graph_row(node_id)
        rows = {r[0]: r for r in reader.doc_rows(node_id)}
        one = len(rows) == 1 and g[1] in rows
        doc = rows.get(g[1]) if one else None
        cls = P3
        if one and not validate_node(g[1], g[0]).valid and validate_node(g[1], doc[1]).valid:
            cls = P1
        elif one and g[0] == "LogEntry" and doc[1] == "TextUnit":
            cls = P2
        snap = reader.snapshot(node_id, with_reg_edges=cls == P3)
        rec: dict[str, Any] = {"node_id": node_id, "class": cls, "recorded": snap}
        try:
            if cls == P1:
                rec["legs"] = plan_p1(node_id, g, doc)
            elif cls == P2:
                rec["legs"] = plan_p2(node_id, g)
        except Exception as exc:  # noqa: BLE001 -- an unplannable id is reported with P3
            cls = rec["class"] = P3
            rec.pop("legs", None)
            rec["plan_error"] = f"{type(exc).__name__}: {exc}"
            rec["recorded"] = snap = reader.snapshot(node_id, with_reg_edges=True)
        if cls == P3:
            rec["evidence"] = p3_evidence(node_id, g, snap, rows)
        out.append(rec)
    for (node_id,) in c.execute(DOC_ONLY_SQL).fetchall():
        rows = {r[0]: r for r in reader.doc_rows(node_id)}
        snap = reader.snapshot(node_id, with_reg_edges=True)
        proposal, evidence = p4_proposal(node_id, rows, inputs, snap["graph_edges"], "vector" in snap)
        out.append({"node_id": node_id, "class": P4, "recorded": snap, "legs": plan_p4_delete(snap),
                    "proposal": proposal, "evidence": evidence})
    return out


def summarize(cands: list[dict[str, Any]]) -> dict[str, Any]:
    by_class = Counter(r["class"] for r in cands)
    proposals = Counter(r.get("proposal") or "none" for r in cands if r["class"] == P4)
    p4_packs = Counter((r.get("evidence", {}).get("pack_id", ""), r.get("proposal") or "none")
                       for r in cands if r["class"] == P4)
    inputs = {}
    for r in cands:
        ev = r.get("evidence", {})
        if r["class"] == P4 and ev.get("input"):
            inputs[ev["pack_id"]] = ev["input"]
    p3 = [{"node_id": r["node_id"], "plan_error": r.get("plan_error"), **r["evidence"]}
          for r in cands if r["class"] == P3]
    p4 = [{"node_id": r["node_id"], "proposal": r.get("proposal"),
           **{k: v for k, v in r.get("evidence", {}).items() if k != "input"}}
          for r in cands if r["class"] == P4]
    return {"by_class": dict(sorted(by_class.items())), "p4_proposals": dict(proposals),
            "p4_by_pack": [[p, a, n] for (p, a), n in sorted(p4_packs.items())],
            "pack_inputs": inputs, "p3": p3, "p4": p4}


# ---------------------------------------------------------------------------
# Run directory: atomic publication, manifest, progress
# ---------------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def publish_run(run_dir: Path, cands: list[dict[str, Any]]) -> str:
    """Candidate file first, manifest last: the manifest is the commit point."""
    body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in cands).encode("utf-8")
    _publish(run_dir / "candidates.jsonl", body)
    sha = _sha(body)
    _publish(run_dir / "manifest.json",
             json.dumps({"candidates_sha256": sha, "lines": len(cands)}, sort_keys=True).encode("utf-8"))
    return sha


def load_run(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    manifest = run_dir / "manifest.json"
    cand = run_dir / "candidates.jsonl"
    if not manifest.is_file() or not cand.is_file():
        raise RepairRefusedError(f"{run_dir} has no published candidate file (manifest missing)")
    body = cand.read_bytes()
    m = json.loads(manifest.read_text(encoding="utf-8"))
    cands = [json.loads(line) for line in body.decode("utf-8").splitlines() if line]
    if m.get("candidates_sha256") != _sha(body) or m.get("lines") != len(cands):
        raise RepairRefusedError("candidate file does not match its manifest")
    return cands, m["candidates_sha256"]


class Progress:
    def __init__(self, path: Path, cand_sha: str, batch: int) -> None:
        self._fh = path.open("a", encoding="utf-8", newline="\n")
        self._sha = cand_sha
        self._batch = max(1, batch)
        self._n = 0

    def write(self, node_id: str, leg: str, outcome: str, detail: str | None = None) -> None:
        self._fh.write(json.dumps({"cand": self._sha[:16], "node_id": node_id, "leg": leg,
                                   "outcome": outcome, "detail": detail}, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._n += 1
        if self._n % self._batch == 0:
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def load_approval(path: Path | None, cands: list[dict[str, Any]]) -> set[str]:
    """Approved P4 delete ids. Only ``delete`` for an id with the ``delete``
    proposal is accepted; any other line refuses the whole file, so P3 ids,
    P4 hold ids and other actions have no path to a write."""
    if path is None:
        return set()
    by_id = {r["node_id"]: r for r in cands}
    out: set[str] = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            node_id, action = item["node_id"], item["action"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RepairRefusedError(f"approval line {n}: malformed ({exc})") from exc
        rec = by_id.get(node_id)
        if rec is None or rec["class"] != P4 or rec.get("proposal") != "delete":
            raise RepairRefusedError(f"approval line {n}: {node_id!r} is not a P4 row with the delete proposal")
        if action != "delete":
            raise RepairRefusedError(f"approval line {n}: action {action!r} is not allowed")
        if node_id in out:
            raise RepairRefusedError(f"approval line {n}: {node_id!r} appears twice")
        out.add(node_id)
    return out


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def holders(data_dir: Path) -> tuple[list[int], int]:
    """Processes (other than this one) that hold a DB file of ``data_dir``.

    Two sources, both Linux ``/proc``:

    - open file descriptors, for every process whose fd table this user can
      read;
    - POSIX locks in ``/proc/locks`` (readable for every process): an open
      SQLite connection in WAL mode keeps a lock on the ``-shm`` file, and a
      writer locks the main file. This also covers processes whose fd table
      cannot be read (other users, or this user's non-dumpable processes such
      as ``ssh-agent``).

    Returns the holder pids and the number of processes whose fd table could
    not be read (reported, covered only by the lock source)."""
    paths = [data_dir / f"{n}{s}" for n in DB_FILES for s in SIDECARS]
    targets = {str(p.resolve()) for p in paths}
    inodes = set()
    for p in paths:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        inodes.add((os.major(st.st_dev), os.minor(st.st_dev), st.st_ino))
    me = os.getpid()
    found: set[int] = set()
    unreadable = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            fds = os.listdir(f"/proc/{entry}/fd")
        except FileNotFoundError:
            continue
        except PermissionError:
            unreadable += 1
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{entry}/fd/{fd}") in targets:
                    found.add(int(entry))
                    break
            except OSError:
                continue
    found |= _lock_holders(inodes, me)
    return sorted(found), unreadable


def _lock_holders(inodes: set[tuple[int, int, int]], me: int) -> set[int]:
    out: set[int] = set()
    try:
        lines = Path("/proc/locks").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        # "1: POSIX  ADVISORY  WRITE 1234 08:02:123456 0 EOF"; a blocked
        # waiter line has "->" after the number.
        if len(parts) > 1 and parts[1] == "->":
            parts = [parts[0]] + parts[2:]
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[4])
            maj, mnr, ino = parts[5].split(":")
            key = (int(maj, 16), int(mnr, 16), int(ino))
        except ValueError:
            continue
        if key in inodes and pid != me:
            out.add(pid)
    return out


def graph_schema_state_ro(data_dir: Path) -> str:
    """The graph schema state, read on a ``mode=ro`` connection with the
    store's own classification (it uses no instance state, writes nothing)."""
    from opencrab.stores.local_graph_store import LocalGraphStore

    path = data_dir / "graph.db"
    if not path.is_file():
        return "missing"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return LocalGraphStore._classify_schema(None, conn)
    finally:
        conn.close()


def check_holders(data_dir: Path, scan: Callable[[Path], tuple[list[int], int]] = holders) -> None:
    found, _ = scan(data_dir)
    if found:
        raise RepairRefusedError(f"processes hold the data files open: {found}; stop the services", EXIT_GUARD)


# ---------------------------------------------------------------------------
# Stores and leg execution
# ---------------------------------------------------------------------------


class Stores:
    """The four store objects the legs write through, plus a raw registry
    connection for the deletes no store method offers."""

    def __init__(self, graph: Any, docs: Any, sql: Any, vec: Any) -> None:
        self.graph, self.docs, self.sql, self.vec = graph, docs, sql, vec


def build_stores(data_dir: Path, settings: Any) -> Stores:
    from opencrab.stores.factory import make_vector_store
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sql_store import SQLStore
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    # Checked before any constructor runs: the vector store constructor runs DDL.
    if settings.vector_backend_resolved != "sqlite-vec" or settings.embedding_backend != "openai":
        raise RepairRefusedError("the vector backend must be sqlite-vec (VECTOR_BACKEND, EMBEDDING_BACKEND)")
    if (Path(settings.local_data_dir) / settings.vector_db_file).resolve() != (data_dir / "vectors.db").resolve():
        raise RepairRefusedError("the vector store does not use <data dir>/vectors.db")
    vec = make_vector_store(settings)
    if not isinstance(vec, SqliteVecStore):
        raise RepairRefusedError("the vector backend must be sqlite-vec (VECTOR_BACKEND, EMBEDDING_BACKEND)")
    if Path(vec._db_path).resolve() != (data_dir / "vectors.db").resolve():
        raise RepairRefusedError("the vector store does not use <data dir>/vectors.db")
    return Stores(
        LocalGraphStore(str(data_dir / "graph.db")),
        LocalSQLDocStore(str(data_dir / "doc_store.db")),
        SQLStore(f"sqlite:///{data_dir / 'opencrab.db'}", create_tables=False),
        vec,
    )


class LegError(RuntimeError):
    pass


def execute_leg(stores: Stores, reader: Reader, node_id: str, leg: dict[str, Any], recorded: dict[str, Any]) -> None:
    """One write. Content is built from the current rows and must have the
    recorded target digest before anything is written."""
    op = leg["op"]
    if op in ("graph_reclassify", "doc_upsert", "vector_upsert"):
        g = reader.graph_row(node_id)
        if g is None:
            raise LegError("graph row vanished")
    if op == "graph_reclassify":
        props = _graph_props_for_update(g)
        if prepare_node(leg["new_type"], node_id, props, leg["new_space"])[3] != leg["target"]["digest"]:
            raise LegError("built graph write does not match the recorded target")
        stores.graph.reclassify_node(node_id, expected_current_digest=recorded["graph"]["digest"],
                                     new_type=leg["new_type"], new_space_id=leg["new_space"],
                                     new_properties=props)
    elif op == "doc_upsert":
        props = doc_props_from_graph(g[0], node_id, g[1], g[2])
        if props is None or g[0] != leg["node_type"] or json_digest(props) != leg["target"]["props"]:
            raise LegError("built doc row does not match the recorded target")
        stores.docs.upsert_node_doc(leg["space"], g[0], node_id, props)
    elif op == "vector_upsert":
        props = doc_props_from_graph(g[0], node_id, g[1], g[2])
        if props is None:
            raise LegError("graph row cannot be decoded")
        vt = vector_target(node_id, g[0], leg["space"], props)
        if vt["value"] != leg["target"]:
            raise LegError("built vector does not match the recorded target")
        stores.vec.upsert_texts(texts=[vt["text"]], ids=[node_id], metadatas=[vt["meta"]])
    elif op == "reg_upsert":
        stores.sql.register_node(leg["space"], leg["node_type"], node_id)
    elif op == "doc_delete":
        stores.docs.delete_node_doc(leg["space"], node_id)
    elif op == "reg_delete":
        rec = recorded[leg["key"]]
        _registry_delete(stores, "DELETE FROM ontology_nodes WHERE id=:id AND space=:space"
                                 " AND node_id=:nid AND node_type=:t",
                         {"id": rec["rowid"], "space": leg["space"], "nid": node_id, "t": rec["type"]})
    elif op == "reg_edge_delete":
        c = recorded[leg["key"]]
        _registry_delete(stores, "DELETE FROM ontology_edges WHERE id=:id AND from_space=:fs AND from_id=:fi"
                                 " AND relation=:rel AND to_space=:ts AND to_id=:ti",
                         {"id": int(leg["key"].split(":", 1)[1]), "fs": c[0], "fi": c[1], "rel": c[2],
                          "ts": c[3], "ti": c[4]})
    else:  # pragma: no cover - the candidate file is ours
        raise LegError(f"unknown leg op {op!r}")


def _registry_delete(stores: Stores, sql: str, params: dict[str, Any]) -> None:
    # No SQLStore method deletes registry rows; this uses its engine in one
    # transaction, guarded by every recorded column.
    from sqlalchemy import text

    with stores.sql._engine.begin() as conn:
        conn.execute(text(sql), params)


def p4_invariant(snap: dict[str, Any]) -> str | None:
    """A P4 delete needs no graph node, no graph edge and no vector row of the
    id, whatever the candidate recorded."""
    if snap.get("graph") is not None:
        return "a graph node exists"
    if snap.get("graph_edges"):
        return "a graph edge names the id"
    if "vector" in snap:
        return "a vector row exists"
    return None


def run_id(stores: Stores, reader: Reader, rec: dict[str, Any], legs: list[dict[str, Any]],
           progress: Progress, before_write: Callable[[], str | None] = lambda: None) -> str:
    """Bring one id to its targets. Returns ``done``, ``conflict`` or ``error``.

    Before each leg the whole snapshot of the id is read again; every
    component must be at its recorded value or at a target of this id's
    legs. ``before_write`` may veto the write with a conflict reason."""
    node_id, recorded = rec["node_id"], rec["recorded"]
    targets = {leg["key"]: leg["target"] for leg in legs}
    with_edges = rec["class"] == P4
    for leg in legs:
        snap = reader.snapshot(node_id, with_reg_edges=with_edges)
        if rec["class"] == P4:
            broken = p4_invariant(snap)
            if broken is not None:
                progress.write(node_id, leg["key"], "conflict", broken)
                return "conflict"
        bad = check_snapshot(snap, recorded, targets)
        if bad is not None:
            progress.write(node_id, leg["key"], "conflict", f"component {bad} changed")
            return "conflict"
        if _eq_target(leg["key"], snap.get(leg["key"]), leg["target"]):
            progress.write(node_id, leg["key"], "done (already)")
            continue
        reason = before_write()
        if reason is not None:
            progress.write(node_id, leg["key"], "conflict", reason)
            return "conflict"
        try:
            execute_leg(stores, reader, node_id, leg, recorded)
        except Exception as exc:  # noqa: BLE001 -- one id never stops the run
            progress.write(node_id, leg["key"], "error", f"{type(exc).__name__}: {exc}"[:300])
            return "error"
        after = reader.snapshot(node_id, with_reg_edges=with_edges)
        if not _eq_target(leg["key"], after.get(leg["key"]), leg["target"]) or check_snapshot(after, recorded, targets):
            progress.write(node_id, leg["key"], "error", "target not reached after the write")
            return "error"
        progress.write(node_id, leg["key"], "done")
    return "done"


def _p4_input_unchanged(rec: dict[str, Any], root: Path) -> bool:
    """The pack input file still has the identity the proposal recorded.
    Every approved row has one (the delete proposal requires it)."""
    ident = rec.get("evidence", {}).get("input")
    if ident is None:
        return False
    cur = _file_identity(root / ident["path"], root)
    return cur is not None and cur["sha256"] == ident["sha256"]


def apply_candidates(stores: Stores, reader: Reader, cands: list[dict[str, Any]], approved: set[str],
                     progress: Progress, *, input_root: Path, batch: int,
                     between_batches: Callable[[], None]) -> Counter:
    """Steps in order: P1, P2, approved P4 delete. P3 and P4 rows without an
    approval are never reached."""
    order = [r for cls in (P1, P2) for r in cands if r["class"] == cls]
    order += [r for r in cands if r["class"] == P4 and r["node_id"] in approved]
    # A registry edge row can name two approved P4 ids. Each id has every
    # edge row that names it as a target-absent leg, so the second id reads
    # the row deleted by the first as "done (already)", not as a conflict.
    results: Counter = Counter()
    for n, rec in enumerate(order):
        if n % max(1, batch) == 0:
            between_batches()
        check = (lambda r=rec: None if _p4_input_unchanged(r, input_root) else "pack input file changed") \
            if rec["class"] == P4 else (lambda: None)
        results[(rec["class"], run_id(stores, reader, rec, rec["legs"], progress, check))] += 1
    return results


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def dry_run(data_dir: Path, run_dir: Path, input_root: Path, vector_table: str) -> dict[str, Any]:
    if (run_dir / "manifest.json").exists() or (run_dir / "candidates.jsonl").exists():
        raise RepairRefusedError(f"{run_dir} already holds a candidate file; use a new run dir")
    run_dir.mkdir(parents=True, exist_ok=True)
    require_empty_wal(data_dir)
    conn = open_reader(data_dir, immutable=True)
    try:
        reader = Reader(conn, vector_table)
        cands = select(reader, PackInputs(input_root, reader))
    finally:
        conn.close()
    publish_run(run_dir, cands)
    summary = summarize(cands)
    _publish(run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
    proposed = "".join(
        json.dumps({"node_id": r["node_id"], "action": r["proposal"]}, ensure_ascii=False) + "\n"
        for r in cands if r["class"] == P4 and r.get("proposal") == "delete")
    _publish(run_dir / "proposed-approval.jsonl", proposed.encode("utf-8"))
    return summary


def apply(data_dir: Path, run_dir: Path, input_root: Path, approved: Path | None, *, stores_factory: Callable[[], Stores],
          vector_table: str, batch: int = 500,
          scan: Callable[[Path], tuple[list[int], int]] = holders, lock_timeout: float = 10.0) -> Counter:
    from opencrab.locking import write_lock

    with ExitStack() as stack:
        stack.enter_context(write_lock(str(data_dir), timeout=lock_timeout, purpose="repair_store_mismatch_499"))
        check_holders(data_dir, scan)
        if (run_dir / "manifest.json").exists():
            cands, sha = load_run(run_dir)
        else:
            if (run_dir / "candidates.jsonl").exists():
                raise RepairRefusedError(f"{run_dir} has a candidate file without a manifest; use a new run dir")
            run_dir.mkdir(parents=True, exist_ok=True)
            require_empty_wal(data_dir)
            conn = open_reader(data_dir, immutable=True)
            try:
                sel = Reader(conn, vector_table)
                cands = select(sel, PackInputs(input_root, sel))
            finally:
                conn.close()
            sha = publish_run(run_dir, cands)
            _publish(run_dir / "summary.json",
                     json.dumps(summarize(cands), ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))
        approval = load_approval(approved, cands)
        stores = stores_factory()
        if stores.graph.graph_schema_state() != "target":
            raise RepairRefusedError("graph schema state is not 'target'", EXIT_GUARD)
        conn = open_reader(data_dir, immutable=False)
        stack.callback(conn.close)
        reader = Reader(conn, vector_table)
        progress = Progress(run_dir / "progress.jsonl", sha, batch)
        stack.callback(progress.close)
        return apply_candidates(stores, reader, cands, approval, progress, input_root=input_root, batch=batch,
                                between_batches=lambda: check_holders(data_dir, scan))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--local-data-dir", required=True, metavar="D")
    p.add_argument("--run-dir", required=True, metavar="R")
    p.add_argument("--pack-input-root", required=True, metavar="P")
    p.add_argument("--approved", default=None, metavar="FILE")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--check-guard", action="store_true")
    p.add_argument("--batch", type=int, default=500)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir = Path(args.local_data_dir).expanduser().resolve()
    run_dir = Path(args.run_dir).expanduser().resolve()
    input_root = Path(args.pack_input_root).expanduser().resolve()
    from opencrab.config import Settings

    settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(data_dir))
    vector_table = settings.vector_collection
    try:
        if args.check_guard:
            from opencrab.locking import write_lock

            with write_lock(str(data_dir), timeout=10.0, purpose="repair_store_mismatch_499 check"):
                found, foreign = holders(data_dir)
                state = graph_schema_state_ro(data_dir)
            print(json.dumps({"holders": found, "fd_tables_not_readable": foreign, "graph_schema": state}))
            return EXIT_GUARD if found or state != "target" else EXIT_OK
        if not args.apply:
            summary = dry_run(data_dir, run_dir, input_root, vector_table)
            print(json.dumps({"by_class": summary["by_class"], "p4_proposals": summary["p4_proposals"]}))
            return EXIT_OK
        results = apply(data_dir, run_dir, input_root, Path(args.approved) if args.approved else None,
                        stores_factory=lambda: build_stores(data_dir, settings),
                        vector_table=vector_table, batch=args.batch)
    except RepairRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return exc.code
    except TimeoutError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_GUARD
    print(json.dumps({f"{c}:{o}": n for (c, o), n in sorted(results.items())}))
    return EXIT_OK if all(o == "done" for (_, o) in results) else EXIT_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
