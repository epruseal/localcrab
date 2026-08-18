"""Pack-scoped raw vector export/import contract (issue #200).

The consumer is ``pack_fork`` (#201): export a pack's vectors whole, rewrite
the ids and the ownership tag, import them into a new pack without ever
calling the embedding function. These tests pin the properties that flow
makes load-bearing, per backend, because the backends do NOT agree on all of
them:

- sqlite-vec and pgvector round-trip an embedding EXACTLY. chroma does not --
  on a ``hnsw:space=cosine`` collection (the only kind ChromaStore creates) a
  component can shift by one float32 ULP. So the fidelity assertion is
  per-backend: byte/vector equality for two of them, a ULP tolerance plus
  identical KNN ordering for the third.
- chroma's ``add()`` on an existing id is a silent no-op, where the other two
  raise. ``ChromaStore.import_vectors`` closes that itself, so the
  "duplicate id raises" assertion holds everywhere.

See ``opencrab/stores/_vector_base.py`` for the contract text and
``docs/vector-backends.md`` for the backend fidelity table.
"""

from __future__ import annotations

import math

import pytest
from _vec_helpers import MockEF, build_vector_store

BACKENDS = ["chroma", "sqlite-vec", "sqlite-vec-binary", "pg"]

SRC_PACK = "srcpack"
DST_PACK = "dstpack"
OTHER_PACK = "otherpack"


class CountingEF(MockEF):
    """MockEF that records how many times it was asked to embed."""

    def __init__(self, dim: int = 32) -> None:
        super().__init__(dim)
        self.calls = 0
        self.texts = 0

    def __call__(self, input):  # noqa: A002 - EF interface
        self.calls += 1
        self.texts += len(input)
        return super().__call__(input)


def _make(backend: str, tmp_path, *, ef=None, dim: int = 32):
    if backend.startswith("sqlite-vec"):
        # Skip rather than error when the extension is absent, the way the pg
        # branch of build_vector_store already does for a missing DSN. An
        # unavailable backend is an environment fact, not a failure of this
        # contract, and erroring would make every one of these cases show up
        # in a regression diff on machines without the extension.
        pytest.importorskip("sqlite_vec")
        return build_vector_store(
            "sqlite-vec",
            tmp_path,
            dim,
            ef=ef,
            **({"ann": "binary"} if backend.endswith("binary") else {}),
        )
    return build_vector_store(backend, tmp_path, dim, ef=ef)


def _drop_pg(backend: str, store) -> None:
    """Shared *_test DB must not accumulate tables (parity-suite convention)."""
    if backend != "pg":
        return
    try:
        from sqlalchemy import text

        with store._engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {store._table}"))
    except Exception:  # noqa: BLE001 - teardown is best effort
        pass


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    ef = CountingEF(32)
    s = _make(request.param, tmp_path, ef=ef)
    assert s.available
    s._test_ef = ef
    s._test_backend = request.param
    yield s
    _drop_pg(request.param, s)
    if hasattr(s, "close"):
        s.close()


CORPUS = [
    ("n1", "apple fruit red sweet", {"space": "s1", "node_id": "n1", "kind": "a"}),
    ("n2", "banana fruit yellow soft", {"space": "s1", "node_id": "owner-n2"}),
    ("n3", "car vehicle fast road", {"space": "s2", "char_start": 17}),
    ("n4", "train vehicle rail steel", {"space": "s1", "score": 0.25}),
    ("n5", "python snake reptile green", {"space": "s2"}),
]


def _seed(store, pack: str = SRC_PACK, corpus=None):
    corpus = CORPUS if corpus is None else corpus
    texts = [t for _, t, _ in corpus]
    ids = [i for i, _, _ in corpus]
    metas = [{**m, "pack_id": pack} for _, _, m in corpus]
    store.upsert_texts(texts=texts, metadatas=metas, ids=ids)
    return ids


