"""packaging/agent-plugin/tools -- Agent Plugins 1.0.0 저작 도구 모음.

빌더(build.py)·이중모드 검증기(validate.py)·환경 계약(env_contract.py)·레퍼런스
클라이언트(refclient.py)를 담는다. opencrab 런타임 wheel 에는 포함되지 않는다
(pyproject 의 packages.find 는 opencrab* 만 대상 -- 이 패키지는 저작 시점
도구일 뿐 배포 계약이 아니다). 테스트는 이 디렉터리의 부모를 sys.path 에
삽입해 `tools.validate` 형태로 임포트한다(레포 conftest.py 의 sys.path 삽입
관례를 그대로 연장한 것).
"""

from __future__ import annotations
