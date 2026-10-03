#!/usr/bin/env python3
"""fray self-hosted frontend gate.

The self-hosted compiler (compiler/*.fray) is a separate frontend from the
Python bootstrap and deliberately supports a subset of the language. Three
invariants matter more than the size of that subset:

  * it always terminates — unsupported syntax is reported, never spun on
    (the original defect: `struct` fields with defaults hung the parser)
  * it never reports an internal error — a rejection is a diagnostic that
    names the stage, file, line and column; a crash is a compiler bug
  * where it accepts a program, the result is correct — accepted golden
    cases must produce their .expected output

The gate enforces those invariants over every golden case and an inline
matrix of syntax the frontend does not (yet) support. Cases that compile are
tracked in tests/selfhosted_supported.txt so current coverage cannot quietly
regress: a listed case that starts reporting a diagnostic fails the gate.

Usage:
    python tools/check_frontend.py              # golden cases (IR verify)
    python tools/check_frontend.py --run        # compile, link and run them
    python tools/check_frontend.py --probes     # unsupported-syntax matrix
    python tools/check_frontend.py --stage1     # compile the compiler's own modules
    python tools/check_frontend.py --single-unit  # all compiler modules in ONE binary
    python tools/check_frontend.py --packages    # dotted modules resolve to packages
    python tools/check_frontend.py --update     # rewrite the supported list
"""

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
BOOTSTRAP_DIR = REPO_ROOT / "bootstrap"
CASES_DIR = REPO_ROOT / "tests" / "cases"
DRIVER = TOOLS_DIR / "frayc_selfhosted.py"
ORACLE = TOOLS_DIR / "fray_oracle.py"
SUPPORT_LIST = REPO_ROOT / "tests" / "selfhosted_supported.txt"

sys.path.insert(0, str(TOOLS_DIR))
# Reuse the golden runner's interpreter discovery: the driver needs llvmlite
# to emit objects, and the gate must invoke it with an interpreter that has it.
from run_tests import _find_python_with_llvmlite  # noqa: E402
import target  # platform detection (bootstrap/target.py)  # noqa: E402
# The object emitter the release chain uses. The gate holds the emitted IR to
# *its* parser, not just to llvmlite's: llvmlite bundles an LLVM whose pointer
# types were folded into `ptr` and which skips numbers leniently, so modules
# that `llc` rejects used to pass every check here and fail only in the native
# chain (`instruction expected to be numbered '%0'`, array element type
# mismatches).
from frayc_selfhosted import find_llc  # noqa: E402

PYTHON = _find_python_with_llvmlite()

DIAG_RE = re.compile(r"^(LEX|PARSE|SEMA|CODEGEN) ERROR: ")
INTERNAL_MARKER = "Internal error in the self-hosted compiler"

# Syntax the self-hosted frontend does not support. Each entry must be
# handled honestly: a diagnostic naming the stage and position, or — if
# support is added later — a compiled program that matches the oracle.
# "Starting with structs": these are shapes of the struct feature the
# bootstrap accepts and the self-hosted frontend does not (yet).
PROBES = {
    "struct_field_default_no_args": """
struct Token:
    kind = 0

t = Token()
print(t.kind)
""",
    "struct_method": """
struct Counter:
    n = 0

    def bump(self):
        self.n = self.n + 1
""",
    "struct_generic": """
struct Box[T]:
    value = 0

b = Box(1)
print(b.value)
""",
    "struct_missing_field": """
struct Point:
    x = 0
    y = 0

p = Point(1)
print(p.x)
""",
    "struct_missing_field_no_default": """
struct Bare:
    x
    y

b = Bare(1)
print(b.x)
print(b.y)
""",
    "struct_extra_args": """
struct Point:
    x = 0
    y = 0

p = Point(1, 2, 3)
print(p.x)
print(p.y)
""",
    # Shapes the compiler's own sources are made of. Each was a real codegen
    # defect: locals assigned only at a function body's top level were treated
    # as module globals, structs constructed inside a function were reported
    # as undefined functions, and rebinding a parameter released a reference
    # the caller owned (a double free).
    "func_local_toplevel": """
def g(x):
    y = x + 1
    return y

print(g(1))
""",
    "func_local_struct": """
struct S:
    a
    b

def g(x):
    s = S()
    s.a = x
    return s.a

print(g(1))
""",
    "func_local_map_subscript": """
def g():
    kw = {}
    kw["a"] = 1
    return kw["a"]

print(g())
""",
    "func_local_list": """
def g(x):
    xs = []
    xs.append(x)
    return xs[0]

print(g(1))
""",
    "param_rebind": """
def g(x):
    x = x + 1
    return x

print(g(1))
""",
    "local_in_if_used_after": """
def g(x):
    if x > 0:
        y = x + 1
    else:
        y = 0
    return y

print(g(1))
""",
    "string_iteration": """
s = "ab"
for c in s:
    print(c)
""",
    "map_iteration": """
m = {"one": 1, "two": 2, "three": 3}
for k in m:
    print(k)
""",
    "struct_bad_field": """
struct Broken:
    1 + 1 = 2
""",
    "struct_no_block": """
struct Empty:
""",
    "struct_trailing_junk": """
struct Point:
    x = 0
        y = 0
""",
    "enum_value_syntax": """
enum Color:
    RED = 1
    GREEN = 2

print(Color.RED)
""",
    # The value form of an enum variant ('Color.Red', no call) is how the
    # oracle and the bootstrap engine write a parameterless variant —
    # match_basic and enum_basic are made of it, and the self-hosted codegen
    # compiles it now. The call shapes around it are pinned too: a
    # parameterless variant is not callable (the oracle rejects the call), an
    # arity mismatch used to lower silently to fray_struct_new, which bound
    # positionally — too few arguments left fields null, too many were
    # dropped — and a bare parameterized variant is a placeholder even in the
    # oracle and a compile-time error in the bootstrap engine.
    "enum_paramless_call": """
enum Color:
    case Red

x = Color.Red()
print(x._variant)
""",
    "enum_call_arity": """
enum Color:
    case Green(r, g, b)

x = Color.Green(255)
print(x._variant)
""",
    "enum_bare_param_variant": """
enum Shape:
    case Circle(radius)

x = Shape.Circle
print(x._variant)
""",
    "enum_unknown_variant": """
enum Color:
    case Red

x = Color.Bogus
print(x._variant)
""",
    "match_bad_arm": """
x = 2
match x:
    case 1:
        print("one")
""",
    "match_junk_arm": """
x = 2
match x:
    case 1 =:
        print("one")
""",
    "lambda_expr": """
f = lambda x: x + 1
print(f(1))
""",
    "decorator": """
@deco
def f():
    return 1
""",
    "async_def": """
async def f():
    return 1
""",
    # Coroutines with a scheduler: an async function is a coroutine the
    # runtime starts with its own argument list, `await` suspends the calling
    # fiber until the target finishes, and the awaited value crosses back.
    "async_chain": """
async def answer(x):
    yieldNow()
    return x + 1

async def main():
    r = await answer(41)
    print(r)

main()
runUntilComplete()
""",
    "channels": """
ch = channel()

async def producer():
    send(ch, 7)

async def consumer():
    print(recv(ch))

producer()
consumer()
runUntilComplete()
""",
    # `await` may only appear in an async body; both of these are compile-time
    # rejections in the bootstrap emitter, and the self-hosted one has to say
    # the same thing rather than emit a call that fails at run time.
    "await_outside_async": """
def f():
    return await g()
""",
    "async_function_as_value": """
async def f():
    return 1

g = f
print(g)
""",
    "with_stmt": """
with open("x") as f:
    print(1)
""",
    "generic_def": """
def f(x: int) -> int:
    return x
print(f(3))
""",
    "default_param": """
def f(x = 5):
    return x
print(f())
""",
    "star_args": """
def f(*args):
    return 1
""",
    "interface": """
interface Shape:
    def area(self):
        pass
""",
    "fstring": """
name = "x"
print(f"hi {name}")
""",
    "tuple_unpack": """
a, b = 1, 2
print(a, b)
""",
    "unterminated_string": """
s = "abc
print(s)
""",
    "unexpected_char": """
x = 1 $ 2
print(x)
""",
    "stray_indent": """
x = 1
    y = 2
""",
    "bare_expr_line": """
]
""",
    # A call whose argument count disagrees with the declaration. These used to
    # reach the emitter, which wrote the call from the call site and left LLVM
    # to reject the whole module with a type error naming neither the function
    # nor the count — or, on the unboxed fast path, bound fewer parameters than
    # were declared and ran on a wrong answer instead. sema now reports the
    # oracle's message. The third probe is the other half of the change: a call
    # to a function defined LATER in the file is legal, so collecting arities
    # must happen before bodies are checked rather than during.
    "call_too_few_args": """
def power(base, exponent):
    return base * exponent

print(power(2))
""",
    "call_too_many_args": """
def power(base, exponent):
    return base * exponent

print(power(2, 3, 4))
""",
    "forward_call_arity_ok": """
def caller():
    return helper(1, 2)

def helper(a, b):
    return a + b

print(caller())
""",
}


