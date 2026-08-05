"""팩 생산자(opencrab.pack.build) 테스트.

이관 전 이 코드는 opencrab-dump 에 있었고 테스트가 없었다. 계약(schema)과 같은 리포로
옮겨온 목적이 "생산자와 소비자를 한 스위트에서 묶는 것"이므로, 여기서는 생산자가 계약을
지키는지와 이관에서 바뀐 두 가지(출력 루트, remap 함정 검사)를 고정한다.
"""

import inspect
import json
import os

import pytest

from opencrab.pack import build as build_mod
from opencrab.pack.build import Pack
from opencrab.pack.jsonl_io import iter_jsonl
from opencrab.pack.schema import NODE_TYPE_OVERRIDE, PackSchemaError


@pytest.fixture(autouse=True)
def _no_host_default(monkeypatch):
    """호스트 리포(opencrab-dump shim)가 주입하는 기본값에서 테스트를 격리한다."""
    monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", None)
    monkeypatch.delenv("PACK_OUT_ROOT", raising=False)


@pytest.fixture
def pack(tmp_path):
    return Pack("t", "제목", out_root=str(tmp_path))


# ---------------------------------------------------------------------------
# 출력 루트 — 이관에서 바뀐 지점
# ---------------------------------------------------------------------------

class TestOutRoot:
    def test_explicit_out_root(self, tmp_path):
        p = Pack("t", "제목", out_root=str(tmp_path))
        assert p.out == tmp_path / "t"
        assert p.out.is_dir()

    def test_env_out_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PACK_OUT_ROOT", str(tmp_path))
        assert Pack("t", "제목").out == tmp_path / "t"

    def test_explicit_arg_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PACK_OUT_ROOT", str(tmp_path / "env"))
        assert Pack("t", "제목", out_root=str(tmp_path / "arg")).out == tmp_path / "arg" / "t"

    def test_missing_out_root_raises(self):
        """자동 유도는 리포를 넘는 순간 조용히 틀린 곳에 쓴다(mkdir(exist_ok=True) 라 예외도 없다).

        이관 전에는 `Path(__file__).parents[2] / 'by-pack'` 이었고, 이 파일이 localcrab 으로
        온 지금 그 코드가 남아 있었다면 localcrab 리포 안에 by-pack 을 만들었을 것이다.
        """
        with pytest.raises(ValueError, match="출력 루트"):
            Pack("t", "제목")

    def test_empty_env_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("PACK_OUT_ROOT", "")
        with pytest.raises(ValueError, match="출력 루트"):
            Pack("t", "제목")

    def test_host_default_out_root_is_used(self, tmp_path, monkeypatch):
        """호스트 리포가 주입하는 기본값. opencrab-dump shim 이 이 경로로 by-pack 을 준다."""
        monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", str(tmp_path))
        assert Pack("t", "제목").out == tmp_path / "t"

    def test_env_beats_host_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", str(tmp_path / "host"))
        monkeypatch.setenv("PACK_OUT_ROOT", str(tmp_path / "env"))
        assert Pack("t", "제목").out == tmp_path / "env" / "t"

    def test_does_not_write_pack_out_root_into_environ(self, tmp_path, monkeypatch):
        """**회귀 검사.** 한때 shim 이 `os.environ.setdefault('PACK_OUT_ROOT', ...)` 로
        기본값을 넣었다. 그러면 `os.environ.get('PACK_OUT_ROOT')` 로 "호출자가 드라이런을
        지정했는가"를 판정하던 코드가 항상 참이 되어, 운영 빌드가 git 추적 산출물
        (chatgpt/canonical/, *-prep/00_index/) 대신 스크래치 경로로 조용히 새 나갔다.
        exit 0 에 성공 배너까지 나서 아무도 몰랐다(2026-08-04 검증에서 발각).

        출력 루트 해석은 프로세스 전역 상태를 건드리면 안 된다.
        """
        monkeypatch.setattr(build_mod, "DEFAULT_OUT_ROOT", str(tmp_path))
        Pack("t", "제목")
        assert os.environ.get("PACK_OUT_ROOT") is None


