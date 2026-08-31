"""Agent Plugin 빌더 -- src/ 게이트 -> allowlist 스테이징 -> SHA256SUMS 사이드카.

3단계(설계 v3 §7):
  1. src 게이트: packaging/agent-plugin/src/ 전량 열거, symlink 거부, allowlist
     밖 잉여 거부, authoring(게이트) 검증(LICENSE 부재는 이 단계에서 오류 아님).
  2. 스테이징: out_dir/localcrab-plugin/ 에 allowlist + repo LICENSE 복사,
     전량 열거 == STAGED_ALLOWLIST, 게이트 검증 재실행(LICENSE 포함),
     plugin.json version == pyproject [project].version(tomllib).
  3. 사이드카: out_dir/localcrab-plugin.SHA256SUMS(정렬 상대경로) 생성.
     실패 시 스테이징 디렉터리 제거 후 BuildError.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from pathlib import Path

from .validate import MODE_GATE, validate_package

SRC_ALLOWLIST = {
    "plugin.json",
    "mcp.json",
    "README.md",
    "skills/localcrab-query/SKILL.md",
}
STAGED_ALLOWLIST = SRC_ALLOWLIST | {"LICENSE"}

# 저작 시점(빌드 전) 게이트 검증에는 아직 실제 PLUGIN_DATA 디렉터리가 없다.
# cwd/env 의 ${PLUGIN_DATA} 형식을 lexical+realpath 로 이중 확인하려면 실존은
# 필요 없다(os.path.realpath 는 미존재 경로도 정규화만 한다) -- 문법 검증용
# 합성 루트 하나면 충분하다. 실제 런타임 상태 디렉터리와는 무관하다.
_AUTHORING_PLUGIN_DATA = "/nonexistent/agent-plugin-authoring-plugin-data"


class BuildError(Exception):
    """빌드 실패(게이트 위반, allowlist 불일치, version 불일치 등)."""


def _relative_files(root: Path) -> list[Path]:
    """root 하위 전 파일의 상대경로(정렬, 디렉터리 제외). symlink 는 즉시 BuildError."""
    out = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_symlink():
            raise BuildError(f"symlink rejected: {rel.as_posix()}")
        if path.is_file():
            out.append(rel)
    return out


def _run_gate(plugin_root: Path, *, require_license: bool) -> None:
    report = validate_package(plugin_root, MODE_GATE, plugin_data=_AUTHORING_PLUGIN_DATA)
    errors = list(report.errors)
    if require_license and not (plugin_root / "LICENSE").is_file():
        errors.append("LICENSE is required in the staged package")
    if errors:
        raise BuildError("authoring gate failed:\n" + "\n".join(f"  - {e}" for e in errors))


def build(repo_root, out_dir) -> Path:
    repo_root = Path(repo_root)
    out_dir = Path(out_dir)
    src_dir = repo_root / "packaging" / "agent-plugin" / "src"

    # --- 1단계: src 게이트 ---
    src_files = {p.as_posix() for p in _relative_files(src_dir)}
    extra = src_files - SRC_ALLOWLIST
    if extra:
        raise BuildError(f"unexpected file(s) in src/: {', '.join(sorted(extra))}")
    missing = SRC_ALLOWLIST - src_files
    if missing:
        # README.md/SKILL.md 는 병렬 저작 중 아직 없을 수 있다 -- 이 경우 이
        # 메시지가 그대로 "정상적으로 실패"하는 self-check 신호가 된다.
        raise BuildError(f"missing required src/ file(s): {', '.join(sorted(missing))}")
    _run_gate(src_dir, require_license=False)

    # --- 2단계: 스테이징 ---
    staged_root = out_dir / "localcrab-plugin"
    if staged_root.exists():
        shutil.rmtree(staged_root)
    staged_root.mkdir(parents=True)
    try:
        for rel in SRC_ALLOWLIST:
            src_path = src_dir / rel
            dst_path = staged_root / rel
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path, follow_symlinks=False)

        license_src = repo_root / "LICENSE"
        if not license_src.is_file() or license_src.is_symlink():
            raise BuildError("repo root LICENSE is required to stage the package")
        shutil.copy2(license_src, staged_root / "LICENSE", follow_symlinks=False)

        staged_files = {p.as_posix() for p in _relative_files(staged_root)}
        if staged_files != STAGED_ALLOWLIST:
            unexpected = staged_files - STAGED_ALLOWLIST
            absent = STAGED_ALLOWLIST - staged_files
            detail = []
            if unexpected:
                detail.append(f"unexpected: {', '.join(sorted(unexpected))}")
            if absent:
                detail.append(f"missing: {', '.join(sorted(absent))}")
            raise BuildError("staged package does not match allowlist (" + "; ".join(detail) + ")")

        _run_gate(staged_root, require_license=True)

        manifest = json.loads((staged_root / "plugin.json").read_text(encoding="utf-8"))
        pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        pkg_version = pyproject["project"]["version"]
        if manifest.get("version") != pkg_version:
            raise BuildError(
                f"plugin.json version {manifest.get('version')!r} != pyproject version {pkg_version!r}"
            )

        # --- 3단계: 사이드카 ---
        sidecar_path = out_dir / "localcrab-plugin.SHA256SUMS"
        lines = []
        for rel in sorted(_relative_files(staged_root)):
            digest = hashlib.sha256((staged_root / rel).read_bytes()).hexdigest()
            lines.append(f"{digest}  {rel.as_posix()}")
        sidecar_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        shutil.rmtree(staged_root, ignore_errors=True)
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"staging failed: {exc}") from exc

    return staged_root
