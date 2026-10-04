#!/usr/bin/env python3
"""
frayc-selfhosted — Self-hosted compiler driver.

Runs the self-hosted lexer→parser→link→sema→codegen pipeline through the
oracle, then feeds the resulting LLVM IR to LLVM's own `llc` for native
compilation. `link` resolves the program's `import`s against a module catalog
the host builds from the source's directory and the compiler's own directory.

Usage:
    python tools/frayc_selfhosted.py build program.fray -o program
    python tools/frayc_selfhosted.py run program.fray
    python tools/frayc_selfhosted.py ir program.fray
"""

import argparse
import contextlib
import io
import os
import shutil
import signal
import sys
import subprocess
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bootstrap"))

import target  # platform detection (bootstrap/target.py)

# The wrapper source: reads the _SOURCE_INPUT, _FILENAME and
# _MODULE_CATALOG globals the host defines, runs the self-hosted compiler
# pipeline (lex → parse → link → sema → codegen), and prints IR between
# markers.
#
# `link` resolves the program's imports against _MODULE_CATALOG — a map from
# module name to source that the host builds by reading the source's directory
# and the compiler's own directory (the self-hosted compiler has no file I/O).
# That is what lets a program that `import`s modules compile to one binary
# without any host-side text mangling.
#
# Each stage runs only if the previous one reported no errors, and every
# stage reports diagnostics as "<STAGE> ERROR: <line>:<col>: <message>" —
# later stages never see input a previous stage already rejected.
STAGE_MARKERS = ("LEX ERROR: ", "PARSE ERROR: ", "LINK ERROR: ",
                 "SEMA ERROR: ", "CODEGEN ERROR: ")

WRAPPER_LINES = [
    "import lexer",
    "import parser",
    "import sema",
    "import codegen",
    "import link",
    "",
    "src = _SOURCE_INPUT",
    "tokens = lexer.tokenize(src)",
    "lerrs = lexer.get_errors()",
    "if len(lerrs) > 0:",
    "    i = 0",
    "    while i < len(lerrs):",
    '        print("LEX ERROR: " + _FILENAME + ":" + lerrs[i])',
    "        i = i + 1",
    "else:",
    "    ast = parser.parse(tokens)",
    "    perrs = parser.get_errors()",
    "    if len(perrs) > 0:",
    "        i = 0",
    "        while i < len(perrs):",
    '            print("PARSE ERROR: " + _FILENAME + ":" + perrs[i])',
    "            i = i + 1",
    "    else:",
    "        linked = link.link(ast, _MODULE_CATALOG, _PACKAGE_INIT)",
    "        linkerrs = link.get_errors()",
    "        if len(linkerrs) > 0:",
    "            i = 0",
    "            while i < len(linkerrs):",
    '                print("LINK ERROR: " + _FILENAME + ":" + linkerrs[i])',
    "                i = i + 1",
    "        else:",
    '            errs = sema.analyze(linked, _FILENAME)',
    "            if len(errs) > 0:",
    "                i = 0",
    "                while i < len(errs):",
    '                    print("SEMA ERROR: " + _FILENAME + ":" + errs[i])',
    "                    i = i + 1",
    "            else:",
    "                ir = codegen.compile_ir(linked)",
    "                cerrs = codegen.get_errors()",
    "                if len(cerrs) > 0:",
    "                    i = 0",
    "                    while i < len(cerrs):",
    '                        print("CODEGEN ERROR: " + _FILENAME + ":" + cerrs[i])',
    "                        i = i + 1",
    "                else:",
    '                    print("===IR_START===")',
    "                    print(ir)",
    '                    print("===IR_END===")',
]


class SelfHostedCompileError(RuntimeError):
    """The self-hosted compiler rejected the program, with diagnostics printed."""


class SelfHostedTimeout(RuntimeError):
    """The self-hosted compiler did not terminate (a compiler bug)."""


def _diagnostics(output: str):
    """Diagnostic lines the wrapper printed, in stage order."""
    return [line for line in output.split("\n") if line.startswith(STAGE_MARKERS)]


