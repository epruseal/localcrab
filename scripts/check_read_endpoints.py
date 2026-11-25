#!/usr/bin/env python3
"""실행 중인 localcrab 인스턴스 하나에 읽기 질의만 보내 전환 뒤 읽기를 검증한다 (#710).

단일 DB 전환 실행표(docs/issue-671-single-db-conversion.md 8.9절)의 임시 인스턴스 읽기 검증과
문을 연 뒤 검사에 쓴다. 쓰기 도구와 쓰기 경로를 부르지 않는다.

사용법:
    python scripts/check_read_endpoints.py <포트> mcp|api (--token-env <변수> | --client-config <파일> [--server <이름>])
        [--host 127.0.0.1] [--allow-remote] [--save-expected <파일> | --expected <파일>]

토큰은 이 프로세스 안에서 Authorization 헤더로만 보낸다. 출력, 오류 문자열, 파일, 질의 문자열에 넣지 않는다.
리디렉션은 따라가지 않고 프록시 환경변수는 읽지 않는다(헤더가 다른 곳으로 새는 길을 막는다). 호스트가 루프백이 아니면
`--allow-remote` 없이 거부한다(토큰이 평문으로 나가지 않게 한다).

mcp 형태는 `_meta` 없는 단순 `tools/call` 요청(레거시 경로)을 보낸다. 이 경로는 도구 오류를 HTTP 200에
`{"error": ...}` 본문으로 돌려주고 `isError` 키를 넣지 않는다. 그래서 JSON-RPC `error` 봉투, 본문의 최상위
`error` 키, `isError` 참을 모두 실패로 읽는다.
`ontology_query`는 billing 행을 더하고 `POST /api/query`는 질의 이벤트를 쓴다. 이 도구는 팩 데이터를 바꾸는
호출을 보내지 않는다.
mcp 형태는 `POST /mcp`로 `ontology_query`(토큰 있음, 없음)와 `content_pack_list`를 부른다.
api 형태는 `GET /api/packs`(토큰 있음, 없음), `GET /api/status`, `POST /api/query`를 부른다.
출력은 HTTP 상태, 결과 건수, isError, 팩 id 목록, 기대 집합과의 두 차집합이다.
`--save-expected`는 합격한 실행에서만 팩 id 목록을 한 줄에 하나씩 파일에 쓴다(덮어쓰지 않는다).
`--expected`는 그런 파일을 읽어 대조한다. 기대에 있고 응답에 없는 id가 하나라도 있으면 실패다.

종료 코드: 0 합격. 1 검사 실패(토큰 질의가 200이 아니거나 isError, 무토큰 질의가 401이 아님, 기대 대비 누락).
2 사용법이나 설정 오류(토큰의 제어 문자, `--expected` 파일 읽기 실패, `--save-expected`의 상위 디렉터리 없음이나 쓰기 실패 포함).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_TIMEOUT = 180


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# 프록시 환경변수를 읽지 않는다(토큰이 프록시로 새거나 검사 대상이 프록시로 바뀌는 길을 막는다).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _bearer(value: str) -> str:
    value = value.strip()
    # 제어 문자와 비ASCII 문자는 http.client가 예외 메시지에 헤더 값을 넣는 길이다. 네트워크 호출 전에 거부한다.
    if not value.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("토큰에 제어 문자나 비ASCII 문자가 있다")
    return value if value.lower().startswith("bearer ") else "Bearer " + value


def _read_token(args: argparse.Namespace) -> str:
    if args.token_env:
        value = os.environ.get(args.token_env, "")
        if not value.strip():
            raise ValueError(f"환경변수 {args.token_env}이 비어 있거나 없다")
        return _bearer(value)
    try:
        config = json.loads(Path(args.client_config).read_text(encoding="utf-8"))
        value = config["mcpServers"][args.server]["headers"]["Authorization"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"클라이언트 설정에서 토큰을 읽지 못했다: {type(exc).__name__}") from exc
    if not isinstance(value, str) or not value.strip():
        raise ValueError("클라이언트 설정의 Authorization 값이 문자열이 아니거나 비어 있다")
    return _bearer(value)


def _call(base: str, method: str, path: str, body: dict | None, auth: str | None):
    """HTTP 호출 하나. 상태 코드(문자열이면 연결 실패 유형)와 본문을 돌려준다."""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # 메시지에 요청 정보가 들 수 있어 유형 이름만 남긴다.
        return "ERR:" + type(exc).__name__, b""


def _parse(raw: bytes):
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("data:") or "\ndata:" in text or text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    try:
        return json.loads(text)
    except ValueError:
        return None


def _mcp_tool(base: str, name: str, arguments: dict, request_id: int, auth: str | None) -> dict:
    body = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    code, raw = _call(base, "POST", "/mcp", body, auth)
    out = {"http": code, "isError": None, "inner": None}
    payload = _parse(raw) if code == 200 else None
    if isinstance(payload, dict):
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        out["isError"] = result.get("isError")
        content = result.get("content")
        content = content if isinstance(content, list) else []
        text = "".join(c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str))
        try:
            out["inner"] = json.loads(text) if text else None
        except ValueError:
            out["inner"] = None
        if payload.get("error") is not None:
            out["isError"] = True
    return out


def _pack_ids(rows) -> list[str]:
    return sorted(str(row.get("pack_id")) for row in rows if isinstance(row, dict))


def _check_mcp(base: str, auth: str) -> tuple[bool, list[str] | None]:
    ok = True
    query = _mcp_tool(base, "ontology_query", {"question": "direct", "limit": 10, "offset": 0}, 1, auth)
    inner = query["inner"] if isinstance(query["inner"], dict) else {}
    print("ontology_query token: http", query["http"], "isError", query["isError"],
          "results", len(inner["results"]) if isinstance(inner.get("results"), list) else None,
          "error_key", "error" in inner)
    ok &= (query["http"] == 200 and not query["isError"] and "error" not in inner
           and isinstance(inner.get("results"), list))
    anonymous = _mcp_tool(base, "ontology_query", {"question": "direct", "limit": 10, "offset": 0}, 2, None)
    print("ontology_query no_token: http", anonymous["http"])
    ok &= anonymous["http"] == 401
    packs = _mcp_tool(base, "content_pack_list", {}, 3, auth)
    inner = packs["inner"] if isinstance(packs["inner"], dict) else {}
    rows = inner.get("packs")
    print("content_pack_list token: http", packs["http"], "isError", packs["isError"],
          "packs", len(rows) if isinstance(rows, list) else None)
    ok &= (packs["http"] == 200 and not packs["isError"] and "error" not in inner
           and isinstance(rows, list))
    return ok, _pack_ids(rows) if isinstance(rows, list) else None


def _check_api(base: str, auth: str) -> tuple[bool, list[str] | None]:
    ok = True
    code, raw = _call(base, "GET", "/api/packs", None, auth)
    payload = _parse(raw) if code == 200 else None
    rows = payload.get("packs") if isinstance(payload, dict) else None
    print("GET /api/packs token: http", code, "packs", len(rows) if isinstance(rows, list) else None)
    ok &= code == 200 and isinstance(rows, list) and "error" not in (payload or {})
    anonymous = _call(base, "GET", "/api/packs", None, None)[0]
    print("GET /api/packs no_token: http", anonymous)
    ok &= anonymous == 401
    status = _call(base, "GET", "/api/status", None, auth)[0]
    print("GET /api/status token: http", status)
    ok &= status == 200
    code, raw = _call(base, "POST", "/api/query", {"question": "direct", "limit": 10}, auth)
    payload = _parse(raw) if code == 200 else None
    results = payload.get("results") if isinstance(payload, dict) else None
    print("POST /api/query token: http", code, "results", len(results) if isinstance(results, list) else None)
    ok &= code == 200 and isinstance(results, list)
    return ok, _pack_ids(rows) if isinstance(rows, list) else None


def _read_expected(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"--expected 파일을 읽지 못했다: {type(exc).__name__}") from exc
    return [line.strip() for line in text.splitlines() if line.strip()]


def run(base: str, shape: str, auth: str, *, expected: Path | None, save_expected: Path | None) -> int:
    ok, ids = (_check_mcp if shape == "mcp" else _check_api)(base, auth)
    if ids is None:
        print("pack_ids: 없음(팩 목록 응답을 읽지 못했다)")
        return 1
    print(f"pack_ids ({len(ids)}):", " ".join(ids))
    if save_expected is not None and ok:
        created = False
        try:
            with open(save_expected, "x", encoding="utf-8") as handle:
                created = True  # 이 실행이 만든 파일만 지운다.
                handle.write("\n".join(ids) + "\n")
        except OSError as exc:
            if created:
                try:
                    os.unlink(save_expected)  # 빈 기대 파일이 남으면 이후 누락 검사가 의미 없이 통과한다.
                except OSError:
                    pass
            print(f"ERROR: 기대 목록을 쓰지 못했다: {type(exc).__name__}", file=sys.stderr)
            return 2
        print("expected saved:", save_expected)
    if expected is not None:
        try:
            wanted = _read_expected(expected)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        missing = sorted(set(wanted) - set(ids))
        extra = sorted(set(ids) - set(wanted))
        print("expected_minus_got:", missing)
        print("got_minus_expected:", extra)
        ok &= not missing
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="localcrab 인스턴스 하나에 읽기 질의만 보내 검증한다.")
    parser.add_argument("port", type=int)
    parser.add_argument("shape", choices=("mcp", "api"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--allow-remote", action="store_true", help="루프백이 아닌 호스트를 허용한다")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--token-env", help="토큰이 든 환경변수 이름")
    source.add_argument("--client-config", help="mcpServers.<이름>.headers.Authorization이 있는 JSON 파일")
    parser.add_argument("--server", default="localcrab", help="--client-config의 서버 이름")
    saving = parser.add_mutually_exclusive_group()
    saving.add_argument("--save-expected", type=Path)
    saving.add_argument("--expected", type=Path)
    args = parser.parse_args(argv)
    try:
        if not 0 < args.port < 65536:
            raise ValueError("포트 범위가 맞지 않다")
        if not _is_loopback(args.host) and not args.allow_remote:
            raise ValueError("루프백이 아닌 호스트는 --allow-remote 없이 거부한다")
        if args.expected is not None:
            _read_expected(args.expected)  # 네트워크 호출 전에 읽고 디코드해 본다.
        if args.save_expected is not None:
            if args.save_expected.exists():
                raise ValueError("--save-expected 파일이 이미 있다")
            if not args.save_expected.parent.is_dir():
                raise ValueError("--save-expected의 상위 디렉터리가 없다")
        auth = _read_token(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    host = f"[{args.host}]" if ":" in args.host else args.host
    return run(f"http://{host}:{args.port}", args.shape, auth,
               expected=args.expected, save_expected=args.save_expected)


if __name__ == "__main__":
    sys.exit(main())
