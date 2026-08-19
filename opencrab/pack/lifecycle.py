"""Registry lifecycle: anchor probing and stale-row repair (#170, execution 5b of #143).

The ``packs`` registry (``opencrab/pack/ownership.py``) and the stores that
hold a pack's content are not one transaction. A failure between them used to
leave two equally wrong outcomes: delete the registry row and risk erasing the
row of a pack whose content DID land (which makes
``read_scope.assert_registry_covers_graph`` refuse the next process start), or
keep it and let an incomplete pack look exactly like a complete one.

``packs.status`` closes that by making the in-between state representable.
This module owns the two things built on top of it:

- :func:`probe_anchor` / :func:`anchor_verdict` -- "did this pack's anchor
  actually land?", asked of the stores directly. ``pack_create`` uses it to
  re-read after an ambiguous commit (``store_write_succeeded`` is fail-closed,
  so a commit followed by a dropped connection reads as "not landed" even when
  it landed); the repair pass below uses it to decide a stale row's fate.
- :func:`repair_incomplete_packs` -- the offline pass over rows that never
  reached ``ready``.

ONE RULE GOVERNS THIS MODULE, and it is why none of the above has to prove a
completeness lemma: **nothing here deletes a registry row.** The only deletion
in the whole lifecycle is ``pack_create``'s branch that fails BEFORE the
content writer is ever called, where "no content exists" follows from control
flow rather than from a probe. Once the writer has run, a pack that cannot be
finalised is demoted to ``partial`` instead.

"Finalised" is narrower than "everything went well", and the two ``status``
fields in play make that easy to misread. A pack becomes ``ready`` the moment
its graph anchor is confirmed; failures AFTER that point -- an optional store
(docs/vector) rejecting the anchor, or individual nodes and edges failing
during ingest -- leave the registry at ``ready`` and are reported in the
*response*'s own ``status`` field as ``"partial"``. That response field and
this module's registry status are different things that happen to share a
word. Only a failure to confirm the anchor itself demotes the row.

That rule is not fastidiousness. It was reached by trying the alternative:
an earlier draft deleted on probe evidence and justified it with "the write
gate lets nothing but the anchor into a pack that is not ready", which was
false at the time -- ``opencrab/pack/load.py``'s chunk loader wrote to the doc
and vector stores outside ``write_gate.authorize``. #205 has since closed that
particular hole (the loader now authorizes on entry), but the rule stays,
because the reason that outlives it is the one probing cannot fix: any
emptiness check is a statement about ONE INSTANT, and a slow remote commit
landing a moment later invalidates it. Restoring a probe-based delete would
mean re-proving completeness against every writer that exists at that moment,
and then still being wrong about the next millisecond. Demotion needs no such
proof.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

# Per-store probe outcomes. `unknown` is deliberately NOT `absent`: a store we
# could not ask must never read as a store that answered "no" (fail-closed).
PROBE_PRESENT = "present"
PROBE_ABSENT = "absent"
PROBE_UNKNOWN = "unknown"

# Composite verdicts over the three per-store outcomes.
ANCHOR_GRAPH = "graph"
ANCHOR_OPTIONAL_ONLY = "optional-only"
ANCHOR_ABSENT = "absent"
ANCHOR_UNVERIFIABLE = "unverifiable"

_ANCHOR_SPACE = "resource"
_ANCHOR_NODE_TYPE = "Dataset"


def _probe_one(store: Any, method_name: str, args: tuple[Any, ...],
               path: tuple[str, ...], pack_id: str) -> str:
    """One store's answer about the anchor, as PRESENT/ABSENT/UNKNOWN.

    Mirrors ``write_gate._check_probes``' handling of malformed and missing
    stores, but answers a different question: that one asks "is this slot
    someone else's", this one asks "is this pack's own anchor here".

    Only two answers are ever positive knowledge, and both require having
    actually READ a non-empty string attribution:

    - it equals ``pack_id`` -> ``present``, this is our anchor
    - it is some other pack's -> ``absent``, our anchor is not in this slot

    Everything else is ``unknown``, including the case that looks most like
    an answer: a row that exists but carries no ``pack_id`` (a missing key,
    ``None``, ``""``). That is an UNATTRIBUTED row sitting at our anchor's
    identity, and whether it is ours is exactly what cannot be told. This is
    where the contrast with ``_check_probes`` matters -- that function reads
    the same falsy attribution as "unattributed legacy data, no conflict,
    let the write through", which is the fail-closed answer to ITS question
    and the fail-OPEN answer to this one. Reading it as ``absent`` here
    would demote a pack whose anchor may well have landed, and since
    promotion requires ``present``, nothing would ever bring it back.

    A truthy value that is not a string (``123``, a dict, a list) is a shape
    error, not evidence of another pack, so it lands in ``unknown`` too.

    ``result is None`` -- the store answering "no such row" rather than
    handing back a row -- is the one ``absent`` that needs no attribution
    read. Every graph backend returns exactly that on a miss, which is what
    keeps a genuinely empty ``creating`` pack resolvable by the repair pass.
    """
    if store is None or not getattr(store, "available", False):
        return PROBE_UNKNOWN
    method = getattr(store, method_name, None)
    if method is None:
        return PROBE_UNKNOWN
    try:
        result = method(*args)
    except Exception:  # noqa: BLE001 -- any failure is "cannot tell"
        return PROBE_UNKNOWN
    if result is None:
        return PROBE_ABSENT
    if not isinstance(result, Mapping):
        return PROBE_UNKNOWN
    value: Any = result
    for key in path:
        if not isinstance(value, Mapping):
            return PROBE_UNKNOWN
        value = value.get(key)
    if not isinstance(value, str) or not value:
        return PROBE_UNKNOWN
    return PROBE_PRESENT if value == pack_id else PROBE_ABSENT


def probe_anchor(graph: Any, docs: Any, vector: Any, pack_id: str) -> dict[str, str]:
    """Ask every store whether ``pack_id``'s anchor node is there.

    Returns ``{"graph": ..., "docs": ..., "vector": ...}`` with one of
    PROBE_PRESENT / PROBE_ABSENT / PROBE_UNKNOWN each. The per-store detail is
    returned rather than only the composite verdict because the repair pass
    reports it as its evidence -- an operator deciding what to do with a
    ``partial`` row needs to see which store holds what, not just a summary.

    The probe methods and their result shapes are the same three
    ``write_gate``'s identity guard uses, so a backend that satisfies one
    satisfies the other.
    """
    from opencrab.pack.ownership import anchor_node_id

    node_id = anchor_node_id(pack_id)
    return {
        "graph": _probe_one(
            graph, "get_node", (_ANCHOR_NODE_TYPE, node_id), ("pack_id",), pack_id
        ),
        "docs": _probe_one(
            docs, "get_node_doc", (_ANCHOR_SPACE, node_id),
            ("properties", "pack_id"), pack_id,
        ),
        "vector": _probe_one(
            vector, "get_by_id", (node_id,), ("metadata", "pack_id"), pack_id
        ),
    }


def anchor_verdict(probes: Mapping[str, str]) -> str:
    """Collapse :func:`probe_anchor`'s per-store answers into one verdict.

    - ``graph`` -- the graph store holds the anchor. The graph is the system
      of record ("anchor missing = no pack"), so this is the only verdict that
      means the pack really exists.
    - ``unverifiable`` -- the graph could not be asked. Fail-closed: not
      knowing must never read as either "landed" or "did not land".
    - ``optional-only`` -- the graph positively does not hold it but a doc or
      vector row does. Reachable because ``OntologyBuilder.add_node`` keeps
      writing to the optional stores after the graph write raises (it only
      skips them when the graph store is *unavailable*).
    - ``absent`` -- every store that answered said no, and none of the
      remaining ones could hold it either.

    Callers must not read ``absent`` as licence to delete the registry row;
    see this module's docstring.
    """
    graph = probes.get("graph")
    if graph == PROBE_PRESENT:
        return ANCHOR_GRAPH
    if graph != PROBE_ABSENT:
        return ANCHOR_UNVERIFIABLE
    optional = (probes.get("docs"), probes.get("vector"))
    if PROBE_PRESENT in optional:
        return ANCHOR_OPTIONAL_ONLY
    if PROBE_UNKNOWN in optional:
        return ANCHOR_UNVERIFIABLE
    return ANCHOR_ABSENT


def _parse_updated_at(value: Any) -> datetime | None:
    """Parse a registry row's ``updated_at`` into an aware UTC ``datetime``,
    or ``None`` if it cannot be judged.

    ``None`` covers three cases the caller must treat identically -- absent,
    unparseable, and (checked by the caller, not here) in the future --
    because a repair pass must never let a bad timestamp read as "old
    enough to act on". ``list_incomplete_packs`` -> ``_row_to_dict`` always
    hands this a ``str`` (SQLite's ``TEXT`` timestamps and PostgreSQL's
    ``TIMESTAMPTZ`` are both stringified there), but a bare ``datetime`` is
    accepted too so this stays usable if a caller ever passes a raw row.

    A naive result (SQLite's ``datetime('now')`` has no offset) is assumed
    UTC -- ``now_expr`` never writes anything else. An aware result (PG's
    ``TIMESTAMPTZ``, rendered with a ``+00:00`` offset) is left as-is.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def repair_incomplete_packs(
    sql: Any,
    graph: Any,
    docs: Any,
    vector: Any,
    *,
    older_than_seconds: int = 3600,
    apply: bool = False,
    promote: str | None = None,
) -> dict[str, Any]:
    """Offline repair pass over ``packs`` rows that never reached ``ready``
    (#170, design v4 §3.6) -- the engine behind ``opencrab packs
    repair-registry``.

    **This function never deletes a registry row, in any branch, under any
    argument combination.** It does not import ``delete_pack_row``. The only
    deletion in this design is ``pack_create``'s anchor-identity-conflict
    branch, which runs BEFORE the content writer is ever called (see this
    module's docstring and design v4 §3.0) -- a control-flow guarantee this
    function has no access to, since every row it looks at is, by
    definition, one where SOME attempt already ran. The only outcomes here
    are promotion (``ready``), demotion (``partial``), and reporting.

    **Age is judged in Python, not SQL.** ``updated_at`` is SQLite ``TEXT``
    (``datetime('now')``, UTC, naive) on one backend and PostgreSQL
    ``TIMESTAMPTZ`` on the other; a SQL-side age comparison would have to
    branch on dialect the way ``now_expr`` already does for writes, and the
    candidate-row count is structurally tiny (a healthy deployment holds a
    handful of ``creating``/``partial`` rows at most -- see
    ``list_incomplete_packs``), so fetching everything and filtering here
    costs nothing and avoids a second dialect branch. Rows whose
    ``updated_at`` is ``None``, fails to parse, or names a time in the
    future are left untouched and reported as ``skipped (unknown age)`` --
    guessing an age for those would risk acting on a row that is not
    actually stale.

    **Clock skew's blast radius is bounded by the no-delete rule.** A clock
    that runs fast or slow shifts the age threshold, so a row could be acted
    on a little earlier or later than ``older_than_seconds`` strictly
    implies. That is tolerable here specifically because every action this
    function can take is non-destructive: the worst outcome of an early
    judgment is a ``creating`` row demoted to ``partial`` a bit sooner than
    it "should" have been, which a later run (or the anchor actually
    landing) can still promote to ``ready``. There is no action a bad clock
    can trigger that loses data.

    **``partial`` rows get no automatic remediation.** Reaching ``partial``
    already means something (the anchor-landed check, a prior repair pass,
    or the original ``pack_create`` call) decided this attempt did not
    complete -- automatically flipping it back to ``ready`` from a repair
    pass would be exactly the "second-guess the row that gave up on itself"
    move design v4 §9 rejects for ``pack_create``'s own reconciliation
    branch. A ``partial`` row is reported with its anchor-probe evidence
    (which store, if any, holds something under this pack_id) so an
    operator can decide; explicit promotion is available only through
    ``--promote``, which still requires a positive graph-anchor probe before
    acting. Removing a ``partial`` row entirely is out of scope -- that is
    ``pack_delete`` (a separate, not-yet-built tool; see this module's and
    ``delete_pack_row``'s docstrings).

    **A ``creating`` row with ``forked_from`` set follows a different rule
    (#201 §4-F).** It was reserved by ``pack_fork``, which lands its graph
    anchor FIRST and copies content after -- the opposite order from
    ``pack_create``, whose anchor write is its last step and therefore its
    completion proof. So a stale forked ``creating`` row is demoted to
    ``partial`` on age ALONE, independent of what the anchor probe says (an
    incomplete fork copy's anchor probes exactly as PRESENT as a complete
    one's), and it is never auto-promoted by this pass -- only ``pack_fork``
    itself ever promotes a fork it is running. The row still appears in
    ``rows`` either way; this pass never drops a row from the report just
    because its fate was decided differently.

    **Recovery for a failed fork is NOT promotion.** A ``partial`` row with
    ``forked_from`` set is refused by ``--promote`` -- and by
    ``promote_partial_pack`` directly -- with the explicit reason
    ``opencrab.pack.ownership.FORKED_PARTIAL_PROMOTE_REFUSAL`` names,
    because there is no anchor-probe (or any other) evidence available here
    that would make flipping it to ``ready`` sound: an incomplete fork copy
    and a complete one are indistinguishable by the anchor alone, and
    re-running the fork's own post-copy verification would need the source
    pack and the id-remap salt, neither of which outlives the ``pack_fork``
    call that generated them. The actual recovery procedure is ops
    ``delete_pack`` (frees the copied content) followed by the caller
    re-forking. Two limits on that procedure, precisely:

    - ``delete_pack`` (``opencrab/pack/load.py``) takes no ``sql`` handle,
      so it does not touch the registry -- the stranded ``partial`` row
      stays, and it keeps occupying the fork's preferred slug (``{src}-fork``
      by default). A re-fork requesting that same slug does not fail on
      this; it relies on ``begin_pack_creation``'s existing collision
      negotiation to land on a suffixed id instead, exactly as any other
      slug collision would.
    - ``older_than_seconds`` must be set larger than the longest fork this
      deployment expects to run. A healthy, still-copying fork whose row
      crosses that age threshold gets demoted mid-flight by this very pass
      (the rule two paragraphs up does not distinguish "still running" from
      "died") -- and if the retry takes just as long, it crosses the same
      threshold and gets demoted again, so the fork never converges to
      ``ready`` under a threshold shorter than its own runtime. Raising the
      threshold above the expected fork duration is what keeps an
      in-progress row under the age gate long enough to finish.

    Returns a JSON-serializable dict:

    - ``older_than_seconds``, ``apply``, ``checked_at`` -- the run's inputs
      and the wall-clock time age was judged against.
    - ``counts`` -- ``rows_examined``, ``creating``, ``partial``,
      ``promoted``, ``demoted``, ``skipped`` (all over the age-filtered
      pass; ``promote``'s single-pack action is counted separately in
      ``promote_result``).
    - ``rows`` -- one entry per incomplete row:
      ``{pack_id, owner_id, status, action, reason?, probes?, applied?}``.
      ``status`` reflects the row's status AFTER this call when ``apply`` is
      true and the transition actually happened; otherwise it is the status
      the row had going in.
    - ``promote_result`` -- ``None`` unless ``promote`` was given, else the
      single-pack outcome of that targeted promotion (see below).

    ``promote="<pack_id>"`` acts on exactly that pack, independent of the
    age threshold (an operator named it explicitly, so "is it old enough"
    does not apply). It only calls ``promote_partial_pack`` -- which only
    transitions ``partial`` -> ``ready`` -- and only when
    ``probe_anchor(...)["graph"] == PROBE_PRESENT`` for that pack_id;
    anything else (absent, unknown/unverifiable, or no such pack) is
    rejected with a reason and no write happens. This is deliberate even
    though ``promote_partial_pack``'s own WHERE clause already refuses to
    touch anything but a ``partial`` row: skipping the anchor check here
    would let an operator flip ``status`` to ``ready`` without the anchor
    that status is supposed to mean actually existing (design v4 §3.6,
    codex r2 P2-3). ``apply=False`` (the default) reports the plan without
    calling it.
    """
    if older_than_seconds < 0:
        # Not a wider window -- no window. Every row not dated in the future
        # compares as older than a negative threshold, so the pass would act
        # on a `creating` row a pack_create is holding right now and demote a
        # pack mid-creation. The age gate is the only thing keeping this pass
        # off in-flight rows. Checked here as well as in the CLI because the
        # CLI is not the only caller this function can ever have.
        raise ValueError(
            f"older_than_seconds must be >= 0, got {older_than_seconds}"
        )

    from opencrab.pack.ownership import (
        FORKED_PARTIAL_PROMOTE_REFUSAL,
        PACK_STATUS_CREATING,
        PACK_STATUS_PARTIAL,
        PACK_STATUS_READY,
        get_pack,
        list_incomplete_packs,
        mark_pack_partial,
        mark_pack_ready,
        promote_partial_pack,
    )

    now = datetime.now(UTC)
    rows = list_incomplete_packs(sql)

    counts = {
        "rows_examined": len(rows),
        "creating": 0,
        "partial": 0,
        "promoted": 0,
        "demoted": 0,
        "skipped": 0,
    }
    results: list[dict[str, Any]] = []

    for row in rows:
        pack_id = row["pack_id"]
        owner_id = row["owner_id"]
        status = row["status"]
        entry: dict[str, Any] = {
            "pack_id": pack_id,
            "owner_id": owner_id,
            "status": status,
        }
        if status == PACK_STATUS_CREATING:
            counts["creating"] += 1
        elif status == PACK_STATUS_PARTIAL:
            counts["partial"] += 1

        dt = _parse_updated_at(row.get("updated_at"))
        if dt is None or dt > now:
            entry["action"] = "skipped (unknown age)"
            entry["reason"] = "updated_at is missing, unparseable, or in the future"
            counts["skipped"] += 1
            results.append(entry)
            continue

        age_seconds = (now - dt).total_seconds()
        if age_seconds < older_than_seconds:
            entry["action"] = "skipped (too recent)"
            entry["reason"] = f"age {age_seconds:.0f}s < threshold {older_than_seconds}s"
            counts["skipped"] += 1
            results.append(entry)
            continue

        if status == PACK_STATUS_CREATING:
            probes = probe_anchor(graph, docs, vector, pack_id)
            entry["probes"] = probes
            graph_probe = probes.get("graph")
            if row.get("forked_from"):
                # #201 §4-F fix 1. This row was reserved by `pack_fork`, not
                # `pack_create` -- and the two have OPPOSITE relationships
                # between "anchor present" and "attempt complete".
                # `pack_create` writes its anchor LAST (after everything
                # else it needs has been validated), so a landed anchor IS
                # its completion criterion -- that is what the
                # PROBE_PRESENT branch below promotes on. `pack_fork`
                # writes its anchor FIRST and only then copies content, so
                # for a fork the anchor is merely evidence of the FIRST
                # write, never of completion. Falling through to the same
                # PROBE_PRESENT/PROBE_ABSENT branching below would auto-
                # promote a fork whose copy is still running or died
                # mid-copy -- and dying right after the anchor lands is
                # fork's ordinary failure mode, not a corner case, so this
                # is not a rare mistake to tolerate.
                #
                # A guard that only closed the PROBE_PRESENT promote branch
                # (leaving the PROBE_ABSENT demote branch as the sole
                # fallback) would still be wrong: a dead fork's anchor is
                # PRESENT (it was the first thing written), so that row
                # would never reach the demote branch either and would sit
                # in `creating` forever -- invisible to every read path,
                # unrecoverable by any promotion, since nothing but this
                # pass and `pack_fork` itself ever calls
                # `mark_pack_ready`/`mark_pack_partial`. So a forked
                # `creating` row demotes on age ALONE, independent of the
                # probe outcome, and never auto-promotes here -- promotion
                # of a fork is owned exclusively by the `pack_fork` call
                # that is copying it.
                #
                # This branch still runs the same demote CALL as the
                # PROBE_ABSENT branch below (not a `continue`): skipping the
                # row would drop it from `results` entirely, which makes a
                # dead fork LESS observable to the operator reading this
                # pass's report -- the opposite of what finding it is for.
                entry["action"] = "demote"
                if apply:
                    applied = mark_pack_partial(sql, pack_id, owner_id)
                    entry["applied"] = applied
                    if applied:
                        entry["status"] = PACK_STATUS_PARTIAL
                        counts["demoted"] += 1
            elif graph_probe == PROBE_PRESENT:
                entry["action"] = "promote"
                if apply:
                    applied = mark_pack_ready(sql, pack_id, owner_id)
                    entry["applied"] = applied
                    if applied:
                        entry["status"] = PACK_STATUS_READY
                        counts["promoted"] += 1
            elif graph_probe == PROBE_ABSENT:
                entry["action"] = "demote"
                if apply:
                    applied = mark_pack_partial(sql, pack_id, owner_id)
                    entry["applied"] = applied
                    if applied:
                        entry["status"] = PACK_STATUS_PARTIAL
                        counts["demoted"] += 1
            else:
                entry["action"] = "skipped (unverifiable)"
                entry["reason"] = (
                    "graph store probe was inconclusive (store unavailable, "
                    "probe method missing, the call raised, or a row was "
                    "returned whose pack_id could not be read) -- fail-closed, "
                    "no action taken"
                )
                counts["skipped"] += 1
        else:
            # PACK_STATUS_PARTIAL (the only other value list_incomplete_packs
            # can hand back, since it selects status <> 'ready'). No
            # automatic action -- see docstring.
            probes = probe_anchor(graph, docs, vector, pack_id)
            entry["probes"] = probes
            entry["action"] = "report only (no automatic remediation)"
            entry["reason"] = (
                "removing this row requires the pack_delete tool (separate "
                "issue); explicit promotion is available via --promote, "
                "which still requires a positive graph anchor probe"
            )

        results.append(entry)

    promote_result: dict[str, Any] | None = None
    if promote is not None:
        target = get_pack(sql, promote)
        probes = probe_anchor(graph, docs, vector, promote)
        promote_result = {"pack_id": promote, "probes": probes}
        if target is None:
            promote_result["action"] = "rejected (no such pack)"
        elif target["status"] != PACK_STATUS_PARTIAL:
            # Checked BEFORE planning, in both modes, because the plan a
            # dry-run prints has to be the operation an --apply would perform.
            # `promote_partial_pack`'s WHERE only matches `partial`, so
            # announcing "promote" for a `ready` or `creating` target would
            # advertise something that could never happen -- and in apply mode
            # the operator would get `applied: false` after being told the
            # opposite. A `creating` target is not a near-miss to nudge along
            # either: the unattended pass promotes those on its own when their
            # anchor is confirmed, and `--promote` exists for `partial` rows
            # precisely because those are the ones it refuses to touch.
            promote_result["action"] = "rejected (not a partial pack)"
            promote_result["reason"] = (
                f"--promote only promotes a {PACK_STATUS_PARTIAL!r} row; this "
                f"one is {target['status']!r}"
                + (
                    " -- the unattended pass promotes a confirmed 'creating' "
                    "row without being asked"
                    if target["status"] == PACK_STATUS_CREATING
                    else ""
                )
            )
        elif target.get("forked_from"):
            # #201 §4-F fix 3. Checked BEFORE the anchor-probe branch below,
            # and BEFORE `apply` is consulted, for the same "plan == apply"
            # reason as the status check above: `promote_partial_pack`
            # itself refuses a forked `partial` row outright (raises
            # `ValueError`, see its docstring) because the anchor-probe
            # gate that licenses every OTHER promotion here is vacuous for
            # a fork -- `pack_fork` writes its anchor before copying any
            # content, so an incomplete copy's anchor probes PRESENT just
            # as reliably as a complete one's does. If this rejection lived
            # only inside `promote_partial_pack`, a dry-run (`apply=False`)
            # would still walk past this `elif` chain into the `else`
            # branch and print action "promote" -- a plan `--apply` could
            # never actually perform, since the very next call would raise.
            # `get_pack`'s `_SELECT_COLS` already includes `forked_from`
            # (see `ownership.py`), so `target` (fetched once, above) has
            # what is needed here without a second query.
            #
            # Reason string is imported from `ownership.py`, not
            # re-spelled here, so this planning-time rejection and
            # `promote_partial_pack`'s own runtime rejection can never say
            # two different things about the same refusal.
            promote_result["action"] = "rejected (forked partial row)"
            promote_result["reason"] = FORKED_PARTIAL_PROMOTE_REFUSAL
        elif probes.get("graph") != PROBE_PRESENT:
            promote_result["action"] = "rejected (graph anchor not confirmed present)"
            promote_result["reason"] = (
                f"graph probe returned {probes.get('graph')!r}, not "
                f"{PROBE_PRESENT!r} -- refusing to flip status without proof "
                "the anchor exists"
            )
        else:
            promote_result["action"] = "promote"
            promote_result["owner_id"] = target["owner_id"]
            if apply:
                applied = promote_partial_pack(sql, promote, target["owner_id"])
                promote_result["applied"] = applied
                if applied:
                    promote_result["status"] = PACK_STATUS_READY
                else:
                    # promote_partial_pack's WHERE pins both `partial` and the
                    # owner, so False means one of those did not hold. Which
                    # one is not knowable from the return value, and the row
                    # may have moved again since -- so report the status read
                    # BEFORE the update and the status now, separately
                    # labelled, instead of asserting a cause. A fixed "it was
                    # not partial" would be a claim never checked, and quoting
                    # only the earlier read would misdescribe a row that has
                    # since changed.
                    current = get_pack(sql, promote)
                    promote_result["reason"] = (
                        f"no row transitioned: --promote requires a "
                        f"{PACK_STATUS_PARTIAL!r} row owned by the same owner. "
                        f"Status read before the update: {target['status']!r}; "
                        f"status now: "
                        f"{(current or {}).get('status', '<row gone>')!r}"
                    )

    return {
        "older_than_seconds": older_than_seconds,
        "apply": apply,
        "checked_at": now.isoformat(),
        "counts": counts,
        "rows": results,
        "promote_result": promote_result,
    }
