"""writer 1 (the builder) actually enforces the gate, not just imports it (#148).

Written because a mutation survived: removing the *effect* of `authorize`
from `add_node`/`add_edge` while leaving the name in place changed nothing in
the suite -- the only thing guarding that call was an AST name-presence check,
and the source writer's behavioural tests cover the other writer, not this one.

Every test here fails if the builder stops authorizing, stops stamping, or
starts telling a caller which of "no such pack" and "someone else's private
pack" they hit.
"""

from __future__ import annotations

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack.ownership import (
    PackForbiddenError,
    PackNotFoundError,
    create_pack,
    set_visibility,
)
from opencrab.pack.write_gate import ClientIdentityFieldError

ALICE = Principal(user_id="user_alice", is_local=False, disabled=False)
BOB = Principal(user_id="user_bob", is_local=False, disabled=False)


class _Graph:
    """Graph double honouring the slot contract the identity guard probes."""

    available = True

    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}
        self.edges: list[dict] = []

    def get_node(self, node_type, node_id):
        return self.nodes.get((node_type, node_id))

    def get_nodes_by_id(self, node_id):
        return [v for (_t, i), v in sorted(self.nodes.items()) if i == node_id]

    existing_edge = None

    def get_edge(self, from_type, from_id, relation, to_type, to_id):  # noqa: ARG002
        # Keyed on the REAL node types, like every backend's upsert conflict
        # key. A double that ignored its arguments would hide a probe called
        # with the wrong ones.
        if from_type != "Document" or to_type != "Document":
            return None
        return self.existing_edge

    def upsert_node(self, node_type, node_id, properties, space_id):
        self.nodes[(node_type, node_id)] = {**properties, "space": space_id}
        return dict(properties)

    def lookup_node_type(self, node_id):
        for (t, i) in self.nodes:
            if i == node_id:
                return t
        return None

    def upsert_edge(self, from_type, from_id, relation, to_type, to_id, properties):  # noqa: ARG002
        # Records the properties it is handed. A double that discarded them
        # made the stamping assertion below vacuous -- removing edge stamping
        # from the builder killed nothing.
        self.edges.append(dict(properties))
        return True


class _Docs:
    available = True

    def get_node_doc(self, space, node_id):  # noqa: ARG002
        return None

    def upsert_node_doc(self, space, node_type, node_id, properties):  # noqa: ARG002
        return "doc-1"

    def log_event(self, *a, **kw):  # noqa: ARG002
        return "ev-1"


class _Vec:
    available = True

    def get_by_id(self, doc_id):  # noqa: ARG002
        return None

    def upsert_texts(self, texts, ids, metadatas):  # noqa: ARG002
        return list(ids)


@pytest.fixture
def sql(tmp_path):
    """alice owns pack-a (private) and pack-pub (public-read); bob owns nothing."""
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
    create_pack(store, ALICE.user_id, "pack-pub")
    set_visibility(store, ALICE, "pack-pub", "public-read")
    return store


@pytest.fixture
def builder(sql):
    return OntologyBuilder(_Graph(), _Docs(), sql, vec=_Vec())


def _add(builder, principal=ALICE, pack_id="pack-a", node_id="n1", props=None,
         origin="client"):
    with principal_scope(principal):
        return builder.add_node(
            "resource", "Document", node_id, props or {"title": "t"},
            pack_id=pack_id, origin=origin,
        )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_owner_may_write(builder):
    assert _add(builder)["stores"]["graph"] == "ok"


def test_non_owner_is_refused_on_a_visible_pack(builder):
    with pytest.raises(PackForbiddenError):
        _add(builder, principal=BOB, pack_id="pack-pub")


def test_non_owner_gets_not_found_for_a_private_pack(builder):
    """#143 invariant 7: someone else's private pack looks like no pack."""
    with pytest.raises(PackNotFoundError):
        _add(builder, principal=BOB, pack_id="pack-a")


def test_missing_pack_is_the_same_error_as_a_foreign_private_one(builder):
    with pytest.raises(PackNotFoundError):
        _add(builder, pack_id="no-such-pack")


def test_refusal_writes_nothing(builder):
    graph = builder._neo4j
    with pytest.raises(PackNotFoundError):
        _add(builder, principal=BOB, pack_id="pack-a", node_id="ghost")
    assert graph.get_nodes_by_id("ghost") == []


def test_registry_unavailable_fails_closed(sql):
    """Cannot verify ownership must never read as "allowed"."""
    class Down:
        available = False

    b = OntologyBuilder(_Graph(), _Docs(), Down(), vec=_Vec())
    with pytest.raises(RuntimeError, match="registry unavailable"):
        _add(b)


