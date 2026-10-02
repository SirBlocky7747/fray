"""
fray semantic analyzer — name resolution, const enforcement, control flow checks.

Usage:
    from sema import analyze
    analyze(program, "filename.fray")
"""

from __future__ import annotations
from typing import Optional

from ast_nodes import (
    Program, Assignment, AugmentedAssignment, ExprStatement,
    ReturnStatement, ConstStatement, ImportStatement, ExternFuncDecl,
    FunctionDef, IfStatement, ForLoop, WhileLoop, TryStatement,
    AwaitExpr,
    BreakStatement, ContinueStatement,
    IntLiteral, FloatLiteral, StringLiteral, BoolLiteral, ConstantLiteral,
    Identifier, BinaryOp, UnaryOp, Call, MemberAccess, Index,
    ListLiteral, SetLiteral, TupleLiteral,
    EnumDef, EnumCase,
    Node,
)


class SemanticError(Exception):
    def __init__(self, msg: str, filename: str, line: int, col: int):
        self.filename = filename
        self.line = line
        self.col = col
        super().__init__(f"{filename}:{line}:{col}: {msg}")


class Scope:
    """A single scope level tracking defined names and const bindings."""
    def __init__(self):
        self.names: set[str] = set()
        self.consts: set[str] = set()

    def define(self, name: str, is_const: bool = False):
        self.names.add(name)
        if is_const:
            self.consts.add(name)

    def is_defined(self, name: str) -> bool:
        return name in self.names

    def is_const(self, name: str) -> bool:
        return name in self.consts


# Known exception types
KNOWN_EXCEPTIONS = {
    "TypeError", "ValueError", "IndexError", "KeyError",
    "NameError", "RuntimeError", "OverflowError",
    "ZeroDivisionError", "ImportError", "Exception",
}


