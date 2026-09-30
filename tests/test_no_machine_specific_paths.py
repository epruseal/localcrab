"""#154 재발 방지 게이트 회귀 테스트 — scripts/qa/check_no_machine_paths.py.

설계: 이슈 #429(스캐너 검증, 보류 로직 테스트, 경계 변이 테스트).

이 파일 자체가 저장소 게이트 대상이므로, 계획된 머신 고유 경로/사설망 IP
리터럴은 소스에 그대로 쓰지 않고 런타임에 조립한다(_fake_home_path 등) —
그렇지 않으면 이 테스트 파일 자신이 test_repo_has_no_new_machine_paths 를
깨뜨린다.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module_from_path(name: str, relpath: str):
    """scripts/ 처럼 패키지가 아닌 모듈을 파일 경로로 직접 로드한다.

    exec_module 전에 sys.modules 에 등록해야 한다 — 로드 대상이 dataclass 를
    쓰면 dataclasses 내부가 cls.__module__ 로 sys.modules 를 되찾아 조회하는데,
    등록 전이면 그 조회가 None 이 되어 AttributeError 가 난다.
    """
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module_from_path("check_no_machine_paths", "scripts/qa/check_no_machine_paths.py")


def _fake_home_path(user: str, suffix: str) -> str:
    return "/".join(["", "home", user, suffix])


def _fake_macos_path(user: str, suffix: str) -> str:
    return "/".join(["", "Users", user, suffix])


def _num_dotted(*parts: object) -> str:
    return ".".join(str(p) for p in parts)


def _unexpected_and_missing(violations, deferred):
    found_pairs = {(str(v.path), v.matched_text) for v in violations}
    return found_pairs - deferred, deferred - found_pairs


# ---------------------------------------------------------------------------
# 기본 탐지
# ---------------------------------------------------------------------------


def test_scanner_detects_planted_home_path(tmp_path):
    target = tmp_path / "note.txt"
    planted = _fake_home_path("workeruser", "project/file.py")
    target.write_text(f"참고: {planted} 를 확인한다\n", encoding="utf-8")

    violations = mod.scan([target])

    assert len(violations) == 1
    assert violations[0].kind == "home_path"
    assert violations[0].matched_text == _fake_home_path("workeruser", "")


def test_scanner_detects_planted_macos_path(tmp_path):
    target = tmp_path / "note.txt"
    planted = _fake_macos_path("workeruser", "project/file.py")
    target.write_text(f"참고: {planted} 를 확인한다\n", encoding="utf-8")

    violations = mod.scan([target])

    assert len(violations) == 1
    assert violations[0].kind == "home_path"
    assert violations[0].matched_text == _fake_macos_path("workeruser", "").rstrip("/")


def test_scanner_detects_planted_private_ip(tmp_path):
    target = tmp_path / "note.txt"
    ip = _num_dotted(10, 1, 2, 3)
    target.write_text(f"host={ip} 로 접속\n", encoding="utf-8")

    violations = mod.scan([target])

    assert len(violations) == 1
    assert violations[0].kind == "private_ip"
    assert violations[0].matched_text == ip


# ---------------------------------------------------------------------------
# 경계값 — CIDR 범위와 리딩/트레일링 가드
# ---------------------------------------------------------------------------

_CIDR_CASES = [
    (_num_dotted(10, 0, 0, 1), True),
    (_num_dotted(10, 255, 255, 255), True),
    (_num_dotted(192, 168, 0, 1), True),
    (_num_dotted(192, 168, 255, 255), True),
    (_num_dotted(172, 16, 0, 1), True),
    (_num_dotted(172, 31, 255, 255), True),
    (_num_dotted(100, 64, 0, 1), True),
    (_num_dotted(100, 127, 255, 255), True),
    (_num_dotted(172, 15, 255, 255), False),
    (_num_dotted(172, 32, 0, 0), False),
    (_num_dotted(100, 63, 255, 255), False),
    (_num_dotted(100, 128, 0, 0), False),
    (_num_dotted(192, 167, 255, 255), False),
    (_num_dotted(192, 169, 0, 0), False),
    (_num_dotted(8, 8, 8, 8), False),
    (_num_dotted(11, 0, 0, 1), False),
]


def test_scanner_ip_cidr_boundaries(tmp_path):
    for idx, (ip, expect_match) in enumerate(_CIDR_CASES):
        target = tmp_path / f"case_{idx}.txt"
        target.write_text(f"addr={ip} end\n", encoding="utf-8")
        violations = mod.scan([target])
        ip_violations = [v for v in violations if v.kind == "private_ip"]
        if expect_match:
            assert len(ip_violations) == 1, f"{ip} 는 매치돼야 한다"
            assert ip_violations[0].matched_text == ip
        else:
            assert ip_violations == [], f"{ip} 는 매치되면 안 된다"


def test_scanner_rejects_ip_with_extra_leading_digit_token(tmp_path):
    """반례: "1.10.1.2.3" — 트레일링 가드만 있으면 앞의 "1." 때문에
    뒤 4토큰이 사설 IP로 오탐된다. 리딩 가드가 이를 막는다."""
    target = tmp_path / "note.txt"
    target.write_text(f"token={_num_dotted(1, 10, 1, 2, 3)} end\n", encoding="utf-8")

    violations = mod.scan([target])

    assert [v for v in violations if v.kind == "private_ip"] == []


def test_scanner_rejects_out_of_range_octet(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text(f"addr={_num_dotted(192, 168, 1, 999)} end\n", encoding="utf-8")

    violations = mod.scan([target])

    assert [v for v in violations if v.kind == "private_ip"] == []


# ---------------------------------------------------------------------------
# 파일 예외 없음(경로) vs 정규식 정밀도로 예외 없앰(IP)
# ---------------------------------------------------------------------------


def test_scanner_flags_absolute_path_inside_package_json(tmp_path):
    target = tmp_path / "package.json"
    planted = _fake_home_path("produser", "bin/build.sh")
    target.write_text(
        '{\n  "scripts": {\n    "build": "' + planted + '"\n  }\n}\n',
        encoding="utf-8",
    )

    violations = mod.scan([target])

    assert len(violations) == 1
    assert violations[0].kind == "home_path"


def test_scanner_does_not_flag_semver_as_ip(tmp_path):
    target = tmp_path / "package-lock.json"
    semver = ".".join(str(p) for p in (10, 4, 27))
    target.write_text('{\n  "version": "' + semver + '"\n}\n', encoding="utf-8")

    violations = mod.scan([target])

    assert [v for v in violations if v.kind == "private_ip"] == []


# ---------------------------------------------------------------------------
# 중립 placeholder 사용자명
# ---------------------------------------------------------------------------


def test_allowed_placeholder_users_actually_filters(tmp_path):
    allowed_target = tmp_path / "allowed.txt"
    allowed_target.write_text(_fake_home_path("alice", "x") + "\n", encoding="utf-8")
    real_target = tmp_path / "real.txt"
    real_target.write_text(_fake_home_path("alice2", "x") + "\n", encoding="utf-8")

    assert mod.scan([allowed_target]) == []
    violations = mod.scan([real_target])
    assert len(violations) == 1
    assert violations[0].kind == "home_path"


# ---------------------------------------------------------------------------
# (path, matched_text) 쌍 키 보류 로직
# ---------------------------------------------------------------------------


def test_scan_flags_new_violation_in_deferred_file(tmp_path):
    """같은 파일 안이라도 보류된 matched_text 와 다른 신규 위반은 잡혀야 한다
    — 줄 번호가 아니라 (path, matched_text) 쌍이 보류 키라서 가능한 정밀도."""
    target = tmp_path / "owned.py"
    recorded = _fake_home_path("owneruser", "old.py")
    new_violation = _fake_home_path("intruderuser", "new.py")
    target.write_text(f"{recorded}\n{new_violation}\n", encoding="utf-8")
    deferred = frozenset({(str(target), _fake_home_path("owneruser", ""))})

    violations = mod.scan([target])
    unexpected, missing = _unexpected_and_missing(violations, deferred)

    assert unexpected == {(str(target), _fake_home_path("intruderuser", ""))}
    assert missing == set()


def test_scan_passes_with_only_recorded_violations(tmp_path):
    target = tmp_path / "owned.py"
    recorded = _fake_home_path("owneruser", "old.py")
    target.write_text(f"{recorded}\n", encoding="utf-8")
    deferred = frozenset({(str(target), _fake_home_path("owneruser", ""))})

    violations = mod.scan([target])
    unexpected, missing = _unexpected_and_missing(violations, deferred)

    assert unexpected == set()
    assert missing == set()


# ---------------------------------------------------------------------------
# 역변이 — 두 탐지기가 서로 독립임을 확인(한쪽을 죽여도 다른 쪽은 산다)
# ---------------------------------------------------------------------------

_NEVER_MATCHES = re.compile(r"(?!)")


def test_disabling_home_path_detector_leaves_ip_detector_working(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "HOME_PATH_RE", _NEVER_MATCHES)
    target = tmp_path / "note.txt"
    target.write_text(f"addr={_num_dotted(10, 1, 2, 3)} end\n", encoding="utf-8")

    violations = mod.scan([target])

    assert [v.kind for v in violations] == ["private_ip"]


def test_disabling_ip_detector_leaves_home_path_detector_working(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PRIVATE_IP_RE", _NEVER_MATCHES)
    target = tmp_path / "note.txt"
    planted = _fake_home_path("workeruser", "file.py")
    target.write_text(f"{planted}\n", encoding="utf-8")

    violations = mod.scan([target])

    assert [v.kind for v in violations] == ["home_path"]


# ---------------------------------------------------------------------------
# 실제 저장소 게이트 — "미회수 위반 0건"
# ---------------------------------------------------------------------------


def test_repo_has_no_new_machine_paths():
    files = mod.enumerate_git_files()
    violations = mod.scan(files)
    found_pairs = {(str(v.path), v.matched_text) for v in violations}
    unexpected = found_pairs - mod.DEFERRED_VIOLATIONS
    assert unexpected == set(), f"미회수 위반: {sorted(unexpected)}"


def test_deferred_violations_are_exactly_recorded():
    files = mod.enumerate_git_files()
    violations = mod.scan(files)
    found_pairs = {(str(v.path), v.matched_text) for v in violations}
    missing = mod.DEFERRED_VIOLATIONS - found_pairs
    assert missing == set(), f"보류 등록됐지만 저장소에서 사라진 위반: {sorted(missing)}"
