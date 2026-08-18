"""writer 2: one stamp, doc row first, vector second (#148)."""

from __future__ import annotations

from typing import Any

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.pack.source_writer import write_source
from opencrab.pack.write_gate import ClientIdentityFieldError

ALICE = Principal(user_id="user_alice", is_local=False, disabled=False)


class _Docs:
    def __init__(self, available=True, raises=False):
        self.available = available
        self._raises = raises
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def upsert_source(self, source_id, text, metadata):
        if self._raises:
            raise RuntimeError("doc store exploded")
        self.calls.append((source_id, text, dict(metadata)))
        return source_id


class _Hybrid:
    def __init__(self, status="ok (id=s1)"):
        self._status = status
        self.calls: list[dict[str, Any]] = []

    def ingest(self, text, source_id, metadata=None):
        self.calls.append({"text": text, "source_id": source_id, "metadata": metadata})
        return {"source_id": source_id, "stores": {"chromadb": self._status}}


def _write(docs, hybrid, **kw):
    with principal_scope(ALICE):
        return write_source(
            hybrid, docs, text="t", source_id="s1", pack_id="pack-a", **kw
        )


def test_stamps_pack_and_user_on_both_writes():
    docs, hybrid = _Docs(), _Hybrid()
    receipt = _write(docs, hybrid)
    assert receipt["metadata"]["pack_id"] == "pack-a"
    assert receipt["metadata"]["user_id"] == "user_alice"
    # The doc row and the vector must carry the SAME stamp, not two copies
    # that can drift.
    assert docs.calls[0][2]["pack_id"] == "pack-a"
    assert hybrid.calls[0]["metadata"]["pack_id"] == "pack-a"
    assert hybrid.calls[0]["metadata"]["user_id"] == "user_alice"


def test_user_id_is_assigned_not_merely_defaulted():
    """The free-tier quota counts on this key; a caller must not own it."""
    with pytest.raises(ClientIdentityFieldError):
        _write(_Docs(), _Hybrid(), metadata={"user_id": "someone_else"})


def test_matching_user_id_passes():
    receipt = _write(_Docs(), _Hybrid(), metadata={"user_id": "user_alice"})
    assert receipt["metadata"]["user_id"] == "user_alice"


def test_default_space_is_filled():
    """Without it the FTS space filter silently drops the source (#52/#110)."""
    receipt = _write(_Docs(), _Hybrid())
    assert receipt["metadata"]["space"] == "evidence"


def test_caller_space_is_kept():
    receipt = _write(_Docs(), _Hybrid(), metadata={"space": "resource"})
    assert receipt["metadata"]["space"] == "resource"


def test_doc_row_is_written_before_the_vector():
    order: list[str] = []

    class Docs(_Docs):
        def upsert_source(self, source_id, text, metadata):
            order.append("doc")
            return super().upsert_source(source_id, text, metadata)

    class Hybrid(_Hybrid):
        def ingest(self, text, source_id, metadata=None):
            order.append("vector")
            return super().ingest(text, source_id, metadata)

    _write(Docs(), Hybrid())
    assert order == ["doc", "vector"]


def test_vector_unavailable_still_records_the_source():
    """The regression an earlier vector-first draft would have shipped: on a
    deployment with no vector store, ingest became a silent no-op."""
    docs = _Docs()
    receipt = _write(docs, _Hybrid(status="unavailable"))
    assert docs.calls, "the source row must be written even with no vector store"
    assert receipt["stores"]["documents"].startswith("ok")
    assert receipt["stores"]["chromadb"] == "unavailable"


def test_doc_error_stops_the_vector_write():
    """A vector row for a source that failed to record is an orphan."""
    hybrid = _Hybrid()
    receipt = _write(_Docs(raises=True), hybrid)
    assert receipt["stores"]["documents"].startswith("error:")
    assert receipt["stores"]["chromadb"] == "skipped (source record failed)"
    assert hybrid.calls == []


def test_doc_unavailable_does_not_stop_the_vector_write():
    """Unavailable is a deployment shape, not a failure."""
    hybrid = _Hybrid()
    receipt = _write(_Docs(available=False), hybrid)
    assert receipt["stores"]["documents"] == "unavailable"
    assert hybrid.calls, "vector-only deployments must still ingest"


def test_pack_id_is_required():
    with pytest.raises(TypeError):
        with principal_scope(ALICE):
            write_source(_Hybrid(), _Docs(), text="t", source_id="s1")


def test_requires_a_bound_principal():
    with pytest.raises(LookupError):
        write_source(
            _Hybrid(), _Docs(), text="t", source_id="s1", pack_id="pack-a"
        )
