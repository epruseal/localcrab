"""
Pack registry — scans ``<local_data_dir>/packs/*/stage/manifest.json`` and
exposes deterministic auto-pack selection.

Public API:
    load_pack_registry(local_data_dir)
    get_pack(local_data_dir, pack_id)
    choose_packs(question, packs, min_score=...)

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


def _resolve_aliases(question_tokens: set[str]) -> set[str]:
    expanded = set(question_tokens)
    for canonical, variants in _ALIASES.items():
        for variant in variants:
            if variant.lower() in question_tokens:
                expanded.add(canonical)
                expanded.update(v.lower() for v in variants)
                break
    return expanded


def _score_pack(question: str, pack: PackInfo) -> tuple[float, list[str]]:
    q_lower = (question or "").lower()
    if not q_lower:
        return 0.0, []

    q_tokens = _tokens(question)
    q_aliases = _resolve_aliases(q_tokens)

    matched: list[str] = []
    score = 0.0

    pack_id_lower = pack.pack_id.lower()
    if pack_id_lower and pack_id_lower in q_lower:
        score += 100.0
        matched.append(f"pack_id:{pack.pack_id}")

    title_lower = pack.title.lower()
    if title_lower and title_lower in q_lower:
        score += 50.0
        matched.append("title")

    if pack.source_label:
        source_lower = pack.source_label.lower()
        if source_lower and source_lower in q_lower:
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
    for canonical, variants in _ALIASES.items():
        if any(v.lower() in q_lower for v in variants) and any(
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
        score, matched = _score_pack(question, pack)
        if score >= threshold and score > 0.0:
            scored.append((pack, score, matched))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[: max(1, limit)]


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
