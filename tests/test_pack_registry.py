from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencrab.ontology.pack_registry import (
    PER_PACK_PROBE_LIMIT,
    PackInfo,
    _choose_by_content,
    build_candidate_registry,
    choose_packs,
    get_pack,
    load_pack_registry,
    score_pack,
)


def _write_manifest(root: Path, pack_id: str, manifest: dict) -> Path:
    stage = root / "packs" / pack_id / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_t1_load_pack_registry_two_manifests(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "nemo-personas-v2", {
        "pack_id": "nemo-personas-v2",
        "title": "NVIDIA Nemotron Personas Korea",
        "version": "2.1.0",
        "description": "한국어 페르소나 9축 온톨로지",
        "source": {"label": "nvidia/Nemotron-Personas-Korea", "url": "https://example"},
        "counts": {"nodes": 5414, "edges": 12715},
    })
    _write_manifest(tmp_path, "unrelated-corpus", {
        "pack_id": "unrelated-corpus",
        "title": "Old construction reports",
        "version": "0.1.0",
        "description": "건설 보고서 모음",
        "counts": {"nodes": 10},
    })

    registry = load_pack_registry(tmp_path)
    pack_ids = {p.pack_id for p in registry}
    assert pack_ids == {"nemo-personas-v2", "unrelated-corpus"}

    nemo = get_pack(tmp_path, "nemo-personas-v2")
    assert nemo is not None
    assert nemo.title.startswith("NVIDIA")
    assert nemo.counts["nodes"] == 5414


def test_t1_load_pack_registry_empty(tmp_path: Path) -> None:
    assert load_pack_registry(tmp_path) == []


def test_t1_load_pack_registry_skips_missing_fields(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "lean-pack", {"pack_id": "lean-pack"})
    registry = load_pack_registry(tmp_path)
    assert len(registry) == 1
    pack = registry[0]
    assert pack.title == ""
    assert pack.counts == {}
    assert pack.keywords == []


def test_t2_choose_packs_top1_with_korean_alias(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "nvidia-nemotron-personas-korea-ontology-v2-1", {
        "pack_id": "nvidia-nemotron-personas-korea-ontology-v2-1",
        "title": "NVIDIA Nemotron Personas Korea — 9-axis stratified ontology pack",
        "description": (
            "한국어 합성 페르소나의 다양성, 분포 한계, 정책 제약, 샘플링 레버를 "
            "질의하면서도 운영 가능한 크기의 그래프를 유지하는 데 유용합니다."
        ),
        "source": {"label": "nvidia/Nemotron-Personas-Korea"},
    })
    _write_manifest(tmp_path, "ko-construction-spec", {
        "pack_id": "ko-construction-spec",
        "title": "한국 건설 시방 표준",
        "description": "방수/방염 시공 표준 문서 모음",
    })
    registry = load_pack_registry(tmp_path)
    candidates = choose_packs("네모트론 페르소나 직업 다양성", registry, limit=1)
    assert candidates, "auto-pack should pick at least one pack"
    pack, score, matched = candidates[0]
    assert pack.pack_id == "nvidia-nemotron-personas-korea-ontology-v2-1"
    assert score > 0
    assert matched


def test_t2_choose_packs_returns_empty_below_threshold(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "weather-data", {
        "pack_id": "weather-data",
        "title": "Daily weather observations",
        "description": "rainfall and humidity",
    })
    registry = load_pack_registry(tmp_path)
    assert choose_packs("페르소나 분포", registry, limit=1, min_score=50.0) == []


def test_t2_choose_packs_explicit_min_score_override(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "tiny", {"pack_id": "tiny", "title": "tiny"})
    registry = load_pack_registry(tmp_path)
    candidates = choose_packs("tiny", registry, limit=1, min_score=0.0)
    assert candidates and candidates[0][0].pack_id == "tiny"


@pytest.mark.parametrize("env_value,expected_min", [(None, 10.0), ("1", 1.0)])
def test_t2_env_min_score_default(monkeypatch, tmp_path: Path, env_value, expected_min) -> None:
    _write_manifest(tmp_path, "tiny", {"pack_id": "tiny", "title": "tiny"})
    registry = load_pack_registry(tmp_path)
    if env_value is None:
        monkeypatch.delenv("OPENCRAB_AUTO_PACK_MIN_SCORE", raising=False)
    else:
        monkeypatch.setenv("OPENCRAB_AUTO_PACK_MIN_SCORE", env_value)
    candidates = choose_packs("tiny", registry, limit=1)
    # The "tiny" pack scores 100 + 50 (pack_id + title), well above either threshold.
    assert candidates and candidates[0][1] >= expected_min


