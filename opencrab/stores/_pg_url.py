"""PostgreSQL URL driver normalization.

SQLAlchemy 2.1 changed the default DBAPI for a bare ``postgresql://`` URL
from psycopg2 to psycopg (v3). This project installs only
``psycopg2-binary`` and never ``psycopg``, so an unnormalized URL makes
``create_engine()`` raise ``ModuleNotFoundError: No module named 'psycopg'``
on any environment that resolves SQLAlchemy 2.1.x, regardless of whether a
PostgreSQL server is even reachable.

``normalize_pg_url`` closes this gap at every ``create_engine()`` call site
in this project (see ``docs/gate-recipes.md`` for the full call-site list).
"""

from __future__ import annotations

from sqlalchemy.engine import make_url


def normalize_pg_url(url: str) -> str:
    """Return ``url`` with an explicit psycopg2 driver for bare PostgreSQL URLs.

    A URL that already names a driver (``postgresql+psycopg2://``,
    ``postgresql+asyncpg://``, ...) or that targets a non-PostgreSQL
    dialect (``sqlite://``, ...) is returned unchanged, as the original
    string object, so it stays byte-identical.

    A bare ``postgresql://`` or ``postgres://`` URL is rewritten to
    ``postgresql+psycopg2://``. The rewritten string preserves the
    password, multi-host authority, and query-string carrier keys, but is
    not guaranteed to be byte-identical to a hand-written equivalent:
    ``sqlalchemy.engine.URL.render_as_string()`` may reorder query keys
    and re-encode values during that rewrite.
    """
    parsed = make_url(url)
    if parsed.drivername not in ("postgresql", "postgres"):
        return url
    return parsed.set(drivername="postgresql+psycopg2").render_as_string(
        hide_password=False
    )