@contextlib.contextmanager
def _wall_clock_guard(seconds: int):
    """Turn a non-terminating compiler into a clear failure.

    The pipeline runs as fray source inside the oracle, so a frontend bug used
    to surface as an infinite loop (the parser's struct-field hang). Every
    frontend loop now guarantees progress — see compiler/parser.fray — and this
    is the backstop for anything a future change reintroduces, so a hang can
    never look like a compiler that simply takes forever.
    """
    if not hasattr(signal, "SIGALRM") or seconds <= 0:  # pragma: no cover
        yield
        return

    def on_alarm(signum, frame):
        raise SelfHostedTimeout(
            f"the self-hosted compiler did not terminate within {seconds}s "
            "(this is a compiler bug — it must always terminate)"
        )

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _module_catalog(source_path: str):
    """The module catalog and package set for the program's import search path.

    The self-hosted compiler resolves imports itself but cannot read files, so
    the host supplies every candidate module. Two roots are searched: the
    program's own directory (its siblings, and subdirectories as dotted paths —
    `pkg/a/b.fray` is the module `pkg.a.b`), the compiler's directory (so a
    driver can import lexer/parser/sema/codegen/link) and the standard library
    (`$FRAY_STDLIB`, or `stdlib/` beside `bootstrap/`), so `import random`
    means the same thing from any directory. The program's directory wins a
    name clash; the linker picks out the modules actually imported,
    recursively.

    A package initializer (`pkg/__init__.fray`) is registered under the package
    name `pkg`, and `pkg` is listed as a package — that is what lets the linker
    resolve a relative import (`from .util import twice`) from the right level.
    A plain `pkg.fray` beside a `pkg/` directory wins, matching the other
    engines. Returns `(catalog, packages)`.
    """
    from evaluator import FrayMap
    from modules import stdlib_root

    catalog = FrayMap()
    packages = FrayMap()
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    source_dir = os.path.dirname(os.path.abspath(source_path))
    compiler_dir = os.path.normpath(os.path.join(tools_dir, "..", "compiler"))
    for root in (source_dir, compiler_dir, stdlib_root()):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Deterministic order, and skip build noise.
            dirnames[:] = sorted(d for d in dirnames
                                 if d != "__pycache__" and not d.startswith("."))
            for entry in sorted(filenames):
                if not entry.endswith(".fray"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, entry), root)
                parts = rel[: -len(".fray")].split(os.sep)
                is_init = parts[-1] == "__init__"
                if is_init:
                    if len(parts) < 2:
                        continue  # a top-level __init__.fray names no package
                    name = ".".join(parts[:-1])
                else:
                    name = ".".join(parts)
                if name in catalog:
                    continue  # a plain module file wins over a package directory
                with open(os.path.join(dirpath, entry), "r", encoding="utf-8") as f:
                    catalog[name] = f.read()
                if is_init:
                    packages[name] = True
    return catalog, packages


def compile_to_ir_selfhosted(source: str, filename: str = "<string>",
                             timeout: float = 120.0) -> str:
    """Run the self-hosted compiler pipeline through the oracle to produce LLVM IR.

    Raises SelfHostedCompileError (diagnostics already printed to stderr) when
    the frontend rejects the program, and SelfHostedTimeout when the compiler
    itself fails to terminate.
    """
    from evaluator import Evaluator, tokenize, parse, analyze

    wrapper = "\n".join(WRAPPER_LINES)

    # Parse and run the wrapper, injecting source as global variables
    # Use compiler/dummy.fray as filename so imports resolve from compiler/
    compiler_dir = os.path.join(os.path.dirname(__file__), "..", "compiler")
    dummy_filename = os.path.join(compiler_dir, "dummy.fray")

    tokens = tokenize(wrapper, dummy_filename)
    ast = parse(tokens, dummy_filename)
    analyze(ast, dummy_filename)

    captured = io.StringIO()
    try:
        with _wall_clock_guard(timeout), contextlib.redirect_stdout(captured):
            evaluator = Evaluator(filename=dummy_filename)
            catalog, packages = _module_catalog(filename)
            evaluator.env.define("_SOURCE_INPUT", source)
            evaluator.env.define("_FILENAME", filename)
            evaluator.env.define("_MODULE_CATALOG", catalog)
            evaluator.env.define("_PACKAGE_INIT", packages)
            evaluator.run(ast)
    except SelfHostedTimeout:
        raise
    except Exception as e:
        # A crash inside the compiler (not a diagnostic about the input): show
        # whatever the frontend managed to report, then say plainly that this
        # is an internal error so it is not mistaken for a problem in the
        # program being compiled.
        debug_out = captured.getvalue()
        diagnostics = _diagnostics(debug_out)
        for line in diagnostics:
            print(line, file=sys.stderr)
        print(f"Internal error in the self-hosted compiler: {type(e).__name__}: {e}",
              file=sys.stderr)
        if debug_out.strip() and not diagnostics:
            print("Compiler output before the error:", file=sys.stderr)
            print(debug_out, file=sys.stderr)
        raise

    output = captured.getvalue()

    # Extract IR between markers
    start_marker = "===IR_START==="
    end_marker = "===IR_END==="

    start_idx = output.find(start_marker)
    end_idx = output.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        diagnostics = _diagnostics(output)
        for line in diagnostics:
            print(line, file=sys.stderr)
        if diagnostics:
            raise SelfHostedCompileError(
                f"self-hosted compilation failed with {len(diagnostics)} error(s)")
        print(f"Self-hosted compiler output:\n{output}", file=sys.stderr)
        raise RuntimeError("Self-hosted compiler did not produce IR output")

    ir_text = output[start_idx + len(start_marker):end_idx].strip()
    return ir_text


