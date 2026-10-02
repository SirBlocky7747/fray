"""
fray tree-walking evaluator — the semantics reference.

Not the product (you chose AOT). Exists only as the referee for differential testing.

Usage:
    from evaluator import run
    output = run(source, "test.fray")
"""

from __future__ import annotations
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Any

from ast_nodes import (
    AwaitExpr,
    Program, Assignment, AugmentedAssignment, ExprStatement,
    ReturnStatement, ConstStatement, ImportStatement, ExternFuncDecl,
    FunctionDef, IfStatement, ForLoop, WhileLoop, TryStatement,
    BreakStatement, ContinueStatement,
    IntLiteral, FloatLiteral, StringLiteral, BoolLiteral, ConstantLiteral,
    Identifier, BinaryOp, UnaryOp, Call, MemberAccess, Index, Slice, QuestionMark,
    ListLiteral, SetLiteral, TupleLiteral,
    StructDef, StructLiteral, MapLiteral, DelStatement, EnumDef, EnumCase,
    MatchExpression, MatchCase,
    Node,
)
from lexer import tokenize, LexerError
from parser import parse, ParseError
from sema import analyze, SemanticError
import modules


# ── Exceptions ──

class FrayError(Exception):
    """Base for all runtime errors."""
    pass


class FrayProgramExit(Exception):
    """The program called exit(code). Not an error: the exit status must
    reach the host process the way the native runtime's exit() does."""
    def __init__(self, code: int):
        super().__init__(f"exit({code})")
        self.code = code

class FrayTypeError(FrayError):
    pass

class FrayValueError(FrayError):
    pass

class FrayIndexError(FrayError):
    pass

class FrayNameError(FrayError):
    pass

class FrayZeroDivisionError(FrayError):
    pass

class FrayImportError(FrayError):
    pass

class FrayKeyError(FrayError):
    pass

class FrayRuntimeError(FrayError):
    pass


# ── Sentinel values ──

class _Return(Exception):
    """Sentinel for return statements."""
    def __init__(self, value: Any = None):
        self.value = value

class _Break(Exception):
    pass

class _Continue(Exception):
    pass


# ── Container types ──

class FrayList:
    """Mutable list."""
    def __init__(self, elements: list[Any]):
        self.elements = list(elements)

    def __len__(self):
        return len(self.elements)

    def __getitem__(self, idx: int):
        if idx < 0:
            idx += len(self.elements)
        if idx < 0 or idx >= len(self.elements):
            raise FrayIndexError(f"list index {idx} out of range (length {len(self.elements)})")
        return self.elements[idx]

    def __setitem__(self, idx: int, val: Any):
        if idx < 0:
            idx += len(self.elements)
        if idx < 0 or idx >= len(self.elements):
            raise FrayIndexError(f"list index {idx} out of range (length {len(self.elements)})")
        self.elements[idx] = val

    def append(self, val: Any):
        self.elements.append(val)
        return None

    def depend(self):
        """Remove and return last element (like pop())."""
        if not self.elements:
            raise FrayIndexError("pop from empty list")
        return self.elements.pop()

    def __repr__(self):
        return f"[{', '.join(_repr(e) for e in self.elements)}]"


class FraySet:
    """Mutable set."""
    def __init__(self, elements: list[Any]):
        # Use a list to preserve insertion order but deduplicate
        seen = set()
        self.elements = []
        for e in elements:
            key = _hashable_key(e)
            if key not in seen:
                seen.add(key)
                self.elements.append(e)

    def __len__(self):
        return len(self.elements)

    def append(self, val: Any):
        """Add element (like Python's set.add)."""
        key = _hashable_key(val)
        existing_keys = {_hashable_key(e) for e in self.elements}
        if key not in existing_keys:
            self.elements.append(val)
        return None

    def depend(self):
        """Remove an arbitrary element."""
        if not self.elements:
            raise FrayIndexError("pop from empty set")
        return self.elements.pop()

    def __repr__(self):
        return "{" + ", ".join(_repr(e) for e in self.elements) + "}"


class FrayStructDef:
    """Struct type definition — stores field names and defaults."""
    def __init__(self, name: str, fields: list[tuple[str, Any]]):
        self.name = name
        self.fields = fields  # [(field_name, default_value_or_sentinel), ...]

    def __repr__(self):
        return f"<struct {self.name}>"


class FrayStruct:
    """Struct instance — mutable named fields."""
    def __init__(self, struct_def: FrayStructDef):
        self._struct_def = struct_def
        self._fields: dict[str, Any] = {}
        # Initialize with defaults
        for fname, default in struct_def.fields:
            self._fields[fname] = default

    def get(self, name: str) -> Any:
        if name not in self._fields:
            raise FrayRuntimeError(f"struct has no field '{name}'")
        return self._fields[name]

    def set(self, name: str, val: Any):
        if name not in self._fields:
            raise FrayRuntimeError(f"struct has no field '{name}'")
        self._fields[name] = val

    def __repr__(self):
        fields = ", ".join(f"{k}={_repr(v)}" for k, v in self._fields.items())
        return f"{self._struct_def.name}({fields})"


SENTINEL = object()  # marks fields with no default

# Module cache: absolute path → FrayModule
_module_cache: dict[str, 'FrayModule'] = {}


class FrayMap:
    """Insertion-ordered hash map."""
    def __init__(self):
        self._data: dict[int, Any] = {}  # hash -> (key, value)
        self._order: list[int] = []       # insertion order

    def __len__(self):
        return len(self._data)

    def __setitem__(self, key: Any, val: Any):
        h = hash(_hashable_key(key))
        if h not in self._data:
            self._order.append(h)
        self._data[h] = (key, val)

    def __getitem__(self, key: Any) -> Any:
        h = hash(_hashable_key(key))
        if h not in self._data:
            raise FrayKeyError(f"key not found")
        _, val = self._data[h]
        return val

    def __contains__(self, key: Any) -> bool:
        h = hash(_hashable_key(key))
        return h in self._data

    def delete(self, key: Any):
        h = hash(_hashable_key(key))
        if h not in self._data:
            raise FrayKeyError(f"key not found")
        del self._data[h]
        self._order.remove(h)

    def __repr__(self):
        items = []
        for h in self._order:
            k, v = self._data[h]
            items.append(f"{_repr(k)}: {_repr(v)}")
        return "{" + ", ".join(items) + "}"


class FraySome:
    """Option type: wraps a value."""
    def __init__(self, val: Any):
        self.val = val
    def __repr__(self):
        return f"Some({_repr(self.val)})"


class FrayOk:
    """Result type: success wrapper."""
    def __init__(self, val: Any):
        self.val = val
    def __repr__(self):
        return f"Ok({_repr(self.val)})"


class FrayErr:
    """Result type: error wrapper."""
    def __init__(self, err: Any):
        self.err = err
    def __repr__(self):
        return f"Err({_repr(self.err)})"


class FrayEnumDef:
    """Enum type definition — stores variant names and their param counts."""
    def __init__(self, name: str, cases: list[tuple[str, list[str]]]):
        self.name = name
        self.cases = cases  # [(variant_name, [param_names]), ...]

    def __repr__(self):
        return f"<enum {self.name}>"

    def get_case(self, variant_name: str) -> tuple[str, list[str]]:
        for vname, params in self.cases:
            if vname == variant_name:
                return vname, params
        raise FrayRuntimeError(f"'{self.name}' has no variant '{variant_name}'")


class FrayEnum:
    """Enum variant instance."""
    def __init__(self, enum_def: FrayEnumDef, variant: str, values: list[Any]):
        self._enum_def = enum_def
        self._variant = variant
        self._values = tuple(values)

    @property
    def variant(self) -> str:
        return self._variant

    def __eq__(self, other):
        if not isinstance(other, FrayEnum):
            return NotImplemented
        return (self._enum_def is other._enum_def and
                self._variant == other._variant and
                self._values == other._values)

    def __repr__(self):
        vals = ", ".join(_repr(v) for v in self._values)
        if self._values:
            return f"{self._enum_def.name}.{self._variant}({vals})"
        return f"{self._enum_def.name}.{self._variant}"


class FrayModule:
    """Module object — holds top-level definitions from a .fray file."""
    def __init__(self, name: str, attrs: dict[str, Any]):
        self.name = name
        self.attrs = attrs

    def __repr__(self):
        return f"<module '{self.name}'>"


class FrayTuple:
    """Immutable tuple."""
    def __init__(self, elements: list[Any]):
        self.elements = tuple(elements)

    def __len__(self):
        return len(self.elements)

    def __getitem__(self, idx: int):
        if idx < 0:
            idx += len(self.elements)
        if idx < 0 or idx >= len(self.elements):
            raise FrayIndexError(f"tuple index {idx} out of range (length {len(self.elements)})")
        return self.elements[idx]

    def __repr__(self):
        if len(self.elements) == 1:
            return f"({_repr(self.elements[0])},)"
        return f"({', '.join(_repr(e) for e in self.elements)})"


