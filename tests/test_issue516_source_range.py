"""#516 G5/G6: query provenance and source range agree on one row judgment.

Both entry points read a scoped exact row and hand it to the single G1
``validate_source_row_payload``. These cases give both the same scoped-reader
row and require the same accept or reject decision.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any
from unittest.mock import patch

import pytest

from opencrab.mcp import tools
from opencrab.mcp.tools import query as mcp_query
from opencrab.ontology import query as query_mod
from opencrab.stores._source_integrity import strict_utf8_digest
from tests import _issue516_flow as flow

pytestmark = pytest.mark.usefixtures("bind_test_principal")

TEXT = "exact source text 한글"
PACK = "p1"


def _row() -> dict[str, Any]:
    digest = strict_utf8_digest(TEXT)
    return {
        "source_id": "s1",
        "text": TEXT,
        "metadata": {"pack_id": PACK},
        "_source_classification": {
            "source_id": "s1",
            "text": TEXT,
            "pack_id": PACK,
            "text_digest": digest,
            "complete": True,
            "state_kind": "complete",
        },
    }


def _mutated(**classification: Any) -> dict[str, Any]:
    row = _row()
    row["_source_classification"].update(classification)
    return row


class _Reader:
    def __init__(self, row: Any) -> None:
        self.row = copy.deepcopy(row)

    def get_source_scoped_exact(self, source_id: str, pack_ids: list[str]) -> Any:
        return copy.deepcopy(self.row)


def _range(reader: _Reader, digest: str | None = None) -> dict[str, Any]:
    locator = {
        "version": "1",
        "source_id": "s1",
        "pack_id": PACK,
        "source_digest": digest or strict_utf8_digest(TEXT),
    }
    ctx = {"mongo": reader}
    with (
        patch.object(tools, "_get_context", return_value=ctx),
        patch.object(tools, "_current_read_scope", return_value={PACK}),
    ):
        return tools.ontology_get_source_range(locator, 0, 1000)


CASES = [
    pytest.param(_row(), True, id="complete"),
    pytest.param(_mutated(state_kind="legacy_absent"), True, id="legacy-absent"),
    pytest.param(_mutated(state_kind="unknown"), False, id="forged-unknown"),
    pytest.param(_mutated(state_kind="malformed"), False, id="forged-malformed"),
    pytest.param(_mutated(state_kind="unsupported_version"), False, id="forged-unsupported"),
    pytest.param(_mutated(state_kind="present_null"), False, id="forged-present-null"),
    pytest.param(_mutated(state_kind=["complete"]), False, id="state-kind-list"),
    pytest.param(_mutated(complete=1), False, id="complete-not-true"),
    pytest.param(_mutated(complete=False), False, id="incomplete"),
    pytest.param(_mutated(source_id="other"), False, id="classification-id"),
    pytest.param(_mutated(text="other text"), False, id="classification-text"),
    pytest.param(_mutated(pack_id="other"), False, id="classification-pack"),
    pytest.param(_mutated(text_digest="0" * 64), False, id="classification-digest"),
]


@pytest.mark.parametrize(("row", "allowed"), CASES)
def test_query_range_row_payload_agree(row, allowed):
    reader = _Reader(row)
    proof = query_mod._source_provenance(
        reader, "s1", TEXT, permitted_packs={PACK}
    )
    response = _range(reader)
    assert (proof is not None) is allowed
    if allowed:
        assert proof == {
            "source_id": "s1",
            "pack_id": PACK,
            "source_digest": strict_utf8_digest(TEXT),
        }
        assert response["text"] == TEXT
    else:
        assert response == {"error": "full_text_unavailable"}


def test_query_provenance_rejects_a_changed_result_body():
    # The row is valid, but the result body differs from the stored body.
    reader = _Reader(_row())
    assert query_mod._source_provenance(
        reader, "s1", TEXT + " cut", permitted_packs={PACK}
    ) is None


def test_range_reports_a_changed_digest_as_stale():
    assert _range(_Reader(_row()), digest="f" * 64) == {"error": "stale_source_locator"}


def test_query_and_range_use_the_shared_validator(monkeypatch):
    from opencrab.stores import _source_integrity as shared

    calls: list[str] = []
    real = shared.validate_source_row_payload

    def recording(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("expected_source_id", ""))
        return real(*args, **kwargs)

    monkeypatch.setattr(query_mod, "validate_source_row_payload", recording)
    monkeypatch.setattr(mcp_query, "validate_source_row_payload", recording)
    reader = _Reader(_row())
    assert query_mod._source_provenance(reader, "s1", TEXT, permitted_packs={PACK})
    assert _range(reader)["text"] == TEXT
    assert calls == ["s1", "s1"]



# ---- appended to tests/test_issue516_source_range.py ----

_LP = "p1"


def _legacy_text(length: int) -> str:
    return ("legacy " * 700)[:length]


def _seed_row(stack, source_id: str, text: str, raw: Any = "absent") -> None:
    """Store one row through the raw layer of its backend. ``raw`` is the helper state."""
    meta = {"pack_id": _LP, "space": "evidence", "node_id": source_id}
    backend = stack["backend"]
    docs = stack["docs"]
    if backend == "local":
        row = {"source_id": source_id, "text": text, "metadata": meta}
        if raw != "absent":
            row["source_integrity"] = raw
        docs._save("sources", {source_id: row})
    elif backend == "sql":
        if raw == "absent":
            docs._exec_write(
                "INSERT INTO doc_sources VALUES (:sid, :text, :meta, '2026-01-01')",
                {"sid": source_id, "text": text, "meta": json.dumps(meta)},
            )
        else:
            docs.upsert_source(source_id, text, meta)
            value = raw if isinstance(raw, str) or raw is None else json.dumps(raw)
            docs._exec_write(
                "UPDATE doc_source_integrity SET source_integrity=:value WHERE source_id=:sid",
                {"value": value, "sid": source_id},
            )
    else:
        doc = {"source_id": source_id, "text": text, "metadata": meta}
        if raw != "absent":
            doc["source_integrity"] = raw
        stack["harness"].doc = doc


LEGACY_CELLS = [
    pytest.param("local", 4095, True, id="local-4095"),
    pytest.param("local", 4096, False, id="local-4096"),
    pytest.param("local", 4097, False, id="local-4097"),
    pytest.param("sql", 4097, True, id="sql-4097"),
    pytest.param("mongo", 4096, True, id="mongo-4096"),
    pytest.param("mongo", 4097, True, id="mongo-4097"),
]


@pytest.mark.parametrize(("backend", "length", "allowed"), LEGACY_CELLS)
def test_legacy_boundary_query_range(tmp_path, monkeypatch, backend, length, allowed):
    """A helper-free legacy row follows the backend policy through direct read, state,
    scoped read, the real query and the range handler. Mongo cells use a mocked collection."""
    source_id, text = "legacy-row", _legacy_text(length)
    stack = flow.build_stack(tmp_path, _LP, backend, document={"source_id": "placeholder", "text": "x"})
    try:
        _seed_row(stack, source_id, text)
        docs = stack["docs"]
        state = docs.read_source_state_exact(source_id)
        assert state is not None and state.complete is allowed
        scoped = docs.get_source_scoped_exact(source_id, [_LP])
        assert scoped is not None and scoped["text"] == text
        assert scoped["_source_classification"]["complete"] is allowed
        assert docs.get_source_scoped_exact(source_id, ["other"]) is None
        item = flow.query_rows(stack, monkeypatch, _LP, {source_id: text})[source_id]
        truncation = item["truncation"]
        if allowed:
            locator = truncation["source_locator"]
            assert locator["source_id"] == source_id
            chunk = flow.range_call(stack, locator, 64)
            assert chunk["text"] == text[:64] and chunk["total_bytes"] == len(text.encode("utf-8"))
        else:
            assert truncation["full_text_unavailable"] is True
            assert "source_locator" not in truncation and "full_text_tool" not in truncation
            assert flow.range_call(stack, flow.hand_locator(_LP, source_id, text)) == {"error": "full_text_unavailable"}
    finally:
        flow.close_stack(stack)


def _complete_state(text: str) -> dict[str, Any]:
    return {"version": 1, "state": "complete", "text_digest": hashlib.sha256(text.encode("utf-8")).hexdigest()}


BACKEND_CELLS = [
    pytest.param("sql", None, "present_null", id="sql-present-null"),
    pytest.param("sql", "{not json", "malformed", id="sql-malformed"),
    pytest.param("sql", "UNSUPPORTED", "unsupported_version", id="sql-unsupported"),
    pytest.param("mongo", None, "present_null", id="mongo-present-null"),
    pytest.param("mongo", "EXTRA", "malformed", id="mongo-malformed"),
    pytest.param("mongo", "UNSUPPORTED", "unsupported_version", id="mongo-unsupported"),
]


@pytest.mark.parametrize(("backend", "raw", "kind"), BACKEND_CELLS)
def test_backend_reader_raw_state_query_range(tmp_path, monkeypatch, backend, raw, kind):
    """Raw helper states must never read as complete. Each state goes through a real SQL reader or a mocked
    Mongo reader, then through the real query and the range handler."""
    source_id, text = "state-row", "state text " * 500
    if raw == "UNSUPPORTED":
        raw = {**_complete_state(text), "version": 2}
    elif raw == "EXTRA":
        raw = {**_complete_state(text), "extra": 1}
    stack = flow.build_stack(tmp_path, _LP, backend, document={"source_id": "placeholder", "text": "x"})
    try:
        _seed_row(stack, source_id, text, raw=raw)
        docs = stack["docs"]
        state = docs.read_source_state_exact(source_id)
        assert state is not None and state.complete is False
        scoped = docs.get_source_scoped_exact(source_id, [_LP])
        assert scoped is not None
        assert scoped["_source_classification"]["complete"] is False
        assert scoped["_source_classification"]["state_kind"] == kind
        item = flow.query_rows(stack, monkeypatch, _LP, {source_id: text})[source_id]
        truncation = item["truncation"]
        assert truncation["full_text_unavailable"] is True
        assert "source_locator" not in truncation and "full_text_tool" not in truncation
        assert flow.range_call(stack, flow.hand_locator(_LP, source_id, text)) == {"error": "full_text_unavailable"}
    finally:
        flow.close_stack(stack)
