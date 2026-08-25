"""Every name a module's top level binds. One file, one definition.

Why this lives in the test tree. The gate in `test_issue_194_201_merge.py` asks
"is this name bound anywhere other than its def?", and for three rounds of
review the answer came from a walk the gate had written for itself, because the
canonical collector sat outside the repository and no test could import it. Each
private walk drifted -- the last one counted a tuple rebinding once where the
canonical counts it twice, which is the difference between catching a shadowed
window function and waving it through. A definition a caller cannot import is a
definition that gets rewritten, so it moved here, next to the only tests that
ask it anything.

The gap earlier versions had: walking only `tree.body` never enters the body of
a top-level compound statement, so `if True: NEW = ...` binds NEW at module
level invisibly. Recursion is not an extra feature -- without it the collector
answers a different question than the gate asks.

WHAT THIS DOES NOT CLAIM. Every review round found a form the previous version
missed: compound-statement bodies, then a lambda walrus over-report and a bare
`global` over-count, then walruses in decorators, defaults, annotations and


class bases. The honest reading is that "every module-level binding" is a target,
not a proven property. What is proven is that the forms in
`test_the_binding_collector_sees_every_module_level_form` are handled, and that
list grows each time something slips. Do not cite this collector as a proof;
cite the test.
"""

from __future__ import annotations

import ast


def _walk_own_scope(node):
    """ast.walk restricted to expressions evaluated in THIS scope.

    Lambda bodies, and the bodies of nested functions and classes, open their own
    scope: a walrus there binds elsewhere and must not be counted. v10 skipped
    lambdas but not nested defs, and over-reported (codex).

    A lambda is not skipped whole, though. Its BODY is deferred, but its
    parameter DEFAULTS are evaluated right here, the moment the lambda
    expression is built -- the same property that makes a def's defaults a real
    channel and put a "no defaults" assertion on the window functions. Skipping
    the node entirely took the defaults with it, so
    `BOX = (lambda x=(NAME := replacement): x,)` rebound a module name that this
    collector then reported as bound once (codex review, PR #234).
    """
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, ast.Lambda):
            defaults = list(cur.args.defaults)
            defaults += [d for d in cur.args.kw_defaults if d is not None]
            stack.extend(defaults)
            continue                       # body: own scope, do not descend
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                continue
            stack.append(child)


def _targets(node, out):
    """Names bound by an assignment target, unwrapping tuples/lists/stars."""
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            _targets(e, out)
    elif isinstance(node, ast.Starred):
        _targets(node.value, out)


