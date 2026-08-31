"""Agent Plugins 레퍼런스 클라이언트 스모크 테스트 (이슈 #137, design v2 §8/v3 §8, clean 성격).

packaging/agent-plugin/tools/refclient.py 를 실제 opencrab 콘솔 스크립트에 대해 end-to-end 로
구동한다: 실 레포 빌드 → 로더 모드 discovery(§5→§6→§7) → placeholder 확장(§9.2) → sanitize 된
최소 base env(PATH·HOME 만, §9.1) → `opencrab init` 프로비저닝(README 정본 명령) → stdio MCP
서버 기동 → initialize/tools-list/tools-call(ontology_manifest) 라운드트립 → 빌드 산출물 불변 확인.

라이브 자원 무접촉: 포트 8765/8766·systemd·~/.openclaw 어느 것도 쓰지 않는다. HOME·PLUGIN_DATA
모두 이 테스트가 tmp_path 아래 새로 만든 디렉터리다. ontology_manifest 는 describe_grammar()의
순수 호출(스토어 무접촉)이므로 임베딩/외부 스토어 접촉이 전혀 필요 없다 -- 이 스모크가 "clean"
성격을 유지하는 근거다.

TDD RED 단계: packaging/agent-plugin/tools/{validate,build,refclient}.py 는 아직 구현되지
않았다. 이 파일을 수집(collect)하면 ImportError 로 실패하는 것이 이 시점의 올바른 결과다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packaging" / "agent-plugin"))

from tools import build as b  # noqa: E402
from tools import refclient as rc  # noqa: E402
from tools import validate as v  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"


def _tree_hash(root: Path) -> str:
    """plugin root 하위 전 파일의 (상대경로, 내용) 을 정렬해 단일 해시로 묶는다.

    빌드 산출물이 스모크 실행(레퍼런스 클라이언트의 discovery·기동·tools/call) 전후로 바뀌지
    않았음 -- 즉 클라이언트가 패키지 자체에 아무것도 쓰지 않았음 -- 을 증명한다 [R4].
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root)).replace(os.sep, "/")
            digest.update(rel.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _resolve_bin_dir() -> Path:
    """레퍼런스 클라이언트가 `opencrab` 콘솔 스크립트를 찾을 PATH 디렉터리를 정한다.

    OPENCRAB_SMOKE_BIN_DIR 환경변수가 있으면 그것을 쓰고, 없으면 `shutil.which("opencrab")`가
    가리키는 디렉터리를 쓴다. 어느 쪽도 없으면 sys.executable 폴백 없이 명확히 실패한다 -- 이
    폴백을 두지 않는 것 자체가 [R7] "client-defined PATH" 원칙(§7.2.1)의 검증이다: 레퍼런스
    클라이언트는 자신이 정의한 PATH 로만 bare command 를 해석해야 하고, 테스트 러너 자신의
    인터프리터로 조용히 대체해서는 안 된다.
    """
    override = os.environ.get("OPENCRAB_SMOKE_BIN_DIR")
    if override:
        return Path(override)
    found = shutil.which("opencrab")
    if not found:
        pytest.fail(
            "opencrab 콘솔 스크립트를 PATH 에서 찾을 수 없다. OPENCRAB_SMOKE_BIN_DIR 로 bin "
            "디렉터리를 지정하거나 PATH 에 opencrab 을 설치하라 (sys.executable 폴백은 "
            "§7.2.1 의 client-defined PATH 원칙에 어긋나므로 쓰지 않는다)."
        )
    return Path(found).resolve().parent


@pytest.fixture
def built_plugin(tmp_path):
    """실제 레포(REPO)를 빌드해 tmp 산출물을 만든다. REPO 자체는 읽기 전용으로만 다룬다
    (build() 는 out_dir 에만 쓰고 소스 레포를 변형하지 않는다)."""
    out_dir = tmp_path / "dist"
    return b.build(REPO, out_dir)


class TestAgentPluginSmoke:
    """레퍼런스 클라이언트 clean 스모크: 빌드된 실 패키지를 실 opencrab 런타임에 대해 기동한다."""

    def test_reference_client_end_to_end(self, built_plugin, tmp_path):
        plugin_root = built_plugin
        before_hash = _tree_hash(plugin_root)

        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        home_dir = tmp_path / "home"
        home_dir.mkdir()

        # 1. 로더 모드 discovery (§5→§6→§7) -- 구현 네임스페이스 없음(이 패키지는 extensions 미사용)
        loaded = rc.load_plugin(plugin_root, plugin_data, implemented_namespaces=frozenset())
        assert "localcrab" in loaded.servers
        server_cfg = loaded.servers["localcrab"]

        # 2. bin_dir 해석 + sanitize 된 최소 base env(PATH·HOME 만) [R3]
        bin_dir = _resolve_bin_dir()
        opencrab_path = bin_dir / "opencrab"
        assert opencrab_path.is_file(), f"{opencrab_path} 가 존재하지 않는다"
        base_env = {"PATH": str(bin_dir), "HOME": str(home_dir)}

        # 3. bare command 해석: client-defined PATH 탐색만, sys.executable 폴백 없음 [R7]
        resolved_command = rc.resolve_command(server_cfg["command"], base_env["PATH"])
        assert resolved_command is not None
        assert Path(resolved_command).resolve() == opencrab_path.resolve()

        # ${PLUGIN_DATA} → tmp 확장 (§9.2)
        expanded_cwd = v.expand_placeholders(server_cfg["cwd"], str(plugin_root), str(plugin_data))
        assert expanded_cwd == str(plugin_data)

        # PLUGIN_ROOT/PLUGIN_DATA 는 base env → configured env → 최종 오버라이드 순으로 주입 (§9.1)
        full_env = rc.build_subprocess_env(
            server_cfg.get("env", {}), str(plugin_root), str(plugin_data), base_env
        )
        assert full_env["PLUGIN_ROOT"] == str(plugin_root)
        assert full_env["PLUGIN_DATA"] == str(plugin_data)
        assert full_env["LOCAL_DATA_DIR"] == str(plugin_data)

        # 4. 프로비저닝: README 정본 명령 그대로 (cwd=PLUGIN_DATA, env=mcp.json 확장값)
        init_result = subprocess.run(
            [resolved_command, "init"],
            cwd=expanded_cwd,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert init_result.returncode == 0, (
            f"opencrab init 실패: stdout={init_result.stdout!r} stderr={init_result.stderr!r}"
        )
        db_path = plugin_data / "opencrab.db"
        assert db_path.is_file(), "부트스트랩이 침묵 skip 됐다 -- opencrab.db 가 생성되지 않았다"

        # 5. serve 기동 → initialize → tools/list → tools/call(ontology_manifest)
        client = rc.JsonRpcStdioClient(
            cmd=[resolved_command, "serve"], env=full_env, cwd=expanded_cwd
        )
        try:
            # JsonRpcStdioClient.request() 는 JSON-RPC 봉투 전체를 반환한다
            # ({"jsonrpc", "id", "result"}) -- result 만 벗겨써야 한다(실측 확인).
            init_response = client.request("initialize", {"protocolVersion": PROTOCOL_VERSION})
            assert init_response["result"]["protocolVersion"] == PROTOCOL_VERSION

            tools_response = client.request("tools/list", {})
            tool_names = [tool["name"] for tool in tools_response["result"]["tools"]]
            assert "ontology_manifest" in tool_names

            call_response = client.request(
                "tools/call", {"name": "ontology_manifest", "arguments": {}}
            )
            payload = json.loads(call_response["result"]["content"][0]["text"])
            # v5 [X2]: describe_grammar() 실제 키는 spaces/meta_edges (하이픈 표기 아님)
            assert "spaces" in payload
            assert "meta_edges" in payload
        finally:
            client.close()

        # 6. 전후 plugin root 트리 해시 불변 -- 패키지 자체는 건드리지 않았다 [R4]
        after_hash = _tree_hash(plugin_root)
        assert after_hash == before_hash
