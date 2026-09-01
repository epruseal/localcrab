"""Agent Plugin 릴리스 세트(checksum + compatibility report) 테스트 (이슈 #247).

설계 정본 체인: design-v3.md 가 design-v2.md 를 개정한다(각 [S#] 델타가 v2 해당 절을 대체).

이 파일은 이슈 #137 의 `tests/test_agent_plugin_packaging.py` 와 별도 파일이다(o248 축과의
충돌 회피). fake repo 헬퍼는 이 파일 안에 자체 보유하고 다른 테스트 파일에서 import 하지
않는다. 레포 관례를 따른다: 클래스 단위 그룹핑, 한국어 주석/독스트링, 실제 자원 무접촉
(포트 8765/8766·systemd·~/.openclaw 미사용 -- tmp_path 와 실 repo(읽기 전용)만 다룬다).

TDD RED 단계: `packaging/agent-plugin/tools/build.py` 에 `_safe_version`,
`write_compat_report`, `_deterministic_tar`, `build_release`, `verify_release` 가 아직
구현되지 않았다. 이 시점에는 AttributeError 로 실패하는 것이 올바른 결과다 -- 구현이 들어오면
이 스위트가 green 이 되는 것으로 완료를 판정한다.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import io
import json
import os
import re
import signal
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging" / "agent-plugin"))

from tools import build as b  # noqa: E402

COMPAT_DOC_PATH = REPO / "docs" / "agent-plugin-compatibility.md"


# ---------------------------------------------------------------------------
# 공용 헬퍼 · 픽스처 (이 파일 자체 보유 -- 다른 테스트 파일에서 import 하지 않는다)
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _valid_manifest(**overrides) -> dict:
    manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "localcrab",
        "version": "0.1.0",
        "description": "Local-first MetaOntology knowledge service.",
        "author": {"name": "OpenCrab Contributors"},
        "license": "MIT",
    }
    manifest.update(overrides)
    return manifest


def _valid_mcp_obj(**overrides) -> dict:
    obj = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            "localcrab": {
                "type": "stdio",
                "command": "opencrab",
                "args": ["serve"],
                "cwd": "${PLUGIN_DATA}",
                "env": {
                    "STORAGE_MODE": "local",
                    "LOCAL_DATA_DIR": "${PLUGIN_DATA}",
                    "LOCALCRAB_ENV_FILE": "${PLUGIN_DATA}/localcrab.env",
                },
            }
        },
    }
    obj.update(overrides)
    return obj


_FAKE_COMPAT_DOC = """# 호환성 스텁

이 문서는 테스트용 fake repo 의 compat 정본 스텁이다.

## Compatibility matrix

| | 설치 방식 | stdio |
|---|---|---|
| **OpenClaw** | 로컬 디렉터리 install | 지원 |
| **Claude Code** | 수동 매핑 필요 | 자체 포맷 재작성 |
"""

_FAKE_COMPAT_DOC_TABLE_IN_FENCE_ONLY = """# 호환성 스텁 (표가 펜스 안에만 있음)

```
| | 설치 방식 |
|---|---|
| **OpenClaw** | 지원 |
```

본문에는 표가 없다.
"""

_FAKE_COMPAT_DOC_NO_TABLE = """# 호환성 스텁 (표 없음)

본문에 마크다운 표가 전혀 없다.
"""


def _fake_repo(
    tmp_path: Path,
    *,
    version: str = "0.1.0",
    with_compat_doc: bool = True,
    compat_text: str | None = None,
) -> Path:
    """build_release()/verify_release() 를 빠르게·격리해서 테스트하기 위한 합성 미니 레포."""
    repo = tmp_path / "fake-repo"
    src = repo / "packaging" / "agent-plugin" / "src"
    (src / "skills" / "localcrab-query").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "opencrab"\nversion = "{version}"\n', encoding="utf-8"
    )
    (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    _write_json(src / "plugin.json", _valid_manifest(version=version))
    _write_json(src / "mcp.json", _valid_mcp_obj())
    (src / "README.md").write_text("# Fake plugin\n\nNo secrets here.\n", encoding="utf-8")
    (src / "skills" / "localcrab-query" / "SKILL.md").write_text(
        "---\nname: localcrab-query\ndescription: test skill description text, long enough.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    if with_compat_doc:
        docs_dir = repo / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "agent-plugin-compatibility.md").write_text(
            compat_text if compat_text is not None else _FAKE_COMPAT_DOC, encoding="utf-8"
        )
    return repo


def _bump_version(repo: Path, version: str) -> None:
    """fake repo 의 pyproject.toml + plugin.json version 을 함께 갱신한다."""
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "opencrab"\nversion = "{version}"\n', encoding="utf-8"
    )
    manifest_path = repo / "packaging" / "agent-plugin" / "src" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    _write_json(manifest_path, manifest)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar_names(archive_path: Path) -> list[str]:
    with tarfile.open(archive_path, mode="r:gz") as tar:
        return [m.name for m in tar.getmembers()]


def _rewrite_release_file(release_path: Path, entries: dict[str, str]) -> None:
    lines = [f"{digest}  {name}" for name, digest in sorted(entries.items())]
    release_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _recompute_release(out_dir: Path, version: str) -> None:
    """RELEASE.SHA256SUMS 를 out_dir 의 현재 파일 상태에 맞춰 전량 재계산한다.

    TestVerifyRelease 의 성격규정(전량 재계산 세트는 통과) 테스트에서 사용한다.
    """
    release_path = out_dir / f"localcrab-plugin-{version}.RELEASE.SHA256SUMS"
    names = [
        f"localcrab-plugin-{version}.tar.gz",
        f"localcrab-plugin-{version}.COMPATIBILITY.md",
        "localcrab-plugin.SHA256SUMS",
    ]
    entries = {name: _sha256(out_dir / name) for name in names}
    _rewrite_release_file(release_path, entries)


def _count_tarfile_open_calls(monkeypatch) -> dict[str, int]:
    """PR #257 리뷰 라운드 2 [X1] 게이트 검증용: `tarfile.open` 호출 횟수를 계측한다
    (`build.py` 는 `import tarfile` 을 통해 모듈 전역으로 부르므로 `b.tarfile.open` 을
    패치하면 verify_release 내부 호출까지 그대로 계측된다)."""
    calls = {"n": 0}
    original = tarfile.open

    def spy(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(b.tarfile, "open", spy)
    return calls


class _SpyLimitedReader(getattr(b, "_LimitedReader", object)):
    """[Y1] 전개 예산 계측용 감시 래퍼 -- 마지막으로 생성된 인스턴스를 클래스 변수에
    남겨, verify_release 종료 후에도 소비량(`consumed`)을 테스트에서 확인할 수 있게
    한다. `b._LimitedReader` 를 그대로 상속하므로 동작은 실제 클래스와 동일하다.

    RED 단계(구현 전)에는 `b._LimitedReader` 가 아직 없으므로 `object` 를 기반으로
    삼는다 -- 이 클래스를 실제로 쓰는 테스트만 개별적으로 실패하고, 모듈 수집 자체가
    깨지지 않도록 하기 위함이다(`getattr(b, "_LimitedReader", object)` 는 import 시점에
    평가되므로, RED 커밋 시점에는 이 파일이 `object` 기반으로 정의된다는 뜻이다)."""

    last_instance: _SpyLimitedReader | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        type(self).last_instance = self


def _write_plain_tar_gz(archive_path: Path, members: list[tuple[str, bytes]]) -> None:
    """단순 멤버 목록으로 tar.gz 를 새로 작성한다(표준 GNU 포맷, 압축 wrap)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive_path.write_bytes(gzip.compress(buf.getvalue()))