def _remap(records, dst: str = DST_PACK):
    """What #201 does between export and import: new ids, new ownership tag,
    and the reference keys that name the source pack's id-space."""
    out = []
    for rec in records:
        new_id = f"{rec['id']}@{dst}"
        meta = {**rec["metadata"], "pack_id": dst}
        for key in ("node_id", "source_id", "document_id"):
            if key in meta:
                meta[key] = f"{meta[key]}@{dst}"
        copy = {**rec, "id": new_id, "metadata": meta}
        out.append(copy)
    return out


def _ulp_close(a: float, b: float) -> bool:
    """Within 2 float32 ULP at the components' own magnitude.

    chroma's cosine-space drift is one ULP per component, measured against the
    component's own size -- not the vector norm -- so the tolerance has to
    scale the same way or it fails on small components.
    """
    scale = max(abs(a), abs(b))
    if scale == 0.0:
        return a == b
    # float32 spacing at `scale`, without requiring numpy.
    return abs(a - b) <= 2.0 * (2.0 ** (math.floor(math.log2(scale)) - 23))


# ---------------------------------------------------------------------------
# Round-trip: completeness, fidelity, isolation
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_export_returns_the_whole_pack_and_nothing_else(self, store):
        _seed(store)
        _seed(store, OTHER_PACK, corpus=[("z1", "unrelated text", {})])

        records = store.export_pack_vectors(SRC_PACK)

        assert {r["id"] for r in records} == {i for i, _, _ in CORPUS}
        assert all(r["metadata"]["pack_id"] == SRC_PACK for r in records)

    def test_export_embedding_is_a_list_of_python_floats(self, store):
        """chroma hands back float64 ndarrays; the contract says list[float].

        ``isinstance`` would not catch it -- ``isinstance(np.float64(0.1),
        float)`` is True -- so the type is compared exactly.
        """
        _seed(store)

        for record in store.export_pack_vectors(SRC_PACK):
            assert type(record["embedding"]) is list
            assert all(type(x) is float for x in record["embedding"])

    def test_embedding_survives_the_round_trip(self, store):
        _seed(store)
        source = {r["id"]: r for r in store.export_pack_vectors(SRC_PACK)}

        store.import_vectors(_remap(source.values()), pack_id=DST_PACK)

        copied = {r["id"]: r for r in store.export_pack_vectors(DST_PACK)}
        assert len(copied) == len(source)
        for src_id, src in source.items():
            dst = copied[f"{src_id}@{DST_PACK}"]
            if store._test_backend == "chroma":
                # Not bit-identical by design -- see the module docstring.
                assert all(
                    _ulp_close(a, b)
                    for a, b in zip(src["embedding"], dst["embedding"])
                )
            else:
                assert dst["embedding"] == src["embedding"]

    def test_document_and_unrewritten_metadata_survive_the_round_trip(self, store):
        _seed(store)
        source = {r["id"]: r for r in store.export_pack_vectors(SRC_PACK)}

        store.import_vectors(_remap(source.values()), pack_id=DST_PACK)

        copied = {r["id"]: r for r in store.export_pack_vectors(DST_PACK)}
        for src_id, src in source.items():
            dst = copied[f"{src_id}@{DST_PACK}"]
            assert dst["document"] == src["document"]
            untouched = {"pack_id", "node_id", "source_id", "document_id"}
            for key, value in src["metadata"].items():
                if key not in untouched:
                    assert dst["metadata"][key] == value

    def test_import_never_calls_the_embedding_function(self, store):
        _seed(store)
        records = _remap(store.export_pack_vectors(SRC_PACK))
        before = store._test_ef.calls

        store.import_vectors(records, pack_id=DST_PACK)

        assert store._test_ef.calls == before

    def test_the_copy_is_searchable_in_its_new_pack(self, store):
        _seed(store)
        store.import_vectors(
            _remap(store.export_pack_vectors(SRC_PACK)), pack_id=DST_PACK
        )

        src_hits = store.query("apple fruit red sweet", 3, {"pack_id": SRC_PACK})
        dst_hits = store.query("apple fruit red sweet", 3, {"pack_id": DST_PACK})

        assert [h["id"] for h in dst_hits] == [
            f"{h['id']}@{DST_PACK}" for h in src_hits
        ]

    def test_importing_does_not_disturb_the_source_pack(self, store):
        _seed(store)
        before = {r["id"]: r for r in store.export_pack_vectors(SRC_PACK)}

        store.import_vectors(_remap(before.values()), pack_id=DST_PACK)

        after = {r["id"]: r for r in store.export_pack_vectors(SRC_PACK)}
        assert set(after) == set(before)
        for node_id, record in before.items():
            assert after[node_id]["embedding"] == record["embedding"]
            assert after[node_id]["metadata"] == record["metadata"]
        assert not [
            h for h in store.query("apple fruit", 10, {"pack_id": SRC_PACK})
            if h["id"].endswith(f"@{DST_PACK}")
        ]

    def test_export_of_an_unknown_pack_is_empty(self, store):
        _seed(store)

        assert store.export_pack_vectors("no-such-pack") == []

    def test_importing_nothing_is_a_no_op(self, store):
        _seed(store)
        before = store.count()

        assert store.import_vectors([], pack_id=DST_PACK) == []
        assert store.count() == before

    def test_import_returns_the_landed_ids_in_order(self, store):
        _seed(store)
        records = _remap(store.export_pack_vectors(SRC_PACK))

        landed = store.import_vectors(records, pack_id=DST_PACK)

        assert landed == [r["id"] for r in records]


