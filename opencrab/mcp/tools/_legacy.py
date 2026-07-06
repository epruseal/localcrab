"""Compatibility shim — transitional layout note (R9 / Stage 7).

The actual tool implementations live in ``opencrab.mcp.tools`` (this
package's ``__init__.py``), not here — see that module's docstring for why
(mock.patch namespace binding: functions must live in the module literally
named ``opencrab.mcp.tools`` for ``patch("opencrab.mcp.tools.<name>")`` to
reach their internal call sites). This file exists only so
``opencrab.mcp.tools._legacy`` is a stable, importable path during the
G-agent migration; it just re-exports everything from the package.

G-agents: read/copy handler source out of ``__init__.py``, not this file.
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