# ---------------------------------------------------------------------------
# #400: score_pack의 whole-토큰 게이트. 질의는 whole 토큰(공백/구두점 경계)
# 으로만 판정하고, fragment(n-gram 조각)는 판정 통과 뒤 순위에만 쓴다. 팩
# 쪽 게이트 집합은 기존 _tokens()(fragment 포함)와 신규 _whole_tokens()
# (길이 무관)의 합집합이다. design 문서:
# /home/asdf/orch-scratch/o400/design-v5-full.md §4.1/§6/§10.
# ---------------------------------------------------------------------------


def test_t400_single_char_literal_match_no_regression() -> None:
    """title="혈" + 질의 "혈" -- 라운드5 필수조건2. 팩 쪽 게이트가
    길이 2 미만을 버리는 기존 _tokens()만 썼다면 이 리터럴 매치가 막힌다."""
    pack = PackInfo(pack_id="p3", title="혈")
    assert score_pack("혈", pack) == (50.0, ["title"])


def test_t400_particle_only_gate_overlap_is_rejected() -> None:
    """라운드5 차단 지적(codex FAIL) 그대로: 게이트 교집합이 조사 "는"
    하나뿐이면 무관 팩을 fragment 점수로 선택해선 안 된다. _STOPWORDS에
    "는"류 단음절 조사가 빠지면 이 assertion이 RED로 되돌아간다."""
    pack = PackInfo(
        pack_id="fixture", title="자리 는", description="자리", keywords=["자리"],
    )
    assert score_pack("혈자리 는", pack) == (0.0, [])
    assert choose_packs("혈자리 는", [pack]) == []


def test_t400_script_boundary_fragment_is_not_a_whole_token() -> None:
    """라운드4 반례a의 실제 검출 케이스(설계 §7 테스트2c). ASCII/숫자와
    한글이 구분자 없이 붙은 질의("x자리"/"혈1자리")는 하나의 whole
    토큰이라 "자리" 단독 팩과 겹치면 안 된다. _WORD_RE/_HANGUL_RE를 따로
    findall하던 구현으로 되돌리면 "x"+"자리"로 잘못 쪼개져 63.0으로 뚫린다."""
    pack = PackInfo(pack_id="p2", title="자리", description="자리", keywords=["자리"])
    assert score_pack("x자리", pack) == (0.0, [])
    assert score_pack("혈1자리", pack) == (0.0, [])


def test_t400_fragment_only_overlap_without_space_is_rejected() -> None:
    """설계 §7 테스트2/4(거부 절반). "혈자리"(공백 없음)는 하나의 whole
    토큰이므로 "자리"만 가진 팩과 fragment로만 겹쳐도 게이트가 안 열린다."""
    pack = PackInfo(pack_id="p2", title="자리", description="자리", keywords=["자리"])
    assert score_pack("혈자리", pack) == (0.0, [])


def test_t400_whole_word_query_matches_exact_field_value() -> None:
    """설계 §7 테스트4(선택 절반, v3의 오분류를 v4에서 정정). 질의 "자리"는
    팩이 실제로 가진 낱말이므로 정상 선택 대상이고 회귀시켜선 안 된다."""
    pack = PackInfo(pack_id="p2", title="자리", description="자리", keywords=["자리"])
    assert score_pack("자리", pack) == (63.0, ["title", "자리"])


def test_t400_space_separated_query_opens_gate_via_pack_side_fragment() -> None:
    """설계 §7 테스트2b(정정). "자리"가 "별자리" 안에만 내장된 무관 팩에
    질의 "혈 자리"(공백 있음)는 게이트는 열리되(의도된 비대칭) 기본
    임계값(10.0) 아래라 선택은 안 되고, 임계값을 낮추면 선택된다. "무조건
    미선택"은 아니다(라운드4 반례c)."""
    pack = PackInfo(
        pack_id="stargazing", title="별자리 관측", description="밤하늘 별자리 사진",
        keywords=["별자리"],
    )
    assert score_pack("혈 자리", pack) == (8.0, ["자리"])
    assert choose_packs("혈 자리", [pack]) == []
    assert choose_packs("혈 자리", [pack], min_score=5) == [(pack, 8.0, ["자리"])]
    # 공백 없는 질의는 하나의 whole 토큰이라 이 fragment 겹침 경로 자체가 안 열린다.
    assert score_pack("혈자리", pack) == (0.0, [])


