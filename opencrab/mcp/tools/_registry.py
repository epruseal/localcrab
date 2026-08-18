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
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opencrab.pack.write_gate import boundary_identity_violations

if TYPE_CHECKING:
    from opencrab.auth import Principal

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ToolSpec] = {}


class AccessTier(StrEnum):
    """Exposure/permission tier for MCP tool listing and dispatch (#150).

    A DIFFERENT axis from ``ToolSpec.writes``: `writes` means "needs the
    cross-process write.lock", not "read vs write vs admin" (see that
    field's own docstring). A tool can be `writes=False` and still be
    WRITE- or ADMIN-tier here — `ontology_query` is the concrete example:
    `writes=False` (no lock needed, its billing insert lives in its own
    SQLite file) but it is NOT the side-effect-free read its name suggests,
    so it is WRITE-tier, not READ-tier (#150's test pins this).

    READ  -- no mutation of any kind.
    WRITE -- mutates graph/doc/vector/SQL state, including a read-shaped
             tool with a write side effect (ontology_query's billing insert).
    ADMIN -- mutates the host filesystem or process-global schema state
             (schema_pack_install/uninstall write YAML under
             schemas/types/; harness_promotion_apply applies a promotion
             package). See `allowed_access_tiers` for who gets to see/call
             ADMIN-tier tools.
    """

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict[str, Any]
    fn: Callable[..., Any]
    order: int
    # Required, no default (#150): a tool registered without an access tier
    # must fail at import time (like a duplicate name / order collision
    # below), not silently default to some tier. See `tool()`'s `access`
    # parameter, which is what actually enforces "no default".
    access: AccessTier
    # NOTE: `writes` means "needs the cross-process write.lock", NOT "touches a
    # store". See `tool()`'s docstring — a tool can INSERT and still be
    # writes=False if serialising it behind the lock would cost more (a
    # read-shaped, high-frequency path) than the write is worth protecting
    # (billing_events via ontology_query is the concrete example, decided in
    # issue #65's review; issue #105 corrected the idempotency rationale
    # originally recorded here — see `tool()`'s docstring below).
    writes: bool = False


def tool(
    name: str,
    schema: dict[str, Any],
    *,
    access: AccessTier,
    order: int | None = None,
    writes: bool = False,
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
    same event_id, and can never resurrect one that failed to persist), and
    NOT because losing a billing event would be an acceptable trade for
    query throughput — protecting billing IS a correctness goal, which is
    the entire reason issue #105 exists. `writes=False` is safe here because
    the contention this lock would otherwise be needed for DOESN'T HAPPEN:
    billing_events lives in its own SQLite file (`billing.db`, local/kuzu
    mode — see `opencrab.stores.factory.make_billing_sql_store`), separate
    from the write.lock'd tables' file (`opencrab.db`). SQLite's write lock
    is per-file, so a billing insert never contends with an unrelated write
    there no matter how long that write holds `write.lock`. (An earlier
    version of this fix instead retried the insert with backoff on lock
    contention while still sharing the file — that only shrank the failure
    window and blocked the request thread doing it; see
    `opencrab.billing.hooks`'s module docstring for the full analysis,
    including why WAL would not have been sufficient either.) Reviewed
    against issue #68 (E-4, lock ownership map) in #65's review round. If a
    future handler performs a store mutation that a shared file's write
    lock is the only thing protecting, it must be `writes=True` regardless
    of how "read-shaped" the tool's name looks (this is exactly how #65 was
    missed for ontology_impact/ontology_lever_simulate).

    `access` (#150) is required and keyword-only with NO default: a missing
    value fails the call with ``TypeError`` before ``deco`` even runs, i.e.
    at import time, the same way a duplicate name or an order collision does
    below — a default would let a new tool register with an unreviewed tier
    instead of failing loudly. See ``AccessTier`` for what each tier means
    and ``allowed_access_tiers`` for how a tier maps to who can see/call it.
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} already registered")
        resolved_order = order if order is not None else len(_REGISTRY)
        collision = next((s.name for s in _REGISTRY.values() if s.order == resolved_order), None)
        if collision is not None:
            raise ValueError(f"tool {name!r} order={resolved_order} collides with {collision!r}")
        _REGISTRY[name] = ToolSpec(
            name=name, schema=schema, fn=fn, order=resolved_order, access=access, writes=writes
        )
        return fn

    return deco


