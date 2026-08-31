"""Agent Plugins 1.0.0 저작 도구(tools/) 단위·통합 테스트 (이슈 #137).

설계 정본 체인: design-v5.md > v4 > v3 > v2 (각 델타가 이전 문서를 개정).
케이스 커버리지는 v2 §8(기본 전략) + v3 §8([V3] extensions 분기, [V7] 규칙 함수 직접 단위 테스트)
+ v4 [W1](env 전수 동기화 가드) + v5 [X1](AST 기반 추출·양방향 동치)을 따른다.

TDD RED 단계: packaging/agent-plugin/tools/{validate,build,env_contract}.py 는 아직 구현되지
않았다. 이 파일을 수집(collect)하면 ImportError 로 실패하는 것이 이 시점의 올바른 결과다 —
구현이 들어오면 이 스위트가 green 이 되는 것으로 완료를 판정한다.

레포 관례를 따른다: 클래스 단위 그룹핑(tests/test_mcp.py), 한국어 주석/독스트링, 실제 자원
무접촉(포트 8765/8766·systemd·~/.openclaw 미사용 — 이 파일의 모든 검증은 tmp_path 와 실제
packaging/agent-plugin/src(읽기 전용)만 다룬다).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging" / "agent-plugin"))

from tools import build as b  # noqa: E402
from tools import env_contract  # noqa: E402
from tools import refclient as rc  # noqa: E402
from tools import validate as v  # noqa: E402

SRC_DIR = REPO / "packaging" / "agent-plugin" / "src"
SCHEMAS_DIR = REPO / "packaging" / "agent-plugin" / "schemas"


# ---------------------------------------------------------------------------
# 공용 헬퍼 · 픽스처
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _valid_manifest(**overrides) -> dict:
    """§3 정본 plugin.json 과 동형인 최소 유효 manifest."""
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
    """§4 정본 mcp.json 과 동형인 최소 유효 설정."""
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


def _fake_repo(tmp_path: Path, *, version: str = "0.1.0", extra_src_files: dict | None = None) -> Path:
    """build() 를 빠르게·격리해서 테스트하기 위한 합성 미니 레포.

    실제 레포 전체를 복사하지 않는다 -- 벡터DB 등 큰 실 자산을 건드릴 위험도 없고 느리다.
    src 게이트·스테이징 게이트·사이드카 각각을 독립적으로 주입/오염시킬 수 있는 최소 구조만 만든다.
    """
    repo = tmp_path / "fake-repo"
    src = repo / "packaging" / "agent-plugin" / "src"
    (src / "skills" / "localcrab-query").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "opencrab"\nversion = "{version}"\n', encoding="utf-8"
    )
    (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    _write_json(src / "plugin.json", _valid_manifest())
    _write_json(src / "mcp.json", _valid_mcp_obj())
    (src / "README.md").write_text("# Fake plugin\n\nNo secrets here.\n", encoding="utf-8")
    (src / "skills" / "localcrab-query" / "SKILL.md").write_text(
        "---\nname: localcrab-query\ndescription: test skill description text, long enough.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    if extra_src_files:
        for rel, content in extra_src_files.items():
            path = src / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
    return repo


@pytest.fixture
def plugin_root(tmp_path) -> Path:
    root = tmp_path / "plugin-root"
    root.mkdir()
    return root


@pytest.fixture
def plugin_data(tmp_path) -> Path:
    data = tmp_path / "plugin-data"
    data.mkdir()
    return data


# ---------------------------------------------------------------------------
# 텍스트층 규칙 함수 직접 단위 테스트 [V7]
# ---------------------------------------------------------------------------


class TestValidateName:
    """plugin.schema.json name 패턴의 텍스트층 재검증 (§5.5)."""

    @pytest.mark.parametrize("name", ["localcrab", "a", "a1", "a-b.c", "x" * 64])
    def test_valid_names_pass(self, name):
        assert v.validate_name(name) == []

    @pytest.mark.parametrize(
        "name",
        [
            "Localcrab",  # 대문자
            "-localcrab",  # 선행 하이픈
            "localcrab-",  # 후행 하이픈
            "local--crab",  # 연속 하이픈
            "local..crab",  # ".." 포함
            "",  # minLength 위반
            "x" * 65,  # maxLength 초과
        ],
    )
    def test_invalid_names_fail(self, name):
        assert v.validate_name(name) != []


class TestValidateCommandToken:
    """stdio command 단일 토큰 규칙 (§7.2.1): bare 또는 ./ 시작만 허용."""

    @pytest.mark.parametrize("token", ["opencrab", "./bin/opencrab", "./a"])
    def test_valid_tokens_pass(self, token):
        assert v.validate_command_token(token) == []

    @pytest.mark.parametrize(
        "token",
        [
            "/usr/bin/opencrab",  # 절대경로
            "../opencrab",  # 상위 이탈 표기
            "opencrab serve",  # 공백(다중 토큰)
            "${PLUGIN_ROOT}/opencrab",  # command 는 비확장 대상 -- placeholder 자체가 무효
            "./foo bar",  # ./ 분기도 단일 토큰이어야 한다 (PR #244 P2-1)
            "./${PLUGIN_ROOT}",  # ./ 분기도 placeholder 금지 (PR #244 P2-1)
            "./",  # 프리픽스뿐인 command (PR #244 P2-1)
            ".",  # 해석 불가능한 bare 토큰
            "..",  # 해석 불가능한 bare 토큰
            "./a\x00b",  # NUL 바이트 — 경로 API 도달 전 거부
            "",
        ],
    )
    def test_invalid_tokens_fail(self, token):
        assert v.validate_command_token(token) != []


class TestValidateCwdForm:
    """cwd 는 3형식만 허용한다 (§7.2.1): ./..., ${PLUGIN_ROOT}(/...)?, ${PLUGIN_DATA}(/...)?."""

    @pytest.mark.parametrize(
        "cwd",
        [
            "./",
            "./sub/dir",
            "${PLUGIN_ROOT}",
            "${PLUGIN_ROOT}/sub",
            "${PLUGIN_DATA}",
            "${PLUGIN_DATA}/sub",
        ],
    )
    def test_valid_forms_pass(self, cwd):
        assert v.validate_cwd_form(cwd) == []

    @pytest.mark.parametrize(
        "cwd",
        ["data", "${HOME}/x", "/abs/path", "../escape", "PLUGIN_DATA", "${PLUGIN_DATAX}"],
    )
    def test_invalid_forms_fail(self, cwd):
        assert v.validate_cwd_form(cwd) != []

    def test_nul_byte_cwd_fails(self):
        # NUL 은 JSON 문자열로 유입 가능하고 경로 API 에서 ValueError 를 유발한다 —
        # 형식 검사 단계에서 거부한다 (PR #244 P2-2 와 같은 예외 누출 클래스).
        assert v.validate_cwd_form("${PLUGIN_DATA}/a\x00b") != []


class TestCheckContainment:
    """§4.1/§7.2.1 컨테인먼트: 기준 루트별 분리[R5] + lexical(posixpath.normpath)·realpath 이중 확인.

    실제 시그니처: check_containment(value, plugin_root, plugin_data, kind) -- value 가 먼저이고
    plugin_root/plugin_data 는 둘 다 항상 넘긴다(내부가 kind 와 value 안의 placeholder 를 보고
    어느 루트를 기준으로 볼지 스스로 고른다). kind="file" 의 value 는 절대경로(스테이징된 실
    파일의 실제 경로)도 받는다 -- symlink 이탈 검사에 쓴다(빌더의 lstat 거부와는 별개 방어선).
    """

    def test_cwd_dot_slash_within_root_passes(self, plugin_root, plugin_data):
        assert v.check_containment("./sub/dir", str(plugin_root), str(plugin_data), kind="cwd") == []

    def test_cwd_plugin_root_escape_fails(self, plugin_root, plugin_data):
        errors = v.check_containment(
            "${PLUGIN_ROOT}/../escape", str(plugin_root), str(plugin_data), kind="cwd"
        )
        assert errors != []

    def test_cwd_plugin_data_escape_fails(self, plugin_root, plugin_data):
        errors = v.check_containment(
            "${PLUGIN_DATA}/../escape", str(plugin_root), str(plugin_data), kind="cwd"
        )
        assert errors != []

    def test_cwd_plugin_data_within_root_passes(self, plugin_root, plugin_data):
        assert v.check_containment("${PLUGIN_DATA}/sub", str(plugin_root), str(plugin_data), kind="cwd") == []

    def test_command_dot_slash_escape_fails(self, plugin_root, plugin_data):
        errors = v.check_containment("./a/../../escape", str(plugin_root), str(plugin_data), kind="command")
        assert errors != []

    def test_command_dot_slash_within_root_passes(self, plugin_root, plugin_data):
        assert v.check_containment("./bin/opencrab", str(plugin_root), str(plugin_data), kind="command") == []

    def test_file_symlink_escape_fails(self, plugin_root, plugin_data, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        link = plugin_root / "escape.txt"
        link.symlink_to(outside)
        errors = v.check_containment(str(link), str(plugin_root), str(plugin_data), kind="file")
        assert errors != []

    def test_file_regular_within_root_passes(self, plugin_root, plugin_data):
        target = plugin_root / "real.txt"
        target.write_text("x", encoding="utf-8")
        assert v.check_containment(str(target), str(plugin_root), str(plugin_data), kind="file") == []


class TestValidateUrl:
    """§7.2.1 URL 규칙: loopback 판정 + non-loopback HTTPS 강제 + userinfo/fragment 금지 [R10]."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/mcp",
            "http://127.0.0.1:8080/mcp",
            "http://127.0.0.5/mcp",
            "http://[::1]:9000/mcp",
            "https://example.com/mcp",
        ],
    )
    def test_valid_urls_pass(self, url):
        assert v.validate_url(url) == []

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/mcp",  # non-loopback + http
            "http://localhost.example/mcp",  # suffix 는 non-loopback [R10]
            "https://user:pass@example.com/mcp",  # userinfo 금지
            "https://example.com/mcp#frag",  # fragment 금지
            "ftp://example.com/mcp",  # 허용되지 않는 스킴
            "http://[::1",  # 비파싱 URL — 예외가 아니라 오류여야 한다 (PR #244 P2-2)
        ],
    )
    def test_invalid_urls_fail(self, url):
        assert v.validate_url(url) != []