class Analyzer:
    def __init__(self, filename: str = "<string>"):
        self.filename = filename
        self.scopes: list[Scope] = []
        self.in_loop = False
        self.in_function = False

    def analyze(self, program: Program):
        """Run semantic analysis on a parsed Program."""
        self._push_scope()

        # Pre-define builtins
        builtins = {
            "print", "input", "inputStr", "inputInt", "inputFloat",
            "len", "min", "max", "sum", "abs", "sqrt", "isqrt",
            "round", "int", "float", "str", "range",
            "mean", "med", "mode", "mid",
            "spawn", "join", "joinAll", "atomic",
            "sleep", "channel", "send", "recv", "close",
            "coroCount", "yield", "yieldNow", "runUntilComplete",
        }
        for name in builtins:
            self._current_scope().define(name)

        for stmt in program.body:
            self._check_statement(stmt)

        self._pop_scope()

    # ── Scope helpers ──

    def _push_scope(self):
        self.scopes.append(Scope())

    def _pop_scope(self):
        self.scopes.pop()

    def _current_scope(self) -> Scope:
        return self.scopes[-1]

    def _define(self, name: str, is_const: bool = False, line: int = 0, col: int = 0):
        # Allow const redefinition of already-defined variables
        if self._current_scope().is_defined(name):
            if is_const:
                self._current_scope().consts.add(name)
            else:
                self._error(f"'{name}' is already defined in this scope", line, col)
        else:
            self._current_scope().define(name, is_const)

    def _resolve(self, name: str) -> bool:
        """Check if name is defined in any enclosing scope."""
        for scope in reversed(self.scopes):
            if scope.is_defined(name):
                return True
        return False

    def _is_const(self, name: str) -> bool:
        for scope in reversed(self.scopes):
            if scope.is_defined(name):
                return scope.is_const(name)
        return False

    def _error(self, msg: str, line: int, col: int):
        raise SemanticError(msg, self.filename, line, col)

    # ── Statement checking ──

    def _check_statement(self, node: Node):
        if isinstance(node, Assignment):
            self._check_assignment(node)
        elif isinstance(node, AugmentedAssignment):
            self._check_augmented_assignment(node)
        elif isinstance(node, ExprStatement):
            self._check_expr(node.expr)
        elif isinstance(node, ReturnStatement):
            self._check_return(node)
        elif isinstance(node, ConstStatement):
            self._check_const(node)
        elif isinstance(node, ImportStatement):
            pass  # imports are validated at runtime
        elif isinstance(node, ExternFuncDecl):
            self._define(node.name, line=node.line, col=node.col)
        elif isinstance(node, EnumDef):
            # Define the enum type name in scope
            self._define(node.name, line=node.line, col=node.col)
        elif isinstance(node, FunctionDef):
            self._check_function_def(node)
        elif isinstance(node, IfStatement):
            self._check_if(node)
        elif isinstance(node, ForLoop):
            self._check_for(node)
        elif isinstance(node, WhileLoop):
            self._check_while(node)
        elif isinstance(node, TryStatement):
            self._check_try(node)
        elif isinstance(node, BreakStatement):
            if not self.in_loop:
                self._error("'break' outside of loop", node.line, node.col)
        elif isinstance(node, ContinueStatement):
            if not self.in_loop:
                self._error("'continue' outside of loop", node.line, node.col)

    def _check_assignment(self, node: Assignment):
        self._check_target(node.target)
        self._check_expr(node.value)

    def _check_augmented_assignment(self, node: AugmentedAssignment):
        self._check_target(node.target)
        self._check_expr(node.value)

    def _check_target(self, node: Optional[Node]):
        if node is None:
            return
        if isinstance(node, Identifier):
            if self._is_const(node.name):
                self._error(f"cannot reassign const '{node.name}'", node.line, node.col)
            # Define variable if not already in scope
            if not self._resolve(node.name):
                self._define(node.name, line=node.line, col=node.col)
            # If it's already defined but not const, that's fine (reassignment allowed)
        elif isinstance(node, (MemberAccess, Index)):
            self._check_expr(node.obj if isinstance(node, MemberAccess) else node.obj)
        else:
            self._error("invalid assignment target", node.line, node.col)

    def _check_return(self, node: ReturnStatement):
        if not self.in_function:
            self._error("'return' outside of function", node.line, node.col)
        if node.value is not None:
            self._check_expr(node.value)

    def _check_const(self, node: ConstStatement):
        self._check_expr(node.value)
        self._define(node.name, is_const=True, line=node.line, col=node.col)

    def _check_function_def(self, node: FunctionDef):
        self._define(node.name, line=node.line, col=node.col)
        self._push_scope()
        old_in_func = self.in_function
        self.in_function = True
        for param in node.params:
            self._current_scope().define(param)
        for stmt in node.body:
            self._check_statement(stmt)
        self.in_function = old_in_func
        self._pop_scope()

    def _check_if(self, node: IfStatement):
        for cond, body in node.branches:
            if cond is not None:
                self._check_expr(cond)
            self._push_scope()
            for stmt in body:
                self._check_statement(stmt)
            self._pop_scope()
        if node.else_body:
            self._push_scope()
            for stmt in node.else_body:
                self._check_statement(stmt)
            self._pop_scope()

    def _check_for(self, node: ForLoop):
        self._check_expr(node.iterable)
        self._push_scope()
        self._current_scope().define(node.var)
        old_in_loop = self.in_loop
        self.in_loop = True
        for stmt in node.body:
            self._check_statement(stmt)
        self.in_loop = old_in_loop
        self._pop_scope()

    def _check_while(self, node: WhileLoop):
        self._check_expr(node.condition)
        self._push_scope()
        old_in_loop = self.in_loop
        self.in_loop = True
        for stmt in node.body:
            self._check_statement(stmt)
        self.in_loop = old_in_loop
        self._pop_scope()

    def _check_try(self, node: TryStatement):
        for stmt in node.body:
            self._check_statement(stmt)
        for exc_type, body in node.except_clauses:
            if exc_type is not None and exc_type not in KNOWN_EXCEPTIONS:
                self._error(f"unknown exception type '{exc_type}'", node.line, node.col)
            self._push_scope()
            for stmt in body:
                self._check_statement(stmt)
            self._pop_scope()
        if node.finally_body:
            self._push_scope()
            for stmt in node.finally_body:
                self._check_statement(stmt)
            self._pop_scope()

    # ── Expression checking ──

    def _check_expr(self, node: Optional[Node]):
        if node is None:
            return
        if isinstance(node, Identifier):
            if not self._resolve(node.name):
                # Allow some names that might be defined later (forward refs)
                # For now, just warn — could be strict
                pass
        elif isinstance(node, BinaryOp):
            self._check_expr(node.left)
            self._check_expr(node.right)
        elif isinstance(node, UnaryOp):
            self._check_expr(node.operand)
        elif isinstance(node, AwaitExpr):
            self._check_expr(node.value)
        elif isinstance(node, Call):
            self._check_expr(node.func)
            for arg in node.args:
                self._check_expr(arg)
        elif isinstance(node, MemberAccess):
            self._check_expr(node.obj)
        elif isinstance(node, Index):
            self._check_expr(node.obj)
            self._check_expr(node.index_expr)
        elif isinstance(node, ListLiteral):
            for elem in node.elements:
                self._check_expr(elem)
        elif isinstance(node, SetLiteral):
            for elem in node.elements:
                self._check_expr(elem)
        elif isinstance(node, TupleLiteral):
            for elem in node.elements:
                self._check_expr(elem)
        # Literals are always valid


def analyze(program: Program, filename: str = "<string>"):
    """Run semantic analysis. Raises SemanticError on failure."""
    Analyzer(filename).analyze(program)
