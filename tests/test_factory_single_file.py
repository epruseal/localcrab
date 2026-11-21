"""factory 단일 파일 연결(#671)의 시험 바탕 `_authorized_root`와 그 자체 점검(S0).

`_authorized_root`는 다중 파일 factory 네 개가 실제로 만든 데이터 디렉터리를 게시한 뒤
대장을 authorized로 다시 쓴 root를 돌려준다. 이 파일은 헬퍼만 가진다. 단일 모드 factory
시험은 S1 이후에 이 파일에 더한다.
"""

from __future__ import annotations

import hashlib
import json
import resource
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from tests.test_merge_sqlite_stores import _ready_setup, merge_mod

_DIM = 4


def _hash_embed(texts):
    """네트워크와 모델 내려받기를 쓰지 않는 4차원 해시 임베딩."""
    vectors = []
    for text in texts:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        vectors.append([digest[i] / 255.0 for i in range(_DIM)])
    return vectors


def _plain_table_counts(db_path: Path) -> dict[str, int]:
    """가상표를 뺀 일반 표의 행 수. 그림자 표는 일반 표로 센다."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name"
            )
        ]
        return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}
    finally:
        conn.close()


def _authorized_root(base_dir, monkeypatch):
    """`base_dir` 아래에 live, src, published를 만들고 authorized 대장의 게시 root를 돌려준다.

    한 시험에서 두 번 이상 부르면 호출마다 새 하위 디렉터리를 만들어 넘긴다. 같은 디렉터리로
    다시 부르면 단일 로컬 사용자 색인과 게시의 "publication root already exists"가 막는다.
    """
    from opencrab.auth import create_user
    from opencrab.config import Settings, get_settings
    from opencrab.pack.ownership import ensure_default_pack
    from opencrab.stores import factory

    base_dir = Path(base_dir)
    live = base_dir / "live"
    live.mkdir()
    monkeypatch.setenv("LOCAL_DATA_DIR", str(live))
    get_settings.cache_clear()
    monkeypatch.setattr(factory, "_make_kure_embedding_function", lambda settings: _hash_embed)
    settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(live), EMBEDDING_BACKEND="openai", EMBED_DIM=_DIM)

    graph = factory.make_graph_store(settings)
    docs = factory.make_doc_store(settings)
    vec = factory.make_vector_store(settings)
    sql = factory.make_sql_store(settings)
    try:
        owner = create_user(sql, "owner", is_local=True)
        pack_id = ensure_default_pack(sql, owner)
        graph.upsert_node("Entity", "n1", {"pack_id": pack_id, "name": "node one"})
        docs.upsert_node_doc("space1", "Entity", "n1", {"pack_id": pack_id, "name": "node one"})
        docs.upsert_source("src1", "source text one", {"pack_id": pack_id})
        vec.upsert_texts(["vector text one"], metadatas=[{"pack_id": pack_id}], ids=["v1"])
    finally:
        for store in (vec, docs, graph, sql):
            store.close()
    leftovers = sorted(p.name for p in live.iterdir() if p.name.endswith(("-wal", "-shm")))
    assert leftovers == [], leftovers
    counts = {}
    for name in merge_mod.FILES:
        for table, rows in _plain_table_counts(live / name).items():
            counts[f"{name}:{table}"] = rows

    src = base_dir / "src"
    src.mkdir()
    for name in merge_mod.FILES:
        shutil.copy2(live / name, src / name)
    ctx = _ready_setup(src, base_dir)
    rc = merge_mod.main(ctx.args)
    assert rc == 0, rc

    ledger_path = ctx.root / "ledger.json"
    ledger = json.loads(ledger_path.read_text())
    assert ledger["state"] == "ready", ledger["state"]
    ledger_path.write_text(json.dumps({**ledger, "state": "authorized"}, sort_keys=True, separators=(",", ":")))

    uri = f"{(ctx.root / 'single.db').as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        incarnations = [row[0] for row in conn.execute("SELECT incarnation FROM packs")]
    finally:
        conn.close()
    assert incarnations and all(value for value in incarnations), incarnations
    return SimpleNamespace(root=ctx.root, owner=owner, pack_id=pack_id, counts=counts,
                           incarnations=incarnations)


def test_authorized_root_helper_self_check(tmp_path, monkeypatch, capsys):
    """S0 자체 점검: 게시 rc 0과 incarnation은 헬퍼가 단언한다. 여기서는 BM25 상태와 비용을 잰다."""
    from opencrab.stores import _node_bm25_sqlite as nbm

    started = time.monotonic()
    built = _authorized_root(tmp_path, monkeypatch)
    elapsed = time.monotonic() - started

    ledger = json.loads((built.root / "ledger.json").read_text())
    assert ledger["state"] == "authorized"

    uri = f"{(built.root / 'single.db').as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        meta = nbm._meta(conn)
        pending = conn.execute(f"SELECT COUNT(*) FROM {nbm.PENDING}").fetchone()[0]
        indexed = conn.execute(f"SELECT doc_count FROM {nbm.STATS} WHERE id=1").fetchone()
        rows = conn.execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0]
    finally:
        conn.close()
    assert meta.get("state") == nbm.STATE_READY, meta
    assert pending == 0, pending
    assert indexed is not None and int(indexed[0]) == rows and rows >= 1, (indexed, rows)

    maxrss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with capsys.disabled():
        print(f"\nS0-MEASURE elapsed_s={elapsed:.2f} maxrss_kib={maxrss_kib} "
              f"bm25_state={meta.get('state')} pending={pending} doc_count={indexed[0]} doc_nodes={rows} "
              f"incarnations={built.incarnations}")
        print("S0-COUNTS " + json.dumps(built.counts, sort_keys=True))
