#!/usr/bin/env python3
"""
Test fray codegen — generates LLVM IR and verifies LLVM itself accepts it.

The IR is handed to `llc`, the same emitter the release chain uses, so "does
this parse" has exactly one answer rather than one per LLVM binding in the
tree. Requires `llc` on PATH; does not require a C compiler.
"""

import sys
import os
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from codegen import compile_to_ir, find_llc


SNIPPETS = {
    "print_int": "print(1)",
    "print_string": 'print("Hello")',
    "arithmetic": "print(1 + 2)",
    "print_float": "print(3.14)",
    "comparison": "print(1 == 2)",
    "boolean": "print(True)",
    "negation": "print(not True)",
    "if_else": """\
x = 2
if x == 1:
    print("one")
else:
    print("other")
""",
    "while_loop": """\
x = 0
while x < 3:
    x += 1
print(x)
""",
    "list_literal": "print([1, 2, 3])",
    "string_concat": 'print("a" + "b")',
    "power": "print(2 ^ 3)",
    "floor_div": "print(7 // 2)",
    "augmented": """\
x = 10
x += 5
print(x)
""",
    "for_range": """\
for i in range(3):
    print(i)
""",
    "nested_if": """\
x = 2
if x == 1:
    print("a")
elif x == 2:
    print("b")
else:
    print("c")
""",
}


def verify_with_llc(ir_text):
    """Return None when LLVM accepts the IR, else the first error line."""
    llc = find_llc()
    if not llc:
        return "no `llc` found: install LLVM to run these tests"
    with tempfile.TemporaryDirectory(prefix="fray_test_") as td:
        ir_path = os.path.join(td, "module.ll")
        obj_path = os.path.join(td, "module.o")
        with open(ir_path, "w") as f:
            f.write(ir_text)
        result = subprocess.run([llc, "-filetype=obj", ir_path, "-o", obj_path],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return None
        for line in result.stderr.splitlines():
            if "error:" in line:
                return line.strip()
        return result.stderr.strip()[-200:] or "llc failed"


def test_codegen():
    """Test IR generation for all snippets."""
    print("=== Codegen IR tests ===")
    passed = 0
    failed = 0
    for name, code in SNIPPETS.items():
        try:
            ir_text = compile_to_ir(code, f"{name}.fray")
            problem = verify_with_llc(ir_text)
            if problem:
                print(f"  FAIL  {name}: {problem}")
                failed += 1
            else:
                print(f"  PASS  {name} ({len(ir_text)} chars IR)")
                passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nCodegen: {passed} passed, {failed} failed\n")
    return failed == 0


def main():
    ir_ok = test_codegen()

    if ir_ok:
        print("All IR generation tests passed!")
        return 0
    else:
        print("Some IR generation tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
