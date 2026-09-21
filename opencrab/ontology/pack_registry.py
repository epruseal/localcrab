"""
Pack registry — scans ``<local_data_dir>/packs/*/stage/manifest.json`` and
exposes deterministic auto-pack selection.

Public API:
    load_pack_registry(local_data_dir)
    get_pack(local_data_dir, pack_id)
    choose_packs(question, packs, min_score=...)
    score_pack(question, pack)
    build_candidate_registry(sql_rows, fs_packs)

Auto-pack scoring is deterministic and keyword-based (no LLM). The first
implementation returns only the top-1 candidate above ``min_score``; multi-
candidate / margin selection is left to a follow-up.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_AUTO_PACK_MIN_SCORE = 10.0


def _env_min_score(default: float = DEFAULT_AUTO_PACK_MIN_SCORE) -> float:
    raw = os.environ.get("OPENCRAB_AUTO_PACK_MIN_SCORE")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid OPENCRAB_AUTO_PACK_MIN_SCORE=%r; using %.1f", raw, default)
        return default


# Korean ↔ English aliases for common pack themes. Hardcoded for the first
# pass; pack maintainers can add manifest hints in a future iteration.
_ALIASES: dict[str, tuple[str, ...]] = {
    "nemotron": ("nemotron", "네모트론", "nvidia", "엔비디아"),
    "persona": ("persona", "personas", "페르소나", "인물", "프로필"),
    "korea": ("korea", "korean", "한국", "한국어"),
}


@dataclass
class PackInfo:
    pack_id: str
    title: str = ""
    description: str = ""
    version: str = ""
    source_label: str | None = None
    source_url: str | None = None
    path: Path = field(default_factory=Path)
    manifest_path: Path = field(default_factory=Path)
    counts: dict[str, Any] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "title": self.title,
            "version": self.version,
            "counts": self.counts,
            "path": str(self.path),
            "source": {
                "label": self.source_label,
                "url": self.source_url,
            },
        }


def _read_manifest(manifest_path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read manifest %s: %s", manifest_path, exc)
        return None


def _pack_info_from_manifest(
    manifest_path: Path,
    stage_dir: Path,
    manifest: dict[str, Any],
) -> PackInfo | None:
    pack_id = manifest.get("pack_id") or manifest_path.parent.parent.name
    if not pack_id:
        return None
    source = manifest.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    counts = manifest.get("counts") or {}
    if not isinstance(counts, dict):
        counts = {}
    keywords = manifest.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    tags = manifest.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return PackInfo(
        pack_id=str(pack_id),
        title=str(manifest.get("title") or ""),
        description=str(manifest.get("description") or ""),
        version=str(manifest.get("version") or ""),
        source_label=str(source["label"]) if source.get("label") else None,
        source_url=str(source["url"]) if source.get("url") else None,
        path=stage_dir.parent,
        manifest_path=manifest_path,
        counts=counts,
        keywords=[str(k) for k in keywords if isinstance(k, (str, int, float))],
        tags=[str(t) for t in tags if isinstance(t, (str, int, float))],
        raw=manifest,
    )


def load_pack_registry(local_data_dir: str | Path) -> list[PackInfo]:
    """Scan ``<local_data_dir>/packs/*/stage/manifest.json`` and return PackInfos.

    Missing/invalid manifests are skipped with a warning. Returns an empty
    list when no ``packs/`` directory exists.
    """
    root = Path(local_data_dir) / "packs"
    if not root.is_dir():
        return []

    packs: list[PackInfo] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        stage_dir = entry / "stage"
        manifest_path = stage_dir / "manifest.json"
        if not manifest_path.is_file():
            # Fall back to entry/manifest.json for non-staged layouts.
            alt = entry / "manifest.json"
            if alt.is_file():
                manifest_path = alt
                stage_dir = entry
            else:
                continue
        manifest = _read_manifest(manifest_path)
        if manifest is None:
            continue
        info = _pack_info_from_manifest(manifest_path, stage_dir, manifest)
        if info is not None:
            packs.append(info)
    return packs


def get_pack(local_data_dir: str | Path, pack_id: str) -> PackInfo | None:
    for pack in load_pack_registry(local_data_dir):
        if pack.pack_id == pack_id:
            return pack
    return None


# ---------------------------------------------------------------------------
# Deterministic keyword scoring
# ---------------------------------------------------------------------------

_HANGUL_RE = re.compile(r"[가-힣]+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = {
    "and", "or", "the", "a", "an", "to", "of", "in", "for", "with",
    "from", "by", "on", "at", "is", "are", "was", "were", "be", "as",
    "이", "그", "저", "것", "수", "을", "를", "에", "의", "도",
    "는", "은", "가", "과", "와", "로", "만",
}


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    for match in _WORD_RE.findall(text):
        if len(match) >= 2 and match not in _STOPWORDS:
            tokens.add(match)
    for match in _HANGUL_RE.findall(text):
        if len(match) >= 2 and match not in _STOPWORDS:
            tokens.add(match)
            # add 2- and 3-gram fragments to broaden Korean recall
            for n in (2, 3):
                for i in range(len(match) - n + 1):
                    fragment = match[i : i + n]
                    if fragment not in _STOPWORDS:
                        tokens.add(fragment)
    return tokens


# #400: 게이트 전용 whole-token 토크나이저. 공백/구두점 경계로만 나뉜 낱말이며
# _tokens() 와 달리 n-gram fragment 를 만들지 않고 길이 하한도 두지 않는다.
# 스크립트 종류(ASCII/한글) 전환은 경계로 보지 않는다 — "k2관세" 같은 혼용
# 토큰을 하나로 유지해 fragment 매치를 게이트에서 배제한다.
_WHOLE_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣]+")


def _whole_tokens(text: str) -> set[str]:
    text = (text or "").lower()
    return {m for m in _WHOLE_TOKEN_RE.findall(text) if m not in _STOPWORDS}


def _ordered_whole_tokens(text: str) -> list[str]:
    """`_whole_tokens()`의 순서 보존 버전. `_phrase_tokens_in_order()`의
    연속 부분열 비교에 쓴다 -- 집합은 순서를 버리므로 이 비교에는 못 쓴다."""
    text = (text or "").lower()
    return [m for m in _WHOLE_TOKEN_RE.findall(text) if m not in _STOPWORDS]


def _phrase_at_boundary(phrase: str, text: str) -> bool:
    """#400 제목 보너스 전용: 경계 조건을 더한 원문 substring 판정.

    #400 이전 코드는 ``title in question`` 그대로 substring 으로 판정했다.
    순서에는 민감하지만, 짧은 title 이 더 긴 낱말 안에 경계 없이 박혀 있어도
    (질의 "혈자리" 안의 title "자리") 보너스가 열리는 결함이 있었다. #400은
    이를 막으려 whole-token *부분집합* 비교로 바꿨는데, 부분집합 비교는
    순서를 보지 않는다 -- 같은 낱말을 다른 순서로 쓴 두 title
    ("man bites dog"와 "dog bites man")이 같은 토큰 집합이 되어 같은
    보너스로 동점 처리되고, 안정 정렬 + 상위 1건 반환 조합에서 정확한
    title 이 순서만 다른 title 에 밀려 탈락한다(회귀).

    substring 판정으로 되돌려 순서 민감성을 복원하되, 매치 앞뒤 문자(있는
    경우)가 whole-token 문자 클래스(``_WHOLE_TOKEN_RE``, #400 게이트가 이미
    쓰는 것과 동일)를 벗어나야 한다는 경계 조건을 더해 기존 fragment 차단을
    유지한다.
    """
    if not phrase:
        return False
    start = 0
    while True:
        idx = text.find(phrase, start)
        if idx == -1:
            return False
        before_ok = idx == 0 or not _WHOLE_TOKEN_RE.match(text[idx - 1])
        end = idx + len(phrase)
        after_ok = end == len(text) or not _WHOLE_TOKEN_RE.match(text[end])
        if before_ok and after_ok:
            return True
        start = idx + 1


def _alias_equivalent(a: str, b: str) -> bool:
    """`_phrase_tokens_in_order()` 전용: 두 whole-token이 같거나 같은
    `_ALIASES` 그룹의 변형이면 동치로 본다. 별도 별칭 표를 새로 두지 않고
    기존 `_ALIASES`를 그대로 재사용해 "같은 개념, 두 정의" 드리프트를
    피한다."""
    if a == b:
        return True
    for variants in _ALIASES.values():
        lowered = {v.lower() for v in variants}
        if a in lowered and b in lowered:
            return True
    return False


def _phrase_tokens_in_order(phrase_tokens: list[str], question_tokens: list[str]) -> bool:
    """pack_id/source_label 보너스 전용: 순서를 지키는 연속 부분열 비교.

    title 보너스는 `_phrase_at_boundary()`로 원문 substring을 그대로 쓴다 --
    title은 질의와 같은 자연어 문자열이라 구분자가 같다는 전제가 성립한다.
    pack_id는 다르다. #400은 pack_id 보너스를 whole-token *부분집합* 비교로
    바꿔 "acupoint-medical" 같은 하이픈 식별자가 "acupoint medical" 같은
    공백 질의와도 만나게 했다(의도적 개선, `test_t400_hyphenated_pack_id
    _bonus_via_whole_token_subset`가 고정). 그런데 부분집합 비교는 순서를
    보지 않아 "dog-bites-man"과 "man-bites-dog"가 같은 토큰 집합이 되고,
    안정 정렬 + 기본 limit=1 때문에 순서만 다른 pack_id가 정확한 pack_id를
    밀어낸다(대체 리뷰 PR #405 반례).

    구분자 관용은 그대로 지키면서 순서만 구분하려면 원문 substring이 아니라
    whole-token *순서열*을 비교해야 한다 -- `_WHOLE_TOKEN_RE`가 토큰 경계를
    이미 정하므로 하이픈이든 공백이든 같은 토큰열이 나온다. 부분집합이 아니라
    질의 토큰열 안에 pack 토큰열이 "연속"으로 나타나는지를 본다. 위치별
    비교에는 `_alias_equivalent()`를 써서 기존 별칭 매치 능력도 유지한다.
    """
    n = len(phrase_tokens)
    m = len(question_tokens)
    if n == 0 or n > m:
        return False
    for start in range(m - n + 1):
        if all(
            _alias_equivalent(question_tokens[start + i], phrase_tokens[i])
            for i in range(n)
        ):
            return True
    return False


def _resolve_aliases(question_tokens: set[str]) -> set[str]:
    expanded = set(question_tokens)
    for canonical, variants in _ALIASES.items():
        for variant in variants:
            if variant.lower() in question_tokens:
                expanded.add(canonical)
                expanded.update(v.lower() for v in variants)
                break
    return expanded


def _resolve_aliases_whole(whole_tokens: set[str]) -> set[str]:
    """게이트 전용 별칭 확장. _resolve_aliases() 와 동일한 로직을 whole-token
    집합에 적용한다 (fragment 오염 없는 별칭 매치)."""
    expanded = set(whole_tokens)
    for canonical, variants in _ALIASES.items():
        for variant in variants:
            if variant.lower() in whole_tokens:
                expanded.add(canonical)
                expanded.update(v.lower() for v in variants)
                break
    return expanded


def score_pack(question: str, pack: PackInfo) -> tuple[float, list[str]]:
    """Deterministic keyword score of one pack against ``question``.

    Public because ``content_pack_list(query=...)`` ranks graph-loaded packs
    with the *same* function auto-pack uses — two scorers would let the browse
    ranking and the auto-selection disagree about which pack a question means.
    Callers that only want the ranking (not auto_pack's 10.0 threshold) apply
    their own cutoff to the returned score.
    """
    q_lower = (question or "").lower()
    if not q_lower:
        return 0.0, []

    # #400: 게이트 — 질의의 whole 토큰이 팩 어딘가에 리터럴로 존재하는가.
    # pack_id/title/description/source_label 네 필드는 기존 _tokens()
    # (fragment 포함)와 신규 _whole_tokens()(길이 무관)의 합집합을 쓴다 —
    # _tokens()만으로는 1글자 리터럴 매치가 막히고, _whole_tokens()만으로는
    # "직업분포" 같은 붙여쓴 복합어가 "직업 분포" 공백 질의와 게이트에서 못
    # 만난다. keywords/tags 는 원래부터 자유 텍스트가 아니라 태그이므로
    # 토큰화하지 않고 원문을 소문자로만 바꿔 그대로 쓴다(변경 없음).
    q_whole = _resolve_aliases_whole(_whole_tokens(question))

    pack_tokens_all = (
        _tokens(pack.pack_id) | _whole_tokens(pack.pack_id)
        | _tokens(pack.title) | _whole_tokens(pack.title)
        | _tokens(pack.description) | _whole_tokens(pack.description)
        | {k.lower() for k in pack.keywords}
        | {t.lower() for t in pack.tags}
        | (
            (_tokens(pack.source_label) | _whole_tokens(pack.source_label))
            if pack.source_label
            else set()
        )
    )
    gate_open = bool(q_whole & pack_tokens_all)
    if not gate_open:
        return 0.0, []

    q_tokens = _tokens(question)
    q_aliases = _resolve_aliases(q_tokens)

    matched: list[str] = []
    score = 0.0

    # pack_id/source_label 보너스는 순서 보존 연속 부분열 비교를 쓴다(대체
    # 리뷰 PR #405 반례: whole-token 부분집합 비교는 순서를 무시해 순서만
    # 다른 pack_id/source_label 이 정답과 동점이 된다). 질의 쪽 순서열은
    # 한 번만 뽑아 두 보너스에서 재사용한다.
    #
    # title은 이 방식을 쓰지 않고 아래처럼 _phrase_at_boundary(원문
    # substring + 경계 조건)를 그대로 쓴다. pack_id/source_label은 식별자
    # 성격의 문자열이라 하이픈/공백/밑줄 같은 구분자가 값 자체의 의미를
    # 바꾸지 않는다(같은 대상을 어떻게 표기하느냐의 차이일 뿐이다). title은
    # manifest 저자가 쓴 자연어 문장이라 구분자(공백, 구두점)가 문장의
    # 읽는 방식과 뜻을 이룬다. 그래서 pack_id/source_label은 구분자를
    # 정규화하는 토큰 비교가 맞고, title은 원문 그대로의 substring 비교가
    # 맞다. 순서 보존 여부와 구분자 관용 여부는 서로 다른 축이라(#400
    # 원본 결함은 순서 축, 하이픈-공백 관용은 별개로 이미 받아들여진
    # 기능) 세 보너스가 서로 다른 두 방식을 쓰는 것은 통일해야 할 불일치가
    # 아니다.
    q_whole_seq = _ordered_whole_tokens(question)

    pack_id_seq = _ordered_whole_tokens(pack.pack_id)
    if pack_id_seq and _phrase_tokens_in_order(pack_id_seq, q_whole_seq):
        score += 100.0
        matched.append(f"pack_id:{pack.pack_id}")

    title_lower = pack.title.lower()
    if title_lower and _phrase_at_boundary(title_lower, q_lower):
        score += 50.0
        matched.append("title")

    if pack.source_label:
        source_seq = _ordered_whole_tokens(pack.source_label)
        if source_seq and _phrase_tokens_in_order(source_seq, q_whole_seq):
            score += 30.0
            matched.append(f"source:{pack.source_label}")

    title_tokens = _tokens(pack.title)
    overlap_title = q_aliases & title_tokens
    if overlap_title:
        score += 5.0 * len(overlap_title)
        matched.extend(sorted(overlap_title))

    desc_tokens = _tokens(pack.description)
    overlap_desc = q_aliases & desc_tokens
    if overlap_desc:
        score += 3.0 * len(overlap_desc)
        matched.extend(sorted(overlap_desc))

    kw_tokens = {k.lower() for k in pack.keywords}
    overlap_kw = q_aliases & kw_tokens
    if overlap_kw:
        score += 5.0 * len(overlap_kw)
        matched.extend(sorted(overlap_kw))

    tag_tokens = {t.lower() for t in pack.tags}
    overlap_tags = q_aliases & tag_tokens
    if overlap_tags:
        score += 4.0 * len(overlap_tags)
        matched.extend(sorted(overlap_tags))

    # Korean alias bonus: any explicit alias hit adds +20 once.
    # #400: 질의 쪽 판정을 substring(q_lower) 에서 whole-token 멤버십(q_whole)
    # 으로 바꿔 fragment 우연 일치로 보너스가 열리지 않게 한다.
    for canonical, variants in _ALIASES.items():
        if any(v.lower() in q_whole for v in variants) and any(
            v.lower() in (pack.title + " " + pack.description + " " + pack.pack_id).lower()
            for v in variants
        ):
            score += 20.0
            matched.append(f"alias:{canonical}")
            break

    # De-duplicate matched keys while preserving order
    seen: set[str] = set()
    unique_matched: list[str] = []
    for item in matched:
        if item not in seen:
            seen.add(item)
            unique_matched.append(item)
    return score, unique_matched


def choose_packs(
    question: str,
    packs: list[PackInfo],
    limit: int = 1,
    min_score: float | None = None,
) -> list[tuple[PackInfo, float, list[str]]]:
    """Score every pack against ``question`` and return the top candidates.

    ``min_score`` defaults to ``OPENCRAB_AUTO_PACK_MIN_SCORE`` env (10.0).
    Returns an empty list when no pack clears the threshold.
    """
    if not packs:
        return []
    threshold = _env_min_score() if min_score is None else float(min_score)

    scored: list[tuple[PackInfo, float, list[str]]] = []
    for pack in packs:
        score, matched = score_pack(question, pack)
        if score >= threshold and score > 0.0:
            scored.append((pack, score, matched))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[: max(1, limit)]


# ---------------------------------------------------------------------------
# #400 §4: BM25/FTS 콘텐츠 폴백 — score_pack()의 게이트가 전부 닫혔을 때(질의의
# whole 토큰이 pack_id/title/description/keywords/tags/source_label 어디에도
# 없을 때)만 쓰는 마지막 안전망. 팩이 title/description에 없는 고유명사·코드도
# 실제로 가진 노드 본문(text)에는 있을 수 있으므로, 팩별로 BM25/FTS를 probe해
# 원문과 질의의 whole 토큰 집합이 겹치는 팩을 고른다.
# ---------------------------------------------------------------------------

PER_PACK_PROBE_LIMIT = 50


def _accumulate(
    question: str,
    pid: str,
    bm25_hits: list[dict[str, Any]],
    fts_hits: list[dict[str, Any]],
    pack_hits: dict[str, set[str]],
    pack_hit_scores: dict[str, float],
) -> None:
    """``bm25_hits``/``fts_hits`` (원시 결과) 가운데 실제로 ``pid`` 팩에
    속하고 원문(text)이 질의의 whole 토큰과 겹치는 것만 누적한다.

    substring 스캔이 아니라 토큰 집합 대 토큰 집합 비교다 -- "art"가
    "cartography" 안에서 우연히 잡히는 것과 같은 모양의 오탐을 막는다
    (§4.3). BM25 히트는 top-level ``pack_id``, FTS 히트는
    ``metadata.pack_id``에 소속 팩이 실려 온다.
    """
    q_whole = _resolve_aliases_whole(_whole_tokens(question))
    for hit in (*bm25_hits, *fts_hits):
        hit_pid = hit.get("pack_id") or (hit.get("metadata") or {}).get("pack_id")
        text = hit.get("text") or ""
        if hit_pid != pid or not text:
            continue
        hit_whole = _whole_tokens(text)
        matched = q_whole & hit_whole
        if not matched:
            continue
        pack_hits.setdefault(pid, set()).update(matched)
        pack_hit_scores[pid] = pack_hit_scores.get(pid, 0.0) + float(hit.get("score") or 0.0)


def _choose_by_content(
    question: str,
    registry: list[PackInfo],
    hybrid: Any,
    spaces: list[str] | None,
    min_score: float | None = None,
) -> tuple[list[tuple[PackInfo, float, list[str]]], list[str]]:
    """``choose_packs``가 빈 리스트를 낸 뒤에만 호출되는 콘텐츠 폴백.

    팩별로 개별 조회한다(§4.2) -- 전역 한 번 조회는 작은 팩의 히트가 큰
    팩에 밀려 사라지는 문제(§7 항목6)가 있다. 반환값은
    ``(candidates, truncated_packs)``: ``truncated_packs``는 히트 수가
    ``PER_PACK_PROBE_LIMIT``에 닿아 그 팩의 실제 매치가 더 있을 수 있음을
    관측용으로 알리는 목록이다(§7 항목9, 선택 결과 자체에는 영향 없음).

    선택 기준은 (겹친 고유 토큰 수, 누적 BM25/FTS 점수, pack_id) 내림차순 --
    더 많은 서로 다른 질의어를 커버하는 팩을 우선하고, 동점이면 결정론적으로
    pack_id로 가른다(§7 항목11). 반환 점수는 실제 BM25 점수가 아니라 문턱값
    그 자체다(§4.4) -- score_pack()의 가산 점수와 척도가 달라 비교 불가능
    하므로, "문턱을 넘었다"는 사실만 표현한다(§7 항목12: 문턱을 바꾸면 이
    보고 점수도 그 값을 따른다).
    """
    if not registry or hybrid is None:
        return [], []
    threshold = _env_min_score() if min_score is None else float(min_score)

    pack_hits: dict[str, set[str]] = {}
    pack_hit_scores: dict[str, float] = {}
    truncated_packs: list[str] = []
    for pid in (p.pack_id for p in registry):
        bm25_hits = hybrid._bm25_search(  # noqa: SLF001 — 내부 프로브 전용 호출
            question, spaces, PER_PACK_PROBE_LIMIT, pack_ids=[pid]
        )
        fts_hits = hybrid._fts_search(  # noqa: SLF001 — 내부 프로브 전용 호출
            question, spaces, PER_PACK_PROBE_LIMIT, pack_ids=[pid]
        )
        if len(bm25_hits) >= PER_PACK_PROBE_LIMIT or len(fts_hits) >= PER_PACK_PROBE_LIMIT:
            truncated_packs.append(pid)
        _accumulate(question, pid, bm25_hits, fts_hits, pack_hits, pack_hit_scores)

    if not pack_hits:
        return [], truncated_packs

    best_pid = max(pack_hits, key=lambda k: (len(pack_hits[k]), pack_hit_scores.get(k, 0.0), k))
    pack = next(p for p in registry if p.pack_id == best_pid)
    matched_tokens = sorted(pack_hits[best_pid])
    return [(pack, threshold, matched_tokens)], truncated_packs


def build_candidate_registry(
    sql_rows: list[dict[str, Any]], fs_packs: list[PackInfo]
) -> list[PackInfo]:
    """Merge SQL ``packs`` rows with filesystem manifest data into ``PackInfo``.

    #397: the SQL rows define the candidate scope. A pack_id that exists
    only in a filesystem manifest, with no matching SQL row, does not enter
    the returned list -- the SQL ``packs`` table is the read-scope authority
    (#143), so admitting a manifest-only pack here would open a path around
    that scope. ``title``/``description`` come from the SQL row when it has
    a non-empty value; the manifest fills them only when the SQL row does
    not, since ``pack_publish`` keeps the SQL row current while the
    manifest is a load-time snapshot. Every other field
    (``source_label``, ``source_url``, ``keywords``, ``tags``, ``counts``,
    ``path``, ``manifest_path``) is manifest-only data with no SQL
    equivalent, so it is copied over whenever a manifest for the same
    pack_id exists and left at the ``PackInfo`` default otherwise.
    """
    fs_by_id = {p.pack_id: p for p in fs_packs}
    registry: list[PackInfo] = []
    for row in sql_rows:
        pack_id = row["pack_id"]
        fs = fs_by_id.get(pack_id)
        title = row.get("title") or (fs.title if fs else "") or ""
        description = row.get("description") or (fs.description if fs else "") or ""
        registry.append(
            PackInfo(
                pack_id=pack_id,
                title=title,
                description=description,
                version=fs.version if fs else "",
                source_label=fs.source_label if fs else None,
                source_url=fs.source_url if fs else None,
                path=fs.path if fs else Path(),
                manifest_path=fs.manifest_path if fs else Path(),
                counts=dict(fs.counts) if fs else {},
                keywords=list(fs.keywords) if fs else [],
                tags=list(fs.tags) if fs else [],
                raw=dict(fs.raw) if fs else {},
            )
        )
    return registry


# ---------------------------------------------------------------------------
# Optional in-memory cache for long-running processes (MCP)
# ---------------------------------------------------------------------------


class PackRegistryCache:
    """Mtime-aware lazy cache of pack registry.

    Designed for long-running processes (MCP server). CLI callers should
    use ``load_pack_registry`` directly so they always observe the latest
    manifest.
    """

    def __init__(self, local_data_dir: str | Path) -> None:
        self._local_data_dir = Path(local_data_dir)
        self._packs: list[PackInfo] = []
        self._mtime: float = -1.0
        self._lock = threading.Lock()

    def _packs_dir_mtime(self) -> float:
        root = self._local_data_dir / "packs"
        if not root.is_dir():
            return -1.0
        latest = root.stat().st_mtime
        for entry in root.iterdir():
            if entry.is_dir():
                try:
                    latest = max(latest, entry.stat().st_mtime)
                    manifest = entry / "stage" / "manifest.json"
                    if manifest.is_file():
                        latest = max(latest, manifest.stat().st_mtime)
                except OSError:
                    pass
        return latest

    def packs(self) -> list[PackInfo]:
        with self._lock:
            mtime = self._packs_dir_mtime()
            if mtime != self._mtime:
                self._packs = load_pack_registry(self._local_data_dir)
                self._mtime = mtime
            return list(self._packs)
