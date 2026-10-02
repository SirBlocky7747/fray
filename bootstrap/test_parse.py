#!/usr/bin/env python3
"""
Test the fray lexer + parser + printer on fray.txt snippets.

Verifies that every snippet parses cleanly and round-trips through AST → printer.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from lexer import tokenize, LexerError
from parser import parse, ParseError
from sema import analyze, SemanticError
from printer import print_program


# All code snippets from fray.txt, extracted individually
SNIPPETS = {
    "variable_assignment": """\
x = 1
x = 2
x = "Hello"
x = 1.1
const x = 1
""",
    "basic_math": """\
1 + 1
1 - 1
1 * 1
1 / 1
""",
    "augmented_assignment": """\
x = 0
x += 1
x -= 1
x *= 1
x /= 1
""",
    "print": """\
print(1)
""",
    "function_no_args": """\
def x():
    y = 1 + 1
    return y
print(x())
""",
    "function_with_args": """\
def x(a, b):
    y = a + b
    return y
print(x(1, 1))
""",
    "input_functions": """\
input()
inputInt()
inputFloat()
inputStr()
inputStr("Insert your name: ")
""",
    "mutable_list": """\
x = [1, 2, 1, 4]
x[0]
x[0] = 2
len(x)
x.append(1)
x.depend
x[0].append(1)
x[0].depend
print(x)
""",
    "random_set": """\
x = {1, 2, 3, 4}
x.append(1)
x.depend
len(x)
print(x)
""",
    "immutable_tuple": """\
x = (1, 2, 1, 4)
len(x)
print(x)
""",
    "import_module": """\
import random
random.randomInt(0, 1)
""",
    "relative_imports": """\
from .util import twice
from ..pkg.util import three
import .sibling
import ..uncle
from . import more
""",
    "booleans_and_logic": """\
x = True
y = False
a = x and y
a = x or y
a = x xor y
a = not x
a = x xnor y
""",
    "for_loop": """\
for i in range(2):
    x += 1
""",
    "while_loop": """\
while True:
    x += 1
""",
    "comparisons": """\
x = 1
x == 1
x != 1
x > 1
x < 1
x >= 1
x <= 1
min(1, 2, 3)
max(1, 2, 3)
sum([1, 2, 3])
""",
    "complex_math": """\
abs(-1)
x % y
x ^ 2
x // y
sqrt(4)
isqrt(4)
""",
    "type_conversion": """\
x = int("1")
x = float(1)
x = float("1")
x = float("1.0")
x = str(1)
x = str(1.0)
x = round(1.1)
""",
    "conditions": """\
if x == 1:
    x += 1
elif x == 2:
    x -= 1
else:
    x = 0
""",
    "error_handling": """\
try:
    x = 1 + "10"
except TypeError:
    print("Cannot add a string to a number!")
finally:
    print(x)
""",
    "statistics": """\
x = [10, 20, 15, 12]
mean(x)
med(x)
mode(x)
mid(x)
""",
}


def test_lexer():
    """Test that all snippets tokenize without errors."""
    print("=== Lexer tests ===")
    passed = 0
    failed = 0
    for name, code in SNIPPETS.items():
        try:
            tokens = tokenize(code, f"{name}.fray")
            print(f"  PASS  {name} ({len(tokens)} tokens)")
            passed += 1
        except LexerError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\nLexer: {passed} passed, {failed} failed\n")
    return failed == 0


def test_parser():
    """Test that all snippets parse without errors."""
    print("=== Parser tests ===")
    passed = 0
    failed = 0
    for name, code in SNIPPETS.items():
        try:
            tokens = tokenize(code, f"{name}.fray")
            ast = parse(tokens, f"{name}.fray")
            print(f"  PASS  {name} ({len(ast.body)} statements)")
            passed += 1
        except (LexerError, ParseError) as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\nParser: {passed} passed, {failed} failed\n")
    return failed == 0


def test_roundtrip():
    """Test that ASTs round-trip through the printer."""
    print("=== Round-trip tests ===")
    passed = 0
    failed = 0
    for name, code in SNIPPETS.items():
        try:
            tokens = tokenize(code, f"{name}.fray")
            ast = parse(tokens, f"{name}.fray")
            printed = print_program(ast)

            # Re-parse the printed output to verify it's valid
            tokens2 = tokenize(printed, f"{name}_roundtrip.fray")
            ast2 = parse(tokens2, f"{name}_roundtrip.fray")
            printed2 = print_program(ast2)

            if printed == printed2:
                print(f"  PASS  {name}")
                passed += 1
            else:
                print(f"  FAIL  {name}: round-trip not stable")
                print(f"    First:  {printed[:80]!r}...")
                print(f"    Second: {printed2[:80]!r}...")
                failed += 1
        except (LexerError, ParseError) as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\nRound-trip: {passed} passed, {failed} failed\n")
    return failed == 0


def test_sema():
    """Test semantic analysis on snippets."""
    print("=== Semantic analysis tests ===")
    passed = 0
    failed = 0
    for name, code in SNIPPETS.items():
        try:
            tokens = tokenize(code, f"{name}.fray")
            ast = parse(tokens, f"{name}.fray")
            analyze(ast, f"{name}.fray")
            print(f"  PASS  {name}")
            passed += 1
        except (LexerError, ParseError, SemanticError) as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\nSemantic analysis: {passed} passed, {failed} failed\n")
    return failed == 0


def main():
    results = []
    results.append(("Lexer", test_lexer()))
    results.append(("Parser", test_parser()))
    results.append(("Round-trip", test_roundtrip()))
    results.append(("Sema", test_sema()))

    print("=== Summary ===")
    all_pass = True
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {name}")
        if not ok:
            all_pass = False

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
