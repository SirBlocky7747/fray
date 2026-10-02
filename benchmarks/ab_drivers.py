#!/usr/bin/env python
"""A/B two frayc drivers on the range-heavy benchmarks.

usage: ab_drivers.py DRIVER_A DRIVER_B [bench.fray ...]

Compiles each benchmark with both drivers, runs each binary three times and
reports the best wall time, so a change in the emitter can be measured
without going through the full benchmark harness.
"""
import statistics
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
RUNTIME = REPO / "runtime" / "libfrayrt.a"
DEFAULT_BENCHES = ["arith.fray", "list_sum.fray", "string_build.fray"]
RUNS = 3


def compile_with(driver: str, src: Path, out: Path) -> float:
    t0 = time.perf_counter()
    r = subprocess.run(
        [driver, str(src)], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise SystemExit(f"driver failed on {src}: {r.stdout[-400:]}")
    ir = r.stdout.split("===IR_START===\n", 1)[1].split("===IR_END===", 1)[0]
    ll = out.with_suffix(".ll")
    ll.write_text(ir)
    obj = out.with_suffix(".o")
    subprocess.run(["llc-14", "-filetype=obj", str(ll), "-o", str(obj)],
                   check=True)
    subprocess.run(["cc", "-o", str(out), str(obj), str(RUNTIME),
                    "-no-pie", "-lm", "-lpthread"], check=True)
    return time.perf_counter() - t0


def timeit(exe: Path) -> tuple:
    times = []
    out = ""
    for _ in range(RUNS):
        t0 = time.perf_counter()
        r = subprocess.run([str(exe)], capture_output=True, text=True,
                           timeout=600)
        times.append(time.perf_counter() - t0)
        out = r.stdout
    return min(times), out


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    a, b = sys.argv[1], sys.argv[2]
    names = sys.argv[3:] or DEFAULT_BENCHES
    print(f"{'benchmark':<18} {Path(a).name:>12} {Path(b).name:>12}"
          f" {'speedup':>9}  compile A/B")
    for name in names:
        src = BENCH / name
        exe_a = BENCH / ("ab_a_" + name.replace(".fray", ""))
        exe_b = BENCH / ("ab_b_" + name.replace(".fray", ""))
        ca = compile_with(a, src, exe_a)
        cb = compile_with(b, src, exe_b)
        ta, out_a = timeit(exe_a)
        tb, out_b = timeit(exe_b)
        same = "ok" if out_a == out_b else "OUTPUT DIFFERS"
        print(f"{name:<18} {ta:>12.4f} {tb:>12.4f} {ta / tb:>8.2f}x  "
              f"{ca:.2f}/{cb:.2f}s  {same}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
