"""스위트 자신의 데이터 디렉터리 격리 계약 (`tests/conftest.py`).

**왜 별도 파일인가.** 이 계약은 `LOCAL_DATA_DIR` 이 세션 내내 임시 디렉터리를 가리킨다는
것인데, `test_pack_live_data.py` 는 `require_live_data` 를 시험하느라 autouse fixture 로
그 env 를 **지운다**. 남의 fixture 가 지배하는 모듈에 두면 계약이 그 fixture 를 검사하게
된다 — 격리 자체를 못 본다.

**왜 계약으로 거는가.** 격리는 `conftest.py` 모듈 최상단의 몇 줄이라 조용히 지워지거나
`setdefault` 로 되돌아가기 쉽다. 그러면 스위트가 다시 운영자의 실 데이터 디렉터리에 쓰는데
**테스트는 여전히 전부 초록이다.** 실제로 그 상태였고 두 적대 검증자가 각각 다른 방법으로
잡아냈다(2026-08-10): 한쪽은 OS 샌드박스로 그 경로를 막아 11건 PermissionError 를 냈고,
다른 쪽은 `HOME` 을 가짜로 두고 전체 스위트를 돌려 `write.lock` 1개 생성을 관측했다.

교정 후 재측정(같은 방법): 가짜 HOME 아래 생성 파일 **0개**,
`LOCAL_DATA_DIR` 을 export 한 상태에서도 그 경로 생성 파일 **0개**.
뒤엣것이 `setdefault` 였다면 뚫렸을 자리다 — 이 리포의 운영 워크플로가 그 env 를
export 한 채로 돌기 때문이다.
"""
from __future__ import annotations

import os
from pathlib import Path

from opencrab.mcp.tools import _lock_data_dir


def test_local_data_dir_points_at_a_throwaway_dir_not_home():
    got = os.environ.get("LOCAL_DATA_DIR")
    assert got, "conftest 가 LOCAL_DATA_DIR 을 안 걸었다 — 스위트가 실 디렉터리에 쓴다"
    p = Path(got).resolve()
    assert p != (Path.home() / ".local/share/localcrab").resolve(), \
        f"실 데이터 디렉터리를 그대로 쓰고 있다: {p}"
    assert "localcrab-test-data-" in p.name, \
        f"conftest 가 만든 임시 디렉터리가 아니다: {p} — 누가 덮어썼는지 확인하라"


def test_the_lock_helper_resolves_inside_that_dir():
    """격리가 **실제 락 경로**까지 닿는가. env 만 바꾸고 코드가 다른 경로를 쓰면 무의미하다."""
    assert os.path.realpath(_lock_data_dir()) == \
        os.path.realpath(os.environ["LOCAL_DATA_DIR"]), "락 경로가 격리를 벗어난다"


def test_the_override_is_unconditional_not_setdefault():
    """`setdefault` 로 되돌아가면 여기서 죽는다.

    `conftest.py` 소스를 읽어 `LOCAL_DATA_DIR` 을 **대입**으로 덮는지 확인한다.
    행동으로는 못 가른다 — 실행 시점에는 이미 값이 들어 있어 둘이 구분되지 않는다.
    """
    src = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    assert 'os.environ["LOCAL_DATA_DIR"] =' in src, (
        "conftest 가 LOCAL_DATA_DIR 을 대입으로 덮지 않는다 — setdefault 면 운영자 셸에 "
        "그 env 가 export 돼 있을 때 보호가 사라진다")
    assert 'setdefault("LOCAL_DATA_DIR"' not in src
