"""Pack ownership/visibility registry (#146, execution 3 of #143).

Backs the ``packs`` table (DDL in ``opencrab/stores/sql_store.py``, added
in #144 for #146 to build on without a DDL migration). This module is the
SINGLE AUTHORITY for "does this pack exist, who owns it, is it visible to
`principal`" -- ``graph.list_packs()`` (a ``GROUP BY`` over
``graph_nodes.properties->>'pack_id'``) is only ever an auxiliary
node-count/title source now (#143 acceptance criteria: "graph.list_packs()
를 팩 목록의 권위로 쓰는 호출부가 0건").

Function signatures mirror ``opencrab/auth.py``'s: every function takes
``sql`` (a ``SQLStore``) as its first argument and issues its own
short-lived connection/transaction -- no ORM session to thread through
call sites, same as every other ``opencrab.auth`` function.

**``status == 'ready'`` is NOT proof an anchor exists (#170, design v4
§3.2).** ``ready`` has two different producers with two different meanings:
a pack that went through the ``creating`` -> ``ready`` (or ``partial`` ->
``ready``) lifecycle really does have a graph anchor node, but a pack
registered by ``ensure_default_pack`` (the default pack) or by the
ownership-migration script never goes through that lifecycle at all -- it
is inserted straight into ``ready`` because those registrations have no
anchor concept to begin with (see each of those two functions' own
docstrings). No code anywhere may treat ``status == 'ready'`` as "this
pack's anchor node is in the graph".
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencrab.auth import Principal

VISIBILITIES = ("private", "public-read", "public-fork")

# Registry lifecycle states (#170, design v4 §3.2). ``ready`` is the
# absorbing state: nothing transitions out of it, and nothing transitions
# back into ``creating``. See ``begin_pack_creation``/``mark_pack_ready``/
# ``mark_pack_partial``/``promote_partial_pack`` for the three transitions
# that exist.
PACK_STATUS_CREATING = "creating"
PACK_STATUS_PARTIAL = "partial"
PACK_STATUS_READY = "ready"
PACK_STATUSES = (PACK_STATUS_CREATING, PACK_STATUS_PARTIAL, PACK_STATUS_READY)

_SELECT_COLS = (
    "pack_id, owner_id, visibility, title, description, forked_from, "
    "is_default, status, created_at, updated_at"
)


def anchor_node_id(pack_id: str) -> str:
    """The graph anchor node id convention for ``pack_id``'s existence.

    팩의 존재 기준이 되는 graph 앵커 노드의 id 규약. ``pack_create`` 와 회수
    작업이 같은 것을 보게 하려고 여기에 둔다.
    """
    return f"dataset:{pack_id}"

# #148: default-pack id prefix + random suffix, mirroring create_pack's
# collision-suffix shape -- never `default-{user_id}` (that string-convention
# design was rejected: it exposes user_id, couples the pack_id format to the
# user_id format, and can't stop a second process from racing to the same id).
_DEFAULT_PACK_ID_PREFIX = "default-"

# On a pack_id collision, create_pack retries this many random-suffixed
# candidates before giving up (see create_pack).
_MAX_RANDOM_ATTEMPTS = 8


class PackNotFoundError(LookupError):
    """No such pack -- OR a private pack ``principal`` does not own.

    Those two cases are folded into ONE exception on purpose (#143
    invariant 7, "존재 누출 금지"): a private pack's existence must not be
    observable to anyone but its owner, so "doesn't exist" and "exists but
    you can't see it" have to look identical to every caller.
    """


class PackForbiddenError(PermissionError):
    """Pack exists and is visible to ``principal`` (it's public), but
    ``principal`` isn't its owner. Never raised for a private pack owned
    by someone else -- that's ``PackNotFoundError`` instead (see its
    docstring). Safe to distinguish from "not found" here because the
    pack's existence is already observable (e.g. via ``content_pack_list``)
    to anyone, not just the owner.
    """


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "pack_id": row[0],
        "owner_id": row[1],
        "visibility": row[2],
        "title": row[3],
        "description": row[4],
        "forked_from": row[5],
        # SQLite stores this as 0/1 (INTEGER, no native BOOLEAN type), PG as
        # a real boolean -- normalize both to bool so callers never branch
        # on backend.
        "is_default": bool(row[6]),
        "status": row[7],
        "created_at": str(row[8]) if row[8] is not None else None,
        "updated_at": str(row[9]) if row[9] is not None else None,
    }


def get_pack(sql: Any, pack_id: str) -> dict[str, Any] | None:
    """Raw registry lookup by pack_id, UNSCOPED by visibility -- this is
    not access control. Callers that need principal-aware access decide
    with ``assert_writable`` / ``readable_pack_ids`` / ``list_packs_for``
    instead of gating on this return value directly.
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM packs WHERE pack_id = :pid"),  # noqa: S608
            {"pid": pack_id},
        ).fetchone()
    return _row_to_dict(row) if row else None


