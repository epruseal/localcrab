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

import re
import sqlite3
from pathlib import Path
from typing import Any


class DerivedRowProtectionError(RuntimeError):
    """단일 모드의 파생 행 보호를 통과하지 못했다."""


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


_PROTECTED_VOCAB = "source_fts_protection_vinst"
_VOCAB_ATOM = r'''(?:[A-Za-z_][A-Za-z_0-9]*|"(?:[^"]|"")*"|'(?:[^']|'')*'|`(?:[^`]|``)*`|\[[^\]]*\])'''
_VOCAB_DECLARATION = re.compile(
    rf"CREATE\s+VIRTUAL\s+TABLE\s+({_VOCAB_ATOM})\s+"
    rf"USING\s+({_VOCAB_ATOM})\s*\(\s*({_VOCAB_ATOM})\s*,\s*"
    rf"({_VOCAB_ATOM})\s*,\s*({_VOCAB_ATOM})\s*\)\s*;?\s*",
    re.IGNORECASE | re.ASCII,
)


def _vocab_atom(token: str) -> str:
    if token[0] == "[":
        return token[1:-1]
    if token[0] in "\"'`":
        quote = token[0]
        return token[1:-1].replace(quote * 2, quote)
    return token


def require_protection_vocab(conn: sqlite3.Connection, *, create: bool = False) -> str:
    """main 이름 충돌을 피하고 고정 TEMP vocab 선언을 검사한다."""
    from opencrab.stores._single_file import ascii_identifier

    main_names = {ascii_identifier(row[0]) for row in conn.execute(
        "SELECT name FROM main.sqlite_master"
    )}
    name = _PROTECTED_VOCAB
    suffix = 0
    while ascii_identifier(name) in main_names:
        suffix += 1
        name = f"{_PROTECTED_VOCAB}_{suffix}"
    rows = conn.execute("SELECT name, type, sql FROM temp.sqlite_master").fetchall()
    row = next((row for row in rows if ascii_identifier(row[0]) == ascii_identifier(name)), None)
    if row is None and create:
        conn.execute(
            f"CREATE VIRTUAL TABLE temp.{name} USING fts5vocab(main, '{FTS}', 'instance')"
        )
        rows = conn.execute("SELECT name, type, sql FROM temp.sqlite_master").fetchall()
        row = next((row for row in rows if ascii_identifier(row[0]) == ascii_identifier(name)), None)
        if row is None:
            raise DerivedRowProtectionError("source FTS TEMP vocabulary definition missing")
    if row is None:
        return f"temp.{name}"
    actual_name, kind, declaration = row
    match = _VOCAB_DECLARATION.fullmatch(declaration or "")
    expected = (name, "fts5vocab", "main", FTS, "instance")
    if kind != "table" or match is None or tuple(
        ascii_identifier(_vocab_atom(token)) for token in match.groups()
    ) != tuple(ascii_identifier(token) for token in expected):
        raise DerivedRowProtectionError("source FTS TEMP vocabulary definition differs")
    quoted_name = '"' + actual_name.replace('"', '""') + '"'
    columns = [entry[1] for entry in conn.execute(f"PRAGMA temp.table_info({quoted_name})")]
    if columns != ["term", "doc", "col", "offset"]:
        raise DerivedRowProtectionError("source FTS TEMP vocabulary layout differs")
    return f"temp.{actual_name}"


def _protected_vocab(conn: sqlite3.Connection) -> str:
    """파생 쓰기에 필요한 vocab만 생성하고 기존 선언을 다시 검사한다."""
    return require_protection_vocab(conn, create=True)


def _rowid_occupied(conn: sqlite3.Connection, vocab: str, rowid: int) -> bool:
    return any(conn.execute(sql, (rowid,)).fetchone() is not None for sql in (
        f"SELECT 1 FROM main.{CONTENT} WHERE id=?",
        f"SELECT 1 FROM main.{FTS}_docsize WHERE id=?",
        f"SELECT 1 FROM {vocab} WHERE doc=? LIMIT 1",
    ))


def delete_protected(conn: sqlite3.Connection, source_id: str) -> None:
    """대상의 모든 FTS 행을 지운 뒤 대상 rowid의 잔여를 거부한다."""
    vocab = _protected_vocab(conn)
    rowids = [row[0] for row in conn.execute(
        f"SELECT id FROM main.{CONTENT} WHERE c0=?", (source_id,)
    )]
    for rowid in rowids:
        conn.execute(f"DELETE FROM main.{FTS} WHERE rowid=?", (rowid,))
        if _rowid_occupied(conn, vocab, rowid):
            raise DerivedRowProtectionError(f"source FTS residue after delete: {source_id!r}")


def insert_protected(conn: sqlite3.Connection, source_id: str, text: str) -> None:
    """실제 세 FTS 저장 영역에서 비어 있는 양의 rowid만 쓴다."""
    vocab = _protected_vocab(conn)
    highest = max(0, *(conn.execute(sql).fetchone()[0] or 0 for sql in (
        f"SELECT max(id) FROM main.{CONTENT}",
        f"SELECT max(id) FROM main.{FTS}_docsize",
    )))
    rowid = highest + 1 if highest < 9223372036854775807 else 1
    # vocab만 남은 구간은 순서대로 검사한다. 큰 손상 구간은 별도 정비가 필요하다.
    while _rowid_occupied(conn, vocab, rowid):
        rowid = rowid + 1 if rowid < 9223372036854775807 else 1
        if rowid == (highest + 1 if highest < 9223372036854775807 else 1):
            raise DerivedRowProtectionError(f"source FTS rowid exhausted: {source_id!r}")
    conn.execute(
        f"INSERT INTO main.{FTS}(rowid, source_id, text) VALUES (?, ?, ?)",
        (rowid, source_id, text),
    )


def report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts of the source FTS rows. All values come from one read snapshot.

    The report holds flags, counts, rowid bounds and plan text. It holds no
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
