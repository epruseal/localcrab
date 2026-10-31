import json
import os
import sys
from pathlib import Path

root, evidence, run_id, nonce = sys.argv[1:5]
sys.path.insert(0, root)
from scripts.search_baseline import Watchdog, watchdog_scope  # noqa: E402

if len(sys.argv) > 5:
    import scripts.search_baseline as baseline
    if sys.argv[5] in ("stderr-write", "stderr-flush", "both"):
        class BrokenStderr:
            def write(self, value):
                if sys.argv[5] in ("stderr-write", "both"):
                    raise OSError("stderr write")
            def flush(self):
                raise OSError("stderr flush")
        sys.stderr = BrokenStderr()
    if sys.argv[5] in ("marker-fails", "both"):
        def fail_marker(*args):
            raise OSError("marker")
        baseline._atomic_json = fail_marker
    watchdog = Watchdog("S0", "run", Path(evidence).parent / "abort.json", [])
    watchdog._condition = lambda: "test_timeout"
    watchdog.start()
    watchdog.join(timeout=5)
    raise SystemExit(7)

watchdog = Watchdog("S0", "run", Path(evidence).parent / "abort.json", [])
error = RuntimeError("diagnostic")
try:
    with watchdog_scope(watchdog):
        watchdog.op_start("diagnostic")
        try:
            raise error
        finally:
            watchdog.op_end()
except RuntimeError as caught:
    identity = caught is error
else:
    identity = False
result = {"run_id": run_id, "nonce": nonce, "identity": identity,
          "state": watchdog.state, "alive": watchdog.is_alive(), "slots": dict(watchdog._ops)}
path = Path(evidence)
temp = path.with_suffix(".tmp")
with temp.open("x", encoding="utf-8") as stream:
    json.dump(result, stream)
    stream.flush()
    os.fsync(stream.fileno())
temp.rename(path)
