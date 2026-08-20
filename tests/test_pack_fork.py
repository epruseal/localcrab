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

import math
from typing import Any

import pytest
from sqlalchemy import text as _sql_text

from opencrab.auth import Principal, principal_scope
from opencrab.config import Settings
from opencrab.ontology.builder import OntologyBuilder
from opencrab.ontology.query import HybridQuery
from opencrab.pack import fork as fork_mod
from opencrab.pack.fork import fork_pack
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

    return {
        "sql": sql, "graph": graph, "docs": docs, "vector": vector,
        "builder": builder, "hybrid": hybrid,
    }


def _fork(stack, *, principal, src_pack_id, **kw) -> dict[str, Any]:
    with principal_scope(principal):
        return fork_pack(
            stack["sql"], stack["graph"], stack["docs"], stack["vector"],
            stack["hybrid"], stack["builder"],
            principal=principal, src_pack_id=src_pack_id, **kw,
        )


def _seed_pack(
    stack, owner: Principal, pack_id: str, *,
    node_count: int = 2, with_edge: bool = True, with_source: bool = True,
    visibility: str | None = None,
) -> str:
    """Build a fully-``ready`` source pack: anchor (with its own vector,
    matching an ordinary ``pack_create``d pack -- needed for T28) + N
    ordinary content nodes + an edge between the first two (if any) +
    one legacy text source. Returns the actual pack_id assigned."""
    sql, builder, docs, vector, hybrid = (
        stack["sql"], stack["builder"], stack["docs"], stack["vector"], stack["hybrid"],
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
                sql, hybrid, docs, vector,
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
        stack["graph"]._exec_write(
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
        stack["graph"]._exec_write(
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
        stack["graph"]._exec_write(
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
        stack["graph"]._exec_write(
            "UPDATE graph_nodes SET node_type = 'Bogus' WHERE node_id = :nid",
            {"nid": f"{src_ok}-n0"},
        )
        out_ok = _fork(stack, principal=ALICE, src_pack_id=src_ok)
        assert out_ok["status"] == "ok", out_ok
        assert out_ok["copied"]["nodes"] == 9

        # 10 nodes, drop 2 (20% > 10% floor, must reject before reservation).
        src_bad = _seed_pack(stack, ALICE, "src-t39-bad", node_count=10, with_edge=False, with_source=False)
        stack["graph"]._exec_write(
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
        stack["graph"]._exec_write(
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
        stack["graph"]._exec_write(
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
        stack["graph"]._exec_write(
            "UPDATE graph_nodes SET properties = json_set(properties, '$.source', :v) "
            "WHERE node_id = :nid",
            {"v": composite, "nid": f"{src}-n1"},
        )
        stack["graph"]._exec_write(
            "UPDATE graph_nodes SET properties = json_set(properties, '$.node_id', json(:v)) "
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
        assert n1_new["node_id"] == ["x", "y"], "nested list must be left exactly as-is"


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
# T44 / T51 -- design v7 §5-1-6b's 2-pass vector batch decomposition.
# fork.py's preflight now runs the deterministic 2-pass decomposition the
# design specifies: pass 1 walks the exported batch in order, keeping the
# first occurrence of a duplicate id and establishing the reference
# dimension from the first surviving record (dropping anything that
# disagrees) -- both drops are Tier 1, counted under
# `skipped.vector_batch_invalid`; pass 2 re-runs the REAL
# `validate_import_records` over the survivors to confirm the decomposition
# actually worked, and refuses the WHOLE preflight (a bug signal, not a
# Tier 1 loss -- see `_vector_record_invalid`'s docstring and the 2-pass
# block right after it in fork.py) if it still finds something wrong.
#
# T44 exercises pass 1's own normal path: a duplicate id that pass 1 can
# fully resolve by itself, with pass 2 then confirming a clean pass.
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
        """T44 (design v7 §5-1-6b pass 1): a duplicate id surviving
        classification is decomposed BEFORE reservation -- pass 1 keeps the
        first occurrence and drops the second as a Tier 1 loss
        (`skipped.vector_batch_invalid`), pass 2 confirms the surviving
        batch now passes the real validator, and the fork completes as
        `ready`/`ok` with no Tier 2 demotion: nodes that wrote successfully
        (`copied.nodes == 10`) are NOT dragged down by a duplicate that was
        always a Tier 1, original-data defect, never one of our own writes
        failing."""
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
            "design v7 §5-1-6b pass 1: a duplicate vector id should be "
            f"deduped (keep-first) as a Tier 1 loss, not demote the fork; "
            f"got {out}"
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
        stack["graph"]._exec_write(
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
        stack["graph"]._exec_write(
            "UPDATE graph_nodes SET properties = json_set(properties, '$.node_id', json(:v)) "
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
        assert n1_new["node_id"] == [old_n0, "unrelated"], (
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
# T26 / T50 -- pack_id/slug length budget (design §3): _PACK_ID_BUDGET =
# 256 (registry column limit) - 9 (begin_pack_creation's worst-case
# collision suffix) = 247. An explicit new_pack_id over budget is REJECTED
# (never truncated); the DEFAULT slug is truncated instead so "{src}-fork"
# always fits.
# ---------------------------------------------------------------------------


class TestExplicitSlugLengthRejection:
    def test_t26_explicit_new_pack_id_over_budget_rejected_not_truncated(self, stack):
        """T26 (design §8 T26, §3, §5-1 step 8): an explicitly
        caller-supplied `new_pack_id` that exceeds the 247-char budget is
        REJECTED outright -- never silently truncated the way the DEFAULT
        slug is (design §3: truncating a name the caller chose on purpose
        would be surprising). Checked across several over-budget lengths
        (248, the next boundary; 256, the old un-budgeted limit; 300,
        comfortably past both) to prove this is a real inequality check,
        not one that only happens to catch a single value. Reverse-
        mutation: the `if len(requested_slug) > _PACK_ID_BUDGET: raise
        _reject(...)` branch for the explicit-new_pack_id path (fork.py
        step 8) was removed -- every one of these calls then proceeded to
        `begin_pack_creation` instead of being rejected, breaking this
        test's uniform "always error, never a reserved row" assertion."""
        src = _seed_pack(stack, ALICE, "src-t26", node_count=1, with_edge=False, with_source=False)
        for n in (248, 256, 300):
            new_id = "z" * n
            out = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=new_id)
            assert "error" in out and "budget" in out["error"], (n, out)
            assert get_pack(stack["sql"], new_id) is None, n


class TestSlugLengthBoundaries:
    def test_t50a_default_path_truncation_boundary_242_243(self, stack):
        """T50(a) (design §8 T50, §3, §5-1 step 8): the DEFAULT slug is
        `f"{src}-fork"`; when that exceeds the 247-char budget, src_pack_id
        itself is truncated (not the whole fork rejected) so the "-fork"
        suffix always fits. The pass/truncate boundary is exactly src
        length 242 (slug length 247, fits) vs 243 (slug length 248,
        truncated). Reverse-mutation: `_PACK_ID_BUDGET`'s definition was
        reverted from `256 - 9` to a bare `256` -- at src length 243 the
        (un-truncated) slug is only 248 chars, which now fits the wrong
        budget, so this test's "truncation actually happened" assertion
        for the 243 case failed."""
        pad = "a" * (242 - len("src-t50a-"))
        src_242 = _seed_pack(
            stack, ALICE, f"src-t50a-{pad}", node_count=1, with_edge=False, with_source=False,
        )
        assert len(src_242) == 242, len(src_242)
        out_242 = _fork(stack, principal=ALICE, src_pack_id=src_242)
        assert out_242["status"] == "ok", out_242
        assert out_242["pack_id"] == f"{src_242}-fork", out_242
        assert len(out_242["pack_id"]) == 247

        pad3 = "a" * (243 - len("src-t50a3-"))
        src_243 = _seed_pack(
            stack, ALICE, f"src-t50a3-{pad3}", node_count=1, with_edge=False, with_source=False,
        )
        assert len(src_243) == 243, len(src_243)
        out_243 = _fork(stack, principal=ALICE, src_pack_id=src_243)
        assert out_243["status"] == "ok", out_243
        assert out_243["pack_id"] != f"{src_243}-fork", (
            "a 243-char src's default slug (248 chars) exceeds the 247 "
            "budget and must be TRUNCATED, not passed through unchanged"
        )
        assert out_243["pack_id"].endswith("-fork")
        assert len(out_243["pack_id"]) <= 247
        assert out_243["pack_id"].startswith(src_243[:242])

    def test_t50b_explicit_new_pack_id_boundary_247_248(self, stack):
        """T50(b): an explicit `new_pack_id` at EXACTLY the 247-char
        budget is accepted verbatim (no truncation, no rejection); one
        character over (248) is rejected outright (T26 covers the
        rejection path more broadly; this pins the exact boundary
        transition). Reverse-mutation: `_PACK_ID_BUDGET` was changed from
        `256 - 9` to a plain `256` -- the 248-char explicit id, which
        should be rejected, was instead accepted, and this test's
        `'error' in out_248` assertion failed."""
        src = _seed_pack(stack, ALICE, "src-t50b", node_count=1, with_edge=False, with_source=False)

        new_247 = "b" * 247
        out_247 = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=new_247)
        assert out_247["status"] == "ok", out_247
        assert out_247["pack_id"] == new_247, out_247

        new_248 = "c" * 248
        out_248 = _fork(stack, principal=ALICE, src_pack_id=src, new_pack_id=new_248)
        assert "error" in out_248 and "budget" in out_248["error"], out_248
        assert get_pack(stack["sql"], new_248) is None


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
        stack["graph"]._exec_write(
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