def _normalize_ir(ir_text: str) -> str:
    """Strip target triple/datalayout lines so IR comparisons (diff mode)
    stay meaningful across compilers that target different hosts."""
    return "\n".join(
        line for line in ir_text.splitlines()
        if not line.startswith("target triple =")
        and not line.startswith("target datalayout =")
    )





def find_llc():
    """Path to LLVM's `llc`, or None when it is not installed.

    `llc` is the backend the release chain uses: the frontend is already a
    native binary (compiler/frayc.fray), and going IR → object with LLVM's own
    tool keeps Python out of the compile loop entirely (frayc → llc → cc →
    binary). It is also strict about the IR text — every function's unnamed
    temporaries must start at %0 — so it double-checks what the frontend emits.

    Newest first, then the unversioned name. Distribution packages are
    versioned (`llc-22`), and a machine can carry several at once, so the order
    is a choice rather than an accident: newest first, so a box with both
    LLVM 14 and a current LLVM uses the current one.
    """
    for name in tuple(f"llc-{major}" for major in range(22, 13, -1)) + ("llc",):
        path = shutil.which(name)
        if path:
            return path
    return None


def runtime_source_paths(runtime_dir: str = None):
    if runtime_dir is None:
        runtime_dir = os.path.join(os.path.dirname(__file__), "..", "runtime")
    sources = ["objects.c", "cycles.c", "ops.c", "printing.c", "builtins.c",
               "threads.c", "atomics.c", "coroutine.c", "io.c", "structs.c",
               "maps.c", "option_result.c"]
    return runtime_dir, [os.path.join(runtime_dir, name) for name in sources]