# Accepted programs whose output legitimately differs from the oracle's.
# Each entry must say why, and the gate fails if one stops diverging so the
# exemption is removed rather than forgotten. Reference: plan.md.
KNOWN_DIVERGENCES = {
    # fray_map_keys walks the hash table's slots; the oracle keeps insertion
    # order. Both native engines (this one and the bootstrap's compiled path)
    # walk the same table, so they agree with each other and not the oracle.
    "map_iteration": "map key order: native hash-table order vs oracle insertion order",
}


def _text(value) -> str:
    """Normalize subprocess output that may be bytes (on timeout)."""
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _run(cmd, timeout):
    """Run a command, returning (stdout, stderr, returncode, timed_out)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr, r.returncode, False
    except subprocess.TimeoutExpired as e:
        return _text(e.stdout), _text(e.stderr), -1, True


def compile_selfhosted(path: Path, timeout: float, mode: str = "ir"):
    """Run the self-hosted pipeline on one program."""
    return _run([PYTHON, str(DRIVER), mode, str(path), "--timeout", str(timeout)],
                timeout + 60)


def run_oracle(path: Path, timeout: float):
    return _run([PYTHON, str(ORACLE), str(path)], timeout)


def classify(rc: int, stderr: str, timed_out: bool):
    """Classify how the frontend handled a program in `ir` mode.

    Returns (kind, diagnostics) where kind is one of:
      hang            — did not terminate (compiler bug)
      internal-error  — crashed instead of diagnosing (compiler bug)
      silent-failure  — exited nonzero without naming a stage (compiler bug)
      rejected        — clean diagnostic (honest "not supported")
      accepted        — the pipeline produced IR
    """
    if timed_out:
        return "hang", []
    diags = [line for line in stderr.splitlines() if DIAG_RE.match(line)]
    if INTERNAL_MARKER in stderr:
        return "internal-error", diags
    if rc == 0:
        return "accepted", diags
    if diags:
        return "rejected", diags
    return "silent-failure", diags


def classify_run(timed_out: bool, stderr: str):
    """Classify a `run` invocation.

    Same kinds as classify(), except that the program's own exit status is not
    conflated with the compiler's: `accepted` means a binary was produced and
    ran, and its exit status is the program's business.
    """
    if timed_out:
        return "hang", []
    diags = [line for line in stderr.splitlines() if DIAG_RE.match(line)]
    if INTERNAL_MARKER in stderr:
        return "internal-error", diags
    if "  Linked " in stderr:
        return "accepted", diags
    if diags:
        return "rejected", diags
    return "silent-failure", diags


def verify_ir(ir_text: str):
    """Parse and verify IR with llvmlite. Returns None when fine, else a reason."""
    try:
        from llvmlite import binding
    except ImportError:  # pragma: no cover - llvmlite is a CI dependency
        return None
    try:
        mod = binding.parse_assembly(ir_text)
        mod.verify()
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:200]
    return None


def llc_rejects(ir_text: str):
    """Run LLVM's own emitter over the IR. None when it accepts it, else the
    first error line; None as well when no `llc` is installed (the gate then
    has only llvmlite's word for it, which is the weaker one)."""
    llc = find_llc()
    if not llc:
        return None
    with tempfile.TemporaryDirectory(prefix="fray_llc_") as td:
        ir_path = Path(td) / "module.ll"
        obj_path = Path(td) / "module.o"
        ir_path.write_text(ir_text)
        result = subprocess.run([llc, "-filetype=obj", str(ir_path),
                                 "-o", str(obj_path)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return None
        for line in result.stderr.splitlines():
            if "error:" in line:
                return line.strip()[:200]
        return result.stderr.strip().splitlines()[-1][:200] if result.stderr.strip() else "llc failed"


# The evaluator represents a missing value as an opaque sentinel, so a None
# that reaches stdout prints as "<object object at 0x...>" while both compiled
# engines print "None". Normalize that one representation so output comparison
# measures the program and not the oracle's repr; the divergence itself is
# recorded in plan.md rather than hidden here.
NONE_REPR_RE = re.compile(r"<object object at 0x[0-9a-fA-F]+>")


def normalize(s: str) -> str:
    return NONE_REPR_RE.sub("None", s.replace("\r\n", "\n")).strip()


def read_support_list() -> set:
    if not SUPPORT_LIST.exists():
        return set()
    return {
        line.strip() for line in SUPPORT_LIST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def write_support_list(names):
    header = (
        "# Golden cases the self-hosted frontend compiles and runs correctly.\n"
        "# Maintained by tools/check_frontend.py (--update). A case listed here\n"
        "# that starts reporting a diagnostic fails the gate; a case that starts\n"
        "# compiling is reported as newly supported so it can be added.\n"
    )
    SUPPORT_LIST.write_text(header + "\n".join(sorted(names)) + "\n")


def _diff(name, expected, actual):
    return "".join(difflib.unified_diff(
        normalize(expected).splitlines(keepends=True),
        normalize(actual).splitlines(keepends=True),
        fromfile=f"{name}.expected", tofile=f"{name}.actual",
    ))


def check_cases(timeout: float, use_run: bool, verbose: bool, update: bool) -> int:
    cases = sorted(
        p for p in CASES_DIR.glob("*.fray") if p.with_suffix(".expected").exists()
    )
    if not cases:
        print("No golden cases found in tests/cases/")
        return 0

    listed = read_support_list()
    supported, unsupported, failures = [], [], []
    newly_supported = []

    for case in cases:
        name = case.stem
        expected = case.with_suffix(".expected").read_text()
        mode = "run" if use_run else "ir"
        out, err, rc, timed = compile_selfhosted(case, timeout, mode)
        if use_run:
            kind, diags = classify_run(timed, err)
        else:
            kind, diags = classify(rc, err, timed)

        if kind in ("hang", "internal-error", "silent-failure"):
            failures.append(name)
            print(f"  FAIL  {name}: {kind}")
            for line in (diags or err.strip().splitlines())[:4]:
                print(f"        {line}")
            continue

        if kind == "rejected":
            if name in listed:
                failures.append(name)
                print(f"  FAIL  {name}: was supported, now rejected")
                for line in diags[:2]:
                    print(f"        {line}")
            else:
                unsupported.append(name)
                if verbose:
                    detail = diags[0][:120] if diags else ""
                    print(f"  --    {name}: not supported yet   {detail}")
            continue

        # accepted: verify the result
        problem = None
        if use_run:
            if normalize(out) != normalize(expected):
                problem = "output differs from .expected"
        else:
            problem = verify_ir(out) or llc_rejects(out)

        if problem:
            failures.append(name)
            print(f"  FAIL  {name}: {problem}")
            if use_run and verbose:
                print(_diff(name, expected, out))
            continue

        supported.append(name)
        if name not in listed:
            newly_supported.append(name)
            print(f"  ++    {name}: newly supported (add to {SUPPORT_LIST.name})")
        elif verbose:
            print(f"  PASS  {name}" + (" (compiled and ran)" if use_run else ""))

    if update and (newly_supported or set(supported) != listed):
        write_support_list(supported)
        print(f"\nUpdated {SUPPORT_LIST.relative_to(REPO_ROOT)} "
              f"({len(supported)} cases)")

    total = len(cases)
    print(f"\nGolden cases: {len(supported)}/{total} supported, "
          f"{len(unsupported)} reported cleanly as not supported, "
          f"{len(failures)} failed")
    if failures:
        print(f"Failures: {', '.join(failures)}")
        return 1
    if newly_supported and not update:
        print("Newly supported cases are not a failure — record them with --update.")
    return 0


def check_probes(timeout: float, verbose: bool) -> int:
    """Every probe must terminate and either be rejected cleanly or be correct.

    This is the regression guard for the original defect: unsupported syntax
    is a diagnostic, never a hang and never an internal error.
    """
    failures = []
    with tempfile.TemporaryDirectory(prefix="fray_frontend_probes_") as td:
        workdir = Path(td)
        for name, source in PROBES.items():
            path = workdir / f"{name}.fray"
            path.write_text(source.lstrip("\n"))
            out, err, rc, timed = compile_selfhosted(path, timeout, "run")
            kind, diags = classify_run(timed, err)

            if kind in ("hang", "internal-error", "silent-failure"):
                failures.append(name)
                print(f"  FAIL  {name}: {kind}")
                for line in (diags or err.strip().splitlines())[:4]:
                    print(f"        {line}")
                continue

            if kind == "rejected":
                stage = diags[0].split(" ERROR: ")[0]
                print(f"  ok    {name}: reported by {stage}"
                      + (f"   {diags[0][:110]}" if verbose else ""))
                continue

            # Accepted: it must agree with the oracle. Silently compiling a
            # program to different behaviour is the failure this catches.
            problem = None
            oracle_out, _, oracle_rc, oracle_timed = run_oracle(path, timeout)
            if oracle_timed:
                print(f"  ?     {name}: accepted (oracle did not terminate)")
                continue
            if oracle_rc == 0:
                if normalize(oracle_out) != normalize(out):
                    if name in KNOWN_DIVERGENCES:
                        print(f"  ~     {name}: known divergence — "
                              f"{KNOWN_DIVERGENCES[name]}")
                        continue
                    problem = "self-hosted output differs from oracle"
                elif name in KNOWN_DIVERGENCES:
                    failures.append(name)
                    print(f"  FAIL  {name}: no longer diverges — remove it from "
                          "KNOWN_DIVERGENCES")
                    continue
                else:
                    print(f"  ++    {name}: now supported — matches the oracle")
                    continue
            else:
                print(f"  ++    {name}: accepted (oracle rejects it — "
                      "language support differs)")
                continue
            failures.append(name)
            print(f"  FAIL  {name}: accepted but {problem}")
            if verbose and out.strip():
                print(f"        {out.strip()[:200]}")

    total = len(PROBES)
    print(f"\nUnsupported-syntax probes: {total - len(failures)}/{total} handled "
          "honestly (no hangs, no internal errors)")
    if failures:
        print(f"Failures: {', '.join(failures)}")
        return 1
    return 0


STAGE1_MODULES = ("lexer", "parser", "sema", "codegen")
STAGE1_DRIVERS = REPO_ROOT / "tests" / "selfhost"
# The compiler's command-line driver: the Stage-2 prerequisite binary.
NATIVE_DRIVER = REPO_ROOT / "compiler" / "frayc.fray"


def stage1_unit(module: str) -> str:
    """One compiler module as a single translation unit, plus its driver.

    Appending a driver to a module is not how the compiler is built — three
    modules define `get_errors` and two define `node_type`, so compiling them
    together needs the linker to disambiguate. compiler/main.fray imports all
    of them and check_single_unit() builds that as one binary; building a
    module on its own here is what lets the compiled module be run and compared
    against the oracle.
    """
    source = (REPO_ROOT / "compiler" / f"{module}.fray").read_text()
    lines = [line for line in source.splitlines()
             if not re.match(r"^\s*import\s+", line)]
    driver = STAGE1_DRIVERS / f"{module}_driver.fray"
    if driver.exists():
        lines.append(driver.read_text())
    return "\n".join(lines) + "\n"


def check_native_driver(timeout: float, verbose: bool, driver_bin: Path) -> int:
    """The native driver must compile golden cases with no host pipeline.

    The host only receives the IR, verifies it with LLVM, emits the object
    and links — the lexing, parsing, linking, semantic analysis and codegen
    all happened inside the driver binary.

    What counts as passing is the same rule the rest of the frontend is held
    to: a case listed in tests/selfhosted_supported.txt must compile, link,
    run and match; any other case must be *reported* — exited with a staged
    LEX/PARSE/LINK/SEMA/CODEGEN diagnostic. An exit status with no diagnostic
    is a crash inside the compiler (`KeyError: key not found` used to be the
    shape of it), not an honest rejection, and a supported case the driver
    cannot compile is exactly the divergence between the two engines this
    gate exists to catch. `# selfhost-skip` still marks a case the frontend
    deliberately reports.
    """
    cases = sorted(
        p for p in CASES_DIR.glob("*.fray") if p.with_suffix(".expected").exists()
    )
    supported = read_support_list()
    failures = []
    reported = []
    extra = []
    with tempfile.TemporaryDirectory(prefix="fray_native_gate_") as td:
        workdir = Path(td)
        for case in cases:
            name = case.stem
            source = case.read_text()
            skip_marked = "# selfhost-skip" in source
            expected = case.with_suffix(".expected").read_text()
            ir_path = workdir / f"{name}.ll"
            # The compiler's own directory (so a self-compile finds its
            # modules by name) and then the standard library: this binary lives
            # in a temporary directory, so it cannot find `stdlib/` beside
            # itself the way the shipped one does.
            out, err, rc, timed = _run(
                [str(driver_bin), str(case), str(REPO_ROOT / "compiler"),
                 str(REPO_ROOT / "stdlib")],
                timeout,
            )
            if timed:
                failures.append(name)
                print(f"  FAIL  {name}: driver did not terminate")
                continue
            # Diagnostics go to stdout (the driver has no stderr writer yet),
            # so scan both streams.
            diags = [l for l in (out + "\n" + err).splitlines()
                     if DIAG_RE.match(l)]
            if rc != 0 or "===IR_START===" not in out:
                if not diags:
                    failures.append(name)
                    print(f"  FAIL  {name}: driver neither compiled nor "
                          "diagnosed — internal error")
                    detail = [l for l in (err + "\n" + out).strip().splitlines() if l]
                    if detail:
                        print(f"        {detail[-1][:160]}")
                    continue
                if name in supported and not skip_marked:
                    failures.append(name)
                    print(f"  FAIL  {name}: supported by the pipeline, but the "
                          "driver rejected it")
                    print(f"        {diags[0][:160]}")
                    continue
                reported.append(name)
                if verbose:
                    print(f"  --    {name}: reported {diags[0][:120]}")
                continue
            if skip_marked:
                print(f"  note  {name}: '# selfhost-skip' is stale — the "
                      "driver compiled it")
            elif name not in supported:
                extra.append(name)
                print(f"  ++    {name}: compiled by the driver, not listed in "
                      f"{SUPPORT_LIST.name}")
            ir_text = out.split("===IR_START===", 1)[1].split("===IR_END===", 1)[0]
            ir_path.write_text(ir_text)
            exe = workdir / f"{name}{target.exe_suffix()}"
            # Strict backend: where llc is installed the driver is told to use
            # it and nothing else, so a module llc rejects fails this case
            # instead of quietly falling back to llvmlite (which is how four
            # cases with unnumbered temporaries and one with a constant-array
            # type mismatch passed this gate while the native chain could not
            # build them).
            build_out, build_err, build_rc, build_timed = _run(
                [PYTHON, str(DRIVER), "--ir", str(ir_path), "-o", str(exe),
                 "--backend", "llc" if find_llc() else "auto"],
                timeout + 120,
            )
            if build_timed or build_rc != 0 or not exe.exists():
                failures.append(name)
                print(f"  FAIL  {name}: host build of driver IR failed")
                for line in [l for l in build_err.strip().splitlines() if l][-2:]:
                    print(f"        {line}")
                continue
            run_out, run_err, run_rc, run_timed = _run(
                [str(exe)], timeout)
            if run_timed:
                failures.append(name)
                print(f"  FAIL  {name}: compiled program did not terminate")
                continue
            if normalize(run_out) != normalize(expected):
                failures.append(name)
                print(f"  FAIL  {name}: output differs from .expected")
                if verbose:
                    print(_diff(name, expected, run_out))
                continue
            if verbose:
                print(f"  PASS  {name}")

    total = len(cases)
    compiled = total - len(failures) - len(reported)
    print(f"\nNative driver: {compiled}/{total} golden cases compiled by "
          f"compiler/frayc.fray and matched ({len(reported)} reported cleanly)")
    if reported:
        print(f"Reported: {', '.join(reported)}")
    if extra:
        print(f"Not in {SUPPORT_LIST.name}: {', '.join(extra)}")
    if failures:
        print(f"Failures: {', '.join(failures)}")
        return 1
    return 0


def check_stage1(timeout: float, verbose: bool, driver_out: Path = None) -> int:
    """The self-hosted compiler must be able to compile its own source.

    Per module: produce IR with the self-hosted pipeline and have LLVM verify
    it, then compile, link and run it — module-level code executes, so this
    catches crashes the verifier cannot see. A module with a driver in
    tests/selfhost/ additionally has its output compared against the oracle.
    """
    stage1_timeout = max(timeout, 600.0)
    failures = []
    native_ran = False
    with tempfile.TemporaryDirectory(prefix="fray_stage1_") as td:
        workdir = Path(td)
        # compiler/frayc.fray rides along: compile it (it imports the four
        # modules, so a failure here is usually one of theirs), link it, and
        # keep the binary for check_native_driver().
        out, err, rc, timed = compile_selfhosted(NATIVE_DRIVER, stage1_timeout, "ir")
        kind, diags = classify(rc, err, timed)
        if kind != "accepted":
            failures.append("frayc driver")
            print(f"  FAIL  frayc driver: {kind} ({len(diags)} diagnostics)")
            for line in (diags or err.strip().splitlines())[:4]:
                print(f"        {line}")
        else:
            problem = verify_ir(out)
            if problem:
                failures.append("frayc driver")
                print(f"  FAIL  frayc driver: LLVM rejected the IR: {problem}")
            else:
                print(f"  ok    frayc driver: {len(out) / 1024:.0f} KB IR verified")
        driver_bin = workdir / f"frayc_driver{target.exe_suffix()}"
        driver_ready = False
        if "frayc driver" not in failures:
            # Build to a path that survives this run: `run` links into a
            # temporary file, starts it and deletes it, so nothing would be left
            # to hand to the native-driver gate or to --driver.
            build_out, build_err, build_rc, build_timed = _run(
                [PYTHON, str(DRIVER), "build", str(NATIVE_DRIVER),
                 "-o", str(driver_bin), "--timeout", str(stage1_timeout)],
                stage1_timeout + 120,
            )
            if build_timed or build_rc != 0 or not driver_bin.exists():
                failures.append("frayc driver")
                print(f"  FAIL  frayc driver: build failed (exit {build_rc})")
                for line in [l for l in build_err.strip().splitlines() if l][-3:]:
                    print(f"        {line}")
            else:
                # The driver is a command-line program: started with no
                # arguments it prints its usage and exits 2, which is success
                # here. That it compiles real programs is checked below, by the
                # native-driver gate this binary is handed to.
                run_out, run_err, run_rc, run_timed = _run(
                    [str(driver_bin)], stage1_timeout)
                usage = run_rc == 2 and "usage:" in run_out
                if run_timed or (run_rc != 0 and not usage):
                    failures.append("frayc driver")
                    print(f"  FAIL  frayc driver: did not start (exit {run_rc})")
                    for line in [l for l in run_err.strip().splitlines() if l][-3:]:
                        print(f"        {line}")
                else:
                    driver_ready = True
                    note = f"usage, exit {run_rc}" if usage else f"exit {run_rc}"
                    print(f"  ok    frayc driver: built, linked and started ({note})")
        if driver_ready and driver_out is not None:
            shutil.copy2(driver_bin, driver_out)
        if "frayc driver" in failures:
            print("\nSelf-compilation (Stage 1): driver build failed — "
                  "the native-driver gate is skipped")
            print("Failures: frayc driver")
            return 1

        for module in STAGE1_MODULES:
            path = workdir / f"{module}.fray"
            path.write_text(stage1_unit(module))

            out, err, rc, timed = compile_selfhosted(path, stage1_timeout, "ir")
            kind, diags = classify(rc, err, timed)
            if kind != "accepted":
                failures.append(module)
                print(f"  FAIL  {module}: {kind} ({len(diags)} diagnostics)")
                for line in (diags or err.strip().splitlines())[:4]:
                    print(f"        {line}")
                continue
            problem = verify_ir(out)
            if problem:
                failures.append(module)
                print(f"  FAIL  {module}: LLVM rejected the IR: {problem}")
                continue
            ir_kb = len(out) / 1024

            run_out, run_err, run_rc, run_timed = compile_selfhosted(
                path, stage1_timeout, "run")
            run_kind, _ = classify_run(run_timed, run_err)
            if run_kind != "accepted" or run_rc != 0:
                failures.append(module)
                print(f"  FAIL  {module}: build/run failed ({run_kind}, exit {run_rc})")
                for line in [l for l in run_err.strip().splitlines() if l][-3:]:
                    print(f"        {line}")
                continue

            driver = STAGE1_DRIVERS / f"{module}_driver.fray"
            if not driver.exists():
                print(f"  ok    {module}: {ir_kb:.0f} KB IR verified, built and ran")
                continue

            oracle_out, _, oracle_rc, oracle_timed = run_oracle(path, stage1_timeout)
            if oracle_timed or oracle_rc != 0:
                failures.append(module)
                print(f"  FAIL  {module}: driver did not run under the oracle"
                      f" (exit {oracle_rc})")
                continue
            if normalize(run_out) != normalize(oracle_out):
                failures.append(module)
                print(f"  FAIL  {module}: compiled output differs from the oracle")
                if verbose:
                    print(_diff(f"{module}_driver", oracle_out, run_out))
                continue
            print(f"  ok    {module}: {ir_kb:.0f} KB IR verified, built, ran, "
                  "output matches the oracle")

        # Finally, the compiler this run built has to compile real programs:
        # the native-driver gate on that exact binary, so Stage 1 cannot pass
        # with a driver that builds but crashes on input (a bare `KeyError`
        # used to be indistinguishable from an honest rejection here).
        if driver_ready and "frayc driver" not in failures:
            native_ran = True
            print("\nNative-driver gate on the driver this run built:")
            if check_native_driver(timeout, verbose, driver_bin) != 0:
                failures.append("native driver")

    total = len(STAGE1_MODULES) + 1 + (1 if native_ran else 0)
    print(f"\nSelf-compilation (Stage 1): {total - len(failures)}/{total} units "
          "compile to LLVM-verified IR, build and run (incl. the frayc driver"
          f"{' and the golden cases it compiles' if native_ran else ''})")
    if failures:
        print(f"Failures: {', '.join(failures)}")
        return 1
    return 0


def check_stage2(timeout: float, verbose: bool, driver_in: Path = None,
                 backend: str = "auto") -> int:
    """Stage 2: the compiler rebuilds itself, and the result is a fixed point.

    The chain this gate runs is the one the plan's release criterion names:

      v1 = Stage 1 binary (frayc.fray compiled by the Python pipeline)
      v1 compiles frayc.fray          -> IR1
      IR1 -> object -> link           -> v2      (the shipped compiler)
      v2 compiles frayc.fray          -> IR2      (IR1 must equal IR2)
      IR2 -> object -> link           -> v3
      v3 compiles frayc.fray          -> IR3
      require v2 ≡ v3: IR2 == IR3 and, when LLVM's own emitter is used, the two
                       binaries byte-for-byte (llc records the input file name
                       in the object, so both builds read the same .ll path)

    Then the golden suite runs through v2 — the binary a user would ship — so
    "the compiler rebuilds itself" and "the compiler compiles programs" are
    checked as one story rather than two.

    `driver_in` (--driver) skips the Stage 1 build by supplying an existing v1.
    """
    stage2_timeout = max(timeout, 900.0)
    repo_root = str(REPO_ROOT)
    case = str(NATIVE_DRIVER)
    module_root = str(REPO_ROOT / "compiler")
    failures = []

    with tempfile.TemporaryDirectory(prefix="fray_stage2_") as td:
        work = Path(td)
        # One stable IR path for every build in this run: llc writes the input
        # file name into the object, so v2 and v3 only compare byte-for-byte
        # when both were emitted from the same .ll path.
        self_ll = work / "frayc.self.ll"
        v1 = Path(driver_in) if driver_in else None

        if v1 is None:
            v1 = work / f"frayc_v1{target.exe_suffix()}"
            print(f"  ..    building v1 (Stage 1: compiler/frayc.fray via the "
                  f"Python pipeline) — this takes a few minutes")
            out, err, rc, timed = _run(
                [PYTHON, str(DRIVER), "build", case, "-o", str(v1),
                 "--timeout", str(stage2_timeout), "--backend", backend],
                stage2_timeout + 300,
            )
            if timed or rc != 0 or not v1.exists():
                failures.append("v1")
                print(f"  FAIL  v1: Stage 1 build failed (exit {rc})")
                for line in [l for l in err.strip().splitlines() if l][-4:]:
                    print(f"        {line}")
                print("\nStage 2: FAIL (no v1 to run)")
                return 1
            print(f"  ok    v1: built ({v1.stat().st_size / 1024:.0f} KB)")
        else:
            print(f"  ok    v1: supplied ({v1})")

        def report_ir_difference(a: str, b: str, na: str, nb: str) -> None:
            """Say *how* two IR texts differ, not just that they do.

            Both legs of the fixed point compare the output of the *same*
            compiler over the *same* source, so a difference means the result
            depends on something outside the input, and "different" alone does
            not name what. Classify it first — an ordering-only difference
            (same lines, shuffled) points at hash-table iteration; anything
            else points at the emitted code — then keep both texts outside the
            temp dir, which is deleted with this block, and show the first hunk.
            """
            alines, blines = (a or "").splitlines(), (b or "").splitlines()
            same_multiset = sorted(alines) == sorted(blines)
            print(f"        {len(alines)} vs {len(blines)} lines; "
                  + ("same lines in a different order (hash-table order?)"
                     if same_multiset else "line contents differ"))
            kept = REPO_ROOT / "build"
            kept.mkdir(exist_ok=True)
            (kept / f"stage2_{na}.ll").write_text(a or "")
            (kept / f"stage2_{nb}.ll").write_text(b or "")
            print(f"        both kept: {kept}/stage2_{na}.ll, "
                  f"{kept}/stage2_{nb}.ll")
            diff = difflib.unified_diff(alines, blines, na, nb, n=1, lineterm="")
            for n, line in enumerate(diff):
                if n >= 12:
                    print("        …")
                    break
                print("        " + line)

        def compile_frayc(binary: Path, label: str):
            """Run a compiler binary over its own source; return the IR text."""
            out, err, rc, timed = _run(
                [str(binary), case, module_root], stage2_timeout)
            if timed or rc != 0 or "===IR_START===" not in out:
                failures.append(label)
                print(f"  FAIL  {label}: driver neither compiled nor diagnosed"
                      f" (exit {rc})")
                diags = [l for l in (out + "\n" + err).splitlines()
                         if DIAG_RE.match(l)]
                for line in (diags or [l for l in err.strip().splitlines() if l])[:4]:
                    print(f"        {line}")
                return None
            ir = out.split("===IR_START===", 1)[1].split("===IR_END===", 1)[0]
            if not ir.strip():
                failures.append(label)
                print(f"  FAIL  {label}: no IR between the markers")
                return None
            return ir.strip() + "\n"

        def build_from_ir(ir_text: str, out_bin: Path, label: str):
            self_ll.write_text(ir_text)
            out, err, rc, timed = _run(
                [PYTHON, str(DRIVER), "--ir", str(self_ll), "-o", str(out_bin),
                 "--backend", backend], stage2_timeout + 300)
            used = None
            for line in err.splitlines():
                if "Generated object file (" in line:
                    used = line.rsplit("(", 1)[1].rstrip(")")
            if timed or rc != 0 or not out_bin.exists():
                failures.append(label)
                print(f"  FAIL  {label}: host build from IR failed (exit {rc})")
                for line in [l for l in err.strip().splitlines() if l][-4:]:
                    print(f"        {line}")
                return None
            print(f"  ok    {label}: linked ({used or 'unknown'} backend)")
            return used

        ir1 = None
        if "v1" not in failures:
            ir1 = compile_frayc(v1, "v1 -> frayc.fray")
        if ir1 is not None:
            problem = verify_ir(ir1)
            if problem:
                failures.append("IR1")
                print(f"  FAIL  IR1: LLVM rejected it: {problem}")
            else:
                print(f"  ok    v1 -> frayc.fray: {len(ir1) / 1024:.0f} KB of IR, verified by LLVM")

        v2, v3 = work / f"frayc_v2{target.exe_suffix()}", work / f"frayc_v3{target.exe_suffix()}"
        used_backend = None
        if ir1 is not None and "IR1" not in failures:
            used_backend = build_from_ir(ir1, v2, "IR1 -> v2")
        ir2 = None
        if v2.exists():
            ir2 = compile_frayc(v2, "v2 -> frayc.fray")
        if ir2 is not None:
            if ir1 is not None and ir2 == ir1:
                print("  ok    fixed point (Stage 2): v1 and v2 emit identical IR for frayc.fray")
            elif ir1 is not None:
                failures.append("IR1!=IR2")
                print("  FAIL  v1 and v2 emit different IR for the same source")
                report_ir_difference(ir1, ir2, "ir1", "ir2")
            problem = verify_ir(ir2)
            if problem:
                failures.append("IR2")
                print(f"  FAIL  IR2: LLVM rejected it: {problem}")
            else:
                print(f"  ok    v2 -> frayc.fray: {len(ir2) / 1024:.0f} KB of IR, verified by LLVM")

        if ir2 is not None and "IR2" not in failures:
            build_from_ir(ir2, v3, "IR2 -> v3")
        if v3.exists():
            ir3 = compile_frayc(v3, "v3 -> frayc.fray")
            if ir3 is not None:
                if ir2 is not None and ir3 == ir2:
                    print("  ok    fixed point (release criterion): v2 and v3 emit identical IR")
                else:
                    failures.append("IR2!=IR3")
                    print("  FAIL  v2 and v3 emit different IR — not a fixed point")
                    report_ir_difference(ir2, ir3, "ir2", "ir3")
        # Byte-for-byte comparison of the two compiler binaries. llc is
        # deterministic and the input path is the same for both builds, so a
        # difference here is a real difference in the code that was emitted.
        if v2.exists() and v3.exists():
            if v2.read_bytes() == v3.read_bytes():
                print("  ok    v2 == v3: the two compiler binaries are bit-identical"
                      f" ({v2.stat().st_size / 1024:.0f} KB)")
            elif used_backend == "llc":
                failures.append("v2!=v3")
                print("  FAIL  v2 and v3 differ byte-for-byte (llc emitted the "
                      "objects, so their IR differs)")
            else:
                print("  note  v2 and v3 differ byte-for-byte (their IR is "
                      "identical; the object backend is llvmlite, not llc)")

        # The compiler a user would ship has to compile real programs.
        if v2.exists() and "v1 -> frayc.fray" not in failures:
            print("\nNative-driver gate on v2 (the compiler this run built):")
            if check_native_driver(timeout, verbose, v2) != 0:
                failures.append("golden suite on v2")

    print(f"\nStage 2: {'PASS' if not failures else 'FAIL'} "
          "(self-rebuild, fixed point, golden suite)")
    if failures:
        print(f"Failures: {', '.join(failures)}")
        return 1
    return 0


def check_single_unit(timeout: float, verbose: bool) -> int:
    """The compiler modules link into one binary through native imports.

    Stage 1 builds each module separately; nothing proves they can coexist,
    because they define the same top-level names (`get_errors`, `clear_errors`,
    `node_type`, `node_get` and the private `errors` list). compiler/main.fray
    imports all of them, and the self-hosted compiler's linker resolves those
    imports and renames the collisions to `<module>_<name>` — no host-side
    amalgamation. This builds main.fray with the self-hosted pipeline, links
    it, and runs the resulting single binary; its output is diffed against the
    oracle's on the same source, so the binary must be the same compiler the
    oracle describes.
    """
    path = REPO_ROOT / "compiler" / "main.fray"
    if not path.exists():
        print(f"  no program at {path} — nothing to link")
        return 0

    # Linking all the compiler modules under the oracle is the slow part, so
    # this gate gets a generous ceiling rather than the golden-run timeout.
    single_timeout = max(timeout, 600.0)
    failures = []

    # `run` compiles the program to IR, has LLVM verify it while emitting the
    # object, links it against the runtime and runs it — so success here means
    # the single binary was really built, not just parsed.
    run_out, run_err, run_rc, run_timed = compile_selfhosted(
        path, single_timeout, "run")
    kind, diags = classify_run(run_timed, run_err)
    if kind != "accepted" or run_rc != 0:
        failures.append("build")
        print(f"  FAIL  single unit: {kind} (exit {run_rc})")
        for line in (diags or run_err.strip().splitlines())[-4:]:
            print(f"        {line}")

    oracle_out, _, oracle_rc, oracle_timed = run_oracle(path, timeout)
    if oracle_timed or oracle_rc != 0:
        failures.append("oracle")
        print(f"  FAIL  single unit: oracle did not run it (exit {oracle_rc})")
    elif not failures and normalize(run_out) != normalize(oracle_out):
        failures.append("divergence")
        print("  FAIL  single unit: the binary's output differs from the oracle")
        if verbose:
            print(_diff("single_unit", oracle_out, run_out))
    elif not failures:
        n = len(normalize(run_out).splitlines())
        print(f"  ok    single unit: compiler/main.fray built, linked and ran as "
              f"one binary; {n} output lines match the oracle")
        if verbose:
            for line in normalize(run_out).splitlines():
                print(f"        {line}")

    print(f"\nSingle-unit build: {'PASS' if not failures else 'FAIL'} "
          "(all compiler modules in one binary, via native imports)")
    if failures:
        print(f"Failures: {', '.join(failures)}")
        return 1
    return 0


def check_package_imports(timeout: float, verbose: bool) -> int:
    """Package initializers, relative imports and re-exports.

    Two scenarios, both through the self-hosted compiler:

      1. Dotted module paths resolve into subdirectories, like Python
         packages. The self-hosted linker resolves `import a.b` and
         `from a.b import c` against the module catalog, where a nested file
         `a/b.fray` is the module `a.b`. Two packages here define the same
         function name, so a wrong prefix shows up as the wrong number rather
         than as a link error. The oracle and the bootstrap codegen handle
         `import a.b`'s qualified name differently (the reference binds the
         leaf module to the first segment), so this case is checked against an
         expected output rather than against the oracle.

      2. A package initializer (`pkg/__init__.fray`) names the package, and its
         relative imports (`from .util import twice`, `from . import more`)
         let the package be split across files and re-export names from it.
         `from pkg import twice` reaches `pkg/util.fray` through the
         initializer, and a nested initializer (`pkg/sub/__init__.fray`) uses
         `..util` to climb to its parent's sibling.

      3. Two modules that define the same *enum* name: the link pass renames
         both (`shapes_Kind`, `other_Kind`), and a qualified match arm
         (`case Kind.Circle(r):`) has to follow the rename — it is compared
         against the name the enum's instances carry, so a qualifier left in
         its pre-link spelling would never match.
    """
    scenarios = [
        (
            "dotted",
            {
                "a/b.fray": "def f():\n    return 42\n\nVALUE = 7\n",
                "c/d.fray": "def f():\n    return 99\n",
                "main.fray": ("import a.b\n"
                              "import c.d\n"
                              "from a.b import f\n"
                              "print(a.b.f())\n"
                              "print(c.d.f())\n"
                              "print(a.b.VALUE)\n"
                              "print(f())\n"),
            },
            "42\n99\n7\n42",
            "import a.b / from a.b import c resolved to a/b.fray; output correct",
        ),
        (
            "package-init",
            {
                "pkg/__init__.fray": ("from .util import twice\n"
                                      "from . import more\n"
                                      "\n"
                                      "def describe(n):\n"
                                      '    return "pkg:" + str(twice(n)) + ":" + str(more.three())\n'),
                "pkg/util.fray": "def twice(x):\n    return x * 2\n",
                "pkg/more.fray": "def three():\n    return 3\n",
                "pkg/sub/__init__.fray": ("from ..util import twice\n"
                                          "\n"
                                          "def four(x):\n"
                                          "    return twice(twice(x))\n"),
                "main.fray": ("from pkg import describe, twice\n"
                              "from pkg.sub import four\n"
                              "print(describe(4))\n"
                              "print(twice(5))\n"
                              "print(four(3))\n"),
            },
            "pkg:8:3\n10\n12",
            "pkg/__init__.fray re-exported twice from pkg/util.fray; "
            ".util / . import more / ..util resolved; output correct",
        ),
        (
            "enum-namespace",
            {
                "shapes.fray": ("enum Kind:\n"
                                "    case Circle(r)\n"
                                "    case Square(s)\n"
                                "\n"
                                "def area():\n"
                                '    return "shapes"\n'),
                # Same enum name and same function name, so the link pass has
                # to rename both modules' symbols (shapes_Kind, other_Kind,
                # shapes_area, other_area).
                "other.fray": ("enum Kind:\n"
                               "    case Wheel(r)\n"
                               "\n"
                               "def area():\n"
                               '    return "other"\n'),
                "main.fray": ("from shapes import Kind, area\n"
                              "import other\n"
                              "\n"
                              "c = Kind.Circle(5)\n"
                              "match c:\n"
                              "    case Kind.Square(s):\n"
                              '        print("square " + str(s))\n'
                              "    case Kind.Circle(r):\n"
                              '        print("circle " + str(r))\n'
                              "\n"
                              "w = other.Kind.Wheel(2)\n"
                              "match w:\n"
                              "    case Kind.Circle(r):\n"
                              '        print("circle " + str(r))\n'
                              "    case _:\n"
                              '        print("other " + w._variant)\n'
                              "\n"
                              'print(area())\n'),
            },
            "circle 5\nother Wheel\nshapes",
            "colliding enum names renamed (shapes_Kind / other_Kind) and a "
            "qualified match arm follows the rename; output correct",
        ),
    ]

    failures = 0
    for label, files, expected, ok_msg in scenarios:
        with tempfile.TemporaryDirectory(prefix=f"fray_packages_{label}_") as td:
            root = Path(td)
            for rel, text in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            main = root / "main.fray"

            out, err, rc, timed = compile_selfhosted(main, max(timeout, 300.0), "run")
            kind, diags = classify_run(timed, err)
            if kind != "accepted" or rc != 0:
                print(f"  FAIL  packages/{label}: {kind} (exit {rc})")
                for line in (diags or err.strip().splitlines())[-4:]:
                    print(f"        {line}")
                failures += 1
                continue
            if normalize(out) != normalize(expected):
                print(f"  FAIL  packages/{label}: output differs from expected")
                if verbose:
                    print(_diff(f"packages/{label}", expected, out))
                failures += 1
                continue
            print(f"  ok    packages/{label}: {ok_msg}")
            if verbose:
                print(f"        expected {expected!r}, got {normalize(out)!r}")

    if failures:
        print("\nPackage imports: FAIL")
        return 1
    print("\nPackage imports: PASS (dotted paths, initializers and relative imports)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="fray self-hosted frontend gate")
    ap.add_argument("--stage1", action="store_true",
                    help="compile the self-hosted compiler's own modules with it")
    ap.add_argument("--driver", type=str, default=None,
                    help="with --stage1: copy the built compiler driver binary "
                         "(compiler/frayc.fray) to this path; with --stage2: use "
                         "this binary as v1 instead of rebuilding it")
    ap.add_argument("--native-gate", type=str, default=None,
                    help="run the golden cases through a previously built "
                         "native driver binary (the path --stage1 --driver "
                         "produced); the host only emits objects and links")
    ap.add_argument("--single-unit", action="store_true",
                    help="link the compiler modules into one binary through "
                         "native imports (compiler/main.fray)")
    ap.add_argument("--stage2", action="store_true",
                    help="the compiler rebuilds itself: v1 -> IR -> v2 -> IR -> "
                         "v3, the fixed point (v2 ≡ v3, bit-identical with "
                         "llc) and the golden suite through v2")
    ap.add_argument("--backend", choices=("auto", "llc", "llvmlite"), default="auto",
                    help="object backend for the stage 2 builds (default: auto "
                         "— llc when installed, which keeps Python out of the "
                         "compile loop)")
    ap.add_argument("--packages", action="store_true",
                    help="check dotted module imports resolve into packages")
    ap.add_argument("--probes", action="store_true",
                    help="run the unsupported-syntax matrix instead of golden cases")
    ap.add_argument("--run", action="store_true",
                    help="compile, link and run golden cases (slower, checks output)")
    ap.add_argument("--update", action="store_true",
                    help="rewrite tests/selfhosted_supported.txt")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="seconds per program before it counts as a hang (default: 20)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    started = time.time()
    print(f"Self-hosted frontend gate ({PYTHON})")
    if args.stage1:
        rc = check_stage1(args.timeout, args.verbose,
                          Path(args.driver) if args.driver else None)
    elif args.native_gate:
        rc = check_native_driver(args.timeout, args.verbose, Path(args.native_gate))
    elif args.stage2:
        rc = check_stage2(args.timeout, args.verbose,
                          Path(args.driver) if args.driver else None,
                          args.backend)
    elif args.single_unit:
        rc = check_single_unit(args.timeout, args.verbose)
    elif args.packages:
        rc = check_package_imports(args.timeout, args.verbose)
    elif args.probes:
        rc = check_probes(args.timeout, args.verbose)
    else:
        rc = check_cases(args.timeout, args.run, args.verbose, args.update)
    print(f"Finished in {time.time() - started:.1f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
