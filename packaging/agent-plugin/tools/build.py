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

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
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


# ---------------------------------------------------------------------------
# 이슈 #247: 릴리스 세트(결정론 아카이브 + compat report + RELEASE.SHA256SUMS)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_RELEASE_TMP_DIRNAME = ".release-build-tmp"
_ARCHIVE_PREFIX = "localcrab-plugin/"
_HASH_LINE_RE = re.compile(r"([0-9a-f]{64})  (.+)")


def _safe_version(version: str) -> str:
    """릴리스 파일명에 안전하게 삽입 가능한 버전 문자열인지 검사한다.

    경로 구분자·`..` 류 이탈을 막는 파일명 안전성 검사일 뿐이며 PEP 440 전면 검증은 하지
    않는다 -- 이 경계의 책임은 파일명 안전성으로 한정한다.
    """
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise BuildError(f"버전 문자열이 파일명으로 안전하지 않다: {version!r}")
    return version


def _strip_fenced_code_blocks(text: str) -> str:
    """펜스 코드 블록(``` 또는 ~~~, 3개 이상) 내부 라인을 제거한 본문을 돌려준다.

    CommonMark 전체 파서가 아니라 간단한 상태 순회다 -- compat 정본의 표 실재 여부만
    확인하면 되는 이 경계에는 이 정도로 충분하다.
    """
    out_lines = []
    fence_char: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if fence_char is None:
            if stripped.startswith("```"):
                fence_char = "`"
                continue
            if stripped.startswith("~~~"):
                fence_char = "~"
                continue
            out_lines.append(line)
        else:
            if fence_char == "`" and stripped.startswith("```"):
                fence_char = None
            elif fence_char == "~" and stripped.startswith("~~~"):
                fence_char = None
            # 펜스 내부 라인은 버린다(닫는 펜스 라인 자체도 버린다).
    return "\n".join(out_lines)


def _is_table_separator_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or "-" not in stripped:
        return False
    return set(stripped) <= set("|-: ")


def _has_markdown_table(text: str) -> bool:
    """펜스 코드 블록 밖 본문에 마크다운 표(헤더 행 + 구분선 행)가 있는지 확인한다."""
    body = _strip_fenced_code_blocks(text)
    lines = body.splitlines()
    for i in range(len(lines) - 1):
        header, separator = lines[i], lines[i + 1]
        if "|" in header and _is_table_separator_line(separator):
            return True
    return False