def link_object(obj_path: str, output_path: str, runtime_dir: str = None):
    """Link one object with the fray runtime into an executable.

    The runtime is compiled from source here rather than taken from a
    prebuilt archive: a stale libfrayrt.a links cleanly until the program
    calls a new runtime entry point, which is a confusing failure (`undefined
    reference` at best, a silent old behaviour at worst).
    """
    runtime_dir, runtime_sources = runtime_source_paths(runtime_dir)
    gcc = target.find_c_compiler()
    if not gcc:
        raise RuntimeError("C compiler not found (need gcc, clang or cc) — cannot link runtime")

    runtime_objs = []
    try:
        for rc in runtime_sources:
            if not os.path.exists(rc):
                continue
            ro = output_path + ".rt." + os.path.basename(rc) + ".o"
            result = subprocess.run([gcc, "-c", "-O2", "-o", ro, rc],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                # The link and llc steps below both report the compiler's own
                # diagnostics on failure. This one used capture_output with
                # check=True, so a failed C compile surfaced nothing but an
                # exit status and the actual error never reached the log --
                # which is why a real compile failure on one platform could not
                # be told apart from a missing toolchain.
                print(f"C compile error in {rc}:\n{result.stderr}",
                      file=sys.stderr)
                raise subprocess.CalledProcessError(
                    result.returncode, [gcc, "-c", "-O2", "-o", ro, rc])
            runtime_objs.append(ro)

        cmd = [gcc, "-o", output_path, obj_path] + runtime_objs
        # Linux: link non-PIE — the emitted objects use small-code-model
        # absolute relocations (non-PIC).
        if not target.is_windows() and not target.is_macos():
            cmd += ["-no-pie"]
        cmd += ["-lm", "-lpthread", "-lgcc"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Link error:\n{result.stderr}\n{result.stdout}", file=sys.stderr)
            raise RuntimeError(f"Linker failed with exit code {result.returncode}")
    finally:
        for ro in runtime_objs:
            if os.path.exists(ro):
                os.remove(ro)


def compile_ir_file(ir_path: str, output_path: str, runtime_dir: str = None,
                    backend: str = "auto") -> str:
    """Emit an object from an IR *file* with `llc` and link it.

    The IR arrives as a file (the native driver prints it between markers) and
    stays one all the way to `llc`: nothing parses it on the host side. Note
    that llc records the input file name in the object, so two builds compare
    byte-for-byte only when they are given the same `ir_path`.

    `backend` is accepted and ignored. It used to choose between `llc` and a
    llvmlite fallback, and the fallback was a liability rather than a safety
    net: it is a second, different LLVM, so when `llc` rejected the frontend's
    IR the build quietly re-checked it against something more lenient and went
    green. That is how four cases with unnumbered temporaries and one with a
    constant-array type mismatch passed a gate the native chain could not
    build. There is one emitter now, it is LLVM's, and its verdict is final.
    """
    del backend
    llc = find_llc()
    if not llc:
        raise RuntimeError(
            "llc not found — LLVM is required to emit objects. Install LLVM "
            "(e.g. `apt-get install llvm-22`) and retry.")
    with tempfile.NamedTemporaryFile(suffix=".o", delete=False) as tmp:
        obj_path = tmp.name
    try:
        result = subprocess.run([llc, "-filetype=obj", ir_path, "-o", obj_path],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"llc error:\n{result.stderr}", file=sys.stderr)
            raise RuntimeError(
                f"llc failed to emit an object from {ir_path} "
                f"(exit {result.returncode})")
        print(f"  Generated object file (llc)", file=sys.stderr)
        link_object(obj_path, output_path, runtime_dir)
        print(f"  Linked {output_path}", file=sys.stderr)
        return "llc"
    finally:
        if os.path.exists(obj_path):
            os.remove(obj_path)



def compile_program(source_path: str, output_path: str, runtime_dir: str = None,
                    timeout: float = 120.0, backend: str = "auto") -> str:
    """Full compilation: self-hosted pipeline → LLVM IR → object → native binary."""
    with open(source_path, "r") as f:
        source = f.read()

    # Progress goes to stderr: stdout belongs to the compiled program, so a
    # wrapper that pipes `run` output must see only the program's own output.
    print(f"Compiling {source_path} with self-hosted compiler...", file=sys.stderr)
    ir_text = compile_to_ir_selfhosted(source, source_path, timeout=timeout)
    print(f"  Generated {len(ir_text)} bytes of LLVM IR", file=sys.stderr)

    return link_ir(ir_text, output_path, runtime_dir, backend)


def link_ir(ir_text: str, output_path: str, runtime_dir: str = None,
            backend: str = "auto") -> str:
    """Object + link half of compile_program, for IR that is already in hand.

    The native driver (compiler/frayc.fray) prints its IR instead of building
    it, so the host's job is only to emit the object and link the runtime —
    the frontend ran inside the driver binary. See `--ir`.
    """
    with tempfile.NamedTemporaryFile(suffix=".ll", delete=False, mode="w") as tmp:
        tmp.write(ir_text)
        ir_path = tmp.name
    try:
        return compile_ir_file(ir_path, output_path, runtime_dir, backend)
    finally:
        if os.path.exists(ir_path):
            os.remove(ir_path)


def _compile_failed(e, label: str = "Error"):
    """Report a self-hosted failure. Diagnostics were printed as they were found,
    so this is the one-line summary that explains why the run stopped."""
    print(f"{label}: {e}", file=sys.stderr)
    return 1


def cmd_build(args):
    source_path = args.source
    output_path = args.output
    if not output_path:
        output_path = os.path.splitext(source_path)[0]
    try:
        compile_program(source_path, output_path, timeout=args.timeout,
                        backend=args.backend)
    except (SelfHostedCompileError, SelfHostedTimeout) as e:
        sys.exit(_compile_failed(e))
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_run(args):
    source_path = args.source
    with tempfile.NamedTemporaryFile(suffix=target.exe_suffix() or ".out",
                                     delete=False) as tmp:
        binary_path = tmp.name
    try:
        try:
            compile_program(source_path, binary_path, timeout=args.timeout,
                            backend=args.backend)
        except (SelfHostedCompileError, SelfHostedTimeout) as e:
            sys.exit(_compile_failed(e))
        result = subprocess.run([binary_path])
        sys.exit(result.returncode)
    finally:
        if os.path.exists(binary_path):
            os.remove(binary_path)


def cmd_ir(args):
    source_path = args.source
    with open(source_path, "r") as f:
        source = f.read()
    try:
        ir_text = compile_to_ir_selfhosted(source, source_path, timeout=args.timeout)
        print(ir_text)
    except (SelfHostedCompileError, SelfHostedTimeout) as e:
        sys.exit(_compile_failed(e))
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_diff(args):
    source_path = args.source
    with open(source_path, "r") as f:
        source = f.read()

    from codegen import compile_to_ir as python_ir

    try:
        selfhosted = compile_to_ir_selfhosted(source, source_path, timeout=args.timeout)
    except (SelfHostedCompileError, SelfHostedTimeout) as e:
        sys.exit(_compile_failed(e, "Self-hosted error"))
    except Exception as e:
        print(f"Self-hosted error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        python = python_ir(source, source_path)
    except Exception as e:
        print(f"Python codegen error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    if _normalize_ir(selfhosted).strip() == _normalize_ir(python).strip():
        print("IR output matches!")
    else:
        print("IR output differs.")
        with open("/tmp/selfhosted_ir.ll", "w") as f:
            f.write(selfhosted)
        with open("/tmp/python_ir.ll", "w") as f:
            f.write(python)
        subprocess.run(["diff", "--color=auto", "-u", "/tmp/python_ir.ll", "/tmp/selfhosted_ir.ll"])
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="fray self-hosted compiler")
    subparsers = parser.add_subparsers(dest="command")

    timeout_help = ("seconds before the compiler is declared non-terminating "
                    "(default: 120)")

    build_parser = subparsers.add_parser("build", help="Compile .fray to native binary")
    build_parser.add_argument("source", help="Source file (.fray)")
    build_parser.add_argument("-o", "--output", help="Output binary path")
    build_parser.add_argument("--timeout", type=float, default=120.0, help=timeout_help)

    run_parser = subparsers.add_parser("run", help="Compile and run .fray")
    run_parser.add_argument("source", help="Source file (.fray)")
    run_parser.add_argument("--timeout", type=float, default=120.0, help=timeout_help)

    ir_parser = subparsers.add_parser("ir", help="Show LLVM IR from self-hosted compiler")
    ir_parser.add_argument("source", help="Source file (.fray)")
    ir_parser.add_argument("--timeout", type=float, default=120.0, help=timeout_help)

    # Objects come from LLVM's `llc` and nothing else. The flag survives as a
    # no-op so existing invocations (the gates, the docs) keep working; there
    # is no longer a second backend for it to select.
    backend_help = ("accepted and ignored: objects are always emitted by "
                    "LLVM's `llc`")
    for sub in (build_parser, run_parser):
        sub.add_argument("--backend", choices=("auto", "llc"),
                         default="auto", help=backend_help)
    parser.add_argument("--backend", choices=("auto", "llc"),
                        default="auto", help=backend_help)

    # Host-side half of the native-driver contract: the driver prints the IR
    # (===IR_START===/===IR_END===) and this turns that text into a binary.
    parser.add_argument("--ir", metavar="IR_FILE",
                        help="compile LLVM IR text (frontend already ran) to a binary")
    parser.add_argument("-o", "--output", help="output binary path (with --ir)")

    diff_parser = subparsers.add_parser("diff", help="Compare self-hosted IR with Python IR")
    diff_parser.add_argument("source", help="Source file (.fray)")
    diff_parser.add_argument("--timeout", type=float, default=120.0, help=timeout_help)

    args = parser.parse_args()

    if args.ir:
        output_path = args.output or os.path.splitext(args.ir)[0]
        try:
            # Straight from the file: the IR is never parsed on the host side
            # (llc reads it), so the driver's output goes to the backend
            # untouched.
            compile_ir_file(args.ir, output_path, backend=args.backend)
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "build":
        cmd_build(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "ir":
        cmd_ir(args)
    elif args.command == "diff":
        cmd_diff(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
