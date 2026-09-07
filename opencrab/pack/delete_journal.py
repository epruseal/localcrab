"""`delete_pack` 재개 저널 — 크래시로 남은 부분 삭제 상태를 식별한다(#327).

`delete_pack` 은 graph·doc·vector 세 스토어를 **따로** 커밋한다(크로스 스토어 단일
트랜잭션이 없다). 중간에 죽으면 일부 축만 지워진 채 남는데, 이 모듈이 그 상태를
기록하고 다음 실행이 읽어 판단하게 한다.

**계약은 명시-확인 전용이다(리드 재정, opencrab-dump #61 rename 저널과 동형의 원자적
교체 방식).** 자동 재개는 없다.

  1. 재실행이 기존 저널을 보면 **기본은 탐지·보고뿐**이다 — 스토어를 건드리지
     않고 `DeletePackJournalPending` 을 던진다(`opencrab.pack.load.delete_pack` 이
     축 상태·라이브 카운트·창 경고를 담아 던진다, 이 모듈은 예외 타입만 정의한다).
  2. 운영자가 그 보고를 보고 `resume=True` 를 명시해야만 완주한다.
  3. `resume=True` 라도 팩 동일성 부정 신호(저널 생성 시점 대비 레지스트리 행 소멸
     또는 `created_at` 불일치)가 있으면 `DeletePackJournalConflict` 로 거부한다 —
     일치는 증명이 아니므로 통과 조건으로만 쓰고, 불일치만 확실한 부정 신호로
     차단에 쓴다(SQLite `datetime('now')` 초 단위 충돌 실측, 5라운드 codex).
  4. 저널이 찢어져 파싱할 수 없으면 "없음"으로 접지 않고 `DeletePackJournalCorrupt`
     를 던진다 — 소유 증거가 있는데 없는 것처럼 굴면 다른 실행이 같은 팩에 겹쳐
     쓴다.

축 값의 재실행 여부는 **`done` 플래그로만** 판정한다(`count`/`clean` 이 채워져
있어도 그것을 "이미 실행됨"의 근거로 쓰지 않는다) — 정상 종료해도 개별 노드 실패를
삼키고 진행하는 축이 있어(그래프·doc 삭제 루프) `count>0` 인 채로 일부만 지워질 수
있기 때문이다.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

JOURNAL_SCHEMA = 1


class DeletePackJournalCorrupt(Exception):  # noqa: N818 - public domain exception name
    """저널 파일이 있는데 읽을 수 없다(파싱 실패 또는 스키마 불일치).

    "없음"으로 접으면 소유 증거가 사라진 채 재개가 열린다 — opencrab-dump #61
    rename 저널의 `load_journal` 계약과 동형.
    """


class DeletePackJournalPending(Exception):  # noqa: N818 - public domain exception name
    """저널이 있고 `resume` 플래그가 없다 — 탐지·보고만 하고 무쓰기로 멈춘다."""


class DeletePackJournalConflict(Exception):  # noqa: N818 - public domain exception name
    """`resume=True` 인데 팩 동일성 부정 신호가 있어 거부한다(행 소멸/created_at 불일치)."""


def _slug(pack_name: str) -> str:
    """팩 이름을 파일 시스템에 안전한 고정 길이 토큰으로 만든다.

    팩 이름은 유니코드·구분자를 포함할 수 있어 파일명으로 직접 쓰면 경로 구분자
    충돌·길이 제한을 만난다. 해시라 이름이 달라도 충돌 확률은 무시할 수준이고,
    잠금 파일명과 저널 파일명이 같은 `_slug` 를 공유해 항상 짝을 이룬다.
    """
    return hashlib.sha256(pack_name.encode("utf-8")).hexdigest()[:32]


def lock_filename(pack_name: str) -> str:
    """`delete_pack` 실행 전체를 감싸는 `opencrab.locking.file_lock` 파일명.

    팩마다 갈라야 서로 다른 팩의 `delete_pack` 이 불필요하게 직렬화되지 않는다.
    """
    return f"delete-pack-{_slug(pack_name)}.lock"


def journal_path(data_dir: str | os.PathLike[str], pack_name: str) -> Path:
    return Path(data_dir) / "delete-pack-journals" / f"{_slug(pack_name)}.json"


def load_journal(data_dir: str | os.PathLike[str], pack_name: str) -> dict[str, Any] | None:
    """저널이 없으면 `None`, 있는데 못 읽으면 `DeletePackJournalCorrupt`."""
    path = journal_path(data_dir, pack_name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeletePackJournalCorrupt(
            f"삭제 저널을 읽을 수 없다({path}): {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != JOURNAL_SCHEMA:
        raise DeletePackJournalCorrupt(
            f"삭제 저널 스키마가 다르다({path}): {payload!r}"
        )
    return payload


def save_journal(data_dir: str | os.PathLike[str], pack_name: str, payload: dict[str, Any]) -> None:
    """임시파일 + fsync + `os.replace` 로 원자적 교체(opencrab-dump #61 rename 저널과 동형).

    쓰기 도중 죽어도(예: `os.replace` 직전) 기존 저널 파일은 그대로 남는다 —
    이전 값을 읽는 재실행이 "반쯤 쓰인" 내용을 절대 보지 않는다.
    """
    path = journal_path(data_dir, pack_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + f".tmp-{os.getpid()}")
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)  # replace 성공 뒤엔 이미 없어 무해한 no-op
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def clear_journal(data_dir: str | os.PathLike[str], pack_name: str) -> None:
    """모든 축이 `done` 이면 호출자가 부른다 — 완료된 삭제의 저널을 치운다."""
    journal_path(data_dir, pack_name).unlink(missing_ok=True)
