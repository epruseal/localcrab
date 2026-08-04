"""by-pack 행 → 라이브 스토어 표현의 **순수** 정규화 계층.

여기 있는 것은 전부 부작용 없는 결정적 변환이다 — 스토어 쓰기·env·파일 I/O 없음.
적재기(load), 증분 대조, 게이트(check_grammar_fit)가 **같은 함수**를 봐야 하므로 이 모듈이 정본이다.

**왜 분리했는가.** 2026-08-04 이관 전에는 같은 판정이 세 곳에 있었다:
  1. `load_local_packs.load_edges()` 인라인 (실제 적재 경로, 정본)
  2. `check_grammar_fit.fits()` / `resolve_effective_space()` 재구현 (게이트)
  3. `edge_skip_report` 가 표만 import 해 자체 해석
게이트가 정본을 재구현하면 게이트 통과가 적재 성공을 뜻하지 않게 된다. 실제로
`transform_node` 가 커스텀 필드를 못 읽어 91만 필드가 라이브에 도달하지 못한 사고와
같은 계층 문제다. 해석은 `resolve_node_space_type()` / `resolve_edge()` 하나뿐이다.
"""

from __future__ import annotations

from opencrab.grammar.manifest import SPACES as _GRAMMAR_SPACES
from opencrab.pack.schema import NODE_STRUCT_KEYS, NODE_TYPE_OVERRIDE, SPACE_DEFAULT_TYPE

# grammar에서 허용 node_type 집합 동적 생성 (하드코딩 없이 manifest 기준)
_GRAMMAR_NODE_TYPES: frozenset[str] = frozenset(
    nt for s in _GRAMMAR_SPACES.values() for nt in s["node_types"]
)

# 원본 graphrag/dump 관계 → 9-space 정규화 관계
REL_MAP: dict[str, str] = {
    # Honda GraphRAG
    "HAS_ASSEMBLY":        "part_of",       # 반전: Vehicle→Assembly → Assembly part_of Vehicle
    "HAS_PART":            "part_of",       # 반전: Assembly→Part → Part part_of Assembly
    "HAS_PART_NUMBER":     "related_to",    # concept→concept
    "SHOWN_IN":            "mentions",      # 반전: Part→Diagram → Diagram mentions Part (resource→concept)
    "USED_IN_MODEL":       "part_of",
    "INSTANCE_OF":         "subclass_of",
    "USES_CANONICAL_PART": "related_to",
    # claude/codex 대화팩
    "HAS_SESSION":         "contains",      # resource/Project → evidence/LogEntry
    "CONTAINS_TURN":       "describes",     # evidence/LogEntry → concept/Topic
    # krds / krds-enhanced
    "ACHIEVES":            "contributes_to",  # concept→outcome
    "ENABLES":             "contributes_to",  # concept→outcome
    "AFFECTS":             "optimizes",       # lever→outcome
    "HAS_CHUNK":           "contains",        # resource→evidence
    "HAS_RESOURCE":        "owns",            # subject→resource
    "HAS_TOKEN_CATEGORY":  "mentions",        # resource→concept
    "SERVES":              "serves",          # concept→community
    "HAS_MARKUP":          "has_markup",      # concept→resource
    "HAS_STYLE":           "has_style",       # concept→resource
    "HAS_COMPONENT":       "has_component",   # subject→concept
    "ENSURES":             "ensures",         # policy→outcome
    "COMPLIES_WITH":       "complies_with",   # claim→policy
    "HAS_VARIANT":         "has_variant",     # concept→lever
    "GUIDED_BY":           "guided_by",       # concept→claim
    "DEFINES":             "defines",         # resource→policy, subject→concept
    "HAS_CATEGORY":        "has_category",    # subject→subject
    "HAS_MODE":            "has_mode",        # resource→lever
    "STATES":              "states",          # resource→claim
    "PROTECTS":            "protects",        # policy→community
    # SUPPORTS: 공간쌍별로 다르게 처리 → LABEL_SPACE_OVERRIDE 참조
    # claim→evidence 는 반전해서 evidence→claim:supports
    # 나머지(claim→outcome 등)는 아래 기본값 사용
    "SUPPORTS":            "supports",
    # 공공데이터품질관리
    "EVIDENCES":           "contains",        # resource→evidence
    "CITES":               "cites",           # policy→evidence
    "GOVERNS":             "governs",         # subject→claim, subject→policy
    "MEASURED_BY":         "measured_by",     # subject→lever
    "TARGETS":             "targets",         # subject→outcome
    "HAS_DOMAIN":          "has_domain",      # subject→subject
    # 공통 canonical (대문자 → lowercase 정규화)
    "DESCRIBES":           "describes",
    "CONTAINS":            "contains",
    "MENTIONS":            "mentions",
    "OWNS":                "owns",
    "DERIVED_FROM":        "derived_from",
    "EVIDENCED_BY":        "evidenced_by",
    "CLUSTERS":            "clusters",
    "PART_OF":             "part_of",
    "DEPENDS_ON":          "related_to",
    "SCOPES":              "scopes",
    "REQUIRES_APPROVAL":   "governs",
    "CONTRIBUTES_TO":      "contributes_to",
    "PREDICTS":            "related_to",
    "STABILIZES":          "optimizes",
    "CONSTRAINS":          "governs",
    "OPTIMIZES":           "optimizes",
    "RAISES":              "raises",
    "LOWERS":              "lowers",
    "RELATED_TO":          "related_to",
    # 명문장1007
    "EXEMPLIFIES":         "exemplifies",     # evidence→concept: exemplifies (supports 아님)
}