# ---------------------------------------------------------------------------
# 노드
# ---------------------------------------------------------------------------

class TestNode:
    def test_props_go_into_nested_properties(self, pack):
        """91만 필드 사고의 회귀 검사 — 최상위로 펼치면 적재기가 통째로 못 읽었다."""
        pack.node("n1", "L", "Concept", "concept", {"mst": "268103"})
        rec = pack.nodes[0]
        assert rec["properties"] == {"mst": "268103"}
        assert "mst" not in {k for k in rec if k != "properties"}

    def test_no_properties_key_when_props_empty(self, pack):
        pack.node("n1", "L", "Concept", "concept")
        assert "properties" not in pack.nodes[0]

    def test_duplicate_id_is_ignored(self, pack):
        pack.node("n1", "A", "Concept", "concept")
        pack.node("n1", "B", "Concept", "concept")
        assert len(pack.nodes) == 1
        assert pack.nodes[0]["label"] == "A"

    def test_label_truncated_to_300(self, pack):
        pack.node("n1", "가" * 500, "Concept", "concept")
        assert len(pack.nodes[0]["label"]) == 300

    def test_source_type_override_is_hoisted_not_duplicated(self, pack):
        """팩 기본값을 노드별로 덮는 것은 의도된 기능. 중첩에 중복으로 남으면 안 된다."""
        pack.node("n1", "L", "Concept", "concept", {"source_type": "self-authored"})
        rec = pack.nodes[0]
        assert rec["source_type"] == "self-authored"
        assert "properties" not in rec

    def test_reserved_key_rejected_via_schema(self, pack):
        with pytest.raises(PackSchemaError, match="예약 키"):
            pack.node("n1", "L", "Concept", "concept", {"id": "x"})

    def test_pre_wrapped_props_rejected_via_schema(self, pack):
        with pytest.raises(PackSchemaError, match="평면 dict"):
            pack.node("n1", "L", "Concept", "concept", {"properties": {}})

    def test_schema_error_is_a_value_error(self, pack):
        """기존 빌더가 `except ValueError` 로 잡는다 — 계층을 깨면 조용히 안 잡힌다."""
        with pytest.raises(ValueError):
            pack.node("n1", "L", "Concept", "concept", {"space": "x"})

    def test_caller_props_dict_not_mutated(self, pack):
        """source_type 을 pop 하므로 호출자 dict 를 직접 건드리면 안 된다."""
        props = {"source_type": "self-authored", "a": 1}
        pack.node("n1", "L", "Concept", "concept", props)
        assert props == {"source_type": "self-authored", "a": 1}


# ---------------------------------------------------------------------------
# 엣지 grammar 정합화
# ---------------------------------------------------------------------------

