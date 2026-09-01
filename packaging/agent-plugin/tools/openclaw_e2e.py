"""실 클라이언트 종단 검증 하네스 -- `tools/refclient.py` 의 형제.

`refclient.py` 가 "레퍼런스 클라이언트로 패키지를 기동할 수 있는가"를 CI 에서
확인한다면, 이 모듈은 "**실제** Agent Plugins 클라이언트가 이 패키지를 통해
`tools/call` 을 왕복하는가"를 로컬에서 확인하는 데 쓴다. 절차와 판정 기준은
`docs/agent-plugin-packaging.md` 의 "실 클라이언트 검증" 절이 정본이다.

이 하네스가 대체하는 것은 **LLM 자리 하나뿐**이다. 클라이언트도 서버도 실물이며,
드라이버는 "이 도구를 호출하라"는 결정만 내고 도구 결과를 만들지도 해석하지도
않는다. 따라서 이 하네스로 얻는 증거의 정확한 명칭은 "실 클라이언트 플러그인·MCP
통합 종단 검증(모델 제공자만 결정론적 드라이버로 대체)"이다.

## 증거가 순환하지 않게 만드는 세 장치

1. **경계 기록기는 파싱하지 않는다.** 클라이언트와 서버 사이에 놓이는 tee 는
   양방향 바이트를 그대로 복사만 한다. JSON 을 해석하지 않으므로 프레임을
   위조하거나 판정에 개입할 수 없다.
2. **변경 연산과 실행별 난수.** 정적 무인자 도구만 호출하면 "결과를 합성해
   넣었다"는 반례가 성립한다. 실행마다 새로 뽑은 난수를 인자로 받는 변경 연산을
   한 번 호출하면 그 반례가 소거된다.
3. **사후 독립 조회.** turn 이 끝난 뒤 별도 프로세스가 같은 스토어에 새 stdio
   세션을 열어 그 난수의 부작용이 실재하는지 확인한다.

인과를 가장 강하게 보이려면 `record_dir=None` 으로 기록기를 아예 빼고 한 번 더
돌린다. 그러면 기동 경로에 이 하네스의 코드가 남지 않으므로, 난수 노드가 생겼다는
사실 자체가 클라이언트의 실제 호출을 증명한다.

## 함정 (실측으로 확인)

클라이언트는 MCP 서버 자식 프로세스에 **환경을 소독해 넘긴다.** 자식이 받는
변수는 극소수다. 따라서 경계에 끼우는 래퍼는 자기 설정을 환경 변수로 받으면
자식에서 즉사하고, 증상은 서버 기동 실패로만 보인다. `write_recorder_shim()` 이
경로를 소스에 상수로 박는 이유가 이것이다.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL_ID = "localcrab-e2e-driver"

#: 1회차 호출 -- 정적 무인자 도구. 도구 노출과 결과 형태만 확인한다.
PROBE_TOOL = "ontology_manifest"
#: 2회차 호출 -- 실행별 난수를 인자로 받는 **변경 연산**. 인과 증거의 핵심이다.
MUTATING_TOOL = "ontology_add_node"
#: 사후 독립 조회에 쓰는 읽기 도구.
READBACK_TOOL = "ontology_get_node"

#: 클라이언트가 도구 이름에 붙일 수 있는 네임스페이스 구분자.
_NS_SEPARATORS = "__./-:"


def new_nonce() -> str:
    """실행별 난수. 노드 id 로 그대로 쓰이므로 영숫자와 하이픈만 쓴다."""
    return "e2e-nonce-" + secrets.token_hex(8)


def match_tool(offered: list[str], suffix: str) -> str | None:
    """클라이언트가 붙인 네임스페이스를 무시하고 도구 하나를 고른다.

    후보가 정확히 하나일 때만 고른다. 둘 이상이면 어느 쪽을 부를지 하네스가
    임의로 정하게 되고, 그러면 판정이 무엇을 검증했는지 모호해진다.
    """
    hits = [
        name
        for name in offered
        if name == suffix
        or (name.endswith(suffix) and len(name) > len(suffix) and name[-len(suffix) - 1] in _NS_SEPARATORS)
    ]
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------
# 증거 판정 -- 기록기와 구현을 공유하지 않는 독립 파서
# --------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(name, passed, detail))

    def render(self) -> str:
        lines = [f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks]
        lines.append(f"VERDICT: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def _parse_frames(raw: str) -> list[dict]:
    """줄 단위 JSON-RPC 프레임을 읽는다. 해석 불가한 줄은 조용히 버린다.

    기록기가 바이트 tee 라 부분 프레임이 남을 수 있으므로 관대하게 읽는다.
    """
    frames = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            frames.append(obj)
    return frames


def verify_evidence(
    nonce: str,
    client_to_server: str,
    server_to_client: str,
    provider_log: str,
    expect_boundary: bool = True,
) -> Verdict:
    """네 산출물을 대사해 난수가 경계를 실제로 건넜는지 판정한다.

    `client_to_server`/`server_to_client` 는 경계 기록기가 남긴 원문이고,
    `provider_log` 는 드라이버가 남긴 JSONL 이다.

    `expect_boundary` 는 **호출자가 기록기를 태웠는지**를 말한다. 이 값을 받지
    않으면 판정이 fail-open 이 된다: 기록기를 태웠는데 PATH 해석이 어긋나 아무
    바이트도 안 잡힌 실행과, 애초에 기록기를 뺀 실행이 구별되지 않아 전자가
    조용히 통과한다. 태웠다고 말했는데 원문이 없으면 실패다.

    두 모드는 증명하는 것이 다르며 서로를 대체하지 않는다.

    - 기록기 활성: 클라이언트가 실제 `tools/call` 프레임을 보냈음을 보인다.
    - 기록기 비활성(`expect_boundary=False`): 기동 경로에 하네스 코드가 없는
      상태에서 부작용이 생겼음을 보인다. 클라이언트가 상태를 바꿨다는 것까지만
      증명하고, 그 수단이 MCP 였다는 것은 증명하지 않는다 -- 클라이언트가 MCP
      아닌 경로로 같은 스토어를 바꿨을 가능성이 남는다. 그 가능성은 기록기
      활성 실행이 닫는다. **두 모드를 모두 돌려야 결론이 닫힌다.**
    """
    verdict = Verdict()
    outbound = _parse_frames(client_to_server)
    inbound = _parse_frames(server_to_client)
    # 파싱 결과가 아니라 **원문 유무**로 판정한다. 파싱된 프레임으로 판정하면
    # 잘린 비 JSON 바이트만 남은 캡처가 프레임 0건이 되어 "기록기 없음" 으로
    # 새어 나간다. 어느 방향이든 바이트가 있으면 기록기가 개입한 것이다.
    boundary_recorded = bool(client_to_server.strip()) or bool(server_to_client.strip())

    # --- provider 측: 드라이버가 난수를 인자로 실어 보냈는가 ---
    provider_events = []
    for line in provider_log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            provider_events.append(json.loads(line))
        except ValueError:
            continue

    assistant_calls = [
        e["payload"]
        for e in provider_events
        if e.get("kind") == "decision" and (e.get("payload") or {}).get("type") == "tool_call"
    ]
    nonce_calls = [c for c in assistant_calls if nonce in (c.get("args") or "")]
    verdict.add(
        "provider_issued_nonce_call",
        len(nonce_calls) == 1,
        f"난수를 인자로 실은 assistant tool call {len(nonce_calls)}건 (기대 1건)",
    )

    # 난수가 문자열 어딘가에 있는 것만으로는 부족하다. 파싱한 인자의 노드 id 이고
    # 그 호출이 변경 연산이어야 한다. 그래야 "정적 도구만 불렀다"가 걸러진다.
    bound = False
    detail = "난수를 실은 호출이 없다"
    if len(nonce_calls) == 1:
        call = nonce_calls[0]
        try:
            parsed = json.loads(call.get("args") or "{}")
        except ValueError:
            parsed = {}
        name_ok = match_tool([call.get("tool") or ""], MUTATING_TOOL) is not None
        arg_ok = parsed.get("node_id") == nonce
        bound = name_ok and arg_ok
        detail = (f"도구={call.get('tool')!r} (변경 연산 여부 {name_ok}), "
                  f"파싱된 node_id 가 난수와 일치 {arg_ok}")
    verdict.add("provider_call_is_the_mutating_tool", bound, detail)

    # --- provider 측: 도구 결과가 난수를 담아 되돌아왔는가 ---
    # 발행한 호출의 call_id 와 묶는다. 아무 role=tool 본문에 난수가 있는지만 보면
    # 다른 호출의 결과가 그 자리를 대신해도 통과한다. 클라이언트가 id 에서 비영숫자를
    # 지우는 경우가 있으므로 정규화해서 비교한다.
    def norm(value: str) -> str:
        return "".join(ch for ch in value if ch.isalnum())

    # 빈 문자열을 버린다. 남겨 두면 양쪽 id 가 모두 없는 입력에서 "" == "" 로
    # 맞아떨어져 결속이 공전한다 -- 경계 쪽 None == None 과 같은 결함 유형이다.
    issued_ids = {norm(c.get("call_id") or "") for c in nonce_calls} - {""}
    tool_msgs = []
    for e in provider_events:
        if e.get("kind") != "request":
            continue
        for msg in (e["payload"].get("messages") or []):
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            tool_msgs.append((norm(msg.get("tool_call_id") or ""), content))
    matched = [c for (cid, c) in tool_msgs if cid in issued_ids and nonce in c]
    verdict.add(
        "provider_received_nonce_result",
        len(matched) >= 1,
        f"발행한 call_id 에 대응하면서 난수를 담은 role=tool 메시지 {len(matched)}건 "
        f"(기대 1건 이상, 전체 {len(tool_msgs)}건)",
    )

    if boundary_recorded and not expect_boundary:
        verdict.add(
            "boundary_recorded_as_expected",
            False,
            f"기록기를 빼고 돌렸다고 했는데 경계 원문이 있다 "
            f"(client->server {len(client_to_server)}바이트, "
            f"server->client {len(server_to_client)}바이트) -- "
            "이전 실행의 잔여 로그를 읽고 있을 수 있다",
        )
        return verdict
    if not boundary_recorded:
        verdict.add(
            "boundary_recorded_as_expected",
            not expect_boundary,
            "기록기를 태웠다고 했는데 경계 원문이 비었다 -- 기록기가 개입하지 못했다"
            if expect_boundary
            else "기록기 없이 실행됐다. 이 모드는 클라이언트가 상태를 바꿨음까지만 "
                 "보인다 -- MCP 를 썼다는 증명은 기록기 활성 실행이 담당한다",
        )
        return verdict
    verdict.add(
        "boundary_recorded_as_expected",
        True,
        f"경계 원문 관측 (client->server {len(outbound)}프레임, "
        f"server->client {len(inbound)}프레임)",
    )

    # --- 경계: 난수를 실은 tools/call 프레임이 실재하는가 ---
    # 난수가 arguments 어딘가에 있는 것으로는 부족하다. 정확히 node_id 여야
    # provider 쪽 결속과 대칭이 되고, 엉뚱한 필드에 난수를 실은 호출이 걸러진다.
    nonce_frames = [
        f
        for f in outbound
        if f.get("method") == "tools/call"
        and isinstance((f.get("params") or {}).get("arguments"), dict)
        and (f["params"]["arguments"] or {}).get("node_id") == nonce
    ]
    verdict.add(
        "boundary_tools_call_carries_nonce",
        len(nonce_frames) == 1,
        f"node_id 가 정확히 난수인 tools/call 프레임 {len(nonce_frames)}건 (기대 1건)",
    )

    # --- 경계: 그 호출에 대응하는 서버 응답이 난수를 되돌려주는가 ---
    call_rpc_id = nonce_frames[0].get("id") if nonce_frames else None
    if nonce_frames and call_rpc_id is not None:
        call_id = call_rpc_id
        # id 가 None 인 프레임끼리 None == None 으로 짝지어지면 안 된다.
        replies = [f for f in inbound if f.get("id") == call_id and "result" in f]
        echoed = [r for r in replies if nonce in json.dumps(r.get("result"), ensure_ascii=False)]
        verdict.add(
            "boundary_response_echoes_nonce",
            len(replies) == 1 and len(echoed) == 1,
            f"id={call_id!r} 응답 {len(replies)}건, 그중 난수 포함 {len(echoed)}건 (기대 1/1)",
        )
        called_name = (nonce_frames[0].get("params") or {}).get("name")
    else:
        verdict.add(
            "boundary_response_echoes_nonce",
            False,
            "난수를 실은 tools/call 프레임이 없거나 JSON-RPC id 가 없어 응답을 짝지을 수 없다",
        )
        called_name = None

    # --- 경계: 호출한 도구가 tools/list 로 광고된 것인가 ---
    advertised: set[str] = set()
    for f in inbound:
        for tool in ((f.get("result") or {}).get("tools") or []):
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                advertised.add(tool["name"])
    verdict.add(
        "called_tool_was_advertised",
        bool(called_name) and called_name in advertised,
        f"호출한 도구 {called_name!r} 가 tools/list 광고 {len(advertised)}개 안에 있는가",
    )
    verdict.add(
        "boundary_call_is_the_mutating_tool",
        bool(called_name) and match_tool([called_name], MUTATING_TOOL) is not None,
        f"경계에서 난수를 실어 부른 도구 {called_name!r} 가 변경 연산인가",
    )

    return verdict


# --------------------------------------------------------------------------
# 경계 기록기 -- 파싱하지 않는 바이트 tee
# --------------------------------------------------------------------------

_SHIM_SOURCE = '''\
"""경계 stdio 기록기. PATH 에 `opencrab` 이름으로 놓인다.

