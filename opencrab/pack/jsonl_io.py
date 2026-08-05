"""
공용 JSONL 입출력 헬퍼 + 분할저장(shard) 계층.

배경(2026-07-15): GitHub 50MB 경고/100MB 하드리밋 대응. 논리 파일 하나
(예: chunks.jsonl)를 바이트 상한(SHARD_LIMIT, 기본 40MB)으로 물리 분할하되,
생산·소비 코드는 "논리적 단일 스트림"으로 다룬다.

물리 스킴 (loud-fail 설계):
  - 미분할: chunks.jsonl 단일 파일 그대로.
  - 분할:   chunks.00.jsonl, chunks.01.jsonl, ... (zero-pad 2자리, base 파일 제거).
    미개조 소비자가 base만 읽고 일부 데이터만 조용히 얻는 silent partial read를
    FileNotFoundError로 즉사시키기 위해, 분할 시 base 이름의 파일은 존재하지 않는다.
  - base와 shard가 동시에 존재하면 손상 상태로 보고 RuntimeError.

라인은 절대 쪼개지 않는다(라인 1개가 상한을 넘어도 그대로 기록).
기존 write_jsonl 호출자는 write_jsonl_sharded로 자동 위임되어 무료 전환된다.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

SHARD_LIMIT = int(os.environ.get("JSONL_SHARD_LIMIT", 40 * 1024 * 1024))
_MAX_SHARDS = 100  # 2자리 zero-pad — 논리 파일당 최대 ~4GB(40MB×100)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def slug(value: str) -> str:
    out = [c if c.isalnum() else "-" for c in value.lower().strip()]
    return "-".join("".join(out).strip("-").split("-"))[:80] or "unknown"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── shard 경로 계층 ──────────────────────────────────────────

def _shard_path(path: Path, idx: int) -> Path:
    assert idx < _MAX_SHARDS, f"shard 개수 한도 초과({idx}) — 상한/스킴 재검토 필요: {path}"
    return path.with_name(f"{path.stem}.{idx:02d}{path.suffix}")


def shard_paths(path: Path | str) -> list[Path]:
    """논리 경로 → 실제 물리 파일 목록.
    base만 → [base] / shards만 → 정렬된 shard들 / 둘 다 → RuntimeError / 없음 → []."""
    path = Path(path)
    shards = sorted(path.parent.glob(f"{path.stem}.[0-9][0-9]{path.suffix}"))
    if path.exists() and shards:
        raise RuntimeError(f"base와 shard가 동시에 존재(부분 마이그레이션/롤백 잔재): {path}")
    if path.exists():
        return [path]
    return shards


def jsonl_exists(path: Path | str) -> bool:
    return bool(shard_paths(path))


def count_jsonl(path: Path | str) -> int:
    """전 shard 라인수 합 (wc -l 동등, 스트리밍)."""
    return sum(sum(1 for _ in open(p, encoding="utf-8")) for p in shard_paths(path))


def logical_sha256(path: Path | str) -> str:
    """전 shard 바이트를 순서대로 연결한 sha256 — 마이그레이션 무손실 검증용."""
    h = hashlib.sha256()
    for p in shard_paths(path):
        with open(p, "rb") as f:
            # 블록 크기는 **결과에 영향이 없다**(2026-08-05 측정: 4,000행 다중 shard 에서
            # 1<<20 / 2<<20 / 1<<21 / 1<<3 전부 같은 sha). 스트리밍 해시라 청크 경계가
            # 달라도 바이트 열이 같기 때문이다. 성능 상수일 뿐 계약이 아니다.
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


def iter_jsonl_lines(path: Path | str, missing_ok: bool = False) -> Iterator[str]:
    """파싱 없는 raw 라인 스트림(개행 제거, 빈 줄 포함) — 자체 파싱/오류집계 소비자용."""
    paths = shard_paths(path)
    if not paths and not missing_ok:
        raise FileNotFoundError(f"jsonl 없음(단일/분할 모두): {path}")
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                yield line.rstrip("\n")


def iter_jsonl(path: Path | str, missing_ok: bool = False) -> Iterator[dict]:
    """논리 스트림 리더 — 단일 파일/분할 모두 투명 흡수, 빈 줄 skip."""
    for line in iter_jsonl_lines(path, missing_ok=missing_ok):
        if line.strip():
            yield json.loads(line)


# ── writer ──────────────────────────────────────────────────

class ShardedAppender:
    """shard-aware append writer (context manager).

    마지막 물리 파일에 이어 쓰고, 상한 초과 시 롤오버한다:
    현재 파일이 base형이면 base → {stem}.00{suffix} rename(기존 바이트 무변경) 후
    다음 번호 shard를 새로 연다. 기존 내용을 재분할하지 않으므로
    poolpcon-talk '전량 재빌드 금지' 제약과 정합.
    """

    def __init__(self, path: Path | str, limit: int = None):
        self.logical = Path(path)
        self.limit = limit if limit is not None else SHARD_LIMIT
        paths = shard_paths(self.logical)
        self.current = paths[-1] if paths else self.logical
        self.size = self.current.stat().st_size if self.current.exists() else 0
        self.logical.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.current, "a", encoding="utf-8")

    def _rollover(self):
        # close() 를 지워도 결과는 같다(2026-08-05 측정: 20KB 버퍼 잔류 상태에서도 첫 shard
        # 내용 동일). rename 이 inode 를 따라가고 `self._f` 재대입 시 옛 객체가 같은 inode 로
        # flush 되기 때문이다. **그래도 명시적으로 닫는다** — 그 등가성은 CPython 의 참조계수
        # 즉시 해제에 기대는 것이고, 다른 구현·다른 GC 타이밍에서는 성립하지 않는다.
        self._f.close()
        if self.current == self.logical:                     # base형 → .00 rename
            renamed = _shard_path(self.logical, 0)
            self.current.rename(renamed)
            self.current = renamed
        # maxsplit=1 이 계약이다. 논리 이름에 점이 있으면(chunks.v1.jsonl) 분할 stem 이
        # `chunks.v1.03` 이 되고 rsplit(".", 2)[1] 은 'v1' 을 집어 int() 에서 터진다.
        idx = int(self.current.stem.rsplit(".", 1)[1]) + 1
        self.current = _shard_path(self.logical, idx)
        self._f = open(self.current, "a", encoding="utf-8")
        self.size = 0

    def write_line(self, line: str):
        """개행 미포함 직렬화 라인 1개 기록."""
        nbytes = len(line.encode("utf-8")) + 1
        if self.size > 0 and self.size + nbytes > self.limit:
            self._rollover()
        self._f.write(line + "\n")
        self.size += nbytes

    def write(self, rec) -> None:
        self.write_line(json.dumps(rec, ensure_ascii=False))

    def flush(self):
        self._f.flush()

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def write_jsonl_sharded(path: Path | str, records, limit: int = None) -> list[Path]:
    """전량 rewrite (기존 open('w') 대체). 총량이 상한 이하면 base 단일 파일,
    초과면 전부 번호 shard로 기록하고 base·잉여 구 shard를 제거한다.
    반환: 기록된 물리 파일 목록.

    주의: str 레코드는 이미 직렬화된 JSON 라인으로 간주해 그대로 기록한다
    (dict만 json.dumps — 구 write_jsonl은 str도 dumps했음). crash 내성은
    구 open('w')와 동일 수준(비원자적) — rewrite 생산자는 소스에서 전량
    재생성 가능하므로 재실행이 복구 경로다."""
    path = Path(path)
    limit = limit if limit is not None else SHARD_LIMIT
    path.parent.mkdir(parents=True, exist_ok=True)
    old = set(path.parent.glob(f"{path.stem}.[0-9][0-9]{path.suffix}")) | (
        {path} if path.exists() else set())

    written: list[Path] = []
    cur, size, f = path, 0, None

    def _open(p: Path):
        nonlocal f
        f = open(p, "w", encoding="utf-8")
        written.append(p)

    _open(cur)
    try:
        for rec in records:
            line = rec if isinstance(rec, str) else json.dumps(rec, ensure_ascii=False)
            nbytes = len(line.encode("utf-8")) + 1
            if size > 0 and size + nbytes > limit:
                f.close()
                if cur == path:                               # base형 → .00 rename
                    renamed = _shard_path(path, 0)
                    cur.rename(renamed)
                    written[0] = renamed
                    cur = renamed
                cur = _shard_path(path, int(cur.stem.rsplit(".", 1)[1]) + 1)
                _open(cur)
                size = 0
            f.write(line + "\n")
            size += nbytes
    finally:
        f.close()

    for stale in old - set(written):
        stale.unlink(missing_ok=True)  # base→.00 rename된 경우 base는 이미 없음
    return written


def write_jsonl(path: Path, records: list) -> None:
    """기존 API 호환 — shard-aware rewrite로 위임."""
    write_jsonl_sharded(path, records)