def test_t400_compound_word_pack_title_matches_spaced_query_unchanged() -> None:
    """설계 §7 테스트3. 붙여쓴 title과 필드에 나뉜 값 둘 다 기존 점수
    그대로 통과해야 한다(무회귀)."""
    concatenated = PackInfo(pack_id="p5", title="직업분포")
    assert score_pack("직업 분포", concatenated) == (10.0, ["분포", "직업"])

    split_fields = PackInfo(
        pack_id="p6", title="직업 안내", description="분포 자료", keywords=["직업"],
    )
    assert score_pack("직업 분포", split_fields) == (13.0, ["직업", "분포"])


def test_t400_hyphenated_pack_id_bonus_via_whole_token_subset() -> None:
    """설계 §7 테스트13. pack_id의 whole 토큰이 질의의 whole 토큰 부분집합이면
    구분자(하이픈 vs 공백)가 달라도 +100 보너스가 붙는다(개선, 기존은 원문
    그대로 일치해야 붙었음). 부분 단어만 쓴 질의는 보너스가 안 붙는다."""
    pack = PackInfo(pack_id="acupoint-medical", title="Acupoint Medical Atlas")
    score, matched = score_pack("acupoint medical", pack)
    assert score == 110.0
    assert "pack_id:acupoint-medical" in matched

    score2, matched2 = score_pack("acupoint", pack)
    assert score2 == 5.0
    assert not any(m.startswith("pack_id:") for m in matched2)


# ---------------------------------------------------------------------------
# #400 §4: _choose_by_content -- BM25/FTS 콘텐츠 폴백. score_pack()의 게이트가
# 전부 닫혔을 때(title/description/keywords/tags 어디에도 리터럴이 없을 때)만
# 쓰는 마지막 안전망. 실제 HybridQuery 대신 pack_id별 히트를 미리 준비해 두는
# 가짜 객체로 격리 시험한다.
# ---------------------------------------------------------------------------


class _FakeHybrid:
    """``hybrid._bm25_search``/``_fts_search``만 흉내 낸다. 호출 인자(질의,
    spaces, limit, pack_ids)를 그대로 기록해 배선(§7 항목10) 확인에 쓴다."""

    def __init__(
        self,
        bm25_by_pack: dict[str, list[dict]] | None = None,
        fts_by_pack: dict[str, list[dict]] | None = None,
    ) -> None:
        self.bm25_by_pack = bm25_by_pack or {}
        self.fts_by_pack = fts_by_pack or {}
        self.bm25_calls: list[tuple] = []
        self.fts_calls: list[tuple] = []

    def _bm25_search(self, question, spaces, limit, *, pack_ids):
        self.bm25_calls.append((question, spaces, limit, tuple(pack_ids)))
        pid = pack_ids[0]
        return list(self.bm25_by_pack.get(pid, []))

    def _fts_search(self, question, spaces, limit, *, pack_ids):
        self.fts_calls.append((question, spaces, limit, tuple(pack_ids)))
        pid = pack_ids[0]
        return list(self.fts_by_pack.get(pid, []))


def test_t400_content_fallback_selects_pack_by_literal_body_text() -> None:
    """설계 §7 항목1. title/description엔 없는 고유명사가 doc 본문에만
    whole 토큰으로 있으면 콘텐츠 폴백이 그 팩을 고른다."""
    pack = PackInfo(pack_id="misc-pack", title="기타 자료", description="분류 없음")
    hybrid = _FakeHybrid(
        bm25_by_pack={"misc-pack": [{"pack_id": "misc-pack", "text": "네오다임 합금 규격", "score": 3.5}]},
    )
    candidates, truncated = _choose_by_content("네오다임", [pack], hybrid, spaces=None)
    assert truncated == []
    assert len(candidates) == 1
    got_pack, score, matched = candidates[0]
    assert got_pack.pack_id == "misc-pack"
    assert matched == ["네오다임"]
    assert score == 10.0  # 기본 OPENCRAB_AUTO_PACK_MIN_SCORE -- 실제 BM25 점수가 아니라 문턱값 자체


def test_t400_content_fallback_rejects_fragment_only_body_hit() -> None:
    """§4.3. 원문 재검증은 substring 스캔이 아니라 whole-토큰 집합 비교다 --
    "art"가 "cartography" 안에서 우연히 잡히는 것과 같은 모양의 오탐을 막는다."""
    pack = PackInfo(pack_id="geo-pack", title="지도", description="지도 제작")
    hybrid = _FakeHybrid(
        bm25_by_pack={"geo-pack": [{"pack_id": "geo-pack", "text": "cartography basics", "score": 9.0}]},
    )
    candidates, _ = _choose_by_content("art", [pack], hybrid, spaces=None)
    assert candidates == []