def _is_pack_id_conflict(exc: Any) -> bool:
    """True only for the pack_id PK/UNIQUE violation ``_insert_pack``'s
    slug-collision retry is meant to swallow. Every other ``IntegrityError``
    (FK, CHECK, NOT NULL, or a form we don't recognise) must be re-raised --
    blindly treating every ``IntegrityError`` as "slug taken" is the bug
    this classifies away from (a FK violation, say, would otherwise be
    silently reported as a collision and retried forever).

    Reads ``exc.orig`` (the driver-native exception DBAPI wraps), not the
    outer SQLAlchemy exception -- the identifying attributes below only
    exist on the driver object.

    - SQLite: ``orig.sqlite_errorname`` is ``SQLITE_CONSTRAINT_PRIMARYKEY``
      or ``SQLITE_CONSTRAINT_UNIQUE`` AND the message names
      ``packs.pack_id`` (so a UNIQUE violation on some other column/table
      isn't misclassified as a pack_id collision).
    - PostgreSQL: ``orig.pgcode == "23505"`` (unique_violation).
    """
    orig = getattr(exc, "orig", None)
    sqlite_errorname = getattr(orig, "sqlite_errorname", None)
    if sqlite_errorname is not None:
        return sqlite_errorname in (
            "SQLITE_CONSTRAINT_PRIMARYKEY",
            "SQLITE_CONSTRAINT_UNIQUE",
        ) and "packs.pack_id" in str(orig)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode is not None:
        return pgcode == "23505"
    return False


