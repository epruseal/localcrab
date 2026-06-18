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


@dataclass(frozen=True)
class PackWarning:
    code: str
    detail: str = ""  # e.g. the exception message for AUTO_PACK_FAILED


@dataclass
class PackSelection:
    effective_pack_ids: list[str] | None
    selected_packs: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[PackWarning] = field(default_factory=list)
    # auto_pack flag *after* the pack_ids override (what MCP reports as
    # ``pack_filter.auto_pack``).
    auto_pack_active: bool = False


def resolve_packs(
    question: str,
    pack_ids: list[str] | None,
    auto_pack: bool,
    include_unpackaged: bool,
    local_data_dir: str,
    *,
    raise_on_error: bool,
) -> PackSelection:
    """Resolve the effective pack filter shared by the MCP/CLI query paths.

    ``raise_on_error=False`` reproduces the MCP behaviour (auto_pack failures are
    swallowed and reported as an ``AUTO_PACK_FAILED`` warning); ``True``
    reproduces the CLI behaviour (the exception propagates).
    """
    from opencrab.ontology.pack_registry import choose_packs, load_pack_registry

    effective: list[str] | None = list(pack_ids) if pack_ids else None
    selected_packs: list[dict[str, Any]] = []
    warnings: list[PackWarning] = []

    if effective and auto_pack:
        warnings.append(PackWarning(PACK_IDS_OVERRIDE_AUTO))
        auto_pack = False

    if auto_pack:
        failed = False
        try:
            registry = load_pack_registry(local_data_dir)
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

    if include_unpackaged and not effective:
        warnings.append(PackWarning(INCLUDE_UNPACKAGED_NOOP))

    return PackSelection(effective, selected_packs, warnings, auto_pack)


# --- Interface adapters: code -> wording (channel is the caller's concern) ---

_MCP_WARNINGS = {
    PACK_IDS_OVERRIDE_AUTO: "pack_ids provided; ignoring auto_pack",
    AUTO_PACK_BELOW_THRESHOLD: (
        "auto_pack could not select a pack above the score threshold; "
        "falling back to full-store search"
    ),
    INCLUDE_UNPACKAGED_NOOP: "include_unpackaged has no effect without pack_ids/auto_pack",
}

_CLI_WARNINGS = {
    PACK_IDS_OVERRIDE_AUTO: "warning: --pack-id provided; ignoring --auto-pack.",
    AUTO_PACK_BELOW_THRESHOLD: (
        "warning: --auto-pack could not select a pack above the score threshold; "
        "falling back to a full-store search."
    ),
    INCLUDE_UNPACKAGED_NOOP: (
        "warning: --include-unpackaged has no effect without --pack-id or --auto-pack."
    ),
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
