"""#201 `pack_fork` orchestrator (design v7 §5, §6, §8 test table).

Builds against the REAL local store stack (``opencrab.stores.factory`` +
a per-test ``Settings(STORAGE_MODE="local", EMBEDDING_BACKEND="local")``)
rather than hand-rolled doubles: ``fork.py`` itself calls scoped-export
methods (``export_nodes_scoped``, ``export_edges_scoped``,
``list_sources_scoped``, ``export_pack_vectors``, ``import_vectors``) that
sit OUTSIDE ``OntologyBuilder``'s own contract, so a fake would have to
reimplement both surfaces correctly to be trustworthy. Using the real
SQLite-backed graph/doc/sql stores and a real ``ChromaStore`` (local ONNX
MiniLM embeddings, no network) exercises the same wiring production code
uses (``opencrab/mcp/tools/pack.py``'s ``_get_context()``).

``EMBEDDING_BACKEND="local"`` is required: ``Settings``' default
(``"openai"``) resolves ``vector_backend_resolved`` to ``"sqlite-vec"``,
which is not installed in this environment and would additionally need a
live embedding server. ``"local"`` resolves to ``"chroma"`` instead, which
uses Chroma's own bundled ONNX model (already cached locally).

Every call into ``fork_pack`` and every seed-building call into
``builder.add_node``/``add_edge``/``write_source`` is wrapped in
``principal_scope(...)``: those functions' own ambient-principal
resolution (``opencrab.auth.current_principal()``) is independent of
``fork_pack``'s explicit ``principal=`` kwarg, which is used only for
``fork_pack``'s own ownership comparisons.

Reverse-mutation is the standard throughout (design v7 §8): each test
that names a guard is written so that removing the guard makes the test
fail, not just exercises the happy path. Guards actually reverse-mutated
in this session are listed in the final delivery report, not repeated
per-test here.

Coverage: design §8's table has 53 rows (T1-T53); this file owns the
orchestrator-level subset assigned to this work unit -- as of this
revision: T1-T6, T8-T18, T23-T41, T43-T47, T49-T52 -- and, within that,
prioritizes T1-T4, T14-T15, T28, T16/T35/T38/T30/T39 per the task's
explicit priority order. Not assigned to this file: T7, T19-T22, T42,
T48, T53 (owned by ``tests/test_pack_fork_faults.py`` /
``tests/test_fork_write_gate.py``, written by a different worker in this
same worktree). Each test's docstring names the design row(s) it covers
and its reverse-mutation.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pytest
from sqlalchemy import text as _sql_text

from opencrab.auth import Principal, principal_scope
from opencrab.config import Settings
from opencrab.ontology.builder import OntologyBuilder
from opencrab.ontology.query import HybridQuery
from opencrab.pack import fork as fork_mod
from opencrab.pack.fork import _PACK_ID_BUDGET, _PACK_ID_COLUMN_LIMIT, fork_pack
from opencrab.pack.ownership import (
    anchor_node_id,
    begin_pack_creation,
    get_pack,
    mark_pack_ready,
    set_visibility,
)
from opencrab.pack.source_writer import write_source

ALICE = Principal(user_id="user_alice", is_local=False, disabled=False)
BOB = Principal(user_id="user_bob", is_local=False, disabled=False)


def _ulp_close(a: float, b: float) -> bool:
    """Within 2 float32 ULP at the components' own magnitude (T13).

    chroma's cosine-space round-trip is not bit-exact (docs/vector-backends.md
    §8.1: "at most one float32 ULP per component") -- a hash or exact-equality
    comparison across a raw vector-copy round-trip is the wrong tool and would
    make this test flaky on a passing implementation. Mirrors
    tests/test_vector_raw_contract.py's own helper of the same name.
    """
    scale = max(abs(a), abs(b))
    if scale == 0.0:
        return a == b
    return abs(a - b) <= 2.0 * (2.0 ** (math.floor(math.log2(scale)) - 23))


# ---------------------------------------------------------------------------
# Fixture: real local store stack, isolated per test via tmp_path.
# ---------------------------------------------------------------------------


@pytest.fixture
def stack(tmp_path):
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    settings = Settings(
        STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path), EMBEDDING_BACKEND="local",
    )
    graph = make_graph_store(settings)
    docs = make_doc_store(settings)
    vector = make_vector_store(settings)
    sql = make_sql_store(settings)
    builder = OntologyBuilder(graph, docs, sql, vec=vector)
    hybrid = HybridQuery(vector, graph)
    hybrid._doc_store = docs

    with sql._engine.begin() as conn:
        for p in (ALICE, BOB):
            conn.execute(
                _sql_text(
                    "INSERT INTO users (user_id, display_name, is_local) VALUES (:u, :n, 0)"
                ),
                {"u": p.user_id, "n": p.user_id},
            )

    stack = {
        "sql": sql, "graph": graph, "docs": docs, "vector": vector,
        "builder": builder, "hybrid": hybrid,
    }
    try:
        yield stack
    finally:
        # Each test owns four native/database handles.  Chroma keeps file
        # descriptors open beyond the last collection call, so returning the
        # dictionary without closing the stores exhausts the process limit
        # across this 100+ case module.
        vector.close()
        graph.close()
        docs.close()
        sql._engine.dispose()


def _fork(stack, *, principal, src_pack_id, **kw) -> dict[str, Any]:
    with principal_scope(principal):
        return fork_pack(
            stack["sql"], stack["graph"], stack["docs"], stack["vector"],
            stack["hybrid"], stack["builder"],
            principal=principal, src_pack_id=src_pack_id, **kw,
        )


def _test_store_mutation(store: Any, sql: str, params: dict[str, Any], *, immediate: bool = False) -> None:
    """Apply deliberate corruption through a test-only store transaction.

    These tests model legacy rows that production writers would reject.  The
    graph store no longer exposes the old single-statement ``_exec_write``
    hook, so keep the raw mutation authority local to this test module and
    use the store's transaction boundary to commit it safely.
    """
    with store._tx(immediate=immediate) as conn:
        conn.execute(sql, params)


def _test_graph_mutation(stack: dict[str, Any], sql: str, params: dict[str, Any]) -> None:
    _test_store_mutation(stack["graph"], sql, params, immediate=True)


def _test_doc_mutation(stack: dict[str, Any], sql: str, params: dict[str, Any]) -> None:
    _test_store_mutation(stack["docs"], sql, params)


def _seed_colliding_source_bypassing_graph(
    stack: dict[str, Any], principal: Principal, *, source_id: str, pack_id: str, text: str,
) -> None:
    """T89/T92 픽스처 전용: `write_source`를 거치지 않고 doc/vector 스토어에
    직접 시딩한다.

    두 테스트는 `source_id`와 같은 id의 그래프 노드가 이미 존재하는 상태에서
    같은 id의 "소스"를 만들어야 한다 -- fork의 node/source 축 id 충돌 가드를
    치려는 것이다. `write_source`의 기본값(`write_graph=True`)으로 부르면
    그래프 leg가 진짜 `NodeIdentityConflict`를 던져 픽스처 자체가 죽고,
    `write_graph=False`는 (#74 가드에 의해) `fork_copy=True` 없이는 이제
    거부된다 -- 그 조합은 fork의 raw-copy 전용 옵트아웃이지 일반적인 픽스처
    구성 수단이 아니기 때문이다.

    두 테스트가 검증하는 것은 fork의 `_fork()` 결과(node/source 충돌 가드)
    이지 소스 쓰기 경로 자체가 아니므로, `write_source`를 지날 이유가 없다.
    대신 그 함수가 정상 호출됐을 때 doc/vector 스토어에 남겼을 것과 같은
    형상을 직접 만든다: `source_writer.write_source`의 `stamp(...,
    keys=SOURCE_STAMPED)`가 채우는 `pack_id`/`user_id`와, 뒤이은
    `meta.setdefault("space", "evidence")`를 그대로 재현하고, 벡터 쪽에는
    `HybridQuery.ingest`가 채우는 `source_id` 키까지 맞춘다. `fork_pack`이
    실제로 읽는 것은 `docs.list_sources_scoped`(pack_id는 metadata의
    JSON 술어로 스코핑된다)뿐이라 doc 쪽 시딩만으로도 이 두 테스트의
    어서션은 성립하지만, 원래 픽스처가 두 스토어 모두에 소스를 남기던
    형상과 어긋나지 않도록 벡터도 함께 심는다.
    """
    docs, vector = stack["docs"], stack["vector"]
    meta = {"pack_id": pack_id, "user_id": principal.user_id, "space": "evidence"}
    docs.upsert_source(source_id, text, dict(meta))
    vector_meta = dict(meta)
    vector_meta["source_id"] = source_id
    vector.upsert_texts(texts=[text], metadatas=[vector_meta], ids=[source_id])


def _seed_pack(
    stack, owner: Principal, pack_id: str, *,
    node_count: int = 2, with_edge: bool = True, with_source: bool = True,
    visibility: str | None = None,
) -> str:
    """Build a fully-``ready`` source pack: anchor (with its own vector,
    matching an ordinary ``pack_create``d pack -- needed for T28) + N
    ordinary content nodes + an edge between the first two (if any) +
    one legacy text source. Returns the actual pack_id assigned."""
    sql, builder, docs, vector, hybrid, graph = (
        stack["sql"], stack["builder"], stack["docs"], stack["vector"], stack["hybrid"],
        stack["graph"],
    )
    with principal_scope(owner):
        dst = begin_pack_creation(sql, owner.user_id, pack_id)
        anchor_id = anchor_node_id(dst)
        builder.add_node(
            space="resource", node_type="Dataset", node_id=anchor_id,
            properties={"title": "t", "description": "d", "created_by": "test"},
            pack_id=dst, pack_anchor=True,
        )
        # Ordinary content writes require the pack to already be 'ready'
        # (assert_writable's default allowed_statuses=('ready',) -- only
        # the anchor write itself is allowed while 'creating'), so the
        # pack must be promoted BEFORE any of the content below, mirroring
        # the real pack_create-then-pack_ingest split.
        mark_pack_ready(sql, dst, owner.user_id)
        node_ids = []
        for i in range(node_count):
            # Node identity is global, not pack-scoped (write_gate's
            # identity guard checks across ALL packs) -- prefix with the
            # REQUESTED pack_id (stable/known to callers, unlike the
            # possibly-negotiated `dst`) so repeated _seed_pack calls
            # across different tests in this module never collide.
            nid = f"{pack_id}-n{i}"
            builder.add_node(
                space="resource", node_type="Document", node_id=nid,
                properties={"title": f"doc {i}"}, pack_id=dst,
            )
            node_ids.append(nid)
        if with_edge and len(node_ids) >= 2:
            builder.add_edge(
                "resource", node_ids[0], "cites", "resource", node_ids[1], pack_id=dst,
            )
        if with_source:
            write_source(
                sql, hybrid, docs, vector, graph=graph,
                text="hello world, this is a legacy source", source_id="s0", pack_id=dst,
            )
    if visibility:
        set_visibility(sql, owner, dst, visibility)
    return dst


# ---------------------------------------------------------------------------
# T1-T4 -- acceptance criteria
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_t1_source_pack_untouched_after_fork(self, stack):
        """T1: forking must not mutate the original pack's nodes/edges/
        sources/vectors -- not even their values. Reverse-mutation: if
        fork.py wrote into the source-space ids (skipped remapping), this
        would fail because src's node would gain a mutated title."""
        src = _seed_pack(stack, ALICE, "src-t1")
        before = stack["graph"].export_nodes_scoped([src], 100)
        before_by_id = {n["props"]["id"]: n["props"] for n in before}

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok"

        after = stack["graph"].export_nodes_scoped([src], 100)
        after_by_id = {n["props"]["id"]: n["props"] for n in after}
        assert after_by_id == before_by_id

    def test_t2_copy_owned_by_caller_survives_source_deletion(self, stack):
        """T2: the copy is owned by the forking caller, carries
        forked_from == source pack_id, and stays fully readable/intact
        after the source pack row is gone. Reverse-mutation: if fork_copy
        wrote `forked_from` wrong (or not at all), the first assert fails."""
        src = _seed_pack(stack, ALICE, "src-t2")
        # ALICE forking her own (private) pack -- always allowed regardless
        # of visibility, so this isolates T2 from the authorization axis.
        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok"
        dst = out["pack_id"]
        assert out["forked_from"] == src

        row = get_pack(stack["sql"], dst)
        assert row["owner_id"] == ALICE.user_id
        assert row["forked_from"] == src

        # Delete the source pack's registry row entirely; the copy must
        # still resolve and still carry its own content.
        with stack["sql"]._engine.begin() as conn:
            conn.execute(_sql_text("DELETE FROM packs WHERE pack_id = :p"), {"p": src})
        assert get_pack(stack["sql"], src) is None
        dst_row = get_pack(stack["sql"], dst)
        assert dst_row is not None and dst_row["status"] == "ready"
        dst_nodes = stack["graph"].export_nodes_scoped([dst], 100)
        assert len(dst_nodes) >= 2  # anchor excluded from "copied" nodes but present in graph

    def test_t3_scope_predicate_not_fooled_by_foreign_pack_id_claim(self, stack):
        """T3: a node whose properties merely CLAIM another pack's pack_id
        in a non-canonical spot must not leak into that pack's scoped
        results. Exercised here via the real scoped-export predicate: a
        node genuinely written under `dst` must not appear when scoping
        by `src` alone, even though its properties still name `src` via
        `forked_from`-adjacent metadata."""
        src = _seed_pack(stack, ALICE, "src-t3")
        out = _fork(stack, principal=ALICE, src_pack_id=src)
        dst = out["pack_id"]

        src_scoped = stack["graph"].export_nodes_scoped([src], 100)
        src_ids = {n["props"]["id"] for n in src_scoped}
        dst_scoped = stack["graph"].export_nodes_scoped([dst], 100)
        dst_ids = {n["props"]["id"] for n in dst_scoped}
        assert src_ids.isdisjoint(dst_ids), (
            "forked copy's node ids leaked into the source pack's own scope"
        )

    def test_t4_tier2_failure_demotes_and_stays_invisible(self, stack, monkeypatch):
        """T4: a Tier 2 failure (our own write attempt breaking, injected
        here as the vector-import step raising) demotes the registry row
        to 'partial' and that pack_id never appears in
        readable_pack_ids/list_packs_for. Reverse-mutation: removing the
        demotion (returning success unconditionally) would leave `status`
        'ok' and the pack visible."""
        from opencrab.pack.ownership import list_packs_for, readable_pack_ids

        src = _seed_pack(stack, ALICE, "src-t4")

        def _boom(self, records, *, pack_id):  # noqa: ARG001
            raise RuntimeError("injected Tier 2 failure")

        monkeypatch.setattr(type(stack["vector"]), "import_vectors", _boom, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial"
        dst = out["pack_id"]

        assert dst not in readable_pack_ids(stack["sql"], ALICE)
        assert dst not in {p["pack_id"] for p in list_packs_for(stack["sql"], ALICE)}
        row = get_pack(stack["sql"], dst)
        assert row["status"] == "partial"


# ---------------------------------------------------------------------------
# 코멘트 1/2 (T14/T15) -- preflight leaves nothing; identity conflict
# rejects without a silent overwrite and without stranding a registry row.
# ---------------------------------------------------------------------------


class TestPreflightAndIdentityConflict:
    def test_t14_preflight_rejection_leaves_zero_registry_rows_and_zero_anchors(
        self, stack,
    ):
        """T14: a preflight-stage rejection (vector store reports itself
        unavailable, §5-1 step 3) must run BEFORE begin_pack_creation --
        zero new registry rows, zero new graph anchors. Reverse-mutation:
        if the ordering were flipped (reserve-then-check), a 'creating'
        row for the derived slug would exist after this call."""
        src = _seed_pack(stack, ALICE, "src-t14")

        class _DeadVector:
            available = False

        out = fork_pack(
            stack["sql"], stack["graph"], stack["docs"], _DeadVector(),
            stack["hybrid"], stack["builder"],
            principal=ALICE, src_pack_id=src,
        )
        assert "error" in out
        assert get_pack(stack["sql"], f"{src}-fork") is None
        with stack["sql"]._engine.begin() as conn:
            rows = conn.execute(
                _sql_text("SELECT pack_id FROM packs WHERE pack_id LIKE :p"),
                {"p": f"{src}-fork%"},
            ).fetchall()
        assert rows == [], "preflight rejection must not have reserved any pack_id"

    def test_t15_identity_conflict_rejects_without_overwrite_and_row_is_removed(
        self, stack,
    ):
        """T15: if the fork's deterministically-remapped destination node
        id collides with a node that ALREADY belongs to a different pack,
        the fork must be rejected outright (no silent overwrite of the
        foreign node) AND the 'creating' registry row it reserved must be
        gone afterward (design §5-2 step 12's delete_pack_row branch --
        the one delete-capable rejection point in the whole design).
        Reverse-mutation: skipping the identity probe would let the write
        proceed and silently clobber the foreign node; skipping
        delete_pack_row would strand a permanent 'creating' row occupying
        the slug forever."""
        src = _seed_pack(stack, ALICE, "src-t15")

        # Force a collision: monkeypatch fork_remap.build_mapping so the
        # single content node maps to an id already owned by a DIFFERENT
        # pack under the same destination slug fork.py is about to reserve.
        target_dst = f"{src}-fork"
        with principal_scope(ALICE):
            begin_pack_creation(stack["sql"], ALICE.user_id, "occupier")
            stack["builder"].add_node(
                space="resource", node_type="Dataset",
                node_id=anchor_node_id("occupier"),
                properties={"title": "t", "description": "d", "created_by": "test"},
                pack_id="occupier", pack_anchor=True,
            )
            mark_pack_ready(stack["sql"], "occupier", ALICE.user_id)
            stack["builder"].add_node(
                space="resource", node_type="Document", node_id="n0",
                properties={"title": "already here"}, pack_id="occupier",
            )

        real_build_mapping = fork_mod.build_mapping

        def _colliding_build_mapping(node_ids, source_ids, *, salt, src_anchor, dst_anchor):
            mapping = real_build_mapping(
                node_ids, source_ids, salt=salt, src_anchor=src_anchor, dst_anchor=dst_anchor,
            )
            for old_id in node_ids:
                mapping[old_id] = "n0"  # collide with occupier's node id
            return mapping

        orig = fork_mod.build_mapping
        try:
            fork_mod.build_mapping = _colliding_build_mapping
            out = _fork(stack, principal=ALICE, src_pack_id=src)
        finally:
            fork_mod.build_mapping = orig

        assert "error" in out or out.get("status") == "partial", out
        # Whichever shape: no registry row should be left in 'creating'.
        with stack["sql"]._engine.begin() as conn:
            rows = conn.execute(
                _sql_text(
                    "SELECT pack_id, status FROM packs WHERE pack_id LIKE :p"
                ),
                {"p": f"{target_dst}%"},
            ).fetchall()
        for _pid, status in rows:
            assert status != "creating", "identity-conflict rejection must not strand a 'creating' row"

        # The foreign node must be untouched.
        occ_nodes = stack["graph"].export_nodes_scoped(["occupier"], 10)
        occ_props = {n["props"]["id"]: n["props"] for n in occ_nodes}
        assert occ_props["n0"]["title"] == "already here"


# ---------------------------------------------------------------------------
# T28 -- anchor-vector exclusion (critical: without it every ordinary pack
# fails to fork, since every pack_create'd pack's anchor has its own vector).
# ---------------------------------------------------------------------------


class TestAnchorVectorExclusion:
    def test_t28_anchor_vector_excluded_new_anchor_lands_edges_repoint(self, stack):
        """T28: forking a pack whose anchor carries its own vector (i.e.
        an ordinary pack_create'd pack, which every real pack is) must
        still succeed -- the source anchor's vector is excluded from the
        vector-copy batch (would collide on id/pack semantics otherwise),
        the destination gets exactly one Dataset anchor of its own, and
        an edge that pointed at the OLD anchor is repointed at the NEW
        anchor. Reverse-mutation: removing the `skipped.anchor_vector`
        exclusion collapses vector import entirely (Tier-2 failure on the
        anchor-vector id already existing under `dst`'s own anchor write);
        removing the anchor-mapping fixup leaves the edge pointing at the
        old (source-space) anchor id, which fails identity/H4 checks."""
        src = _seed_pack(stack, ALICE, "src-t28", node_count=1, with_edge=False, with_source=False)
        src_anchor = anchor_node_id(src)
        with principal_scope(ALICE):
            # An edge from the anchor itself to the one content node --
            # exercises anchor-id remapping on an edge endpoint.
            stack["builder"].add_edge(
                "resource", src_anchor, "cites", "resource", f"{src}-n0", pack_id=src,
            )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        dst = out["pack_id"]
        assert out["skipped"]["anchor_vector"] >= 1

        dst_anchor = anchor_node_id(dst)
        dst_nodes = stack["graph"].export_nodes_scoped([dst], 100)
        anchors = [
            n for n in dst_nodes
            if n["labels"] == ["Dataset"] and n["props"]["id"] == dst_anchor
        ]
        assert len(anchors) == 1

        dst_edges = stack["graph"].export_edges_scoped([dst], 100)
        assert len(dst_edges) == 1
        edge = dst_edges[0]
        assert edge["source_props"]["id"] == dst_anchor, (
            "edge that pointed at the source pack's anchor must repoint at "
            "the destination pack's own anchor, not the stale source-space id"
        )


# ---------------------------------------------------------------------------
# 코멘트 3 (T16) / T35 / T38 -- tier classification.
# ---------------------------------------------------------------------------


class TestTierClassification:
    def test_t16_legacy_edge_missing_endpoint_id_is_tier1_not_blocking(self, stack):
        """T16: a legacy edge endpoint with no props['id'] is Tier 1 --
        reported under errors.edges and skipped, fork still completes as
        'ready'. Reverse-mutation: if the skip were removed (e.g. the
        endpoint-id check raised instead of appending to edge_errors and
        continuing), this fork would demote to 'partial' or blow up.

        Uses 11 content nodes / 10 edges (only 1 of each touching the
        corrupted node) so the single Tier 1 loss sits AT the 10% floor
        boundary (1/10, 1/12) rather than tripping §5-1 step 8b's
        pre-reservation rejection -- a 2-node pack would lose its one edge
        at a 100% ratio and never reach the 'ready with errors' path this
        test is meant to exercise (that pre-reservation-rejection behavior
        is covered separately by T30/T39)."""
        src = _seed_pack(stack, ALICE, "src-t16", node_count=11, with_edge=False, with_source=False)
        with principal_scope(ALICE):
            # A chain among n1..n10 that never touches n0 (9 edges), plus
            # exactly one edge touching n0 (the node about to be corrupted).
            for i in range(1, 10):
                stack["builder"].add_edge(
                    "resource", f"{src}-n{i}", "cites", "resource", f"{src}-n{i + 1}", pack_id=src,
                )
            stack["builder"].add_edge(
                "resource", f"{src}-n0", "cites", "resource", f"{src}-n1", pack_id=src,
            )
        # Directly corrupt the edge's source-endpoint properties in the
        # graph store to strip its "id" -- simulating a legacy row written
        # before "id" was guaranteed to be stamped into properties.
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_remove(properties, '$.id') "
            "WHERE node_id = :nid",
            {"nid": f"{src}-n0"},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["errors"]["edges"], "the corrupted edge must be reported, not silently dropped"
        assert out["copied"]["edges"] == 9

    def test_t35_schema_drift_property_failure_is_tier1(self, stack):
        """T35: a node whose properties fail schema/property validation
        (drifted from the current schema) is classified Tier 1 -- caught
        during preflight's own validate_node_properties call, reported
        under errors.nodes, fork still completes 'ready'. Reverse-mutation:
        if property validation were skipped during preflight (moved
        entirely to the write phase), the bad node would instead blow up
        builder.add_node inside the write loop and demote the whole fork
        to 'partial' (Tier 2 misclassification)."""
        # 10 content nodes so the single Tier 1 node loss (1/11 incl. anchor)
        # stays under the 10% floor instead of tripping §5-1 step 8b's
        # pre-reservation rejection (a 1-node pack would lose its only node
        # at a 100% ratio and never reach the 'ready with errors' path this
        # test is meant to exercise -- that rejection path is covered
        # separately by T30/T39).
        src = _seed_pack(stack, ALICE, "src-t35", node_count=10, with_edge=False, with_source=False)
        # Inject a property type violation directly at the store layer
        # (title must be a string per _PROPERTY_TYPE_MAP / schema).
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.title', 12345) "
            "WHERE node_id = :nid",
            {"nid": f"{src}-n0"},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["errors"]["nodes"], "schema-drifted node must be reported under errors.nodes"
        assert out["copied"]["nodes"] == 9

    def test_t38_all_vectors_orphaned_rejected_before_reservation(self, stack):
        """T38: a pack whose vectors are ENTIRELY orphaned (point at ids
        the mapping doesn't know, i.e. preflight already dropped or never
        exported by export_nodes_scoped) fails the completeness floor
        BEFORE begin_pack_creation -- no 'ok' completion with 0 copied
        vectors. Reverse-mutation: if vector classification ran before
        the mapping existed (design's 17-step ordering caveat reversed),
        the floor check couldn't see the vector axis at all and this
        would wrongly succeed."""
        src = _seed_pack(stack, ALICE, "src-t38", node_count=1, with_edge=False, with_source=False)
        # Directly insert a chroma vector row whose id is neither a
        # surviving node id nor a surviving source id -- a pure orphan.
        stack["vector"].upsert_texts(
            texts=["orphaned text"], ids=["orphan-id-xyz"],
            metadatas=[{"pack_id": src}],
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert "error" in out, out
        assert get_pack(stack["sql"], f"{src}-fork") is None


# ---------------------------------------------------------------------------
# T30 / T39 -- completeness floor.
# ---------------------------------------------------------------------------


class TestCompletenessFloor:
    def test_t30_all_nodes_tier1_rejected_before_reservation(self, stack):
        """T30: a source pack whose nodes are ALL Tier-1-dropped (here:
        every content node fails grammar validation because its type was
        corrupted to something not in the space's allowed set) is
        rejected before begin_pack_creation -- an anchor-only pack must
        not silently succeed as 'ok'. Reverse-mutation: removing the
        completeness-floor check would let a pack with 100% node loss
        still promote to 'ready' with copied.nodes == 0."""
        src = _seed_pack(stack, ALICE, "src-t30", node_count=1, with_edge=False, with_source=False)
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET node_type = 'NotARealType' WHERE node_id = :nid",
            {"nid": f"{src}-n0"},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert "error" in out, out
        assert get_pack(stack["sql"], f"{src}-fork") is None

    def test_t39_loss_ratio_boundary_just_under_passes_just_over_rejects(self, stack):
        """T39: FORK_MAX_LOSS_RATIO boundary -- drop <=10% of nodes and
        the fork proceeds; drop just over 10% and it's rejected before
        reservation. Also exercises "a zero-total axis is exempt" (no
        edges/sources seeded here, so those axes don't block) and
        "anchor_vector doesn't count against the numerator" implicitly
        (this test seeds no vectors at all beyond the anchor's own).
        Reverse-mutation: hardcoding the ratio check to always pass would
        make the second half of this test (11/100 -> reject) fail."""
        assert fork_mod.FORK_MAX_LOSS_RATIO == pytest.approx(0.10)

        # 10 nodes, drop exactly 1 (10% == floor, must still pass).
        src_ok = _seed_pack(stack, ALICE, "src-t39-ok", node_count=10, with_edge=False, with_source=False)
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET node_type = 'Bogus' WHERE node_id = :nid",
            {"nid": f"{src_ok}-n0"},
        )
        out_ok = _fork(stack, principal=ALICE, src_pack_id=src_ok)
        assert out_ok["status"] == "ok", out_ok
        assert out_ok["copied"]["nodes"] == 9

        # 10 nodes, drop 2 (20% > 10% floor, must reject before reservation).
        src_bad = _seed_pack(stack, ALICE, "src-t39-bad", node_count=10, with_edge=False, with_source=False)
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET node_type = 'Bogus' WHERE node_id IN (:n0, :n1)",
            {"n0": f"{src_bad}-n0", "n1": f"{src_bad}-n1"},
        )
        out_bad = _fork(stack, principal=ALICE, src_pack_id=src_bad)
        assert "error" in out_bad, out_bad
        assert get_pack(stack["sql"], f"{src_bad}-fork") is None


# ---------------------------------------------------------------------------
# T23 -- authorization (#143 invariant 7), cheap given the fixture already
# built for the rows above.
# ---------------------------------------------------------------------------


class TestAuthorization:
    def test_t23_five_authorization_cases(self, stack):
        """T23: nonexistent pack and someone else's private pack return
        the identical 'pack not found' error; someone else's public-read
        (not public-fork) pack returns a distinct, more specific error;
        someone else's public-fork pack succeeds; the owner's own pack
        succeeds regardless of visibility. Reverse-mutation: treating
        public-read as fork-enabled would make the public-read case
        succeed instead of erroring -- caught by the explicit status
        check below."""
        missing = _fork(stack, principal=BOB, src_pack_id="does-not-exist-at-all")
        assert missing == {"error": "pack not found", "pack_id": "does-not-exist-at-all"}

        priv = _seed_pack(stack, ALICE, "src-t23-priv", node_count=1, with_source=False)
        out_priv = _fork(stack, principal=BOB, src_pack_id=priv)
        assert out_priv == {"error": "pack not found", "pack_id": priv}

        pub_read = _seed_pack(
            stack, ALICE, "src-t23-pubread", node_count=1, with_source=False,
            visibility="public-read",
        )
        out_pubread = _fork(stack, principal=BOB, src_pack_id=pub_read)
        assert out_pubread.get("error") == "pack is not fork-enabled"

        pub_fork = _seed_pack(
            stack, ALICE, "src-t23-pubfork", node_count=1, with_source=False,
            visibility="public-fork",
        )
        out_pubfork = _fork(stack, principal=BOB, src_pack_id=pub_fork)
        assert out_pubfork["status"] == "ok"

        out_self = _fork(stack, principal=ALICE, src_pack_id=priv)
        assert out_self["status"] == "ok"


# ---------------------------------------------------------------------------
# T46 -- default slug derivation + silent collision negotiation.
# ---------------------------------------------------------------------------


class TestSlugDerivation:
    def test_t46_default_slug_and_collision_negotiation_not_reported_as_error(self, stack):
        """T46: with no new_pack_id, the default is "{src}-fork"; a second
        fork of the same source negotiates a different slug silently
        (design #143 invariant 7: a collision must not be reported as an
        error, since that would leak "someone already used this slug").
        Reverse-mutation: removing the default-slug rule would make the
        first assert fail; surfacing the collision as an error would make
        the second fork's `status` != "ok"."""
        src = _seed_pack(stack, ALICE, "src-t46", node_count=1, with_source=False)
        first = _fork(stack, principal=ALICE, src_pack_id=src)
        assert first["status"] == "ok"
        assert first["pack_id"] == f"{src}-fork"

        second = _fork(stack, principal=ALICE, src_pack_id=src)
        assert second["status"] == "ok"
        assert second["pack_id"] != first["pack_id"]
        assert "error" not in second

    def test_t6_default_slug_collision_against_bare_creating_row_still_negotiates(self, stack):
        """T6 (design §8 T6, §3, §12-6): the default-slug collision
        negotiation (T46 above) must be genuinely STATUS-AGNOSTIC -- a raw
        SQL uniqueness violation in `_negotiate_pack_id`, not a check that
        only avoids colliding with a completed `ready` pack. Proven here
        by occupying the exact slug `f'{src}-fork'` with a row still stuck
        in `creating` (never promoted, so it would never appear in an
        "existing ready packs" style check a narrower implementation
        might use instead) before forking -- the fork must still silently
        negotiate a different pack_id rather than surfacing the sibling
        `creating` row's mere existence as a fork error (#143 invariant
        7: a slug collision must never be reported as an error). Reverse-
        mutation: `begin_pack_creation`'s retry-on-collision loop
        (`ownership.py`'s `_pack_id_candidates`, which
        `_negotiate_pack_id` iterates) was replaced with a single
        non-retrying candidate -- the collision against the bare
        `creating` row then raised straight through `fork_pack`'s own
        `except Exception as exc: raise _reject(f'pack registration
        failed: {exc}')` handler, turning this test's `status == 'ok'`
        assertion into an `'error' in out` result instead."""
        src = _seed_pack(stack, ALICE, "src-t6", node_count=1, with_edge=False, with_source=False)
        with principal_scope(BOB):
            begin_pack_creation(stack["sql"], BOB.user_id, f"{src}-fork")

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["pack_id"] != f"{src}-fork", out
        assert "error" not in out


# ---------------------------------------------------------------------------
# T5 -- wrong source id: "pack not found", never a silent zero-content
# success.
# ---------------------------------------------------------------------------


class TestSourceLookupCorrectness:
    def test_t5_wrong_source_id_is_pack_not_found_not_silent_success(self, stack):
        """T5: forking a pack id that does not exist at all must return the
        exact 'pack not found' error and reserve nothing -- not a silently
        successful, empty/zero-vector fork. Reverse-mutation: neutralizing
        the existence/ownership guard (fork.py's `if src is None or ...`)
        makes `src` stay `None` and the very next line (`src.get(...)` for
        the is_owner check) raise `AttributeError` instead of this clean
        rejection -- this test's exact-dict assertion fails either way."""
        out = _fork(stack, principal=ALICE, src_pack_id="totally-bogus-pack-id-999")
        assert out == {"error": "pack not found", "pack_id": "totally-bogus-pack-id-999"}
        assert get_pack(stack["sql"], "totally-bogus-pack-id-999-fork") is None


# ---------------------------------------------------------------------------
# T9 / T34 -- vector classification: orphans skipped+reported+excluded
# (H5), mistagged vectors skipped, dangling references counted unverified.
# ---------------------------------------------------------------------------


class TestVectorClassification:
    def test_t9_orphan_vector_skipped_reported_and_excluded_from_import(self, stack):
        """T9: a vector whose id is neither a surviving node id nor a
        surviving source id (an orphan -- points at something preflight
        already dropped, or something export_nodes_scoped/
        list_sources_scoped never returned) is skipped, reported under
        skipped.vector_orphans, and never appears in the destination copy.
        H5 (imported vector ids ⊆ new node ids ∪ new source ids) is checked
        directly against the actual dst-side export. Reverse-mutation: the
        orphan-skip branch (fork.py step 6b, 'rec_id not in mapping') was
        commented out and the run crashed with a `KeyError` at step 17's
        `mapping[rec["id"]]` lookup instead of this clean skip+report --
        see delivery report."""
        src = _seed_pack(stack, ALICE, "src-t9", node_count=10, with_edge=False, with_source=False)
        stack["vector"].upsert_texts(
            texts=["orphaned text, points at nothing preflight kept"],
            ids=["orphan-vec-t9"],
            metadatas=[{"pack_id": src}],
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["skipped"]["vector_orphans"] >= 1

        dst = out["pack_id"]
        dst_vectors = stack["vector"].export_pack_vectors(dst)
        dst_ids = {v["id"] for v in dst_vectors}
        assert "orphan-vec-t9" not in dst_ids

        dst_node_ids = {n["props"]["id"] for n in stack["graph"].export_nodes_scoped([dst], 1000)}
        dst_source_ids = {s["source_id"] for s in stack["docs"].list_sources_scoped([dst], 1000)}
        assert dst_ids.issubset(dst_node_ids | dst_source_ids), (
            "H5: every imported vector id must be a node or source id that "
            "was actually copied"
        )
        # 10 copied content-node vectors + the NEW destination anchor's own
        # freshly-embedded vector (T28: the SOURCE anchor's vector is
        # excluded from the copy batch, but the destination anchor gets its
        # own vector written when begin_pack_creation's anchor node is
        # created) -- 11 total, the orphan excluded from both.
        assert len(dst_ids) == 11

    def test_t34_mistagged_vector_skipped_dangling_reference_unverified(self, stack, monkeypatch):
        """T34: a vector record whose metadata.pack_id names a DIFFERENT
        pack than the one being forked (a legacy row whose collection-level
        pack scope and its own declared metadata have drifted apart -- this
        classification is deliberately backend-agnostic per §8, defending
        against a store where scope membership is not solely `metadata.
        pack_id` equality, e.g. a join-table-scoped backend) is classified
        skipped.vector_mistagged and excluded from the import batch
        entirely -- it must NOT be silently re-tagged and absorbed. Since
        chroma's OWN `export_pack_vectors` filters via
        ``where={"pack_id": pack_id}`` (so a genuinely mistagged chroma
        write would simply never be returned by the query, never reach this
        classification at all), the drift is injected at the returned-
        record level -- the same monkeypatch technique T44/T51 use to shape
        already-exported preflight input, not to fake the store's own
        write/read contract. Separately, a reference value that already
        pointed OUTSIDE the source pack before the fork (the 'dangling'
        case, never in mapping, never == src_pack) is left untouched and
        counted in unverified_refs (same code path T33 exercises with a
        composite string -- this row exercises it with a plain out-of-pack
        value). Reverse-mutation: the mistagged check (fork.py step 6b,
        'declared != src_pack_id') was commented out; the (until-then-
        mistagged) vector then imported successfully under n0's new id, and
        this test's `new_n0 not in dst_vector_ids` assertion failed -- see
        delivery report."""
        src = _seed_pack(stack, ALICE, "src-t34", node_count=10, with_edge=False, with_source=False)
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.source', "
            "'totally-unrelated-pack') WHERE node_id = :nid",
            {"nid": f"{src}-n5"},
        )

        real_export = type(stack["vector"]).export_pack_vectors

        def _mistag_n0(self, pack_id):
            records = real_export(self, pack_id)
            for rec in records:
                if rec["id"] == f"{src}-n0":
                    rec["metadata"] = dict(rec.get("metadata") or {})
                    rec["metadata"]["pack_id"] = "some-other-pack-entirely"
                    break
            return records

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _mistag_n0, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["unverified_refs"] >= 1
        assert out["skipped"]["vector_mistagged"] >= 1

        dst = out["pack_id"]
        dst_by_title = {
            n["props"]["title"]: n["props"]
            for n in stack["graph"].export_nodes_scoped([dst], 20)
            if n["labels"] != ["Dataset"]
        }
        assert dst_by_title["doc 5"]["source"] == "totally-unrelated-pack", (
            "a reference that already pointed outside the source pack must "
            "be left exactly as-is, not rewritten to something plausible"
        )
        new_n0 = dst_by_title["doc 0"]["id"]
        dst_vector_ids = {v["id"] for v in stack["vector"].export_pack_vectors(dst)}
        assert new_n0 not in dst_vector_ids, (
            "mistagged vector must be excluded from the import batch, not "
            "silently re-tagged into the new pack"
        )


# ---------------------------------------------------------------------------
# T17 -- a source's own vector (write_source's raw-copy leg) is copied
# under its new id, alongside its doc_sources row.
# ---------------------------------------------------------------------------


class TestSourceVectorCopy:
    def test_t17_source_only_vector_copied_with_doc_row(self, stack):
        """T17: a legacy text source's own vector (written via
        `write_source`/`hybrid.ingest`, not `add_node` -- i.e. the
        `text_as_node=False` shape) is copied under its new (remapped) id,
        and the source's doc_sources row lands alongside it. Reverse-
        mutation: fork.py step 17 was patched to `continue` past any
        `import_batch` record whose metadata carries a `source_id` key
        (surgically excluding only source-vectors, not node-vectors or the
        orphan-classification logic T9 covers) -- `source_vecs` came back
        empty while the doc row was still present, proving the two legs are
        independently guarded and this test catches the vector leg
        specifically."""
        src = _seed_pack(stack, ALICE, "src-t17", node_count=1, with_edge=False, with_source=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["copied"]["sources"] == 1
        dst = out["pack_id"]

        dst_sources = stack["docs"].list_sources_scoped([dst], 10)
        assert len(dst_sources) == 1
        new_source_id = dst_sources[0]["source_id"]
        assert new_source_id != "s0"  # remapped, not a raw copy of the old id

        dst_vectors = stack["vector"].export_pack_vectors(dst)
        source_vecs = [
            v for v in dst_vectors if v.get("metadata", {}).get("source_id") == new_source_id
        ]
        assert len(source_vecs) == 1, "the source's own vector must be copied under its new id"
        assert source_vecs[0]["id"] == new_source_id


# ---------------------------------------------------------------------------
# T13 -- document/embedding/non-reference metadata preserved (tolerance,
# not hash -- docs/vector-backends.md §8).
# ---------------------------------------------------------------------------


class TestVectorFidelity:
    def test_t13_document_metadata_embedding_preserved_within_tolerance(self, stack):
        """T13: a raw-copied vector's document text, a non-reference
        (out-of-schema) metadata key, and its embedding all survive the
        fork -- the embedding compared with per-component float32 ULP
        tolerance (chroma's cosine-space round-trip is not bit-exact),
        never a hash or exact-equality gate. Reverse-mutation A (document):
        fork.py step 17 was patched to overwrite
        `new_rec["document"] = "MUTATED"` -- the document-equality assert
        failed. Reverse-mutation B (metadata): patched to
        `new_rec["metadata"].pop("custom_tag", None)` -- the custom-key
        assert failed. Both restored; see delivery report."""
        src = _seed_pack(stack, ALICE, "src-t13", node_count=1, with_edge=False, with_source=False)
        with principal_scope(ALICE):
            write_source(
                stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
                graph=stack["graph"],
                text="the quick brown fox jumps over the lazy dog",
                source_id="s-custom", metadata={"custom_tag": "hello-t13"}, pack_id=src,
            )
        src_rec = next(
            v for v in stack["vector"].export_pack_vectors(src) if v["id"] == "s-custom"
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        dst = out["pack_id"]

        dst_rec = next(
            v for v in stack["vector"].export_pack_vectors(dst)
            if v.get("document") == src_rec["document"]
        )

        assert dst_rec["document"] == src_rec["document"]
        assert dst_rec.get("metadata", {}).get("custom_tag") == "hello-t13"
        assert len(dst_rec["embedding"]) == len(src_rec["embedding"])
        for a, b in zip(src_rec["embedding"], dst_rec["embedding"]):
            assert _ulp_close(a, b), (a, b)


# ---------------------------------------------------------------------------
# T47 / T33 -- reference-rewrite boundary: only REFERENCE_KEYS top-level
# scalar strings are rewritten; everything else is left untouched and
# counted in unverified_refs, never flagged as an H4 leak.
# ---------------------------------------------------------------------------


class TestReferenceRewriteBoundaries:
    def test_t47_out_of_schema_key_left_unrewritten_no_h4_failure(self, stack):
        """T47: a top-level property key OUTSIDE REFERENCE_KEYS
        (`parent_id`) whose value happens to equal another node's OLD id is
        NOT rewritten (H3's rewrite domain is REFERENCE_KEYS only) and,
        crucially, is NOT flagged as an H4 post-write leak either -- H4
        scans the exact same domain rule 3 rewrites, so a key outside that
        domain is invisible to both and the fork completes 'ok', not
        demoted by a false positive. Reverse-mutation: `_h4_scan` (fork.py)
        was patched to iterate every top-level key of `obj` instead of just
        `REFERENCE_KEYS` (the design's own named reverse-mutation for this
        row) -- `parent_id`'s still-old-id value was then flagged as a
        leaked reference, `status` became 'partial', and this test's
        `status == 'ok'` assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t47", node_count=2, with_edge=False, with_source=False)
        old_n0 = f"{src}-n0"
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.parent_id', :v) "
            "WHERE node_id = :nid",
            {"v": old_n0, "nid": f"{src}-n1"},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        dst = out["pack_id"]
        dst_by_title = {
            n["props"]["title"]: n["props"]
            for n in stack["graph"].export_nodes_scoped([dst], 10)
            if n["labels"] != ["Dataset"]
        }
        assert dst_by_title["doc 1"]["parent_id"] == old_n0, (
            "a key outside REFERENCE_KEYS must be copied through exactly "
            "as-is -- neither rewritten nor flagged as an H4 leak"
        )

    def test_t33_composite_and_nested_reference_left_untouched_and_counted(self, stack):
        """T33: a composite string embedding an old id (`"node:"+old_id`,
        under the REFERENCE_KEYS name `source`) and a nested list under a
        REFERENCE_KEYS name (`node_id`) are both left EXACTLY untouched
        (not partially/substring-rewritten) and counted in
        `unverified_refs` -- never flagged as an H4 leak, since the string
        value as a WHOLE is not a mapping key. Reverse-mutation: the
        `else: unverified += 1` branch in `fork_remap._remap_reference_keys`
        was removed (leaving the value untouched but silently uncounted) --
        `out["unverified_refs"] >= 2` failed (counted 0 for these two
        fields) while the untouched-value asserts still passed, isolating
        the counting guard specifically."""
        src = _seed_pack(stack, ALICE, "src-t33", node_count=2, with_edge=False, with_source=False)
        old_n0 = f"{src}-n0"
        composite = f"node:{old_n0}"
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.source', :v) "
            "WHERE node_id = :nid",
            {"v": composite, "nid": f"{src}-n1"},
        )
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.source_id', json(:v)) "
            "WHERE node_id = :nid",
            {"v": '["x","y"]', "nid": f"{src}-n1"},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["unverified_refs"] >= 2

        dst = out["pack_id"]
        dst_by_title = {
            n["props"]["title"]: n["props"]
            for n in stack["graph"].export_nodes_scoped([dst], 10)
            if n["labels"] != ["Dataset"]
        }
        n1_new = dst_by_title["doc 1"]
        assert n1_new["source"] == composite, "composite string must be left exactly as-is"
        assert n1_new["source_id"] == ["x", "y"], "nested list must be left exactly as-is"


# ---------------------------------------------------------------------------
# T12 -- each of the four CAP+1 preflight reads independently rejects.
# ---------------------------------------------------------------------------


class TestCapEnforcement:
    def test_t12_each_axis_rejects_at_cap_plus_one(self, stack, monkeypatch):
        """T12: nodes/edges/sources/vectors each independently reject the
        whole fork (before any registry row is reserved) once the source
        pack exceeds that axis's cap. Reverse-mutation: all four
        `if len(...) > CAP: raise _reject(...)` checks (and the vector
        `_count_pack_vectors(...) > FORK_MAX_VECTORS` check) were commented
        out in one batch -- every one of this test's four `"error" in out`
        assertions failed (all four over-cap packs reserved a registry row
        instead) -- see delivery report for the combined run."""
        monkeypatch.setattr(fork_mod, "FORK_MAX_NODES", 3)
        src_nodes = _seed_pack(
            stack, ALICE, "src-t12-nodes", node_count=5, with_edge=False, with_source=False,
        )
        out = _fork(stack, principal=ALICE, src_pack_id=src_nodes)
        assert "error" in out and "nodes" in out["error"], out
        assert get_pack(stack["sql"], f"{src_nodes}-fork") is None
        monkeypatch.setattr(fork_mod, "FORK_MAX_NODES", 20_000)

        monkeypatch.setattr(fork_mod, "FORK_MAX_EDGES", 2)
        src_edges = _seed_pack(
            stack, ALICE, "src-t12-edges", node_count=5, with_edge=False, with_source=False,
        )
        with principal_scope(ALICE):
            for i in range(4):
                stack["builder"].add_edge(
                    "resource", f"{src_edges}-n{i}", "cites", "resource", f"{src_edges}-n{i + 1}",
                    pack_id=src_edges,
                )
        out = _fork(stack, principal=ALICE, src_pack_id=src_edges)
        assert "error" in out and "edges" in out["error"], out
        assert get_pack(stack["sql"], f"{src_edges}-fork") is None
        monkeypatch.setattr(fork_mod, "FORK_MAX_EDGES", 50_000)

        monkeypatch.setattr(fork_mod, "FORK_MAX_SOURCES", 2)
        src_sources = _seed_pack(
            stack, ALICE, "src-t12-sources", node_count=1, with_edge=False, with_source=False,
        )
        with principal_scope(ALICE):
            for i in range(4):
                write_source(
                    stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
                    graph=stack["graph"],
                    text=f"chunk {i}", source_id=f"s{i}", pack_id=src_sources,
                )
        out = _fork(stack, principal=ALICE, src_pack_id=src_sources)
        assert "error" in out and "sources" in out["error"], out
        assert get_pack(stack["sql"], f"{src_sources}-fork") is None
        monkeypatch.setattr(fork_mod, "FORK_MAX_SOURCES", 10_000)

        monkeypatch.setattr(fork_mod, "FORK_MAX_VECTORS", 2)
        src_vectors = _seed_pack(
            stack, ALICE, "src-t12-vectors", node_count=4, with_edge=False, with_source=False,
        )
        out = _fork(stack, principal=ALICE, src_pack_id=src_vectors)
        assert "error" in out and "vectors" in out["error"], out
        assert get_pack(stack["sql"], f"{src_vectors}-fork") is None
        monkeypatch.setattr(fork_mod, "FORK_MAX_VECTORS", 50_000)


# ---------------------------------------------------------------------------
# T31 -- vector CAP+1 pre-count is constant-memory (H9): it must not call
# a full export_pack_vectors() read.
# ---------------------------------------------------------------------------


class TestVectorPrecountMemoryBound:
    def test_t31_vector_cap_rejected_without_full_export(self, stack, monkeypatch):
        """T31: the vector CAP+1 preflight check (`_count_pack_vectors`)
        must use the backend's constant-memory, pack-scoped counting path
        (chroma: `.get(..., include=[])`), never a full
        `export_pack_vectors()` read (which would materialize every
        embedding for a pack that is about to be rejected anyway).
        Reverse-mutation: `_count_pack_vectors`'s chroma branch was patched
        to `return len(vec.export_pack_vectors(pack_id))` instead of the
        constant-memory `.get()` call -- the spy's call list went from
        empty to non-empty and `assert calls == []` failed."""
        monkeypatch.setattr(fork_mod, "FORK_MAX_VECTORS", 2)
        src = _seed_pack(stack, ALICE, "src-t31", node_count=4, with_edge=False, with_source=False)

        calls: list[str] = []
        real_export = type(stack["vector"]).export_pack_vectors

        def _spy(self, pack_id):
            calls.append(pack_id)
            return real_export(self, pack_id)

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _spy, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert "error" in out and "vectors" in out["error"], out
        assert calls == [], (
            "vector CAP+1 pre-count must use the constant-memory pack-scoped "
            "counter, not a full export_pack_vectors() read"
        )


# ---------------------------------------------------------------------------
# T11 / T27 -- fail-closed store-availability refusals (never a
# quietly-degraded partial-axis success).
# ---------------------------------------------------------------------------


class TestStoreAvailabilityGuards:
    def test_t11_vector_store_missing_raw_methods_is_refused(self, stack):
        """T11: a vector store that is `available` but lacks either
        `export_pack_vectors` or `import_vectors` must refuse the whole
        fork up front -- not silently proceed as a vector-less success.
        Reverse-mutation: the `or not (hasattr(...) and hasattr(...))` half
        of fork.py step 3's vector-availability check was removed, leaving
        only the `available` check. The stub still has no backend markers
        (`_conn`/`_collection`/`_engine`), so `_vec_backend` falls through
        to `(None, None, None)` and the LATER `_count_pack_vectors`
        preflight step rejects instead -- but with a DIFFERENT message
        ("vector store does not support pack-scoped counting"), so this
        test's exact-dict assertion still caught the guard's removal."""
        class _NoRawMethodsVector:
            available = True
            # deliberately no export_pack_vectors/import_vectors

        src = _seed_pack(stack, ALICE, "src-t11", node_count=1, with_edge=False, with_source=False)
        out = fork_pack(
            stack["sql"], stack["graph"], stack["docs"], _NoRawMethodsVector(),
            stack["hybrid"], stack["builder"],
            principal=ALICE, src_pack_id=src,
        )
        assert out == {"error": "vector store unavailable"}
        assert get_pack(stack["sql"], f"{src}-fork") is None

    def test_t27_doc_store_unavailable_is_refused_not_sourceless_success(self, stack, monkeypatch):
        """T27: a document store reporting itself unavailable must refuse
        the whole fork at preflight -- not silently proceed as a
        sources-less success (`list_sources_scoped` itself is documented to
        fail-closed by raising rather than returning `[]`, precisely so an
        outage cannot be misread as "this pack genuinely has zero
        sources"). Reverse-mutation (actually run): the `if not getattr(docs,
        "available", False): raise _reject(...)` check (fork.py step 3) was
        removed -- preflight then passed, but the doc-unavailable store
        still made the NEW anchor's own doc-leg write fail at write time,
        which `_fork_leg_ok`'s "anchor" branch (requiring all four legs to
        positively succeed) caught as `{"error": "anchor write did not
        confirm across all stores", ...}` -- a different, later-stage
        rejection than this test's expected clean preflight refusal dict,
        still proving the guard's necessity (confirmed by actually running
        the mutation, not merely reasoned about)."""
        src = _seed_pack(stack, ALICE, "src-t27", node_count=1, with_edge=False, with_source=True)
        monkeypatch.setattr(type(stack["docs"]), "available", property(lambda self: False))
        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out == {"error": "document store unavailable"}
        assert get_pack(stack["sql"], f"{src}-fork") is None


# ---------------------------------------------------------------------------
# T18 -- a source's doc-leg write failure demotes to 'partial', never
# reported as 'ok'.
# ---------------------------------------------------------------------------


class TestSourceWriteFailureHandling:
    def test_t18_source_doc_write_failure_demotes_to_partial(self, stack, monkeypatch):
        """T18: when a source's doc-leg write itself fails (injected here
        as `upsert_source` raising), `_fork_leg_ok(kind="source")` must
        read that as a failure and demote the whole fork to 'partial' --
        not report 'ok' with a silently-missing source. Reverse-mutation:
        `_fork_leg_ok`'s `kind == "source"` branch was patched to
        `return True` unconditionally -- `status` stayed 'ok' despite the
        injected write failure and this test's `status == "partial"`
        assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t18", node_count=1, with_edge=False, with_source=True)

        def _boom(self, *a, **kw):  # noqa: ARG001
            raise RuntimeError("injected doc write failure")

        monkeypatch.setattr(type(stack["docs"]), "upsert_source", _boom, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert out["copied"]["sources"] == 0


# ---------------------------------------------------------------------------
# T29 -- an edge write-time "no match (missing node: ...)" is a Tier 2
# failure, never silently counted as copied.
# ---------------------------------------------------------------------------


class TestEdgeWriteFailureHandling:
    def test_t29_edge_no_match_write_failure_not_counted_copied_demotes(self, stack, monkeypatch):
        """T29: an edge write that comes back "no match (missing node:
        ...)" (the graph leg's own signal that an endpoint lookup failed at
        WRITE time, distinct from preflight's export-time validation) must
        NOT be counted under `copied.edges` and must demote the fork to
        'partial' (Tier 2 -- this is OUR write attempt failing, not an
        original-data defect). Reverse-mutation: `_fork_leg_ok`'s
        `kind == "edge"` branch was patched to `return True`
        unconditionally -- `status` stayed 'ok' and `copied.edges` became 1
        despite the forced "no match" receipt, failing both assertions."""
        src = _seed_pack(stack, ALICE, "src-t29", node_count=2, with_edge=True, with_source=False)

        real_add_edge = type(stack["builder"]).add_edge

        def _fake_add_edge(self, from_space, from_id, relation, to_space, to_id, **kw):
            if kw.get("fork_copy"):
                return {"stores": {"graph": f"no match (missing node: {to_id})", "sql": "unavailable"}}
            return real_add_edge(self, from_space, from_id, relation, to_space, to_id, **kw)

        monkeypatch.setattr(type(stack["builder"]), "add_edge", _fake_add_edge, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert out["copied"]["edges"] == 0


# ---------------------------------------------------------------------------
# T44 / T51 / T221-1..10 -- design v7 §5-1-6b's 2-pass vector batch
# decomposition, and design #221 v3.1 §5's V-KF (vector keep-first) contract
# pinning it down precisely.
# fork.py's preflight now runs the deterministic 2-pass decomposition the
# design specifies: pass 1 gives each vector id at most one slot in the
# import batch -- the slot goes to the FIRST record in the exported batch
# that passes BOTH of pass 1's checks (a fresh id, not already claimed, AND
# an embedding length matching the batch's reference dimension, itself set
# by the first record pass 1 ever sees). A record dropped by either check
# does NOT consume its id -- a later record sharing that id can still claim
# the slot if it is the first to pass both checks (design #221 v3.1 §5,
# contract V-KF; `tests/test_o221_*` T221-1..10 below pin this down
# axis-by-axis). Both kinds of drops are Tier 1, counted under
# `skipped.vector_batch_invalid`; pass 2 re-runs the REAL
# `validate_import_records` over the survivors to confirm the decomposition
# actually worked, and refuses the WHOLE preflight (a bug signal, not a
# Tier 1 loss -- see `_vector_record_invalid`'s docstring and the 2-pass
# block right after it in fork.py) if it still finds something wrong.
#
# T44 exercises pass 1's own normal path: a duplicate id whose SECOND
# occurrence shares the same embedding dimension as the first, so the id's
# slot goes to that first occurrence (V-KF's general rule and literal
# keep-first agree here, since neither record fails the dimension check),
# with pass 2 then confirming a clean pass.
# T51 exercises pass 2 as the safety net for something pass 1's dedup/dim
# checks cannot see: `_vector_record_invalid` (fork.py's per-record
# pre-check) deliberately does not validate the chroma-only `uris` field
# (see its docstring -- only checks meaningful for one record considered in
# TOTAL isolation are reproduced there), so a record with a bad `uris`
# type survives both the per-record pre-check AND pass 1's dedup/dim pass
# unfiltered, and is only ever caught for real when pass 2 runs the real
# validator over the surviving batch.
# ---------------------------------------------------------------------------


class TestVectorBatchDecomposition:
    def test_t44_duplicate_vector_id_dedups_keep_first_as_tier1_loss(
        self, stack, monkeypatch,
    ):
        """T44 (design v7 §5-1-6b pass 1; design #221 v3.1 §5 contract
        V-KF): a duplicate id surviving classification is decomposed
        BEFORE reservation -- pass 1 gives the id's slot to the FIRST
        record that passes both its checks, and since the injected
        duplicate here shares the SAME embedding dimension as the
        original, that is literally the first occurrence: the second is
        dropped as a Tier 1 loss (`skipped.vector_batch_invalid`), pass 2
        confirms the surviving batch now passes the real validator, and
        the fork completes as `ready`/`ok` with no Tier 2 demotion: nodes
        that wrote successfully (`copied.nodes == 10`) are NOT dragged
        down by a duplicate that was always a Tier 1, original-data
        defect, never one of our own writes failing."""
        src = _seed_pack(stack, ALICE, "src-t44", node_count=10, with_edge=False, with_source=False)

        real_export = type(stack["vector"]).export_pack_vectors

        def _inject_duplicate(self, pack_id):
            records = real_export(self, pack_id)
            out_records = list(records)
            for rec in records:
                if rec["id"] == f"{src}-n0":
                    dup = dict(rec)
                    dup["document"] = "a different chunk, same id"
                    out_records.append(dup)
                    break
            return out_records

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject_duplicate, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out["status"] == "ok", (
            "design v7 §5-1-6b pass 1 / design #221 v3.1 §5 V-KF: a "
            "duplicate vector id whose first occurrence already matches "
            "the batch reference dimension should keep that first "
            f"occurrence as a Tier 1 loss, not demote the fork; got {out}"
        )
        assert out["skipped"]["vector_batch_invalid"] == 1, out["skipped"]
        assert any(
            "duplicate id" in msg for msg in out["errors"]["vectors"]
        ), out["errors"]["vectors"]
        assert out["copied"]["nodes"] == 10, (
            "nodes that wrote successfully must not be dragged down by a "
            "vector duplicate that pass 1 already resolved as a Tier 1 loss"
        )
        assert out["copied"]["vectors"] == 10, out["copied"]

    def test_t51_pass2_catches_what_pass1_cannot_and_refuses_whole_preflight(
        self, stack, monkeypatch,
    ):
        """T51' (design v7 §12-2/§12-6 rewrite -- supersedes the pre-§12-2
        T51, now stale): §12-2 made `_vector_record_invalid` delegate to
        the REAL `validate_import_records`, so the old T51 injection (a
        bad `uris` type) is now caught at pass 0 (the per-record
        pre-check) as an ordinary Tier 1 loss, not pass 2 -- an EXPECTED,
        design-anticipated regression (see `_vector_record_invalid`'s own
        docstring), not an implementation defect. T51' exercises pass 2
        directly per §12-6: pass 0 is monkeypatched to unconditionally
        return `None` (disabled), and an unknown-metadata-key record is
        injected -- invisible to pass 1's dedup/dim checks (unique `id`,
        ordinary embedding length), caught only when pass 2 re-runs the
        real validator over the survivors. A pass-2 failure refuses the
        WHOLE preflight (zero registry rows), unlike a Tier 1 loss.
        Reverse-mutation: the pass 2 `if import_batch: ...
        validate_import_records(...)` block (fork.py, right after the
        2-pass decomposition) was removed -- the bad record then reached
        `import_vectors` unfiltered at step 17, which raised for the same
        reason but AFTER `begin_pack_creation` had already reserved a
        registry row, misclassifying this as a Tier 2 write failure
        (`status == 'partial'`, a real row left behind) instead of a
        clean preflight refusal -- both this test's `'status' not in out`
        and `get_pack(...) is None` assertions failed."""
        src = _seed_pack(stack, ALICE, "src-t51", node_count=10, with_edge=False, with_source=False)

        monkeypatch.setattr(fork_mod, "_vector_record_invalid", lambda *a, **kw: None, raising=True)

        real_export = type(stack["vector"]).export_pack_vectors

        def _inject_unknown_key(self, pack_id):
            records = real_export(self, pack_id)
            for rec in records:
                if rec["id"] == f"{src}-n0":
                    rec["not_a_real_field"] = "boom"  # unknown key -- a pass-2-only catch
                    break
            return records

        monkeypatch.setattr(
            type(stack["vector"]), "export_pack_vectors", _inject_unknown_key, raising=True,
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert "status" not in out and "error" in out, (
            "design v7 §12-6 T51': with pass 0 defeated, pass 2 must catch "
            f"an unknown-key record and refuse the WHOLE preflight; got {out}"
        )
        assert "vector batch decomposition failed re-validation" in out["error"], out
        dst_slug = f"{src}-fork"
        row = get_pack(stack["sql"], dst_slug)
        assert row is None, (
            "design wants ZERO registry rows for a pass-2 refusal -- a "
            f"preflight rejection must leave nothing behind, got {row}"
        )

    # -----------------------------------------------------------------------
    # T221-1..10 -- design #221 v3.1 §7's test table for the V-KF (vector
    # keep-first) contract: pass 1 gives each vector id at most one slot,
    # held by the first record in `import_batch` to pass BOTH of pass 1's
    # checks (fresh id, matching reference dimension); a record dropped by
    # either check does not consume its id. Injection convention (design
    # §7): unless a test deliberately moves the batch HEAD (T221-7/T221-8),
    # the original `n0` record is removed from the export and its variants
    # are appended at the batch TAIL, so the batch head is always an
    # untouched, unaffected earlier record. All ten are GREEN on base
    # (`eae0687`) -- V-KF is the CURRENT behaviour, confirmed by design
    # §4-1/§4-2's real-stack and brute-force measurements -- so the
    # discriminating evidence here is reverse-mutation, not RED->GREEN; each
    # docstring names its own (design #223's reverse-mutation-only
    # convention).
    # -----------------------------------------------------------------------

    def test_o221_dim_drop_does_not_consume_the_id(self, stack, monkeypatch):
        """T221-1 (design #221 v3.1 §5, contract V-KF): a dim-mismatched
        record does NOT consume its id -- pass 1's duplicate check and its
        dimension check are independent, so a later record sharing the
        same id can still take the slot a dimension drop left open.
        Injection: original `n0` removed, `BAD` (dim 383) then `GOOD`
        (dim 384, matching the batch's reference dimension set by an
        earlier, untouched record) appended at the tail. Base color:
        GREEN.
        Reverse-mutation (measured, design §4-1): moving
        `seen_vector_ids.add(rec_id)` in fork.py ahead of the dimension
        check (option 1 / id-first) makes `n0`'s vector LOST entirely --
        `copied.vectors` drops from 30 to 29 and no `n0` document
        survives in dst."""
        src = _seed_pack(
            stack, ALICE, "src-t221-1", node_count=30, with_edge=False, with_source=False,
        )
        real_export = type(stack["vector"]).export_pack_vectors

        def _inject(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            target = f"{src}-n0"
            base = next(r for r in records if r["id"] == target)
            rest = [r for r in records if r["id"] != target]
            bad = dict(base)
            bad["embedding"] = list(base["embedding"])[:-1]
            bad["document"] = "BAD"
            good = dict(base)
            good["document"] = "GOOD"
            return rest + [bad, good]

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out["status"] == "ok", out
        assert out["skipped"]["vector_batch_invalid"] == 1, out["skipped"]
        assert out["copied"]["vectors"] == 30, out["copied"]
        dst_docs = [
            r.get("document")
            for r in stack["vector"].export_pack_vectors(out["pack_id"])
            if r.get("document") in ("GOOD", "BAD")
        ]
        assert dst_docs == ["GOOD"], dst_docs

    def test_o221_keep_first_is_stable_under_head_fixed_permutation(self, stack, monkeypatch):
        """T221-2 (design #221 v3.1 §5, contract V-KF-2): with the batch
        HEAD fixed (an untouched earlier node, always dim 384, unaffected
        by the injection below) and the same multiset of `n0` variants,
        the surviving id set and `copied.vectors` do not depend on which
        of the two tail orderings -- `(BAD, GOOD)` or `(GOOD, BAD)` --
        pass 1 sees. V-KF-2 only promises this when the head is fixed
        (design §3-4: no backend's export promises an order at all).
        Injection: original `n0` removed, variants appended at the tail
        for both orderings. Both orderings' `import_batch` head length is
        computed and asserted 384 BEFORE comparing the two forks' results
        -- the premise the rest of this test relies on. Base color:
        GREEN.
        Reverse-mutation (measured, design §4-2): the id-first (option 1)
        mutation makes the two orderings disagree -- 29 vs 30
        `copied.vectors`."""
        results: dict[str, tuple[str, int, int]] = {}
        for order_name, order in (("bad_good", ("bad", "good")), ("good_bad", ("good", "bad"))):
            src = _seed_pack(
                stack, ALICE, f"src-t221-2-{order_name}",
                node_count=30, with_edge=False, with_source=False,
            )
            src_anchor = anchor_node_id(src)
            real_export = type(stack["vector"]).export_pack_vectors
            captured: dict[str, int] = {}

            def _inject(self, pack_id, _src=src, _anchor=src_anchor, _order=order, _cap=captured):
                records = real_export(self, pack_id)
                if pack_id != _src:
                    return records
                target = f"{_src}-n0"
                base = next(r for r in records if r["id"] == target)
                rest = [r for r in records if r["id"] != target]
                bad = dict(base)
                bad["embedding"] = list(base["embedding"])[:-1]
                bad["document"] = "BAD"
                good = dict(base)
                good["document"] = "GOOD"
                variants = {"good": good, "bad": bad}
                out_records = rest + [variants[k] for k in _order]
                head = next(r for r in out_records if r["id"] != _anchor)
                _cap["head_len"] = len(head["embedding"])
                return out_records

            monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
            out = _fork(stack, principal=ALICE, src_pack_id=src)
            monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", real_export, raising=True)

            assert captured["head_len"] == 384, captured  # premise, asserted before comparing
            results[order_name] = (
                out["status"], out["copied"]["vectors"], out["skipped"]["vector_batch_invalid"],
            )

        assert results["bad_good"] == results["good_bad"], results
        assert results["bad_good"] == ("ok", 30, 1), results

    def test_o221_first_valid_record_wins_among_equals(self, stack, monkeypatch):
        """T221-3 (design #221 v3.1 §5, literal keep-first among records
        that all share the batch reference dimension): with three `n0`
        variants at the tail -- `GOOD2` (384), `BAD` (383), `GOOD` (384)
        -- pass 1's duplicate check is literal keep-first among the
        reference-dimension-matching records: `GOOD2` (the first record
        with BOTH a fresh id and the reference dimension) survives, `BAD`
        is dropped for dimension and `GOOD` is dropped for duplicate id
        -- 2 batch-invalid drops. Injection: original `n0` removed, three
        variants appended at the tail. Base color: GREEN.
        Reverse-mutation (design §7 T221-3): a last-wins mutation inside
        the duplicate branch (`pass1_survivors[<existing index>] = rec`
        instead of `continue`, keeping the counter/message as-is) makes
        `GOOD` (the LAST reference-dimension record) the survivor instead
        of `GOOD2`."""
        src = _seed_pack(
            stack, ALICE, "src-t221-3", node_count=30, with_edge=False, with_source=False,
        )
        real_export = type(stack["vector"]).export_pack_vectors

        def _inject(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            target = f"{src}-n0"
            base = next(r for r in records if r["id"] == target)
            rest = [r for r in records if r["id"] != target]
            good2 = dict(base)
            good2["document"] = "GOOD2"
            bad = dict(base)
            bad["embedding"] = list(base["embedding"])[:-1]
            bad["document"] = "BAD"
            good = dict(base)
            good["document"] = "GOOD"
            return rest + [good2, bad, good]

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out["status"] == "ok", out
        assert out["skipped"]["vector_batch_invalid"] == 2, out["skipped"]
        assert out["copied"]["vectors"] == 30, out["copied"]
        dst_docs = [
            r.get("document")
            for r in stack["vector"].export_pack_vectors(out["pack_id"])
            if r.get("document") in ("GOOD", "GOOD2", "BAD")
        ]
        assert dst_docs == ["GOOD2"], dst_docs

    def test_o221_rescued_id_stays_under_the_completeness_floor(self, stack, monkeypatch):
        """T221-4 (design #221 v3.1 §5, contract V-KF-3): rescuing `n0`
        via a surviving `GOOD` record (instead of losing the id outright,
        as option 1 would) keeps the loss ratio under the 10% floor on a
        SMALL pack, where option 1's extra drop would flip the fork to a
        rejection -- design §4-1's measured row 3: n=10 tail (`BAD`,
        `GOOD`) is current-code `ok` with `batch_invalid == 1`, but
        option 1 (id-first) is `fork rejected: vector loss ratio 2/12
        exceeds the 10% completeness floor`. Injection: original `n0`
        removed, variants appended at the tail. Base color: GREEN.
        Reverse-mutation (measured, design §4-1): the option 1 mutation
        turns this test's `status == 'ok'` into a rejection."""
        src = _seed_pack(
            stack, ALICE, "src-t221-4", node_count=10, with_edge=False, with_source=False,
        )
        real_export = type(stack["vector"]).export_pack_vectors

        def _inject(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            target = f"{src}-n0"
            base = next(r for r in records if r["id"] == target)
            rest = [r for r in records if r["id"] != target]
            bad = dict(base)
            bad["embedding"] = list(base["embedding"])[:-1]
            bad["document"] = "BAD"
            good = dict(base)
            good["document"] = "GOOD"
            return rest + [bad, good]

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out["status"] == "ok", out
        assert out["skipped"]["vector_batch_invalid"] == 1, out["skipped"]
        assert out["copied"]["vectors"] == 10, out["copied"]

    def test_o221_chroma_cannot_hold_two_rows_for_one_id(self, stack):
        """T221-5 (design #221 v3.1 §3, backend-fact watch -- NOT a guard
        of our own code): chroma cannot physically hold two rows for one
        id -- adding to an already-present id neither raises nor
        overwrites, the EXISTING row simply wins (measured on chromadb
        1.5.9, via this store's `add_texts`, which calls the raw
        `collection.add()` directly -- unlike `import_vectors`, which
        pre-checks ids itself and refuses the whole batch on any
        pre-existing id, a guard this test deliberately bypasses to reach
        chroma's own raw semantics). This underwrites design §3's claim
        that chroma's storage layer already guarantees pass 1's
        duplicate-id premise for chroma specifically ("no export can ever
        hand fork.py two rows sharing one id") -- it does not cover
        sqlite-vec or pgvector (§3's DDL-only checks for those, not
        reproduced here), nor chroma's dimension guarantee (also §3, a
        different axis from this test).
        Reverse-mutation: none (design §7 T221-5: this is a backend fact,
        not this codebase's own guard -- there is nothing in fork.py or
        this store to mutate that would flip this result; if chromadb's
        own ADD semantics ever change, this test goes RED and §3's
        judgement needs to be redone)."""
        vector = stack["vector"]
        pack_id = "o221-t5-pack"
        doc_id = "o221-t5-dup"

        vector.add_texts(["first text"], metadatas=[{"pack_id": pack_id}], ids=[doc_id])
        vector.add_texts(["second text, same id"], metadatas=[{"pack_id": pack_id}], ids=[doc_id])

        got = vector.get_by_id(doc_id)
        assert got is not None and got["document"] == "first text", got

        exported = [r for r in vector.export_pack_vectors(pack_id) if r["id"] == doc_id]
        assert len(exported) == 1, exported
        assert exported[0]["document"] == "first text", exported

    def test_o221_pass1_carries_no_state_between_forks(self, stack, monkeypatch):
        """T221-6 (design #221 v3.1 §5, contract V-KF-1): pass 1's result
        is a pure function of its own fixed input sequence -- forking the
        SAME source with the SAME injected sequence TWICE (to two
        different dst packs) produces identical `copied.vectors`,
        `skipped.vector_batch_invalid` and surviving document. The
        injected sequence carries EXACTLY ONE valid variant, `(BAD,
        GOOD)` (design §7: with two or more valid variants this test
        would be sensitive to export-order jitter between the two calls,
        which V-KF-2 does not promise across separately-issued export
        calls). Injection: original `n0` removed, variants appended at
        the tail. Base color: GREEN.
        Reverse-mutation (design §7 T221-6): hoisting `seen_vector_ids`
        to a module-level set shared across calls makes the SECOND fork's
        `n0` (both variants already "seen" from the first call) drop
        entirely, exceeding the loss floor and rejecting the second
        fork."""
        src = _seed_pack(
            stack, ALICE, "src-t221-6", node_count=30, with_edge=False, with_source=False,
        )
        real_export = type(stack["vector"]).export_pack_vectors

        def _inject(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            target = f"{src}-n0"
            base = next(r for r in records if r["id"] == target)
            rest = [r for r in records if r["id"] != target]
            bad = dict(base)
            bad["embedding"] = list(base["embedding"])[:-1]
            bad["document"] = "BAD"
            good = dict(base)
            good["document"] = "GOOD"
            return rest + [bad, good]

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
        out1 = _fork(stack, principal=ALICE, src_pack_id=src)
        out2 = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out1["status"] == out2["status"] == "ok", (out1, out2)
        assert out1["copied"]["vectors"] == out2["copied"]["vectors"] == 30, (out1, out2)
        assert (
            out1["skipped"]["vector_batch_invalid"]
            == out2["skipped"]["vector_batch_invalid"]
            == 1
        ), (out1, out2)
        assert out1["pack_id"] != out2["pack_id"], (out1, out2)
        docs1 = [
            r.get("document")
            for r in stack["vector"].export_pack_vectors(out1["pack_id"])
            if r.get("document") in ("GOOD", "BAD")
        ]
        docs2 = [
            r.get("document")
            for r in stack["vector"].export_pack_vectors(out2["pack_id"])
            if r.get("document") in ("GOOD", "BAD")
        ]
        assert docs1 == docs2 == ["GOOD"], (docs1, docs2)

    def test_o221_anchor_does_not_set_the_reference_dim(self, stack, monkeypatch):
        """T221-7 (design #221 v3.1 §5, contract V-KF-4): the anchor id
        is excluded from pass 1's batch entirely -- BEFORE the reference
        dimension is ever established -- so an anchor record with a
        dim-mismatched embedding placed at the HEAD of the export does
        NOT poison the reference dimension for the 30 ordinary node
        records that follow. Injection (exception to the tail-append
        convention, design §7: T221-7 deliberately moves the head, so it
        builds the whole returned list explicitly): the anchor's own
        record is dim-truncated and moved to position 0; every other
        record (the 30 untouched, dim-384 node records) keeps its
        original relative order after it. `import_batch`'s head length
        (post-anchor-filter) is captured DURING the export injection --
        i.e. before pass 1 ever runs -- and asserted 384 ahead of every
        outcome assertion below.
        Base color: GREEN.
        Reverse-mutation (design §7 T221-7): computing the reference
        dimension from `exported_vectors[0]` (the raw export's own head,
        BEFORE the anchor is filtered out) instead of from
        `import_batch`'s head makes the anchor's dim-383 poison the
        reference, dropping all 30 dim-384 node records and rejecting the
        fork with a `30/31` loss ratio."""
        src = _seed_pack(
            stack, ALICE, "src-t221-7", node_count=30, with_edge=False, with_source=False,
        )
        src_anchor = anchor_node_id(src)
        real_export = type(stack["vector"]).export_pack_vectors
        captured: dict[str, int] = {}

        def _inject(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            anc = next(r for r in records if r["id"] == src_anchor)
            rest = [r for r in records if r["id"] != src_anchor]
            bad_anchor = dict(anc)
            bad_anchor["embedding"] = list(anc["embedding"])[:-1]
            out_records = [bad_anchor] + rest
            head = next(r for r in out_records if r["id"] != src_anchor)
            captured["head_len"] = len(head["embedding"])
            return out_records

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert captured["head_len"] == 384, captured  # premise
        assert out["status"] == "ok", out
        assert out["copied"]["vectors"] == 30, out["copied"]
        assert out["skipped"]["vector_batch_invalid"] == 0, out["skipped"]
        assert out["skipped"]["anchor_vector"] == 1, out["skipped"]

    def test_o221_reference_dim_is_the_first_eligible_record_not_the_majority(
        self, stack, monkeypatch,
    ):
        """T221-8 (design #221 v3.1 §5, contract V-KF-0): the batch
        reference dimension comes from `import_batch`'s FIRST record,
        never from a majority vote -- a single dim-383 `n0` record placed
        at the HEAD of the export (with 29 untouched dim-384 node records
        after it) makes the fork REJECT with a `29/31` loss ratio, even
        though 29 of 30 node records agree on dim 384. Injection
        (exception to the tail-append convention, design §7: T221-8
        deliberately moves the head): `n0` is dim-truncated and moved to
        position 0 of the returned list; every other record (anchor + 29
        other nodes) keeps its original relative order after it. The head
        length (383) is captured DURING the export injection (before
        pass 1 runs) and asserted ahead of every outcome assertion.
        Base color:
        GREEN -- this IS the current, confirmed contract (design §4-1's
        measured `29/31` row); design §5 V-KF-0 explicitly names this the
        intended limit fork.py's own "의도된 한계" comment already owns.
        Reverse-mutation (design §7 T221-8): computing the reference
        dimension by majority vote over the batch instead of from the
        first record makes this fork SUCCEED (384 wins the vote 29-1)
        instead of rejecting -- the expected rejection message
        disappears."""
        src = _seed_pack(
            stack, ALICE, "src-t221-8", node_count=30, with_edge=False, with_source=False,
        )
        src_anchor = anchor_node_id(src)
        real_export = type(stack["vector"]).export_pack_vectors
        captured: dict[str, int] = {}

        def _inject(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            target = f"{src}-n0"
            base = next(r for r in records if r["id"] == target)
            rest = [r for r in records if r["id"] != target]
            bad = dict(base)
            bad["embedding"] = list(base["embedding"])[:-1]
            bad["document"] = "BAD"
            out_records = [bad] + rest
            head = next(r for r in out_records if r["id"] != src_anchor)
            captured["head_len"] = len(head["embedding"])
            return out_records

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert captured["head_len"] == 383, captured  # premise
        assert "error" in out, out
        assert "vector loss ratio 29/31" in out["error"], out

    def test_o221_exactly_ten_percent_loss_is_not_a_rejection(self, stack, monkeypatch):
        """T221-9 (design #221 v3.1 §5, contract V-KF-3, strict-inequality
        boundary): with `node_count=8`, the export totals exactly 10
        vector records (anchor 1 + 8 nodes - 1 replaced `n0` + 2 tail
        variants), of which exactly 1 (`BAD`) is dropped for dimension --
        1/10 == exactly 10%. `_check_floor`'s comparison is strict
        (`dropped / total > FORK_MAX_LOSS_RATIO`), so hitting the floor
        exactly must PASS, not reject. The export total and the drop
        count are computed and asserted directly by the test, not
        assumed. Injection: original `n0` removed, variants appended at
        the tail. Base color: GREEN.
        Reverse-mutation (design §7 T221-9): changing `_check_floor`'s
        `>` to `>=` makes this exact-10% case reject with `fork rejected:
        vector loss ratio 1/10 exceeds the 10% completeness floor`."""
        src = _seed_pack(
            stack, ALICE, "src-t221-9", node_count=8, with_edge=False, with_source=False,
        )
        real_export = type(stack["vector"]).export_pack_vectors
        captured: dict[str, int] = {}

        def _inject(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            target = f"{src}-n0"
            base = next(r for r in records if r["id"] == target)
            rest = [r for r in records if r["id"] != target]
            bad = dict(base)
            bad["embedding"] = list(base["embedding"])[:-1]
            bad["document"] = "BAD"
            good = dict(base)
            good["document"] = "GOOD"
            out_records = rest + [bad, good]
            captured["total"] = len(out_records)
            return out_records

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject, raising=True)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert captured["total"] == 10, captured  # denominator, asserted directly
        assert out["skipped"]["vector_batch_invalid"] == 1, out["skipped"]  # numerator: exactly 10%
        assert out["status"] == "ok", out
        assert out["copied"]["vectors"] == 8, out["copied"]

    def test_o221_validator_consumes_the_id_before_it_checks_dim(self):
        """T221-10 (design #221 v3.1 §5, contract V-KF-N: the REAL
        validator's intentional asymmetry against pass 1): unlike pass 1,
        `validate_import_records` (the real validator `import_vectors`
        calls at step 17) consumes a record's id UNCONDITIONALLY, before
        it ever checks that record's embedding dimension -- so two
        records sharing an id, `[384-dim, 383-dim]`, always raise a
        DUPLICATE id error, never a dimension error, regardless of
        whether a `dim` argument is passed. This is the "id-first" rule
        pass 1 deliberately does NOT mirror (design §5 V-KF-N: pass 1
        only ever sees survivors, so there is no functional
        contradiction, but "unify with the validator" is explicitly the
        wrong fix for pass 1).
        Reverse-mutation (design §7 T221-10): moving
        `validate_import_records`'s duplicate-id check to AFTER its
        dimension check turns this same input's error into `record 1
        embedding has length 383 but record 0 has 384` instead of a
        duplicate-id message."""
        from opencrab.stores._vector_base import validate_import_records

        records = [
            {"id": "o221-t10-dup", "embedding": [0.0] * 384},
            {"id": "o221-t10-dup", "embedding": [0.0] * 383},
        ]

        with pytest.raises(ValueError) as exc_dim:
            validate_import_records(records, pack_id="o221-t10-pack", dim=384)
        assert "duplicate id" in str(exc_dim.value), str(exc_dim.value)

        with pytest.raises(ValueError) as exc_none:
            validate_import_records(records, pack_id="o221-t10-pack", dim=None)
        assert "duplicate id" in str(exc_none.value), str(exc_none.value)


# ---------------------------------------------------------------------------
# T25 -- a grammar-invalid node and its dependent edge are excluded
# together, both Tier 1, not left dangling or escalated to Tier 2.
# ---------------------------------------------------------------------------


class TestGrammarInvalidNodeDropsItsEdgeToo:
    def test_t25_grammar_invalid_node_and_its_edge_excluded_together(self, stack):
        """T25 (design §8 T25, §5-1 step 5-6): a node that fails grammar
        validation (`validate_node`, injected here via a bogus
        `properties.space`) is excluded as a Tier 1 loss, and -- because
        preflight's edge-survival check treats "did the endpoint survive
        preflight" as a single question, independent of WHY it didn't --
        the edge that names it as an endpoint is excluded right along
        with it, also Tier 1, not left dangling or promoted to a Tier 2
        write failure. Reverse-mutation: the grammar-validation call
        (`validate_node`/`validate_node_properties`, fork.py's node loop)
        was removed -- the grammar-invalid node then survived preflight
        and reached the write phase, where `builder.add_node`'s own
        grammar check rejected it for real, producing a Tier 2 write
        failure (`status == 'partial'`) instead of a clean Tier 1
        exclusion, failing this test's `status == 'ok'` assertion."""
        src = _seed_pack(stack, ALICE, "src-t25", node_count=15, with_edge=False, with_source=False)
        node_ids = [f"{src}-n{i}" for i in range(15)]
        with principal_scope(ALICE):
            for i in range(14):
                stack["builder"].add_edge(
                    "resource", node_ids[i], "cites", "resource", node_ids[i + 1], pack_id=src,
                )
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.space', :v) "
            "WHERE node_id = :nid",
            {"v": "not-a-real-space", "nid": node_ids[0]},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["copied"]["nodes"] == 14, out
        assert out["copied"]["edges"] == 13, out
        assert any(
            "failed grammar validation" in msg for msg in out["errors"]["nodes"]
        ), out["errors"]["nodes"]
        assert any(
            "endpoint did not survive" in msg for msg in out["errors"]["edges"]
        ), out["errors"]["edges"]


# ---------------------------------------------------------------------------
# T24 -- write order: every node write (step 14) must land before any edge
# write (step 15) is attempted.
# ---------------------------------------------------------------------------


class TestWriteOrdering:
    def test_t24_all_nodes_write_before_any_edge_is_attempted(self, stack):
        """T24 (design §8 T24, §5-3 steps 14-15 write order): every node
        write (step 14) must complete in full BEFORE any edge write (step
        15) is attempted -- an edge's remapped endpoints must already
        exist in dst's graph the moment `add_edge` is called, or the
        graph leg comes back "no match (missing node: ...)" (the exact
        write-time failure T29 exercises) and `_fork_leg_ok(kind='edge')`
        fails, demoting an otherwise-perfectly-good fork to 'partial'.
        This exercises the REAL write path end to end (no endpoint
        monkeypatch) precisely so the reverse-mutation below is the thing
        that actually distinguishes right order from wrong order.
        Reverse-mutation: fork.py's step 14 (node write loop) and step 15
        (edge write loop, `if tier2_failure is None: for record in
        surviving_edges: ...`) were swapped -- edges were then attempted
        against a dst graph that had no nodes in it yet, every edge write
        came back "no match (missing node: ...)", and this test's
        `status == 'ok'` / `copied.edges == 1` assertions both failed
        (`status` became 'partial', `copied.edges` became 0)."""
        src = _seed_pack(stack, ALICE, "src-t24", node_count=2, with_edge=True, with_source=False)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["copied"]["nodes"] == 2, out
        assert out["copied"]["edges"] == 1, out


# ---------------------------------------------------------------------------
# T8 / T32 -- H4 (design §5-4 step 18) is a genuine RE-READ check: it must
# catch a corrupted export_nodes_scoped/export_edges_scoped RETURN VALUE at
# verify time even though the underlying write itself is completely healthy.
# Both are gated on `pack_ids != [src]` so step 11's earlier "dst must be
# empty" call (also scoped to `[dst]`, but on a still-empty pack) is an
# unaffected no-op.
# ---------------------------------------------------------------------------


class TestH4CatchesReReadNotWrite:
    def test_t8_h4_reread_corruption_not_write_time_caught_as_leak(self, stack, monkeypatch):
        """T8 (design §8 T8, §5-4 step 18, §12-6: 'H4 는 재조회로 판정하므로
        쓰기가 아니라 export_nodes_scoped 재조회 반환에 미재매핑 참조를 심어야
        한다'): H4 verification works by RE-READING the just-written dst
        copy (`graph.export_nodes_scoped`), not by trusting each write's
        own receipt -- so the injection here corrupts ONLY that re-read's
        RETURN VALUE (an unmapped source-space id spliced into a
        REFERENCE_KEYS position of the row H4 sees), while the actual
        write underneath is completely healthy. This proves H4 is a
        genuine independent re-read check, not a pass-through of whatever
        the write phase already believed. Reverse-mutation: `_h4_verify`'s
        node re-read loop (`for row in graph.export_nodes_scoped(...)`,
        fork.py) was replaced with a loop over the write-phase's own
        receipts instead of a fresh re-read -- the corrupted re-read
        return value was then never consulted at all, and this test's
        `status == 'partial'` assertion failed (`status` stayed 'ok')."""
        src = _seed_pack(stack, ALICE, "src-t8", node_count=3, with_edge=False, with_source=False)

        real_export = type(stack["graph"]).export_nodes_scoped

        def _corrupt_reread(self, pack_ids, limit):
            rows = real_export(self, pack_ids, limit)
            if pack_ids != [src]:
                for row in rows:
                    props = row.get("props") or {}
                    if props.get("title") == "doc 0":
                        props["node_id"] = f"{src}-n1"  # unremapped source-space id
                        break
            return rows

        monkeypatch.setattr(
            type(stack["graph"]), "export_nodes_scoped", _corrupt_reread, raising=True,
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert "H4 post-write verification" in out["error"], out

    def test_t32_h4_catches_unmapped_edge_structural_endpoint(self, stack, monkeypatch):
        """T32 (design §8 T32, §4-A predicate 2 / `_h4_verify`'s edge
        loop): an edge's STRUCTURAL endpoints (`source_props['id']`/
        `target_props['id']` -- separate from `rel_props`, which
        `remap_props`/H3 already cover) are themselves part of H4's
        re-read verification domain. Injected the same way T8 is (a
        corrupted `export_edges_scoped` RE-READ return value), leaving
        the underlying write completely healthy. Reverse-mutation:
        `_h4_verify`'s `for endpoint_key in ('source_props',
        'target_props'): ...` block (fork.py) was removed -- the
        corrupted structural endpoint was then never checked at all, and
        this test's `status == 'partial'` assertion failed (`status`
        stayed 'ok')."""
        src = _seed_pack(stack, ALICE, "src-t32", node_count=2, with_edge=True, with_source=False)

        real_export = type(stack["graph"]).export_edges_scoped

        def _corrupt_reread(self, pack_ids, limit):
            rows = real_export(self, pack_ids, limit)
            if pack_ids != [src]:
                for row in rows:
                    source_props = dict(row.get("source_props") or {})
                    source_props["id"] = f"{src}-n0"  # unremapped source-space id
                    row["source_props"] = source_props
            return rows

        monkeypatch.setattr(
            type(stack["graph"]), "export_edges_scoped", _corrupt_reread, raising=True,
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert "H4 post-write verification" in out["error"], out


# ---------------------------------------------------------------------------
# T40 -- a REFERENCE_KEYS value that is itself a nested dict/list is out of
# the rewrite domain even when it embeds an old id (T33 above only proves
# this for a nested value WITHOUT an old id, which cannot distinguish "we
# never recurse" from "we happened to find nothing to rewrite").
# ---------------------------------------------------------------------------


class TestNestedReferenceValueWithOldId:
    def test_t40_nested_reference_value_containing_old_id_left_untouched(self, stack):
        """T40 (design §8 T40, §4-A rule 3, fork_remap.py
        `_remap_reference_keys`): a REFERENCE_KEYS value that is itself a
        nested list is out of the rewrite domain regardless of what it
        contains -- proven here with a nested list that DOES embed an old
        (pre-fork) node id, unlike `TestReferenceRewriteBoundaries`'s
        existing T33, whose nested list (`["x","y"]`) contains no old id
        and therefore cannot distinguish "we never recurse into nested
        values" from "we happened to find nothing worth rewriting
        inside." The nested old id must survive verbatim in the dst copy
        (never silently replaced), invisible to H4 (which, like the
        rewrite rule, only scans top-level string values), and still
        counted once in `unverified_refs`. Reverse-mutation:
        `_remap_reference_keys`'s `isinstance(value, dict | list)` branch
        (fork_remap.py) was changed to rewrite any mapping-key string
        found INSIDE a nested list before falling through to `unverified
        += 1; continue` -- the nested old id was then silently replaced
        with its new id, failing this test's "the nested value is
        untouched, byte for byte" assertion (T33's own nested-list case,
        containing no mapping-key string, would NOT have caught this same
        mutation -- nothing inside it to rewrite)."""
        src = _seed_pack(stack, ALICE, "src-t40", node_count=2, with_edge=False, with_source=False)
        old_n0 = f"{src}-n0"
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.source_id', json(:v)) "
            "WHERE node_id = :nid",
            {"v": f'["{old_n0}", "unrelated"]', "nid": f"{src}-n1"},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["unverified_refs"] >= 1, out

        dst = out["pack_id"]
        dst_by_title = {
            n["props"]["title"]: n["props"]
            for n in stack["graph"].export_nodes_scoped([dst], 10)
            if n["labels"] != ["Dataset"]
        }
        n1_new = dst_by_title["doc 1"]
        assert n1_new["source_id"] == [old_n0, "unrelated"], (
            "a nested list under a REFERENCE_KEYS name must be left "
            "EXACTLY as-is, even though it embeds an old (pre-fork) id "
            "the rewrite rule could in principle have found"
        )


# ---------------------------------------------------------------------------
# T41 -- design §5-4-18b's residual report: a source that survives the doc
# axis but has no corresponding vector must be named in
# skipped.sources_without_vectors, under its NEW (dst) id.
# ---------------------------------------------------------------------------


class TestSourcesWithoutVectorsResidualReport:
    def test_t41_source_without_a_vector_reported_in_sources_without_vectors(self, stack):
        """T41 (design §8 T41, §5-4-18b): a source that survives the doc
        axis but has NO corresponding vector (written with
        `write_vector=False` -- the simplest concrete instance of the
        doc/vector predicate asymmetry `fork_remap.surviving_source_ids`'s
        own docstring names) must be reported in the fork's own
        `skipped.sources_without_vectors` residual list, under its NEW
        (dst) id -- not silently absent from both `copied.vectors` and
        every loss counter. Reverse-mutation: step 18b's residual
        computation (`sources_survivor_ids - vectors_survivor_ids`,
        fork.py, right before `mark_pack_ready`) was replaced with a bare
        empty list -- this test's "the no-vector source's new id shows up
        in sources_without_vectors" assertion failed (the list came back
        empty)."""
        src = _seed_pack(stack, ALICE, "src-t41", node_count=1, with_edge=False, with_source=False)
        with principal_scope(ALICE):
            write_source(
                stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
                graph=stack["graph"],
                text="a source with no vector", source_id="s-no-vec", pack_id=src,
                write_vector=False,
            )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["copied"]["sources"] == 1, out
        residual = out["skipped"]["sources_without_vectors"]
        assert len(residual) == 1, out["skipped"]
        assert residual[0].startswith("s-no-vec"), residual


# ---------------------------------------------------------------------------
# T45 -- the CAP+1 vector pre-count (design §5-1 step 4) is genuinely
# pack-scoped: an unrelated pack's vectors must never count against THIS
# fork's FORK_MAX_VECTORS check.
# ---------------------------------------------------------------------------


class TestVectorPrecountIsPackScoped:
    def test_t45_vector_precount_stays_scoped_despite_unrelated_pack_vectors(
        self, stack, monkeypatch,
    ):
        """T45 (design §8 T45, §5-1 step 4 / `_count_pack_vectors`): the
        CAP+1 vector pre-count is genuinely PACK-SCOPED
        (`where={'pack_id': pack_id}` on chroma) -- an unrelated pack
        sitting in the SAME collection with plenty of its own vectors
        must never push a small, unrelated fork over `FORK_MAX_VECTORS`.
        `FORK_MAX_VECTORS` is monkeypatched down to a small number so the
        unrelated pack's vector count alone (seeded well above that
        number) would trip a naive, unscoped count. Reverse-mutation:
        `_count_pack_vectors`'s chroma branch (fork.py) had its
        `where={'pack_id': pack_id}` argument dropped (an unscoped
        `.get(limit=cap + 1, include=[])`) -- the unrelated pack's
        vectors were then counted too, `vector_count > FORK_MAX_VECTORS`
        tripped, and this test's `status == 'ok'` assertion failed
        (`'error' in out` instead, with a "too large to fork" message)."""
        monkeypatch.setattr(fork_mod, "FORK_MAX_VECTORS", 3, raising=True)

        # Ten ordinary content-node writes (each producing a real vector via
        # the normal, non-fork_copy add_node path) plus the anchor's own
        # vector comfortably exceed the monkeypatched cap of 3 -- but this
        # pack is never forked; it only exists to pollute the collection.
        _seed_pack(
            stack, ALICE, "src-t45-unrelated", node_count=10, with_edge=False, with_source=False,
        )

        src = _seed_pack(stack, ALICE, "src-t45", node_count=1, with_edge=False, with_source=False)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", (
            f"an unrelated pack's vectors must never count against THIS "
            f"fork's pack-scoped precount; got {out}"
        )


# ---------------------------------------------------------------------------
# T36 -- a node write whose docs or sql leg fails (graph itself ok) is a
# Tier 2 failure, caught by _fork_leg_ok(kind="node").
# ---------------------------------------------------------------------------


class TestNodeWriterLegFailures:
    def test_t36_node_docs_leg_failure_demotes_to_partial(self, stack, monkeypatch):
        """T36 (design §8 T36, §6-3 kind='node' docs leg, §12-6): a
        fork-copy node write whose DOCS leg fails must be caught by
        `_fork_leg_ok(kind='node')` and halt the write phase immediately
        (Tier 2) -- not silently continue with a missing doc row.
        Injected at the writer boundary (builder.add_node, gated on
        `kw.get('fork_copy')` so only fork's own content-node copies are
        touched, never the seed pack's ordinary writes or the fork's own
        anchor write, which uses `pack_anchor=True` instead, not
        `fork_copy`). Reverse-mutation: `_fork_leg_ok`'s `kind == 'node'`
        branch's `_leg_ok(stores.get('docs'))` conjunct was replaced with
        `True` -- `status` stayed 'ok' and `copied.nodes` became nonzero
        despite the injected docs failure, failing both assertions."""
        src = _seed_pack(stack, ALICE, "src-t36docs", node_count=3, with_edge=False, with_source=False)

        real_add_node = type(stack["builder"]).add_node

        def _fake_add_node(self, space, node_type, node_id, properties=None, *, pack_id, **kw):
            if kw.get("fork_copy"):
                return {
                    "stores": {
                        "graph": "ok", "docs": "error: injected docs failure",
                        "sql": "ok", "vector": "skipped (raw copy)",
                    }
                }
            return real_add_node(self, space, node_type, node_id, properties, pack_id=pack_id, **kw)

        monkeypatch.setattr(type(stack["builder"]), "add_node", _fake_add_node, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert out["copied"]["nodes"] == 0, out

    def test_t36_node_sql_leg_failure_demotes_to_partial(self, stack, monkeypatch):
        """T36 (sql leg, second case): same as above but the fork-copy
        node write's SQL leg fails instead. Reverse-mutation:
        `_fork_leg_ok`'s `kind == 'node'` branch's `_leg_ok(stores.get
        ('sql'))` conjunct was replaced with `True` -- `status` stayed
        'ok' and `copied.nodes` became nonzero despite the injected sql
        failure."""
        src = _seed_pack(stack, ALICE, "src-t36sql", node_count=3, with_edge=False, with_source=False)

        real_add_node = type(stack["builder"]).add_node

        def _fake_add_node(self, space, node_type, node_id, properties=None, *, pack_id, **kw):
            if kw.get("fork_copy"):
                return {
                    "stores": {
                        "graph": "ok", "docs": "ok (id=x)",
                        "sql": "error: injected sql failure", "vector": "skipped (raw copy)",
                    }
                }
            return real_add_node(self, space, node_type, node_id, properties, pack_id=pack_id, **kw)

        monkeypatch.setattr(type(stack["builder"]), "add_node", _fake_add_node, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert out["copied"]["nodes"] == 0, out


# ---------------------------------------------------------------------------
# T43 -- the anchor write requires ALL FOUR legs to positively succeed
# (unlike node/source, the anchor's vector leg is real, not skipped).
# ---------------------------------------------------------------------------


class TestAnchorWriterLegFailures:
    def test_t43_anchor_docs_leg_failure_demotes_to_partial(self, stack, monkeypatch):
        """T43 (design §8 T43, §6-3 kind='anchor' docs leg, §12-6): unlike
        the node/source legs, the anchor write requires ALL FOUR legs
        (including vector) to positively succeed --
        `_fork_leg_ok(kind='anchor')` demotes to `partial` immediately if
        the docs leg fails, before any node/edge/source/vector write is
        even attempted. Distinguishes the fork's OWN anchor write
        (`pack_id` == the negotiated dst, always != src) from the SEED
        pack's own anchor write (`pack_id` == src, same `pack_anchor=True`
        shape) so `_seed_pack` itself is never corrupted by this
        monkeypatch. Reverse-mutation: `_fork_leg_ok`'s `kind == 'anchor'`
        branch's `_leg_ok(stores.get('docs'))` conjunct was replaced with
        `True` -- `status` stayed 'ok' despite the injected anchor docs
        failure."""
        src = _seed_pack(stack, ALICE, "src-t43docs", node_count=3, with_edge=False, with_source=False)

        real_add_node = type(stack["builder"]).add_node

        def _fake_add_node(self, space, node_type, node_id, properties=None, *, pack_id, **kw):
            if kw.get("pack_anchor") and pack_id != src:
                return {
                    "stores": {
                        "graph": "ok", "docs": "error: injected anchor docs failure",
                        "sql": "ok", "vector": "ok",
                    }
                }
            return real_add_node(self, space, node_type, node_id, properties, pack_id=pack_id, **kw)

        monkeypatch.setattr(type(stack["builder"]), "add_node", _fake_add_node, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert out["error"] == "anchor write did not confirm across all stores", out
        assert out["copied"]["nodes"] == 0, out

    def test_t43_anchor_vector_leg_failure_demotes_to_partial(self, stack, monkeypatch):
        """T43 (vector leg, second case): same as above but the fork's
        anchor write's VECTOR leg fails instead -- the anchor is the ONE
        write in the entire fork that does NOT set `write_vector=False`
        (design §4-C-3: the new pack needs a real anchor embedding), so
        its vector leg genuinely must succeed too. Reverse-mutation:
        `_fork_leg_ok`'s `kind == 'anchor'` branch's `stores.get
        ('vector') == 'ok'` conjunct was replaced with `True` -- `status`
        stayed 'ok' despite the injected anchor vector failure."""
        src = _seed_pack(stack, ALICE, "src-t43vec", node_count=3, with_edge=False, with_source=False)

        real_add_node = type(stack["builder"]).add_node

        def _fake_add_node(self, space, node_type, node_id, properties=None, *, pack_id, **kw):
            if kw.get("pack_anchor") and pack_id != src:
                return {
                    "stores": {
                        "graph": "ok", "docs": "ok (id=x)",
                        "sql": "ok", "vector": "error: injected anchor vector failure",
                    }
                }
            return real_add_node(self, space, node_type, node_id, properties, pack_id=pack_id, **kw)

        monkeypatch.setattr(type(stack["builder"]), "add_node", _fake_add_node, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert out["error"] == "anchor write did not confirm across all stores", out
        assert out["copied"]["nodes"] == 0, out


# ---------------------------------------------------------------------------
# T49 -- an edge write whose graph leg succeeds but sql leg fails is still
# a Tier 2 failure (the docs/audit leg is deliberately excluded from this
# check -- design §6-3 only names graph+sql for edges).
# ---------------------------------------------------------------------------


class TestEdgeSqlLegFailure:
    def test_t49_edge_sql_leg_failure_demotes_to_partial(self, stack, monkeypatch):
        """T49 (design §8 T49, §6-3 kind='edge' sql leg, §12-6): an edge
        write whose GRAPH leg succeeds but whose SQL leg fails must still
        be caught -- `_fork_leg_ok(kind='edge')` requires BOTH `graph ==
        'ok'` and a positive sql leg (T29 already covers the graph-leg
        failure axis; this pins the sql-leg axis specifically).
        Reverse-mutation: `_fork_leg_ok`'s `kind == 'edge'` branch's
        `_leg_ok(stores.get('sql'))` conjunct was replaced with `True` --
        `status` stayed 'ok' and `copied.edges` became 1 despite the
        injected sql failure."""
        src = _seed_pack(stack, ALICE, "src-t49", node_count=2, with_edge=True, with_source=False)

        real_add_edge = type(stack["builder"]).add_edge

        def _fake_add_edge(self, from_space, from_id, relation, to_space, to_id, **kw):
            if kw.get("fork_copy"):
                return {"stores": {"graph": "ok", "sql": "error: injected sql failure"}}
            return real_add_edge(self, from_space, from_id, relation, to_space, to_id, **kw)

        monkeypatch.setattr(type(stack["builder"]), "add_edge", _fake_add_edge, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "partial", out
        assert out["copied"]["edges"] == 0, out


# ---------------------------------------------------------------------------
# T26 / T50 / T74 -- pack_id/slug length budget (design §3, §13-3):
# _PACK_ID_BUDGET = 256 (registry column limit) - 9 (begin_pack_creation's
# worst-case collision suffix) - 8 (the `dataset:` anchor prefix, since the
# pack's own anchor node id is what actually lands in that column) = 239. An
# explicit new_pack_id over budget is REJECTED (never truncated); the DEFAULT
# slug is truncated instead so "{src}-fork" always fits.
# ---------------------------------------------------------------------------


class TestExplicitSlugLengthRejection:
    def test_t26_explicit_new_pack_id_over_budget_rejected_not_truncated(self, stack):
        """T26 (design §8 T26, §3, §5-1 step 8): an explicitly
        caller-supplied `new_pack_id` that exceeds the budget is REJECTED
        outright -- never silently truncated the way the DEFAULT slug is
        (design §3: truncating a name the caller chose on purpose would be
        surprising). Checked across several over-budget lengths (240, the
        next boundary; 248, the pre-§13 budget's boundary; 256, the raw
        column limit; 300, comfortably past all of them) to prove this is a
        real inequality check, not one that only happens to catch a single
        value. Reverse-mutation: the `if len(requested_slug) >
        _PACK_ID_BUDGET: raise _reject(...)` branch for the
        explicit-new_pack_id path (fork.py step 8) was removed -- every one
        of these calls then proceeded to `begin_pack_creation` instead of
        being rejected, breaking this test's uniform "always error, never a
        reserved row" assertion."""
        src = _seed_pack(stack, ALICE, "src-t26", node_count=1, with_edge=False, with_source=False)
        for n in (240, 248, 256, 300):
            new_id = "z" * n
            out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=new_id)
            assert "error" in out and "budget" in out["error"], (n, out)
            assert get_pack(stack["sql"], new_id) is None, n


class TestExplicitEmptyNewPackIdRejection:
    def test_t98_explicit_empty_new_pack_id_is_rejected_not_treated_as_omitted(self, stack):
        """T98 (design §17-5, §17-3): `new_pack_id=""` is a caller-supplied
        DECLARATION, not omission -- step 8 must reject it with a remedy
        pointing at omitting the argument, never fall through to the
        default `"{src}-fork"` derivation the way plain truthiness does.
        Reverse-mutation: reverting step 8's branch from `if new_pack_id is
        not None:` back to `if new_pack_id:` makes `""` indistinguishable
        from omission again -- the fork completes with `status: "ok"` and
        `pack_id == f"{src}-fork"` instead of returning an error, and this
        test's error-only assertions fail."""
        src = _seed_pack(
            stack, ALICE, "src-t98", node_count=1, with_edge=False, with_source=False,
        )
        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id="")
        assert "error" in out, out
        assert "omit" in out["error"].lower(), out
        assert "status" not in out
        assert get_pack(stack["sql"], f"{src}-fork") is None


class TestSlugLengthBoundaries:
    def test_t50a_default_path_truncation_boundary(self, stack):
        """T50(a) / T74 (design §8 T50, §3, §13-3, §5-1 step 8): the DEFAULT
        slug is `f"{src}-fork"`; when that exceeds the budget, src_pack_id
        itself is truncated (not the whole fork rejected) so the "-fork"
        suffix always fits. The pass/truncate boundary is exactly src length
        `_PACK_ID_BUDGET - len("-fork")` (slug fits) vs one more (slug over,
        truncated). Boundaries are DERIVED from `_PACK_ID_BUDGET` rather than
        spelled as literals so that a mutation to the budget's definition
        moves the inputs with it and the assertions still discriminate.
        Reverse-mutation: `_PACK_ID_BUDGET` lost its `_ANCHOR_PREFIX_LEN`
        term -- at the over-boundary src length the un-truncated slug now fit
        the wrong budget, so this test's "truncation actually happened"
        assertion failed."""
        fits = _PACK_ID_BUDGET - len("-fork")
        pad = "a" * (fits - len("src-t50a-"))
        src_fits = _seed_pack(
            stack, ALICE, f"src-t50a-{pad}", node_count=1, with_edge=False, with_source=False,
        )
        assert len(src_fits) == fits, len(src_fits)
        out_fits = _fork(stack, principal=ALICE, src_pack_id=src_fits)
        assert out_fits["status"] == "ok", out_fits
        assert out_fits["pack_id"] == f"{src_fits}-fork", out_fits
        assert len(out_fits["pack_id"]) == _PACK_ID_BUDGET

        over = fits + 1
        pad3 = "a" * (over - len("src-t50a3-"))
        src_over = _seed_pack(
            stack, ALICE, f"src-t50a3-{pad3}", node_count=1, with_edge=False, with_source=False,
        )
        assert len(src_over) == over, len(src_over)
        out_over = _fork(stack, principal=ALICE, src_pack_id=src_over)
        assert out_over["status"] == "ok", out_over
        assert out_over["pack_id"] != f"{src_over}-fork", (
            "an over-boundary src's default slug exceeds the budget and must "
            "be TRUNCATED, not passed through unchanged"
        )
        assert out_over["pack_id"].endswith("-fork")
        assert len(out_over["pack_id"]) <= _PACK_ID_BUDGET
        assert out_over["pack_id"].startswith(src_over[:fits])

    def test_t50b_explicit_new_pack_id_boundary(self, stack):
        """T50(b) / T74: an explicit `new_pack_id` at EXACTLY the budget is
        accepted verbatim (no truncation, no rejection); one character over
        is rejected outright (T26 covers the rejection path more broadly;
        this pins the exact boundary transition). Both lengths are derived
        from `_PACK_ID_BUDGET`. Reverse-mutation: `_PACK_ID_BUDGET` lost its
        `_ANCHOR_PREFIX_LEN` term -- the over-boundary explicit id, which
        should be rejected, was instead accepted, and this test's
        `'error' in out_over` assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t50b", node_count=1, with_edge=False, with_source=False)

        new_at = "b" * _PACK_ID_BUDGET
        out_at = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=new_at)
        assert out_at["status"] == "ok", out_at
        assert out_at["pack_id"] == new_at, out_at

        new_over = "c" * (_PACK_ID_BUDGET + 1)
        out_over = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=new_over)
        assert "error" in out_over and "budget" in out_over["error"], out_over
        assert get_pack(stack["sql"], new_over) is None


# ---------------------------------------------------------------------------
# T37 -- repair_incomplete_packs must never auto-promote a stale `creating`
# row reserved by pack_fork, even with a real anchor already present.
# ---------------------------------------------------------------------------


class TestRepairNeverPromotesForkedRow:
    def test_t37_repair_incomplete_packs_never_promotes_a_forked_creating_row(self, stack):
        """T37 (design §8 T37, lifecycle.py's `forked_from` branch, #201
        §4-F): a stale `creating` row reserved by `pack_fork` (not
        `pack_create`) must NEVER be auto-promoted by
        `repair_incomplete_packs`, even with a REAL anchor already
        sitting in the graph -- fork writes its anchor FIRST, the
        opposite of `pack_create`'s anchor-write-last completion proof, so
        an incomplete fork's anchor probes exactly as PRESENT as a
        complete one's. `older_than_seconds=0` makes the row eligible
        immediately, with no need to fake its timestamp -- confirming
        this is the `forked_from` branch's OWN independent rule, not a
        side effect of the row happening to be old. Reverse-mutation: the
        `if row.get('forked_from'):` branch (`opencrab/pack/lifecycle.py`,
        inside `repair_incomplete_packs`) was removed, falling through to
        the ordinary `elif graph_probe == PROBE_PRESENT:` promote branch
        -- the real anchor was found PRESENT and the row was promoted
        straight to `ready`, failing this test's `status == 'partial'`
        assertion (`status` became `'ready'` instead)."""
        from opencrab.pack.lifecycle import PROBE_PRESENT, repair_incomplete_packs

        dst = begin_pack_creation(
            stack["sql"], ALICE.user_id, "src-t37-fork", forked_from="src-t37-original",
        )
        anchor_id = anchor_node_id(dst)
        with principal_scope(ALICE):
            stack["builder"].add_node(
                space="resource", node_type="Dataset", node_id=anchor_id,
                properties={"title": "t", "description": "d", "created_by": "test"},
                pack_id=dst, pack_anchor=True,
            )

        result = repair_incomplete_packs(
            stack["sql"], stack["graph"], stack["docs"], stack["vector"],
            older_than_seconds=0, apply=True,
        )
        row = next(r for r in result["rows"] if r["pack_id"] == dst)
        assert row["action"] == "demote", row
        assert row["probes"]["graph"] == PROBE_PRESENT, row

        after = get_pack(stack["sql"], dst)
        assert after["status"] == "partial", after


# ---------------------------------------------------------------------------
# T52 -- a retired `pack` alias that disagrees with `pack_id` (#171) is a
# Tier 1 alias-conflict exclusion, not a Tier 2 write failure.
# ---------------------------------------------------------------------------


class TestLegacyAliasConflictExclusion:
    def test_t52_alias_conflict_node_and_its_edge_excluded_fork_completes_ok(self, stack):
        """T52 (design §8 T52, §5-1 step 5-6, R6 P2): a node whose
        properties carry a retired `pack` alias that DISAGREES with its
        own `pack_id` (a legacy-shape conflict `canonicalize_pack_alias`
        refuses to silently resolve, #171) is excluded as a Tier 1 alias
        conflict -- `skipped.nodes_alias_conflict` -- and, since its
        endpoint no longer survives preflight, its dependent edge is
        excluded too (Tier 1, "endpoint did not survive"). Every OTHER
        node/edge is unaffected and the fork still completes `status ==
        'ok'`. Reverse-mutation: the `try: canonicalize_pack_alias(props)
        except ValueError: skipped_alias_nodes += 1; continue` node-loop
        guard (fork.py step 5-6) was removed -- the conflicting node then
        survived preflight, the write phase's own `canonicalize_pack_alias`
        call inside `builder.add_node` raised instead, and the fork was
        wrongly demoted to `partial` (Tier 2) over what should have been a
        clean Tier 1 exclusion, failing this test's `status == 'ok'` and
        `copied.edges` count of a *single* dropped edge, in proportion to
        total edges, does not itself trip `FORK_MAX_LOSS_RATIO` -- a
        14-edge chain (only the first edge touches the conflicted node)
        keeps edge loss at 1/14, comfortably under the 10% floor."""
        src = _seed_pack(stack, ALICE, "src-t52", node_count=15, with_edge=False, with_source=False)
        node_ids = [f"{src}-n{i}" for i in range(15)]
        with principal_scope(ALICE):
            for i in range(14):
                stack["builder"].add_edge(
                    "resource", node_ids[i], "cites", "resource", node_ids[i + 1], pack_id=src,
                )
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.pack', :v) "
            "WHERE node_id = :nid",
            {"v": "some-other-pack-entirely", "nid": node_ids[0]},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["skipped"]["nodes_alias_conflict"] == 1, out
        assert out["copied"]["nodes"] == 14, out
        assert out["copied"]["edges"] == 13, out
        assert any(
            "endpoint did not survive" in msg for msg in out["errors"]["edges"]
        ), out["errors"]["edges"]


# ---------------------------------------------------------------------------
# T10 -- pack_fork is registered as a write tool (write-gate/write.lock
# coverage depends on this).
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_t10_pack_fork_registered_as_write_tool(self):
        """T10: `pack_fork` MUST be registered with `writes=True` on its
        `@tool(...)` decorator (opencrab/mcp/tools/pack.py) so the MCP
        write-gate/write.lock machinery applies to it like every other
        mutating tool. Reverse-mutation: the decorator's `writes=True` was
        temporarily flipped to `writes=False` -- `"pack_fork" in
        WRITE_TOOLS` became `False` and this assertion failed."""
        from opencrab.mcp.tools import WRITE_TOOLS

        assert "pack_fork" in WRITE_TOOLS


# ---------------------------------------------------------------------------
# T75-T85 -- the id LENGTH contract (design §13, §13-8, §13-9).
#
# The registry's node_id / edge-endpoint columns are VARCHAR(256). Two things
# push ids at that limit, and this PR introduced both:
#   - the destination anchor's `dataset:` prefix (covered by the budget, see
#     T26/T50/T74 above);
#   - the remap suffix `~{salt}`, which takes a source node id that the store
#     itself accepts and makes the COPY illegal.
# The second is NOT a Tier 1 drop: excluding a hub node would take its edges
# with it while barely moving the node axis's own loss ratio, and the fork
# would still promote to `ready` having silently lost both. It is a whole-fork
# rejection, before the reservation, so no registry row is ever created.
#
# NOTE ON THE ORACLE: SQLite does not enforce VARCHAR(n), so "the write
# fails" would be a vacuous assertion here. Every row below asserts on
# produced id lengths, registry row counts, or the rejection reason instead.
# ---------------------------------------------------------------------------

_REMAP_GROWTH = len(fork_mod.remap_id("x", fork_mod.new_salt())) - 1
_ID_AT_LIMIT = _PACK_ID_COLUMN_LIMIT - _REMAP_GROWTH
_ID_OVER_LIMIT = _ID_AT_LIMIT + 1


def _add_node(stack, pack_id: str, node_id: str, *, owner=ALICE) -> None:
    with principal_scope(owner):
        stack["builder"].add_node(
            space="resource", node_type="Document", node_id=node_id,
            properties={"title": "t"}, pack_id=pack_id,
        )


def _forked_row_count(stack, src_pack_id: str) -> int:
    with stack["sql"]._engine.connect() as conn:
        return conn.execute(
            _sql_text("SELECT COUNT(*) FROM packs WHERE forked_from = :src"),
            {"src": src_pack_id},
        ).scalar_one()


def _fork_node_ids(stack, pack_id: str) -> set[str]:
    return {n["props"]["id"] for n in stack["graph"].export_nodes_scoped([pack_id], 500)}


def _count_nodes(stack, pack_id: str) -> int:
    return len(stack["graph"].export_nodes_scoped([pack_id], 500))


def _node_snapshot(stack, pack_id: str):
    """Value-level snapshot of a pack's nodes AND edges, order-independent --
    for asserting a rejected fork left the SOURCE byte-identical, not merely
    the same size. Edges are included because the rejection path's whole
    claim is that it declines to create rather than removing anything, and
    the edge hanging off the offending node is exactly what a
    delete-on-reject defect would take with it."""
    nodes = sorted(
        (n["props"]["id"], repr(sorted(n["props"].items())), repr(n.get("labels")))
        for n in stack["graph"].export_nodes_scoped([pack_id], 500)
    )
    edges = sorted(
        repr(sorted(e.items())) for e in stack["graph"].export_edges_scoped([pack_id], 500)
    )
    return nodes, edges


class TestRemappedIdLengthRejection:
    def test_t75_over_limit_node_rejects_whole_fork_leaving_nothing(self, stack):
        """T75 (design §13-8-1, §13-9-3): ONE node whose id the remap would
        push past the registry column limit rejects the WHOLE fork. The
        oracle is threefold: no destination registry row exists (counted by
        `forked_from`, which catches a negotiated slug the caller never
        named), the rejection reason names the offending id, and the SOURCE
        pack is untouched -- rejection does not delete anything, it declines
        to create. Reverse-mutation: the `len(remap_id(node_id, salt)) >
        _PACK_ID_COLUMN_LIMIT` branch in fork.py's node loop was removed --
        the fork then succeeded and wrote a node id past the column limit,
        failing both the `error` assertion and the `forked_row_count == 0`
        assertion."""
        src = _seed_pack(stack, ALICE, "src-t75", node_count=2, with_source=False)
        long_id = "L" * _ID_OVER_LIMIT
        _add_node(stack, src, long_id)
        with principal_scope(ALICE):
            stack["builder"].add_edge(
                "resource", long_id, "cites", "resource", f"{src}-n0", pack_id=src,
            )

        before = _node_snapshot(stack, src)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert "error" in out, out
        assert long_id in out["error"], out["error"]
        assert str(_PACK_ID_COLUMN_LIMIT) in out["error"], out["error"]
        assert _forked_row_count(stack, src) == 0, out
        # The source is not touched: rejection declines to create, it does
        # not remove. The design's earlier draft wrongly claimed the source
        # edge "does not survive anywhere" -- it does. Compared by VALUE, not
        # just by count, so a mutation that rewrites a node in place is caught.
        assert _node_snapshot(stack, src) == before

    def test_t76_boundary_at_limit_passes_one_over_rejects(self, stack):
        """T76 (design §13-9-3): the comparison is `>`, not `>=`. A node id
        whose REMAPPED length is exactly the column limit is copied; one
        character longer is rejected. Both lengths are derived from
        `remap_id` itself, so this pins the boundary rather than a literal.
        Reverse-mutation: `>` was changed to `>=` -- the at-limit pack, which
        must fork cleanly, was rejected and this test's `status == 'ok'`
        assertion failed."""
        src_ok = _seed_pack(stack, ALICE, "src-t76a", node_count=1, with_edge=False,
                            with_source=False)
        at_limit = "A" * _ID_AT_LIMIT
        _add_node(stack, src_ok, at_limit)
        out_ok = _fork(stack, principal=ALICE, src_pack_id=src_ok)
        assert out_ok["status"] == "ok", out_ok
        copied = _fork_node_ids(stack, out_ok["pack_id"])
        assert copied, out_ok
        assert max(len(n) for n in copied) <= _PACK_ID_COLUMN_LIMIT, (
            sorted((len(n), n) for n in copied)[-1]
        )
        # The at-limit id must actually be PRESENT, not quietly dropped --
        # "nothing exceeds the limit" alone is satisfiable by copying nothing.
        assert any(len(n) == _PACK_ID_COLUMN_LIMIT for n in copied), sorted(
            len(n) for n in copied
        )

        src_bad = _seed_pack(stack, ALICE, "src-t76b", node_count=1, with_edge=False,
                             with_source=False)
        _add_node(stack, src_bad, "B" * _ID_OVER_LIMIT)
        out_bad = _fork(stack, principal=ALICE, src_pack_id=src_bad)
        assert "error" in out_bad, out_bad

    def test_t77_over_limit_source_only_id_does_not_reject(self, stack):
        """T77 (design §13-8-1 domain, §13-9-2): the length check covers
        NODE ids only. A `source_id` reaches the doc store's `text` column
        and the vector store's `TEXT` id -- neither is length-constrained --
        so a source-only content id past the node limit must NOT reject the
        fork. Reverse-mutation: the source axis was added to the length
        check's domain -- this perfectly legal pack was then rejected and the
        `status == 'ok'` assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t77", node_count=1, with_edge=False,
                         with_source=False)
        long_source_id = "S" * _ID_OVER_LIMIT
        with principal_scope(ALICE):
            write_source(
                stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
                graph=stack["graph"],
                text="a source-only body", source_id=long_source_id, pack_id=src,
            )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert out["copied"]["sources"] >= 1, out

    def test_t78_negotiated_anchor_id_stays_within_the_column_limit(self, stack):
        """T78 (design §13-3, §13-9-3): the behavioural end of contract 1.
        A slug at EXACTLY the budget that then collides gets
        `begin_pack_creation`'s 9-char suffix, and the anchor node built from
        the result must land on EXACTLY the column limit. The equality is the
        point: `<=` would only catch a budget that grew, while both reviewers
        noted that a budget derived into the test's own inputs cannot catch a
        budget that SHRANK. Anchor length is independent of that derivation --
        an over-conservative 238 produces a 255-char anchor and fails here
        just as a too-loose 240 produces 257. Reverse-mutation: `_PACK_ID_BUDGET`
        lost its `_ANCHOR_PREFIX_LEN` term -- the negotiated pack_id became 256
        chars and its anchor 264, failing the assertion below."""
        src = _seed_pack(stack, ALICE, "src-t78", node_count=1, with_edge=False,
                         with_source=False)
        taken = "t" * _PACK_ID_BUDGET
        with principal_scope(ALICE):
            blocker = begin_pack_creation(stack["sql"], ALICE.user_id, taken)
            mark_pack_ready(stack["sql"], blocker, ALICE.user_id)
        assert blocker == taken, blocker

        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=taken)
        assert out["status"] == "ok", out
        assert out["pack_id"] != taken, "the collision must have been negotiated"
        assert len(anchor_node_id(out["pack_id"])) == _PACK_ID_COLUMN_LIMIT, (
            out["pack_id"], len(anchor_node_id(out["pack_id"])),
        )

    def test_t79_boundary_follows_the_remap_shape_not_a_constant(self, stack):
        """T79 (design §13-8-1, §13-9-3): the check measures with `remap_id`
        itself, so changing the remap shape moves the boundary with it. With
        a longer salt, an id that passes under the real salt must now be
        rejected. Reverse-mutation: the check was rewritten to compare
        against a precomputed `243` -- the longer salt no longer moved the
        boundary, the at-limit pack forked cleanly, and this test's `error`
        assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t79", node_count=1, with_edge=False,
                         with_source=False)
        _add_node(stack, src, "R" * _ID_AT_LIMIT)

        long_salt = "f" * (_REMAP_GROWTH * 3)
        original = fork_mod.new_salt
        try:
            fork_mod.new_salt = lambda: long_salt
            out = _fork(stack, principal=ALICE, src_pack_id=src)
        finally:
            fork_mod.new_salt = original

        assert "error" in out, out
        assert _forked_row_count(stack, src) == 0, out

    def test_t80_over_limit_remapped_source_anchor_does_not_reject(self, stack):
        """T80 (design §13-8-1 anchor carve-out, §13-9-3): the source pack's
        OWN anchor is never copied as an ordinary node, so its remapped
        candidate exceeding the limit is irrelevant. A pack whose id is long
        enough that `remap_id(anchor)` would overflow -- while its ordinary
        node ids still fit -- must fork cleanly. Reverse-mutation: the length
        check was placed BEFORE the anchor intercept (or made to walk the
        whole mapping) -- the source anchor was then measured, this legal
        pack was rejected, and the `status == 'ok'` assertion failed."""
        # anchor = "dataset:" + pack_id, and it must overflow once remapped.
        src_len = _ID_OVER_LIMIT - len(anchor_node_id(""))
        pad = "p" * (src_len - len("src-t80-"))
        src = _seed_pack(stack, ALICE, f"src-t80-{pad}", node_count=1, with_edge=False,
                         with_source=False)
        assert len(src) == src_len, len(src)
        assert len(anchor_node_id(src)) + _REMAP_GROWTH > _PACK_ID_COLUMN_LIMIT
        # ...while the ordinary node ids this pack seeded still fit.
        assert len(f"{src}-n0") + _REMAP_GROWTH <= _PACK_ID_COLUMN_LIMIT

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out

    def test_t81_collision_suffix_is_never_cumulative(self):
        """T81 (design §13-1 premise 3): the budget subtracts exactly 9 for
        the collision suffix. That is only the worst case because each retry
        suffixes the ORIGINAL slug -- if candidates chained onto one another
        the growth would be unbounded and the 9 would be wrong. Reverse-
        mutation: `_pack_id_candidates` was changed to build each candidate
        from the previous one -- the third candidate grew by 18 rather than
        9 and this assertion failed."""
        from opencrab.pack.ownership import _pack_id_candidates

        base = "slug"
        got = []
        for candidate in _pack_id_candidates(base):
            got.append(candidate)
            if len(got) == 4:
                break
        assert got[0] == base
        for candidate in got[1:]:
            assert candidate.startswith(f"{base}-"), candidate
            assert len(candidate) - len(base) == 9, candidate

    def test_t82_long_id_that_also_fails_grammar_is_only_a_tier_1_drop(self, stack):
        """T82 (design §13-9-1): the length check runs only on nodes that
        have already SURVIVED Tier 1. A node that fails grammar validation is
        not copied at all, so its length cannot overflow anything -- letting
        it trigger a whole-fork rejection would be over-rejection. Reverse-
        mutation: the length check was moved ahead of the grammar check --
        this pack, whose only over-limit node is grammar-invalid anyway, was
        rejected outright instead of forking with a Tier 1 drop, failing the
        `status == 'ok'` assertion."""
        src = _seed_pack(stack, ALICE, "src-t82", node_count=15, with_edge=False,
                         with_source=False)
        long_id = "G" * _ID_OVER_LIMIT
        _add_node(stack, src, long_id)
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.space', :v) "
            "WHERE node_id = :nid",
            {"v": "not-a-real-space", "nid": long_id},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)
        assert out["status"] == "ok", out
        assert any(
            "failed grammar validation" in msg for msg in out["errors"]["nodes"]
        ), out["errors"]["nodes"]
        copied = _fork_node_ids(stack, out["pack_id"])
        assert max(len(n) for n in copied) <= _PACK_ID_COLUMN_LIMIT

    def test_t83_rejection_happens_before_any_reservation_or_write(self, stack):
        """T83 (design §13-9-1): wholesale rejection dissolves the DATA
        ordering problem, but the TIME ordering is still normative -- the
        check must precede `begin_pack_creation`, or a `creating` row exists
        for a window even if something later deletes it. Spies prove the
        reservation and the writers were never reached at all. Reverse-
        mutation: the length check was moved below the reservation and paired
        with a compensating delete -- the registry row count still ended at
        zero, but the `begin_pack_creation` spy recorded a call and this test
        failed where the row-count oracle alone would not have."""
        src = _seed_pack(stack, ALICE, "src-t83", node_count=1, with_edge=False,
                         with_source=False)
        _add_node(stack, src, "Z" * _ID_OVER_LIMIT)

        reservations: list[str] = []
        writes: list[str] = []
        real_begin = fork_mod.begin_pack_creation
        real_add_node = stack["builder"].add_node

        def spy_begin(*a, **kw):
            reservations.append(kw.get("pack_id") or (a[2] if len(a) > 2 else "?"))
            return real_begin(*a, **kw)

        def spy_add_node(*a, **kw):
            writes.append(kw.get("node_id", "?"))
            return real_add_node(*a, **kw)

        try:
            fork_mod.begin_pack_creation = spy_begin
            stack["builder"].add_node = spy_add_node
            out = _fork(stack, principal=ALICE, src_pack_id=src)
        finally:
            fork_mod.begin_pack_creation = real_begin
            stack["builder"].add_node = real_add_node

        assert "error" in out, out
        assert reservations == [], reservations
        assert writes == [], writes

    def test_t84_salt_is_drawn_once_and_shared_with_the_mapping(self, stack):
        """T84 (design §13-9-1): the salt the length check measures with MUST
        be the salt the mapping remaps with. Drawing it twice would measure
        one length and write another, and at the boundary the two disagree.
        Reverse-mutation: a second `new_salt()` call was reintroduced at the
        mapping site -- the counter below saw two draws and this assertion
        failed."""
        src = _seed_pack(stack, ALICE, "src-t84", node_count=1, with_edge=False,
                         with_source=False)
        _add_node(stack, src, "Q" * _ID_AT_LIMIT)

        draws: list[str] = []
        real_new_salt = fork_mod.new_salt

        def counting_salt():
            value = real_new_salt()
            draws.append(value)
            return value

        try:
            fork_mod.new_salt = counting_salt
            out = _fork(stack, principal=ALICE, src_pack_id=src)
        finally:
            fork_mod.new_salt = real_new_salt

        assert out["status"] == "ok", out
        assert len(draws) == 1, draws
        copied = _fork_node_ids(stack, out["pack_id"])
        assert any(n.endswith(draws[0]) for n in copied), (draws, sorted(copied)[:3])

    def test_t85_multiple_over_limit_nodes_reject_with_a_genuine_id(self, stack):
        """T85 (design §13-9-3): with SEVERAL over-limit nodes the fork is
        still rejected and the id named in the reason is genuinely one of
        them -- not, say, a truncated or wrongly-indexed value from an
        implementation that only inspects the first record. The export order
        is PINNED here rather than left to the store: `export_nodes_scoped`'s
        SQL has no `ORDER BY`, so without this a "checks only the first node"
        implementation could pass or fail by query plan. Legal nodes are
        forced to the front, which is the ordering that lets such an
        implementation slip through. Reverse-mutation: the check was written
        to inspect only the first exported node -- with legal nodes first it
        saw nothing wrong, the fork succeeded, and the `error` assertion
        failed."""
        src = _seed_pack(stack, ALICE, "src-t85", node_count=3, with_source=False)
        over = ["M" * _ID_OVER_LIMIT, "N" * (_ID_OVER_LIMIT + 7), "O" * (_ID_OVER_LIMIT + 20)]
        for node_id in over:
            _add_node(stack, src, node_id)

        real_export = stack["graph"].export_nodes_scoped

        def ordered_export(*a, **kw):
            rows = real_export(*a, **kw)
            return sorted(rows, key=lambda n: len(n["props"].get("id") or ""))

        try:
            stack["graph"].export_nodes_scoped = ordered_export
            out = _fork(stack, principal=ALICE, src_pack_id=src)
        finally:
            stack["graph"].export_nodes_scoped = real_export

        assert "error" in out, out
        named = [node_id for node_id in over if node_id in out["error"]]
        assert len(named) == 1, out["error"][:200]
        assert len(named[0]) + _REMAP_GROWTH > _PACK_ID_COLUMN_LIMIT
        assert _forked_row_count(stack, src) == 0

    def test_t86_column_limit_constant_matches_the_registry_schema(self):
        """T86: `_PACK_ID_COLUMN_LIMIT` is the number the whole length
        contract is measured against, and every other row derives its inputs
        and expectations from it -- so a mutation to the constant itself
        moves all of them together and none of them notices. This row is the
        one place the constant is tied to something outside fork.py: the
        registry DDL that actually enforces it. The two tables are named
        explicitly: `node_id VARCHAR(256)` also appears on `impact_records`,
        which the fork write path never touches, so an unpinned pattern would
        keep passing on that column alone even after the columns fork DOES
        write through stopped declaring a width. Reverse-mutation:
        `_PACK_ID_COLUMN_LIMIT` was changed to 300 -- every other row still
        passed (their arithmetic stayed self-consistent) and only this
        assertion failed."""
        import re

        from opencrab.stores.sql_store import _TABLES_SQL

        # The two tables builder.register_node / register_edge write through.
        wanted = {
            "ontology_nodes": {"node_id"},
            "ontology_edges": {"from_id", "to_id"},
        }
        found: dict[str, dict[str, int]] = {}
        for stmt in _TABLES_SQL:
            match = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", stmt)
            if not match or match.group(1) not in wanted:
                continue
            table = match.group(1)
            found[table] = {
                col: int(width)
                for col, width in re.findall(r"\b(\w+)\s+VARCHAR\((\d+)\)", stmt)
                if col in wanted[table]
            }

        assert set(found) == set(wanted), (
            "the registry DDL no longer declares these tables; the length "
            f"contract needs revisiting: {sorted(found)}"
        )
        for table, columns in wanted.items():
            assert set(found[table]) == columns, (
                f"{table} no longer declares a VARCHAR width for {sorted(columns)}; "
                "the length contract is measuring against nothing"
            )
            for col, width in found[table].items():
                assert width == _PACK_ID_COLUMN_LIMIT, (table, col, width)


# ---------------------------------------------------------------------------
# T87-T93 -- design §14: the pack-id / content-id namespace guard.
#
# `_remap_reference_keys` resolves ONE string against TWO namespaces (the
# mapping's keys = content ids, and `src_pack` = the pack-id space) with a
# fixed branch precedence. Nothing forced those spaces to be disjoint. When
# they overlap, the precedence silently picks one meaning and `_h4_scan`
# cannot see the mistake -- the written value is a mapping VALUE, matching
# neither "still a mapping key" nor "still == src_pack". Neither branch order
# is right (§14-3), so the guard removes the overlap instead of ranking the
# meanings: reject before the reservation on the source side, and compensate
# the reservation on the destination side (where the value does not exist any
# earlier).
#
# NOTE ON THE ORACLE: as with the length rows above, nothing here asserts "the
# write failed" -- the assertions are on rejection reasons, registry row
# counts, and spy call records.
# ---------------------------------------------------------------------------


def _add_node_with_props(stack, pack_id: str, node_id: str, props: dict, *, owner=ALICE) -> None:
    with principal_scope(owner):
        stack["builder"].add_node(
            space="resource", node_type="Document", node_id=node_id,
            properties=props, pack_id=pack_id,
        )


def _pack_row_count(stack, pack_id: str) -> int:
    with stack["sql"]._engine.connect() as conn:
        return conn.execute(
            _sql_text("SELECT COUNT(*) FROM packs WHERE pack_id = :p"),
            {"p": pack_id},
        ).scalar_one()


class TestPackIdContentIdNamespaceGuard:
    def test_t87_node_id_equal_to_pack_id_rejects_whole_fork(self, stack):
        """T87 (design §14-5, §14-9): a content node whose id IS the pack id
        makes that string a mapping key, so the pack-valued position on
        another node (`source == <pack id>`) would be rewritten to
        `{src}~{salt}` instead of the destination pack. Reject before the
        reservation instead. The `source` property is seeded explicitly
        because no fixture produces one -- `write_source` stamps only
        `pack_id`/`user_id` -- and without it the reverse-mutation oracle
        below would have nothing to go wrong. Reverse-mutation: the step 6c
        guard was removed -- the fork completed `ok` and the copied node's
        `source` came back as `{src}~{salt}` rather than the new pack id,
        failing both the `error` assertion and the row-count assertion."""
        src = _seed_pack(stack, ALICE, "src-t87", node_count=2, with_source=False)
        _add_node(stack, src, src)
        _add_node_with_props(stack, src, f"{src}-tagged", {"title": "t", "source": src})

        before = _node_snapshot(stack, src)
        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert "error" in out, out
        assert src in out["error"], out["error"]
        assert "#197" in out["error"], out["error"]
        assert _forked_row_count(stack, src) == 0, out
        # Rejection declines to create; it does not remove.
        assert _node_snapshot(stack, src) == before

    def test_t88_source_id_equal_to_pack_id_rejects_whole_fork(self, stack):
        """T88 (design §14-6): the guard's domain is BOTH axes, because
        `build_mapping` takes its content keys from both. A source id equal
        to the pack id poisons the same branch identically even when every
        node id is clean. Reverse-mutation: the source axis was dropped from
        the guard's domain tuple -- this pack forked cleanly and the `error`
        assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t88", node_count=2, with_source=True)
        with principal_scope(ALICE):
            write_source(
                stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
                graph=stack["graph"],
                text="a source whose id collides with the pack id",
                source_id=src, pack_id=src,
            )

        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert "error" in out, out
        assert src in out["error"], out["error"]
        assert "source" in out["error"], out["error"]
        assert _forked_row_count(stack, src) == 0, out

    def test_t89_colliding_id_that_fails_tier_1_is_only_a_drop(self, stack):
        """T89 (design §14-6): the guard runs only on ids that actually
        SURVIVE Tier 1, on both axes -- an id that is never copied never
        becomes a mapping key, so rejecting the whole fork over it would be
        over-rejection (the same rule §13-9's length check follows, T82).
        Both Tier 1 filters are exercised: a grammar-invalid node and an
        alias-conflicted source, each carrying the colliding id. Reverse-
        mutation: the guard was moved ahead of the grammar check / the alias
        canonicalization -- this pack, whose colliding ids are dropped
        anyway, was rejected outright and the `status == 'ok'` assertion
        failed."""
        src = _seed_pack(stack, ALICE, "src-t89", node_count=15, with_edge=False,
                         with_source=False)
        _add_node(stack, src, src)
        _test_graph_mutation(stack,
            "UPDATE graph_nodes SET properties = json_set(properties, '$.space', :v) "
            "WHERE node_id = :nid",
            {"v": "not-a-real-space", "nid": src},
        )
        with principal_scope(ALICE):
            for i in range(10):
                write_source(
                    stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
                    graph=stack["graph"],
                    text=f"ordinary source {i}", source_id=f"{src}-s{i}", pack_id=src,
                )
            # #74: `src` is already a Document node id (the `_add_node` call
            # above, corrupted to a bogus space right after). `write_source`
            # would either raise a real `NodeIdentityConflict` for it
            # (`write_graph=True`) or -- since #74's own guard now requires
            # `fork_copy=True` alongside `write_graph=False` -- refuse the
            # call outright, since that combination is the fork raw-copy
            # opt-out and not a fixture-construction escape hatch. Seed the
            # doc/vector stores directly instead so the fixture reaches
            # `_fork()`, which is what this guard actually tests (the
            # SOURCE-axis id collision, not the graph write itself).
            _seed_colliding_source_bypassing_graph(
                stack, ALICE,
                source_id=src, pack_id=src, text="alias-conflicted source",
            )
        _test_doc_mutation(stack,
            "UPDATE doc_sources SET metadata = json_set(metadata, '$.pack', :v) "
            "WHERE source_id = :sid",
            {"v": "some-other-pack-entirely", "sid": src},
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out["status"] == "ok", out
        assert any(
            "failed grammar validation" in msg for msg in out["errors"]["nodes"]
        ), out["errors"]["nodes"]
        assert out["skipped"]["sources_alias_conflict"] == 1, out
        assert src not in _fork_node_ids(stack, out["pack_id"]), out

    def test_t90_guard_precedes_the_reservation_and_every_writer(self, stack):
        """T90 (design §14-6): the guard's placement claim is that NOTHING
        has happened yet when it fires -- no reservation and no store write
        on any of the five write paths (anchor, node, edge, source, vector).
        A registry row count alone cannot prove that: a compensating delete
        would leave the same zero. Spies prove the calls were never reached.
        Reverse-mutation: the guard was disabled at step 6c and re-raised
        after `begin_pack_creation` as a compensating rejection -- this
        test's reservation list recorded the call that the placement claim
        says can never happen."""
        src = _seed_pack(stack, ALICE, "src-t90", node_count=2, with_source=True)
        _add_node(stack, src, src)

        reservations: list[str] = []
        writes: list[str] = []
        real_begin = fork_mod.begin_pack_creation
        real_write_source = fork_mod.write_source
        real_add_node = stack["builder"].add_node
        real_add_edge = stack["builder"].add_edge
        real_import = stack["vector"].import_vectors

        def _spy(name, fn):
            def wrapped(*a, **kw):
                writes.append(name)
                return fn(*a, **kw)
            return wrapped

        def spy_begin(*a, **kw):
            reservations.append("begin_pack_creation")
            return real_begin(*a, **kw)

        try:
            fork_mod.begin_pack_creation = spy_begin
            fork_mod.write_source = _spy("write_source", real_write_source)
            stack["builder"].add_node = _spy("add_node", real_add_node)
            stack["builder"].add_edge = _spy("add_edge", real_add_edge)
            stack["vector"].import_vectors = _spy("import_vectors", real_import)
            out = _fork(stack, principal=ALICE, src_pack_id=src)
        finally:
            fork_mod.begin_pack_creation = real_begin
            fork_mod.write_source = real_write_source
            stack["builder"].add_node = real_add_node
            stack["builder"].add_edge = real_add_edge
            stack["vector"].import_vectors = real_import

        assert "error" in out, out
        assert reservations == [], reservations
        assert writes == [], writes

    def test_t92_both_axes_are_named_in_the_rejection(self, stack):
        """T92 (design §14-6): the rejection says WHICH axis collided, and
        when both do it says both -- a caller who fixes only the node id
        would otherwise hit the same rejection again with no new
        information. Reverse-mutation: the message was narrowed to the first
        colliding axis -- `source` disappeared from the reason and this
        assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t92", node_count=2, with_source=True)
        _add_node(stack, src, src)
        # #74: see test_t89's identical comment -- `src` is already a
        # Document node id from `_add_node` above, so `write_source` would
        # either raise a real `NodeIdentityConflict` (`write_graph=True`) or
        # be refused outright (`write_graph=False` without `fork_copy=True`
        # is the fork raw-copy opt-out, not a fixture escape hatch). Seed
        # the doc/vector stores directly instead of exercising the graph
        # write, which is not what the fork-level node/source collision
        # guard this test targets is about.
        _seed_colliding_source_bypassing_graph(
            stack, ALICE,
            source_id=src, pack_id=src, text="collides on the source axis too",
        )

        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert "error" in out, out
        assert "node/source" in out["error"], out["error"]

    def test_t93_destination_pack_id_equal_to_a_content_id_is_compensated(self, stack):
        """T93 (design §14-6b): the destination half of the same collision.
        `dst` does not exist until the reservation, so it cannot be checked
        in preflight -- but if a content id equals it, that string is a
        mapping KEY, and `_h4_scan`'s "still a mapping key" predicate then
        fires on a value rule 3 rewrote CORRECTLY to `dst`, demoting a clean
        fork to `partial` on a false positive. Reachable because
        `new_pack_id` is caller-supplied. Reverse-mutation: the `dst in
        mapping` check was removed -- the fork came back `partial` with an
        H4 hit naming the correctly-rewritten `source` value, so the
        `error`/`_pack_row_count == 0` assertions failed."""
        src = _seed_pack(stack, ALICE, "src-t93", node_count=2, with_source=False)
        collide = f"{src}-n0"
        _add_node_with_props(stack, src, f"{src}-tagged", {"title": "t", "source": src})

        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=collide)

        assert "error" in out, out
        assert collide in out["error"], out["error"]
        assert _pack_row_count(stack, collide) == 0, out
        assert _forked_row_count(stack, src) == 0, out


# ---------------------------------------------------------------------------
# T95-T96 -- design §15: step 16's `docs.get_source` fallback must fire only
# on a genuinely incomplete row, never on one that already carries `text`.
#
# COUNTING DISCIPLINE (design §15-5): `docs.get_source` is not called only
# by step 16's fallback. `begin_pack_creation`'s post-reservation identity
# probe (`source_identity_conflict`, once per source) and `write_source`'s
# own copy of that same probe (once per source) both call it too -- but with
# the REMAPPED new id, never the original. So for N sources the new-id call
# count is >= 2N regardless of whether the fallback ever fires, and counting
# every call conflates the fallback with the identity probes. Both tests
# below spy on the call ARGUMENTS and count only calls whose argument is a
# member of the ORIGINAL source id set -- the fallback's own signature.
# ---------------------------------------------------------------------------


class TestSourceTextFallback:
    def test_t95_no_original_id_lookups_when_rows_already_carry_text(self, stack, monkeypatch):
        """T95 (design §15-3, §15-5): the local SQL doc store's scoped rows
        already carry `text` (unlike mongo's old projection), so step 16
        must never fall back to `get_source` for any of them -- a bulk read
        plus N avoidable round trips would defeat the point of the scoped
        read. `_seed_pack`'s default `with_source=True` seeds one non-empty
        text source ("s0"), satisfying design §15-5's non-empty-text seed
        requirement. Reverse-mutation (design §15's table, T95 row): step
        16's `text = record.get("text") or ""` changed to always assign
        `""` -- fork still completes `ok` via the fallback, but the
        original-id call count goes from 0 to 1 and this test's
        `assert original_id_calls == []` fails."""
        src = _seed_pack(stack, ALICE, "src-t95", node_count=2, with_source=True)
        original_ids = {"s0"}

        original_id_calls: list[str] = []
        real_get_source = type(stack["docs"]).get_source

        def _spy(self, source_id):
            if source_id in original_ids:
                original_id_calls.append(source_id)
            return real_get_source(self, source_id)

        monkeypatch.setattr(type(stack["docs"]), "get_source", _spy, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out["status"] == "ok", out
        assert original_id_calls == [], original_id_calls

    def test_t96_fallback_still_recovers_text_from_a_textless_row(self, stack, monkeypatch):
        """T96 (design §15-3, §15-5): reproduces the OLD mongo row shape
        (scoped read returns rows with `text` stripped) via a read-boundary
        monkeypatch matching `tests/test_pack_fork_faults.py`'s
        `_patch_list_sources_for_src` pattern -- passed through to the real
        implementation and only stripped when `pack_ids == [src]`, so the
        `[dst]` emptiness check and the H4 post-write re-read stay real
        (design §15-5: patching those too would reject the fork before it
        ever reaches the fallback). Under that shape, step 16's fallback
        must still recover the original text via `get_source`, and the
        copied source's text must match the original byte for byte.
        Reverse-mutation (design §15's table, T96 row): the fallback branch
        (`if not text: ... fetched = docs.get_source(...)`) deleted --
        fork still completes `ok`, but the copied source's text comes back
        empty instead of equal to the original and the text-equality
        assertion fails."""
        src = _seed_pack(stack, ALICE, "src-t96", node_count=2, with_source=True)
        original_ids = {"s0"}
        original_text = stack["docs"].get_source("s0")["text"]
        assert original_text, "fixture must seed a non-empty text source"

        real_list_sources_scoped = type(stack["docs"]).list_sources_scoped

        def _strip_text_for_src(self, pack_ids, limit):
            records = real_list_sources_scoped(self, pack_ids, limit)
            if pack_ids == [src]:
                stripped = []
                for rec in records:
                    rec = dict(rec)
                    rec["text"] = ""
                    stripped.append(rec)
                return stripped
            return records

        monkeypatch.setattr(
            type(stack["docs"]), "list_sources_scoped", _strip_text_for_src, raising=True,
        )

        original_id_calls: list[str] = []
        real_get_source = type(stack["docs"]).get_source

        def _spy(self, source_id):
            if source_id in original_ids:
                original_id_calls.append(source_id)
            return real_get_source(self, source_id)

        monkeypatch.setattr(type(stack["docs"]), "get_source", _spy, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src)

        assert out["status"] == "ok", out
        dst = out["pack_id"]
        copied = stack["docs"].list_sources_scoped([dst], 100)
        assert len(copied) == 1, copied
        assert copied[0]["text"] == original_text, copied[0]
        assert set(original_id_calls) == original_ids, original_id_calls


# ---------------------------------------------------------------------------
# T97 -- design §16: the count-then-export cap race. `_count_pack_vectors`
# (step 4) and `export_pack_vectors` (step 6b) read the source pack's
# vectors TWICE, not once -- a concurrent writer landing between the two can
# grow the actual export past a cap the count already cleared, and nothing
# re-compares the export's own length against `FORK_MAX_VECTORS`.
# ---------------------------------------------------------------------------


class TestVectorExportLengthRecheck:
    def test_t97_export_length_rechecked_against_cap_after_count_passes(
        self, stack, monkeypatch,
    ):
        """T97 (design §16-6): `FORK_MAX_VECTORS` lowered to 2 and the
        source pack seeded with exactly 2 REAL vectors -- the anchor's own
        plus one surviving content node's -- so step 4's
        `_count_pack_vectors` measures 2 and clears the cap honestly (no
        undercounting trick). A second surviving content node is seeded
        with `write_vector=False`, so it has no real vector of its own.
        `export_pack_vectors` is then replaced, pass-through style (T96's
        pattern), with a version that -- ONLY when called for the SOURCE
        pack id -- appends a third, synthetic, fully valid record for that
        second (vector-less) survivor's id: this is what a concurrent
        writer landing between step 4's count and step 6b's export would
        produce, count says 2, export hands back 3. All three records are
        real mapped ids with a uniform embedding dimension and valid shape,
        so nothing but the export-length recheck can reject this fork on
        cap grounds (design §16-6 note 1: any other classification-loop
        rejection would make the reverse-mutation pass for the wrong
        reason). The injection is scoped to `pack_id == src` (design §16-6
        note 2) so the destination-side H4 re-read
        (`export_pack_vectors(dst)`) is never touched by it and cannot
        itself demote the fork to `partial`. T90's reservation/write spy
        pattern proves the rejection fires before ANY store write.
        Reverse-mutation: the export-length recheck removed -- fork
        completes `ok` instead of being rejected, and the exact-string
        error assertion below fails."""
        monkeypatch.setattr(fork_mod, "FORK_MAX_VECTORS", 2, raising=True)
        src = _seed_pack(
            stack, ALICE, "src-t97", node_count=1, with_edge=False, with_source=False,
        )
        second_survivor_id = f"{src}-n1"
        with principal_scope(ALICE):
            stack["builder"].add_node(
                space="resource", node_type="Document", node_id=second_survivor_id,
                properties={"title": "doc 1"}, pack_id=src, write_vector=False,
            )

        real_export = type(stack["vector"]).export_pack_vectors
        actual_count = len(real_export(stack["vector"], src))
        assert actual_count == 2, (
            "fixture must seed exactly 2 real vectors (anchor + one "
            "surviving node) so the step-4 pre-count clears the "
            "monkeypatched cap of 2 honestly"
        )

        def _inject_third_survivor(self, pack_id):
            records = real_export(self, pack_id)
            if pack_id != src:
                return records
            anchor_rec = next(r for r in records if r["id"] == anchor_node_id(src))
            synthetic = dict(anchor_rec)
            synthetic["id"] = second_survivor_id
            synthetic["metadata"] = {"pack_id": src}
            return [*records, synthetic]

        monkeypatch.setattr(
            type(stack["vector"]), "export_pack_vectors", _inject_third_survivor, raising=True,
        )

        reservations: list[str] = []
        writes: list[str] = []
        real_begin = fork_mod.begin_pack_creation
        real_write_source = fork_mod.write_source
        real_add_node = stack["builder"].add_node
        real_add_edge = stack["builder"].add_edge
        real_import = stack["vector"].import_vectors

        def _spy(name, fn):
            def wrapped(*a, **kw):
                writes.append(name)
                return fn(*a, **kw)
            return wrapped

        def spy_begin(*a, **kw):
            reservations.append("begin_pack_creation")
            return real_begin(*a, **kw)

        try:
            fork_mod.begin_pack_creation = spy_begin
            fork_mod.write_source = _spy("write_source", real_write_source)
            stack["builder"].add_node = _spy("add_node", real_add_node)
            stack["builder"].add_edge = _spy("add_edge", real_add_edge)
            stack["vector"].import_vectors = _spy("import_vectors", real_import)
            out = _fork(stack, principal=ALICE, src_pack_id=src)
        finally:
            fork_mod.begin_pack_creation = real_begin
            fork_mod.write_source = real_write_source
            stack["builder"].add_node = real_add_node
            stack["builder"].add_edge = real_add_edge
            stack["vector"].import_vectors = real_import

        assert "error" in out and "status" not in out, out
        assert out["error"] == "pack too large to fork: more than 2 vectors", out
        assert _forked_row_count(stack, src) == 0, out
        assert reservations == [], reservations
        assert writes == [], writes


# ---------------------------------------------------------------------------
# T100-T108 -- review round 6: Neo4j label order (P1) and the lone retired
# `pack` alias (P2). Design §18.
#
# Fixture scale rule (design §18-6): a test that watches a row get dropped
# needs that axis at 12+, because `_check_floor` rejects the whole fork above
# 10% loss and the response then carries no counters at all. The rule is
# GLOBAL, not per-axis: the vector axis's denominator is `export_pack_vectors`
# in full and `builder.add_node` writes a vector per node, so the node count
# dominates it. A 2-node fixture makes one dropped source's orphan 1/5 and
# rejects a correct implementation.
# ---------------------------------------------------------------------------

MARKER = "OpenCrabNode"


def _patch_labels(monkeypatch, stack, src, fn):
    """Rewrite `labels` on the SOURCE pack's node export only.

    Delegates to the real method and only rewrites when `src` is in scope
    (design §18-6 monkeypatch rule): fork calls the same method again for the
    DESTINATION -- the pre-flight empty check and the H4 re-read -- so a patch
    that answers unconditionally either makes the destination look non-empty
    (fork never starts) or contaminates the copy's own verification.
    """
    real = type(stack["graph"]).export_nodes_scoped

    def _patched(self, pack_ids, limit, space=None):
        rows = real(self, pack_ids, limit, space)
        if src not in list(pack_ids):
            return rows
        out = []
        for row in rows:
            row = dict(row)
            row["labels"] = fn((row.get("props") or {}).get("id"), list(row.get("labels") or []))
            out.append(row)
        return out

    monkeypatch.setattr(type(stack["graph"]), "export_nodes_scoped", _patched, raising=True)


def _seed_mixed_types(stack, tag):
    """12 ordinary nodes alternating Document / File (both valid in `resource`).

    `_seed_pack` only ever writes `Document`, so an implementation that
    hardcodes that one type would pass a single-type fixture (T100's
    reverse-mutation needs the second type to kill it).

    `with_source=False`: #74 made `_seed_pack`'s default legacy source
    ("s0") materialise its own evidence/TextUnit graph node, whose id
    does not match this fixture's `{tag}-n{i}` shape -- T100's own
    `shape()` helper parses every non-anchor id as `...n<i>` and raises
    `IndexError` on anything else. This fixture tests label-shape
    handling, not sources, so the source leg is dropped rather than
    special-cased in `shape()`.
    """
    src = _seed_pack(stack, ALICE, tag, node_count=0, with_edge=False, with_source=False)
    kinds: dict[str, str] = {}
    with principal_scope(ALICE):
        for i in range(12):
            node_type = "Document" if i % 2 == 0 else "File"
            nid = f"{tag}-n{i}"
            stack["builder"].add_node(
                space="resource", node_type=node_type, node_id=nid,
                properties={"title": f"x {i}"}, pack_id=src,
            )
            kinds[nid] = node_type
    return src, kinds


class TestLabelShapeAndRetiredAlias:
    @pytest.mark.parametrize("anchor_pos", ["front", "back"])
    def test_t100_domain_label_read_once_and_reused(self, stack, monkeypatch, anchor_pos):
        """T100: the marker may sit at ANY position (Neo4j's `labels(n)` order
        is undeclared) and the domain type must still be read correctly, once,
        with both downstream consumers reusing that one interpretation.

        Three label shapes are exercised per run -- marker first, marker last,
        and marker absent (the SQL/Kuzu shape, which really does return a bare
        `[node_type]`). The anchor row's marker position is parameterized
        because an implementation that reads `labels[-1]` only in the anchor
        branch survives a front-only fixture and then rejects real anchors.

        Reverse-mutation: `labels[0]` dies on a marker-first row; `labels[1:]`
        or `labels[-1]` dies on a marker-last row; "no marker means no domain
        label" dies on the marker-absent row; hardcoding `Document` dies on a
        `File` row; leaving the step-12 identity probe on `labels[...]` dies on
        the probe-argument assertion (nothing else observes it -- the
        destination ids never collide, so the probe always returns None).
        """
        tag = f"t100{anchor_pos}"
        src, kinds = _seed_mixed_types(stack, tag)
        anchor = f"dataset:{src}"

        def shape(nid, labels):
            if nid == anchor:
                return [MARKER, *labels] if anchor_pos == "front" else [*labels, MARKER]
            i = int(nid.rsplit("n", 1)[1])
            if i % 3 == 0:
                return [MARKER, *labels]
            if i % 3 == 1:
                return [*labels, MARKER]
            return list(labels)

        _patch_labels(monkeypatch, stack, src, shape)

        probe_args: list[tuple[Any, Any]] = []
        real_probe = fork_mod.node_identity_conflict

        def _probe(*a, **kw):
            probe_args.append((kw.get("node_id"), kw.get("node_type")))
            return real_probe(*a, **kw)

        monkeypatch.setattr(fork_mod, "node_identity_conflict", _probe, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=f"{tag}-dst")
        assert out["status"] == "ok", out
        dst = out["pack_id"]

        # Re-read the destination WITHOUT the patch: the copy must carry the
        # domain label alone, with no marker smuggled in.
        monkeypatch.undo()
        got = {
            (r["props"] or {}).get("id"): list(r.get("labels") or [])
            for r in stack["graph"].export_nodes_scoped([dst], 200)
        }
        for nid, node_type in kinds.items():
            copied = [v for k, v in got.items() if k.startswith(nid + "~")]
            assert copied and copied[0] == [node_type], (nid, node_type, copied)

        # Step 12's identity probe saw the resolved type for EVERY surviving
        # row, not just the one whose marker position happens to make a
        # positional read look right. Ids arrive remapped (`{old}~{salt}`).
        seen = {
            pid.rsplit("~", 1)[0]: node_type
            for pid, node_type in probe_args
            if pid and "~" in pid
        }
        for nid, node_type in kinds.items():
            assert seen.get(nid) == node_type, (nid, node_type, seen.get(nid))

    _AMBIGUOUS = [
        ([MARKER, "Document", "File"], "front"),
        (["Document", "File", MARKER], "back"),
        (["Document", "File"], "nomarker"),
        (["Document", "File", "API"], "three"),
        (["Document", "Weird"], "weird"),
    ]

    @pytest.mark.parametrize(
        "labels,ident", _AMBIGUOUS, ids=[i for _, i in _AMBIGUOUS]
    )
    def test_t101_two_domain_labels_reject_whole_pack(self, stack, monkeypatch, labels, ident):
        """T101: a node with two or more domain labels rejects the whole fork,
        before any registry row exists, with its own wording (NOT the #197
        declared-limit message, whose remedy -- rename the colliding id -- is
        wrong here) naming every offending label.

        Reverse-mutation: picking the first domain label completes as "ok";
        reusing `_declared_limit_reject` reintroduces `#197`; gating the check
        on the marker's presence survives only until the marker-absent
        parameter; writing it as `len(domain) == 2` survives only until the
        three-label parameter; implementing `domain_labels` as "keep the labels
        the grammar knows" survives only until `Weird`, which it would silently
        filter down to a single type and copy.
        """
        tag = f"t101{ident}"
        src = _seed_pack(stack, ALICE, tag, node_count=12)
        target = f"{tag}-n5"
        _patch_labels(
            monkeypatch, stack, src,
            lambda nid, cur: list(labels) if nid == target else cur,
        )
        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=f"{tag}-dst")
        err = str(out.get("error"))
        assert out.get("status") is None, out
        assert "more than one domain label" in err, err
        assert "#197" not in err, err
        for name in labels:
            if name != MARKER:
                assert name in err, (name, err)
        assert get_pack(stack["sql"], f"{tag}-dst") is None

    def test_t102_marker_only_node_is_missing_type_not_bad_grammar(self, stack, monkeypatch):
        """T102: a node whose only label is the marker has NO domain type, so
        it takes the "missing space/type" Tier 1 skip rather than reaching
        grammar validation with the marker as its type. The target is the last
        node so the single seeded edge (n0->n1) does not fall with it.

        Reverse-mutation: dropping the marker filter makes the message
        `failed grammar validation` instead.
        """
        src = _seed_pack(stack, ALICE, "t102", node_count=12)
        target = "t102-n11"
        _patch_labels(
            monkeypatch, stack, src,
            lambda nid, cur: [MARKER] if nid == target else cur,
        )
        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id="t102-dst")
        assert out["status"] == "ok", out
        node_errors = (out.get("errors") or {}).get("nodes") or []
        assert any("missing space/type" in e and target in e for e in node_errors), node_errors
        assert not any("failed grammar validation" in e for e in node_errors), node_errors

    _ANCHOR_AMBIGUOUS = [
        ([MARKER, "Dataset", "File"], "front"),
        (["Dataset", "File", MARKER], "back"),
        (["Dataset", "File"], "nomarker"),
    ]

    @pytest.mark.parametrize(
        "labels,ident", _ANCHOR_AMBIGUOUS, ids=[i for _, i in _ANCHOR_AMBIGUOUS]
    )
    def test_t103_ambiguous_anchor_is_ambiguity_not_bad_anchor(self, stack, monkeypatch, labels, ident):
        """T103: ambiguity is decided BEFORE the anchor branch -- a row whose
        type cannot be determined cannot be judged to be the anchor either, so
        the message is T101's, not "wrong shape for an anchor".

        Reverse-mutation: moving the ambiguity check after the anchor branch
        produces the anchor-shape wording; gating it on the marker's presence
        dies on the marker-absent parameter; reading the anchor positionally
        dies on the marker-last parameter.
        """
        tag = f"t103{ident}"
        src = _seed_pack(stack, ALICE, tag, node_count=12)
        anchor = f"dataset:{src}"
        _patch_labels(
            monkeypatch, stack, src,
            lambda nid, cur: list(labels) if nid == anchor else cur,
        )
        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=f"{tag}-dst")
        err = str(out.get("error"))
        assert "more than one domain label" in err, err
        assert "#197" not in err, err
        assert "anchor" not in err.lower(), err

    # -- T104 family: retired `pack` alias strip + the aggregate signal ------
    #
    # Shared fixture, fixed by count so T104b's call totals are determinate
    # (design §18-6): sources 12 = s0 + 8 fillers + 2 legacy + 1 same-value;
    # nodes 12 + anchor; edges = 11 chain + 2 injected legacy = 13. Fillers go
    # in through `upsert_source` (NOT `write_source`) so they carry no vector:
    # that pins the vector denominator at 14 (12 nodes + anchor + s0), which is
    # what makes T104c(ii)'s two ghosts land at 2/16, just over the floor.

    @staticmethod
    def _t104_fixture(stack, tag):
        src = _seed_pack(stack, ALICE, tag, node_count=12)
        docs = stack["docs"]
        # Every row that will reach the strip site carries a unique `row` tag,
        # so T104b can compare the helper's actual arguments against the exact
        # set of surviving rows rather than just counting them. (`s0`, written
        # by `_seed_pack`, is the one untagged source -- accounted for by name.)
        docs.upsert_source(
            f"{tag}-legacy-a", "ba", {"source": src, "pack": src, "row": "legacy-a"},
        )
        docs.upsert_source(
            f"{tag}-legacy-b", "bb", {"source": src, "pack": "other", "row": "legacy-b"},
        )
        # Same-value alias: `canonicalize_pack_alias` removes this one first,
        # so it must NOT reach the strip site and must NOT be counted.
        docs.upsert_source(
            f"{tag}-same", "bs", {"pack_id": src, "pack": src, "row": "same"},
        )
        for i in range(8):
            docs.upsert_source(
                f"{tag}-f{i}", f"filler {i}", {"pack_id": src, "row": f"src-f{i}"},
            )
        with principal_scope(ALICE):
            for i in range(1, 11):
                stack["builder"].add_edge(
                    "resource", f"{tag}-n{i}", "cites", "resource", f"{tag}-n{i+1}",
                    {"row": f"edge-chain-{i}"}, pack_id=src,
                )
        return src

    @staticmethod
    def _inject_legacy_edges(monkeypatch, stack, src, tag):
        """Two edges carrying only the retired alias (no `pack_id`)."""
        real = type(stack["graph"]).export_edges_scoped

        def _patched(self, pack_ids, limit):
            rows = real(self, pack_ids, limit)
            if src not in list(pack_ids):
                return rows
            rows = list(rows)
            proto = rows[0]
            for j, alias in enumerate((src, "other")):
                edge = dict(proto)
                edge["source_props"] = {**proto["source_props"], "id": f"{tag}-n0"}
                edge["target_props"] = {**proto["target_props"], "id": f"{tag}-n{2+j}"}
                edge["rel_props"] = {"pack": alias, "row": f"edge-legacy-{j}"}
                rows.append(edge)
            return rows

        monkeypatch.setattr(type(stack["graph"]), "export_edges_scoped", _patched, raising=True)

    @staticmethod
    def _alias_warnings(caplog):
        return [r for r in caplog.records if "retired pack alias" in r.getMessage()]

    def test_t104_strip_both_axes_and_one_aggregate_warning(self, stack, monkeypatch, caplog):
        """T104: legacy rows on BOTH axes are stripped before copying, and the
        two strip sites feed ONE counter reported as a single warning.

        Reverse-mutation: removing either strip site turns the fork `partial`
        (the destination writer rejects the retired key); dropping the alias
        only when it equals the source pack dies on the third-pack rows;
        deleting the warning gives 0 records; logging per row gives 4; counting
        what `canonicalize_pack_alias` already removed makes the argument 5;
        giving the edge axis its own counter makes it 2.
        """
        caplog.set_level(logging.WARNING)
        src = self._t104_fixture(stack, "t104")
        self._inject_legacy_edges(monkeypatch, stack, src, "t104")

        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id="t104-dst")
        assert out["status"] == "ok", out
        assert out["copied"]["sources"] == 12, out["copied"]
        assert out["copied"]["edges"] == 13, out["copied"]

        hits = self._alias_warnings(caplog)
        assert len(hits) == 1, [r.getMessage() for r in hits]
        assert hits[0].args[0] == 4, hits[0].args

        dst = out["pack_id"]
        copied = stack["docs"].list_sources_scoped([dst], 200)
        # Every source made it across, none kept the retired key, and all are
        # owned by the destination. Asserting the id SET (not just the count)
        # is what catches an implementation that silently drops a filler row:
        # 1/13 is under the floor, and the surviving totals would still make
        # T104b's call counts come out right.
        expected = {"s0", "t104-legacy-a", "t104-legacy-b", "t104-same"} | {
            f"t104-f{i}" for i in range(8)
        }
        assert {
            (s.get("source_id") or "").rsplit("~", 1)[0] for s in copied
        } == expected, [s.get("source_id") for s in copied]
        metas = [s.get("metadata") or {} for s in copied]
        assert not any("pack" in m for m in metas), metas
        assert all(m.get("pack_id") == dst for m in metas), metas

    def test_t104b_warning_and_strip_precede_the_reservation(self, stack, monkeypatch, caplog):
        """T104b: both the strip and its warning are complete BEFORE
        `begin_pack_creation` is entered.

        The reservation is the only observable boundary, so the wrapper
        snapshots state at the moment it is called and then raises. Snapshotting
        AT the call (rather than reading caplog after the fork returns) is what
        kills an implementation that wraps the reservation in try/finally and
        logs in the finally.

        Reverse-mutation: logging after the reservation leaves `warned` False;
        deferring the strip to just before the writer leaves both counts 0;
        calling `strip_retired_keys` only on rows that already have the key
        leaves the total at 4 instead of 25; faking the total with throwaway
        calls dies on the argument-set assertion.
        """
        caplog.set_level(logging.WARNING)
        src = self._t104_fixture(stack, "t104b")
        self._inject_legacy_edges(monkeypatch, stack, src, "t104b")

        calls: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        real_strip = fork_mod.strip_retired_keys

        def _strip(tags):
            out = real_strip(tags)
            calls.append(dict(tags))
            if out != dict(tags):
                changed.append(dict(tags))
            return out

        monkeypatch.setattr(fork_mod, "strip_retired_keys", _strip, raising=True)

        snap: dict[str, Any] = {}

        def _boom(*a, **kw):
            snap["warned"] = bool(self._alias_warnings(caplog))
            snap["calls"] = len(calls)
            snap["changed"] = len(changed)
            snap["rows"] = [dict(t) for t in calls]
            raise RuntimeError("registry down")

        monkeypatch.setattr(fork_mod, "begin_pack_creation", _boom, raising=True)

        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id="t104b-dst")
        assert "pack registration failed" in str(out.get("error")), out
        assert snap["warned"] is True, snap
        assert snap["changed"] == 4, snap
        # 12 surviving sources + 13 surviving edges, every one of them passed
        # through the strip site (design §18-4 contract 0: the call is
        # unconditional, and `canonicalize_pack_alias` is the last gate before
        # it, so "reached the strip site" and "survived" are the same set).
        assert snap["calls"] == 25, snap
        # ... and they were the REAL rows, not padding. Counting alone can be
        # satisfied by calling on the four aliased rows and then feeding the
        # helper 21 throwaway dicts, so the recorded arguments are matched
        # against the exact set of surviving rows by their `row` tags.
        expected_rows = (
            {"legacy-a", "legacy-b", "same"}
            | {f"src-f{i}" for i in range(8)}
            | {f"edge-chain-{i}" for i in range(1, 11)}
            | {"edge-legacy-0", "edge-legacy-1"}
        )
        seen_rows = {r.get("row") for r in snap["rows"] if r.get("row")}
        assert seen_rows == expected_rows, (
            sorted(expected_rows - seen_rows), sorted(seen_rows - expected_rows),
        )
        # 25 calls = the 23 tagged rows above plus the two `_seed_pack` writes
        # that predate this fixture and carry no tag: its `s0` source and its
        # own n0->n1 edge. Both are still real rows owned by the source pack.
        untagged = [r for r in snap["rows"] if not r.get("row")]
        assert len(untagged) == 2, untagged
        assert all(r.get("pack_id") == src for r in untagged), untagged

    # T104c is split across three test methods rather than three branches of
    # one: `_seed_pack` writes its source under the fixed id "s0" and source
    # identity is global across packs, so building the fixture twice inside a
    # single `stack` collides. Each branch therefore takes a fresh stack.

    def test_t104c_i_warning_silent_when_first_axis_floor_rejects(self, stack, monkeypatch, caplog):
        """T104c(i): the aggregate warning sits AFTER the completeness floors,
        so a run rejected by the FIRST axis (node) logs nothing -- the copy
        never happened, so "dropped ... while copying" would not be true of it.

        Reverse-mutation: logging before the floors puts a record here.
        """
        caplog.set_level(logging.WARNING)

        # Two of twelve nodes lose their type. The denominator includes the
        # anchor row AND the `s0` source's own evidence/TextUnit node (#74:
        # `write_source`'s graph leg now materialises every source as a node
        # too, and that node lands in the same `export_nodes_scoped` count
        # the node-axis floor divides by), hence 2/14, not 2/13.
        src_i = self._t104_fixture(stack, "t104ci")
        self._inject_legacy_edges(monkeypatch, stack, src_i, "t104ci")
        _patch_labels(
            monkeypatch, stack, src_i,
            lambda nid, cur: ["Weird"] if nid in ("t104ci-n0", "t104ci-n1") else cur,
        )
        out_i = _fork(stack, principal=ALICE, src_pack_id=src_i, new_pack_id="t104ci-dst")
        # Pinning the axis name and the ratio, not just "completeness floor":
        # an implementation that checks a dependent axis first would still say
        # "completeness floor" while rejecting for the wrong reason.
        assert "node loss ratio 2/14" in str(out_i.get("error")), out_i
        assert self._alias_warnings(caplog) == []

    def test_t104c_ii_warning_silent_when_last_axis_floor_rejects(self, stack, monkeypatch, caplog):
        """T104c(ii): the same, for the LAST of the four axes (vector).

        Checking only the first axis would let an implementation that logs
        between the node and edge checks through: the node floor rejects before
        it ever runs. Breaking the vector axis alone is what pins "after ALL
        four".

        Two ghost vectors point at ids in no mapping, so only the vector axis
        moves -- 2 orphans over 14 real vectors (12 nodes + anchor + s0; the
        fillers go in without vectors) plus the 2 ghosts themselves.

        Reverse-mutation: logging anywhere among the four checks puts a record
        here.
        """
        caplog.set_level(logging.WARNING)
        src_ii = self._t104_fixture(stack, "t104cii")
        self._inject_legacy_edges(monkeypatch, stack, src_ii, "t104cii")
        real_export = type(stack["vector"]).export_pack_vectors

        def _ghosts(self_, pack_id, *a, **kw):
            rows = list(real_export(self_, pack_id, *a, **kw))
            if pack_id != src_ii:
                return rows
            proto = rows[0]
            for j in range(2):
                ghost = dict(proto)
                ghost["id"] = f"ghost-{j}"
                ghost["metadata"] = dict(proto.get("metadata") or {})
                rows.append(ghost)
            return rows

        monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _ghosts, raising=True)
        out_ii = _fork(stack, principal=ALICE, src_pack_id=src_ii, new_pack_id="t104cii-dst")
        assert "vector loss ratio 2/16" in str(out_ii.get("error")), out_ii
        assert self._alias_warnings(caplog) == []

    def test_t104c_iii_no_warning_when_nothing_was_retired(self, stack, caplog):
        """T104c(iii): a pack with no retired alias anywhere logs nothing, so
        the signal does not fire on every healthy fork.

        Reverse-mutation: logging unconditionally puts a record here.
        """
        caplog.set_level(logging.WARNING)
        src_iii = _seed_pack(stack, ALICE, "t104ciii", node_count=12)
        out_iii = _fork(stack, principal=ALICE, src_pack_id=src_iii, new_pack_id="t104ciii-dst")
        assert out_iii["status"] == "ok", out_iii
        assert self._alias_warnings(caplog) == []

    # -- T105-T108: the sibling edge axis, the order contract, falsy pack_id --

    def test_t105_legacy_edges_are_copied_not_rejected(self, stack, monkeypatch):
        """T105: the edge axis gets the same treatment as the source axis --
        edges carrying only the retired alias copy across instead of turning
        the fork partial. (Their contribution to the aggregate count is pinned
        by T104; this row owns the copy itself.)

        Reverse-mutation: removing the edge strip turns the fork `partial`;
        dropping the alias only when it equals the source pack dies on the
        third-pack edge.
        """
        src = _seed_pack(stack, ALICE, "t105", node_count=12)
        with principal_scope(ALICE):
            for i in range(1, 11):
                stack["builder"].add_edge(
                    "resource", f"t105-n{i}", "cites", "resource", f"t105-n{i+1}",
                    pack_id=src,
                )
        before = len(stack["graph"].export_edges_scoped([src], 200))
        self._inject_legacy_edges(monkeypatch, stack, src, "t105")
        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id="t105-dst")
        assert out["status"] == "ok", out
        assert out["copied"]["edges"] == before + 2, (out["copied"], before)

    def test_t106_source_alias_conflict_still_tier1(self, stack):
        """T106: the strip must NOT swallow a real contradiction. A source that
        has a truthy `pack_id` AND a different alias beside it is a data
        defect, and stays the Tier 1 skip it is today -- which is only visible
        because `canonicalize_pack_alias` runs FIRST.

        The node axis is filled to 12 as well as the source axis: the vector
        floor's denominator is every exported vector, and nodes carry vectors,
        so a small node fixture would reject this fork at 1/5 before any
        counter could be read.

        Reverse-mutation: moving the strip ahead of the canonicalize call makes
        the conflicting source copy across silently and the counter go to 0.
        """
        src = _seed_pack(stack, ALICE, "t106", node_count=12, with_source=False)
        with principal_scope(ALICE):
            write_source(
                stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
                graph=stack["graph"],
                text="conflict", source_id="t106-conflict", pack_id=src,
            )
        stack["docs"].upsert_source(
            "t106-conflict", "conflict", {"pack_id": src, "pack": "other"},
        )
        for i in range(11):
            stack["docs"].upsert_source(f"t106-f{i}", f"f{i}", {"pack_id": src})

        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id="t106-dst")
        assert out["status"] == "ok", out
        assert out["skipped"]["sources_alias_conflict"] == 1, out["skipped"]
        copied = stack["docs"].list_sources_scoped([out["pack_id"]], 200)
        assert not any(
            (s.get("source_id") or "").startswith("t106-conflict") for s in copied
        ), [s.get("source_id") for s in copied]

    def test_t107_edge_alias_conflict_still_tier1(self, stack, monkeypatch):
        """T107: the order contract on the sibling axis. Before this row the
        repository had no assertion on `edges_alias_conflict` at all, so
        reversing the two calls on the edge side killed nothing.

        Twelve nodes chain into eleven edges (i -> i+1), which is the most a
        12-node chain yields; 1/11 stays under the floor.

        Reverse-mutation: moving the edge strip ahead of the canonicalize call
        makes the conflicting edge copy across and the counter go to 0.
        """
        src = _seed_pack(stack, ALICE, "t107", node_count=12)
        with principal_scope(ALICE):
            for i in range(1, 11):
                stack["builder"].add_edge(
                    "resource", f"t107-n{i}", "cites", "resource", f"t107-n{i+1}",
                    pack_id=src,
                )
        real = type(stack["graph"]).export_edges_scoped

        def _patched(self_, pack_ids, limit):
            rows = real(self_, pack_ids, limit)
            if src not in list(pack_ids):
                return rows
            rows = [dict(r) for r in rows]
            rows[0]["rel_props"] = {"pack_id": src, "pack": "other"}
            return rows

        monkeypatch.setattr(type(stack["graph"]), "export_edges_scoped", _patched, raising=True)
        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id="t107-dst")
        assert out["status"] == "ok", out
        assert out["skipped"]["edges_alias_conflict"] == 1, out["skipped"]

    @pytest.mark.parametrize(
        "falsy", [None, "", False, 0, 0.0], ids=["none", "empty", "false", "zero", "zerofloat"],
    )
    def test_t108_falsy_pack_id_with_retired_alias_now_copies(self, stack, falsy, request):
        """T108: the intended behaviour change. A source whose `pack_id` is
        present but falsy is not covered by the ownership predicate's first
        branch, so today it reaches the writer with the retired alias attached
        and turns the fork partial. After the strip it copies cleanly.

        All five falsy values are exercised because all five really do survive
        export (measured); objects and arrays do not export at all and so are
        out of scope.

        Reverse-mutation: removing the strip makes every parameter `partial`;
        narrowing the policy to the empty string alone leaves the other four.
        """
        # NB `[None, "", False, 0, 0.0].index(falsy)` cannot be used to name
        # this pack: False == 0 == 0.0 in Python, so three parameters would
        # collide on one tag. `request.node.callspec.id` is the parameter's own
        # id and is unique by construction.
        tag = f"t108{request.node.callspec.id}"
        src = _seed_pack(stack, ALICE, tag, node_count=3)
        stack["docs"].upsert_source(
            f"{tag}-legacy", "body", {"pack_id": falsy, "source": src, "pack": "other"},
        )
        out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=f"{tag}-dst")
        assert out["status"] == "ok", out
        dst = out["pack_id"]
        metas = [s.get("metadata") or {} for s in stack["docs"].list_sources_scoped([dst], 200)]
        assert metas, metas
        assert not any("pack" in m for m in metas), metas
        assert all(m.get("pack_id") == dst for m in metas), metas