def _write_sparse_bomb_tar_gz(archive_path: Path, name: str, realsize: int) -> None:
    """PAX `GNU.sparse.map`/`GNU.sparse.realsize` 헤더를 직접 주입해, 물리 크기는
    작지만 reader 관점에서 `issparse()==True` 이고 `member.size==realsize` 인 멤버를
    담은 tar.gz 를 만든다(본 세션 실측 기법 -- 실제 파일시스템 sparse 지원 불필요)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(name=name)
        data = b"x"
        info.size = len(data)
        info.pax_headers = {"GNU.sparse.map": "0,1", "GNU.sparse.realsize": str(realsize)}
        tar.addfile(info, io.BytesIO(data))
    archive_path.write_bytes(gzip.compress(buf.getvalue()))


# ---------------------------------------------------------------------------
# TestCompatReport
# ---------------------------------------------------------------------------


class TestCompatReport:
    """compat 정본 문서(docs/agent-plugin-compatibility.md) 로부터 report 생성."""

    def test_real_repo_report_generation_succeeds(self, tmp_path):
        report_path = b.write_compat_report(REPO, tmp_path, "9.9.9")
        assert report_path.is_file()
        assert report_path.name == "localcrab-plugin-9.9.9.COMPATIBILITY.md"

    def test_real_repo_report_body_is_verbatim_suffix(self, tmp_path):
        report_path = b.write_compat_report(REPO, tmp_path, "9.9.9")
        report_bytes = report_path.read_bytes()
        compat_bytes = COMPAT_DOC_PATH.read_bytes()
        assert report_bytes.endswith(compat_bytes)
        # preamble 이 실재해서 report 가 정본 파일 그 자체의 복사본은 아님을 확인한다.
        assert len(report_bytes) > len(compat_bytes)

    def test_real_compat_doc_has_no_relative_markdown_links(self):
        text = COMPAT_DOC_PATH.read_text(encoding="utf-8")
        assert "](./" not in text
        assert "](../" not in text
        # reference-style 링크 정의(`[label]: ./path`)도 배제한다.
        assert not re.search(r"^\[[^\]]+\]:\s*\.{1,2}/", text, flags=re.MULTILINE)

    def test_real_compat_doc_has_matrix_table_with_client_rows(self):
        text = COMPAT_DOC_PATH.read_text(encoding="utf-8")
        assert "**OpenClaw**" in text
        assert "**Claude Code**" in text
        assert re.search(r"^\|[-|: ]+\|\s*$", text, flags=re.MULTILINE)

    def test_missing_compat_doc_raises(self, tmp_path):
        repo = _fake_repo(tmp_path, with_compat_doc=False)
        with pytest.raises(b.BuildError):
            b.write_compat_report(repo, tmp_path / "out", "1.0.0")

    def test_table_only_inside_fenced_block_raises(self, tmp_path):
        repo = _fake_repo(tmp_path, compat_text=_FAKE_COMPAT_DOC_TABLE_IN_FENCE_ONLY)
        with pytest.raises(b.BuildError):
            b.write_compat_report(repo, tmp_path / "out", "1.0.0")

    def test_no_table_at_all_raises(self, tmp_path):
        repo = _fake_repo(tmp_path, compat_text=_FAKE_COMPAT_DOC_NO_TABLE)
        with pytest.raises(b.BuildError):
            b.write_compat_report(repo, tmp_path / "out", "1.0.0")

    def test_regeneration_is_byte_identical(self, tmp_path):
        repo = _fake_repo(tmp_path)
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        p1 = b.write_compat_report(repo, out1, "1.0.0")
        p2 = b.write_compat_report(repo, out2, "1.0.0")
        assert p1.read_bytes() == p2.read_bytes()


# ---------------------------------------------------------------------------
# TestBuildRelease
# ---------------------------------------------------------------------------


class TestBuildRelease:
    """build_release(): 산출물 실재·명명·구성·기존 build() 후방호환."""

    def test_release_set_files_exist_and_named(self, tmp_path):
        repo = _fake_repo(tmp_path, version="2.5.1")
        out_dir = tmp_path / "dist"
        staged_root = b.build_release(repo, out_dir)
        assert staged_root == out_dir / "localcrab-plugin"
        assert staged_root.is_dir()
        assert (out_dir / "localcrab-plugin.SHA256SUMS").is_file()
        assert (out_dir / "localcrab-plugin-2.5.1.tar.gz").is_file()
        assert (out_dir / "localcrab-plugin-2.5.1.COMPATIBILITY.md").is_file()
        assert (out_dir / "localcrab-plugin-2.5.1.RELEASE.SHA256SUMS").is_file()
        # 신규 산출물은 plugin root 밖(형제 파일)이다.
        assert not (staged_root / "localcrab-plugin-2.5.1.tar.gz").exists()
        assert not (staged_root / "localcrab-plugin-2.5.1.COMPATIBILITY.md").exists()
        assert not (staged_root / "localcrab-plugin-2.5.1.RELEASE.SHA256SUMS").exists()

    def test_release_sha256sums_is_sorted_three_entries_sha256sum_c_format(self, tmp_path):
        repo = _fake_repo(tmp_path, version="3.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        release_path = out_dir / "localcrab-plugin-3.0.0.RELEASE.SHA256SUMS"
        lines = [ln for ln in release_path.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 3
        for line in lines:
            assert re.fullmatch(r"[0-9a-f]{64}  \S+", line)
        names = [ln.split("  ", 1)[1] for ln in lines]
        assert names == sorted(names)
        assert set(names) == {
            "localcrab-plugin-3.0.0.tar.gz",
            "localcrab-plugin-3.0.0.COMPATIBILITY.md",
            "localcrab-plugin.SHA256SUMS",
        }
        for line in lines:
            digest, name = line.split("  ", 1)
            assert digest == _sha256(out_dir / name)

    def test_archive_member_set_matches_staged_allowlist(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.1.1")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        names = _tar_names(out_dir / "localcrab-plugin-1.1.1.tar.gz")
        rels = {n[len("localcrab-plugin/") :] for n in names}
        assert rels == b.STAGED_ALLOWLIST
        assert all(n.startswith("localcrab-plugin/") for n in names)

    def test_build_alone_does_not_produce_release_artifacts(self, tmp_path):
        repo = _fake_repo(tmp_path, version="4.4.4")
        out_dir = tmp_path / "dist"
        b.build(repo, out_dir)
        assert not (out_dir / "localcrab-plugin-4.4.4.tar.gz").exists()
        assert not (out_dir / "localcrab-plugin-4.4.4.COMPATIBILITY.md").exists()
        assert not (out_dir / "localcrab-plugin-4.4.4.RELEASE.SHA256SUMS").exists()
        assert (out_dir / "localcrab-plugin").is_dir()
        assert (out_dir / "localcrab-plugin.SHA256SUMS").is_file()

    @pytest.mark.parametrize("bad_version", ["../evil", "1.0.0/../etc", "/abs", "", ".hidden"])
    def test_unsafe_version_filename_rejected(self, tmp_path, bad_version):
        repo = _fake_repo(tmp_path, version=bad_version or "0.0.0")
        if bad_version == "":
            # 빈 버전 문자열도 별도 픽스처로 검증 -- pyproject/manifest 에 직접 주입.
            (repo / "pyproject.toml").write_text('[project]\nname = "opencrab"\nversion = ""\n', encoding="utf-8")
            manifest_path = repo / "packaging" / "agent-plugin" / "src" / "plugin.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = ""
            _write_json(manifest_path, manifest)
        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build_release(repo, out_dir)

    def test_stale_version_files_removed_on_rebuild(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        assert (out_dir / "localcrab-plugin-1.0.0.tar.gz").exists()

        _bump_version(repo, "2.0.0")
        b.build_release(repo, out_dir)

        assert not (out_dir / "localcrab-plugin-1.0.0.tar.gz").exists()
        assert not (out_dir / "localcrab-plugin-1.0.0.COMPATIBILITY.md").exists()
        assert not (out_dir / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS").exists()
        assert (out_dir / "localcrab-plugin-2.0.0.tar.gz").exists()
        assert (out_dir / "localcrab-plugin-2.0.0.COMPATIBILITY.md").exists()
        assert (out_dir / "localcrab-plugin-2.0.0.RELEASE.SHA256SUMS").exists()
        release_files = list(out_dir.glob("localcrab-plugin-*.RELEASE.SHA256SUMS"))
        assert len(release_files) == 1
        tar_files = list(out_dir.glob("localcrab-plugin-*.tar.gz"))
        assert len(tar_files) == 1


# ---------------------------------------------------------------------------
# TestBuildReleaseAtomicity (v3 [S1])
# ---------------------------------------------------------------------------


class _InjectedReplaceError(OSError):
    """os.replace 주입 실패임을 무관 예외와 구분하기 위한 전용 예외 클래스 (F2 강화)."""


class TestBuildReleaseAtomicity:
    """build_release() 게시 원자성: 조립 실패는 기존 게시물을 건드리지 않고, 부분 게시는
    verify_release 에서 fail-closed 로 드러난다."""

    def test_assembly_failure_leaves_existing_publish_intact(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        b.verify_release(out_dir)  # baseline: A 는 유효하다.

        before = {
            p.name: p.read_bytes()
            for p in out_dir.iterdir()
            if p.is_file()
        }

        # 정본 문서를 제거해 두 번째 build_release 호출(같은 버전, 재조립)을 실패시킨다.
        (repo / "docs" / "agent-plugin-compatibility.md").unlink()
        with pytest.raises(b.BuildError):
            b.build_release(repo, out_dir)

        # out_dir 의 A 세트가 완전히 무손상이어야 한다.
        after = {p.name: p.read_bytes() for p in out_dir.iterdir() if p.is_file()}
        assert after == before
        b.verify_release(out_dir)  # A 는 여전히 유효하다.
        assert not (out_dir / ".release-build-tmp").exists()

    @pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
    def test_partial_publish_fails_closed(self, tmp_path, monkeypatch, fail_at):
        """게시 경로의 5개 os.replace 지점 각각에서 주입 실패가 실제로 도달하고,
        build_release 가 그 원인을 보존한 BuildError 로 래핑하며, 결과 부분 게시 상태가
        verify_release 에서 fail-closed 로 드러남을 확인한다 (F2 강화).

        `pytest.raises(Exception)` 만으로는 주입과 무관한 예외로도 우연히 통과할 수 있고
        주입 지점에 실제 도달했는지도 확인하지 않는다 -- 고유 예외 타입 + 도달 카운터 +
        원인 체인(`__cause__`) 3중 assert 로 이를 막는다.
        """
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"

        real_replace = os.replace
        call_count = {"n": 0}

        def fake_replace(src, dst, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == fail_at:
                raise _InjectedReplaceError(f"injected failure at call {fail_at}")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(b.os, "replace", fake_replace)

        # build_release 의 게시 단계는 os.replace 실패(OSError 계열)를 BuildError 로
        # 래핑한다(`raise BuildError(...) from exc`) -- 조립 단계에서 발생하는 BuildError
        # 는 그대로 재던져지지만, 게시 단계는 항상 이 경로를 지난다.
        with pytest.raises(b.BuildError) as exc_info:
            b.build_release(repo, out_dir)

        monkeypatch.undo()

        # 주입 지점(k번째 os.replace 호출)에 실제로 도달했음을 확인한다 -- 예외가 그
        # 호출에서 발생해 이후 호출은 일어나지 않으므로 카운터는 정확히 fail_at 이어야 한다.
        assert call_count["n"] == fail_at
        # 무관한 예외로 우연히 통과하지 않도록 원인 체인의 타입까지 확인한다.
        assert isinstance(exc_info.value.__cause__, _InjectedReplaceError)

        # 부분 게시 상태는 RELEASE 마커 부재/불일치로 반드시 시끄럽게 실패해야 한다.
        with pytest.raises(b.BuildError):
            b.verify_release(out_dir)


# ---------------------------------------------------------------------------
# TestReproducibility
# ---------------------------------------------------------------------------


class TestReproducibility:
    """다른 out_dir/시각에서 빌드해도 릴리스 세트 바이트가 동일하다."""

    def test_two_out_dirs_bytewise_identical(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.2.3")
        out1 = tmp_path / "dist1"
        out2 = tmp_path / "dist2"
        b.build_release(repo, out1)
        b.build_release(repo, out2)
        for suffix in (
            "localcrab-plugin-1.2.3.tar.gz",
            "localcrab-plugin-1.2.3.COMPATIBILITY.md",
            "localcrab-plugin-1.2.3.RELEASE.SHA256SUMS",
        ):
            assert (out1 / suffix).read_bytes() == (out2 / suffix).read_bytes()

    def test_tar_sha256_independent_of_source_mtime(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.2.3")
        out1 = tmp_path / "dist1"
        b.build_release(repo, out1)
        digest1 = _sha256(out1 / "localcrab-plugin-1.2.3.tar.gz")

        src = repo / "packaging" / "agent-plugin" / "src"
        for path in src.rglob("*"):
            if path.is_file():
                os.utime(path, (1_000_000, 1_000_000))

        out2 = tmp_path / "dist2"
        b.build_release(repo, out2)
        digest2 = _sha256(out2 / "localcrab-plugin-1.2.3.tar.gz")
        assert digest1 == digest2


# ---------------------------------------------------------------------------
# TestVerifyRelease
# ---------------------------------------------------------------------------


class TestVerifyRelease:
    """verify_release(): 무결성·일관성 검출. 성격규정: 전량 재계산은 검출되지 않는다."""

    @pytest.fixture
    def release(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        return out_dir

    def test_valid_release_passes(self, release):
        b.verify_release(release)  # 예외 없어야 함

    def test_archive_bitflip_detected(self, release):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        data = bytearray(archive.read_bytes())
        data[-1] ^= 0xFF
        archive.write_bytes(bytes(data))
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_compat_report_tampered_detected(self, release):
        report = release / "localcrab-plugin-1.0.0.COMPATIBILITY.md"
        report.write_text(report.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_package_sidecar_tampered_detected(self, release):
        sidecar = release / "localcrab-plugin.SHA256SUMS"
        sidecar.write_text(sidecar.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_release_entry_removed_shrinks_composition(self, release):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        lines = [ln for ln in release_path.read_text(encoding="utf-8").splitlines() if ln]
        kept = [ln for ln in lines if "COMPATIBILITY.md" not in ln]
        release_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_release_entry_absolute_path_rejected(self, release):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        digest = _sha256(release / "localcrab-plugin.SHA256SUMS")
        entries = {
            "localcrab-plugin-1.0.0.tar.gz": _sha256(release / "localcrab-plugin-1.0.0.tar.gz"),
            "localcrab-plugin-1.0.0.COMPATIBILITY.md": _sha256(
                release / "localcrab-plugin-1.0.0.COMPATIBILITY.md"
            ),
            "/etc/localcrab-plugin.SHA256SUMS": digest,
        }
        _rewrite_release_file(release_path, entries)
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_release_entry_dotdot_rejected(self, release):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        digest = _sha256(release / "localcrab-plugin.SHA256SUMS")
        entries = {
            "localcrab-plugin-1.0.0.tar.gz": _sha256(release / "localcrab-plugin-1.0.0.tar.gz"),
            "localcrab-plugin-1.0.0.COMPATIBILITY.md": _sha256(
                release / "localcrab-plugin-1.0.0.COMPATIBILITY.md"
            ),
            "../localcrab-plugin.SHA256SUMS": digest,
        }
        _rewrite_release_file(release_path, entries)
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_release_duplicate_entry_rejected(self, release):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        digest = _sha256(release / "localcrab-plugin-1.0.0.tar.gz")
        line = f"{digest}  localcrab-plugin-1.0.0.tar.gz"
        text = release_path.read_text(encoding="utf-8").rstrip("\n") + "\n" + line + "\n"
        release_path.write_text(text, encoding="utf-8")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_archive_extra_member_outside_sidecar_rejected(self, release):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            contents = {m.name: tar.extractfile(m).read() for m in members if m.isfile()}
        contents["localcrab-plugin/EXTRA.txt"] = b"not in sidecar\n"
        with tarfile.open(archive, mode="w:gz") as tar:
            for name, data in sorted(contents.items()):
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        _recompute_release(release, "1.0.0")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_archive_duplicate_member_name_rejected(self, release):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            contents = [(m.name, tar.extractfile(m).read()) for m in members if m.isfile()]
        with tarfile.open(archive, mode="w:gz") as tar:
            for name, data in contents:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
            # 첫 멤버를 동일 이름으로 한 번 더 추가한다(동명 중복).
            dup_name, dup_data = contents[0]
            info = tarfile.TarInfo(name=dup_name)
            info.size = len(dup_data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(dup_data))
        _recompute_release(release, "1.0.0")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_archive_symlink_member_rejected(self, release):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            contents = [(m.name, tar.extractfile(m).read()) for m in members if m.isfile()]
        with tarfile.open(archive, mode="w:gz") as tar:
            # 첫 멤버를 실제 파일 대신 symlink 멤버로 대체한다.
            first_name, _ = contents[0]
            link_info = tarfile.TarInfo(name=first_name)
            link_info.type = tarfile.SYMTYPE
            link_info.linkname = "/etc/passwd"
            tar.addfile(link_info)
            for name, data in contents[1:]:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        _recompute_release(release, "1.0.0")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_zero_release_markers_rejected(self, release):
        (release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS").unlink()
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_multiple_release_markers_rejected(self, release):
        original = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        duplicate = release / "localcrab-plugin-9.9.9.RELEASE.SHA256SUMS"
        duplicate.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    # -- PR #257 리뷰 P2: 수신자 검증 경로의 읽기 오류 방호 (fix-design-v2) -------------

    def test_release_file_non_utf8_bytes_rejected(self, release):
        """RELEASE.SHA256SUMS 가 비 UTF-8(손상)이면 UnicodeDecodeError 가 아니라
        BuildError 로 종료해야 한다(pytest.raises 가 타입을 고정한다)."""
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        data = bytearray(release_path.read_bytes())
        data[3] = 0xFF
        release_path.write_bytes(bytes(data))
        with pytest.raises(b.BuildError):
            b.verify_release(release)

    def test_release_file_permission_denied_rejected(self, release):
        """RELEASE.SHA256SUMS 를 읽을 수 없으면(권한) BuildError 로 종료해야 한다."""
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        original_mode = release_path.stat().st_mode
        os.chmod(release_path, 0o000)
        try:
            try:
                release_path.read_bytes()
            except OSError:
                pass
            else:
                pytest.skip(
                    "현재 환경에서 chmod 0o000 이 읽기를 막지 못한다(root 등 권한 강제 미적용) -- 스킵"
                )
            with pytest.raises(b.BuildError):
                b.verify_release(release)
        finally:
            os.chmod(release_path, original_mode)

    def test_sidecar_non_utf8_bytes_rejected_with_message(self, release):
        """패키지 사이드카가 비 UTF-8 이면 BuildError 위반 메시지에 '패키지 사이드카' 와
        'UTF-8' 이 함께 실려야 한다(다른 분기로 우연히 통과하는 것을 방지)."""
        sidecar_path = release / "localcrab-plugin.SHA256SUMS"
        data = bytearray(sidecar_path.read_bytes())
        data[3] = 0xFF
        sidecar_path.write_bytes(bytes(data))
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        message = str(exc_info.value)
        assert "패키지 사이드카" in message
        assert "UTF-8" in message

    def test_release_entry_file_permission_denied_rejected(self, release):
        """RELEASE 항목 파일(tar.gz)을 읽을 수 없으면 BuildError 위반 목록에 '읽을 수
        없다' 가 실려야 한다."""
        archive_path = release / "localcrab-plugin-1.0.0.tar.gz"
        original_mode = archive_path.stat().st_mode
        os.chmod(archive_path, 0o000)
        try:
            try:
                archive_path.read_bytes()
            except OSError:
                pass
            else:
                pytest.skip(
                    "현재 환경에서 chmod 0o000 이 읽기를 막지 못한다(root 등 권한 강제 미적용) -- 스킵"
                )
            with pytest.raises(b.BuildError) as exc_info:
                b.verify_release(release)
            assert "읽을 수 없다" in str(exc_info.value)
        finally:
            os.chmod(archive_path, original_mode)

    def test_full_recompute_set_passes_characterization(self, release):
        """성격규정(characterization) -- 진본성 비보장 경계를 명문화한다.

        설계 문구("전량(아카이브+사이드카+RELEASE) 재계산 세트는 통과")를 문자 그대로
        실증한다: COMPATIBILITY.md 만 바꾸는 게 아니라 **아카이브 멤버 내용
        (localcrab-plugin/README.md) 을 변조**하고, 그 변조된 아카이브를 빌더
        (`_deterministic_tar`)와 동일한 결정론 파라미터로 재작성한 뒤(USTAR 포맷,
        UTF-8 + errors="strict", 정렬된 멤버 순서, REGTYPE, uid=gid=0, uname=gname="",
        mtime=0, mode=0o644, gzip mtime=0/filename=""/compresslevel=9), 패키지 사이드카의
        해당 항목 해시와 RELEASE.SHA256SUMS 3항목을 전부 재계산한다. 즉 세 파일 전량을
        공격자가 다시 계산해 세트 내부를 일관되게 맞춘 뒤에도 verify_release 가 통과함을
        확인한다. 이는 결함이 아니라 verify_release 의 설계된 한계다: 무결성·일관성만
        검출하고 진본성은 검출하지 않는다(공격자가 아카이브·사이드카·RELEASE 세 파일을
        모두 통제하는 경우). 진본성 대사는 공표 기준값(운영 정책 문서)의 몫이다.
        """
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        sidecar = release / "localcrab-plugin.SHA256SUMS"
        target_member = f"{b._ARCHIVE_PREFIX}README.md"

        # 1. 기존 tar 를 읽어(닫은 뒤) 멤버 전체 내용을 손에 쥔다. README.md 내용만 변조.
        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            contents = {m.name: tar.extractfile(m).read() for m in members if m.isfile()}
        assert target_member in contents
        contents[target_member] = "공격자가 전량 재계산해 바꿔치기한 README\n".encode()

        # 2. 빌더(_deterministic_tar)와 동일한 결정론 파라미터로 아카이브를 재작성한다.
        #    (기존 tar 를 완전히 닫은 뒤 출력을 새로 연다 -- 동시에 열지 않는다.)
        with open(archive, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="", compresslevel=9) as gz:
                with tarfile.open(
                    fileobj=gz,
                    mode="w",
                    format=tarfile.USTAR_FORMAT,
                    encoding="utf-8",
                    errors="strict",
                ) as tar:
                    for name in sorted(contents):
                        data = contents[name]
                        info = tarfile.TarInfo(name=name)
                        info.size = len(data)
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = 0o644
                        info.type = tarfile.REGTYPE
                        tar.addfile(info, io.BytesIO(data))

        # 3. 패키지 사이드카의 README.md 항목 해시를 재계산한다(다른 항목은 불변이므로 유지).
        sidecar_entries, sidecar_violations = b._parse_hash_list(
            sidecar.read_text(encoding="utf-8"), reject_separators=False
        )
        assert not sidecar_violations
        sidecar_entries["README.md"] = hashlib.sha256(contents[target_member]).hexdigest()
        _rewrite_release_file(sidecar, sidecar_entries)

        # 4. RELEASE.SHA256SUMS 3항목(아카이브·리포트·사이드카)을 전부 재계산한다 -- 사이드카
        #    내용이 바뀌었으므로 사이드카 해시도, 그 사이드카를 포함하는 RELEASE 도 바뀐다.
        _recompute_release(release, "1.0.0")

        b.verify_release(release)  # 통과 -- 진본성 비보장의 실증(전량 재계산 세트).


