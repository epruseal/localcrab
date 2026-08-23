"""Cross-backend equivalence for the pack-ownership predicates (issue #222).

WHY THIS FILE EXISTS. `MongoStore.list_nodes_scoped` and
`list_sources_scoped` decide which rows belong to a pack: the first is
`ontology_list_nodes`' read scope, the second is `pack_fork`'s copy range.
Their SQL twins (`_SqlDocStoreBase`, `_doc_owner_pred_scoped`) decide the
same thing. Before this file the only mongo coverage asserted the SHAPE of
the query document, so a backend could answer "this row is yours" where SQL
answered "it is not" with the whole suite green -- and it did, for every
array-valued `pack_id`/`source` and for an array-valued metadata container.

There is no mongo server here and none in CI, so equivalence is checked by
pulling the REAL query document out of the store and evaluating it against
MongoDB's documented matching semantics, while the SAME fixture rows go
through a REAL `LocalSQLDocStore`. The evaluator is the risky part -- it is
both the instrument and, if wrong, a way to agree with a bug -- so
`TestMatcherSelfCheck` pins each documented behaviour it must reproduce
before any equivalence assertion runs. Two of those rows exist because two
independent implementations of this evaluator got them wrong during review.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

PACK = "pack-a"
OTHER = "pack-z"

# Sentinel for "this key is absent", distinct from a present `None`.
MISSING = object()


# ---------------------------------------------------------------------------
# A minimal MongoDB query evaluator, per the documented semantics
# ---------------------------------------------------------------------------
# Quotations are from the MongoDB Manual (checked 2026-08):
#
#   $type / Behavior / Arrays:
#     "For documents where `field` is an array, `$type` returns documents in
#      which at least one array element matches a type passed to `$type`."
#     "Queries for `$type: 'array'` return documents where the field itself
#      is an array."
#   $in / Behavior:
#     "If `field` has an array, the `$in` operator selects the documents whose
#      `field` has an array that contains at least one element that matches a
#      value in the specified array."
#   Query for Null or Missing Fields:
#     "The `{ item : null }` query matches documents that contain the `item`
#      field with a `null` value or do not contain the `item` field."
#   $not:
#     "...selects the documents that do not match the <operator-expression>.
#      This includes documents that do not contain the field."
#   Query an Array of Embedded Documents:
#     a dotted path `a.b` reaches the `b` of every embedded document in `a`.
#
# Numbers and booleans sit in different BSON canonical types, so `false` does
# not equal `0`; int and double compare by value.


def _field_values(doc, path):
    """Candidate values a dotted path reaches, traversing embedded arrays.

    An array element that lacks the terminal key contributes MISSING rather
    than dropping out -- real MongoDB reads it as null, so `$in: [None]`
    matches it.
    """
    current = [doc]
    for seg in path.split("."):
        nxt = []
        for cur in current:
            if isinstance(cur, dict):
                if seg in cur:
                    nxt.append(cur[seg])
            elif isinstance(cur, list):
                for el in cur:
                    if isinstance(el, dict):
                        nxt.append(el.get(seg, MISSING))
        current = nxt
    return current or [MISSING]


def _eq(value, target):
    if target is None:
        return value is None or value is MISSING
    if value is MISSING:
        return False
    if isinstance(target, bool) or isinstance(value, bool):
        return isinstance(target, bool) and isinstance(value, bool) and target == value
    if isinstance(target, (int, float)) and isinstance(value, (int, float)):
        return float(target) == float(value)
    if type(target) is not type(value):
        return False
    return target == value


def _bson_type(v):
    if v is MISSING:
        return "missing"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "double"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    raise AssertionError(f"unmodelled BSON shape: {v!r}")


def _type_match(value, wanted):
    if wanted == "array":
        return isinstance(value, list)          # field-level, per the manual
    if _bson_type(value) == wanted:
        return True
    if isinstance(value, list):
        return any(_bson_type(e) == wanted for e in value)
    return False


def _op_match(value, op, arg):
    if op == "$in":
        if any(_eq(value, t) for t in arg):
            return True
        if isinstance(value, list):
            return any(_eq(e, t) for e in value for t in arg)
        return False
    if op == "$type":
        wanted = arg if isinstance(arg, list) else [arg]
        return any(_type_match(value, w) for w in wanted)
    if op == "$exists":
        return (value is not MISSING) == bool(arg)
    raise NotImplementedError(f"operator not modelled: {op}")


def _field_match(values, cond):
    """Operators on one field. Each is tested against the array independently;
    `$not` negates the whole field-level result, which is why it also matches
    a document that lacks the field."""
    for op, arg in cond.items():
        if op == "$not":
            if _field_match(values, arg):
                return False
        elif not any(_op_match(v, op, arg) for v in values):
            return False
    return True


def mongo_matches(doc, query):
    for key, cond in query.items():
        if key == "$or":
            if not any(mongo_matches(doc, sub) for sub in cond):
                return False
        elif key == "$and":
            if not all(mongo_matches(doc, sub) for sub in cond):
                return False
        elif key.startswith("$"):
            raise NotImplementedError(f"top-level operator not modelled: {key}")
        else:
            values = _field_values(doc, key)
            if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
                if not _field_match(values, cond):
                    return False
            # A bare value is equality, and equality traverses arrays exactly
            # as `$in` does -- routed through the same path so the two cannot
            # drift apart.
            elif not _field_match(values, {"$in": [cond]}):
                return False
    return True


class TestMatcherSelfCheck:
    """The evaluator must reproduce the documented behaviours the equivalence
    assertions depend on. If it cannot, those assertions prove nothing.

    The two rows marked below were each missed by an independent
    implementation during review of this change -- they are the reason this
    class exists rather than a comment claiming the semantics are obvious.
    """

    @pytest.mark.parametrize(
        "doc,query,expected",
        [
            # $in traverses array elements
            ({"a": ["x"]}, {"a": {"$in": ["x"]}}, True),
            ({"a": ["y"]}, {"a": {"$in": ["x"]}}, False),
            # $type traverses array elements, but "array" is field-level
            ({"a": ["x"]}, {"a": {"$type": "string"}}, True),
            ({"a": ["x"]}, {"a": {"$type": "array"}}, True),
            ({"a": "x"}, {"a": {"$type": "array"}}, False),
            # null matches both an explicit null and a missing field, but not []
            ({}, {"a": {"$in": [None]}}, True),
            ({"a": None}, {"a": {"$in": [None]}}, True),
            ({"a": []}, {"a": {"$in": [None]}}, False),
            # $not is field-level and also matches a missing field
            ({}, {"a": {"$not": {"$type": "array"}}}, True),
            ({"a": ["x"]}, {"a": {"$not": {"$type": "array"}}}, False),
            ({"a": "x"}, {"a": {"$not": {"$type": "array"}}}, True),
            # dotted paths traverse embedded documents and arrays of them
            ({"a": {"b": "x"}}, {"a.b": {"$in": ["x"]}}, True),
            ({"a": [{"b": "x"}]}, {"a.b": {"$in": ["x"]}}, True),   # missed once
            # bare equality traverses arrays the same way $in does
            ({"a": ["x"]}, {"a": "x"}, True),                        # missed once
            ({"a": [{"b": "x"}]}, {"a.b": "x"}, True),
            ({"a": "x"}, {"a": "x"}, True),
            ({"a": ["y"]}, {"a": "x"}, False),
            # an embedded element lacking the key reads as null
            ({"a": [{"b": "y"}, {"c": 1}]}, {"a.b": {"$in": [None]}}, True),
            ({"a": [{"b": "y"}]}, {"a.b": {"$in": [None]}}, False),
            # BSON type brackets: bool is not a number, int equals double
            ({"a": False}, {"a": {"$in": [0]}}, False),
            ({"a": 0}, {"a": {"$in": [0.0]}}, True),
        ],
    )
    def test_documented_semantics(self, doc, query, expected):
        assert mongo_matches(doc, query) is expected


# ---------------------------------------------------------------------------
# Fixture shapes
# ---------------------------------------------------------------------------

VALUE_SHAPES: list[tuple[str, object]] = [
    ("string-match", PACK),
    ("string-other", OTHER),
    ("array-1elem", [PACK]),
    ("array-2elem", [PACK, OTHER]),
    ("array-falsy", [0]),
    ("array-null", [None]),
    ("array-empty", []),
    ("object", {"x": PACK}),
    ("object-empty", {}),
    ("null", None),
    ("empty-string", ""),
    ("int-zero", 0),
    ("float-zero", 0.0),
    ("false", False),
    ("true", True),
    ("int-one", 1),
    ("missing", MISSING),
]

# The metadata/properties container itself, not the pack_id inside it.
CONTAINER_SHAPES: list[tuple[str, object]] = [
    ("dict", {"pack_id": PACK}),
    ("list-of-dict", [{"pack_id": PACK}]),
    ("list-of-dict-source", [{"source": PACK}]),
    ("list-scalar", [PACK]),
    ("str", PACK),
    ("null", None),
]


def _metadata(pack=MISSING, source=MISSING):
    md: dict[str, object] = {}
    if pack is not MISSING:
        md["pack_id"] = pack
    if source is not MISSING:
        md["source"] = source
    return md


def _source_cases():
    """(id, metadata) over three series: pack only, pack+source, source only,
    plus the container axis."""
    out = []
    for name, val in VALUE_SHAPES:
        out.append((f"pack__{name}", _metadata(pack=val)))
        out.append((f"packsource__{name}", _metadata(pack=val, source=PACK)))
        out.append((f"source__{name}", _metadata(source=val)))
    for name, container in CONTAINER_SHAPES:
        out.append((f"container__{name}", container))
    return out


def _node_cases():
    out = [
        (f"pack__{name}", {"pack_id": val} if val is not MISSING else {})
        for name, val in VALUE_SHAPES
    ]
    out += [(f"container__{name}", c) for name, c in CONTAINER_SHAPES]
    return out


SOURCE_CASES = _source_cases()
NODE_CASES = _node_cases()


@pytest.fixture(scope="module")
def sql_verdicts(tmp_path_factory):
    """Ownership verdicts from a REAL SQL doc store over the same fixtures."""
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    path = tmp_path_factory.mktemp("owner_equiv") / "doc_store.db"
    store = LocalSQLDocStore(str(path))
    try:
        for sid, md in SOURCE_CASES:
            store.upsert_source(sid, f"text of {sid}", md)
        owned_sources = {
            row["source_id"] for row in store.list_sources_scoped([PACK], limit=10_000)
        }
        for nid, props in NODE_CASES:
            store.upsert_node_doc("subject", "Entity", nid, props)
        owned_nodes = {
            row["node_id"] for row in store.list_nodes_scoped([PACK], limit=10_000)
        }
    finally:
        store.close()
    return owned_sources, owned_nodes


def _mongo_query(method, *args, collection):
    """The query document the real MongoStore builds, via a collection double."""
    from opencrab.stores.mongo_store import MongoStore

    store = MongoStore.__new__(MongoStore)
    store._available = True
    cursor = MagicMock()
    cursor.limit.return_value = []
    coll = MagicMock()
    coll.find.return_value = cursor
    store._db = {collection: coll}
    getattr(store, method)(*args)
    return coll.find.call_args[0][0], coll, cursor


@pytest.fixture(scope="module")
def sources_query():
    query, _coll, _cursor = _mongo_query(
        "list_sources_scoped", [PACK], 10_000, collection="sources"
    )
    return query


@pytest.fixture(scope="module")
def nodes_query():
    query, _coll, _cursor = _mongo_query(
        "list_nodes_scoped", [PACK], None, 10_000, collection="nodes"
    )
    return query


class TestOwnershipEquivalence:
    """The measured defect: mongo answered "owned" where SQL answered "not"
    for every array shape, on both predicates and on the container itself."""

    @pytest.mark.parametrize("case_id,metadata", SOURCE_CASES, ids=[c for c, _ in SOURCE_CASES])
    def test_sources_agree_with_sql(self, case_id, metadata, sql_verdicts, sources_query):
        owned_sources, _ = sql_verdicts
        sql_says = case_id in owned_sources
        mongo_says = mongo_matches({"metadata": metadata}, sources_query)
        assert mongo_says is sql_says, (
            f"{case_id}: SQL={sql_says} mongo={mongo_says} -- "
            + ("mongo over-includes (pack-boundary leak)" if mongo_says else "mongo under-includes")
        )

    @pytest.mark.parametrize("case_id,properties", NODE_CASES, ids=[c for c, _ in NODE_CASES])
    def test_nodes_agree_with_sql(self, case_id, properties, sql_verdicts, nodes_query):
        _, owned_nodes = sql_verdicts
        sql_says = case_id in owned_nodes
        mongo_says = mongo_matches({"properties": properties}, nodes_query)
        assert mongo_says is sql_says, (
            f"{case_id}: SQL={sql_says} mongo={mongo_says} -- "
            + ("mongo over-includes (read-scope leak)" if mongo_says else "mongo under-includes")
        )

    def test_no_shape_is_over_included(self, sql_verdicts, sources_query, nodes_query):
        """The property that matters on its own: whatever else differs, mongo
        must never claim a row SQL does not. Over-inclusion is what moves data
        across a pack boundary; under-inclusion is fail-closed."""
        owned_sources, owned_nodes = sql_verdicts
        over = [
            case_id
            for case_id, md in SOURCE_CASES
            if mongo_matches({"metadata": md}, sources_query) and case_id not in owned_sources
        ] + [
            case_id
            for case_id, props in NODE_CASES
            if mongo_matches({"properties": props}, nodes_query) and case_id not in owned_nodes
        ]
        assert over == []


class TestSpaceAxis:
    """`list_nodes_scoped`'s space filter, which no equivalence case above
    exercises (they all query with space=None). `list_sources_scoped` has no
    space axis: `doc_sources` has no space column."""

    def test_space_filter_matches_sql(self, tmp_path):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        store = LocalSQLDocStore(str(tmp_path / "space.db"))
        try:
            store.upsert_node_doc("concept", "Entity", "in-space", {"pack_id": PACK})
            store.upsert_node_doc("subject", "Entity", "other-space", {"pack_id": PACK})
            sql_owned = {
                r["node_id"] for r in store.list_nodes_scoped([PACK], space="concept", limit=100)
            }
        finally:
            store.close()
        assert sql_owned == {"in-space"}

        query, _coll, _cursor = _mongo_query(
            "list_nodes_scoped", [PACK], "concept", 100, collection="nodes"
        )
        mongo_owned = {
            nid
            for nid, space in (("in-space", "concept"), ("other-space", "subject"))
            if mongo_matches({"properties": {"pack_id": PACK}, "space": space}, query)
        }
        assert mongo_owned == sql_owned


class TestQueryShape:
    """Clauses that carry meaning no fixture shape can distinguish, pinned
    here so a reverse mutation still has something to kill."""

    def test_string_type_clause_is_present_on_every_membership_leg(
        self, sources_query, nodes_query
    ):
        """`$type: "string"` mirrors the SQL canon's `json_type='text'`. With
        `$in` bound to a list of `str` it changes no scalar verdict, so no
        equivalence case dies without it -- but dropping it would let a
        contract-violating non-string entry in `pack_ids` match where SQL
        would not. All three legs must carry it."""
        assert sources_query["$or"][0]["metadata.pack_id"]["$type"] == "string"
        fallback = sources_query["$or"][1]["$and"][1]["metadata.source"]
        assert fallback["$type"] == "string"
        assert nodes_query["properties.pack_id"]["$type"] == "string"

    def test_array_exclusion_is_present_on_every_leg_and_container(
        self, sources_query, nodes_query
    ):
        not_array = {"$type": "array"}
        assert sources_query["metadata"]["$not"] == not_array
        assert sources_query["$or"][0]["metadata.pack_id"]["$not"] == not_array
        assert sources_query["$or"][1]["$and"][0]["metadata.pack_id"]["$not"] == not_array
        assert sources_query["$or"][1]["$and"][1]["metadata.source"]["$not"] == not_array
        assert nodes_query["properties"]["$not"] == not_array
        assert nodes_query["properties.pack_id"]["$not"] == not_array

    def test_fallback_stays_conditional_on_absent_pack_id(self, sources_query):
        """`source` is consulted only when `pack_id` is absent. An
        unconditional OR would pull a mixed-tag row (`pack_id="B",
        source="A"`) into A's scope."""
        fallback = sources_query["$or"][1]
        assert set(fallback) == {"$and"}
        assert fallback["$and"][0]["metadata.pack_id"]["$in"] == [None, "", 0, 0.0, False]

    @pytest.mark.parametrize(
        "method,args,collection",
        [
            ("list_sources_scoped", ([PACK], 37), "sources"),
            ("list_nodes_scoped", ([PACK], None, 37), "nodes"),
        ],
    )
    def test_caller_limit_reaches_the_cursor(self, method, args, collection):
        """`pack_fork` reads CAP+1 rows and treats "exactly CAP+1 back" as
        truncation, so the caller's limit must arrive unchanged. 37 rather
        than the 100 default: a mutation replacing the argument with the
        default constant would survive a default-valued test."""
        _query, _coll, cursor = _mongo_query(*(method, *args), collection=collection)
        cursor.limit.assert_called_once_with(37)

    def test_helpers_return_fresh_dicts(self):
        """The array-exclusion fragment is aliased several times inside one
        query; a shared module-level dict would let a caller mutating one
        returned query corrupt every later one."""
        from opencrab.stores import mongo_store

        first, second = mongo_store._array(), mongo_store._array()
        assert first == second and first is not second
        first["$type"] = "mutated"
        assert mongo_store._array() == {"$type": "array"}


