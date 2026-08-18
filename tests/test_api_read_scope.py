"""Read scoping on the REST surface (#147).

`apps/api/main.py`'s GET handlers had no test at all: the repository's
existing `/api/nodes` and `/api/edges` coverage is for the POST (write)
handlers, and nothing called the view handlers. That gap let the whole
issue-#147 change to this file be reverted -- `export_nodes_scoped` back to
`export_nodes` -- with the full suite still green while another user's
private nodes and edges appeared in the response.

The handlers are called directly with a hand-built `ApiContext` and
`AuthContext` rather than through a live app, so no server or bearer-token
plumbing is needed; what is under test is the scoping, not FastAPI.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from apps.api import main as api
from opencrab.auth import Principal, create_user
from opencrab.pack.ownership import create_pack, set_visibility

PACK_A = "pack-a"
PACK_B = "pack-b"
PACK_PUBLIC = "pack-public"


@pytest.fixture
def sql(tmp_path):
    from opencrab.stores.sql_store import SQLStore

    return SQLStore(f"sqlite:///{tmp_path / 'registry.db'}")


@pytest.fixture
def graph(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    return LocalGraphStore(str(tmp_path / "graph.db"))


@pytest.fixture
def docs(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "docs.db"))


@pytest.fixture
def world(sql, graph, docs):
    """alice and bob, one private pack each plus a shared public one."""
    alice = Principal(user_id=create_user(sql, "Alice"), is_local=False, disabled=False)
    bob = Principal(user_id=create_user(sql, "Bob"), is_local=False, disabled=False)
    create_pack(sql, alice.user_id, PACK_A, title="alice private")
    create_pack(sql, bob.user_id, PACK_B, title="bob private")
    create_pack(sql, alice.user_id, PACK_PUBLIC, title="shared")
    set_visibility(sql, alice, PACK_PUBLIC, "public-read")

    def node(pack_id: str, node_id: str) -> None:
        props = {"pack_id": pack_id, "node_id": node_id, "title": f"t {node_id}"}
        graph.upsert_node("Document", node_id, dict(props), space_id="resource")
        docs.upsert_node_doc("resource", "Document", node_id, dict(props))

    node(PACK_A, "a-doc")
    node(PACK_A, "a-doc2")
    node(PACK_B, "b-secret")
    node(PACK_B, "b-secret2")
    node(PACK_PUBLIC, "shared-doc")
    # An edge inside each private pack. Without them "no foreign edge" is
    # satisfied by an empty edge table and proves nothing.
    graph.upsert_edge("Document", "a-doc", "relates_to", "Document", "a-doc2", {})
    graph.upsert_edge("Document", "b-secret", "relates_to", "Document", "b-secret2", {})
    return {"alice": alice, "bob": bob}


@pytest.fixture
def ctx(graph, docs, sql):
    from opencrab.config import get_settings

    return api.ApiContext(
        settings=get_settings(),
        graph=graph,
        vector=MagicMock(available=False),
        docs=docs,
        sql=sql,
        hybrid=MagicMock(),
        impact=MagicMock(),
    )


def _auth(principal: Principal) -> Any:
    return api.AuthContext(user_id=principal.user_id, tier="free", principal=principal)


class TestGraphViewEndpointsAreScoped:
    def test_get_nodes_returns_own_and_public_only(self, ctx, world):
        got = {n["id"] for n in api.list_nodes(auth=_auth(world["alice"]), ctx=ctx)["nodes"]}
        # Both directions: bob's rows absent AND alice's own present. The
        # absence alone would pass on a handler that returned nothing.
        assert got == {"a-doc", "a-doc2", "shared-doc"}

    def test_get_nodes_is_symmetric_for_the_other_user(self, ctx, world):
        got = {n["id"] for n in api.list_nodes(auth=_auth(world["bob"]), ctx=ctx)["nodes"]}
        assert got == {"b-secret", "b-secret2", "shared-doc"}

    def test_get_edges_withholds_edges_with_an_unreadable_endpoint(self, ctx, world):
        edges = api.list_edges(auth=_auth(world["alice"]), ctx=ctx)["edges"]
        pairs = {(e["from_id"], e["to_id"]) for e in edges}
        assert ("a-doc", "a-doc2") in pairs
        assert ("b-secret", "b-secret2") not in pairs

    def test_degree_in_view_is_computed_from_the_scoped_edge_set(self, ctx, world):
        """`/api/nodes` also reads edges, to size nodes in the rendered view.

        That second read needs the same scope: an unscoped one would let a
        node's degree reflect edges the caller cannot see.
        """
        nodes = {n["id"]: n for n in api.list_nodes(auth=_auth(world["alice"]), ctx=ctx)["nodes"]}
        assert nodes["a-doc"]["degree_in_view"] == 1
        assert nodes["shared-doc"]["degree_in_view"] == 0


class TestStatusAndUsage:
    def test_status_requires_authentication(self):
        """It reports storage_mode and per-store availability, so it is not a
        liveness probe -- `/healthz` is, and stays unauthenticated."""
        import inspect

        params = inspect.signature(api.get_status).parameters
        assert "auth" in params
        assert api.healthz() == {"ok": True}

    def test_usage_has_no_cross_user_aggregate_block(self, ctx, world):
        """The removed `system` block counted every user's queries, nodes and
        vectors on an endpoint that reports the caller's own usage."""
        out = api.get_usage(auth=_auth(world["alice"]), ctx=ctx)
        assert "system" not in out
        assert not hasattr(api, "_count_total_queries")


class TestImpactAndQueryPassTheCallersScope:
    def test_impact_passes_the_readable_scope(self, ctx, world):
        api.analyse_impact(
            payload=api.ImpactRequest(node_id="a-doc"),
            auth=_auth(world["alice"]),
            ctx=ctx,
        )
        kwargs = ctx.impact.analyse.call_args.kwargs
        assert set(kwargs["pack_ids"]) == {PACK_A, PACK_PUBLIC}

    def test_query_passes_the_readable_scope_to_both_legs(self, ctx, world):
        ctx.hybrid.query.return_value = MagicMock(results=[], warnings=[])
        ctx.hybrid.keyword_search.return_value = []
        api.query_ontology(
            payload=api.QueryRequest(question="anything"),
            auth=_auth(world["bob"]),
            ctx=ctx,
        )
        assert set(ctx.hybrid.query.call_args.kwargs["pack_ids"]) == {PACK_B, PACK_PUBLIC}
        # The zero-result fallback is a second read path and needs it too.
        assert set(ctx.hybrid.keyword_search.call_args.kwargs["pack_ids"]) == {
            PACK_B,
            PACK_PUBLIC,
        }
