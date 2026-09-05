"""Single source of truth for the SQL-store tables the two migration scripts copy.

Before this module the table list was hard-coded in three places
(``migrate_sqlite_to_pg.py::migrate_sql``, ``migrate_to_local.py::preflight`` and
``migrate_to_local.py::migrate_sql``) and drifted silently -- #144 added
``users``/``api_tokens``/``packs`` to :class:`~opencrab.stores.sql_store.SQLStore`
and none of the three lists learned about them, so a migration would have dropped
every user and every issued token while reporting success. #151 collapses the
three lists into :data:`SQL_TABLE_SPECS` and adds a test that pins it against the
tables ``SQLStore`` actually creates (:func:`sqlstore_owned_tables`), so the next
table added there breaks CI instead of losing data.

The *columns* to copy are deliberately NOT declared here. They are derived at
run time from both catalogues (``PRAGMA table_info`` / SQLAlchemy ``inspect``) so
that a column added to both dialects is copied without touching this file, while
a column present on only one side is a hard error rather than a silent partial
copy. :attr:`SqlTableSpec.required_columns` is the contract *minimum* used to
reject an outdated table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class MigrationError(Exception):
    """An error raised by the migration scripts themselves (not by a driver).

    CONTRACT: the message must be assembled from identifiers (table, column,
    constraint names), natural-key values and static guidance text only -- never
    from row values. :func:`safe_error_text` passes this class through verbatim
    precisely because of that contract; every other exception is reduced to
    identifiers, because driver and server messages can and do echo row values
    (PostgreSQL's ``DETAIL: Key (token_hash)=(...)``, its ``LINE 1: ...`` context
    line, sqlite3's "Could not decode to UTF-8 column 'x' with text '...'").

    The single exception to "no values" is the corrupted-boolean report, which
    needs the offending value to be actionable -- and prints it only when it is
    an ``int``/``bool`` (see :func:`describe_bad_value`).
    """


class TargetOnlyAuthError(MigrationError):
    """The target holds credentials the source does not.

    Its own class so the forward script can map it to the same exit code as
    #144's guard -- both refuse before any work happens, and both mean "the
    operator has to decide", not "the migration broke".
    """


# Credentials only. `packs` is ownership metadata rather than a credential, and
# a local install legitimately has its own packs, so checking it would fire on
# the ordinary path. `rebac_policies` grants do widen access, but that table was
# migrated long before this change, so a target-only grant surviving is a
# pre-existing defect rather than one this work makes newly reachable.
AUTH_CREDENTIAL_TABLES: tuple[str, ...] = ("users", "api_tokens")


@dataclass(frozen=True)
class SqlTableSpec:
    """One SQL-store table, in the order it must be copied.

    ``conflict_key`` is the natural key used for ``ON CONFLICT`` upserts, or
    ``None`` for the two tables that have none (history rows -- re-running
    duplicates them; that predates #151 and is tracked separately).

    ``exclude_columns`` holds surrogate keys the target generates itself.
    Copying an explicit ``id`` into a PostgreSQL ``SERIAL`` does not advance its
    sequence, so the first application write after the migration collides on the
    primary key. Both scripts already excluded ``id``; this preserves that.
    """

    name: str
    conflict_key: tuple[str, ...] | None
    required_columns: frozenset[str]
    exclude_columns: frozenset[str] = frozenset()


# Ordered: `users` first because api_tokens.user_id and packs.owner_id reference
# it. Both PostgreSQL and SQLite enforce these FKs now (#181: migrate_to_local.py
# turns PRAGMA foreign_keys=ON on its SQLite write engine too), so an orphan
# owner_id row here is rejected on insert, not silently accepted.
SQL_TABLE_SPECS: tuple[SqlTableSpec, ...] = (
    SqlTableSpec(
        "users",
        ("user_id",),
        frozenset({"user_id", "display_name", "is_local", "disabled", "created_at"}),
    ),
    SqlTableSpec(
        "api_tokens",
        ("token_id",),
        frozenset(
            {
                "token_id",
                "user_id",
                "token_hash",
                "name",
                "created_at",
                "last_used_at",
                "revoked_at",
            }
        ),
    ),
    SqlTableSpec(
        "packs",
        ("pack_id",),
        frozenset(
            {
                "pack_id",
                "owner_id",
                "visibility",
                "title",
                "description",
                "forked_from",
                "created_at",
                "updated_at",
            }
        ),
    ),
    SqlTableSpec(
        "ontology_nodes",
        ("space", "node_id"),
        frozenset({"space", "node_type", "node_id", "created_at", "updated_at"}),
        frozenset({"id"}),
    ),
    SqlTableSpec(
        "ontology_edges",
        ("from_space", "from_id", "relation", "to_space", "to_id"),
        frozenset({"from_space", "from_id", "relation", "to_space", "to_id", "created_at"}),
        frozenset({"id"}),
    ),
    SqlTableSpec(
        "impact_records",
        None,
        frozenset({"node_id", "change_type", "impact_json", "analyzed_at"}),
        frozenset({"id"}),
    ),
    SqlTableSpec(
        "lever_simulations",
        None,
        frozenset({"lever_id", "direction", "magnitude", "results", "simulated_at"}),
        frozenset({"id"}),
    ),
    SqlTableSpec(
        "rebac_policies",
        ("subject_id", "permission", "resource_id"),
        frozenset({"subject_id", "permission", "resource_id", "granted", "created_at"}),
        frozenset({"id"}),
    ),
)

SPEC_BY_NAME: dict[str, SqlTableSpec] = {s.name: s for s in SQL_TABLE_SPECS}
MIGRATED_TABLES: frozenset[str] = frozenset(SPEC_BY_NAME)

# Static remedy text keyed by the constraint name a failure reports. The
# sanitizer can surface a constraint name but cannot invent guidance.
CONSTRAINT_REMEDIES: dict[str, str] = {
    "idx_users_single_local": (
        "타깃에 이미 다른 is_local 사용자가 있다. "
        "타깃의 로컬 사용자를 정리한 뒤 재실행하라."
    ),
}


def sqlstore_owned_tables() -> frozenset[str]:
    """Tables ``SQLStore``'s constructor actually creates, measured not declared.

    ``SQLStore.table_counts`` hard-codes its own list, so it cannot serve as the
    drift reference -- that hard-coding is exactly what drifts. Reading
    ``sqlite_master`` off a throwaway in-memory store is the only way to observe
    the real set, which is why this function reaches for the private ``_engine``
    (the one deliberate exception to #151 removing private-attribute access).
    """
    from sqlalchemy import text

    from opencrab.stores.sql_store import SQLStore

    store = SQLStore(url="sqlite:///:memory:")
    if not store.available:
        # The constructor swallows failures and only records availability, so an
        # empty result here would silently disarm the guard built on top of it.
        raise MigrationError("SQLStore(sqlite:///:memory:) unavailable -- cannot enumerate tables")
    try:
        with store._engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
            # AUTOINCREMENT makes SQLite create sqlite_sequence alongside.
            return frozenset(r[0] for r in rows if not r[0].startswith("sqlite_"))
    finally:
        store._engine.dispose()


def unmigrated_tables(source_tables: Any) -> list[str]:
    """SQL-store tables present in the source that no spec covers.

    #144 added a fail-closed guard against exactly this. #151 migrates the three
    auth tables, so the set is empty today -- but it is *derived*, not a literal,
    so a ninth SQLStore table trips it at run time instead of being dropped.
    Tables that merely share opencrab.db without being SQLStore's (node_aliases,
    approval_queue, workflow_runs, ...) fall outside the intersection and do not
    false-positive here.
    """
    return sorted((sqlstore_owned_tables() & set(source_tables)) - MIGRATED_TABLES)


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------


def resolve_columns(spec: SqlTableSpec, src_columns: list[str], dst_columns: list[str]) -> list[str]:
    """Columns to copy, in source declaration order, or raise :class:`MigrationError`.

    The rule is deliberately asymmetric: a column only the target has is filled
    by the target's default (old SQLite into a newer PostgreSQL), but a column
    only the source has would be dropped, and a silent partial copy is the exact
    failure #151 exists to prevent.
    """
    src = [c for c in src_columns if c not in spec.exclude_columns]
    dst = [c for c in dst_columns if c not in spec.exclude_columns]
    src_set, dst_set = set(src), set(dst)

    missing_src = spec.required_columns - src_set
    if missing_src:
        raise MigrationError(
            f"source table {spec.name}: required column(s) {sorted(missing_src)} missing"
        )
    missing_dst = spec.required_columns - dst_set
    if missing_dst:
        raise MigrationError(
            f"target table {spec.name}: required column(s) {sorted(missing_dst)} missing "
            "-- upgrade the target schema first"
        )
    source_only = src_set - dst_set
    if source_only:
        raise MigrationError(
            f"table {spec.name}: column(s) {sorted(source_only)} exist only in the source "
            "-- refusing a partial copy that would drop them"
        )
    return [c for c in src if c in dst_set]


def sqlite_columns(conn: Any, table: str) -> list[str]:
    """Column names of a SQLite table in declaration order (cid order)."""
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def pg_typed_columns(engine: Any, table: str) -> tuple[list[str], set[str], set[str]]:
    """``(columns, boolean_columns, timestamp_columns)`` for a table on ``engine``.

    The PostgreSQL side is the authority on which columns are boolean/timestamp:
    SQLite has neither type, storing them as INTEGER and TEXT. Deriving from the
    catalogue rather than a literal column list means a boolean added later is
    handled without editing this file -- the previous code special-cased the
    single name ``granted``.
    """
    from sqlalchemy import Boolean, DateTime, inspect

    cols = inspect(engine).get_columns(table)
    names = [c["name"] for c in cols]
    booleans = {c["name"] for c in cols if isinstance(c["type"], Boolean)}
    timestamps = {c["name"] for c in cols if isinstance(c["type"], DateTime)}
    return names, booleans, timestamps


# ---------------------------------------------------------------------------
# Value conversion -- strict, because silent coercion is the bug
# ---------------------------------------------------------------------------

_TRUTHY = (0, 1, False, True)


def describe_bad_value(value: Any) -> str:
    """Render an offending value for an error message without leaking a secret.

    SQLite is dynamically typed, so a column the PostgreSQL schema calls BOOLEAN
    can hold an arbitrary string. Printing a truncated repr would leak the head
    of it, so only int/bool -- the values that are actually diagnostic here --
    are shown literally.
    """
    if isinstance(value, bool | int):
        return f"invalid value {value}"
    return f"<{type(value).__name__}>"


def to_pg_bool(value: Any, *, table: str, column: str, key: str) -> Any:
    """SQLite INTEGER -> PostgreSQL BOOLEAN, refusing anything outside 0/1.

    The previous code called ``bool()``, so a corrupted ``2`` became ``True``.
    Only ``users`` gained a CHECK constraint for this; ``rebac_policies.granted``
    still has none, so a real database can hold such a row today.
    """
    if value is None:
        return None
    if isinstance(value, bool | int) and value in _TRUTHY:
        return bool(value)
    raise MigrationError(
        f"{table}.{column} (key={key}): boolean column holds a non-0/1 value "
        f"-- {describe_bad_value(value)}"
    )


def to_sqlite_bool(value: Any, *, table: str, column: str, key: str) -> Any:
    """PostgreSQL BOOLEAN -> SQLite INTEGER 0/1."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in _TRUTHY:
        return int(bool(value))
    raise MigrationError(
        f"{table}.{column} (key={key}): boolean column holds a non-boolean value "
        f"-- {describe_bad_value(value)}"
    )


_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def check_sqlite_timestamp(value: Any, *, table: str, column: str, key: str) -> None:
    """Reject text that PostgreSQL would refuse as a TIMESTAMPTZ literal.

    SQLite's dynamic typing lets a TEXT timestamp column hold anything, and the
    forward migration binds that text straight into TIMESTAMPTZ. Without this
    check PostgreSQL raises ``invalid input syntax for type timestamp with time
    zone: "<the value>"`` -- an error whose *primary* message echoes the cell,
    mid-run, after earlier tables have already been committed.

    ``fromisoformat`` is deliberately permissive (it accepts a bare date and
    non-UTC offsets); the point is to reject unparseable text, not to enforce a
    single spelling.
    """
    if value is None:
        return
    if isinstance(value, datetime):
        return
    if isinstance(value, str):
        for fmt in _TS_FORMATS:
            try:
                datetime.strptime(value, fmt)
                return
            except ValueError:
                pass
        try:
            datetime.fromisoformat(value)
            return
        except ValueError:
            pass
    raise MigrationError(
        f"{table}.{column} (key={key}): timestamp column holds text PostgreSQL cannot parse"
    )


def to_sqlite_timestamp(value: Any) -> Any:
    """PostgreSQL datetime -> SQLite's own timestamp text, in UTC.

    Binding the tz-aware datetime psycopg2 returns straight into a TEXT column
    yields ``'2026-08-18 12:34:56+00:00'``, which is not what ``datetime('now')``
    and the DDL defaults write, so a round trip did not come back byte-identical.
    Serialising to SQLite's own spelling makes SQLite -> PG -> SQLite exact for
    canonical values; legacy ``+00:00`` text is normalised to it (same instant).
    """
    if not isinstance(value, datetime):
        return value
    dt = value.astimezone(UTC) if value.tzinfo is not None else value
    if dt.microsecond:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Operator-facing error text
# ---------------------------------------------------------------------------

_SECRET_FREE_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def safe_error_text(exc: BaseException, *, table: str | None = None, key: str | None = None) -> str:
    """Describe a failure using identifiers only -- never driver message text.

    Three narrower rules were tried and each leaked. ``hide_parameters=True``
    still let PostgreSQL's ``DETAIL: Key (token_hash)=(...)`` through; dropping
    DETAIL/HINT/CONTEXT lines still let the ``LINE 1: ...`` context line through
    (21 characters of a token hash survived); keeping only the first line still
    leaked, because a syntax error quotes the offending literal *in* that first
    line and psycopg2 interpolates values client-side. Judging whether a message
    is safe does not work, so nothing free-form is emitted at all: the class
    name, psycopg2's ``diag`` identifiers, and the caller's context.

    ``key`` must be a declared ``conflict_key`` value (a user_id/token_id/...),
    never ``token_hash``.

    Two limits worth knowing: a constraint whose *name* was derived from data
    would have that name surfaced, and a failed ``executemany`` cannot say which
    row it was, so ``key`` is absent there.
    """
    if isinstance(exc, MigrationError):
        # Provenance, not content: this class's message is contractually built
        # from identifiers, so it is the one thing that passes through intact.
        return str(exc)

    orig = getattr(exc, "orig", None) or exc
    parts = [type(orig).__name__]
    diag = getattr(orig, "diag", None)
    if diag is not None:
        for label, value in (
            ("", getattr(diag, "sqlstate", None)),
            ("constraint=", getattr(diag, "constraint_name", None)),
            ("pg_table=", getattr(diag, "table_name", None)),
            ("pg_column=", getattr(diag, "column_name", None)),
        ):
            if value:
                parts.append(f"[{value}]" if not label else f"{label}{value}")
        remedy = CONSTRAINT_REMEDIES.get(getattr(diag, "constraint_name", None) or "")
        if remedy:
            parts.append(f"-- {remedy}")
    if table:
        parts.append(f"table={table}")
    if key:
        parts.append(f"key={key}")
    return " ".join(parts)


def local_identity(sqlite_conn: Any, existing_tables: Any) -> tuple[str | None, set[str]]:
    """The local user id and its token ids in a local-mode database.

    Local mode needs exactly one ``is_local`` user to bind stdio/CLI to, so that
    row is this installation's own identity rather than a credential the source
    failed to account for. ``opencrab init`` creates it together with a token,
    which is why both are recognised here.

    Compared with ``= 1``, not truthiness: a database created before the CHECK
    constraint can hold ``is_local = 2``, and ``verify_token`` compares against
    1 exactly, so such a row is not the local identity and must not be treated
    as one.
    """
    if "users" not in existing_tables:
        # A database predating those tables has no local identity to recognise.
        return None, set()
    rows = sqlite_conn.execute("SELECT user_id FROM users WHERE is_local = 1").fetchall()
    if len(rows) > 1:
        # Unreachable through SQLStore, whose DDL creates idx_users_single_local
        # alongside the table -- kept for a database built without that index.
        raise TargetOnlyAuthError(
            f"target has {len(rows)} is_local users; expected at most one -- "
            "resolve which one is this installation before migrating"
        )
    if not rows:
        return None, set()
    user_id = rows[0][0]
    if "api_tokens" not in existing_tables:
        return user_id, set()
    tokens = sqlite_conn.execute(
        "SELECT token_id FROM api_tokens WHERE user_id = ?", (user_id,)
    ).fetchall()
    return user_id, {r[0] for r in tokens}


def target_only_report(target_only: dict[str, list[str]]) -> str:
    """Abort text for credentials present only in the target.

    Natural keys only -- ``token_hash`` is not a conflict_key of any table, so
    nothing secret reaches this message (see :func:`safe_error_text`).
    """
    parts = [
        f"{table}: {len(keys)} row(s) e.g. {sorted(keys)[:3]}"
        for table, keys in sorted(target_only.items())
        if keys
    ]
    return (
        "target holds credentials the source does not -- completing would leave "
        "them working while the source does not know about them (" + "; ".join(parts) + "). "
        "Remove the listed rows from the target and re-run, or pass "
        "--allow-target-only-auth to keep them."
    )