class TestValidateHeaders:
    """headers: 대소문자 무시 중복 금지, CR/LF 값 거부, RFC 9110 field-name 문자 검사 [R10]."""

    def test_valid_headers_pass(self):
        assert v.validate_headers({"X-Api-Key": "abc", "Accept": "application/json"}) == []

    def test_case_insensitive_duplicate_fails(self):
        assert v.validate_headers({"X-Api-Key": "a", "x-api-key": "b"}) != []

    def test_crlf_in_value_fails(self):
        assert v.validate_headers({"X-Api-Key": "a\r\nEvil: 1"}) != []

    def test_invalid_field_name_fails(self):
        assert v.validate_headers({"Bad Header Name": "a"}) != []


class TestExpandPlaceholders:
    """placeholder 확장: 다중 발생 전부 치환, 비재귀, 미인식 보존 [R10]."""

    def test_multiple_occurrences_all_replaced(self):
        text = "${PLUGIN_ROOT}/a:${PLUGIN_ROOT}/b"
        assert v.expand_placeholders(text, "/root", "/data") == "/root/a:/root/b"

    def test_non_recursive_expansion(self):
        # PLUGIN_ROOT 의 치환 결과 문자열 자체가 "${PLUGIN_DATA}" 를 담고 있어도 재확장하지 않는다.
        result = v.expand_placeholders("${PLUGIN_ROOT}", "${PLUGIN_DATA}", "/data")
        assert result == "${PLUGIN_DATA}"

    def test_unrecognized_placeholder_preserved(self):
        assert v.expand_placeholders("${FOO}/x", "/root", "/data") == "${FOO}/x"

    def test_plugin_data_expansion(self):
        assert v.expand_placeholders("${PLUGIN_DATA}/db", "/root", "/data") == "/data/db"

    def test_text_without_placeholder_unchanged(self):
        assert v.expand_placeholders("plain text", "/root", "/data") == "plain text"


