"""Read-path pack scope derivation (#147, execution 4 of #143).

This module owns the ONE answer to "which packs may this principal read
right now". Everything on a read path derives its filter from here, and
the type it hands back is a concrete ``frozenset[str]`` -- there is no
value this module can return that means "no filter at all". That
unrepresentability is the whole point of #143 invariant 3.

Two states, and they are NOT the same thing:

- ``None`` -- "no filter". Only legacy, non-authorization callers may use
  it (``opencrab/ontology/rebac.py``'s policy traversal, the QA
  benchmarks). Nothing in this module ever produces it.
- ``frozenset()`` / ``[]`` -- "nothing is readable". A principal who owns
  no pack and where no public pack exists gets exactly this, and every
  scoped read must return zero rows for it.

Conflating the two is the fail-open this execution exists to close: the
store helpers used to treat an empty pack set as "pass everything", so a
brand-new user with no packs would have seen the entire corpus.

Signatures mirror ``opencrab/auth.py`` and ``opencrab/pack/ownership.py``:
``sql`` (a ``SQLStore``) first, each function issuing its own short-lived
connection.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencrab.auth import Principal

logger = logging.getLogger(__name__)


def read_scope(sql: Any, principal: Principal) -> frozenset[str]:
    """Every pack_id ``principal`` may read, as a concrete set.

    Delegates to ``ownership.readable_pack_ids`` rather than re-deriving
    the predicate -- that function is the single authority for
    ``{owner_id = principal} ∪ {visibility != 'private'}`` and a second
    copy here would be free to drift from it.

    Not cached. Visibility changes (``pack_publish``) must take effect on
    the next read, and a per-process cache would keep serving a stale
    scope after a pack is un-published -- which is the failure direction
    that matters.

    Exceptions from the store are NOT swallowed. A caller that cannot
    determine its scope must fail; degrading to an empty set would hide
    the caller's own data behind what looks like a permission result, and
    degrading to "everything" would be the fail-open this module exists to
    prevent. Callers surface it as an error response, never as a filter.
    """
    from opencrab.pack.ownership import readable_pack_ids

    return frozenset(readable_pack_ids(sql, principal))


def narrow(
    scope: frozenset[str], requested: Iterable[str] | None
) -> tuple[list[str], bool]:
    """Intersect a caller's requested pack_ids with what they may read.

    Returns ``(effective, dropped_any)``:

    - ``requested`` empty/None -> ``(sorted(scope), False)``. "I did not
      name any pack" means "everything I may read", never "everything".
    - otherwise -> ``(sorted(set(requested) & scope), <anything dropped?>)``.

    The second element is only ever used to decide whether to attach a
    GENERIC warning. It deliberately does not say WHICH ids were dropped
    or WHY, because "no such pack" and "someone else's private pack" must
    be indistinguishable to the caller (#143 invariant 7). Both land in
    the same branch here, so there is no code path that could tell them
    apart even if a future caller wanted to.

    An empty return list means "match nothing", not "match everything".
    """
    if not requested:
        return sorted(scope), False
    asked = set(requested)
    kept = asked & scope
    return sorted(kept), len(kept) != len(asked)


class RegistryGraphMismatchError(RuntimeError):
    """The graph holds pack_ids the ``packs`` registry has never heard of.

    Raised at process start (see ``assert_registry_covers_graph``), never
    during a request.
    """


def assert_registry_covers_graph(sql: Any, graph: Any) -> None:
    """Refuse to start when the graph holds pack_ids missing from the registry.

    This is issue #147's deployment guard, and it is deliberately narrower
    than "detect a deployment that will hide its data". It refuses exactly
    one condition, the one the issue names: a ``pack_id`` exists on graph
    data but has no ``packs`` row, so read scoping will drop it while the
    operator has no way to see that from the outside. Re-running
    ``scripts/migrate_pack_ownership.py --apply`` genuinely fixes this on
    every backend: its registry stage enumerates the graph's packs and
    registers what it finds, so re-running it clears the condition. That is
    what makes the refusal message actionable rather than a dead end.
    (The script enumerates via ``list_packs``, so on Neo4j it shares that
    method's label restriction -- see below. An imported pack this guard now
    reports may therefore need the manual registration the deployment
    checklist describes rather than a bare re-run.)

    WHAT THIS DOES NOT DO, and why not (two earlier designs tried and both
    were wrong):

    - It does not refuse when rows carry NO pack_id at all. That state is
      un-fixable on pg/kuzu/docker, because ``migrate_pack_ownership``'s
      graph backfill only runs for ``STORAGE_MODE=local`` -- refusing on it
      would brick those deployments from first boot while telling the
      operator to run a script that cannot help them. On local it is
      reachable through ordinary use too: every write path that does not
      stamp a pack_id (see #148) would arm the refusal for the next
      restart.
    - It does not refuse on "registry is empty while the graph has nodes".
      That is both too weak and too strong: a registry holding a single
      row (the migration's own ``default`` pack) with an entirely
      pack-less graph slips past it, while a fresh install that writes one
      node before creating any pack trips it -- as does a Neo4j instance
      shared with another application, whose ``count_nodes()`` counts
      labels this system never wrote.

    The generalisation, recorded so the next attempt does not repeat it:
    "the data will be invisible" has no form that can be safely refused on
    the non-local backends. Refuse only what the migration can actually
    repair. A trustworthy gate would need a persisted "migration applied"
    marker, which needs its own table and coordination with ``opencrab
    init`` -- tracked as a follow-up, and #148 removes the ongoing source
    of pack-less rows anyway.

    Everything else is a WARNING line, below: an empty registry alongside a
    graph store in use. It carries no counts -- a count of "rows this
    deployment cannot see" has no single definition across the backends, and
    a number nobody can compare is worse than none.

    The graph side comes from ``graph.list_pack_ids()``, NOT from
    ``list_packs``. Two reasons, both of which made the earlier
    ``list_packs``-based version wrong:

    - ``Neo4jStore.list_packs`` matches ``(n:OpenCrabNode)``, but
      ``scripts/import_pack_graph_to_neo4j.py`` MERGEs each node under its
      own domain label. A whole imported pack missing from the registry was
      therefore absent from the comparison, the guard stayed silent, and
      scoped reads hid the pack -- exactly the outcome this exists to
      prevent.
    - ``list_packs`` groups by the bare JSON extraction, so a pack_id of
      ``""``/``0``/``false`` surfaced as a pack named ``"0"`` and could
      trigger a refusal over rows no read can reach.
      ``list_pack_ids`` applies the same truthiness rule as
      ``_node_pack_id``/``scope_pack_id``, so the set compared here is
      exactly the set scoped reads can resolve.

    Skipped entirely when the graph store is unavailable: the pack_id set
    cannot be enumerated, so the check cannot run. Skipping does not widen
    anyone's read scope -- that stays the registry-derived set either way.

    Cost: a ``SELECT DISTINCT`` over the node table and another over the
    edge table (or their per-backend equivalents), once per process (or per one-shot CLI command), not per request. It is
    not cheap: the same store measured a JSON-expression scan at ~439ms
    over 250k rows.
    """
    if not getattr(graph, "available", False):
        logger.info(
            "Pack registry/graph reconciliation skipped: graph store unavailable, "
            "so its pack_ids cannot be enumerated. Read scope is unaffected "
            "(it is derived from the packs registry either way)."
        )
        return

    from sqlalchemy import text

    with sql._engine.connect() as conn:
        registered = {r[0] for r in conn.execute(text("SELECT pack_id FROM packs")).fetchall()}

    graph_packs = graph.list_pack_ids()
    missing = sorted(graph_packs - registered)

    if missing:
        shown = ", ".join(missing[:10])
        more = f" (and {len(missing) - 10} more)" if len(missing) > 10 else ""
        raise RegistryGraphMismatchError(
            f"Refusing to start: {len(missing)} pack_id(s) exist on graph data but "
            f"have no row in the 'packs' registry: {shown}{more}.\n"
            "Read scoping resolves against the registry, so data in those packs "
            "would be invisible to every user, including its owner.\n"
            "Fix by running: python scripts/migrate_pack_ownership.py --apply\n"
            "(the migration is a hard prerequisite for deploying this version -- "
            "it is dry-run by default, so merging the code does not attribute "
            "anything on its own)."
        )

    if not registered:
        logger.warning(
            "Pack registry is empty while the graph store is in use. If this "
            "deployment has existing data, it has not been attributed yet and "
            "will be invisible to every user -- run "
            "'python scripts/migrate_pack_ownership.py --apply'. This is a "
            "warning, not the condition this check refuses on -- see its "
            "docstring for why that state cannot be safely refused."
        )
