"""§5/§6/§7/§9 최소 conformant client(§11.1) -- 참조 구현.

이 모듈은 opencrab 런타임의 일부가 아니라, 저작한 Agent Plugin 패키지가 실제
클라이언트 관점에서 로드/실행 가능한지 검증하는 데 쓰는 최소 참조 클라이언트다.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .validate import MODE_LOADER, expand_placeholders, validate_package


class PluginRejectedError(Exception):
    """§5.2/§5.3/§11.1: 로더 모드 검증에서 치명적 오류가 발견되어 플러그인 전체를 거부."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


@dataclass
class LoadedPlugin:
    manifest: dict
    servers: dict[str, dict] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_plugin(plugin_root, plugin_data, implemented_namespaces: frozenset = frozenset()) -> LoadedPlugin:
    """validate_package(mode=loader) 위에 구축. 치명적 오류가 있으면 PluginRejectedError."""
    plugin_root = Path(plugin_root)
    report = validate_package(
        plugin_root, MODE_LOADER, plugin_data=plugin_data, implemented_namespaces=implemented_namespaces
    )
    if report.errors:
        raise PluginRejectedError(report.errors)
    manifest_obj = json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8"))
    return LoadedPlugin(
        manifest=manifest_obj,
        servers=dict(report.servers),
        skills=list(report.skills),
        warnings=list(report.warnings),
    )


def resolve_command(command: str, path_env: str) -> str | None:
    """§7.2.1 command 해석. 그 외 폴백 없음.

    bare 커맨드(경로 구분자 없음)는 shutil.which(command, path=path_env) 로만
    플랫폼 PATH 탐색한다. './'-접두 커맨드는 plugin root 기준 상대 경로다 --
    이 좁은 2-인자 시그니처에는 plugin_root 자리가 따로 없으므로, 그 분기에서는
    `path_env` 인자 자리에 plugin_root 절대경로를 넘기는 것이 호출 관례다
    (호출자가 command 형태에 맞는 값을 골라 넘긴다).
    """
    if command.startswith("./"):
        plugin_root = path_env
        candidate = os.path.normpath(os.path.join(plugin_root, command[len("./"):]))
        root = os.path.normpath(plugin_root)
        if candidate != root and not candidate.startswith(root + os.sep):
            return None  # containment 위반
        real_candidate = os.path.realpath(candidate)
        real_root = os.path.realpath(root)
        if real_candidate != real_root and not (real_candidate + os.sep).startswith(real_root + os.sep):
            return None
        return candidate if os.path.isfile(candidate) else None
    return shutil.which(command, path=path_env)


def build_subprocess_env(server_env: dict, plugin_root: str, plugin_data: str, base_env: dict) -> dict:
    """§9.1: base 복사 -> server_env 각 값 expand_placeholders 후 오버레이 ->
    마지막에 PLUGIN_ROOT/PLUGIN_DATA 강제 설정(순서 고정 -- 덮어쓰기 불가)."""
    env = dict(base_env)
    for key, value in (server_env or {}).items():
        env[key] = expand_placeholders(value, plugin_root, plugin_data)
    env["PLUGIN_ROOT"] = plugin_root
    env["PLUGIN_DATA"] = plugin_data
    return env


class JsonRpcStdioClient:
    """newline-delimited JSON-RPC over subprocess stdio (§11.1 최소 conformant client).

    한계: 응답 대기는 최초 가독 이벤트만 selectors 로 시간제한하고, 그 이후
    한 줄을 다 받을 때까지는 blocking readline() 을 쓴다. 로컬에서 완전한
    한 줄씩 즉시 flush 하는 정상 서버를 상대하는 참조/테스트 클라이언트로는
    충분하지만, 응답을 스트리밍으로 찔끔찔끔 보내는 서버에는 timeout 이
    정확히 보장되지 않는다 -- 업그레이드 경로는 저수준 논블로킹 파서 도입.
    """

    def __init__(self, cmd: list[str], env: dict, cwd: str) -> None:
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            text=True,
            bufsize=1,
        )
        self._next_id = 1

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        req_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}

        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

        assert self._proc.stdout is not None
        sel = selectors.DefaultSelector()
        sel.register(self._proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no response to {method!r} within {timeout}s")
                if not sel.select(timeout=remaining):
                    continue
                line = self._proc.stdout.readline()
                if not line:
                    stderr_tail = self._proc.stderr.read() if self._proc.stderr is not None else ""
                    raise RuntimeError(f"server closed stdout before responding to {method!r}: {stderr_tail}")
                line = line.strip()
                if not line:
                    continue
                resp = json.loads(line)
                if resp.get("id") == req_id:
                    return resp
                # id 불일치(다른 요청 응답/notification) -- 계속 대기
        finally:
            sel.close()

    def close(self) -> None:
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
            self._proc.wait()
