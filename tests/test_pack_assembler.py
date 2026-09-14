import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from opencrab.pack import assemble_pack_v1
from opencrab.pack.assembler import _quality_report, _read_jsonl, _write_jsonl


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


@pytest.mark.parametrize("pack_id", ["../evil", "/abs/path", "a/b"])
def test_assemble_pack_v1_rejects_invalid_pack_id(tmp_path: Path, pack_id: str):
    source = tmp_path / "stage"
    source.mkdir()
    out = tmp_path / "pack.zip"

    with pytest.raises(ValueError, match="invalid pack_id"):
        assemble_pack_v1(source, out, pack_id=pack_id, title="Pack Test")


def test_assemble_pack_v1_accepts_valid_pack_id(tmp_path: Path):
    source = tmp_path / "stage"
    source.mkdir()
    out = tmp_path / "pack.zip"

    status = assemble_pack_v1(source, out, pack_id="my-pack_v1", title="Pack Test")

    assert status["status"] == "ok"
    assert status["pack_id"] == "my-pack_v1"


# --- _quality_report: metrics must reflect real pack state, not hardcoded 1.0 ---


def test_quality_report_healthy_pack_scores_high_but_real():
    nodes = [{"id": "n1", "evidence_refs": ["e1"]}, {"id": "n2", "evidence_refs": ["e1"]}]
    edges = [{"from_id": "n1", "to_id": "n2", "evidence_refs": ["e1"]}]
    evidence = [{"id": "e1"}]

    report = _quality_report(nodes, edges, evidence)

    assert report["status"] == "pass"
    assert report["summary"]["evidence_coverage"] == 1.0
    assert report["summary"]["node_evidence_integrity"] == 1.0
    assert report["summary"]["edge_evidence_integrity"] == 1.0
    assert report["summary"]["relationship_evidence_coverage"] == 1.0
    assert report["summary"]["graph_reference_integrity"] == 1.0
    assert report["counts"]["missing_evidence_refs"] == 0
    assert report["counts"]["broken_edges"] == 0
    assert report["counts"]["orphan_nodes"] == 0


def test_quality_report_malformed_pack_reflects_real_degradation():
    # evidence index is empty (nothing to resolve refs against) and one edge
    # points at a node that doesn't exist -- both should show up as real
    # failures, not the old hardcoded 1.0.
    nodes = [{"id": "n1", "evidence_refs": ["missing-e"]}]
    edges = [{"from_id": "n1", "to_id": "ghost", "evidence_refs": ["missing-e"]}]
    evidence: list[dict] = []

    report = _quality_report(nodes, edges, evidence)

    assert report["status"] == "warn"
    assert report["summary"]["node_evidence_integrity"] == 0.0
    assert report["summary"]["edge_evidence_integrity"] == 0.0
    assert report["summary"]["graph_reference_integrity"] == 0.0
    assert report["counts"]["missing_evidence_refs"] == 2
    assert report["counts"]["broken_edges"] == 1
    assert report["checks"]["broken_edges"] == "fail"
    assert report["checks"]["evidence_refs"] == "warn"


def test_quality_report_empty_pack_does_not_falsely_report_perfect_scores():
    report = _quality_report([], [], [])

    assert report["status"] == "pass"
    assert report["counts"]["missing_evidence_refs"] == 0
    assert report["counts"]["broken_edges"] == 0
    assert report["counts"]["orphan_nodes"] == 0
    # Nothing to measure -> undetermined (None), never a fabricated 1.0.
    for key in (
        "evidence_coverage",
        "node_evidence_integrity",
        "edge_evidence_integrity",
        "relationship_evidence_coverage",
        "graph_reference_integrity",
    ):
        assert report["summary"][key] is None


def test_quality_report_orphan_node_is_detected():
    nodes = [{"id": "n1"}, {"id": "orphan"}]
    edges = [{"from_id": "n1", "to_id": "n1", "evidence_refs": []}]

    report = _quality_report(nodes, edges, [])

    assert report["counts"]["orphan_nodes"] == 1
    assert report["checks"]["orphan_nodes"] == "warn"


