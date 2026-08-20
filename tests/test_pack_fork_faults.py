"""#201 `pack_fork` fault-injection tests (design v7 §12-6 "신규 행").

Companion to ``tests/test_pack_fork.py`` (owned by a different concurrent
worker, imported from here read-only for its ``stack`` fixture and seed
helpers -- see design §12-6: "신규 결함 주입형은 tests/test_pack_fork_faults.py
새 파일에 둔다"). This file owns every "신규 행" of §12-6's test table except
T51' (owned by ``tests/test_pack_fork.py``): T54, T54b, T55, T55b, T55c,
T55d, T56, T57, T57b, T57c, T58, T58b, T59, T60, T61, T62, T63, T63b, T63c,
T63d, T64, T65, T66, T66b, T67, T68.

Injection discipline (design §12-6, mandatory per row):
  - Tier 1 (the ORIGINAL exported data is already broken -- a shape the real
    store's own write API could never produce) is injected at the READ
    boundary: monkeypatching the return value of ``vector.export_pack_vectors``,
    ``docs.list_sources_scoped``, or ``graph.export_nodes_scoped``.
  - Tier 2 (OUR OWN write attempt failing) is injected at the WRITER
    boundary: monkeypatching ``builder.add_node``/``add_edge``,
    ``docs.upsert_source``, ``vector.import_vectors``, or the
    ``opencrab.pack.ownership`` helpers ``fork.py`` calls by name
    (``mark_pack_ready``/``mark_pack_partial``/``delete_pack_row``/
    ``get_pack`` -- patched as ``fork_mod.<name>`` so only fork.py's own
    calls are affected, never the test's own verification calls against the
    real registry).

Reverse-mutation is the completion condition for every row here (design
§12-6): each test's docstring names the exact mutation applied against
``opencrab/pack/fork.py`` during this session's verification pass and how
the test died under it. Every mutation was applied with ``Edit``, run, and
reverted with ``git checkout -- opencrab/pack/fork.py`` before moving to the
next row -- see the final delivery report for the transcript excerpts (not
repeated per-test here, matching this repo's existing convention in
``tests/test_pack_fork.py``'s own docstring).

The completeness-floor rows (T64, T65, T66, T66b, T67, T68) seed
``node_count=10`` so a single injected loss on the vector axis stays at
1/11 (~9%), under ``FORK_MAX_LOSS_RATIO`` (10%) -- design §12-6: "그러지
않으면 상세 skipped 응답이 아니라 완전성 하한의 bare rejection 이 나온다."
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text as _sql_text

from opencrab.auth import principal_scope
from opencrab.pack import fork as fork_mod
from opencrab.pack.ownership import anchor_node_id, begin_pack_creation, get_pack, mark_pack_ready
from opencrab.pack.source_writer import write_source
from tests.test_pack_fork import (  # noqa: F401 (pytest fixture re-export)
    ALICE,
    _fork,
    _seed_pack,
    stack,
)

# ruff's pyflakes-derived F811 cannot tell a pytest fixture parameter from a
# real name redefinition: every `def test_...(stack, ...)` below looks like
# it "redefines" the `stack` import above, cascading into one false-positive
# per test function. This directive is scoped to this file only (it does not
# touch shared lint config) and does not weaken any OTHER rule -- confirmed
# by re-running the full `ruff check .` gate after adding it (see the final
# report for the exact command and its clean output).
# ruff: noqa: F811


def _rows_for_slug(stack: dict[str, Any], prefix: str) -> list[tuple[str, str]]:
    """Every ``packs`` row whose pack_id starts with ``prefix`` -- used to
    confirm a rejection left zero rows behind (or exactly the compensating
    delete happened), mirroring ``tests/test_pack_fork.py``'s T14/T15/T39."""
    with stack["sql"]._engine.begin() as conn:
        return conn.execute(
            _sql_text("SELECT pack_id, status FROM packs WHERE pack_id LIKE :p"),
            {"p": f"{prefix}%"},
        ).fetchall()


def _patch_export_nodes_for_src(monkeypatch, stack, src: str, extra: list[dict[str, Any]]):
    """Tier 1 read-boundary injection: append ``extra`` fake records to the
    ONE preflight read of ``graph.export_nodes_scoped([src], ...)``, real
    everywhere else (in particular the R1 step-11 empty-dst check and H4's
    post-write re-read both call this same method against ``[dst]``, which
    must stay real or every test using this helper would spuriously reject
    at step 11 instead of exercising the classification loop)."""
    real = type(stack["graph"]).export_nodes_scoped

    def _patched(self, pack_ids, limit):
        records = real(self, pack_ids, limit)
        if pack_ids == [src]:
            return list(records) + extra
        return records

    monkeypatch.setattr(type(stack["graph"]), "export_nodes_scoped", _patched, raising=True)


def _patch_list_sources_for_src(monkeypatch, stack, src: str, extra: list[dict[str, Any]]):
    """Tier 1 read-boundary injection, source axis -- same shape as
    ``_patch_export_nodes_for_src`` above."""
    real = type(stack["docs"]).list_sources_scoped

    def _patched(self, pack_ids, limit):
        records = real(self, pack_ids, limit)
        if pack_ids == [src]:
            return list(records) + extra
        return records

    monkeypatch.setattr(type(stack["docs"]), "list_sources_scoped", _patched, raising=True)


def _patch_export_vectors(monkeypatch, stack, target_id: str, mutate):
    """Tier 1 read-boundary injection, vector axis: find the exported record
    whose ``id == target_id`` and mutate it in place (real store cannot
    produce most of these shapes directly, per design §12-6's mandatory
    read-boundary rule)."""
    real = type(stack["vector"]).export_pack_vectors

    def _patched(self, pack_id):
        records = real(self, pack_id)
        for rec in records:
            if isinstance(rec, dict) and rec.get("id") == target_id:
                mutate(rec)
                break
        return records

    monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _patched, raising=True)