def _insert_pack(
    sql: Any,
    pack_id: str,
    owner_id: str,
    title: str | None,
    description: str | None,
    forked_from: str | None,
    *,
    conn: Any = None,
    status: str = PACK_STATUS_READY,
) -> bool:
    """Attempt one INSERT. True on success, False if pack_id is already
    taken (PK violation). A plain SELECT-then-INSERT would race two
    concurrent ``pack_create`` calls onto the same slug; letting the PK's
    UNIQUE constraint be the single point of truth avoids that TOCTOU
    window entirely.

    Only a pack_id collision (see ``_is_pack_id_conflict``) is swallowed
    into ``False`` -- any other ``IntegrityError`` (FK, CHECK, ...) is
    re-raised rather than misreported as "slug taken".

    ``conn`` (#148): when given, the INSERT runs on that connection instead
    of opening its own ``sql._engine.begin()`` transaction -- lets
    ``ensure_default_pack`` compose this into a caller-supplied transaction
    (e.g. ``create_user``'s single users+packs transaction) instead of
    committing on its own.

    ``status`` (#170) is always named explicitly in the INSERT, never left
    to the column's ``DEFAULT 'ready'``. The default has to stay
    fail-open-shaped for the reverse-migration case (#151: an old source row
    with no ``status`` column resolves to the target's default), which means
    every *production* INSERT must say what it means instead of leaning on
    that default -- see ``tests/test_packs_lifecycle_registry.py``'s INSERT
    contract test.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    stmt = text(
        "INSERT INTO packs (pack_id, owner_id, visibility, title, description, "
        "forked_from, status) "
        "VALUES (:pid, :oid, 'private', :title, :desc, :forked, :status)"
    )
    params = {
        "pid": pack_id,
        "oid": owner_id,
        "title": title,
        "desc": description,
        "forked": forked_from,
        "status": status,
    }
    try:
        if conn is not None:
            conn.execute(stmt, params)
        else:
            with sql._engine.begin() as owned_conn:
                owned_conn.execute(stmt, params)
        return True
    except IntegrityError as exc:
        if _is_pack_id_conflict(exc):
            return False
        raise


def _negotiate_pack_id(
    sql: Any,
    owner_id: str,
    pack_id: str,
    title: str | None,
    description: str | None,
    forked_from: str | None,
    *,
    status: str,
) -> str:
    """Shared slug-collision retry for ``create_pack``/``begin_pack_creation``
    (#170) -- both need EXACTLY the same negotiation (see ``create_pack``'s
    docstring for why the retry suffix is random, not sequential), and two
    copies of that loop would be free to drift from each other. The only
    thing that differs between the two callers is the ``status`` the row is
    inserted with.
    """
    if _insert_pack(sql, pack_id, owner_id, title, description, forked_from, status=status):
        return pack_id
    for _ in range(_MAX_RANDOM_ATTEMPTS):
        candidate = f"{pack_id}-{secrets.token_hex(4)}"
        if _insert_pack(
            sql, candidate, owner_id, title, description, forked_from, status=status
        ):
            return candidate
    raise RuntimeError(f"could not allocate a unique pack_id for {pack_id!r}")


def create_pack(
    sql: Any,
    owner_id: str,
    pack_id: str,
    title: str | None = None,
    description: str | None = None,
    forked_from: str | None = None,
) -> str:
    """Insert a new pack registry row owned by ``owner_id``, immediately
    ``ready``. Returns the pack_id actually assigned -- which may differ
    from the requested ``pack_id``.

    For registering an ALREADY-COMPLETE pack that has no lifecycle to run
    (the default pack, an ownership-migration row): there is no anchor to
    wait for, so the row is ``ready`` from the moment it exists. A pack
    that needs to run the ``creating`` -> ``ready``/``partial`` lifecycle
    (anything with a graph anchor to land first) uses ``begin_pack_creation``
    instead.

    When ``pack_id`` is already taken (by anyone, including another user),
    this quietly appends a random 8-hex-char suffix and retries rather than
    erroring (#143 invariant 7: an error naming "already exists" would tell
    the caller someone else already holds that exact slug -- see
    ``pack_create`` in ``opencrab/mcp/tools/pack.py`` for the caller side of
    this contract, and pack_id format is never changed, only suffixed). A
    sequential ``-2``, ``-3``, ... suffix would leak a second bit beyond
    "a collision happened" -- how many others are already using this
    exact slug -- so every retry after the first collision draws an
    independent random suffix instead. Gives up after
    ``_MAX_RANDOM_ATTEMPTS`` tries (collisions across 8 independent
    32-bit-space draws is not a real-world flood, only a stuck RNG or a
    saturated keyspace) and raises ``RuntimeError`` rather than looping
    forever -- no row is left behind by a failed call.
    """
    return _negotiate_pack_id(
        sql, owner_id, pack_id, title, description, forked_from, status=PACK_STATUS_READY
    )


def begin_pack_creation(
    sql: Any,
    owner_id: str,
    pack_id: str,
    title: str | None = None,
    description: str | None = None,
    forked_from: str | None = None,
) -> str:
    """Reserve a pack_id in ``status='creating'`` -- the start of the
    two-phase creation lifecycle (#170, design v4 §3.2/§3.5): the slug is
    claimed here, before the caller has written the pack's graph anchor.
    Until the anchor lands and the row is promoted (``mark_pack_ready``) or
    the attempt is given up on (``mark_pack_partial``), the row is invisible
    to every read path (``readable_pack_ids``/``list_packs_for`` filter on
    ``status = 'ready'``) and unwritable through anything but the anchor
    write itself (``write_gate.authorize``'s default ``allowed_statuses``
    excludes ``creating``).

    Same slug negotiation as ``create_pack`` (see its docstring) -- this is
    the ``status='creating'`` sibling of that call, for a pack whose
    completeness isn't known yet.
    """
    return _negotiate_pack_id(
        sql, owner_id, pack_id, title, description, forked_from, status=PACK_STATUS_CREATING
    )


def ensure_default_pack(sql: Any, owner_id: str, *, conn: Any = None) -> str:
    """Return ``owner_id``'s default pack (``is_default=TRUE/1``), creating
    it on first call. Idempotent: every subsequent call for the same
    ``owner_id`` returns the same pack_id without inserting anything.

    This is the ONLY way a pack gets ``is_default=TRUE`` -- the id is never
    a string convention like ``default-{user_id}`` (rejected design: leaks
    user_id, couples the pack_id format to the user_id format, and can't
    stop two processes from racing to the same id). Instead the id is a
    random ``default-{8 hex}`` slug, same shape as ``create_pack``'s
    collision suffix, and uniqueness-per-owner is enforced by
    ``idx_packs_one_default`` (the partial unique index migrated in
    ``sql_store.py``), not by anything in this function.

    Registered ``status='ready'`` immediately (#170): the default pack has
    no anchor-node lifecycle to run -- it is not a pack a caller "finishes
    creating", it is the account's always-there pack -- so ``ready`` here
    means only "visible and writable", not "this pack's anchor landed in
    the graph" (see module docstring).

    ``conn`` (#148): when given (e.g. by ``opencrab.auth.create_user``, which
    must create the user row and its default pack in ONE transaction because
    ``packs.owner_id`` FK-references ``users.user_id``), this runs on that
    connection and does not open or commit its own transaction -- the caller
    owns the commit/rollback. When omitted, this opens its own
    ``sql._engine.begin()`` transaction.
    """
    if conn is not None:
        return _ensure_default_pack_on(sql, owner_id, conn)
    with sql._engine.begin() as owned_conn:
        return _ensure_default_pack_on(sql, owner_id, owned_conn)


def _ensure_default_pack_on(sql: Any, owner_id: str, conn: Any) -> str:
    """``ensure_default_pack``'s body, given an already-open ``conn``."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    select_existing = text(
        "SELECT pack_id FROM packs WHERE owner_id = :oid AND is_default = :is_default"
    )
    # A Python bool binds correctly on both drivers here: pysqlite treats
    # bool as an int subclass (True -> 1) against the INTEGER column, and
    # psycopg binds it to a real boolean -- no per-dialect branch needed for
    # this parameterized comparison (unlike the INSERT/ON CONFLICT text
    # below, which is NOT parameterized because its literal spelling must
    # match the partial index's predicate character-for-character).
    row = conn.execute(select_existing, {"oid": owner_id, "is_default": True}).fetchone()
    if row is not None:
        return row[0]

    # Measured (#148 round-5 verification): "ON CONFLICT (owner_id) WHERE
    # is_default" (the shorthand PostgreSQL/SQLite upsert examples usually
    # show) fails with "ON CONFLICT clause does not match any PRIMARY KEY or
    # UNIQUE constraint" -- only the fully-spelled `= 1` / `= TRUE` predicate,
    # matching idx_packs_one_default's own DDL text, is accepted as the
    # arbiter. Keep these two predicates in sync if either ever changes.
    if sql._is_sqlite:
        insert_default = text(
            "INSERT INTO packs (pack_id, owner_id, visibility, title, is_default, status) "
            "VALUES (:pid, :oid, 'private', :title, 1, 'ready') "
            "ON CONFLICT (owner_id) WHERE is_default = 1 DO NOTHING"
        )
    else:
        insert_default = text(
            "INSERT INTO packs (pack_id, owner_id, visibility, title, is_default, status) "
            "VALUES (:pid, :oid, 'private', :title, TRUE, 'ready') "
            "ON CONFLICT (owner_id) WHERE is_default = TRUE DO NOTHING"
        )

    for _ in range(_MAX_RANDOM_ATTEMPTS):
        candidate = f"{_DEFAULT_PACK_ID_PREFIX}{secrets.token_hex(8)}"
        try:
            # Measured (#148 round-5 verification): a pack_id PK collision on
            # this INSERT surfaces as an IntegrityError, NOT rowcount 0 --
            # the targeted ON CONFLICT arbiter above is idx_packs_one_default
            # (owner_id), so it only absorbs an owner_id conflict; a pack_id
            # collision (this random candidate happening to match some
            # unrelated existing pack) isn't that arbiter's conflict to
            # swallow. Without begin_nested()'s SAVEPOINT, that IntegrityError
            # would abort the WHOLE outer transaction on PostgreSQL (not just
            # this statement), taking down anything else conn is doing (e.g.
            # create_user's users INSERT).
            with conn.begin_nested():
                result = conn.execute(
                    insert_default, {"pid": candidate, "oid": owner_id, "title": "Default pack"}
                )
        except IntegrityError as exc:
            if _is_pack_id_conflict(exc):
                continue
            raise
        if result.rowcount > 0:
            return candidate
        # rowcount 0: the ON CONFLICT DO NOTHING fired, meaning another
        # process/thread already holds owner_id's default pack -- go read it.
        row = conn.execute(select_existing, {"oid": owner_id, "is_default": True}).fetchone()
        assert row is not None  # the DO NOTHING branch means one must exist
        return row[0]
    raise RuntimeError(f"could not allocate a unique default pack_id for owner {owner_id!r}")


def resolve_write_pack(sql: Any, principal: Principal, requested: str | None) -> str:
    """The pack_id a write should target: ``requested`` if the caller named
    one, else ``principal``'s default pack.

    Authorization is NOT this function's job -- a caller-supplied
    ``requested`` is returned as-is, and it's on the caller to run it through
    ``assert_writable`` (this keeps ``resolve_write_pack`` usable in read
    paths too, where "may I write here" doesn't apply). This exists so no
    write path can leave ``pack_id`` unset: every write lands in a pack, one
    way or another (see module docstring / #143 "팩 없는 쓰기 경로를 남기지
    않는다").
    """
    if requested:
        return requested
    return ensure_default_pack(sql, principal.user_id)


def delete_pack_row(
    sql: Any,
    pack_id: str,
    owner_id: str,
    *,
    only_status: tuple[str, ...] | None = None,
) -> bool:
    """Delete ONE row from the ``packs`` registry table -- ``pack_id`` AND
    ``owner_id`` must both match (an owner can only ever remove their own
    row; this is not a general admin delete). Returns True iff a row was
    actually removed.

    Registry row only -- does NOT touch any graph/doc/sql/vector content
    tagged with this pack_id. Used only as ``pack_create``'s compensating
    delete for the ONE branch in this design that deletes at all (#170,
    design v4 §3.0): an anchor identity conflict caught BEFORE
    ``builder.add_node`` is ever called, where "no content exists" is
    proven by control flow, not by a probe. Every failure after the first
    writer call ends in ``mark_pack_partial`` instead -- never a delete.
    A full ``pack_delete`` (removing content too) is a separate,
    not-yet-built tool.

    ``only_status`` (#170): when given, the DELETE only matches rows whose
    ``status`` is in this set -- ``AND status IN (...)``. Callers that
    delete the ``creating`` row they themselves just reserved pass
    ``('creating',)`` so a state change that raced in underneath them
    (e.g. an operator's ``--promote``) is never clobbered by a stale
    compensating delete. Omitted (``None``) keeps the old status-blind
    behaviour.
    """
    from sqlalchemy import text

    where = "pack_id = :pid AND owner_id = :oid"
    params: dict[str, Any] = {"pid": pack_id, "oid": owner_id}
    if only_status is not None:
        placeholders = ", ".join(f":st{i}" for i in range(len(only_status)))
        where += f" AND status IN ({placeholders})"
        for i, st in enumerate(only_status):
            params[f"st{i}"] = st

    with sql._engine.begin() as conn:
        result = conn.execute(
            text(f"DELETE FROM packs WHERE {where}"),  # noqa: S608 -- where is a fixed shape, values are bound
            params,
        )
        return result.rowcount > 0


def mark_pack_ready(sql: Any, pack_id: str, owner_id: str) -> bool:
    """``creating`` -> ``ready``. Returns True iff a row actually
    transitioned.

    The WHERE clause pins the FROM state (``status = 'creating'``) as well
    as the TO state -- not just ``pack_id``/``owner_id`` -- so this can never
    silently re-promote a row a concurrent ``mark_pack_partial`` (or an
    operator's manual demotion) already moved out of ``creating``. That is
    enforced by the predicate, not by caller discipline: a caller that races
    this against another transition gets ``False``, not a corrupted state.
    """
    from sqlalchemy import text

    from opencrab.execution._sql import now_expr

    with sql._engine.begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE packs SET status = 'ready', updated_at = {now_expr(sql)} "
                "WHERE pack_id = :pid AND owner_id = :oid AND status = 'creating'"
            ),
            {"pid": pack_id, "oid": owner_id},
        )
        return result.rowcount > 0