# 원본 방향을 반전시켜야 하는 관계
REVERSE_RELATIONS: set[str] = {
    "HAS_ASSEMBLY",
    "HAS_PART",
    "SHOWN_IN",
}

# (label, src_space, tgt_space) → (relation, reverse)
# REL_MAP이 공간쌍을 구분 못 하는 경우 명시 매핑. 3-tuple로 정확히 분기.
LABEL_SPACE_OVERRIDE: dict[tuple[str, str, str], tuple[str, bool]] = {
    # ── SUPPORTS ─────────────────────────────────────────────────────────────
    ("SUPPORTS", "claim",     "evidence"): ("supports",      True),   # 반전 → evidence→claim:supports
    ("SUPPORTS", "claim",     "outcome"):  ("supports",      False),  # claim→outcome:supports (krds 18건)
    ("SUPPORTS", "lever",     "evidence"): ("evidenced_by",  False),
    ("SUPPORTS", "policy",    "evidence"): ("cites",         False),
    ("SUPPORTS", "outcome",   "evidence"): ("evidenced_by",  False),
    ("SUPPORTS", "concept",   "evidence"): ("evidenced_by",  False),
    ("SUPPORTS", "community", "evidence"): ("evidenced_by",  False),
    ("SUPPORTS", "subject",   "evidence"): ("evidenced_by",  False),  # 공공 18건
    ("SUPPORTS", "resource",  "evidence"): ("derived_from",  False),  # 공공 6건
    # ── CITES ─────────────────────────────────────────────────────────────────
    ("CITES",    "resource",  "evidence"): ("derived_from",  False),  # 공공 33건 (article→evidence)
    # ── DEFINES ───────────────────────────────────────────────────────────────
    ("DEFINES",  "concept",   "concept"):  ("related_to",    False),  # 공공 25건 흡수
    # ── MEASURED_BY ───────────────────────────────────────────────────────────
    ("MEASURED_BY", "concept", "concept"):  ("related_to",    False),  # 공공 17건 흡수
    ("MEASURED_BY", "concept", "claim"):    ("measured_by",   False),  # 공공 12건 (grammar 보강)
    ("MEASURED_BY", "concept", "lever"):    ("measured_by",   False),  # 공공 3건 (grammar 보강)
    ("MEASURED_BY", "concept", "resource"): ("measured_by",   False),  # 공공 3건 (grammar 보강)
    # ── TARGETS ───────────────────────────────────────────────────────────────
    ("TARGETS",  "concept",   "outcome"):  ("contributes_to", False),  # 공공 6건 흡수
    # ── GOVERNS ───────────────────────────────────────────────────────────────
    ("GOVERNS",  "concept",   "policy"):   ("governs",       False),  # 공공 33건 (grammar 신규)
    ("GOVERNS",  "concept",   "resource"): ("governs",       False),  # 공공 8건 (grammar 보강)
    # ── HAS_DOMAIN ────────────────────────────────────────────────────────────
    ("HAS_DOMAIN", "policy",  "concept"):  ("scopes",        False),  # 공공 4건 (grammar 신규)
    # ── CITES ─────────────────────────────────────────────────────────────────
    ("CITES",      "resource", "resource"): ("cites",        False),  # 공공 5건 Law→LawText
    # ── EVIDENCED_BY ──────────────────────────────────────────────────────────
    ("EVIDENCED_BY", "resource", "evidence"): ("contains",   False),  # resource→evidence: contains
    ("EVIDENCED_BY", "claim",    "evidence"): ("supports",   True),   # 반전 → evidence→claim:supports (24건)
    # ── SCOPES ────────────────────────────────────────────────────────────────
    ("SCOPES",       "policy",   "resource"): ("classifies", False),  # policy→resource: classifies (scopes 없음)
    # ── openclaw 대화 아카이브 팩 (2026-07-05, 54k 전체팩 적재용) ────────────────
    # supported_by(claim→evidence)는 evidence_for의 역방향 중복이라 의도적 미매핑(skip) — 53,834건 중복 방지
    ("ORGANIZES",    "concept",  "evidence"): ("evidenced_by", False),  # 세션조직 concept→turn evidence (53,834건)
    ("ORGANIZES",    "resource", "resource"): ("cites",        False),  # 세션→세션 조직
    ("PRODUCED",     "subject",  "evidence"): ("evidenced_by", False),  # 발화 주체→evidence (53,834건)
    ("HAS_EVIDENCE", "resource", "evidence"): ("contains",     False),  # 세션문서→turn (53,834건)
    ("EVIDENCE_FOR", "evidence", "claim"):    ("supports",     False),  # turn→claim (53,834건)
    ("HAS_CLAIM",    "resource", "claim"):    ("states",       False),  # 세션문서→claim
    ("CONSTRAINS",   "policy",   "resource"): ("restricts",    False),  # 정책→세션 제약 (366건)
    ("ORGANIZES",    "concept",  "resource"): ("governs",      False),  # 세션조직 concept→세션문서 (275건)
    ("ABOUT",        "resource", "concept"):  ("mentions",     False),  # 세션문서→주제 concept (275건)
    ("SUPPORTS_OUTCOME", "claim", "outcome"): ("supports",     False),  # claim→성과 (275건, 07-06 회수)
    ("MENTIONS_MODEL", "resource", "concept"): ("mentions",    False),  # 세션→모델 concept (236건, 07-06 회수)
    # (미매핑 의도적 skip: claim→evidence supported_by 53,834=evidence_for 중복,
    #  concept→claim organizes 275·community→resource has_session 275=grammar 쌍 부재)
    # ── 전자정부사업관리 ────────────────────────────────────────────────────────
    ("CONTAINS",     "policy",   "evidence"): ("cites",      False),  # policy→evidence: cites (379건)
    ("MENTIONS",     "resource", "resource"): ("cites",      False),  # resource→resource: cites (75건)
    ("CONTAINS",     "resource", "resource"): ("cites",      False),  # resource→resource: cites (15건)
    ("DESCRIBES",    "resource", "concept"):  ("mentions",   False),  # resource→concept: mentions (15건)
    ("EVIDENCED_BY", "concept",  "resource"): ("measured_by",False),  # concept→resource: measured_by (6건)
    ("CITES",        "policy",   "resource"): ("classifies", False),  # policy→resource: classifies (5건)
    ("REQUIRES_APPROVAL","policy","subject"): ("requires_approval", False),  # policy→subject: requires_approval (6건)
    ("PREDICTS",     "concept",  "outcome"):  ("predicts",   False),  # concept→outcome: predicts (3건)
    ("AFFECTS",      "lever",    "concept"):  ("affects",    False),  # lever→concept: affects (3건)
    ("CONSTRAINS",   "concept",  "outcome"):  ("constrains", False),  # concept→outcome: constrains (1건)
    ("PART_OF",      "resource", "concept"):  ("mentions",   False),  # resource→concept: mentions (1건)
    # AdminRule/Guideline → resource/Document 전환 후 처리
    ("DEFINES",      "resource", "resource"): ("cites",      False),  # resource→resource: cites (AdminRule→Guideline 5건)
    ("COMPLIES_WITH","claim",    "resource"): ("states",     True),   # 반전 → resource→claim:states (8건)
    ("SCOPES",       "resource", "concept"):  ("mentions",   False),  # resource→concept: mentions (6건)
}

