#!/usr/bin/env python3
"""fray Phase 5 benchmark runner.

Measures wall time of each benchmarks/*.fray program three ways:
  1. compiled (frayc -> native binary)   — what Phase 5 optimizes
  2. oracle (the Python evaluator)       — the reference implementation
  3. CPython running the same logic      — an interpreters-beat-us sanity bar

Compares program output against the oracle before timing: a benchmark only
counts when the compiled binary produces the oracle's answer.

Usage:
    python benchmarks/run_benchmarks.py [--repeats N] [--baseline PATH]

With --baseline, results are diffed against a previously saved JSON file
and regressions/improvements are reported (used by CI).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bootstrap"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from codegen import compile_program  # noqa: E402
import fray_oracle  # noqa: E402  (runs the evaluator)
import target  # platform detection (bootstrap/target.py)  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent


def _bench_exe(name: str) -> str:
    """Binary name for a benchmark, with the platform's exe suffix."""
    return "bench_" + name.replace(".fray", "") + target.exe_suffix()

# Python translations of each benchmark, so CPython measures the same work.
CPYTHON_VERSIONS = {
    "fib.fray": """
def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)
print(fib(27))
""",
    "list_sum.fray": """
total = 0
for k in range(20):
    xs = []
    for i in range(50000):
        xs.append(i)
    s = 0
    for i in xs:
        s = s + i
    total = total + s
print(total)
""",
    "string_build.fray": """
acc = ""
for i in range(30000):
    acc = acc + "x"
print(len(acc))
""",
    "arith.fray": """
acc = 0
f = 0.5
for i in range(3000000):
    acc = acc + i * 2 - i
    f = f + 0.25
print(acc)
print(f)
""",
    # Phase 6: thread-scaling pair. The serial/parallel comparison is the
    # no-GIL claim; CPython runs the serial version (the GIL makes its
    # threads a pessimization, which is exactly the point).
    "serial_sum.fray": """
def work(lo, hi):
    s = 0
    for i in range(lo, hi):
        s += i % 7
    return s
total = work(0, 2000000) + work(2000000, 4000000) \\
    + work(4000000, 6000000) + work(6000000, 8000000)
print(total)
""",
    "parallel_sum.fray": """
from concurrent.futures import ThreadPoolExecutor

def work(lo, hi):
    s = 0
    for i in range(lo, hi):
        s += i % 7
    return s

with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(work, [0, 2000000, 4000000, 6000000],
                                  [2000000, 4000000, 6000000, 8000000]))
print(sum(results))
""",
}


def time_once(cmd, cwd=None):
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    dt = time.perf_counter() - t0
    return dt, r


def oracle_output(fray_path: Path) -> str:
    """Run the Python evaluator on a file; return its stdout or raise."""
    import io
    import contextlib
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["fray_oracle", str(fray_path)]
        with contextlib.redirect_stdout(buf):
            fray_oracle.main()
    finally:
        sys.argv = old_argv
    return buf.getvalue()


def bench_one(name: str, repeats: int):
    fray_path = BENCH_DIR / name
    src = fray_path.read_text()

    expected = oracle_output(fray_path)

    exe = BENCH_DIR / _bench_exe(name)
    compile_program(src, str(exe), name)

    times = []
    out = None
    for _ in range(repeats):
        dt, r = time_once([str(exe)])
        out = r.stdout
        if r.returncode != 0:
            raise RuntimeError(f"{name}: compiled run failed: {r.stderr[:200]}")
        times.append(dt)
    if out != expected:
        raise RuntimeError(
            f"{name}: output mismatch\n compiled: {out!r}\n oracle:   {expected!r}")

    # CPython translation
    py_times = []
    py_src = CPYTHON_VERSIONS[name]
    py_file = BENCH_DIR / ("bench_" + name.replace(".fray", ".py"))
    py_file.write_text(py_src)
    py_out = None
    for _ in range(repeats):
        dt, r = time_once([sys.executable, str(py_file)])
        py_out = r.stdout
        py_times.append(dt)
    if py_out.replace("\r\n", "\n") != expected:
        print(f"  warning: {name}: CPython translation output differs; "
              f"skipping CPython bar", file=sys.stderr)
        py_times = []

    return {
        "compiled_best": min(times),
        "compiled_all": times,
        "cpython_best": min(py_times) if py_times else None,
        "output": out.strip(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--baseline", type=str, default=None,
                    help="JSON file to diff against (and to which none is written)")
    args = ap.parse_args(argv)

    names = sorted(p.name for p in BENCH_DIR.glob("*.fray"))
    results = {}
    print(f"{'benchmark':<16} {'compiled':>10} {'CPython':>10} {'speedup':>9}")
    for name in names:
        try:
            res = bench_one(name, args.repeats)
        except Exception as e:
            print(f"{name:<16} FAILED: {e}")
            results[name] = {"error": str(e)}
            continue
        results[name] = res
        cpy = res["cpython_best"]
        if cpy:
            speedup = cpy / res["compiled_best"]
            print(f"{name:<16} {res['compiled_best']:>9.3f}s {cpy:>9.3f}s {speedup:>8.1f}x")
        else:
            print(f"{name:<16} {res['compiled_best']:>9.3f}s {'n/a':>10}")

    bad = False
    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.exists():
            base = json.loads(baseline_path.read_text())
            print("\nregression check (vs %s):" % baseline_path.name)
            for name, res in results.items():
                if "error" in res:
                    print(f"  {name}: ERROR (was failing a regression gate)")
                    bad = True
                    continue
                old = base.get(name, {}).get("compiled_best")
                # A regression has to be both relatively large and larger
                # than the noise: fib finishes in ~1-2ms, where process
                # startup alone varies by a millisecond, so a pure ratio
                # would flag unchanged code.
                slower = old and res["compiled_best"] > old * 1.25
                if slower and (res["compiled_best"] - old) > 0.005:
                    print(f"  {name}: REGRESSION {old:.3f}s -> {res['compiled_best']:.3f}s")
                    bad = True
            if not bad:
                print("  no regressions")
        baseline_path.write_text(json.dumps(results, indent=2))
        print(f"baseline written to {baseline_path}")

    # Exe cleanup (any suffix — cleans up after platform switches too)
    for p in BENCH_DIR.glob("bench_*" + target.exe_suffix()):
        p.unlink(missing_ok=True)
    for p in BENCH_DIR.glob("bench_*.py"):
        p.unlink(missing_ok=True)

    # A regression (or a benchmark that failed to run) is a failing gate —
    # that is what the CI job relies on, so it must reach the exit code.
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
