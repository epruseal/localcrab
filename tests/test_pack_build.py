"""팩 생산자(opencrab.pack.build) 테스트.

이관 전 이 코드는 호스트 리포에 있었고 테스트가 없었다. 계약(schema)과 같은 리포로
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
from opencrab.pack.schema import (
    ALL_SPACES,
    CHUNK_STRUCT_KEYS,
    EDGE_STRUCT_KEYS,
    NODE_STRUCT_KEYS,
    NODE_TYPE_OVERRIDE,
    SPACE_DEFAULT_TYPE,
    PackSchemaError,
)


@pytest.fixture(autouse=True)
def _no_host_default(monkeypatch):
    """호스트 리포의 shim 이 주입하는 기본값에서 테스트를 격리한다."""
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

        이관 전에는 `Path(__file__).parents[2] / <출력 디렉터리>` 였고, 이 파일이 이리로
        온 지금 그 코드가 남아 있었다면 **이 리포 안에** 출력 디렉터리를 만들었을 것이다.
        """
        with pytest.raises(ValueError, match="출력 루트"):
            Pack("t", "제목")

    def test_empty_env_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("PACK_OUT_ROOT", "")
        with pytest.raises(ValueError, match="출력 루트"):
            Pack("t", "제목")

    def test_host_default_out_root_is_used(self, tmp_path, monkeypatch):
        """호스트 리포가 주입하는 기본값. 호스트 shim 이 이 경로로 출력 루트를 준다."""
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
        (git 추적 산출물) 대신 스크래치 경로로 조용히 새 나갔다.
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


class TestFixIsTheRepresentativeNotTheAlphabeticalFirst:
    """정합 불가 라벨의 대체값은 **FIX 표의 대표값**이지 사전순 첫 원소가 아니다.

    `rel = _FIX.get((ss, tt)) or sorted(allowed)[0]` 에서 `or` 를 `and` 로 바꾸면
    결과가 `sorted(allowed)[0]` 이 되는데, 스윕에서 그 변이가 살아남았다(2026-08-05).
    기존 검사가 `resource->evidence` 만 썼고 그 쌍은 FIX 와 sorted[0] 이 같아서
    두 값을 구분하지 못했기 때문이다.

    실측(2026-08-05): ALLOWED 38 쌍 중 **12 쌍**에서 둘이 다르고, 그중에는 의미가
    정반대인 것이 있다.

        lever   -> outcome   FIX=raises      sorted[0]=lowers
        evidence-> claim     FIX=supports    sorted[0]=contradicts
        policy  -> subject   FIX=requires_approval  sorted[0]=denies

    사전순으로 새면 라이브 그래프에서 엣지 의미가 뒤집힌다. 발산하는 쌍으로 못박는다.
    """

    @pytest.mark.parametrize("src_space,tgt_space,fix_rel,alpha_first", [
        ("lever", "outcome", "raises", "lowers"),
        ("evidence", "claim", "supports", "contradicts"),
        ("policy", "subject", "requires_approval", "denies"),
    ])
    def test_unfit_label_becomes_the_fix_value(
            self, pack, src_space, tgt_space, fix_rel, alpha_first):
        from opencrab.pack.schema import ALLOWED, FIX
        assert FIX[(src_space, tgt_space)] == fix_rel
        assert sorted(ALLOWED[(src_space, tgt_space)])[0] == alpha_first
        assert fix_rel != alpha_first, "발산하지 않으면 이 검사는 두 값을 구분하지 못한다"

        pack.node("s", "S", SPACE_DEFAULT_TYPE[src_space], src_space)
        pack.node("t", "T", SPACE_DEFAULT_TYPE[tgt_space], tgt_space)
        pack.edge("s", "t", "정합불가라벨")
        assert pack.edges[0]["label"] == fix_rel

    def test_reversed_pair_also_uses_the_fix_value(self, pack):
        """반전 분기도 같은 규칙이다 — 한쪽만 걸면 다른 쪽이 무방비가 된다.

        outcome -> lever 는 grammar 에 없고 lever -> outcome 은 있다. 반전 후
        FIX=raises / sorted[0]=lowers 로 발산한다.
        """
        from opencrab.pack.schema import ALLOWED
        assert ("outcome", "lever") not in ALLOWED and ("lever", "outcome") in ALLOWED
        pack.node("o", "O", "Outcome", "outcome")
        pack.node("lv", "L", "Lever", "lever")
        pack.edge("o", "lv", "정합불가라벨")
        e = pack.edges[0]
        assert (e["source_id"], e["target_id"]) == ("lv", "o")
        assert e["label"] == "raises"