def flatten_props(d: dict) -> dict:
    """Node props: primitives 유지, nested dict → str."""
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif v is None:
            out[k] = ""
        elif isinstance(v, list):
            if all(isinstance(i, (str, int, float, bool)) for i in v):
                out[k] = v
            else:
                out[k] = str(v)
        else:
            out[k] = str(v)
    return out


def chroma_safe_meta(d: dict) -> dict:
    """Chroma 메타데이터 정제: 스칼라(str/int/float/bool)만 허용.

    flatten_props와 달리 list 도 str() 직렬화한다(Chroma는 list 메타를 거부).
    None 은 키 제거(Chroma는 None 값도 거부). doc_sources(JSON TEXT)에도 그대로 사용.
    """
    out: dict = {}
    for k, v in (d or {}).items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def resolve_node_space_type(space: str, node_type: str) -> tuple[str, str]:
    """원본 (space, node_type) → **적재기가 실제로 쓰는** (space, node_type).

    transform_node 의 앞부분을 그대로 뽑은 것이다. 게이트(check_grammar_fit)가 이 판정을
    재구현하면 게이트 통과가 적재 성공을 뜻하지 않게 되므로 양쪽이 이 함수를 공용한다.

    순서가 계약이다:
      1. NODE_TYPE_OVERRIDE 명시 매핑이 최우선
      2. grammar 미등록 타입은 space 기반 fallback
      3. grammar 등록 타입이어도 그 space 에 무효하면 다시 fallback
         (예: subject/Document — Document 는 resource 전용) → skip 방지
    """
    if node_type in NODE_TYPE_OVERRIDE:
        space, node_type = NODE_TYPE_OVERRIDE[node_type]
    elif node_type not in _GRAMMAR_NODE_TYPES:
        fallback = SPACE_DEFAULT_TYPE.get(space)
        if fallback:
            space, node_type = fallback
    _allowed = _GRAMMAR_SPACES.get(space, {}).get("node_types", ())
    if _allowed and node_type not in _allowed:
        fallback = SPACE_DEFAULT_TYPE.get(space)
        if fallback:
            space, node_type = fallback
    return space, node_type


