"""The write gate's three rules: boundary rejection, stamping, slot classification (#148)."""

from __future__ import annotations

import pytest

from opencrab.auth import Principal
from opencrab.pack.write_gate import (
    EDGE_STAMPED,
    NODE_STAMPED,
    SOURCE_STAMPED,
    ClientIdentityFieldError,
    boundary_identity_violations,
    by_id_conflict,
    classify_by_id_rows,
    normalize_tags,
    reject_boundary_identity,
    stamp,
)

ALICE = Principal(user_id="user_alice", is_local=False, disabled=False)


# ---------------------------------------------------------------------------
# Layer 1: external request boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["tenant_id", "subject_id", "created_by", "owner_id", "user_id"]
)
def test_boundary_rejects_reserved_key_in_properties(key):
    with pytest.raises(ClientIdentityFieldError) as exc:
        reject_boundary_identity({"properties": {key: "whatever"}})
    assert key in str(exc.value)


def test_boundary_rejects_regardless_of_value():
    """Unlike the stamp rule, the boundary refuses even the server's own value.

    A caller that happens to guess right still has no business naming who it
    is; accepting it makes the rule value-dependent and unteachable.
    """
    with pytest.raises(ClientIdentityFieldError):
        reject_boundary_identity({"properties": {"owner_id": ALICE.user_id}})


def test_boundary_walks_nested_lists_and_dicts():
    args = {"nodes": [{"properties": {"ok": 1}}, {"properties": {"owner_id": "x"}}]}
    assert boundary_identity_violations(args) == ["nodes[1].properties.owner_id"]


def test_boundary_ignores_non_payload_dicts():
    """Only `properties`/`metadata` are caller-authored persisted payloads."""
    assert boundary_identity_violations({"filters": {"owner_id": "x"}}) == []


def test_boundary_allows_pack_id():
    """pack_id names the write target, not the writer -- the gate checks it."""
    reject_boundary_identity({"properties": {"pack_id": "pack-a"}})


# ---------------------------------------------------------------------------
# Layer 2: stamping
# ---------------------------------------------------------------------------


def test_stamp_assigns_server_values():
    out = stamp({"title": "t"}, principal=ALICE, pack_id="pack-a", keys=NODE_STAMPED)
    assert out == {"title": "t", "pack_id": "pack-a", "owner_id": "user_alice"}


def test_stamp_does_not_mutate_input():
    payload = {"title": "t"}
    stamp(payload, principal=ALICE, pack_id="pack-a", keys=NODE_STAMPED)
    assert payload == {"title": "t"}


def test_stamp_accepts_none_payload():
    assert stamp(None, principal=ALICE, pack_id="p", keys=EDGE_STAMPED) == {
        "pack_id": "p"
    }


def test_stamp_passes_matching_value():
    """Server-side callers pre-fill pack_id; an equal value needs no bypass."""
    out = stamp(
        {"pack_id": "pack-a"}, principal=ALICE, pack_id="pack-a", keys=NODE_STAMPED
    )
    assert out["pack_id"] == "pack-a"


def test_stamp_rejects_mismatched_client_value():
    with pytest.raises(ClientIdentityFieldError) as exc:
        stamp(
            {"pack_id": "someone-elses"},
            principal=ALICE,
            pack_id="pack-a",
            keys=NODE_STAMPED,
        )
    assert "pack_id" in str(exc.value)


def test_stamp_server_origin_overwrites_instead_of_rejecting():
    """The loader replays dumps this server wrote; refusing them fails reload."""
    out = stamp(
        {"owner_id": "user_bob"},
        principal=ALICE,
        pack_id="pack-a",
        keys=NODE_STAMPED,
        origin="server",
    )
    assert out["owner_id"] == "user_alice"


def test_source_stamp_carries_user_id():
    """The free-tier quota reads metadata.user_id and nothing else."""
    out = stamp({}, principal=ALICE, pack_id="pack-a", keys=SOURCE_STAMPED)
    assert out == {"pack_id": "pack-a", "user_id": "user_alice"}


def test_stamp_leaves_created_by_alone():
    """created_by is a provenance sentinel here, not an identity."""
    out = stamp(
        {"created_by": "title-backfill"},
        principal=ALICE,
        pack_id="pack-a",
        keys=NODE_STAMPED,
    )
    assert out["created_by"] == "title-backfill"


def test_stamp_runs_before_alias_normalisation():
    """apply_pack_tag rewrites pack_id in place; stamping after it sees nothing.

    Pinning the order here because the bypass it prevents is invisible in the
    call site: normalise-then-stamp silently accepts a forged pack_id.
    """
    forged = {"pack_id": "someone-elses", "pack": "someone-elses"}
    with pytest.raises(ClientIdentityFieldError):
        stamp(forged, principal=ALICE, pack_id="pack-a", keys=NODE_STAMPED)


def test_normalize_tags_drops_duplicate_alias_after_stamp():
    tags = stamp(
        {"pack": "pack-a"}, principal=ALICE, pack_id="pack-a", keys=NODE_STAMPED
    )
    normalize_tags(tags)
    assert "pack" not in tags
    assert tags["pack_id"] == "pack-a"


