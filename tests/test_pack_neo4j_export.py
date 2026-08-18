from __future__ import annotations

import json

import pytest

from opencrab.pack import export_neo4j_opencrab_ingest
from opencrab.pack.neo4j_export import _normalise_edge, _normalise_node


class FakeNeo4jStore:
    """Stands in for a real Neo4jStore — export_nodes_scoped()/
    export_edges_scoped() are the GraphStoreExtended methods
    export_neo4j_opencrab_ingest() now calls directly (issue #147 §3.4(b):
    the plain export_nodes()/export_edges() pair's 3/5-way OR predicate is
    forgeable and is no longer used for authorization -- see
    opencrab/stores/_graph_protocol.py); this fake mirrors the ``_scoped``
    methods' Cypher-native return shape rather than a run_cypher() call."""

    available = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int]] = []

    def export_nodes_scoped(self, pack_ids: list[str], limit: int):
        self.calls.append(("export_nodes_scoped", pack_ids, limit))
        return [
            {
                "props": {
                    "id": "node:fire-risk",
                    "label": "내화성능 미달 위험",
                    "space": "claim",
                    "node_type": "Claim",
                    "pack_id": "bench-pack",
                    "evidence_refs": ["evidence:1"],
                },
                "labels": ["Claim"],
            }
        ]

    def export_edges_scoped(self, pack_ids: list[str], limit: int):
        self.calls.append(("export_edges_scoped", pack_ids, limit))
        return [
            {
                "source_props": {"id": "node:material", "space": "concept", "node_type": "Entity"},
                "source_labels": ["Entity"],
                "target_props": {"id": "node:law", "space": "policy", "node_type": "Policy"},
                "target_labels": ["Policy"],
                "rel_props": {"confidence": 0.93, "evidence_refs": ["evidence:2"]},
                "relation": "CONSTRAINS",
            }
        ]


def test_export_neo4j_opencrab_ingest_writes_nodes_edges_and_status(tmp_path) -> None:
    output = tmp_path / "neo4j" / "opencrab_ingest.jsonl"
    status = export_neo4j_opencrab_ingest(
        FakeNeo4jStore(), output, pack_id="bench-pack", scope=frozenset({"bench-pack"}),
    )

    assert status["nodes"] == 1
    assert status["edges"] == 1
    assert (tmp_path / "neo4j" / "export_status.json").exists()

    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["kind"] == "node"
    assert lines[0]["payload"]["id"] == "node:fire-risk"
    assert lines[1]["kind"] == "edge"
    assert lines[1]["payload"]["relation"] == "constrains"
    assert lines[1]["payload"]["evidence_refs"] == ["evidence:2"]


def test_export_neo4j_opencrab_ingest_requires_available_store(tmp_path) -> None:
    class Unavailable:
        available = False

    with pytest.raises(RuntimeError, match="Neo4j store is not available"):
        export_neo4j_opencrab_ingest(Unavailable(), tmp_path / "out.jsonl", scope=frozenset())


# --- preset= convenience: matches the equivalent individual-flag combination ---


def test_normalise_node_lenient_preset_matches_bare_defaults():
    row = {"props": {"id": "n1", "name": "Doc A"}, "labels": ["Document"]}
    assert _normalise_node(row, preset="lenient") == _normalise_node(row)
    assert _normalise_node(row, preset=None) == _normalise_node(row)


def test_normalise_node_strict_preset_matches_strict_copy_flags():
    row = {"props": {"id": "n1", "name": "Doc A"}, "labels": ["Document"]}
    assert _normalise_node(row, preset="strict") == _normalise_node(row, strict=True, copy=True)


def test_normalise_node_strict_preset_raises_on_missing_keys():
    with pytest.raises(KeyError):
        _normalise_node({}, preset="strict")


def test_normalise_node_explicit_flag_overrides_preset():
    row = {"props": {"id": "n1"}, "labels": ["Document"]}
    props = row["props"]
    out = _normalise_node(row, preset="strict", copy=False)
    # copy=False explicitly wins over the strict preset's copy=True.
    assert out["payload"]["properties"] is props


def test_normalise_edge_strict_preset_matches_individual_flags():
    erow = {
        "source_props": {"id": "s1", "space": "concept"},
        "target_props": {"id": "t1", "space": "policy"},
        "rel_props": {"confidence": 0.9},
        "relation": "CONSTRAINS",
        "source_labels": ["Entity"],
        "target_labels": ["Policy"],
    }
    assert _normalise_edge(erow, preset="strict") == _normalise_edge(
        erow, strict=True, copy=True, rel_endpoint_fallback=True
    )


def test_normalise_edge_lenient_preset_matches_bare_defaults():
    erow = {
        "source_props": {"id": "s1"},
        "target_props": {"id": "t1"},
        "rel_props": {},
        "relation": "R",
        "source_labels": [],
        "target_labels": [],
    }
    assert _normalise_edge(erow, preset="lenient") == _normalise_edge(erow)


def test_requested_pack_outside_the_scope_exports_nothing(tmp_path) -> None:
    """#147: the `narrow()` intersection inside the exporter.

    Deleting it let `--pack-id <someone else's pack>` export that pack's
    nodes and edges: the scoped store methods trust the list they are given,
    so this intersection is the only thing between the caller's request and
    the data. The pre-existing wiring test asserts `scope=frozenset()` is
    passed through, which cannot catch this -- an empty scope narrows
    everything away no matter what the code does.
    """
    from opencrab.pack import export_neo4j_opencrab_ingest

    store = FakeNeo4jStore()
    out = tmp_path / "ingest.jsonl"

    export_neo4j_opencrab_ingest(
        store, out, pack_id="pack-theirs", scope=frozenset({"pack-mine"})
    )

    # The assertion is on what reached the store, not on the row count:
    # FakeNeo4jStore returns canned rows regardless of its argument, which
    # is exactly why the count cannot show whether the filter was applied.
    # A real store returns nothing for an empty list.
    assert store.calls, "the exporter did not call the store at all"
    for _name, pack_ids, _limit in store.calls:
        assert pack_ids == [], "the caller's pack_id was not intersected with the scope"


def test_requested_pack_inside_the_scope_exports_it(tmp_path) -> None:
    """Positive control: the emptiness above is the intersection refusing."""
    from opencrab.pack import export_neo4j_opencrab_ingest

    store = FakeNeo4jStore()
    out = tmp_path / "ingest.jsonl"

    status = export_neo4j_opencrab_ingest(
        store, out, pack_id="pack-mine", scope=frozenset({"pack-mine"})
    )

    assert status["nodes"] > 0
    for _name, pack_ids, _limit in store.calls:
        assert pack_ids == ["pack-mine"]
    assert out.read_text(encoding="utf-8").strip()

