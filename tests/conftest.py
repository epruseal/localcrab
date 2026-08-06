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

# 테스트 스위트가 사용자의 실제 데이터 디렉터리(LOCAL_DATA_DIR 미설정 시
# get_settings().local_data_dir 의 HOME 파생 기본값)에 쓰지 못하도록 세션
# 전체를 강제 격리한다 (이슈 #126).
#
# 계기: 개별 테스트가 store/context 생성을 mock 하면서도 _write_lock()
# (opencrab/mcp/tools/__init__.py) 은 그대로 통과시켜, 실제 경로에
# write.lock 을 남기는 사례가 반복됐다(tests/test_mcp.py 등). 과거에는
# 부분적으로만 mock 된 테스트가 실제 경로에 잘못된 스키마의 .db 까지
# 만든 적도 있다. 원인은 개별 테스트의 실수가 아니라, LOCAL_DATA_DIR 이
# 비어 있으면 어떤 코드 경로든 조용히 실제 HOME 기반 기본값으로
# 폴백한다는 구조 자체다 — 그래서 개별 테스트를 고치는 대신(A) 모든
# 테스트가 거쳐가는 지점에서 폴백 자체를 차단한다(B, 근본 원인).
#
# setdefault 인 이유는 LOCALCRAB_ENV_FILE 과 동일: 호출자가 LOCAL_DATA_DIR
# 을 명시하면(예: tests/test_config_defaults.py 가 monkeypatch.delenv 뒤
# HOME 파생 기본값 자체를 검증) 그대로 존중된다. 모듈 최상단인 이유도
# 동일 — get_settings() 는 lru_cache 이고 일부 모듈은 임포트 시점에
# Settings 를 만들므로 fixture 로는 늦는다.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="localcrab-test-data-")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)
os.environ.setdefault("LOCAL_DATA_DIR", _TEST_DATA_DIR)

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