class FrayFunction:
    """User-defined function."""
    def __init__(self, name: str, params: list[str], body: list[Node],
                 closure: "Environment", is_async: bool = False):
        self.name = name
        self.params = params
        self.body = body
        self.closure = closure
        self.is_async = is_async

    def __repr__(self):
        return f"<function {self.name}>"


# ── Helpers ──

def _hashable_key(val: Any) -> Any:
    """Convert a value to a hashable key for set operations."""
    if isinstance(val, FrayList):
        return tuple(_hashable_key(e) for e in val.elements)
    if isinstance(val, FraySet):
        return frozenset(_hashable_key(e) for e in val.elements)
    if isinstance(val, FrayTuple):
        return tuple(_hashable_key(e) for e in val.elements)
    return val


def _values_equal(a: Any, b: Any) -> bool:
    """Deep equality for fray values (used by set membership tests)."""
    if type(a) != type(b):
        return False
    if isinstance(a, FrayList):
        if len(a.elements) != len(b.elements):
            return False
        return all(_values_equal(x, y) for x, y in zip(a.elements, b.elements))
    if isinstance(a, FrayTuple):
        if len(a.elements) != len(b.elements):
            return False
        return all(_values_equal(x, y) for x, y in zip(a.elements, b.elements))
    if isinstance(a, FraySet):
        if len(a.elements) != len(b.elements):
            return False
        return all(any(_values_equal(x, y) for y in b.elements) for x in a.elements)
    return a == b


def _repr(val: Any) -> str:
    """repr() for fray values."""
    if val is None:
        return "None"
    if isinstance(val, bool):
        return "True" if val else "False"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(val)
    if isinstance(val, str):
        return val  # strings print without quotes in fray
    if isinstance(val, FrayList):
        return repr(val)
    if isinstance(val, FraySet):
        return repr(val)
    if isinstance(val, FrayTuple):
        return repr(val)
    if isinstance(val, FrayMap):
        return repr(val)
    if isinstance(val, FrayStruct):
        return repr(val)
    if isinstance(val, FrayFunction):
        return repr(val)
    if isinstance(val, FrayEnum):
        return repr(val)
    if isinstance(val, FrayEnumDef):
        return repr(val)
    if isinstance(val, FrayModule):
        return repr(val)
    if isinstance(val, FrayStructDef):
        return repr(val)
    return str(val)

def _is_truthy(val: Any) -> bool:
    if val is None:
        return False
    if val is False:
        return False
    if val == 0:
        return False
    if val == 0.0:
        return False
    if val == "":
        return False
    if isinstance(val, FrayList) and len(val) == 0:
        return False
    if isinstance(val, FraySet) and len(val) == 0:
        return False
    if isinstance(val, FrayTuple) and len(val) == 0:
        return False
    if isinstance(val, FrayMap) and len(val) == 0:
        return False
    return True


# ── Environment (scope) ──

class Environment:
    def __init__(self, parent: Optional["Environment"] = None):
        self.vars: dict[str, Any] = {}
        self.consts: set[str] = set()
        self.parent = parent

    def get(self, name: str) -> Any:
        if name in self.vars:
            return self.vars[name]
        if self.parent is not None:
            return self.parent.get(name)
        raise FrayNameError(f"name '{name}' is not defined")

    def set(self, name: str, val: Any, is_const: bool = False):
        """Set a variable, walking up the scope chain if it already exists."""
        # Check if it's const in the current scope
        if name in self.consts and name in self.vars:
            raise FrayTypeError(f"cannot reassign const '{name}'")
        # If the variable exists in this scope, set it here
        if name in self.vars:
            self.vars[name] = val
            if is_const:
                self.consts.add(name)
            return
        # Walk up the scope chain
        if self.parent is not None and self.parent._has(name):
            self.parent.set(name, val, is_const)
            return
        # Not found anywhere — define in current scope
        self.vars[name] = val
        if is_const:
            self.consts.add(name)

    def _has(self, name: str) -> bool:
        """Check if name exists in this scope or any parent."""
        if name in self.vars:
            return True
        if self.parent is not None:
            return self.parent._has(name)
        return False

    def define(self, name: str, val: Any, is_const: bool = False):
        """Define a new variable (used in function params, for loops, etc.)."""
        self.vars[name] = val
        if is_const:
            self.consts.add(name)

    def undef(self, name: str):
        """Remove a variable from the current scope."""
        if name in self.vars:
            del self.vars[name]
            self.consts.discard(name)
        elif self.parent is not None:
            self.parent.undef(name)
        else:
            raise FrayNameError(f"cannot delete '{name}': not defined")


# ── Built-in functions ──

def _builtin_print(args: list[Any]) -> None:
    parts = [_repr(a) for a in args]
    print(" ".join(parts) if parts else "")
    return None


def _builtin_input(args: list[Any]) -> str:
    if args:
        sys.stdout.write(_repr(args[0]))
        sys.stdout.flush()
    return input()


def _builtin_input_str(args: list[Any]) -> str:
    if args:
        sys.stdout.write(_repr(args[0]))
        sys.stdout.flush()
    return input()


def _builtin_input_int(args: list[Any]) -> int:
    if args:
        sys.stdout.write(_repr(args[0]))
        sys.stdout.flush()
    return int(input())


def _builtin_input_float(args: list[Any]) -> float:
    if args:
        sys.stdout.write(_repr(args[0]))
        sys.stdout.flush()
    return float(input())


def _builtin_len(args: list[Any]) -> int:
    if len(args) != 1:
        raise FrayTypeError("len() takes exactly one argument")
    val = args[0]
    if isinstance(val, (FrayList, FraySet, FrayTuple, FrayMap, str)):
        return len(val)
    raise FrayTypeError(f"len() not supported for {type(val).__name__}")


def _builtin_min(args: list[Any]) -> Any:
    if not args:
        raise FrayTypeError("min() requires at least one argument")
    return min(args, key=lambda x: x)


def _builtin_max(args: list[Any]) -> Any:
    if not args:
        raise FrayTypeError("max() requires at least one argument")
    return max(args, key=lambda x: x)


def _builtin_ord(args: list[Any]) -> int:
    if len(args) != 1:
        raise FrayTypeError("ord() takes exactly one argument")
    c = args[0]
    if not isinstance(c, str) or len(c) != 1:
        raise FrayTypeError("ord() expected a single character")
    return ord(c)


def _builtin_chr(args: list[Any]) -> str:
    if len(args) != 1:
        raise FrayTypeError("chr() takes exactly one argument")
    n = args[0]
    if not isinstance(n, int):
        raise FrayTypeError("chr() expected an integer")
    if n < 0 or n > 127:
        raise FrayTypeError("chr() arg out of range")
    return chr(n)


def _builtin_some(args: list[Any]) -> FraySome:
    if len(args) != 1:
        raise FrayTypeError("Some() takes exactly one argument")
    return FraySome(args[0])


def _builtin_ok(args: list[Any]) -> FrayOk:
    if len(args) != 1:
        raise FrayTypeError("Ok() takes exactly one argument")
    return FrayOk(args[0])


def _builtin_err(args: list[Any]) -> FrayErr:
    if len(args) != 1:
        raise FrayTypeError("Err() takes exactly one argument")
    return FrayErr(args[0])


def _builtin_unwrap(args: list[Any]) -> Any:
    if len(args) != 1:
        raise FrayTypeError("unwrap() takes exactly one argument")
    val = args[0]
    if isinstance(val, FraySome):
        return val.val
    if isinstance(val, FrayOk):
        return val.val
    if isinstance(val, FrayErr):
        raise FrayRuntimeError(f"called unwrap() on Err: {_repr(val.err)}")
    if val is None:
        raise FrayRuntimeError("called unwrap() on None")
    return val


def _builtin_is_some(args: list[Any]) -> bool:
    if len(args) != 1:
        raise FrayTypeError("isSome() takes exactly one argument")
    return isinstance(args[0], FraySome)


def _builtin_is_none_val(args: list[Any]) -> bool:
    if len(args) != 1:
        raise FrayTypeError("isNone() takes exactly one argument")
    return args[0] is None or isinstance(args[0], FraySome) and args[0].val is None


def _builtin_is_ok(args: list[Any]) -> bool:
    if len(args) != 1:
        raise FrayTypeError("isOk() takes exactly one argument")
    return isinstance(args[0], FrayOk)


def _builtin_is_err(args: list[Any]) -> bool:
    if len(args) != 1:
        raise FrayTypeError("isErr() takes exactly one argument")
    return isinstance(args[0], FrayErr)


