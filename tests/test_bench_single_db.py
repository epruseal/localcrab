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
    source_s = Path(s)
    source_f = Path(f1)
    second_s = source_s.with_name("second-" + source_s.name)
    second_f = source_f.with_name("second-" + source_f.name)
    second_s.write_text(source_s.read_text(encoding="utf-8"), encoding="utf-8")
    second_f.write_text(source_f.read_text(encoding="utf-8"), encoding="utf-8")
    assert bench.main(["judge", "--s", s, str(second_s), "--f1", f1, str(second_f)]) == 0
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


def test_noisy_runs_are_excluded_from_judgment(tmp_path, capsys):
    valid_s = [_run_file(tmp_path, f"s{i}.json", BASE) for i in range(2)]
    valid_f = [_run_file(tmp_path, f"f{i}.json", BASE) for i in range(2)]
    noisy = tmp_path / "noisy.json"
    noisy.write_text(json.dumps({"noisy": True}), encoding="utf-8")
    assert bench.main(["judge", "--s", str(noisy), *valid_s,
                       "--f1", *valid_f, str(noisy)]) == 0
    assert json.loads(capsys.readouterr().out)["kinds"]["graph_bfs"]["status"] == "충족"


def test_fewer_than_two_valid_runs_is_inconclusive(tmp_path, capsys):
    s = _run_file(tmp_path, "s.json", BASE)
    f = _run_file(tmp_path, "f.json", BASE)
    assert bench.main(["judge", "--s", s, "--f1", f]) == 0
    assert json.loads(capsys.readouterr().out)["kinds"]["graph_bfs"]["status"] == "미결(유효 회차 부족)"


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("valid_count", [0, 1, 2])
def test_noise_and_valid_round_counts(tmp_path, capsys, position, valid_count):
    s_runs = [_run_file(tmp_path, f"s{i}.json", BASE) for i in range(valid_count)]
    f_runs = [_run_file(tmp_path, f"f{i}.json", BASE) for i in range(valid_count)]
    noisy = tmp_path / "noise.json"
    noisy.write_text(json.dumps({"noisy": True}), encoding="utf-8")
    for runs in (s_runs, f_runs):
        runs.insert(0 if position == "first" else len(runs), str(noisy))
    assert bench.main(["judge", "--s", *s_runs, "--f1", *f_runs]) == 0
    status = json.loads(capsys.readouterr().out)["kinds"]["graph_bfs"]["status"]
    assert status == {0: "미측정", 1: "미결(유효 회차 부족)", 2: "충족"}[valid_count]


def test_later_run_fingerprint_mismatch_is_invalid(tmp_path, capsys):
    s = [_run_file(tmp_path, "s1.json", BASE), _run_file(tmp_path, "s2.json", BASE, fp="bb")]
    f = [_run_file(tmp_path, f"f{i}.json", BASE) for i in range(2)]
    assert bench.main(["judge", "--s", *s, "--f1", *f]) == 0
    assert json.loads(capsys.readouterr().out)["kinds"]["graph_bfs"]["status"].startswith("무효")


def test_missing_fingerprints_cannot_pass(tmp_path, capsys):
    s = [_run_file(tmp_path, f"s{i}.json", BASE) for i in range(2)]
    f = [_run_file(tmp_path, f"f{i}.json", BASE) for i in range(2)]
    for filename in s + f:
        path = Path(filename)
        run = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        run["fingerprints"] = {}
        path.write_text(json.dumps(run), encoding="utf-8")
    assert bench.main(["judge", "--s", *s, "--f1", *f]) == 0
    assert json.loads(capsys.readouterr().out)["kinds"]["graph_bfs"]["status"].startswith("무효")


def test_noisy_f2_does_not_turn_into_no_control(tmp_path, capsys):
    s = [_run_file(tmp_path, f"s{i}.json", BASE) for i in range(2)]
    f = [_run_file(tmp_path, f"f{i}.json", BASE) for i in range(2)]
    noise = tmp_path / "noise.json"
    noise.write_text(json.dumps({"noisy": True}), encoding="utf-8")
    assert bench.main(["judge", "--s", *s, "--f1", *f, "--f2", str(noise)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["a_a_independent_copy"] is True
    assert result["kinds"]["graph_bfs"]["status"] == "미결(유효 회차 부족)"


def test_duplicate_run_file_is_rejected(tmp_path):
    s = _run_file(tmp_path, "s.json", BASE)
    f = _run_file(tmp_path, "f.json", BASE)
    with pytest.raises(ValueError, match="서로 다른 파일"):
        bench.main(["judge", "--s", s, s, "--f1", f])


def test_memory_backed_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bench.merge_tool, "fs_type", lambda _path: "tmpfs")
    with pytest.raises(bench.merge_tool.MergeError):
        bench.check_dirs(tmp_path)


def test_q1_bench_unknown(tmp_path, monkeypatch):
    """/proc/mounts를 읽지 못해도 owner 밖 벤치마크 경로는 거부하지 않는다."""
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == "/proc/mounts":
            raise OSError("mounts unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert bench.merge_tool.fs_type(tmp_path) == "unknown"
    bench.check_dirs(tmp_path)