# ---------------------------------------------------------------------------
# TestVerifyReleaseArchiveCorruption
# ---------------------------------------------------------------------------


class TestVerifyReleaseArchiveCorruption:
    """PR #257 3차 이중 적대검증(채널 B, MED) -- 손상된 tar.gz 가 verify_release 의
    아카이브 블록에서 EOFError/IndexError/ValueError 등 raw 예외로 누출되지 않고
    문서화된 BuildError("release verification failed: ...") 로 수렴해야 한다.
    설계 정본: fix-design-v3.md 를 v4.md 가 개정하고([T1][T2]), v4.md 를 v5.md 가
    추가 개정한다([U1][U2]) -- v5 가 최우선.
    """

    @pytest.fixture
    def release(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        return out_dir

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda data: data[:10], id="truncate-10-bytes"),
            pytest.param(lambda data: data[: len(data) // 2], id="truncate-50-percent"),
            pytest.param(
                lambda data: gzip.compress(os.urandom(len(data))), id="gzip-valid-content-garbage"
            ),
        ],
    )
    def test_corrupted_archive_rejected_without_traceback(self, release, mutate):
        """절단(10바이트·50%) 및 gzip 자체는 유효하나 내용이 무작위인 아카이브 -- 전부
        아카이브 블록의 `except Exception` 에서 BuildError 로 수렴해야 한다(EOFError 는
        절단, 3차 채널 B 실측: /home/asdf/orch-scratch/o247/p2b-repro/before-fix-eoferror.txt).
        RELEASE.SHA256SUMS 를 손상된 아카이브의 새 해시로 재계산해, 앞선 RELEASE 해시
        비교 단계가 아니라 이 아카이브 파싱 블록에 실제로 도달함을 보장한다(재계산 없이는
        해시 불일치가 먼저 걸려 이 테스트가 무의미해진다).
        """
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        archive.write_bytes(mutate(archive.read_bytes()))
        _recompute_release(release, "1.0.0")
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브를 열 수 없다" in str(exc_info.value)

    def test_pax_sparse_header_value_error_rejected(self, release):
        """회귀 방지(v5 [U1]): `except Exception` 을 v3 식 열거형
        `(TarError, OSError, EOFError, zlib.error)` 로 되돌려도 이 테스트 이외의 절단류
        테스트는 여전히 통과할 수 있다(전부 EOFError/ReadError 클래스이므로). 이 테스트는
        그 회귀를 잡기 위해 열거형에 없는 `ValueError` 를 던지는 손상 유형(PAX
        sparse 헤더)을 별도로 고정한다.

        실측(본 세션): `tarfile.PAX_FORMAT` writer 는
        `pax_headers={"GNU.sparse.map": "a"}` 를 검증 없이 그대로 기록하지만, reader 의
        `getmembers()` 가 이를 정수로 파싱하려다 `ValueError: invalid literal for int()
        with base 10: 'a'` 를 던진다(3차 채널 B 실측 클래스와 동일 계열).
        """
        archive = release / "localcrab-plugin-1.0.0.tar.gz"

        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo(name=f"{b._ARCHIVE_PREFIX}sparse-bomb")
            data = b"x"
            info.size = len(data)
            info.pax_headers = {"GNU.sparse.map": "a"}
            tar.addfile(info, io.BytesIO(data))
        tar_bytes = raw.getvalue()

        archive.write_bytes(gzip.compress(tar_bytes))
        _recompute_release(release, "1.0.0")

        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        message = str(exc_info.value)
        assert "아카이브를 열 수 없다" in message
        assert "ValueError" in message

    def test_truncate_last_byte_without_recompute_caught_by_hash_layer(self, release):
        """계층 방어 실증(v4 [T2], PR #257 리뷰 라운드 2 [X1] 반영으로 독스트링 갱신):
        아카이브를 전체-1바이트만 절단하면 실측상 tarfile 이 gzip 트레일러(CRC)를 읽지
        않고 end-of-archive 영블록에서 조용히 멈춰(design-v4 실측), 과거(라운드 1)
        구조에서는 아카이브 파싱 블록의 `except Exception` 이 침묵할 수 있었다. 이
        테스트는 RELEASE 를 **재계산하지 않음으로써** 그 경우에도 더 앞선 해시 비교
        루프가 "해시 불일치" 위반으로 여전히 잡아냄을 증명한다.

        [X1] 긍정 확립 게이트 도입 이후에는 방어가 한 단계 더 강해진다 -- 해시가
        불일치하는 순간 게이트가 파싱 자체를 아예 생략하므로("아카이브 파싱 생략"
        위반이 함께 추가됨), 이 테스트가 원래 우려했던 "파싱 블록이 침묵하는 경우"는
        이제 발생 조건 자체가 없다(파싱을 시도하지 않으므로). assertion 은 변경하지
        않는다 -- 해시 비교 루프가 여전히 "해시 불일치" 로 잡아낸다는 사실 자체는
        그대로 유지되기 때문이다.
        """
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        data = archive.read_bytes()
        archive.write_bytes(data[:-1])
        # 의도적으로 _recompute_release 를 호출하지 않는다 -- RELEASE 는 원본 해시를
        # 그대로 유지하므로 아카이브 파싱 이전의 해시 비교 루프가 먼저 걸려야 한다.

        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        message = str(exc_info.value)
        assert "해시 불일치" in message
        assert "localcrab-plugin-1.0.0.tar.gz" in message