순수 바이트 tee 다. 실제 바이너리를 exec 하고 양방향을 그대로 복사하며 각 방향을
원문 로그에 적는다. 아무것도 파싱하지 않으므로 프레임에 개입할 수 없다.

경로를 **환경 변수가 아니라 상수로** 박는 이유: 클라이언트는 MCP 서버 자식
프로세스에 환경을 소독해 넘기므로, 환경 변수로 받으려 하면 자식에서 즉사하고
증상이 서버 기동 실패로만 보인다.
"""
import os, sys, threading, subprocess

REAL = {real_bin!r}
OUT = {record_dir!r}


def pump(src, dst, log_path):
    # 1바이트씩 읽는다. 프레임 경계를 몰라도 되고 버퍼링으로 순서가 뒤바뀌지
    # 않는다는 것이 이 단순화의 값이다. 대가는 바이트 수만큼의 syscall 이며,
    # 로컬 stdio 한 세션 규모에서는 문제되지 않는다. 처리량이 문제가 되면
    # 프레임 경계를 보존하는 청크 단위 tee 로 올린다.
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        while True:
            chunk = src.read(1)
            if not chunk:
                break
            os.write(fd, chunk)
            dst.write(chunk)
            dst.flush()
    finally:
        os.close(fd)
        try:
            dst.close()
        except Exception:
            pass


os.makedirs(OUT, mode=0o700, exist_ok=True)
proc = subprocess.Popen([REAL] + sys.argv[1:], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=None)
threads = [
    threading.Thread(target=pump, args=(sys.stdin.buffer, proc.stdin,
                                        os.path.join(OUT, "client_to_server.raw")), daemon=True),
    threading.Thread(target=pump, args=(proc.stdout, sys.stdout.buffer,
                                        os.path.join(OUT, "server_to_client.raw")), daemon=True),
]
for t in threads:
    t.start()
rc = proc.wait()
# 양방향 모두 회수한다. 서버->클라이언트만 join 하면 클라이언트->서버 말미
# 바이트가 잘려 부분 캡처가 남는다. join 은 timeout 을 넘겨도 조용히 돌아오므로
# 살아남은 스레드가 있으면 캡처가 불완전하다고 알린다 -- 판정기가 이 실행을
# 통과시키면 안 된다.
for t in threads:
    t.join(timeout=5)
stragglers = [t.name for t in threads if t.is_alive()]
if stragglers:
    # stderr 로 쓰면 아무 데도 도달하지 않는다 -- 클라이언트가 MCP 서버 자식의
    # stderr 를 호출자에게 넘겨주지 않는다. 러너가 읽을 수 있도록 파일로 남긴다.
    with open(os.path.join(OUT, "capture_incomplete"), "a") as fh:
        fh.write(",".join(stragglers) + "\\n")
sys.exit(rc)
'''


def write_recorder_shim(shim_dir: str | os.PathLike, real_bin: str, record_dir: str | os.PathLike) -> Path:
    """`shim_dir` 에 `opencrab` 이름의 경계 기록기를 만들고 그 경로를 돌려준다.

    `shim_dir` 을 실행 PATH 맨 앞에 놓으면 클라이언트가 이 기록기를 서버로 띄운다.
    `real_bin` 은 실제 `opencrab` 실행 파일의 **절대 경로**여야 한다 -- 기록기는
    PATH 를 다시 타지 않는다(자기 자신을 무한히 재귀 실행하게 된다).
    """
    shim_dir = Path(shim_dir)
    record_dir = Path(record_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    record_dir.mkdir(parents=True, exist_ok=True)
    real_bin = os.path.abspath(real_bin)

    body = _SHIM_SOURCE.format(real_bin=real_bin, record_dir=str(record_dir))
    py = shim_dir / "opencrab_recorder.py"
    py.write_text(body, encoding="utf-8")

    launcher = shim_dir / "opencrab"
    # 경로에 공백이나 셸 메타문자가 있어도 깨지지 않도록 인용한다.
    launcher.write_text(f'#!/bin/sh\nexec python3 {shlex.quote(str(py))} "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


# --------------------------------------------------------------------------
# 결정론적 모델 드라이버 -- LLM 자리를 대체하는 유일한 조각
# --------------------------------------------------------------------------


class DeterministicProvider:
    """OpenAI 호환 chat-completions 를 말하는 최소 서버.

    LocalCrab 을 흉내내지 않는다. 하는 일은 실 클라이언트가 실제 MCP
    `tools/call` 을 내도록 만드는 결정 두 번과 종료 한 번이 전부다. 스토어에
    접근할 수단이 없고(파일 경로도 opencrab 임포트도 갖지 않는다), 도구 결과를
    만들지도 해석하지도 않는다.
    """

    def __init__(self, nonce: str, log_path: str | os.PathLike) -> None:
        self.nonce = nonce
        self.log_path = Path(log_path)
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- 로깅 ---
    def log(self, kind: str, payload) -> None:
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "kind": kind, "payload": payload},
                                    ensure_ascii=False) + "\n")

    # --- 결정 ---
    def decide(self, body: dict) -> dict:
        offered = [
            (t.get("function") or {}).get("name") or t.get("name") or ""
            for t in (body.get("tools") or [])
        ]
        done = sum(1 for m in (body.get("messages") or []) if m.get("role") == "tool")
        probe = match_tool(offered, PROBE_TOOL)
        mutate = match_tool(offered, MUTATING_TOOL)
        if done == 0 and probe:
            return {"type": "tool_call", "tool": probe, "args": "{}", "call_id": "call-e2e-probe"}
        if done == 1 and mutate:
            args = json.dumps({"space": "concept", "node_type": "Concept", "node_id": self.nonce})
            return {"type": "tool_call", "tool": mutate, "args": args, "call_id": "call-e2e-" + self.nonce}
        return {"type": "text"}

    # --- 수명 ---
    def start(self, port: int = 0) -> int:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # 표준 접근 로그를 끈다
                pass

            def _send(self, code: int, payload: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    body = {"object": "list",
                            "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "local"}]}
                    self._send(200, json.dumps(body).encode())
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                provider.log("request", body)
                action = provider.decide(body)
                provider.log("decision", action)
                if body.get("stream"):
                    self._stream(action)
                else:
                    self._json(action)

            # --- 응답 조립 ---
            def _message(self, action):
                if action["type"] == "tool_call":
                    return ({"role": "assistant", "content": None,
                             "tool_calls": [{"id": action["call_id"], "type": "function",
                                             "function": {"name": action["tool"],
                                                          "arguments": action["args"]}}]},
                            "tool_calls")
                return ({"role": "assistant",
                         "content": "E2E driver: requested tool calls issued and results received."},
                        "stop")

            def _json(self, action):
                msg, finish = self._message(action)
                body = {"id": "chatcmpl-e2e", "object": "chat.completion",
                        "created": int(time.time()), "model": MODEL_ID,
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
                provider.log("response", body)
                self._send(200, json.dumps(body).encode())

            def _stream(self, action):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                created = int(time.time())

                def chunk(delta, finish=None):
                    payload = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk",
                               "created": created, "model": MODEL_ID,
                               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    data = ("data: " + json.dumps(payload) + "\n\n").encode()
                    self.wfile.write(hex(len(data))[2:].encode() + b"\r\n" + data + b"\r\n")

                if action["type"] == "tool_call":
                    chunk({"role": "assistant", "content": None,
                           "tool_calls": [{"index": 0, "id": action["call_id"], "type": "function",
                                           "function": {"name": action["tool"], "arguments": ""}}]})
                    chunk({"tool_calls": [{"index": 0, "function": {"arguments": action["args"]}}]})
                    chunk({}, "tool_calls")
                else:
                    msg, _ = self._message(action)
                    chunk({"role": "assistant", "content": msg["content"]})
                    chunk({}, "stop")
                done = b"data: [DONE]\n\n"
                self.wfile.write(hex(len(done))[2:].encode() + b"\r\n" + done + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                provider.log("response", {"streamed": action})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self._httpd.server_address[1]

    def stop(self) -> None:
        """소켓과 스레드까지 회수한다. shutdown() 만으로는 소켓이 남는다."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def __enter__(self) -> DeterministicProvider:
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