# ---------------------------------------------------------------------------
# Ownership: what the contract enforces, and what it deliberately does not
# ---------------------------------------------------------------------------


class TestOwnership:
    def test_a_record_still_tagged_with_the_source_pack_is_rejected(self, store):
        """The failure #201 is most likely to make: rewrite the id, forget the
        ownership tag. Nothing in the storage layer would catch it -- the ids
        do not collide -- and the copies would land back in the source pack.
        """
        _seed(store)
        records = store.export_pack_vectors(SRC_PACK)
        for record in records:
            record["id"] = f"{record['id']}@{DST_PACK}"  # id only, tag left alone
        before = store.count()

        with pytest.raises(ValueError, match="disagrees with the declared target"):
            store.import_vectors(records, pack_id=DST_PACK)

        assert store.count() == before

    def test_metadata_without_a_pack_tag_is_stamped(self, store):
        store.import_vectors(
            [{"id": "fresh", "embedding": [0.1] * 32, "document": "d",
              "metadata": {"space": "s1"}}],
            pack_id=DST_PACK,
        )

        exported = store.export_pack_vectors(DST_PACK)
        assert [r["metadata"]["pack_id"] for r in exported] == [DST_PACK]

    def test_a_reference_key_pointing_elsewhere_is_allowed(self, store):
        """``metadata["node_id"]`` may legitimately differ from the record id --
        a chunk vector names its OWNING node. An earlier revision enforced
        equality here and that rejected normal packs, so this pins the
        non-enforcement.
        """
        store.import_vectors(
            [{"id": "chunk-1", "embedding": [0.2] * 32, "document": "c",
              "metadata": {"node_id": "some-other-node"}}],
            pack_id=DST_PACK,
        )

        exported = store.export_pack_vectors(DST_PACK)
        assert exported[0]["metadata"]["node_id"] == "some-other-node"

    def test_the_retired_pack_alias_is_dropped(self, store):
        """Legacy rows can carry ``pack`` alongside ``pack_id`` (#159/#171).
        Carrying it into the new pack would plant the source pack's name there.
        """
        store.import_vectors(
            [{"id": "legacy", "embedding": [0.3] * 32, "document": "l",
              "metadata": {"pack": "an-older-name", "space": "s1"}}],
            pack_id=DST_PACK,
        )

        metadata = store.export_pack_vectors(DST_PACK)[0]["metadata"]
        assert "pack" not in metadata
        assert metadata["pack_id"] == DST_PACK
        assert metadata["space"] == "s1"

    def test_an_id_that_already_exists_is_refused(self, store):
        """Slot identity is node_id alone and global, so an existing id means
        another pack's row. chroma's ``add()`` would silently keep the old
        record; the store closes that itself.
        """
        _seed(store)
        taken = CORPUS[0][0]
        before = {r["id"]: r for r in store.export_pack_vectors(SRC_PACK)}

        with pytest.raises(Exception):
            store.import_vectors(
                [{"id": taken, "embedding": [0.4] * 32, "document": "intruder",
                  "metadata": {"space": "s9"}}],
                pack_id=DST_PACK,
            )

        after = {r["id"]: r for r in store.export_pack_vectors(SRC_PACK)}
        assert after[taken]["document"] == before[taken]["document"]
        assert after[taken]["metadata"] == before[taken]["metadata"]
        assert store.export_pack_vectors(DST_PACK) == []