def test_t400_content_fallback_fts_only_pack_is_selected() -> None:
    """설계 §7 항목5. doc_nodes(BM25)엔 없고 doc_sources(FTS)에만 원문이 있는
    팩도 콘텐츠 폴백으로 정상 선택된다."""
    pack = PackInfo(pack_id="fts-pack", title="본문 전용", description="")
    hybrid = _FakeHybrid(
        fts_by_pack={"fts-pack": [{"metadata": {"pack_id": "fts-pack"}, "text": "표준번호 KS123", "score": 2.0}]},
    )
    candidates, _ = _choose_by_content("KS123", [pack], hybrid, spaces=None)
    assert len(candidates) == 1
    assert candidates[0][0].pack_id == "fts-pack"
    assert candidates[0][2] == ["ks123"]


def test_t400_content_fallback_small_pack_hit_not_shadowed_by_large_pack() -> None:
    """설계 §7 항목6. 팩별 개별 조회이므로 큰 팩의 히트 수가 많아도 작은 팩의
    (매치 없는) 히트가 작은 팩의 조회 자체를 가리지 않는다."""
    big = PackInfo(pack_id="big-pack", title="대형", description="")
    small = PackInfo(pack_id="small-pack", title="소형", description="")
    hybrid = _FakeHybrid(
        bm25_by_pack={
            "big-pack": [{"pack_id": "big-pack", "text": "무관 본문", "score": 1.0}],
            "small-pack": [{"pack_id": "small-pack", "text": "고유토큰123 매치", "score": 1.0}],
        },
    )
    candidates, _ = _choose_by_content("고유토큰123", [big, small], hybrid, spaces=None)
    assert len(candidates) == 1
    assert candidates[0][0].pack_id == "small-pack"


def test_t400_content_fallback_not_invoked_when_lexical_gate_already_selected() -> None:
    """설계 §7 항목7 (회귀 대조군). choose_packs가 이미 후보를 낸 경우
    resolve_packs 수준에서는 콘텐츠 폴백을 아예 부르지 않는다 -- 이 테스트는
    _choose_by_content 자체가 아니라 그 전제(폴백은 lexical이 빈 리스트일
    때만 실행)를 score_pack을 통해 재확인한다."""
    pack = PackInfo(pack_id="p2", title="자리", description="자리", keywords=["자리"])
    assert choose_packs("자리", [pack]) != []  # lexical 경로가 이미 후보를 낸다 -- 폴백 불필요 조건


def test_t400_content_fallback_truncation_is_observed() -> None:
    """설계 §7 항목9. 팩 하나의 히트 수가 PER_PACK_PROBE_LIMIT에 닿으면
    truncated_packs에 나타난다 -- 선택 결과 자체에는 영향 없음."""
    hits = [{"pack_id": "cap-pack", "text": f"항목{i} 고유토큰", "score": 1.0} for i in range(PER_PACK_PROBE_LIMIT)]
    pack = PackInfo(pack_id="cap-pack", title="", description="")
    hybrid = _FakeHybrid(bm25_by_pack={"cap-pack": hits})
    candidates, truncated = _choose_by_content("고유토큰", [pack], hybrid, spaces=None)
    assert truncated == ["cap-pack"]
    assert len(candidates) == 1


def test_t400_content_fallback_forwards_spaces_to_both_legs() -> None:
    """설계 §7 항목10. spaces 인자가 BM25/FTS 양쪽 조회 호출에 그대로 전달된다."""
    pack = PackInfo(pack_id="p1", title="", description="")
    hybrid = _FakeHybrid()
    _choose_by_content("무관질의", [pack], hybrid, spaces=["space-a"])
    assert hybrid.bm25_calls[0][1] == ["space-a"]
    assert hybrid.fts_calls[0][1] == ["space-a"]


def test_t400_content_fallback_tiebreak_prefers_higher_summed_score() -> None:
    """설계 §7 항목11. 매치된 고유 토큰 수가 같으면(둘 다 1개) 누적 BM25/FTS
    점수 합이 더 큰 팩이 선택된다."""
    low = PackInfo(pack_id="low-pack", title="", description="")
    high = PackInfo(pack_id="high-pack", title="", description="")
    hybrid = _FakeHybrid(
        bm25_by_pack={
            "low-pack": [{"pack_id": "low-pack", "text": "고유토큰", "score": 1.0}],
            "high-pack": [{"pack_id": "high-pack", "text": "고유토큰", "score": 9.0}],
        },
    )
    candidates, _ = _choose_by_content("고유토큰", [low, high], hybrid, spaces=None)
    assert len(candidates) == 1
    assert candidates[0][0].pack_id == "high-pack"