class TestSha256StreamAndLimitedReader:
    """PR #257 리뷰 라운드 2 [W1] 대응 -- 경계 없는 `extracted.read()` 를 청크 단위
    스트리밍으로 바꾼 핵심 헬퍼(`_LimitedReader`/`_sha256_stream`) 자체의 단위 테스트.
    설계 정본: fix-design-v6.md ~ v11.md(v11 최종)."""

    def test_matches_hashlib_baseline_empty(self):
        digest, consumed = b._sha256_stream(io.BytesIO(b""), budget=100)
        assert digest == hashlib.sha256(b"").hexdigest()
        assert consumed == 0

    def test_matches_hashlib_baseline_exact_chunk_multiple(self):
        data = os.urandom(b._VERIFY_CHUNK_BYTES * 3)
        digest, consumed = b._sha256_stream(io.BytesIO(data), budget=len(data))
        assert digest == hashlib.sha256(data).hexdigest()
        assert consumed == len(data)

    def test_matches_hashlib_baseline_with_remainder(self):
        data = os.urandom(b._VERIFY_CHUNK_BYTES + 123)
        digest, consumed = b._sha256_stream(io.BytesIO(data), budget=len(data))
        assert digest == hashlib.sha256(data).hexdigest()
        assert consumed == len(data)

    def test_sha256_stream_raises_on_budget_overrun(self):
        data = os.urandom(1000)
        with pytest.raises(b._BudgetExceededError):
            b._sha256_stream(io.BytesIO(data), budget=10)

    def test_limited_reader_clamps_underlying_request_size_and_raises_on_overrun(self):
        """핵심 메모리 상한 증거: 호출자가 아무리 큰 size 를 요청해도(1000만 바이트),
        `_LimitedReader` 가 내부 fileobj 에 실제로 요청하는 크기는 budget+1 로
        clamp 된다 -- 압축률 높은 멀티 GB 멤버라도 이 clamp 덕에 단 한 번의 read 로
        전체를 메모리에 적재하는 일이 없다([W1] 핵심)."""

        class _RequestSizeSpy:
            def __init__(self) -> None:
                self.last_request: int | None = None

            def read(self, size: int = -1) -> bytes:
                self.last_request = size
                return b"\x00" * size

        budget = 100
        spy = _RequestSizeSpy()
        limited = b._LimitedReader(spy, budget)
        with pytest.raises(b._BudgetExceededError):
            limited.read(10_000_000)
        assert spy.last_request == budget + 1
        assert limited.consumed == budget + 1


