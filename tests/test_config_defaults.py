"""config.py / llamacpp_embedding.py 의 기본 경로가 특정 사용자 홈("/home/asdf")에
하드코딩되지 않고 실행 사용자의 HOME 에서 파생되는지 검증한다.

배경: CI(ubuntu 러너)에서 "/home/asdf" 로 고정된 기본값 때문에
[Errno 13] Permission denied 가 발생했다 — 이 파일은 그 회귀 방지 테스트다.
"""

from __future__ import annotations

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
