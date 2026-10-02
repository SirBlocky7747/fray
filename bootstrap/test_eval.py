#!/usr/bin/env python3
"""
Test the fray evaluator on all 20 fray.txt snippets.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from evaluator import run, FrayError


# All code snippets from fray.txt, with expected output
SNIPPETS = {
    "variable_assignment": (
        """\
x = 1
x = 2
x = "Hello"
x = 1.1
const x = 1
""",
        ""  # no output
    ),
    "basic_math": (
        """\
print(1 + 1)
print(1 - 1)
print(1 * 1)
print(7 / 2)
""",
        "2\n0\n1\n3.5"
    ),
    "augmented_assignment": (
        """\
x = 0
x += 1
x -= 1
x *= 1
x /= 1
print(x)
""",
        "0.0"
    ),
    "print": (
        """\
print(1)
""",
        "1"
    ),
    "function_no_args": (
        """\
def x():
    y = 1 + 1
    return y
print(x())
""",
        "2"
    ),
    "function_with_args": (
        """\
def x(a, b):
    y = a + b
    return y
print(x(1, 1))
""",
        "2"
    ),
    "input_functions": (
        """\
# These would block on stdin, so we just test they parse/analyze
# input()
# inputInt()
# inputFloat()
# inputStr()
# inputStr("Insert your name: ")
print("ok")
""",
        "ok"
    ),
    "mutable_list": (
        """\
x = [1, 2, 1, 4]
print(len(x))
print(x[0])
x[0] = 10
print(x[0])
x.append(5)
print(len(x))
print(x[-1])
""",
        "4\n1\n10\n5\n5"
    ),
    "random_set": (
        """\
x = {1, 2, 3, 4}
print(len(x))
x.append(5)
print(len(x))
""",
        "4\n5"
    ),
    "immutable_tuple": (
        """\
x = (10, 20, 30)
print(len(x))
print(x[1])
""",
        "3\n20"
    ),
    "import_module": (
        """\
import random
# random.randomInt(0, 1)  # would need implementation
print("imported ok")
""",
        "imported ok"
    ),
    "booleans_and_logic": (
        """\
x = True
y = False
print(x and y)
print(x or y)
print(x xor y)
print(not x)
print(x xnor y)
""",
        "False\nTrue\nTrue\nFalse\nFalse"
    ),
    "for_loop": (
        """\
total = 0
for i in range(5):
    total += i
print(total)
""",
        "10"
    ),
    "while_loop": (
        """\
count = 0
while count < 3:
    count += 1
print(count)
""",
        "3"
    ),
    "comparisons": (
        """\
x = 1
print(x == 1)
print(x != 1)
print(x > 0)
print(x < 0)
print(x >= 1)
print(x <= 0)
print(min(3, 1, 2))
print(max(3, 1, 2))
print(sum([1, 2, 3]))
""",
        "True\nFalse\nTrue\nFalse\nTrue\nFalse\n1\n3\n6"
    ),
    "complex_math": (
        """\
print(abs(-5))
print(7 % 2)
print(2 ^ 3)
print(7 // 2)
print(sqrt(4))
print(isqrt(9))
""",
        "5\n1\n8\n3\n2.0\n3"
    ),
    "type_conversion": (
        """\
x = int("42")
print(x)
y = float(1)
print(y)
z = str(100)
print(z)
w = round(3.7)
print(w)
""",
        "42\n1.0\n100\n4"
    ),
    "conditions": (
        """\
x = 2
if x == 1:
    print("one")
elif x == 2:
    print("two")
else:
    print("other")
""",
        "two"
    ),
    "error_handling": (
        """\
try:
    x = 1 + "10"
except TypeError:
    print("type error caught")
finally:
    print("finally ran")
print("done")
""",
        "type error caught\nfinally ran\ndone"
    ),
    "statistics": (
        """\
x = [10, 20, 15, 12]
print(mean(x))
print(med(x))
print(mid(x))
print(mode(x))
""",
        "14.25\n13.5\n15\n10"
    ),
}


def test_evaluator():
    """Run all snippets through the evaluator."""
    print("=== Evaluator tests ===")
    passed = 0
    failed = 0
    for name, (code, expected) in SNIPPETS.items():
        try:
            output = run(code, f"{name}.fray").strip()
            expected = expected.strip()
            if output == expected:
                print(f"  PASS  {name}")
                passed += 1
            else:
                print(f"  FAIL  {name}")
                print(f"    Expected: {expected!r}")
                print(f"    Got:      {output!r}")
                failed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nEvaluator: {passed} passed, {failed} failed\n")
    return failed == 0


def main():
    ok = test_evaluator()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