def mark_pack_partial(sql: Any, pack_id: str, owner_id: str) -> bool:
    """``creating`` -> ``partial``. Returns True iff a row actually
    transitioned.

    Same reasoning as ``mark_pack_ready``: the WHERE clause requires
    ``status = 'creating'`` so an already-``ready`` row can never be
    demoted by this call -- a stale/late compensating call landing after
    the pack already finished successfully is a no-op, not a regression.
    """
    from sqlalchemy import text

    from opencrab.execution._sql import now_expr

    with sql._engine.begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE packs SET status = 'partial', updated_at = {now_expr(sql)} "
                "WHERE pack_id = :pid AND owner_id = :oid AND status = 'creating'"
            ),
            {"pid": pack_id, "oid": owner_id},
        )
        return result.rowcount > 0


def promote_partial_pack(sql: Any, pack_id: str, owner_id: str) -> bool:
    """``partial`` -> ``ready``. Returns True iff a row actually
    transitioned.

    Operator-explicit-action only (the ``packs repair-registry --promote``
    CLI path) -- the CALLER must confirm the graph anchor positively exists
    for this pack before calling this. This function only performs the
    status flip; it does not (and cannot, from here) verify the anchor.

    Same WHERE-pins-the-FROM-state reasoning as the other two transitions:
    ``status = 'partial'`` means a ``creating`` row can never be
    accidentally promoted by this call -- only a row someone already gave
    up on.
    """
    from sqlalchemy import text

    from opencrab.execution._sql import now_expr

    with sql._engine.begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE packs SET status = 'ready', updated_at = {now_expr(sql)} "
                "WHERE pack_id = :pid AND owner_id = :oid AND status = 'partial'"
            ),
            {"pid": pack_id, "oid": owner_id},
        )
        return result.rowcount > 0


