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

Two detectors, for two different failure shapes:

  ASan (always)      every program, once. Catches the leaks and the memory
                     errors the runtime makes on one thread.
  valgrind --valgrind
                     every program that uses threads, coroutines, channels or
                     sockets, repeated. Catches the cross-thread lifetime bugs
                     ASan cannot see -- see the note beside CONCURRENCY_RE for
                     the measurement behind that, and for why memcheck and not
                     helgrind.

Usage:
    python tools/check_memory.py            # benchmarks (the heavy allocators)
    python tools/check_memory.py --all      # benchmarks + every golden case
    python tools/check_memory.py --all --valgrind
    python tools/check_memory.py --keep-going

Requires gcc/clang with libasan (Linux) or clang with ASan (macOS). Skips
cleanly, with a warning, when no sanitizer-capable compiler is present, and
skips the valgrind pass with a warning when valgrind is not installed.
"""

import argparse
import os
import re
import shutil
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

# The fixtures the io_* benchmarks read. benchmarks/run_io_benchmarks.py
# owns that setup; see _ensure_io_fixtures for why this gate needs its own.
IO_DATA_DIR = Path("/tmp/fray_io_bench")
IO_PAYLOAD_LINE = "fray io benchmark payload line\n"
IO_PAYLOAD_LINES = 200

import target  # noqa: E402  (bootstrap/target.py)

# ── Which programs the valgrind pass covers, and why it repeats them ──

# Why this pass exists at all. ASan cannot see a cross-thread lifetime bug in
# this runtime, and the cause is timing rather than configuration. On the
# runtime as it stood before the loop-lock fix, ASan reported the
# use-after-free 0 times in 240 runs spread over six ASAN_OPTIONS settings
# (quarantine from 1 MB to 4 GB, detect_leaks off, halt_on_error off, strict
# init order on) and 0 times in 40 runs at sixteen times the workload, while a
# deliberate free-then-read control in the same binary was reported at once.
# The window between a coroutine being queued for the loop and the store that
# follows it is a couple of instructions wide, so at native speed the posting
# thread always finishes first. memcheck's own scheduling does interleave it.
#
# memcheck and not helgrind: helgrind reports the race directly rather than the
# symptom, which is what one would want, but it does not understand
# swapcontext fibers -- on the fixed runtime it draws 310 errors from 4
# contexts and 162959 suppressions for benchmarks/io_socket_coro.fray alone.
# Making it usable would mean suppressing the very instrument meant to catch
# this, so it is not the detector here.
#
# A program whose source reaches for threads, coroutines, channels or sockets.
# The rule is the source text rather than a hand-kept list, so a new
# concurrent program is covered the day it is written.
CONCURRENCY_RE = re.compile(
    r"\basync\s+def\b|\bchannel\(|\bspawn\b|\bthread\b|\btcpListen\b"
    r"|\brunUntilComplete\b")

# memcheck reports the cross-thread bug only when its scheduling interleaves
# the two threads inside that window, which happened on 11 of 20 runs of
# benchmarks/io_socket_coro.fray and 2 of 20 of io_file_coro.fray against the
# pre-fix runtime. Ten repeats put the odds of missing it below one in ten
# thousand for the first, and cost about a minute.
VALGRIND_REPEATS = 10

# An invalid access, a use of an uninitialised value, or a definitely-lost
# block fails. "Still reachable" and "possibly lost" do not: the first is the
# runtime's own pools and singletons, and the second is how a coroutine's
# malloc'd fiber stack looks to memcheck, which is not told the block is a
# stack. Neither is a defect and neither is silenced to make a count green --
# they simply are not what this pass is looking for, and the leak check that
# is looking for unreachable memory is the ASan pass above.
VG_INVALID_RE = re.compile(
    r"^==\d+== (Invalid read|Invalid write|Invalid free|Invalid delete)")
VG_UNINIT_RE = re.compile(
    r"^==\d+== (Conditional jump or move depends on uninitialised|"
    r"Use of uninitialised value|Syscall param)")
VG_LOST_RE = re.compile(r"definitely lost:\s*([\d,]+) bytes")


def _int_constant(text: str, name: str, default: int) -> int:
    """Read an integer constant out of fray source (e.g. NFILES = 64)."""
    m = re.search(rf"^{name}\s*=\s*(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else default


def _expected_exit(source_path: Path) -> int:
    """The status a golden case is supposed to end with.

    tests/cases/<name>.exit records the cases that are *meant* to fail — an
    uncaught exception is the successful outcome of the error path, not a
    broken program. Everything else is expected to exit 0. This mirrors the
    convention tools/check_cases.sh already follows.
    """
    marker = source_path.with_suffix(".exit")
    if not marker.exists():
        return 0
    try:
        return int(marker.read_text().strip())
    except ValueError:
        return 0


def _ensure_io_fixtures(programs: list) -> None:
    """Create the files the io_file_* benchmarks read.

    benchmarks/run_io_benchmarks.py owns those programs and lays down
    /tmp/fray_io_bench before running them; the memory gate compiles them
    straight from the tree, where that directory does not exist. Every
    io_file_* program therefore died on `ValueError: no such file` — and a
    program that dies early produces no ASan output, so the gate scored it
    "clean" and the failure never surfaced. Building the fixtures is what
    turns three dead entries in the check into three real ones, and keeps
    io_socket_coro (which finds a real leak) inside the gate.
    """
    sources = [p for p in programs if p.stem.startswith("io_file")]
    if not sources:
        return
    IO_DATA_DIR.mkdir(exist_ok=True)
    body = IO_PAYLOAD_LINE * IO_PAYLOAD_LINES
    for path in sources:
        nfiles = _int_constant(path.read_text(), "NFILES", 64)
        for i in range(nfiles):
            (IO_DATA_DIR / f"f{i}.txt").write_text(body)


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


def _build_exe(source_path: Path, workdir: Path, cc: str, flags: list,
               rt_objs: list, tag: str) -> Path:
    """Compile and link one program with `flags`; return the binary.

    Shared by the two passes, which cannot share a build: memcheck needs an
    uninstrumented binary, so the same source is compiled twice with different
    flags rather than the passes reusing each other's objects.
    """
    sys.path.insert(0, str(BOOTSTRAP_DIR))
    import codegen

    name = source_path.stem
    obj = workdir / f"{name}.{tag}.o"
    exe = workdir / f"{name}.{tag}.bin"

    codegen.compile_to_object(source_path.read_text(), str(obj), str(source_path))
    # Same link model as the compiler: the emitted objects use absolute
    # relocations, so a PIE link is not an option on Linux.
    link = [cc] + flags
    if not target.is_windows() and not target.is_macos():
        link.append("-no-pie")
    subprocess.run(
        link + ["-o", str(exe), str(obj)] + rt_objs + ["-lm", "-lpthread"],
        check=True, capture_output=True,
    )
    return exe


def _run_case(source_path: Path, workdir: Path, cc: str, flags: list,
              rt_objs: list, log) -> tuple[bool, str, int]:
    """Compile and run one program; return (clean, detail)."""
    return _run_under_asan(_build_exe(source_path, workdir, cc, flags, rt_objs,
                                     "asan"))


def _run_under_asan(exe: Path) -> tuple[bool, str, int]:
    """Run one instrumented binary; return (clean, detail, exit status).

    The exit status comes back separately because a program can fail without
    saying anything to stderr — a segfault, or an uncaught exception — and a
    gate that reads only ASan's output scores those runs "clean".
    """
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1")
    try:
        proc = subprocess.run([str(exe)], capture_output=True, text=True,
                              env=env, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "timed out after 600s", -1

    # ASan reports both memory errors and leaks on stderr; either fails.
    err = proc.stderr
    for line in err.splitlines():
        if "Sanitizer:" in line and "SUMMARY" in line:
            return False, line.split("Sanitizer: ", 1)[-1], proc.returncode
    if "Sanitizer" in err:
        first = next((l for l in err.splitlines() if "Sanitizer" in l), "")
        return False, first.strip(), proc.returncode
    detail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return True, detail, proc.returncode


def _valgrind_flags() -> list:
    """Flags for the uninstrumented build memcheck runs against.

    Deliberately the same optimisation and frame-pointer settings the ASan
    build uses, so the two passes differ in the detector and not in the code.
    """
    return ["-g", "-O1", "-fno-omit-frame-pointer"]


def _run_under_valgrind(exe: Path, repeats: int) -> tuple[bool, str]:
    """memcheck one program `repeats` times; return (clean, detail).

    Every repeat is checked and a single bad one fails, so repeating raises
    the odds of catching an intermittent error without relaxing what counts as
    one. --error-exitcode catches anything memcheck counts as an error, and the
    stderr scan is there as well so the report that produced the failure can be
    named rather than reduced to a number.
    """
    for attempt in range(repeats):
        try:
            proc = subprocess.run(
                ["valgrind", "--error-exitcode=42", "--leak-check=full",
                 "--show-leak-kinds=definite", "--num-callers=20", str(exe)],
                capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            return False, f"timed out on repeat {attempt + 1}/{repeats}"
        err = proc.stderr
        detail = ""
        for line in err.splitlines():
            if VG_INVALID_RE.match(line):
                detail = line.split("== ", 1)[-1].strip()
                break
            if VG_UNINIT_RE.match(line):
                detail = line.split("== ", 1)[-1].strip()
                break
        if not detail:
            lost = VG_LOST_RE.search(err)
            if lost and lost.group(1) not in ("0", "0,0"):
                detail = f"{lost.group(1)} bytes definitely lost"
        if detail:
            return False, f"repeat {attempt + 1}/{repeats}: {detail}"
        if proc.returncode == 42:
            return False, (f"repeat {attempt + 1}/{repeats}: memcheck reported "
                           "an error with no recognised line")
    return True, f"clean in {repeats} run(s)"


def _valgrind_pass(programs: list, workdir: Path, cc: str, driver: Path,
                   repeats: int, log) -> list:
    """memcheck every program that uses threads, coroutines or sockets.

    Additive: the ASan pass has already covered all of `programs` and still
    does. This adds the failure mode ASan cannot see, and nothing here changes
    what the ASan pass accepts.
    """
    targets = [p for p in programs
               if CONCURRENCY_RE.search(p.read_text())]
    if not targets:
        print("check_memory: no concurrent programs to memcheck")
        return []
    if shutil.which("valgrind") is None:
        print("check_memory: valgrind not installed — "
              "skipping the cross-thread memory check")
        return []

    vg_dir = workdir / "vg"
    vg_dir.mkdir(exist_ok=True)
    rt_objs = _build_runtime_objects(cc, _valgrind_flags(), vg_dir, log)
    print(f"check_memory: memcheck under valgrind, {len(targets)} concurrent "
          f"program(s) x {repeats} run(s)")

    failures = []
    for path in targets:
        rel = path.relative_to(REPO_ROOT)
        try:
            if driver is not None:
                exe = _build_exe_driver(path, vg_dir, cc, _valgrind_flags(),
                                        rt_objs, driver, "vg")
            else:
                exe = _build_exe(path, vg_dir, cc, _valgrind_flags(), rt_objs,
                                 "vg")
            clean, detail = _run_under_valgrind(exe, repeats)
        except subprocess.CalledProcessError as e:
            clean, detail = False, f"build failed: {e.stderr.decode()[:200]}"
        except Exception as e:  # compile error: report, don't crash the run
            clean, detail = False, f"{type(e).__name__}: {e}"[:200]
        status = "clean" if clean else "LEAK/ERROR"
        print(f"  {str(rel):40s} {status}  [{detail}]")
        if not clean:
            failures.append((rel, detail))
    return failures


def _build_exe_driver(source_path: Path, workdir: Path, cc: str, flags: list,
                      rt_objs: list, driver: Path, tag: str) -> Path:
    """Compile one program with the native compiler driver; return the binary.

    This is the build the memory gate uses for the self-hosted codegen: the
    bootstrap path checks the Python emitter, and the ownership rules the two
    share (a callee owns its parameters, argument lists own what they carry,
    and a caller owns what a call returns) are exactly what drifts between
    them. The driver's IR is emitted to an object by llc, or by llvmlite where
    llc is not installed.
    """
    sys.path.insert(0, str(TOOLS_DIR))
    import frayc_selfhosted as host

    name = source_path.stem
    obj = workdir / f"{name}.{tag}driver.o"
    exe = workdir / f"{name}.{tag}driver.bin"
    result = subprocess.run([str(driver), str(source_path)],
                            capture_output=True, text=True, timeout=600)
    out = result.stdout
    if result.returncode != 0 or "===IR_START===" not in out:
        diags = [l for l in out.splitlines() if DIAG_RE.match(l)]
        raise RuntimeError(diags[0][:160] if diags
                           else (result.stderr.strip().splitlines() or
                                 [f"driver exit {result.returncode}"])[-1][:160])
    ir_text = out.split("===IR_START===", 1)[1].split("===IR_END===", 1)[0]
    ir_path = workdir / f"{name}.{tag}driver.ll"
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
    return exe


def _run_case_driver(source_path: Path, workdir: Path, cc: str, flags: list,
                     rt_objs: list, log, driver: Path) -> tuple[bool, str, int]:
    """Same check, but the object is compiled by the native compiler driver."""
    return _run_under_asan(
        _build_exe_driver(source_path, workdir, cc, flags, rt_objs, driver,
                          "asan"))


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
    ap.add_argument("--valgrind", action="store_true",
                    help="additionally memcheck every program that uses "
                         "threads, coroutines, channels or sockets, repeated "
                         f"{VALGRIND_REPEATS} times — the cross-thread memory "
                         "errors ASan cannot see (see CONCURRENCY_RE)")
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
        _ensure_io_fixtures(programs)

        failures = []
        for path in programs:
            rel = path.relative_to(REPO_ROOT)
            try:
                if driver is not None:
                    clean, detail, rc = _run_case_driver(
                        path, Path(tmp), cc, flags, rt_objs, log, driver)
                else:
                    clean, detail, rc = _run_case(path, Path(tmp), cc, flags,
                                                  rt_objs, log)
            except subprocess.CalledProcessError as e:
                clean, detail, rc = (False,
                                     f"build failed: {e.stderr.decode()[:200]}",
                                     -1)
            except Exception as e:  # compile error: report, don't crash the run
                clean, detail, rc = False, f"{type(e).__name__}: {e}"[:200], -1
            if clean:
                want = _expected_exit(path)
                if rc != want:
                    clean = False
                    detail = f"exit {rc}, expected {want}"
            status = "clean" if clean else "LEAK/ERROR"
            print(f"  {str(rel):40s} {status}"
                  + (f"  [{detail}]" if detail and not clean else ""))
            if not clean:
                failures.append((rel, detail))
                if not args.keep_going:
                    break

        print()
        if args.valgrind:
            print("##### valgrind: cross-thread memory errors #####")
            failures += _valgrind_pass(programs, Path(tmp), cc, driver,
                                       VALGRIND_REPEATS, log)
            print()

        if failures:
            print(f"{len(failures)} program(s) are not clean:")
            for rel, detail in failures:
                print(f"  {rel}: {detail}")
            print("Run the same program under ASan by hand to see the traces "
                  "(see tools/check_memory.py for the exact flags), or under "
                  "valgrind for the cross-thread check "
                  "(--valgrind, flags in _run_under_valgrind).")
            return 1
        print(f"All {len(programs)} programs clean: no leaks, "
              "no memory errors.")
        if args.valgrind:
            print("Cross-thread pass clean under valgrind memcheck.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
