"""writer 2: authorize, guard the slot, one stamp, doc first, vector second (#148)."""

from __future__ import annotations

from typing import Any

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.pack.ownership import PackForbiddenError, PackNotFoundError, create_pack
from opencrab.pack.source_writer import write_source
from opencrab.pack.write_gate import ClientIdentityFieldError

ALICE = Principal(user_id="user_alice", is_local=False, disabled=False)
BOB = Principal(user_id="user_bob", is_local=False, disabled=False)


class _Docs:
    def __init__(self, available=True, raises=False, existing=None):
        self.available = available
        self._raises = raises
        self._existing = existing
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def upsert_source(self, source_id, text, metadata):
        if self._raises:
            raise RuntimeError("doc store exploded")
        self.calls.append((source_id, text, dict(metadata)))
        return source_id

    def get_source(self, source_id):  # noqa: ARG002
        return self._existing

    # #74: the graph leg's `OntologyBuilder.add_node` runs the node-level
    # identity guard against this same `docs` double (its `doc_nodes` row,
    # NOT `get_source`'s `doc_sources` row). `_check_probes` treats a missing
    # probe method on an `available` store as CONFLICT_UNVERIFIABLE and fails
    # closed, so this must exist; returning None (unattributed) keeps every
    # existing source-identity test's own `get_source`-based scenario the
    # sole source of a conflict.
    def get_node_doc(self, space, node_id):  # noqa: ARG002
        return None


class _Hybrid:
    def __init__(self, status="ok (id=s1)"):
        self._status = status
        self.calls: list[dict[str, Any]] = []

    def ingest(self, text, source_id, metadata=None):
        self.calls.append({"text": text, "source_id": source_id, "metadata": metadata})
        return {"source_id": source_id, "stores": {"chromadb": self._status}}


class _Vec:
    available = True

    def __init__(self, row=None):
        self._row = row

    def get_by_id(self, doc_id):  # noqa: ARG002
        return self._row


# #74: `write_source` now materialises an evidence/TextUnit graph node before
# doc/vector, and the graph is the system of record -- a doc/vector-only
# double is no longer enough, `write_source` raises TypeError without a
# `graph`. This minimal double gives `_graph_leg` -> `OntologyBuilder.add_node`
# just the surface it actually calls (`available`, `get_node`,
# `get_nodes_by_id`, `upsert_node`); it has no `get_node_digest`, so every
# write here takes the plain-insert path rather than the CAS reclassify one,
# which is fine since no test in this file re-writes the same source_id
# through more than one graph state.
class _Graph:
    available = True

    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}

    def get_node(self, node_type, node_id):
        return self.nodes.get((node_type, node_id))

    def get_nodes_by_id(self, node_id):
        return [v for (t, i), v in sorted(self.nodes.items()) if i == node_id]

    def upsert_node(self, node_type, node_id, properties, space_id):
        self.nodes[(node_type, node_id)] = {**properties, "space": space_id}
        return dict(properties)


@pytest.fixture
def sql(tmp_path):
    """Real registry: alice owns pack-a, bob owns nothing."""
    from sqlalchemy import text as _t

    from opencrab.stores.sql_store import SQLStore

    store = SQLStore(f"sqlite:///{tmp_path}/o.db")
    with store._engine.begin() as conn:
        for p in (ALICE, BOB):
            conn.execute(
                _t("INSERT INTO users (user_id, display_name, is_local) "
                   "VALUES (:u, :n, 0)"),
                {"u": p.user_id, "n": p.user_id},
            )
    create_pack(store, ALICE.user_id, "pack-a")
    return store


def _write(sql, docs=None, hybrid=None, vector=None, graph=None, principal=ALICE,
           pack_id="pack-a", **kw):
    with principal_scope(principal):
        return write_source(
            sql, hybrid or _Hybrid(), docs or _Docs(), vector or _Vec(),
            graph=graph if graph is not None else _Graph(),
            text="t", source_id="s1", pack_id=pack_id, **kw
        )


