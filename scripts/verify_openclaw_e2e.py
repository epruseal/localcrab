#!/usr/bin/env python3
"""실 클라이언트 종단 검증 러너 (#249) -- `docs/agent-plugin-packaging.md` 의 실행판.

실 Agent Plugins 클라이언트로 이 패키지를 설치·발견시키고, embedded agent turn 에서
실제 MCP `tools/call` 이 왕복하는지 경계 원문과 스토어 부작용으로 확인한다.
LLM 자리 하나만 결정론적 드라이버로 대체하며 클라이언트도 서버도 실물이다.

CI 게이트가 아니다. CI 는 `tests/test_agent_plugin_smoke.py` 의 레퍼런스 클라이언트가
담당하고, 이 러너의 판정기만 `tests/test_openclaw_e2e_evidence.py` 로 회귀를 잡는다.

격리 계약: 스크래치 `HOME`, 허용목록 환경, 임시 루프백 포트만 쓴다. 클라이언트를
`--profile` 로 실행하지 않는다 -- 읽기 명령이라도 라이브 상태를 마이그레이션한다.

사용:
  python scripts/verify_openclaw_e2e.py \
      --plugin-dist dist/localcrab-plugin \
      --opencrab-bin "$(command -v opencrab)" \
      --client-bin "$(command -v openclaw)" \
      --scratch /tmp/openclaw-e2e

  # 인과를 가장 강하게 보이려면 기록기를 빼고 한 번 더 돌린다
  python scripts/verify_openclaw_e2e.py ... --no-recorder
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_PARENT = REPO_ROOT / "packaging" / "agent-plugin"
if str(_TOOLS_PARENT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_PARENT))

from tools.openclaw_e2e import (  # noqa: E402  (sys.path 삽입 후 의도된 임포트 순서)
    MODEL_ID,
    DeterministicProvider,
    check_persisted,
    new_nonce,
    verify_evidence,
    write_recorder_shim,
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_env(home: Path, tmpdir: Path, path_dirs: list[str]) -> dict:
    """실행 환경을 상속하지 않고 처음부터 조립한다.

    ambient 를 물려주면 스트레이 `OPENCRAB_*`/`LOCALCRAB_*` 변수가 서버가 읽고 쓰는
    위치를 바꿀 수 있어 측정 자체가 무효가 된다.
    """
    return {
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "PATH": os.pathsep.join(path_dirs),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def write_client_config(home: Path, port: int) -> Path:
    """설치가 기록한 `plugins.entries` 를 보존한 채 provider 설정만 병합한다."""
    cfg_path = home / ".openclaw" / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg.setdefault("models", {}).setdefault("providers", {})["vllm"] = {
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "apiKey": "local-e2e",
        "api": "openai-completions",
        "timeoutSeconds": 120,
        "models": [{
            "id": MODEL_ID, "name": "LocalCrab E2E Driver", "reasoning": False,
            "input": ["text"], "contextWindow": 128000, "maxTokens": 4096,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }],
    }
    cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = {"primary": f"vllm/{MODEL_ID}"}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin-dist", required=True, help="빌드된 plugin root (build_agent_plugin.py 산출물)")
    ap.add_argument("--opencrab-bin", required=True, help="실제 opencrab 실행 파일의 절대 경로")
    ap.add_argument("--client-bin", required=True, help="실 클라이언트 실행 파일의 절대 경로")
    ap.add_argument("--scratch", required=True, help="스크래치 루트. 매 실행 초기화된다")
    ap.add_argument("--no-recorder", action="store_true",
                    help="경계 기록기를 빼고 돈다. 기동 경로에 하네스 코드가 남지 않으므로 "
                         "부작용 실재가 곧 인과 증거가 된다")
    ap.add_argument("--keep-scratch", action="store_true", help="실패 조사용으로 스크래치를 남긴다")
    args = ap.parse_args(argv)

    scratch = Path(args.scratch).resolve()
    if scratch.exists():
        shutil.rmtree(scratch)
    home, tmpdir, shim_dir, record_dir = (scratch / n for n in ("home", "tmp", "shim", "record"))
    for d in (home, tmpdir, record_dir):
        d.mkdir(parents=True)
    scratch.chmod(0o700)

    opencrab_bin = os.path.abspath(args.opencrab_bin)
    client_bin = os.path.abspath(args.client_bin)
    nonce = new_nonce()
    provider_log = scratch / "provider.jsonl"

    # 설치 환경과 실행 환경을 분리한다. 설치에는 기록기를 태우지 않는다.
    install_path = [os.path.dirname(client_bin), os.path.dirname(opencrab_bin)]
    # 클라이언트가 node 앱이면 런타임도 이 PATH 로 찾아야 한다. ambient 를 상속하지
    # 않으므로 해석은 여기서 한 번만 하고 결과 디렉터리만 허용목록에 넣는다.
    node_bin = shutil.which("node")
    if node_bin:
        install_path.append(os.path.dirname(node_bin))
    install_path += ["/usr/bin", "/bin"]
    install_path = list(dict.fromkeys(install_path))
    install_env = run_env(home, tmpdir, install_path)

    print(f"nonce: {nonce}")
    print("1/6 설치")
    # --force: ClawHub 외부 로컬 경로.  --accept-capabilities: MCP 를 선언한 번들.
    # 후자가 없으면 설치는 되지만 이후 CLI 기동 자체가 막힌다.
    subprocess.run([client_bin, "plugins", "install", "--force", "--accept-capabilities",
                    os.path.abspath(args.plugin_dist)], env=install_env, check=True)

    print("2/6 발견")
    inspect = subprocess.run([client_bin, "plugins", "inspect", "localcrab"],
                             env=install_env, check=True, capture_output=True, text=True).stdout
    for needle in ("Bundle format: agent", "mcpServers", "MCP servers:"):
        if needle not in inspect:
            print(f"FAIL: plugins inspect 출력에 {needle!r} 가 없다\n{inspect}", file=sys.stderr)
            return 1
    print("     번들 감지 확인 (MCP 서버 선언 포함)")

    plugin_data = home / ".openclaw" / "plugin-data" / "localcrab"
    plugin_data.mkdir(parents=True, exist_ok=True)

    with DeterministicProvider(nonce, provider_log) as provider:
        port = provider.start(free_port())
        write_client_config(home, port)

        print("3/6 사전 조회 -- 난수 노드가 아직 없어야 한다")
        before = check_persisted(opencrab_bin, plugin_data, nonce)
        if before.get("found"):
            print(f"FAIL: 실행 전에 이미 존재한다: {before}", file=sys.stderr)
            return 1
        print(f"     found={before.get('found')}")

        run_path = list(install_path)
        if not args.no_recorder:
            write_recorder_shim(shim_dir, opencrab_bin, record_dir)
            run_path.insert(0, str(shim_dir))   # 기록기가 실제 바이너리를 가린다
            print("     경계 기록기 활성 (바이트 tee)")
        else:
            print("     경계 기록기 없음 -- 기동 경로에 하네스 코드가 남지 않는다")

        print("4/6 embedded agent turn")
        proc = subprocess.run(
            [client_bin, "agent", "--local", "--json",
             "--session-key", f"agent:main:e2e-{nonce}",
             "--model", f"vllm/{MODEL_ID}",
             "-m", "Call the LocalCrab tools as instructed."],
            env=run_env(home, tmpdir, run_path), capture_output=True, text=True, timeout=600,
        )
        (scratch / "agent.json").write_text(proc.stdout, encoding="utf-8")
        (scratch / "agent.err").write_text(proc.stderr, encoding="utf-8")
        if "failed to start server" in proc.stderr:
            first = next((ln for ln in proc.stderr.splitlines() if "failed to start server" in ln), "")
            print(f"FAIL: MCP 서버 기동 실패\n{first}", file=sys.stderr)
            return 1

    print("5/6 사후 독립 조회 -- 별도 프로세스, 같은 스토어")
    after = check_persisted(opencrab_bin, plugin_data, nonce)
    print(f"     found={after.get('found')}")
    if not after.get("found"):
        print(f"FAIL: 변경 연산의 부작용이 스토어에 없다: {after}", file=sys.stderr)
        return 1

    print("6/6 증거 판정")
    def read(p: Path) -> str:
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    verdict = verify_evidence(
        nonce=nonce,
        client_to_server=read(record_dir / "client_to_server.raw"),
        server_to_client=read(record_dir / "server_to_client.raw"),
        provider_log=read(provider_log),
    )
    print(verdict.render())
    if not verdict.passed:
        return 1

    print(f"\n증거 원문: {scratch}")
    if not args.keep_scratch:
        print("(--keep-scratch 를 주지 않으면 다음 실행이 이 디렉터리를 지운다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
