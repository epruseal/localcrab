"""Agent Plugins 1.0.0 이중모드 검증기 (gate=저작/CI, loader=참조 클라이언트).

스펙 §4.1(경계 격리) · §5(plugin.json) · §6(발견) · §7(mcp.json) · §8(extensions)
· §9(환경/placeholder) · §10.1(버전 정합) · §11(클라이언트 준수 이중 실패 경계)을
근거로 한다. jsonschema 는 canonical_validate() 안에서만 lazy import 한다
(저작 도구 전용 의존성 -- opencrab 런타임은 요구하지 않는다).
"""

from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MODE_GATE = "gate"
MODE_LOADER = "loader"

PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_PLUGIN_SCHEMA_FILE = _SCHEMA_DIR / "plugin.schema.json"
_MCP_SCHEMA_FILE = _SCHEMA_DIR / "mcp.schema.json"

_PLUGIN_ALLOWED_FIELDS = {
    "$schema", "name", "version", "description", "author",
    "homepage", "repository", "license", "keywords", "extensions",
}
_MCP_ALLOWED_TOP = {"$schema", "mcpServers"}
_STDIO_ALLOWED = {"type", "command", "args", "env", "cwd"}
_STDIO_REQUIRED = {"type", "command"}
_REMOTE_ALLOWED = {"type", "url", "headers"}
_REMOTE_REQUIRED = {"type", "url"}
_RESERVED_ENV_KEYS = {"PLUGIN_ROOT", "PLUGIN_DATA"}

_ALNUM_LOWER = set("abcdefghijklmnopqrstuvwxyz0123456789")
_NAME_CHARSET_RE = re.compile(r"^[a-z0-9.-]+$")
_SKILL_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")
_SCHEMA_VERSION_RE = re.compile(r"/schemas/([^/]+)/")
_FIELD_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

_TEXT_EXTS = {".json", ".md", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".py", ".sh"}

# 오탐 방지: README/스펙 문서 예시가 흔히 쓰는 표기 자체는 시크릿이 아니다.
_SECRET_ALLOWED_LITERALS = ("<PLUGIN_DATA>", "${PLUGIN_ROOT}", "${PLUGIN_DATA}")

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("lc_ 토큰", re.compile(r"lc_[A-Za-z0-9]{8,}")),
    ("sk- 토큰", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("AWS 액세스 키", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub 토큰", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("private key 블록", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY")),
    ("authorization 헤더 값", re.compile(r"(?i)authorization\s*[:=]\s*\S")),
    ("URL 질의 자격증명", re.compile(r"[?&](?:token|key|secret|password)=")),
    ("개인 경로(/home)", re.compile(r"/home/[^/\s]+")),
    ("개인 경로(/Users)", re.compile(r"/Users/[^/\s]+")),
    ("개인 경로(Windows Users)", re.compile(r"C:\\Users\\")),
    ("개인 경로(${HOME})", re.compile(r"\$\{HOME\}")),
    ("개인 경로(~/)", re.compile(r"(?:^|\s)~/")),
    ("운영 파일명(localcrab-kure.env)", re.compile(r"localcrab-kure\.env")),
    ("운영 파일명(localcrab-mcp.token)", re.compile(r"localcrab-mcp\.token")),
]


