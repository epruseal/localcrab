"""SQLite source FTS (``doc_sources_fts``) row lookup by source_id (#642).

FTS5 keeps the declared columns of a table with regular content in its
content table ``doc_sources_fts_content``. In that table ``id`` is the FTS
rowid and ``c0`` is the first declared column, ``source_id``. A condition on
the ``source_id`` column of the FTS table reads every FTS row. The statements
here find the rows through an index on ``c0`` and delete them by rowid.

An orphan row is an FTS row without a ``doc_sources`` row. The orphan
condition makes the same comparison as the keyword search join, so an orphan
is an FTS row that the join cannot match. Only the operator command
``packs source-fts-orphans --apply`` deletes orphans.

See docs/issue-642-source-fts-lookup.md for the decisions and the tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

FTS = "doc_sources_fts"
CONTENT = "doc_sources_fts_content"
INDEX = "idx_doc_sources_fts_source_id"
INDEX_DDL = f"CREATE INDEX IF NOT EXISTS {INDEX} ON {CONTENT}(c0)"

_DECLARED_COLUMNS = ["source_id", "text"]
_CONTENT_COLUMNS = ["id", "c0", "c1"]

# The keyword search joins "doc_sources s ON s.source_id = f.source_id", and
# FTS5 returns c0 for f.source_id. This condition makes the same comparison.
ORPHAN_CONDITION = "NOT EXISTS (SELECT 1 FROM doc_sources AS s WHERE s.source_id = c.c0)"


def layout_ok(conn: Any) -> bool:
    """True when the FTS table and its content table have the layout this store makes."""
    declared = [row[1] for row in conn.execute(f"PRAGMA table_info({FTS})")]
    content = [row[1] for row in conn.execute(f"PRAGMA table_info({CONTENT})")]
    return declared == _DECLARED_COLUMNS and content == _CONTENT_COLUMNS


def require_layout(conn: Any) -> None:
    """Raise when ``c0`` is not the ``source_id`` column of the FTS table."""
    if not layout_ok(conn):
        raise RuntimeError(f"{FTS} does not have the layout that LocalSQLDocStore creates")


def delete_sql(source_id_condition: str) -> str:
    """Return the FTS delete for the rows whose source_id meets the condition.

    ``source_id_condition`` follows ``c0``, for example ``= :source_id`` or
    ``IN (:sid_0, :sid_1)``. ``IN`` keeps the old behaviour: every FTS row of
    the source goes, also a duplicate row.
    """
    return (
        f"DELETE FROM {FTS} WHERE rowid IN "
        f"(SELECT id FROM {CONTENT} WHERE c0 {source_id_condition})"
    )


def report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts of the source FTS rows. All values come from one read snapshot.

    The report holds counts, rowid bounds and plan text only. It holds no
    body text and no source_id values.
    """
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        ok = layout_ok(conn)
        out: dict[str, Any] = {
            "layout_ok": ok,
            "index_present": bool(conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
            ).fetchone()[0]),
            "doc_sources": conn.execute("SELECT count(*) FROM doc_sources").fetchone()[0],
        }
        if not ok:
            return out
        orphans = conn.execute(
            f"SELECT count(*), min(c.id), max(c.id) FROM {CONTENT} AS c WHERE {ORPHAN_CONDITION}"
        ).fetchone()
        out.update({
            "fts_rows": conn.execute(f"SELECT count(*) FROM {CONTENT}").fetchone()[0],
            "orphan_rows": orphans[0],
            "orphan_rowid_min": orphans[1],
            "orphan_rowid_max": orphans[2],
            # A LEFT JOIN lets SQLite build a temporary index when the lookup
            # index does not exist yet, so a legacy DB is not read N x M times.
            "sources_without_fts": conn.execute(
                f"SELECT count(*) FROM doc_sources AS s LEFT JOIN {CONTENT} AS c "
                "ON s.source_id = c.c0 WHERE c.id IS NULL"
            ).fetchone()[0],
            "duplicate_source_ids": conn.execute(
                f"SELECT count(*) FROM (SELECT c0 FROM {CONTENT} WHERE c0 IS NOT NULL "
                "GROUP BY c0 HAVING count(*) > 1)"
            ).fetchone()[0],
            "delete_plan": [
                row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + delete_sql("= ?"), ("",))
            ],
        })
        return out
    finally:
        if own:
            conn.commit()


def report_consistent(summary: dict[str, Any]) -> bool:
    """True when every source has exactly one FTS row and no FTS row is an orphan."""
    return bool(
        summary.get("layout_ok")
        and summary.get("index_present")
        and summary.get("orphan_rows") == 0
        and summary.get("sources_without_fts") == 0
        and summary.get("duplicate_source_ids") == 0
        and summary.get("fts_rows") == summary.get("doc_sources")
    )


def read_only_report(db_path: str) -> dict[str, Any]:
    """Report on a doc store file through a read-only connection.

    The connection opens the file with ``mode=ro``. It does not run the store
    bootstrap, so it does not create the lookup index.
    """
    conn = sqlite3.connect(f"{Path(db_path).absolute().as_uri()}?mode=ro", uri=True)
    try:
        return report(conn)
    finally:
        conn.close()


def prune_orphans(conn: sqlite3.Connection) -> dict[str, Any]:
    """Delete the orphan FTS rows. The caller holds a write transaction.

    The orphan set comes from this transaction. Returns the number of deleted
    rows and their rowid bounds.
    """
    count, low, high = conn.execute(
        f"SELECT count(*), min(c.id), max(c.id) FROM {CONTENT} AS c WHERE {ORPHAN_CONDITION}"
    ).fetchone()
    deleted = conn.execute(
        f"DELETE FROM {FTS} WHERE rowid IN "
        f"(SELECT c.id FROM {CONTENT} AS c WHERE {ORPHAN_CONDITION})"
    ).rowcount
    if deleted != count:
        # The write transaction holds the database, so the two counts agree.
        # Raise, and so roll back, if they do not.
        raise RuntimeError(f"orphan delete removed {deleted} rows, expected {count}")
    return {"deleted": deleted, "rowid_min": low, "rowid_max": high}