class TestKnownContractGap:
    """Non-string `pack_id` is OUTSIDE the mongo predicate's contract.

    SQL is not self-consistent here: `opencrab/pack/load.py`'s `_json_str_eq`
    (what `delete_pack` uses) is string-strict, while
    `_SqlDocStoreBase.list_nodes_scoped`'s `json_truthy_text` stringifies
    numbers, booleans and even composite values, so a scope naming that text
    matches. Mongo follows the string-strict one -- the policy `_json_str_eq`
    documents and `scripts/migrate_pack_ownership.py`'s `_classify_pack_id`
    enforces. These assertions pin the resulting difference so it is inherited
    knowingly rather than rediscovered, AND pin its direction: every one of
    them must be under-inclusion. If any flips to over-inclusion it is a pack
    boundary leak and this test must fail.

    THIS IS THE INTENDED STATE, NOT A LATENT BUG (issue #222 shipped it
    deliberately; issue #226 owns the resolution). Deciding the canon for
    non-string ``pack_id`` -- and enforcing it at write time, without which
    the shape simply comes back -- is #226's scope, and unifying the SQL side
    would SHRINK what ``ontology_list_nodes`` returns on the primary backend,
    which needs its own regression argument. Until then these assertions are
    the baseline: whatever #226 changes shows up here as a diff instead of a
    silent behaviour shift.
    """

    NON_STRING_SHAPES = [
        ("int", 1),
        ("float", 1.5),
        ("bool", True),
        ("array-1elem", [PACK]),
        ("array-2elem", [PACK, OTHER]),
        ("array-falsy", [0]),
        ("array-null", [None]),
        ("array-empty", []),
        ("object", {"x": PACK}),
        ("object-empty", {}),
    ]

    @staticmethod
    def _scope_text(value):
        """The text `json_truthy_text` yields, which a scope list would have
        to name for SQL to match."""
        if isinstance(value, (list, dict)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    @pytest.mark.parametrize(
        "name,value", NON_STRING_SHAPES, ids=[n for n, _ in NON_STRING_SHAPES]
    )
    def test_sql_matches_and_mongo_does_not(self, name, value, tmp_path):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        scope = self._scope_text(value)
        store = LocalSQLDocStore(str(tmp_path / f"gap_{name}.db"))
        try:
            store.upsert_node_doc("subject", "Entity", "row", {"pack_id": value})
            sql_says = bool(store.list_nodes_scoped([scope], limit=100))
        finally:
            store.close()

        query, _coll, _cursor = _mongo_query(
            "list_nodes_scoped", [scope], None, 100, collection="nodes"
        )
        mongo_says = mongo_matches({"properties": {"pack_id": value}}, query)

        assert sql_says is True, f"{name}: SQL no longer matches scope {scope!r}"
        assert mongo_says is False, (
            f"{name}: mongo now matches scope {scope!r} -- if this became "
            "over-inclusion it is a pack-boundary leak, not a contract gap"
        )
