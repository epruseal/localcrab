"""팩 소스 포맷의 계약(contract) 정본.

`<팩>/{nodes,edges,chunks}.jsonl` 은 생산자(팩 빌더)와 소비자(적재기, 채점기,
cloud-pack 조립기)가 공유하는 교환 포맷이다. 이 모듈이 그 포맷을 코드로 정의한다.
이전에는 정의가 어디에도 없고 생산자와 소비자가 서로 다른 리포에서 같은 규칙을
각자 재선언했으며, 그 결과 두 번의 실사고가 났다.

1. 노드 커스텀 필드 91만 건이 라이브에 도달하지 못했다. 생산자는 props 를 노드
   최상위에 펼쳤고 소비자는 중첩 ``properties`` 만 읽었다. 어느 게이트도 잡지
   못했다(2026-08-03 발각).
2. 엣지 방향 정규화 규칙이 생산 쪽에 없어, 파일과 라이브의 엣지 수 차이가 유실인지
   판정하려면 저장소 스키마를 역공학해야 했다(2026-08-04).

따라서 이 모듈의 상수는 **복제 금지**다. 값이 필요하면 여기서 import 한다.

물리 레이아웃(단일 파일 대 shard 분할)은 :mod:`opencrab.pack.jsonl_io` 가 정의한다.
이 모듈은 레코드 한 건의 모양만 다룬다.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from opencrab.grammar.manifest import META_EDGES

# 9-space. 노드 ``space`` 필드가 취할 수 있는 값의 전부다.
ALL_SPACES: tuple[str, ...] = (
    "subject", "resource", "evidence", "concept", "claim",
    "community", "outcome", "lever", "policy",
)

# ── 레코드 구조 필드 ────────────────────────────────────────────

# 노드 최상위에 허용되는 키. 이 밖의 최상위 키는 커스텀 필드가 잘못된 위치에
# 있다는 뜻이다(아래 absorb_legacy_top_level 참조).
#
# ``degree`` 는 생산자가 쓰지 않지만 적재기·게이트·채점기가 구조 키로 취급한다.
# 집합이 어긋나면 "빌드는 죽는데 게이트는 통과"하는 비대칭이 생기므로 맞춘다.
NODE_STRUCT_KEYS: frozenset[str] = frozenset({
    "id", "workspace_id", "label", "node_type", "space",
    "source_type", "created_at", "properties", "degree",
})

EDGE_STRUCT_KEYS: frozenset[str] = frozenset({
    "id", "workspace_id", "source_id", "target_id", "label",
    "created_at", "properties",
})

CHUNK_STRUCT_KEYS: frozenset[str] = frozenset({
    "id", "document_id", "workspace_id", "text", "source",
    "source_type", "created_at", "metadata",
})

# 노드 props 가 덮어써서는 안 되는 최상위 필드.
#
# ``id``/``workspace_id``: 덮이면 nodes.jsonl 에 유령 id 가 실리는데 생산자의 내부
#   장부에는 원래 값이 남아 dangling 검사를 그대로 통과한다(2026-08-03 실측 재현).
# ``node_type``/``space``: 덮이면 space 맵과 어긋나 grammar 라우팅이 잘못된다.
# ``label``/``created_at``: 조용한 표시 오염.
#
# ``source_type`` 은 일부러 뺀다. 팩 기본값을 노드별로 덮는 것이 의도된 기능이고
# (생성본을 self-authored 로 구분하는 등) 내부 장부가 참조하지 않아 안전하다.
RESERVED_NODE_KEYS: frozenset[str] = frozenset({
    "id", "workspace_id", "label", "node_type", "space", "created_at",
})


# ── grammar 정책 ────────────────────────────────────────────────

def _allowed_from_manifest() -> dict[tuple[str, str], frozenset[str]]:
    """(from_space, to_space) -> 허용 relation 집합, grammar manifest 에서 직접 유도.

    스냅샷을 뜨지 않는다. 호출자 쪽에 이 표를 손으로 베낀 사본이 있었고 "변경 시
    동기화" 주석에 정합성을 맡기고 있었다. 유도로 바꾸면 드리프트가 구조적으로
    불가능해진다.
    """
    return {
        (e["from_space"], e["to_space"]): frozenset(e["relations"])
        for e in META_EDGES
    }


ALLOWED: dict[tuple[str, str], frozenset[str]] = _allowed_from_manifest()

# 공간쌍별 대표 relation. 정합하지 않는 라벨을 이 값으로 치환한다.
# manifest 에서 유도할 수 없는 편집 판단이므로(어느 relation 이 "기본"인지는
# 의미론이다) 데이터로 유지한다. 단 키 집합은 ALLOWED 와 일치해야 하며
# 아래 모듈 말미의 정합성 검사가 이를 강제한다.
FIX: dict[tuple[str, str], str] = {
    ("subject", "resource"): "owns",
    ("resource", "evidence"): "contains",
    ("evidence", "concept"): "mentions",
    ("evidence", "claim"): "supports",
    ("concept", "concept"): "related_to",
    ("concept", "outcome"): "contributes_to",
    ("resource", "concept"): "mentions",
    ("lever", "outcome"): "raises",
    ("lever", "concept"): "affects",
    ("community", "concept"): "summarizes",
    ("policy", "resource"): "classifies",
    ("policy", "subject"): "requires_approval",
    ("claim", "outcome"): "supports",
    ("claim", "policy"): "complies_with",
    ("community", "evidence"): "evidenced_by",
    ("concept", "claim"): "measured_by",
    ("concept", "community"): "serves",
    ("concept", "evidence"): "evidenced_by",
    ("concept", "lever"): "measured_by",
    ("concept", "resource"): "measured_by",
    ("lever", "evidence"): "evidenced_by",
    ("outcome", "evidence"): "evidenced_by",
    ("policy", "community"): "protects",
    ("policy", "evidence"): "cites",
    ("policy", "outcome"): "ensures",
    ("resource", "claim"): "states",
    ("resource", "lever"): "has_mode",
    ("resource", "policy"): "defines",
    ("subject", "claim"): "governs",
    ("subject", "concept"): "defines",
    ("subject", "lever"): "measured_by",
    ("subject", "outcome"): "targets",
    ("subject", "policy"): "governs",
    ("subject", "evidence"): "evidenced_by",
    ("subject", "subject"): "has_category",
    ("concept", "policy"): "governs",
    ("policy", "concept"): "scopes",
    ("resource", "resource"): "cites",
}

# 적재기가 방향을 뒤집어 저장하는 공간쌍. 생산자는 라벨을 그대로 두고
# 파일에는 claim -> evidence 로 적는다. 적재기가 evidence --supports--> claim
# 으로 반전시킨다. 파일과 라이브의 엣지 수가 달라 보이는 원인이 이것이다.
KEEP: dict[tuple[str, str], str] = {("claim", "evidence"): "EVIDENCED_BY"}

# traceability 근원 공간. 이 공간에서 evidence/resource 로 향하는 엣지는
# 방향을 유지한다(반전하면 채점기의 근거 연결 계산이 무너진다).
TRACE_SRC: frozenset[str] = frozenset({"claim", "lever", "outcome", "policy"})


# ── node_type -> (space, node_type) 정규화 표 ───────────────────
#
# 팩은 도메인 어휘로 node_type 을 쓴다("LawArticle", "Quote", "DesignToken"). grammar 는
# 9-space 별 정해진 타입만 안다. 적재기가 이 표로 번역한다.
#
# **이 표는 생산자에게도 계약이다.** 선언한 space 와 표가 지목하는 space 가 다르면
# 적재 시 노드가 다른 space 로 재태깅되고, 그 노드에 붙은 엣지가 grammar 위반으로
# 통째로 skip 된다(예: evidence 노드에 node_type="TextUnit" -> 적재기가 concept 으로
# remap -> 연결 엣지 전량 유실). 그래서 빌더의 validate() 가 이 표를 보고 함정을 경고한다.
# 이관 전에는 이 표가 적재기에만 있어 빌더가 역방향 import(try/except) 로 겨우 참조했다.
NODE_TYPE_OVERRIDE: Mapping[str, tuple[str, str]] = MappingProxyType({
    # codex 구형 (evidence -> concept 수정)
    "TextUnit":             ("concept",    "Topic"),
    # graphrag/구형 Entity(필수 prop name·entity_type 미보유) -> Concept 통합
    # (aprilia-mana-850 등 concept/Entity 노드 skip 방지; original_type 보존)
    "Entity":               ("concept",    "Concept"),
    # 9-space 구조: axis 노드 -> concept/Concept 통합 (구조적 메타 노드)
    "OntologyAxis":         ("concept",    "Concept"),
    # krds 커스텀 타입
    "DesignSystem":         ("subject",    "Org"),
    "ComponentCategory":    ("subject",    "Team"),
    "Designer":             ("community",  "Community"),
    "Developer":            ("community",  "Community"),
    "GovernmentStaff":      ("community",  "Community"),
    "Citizen":              ("community",  "Community"),
    "ElderlyUser":          ("community",  "Community"),
    "UserWithDisability":   ("community",  "Community"),
    "LowVisionUser":        ("community",  "Community"),
    "DesignOutcome":        ("outcome",    "Outcome"),
    "DesignPolicy":         ("policy",     "Policy"),
    "Component":            ("concept",    "Concept"),
    "ComponentHTML":        ("resource",   "Document"),
    "ComponentSCSS":        ("resource",   "Document"),
    "IconSet":              ("resource",   "Document"),
    "TokenFile":            ("resource",   "Document"),
    "GuidelinePage":        ("resource",   "Document"),
    "DesignToken":          ("concept",    "Concept"),
    "ComponentVariant":     ("lever",      "Lever"),
    "TokenMode":            ("lever",      "Lever"),
    "GuidelineEvidence":    ("evidence",   "Evidence"),
    "MarkupEvidence":       ("evidence",   "Evidence"),
    "TokenEvidence":        ("evidence",   "Evidence"),
    "DesignPrinciple":      ("policy",     "Policy"),
    "UsageGuideline":       ("claim",      "Claim"),
    "AccessibilityGuideline": ("claim",    "Claim"),
    # 공공데이터품질관리 커스텀 타입 + 이미지 evidence
    "TextEvidence":         ("evidence",   "Evidence"),
    "VisualEvidence":       ("evidence",   "Evidence"),
    "Guideline":            ("resource",   "Document"),  # 지침서=문서, policy->resource:classifies 경로 활용
    "DiagnosisCriterion":   ("claim",      "Claim"),
    "QualityIndicator":     ("concept",    "Concept"),
    "Article":              ("resource",   "Document"),
    "Principle":            ("policy",     "Policy"),
    "Law":                  ("resource",   "Document"),
    "QualityAttribute":     ("concept",    "Concept"),
    "LawText":              ("resource",   "Document"),  # 법령 조문=출처문서, evidence 아님
    "Domain":               ("concept",    "Concept"),
    "StandardArtifact":     ("resource",   "Document"),
    "Stakeholder":          ("community",  "Community"),
    "Role":                 ("subject",    "Team"),
    "ProvisionMode":        ("lever",      "Lever"),
    "WebPage":              ("resource",   "Document"),
    "Committee":            ("subject",    "Team"),
    "Center":               ("subject",   "Org"),
    "Agency":               ("subject",    "Org"),
    "Governance":           ("policy",     "Policy"),
    # 전자정부사업관리
    "AdminRule":            ("resource",   "Document"),   # 행정규칙=공식문서, resource->resource:cites 경로 활용
    "Program":              ("subject",    "Org"),
    "RoleCategory":         ("subject",    "Team"),
    "Authority":            ("subject",    "Org"),
    "StandardDeliverable":  ("resource",   "Document"),
    "Deliverable":          ("resource",   "Document"),
    "DeliverableCatalog":   ("resource",   "Document"),
    "LawArticle":           ("resource",   "Document"),
    "Guide":                ("resource",   "Document"),
    "Manual":               ("resource",   "Document"),
    "Stage":                ("concept",    "Concept"),
    "SecurityWeaknessCategory": ("concept", "Concept"),
    "Lifecycle":            ("concept",    "Concept"),
    "Sensitivity":          ("policy",     "Sensitivity"),   # grammar: policy/Sensitivity
    "ApprovalRule":         ("policy",     "ApprovalRule"),  # grammar: policy/ApprovalRule
    "Risk":                 ("outcome",    "Risk"),          # grammar: outcome/Risk
    "CommunityReport":      ("community",  "CommunityReport"),  # grammar: community/CommunityReport
    "KPI":                  ("outcome",    "KPI"),           # grammar: outcome/KPI
    # 명문장1007
    "Quote":                ("evidence",   "Evidence"),
    "Work":                 ("resource",   "Document"),
    "Author":               ("subject",    "Org"),      # 행위자: owns/has_category 엣지 필요
    "Film":                 ("resource",   "Document"),
    "Poem":                 ("resource",   "Document"),
    "Play":                 ("resource",   "Document"),
    "Scripture":            ("resource",   "Document"),
    "Collection":           ("concept",    "Concept"),  # space=concept — related_to/part_of 엣지 경로
    "Character":            ("subject",    "Org"),      # 행위자: has_category 엣지 필요
    "Speech":               ("resource",   "Document"), # space=resource — contains/owns 엣지 경로
    "Source":               ("subject",    "Org"),      # space=subject — has_category 엣지 경로
    "AuthorIndex":          ("subject",    "Org"),
    "Theme":                ("concept",    "Concept"),
    "Genre":                ("concept",    "Concept"),
})

# node_type 이 NODE_TYPE_OVERRIDE 에도 없을 때 space 로 기본 타입 결정.
SPACE_DEFAULT_TYPE: Mapping[str, tuple[str, str]] = MappingProxyType({
    "evidence":  ("evidence",  "Evidence"),
    "resource":  ("resource",  "Document"),
    "subject":   ("subject",   "Org"),
    "concept":   ("concept",   "Concept"),
    "lever":     ("lever",     "Lever"),
    "outcome":   ("outcome",   "Outcome"),
    "policy":    ("policy",    "Policy"),
    "claim":     ("claim",     "Claim"),
    "community": ("community", "Community"),
})


# ── 레거시 흡수 규칙 ────────────────────────────────────────────

def absorb_legacy_top_level(row: dict[str, Any]) -> dict[str, Any]:
    """노드 행에서 "실질 properties" 를 만든다 — 최상위 커스텀 필드 흡수 포함.

    2026-08-03 이전 생산자는 커스텀 필드를 노드 최상위에 펼쳤다. 그 팩들은 재빌드
    없이 살아 있어야 하므로(디스크에 91만 건이 그대로 있다) 소비자는 최상위 잔여
    키도 properties 로 흡수한다.

    **중첩이 우선한다.** 정본 위치가 중첩이기 때문이다. 두 위치에 같은 키가 서로
    다른 값으로 있으면 최상위 값이 버려진다 — 2026-08-04 전수 실측에서 그런 노드는
    0건이었으므로 현재 데이터에서 이 규칙은 무손실이다.

    반환값은 새 dict 다. ``row`` 는 변경하지 않는다.
    """
    nested = dict(row.get("properties") or {})
    stray = {k: v for k, v in row.items() if k not in NODE_STRUCT_KEYS}
    if not stray:
        return nested
    merged = dict(stray)
    merged.update(nested)
    return merged


def stray_top_level_keys(row: dict[str, Any]) -> dict[str, Any]:
    """노드 행의 최상위 비구조 키만 추린다(진단·게이트용)."""
    return {k: v for k, v in row.items() if k not in NODE_STRUCT_KEYS}


# ── 검증 ────────────────────────────────────────────────────────

class PackSchemaError(ValueError):
    """팩 레코드가 계약을 어겼다.

    **진단은 문자열이 아니라 속성으로도 노출한다.** 소비자(적재기·게이트·리포트)가
    "어느 행의 어느 필드가 문제인가"를 알아야 하는데, 메시지 문구를 파싱하게 하면
    문구를 다듬는 순간 소비자가 깨진다. 실제로 그 계약을 문자열 검사로 지키려다
    단언을 네 번 고쳤고, 마지막에도 부정문 우회가 남았다
    (`... 가 없다? 아니다, 있다: ...` 가 통과, 2026-08-06 적대 검증).

    **문구도 계약이다 — 속성만으로는 부족하다.** 처음엔 "문구는 리뷰 사안"이라고 판단했는데
    틀렸다. MCP 도구가 예외를 `{"error": str(exc)}` 로 감싸 그대로 응답에 싣고
    (`opencrab/mcp/tools/graph.py:128,202`, `_registry.py:82`), 적재기·API 도 `str(exc)` 만
    로그에 남긴다. `missing_field`/`row_id` 는 현재 이 모듈과 테스트 밖에서 아무도 안 읽는다.
    즉 **거짓 문구가 운영 사용자에게 그대로 도달하는 실경로가 있다**(적대 검증이 MCP handler 에
    거짓 메시지를 주입해 실증, 2026-08-06).

    그래서 문구를 **속성에서 생성**한다. 세 검사가 각자 f-string 을 손으로 쓰면 문구와 속성이
    어긋날 수 있지만, 아래 :meth:`missing_required` 를 통하면 어긋날 수가 없다 — 템플릿이
    한 곳뿐이고 그 한 곳만 검사하면 된다.
    """

    def __init__(self, message: str, *, missing_field: str | None = None,
                 row_id: Any = None) -> None:
        super().__init__(message)
        self.missing_field = missing_field
        self.row_id = row_id

    @classmethod
    def missing_required(cls, kind: str, key: str, row_id: Any) -> PackSchemaError:
        """필수 필드 부재 오류. **문구와 속성을 한 자리에서 함께 만든다.**

        호출부가 f-string 을 직접 쓰지 못하게 하는 것이 요점이다. 문구를 바꾸려면 이
        템플릿을 고쳐야 하고, 그러면 이 한 곳을 보는 테스트가 곧바로 잡는다.
        """
        return cls(f"{kind}에 필수 필드 {key!r} 가 없다: {row_id!r}",
                   missing_field=key, row_id=row_id)


def validate_node_props(nid: Any, props: dict[str, Any] | None) -> None:
    """생산자가 ``node()`` 에 넘기는 props 가 규약을 지키는지 검사한다.

    props 는 **평면 dict** 다. 중첩은 생산자가 한다. 이미 감싼 형태를 넘기면
    ``properties.properties`` 가 되어 조용히 한 겹 깊어지므로 명시적으로 실패시킨다
    — 호출 규약은 하나여야 한다.
    """
    if not props:
        return
    if "properties" in props:
        raise PackSchemaError(
            f"node({nid!r}) props 에 'properties' 키가 있다. props 는 평면 dict 로 넘겨라. "
            "중첩은 빌더가 알아서 한다.")
    clash = RESERVED_NODE_KEYS & props.keys()
    if clash:
        raise PackSchemaError(
            f"node({nid!r}) props 에 예약 키 {sorted(clash)} 가 있다. "
            f"예약 키: {sorted(RESERVED_NODE_KEYS)}. 다른 이름을 쓰라.")


def validate_node(row: dict[str, Any], *, allow_legacy_top_level: bool = True) -> None:
    """nodes.jsonl 한 행을 검사한다.

    ``allow_legacy_top_level=False`` 로 부르면 최상위 커스텀 필드를 계약 위반으로
    본다. 새로 만든 팩에 쓴다 — 기존 팩은 레거시 흡수 대상이라 True 여야 통과한다.
    """
    # 메시지 안의 `row.get('id')` 는 **`.get` 이어야 한다.** 여기 오는 행은 정의상 필수 필드가
    # 빠져 있고, `row['id']` 로 바꾸면 진단을 만들다가 `KeyError` 가 나서 `PackSchemaError`
    # 계약이 깨진다(적대 검증 실증, 2026-08-05). 표시되는 **값**(id 가 없으면 None)은 진단용
    # 이지만 **접근 방식**은 계약이다 — "메시지일 뿐"이라고 단순화하지 마라.
    for key in ("id", "label", "node_type", "space"):
        if not row.get(key):
            raise PackSchemaError.missing_required("노드", key, row.get("id"))
    if row["space"] not in ALL_SPACES:
        raise PackSchemaError(
            f"노드 {row.get('id')!r} 의 space {row.get('space')!r} 가 9-space 밖이다: {ALL_SPACES}")
    props = row.get("properties")
    if props is not None and not isinstance(props, dict):
        raise PackSchemaError(
            f"노드 {row.get('id')!r} 의 properties 가 dict 가 아니다: {type(props).__name__}")
    if not allow_legacy_top_level:
        stray = stray_top_level_keys(row)
        if stray:
            raise PackSchemaError(
                f"노드 {row.get('id')!r} 최상위에 비구조 키 {sorted(stray, key=repr)} 가 있다. "
                ' 커스텀 필드는 중첩 "properties" 에 넣어야 적재기가 읽는다.')


def validate_edge(row: dict[str, Any]) -> None:
    """edges.jsonl 한 행을 검사한다."""
    for key in ("id", "source_id", "target_id", "label"):
        if not row.get(key):
            raise PackSchemaError.missing_required("엣지", key, row.get("id"))
    props = row.get("properties")
    if props is not None and not isinstance(props, dict):
        raise PackSchemaError(
            f"엣지 {row.get('id')!r} 의 properties 가 dict 가 아니다: {type(props).__name__}")
    stray = {k for k in row if k not in EDGE_STRUCT_KEYS}
    if stray:
        raise PackSchemaError(
            f"엣지 {row.get('id')!r} 최상위에 비구조 키 {sorted(stray, key=repr)} 가 있다. "
            "엣지에는 레거시 흡수 경로가 없다 — properties 에 넣어라.")


def validate_chunk(row: dict[str, Any]) -> None:
    """chunks.jsonl 한 행을 검사한다."""
    for key in ("id", "document_id", "text"):
        if row.get(key) is None:
            raise PackSchemaError.missing_required("청크", key, row.get("id"))
    meta = row.get("metadata")
    if meta is not None and not isinstance(meta, dict):
        raise PackSchemaError(
            f"청크 {row.get('id')!r} 의 metadata 가 dict 가 아니다: {type(meta).__name__}")
    stray = {k for k in row if k not in CHUNK_STRUCT_KEYS}
    if stray:
        raise PackSchemaError(
            f"청크 {row.get('id')!r} 최상위에 비구조 키 {sorted(stray, key=repr)} 가 있다. "
            "청크에는 레거시 흡수 경로가 없다 — metadata 에 넣어라.")


# ── 표 자기정합성 ───────────────────────────────────────────────

def check_grammar_tables(
    allowed: dict[tuple[str, str], frozenset[str]],
    fix: dict[tuple[str, str], str],
) -> None:
    """FIX 가 ALLOWED 와 정합한지 검사한다. 어긋나면 ``RuntimeError``.

    어긋난 채로 두면 생산자가 grammar 위반 엣지를 만들고 적재기가 그것을 조용히
    버린다. manifest 가 바뀌었는데 FIX 를 안 고친 경우가 정확히 이 함정이다.

    모듈 import 시점에 호출한다. 함수로 분리한 이유는 **가드 자체가 사문이 돼도
    아무도 모르는 것**을 막기 위해서다 — inline 이면 무력화해도 테스트가 통과했다
    (2026-08-04 검증에서 실측).
    """
    fix_orphans = sorted(set(fix) - set(allowed))
    if fix_orphans:
        raise RuntimeError(
            f"FIX 에 grammar manifest 에 없는 공간쌍이 있다: {fix_orphans}")
    fix_bad = sorted(k for k, v in fix.items() if v not in allowed[k])
    if fix_bad:
        raise RuntimeError(
            f"FIX 의 대표 relation 이 해당 공간쌍의 허용 집합 밖이다: {fix_bad}")
    allowed_orphans = sorted(set(allowed) - set(fix))
    if allowed_orphans:
        raise RuntimeError(
            f"grammar manifest 공간쌍에 FIX 대표값이 없다: {allowed_orphans}")


check_grammar_tables(ALLOWED, FIX)