class TestEdgeNeedsBothSpacesToValidate:
    """`if ss and tt:` — 양끝 space 를 **둘 다** 알아야 grammar 검사를 한다.

    `and` 를 `or` 로 바꾼 변이가 살아남았다. 기존 검사가 "둘 다 모르는" 경우만 봐서
    한쪽만 아는 경우를 구분하지 못했다. 한쪽만 알고 검사에 들어가면
    `_ALLOWED.get((ss, None))` 이 되어 정합 라벨이 엉뚱하게 드롭되거나 치환된다.
    """

    @pytest.mark.parametrize("known", ["source", "target"])
    def test_one_known_endpoint_skips_grammar_validation(self, pack, known):
        if known == "source":
            pack.node("a", "A", "Concept", "concept")
            pack.edge("a", "unknown-b", "슬쩍만든라벨")
        else:
            pack.node("b", "B", "Concept", "concept")
            pack.edge("unknown-a", "b", "슬쩍만든라벨")
        assert len(pack.edges) == 1, "한쪽만 알면 드롭하지 않는다"
        assert pack.edges[0]["label"] == "슬쩍만든라벨", "치환도 하지 않는다"
        assert pack.edges[0]["properties"] == {}
        assert sum(pack._eskip.values()) == 0


class TestUidDiscriminators:
    """헬퍼마다 uid 앞에 붙이는 **판별자**가 팩 안의 id 충돌을 막는다.

    판별자가 비면 같은 slug 를 쓴 서로 다른 space 의 노드가 **같은 id** 가 되어 뒤에
    온 쪽이 조용히 사라진다(`node()` 는 이미 등록된 id 를 그냥 반환한다).

    **처음에는 "8개 헬퍼에 같은 slug 를 주고 id 가 전부 다른지" 로 썼는데 그게 틀렸다.**
    판별자 **하나만** 비어도 나머지 7개가 그대로라 id 는 여전히 전부 다르다. 즉 그
    검사가 잡는 것은 8곳이 동시에 바뀌는 복합 변이뿐인데, 스윕 도구는 복합 변이를
    생성하지 않는다고 스스로 명시했다 — 스윕이 만들 수 없는 입력만 덮은 셈이다
    (적대 검증 지적, 2026-08-05: 8개 판별자를 하나씩 비우는 변이가 전부 생존).
    **한 자리 변이로 죽도록** 헬퍼별 절대 id 를 못박는다.
    """

    # slug "동일슬러그", 팩 slug "t" 기준 실측값. uuid5 라 결정적이다.
    EXPECTED_IDS = {
        "resource": "a345c018-d97c-5ff7-b77f-4a4b811fd660",
        "subject": "2bc34ed5-3822-58e9-bbb0-c2988ee1a83b",
        "concept": "9fea1a26-c1d9-5adf-86e2-161da5b87035",
        "claim": "7c8c3172-752b-53ce-9cc9-61adca71bdd6",
        "community": "ccfdd8d1-ef45-5bc8-96fd-d0170571439c",
        "outcome": "b146f873-4c95-57d8-9343-8f5830d9b206",
        "lever": "d6b87152-6575-5eaf-a813-d49658af880f",
        "policy": "8623978d-e9fb-5e9b-9a1f-48d2c12354ea",
    }

    @pytest.mark.parametrize("helper", sorted(EXPECTED_IDS))
    def test_helper_id_is_pinned(self, pack, helper):
        """한 자리만 바뀌어도 여기서 죽는다."""
        assert getattr(pack, helper)("동일슬러그", "라벨") == self.EXPECTED_IDS[helper]

    def test_pinned_ids_are_all_distinct(self, pack):
        """위 고정값들이 서로 달라야 판별자가 제 역할을 한다(비공허성 보증)."""
        assert len(set(self.EXPECTED_IDS.values())) == len(self.EXPECTED_IDS)

    def test_edge_uid_does_not_collide_with_node_uid(self, pack):
        """엣지 id 는 `uid('edge', src, rel, tgt)` 다 — 판별자가 없으면 노드와 겹칠 수 있다."""
        r = pack.resource("d", "문서")
        e = pack.node("ev", "E", "Evidence", "evidence")
        pack.edge(r, e, "contains")
        assert pack.edges[0]["id"] == "19ac6ed5-2a73-5965-86d4-63e3599be869"
        assert pack.edges[0]["id"] not in {n["id"] for n in pack.nodes}

    def test_node_returns_the_id_it_registered(self, pack):
        """`node()` 의 `return nid` 를 지워도 아무도 안 죽었다.

        기존 검사가 `e = pack.node(...)` 로 받아 **기대값도 같은 e 로** 계산해서
        자기정합이었다. 반환값은 헬퍼·`ev()`·호출자 전부가 의존하는 계약이다.
        """
        assert pack.node("zz", "Z", "Concept", "concept") == "zz"
        assert pack.node("zz", "다시", "Concept", "concept") == "zz", "중복 등록도 id 를 돌려준다"


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
        """`<이 모듈>.json.dumps(...)` 처럼 모듈을 통해 접근하는 빌더가 있다(실측 3곳)."""
        from opencrab.pack import build
        assert build.json is json