def _stmt(node, out):
    """Names bound by one statement, recursing into compound-statement bodies.

    Module-level compound statements bind into the module namespace, so their
    bodies must be walked. Function and class bodies must NOT be -- those open
    their own scope, and their own name is the only thing they add here.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(node.name)
        # The BODY opens its own scope, but the header does not: decorators,
        # default values, annotations, and class bases are all evaluated right
        # here. A walrus in any of them binds in THIS scope. v10 returned
        # immediately and missed them (codex found `HIDDEN` in a default and
        # `DEC` in a decorator reaching the module namespace unseen).
        header = list(getattr(node, "decorator_list", []))
        header += list(getattr(node, "bases", []))
        header += [k.value for k in getattr(node, "keywords", []) or []]
        a = getattr(node, "args", None)
        if a is not None:
            header += [d for d in a.defaults if d is not None]
            header += [d for d in a.kw_defaults if d is not None]
            for arg in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs):
                if arg.annotation is not None:
                    header.append(arg.annotation)
            if getattr(node, "returns", None) is not None:
                header.append(node.returns)
        if a is not None:
            for extra in (a.vararg, a.kwarg):        # *args / **kwargs 애노테이션
                if extra is not None and extra.annotation is not None:
                    header.append(extra.annotation)
        for expr in header:
            # 기본값 자체가 lambda 면 그 본문은 lambda 스코프다. 헤더에서 이 스코프에
            # 평가되는 것은 lambda 의 *기본값*뿐이다. v11 은 본문까지 들어가 과잉 보고했다.
            # 그 구분은 이제 `_walk_own_scope` 안에 한 번만 있다 -- 여기에 사본을 두었더니
            # 일반 경로에는 없어서 같은 형태를 놓쳤다 (codex review, PR #234).
            for sub_node in _walk_own_scope(expr):
                if isinstance(sub_node, ast.NamedExpr):
                    _targets(sub_node.target, out)
        return                                    # body: own scope, do not descend
    if isinstance(node, ast.Assign):
        for t in node.targets:
            _targets(t, out)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        _targets(node.target, out)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            out.add(a.asname or a.name.split(".")[0])
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        _targets(node.target, out)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for it in node.items:
            if it.optional_vars is not None:
                _targets(it.optional_vars, out)
    # `global X` at module level declares nothing new -- the name is already
    # module scope, and the statement binds no value. v9 counted it and thereby
    # over-reported (opencode). Over-reporting is not harmless: it makes the
    # §4-2 difference gate reject a correct implementation.
    elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
        _targets(node.name, out)                  # `type X = ...` binds X

    # Walrus binds into the *enclosing* scope, so a module-level one counts --
    # including inside a comprehension. Inside a lambda it does not: the lambda
    # is its own scope. v9 swept every NamedExpr and reported names that never
    # reach the module namespace (opencode). The test passed because it did not
    # look at that axis, which is the failure this project keeps meeting.
    for sub in _walk_own_scope(node):
        if isinstance(sub, ast.NamedExpr):
            _targets(sub.target, out)
        elif isinstance(sub, ast.MatchAs) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, ast.MatchStar) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, ast.MatchMapping) and sub.rest:
            out.add(sub.rest)

    # recurse into the bodies of compound statements that bind into this scope
    for field in ("body", "orelse", "finalbody"):
        for child in getattr(node, field, []) or []:
            if isinstance(child, ast.stmt):
                _stmt(child, out)
    for handler in getattr(node, "handlers", []) or []:
        if handler.name:
            out.add(handler.name)                 # `except E as e` binds e
        for child in handler.body:
            _stmt(child, out)
    for case in getattr(node, "cases", []) or []:
        for child in case.body:
            _stmt(child, out)


class _Counter(set):
    """A set that also tallies how many times each name was bound.

    Assertion 9 asks "is this name bound anywhere other than its def?", which is a
    COUNT question, and v11's collector returned a bare set. So `gate_check.py` and
    the test each rebuilt their own counting walk -- three walks again, the exact
    shape the single-file move was supposed to end (codex 8회차). Carrying the tally
    on the canonical object lets both callers import it instead of reimplementing it.
    """

    def __init__(self):
        super().__init__()
        self.counts: dict[str, int] = {}

    def add(self, name):
        super().add(name)
        self.counts[name] = self.counts.get(name, 0) + 1

    def update(self, names):
        for n in names:
            self.add(n)


def bound_name_counts(src: str) -> dict[str, int]:
    """Every module-level binding, with how many times each name is bound.

    This is the canonical entry point. Assertion 9 reads the count; §4-2 reads the
    key set. Nobody re-walks.
    """
    out = _Counter()
    for node in ast.parse(src).body:
        _stmt(node, out)
    return dict(out.counts)


def bound_names(src: str) -> set[str]:
    """Every name bound at module level, including inside compound statements."""
    return set(bound_name_counts(src))


def top_level_names(path: str) -> set[str]:
    with open(path) as fh:
        return bound_names(fh.read())


def imported_names(src: str) -> set[str]:
    """Names a module's top level brings in by import, at any nesting depth.

    §4-2 compares new top-level names against the set of functions the design
    introduces. A signature change can pull in a new typing import -- `Callable`
    for the writer parameters -- and that name is a new top-level binding while
    being nothing the gate cares about: an import holds no pre-lock value. v9
    counted it and the gate therefore rejected a correct implementation.
    """
    import ast as _ast
    out: set[str] = set()

    def walk(node):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            return
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        for field in ("body", "orelse", "finalbody"):
            for child in getattr(node, field, []) or []:
                if isinstance(child, _ast.stmt):
                    walk(child)
        for h in getattr(node, "handlers", []) or []:
            for child in h.body:
                walk(child)
        for c in getattr(node, "cases", []) or []:
            for child in c.body:
                walk(child)

    for node in _ast.parse(src).body:
        walk(node)
    return out


def imported_from_path(path: str) -> set[str]:
    with open(path) as fh:
        return imported_names(fh.read())
