"""쓰기 경로 공통 가드 — ``LOCAL_DATA_DIR`` 실재 확인.

``os.environ.setdefault`` 는 write-path 에서 fail-open 이다: 낡은 값이 export 돼 있으면
조용히 다른 저장소에 쓴다. 그래서 쓰기 직전에 그 경로가 실재하는지 확인해야 하는데,
**스크립트 헤더마다 같은 3줄을 복사하는 방식은 새 쓰기 경로가 생길 때마다 빠진다**
(실측: 적재 함수를 직접 호출해 진입점의 가드를 건너뛰던 호출 스크립트가 3종 있었다).

그래서 정의는 이 모듈 하나뿐이고, **쓰기 함수 자신**이 호출한다 — 호출자가 잊어도 걸린다.
읽기 전용 도구는 호출하지 않는다(라이브가 없는 장비에서 통째로 죽으면 안 된다 — import
시점 가드가 실제로 읽기 전용 게이트 2종을 죽인 적이 있다).

**불변식 — 이 가드는 ``LOCAL_DATA_DIR`` 값을 어떤 방식으로도 변형하지 않는다.**
수용 집합은 정확히 ``Path(값).is_dir()`` 이다. ``expanduser``·``strip``·``resolve``·
``normpath``·``realpath``·``expandvars``·``normcase``·``is_symlink`` 관용은 전부
"개선"처럼 보이지만 이 가드에 한해서는 **약화**다 — 정규화는 예외 없이 수용 집합을 넓히고,
넓어진 만큼이 엉뚱한 저장소다. 그리고 ``os.environ`` 은 **정확히 한 번, 이 키로만** 읽는다:
다른 키로 폴백하거나 건너뛰기 백도어를 두면 값이 아니라 **출처**가 확대된다.
(이 문장을 테스트에만 두었더니 다음 사람이 이 파일만 보고 "개선"을 넣을 수 있었다 —
적대 검증 지적, 2026-08-10. 계약은 ``tests/test_pack_live_data.py`` 가 오라클과
소스 단언으로 강제한다.)

**설정 해석기가 아니다.** ``opencrab.config`` 의 ``local_data_dir`` 은 미설정 시 홈 파생
기본값을 만들고, ``opencrab.mcp.tools._lock_data_dir`` 은 없으면 ``os.makedirs`` 로
**만든다.** 둘 다 "일할 자리를 마련한다"는 목적이라 옳다. 이 가드는 정반대 목적이다 —
**대상 저장소가 이미 있어야 한다.** 비어 있는 새 디렉터리에 적재가 성공하면 그건 성공이
아니라 엉뚱한 곳에 쓴 것이다. 두 정책을 같은 패키지 안에 두게 됐으므로 여기 명시한다:
이 가드가 있는 경로에서 데이터 디렉터리를 자동 생성하는 helper 를 끌어 쓰면 가드는
**조용히 무력해진다.**
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def require_live_data(ctx: str = "") -> None:
    """LOCAL_DATA_DIR이 설정돼 있고 실재하는 디렉터리인지 확인. 아니면 즉시 중단."""
    suffix = f" [{ctx}]" if ctx else ""
    d = os.environ.get("LOCAL_DATA_DIR")
    if not d:
        sys.exit(f"LOCAL_DATA_DIR 미설정 — 쓰기 경로는 대상 저장소를 명시해야 한다{suffix}")
    # `is_dir()` 이 항상 bool 을 준다는 통념은 틀렸다 — 이름이 너무 길거나(ENAMETOOLONG)
    # 경로 해석이 실패하면 `OSError` 를 던진다. 그대로 두면 가드가 트레이스백으로 죽어
    # "PackSchemaError/SystemExit 만 나온다"는 계약이 깨지고, 운영자는 원인을 못 본다.
    # **답을 못 하는 경우는 거부**다 — 판정 불가를 통과로 바꾸는 것이 곧 수용 집합 확대다.
    try:
        ok = Path(d).is_dir()
    except OSError as exc:
        # 경로를 그대로 두 번 실으면 4천자 경로에서 메시지가 8천자가 되고, 정작 행동
        # 가능한 정보(`File name too long`)가 그 사이에 묻힌다(적대 검증 지적).
        # 앞뒤만 남기고 가운데를 접는다 — 어느 경로인지는 양끝으로 충분히 식별된다.
        shown = d if len(d) <= 140 else f"{d[:60]}…({len(d)}자)…{d[-60:]}"
        sys.exit(f"LOCAL_DATA_DIR 경로 없음: {shown} — 경로를 확인할 수 없다"
                 f"({type(exc).__name__}: {exc.strerror or exc}){suffix}")
    if not ok:
        sys.exit(f"LOCAL_DATA_DIR 경로 없음: {d} — 오타이거나 스테일 export다{suffix}")