def resolve_edge(
    label: str, from_space: str, to_space: str
) -> tuple[str, str, str, bool]:
    """원본 (라벨, 양끝 space) → (from_space, relation, to_space, 반전여부).

    적재기 load_edges() 의 라벨 해석부를 그대로 뽑은 것이다. 반전이 일어나면 반환되는
    from_space/to_space 는 **이미 뒤바뀐** 값이며, 호출부는 src/tgt 도 함께 바꿔야 한다.

    lookup 은 대소문자를 무시한다(원본 라벨은 호출부가 properties.source_label 로 보존).
    어느 표에도 없으면 lowercase 로 귀착시켜 grammar 에 직접 맡긴다 — 정보 보존 우선.
    """
    lookup = (label or "").upper()
    override = LABEL_SPACE_OVERRIDE.get((lookup, from_space, to_space))
    if override:
        relation, do_reverse = override
        if do_reverse:
            from_space, to_space = to_space, from_space
            return from_space, relation, to_space, True
        return from_space, relation, to_space, False
    relation = REL_MAP.get(lookup)
    if relation is None:
        # lowercase fallback: grammar에 직접 있는 relation이면 통과
        relation = (label or "").lower()
    if lookup in REVERSE_RELATIONS:
        from_space, to_space = to_space, from_space
        return from_space, relation, to_space, True
    return from_space, relation, to_space, False