def _force_step11_reject(monkeypatch, stack, src: str):
    """T63/T63b/T63c share one precondition: a step-11 ("dst must be
    genuinely empty") rejection, needed so ``_compensate_reservation`` (the
    design §12-4 helper under test) actually gets called at all. Forced
    deterministically by making ``docs.list_sources_scoped([dst], 1)``
    report non-empty -- real for the ``[src]`` preflight read, real for
    everything else, so this cannot also mask an unrelated failure."""
    real = type(stack["docs"]).list_sources_scoped

    def _fake(self, pack_ids, limit):
        if pack_ids == [src]:
            return real(self, pack_ids, limit)
        return [{"source_id": "bogus-nonempty-marker", "metadata": {}, "text": ""}]

    monkeypatch.setattr(type(stack["docs"]), "list_sources_scoped", _fake, raising=True)


# ---------------------------------------------------------------------------
# §12-1 R2 -- the writer span's own exception guard and _demote's registry
# reconfirmation logic (T54, T54b, T55, T55b, T55c, T55d).
# ---------------------------------------------------------------------------


def test_t54_node_writer_exception_demotes_with_registry_fields(stack, monkeypatch):
    """T54: a node writer raising a real exception (not a bad receipt) must
    be caught by R2's try/except and demote to 'partial', reporting
    registry_status_observed == "partial" and registry_transition_confirmed
    is True (mark_pack_partial itself is left real here, so it genuinely
    succeeds). Reverse-mutation: R2's `except Exception as exc:` was
    narrowed to `except ZeroDivisionError as exc:` (equivalent to removing
    the guard for our injected RuntimeError) -- the injected RuntimeError
    then propagated straight out of `_fork()` instead of being caught,
    raising `RuntimeError: injected T54 node writer failure` past the test
    body and failing it with that uncaught exception rather than the
    expected `status == "partial"` assertion.
    """
    src = _seed_pack(stack, ALICE, "src-t54", node_count=1, with_edge=False, with_source=False)

    real_add_node = type(stack["builder"]).add_node

    def _boom(self, *a, **kw):
        if kw.get("fork_copy"):
            raise RuntimeError("injected T54 node writer failure")
        return real_add_node(self, *a, **kw)

    monkeypatch.setattr(type(stack["builder"]), "add_node", _boom, raising=True)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    assert "write phase raised" in out["error"], out
    assert out["copied"]["nodes"] == 0, out["copied"]
    assert out["registry_status_observed"] == "partial", out
    assert out["registry_transition_confirmed"] is True, out
    row = get_pack(stack["sql"], out["pack_id"])
    assert row["status"] == "partial"


def test_t54b_clean_fork_reports_ready_registry_fields(stack):
    """T54b: a defect-free fork must report status: "ok",
    registry_status_observed == "ready", registry_transition_confirmed is
    True. Reverse-mutation: the two literal fields
    (`"registry_status_observed": "ready", "registry_transition_confirmed":
    True,`) were deleted from the success-path return dict (lines ~1404-1405)
    -- `out["registry_status_observed"]` then raised `KeyError:
    'registry_status_observed'`, since the field was absent from the
    response entirely rather than merely wrong (a contract violation this
    test would otherwise have missed under a weaker `.get(...)` assertion).
    """
    src = _seed_pack(stack, ALICE, "src-t54b", node_count=1, with_edge=False, with_source=False)
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["registry_status_observed"] == "ready", out
    assert out["registry_transition_confirmed"] is True, out


def test_t55_mark_pack_partial_false_creating_reports_observed_creating(stack, monkeypatch):
    """T55: mark_pack_partial returns False and the row's real status is
    still "creating" -> observed: "creating", confirmed: False.
    Reverse-mutation: `_demote`'s `else:` (not-`promoted`) branch was
    replaced with a hardcoded
    `registry_status_observed, registry_transition_confirmed = "partial", True`
    (skipping the requery entirely) -- `out["registry_status_observed"]`
    became `"partial"` instead of the expected `"creating"`, failing the
    assertion (the mutation makes the confirmation field falsely claim a
    transition it never actually re-checked).
    """
    src = _seed_pack(stack, ALICE, "src-t55", node_count=1, with_edge=False, with_source=False)

    def _boom_import(self, records, *, pack_id):
        raise RuntimeError("injected T55 tier2 failure")

    monkeypatch.setattr(type(stack["vector"]), "import_vectors", _boom_import, raising=True)
    monkeypatch.setattr(fork_mod, "mark_pack_partial", lambda *a, **kw: False)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    assert out["registry_status_observed"] == "creating", out
    assert out["registry_transition_confirmed"] is False, out
    row = get_pack(stack["sql"], out["pack_id"])
    assert row["status"] == "creating"


def test_t55b_mark_pack_partial_and_requery_both_raise_reports_unknown(stack, monkeypatch):
    """T55b: mark_pack_partial raises AND the requery also raises ->
    observed: "unknown", confirmed: False, and `_demote` itself never lets
    the exception escape. Reverse-mutation: `_demote`'s requery
    `except Exception as exc: requery_ok = False` was narrowed to
    `except ZeroDivisionError as exc:` -- the injected `get_pack` RuntimeError
    then propagated out of `_demote`, out of `_fork_pack_inner`'s R2
    `except Exception as exc: return _demote(...)` handler (which had
    already fired once and cannot re-catch what its own call raises), and
    out of `_fork()` itself, so the test's `out["status"]` access raised
    `RuntimeError: injected T55b get_pack failure` instead.
    """
    src = _seed_pack(stack, ALICE, "src-t55b", node_count=1, with_edge=False, with_source=False)

    def _boom_import(self, records, *, pack_id):
        raise RuntimeError("injected T55b tier2 failure")

    monkeypatch.setattr(type(stack["vector"]), "import_vectors", _boom_import, raising=True)

    def _boom_mark_partial(*a, **kw):
        raise RuntimeError("injected T55b mark_pack_partial failure")

    def _boom_get_pack(sql_, pack_id):
        if pack_id == src:
            return get_pack(sql_, pack_id)
        raise RuntimeError("injected T55b get_pack failure")

    monkeypatch.setattr(fork_mod, "mark_pack_partial", _boom_mark_partial)
    monkeypatch.setattr(fork_mod, "get_pack", _boom_get_pack)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    assert out["registry_status_observed"] == "unknown", out
    assert out["registry_transition_confirmed"] is False, out


