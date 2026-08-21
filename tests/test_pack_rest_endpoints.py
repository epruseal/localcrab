"""REST surface for the pack registry (#149 design v6 §5.2/§5.3/§5.8).

`apps/api/main.py` had zero pack endpoints before this: the dashboard could
not show a caller which packs they own vs. can only read, nor let them
change a pack's visibility. `opencrab.pack.ownership` is the single
authority for that scoping and for write authorization (#143 invariant 7,
"존재 누출 금지" -- a private pack owned by someone else must look
identical to "does not exist"); this REST layer is only a thin projection
onto the five-field screen contract plus REST-side sorting and a
display-name fallback. It must add zero scope predicates or visibility/
ownership branches of its own -- design v6 §5.5's "권한 재발명 금지".

Handlers are called directly (see tests/test_api_read_scope.py for the same
pattern) with a hand-built `ApiContext`/`AuthContext`, so no live server or
bearer-token plumbing is needed here -- what is under test is the pack
scoping/response-shaping, not FastAPI's routing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from apps.api import main as api
from opencrab.auth import Principal, create_user
from opencrab.pack.ownership import (
    begin_pack_creation,
    create_pack,
    ensure_default_pack,
    mark_pack_partial,
    set_visibility,
)

_PACK_KEYS = {"pack_id", "title", "visibility", "is_default", "is_owner"}


@pytest.fixture
def sql(tmp_path):
    from opencrab.stores.sql_store import SQLStore

    return SQLStore(f"sqlite:///{tmp_path / 'registry.db'}")


@pytest.fixture
def ctx(sql):
    from opencrab.config import get_settings

    return api.ApiContext(
        settings=get_settings(),
        graph=MagicMock(),
        vector=MagicMock(available=False),
        docs=MagicMock(available=False),
        sql=sql,
        hybrid=MagicMock(),
        impact=MagicMock(),
    )


@pytest.fixture
def world(sql):
    alice = Principal(user_id=create_user(sql, "Alice"), is_local=False, disabled=False)
    bob = Principal(user_id=create_user(sql, "Bob"), is_local=False, disabled=False)
    return {"alice": alice, "bob": bob}


def _auth(principal: Principal) -> Any:
    return api.AuthContext(user_id=principal.user_id, tier="free", principal=principal)


class TestVisibilityExistenceLeak:
    def test_missing_and_others_private_are_identical_and_fixed_404(self, ctx, world):
        """Design v6 §5.8 test 1: relative equality alone would still pass if
        both cases were mapped to 403 -- the absolute (404, fixed detail)
        assertion is what closes that gap."""
        alice, bob = world["alice"], world["bob"]
        bob_private = create_pack(ctx.sql, bob.user_id, "bob-private-vis")

        with pytest.raises(HTTPException) as exc_missing:
            api.set_pack_visibility(
                "no-such-pack-at-all",
                api.VisibilityRequest(visibility="public-read"),
                auth=_auth(alice),
                ctx=ctx,
            )
        with pytest.raises(HTTPException) as exc_private:
            api.set_pack_visibility(
                bob_private,
                api.VisibilityRequest(visibility="public-read"),
                auth=_auth(alice),
                ctx=ctx,
            )

        assert (exc_missing.value.status_code, exc_missing.value.detail) == (
            exc_private.value.status_code,
            exc_private.value.detail,
        )
        assert exc_missing.value.status_code == 404
        assert exc_missing.value.detail == "pack not found; use pack_create first"


class TestListScope:
    def test_list_scope_covers_own_public_and_status_filtering(self, ctx, world):
        alice, bob = world["alice"], world["bob"]

        mine_private = create_pack(ctx.sql, alice.user_id, "alice-priv2")
        bob_public_read = create_pack(ctx.sql, bob.user_id, "bob-pub-read")
        set_visibility(ctx.sql, bob, bob_public_read, "public-read")
        bob_public_fork = create_pack(ctx.sql, bob.user_id, "bob-pub-fork")
        set_visibility(ctx.sql, bob, bob_public_fork, "public-fork")
        bob_private = create_pack(ctx.sql, bob.user_id, "bob-priv2")

        creating_id = begin_pack_creation(ctx.sql, alice.user_id, "alice-creating")
        partial_id = begin_pack_creation(ctx.sql, alice.user_id, "alice-partial")
        assert mark_pack_partial(ctx.sql, partial_id, alice.user_id)

        got = {p["pack_id"] for p in api.list_packs(auth=_auth(alice), ctx=ctx)["packs"]}

        assert mine_private in got
        assert bob_public_read in got
        assert bob_public_fork in got
        assert bob_private not in got
        assert creating_id not in got
        assert partial_id not in got


class TestResponseShape:
    def test_exactly_five_keys_no_owner_id_leak_and_is_owner_both_ways(self, ctx, world):
        import json

        alice, bob = world["alice"], world["bob"]
        mine = create_pack(ctx.sql, alice.user_id, "alice-shape")
        bob_pub = create_pack(ctx.sql, bob.user_id, "bob-shape")
        set_visibility(ctx.sql, bob, bob_pub, "public-read")

        resp = api.list_packs(auth=_auth(alice), ctx=ctx)
        packs = resp["packs"]
        for p in packs:
            assert set(p.keys()) == _PACK_KEYS

        body_str = json.dumps(resp)
        assert "owner_id" not in body_str
        assert bob.user_id not in body_str

        mine_row = next(p for p in packs if p["pack_id"] == mine)
        bob_row = next(p for p in packs if p["pack_id"] == bob_pub)
        assert mine_row["is_owner"] is True
        assert bob_row["is_owner"] is False


class TestSorting:
    def test_response_is_ascending_by_pack_id_regardless_of_insert_order(self, ctx, world):
        alice = world["alice"]
        ids = ["zzz-pack", "mmm-pack", "aaa-pack"]
        returned = [create_pack(ctx.sql, alice.user_id, pid) for pid in ids]

        packs = api.list_packs(auth=_auth(alice), ctx=ctx)["packs"]
        mine = [p["pack_id"] for p in packs if p["pack_id"] in returned]
        assert mine == sorted(returned)


class TestDisplayNameFallback:
    def test_null_title_falls_back_to_pack_id(self, ctx, world):
        alice = world["alice"]
        pid = create_pack(ctx.sql, alice.user_id, "alice-no-title")  # title=None (default)

        packs = api.list_packs(auth=_auth(alice), ctx=ctx)["packs"]
        row = next(p for p in packs if p["pack_id"] == pid)
        assert row["title"] == pid


class TestForbiddenVsNotFound:
    def test_changing_someone_elses_public_pack_is_403_distinct_from_404(self, ctx, world):
        alice, bob = world["alice"], world["bob"]
        bob_pub = create_pack(ctx.sql, bob.user_id, "bob-pub-forbidden")
        set_visibility(ctx.sql, bob, bob_pub, "public-read")

        with pytest.raises(HTTPException) as exc_forbidden:
            api.set_pack_visibility(
                bob_pub, api.VisibilityRequest(visibility="private"), auth=_auth(alice), ctx=ctx
            )
        with pytest.raises(HTTPException) as exc_missing:
            api.set_pack_visibility(
                "nonexistent-xyz",
                api.VisibilityRequest(visibility="private"),
                auth=_auth(alice),
                ctx=ctx,
            )

        assert exc_forbidden.value.status_code == 403
        assert exc_missing.value.status_code == 404
        assert exc_forbidden.value.status_code != exc_missing.value.status_code


class TestValidationOrder:
    def test_unknown_visibility_on_missing_pack_is_422_not_404(self, ctx, world):
        alice = world["alice"]
        with pytest.raises(HTTPException) as exc:
            api.set_pack_visibility(
                "nonexistent-xyz-2",
                api.VisibilityRequest(visibility="not-a-real-visibility"),
                auth=_auth(alice),
                ctx=ctx,
            )
        assert exc.value.status_code == 422


class TestDefaultPackDisplay:
    def test_is_default_true_only_for_the_actual_default_pack(self, ctx, world):
        alice = world["alice"]
        default_id = ensure_default_pack(ctx.sql, alice.user_id)
        other_id = create_pack(ctx.sql, alice.user_id, "alice-not-default")

        by_id = {p["pack_id"]: p for p in api.list_packs(auth=_auth(alice), ctx=ctx)["packs"]}
        assert by_id[default_id]["is_default"] is True
        assert by_id[other_id]["is_default"] is False


class TestVisibilityChangeSuccessPath:
    def test_change_reflected_in_post_response_and_subsequent_list(self, ctx, world):
        alice = world["alice"]
        pid = create_pack(ctx.sql, alice.user_id, "alice-change-me")

        resp1 = api.set_pack_visibility(
            pid, api.VisibilityRequest(visibility="public-read"), auth=_auth(alice), ctx=ctx
        )
        assert set(resp1.keys()) == _PACK_KEYS
        assert resp1["visibility"] == "public-read"

        listed1 = api.list_packs(auth=_auth(alice), ctx=ctx)["packs"]
        row1 = next(p for p in listed1 if p["pack_id"] == pid)
        assert row1["visibility"] == "public-read"

        resp2 = api.set_pack_visibility(
            pid, api.VisibilityRequest(visibility="public-fork"), auth=_auth(alice), ctx=ctx
        )
        assert set(resp2.keys()) == _PACK_KEYS
        assert resp2["visibility"] == "public-fork"

        listed2 = api.list_packs(auth=_auth(alice), ctx=ctx)["packs"]
        row2 = next(p for p in listed2 if p["pack_id"] == pid)
        assert row2["visibility"] == "public-fork"
