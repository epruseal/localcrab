#!/usr/bin/env python
"""모듈 하나 또는 팩 계층 전체에 대한 전면 기계 돌연변이 — 테스트의 검출력을 수치로 잰다.

**왜 있는가.** 2026-08-04 이 계층을 이관하며 적대 검증을 9라운드 돌렸는데, 매 라운드
새 결함이 나왔다. 원인은 코드가 아니라 방법이었다 — 검증자가 지적한 지점만 손으로
막으니 **인접한 같은 클래스**가 다음 라운드에 다시 나왔다. 7라운드에서 이 도구로
전면을 훑자 215종 중 생존 39종이 한 번에 드러났고, 그것이 두 클래스
("기존 유효값을 덮지 않는가", "유효값 튜플의 모든 원소를 존중하는가")로 수렴했다.
지적 대응이 아니라 **클래스 폐쇄**로 바꾼 것이 루프를 끊었다.

**9라운드에서 이 도구 자신의 결함 두 종류가 드러났다(2026-08-05).**

- *RC1 — 소급 미적용.* 도구를 만든 뒤 "그때 내가 만진 모듈"만 돌렸다. 먼저 이관된
  ``jsonl_io.py``·``build.py`` 는 도구가 없던 시절 옮겨졌고 그 뒤로 **한 번도 스윕되지
  않았다**. 첫 스윕이 곧바로 생존 21종을 냈다 — 누적 잔량이다. 그래서 대상을 사람이
  고르지 못하게 ``--all`` 과 :data:`PACK_SUITES` 를 둔다. 등록되지 않은 모듈이
  ``opencrab/pack/`` 에 생기면 ``--all`` 이 **실패**한다.
- *RC2 — 조용한 0.* ``_module_functions`` 가 ``tree.body`` 만 봐서 클래스 메서드를 하나도
  못 모았고, 그 결과 ``build.py`` 의 호출대상 변이가 **0** 으로 보고됐다. 사람은 그 0을
  "대상 없음"으로 읽는다. "수집 실패"와 "대상 없음"이 같은 숫자로 나오면 안 된다.
  이 리포가 ``check_shard_guards.py`` 까지 만들어 막아온 클래스를 검사 도구가 저지르고
  있었다. 아래 네 자리를 전부 고쳤다: 클래스 메서드 수집, 클래스 본문 표 수집,
  적용불가 op 라벨 출력, KILLED/BROKEN 분리.

변이 대상: 비교 연산자, not, and/or, ``.get(k, d)``·``.setdefault(k, d)`` 의 기본값, 상수,
문장 삭제, 모듈·클래스 최상단 표 리터럴, 호출 대상(모듈 함수 / 같은 클래스의 메서드).
docstring 과 f-string 의 **리터럴 텍스트** 는 동작이 아니라 제외한다.

**함수 기본 인자값은 이미 대상이다.** ``ast.walk(fn)`` 이 ``fn.args.defaults`` 까지 돌기
때문에 ``missing_ok: bool = False`` 는 ``const:False->True`` 로 생성된다(9라운드에서
"mutator 에 기본값 축을 추가하라"는 처방이 나왔으나 측정해 보니 오진이었다 — 이미
생성되고 있었고 **테스트가 없어서 생존**했다). 진단을 그대로 따랐으면 중복 오퍼레이터만
늘고 진짜 원인인 테스트 부재는 그대로 남았을 것이다.

**복합(두 위치 동시) 변이는 하지 않는다.** n 개 변이의 쌍은 n²/2 이고 normalize 기준
812² /2 ≈ 33 만 회의 pytest 실행이라 현실적이지 않다. 단일 변이가 전부 죽는 상태에서
쌍이 살아남는 경우(서로 상쇄하는 변이)는 실제로는 드물다. **안 한다고 여기 적어 둔다 —
"전부 훑었다"고 읽히면 안 된다.**

판정 네 갈래:
  KILLED   테스트가 실패했다(pytest rc=1). 계약이 실제로 검사하고 있다.
  BROKEN   모듈이 뜨지도 못했다(rc>=2, 수집 에러). 검출은 됐지만 **계약 검증이 아니다** —
           검출력 지표에서 분리해야 "N 종 KILLED" 가 과대평가되지 않는다.
  HUNG     제한 시간(RUN_TIMEOUT) 안에 안 끝났다. 무한 루프·메모리 폭증. 역시 계약
           검증이 아니고, 무엇보다 **분리해 세지 않으면 스윕이 통째로 멈춘 걸 모른다.**
  SURVIVED 미검사 경로(고쳐라) 또는 등가 변이(추론하지 말고 입력 격자로 차분 0 을
           **측정해** 등가임을 보이고, 그 전제를 불변식 테스트로 못박아라).

사용:
    python scripts/qa/mutate_module.py <리포루트> --all [결과.json]
    python scripts/qa/mutate_module.py <리포루트> <모듈> <테스트>[,<테스트>...] [결과.json]
예:
    python scripts/qa/mutate_module.py . opencrab/pack/normalize.py \
        tests/test_pack_normalize.py /tmp/sweep.json
"""
import ast
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# 모듈 -> 그 모듈의 계약을 검사하는 테스트. `--all` 이 이 표를 쓴다.
#
# **이 표가 RC1(소급 미적용)의 폐쇄 장치다.** 사람이 "이번에 만진 모듈"만 고르는 경로를
# 없애려고 둔다. `opencrab/pack/` 에 모듈이 새로 생겼는데 여기 없으면 `--all` 이 죽는다.
PACK_SUITES: dict[str, tuple[str, ...]] = {
    "opencrab/pack/normalize.py": ("tests/test_pack_normalize.py",),
    "opencrab/pack/schema.py": ("tests/test_pack_schema.py",),
    "opencrab/pack/jsonl_io.py": ("tests/test_pack_jsonl_io.py",),
    "opencrab/pack/build.py": ("tests/test_pack_build.py",),
    "opencrab/pack/assembler.py": ("tests/test_pack_assembler.py",),
    "opencrab/pack/neo4j_export.py": ("tests/test_pack_neo4j_export.py",),
}

