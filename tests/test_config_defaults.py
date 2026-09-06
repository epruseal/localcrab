"""config.py / llamacpp_embedding.py 의 기본 경로가 특정 사용자 홈("/home/asdf")에
하드코딩되지 않고 실행 사용자의 HOME 에서 파생되는지 검증한다.

배경: CI(ubuntu 러너)에서 "/home/asdf" 로 고정된 기본값 때문에
[Errno 13] Permission denied 가 발생했다 — 이 파일은 그 회귀 방지 테스트다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# config.Settings.local_data_dir
# ---------------------------------------------------------------------------


def test_local_data_dir_default_derives_from_home(monkeypatch, tmp_path):
    """LOCAL_DATA_DIR 미설정 + HOME=tmp_path 이면 기본값이 tmp_path 하위여야 한다."""
    monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_data_dir.startswith(str(tmp_path))
    # 옛 하드코딩 기본값으로 고정되어 있지 않은지 확인.
    assert settings.local_data_dir != "/home/asdf/.openclaw/workspace/data/localcrab"


def test_local_data_dir_env_override_wins(monkeypatch, tmp_path):
    """LOCAL_DATA_DIR 환경변수가 설정되면 default_factory 보다 우선한다."""
    override = str(tmp_path / "explicit-dir")
    monkeypatch.setenv("LOCAL_DATA_DIR", override)
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_data_dir == override


def test_local_data_dir_default_factory_reevaluates_per_instance(monkeypatch, tmp_path):
    """default_factory 는 인스턴스화 시점마다 평가된다 — 임포트 시점에 HOME 이
    고정되어 두 번째 HOME 변경이 무시되는 회귀를 막는다."""
    monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)
    from opencrab.config import Settings

    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"

    monkeypatch.setenv("HOME", str(home_a))
    settings_a = Settings(_env_file=None)

    monkeypatch.setenv("HOME", str(home_b))
    settings_b = Settings(_env_file=None)

    assert settings_a.local_data_dir.startswith(str(home_a))
    assert settings_b.local_data_dir.startswith(str(home_b))
    assert settings_a.local_data_dir != settings_b.local_data_dir


# ---------------------------------------------------------------------------
# #67: LOCAL_DATA_DIR/LOCAL_GGUF_PATH 에 "~" 를 지정해도 홈 디렉터리로 펼쳐져야
# 한다. Settings 가 이 값을 리터럴 "~..." 문자열 그대로 반환하면, 이 값을 그대로
# os.makedirs 에 넘기는 소비자(예: opencrab.locking.lock_data_dir())가 CWD 밑에
# 문자 그대로 "~" 디렉터리를 만든다.
# ---------------------------------------------------------------------------


def test_local_data_dir_env_override_expands_tilde(monkeypatch, tmp_path):
    """LOCAL_DATA_DIR="~/sub" + HOME=tmp_path 이면 HOME 하위 절대경로로 펼쳐져야 한다."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCAL_DATA_DIR", "~/sub")
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_data_dir == str(tmp_path / "sub")
    assert "~" not in settings.local_data_dir


def test_local_data_dir_env_override_bare_tilde_equals_home(monkeypatch, tmp_path):
    """LOCAL_DATA_DIR="~" 단독이면 HOME 그 자체와 같아야 한다."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCAL_DATA_DIR", "~")
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_data_dir == str(tmp_path)


def test_local_data_dir_env_override_unknown_user_tilde_left_literal(monkeypatch, tmp_path):
    """"~nouser/x" 처럼 stdlib 이 풀 수 없는 사용자명은 크래시 없이 리터럴로
    남아야 한다(회귀 아님 — os.path.expanduser 자체의 계약을 문서화)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCAL_DATA_DIR", "~nouserxyz123/x")
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_data_dir == "~nouserxyz123/x"


def test_local_data_dir_env_override_absolute_path_unaffected(monkeypatch, tmp_path):
    """물결 없는 절대경로는 그대로 통과해야 한다(회귀 가드)."""
    override = str(tmp_path / "explicit-dir")
    monkeypatch.setenv("LOCAL_DATA_DIR", override)
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_data_dir == override


def test_local_data_dir_env_override_relative_path_unaffected(monkeypatch, tmp_path):
    """물결 없는 상대경로도 그대로 통과해야 한다(회귀 가드)."""
    monkeypatch.setenv("LOCAL_DATA_DIR", "relative/sub/dir")
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_data_dir == "relative/sub/dir"


# ---------------------------------------------------------------------------
# llamacpp_embedding._default_gguf_dir / _ensure_local_gguf
# ---------------------------------------------------------------------------


def test_gguf_default_dir_derives_from_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from opencrab.stores.llamacpp_embedding import _default_gguf_dir

    result = _default_gguf_dir()

    assert result.startswith(str(tmp_path))
    # 옛 하드코딩 기본값으로 고정되어 있지 않은지 확인.
    assert result != "/home/asdf/models"


