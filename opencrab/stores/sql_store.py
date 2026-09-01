"""
SQLAlchemy relational store adapter (PostgreSQL / SQLite).

Manages structured ontology metadata: impact records, ReBAC policy
assignments, lever simulations, and configuration tables.
Falls back to SQLite for development if Postgres is unavailable.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLAlchemy table declarations (metadata-only, not using ORM declarative)
# ---------------------------------------------------------------------------

_TABLES_SQL = [
    # Ontology nodes registry (lightweight, structural)
    """
    CREATE TABLE IF NOT EXISTS ontology_nodes (
        id          SERIAL PRIMARY KEY,
        space       VARCHAR(64)  NOT NULL,
        node_type   VARCHAR(64)  NOT NULL,
        node_id     VARCHAR(256) NOT NULL,
        created_at  TIMESTAMPTZ  DEFAULT NOW(),
        updated_at  TIMESTAMPTZ  DEFAULT NOW(),
        UNIQUE (space, node_id)
    )
    """,
    # Ontology edges registry
    """
    CREATE TABLE IF NOT EXISTS ontology_edges (
        id          SERIAL PRIMARY KEY,
        from_space  VARCHAR(64)  NOT NULL,
        from_id     VARCHAR(256) NOT NULL,
        relation    VARCHAR(64)  NOT NULL,
        to_space    VARCHAR(64)  NOT NULL,
        to_id       VARCHAR(256) NOT NULL,
        created_at  TIMESTAMPTZ  DEFAULT NOW(),
        UNIQUE (from_space, from_id, relation, to_space, to_id)
    )
    """,
    # Impact analysis records
    """
    CREATE TABLE IF NOT EXISTS impact_records (
        id          SERIAL PRIMARY KEY,
        node_id     VARCHAR(256) NOT NULL,
        change_type VARCHAR(64)  NOT NULL,
        impact_json TEXT         NOT NULL,
        analyzed_at TIMESTAMPTZ  DEFAULT NOW()
    )
    """,
    # Lever simulation records
    """
    CREATE TABLE IF NOT EXISTS lever_simulations (
        id          SERIAL PRIMARY KEY,
        lever_id    VARCHAR(256) NOT NULL,
        direction   VARCHAR(32)  NOT NULL,
        magnitude   FLOAT        NOT NULL,
        results     TEXT         NOT NULL,
        simulated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    # ReBAC policy table
    """
    CREATE TABLE IF NOT EXISTS rebac_policies (
        id           SERIAL PRIMARY KEY,
        subject_id   VARCHAR(256) NOT NULL,
        permission   VARCHAR(64)  NOT NULL,
        resource_id  VARCHAR(256) NOT NULL,
        granted      BOOLEAN      NOT NULL DEFAULT TRUE,
        created_at   TIMESTAMPTZ  DEFAULT NOW(),
        UNIQUE (subject_id, permission, resource_id)
    )
    """,
    # Users (#144: verified-principal registry -- see opencrab/auth.py)
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id      VARCHAR(64)  PRIMARY KEY,
        display_name VARCHAR(256) NOT NULL,
        is_local     BOOLEAN      NOT NULL DEFAULT FALSE,
        disabled     BOOLEAN      NOT NULL DEFAULT FALSE,
        created_at   TIMESTAMPTZ  DEFAULT NOW()
    )
    """,
    # At most one is_local=TRUE user -- stdio/CLI needs exactly one to bind to.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_local
    ON users (is_local) WHERE is_local = TRUE
    """,
    # API tokens. token_hash is the sha256 hex of the presented secret --
    # the plaintext is never persisted (see #144 acceptance criteria).
    """
    CREATE TABLE IF NOT EXISTS api_tokens (
        token_id     VARCHAR(64)  PRIMARY KEY,
        user_id      VARCHAR(64)  NOT NULL REFERENCES users (user_id),
        token_hash   VARCHAR(64)  NOT NULL UNIQUE,
        name         VARCHAR(256),
        created_at   TIMESTAMPTZ  DEFAULT NOW(),
        last_used_at TIMESTAMPTZ,
        revoked_at   TIMESTAMPTZ
    )
    """,
    # Pack ownership/visibility registry (#143 sec. "설계의 축"). Created here
    # so #146 can start without a DDL migration; not read/written by anything
    # yet in this PR.
    """
    CREATE TABLE IF NOT EXISTS packs (
        pack_id      VARCHAR(256) PRIMARY KEY,
        owner_id     VARCHAR(64)  NOT NULL REFERENCES users (user_id),
        visibility   VARCHAR(32)  NOT NULL DEFAULT 'private',
        title        VARCHAR(512),
        description  TEXT,
        forked_from  VARCHAR(256),
        is_default   BOOLEAN      NOT NULL DEFAULT FALSE,
        status       VARCHAR(32)  NOT NULL DEFAULT 'ready'
                     CHECK (status IN ('creating', 'partial', 'ready')),
        created_at   TIMESTAMPTZ  DEFAULT NOW(),
        updated_at   TIMESTAMPTZ  DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_packs_owner ON packs (owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_packs_visibility ON packs (visibility)",
]

# SQLite-compatible equivalents (no SERIAL, no TIMESTAMPTZ)
_TABLES_SQL_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS ontology_nodes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        space       TEXT NOT NULL,
        node_type   TEXT NOT NULL,
        node_id     TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now')),
        updated_at  TEXT DEFAULT (datetime('now')),
        UNIQUE (space, node_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ontology_edges (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_space  TEXT NOT NULL,
        from_id     TEXT NOT NULL,
        relation    TEXT NOT NULL,
        to_space    TEXT NOT NULL,
        to_id       TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now')),
        UNIQUE (from_space, from_id, relation, to_space, to_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS impact_records (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id     TEXT NOT NULL,
        change_type TEXT NOT NULL,
        impact_json TEXT NOT NULL,
        analyzed_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lever_simulations (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        lever_id     TEXT NOT NULL,
        direction    TEXT NOT NULL,
        magnitude    REAL NOT NULL,
        results      TEXT NOT NULL,
        simulated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rebac_policies (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id   TEXT NOT NULL,
        permission   TEXT NOT NULL,
        resource_id  TEXT NOT NULL,
        granted      INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT DEFAULT (datetime('now')),
        UNIQUE (subject_id, permission, resource_id)
    )
    """,
    # Users (#144: verified-principal registry -- see opencrab/auth.py).
    # is_local/disabled get an explicit value-domain CHECK on SQLite --
    # PostgreSQL's BOOLEAN columns already constrain the domain by type, but
    # SQLite has no BOOLEAN type (it's just an INTEGER convention), so
    # nothing before this stopped e.g. is_local=2 from being written.
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id      TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        is_local     INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0, 1)),
        disabled     INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
        created_at   TEXT DEFAULT (datetime('now'))
    )
    """,
    # At most one is_local=1 user -- stdio/CLI needs exactly one to bind to.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_single_local
    ON users (is_local) WHERE is_local = 1
    """,
    # API tokens. token_hash is the sha256 hex of the presented secret --
    # the plaintext is never persisted (see #144 acceptance criteria).
    """
    CREATE TABLE IF NOT EXISTS api_tokens (
        token_id     TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL REFERENCES users (user_id),
        token_hash   TEXT NOT NULL UNIQUE,
        name         TEXT,
        created_at   TEXT DEFAULT (datetime('now')),
        last_used_at TEXT,
        revoked_at   TEXT
    )
    """,
    # Pack ownership/visibility registry (#143 sec. "설계의 축"). Created here
    # so #146 can start without a DDL migration; not read/written by anything
    # yet in this PR.
    """
    CREATE TABLE IF NOT EXISTS packs (
        pack_id      TEXT PRIMARY KEY,
        owner_id     TEXT NOT NULL REFERENCES users (user_id),
        visibility   TEXT NOT NULL DEFAULT 'private',
        title        TEXT,
        description  TEXT,
        forked_from  TEXT,
        is_default   INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
        status       TEXT NOT NULL DEFAULT 'ready'
                     CHECK (status IN ('creating', 'partial', 'ready')),
        created_at   TEXT DEFAULT (datetime('now')),
        updated_at   TEXT DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_packs_owner ON packs (owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_packs_visibility ON packs (visibility)",
]


class SQLStore:
    """SQLAlchemy adapter supporting PostgreSQL and SQLite."""

    def __init__(self, url: str, *, create_tables: bool = True) -> None:
        self._url = url
        self._engine: Any = None
        self._available = False
        self._is_sqlite = url.startswith("sqlite")
        # issue #105 (verifier follow-up): billing.db is meant to hold only
        # billing_events (BillingHooks._ensure_tables() creates that table
        # itself), but SQLStore._connect() unconditionally created the full
        # ontology_nodes/ontology_edges/impact_records/lever_simulations/
        # rebac_policies schema on every connect -- harmless, but it
        # contradicted "billing-only file" and would have cost a future
        # reader (operator or migration script) time figuring out why empty
        # tables were sitting in billing.db. make_billing_sql_store passes
        # create_tables=False; every other caller keeps the default (True).
        self._create_tables_on_connect = create_tables
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            from sqlalchemy import create_engine, text  # type: ignore[import]

            connect_args: dict[str, Any] = {}
            if self._is_sqlite:
                connect_args["check_same_thread"] = False
                # issue #105: SQLAlchemy 2.0's pysqlite dialect only forwards
                # a `timeout` (the DBAPI busy-wait, in seconds, before a
                # locked table raises "database is locked") when it appears
                # in the URL query string -- config.py's sqlite_url has no
                # query, so without this the value in effect was silently
                # the sqlite3 default (5.0s; measured 5.01s on this
                # machine). Pinning it here makes that a decision instead of
                # an accident. Left at the sqlite3 default rather than
                # raised: this is the engine used by every SQLStore caller
                # (not just billing), so a bigger value would make ANY
                # contended statement here block longer. Billing doesn't
                # need a bigger value anyway: billing_events lives in its
                # own SQLite file (billing.db, local/kuzu mode) so it never
                # contends with a long writer transaction on THIS engine's
                # file in the first place -- see
                # opencrab.stores.factory.make_billing_sql_store.
                connect_args["timeout"] = 5.0

            self._engine = create_engine(self._url, connect_args=connect_args)
            self._text = text

            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))

            self._available = True
            logger.info("SQL store connected (%s)", self._url.split("@")[-1])
            if self._create_tables_on_connect:
                self._create_tables()
        except Exception as exc:
            logger.warning("SQL store unavailable: %s", exc)
            self._available = False

    def _create_tables(self) -> None:
        """Create all tables if they do not exist."""
        from sqlalchemy import text

        tables = _TABLES_SQL_SQLITE if self._is_sqlite else _TABLES_SQL
        with self._engine.begin() as conn:
            for ddl in tables:
                conn.execute(text(ddl))
        logger.debug("SQL tables ensured.")
        self._migrate_packs_is_default()
        self._migrate_packs_status()

    def _migrate_packs_is_default(self) -> None:
        """Additive migration for pre-#148 ``packs`` tables that predate the
        ``is_default`` column (the CREATE TABLE above only fires for a
        brand-new table -- IF NOT EXISTS is a no-op against an existing
        one). Runs after the static DDL loop so a from-scratch DB (which
        already has the column) is a cheap no-op here.

        The partial unique index (at most one is_default=TRUE/1 row per
        owner_id) is built here too, not in the static DDL lists: on an
        existing DB the ALTER below runs first in this same call, but if
        the index were a static DDL statement it would run BEFORE any
        ALTER on a first _create_tables() pass across an old DB, and
        "CREATE ... WHERE is_default = ..." against a table that doesn't
        yet have that column raises "no such column".
        """
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        with self._engine.begin() as conn:
            if self._is_sqlite:
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(packs)"))}
            else:
                cols = {
                    row[0]
                    for row in conn.execute(
                        text(
                            # Scoped to the active schema, like the table
                            # introspection later in this module. Unscoped, a
                            # `packs.is_default` visible in ANOTHER schema
                            # makes this skip the ALTER while the unqualified
                            # CREATE INDEX below still targets the active
                            # schema's legacy table -- which then fails and
                            # takes the whole SQL store down.
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'packs' "
                            "AND table_schema = current_schema()"
                        )
                    )
                }
            if "is_default" not in cols:
                # SQLite has no `ADD COLUMN IF NOT EXISTS` (measured: "near
                # "EXISTS": syntax error") so the existence check above is
                # the only guard -- and it's a TOCTOU: two processes racing
                # to open the same DB can both see the column missing and
                # both ALTER. The try/except absorbs the loser's "duplicate
                # column" error as success rather than crashing it.
                # The ALTER runs inside a SAVEPOINT. On PostgreSQL, catching
                # the loser's error is not enough: the failed statement leaves
                # the whole transaction aborted, so the CREATE INDEX below
                # would raise InFailedSqlTransaction and this replica's SQL
                # store would be marked unavailable. Rolling back to the
                # savepoint restores a usable transaction. SQLite does not need
                # this but tolerates it, so there is one code path.
                try:
                    with conn.begin_nested():
                        if self._is_sqlite:
                            conn.execute(
                                text(
                                    "ALTER TABLE packs ADD COLUMN is_default INTEGER "
                                    "NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1))"
                                )
                            )
                        else:
                            conn.execute(
                                text(
                                    "ALTER TABLE packs ADD COLUMN is_default BOOLEAN "
                                    "NOT NULL DEFAULT FALSE"
                                )
                            )
                except DBAPIError as exc:
                    message = str(getattr(exc, "orig", exc)).lower()
                    if "duplicate column" not in message and "already exists" not in message:
                        logger.error("packs.is_default migration failed: %s", exc)
                        raise

            # The ON CONFLICT target in ownership.ensure_default_pack must
            # name this index's predicate LITERALLY (measured: an
            # "ON CONFLICT (owner_id) WHERE is_default" shorthand fails
            # with "does not match any PRIMARY KEY or UNIQUE constraint";
            # only the fully-written `= 1` / `= TRUE` form matches) -- keep
            # the two in sync if this predicate ever changes.
            if self._is_sqlite:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_packs_one_default "
                        "ON packs (owner_id) WHERE is_default = 1"
                    )
                )
            else:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_packs_one_default "
                        "ON packs (owner_id) WHERE is_default = TRUE"
                    )
                )

    def _migrate_packs_status(self) -> None:
        """Additive migration for pre-#170 ``packs`` tables that predate the
        ``status`` column, run right after :meth:`_migrate_packs_is_default`
        for the same reason that one runs after the static DDL loop: a
        from-scratch DB already has the column (the CREATE TABLE above
        covers it) so this is a cheap no-op there, and only an existing DB
        needs the ALTER.

        Shape mirrors ``_migrate_packs_is_default`` exactly and for the same
        reasons -- see that method's docstring for the TOCTOU-absorbing
        try/except around the ALTER and for why it runs inside a
        ``conn.begin_nested()`` SAVEPOINT (on PostgreSQL a failed statement
        aborts the whole transaction; rolling back to the savepoint leaves a
        usable one for the CREATE INDEX that follows). The index is built
        here rather than in the static DDL lists for the same reason
        ``idx_packs_one_default`` is: on an existing DB the ALTER above runs
        first in this same call, but a static "CREATE INDEX ... (status)"
        statement would run BEFORE any ALTER on a first pass across an old
        DB and raise "no such column".

        ``DEFAULT 'ready'`` (not ``'creating'``) for two reasons. First, this
        migration must land every *existing* row as ``ready`` (nothing about
        an already-registered pack is "in progress"), and SQLite cannot
        ``ADD COLUMN`` a ``NOT NULL`` column without a default in the first
        place -- so the column default does both jobs by itself, no UPDATE
        pass needed. Second, and the reason it stays ``'ready'`` rather than
        anything else that also satisfies the first requirement: the forward
        migration (old SQLite -> new PostgreSQL,
        ``scripts/_migration_tables.py::resolve_columns``) drops a column
        from the copy list when the *source* table doesn't have it and lets
        the *target* column's default fill it in instead. A pre-#170 source
        has no ``status`` column at all, so every pack copied from it would
        land with whatever this default is. If that default were
        ``'creating'``, every pack that ever went through a forward
        migration would land invisible, and nothing would reliably bring it
        back: ``opencrab packs repair-registry`` promotes a ``creating`` row
        only when it can positively find that pack's graph anchor, and a
        legacy pack migrated from a pre-anchor era generally has none -- so
        the repair pass would demote it to ``partial``, which is exactly the
        state that requires a human to decide. ``'ready'`` is the correct
        reading instead: a migrated pre-#170 row never had a lifecycle state
        to lose, so it keeps behaving as it did before #170.

        That fail-open default is safe only because production INSERTs into
        ``packs`` are required to name ``status`` explicitly rather than
        rely on this default -- ``_insert_pack``/``_ensure_default_pack_on``
        in ``opencrab/pack/ownership.py`` do this; this default exists for
        the migration path only.

        ROLLING THIS BACK. The column is additive, so reverting the code
        leaves an older build reading this DB without complaint (every
        SELECT names its columns). What an older build does NOT do is
        honour ``status``: it has no such filter, so any ``creating`` or
        ``partial`` row becomes visible and writable again the moment it
        starts. Counting those rows is not enough on its own either -- a
        ``pack_create`` landing between the count and the restart makes a
        fresh one. The procedure is:

        1. stop or drain pack writes,
        2. with the graph store reachable, run
           ``opencrab packs repair-registry --apply`` (with the graph down
           it can only skip, and its ``--promote`` refusals then mean
           "could not check" rather than "no anchor"),
        3. re-run ``SELECT count(*) FROM packs WHERE status <> 'ready'``
           while writes are still stopped,
        4. at zero, start the older build and check once more,
        5. above zero, classify what is left from step 2's report rather
           than treating it as one kind of leftover.

        Step 2 does not touch ``partial`` rows at all -- it only resolves
        ``creating`` ones -- so the remainder is a mix, and each part needs
        a different move:

        - a ``partial`` row whose graph probe says ``present``: promote it
          with ``--promote <pack_id> --apply``. It has a real anchor.
        - a ``partial`` row whose graph probe says ``absent``: this is the
          only leftover confirmed to have no anchor, and the only one a
          human should resolve directly in SQL.
        - a row whose graph probe says ``unknown``: NOT known to be
          anchorless. Deleting it can orphan an anchor that a later probe
          would have found, which is the state #147's startup check
          refuses. Restore the graph store and go back to step 2.
        - a ``creating`` row skipped for age: ``too recent`` clears by
          lowering ``--older-than`` and re-running step 2 (with writes
          stopped, no new ones appear). ``unknown age`` does not -- it
          means ``updated_at`` is absent, unparseable, or in the future, so
          the age gate never applies; inspect that column and decide by
          hand.

        Then return to step 3.
        """
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        with self._engine.begin() as conn:
            if self._is_sqlite:
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(packs)"))}
            else:
                cols = {
                    row[0]
                    for row in conn.execute(
                        text(
                            # Scoped to the active schema for the same reason
                            # as the is_default migration's equivalent query
                            # above: unscoped, a packs.status visible in
                            # ANOTHER schema makes this skip the ALTER while
                            # the unqualified CREATE INDEX below still
                            # targets the active schema's legacy table.
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'packs' "
                            "AND table_schema = current_schema()"
                        )
                    )
                }
            if "status" not in cols:
                # Same TOCTOU/SAVEPOINT rationale as the is_default ALTER
                # above: two processes can race to open the same DB and both
                # see the column missing, so the loser's "duplicate column"
                # is absorbed rather than crashing it, and the SAVEPOINT
                # keeps PostgreSQL's transaction usable afterwards either way.
                try:
                    with conn.begin_nested():
                        if self._is_sqlite:
                            conn.execute(
                                text(
                                    "ALTER TABLE packs ADD COLUMN status TEXT "
                                    "NOT NULL DEFAULT 'ready' "
                                    "CHECK (status IN ('creating', 'partial', 'ready'))"
                                )
                            )
                        else:
                            conn.execute(
                                text(
                                    "ALTER TABLE packs ADD COLUMN status VARCHAR(32) "
                                    "NOT NULL DEFAULT 'ready' "
                                    "CHECK (status IN ('creating', 'partial', 'ready'))"
                                )
                            )
                except DBAPIError as exc:
                    message = str(getattr(exc, "orig", exc)).lower()
                    if "duplicate column" not in message and "already exists" not in message:
                        logger.error("packs.status migration failed: %s", exc)
                        raise

            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_packs_status ON packs (status)"))

    @property
    def available(self) -> bool:
        return self._available

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("SQL store is not available.")

    def ping(self) -> bool:
        """Return True if the database is reachable."""
        try:
            from sqlalchemy import text

            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Node registry
    # ------------------------------------------------------------------

    def register_node(self, space: str, node_type: str, node_id: str) -> None:
        """Insert or update a node registry entry.

        Re-registering an existing ``(space, node_id)`` updates the existing
        row rather than deleting and reinserting it: the row keeps its ``id``
        and ``created_at``, and only ``node_type`` and ``updated_at`` change.
        The SQLite branch used to emit SQLite's REPLACE conflict-resolution
        form, which is a DELETE-then-INSERT — it reallocated the AUTOINCREMENT
        ``id`` and re-evaluated the ``created_at`` DEFAULT, so "first seen at"
        silently became "last seen at", while the PG branch preserved both.
        One ``ON CONFLICT (...) DO UPDATE`` now serves both dialects, which is
        the same contract ``_sql_dialect.py``'s "ROWID STABILITY" note already
        fixed for the graph/doc stores (issue #81).

        The shared contract is exactly those two columns. On SQLite the
        rewrite additionally keeps the row's rowid (``id`` is the rowid alias
        here), which is what that note is about; PostgreSQL gives no such
        guarantee — an UPDATE writes a new tuple version under MVCC — and
        nothing here asks it to.

        That contract is pinned two ways in
        ``tests/test_sql_store_upsert_parity.py``: the SQL both methods
        actually execute is captured and checked, and this file's source is
        searched for the literal two-word spelling of SQLite's conflict-
        resolution clause (the one named in that same note). The pin greps for
        those two words adjacent, which is why the prose above never puts them
        side by side.

        Only the "current time" fragment still comes from the dialect
        (``datetime('now')`` vs ``NOW()``); the statement itself is shared.
        ``updated_at`` is written explicitly rather than left to the DDL
        DEFAULT so the INSERT path keeps the PG branch's exact previous
        meaning even on a schema whose column has no DEFAULT.

        Assumes the target table carries the ``UNIQUE (space, node_id)``
        constraint this store's own DDL creates (present since the first
        commit). A hand-rolled schema without it has never supported this
        method's upsert semantics — with no constraint to conflict on, the old
        statement silently appended a duplicate row on every call instead of
        updating; ``ON CONFLICT`` reports the schema mismatch instead.
        """
        self._require_available()

        from sqlalchemy import text

        from opencrab.stores._sql_dialect import POSTGRES, SQLITE

        now = (SQLITE if self._is_sqlite else POSTGRES).now_expr()
        sql = text(
            "INSERT INTO ontology_nodes (space, node_type, node_id, updated_at) "
            f"VALUES (:space, :node_type, :node_id, {now}) "
            "ON CONFLICT (space, node_id) DO UPDATE SET "
            f"node_type = EXCLUDED.node_type, updated_at = {now}"
        )

        with self._engine.begin() as conn:
            conn.execute(sql, {"space": space, "node_type": node_type, "node_id": node_id})

    def register_edge(
        self, from_space: str, from_id: str, relation: str, to_space: str, to_id: str
    ) -> None:
        """Insert or ignore an edge registry entry."""
        self._require_available()

        from sqlalchemy import text

        if self._is_sqlite:
            sql = text(
                "INSERT OR IGNORE INTO ontology_edges "
                "(from_space, from_id, relation, to_space, to_id) "
                "VALUES (:fs, :fi, :rel, :ts, :ti)"
            )
        else:
            sql = text(
                "INSERT INTO ontology_edges (from_space, from_id, relation, to_space, to_id) "
                "VALUES (:fs, :fi, :rel, :ts, :ti) "
                "ON CONFLICT DO NOTHING"
            )

        with self._engine.begin() as conn:
            conn.execute(
                sql,
                {"fs": from_space, "fi": from_id, "rel": relation, "ts": to_space, "ti": to_id},
            )

    # ------------------------------------------------------------------
    # Impact records
    # ------------------------------------------------------------------

    def save_impact(
        self, node_id: str, change_type: str, impact: dict[str, Any]
    ) -> int:
        """Persist an impact analysis result. Returns the row id."""
        self._require_available()

        import json

        from sqlalchemy import text

        sql = text(
            "INSERT INTO impact_records (node_id, change_type, impact_json) "
            "VALUES (:node_id, :change_type, :json) "
            "RETURNING id"
        )
        if self._is_sqlite:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO impact_records (node_id, change_type, impact_json) "
                        "VALUES (:node_id, :change_type, :json)"
                    ),
                    {"node_id": node_id, "change_type": change_type, "json": json.dumps(impact)},
                )
                result = conn.execute(text("SELECT last_insert_rowid()"))
                row = result.fetchone()
                return int(row[0]) if row else -1
        else:
            with self._engine.begin() as conn:
                result = conn.execute(
                    sql,
                    {"node_id": node_id, "change_type": change_type, "json": json.dumps(impact)},
                )
                row = result.fetchone()
                return int(row[0]) if row else -1

    def get_impacts(self, node_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Retrieve recent impact records for a node."""
        self._require_available()

        import json

        from sqlalchemy import text

        sql = text(
            "SELECT id, node_id, change_type, impact_json, analyzed_at "
            "FROM impact_records WHERE node_id = :node_id "
            "ORDER BY analyzed_at DESC LIMIT :limit"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"node_id": node_id, "limit": limit}).fetchall()
        return [
            {
                "id": r[0],
                "node_id": r[1],
                "change_type": r[2],
                "impact": json.loads(r[3]),
                "analyzed_at": str(r[4]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Lever simulations
    # ------------------------------------------------------------------

    def save_simulation(
        self, lever_id: str, direction: str, magnitude: float, results: dict[str, Any]
    ) -> int:
        """Persist a lever simulation result."""
        self._require_available()

        import json

        from sqlalchemy import text

        if self._is_sqlite:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO lever_simulations (lever_id, direction, magnitude, results) "
                        "VALUES (:lever_id, :direction, :magnitude, :results)"
                    ),
                    {
                        "lever_id": lever_id,
                        "direction": direction,
                        "magnitude": magnitude,
                        "results": json.dumps(results),
                    },
                )
                row = conn.execute(text("SELECT last_insert_rowid()")).fetchone()
                return int(row[0]) if row else -1
        else:
            sql = text(
                "INSERT INTO lever_simulations (lever_id, direction, magnitude, results) "
                "VALUES (:lever_id, :direction, :magnitude, :results) RETURNING id"
            )
            with self._engine.begin() as conn:
                row = conn.execute(
                    sql,
                    {
                        "lever_id": lever_id,
                        "direction": direction,
                        "magnitude": magnitude,
                        "results": json.dumps(results),
                    },
                ).fetchone()
                return int(row[0]) if row else -1

    # ------------------------------------------------------------------
    # ReBAC policies
    # ------------------------------------------------------------------

    def set_policy(
        self,
        subject_id: str,
        permission: str,
        resource_id: str,
        granted: bool = True,
    ) -> None:
        """Upsert a ReBAC policy row.

        Re-setting an existing ``(subject_id, permission, resource_id)``
        updates the existing row's ``granted`` rather than deleting and
        reinserting it: the row keeps its ``id`` and ``created_at``. Same fix and same reasoning as ``register_node`` above
        (issue #81) — the SQLite branch's REPLACE form destroyed both.
        ``rebac_policies`` has no ``updated_at`` column, so this statement needs
        no dialect-specific fragment at all and is shared verbatim.

        Assumes the ``UNIQUE (subject_id, permission, resource_id)`` constraint
        from this store's own DDL; see ``register_node``.
        """
        self._require_available()

        from sqlalchemy import text

        sql = text(
            "INSERT INTO rebac_policies (subject_id, permission, resource_id, granted) "
            "VALUES (:sid, :perm, :rid, :granted) "
            "ON CONFLICT (subject_id, permission, resource_id) DO UPDATE SET "
            "granted = EXCLUDED.granted"
        )
        with self._engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "sid": subject_id,
                    "perm": permission,
                    "rid": resource_id,
                    "granted": granted,
                },
            )

    def check_policy(
        self, subject_id: str, permission: str, resource_id: str
    ) -> bool | None:
        """
        Look up a stored ReBAC policy.

        Returns True/False if a policy exists, None if no policy row found.
        """
        self._require_available()

        from sqlalchemy import text

        sql = text(
            "SELECT granted FROM rebac_policies "
            "WHERE subject_id=:sid AND permission=:perm AND resource_id=:rid"
        )
        with self._engine.connect() as conn:
            row = conn.execute(
                sql,
                {"sid": subject_id, "perm": permission, "rid": resource_id},
            ).fetchone()
        if row is None:
            return None
        return bool(row[0])

    def list_policies(self, subject_id: str) -> list[dict[str, Any]]:
        """List all policies for a given subject."""
        self._require_available()

        from sqlalchemy import text

        sql = text(
            "SELECT subject_id, permission, resource_id, granted, created_at "
            "FROM rebac_policies WHERE subject_id=:sid"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"sid": subject_id}).fetchall()
        return [
            {
                "subject_id": r[0],
                "permission": r[1],
                "resource_id": r[2],
                "granted": bool(r[3]),
                "created_at": str(r[4]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def table_counts(self) -> dict[str, int]:
        """Return row counts for tables that currently exist.

        A store opened with ``create_tables=False`` (see
        ``factory.make_billing_sql_store``), or a pre-#144 database that
        predates the users/api_tokens/packs tables, may not have all of
        the tables below -- querying one unconditionally raised "no such
        table". Pre-querying which tables actually exist and counting only
        those avoids that. Deliberately NOT per-table try/except: on
        PostgreSQL a failed statement aborts the whole transaction, so any
        count after the first failure would fail too.
        """
        if not self._available:
            return {}

        from sqlalchemy import text

        tables = [
            "ontology_nodes",
            "ontology_edges",
            "impact_records",
            "lever_simulations",
            "rebac_policies",
            "users",
            "api_tokens",
            "packs",
        ]
        counts: dict[str, int] = {}
        with self._engine.connect() as conn:
            if self._is_sqlite:
                existing = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type = 'table'")
                    )
                }
            else:
                existing = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = current_schema()"
                        )
                    )
                }
            for table in tables:
                if table not in existing:
                    continue
                row = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()  # noqa: S608
                counts[table] = int(row[0]) if row else 0
        return counts