# ---------------------------------------------------------------------------
# Identity slot classification
# ---------------------------------------------------------------------------


def test_classify_absent():
    assert classify_by_id_rows([], "pack-a") == "absent"


def test_classify_own():
    rows = [{"pack_id": "pack-a", "node_type": "Document"}]
    assert classify_by_id_rows(rows, "pack-a") == "own"


def test_classify_foreign():
    rows = [{"pack_id": "pack-b", "node_type": "Concept"}]
    assert classify_by_id_rows(rows, "pack-a") == "foreign"


def test_classify_unattributed():
    assert classify_by_id_rows([{"node_type": "Document"}], "pack-a") == "unattributed"
    assert classify_by_id_rows([{"pack_id": ""}], "pack-a") == "unattributed"


def test_classify_own_wins_over_foreign():
    rows = [{"pack_id": "pack-b"}, {"pack_id": "pack-a"}]
    assert classify_by_id_rows(rows, "pack-a") == "own"


def test_classify_mixed_unattributed_and_foreign_is_foreign():
    """The exact hole LIMIT 1 left: an unattributed row winning the draw."""
    rows = [{"node_type": "Document"}, {"pack_id": "pack-b"}]
    assert classify_by_id_rows(rows, "pack-a") == "foreign"
    assert by_id_conflict(rows, "pack-a") is True


def test_classify_rejects_non_list():
    with pytest.raises(TypeError):
        classify_by_id_rows({"pack_id": "pack-a"}, "pack-a")


def test_classify_rejects_non_mapping_row():
    with pytest.raises(TypeError):
        classify_by_id_rows(["pack-a"], "pack-a")


# ---------------------------------------------------------------------------
# Promoted identity guard (#146 -> #148)
# ---------------------------------------------------------------------------


class _Graph:
    """Graph double honouring the slot contract the guard probes."""

    available = True

    def __init__(self, exact=None, by_id=None, edge=None):
        self._exact = exact
        self._by_id = by_id or []
        self._edge = edge

    def get_node(self, node_type, node_id):  # noqa: ARG002
        return self._exact

    def get_nodes_by_id(self, node_id):  # noqa: ARG002
        return list(self._by_id)

    def get_edge(self, *args):  # noqa: ARG002
        return self._edge


class _Slot:
    """docs/vector double: one optional row keyed however the guard asks."""

    available = True

    def __init__(self, row=None):
        self._row = row

    def get_node_doc(self, space, node_id):  # noqa: ARG002
        return self._row

    def get_by_id(self, doc_id):  # noqa: ARG002
        return self._row

    def get_source(self, source_id):  # noqa: ARG002
        return self._row


def _node_conflict(graph=None, docs=None, vector=None, pack_id="pack-a"):
    from opencrab.pack.write_gate import node_identity_conflict

    return node_identity_conflict(
        graph or _Graph(), docs or _Slot(), vector or _Slot(),
        space="subject", node_type="User", node_id="u1", pack_id=pack_id,
    )


def test_free_identity_passes():
    assert _node_conflict() is None


def test_exact_graph_slot_owned_elsewhere_is_rejected():
    assert _node_conflict(graph=_Graph(exact={"pack_id": "pack-b"})) == "foreign"


def test_doc_slot_owned_elsewhere_is_rejected():
    """Knowing the graph slot is free proves nothing about the doc slot --
    the builder overwrites both in the same call."""
    docs = _Slot({"properties": {"pack_id": "pack-b"}})
    assert _node_conflict(docs=docs) == "foreign"


def test_vector_slot_owned_elsewhere_is_rejected():
    """The vector store keys on node_id alone, with no pack predicate."""
    vector = _Slot({"metadata": {"pack_id": "pack-b"}})
    assert _node_conflict(vector=vector) == "foreign"


def test_by_id_axis_rejects_a_foreign_row_under_another_type():
    assert _node_conflict(graph=_Graph(by_id=[{"pack_id": "pack-b"}])) == "foreign"


def test_by_id_axis_passes_the_owners_own_row():
    graph = _Graph(exact={"pack_id": "pack-a"}, by_id=[{"pack_id": "pack-a"}])
    assert _node_conflict(graph=graph) is None


def test_missing_probe_method_is_fail_closed():
    class Bare:
        available = True

    assert _node_conflict(graph=Bare()) == "unverifiable"


def test_by_id_returning_a_non_list_is_fail_closed():
    class Weird(_Graph):
        def get_nodes_by_id(self, node_id):  # noqa: ARG002
            return {"pack_id": "pack-a"}

    assert _node_conflict(graph=Weird()) == "unverifiable"


def test_unavailable_store_is_skipped_not_failed():
    class Down:
        available = False

    assert _node_conflict(docs=Down(), vector=Down()) is None


def test_reject_message_never_names_the_other_pack():
    from opencrab.pack.write_gate import identity_reject_message

    msg = identity_reject_message("node", "u1", "foreign")
    assert "pack-b" not in msg and "u1" in msg
