from __future__ import annotations

import json

import pytest

from opencrab.pack import export_neo4j_opencrab_ingest


class FakeNeo4jStore:
    available = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str | None]]] = []

    def run_cypher(self, cypher: str, params: dict[str, str | None]):
        self.calls.append((cypher, params))
        if "MATCH (n)" in cypher:
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