def list_incomplete_packs(sql: Any) -> list[dict[str, Any]]:
    """Every registry row whose ``status`` is not ``ready`` -- the
    ``packs repair-registry`` CLI's candidate set. Unscoped by owner (this
    is an operator tool, not a principal-facing read path) and expected to
    be structurally tiny: a healthy deployment never accumulates more than
    a handful of ``creating``/``partial`` rows at once.
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM packs WHERE status <> 'ready'")  # noqa: S608
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def readable_pack_ids(sql: Any, principal: Principal) -> set[str]:
    """``{status = 'ready'} ∩ ({owner_id = principal} ∪ {visibility != 'private'})``
    (#143 invariant 3, narrowed by #170). Always a concrete set -- there is
    no way to call this and get back "everything, unfiltered"; that state
    must be unrepresentable.

    The parentheses around the owner/visibility disjunction are load-bearing,
    not stylistic: SQL's ``AND`` binds tighter than ``OR``, so
    ``status = 'ready' AND owner_id = :uid OR visibility != 'private'``
    (no parens) would parse as
    ``(status = 'ready' AND owner_id = :uid) OR (visibility != 'private')``
    -- every public pack leaking through regardless of its status, a
    fail-open hole in exactly the row class (``creating``/``partial``) this
    filter exists to hide.
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT pack_id FROM packs "
                "WHERE status = 'ready' AND (owner_id = :uid OR visibility != 'private')"
            ),
            {"uid": principal.user_id},
        ).fetchall()
    return {r[0] for r in rows}


