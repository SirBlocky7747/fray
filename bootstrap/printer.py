"""
fray AST round-trip printer — prints an AST back to fray source.

Usage:
    from printer import print_program
    source = print_program(ast)
"""

from __future__ import annotations
from typing import Optional

from ast_nodes import (
    Program, Assignment, AugmentedAssignment, ExprStatement,
    ReturnStatement, ConstStatement, ImportStatement,
    FunctionDef, IfStatement, ForLoop, WhileLoop, TryStatement,
    BreakStatement, ContinueStatement,
    IntLiteral, FloatLiteral, StringLiteral, BoolLiteral, ConstantLiteral,
    Identifier, BinaryOp, UnaryOp, Call, MemberAccess, Index,
    ListLiteral, SetLiteral, TupleLiteral,
    Node,
)


def print_program(program: Program) -> str:
    """Print a Program AST back to fray source code."""
    lines: list[str] = []
    for stmt in program.body:
        lines.append(_print_stmt(stmt, indent=0))
    return "\n".join(lines) + "\n"


def _indent(level: int) -> str:
    return "    " * level


def _print_stmt(node: Node, indent: int) -> str:
    """Print a single statement at the given indent level."""
    pad = _indent(indent)

    if isinstance(node, Assignment):
        target = _print_expr(node.target)
        value = _print_expr(node.value)
        return f"{pad}{target} = {value}"

    if isinstance(node, AugmentedAssignment):
        target = _print_expr(node.target)
        value = _print_expr(node.value)
        return f"{pad}{target} {node.op} {value}"

    if isinstance(node, ExprStatement):
        return f"{pad}{_print_expr(node.expr)}"

    if isinstance(node, ReturnStatement):
        if node.value is not None:
            return f"{pad}return {_print_expr(node.value)}"
        return f"{pad}return"

    if isinstance(node, ConstStatement):
        return f"{pad}const {node.name} = {_print_expr(node.value)}"

    if isinstance(node, ImportStatement):
        dots = "." * node.level
        if node.from_import:
            return f"{pad}from {dots}{node.module} import {', '.join(node.names)}"
        return f"{pad}import {dots}{node.module}"

    if isinstance(node, FunctionDef):
        params = ", ".join(node.params)
        lines = [f"{pad}def {node.name}({params}):"]
        for stmt in node.body:
            lines.append(_print_stmt(stmt, indent + 1))
        return "\n".join(lines)

    if isinstance(node, IfStatement):
        lines = []
        for i, (cond, body) in enumerate(node.branches):
            keyword = "if" if i == 0 else "elif"
            lines.append(f"{pad}{keyword} {_print_expr(cond)}:")
            for stmt in body:
                lines.append(_print_stmt(stmt, indent + 1))
        if node.else_body:
            lines.append(f"{pad}else:")
            for stmt in node.else_body:
                lines.append(_print_stmt(stmt, indent + 1))
        return "\n".join(lines)

    if isinstance(node, ForLoop):
        lines = [f"{pad}for {node.var} in {_print_expr(node.iterable)}:"]
        for stmt in node.body:
            lines.append(_print_stmt(stmt, indent + 1))
        return "\n".join(lines)

    if isinstance(node, WhileLoop):
        lines = [f"{pad}while {_print_expr(node.condition)}:"]
        for stmt in node.body:
            lines.append(_print_stmt(stmt, indent + 1))
        return "\n".join(lines)

    if isinstance(node, TryStatement):
        lines = [f"{pad}try:"]
        for stmt in node.body:
            lines.append(_print_stmt(stmt, indent + 1))
        for exc_type, body in node.except_clauses:
            if exc_type:
                lines.append(f"{pad}except {exc_type}:")
            else:
                lines.append(f"{pad}except:")
            for stmt in body:
                lines.append(_print_stmt(stmt, indent + 1))
        if node.finally_body:
            lines.append(f"{pad}finally:")
            for stmt in node.finally_body:
                lines.append(_print_stmt(stmt, indent + 1))
        return "\n".join(lines)

    if isinstance(node, BreakStatement):
        return f"{pad}break"

    if isinstance(node, ContinueStatement):
        return f"{pad}continue"

    return f"{pad}# <unknown statement: {type(node).__name__}>"


def _print_expr(node: Optional[Node]) -> str:
    """Print an expression node to source code."""
    if node is None:
        return ""

    if isinstance(node, IntLiteral):
        return str(node.value)

    if isinstance(node, FloatLiteral):
        s = str(node.value)
        # Ensure there's a decimal point for floats
        if "." not in s and "e" not in s and "E" not in s:
            s += ".0"
        return s

    if isinstance(node, StringLiteral):
        escaped = node.value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'

    if isinstance(node, BoolLiteral):
        return "True" if node.value else "False"

    if isinstance(node, ConstantLiteral):
        return node.name

    if isinstance(node, Identifier):
        return node.name

    if isinstance(node, BinaryOp):
        left = _print_expr(node.left)
        right = _print_expr(node.right)
        return f"{left} {node.op} {right}"

    if isinstance(node, UnaryOp):
        operand = _print_expr(node.operand)
        if node.op == "not":
            return f"not {operand}"
        return f"{node.op}{operand}"

    if isinstance(node, Call):
        func = _print_expr(node.func)
        args = ", ".join(_print_expr(a) for a in node.args)
        return f"{func}({args})"

    if isinstance(node, MemberAccess):
        obj = _print_expr(node.obj)
        return f"{obj}.{node.attr}"

    if isinstance(node, Index):
        obj = _print_expr(node.obj)
        idx = _print_expr(node.index_expr)
        return f"{obj}[{idx}]"

    if isinstance(node, ListLiteral):
        elements = ", ".join(_print_expr(e) for e in node.elements)
        return f"[{elements}]"

    if isinstance(node, SetLiteral):
        elements = ", ".join(_print_expr(e) for e in node.elements)
        return f"{{{elements}}}"

    if isinstance(node, TupleLiteral):
        elements = ", ".join(_print_expr(e) for e in node.elements)
        if len(node.elements) == 1:
            return f"({elements},)"
        return f"({elements})"

    return f"# <unknown expr: {type(node).__name__}>"
