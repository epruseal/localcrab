"""쓰기 도중 SIGKILL 뒤의 고아 행: 다섯 파일 구조(대조군)와 단일 파일 (#671).

``scripts/probe_single_db_crash.py``가 자식 프로세스에서 실제 ``load_chunks``를 돌리고 지정한
지점에서 SIGKILL을 보낸다. 순수 sqlite 연결이 파일을 열어 고아를 센다. 배치 크기를 B(=8)라 한다.

중단 지점(정의는 스크립트 docstring과 같다):
- P0: doc 쓰기 0건 직후. P3: 3건 직후.
- PB: 첫 배치의 마지막 doc 쓰기 직후이고 commit 전.
- PN: 첫 배치 commit 직후이고 둘째 배치의 첫 doc 쓰기 직전(K = B).

거는 것:
- 단일 파일은 어느 지점에서도 고아가 없고 부분 커밋도 없다. 죽은 시점이 열린 트랜잭션 안이고 그 안에
  쌓인 쓰기가 있었음을 자식이 넘긴 ``signal_state``로 증명한다(공허 통과 방지).
- 대조군(다섯 파일)은 P0, P3에서 고아가 재현된다. 재현되지 않으면 시험이 검출력이 없다.
- 죽이지 않고 완주시킨 두 구조는 전량 쓰이고 고아가 없다(하네스 자체의 대조).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "probe_single_db_crash.py"
B = 8
ORPHAN_KEYS = ("vec_without_doc", "doc_without_vec", "fts_without_doc", "doc_without_fts")

# 지점 이름 -> (스크립트 인자)
POINTS = {
    "P0": ["--kill-after-docs", "0"],
    "P3": ["--kill-after-docs", "3"],
    "PB": ["--kill-after-last-doc"],
    "PN": ["--kill-after-docs", str(B)],
}


def _run(tmp_path: Path, layout: str, *extra: str) -> dict:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "--layout", layout, "--dir", str(tmp_path / layout),
         "--batches", "2", "--batch-size", str(B), *extra],
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


# (point) -> (five vec, five doc, five vec_without_doc), (single vec, single doc)
FIVE_EXPECTED = {"P0": (B, 0, B), "P3": (B, 3, B - 3), "PB": (B, B, 0), "PN": (2 * B, B, B)}
SINGLE_EXPECTED = {"P0": (0, 0), "P3": (0, 0), "PB": (0, 0), "PN": (B, B)}
# 자식이 신호 직전에 자기 연결에서 본 (벡터 행 수, doc 행 수)
SIGNAL_SINGLE = {"P0": (B, 0), "P3": (B, 3), "PB": (B, B), "PN": (2 * B, B)}


@pytest.mark.parametrize("point", ["P0", "P3", "PB", "PN"])
def test_control_five_files(tmp_path, point):
    result = _run(tmp_path, "five", *POINTS[point])
    assert result["killed"] is True
    vec, doc, orphans = FIVE_EXPECTED[point]
    assert (result["vec_rows"], result["doc_rows"], result["vec_without_doc"]) == (vec, doc, orphans)
    assert result["signal_state"]["in_transaction"] is False
    if point in ("P0", "P3"):
        assert result["vec_without_doc"] > 0, "대조군이 고아를 재현하지 못했다: 시험 결함"


@pytest.mark.parametrize("point", ["P0", "P3", "PB", "PN"])
def test_single_file_leaves_no_orphans_and_died_inside_an_open_transaction(tmp_path, point):
    result = _run(tmp_path, "single", *POINTS[point])
    assert result["killed"] is True
    assert (result["vec_rows"], result["doc_rows"]) == SINGLE_EXPECTED[point]
    assert all(result[k] == 0 for k in ORPHAN_KEYS), {k: result[k] for k in ORPHAN_KEYS}
    assert result["integrity"] == "ok"
    assert result["fts_integrity"] == "ok"
    state = result["signal_state"]
    assert state["in_transaction"] is True, "죽은 시점이 열린 트랜잭션 안이 아니다: 시험이 공허하다"
    assert (state["vec_in_txn"], state["doc_in_txn"]) == SIGNAL_SINGLE[point]


@pytest.mark.parametrize("layout", ["five", "single"])
def test_completed_run_writes_everything_without_orphans(tmp_path, layout):
    result = _run(tmp_path, layout, "--none")
    assert result["killed"] is False
    assert result["child_returncode"] == 0
    assert result["vec_rows"] == result["doc_rows"] == result["expected_rows"]
    assert all(result[k] == 0 for k in ORPHAN_KEYS)
