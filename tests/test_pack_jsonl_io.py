"""by-pack 물리 포맷(opencrab.pack.jsonl_io) 테스트.

opencrab-dump 의 수동 스모크(scripts/qa/test_jsonl_io.py, 17 checks)를 pytest 로
옮기고 shard 계층의 loud-fail 설계를 검사로 고정한다.

핵심 불변식: **분할되면 base 이름의 파일은 존재하지 않는다.** 미개조 소비자가
base 만 읽고 일부 데이터만 조용히 얻는 silent partial read 를 FileNotFoundError
로 즉사시키기 위한 설계다. 이 불변식이 깨지면 데이터 유실이 조용히 일어난다.
"""

import hashlib
import json

import pytest

from opencrab.pack.jsonl_io import (
    SHARD_LIMIT,
    ShardedAppender,
    count_jsonl,
    iter_jsonl,
    iter_jsonl_lines,
    jsonl_exists,
    logical_sha256,
    now_iso,
    sha,
    shard_paths,
    slug,
    write_jsonl,
    write_jsonl_sharded,
)


def rec(i, pad=0):
    return {"id": i, "text": "x" * pad}


@pytest.fixture
def p(tmp_path):
    return tmp_path / "chunks.jsonl"


# ---------------------------------------------------------------------------
# rewrite: 미분할 / 분할 / 축소 복귀
# ---------------------------------------------------------------------------

class TestWriteRewrite:
    def test_small_stays_single_base_file(self, p):
        rows = [rec(i) for i in range(10)]
        assert write_jsonl_sharded(p, rows, limit=10_000) == [p]
        assert p.exists()
        assert list(iter_jsonl(p)) == rows

    def test_over_limit_splits_and_removes_base(self, p):
        big = [rec(i, pad=400) for i in range(50)]
        out = write_jsonl_sharded(p, big, limit=5_000)
        assert not p.exists(), "분할됐는데 base 가 남으면 미개조 소비자가 부분만 읽는다"
        assert len(out) >= 4
        assert out[0].name == "chunks.00.jsonl"
        assert list(iter_jsonl(p)) == big

    def test_shard_names_are_zero_padded_two_digits(self, p):
        big = [rec(i, pad=400) for i in range(50)]
        out = write_jsonl_sharded(p, big, limit=5_000)
        for i, path in enumerate(out):
            assert path.name == f"chunks.{i:02d}.jsonl"

    def test_each_shard_respects_limit(self, p):
        write_jsonl_sharded(p, [rec(i, pad=400) for i in range(50)], limit=5_000)
        assert all(s.stat().st_size <= 5_000 for s in shard_paths(p))

    def test_shrink_returns_to_base_and_cleans_stale_shards(self, p):
        write_jsonl_sharded(p, [rec(i, pad=400) for i in range(50)], limit=5_000)
        assert len(shard_paths(p)) > 1
        out = write_jsonl_sharded(p, [rec(i) for i in range(10)], limit=10_000)
        assert out == [p]
        assert shard_paths(p) == [p], "구 shard 가 남으면 base+shard 공존으로 손상 판정된다"

    def test_oversized_single_line_is_never_split(self, p):
        rows = [rec(0, 9_000), rec(1, 9_000)]
        write_jsonl_sharded(p, rows, limit=1_000)
        assert list(iter_jsonl(p)) == rows
        assert len(shard_paths(p)) == 2, "라인 1개가 상한을 넘어도 라인 단위는 유지된다"

    def test_str_records_written_verbatim(self, p):
        """str 레코드는 이미 직렬화된 JSON 라인으로 간주한다(dict 만 dumps)."""
        write_jsonl_sharded(p, ['{"id": 1}', {"id": 2}], limit=10_000)
        assert p.read_text(encoding="utf-8") == '{"id": 1}\n{"id": 2}\n'

    def test_non_ascii_is_not_escaped(self, p):
        write_jsonl_sharded(p, [{"k": "한글"}], limit=10_000)
        assert "한글" in p.read_text(encoding="utf-8")

    def test_empty_records_creates_empty_base(self, p):
        assert write_jsonl_sharded(p, [], limit=10_000) == [p]
        assert p.read_text(encoding="utf-8") == ""
        assert list(iter_jsonl(p)) == []

    def test_creates_parent_directory(self, tmp_path):
        deep = tmp_path / "a" / "b" / "nodes.jsonl"
        write_jsonl_sharded(deep, [rec(0)])
        assert deep.exists()

    def test_write_jsonl_delegates_to_sharded(self, p):
        write_jsonl(p, [rec(i) for i in range(3)])
        assert list(iter_jsonl(p)) == [rec(i) for i in range(3)]

    def test_default_limit_is_shard_limit(self, p):
        """limit 생략 시 모듈 기본값을 쓴다 — 40MB 미만은 단일 파일."""
        assert SHARD_LIMIT >= 1 << 20
        assert write_jsonl_sharded(p, [rec(i) for i in range(5)]) == [p]


# ---------------------------------------------------------------------------
# append: 롤오버와 이어쓰기
# ---------------------------------------------------------------------------

