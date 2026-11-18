"""``scripts/bench_single_db.py``의 판정 규칙 (#671 시제품).

측정 자체(수 분이 걸리는 실데이터 질의)는 시험하지 않는다. 사전 등록한 판정 규칙만 합성 입력으로 건다:
- 같은 분포면 충족, 크게 느리면 미충족.
- 결과 지문이 다르면 그 종류는 무효이며 충족으로 읽히지 않는다.
- 3-hop 100 ms 게이트는 비율과 별개로 건다.
- 메모리 기반 파일시스템의 시험 디렉터리는 거부한다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench_single_db.py"
_spec = importlib.util.spec_from_file_location("bench_single_db", SCRIPT)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def _run_file(tmp_path: Path, name: str, graph_ms: list[float], fp: str = "aa") -> str:
    raw = {k: [] for k in bench.KINDS}
    raw["graph_bfs"] = graph_ms
    run = {"mode": "latency", "raw": raw,
           "fingerprints": {"graph_bfs": {"n1": [fp, 5, False]}}}
    path = tmp_path / name
    path.write_text("heavy-run: start\n" + json.dumps(run) + "\n", encoding="utf-8")
    return str(path)


def _judge(capsys, s: str, f1: str) -> dict:
    assert bench.main(["judge", "--s", s, "--f1", f1]) == 0
    return json.loads(capsys.readouterr().out)["kinds"]["graph_bfs"]


BASE = [10.0 + i * 0.1 for i in range(100)]  # n=100이면 p95도 판정한다


def test_same_distribution_is_met(tmp_path, capsys):
    got = _judge(capsys, _run_file(tmp_path, "s.json", BASE), _run_file(tmp_path, "f.json", BASE))
    assert got["status"] == "충족"
    assert got["grade_p50"] == "충족" and got["grade_p95"] == "충족"


def test_much_slower_single_file_is_not_met(tmp_path, capsys):
    slow = [x * 3 for x in BASE]
    got = _judge(capsys, _run_file(tmp_path, "s.json", slow), _run_file(tmp_path, "f.json", BASE))
    assert got["status"] == "미충족"


def test_result_mismatch_invalidates_the_kind(tmp_path, capsys):
    got = _judge(capsys, _run_file(tmp_path, "s.json", BASE, fp="bb"), _run_file(tmp_path, "f.json", BASE))
    assert got["status"].startswith("무효")


def test_three_hop_gate_applies_even_when_the_ratio_passes(tmp_path, capsys):
    slow = [120.0 + i for i in range(100)]
    got = _judge(capsys, _run_file(tmp_path, "s.json", slow), _run_file(tmp_path, "f.json", slow))
    assert got["grade_p50"] == "충족"
    assert got["grade_gate_100ms"] == "미충족" and got["status"] == "미충족"


def test_memory_backed_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bench.merge_tool, "fs_type", lambda _path: "tmpfs")
    with pytest.raises(bench.merge_tool.MergeError):
        bench.check_dirs(tmp_path)
