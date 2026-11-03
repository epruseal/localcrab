"""#568 (absorbs #545): REST /api/query wires the doc store the way MCP does.

Design: docs/issue-568-rest-doc-store.md.

The REST context is built by the real ``apps.api.main._build_context`` and the
MCP context by the real ``opencrab.mcp.tools._assemble_context``. Only the store
factories are replaced, so both contexts sit over the same real SQLite stores.
Queries go through the real handlers ``query_ontology`` (REST) and
``ontology_query`` (MCP).
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")

from apps.api import main as api  # noqa: E402
from opencrab.auth import Principal, create_user, principal_scope  # noqa: E402
from opencrab.ontology.query import HybridQuery  # noqa: E402
from opencrab.pack.ownership import create_pack  # noqa: E402
from tests import _issue516_flow as flow  # noqa: E402
from tests._vec_helpers import build_vector_store  # noqa: E402

PACK = "pack-568"
OTHER_PACK = "pack-568-other"
RARE = "zyxqrareterm"
LONG_ID = "long-source"
LONG_TEXT = ("lorem ipsum " * 400).strip() + f" {RARE}"
WAIT = 30


@pytest.fixture
def world(tmp_path, monkeypatch):
    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.sql_store import SQLStore

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCRAB_BM25_DEBOUNCE", "0")
    vec = build_vector_store("sqlite-vec", tmp_path)
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    sql = SQLStore(f"sqlite:///{tmp_path / 'registry.db'}")
    owner = Principal(user_id=create_user(sql, "Owner"), is_local=False, disabled=False)
    create_pack(sql, owner.user_id, PACK, title="pack alpha")
    create_pack(sql, owner.user_id, OTHER_PACK, title="pack beta")
    # One node that only the BM25 leg can find: the vector store holds no vector
    # for it, the graph leg expands from anchors only, and the node text is not a
    # source row (so the FTS leg cannot see it either).
    props = {"pack_id": PACK, "node_id": "n-rare", "title": f"title {RARE}", "text": f"body {RARE}"}
    graph.upsert_node("Document", "n-rare", dict(props), space_id="resource")
    docs.upsert_node_doc("resource", "Document", "n-rare", dict(props))
    other = {"pack_id": OTHER_PACK, "node_id": "n-other", "title": "unrelated", "text": "plain words"}
    graph.upsert_node("Document", "n-other", dict(other), space_id="resource")
    docs.upsert_node_doc("resource", "Document", "n-other", dict(other))
    try:
        yield {"vec": vec, "graph": graph, "docs": docs, "sql": sql, "owner": owner}
    finally:
        for closer in (vec.close, graph.close, docs.close):
            try:
                closer()
            except Exception:  # noqa: BLE001 -- a test may have closed it already
                pass
        sql._engine.dispose()


def _rest_ctx(world, monkeypatch):
    monkeypatch.setattr(api, "make_graph_store", lambda s: world["graph"])
    monkeypatch.setattr(api, "make_vector_store", lambda s: world["vec"])
    monkeypatch.setattr(api, "make_doc_store", lambda s: world["docs"])
    monkeypatch.setattr(api, "make_sql_store", lambda s: world["sql"])
    return api._build_context()


def _mcp_ctx(world, monkeypatch):
    from opencrab.mcp import tools

    monkeypatch.setattr(tools, "_context", {})
    return tools._assemble_context(
        world["graph"], world["vec"], world["docs"], world["sql"], world["sql"]
    )


def _rest_ask(ctx, world, question, **kw):
    auth = api.AuthContext(
        user_id=world["owner"].user_id, tier="free", principal=world["owner"]
    )
    return api.query_ontology(api.QueryRequest(question=question, **kw), auth, ctx)


def _mcp_ask(mcp_ctx, world, question, **kw):
    from opencrab.mcp import tools

    with principal_scope(world["owner"]), patch.object(
        tools, "_get_context", return_value=mcp_ctx
    ):
        return tools.ontology_query(question=question, **kw)


def _legs(response):
    return [(r["node_id"], r["source"]) for r in response["results"]]


def test_rest_context_uses_its_own_doc_store(world, monkeypatch):
    ctx = _rest_ctx(world, monkeypatch)
    assert ctx.docs is world["docs"]
    assert ctx.hybrid._doc_store is ctx.docs


def test_rest_and_mcp_contexts_use_the_same_doc_store(world, monkeypatch):
    ctx = _rest_ctx(world, monkeypatch)
    mcp_ctx = _mcp_ctx(world, monkeypatch)
    assert ctx.hybrid._doc_store is mcp_ctx["hybrid"]._doc_store is world["docs"]


def test_rest_query_returns_the_bm25_leg(world, monkeypatch):
    ctx = _rest_ctx(world, monkeypatch)
    response = _rest_ask(ctx, world, RARE, pack_ids=[PACK])
    # "bm25" alone: a "hybrid" item merged from several legs would not prove it.
    assert ("n-rare", "bm25") in _legs(response), response["results"]


def test_unconnected_hybrid_query_has_no_bm25_item(world):
    """Control: without a doc store the same query yields no BM25 item."""
    hybrid = HybridQuery(world["vec"], world["graph"])
    outcome = hybrid.query(RARE, pack_ids=[PACK], full_scope={PACK})
    assert all(result.source != "bm25" for result in outcome.results)


def test_rest_and_mcp_return_the_same_nodes_and_legs(world, monkeypatch):
    ctx = _rest_ctx(world, monkeypatch)
    mcp_ctx = _mcp_ctx(world, monkeypatch)
    rest = _legs(_rest_ask(ctx, world, RARE, pack_ids=[PACK]))
    mcp = _legs(_mcp_ask(mcp_ctx, world, RARE, pack_ids=[PACK]))
    assert rest and rest == mcp


def test_rest_and_mcp_choose_the_same_pack_for_auto_pack(world, monkeypatch):
    """The pack metadata never mentions the term; only one pack's content does."""
    ctx = _rest_ctx(world, monkeypatch)
    mcp_ctx = _mcp_ctx(world, monkeypatch)
    rest = _rest_ask(ctx, world, RARE, auto_pack=True)
    mcp = _mcp_ask(mcp_ctx, world, RARE, auto_pack=True)
    assert [p["pack_id"] for p in mcp["selected_packs"]] == [PACK]
    assert [p["pack_id"] for p in rest["selected_packs"]] == [PACK]
    assert rest["pack_filter"]["pack_ids"] == mcp["pack_filter"]["pack_ids"] == [PACK]
    assert _legs(rest) == _legs(mcp)