class TestScanSecrets:
    """2차 방어선: 시크릿/개인 경로/운영 파일명 패턴 스캔 [R6]."""

    @pytest.mark.parametrize(
        "text",
        [
            "token: lc_abcdefgh12345",
            "key=sk-abcdefghijklmnopqrst",  # sk- 는 20자 이상 요구(실측 확인)
            "AKIAABCDEFGHIJKLMNOP",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "-----BEGIN RSA PRIVATE KEY-----",
            "Authorization: Bearer xyz",
            "https://x.example/mcp?token=abc123",
            "/home/asdf/secret.txt",
            "/Users/asdf/secret.txt",
            r"C:\Users\asdf\secret.txt",
            "${HOME}/secret",
            "~/secret",
            "see localcrab-kure.env",
            "see localcrab-mcp.token",
        ],
    )
    def test_secret_patterns_detected(self, text):
        assert v.scan_secrets(text, source="test") != []

    def test_clean_text_passes(self):
        assert v.scan_secrets("This is a clean README with no secrets.", source="test") == []


# ---------------------------------------------------------------------------
# 스키마 통합(canonical_validate) 및 이중 모드 검증기
# ---------------------------------------------------------------------------


class TestCanonicalValidate:
    """canonical_validate 는 jsonschema 기반 스키마 준수 검사(텍스트층보다 앞선 1차 방어선).

    실제 시그니처: canonical_validate(obj, schema_file) -- schema_file 은 파일 경로이고 함수가
    직접 열어 읽는다(사전 로드한 dict 를 받지 않는다).
    """

    def test_valid_plugin_manifest_passes(self):
        assert v.canonical_validate(_valid_manifest(), SCHEMAS_DIR / "plugin.schema.json") == []

    def test_unknown_field_fails_schema(self):
        manifest = _valid_manifest()
        manifest["totally_unknown_field"] = 1
        assert v.canonical_validate(manifest, SCHEMAS_DIR / "plugin.schema.json") != []

    def test_valid_mcp_config_passes(self):
        assert v.canonical_validate(_valid_mcp_obj(), SCHEMAS_DIR / "mcp.schema.json") == []

    def test_unknown_server_type_fails_schema(self):
        obj = _valid_mcp_obj()
        obj["mcpServers"]["localcrab"]["type"] = "websocket"
        assert v.canonical_validate(obj, SCHEMAS_DIR / "mcp.schema.json") != []


class TestValidatePluginManifestModes:
    """이중 모드 분기 [R9][V3]: 게이트는 전부 오류로 승격, 로더는 §5.2/§8.1 관용 그대로 유지."""

    def test_unknown_top_level_field_gate_fails(self):
        manifest = _valid_manifest(mystery_field=1)
        errors, _warnings = v.validate_plugin_manifest(manifest, v.MODE_GATE)
        assert errors != []

    def test_unknown_top_level_field_loader_warns_and_continues(self):
        manifest = _valid_manifest(mystery_field=1)
        errors, warnings = v.validate_plugin_manifest(manifest, v.MODE_LOADER)
        assert errors == []
        assert warnings != []

    def test_non_object_extensions_gate_fails(self):
        manifest = _valid_manifest(extensions="not-an-object")
        errors, _warnings = v.validate_plugin_manifest(manifest, v.MODE_GATE)
        assert errors != []

    def test_non_object_extensions_loader_warns_and_continues(self):
        manifest = _valid_manifest(extensions="not-an-object")
        errors, warnings = v.validate_plugin_manifest(manifest, v.MODE_LOADER)
        assert errors == []
        assert warnings != []

    def test_unimplemented_namespace_member_ignored_without_validation(self):
        # §8.1: 구현하지 않은 네임스페이스 멤버는 값의 타입 포함 일절 검증 없이 무시한다.
        # {"extensions": {"com.unknown": 7}} 은 로더에서 거부 사유가 아니다(값이 object 가 아니어도).
        manifest = _valid_manifest(extensions={"com.unknown": 7})
        errors, _warnings = v.validate_plugin_manifest(
            manifest, v.MODE_LOADER, implemented_namespaces=frozenset()
        )
        assert errors == []

    def test_required_field_violation_rejects_in_both_modes(self):
        manifest = _valid_manifest()
        del manifest["name"]
        gate_errors, _ = v.validate_plugin_manifest(manifest, v.MODE_GATE)
        loader_errors, _ = v.validate_plugin_manifest(manifest, v.MODE_LOADER)
        assert gate_errors != []
        assert loader_errors != []

    def test_valid_manifest_passes_both_modes(self):
        manifest = _valid_manifest()
        gate_errors, _ = v.validate_plugin_manifest(manifest, v.MODE_GATE)
        loader_errors, _ = v.validate_plugin_manifest(manifest, v.MODE_LOADER)
        assert gate_errors == []
        assert loader_errors == []

    @pytest.mark.parametrize(
        "name",
        ["Localcrab", "-bad", "bad-", "bad--name", "bad..name", "x" * 65],
    )
    def test_invalid_name_rejects_in_both_modes(self, name):
        manifest = _valid_manifest(name=name)
        gate_errors, _ = v.validate_plugin_manifest(manifest, v.MODE_GATE)
        loader_errors, _ = v.validate_plugin_manifest(manifest, v.MODE_LOADER)
        assert gate_errors != []
        assert loader_errors != []

    def test_schema_version_mismatch_rejects(self):
        manifest = _valid_manifest()
        manifest["$schema"] = "https://agent-plugins.org/schemas/9.9.9/plugin.schema.json"
        gate_errors, _ = v.validate_plugin_manifest(manifest, v.MODE_GATE)
        assert gate_errors != []


