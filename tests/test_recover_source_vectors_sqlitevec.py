from __future__ import annotations

from scripts import recover_source_vectors as recovery
from tests._vec_helpers import MockEF


def _store(path: str, *, ann: str = ""):
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    return SqliteVecStore(
        db_path=path,
        embedding_function=MockEF(32),
        dim=32,
        collection_name="recovery_vectors",
        ann=ann,
    )


def test_verify_recovery_row_accepts_normal_real_sqlite_vec_row(tmp_path):
    vec = _store(str(tmp_path / "vectors.db"), ann="binary")
    try:
        vec.upsert_texts(texts=["source body"], ids=["id-a"], metadatas=[{"pack_id": "pack-a"}])
        assert vec.verify_recovery_row("id-a") is True
    finally:
        vec.close()


def test_verify_recovery_row_rejects_real_bit_mismatch(tmp_path):
    from opencrab.stores.sqlite_vec_store import _sign_bits

    vec = _store(str(tmp_path / "vectors.db"), ann="binary")
    try:
        values = vec._embed(["source body"])[0]
        wrong_bits = _sign_bits([-value for value in values])
        vec._conn.execute(
            vec._insert_sql(),
            ("id-a", "pack-a", vec._serialize(values), wrong_bits, "source body", '{"pack_id":"pack-a"}'),
        )
        vec._conn.commit()
        assert vec.verify_recovery_row("id-a") is False
    finally:
        vec.close()


def test_vector_identity_resolves_relative_paths_by_actual_cwd(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    vec1 = _store("vectors.db")
    monkeypatch.chdir(second)
    vec2 = _store("vectors.db")
    try:
        assert recovery.vector_identity(vec1)["path_digest"] != recovery.vector_identity(vec2)["path_digest"]
    finally:
        vec1.close()
        vec2.close()


def test_vector_identity_resolves_symlink_alias(tmp_path):
    target = tmp_path / "vectors.db"
    alias = tmp_path / "alias.db"
    vec1 = _store(str(target))
    vec1.close()
    alias.symlink_to(target)
    vec2 = _store(str(alias))
    vec3 = _store(str(target))
    try:
        assert recovery.vector_identity(vec2)["path_digest"] == recovery.vector_identity(vec3)["path_digest"]
    finally:
        vec2.close()
        vec3.close()
