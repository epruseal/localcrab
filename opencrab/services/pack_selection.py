"""Shared pack-selection logic for the MCP and CLI query paths.

Both ``ontology_query`` (MCP) and the ``query`` CLI command derive the effective
pack filter from the same ``choose_packs`` + ``load_pack_registry`` logic.
Previously each re-implemented the ~5 lines around it with its own warning
wording, delivery channel (MCP appends to a ``pack_filter.warnings`` list; CLI
echoes to stderr) and error policy (MCP swallows exceptions and degrades; CLI
lets them propagate).

This module centralises the *decision* and emits interface-neutral warning
*codes*. Each caller maps codes to its own wording/channel via the helper
functions below, so the observable behaviour of each interface is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Interface-neutral warning codes. Callers map these to their own wording.
PACK_IDS_OVERRIDE_AUTO = "pack_ids_override_auto"
AUTO_PACK_BELOW_THRESHOLD = "auto_pack_below_threshold"
AUTO_PACK_FAILED = "auto_pack_failed"
INCLUDE_UNPACKAGED_NOOP = "include_unpackaged_noop"
# #147: one or more requested pack_ids are outside the caller's read scope.
# The wording each interface maps this to must stay generic: "no such pack"
# and "someone else's private pack" arrive here through the SAME branch and
# must stay indistinguishable in the response (#143 invariant 7).
PACK_IDS_OUT_OF_SCOPE = "pack_ids_out_of_scope"


@dataclass(frozen=True)
class PackWarning:
    code: str
    detail: str = ""  # e.g. the exception message for AUTO_PACK_FAILED


@dataclass
class PackSelection:
    # #147: never None. "The caller named no pack" resolves to their whole
    # readable scope, and an empty list means "nothing readable" -- it is
    # NOT a synonym for "unfiltered". No value of this field can express
    # "search the whole store" (#143 invariant 3).
    effective_pack_ids: list[str]
    selected_packs: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[PackWarning] = field(default_factory=list)
    # auto_pack flag *after* the pack_ids override (what MCP reports as
    # ``pack_filter.auto_pack``).
    auto_pack_active: bool = False
    # #147: always False. Kept as a field so each interface reports what
    # actually happened from one place instead of hardcoding False in three
    # response builders.
    include_unpackaged_effective: bool = False


def resolve_packs(
    question: str,
    pack_ids: list[str] | None,
    auto_pack: bool,
    include_unpackaged: bool,
    local_data_dir: str,
    *,
    scope: frozenset[str],
    raise_on_error: bool,
) -> PackSelection:
    """Resolve the effective pack filter shared by the MCP/REST/CLI query paths.

    ``scope`` is the caller's readable pack set (``opencrab.pack.read_scope``)
    and is a REQUIRED keyword argument. There is no default, because a
    default would be a way to spell "unfiltered" -- the state #143
    invariant 3 requires to be unrepresentable. Every result is bounded by
    it: an explicit ``pack_ids`` is intersected with it, and auto_pack
    chooses only from within it.

    ``raise_on_error=False`` reproduces the MCP behaviour (auto_pack failures are
    swallowed and reported as an ``AUTO_PACK_FAILED`` warning); ``True``
    reproduces the CLI behaviour (the exception propagates).
    """
    from opencrab.ontology.pack_registry import choose_packs, load_pack_registry
    from opencrab.pack.read_scope import narrow

    # Whether the CALLER named packs, kept separate from what survived the
    # intersection. Gating auto_pack on the narrowed list instead would
    # resurrect auto_pack exactly when every requested id fell outside the
    # scope -- silently answering a different question than the one asked,
    # out of packs the caller did not name.
    requested_explicit = bool(pack_ids)

    effective, dropped_any = narrow(scope, pack_ids)
    selected_packs: list[dict[str, Any]] = []
    warnings: list[PackWarning] = []

    if dropped_any:
        warnings.append(PackWarning(PACK_IDS_OUT_OF_SCOPE))

    if requested_explicit and auto_pack:
        warnings.append(PackWarning(PACK_IDS_OVERRIDE_AUTO))
        auto_pack = False

    if auto_pack:
        failed = False
        try:
            registry = load_pack_registry(local_data_dir)
            # #147: filter candidates BEFORE scoring, not after. load_pack_registry
            # scans every manifest on disk regardless of ownership, so scoring
            # first would let someone else's pack win and leave the caller with
            # nothing -- even when a pack they can read also matched.
            registry = [p for p in registry if p.pack_id in scope]
            candidates = choose_packs(question, registry, limit=1)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully (MCP) or re-raise (CLI)
            if raise_on_error:
                raise
            logger.warning("auto_pack selection failed: %s", exc)
            warnings.append(PackWarning(AUTO_PACK_FAILED, str(exc)))
            candidates = []
            failed = True
        if candidates:
            pack, score, matched = candidates[0]
            effective = [pack.pack_id]
            selected_packs.append({"pack_id": pack.pack_id, "score": score, "matched": matched})
        elif not failed:
            # Empty registry / below threshold (distinct from an exception).
            warnings.append(PackWarning(AUTO_PACK_BELOW_THRESHOLD))

    # #147: honoured nowhere anymore. Data outside every pack is outside
    # every read scope (#143 invariant 5), so this flag can only widen a
    # scope past what the principal may read. Warned on whenever it is
    # asked for -- silently ignoring it would let a caller believe legacy
    # rows were included.
    if include_unpackaged:
        warnings.append(PackWarning(INCLUDE_UNPACKAGED_NOOP))

    return PackSelection(effective, selected_packs, warnings, auto_pack, False)


# --- Interface adapters: code -> wording (channel is the caller's concern) ---

# #147: every wording below had to change or would now be a lie. There is no
# "full-store search" to fall back to (the fallback is the caller's readable
# scope), and include_unpackaged is no longer conditional on pack_ids. A
# response that describes a filter the server did not apply is worse than no
# warning at all. Both maps must define EVERY code -- the lookup below is a
# direct index, so a missing entry turns a warning into a KeyError and takes
# the request down with it.
_MCP_WARNINGS = {
    PACK_IDS_OVERRIDE_AUTO: "pack_ids provided; ignoring auto_pack",
    AUTO_PACK_BELOW_THRESHOLD: (
        "auto_pack could not select a pack above the score threshold; "
        "searching all packs you can read"
    ),
    INCLUDE_UNPACKAGED_NOOP: (
        "include_unpackaged is not honoured: reads are always scoped to the "
        "packs you can read"
    ),
    PACK_IDS_OUT_OF_SCOPE: "one or more requested pack_ids are unavailable",
}

_CLI_WARNINGS = {
    PACK_IDS_OVERRIDE_AUTO: "warning: --pack-id provided; ignoring --auto-pack.",
    AUTO_PACK_BELOW_THRESHOLD: (
        "warning: --auto-pack could not select a pack above the score threshold; "
        "searching all packs you can read."
    ),
    INCLUDE_UNPACKAGED_NOOP: (
        "warning: --include-unpackaged is not honoured; reads are always scoped "
        "to the packs you can read."
    ),
    PACK_IDS_OUT_OF_SCOPE: "warning: one or more --pack-id values are unavailable.",
}


def mcp_warning_text(warning: PackWarning) -> str:
    """MCP wording for a warning code (appended to ``pack_filter.warnings``)."""
    if warning.code == AUTO_PACK_FAILED:
        return f"auto_pack failed: {warning.detail}"
    return _MCP_WARNINGS[warning.code]


def cli_warning_text(warning: PackWarning) -> str:
    """CLI wording for a warning code (echoed to stderr)."""
    if warning.code == AUTO_PACK_FAILED:  # CLI uses raise_on_error=True, so this is unreachable
        return f"warning: auto-pack failed: {warning.detail}"
    return _CLI_WARNINGS[warning.code]