def test_t55c_mark_pack_partial_false_row_missing_reports_missing(stack, monkeypatch):
    """T55c: mark_pack_partial returns False and the requery finds no row
    at all -> observed: "missing", confirmed: False (a DIFFERENT domain
    value than "unknown", which is reserved for "the requery itself
    failed"). Reverse-mutation: the `elif row is None:` branch's assignment
    was changed from `"missing", False` to `"unknown", False` (merging the
    two domains) -- `out["registry_status_observed"] == "missing"` failed
    (`"unknown" != "missing"`), showing the two failure kinds had become
    indistinguishable to a caller.
    """
    src = _seed_pack(stack, ALICE, "src-t55c", node_count=1, with_edge=False, with_source=False)

    def _boom_import(self, records, *, pack_id):
        raise RuntimeError("injected T55c tier2 failure")

    monkeypatch.setattr(type(stack["vector"]), "import_vectors", _boom_import, raising=True)
    monkeypatch.setattr(fork_mod, "mark_pack_partial", lambda *a, **kw: False)

    def _missing_get_pack(sql_, pack_id):
        if pack_id == src:
            return get_pack(sql_, pack_id)
        return None

    monkeypatch.setattr(fork_mod, "get_pack", _missing_get_pack)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    assert out["registry_status_observed"] == "missing", out
    assert out["registry_transition_confirmed"] is False, out


def test_t55d_mark_pack_partial_false_requery_already_partial_reports_confirmed(stack, monkeypatch):
    """T55d: mark_pack_partial returns False but the requery finds the row
    ALREADY "partial" (another actor's age-demotion beat us to it) ->
    observed: "partial", confirmed: True (the isolation goal was already
    achieved, just not by this call). Reverse-mutation: the
    `elif row.get("status") == "partial": ... True` special case was
    deleted, falling through to the generic `else` branch which always sets
    `registry_transition_confirmed = False` -- `out["registry_transition_confirmed"]
    is True` failed (`False is not True`), an already-achieved isolation
    was reported as unconfirmed.
    """
    src = _seed_pack(stack, ALICE, "src-t55d", node_count=1, with_edge=False, with_source=False)

    def _boom_import(self, records, *, pack_id):
        raise RuntimeError("injected T55d tier2 failure")

    monkeypatch.setattr(type(stack["vector"]), "import_vectors", _boom_import, raising=True)
    monkeypatch.setattr(fork_mod, "mark_pack_partial", lambda *a, **kw: False)

    def _already_partial_get_pack(sql_, pack_id):
        if pack_id == src:
            return get_pack(sql_, pack_id)
        return {"pack_id": pack_id, "status": "partial", "owner_id": ALICE.user_id}

    monkeypatch.setattr(fork_mod, "get_pack", _already_partial_get_pack)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    assert out["registry_status_observed"] == "partial", out
    assert out["registry_transition_confirmed"] is True, out


# ---------------------------------------------------------------------------
# §12-1 R1 -- the reservation-span exception guard (T56).
# ---------------------------------------------------------------------------


def test_t56_step11_store_exception_compensates_with_delete(stack, monkeypatch):
    """T56: an exception raised INSIDE step 11's axis-empty check (not a
    normal rejection) must still be caught by R1's generic
    `except Exception as exc:` and compensated with a delete, leaving ZERO
    registry rows -- not leak past `fork_pack` with a stranded "creating"
    row. Reverse-mutation: R1's `except Exception as exc:` was narrowed to
    `except ZeroDivisionError as exc:` -- the injected RuntimeError then
    propagated out of `_fork_pack_inner`, out of `fork_pack`'s own
    `except _RejectedError` (which does not match a plain RuntimeError),
    and out of `_fork()` itself, so `out = _fork(...)` raised
    `RuntimeError: injected T56 step11 failure` instead of returning a
    dict, and (separately confirmed) a "creating" row for the derived slug
    was left behind since the compensating delete never ran.
    """
    src = _seed_pack(stack, ALICE, "src-t56", node_count=1, with_edge=False, with_source=False)

    real = type(stack["graph"]).export_nodes_scoped

    def _patched(self, pack_ids, limit):
        if pack_ids == [src]:
            return real(self, pack_ids, limit)
        raise RuntimeError("injected T56 step11 failure")

    monkeypatch.setattr(type(stack["graph"]), "export_nodes_scoped", _patched, raising=True)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert "error" in out, out
    assert _rows_for_slug(stack, f"{src}-fork") == []


# ---------------------------------------------------------------------------
# §12-1 R3 -- the verdict-span reconfirmation logic (T57, T57c, T57b).
# ---------------------------------------------------------------------------


def test_t57_mark_pack_ready_raises_but_registry_is_ready_reports_ok(stack, monkeypatch):
    """T57: mark_pack_ready raises but the write genuinely committed (the
    real row IS "ready") -> the fork must still report status: "ok",
    observed: "ready", confirmed: True (commit succeeded, only the response
    was lost). Reverse-mutation: the entire `if not finalized:` reconfirm
    block was disabled (replaced with an unconditional
    `return _demote("could not promote to ready after a fully successful
    write phase")`) -- `out["status"] == "ok"` failed (`"partial" !=
    "ok"`), a successful fork was misclassified as a failure purely because
    reporting the SUCCESS raised, without ever checking whether it had.
    """
    src = _seed_pack(stack, ALICE, "src-t57", node_count=1, with_edge=False, with_source=False)

    real_mark_ready = fork_mod.mark_pack_ready

    def _raise_after_real_commit(sql_, pack_id, owner_id):
        real_mark_ready(sql_, pack_id, owner_id)
        raise RuntimeError("injected T57 mark_pack_ready failure (after real commit)")

    monkeypatch.setattr(fork_mod, "mark_pack_ready", _raise_after_real_commit)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["registry_status_observed"] == "ready", out
    assert out["registry_transition_confirmed"] is True, out