class TestEdge:
    def test_allowed_relation_is_lowercased(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "CONTAINS")
        assert pack.edges[0]["label"] == "contains"
        # 대소문자만 달라도 source_label 이 남는다(characterization). 조건이 `rel != raw`
        # 라서 그렇다 — 원본 표기를 잃지 않는 쪽이 안전하므로 그대로 고정한다.
        assert pack.edges[0]["properties"] == {"source_label": "CONTAINS"}

    def test_exactly_matching_relation_leaves_properties_empty(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "contains")
        assert pack.edges[0]["properties"] == {}

    def test_unfit_label_is_replaced_and_original_preserved(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "슬쩍만든라벨")
        assert pack.edges[0]["label"] == "contains"
        assert pack.edges[0]["properties"]["source_label"] == "슬쩍만든라벨"

    def test_direction_reversed_when_only_inverse_pair_exists(self, pack):
        """(evidence, resource) 는 grammar 에 없고 (resource, evidence) 는 있다."""
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("e", "r", "contains")
        assert (pack.edges[0]["source_id"], pack.edges[0]["target_id"]) == ("r", "e")

    def test_claim_to_evidence_keeps_direction_for_loader_reversal(self, pack):
        """KEEP 공간쌍. 파일에는 claim->evidence 로 적히고 적재기가 뒤집는다 —
        파일과 라이브의 엣지 수가 달라 보이는 원인이 이것이다."""
        pack.node("c", "C", "Claim", "claim")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("c", "e", "supports")
        assert (pack.edges[0]["source_id"], pack.edges[0]["target_id"]) == ("c", "e")
        assert pack.edges[0]["label"] == "EVIDENCED_BY"

    def test_incompatible_pair_is_dropped_and_counted(self, pack):
        pack.node("co", "CO", "Community", "community")
        pack.node("s", "S", "Org", "subject")
        pack.edge("co", "s", "owns")
        assert pack.edges == []
        assert sum(pack._eskip.values()) == 1

    def test_duplicate_edge_deduped(self, pack):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "contains")
        pack.edge("r", "e", "contains")
        assert len(pack.edges) == 1

    def test_edge_between_unknown_nodes_passes_through_unvalidated(self, pack):
        """space 를 모르면 grammar 검사를 못 한다. 그 통과분을 validate() 가 잡는다."""
        pack.edge("unknown-a", "unknown-b", "whatever")
        assert pack.edges[0]["label"] == "whatever"


class TestEv:
    def test_creates_node_chunk_and_contains_edge(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "  본문  ")
        assert len(pack.nodes) == 2 and len(pack.chunks) == 1
        assert pack.chunks[0]["text"] == "본문"
        assert [e["label"] for e in pack.edges] == ["contains"]

    def test_extra_goes_to_chunk_meta_and_node_props_to_node(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문", extra={"page": 3}, node_props={"mst": "1"})
        assert pack.chunks[0]["metadata"]["page"] == 3
        assert "page" not in (pack.nodes[1].get("properties") or {})
        assert pack.nodes[1]["properties"] == {"mst": "1"}
        assert "mst" not in pack.chunks[0]["metadata"]

    def test_evidence_index_increments_per_document(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "1", "a")
        pack.ev("e2", doc, "2", "b")
        assert [c["metadata"]["evidence_index"] for c in pack.chunks] == [1, 2]

    def test_char_end_is_untrimmed_length(self, pack):
        """text 는 strip 해서 싣지만 char_end 는 원문 길이다(characterization)."""
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "  ab  ")
        assert pack.chunks[0]["metadata"]["char_end"] == 6
        assert pack.chunks[0]["text"] == "ab"

    def test_reregistering_same_eid_is_a_silent_noop(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "a", node_props={"k": 1})
        pack.ev("e1", doc, "다른라벨", "b", node_props={"k": 2})
        assert len(pack.chunks) == 1


# ---------------------------------------------------------------------------
# validate — 이관으로 되살아난 검사
# ---------------------------------------------------------------------------

class TestValidateRemapHazard:
    def test_hazard_is_detected(self, pack, capsys):
        """선언 space 와 적재기 remap 후 space 가 다르면 연결 엣지가 전량 skip 된다.

        **이관 전 이 검사는 사실상 꺼져 있었다.** 표를 적재기에서 지연 import 하고
        `except Exception: _NTO = None` 으로 삼켰기 때문에, 적재기를 import 할 수 없는
        빌드 환경(= 대부분)에서는 통째로 건너뛰었다. 표가 schema 로 오면서 항상 돈다.
        """
        assert NODE_TYPE_OVERRIDE["TextUnit"] == ("concept", "Topic")
        pack.node("n1", "L", "TextUnit", "evidence")
        pack.validate()
        assert "remap 함정" in capsys.readouterr().out

    def test_hazard_blocks_in_strict_mode(self, pack):
        pack.node("n1", "L", "TextUnit", "evidence")
        with pytest.raises(ValueError, match="remap 함정"):
            pack.validate(strict=True)

    def test_matching_type_is_not_a_hazard(self, pack, capsys):
        pack.node("n1", "L", "TextEvidence", "evidence")
        pack.validate()
        assert "remap 함정" not in capsys.readouterr().out

    def test_strict_can_be_enabled_by_env(self, pack, monkeypatch):
        monkeypatch.setenv("PACK_LIB_STRICT", "1")
        pack.node("n1", "L", "TextUnit", "evidence")
        with pytest.raises(ValueError):
            pack.validate()