def test_rest_response_carries_source_locator_like_mcp(world, monkeypatch):
    """#545: a cut text gets a locator when the doc store is connected."""
    flow.install_vector_leg(world["vec"], monkeypatch, PACK, [LONG_ID], LONG_TEXT)
    world["docs"].upsert_source(
        LONG_ID, LONG_TEXT, {"pack_id": PACK, "space": "evidence", "node_id": LONG_ID}
    )
    ctx = _rest_ctx(world, monkeypatch)
    mcp_ctx = _mcp_ctx(world, monkeypatch)

    item = _rest_ask(ctx, world, "lorem", pack_ids=[PACK])["results"][0]
    mcp_item = _mcp_ask(mcp_ctx, world, "lorem", pack_ids=[PACK])["results"][0]
    assert "source_locator" in item["truncation"], item["truncation"]
    assert item["truncation"]["full_text_tool"] == "ontology_get_source_range"
    assert "full_text_unavailable" not in item["truncation"]
    assert item["truncation"]["source_locator"] == mcp_item["truncation"]["source_locator"]

    # Control: a query object with no doc store cannot prove the source row.
    bare = api.ApiContext(
        settings=ctx.settings,
        graph=ctx.graph,
        vector=ctx.vector,
        docs=ctx.docs,
        sql=ctx.sql,
        hybrid=HybridQuery(world["vec"], world["graph"]),
        impact=ctx.impact,
    )
    unproven = _rest_ask(bare, world, "lorem", pack_ids=[PACK])["results"][0]
    assert unproven["truncation"].get("full_text_unavailable") is True
    assert "source_locator" not in unproven["truncation"]


