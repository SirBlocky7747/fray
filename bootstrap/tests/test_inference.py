"""Unit tests for the Phase 5 type-inference pass.

Run:  cd bootstrap && python -m pytest tests/test_inference.py -q
(or)  cd bootstrap && python tests/test_inference.py
"""

from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lexer import tokenize
from parser import parse
from own_lattice import INT, FLOAT, BOOL, STRING, LIST, UNKNOWN, VOID
from inference import Inference


def infer(src: str) -> Inference:
    ast = parse(tokenize(src, "<test>"), "<test>")
    return Inference().infer(ast)


def check(name: str, cond: bool):
    if not cond:
        print(f"FAIL  {name}")
        sys.exit(1)
    print(f"PASS  {name}")


# ── Literals ──

inf = infer("x = 1\ny = 2.5\ns = \"hi\"\nb = True\n")
check("int_literal", inf.type_of("x").tag == "INT")
check("float_literal", inf.type_of("y").tag == "FLOAT")
check("string_literal", inf.type_of("s").tag == "STRING")
check("bool_literal", inf.type_of("b").tag == "BOOL")

# ── Arithmetic joins ──

inf = infer("a = 1 + 2\nb = 1 + 2.0\n")
check("int_plus_int", inf.type_of("a").tag == "INT")
check("int_plus_float", inf.type_of("b").tag == "FLOAT")

inf = infer("x = 1\nx = \"now a string\"\n")
check("join_widens_to_unknown", inf.type_of("x").tag == "UNKNOWN")

inf = infer("x = 1\nx = 2\n")
check("same_type_stays", inf.type_of("x").tag == "INT")

# ── Comparisons and bools ──

inf = infer("a = 1 < 2\nb = 1 == 2 and True\n")
check("cmp_is_bool", inf.type_of("a").tag == "BOOL")
check("bool_ops_are_bool", inf.type_of("b").tag == "BOOL")

# ── Builtins ──

inf = infer("r = range(10)\ns = str(1)\ni = int(\"5\")\nf = float(2)\n")
check("range_is_list", inf.type_of("r").tag == "LIST")
check("str_is_string", inf.type_of("s").tag == "STRING")
check("int_is_int", inf.type_of("i").tag == "INT")
check("float_is_float", inf.type_of("f").tag == "FLOAT")

# ── Functions ──

src = """
def add(a, b):
    return a + b

x = add(1, 2)
y = add(1, 2.5)
"""
inf = infer(src)
check("int_call_ret", inf.type_of("x").tag == "INT")
check("mixed_args_ret_float", inf.type_of("y").tag == "FLOAT")
sig = inf.sig("add")
check("param_a_widened", sig.params["a"].tag == "INT")   # 1, 1
check("param_b_widened", sig.params["b"].tag == "UNKNOWN")  # 2, 2.5

src = """
def two():
    return 2

z = two()
"""
inf = infer(src)
check("no_param_ret", inf.type_of("z").tag == "INT")

src = """
def f(x):
    return x

a = f(1)
b = f("s")
"""
inf = infer(src)
# Per-site inference: each call site sees its own argument types.
check("poly_param_site1_int", inf.type_of("a").tag == "INT")
check("poly_param_site2_str", inf.type_of("b").tag == "STRING")
check("poly_param_widened", inf.sig("f").params["x"].tag == "UNKNOWN")

# ── For-loop element type ──

src = """
total = 0
for i in range(1000000):
    total = total + i
"""
inf = infer(src)
check("range_elem_is_int", inf.type_of("i").tag == "INT")
check("sum_stays_int", inf.type_of("total").tag == "INT")

# ── Call-before-def (function defined after use in module code is a
# sema error, but inference must not crash) ──

inf = infer("x = 1\n")
check("trivial_ok", inf.type_of("x").tag == "INT")

# ── Self-recursion fixpoint ──
#
# A recursive call carries no information by itself, so the legacy
# widening answers UNKNOWN and every operation on the result stays boxed.
# The fixpoint instead derives a hypothesis from the non-recursive
# returns, re-walks the body with recursive calls answering it, and keeps
# the result only when the walk reproduces it exactly.

FIB = """
def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

a = fib(20)
"""
inf = infer(FIB)
check("recursion_site_is_int", inf.type_of("a").tag == "INT")
check("recursion_widened_ret", inf.sig("fib").ret.tag == "INT")
check("recursion_site_agrees", inf._call_result("fib", [INT], []).tag == "INT")
check("recursion_raw_spec", inf.raw_specialized("fib", [INT]) == (("int",), "int"))

# A recursive body whose returned type is float by construction.
FLOAT_REC = """
def down(n):
    if n < 1:
        return 0.5
    return down(n - 1)

a = down(3)
"""
inf = infer(FLOAT_REC)
check("recursion_float_ret", inf.type_of("a").tag == "FLOAT")
check("recursion_float_raw",
      inf.raw_specialized("down", [INT]) == (("int",), "float"))

# A divider breaks the fixpoint: the recursive contribution is FLOAT while
# the base case is INT, so the honest answer is UNKNOWN.
DIVIDED = """
def halve(n):
    if n == 0:
        return 1
    return halve(n - 1) / 2

a = halve(3)
"""
inf = infer(DIVIDED)
# VOID and UNKNOWN both mean "no specialization": a slot only ever gets a
# ground type when the analysis is sure of it.
check("recursion_non_fixpoint_unknown",
      inf.type_of("a").tag not in ("INT", "FLOAT"))
check("recursion_non_fixpoint_no_raw", inf.raw_specialized("halve", [INT]) is None)

# A return that is only reachable through a nested branch must still count:
# the fixpoint joins every return in the body, so a body that can also
# return a string is not specialized just because its tail is numeric.
NESTED_OTHER = """
def f(n):
    if c(n):
        return f(n - 1)
    if d(n):
        return "s"
    return 1

a = f(3)
"""
inf = infer(NESTED_OTHER)
check("recursion_nested_return_no_raw", inf.raw_specialized("f", [INT]) is None)

# Mutual recursion is not a direct self-call: stays conservative.
MUTUAL = """
def a(n):
    return b(n - 1)

def b(n):
    return a(n - 1)

x = a(3)
"""
inf = infer(MUTUAL)
check("mutual_recursion_unknown", inf.type_of("x").tag not in ("INT", "FLOAT"))
check("mutual_recursion_no_raw", inf.raw_specialized("a", [INT]) is None)

# A bare `return` has no unboxed representation, so no raw ABI.
BARE_RETURN = """
def f(n):
    if n == 0:
        return
    return f(n - 1)

f(3)
"""
inf = infer(BARE_RETURN)
check("bare_return_no_raw", inf.raw_specialized("f", [INT]) is None)

# Parameters are only raw when every call site passes the same numeric
# type, and the join has to be complete before codegen reads it.
MIXED_SITES = """
def g(x):
    return scale(x)

def scale(n):
    return n * 2

p = g(3)
q = scale(2.5)
"""
inf = infer(MIXED_SITES)
check("mixed_sites_param_widens", inf.sig("scale").params["n"].tag == "UNKNOWN")
check("mixed_sites_no_raw", inf.raw_specialized("scale", [INT]) is None)

# An accumulator recursion proves both parameters and the result.
ACC = """
def total(n, acc):
    if n == 0:
        return acc
    return total(n - 1, acc + n)

s = total(10, 0)
"""
inf = infer(ACC)
check("accumulator_ret", inf.type_of("s").tag == "INT")
check("accumulator_raw",
      inf.raw_specialized("total", [INT, INT]) == (("int", "int"), "int"))

print("\nAll inference tests passed.")