class TestValidateMcpConfig:
    """mcp.json 이중 모드: top-level 위반은 MCP 전체 비활성, entry 단위 위반은 그 entry 만 skip (§7.2.2).

    실제 시그니처: validate_mcp_config(obj, mode, plugin_root, plugin_data) ->
    (errors, warnings, valid_servers) -- 검증 통과한 entry 만 담긴 dict 가 세 번째로 온다.
    """

    def test_empty_mcp_servers_valid(self, plugin_root, plugin_data):
        # §6.2: mcpServers 빈 객체도 유효하다.
        obj = _valid_mcp_obj(mcpServers={})
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_LOADER, str(plugin_root), str(plugin_data)
        )
        assert errors == []

    def test_valid_stdio_entry_passes(self, plugin_root, plugin_data):
        errors, _warnings, _servers = v.validate_mcp_config(
            _valid_mcp_obj(), v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors == []

    def test_top_level_schema_violation_rejects_whole_config(self, plugin_root, plugin_data):
        obj = _valid_mcp_obj()
        del obj["$schema"]
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    def test_invalid_entry_skipped_others_kept_loader_mode(self, plugin_root, plugin_data):
        obj = _valid_mcp_obj()
        obj["mcpServers"]["broken"] = {"type": "stdio", "command": "/abs/not/allowed"}
        errors, warnings, servers = v.validate_mcp_config(
            obj, v.MODE_LOADER, str(plugin_root), str(plugin_data)
        )
        assert errors == []
        assert warnings != []
        assert "localcrab" in servers
        assert "broken" not in servers

    @pytest.mark.parametrize(
        "command",
        ["/usr/bin/opencrab", "../opencrab", "opencrab serve", "${PLUGIN_ROOT}/opencrab"],
    )
    def test_invalid_command_gate_fails(self, plugin_root, plugin_data, command):
        obj = _valid_mcp_obj()
        obj["mcpServers"]["localcrab"]["command"] = command
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    def test_env_reserved_key_plugin_root_rejected(self, plugin_root, plugin_data):
        obj = _valid_mcp_obj()
        obj["mcpServers"]["localcrab"]["env"]["PLUGIN_ROOT"] = "x"
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    def test_env_reserved_key_plugin_data_rejected(self, plugin_root, plugin_data):
        obj = _valid_mcp_obj()
        obj["mcpServers"]["localcrab"]["env"]["PLUGIN_DATA"] = "x"
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    @pytest.mark.parametrize("cwd", ["data", "${HOME}/x", "/abs/path"])
    def test_invalid_cwd_form_gate_fails(self, plugin_root, plugin_data, cwd):
        obj = _valid_mcp_obj()
        obj["mcpServers"]["localcrab"]["cwd"] = cwd
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    @pytest.mark.parametrize("cwd", ["${PLUGIN_DATA}/../escape", "${PLUGIN_ROOT}/../escape"])
    def test_cwd_escape_after_resolution_gate_fails(self, plugin_root, plugin_data, cwd):
        obj = _valid_mcp_obj()
        obj["mcpServers"]["localcrab"]["cwd"] = cwd
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    def test_non_loopback_http_url_rejected(self, plugin_root, plugin_data):
        obj = _valid_mcp_obj(
            mcpServers={"remote": {"type": "streamable-http", "url": "http://example.com/mcp"}}
        )
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    def test_loopback_http_url_accepted(self, plugin_root, plugin_data):
        obj = _valid_mcp_obj(
            mcpServers={"local-http": {"type": "streamable-http", "url": "http://127.0.0.1:9000/mcp"}}
        )
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors == []

    def test_headers_case_insensitive_duplicate_rejected(self, plugin_root, plugin_data):
        obj = _valid_mcp_obj(
            mcpServers={
                "remote": {
                    "type": "sse",
                    "url": "https://example.com/mcp",
                    "headers": {"X-Api-Key": "a", "x-api-key": "b"},
                }
            }
        )
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, str(plugin_root), str(plugin_data)
        )
        assert errors != []

    def test_unparseable_url_gate_error_loader_skip(self, plugin_root, plugin_data):
        # PR #244 P2-2 통합: urlsplit ValueError 가 예외로 새지 않고 게이트=오류,
        # 로더=해당 entry 만 skip 이어야 한다 (§7.2.2).
        obj = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "bad": {"type": "streamable-http", "url": "http://[::1"},
                "ok": {"type": "stdio", "command": "opencrab"},
            },
        }
        errors, _warnings, servers = v.validate_mcp_config(
            obj, v.MODE_GATE, plugin_root, plugin_data
        )
        assert errors != []
        _errors, warnings, servers = v.validate_mcp_config(
            obj, v.MODE_LOADER, plugin_root, plugin_data
        )
        assert "bad" not in servers and "ok" in servers
        assert warnings != []


# ---------------------------------------------------------------------------
# 패키지 통합 검증 (실 src 대상 + 합성 오염 케이스)
# ---------------------------------------------------------------------------


