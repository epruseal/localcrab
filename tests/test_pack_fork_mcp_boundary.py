"""MCP `pack_fork` tool boundary: None vs explicit empty `new_pack_id`
(design v7 §17, issue #201).

T99 (design §17-5) exercises exactly one thing: whether the MCP tool
function's own `new_pack_id` cleaning line preserves the distinction
between "omitted" (`None`) and "supplied as an empty string" (`""`) before
handing off to `opencrab.pack.fork.fork_pack`. The orchestrator's own
rejection of `""` is `tests/test_pack_fork.py`'s
`TestExplicitEmptyNewPackIdRejection` (T98) -- not this file's job.
`fork_pack` is monkeypatched as a spy directly on `opencrab.pack.fork` (the
module object the tool function's own function-scope `from opencrab.pack
import fork as _fork` import binds), so the boundary line's actual output
is observed without needing a real store stack.

Conventions follow `tests/test_pack_create_lifecycle.py` /
`tests/test_pack_ingest_exact_match.py`: `opencrab.mcp.tools._get_context`
patched at the PACKAGE level (see `opencrab/mcp/tools/pack.py`'s own module
docstring for why a submodule-level patch would not be observed), a real
`Principal` bound via `principal_scope`, and `MagicMock` doubles for every
store the spied call never actually touches.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from opencrab.auth import Principal, principal_scope
from opencrab.pack import fork as fork_mod

ALICE = Principal(user_id="alice-t99", is_local=True, disabled=False)


def _ctx() -> dict:
    return {
        "sql": MagicMock(),
        "neo4j": MagicMock(),
        "mongo": MagicMock(),
        "chroma": MagicMock(),
        "hybrid": MagicMock(),
        "builder": MagicMock(),
    }


def test_t99_explicit_empty_new_pack_id_reaches_fork_pack_as_empty_string(monkeypatch):
    """T99: calling the `pack_fork` MCP tool with `new_pack_id=""` must
    hand `fork_pack` `""`, not `None` -- the boundary line is the only
    place before the orchestrator where this distinction could be lost.
    Reverse-mutation: reverting the boundary line in
    `opencrab/mcp/tools/pack.py` to plain truthiness
    (`_clean_str(new_pack_id) if new_pack_id else None`) makes `""` falsy
    again, so the spy receives `new_pack_id=None` instead of `""` and this
    test's assertion fails."""
    from opencrab.mcp.tools import pack_fork

    spy = MagicMock(return_value={"status": "ok", "pack_id": "src-fork"})
    monkeypatch.setattr(fork_mod, "fork_pack", spy)

    ctx = _ctx()
    with patch("opencrab.mcp.tools._get_context", return_value=ctx):
        with principal_scope(ALICE):
            pack_fork(pack_id="src", new_pack_id="")

    assert spy.call_count == 1
    assert spy.call_args.kwargs["new_pack_id"] == ""
    assert spy.call_args.kwargs["new_pack_id"] is not None
