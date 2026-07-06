"""
Shared tokenisation and intent-cue constants for the query/BM25/reranker stack.

Centralises pieces that used to be duplicated verbatim between bm25.py and
reranker.py (the Hangul-aware BM25 tokenizer), and the relation/multi-hop
"intent cue" word lists that query.py uses to widen retrieval depth and that
reranker.py uses to boost graph/keyword sources during rerank.

NOTE: ``QUERY_RELATION_CUES`` (query.py's former ``_RELATION_QUERY_CUES``) and
``RERANK_RELATION_CUES`` (reranker.py's former ``_RELATION_CUES``) were
near-duplicates that predate this extraction: the query-side list had
"applicable" where the rerank-side list had "because" instead. Unified
2026-07-06 by user decision (recall-favoring) into one canonical
``RELATION_CUES`` tuple; both names are now aliases of it.
"""

from __future__ import annotations

import re

HANGUL_RE = re.compile(r"[가-힣]")


def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace.

    Korean construction benchmarks contain many compound nouns and short
    relation cues ("변경 이유", "적용 불가", "개정"). Whitespace tokenisation
    alone misses those matches, so Hangul tokens also contribute 2- and 3-char
    n-grams. This keeps exact English behaviour while making Korean recall
    much less brittle.
    """
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens: list[str] = []
    for token in text.split():
        if not token:
            continue
        tokens.append(token)
        if HANGUL_RE.search(token) and len(token) >= 3:
            for n in (2, 3):
                tokens.extend(token[i : i + n] for i in range(0, len(token) - n + 1))
    return tokens


# Canonical relation-cue set. Widens vector/bm25/graph limits and graph_depth
# in query.py, and boosts graph/keyword sources during rerank.py's rerank —
# both consumers share this single word list (see module note above).
RELATION_CUES = (
    "why",
    "reason",
    "rationale",
    "change",
    "revision",
    "background",
    "cannot",
    "because",
    "applicable",
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

# Back-compat aliases for the existing per-module import names.
QUERY_RELATION_CUES = RELATION_CUES

# query.py: additionally widens graph_depth up to 3 for multi-hop questions.
QUERY_MULTIHOP_CUES = (
    "connect",
    "relationship",
    "multi",
    "chain",
    "cause",
    "effect",
    "연결",
    "관계",
    "원인",
    "영향",
    "단계",
    "구분",
)

RERANK_RELATION_CUES = RELATION_CUES
