#!/usr/bin/env python3
"""#154 재발 방지 게이트: 머신 고유 절대경로/사설망 IP를 저장소에서 검출한다.

배경(AGENTS.md "#154 재발 방지 규율"):
    머신 고유 절대경로(`/home/<사용자명>/...`, `/Users/<사용자명>/...`)와 사설망
    IP를 공개 텍스트(문서, 커밋 메시지, PR 본문, 코드 주석)에 남기지 않는다.
    코드가 강제하는 루프백 주소(127.0.0.1 등)는 예외다.

설계: 이슈 #429.

받아들임 기준은 "원시 매치 0건"이 아니라 "미회수 위반 0건"이다. 중립
placeholder 사용자명(ALLOWED_PLACEHOLDER_USERS)과 완전 검증된 IP 옥텟이
아닌 문자열(예: package-lock.json의 3-part semver)은 애초에 위반이 아니다.
다른 이슈가 소유한 파일의 기존 위반을 잠시 넘겨야 하면 DEFERRED_VIOLATIONS에
(path, matched_text) 쌍으로 기록한다. 쌍 단위로 기록하므로 같은 파일 안의 다른
신규 위반은 그대로 잡힌다. 줄 번호는 다른 커밋이 줄을 밀면 거짓 판정을 내므로
키로 쓰지 않는다. 현재 보류 목록은 비어 있다.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 중립 placeholder 사용자명. 테스트 픽스처가 쓰는 가짜 홈 경로는 실제 머신을
# 가리키지 않으므로 위반이 아니다.
ALLOWED_PLACEHOLDER_USERS = {"user", "someone", "alice"}

# 경로 검사는 모든 파일에 예외 없이 건다(package.json의 scripts 필드 등에도
# 실제 머신 경로가 들어갈 수 있다).
HOME_PATH_RE = re.compile(
    r"/home/(?P<user>[a-z_][a-z0-9_-]*)/|/Users/(?P<user2>[A-Za-z][A-Za-z0-9_-]*)"
)

# 이슈 #429의 재현 명령(CMD2)은 3개 숫자 그룹만 요구하는 느슨한 정규식이라
# package-lock.json류의 3-part semver 버전 문자열("10.4.27" 등)을 사설망 IP로
# 오탐한다. 실제 IPv4는 언제나 옥텟 4개이므로, 이 스캐너는 4개 옥텟이 전부
# 0-255 범위인 완전한 점 표기만 매치한다. 그래서 3-part 문자열은 애초에 걸리지
# 않는다(파일 예외가 필요 없다). 리딩/트레일링 가드(둘 다 필수.
# 반례 "1.10.1.2.3": 트레일링 가드만 있으면 앞에 더 붙은 "1." 때문에
# 뒤 4토큰이 사설 IP로 오탐된다)로 더 긴 숫자열의 부분 문자열 매치도 막는다.
_OCTET = r"(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]|0)"
PRIVATE_IP_RE = re.compile(
    r"(?<![\d.])"
    rf"(?:10\.{_OCTET}\.{_OCTET}\.{_OCTET}"
    rf"|192\.168\.{_OCTET}\.{_OCTET}"
    rf"|172\.(?:1[6-9]|2[0-9]|3[01])\.{_OCTET}\.{_OCTET}"
    rf"|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.{_OCTET}\.{_OCTET})"
    r"(?![\d.])"
)

# 옥텟 검증 뒤에도 남는 IP 오탐이 생기면 그때만 파일 단위로 등록한다. 현재는
# 실측 결과 잔여 오탐이 없어 빈 튜플이다(경로 검사에는 적용하지 않는다, IP
# 검사 전용).
ALLOWLIST_IP_PATH_GLOBS: tuple[str, ...] = ()

# 보류 목록. 다른 이슈가 소유한 파일의 기존 위반만 (path, matched_text) 쌍으로
# 기록한다. 기록된 쌍이 저장소에서 사라지면 main()이 실패해 목록 갱신을 강제한다.
DEFERRED_VIOLATIONS: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    matched_text: str
    kind: str  # "home_path" | "private_ip"


def _iter_lines(text: str) -> Iterable[tuple[int, str]]:
    yield from enumerate(text.splitlines(), start=1)


def _scan_home_paths(path: Path, text: str) -> list[Violation]:
    violations: list[Violation] = []
    for lineno, line in _iter_lines(text):
        for m in HOME_PATH_RE.finditer(line):
            user = m.group("user") or m.group("user2")
            if user in ALLOWED_PLACEHOLDER_USERS:
                continue
            violations.append(
                Violation(path=path, line=lineno, matched_text=m.group(0), kind="home_path")
            )
    return violations


def _path_is_ip_allowlisted(path: Path) -> bool:
    return any(path.match(glob) for glob in ALLOWLIST_IP_PATH_GLOBS)


def _scan_private_ips(path: Path, text: str) -> list[Violation]:
    if _path_is_ip_allowlisted(path):
        return []
    violations: list[Violation] = []
    for lineno, line in _iter_lines(text):
        for m in PRIVATE_IP_RE.finditer(line):
            violations.append(
                Violation(path=path, line=lineno, matched_text=m.group(0), kind="private_ip")
            )
    return violations


def scan(files: Iterable[Path]) -> list[Violation]:
    """전달받은 파일 목록만 검사한다. 파일 열거(git ls-files 등)는 호출자의
    책임이다. scan()은 자기 안에서 대상 파일을 찾지 않는다. 전달받은 파일
    집합과 그 시점의 내용에만 의존하고 숨은 전역 상태(git 인덱스, cwd)를
    참조하지 않는다는 뜻에서 결정적이다. 디코드 실패(바이너리) 파일은 두
    검사 모두 건너뛴다.
    """
    violations: list[Violation] = []
    for path in files:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        violations.extend(_scan_home_paths(path, text))
        violations.extend(_scan_private_ips(path, text))
    return violations


def classify_violations(
    violations: Iterable[Violation], deferred: frozenset[tuple[str, str]]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    found_pairs = {(str(v.path), v.matched_text) for v in violations}
    return found_pairs - deferred, deferred - found_pairs


def enumerate_git_files() -> list[Path]:
    """`git ls-files -z`로 추적 파일 목록을 얻는다. 호출 시점의 cwd가 저장소
    루트라고 가정한다(이 저장소의 다른 회귀 대사 grep 명령과 동일한 관례)."""
    out = subprocess.check_output(["git", "ls-files", "-z"])
    names = [n for n in out.decode("utf-8").split("\0") if n]
    return [Path(n) for n in names]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    files = enumerate_git_files()
    violations = scan(files)
    unexpected, missing_deferred = classify_violations(violations, DEFERRED_VIOLATIONS)

    if unexpected:
        print("미회수 위반:")
        for path, matched in sorted(unexpected):
            print(f"  {path}: {matched!r}")
        return 1

    if missing_deferred:
        print("기록된 보류 위반이 저장소에서 사라졌다. DEFERRED_VIOLATIONS에서 해당 쌍을 지운다:")
        for path, matched in sorted(missing_deferred):
            print(f"  {path}: {matched!r}")
        return 1

    print(f"OK: 미회수 위반 0건 (보류 {len(DEFERRED_VIOLATIONS)}건)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