class TestStrayTopLevelAlwaysBlocks:
    """**91만 필드 사고의 재발 방지 불변식.** 적대 검증에서 이 축이 무보호였다.

    `build.py` 의 stray 검사는 `errors.append(msg)` 를 **strict 와 무관하게** 한다.
    그것을 `if strict:` 로 되돌려도 기존 테스트 96건이 전부 통과했다(변이 V7 생존).
    커스텀 필드가 노드 최상위에 펼쳐지면 적재기가 통째로 못 읽어 조용히 사라진다 —
    경고로 낮추는 순간 사고가 그대로 재발한다.
    """

    def test_stray_top_level_raises_even_without_strict(self, pack):
        pack.node("n1", "L", "Concept", "concept")
        pack.nodes[0]["brand"] = "Yamaha"          # 생산자를 우회해 직접 오염
        with pytest.raises(ValueError, match="비구조 키"):
            pack.validate(strict=False)

    def test_stray_message_names_the_key_and_the_fix(self, pack):
        pack.node("n1", "L", "Concept", "concept")
        pack.nodes[0]["brand"] = "Yamaha"
        with pytest.raises(ValueError) as ei:
            pack.validate(strict=False)
        assert "brand" in str(ei.value)
        assert "properties" in str(ei.value)

    def test_save_is_blocked_by_stray(self, pack):
        """save() 가 validate() 를 먼저 부르므로 오염된 팩은 디스크에 안 남는다."""
        pack.node("n1", "L", "Concept", "concept")
        pack.nodes[0]["brand"] = "Yamaha"
        with pytest.raises(ValueError, match="비구조 키"):
            pack.save()
        assert not (pack.out / "nodes.jsonl").exists()