def test_gguf_default_dir_reevaluates_per_call(monkeypatch, tmp_path):
    """모듈 임포트 시점이 아니라 호출 시점마다 Path.home() 을 평가해야 한다."""
    from opencrab.stores.llamacpp_embedding import _default_gguf_dir

    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"

    monkeypatch.setenv("HOME", str(home_a))
    result_a = _default_gguf_dir()

    monkeypatch.setenv("HOME", str(home_b))
    result_b = _default_gguf_dir()

    assert result_a.startswith(str(home_a))
    assert result_b.startswith(str(home_b))
    assert result_a != result_b


def test_ensure_local_gguf_uses_home_derived_default_when_no_override(monkeypatch, tmp_path):
    """requested_path 가 비어 있고 huggingface_hub 이 없을 때(에러 메시지 경로),
    RuntimeError 메시지가 하드코딩된 "/home/asdf" 가 아니라 HOME 파생 경로를 담아야 한다."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setitem(
        __import__("sys").modules, "huggingface_hub", None
    )  # import 실패를 유도
    from opencrab.stores import llamacpp_embedding

    with pytest.raises(RuntimeError) as exc_info:
        llamacpp_embedding._ensure_local_gguf("")

    # 옛 하드코딩 기본 디렉터리("/home/asdf/models")가 아니라 HOME 파생 경로를 담아야 한다.
    assert "/home/asdf/models" not in str(exc_info.value)
    assert str(tmp_path) in str(exc_info.value)


def test_ensure_local_gguf_respects_explicit_requested_path(tmp_path):
    """requested_path 로 지정한 기존 파일이 있으면 그대로 반환(다운로드 시도 없음)."""
    gguf_file = tmp_path / "custom.gguf"
    gguf_file.write_bytes(b"fake-gguf-content")
    from opencrab.stores.llamacpp_embedding import _ensure_local_gguf

    result = _ensure_local_gguf(str(gguf_file))

    assert result == str(gguf_file)


# ---------------------------------------------------------------------------
# #67: config.Settings.local_gguf_path 도 local_data_dir 과 같은 클래스(같은
# 필드 선언 없음, 같은 Settings)의 결함이다 — "~" 지정 시 리터럴 그대로
# _ensure_local_gguf() 에 넘어간다.
# ---------------------------------------------------------------------------


def test_local_gguf_path_env_override_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCAL_GGUF_PATH", "~/models/x.gguf")
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_gguf_path == str(tmp_path / "models" / "x.gguf")
    assert "~" not in settings.local_gguf_path


def test_local_gguf_path_default_empty_string_not_corrupted(monkeypatch, tmp_path):
    """미설정(빈 문자열 센티널, "자동 다운로드" 의미)은 그대로 "" 여야 한다.
    ``Path("").expanduser()`` 는 ``"."`` 를 반환해 이 센티널을 오염시키므로,
    구현이 ``os.path.expanduser`` 를 쓰는지(빈 문자열은 그대로 두는지)를
    검증하는 회귀 가드다."""
    monkeypatch.delenv("LOCAL_GGUF_PATH", raising=False)
    from opencrab.config import Settings

    settings = Settings(_env_file=None)

    assert settings.local_gguf_path == ""


# ---------------------------------------------------------------------------
# opencrab.mcp.tools._lock_data_dir
#
# 배경(PR #25 CI 8건 실패): _lock_data_dir()가 os.environ.get() 직독 대신
# get_settings()(lru_cache)만 쓰도록 바뀌면서 ① 테스트가 monkeypatch한
# LOCAL_DATA_DIR을 캐시가 stale이라 못 보고, ② CI(.env 없음)에서 홈 파생 기본
# 디렉터리가 실제로 존재하지 않아 open()이 FileNotFoundError로 실패했다.
# 로컬은 repo의 .env 덕에 우연히 통과했었다(디렉터리가 이미 존재).
# ---------------------------------------------------------------------------


def test_lock_data_dir_creates_missing_directory_from_env(monkeypatch, tmp_path):
    """LOCAL_DATA_DIR이 아직 존재하지 않는 경로를 가리켜도 자동 생성되어야 한다."""
    nonexistent = tmp_path / "not-yet-created" / "nested"
    monkeypatch.setenv("LOCAL_DATA_DIR", str(nonexistent))
    from opencrab.mcp import tools

    result_dir = tools._lock_data_dir()

    assert result_dir == str(nonexistent)
    assert nonexistent.is_dir()


def test_lock_data_dir_falls_back_to_settings_default_and_creates_dir(monkeypatch, tmp_path):
    """LOCAL_DATA_DIR 미설정 + HOME=fake + CWD에 .env 없음(CI 재현) 이면
    get_settings() 기본값(HOME 파생)을 쓰고, 그 디렉터리가 없으면 자동 생성해야 한다."""
    monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(tmp_path)  # .env 없는 디렉터리로 이동 — CI의 CWD 조건 재현
    from opencrab.config import get_settings

    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    from opencrab.mcp import tools

    result_dir = tools._lock_data_dir()

    assert result_dir.startswith(str(fake_home))
    assert Path(result_dir).is_dir()
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()


def test_lock_data_dir_env_change_reflected_without_stale_cache(monkeypatch, tmp_path):
    """os.environ.get() 직독이 우선이므로 get_settings() lru_cache가 이전 값으로
    stale이어도 최신 LOCAL_DATA_DIR을 즉시 반영해야 한다."""
    from opencrab.config import get_settings

    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    from opencrab.mcp import tools

    dir_a = tmp_path / "dir_a"
    monkeypatch.setenv("LOCAL_DATA_DIR", str(dir_a))
    get_settings()  # 캐시를 dir_a 기준으로 채움(스테일 시나리오 재현)

    dir_b = tmp_path / "dir_b"
    monkeypatch.setenv("LOCAL_DATA_DIR", str(dir_b))

    result_dir = tools._lock_data_dir()

    assert result_dir == str(dir_b)
    assert Path(dir_b).is_dir()
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()


def test_lock_data_dir_expands_tilde_and_does_not_create_literal_tilde_dir(
    monkeypatch, tmp_path
):
    """#67 재현: LOCAL_DATA_DIR="~/sub" 를 os.environ.get() 직독 분기가 그대로
    os.makedirs 에 넘기면 CWD 밑에 문자 그대로 "~" 디렉터리가 생긴다. HOME 과
    CWD 를 서로 다른 격리 디렉터리로 분리해, 결과가 HOME 하위에 생기고 CWD 에는
    아무 "~" 디렉터리도 남지 않아야 함을 단언한다."""
    home_dir = tmp_path / "home"
    cwd_dir = tmp_path / "cwd"
    home_dir.mkdir()
    cwd_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("LOCAL_DATA_DIR", "~/sub")
    monkeypatch.chdir(cwd_dir)
    from opencrab.mcp import tools

    result_dir = tools._lock_data_dir()

    expected = str(home_dir / "sub")
    assert result_dir == expected
    assert Path(expected).is_dir()
    assert not (cwd_dir / "~").exists()


def test_lock_data_dir_rewrites_env_so_require_live_data_agrees_with_write_target(
    monkeypatch, tmp_path
):
    """리뷰 지적(#67 PR): 구버전 버그가 남긴 문자 그대로의 "~/sub" 디렉터리가 CWD 밑에
    이미 있는 상태에서 LOCAL_DATA_DIR="~/sub" 를 다시 설정하면, _lock_data_dir() 가
    펼친 실제 쓰기 대상(HOME 하위)과 opencrab.pack.live_data.require_live_data() 가
    검사하는 원시 문자열이 서로 다른 경로를 가리키게 된다 — 가드는 스테일 리터럴
    디렉터리를 보고 통과하지만 실제 쓰기는 다른 곳으로 간다. _lock_data_dir() 는
    펼친 값을 os.environ 에 되써서, require_live_data() 가 스스로는 아무 것도
    바꾸지 않으면서도 실제 쓰기 대상과 같은 경로를 보게 해야 한다."""
    home_dir = tmp_path / "home"
    cwd_dir = tmp_path / "cwd"
    home_dir.mkdir()
    cwd_dir.mkdir()
    stale_literal_tilde_dir = cwd_dir / "~" / "sub"
    stale_literal_tilde_dir.mkdir(parents=True)  # 구버전 버그가 남긴 스테일 디렉터리 재현
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("LOCAL_DATA_DIR", "~/sub")
    monkeypatch.chdir(cwd_dir)
    from opencrab.mcp import tools
    from opencrab.pack.live_data import require_live_data

    expected = str(home_dir / "sub")
    result_dir = tools._lock_data_dir()
    assert result_dir == expected

    # 픽스처 의도 명시: 스테일 리터럴 경로와 펼친 실제 쓰기 경로는 서로 다른 곳이다
    # (그래서 가드가 둘 중 어느 쪽을 보는지가 실제로 문제된다).
    assert str(stale_literal_tilde_dir) != expected

    # 핵심 단언: 환경변수 자체가 펼친 값으로 되써져 있어야 한다. 수정 전에는 원시
    # "~/sub" 그대로 남아, require_live_data() 가 스테일 리터럴 디렉터리를 보고
    # 우연히 통과한다(그 사이 실제 쓰기는 expected 로 감).
    assert os.environ["LOCAL_DATA_DIR"] == expected

    # require_live_data() 자신은 한 글자도 안 바뀌었다 — 그런데도 지금은 실제 쓰기
    # 대상(expected)과 같은 경로를 보고 정상 통과해야 한다(SystemExit 없음).
    require_live_data("test")
