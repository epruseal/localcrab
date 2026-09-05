"""Shared pack/owner test helpers (#181).

Kept separate from ``conftest.py`` because it is imported explicitly by the
handful of test modules that need it, not auto-applied session-wide.
"""

from __future__ import annotations

from typing import Any


def ensure_test_user(sql: Any, user_id: str) -> str:
    """Make sure a ``users`` row named ``user_id`` exists.

    #181 turned SQLite foreign-key enforcement on, so ``packs.owner_id``
    literal strings (``"alice"``, ``"someone-else-owner"``, ...) that older
    fixtures pass straight to ``create_pack``/``_insert_pack`` now need a
    matching ``users`` row or the insert raises ``IntegrityError``.

    Unlike ``opencrab.auth.create_user`` this does NOT generate a new
    ``user_id`` -- it inserts the caller-supplied literal as-is, so existing
    assertions pinned to that literal keep working. It also does not create
    a default pack (``ensure_default_pack``): callers that need this helper
    plant their own pack rows, and a surprise default pack would corrupt
    snapshot-style assertions (e.g. "exactly one pack exists for this
    owner"). Idempotent (``ON CONFLICT ... DO NOTHING``) because the same
    literal owner is reused across fixtures within a session.
    """
    from sqlalchemy import text

    with sql._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, display_name, is_local) "
                "VALUES (:uid, :uid, 0) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"uid": user_id},
        )
    return user_id