def _builtin_sum(args: list[Any]) -> Any:
    if len(args) != 1:
        raise FrayTypeError("sum() takes exactly one argument")
    val = args[0]
    if isinstance(val, FrayList):
        total = 0
        for e in val.elements:
            total = total + e
        return total
    raise FrayTypeError("sum() argument must be a list")


def _builtin_abs(args: list[Any]) -> Any:
    if len(args) != 1:
        raise FrayTypeError("abs() takes exactly one argument")
    return abs(args[0])


def _builtin_sqrt(args: list[Any]) -> float:
    if len(args) != 1:
        raise FrayTypeError("sqrt() takes exactly one argument")
    return math.sqrt(args[0])


def _builtin_isqrt(args: list[Any]) -> int:
    if len(args) != 1:
        raise FrayTypeError("isqrt() takes exactly one argument")
    return math.isqrt(args[0])


def _builtin_round(args: list[Any]) -> int:
    if len(args) != 1:
        raise FrayTypeError("round() takes exactly one argument")
    return round(args[0])


def _builtin_int(args: list[Any]) -> int:
    if len(args) != 1:
        raise FrayTypeError("int() takes exactly one argument")
    val = args[0]
    if isinstance(val, str):
        return int(val)
    if isinstance(val, float):
        return int(val)
    if isinstance(val, int):
        return val
    raise FrayTypeError(f"int() not supported for {type(val).__name__}")


def _builtin_float(args: list[Any]) -> float:
    if len(args) != 1:
        raise FrayTypeError("float() takes exactly one argument")
    val = args[0]
    if isinstance(val, str):
        return float(val)
    if isinstance(val, (int, float)):
        return float(val)
    raise FrayTypeError(f"float() not supported for {type(val).__name__}")


def _builtin_str(args: list[Any]) -> str:
    if len(args) != 1:
        raise FrayTypeError("str() takes exactly one argument")
    return _repr(args[0])


# ── Program arguments (oracle side) ──
# tools/fray_oracle.py wires sys.argv in before running, so progName()/args()
# see the same command line the native runtime's fray_init_args stored. The
# defaults keep direct `run()` calls (tests, REPL) argument-free.

_ORACLE_PROG_NAME = ""
_ORACLE_ARGV: list[str] = []


def set_oracle_argv(prog_name: str, argv: list[str]) -> None:
    global _ORACLE_PROG_NAME, _ORACLE_ARGV
    _ORACLE_PROG_NAME = prog_name
    _ORACLE_ARGV = list(argv)


def _builtin_prog_name(args: list[Any]) -> str:
    return _ORACLE_PROG_NAME


def _builtin_program_args(args: list[Any]) -> FrayList:
    return FrayList(list(_ORACLE_ARGV))


# ── Files (oracle side of the runtime's fray_file_* in builtins.c) ──