def write_compat_report(repo_root, out_dir, version: str) -> Path:
    """`docs/agent-plugin-compatibility.md` 정본을 verbatim 으로 포함하는 릴리스 compat
    report 를 `<out_dir>/localcrab-plugin-<version>.COMPATIBILITY.md` 로 쓴다.

    정본 파일은 파싱 없이 그대로 읽어(verbatim) 결정론 preamble(패키지명, 버전, 정본의
    절대 GitHub URL, 검증 명령 2종) 뒤에 이어 붙인다. 타임스탬프·환경값·절대 로컬경로는
    두지 않는다 -- preamble 이 재현성을 깨는 원천이 되지 않게 하기 위함이다.

    구조 검증은 최소한만 한다: 펜스 코드 블록 밖 본문에 마크다운 표(헤더 행 + 구분선 행)가
    실재해야 한다 -- 정본이 비었거나 표가 사라진 부패를 게이트하는 목적이며 표 내용 자체를
    해석하지는 않는다.
    """
    version = _safe_version(version)
    repo_root = Path(repo_root)
    out_dir = Path(out_dir)

    compat_path = repo_root / "docs" / "agent-plugin-compatibility.md"
    if not compat_path.is_file():
        raise BuildError(f"compat 정본 문서가 없다: {compat_path}")

    compat_bytes = compat_path.read_bytes()
    try:
        compat_text = compat_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(f"compat 정본 문서가 UTF-8 이 아니다: {compat_path}") from exc

    if not _has_markdown_table(compat_text):
        raise BuildError(
            f"compat 정본 문서에 마크다운 표(헤더 행 + 구분선 행)가 없다: {compat_path}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"localcrab-plugin-{version}.COMPATIBILITY.md"
    preamble = (
        f"# localcrab-plugin {version} 호환성 리포트\n\n"
        "이 문서는 릴리스 세트 동봉용 compatibility report 다. 아래 본문은 정본 문서\n"
        "(docs/agent-plugin-compatibility.md) 전체를 verbatim 으로 포함한다.\n\n"
        "정본: https://github.com/epruseal/localcrab/blob/main/docs/agent-plugin-compatibility.md\n\n"
        "검증 명령:\n\n"
        f"    sha256sum -c localcrab-plugin-{version}.RELEASE.SHA256SUMS\n"
        "    python scripts/build_agent_plugin.py --verify --out <dist>\n\n"
        "---\n\n"
    )
    with open(report_path, "wb") as f:
        f.write(preamble.encode("utf-8"))
        f.write(compat_bytes)
    return report_path


def _deterministic_tar(staged_root: Path, archive_path: Path) -> None:
    """staged_root 하위 파일들을 결정론적 tar.gz 로 archive_path 에 쓴다.

    정렬된 파일 멤버만 담는다(디렉터리 엔트리 없음). 멤버명은 `localcrab-plugin/<rel posix>`.
    TarInfo 는 uid=gid=0, uname=gname="", mtime=0, mode=0o644 로 정규화한다. USTAR 포맷 +
    UTF-8 + errors="strict" 로 고정해 PAX 헤더로 인한 비결정성과 비 UTF-8 이름의 우연 통과를
    배제한다. gzip 헤더에는 파일명·시각을 기록하지 않는다(mtime=0, filename="").

    재현성 주장 범위: 동일 도구체계(CPython 동일 계열 + 번들 zlib)와 LF checkout(개행
    번역이 없는 POSIX 도구체계) 에서 바이트 동일 -- zlib 구현이 다른 환경 간 gz 바이트
    동일까지는 주장하지 않는다. 권위 해시는 릴리스 빌드가 산출한 값이다.
    """
    staged_root = Path(staged_root)
    archive_path = Path(archive_path)
    rels = sorted(_relative_files(staged_root))
    with open(archive_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="", compresslevel=9) as gz:
            with tarfile.open(
                fileobj=gz,
                mode="w",
                format=tarfile.USTAR_FORMAT,
                encoding="utf-8",
                errors="strict",
            ) as tar:
                for rel in rels:
                    data = (staged_root / rel).read_bytes()
                    info = tarfile.TarInfo(name=f"{_ARCHIVE_PREFIX}{rel.as_posix()}")
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mode = 0o644
                    info.type = tarfile.REGTYPE
                    tar.addfile(info, io.BytesIO(data))


def build_release(repo_root, out_dir) -> Path:
    """`build()` 산출물에 결정론 아카이브·compat report·릴리스 해시 세트를 더해 원자적으로
    게시하고 staged 디렉터리 경로를 돌려준다.

    단일 작성자를 전제한다 -- 동일 out_dir 에 대한 동시 build_release 호출은 지원하지
    않는다(조립 공간 `out_dir/.release-build-tmp/` 를 공유하므로 경합 시 결과가 정의되지
    않는다). 전원 장애 등에 대한 fsync 수준의 내구성도 보장하지 않는다. 이 함수가 보장하는
    성질은 "부분 게시 상태는 반드시 verify_release 에서 실패로 드러난다(fail-closed)"는
    것뿐이며, 파일시스템 수준의 전면 트랜잭션은 제공하지 않는다.

    절차:
      1. 격리 조립 -- out_dir/.release-build-tmp/ (선존재 시 제거 후 재생성) 안에서
         build() + compat report + 결정론 아카이브 + RELEASE.SHA256SUMS 를 전부 완성한다.
         이 단계의 어떤 실패도 out_dir 의 기존 게시물을 건드리지 않는다.
      2. 게시(커밋 마커 순서) -- 기존 RELEASE.SHA256SUMS 를 먼저 지워 out_dir 를 "미게시"
         상태로 만든 뒤, 기존 staged 디렉터리·사이드카·구버전 릴리스 파일을 지우고, 신규
         산출물을 os.replace 로 최종 이름으로 옮긴다(staged 디렉터리 -> 사이드카 -> tar.gz
         -> COMPATIBILITY.md -> RELEASE.SHA256SUMS 순, RELEASE 가 커밋 마커라 마지막).
    """
    repo_root = Path(repo_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / _RELEASE_TMP_DIRNAME

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        staged_root = build(repo_root, tmp_dir)
        manifest = json.loads((staged_root / "plugin.json").read_text(encoding="utf-8"))
        version = _safe_version(manifest["version"])

        report_tmp = write_compat_report(repo_root, tmp_dir, version)
        archive_tmp = tmp_dir / f"localcrab-plugin-{version}.tar.gz"
        _deterministic_tar(staged_root, archive_tmp)
        sidecar_tmp = tmp_dir / "localcrab-plugin.SHA256SUMS"

        entries = sorted(
            (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in (archive_tmp, report_tmp, sidecar_tmp)
        )
        release_tmp = tmp_dir / f"localcrab-plugin-{version}.RELEASE.SHA256SUMS"
        release_tmp.write_text(
            "\n".join(f"{digest}  {name}" for name, digest in entries) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"release assembly failed: {exc}") from exc

    final_staged = out_dir / "localcrab-plugin"
    final_sidecar = out_dir / "localcrab-plugin.SHA256SUMS"
    final_archive = out_dir / archive_tmp.name
    final_report = out_dir / report_tmp.name
    final_release = out_dir / release_tmp.name

    try:
        # a. 기존 RELEASE 마커 제거 -- 이 순간부터 out_dir 는 "미게시" 상태다.
        for stale_release in out_dir.glob("localcrab-plugin-*.RELEASE.SHA256SUMS"):
            stale_release.unlink()
        # b. 기존 staged 디렉터리·사이드카·구버전 릴리스 파일(빌더 소유 glob) 제거.
        if final_staged.exists():
            shutil.rmtree(final_staged)
        if final_sidecar.exists():
            final_sidecar.unlink()
        for pattern in ("localcrab-plugin-*.tar.gz", "localcrab-plugin-*.COMPATIBILITY.md"):
            for stale in out_dir.glob(pattern):
                stale.unlink()
        # c. 신규 산출물 이동 (동일 파일시스템 -- tmp_dir 는 out_dir 하위).
        os.replace(staged_root, final_staged)
        os.replace(sidecar_tmp, final_sidecar)
        os.replace(archive_tmp, final_archive)
        os.replace(report_tmp, final_report)
        # d. RELEASE.SHA256SUMS 를 마지막에 이동 -- 커밋 마커.
        os.replace(release_tmp, final_release)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"release publish failed: {exc}") from exc

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return final_staged


def _parse_hash_list(text: str, *, reject_separators: bool) -> tuple[dict[str, str], list[str]]:
    """`sha256sum -c` 포맷(`<64자 hex>  <이름>`) 목록을 파싱한다.

    중복·절대경로·`..` 항목과 포맷 위반 행을 위반 목록에 모아 돌려준다. reject_separators
    가 참이면 경로 구분자(`/`, `\\`) 를 포함한 이름도 거부한다(RELEASE.SHA256SUMS 는 형제
    파일명만 담아야 하고, 패키지 사이드카는 스테이징 트리의 상대경로를 정당하게 담는다).

    위반이 있는 행은 `entries` 에 등록하지 않는다(방어 심도) -- 위반 목록만으로 이미 실패가
    확정되므로, 등록해서 얻는 이득 없이 절대경로·`..` 항목을 뒤이어 그대로 해싱 대상으로
    넘기는 부작용(out_dir 밖 경로를 read-only 로 여는 것)만 남기지 않기 위함이다. 중복 항목은
    최초 유효 항목만 유지한다(이후 동명 행은 위반으로 기록되고 값은 덮어쓰지 않는다).
    """
    entries: dict[str, str] = {}
    violations: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _HASH_LINE_RE.fullmatch(line)
        if not match:
            violations.append(f"라인 포맷 위반: {line!r}")
            continue
        digest, name = match.group(1), match.group(2)
        line_ok = True
        if name in entries:
            violations.append(f"중복 항목: {name}")
            line_ok = False
        if os.path.isabs(name) or ".." in Path(name).parts:
            violations.append(f"경로 이탈 항목: {name}")
            line_ok = False
        if reject_separators and ("/" in name or "\\" in name):
            violations.append(f"경로 구분자 포함 항목: {name}")
            line_ok = False
        if line_ok:
            entries[name] = digest
    return entries, violations


# -- PR #257 리뷰 라운드 2 [W1] 대응: 경계 없는 전체 판독(메모리 소진)과 무근거 확립 없는
# 아카이브 파싱을 막기 위한 경계 상수·헬퍼 (fix-design-v6~v11, v11 최종) ------------------

_VERIFY_CHUNK_BYTES = 1 * 1024 * 1024  # 1 MiB -- 모든 청크 판독 루프의 공통 단위
_MAX_SUMS_BYTES = 1 * 1024 * 1024  # 1 MiB -- RELEASE 자체 + 이름이 .SHA256SUMS 로 끝나는 항목
_MAX_RELEASE_FILE_BYTES = 512 * 1024 * 1024  # 512 MiB -- 그 외 RELEASE 항목(tar.gz/COMPATIBILITY.md)
_MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024 * 1024  # 256 MiB -- 아카이브 전개(물리+논리) 예산
_MAX_MEMBERS = 1000  # 아카이브 멤버 수 상한(스트림 모드에서도 tar.members 는 누적된다)


class _BudgetExceededError(Exception):
    """청크 판독 누적량이 허용 예산을 초과했다(초과 인지 직후 즉시 발생)."""


class _LimitedReader:
    """fileobj 를 감싸 매 read() 요청 크기를 남은 예산+1 로 clamp 하고, 누적 소비가
    예산을 초과하는 순간 `_BudgetExceededError` 를 던진다.

    clamp 는 요청 자체를 줄이므로 피감쌈 객체(gzip 내부 read-ahead 포함)가 한 번에
    아무리 큰 크기를 요구해도 그 한 번의 clamp 된 요청분만큼만 예산을 초과할 수
    있다(최대 +1 read 초과) -- 그 직후 예외로 중단되므로 무제한 확장은 불가능하다.
    이 클램프 하나로 RELEASE/사이드카 적재, 아이템 파일 해싱, 아카이브 전체 전개 예산을
    전부 통일해 처리한다(경로마다 clamp 산식을 따로 두지 않는다).
    """

    def __init__(self, fileobj, budget: int) -> None:
        self._fileobj = fileobj
        self._budget = budget
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._budget - self.consumed
        request = (remaining + 1) if (size is None or size < 0) else min(size, remaining + 1)
        if request < 0:
            request = 0
        chunk = self._fileobj.read(request)
        self.consumed += len(chunk)
        if self.consumed > self._budget:
            raise _BudgetExceededError(self.consumed)
        return chunk


def _open_regular(path: Path):
    """lstat -> O_NOFOLLOW open -> fstat 순서로만 일반 파일을 연다.

    lstat 를 open() 이전에 실행한다 -- FIFO 는 open(O_RDONLY) 가 대응 writer 를 무기한
    기다리며 블록하므로, 블로킹 open 을 시도하기 전에 걸러야 한다(정적 스냅샷
    위협모델 -- verify_release 독스트링 참고 -- 에서는 이 lstat 시점 판단을 신뢰할
    근거로 삼는다). open 이후 fstat 재확인은 lstat-open 사이에 경로가 다른 파일
    타입으로 치환되는 레이스의 일부만 방어한다(전체 보장이 아니다). O_NOFOLLOW 는
    경로의 마지막 구성요소에만 적용된다(그 앞 경로 성분의 symlink 는 보호 대상이
    아니다).
    """
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise OSError(f"일반 파일이 아니다(symlink/특수 파일 거부): {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise OSError(f"open 이후 재확인에서 일반 파일이 아니다: {path}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _read_all_limited(fileobj, *, budget: int) -> bytes:
    """budget+1 바이트까지 clamp 해 청크 단위로 누적 판독한다. 초과 시
    `_BudgetExceededError` 를 던진다(전체를 먼저 적재한 뒤 길이를 재는 방식이 아니라,
    판독 자체를 경계에서 멈춘다)."""
    limited = _LimitedReader(fileobj, budget)
    chunks: list[bytes] = []
    while True:
        chunk = limited.read(_VERIFY_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _sha256_stream(fileobj, *, budget: int) -> tuple[str, int]:
    """fileobj 를 청크 단위로 판독하며 sha256 을 누적한다(RELEASE 항목 파일 해싱
    전용 -- 예산은 이 호출 하나가 스스로 강제한다). 반환값은 (hexdigest, 소비
    바이트 수). budget 초과 시 `_BudgetExceededError` 를 던진다."""
    limited = _LimitedReader(fileobj, budget)
    digest = hashlib.sha256()
    while True:
        chunk = limited.read(_VERIFY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest(), limited.consumed


def _hash_chunked(fileobj) -> str:
    """fileobj 를 청크 단위로 판독하며 sha256 을 누적한다(아카이브 멤버 해싱 전용 --
    자체 예산을 두지 않는다. 이 fileobj(`tar.extractfile()` 반환)의 모든 판독은 이미
    아카이브 전체를 감싼 `_LimitedReader` 를 통과하므로, 예산 시행은 그쪽 하나로
    충분하다 -- PR #257 리뷰 라운드 2 [W1])."""
    digest = hashlib.sha256()
    while True:
        chunk = fileobj.read(_VERIFY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _line_count_for_name(text: str, name: str) -> int:
    """RELEASE 원문에서 `name` 을 정확히 지칭하는 라인 수를 형식 유효 여부와 무관하게
    센다. `_parse_hash_list` 는 최초 유효 항목만 dict 에 유지하므로, dict 존재
    여부만으로는 중복 라인이 있었는지 알 수 없다 -- [X1] 긍정 확립 게이트가 "정확히
    1개" 를 판정하는 데 이 카운트를 쓴다."""
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _HASH_LINE_RE.fullmatch(line)
        if match and match.group(2) == name:
            count += 1
    return count


def verify_release(out_dir) -> None:
    """out_dir 의 릴리스 세트(아카이브 + 패키지 사이드카 + RELEASE.SHA256SUMS) 내부
    무결성·일관성을 검증한다. 위반이 있으면 전체 목록을 모아 단일 BuildError 로 알린다.

    신뢰 경계: 이 함수는 무결성(우발적 손상)과 일관성(세트 내부 상호 대사)만 검출한다.
    **진본성은 보장하지 않는다** -- 아카이브·패키지 사이드카·RELEASE.SHA256SUMS 를 전량
    재계산해 일관되게 바꿔치기하면 이 검증은 통과한다(공격자가 세 파일을 모두 통제하는
    경우). 이는 결함이 아니라 설계된 한계다: 진본성 대사(예: GitHub Release notes 에 기재된
    RELEASE.SHA256SUMS 자체의 공표 기준값과의 대사)는 운영 정책 문서의 몫이다.

    위협모델(PR #257 리뷰 라운드 2 이후 명문화): out_dir 는 **정적 스냅샷**으로
    취급한다 -- 이 함수 실행 중 로컬 행위자가 out_dir 파일을 동시에 변경하는 시나리오는
    범위 밖이다(그런 행위자는 검증 종료 직후에도 파일을 바꿔치기할 수 있어 어떤 검증도
    무의미해지므로, 그 위협에 대응하는 락/스냅샷 장치는 이 함수의 책임이 아니다). 파일은
    lstat 로 symlink/특수 파일을 먼저 배제한 뒤(FIFO 의 open() 무기한 블록을 피하기
    위함) O_NOFOLLOW 로 열고 fstat 로 재확인한다(`_open_regular` 참고) -- 이는
    lstat-open 사이의 파일 타입 치환 레이스 일부만 방어한다. 아카이브 항목은 해시
    확인에 쓴 fd 를 닫지 않고 그대로 seek(0) 해 파싱에 재사용하는데, 이는 "확립한
    바이트와 파싱한 바이트가 동일함"을 보장하지 않는다 -- 경로 치환(rename/재지정)을
    막고 최종 경로 구성요소의 symlink 를 거부하는 정도의 보장이다. 크기 상한은 fstat
    이 아니라 실제 판독 루프(clamp 된 read)로만 강제한다 -- fstat 시점 크기 확인은
    판독 도중의 크기 변화(TOCTOU)에 취약한 빠른 실패 신호일 뿐이다.

    repo_root 를 요구하지 않는다 -- 수령자가 dist 세트만으로 실행 가능해야 한다.
    """
    out_dir = Path(out_dir)

    try:
        release_candidates = sorted(out_dir.glob("localcrab-plugin-*.RELEASE.SHA256SUMS"))
    except OSError as exc:
        raise BuildError(f"out_dir 를 나열할 수 없다: {exc}") from exc
    if len(release_candidates) != 1:
        raise BuildError(
            "RELEASE.SHA256SUMS 파일이 정확히 1개여야 한다 "
            f"(발견: {len(release_candidates)}개: {[p.name for p in release_candidates]})"
        )
    release_path = release_candidates[0]
    name_match = re.fullmatch(
        r"localcrab-plugin-(?P<version>.+)\.RELEASE\.SHA256SUMS", release_path.name
    )
    if not name_match:
        raise BuildError(f"RELEASE 파일명 형식 위반: {release_path.name}")
    version = name_match.group("version")

    # RELEASE 목록 자체가 못 읽히면 이후 검증이 전부 무의미하므로(위반 목록에 누적하지
    # 않고) 즉시 단일 오류로 종료한다 -- 위의 "정확히 1개" 전제 실패와 같은 클래스다.
    try:
        release_file = _open_regular(release_path)
    except OSError as exc:
        raise BuildError(f"RELEASE.SHA256SUMS 를 읽을 수 없다: {exc}") from exc
    try:
        try:
            release_bytes = _read_all_limited(release_file, budget=_MAX_SUMS_BYTES)
        except _BudgetExceededError:
            raise BuildError(
                f"RELEASE.SHA256SUMS 크기가 허용 상한을 초과했다({_MAX_SUMS_BYTES} 바이트)"
            ) from None
    finally:
        release_file.close()
    try:
        release_text = release_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(
            f"RELEASE.SHA256SUMS 가 UTF-8 이 아니다(손상 가능): {release_path.name}"
        ) from exc

    violations: list[str] = []

    release_entries, release_violations = _parse_hash_list(release_text, reject_separators=True)
    violations.extend(f"RELEASE: {v}" for v in release_violations)

    archive_name = f"localcrab-plugin-{version}.tar.gz"
    compat_name = f"localcrab-plugin-{version}.COMPATIBILITY.md"
    sidecar_name = "localcrab-plugin.SHA256SUMS"
    expected_names = {archive_name, compat_name, sidecar_name}
    actual_names = set(release_entries)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        detail = []
        if missing:
            detail.append(f"missing={missing}")
        if unexpected:
            detail.append(f"unexpected={unexpected}")
        violations.append("RELEASE 구성 불일치 (" + ", ".join(detail) + ")")

    # [X1] 긍정 확립 게이트: 아카이브 이름을 지칭하는 RELEASE 라인이 정확히 1개이고
    # (dict 에 등록된) 정형이며 실제 파일의 스트림 해시와 일치하는 경우에만 파싱을
    # 진행한다. 그 밖의 모든 경우(누락/포맷 위반/중복/해시 불일치)는 전부 "생략" 으로
    # 취급한다 -- 미래의 미열거 엣지케이스에도 fail-closed 하도록 부정 조건을 열거하는
    # 대신 긍정 조건 하나만 확인한다.
    archive_line_count = _line_count_for_name(release_text, archive_name)
    archive_confirmed = False
    archive_file = None  # 확립되면 해시에 쓴 fd 를 닫지 않고 그대로 파싱에 재사용한다.

    for name, digest in release_entries.items():
        path = out_dir / name
        if not path.is_file():
            violations.append(f"파일 없음: {name}")
            continue
        cap = _MAX_SUMS_BYTES if name.endswith(".SHA256SUMS") else _MAX_RELEASE_FILE_BYTES
        try:
            entry_file = _open_regular(path)
        except OSError:
            violations.append(f"파일을 읽을 수 없다: {name}")
            continue
        try:
            actual_digest, _consumed = _sha256_stream(entry_file, budget=cap)
        except _BudgetExceededError:
            violations.append(f"파일 크기가 허용 상한을 초과했다: {name}")
            entry_file.close()
            continue
        if actual_digest != digest:
            violations.append(f"해시 불일치: {name}")
            entry_file.close()
            continue
        if name == archive_name and archive_line_count == 1:
            archive_confirmed = True
            archive_file = entry_file  # 아래 아카이브 처리부의 finally 에서 닫는다.
        else:
            entry_file.close()

    sidecar_entries: dict[str, str] = {}
    sidecar_path = out_dir / sidecar_name
    try:
        sidecar_file = _open_regular(sidecar_path)
    except OSError as exc:
        violations.append(f"패키지 사이드카를 읽을 수 없다: {exc}")
    else:
        sidecar_bytes = None
        try:
            try:
                sidecar_bytes = _read_all_limited(sidecar_file, budget=_MAX_SUMS_BYTES)
            except _BudgetExceededError:
                violations.append(
                    f"패키지 사이드카 크기가 허용 상한을 초과했다({_MAX_SUMS_BYTES} 바이트)"
                )
        finally:
            sidecar_file.close()
        if sidecar_bytes is not None:
            try:
                sidecar_text = sidecar_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                violations.append(f"패키지 사이드카가 UTF-8 이 아니다(손상 가능): {exc}")
            else:
                sidecar_entries, sidecar_violations = _parse_hash_list(
                    sidecar_text, reject_separators=False
                )
                violations.extend(f"사이드카: {v}" for v in sidecar_violations)

    if not archive_confirmed:
        violations.append("아카이브 파싱 생략: 외부 체크섬 미확립")
        if archive_file is not None:  # 방어적: 이 분기는 실제로 도달하지 않는다.
            archive_file.close()
    else:
        raw_member_names: list[str] = []
        member_hashes: dict[str, str] = {}
        gz = None
        tar = None
        try:
            archive_file.seek(0)
            # 신뢰 경계(파서 경계): 이 try 는 gzip 해제 + tarfile 스트림 순회(검사·읽기·
            # 해시 수집)까지만 감싼다 -- 손상된 tar.gz 는 tarfile 이 TarError 외에도
            # EOFError(절단, /home/asdf/orch-scratch/o247/p2b-repro 실측)·ValueError(손상
            # PAX/sparse 헤더, 3차 채널 B 실측)·zlib.error 등을 던질 수 있고 그 전수 열거는
            # CPython tarfile 파서 버전에 종속돼 유지 불가능하므로 `except Exception` 으로
            # 광역 수렴한다. `_BudgetExceededError` 는 그보다 먼저 잡아 별도 위반으로 구분한다.
            # mode="r|"(스트림) + 멤버 수 상한을 함께 쓴다 -- 스트림 모드도 tar.members 에
            # TarInfo 를 계속 누적하므로(CPython 실측, 3.11/3.13 동일), 스트림 모드 자체가
            # 아니라 멤버 수 상한이 그 누적을 막는다.
            gz = gzip.GzipFile(fileobj=archive_file)
            limited = _LimitedReader(gz, _MAX_ARCHIVE_EXPANDED_BYTES)
            tar = tarfile.open(fileobj=limited, mode="r|")
            member_count = 0
            logical_remaining = _MAX_ARCHIVE_EXPANDED_BYTES
            for member in tar:
                member_count += 1
                if member_count > _MAX_MEMBERS:
                    violations.append(f"아카이브 멤버 수 상한 초과: {_MAX_MEMBERS}")
                    break
                if member.issparse():
                    violations.append(f"아카이브 sparse 멤버 거부: {member.name}")
                    continue
                if member.size < 0:
                    violations.append(f"아카이브 멤버 크기 위반(음수): {member.name}")
                    continue
                # [Z1] 논리 예산: 물리 계층(`_LimitedReader`)과 별개로, 선언된 크기를
                # 처리 전에 미리 차감한다 -- sparse 는 위에서 이미 무조건 거부하므로,
                # 이 계층은 향후 다른 방식의 합성 확장 벡터에 대비한 방어 심도다.
                logical_remaining -= member.size
                if logical_remaining < 0:
                    violations.append("아카이브 전개 크기 상한 초과")
                    break
                raw_member_names.append(member.name)
                if not member.isfile():
                    violations.append(
                        f"아카이브 비정규 멤버: {member.name} (type={member.type!r})"
                    )
                    continue
                if not member.name.startswith(_ARCHIVE_PREFIX):
                    violations.append(f"아카이브 prefix 위반: {member.name}")
                    continue
                rel = member.name[len(_ARCHIVE_PREFIX) :]
                if not rel or rel.startswith("/") or ".." in Path(rel).parts:
                    violations.append(f"아카이브 경로 이탈: {member.name}")
                    continue
                extracted = tar.extractfile(member)
                member_hashes[rel] = _hash_chunked(
                    extracted if extracted is not None else io.BytesIO(b"")
                )
        except _BudgetExceededError:
            violations.append("아카이브 전개 크기 상한 초과")
        except Exception as exc:
            violations.append(f"아카이브를 열 수 없다(손상 가능): {type(exc).__name__}: {exc}")
        else:
            dup_counts: dict[str, int] = {}
            for member_name in raw_member_names:
                dup_counts[member_name] = dup_counts.get(member_name, 0) + 1
            dup_names = sorted(name for name, count in dup_counts.items() if count > 1)
            if dup_names:
                violations.append(f"아카이브 중복 멤버: {dup_names}")

            member_rel_set = set(member_hashes)
            sidecar_rel_set = set(sidecar_entries)
            if member_rel_set != sidecar_rel_set:
                missing = sorted(sidecar_rel_set - member_rel_set)
                unexpected = sorted(member_rel_set - sidecar_rel_set)
                detail = []
                if missing:
                    detail.append(f"missing={missing}")
                if unexpected:
                    detail.append(f"unexpected={unexpected}")
                violations.append("아카이브-사이드카 멤버 집합 불일치 (" + ", ".join(detail) + ")")
            else:
                for rel, digest in sidecar_entries.items():
                    if member_hashes.get(rel) != digest:
                        violations.append(f"아카이브 해시 불일치: {rel}")
        finally:
            # gzip.GzipFile/tarfile.TarFile 은 외부에서 넘긴 fileobj 를 자신의 close()
            # 에서 닫지 않는다(본 세션 실측: TrackedBytesIO 로 close() 미호출 확인) --
            # 따라서 열었던 역순(tar -> gz -> archive_file)으로 명시적으로 닫는다.
            if tar is not None:
                tar.close()
            if gz is not None:
                gz.close()
            archive_file.close()

    if violations:
        raise BuildError("release verification failed:\n" + "\n".join(f"  - {v}" for v in violations))
