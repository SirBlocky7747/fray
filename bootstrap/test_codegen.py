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


def _ref_counts(code: str, name: str) -> tuple[int, int]:
    """(releases, retains) actually emitted in the function bodies."""
    ir_text = compile_to_ir(code, f"{name}.fray")
    rel = ir_text.count('call void @"fray_release"')
    ret = ir_text.count('call void @"fray_retain"')
    return rel, ret


# Rebinding a local from a call that returns a heap box must release the
# previous binding. In a function-local (alloca) slot the store used to skip
# that release, so every rebinding in a loop orphaned its predecessor and only
# the last value was dropped by the return path — `msg = readAsync(conn, 64)`
# in a coroutine leaked one string per round-trip. An async def is where this
# showed up as a leak because only the coroutine's loop rebinds across the
# park; a plain def happened to reach the other slot path.
_REBIND_CASES = {
    "rebind_plain_literal": """def f(n):
    m = [0]
    i = 0
    while i < 3:
        m = [n, n]
        i += 1
    return m
""",
    "rebind_plain_boxedcall": """def make(n):
    return [n, n]

def f(n):
    m = [0]
    i = 0
    while i < 3:
        m = make(n)
        i += 1
    return m
""",
    "rebind_async_boxedcall": """def make(n):
    return [n, n]

async def f(n):
    m = [0]
    i = 0
    while i < 3:
        m = make(n)
        i += 1
    return m
""",
}


def test_local_rebind_releases():
    """Every rebinding shape must give back the reference the slot held."""
    print("=== Local rebind releases its previous value ===")
    nets = {}
    failed = 0
    for name, code in _REBIND_CASES.items():
        try:
            rel, ret = _ref_counts(code, name)
            nets[name] = rel - ret
            print(f"  {name}: {rel} release, {ret} retain (net {rel - ret})")
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        return False
    # The async shape is the one that leaked; it must now match the plain one.
    if nets["rebind_async_boxedcall"] != nets["rebind_plain_boxedcall"]:
        print("  FAIL  async rebind releases fewer references than the plain "
              f"def: {nets['rebind_async_boxedcall']} != "
              f"{nets['rebind_plain_boxedcall']}")
        return False
    print("  PASS  async and plain rebinds balance identically")
    return True


def main():
    ir_ok = test_codegen()
    rebind_ok = test_local_rebind_releases()

    if ir_ok and rebind_ok:
        print("All IR generation tests passed!")
        return 0
    else:
        print("Some IR generation tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
