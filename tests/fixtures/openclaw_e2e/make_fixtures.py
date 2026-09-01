#!/usr/bin/env python3
"""이 디렉터리의 픽스처를 러너 스크래치에서 다시 만든다 -- 편집의 재현 가능한 정본.

픽스처는 손으로 지어낸 것이 아니라 실제 실행 캡처이며, 원문에서 아래 **네 가지
변환만** 적용했다. 변환을 코드로 남기는 이유는, 문서로만 서술하면 실제 편집과
어긋나도 아무도 눈치채지 못하기 때문이다.

1. 실행별 난수를 고정값 `FIXTURE_NONCE` 로 치환한다(파일 전량, 부분 문자열 포함).
2. 스토어 신원 식별자(`pack_id`, `owner_id`, `receipt_id`)를 고정 placeholder 로
   치환한다. 저장소에 남길 값이 아니다.
3. `tools/list` 응답과 provider 요청의 도구 목록을 판정에 쓰이는 두 개만 남긴다.
   나머지는 크기만 차지한다.
4. 난수를 포함하지 않는 긴 본문(정적 manifest 결과)을 앞부분만 남기고 자른다.

이벤트 종류와 개수, JSON-RPC id 대응, 메시지 역할 구성은 **줄이지 않는다.**
판정기가 읽지 않는 필드라도 형상이 실제와 달라지면 픽스처가 회귀 대상으로서
가치를 잃는다.

사용:
  python scripts/verify_openclaw_e2e.py ... --scratch <S> --keep-scratch
  python tests/fixtures/openclaw_e2e/make_fixtures.py --scratch <S> --nonce <그 실행의 난수>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE_NONCE = "e2e-nonce-0000000000000000"
KEEP_TOOLS = {"ontology_manifest", "ontology_add_node"}
TRUNCATE_OVER = 400
IDENTITY_PATTERNS = [
    (re.compile(r'default-[0-9a-f]{12,}'), "default-fixturepack0000"),
    (re.compile(r'user_[0-9a-f]{8,}'), "user_fixture0000"),
    (re.compile(r'rcpt_[0-9a-f]{8,}'), "rcpt_fixture0000"),
]


def scrub(text: str, nonce: str) -> str:
    """변환 1 과 2. 난수는 파생 문자열(call id 등) 안에서도 지워야 한다."""
    text = text.replace(nonce, FIXTURE_NONCE)
    bare = nonce.replace("-", "")
    if bare != nonce:
        text = text.replace(bare, FIXTURE_NONCE.replace("-", ""))
    for pattern, replacement in IDENTITY_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def shrink_tools(tools: list) -> list:
    """변환 3."""
    out = []
    for tool in tools:
        name = tool.get("name") or (tool.get("function") or {}).get("name") or ""
        if name in KEEP_TOOLS or name.split("__")[-1] in KEEP_TOOLS:
            out.append(tool)
    return out


def truncate(text: str, protect_nonce: bool = False) -> str:
    """변환 4. 도구 결과 본문만 난수 보호 대상이다.

    `protect_nonce` 를 모든 본문에 적용하면 안 된다: 클라이언트가 세션 식별자
    따위를 시스템 프롬프트에 실어 난수가 거기 섞이면, 36KB 프롬프트가 통째로
    보호 대상이 되어 픽스처가 쓸데없이 커진다.
    """
    if len(text) > TRUNCATE_OVER and not (protect_nonce and FIXTURE_NONCE in text):
        return text[:200] + '..."}'
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True, help="러너가 --keep-scratch 로 남긴 디렉터리")
    ap.add_argument("--nonce", required=True, help="그 실행의 난수 (러너가 첫 줄에 출력한다)")
    args = ap.parse_args()
    scratch = Path(args.scratch).resolve()

    c2s = scrub((scratch / "record" / "client_to_server.raw").read_text(encoding="utf-8"), args.nonce)
    (HERE / "client_to_server.raw").write_text(c2s, encoding="utf-8")

    lines = []
    for line in (scratch / "record" / "server_to_client.raw").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(scrub(line, args.nonce))
        result = obj.get("result")
        if isinstance(result, dict):
            if isinstance(result.get("tools"), list):
                result["tools"] = shrink_tools(result["tools"])
            for item in (result.get("content") or []):
                if isinstance(item.get("text"), str):
                    item["text"] = truncate(item["text"], protect_nonce=True)
        lines.append(json.dumps(obj, ensure_ascii=False))
    (HERE / "server_to_client.raw").write_text("\n".join(lines) + "\n", encoding="utf-8")

    events = []
    for line in (scratch / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(scrub(line, args.nonce))
        payload = event.get("payload") or {}
        if isinstance(payload.get("tools"), list):
            payload["tools"] = shrink_tools(payload["tools"])
        for msg in (payload.get("messages") or []):
            if isinstance(msg.get("content"), str):
                msg["content"] = truncate(msg["content"], protect_nonce=msg.get("role") == "tool")
        events.append(json.dumps(event, ensure_ascii=False))
    (HERE / "provider.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")

    for path in sorted(HERE.glob("*.raw")) + [HERE / "provider.jsonl"]:
        print(f"{path.name:24} {path.stat().st_size:7} bytes  {len(path.read_text().splitlines()):3} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
