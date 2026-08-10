"""Ensure the tests directory is importable so sibling helper modules
(e.g. ``_vec_helpers``) can be imported by test modules regardless of pytest's
import mode."""

import atexit
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# 테스트는 호스트의 운영 env 파일을 읽지 않는다.
#
# Settings 는 표준 위치(~/.openclaw/localcrab-kure.env)를 자동으로 읽는다
# (config._default_env_files, 2026-08-04). 운영 배선이 실행 경로마다 갈리던
# 결함을 닫기 위한 것인데, 그대로 두면 이 머신에서만 "기본값" 테스트가 깨진다
# — 실제로 OPENAI_API_BASE(단일 base 기대)와 VECTOR_BACKEND(pg 모드 기대)가
# 운영값에 덮여 3건이 실패했다. 호스트마다 결과가 달라지는 테스트는 게이트로서
# 쓸모가 없으므로 세션 전체를 격리한다.
#
# fixture 가 아니라 모듈 최상단인 이유: get_settings() 는 lru_cache 이고 일부
# 모듈은 임포트 시점에 Settings 를 만든다. fixture 로는 늦는다.
# setdefault 이므로 호출자가 LOCALCRAB_ENV_FILE 을 명시하면 그대로 존중된다
# (표준 env 로딩 자체를 검증하는 테스트가 이 경로로 실제 파일을 지정한다).
os.environ.setdefault("LOCALCRAB_ENV_FILE", os.devnull)

# 테스트는 호스트의 **실 데이터 디렉터리**에도 쓰지 않는다.
#
# `LOCAL_DATA_DIR` 이 없으면 `Settings` 가 `~/.local/share/localcrab` 로 폴백하고,
# `opencrab/mcp/tools/__init__.py::_write_lock()` 이 거기에 `write.lock` 을 만든다.
# 실측(2026-08-10, `HOME` 을 가짜로 두고 전체 스위트 실행): 가짜 HOME 아래
# `.local/share/localcrab/write.lock` **1개 생성**. 운영자 HOME 이면 그것이 라이브다.
# 적대 검증도 독립적으로 잡았다 — OS 샌드박스로 그 경로를 막자 11건이 PermissionError 로
# 실패했다. 즉 그때까지의 초록은 **실 디렉터리에 쓸 수 있어서** 났던 것이다.
#
# **`setdefault` 가 아니라 항상 덮는다.** 이 리포의 운영 워크플로는 셸에
# `LOCAL_DATA_DIR` 을 export 한 채로 돈다 — `setdefault` 면 바로 그 상황에서 보호가
# 사라진다. 실 디렉터리를 겨냥하려면 `OPENCRAB_TEST_USE_REAL_DATA_DIR=1` 로 **명시**해야
# 한다(선언은 값이 아니라 의도여야 한다).
#
# fixture 가 아니라 모듈 최상단인 이유는 위 `LOCALCRAB_ENV_FILE` 과 같다 —
# `get_settings()` 는 lru_cache 이고 일부 모듈은 임포트 시점에 Settings 를 만든다.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="localcrab-test-data-")
if os.environ.get("OPENCRAB_TEST_USE_REAL_DATA_DIR") != "1":
    os.environ["LOCAL_DATA_DIR"] = _TEST_DATA_DIR
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)



@pytest.fixture(scope="session", autouse=True)
def _pg_test_db_tripwire():
    """세션 전체 하드 게이트.

    OPENCRAB_PG_TEST_URL이 설정되어 있는데 가리키는 데이터베이스 이름이
    ``_test``로 끝나지 않으면, 실수로 운영/개발 DB를 겨냥한 채 파괴적 PG
    테스트를 실행하는 사고를 막기 위해 세션 전체를 즉시 중단한다.
    (기존 tests/test_pg_env.py 의 테스트 단위 tripwire를 세션 단위로 승격.)

    env가 설정되지 않은 경우 아무 임포트도 하지 않고 즉시 반환한다.
    """
    pg_url = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not pg_url:
        return
    db_name = pg_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not db_name.endswith("_test"):
        pytest.exit(
            f"[PG tripwire] OPENCRAB_PG_TEST_URL이 가리키는 데이터베이스 "
            f"{db_name!r}가 '_test'로 끝나지 않습니다. 운영/개발 DB에 대해 "
            "파괴적 PG 테스트가 실행되는 것을 막기 위해 전체 테스트 세션을 "
            "중단합니다. OPENCRAB_PG_TEST_URL을 전용 *_test 데이터베이스"
            "(예: opencrab_test)로 설정하세요.",
            returncode=1,
        )


@pytest.fixture
def fast_mongo_timeout(monkeypatch):
    """``pymongo.MongoClient``를 패치해 도달 불가능한 호스트에 대한
    서버 선택 타임아웃을 짧게(100ms) 강제한다.

    프로덕션 코드(mongo_store.py)는 그대로 ``serverSelectionTimeoutMS=5000``
    으로 호출하지만, 테스트에서는 이 값을 덮어써서 "연결 불가" 동작 자체
    (store.available=False 등)는 동일하게 재현하면서 실제 대기 시간만
    줄인다. invalid-host 테스트가 매번 5초씩 실제로 기다리는 것이 스위트
    성능의 지배적 병목이었다.
    """
    import pymongo

    real_client = pymongo.MongoClient

    def _fast_client(uri, **kwargs):
        kwargs["serverSelectionTimeoutMS"] = 100
        return real_client(uri, **kwargs)

    monkeypatch.setattr(pymongo, "MongoClient", _fast_client)
