import json
import zipfile
from pathlib import Path

from opencrab.pack import assemble_pack_v1


def test_assemble_pack_v1_from_neo4j_ingest(tmp_path: Path):
    source = tmp_path / "stage"
    neo = source / "neo4j"
    neo.mkdir(parents=True)
    rows = [
        {"kind": "node", "payload": {"id": "node:a", "label": "A", "evidence_refs": []}},
        {"kind": "node", "payload": {"id": "node:b", "label": "B", "evidence_refs": []}},
        {"kind": "edge", "payload": {"id": "edge:ab", "from_id": "node:a", "to_id": "node:b", "relation": "mentions", "evidence_refs": []}},
    ]
    (neo / "opencrab_ingest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    out = tmp_path / "pack.zip"

    status = assemble_pack_v1(source, out, pack_id="pack-test", title="Pack Test")

    assert status["status"] == "ok"
    assert status["nodes"] == 2
    assert status["edges"] == 1
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "graph/nodes.jsonl" in names
        assert "graph/edges.jsonl" in names
        assert "neo4j/opencrab_ingest.jsonl" in names
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format_version"] == "opencrab-pack-v1"
    assert manifest["counts"]["nodes"] == 2
