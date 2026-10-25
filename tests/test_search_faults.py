"""#542 fault injection harness: judge self test and S0 end to end.

Design: docs/issue-542-fault-injection.md, section 7.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts import search_faults as faults

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_judge_detects_every_planted_case():
    results = faults.selftest_cases()
    assert results, "selftest produced no cases"
    assert [(name, actual) for name, expected, actual in results if expected != actual] == []


def _run(args: list[str], tmp_path: Path, script: str) -> subprocess.CompletedProcess:
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(tmp))
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=1500,
    )


def test_s0_fault_run_injects_every_scenario(tmp_path):
    corpus = tmp_path / "corpus"
    generated = _run(["generate", "--preset", "S0", "--seed", "542", "--out", str(corpus)],
                     tmp_path, "search_corpus.py")
    assert generated.returncode == 0, generated.stdout + generated.stderr
    work = tmp_path / "work"
    loaded = _run(["load", "--corpus", str(corpus), "--work", str(work)], tmp_path, "search_baseline.py")
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr

    report_path = tmp_path / "faults.json"
    ran = _run(["run", "--corpus", str(corpus), "--work", str(work), "--run-root", str(tmp_path / "faults"),
                "--out", str(report_path)], tmp_path, "search_faults.py")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verdict"] == "COMPLETE", (report.get("reasons"), ran.stdout[-3000:], ran.stderr[-3000:])
    assert ran.returncode == 0
    values = {s["id"]: s["value"] for s in report["scenarios"]}
    assert set(values) == set(faults.SCENARIOS)
    # Product verdicts are observations (design 7); only the injection is asserted.
    assert all(v != "not_injected" for v in values.values()), values
    assert all(v != "unavailable" or sid == "D1b" for sid, v in values.items()), values
    for scenario in report["scenarios"]:
        assert (tmp_path / "faults" / scenario["id"] / "result.json").exists()
