"""Pack residue diagnostics (issue #407) -- read-only, evidence-only
per-axis inspection and classification for ONE already-authorized,
already-``ready`` registry pack_id.

``content_pack_list``'s ``node_count=0`` only confirms graph nodes are
absent (or unknown). It says nothing about ``doc_sources``, ``doc_nodes``,
or vector residue, so a ``ready`` row with ``node_count=0`` could be either
harmless registry hygiene (``ensure_default_pack``, ownership-migration
inserts, and generic ``create_pack`` all legally produce anchor-less
``ready`` rows -- see ``opencrab.pack.ownership``'s module docstring) or
genuine data-loss residue (a partial write that landed docs/vectors but
never confirmed its graph anchor). This module tells the two apart by
actually counting all five axes, never by guessing from one.

Design doc: the approved (round-5 PASS) design lives outside this repo, in
the orchestrating session's scratch space -- see PR/issue #407 for the
text. The evidence-only state/classification contract below is copied from
it verbatim; do not drift the two apart without re-reviewing both.

Per-axis result shape
----------------------
Every axis function returns ``{"state": ..., "count": ...}`` with:

- ``"known"``, ``count=n``  -- an EXACT count. ``n == 0`` is the only
  state/count pair that means "confirmed empty".
- ``"known_at_least"``, ``count=n`` -- a positive LOWER bound (vectors
  only, when a bounded backend read hits its cap). The true count is
  ``>= n`` but unknown beyond that.
- ``"unknown"``, ``count=None`` -- the axis is configured (its store type
  exists in this deployment) but this call could not count it: the store
  is unavailable, the connection failed, or the capability raised.
  NEVER collapsed to ``0`` -- see ``opencrab.pack.load.pack_live_counts``
  and ``opencrab.pack.fork._count_pack_vectors``'s docstrings for the
  None-vs-0 confusion this repeats a stance against.
- ``"not_applicable"``, ``count=None`` -- the axis's store type has no
  concept of this axis at all in this deployment (no write path exists),
  a STRUCTURAL fact, never a stand-in for "count failed" or "unavailable".

Classification
---------------
``classify()`` turns ``registry_ready`` plus the five axis results into
exactly one of four labels, using evidence only -- it never guesses a
cause or a repair action:

- ``not_candidate``: the registry row is absent/non-ready, OR the graph
  node count is known positive. Not a residue candidate at all.
- ``confirmed_empty``: ready, graph node count is known zero, every
  applicable axis is known zero, and no axis is unknown. Hygiene, not
  loss -- NOT an automatic deletion candidate.
- ``content_residue``: ready, graph node count is known zero, and some
  applicable axis is known positive or known_at_least. Potential loss --
  NOT an automatic attribution of cause.
- ``incomplete_observation``: ready, graph node count is not known
  positive, and the row is neither of the above two. This is the
  EXPLICIT COMPLEMENT of the other three, not a separate rule -- so an
  uncertain axis (graph unknown, or an applicable doc/vector axis
  unknown) never leaks into ``confirmed_empty``.

These four labels partition every possible ``registry_ready`` +
graph-node-state combination exhaustively and without overlap: absent/
non-ready and graph-known-positive go to ``not_candidate`` first; of the
remaining ready+graph-not-known-positive rows, graph-known-zero rows split
into ``content_residue``/``confirmed_empty`` by residue/unknown evidence,
and every row that is neither (including graph-unknown or
graph-not_applicable rows, regardless of what the other axes show) falls
to ``incomplete_observation``.
"""

from __future__ import annotations

import logging
from typing import Any

from opencrab.pack.fork import FORK_MAX_VECTORS, count_pack_vectors_bounded

logger = logging.getLogger(__name__)

# The four applicable-axis names classify() reasons about alongside
# graph_nodes (which drives not_candidate/confirmed_empty/content_residue
# on its own and is therefore handled separately, not folded into this
# tuple).
_OTHER_AXES = ("graph_edges", "doc_sources", "doc_nodes", "vectors")


def _axis(state: str, count: int | None) -> dict[str, Any]:
    return {"state": state, "count": count}


def graph_nodes_axis(graph: Any, pack_id: str) -> dict[str, Any]:
    """Strict ``properties.pack_id`` exact count, via the existing
    ``count_exported_nodes_scoped`` (issue #147's scope-safe predicate).
    NEVER ``count_exported_nodes(pack_id=...)`` -- that also matches
    ``source``/``source_id``, which would silently widen this axis past
    the strict-ownership contract #407's design requires."""
    if not hasattr(graph, "count_exported_nodes_scoped"):
        return _axis("not_applicable", None)
    try:
        return _axis("known", int(graph.count_exported_nodes_scoped([pack_id])))
    except Exception as exc:  # noqa: BLE001 -- unavailable/connection failure -> unknown, never 0
        logger.debug("graph_nodes_axis(%s): count failed: %s", pack_id, exc)
        return _axis("unknown", None)