class TestPathResolutionExceptionLeaks:
    """JSON 이스케이프로 유입 가능한 NUL·고립 서로게이트가 경로 해석에서 예외로
    새지 않아야 한다 (PR #244 P2-3/P2-4). 게이트=오류, 로더=entry skip (§7.2.2).
    디스크 mcp.json 왕복으로 유입 형태를 재현한다."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("command", "./a\u0000b"),  # NUL — 형식 오류 후에도 containment 로 새면 안 된다
            ("command", "./\ud800x"),  # 고립 서로게이트 — 형식 검사를 통과하는 입력
            ("cwd", "./\ud800x"),
            ("cwd", "${PLUGIN_DATA}/\ud800"),
        ],
    )
    def test_no_exception_gate_error_loader_skip(
        self, tmp_path, plugin_root, plugin_data, field, value
    ):
        entry = {"type": "stdio", "command": "opencrab"}
        entry[field] = value
        obj = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {"bad": entry, "ok": {"type": "stdio", "command": "opencrab"}},
        }
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps(obj), encoding="utf-8")  # ensure_ascii 라 ASCII 로 기록됨
        loaded = json.loads(path.read_text(encoding="utf-8"))
        errors, _warnings, _servers = v.validate_mcp_config(
            loaded, v.MODE_GATE, plugin_root, plugin_data
        )
        assert errors != []
        _errors, warnings, servers = v.validate_mcp_config(
            loaded, v.MODE_LOADER, plugin_root, plugin_data
        )
        assert "bad" not in servers and "ok" in servers
        assert warnings != []


class TestValidatePackage:
    """validate_package 통합. 정상 케이스는 합성 픽스처가 아니라 실제
    packaging/agent-plugin/src 를 대상으로 한다(§8 "정상: src authoring PASS").

    실제 시그니처: validate_package(plugin_root, mode, plugin_data=None, implemented_namespaces=...)
    -> ValidationReport(errors, warnings, servers, skills) -- 튜플이 아니라 dataclass 다.
    실 src 의 mcp.json cwd 가 "${PLUGIN_DATA}" 이므로 plugin_data 는 실경로를 넘겨야 한다
    (None 이면 containment 검사가 "plugin_data root is required" 오류를 낸다).

    범위 정정(실 구현 확인 결과): 잉여 파일 거부·필수 파일(README.md) 부재 거부·env 값이
    기대 상수와 다를 때의 거부는 이 함수가 아니라 build.py 의 3단계 allowlist 게이트(src
    게이트) 몫이다 -- validate_package 는 발견된 plugin.json/mcp.json/skills 만 검증하고
    디렉터리 전체의 파일 목록을 allowlist 와 대조하지 않는다. 해당 케이스는 TestBuild 로 옮겼다.
    """

    def test_real_src_passes_authoring_gate(self, plugin_data):
        report = v.validate_package(SRC_DIR, v.MODE_GATE, plugin_data=str(plugin_data))
        assert report.errors == [], f"authoring 게이트 위반: {report.errors}"

    def test_skill_name_mismatch_dirname_fails(self, tmp_path, plugin_data):
        copy_dir = tmp_path / "src-copy"
        shutil.copytree(SRC_DIR, copy_dir)
        skill_path = copy_dir / "skills" / "localcrab-query" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        skill_path.write_text(
            text.replace("name: localcrab-query", "name: something-else"), encoding="utf-8"
        )
        report = v.validate_package(copy_dir, v.MODE_GATE, plugin_data=str(plugin_data))
        assert report.errors != []

    def test_skills_dir_absent_is_valid(self, tmp_path, plugin_data):
        # §6.2: skills/ 자체가 없는 패키지도 유효하다.
        minimal = tmp_path / "minimal-src"
        minimal.mkdir()
        _write_json(minimal / "plugin.json", _valid_manifest())
        _write_json(minimal / "mcp.json", _valid_mcp_obj())
        (minimal / "README.md").write_text("# minimal\n", encoding="utf-8")
        report = v.validate_package(minimal, v.MODE_GATE, plugin_data=str(plugin_data))
        assert report.errors == []


class TestSecretScanIntegration:
    """2차 패턴 스캔이 README/SKILL.md 등 전 텍스트 파일에 실제 적용되는지 통합 확인 [R6]."""

    def test_secret_in_readme_fails_gate(self, tmp_path, plugin_data):
        copy_dir = tmp_path / "src-copy"
        shutil.copytree(SRC_DIR, copy_dir)
        readme = copy_dir / "README.md"
        # sk- 패턴은 20자 이상 요구(_SECRET_PATTERNS: r"sk-[A-Za-z0-9]{20,}") -- 실측 확인.
        readme.write_text(
            readme.read_text(encoding="utf-8") + f"\ntoken=sk-{'a' * 25}\n", encoding="utf-8"
        )
        report = v.validate_package(copy_dir, v.MODE_GATE, plugin_data=str(plugin_data))
        assert report.errors != []

    def test_personal_path_in_skill_fails_gate(self, tmp_path, plugin_data):
        copy_dir = tmp_path / "src-copy"
        shutil.copytree(SRC_DIR, copy_dir)
        skill = copy_dir / "skills" / "localcrab-query" / "SKILL.md"
        skill.write_text(
            skill.read_text(encoding="utf-8") + "\nSee /home/alice/notes.md\n", encoding="utf-8"
        )
        report = v.validate_package(copy_dir, v.MODE_GATE, plugin_data=str(plugin_data))
        assert report.errors != []


# ---------------------------------------------------------------------------
# 빌더: 3단계 allowlist [V6]
# ---------------------------------------------------------------------------


class TestBuild:
    """3단계 빌더: src 게이트 → 스테이징 게이트 → 사이드카 생성 (dist/ 밖)."""

    def test_build_produces_expected_layout(self, tmp_path):
        repo = _fake_repo(tmp_path)
        out_dir = tmp_path / "dist"
        plugin_root_out = b.build(repo, out_dir)
        assert plugin_root_out == out_dir / "localcrab-plugin"
        assert (plugin_root_out / "plugin.json").is_file()
        assert (plugin_root_out / "mcp.json").is_file()
        assert (plugin_root_out / "README.md").is_file()
        assert (plugin_root_out / "LICENSE").is_file()
        assert (plugin_root_out / "skills" / "localcrab-query" / "SKILL.md").is_file()

    def test_sha256sums_sidecar_outside_plugin_root(self, tmp_path):
        repo = _fake_repo(tmp_path)
        out_dir = tmp_path / "dist"
        plugin_root_out = b.build(repo, out_dir)
        sidecar = out_dir / "localcrab-plugin.SHA256SUMS"
        assert sidecar.is_file()
        assert not (plugin_root_out / "SHA256SUMS").exists()
        assert not (plugin_root_out / sidecar.name).exists()

    def test_sha256sums_lists_sorted_relative_paths(self, tmp_path):
        repo = _fake_repo(tmp_path)
        out_dir = tmp_path / "dist"
        b.build(repo, out_dir)
        sidecar = out_dir / "localcrab-plugin.SHA256SUMS"
        lines = [ln for ln in sidecar.read_text(encoding="utf-8").splitlines() if ln.strip()]
        paths = [ln.split(maxsplit=1)[-1] for ln in lines]
        assert paths == sorted(paths)
        assert "plugin.json" in paths
        assert "LICENSE" in paths

    def test_rebuild_is_idempotent(self, tmp_path):
        repo = _fake_repo(tmp_path)
        out_dir1 = tmp_path / "dist1"
        out_dir2 = tmp_path / "dist2"
        b.build(repo, out_dir1)
        b.build(repo, out_dir2)
        sidecar1 = (out_dir1 / "localcrab-plugin.SHA256SUMS").read_text(encoding="utf-8")
        sidecar2 = (out_dir2 / "localcrab-plugin.SHA256SUMS").read_text(encoding="utf-8")
        assert sidecar1 == sidecar2

    def test_version_mismatch_rejected(self, tmp_path):
        repo = _fake_repo(tmp_path, version="9.9.9")
        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build(repo, out_dir)
        assert not (out_dir / "localcrab-plugin").exists()

    def test_extra_file_in_src_rejected(self, tmp_path):
        repo = _fake_repo(tmp_path, extra_src_files={"stray.txt": "nope"})
        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build(repo, out_dir)

    def test_missing_readme_in_src_rejected(self, tmp_path):
        # §8 "정상: src authoring PASS" 의 대응 부정 케이스 -- 필수 src 파일(README.md) 부재.
        # 이 allowlist 대조는 validate_package 가 아니라 build() 1단계(src 게이트)의 몫이다.
        repo = _fake_repo(tmp_path)
        src = repo / "packaging" / "agent-plugin" / "src"
        (src / "README.md").unlink()
        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build(repo, out_dir)

    def test_missing_license_in_repo_root_rejected(self, tmp_path):
        repo = _fake_repo(tmp_path)
        (repo / "LICENSE").unlink()
        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build(repo, out_dir)

    def test_symlink_in_src_rejected(self, tmp_path):
        repo = _fake_repo(tmp_path)
        src = repo / "packaging" / "agent-plugin" / "src"
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        (src / "README.md").unlink()
        (src / "README.md").symlink_to(outside)
        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build(repo, out_dir)

    def test_secret_in_src_rejected(self, tmp_path):
        repo = _fake_repo(tmp_path)
        src = repo / "packaging" / "agent-plugin" / "src"
        # sk- 패턴은 20자 이상 요구(_SECRET_PATTERNS: r"sk-[A-Za-z0-9]{20,}") -- 실측 확인.
        (src / "README.md").write_text(f"token: sk-{'a' * 25}\n", encoding="utf-8")
        out_dir = tmp_path / "dist"
        with pytest.raises(b.BuildError):
            b.build(repo, out_dir)

    def test_src_allowlist_and_staged_allowlist_differ_by_license(self):
        assert "LICENSE" not in b.SRC_ALLOWLIST
        assert "LICENSE" in b.STAGED_ALLOWLIST
        assert b.STAGED_ALLOWLIST == b.SRC_ALLOWLIST | {"LICENSE"}


# ---------------------------------------------------------------------------
# 환경 변수 문서-코드 동기화 가드 [W1][X1] -- AST 기반, tests 내부 헬퍼(stdlib only)
# ---------------------------------------------------------------------------


def _is_environ_attr(node: ast.expr) -> bool:
    """node 가 `os.environ` 을 가리키는 Attribute 표현인지 판정한다."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _top_level_str_consts(tree: ast.Module) -> dict[str, list[str]]:
    """모듈 최상위 대입문에서 문자열 상수, 또는 문자열로만 이루어진 튜플/리스트 상수를 수집한다."""
    consts: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not names:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for name in names:
                consts[name] = [value.value]
        elif isinstance(value, (ast.Tuple, ast.List)):
            items = [
                el.value
                for el in value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            ]
            if items:
                for name in names:
                    consts[name] = items
    return consts