class TestTraceabilityRetarget:
    """traceability(claim/lever/outcome/policy)의 방향·리타겟 규칙.

    적대 검증에서 `_TRACE_SRC` 조건 반전(V4)과 reverse 가드 제거(V5)가 둘 다 생존했다.
    이 규칙이 깨지면 채점기의 근거 연결 계산이 조용히 무너진다.
    """

    def test_claim_to_resource_is_retargeted_to_its_evidence(self, pack):
        """claim -> resource 는 그 resource 의 evidence 로 옮겨 붙는다(정방향 유지)."""
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "근거", "본문")
        c = pack.claim("c", "주장")
        pack.edge(c, doc, "supports")
        retargeted = [e for e in pack.edges if e["source_id"] == c]
        assert len(retargeted) == 1
        assert retargeted[0]["target_id"] == "e1"
        assert retargeted[0]["label"] == "EVIDENCED_BY"

    def test_retarget_picks_the_first_evidence_of_the_document(self, pack):
        """evidence 가 여러 건이면 **첫 번째**(문서 대표)로 간다.

        적대 검증에서 `self._ev_of[tgt][0]` 을 `[-1]` 로 바꿔도 116건이 전부 통과했다.
        위 테스트가 evidence 1건짜리라 첫/끝을 구분하지 못했기 때문이다. 실팩에서
        resource 하나에 evidence 가 여러 건 달리는 것은 정상이고(egov-audit 최대 43,
        n2sf 최대 52, msk-imaging 최대 26), "리타겟된 근거가 어느 것인가"는 팩 의미에
        직결된다.
        """
        doc = pack.resource("d", "문서")
        # **등록순과 사전순을 일부러 어긋나게** 둔다. `ev-a/b/c` 순으로 넣으면
        # `sorted(self._ev_of[tgt])[0]` 같은 회귀가 같은 답을 내서 생존한다(적대 검증 실측).
        for i in ("b", "a", "c"):
            pack.ev(f"ev-{i}", doc, f"근거{i}", f"본문{i}")
        c = pack.claim("c", "주장")
        pack.edge(c, doc, "supports")
        retargeted = [e for e in pack.edges if e["source_id"] == c]
        assert [e["target_id"] for e in retargeted] == ["ev-b"]

    @pytest.mark.parametrize("space", ["claim", "lever", "outcome", "policy"])
    def test_every_trace_src_space_is_retargeted(self, pack, space):
        """리타겟은 `_TRACE_SRC` **네 공간 전부**에 걸린다.

        기존 검사가 claim 하나뿐이라 lever/outcome/policy 를 `_TRACE_SRC` 에서 빼도
        스위트가 통과했다(적대 검증 실측).
        """
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "근거", "본문")
        src = getattr(pack, space)("s", "노드")
        pack.edge(src, doc, "supports")
        assert [(e["source_id"], e["target_id"]) for e in pack.edges
                if e["source_id"] == src] == [(src, "e1")]

    # reverse 가드가 **실효**인 공간쌍은 이 둘뿐이다. 정방향 grammar 가 없고 역방향은
    # 있어서, 가드가 없으면 `resource --states--> claim` / `resource --has_mode--> lever`
    # 로 뒤집혀 실린다. 나머지 둘은 다른 분기다(아래 두 테스트가 그 차이를 못박는다).
    @pytest.mark.parametrize("space,label,reverse_rel", [
        ("claim", "supports", "states"),
        ("lever", "affects", "has_mode"),
    ])
    def test_trace_src_to_resource_without_evidence_is_dropped(
            self, pack, space, label, reverse_rel):
        """리타겟할 evidence 가 없으면 **반전하지 않고 드롭**한다.

        **양끝을 다 본다.** 예전에는 claim 쪽만 `in (c, doc)` 로 보고 lever 쪽은
        `== lv` 만 봐서, 가드를 `ss == 'claim'` 으로 좁히면 lever 가 target 으로 뒤집힌
        형태를 놓쳤다. 두 검사가 비대칭인데 커밋 메시지는 대칭이라고 적었다(적대 검증 지적).
        """
        from opencrab.pack.schema import ALLOWED
        assert ("resource", space) in ALLOWED and (space, "resource") not in ALLOWED, \
            "가드가 실효인 전제(정방향 없음 + 역방향 있음)가 깨졌다"
        doc = pack.resource("d", "문서")
        src = getattr(pack, space)("s", "노드")
        pack.edge(src, doc, label)
        assert [e for e in pack.edges if src in (e["source_id"], e["target_id"])] == []
        assert reverse_rel not in [e["label"] for e in pack.edges]
        assert sum(pack._eskip.values()) == 1

    def test_outcome_to_resource_drops_without_needing_the_guard(self, pack):
        """outcome -> resource 는 양방향 모두 grammar 에 없어 가드와 무관하게 드롭된다.

        위 테스트에 outcome 을 끼워 넣으면 "가드가 지킨다"는 거짓 인상을 준다.
        분기가 다르다는 사실 자체를 못박는다.
        """
        from opencrab.pack.schema import ALLOWED
        assert ("outcome", "resource") not in ALLOWED
        assert ("resource", "outcome") not in ALLOWED
        doc = pack.resource("d", "문서")
        oc = pack.outcome("o", "성과")
        pack.edge(oc, doc, "supports")
        assert [e for e in pack.edges if oc in (e["source_id"], e["target_id"])] == []

    def test_policy_to_resource_is_a_valid_pair_and_is_not_dropped(self, pack):
        """policy -> resource 는 정합 공간쌍이라 드롭이 아니라 FIX 된다.

        `_TRACE_SRC` 네 공간을 뭉뚱그려 "전부 드롭"이라고 쓰면 이 케이스에서 거짓이 된다.
        """
        doc = pack.resource("d", "문서")
        po = pack.policy("p", "정책")
        pack.edge(po, doc, "supports")
        edges = [e for e in pack.edges if e["source_id"] == po]
        assert len(edges) == 1
        assert edges[0]["target_id"] == doc
        assert edges[0]["label"] == "classifies"        # FIX 대표값
        assert edges[0]["properties"]["source_label"] == "supports"

    def test_non_traceability_space_still_reverses(self, pack):
        """가드는 traceability 에만 걸린다 — 일반 공간쌍은 그대로 반전한다."""
        doc = pack.resource("d", "문서")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("e", doc, "contains")
        assert (pack.edges[0]["source_id"], pack.edges[0]["target_id"]) == (doc, "e")


