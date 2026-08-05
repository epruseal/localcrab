"""팩 물리 포맷(opencrab.pack.jsonl_io) 테스트.

호스트 쪽 수동 스모크(17 checks)를 pytest 로 옮기고 shard 계층의 loud-fail 설계를
검사로 고정한다.

핵심 불변식: **분할되면 base 이름의 파일은 존재하지 않는다.** 미개조 소비자가
base 만 읽고 일부 데이터만 조용히 얻는 silent partial read 를 FileNotFoundError
로 즉사시키기 위한 설계다. 이 불변식이 깨지면 데이터 유실이 조용히 일어난다.
"""

import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from opencrab.pack import jsonl_io as jsonl_io_mod
from opencrab.pack.jsonl_io import (
    _MAX_SHARDS,
    SHARD_LIMIT,
    ShardedAppender,
    _shard_path,
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


def _line_bytes(record) -> int:
    """이 레코드가 파일에서 차지하는 바이트(직렬화 + 개행).

    경계 테스트가 limit 을 이 값에서 **유도**하게 하려고 둔다. 상수를 손으로 고르면
    롤오버 조건이 1바이트 흔들려도 분할 지점이 안 바뀌어 경계 변이가 그냥 산다.
    """
    return len(json.dumps(record, ensure_ascii=False).encode("utf-8")) + 1


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

    def test_iter_jsonl_lines_default_raises_on_missing(self, tmp_path):
        """raw 라인 스트림도 기본은 loud-fail 이다.

        `iter_jsonl` 쪽만 검사하고 있었다. 그래서 `missing_ok: bool = False` 를 `True` 로
        바꾸는 변이가 40 건 전부 통과한 채 살아남았다(2026-08-05 적대 검증). 두 함수는
        기본값이 같아야 하고, 그 기본값이 이 리포의 silent partial read 방지 설계다.
        """
        with pytest.raises(FileNotFoundError):
            list(iter_jsonl_lines(tmp_path / "none.jsonl"))

    def test_iter_jsonl_lines_missing_ok_yields_nothing(self, tmp_path):
        assert list(iter_jsonl_lines(tmp_path / "none.jsonl", missing_ok=True)) == []

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

class TestShardBoundaries:
    """경계 조건. 전부 2026-08-04 적대 검증에서 **생존한 돌연변이**를 닫는 검사다."""

    def test_shard_index_limit_is_enforced_at_the_boundary(self, p):
        """`assert idx < _MAX_SHARDS` 를 `<=` 로 바꿔도 아무 테스트가 안 죽었다.

        idx=100 이면 `chunks.100.jsonl` 이 만들어지는데 `shard_paths` 의 glob 은
        `[0-9][0-9]` 라 그 파일을 **못 본다**. 즉 데이터가 조용히 사라진다.
        이 assert 가 유일한 방어선이므로 경계 양쪽을 못박는다.
        """
        assert _shard_path(p, _MAX_SHARDS - 1).name == "chunks.99.jsonl"
        with pytest.raises(AssertionError):
            _shard_path(p, _MAX_SHARDS)

    def test_glob_cannot_see_three_digit_shards(self, p):
        """위 assert 가 왜 유일한 방어선인지 — 규칙 자체를 고정한다."""
        p.with_name("chunks.100.jsonl").write_text('{"id":0}\n', encoding="utf-8")
        assert shard_paths(p) == []

    def test_appender_shards_never_exceed_limit(self, tmp_path):
        """append writer 가 만든 shard 는 전부 limit 이하다.

        limit 을 **레코드 크기에서 유도**한다. 예전에는 `limit=300` 에 60바이트 레코드를
        썼는데, 그러면 롤오버 조건이 1바이트 흔들려도 분할 지점이 안 바뀌어 경계 변이가
        그냥 산다(`> limit` -> `> limit + 1` 이 실제로 생존했다, 2026-08-04 실측).
        `limit = 2*n - 1` 이면 두 번째 줄에서 `size + nbytes == limit + 1` 이 정확히
        성립해 그 1바이트가 판정을 뒤집는다 — 상수를 손으로 고르면 이 성질을 잃는다.
        """
        q = tmp_path / "raw.jsonl"
        n = _line_bytes(rec(1))
        limit = 2 * n - 1
        with ShardedAppender(q, limit=limit) as w:
            w.write(rec(1))
            w.write(rec(1))
        shards = shard_paths(q)
        assert len(shards) == 2, "두 번째 줄은 limit 을 1바이트 넘겨 롤오버해야 한다"
        assert all(s.stat().st_size <= limit for s in shards), \
            [s.stat().st_size for s in shards]

    def test_rewrite_shards_never_exceed_limit(self, p):
        """rewrite writer 도 같은 경계를 지킨다 — **두 writer 를 대칭으로 건다.**

        예전 주석은 "이 변이가 write_jsonl_sharded 에서는 잡힌다"고 적었는데 거짓이었다.
        그 오진 때문에 rewrite 쪽 경계가 점검에서 통째로 빠져 있었다(2026-08-04 적대
        검증). 진단을 믿고 한쪽을 빼면 그 한쪽이 그대로 무방비가 된다.
        """
        n = _line_bytes(rec(1))
        limit = 2 * n - 1
        out = write_jsonl_sharded(p, [rec(1), rec(1)], limit=limit)
        assert len(out) == 2
        assert all(s.stat().st_size <= limit for s in shard_paths(p))

    def test_rewrite_exact_fit_does_not_roll_over(self, p):
        """rewrite 쪽 롤오버 경계(`>` vs `>=`)도 대칭으로 고정한다."""
        n = _line_bytes(rec(1))
        assert write_jsonl_sharded(p, [rec(1), rec(1)], limit=2 * n) == [p]

    def test_appender_exact_fit_does_not_roll_over(self, tmp_path):
        """롤오버 조건 `>` 를 `>=` 로 바꿔도 생존했다 — 경계 동작을 고정한다."""
        q = tmp_path / "raw.jsonl"
        n = _line_bytes(rec(1))
        with ShardedAppender(q, limit=2 * n) as w:   # 정확히 두 줄이 들어간다
            w.write(rec(1))
            w.write(rec(1))
        assert shard_paths(q) == [q], "정확히 맞는 두 번째 줄은 롤오버를 유발하면 안 된다"

    def test_default_shard_limit_is_forty_megabytes(self):
        """테스트가 늘 limit 을 주입해서 기본값 자체는 무검증이었다(변이 생존)."""
        assert SHARD_LIMIT == 40 * 1024 * 1024


class TestTwoWritersHoldTheSameContract:
    """rewrite(`write_jsonl_sharded`) 와 append(`ShardedAppender`) 는 **대칭** 이다.

    이 파일에는 이미 "두 writer 를 대칭으로 건다"는 교훈이 적혀 있다(경계 조건 쪽,
    2026-08-04). 그런데 그 원칙을 **경계에만** 적용하고 나머지 계약에는 적용하지
    않았다. 전면 스윕이 그 대가를 보여줬다(2026-08-05): 아래 넷이 전부 rewrite 쪽만
    검사돼 append 쪽 변이가 그대로 생존했다.

        부모 디렉터리 생성 / ensure_ascii=False / 첫 shard 이름 .00 / str 경로 수용

    한쪽만 거는 순간 다른 쪽이 무방비가 된다 — 클래스로 닫는다.
    """

    def test_appender_creates_parent_directory(self, tmp_path):
        deep = tmp_path / "a" / "b" / "nodes.jsonl"
        with ShardedAppender(deep) as w:
            w.write(rec(0))
        assert deep.exists()

    def test_appender_does_not_escape_non_ascii(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        with ShardedAppender(q) as w:
            w.write({"k": "한글"})
        assert "한글" in q.read_text(encoding="utf-8")

    def test_appender_rollover_names_first_shard_zero_zero(self, tmp_path):
        """base -> .00 rename. 인덱스를 1 로 바꿔도 아무 검사가 안 죽었다."""
        q = tmp_path / "raw.jsonl"
        n = _line_bytes(rec(1))
        with ShardedAppender(q, limit=2 * n - 1) as w:
            w.write(rec(1))
            w.write(rec(1))
        assert [s.name for s in shard_paths(q)] == ["raw.00.jsonl", "raw.01.jsonl"]

    @pytest.mark.parametrize("writer", ["rewrite", "append"])
    def test_str_path_is_accepted(self, tmp_path, writer):
        """시그니처가 `Path | str` 다. `path = Path(path)` 를 지워도 아무도 안 죽었다."""
        q = tmp_path / "raw.jsonl"
        if writer == "rewrite":
            write_jsonl_sharded(str(q), [rec(0)])
        else:
            with ShardedAppender(str(q)) as w:
                w.write(rec(0))
        assert list(iter_jsonl(str(q))) == [rec(0)]
        assert shard_paths(str(q)) == [q]
        assert count_jsonl(str(q)) == 1
        assert jsonl_exists(str(q))


class TestSizeTrackingSurvivesRollover:
    """롤오버 **이후** 의 크기 추적. 두 writer 대칭으로 건다.

    기존 경계 검사가 전부 **롤오버 1 회짜리**(2 줄)라 "롤오버 뒤 size 를 0 으로
    리셋하는가"가 무검사로 남았다. `self.size = 0` 을 `= 1` 로 바꾸거나 그 문장을
    지워도 통과했다(2026-08-05 스윕). 리셋이 틀어지면 **두 번째 shard 부터** 상한이
    조금씩 밀려 shard 크기가 계약을 넘는다 — GitHub 100MB 하드리밋이 그 계약의 이유다.

    n 줄이 정확히 shard 하나에 들어가는 limit 을 쓰고 3 개 shard 를 만들어, 두 번째
    롤오버까지 태운다.
    """

    def test_appender_rolls_over_at_the_same_boundary_every_time(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        n = _line_bytes(rec(1))
        limit = 2 * n                      # 정확히 두 줄이 한 shard
        with ShardedAppender(q, limit=limit) as w:
            for _ in range(6):
                w.write(rec(1))
        shards = shard_paths(q)
        assert [s.name for s in shards] == [
            "raw.00.jsonl", "raw.01.jsonl", "raw.02.jsonl"]
        assert [s.stat().st_size for s in shards] == [limit, limit, limit], \
            "리셋이 틀어지면 두 번째 shard 부터 크기가 밀린다"
        assert len(list(iter_jsonl(q))) == 6

    def test_rewrite_rolls_over_at_the_same_boundary_every_time(self, p):
        n = _line_bytes(rec(1))
        limit = 2 * n
        out = write_jsonl_sharded(p, [rec(1)] * 6, limit=limit)
        assert [f.name for f in out] == [
            "chunks.00.jsonl", "chunks.01.jsonl", "chunks.02.jsonl"]
        assert [f.stat().st_size for f in out] == [limit, limit, limit]
        assert len(list(iter_jsonl(p))) == 6

    def test_appender_size_starts_from_the_existing_file(self, tmp_path):
        """이어쓰기는 기존 파일 크기에서 출발한다 — 0 에서 시작하면 상한을 넘긴다."""
        q = tmp_path / "raw.jsonl"
        n = _line_bytes(rec(1))
        write_jsonl_sharded(q, [rec(1)], limit=10 * n)
        with ShardedAppender(q, limit=2 * n) as w:
            w.write(rec(1))
            w.write(rec(1))              # 여기서 상한 초과 -> 롤오버
        assert all(s.stat().st_size <= 2 * n for s in shard_paths(q))
        assert len(list(iter_jsonl(q))) == 3


class TestOneByteBoundary:
    """`if size > 0 and …` 의 **0** 경계. 두 writer 대칭.

    `> 0` 을 `> 1` 로 바꾸면 **size 가 정확히 1일 때만** 판정이 갈린다. 그런 상태에 닿는
    입력은 하나뿐이다 — 빈 문자열 레코드(`nbytes = 0 + 1 = 1`), 또는 개행 하나만 든 기존 파일.
    기존 경계 검사가 전부 정상 크기 레코드를 써서 이 한 칸을 못 봤다(2026-08-05 스윕).

    실측: 원본은 상한 초과를 감지해 분할하고, `> 1` 변이는 **분할하지 않는다.**
    상한을 넘긴 파일이 조용히 하나로 남으면 GitHub 100MB 하드리밋에 그대로 걸린다.
    """

    def test_rewrite_rolls_over_after_a_one_byte_record(self, p):
        out = write_jsonl_sharded(p, ["", "abc"], limit=2)
        assert [f.name for f in out] == ["chunks.00.jsonl", "chunks.01.jsonl"], \
            "size 가 정확히 1 이어도 상한 초과는 감지해야 한다"
        assert list(iter_jsonl_lines(p)) == ["", "abc"]

    def test_appender_rolls_over_from_a_one_byte_existing_file(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        q.write_text("\n", encoding="utf-8")           # 정확히 1바이트
        with ShardedAppender(q, limit=2) as w:
            w.write_line("abc")
        assert [s.name for s in shard_paths(q)] == ["raw.00.jsonl", "raw.01.jsonl"]
        assert list(iter_jsonl_lines(q)) == ["", "abc"]


class TestDottedLogicalNames:
    """논리 파일명에 점이 있어도 shard 인덱스를 정확히 집는다.

    `int(stem.rsplit(".", 1)[1])` 의 **maxsplit 1** 이 계약이다. 처음엔 "shard stem 의 점은
    항상 1개"라고 측정해 등가로 판정했는데 **틀렸다** — `chunks.jsonl` 한 종류만 봤다.
    논리 이름에 점이 있으면 분할 stem 은 `chunks.v1.03` 이 되고, `rsplit(".", 2)[1]` 은
    `'v1'` 을 집어 `int()` 에서 터진다(적대 검증 실증, 2026-08-05).

    측정할 때도 입력이 분기를 구분해야 한다는 것의 실례다.
    """

    @pytest.mark.parametrize("name", ["chunks.v1.jsonl", "nodes.2026-08.jsonl"])
    def test_rollover_reads_the_index_not_a_name_segment(self, tmp_path, name):
        q = tmp_path / name
        n = _line_bytes(rec(1))
        with ShardedAppender(q, limit=2 * n - 1) as w:
            for _ in range(4):
                w.write(rec(1))
        stem = Path(name).stem
        assert [s.name for s in shard_paths(q)] == [
            f"{stem}.{i:02d}.jsonl" for i in range(4)]
        assert len(list(iter_jsonl(q))) == 4

    @pytest.mark.parametrize("name", ["chunks.v1.jsonl", "nodes.2026-08.jsonl"])
    def test_rewrite_rollover_handles_dotted_names(self, tmp_path, name):
        q = tmp_path / name
        n = _line_bytes(rec(1))
        out = write_jsonl_sharded(q, [rec(1)] * 4, limit=2 * n - 1)
        stem = Path(name).stem
        assert [f.name for f in out] == [f"{stem}.{i:02d}.jsonl" for i in range(4)]
        assert len(list(iter_jsonl(q))) == 4


class TestStaleShardCleanup:
    """축소 rewrite 가 구 shard 를 지우는 경로. `unlink(missing_ok=True)` 가 계약이다.

    base -> .00 rename 이 일어나면 `old` 에 담아 둔 base 는 **이미 사라진 뒤**라
    `missing_ok=False` 면 rewrite 가 통째로 터진다. 기존 검사는 빈 디렉터리에서
    시작해 `old` 가 비어 있었고, 그래서 unlink 가 한 번도 안 불렸다.
    """

    def test_rewrite_over_an_existing_base_that_gets_renamed(self, p):
        n = _line_bytes(rec(1))
        write_jsonl_sharded(p, [rec(1)], limit=10 * n)      # base 단일 파일 생성
        assert p.exists()
        out = write_jsonl_sharded(p, [rec(1)] * 4, limit=2 * n)  # 분할 -> base rename
        assert out[0].name == "chunks.00.jsonl"
        assert not p.exists()
        assert len(list(iter_jsonl(p))) == 4

    def test_shrinking_from_many_shards_removes_every_leftover(self, p):
        n = _line_bytes(rec(1))
        write_jsonl_sharded(p, [rec(1)] * 8, limit=2 * n)
        before = shard_paths(p)
        assert len(before) == 4
        # 2 줄은 정확히 상한에 맞아 롤오버가 없다 -> base 단일 파일로 돌아온다.
        write_jsonl_sharded(p, [rec(1)] * 2, limit=2 * n)
        assert shard_paths(p) == [p]
        assert not any(s.exists() for s in before), "구 shard 가 전부 지워져야 한다"
        assert len(list(iter_jsonl(p))) == 2


class TestAppenderClosesItsFile:
    """`__exit__` 이 `close()` 대신 `flush()` 를 불러도 22 건이 전부 통과했다.

    CPython 이 참조가 끊긴 파일 객체를 GC 로 닫아 주기 때문이다. 그 동작에 기대면
    (a) 다른 구현·다른 GC 타이밍에서 버퍼가 남고 (b) 열린 fd 가 누적된다.
    context manager 를 쓰는 이유가 결정적 해제이므로 계약으로 못박는다.
    """

    def test_exit_closes_the_underlying_file(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        with ShardedAppender(q) as w:
            w.write(rec(1))
            assert not w._f.closed
        assert w._f.closed, "with 블록을 벗어나면 파일은 닫혀 있어야 한다"

    def test_close_is_not_merely_flush(self, tmp_path):
        q = tmp_path / "raw.jsonl"
        w = ShardedAppender(q)
        w.write(rec(1))
        w.flush()
        assert not w._f.closed, "flush 는 닫지 않는다 — 둘은 다른 연산이다"
        w.close()
        assert w._f.closed


class TestRewriteInternals:
    """rewrite 경로의 내부 분기. 스윕에서 생존한 것들을 닫는다."""

    def test_stale_shard_removal_tolerates_already_gone_base(self, p):
        """base -> .00 rename 뒤 base 는 이미 없다. `unlink(missing_ok=True)` 가 그
        전제이고, False 로 바꾸면 분할이 일어나는 모든 rewrite 가 터진다."""
        n = _line_bytes(rec(1))
        out = write_jsonl_sharded(p, [rec(1), rec(1)], limit=2 * n - 1)
        assert out[0].name == "chunks.00.jsonl"
        assert not p.exists()
        assert list(iter_jsonl(p)) == [rec(1), rec(1)]

    def test_rewrite_returns_exactly_the_files_it_wrote(self, p):
        """반환 목록이 실제 물리 파일과 일치해야 소비자가 정리·검증을 할 수 있다."""
        out = write_jsonl_sharded(p, [rec(i, 400) for i in range(50)], limit=5_000)
        assert out == shard_paths(p)
        assert all(f.exists() for f in out)

    def test_shrink_unlinks_every_stale_shard(self, p):
        write_jsonl_sharded(p, [rec(i, 400) for i in range(300)], limit=2_000)
        many = shard_paths(p)
        assert len(many) > 9
        write_jsonl_sharded(p, [rec(0)], limit=10_000)
        assert shard_paths(p) == [p]
        assert not any(s.exists() for s in many if s != p), "구 shard 가 하나라도 남으면 손상 판정된다"


class TestExistenceIsAboutTheFileNotItsContents:
    """`jsonl_exists` 는 "파일이 있는가"이지 "내용이 있는가"가 아니다.

    적대 검증이 `return bool(shard_paths(path))` 를 `return bool(count_jsonl(path))` 로
    바꿨는데 40 건이 전부 통과했다(2026-08-05). 기존 검사가 레코드를 **쓴 뒤에만**
    존재를 봤기 때문이다.

    이 구분이 왜 계약인가. 호출부가 약 60 곳이고 전부 `if not jsonl_exists(x): skip`
    형태의 게이트다. 라인수 기반이 되면 "생산은 됐는데 0 건"과 "생산 자체가 안 됨"이
    같은 답을 내서 진단이 불가능해진다. 실제로 `check_dangling` 은 빈 edges.jsonl 팩을
    통째로 건너뛰게 되고, 적재기는 빈 nodes.jsonl 팩을 조용히 지나친다.
    생산자 쪽 계약(`test_empty_records_creates_empty_base`)이 빈 base 파일 생성을
    보장하므로 이 입력은 가정이 아니라 실재한다.
    """

    def test_empty_base_file_exists(self, p):
        write_jsonl_sharded(p, [])
        assert p.exists() and p.read_text(encoding="utf-8") == ""
        assert jsonl_exists(p) is True
        assert count_jsonl(p) == 0, "빈 파일은 존재하지만 0 건 — 둘은 다른 질문이다"

    def test_empty_shards_exist(self, p):
        """분할 상태에서도 같다. base 는 없고 shard 만 비어 있는 경우."""
        for i in range(2):
            p.with_name(f"chunks.{i:02d}.jsonl").write_text("", encoding="utf-8")
        assert not p.exists()
        assert jsonl_exists(p) is True
        assert count_jsonl(p) == 0

    def test_missing_does_not_exist(self, tmp_path):
        assert jsonl_exists(tmp_path / "none.jsonl") is False

    def test_corruption_propagates_rather_than_being_swallowed(self, p):
        """base+shard 공존은 `jsonl_exists` 를 통해서도 조용히 True 가 되면 안 된다."""
        p.write_text('{"id":0}\n', encoding="utf-8")
        p.with_name("chunks.00.jsonl").write_text('{"id":1}\n', encoding="utf-8")
        with pytest.raises(RuntimeError, match="base와 shard"):
            jsonl_exists(p)


class TestModuleLevelConstants:
    """모듈 최상단 **스칼라** 상수. 돌연변이 스윕이 오래 못 보던 사각지대였다.

    스윕이 컨테이너 리터럴 안만 훑어서, 표에 안 들어 있는 최상단 상수는 어느 축에도
    안 걸렸다(2026-08-05 발견). 아래 둘은 값 자체가 계약이다.
    """

    def test_max_shards_is_two_digits_worth(self):
        """`_MAX_SHARDS` 는 glob 패턴 `[0-9][0-9]` 와 **짝** 이다.

        100 이상이면 `chunks.100.jsonl` 이 만들어지는데 glob 이 그 파일을 못 봐서
        데이터가 조용히 사라진다. 두 값은 따로 못 움직인다.
        """
        assert _MAX_SHARDS == 100
        assert _shard_path(Path("x/chunks.jsonl"), _MAX_SHARDS - 1).name == "chunks.99.jsonl"

    def test_shard_limit_reads_the_documented_env_var(self, tmp_path):
        """env 변수 **이름** 이 운영 계약이다 — 오타가 나면 조용히 기본값으로 돌아간다.

        모듈을 reload 하면 이미 이 모듈을 import 한 다른 모듈(build 등)이 옛 객체를
        붙든 채 남아 테스트 순서에 따라 결과가 달라진다. 별도 프로세스에서 읽는다.
        """
        root = Path(jsonl_io_mod.__file__).resolve().parents[2]
        env = {**os.environ, "JSONL_SHARD_LIMIT": "12345",
               "PYTHONPATH": str(root)}
        r = subprocess.run(
            [sys.executable, "-c",
             "import opencrab.pack.jsonl_io as m; print(m.SHARD_LIMIT)"],
            cwd=root, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "12345"


class TestDefaultArguments:
    """공개 호출부의 기본 인자값 고정.

    `iter_jsonl_lines(missing_ok=False)` 변이가 살아남은 뒤에 둔다. 개별 행동 검사가
    본체이고(위 `test_iter_jsonl_lines_default_raises_on_missing` 등) 이 표는 **행동으로
    관측하기 어려운 나머지**를 덮는 그물이다. 표만 두면 기대값을 같이 고치는 순간
    통과하므로, 행동 검사를 대체하지 않는다.
    """

    EXPECTED = {
        "iter_jsonl_lines": {"missing_ok": False},
        "iter_jsonl": {"missing_ok": False},
        "write_jsonl_sharded": {"limit": None},
    }

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_function_defaults_are_pinned(self, name):
        fn = getattr(jsonl_io_mod, name)
        got = {k: v.default for k, v in inspect.signature(fn).parameters.items()
               if v.default is not inspect.Parameter.empty}
        assert got == self.EXPECTED[name]

    def test_appender_limit_default_is_none_meaning_module_limit(self):
        got = {k: v.default
               for k, v in inspect.signature(ShardedAppender.__init__).parameters.items()
               if v.default is not inspect.Parameter.empty}
        assert got == {"limit": None}


class TestHelpers:
    def test_sha_is_sha256_utf8(self):
        assert sha("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_sha_of_lone_surrogate_is_pinned(self):
        """`surrogatepass` 를 `replace` 로 바꿔도 길이 검사만으로는 안 잡혔다(변이 생존).

        sha() 는 노드 ID 생성기다. 카톡·카페 덤프처럼 lone surrogate 가 섞인 원천에서
        인코딩 정책이 바뀌면 **팩 전체의 id 가 조용히 갈린다**. 고정값으로 못박는다.
        """
        assert sha("\ud800") == hashlib.sha256(
            "\ud800".encode("utf-8", "surrogatepass")).hexdigest()
        assert sha("\ud800") != hashlib.sha256(
            "\ud800".encode("utf-8", "replace")).hexdigest()

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