def list_packs_for(sql: Any, principal: Principal) -> list[dict[str, Any]]:
    """Full registry rows readable by ``principal`` -- same predicate as
    ``readable_pack_ids``, kept as one query instead of N ``get_pack``
    round-trips.

    See ``readable_pack_ids`` for why the parentheses around the
    owner/visibility disjunction are load-bearing (operator precedence,
    not style).
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM packs "  # noqa: S608
                "WHERE status = 'ready' AND (owner_id = :uid OR visibility != 'private')"
            ),
            {"uid": principal.user_id},
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def assert_writable(
    sql: Any,
    principal: Principal,
    pack_id: str,
    *,
    allowed_statuses: tuple[str, ...] = (PACK_STATUS_READY,),
) -> dict[str, Any]:
    """Raise unless ``principal`` owns ``pack_id`` AND its ``status`` is one
    of ``allowed_statuses``. Returns the pack row on success (saves the
    caller a second ``get_pack`` call).

    The status check runs FIRST -- immediately after the ``pack is None``
    check, before the owner/visibility branches -- and on failure raises
    ``PackNotFoundError`` regardless of ownership (#170, design v4 §3.3). An
    incomplete pack (``creating``/``partial``) must be unobservable, full
    stop: if it instead raised ``PackForbiddenError`` for a non-owner it
    would confirm the pack's existence to someone who has no business
    knowing a slug is in use (#143 invariant 7, "존재 누출 금지" -- the same
    invariant that already folds "no such row" and "someone else's private
    row" into one exception below).

    ``allowed_statuses`` is a set, not a boolean flag, because the one path
    that needs anything other than the default is narrow: only the pack's
    own anchor write, while the pack is ``creating``
    (``allowed_statuses=('creating',)`` -- see
    ``OntologyBuilder.add_node``'s ``pack_anchor`` parameter). A boolean
    "allow incomplete too" would also open the door for ``partial``, which
    must stay closed to every writer including the owner.

    - No such row, OR its status is not in ``allowed_statuses``, OR it is a
      private row owned by someone else -> ``PackNotFoundError``. All three
      must be indistinguishable to the caller (#143 invariant 7).
    - A ``ready``-eligible visible (public-read/public-fork) row owned by
      someone else -> ``PackForbiddenError``. Safe to distinguish from "not
      found" because the pack's existence is already observable to anyone
      (it shows up in ``content_pack_list``, which only ever lists ``ready``
      rows).
    - Owned by ``principal`` (any visibility) and status-eligible -> returns
      the row.
    """
    pack = get_pack(sql, pack_id)
    if pack is None:
        raise PackNotFoundError(pack_id)
    if pack["status"] not in allowed_statuses:
        raise PackNotFoundError(pack_id)
    if pack["owner_id"] == principal.user_id:
        return pack
    if pack["visibility"] == "private":
        raise PackNotFoundError(pack_id)
    raise PackForbiddenError(pack_id)


def set_visibility(sql: Any, principal: Principal, pack_id: str, visibility: str) -> dict[str, Any]:
    """Owner-only. Returns the updated row.

    Raises ``ValueError`` for an unrecognised ``visibility`` -- checked
    BEFORE the ownership lookup so a typo'd value fails the same way
    regardless of who is calling (no existence/ownership signal leaks
    through a validation-order side channel).
    """
    if visibility not in VISIBILITIES:
        raise ValueError(f"invalid visibility {visibility!r}; must be one of {VISIBILITIES}")
    assert_writable(sql, principal, pack_id)

    from sqlalchemy import text

    from opencrab.execution._sql import now_expr

    with sql._engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE packs SET visibility = :vis, updated_at = {now_expr(sql)} "
                "WHERE pack_id = :pid"
            ),
            {"vis": visibility, "pid": pack_id},
        )
    pack = get_pack(sql, pack_id)
    assert pack is not None  # just wrote it inside this same function
    return pack
