"""@tool registry scaffolding for the R9 tools.py -> tools/ package migration.

Not wired in yet: the package's TOOLS / dispatch_tool / UnknownToolError
(exported from ``__init__.py`` — see its module docstring) still come from
the pre-migration implementation. As G-agent modules extract handlers out of
``__init__.py`` into ``query.py`` / ``pack.py`` / ``schema.py`` / ``graph.py``
/ ``harness.py``, each handler registers itself here via
``@tool(name, schema)``. Once every handler has moved, ``__init__.py``
switches TOOLS/dispatch_tool/UnknownToolError over to ``build_tools()`` /
``dispatch_tool()`` below, and this module (plus a slimmed ``__init__.py``)
replaces the pre-migration implementation; ``_legacy.py`` is then deleted.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_REGISTRY: dict[str, ToolSpec] = {}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict[str, Any]
    fn: Callable[..., Any]
    order: int


def tool(name: str, schema: dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register `fn` as the handler for MCP tool `name`, with its tools/list schema."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} already registered")
        _REGISTRY[name] = ToolSpec(name=name, schema=schema, fn=fn, order=len(_REGISTRY))
        return fn

    return deco


class UnknownToolError(KeyError):
    """Raised by dispatch_tool() when `name` is not a registered tool.

    Mirrors the pre-migration ``opencrab.mcp.tools.UnknownToolError`` exactly
    (KeyError subclass, identical "Unknown tool: ... Available: ..." message)
    so the eventual cut-over is behavior-identical. Not the canonical class
    yet — ``__init__.py`` still exports the pre-migration one.
    """


def _envelope(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap `fn` so any exception becomes ``{"error": str(exc)}`` exactly once.

    Unused for now. Today's handlers already self-wrap their own
    try/except -> {"error": ...} (~23 copies in __init__.py), and
    dispatch_tool must reproduce that exact observable behavior during the
    migration. This decorator is for a LATER dedup pass, applied only once a
    handler's self-wrapping is proven behavior-identical to it.
    """

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    return wrapped


def build_tools() -> list[dict[str, Any]]:
    """Combined tool descriptor list (name + schema), in registration order."""
    return [
        {"name": spec.name, **spec.schema}
        for spec in sorted(_REGISTRY.values(), key=lambda s: s.order)
    ]


def dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Look up and call a registered tool by name.

    Thin lookup + call, matching the pre-migration dispatch_tool exactly: no
    extra error wrapping (handlers self-wrap), and write-serialising via the
    shared write_lock/WRITE_TOOLS for tools that mutate the stores.
    """
    from opencrab.mcp.tools._context import WRITE_TOOLS, _write_lock

    spec = _REGISTRY.get(name)
    if spec is None:
        raise UnknownToolError(f"Unknown tool: '{name}'. Available: {list(_REGISTRY)}")
    if name in WRITE_TOOLS:
        with _write_lock():
            return spec.fn(**arguments)
    return spec.fn(**arguments)
