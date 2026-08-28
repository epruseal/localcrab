"""Exact-selection issue #80 test runner."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.verify_issue80 import (
    ROOT,
    create_evidence_child,
    load_manifest,
    manifest_nodeids,
)


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="run the exact issue #80 manifest")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--junit-name", default="issue80.xml")
    return parser.parse_args(argv)


def _selection_file(child: Path, nodeids: tuple[str, ...]) -> Path:
    path = child / "selection.txt"
    path.write_text("\n".join(nodeids) + "\n", encoding="utf-8")
    return path


def _collected_ids(output: str, nodeids: tuple[str, ...]) -> set[str]:
    return {nodeid for nodeid in nodeids if nodeid in output}


def _junit_failures(path: Path) -> list[str]:
    root = ET.parse(path).getroot()
    failures: list[str] = []
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            failures.append(case.attrib.get("name", "unknown"))
    return failures


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("RUN_DIR"):
        raise RuntimeError("RUN_DIR must be empty; the wrapper owns the evidence child")
    args = _args(argv)
    if Path(args.junit_name).name != args.junit_name:
        raise ValueError("junit name must be a basename")
    manifest = load_manifest(args.manifest)
    child = create_evidence_child(args.base_dir)
    print(f"RUN_DIR={child}", flush=True)
    nodeids = manifest_nodeids(manifest)
    selection = _selection_file(child, nodeids)
    junit = child / args.junit_name
    command = [sys.executable, "-m", "pytest", "-q", f"@{selection}"]
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "--override-ini", "addopts=", f"@{selection}"],
        cwd=ROOT, text=True, capture_output=True,
    )
    (child / "collection.raw.txt").write_text(collect.stdout + collect.stderr, encoding="utf-8")
    if collect.returncode != 0:
        print(collect.stdout + collect.stderr, file=sys.stderr)
        return collect.returncode
    collected = _collected_ids(collect.stdout, nodeids)
    if collected != set(nodeids):
        missing = sorted(set(nodeids) - collected)
        raise RuntimeError(f"manifest collection mismatch; missing={missing}")
    result = subprocess.run(
        command + [f"--junitxml={junit}"], cwd=ROOT, text=True, capture_output=True,
    )
    (child / "pytest.raw.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        print(result.stdout + result.stderr, file=sys.stderr)
        return result.returncode
    if not junit.is_file():
        raise RuntimeError("pytest did not produce the required JUnit artifact")
    failures = _junit_failures(junit)
    if failures:
        raise RuntimeError(f"JUnit contains failed/skipped cases: {failures}")
    print(f"ISSUE80 PASS collected={len(collected)} skipped=0 failed=0 xfailed=0 xpassed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