PY = sys.executable

# 변이 1건당 테스트 실행 상한(초). 정상 스위트는 1 초 미만이라 200 배 여유다.
#
# **왜 필요한가.** 상한이 없으면 스윕이 조용히 영원히 멈춘다. 실측(2026-08-05):
# `ShardedAppender.write` 안의 `self.write_line(...)` 를 `self.write(...)` 로 바꾸는
# 변이가 무한 재귀에 빠지는데, 매 단계 문자열을 재직렬화해 RecursionError 전에 메모리가
# 폭증한다. 스윕이 6 분간 같은 지점에 멈춰 있었고 아무 신호도 없었다 — 이 도구가 닫으려는
# "조용한 실패" 클래스를 도구 자신이 저지르고 있었다(RC2 와 같은 부류).
RUN_TIMEOUT = 180

# pytest 가 쓰지 않는 반환값. 시간 초과를 rc 로 흘려보낸다.
_HUNG = -9

# 산술 연산자 뒤집기 표.
ARITH_FLIP = {ast.Add: ast.Sub, ast.Sub: ast.Add,
              ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult,
              ast.LShift: ast.RShift, ast.RShift: ast.LShift,
              ast.BitOr: ast.BitAnd, ast.BitAnd: ast.BitOr}


def _pos(n):
    """노드 고유 위치. `a.get(x,{}).get(y,())` 처럼 중첩된 호출은 col_offset 이 같아
    시작 위치만으로는 구분되지 않는다 — 끝 위치까지 포함해야 안쪽/바깥쪽이 갈린다.
    (이 맹점 때문에 실제로 비등가 변이 하나가 검사되지 않고 지나갔다.)"""
    return (type(n).__name__, getattr(n, "lineno", -1), getattr(n, "col_offset", -1),
            getattr(n, "end_lineno", -1), getattr(n, "end_col_offset", -1))


