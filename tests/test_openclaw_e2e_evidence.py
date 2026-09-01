"""#249: 실 클라이언트 종단 검증 하네스의 **증거 판정기** 회귀 테스트.

하네스 전체는 실 클라이언트 설치와 node 런타임을 요구해 CI 에서 돌지 않는다.
판정기 `verify_evidence()` 는 순수 함수이므로 CI 에서 회귀를 잡을 수 있다.

`tests/fixtures/openclaw_e2e/` 의 세 파일은 실 클라이언트 실행에서 캡처한 것이며,
적용한 편집은 같은 디렉터리의 `make_fixtures.py` 가 정본이다(난수 치환, 스토어
신원 식별자 치환, 도구 목록 축소, 난수 없는 긴 본문 절단 -- 그 넷뿐). 이벤트
종류와 개수, JSON-RPC id 대응, 메시지 역할 구성은 줄이지 않았다.

판정기가 무엇을 검출하는지는 **역변이**로 증명한다: 각 산출물을 한 번씩 훼손하면
대응하는 검사가 반드시 실패해야 한다. 그렇지 않으면 그 검사는 공허하다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_PARENT = REPO_ROOT / "packaging" / "agent-plugin"
if str(_TOOLS_PARENT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_PARENT))

from tools.openclaw_e2e import (  # noqa: E402  (sys.path 삽입 후 의도된 임포트 순서)
    MUTATING_TOOL,
    PROBE_TOOL,
    match_tool,
    new_nonce,
    verify_evidence,
    write_recorder_shim,
)

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "openclaw_e2e"
FIXTURE_NONCE = "e2e-nonce-0000000000000000"

ALL_CHECKS = [
    "provider_issued_nonce_call",
    "provider_call_is_the_mutating_tool",
    "provider_received_nonce_result",
    "boundary_recorded_as_expected",
    "boundary_tools_call_carries_nonce",
    "boundary_response_echoes_nonce",
    "called_tool_was_advertised",
    "boundary_call_is_the_mutating_tool",
]


def _load() -> dict:
    return {
        "nonce": FIXTURE_NONCE,
        "client_to_server": (FIXTURES / "client_to_server.raw").read_text(encoding="utf-8"),
        "server_to_client": (FIXTURES / "server_to_client.raw").read_text(encoding="utf-8"),
        "provider_log": (FIXTURES / "provider.jsonl").read_text(encoding="utf-8"),
    }


def _failed(verdict) -> set[str]:
    return {c.name for c in verdict.checks if not c.passed}


# --------------------------------------------------------------------------
# 정상 경로
# --------------------------------------------------------------------------


def test_captured_run_passes_every_check():
    verdict = verify_evidence(**_load())
    assert verdict.passed, verdict.render()
    assert [c.name for c in verdict.checks] == ALL_CHECKS


def test_recorder_absent_mode_is_declared_not_inferred():
    """기록기를 뺐다고 **선언한** 실행만 경계 검사를 건너뛴다."""
    data = _load() | {"client_to_server": "", "server_to_client": "", "expect_boundary": False}
    verdict = verify_evidence(**data)
    assert verdict.passed, verdict.render()
    assert [c.name for c in verdict.checks][-1] == "boundary_recorded_as_expected"


# --------------------------------------------------------------------------
# fail-open 회귀 -- 이 단위에서 실제로 발견된 결함
# --------------------------------------------------------------------------


def test_missing_boundary_when_expected_is_a_failure():
    """기록기를 태웠다고 했는데 원문이 비면 실패다.

    이것을 통과시키면 판정이 fail-open 이 된다: PATH 해석이 어긋나 기록기가
    개입하지 못한 실행과, 애초에 기록기를 뺀 실행이 구별되지 않아 전자가 조용히
    통과한다. 두 실행은 증명하는 것이 다르므로 같은 판정을 받아선 안 된다.
    """
    data = _load() | {"client_to_server": "", "server_to_client": ""}
    verdict = verify_evidence(**data)   # expect_boundary 기본값 True
    assert not verdict.passed, verdict.render()
    assert "boundary_recorded_as_expected" in _failed(verdict)


def test_partial_boundary_capture_does_not_silently_downgrade():
    """말미가 잘린 부분 캡처도 '기록기 없음'으로 강등되지 않는다."""
    data = _load()
    data["client_to_server"] = "\n".join(data["client_to_server"].splitlines()[:2]) + "\n"
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "boundary_tools_call_carries_nonce" in _failed(verdict)
    assert "boundary_recorded_as_expected" not in _failed(verdict)


# --------------------------------------------------------------------------
# 역변이 -- 각 검사의 검출력을 개별로 증명한다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact", "expected_failure"),
    [
        ("client_to_server", "boundary_tools_call_carries_nonce"),
        ("server_to_client", "boundary_response_echoes_nonce"),
    ],
)
def test_corrupting_the_nonce_fails_the_matching_boundary_check(artifact, expected_failure):
    data = _load()
    data[artifact] = data[artifact].replace(FIXTURE_NONCE, "e2e-nonce-ffffffffffffffff")
    verdict = verify_evidence(**data)
    assert not verdict.passed, f"난수를 훼손했는데도 통과했다:\n{verdict.render()}"
    assert expected_failure in _failed(verdict)


def test_provider_issue_check_mutation_cascades_by_design():
    """decision 이벤트를 훼손하면 발행 검사가 실패하고, 수신 검사도 따라 실패한다.

    수신 검사가 발행한 `call_id` 에 묶여 있으므로 이 연쇄는 설계된 의존이다.
    수신 검사의 **독립** 검출력은 아래 역방향 변이가 따로 증명한다.
    """
    data = _load()
    out = []
    for line in data["provider_log"].splitlines():
        event = json.loads(line)
        if event.get("kind") == "decision":
            line = line.replace(FIXTURE_NONCE, "e2e-nonce-ffffffffffffffff")
        out.append(line)
    data["provider_log"] = "\n".join(out) + "\n"
    verdict = verify_evidence(**data)
    failed = _failed(verdict)
    assert "provider_issued_nonce_call" in failed
    assert "provider_received_nonce_result" in failed, "call_id 결속이 끊겼다"


def test_provider_receipt_check_is_detected_independently():
    """role=tool 본문만 훼손한다 -- 발행 검사는 건드리지 않는다.

    이 방향이 수신 검사의 독립 검출력을 증명한다.
    """
    data = _load()
    out = []
    for line in data["provider_log"].splitlines():
        event = json.loads(line)
        changed = False
        for msg in ((event.get("payload") or {}).get("messages") or []):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
                msg["content"] = msg["content"].replace(FIXTURE_NONCE, "e2e-nonce-ffffffffffffffff")
                changed = True
        out.append(json.dumps(event, ensure_ascii=False) if changed else line)
    data["provider_log"] = "\n".join(out) + "\n"
    verdict = verify_evidence(**data)
    failed = _failed(verdict)
    assert "provider_received_nonce_result" in failed
    assert "provider_issued_nonce_call" not in failed, "발행 검사가 함께 훼손됐다 -- 독립 검출이 아니다"


def test_tool_result_from_a_different_call_is_rejected():
    """난수를 담았어도 발행한 call_id 에 대응하지 않는 tool 결과는 인정하지 않는다."""
    data = _load()
    out = []
    for line in data["provider_log"].splitlines():
        event = json.loads(line)
        for msg in ((event.get("payload") or {}).get("messages") or []):
            if msg.get("role") == "tool" and FIXTURE_NONCE in str(msg.get("content")):
                msg["tool_call_id"] = "some-unrelated-call-id"
        out.append(json.dumps(event, ensure_ascii=False))
    data["provider_log"] = "\n".join(out) + "\n"
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "provider_received_nonce_result" in _failed(verdict)


def test_nonce_in_a_non_node_id_argument_is_rejected():
    """난수가 arguments 어딘가에 있기만 한 호출은 인정하지 않는다."""
    data = _load()
    data["client_to_server"] = data["client_to_server"].replace(
        f'"node_id":"{FIXTURE_NONCE}"', f'"node_id":"other","note":"{FIXTURE_NONCE}"'
    )
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "boundary_tools_call_carries_nonce" in _failed(verdict)


def test_declaring_no_recorder_while_boundary_exists_is_rejected():
    """기록기를 뺐다고 선언했는데 경계 원문이 있으면 모순이므로 기각한다."""
    data = _load() | {"expect_boundary": False}
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "boundary_recorded_as_expected" in _failed(verdict)


def test_synthesized_result_without_a_real_call_is_rejected():
    """경계에 tools/call 이 없는데 provider 만 결과를 받은 반례를 기각한다.

    이것이 판정기가 막아야 하는 핵심 반례다: 클라이언트가 실제 호출을 보내지
    않고 결과를 합성해 tool 메시지에 넣는 경우.
    """
    data = _load()
    data["client_to_server"] = "\n".join(
        line for line in data["client_to_server"].splitlines() if FIXTURE_NONCE not in line
    ) + "\n"
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "boundary_tools_call_carries_nonce" in _failed(verdict)


def test_calling_an_unadvertised_tool_is_rejected():
    data = _load()
    data["server_to_client"] = data["server_to_client"].replace(
        f'"name": "{MUTATING_TOOL}"', f'"name": "{MUTATING_TOOL}_DIFFERENT"'
    )
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "called_tool_was_advertised" in _failed(verdict)


def test_static_tool_only_run_is_rejected():
    """정적 무인자 도구를 난수와 함께 부른 것처럼 꾸며도 기각한다.

    변경 연산이 아니면 '결과를 합성해 넣었다'는 반례가 되살아난다.
    """
    data = _load()
    data["client_to_server"] = data["client_to_server"].replace(
        f'"name":"{MUTATING_TOOL}"', f'"name":"{PROBE_TOOL}"'
    )
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "boundary_call_is_the_mutating_tool" in _failed(verdict)


def test_provider_call_naming_a_non_mutating_tool_is_rejected():
    data = _load()
    out = []
    for line in data["provider_log"].splitlines():
        event = json.loads(line)
        payload = event.get("payload") or {}
        if event.get("kind") == "decision" and FIXTURE_NONCE in (payload.get("args") or ""):
            payload["tool"] = f"localcrab__{PROBE_TOOL}"
        out.append(json.dumps(event, ensure_ascii=False))
    data["provider_log"] = "\n".join(out) + "\n"
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "provider_call_is_the_mutating_tool" in _failed(verdict)


# --------------------------------------------------------------------------
# 픽스처 신선도 -- 커밋된 하네스가 만들 수 없는 값이 박히는 것을 막는다
# --------------------------------------------------------------------------


def test_fixture_call_ids_match_the_committed_harness():
    """픽스처가 현재 하네스의 산물인지 확인한다.

    하네스가 바뀌었는데 픽스처가 그대로면, CI 가 보는 유일한 증거물이 실제
    산출물과 어긋난 채 굳는다. 실제로 그 상태로 커밋된 적이 있다.
    """
    log = (FIXTURES / "provider.jsonl").read_text(encoding="utf-8")
    call_ids = {
        (json.loads(line).get("payload") or {}).get("call_id")
        for line in log.splitlines()
        if json.loads(line).get("kind") == "decision"
    } - {None}
    assert call_ids == {"call-e2e-probe", f"call-e2e-{FIXTURE_NONCE}"}, call_ids


def test_fixture_preserves_real_event_shape():
    """축약이 이벤트 형상까지 지우지 않았는지 확인한다."""
    kinds = [json.loads(line)["kind"] for line in
             (FIXTURES / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    assert kinds == ["request", "decision", "response"] * 3, kinds


def test_fixture_carries_no_environment_identifiers():
    data_files = sorted(FIXTURES.glob("*.raw")) + [FIXTURES / "provider.jsonl"]
    assert len(data_files) == 3, [p.name for p in data_files]
    for path in data_files:
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text, path.name
        for leaked in ("rcpt_", "user_", "default-"):
            for token in text.split(leaked)[1:]:
                assert token.startswith("fixture"), f"{path.name}: {leaked}{token[:24]}"


# --------------------------------------------------------------------------
# 보조 단위
# --------------------------------------------------------------------------


def test_match_tool_ignores_client_namespace_prefix():
    assert match_tool([f"localcrab__{PROBE_TOOL}"], PROBE_TOOL) == f"localcrab__{PROBE_TOOL}"
    assert match_tool([PROBE_TOOL], PROBE_TOOL) == PROBE_TOOL


def test_match_tool_refuses_ambiguous_and_missing():
    """후보가 0개거나 2개 이상이면 고르지 않는다 -- 임의 선택은 판정을 모호하게 만든다."""
    assert match_tool([], PROBE_TOOL) is None
    assert match_tool([f"a__{PROBE_TOOL}", f"b__{PROBE_TOOL}"], PROBE_TOOL) is None
    assert match_tool([f"{PROBE_TOOL}_extended"], PROBE_TOOL) is None


def test_new_nonce_is_unique_and_node_id_safe():
    a, b = new_nonce(), new_nonce()
    assert a != b
    assert all(ch.isalnum() or ch == "-" for ch in a), a


def test_recorder_shim_source_is_syntactically_valid(tmp_path):
    """생성한 기록기가 실제로 컴파일되는지 확인한다.

    기록기는 클라이언트가 서버로 띄우는 실행 파일이다. 여기서 나는 어떤 오류든
    `failed to start server ... Connection closed` 라는 **서버 기동 실패로만**
    보이므로, 원인이 하네스에 있다는 사실이 증상에 전혀 드러나지 않는다.
    이 단위에서 두 번(환경 변수 접근, 소스 이스케이프) 실제로 겪은 함정이다.
    """
    import py_compile

    write_recorder_shim(tmp_path / "shim", "/opt/somewhere/opencrab", tmp_path / "rec")
    py_compile.compile(str(tmp_path / "shim" / "opencrab_recorder.py"), doraise=True)


def test_recorder_shim_bakes_paths_as_constants(tmp_path):
    """기록기는 경로를 환경 변수로 읽으면 안 된다.

    클라이언트가 MCP 서버 자식의 환경을 소독하므로, 환경 변수로 받으면 자식에서
    즉사하고 증상이 서버 기동 실패로만 보인다(#249 에서 실제로 겪은 함정).
    """
    launcher = write_recorder_shim(tmp_path / "shim", "/opt/somewhere/opencrab", tmp_path / "rec")
    source = (tmp_path / "shim" / "opencrab_recorder.py").read_text(encoding="utf-8")
    assert launcher.exists() and launcher.stat().st_mode & 0o111
    assert "/opt/somewhere/opencrab" in source
    assert str(tmp_path / "rec") in source
    assert "os.environ[" not in source
    assert "environ.get" not in source
