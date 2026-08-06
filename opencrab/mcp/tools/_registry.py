"""@tool registry — the single source for MCP tool name/schema/handler/order.

Handlers live in the sibling submodules (``graph.py`` / ``query.py`` /
``pack.py`` / ``schema.py`` / ``harness.py``) and register themselves via
``@tool(name, schema, order=N)``. The package ``__init__.py`` imports those
modules, then derives TOOLS / TOOL_SCHEMAS / _TOOL_FUNCTIONS / dispatch_tool
from ``_REGISTRY`` here. ``order`` pins the golden TOOLS ordering (which is
interleaved across modules — see ``tool()``'s docstring); duplicate names and
order collisions both raise at import time.
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
    # NOTE: `writes` means "needs the cross-process write.lock", NOT "touches a
    # store". See `tool()`'s docstring — a tool can INSERT and still be
    # writes=False if serialising it behind the lock would cost more (a
    # read-shaped, high-frequency path) than the write is worth protecting
    # (billing_events via ontology_query is the concrete example, decided in
    # issue #65's review; issue #105 corrected the idempotency rationale
    # originally recorded here — see `tool()`'s docstring below).
    writes: bool = False


def tool(
    name: str, schema: dict[str, Any], *, order: int | None = None, writes: bool = False
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register `fn` as the handler for MCP tool `name`, with its tools/list schema.

    `order` pins the position in ``build_tools()``'s output explicitly. Needed
    because the golden tool order (tests/test_tool_registry_contract.py) is
    interleaved across handler modules (e.g. graph.py, query.py, graph.py,
    query.py, ...) — plain decoration/import order can't reproduce that once
    handlers live in separate files, since each module executes top-to-bottom
    as one unit. When omitted, falls back to registration (insertion) order,
    which is sufficient for ad-hoc/test registrations.

    `writes=True` means *this handler needs the cross-process write.lock held
    for its call* — it is NOT a "does this handler ever touch a store" flag.
    `dispatch_tool` reads it to decide whether to wrap the call in
    `_write_lock()` (see `opencrab.mcp.tools.WRITE_TOOLS`, which is *derived*
    from this flag rather than hand-maintained — issue #65: a hand-maintained
    WRITE_TOOLS set silently missed two write handlers).

    The lock exists to serialise concurrent writers across processes so they
    don't race each other. `ontology_query` -> `billing.on_query()` ->
    `billing_events` INSERT is the one handler that skips it despite writing:
    NOT because that insert is idempotent (issue #105: it isn't — each call
    mints a fresh event_id, so the table's UNIQUE(event_id) + INSERT OR
    IGNORE / ON CONFLICT DO NOTHING only dedupes a literal double-send of the
    same event_id, and can never resurrect one that failed to persist), but
    because forcing writes=True here would serialise every `ontology_query`
    call — a read-shaped, high-frequency path — behind the single
    cross-process mutex for no correctness gain, purely to protect a billing
    side-effect. The real cost of that choice: this billing insert is
    best-effort. If a long-running write.lock holder outlasts
    `BillingHooks.emit`'s own lock-contention retries (see
    opencrab/billing/hooks.py), the usage event is lost — observably (an
    `ok: False` return / `emit_failure_count`), not silently, but lost.
    Reviewed against issue #68 (E-4, lock ownership map) in #65's review
    round. If a future handler performs a store mutation whose loss under
    contention is NOT acceptable, it must be `writes=True` regardless of how
    "read-shaped" the tool's name looks (this is exactly how #65 was missed
    for ontology_impact/ontology_lever_simulate).
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} already registered")
        resolved_order = order if order is not None else len(_REGISTRY)
        collision = next((s.name for s in _REGISTRY.values() if s.order == resolved_order), None)
        if collision is not None:
            raise ValueError(f"tool {name!r} order={resolved_order} collides with {collision!r}")
        _REGISTRY[name] = ToolSpec(name=name, schema=schema, fn=fn, order=resolved_order, writes=writes)
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
    shared write_lock for tools declared `writes=True` (see `tool()`).
    """
    # Import from the package (__init__.py) itself, NOT the opencrab.mcp.tools._shared
    # shim submodule: importing a submodule for the first time binds it as an
    # attribute of the parent package under its own name, which would silently
    # overwrite __init__.py's module-level `_context` dict (same name) the moment
    # this import ran — verified empirically (tests/test_mcp.py's `_context.clear()`
    # started hitting a module object instead of a dict).
    from opencrab.mcp.tools import _write_lock

    spec = _REGISTRY.get(name)
    if spec is None:
        # order 정렬 — 물리 분할 후에도 pre-split과 동일한 목록 순서 유지.
        available = [s.name for s in sorted(_REGISTRY.values(), key=lambda s: s.order)]
        raise UnknownToolError(f"Unknown tool: '{name}'. Available: {available}")
    if spec.writes:
        with _write_lock():
            return spec.fn(**arguments)
    return spec.fn(**arguments)