def test_add_edge_authorizes_too(builder):
    _add(builder, node_id="a")
    _add(builder, node_id="b")
    with pytest.raises(PackNotFoundError), principal_scope(BOB):
        builder.add_edge("resource", "a", "cites", "resource", "b", pack_id="pack-a")


def test_an_edge_may_not_straddle_two_packs(builder, sql):
    """The cross-pack edge guard is a live refusal, so it needs a live test.

    An edge whose endpoints sit in different packs is a row its own pack's
    readers can never see -- export_edges_scoped requires BOTH endpoints in
    scope. Refusing it is the point; a guard nothing exercises is a guard
    that can be deleted by accident.
    """
    create_pack(sql, ALICE.user_id, "pack-b")
    _add(builder, node_id="a", pack_id="pack-a")
    _add(builder, node_id="b", pack_id="pack-b")
    with pytest.raises(ValueError, match="already attributed"), principal_scope(ALICE):
        builder.add_edge("resource", "a", "cites", "resource", "b", pack_id="pack-a")
    assert builder._neo4j.edges == [], "refused, so nothing may have been written"


def test_an_unattributed_endpoint_is_allowed(builder):
    """Legacy nodes carry no pack_id and the seed scripts still make them;
    refusing those would block edges over data not yet migrated."""
    _add(builder, node_id="a")
    builder._neo4j.nodes[("Document", "legacy")] = {"title": "t", "space": "resource"}
    with principal_scope(ALICE):
        out = builder.add_edge(
            "resource", "a", "cites", "resource", "legacy", pack_id="pack-a"
        )
    assert out["stores"]["graph"] == "ok"


# ---------------------------------------------------------------------------
# Stamping -- the builder is the authority for these values
# ---------------------------------------------------------------------------


def test_owner_and_pack_are_stamped(builder):
    props = _add(builder)["properties"]
    assert props["pack_id"] == "pack-a"
    assert props["owner_id"] == "user_alice"


def test_a_conflicting_client_value_is_refused(builder):
    with pytest.raises(ClientIdentityFieldError):
        _add(builder, props={"owner_id": "user_bob"})


def test_a_matching_client_value_passes(builder):
    """Server-side callers legitimately pre-fill pack_id; equal is not forged."""
    out = _add(builder, props={"title": "t", "pack_id": "pack-a"})
    assert out["properties"]["pack_id"] == "pack-a"


def test_edge_is_stamped_with_its_pack(builder):
    """The edge's own pack_id is a read-scoping predicate, not decoration --
    `export_edges_scoped` reads it. Losing the stamp silently narrows scoping
    to the endpoint checks alone."""
    _add(builder, node_id="a")
    _add(builder, node_id="b")
    with principal_scope(ALICE):
        out = builder.add_edge("resource", "a", "cites", "resource", "b", pack_id="pack-a")
    assert out["stores"]["graph"] == "ok"
    assert builder._neo4j.edges[-1]["pack_id"] == "pack-a"


# ---------------------------------------------------------------------------
# Loader replay (#148 review P2)
# ---------------------------------------------------------------------------


def test_server_origin_overwrites_a_replayed_owner(builder):
    """A pack dump can carry an owner_id a past server stamped -- restoring it
    after a re-init under a new user, say. Client-origin would call that forged
    and drop the node from the reload; server-origin overwrites it."""
    out = _add(builder, props={"title": "t", "owner_id": "user_from_a_past_life"},
               origin="server")
    assert out["properties"]["owner_id"] == "user_alice"


def test_client_origin_still_refuses_the_same_value(builder):
    """The exemption is for replay only; the client path must stay strict."""
    with pytest.raises(ClientIdentityFieldError):
        _add(builder, props={"title": "t", "owner_id": "user_from_a_past_life"})


def test_edge_slot_probe_uses_resolved_types_not_spaces(builder, sql):
    """Review finding: the edge slot probe was keyed on ontology *spaces*.

    The upsert conflict key is (from_type, from_id, relation, to_type, to_id)
    with the REAL node types, so probing with "resource" instead of "Document"
    matched nothing and the write below reattributed an existing edge to
    another pack. Measured before the fix: the takeover succeeded and the
    edge's pack_id flipped.

    Reached through UNATTRIBUTED endpoints on purpose -- those pass the
    endpoint guard by design (legacy data), which leaves this probe as the
    only thing standing.
    """
    create_pack(sql, ALICE.user_id, "pack-b")
    graph = builder._neo4j
    graph.nodes[("Document", "x")] = {"title": "t", "space": "resource"}
    graph.nodes[("Document", "y")] = {"title": "t", "space": "resource"}
    graph.existing_edge = {"pack_id": "pack-b"}

    with pytest.raises(ValueError, match="already attributed"), principal_scope(ALICE):
        builder.add_edge("resource", "x", "cites", "resource", "y", pack_id="pack-a")
    assert graph.edges == [], "refused, so the foreign edge must be untouched"
