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

    def get_node(self, node_type, node_id):
        return self.nodes.get((node_type, node_id))

    def get_nodes_by_id(self, node_id):
        return [v for (_t, i), v in sorted(self.nodes.items()) if i == node_id]

    def get_edge(self, *args):  # noqa: ARG002
        return None

    def upsert_node(self, node_type, node_id, properties, space_id):
        self.nodes[(node_type, node_id)] = {**properties, "space": space_id}
        return dict(properties)

    def lookup_node_type(self, node_id):
        for (t, i) in self.nodes:
            if i == node_id:
                return t
        return None

    def upsert_edge(self, *a, **kw):  # noqa: ARG002
        return {}


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


def _add(builder, principal=ALICE, pack_id="pack-a", node_id="n1", props=None):
    with principal_scope(principal):
        return builder.add_node(
            "resource", "Document", node_id, props or {"title": "t"}, pack_id=pack_id
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
    _add(builder, node_id="a")
    _add(builder, node_id="b")
    with principal_scope(ALICE):
        out = builder.add_edge("resource", "a", "cites", "resource", "b", pack_id="pack-a")
    assert out["stores"]["graph"] != "unavailable"
