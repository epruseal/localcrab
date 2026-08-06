"""``opencrab.pack.live_data.require_live_data`` 계약.

이 가드는 적재 계층이 **엉뚱한 저장소에 쓰는 것**을 막는 유일한 방벽이다. 이관 전에는
호출자 리포의 정적 게이트가 "쓰기 함수 전량이 이 가드를 부르는가"를 AST 로 검사했다.
적재 함수가 이 패키지로 넘어오면 그 검사는 남의 리포 파일을 뒤지게 되므로, 계약을
**여기서** 건다.

이 파일이 거는 것은 가드 **자체의 행동**이다. "쓰기 함수 전량이 이것을 부르는가"라는
커버리지 계약은 적재 함수가 넘어오는 시점에 별도로 건다.
"""
from __future__ import annotations

import pytest

from opencrab.pack.live_data import require_live_data


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)


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
