"""
Result Reranker — score-fusion and BM25-based cross-reranking.

Takes a mixed list of QueryResult objects (from vector + BM25 + graph sources)
and produces a unified ranking using reciprocal rank fusion (RRF) as the
primary method, with an optional BM25 cross-score pass for text-heavy queries.

Approach:
  - Reciprocal Rank Fusion (RRF): robust, parameter-free, handles multiple
    result lists without needing to normalise scores across different scales.
  - BM25 cross-score: when the original query text is available, additionally
    scores each result's text against the query using in-memory BM25.
  - Final score = alpha * rrf_score + (1-alpha) * bm25_cross_score

RRF constant k=60 (standard, prevents high-rank docs dominating).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_RRF_K = 60
_ALPHA = 0.7   # weight for RRF vs BM25 cross-score
_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens: list[str] = []
    for token in text.split():
        if not token:
            continue
        tokens.append(token)
        if re.search(r"[가-힣]", token) and len(token) >= 3:
            for n in (2, 3):
                tokens.extend(token[i : i + n] for i in range(0, len(token) - n + 1))
    return tokens


_RELATION_CUES = (
    "why",
    "reason",
    "rationale",
    "change",
    "revision",
    "background",
    "because",
    "cannot",
    "risk",
    "law",
    "regulation",
    "이유",
    "변경",
    "개정",
    "배경",
    "불가",
    "불가능",
    "위험",
    "법규",
    "조합",
    "관계",
    "연결",
)
_SOURCE_WEIGHTS = {
    "bm25": 1.08,
    "vector": 1.0,
    "graph": 0.96,
    "hybrid": 1.0,
}


def _query_has_relation_intent(query: str) -> bool:
    q = query.lower()
    return any(cue in q for cue in _RELATION_CUES)


def _item_text(item: dict[str, Any]) -> str:
    parts = [str(item.get("text") or "")]
    metadata = item.get("metadata") or {}
    graph_context = item.get("graph_context") or {}
    try:
        parts.append(str(metadata))
        parts.append(str(graph_context))
    except Exception:
        pass
    return " ".join(parts).lower()


def _intent_boost(query: str, item: dict[str, Any]) -> float:
    if not _query_has_relation_intent(query):
        return 1.0

    source = str(item.get("source") or "hybrid")
    sources = set(item.get("sources") or [source])
    boost = max(_SOURCE_WEIGHTS.get(str(candidate), 1.0) for candidate in sources)
    haystack = _item_text(item)

    matched_cues = sum(1 for cue in _RELATION_CUES if cue in query.lower() and cue in haystack)
    if matched_cues:
        boost += min(0.24, matched_cues * 0.08)
    if "graph" in sources:
        boost += 0.12

    return min(boost, 1.35)


def _merge_duplicate(existing: dict[str, Any], item: dict[str, Any]) -> None:
    """Merge duplicate node hits so consensus survives reranking."""
    existing_sources = set(existing.get("sources") or [existing.get("source", "hybrid")])
    existing_sources.add(item.get("source", "hybrid"))
    existing["sources"] = sorted(str(s) for s in existing_sources if s)
    if len(existing["sources"]) > 1:
        existing["source"] = "hybrid"

    existing["score"] = max(float(existing.get("score") or 0.0), float(item.get("score") or 0.0))

    old_text = existing.get("text") or ""
    new_text = item.get("text") or ""
    if new_text and new_text not in old_text:
        existing["text"] = f"{old_text}\n{new_text}".strip()[:4000]

    metadata = dict(existing.get("metadata") or {})
    metadata.update(item.get("metadata") or {})
    existing["metadata"] = metadata

    if not existing.get("graph_context") and item.get("graph_context"):
        existing["graph_context"] = item["graph_context"]


def _bm25_cross_score(query_tokens: list[str], doc_tokens: list[str], avgdl: float) -> float:
    """BM25 score for a single (query, doc) pair."""
    if not query_tokens or not doc_tokens:
        return 0.0
    dl = len(doc_tokens)
    tf_map = Counter(doc_tokens)
    n = 1  # single doc, IDF degenerates; use tf-based saturation only
    score = 0.0
    for term in query_tokens:
        tf = tf_map.get(term, 0)
        if tf == 0:
            continue
        idf = 1.0  # flat IDF since we can't compute corpus DF here
        num = tf * (_K1 + 1)
        den = tf + _K1 * (1 - _B + _B * dl / max(avgdl, 1))
        score += idf * (num / den)
    return score


class Reranker:
    """
    Reranks a list of QueryResult dicts using RRF + optional BM25 cross-scoring.

    Input format: list of dicts with keys source, node_id, score, text, metadata.
    (Compatible with QueryResult.to_dict().)
    """

    def rerank(
        self,
        query: str,
        result_lists: list[list[dict[str, Any]]],
        top_k: int = 10,
        use_bm25_cross: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Merge multiple result lists and rerank using RRF + BM25.

        Parameters
        ----------
        query:
            Original query string.
        result_lists:
            One list per source (e.g. [vector_results, bm25_results, graph_results]).
            Each item must have 'node_id' and optionally 'score', 'text'.
        top_k:
            Number of results to return.
        use_bm25_cross:
            If True, supplement RRF with a BM25 cross-score over result texts.

        Returns
        -------
        Reranked list of result dicts with added 'rerank_score' field.
        """
        # Collect all unique results keyed by node_id
        all_results: dict[str, dict[str, Any]] = {}
        for results in result_lists:
            for item in results:
                nid = item.get("node_id")
                if not nid:
                    continue
                if nid not in all_results:
                    all_results[nid] = dict(item)
                    all_results[nid]["sources"] = [item.get("source", "hybrid")]
                else:
                    _merge_duplicate(all_results[nid], item)

        if not all_results:
            return []

        # RRF: accumulate reciprocal rank from each source list
        rrf_scores: dict[str, float] = defaultdict(float)
        for results in result_lists:
            for rank, item in enumerate(results):
                nid = item.get("node_id")
                if nid:
                    rrf_scores[nid] += 1.0 / (_RRF_K + rank + 1)

        # Normalise RRF scores to [0, 1]
        max_rrf = max(rrf_scores.values()) if rrf_scores else 1.0
        if max_rrf == 0:
            max_rrf = 1.0

        # BM25 cross-score (query vs each result's text)
        q_tokens = _tokenize(query)
        bm25_cross: dict[str, float] = {}

        if use_bm25_cross and q_tokens:
            doc_texts = {
                nid: _tokenize(item.get("text") or "")
                for nid, item in all_results.items()
            }
            avg_dl = sum(len(t) for t in doc_texts.values()) / max(len(doc_texts), 1)
            raw_bm25 = {
                nid: _bm25_cross_score(q_tokens, toks, avg_dl)
                for nid, toks in doc_texts.items()
            }
            max_bm25 = max(raw_bm25.values()) if raw_bm25 else 1.0
            if max_bm25 == 0:
                max_bm25 = 1.0
            bm25_cross = {nid: s / max_bm25 for nid, s in raw_bm25.items()}

        # Final score fusion
        final: list[tuple[str, float]] = []
        for nid in all_results:
            rrf = rrf_scores.get(nid, 0.0) / max_rrf
            bm25 = bm25_cross.get(nid, 0.0) if use_bm25_cross else 0.0
            if use_bm25_cross:
                score = _ALPHA * rrf + (1 - _ALPHA) * bm25
            else:
                score = rrf
            source_count = len(all_results[nid].get("sources") or [])
            if source_count > 1:
                score += min(0.1, 0.04 * (source_count - 1))
            score *= _intent_boost(query, all_results[nid])
            final.append((nid, round(score, 4)))

        final.sort(key=lambda x: x[1], reverse=True)

        output = []
        for nid, score in final[:top_k]:
            item = dict(all_results[nid])
            item["rerank_score"] = score
            output.append(item)

        return output
