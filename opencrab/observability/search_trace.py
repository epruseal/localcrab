"""합성 검색 실행의 선택적 span과 시간선을 기록한다.

운영 입력과 예외 원문을 기록하지 않는다. 관측 오류는 검색 예외를 대체하지 않는다.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_sink: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("search_trace", default=None)
_stack: contextvars.ContextVar[tuple[int, ...]] = contextvars.ContextVar("search_spans", default=())
_meta: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("trace_meta", default={})
_request: contextvars.ContextVar[int | None] = contextvars.ContextVar("search_request", default=None)
_OMITTED = object()


def active() -> Trace | None:
    """현재 context의 관측기를 반환한다."""
    return _sink.get()


def _observation_failure(trace, error, original=None):
    if trace is not None:
        trace.failed = True
    if original is None and not isinstance(error, Exception):
        raise error


def _reset_tokens(tokens, trace, original=_OMITTED):
    if original is _OMITTED:
        original = sys.exception()
    first = None
    for variable, token in reversed(tokens):
        try:
            variable.reset(token)
        except BaseException as error:
            if first is None:
                first = error
    if first is not None:
        _observation_failure(trace, first, original)
        if original is None:
            raise first


class Trace:
    """상한이 있는 메모리 버퍼에 정제 이벤트를 보관한다."""

    def __init__(self, run_id: str, *, max_events: int = 100_000, mode: str = "general"):
        if not run_id or max_events < 1:
            raise ValueError("invalid trace configuration")
        if mode not in ("general", "ledger"):
            raise ValueError("invalid trace mode")
        self.run_id = run_id
        self.mode = mode
        self.max_events = max_events
        self.events: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.failed = False
        self.dropped = 0
        self._next = 0
        self._seq: dict[int, int] = {}
        self._lock = threading.Lock()

    def allocate(self) -> int:
        with self._lock:
            self._next += 1
            return self._next

    def emit(self, kind: str, operation: str, **fields: Any) -> None:
        """실패하거나 넘친 이벤트는 불완전 상태로 표시한다."""
        try:
            with self._lock:
                tid = threading.get_ident()
                seq = self._seq.get(tid, 0) + 1
                self._seq[tid] = seq
                if len(self.events) >= self.max_events:
                    self.dropped += 1
                    self.failed = True
                    return
                self.events.append({
                    "kind": kind, "operation": operation, "thread_id": tid,
                    "thread_seq": seq, "monotonic_ns": int(time.perf_counter() * 1e9),
                    "thread_cpu_ns": time.thread_time_ns(), "request_id": _request.get(),
                    **_meta.get(),
                    **fields,
                })
        except BaseException as error:
            _observation_failure(self, error)

    def sample(self, request_id: int | None, elapsed_ms: float, *, accepted: bool,
               phase: str, qid: str, attempt: int) -> None:
        """기존 타이머의 값을 span과 별도인 표본 원장에 넣는다."""
        try:
            if len(self.samples) >= self.max_events:
                self.failed = True
                return
            self.samples.append({"request_id": request_id, "elapsed_ms": elapsed_ms,
                                 "accepted": accepted, "phase": phase, "qid": qid,
                                 "attempt": attempt})
        except BaseException as error:
            _observation_failure(self, error)

    def document(self) -> dict[str, Any]:
        return {"schema_version": 1, "mode": self.mode, "run_id": self.run_id, "events": self.events,
                "samples": self.samples, "dropped": self.dropped,
                "status": "incomplete" if self.failed else "complete",
                "unobserved": ["sqlite_internal_wait", "native_commit", "network", "client"]}


@contextmanager
def observe(trace: Trace):
    """독립 스레드 root를 설치하고 기존 context를 복원한다."""
    tokens = []
    pending_error = None
    try:
        for variable, value in ((_sink, trace), (_stack, ()), (_request, None), (_meta, {})):
            tokens.append((variable, variable.set(value)))
        yield trace
    except BaseException as error:
        pending_error = error
        raise
    finally:
        _reset_tokens(tokens, trace, pending_error)


@contextmanager
def span(operation: str):
    """실제 호출 경계를 기록하되 관측 실패를 전파하지 않는다."""
    try:
        trace = active()
    except BaseException as error:
        try:
            trace = _sink.get()
        except BaseException:
            trace = None
        _observation_failure(trace, error)
        yield None
        return
    if trace is None:
        yield None
        return
    tokens = []
    pending_error = None
    body_error = None
    try:
        try:
            ident = trace.allocate()
            parents = _stack.get()
            parent = parents[-1] if parents else None
            tokens.append((_stack, _stack.set((*parents, ident))))
            trace.emit("start", operation, span_id=ident, parent_id=parent)
        except BaseException as error:
            _observation_failure(trace, error)
            yield None
            return
        status = "ok"
        try:
            yield ident
        except BaseException as exc:
            body_error = exc
            status = "cancelled" if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) else "error"
            raise
        finally:
            try:
                trace.emit("end", operation, span_id=ident, parent_id=parent, status=status)
            except BaseException as error:
                _observation_failure(trace, error, body_error)
    except BaseException as error:
        pending_error = error
        raise
    finally:
        _reset_tokens(tokens, trace, pending_error)


@contextmanager
def metadata(**fields):
    """합성 호출 메타데이터를 현재 scope에만 붙인다."""
    tokens = []
    trace = active()
    pending_error = None
    try:
        tokens.append((_meta, _meta.set({**_meta.get(), **fields})))
        yield
    except BaseException as error:
        pending_error = error
        raise
    finally:
        _reset_tokens(tokens, trace, pending_error)


class RequestTimer:
    """기존 하네스 시계값을 이벤트와 독립 표본에 전달한다."""

    def __init__(self):
        self.trace = active()
        self.ident = None
        self.parent = None
        self.token = None
        self.stack_token = None
        if self.trace is not None:
            try:
                self.ident = self.trace.allocate()
                parents = _stack.get()
                self.parent = parents[-1] if parents else None
                self.token = _request.set(self.ident)
                self.stack_token = _stack.set((*parents, self.ident))
            except BaseException as error:
                try:
                    self.close()
                finally:
                    _observation_failure(self.trace, error)

    def start(self, value):
        self.t0 = value
        if self.trace is not None and self.ident is not None:
            try:
                self.trace.emit("start", "request", span_id=self.ident, parent_id=self.parent,
                                monotonic_ns=int(value * 1e9), timer=value)
            except BaseException as error:
                _observation_failure(self.trace, error)

    def end(self, value, status="ok", *, original=_OMITTED):
        self.t1 = value
        if self.trace is not None and self.ident is not None:
            if original is _OMITTED:
                original = sys.exception()
            try:
                self.trace.emit("end", "request", span_id=self.ident, parent_id=self.parent,
                                monotonic_ns=int(value * 1e9), timer=value, status=status)
            except BaseException as error:
                _observation_failure(self.trace, error, original)

    def close(self, *, original=_OMITTED):
        tokens = []
        if self.token is not None:
            tokens.append((_request, self.token))
        if self.stack_token is not None:
            tokens.append((_stack, self.stack_token))
        _reset_tokens(tokens, self.trace, original)


@contextmanager
def request_span():
    """시험과 독립 호출에서 요청 식별자를 설치한다."""
    timer = RequestTimer()
    body_error = None
    pending_error = None
    status = "ok"
    try:
        try:
            timer.start(time.perf_counter())
            try:
                yield timer.ident
            except BaseException as error:
                body_error = error
                status = ("cancelled" if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
                          else "error")
                raise
            finally:
                try:
                    timer.end(time.perf_counter(), status, original=body_error)
                except BaseException as error:
                    _observation_failure(timer.trace, error, body_error)
        except BaseException as error:
            pending_error = error
            raise
    finally:
        timer.close(original=pending_error)


def decision(operation: str, reason: str, count: int = 0):
    """정적 분기 이유와 합성 개수를 기록한다."""
    trace = None
    try:
        trace = active()
        if trace is not None:
            parents = _stack.get()
            trace.emit("decision", operation, reason=reason, count=count,
                       parent_id=parents[-1] if parents else None)
    except BaseException as error:
        if trace is None:
            try:
                trace = _sink.get()
            except BaseException:
                pass
        _observation_failure(trace, error, None)


class SampleLedger:
    """span sink와 별도로 기존 타이머와 채택 상태를 보관한다."""

    def __init__(self, limit=100_000):
        self.rows = []
        self.limit = limit
        self.failed = False

    def append(self, **row):
        try:
            if len(self.rows) >= self.limit:
                self.failed = True
                return
            self.rows.append(row)
        except BaseException as error:
            _observation_failure(self, error)

    def classify(self, index, disposition):
        if index is not None:
            try:
                self.rows[index]["disposition"] = disposition
            except BaseException as error:
                _observation_failure(self, error)




def observed(operation: str):
    """함수의 실제 진입 경계에 span을 둔다. dispatch는 바꾸지 않는다."""
    def decorate(fn):
        @functools.wraps(fn)
        def call(*args, **kwargs):
            with span(operation):
                return fn(*args, **kwargs)
        return call
    return decorate


def quota_snapshot() -> dict[str, int]:
    """프로세스 cgroup의 집계 값만 읽는다. 요청 대기로 귀속하지 않는다."""
    try:
        group = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        path = Path("/sys/fs/cgroup") / group.lstrip("/") / "cpu.stat"
        return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}
    except (OSError, ValueError, IndexError):
        return {}


@contextmanager
def phase(operation: str):
    """phase 집계 quota를 기록하고 요청 대기로 귀속하지 않는다."""
    trace = active()
    before = {}
    if trace is not None:
        try:
            before = quota_snapshot()
        except BaseException as error:
            _observation_failure(trace, error)
    with span(operation):
        body_error = None
        try:
            yield
        except BaseException as error:
            body_error = error
            raise
        finally:
            if trace is not None:
                try:
                    after = quota_snapshot()
                    trace.emit("decision", operation, reason="cgroup_aggregate",
                               quota_delta={key: value - before.get(key, value)
                                            for key, value in after.items()})
                except BaseException as error:
                    _observation_failure(trace, error, body_error)


@contextmanager
def measured_lock(lock):
    """실제 잠금 진입 대기와 보유 구간을 분리한다."""
    acquired = False
    try:
        with span("write_lock_wait"):
            lock.__enter__()
            acquired = True
        with span("write_lock_hold"):
            yield
    except BaseException:
        if acquired:
            acquired = False
            if lock.__exit__(*sys.exc_info()):
                return
        raise
    finally:
        if acquired:
            lock.__exit__(None, None, None)


def validate(doc: dict[str, Any], *, expected_mode: str) -> list[str]:
    """이벤트 쌍, 부모 범위, 순서와 표본의 연결을 검증한다."""
    errors: list[str] = []
    if doc.get("schema_version") != 1:
        return ["schema"]
    if expected_mode not in ("general", "ledger") or doc.get("mode") != expected_mode:
        errors.append("mode")
    if doc.get("status") != "complete" or doc.get("dropped") != 0:
        errors.append("incomplete")
    starts: dict[int, dict[str, Any]] = {}
    ends: dict[int, dict[str, Any]] = {}
    seq: dict[int, int] = {}
    times: dict[int, int] = {}
    for event in doc.get("events", []):
        tid = event.get("thread_id")
        if event.get("thread_seq") != seq.get(tid, 0) + 1:
            errors.append("thread_sequence")
        seq[tid] = event.get("thread_seq", 0)
        now = event.get("monotonic_ns", -1)
        if now < times.get(tid, -1):
            errors.append("time_order")
        times[tid] = now
        kind, ident = event.get("kind"), event.get("span_id")
        if kind == "decision":
            continue
        if kind not in ("start", "end") or not isinstance(ident, int):
            errors.append("event_shape")
            continue
        table = starts if kind == "start" else ends
        if ident in table:
            errors.append("duplicate_span")
        table[ident] = event
    if starts.keys() != ends.keys():
        errors.append("unpaired")
    for ident in starts.keys() & ends.keys():
        start, end = starts[ident], ends[ident]
        if (start["operation"] != end["operation"] or start["thread_id"] != end["thread_id"]
                or start.get("parent_id") != end.get("parent_id")
                or end["monotonic_ns"] < start["monotonic_ns"]):
            errors.append("span_mismatch")
        parent = start.get("parent_id")
        if parent is not None:
            if parent not in starts or parent not in ends:
                errors.append("orphan")
            elif (starts[parent]["thread_id"] != start["thread_id"]
                  or starts[parent]["monotonic_ns"] > start["monotonic_ns"]
                  or ends[parent]["monotonic_ns"] < end["monotonic_ns"]):
                errors.append("parent_range")
    for event in doc.get("events", []):
        request_id = event.get("request_id")
        if request_id is not None and (
            request_id not in starts or starts[request_id]["operation"] != "request"
            or starts[request_id]["thread_id"] != event["thread_id"]
        ):
            errors.append("request_context")
    if expected_mode == "ledger":
        attempts = {ident: event for ident, event in starts.items() if event["operation"] == "attempt"}
        rows = doc.get("samples", [])
        row_ids = [row.get("attempt_id") for row in rows]
        if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(attempts):
            errors.append("attempt_samples")
        requests = {}
        for ident, event in starts.items():
            if event["operation"] != "request":
                continue
            parent = event.get("parent_id")
            visited = set()
            while parent is not None and parent not in attempts and parent not in visited:
                visited.add(parent)
                parent = starts.get(parent, {}).get("parent_id")
            if parent not in attempts:
                errors.append("request_attempt")
            else:
                requests.setdefault(parent, []).append(ident)
        for row in rows:
            attempt_id = row.get("attempt_id")
            start = attempts.get(attempt_id)
            end = ends.get(attempt_id)
            if start is None or end is None:
                continue
            for key in ("phase", "qid", "shape", "repeat", "attempt"):
                if key not in row or row[key] != start.get(key) or row[key] != end.get(key):
                    errors.append("attempt_metadata")
            status = row.get("status")
            disposition = row.get("disposition")
            phase = row.get("phase")
            accepted = row.get("accepted_for_metric")
            if status != end.get("status") or status not in ("ok", "error", "cancelled") or not isinstance(accepted, bool):
                errors.append("attempt_status")
            if status == "ok":
                allowed = {"cold": ("cold",), "warm": ("accepted", "discarded", "excluded_warmup"),
                           "concurrent": ("accepted",)}
                if disposition not in allowed.get(phase, ()) or accepted != (disposition == "accepted"):
                    errors.append("attempt_disposition")
            elif disposition != status:
                errors.append("attempt_disposition")
            children = requests.get(attempt_id, [])
            ident = row.get("request_id")
            if len(children) > 1 or (children and ident != children[0]) or (not children and (ident is not None or status == "ok")):
                errors.append("sample_request")
                continue
            if children:
                req_start = starts[ident]
                req_end = ends.get(ident, {})
                if status == "ok" and req_end.get("status") != "ok":
                    errors.append("request_status")
                if req_end.get("status") not in ("ok", "error", "cancelled"):
                    errors.append("request_status")
                t0, t1 = row.get("t0"), row.get("t1")
                if (not isinstance(t0, (int, float)) or isinstance(t0, bool)
                        or not isinstance(t1, (int, float)) or isinstance(t1, bool)
                        or t0 != req_start.get("timer") or t1 != req_end.get("timer")
                        or row.get("elapsed_ms") != (t1 - t0) * 1000 or t1 < t0):
                    errors.append("sample_time")
            elif any(key in row for key in ("t0", "t1", "elapsed_ms")):
                errors.append("sample_time")
    else:
        for sample in doc.get("samples", []):
            ident = sample.get("request_id")
            if ident not in starts or starts[ident]["operation"] != "request":
                errors.append("sample_request")
    return sorted(set(errors))


def timeline(doc: dict[str, Any], *, expected_mode: str) -> str:
    """검증한 정제 이벤트만 텍스트 시간선으로 보여 준다."""
    errors = validate(doc, expected_mode=expected_mode)
    if errors:
        raise ValueError(",".join(errors))
    return "\n".join(
        f"{e['monotonic_ns']} thread={e['thread_id']} span={e.get('span_id')} "
        f"parent={e.get('parent_id')} {e['kind']} {e['operation']}"
        for e in sorted(doc["events"], key=lambda e: (e["monotonic_ns"], e["thread_id"], e["thread_seq"]))
    )


class TraceFiles:
    """새 출력 디렉터리의 pending/terminal 계약을 관리한다."""

    def __init__(self, root: Path, *, run_id: str | None = None, nonce: str | None = None):
        # 경로 보호는 하네스 check_write_paths가 이 생성자 전에 수행한다.
        root.mkdir(parents=False, exist_ok=False)
        self.root = root
        self.pending = root / "pending.json"
        self.run_id = run_id or root.name
        self.owner = nonce or os.urandom(16).hex()
        with self.pending.open("x", encoding="utf-8") as fh:
            json.dump({"status": "pending", "run_id": self.run_id, "owner": self.owner}, fh)
            fh.flush()
            os.fsync(fh.fileno())

    def finish(self, trace: Trace, ledger: SampleLedger | None = None, *, expected_mode: str,
               cleanup: bool = False, observation: dict[str, bool] | None = None) -> bool:
        try:
            cleanup = cleanup and observation is not None and all(
                observation.get(key) is True for key in ("watchdog", "slots", "writer")
            )
            started = time.perf_counter_ns()
            cpu = time.thread_time_ns()
            doc = trace.document()
            if ledger is not None:
                doc["samples"] = ledger.rows
                if ledger.failed:
                    doc["status"] = "incomplete"
            doc["owner"] = self.owner
            payload = json.dumps(doc, ensure_ascii=False).encode()
            path = self.root / "trace.json"
            with path.open("xb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            terminal = {"status": "complete" if cleanup and not validate(doc, expected_mode=expected_mode) else "incomplete",
                        "run_id": self.run_id, "owner": self.owner, "cleanup": cleanup,
                        "events": len(doc["events"]), "samples": len(doc["samples"]),
                        "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                        "flush_ns": time.perf_counter_ns() - started,
                        "flush_cpu_ns": time.thread_time_ns() - cpu}
            temp = self.root / "terminal.tmp"
            with temp.open("x", encoding="utf-8") as fh:
                json.dump(terminal, fh)
                fh.flush()
                os.fsync(fh.fileno())
            temp.rename(self.root / "terminal.json")
            return terminal["status"] == "complete"
        except BaseException as error:
            _observation_failure(trace, error)
            return False


def read_trace(root: Path, *, expected_mode: str, expected_run_id: str | None = None,
               expected_nonce: str | None = None) -> dict[str, Any]:
    """terminal과 checksum이 없는 접두 자료는 불완전으로 거부한다."""
    if not (root / "pending.json").is_file() or not (root / "terminal.json").is_file():
        raise ValueError("incomplete")
    for name in ("pending.json", "terminal.json", "trace.json"):
        path = root / name
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("trace_size_or_link")
    raw = (root / "trace.json").read_bytes()
    terminal = json.loads((root / "terminal.json").read_text())
    if (terminal.get("status") != "complete" or terminal.get("cleanup") is not True
            or terminal.get("sha256") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("incomplete")
    doc = json.loads(raw)
    pending = json.loads((root / "pending.json").read_text())
    if ((expected_run_id is not None and terminal.get("run_id") != expected_run_id)
            or (expected_nonce is not None and terminal.get("owner") != expected_nonce)
            or doc.get("run_id") != terminal.get("run_id")
            or pending.get("run_id") != terminal.get("run_id")
            or pending.get("owner") != terminal.get("owner")
            or doc.get("owner") != terminal.get("owner")
            or len(doc.get("events", [])) != terminal.get("events")
            or len(doc.get("samples", [])) != terminal.get("samples")):
        raise ValueError("terminal_mismatch")
    if validate(doc, expected_mode=expected_mode):
        raise ValueError("invalid_trace")
    return doc