# ---------------------------------------------------------------------------
# 레코드 구조 — 노드만 지키고 엣지·청크는 무방비였다
# ---------------------------------------------------------------------------

class TestRecordStructureMatchesTheSchema:
    """생산된 세 레코드의 **키 집합**이 계약(schema)과 정확히 같아야 한다.

    노드는 `validate()` 의 stray 검사가 지키지만 **엣지·청크에는 같은 검사가 없다.**
    전면 스윕에서 `'source_id'`·`'document_id'`·`'char_start'` 같은 키 이름을 빈
    문자열로 바꾼 변이가 전부 생존했다(2026-08-05). 키 하나가 바뀌면 적재기가 그
    필드를 통째로 못 읽는데, 그게 정확히 91만 필드 사고의 형태다.

    schema 의 집합을 그대로 기대값으로 쓴다 — 여기 값을 따로 적으면 계약이 또 이중화된다.
    """

    @pytest.fixture
    def built(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문", extra={"page": 3})
        return pack

    def test_edge_keys_are_exactly_the_edge_struct_keys(self, built):
        assert set(built.edges[0]) == set(EDGE_STRUCT_KEYS)

    def test_chunk_keys_are_exactly_the_chunk_struct_keys(self, built):
        assert set(built.chunks[0]) == set(CHUNK_STRUCT_KEYS)

    def test_node_keys_are_a_subset_of_the_node_struct_keys(self, built):
        """노드는 선택 키(properties·degree)가 있어 부분집합이다. 초과가 사고였다."""
        for n in built.nodes:
            assert set(n) <= set(NODE_STRUCT_KEYS), set(n) - set(NODE_STRUCT_KEYS)

    def test_chunk_metadata_carries_the_documented_fields(self, built):
        meta = built.chunks[0]["metadata"]
        assert set(meta) == {"evidence_index", "char_start", "char_end",
                             "source_package_title", "page"}
        assert meta["char_start"] == 0
        assert meta["source_package_title"] == built.title

    def test_edge_endpoints_are_not_swapped(self, pack):
        """`source_id`/`target_id` 이름이 서로 바뀌어도 키 집합 검사는 통과한다 —
        값까지 본다."""
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "contains")
        assert (pack.edges[0]["source_id"], pack.edges[0]["target_id"]) == ("r", "e")

    def test_chunk_source_and_document_id_both_point_at_the_document(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        c = pack.chunks[0]
        assert (c["document_id"], c["source"], c["id"]) == (doc, doc, "e1")


# ---------------------------------------------------------------------------
# 9-space 헬퍼 — 각자 어느 space·node_type 을 붙이는가
# ---------------------------------------------------------------------------

class TestSpaceHelpers:
    """9 개 헬퍼가 붙이는 `(space, node_type)`. 전면 스윕에서 전부 무검사였다.

    `pack.claim(...)` 이 claim 이 아닌 space 를 붙이면 그 노드에 걸린 엣지가 grammar
    불일치로 적재 시 대량 skip 된다 — 조용한 데이터 유실이다.
    """

    EXPECTED = {
        "resource": ("resource", "Document"),
        "subject": ("subject", "Org"),
        "concept": ("concept", "Concept"),
        "claim": ("claim", "Claim"),
        "community": ("community", "Community"),
        "outcome": ("outcome", "Outcome"),
        "lever": ("lever", "Lever"),
        "policy": ("policy", "Policy"),
    }

    @pytest.mark.parametrize("helper", sorted(EXPECTED))
    def test_helper_space_and_node_type(self, pack, helper):
        getattr(pack, helper)("s", "라벨")
        n = pack.nodes[0]
        assert (n["space"], n["node_type"]) == self.EXPECTED[helper]

    @pytest.mark.parametrize("helper", ["concept", "claim", "community",
                                        "outcome", "lever", "policy"])
    def test_desc_lands_in_description_property(self, pack, helper):
        getattr(pack, helper)("s", "라벨", "설명")
        assert pack.nodes[0]["properties"] == {"description": "설명"}

    def test_evidence_helper_space(self, pack):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        assert pack.nodes[1]["space"] == "evidence"

    def test_helpers_cover_every_grammar_space(self, pack):
        """evidence 는 `ev()`, 나머지 8 개는 전용 헬퍼. 9-space 를 다 덮는지."""
        covered = {sp for sp, _ in self.EXPECTED.values()} | {"evidence"}
        assert covered == set(ALL_SPACES)


# ---------------------------------------------------------------------------
# validate / save 진단 — "출력이 났다" 만 보고 수치는 안 봤다
# ---------------------------------------------------------------------------

class TestDiagnosticsReportRealNumbers:
    """진단 출력의 **집계**가 계약이다.

    기존 검사는 전부 `"..." in out` 형태라 문구만 봤다. 그래서 `stray[k] += 1` 을
    `+= 2` 로, 카운터 증분과 상위 N 캡을 바꾸는 변이가 전부 생존했다(2026-08-05).
    운영자는 이 숫자를 보고 팩을 고칠지 말지 정한다 — 숫자가 틀리면 판단이 틀린다.
    """

    def test_stray_counts_kinds_and_occurrences(self, pack):
        pack.node("n1", "L", "Concept", "concept")
        pack.node("n2", "L", "Concept", "concept")
        pack.nodes[0]["brand"] = "Yamaha"      # 1종
        pack.nodes[1]["brand"] = "Honda"       # 같은 종, 2건째
        pack.nodes[1]["model"] = "CB"          # 2종
        with pytest.raises(ValueError) as ei:
            pack.validate()
        assert "2종 3건" in str(ei.value), str(ei.value)

    def test_stray_key_list_is_capped_but_the_total_is_not(self, pack, capsys):
        """목록은 8 개로 자르되 **총계는 자르지 않는다** — 잘린 걸 알 수 있어야 한다."""
        pack.node("n1", "L", "Concept", "concept")
        for i in range(12):
            pack.nodes[0][f"k{i:02d}"] = i
        with pytest.raises(ValueError):
            pack.validate()
        out = capsys.readouterr().out
        assert "12종 12건" in out
        assert "'k07'" in out and "'k08'" not in out, "상위 8개만 나열해야 한다"

    def test_dangling_count_is_reported(self, pack, capsys):
        pack.edge("ghost-a", "ghost-b", "related_to")
        pack.edge("ghost-c", "ghost-d", "related_to")
        pack.validate()
        assert "dangling 노드 참조 엣지 2건" in capsys.readouterr().out

    @pytest.mark.parametrize("missing", ["source", "target"])
    def test_dangling_detects_a_single_missing_endpoint(self, pack, capsys, missing):
        """`source not in _nid **or** target not in _nid` — 한쪽만 없어도 dangling 이다.

        기존 검사가 **양끝 다 없는** 엣지만 써서 `or` 의 두 항을 구분하지 못했다.
        그래서 `not in` 을 `in` 으로 뒤집는 변이가 양쪽 항 모두에서 살아남았다
        (2026-08-05). 한쪽씩 빠뜨려 두 항을 따로 건다.
        """
        pack.node("real", "R", "Concept", "concept")
        if missing == "source":
            pack.edge("ghost", "real", "related_to")
        else:
            pack.edge("real", "ghost", "related_to")
        pack.validate()
        assert "dangling 노드 참조 엣지 1건" in capsys.readouterr().out

    def test_grammar_violation_count_is_reported(self, pack, capsys):
        for i in range(3):
            pack.edge(f"a{i}", f"b{i}", "related_to")     # space 미확정으로 우회
        for i in range(3):
            pack.node(f"a{i}", "A", "Concept", "concept")
            pack.node(f"b{i}", "B", "Evidence", "evidence")
        pack.validate()
        assert "grammar 위반 엣지 3건" in capsys.readouterr().out

    def test_fix_substitution_count_is_reported(self, pack, capsys):
        pack.node("r", "R", "Document", "resource")
        pack.node("e", "E", "Evidence", "evidence")
        pack.edge("r", "e", "슬쩍만든라벨")
        pack.validate()
        assert "자동치환(FIX) 엣지 1건" in capsys.readouterr().out

    def test_unlinked_claim_concept_ratio_is_reported(self, pack, capsys):
        pack.claim("c1", "주장1")
        pack.claim("c2", "주장2")
        pack.concept("k1", "개념")
        pack.validate()
        assert "evidence_refs 없는 claim/concept 3/3건" in capsys.readouterr().out

    def test_evidence_linked_nodes_are_excluded_from_the_unlinked_count(
            self, pack, capsys):
        """근거에 **연결된** claim/concept 은 세지 않는다.

        기존 검사에 엣지가 하나도 없어서 `ev_touch` 집계 루프가 통째로 안 돌았다.
        그래서 그 안의 비교·키 접근·문장 삭제 변이가 전부 살아남았다(2026-08-05).
        양방향(evidence 가 src 인 경우와 tgt 인 경우)을 모두 태운다.
        """
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "근거", "본문")
        linked_claim = pack.claim("c1", "연결된 주장")
        pack.edge(linked_claim, "e1", "supports")      # claim -> evidence (KEEP)
        linked_concept = pack.concept("k1", "연결된 개념")
        pack.edge("e1", linked_concept, "mentions")    # evidence -> concept
        pack.claim("c2", "고아 주장")
        pack.validate()
        assert "evidence_refs 없는 claim/concept 1/3건" in capsys.readouterr().out

    def test_empty_spaces_are_listed_by_name(self, pack, capsys):
        """채워진 space 는 빠지고 **나머지 8 개가 전부** 나열돼야 한다."""
        pack.node("n1", "L", "Concept", "concept")
        pack.validate()
        line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if "9-space 비어있음" in ln)
        assert "'concept'" not in line
        for sp in ALL_SPACES:
            if sp != "concept":
                assert f"'{sp}'" in line, f"{sp} 가 빈 space 목록에서 빠졌다"

    def test_remap_hazard_count_is_reported(self, pack, capsys):
        pack.node("n1", "L", "TextUnit", "evidence")
        pack.node("n2", "L", "TextUnit", "evidence")
        pack.validate()
        assert "remap 함정 2건" in capsys.readouterr().out

    def test_save_reports_final_counts_and_space_histogram(self, pack, capsys):
        doc = pack.resource("d", "문서")
        pack.ev("e1", doc, "라벨", "본문")
        pack.save()
        out = capsys.readouterr().out
        assert "FINAL t: 2 nodes, 1 edges, 1 chunks" in out
        assert "'resource': 1" in out and "'evidence': 1" in out

    def test_save_reports_dropped_edge_count(self, pack, capsys):
        pack.node("co", "CO", "Community", "community")
        pack.node("s", "S", "Org", "subject")
        pack.edge("co", "s", "owns")
        pack.edge("co", "s", "owns2")
        pack.save()
        assert "grammar 드롭 엣지 2건" in capsys.readouterr().out

    def test_reports_list_actionable_detail_lines_not_just_totals(self, pack, capsys):
        """총계 밑의 **상세 줄**이 실제 작업 지시다 — 루프를 지워도 총계는 그대로다.

        네 리포트(grammar 위반 / FIX 치환 / remap 함정 / 드롭 엣지)의 상세 루프를
        삭제하는 변이가 전부 살아남았다(2026-08-05). 총계만 검사했기 때문이다.
        운영자는 "무엇을" 고칠지를 이 줄에서 읽는다.
        """
        pack.node("a", "A", "Concept", "concept")
        pack.node("b", "B", "Evidence", "evidence")
        pack.edge("a", "b", "정합불가라벨")            # FIX 치환
        pack.node("n1", "L", "TextUnit", "evidence")   # remap 함정
        pack.edge("ghost-a", "ghost-b", "related_to")
        pack.node("ghost-a", "A", "Concept", "concept")
        pack.node("ghost-b", "B", "Evidence", "evidence")   # grammar 위반(우회 통과분)
        pack.validate()
        out = capsys.readouterr().out
        assert "concept→evidence" in out, "위반·치환 줄에 공간쌍이 나와야 한다"
        assert "정합불가라벨" in out, "치환 줄에 원본 라벨이 나와야 한다"
        assert "node_type='TextUnit'" in out, "함정 줄에 문제의 node_type 이 나와야 한다"

    def test_fix_detail_line_shows_the_pair_in_the_edge_direction(self, pack, capsys):
        """FIX 상세줄의 공간쌍 방향을 **그 줄만 특정해** 못박는다.

        위 검사는 `"concept→evidence" in out` 인데 그 부분문자열을 **grammar 위반 줄이
        대신 공급**한다. 그래서 FIX 줄의 `(ss, tt)` 를 `(tt, ss)` 로 뒤집는 변이가
        135 건 통과한 채 살아남았다(적대 검증 실증, 2026-08-05).
        방향이 뒤집히면 운영자가 반대쪽 공간쌍을 고치러 간다.
        """
        pack.node("a", "A", "Concept", "concept")
        pack.node("b", "B", "Evidence", "evidence")
        pack.edge("a", "b", "정합불가라벨")           # FIX 치환만 발생(위반 아님)
        pack.validate()
        fix_block = capsys.readouterr().out.split("자동치환(FIX)")[1]
        line = next(ln for ln in fix_block.splitlines() if "정합불가라벨" in ln)
        assert "concept→evidence" in line, f"엣지 방향 그대로여야 한다: {line}"
        assert "evidence→concept" not in line

    def test_detail_lines_are_capped_at_eight(self, pack, capsys):
        """상위 8건만 나열한다. 캡이 바뀌면 운영자가 보는 정보량이 조용히 달라진다."""
        # community -> subject 는 양방향 모두 grammar 에 없어 드롭된다.
        # 라벨이 서로 달라야 _eskip 키가 9종으로 갈린다.
        for i in range(9):
            pack.node(f"co{i}", "CO", "Community", "community")
            pack.node(f"s{i}", "S", "Org", "subject")
            pack.edge(f"co{i}", f"s{i}", f"라벨{i}")
        assert pack.edges == [], "이 쌍은 드롭돼야 이 검사가 성립한다"
        pack.save()
        out = capsys.readouterr().out
        detail = [ln for ln in out.splitlines() if "community→subject" in ln]
        assert len(detail) == 8, f"상위 8건이어야 한다: {len(detail)}"
        assert "드롭 엣지 9건" in out, "총계는 자르지 않는다"

    def test_save_space_histogram_is_labelled(self, pack, capsys):
        pack.node("n1", "L", "Concept", "concept")
        pack.save()
        assert "  spaces:" in capsys.readouterr().out

    def test_error_message_names_which_gate_blocked(self, pack):
        """차단 사유가 strict 항목인지 항상차단 항목인지 문구로 구분된다.

        예전 문구가 PACK_LIB_STRICT 만 언급해 "env 를 끄면 우회된다"는 오해를 낳았다.
        두 문구를 각각 못박는다 — 빈 문자열로 바꾸는 변이가 둘 다 살아남았다.
        """
        pack.node("n1", "L", "Concept", "concept")
        pack.nodes[0]["brand"] = "Yamaha"
        with pytest.raises(ValueError, match=r"항상 차단 항목\(strict 무관\)"):
            pack.validate(strict=False)
        with pytest.raises(ValueError, match="PACK_LIB_STRICT=1 항목 또는 항상 차단 항목"):
            pack.validate(strict=True)

    def test_hazard_detail_names_the_loader_space_not_the_declared_one(
            self, pack, capsys):
        """함정 줄은 `선언 space -> 로더 space` 를 보여준다. 두 값이 뒤바뀌면 무의미하다."""
        pack.node("n1", "L", "TextUnit", "evidence")
        pack.validate()
        line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if "node_type='TextUnit'" in ln)
        assert "선언 evidence" in line and "로더 concept" in line

    def test_multiple_errors_are_joined_not_truncated(self, pack):
        """차단 사유가 여럿이면 전부 보여야 한다 — 하나만 고치고 다시 도는 낭비를 막는다."""
        pack.node("n1", "L", "TextUnit", "evidence")
        pack.nodes[0]["brand"] = "Yamaha"
        with pytest.raises(ValueError) as ei:
            pack.validate(strict=True)
        msg = str(ei.value)
        assert "비구조 키" in msg and "remap 함정" in msg
        assert "; " in msg