# ---------------------------------------------------------------------------
# Authorization -- the hole an adversarial review found in the first cut
# ---------------------------------------------------------------------------


def test_non_owner_cannot_write_into_a_visible_pack(sql):
    from opencrab.pack.ownership import set_visibility

    set_visibility(sql, ALICE, "pack-a", "public-read")
    with pytest.raises(PackForbiddenError):
        _write(sql, principal=BOB)


def test_non_owner_gets_not_found_for_a_private_pack(sql):
    """#143 invariant 7: someone else's private pack must look like no pack."""
    with pytest.raises(PackNotFoundError):
        _write(sql, principal=BOB)


def test_missing_pack_is_the_same_error_as_a_foreign_private_one(sql):
    with pytest.raises(PackNotFoundError):
        _write(sql, pack_id="no-such-pack")


def test_owner_may_write(sql):
    receipt = _write(sql)
    assert receipt["stores"]["documents"].startswith("ok")


def test_registry_unavailable_fails_closed(sql):
    class Down:
        available = False

    with pytest.raises(RuntimeError, match="registry unavailable"):
        _write(Down())


# ---------------------------------------------------------------------------
# Identity slot -- source_id is a global key on both sinks
# ---------------------------------------------------------------------------


def test_source_id_owned_by_another_pack_is_refused(sql):
    docs = _Docs(existing={"metadata": {"pack_id": "someone-elses"}})
    with pytest.raises(ValueError, match="already attributed"):
        _write(sql, docs=docs)
    assert docs.calls == [], "the foreign row must not be overwritten"


def test_vector_slot_owned_by_another_pack_is_refused(sql):
    vec = _Vec({"metadata": {"pack_id": "someone-elses"}})
    docs = _Docs()
    with pytest.raises(ValueError, match="already attributed"):
        _write(sql, docs=docs, vector=vec)
    assert docs.calls == [], "refused on the vector slot, so no doc row either"


def test_own_source_id_may_be_rewritten(sql):
    docs = _Docs(existing={"metadata": {"pack_id": "pack-a"}})
    assert _write(sql, docs=docs)["stores"]["documents"].startswith("ok")


def test_unverifiable_probe_fails_closed(sql):
    class Bare:
        available = True

    with pytest.raises(ValueError, match="cannot verify"):
        _write(sql, vector=Bare())


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------


def test_stamps_pack_and_user_on_both_writes(sql):
    docs, hybrid = _Docs(), _Hybrid()
    receipt = _write(sql, docs=docs, hybrid=hybrid)
    assert receipt["metadata"]["pack_id"] == "pack-a"
    assert receipt["metadata"]["user_id"] == "user_alice"
    # The doc row and the vector must carry the SAME stamp, not two copies
    # that can drift.
    assert docs.calls[0][2]["pack_id"] == "pack-a"
    assert hybrid.calls[0]["metadata"]["user_id"] == "user_alice"


def test_user_id_is_assigned_not_merely_defaulted(sql):
    """The free-tier quota counts on this key; a caller must not own it."""
    with pytest.raises(ClientIdentityFieldError):
        _write(sql, metadata={"user_id": "someone_else"})


def test_matching_user_id_passes(sql):
    receipt = _write(sql, metadata={"user_id": "user_alice"})
    assert receipt["metadata"]["user_id"] == "user_alice"


def test_default_space_is_filled(sql):
    """Without it the FTS space filter silently drops the source (#52/#110)."""
    assert _write(sql)["metadata"]["space"] == "evidence"


def test_caller_space_is_kept(sql):
    assert _write(sql, metadata={"space": "resource"})["metadata"]["space"] == "resource"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_doc_row_is_written_before_the_vector(sql):
    order: list[str] = []

    class Docs(_Docs):
        def upsert_source(self, source_id, text, metadata):
            order.append("doc")
            return super().upsert_source(source_id, text, metadata)

    class Hybrid(_Hybrid):
        def ingest(self, text, source_id, metadata=None):
            order.append("vector")
            return super().ingest(text, source_id, metadata)

    _write(sql, docs=Docs(), hybrid=Hybrid())
    assert order == ["doc", "vector"]