class TestShardedAppender:
    def test_rollover_renames_base_preserving_bytes(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        write_jsonl_sharded(q, [rec(i, 100) for i in range(5)], limit=100_000)
        before = q.read_bytes()
        with ShardedAppender(q, limit=1_000) as w:
            for i in range(5, 25):
                w.write(rec(i))
        shards = shard_paths(q)
        assert not q.exists()
        assert len(shards) >= 2
        assert shards[0].read_bytes()[:len(before)] == before, \
            "base->.00 rename 은 기존 바이트를 재작성하지 않아야 한다"
        assert [r["id"] for r in iter_jsonl(q)] == list(range(25))

    def test_append_continues_last_shard_when_under_limit(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        write_jsonl_sharded(q, [rec(i, 100) for i in range(5)], limit=100_000)
        with ShardedAppender(q, limit=1_000) as w:
            for i in range(5, 25):
                w.write(rec(i))
        last = shard_paths(q)[-1]
        with ShardedAppender(q, limit=1_000_000) as w:
            w.write(rec(99))
        assert shard_paths(q)[-1] == last
        assert [r["id"] for r in iter_jsonl(q)][-1] == 99

    def test_append_to_missing_file_creates_base(self, tmp_path):
        q = tmp_path / "new.jsonl"
        with ShardedAppender(q) as w:
            w.write(rec(1))
        assert q.exists()
        assert list(iter_jsonl(q)) == [rec(1)]

    def test_write_line_takes_serialized_line(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        with ShardedAppender(q) as w:
            w.write_line(json.dumps({"id": 7}))
        assert list(iter_jsonl(q)) == [{"id": 7}]

    def test_flush_and_close_are_callable(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        w = ShardedAppender(q)
        w.write(rec(1))
        w.flush()
        assert q.read_text(encoding="utf-8").strip() != ""
        w.close()


# ---------------------------------------------------------------------------
# 논리 스트림 읽기와 loud-fail
# ---------------------------------------------------------------------------

class TestReadAndLoudFail:
    def test_missing_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            list(iter_jsonl(tmp_path / "none.jsonl"))

    def test_missing_ok_yields_nothing(self, tmp_path):
        assert list(iter_jsonl(tmp_path / "none.jsonl", missing_ok=True)) == []

    def test_base_and_shard_coexisting_is_corruption(self, tmp_path):
        """부분 마이그레이션/롤백 잔재. 조용히 한쪽만 읽으면 데이터가 반쯤 사라진다."""
        q = tmp_path / "raw.jsonl"
        write_jsonl_sharded(q, [rec(i, 400) for i in range(30)], limit=2_000)
        assert len(shard_paths(q)) > 1
        q.write_text('{"id":0}\n', encoding="utf-8")
        with pytest.raises(RuntimeError, match="base와 shard"):
            shard_paths(q)

    def test_jsonl_exists(self, p, tmp_path):
        write_jsonl_sharded(p, [rec(0)])
        assert jsonl_exists(p)
        assert not jsonl_exists(tmp_path / "none.jsonl")

    def test_count_jsonl_sums_all_shards(self, p):
        write_jsonl_sharded(p, [rec(i, 400) for i in range(50)], limit=5_000)
        assert count_jsonl(p) == 50

    def test_logical_sha256_equals_concatenated_bytes(self, p):
        write_jsonl_sharded(p, [rec(i, 400) for i in range(50)], limit=5_000)
        concat = b"".join(s.read_bytes() for s in shard_paths(p))
        assert logical_sha256(p) == hashlib.sha256(concat).hexdigest()

    def test_logical_sha256_is_order_dependent(self, p):
        """shard 순서가 뒤바뀌면 다른 해시여야 한다 — 무손실 검증의 전제."""
        write_jsonl_sharded(p, [rec(i, 400) for i in range(50)], limit=5_000)
        forward = logical_sha256(p)
        parts = [s.read_bytes() for s in shard_paths(p)]
        reversed_hash = hashlib.sha256(b"".join(reversed(parts))).hexdigest()
        assert forward != reversed_hash

    def test_blank_lines_skipped_by_iter_jsonl_but_kept_in_raw(self, p):
        p.write_text('{"id":1}\n\n{"id":2}\n', encoding="utf-8")
        assert list(iter_jsonl(p)) == [{"id": 1}, {"id": 2}]
        assert list(iter_jsonl_lines(p)) == ['{"id":1}', "", '{"id":2}']

    def test_shard_paths_are_returned_in_numeric_order(self, p):
        write_jsonl_sharded(p, [rec(i, 400) for i in range(300)], limit=2_000)
        names = [s.name for s in shard_paths(p)]
        assert names == sorted(names)
        assert len(names) > 9, "두 자리 정렬 문제를 드러내려면 10개 이상 필요"


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_sha_is_sha256_utf8(self):
        assert sha("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_sha_handles_surrogates(self):
        """surrogatepass 없이는 크롤링 원문에서 UnicodeEncodeError 가 난다."""
        assert len(sha("\ud800")) == 64

    def test_slug_lowercases_and_dashes(self):
        assert slug("Hello World!") == "hello-world"

    def test_slug_strips_edges_but_does_not_collapse_runs(self):
        """양끝만 다듬고 연속 구분자는 합치지 않는다(characterization).

        `"-".join(s.strip("-").split("-"))` 는 항등식이라 실제로 압축이 일어나지
        않는다. 이름이 그렇게 읽히므로 여기 못박아 둔다 — 압축을 도입하면
        기존 slug 로 만들어진 팩·문서 id 가 전부 바뀐다.
        """
        assert slug("  --A  B--  ") == "a--b"

    def test_slug_caps_at_80_chars(self):
        assert len(slug("a" * 200)) == 80

    def test_slug_empty_falls_back(self):
        assert slug("!!!") == "unknown"

    def test_now_iso_is_utc_isoformat(self):
        assert now_iso().endswith("+00:00")
