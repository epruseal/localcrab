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
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencrab.auth import Principal

VISIBILITIES = ("private", "public-read", "public-fork")

_SELECT_COLS = "pack_id, owner_id, visibility, title, description, forked_from, created_at, updated_at"

# _insert_pack retries pack_id, pack_id-2, pack_id-3, ... this many times
# before falling back to a random-suffixed slug (see create_pack).
_MAX_SUFFIX_ATTEMPTS = 50


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
        "created_at": str(row[6]) if row[6] is not None else None,
        "updated_at": str(row[7]) if row[7] is not None else None,
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


def _insert_pack(
    sql: Any,
    pack_id: str,
    owner_id: str,
    title: str | None,
    description: str | None,
    forked_from: str | None,
) -> bool:
    """Attempt one INSERT. True on success, False if pack_id is already
    taken (PK violation). A plain SELECT-then-INSERT would race two
    concurrent ``pack_create`` calls onto the same slug; letting the PK's
    UNIQUE constraint be the single point of truth avoids that TOCTOU
    window entirely.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    try:
        with sql._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO packs (pack_id, owner_id, visibility, title, description, forked_from) "
                    "VALUES (:pid, :oid, 'private', :title, :desc, :forked)"
                ),
                {
                    "pid": pack_id,
                    "oid": owner_id,
                    "title": title,
                    "desc": description,
                    "forked": forked_from,
                },
            )
        return True
    except IntegrityError:
        return False


def create_pack(
    sql: Any,
    owner_id: str,
    pack_id: str,
    title: str | None = None,
    description: str | None = None,
    forked_from: str | None = None,
) -> str:
    """Insert a new pack registry row owned by ``owner_id``. Returns the
    pack_id actually assigned -- which may differ from the requested
    ``pack_id``.

    When ``pack_id`` is already taken (by anyone, including another user),
    this quietly appends ``-2``, ``-3``, ... and retries rather than
    erroring (#143 invariant 7: an error naming "already exists" would
    tell the caller someone else already holds that exact slug -- see
    ``pack_create`` in ``opencrab/mcp/tools/pack.py`` for the caller side
    of this contract, and pack_id format is never changed, only
    suffixed). Falls back to a random 8-hex-char suffix after
    ``_MAX_SUFFIX_ATTEMPTS`` sequential slugs are all taken, so this
    always terminates even under a flood of identically-titled packs.
    """
    if _insert_pack(sql, pack_id, owner_id, title, description, forked_from):
        return pack_id
    for n in range(2, _MAX_SUFFIX_ATTEMPTS + 2):
        candidate = f"{pack_id}-{n}"
        if _insert_pack(sql, candidate, owner_id, title, description, forked_from):
            return candidate
    candidate = f"{pack_id}-{secrets.token_hex(4)}"
    if _insert_pack(sql, candidate, owner_id, title, description, forked_from):
        return candidate
    raise RuntimeError(f"could not allocate a unique pack_id for {pack_id!r}")


def readable_pack_ids(sql: Any, principal: Principal) -> set[str]:
    """``{owner_id = principal} ∪ {visibility != 'private'}`` (#143
    invariant 3). Always a concrete set -- there is no way to call this
    and get back "everything, unfiltered"; that state must be
    unrepresentable.
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(
            text("SELECT pack_id FROM packs WHERE owner_id = :uid OR visibility != 'private'"),
            {"uid": principal.user_id},
        ).fetchall()
    return {r[0] for r in rows}


def list_packs_for(sql: Any, principal: Principal) -> list[dict[str, Any]]:
    """Full registry rows readable by ``principal`` -- same predicate as
    ``readable_pack_ids``, kept as one query instead of N ``get_pack``
    round-trips.
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {_SELECT_COLS} FROM packs "  # noqa: S608
                "WHERE owner_id = :uid OR visibility != 'private'"
            ),
            {"uid": principal.user_id},
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def assert_writable(sql: Any, principal: Principal, pack_id: str) -> dict[str, Any]:
    """Raise unless ``principal`` owns ``pack_id``. Returns the pack row on
    success (saves the caller a second ``get_pack`` call).

    - No such row, OR a private row owned by someone else -> ``PackNotFoundError``.
      The two must be indistinguishable to the caller (#143 invariant 7).
    - A visible (public-read/public-fork) row owned by someone else ->
      ``PackForbiddenError``. Safe to distinguish from "not found" because the
      pack's existence is already observable to anyone (it shows up in
      ``content_pack_list``).
    - Owned by ``principal`` (any visibility) -> returns the row.
    """
    pack = get_pack(sql, pack_id)
    if pack is None:
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