def test_assemble_pack_v1_manifest_quality_reflects_broken_edges(tmp_path: Path):
    source = tmp_path / "stage"
    neo = source / "neo4j"
    neo.mkdir(parents=True)
    rows = [
        {"kind": "node", "payload": {"id": "node:a", "label": "A", "evidence_refs": []}},
        {"kind": "edge", "payload": {"id": "edge:ab", "from_id": "node:a", "to_id": "node:missing", "relation": "mentions", "evidence_refs": []}},
    ]
    (neo / "opencrab_ingest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    out = tmp_path / "pack.zip"

    assemble_pack_v1(source, out, pack_id="pack-broken", title="Broken")

    with zipfile.ZipFile(out) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        report = json.loads(archive.read("quality/report.json"))

    assert manifest["quality"]["graph_reference_integrity"] != 1.0
    assert report["counts"]["broken_edges"] == 1
    assert report["status"] == "warn"


def test_quality_report_no_longer_emits_hardcoded_placeholder_metrics():
    # These were previously hardcoded constants unrelated to the actual pack
    # (parsing_completeness=1.0, chunk_coverage=1.0, multihop_path_coverage=1.0,
    # ocr_completeness=None, clip_coverage=None) -- uncomputable from
    # nodes/edges/evidence alone, so they must not appear as fake scores.
    report = _quality_report([], [], [])
    for key in ("parsing_completeness", "ocr_completeness", "clip_coverage", "chunk_coverage", "multihop_path_coverage"):
        assert key not in report["summary"]


# --- #382: _read_jsonl/_write_jsonl 은 레코드 경계를 LF 하나로만 인식한다 ---


def test_read_jsonl_structural_cr_is_not_a_record_boundary(tmp_path: Path):
    """JSON 문자열 값 안의 구조적 CR 은 레코드 경계가 아니다.

    수정 전에는 `path.read_text(encoding="utf-8").splitlines()`가 CR 을 줄 경계로
    삼아 레코드를 조용히 둘로 쪼갰다(#382)."""
    p = tmp_path / "rows.jsonl"
    with p.open("w", encoding="utf-8", newline="") as f:
        f.write('{"a":\r1}\n{"b":2}\n')
    assert _read_jsonl(p) == [{"a": 1}, {"b": 2}]


def test_read_jsonl_unicode_line_separator_in_string_value_is_not_a_record_boundary(tmp_path: Path):
    """U+2028(LINE SEPARATOR)이 JSON 문자열 값 안에 리터럴로 있어도 레코드를 쪼개지 않는다.

    `str.splitlines()`는 U+2028 도 줄 경계로 잡는다. `ensure_ascii=False`로 직렬화해야
    이스케이프 시퀀스가 아니라 리터럴 문자로 남는다(#382)."""
    p = tmp_path / "rows.jsonl"
    row = json.dumps({"id": "n1", "note": "a b"}, ensure_ascii=False)
    assert "\\u2028" not in row, "ensure_ascii=False 인데도 이스케이프됐다면 테스트 전제가 깨진다"
    p.write_text(row + "\n" + json.dumps({"id": "n2"}) + "\n", encoding="utf-8", newline="")
    assert _read_jsonl(p) == [{"id": "n1", "note": "a b"}, {"id": "n2"}]


def test_read_jsonl_unicode_paragraph_separator_in_string_value_is_not_a_record_boundary(tmp_path: Path):
    """U+2029(PARAGRAPH SEPARATOR)도 U+2028과 독립으로 고정한다(둘 중 하나만 고치는
    변형 구현이 나오면 이 케이스만 걸릴 수 있어 별도 테스트로 둔다, #382)."""
    p = tmp_path / "rows.jsonl"
    row = json.dumps({"id": "n1", "note": "a b"}, ensure_ascii=False)
    assert "\\u2029" not in row, "ensure_ascii=False 인데도 이스케이프됐다면 테스트 전제가 깨진다"
    p.write_text(row + "\n" + json.dumps({"id": "n2"}) + "\n", encoding="utf-8", newline="")
    assert _read_jsonl(p) == [{"id": "n1", "note": "a b"}, {"id": "n2"}]


def test_write_jsonl_uses_lf_newline_argument(tmp_path: Path):
    """`os.linesep == "\\n"` 인 Linux 에서는 바이트 비교로 역변이를 검출할 수 없다.
    `Path.write_text` 호출 인자에 `newline="\\n"`이 실제로 전달되는지 직접 확인한다."""
    p = tmp_path / "rows.jsonl"
    with patch("pathlib.Path.write_text", autospec=True) as mock_write_text:
        _write_jsonl(p, [{"id": "n1"}])
    assert mock_write_text.call_args.kwargs.get("newline") == "\n"