# ---------------------------------------------------------------------------
# Input validation -- all of it before any store is touched
# ---------------------------------------------------------------------------


def _one(**over):
    base = {"id": "v1", "embedding": [0.1] * 32, "document": "d", "metadata": {}}
    base.update(over)
    return base


class TestValidation:
    @pytest.mark.parametrize(
        "records, expected",
        [
            ("not-a-sequence", "records must be a sequence"),
            ([["not", "a", "dict"]], "must be a dict"),
            ([{"embedding": [0.1] * 32}], "missing"),
            ([_one(id="")], "non-empty str"),
            ([_one(id=7)], "non-empty str"),
            ([_one(metadata="nope")], "metadata must be a dict"),
            ([_one(embedding=[])], "embedding is empty"),
            ([_one(embedding="0.1")], "must be a sequence of numbers"),
            ([_one(extra="surprise")], "unknown key"),
            ([_one(id="dup"), _one(id="dup")], "duplicate id"),
            ([_one(embedding=[0.1] * 8)], "!= table dim"),
            ([_one(id="a"), _one(id="b", embedding=[0.1] * 31)], "dimensionally uniform"),
            ([_one(embedding=[1e40] + [0.1] * 31)], "finite float32"),
            ([_one(document=7)], "document must be str or None"),
            ([_one(metadata={7: "x"})], "non-str key"),
        ],
    )
    def test_a_bad_batch_is_refused_without_touching_the_store(
        self, store, records, expected
    ):
        if expected == "!= table dim" and store._test_backend == "chroma":
            pytest.skip("chroma has no app-side dim; length uniformity covers it")
        _seed(store)
        before = {r["id"]: r for r in store.export_pack_vectors(SRC_PACK)}

        with pytest.raises(ValueError, match=expected):
            store.import_vectors(records, pack_id=DST_PACK)

        assert {
            r["id"]: r for r in store.export_pack_vectors(SRC_PACK)
        } == before
        assert store.export_pack_vectors(DST_PACK) == []

    @pytest.mark.parametrize("pack_id", ["", 1, None])
    def test_a_bad_target_pack_is_refused(self, store, pack_id):
        """The record metadata deliberately carries no ``pack_id``: with one,
        the per-record mismatch check would fire first and hide whether the
        argument itself is validated at all.
        """
        _seed(store)
        before = store.count()

        with pytest.raises(ValueError, match="pack_id must be a non-empty str"):
            store.import_vectors([_one()], pack_id=pack_id)

        assert store.count() == before

    def test_an_empty_batch_still_validates_the_target_pack(self, store):
        with pytest.raises(ValueError, match="pack_id must be a non-empty str"):
            store.import_vectors([], pack_id="")

    def test_the_callers_records_are_not_mutated(self, store):
        records = [{"id": "keep", "embedding": [0.1] * 32, "document": "d",
                    "metadata": {"space": "s1"}},
                   {"id": "bad", "embedding": [0.1] * 4, "metadata": {}}]

        with pytest.raises(ValueError):
            store.import_vectors(records, pack_id=DST_PACK)

        assert records[0]["metadata"] == {"space": "s1"}

    def test_validation_runs_before_the_store_is_reached(self, store):
        """A rejected batch must fail in validation, not on the way into the
        backend -- otherwise the error depends on the backend and a partially
        applied write becomes possible.
        """
        if store._test_backend == "chroma":
            records = [_one(id="a"), _one(id="b", embedding=[0.1] * 31)]
        else:
            records = [_one(embedding=[0.1] * 8)]
        store._available = False
        store._available = True  # keep availability, break the storage instead
        broken = "no_such_table_for_import"
        original = getattr(store, "_table", None)
        if original is not None:
            store._table = broken
        try:
            with pytest.raises(ValueError):
                store.import_vectors(records, pack_id=DST_PACK)
        finally:
            if original is not None:
                store._table = original