def transform_node(pack_name: str, row: dict) -> tuple[str, str, str, dict]:
    """by-pack 노드 행 → (space, node_type, node_id, props) 결정적 변환.

    적재(load_nodes)와 증분 대조(load_nodes_incremental)가 공용한다 —
    라이브 properties와의 동일성 비교가 이 변환의 결정성에 의존하므로
    비결정 요소(시각·난수) 삽입 금지.
    """
    node_id   = row["id"]
    space     = row.get("space", "concept")
    node_type = row.get("node_type", "Concept")

    # node_type 오버라이드 (구형/커스텀 타입 → grammar 기본 타입).
    # 판정 자체는 resolve_node_space_type 가 정본 — 게이트도 같은 함수를 본다.
    original_type = node_type
    space, node_type = resolve_node_space_type(space, node_type)

    props     = flatten_props(row.get("properties") or {})
    # 레거시 호환: 2026-08-03 이전 pack_lib은 커스텀 필드를 노드 최상위에 펼쳤고
    # 이 함수가 중첩 "properties"만 읽어 그 필드들이 라이브에 하나도 실리지 않았다
    # (약 120개 팩, 실측 확인). 재빌드 없이 회수하려면 최상위도 흡수해야 한다.
    # 중첩 값이 우선한다 — 정본 위치가 중첩이기 때문이다.
    # statement 폴백은 흡수 전 값으로만 판단한다(아래 사유). **사본**인 것이 중요하다.
    #
    # 지금 이 dict() 를 지우고 별칭으로 둬도 동작은 같다 — 적대 검증이 128팩 238,987 노드로
    # 실측했다(characterization sha256 불변, 2026-08-04). 흡수가 props 를 in-place 로 바꾸지
    # 않고 새 dict 로 **재바인딩**하고, 사본 시점과 아래 폴백 사이에 statement/text/description
    # 을 쓰는 코드가 없기 때문이다. 즉 등가성은 코드 순서라는 깨지기 쉬운 전제에 얹혀 있다.
    # 그 전제가 깨지는 순간 이미 적재된 Claim 2,039건(59팩)의 statement 가 조용히 바뀌므로
    # 사본을 유지한다. 테스트로는 못 잡는 종류라 여기에 남긴다.
    _nested_only = dict(props)
    _stray = {k: v for k, v in row.items() if k not in NODE_STRUCT_KEYS}
    if _stray:
        _merged = flatten_props(_stray)
        _merged.update(props)
        props = _merged
    # 원본 타입 보존 (오버라이드된 경우)
    # 흡수분이 로더가 계산한 original_type을 덮지 않도록, 레거시 값이 있어도 실제 원본 타입을 쓴다.
    if original_type != node_type:
        props["original_type"] = original_type

    # 일관 태깅
    props["pack_id"] = pack_name
    props["pack"]    = pack_name
    props.setdefault("degree", row.get("degree", 0))
    label = row.get("label") or node_id
    # schema 필수 필드 자동 채움
    if node_type == "Document" and not props.get("title"):
        props["title"] = label
    if node_type in ("Team", "Org", "Outcome", "Lever") and not props.get("name"):
        props["name"] = label
    # 발동 조건도 _nested_only로 본다. 흡수 후 props를 보면, 최상위에 statement 키를 가진
    # 팩이 생기는 순간 흡수분이 그대로 statement가 되어 같은 무음 변경이 재발한다
    # (현재 데이터에는 최상위 statement 0건이라 미발현, 2026-08-04 검증 지적).
    if node_type in ("Claim", "CollectionCompleteness", "Covariate") and not _nested_only.get("statement"):
        # 폴백 소스는 _nested_only(정본 위치)만 본다. 레거시 최상위 흡수분을 여기 끌어들이면
        # 이미 적재된 Claim 2,039건(59팩)의 statement가 조용히 바뀐다. 실측 확인 결과 그 변화가
        # 개선이 아니었다: label "명확하고 구체적인 지시가 출력 품질을 높인다"가
        # description "모호성 제거."로 대체되는 식으로 완결 문장이 단편으로 퇴화한다.
        # 흡수는 없던 필드를 회수하는 것이지 기존 라이브 값을 바꾸는 것이 아니다.
        props["statement"] = (_nested_only.get("text") or _nested_only.get("description")
                              or label)
    if node_type == "CollectionCompleteness":
        if props.get("status") not in ("pass", "retry", "fail"):
            props["status"] = "pass"
        props.setdefault("score", 1.0)
    if node_type == "Policy":
        if not props.get("name"):
            props["name"] = label
        if not props.get("rule_type"):
            props["rule_type"] = "classification"
    if node_type == "User":
        if not props.get("name"):
            props["name"] = label
        # User 스키마는 name/email/role 을 모두 required 로 잡는데 by-pack 의
        # 저자 노드에는 이메일이 없다. 채우지 않으면 add_node 가 ValueError 를
        # 던지고 load_nodes 가 skip 으로 삼켜, 그 저자에게 달린 엣지만 남아
        # 영구 dangling 이 된다(2026-07-29 실측: 저자 4명 누락 -> 엣지 11,833건).
        # .invalid 는 RFC 2606 이 "절대 resolve 되지 않는다"고 예약한 TLD 라
        # 합성값임이 자명하고 실제 주소와 충돌하지 않는다.
        if not props.get("email"):
            props["email"] = f"{node_id}@local.invalid"
            props["email_synthesized"] = True
    # enum 위반 교정(2026-07-29): validate_node_properties 는 required 뿐 아니라
    # enum 도 검사한다. 위반값이 들어가면 add_node 가 ValueError → load_nodes 가
    # skip 으로 삼켜버린다(log.debug 라 사유가 보이지 않음).
    #
    # 실측 범위 주의 — 이 함수는 550행에서 row["properties"] 하위만 읽는다:
    #   · User.role   4건: naeil-blog 등 properties 에 실재 → **이 경로에서 교정됨**
    #   · Document.format 19건(n2sf 15 · krmf 3 · aiready 1, 예 'pdf(pdftotext)'):
    #     해당 팩들은 properties 키 자체가 없고 format 이 top-level 이라
    #     여기까지 도달하지 않는다. 즉 아래 Document 분기는 이 파일에서는
    #     **현재 사문(방어용)** 이며, top-level 을 props 로 승격하는 로더
    #     (VM106 cnontograph 의 import_pack_zip.py)에서 실제로 발동한다.
    #     2026-07-29 VM 적재 로그 실측: krmf 3 · n2sf 15 · aiready 1.
    #
    # 원값은 <field>_original 로 보존한다. 대체값은 스키마 default(plain) 및
    # 최소권한 원칙(viewer)을 따른다. CollectionCompleteness.status 는 위에서 처리.
    # 미커버 enum(enum 보유 7타입 11필드 중 나머지)은 현재 위반 0건이라 비차단.
    if node_type == "Document" and props.get("format") not in (
        None, "markdown", "pdf", "html", "plain",
    ):
        props["format_original"] = props["format"]
        props["format"] = "plain"
    if node_type == "User" and props.get("role") not in (
        None, "admin", "editor", "viewer", "agent", "analyst", "engineer",
    ):
        props["role_original"] = props["role"]
        props["role"] = "viewer"
    # label→name 일반화(2026-07-22): by-pack label은 top-level 필드라 여기서
    # props에 보존하지 않으면 라이브에서 표시이름이 완전 유실된다
    # (실측: Concept 6,142/6,196·Topic 545 전건 name 부재). UUID 오염 방지를
    # 위해 실제 label 보유 행에만 주입. Concept 등은 타입 스키마가 없어
    # unknown 필드 제약도 없음(validate_node_properties는 required·enum만 검사).
    if row.get("label"):
        props.setdefault("label", row["label"])
        props.setdefault("name", row["label"])

    return space, node_type, node_id, props


def transform_chunk_meta(pack_name: str, row: dict) -> dict:
    """by-pack 청크 행 → Chroma/doc_sources 저장용 meta dict 결정적 변환.

    load_chunks(전량)와 load_chunks_incremental(증분 대조)이 공용한다 —
    라이브 metadata와의 동일성 비교가 이 변환의 결정성에 의존한다.
    """
    # 원본 청크 메타(char_start/char_end, evidence_index, 지리·매출 코드 등)
    # 전체 보존 — Chroma 스칼라 제약에 맞춰 정제. doc_sources(JSON)에도 동일 사용.
    meta: dict = chroma_safe_meta(row.get("metadata"))
    # 원본 문서 경로는 source_doc 으로 보존(아래 source=pack_name 과 충돌 방지)
    if "source" in meta:
        meta["source_doc"] = meta.pop("source")
    # 권위 필드는 항상 pack_name 으로 고정(기존 적재분과 의미 일치 — pack 필터/삭제 호환)
    meta["pack_id"] = pack_name
    meta["source"]  = pack_name
    if row.get("document_id"):
        meta["document_id"] = row["document_id"]
    return meta
