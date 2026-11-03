"""#596: run the fault harness with perturbed timing.

Usage: search_faults_timing_child_596.py <ro-delay seconds> <budget scale> <harness arguments...>

The parent harness reads child progress with ``ro_status`` and gives the
child a ``--max-seconds`` budget. A parent that stalls after a read leaves
the child time to run on, which the first value models. A faster runner
leaves a fixed budget larger relative to the work, which the second value
models. The wrapper changes only the parent process. The child processes
run at their own speed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import search_faults as faults  # noqa: E402

ro_delay, budget_scale = float(sys.argv[1]), float(sys.argv[2])

_ro_status = faults.ro_status


def slow_ro_status(directory: Path) -> dict:
    status = _ro_status(directory)
    time.sleep(ro_delay)
    return status


_run_child = faults.Scenario.run_child


def scaled_run_child(self: faults.Scenario, role: str, *args: str, **kw: object) -> faults.Child:
    argv = list(args)
    if "--max-seconds" in argv:
        at = argv.index("--max-seconds") + 1
        argv[at] = str(float(argv[at]) * budget_scale)
    return _run_child(self, role, *argv, **kw)


faults.ro_status = slow_ro_status
faults.Scenario.run_child = scaled_run_child
raise SystemExit(faults.main(sys.argv[3:]))
