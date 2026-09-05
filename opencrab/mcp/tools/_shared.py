"""Shared plumbing shim (Stage 10 rename of ``_context.py``).

Re-exports the shared context/lock/sanitisation helpers from the package
(``__init__.py``, where they still physically live) rather than hosting them
here. A real move would need to happen in the same change that repoints
every internal call site AND every test that does
``patch("opencrab.mcp.tools._get_context")`` (etc.) to the new location —
otherwise those patches silently stop taking effect (see ``__init__.py``'s
module docstring for the mock.patch namespace-binding reasoning, verified
empirically). No code currently imports this submodule directly (only the
package's own re-exported names are used); it exists as a stable landing
spot for a future physical move.
"""

from __future__ import annotations

from opencrab.mcp.tools import (
    WRITE_TOOLS,
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
    "_clean_meta",
    "_clean_str",
    "_get_context",
    "_lock_data_dir",
    "_nine_space_hint",
    "_slugify",
    "_write_lock",
]