# --------------------------------------------------------------------------
# 사후 독립 조회
# --------------------------------------------------------------------------


def check_persisted(real_bin: str, plugin_data: str | os.PathLike, node_id: str,
                    timeout: float = 60.0) -> dict:
    """별도 프로세스로 같은 스토어에 새 stdio 세션을 열어 노드 실재를 확인한다.

    하네스가 기동 경로에 남긴 것이 없는 상태에서도 성립해야 하는 검사다.
    난수 노드가 실행 전 없고 실행 후 있으면, 그 상태 변화를 만든 주체는
    클라이언트의 실제 `tools/call` 뿐이다.
    """
    plugin_data = str(plugin_data)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": plugin_data,
        "STORAGE_MODE": "local",
        "LOCAL_DATA_DIR": plugin_data,
        "LOCALCRAB_ENV_FILE": os.path.join(plugin_data, "localcrab.env"),
        "OPENCRAB_BOOTSTRAP_ON_EMPTY": "1",
    }
    # 세 메시지를 한 번에 보내고 communicate 로 받는다. readline 을 쓰면 서버가
    # 기동 중 멈췄을 때 timeout 이 걸리지 않아 무한 대기한다.
    #
    # 의도적으로 깎은 모서리: `initialize` 응답을 기다리지 않고 파이프라인으로
    # 보낸다. 이 서버는 stdin 을 줄 단위로 순차 처리하므로 성립하고 매 실행에서
    # 성립을 확인한다. 초기화 응답 수신을 엄격히 요구하는 서버를 상대하려면
    # 순차 읽기(방향별 리더 스레드 + 남은 시간 예산)로 올려야 한다 -- 이 함수는
    # 범용 MCP 클라이언트가 아니라 같은 저장소 서버에 대한 사후 조회 도구다.
    script = "".join(json.dumps(msg) + "\n" for msg in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "openclaw-e2e-readback", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": READBACK_TOOL, "arguments": {"node_id": node_id}}},
    ))
    proc = subprocess.Popen([os.path.abspath(real_bin), "serve"], cwd=plugin_data, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        out, _ = proc.communicate(input=script, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        # communicate() 를 다시 부르면 안 된다. 서버가 자식을 남기고 죽으면 그
        # 자식이 파이프를 붙들고 있어 EOF 가 오지 않고 또 무한 대기한다.
        # 파이프를 직접 닫고 죽은 프로세스만 회수한다.
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return {"found": False, "error": f"readback timed out after {timeout}s"}

    reply = {}
    for line in out.splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict) and frame.get("id") == 2:
            reply = frame
            break

    text = ((reply.get("result") or {}).get("content") or [{}])[0].get("text", "")
    try:
        return json.loads(text)
    except ValueError:
        return {"found": False, "raw": text}
