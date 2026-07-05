"""
Contract tests for opencrab.stores._vector_base (ID generation, embedding
dim-validation, add/upsert length validation).
"""

from __future__ import annotations

import pytest

from opencrab.stores._vector_base import (
    default_metadatas,
    embed_and_validate,
    generate_add_ids,
    generate_upsert_ids,
    validate_lengths,
)

# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestIdGeneration:
    def test_upsert_ids_are_content_deterministic(self):
        texts = ["hello", "world"]
        ids1 = generate_upsert_ids(texts)
        ids2 = generate_upsert_ids(texts)
        assert ids1 == ids2
        assert len(ids1[0]) == 16

    def test_upsert_ids_differ_for_different_content(self):
        ids = generate_upsert_ids(["hello", "world"])
        assert ids[0] != ids[1]

    def test_add_ids_are_unique_even_for_identical_text(self):
        ids = generate_add_ids(["same text", "same text"])
        assert ids[0] != ids[1]  # time-salted -> no collision
        assert all(len(i) == 16 for i in ids)

    def test_default_metadatas_fills_empty_dicts(self):
        result = default_metadatas(["a", "b"], None)
        assert result == [{}, {}]

    def test_default_metadatas_passthrough(self):
        metas = [{"k": "v"}]
        assert default_metadatas(["a"], metas) is metas


class TestEmbedAndValidateNormal:
    def test_returns_embedding_function_output(self):
        def ef(texts):
            return [[0.0, 0.0, 0.0] for _ in texts]

        result = embed_and_validate(ef, 3, ["a", "b"])
        assert result == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]


class TestValidateLengthsNormal:
    def test_matching_lengths_no_raise(self):
        validate_lengths(["a", "b"], [{}, {}], ["id1", "id2"])  # must not raise


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestEmbedAndValidateError:
    def test_dim_mismatch_raises_informative_message(self):
        def ef(texts):
            return [[0.0, 0.0] for _ in texts]  # dim 2, expect 3

        with pytest.raises(RuntimeError, match=r"Embedding dim 2 != table dim 3\."):
            embed_and_validate(ef, 3, ["a"])

    def test_partial_dim_mismatch_among_batch_raises(self):
        def ef(texts):
            return [[0.0, 0.0, 0.0], [0.0, 0.0]]

        with pytest.raises(RuntimeError, match="Embedding dim 2 != table dim 3."):
            embed_and_validate(ef, 3, ["a", "b"])


class TestValidateLengthsError:
    def test_ids_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="same length"):
            validate_lengths(["a", "b"], [{}, {}], ["id1"])

    def test_metadatas_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="same length"):
            validate_lengths(["a", "b"], [{}], ["id1", "id2"])


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_texts_generate_empty_ids(self):
        assert generate_add_ids([]) == []
        assert generate_upsert_ids([]) == []

    def test_empty_texts_validate_lengths_no_raise(self):
        validate_lengths([], [], [])  # must not raise

    def test_empty_texts_embed_and_validate_returns_empty(self):
        def ef(texts):
            return []

        assert embed_and_validate(ef, 3, []) == []

    def test_default_metadatas_empty_texts(self):
        assert default_metadatas([], None) == []
