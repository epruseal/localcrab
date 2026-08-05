#!/usr/bin/env python
"""모듈 하나에 대한 전면 기계 돌연변이 — 테스트의 검출력을 수치로 잰다.

**왜 있는가.** 2026-08-04 이 계층을 이관하며 적대 검증을 7라운드 돌렸는데, 매 라운드
새 결함이 나왔다. 원인은 코드가 아니라 방법이었다 — 검증자가 지적한 지점만 손으로
막으니 **인접한 같은 클래스**가 다음 라운드에 다시 나왔다. 7라운드에서 이 도구로
전면을 훑자 215종 중 생존 39종이 한 번에 드러났고, 그것이 두 클래스
("기존 유효값을 덮지 않는가", "유효값 튜플의 모든 원소를 존중하는가")로 수렴했다.
지적 대응이 아니라 **클래스 폐쇄**로 바꾼 것이 루프를 끊었다.

변이 대상: 비교 연산자, not, and/or, `.get(k, default)`·`.setdefault(k, default)` 의
기본값, 상수, 문장 삭제. docstring 은 동작이 아니라 제외한다.

생존자가 나오면 둘 중 하나다 — 미검사 경로(고쳐라) 또는 등가 변이(추론하지 말고
입력 격자로 차분 0 을 **측정해** 등가임을 보이고, 그 전제를 불변식 테스트로 못박아라).

사용:
    python scripts/qa/mutate_module.py <리포루트> <모듈경로> <테스트경로> [결과.json]
예:
    python scripts/qa/mutate_module.py . opencrab/pack/normalize.py \
        tests/test_pack_normalize.py /tmp/sweep.json
"""
import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

CLONE = Path(sys.argv[1])
TARGET = CLONE / sys.argv[2]
TESTS = sys.argv[3]
PY = sys.executable
ORIG_SRC = TARGET.read_text()
TREE = ast.parse(ORIG_SRC)


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


def _collect(tree):
    ops = []
    skip = _docstring_nodes(tree)
    # 함수 본문 안만 대상으로 한다(모듈 최상단 표 리터럴은 별도 축).
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
            # 5. 상수 뒤집기
            if isinstance(node, ast.Constant) and (node.lineno, node.col_offset) not in skip:
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
        # 6. 문장 삭제(본문이 비지 않는 선에서)
        for holder in ast.walk(fn):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(holder, field, None)
                if not isinstance(stmts, list) or len(stmts) < 2:
                    continue
                for idx, st in enumerate(stmts):
                    if isinstance(st, ast.Return) or (st.lineno, st.col_offset) in skip:
                        continue
                    ops.append(_Op(f"del-stmt:{type(st).__name__}", st,
                                   lambda n, h=holder, f=field, i=idx:
                                   getattr(h, f).__setitem__(i, ast.Pass())))
    return ops


def _mutate_source(op):
    tree = ast.parse(ORIG_SRC)
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


def run_tests():
    for pc in CLONE.rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)
    r = subprocess.run(
        [PY, "-B", "-m", "pytest", TESTS, "-x", "-q",
         "-p", "no:cacheprovider", "-o", "addopts="],
        cwd=CLONE, capture_output=True, text=True)
    return r.returncode


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    ops = _collect(TREE)
    print(f"수집한 변이 {len(ops)}종", flush=True)
    assert run_tests() == 0, "baseline 이 깨져 있으면 매트릭스는 무효다"
    print("BASELINE rc=0", flush=True)

    survived, invalid, killed = [], 0, 0
    try:
        for i, op in enumerate(ops, 1):
            src = _mutate_source(op)
            if src is None:
                invalid += 1
                continue
            try:
                compile(src, "<mut>", "exec")
            except SyntaxError:
                invalid += 1
                continue
            TARGET.write_text(src)
            if run_tests() == 0:
                survived.append(op.label())
            else:
                killed += 1
            if i % 25 == 0:
                print(f"  {i}/{len(ops)}  killed={killed} survived={len(survived)}",
                      flush=True)
    finally:
        TARGET.write_text(ORIG_SRC)

    print(f"\n총 {len(ops)}  적용불가 {invalid}  KILLED {killed}  SURVIVED {len(survived)}")
    for s in survived:
        print("  생존:", s)
    if len(sys.argv) > 4:
        Path(sys.argv[4]).write_text(json.dumps(
            {"total": len(ops), "invalid": invalid, "killed": killed,
             "survived": survived}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