class TestValidateOther:
    def test_dangling_edge_blocks_only_in_strict(self, pack):
        pack.edge("ghost-a", "ghost-b", "related_to")
        pack.validate()                       # 경고만
        with pytest.raises(ValueError, match="dangling"):
            pack.validate(strict=True)

    def test_grammar_violation_reported(self, pack, capsys):
        pack.edge("ghost-a", "ghost-b", "related_to")   # space 미확정으로 검증 우회
        pack.node("ghost-a", "A", "Concept", "concept")
        pack.node("ghost-b", "B", "Evidence", "evidence")
        pack.validate()
        assert "grammar 위반" in capsys.readouterr().out

    def test_empty_spaces_warned(self, pack, capsys):
        pack.node("n1", "L", "Concept", "concept")
        pack.validate()
        assert "9-space 비어있음" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------

class TestSave:
    def test_writes_three_logical_files(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        pack.save()
        assert [r["id"] for r in iter_jsonl(pack.out / "chunks.jsonl")] == ["e1"]
        assert len(list(iter_jsonl(pack.out / "nodes.jsonl"))) == 2
        assert len(list(iter_jsonl(pack.out / "edges.jsonl"))) == 1

    def test_save_runs_validate(self, pack):
        pack.node("n1", "L", "TextUnit", "evidence")
        with pytest.raises(ValueError, match="remap 함정"):
            pack.validate(strict=True)

    def test_output_is_utf8_unescaped(self, pack):
        pack.node("n1", "한글 라벨", "Concept", "concept")
        pack.save()
        assert "한글 라벨" in (pack.out / "nodes.jsonl").read_text(encoding="utf-8")

    def test_uid_is_deterministic_and_slug_namespaced(self, tmp_path):
        a = Pack("pack-a", "A", out_root=str(tmp_path))
        b = Pack("pack-b", "B", out_root=str(tmp_path))
        assert a.uid("x", 1) == Pack("pack-a", "A", out_root=str(tmp_path)).uid("x", 1)
        assert a.uid("x", 1) != b.uid("x", 1)

    def test_created_at_is_uniform_within_a_pack(self, pack):
        """전 레코드에 같은 빌드 시각이 박힌다 — 재빌드 diff 검증이 이 성질에 의존한다."""
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        stamps = {r["created_at"] for r in pack.nodes + pack.edges + pack.chunks}
        assert len(stamps) == 1

    def test_module_reexports_json_for_legacy_builders(self):
        """`pack_lib.json.dumps(...)` 를 쓰는 빌더가 있다(실측 3곳)."""
        from opencrab.pack import build
        assert build.json is json


# ---------------------------------------------------------------------------
# 기본값 — 호출부가 생략하는 값이라 무검사로 남기 쉽다
# ---------------------------------------------------------------------------

class TestDefaults:
    """생략 가능한 인자의 기본값. **호출부 다수가 이 값에 의존한다.**

    적대 검증이 `source_type='reference-public'` 를 `'reference-private'` 로 바꿨는데
    57 건이 전부 통과했다(2026-08-05). 기존 검사가 override 경로만 봤기 때문이다.
    실측: opencrab-dump 의 `Pack()` 대입 60 건 중 `source_type` 을 명시하는 것은 22 건
    뿐이고 **38 건이 이 기본값에 의존**한다. 이 값은 노드·청크의 최상위 필드로 그대로
    라이브에 실린다.
    """

    def test_default_source_type_lands_on_nodes_and_chunks(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        assert pack.nodes[0]["source_type"] == "reference-public"
        assert pack.chunks[0]["source_type"] == "reference-public"

    def test_explicit_source_type_replaces_the_default_everywhere(self, tmp_path):
        p = Pack("t", "제목", source_type="self-authored", out_root=str(tmp_path))
        doc = p.resource("d", "문서")
        p.ev("e1", doc, "라벨", "본문")
        assert {n["source_type"] for n in p.nodes} == {"self-authored"}
        assert p.chunks[0]["source_type"] == "self-authored"

    @pytest.mark.parametrize("maker,expected_type", [
        ("resource", "Document"),
        ("subject", "Org"),
    ])
    def test_default_node_types(self, pack, maker, expected_type):
        getattr(pack, maker)("s", "라벨")
        assert pack.nodes[0]["node_type"] == expected_type

    def test_ev_default_node_type(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        assert pack.nodes[1]["node_type"] == "TextEvidence"

    @pytest.mark.parametrize(
        "maker", ["concept", "claim", "community", "outcome", "lever", "policy"])
    def test_blank_desc_produces_no_properties_key(self, pack, maker):
        """`desc=''` 는 properties 를 만들지 않는다 — 빈 설명이 실리면 안 된다."""
        getattr(pack, maker)("s", "라벨")
        assert "properties" not in pack.nodes[0]

    def test_uid_namespace_uuid_is_pinned(self, tmp_path):
        """`_NS` 가 바뀌면 **모든 팩의 모든 노드 id 가 통째로 갈린다.**

        기존 uid 검사는 결정성(같은 입력 → 같은 출력)과 팩 간 분리만 봤다. 둘 다 상대
        비교라 네임스페이스를 통째로 바꿔도 그대로 성립한다. 절대값으로 못박는다.
        """
        assert str(build_mod._NS) == "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
        assert Pack("t", "제목", out_root=str(tmp_path)).uid("x", 1) == \
            "2fd29310-7523-5e4f-a6b5-da85617f0693"

    EXPECTED_DEFAULTS = {
        "__init__": {"source_type": "reference-public", "out_root": None},
        "node": {"props": None},
        "edge": {"props": None},
        "ev": {"extra": None, "ntype": "TextEvidence", "node_props": None},
        "resource": {"nt": "Document", "props": None},
        "subject": {"nt": "Org", "props": None},
        "concept": {"desc": ""},
        "claim": {"desc": ""},
        "community": {"desc": ""},
        "outcome": {"desc": ""},
        "lever": {"desc": ""},
        "policy": {"desc": ""},
        "validate": {"strict": None},
    }

    @pytest.mark.parametrize("name", sorted(EXPECTED_DEFAULTS))
    def test_method_defaults_are_pinned(self, name):
        """행동 검사로 덮이지 않는 나머지를 메우는 그물. 행동 검사를 대체하지 않는다 —
        기대값을 같이 고치면 통과하므로 변경 감지이지 의미 보존 증명이 아니다."""
        got = {k: v.default
               for k, v in inspect.signature(getattr(Pack, name)).parameters.items()
               if v.default is not inspect.Parameter.empty}
        assert got == self.EXPECTED_DEFAULTS[name]
