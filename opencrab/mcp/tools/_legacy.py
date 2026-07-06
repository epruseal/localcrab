"""Compatibility shim — stable import path only.

Tool handlers live in the sibling submodules (``graph.py`` / ``query.py`` /
``pack.py`` / ``schema.py`` / ``harness.py``); shared plumbing
(``_get_context``, the ``_context`` cache dict, locks, sanitizers) stays in
the package ``__init__.py`` so ``patch("opencrab.mcp.tools.<name>")`` keeps
working (handlers late-import those names from the package at call time).
This file just re-exports everything so ``opencrab.mcp.tools._legacy``
remains importable for older callers.
"""

from __future__ import annotations

from opencrab.mcp.tools import *  # noqa: F401,F403
from opencrab.mcp.tools import (
    _TOOL_FUNCTIONS,
    TOOL_SCHEMAS,
    TOOLS,
    WRITE_TOOLS,
    UnknownToolError,
    _acquire_chroma_shared_lock,
    _clean_meta,
    _clean_str,
    _context,
    _get_context,
    _lock_data_dir,
    _nine_space_hint,
    _slugify,
    _write_lock,
    dispatch_tool,
)

__all__ = [
    "TOOLS",
    "TOOL_SCHEMAS",
    "UnknownToolError",
    "WRITE_TOOLS",
    "_TOOL_FUNCTIONS",
    "_acquire_chroma_shared_lock",
    "_clean_meta",
    "_clean_str",
    "_context",
    "_get_context",
    "_lock_data_dir",
    "_nine_space_hint",
    "_slugify",
    "_write_lock",
    "dispatch_tool",
]