def _for_loop_var_sources(tree: ast.Module, top_consts: dict[str, list[str]]) -> dict[str, list[str]]:
    """`for x in NAME:` 문과 컴프리헨션의 `for x in NAME` 절 모두에서 순회 변수 x 를,
    NAME 이 최상위 문자열 상수 튜플/리스트일 때 그 원소 목록에 매핑한다.

    실측 케이스(opencrab/auth.py): `[name for name in _STALE_SECRET_ENV_VARS if ...]` 는
    ast.For 가 아니라 ast.comprehension 이므로 둘 다 다뤄야 한다(v5 [X1]이 지목한 정규식의
    누락 지점 중 하나).
    """
    sources: dict[str, list[str]] = {}

    def _record(target: ast.expr, iterable: ast.expr) -> None:
        if isinstance(target, ast.Name) and isinstance(iterable, ast.Name):
            if iterable.id in top_consts:
                sources[target.id] = top_consts[iterable.id]

    for node in ast.walk(tree):
        if isinstance(node, ast.For):
            _record(node.target, node.iter)
        elif isinstance(node, ast.comprehension):
            _record(node.target, node.iter)
    return sources


def _resolve_name_arg(
    arg: ast.expr, top_consts: dict[str, list[str]], loop_sources: dict[str, list[str]]
) -> list[str] | None:
    """인자 노드가 문자열 리터럴이면 그대로, Name 이면 최상위 상수/순회 소스로 해석한다.
    해석 불가능하면 None(호출자가 "해석 불가 위치"로 보고한다)."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.Name):
        if arg.id in loop_sources:
            return loop_sources[arg.id]
        if arg.id in top_consts:
            return top_consts[arg.id]
    return None


def collect_direct_env_access(root_dirs: list[Path]) -> tuple[set[str], list[str]]:
    """os.getenv(...)/os.environ[...]/os.environ.get(...)/os.environ.setdefault(...) 및
    click 데코레이터의 envvar=... 인자를 root_dirs 전체(.py 전량)에서 AST 로 수집한다.

    반환: (수집된 이름 집합, 해석 불가능한 동적 접근의 "상대경로:라인" 목록).
    """
    names: set[str] = set()
    unresolved: list[str] = []

    for root in root_dirs:
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            top_consts = _top_level_str_consts(tree)
            loop_sources = _for_loop_var_sources(tree, top_consts)
            rel = path.relative_to(REPO)

            for node in ast.walk(tree):
                candidates: list[ast.expr] = []

                if isinstance(node, ast.Call):
                    func = node.func
                    is_getenv = (
                        isinstance(func, ast.Attribute)
                        and func.attr == "getenv"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "os"
                    )
                    is_environ_get_or_setdefault = (
                        isinstance(func, ast.Attribute)
                        and func.attr in ("get", "setdefault")
                        and _is_environ_attr(func.value)
                    )
                    if (is_getenv or is_environ_get_or_setdefault) and node.args:
                        candidates.append(node.args[0])
                    for kw in node.keywords:
                        if kw.arg == "envvar":
                            candidates.append(kw.value)

                if isinstance(node, ast.Subscript) and _is_environ_attr(node.value):
                    candidates.append(node.slice)

                for candidate in candidates:
                    resolved = _resolve_name_arg(candidate, top_consts, loop_sources)
                    if resolved is None:
                        unresolved.append(f"{rel}:{getattr(node, 'lineno', '?')}")
                    else:
                        names.update(resolved)

    return names, unresolved


def collect_config_aliases(config_path: Path) -> set[str]:
    """opencrab/config.py 의 Settings 필드에서 `Field(..., alias="X")` 리터럴을 AST 로 수집한다."""
    aliases: set[str] = set()
    tree = ast.parse(config_path.read_text(encoding="utf-8"), filename=str(config_path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_field_call = (isinstance(func, ast.Name) and func.id == "Field") or (
            isinstance(func, ast.Attribute) and func.attr == "Field"
        )
        if not is_field_call:
            continue
        for kw in node.keywords:
            if (
                kw.arg == "alias"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                aliases.add(kw.value.value)
    return aliases


def scan_env_contract() -> tuple[set[str], list[str]]:
    """opencrab/·apps/(tests/·scripts/ 는 애초에 이 두 디렉터리 밖이라 자동 제외) 전수 스캔.

    반환: (직접 접근 이름 ∪ Settings alias 이름, AST 로 해석 불가능한 동적 접근 위치 목록).
    """
    direct_names, unresolved = collect_direct_env_access([REPO / "opencrab", REPO / "apps"])
    aliases = collect_config_aliases(REPO / "opencrab" / "config.py")
    return direct_names | aliases, unresolved


class TestEnvContractSync:
    """환경 변수 문서-코드 동기화 가드 [W1][X1].

    이 테스트는 **의도적으로** 코드에 새 환경변수 접근이 추가되고 tools.env_contract.ENV_CONTRACT
    (+ INDIRECT_ENV_ACCESS)가 갱신되지 않으면 실패하도록 설계됐다. 그 실패는 이 테스트의 결함이
    아니라 가드가 제 역할(전수성 동기화)을 하는 것이다 -- 고칠 대상은 프로덕션 코드가 아니라
    ENV_CONTRACT/INDIRECT_ENV_ACCESS 쪽이다.
    """

    def test_no_unresolved_dynamic_env_access(self):
        _discovered, unresolved = scan_env_contract()
        assert unresolved == [], (
            f"AST 로 해석 불가능한 동적 env 접근: {unresolved} -- "
            "tools.env_contract.INDIRECT_ENV_ACCESS 에 이름과 근거 위치를 등재하라."
        )

    def test_discovered_names_are_bidirectionally_equivalent_to_contract(self):
        discovered, _unresolved = scan_env_contract()

        # 실 구현 확인 결과 INDIRECT_ENV_ACCESS 의 실제 관례는 이름→근거설명(키가 변수명)이다.
        flat_indirect: set[str] = set(env_contract.INDIRECT_ENV_ACCESS.keys())

        documented = set(env_contract.ENV_CONTRACT.keys())
        code_side = discovered | flat_indirect

        missing_from_contract = code_side - documented
        missing_from_code = documented - code_side

        assert missing_from_contract == set(), (
            f"코드에는 있으나 ENV_CONTRACT 에 없는 이름: {sorted(missing_from_contract)}"
        )
        assert missing_from_code == set(), (
            f"ENV_CONTRACT 에는 있으나 코드에서 발견되지 않는 이름: {sorted(missing_from_code)}"
        )

    def test_known_regression_cases_are_captured(self):
        # v5 [X1] 실측: 정규식(-oE)은 작은따옴표 리터럴을 놓쳤다(opencrab/pack/build.py).
        discovered, _unresolved = scan_env_contract()
        assert "PACK_OUT_ROOT" in discovered
        assert "PACK_LIB_STRICT" in discovered

    def test_stale_secret_env_vars_resolved_without_indirect_registry(self):
        # opencrab/auth.py 의 `[name for name in _STALE_SECRET_ENV_VARS if ...]` 는 top-level
        # 상수 + comprehension 소스 해석만으로 직접 풀려야 한다 -- INDIRECT_ENV_ACCESS 에 등재할
        # 필요가 없다는 것이 이 설계의 결정이다(등재 없이도 미해결 목록에 남지 않아야 한다).
        discovered, unresolved = scan_env_contract()
        for name in ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"):
            assert name in discovered
        assert not any("auth.py" in loc for loc in unresolved)

    def test_click_envvar_anthropic_api_key_captured(self):
        discovered, _unresolved = scan_env_contract()
        assert "ANTHROPIC_API_KEY" in discovered

    def test_config_alias_localcrab_env_file_captured(self):
        discovered, _unresolved = scan_env_contract()
        assert "LOCALCRAB_ENV_FILE" in discovered


# ---------------------------------------------------------------------------
# #248: env 전달 계층 방호 (refclient 원천 검사 + validate 층 키·값 분리 검사)
# ---------------------------------------------------------------------------


class TestEnvGuard:
    """#248: env 키·값의 비문자열·NUL·고립 서로게이트를 subprocess/execve 예외
    (ValueError/UnicodeEncodeError/TypeError) 누출 전에 명시 ValueError 로 거부한다.

    원천 3종(강제 변수 값, base_env, server_env 원본)을 placeholder 확장 전에
    각 1회 검사한다 -- 검증된 스칼라 문자열의 치환 연접은 새 위반 문자를 만들 수
    없으므로 합성 결과 재검사는 없다 (설계 v4 불변식)."""

    ROOT = "/plugin/root"
    DATA = "/plugin/data"

    def _base(self):
        return {"PATH": "/usr/bin", "HOME": "/home/user"}

    # -- 정상 --

    def test_normal_synthesis_unchanged(self):
        server_env = {"LOCAL_DATA_DIR": "${PLUGIN_DATA}", "PLAIN": "value"}
        env = rc.build_subprocess_env(server_env, self.ROOT, self.DATA, self._base())
        assert env["LOCAL_DATA_DIR"] == self.DATA
        assert env["PLAIN"] == "value"
        assert env["PLUGIN_ROOT"] == self.ROOT
        assert env["PLUGIN_DATA"] == self.DATA
        assert env["PATH"] == "/usr/bin"

    # -- 오류 --

    def test_nul_value_from_json_roundtrip_rejected(self, tmp_path):
        # JSON NUL 이스케이프로 디스크 왕복해 유입되는 실제 형태를 재현한다.
        path = tmp_path / "env.json"
        path.write_text(json.dumps({"API_MODE": "a\x00b"}), encoding="utf-8")
        server_env = json.loads(path.read_text(encoding="utf-8"))
        with pytest.raises(ValueError) as exc:
            rc.build_subprocess_env(server_env, self.ROOT, self.DATA, self._base())
        assert "API_MODE" in str(exc.value)
        assert "NUL" in str(exc.value)

    def test_multiple_entries_all_listed(self):
        with pytest.raises(ValueError) as exc:
            rc.build_subprocess_env(
                {"A": "x\x00y", "B": "\udc80"}, self.ROOT, self.DATA, self._base()
            )
        msg = str(exc.value)
        assert "'A'" in msg and "'B'" in msg

    def test_same_entry_key_and_value_violations_both_reported(self):
        # 키 NUL 과 값 서로게이트가 같은 항목에서 각각 보고된다 (위반 손실 금지).
        with pytest.raises(ValueError) as exc:
            rc.build_subprocess_env({"B\x00": "\ud800"}, self.ROOT, self.DATA, self._base())
        msg = str(exc.value)
        assert "NUL" in msg
        assert "surrogate" in msg

    @pytest.mark.parametrize("bad", [5, b"bytes", None, ["x"]])
    def test_non_string_value_explicit_value_error(self, bad):
        # expand_placeholders(re.sub) 의 TypeError 누출이 아니라 명시 ValueError 다.
        with pytest.raises(ValueError, match="must be a string"):
            rc.build_subprocess_env({"A": bad}, self.ROOT, self.DATA, self._base())

    # -- 엣지 --

    @pytest.mark.parametrize(
        ("server_env", "base_env", "data"),
        [
            ({"A": "\ud800"}, None, DATA),  # 고역 서로게이트 값
            # 저역: POSIX surrogateescape 로 왕복 가능한 범위지만 정책상 일괄 거부
            # (결정론적 §9.1 계약 -- 정책 자체를 고정하는 케이스)
            ({"A": "\udc80"}, None, DATA),
            ({"A\x00": "v"}, None, DATA),  # server_env 키 NUL
            (None, {"PATH": "x\x00"}, DATA),  # base_env 값 오염
            (None, {"P\ud800": "v"}, DATA),  # base_env 키 오염
            (None, None, "/d\x00"),  # 강제 변수 값(plugin_data) 오염
        ],
    )
    def test_contaminated_sources_rejected(self, server_env, base_env, data):
        with pytest.raises(ValueError):
            rc.build_subprocess_env(
                server_env or {},
                self.ROOT,
                data,
                base_env if base_env is not None else self._base(),
            )

    @pytest.mark.parametrize("field", ["root", "data"])
    def test_non_string_forced_value_no_typeerror_leak(self, field):
        root = Path(self.ROOT) if field == "root" else self.ROOT
        data = Path(self.DATA) if field == "data" else self.DATA
        with pytest.raises(ValueError, match="must be a string"):
            rc.build_subprocess_env({"X": "${PLUGIN_ROOT}/bin"}, root, data, self._base())

    def test_stdio_client_rejects_before_popen(self, monkeypatch):
        # 합성 함수를 우회한 직접 생성 경로(sink)에서도 Popen 도달 전에 거부된다.
        calls = []
        monkeypatch.setattr(rc.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
        with pytest.raises(ValueError):
            rc.JsonRpcStdioClient(cmd=["prog"], env={"A": "x\x00"}, cwd="/tmp")
        assert calls == []


class TestValidateEnvChokepoint:
    """#248: mcp.json env 오염은 게이트=오류, 로더=entry skip (§7.2.2).
    기존 TestPathResolutionExceptionLeaks 와 같은 JSON 디스크 왕복 유입 형태다."""

    @pytest.mark.parametrize(
        "env",
        [
            {"A": "x\x00y"},  # 값 NUL
            {"A": "\ud800"},  # 값 고립 서로게이트
            {"A\x00": "v"},  # 키 NUL
        ],
    )
    def test_gate_error_loader_skip(self, tmp_path, plugin_root, plugin_data, env):
        obj = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "bad": {"type": "stdio", "command": "opencrab", "env": env},
                "ok": {"type": "stdio", "command": "opencrab"},
            },
        }
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        errors, _warnings, _servers = v.validate_mcp_config(
            loaded, v.MODE_GATE, plugin_root, plugin_data
        )
        assert errors != []
        _errors, warnings, servers = v.validate_mcp_config(
            loaded, v.MODE_LOADER, plugin_root, plugin_data
        )
        assert "bad" not in servers and "ok" in servers
        assert warnings != []

    def test_non_string_value_does_not_mask_key_violation(self, plugin_root, plugin_data):
        # 값이 비문자열이어도 같은 항목의 키 오염이 함께 보고된다 (분리 검사).
        obj = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "bad": {"type": "stdio", "command": "opencrab", "env": {"K\x00": 5}}
            },
        }
        errors, _warnings, _servers = v.validate_mcp_config(
            obj, v.MODE_GATE, plugin_root, plugin_data
        )
        joined = "; ".join(errors)
        assert "must be a string" in joined
        assert "NUL" in joined


class TestDevExtraContract:
    """#246: packaging 검증기의 직접 의존(jsonschema)은 dev extra 에 직접 선언한다.
    chromadb 전이 의존으로 우연히 설치되는 상태는 계약이 아니다."""

    def test_jsonschema_declared_in_dev_extra(self):
        with open(REPO / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        dev_deps = data["project"]["optional-dependencies"]["dev"]
        assert any(
            re.match(r"jsonschema\s*(\[|>|=|<|!|~|;|$)", d) for d in dev_deps
        ), dev_deps