# ---------------------------------------------------------------------------
# __all__ — 레거시 빌더가 모듈 속성으로 접근하는 이름들
# ---------------------------------------------------------------------------

class TestPublicSurface:
    """`__all__` 은 장식이 아니라 레거시 빌더의 접근 계약이다.

    `<이 모듈>.json.dumps(...)`, `<이 모듈>.ALL_SPACES`, `<이 모듈>._NODE_STRUCT_KEYS`
    처럼 모듈을 통해 접근하는 빌더가 있다(실측 3곳/2곳/1곳). 스윕에서 `__all__` 항목
    문자열을 바꾸는 변이 6 건이 전부 생존했다.
    """

    def test_all_names_exist_on_the_module(self):
        missing = [n for n in build_mod.__all__ if not hasattr(build_mod, n)]
        assert not missing, f"__all__ 에 있는데 모듈에 없다: {missing}"

    def test_documented_legacy_names_are_exported(self):
        assert set(build_mod.__all__) == {
            "Pack", "ALL_SPACES", "DEFAULT_OUT_ROOT",
            "json", "_NODE_STRUCT_KEYS", "_RESERVED_NODE_KEYS"}

    def test_star_import_provides_the_legacy_names(self):
        ns: dict = {}
        exec("from opencrab.pack.build import *", ns)      # noqa: S102
        for n in ("Pack", "ALL_SPACES", "json",
                  "_NODE_STRUCT_KEYS", "_RESERVED_NODE_KEYS"):
            assert n in ns, f"{n} 이 star import 로 안 나온다"

    def test_underscore_aliases_are_the_schema_tables_themselves(self):
        """별칭이 다른 객체를 가리키면 계약이 조용히 이중화된다."""
        assert build_mod._NODE_STRUCT_KEYS is NODE_STRUCT_KEYS
        assert build_mod._NTO is NODE_TYPE_OVERRIDE


# ---------------------------------------------------------------------------
# 기본값 — 호출부가 생략하는 값이라 무검사로 남기 쉽다
# ---------------------------------------------------------------------------

class TestDefaults:
    """생략 가능한 인자의 기본값. **호출부 다수가 이 값에 의존한다.**

    적대 검증이 `source_type='reference-public'` 를 `'reference-private'` 로 바꿨는데
    57 건이 전부 통과했다(2026-08-05). 기존 검사가 override 경로만 봤기 때문이다.
    실측: 호스트 리포의 `Pack()` 대입 60 건 중 `source_type` 을 명시하는 것은 22 건
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