@dataclass
class ValidationReport:
    """validate_package() 의 집계 결과. servers/skills 는 검증 통과분만 담는다."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    servers: dict[str, dict] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# §5.5 plugin name
# ---------------------------------------------------------------------------


def validate_name(name: str) -> list[str]:
    """§5.5: 1..64자, [a-z0-9.-], 양끝 영숫자, '--'·'..' 금지."""
    if not isinstance(name, str):
        return ["name must be a string"]
    errors: list[str] = []
    if not (1 <= len(name) <= 64):
        errors.append(f"name must be 1-64 characters (got {len(name)})")
    if name and not _NAME_CHARSET_RE.fullmatch(name):
        errors.append("name must contain only lowercase letters, digits, '-', '.'")
    if name and (name[0] not in _ALNUM_LOWER or name[-1] not in _ALNUM_LOWER):
        errors.append("name must start and end with a lowercase alphanumeric character")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens ('--')")
    if ".." in name:
        errors.append("name must not contain consecutive periods ('..')")
    return errors


def _validate_skill_name(name: str) -> list[str]:
    """Agent Skills name: 소문자 a-z0-9 와 하이픈만, 양끝 영숫자, '--' 금지, 1..64자."""
    if not isinstance(name, str):
        return ["name must be a string"]
    errors: list[str] = []
    if not (1 <= len(name) <= 64):
        errors.append("name must be 1-64 characters")
    if not _SKILL_NAME_RE.fullmatch(name):
        errors.append(
            "name must be lowercase alphanumerics/hyphens only, "
            "starting/ending alphanumeric, no consecutive hyphens"
        )
    return errors


# ---------------------------------------------------------------------------
# §7.2.1 command / cwd 형식
# ---------------------------------------------------------------------------


def validate_command_token(command: str) -> list[str]:
    """§7.2.1: bare(경로구분자·공백·placeholder 없음) 또는 './' 시작만 허용.

    공백·placeholder·NUL 검사는 './' 분기에도 적용한다 — "single executable
    token" 은 형식 전체에 대한 요구이고, command 는 클라이언트가 placeholder 를
    확장하지 않는 필드라(§7.2.1) 포함 자체가 저작 오류다 (PR #244 P2-1).
    공백 파일명을 번들하는 spec-경계 사례보다 단일 토큰 해석의 일관성을 택했다.
    """
    if not isinstance(command, str) or not command:
        return ["command must be a non-empty string"]
    errors = []
    if any(ch.isspace() for ch in command):
        errors.append("command must be a single token without whitespace")
    if "\x00" in command:
        # NUL 은 JSON 문자열로 유입 가능하고 이후 경로 API 에서 ValueError 를
        # 유발한다 -- 예외 누출 전에 형식 단계에서 거부한다.
        errors.append("command must not contain NUL bytes")
    if _PLACEHOLDER_RE.search(command) or "${" in command:
        errors.append("command must not contain placeholder expansion")
    if command.startswith("./"):
        if command == "./":
            errors.append("command './' does not name an executable")
    else:
        if "/" in command or "\\" in command:
            errors.append("command must be a bare executable name or a './'-relative path")
        elif command in (".", ".."):
            errors.append("command must name an executable, not a directory reference")
    return errors


def validate_cwd_form(cwd: str) -> list[str]:
    """cwd 는 './...' | '${PLUGIN_ROOT}'[/...] | '${PLUGIN_DATA}'[/...] 만 허용."""
    if not isinstance(cwd, str) or not cwd:
        return ["cwd must be a non-empty string"]
    if "\x00" in cwd:
        # command 와 같은 이유 -- 경로 API 도달 전 거부 (PR #244 P2-2 클래스).
        return ["cwd must not contain NUL bytes"]
    if cwd.startswith("./"):
        return []
    if cwd == "${PLUGIN_ROOT}" or cwd.startswith("${PLUGIN_ROOT}/"):
        return []
    if cwd == "${PLUGIN_DATA}" or cwd.startswith("${PLUGIN_DATA}/"):
        return []
    return ["cwd must start with './', '${PLUGIN_ROOT}', or '${PLUGIN_DATA}'"]


# ---------------------------------------------------------------------------
# §4.1 containment (이중 확인: 어휘적 normpath + 물리적 realpath)
# ---------------------------------------------------------------------------


def _check_root_containment(candidate: str, root: str) -> list[str]:
    norm_root = posixpath.normpath(root)
    norm_candidate = posixpath.normpath(candidate)
    if norm_candidate != norm_root and not norm_candidate.startswith(norm_root + "/"):
        return [f"path escapes root after lexical normalization: {candidate!r} not under {norm_root!r}"]
    try:
        real_root = os.path.realpath(root)
        real_candidate = os.path.realpath(candidate)
    except (ValueError, UnicodeError, OSError):
        # NUL·고립 서로게이트 등 JSON 문자열로 유입 가능한 값이 경로 API 에서
        # 예외를 낸다 -- §7.2.2 의 entry invalid 로 승격한다 (PR #244 P2-3/P2-4).
        # kind 3종(command/cwd/file)·두 모드가 전부 이 초크포인트를 지난다.
        return ["path is not resolvable"]
    if real_candidate != real_root and not (real_candidate + os.sep).startswith(real_root + os.sep):
        return [f"path escapes root after realpath resolution: {candidate!r} resolves outside {real_root!r}"]
    return []


def check_containment(value, plugin_root, plugin_data, kind: str) -> list[str]:
    """kind: 'command'|'cwd'|'file'. placeholder 를 해당 루트로 치환 후 이중 확인한다.

    bare command(플랫폼 PATH 탐색 대상, §7.2.1)는 containment 대상이 아니므로
    './' 접두가 아니면 빈 리스트를 반환한다.
    """
    if kind not in ("command", "cwd", "file"):
        raise ValueError(f"unknown containment kind: {kind!r}")

    if kind == "command":
        if not isinstance(value, str) or not value.startswith("./"):
            return []
        root = plugin_root
        candidate = posixpath.normpath(posixpath.join(root, value[len("./"):]))
    elif kind == "cwd":
        if not isinstance(value, str):
            return ["cwd must be a string"]
        if value == "${PLUGIN_DATA}" or value.startswith("${PLUGIN_DATA}/"):
            root = plugin_data
            suffix = value[len("${PLUGIN_DATA}"):].lstrip("/")
        elif value == "${PLUGIN_ROOT}" or value.startswith("${PLUGIN_ROOT}/"):
            root = plugin_root
            suffix = value[len("${PLUGIN_ROOT}"):].lstrip("/")
        elif value.startswith("./"):
            root = plugin_root
            suffix = value[len("./"):]
        else:
            return [f"cwd form not recognized for containment check: {value!r}"]
        if root is None:
            return ["plugin_data root is required to validate a ${PLUGIN_DATA} cwd"]
        candidate = posixpath.normpath(posixpath.join(root, suffix)) if suffix else posixpath.normpath(root)
    else:  # file
        if not isinstance(value, str):
            return ["file path must be a string"]
        root = plugin_root
        candidate = posixpath.normpath(value) if posixpath.isabs(value) else posixpath.normpath(
            posixpath.join(root, value)
        )

    return _check_root_containment(candidate, root)


# ---------------------------------------------------------------------------
# §7.2.2 remote transport: url / headers
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return addr in ipaddress.ip_network("127.0.0.0/8")
    return addr == ipaddress.IPv6Address("::1")


def validate_url(url) -> list[str]:
    """절대 http(s), userinfo·fragment 금지, non-loopback 은 https 필수.

    loopback = localhost | 127.0.0.0/8 | [::1] 뿐이다. 'localhost.example' 은
    hostname 전체 일치가 아니므로 non-loopback 이다.
    """
    if not isinstance(url, str) or not url:
        return ["url must be a non-empty string"]
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        # 예: 'http://[::1' (IPv6 괄호 불일치). urlsplit/hostname 의 ValueError 를
        # 밖으로 내보내면 게이트·빌드 CLI 가 traceback 으로 죽는다 (PR #244 P2-2).
        return ["url is not a parseable URL"]
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ["url must be an absolute http(s) URL"]
    errors = []
    if "@" in parts.netloc:
        errors.append("url must not contain userinfo")
    if parts.fragment:
        errors.append("url must not contain a fragment")
    if parts.scheme == "http" and not _is_loopback_host(hostname):
        errors.append("non-loopback url must use https")
    return errors


def validate_headers(headers) -> list[str]:
    """대소문자 무시 중복 금지, RFC 9110 field-name 문자, 값 CR/LF 금지, 값 시크릿 스캔."""
    if not isinstance(headers, dict):
        return ["headers must be an object"]
    errors: list[str] = []
    seen_lower: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not _FIELD_NAME_RE.match(name):
            errors.append(f"invalid header field-name: {name!r}")
            continue
        lname = name.lower()
        if lname in seen_lower:
            errors.append(f"duplicate header name (case-insensitive): {name!r}")
        else:
            seen_lower[lname] = name
        if not isinstance(value, str):
            errors.append(f"header value must be a string: {name!r}")
            continue
        if "\r" in value or "\n" in value:
            errors.append(f"header value must not contain CR/LF: {name!r}")
        errors.extend(scan_secrets(value, source=f"header:{name}"))
    return errors


# ---------------------------------------------------------------------------
# §9.2 placeholder 확장 / 시크릿 스캔
# ---------------------------------------------------------------------------


def expand_placeholders(text: str, plugin_root: str, plugin_data: str) -> str:
    """§9.2: 단일 패스 전 발생 치환. re.sub 콜백 단일 호출로 비재귀가 자연 충족된다.

    ${PLUGIN_ROOT}/${PLUGIN_DATA} 외 다른 어떤 placeholder 도 인식하지 않으므로
    미인식 ${FOO} 는 정규식이 매치하지 않아 그대로 보존된다.
    """
    mapping = {"PLUGIN_ROOT": plugin_root, "PLUGIN_DATA": plugin_data}
    return _PLACEHOLDER_RE.sub(lambda m: mapping[m.group(1)], text)


def scan_secrets(text: str, source: str) -> list[str]:
    """시크릿·개인 경로·운영 전용 파일명 패턴 스캔. placeholder 표기 자체는 오탐 제외."""
    if not isinstance(text, str):
        return []
    errors = []
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            errors.append(f"{source}: possible secret/private-path pattern ({label})")
    return errors


def env_text_errors(role: str, key, text) -> list[str]:
    """env 항목의 한쪽(role: "key" | "value") 텍스트를 검사한다 (#248).

    계약: env 키와 값은 UTF-8 로 인코딩 가능한 유니코드 스칼라 문자열(str)이어야
    한다. bytes 를 포함한 비문자열은 "must be a string" 오류다. POSIX
    surrogateescape 로 우연히 바이트 왕복이 되는 저역 서로게이트(U+DC80..U+DCFF)도
    일괄 거부한다: 참조 클라이언트의 목적은 플랫폼 우연 통과가 아니라 결정론적
    §9.1 계약 시연이고(Windows 에서는 왕복이 성립하지 않는다), 정본 유입원인
    mcp.json 은 JSON 텍스트라 정당한 비스칼라 값이 없다. 비 UTF-8 호스트
    경로(surrogateescape 문자열)를 그대로 쓰는 호출자는 이 참조 클라이언트의
    지원 범위 밖이다. 키 문법('=' 포함 등)은 검사하지 않는다: subprocess 가
    명시적 ValueError 로 거부하므로 예외 누출 클래스가 아니다.
    """
    if not isinstance(text, str):
        return [f"env {role} for {key!r} must be a string"]
    errors = []
    if "\x00" in text:
        errors.append(f"env {role} for {key!r} must not contain NUL bytes")
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        errors.append(f"env {role} for {key!r} must not contain lone surrogates")
    return errors


def env_entry_errors(key, value) -> list[str]:
    """env 한 항목의 키·값 양쪽을 검사한다 (#248, refclient 원천 검사용)."""
    return env_text_errors("key", key, key) + env_text_errors("value", key, value)



# ---------------------------------------------------------------------------
# canonical JSON Schema 검증 (jsonschema lazy import)
# ---------------------------------------------------------------------------


def canonical_validate(obj, schema_file) -> list[str]:
    try:
        import jsonschema
    except ImportError as exc:
        raise RuntimeError(
            "jsonschema 가 설치되어 있지 않다. 저작 도구 전용 의존성이므로 "
            "`pip install jsonschema` 로 설치한 뒤 다시 실행하라."
        ) from exc
    with open(schema_file, encoding="utf-8") as f:
        schema = json.load(f)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    return [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(obj)
    ]


def _schema_version(schema_id) -> str | None:
    if not isinstance(schema_id, str):
        return None
    m = _SCHEMA_VERSION_RE.search(schema_id)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# §5 plugin.json
# ---------------------------------------------------------------------------


def _plugin_field_type_errors(obj: dict) -> list[str]:
    """로더 모드 텍스트층: canonical schema 없이 §5.3/§5.4 필수·타입만 재확인."""
    errors = []
    schema_val = obj.get("$schema")
    if schema_val != PLUGIN_SCHEMA_ID:
        errors.append(f"$schema must be {PLUGIN_SCHEMA_ID!r} (got {schema_val!r})")
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        errors.append("name is required and must be a non-empty string")
    if "version" in obj and not isinstance(obj["version"], str):
        errors.append("version must be a string")
    if "description" in obj and not isinstance(obj["description"], str):
        errors.append("description must be a string")
    if "author" in obj:
        author = obj["author"]
        if not isinstance(author, dict):
            errors.append("author must be an object")
        else:
            allowed = {"name", "email", "url"}
            extra = set(author) - allowed
            if extra:
                errors.append(f"author has unsupported field(s): {', '.join(sorted(extra))}")
            for k, v in author.items():
                if k in allowed and not isinstance(v, str):
                    errors.append(f"author.{k} must be a string")
    for field_name in ("homepage", "repository", "license"):
        if field_name in obj and not isinstance(obj[field_name], str):
            errors.append(f"{field_name} must be a string")
    if "keywords" in obj:
        kw = obj["keywords"]
        if not isinstance(kw, list) or not all(isinstance(k, str) for k in kw):
            errors.append("keywords must be an array of strings")
    return errors


def validate_plugin_manifest(
    obj, mode: str, implemented_namespaces: frozenset = frozenset()
) -> tuple[list[str], list[str]]:
    """§5/§8.1/§11.1. 게이트: canonical+텍스트층 전부 오류. 로더: unknown 필드/비객체
    extensions 는 warning+계속, 미구현 네임스페이스 멤버는 값 타입 포함 무검증 무시.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(obj, dict):
        errors.append("plugin.json must be a JSON object")
        return errors, warnings

    unknown_fields = sorted(set(obj) - _PLUGIN_ALLOWED_FIELDS)
    extensions = obj.get("extensions")
    extensions_non_object = "extensions" in obj and not isinstance(extensions, dict)

    if mode == MODE_GATE:
        if unknown_fields:
            errors.append(f"unknown top-level field(s): {', '.join(unknown_fields)}")
        if extensions_non_object:
            errors.append("extensions must be an object")
        errors.extend(canonical_validate(obj, _PLUGIN_SCHEMA_FILE))
    else:
        for f in unknown_fields:
            warnings.append(f"unknown top-level field ignored: {f}")
        if extensions_non_object:
            warnings.append("extensions is not an object; ignoring")
        known_obj = {k: v for k, v in obj.items() if k in _PLUGIN_ALLOWED_FIELDS}
        if extensions_non_object:
            known_obj.pop("extensions", None)
        errors.extend(_plugin_field_type_errors(known_obj))

    name = obj.get("name")
    if isinstance(name, str):
        errors.extend(f"name: {e}" for e in validate_name(name))

    if isinstance(extensions, dict):
        for ns, val in extensions.items():
            if mode == MODE_GATE:
                if not isinstance(val, dict):
                    errors.append(f"extensions.{ns} must be an object")
            elif ns in implemented_namespaces and not isinstance(val, dict):
                errors.append(f"extensions.{ns} must be an object (implemented namespace)")
            # else: §8.1 -- 미구현 네임스페이스 멤버는 값 타입 포함 일절 검증 없이 무시

    return errors, warnings


# ---------------------------------------------------------------------------
# §7 mcp.json
# ---------------------------------------------------------------------------


def _validate_server_entry(entry, plugin_root, plugin_data) -> list[str]:
    if not isinstance(entry, dict):
        return ["server entry must be an object"]
    errors: list[str] = []
    server_type = entry.get("type")

    if server_type == "stdio":
        extra = set(entry) - _STDIO_ALLOWED
        if extra:
            errors.append(f"unknown field(s) for stdio server: {', '.join(sorted(extra))}")
        missing = _STDIO_REQUIRED - set(entry)
        if missing:
            errors.append(f"missing required field(s): {', '.join(sorted(missing))}")

        command = entry.get("command")
        if isinstance(command, str):
            cmd_form_errors = validate_command_token(command)
            if cmd_form_errors:
                # cwd 분기와 동일한 단락 -- 형식 위반 값을 경로 API 에 넘기지 않는다.
                errors.extend(f"command: {e}" for e in cmd_form_errors)
            else:
                errors.extend(
                    f"command: {e}"
                    for e in check_containment(command, plugin_root, plugin_data, "command")
                )
        elif "command" in entry:
            errors.append("command must be a string")

        args = entry.get("args")
        if args is not None and (not isinstance(args, list) or not all(isinstance(a, str) for a in args)):
            errors.append("args must be an array of strings")

        env = entry.get("env")
        if env is not None:
            if not isinstance(env, dict):
                errors.append("env must be an object")
            else:
                for k, v in env.items():
                    if k in _RESERVED_ENV_KEYS:
                        errors.append(f"env must not contain reserved key {k!r}")
                    # #248: 키는 값 타입과 무관하게 항상 검사한다 (오염 키 가림 금지).
                    errors.extend(env_text_errors("key", k, k))
                    if not isinstance(v, str):
                        errors.append(f"env.{k} must be a string")
                    else:
                        errors.extend(env_text_errors("value", k, v))
                        errors.extend(scan_secrets(v, source=f"env.{k}"))

        cwd = entry.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                errors.append("cwd must be a string")
            else:
                cwd_form_errors = validate_cwd_form(cwd)
                if cwd_form_errors:
                    errors.extend(f"cwd: {e}" for e in cwd_form_errors)
                else:
                    errors.extend(f"cwd: {e}" for e in check_containment(cwd, plugin_root, plugin_data, "cwd"))

    elif server_type in ("streamable-http", "sse"):
        extra = set(entry) - _REMOTE_ALLOWED
        if extra:
            errors.append(f"unknown field(s) for {server_type} server: {', '.join(sorted(extra))}")
        missing = _REMOTE_REQUIRED - set(entry)
        if missing:
            errors.append(f"missing required field(s): {', '.join(sorted(missing))}")

        url = entry.get("url")
        if isinstance(url, str):
            errors.extend(f"url: {e}" for e in validate_url(url))
        elif "url" in entry:
            errors.append("url must be a string")

        headers = entry.get("headers")
        if headers is not None:
            errors.extend(f"headers: {e}" for e in validate_headers(headers))
    else:
        errors.append(f"unknown or missing server type: {server_type!r}")

    return errors


def validate_mcp_config(obj, mode: str, plugin_root, plugin_data) -> tuple[list[str], list[str], dict[str, dict]]:
    """§7.2.2. 게이트: 어떤 위반도 오류. 로더: top-level 위반은 MCP 전체 비활성
    (errors 에 기록), 개별 entry 위반은 그 entry 만 제외하고 warning."""
    errors: list[str] = []
    warnings: list[str] = []
    valid_servers: dict[str, dict] = {}

    if mode == MODE_GATE:
        errors.extend(canonical_validate(obj, _MCP_SCHEMA_FILE))

    if not isinstance(obj, dict):
        errors.append("mcp.json must be a JSON object")
        return errors, warnings, valid_servers

    unknown_top = sorted(set(obj) - _MCP_ALLOWED_TOP)
    schema_val = obj.get("$schema")
    servers = obj.get("mcpServers")

    top_errors = []
    if unknown_top:
        top_errors.append(f"unknown top-level field(s): {', '.join(unknown_top)}")
    if schema_val != MCP_SCHEMA_ID:
        top_errors.append(f"$schema must be {MCP_SCHEMA_ID!r} (got {schema_val!r})")
    if not isinstance(servers, dict):
        top_errors.append("mcpServers must be an object")

    if top_errors:
        errors.extend(f"mcp.json: {e}" for e in top_errors)
        if mode == MODE_LOADER:
            return errors, warnings, valid_servers  # §7.2.2: 전체 비활성, 서버 순회 안 함

    if not isinstance(servers, dict):
        return errors, warnings, valid_servers

    for name, entry in servers.items():
        entry_errors = _validate_server_entry(entry, plugin_root, plugin_data)
        if entry_errors:
            if mode == MODE_GATE:
                errors.extend(f"mcp.json: server {name!r}: {e}" for e in entry_errors)
            else:
                warnings.extend(f"mcp.json: server {name!r} skipped: {e}" for e in entry_errors)
            continue
        valid_servers[name] = entry

    return errors, warnings, valid_servers


# ---------------------------------------------------------------------------
# §6 discovery / skills / 전체 패키지
# ---------------------------------------------------------------------------


def _parse_skill_frontmatter(text: str) -> dict[str, str] | None:
    """SKILL.md 의 얕은 YAML frontmatter 파서. name/description 검증에만 쓰므로
    중첩 매핑(metadata 등)은 건너뛴다 -- stdlib 만 쓰는 저작 도구라 PyYAML 의존 없음."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end].strip("\n")
    result: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line[:1] in (" ", "\t"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        result[key] = value
    return result


def _validate_skill_frontmatter(fm: dict[str, str] | None, dirname: str) -> list[str]:
    if fm is None:
        return ["SKILL.md is missing YAML frontmatter"]
    errors = []
    name = fm.get("name")
    if not isinstance(name, str) or not name:
        errors.append("frontmatter name is required")
    else:
        errors.extend(_validate_skill_name(name))
        if name != dirname:
            errors.append(f"name {name!r} must equal directory name {dirname!r}")
    description = fm.get("description")
    if not isinstance(description, str) or not (1 <= len(description) <= 1024):
        errors.append("description is required and must be 1-1024 characters")
    return errors


def _discover_and_validate_skills(plugin_root: Path, mode: str) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    names: list[str] = []
    skills_dir = plugin_root / "skills"
    if not skills_dir.exists():
        return errors, warnings, names  # §6.2: 위치 부재는 오류가 아니다
    if skills_dir.is_symlink() or not skills_dir.is_dir():
        msg = "skills/ exists but is not a directory"
        (errors if mode == MODE_GATE else warnings).append(msg)
        return errors, warnings, names

    for child in sorted(skills_dir.iterdir()):
        if child.is_symlink():
            (errors if mode == MODE_GATE else warnings).append(f"skills/{child.name}: symlink rejected")
            continue
        if not child.is_dir():
            continue  # §7.1: 서브디렉터리만 스킬 후보
        skill_md = child / "SKILL.md"
        if skill_md.is_symlink() or not skill_md.is_file():
            continue  # SKILL.md 없는 서브디렉터리는 스킬이 아니다

        containment_errors = check_containment(str(skill_md), str(plugin_root), None, "file")
        if containment_errors:
            (errors if mode == MODE_GATE else warnings).append(
                f"skills/{child.name}/SKILL.md: {'; '.join(containment_errors)}"
            )
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            (errors if mode == MODE_GATE else warnings).append(f"skills/{child.name}/SKILL.md: unreadable ({exc})")
            continue

        fm = _parse_skill_frontmatter(text)
        skill_errors = _validate_skill_frontmatter(fm, child.name)
        if skill_errors:
            msg = f"skills/{child.name}: {'; '.join(skill_errors)}"
            if mode == MODE_GATE:
                errors.append(msg)
            else:
                warnings.append(f"{msg} (skipped)")
            continue
        names.append(child.name)

    return errors, warnings, names


def _scan_tree(plugin_root: Path, mode: str) -> tuple[list[str], list[str]]:
    """패키지 내 전 파일 realpath containment + symlink 거부 + 텍스트 파일 시크릿 스캔.

    §4.1 이 이름 지정한 4개 경계(plugin.json/컴포넌트 고정 위치/SKILL.md/MCP
    command·cwd) 밖의 잔여 파일에 대한 방어 계층이다. 이 4개는 이미 각자의
    검증 단계에서 개별 확인되므로 여기서 다시 걸리는 것은 대개 그 경계 밖의
    잉여 파일뿐이다 -- 보안 성격상 게이트·로더 구분 없이 항상 errors 로 취급한다.
    """
    errors: list[str] = []
    warnings: list[str] = []
    real_root = os.path.realpath(plugin_root)

    for dirpath, dirnames, filenames in os.walk(plugin_root, followlinks=False):
        pruned = []
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                errors.append(f"{os.path.relpath(full, plugin_root)}: symlink directory rejected")
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, plugin_root)
            if os.path.islink(full):
                errors.append(f"{rel}: symlink file rejected")
                continue
            real_full = os.path.realpath(full)
            if real_full != real_root and not (real_full + os.sep).startswith(real_root + os.sep):
                errors.append(f"{rel}: resolves outside plugin root")
                continue
            if os.path.splitext(fn)[1].lower() in _TEXT_EXTS:
                try:
                    text = Path(full).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for lit in _SECRET_ALLOWED_LITERALS:
                    text = text.replace(lit, "")
                errors.extend(scan_secrets(text, source=rel))

    return errors, warnings


def validate_package(
    plugin_root, mode: str, plugin_data=None, implemented_namespaces: frozenset = frozenset()
) -> ValidationReport:
    """§6 발견 + §4.1 경계 격리 전체를 조율하는 최상위 진입점."""
    plugin_root = Path(plugin_root)
    report = ValidationReport()

    manifest_path = plugin_root / "plugin.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        report.errors.append("plugin.json is required at the plugin root")
        return report
    manifest_containment = check_containment(str(manifest_path), str(plugin_root), plugin_data, "file")
    if manifest_containment:
        report.errors.extend(f"plugin.json: {e}" for e in manifest_containment)
        return report
    try:
        manifest_obj = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f"plugin.json: invalid JSON ({exc})")
        return report

    perrors, pwarnings = validate_plugin_manifest(manifest_obj, mode, implemented_namespaces)
    report.errors.extend(perrors)
    report.warnings.extend(pwarnings)
    if mode == MODE_LOADER and perrors:
        return report  # §5.2/§5.3: manifest 위반은 플러그인 전체 거부

    mcp_path = plugin_root / "mcp.json"
    if mcp_path.exists():
        if mcp_path.is_symlink() or not mcp_path.is_file():
            (report.errors if mode == MODE_GATE else report.warnings).append(
                "mcp.json exists but is not a regular file"
            )
        else:
            mcp_containment = check_containment(str(mcp_path), str(plugin_root), plugin_data, "file")
            if mcp_containment:
                (report.errors if mode == MODE_GATE else report.warnings).append(
                    f"mcp.json: {'; '.join(mcp_containment)}"
                )
            else:
                mcp_obj = None
                try:
                    mcp_obj = json.loads(mcp_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    (report.errors if mode == MODE_GATE else report.warnings).append(
                        f"mcp.json: invalid JSON ({exc})"
                    )
                if mcp_obj is not None:
                    merrors, mwarnings, mservers = validate_mcp_config(
                        mcp_obj, mode, str(plugin_root), str(plugin_data) if plugin_data else None
                    )
                    plugin_schema_version = _schema_version(
                        manifest_obj.get("$schema") if isinstance(manifest_obj, dict) else None
                    )
                    mcp_schema_version = _schema_version(
                        mcp_obj.get("$schema") if isinstance(mcp_obj, dict) else None
                    )
                    if plugin_schema_version != mcp_schema_version:
                        vmsg = (
                            f"mcp.json $schema version ({mcp_schema_version!r}) does not match "
                            f"plugin.json ({plugin_schema_version!r}) -- \u00a710.1"
                        )
                        if mode == MODE_GATE:
                            merrors.append(vmsg)
                        else:
                            mwarnings.append(vmsg + " -- mcp.json disabled")
                            mservers = {}

                    if mode == MODE_GATE:
                        report.errors.extend(merrors)
                    else:
                        report.warnings.extend(mwarnings)
                        report.warnings.extend(f"mcp.json disabled: {e}" for e in merrors)
                    report.servers.update(mservers)

    serrors, swarnings, snames = _discover_and_validate_skills(plugin_root, mode)
    report.errors.extend(serrors)
    report.warnings.extend(swarnings)
    report.skills.extend(snames)

    tree_errors, tree_warnings = _scan_tree(plugin_root, mode)
    report.errors.extend(tree_errors)
    report.warnings.extend(tree_warnings)

    return report
