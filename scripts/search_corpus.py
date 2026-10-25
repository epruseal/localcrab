#!/usr/bin/env python3
"""Synthetic search corpus and gold query set for issue #542.

Design: docs/issue-542-search-baseline.md (sections 3, 5 and 6).

The generator uses the standard library only. Every string comes from seeded
generation rules. The generator copies no real pack name, content or count.
The same seed and preset produce byte-identical files. The generator writes
documents as a stream, so a large preset does not hold every body in memory.

Usage:
    python scripts/search_corpus.py generate --preset S1 --seed 542 --out DIR
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GENERATOR_VERSION = 1
TIMING_FILE = "generate-timing.json"
LIMIT = 10

# Fixed principal ids. opencrab.auth.create_user draws random ids, and a random
# owner_id would enter node properties and BM25 text (design section 5.3).
USER_A = "user_a"
USER_B = "user_b"

# BM25 tokenises the first OPENCRAB_BM25_MAX_NODE_CHARS characters of a node's
# text (default 65536). A marker below BM25_SAFE_OFFSET is inside that window
# with a margin for the node_id and node_type prefix. A long-tail marker is at
# or after LONG_TAIL_OFFSET, past the window. No marker lies between the two.
BM25_SAFE_OFFSET = 60000
LONG_TAIL_OFFSET = 70000
LONG_BODY_CHARS = 75000

# Fractions of the global recency order (design section 3.1).
OLD_FRACTION = 0.30
RECENT_FRACTION = 0.10
SOURCE_FRACTION = 0.25
CONCEPT_FRACTION = 0.10

# Character budget of the default BM25 window (OPENCRAB_BM25_CHAR_BUDGET).
DEFAULT_CHAR_BUDGET = 200_000_000

PRESETS: dict[str, dict[str, Any]] = {
    "S0": {
        "large": [400, 400], "small": [60, 60, 40], "other_private": 60,
        "other_public": 60, "creating": 30, "unpackaged": 20, "measure": 40,
        "per_kind": 1, "long_docs": 4, "pressure_decoys": 12, "vocab": 1500,
    },
    "S1": {
        "large": [8000, 8000], "small": [600, 600, 300], "other_private": 1200,
        "other_public": 1000, "creating": 150, "unpackaged": 100, "measure": 200,
        "per_kind": 3, "long_docs": 12, "pressure_decoys": 40, "vocab": 5000,
    },
    "S2": {
        "large": [51000, 5000], "small": [1000, 1000, 500], "other_private": 1000,
        "other_public": 1000, "creating": 200, "unpackaged": 100, "measure": 300,
        "per_kind": 3, "long_docs": 20, "pressure_decoys": 60, "vocab": 8000,
    },
    "S3": {
        "large": [40000, 40000, 30000], "small": [1500, 1500, 1000, 1000],
        "other_private": 2000, "other_public": 2000, "creating": 300,
        "unpackaged": 200, "measure": 500, "per_kind": 3, "long_docs": 30,
        "pressure_decoys": 80, "vocab": 10000,
    },
    "S4": {
        "large": [55000, 55000, 55000, 55000],
        "small": [2000, 2000, 1500, 1500, 1000, 1000], "other_private": 8000,
        "other_public": 8000, "creating": 2000, "unpackaged": 1000,
        "measure": 1000, "per_kind": 3, "long_docs": 40, "pressure_decoys": 100,
        "vocab": 15000,
    },
}

KINDS = (
    "exact-recent", "exact-old", "explicit-multi", "explicit-mixed",
    "explicit-forbidden", "whole-scope", "auto-content", "auto-title",
    "scope-filter", "space-filter", "korean-variant", "long-tail",
    "duplicate-body", "top-k-pressure",
)

# Hangul syllable index ranges (offset from U+AC00). General vocabulary and
# reserved marker syllables come from disjoint ranges. The particles used by
# korean-variant fall outside both ranges.
_GENERAL_SYLLABLES = range(0, 3000)
_RESERVED_SYLLABLES = range(9000, 10000)
_PARTICLES = ("에서", "으로", "에게")
# Latin general words never contain q, x or z. Markers start with "qx",
# pressure words with "qc", pack ids with "qpk".
_LATIN_CONS = "bcdfghjklmnprstvw"
_LATIN_VOWELS = "aeiou"


def _rng(seed: int, purpose: str) -> random.Random:
    return random.Random(f"{seed}:{purpose}")


def _hangul(indices: list[int]) -> str:
    return "".join(chr(0xAC00 + i) for i in indices)


class _Vocab:
    """Zipf-weighted general vocabulary, half Hangul and half Latin."""

    def __init__(self, seed: int, size: int) -> None:
        rng = _rng(seed, "vocab")
        words: list[str] = []
        seen: set[str] = set()
        while len(words) < size:
            if len(words) % 2 == 0:
                n = rng.randint(2, 4)
                word = _hangul([rng.choice(_GENERAL_SYLLABLES) for _ in range(n)])
            else:
                n = rng.randint(2, 4)
                word = "".join(
                    rng.choice(_LATIN_CONS) + rng.choice(_LATIN_VOWELS) for _ in range(n)
                )
            if word not in seen:
                seen.add(word)
                words.append(word)
        self.words = words
        total = 0.0
        self._cum: list[float] = []
        for rank in range(1, size + 1):
            total += 1.0 / (rank ** 1.1)
            self._cum.append(total)
        self._total = total

    def sample(self, rng: random.Random) -> str:
        return self.words[bisect.bisect_left(self._cum, rng.random() * self._total)]

    def sentence(self, rng: random.Random, chars: int) -> str:
        parts: list[str] = []
        length = 0
        while length < chars:
            word = self.sample(rng)
            parts.append(word)
            length += len(word) + 1
        return " ".join(parts)


class _Reserved:
    """Unique marker tokens that share nothing with the general vocabulary."""

    def __init__(self, seed: int) -> None:
        self._rng = _rng(seed, "reserved")
        self._seen: set[str] = set()

    def latin(self, prefix: str) -> str:
        while True:
            body = "".join(self._rng.choice(_LATIN_CONS + _LATIN_VOWELS) for _ in range(7))
            token = prefix + body
            if token not in self._seen:
                self._seen.add(token)
                return token

    def hangul(self, syllables: int = 3) -> str:
        while True:
            token = _hangul([self._rng.choice(_RESERVED_SYLLABLES) for _ in range(syllables)])
            if token not in self._seen:
                self._seen.add(token)
                return token


def _pack_table(preset: dict[str, Any], seed: int, vocab: _Vocab) -> dict[str, dict[str, Any]]:
    """Return role -> pack record. Titles are unique general-vocabulary phrases."""
    rng = _rng(seed, "packs")
    title_words: set[str] = set()

    def title() -> str:
        while True:
            words = [rng.choice(vocab.words[len(vocab.words) // 2:]) for _ in range(2)]
            if not (set(words) & title_words) and words[0] != words[1]:
                title_words.update(words)
                return " ".join(words)

    packs: dict[str, dict[str, Any]] = {}

    def add(role: str, owner: str, status: str, visibility: str, size: int, group: str) -> None:
        packs[role] = {
            "role": role, "pack_id": f"qpk-{role}", "owner": owner, "status": status,
            "visibility": visibility, "title": title(), "description": title(),
            "size": size, "group": group,
        }

    for i, size in enumerate(preset["large"], 1):
        add(f"l{i}", USER_A, "ready", "private", size, "large")
    for i, size in enumerate(preset["small"], 1):
        add(f"s{i}", USER_A, "ready", "private", size, "small")
    add("bpriv", USER_B, "ready", "private", preset["other_private"], "other_private")
    add("bpub", USER_B, "ready", "public-read", preset["other_public"], "other_public")
    add("creating", USER_A, "creating", "private", preset["creating"], "creating")
    add("measure", USER_A, "ready", "private", preset["measure"], "measure")
    return packs


def _doc_skeleton(preset: dict[str, Any], packs: dict[str, dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """Assign ids, packs, forms, spaces and global recency ranks."""
    rng = _rng(seed, "skeleton")
    docs: list[dict[str, Any]] = []
    for role, pack in packs.items():
        for i in range(pack["size"]):
            docs.append({"id": f"{role}-{i:06d}", "pack": role})
    for i in range(preset["unpackaged"]):
        docs.append({"id": f"u-{i:06d}", "pack": None})
    order = list(range(len(docs)))
    rng.shuffle(order)
    n = len(docs)
    for rank, index in enumerate(order):
        docs[index]["rank"] = rank
    for doc in docs:
        doc["form"] = "S" if (doc["pack"] is not None and rng.random() < SOURCE_FRACTION) else "N"
        doc["space"] = "concept" if (doc["form"] == "N" and rng.random() < CONCEPT_FRACTION) else "evidence"
        doc["old"] = doc["rank"] < math.floor(n * OLD_FRACTION)
        doc["recent"] = doc["rank"] >= math.ceil(n * (1 - RECENT_FRACTION))
    return docs


class _Planner:
    """Pick gold, forbidden and decoy documents and the text to inject."""

    def __init__(self, docs: list[dict[str, Any]], packs: dict[str, dict[str, Any]], seed: int) -> None:
        self.docs = docs
        self.packs = packs
        self.rng = _rng(seed, "plan")
        self.used: set[int] = set()
        self.by_pack: dict[str | None, list[int]] = {}
        for i, doc in enumerate(docs):
            self.by_pack.setdefault(doc["pack"], []).append(i)
        # doc index -> list of injections {"text", "tail"}
        self.inject: dict[int, list[dict[str, Any]]] = {}
        # doc index -> shared body (duplicate-body) or long body flag
        self.body: dict[int, str] = {}
        self.long: set[int] = set()
        self.force: dict[int, dict[str, Any]] = {}

    def pick(self, pack: str | None, **want: Any) -> int:
        pool = [i for i in self.by_pack[pack] if i not in self.used]
        for key, value in want.items():
            pool = [i for i in pool if self.docs[i].get(key) == value]
        if not pool:
            raise RuntimeError(f"no free document in pack {pack!r} with {want}")
        index = self.rng.choice(pool)
        self.used.add(index)
        return index

    def add(self, index: int, text: str, *, tail: bool = False) -> None:
        self.inject.setdefault(index, []).append({"text": text, "tail": tail})


def _plan_queries(
    preset: dict[str, Any], packs: dict[str, dict[str, Any]], docs: list[dict[str, Any]],
    seed: int, reserved: _Reserved,
) -> tuple[list[dict[str, Any]], _Planner]:
    plan = _Planner(docs, packs, seed)
    large = [r for r, p in packs.items() if p["group"] == "large"]
    small = [r for r, p in packs.items() if p["group"] == "small"]
    queries: list[dict[str, Any]] = []
    ids = lambda indices: [docs[i]["id"] for i in indices]  # noqa: E731

    def query(kind: str, k: int, text: str, *, pack_roles: list[str] | None,
              gold: list[int], forbidden: list[int], decoys: list[int] | None = None,
              auto: bool = False, spaces: list[str] | None = None,
              expect_scope_warning: bool = False, expect_empty: bool = False,
              expect_pack: str | None = None, long_tail: set[int] | None = None,
              fts_exact: bool = True) -> None:
        long_tail = long_tail or set()
        bm25 = [i for i in gold if i not in long_tail]
        fts = [i for i in gold if docs[i]["form"] == "S" and fts_exact]
        if pack_roles is None and not auto:
            shape = "whole_scope"
        elif auto:
            shape = "auto"
        elif len(pack_roles) == 1:
            shape = "explicit_single"
        else:
            shape = "explicit_multi"
        queries.append({
            "qid": f"{kind}-{k}", "kind": kind, "shape": shape, "text": text,
            "pack_ids": None if pack_roles is None else [packs[r]["pack_id"] for r in pack_roles],
            "auto_pack": auto, "spaces": spaces, "limit": LIMIT,
            "gold": ids(gold), "forbidden": ids(forbidden), "decoys": ids(decoys or []),
            "must_hit": {"bm25": ids(bm25), "fts": ids(fts), "vector": [], "combined": ids(gold)},
            "expect_scope_warning": expect_scope_warning, "expect_empty": expect_empty,
            "expect_pack": packs[expect_pack]["pack_id"] if expect_pack else None,
        })

    for k in range(preset["per_kind"]):
        la, lb = large[k % len(large)], large[(k + 1) % len(large)]
        sa = small[k % len(small)]
        sb = small[(k + 1) % len(small)]
        form = "S" if k % 2 == 0 else "N"

        # The first query must be an explicit single-pack request (cold boundary).
        m = reserved.latin("qx")
        g = plan.pick(la, recent=True, form=form)
        f = plan.pick(lb)
        plan.add(g, m)
        plan.add(f, m)
        query("exact-recent", k, m, pack_roles=[la], gold=[g], forbidden=[f])

        m = reserved.latin("qx")
        g = plan.pick(la, old=True, form=form)
        f = plan.pick(lb)
        plan.add(g, m)
        plan.add(f, m)
        query("exact-old", k, m, pack_roles=[la], gold=[g], forbidden=[f])

        m = reserved.latin("qx")
        g1, g2 = plan.pick(la, form=form), plan.pick(sa)
        f = plan.pick(lb)
        for i in (g1, g2, f):
            plan.add(i, m)
        query("explicit-multi", k, m, pack_roles=[la, sa], gold=[g1, g2], forbidden=[f])

        m = reserved.latin("qx")
        g = plan.pick(sb, form=form)
        f1, f2 = plan.pick("bpriv"), plan.pick("creating")
        for i in (g, f1, f2):
            plan.add(i, m)
        query("explicit-mixed", k, m, pack_roles=[sb, "bpriv", "creating"], gold=[g],
              forbidden=[f1, f2], expect_scope_warning=True)

        m = reserved.latin("qx")
        f = plan.pick("bpriv")
        plan.add(f, m)
        query("explicit-forbidden", k, m, pack_roles=["bpriv"], gold=[], forbidden=[f],
              expect_scope_warning=True, expect_empty=True)

        m = reserved.latin("qx")
        g1, g2 = plan.pick(lb, form=form), plan.pick("bpub")
        f1, f2, f3 = plan.pick("bpriv"), plan.pick("creating"), plan.pick(None)
        for i in (g1, g2, f1, f2, f3):
            plan.add(i, m)
        query("whole-scope", k, m, pack_roles=None, gold=[g1, g2], forbidden=[f1, f2, f3])

        m = reserved.latin("qx")
        g = plan.pick(sa, form=form)
        f = plan.pick("bpriv")
        plan.add(g, m)
        plan.add(f, m)
        query("auto-content", k, m, pack_roles=None, gold=[g], forbidden=[f], auto=True,
              expect_pack=sa)

        query("auto-title", k, packs[sb]["title"], pack_roles=None, gold=[], forbidden=[],
              auto=True, expect_pack=sb)

        m = reserved.latin("qx")
        g = plan.pick(la, recent=False, old=False, form=form)
        f = plan.pick(lb, recent=True)
        plan.add(g, m)
        plan.add(f, m)
        query("scope-filter", k, m, pack_roles=[la], gold=[g], forbidden=[f])

        m = reserved.latin("qx")
        g = plan.pick(la, form="N", space="concept")
        f = plan.pick(la, space="evidence")
        plan.add(g, m)
        plan.add(f, m)
        query("space-filter", k, m, pack_roles=[la], gold=[g], forbidden=[f], spaces=["concept"])

        stem = reserved.hangul(3)
        g = plan.pick(la, form=form)
        f = plan.pick(lb)
        decoys = [plan.pick(la) for _ in range(3)]
        plan.add(g, stem + _PARTICLES[k % len(_PARTICLES)])
        plan.add(f, stem + _PARTICLES[(k + 1) % len(_PARTICLES)])
        prefix = stem[:2]
        for d in decoys:
            plan.add(d, prefix + _hangul([plan.rng.choice(_RESERVED_SYLLABLES)]))
        query("korean-variant", k, stem, pack_roles=[la], gold=[g], forbidden=[f],
              decoys=decoys, fts_exact=False)

        m = reserved.latin("qx")
        g = plan.pick(lb, form=form)
        plan.long.add(g)
        plan.add(g, m, tail=True)
        query("long-tail", k, m, pack_roles=[lb], gold=[g], forbidden=[], long_tail={g})

        m = reserved.latin("qx")
        g1, g2 = plan.pick(la, form="S"), plan.pick(la, form="S")
        f = plan.pick(lb, form="S")
        shared = f"{m} " + " ".join(reserved.latin("qd") for _ in range(12))
        for i in (g1, g2, f):
            plan.body[i] = shared
        query("duplicate-body", k, m, pack_roles=[la], gold=[g1, g2], forbidden=[f])

        rare = reserved.latin("qx")
        common = reserved.latin("qc")
        g = plan.pick(la, form=form)
        f = plan.pick(lb)
        decoys = [plan.pick(la) for _ in range(preset["pressure_decoys"])]
        plan.add(g, f"{rare} {common}")
        plan.add(f, f"{rare} {common}")
        for d in decoys:
            plan.add(d, " ".join([common] * 5))
        query("top-k-pressure", k, f"{rare} {common}", pack_roles=[la], gold=[g],
              forbidden=[f], decoys=decoys)

    # Remaining long bodies without a marker give the size distribution its tail.
    extra = preset["long_docs"] - len(plan.long)
    for _ in range(max(0, extra)):
        role = large[plan.rng.randrange(len(large))]
        plan.long.add(plan.pick(role))
    return queries, plan


def _body(doc: dict[str, Any], index: int, plan: _Planner, vocab: _Vocab, seed: int) -> tuple[str, list[int]]:
    """Return the body and the character offsets of injected text."""
    rng = _rng(seed, f"body:{doc['id']}")
    if index in plan.body:
        return plan.body[index], [0]
    if index in plan.long:
        base = vocab.sentence(rng, LONG_BODY_CHARS)
    else:
        base = vocab.sentence(rng, max(80, min(4000, int(rng.lognormvariate(6.0, 0.6)))))
    offsets: list[int] = []
    words = base.split(" ")
    head = [inj for inj in plan.inject.get(index, []) if not inj["tail"]]
    tail = [inj for inj in plan.inject.get(index, []) if inj["tail"]]
    for inj in head:
        pos = rng.randrange(0, min(len(words), 40) + 1)
        words.insert(pos, inj["text"])
    text = " ".join(words)
    for inj in head:
        offsets.append(text.index(inj["text"]))
    if tail:
        # The first word boundary at or after LONG_TAIL_OFFSET, so every tail
        # marker starts past the BM25 tokenisation window.
        cut = text.find(" ", LONG_TAIL_OFFSET)
        if cut < 0:
            raise RuntimeError(f"long body shorter than the tail offset: {doc['id']}")
        text = text[:cut] + " " + " ".join(inj["text"] for inj in tail) + text[cut:]
        for inj in tail:
            offsets.append(text.index(inj["text"]))
    return text, offsets


def _stamped_size(doc: dict[str, Any], pack: dict[str, Any] | None) -> int:
    """Stored properties JSON length after write_gate.stamp (design 3.1)."""
    if pack is None:
        props = {"title": doc["title"], "text": doc["text"], "pack_id": ""}
    else:
        props = {"title": doc["title"], "text": doc["text"], "pack_id": pack["pack_id"],
                 "owner_id": pack["owner"]}
    return len(json.dumps(props, ensure_ascii=True))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def generate(preset_name: str, seed: int, out: Path) -> dict[str, Any]:
    """Write the corpus into ``out`` (must not exist) and return the manifest."""
    if preset_name not in PRESETS:
        raise SystemExit(f"unknown preset {preset_name!r}")
    if out.exists():
        raise SystemExit(f"output directory already exists: {out}")
    preset = PRESETS[preset_name]
    vocab = _Vocab(seed, preset["vocab"])
    reserved = _Reserved(seed)
    packs = _pack_table(preset, seed, vocab)
    docs = _doc_skeleton(preset, packs, seed)
    queries, plan = _plan_queries(preset, packs, docs, seed, reserved)

    # Invariant: marker tokens never occur in the general vocabulary.
    vocab_set = set(vocab.words)
    for q in queries:
        for token in q["text"].split():
            if q["kind"] != "auto-title" and token in vocab_set:
                raise RuntimeError(f"marker collides with vocabulary: {q['qid']}")

    out.mkdir(parents=True)
    base = datetime(2020, 1, 1, tzinfo=UTC)
    node_count = source_count = 0
    stored_chars = 0
    marker_offsets: dict[str, list[int]] = {}
    with (out / "docs.jsonl").open("w", encoding="utf-8") as fh:
        for index, doc in enumerate(docs):
            text, offsets = _body(doc, index, plan, vocab, seed)
            rng = _rng(seed, f"meta:{doc['id']}")
            record = {
                "id": doc["id"],
                "pack": doc["pack"],
                "form": doc["form"],
                "space": doc["space"],
                # Node types follow the grammar manifest for the space, so the
                # real add_node path accepts the same rows.
                "node_type": ("TextUnit" if doc["form"] == "S"
                              else "Concept" if doc["space"] == "concept" else "Evidence"),
                "title": vocab.sentence(rng, 20),
                "text": text,
                "updated_at": (base + timedelta(minutes=doc["rank"])).isoformat(),
                "rank": doc["rank"],
                "old": doc["old"],
                "recent": doc["recent"],
            }
            if index in plan.inject or index in plan.body:
                marker_offsets[doc["id"]] = offsets
            for offset in offsets:
                if BM25_SAFE_OFFSET <= offset < LONG_TAIL_OFFSET:
                    raise RuntimeError(f"marker offset inside the guard band: {doc['id']}")
            stored_chars += _stamped_size(record, packs.get(doc["pack"]) if doc["pack"] else None)
            node_count += 1
            source_count += record["form"] == "S"
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    if stored_chars >= DEFAULT_CHAR_BUDGET:
        raise RuntimeError("stored properties exceed the default BM25 character budget")

    pack_records = {
        role: {k: v for k, v in pack.items() if k != "size"} | {"size": pack["size"]}
        for role, pack in packs.items()
    }
    (out / "packs.json").write_text(
        json.dumps({"users": [USER_A, USER_B], "packs": pack_records}, ensure_ascii=False,
                   sort_keys=True, indent=1) + "\n", encoding="utf-8")
    (out / "queries.json").write_text(
        json.dumps(queries, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "preset": preset_name,
        "seed": seed,
        "node_count": node_count,
        "source_count": source_count,
        "query_count": len(queries),
        "stored_properties_chars": stored_chars,
        "files": {name: _sha256(out / name) for name in ("docs.jsonl", "packs.json", "queries.json")},
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return manifest


def manifest_digest(corpus: Path) -> str:
    """Digest that binds the manifest to the three data files."""
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if _sha256(corpus / name) != digest:
            raise ValueError(f"corpus file digest mismatch: {name}")
    return hashlib.sha256((corpus / "manifest.json").read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--preset", required=True, choices=sorted(PRESETS))
    gen.add_argument("--seed", type=int, required=True)
    gen.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "generate":
        out = Path(args.out).resolve()
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from search_baseline import Watchdog, check_write_paths, preset_is_smoke

        smoke = preset_is_smoke(args.preset)
        check_write_paths([out], smoke=smoke)
        out.parent.mkdir(parents=True, exist_ok=True)
        watchdog = Watchdog(args.preset, "generate", out.parent / f"{out.name}.aborted.json",
                            [out.parent], smoke=smoke)
        watchdog.start()
        manifest = generate(args.preset, args.seed, out)
        # Outside manifest["files"], so the corpus digest stays deterministic.
        # load subtracts this from the shared generate+load budget (design 9.2).
        (out / TIMING_FILE).write_text(
            json.dumps({"generate_seconds": round(watchdog.elapsed(), 3)}) + "\n", encoding="utf-8")
        print(json.dumps({"node_count": manifest["node_count"], "query_count": manifest["query_count"],
                          "files": manifest["files"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
