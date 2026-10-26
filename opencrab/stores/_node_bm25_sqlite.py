"""Persisted full node BM25 index inside the SQLite doc store (#541).

The index keeps the token stream of ``text_cues.tokenize`` in an FTS5
contentless table. Python computes the score with the same function as the
memory ``BM25Index`` (``bm25.bm25_score``), so the only change against the
memory index is the indexed range: every ``doc_nodes`` row instead of a
recent-first window. The design is ``docs/issue-541-db-bm25.md``.

All functions take an open ``sqlite3.Connection``. The caller owns the
transaction: ``LocalSQLDocStore`` wraps writes in its write transaction and
reads in one read transaction.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import Any

from opencrab.stores import _node_bm25_prune as _prune

logger = logging.getLogger(__name__)

FORMAT = "1"
PREFIX = "doc_nodes_bm25"
FTS = f"{PREFIX}_fts"
VROW = f"{PREFIX}_vrow"
VINST = f"{PREFIX}_vinst"
DOCS = f"{PREFIX}_docs"
STATS = f"{PREFIX}_stats"
PENDING = f"{PREFIX}_pending"
META = f"{PREFIX}_meta"
TRIGGERS = (f"{PREFIX}_ai", f"{PREFIX}_au", f"{PREFIX}_ad")

STATE_BACKFILL = "backfill"
STATE_READY = "ready"

# FTS5 cuts terms longer than 32768 bytes. Longer tokens become a surrogate
# term: U+2063 (not a \w character, so tokenize never emits it) followed by the
# SHA-256 of the token. Design 4.3.
_TERM_MAX_BYTES = 1024
_SURROGATE_MARK = "⁣"

# Rows read per verification or backfill query. Operational values only.
_SCAN_BATCH = 1000

PENDING_WARNING = "BM25 coverage pending changes: {n} node rows are not indexed yet"
UNAVAILABLE_WARNING = "BM25 coverage unavailable: node index query failed"


class IndexFormatMismatchError(RuntimeError):
    """The index ``meta`` has a format that this code does not write."""


class ReindexError(RuntimeError):
    """``reindex_key`` failed for one key; the message names the key."""


RowToNode = Callable[[Any], dict[str, Any]]


def _q(name: str) -> str:
    return f'"{name}"'


def ddl_statements() -> list[str]:
    """Return the CREATE statements of every index object (all IF NOT EXISTS)."""
    pend_ins = (
        "INSERT INTO {p}(space, node_id) SELECT {r}.space, {r}.node_id "
        "WHERE NOT EXISTS (SELECT 1 FROM {p} WHERE space={r}.space AND node_id={r}.node_id);"
    )
    return [
        f"CREATE TABLE IF NOT EXISTS {META} (key TEXT PRIMARY KEY, value TEXT)",
        f"CREATE TABLE IF NOT EXISTS {STATS} ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), doc_count INTEGER NOT NULL, "
        "total_len INTEGER NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {DOCS} ("
        "doc_key INTEGER PRIMARY KEY, space TEXT NOT NULL, node_id TEXT NOT NULL, "
        "pack_id TEXT NOT NULL, dl INTEGER NOT NULL, updated_at TEXT NOT NULL, "
        "UNIQUE (space, node_id))",
        f"CREATE TABLE IF NOT EXISTS {PENDING} ("
        "space TEXT NOT NULL, node_id TEXT NOT NULL, PRIMARY KEY (space, node_id)"
        ") WITHOUT ROWID",
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS} USING fts5("
        "tokens, content='', contentless_delete=1, tokenize=\"ascii tokenchars '_'\")",
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {VROW} USING fts5vocab({FTS}, 'row')",
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {VINST} USING fts5vocab({FTS}, 'instance')",
        # Design 4.1: INSERT ... WHERE NOT EXISTS, because an outer UPSERT or
        # INSERT OR REPLACE overrides the conflict policy of an INSERT OR
        # IGNORE inside a trigger.
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGERS[0]} AFTER INSERT ON doc_nodes BEGIN "
        + pend_ins.format(p=PENDING, r="NEW") + " END",
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGERS[1]} AFTER UPDATE ON doc_nodes BEGIN "
        + pend_ins.format(p=PENDING, r="OLD") + " "
        + pend_ins.format(p=PENDING, r="NEW") + " END",
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGERS[2]} AFTER DELETE ON doc_nodes BEGIN "
        + pend_ins.format(p=PENDING, r="OLD") + " END",
    ]


def fts_term(token: str) -> str:
    """Return the term the FTS table stores for one tokenize() token."""
    raw = token.encode("utf-8")
    if len(raw) <= _TERM_MAX_BYTES:
        return token
    return _SURROGATE_MARK + hashlib.sha256(raw).hexdigest()


def node_terms(node: dict[str, Any], max_text_chars: int | None) -> list[str]:
    """Return the stored term list of one node, in token order."""
    from opencrab.ontology.bm25 import _node_text
    from opencrab.ontology.text_cues import tokenize

    return [fts_term(t) for t in tokenize(_node_text(node)[:max_text_chars])]


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def _meta(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {k: v for k, v in conn.execute(f"SELECT key, value FROM {META}")}


def _set_meta(conn: sqlite3.Connection, **values: str | None) -> None:
    conn.executemany(
        f"INSERT INTO {META}(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(values.items()),
    )


def _encode_chars(value: int | None) -> str:
    return "0" if value is None else str(int(value))


def _decode_chars(value: str | None) -> int | None:
    number = int(value or "0")
    return number if number > 0 else None


def _reset(conn: sqlite3.Connection, max_text_chars: int | None) -> None:
    """Clear every index row and restart at backfill (design 5.2)."""
    conn.execute(f"DELETE FROM {DOCS}")
    conn.execute(f"INSERT INTO {FTS}({FTS}) VALUES ('delete-all')")
    conn.execute(f"DELETE FROM {PENDING}")
    conn.execute(f"INSERT OR REPLACE INTO {STATS}(id, doc_count, total_len) VALUES (1, 0, 0)")
    _set_meta(
        conn,
        format=FORMAT,
        max_text_chars=_encode_chars(max_text_chars),
        state=STATE_BACKFILL,
        cursor_space=None,
        cursor_node_id=None,
    )


def ensure_schema(conn: sqlite3.Connection, max_text_chars: int | None) -> str:
    """Create or check the index objects inside the caller's transaction.

    Returns the state after the checks. Design 5.2.
    """
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (META,)
    ).fetchone() is not None
    for ddl in ddl_statements():
        conn.execute(ddl)
    if not existed:
        _reset(conn, max_text_chars)
        if conn.execute("SELECT 1 FROM doc_nodes LIMIT 1").fetchone() is None:
            _set_meta(conn, state=STATE_READY)
        return _meta(conn)["state"] or STATE_BACKFILL
    meta = _meta(conn)
    if meta.get("format") != FORMAT or _decode_chars(meta.get("max_text_chars")) != max_text_chars:
        logger.warning(
            "node BM25 index format or MAX_NODE_CHARS changed; the index restarts at backfill"
        )
        _reset(conn, max_text_chars)
        return STATE_BACKFILL
    if meta.get("state") == STATE_READY:
        pending = conn.execute(f"SELECT 1 FROM {PENDING} LIMIT 1").fetchone()
        if pending is None:
            rows = conn.execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0]
            indexed = conn.execute(f"SELECT doc_count FROM {STATS} WHERE id=1").fetchone()
            if indexed is None or int(indexed[0]) != int(rows):
                logger.warning(
                    "node BM25 index row count differs from doc_nodes; the index restarts at backfill"
                )
                _reset(conn, max_text_chars)
                return STATE_BACKFILL
    return meta.get("state") or STATE_BACKFILL


def read_state(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """Return ``(state, format)`` from ``meta``."""
    meta = _meta(conn)
    return meta.get("state"), meta.get("format")


def is_ready(conn: sqlite3.Connection) -> bool:
    state, fmt = read_state(conn)
    return state == STATE_READY and fmt == FORMAT


def _require_format(meta: dict[str, str | None]) -> None:
    if meta.get("format") != FORMAT:
        raise IndexFormatMismatchError(
            f"node BM25 index format {meta.get('format')!r} is not {FORMAT!r}"
        )


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


_NODE_COLUMNS = "space, node_id, node_type, properties, updated_at"


def reindex_key(
    conn: sqlite3.Connection,
    row_to_node: RowToNode,
    space: str,
    node_id: str,
    meta: dict[str, str | None] | None = None,
) -> None:
    """Make the index entry of one key match its current row (design 4.2)."""
    if meta is None:
        meta = _meta(conn)
    _require_format(meta)
    max_text_chars = _decode_chars(meta.get("max_text_chars"))
    row = conn.execute(
        f"SELECT {_NODE_COLUMNS} FROM doc_nodes WHERE space=? AND node_id=?",
        (space, node_id),
    ).fetchone()
    old = conn.execute(
        f"SELECT doc_key, dl FROM {DOCS} WHERE space=? AND node_id=?", (space, node_id)
    ).fetchone()
    if old is not None:
        conn.execute(f"DELETE FROM {FTS} WHERE rowid=?", (old[0],))
        conn.execute(f"DELETE FROM {DOCS} WHERE doc_key=?", (old[0],))
        conn.execute(
            f"UPDATE {STATS} SET doc_count=doc_count-1, total_len=total_len-? WHERE id=1",
            (int(old[1]),),
        )
    if row is not None:
        from opencrab.ontology.pack_provenance import scope_pack_id

        node = row_to_node(row)
        terms = node_terms(node, max_text_chars)
        updated = row["updated_at"]
        cur = conn.execute(
            f"INSERT INTO {DOCS}(space, node_id, pack_id, dl, updated_at) VALUES (?, ?, ?, ?, ?)",
            (space, node_id, scope_pack_id(node) or "", len(terms),
             "" if updated is None else str(updated)),
        )
        conn.execute(f"INSERT INTO {FTS}(rowid, tokens) VALUES (?, ?)",
                     (cur.lastrowid, " ".join(terms)))
        conn.execute(
            f"UPDATE {STATS} SET doc_count=doc_count+1, total_len=total_len+? WHERE id=1",
            (len(terms),),
        )
    conn.execute(f"DELETE FROM {PENDING} WHERE space=? AND node_id=?", (space, node_id))


def _reindex_or_raise(conn, row_to_node, space, node_id, meta) -> None:
    try:
        reindex_key(conn, row_to_node, space, node_id, meta)
    except IndexFormatMismatchError:
        raise
    except Exception as exc:
        raise ReindexError(f"reindex failed for key ({space!r}, {node_id!r}): {exc}") from exc


def drain(conn: sqlite3.Connection, row_to_node: RowToNode, max_rows: int) -> int:
    """Reindex up to ``max_rows`` pending keys. Returns how many were processed."""
    meta = _meta(conn)
    _require_format(meta)
    keys = conn.execute(f"SELECT space, node_id FROM {PENDING} LIMIT ?", (max_rows,)).fetchall()
    for space, node_id in keys:
        _reindex_or_raise(conn, row_to_node, space, node_id, meta)
    return len(keys)


def pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {PENDING}").fetchone()[0])


def has_pending(conn: sqlite3.Connection) -> bool:
    return conn.execute(f"SELECT 1 FROM {PENDING} LIMIT 1").fetchone() is not None


def backfill_step(conn: sqlite3.Connection, row_to_node: RowToNode, batch: int) -> dict[str, Any]:
    """Index the next ``batch`` keys after the cursor (design 5.3)."""
    meta = _meta(conn)
    _require_format(meta)
    if meta.get("state") == STATE_READY:
        return {"state": STATE_READY, "processed": 0}
    cs, cn = meta.get("cursor_space"), meta.get("cursor_node_id")
    if cs is None or cn is None:
        rows = conn.execute(
            "SELECT space, node_id FROM doc_nodes ORDER BY space, node_id LIMIT ?", (batch,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT space, node_id FROM doc_nodes WHERE (space, node_id) > (?, ?) "
            "ORDER BY space, node_id LIMIT ?",
            (cs, cn, batch),
        ).fetchall()
    for space, node_id in rows:
        _reindex_or_raise(conn, row_to_node, space, node_id, meta)
    if rows:
        _set_meta(conn, cursor_space=rows[-1][0], cursor_node_id=rows[-1][1])
        return {"state": STATE_BACKFILL, "processed": len(rows)}
    # Design 5.3: the final step drains every pending key and switches to
    # ready in this one transaction. A failure rolls both back.
    drained = 0
    while True:
        keys = conn.execute(f"SELECT space, node_id FROM {PENDING} LIMIT ?", (batch,)).fetchall()
        if not keys:
            break
        for space, node_id in keys:
            _reindex_or_raise(conn, row_to_node, space, node_id, meta)
        drained += len(keys)
    _set_meta(conn, state=STATE_READY)
    return {"state": STATE_READY, "processed": 0, "pending_drained": drained}


def drop(conn: sqlite3.Connection) -> None:
    """Remove the triggers and every index object (design 5.5)."""
    for trig in TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    for table in (VINST, VROW, FTS, DOCS, STATS, PENDING, META):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


# ---------------------------------------------------------------------------
# search (design 4.5)
# ---------------------------------------------------------------------------


class NotReadyError(Exception):
    """The snapshot shows the index is not ``ready`` in this code's format."""


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def search(
    conn: sqlite3.Connection,
    row_to_node: RowToNode,
    question: str,
    *,
    pack_ids: list[str],
    spaces: list[str] | None,
    limit: int,
    per_pack: bool,
) -> tuple[Any, list[str]]:
    """Search inside the caller's read snapshot.

    Returns ``(hits, warnings)``. ``hits`` is a list, or a dict of lists keyed
    by pack when ``per_pack``. Raises ``NotReadyError`` when the snapshot is not a
    ready index of this format.
    """
    from opencrab.ontology.bm25 import _node_text, bm25_idf, bm25_score
    from opencrab.ontology.pack_provenance import scope_pack_id
    from opencrab.ontology.text_cues import tokenize

    empty: Any = {} if per_pack else []
    # Design 4.5: another process may have dropped the index after the entry
    # check. An absent meta table means "not ready", not "unavailable".
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (META,)
    ).fetchone() is None:
        raise NotReadyError
    if not is_ready(conn):
        raise NotReadyError
    warnings: list[str] = []
    n_pending = pending_count(conn)
    if n_pending:
        warnings.append(PENDING_WARNING.format(n=n_pending))
    pack_set = sorted({p for p in pack_ids if p})
    q_terms = [fts_term(t) for t in tokenize(question)]
    if not q_terms or not pack_set:
        return empty, warnings
    stats = conn.execute(f"SELECT doc_count, total_len FROM {STATS} WHERE id=1").fetchone()
    n_docs = int(stats[0]) if stats else 0
    if n_docs <= 0:
        return empty, warnings
    avgdl = int(stats[1]) / n_docs

    uniq = list(dict.fromkeys(q_terms))
    idf: dict[str, float] = {}
    dfs: dict[str, int] = {}
    cfs: dict[str, int] = {}
    for term, df, cnt in conn.execute(
        f"SELECT term, doc, cnt FROM {VROW} WHERE term IN ({_placeholders(len(uniq))})", uniq
    ):
        if df:
            idf[term] = bm25_idf(n_docs, int(df))
            dfs[term] = int(df)
            cfs[term] = int(cnt)

    def full_scored() -> list[tuple]:
        space_sql = ""
        space_params: list[str] = []
        if spaces:
            space_sql = f" AND d.space IN ({_placeholders(len(spaces))})"
            space_params = list(spaces)
        tf_by_doc: dict[int, dict[str, int]] = defaultdict(dict)
        info: dict[int, tuple[int, str, str, str, str]] = {}
        for term in idf:
            for doc_key, tf, dl, upd, space, node_id, pid in conn.execute(
                f"SELECT v.doc, COUNT(*), d.dl, d.updated_at, d.space, d.node_id, d.pack_id "
                f"FROM {VINST} v JOIN {DOCS} d ON d.doc_key = v.doc "
                f"WHERE v.term = ? AND d.pack_id IN ({_placeholders(len(pack_set))}){space_sql} "
                "GROUP BY v.doc",
                [term, *pack_set, *space_params],
            ):
                tf_by_doc[doc_key][term] = int(tf)
                info[doc_key] = (int(dl), upd, space, node_id, pid)

        scored = []
        for doc_key, tf_map in tf_by_doc.items():
            dl, upd, space, node_id, pid = info[doc_key]
            score = bm25_score(q_terms, tf_map, idf, dl, avgdl)
            if score > 0:
                scored.append((score, upd, space, node_id, pid))
        # (-score, updated_at DESC, space, node_id): stable sorts, last key first.
        scored.sort(key=lambda c: (c[2], c[3]))
        scored.sort(key=lambda c: c[1], reverse=True)
        scored.sort(key=lambda c: -c[0])
        return scored

    allowed = set(pack_set)

    def checked(
        cands: Iterator[tuple], want: int | None, same_pack: bool, strict: bool
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for score, _upd, space, node_id, pid in cands:
            if want is not None and len(out) >= want:
                break
            if conn.execute(
                f"SELECT 1 FROM {PENDING} WHERE space=? AND node_id=?", (space, node_id)
            ).fetchone() is not None:
                if strict:
                    raise _prune.PruneFallbackError("pending row")
                continue
            row = conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM doc_nodes WHERE space=? AND node_id=?",
                (space, node_id),
            ).fetchone()
            if row is None:
                if strict:
                    raise _prune.PruneFallbackError("missing row")
                continue
            node = row_to_node(row)
            cur_pid = scope_pack_id(node)
            if cur_pid is None or cur_pid not in allowed or (same_pack and cur_pid != pid):
                if strict:
                    raise _prune.PruneFallbackError("pack changed")
                continue
            if spaces and node.get("space") not in spaces:
                if strict:
                    raise _prune.PruneFallbackError("space changed")
                continue
            out.append({
                "node_id": node.get("node_id"),
                "space": node.get("space"),
                "node_type": node.get("node_type"),
                "score": round(score, 4),
                "properties": node.get("properties") or {},
                "text": _node_text(node),
                "pack_id": cur_pid,
            })
        return out

    def render(scored: list[tuple], strict: bool) -> Any:
        # BM25Index.search applies ``scores[:limit]``; a non-positive limit keeps
        # that slice meaning, so every candidate is checked first.
        want = limit if limit > 0 else None
        if not per_pack:
            hits = checked(iter(scored), want, same_pack=False, strict=strict)
            return hits if limit > 0 else hits[:limit]
        by_pack: dict[str, list[tuple]] = defaultdict(list)
        for cand in scored:
            by_pack[cand[4]].append(cand)
        out: dict[str, list[dict[str, Any]]] = {}
        for pid in pack_set:
            hits = checked(iter(by_pack.get(pid, ())), want, same_pack=True, strict=strict)
            hits = hits if limit > 0 else hits[:limit]
            if hits:
                out[pid] = hits
        return out

    # #567: exact top-k pruning when the snapshot has no pending row (design 6.1).
    if (
        not n_pending
        and idf
        and os.getenv("OPENCRAB_BM25_PRUNE", "1") != "0"
        and _prune.eligible(len(q_terms), sum(cfs.values()), per_pack, limit)
    ):
        try:
            pruned = _prune.select(
                conn, row_to_node,
                names={"fts": FTS, "vinst": VINST, "docs": DOCS},
                q_terms=q_terms, idf=idf, df=dfs, cf=cfs, avgdl=avgdl,
                pack_set=pack_set, spaces=spaces, limit=limit, per_pack=per_pack,
                max_text_chars=_decode_chars(_meta(conn).get("max_text_chars")),
                node_terms=node_terms, node_columns=_NODE_COLUMNS,
            )
            return render(pruned, strict=True), warnings
        except _prune.PruneFallbackError as exc:
            logger.warning("node BM25 pruned search fell back to the full scan: %s", exc)
    return render(full_scored(), strict=False), warnings