def test_t400_content_fallback_reported_score_follows_min_score_override() -> None:
    """설계 §7 항목12. OPENCRAB_AUTO_PACK_MIN_SCORE를 바꾸면(여기서는
    min_score 인자로) 콘텐츠 폴백이 보고하는 점수도 그 값을 따른다 --
    실제 BM25 점수가 아니라 문턱값 자체이기 때문이다(§4.4)."""
    pack = PackInfo(pack_id="p1", title="", description="")
    hybrid = _FakeHybrid(bm25_by_pack={"p1": [{"pack_id": "p1", "text": "고유토큰", "score": 1.0}]})
    candidates, _ = _choose_by_content("고유토큰", [pack], hybrid, spaces=None, min_score=42.0)
    assert candidates[0][1] == 42.0


# ---------------------------------------------------------------------------
# #397: build_candidate_registry -- SQL rows are the candidate scope,
# manifests only enrich fields.
# ---------------------------------------------------------------------------


def test_build_candidate_registry_excludes_manifest_only_packs(tmp_path: Path) -> None:
    """A pack_id with a manifest but no SQL row must not enter the registry.

    The SQL packs table is the read-scope authority (#143); admitting a
    manifest-only pack here would open a path around that scope.
    """
    _write_manifest(tmp_path, "manifest-only", {
        "pack_id": "manifest-only",
        "title": "manifest only pack",
    })
    fs_packs = load_pack_registry(tmp_path)
    registry = build_candidate_registry([], fs_packs)
    assert registry == []


def test_build_candidate_registry_sql_row_with_no_manifest_still_becomes_a_candidate(
    tmp_path: Path,
) -> None:
    """The 185-of-186 case: a SQL-registered pack with no manifest at all
    must still be scoreable -- this is the #397 root-cause fix itself."""
    sql_rows = [{"pack_id": "sql-only", "title": "quantum widget research", "description": ""}]
    registry = build_candidate_registry(sql_rows, [])
    assert len(registry) == 1
    assert registry[0].pack_id == "sql-only"
    assert registry[0].title == "quantum widget research"
    candidates = choose_packs("quantum widget research", registry, limit=1)
    assert candidates and candidates[0][0].pack_id == "sql-only"


def test_build_candidate_registry_sql_title_wins_over_manifest(tmp_path: Path) -> None:
    """pack_publish keeps the SQL row current; the manifest is a load-time
    snapshot. A non-empty SQL title/description must win."""
    _write_manifest(tmp_path, "dual", {
        "pack_id": "dual",
        "title": "stale manifest title",
        "description": "stale manifest description",
    })
    fs_packs = load_pack_registry(tmp_path)
    sql_rows = [{"pack_id": "dual", "title": "fresh sql title", "description": "fresh sql description"}]
    registry = build_candidate_registry(sql_rows, fs_packs)
    assert len(registry) == 1
    assert registry[0].title == "fresh sql title"
    assert registry[0].description == "fresh sql description"


def test_build_candidate_registry_manifest_fills_empty_sql_title(tmp_path: Path) -> None:
    """When the SQL row's title/description is empty, the manifest value
    fills it instead of leaving the candidate untitled."""
    _write_manifest(tmp_path, "dual", {
        "pack_id": "dual",
        "title": "manifest title",
        "description": "manifest description",
    })
    fs_packs = load_pack_registry(tmp_path)
    sql_rows = [{"pack_id": "dual", "title": "", "description": None}]
    registry = build_candidate_registry(sql_rows, fs_packs)
    assert len(registry) == 1
    assert registry[0].title == "manifest title"
    assert registry[0].description == "manifest description"


def test_build_candidate_registry_enriches_manifest_only_fields(tmp_path: Path) -> None:
    """source_label/keywords/tags have no SQL equivalent -- they must be
    carried over from the manifest whenever one exists for the pack_id."""
    _write_manifest(tmp_path, "dual", {
        "pack_id": "dual",
        "title": "manifest title",
        "source": {"label": "arxiv"},
        "keywords": ["kw1", "kw2"],
        "tags": ["tag1"],
    })
    fs_packs = load_pack_registry(tmp_path)
    sql_rows = [{"pack_id": "dual", "title": "sql title", "description": ""}]
    registry = build_candidate_registry(sql_rows, fs_packs)
    assert len(registry) == 1
    assert registry[0].source_label == "arxiv"
    assert registry[0].keywords == ["kw1", "kw2"]
    assert registry[0].tags == ["tag1"]