class UnknownToolError(KeyError):
    """Raised by dispatch_tool() when `name` is not a registered tool.

    Mirrors the pre-migration ``opencrab.mcp.tools.UnknownToolError`` exactly
    (KeyError subclass, identical "Unknown tool: ... Available: ..." message)
    so the eventual cut-over is behavior-identical. Not the canonical class
    yet — ``__init__.py`` still exports the pre-migration one.
    """


class ForbiddenArgumentError(ValueError):
    """Raised by dispatch_tool() when `arguments` carries a client-supplied
    ``tenant_id`` / ``subject_id`` (#145, #143 invariant 2: principal is
    server-derived, never client-supplied). Rejected explicitly rather than
    silently stripped -- a caller whose value was quietly dropped would
    believe it had taken effect."""


def allowed_access_tiers(principal: Principal) -> frozenset[AccessTier]:
    """Access tiers *principal* may see (tools/list) and call (dispatch_tool).

    Derived from ``Principal.is_local`` -- there is deliberately no
    ``users.role`` column (#150 design decision, see the PR description for
    the full rationale). Short version: ADMIN-tier tools mutate the HOST
    FILESYSTEM or process-global schema state (schema_pack_install/
    uninstall, harness_promotion_apply). A local principal (stdio/CLI) is
    already someone who can edit those same files directly, so there is no
    privilege boundary MCP tool exposure could add for them. A remote
    (token-authenticated, ``is_local=False``) principal has no such standing
    access, so ADMIN-tier tools are withheld from it. The upgrade path, if a
    deployment ever needs more than one remote admin, is to add
    ``users.role`` then -- not to build it now for a boundary with a single
    inhabitant (the local user).

    This is a DIFFERENT axis from pack ownership (#143): pack write
    authorization is per-pack-owner and applies identically to local and
    remote principals; #143's "stdio/CLI has no admin bypass" is about that
    axis, not this one.

    Fail-closed by construction: there is no branch here that returns "all
    tiers" for anything other than ``is_local is True`` on an already
    server-derived ``Principal`` -- a caller with no bound principal never
    reaches this function (``current_principal()`` raises first, both here
    and in ``dispatch_tool``).
    """
    if principal.is_local:
        return frozenset(AccessTier)
    return frozenset({AccessTier.READ, AccessTier.WRITE})


def _tool_allowed(spec: ToolSpec, principal: Principal) -> bool:
    """Shared judgment call used, independently, by both the tools/list
    filter (`tools_for_principal`) and the tools/call gate (`dispatch_tool`)
    -- see #150: neither may be the only place this is decided."""
    return spec.access in allowed_access_tiers(principal)


def tools_for_principal(principal: Principal) -> list[dict[str, Any]]:
    """tools/list view scoped to *principal*'s access tiers (#150).

    Same tool descriptor shape as ``build_tools()``, filtered by
    ``_tool_allowed``. Computed fresh on every call (no memoisation here or
    in the caller) -- with a per-process cache keyed only by tool name, a
    remote caller's filtered list could leak from/into a local caller's
    request on the same server process. There is currently no such cache to
    keep straight: this function and ``build_tools()`` both walk
    ``_REGISTRY`` on every call.
    """
    return [
        {"name": spec.name, **spec.schema}
        for spec in sorted(_REGISTRY.values(), key=lambda s: s.order)
        if _tool_allowed(spec, principal)
    ]


# Client-supplied identity fields dispatch_tool refuses outright. The
# principal reaching a handler must come from current_principal() (bound by
# the caller -- opencrab/mcp/http_app.py's per-request token verification or
# opencrab/cli.py's stdio local-user binding), never from `arguments`.
_FORBIDDEN_ARGS = ("tenant_id", "subject_id")