def test_t57c_mark_pack_ready_and_requery_both_raise_demotes_without_leaking(stack, monkeypatch):
    """T57c: mark_pack_ready raises AND R3's own requery also raises ->
    demoted to "partial", no exception escapes the tool. Reverse-mutation:
    R3's requery `except Exception as exc: requery_ok = False` was narrowed
    to `except ZeroDivisionError as exc:` -- the injected `get_pack`
    RuntimeError then propagated out of `_fork_pack_inner`'s R3 try (whose
    own `except Exception as exc: return _demote(...)` handler is a
    SEPARATE, outer try -- it does catch this, actually, since it wraps the
    whole R3 body) -- concretely this exercised the outer R3
    `except Exception as exc: return _demote(f"verdict phase raised:
    {exc!r}")` fallback path instead of the intended narrower one, and
    still returned `status: "partial"` without leaking, but with a
    DIFFERENT error message than a correctly-guarded implementation would
    give (`"verdict phase raised: RuntimeError(...)"` instead of
    `"could not promote to ready after a fully successful write phase"`) --
    confirming the inner guard has detection power distinct from the outer
    one, since the reported reason changed.
    """
    src = _seed_pack(stack, ALICE, "src-t57c", node_count=1, with_edge=False, with_source=False)

    def _boom_mark_ready(sql_, pack_id, owner_id):
        raise RuntimeError("injected T57c mark_pack_ready failure")

    def _boom_get_pack(sql_, pack_id):
        if pack_id == src:
            return get_pack(sql_, pack_id)
        raise RuntimeError("injected T57c get_pack failure")

    monkeypatch.setattr(fork_mod, "mark_pack_ready", _boom_mark_ready)
    monkeypatch.setattr(fork_mod, "get_pack", _boom_get_pack)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    # Pinned to the INNER guard's own fixed reason (not the outer R3
    # fallback's "verdict phase raised: ..." wrapper) -- this is what makes
    # the narrowed-inner-except mutation described above lethal: under that
    # mutation the message changes to the outer fallback's wording instead.
    assert out["error"] == "could not promote to ready after a fully successful write phase", out


def test_t57b_mark_pack_ready_false_registry_creating_demotes(stack, monkeypatch):
    """T57b: mark_pack_ready returns False (no exception) and the real
    registry row is genuinely still "creating" (never promoted) -> demoted
    to "partial", not misreported as "ok". Reverse-mutation: the
    `if not (requery_ok and requery is not None and requery.get("status")
    == "ready"): return _demote(...)` guard was replaced with `pass` (never
    demote, always fall through to the "ok" response) -- `out["status"] ==
    "partial"` failed (`"ok" != "partial"`), a pack that was never actually
    promoted was reported as a successful fork.
    """
    src = _seed_pack(stack, ALICE, "src-t57b", node_count=1, with_edge=False, with_source=False)

    monkeypatch.setattr(fork_mod, "mark_pack_ready", lambda *a, **kw: False)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    row = get_pack(stack["sql"], out["pack_id"])
    assert row["status"] == "partial"


# ---------------------------------------------------------------------------
# §12-3 -- pre-reservation whole-fork rejections (T58, T58b, T59, T60, T61,
# T62).
# ---------------------------------------------------------------------------


def test_t58_source_sharing_anchor_id_rejects_before_reservation(stack, monkeypatch):
    """T58: a source whose `source_id == anchor_node_id(src)` must reject
    the WHOLE fork before reservation (zero registry rows) -- copying it
    would let the copy impersonate the new pack's own anchor id, and
    dropping it silently would lose its vector unreported. Reverse-mutation:
    the `if source_id == src_anchor: raise _declared_limit_reject(...)`
    check was disabled (`if False and source_id == src_anchor:`) -- the
    fork then proceeded to reservation and a real write attempt instead of
    rejecting, so `"error" in out` failed (the response had a `"status"`
    key instead) and a registry row was left behind.
    """
    src = _seed_pack(stack, ALICE, "src-t58", node_count=1, with_edge=False, with_source=False)
    anchor_id = anchor_node_id(src)
    _patch_list_sources_for_src(
        monkeypatch, stack, src,
        [{"source_id": anchor_id, "metadata": {}, "text": "impersonating the anchor"}],
    )
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert "error" in out, out
    assert "shares the pack anchor's id" in out["error"], out
    assert get_pack(stack["sql"], f"{src}-fork") is None
    assert _rows_for_slug(stack, f"{src}-fork") == []


@pytest.mark.parametrize(
    "case,record",
    [
        ("wrong_node_type", {"space": "resource", "labels": ["Document"]}),
        ("wrong_space", {"space": "evidence", "labels": ["Dataset"]}),
        ("missing_space", {"space": None, "labels": ["Dataset"]}),
        ("empty_labels", {"space": "resource", "labels": []}),
    ],
)
def test_t58b_anchor_id_node_not_a_genuine_anchor_rejects_before_reservation(
    stack, monkeypatch, case, record,
):
    """T58b: a `ready` pack with a node claiming the anchor's own id
    (`node_id == anchor_node_id(src)`) that is NOT a structurally genuine
    anchor (space != "resource" or node_type != "Dataset", including the
    degenerate missing-space/empty-labels shapes) must reject the whole
    fork before reservation, in all four parametrized shapes. Two DIFFERENT
    reverse-mutations were run against this one test during verification
    (see the final report for both transcripts): (1) removing the
    genuine-anchor shape check entirely (`if space == "resource" and
    node_type == "Dataset":` -> `if True:`, unconditionally treating any
    node_id == anchor as the real anchor) killed ALL FOUR cases -- every
    shape is unconditionally absorbed as the genuine anchor and excluded
    from classification, so `"error" in out` failed for wrong_node_type,
    wrong_space, missing_space, AND empty_labels alike; (2) reordering the
    anchor-id check to run AFTER the missing-space/type check killed only
    the missing_space and empty_labels cases (wrong_node_type/wrong_space
    still passed, since those two retain a valid space/node_type and reach
    the anchor-id check unchanged) -- for missing_space and empty_labels,
    the record is instead caught by the (unrelated) missing-space/type
    guard first and folded into an ordinary Tier 1 `node_errors` skip;
    with this test's small `node_count=1` seed that single skip crosses the
    10% completeness floor, so the fork still ends in `"error" in out`
    (`"fork rejected: node loss ratio 1/3 exceeds the 10% completeness
    floor"`) rather than the expected `"...shares the pack anchor's id"`
    message -- it is the specific-message assertion below, not the mere
    presence of an error, that catches this mutation.
    """
    src = _seed_pack(stack, ALICE, "src-t58b", node_count=1, with_edge=False, with_source=False)
    anchor_id = anchor_node_id(src)
    props: dict[str, Any] = {"id": anchor_id, "title": "not a real anchor"}
    if record["space"] is not None:
        props["space"] = record["space"]
    bogus = {"props": props, "labels": record["labels"]}
    _patch_export_nodes_for_src(monkeypatch, stack, src, [bogus])

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert "error" in out, (case, out)
    assert "shares the pack anchor's id" in out["error"], (case, out)
    assert get_pack(stack["sql"], f"{src}-fork") is None
    assert _rows_for_slug(stack, f"{src}-fork") == []


