"""#516 G6: ``ontology_get_source_range`` over actual query locators.

The roundtrip cases take their locator from the real ``ontology_query`` path
and reassemble the stored source from range calls. The contract cases drive the
real handler against a real doc store.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from opencrab.services import result_caps as caps
from opencrab.stores._source_integrity import strict_utf8_digest
from tests import test_query_result_caps as _caps
from tests.test_query_result_caps import BIG, PACK, _ask, _by_id, _node, _write

# Reuse the real-store fixture from the query cap tests. Assigning it keeps the
# fixture visible to pytest without an import that the test arguments shadow.
stack = _caps.stack

pytestmark = pytest.mark.usefixtures("bind_test_principal")


def _range(stack, locator: Any, offset: Any, length: Any) -> dict[str, Any]:
    from opencrab.mcp import tools

    with patch.object(tools, "_get_context", return_value=stack["ctx"]):
        return tools.ontology_get_source_range(locator, offset, length)


@pytest.mark.parametrize("case", ["graphless", "display-id", "multilingual"])
def test_utf8_byte_roundtrip_from_actual_query(stack, case):
    if case == "graphless":
        source_id = node_id = "J" * 300
        text = "graphless zzrt " + BIG
        receipt = _write(stack, text, source_id)
        assert receipt["stores"]["graph"].startswith("skipped")
    elif case == "display-id":
        source_id, node_id = "src:other", "src:real"
        text = "other zzrt " + BIG
        _node(stack, node_id, BIG)
        _write(stack, text, source_id, node_id=node_id)
    else:
        source_id = node_id = "src:multi"
        text = "multi zzrt " + "한글🙂 " * 4000
        _write(stack, text, source_id)

    response = _ask(stack, "zzrt", use_bm25=False, use_fts=False)
    locator = _by_id(response, node_id)["truncation"]["source_locator"]
    assert locator["source_id"] == source_id
    assert locator["source_digest"] == strict_utf8_digest(text)

    raw = text.encode("utf-8")
    collected = b""
    offset = 0
    while True:
        length = 2500
        while True:
            chunk = _range(stack, locator, offset, length)
            if chunk.get("error") == "invalid_source_range" and length > 1:
                length -= 1  # the cut fell inside a code point
                continue
            break
        assert "error" not in chunk, chunk
        assert chunk["source_id"] == source_id and chunk["offset_bytes"] == offset
        assert chunk["total_bytes"] == len(raw)
        assert chunk["source_digest"] == locator["source_digest"]
        piece = chunk["text"].encode("utf-8")  # strict UTF-8 by construction
        assert piece == raw[offset:offset + chunk["length_bytes"]]
        assert caps._wire_length(chunk) <= caps.QUERY_RESPONSE_CHAR_BUDGET
        collected += piece
        offset += chunk["length_bytes"]
        if offset < len(raw):
            assert chunk["next_offset_bytes"] == offset
        else:
            assert "next_offset_bytes" not in chunk
            break
    assert collected == raw


def _one_source(stack, source_id: str = "src:one", text: str = "한글 range text") -> dict[str, Any]:
    stack["docs"].upsert_source(source_id, text, {"pack_id": PACK, "space": "evidence"})
    return {
        "version": "1",
        "source_id": source_id,
        "pack_id": PACK,
        "source_digest": strict_utf8_digest(text),
    }


@pytest.mark.parametrize(
    "case",
    [
        "bad-locator-shape", "bad-version", "bool-offset", "bool-length", "negative-offset",
        "zero-length", "past-end", "inside-code-point", "end-inside-code-point",
        "missing-source", "other-pack", "stale-digest",
    ],
)
def test_range_rejects_scope_stale_and_invalid_bounds(stack, case):
    locator = _one_source(stack)
    offset, length = 0, 100
    expected = None
    if case == "bad-locator-shape":
        locator = {"version": "1", "source_id": "src:one"}
        expected = "invalid_source_locator"
    elif case == "bad-version":
        locator = {**locator, "version": "2"}
        expected = "invalid_source_locator"
    elif case == "bool-offset":
        offset, expected = True, "invalid_source_range"
    elif case == "bool-length":
        length, expected = True, "invalid_source_range"
    elif case == "negative-offset":
        offset, expected = -1, "invalid_source_range"
    elif case == "zero-length":
        length, expected = 0, "invalid_source_range"
    elif case == "past-end":
        offset, expected = 10_000, "invalid_source_range"
    elif case == "inside-code-point":
        offset, expected = 1, "invalid_source_range"  # byte 1 of the first Korean letter
    elif case == "end-inside-code-point":
        length, expected = 1, "invalid_source_range"
    elif case == "missing-source":
        locator = {**locator, "source_id": "src:none"}
        expected = "source_not_found"
    elif case == "other-pack":
        locator = {**locator, "pack_id": "not-the-pack"}
        expected = "source_not_found"
    else:
        locator = {**locator, "source_digest": "f" * 64}
        expected = "stale_source_locator"
    assert _range(stack, locator, offset, length) == {"error": expected}


def test_range_hides_a_source_outside_the_read_scope(stack):
    locator = _one_source(stack)
    stack["docs"].upsert_source("src:out", "hidden", {"pack_id": "outside-pack"})
    hidden = {
        "version": "1",
        "source_id": "src:out",
        "pack_id": "outside-pack",
        "source_digest": strict_utf8_digest("hidden"),
    }
    assert _range(stack, hidden, 0, 10) == {"error": "source_not_found"}
    assert _range(stack, locator, 0, 100)["text"] == "한글 range text"


def test_range_refuses_an_unknown_source_state(stack):
    locator = _one_source(stack)
    stack["docs"]._exec_write(
        "UPDATE doc_source_integrity SET source_integrity=:value WHERE source_id=:sid",
        {"sid": "src:one", "value": '{"version": 1, "state": "unknown"}'},
    )
    assert _range(stack, locator, 0, 100) == {"error": "full_text_unavailable"}
