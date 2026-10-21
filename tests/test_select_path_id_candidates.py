"""경로 id 후보 선택 스크립트(#520)의 합성 fixture 시험이다."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import cleanup_openclaw_heartbeat_493 as cleanup  # noqa: E402
import select_path_id_candidates as selector  # noqa: E402


def _graph(data_dir: Path, rows: list[tuple[str, str, dict | str]]) -> None:
    with sqlite3.connect(data_dir / "graph.db") as conn:
        conn.execute("CREATE TABLE graph_nodes (node_type TEXT, node_id TEXT PRIMARY KEY, space_id TEXT, properties TEXT)")
        for node_type, node_id, props in rows:
            raw = props if isinstance(props, str) else json.dumps(props)
            conn.execute("INSERT INTO graph_nodes VALUES (?,?,?,?)", (node_type, node_id, "evidence", raw))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    stage = str(root.resolve()) + "/staging/run-1/"
    other_stage = str(root.resolve()) + "/staging/run-2/"
    _graph(root, [
        ("TextUnit", stage + "records.jsonl", {"pack_id": "pack-a"}),
        ("TextUnit", stage + "report.md", {"pack_id": "pack-a"}),
        ("TextUnit", other_stage + "records.jsonl", {"pack_id": "pack-a"}),
        ("TextUnit", stage + "notes.md", {"pack_id": "pack-a"}),  # basename 불일치
        ("TextUnit", stage + "summary.md", {"pack_id": "pack-b"}),  # 다른 팩
        ("Evidence", stage + "evidence.md", {"pack_id": "pack-a"}),  # 다른 타입
        ("TextUnit", stage + "broken.md", "not json"),  # 읽을 수 없는 properties
        ("TextUnit", "/elsewhere/records.jsonl", {"pack_id": "pack-a"}),  # 데이터 디렉터리 밖
        ("TextUnit", "chunk:abc", {"pack_id": "pack-a"}),
    ])
    return root


def _main(data_dir: Path, output: Path, *names: str, extra: list[str] | None = None) -> int:
    args = ["--local-data-dir", str(data_dir), "--pack-id", "pack-a", "--node-type", "TextUnit", "--output", str(output)]
    for name in names:
        args += ["--basename", name]
    return selector.main([*args, *(extra or [])])


def test_selects_only_matching_prefix_type_pack_and_basename(data_dir: Path, tmp_path: Path, capsys):
    before = (data_dir / "graph.db").read_bytes()
    output = tmp_path / "ids.json"
    assert _main(data_dir, output, "records.jsonl", "report.md", "summary.md", "evidence.md", "broken.md") == selector.EXIT_OK
    stage = str(data_dir.resolve()) + "/staging/"
    body = json.loads(output.read_text(encoding="utf-8"))
    assert body == {
        "pack_id": "pack-a",
        "node_type": "TextUnit",
        "ids": [stage + "run-1/records.jsonl", stage + "run-1/report.md", stage + "run-2/records.jsonl"],
    }
    summary = json.loads(capsys.readouterr().out)
    assert summary["count"] == 3
    assert summary["per_basename"] == {"records.jsonl": 2, "report.md": 1}
    # stdout의 digest는 정리 도구 manifest가 기록할 approved_ids_sha256과 같다.
    assert summary["ids_sha256"] == cleanup._ids_digest(body["ids"])
    assert (data_dir / "graph.db").read_bytes() == before


def test_output_is_valid_cleanup_input(data_dir: Path, tmp_path: Path):
    output = tmp_path / "ids.json"
    assert _main(data_dir, output, "records.jsonl") == selector.EXIT_OK
    selection = cleanup._load_approved_ids(output)
    assert selection.pack_id == "pack-a"
    assert selection.node_type == "TextUnit"
    assert len(selection.ids) == 2


def test_explicit_path_prefix_narrows_selection(data_dir: Path, tmp_path: Path):
    output = tmp_path / "ids.json"
    prefix = str(data_dir.resolve()) + "/staging/run-2/"
    assert _main(data_dir, output, "records.jsonl", extra=["--path-prefix", prefix]) == selector.EXIT_OK
    assert json.loads(output.read_text(encoding="utf-8"))["ids"] == [prefix + "records.jsonl"]


def test_no_match_exits_one_and_writes_nothing(data_dir: Path, tmp_path: Path):
    output = tmp_path / "ids.json"
    assert _main(data_dir, output, "absent.md") == selector.EXIT_NO_MATCH
    assert not output.exists()


def test_existing_output_is_never_overwritten(data_dir: Path, tmp_path: Path):
    output = tmp_path / "ids.json"
    output.write_text("keep", encoding="utf-8")
    assert _main(data_dir, output, "records.jsonl") == selector.EXIT_REFUSED
    assert output.read_text(encoding="utf-8") == "keep"


def test_missing_graph_db_is_refused_without_creating_it(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _main(empty, tmp_path / "ids.json", "records.jsonl") == selector.EXIT_REFUSED
    assert not (empty / "graph.db").exists()


def test_prefix_bounds_cover_exactly_the_prefix():
    low, high = selector._prefix_bounds("/data/")
    assert low == "/data/"
    assert high == "/data0"
    for node_id, inside in (("/data/x", True), ("/data/", True), ("/data0", False), ("/data-x", False), ("/datb", False)):
        assert (low <= node_id < high) is inside
