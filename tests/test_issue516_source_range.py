"""#516 G5/G6: query provenance and source range agree on one row judgment.

Both entry points read a scoped exact row and hand it to the single G1
``validate_source_row_payload``. These cases give both the same scoped-reader
row and require the same accept or reject decision.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest

from opencrab.mcp import tools
from opencrab.mcp.tools import query as mcp_query
from opencrab.ontology import query as query_mod
from opencrab.stores._source_integrity import strict_utf8_digest

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
