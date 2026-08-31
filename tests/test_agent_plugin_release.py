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

import hashlib
import io
import json
import os
import re
import sys
import tarfile
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
        repo = _fake_repo(tmp_path, version="1.0.0")
        out_dir = tmp_path / "dist"

        real_replace = os.replace
        call_count = {"n": 0}

        def fake_replace(src, dst, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == fail_at:
                raise OSError(f"injected failure at call {fail_at}")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(b.os, "replace", fake_replace)

        with pytest.raises(Exception):
            b.build_release(repo, out_dir)

        monkeypatch.undo()

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

    def test_full_recompute_set_passes_characterization(self, release):
        """성격규정(characterization) -- 진본성 비보장 경계를 명문화한다.

        아카이브·패키지 사이드카·RELEASE.SHA256SUMS 를 전량 함께 재계산해 세트 내부
        일관성을 유지한 채로 COMPATIBILITY.md 내용을 임의로 바꿔도 verify_release 는
        통과한다. 이는 결함이 아니라 verify_release 의 설계된 한계다: 무결성·일관성만
        검출하고 진본성은 검출하지 않는다. 진본성 대사는 공표 기준값(운영 정책 문서)의
        몫이다.
        """
        report = release / "localcrab-plugin-1.0.0.COMPATIBILITY.md"
        report.write_text("공격자가 전량 재계산해 바꿔치기한 본문\n", encoding="utf-8")
        _recompute_release(release, "1.0.0")
        b.verify_release(release)  # 통과 -- 진본성 비보장의 실증.


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