# ---------------------------------------------------------------------------
# status and verification (design 5.4)
# ---------------------------------------------------------------------------


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    meta = _meta(conn)
    stats = conn.execute(f"SELECT doc_count, total_len FROM {STATS} WHERE id=1").fetchone()
    return {
        "state": meta.get("state"),
        "format": meta.get("format"),
        "code_format": FORMAT,
        "max_text_chars": _decode_chars(meta.get("max_text_chars")),
        "cursor": [meta.get("cursor_space"), meta.get("cursor_node_id")],
        "pending": pending_count(conn),
        "doc_count": int(stats[0]) if stats else None,
        "total_len": int(stats[1]) if stats else None,
    }


_MASK = (1 << 128) - 1


def _term_hash(term: str) -> int:
    return int.from_bytes(hashlib.blake2b(term.encode("utf-8"), digest_size=16).digest(), "big")


def verify(conn: sqlite3.Connection, row_to_node: RowToNode) -> dict[str, Any]:
    """Compare the index with ``doc_nodes`` inside the caller's read snapshot."""
    from opencrab.ontology.pack_provenance import scope_pack_id

    meta = _meta(conn)
    max_text_chars = _decode_chars(meta.get("max_text_chars"))
    pending = {(s, n) for s, n in conn.execute(f"SELECT space, node_id FROM {PENDING}")}
    indexed: dict[tuple[str, str], tuple[int, str, int, str]] = {
        (s, n): (k, p, int(dl), u)
        for k, s, n, p, dl, u in conn.execute(
            f"SELECT doc_key, space, node_id, pack_id, dl, updated_at FROM {DOCS}"
        )
    }
    index_print: dict[int, int] = defaultdict(int)
    for term, doc in conn.execute(f"SELECT term, doc FROM {VINST}"):
        index_print[int(doc)] = (index_print[int(doc)] + _term_hash(term)) & _MASK

    missing = mismatched_meta = mismatched_terms = 0
    seen: set[tuple[str, str]] = set()
    total_dl = 0
    last: tuple[str, str] | None = None
    while True:
        if last is None:
            rows = conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM doc_nodes ORDER BY space, node_id LIMIT ?",
                (_SCAN_BATCH,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM doc_nodes WHERE (space, node_id) > (?, ?) "
                "ORDER BY space, node_id LIMIT ?",
                (*last, _SCAN_BATCH),
            ).fetchall()
        if not rows:
            break
        for row in rows:
            key = (row["space"], row["node_id"])
            if key in pending:
                continue
            seen.add(key)
            entry = indexed.get(key)
            if entry is None:
                missing += 1
                continue
            doc_key, pid, dl, upd = entry
            node = row_to_node(row)
            terms = node_terms(node, max_text_chars)
            updated = row["updated_at"]
            if (pid != (scope_pack_id(node) or "") or dl != len(terms)
                    or upd != ("" if updated is None else str(updated))):
                mismatched_meta += 1
            fp = 0
            for term in terms:
                fp = (fp + _term_hash(term)) & _MASK
            if fp != index_print.get(doc_key, 0):
                mismatched_terms += 1
        last = (rows[-1]["space"], rows[-1]["node_id"])
    orphans = sum(1 for key in indexed if key not in seen and key not in pending)
    for key, entry in indexed.items():
        total_dl += entry[2]
    stats = conn.execute(f"SELECT doc_count, total_len FROM {STATS} WHERE id=1").fetchone()
    stats_ok = stats is not None and int(stats[0]) == len(indexed) and int(stats[1]) == total_dl
    ok = not (missing or orphans or mismatched_meta or mismatched_terms) and stats_ok
    return {
        "ok": ok,
        "state": meta.get("state"),
        "pending": len(pending),
        "checked": len(seen),
        "missing": missing,
        "orphans": orphans,
        "mismatched_meta": mismatched_meta,
        "mismatched_terms": mismatched_terms,
        "stats_ok": stats_ok,
    }