# The same identities can also arrive INSIDE a payload, and rejecting only the
# top-level arguments would leave the door beside the gate wide open. That walk
# now lives in opencrab/pack/write_gate.py so REST enforces the SAME rule from
# the SAME code (#148): keeping a second copy here meant a key added on one
# side would silently diverge from the other, which is the failure mode this
# work is supposed to remove.
_reserved_identity_violations = boundary_identity_violations


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
    """Look up, authorize, and call a registered tool by name.

    #150 v3: the dispatch order is deliberate and load-bearing --

        1. principal            -- current_principal(), LookupError if unbound
        2. lookup + authorize   -- merged: a name that isn't registered and a
                                   name the principal isn't tiered for produce
                                   the SAME UnknownToolError, computed from
                                   the SAME tools_for_principal(principal)
                                   "Available" list
        3. client-argument validation -- _FORBIDDEN_ARGS / reserved-identity
        4. handler execution

    Step 2 running before step 3 is the fix: the earlier ordering validated
    arguments before checking tier, so a remote caller that already knew an
    ADMIN-tier tool's name could attach a client-supplied ``subject_id`` and
    get back ForbiddenArgumentError instead of "unknown tool" -- confirming
    the tool exists and revealing which gate rejected it. With lookup+
    authorization first, that path now dead-ends at the exact same
    UnknownToolError a genuinely unregistered name would raise.

    "The same" is intentionally an invariant, not a coincidence: a hidden
    tool (registered, but outside the caller's access tier) must be
    indistinguishable, at the HTTP response, from a name absent from
    ``_REGISTRY`` altogether -- otherwise the list filter in
    ``tools_for_principal`` is decoration, not access control. The
    "Available" list embedded in the message is themselves scoped by
    ``tools_for_principal(principal)`` (not the raw registry), so it never
    differs between the two cases either.

    This does NOT try to close every side channel: server logs (see the
    WARNING below), and dispatch timing, both still distinguish "hidden"
    from "never existed" -- accepted, documented limits (issue #150's PR
    description), not gaps in this function.

    #145: a client-supplied ``tenant_id`` / ``subject_id`` in `arguments` is
    rejected outright (ForbiddenArgumentError), and the handler itself runs
    inside ``principal_scope(current_principal())`` -- re-binding the
    *already* server-derived principal explicitly here means a handler that
    calls ``current_principal()`` never depends on the caller having
    remembered to open the scope correctly; dispatch_tool is the one place
    that must get this right. ``current_principal()`` raises LookupError if
    no principal is bound at all -- by design there is no anonymous
    fallback (#143), so every path into dispatch_tool (stdio via cli.py's
    `serve`, HTTP via http_app.py's `_check`) must have bound one first.
    Calling dispatch_tool() directly with no principal_scope open (e.g. a
    bare script, or a test that forgot to bind one) now fails on step 1
    with LookupError, before the name is even looked up -- a behavior
    change from the pre-#150 dispatch order, where an unregistered name
    would have raised UnknownToolError first regardless of principal.
    """
    # Import from the package (__init__.py) itself, NOT the opencrab.mcp.tools._shared
    # shim submodule: importing a submodule for the first time binds it as an
    # attribute of the parent package under its own name, which would silently
    # overwrite __init__.py's module-level `_context` dict (same name) the moment
    # this import ran — verified empirically (tests/test_mcp.py's `_context.clear()`
    # started hitting a module object instead of a dict).
    from opencrab.auth import current_principal, principal_scope
    from opencrab.mcp.tools import _write_lock

    # Step 1: principal. LookupError propagates as-is (#143: no anonymous
    # fallback) -- nothing below may run without one.
    principal = current_principal()

    # Step 2: lookup + authorization, merged. A caller must not be able to
    # tell "never registered" apart from "registered but not in my tier" --
    # so both produce the identical UnknownToolError, built from the
    # identical tools_for_principal(principal)-scoped Available list.
    spec = _REGISTRY.get(name)
    if spec is None or not _tool_allowed(spec, principal):
        if spec is not None:
            # Hidden, not missing: worth a server-side WARNING (principal,
            # tool, required tier) for operators -- the response itself
            # carries none of this. A genuinely unregistered name logs
            # nothing here, same as before #150.
            logger.warning(
                "hidden tool call: %r requires %r access; principal %r "
                "(is_local=%s) does not have it -- reporting as unknown",
                name, spec.access.value, principal.user_id, principal.is_local,
            )
        available = [s["name"] for s in tools_for_principal(principal)]
        raise UnknownToolError(f"Unknown tool: '{name}'. Available: {available}")

    # Step 3: client-argument validation -- only reached once the caller is
    # both known and authorized for this tool.
    forbidden = [key for key in _FORBIDDEN_ARGS if key in arguments]
    if forbidden:
        raise ForbiddenArgumentError(
            f"tool {name!r}: client-supplied {forbidden} is not allowed -- "
            "the principal is derived server-side from the caller's "
            "authenticated identity, never from tool arguments."
        )

    embedded = _reserved_identity_violations(arguments)
    if embedded:
        raise ForbiddenArgumentError(
            f"tool {name!r}: reserved identity key(s) {embedded} in the payload "
            "are not allowed -- the server derives these from the caller's "
            "authenticated identity. Rejected rather than overwritten so the "
            "caller does not believe its value was stored."
        )

    # Step 4: execute.
    with principal_scope(principal):
        if spec.writes:
            with _write_lock():
                return spec.fn(**arguments)
        return spec.fn(**arguments)