def test_vector_unavailable_still_records_the_source(sql):
    """The regression a vector-first ordering would have shipped: on a
    deployment with no vector store, ingest became a silent no-op."""
    docs = _Docs()
    receipt = _write(sql, docs=docs, hybrid=_Hybrid(status="unavailable"))
    assert docs.calls, "the source row must be written even with no vector store"
    assert receipt["stores"]["chromadb"] == "unavailable"


def test_doc_error_stops_the_vector_write(sql):
    """A vector row for a source that failed to record is an orphan."""
    hybrid = _Hybrid()
    receipt = _write(sql, docs=_Docs(raises=True), hybrid=hybrid)
    assert receipt["stores"]["documents"].startswith("error:")
    assert receipt["stores"]["chromadb"] == "skipped (source record failed)"
    assert hybrid.calls == []


def test_doc_unavailable_does_not_stop_the_vector_write(sql):
    """Unavailable is a deployment shape, not a failure."""
    hybrid = _Hybrid()
    receipt = _write(sql, docs=_Docs(available=False), hybrid=hybrid)
    assert receipt["stores"]["documents"] == "unavailable"
    assert hybrid.calls, "vector-only deployments must still ingest"


def test_pack_id_is_required(sql):
    """`graph` is ALSO keyword-required now (#74), so it must be supplied here
    -- otherwise a TypeError for the missing `pack_id` this test targets is
    indistinguishable from one for the missing `graph` this test does not."""
    with pytest.raises(TypeError), principal_scope(ALICE):
        write_source(
            sql, _Hybrid(), _Docs(), _Vec(), graph=_Graph(),
            text="t", source_id="s1",
        )


def test_requires_a_bound_principal(sql):
    with pytest.raises(LookupError):
        write_source(
            sql, _Hybrid(), _Docs(), _Vec(), graph=_Graph(),
            text="t", source_id="s1", pack_id="pack-a",
        )


# ---------------------------------------------------------------------------
# Rejections must land before any write; store failures must stay in the receipt
# ---------------------------------------------------------------------------


def test_alias_violation_rejects_before_the_doc_row_is_written(sql):
    """`HybridQuery.ingest` raises this one too, but only after the doc row is
    committed -- an earlier cut returned 422 to the client with the row
    already persisted. Check it here, before anything is written."""
    docs = _Docs()
    with pytest.raises(ValueError, match="retired alias"):
        _write(sql, docs=docs, metadata={"pack": "something-else"})
    assert docs.calls == [], "nothing may be written when the payload is rejected"


def test_vector_exception_stays_in_the_receipt(sql):
    """The receipt contract (#158) says store failures are reported, not
    raised. `ingest` can raise -- its alias check sits outside its own try on
    purpose -- so this function must absorb it."""
    class Boom(_Hybrid):
        def ingest(self, text, source_id, metadata=None):
            raise RuntimeError("embed failed")

    docs = _Docs()
    receipt = _write(sql, docs=docs, hybrid=Boom())
    assert receipt["stores"]["chromadb"] == "error: embed failed"
    assert docs.calls, "the doc row stands; only the vector leg failed"


def test_doc_unavailable_with_a_healthy_vector_is_a_successful_ingest(sql):
    """A doc-less deployment is supported: the vector leg carries the ingest.

    Review finding: the CLI treated `documents="unavailable"` as fatal and
    reported the file failed AFTER the vector had been persisted, skipping the
    audit row. The writer's own contract is what that check has to read.
    """
    from opencrab.ontology.builder import store_write_succeeded

    receipt = _write(sql, docs=_Docs(available=False), hybrid=_Hybrid())
    assert receipt["stores"]["documents"] == "unavailable"
    assert store_write_succeeded(receipt["stores"], "chromadb"), (
        "the vector leg must still land, and be readable as success"
    )
