"""
fray AST node definitions.

Every node carries line/col for error reporting.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ── Base ──

@dataclass
class Node:
    line: int = 0
    col: int = 0


# ── Expressions ──

@dataclass
class IntLiteral(Node):
    value: int = 0

@dataclass
class FloatLiteral(Node):
    value: float = 0.0

@dataclass
class StringLiteral(Node):
    value: str = ""

@dataclass
class BoolLiteral(Node):
    value: bool = False

@dataclass
class ConstantLiteral(Node):
    """Built-in constants: pi, e."""
    name: str = ""

@dataclass
class Identifier(Node):
    name: str = ""

@dataclass
class BinaryOp(Node):
    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None

@dataclass
class UnaryOp(Node):
    op: str = ""
    operand: Optional[Node] = None

@dataclass
class AwaitExpr(Node):
    """await expr — suspends the coroutine until the awaited event."""
    value: Optional[Node] = None

@dataclass
class QuestionMark(Node):
    """expr? — unwrap Option/Result, propagate None/Err upward."""
    value: Optional[Node] = None

@dataclass
class Call(Node):
    func: Optional[Node] = None
    args: list[Node] = field(default_factory=list)

@dataclass
class MemberAccess(Node):
    obj: Optional[Node] = None
    attr: str = ""

@dataclass
class Slice(Node):
    """start:stop:step inside [] — any of start/stop/step may be None."""
    start: Optional[Node] = None
    stop: Optional[Node] = None
    step: Optional[Node] = None


@dataclass
class Index(Node):
    obj: Optional[Node] = None
    index_expr: Optional[Node] = None

@dataclass
class ListLiteral(Node):
    elements: list[Node] = field(default_factory=list)

@dataclass
class SetLiteral(Node):
    elements: list[Node] = field(default_factory=list)

@dataclass
class TupleLiteral(Node):
    elements: list[Node] = field(default_factory=list)

@dataclass
class MapLiteral(Node):
    """{"key": val, ...} — insertion-ordered hash map literal."""
    keys: list[Node] = field(default_factory=list)
    values: list[Node] = field(default_factory=list)


# ── Statements ──

@dataclass
class Assignment(Node):
    target: Optional[Node] = None
    value: Optional[Node] = None

@dataclass
class DelStatement(Node):
    """del target — remove a variable, map key, or index."""
    target: Optional[Node] = None

@dataclass
class AugmentedAssignment(Node):
    target: Optional[Node] = None
    op: str = ""
    value: Optional[Node] = None

@dataclass
class ExprStatement(Node):
    expr: Optional[Node] = None

@dataclass
class ReturnStatement(Node):
    value: Optional[Node] = None

@dataclass
class ConstStatement(Node):
    name: str = ""
    value: Optional[Node] = None
    line: int = 0
    col: int = 0

@dataclass
class ImportStatement(Node):
    module: str = ""               # dotted path, without leading dots; "" for `from . import x`
    level: int = 0                  # leading dots: 0 absolute, 1 current package, 2 parent, ...
    from_import: bool = False       # True for `from X import Y`
    names: list[str] = field(default_factory=list)  # names for `from X import a, b`

@dataclass
class ExternFuncDecl(Node):
    """extern ret_type name(param_type param_name, ...)"""
    name: str = ""
    return_type: str = "void"
    param_types: list[str] = field(default_factory=list)
    param_names: list[str] = field(default_factory=list)

@dataclass
class FunctionDef(Node):
    name: str = ""
    params: list[str] = field(default_factory=list)
    body: list[Node] = field(default_factory=list)
    is_async: bool = False

@dataclass
class IfStatement(Node):
    # List of (condition, body) pairs for if/elif, plus optional else body
    branches: list[tuple[Optional[Node], list[Node]]] = field(default_factory=list)
    else_body: Optional[list[Node]] = None

@dataclass
class ForLoop(Node):
    var: str = ""
    iterable: Optional[Node] = None
    body: list[Node] = field(default_factory=list)

@dataclass
class WhileLoop(Node):
    condition: Optional[Node] = None
    body: list[Node] = field(default_factory=list)

@dataclass
class TryStatement(Node):
    body: list[Node] = field(default_factory=list)
    except_clauses: list[tuple[Optional[str], list[Node]]] = field(default_factory=list)
    finally_body: Optional[list[Node]] = None

@dataclass
class BreakStatement(Node):
    pass

@dataclass
class ContinueStatement(Node):
    pass


# ── Structs ──

@dataclass
class StructField(Node):
    """A field declaration inside a struct definition."""
    name: str = ""
    default_value: Optional[Node] = None

@dataclass
class StructDef(Node):
    """struct Name:\n    field1\n    field2 = default"""
    name: str = ""
    fields: list[StructField] = field(default_factory=list)

@dataclass
class StructLiteral(Node):
    """Name(field1=val1, field2=val2) — construction of a struct instance."""
    name: str = ""
    fields: list[tuple[str, Node]] = field(default_factory=list)  # (field_name, value_expr)


# ── Enums ──

@dataclass
class EnumCase(Node):
    """A single variant inside an enum: case Name or case Name(field1, field2)"""
    name: str = ""
    params: list[str] = field(default_factory=list)  # param names (empty = simple tag)

@dataclass
class EnumDef(Node):
    """enum Name:\n    case Variant1\n    case Variant2(a, b)"""
    name: str = ""
    cases: list[EnumCase] = field(default_factory=list)


@dataclass
class MatchCase(Node):
    """A single arm of a match expression: case Pattern => body"""
    enum_name: str = ""       # "" for wildcard
    variant_name: str = ""    # "" for wildcard
    params: list[str] = field(default_factory=list)  # bound names
    body: list[Node] = field(default_factory=list)

@dataclass
class MatchExpression(Node):
    """match expr:\n    case Pattern: body\n    case _: default"""
    subject: Node = None
    cases: list[MatchCase] = field(default_factory=list)


# ── Program ──

@dataclass
class Program(Node):
    body: list[Node] = field(default_factory=list)
