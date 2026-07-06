from __future__ import annotations

import json

import pytest

from opencrab.pack import export_neo4j_opencrab_ingest
from opencrab.pack.neo4j_export import _normalise_edge, _normalise_node


class FakeNeo4jStore:
    """Stands in for a real Neo4jStore — export_nodes()/export_edges() are
    the GraphStoreExtended methods export_neo4j_opencrab_ingest() now calls
    directly (see opencrab/stores/_graph_protocol.py); this fake mirrors
    their Cypher-native return shape rather than a run_cypher() call."""

    available = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int]] = []

    def export_nodes(self, pack_id: str | None, limit: int):
        self.calls.append(("export_nodes", pack_id, limit))
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

    def export_edges(self, pack_id: str | None, limit: int):
        self.calls.append(("export_edges", pack_id, limit))
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
    status = export_neo4j_opencrab_ingest(FakeNeo4jStore(), output, pack_id="bench-pack")

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
        export_neo4j_opencrab_ingest(Unavailable(), tmp_path / "out.jsonl")


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