class TestAvailability:
    def test_both_methods_refuse_an_unavailable_store(self, store):
        store._available = False
        try:
            with pytest.raises(RuntimeError):
                store.export_pack_vectors(SRC_PACK)
            with pytest.raises(RuntimeError):
                store.import_vectors([_one()], pack_id=DST_PACK)
        finally:
            store._available = True


# ---------------------------------------------------------------------------
# The export predicate must not drift from pack/load.py's
# ---------------------------------------------------------------------------


class TestPredicateEquivalence:
    def test_export_selects_exactly_what_live_vec_ids_selects(self, store):
        """``pack/load.py`` reaches into the raw handles to enumerate a pack's
        vectors, and ``delete_pack``/``live_pack_state`` act on that set. Two
        implementations of the same predicate drift; this pins them together.
        """
        from opencrab.pack.load import _live_vec_ids

        _seed(store)
        _seed(store, OTHER_PACK, corpus=[("z1", "unrelated", {})])

        exported = {r["id"] for r in store.export_pack_vectors(SRC_PACK)}

        assert exported == _live_vec_ids(store, SRC_PACK)


# ---------------------------------------------------------------------------
# sqlite-vec: the binary shape's derived column
# ---------------------------------------------------------------------------


class TestSqliteVecBinaryShape:
    def test_the_bit_column_is_rederived_identically(self, tmp_path):
        """``embedding_bit`` is written on every insert but only read by the
        migration path -- the ANN cache recomputes sign bits from the floats --
        so a wrong value here would not fail any search-level assertion.
        """
        pytest.importorskip("sqlite_vec")
        store = build_vector_store("sqlite-vec", tmp_path, 32, ann="binary")
        assert store._has_bit_column
        _seed(store)

        store.import_vectors(
            _remap(store.export_pack_vectors(SRC_PACK)), pack_id=DST_PACK
        )

        rows = store._conn.execute(
            f"SELECT node_id, pack_id, embedding_bit FROM {store._table}"
        ).fetchall()
        bits = {(r["pack_id"], r["node_id"]): bytes(r["embedding_bit"]) for r in rows}
        for node_id, _text, _meta in CORPUS:
            assert bits[(DST_PACK, f"{node_id}@{DST_PACK}")] == bits[
                (SRC_PACK, node_id)
            ]
        store.close()


# ---------------------------------------------------------------------------
# pgvector: text round-trip fidelity
# ---------------------------------------------------------------------------


