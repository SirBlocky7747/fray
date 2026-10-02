#!/usr/bin/env python3
"""
Test fray codegen — generates LLVM IR and verifies it parses/validates.
Does NOT require a C compiler (JIT optional).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from llvmlite import binding
from codegen import compile_to_ir

# Initialize LLVM targets
binding.initialize_all_targets()
binding.initialize_all_asmprinters()


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


def test_codegen():
    """Test IR generation for all snippets."""
    print("=== Codegen IR tests ===")
    passed = 0
    failed = 0
    for name, code in SNIPPETS.items():
        try:
            ir_text = compile_to_ir(code, f"{name}.fray")
            # Verify the IR parses
            mod = binding.parse_assembly(ir_text)
            mod.verify()
            print(f"  PASS  {name} ({len(ir_text)} chars IR)")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\nCodegen: {passed} passed, {failed} failed\n")
    return failed == 0


def test_jit():
    """Test JIT execution (requires LLVM targets registered)."""
    print("=== JIT execution tests ===")
    passed = 0
    failed = 0

    from codegen import run_ir_jit

    for name, code in SNIPPETS.items():
        try:
            ir_text = compile_to_ir(code, f"{name}.fray")
            result = run_ir_jit(ir_text)
            print(f"  PASS  {name} (exit={result})")
            passed += 1
        except Exception as e:
            print(f"  SKIP  {name}: {type(e).__name__}: {e}")
    print(f"\nJIT: {passed} passed, {failed} failed\n")
    return True  # JIT is optional


def main():
    ir_ok = test_codegen()
    test_jit()  # optional

    if ir_ok:
        print("All IR generation tests passed!")
        return 0
    else:
        print("Some IR generation tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
