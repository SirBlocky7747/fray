"""check_scope.py — names that escape the block they are bound in.

The evaluator runs every nested statement body in a child scope
(bootstrap/evaluator.py::_exec_block), while the compiled emitter treats a
function's names as function-scoped. A name first assigned inside an `if`
branch, loop body, match arm or `finally` and then read in the enclosing scope
therefore raises FrayNameError under the evaluator while compiling cleanly —
the program works on the shipping path and dies on the --run path.

`Environment.set` rebinds an *existing* name in whichever enclosing scope
holds it, so a module global or a parameter is fine; what breaks is a name with
no binding anywhere that is introduced inside a block. Bind it before the
block instead, as parse_index_or_slice does with its three bounds.

usage: python tools/check_scope.py [file.fray ...]
       (defaults to the compiler modules and the standard library)
"""
import glob
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bootstrap"))
import ast_nodes as A  # noqa: E402
import lexer  # noqa: E402
import parser  # noqa: E402

BUILTINS = {
    "print", "len", "range", "int", "float", "str", "bool", "list", "map",
    "set", "abs", "min", "max", "sum", "round", "isqrt", "sqrt", "chr",
    "ord", "exit", "input", "inputInt", "inputFloat", "inputStr", "None",
    "True", "False", "self", "spawn", "join", "joinAll", "yieldNow", "send",
    "recv", "atomic", "channel", "sleep", "some", "ok", "err", "unwrap",
    "isOk", "isErr", "isSome", "isNone", "mode", "mean", "med", "mid",
}


def expr_reads(node, out):
    """Identifier names read by an expression (never a target)."""
    if node is None:
        return
    if isinstance(node, A.Identifier):
        out.add(node.name)
        return
    for value in vars(node).values():
        scan_expr(value, out)


def scan_expr(node, out):
    if isinstance(node, list):
        for item in node:
            scan_expr(item, out)
    elif isinstance(node, tuple):
        for item in node:
            scan_expr(item, out)
    elif isinstance(node, A.Node):
        if isinstance(node, A.Identifier):
            out.add(node.name)
            return
        expr_reads(node, out)
        for value in vars(node).values():
            scan_expr(value, out)


def target_name(node):
    return node.name if isinstance(node, A.Identifier) else None


def walk_body(stmts, assigned_top, read_top, assigned_nested, read_nested=None):
    """One statement list. Statements here run in the *current* scope; a
    nested body gets its own assigned_nested and its own read set, because a
    read inside a block is resolved in that block's scope."""
    if read_nested is None:
        read_nested = set()
    for stmt in stmts:
        if isinstance(stmt, A.Assignment):
            name = target_name(stmt.target)
            if name:
                assigned_top.add(name)
            expr_reads(stmt.value, read_top)
        elif isinstance(stmt, A.AugmentedAssignment):
            name = target_name(stmt.target)
            if name:
                assigned_top.add(name)
            expr_reads(stmt.value, read_top)
        elif isinstance(stmt, A.ExprStatement):
            expr_reads(stmt.expr, read_top)
        elif isinstance(stmt, A.ReturnStatement):
            expr_reads(stmt.value, read_top)
        elif isinstance(stmt, A.DelStatement):
            name = target_name(stmt.target)
            if name:
                assigned_top.add(name)
        elif isinstance(stmt, A.IfStatement):
            for cond, body in stmt.branches:
                expr_reads(cond, read_top)
                walk_body(body, assigned_nested, read_nested, assigned_nested,
                          read_nested)
            if stmt.else_body:
                walk_body(stmt.else_body, assigned_nested, read_nested,
                          assigned_nested, read_nested)
        elif isinstance(stmt, A.WhileLoop):
            expr_reads(stmt.condition, read_top)
            walk_body(stmt.body, assigned_nested, read_nested, assigned_nested,
                      read_nested)
        elif isinstance(stmt, A.ForLoop):
            assigned_top.add(stmt.var)
            expr_reads(stmt.iterable, read_top)
            walk_body(stmt.body, assigned_nested, read_nested, assigned_nested,
                      read_nested)
        elif isinstance(stmt, A.MatchExpression):
            for case in stmt.cases:
                walk_body(case.body, assigned_nested, read_nested,
                          assigned_nested, read_nested)
        elif isinstance(stmt, A.TryStatement):
            walk_body(stmt.body, assigned_nested, read_nested, assigned_nested,
                      read_nested)
            for _, body in stmt.except_clauses:
                walk_body(body, assigned_nested, read_nested, assigned_nested,
                          read_nested)
            if stmt.finally_body:
                walk_body(stmt.finally_body, assigned_nested, read_nested,
                          assigned_nested, read_nested)
        elif isinstance(stmt, A.ConstStatement):
            name = target_name(stmt.target)
            if name:
                assigned_top.add(name)


def check_function(fn, path, problems):
    assigned_top = set(fn.params)
    read_top = set()
    assigned_nested = set()
    walk_body(fn.body, assigned_top, read_top, assigned_nested)
    local = {v.name for v in vars(fn).values() if isinstance(v, A.Node)}
    escaped = (assigned_nested - assigned_top) & read_top - BUILTINS - local
    for name in sorted(escaped):
        problems.append(f"{path}: {fn.name}(): '{name}'")


def module_assigned(program):
    """Names bound by module-level statements: the evaluator runs these in the
    global scope, so a function assigning one of them rebinds the global
    instead of a block-local that disappears."""
    names = set()
    top, read, nested = set(), set(), set()
    walk_body(program.body, top, read, nested)
    return top | nested


def main():
    paths = sys.argv[1:] or (
        sorted(glob.glob(os.path.join(REPO, "compiler", "*.fray")))
        + sorted(glob.glob(os.path.join(REPO, "stdlib", "*.fray"))))
    problems = []
    for path in paths:
        program = parser.parse(lexer.tokenize(open(path, encoding="utf-8").read()))
        globals_ = module_assigned(program)
        for stmt in program.body:
            if isinstance(stmt, A.FunctionDef):
                assigned_top = set(stmt.params)
                read_top = set()
                assigned_nested = set()
                walk_body(stmt.body, assigned_top, read_top, assigned_nested)
                escaped = ((assigned_nested - assigned_top) & read_top
                           - BUILTINS - globals_)
                for name in sorted(escaped):
                    rel = os.path.relpath(path, REPO)
                    problems.append(f"{rel}: {stmt.name}(): '{name}'")
    for line in problems:
        print(line)
    print(f"\n{len(problems)} name(s) escape their block")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