class TestPgVectorLiteral:
    def test_negative_zero_keeps_its_sign(self, tmp_path):
        """pgvector renders -0.0 as ``-0``; ``json.loads`` would read that as
        the integer 0 and drop the sign, and pgvector's own ``=`` cannot tell
        the difference either, so nothing else would catch it.
        """
        store = build_vector_store("pg", tmp_path, 32)
        try:
            embedding = [-0.0, 0.0] + [0.5] * 30
            store.import_vectors(
                [{"id": "signed", "embedding": embedding, "document": "d",
                  "metadata": {}}],
                pack_id=DST_PACK,
            )

            got = store.export_pack_vectors(DST_PACK)[0]["embedding"]

            assert math.copysign(1.0, got[0]) == -1.0
            assert math.copysign(1.0, got[1]) == 1.0
        finally:
            _drop_pg("pg", store)
            store.close()

    def test_nested_metadata_round_trips(self, tmp_path):
        """pgvector stores metadata as jsonb with no sanitising, so a pack can
        legitimately hold non-scalar values -- ``/api/ingest`` forwards the
        caller's dict verbatim. Narrowing the contract to chroma's scalar set
        would make such a pack impossible to fork, so the round-trip is pinned
        here rather than left to a fixture that happens to use scalars.
        """
        store = build_vector_store("pg", tmp_path, 32)
        try:
            nested = {"tags": ["a", "b"], "extent": {"start": 1, "end": 9}}
            store.import_vectors(
                [{"id": "nested", "embedding": [0.5] * 32, "document": "d",
                  "metadata": dict(nested)}],
                pack_id=SRC_PACK,
            )

            exported = store.export_pack_vectors(SRC_PACK)[0]
            assert exported["metadata"]["tags"] == ["a", "b"]
            assert exported["metadata"]["extent"] == {"start": 1, "end": 9}

            store.import_vectors(_remap(exported for _ in [0]), pack_id=DST_PACK)
            copied = store.export_pack_vectors(DST_PACK)[0]
            assert copied["metadata"]["tags"] == ["a", "b"]
        finally:
            _drop_pg("pg", store)
            store.close()


# ---------------------------------------------------------------------------
# chroma only: batching, and the races the store closes itself
# ---------------------------------------------------------------------------


@pytest.fixture
def chroma(tmp_path):
    ef = CountingEF(32)
    s = build_vector_store("chroma", tmp_path, 32, ef=ef)
    assert s.available
    s._test_ef = ef
    yield s


def _bulk(n: int, pack: str = SRC_PACK):
    return [
        {"id": f"b{i}", "embedding": [(i % 97) / 100.0] * 32,
         "document": f"doc {i}", "metadata": {"pack_id": pack, "seq": i},
         "uris": None}
        for i in range(n)
    ]


class TestChromaBatching:
    def test_a_batch_larger_than_the_client_limit_still_lands_whole(self, chroma):
        """chroma rejects a single add() over ``get_max_batch_size()`` (5461
        here), so a fork of a large pack needs the store to chunk.
        """
        over = chroma._client.get_max_batch_size() + 1
        records = _bulk(over)

        landed = chroma.import_vectors(records, pack_id=SRC_PACK)

        assert len(landed) == over
        assert len(chroma.export_pack_vectors(SRC_PACK)) == over

    def test_a_duplicate_in_a_later_chunk_stops_the_whole_import(self, chroma):
        """The pre-check covers the entire batch, so a collision that would
        only be reached by the second chunk still prevents the first one.
        """
        chroma.import_vectors(
            [{"id": "taken", "embedding": [0.9] * 32, "document": "existing",
              "metadata": {"pack_id": OTHER_PACK}}],
            pack_id=OTHER_PACK,
        )
        over = chroma._client.get_max_batch_size() + 1
        records = _bulk(over)
        records[-1]["id"] = "taken"

        with pytest.raises(ValueError, match="already exist"):
            chroma.import_vectors(records, pack_id=SRC_PACK)

        assert chroma.export_pack_vectors(SRC_PACK) == []


class _Racing:
    """Collection wrapper that runs a callback the first time add() is called.

    The window this simulates -- between the pre-check and the add -- cannot be
    reached by seeding the id up front, because then the pre-check catches it
    and the post-write verification never runs at all.
    """

    def __init__(self, inner, on_first_add):
        self._inner = inner
        self._on_first_add = on_first_add
        self._fired = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def add(self, **kwargs):
        if not self._fired:
            self._fired = True
            self._on_first_add(self._inner)
        return self._inner.add(**kwargs)


