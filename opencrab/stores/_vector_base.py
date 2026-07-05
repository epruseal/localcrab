"""
Shared vector-store helpers: ID generation, embedding dim-validation, and
add/upsert input-length validation.

EXTRACTED FROM (diffed byte-for-byte before extraction):
    - opencrab/stores/chroma_store.py     (~109-170, add_texts/upsert_texts)
    - opencrab/stores/pg_vector_store.py  (~247-318, _embed/add_texts/upsert_texts)
    - opencrab/stores/sqlite_vec_store.py (~329-436, _embed/add_texts/upsert_texts)

DESIGN CHOICE (plain functions, not a base class): the three stores keep
    unrelated attribute names for the same concepts (``self._ef`` vs
    ``self._embedding_function``, engine vs sqlite3 connection, etc.) and
    have divergent write paths beyond this shared preamble (vec0
    DELETE-then-INSERT vs pgvector native UPSERT vs Chroma's collection API).
    Forcing them under one base class would need either attribute-name
    coupling via a mixin protocol or constructor gymnastics for no benefit —
    these are pure input-transformation/validation steps with no state, so
    plain functions keep each store's adopter change to "call this instead of
    inlining it," which is the mechanical-dedup goal.

INTER-COPY FINDINGS:
    - ID generation is IDENTICAL in all three: add-path uses
      ``sha256(f"{text}{time.time_ns()}")[:16]`` (time-salted, so repeated
      identical text never collides); upsert-path uses ``sha256(text)[:16]``
      (content-deterministic, so re-upserting the same text reuses the same
      id). No parameterisation needed.
    - ``_embed``'s dim-validation error message is IDENTICAL between
      pg_vector_store.py and sqlite_vec_store.py:
      ``f"Embedding dim {len(vec)} != table dim {dim}."`` — unified here
      as-is, no per-store message parameter needed.
      chroma_store.py has NO app-side ``_embed``/dim-check at all (Chroma
      embeds internally via its own EmbeddingFunction, so there's nothing to
      validate at this layer) — this helper is simply unused by that store.
    - The texts/metadatas/ids length-mismatch preamble
      (``"texts, metadatas, and ids must have the same length."``) is
      IDENTICAL between pg_vector_store.py and sqlite_vec_store.py.
      chroma_store.py does NOT perform this check (it passes lists straight
      to the chromadb collection API, which does its own validation) — a
      structural difference, not a bug to unify; chroma's adopter is not
      required to call ``validate_lengths``.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any


def generate_add_ids(texts: list[str]) -> list[str]:
    """Time-salted content hash IDs for the ``add`` path (never collides on
    repeated identical text within the same process)."""
    return [
        hashlib.sha256(f"{t}{time.time_ns()}".encode()).hexdigest()[:16]
        for t in texts
    ]


def generate_upsert_ids(texts: list[str]) -> list[str]:
    """Content-deterministic hash IDs for the ``upsert`` path (re-upserting
    the same text reuses the same id)."""
    return [hashlib.sha256(t.encode()).hexdigest()[:16] for t in texts]


def default_metadatas(
    texts: list[str], metadatas: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """``[{} for _ in texts]`` when metadatas is omitted, else passthrough."""
    if metadatas is None:
        return [{} for _ in texts]
    return metadatas


def validate_lengths(
    texts: list[str], metadatas: list[dict[str, Any]], ids: list[str]
) -> None:
    """Raise ValueError when texts/metadatas/ids lengths disagree.

    NOTE: chroma_store.py does not perform this check — see module docstring.
    """
    if len(ids) != len(texts) or len(metadatas) != len(texts):
        raise ValueError("texts, metadatas, and ids must have the same length.")


def embed_and_validate(
    embedding_function: Callable[[list[str]], list[list[float]]],
    dim: int,
    texts: list[str],
) -> list[list[float]]:
    """Embed ``texts`` and raise RuntimeError if any vector's length != dim.

    NOTE: chroma_store.py has no equivalent — it has no app-side embedding
    function/dim to validate against (see module docstring).
    """
    vectors = embedding_function(list(texts))
    for vec in vectors:
        if len(vec) != dim:
            raise RuntimeError(f"Embedding dim {len(vec)} != table dim {dim}.")
    return vectors
