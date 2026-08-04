"""Settings 가 표준 env 파일을 실행 경로와 무관하게 읽는지 고정한다.

막으려는 사고(2026-08-04 실측):
    OPENAI_API_BASE 가 systemd 유닛의 EnvironmentFile 에만 배선돼 있었다.
    ssh 로 적재 CLI 를 직접 실행한 경로는 그 파일을 읽지 않아 코드 기본값
    "http://localhost:1234/v1" 로 떨어졌고, 그 호스트에는 LM Studio 가 없어
    전 배치가 로컬 GGUF(CPU) 폴백으로 갔다. 도달 가능한 원격 GPU 두 대를 두고
    1,218건에 43분을 썼다.

    같은 클래스가 두 번째였다. 2026-07-07 에는 반대 방향으로 터졌다 — 서비스가
    repo .env 를 읽지 않아(WorkingDirectory=~) LOCAL_DATA_DIR 을 env 파일 쪽에
    고정해야 했다. 그래서 경로별 배선을 늘리지 않고 Settings 가 표준 위치를
    직접 읽게 했다. 이 파일은 그 성질이 조용히 되돌아가지 않게 한다.

우선순위 계약: 실제 환경변수 > CWD .env > 표준 위치.
"""

import os
from pathlib import Path

from opencrab.config import DEFAULT_ENV_FILE, Settings, _default_env_files

REMOTE_GPUS = "http://100.77.10.49:1234/v1,http://100.89.143.59:1234/v1"


def _write(path: Path, base: str) -> Path:
    path.write_text(f"OPENAI_API_BASE={base}\n", encoding="utf-8")
    return path


def test_reads_standard_env_file(tmp_path, monkeypatch):
    """표준 위치의 env 파일을 읽는다 — 사고가 난 바로 그 경로."""
    env = _write(tmp_path / "kure.env", REMOTE_GPUS)
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(env))
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.chdir(tmp_path)          # CWD .env 없음 = ssh 직접 실행 상황

    assert Settings().openai_api_base == REMOTE_GPUS


def test_explicit_env_var_wins(tmp_path, monkeypatch):
    """실제 환경변수가 env 파일을 이긴다.

    systemd 유닛은 EnvironmentFile 로 값을 '실제 환경변수'로 주입한다. 이 순서가
    뒤집히면 서비스 동작이 조용히 바뀐다.
    """
    env = _write(tmp_path / "kure.env", REMOTE_GPUS)
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(env))
    monkeypatch.setenv("OPENAI_API_BASE", "http://explicit:9999/v1")
    monkeypatch.chdir(tmp_path)

    assert Settings().openai_api_base == "http://explicit:9999/v1"


def test_cwd_dotenv_overrides_standard(tmp_path, monkeypatch):
    """CWD .env 가 표준 위치를 덮는다(로컬 개발이 운영값에 갇히지 않게)."""
    env = _write(tmp_path / "kure.env", REMOTE_GPUS)
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(env))
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".env", "http://from-cwd:8888/v1")

    assert Settings().openai_api_base == "http://from-cwd:8888/v1"


def test_missing_env_file_is_not_fatal(tmp_path, monkeypatch):
    """표준 파일이 없는 호스트에서도 죽지 않고 코드 기본값으로 간다."""
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(tmp_path / "nope.env"))
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert Settings().openai_api_base == "http://localhost:1234/v1"


def test_env_files_evaluated_per_instance(tmp_path, monkeypatch):
    """env 파일 목록은 인스턴스화 시점에 평가된다.

    model_config 에 직접 넣으면 모듈 임포트 시점에 고정돼, HOME 이나
    LOCALCRAB_ENV_FILE 을 monkeypatch 하는 테스트가 통째로 무력해진다.
    """
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(tmp_path / "a.env"))
    first = _default_env_files()
    monkeypatch.setenv("LOCALCRAB_ENV_FILE", str(tmp_path / "b.env"))
    second = _default_env_files()

    assert first != second
    assert first[0].endswith("a.env") and second[0].endswith("b.env")


def test_default_path_matches_systemd_unit():
    """표준 경로 상수가 systemd 유닛의 EnvironmentFile 과 같은 파일을 가리킨다.

    두 값이 갈리면 서비스와 CLI 가 다시 다른 설정을 보게 된다 — 이 수정이
    닫으려던 결함 그 자체다. 유닛 파일은 이 리포 밖(운영 호스트)에 있으므로
    경로 문자열을 여기에 고정해 드리프트를 드러낸다.
    """
    assert DEFAULT_ENV_FILE == "~/.openclaw/localcrab-kure.env"
    assert _default_env_files()[-1] == ".env", "CWD .env 가 마지막(최우선)이어야 한다"


def test_standard_path_is_expanded(tmp_path, monkeypatch):
    """~ 를 펼치지 않으면 파일을 못 찾고 조용히 기본값으로 떨어진다."""
    monkeypatch.delenv("LOCALCRAB_ENV_FILE", raising=False)
    files = _default_env_files()

    assert "~" not in files[0]
    assert files[0] == str(Path(DEFAULT_ENV_FILE).expanduser())
    assert os.path.isabs(files[0])