def test_t59_duplicate_source_id_rejects_before_reservation(stack, monkeypatch):
    """T59: two source records sharing the same `source_id` (unreachable
    via the real doc store, whose PK IS source_id -- a store-contract
    violation, not merely messy data) must reject the whole fork. Reverse-
    mutation: the `if source_id in seen_source_ids: raise
    _declared_limit_reject(...)` check was disabled -- the fork then
    proceeded straight through to a clean `"ok"` completion with BOTH
    duplicate records written (`copied["sources"] == 2`, no crash, no
    collision at the real writer either since each gets its own generated
    dst-space id) -- `assert "error" in out` failed outright, confirming
    this whole-fork guard is the only thing standing between a store-
    contract-violating duplicate and a silently accepted double-write.
    """
    src = _seed_pack(stack, ALICE, "src-t59", node_count=1, with_edge=False, with_source=False)
    _patch_list_sources_for_src(
        monkeypatch, stack, src,
        [
            {"source_id": "dup-t59", "metadata": {}, "text": "first"},
            {"source_id": "dup-t59", "metadata": {}, "text": "second"},
        ],
    )
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert "error" in out, out
    assert "appears more than once" in out["error"], out
    assert _rows_for_slug(stack, f"{src}-fork") == []


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_t60_duplicate_node_id_different_type_rejects_regardless_of_order(stack, monkeypatch, order):
    """T60: two node records sharing the same `node_id` but a different
    `node_type` must reject the whole fork REGARDLESS of which record
    `export_nodes_scoped` happens to return first (no ORDER BY is declared,
    so there is no deterministic, lossless way to pick a winner).
    Reverse-mutation: the
    `if node_id in seen_node_types and seen_node_types[node_id] !=
    node_type: raise _declared_limit_reject(...)` check was disabled --
    with this test's small `node_count=1` seed, silently keeping the
    second-classified record still leaves the FIRST record's id
    unaccounted for in the mapping, so the fork still ends in `"error" in
    out` -- but as a generic `"fork rejected: node loss ratio 2/4 exceeds
    the 10% completeness floor"`, not the specific "more than one
    node_type" message, in BOTH the forward and reverse parametrizations.
    It is the specific-message assertion (`assert "more than one
    node_type" in out["error"]`), not the mere presence of an error, that
    catches this mutation and that confirms the two orders no longer share
    a common, order-independent REASON for rejecting.
    """
    src = _seed_pack(stack, ALICE, "src-t60", node_count=1, with_edge=False, with_source=False)
    rec_a = {"props": {"id": "dup-t60", "space": "resource"}, "labels": ["TypeA"]}
    rec_b = {"props": {"id": "dup-t60", "space": "resource"}, "labels": ["TypeB"]}
    extra = [rec_a, rec_b] if order == "forward" else [rec_b, rec_a]
    _patch_export_nodes_for_src(monkeypatch, stack, src, extra)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert "error" in out, (order, out)
    assert "more than one node_type" in out["error"], (order, out)
    assert _rows_for_slug(stack, f"{src}-fork") == []


def test_t61_non_injective_mapping_rejects_and_deletes_reservation(stack, monkeypatch):
    """T61: if `build_mapping` (forced here, mirroring
    tests/test_pack_fork.py's T15 monkeypatch technique) ever returns a
    non-injective mapping after the anchor fix-up, this is a BUG SIGNAL
    (every known real-world cause is already covered by §12-3's four
    dedicated rejections above) -- the fork must reject via the
    compensating-delete path, leaving zero registry rows. Reverse-mutation:
    the `if len(set(mapping.values())) != len(mapping): raise
    _compensate_reservation(...)` check was disabled -- the fork then
    proceeded into the write phase with a colliding mapping, and the
    collision instead surfaced downstream as the vector backend's own
    duplicate-id rejection (`"vector import failed: import_vectors:
    duplicate id '...' within the batch (position 1)"`), a completely
    different error family from the intended `"internal error: id mapping
    is not injective after anchor fix-up"` -- `assert "not injective" in
    out["error"]` failed, confirming this preflight guard is what turns an
    internal bug signal into a clean, well-labeled compensating rejection
    instead of an opaque downstream write-layer crash.
    """
    src = _seed_pack(stack, ALICE, "src-t61", node_count=2, with_edge=False, with_source=False)

    real_build_mapping = fork_mod.build_mapping

    def _colliding(node_ids, source_ids, *, salt, src_anchor, dst_anchor):
        mapping = real_build_mapping(
            node_ids, source_ids, salt=salt, src_anchor=src_anchor, dst_anchor=dst_anchor,
        )
        ids = list(node_ids)
        if len(ids) >= 2:
            mapping[ids[1]] = mapping[ids[0]]
        return mapping

    monkeypatch.setattr(fork_mod, "build_mapping", _colliding)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert "error" in out, out
    assert "not injective" in out["error"], out
    for _pid, status in _rows_for_slug(stack, f"{src}-fork"):
        assert status != "creating", "must not strand a 'creating' row"