def _start_blocked_worker(world, monkeypatch):
    """Build a REST context whose BM25 worker is mid-read on the doc store."""
    monkeypatch.setenv("OPENCRAB_BM25_DB_INDEX", "0")  # force the memory path
    ctx = _rest_ctx(world, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    original = world["docs"].bm25_fingerprint

    def blocked(*args: Any, **kwargs: Any):
        entered.set()
        release.wait(WAIT)
        return original(*args, **kwargs)

    monkeypatch.setattr(world["docs"], "bm25_fingerprint", blocked)
    ctx.hybrid.invalidate_bm25_cache()
    assert entered.wait(WAIT), "the worker never reached the doc store read"
    worker = ctx.hybrid._bm25_worker
    assert worker is not None and worker.is_alive()
    return ctx, worker, release


def test_close_context_waits_for_the_bm25_worker_before_closing_stores(
    world, monkeypatch
):
    ctx, worker, release = _start_blocked_worker(world, monkeypatch)

    join_calls: list[Any] = []
    join_entered = threading.Event()
    original_join = ctx.hybrid._bm25_join

    def spy_join(timeout: Any = None) -> None:
        join_calls.append(timeout)
        join_entered.set()
        original_join(timeout)

    monkeypatch.setattr(ctx.hybrid, "_bm25_join", spy_join)

    worker_alive_at_close: list[bool] = []
    original_close = world["docs"].close

    def recording_close() -> None:
        worker_alive_at_close.append(worker.is_alive())
        original_close()

    monkeypatch.setattr(world["docs"], "close", recording_close)

    closer = threading.Thread(target=api._close_context, args=(ctx,))
    closer.start()
    try:
        # The default wait elapses, then the unbounded wait begins. The read is
        # released only after that, so a bounded second wait would be caught.
        assert join_entered.wait(WAIT), "_close_context never reached the second wait"
        assert closer.is_alive()
        assert worker_alive_at_close == [], "a store was closed under a live worker"
    finally:
        release.set()
        closer.join(WAIT)
    assert not closer.is_alive()
    assert join_calls == [None], join_calls
    assert worker_alive_at_close == [False]


class _IdleSpyEvent(threading.Event):
    """A wake event that reports when a thread is blocked in ``wait``."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        self.waiting.set()
        try:
            return super().wait(timeout)
        finally:
            self.waiting.clear()


def test_close_context_stops_an_idle_bm25_worker(world, monkeypatch):
    """An idle worker sleeps on its wake event; the stop signal alone does not wake it."""
    monkeypatch.setenv("OPENCRAB_BM25_DB_INDEX", "0")  # force the memory path
    ctx = _rest_ctx(world, monkeypatch)
    spy = _IdleSpyEvent()
    monkeypatch.setattr(ctx.hybrid._bm25, "_wake", spy)
    ctx.hybrid.invalidate_bm25_cache()
    worker = ctx.hybrid._bm25_worker
    assert worker is not None

    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if ctx.hybrid._bm25.state is not None and spy.waiting.is_set():
            break
        time.sleep(0.01)
    assert ctx.hybrid._bm25.state is not None and spy.waiting.is_set(), "worker never went idle"

    closer = threading.Thread(target=api._close_context, args=(ctx,), daemon=True)
    closer.start()
    closer.join(WAIT)
    try:
        assert not closer.is_alive(), "_close_context hung on an idle worker"
        assert not worker.is_alive()
    finally:
        spy.set()  # let a hung worker exit if the assertion failed


def test_close_context_does_not_wait_without_a_worker(world, monkeypatch):
    ctx = _rest_ctx(world, monkeypatch)
    assert ctx.hybrid._bm25_worker is None
    closer = threading.Thread(target=api._close_context, args=(ctx,))
    closer.start()
    closer.join(WAIT)
    assert not closer.is_alive()