def graph_edges_axis(graph: Any, pack_id: str) -> dict[str, Any]:
    """Strict AND-rule exact count (both endpoints' pack_id in scope AND
    the edge's own pack_id, if any, in scope), via
    ``count_exported_edges_scoped`` (added for #407, mirrors
    ``export_edges_scoped``'s predicate)."""
    if not hasattr(graph, "count_exported_edges_scoped"):
        return _axis("not_applicable", None)
    try:
        return _axis("known", int(graph.count_exported_edges_scoped([pack_id])))
    except Exception as exc:  # noqa: BLE001 -- unavailable/connection failure -> unknown, never 0
        logger.debug("graph_edges_axis(%s): count failed: %s", pack_id, exc)
        return _axis("unknown", None)


def doc_sources_axis(docs: Any, pack_id: str) -> dict[str, Any]:
    """``_doc_owner_pred``-equivalent exact count (pack_id priority,
    source fallback only when pack_id is absent), via
    ``count_sources_scoped`` (added for #407, mirrors
    ``list_sources_scoped``'s ownership predicate with no LIMIT)."""
    if not hasattr(docs, "count_sources_scoped"):
        return _axis("not_applicable", None)
    try:
        return _axis("known", int(docs.count_sources_scoped([pack_id])))
    except Exception as exc:  # noqa: BLE001 -- unavailable/connection failure -> unknown, never 0
        logger.debug("doc_sources_axis(%s): count failed: %s", pack_id, exc)
        return _axis("unknown", None)


def doc_nodes_axis(docs: Any, pack_id: str) -> dict[str, Any]:
    """Strict ``properties.pack_id`` exact count, NO source fallback (a
    deliberately narrower rule than ``doc_sources_axis`` -- see
    ``count_nodes_scoped``'s own docstring), via ``count_nodes_scoped``
    (added for #407)."""
    if not hasattr(docs, "count_nodes_scoped"):
        return _axis("not_applicable", None)
    try:
        return _axis("known", int(docs.count_nodes_scoped([pack_id])))
    except Exception as exc:  # noqa: BLE001 -- unavailable/connection failure -> unknown, never 0
        logger.debug("doc_nodes_axis(%s): count failed: %s", pack_id, exc)
        return _axis("unknown", None)


def vectors_axis(vec: Any, pack_id: str, cap: int = FORK_MAX_VECTORS) -> dict[str, Any]:
    """SQL/SQLAlchemy exact ``COUNT``, Chroma bounded
    ``get(limit=cap + 1, include=[])`` -- via ``count_pack_vectors_bounded``
    (added for #407, generalizes ``fork.py``'s private
    ``_count_pack_vectors`` for this public reuse). Reuses ``fork.py``'s
    own preflight cap (``FORK_MAX_VECTORS``) as the default bound so this
    diagnostic never materializes more ids than a fork already would."""
    state, count = count_pack_vectors_bounded(vec, pack_id, cap)
    return _axis(state, count)


def classify(registry_ready: bool, axes: dict[str, dict[str, Any]]) -> str:
    """Evidence-only four-way classification -- see module docstring for
    the full contract. ``axes`` must have all five keys (``graph_nodes``
    plus every name in ``_OTHER_AXES``) when ``registry_ready`` is True;
    ignored (never read) when it is False, so a caller short-circuiting on
    a failed ``readable_pack_ids`` lookup may pass an empty dict."""
    if not registry_ready:
        return "not_candidate"

    graph_nodes = axes["graph_nodes"]
    if graph_nodes["state"] == "known" and (graph_nodes["count"] or 0) > 0:
        return "not_candidate"
    graph_known_zero = graph_nodes["state"] == "known" and graph_nodes["count"] == 0

    def _is_residue(axis: dict[str, Any]) -> bool:
        if axis["state"] == "known_at_least":
            return True
        return axis["state"] == "known" and (axis["count"] or 0) > 0

    applicable = [axes[name] for name in _OTHER_AXES if axes[name]["state"] != "not_applicable"]
    any_residue = any(_is_residue(a) for a in applicable)
    any_unknown = any(a["state"] == "unknown" for a in applicable)

    if graph_known_zero and any_residue:
        return "content_residue"
    if graph_known_zero and not any_residue and not any_unknown:
        return "confirmed_empty"
    return "incomplete_observation"


def diagnose_pack(pack_id: str, *, graph: Any, docs: Any, vec: Any) -> dict[str, Any]:
    """Assemble all five axes for one ALREADY-AUTHORIZED, ALREADY-``ready``
    pack_id and classify the result. Callers own the authorization
    boundary (``readable_pack_ids``/``list_packs_for``) -- this function
    trusts ``pack_id`` completely and never checks ownership/visibility
    itself, so it must never be reachable for a pack_id the caller has not
    already confirmed readable and ready."""
    axes = {
        "graph_nodes": graph_nodes_axis(graph, pack_id),
        "graph_edges": graph_edges_axis(graph, pack_id),
        "doc_sources": doc_sources_axis(docs, pack_id),
        "doc_nodes": doc_nodes_axis(docs, pack_id),
        "vectors": vectors_axis(vec, pack_id),
    }
    return {
        "pack_id": pack_id,
        "classification": classify(True, axes),
        "axes": axes,
    }
