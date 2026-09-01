"""#249: 실 클라이언트 종단 검증 하네스의 **증거 판정기** 회귀 테스트.

하네스 전체는 실 OpenClaw 설치와 node 런타임을 요구해 CI 에서 돌지 않는다.
그러나 판정기 `verify_evidence()` 는 순수 함수이므로, 실제 실행에서 캡처한
픽스처를 먹여 CI 에서 회귀를 잡을 수 있다.

`tests/fixtures/openclaw_e2e/` 의 세 파일은 실 OpenClaw 2026.8.1 실행에서
그대로 캡처한 것이다(난수만 고정값으로 치환하고, 정적 manifest 본문과 무관한
도구 목록은 크기를 줄였다). 판정기가 무엇을 검출하는지는 **역변이**로 증명한다:
각 산출물에서 난수를 한 번씩 훼손하면 대응하는 검사가 반드시 실패해야 한다.
그렇지 않으면 그 검사는 공허하다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_PARENT = REPO_ROOT / "packaging" / "agent-plugin"
if str(_TOOLS_PARENT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_PARENT))

from tools.openclaw_e2e import (  # noqa: E402  (sys.path 삽입 후 의도된 임포트 순서)
    match_tool,
    new_nonce,
    verify_evidence,
    write_recorder_shim,
)

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "openclaw_e2e"
FIXTURE_NONCE = "e2e-nonce-0000000000000000"


def _load() -> dict:
    return {
        "nonce": FIXTURE_NONCE,
        "client_to_server": (FIXTURES / "client_to_server.raw").read_text(encoding="utf-8"),
        "server_to_client": (FIXTURES / "server_to_client.raw").read_text(encoding="utf-8"),
        "provider_log": (FIXTURES / "provider.jsonl").read_text(encoding="utf-8"),
    }


# --------------------------------------------------------------------------
# 정상 경로
# --------------------------------------------------------------------------


def test_captured_run_passes_every_check():
    """실제 실행에서 캡처한 증거는 전량 통과해야 한다."""
    verdict = verify_evidence(**_load())
    assert verdict.passed, verdict.render()
    names = [c.name for c in verdict.checks]
    assert names == [
        "provider_issued_nonce_call",
        "provider_received_nonce_result",
        "boundary_tools_call_carries_nonce",
        "boundary_response_echoes_nonce",
        "called_tool_was_advertised",
    ]


def test_boundary_absent_run_still_binds_provider_side():
    """기록기 없이 돈 실행은 경계 검사를 건너뛰되 provider 측은 그대로 판정한다."""
    data = _load()
    data["client_to_server"] = ""
    data["server_to_client"] = ""
    verdict = verify_evidence(**data)
    assert verdict.passed, verdict.render()
    assert [c.name for c in verdict.checks][-1] == "boundary_recorder_absent"


# --------------------------------------------------------------------------
# 역변이 -- 판정기가 실제로 무엇을 검출하는지 증명한다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact", "expected_failure"),
    [
        ("client_to_server", "boundary_tools_call_carries_nonce"),
        ("server_to_client", "boundary_response_echoes_nonce"),
        ("provider_log", "provider_issued_nonce_call"),
    ],
)
def test_corrupting_the_nonce_fails_the_matching_check(artifact, expected_failure):
    """한 산출물에서 난수를 훼손하면 대응하는 검사가 실패해야 한다."""
    data = _load()
    data[artifact] = data[artifact].replace(FIXTURE_NONCE, "e2e-nonce-ffffffffffffffff")
    verdict = verify_evidence(**data)
    assert not verdict.passed, f"난수를 훼손했는데도 통과했다:\n{verdict.render()}"
    failed = {c.name for c in verdict.checks if not c.passed}
    assert expected_failure in failed, f"기대한 검사가 실패하지 않았다: {failed}"


def test_synthesized_result_without_a_real_call_is_rejected():
    """경계에 tools/call 이 없는데 provider 만 결과를 받은 반례를 기각한다.

    이것이 이 판정기가 막아야 하는 핵심 반례다: 클라이언트가 실제 호출을 보내지
    않고 결과를 합성해 tool 메시지에 넣는 경우.
    """
    data = _load()
    data["client_to_server"] = "\n".join(
        line for line in data["client_to_server"].splitlines()
        if FIXTURE_NONCE not in line
    )
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    failed = {c.name for c in verdict.checks if not c.passed}
    assert "boundary_tools_call_carries_nonce" in failed


def test_calling_an_unadvertised_tool_is_rejected():
    """tools/list 로 광고되지 않은 도구를 부른 기록은 기각한다."""
    data = _load()
    data["server_to_client"] = data["server_to_client"].replace(
        '"name": "ontology_add_node"', '"name": "ontology_add_node_DIFFERENT"'
    )
    verdict = verify_evidence(**data)
    assert not verdict.passed, verdict.render()
    assert "called_tool_was_advertised" in {c.name for c in verdict.checks if not c.passed}


# --------------------------------------------------------------------------
# 보조 단위
# --------------------------------------------------------------------------


def test_match_tool_ignores_client_namespace_prefix():
    assert match_tool(["localcrab__ontology_manifest"], "ontology_manifest") == "localcrab__ontology_manifest"
    assert match_tool(["ontology_manifest"], "ontology_manifest") == "ontology_manifest"


def test_match_tool_refuses_ambiguous_and_missing():
    """후보가 0개거나 2개 이상이면 고르지 않는다 -- 임의 선택은 판정을 모호하게 만든다."""
    assert match_tool([], "ontology_manifest") is None
    assert match_tool(["a__ontology_manifest", "b__ontology_manifest"], "ontology_manifest") is None
    assert match_tool(["ontology_manifest_extended"], "ontology_manifest") is None


def test_new_nonce_is_unique_and_node_id_safe():
    a, b = new_nonce(), new_nonce()
    assert a != b
    assert all(ch.isalnum() or ch == "-" for ch in a), a


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
