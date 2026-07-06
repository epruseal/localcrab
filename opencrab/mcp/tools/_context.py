"""Shared plumbing shim — transitional layout note (R9 / Stage 7).

Re-exports the shared context/lock/sanitisation helpers from the package
(where they currently still live, in ``__init__.py``) rather than physically
hosting them here. A real move must happen in the SAME change that repoints
every internal call site in ``__init__.py`` AND every test that does
``patch("opencrab.mcp.tools._get_context")`` (etc.) to the new location —
otherwise those patches silently stop taking effect (see ``__init__.py``'s
module docstring for the mock.patch namespace-binding reasoning, verified
empirically). Deferred to the G-agent migration.
"""

from __future__ import annotations

from opencrab.mcp.tools import (
    WRITE_TOOLS,
    _acquire_chroma_shared_lock,
    _clean_meta,
    _clean_str,
    _get_context,
    _lock_data_dir,
    _nine_space_hint,
    _slugify,
    _write_lock,
)

__all__ = [
    "WRITE_TOOLS",
    "_acquire_chroma_shared_lock",
    "_clean_meta",
    "_clean_str",
    "_get_context",
    "_lock_data_dir",
    "_nine_space_hint",
    "_slugify",
    "_write_lock",
]