def test_t62_h4_catches_unremapped_structural_source_id(stack, monkeypatch):
    """T62: H4's post-write re-read of `docs.list_sources_scoped([dst],
    ...)` must catch a row whose STRUCTURAL `source_id` column (not just
    its metadata) is still an old, source-space id -- injected here as an
    extra row appended to the H4 requery specifically (limit > 1,
    pack_ids != [src]; the real step-11 empty-dst check uses limit == 1 and
    is left untouched). Demotes to "partial". Reverse-mutation: ONLY the
    structural `source_id` check inside `_h4_verify` (the
    `row_source_id = row.get("source_id"); if isinstance(...) and
    row_source_id in mapping_keys: hits.append(...)` block) was removed,
    leaving the metadata scan intact -- `out["status"] == "partial"` failed
    (`"ok" != "partial"`), confirming this leak is invisible to the
    metadata-only scan alone (design's own note: "T8 의 역변이로는 죽지
    않는다").
    """
    src = _seed_pack(stack, ALICE, "src-t62", node_count=1, with_edge=False, with_source=True)

    real = type(stack["docs"]).list_sources_scoped

    def _inject_h4_leak(self, pack_ids, limit):
        rows = real(self, pack_ids, limit)
        if pack_ids != [src] and limit > 1:
            rows = list(rows) + [{"source_id": "s0", "metadata": {}, "text": ""}]
        return rows

    monkeypatch.setattr(type(stack["docs"]), "list_sources_scoped", _inject_h4_leak, raising=True)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "partial", out
    assert "leaked references" in out["error"], out
    assert "'s0'" in out["error"], out


# ---------------------------------------------------------------------------
# §12-4 -- `_compensate_reservation`'s exact branching (T63, T63b, T63c),
# and §12-1 R1's `except _RejectedError: raise` carve-out (T63d).
# ---------------------------------------------------------------------------


def test_t63_delete_false_requery_owned_creating_reports_status_and_pack_id(stack, monkeypatch):
    """T63: with a step-11 rejection forced, `delete_pack_row` returns
    False and the requery finds the row still "creating" and owned by the
    calling principal -> the rejection message must name the observed
    status AND the pack_id. Reverse-mutation: the `if deleted:` check was
    bypassed (forcing `deleted = True` unconditionally right after the
    `delete_pack_row` call) -- `out["error"]` became the bare original
    reason (`"pack registry state inconsistent after reservation"`)
    with no `"could not be cleaned up"` / status / pack_id detail, so
    `assert "could not be cleaned up" in out["error"]` failed: the
    unconfirmed cleanup was masquerading as a clean rejection.
    """
    src = _seed_pack(stack, ALICE, "src-t63", node_count=1, with_edge=False, with_source=False)
    _force_step11_reject(monkeypatch, stack, src)
    monkeypatch.setattr(fork_mod, "delete_pack_row", lambda *a, **kw: False)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    dst = f"{src}-fork"
    assert "error" in out, out
    assert "pack registry state inconsistent after reservation" in out["error"], out
    assert "could not be cleaned up" in out["error"], out
    assert repr(dst) in out["error"], out
    assert "'creating'" in out["error"], out
    row = get_pack(stack["sql"], dst)
    assert row is not None and row["status"] == "creating"


def test_t63b_delete_raises_requery_missing_reports_bare_reason(stack, monkeypatch):
    """T63b: with a step-11 rejection forced, `delete_pack_row` raises AND
    the requery finds no row at all -> treated as an already-achieved
    compensation, message is the ORIGINAL bare reason with no cleanup
    suffix appended. Reverse-mutation: the `if requery_ok and row is None:`
    branch was disabled (`if False and requery_ok and row is None:`),
    forcing fallthrough to the generic suffixed message -- `out == {
    "error": "pack registry state inconsistent after reservation"}` failed
    because the actual message gained the
    `"; reservation cleanup could not be confirmed; operator inspection
    required"` suffix: an already-achieved compensation was reported as a
    failure.
    """
    src = _seed_pack(stack, ALICE, "src-t63b", node_count=1, with_edge=False, with_source=False)
    _force_step11_reject(monkeypatch, stack, src)

    def _boom_delete(*a, **kw):
        raise RuntimeError("injected T63b delete_pack_row failure")

    def _missing_get_pack(sql_, pack_id):
        if pack_id == src:
            return get_pack(sql_, pack_id)
        return None

    monkeypatch.setattr(fork_mod, "delete_pack_row", _boom_delete)
    monkeypatch.setattr(fork_mod, "get_pack", _missing_get_pack)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out == {"error": "pack registry state inconsistent after reservation"}, out


def test_t63c_delete_false_requery_foreign_owner_reports_neutral_message(stack, monkeypatch):
    """T63c: with a step-11 rejection forced, `delete_pack_row` returns
    False and the requery finds a row owned by SOMEONE ELSE -> #143
    invariant 7 forbids exposing another owner's row state, so the message
    must be neutral: no status, no pack_id. Reverse-mutation: the owner
    comparison `row.get("owner_id") == owner_id` was dropped from the
    condition (any row, regardless of owner, took the status-revealing
    branch) -- `out["error"]` gained `"could not be cleaned up (observed
    status 'creating')"` naming the foreign row's status, so the exact
    neutral-message assertion failed, reproducing exactly the #143
    invariant 7 violation this row exists to catch.
    """
    src = _seed_pack(stack, ALICE, "src-t63c", node_count=1, with_edge=False, with_source=False)
    _force_step11_reject(monkeypatch, stack, src)
    monkeypatch.setattr(fork_mod, "delete_pack_row", lambda *a, **kw: False)

    def _foreign_get_pack(sql_, pack_id):
        if pack_id == src:
            return get_pack(sql_, pack_id)
        return {"pack_id": pack_id, "owner_id": "user_mallory_not_the_caller", "status": "creating"}

    monkeypatch.setattr(fork_mod, "get_pack", _foreign_get_pack)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out == {
        "error": (
            "pack registry state inconsistent after reservation; "
            "reservation cleanup could not be confirmed; operator inspection required"
        ),
    }, out


