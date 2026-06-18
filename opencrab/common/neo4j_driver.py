"""Neo4j driver construction helper.

Consolidates the ``GraphDatabase.driver(uri, auth=(user, password), **opts)``
boilerplate duplicated across scripts/ and stores/.

The ``GraphDatabase`` class is passed in by the caller rather than imported
here so that each call site keeps its own ``GraphDatabase`` symbol — this
preserves the existing import style (module-level vs in-function ``from neo4j
import GraphDatabase``) and the patch points the characterization tests rely on.
"""

from __future__ import annotations

from typing import Any


def make_driver(
    graph_database: Any,
    uri: str,
    user: str,
    password: str,
    **opts: Any,
) -> Any:
    """Build a Neo4j driver with ``auth=(user, password)`` plus any extra opts.

    ``graph_database`` is the ``neo4j.GraphDatabase`` class (or a stand-in);
    the caller supplies it from its own scope. Extra keyword options (e.g.
    ``fetch_size``, ``max_connection_lifetime``) are forwarded verbatim.
    """
    return graph_database.driver(uri, auth=(user, password), **opts)