class _Op:
    """(설명, 원본노드 위치, 변형함수) 를 담는다."""

    def __init__(self, kind, node, apply):
        self.kind, self.node, self.apply = kind, node, apply
        self.line = getattr(node, "lineno", 0)

    def label(self):
        return f"{self.kind}@L{self.line}"


def _message_text_nodes(tree):
    """f-string 의 **리터럴 텍스트** 조각. 진단 메시지 문구는 동작이 아니다.

    문구를 바꿔도 예외 종류·발생 여부가 같으므로 "생존"으로 세면 잡음만 늘고 진짜 결함이
    묻힌다(2026-08-05 schema 스윕에서 22종이 전부 이 부류였다). f-string **안의 식**
    (`row.get("id")` 같은 것)은 동작일 수 있으므로 제외하지 않는다.
    """
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for part in n.values:
                if isinstance(part, ast.Constant):
                    out.add((part.lineno, part.col_offset,
                             part.end_lineno, part.end_col_offset))
    return out


def _annotation_nodes(tree):
    """타입 어노테이션 안의 노드 위치. 동작이 아니라 변이 대상에서 뺀다.

    `from __future__ import annotations` 아래에서 어노테이션은 문자열로만 남아 런타임에
    평가되지 않는다. 그래서 `Path | str` -> `Path & str` 같은 변이가 **전부 생존**하는데
    (실측 2026-08-05: jsonl_io 8건 + schema 1건), 이건 미검사가 아니라 잡음이다.
    잡음이 쌓이면 진짜 생존자가 묻힌다 — 산술 축을 새로 넣으면서 같이 딸려온 것이다.
    """
    out = set()
    for n in ast.walk(tree):
        holders = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            holders = [a.annotation for a in
                       (*n.args.args, *n.args.kwonlyargs, *n.args.posonlyargs)
                       if a.annotation] + ([n.returns] if n.returns else [])
        elif isinstance(n, ast.AnnAssign) and n.annotation:
            holders = [n.annotation]
        for h in holders:
            for sub in ast.walk(h):
                out.add(_pos(sub))
    return out