def test_t63d_identity_conflict_compensates_exactly_once_with_original_wording(stack, monkeypatch):
    """T63d: an identity-conflict rejection during step 12's probe must
    call `_compensate_reservation` EXACTLY ONCE, and the response's `error`
    text must be `identity_reject_message`'s wording verbatim -- NOT
    re-wrapped as `"pre-write check failed: ..."`. Reverse-mutation: R1's
    `except _RejectedError: raise` carve-out (line ~1045-1046) was removed,
    letting the generic `except Exception as exc:` catch the already-raised
    `_RejectedError` too -- `delete_pack_row` was then called a SECOND
    time (`len(delete_calls) == 1` failed, it was `2`) and
    `out["error"]` became
    `"pre-write check failed: _RejectedError(...)"` instead of the exact
    `"n0-t63d: identity is already attributed to a different pack"`,
    confirming both assertions independently detect the missing carve-out.
    """
    src = _seed_pack(stack, ALICE, "src-t63d", node_count=1, with_edge=False, with_source=False)

    with principal_scope(ALICE):
        begin_pack_creation(stack["sql"], ALICE.user_id, "occupier-t63d")
        stack["builder"].add_node(
            space="resource", node_type="Dataset", node_id=anchor_node_id("occupier-t63d"),
            properties={"title": "t", "description": "d", "created_by": "test"},
            pack_id="occupier-t63d", pack_anchor=True,
        )
        mark_pack_ready(stack["sql"], "occupier-t63d", ALICE.user_id)
        stack["builder"].add_node(
            space="resource", node_type="Document", node_id="n0-t63d",
            properties={"title": "already here"}, pack_id="occupier-t63d",
        )

    real_build_mapping = fork_mod.build_mapping

    def _colliding(node_ids, source_ids, *, salt, src_anchor, dst_anchor):
        mapping = real_build_mapping(
            node_ids, source_ids, salt=salt, src_anchor=src_anchor, dst_anchor=dst_anchor,
        )
        for old_id in node_ids:
            mapping[old_id] = "n0-t63d"
        return mapping

    monkeypatch.setattr(fork_mod, "build_mapping", _colliding)

    delete_calls: list[Any] = []
    real_delete = fork_mod.delete_pack_row

    def _counting_delete(*a, **kw):
        delete_calls.append((a, kw))
        return real_delete(*a, **kw)

    monkeypatch.setattr(fork_mod, "delete_pack_row", _counting_delete)

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert "error" in out, out
    assert len(delete_calls) == 1, delete_calls
    assert out["error"] == "n0-t63d: identity is already attributed to a different pack", out


# ---------------------------------------------------------------------------
# §12-2 -- vector record validation delegation (T64, T65, T66, T66b, T67,
# T68).
# ---------------------------------------------------------------------------


def test_t64_unknown_metadata_key_is_tier1_skip_ok_completes(stack, monkeypatch):
    """T64: a vector record with an unrecognized top-level key must be a
    per-record Tier 1 skip (`skipped.vector_invalid`), the fork completes
    `ok`, and every other vector still lands. Reverse-mutation:
    `_vector_record_invalid` was replaced with a version that always
    returns `None` (simulating the old hand-rolled mirror this module's own
    docstring says used to miss unknown keys) -- the bad record then
    survived pass 0 unfiltered and pass 2 (the real validator, run over the
    whole decomposed batch) caught it instead, refusing the WHOLE preflight
    -- `out["status"] == "ok"` failed (`"error"` was present, `"status"`
    was not), turning what should be a 1-record Tier 1 loss into a total
    rejection.
    """
    src = _seed_pack(stack, ALICE, "src-t64", node_count=10, with_edge=False, with_source=False)
    _patch_export_vectors(
        monkeypatch, stack, f"{src}-n0", lambda rec: rec.__setitem__("totally_unknown_field", "boom"),
    )
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["skipped"]["vector_invalid"] == 1, out["skipped"]
    assert out["copied"]["vectors"] == 9, out["copied"]


def test_t65_non_finite_embedding_component_is_tier1_skip_ok_completes(stack, monkeypatch):
    """T65: a vector record whose embedding contains a component that is
    not float32-representable (`1e40`, which `struct.pack("f", ...)`
    silently saturates to `inf` rather than raising) must be a per-record
    Tier 1 skip, fork completes `ok`. Reverse-mutation: same as T64 --
    `_vector_record_invalid` forced to always return `None` -- the bad
    embedding then survived pass 0 (the hand-rolled-mirror-style saturation
    the design's own history had before delegating to the real validator)
    and pass 2's real validator caught it, refusing the WHOLE preflight --
    `out["status"] == "ok"` failed the same way as T64's.
    """
    src = _seed_pack(stack, ALICE, "src-t65", node_count=10, with_edge=False, with_source=False)

    def _inject_inf(rec):
        rec["embedding"] = [1e40] + list(rec["embedding"][1:])

    _patch_export_vectors(monkeypatch, stack, f"{src}-n0", _inject_inf)
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["skipped"]["vector_invalid"] == 1, out["skipped"]
    assert out["copied"]["vectors"] == 9, out["copied"]


def test_t66_non_dict_record_is_tier1_skip_no_crash(stack, monkeypatch):
    """T66: a non-dict item in `export_pack_vectors`'s return (a shape
    pgvector's export can hand back) must be a Tier 1 skip with NO
    exception escaping the tool -- the dict-shape check must run BEFORE any
    `.get`/field access. Reverse-mutation: the leading
    `if not isinstance(rec, dict): skipped_vector_invalid += 1; ...;
    continue` guard was removed, letting the plain string fall through to
    `rec_id = rec.get("id")` -- `_fork()` then raised
    `AttributeError: 'str' object has no attribute 'get'` instead of
    returning a dict, so `out["status"]` access failed with that uncaught
    exception.
    """
    src = _seed_pack(stack, ALICE, "src-t66", node_count=10, with_edge=False, with_source=False)
    real = type(stack["vector"]).export_pack_vectors

    def _inject_non_dict(self, pack_id):
        records = real(self, pack_id)
        if pack_id == src:
            return list(records) + ["not-a-dict-record"]
        return records

    monkeypatch.setattr(type(stack["vector"]), "export_pack_vectors", _inject_non_dict, raising=True)
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["skipped"]["vector_invalid"] == 1, out["skipped"]
    assert out["copied"]["vectors"] == 10, out["copied"]


