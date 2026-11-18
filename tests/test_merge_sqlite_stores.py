"""``scripts/merge_sqlite_stores.py``: 여러 SQLite 파일을 한 파일로 합친다 (#671 시제품).

합성 원본(일반 테이블, fts5, vec0, 인덱스, 트리거, AUTOINCREMENT)으로 거는 것:
- 합친 파일의 테이블별 행 수가 원본과 같다(그림자 테이블 포함 전량).
- 합친 파일에서 vec0 KNN과 fts5 MATCH가 원본과 같은 결과를 낸다.
- 이름 충돌은 병합하지 않고 거부한다. 출력이 이미 있으면 거부한다.
- 계획 모드(``--apply`` 없음)는 출력 파일을 만들지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "merge_sqlite_stores.py"
_spec = importlib.util.spec_from_file_location("merge_sqlite_stores", SCRIPT)
merge_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_mod)


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.fixture
def source_dir(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    g = _conn(d / "graph.db")
    g.execute("CREATE TABLE graph_nodes (node_id TEXT PRIMARY KEY, props TEXT)")
    g.execute("CREATE INDEX idx_nodes_props ON graph_nodes(props)")
    g.executemany("INSERT INTO graph_nodes VALUES (?, ?)", [(f"n{i}", f"p{i % 3}") for i in range(30)])
    g.commit()
    g.execute("ANALYZE")  # 실제 스냅숏에 sqlite_stat1이 있었다: 예약 이름이라 DDL로 만들 수 없다
    g.close()

    s = _conn(d / "doc_store.db")
    s.execute("CREATE TABLE doc_sources (source_id TEXT PRIMARY KEY, text TEXT)")
    s.execute("CREATE TABLE seq_t (id INTEGER PRIMARY KEY AUTOINCREMENT, x)")
    s.execute("CREATE VIRTUAL TABLE doc_sources_fts USING fts5(source_id UNINDEXED, text, "
              "tokenize='unicode61 remove_diacritics 0')")
    s.execute("CREATE TRIGGER doc_sources_ai AFTER INSERT ON doc_sources BEGIN "
              "INSERT INTO seq_t(x) VALUES (new.source_id); END")
    for i in range(40):
        s.execute("INSERT INTO doc_sources VALUES (?, ?)", (f"s{i}", f"alpha beta 문서 {i}"))
        s.execute("INSERT INTO doc_sources_fts(source_id, text) VALUES (?, ?)",
                  (f"s{i}", f"alpha beta 문서 {i}"))
    s.execute("DELETE FROM seq_t WHERE id > 35")  # sqlite_sequence가 최댓값보다 크게 남는다
    s.commit()
    s.close()

    v = _conn(d / "vectors.db")
    v.execute("CREATE VIRTUAL TABLE vectors_kure USING vec0(node_id TEXT PRIMARY KEY, "
              "pack_id TEXT partition key, embedding float[4] distance_metric=cosine, +document TEXT)")
    for i in range(40):
        v.execute("INSERT INTO vectors_kure(node_id, pack_id, embedding, document) VALUES (?,?,?,?)",
                  (f"s{i}", "p1" if i % 2 else "p2", sqlite_vec.serialize_float32([i, 1, 0, 0.5]), f"d{i}"))
    v.commit()
    v.close()

    r = _conn(d / "opencrab.db")
    r.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY)")
    r.execute("INSERT INTO packs VALUES ('p1')")
    r.commit()
    r.close()
    return d


def test_plan_only_writes_nothing(source_dir, tmp_path, capsys):
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out)]) == 0
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()
    plan = json.loads(capsys.readouterr().out)
    assert plan["collisions"] == []


def test_merge_copies_every_table_and_queries_agree(source_dir, tmp_path):
    out = tmp_path / "merged.db"
    report_path = tmp_path / "report.json"
    rc = merge_mod.main([
        "--source-dir", str(source_dir), "--out", str(out), "--apply", "--verify", "--min-free-gib", "0",
        "--report", str(report_path), "--cache-kib", "4096",
    ])
    assert rc == 0
    report = json.loads(report_path.read_text())
    assert report["verify"]["ok"] is True
    assert report["finalize"]["journal_mode"] == "wal"
    assert not out.with_name("merged.db.partial").exists()

    merged = _conn(out)
    assert merged.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    for fname, tables in (("graph.db", ["graph_nodes"]),
                          ("doc_store.db", ["doc_sources", "seq_t", "doc_sources_fts"]),
                          ("vectors.db", ["vectors_kure"])):
        src = _conn(source_dir / fname)
        for t in tables:
            assert (merged.execute(f"SELECT count(*) FROM {t}").fetchone()
                    == src.execute(f"SELECT count(*) FROM {t}").fetchone()), t
        src.close()
    q = sqlite_vec.serialize_float32([3, 1, 0, 0.5])
    src_v = _conn(source_dir / "vectors.db")
    sql = "SELECT node_id FROM vectors_kure WHERE embedding MATCH ? AND k=5 AND pack_id='p1'"
    assert merged.execute(sql, (q,)).fetchall() == src_v.execute(sql, (q,)).fetchall()
    src_v.close()
    assert [r[0] for r in merged.execute(
        "SELECT source_id FROM doc_sources_fts WHERE doc_sources_fts MATCH 'alpha' ORDER BY rowid LIMIT 3")
    ] == ["s0", "s1", "s2"]
    # 트리거와 인덱스는 데이터 복사 뒤 만들어졌고 동작한다(복사 중 발화하지 않았다).
    assert merged.execute("SELECT count(*) FROM seq_t").fetchone()[0] == 35
    merged.execute("INSERT INTO doc_sources VALUES ('new', 'x')")
    assert merged.execute("SELECT count(*) FROM seq_t").fetchone()[0] == 36
    # 삭제된 행 때문에 시퀀스가 행 수보다 크다. 병합이 원본의 시퀀스를 유지해야 새 id가 겹치지 않는다.
    assert merged.execute("SELECT seq FROM sqlite_sequence WHERE name='seq_t'").fetchone()[0] == 41
    assert merged.execute("SELECT 1 FROM sqlite_master WHERE name='idx_nodes_props'").fetchone()
    src_g = _conn(source_dir / "graph.db")
    stat = "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx"
    assert merged.execute(stat).fetchall() == src_g.execute(stat).fetchall() != []
    src_g.close()
    merged.close()


def test_name_collision_is_refused(source_dir, tmp_path):
    extra = _conn(source_dir / "opencrab.db")
    extra.execute("CREATE TABLE graph_nodes (x)")
    extra.commit()
    extra.close()
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply"]) == 2
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_existing_output_is_refused(source_dir, tmp_path):
    out = tmp_path / "merged.db"
    out.write_bytes(b"keep")
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--min-free-gib", "0"]) == 1
    assert out.read_bytes() == b"keep"


def test_free_space_floor_refuses_before_writing(source_dir, tmp_path):
    out = tmp_path / "merged.db"
    rc = merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                         "--min-free-gib", "1000000"])
    assert rc == 3
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_memory_backed_output_directory_is_refused(source_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(merge_mod, "fs_type", lambda _path: "tmpfs")
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply"]) == 2
    assert not out.with_name("merged.db.partial").exists()


def test_floor_reached_during_the_copy_removes_the_partial_output(source_dir, tmp_path, monkeypatch):
    calls = {"n": 0}
    real = merge_mod.check_free

    def flaky(path, floor, need=0):
        calls["n"] += 1
        if calls["n"] > 1:  # 첫 호출은 시작 전 점검이다. 복사 중 점검에서 실패시킨다.
            raise merge_mod.DiskFloorError("simulated floor")
        return real(path, floor, need)

    monkeypatch.setattr(merge_mod, "check_free", flaky)
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--min-free-gib", "0"]) == 3
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_physical_rowids_survive_when_rows_and_chunks_were_deleted(source_dir, tmp_path):
    """열만 옮기면 물리 rowid가 1부터 다시 매겨진다. 실제 스냅숏에서 vec0 조회가 깨졌다."""
    v = _conn(source_dir / "vectors.db")
    for i in range(2000):  # 기본 청크 크기(1024)를 넘겨 청크를 둘 이상 만든다
        v.execute("INSERT INTO vectors_kure(node_id, pack_id, embedding, document) VALUES (?,?,?,?)",
                  (f"x{i}", "p1", sqlite_vec.serialize_float32([i, 1, 0, 0.5]), f"d{i}"))
    v.commit()
    # 첫 청크가 비도록 앞의 1024행(s0~s39, x0~x983)을 지운다.
    for node_id in [f"s{i}" for i in range(40)] + [f"x{i}" for i in range(984)]:
        v.execute("DELETE FROM vectors_kure WHERE node_id = ?", (node_id,))
    v.commit()
    first_chunk = v.execute("SELECT min(chunk_id) FROM vectors_kure_chunks").fetchone()[0]
    v.close()
    assert first_chunk > 1, "첫 청크가 비어 사라지지 않았다: 시험이 공허하다"
    s = _conn(source_dir / "doc_store.db")
    s.execute("DELETE FROM doc_sources WHERE source_id = 's3'")  # TEXT 기본키 표의 rowid에 틈을 만든다
    s.commit()
    s.close()

    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply", "--verify",
                           "--min-free-gib", "0", "--cache-kib", "4096"]) == 0
    merged, src = _conn(out), _conn(source_dir / "doc_store.db")
    sql = "SELECT source_id, rowid FROM doc_sources ORDER BY source_id"
    assert merged.execute(sql).fetchall() == src.execute(sql).fetchall()
    got = merged.execute("SELECT length(embedding) FROM vectors_kure WHERE node_id = 'x1500'").fetchone()
    assert got == (16,)
    merged.close()
    src.close()


def test_unknown_shadow_suffix_is_refused_instead_of_copied_as_a_regular_table(source_dir, tmp_path):
    v = _conn(source_dir / "vectors.db")
    v.execute("CREATE TABLE vectors_kure_extra (x)")
    v.commit()
    v.close()
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply"]) == 2
    assert not out.exists()
    assert not out.with_name("merged.db.partial").exists()


def test_publish_syncs_the_file_before_the_rename_and_the_directory_after(source_dir, tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = merge_mod.os.fsync, merge_mod.os.replace

    def fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(merge_mod.os, "fsync", fsync)
    monkeypatch.setattr(merge_mod.os, "replace", replace)
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                           "--min-free-gib", "0", "--cache-kib", "4096"]) == 0
    assert events[-3:] == ["fsync", "replace", "fsync"]


@pytest.mark.parametrize("order", [("notes", "notes_tags"), ("notes_tags", "notes")])
def test_nested_virtual_table_names_are_accepted_in_either_creation_order(source_dir, tmp_path, order):
    s = _conn(source_dir / "doc_store.db")
    for name in order:
        s.execute(f"CREATE VIRTUAL TABLE {name} USING fts5(body)")
    s.commit()
    s.close()
    out = tmp_path / "merged.db"
    assert merge_mod.main(["--source-dir", str(source_dir), "--out", str(out), "--apply",
                           "--min-free-gib", "0", "--cache-kib", "4096"]) == 0
    merged = _conn(out)
    names = {r[0] for r in merged.execute("SELECT name FROM sqlite_master WHERE name LIKE 'notes%'")}
    assert {"notes", "notes_tags", "notes_data", "notes_tags_data"} <= names
    merged.close()
