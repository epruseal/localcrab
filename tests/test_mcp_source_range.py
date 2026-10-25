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
from tests.test_query_result_caps import (
    BIG,
    PACK,
    _ask,
    _ask_defaults,
    _by_id,
    _node,
    _write,
)

# Reuse the real-store fixture from the query cap tests. Assigning it keeps the
# fixture visible to pytest without an import that the test arguments shadow.
stack = _caps.stack

pytestmark = pytest.mark.usefixtures("bind_test_principal")


def _range(stack, locator: Any, offset: Any, length: Any) -> dict[str, Any]:
    from opencrab.mcp import tools

    with patch.object(tools, "_get_context", return_value=stack["ctx"]):
        return tools.ontology_get_source_range(locator, offset, length)


@pytest.mark.parametrize("mode", ["flat", "defaults"])
@pytest.mark.parametrize("case", ["graphless", "display-id", "multilingual"])
def test_utf8_byte_roundtrip_from_actual_query(stack, case, mode):
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

    if mode == "flat":
        response = _ask(stack, "zzrt", use_bm25=False, use_fts=False)
    else:
        # No use_* argument: rerank, bm25 and fts all run, as for a real caller.
        response = _ask_defaults(stack, "zzrt")
    truncation = _by_id(response, node_id)["truncation"]
    assert "source_locator" in truncation, truncation
    locator = truncation["source_locator"]
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


def test_range_response_over_budget_is_refused(stack, monkeypatch):
    locator = _one_source(stack)
    normal = _range(stack, locator, 0, 100)
    assert "error" not in normal
    size = caps._wire_length(normal)
    # One character under the wire length of the normal response: refused.
    monkeypatch.setattr(caps, "QUERY_RESPONSE_CHAR_BUDGET", size - 1)
    refused = _range(stack, locator, 0, 100)
    assert refused["error"] == "response_too_large"
    assert "length_bytes" in refused["message"]
    # The query response text stays as it was.
    assert refused["message"] != caps.RESPONSE_TOO_LARGE_MESSAGE
    assert caps.response_too_large_error()["message"] == caps.RESPONSE_TOO_LARGE_MESSAGE
    # Exactly the wire length: allowed (boundary control).
    monkeypatch.setattr(caps, "QUERY_RESPONSE_CHAR_BUDGET", size)
    assert _range(stack, locator, 0, 100) == normal


@pytest.mark.parametrize("value", ["01", "2", "1.0", ""])
def test_range_rejects_locator_version_values(stack, value):
    # A string that is not exactly "1" passes the string and UTF-8 checks.
    # Only the version comparison of the locator validation rejects it.
    locator = {**_one_source(stack), "version": value}
    assert _range(stack, locator, 0, 100) == {"error": "invalid_source_locator"}


@pytest.mark.parametrize("value", [True, 1, 1.0, None])
def test_range_rejects_locator_version_types(stack, value):
    # Both the version comparison and the string check reject these values, so
    # no single mutation separates them. This is a behavior boundary test.
    locator = {**_one_source(stack), "version": value}
    assert _range(stack, locator, 0, 100) == {"error": "invalid_source_locator"}


@pytest.mark.parametrize("case", ["scalar", "list-id", "int-id", "other-id"])
def test_query_and_range_require_same_scalar_source_id(case):
    """MID-02. The query proof and the range handler judge one scoped row alike."""
    from opencrab.ontology import query as query_mod
    from tests import test_issue516_source_range as rows_mod

    row = rows_mod._row()
    if case == "list-id":
        row["source_id"] = ["s1"]
    elif case == "int-id":
        row["source_id"] = 1
    elif case == "other-id":
        row["source_id"] = "s2"
        row["_source_classification"]["source_id"] = "s2"
    reader = rows_mod._Reader(row)
    proof = query_mod._source_provenance(reader, "s1", rows_mod.TEXT, permitted_packs={rows_mod.PACK})
    response = rows_mod._range(reader)
    if case == "scalar":
        assert proof is not None and response["text"] == rows_mod.TEXT
    else:
        assert proof is None
        assert response == {"error": "full_text_unavailable"}