class TestChromaRaceDetection:
    @pytest.mark.parametrize("field", ["document", "uris"])
    def test_an_id_taken_mid_write_is_reported(self, chroma, field):
        """add() on an existing id is a silent no-op in chroma, so the loser of
        this race would otherwise report success having written nothing.
        Counting rows cannot see it -- the id exists either way.

        Parametrized so each compared field is pinned on its own: with a
        single fixture that differs in several fields at once, dropping one
        comparison would still be caught by the others.
        """
        records = [
            {"id": "contested", "embedding": [0.1] * 32, "document": "ours",
             "metadata": {"pack_id": SRC_PACK, "seq": 1}, "uris": None},
            {"id": "quiet", "embedding": [0.2] * 32, "document": "second",
             "metadata": {"pack_id": SRC_PACK, "seq": 2}, "uris": None},
        ]
        intruder = {
            "document": {"documents": ["theirs"]},
            "uris": {"documents": ["ours"], "uris": ["file:///intruder.png"]},
        }[field]

        def steal(inner):
            inner.add(
                ids=["contested"],
                embeddings=[[0.1] * 32],
                metadatas=[{"pack_id": SRC_PACK, "seq": 1}],
                **intruder,
            )

        chroma._collection = _Racing(chroma._collection, steal)

        with pytest.raises(RuntimeError, match="different payload"):
            chroma.import_vectors(records, pack_id=SRC_PACK)

    def test_a_record_deleted_mid_write_is_reported(self, chroma):
        """The other half of the verification: an id that is simply gone
        afterwards. The payload comparison cannot see this one, because there
        is no payload to compare.
        """
        records = _bulk(3, SRC_PACK)

        def drop_one(inner):
            # Runs before the real add(), so schedule the delete for after it
            # by wrapping delete into the same call sequence.
            pass

        chroma._collection = _Racing(chroma._collection, drop_one)
        real_add = chroma._collection.add

        def add_then_delete(**kwargs):
            result = real_add(**kwargs)
            chroma._collection._inner.delete(ids=[records[0]["id"]])
            return result

        chroma._collection.add = add_then_delete

        with pytest.raises(RuntimeError, match="missing after the write"):
            chroma.import_vectors(records, pack_id=SRC_PACK)


class TestChromaResultShapes:
    def test_verification_does_not_assume_the_returned_order(self, chroma):
        """chroma makes no promise that get() returns ids in the requested
        order, so anything that zips the two by position is a latent bug.
        """
        records = _bulk(4, SRC_PACK)
        inner = chroma._collection

        class Reversing:
            def __getattr__(self, name):
                return getattr(inner, name)

            def get(self, **kwargs):
                got = inner.get(**kwargs)
                order = list(range(len(got["ids"])))[::-1]
                flipped = dict(got)
                for key in ("ids", "documents", "metadatas", "uris"):
                    value = got.get(key)
                    if isinstance(value, list) and len(value) == len(order):
                        flipped[key] = [value[i] for i in order]
                return flipped

        chroma._collection = Reversing()

        assert chroma.import_vectors(records, pack_id=SRC_PACK) == [
            r["id"] for r in records
        ]

    def test_export_normalises_the_shapes_chroma_can_return(self, chroma):
        """A record added without metadata comes back with ``metadatas``
        holding None, and embeddings arrive as float64 ndarrays. Neither is
        reachable through a normal pack export, so a double supplies them.
        """
        import numpy as np

        inner = chroma._collection

        class NullShaped:
            def __getattr__(self, name):
                return getattr(inner, name)

            def get(self, **kwargs):
                return {
                    "ids": ["odd"],
                    "embeddings": np.array([[0.5] * 32], dtype=np.float64),
                    "documents": [None],
                    "metadatas": [None],
                    "uris": None,
                }

        chroma._collection = NullShaped()

        record = chroma.export_pack_vectors(SRC_PACK)[0]

        assert record["metadata"] == {}
        assert record["document"] is None
        assert record["uris"] is None
        assert type(record["embedding"]) is list
        assert all(type(x) is float for x in record["embedding"])