@pytest.mark.parametrize(
    "bad_metadata",
    [
        "this string deliberately contains pack_id as a substring",
        12345,
    ],
    ids=["str_metadata", "int_metadata"],
)
def test_t66b_non_dict_metadata_is_tier1_skip_no_crash(stack, monkeypatch, bad_metadata):
    """T66b: non-dict `metadata` (str or int -- pgvector's json.loads of
    stored jsonb can hand either back) must be a Tier 1 skip with no
    exception escaping. The str case's literal value deliberately CONTAINS
    "pack_id" as a substring (design §12-6's own requirement) -- otherwise
    the str case's `in` check would short-circuit False even with the
    guard removed and this test would not independently detect that
    mutation for the str branch. Reverse-mutation: the `isinstance(meta,
    dict) and` prefix was removed from the mistagged-classification
    condition (now bare `"pack_id" in meta and meta["pack_id"] !=
    src_pack_id`) -- the int case raised
    `TypeError: argument of type 'int' is not iterable` at `"pack_id" in
    meta`, and the str case raised `TypeError: string indices must be
    integers` at `meta["pack_id"]` (its substring DOES make `"pack_id" in
    meta` true, so it reaches the indexing) -- both propagated out of
    `_fork()` uncaught, failing `out["status"]` access in both
    parametrizations independently.
    """
    src = _seed_pack(stack, ALICE, "src-t66b", node_count=10, with_edge=False, with_source=False)
    _patch_export_vectors(monkeypatch, stack, f"{src}-n0", lambda rec: rec.__setitem__("metadata", bad_metadata))
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["skipped"]["vector_invalid"] == 1, out["skipped"]
    assert out["copied"]["vectors"] == 9, out["copied"]


def test_t67_metadata_pack_id_none_is_mistagged_not_invalid(stack, monkeypatch):
    """T67: `metadata.pack_id: None` is a PRESENT-but-different pack_id
    (key-existence, not truthiness -- `None != src_pack_id`), so this must
    be `skipped.vector_mistagged`, not `vector_invalid`, and the fork
    completes `ok`. Reverse-mutation: the key-existence check `"pack_id" in
    meta` was replaced with truthiness (`meta.get("pack_id")`) --
    `out["skipped"]["vector_mistagged"] == 1` failed (became `0`, since
    `None` is falsy and no longer trips the mistagged branch); the SAME
    record then fell through to `_vector_record_invalid`'s real-validator
    call, which caught the `pack_id` mismatch there instead, moving it to
    `vector_invalid` -- confirmed by the paired assertion
    `out["skipped"]["vector_invalid"] == 0` also failing (became `1`).
    """
    src = _seed_pack(stack, ALICE, "src-t67", node_count=10, with_edge=False, with_source=False)

    def _inject_none_pack_id(rec):
        meta = dict(rec.get("metadata") or {})
        meta["pack_id"] = None
        rec["metadata"] = meta

    _patch_export_vectors(monkeypatch, stack, f"{src}-n0", _inject_none_pack_id)
    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["skipped"]["vector_mistagged"] == 1, out["skipped"]
    assert out["skipped"]["vector_invalid"] == 0, out["skipped"]


def test_t68_chroma_uri_bearing_record_survives_raw_copy(stack, monkeypatch):
    """T68: a chroma record carrying a string `uris` value (planted via a
    direct `add(uris=..., embedding=...)` call against the real chroma
    collection -- the only way to create this shape, since no writer this
    codebase owns ever sets `uris` itself) must survive the raw copy with
    its `uris` value intact. Skipped outright on a non-chroma vector
    backend (`_vec_backend`'s `"sql"`/`"sqlalchemy"` kinds have no `uris`
    concept at all). Reverse-mutation: `allow_uris` was hardcoded to
    `False` (`allow_uris = False` instead of
    `_vec_backend(vector)[0] == "chroma"`) -- the uri-bearing record then
    failed `_vector_record_invalid`'s real-validator call (chroma's own
    `import_vectors` still passes `allow_uris=True` internally, unaffected
    by this preflight-only mutation, but PREFLIGHT now believes uris are
    never allowed) and became a Tier 1 `vector_invalid` skip instead of
    surviving -- `len(uri_records) == 1` failed (`0` records with a
    non-None `uris` field landed in the copy), a perfectly valid record
    lost for nothing.
    """
    if _vec_backend_kind(stack["vector"]) != "chroma":
        pytest.skip("uris are only meaningful on the chroma backend")

    src = _seed_pack(stack, ALICE, "src-t68", node_count=10, with_edge=False, with_source=False)
    with principal_scope(ALICE):
        write_source(
            stack["sql"], stack["hybrid"], stack["docs"], stack["vector"],
            text="legacy uri-bearing source", source_id="s-t68", pack_id=src,
            write_vector=False,
        )
    sample = stack["vector"].export_pack_vectors(src)[0]["embedding"]
    dim = len(sample)
    handle = stack["vector"]._collection_handle()
    handle.add(
        ids=["s-t68"],
        embeddings=[[0.001] * dim],
        documents=["legacy uri-bearing source"],
        metadatas=[{"pack_id": src, "source_id": "s-t68"}],
        uris=["file:///legacy/blob.bin"],
    )

    out = _fork(stack, principal=ALICE, src_pack_id=src)
    assert out["status"] == "ok", out
    assert out["copied"]["sources"] == 1, out["copied"]

    exported_dst = stack["vector"].export_pack_vectors(out["pack_id"])
    uri_records = [r for r in exported_dst if r.get("uris")]
    assert len(uri_records) == 1, uri_records
    assert uri_records[0]["uris"] == "file:///legacy/blob.bin", uri_records


def _vec_backend_kind(vec: Any) -> str | None:
    """Mirrors `fork.py`'s own `_vec_backend`'s first element -- duplicated
    here (not imported, it is a private name) purely to decide whether T68
    is meaningful against the fixture's resolved backend."""
    return fork_mod._vec_backend(vec)[0]