def _docstring_nodes(tree):
    """docstring 은 동작이 아니다 — 변이 대상에서 뺀다(잡음 제거)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                out.add((body[0].lineno, body[0].col_offset))
                out.add((body[0].value.lineno, body[0].value.col_offset))
    return out


def _toplevel_constants(tree, skip):
    """모듈 **및 클래스** 최상단 대입에 등장하는 모든 상수.

    함수 본문만 훑으면 표의 **내용**이 무검사로 남는다 — 적대 검증이
    `"HAS_ASSEMBLY": "part_of"` -> `"related_to"` 한 글자로 실증했다(2026-08-05).
    표는 판정의 절반이므로 같은 스윕에 넣는다.

    **여기서 세 번 범위를 넓혔다(전부 "조용한 0" 이었다, RC2).**

    1. *클래스 본문.* 예전에는 `tree.body` 만 봐서 `class X: TABLE = {...}` 이 통째로
       0 으로 보고됐다. 그 0 은 "표가 없다"가 아니라 "안 봤다"였다.
    2. *컨테이너 밖의 스칼라.* 예전에는 Dict/Set/List/Tuple **안**만 봤다. 그래서
       `_MAX_SHARDS = 100`(세 자리 shard 를 glob 이 못 보게 막는 **유일한** 방어선),
       `SHARD_LIMIT = int(os.environ.get("JSONL_SHARD_LIMIT", ...))`(env 이름이 계약),
       `_NS = uuid.UUID('6ba7b810-...')`(바뀌면 **전 팩의 uid 가 갈린다**)가 전부
       무검사로 남았다. 최상단 대입은 어느 것이든 모듈 상태다.
    3. *이름 필터 제거.* UPPER_CASE 만 보면 `__all__` 같은 계약이 빠진다. 최상단
       대입을 이름으로 거르는 근거가 없다 — 값에 상수가 없으면 op 도 안 생긴다.

    중복 제거는 필수다. `ast.walk` 로 중첩 컨테이너를 다시 걸으면 같은 상수가 여러 번
    수집되고, 그러면 **같은 변이를 여러 번 돌린 것이 총 변이 수로 보고된다**
    (구 구현 실측: normalize 593 append / 고유 353 — 240 건이 중복이었다).
    """
    holders = [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    out, seen = [], set()
    for holder in holders:
        for node in holder.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            for c in ast.walk(node):
                if not isinstance(c, ast.Constant):
                    continue
                if (c.lineno, c.col_offset) in skip or _pos(c) in seen:
                    continue
                seen.add(_pos(c))
                out.append(c)
    return out


def _module_functions(tree):
    """최상단 함수명 -> 위치인자 개수. 호출 대상 교체용(`foo(...)` 형태)."""
    return {n.name: len(n.args.args) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _class_methods(tree):
    """ClassDef -> {메서드명: self 를 뺀 위치인자 개수}. `self.foo(...)` 교체용.

    **RC2 의 진원지.** 예전 `_module_functions` 는 `tree.body` 만 봐서 클래스 메서드를
    하나도 못 모았고, 전부 메서드로 이뤄진 `build.py` 는 호출대상 변이가 0 으로
    보고됐다. 0 이 "대상 없음"인지 "수집 실패"인지 구분되지 않는 게 문제였다.
    """
    out = {}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        methods = {}
        for m in cls.body:
            if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # staticmethod 는 self 를 안 받는다 — 일괄 -1 하면 arity 가 하나씩 밀려
            # 교체 후보가 엉뚱하게 매칭되거나 통째로 비어 **또 0 이 된다**.
            static = any(isinstance(d, ast.Name) and d.id == "staticmethod"
                         for d in m.decorator_list)
            n = len(m.args.args) - (0 if static else 1)
            if n >= 0:
                methods[m.name] = n
        out[cls] = methods
    return out


def _collect(tree):
    ops = []
    skip = _docstring_nodes(tree)
    msgs = _message_text_nodes(tree)
    anns = _annotation_nodes(tree)
    funcs = _module_functions(tree)

    # 0-a. 최상단 상수 변이(모듈 + 클래스). 표 리터럴 안팎을 모두 포함한다.
    for c in _toplevel_constants(tree, skip):
        if (c.lineno, c.col_offset, c.end_lineno, c.end_col_offset) in msgs:
            continue
        v = c.value
        repl = None
        if isinstance(v, str) and v:
            repl = v + "_MUT"
        elif v is True:
            repl = False
        elif v is False:
            repl = True
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            repl = v + 1
        if repl is not None:
            ops.append(_Op(f"top-const:{v!r}->{repl!r}", c,
                           lambda n, r=repl: setattr(n, "value", r)))

    # 0-b. 호출 대상 교체 — 모듈 함수(`foo(...)`)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in funcs):
            arity = len(node.args)
            for other, oarity in funcs.items():
                if other != node.func.id and oarity == arity:
                    ops.append(_Op(f"call-target:{node.func.id}->{other}", node,
                                   lambda n, o=other: setattr(n.func, "id", o)))

    # 0-c. 호출 대상 교체 — 같은 클래스의 메서드(`self.foo(...)`)
    for cls, methods in _class_methods(tree).items():
        for node in ast.walk(cls):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and node.func.attr in methods):
                continue
            arity = len(node.args)
            for other, oarity in methods.items():
                if other != node.func.attr and oarity == arity:
                    ops.append(_Op(
                        f"self-call:{node.func.attr}->{other}", node,
                        lambda n, o=other: setattr(n.func, "attr", o)))

    # 함수 본문 안. `ast.walk(fn)` 은 `fn.args.defaults` 도 도므로 **기본 인자값은 아래
    # 상수 축이 이미 잡는다**(별도 축이 필요하다는 진단은 오진이었다, 2026-08-05 측정).
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            # 1. 비교 연산자 뒤집기
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                flip = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
                        ast.In: ast.NotIn, ast.NotIn: ast.In,
                        ast.Is: ast.IsNot, ast.IsNot: ast.Is,
                        ast.Lt: ast.GtE, ast.GtE: ast.Lt,
                        ast.Gt: ast.LtE, ast.LtE: ast.Gt}
                t = type(node.ops[0])
                if t in flip:
                    ops.append(_Op(f"cmp:{t.__name__}->{flip[t].__name__}", node,
                                   lambda n, f=flip[t]: setattr(n, "ops", [f()])))
            # 2. not 제거
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                ops.append(_Op("drop-not", node,
                               lambda n: n.__dict__.update(
                                   {"op": ast.UAdd(), "operand": n.operand})))
            # 3. and <-> or
            if isinstance(node, ast.BoolOp):
                other = ast.Or if isinstance(node.op, ast.And) else ast.And
                ops.append(_Op(f"boolop->{other.__name__}", node,
                               lambda n, o=other: setattr(n, "op", o())))
                # or 의 오른쪽(기본값) 제거: `X or {}` -> `X`
                if isinstance(node.op, ast.Or) and len(node.values) == 2:
                    ops.append(_Op("drop-or-default", node,
                                   lambda n: n.__dict__.update(
                                       {"values": [n.values[0], n.values[0]]})))
            # 4. .get(k, default) -> .get(k)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("get", "setdefault") and len(node.args) == 2):
                ops.append(_Op(f"drop-{node.func.attr}-default", node,
                               lambda n: setattr(n, "args", n.args[:1])))
            # 5. 예외 타입 교체 — `raise X(...)` 의 X 를 바꿔 except 절 계층을 흔든다.
            #    PackSchemaError 가 ValueError 하위라는 계약처럼, 잡히는 쪽이 계약이다.
            if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)):
                cur = node.exc.func.id
                alt = "RuntimeError" if cur != "RuntimeError" else "ValueError"
                ops.append(_Op(f"raise:{cur}->{alt}", node.exc,
                               lambda n, a=alt: setattr(n.func, "id", a)))
            # 6. 키워드 인자 이름 교체 — 같은 호출 안의 다른 키워드로 바꿔 배선을 흔든다.
            if isinstance(node, ast.Call) and len(node.keywords) >= 2:
                names = [k.arg for k in node.keywords if k.arg]
                if len(names) >= 2:
                    ops.append(_Op(f"kwarg:{names[0]}<->{names[1]}", node,
                                   lambda n: _swap_kwargs(n)))
            # 6-b. 산술 연산자 뒤집기(AugAssign / BinOp).
            #
            # **적대 검증이 이 축의 부재를 비등가 생존 4건으로 실증했다**(2026-08-05).
            # `stray[k] += 1` -> `-= 1` 로 바꾸면 진단이 "비구조 키 3종 **-9건**" 을
            # 출력하는데 82 건이 전부 통과했다. Counter 가 음수여도 truthy 라 차단은
            # 유지되지만 운영자가 보는 건수의 부호가 뒤집힌다.
            if isinstance(node, (ast.AugAssign, ast.BinOp)) and _pos(node) not in anns:
                t = type(node.op)
                if t in ARITH_FLIP:
                    tag = "aug" if isinstance(node, ast.AugAssign) else "bin"
                    ops.append(_Op(f"{tag}:{t.__name__}->{ARITH_FLIP[t].__name__}", node,
                                   lambda n, o=ARITH_FLIP[t]: setattr(n, "op", o())))
            # 7. 슬라이스 경계 이동
            if isinstance(node, ast.Slice):
                for fld in ("lower", "upper"):
                    tgt = getattr(node, fld, None)
                    if isinstance(tgt, ast.Constant) and isinstance(tgt.value, int):
                        ops.append(_Op(f"slice-{fld}:{tgt.value}->{tgt.value + 1}", tgt,
                                       lambda n: setattr(n, "value", n.value + 1)))
            # 8. dict comprehension 의 키와 값 교환
            if isinstance(node, ast.DictComp):
                ops.append(_Op("dictcomp-swap-key-value", node,
                               lambda n: n.__dict__.update(
                                   {"key": n.value, "value": n.key})))
            # 9. 상수 뒤집기(기본 인자값 포함 — walk 가 args.defaults 를 돈다)
            if (isinstance(node, ast.Constant)
                    and (node.lineno, node.col_offset) not in skip
                    and (node.lineno, node.col_offset, node.end_lineno,
                         node.end_col_offset) not in msgs):
                v = node.value
                repl = None
                if v is True:
                    repl = False
                elif v is False:
                    repl = True
                elif isinstance(v, str) and v:
                    repl = ""
                elif isinstance(v, float):
                    repl = int(v)
                elif isinstance(v, int) and not isinstance(v, bool):
                    repl = v + 1
                if repl is not None or (v is False):
                    ops.append(_Op(f"const:{v!r}->{repl!r}", node,
                                   lambda n, r=repl: setattr(n, "value", r)))
        # 10. 문장 삭제(본문이 비지 않는 선에서)
        for holder in ast.walk(fn):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(holder, field, None)
                if not isinstance(stmts, list) or len(stmts) < 2:
                    continue
                for idx, st in enumerate(stmts):
                    # return 도 지운다. 예전에는 제외했는데 그러면 "이른 반환으로 나가는
                    # 것"과 "계속 진행하는 것"의 차이가 통째로 무검사가 된다 — 조기 반환은
                    # 중복 방지·드롭 같은 판정의 실행 그 자체다(적대 검증 지적, 2026-08-05).
                    if (st.lineno, st.col_offset) in skip:
                        continue
                    ops.append(_Op(f"del-stmt:{type(st).__name__}", st,
                                   lambda n, h=holder, f=field, i=idx:
                                   getattr(h, f).__setitem__(i, ast.Pass())))
    # 11. 데코레이터 제거 — 데코레이터가 동작의 일부인 경우(@property, @staticmethod 등)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.decorator_list:
            ops.append(_Op(f"drop-decorator:{node.name}", node,
                           lambda n: setattr(n, "decorator_list", [])))
    return ops


def _swap_kwargs(call):
    named = [k for k in call.keywords if k.arg]
    named[0].arg, named[1].arg = named[1].arg, named[0].arg


def _mutate_source(orig_src, op):
    tree = ast.parse(orig_src)
    # 같은 위치의 노드를 새 트리에서 찾는다(줄·컬럼·타입으로 매칭)
    want = _pos(op.node)
    for holder in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(holder, field, None)
            if isinstance(stmts, list):
                for i, st in enumerate(stmts):
                    if _pos(st) == want and op.kind.startswith("del-stmt"):
                        stmts[i] = ast.Pass()
                        return ast.unparse(ast.fix_missing_locations(tree))
    for n in ast.walk(tree):
        if _pos(n) == want:
            op.apply(n)
            return ast.unparse(ast.fix_missing_locations(tree))
    return None


def run_tests(clone: Path, tests: tuple[str, ...]) -> int:
    """변이본을 **반드시** import 하도록 PYTHONPATH 를 도구가 직접 건다.

    호출자가 PYTHONPATH 를 잊으면 pip 설치된 실제 패키지를 읽어 전 변이가 조용히
    생존하거나 조용히 죽는다 — 1 라운드에서 검증자가 실제로 이 함정에 빠져 "변이해도
    characterization 이 안 바뀐다"는 틀린 결론을 냈다(2026-08-04). 도구가 스스로 건다.
    """
    for pc in clone.rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)
    env = {**os.environ, "PYTHONPATH": str(clone.resolve())}
    # 자기 프로세스 그룹으로 띄운다. 시간 초과 시 pytest 만 죽이면 **그 자식들이 고아로
    # 남는다** — 이 스위트에는 하위 프로세스를 띄우는 검사가 있고(env 변수명 계약),
    # 고아가 쌓이면 뒤 변이들의 측정까지 오염된다.
    proc = subprocess.Popen(
        [PY, "-B", "-m", "pytest", *tests, "-x", "-q",
         "-p", "no:cacheprovider", "-o", "addopts="],
        cwd=clone, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    try:
        proc.communicate(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        return _HUNG
    return proc.returncode


def _assert_tests_import_the_module(clone: Path, module: str,
                                    tests: tuple[str, ...]) -> None:
    """테스트 파일에 **그 모듈의 import 문이 있는지** 확인한다. 없으면 죽는다.

    `PACK_SUITES` 는 등록을 강제하지만 대응이 맞는지는 강제하지 않았다. 적대 검증이
    실증했다(2026-08-05): `jsonl_io` 변이를 `normalize` 테스트로 돌리면
    `총 10 KILLED 0 SURVIVED 10` 이 조용히 나온다. 사람은 그 0 을 "테스트가 약하다"로
    읽지 "배선이 틀렸다"로 읽지 않는다 — RC1 과 같은 계열의 구멍이다.

    **이 검사의 한계를 정직하게 적어 둔다.** 보는 것은 import 문의 존재뿐이고
    "그 모듈을 실제로 검사하는가"는 보증하지 않는다. 적대 검증이 30개 오조합 중 3개가
    통과함을 실증했다(2026-08-05): `jsonl_io x test_pack_build`(후자가
    `iter_jsonl` 을 쓴다), `schema x test_pack_{build,normalize}`. 미사용 import 한
    줄을 넣어도 통과한다. 사용 여부까지 올려도 위 세 조합은 실제 사용이라 안 닫힌다 —
    구문 검사로는 여기까지다. 그래서 아래 `sweep()` 이 **경험적 게이트**(KILLED 0 이면
    배선을 의심하라)를 따로 둔다.
    """
    dotted = module.removesuffix(".py").replace("/", ".")
    tail = dotted.rsplit(".", 1)[-1]
    parent = dotted.rsplit(".", 1)[0]
    for t in tests:
        tree = ast.parse((clone / t).read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import) and any(a.name == dotted for a in n.names):
                return
            if isinstance(n, ast.ImportFrom):
                if n.module == dotted:
                    return
                if n.module == parent and any(a.name == tail for a in n.names):
                    return
    sys.exit(f"배선 오류: {' '.join(tests)} 가 {dotted} 를 import 하지 않는다.\n"
             f"오매핑된 스윕은 생존자만 잔뜩 내고 아무것도 검증하지 않는다 — 죽는다.")


def sweep(clone: Path, module: str, tests: tuple[str, ...]) -> dict:
    """모듈 하나를 전면 스윕한다. 원본은 finally 에서 반드시 복원한다."""
    target = clone / module
    _assert_tests_import_the_module(clone, module, tests)
    orig_src = target.read_text()
    ops = _collect(ast.parse(orig_src))
    print(f"\n### {module}  변이 {len(ops)}종  테스트 {' '.join(tests)}", flush=True)

    rc = run_tests(clone, tests)
    assert rc == 0, f"baseline 이 깨져 있으면 매트릭스는 무효다 (rc={rc})"
    print("BASELINE rc=0", flush=True)

    survived, invalid, killed, broken, hung = [], [], 0, [], []
    try:
        for i, op in enumerate(ops, 1):
            src = _mutate_source(orig_src, op)
            if src is None:
                invalid.append(op.label())          # 조용히 세지 않는다(RC2)
                continue
            try:
                compile(src, "<mut>", "exec")
            except SyntaxError:
                invalid.append(op.label())
                continue
            target.write_text(src)
            rc = run_tests(clone, tests)
            if rc == 0:
                survived.append(op.label())
            elif rc == 1:
                killed += 1                          # 테스트가 계약을 검사해서 잡았다
            elif rc == _HUNG:
                hung.append(op.label())              # 무한 루프·메모리 폭증
            else:
                broken.append(op.label())            # 모듈이 뜨지도 못했다 — 계약 검증 아님
            if i % 50 == 0:
                print(f"  {i}/{len(ops)}  killed={killed} broken={len(broken)} "
                      f"hung={len(hung)} survived={len(survived)}", flush=True)
    finally:
        target.write_text(orig_src)

    # 경험적 배선 게이트. 구문 검사(import 문 존재)로는 오매핑을 다 못 막으므로
    # 결과로 한 번 더 본다 — 한 종도 못 죽였다면 그 테스트는 이 모듈을 안 보고 있다.
    if ops and killed == 0:
        print(f"  ⚠ 배선 의심: {module} 변이를 {' '.join(tests)} 가 한 종도 죽이지 못했다. "
              f"오매핑이 아닌지 확인하라.", flush=True)

    res = {"module": module, "tests": list(tests), "total": len(ops),
           "killed": killed, "broken": broken, "hung": hung, "invalid": invalid,
           "survived": survived}
    print(f"총 {len(ops)}  KILLED {killed}  BROKEN {len(broken)}  HUNG {len(hung)}  "
          f"적용불가 {len(invalid)}  SURVIVED {len(survived)}")
    for lbl in survived:
        print("  생존:", lbl)
    for lbl in invalid:
        print("  적용불가:", lbl)            # 정체 불명의 숫자를 남기지 않는다
    for lbl in broken:
        print("  BROKEN(모듈 미기동):", lbl)
    for lbl in hung:
        print(f"  HUNG({RUN_TIMEOUT}s 초과):", lbl)
    return res


def _all_targets(clone: Path) -> list[tuple[str, tuple[str, ...]]]:
    """`opencrab/pack/` 전 모듈. 등록되지 않은 모듈이 있으면 **죽는다**(RC1 폐쇄)."""
    found = {f"opencrab/pack/{p.name}" for p in (clone / "opencrab/pack").glob("*.py")
             if p.name != "__init__.py"}
    missing = sorted(found - set(PACK_SUITES))
    if missing:
        sys.exit(f"PACK_SUITES 에 없는 모듈: {missing}\n"
                 f"스윕 대상을 사람이 고르지 못하게 하려고 죽는다 — 표에 등록하라.")
    gone = sorted(set(PACK_SUITES) - found)
    if gone:
        sys.exit(f"PACK_SUITES 에 있는데 파일이 없다: {gone}")
    return [(m, PACK_SUITES[m]) for m in sorted(found)]


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    clone = Path(sys.argv[1])

    if sys.argv[2] == "--all":
        targets = _all_targets(clone)
        out_json = sys.argv[3] if len(sys.argv) > 3 else None
    else:
        if len(sys.argv) < 4:
            sys.exit(__doc__)
        targets = [(sys.argv[2], tuple(sys.argv[3].split(",")))]
        out_json = sys.argv[4] if len(sys.argv) > 4 else None

    results = [sweep(clone, m, t) for m, t in targets]

    print("\n===== 요약 =====")
    for r in results:
        print(f"  {r['module']:34} 총 {r['total']:4}  KILLED {r['killed']:4}  "
              f"BROKEN {len(r['broken']):3}  HUNG {len(r['hung']):3}  "
              f"SURVIVED {len(r['survived']):3}")
    total_survived = sum(len(r["survived"]) for r in results)
    print(f"  생존 합계 {total_survived}")
    if out_json:
        Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if total_survived else 0


if __name__ == "__main__":
    sys.exit(main())
