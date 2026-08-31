"""Tool catalog discovery: the ``tool_search`` bootstrap surface (#135).

Pure registry introspection -- no stores, no ``_get_context``, no billing
side effects (which is what justifies ``AccessTier.READ`` here, unlike
``ontology_query``'s WRITE tier). The handler therefore has none of the
mock.patch namespace concerns the store-backed handler modules document.

Import-position constraint: the package ``__init__.py`` must import this
module ALONGSIDE the other handler submodules, BEFORE the derived snapshots
(``TOOLS = build_tools()`` / ``TOOL_SCHEMAS`` / ``_TOOL_FUNCTIONS`` /
``WRITE_TOOLS``) are computed -- those are one-shot snapshots, so a
registration that happened after them would be invisible there while still
being dispatchable, and the golden contract test would not see it either.
"""

from __future__ import annotations

from typing import Any

from ._registry import AccessTier, get_tool_catalog, tool

_ACCESS_VALUES = ("read", "write", "admin")

_NOTE = (
    "invoke via standard tools/call with the exact name; "
    "search grants no execution rights"
)


@tool(
    "tool_search",
    {
        # Deliberately does NOT contain the substring "pack": the
        # query="pack" ordering snapshot in tests/test_tool_catalog_search.py
        # pins the match list, and this tool must stay out of it (#135
        # design v4 [R3-2]).
        "description": (
            "Search the MCP tool catalog by case-insensitive substring over tool names "
            "and descriptions. Returns the current catalog fingerprint and matching "
            "tool metadata; grants no execution rights."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                # Every property allows explicit null: the handler treats
                # JSON null as "omitted" (sibling-handler convention), and
                # the PUBLIC schema must say so or schema-validating clients
                # would reject calls the server accepts ([R3-1]).
                "query": {
                    "type": ["string", "null"],
                    "description": (
                        "Case-insensitive substring matched against tool names and "
                        "descriptions. Empty or null returns the whole visible catalog."
                    ),
                },
                "access": {
                    "type": ["string", "null"],
                    "description": (
                        "Filter by access tier: 'read', 'write' or 'admin'. "
                        "Null means no filter."
                    ),
                },
                "include_schema": {
                    "type": ["boolean", "null"],
                    "description": (
                        "When true, embed each matching tool's inputSchema. "
                        "Default (false/null) omits schemas."
                    ),
                },
                "limit": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "Maximum number of tools to return. Null means unbounded.",
                },
            },
            "required": [],
        },
    },
    order=18,
    access=AccessTier.READ,
)
def tool_search(
    query: str | None = None,
    access: str | None = None,
    include_schema: bool | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Search the calling principal's visible tool catalog (#135).

    Identity comes exclusively from ``current_principal()`` (server-derived,
    #145); there is no principal parameter on the MCP surface. Matching is a
    deterministic case-insensitive substring scan: name matches first, then
    description-only matches, each group in catalog (registry ``order``)
    sequence. Results carry the catalog fingerprint so a caller can detect a
    stale earlier response; they grant no execution rights -- invocation
    still goes through ``tools/call`` and ``dispatch_tool``'s own gate.
    """
    # Explicit JSON null == omitted for every parameter (design [R2-1]).
    # Non-null values are strictly type-checked here because the server
    # deliberately does not validate tool inputSchema ([R1-3]).
    if query is None:
        query = ""
    if not isinstance(query, str):
        raise ValueError("'query' must be a string (or null).")
    if access is not None and (not isinstance(access, str) or access not in _ACCESS_VALUES):
        raise ValueError("'access' must be one of 'read', 'write', 'admin' (or null).")
    if include_schema is None:
        include_schema = False
    if not isinstance(include_schema, bool):
        raise ValueError("'include_schema' must be a boolean (or null).")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("'limit' must be an integer >= 1 (or null).")

    from opencrab.auth import current_principal

    catalog = get_tool_catalog(current_principal())
    pool = [e for e in catalog["tools"] if access is None or e["access"] == access]
    q = query.lower()
    if q:
        name_hits = [e for e in pool if q in e["name"].lower()]
        desc_hits = [
            e for e in pool if q not in e["name"].lower() and q in e["description"].lower()
        ]
        matched = name_hits + desc_hits
    else:
        matched = pool

    total = len(matched)
    if limit is not None:
        matched = matched[:limit]

    tools_out: list[dict[str, Any]] = []
    for entry in matched:
        item: dict[str, Any] = {
            "name": entry["name"],
            "description": entry["description"],
            "access": entry["access"],
            "requires_write_lock": entry["requires_write_lock"],
        }
        if include_schema:
            item["inputSchema"] = entry["inputSchema"]
        tools_out.append(item)

    return {
        "catalog_version": catalog["fingerprint"],
        "generated_at": catalog["generated_at"],
        "total_matched": total,
        "returned": len(tools_out),
        "tools": tools_out,
        "note": _NOTE,
    }
