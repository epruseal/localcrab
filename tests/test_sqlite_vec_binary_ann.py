"""Binary 2-stage quantization tests for SqliteVecStore (VECTOR_ANN=binary).

docs/pgvector-migration-plan.md §3.7. Covers:
  - _sign_bits packing correctness (vs hand-computed bytes and SQL
    vec_quantize_binary)
  - 2-stage result == exact result when C >= corpus size (synthetic data)
  - ann off (default) leaves the schema and behaviour 100% unchanged
  - pack-scoped queries never take the ANN path (coarse-stage pack leak = 0
    by construction — whitebox + blackbox)
  - migration script: idempotency, backfill correctness, data preservation
Uses tmp_path + MockEF only (no network, no real data).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from _vec_helpers import MockEF, build_vector_store

DIM = 32  # divisible by 8 (bit packing requirement)

# 60 synthetic docs across three packs (enough for meaningful top-10 ordering)
CORPUS = [
    (f"n{i:02d}", f"synthetic document number {i} about topic {i % 7}",
     {"pack_id": ["A", "B", "C"][i % 3], "space": "s1"})
    for i in range(60)
]


def _load(store):
    store.upsert_texts(
        texts=[t for _, t, _ in CORPUS],
        metadatas=[m for _, _, m in CORPUS],
        ids=[i for i, _, _ in CORPUS],
    )


def _table_cols(db_path: str, table: str) -> list[str]:
    import sqlite_vec

    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.close()
    return cols


# ---------------------------------------------------------------------------
# _sign_bits packing
# ---------------------------------------------------------------------------


class TestSignBits:
    def test_known_vector(self):
        from opencrab.stores.sqlite_vec_store import _sign_bits

        # bit i = 1 iff component i > 0 (0.0 → 0), packed LSB-first per byte —
        # the exact vec_quantize_binary convention (see _sign_bits docstring)
        vec = [1.0, -1.0, 0.0, -0.5, 2.5, -0.1, 3.0, -7.0]
        # bits (i=0..7): 1,0,0,0,1,0,1,0 → LSB-first byte 0b01010001
        assert _sign_bits(vec) == bytes([0b01010001])

    def test_all_positive_negative(self):
        from opencrab.stores.sqlite_vec_store import _sign_bits

        assert _sign_bits([1.0] * 16) == b"\xff\xff"
        assert _sign_bits([-1.0] * 16) == b"\x00\x00"

    def test_matches_sql_vec_quantize_binary(self):
        """App-side packing must be byte-identical to sqlite-vec's own
        vec_quantize_binary (migration backfill uses the SQL side)."""
        import sqlite_vec

        from opencrab.stores.sqlite_vec_store import _sign_bits

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        ef = MockEF(DIM)
        for text in ("alpha", "beta", "gamma", "한국어 텍스트"):
            vec = ef([text])[0]
            sql_bits = conn.execute(
                "SELECT vec_quantize_binary(?)",
                (sqlite_vec.serialize_float32(vec),),
            ).fetchone()[0]
            assert _sign_bits(vec) == bytes(sql_bits)


# ---------------------------------------------------------------------------
# 2-stage query semantics
# ---------------------------------------------------------------------------


class TestBinaryTwoStage:
    def test_exact_when_c_covers_corpus(self, tmp_path):
        """With C >= corpus size the coarse stage returns everything, so the
        2-stage result must equal the exact brute-force result exactly."""
        exact = build_vector_store("sqlite-vec", tmp_path / "exact", dim=DIM)
        _load(exact)
        ann = build_vector_store(
            "sqlite-vec", tmp_path / "ann", dim=DIM,
            ann="binary", ann_coarse_k=len(CORPUS),
        )
        _load(ann)

        for q in ("topic 3 document", "synthetic number", "완전히 다른 질의"):
            ref = exact.query(q, n_results=10)
            got = ann.query(q, n_results=10)
            assert [h["id"] for h in got] == [h["id"] for h in ref]
            for r, g in zip(ref, got):
                assert g["distance"] == pytest.approx(r["distance"], abs=1e-5)
                assert g["document"] == r["document"]
                assert g["metadata"] == r["metadata"]
        exact.close()
        ann.close()

    def test_small_c_returns_valid_hits(self, tmp_path):
        """A small C still returns well-formed top-n (recall may drop; format
        and ordering-by-distance must not)."""
        ann = build_vector_store(
            "sqlite-vec", tmp_path, dim=DIM, ann="binary", ann_coarse_k=5,
        )
        _load(ann)
        hits = ann.query("topic 3 document", n_results=5)
        assert 0 < len(hits) <= 5
        dists = [h["distance"] for h in hits]
        assert dists == sorted(dists)

    def test_bit_column_created_and_synced_on_write(self, tmp_path):
        ann = build_vector_store(
            "sqlite-vec", tmp_path, dim=DIM, ann="binary",
        )
        _load(ann)
        assert "embedding_bit" in _table_cols(str(tmp_path / "vectors.db"), "vtest")
        # upsert overwrite keeps bit in sync (query still finds the new text)
        ann.upsert_texts(
            texts=["totally new replacement text"],
            metadatas=[{"pack_id": "A"}],
            ids=["n00"],
        )
        hits = ann.query("totally new replacement text", n_results=1)
        assert hits and hits[0]["id"] == "n00"
        ann.close()

    def test_invalid_ann_mode_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="ann mode"):
            build_vector_store("sqlite-vec", tmp_path, dim=DIM, ann="ivf")


# ---------------------------------------------------------------------------
# ANN off → 기존 경로 100% 불변
# ---------------------------------------------------------------------------


class TestAnnOffUnchanged:
    def test_no_bit_column_without_ann(self, tmp_path):
        store = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
        _load(store)
        assert "embedding_bit" not in _table_cols(
            str(tmp_path / "vectors.db"), "vtest"
        )
        store.close()

    def test_binary_flag_on_unmigrated_table_falls_back(self, tmp_path):
        """ann='binary' against a pre-existing float-only table must warn and
        keep serving exact results (schema-detection gating)."""
        plain = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
        _load(plain)
        plain.close()

        ann = build_vector_store(
            "sqlite-vec", tmp_path, dim=DIM, ann="binary",
        )
        assert ann.available
        assert ann._has_bit_column is False
        # still float-only schema (IF NOT EXISTS must not clobber)
        assert "embedding_bit" not in _table_cols(
            str(tmp_path / "vectors.db"), "vtest"
        )
        # exact path still serves, and writes still use the 5-column INSERT
        ann.upsert_texts(
            texts=["extra doc"], metadatas=[{"pack_id": "A"}], ids=["zz"]
        )
        hits = ann.query("synthetic document", n_results=5)
        assert len(hits) == 5
        ann.close()


# ---------------------------------------------------------------------------
# pack filter: coarse stage leak = 0
# ---------------------------------------------------------------------------


class TestPackFilterWithAnn:
    def test_pack_scoped_stays_exact_and_leak_free(self, tmp_path):
        ann = build_vector_store(
            "sqlite-vec", tmp_path, dim=DIM, ann="binary", ann_coarse_k=8,
        )
        _load(ann)
        # blackbox: every hit belongs to the requested pack (leak = 0), and
        # results equal a plain exact store's (pack path bypasses ANN entirely)
        exact = build_vector_store("sqlite-vec", tmp_path / "ref", dim=DIM)
        _load(exact)
        for pack in ("A", "B", "C"):
            got = ann.query("topic document", n_results=10,
                            where={"pack_id": pack})
            ref = exact.query("topic document", n_results=10,
                              where={"pack_id": pack})
            assert got, f"no hits for pack {pack}"
            assert all(h["metadata"]["pack_id"] == pack for h in got)
            assert [h["id"] for h in got] == [h["id"] for h in ref]
        # $in across packs also stays exact / leak-free
        got = ann.query("topic document", n_results=10,
                        where={"pack_id": {"$in": ["A", "B"]}})
        assert got and all(h["metadata"]["pack_id"] in ("A", "B") for h in got)
        ann.close()
        exact.close()

    def test_pack_query_never_takes_ann_path(self, tmp_path, monkeypatch):
        """Whitebox regression guard: a pack-constrained query must not call
        _knn_bit_rerank (pack-scoped keeps the exact partition path)."""
        ann = build_vector_store(
            "sqlite-vec", tmp_path, dim=DIM, ann="binary",
        )
        _load(ann)
        called = []
        orig = ann._knn_bit_rerank
        monkeypatch.setattr(
            ann, "_knn_bit_rerank",
            lambda *a, **k: called.append(1) or orig(*a, **k),
        )
        ann.query("topic", n_results=5, where={"pack_id": "A"})
        assert called == []
        # global query DOES take it
        ann.query("topic", n_results=5)
        assert called == [1]
        ann.close()


# ---------------------------------------------------------------------------
# migration script
# ---------------------------------------------------------------------------


SCRIPT = str(
    Path(__file__).resolve().parents[1]
    / "scripts" / "migrate_add_binary_quantization.py"
)


def _run_migration(db: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SCRIPT, "--db-path", str(db),
         "--table", "vtest", "--dim", str(DIM), *extra],
        capture_output=True, text=True, timeout=120,
    )


class TestMigrationScript:
    def _make_plain_db(self, tmp_path) -> Path:
        store = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
        _load(store)
        store.close()
        return tmp_path / "vectors.db"

    def test_requires_backup_flag(self, tmp_path):
        db = self._make_plain_db(tmp_path)
        r = _run_migration(db)
        assert r.returncode == 2
        assert "--backup-to" in r.stdout

    def test_dry_run_touches_nothing(self, tmp_path):
        db = self._make_plain_db(tmp_path)
        r = _run_migration(db, "--dry-run")
        assert r.returncode == 0
        assert "embedding_bit" not in _table_cols(str(db), "vtest")

    def test_migrate_backfill_and_idempotency(self, tmp_path):
        db = self._make_plain_db(tmp_path)
        backup = tmp_path / "backup.db"
        r = _run_migration(db, "--backup-to", str(backup))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "RESULT: PASS" in r.stdout
        assert backup.exists()
        assert "embedding_bit" in _table_cols(str(db), "vtest")
        # backup was taken BEFORE migration → still float-only
        assert "embedding_bit" not in _table_cols(str(backup), "vtest")

        # data preserved: same rows, same documents/metadata, ANN store serves it
        ann = build_vector_store(
            "sqlite-vec", tmp_path, dim=DIM, ann="binary",
            ann_coarse_k=len(CORPUS),
        )
        assert ann._has_bit_column is True
        assert ann.count() == len(CORPUS)
        exact_ref = build_vector_store("sqlite-vec", tmp_path / "ref", dim=DIM)
        _load(exact_ref)
        got = ann.query("topic 3 document", n_results=10)
        ref = exact_ref.query("topic 3 document", n_results=10)
        assert [h["id"] for h in got] == [h["id"] for h in ref]
        ann.close()
        exact_ref.close()

        # second run is a no-op (idempotent) and needs no backup flag
        r2 = _run_migration(db)
        assert r2.returncode == 0
        assert "already migrated" in r2.stdout

    def test_refuses_existing_backup_target(self, tmp_path):
        db = self._make_plain_db(tmp_path)
        backup = tmp_path / "backup.db"
        backup.write_bytes(b"precious")
        r = _run_migration(db, "--backup-to", str(backup))
        assert r.returncode == 2
        assert backup.read_bytes() == b"precious"
