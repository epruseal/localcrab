"""Size handling for the ``ontology_query`` response (#511).

Two steps. The contract is in docs/ARCHITECTURE.md, "질의 응답 크기 처리
(#511)", and the design is in docs/issue-511-query-text-cap.md.

1. Item limit: ``cap_query_item`` and ``cap_selected_pack`` cut a stored-data
   string that is longer than ``QUERY_FIELD_CHAR_LIMIT`` and record where and
   how long it was in a ``truncation`` key. Identifier places are skipped:
   cutting an identifier would name another object.
2. Response budget: ``apply_response_budget`` measures the response in the unit
   of the server's wire form. When it is over budget it drops results from the
   tail, or returns a fixed error when no result fits.

The functions do not assign into their input. A container that changes is
rebuilt. A container that does not change is returned as the same object, so
an item without a long string keeps its serialization.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from opencrab.common.json_transport import serialize_tool_result

logger = logging.getLogger(__name__)

# The graph leg's ``_property_text`` already cuts ``text`` at this length.
QUERY_FIELD_CHAR_LIMIT = 4000

# The unit is ``_wire_length``. Issue #511 reported a response of about
# 620,000 characters that the client could not take. The client's real limit
# was not measured.
QUERY_RESPONSE_CHAR_BUDGET = 500_000

_FULL_TEXT_TOOL = "ontology_get_node"
_FOLD_KEY = "folded_duplicates"
_GRAPH_CONTEXT_SKIP = ("anchor_id", "edge_endpoints", "edge")

RESPONSE_TOO_LARGE_MESSAGE = (
    f"응답 길이가 응답 예산({QUERY_RESPONSE_CHAR_BUDGET}바이트)을 넘고, 결과를 덜어 내어도 "
    "예산 안에 드는 응답을 만들 수 없다. pack_ids나 spaces로 범위를 좁히거나 limit을 "
    "줄여 다시 호출하라. 개별 노드는 ontology_get_node로 읽는다."
)


def response_too_large_error() -> dict[str, str]:
    """The fixed error body. It names neither a result count nor a cause."""
    return {"error": "response_too_large", "message": RESPONSE_TOO_LARGE_MESSAGE}


def _cap(
    value: Any,
    path: list[Any],
    limit: int,
    cuts: list[dict[str, Any]],
    skip_keys: tuple[str, ...] = (),
) -> tuple[Any, bool]:
    """Cut every ``str`` longer than ``limit`` inside ``value``.

    Returns ``(new value, changed)``. ``skip_keys`` applies to the keys of
    ``value`` itself when it is a ``dict``, not to nested dicts. Object keys
    are not cut.
    """
    if isinstance(value, str):
        if len(value) <= limit:
            return value, False
        cuts.append({"path": list(path), "original_chars": len(value)})
        return value[:limit], True
    if isinstance(value, dict):
        rebuilt: dict[Any, Any] = {}
        changed = False
        for key, child in value.items():
            if key in skip_keys:
                rebuilt[key] = child
                continue
            new_child, child_changed = _cap(child, path + [key], limit, cuts)
            rebuilt[key] = new_child
            changed = changed or child_changed
        return (rebuilt, True) if changed else (value, False)
    if isinstance(value, (list, tuple)):
        items = []
        changed = False
        for index, child in enumerate(value):
            new_child, child_changed = _cap(child, path + [index], limit, cuts)
            items.append(new_child)
            changed = changed or child_changed
        if not changed:
            return value, False
        return (tuple(items) if isinstance(value, tuple) else items), True
    return value, False


def cap_query_item(
    item: dict[str, Any],
    *,
    limit: int = QUERY_FIELD_CHAR_LIMIT,
    folded: bool = False,
) -> dict[str, Any]:
    """Cut the long strings of one ``QueryResult.to_dict()`` item.

    ``folded`` is true when the item carries a real fold block
    (``QueryResult.folded_count``). Its members are items too and get their own
    ``truncation``. A caller-written ``folded_duplicates`` value is a plain
    value when ``folded`` is false.
    """
    cuts: list[dict[str, Any]] = []
    updates: dict[str, Any] = {}

    if "text" in item:
        new_text, changed = _cap(item["text"], ["text"], limit, cuts)
        if changed:
            updates["text"] = new_text

    metadata = item.get("metadata")
    metadata_skip = (_FOLD_KEY,) if folded else ()
    new_metadata, metadata_changed = _cap(
        metadata, ["metadata"], limit, cuts, metadata_skip
    )
    if metadata_changed:
        updates["metadata"] = new_metadata

    graph_context = item.get("graph_context")
    new_context, context_changed = _cap(
        graph_context, ["graph_context"], limit, cuts, _GRAPH_CONTEXT_SKIP
    )
    if context_changed:
        updates["graph_context"] = new_context

    members_changed = False
    if folded and isinstance(new_metadata, dict):
        block = new_metadata.get(_FOLD_KEY)
        members = block.get("members") if isinstance(block, dict) else None
        if isinstance(members, list):
            new_members = [
                cap_query_item(member, limit=limit, folded=False) for member in members
            ]
            if any(new is not old for new, old in zip(new_members, members)):
                members_changed = True
                updates["metadata"] = {
                    **new_metadata,
                    _FOLD_KEY: {**block, "members": new_members},
                }

    if not cuts and not members_changed:
        return item
    out = dict(item)
    out.update(updates)
    if cuts:
        out["truncation"] = {
            "limit_chars": limit,
            "fields": cuts,
            "full_text_tool": _FULL_TEXT_TOOL,
        }
    return out


def cap_selected_pack(
    entry: dict[str, Any], *, limit: int = QUERY_FIELD_CHAR_LIMIT
) -> dict[str, Any]:
    """Cut the long strings of one ``selected_packs`` entry; ``pack_id`` stays."""
    cuts: list[dict[str, Any]] = []
    new_entry, changed = _cap(entry, [], limit, cuts, ("pack_id",))
    if not changed:
        return entry
    return {**new_entry, "truncation": {"limit_chars": limit, "fields": cuts}}


def cap_keyword_fallback(
    entry: dict[str, Any], *, limit: int = QUERY_FIELD_CHAR_LIMIT
) -> dict[str, Any]:
    """Cut non-identifier strings in one REST keyword fallback entry."""
    node = entry.get("node")
    cuts: list[dict[str, Any]] = []
    new_node, changed = _cap(
        node,
        ["node"],
        limit,
        cuts,
        ("id", "node_id", "pack_id", "space"),
    )
    if not changed:
        return entry
    out = dict(entry)
    out["node"] = new_node
    out["truncation"] = {"limit_chars": limit, "fields": cuts}
    return out


def _wire_length(value: Any) -> int:
    """Return the UTF-8 byte contribution of tool content in its outer JSON string.

    The server turns a tool result into UTF-8 JSON text. The transport serializes
    that text once more. The JSON-RPC frame, request ID and batch frame are not
    counted.
    """
    text = serialize_tool_result(value)
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def _truncated_response(
    response: dict[str, Any], k: int, n: int, offset: int = 0
) -> dict[str, Any]:
    """``R(k)``: the first ``k`` results and continuation metadata."""
    out: dict[str, Any] = {}
    for key, value in response.items():
        if key == "total":
            out[key] = k
        elif key == "results":
            out[key] = value[:k]
            out["results_truncated"] = True
            out["omitted_results"] = n - k
            out["next_offset"] = offset + k
        else:
            out[key] = value
    return out


def apply_response_budget(
    response: dict[str, Any],
    *,
    budget: int = QUERY_RESPONSE_CHAR_BUDGET,
    offset: int = 0,
    measure: Callable[[Any], int] = _wire_length,
) -> dict[str, Any]:
    """Return the response to send. See the decision table in the design doc.

    Row A: within budget, the same object. Row B: over budget and at least one
    result fits, the largest tested prefix. Row C: the fixed error.
    """
    results = response["results"]
    n = len(results)
    length = measure(response)
    if length <= budget:
        return response

    if n >= 2:
        first = _truncated_response(response, 1, n, offset)
        if measure(first) <= budget:
            fits, over = 1, n
            while over - fits > 1:
                middle = (fits + over) // 2
                if measure(_truncated_response(response, middle, n, offset)) <= budget:
                    fits = middle
                else:
                    over = middle
            chosen = _truncated_response(response, fits, n, offset)
            if measure(chosen) <= budget:
                logger.warning(
                    "ontology_query response over budget: bytes=%d results=%d kept=%d",
                    length, n, fits,
                )
                return chosen

    logger.warning(
        "ontology_query response over budget with no fitting result: bytes=%d results=%d",
        length, n,
    )
    return response_too_large_error()
