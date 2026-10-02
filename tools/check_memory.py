#!/usr/bin/env python3
"""
fray memory checker.

Builds fray programs with an AddressSanitizer-instrumented runtime and runs
them, failing if any of them reports a leak or a memory error. The compiled
object itself is not instrumented (llvmlite emits it), but every call it makes
into the runtime is, which is where fray's allocation, refcounting, GC, thread
and coroutine machinery lives.

This is the gate that keeps "no leaks" true by construction rather than by
occasional manual inspection: the runtime owns a program's memory, so anything
it forgets to hand back shows up here scaled to the workload — a per-element
leak is megabytes, a per-program leak is one box.

Companion to `make -C runtime asan`, which runs the C runtime's own test
suite (refcounting, cycle collector, threads, coroutines) under the same
sanitizer. This tool covers the other half: the code the compiler emits,
run against an instrumented runtime.

Usage:
    python tools/check_memory.py            # benchmarks (the heavy allocators)
    python tools/check_memory.py --all      # benchmarks + every golden case
    python tools/check_memory.py --keep-going

Requires gcc/clang with libasan (Linux) or clang with ASan (macOS). Skips
cleanly, with a warning, when no sanitizer-capable compiler is present.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_DIR = REPO_ROOT / "bootstrap"
TOOLS_DIR = REPO_ROOT / "tools"
RUNTIME_DIR = REPO_ROOT / "runtime"
CASES_DIR = REPO_ROOT / "tests" / "cases"
BENCH_DIR = REPO_ROOT / "benchmarks"
sys.path.insert(0, str(BOOTSTRAP_DIR))

DIAG_RE = re.compile(r"^(LEX|PARSE|LINK|SEMA|CODEGEN) ERROR: ")

import target  # noqa: E402  (bootstrap/target.py)


def _asan_flags(cc: str, probe: str) -> list | None:
    """Flags that enable AddressSanitizer with leak detection, or None when
    the compiler cannot link an instrumented binary."""
    flags = ["-fsanitize=address", "-fno-omit-frame-pointer", "-g", "-O1"]
    if target.is_windows():
        # ASan on MinGW-w64 needs a runtime DLL llvmlite's gcc does not ship;
        # the leak check is a Linux/macOS gate.
        return None
    try:
        subprocess.run([cc] + flags + ["-x", "c", probe, "-o", probe + ".out"],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError:
        return None
    return flags


def _build_runtime_objects(cc: str, flags: list, workdir: Path, log) -> list:
    """Compile each runtime translation unit once, instrumented."""
    sys.path.insert(0, str(BOOTSTRAP_DIR))
    import codegen

    objs = []
    for name in codegen.RUNTIME_SOURCES:
        obj = workdir / (name + ".o")
        log(f"  cc {name}")
        subprocess.run(
            [cc, "-c", "-std=gnu11"] + flags +
            ["-I", str(RUNTIME_DIR), "-o", str(obj), str(RUNTIME_DIR / name)],
            check=True, capture_output=True,
        )
        objs.append(str(obj))
    return objs


def _run_case(source_path: Path, workdir: Path, cc: str, flags: list,
              rt_objs: list, log) -> tuple[bool, str]:
    """Compile and run one program; return (clean, detail)."""
    sys.path.insert(0, str(BOOTSTRAP_DIR))
    import codegen

    name = source_path.stem
    obj = workdir / (name + ".o")
    exe = workdir / (name + ".bin")
    source = source_path.read_text()

    codegen.compile_to_object(source, str(obj), str(source_path))
    # Same link model as the compiler: the emitted objects use absolute
    # relocations, so a PIE link is not an option on Linux.
    link = [cc] + flags
    if not target.is_windows() and not target.is_macos():
        link.append("-no-pie")
    subprocess.run(
        link + ["-o", str(exe), str(obj)] + rt_objs + ["-lm", "-lpthread"],
        check=True, capture_output=True,
    )

    return _run_under_asan(exe)


def _run_under_asan(exe: Path) -> tuple[bool, str]:
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1")
    try:
        proc = subprocess.run([str(exe)], capture_output=True, text=True,
                              env=env, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "timed out after 600s"

    # ASan reports both memory errors and leaks on stderr; either fails.
    err = proc.stderr
    for line in err.splitlines():
        if "Sanitizer:" in line and "SUMMARY" in line:
            return False, line.split("Sanitizer: ", 1)[-1]
    if "Sanitizer" in err:
        first = next((l for l in err.splitlines() if "Sanitizer" in l), "")
        return False, first.strip()
    return True, proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def _run_case_driver(source_path: Path, workdir: Path, cc: str, flags: list,
                     rt_objs: list, log, driver: Path) -> tuple[bool, str]:
    """Same check, but the object is compiled by the native compiler driver.

    This is the memory gate for the self-hosted codegen: the bootstrap path
    above checks the Python emitter, and the ownership rules the two share
    (a callee owns its parameters, argument lists own what they carry, and a
    caller owns what a call returns) are exactly what drifts between them.
    The driver's IR is emitted to an object by llc, or by llvmlite where llc
    is not installed.
    """
    import shutil
    sys.path.insert(0, str(TOOLS_DIR))
    import frayc_selfhosted as host

    name = source_path.stem
    obj = workdir / (name + ".driver.o")
    exe = workdir / (name + ".driver.bin")
    result = subprocess.run([str(driver), str(source_path)],
                            capture_output=True, text=True, timeout=600)
    out = result.stdout
    if result.returncode != 0 or "===IR_START===" not in out:
        diags = [l for l in out.splitlines() if DIAG_RE.match(l)]
        raise RuntimeError(diags[0][:160] if diags
                           else (result.stderr.strip().splitlines() or
                                 [f"driver exit {result.returncode}"])[-1][:160])
    ir_text = out.split("===IR_START===", 1)[1].split("===IR_END===", 1)[0]
    ir_path = workdir / (name + ".driver.ll")
    ir_path.write_text(ir_text)
    # The driver's IR is host-agnostic, so llc emits for the host as it is.
    llc = host.find_llc()
    if llc:
        subprocess.run([llc, "-filetype=obj", str(ir_path), "-o", str(obj)],
                       check=True, capture_output=True)
    else:
        host.compile_to_object(ir_text, str(obj))

    link = [cc] + flags
    if not target.is_windows() and not target.is_macos():
        link.append("-no-pie")
    subprocess.run(
        link + ["-o", str(exe), str(obj)] + rt_objs + ["-lm", "-lpthread"],
        check=True, capture_output=True,
    )
    return _run_under_asan(exe)


def main() -> int:
    ap = argparse.ArgumentParser(description="fray ASan/leak checker")
    ap.add_argument("--all", action="store_true",
                    help="check the golden suite as well as the benchmarks")
    ap.add_argument("--keep-going", action="store_true",
                    help="report every failure instead of stopping at the first")
    ap.add_argument("--driver", type=str, default=None,
                    help="compile the programs with this native compiler driver "
                         "(build/frayc_driver) instead of the Python codegen — "
                         "the same ASan check for the self-hosted codegen")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    def log(msg):
        if args.verbose:
            print(msg, flush=True)

    cc = target.find_c_compiler()
    if not cc:
        print("check_memory: no C compiler found — skipping")
        return 0

    with tempfile.TemporaryDirectory(prefix="fray-asan-") as tmp:
        probe = os.path.join(tmp, "probe.c")
        Path(probe).write_text("int main(void){return 0;}\n")
        flags = _asan_flags(cc, probe)
        if flags is None:
            print("check_memory: no AddressSanitizer support in "
                  f"{cc} — skipping")
            return 0

        driver = Path(args.driver) if args.driver else None
        if driver is not None and not driver.exists():
            print(f"check_memory: no compiler driver at {driver}")
            return 1

        programs = sorted(BENCH_DIR.glob("*.fray"))
        if args.all or driver is not None:
            programs += sorted(CASES_DIR.glob("*.fray"))
        if driver is not None:
            # The driver is the self-hosted compiler: it supports the golden
            # cases listed in tests/selfhosted_supported.txt (the list the
            # frontend gate maintains), not the benchmark programs.
            support = REPO_ROOT / "tests" / "selfhosted_supported.txt"
            listed = {line.strip() for line in support.read_text().splitlines()
                      if line.strip() and not line.startswith("#")}
            programs = sorted(p for p in CASES_DIR.glob("*.fray")
                              if p.stem in listed)
        if not programs:
            print("check_memory: no programs found")
            return 1

        print(f"check_memory: {len(programs)} programs under "
              f"AddressSanitizer ({cc})"
              + (f", compiled by {driver}" if driver else ""))
        rt_objs = _build_runtime_objects(cc, flags, Path(tmp), log)

        failures = []
        for path in programs:
            rel = path.relative_to(REPO_ROOT)
            try:
                if driver is not None:
                    clean, detail = _run_case_driver(path, Path(tmp), cc, flags,
                                                     rt_objs, log, driver)
                else:
                    clean, detail = _run_case(path, Path(tmp), cc, flags, rt_objs, log)
            except subprocess.CalledProcessError as e:
                clean, detail = False, f"build failed: {e.stderr.decode()[:200]}"
            except Exception as e:  # compile error: report, don't crash the run
                clean, detail = False, f"{type(e).__name__}: {e}"[:200]
            status = "clean" if clean else "LEAK/ERROR"
            print(f"  {str(rel):40s} {status}"
                  + (f"  [{detail}]" if detail and not clean else ""))
            if not clean:
                failures.append((rel, detail))
                if not args.keep_going:
                    break

        print()
        if failures:
            print(f"{len(failures)} of {len(programs)} programs are not clean:")
            for rel, detail in failures:
                print(f"  {rel}: {detail}")
            print("Run the same program under ASan by hand to see the traces "
                  "(see tools/check_memory.py for the exact flags).")
            return 1
        print(f"All {len(programs)} programs clean: no leaks, "
              "no memory errors.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
