"""``opencrab.pack.live_data.require_live_data`` 계약.

이 가드는 적재 계층이 **엉뚱한 저장소에 쓰는 것**을 막는 유일한 방벽이다. 이관 전에는
호출자 리포의 정적 게이트가 "쓰기 함수 전량이 이 가드를 부르는가"를 AST 로 검사했다.
적재 함수가 이 패키지로 넘어오면 그 검사는 남의 리포 파일을 뒤지게 되므로, 계약을
**여기서** 건다.

이 파일이 거는 것은 가드 **자체의 행동**이다. "쓰기 함수 전량이 이것을 부르는가"라는
커버리지 계약은 적재 함수가 넘어오는 시점에 별도로 건다.

**불변식 — 가드는 `LOCAL_DATA_DIR` 값을 어떤 방식으로도 변형하지 않는다.**
정규화는 전부 수용 집합을 넓히고, 넓어진 만큼이 엉뚱한 저장소다. `expanduser`·`strip`·
`resolve`·`normpath`·`realpath`·`is_symlink` 관용은 전부 "개선"처럼 보이지만 이 가드에
한해서는 **약화**다. 수용 집합은 정확히 `Path(값).is_dir()` 이어야 한다.

이 불변식을 문장으로만 두면 다음 사람이 또 "개선"한다. 그래서 **코드가 강제한다** —
구조는 두 축으로 갈려 있다:

  · **값 축** — `TestVerdictEqualsIsDir` 이 기대 판정을 손으로 적지 않고
    `_oracle(value)`(= `Path(value).is_dir()`, OSError 는 거부)과 대사한다.
    표로 열거하던 판은 열거 밖 변형(`expandvars`·`rstrip`·백슬래시)이 그대로 통과했다.
  · **출처·조건 축** — `TestGuardDependsOnNothingButThatOneValue` 가 모듈 전체를
    봉인한다(env 1회·`sys.exit` 만·최상위 실행문 금지). 이 축은 값으로 못 잡는다:
    `if "pytest" not in sys.modules: return` 은 **운영에서만** 가드를 무효로 만든다.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from opencrab.pack.live_data import require_live_data


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)


def _lab(tmp_path):
    """값 코퍼스가 참조할 실물들. 심링크 3종을 실제로 만든다."""
    real = tmp_path / "real"
    real.mkdir()
    afile = tmp_path / "afile"
    afile.write_text("not a dir")
    link_dir = tmp_path / "link_to_dir"
    link_dir.symlink_to(real, target_is_directory=True)
    link_file = tmp_path / "link_to_file"
    link_file.symlink_to(afile)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    return real, afile, link_dir, link_file, dangling


# 값 **코퍼스**. 기대 판정을 붙이지 않는다 — 그것은 아래 오라클이 계산한다.
# 표에 기대값을 손으로 적던 판은 열거 밖 변형 축이 그대로 열려 있었다.
_CORPUS = {
    "실재 디렉터리":       lambda tp, lab: str(lab[0]),
    "디렉터리 심링크":     lambda tp, lab: str(lab[2]),
    "파일":                lambda tp, lab: str(lab[1]),
    "파일 심링크":         lambda tp, lab: str(lab[3]),
    "끊어진 심링크":       lambda tp, lab: str(lab[4]),
    "없는 절대경로":       lambda tp, lab: str(tp / "nope"),
    "없는 상대경로":       lambda tp, lab: "no/such/relative/dir",
    "없는 중간요소+..":    lambda tp, lab: str(tp / "ghost" / ".." / "real"),
    "리터럴 틸드":         lambda tp, lab: "~",
    "틸드+하위경로":       lambda tp, lab: "~/localcrab",
    "앞뒤 공백":           lambda tp, lab: f" {lab[0]} ",
    # 아래 4종은 오라클 전환과 함께 들어온 축이다. 각각 expandvars / rstrip /
    # 백슬래시 변환 / OSError 삼킴 변이를 가른다(적대 검증 실증, 2026-08-10: B1·B3·B4·B8).
    "env 변수 표기":       lambda tp, lab: "$TMPDIR",
    "env 변수 중괄호":     lambda tp, lab: "${HOME}",
    "후행 개행":           lambda tp, lab: f"{lab[0]}\n",
    "백슬래시 구분자":     lambda tp, lab: str(tp).replace("/", "\\") + "\\real",
    "초장문 경로":         lambda tp, lab: "/tmp/" + "a" * 4096,
}


def _oracle(value: str) -> bool:
    """계약 그 자체. 이 함수가 곧 불변식이다.

    가드가 값을 **어떻게** 판정해야 하는지를 여기 한 번만 적는다. 값별 기대 판정을
    손으로 적으면 열거 밖 변형이 남지만, 오라클과 대사하면 코퍼스에 그 변형이 건드리는
    값이 하나만 있어도 변이가 죽는다.

    `is_dir()` 이 `OSError` 로 답을 못 하면 **수용해선 안 된다** — 판정 불가를
    통과로 바꾸는 것이 곧 확대다.
    """
    if not value:
        return False          # 미설정과 같은 취급(별도 분기, 아래 테스트가 따로 건다)
    try:
        return pathlib.Path(value).is_dir()
    except OSError:
        return False


class TestVerdictEqualsIsDir:
    """수용 집합은 **정확히** ``Path(값).is_dir()`` 이다 — 오라클로 건다.

    앞선 두 판은 값을 손으로 열거했다. 처음엔 `resolve()` 하나만(canary), 다음엔
    거부 9행·수용 2행 표. 표는 지적된 4종을 정확히 닫았지만 **여전히 열거**라서 열거
    밖 축이 그대로 열려 있었다 — `expandvars`·`rstrip`·백슬래시 변환·`OSError` 삼킴
    4종이 21 passed 를 유지했다(적대 검증 실증, 2026-08-10: B1·B3·B4·B8).

    변형 함수는 무한하다. 열거로는 못 따라간다. 반면 불변식은 한 줄이다 —
    그것을 문장이 아니라 **실행되는 오라클**로 쓰면 축 전체가 닫힌다.
    그러면서도 `Path(d).absolute()`·`os.path.isdir(d)` 같은 **진성 등가**는 오라클과
    판정이 같으므로 계속 산다. 과잉 계약 없이 클래스만 닫힌다.
    """

    @pytest.mark.parametrize("case", sorted(_CORPUS))
    def test_verdict_matches_the_oracle(self, case, monkeypatch, tmp_path):
        value = _CORPUS[case](tmp_path, _lab(tmp_path))
        want_accept = _oracle(value)
        monkeypatch.setenv("LOCAL_DATA_DIR", value)
        if want_accept:
            assert require_live_data("load_nodes") is None, (
                f"{case}: is_dir() 는 True 인데 가드가 거부했다 — 과잉 거부다. {value!r}")
        else:
            with pytest.raises(SystemExit) as ei:
                require_live_data("load_nodes")
            assert "경로 없음" in str(ei.value), f"{case}: {value!r}"

    # 값 축의 잔여 구멍은 **생성**으로 메운다. 코퍼스에 3행 더 붙이면 다시 열거고,
    # 다음엔 다른 문자셋·다른 접두사가 남는다(적대 검증 실증, 2026-08-10: N5 비-ASCII /
    # N6 접두사 허용목록 / N7 길이 분기). 오라클이 이미 기대값을 계산하므로 값을 늘리는
    # 비용이 0에 수렴한다 — 축을 조합으로 훑는다.
    @pytest.mark.parametrize("charset", ["ascii", "한글", "🧭"])
    @pytest.mark.parametrize("length", ["짧음", "4k", "6k"])
    @pytest.mark.parametrize("prefix", ["tmp", "/Volumes", "/mnt", "/net"])
    def test_generated_values_match_the_oracle(self, charset, length, prefix,
                                               monkeypatch, tmp_path):
        stem = {"ascii": "seg", "한글": "구간", "🧭": "🧭"}[charset]
        n = {"짧음": 1, "4k": 4096 // max(1, len(stem)), "6k": 6000 // max(1, len(stem))}[length]
        base = str(tmp_path) if prefix == "tmp" else prefix
        value = f"{base}/{stem * n}"
        want_accept = _oracle(value)
        monkeypatch.setenv("LOCAL_DATA_DIR", value)
        if want_accept:
            assert require_live_data("load_nodes") is None, f"과잉 거부: {value[:80]}…"
        else:
            with pytest.raises(SystemExit) as ei:
                require_live_data("load_nodes")
            assert "경로 없음" in str(ei.value), f"{charset}/{length}/{prefix}"

    def test_the_corpus_actually_splits_both_ways(self, tmp_path):
        """코퍼스가 수용·거부 **양쪽**을 담고 있어야 한다.

        전부 거부면 "항상 거부" 구현이 통과하고, 전부 수용이면 그 반대다.
        오라클 방식에서도 코퍼스가 한쪽으로 쏠리면 검출력이 사라진다.
        """
        lab = _lab(tmp_path)
        verdicts = [_oracle(make(tmp_path, lab)) for make in _CORPUS.values()]
        assert any(verdicts), "코퍼스에 수용되는 값이 하나도 없다"
        assert not all(verdicts), "코퍼스에 거부되는 값이 하나도 없다"
        assert len(verdicts) == len(_CORPUS) >= 15, f"코퍼스 {len(verdicts)}행"


def _run_guard_out_of_process(value, *, env=None, argv=(), flags=(), pre=""):
    """가드를 **별도 프로세스**에서 돌려 수용/거부를 관측한다.

    소스를 문법으로 검사하던 판은 우회가 끝이 없었다. `os.environ` 이라는 attr 모양,
    `sys` 라는 이름, 노드 종류 — 파이썬으로 같은 일을 하는 문법은 값 변형만큼 무한하다
    (적대 검증 실증, 2026-08-10: M1~M11 전원 통과). 값 축에서 오라클이 한 일을
    출처 축에서는 이 함수가 한다 — **문법이 아니라 결과를 본다.**

    서브프로세스인 것이 결정적이다. `if "pytest" not in sys.modules: return` 처럼
    **테스트 안에서만 동작하는 가드**가 원리적으로 불가능해진다.
    """
    import json as _json
    import os as _os
    import subprocess
    import sys as _sys

    import opencrab.pack.live_data as _m
    root = str(pathlib.Path(_m.__file__).resolve().parents[2])
    script = (
        f"{pre}\n"
        "import sys, json\n"
        "from opencrab.pack.live_data import require_live_data\n"
        "try:\n"
        "    require_live_data('load_nodes')\n"
        "    print(json.dumps({'accept': True}))\n"
        "except SystemExit as e:\n"
        "    print(json.dumps({'accept': False, 'msg': str(e)}))\n"
    )
    child = dict(_os.environ)
    child.pop("OPENCRAB_SKIP_LIVE_GUARD", None)
    if value is None:
        child.pop("LOCAL_DATA_DIR", None)      # 미설정 — 폴백 분기에 닿는 유일한 맥락
    else:
        child["LOCAL_DATA_DIR"] = value
    child["PYTHONPATH"] = root + _os.pathsep + child.get("PYTHONPATH", "")
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    child.update(env or {})
    out = subprocess.run(
        [_sys.executable, "-B", *flags, "-c", script, *argv],
        capture_output=True, text=True, env=child, timeout=60,
    )
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    assert line, f"자식 프로세스가 판정을 안 냈다: rc={out.returncode} err={out.stderr[:400]}"
    return _json.loads(line[-1])["accept"]


class TestVerdictIsInvariantAcrossExecutionContexts:
    """**가드의 판정은 `LOCAL_DATA_DIR` 값에만 의존한다.**

    값 축은 오라클이 닫았다. 이 축은 "값이 아니라 **실행 맥락**으로 분기하는" 형태이고,
    어떤 값 코퍼스로도 원리적으로 못 잡는다. 앞선 판은 소스를 AST 로 봉인해 닫으려
    했는데 문법 우회가 11종 전부 통과했다(2026-08-10: M1~M11). 그중:

        platform.system() == "Linux" 면 return
        -> macOS 개발기 스위트는 초록, **Linux 운영기에서만 가드가 통째로 무효**
        from sys import argv 로 --force 백도어      import sys as _s 로 pytest 감지
        from os import environ / getattr(os,"environ") / os.environ.copy() / os.environb
        다른 모듈에 백도어를 두고 import              ClassDef 본문 · 기본인자 평가 · __debug__

    문법을 열거하는 대신 **결과를 관측**한다. 맥락 6종 × 값 3종 = 18 케이스로 유한하고,
    다음에 나올 새 우회 문법도 자동으로 걸린다.
    """

    @staticmethod
    def _values(tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        afile = tmp_path / "afile"
        afile.write_text("x")
        return {"실재 디렉터리": str(real), "없는 경로": str(tmp_path / "nope"),
                "파일": str(afile)}

    def test_unset_stays_rejected_even_when_other_env_points_somewhere_real(self, tmp_path):
        """`LOCAL_DATA_DIR` 미설정이면 **다른 env 가 실재를 가리켜도** 거부다.

        폴백 분기(`environ.get(K) or environ.get("DATA_DIR")`)는 그 키가 **없을 때만**
        닿는다. 위 맥락들은 값을 항상 설정하므로 폴백에 도달하지 못한다
        (자체 측정: `from os import environ` 폴백이 82 passed 로 생존).
        """
        real = tmp_path / "real"
        real.mkdir()
        for other in ("DATA_DIR", "OPENCRAB_DATA_DIR", "HOME"):
            assert _run_guard_out_of_process(None, env={other: str(real)}) is False, (
                f"{other} 가 실재를 가리킨다고 미설정이 통과했다 — 폴백 키가 끼어 있다")

    @pytest.mark.parametrize("value_kind", ["실재 디렉터리", "없는 경로", "파일"])
    @pytest.mark.parametrize("ctx", [
        "기준", "무관 env 대량", "argv 잡음", "최적화 모드", "platform=Linux", "hostname 변조",
    ])
    def test_verdict_matches_the_oracle_in_every_context(self, ctx, value_kind, tmp_path):
        vals = self._values(tmp_path)
        value = vals[value_kind]
        opts = {
            "기준": {},
            "무관 env 대량": {"env": {"DATA_DIR": vals["실재 디렉터리"],
                                    "OPENCRAB_SKIP_LIVE_GUARD": "1",
                                    "OPENCRAB_FORCE": "1"}},
            "argv 잡음": {"argv": ["--force", "--yes"]},
            "최적화 모드": {"flags": ["-O"]},
            "platform=Linux": {"pre": "import platform; platform.system = lambda: 'Linux'"},
            "hostname 변조": {"pre": "import socket; socket.gethostname = lambda: 'prod-01'"},
        }[ctx]
        got = _run_guard_out_of_process(value, **opts)
        assert got is _oracle(value), (
            f"맥락 {ctx!r} 에서 판정이 갈렸다 — 가드가 값 이외의 것에 의존한다. "
            f"값={value_kind} 기대={_oracle(value)} 실제={got}")


class TestSourceSealingWhitelist:
    """소스 봉인은 **화이트리스트**로 한다 — 블랙리스트는 새 문법이 계속 샌다.

    앞선 판은 `environ` 이라는 attr, `sys` 라는 이름, 실행문이라는 노드 종류를
    **금지**했다. 파이썬은 같은 일을 다른 문법으로 하는 방법이 무한하므로 그 방식은
    끝나지 않는다(M1~M11). 허용 집합은 유한하고, 정당한 리팩터가 새 이름을 필요로 하면
    **그때 한 줄 추가하며 리뷰가 발생한다** — 이게 결정적 차이다.

    위 실행 맥락 불변성이 주 방어고, 이 검사는 심층 방어다.
    """

    _ALLOWED_IMPORTS = {"os", "sys", "pathlib", "__future__"}

    @staticmethod
    def _tree():
        import opencrab.pack.live_data as m
        return ast.parse(pathlib.Path(m.__file__).read_text(encoding="utf-8"))

    def test_only_expected_modules_are_imported(self):
        got = set()
        for n in ast.walk(self._tree()):
            if isinstance(n, ast.Import):
                got |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                got.add(n.module.split(".")[0])
        extra = sorted(got - self._ALLOWED_IMPORTS)
        assert not extra, (
            f"허용 밖 모듈을 import 한다: {extra}. 허용 집합은 {sorted(self._ALLOWED_IMPORTS)} — "
            "정당한 필요라면 이 목록에 추가하되 **왜 필요한지 리뷰를 받아라**. "
            "`platform`·`socket` 같은 실행 맥락 모듈이 여기서 걸린다")

    def test_top_level_assignments_are_literal_only(self):
        """최상위 할당은 **우변이 리터럴일 때만** 허용한다.

        노드 **종류**로 금지하던 판은 `__all__ = [...]`·메시지 상수·`if TYPE_CHECKING:`
        같은 표준 관용구까지 막았다(적대 검증 지적: C1·C2·C7 — 백도어 위험 0인데 거부).
        우변의 성질로 판정하면 그것들은 살아나고 `_SKIP = os.environ.get(...)` 은 계속 죽는다.
        """
        bad = []
        for node in self._tree().body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            rhs = node.value
            if rhs is None:
                continue
            if any(isinstance(x, (ast.Call, ast.Attribute, ast.Name))
                   for x in ast.walk(rhs)):
                bad.append(f"L{node.lineno}: {ast.unparse(node)[:70]}")
        assert not bad, (
            "최상위 할당의 우변이 리터럴이 아니다 — 백도어·캐시가 끼어드는 자리다.\n  "
            + "\n  ".join(bad)
            + "\n허용: docstring · from __future__ · import · def · class · "
              "리터럴 할당 · if TYPE_CHECKING 블록")


class TestRejectsUnusableTargets:
    def test_unset_aborts(self):
        with pytest.raises(SystemExit) as ei:
            require_live_data()
        assert "미설정" in str(ei.value)

    def test_empty_string_aborts_like_unset(self, monkeypatch):
        """빈 문자열은 "설정됨"이 아니다.

        `if not d` 를 `if d is None` 으로 바꾸면 빈 값이 통과해 `Path("")` 가
        되고, `Path("").is_dir()` 은 **cwd 를 가리켜 True 다** — 즉 가드가 통째로
        뚫린다. 이 케이스가 없으면 그 변이를 아무도 못 잡는다.
        """
        monkeypatch.setenv("LOCAL_DATA_DIR", "")
        with pytest.raises(SystemExit) as ei:
            require_live_data()
        assert "미설정" in str(ei.value)

    def test_missing_path_aborts(self, monkeypatch, tmp_path):
        gone = tmp_path / "not-there"
        monkeypatch.setenv("LOCAL_DATA_DIR", str(gone))
        with pytest.raises(SystemExit) as ei:
            require_live_data()
        assert "경로 없음" in str(ei.value)
        assert str(gone) in str(ei.value), "어느 경로가 문제인지 말해야 한다"

    def test_lexically_normalising_the_path_must_not_admit_it(self, monkeypatch, tmp_path):
        """`Path(d).is_dir()` 를 `Path(d).resolve().is_dir()` 로 "개선"하면 가드가 약해진다.

        `resolve()` 는 **존재하지 않는 중간 요소를 낀 `..` 를 렉시컬하게 접는다.**
        아래 경로에서 `ghost` 는 없는데 `resolve()` 는 그것을 지워 `real` 로 만들어
        버리므로, 원본 가드가 정상 거부하던 경로가 통과한다:

            Path(tmp/ghost/../real).is_dir()            -> False  (거부: 옳다)
            Path(tmp/ghost/../real).resolve().is_dir()  -> True   (통과: 가드 약화)

        심링크·`~` 처리 개선처럼 보이는 자연스러운 후속 변경이라 실제로 들어올 수 있다.
        기존 8건은 이 변이를 **전부 통과시켰다**(적대 검증 실증, 2026-08-06: N5).
        스테일 export 의 오타가 바로 이 형태를 만든다 — 없는 디렉터리를 거쳐 실재하는
        다른 저장소를 가리키는 경로.
        """
        (tmp_path / "real").mkdir()
        lexical = tmp_path / "ghost" / ".." / "real"
        assert not (tmp_path / "ghost").exists(), "전제: 중간 요소가 실재하면 안 된다"
        assert lexical.resolve().is_dir(), "전제: resolve() 하면 실재 디렉터리가 된다"

        monkeypatch.setenv("LOCAL_DATA_DIR", str(lexical))
        with pytest.raises(SystemExit) as ei:
            require_live_data("load_nodes")
        assert "경로 없음" in str(ei.value)

    def test_file_is_not_a_directory(self, monkeypatch, tmp_path):
        """`is_dir()` 을 `exists()` 로 바꾸는 변이를 잡는다.

        존재하지 않는 경로만 입력으로 쓰면 두 함수의 결과가 같아 갈리지 않는다.
        **파일**을 줘야 갈린다 — 등가를 측정할 때 입력이 변이가 건드리는 축을
        갈라야 한다는 규칙의 적용이다.
        """
        f = tmp_path / "graph.db"
        f.write_text("not a dir")
        monkeypatch.setenv("LOCAL_DATA_DIR", str(f))
        with pytest.raises(SystemExit) as ei:
            require_live_data()
        assert "경로 없음" in str(ei.value)


class TestAcceptsLiveTarget:
    def test_existing_directory_passes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        assert require_live_data() is None

    def test_guard_never_creates_the_directory(self, monkeypatch, tmp_path):
        """가드가 대상 디렉터리를 **만들면 안 된다.**

        같은 패키지의 `opencrab.config.local_data_dir` 은 홈 파생 기본값을 만들고
        `opencrab.mcp.tools._lock_data_dir` 은 없으면 `os.makedirs` 로 만든다. 둘 다
        "일할 자리를 마련한다"는 목적이라 옳지만, 이 가드는 정반대다 — 비어 있는 새
        디렉터리에 적재가 성공하면 그건 성공이 아니라 엉뚱한 곳에 쓴 것이다.

        두 정책이 한 패키지에 있게 됐으므로 "가드는 만들지 않는다"를 코드로 못박는다.
        가드 안에 `mkdir`/`makedirs` 가 끼어드는 순간 이 테스트가 죽는다.
        """
        gone = tmp_path / "must-stay-absent"
        monkeypatch.setenv("LOCAL_DATA_DIR", str(gone))
        with pytest.raises(SystemExit):
            require_live_data()
        assert not gone.exists(), "가드가 대상 디렉터리를 만들었다 — 가드가 무력해진다"


class TestContextLabel:
    def test_ctx_appears_in_both_messages(self, monkeypatch, tmp_path):
        """어느 쓰기 함수가 걸렸는지 말해야 한다.

        두 exit 경로를 **다 건다.** 한쪽만 걸면 다른 쪽에서 `suffix` 를 떨어뜨리는
        변이가 살아남는다.
        """
        with pytest.raises(SystemExit) as unset:
            require_live_data("load_nodes")
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path / "nope"))
        with pytest.raises(SystemExit) as missing:
            require_live_data("load_edges")
        assert "[load_nodes]" in str(unset.value)
        assert "[load_edges]" in str(missing.value)

    def test_no_ctx_leaves_no_empty_brackets(self):
        """ctx 기본값에서 `[]` 가 붙으면 안 된다 — `suffix` 조건문을 지우는 변이."""
        with pytest.raises(SystemExit) as ei:
            require_live_data()
        assert "[" not in str(ei.value)