class TestVerifyReleaseBoundedResources:
    """PR #257 리뷰 라운드 2 [W1] 대응 -- verify_release() 의 경계 자원(청크 해싱,
    [X1] 긍정 확립 게이트, [Y1] 전개 예산, [Z1] sparse/음수크기 거부, [Z2]/[V1]/[Q1]
    단일 서술자 규율)을 실제 release 세트를 통해 검증한다.
    설계 정본: fix-design-v6.md ~ v11.md(v11 최종)."""

    @pytest.fixture
    def release(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        return out_dir

    # -- 실사용 수준 증거: 극단적으로 작은 청크 크기에서도 결과가 동일해야 한다. --

    def test_small_chunk_size_still_verifies_correctly(self, release, monkeypatch):
        monkeypatch.setattr(b, "_VERIFY_CHUNK_BYTES", 7)
        b.verify_release(release)  # 예외 없어야 함

    # -- [X1] 긍정 확립 게이트: 부정 4종 + 긍정 1종, 전부 tarfile.open 호출 횟수로 확인 --

    def test_gate_missing_archive_entry_skips_parsing(self, release, monkeypatch):
        calls = _count_tarfile_open_calls(monkeypatch)
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        lines = [ln for ln in release_path.read_text(encoding="utf-8").splitlines() if ln]
        kept = [ln for ln in lines if "tar.gz" not in ln]
        release_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 파싱 생략" in str(exc_info.value)
        assert calls["n"] == 0

    def test_gate_format_violated_archive_line_skips_parsing(self, release, monkeypatch):
        calls = _count_tarfile_open_calls(monkeypatch)
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        mutated = []
        for ln in release_path.read_text(encoding="utf-8").splitlines():
            if ln.endswith("localcrab-plugin-1.0.0.tar.gz"):
                digest, name = ln.split("  ", 1)
                mutated.append("g" + digest[1:] + "  " + name)  # 비-hex 문자 -> 포맷 위반
            else:
                mutated.append(ln)
        release_path.write_text("\n".join(mutated) + "\n", encoding="utf-8")
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 파싱 생략" in str(exc_info.value)
        assert calls["n"] == 0

    def test_gate_duplicate_archive_entry_skips_parsing(self, release, monkeypatch):
        calls = _count_tarfile_open_calls(monkeypatch)
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        digest = _sha256(release / "localcrab-plugin-1.0.0.tar.gz")
        line = f"{digest}  localcrab-plugin-1.0.0.tar.gz"
        text = release_path.read_text(encoding="utf-8").rstrip("\n") + "\n" + line + "\n"
        release_path.write_text(text, encoding="utf-8")
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 파싱 생략" in str(exc_info.value)
        assert calls["n"] == 0

    def test_gate_hash_mismatch_skips_parsing(self, release, monkeypatch):
        calls = _count_tarfile_open_calls(monkeypatch)
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        data = bytearray(archive.read_bytes())
        data[-1] ^= 0xFF
        archive.write_bytes(bytes(data))
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 파싱 생략" in str(exc_info.value)
        assert calls["n"] == 0

    def test_gate_positive_establishment_parses_exactly_once(self, release, monkeypatch):
        calls = _count_tarfile_open_calls(monkeypatch)
        b.verify_release(release)  # 예외 없어야 함
        assert calls["n"] == 1

    # -- [Y1] 전개 예산: 서로 다른 3가지 경로로 물리 계층에 도달함을 보인다 --

    def test_expansion_budget_reachable_via_normal_member_hashing(self, release, monkeypatch):
        """[Y1]/[Z1] 이 함께 방어함을 보인다 -- 다만 이 표본은 이름과 달리 실제로
        `_hash_chunked` 에 도달하지 않는다: 선언 크기 200,000바이트가 논리 예산
        (32,768바이트)을 `extractfile` 호출 이전에 이미 초과시켜 [Z1] 논리 계층에서
        걸린다(v13 [S1]/A5 LOW1 정정 -- 5차 이중검증 채널 B 실측으로 확인됨). 물리
        계층까지 실제로 해싱을 거쳐 도달하는 표본은
        `TestVerifyReleaseServedBytesInstrumentation.test_member_hashing_genuinely_chunked_multiple_reads`
        를 참고할 것. 이 테스트 자체는 그대로 유지한다 -- [Z1] 이 [Y1] 물리 계층의
        추가 소비 없이도(스파이 소비량이 낮게 유지됨) 단독으로 조기 차단함을 보이는
        유효한 회귀다."""
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        prefix = b._ARCHIVE_PREFIX
        _write_plain_tar_gz(
            archive,
            [(f"{prefix}small.txt", b"hi"), (f"{prefix}big.bin", b"\x00" * 200_000)],
        )
        _recompute_release(release, "1.0.0")
        budget = 32_768
        monkeypatch.setattr(b, "_MAX_ARCHIVE_EXPANDED_BYTES", budget)
        monkeypatch.setattr(b, "_LimitedReader", _SpyLimitedReader)
        _SpyLimitedReader.last_instance = None
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 전개 크기 상한 초과" in str(exc_info.value)
        spy = _SpyLimitedReader.last_instance
        assert spy is not None
        assert spy.consumed <= budget + 65_536

    def test_expansion_budget_reachable_via_pure_header_overhead(self, release, monkeypatch):
        """[Y1] 이 [Z1] 논리 계층과 독립임을 보인다 -- 멤버 전부가 크기 0(선언 크기
        합계가 예산을 전혀 건드리지 않음)이어도, tar 헤더 블록 자체의 누적 물리
        판독량(멤버당 512바이트)만으로 예산을 초과시킨다."""
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        prefix = b._ARCHIVE_PREFIX
        members = [(f"{prefix}f{i}.txt", b"") for i in range(150)]
        _write_plain_tar_gz(archive, members)
        _recompute_release(release, "1.0.0")
        budget = 32_768
        monkeypatch.setattr(b, "_MAX_ARCHIVE_EXPANDED_BYTES", budget)
        monkeypatch.setattr(b, "_LimitedReader", _SpyLimitedReader)
        _SpyLimitedReader.last_instance = None
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 전개 크기 상한 초과" in str(exc_info.value)
        spy = _SpyLimitedReader.last_instance
        assert spy is not None
        assert spy.consumed <= budget + 65_536

    def test_expansion_budget_reachable_via_pax_extended_header(self, release, monkeypatch):
        """[Y1] 이 tarfile 의 헤더 파싱 단계(멤버가 순회 루프에 나오기도 전)에서도
        작동함을 보인다 -- PAX 확장 헤더에 큰 임의 필드를 실어, `member.size` 로는
        전혀 드러나지 않는 물리 판독량으로 예산을 넘긴다."""
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        prefix = b._ARCHIVE_PREFIX
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo(name=f"{prefix}small.txt")
            data = b"hi"
            info.size = len(data)
            info.pax_headers = {"comment": "x" * 100_000}
            tar.addfile(info, io.BytesIO(data))
        archive.write_bytes(gzip.compress(buf.getvalue()))
        _recompute_release(release, "1.0.0")
        budget = 32_768
        monkeypatch.setattr(b, "_MAX_ARCHIVE_EXPANDED_BYTES", budget)
        monkeypatch.setattr(b, "_LimitedReader", _SpyLimitedReader)
        _SpyLimitedReader.last_instance = None
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 전개 크기 상한 초과" in str(exc_info.value)
        spy = _SpyLimitedReader.last_instance
        assert spy is not None
        assert spy.consumed <= budget + 65_536

    # -- [Z1] sparse 거부(무조건, extractfile 미호출) + 음수 크기 거부 --

    def test_sparse_member_rejected_without_extractfile_call(self, release, monkeypatch):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        prefix = b._ARCHIVE_PREFIX
        _write_sparse_bomb_tar_gz(archive, f"{prefix}sparse-bomb", realsize=10**9)
        _recompute_release(release, "1.0.0")
        calls = {"n": 0}
        original_extractfile = tarfile.TarFile.extractfile

        def spy_extract(self, *args, **kwargs):
            calls["n"] += 1
            return original_extractfile(self, *args, **kwargs)

        monkeypatch.setattr(tarfile.TarFile, "extractfile", spy_extract)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 sparse 멤버 거부" in str(exc_info.value)
        assert calls["n"] == 0

    def test_negative_member_size_rejected(self, release, monkeypatch):
        """실측(본 세션): tarfile 자신이 헤더 파싱 시점에 `_block(size)` 로 음수
        오프셋을 즉시 거부하므로(스트림 모드 생성자 자체가 첫 멤버를 미리 파싱한다),
        디스크상의 진짜 tar.gz 헤더에 음수 크기를 심는 구성은 tarfile.open() 생성자
        단계에서 ReadError 로 막혀 우리 루프의 음수-크기 체크에 결코 도달하지 못한다
        (PAX `size` 오버라이드로 우회를 시도해도 pax 처리 경로에서 멤버 자체가
        누락되는 것으로 실측됨). 따라서 이 테스트는 원본 `next()` 로 진짜 헤더
        파싱·오프셋 계산을 전부 정상 수행시킨 뒤, 반환된 TarInfo 객체의 `.size` 값만
        사후에(오프셋 계산이 이미 끝난 뒤) 조작해 우리 자신의 방어 코드 경로만을
        표적으로 삼는다 -- tarfile 내부 파서를 속이려는 것이 아니라, 우리 코드의
        음수-크기 체크 자체가 실제로 위반을 기록하고 이후 처리(prefix/extractfile)를
        생략함을 확인하기 위함이다.
        """
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        prefix = b._ARCHIVE_PREFIX
        target_name = f"{prefix}bad.bin"
        _write_plain_tar_gz(archive, [(target_name, b"x")])
        _recompute_release(release, "1.0.0")

        original_next = tarfile.TarFile.next

        def patched_next(self):
            member = original_next(self)
            if member is not None and member.name == target_name:
                member.size = -1
            return member

        monkeypatch.setattr(tarfile.TarFile, "next", patched_next)
        calls = {"n": 0}
        original_extractfile = tarfile.TarFile.extractfile

        def spy_extract(self, *args, **kwargs):
            calls["n"] += 1
            return original_extractfile(self, *args, **kwargs)

        monkeypatch.setattr(tarfile.TarFile, "extractfile", spy_extract)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 멤버 크기 위반(음수)" in str(exc_info.value)
        assert calls["n"] == 0

    def test_logical_budget_isolated_from_physical_budget(self, release, monkeypatch):
        """[Z1] 논리 예산이 실제 판독(extractfile 호출) 이전, 헤더의 선언 크기만으로도
        단독 작동함을 보인다 -- 물리 계층이 아직 그 멤버의 바이트를 전혀 읽지 않은
        시점에 이미 거부되어야 한다."""
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        prefix = b._ARCHIVE_PREFIX
        _write_plain_tar_gz(archive, [(f"{prefix}big.bin", b"\x00" * 5000)])
        _recompute_release(release, "1.0.0")
        monkeypatch.setattr(b, "_MAX_ARCHIVE_EXPANDED_BYTES", 1000)
        calls = {"n": 0}
        original_extractfile = tarfile.TarFile.extractfile

        def spy_extract(self, *args, **kwargs):
            calls["n"] += 1
            return original_extractfile(self, *args, **kwargs)

        monkeypatch.setattr(tarfile.TarFile, "extractfile", spy_extract)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 전개 크기 상한 초과" in str(exc_info.value)
        assert calls["n"] == 0

    def test_member_count_cap_enforced(self, release, monkeypatch):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        prefix = b._ARCHIVE_PREFIX
        members = [(f"{prefix}f{i}.txt", f"data{i}".encode()) for i in range(5)]
        _write_plain_tar_gz(archive, members)
        _recompute_release(release, "1.0.0")
        monkeypatch.setattr(b, "_MAX_MEMBERS", 3)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "아카이브 멤버 수 상한 초과: 3" in str(exc_info.value)

    # -- 역할별 크기 상한(RELEASE/사이드카/아이템 파일), 서로 격리해 확인 --

    def test_release_file_oversize_raises_immediately(self, release, monkeypatch):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        cap = release_path.stat().st_size - 1
        monkeypatch.setattr(b, "_MAX_SUMS_BYTES", cap)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "RELEASE.SHA256SUMS 크기가 허용 상한을 초과했다" in str(exc_info.value)

    def test_sidecar_oversize_recorded_as_violation(self, release, monkeypatch):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        sidecar_path = release / "localcrab-plugin.SHA256SUMS"
        cap = release_path.stat().st_size + 10
        assert sidecar_path.stat().st_size > cap, (
            "픽스처 전제 위반: 사이드카가 RELEASE+10 바이트보다 작아 이 테스트가 "
            "RELEASE 대신 사이드카만 걸리게 격리되지 않는다"
        )
        monkeypatch.setattr(b, "_MAX_SUMS_BYTES", cap)
        calls = []
        original_parse = b._parse_hash_list

        def spy_parse(*args, **kwargs):
            calls.append(args)
            return original_parse(*args, **kwargs)

        monkeypatch.setattr(b, "_parse_hash_list", spy_parse)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "패키지 사이드카 크기가 허용 상한을 초과했다" in str(exc_info.value)
        assert len(calls) == 1  # RELEASE 파싱 1회뿐 -- 사이드카는 크기 단계에서 걸려 파싱에 못 감

    def test_item_file_oversize_recorded_as_violation(self, release, monkeypatch):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        compat = release / "localcrab-plugin-1.0.0.COMPATIBILITY.md"
        cap = archive.stat().st_size + 5000
        compat.write_bytes(b"x" * (cap + 1000))
        _recompute_release(release, "1.0.0")
        monkeypatch.setattr(b, "_MAX_RELEASE_FILE_BYTES", cap)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        message = str(exc_info.value)
        assert "파일 크기가 허용 상한을 초과했다: localcrab-plugin-1.0.0.COMPATIBILITY.md" in message

    # -- [Z2]/[V1]/[Q1] 단일 서술자 규율: symlink 즉시 거부, FIFO 논블로킹 거부 --

    def test_release_file_symlink_rejected(self, release, tmp_path):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        real_target = tmp_path / "real-release-content.txt"
        real_target.write_bytes(release_path.read_bytes())
        release_path.unlink()
        release_path.symlink_to(real_target)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "RELEASE.SHA256SUMS 를 읽을 수 없다" in str(exc_info.value)

    def test_compat_report_fifo_rejected_without_blocking(self, release):
        """실측: 아이템 루프는 `_open_regular` 를 부르기 전에 `Path.is_file()` 로
        먼저 거르는데, 이는 `os.stat()` 기반(FIFO 에 대해 False)이라 open() 자체를
        전혀 시도하지 않는다 -- lstat-then-open 규율보다도 더 이른 지점에서 이미
        블로킹 위험이 없다. 그래도 SIGALRM 안전망을 두어, 이 전제가 향후 리팩터로
        깨져 실제로 open() 이 시도되고 블록되는 회귀가 생기면 이 테스트가 타임아웃
        으로 확실히 드러내도록 한다."""
        compat = release / "localcrab-plugin-1.0.0.COMPATIBILITY.md"
        compat.unlink()
        os.mkfifo(compat)

        def _on_alarm(signum, frame):
            raise TimeoutError("verify_release 가 FIFO open 에서 블록된 것으로 보인다")

        old_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(5)
        try:
            with pytest.raises(b.BuildError) as exc_info:
                b.verify_release(release)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        assert "파일 없음: localcrab-plugin-1.0.0.COMPATIBILITY.md" in str(exc_info.value)

    def test_sidecar_fifo_rejected_without_blocking(self, release):
        """[Z2]/[V1]/[Q1] 단일 서술자 규율의 실제 표적 -- 패키지 사이드카는 아이템
        루프의 `is_file()` 사전 필터를 거치지 않고 `_open_regular` 를 곧바로 부르므로,
        lstat-먼저 순서(FIFO 의 무기한 blocking open() 을 피하기 위함)가 실제로
        작동함을 이 경로에서 직접 증명한다."""
        sidecar = release / "localcrab-plugin.SHA256SUMS"
        sidecar.unlink()
        os.mkfifo(sidecar)

        def _on_alarm(signum, frame):
            raise TimeoutError("verify_release 가 FIFO open 에서 블록된 것으로 보인다")

        old_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(5)
        try:
            with pytest.raises(b.BuildError) as exc_info:
                b.verify_release(release)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        assert "패키지 사이드카를 읽을 수 없다" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestVerifyReleaseServedBytesInstrumentation
# ---------------------------------------------------------------------------


class _CountingFile:
    """[R1]/[S1] 실제 서빙 바이트 계측 래퍼 -- read() 호출 횟수·각 요청 크기·누적
    서빙 바이트를 기록한다. read()/read(-1) 같은 무인자·음수 요청은 크기가 아니라
    `UNBOUNDED` 표식으로 정규화해 기록한다(v13 [S1] LOW 반영) -- 통째 읽기
    역변이(`fileobj.read()` 1회 호출)가 우연히 작은 size 인자를 쓴 것과 섞이지
    않도록, "제한 없는 요청이었다"는 사실 자체를 크기와 구분되는 값으로 남긴다."""

    UNBOUNDED = "UNBOUNDED"

    def __init__(self, fileobj, *, label: str) -> None:
        self._fileobj = fileobj
        self.label = label
        self.calls = 0
        self.request_sizes: list[int | str] = []
        self.served_bytes = 0

    def read(self, size=-1):
        self.calls += 1
        self.request_sizes.append(self.UNBOUNDED if (size is None or size < 0) else size)
        chunk = self._fileobj.read(size)
        self.served_bytes += len(chunk)
        return chunk

    def close(self) -> None:
        self._fileobj.close()

    def __getattr__(self, name):
        return getattr(self._fileobj, name)

    @property
    def bounded_max_request(self):
        """관측된 요청 중 하나라도 UNBOUNDED 면 None(청크 규율 위반의 직접 증거) --
        그 외에는 관측된 요청 크기 중 최댓값을 돌려준다."""
        if self.UNBOUNDED in self.request_sizes:
            return None
        return max(self.request_sizes) if self.request_sizes else 0


def _install_open_regular_spy(monkeypatch) -> list[_CountingFile]:
    """`_open_regular` 를 spy 래핑해, 호출될 때마다 반환 파일 객체를 `_CountingFile`
    로 감싸 호출 순서대로 누적 리스트에 남긴다(RELEASE 적재·사이드카 적재·항목 파일
    해싱이 각각 몇 번·어떤 이름으로 열렸는지 테스트가 라벨로 식별할 수 있다).
    `_open_regular` 자체는 monkeypatch 하지 않는다 -- 실제 반환 객체를 감쌀 뿐,
    lstat/O_NOFOLLOW/fstat 동작은 그대로 실행된다."""
    opened: list[_CountingFile] = []
    original = b._open_regular

    def spy(path):
        fileobj = original(path)
        cf = _CountingFile(fileobj, label=Path(path).name)
        opened.append(cf)
        return cf

    monkeypatch.setattr(b, "_open_regular", spy)
    return opened


def _install_extractfile_spy(monkeypatch) -> list[_CountingFile]:
    """`TarFile.extractfile` 을 spy 래핑해 **반환된 멤버 파일 객체**를 `_CountingFile`
    로 감싼다(v13 [S1] MED 반영 -- 원시 아카이브 파일을 감싸면 해싱·파싱·물리 예산
    소비가 뒤섞여 구분되지 않으므로, tarfile 이 멤버 단위로 슬라이스해 돌려주는 바로
    그 객체를 계측해야 `_hash_chunked` 의 실제 판독 패턴을 볼 수 있다)."""
    opened: list[_CountingFile] = []
    original = tarfile.TarFile.extractfile

    def spy(self, *args, **kwargs):
        fileobj = original(self, *args, **kwargs)
        if fileobj is None:
            return None
        member = args[0] if args else kwargs.get("member")
        name = getattr(member, "name", str(member))
        cf = _CountingFile(fileobj, label=name)
        opened.append(cf)
        return cf

    monkeypatch.setattr(tarfile.TarFile, "extractfile", spy)
    return opened


class TestVerifyReleaseServedBytesInstrumentation:
    """PR #257 5차 이중검증 채널 B(MED) 대응 -- 유계 읽기 헬퍼(`_read_all_limited`/
    `_sha256_stream`/`_hash_chunked`)를 통째 읽기로 역변이해도 걸러지지 않던 검출력
    공백을 메운다(실측: 세 헬퍼를 전부 무제한 `read()` 로 바꿔도 기존 방호 테스트
    10개가 `10 passed` 로 전부 통과했다 -- /home/asdf/orch-scratch/o247/adv-B5-output.txt).

    원인은 두 가지였다: (1) 역할별 과대 파일 테스트가 오류 문구만 단언하고 실제
    서빙 바이트를 재지 않았다. (2) "정상 멤버 해싱" 표본이 선언 크기만으로 [Z1]
    논리 예산에 먼저 걸려, 실제로는 `_hash_chunked` 에 도달하지 않았다.

    교정: 원시 파일이 아니라 실제 판독 경계(`_open_regular` 각 호출, `extractfile`
    각 반환 객체)를 `_CountingFile` 로 감싸 서빙 바이트·요청 크기·호출 횟수를 직접
    잰다 -- 헬퍼 함수 자체는 monkeypatch 하지 않는다(헬퍼 교체 오구현도 걸러야
    하므로 실제 호출 경로를 계측 대상으로 삼는다). 설계 정본: fix-design-v13.md
    (v12 델타, v12 는 v11 델타)."""

    @pytest.fixture
    def release(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        return out_dir

    # -- [R1](a) RELEASE 적재: served ≤ cap+1 --

    def test_release_load_served_bytes_bounded_by_cap(self, release, monkeypatch):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        cap = 64
        assert release_path.stat().st_size > cap, (
            "픽스처 전제 위반: RELEASE 실제 크기가 cap 보다 커야 초과 경로를 탄다"
        )
        monkeypatch.setattr(b, "_MAX_SUMS_BYTES", cap)
        opened = _install_open_regular_spy(monkeypatch)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "RELEASE.SHA256SUMS 크기가 허용 상한을 초과했다" in str(exc_info.value)
        release_handles = [cf for cf in opened if cf.label == release_path.name]
        assert len(release_handles) == 1
        assert release_handles[0].served_bytes <= cap + 1

    # -- [R1](b)/[S2] 사이드카: 두 경로(항목 해싱 + 본문 적재)에서 각각 열리므로
    #    합계가 아니라 핸들별 최댓값을 단언한다 --

    def test_sidecar_load_served_bytes_bounded_per_handle(self, release, monkeypatch):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        sidecar_path = release / "localcrab-plugin.SHA256SUMS"
        # cap 은 RELEASE 자체보다는 커야(그래야 RELEASE 적재 단계에서 먼저 하드
        # raise 되지 않고 사이드카 로딩까지 도달한다) 사이드카보다는 작아야(초과
        # 경로를 실제로 탄다) 한다 -- test_sidecar_oversize_recorded_as_violation
        # 과 동일한 격리 패턴.
        cap = release_path.stat().st_size + 10
        assert sidecar_path.stat().st_size > cap, (
            "픽스처 전제 위반: 사이드카 실제 크기가 RELEASE+10 바이트보다 커야 "
            "RELEASE 대신 사이드카만 걸리게 격리된다"
        )
        monkeypatch.setattr(b, "_MAX_SUMS_BYTES", cap)
        opened = _install_open_regular_spy(monkeypatch)
        with pytest.raises(b.BuildError):
            b.verify_release(release)
        sidecar_handles = [cf for cf in opened if cf.label == sidecar_path.name]
        assert len(sidecar_handles) >= 1
        for cf in sidecar_handles:
            assert cf.served_bytes <= cap + 1

    # -- [R1](c) 항목 파일 해싱(tar.gz/COMPATIBILITY.md): served ≤ cap+1 --

    def test_item_file_hashing_served_bytes_bounded_by_cap(self, release, monkeypatch):
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        compat = release / "localcrab-plugin-1.0.0.COMPATIBILITY.md"
        cap = archive.stat().st_size + 5000
        compat.write_bytes(b"x" * (cap + 1000))
        _recompute_release(release, "1.0.0")
        monkeypatch.setattr(b, "_MAX_RELEASE_FILE_BYTES", cap)
        opened = _install_open_regular_spy(monkeypatch)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert (
            "파일 크기가 허용 상한을 초과했다: localcrab-plugin-1.0.0.COMPATIBILITY.md"
            in str(exc_info.value)
        )
        compat_handles = [cf for cf in opened if cf.label == compat.name]
        assert len(compat_handles) == 1
        assert compat_handles[0].served_bytes <= cap + 1

    # -- [S1] 멤버 해싱 실도달: 다중 read, 각 요청 ≤ 청크 --

    def test_member_hashing_genuinely_chunked_multiple_reads(self, release, monkeypatch):
        """[S1] `_VERIFY_CHUNK_BYTES` 를 작게 고정하고, 전개 예산(32,768, 기본값) 안에
        여유 있게 드는 멤버(10,000B)를 준비한다 -- [Z1] 논리 예산에 걸리지 않고 실제로
        `_hash_chunked` 까지 도달하는 표본이다. `TarFile.extractfile` 반환 객체를 직접
        계측해, 이 멤버가 정말 청크 단위로(다중 read, 각 요청 ≤ 청크) 소비되는지
        확인한다 -- 통째 읽기 역변이(`read()` 무인자 1회)라면 calls==1 이고
        `bounded_max_request` 가 None(UNBOUNDED) 이 되어 즉시 RED."""
        archive = release / "localcrab-plugin-1.0.0.tar.gz"
        sidecar_path = release / "localcrab-plugin.SHA256SUMS"
        prefix = b._ARCHIVE_PREFIX
        member_name = f"{prefix}mid.bin"
        member_data = b"\x00" * 10_000
        member_size = len(member_data)
        _write_plain_tar_gz(archive, [(member_name, member_data)])
        # 아카이브를 단일 멤버로 교체했으므로, 아카이브-사이드카 멤버 집합 대사가
        # 걸리지 않도록 사이드카도 이 단일 멤버 기준으로 다시 쓴다(원래 사이드카는
        # _fake_repo 의 실제 플러그인 파일 목록을 담고 있어 불일치가 난다).
        _rewrite_release_file(sidecar_path, {"mid.bin": hashlib.sha256(member_data).hexdigest()})
        _recompute_release(release, "1.0.0")
        chunk = 1024
        monkeypatch.setattr(b, "_VERIFY_CHUNK_BYTES", chunk)
        opened = _install_extractfile_spy(monkeypatch)
        b.verify_release(release)  # 예외 없어야 함(예산 안에 드는 멤버)
        member_handles = [cf for cf in opened if cf.label == member_name]
        assert len(member_handles) == 1
        cf = member_handles[0]
        expected_min_calls = -(-member_size // chunk)  # ceil(10000 / 1024) == 10
        assert cf.calls >= expected_min_calls
        assert cf.bounded_max_request is not None, (
            "요청 크기가 UNBOUNDED 로 기록됨 -- 통째 읽기 역변이가 감지되어야 하는 지점"
        )
        assert cf.bounded_max_request <= chunk

    # -- [R2]/[V2] 위협 모델 문구가 verify_release docstring 에 실재함을 단언 --

    def test_verify_release_docstring_states_static_snapshot_threat_model(self):
        doc = b.verify_release.__doc__ or ""
        assert "정적" in doc
        assert "스냅샷" in doc


# ---------------------------------------------------------------------------
# TestSafeVersionNewlineRejection -- PR #257 리뷰 라운드 3 [G2] 대응
# ---------------------------------------------------------------------------


class TestSafeVersionNewlineRejection:
    """`_safe_version` 의 `^...$` + `re.match` 는 `$` 가 말미 개행 직전 매치를 허용해
    `"1.0\\n"` 같은 개행 포함 문자열이 통과했다(PR #257 리뷰 라운드 3 [G2], P2).
    fix-design-v14.md -> v15.md(v15 최종, 통합 테스트 픽스처만 델타)."""

    @pytest.mark.parametrize("version", ["1.0\n", "1.0\r", "1.0\r\n", "\n1.0"])
    def test_safe_version_rejects_embedded_newlines(self, version):
        with pytest.raises(b.BuildError):
            b._safe_version(version)

    def test_build_release_rejects_newline_version_without_publishing(self, tmp_path):
        """v15 [H1]: pyproject 는 TOML 이스케이프(`\\\\n`)로 개행을 기록해야 한다 -- 원시
        개행을 그대로 삽입하면 TOMLDecodeError 가 `_safe_version` 도달 전에 선행해
        이 결함과 무관한 이유로 실패하므로, 파싱 결과가 실제 개행 문자열임을 먼저
        전제 단언으로 못박는다."""
        repo = _fake_repo(tmp_path, version="0.1.0")
        pyproject_text = '[project]\nname = "opencrab"\nversion = "0.1.0\\n"\n'
        (repo / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
        parsed = tomllib.loads(pyproject_text)
        assert parsed["project"]["version"] == "0.1.0\n"  # 전제: 실제 개행 문자열

        manifest_path = repo / "packaging" / "agent-plugin" / "src" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.0\n"
        _write_json(manifest_path, manifest)

        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build_release(repo, out_dir)

        # v15 [H1]: "버전 부착 산출물 게시 전" -- out_dir 에 버전 부착 파일도, 개행
        # 포함 파일명도 존재하지 않아야 한다(격리 조립 단계에서 실패해 게시로 넘어가지
        # 않았음을 직접 확인한다).
        assert out_dir.exists()
        published = list(out_dir.iterdir())
        assert published == []


# ---------------------------------------------------------------------------
# TestVerifyReleaseItemProbeErrorBoundary -- PR #257 리뷰 라운드 3 [G1] 대응
# ---------------------------------------------------------------------------


class TestVerifyReleaseItemProbeErrorBoundary:
    """RELEASE 항목 루프의 `path.is_file()` 탐침이 과장 파일명(NAME_MAX 초과) 등에서
    OSError 를 던져 `--verify` 가 traceback 으로 종료하던 결함(PR #257 리뷰 라운드 3
    [G1], P2). fix-design-v14.md -> v15.md(v15 [H2]: 탐침 실패 위반 문구 직접 단언)."""

    @pytest.fixture
    def release(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        return out_dir

    def test_overlong_entry_name_reported_as_violation_not_oserror(self, release):
        release_path = release / "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS"
        overlong_name = "a" * 300
        digest = "0" * 64
        text = (
            release_path.read_text(encoding="utf-8").rstrip("\n")
            + f"\n{digest}  {overlong_name}\n"
        )
        release_path.write_text(text, encoding="utf-8")

        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        # v15 [H2]: 300자 항목은 "RELEASE 구성 불일치" 위반도 함께 유발하므로, 그것만으로
        # 통과시키지 않고 탐침 실패 문구의 실재를 직접 단언한다.
        assert "항목 경로를 탐침할 수 없다" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestCli
# ---------------------------------------------------------------------------


class TestCli:
    """scripts/build_agent_plugin.py: main(argv) 주입, 기존 stdout 계약 유지, --verify."""

    @pytest.fixture(scope="module")
    def cli(self):
        import importlib.util

        script_path = REPO / "scripts" / "build_agent_plugin.py"
        spec = importlib.util.spec_from_file_location("build_agent_plugin_cli_o247", script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_default_run_exit_zero_and_stdout_contract(self, cli, tmp_path, capsys):
        out_dir = tmp_path / "dist"
        code = cli.main(["--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 0
        lines = [ln for ln in captured.out.splitlines() if ln]
        assert lines[0].startswith("plugin package:")
        assert lines[1].startswith("sha256sums:")
        assert len(lines) > 2  # 신규 산출물 경로 줄이 뒤에 추가된다.

    def test_verify_flag_success(self, cli, tmp_path):
        out_dir = tmp_path / "dist"
        assert cli.main(["--out", str(out_dir)]) == 0
        assert cli.main(["--verify", "--out", str(out_dir)]) == 0

    def test_verify_flag_failure_reports_violations(self, cli, tmp_path, capsys):
        out_dir = tmp_path / "dist"
        assert cli.main(["--out", str(out_dir)]) == 0
        capsys.readouterr()

        release_files = list(out_dir.glob("localcrab-plugin-*.tar.gz"))
        assert len(release_files) == 1
        data = bytearray(release_files[0].read_bytes())
        data[-1] ^= 0xFF
        release_files[0].write_bytes(bytes(data))

        code = cli.main(["--verify", "--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 1
        assert captured.err.strip() != ""

    def test_rerun_same_out_dir_succeeds(self, cli, tmp_path):
        out_dir = tmp_path / "dist"
        assert cli.main(["--out", str(out_dir)]) == 0
        assert cli.main(["--out", str(out_dir)]) == 0

    def test_verify_flag_non_utf8_release_reports_failure_without_traceback(self, cli, tmp_path, capsys):
        """PR #257 리뷰 P2: 비 UTF-8 RELEASE.SHA256SUMS 가 traceback 이 아니라 문서화된
        'release verification failed' 형식 + exit 1 로 종료해야 한다(예외 전파 없음)."""
        out_dir = tmp_path / "dist"
        assert cli.main(["--out", str(out_dir)]) == 0
        capsys.readouterr()

        release_files = list(out_dir.glob("localcrab-plugin-*.RELEASE.SHA256SUMS"))
        assert len(release_files) == 1
        data = bytearray(release_files[0].read_bytes())
        data[3] = 0xFF
        release_files[0].write_bytes(bytes(data))

        code = cli.main(["--verify", "--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 1
        assert "release verification failed" in captured.err

    def test_verify_flag_overlong_entry_name_no_traceback(self, cli, tmp_path, capsys):
        """PR #257 리뷰 라운드 3 [G1]: 과장 파일명(NAME_MAX 초과) RELEASE 항목이 CLI
        층에서도 raw OSError traceback 이 아니라 문서화된 'release verification
        failed' 형식 + exit 1 로 종료해야 한다(예외 전파 없음)."""
        out_dir = tmp_path / "dist"
        assert cli.main(["--out", str(out_dir)]) == 0
        capsys.readouterr()

        release_files = list(out_dir.glob("localcrab-plugin-*.RELEASE.SHA256SUMS"))
        assert len(release_files) == 1
        release_path = release_files[0]
        overlong_name = "a" * 300
        digest = "0" * 64
        text = (
            release_path.read_text(encoding="utf-8").rstrip("\n")
            + f"\n{digest}  {overlong_name}\n"
        )
        release_path.write_text(text, encoding="utf-8")

        code = cli.main(["--verify", "--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 1
        assert "release verification failed" in captured.err

    def test_verify_flag_truncated_archive_reports_failure_without_traceback(self, cli, tmp_path, capsys):
        """3차 이중 적대검증(채널 B, MED): 50% 절단된 tar.gz 가 CLI 층에서도 raw
        EOFError traceback 이 아니라 문서화된 'release verification failed' 형식 +
        exit 1 로 종료해야 한다(예외 전파 없음). p2b-repro 재현 절차와 동일한 손상
        형태를 CLI 경로로 재확인한다."""
        out_dir = tmp_path / "dist"
        assert cli.main(["--out", str(out_dir)]) == 0
        capsys.readouterr()

        release_files = list(out_dir.glob("localcrab-plugin-*.tar.gz"))
        assert len(release_files) == 1
        archive = release_files[0]
        version = archive.name[len("localcrab-plugin-") : -len(".tar.gz")]
        data = archive.read_bytes()
        archive.write_bytes(data[: len(data) // 2])
        _recompute_release(out_dir, version)

        code = cli.main(["--verify", "--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 1
        assert "release verification failed" in captured.err

    def test_verify_flag_release_read_error_no_traceback(self, cli, tmp_path, capsys, monkeypatch):
        """PR #257 리뷰 4차 [S1]: RELEASE 본체를 연 뒤 판독 도중 OSError(EIO) 가 나면 CLI
        층에서도 raw traceback 이 아니라 문서화된 'release verification failed' 형식 +
        exit 1 로 끝나야 한다. fix-design-v16.md [A][B] -> v20.md."""
        out_dir = tmp_path / "dist"
        assert cli.main(["--out", str(out_dir)]) == 0
        capsys.readouterr()

        release_files = list(out_dir.glob("localcrab-plugin-*.RELEASE.SHA256SUMS"))
        assert len(release_files) == 1
        _inject_read_failure(monkeypatch, release_files[0].name)

        code = cli.main(["--verify", "--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 1
        assert "release verification failed" in captured.err
        assert "Traceback" not in captured.err

    def test_build_out_dir_occupied_by_file_no_traceback(self, cli, tmp_path, capsys):
        """PR #257 리뷰 4차 형제 경로 [P1]: build_release 의 초기 파일시스템 조작이 예외
        변환 경계 밖이라 OSError 가 raw traceback 으로 샜다. CLI build 경로에서 문서화된
        'build failed' 형식 + exit 1 로 끝나야 한다. fix-design-v17.md [P1]."""
        occupied = tmp_path / "dist"
        occupied.write_text("not a directory\n", encoding="utf-8")

        code = cli.main(["--out", str(occupied)])
        captured = capsys.readouterr()
        assert code == 1
        assert "build failed" in captured.err
        assert "Traceback" not in captured.err

    def test_published_manifest_not_reread_after_publish(self, cli, tmp_path, capsys, monkeypatch):
        """PR #257 리뷰 4차 [T1]: CLI 는 게시 후 plugin.json 을 다시 읽지 않아야 한다.
        게시 직후(같은 main() 호출 안에서) manifest 를 삭제해도 exit 0 과 경로 출력이
        정상이어야 한다 -- 재판독이 남아 있으면 이 테스트는 실패한다. fix-design-v20.md."""
        out_dir = tmp_path / "dist"
        original = cli.build_release

        def wrapper(repo_root, target_dir):
            result = original(repo_root, target_dir)
            staged_root = result[0] if isinstance(result, tuple) else result
            (staged_root / "plugin.json").unlink()
            return result

        monkeypatch.setattr(cli, "build_release", wrapper)

        code = cli.main(["--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 0
        assert "Traceback" not in captured.err
        lines = [ln for ln in captured.out.splitlines() if ln]
        assert lines[0].startswith("plugin package:")
        assert lines[1].startswith("sha256sums:")
        assert len(lines) == 5

    def test_output_paths_match_published_artifacts(self, cli, tmp_path, capsys, monkeypatch):
        """PR #257 리뷰 4차 [T1]: 출력 경로는 게시된 산출물과 구성적으로 일치해야 한다.
        게시 직후 manifest 의 version 을 형식상 안전한 다른 값('9.9.9')으로 손상시켜도
        출력은 실제 게시물을 가리켜야 한다 -- 재판독 판본은 존재하지 않는
        localcrab-plugin-9.9.9.* 를 출력해 실패한다. fix-design-v20.md 테스트 11."""
        out_dir = tmp_path / "dist"
        original = cli.build_release

        def wrapper(repo_root, target_dir):
            result = original(repo_root, target_dir)
            staged_root = result[0] if isinstance(result, tuple) else result
            manifest_path = staged_root / "plugin.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = "9.9.9"
            _write_json(manifest_path, manifest)
            return result

        monkeypatch.setattr(cli, "build_release", wrapper)

        code = cli.main(["--out", str(out_dir)])
        captured = capsys.readouterr()
        assert code == 0
        assert "9.9.9" not in captured.out

        # 출력이 지목한 경로 3종이 실재하는 정규 파일이어야 한다.
        quoted = [ln.split(":", 1)[1].strip() for ln in captured.out.splitlines() if ":" in ln]
        listed = [Path(value) for value in quoted if value.startswith(str(out_dir))]
        suffixes = (".tar.gz", ".COMPATIBILITY.md", ".RELEASE.SHA256SUMS")
        matched = [path for path in listed if path.name.endswith(suffixes)]
        assert len(matched) == 3
        for path in matched:
            assert path.is_file(), path


# ---------------------------------------------------------------------------
# PR #257 리뷰 4차 대응 -- 판독 중 OSError 오류 경계 (fix-design-v16 [A][B][C] -> v20)
# ---------------------------------------------------------------------------


class _ReadFailingProxy:
    """실제 파일 객체를 감싸 read() 만 OSError 로 실패시킨다(open 은 정상 성공).

    결함 주입은 read 신호 하나뿐이며 릴리스 세트·파일·해시는 전부 실물이다.
    """

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self.close_count = 0

    def read(self, size: int = -1) -> bytes:
        raise OSError(errno.EIO, "injected read failure")

    def seek(self, *args, **kwargs):
        return self._wrapped.seek(*args, **kwargs)

    def close(self) -> None:
        self.close_count += 1
        self._wrapped.close()


class _CloseFailingProxy:
    """판독은 전부 정상이고 close() 만 OSError 로 실패시킨다."""

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped

    def read(self, size: int = -1) -> bytes:
        return self._wrapped.read(size)

    def seek(self, *args, **kwargs):
        return self._wrapped.seek(*args, **kwargs)

    def close(self) -> None:
        self._wrapped.close()
        raise OSError(errno.EIO, "injected close failure")


def _inject_read_failure(monkeypatch, target_name: str, *, occurrence: int = 1) -> list:
    """`_open_regular` 를 감싸 target_name 의 occurrence 번째 open 에서만 read 를 실패시킨다.

    fix-design-v17.md [P3]: 사이드카는 RELEASE 항목 해싱(S2)과 사이드카 판독(S3)에서 각각
    한 번씩 열리므로, 경로만으로 표적하면 두 사이트가 함께 실패해 원인이 격리되지 않는다.
    호출 순번까지 표적해 한 사이트만 실패시킨다.
    """
    original = b._open_regular
    state = {"count": 0}
    proxies: list = []

    def patched(path):
        fileobj = original(path)
        if Path(path).name == target_name:
            state["count"] += 1
            if state["count"] == occurrence:
                proxy = _ReadFailingProxy(fileobj)
                proxies.append(proxy)
                return proxy
        return fileobj

    monkeypatch.setattr(b, "_open_regular", patched)
    return proxies


class TestVerifyReleaseReadErrorBoundary:
    """판독 루프가 `_BudgetExceededError` 만 잡아 판독 중 OSError(EIO 등)가 경계 밖으로
    나가던 결함(PR #257 리뷰 4차 P2). 리뷰어는 RELEASE 본체 1곳만 지목했으나 동일 패턴이
    3곳이라 전수 대응한다. fix-design-v16.md [A][B] -> v20.md."""

    @pytest.fixture
    def release(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        b.build_release(repo, out_dir)
        return out_dir

    def test_release_body_read_error_converges_to_builderror(self, release, monkeypatch):
        """[S1] RELEASE 본체 판독 실패는 즉시 BuildError 로 수렴한다."""
        _inject_read_failure(monkeypatch, "localcrab-plugin-1.0.0.RELEASE.SHA256SUMS")
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "RELEASE.SHA256SUMS 를 끝까지 읽을 수 없다" in str(exc_info.value)

    def test_entry_read_error_converges_to_builderror(self, release, monkeypatch):
        """[S2] RELEASE 항목 해싱 중 판독 실패는 위반으로 누적된다. 아카이브를 표적으로
        삼아 사이드카 판독(S3)과 겹치지 않게 한다."""
        _inject_read_failure(monkeypatch, "localcrab-plugin-1.0.0.tar.gz")
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        assert "파일을 끝까지 읽을 수 없다: localcrab-plugin-1.0.0.tar.gz" in str(exc_info.value)

    def test_entry_read_error_closes_fd(self, release, monkeypatch):
        """[S2] 판독 실패 분기도 fd 를 닫아야 한다(성공+아카이브 경로에서만 fd 를 살려
        두는 구조라 with 를 쓸 수 없다)."""
        proxies = _inject_read_failure(monkeypatch, "localcrab-plugin-1.0.0.tar.gz")
        with pytest.raises(b.BuildError):
            b.verify_release(release)
        assert len(proxies) == 1
        assert proxies[0].close_count == 1

    def test_sidecar_read_error_converges_to_builderror(self, release, monkeypatch):
        """[S3] 사이드카 판독 실패는 위반으로 누적된다. 사이드카의 두 번째 open(S3)만
        표적해 S2 와 격리한다(fix-design-v17.md [P3])."""
        _inject_read_failure(monkeypatch, "localcrab-plugin.SHA256SUMS", occurrence=2)
        with pytest.raises(b.BuildError) as exc_info:
            b.verify_release(release)
        message = str(exc_info.value)
        assert "패키지 사이드카를 끝까지 읽을 수 없다" in message
        # S2 는 정상 통과해야 한다 -- 같은 파일의 첫 번째 open 은 실패시키지 않았다.
        assert "파일을 끝까지 읽을 수 없다: localcrab-plugin.SHA256SUMS" not in message

    def test_close_failure_does_not_break_valid_release(self, release, monkeypatch):
        """[C] 읽기 전용 fd 의 close() 오류는 검증 결과를 바꿀 무결성 신호가 아니다.
        손상 없는 릴리스에서 close 만 실패시켜도 검증은 예외 없이 정상 완료해야 한다.
        fix-design-v17.md [P4]: 'raw OSError 부재'가 아니라 정상 완료 자체를 단언한다."""
        original = b._open_regular

        def patched(path):
            return _CloseFailingProxy(original(path))

        monkeypatch.setattr(b, "_open_regular", patched)
        assert b.verify_release(release) is None


class TestBuildReleaseInitialFilesystemBoundary:
    """`build_release` 의 초기 파일시스템 조작(out_dir mkdir, tmp_dir 정리·생성)이 예외
    변환용 try 보다 먼저 실행돼 OSError 가 raw 로 전파되던 결함. CLI 는 BuildError 만
    잡으므로 traceback 이 노출됐다. fix-design-v17.md [P1]. 실제 파일시스템 조건으로
    유발하며 mock 을 쓰지 않는다."""

    def test_out_dir_occupied_by_regular_file(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        occupied = tmp_path / "dist"
        occupied.write_text("not a directory\n", encoding="utf-8")
        with pytest.raises(b.BuildError) as exc_info:
            b.build_release(repo, occupied)
        assert "release assembly failed" in str(exc_info.value)

    def test_tmp_dir_occupied_by_regular_file(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        out_dir.mkdir()
        (out_dir / b._RELEASE_TMP_DIRNAME).write_text("not a directory\n", encoding="utf-8")
        with pytest.raises(b.BuildError) as exc_info:
            b.build_release(repo, out_dir)
        assert "release assembly failed" in str(exc_info.value)


class TestBuildReleaseReturnsVersion:
    """[T1] `build_release` 는 게시 시 확정한 version 을 함께 돌려준다 -- CLI 가 게시된
    manifest 를 다시 읽을 필요 자체를 없앤다. fix-design-v20.md."""

    def test_returns_staged_root_and_version(self, tmp_path):
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"
        staged_root, version = b.build_release(repo, out_dir)
        assert staged_root == out_dir / "localcrab-plugin"
        assert staged_root.is_dir()
        assert version == "1.0.0"
        # 반환한 version 이 실제 게시물 이름을 확정한 값과 같아야 한다.
        assert (out_dir / f"localcrab-plugin-{version}.tar.gz").is_file()
        assert (out_dir / f"localcrab-plugin-{version}.RELEASE.SHA256SUMS").is_file()
