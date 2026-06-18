"""Text/slug helpers."""

from __future__ import annotations

import re

_ASCII_PATTERN = re.compile(r"[^a-z0-9]+")
_HANGUL_PATTERN = re.compile(r"[^a-z0-9가-힣]+")


def slugify(value: str, *, allow_hangul: bool = False, fallback: str = "pack") -> str:
    """Lowercase ``value`` and collapse runs of disallowed chars into ``-``.

    Consolidates the near-identical slug helpers that diverged across the
    codebase. Callers preserve their previous behaviour by choosing the params:

    * ``mcp.tools._slugify``       -> ``slugify(value, fallback="pack")``
    * ``landscape.adapter._slug``  -> ``slugify(value, fallback="item")``
    * ``import_obsidian_vault``    -> ``slugify(value, allow_hangul=True, fallback="node")``

    With ``allow_hangul=False`` (default) Hangul is stripped, matching the
    historical MCP/landscape behaviour; with ``allow_hangul=True`` it is kept.
    """
    pattern = _HANGUL_PATTERN if allow_hangul else _ASCII_PATTERN
    slug = pattern.sub("-", value.lower()).strip("-")
    return slug or fallback