def _builtin_file_read(args: list[Any]) -> str:
    if len(args) != 1 or not isinstance(args[0], str):
        raise FrayTypeError("fileRead() takes a path string")
    try:
        with open(args[0], "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        raise FrayValueError("no such file") from None


def _builtin_file_exists(args: list[Any]) -> bool:
    if len(args) != 1 or not isinstance(args[0], str):
        raise FrayTypeError("fileExists() takes a path string")
    return os.path.exists(args[0])


def _builtin_file_write(args: list[Any]) -> None:
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise FrayTypeError("fileWrite() takes (path, string)")
    try:
        with open(args[0], "w", encoding="utf-8") as f:
            f.write(args[1])
    except OSError:
        raise FrayValueError("cannot open file for writing") from None


def _builtin_list_dir(args: list[Any]) -> FrayList:
    if len(args) != 1 or not isinstance(args[0], str):
        raise FrayTypeError("listDir() takes a path string")
    try:
        names = sorted(os.listdir(args[0]))
    except OSError:
        raise FrayValueError("cannot read directory") from None
    return FrayList([n for n in names if not n.startswith(".")])


def _builtin_is_dir(args: list[Any]) -> bool:
    if len(args) != 1 or not isinstance(args[0], str):
        raise FrayTypeError("isDir() takes a path string")
    return os.path.isdir(args[0])


def _builtin_exit(args: list[Any]) -> None:
    if len(args) > 1 or (len(args) == 1 and not isinstance(args[0], int)):
        raise FrayTypeError("exit() expects an int")
    raise FrayProgramExit(args[0] if args else 0)


def _builtin_range(args: list[Any]) -> FrayList:
    if len(args) == 1:
        return FrayList(list(range(args[0])))
    elif len(args) == 2:
        return FrayList(list(range(args[0], args[1])))
    elif len(args) == 3:
        return FrayList(list(range(args[0], args[1], args[2])))
    raise FrayTypeError("range() takes 1 to 3 arguments")


def _builtin_mean(args: list[Any]) -> float:
    if len(args) != 1 or not isinstance(args[0], FrayList):
        raise FrayTypeError("mean() takes a list")
    elements = args[0].elements
    if not elements:
        raise FrayValueError("mean() of empty list")
    return sum(elements) / len(elements)


def _builtin_med(args: list[Any]) -> float:
    """Statistical median (sorts values)."""
    if len(args) != 1 or not isinstance(args[0], FrayList):
        raise FrayTypeError("med() takes a list")
    elements = sorted(args[0].elements)
    if not elements:
        raise FrayValueError("med() of empty list")
    n = len(elements)
    if n % 2 == 1:
        return elements[n // 2]
    return (elements[n // 2 - 1] + elements[n // 2]) / 2


def _builtin_mid(args: list[Any]) -> Any:
    """Positional middle element."""
    if len(args) != 1 or not isinstance(args[0], FrayList):
        raise FrayTypeError("mid() takes a list")
    elements = args[0].elements
    if not elements:
        raise FrayValueError("mid() of empty list")
    return elements[len(elements) // 2]


def _builtin_mode(args: list[Any]) -> Any:
    if len(args) != 1 or not isinstance(args[0], FrayList):
        raise FrayTypeError("mode() takes a list")
    elements = args[0].elements
    if not elements:
        raise FrayValueError("mode() of empty list")
    from collections import Counter
    counts = Counter(elements)
    return counts.most_common(1)[0][0]


# ── Threads & atomics (Phase 6) ──
# The oracle uses Python threads; the product uses OS threads in the runtime.
# Only observable semantics must match: spawn runs the function, join waits,
# atomic boxes serialize payload access.

class FrayAtomic:
    """A boxed int/float/bool with serialized payload access."""
    def __init__(self, initial: Any):
        if not isinstance(initial, (int, float, bool)):
            raise FrayTypeError("atomic expects int, float or bool")
        self._lock = threading.Lock()
        self._value = initial

    def get(self) -> Any:
        with self._lock:
            return self._value

    def set(self, val: Any) -> None:
        if not isinstance(val, (int, float, bool)):
            raise FrayTypeError("atomic set expects int, float or bool")
        with self._lock:
            self._value = val

    def add(self, delta: Any) -> Any:
        if not isinstance(delta, (int, float, bool)):
            raise FrayTypeError("atomic add expects int, float or bool")
        with self._lock:
            self._value = self._value + delta
            return self._value

    def __repr__(self):
        return _repr(self.get())


def _builtin_spawn(args: list[Any]) -> int:
    if len(args) != 1 or not isinstance(args[0], FrayFunction):
        raise FrayTypeError("spawn() takes a function")
    fn = args[0]
    if fn.params:
        raise FrayTypeError("spawned function must take no parameters")

    def runner():
        # Each thread gets a child environment; output shares the process
        # stdout like the native runtime shares the process stdout.
        env = Environment(parent=fn.closure)
        sub = Evaluator(env=env)
        try:
            sub._call_function(fn, [])
        except FrayError:
            pass  # uncaught exceptions in threads print nothing (like native)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    with _THREADS_LOCK:
        _THREADS.append(t)
        return len(_THREADS)


def _builtin_join(args: list[Any]) -> None:
    if len(args) != 1 or not isinstance(args[0], int):
        raise FrayTypeError("join() takes a thread id")
    with _THREADS_LOCK:
        idx = args[0] - 1
        t = _THREADS[idx] if 0 <= idx < len(_THREADS) else None
    if t is not None:
        t.join()


_THREADS: list[threading.Thread] = []
_THREADS_LOCK = threading.Lock()


def _builtin_join_all(args: list[Any]) -> None:
    with _THREADS_LOCK:
        threads = list(_THREADS)
    for t in threads:
        t.join()


def _builtin_atomic_new(args: list[Any]) -> FrayAtomic:
    if len(args) != 1:
        raise FrayTypeError("atomic() takes exactly one argument")
    return FrayAtomic(args[0])


def _atomic_method(atomic: FrayAtomic, method: str):
    def call(args: list[Any]) -> Any:
        if method == "get":
            return atomic.get()
        if method == "set":
            if len(args) != 1:
                raise FrayTypeError("set() takes exactly one argument")
            atomic.set(args[0])
            return None
        if method == "add":
            if len(args) != 1:
                raise FrayTypeError("add() takes exactly one argument")
            return atomic.add(args[0])
        raise FrayTypeError(f"atomic has no method '{method}'")
    return call


BUILTINS = {
    "print": _builtin_print,
    "input": _builtin_input,
    "inputStr": _builtin_input_str,
    "inputInt": _builtin_input_int,
    "inputFloat": _builtin_input_float,
    "len": _builtin_len,
    "min": _builtin_min,
    "max": _builtin_max,
    "ord": _builtin_ord,
    "chr": _builtin_chr,
    "Some": _builtin_some,
    "Ok": _builtin_ok,
    "Err": _builtin_err,
    "unwrap": _builtin_unwrap,
    "isSome": _builtin_is_some,
    "isNone": _builtin_is_none_val,
    "isOk": _builtin_is_ok,
    "isErr": _builtin_is_err,
    "sum": _builtin_sum,
    "abs": _builtin_abs,
    "sqrt": _builtin_sqrt,
    "isqrt": _builtin_isqrt,
    "round": _builtin_round,
    "int": _builtin_int,
    "float": _builtin_float,
    "str": _builtin_str,
    "range": _builtin_range,
    "mean": _builtin_mean,
    "med": _builtin_med,
    "mid": _builtin_mid,
    "mode": _builtin_mode,
    "spawn": _builtin_spawn,
    "join": _builtin_join,
    "joinAll": _builtin_join_all,
    "atomic": _builtin_atomic_new,
    "progName": _builtin_prog_name,
    "programArgs": _builtin_program_args,
    "fileRead": _builtin_file_read,
    "fileExists": _builtin_file_exists,
    "fileWrite": _builtin_file_write,
    "listDir": _builtin_list_dir,
    "isDir": _builtin_is_dir,
    "exit": _builtin_exit,
}

# Phase 7 builtins are registered after their definitions (bottom of file).

# ── Coroutines & channels (Phase 7) ──
# Oracle strategy: coroutines run as Python generators on a simple
# round-robin scheduler. Observably this matches the native runtime's
# fiber scheduler: async calls interleave, await suspends until the
# target completes, channels rendezvous, sleep orders by deadline.

class FrayChannel:
    def __init__(self, capacity: int = 16):
        self._buf: list[Any] = []
        self._cap = capacity
        self._closed = False
        self._lock = threading.Lock()

    def send(self, v: Any) -> None:
        # Block (poll) while full — matches fiber suspension observably.
        while not self._closed and len(self._buf) >= self._cap:
            time.sleep(0.001)
        if self._closed:
            raise FrayTypeError("send on closed channel")
        with self._lock:
            self._buf.append(v)

    def recv(self) -> Any:
        while len(self._buf) == 0 and not self._closed:
            time.sleep(0.001)
        with self._lock:
            if len(self._buf) > 0:
                return self._buf.pop(0)
        return None  # closed and drained

    def close(self) -> None:
        self._closed = True


class FrayCoroutine:
    """A started coroutine (oracle model).

    The body runs on its own daemon thread — the closest observable
    match to the native fiber model: coroutines run to completion or
    until they block on a channel/timer, letting others progress; `await`
    joins the target coroutine. Output and value semantics match the
    native runtime; interleaving is whatever the OS scheduler picks
    (identical to Phase 6 thread semantics)."""
    _next_id = 0

    def __init__(self, run_body):
        FrayCoroutine._next_id += 1
        self.id = FrayCoroutine._next_id
        self._run_body = run_body
        self._result = None
        self._error: Optional[Exception] = None
        self._done = threading.Event()
        with _CORO_LOCK:
            _COROUTINES.append(self)

    def start(self):
        def runner():
            try:
                self._result = self._run_body()
            except FrayError as exc:
                self._error = exc
            finally:
                self._done.set()
                with _CORO_LOCK:
                    if self in _COROUTINES:
                        _COROUTINES.remove(self)
        t = threading.Thread(target=runner, daemon=True)
        t.start()

    @property
    def done(self):
        return self._done.is_set()

    def join_value(self):
        self._done.wait()
        if self._error is not None:
            raise self._error
        return self._result


_COROUTINES: list[FrayCoroutine] = []
_CORO_LOCK = threading.Lock()


def _sched_tick():
    """No-op in the thread-based oracle (threads schedule themselves)."""
    return


def _coro_wait_all():
    """Join every outstanding coroutine (program-exit barrier)."""
    while True:
        with _CORO_LOCK:
            pending = [c for c in _COROUTINES if not c.done]
        if not pending:
            return
        for c in pending:
            c.join_value()


def _run_async_call(fn: FrayFunction, args: list[Any]):
    """Return a zero-arg callable that runs the async fn's body with
    `await` resolved through the caller's coroutine machinery."""
    def run_body():
        env = Environment(parent=fn.closure)
        for param, arg in zip(fn.params, args):
            env.define(param, arg)
        sub = Evaluator(env=env)
        result = sub._call_body(fn, args)
        return result
    return run_body


def _builtin_sleep(args: list[Any]) -> None:
    if len(args) != 1 or not isinstance(args[0], (int, float)):
        raise FrayTypeError("sleep() takes a number of milliseconds")
    time.sleep(max(0.0, args[0]) / 1000.0)


def _builtin_channel(args: list[Any]) -> FrayChannel:
    cap = 16
    if len(args) == 1 and isinstance(args[0], int):
        cap = max(0, args[0])
    return FrayChannel(cap)


def _builtin_send(args: list[Any]) -> None:
    if len(args) != 2 or not isinstance(args[0], FrayChannel):
        raise FrayTypeError("send() takes (channel, value)")
    args[0].send(args[1])


def _builtin_recv(args: list[Any]) -> Any:
    if len(args) != 1 or not isinstance(args[0], FrayChannel):
        raise FrayTypeError("recv() takes a channel")
    return args[0].recv()


def _builtin_close(args: list[Any]) -> None:
    if len(args) != 1 or not isinstance(args[0], FrayChannel):
        raise FrayTypeError("close() takes a channel")
    args[0].close()


# ── Non-blocking file & socket I/O (oracle side of runtime/io.c) ──
#
# The oracle runs every `async def` body on its own thread, so a blocking
# call here IS the non-parallel call there: the compiled engine parks the
# fiber on a worker, the oracle simply blocks its thread. Both produce the
# same values in the same order, which is what the differential test needs.
# Sockets are exposed to the program as integers, matching the runtime's file
# descriptors, and the objects behind them live in this table.

_SOCKETS: dict[int, Any] = {}
_SOCKET_LOCK = threading.Lock()
_SOCKET_NEXT = [1000]


def _socket_put(sock: Any) -> int:
    with _SOCKET_LOCK:
        handle = _SOCKET_NEXT[0]
        _SOCKET_NEXT[0] += 1
        _SOCKETS[handle] = sock
    return handle


def _socket_get(handle: Any, who: str) -> Any:
    if not isinstance(handle, int) or isinstance(handle, bool):
        raise FrayTypeError(f"{who}() takes a socket handle")
    with _SOCKET_LOCK:
        sock = _SOCKETS.get(handle)
    if sock is None:
        raise FrayValueError("not a socket")
    return sock


def _builtin_read_file_async(args: list[Any]) -> str:
    if len(args) != 1 or not isinstance(args[0], str):
        raise FrayTypeError("readFileAsync() takes a path string")
    try:
        with open(args[0], "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        raise FrayValueError("no such file") from None


def _builtin_write_file_async(args: list[Any]) -> int:
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], str):
        raise FrayTypeError("writeFileAsync() takes (path, string)")
    try:
        with open(args[0], "w", encoding="utf-8") as f:
            f.write(args[1])
    except OSError:
        raise FrayValueError("cannot open file for writing") from None
    return len(args[1])


def _builtin_read_async(args: list[Any]) -> str:
    if len(args) != 2 or not isinstance(args[1], int) or isinstance(args[1], bool):
        raise FrayTypeError("readAsync() takes (socket, count)")
    sock = _socket_get(args[0], "readAsync")
    if args[1] <= 0:
        raise FrayValueError("readAsync() count must be positive")
    data = sock.recv(args[1])          # b"" on a clean close
    return data.decode("utf-8", errors="replace")


def _builtin_write_async(args: list[Any]) -> int:
    if len(args) != 2 or not isinstance(args[1], str):
        raise FrayTypeError("writeAsync() takes (socket, string)")
    sock = _socket_get(args[0], "writeAsync")
    payload = args[1].encode("utf-8")
    sock.sendall(payload)
    return len(payload)


def _builtin_tcp_listen(args: list[Any]) -> int:
    if len(args) != 1 or not isinstance(args[0], int) or isinstance(args[0], bool):
        raise FrayTypeError("tcpListen() takes a port")
    if not 0 <= args[0] <= 65535:
        raise FrayValueError("port must be 0..65535")
    import socket as _sock_mod
    srv = _sock_mod.socket(_sock_mod.AF_INET, _sock_mod.SOCK_STREAM)
    srv.setsockopt(_sock_mod.SOL_SOCKET, _sock_mod.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", args[0]))
        srv.listen(16)
    except OSError:
        srv.close()
        raise FrayValueError("cannot listen on port") from None
    return _socket_put(srv)


def _builtin_tcp_accept(args: list[Any]) -> int:
    if len(args) != 1:
        raise FrayTypeError("tcpAccept() takes a server socket")
    srv = _socket_get(args[0], "tcpAccept")
    try:
        conn, _addr = srv.accept()
    except OSError:
        raise FrayRuntimeError("accept failed") from None
    return _socket_put(conn)


def _builtin_tcp_connect(args: list[Any]) -> int:
    if len(args) != 2 or not isinstance(args[0], str):
        raise FrayTypeError("tcpConnect() host must be a string")
    if not isinstance(args[1], int) or isinstance(args[1], bool):
        raise FrayTypeError("tcpConnect() takes a port")
    import socket as _sock_mod
    sock = _sock_mod.socket(_sock_mod.AF_INET, _sock_mod.SOCK_STREAM)
    try:
        sock.connect((args[0], args[1]))
    except ConnectionRefusedError:
        sock.close()
        raise FrayValueError("connection refused") from None
    except _sock_mod.gaierror:
        sock.close()
        raise FrayValueError("cannot resolve host") from None
    except OSError:
        sock.close()
        raise FrayValueError("connection failed") from None
    return _socket_put(sock)


def _builtin_close_socket(args: list[Any]) -> None:
    if len(args) != 1:
        raise FrayTypeError("closeSocket() takes a socket")
    sock = _socket_get(args[0], "closeSocket")
    with _SOCKET_LOCK:
        _SOCKETS.pop(args[0], None)
    sock.close()


def _builtin_tcp_port(args: list[Any]) -> int:
    if len(args) != 1:
        raise FrayTypeError("tcpPort() takes a socket")
    sock = _socket_get(args[0], "tcpPort")
    try:
        return sock.getsockname()[1]
    except OSError:
        raise FrayValueError("not a socket") from None


def _builtin_yield_now(args: list[Any]) -> None:
    time.sleep(0)


def _builtin_coro_count(args: list[Any]) -> int:
    with _CORO_LOCK:
        return len(_COROUTINES)


def _builtin_run_until_complete(args: list[Any]) -> None:
    """Wait for all running coroutines to finish.

    Matches fray_coro_run_until_complete_shim in the native runtime.
    """
    deadline = time.monotonic() + 10.0  # 10 s safety timeout
    while True:
        with _CORO_LOCK:
            pending = list(_COROUTINES)
        if not pending:
            break
        if time.monotonic() > deadline:
            break
        time.sleep(0.001)


BUILTINS.update({
    "sleep": _builtin_sleep,
    "channel": _builtin_channel,
    "send": _builtin_send,
    "recv": _builtin_recv,
    "close": _builtin_close,
    "yieldNow": _builtin_yield_now,
    "coroCount": _builtin_coro_count,
    "runUntilComplete": _builtin_run_until_complete,
    "readFileAsync": _builtin_read_file_async,
    "writeFileAsync": _builtin_write_file_async,
    "readAsync": _builtin_read_async,
    "writeAsync": _builtin_write_async,
    "tcpListen": _builtin_tcp_listen,
    "tcpAccept": _builtin_tcp_accept,
    "tcpConnect": _builtin_tcp_connect,
    "closeSocket": _builtin_close_socket,
    "tcpPort": _builtin_tcp_port,
})

CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}


# ── Evaluator ──

def _check_arity(func: "FrayFunction", args: list[Any]):
    """Reject a call whose argument count disagrees with the declaration.

    Without this a missing argument surfaces later as a confusing "name is
    not defined" (the parameter is simply never bound) and an extra one is
    silently dropped, because parameters are bound with `zip`."""
    if len(args) != len(func.params):
        raise FrayTypeError(
            f"'{func.name}' takes {len(func.params)} argument(s) but "
            f"{len(args)} given")


def _bind_params(func: "FrayFunction", args: list[Any], env: "Environment"):
    """Bind a call's arguments to the function's parameters."""
    _check_arity(func, args)
    for param, arg in zip(func.params, args):
        env.define(param, arg)


class Evaluator:
    def __init__(self, env: Optional[Environment] = None, filename: str = "<string>"):
        self.env = env or Environment()
        self.filename = filename
        # Every module path resolves under one root — the program's directory
        # — so a module's own imports mean the same thing at any depth.
        self._root = (os.path.dirname(os.path.abspath(filename))
                      if filename and not filename.startswith("<") else os.getcwd())
        self._package = ""  # the program itself has no package
        # Register builtins
        for name, fn in BUILTINS.items():
            self.env.define(name, fn)
        for name, val in CONSTANTS.items():
            self.env.define(name, val, is_const=True)

    def run(self, program: Program) -> None:
        """Execute a Program AST, then drain all coroutines (the native
        runtime's run-until-complete at program exit)."""
        for stmt in program.body:
            self._exec(stmt)
        _coro_wait_all()

    def _exec(self, node: Node):
        """Execute a statement."""
        if isinstance(node, FunctionDef) and getattr(node, "is_async", False):
            fn = FrayFunction(node.name, node.params, node.body, self.env,
                              is_async=True)
            self.env.define(node.name, fn)
            return
        if isinstance(node, Assignment):
            val = self._eval(node.value)
            self._assign_target(node.target, val)
        elif isinstance(node, AugmentedAssignment):
            target_val = self._eval(node.target)
            val = self._eval(node.value)
            # Extract base operator from augmented assignment (e.g., "+=" → "+")
            base_op = node.op[:-1]  # remove trailing "="
            result = self._binop(base_op, target_val, val)
            self._assign_target(node.target, result)
        elif isinstance(node, ExprStatement):
            self._eval(node.expr)
        elif isinstance(node, ReturnStatement):
            val = self._eval(node.value) if node.value is not None else None
            raise _Return(val)
        elif isinstance(node, ConstStatement):
            val = self._eval(node.value)
            self.env.define(node.name, val, is_const=True)
        elif isinstance(node, ImportStatement):
            self._exec_import(node)
        elif isinstance(node, ExternFuncDecl):
            self._exec_extern(node)
        elif isinstance(node, FunctionDef):
            fn = FrayFunction(node.name, node.params, node.body, self.env)
            self.env.define(node.name, fn)
        elif isinstance(node, StructDef):
            # Evaluate default values in the current environment
            fields = []
            for f in node.fields:
                if f.default_value is not None:
                    default = self._eval(f.default_value)
                else:
                    default = SENTINEL
                fields.append((f.name, default))
            self.env.define(node.name, FrayStructDef(node.name, fields))
        elif isinstance(node, EnumDef):
            cases = [(c.name, c.params) for c in node.cases]
            enum_def = FrayEnumDef(node.name, cases)
            self.env.define(node.name, enum_def)
            # Also define each simple (no-param) variant directly
            for vname, vparams in cases:
                if not vparams:
                    variant_obj = FrayEnum(enum_def, vname, [])
                    self.env.define(f"{node.name}.{vname}", variant_obj)
        elif isinstance(node, IfStatement):
            self._exec_if(node)
        elif isinstance(node, ForLoop):
            self._exec_for(node)
        elif isinstance(node, WhileLoop):
            self._exec_while(node)
        elif isinstance(node, TryStatement):
            self._exec_try(node)
        elif isinstance(node, BreakStatement):
            raise _Break()
        elif isinstance(node, ContinueStatement):
            raise _Continue()
        elif isinstance(node, DelStatement):
            self._exec_del(node)
        elif isinstance(node, MatchExpression):
            self._eval(node)

    def _load_module(self, name: str, module_path: str, is_package: bool) -> "FrayModule":
        """Execute a module (once) and return its exported names.

        The module's *package* is what its own relative imports resolve
        against: a package initializer is its own package, a plain module's
        package is the directory holding it.
        """
        cached = _module_cache.get(module_path)
        if cached is not None:
            return cached

        with open(module_path, encoding="utf-8") as f:
            source = f.read()

        # Cache the module before executing it: a package initializer that
        # imports one of its own submodules (`from . import util`) resolves the
        # package again while it is still being loaded, and must see this same
        # object rather than re-executing the initializer.
        mod = FrayModule(name, {})
        _module_cache[module_path] = mod

        # Execute in an isolated environment
        mod_env = Environment()
        for builtin_name, fn in BUILTINS.items():
            mod_env.define(builtin_name, fn)

        tokens = tokenize(source, module_path)
        ast = parse(tokens, module_path)

        # Execute top-level statements
        saved_env = self.env
        saved_file = self.filename
        saved_pkg = self._package
        self.env = mod_env
        self.filename = module_path
        self._package = modules.package_of(name, is_package)
        try:
            for stmt in ast.body:
                self._exec(stmt)
        finally:
            self.env = saved_env
            self.filename = saved_file
            self._package = saved_pkg

        # Collect top-level definitions (skip builtins); the names a module
        # imported with `from` are part of what it re-exports.
        mod.attrs = {k: v for k, v in mod_env.vars.items()
                     if k not in BUILTINS}
        return mod

    def _exec_import(self, node: ImportStatement):
        """Execute an import statement — load .fray file and make definitions available.

        A module path resolves under the program's root either as a plain
        module file (`pkg/util.fray`) or as a package initializer
        (`pkg/__init__.fray`), and leading dots make it relative to the
        importing module's package. A package initializer's own imports become
        its exports, so `from .util import twice` in `pkg/__init__.fray` makes
        `from pkg import twice` work.
        """
        target = modules.resolve_relative(node.module, node.level, self._package)
        if target is None:
            if node.level:
                raise FrayImportError(
                    f"attempted relative import with no known parent package: "
                    f"{'.' * node.level}{node.module}")
            raise FrayImportError("import needs a module path")

        module_path, is_package = modules.find_module_file(self._root, target)
        if module_path is None:
            looked = os.path.join(self._root, target.replace(".", os.sep) + ".fray")
            raise FrayImportError(
                f"module '{target}' not found (looked for {os.path.normpath(looked)})")

        mod = self._load_module(target, module_path, is_package)

        # Handle import forms
        if node.from_import:
            # `from X import a, b` — bind names directly. An imported name is
            # either a name X exports or, for a package, one of its submodules.
            for name in node.names:
                if name in mod.attrs:
                    self.env.define(name, mod.attrs[name])
                    continue
                sub_name = f"{target}.{name}"
                sub_path, sub_is_pkg = modules.find_module_file(self._root, sub_name)
                if sub_path is not None:
                    self.env.define(name, self._load_module(sub_name, sub_path, sub_is_pkg))
                    continue
                raise FrayImportError(f"'{target}' has no exported '{name}'")
        else:
            # `import X` — bind the module object. For a dotted path the leaf
            # module is bound under the first segment (`import a.b` binds `a`).
            short_name = node.module.split(".")[0]
            self.env.define(short_name, mod)

    def _exec_extern(self, node: ExternFuncDecl):
        """Register an extern C function via ctypes."""
        import ctypes
        import ctypes.util

        # Type mapping: fray type name → ctypes type
        TYPE_MAP = {
            "int": ctypes.c_int64,
            "i64": ctypes.c_int64,
            "i32": ctypes.c_int32,
            "i16": ctypes.c_int16,
            "i8": ctypes.c_int8,
            "u8": ctypes.c_uint8,
            "u16": ctypes.c_uint16,
            "u32": ctypes.c_uint32,
            "u64": ctypes.c_uint64,
            "float": ctypes.c_double,
            "f64": ctypes.c_double,
            "f32": ctypes.c_float,
            "bool": ctypes.c_bool,
            "string": ctypes.c_char_p,
            "char": ctypes.c_char,
            "ptr": ctypes.c_void_p,
            "void": None,
        }

        # Find libc
        libc_path = ctypes.util.find_library("c")
        if not libc_path:
            libc_path = "msvcrt" if __import__('sys').platform == 'win32' else "libc.so.6"
        libc = ctypes.CDLL(libc_path)

        # Map return type
        ret_ctype = TYPE_MAP.get(node.return_type, ctypes.c_int64)
        # Map param types
        arg_ctype = [TYPE_MAP.get(t, ctypes.c_int64) for t in node.param_types]

        # Create wrapper
        func = getattr(libc, node.name)
        func.restype = ret_ctype
        func.argtypes = arg_ctype

        def wrapper(args):
            # Convert fray values to C types
            c_args = []
            for i, (arg, atype) in enumerate(zip(args, node.param_types)):
                if atype in ("string",):
                    if isinstance(arg, str):
                        c_args.append(arg.encode('utf-8'))
                    elif arg is None:
                        c_args.append(None)
                    else:
                        c_args.append(str(arg).encode('utf-8'))
                elif atype in ("bool",):
                    c_args.append(bool(arg))
                elif atype in ("ptr",):
                    c_args.append(ctypes.c_void_p(arg) if isinstance(arg, int) else None)
                else:
                    c_args.append(int(arg))
            result = func(*c_args)
            # Convert result back to fray value
            if node.return_type == "void":
                return None
            if node.return_type == "string":
                return result.decode('utf-8') if result else None
            if node.return_type == "bool":
                return bool(result)
            return result

        self.env.define(node.name, wrapper)

    def _exec_del(self, node: DelStatement):
        target = node.target
        if isinstance(target, Identifier):
            self.env.undef(target.name)
        elif isinstance(target, Index):
            obj = self._eval(target.obj)
            key = self._eval(target.index_expr)
            if isinstance(obj, FrayMap):
                obj.delete(key)
            elif isinstance(obj, FrayList):
                idx = key
                if idx < 0:
                    idx += len(obj.elements)
                if idx < 0 or idx >= len(obj.elements):
                    raise FrayIndexError(f"list index {idx} out of range")
                obj.elements.pop(idx)
            else:
                raise FrayTypeError(f"cannot delete from {type(obj).__name__}")
        else:
            raise FrayTypeError(f"invalid del target")

    def _slice_string(self, s: str, start, stop, step):
        if step is not None and step == 0:
            raise FrayTypeError("ValueError: slice step cannot be zero")
        s_len = len(s)
        step = step if step is not None else 1
        if step > 0:
            start = start if start is not None else 0
            stop = stop if stop is not None else s_len
            if start < 0: start += s_len
            if stop < 0: stop += s_len
            start = max(0, min(s_len, start))
            stop = max(0, min(s_len, stop))
        else:
            start = start if start is not None else s_len - 1
            stop = stop if stop is not None else -1
            if start < 0: start += s_len
            if stop < 0: stop += s_len
            start = max(0, min(s_len - 1, start))
            stop = max(-1, min(s_len, stop))
        result = []
        i = start
        if step > 0:
            while i < stop:
                result.append(s[i])
                i += step
        else:
            while i > stop:
                result.append(s[i])
                i += step
        return "".join(result)

    def _slice_list(self, lst: FrayList, start, stop, step):
        if step is not None and step == 0:
            raise FrayTypeError("ValueError: slice step cannot be zero")
        elems = lst.elements
        e_len = len(elems)
        step = step if step is not None else 1
        if step > 0:
            start = start if start is not None else 0
            stop = stop if stop is not None else e_len
            if start < 0: start += e_len
            if stop < 0: stop += e_len
            start = max(0, min(e_len, start))
            stop = max(0, min(e_len, stop))
        else:
            start = start if start is not None else e_len - 1
            stop = stop if stop is not None else -1
            if start < 0: start += e_len
            if stop < 0: stop += e_len
            start = max(0, min(e_len - 1, start))
            stop = max(-1, min(e_len, stop))
        result = []
        i = start
        if step > 0:
            while i < stop:
                result.append(elems[i])
                i += step
        else:
            while i > stop:
                result.append(elems[i])
                i += step
        return FrayList(result)

    def _assign_target(self, target: Node, val: Any):
        if isinstance(target, Identifier):
            self.env.set(target.name, val)
        elif isinstance(target, MemberAccess):
            obj = self._eval(target.obj)
            if isinstance(obj, FrayStruct):
                obj.set(target.attr, val)
            elif isinstance(obj, FrayMap):
                obj[target.attr] = val
            else:
                setattr(obj, target.attr, val)
        elif isinstance(target, Index):
            obj = self._eval(target.obj)
            idx = self._eval(target.index_expr)
            if isinstance(obj, FrayMap):
                obj[idx] = val
            else:
                obj[idx] = val

    def _exec_if(self, node: IfStatement):
        for cond, body in node.branches:
            if cond is not None:
                val = self._eval(cond)
                if _is_truthy(val):
                    self._exec_block(body)
                    return
            else:
                self._exec_block(body)
                return
        if node.else_body:
            self._exec_block(node.else_body)

    def _exec_for(self, node: ForLoop):
        iterable = self._eval(node.iterable)
        if isinstance(iterable, FrayList):
            items = iterable.elements
        elif isinstance(iterable, FraySet):
            items = iterable.elements
        elif isinstance(iterable, FrayTuple):
            items = list(iterable.elements)
        elif isinstance(iterable, FrayMap):
            items = [iterable._data[h][0] for h in iterable._order]
        elif isinstance(iterable, str):
            items = list(iterable)
        elif isinstance(iterable, range):
            items = list(iterable)
        else:
            raise FrayTypeError(f"cannot iterate over {type(iterable).__name__}")

        last_value = None
        for item in items:
            last_value = item
            try:
                loop_env = Environment(parent=self.env)
                loop_env.define(node.var, item)
                old_env = self.env
                self.env = loop_env
                self._exec_block(node.body)
            except _Break:
                self.env = old_env
                break
            except _Continue:
                self.env = old_env
                continue
            finally:
                self.env = old_env
        # Loop variable persists after the loop (like Python)
        if items:
            self.env.set(node.var, last_value)

    def _exec_while(self, node: WhileLoop):
        while True:
            val = self._eval(node.condition)
            if not _is_truthy(val):
                break
            try:
                self._exec_block(node.body)
            except _Break:
                break
            except _Continue:
                continue

    def _exec_try(self, node: TryStatement):
        try:
            self._exec_block(node.body)
        except _Return:
            raise  # don't catch return/break/continue
        except _Break:
            raise
        except _Continue:
            raise
        except (FrayTypeError, FrayValueError,
                FrayIndexError, FrayNameError, FrayZeroDivisionError) as exc:
            caught = False
            for exc_type, body in node.except_clauses:
                if exc_type is None:
                    self._exec_block(body)
                    caught = True
                    break
                if exc_type == "TypeError" and isinstance(exc, FrayTypeError):
                    self._exec_block(body)
                    caught = True
                    break
                elif exc_type == "ValueError" and isinstance(exc, FrayValueError):
                    self._exec_block(body)
                    caught = True
                    break
                elif exc_type == "IndexError" and isinstance(exc, FrayIndexError):
                    self._exec_block(body)
                    caught = True
                    break
                elif exc_type == "NameError" and isinstance(exc, FrayNameError):
                    self._exec_block(body)
                    caught = True
                    break
                elif exc_type == "ZeroDivisionError" and isinstance(exc, FrayZeroDivisionError):
                    self._exec_block(body)
                    caught = True
                    break
                elif exc_type in ("RuntimeError", "Exception"):
                    self._exec_block(body)
                    caught = True
                    break
            if not caught:
                raise
        finally:
            if node.finally_body:
                self._exec_block(node.finally_body)

    def _call_body(self, func: FrayFunction, args: list[Any]) -> Any:
        """Run a function body synchronously (inside a coroutine fiber)."""
        func_env = Environment(parent=func.closure)
        _bind_params(func, args, func_env)
        old_env = self.env
        self.env = func_env
        try:
            for stmt in func.body:
                self._exec(stmt)
            return None
        except _Return as ret:
            return ret.value
        finally:
            self.env = old_env

    def _eval_await(self, value_node: Optional[Node]) -> Any:
        """Evaluate `await e`.

        e evaluates to a coroutine (async call) or None. Inside a
        coroutine, suspension means yielding to the scheduler generator
        until the target completes; outside, block (join semantics)."""
        target = self._eval(value_node) if value_node is not None else None
        if target is None:
            return None
        if not isinstance(target, FrayCoroutine):
            raise FrayTypeError("await expects a coroutine (call an async function)")
        # Deterministic depth-first resolution: drive the target to
        # completion, then return its result.
        return target.join_value()

    def _call_function(self, func: FrayFunction, args: list[Any]) -> Any:
        """Call a user-defined function."""
        # Checked before the async branch too: an async call's arguments are
        # bound inside the coroutine, where the error would be swallowed.
        _check_arity(func, args)
        if getattr(func, "is_async", False):
            # Async call: start a coroutine, return the handle immediately.
            run_body = _run_async_call(func, args)   # zero-arg callable
            coro = FrayCoroutine(run_body)
            coro.start()
            return coro
        # Create a new scope with the function's closure as parent
        func_env = Environment(parent=func.closure)
        # Bind parameters
        for param, arg in zip(func.params, args):
            func_env.define(param, arg)
        # Execute the function body
        old_env = self.env
        self.env = func_env
        try:
            for stmt in func.body:
                self._exec(stmt)
            return None  # implicit return None
        except _Return as ret:
            return ret.value
        finally:
            self.env = old_env

    def _exec_block(self, stmts: list[Node]):
        """Execute a list of statements in a new scope."""
        block_env = Environment(parent=self.env)
        old_env = self.env
        self.env = block_env
        try:
            for stmt in stmts:
                self._exec(stmt)
        finally:
            self.env = old_env

    # ── Expression evaluation ──

    def _eval(self, node: Optional[Node]) -> Any:
        if node is None:
            return None

        if isinstance(node, IntLiteral):
            return node.value
        if isinstance(node, FloatLiteral):
            return node.value
        if isinstance(node, StringLiteral):
            return node.value
        if isinstance(node, BoolLiteral):
            return node.value
        if isinstance(node, ConstantLiteral):
            if node.name == "null":
                return None
            return self.env.get(node.name)
        if isinstance(node, Identifier):
            return self.env.get(node.name)

        if isinstance(node, BinaryOp):
            if node.op == "and":
                left = self._eval(node.left)
                if not _is_truthy(left):
                    return left
                return self._eval(node.right)
            if node.op == "or":
                left = self._eval(node.left)
                if _is_truthy(left):
                    return left
                return self._eval(node.right)
            left = self._eval(node.left)
            right = self._eval(node.right)
            return self._binop(node.op, left, right)

        if isinstance(node, UnaryOp):
            operand = self._eval(node.operand)
            if node.op == "not":
                return not _is_truthy(operand)
            if node.op == "-":
                return -operand
            if node.op == "+":
                return +operand
            raise FrayTypeError(f"unknown unary operator '{node.op}'")

        if isinstance(node, AwaitExpr):
            return self._eval_await(node.value)

        if isinstance(node, QuestionMark):
            val = self._eval(node.value)
            if isinstance(val, FraySome):
                return val.val
            if isinstance(val, FrayOk):
                return val.val
            if isinstance(val, FrayErr):
                raise FrayRuntimeError(f"called unwrap() on Err: {_repr(val.err)}")
            if val is None:
                raise FrayRuntimeError("called unwrap() on None")
            return val

        # Enum variant construction: Enum.Variant(args) is dispatched here
        # rather than through the member access below, because a parameterized
        # variant has no value for `Enum.Variant` to evaluate — the bare
        # member access is an error (see the MemberAccess branch). Dispatching
        # on the enum definition statically keeps the working call form while
        # the bare form raises, and checking the scope first preserves
        # Python-style shadowing: a value bound over the enum name is used as
        # the callee exactly as before.
        if (isinstance(node, Call) and isinstance(node.func, MemberAccess)
                and isinstance(node.func.obj, Identifier)
                and self.env._has(node.func.obj.name)):
            enum_val = self.env.get(node.func.obj.name)
            if isinstance(enum_val, FrayEnumDef):
                vname, vparams = enum_val.get_case(node.func.attr)
                args = [self._eval(a) for a in node.args]
                if len(args) != len(vparams):
                    raise FrayTypeError(
                        f"{enum_val.name}.{vname}() takes {len(vparams)} "
                        f"argument(s) ({len(args)} given)")
                return FrayEnum(enum_val, vname, args)

        if isinstance(node, Call):
            func = self._eval(node.func)
            args = [self._eval(a) for a in node.args]
            if isinstance(func, FrayStructDef):
                # Struct construction: positional args fill fields in order
                instance = FrayStruct(func)
                for i, arg in enumerate(args):
                    if i < len(func.fields):
                        instance.set(func.fields[i][0], arg)
                return instance
            if isinstance(func, FrayFunction):
                return self._call_function(func, args)
            if callable(func):
                return func(args)
            raise FrayTypeError(f"'{_repr(func)}' is not callable")

        if isinstance(node, MemberAccess):
            obj = self._eval(node.obj)
            if isinstance(obj, FrayModule):
                if node.attr not in obj.attrs:
                    raise FrayImportError(f"'{obj.name}' has no exported '{node.attr}'")
                return obj.attrs[node.attr]
            if isinstance(obj, FrayEnumDef):
                # Enum.Variant — the parameterless form is the variant's value.
                # A parameterized variant has no value form: it is only ever
                # *called* (Color.Green(255, 0, 128)), and the Call branch
                # dispatches that shape before evaluating this member access —
                # so reaching here with parameters means the bare form was
                # written. This engine used to answer it with a constructor
                # closure: it printed as a value, passed for one, and only the
                # first field read died ("object has no attribute ...").
                # Raise like the compiled engines' compile-time rejections.
                vname, vparams = obj.get_case(node.attr)
                if not vparams:
                    # Simple variant — return a singleton
                    return FrayEnum(obj, vname, [])
                raise FrayTypeError(
                    f"bare enum-variant references of a parameterized variant "
                    f"like '{obj.name}.{node.attr}' are not allowed — call it "
                    f"as '{obj.name}.{node.attr}(args)'")
            if isinstance(obj, FrayEnum):
                # Enum field access: Color.Red._variant → "Red", c.radius → 5
                if node.attr == "_variant":
                    return obj.variant
                # The compiled engines keep the enum's own name in a field of
                # the instance (the _variant tag's sibling); they compare it
                # against a qualified match arm's qualifier, and reading it
                # back has to mean the same thing here.
                if node.attr == "_enum":
                    return obj._enum_def.name
                # Try to get from the enum def's case params
                for vname, vparams in obj._enum_def.cases:
                    if vname == obj._variant and node.attr in vparams:
                        idx = vparams.index(node.attr)
                        return obj._values[idx]
                raise FrayTypeError(f"'{obj._enum_def.name}.{obj._variant}' has no field '{node.attr}'")
            if isinstance(obj, FrayStruct):
                return obj.get(node.attr)
            if isinstance(obj, FrayList):
                if node.attr == "append":
                    return lambda args: obj.append(args[0])
                if node.attr == "depend":
                    return lambda args: obj.depend()
            if isinstance(obj, str):
                if node.attr == "upper":
                    return lambda args: obj.upper()
                if node.attr == "lower":
                    return lambda args: obj.lower()
                if node.attr == "trim":
                    return lambda args: obj.strip()
                if node.attr == "find":
                    def _str_find(args):
                        sub = args[0]
                        idx = obj.find(sub)
                        return idx
                    return _str_find
                if node.attr == "replace":
                    def _str_replace(args):
                        return obj.replace(args[0], args[1])
                    return _str_replace
                if node.attr == "split":
                    def _str_split(args):
                        parts = obj.split(args[0])
                        return FrayList(parts)
                    return _str_split
                if node.attr == "startswith":
                    return lambda args: obj.startswith(args[0])
                if node.attr == "endswith":
                    return lambda args: obj.endswith(args[0])
            if isinstance(obj, FraySet):
                if node.attr == "append":
                    return lambda args: obj.append(args[0])
                if node.attr == "depend":
                    return lambda args: obj.depend()
            if isinstance(obj, FrayAtomic):
                return _atomic_method(obj, node.attr)
            raise FrayTypeError(f"object has no attribute '{node.attr}'")

        if isinstance(node, Index):
            obj = self._eval(node.obj)
            if isinstance(node.index_expr, Slice):
                sl = node.index_expr
                start = self._eval(sl.start) if sl.start is not None else None
                stop = self._eval(sl.stop) if sl.stop is not None else None
                step = self._eval(sl.step) if sl.step is not None else None
                if isinstance(obj, str):
                    return self._slice_string(obj, start, stop, step)
                if isinstance(obj, FrayList):
                    return self._slice_list(obj, start, stop, step)
                raise FrayTypeError("object does not support slicing")
            idx = self._eval(node.index_expr)
            if isinstance(obj, FrayMap):
                return obj[idx]
            if isinstance(obj, (FrayList, FrayTuple)):
                return obj[idx]
            if isinstance(obj, str):
                if idx < 0:
                    idx += len(obj)
                return obj[idx]
            raise FrayTypeError(f"object is not subscriptable")

        if isinstance(node, ListLiteral):
            elements = [self._eval(e) for e in node.elements]
            return FrayList(elements)

        if isinstance(node, SetLiteral):
            elements = [self._eval(e) for e in node.elements]
            return FraySet(elements)

        if isinstance(node, TupleLiteral):
            elements = [self._eval(e) for e in node.elements]
            return FrayTuple(elements)

        if isinstance(node, MapLiteral):
            m = FrayMap()
            for k_node, v_node in zip(node.keys, node.values):
                k = self._eval(k_node)
                v = self._eval(v_node)
                m[k] = v
            return m

        if isinstance(node, MatchExpression):
            subject = self._eval(node.subject)
            for arm in node.cases:
                if arm.variant_name == "_":
                    # Wildcard — always matches
                    for i, pname in enumerate(arm.params):
                        self.env.define(pname, subject)
                    result = None
                    for si, stmt in enumerate(arm.body):
                        # A trailing expression statement is the arm's value:
                        # evaluate it once instead of discarding the result.
                        if si == len(arm.body) - 1 and isinstance(stmt, ExprStatement):
                            result = self._eval(stmt.expr)
                        else:
                            self._exec(stmt)
                    return result
                if isinstance(subject, FrayEnum):
                    if subject._variant == arm.variant_name:
                        if arm.enum_name and arm.enum_name != subject._enum_def.name:
                            continue
                        # Bind captured params
                        for i, pname in enumerate(arm.params):
                            if i < len(subject._values):
                                self.env.define(pname, subject._values[i])
                        result = None
                        for si, stmt in enumerate(arm.body):
                            # A trailing expression statement is the arm's value:
                            # evaluate it once instead of discarding the result.
                            if si == len(arm.body) - 1 and isinstance(stmt, ExprStatement):
                                result = self._eval(stmt.expr)
                            else:
                                self._exec(stmt)
                        return result
            raise FrayRuntimeError(f"no match for {_repr(subject)}")

        raise FrayTypeError(f"unknown node type {type(node).__name__}")

    def _binop(self, op: str, left: Any, right: Any) -> Any:
        try:
            # Arithmetic
            if op == "+":
                if isinstance(left, str) and isinstance(right, str):
                    return left + right
                if isinstance(left, FrayList) and isinstance(right, FrayList):
                    return FrayList(left.elements + right.elements)
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                if isinstance(left, str) and isinstance(right, int):
                    return left * right
                if isinstance(left, int) and isinstance(right, str):
                    return right * left
                return left * right
            if op == "/":
                if right == 0:
                    raise FrayZeroDivisionError("division by zero")
                return left / right
            if op == "//":
                if right == 0:
                    raise FrayZeroDivisionError("division by zero")
                return left // right
            if op == "%":
                if right == 0:
                    raise FrayZeroDivisionError("division by zero")
                return left % right
            if op == "^":
                return left ** right

            # Comparison
            if op == "==":
                return left == right
            if op == "!=":
                return left != right
            if op == "<":
                return left < right
            if op == ">":
                return left > right
            if op == "<=":
                return left <= right
            if op == ">=":
                return left >= right

            # Boolean
            if op == "and":
                return _is_truthy(left) and _is_truthy(right)
            if op == "or":
                return _is_truthy(left) or _is_truthy(right)
            if op == "xor":
                return _is_truthy(left) ^ _is_truthy(right)
            if op == "xnor":
                return not (_is_truthy(left) ^ _is_truthy(right))

            # Membership test: key in container
            if op == "in":
                if isinstance(right, FrayMap):
                    return left in right
                if isinstance(right, (FrayList, FrayTuple)):
                    return left in right.elements
                if isinstance(right, FraySet):
                    return any(_values_equal(left, e) for e in right.elements)
                if isinstance(right, str) and isinstance(left, str):
                    return left in right
                raise FrayTypeError(f"'in' not supported for this type")

            raise FrayTypeError(f"unknown operator '{op}'")
        except TypeError as e:
            # Convert native Python TypeErrors to Fray exceptions
            raise FrayTypeError(str(e)) from None
        except ZeroDivisionError as e:
            raise FrayZeroDivisionError(str(e)) from None


# ── Public API ──

def run(source: str, filename: str = "<string>", stdin: Optional[str] = None) -> str:
    """Run fray source code and return stdout as a string.
    
    Args:
        source: fray source code
        filename: for error messages
        stdin: simulated stdin input (not yet used)
    
    Returns:
        stdout output as a string. If the program calls exit(code) the output
        is flushed to real stdout and SystemExit(code) propagates to the host.
    """
    import io
    exit_code = None
    old_stdout = sys.stdout
    sys.stdout = captured = io.StringIO()
    try:
        tokens = tokenize(source, filename)
        ast = parse(tokens, filename)
        analyze(ast, filename)
        evaluator = Evaluator(filename=filename)
        evaluator.run(ast)
    except FrayProgramExit as e:
        exit_code = e.code
    finally:
        sys.stdout = old_stdout
    output = captured.getvalue()
    if exit_code is not None:
        # exit(code): the native runtime's C exit() flushes stdio, so the
        # output printed before the call survives — mirror that here, then
        # hand the status to the host process.
        sys.stdout.write(output)
        raise SystemExit(exit_code)
    return output
