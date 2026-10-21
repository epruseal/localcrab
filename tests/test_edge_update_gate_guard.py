"""엣지 갱신 메서드의 인가 계약과 `load_edges` 반환 형태를 정적으로 고정한다.

`update_edge` / `update_edges_batch` 는 저장소 계층에 principal 이나 등록부 소유권
검사가 없다. 팩 쓰기가 단건 `update_edge` 에 닿는 길은
`OntologyBuilder.add_edge(replace_own=True)` 하나다. 이 파일의 첫 검사는 그 두 이름을 가리키는 프로덕션 참조가 표에 적힌
자리 밖에 없고, 빌더 안의 참조가 최상위 `authorize` 문장 뒤에 있음을 확인한다.

한계: 실행 중에 조립한 이름(`"update_" + "edge"` 등)은 정적 검사로 볼 수 없다.
이 검사는 글자 그대로의 이름만 덮는다. 패키지 밖 호출자는 문서 계약으로만 묶인다.

둘째 검사는 `load_edges` 의 반환문이 함수 본문의 직접 자식인 단 하나의 3원소
튜플 리터럴임을 확인한다. 반환 개수를 정적으로 확정하는 외부 검사기가 같은
조건을 요구한다.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from opencrab.pack import load as pack_load

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("opencrab", "apps", "server", "crabharness", "scripts")
SKIP_PARTS = {".venv", "venv", "node_modules", "__pycache__", ".git", "tests"}
NAMES = frozenset({"update_edge", "update_edges_batch"})

# (파일, 둘러싼 범위, 참조 개수). 범위는 점으로 이은 클래스/함수 이름이고
# 하위 중첩 범위도 포함한다.
ALLOWED = {
    ("opencrab/stores/neo4j_store.py", "Neo4jStore.update_edges_batch"): 1,
    ("opencrab/stores/kuzu_graph_store.py", "KuzuUnavailableGraphStore"): 2,
    ("opencrab/ontology/builder.py", "OntologyBuilder.add_edge"): 1,
}
GATED = ("opencrab/ontology/builder.py", "OntologyBuilder.add_edge")
AUTHORIZERS = frozenset({"authorize", "authorize_fork_copy"})


class _Collector(ast.NodeVisitor):
    def __init__(self):
        self.scope: list[str] = []
        self.refs: list[tuple[str, ast.AST]] = []

    def _enter(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node):
        self._enter(node)

    def visit_FunctionDef(self, node):
        self._enter(node)

    def visit_AsyncFunctionDef(self, node):
        self._enter(node)

    def _hit(self, node):
        self.refs.append((".".join(self.scope), node))

    def visit_Attribute(self, node):
        if node.attr in NAMES:
            self._hit(node)
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in NAMES:
            self._hit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value in NAMES:
            self._hit(node)


def _in_scope(scope: str, allowed: str) -> bool:
    return scope == allowed or scope.startswith(allowed + ".")


def _guarantees_authorize(stmt: ast.stmt) -> bool:
    """문장이 실행되면 `authorize` 계열 호출이 반드시 일어나는가."""
    if isinstance(stmt, (ast.Expr, ast.Assign)):
        value = stmt.value
        return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in AUTHORIZERS)
    if isinstance(stmt, ast.If):
        return (any(_guarantees_authorize(s) for s in stmt.body)
                and any(_guarantees_authorize(s) for s in stmt.orelse))
    return False


def _contains(stmt: ast.stmt, target: ast.AST) -> bool:
    return any(node is target for node in ast.walk(stmt))


def _gated_function(tree: ast.Module) -> ast.FunctionDef | None:
    cls_name, fn_name = GATED[1].split(".")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == fn_name:
                    return item
    return None


def find_update_edge_violations(sources: dict[str, str]) -> list[str]:
    """경로 -> 소스 매핑을 받아 허용표 밖의 참조와 인가 순서 위반을 모은다."""
    problems: list[str] = []
    found: set[tuple[str, str]] = set()
    for path, source in sorted(sources.items()):
        tree = ast.parse(source)
        collector = _Collector()
        collector.visit(tree)
        per_entry: dict[tuple[str, str], list[ast.AST]] = {}
        for scope, node in collector.refs:
            entry = next(
                (key for key in ALLOWED if key[0] == path and _in_scope(scope, key[1])),
                None,
            )
            if entry is None:
                problems.append(f"{path}:{scope or '<module>'}: 허용표 밖의 참조")
                continue
            per_entry.setdefault(entry, []).append(node)
        for entry, nodes in per_entry.items():
            found.add(entry)
            if len(nodes) != ALLOWED[entry]:
                problems.append(
                    f"{entry[0]}:{entry[1]}: 참조 {len(nodes)}개, 표는 {ALLOWED[entry]}개")
            if (path, entry[1]) == GATED:
                fn = _gated_function(tree)
                for node in nodes:
                    idx = next(
                        (i for i, stmt in enumerate(fn.body) if _contains(stmt, node)), None)
                    if idx is None or not any(
                            _guarantees_authorize(s) for s in fn.body[:idx]):
                        problems.append(
                            f"{path}:{entry[1]}: 참조 앞에 최상위 authorize 문장이 없다")
    for key in ALLOWED:
        if key[0] in sources and key not in found:
            problems.append(f"{key[0]}:{key[1]}: 표에 있으나 참조가 없다")
    return problems


def _production_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    for root in SCAN_ROOTS:
        for file in (REPO_ROOT / root).rglob("*.py"):
            if SKIP_PARTS & set(file.relative_to(REPO_ROOT).parts):
                continue
            sources[file.relative_to(REPO_ROOT).as_posix()] = file.read_text(encoding="utf-8")
    return sources


class TestUpdateEdgeCallersAreGated:
    def test_real_tree_has_no_unlisted_reference(self):
        sources = _production_sources()
        assert "opencrab/ontology/builder.py" in sources, "스캔 범위 전제 위반"
        assert find_update_edge_violations(sources) == []

    def test_unlisted_callers_and_aliases_are_reported(self):
        shapes = {
            "direct": "def f(g):\n    return g.update_edge(1)\n",
            "getattr_literal": "def f(g):\n    return getattr(g, 'update_edge')(1)\n",
            "alias": "def f(g):\n    h = g.update_edges_batch\n    return h([])\n",
            "partial": "import functools\ndef f(g):\n    return functools.partial(g.update_edge)\n",
        }
        for label, src in shapes.items():
            assert find_update_edge_violations({"opencrab/other.py": src}), label

    def test_wrapper_inside_stores_is_reported(self):
        src = "class W:\n    def helper(self, g):\n        return g.update_edge(1)\n"
        assert find_update_edge_violations({"opencrab/stores/new_wrapper.py": src})

    def test_builder_reference_needs_a_dominating_authorize(self):
        def builder(body: str) -> dict[str, str]:
            src = (
                "class OntologyBuilder:\n"
                "    def add_edge(self, replace_own=False, fork_copy=False):\n"
                + "".join(f"        {line}\n" for line in body.splitlines())
            )
            return {"opencrab/ontology/builder.py": src}

        good = (
            "if fork_copy:\n    authorize_fork_copy(1)\nelse:\n    authorize(1)\n"
            "self.g.update_edge(1)"
        )
        assert find_update_edge_violations(builder(good)) == []

        bad = {
            "one_branch": "if fork_copy:\n    authorize_fork_copy(1)\nself.g.update_edge(1)",
            "if_false": "if False:\n    authorize(1)\nself.g.update_edge(1)",
            "after_call": "self.g.update_edge(1)\nauthorize(1)",
            "no_authorize": "self.g.update_edge(1)",
            "other_name": "authorize_delete(1)\nself.g.update_edge(1)",
        }
        for label, body in bad.items():
            assert find_update_edge_violations(builder(body)), label


def _return_shape_problems(source: str, name: str, arity: int) -> list[str]:
    fn = next(n for n in ast.parse(source).body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    returns: list[ast.Return] = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                  ast.ClassDef)):
                continue
            if isinstance(child, ast.Return):
                returns.append(child)
            walk(child)

    walk(fn)
    problems = []
    if len(returns) != 1:
        problems.append(f"return 문이 {len(returns)}개")
        return problems
    ret = returns[0]
    if ret not in fn.body:
        problems.append("return 이 함수 본문의 직접 자식이 아니다")
    value = ret.value
    if not isinstance(value, ast.Tuple) or len(value.elts) != arity:
        problems.append("return 값이 지정한 원소 수의 튜플 리터럴이 아니다")
    elif any(isinstance(e, ast.Starred) for e in value.elts):
        problems.append("return 튜플에 별표 확장이 있다")
    return problems


class TestLoadEdgesReturnShape:
    def test_real_function_keeps_one_top_level_three_tuple(self):
        source = Path(inspect.getsourcefile(pack_load)).read_text(encoding="utf-8")
        assert _return_shape_problems(source, "load_edges", 3) == []

    def test_shape_check_rejects_moved_or_widened_returns(self):
        base = "def load_edges():\n{}\n"
        cases = {
            "under_if": "    if True:\n        return 1, 2, 3",
            "two_returns": "    if x:\n        return 1, 2, 3\n    return 1, 2, 3",
            "four_tuple": "    return 1, 2, 3, 4",
            "starred": "    return 1, *x",
            "not_tuple": "    return result",
        }
        for label, body in cases.items():
            assert _return_shape_problems(base.format(body), "load_edges", 3), label
        assert _return_shape_problems(base.format("    return 1, 2, 3"), "load_edges", 3) == []

    def test_consumer_call_shapes_still_bind(self):
        sig = inspect.signature(pack_load.load_edges)
        file = Path("edges.jsonl")
        sig.bind("p", file, object(), {})
        sig.bind("p", file, object(), {}, applied=set())
