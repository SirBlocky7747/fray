#!/usr/bin/env python3
"""
fray golden-test runner.

Finds all .fray files in tests/cases/ and checks them against their .expected
files. Each case runs through two engines when available:

  * the oracle (Python evaluator, tools/fray_oracle.py)
  * the compiled pipeline (bootstrap/codegen.compile_program, e2e)

Both must match the .expected file. A case that only passes one engine is a
failure: the compiled program may never drift from the oracle.

Usage:
    python tools/run_tests.py [--oracle PATH] [--compiled] [--verbose]

Test format:
    tests/cases/foo.fray       — fray source
    tests/cases/foo.expected   — expected stdout
    tests/cases/foo.exit       — expected exit status (optional, default 0)

The .exit file exists for cases that pin a *fatal* behavior — an exception no
clause handles ends the program — where the last thing the program says is on
stderr and the interesting part is the status. Both engines must agree on it,
the same as on stdout.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = REPO_ROOT / "tests" / "cases"
TOOLS_DIR = REPO_ROOT / "tools"
BOOTSTRAP_DIR = REPO_ROOT / "bootstrap"
sys.path.insert(0, str(BOOTSTRAP_DIR))

import target  # platform detection (bootstrap/target.py)


def _find_python_with_llvmlite():
    """Find a Python executable that has llvmlite installed.
    Falls back to sys.executable if none found."""
    # First check if current python already has it
    try:
        import llvmlite  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    # Try common Python locations
    import shutil
    import os
    candidates = []
    # Check conda installs
    home = Path.home()
    for conda_dir in [home / "miniconda3", home / "anaconda3"]:
        if os.name == "nt":
            candidates.append(conda_dir / "python.exe")
        else:
            candidates.append(conda_dir / "bin" / "python3")
    # Check PATH for python3
    which = shutil.which("python3")
    if which:
        candidates.append(Path(which))
    which = shutil.which("python3.11")
    if which:
        candidates.append(Path(which))
    which = shutil.which("python3.12")
    if which:
        candidates.append(Path(which))
    which = shutil.which("python3.13")
    if which:
        candidates.append(Path(which))
    for py in candidates:
        if py.exists():
            r = subprocess.run(
                [str(py), "-c", "import llvmlite"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                return str(py)
    return sys.executable


_PYTHON = _find_python_with_llvmlite()


def find_test_cases():
    """Find all .fray files that have a matching .expected file.

    A case is (source, expected stdout, expected exit status); the status is 0
    unless a .exit file says otherwise.
    """
    cases = []
    for fray_file in sorted(CASES_DIR.glob("*.fray")):
        expected_file = fray_file.with_suffix(".expected")
        if not expected_file.exists():
            continue
        exit_file = fray_file.with_suffix(".exit")
        status = 0
        if exit_file.exists():
            text = exit_file.read_text().strip()
            try:
                status = int(text)
            except ValueError:
                print(f"  FAIL  {fray_file.stem}: {exit_file.name} is not a "
                      f"number ({text!r})")
                status = None
        cases.append((fray_file, expected_file, status))
    return cases


def run_oracle(fray_path):
    """Run a .fray file through the Python evaluator."""
    r = subprocess.run(
        [_PYTHON, str(TOOLS_DIR / "fray_oracle.py"), str(fray_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return r.stdout, r.stderr, r.returncode


def run_compiled(fray_path, workdir: Path):
    """Compile a .fray file to a native binary and run it."""
    exe = workdir / (fray_path.stem + target.exe_suffix())
    r = subprocess.run(
        [_PYTHON, "-c",
         "import sys; sys.path.insert(0, r'%s')\n"
         "from codegen import compile_program\n"
         "compile_program(open(sys.argv[1], encoding='utf-8').read(), "
         "sys.argv[2], sys.argv[1])" % str(BOOTSTRAP_DIR),
         str(fray_path), str(exe)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        return "", r.stderr, r.returncode
    if not exe.exists():
        return "", f"binary not produced at {exe}", 1
    r2 = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120)
    return r2.stdout, r2.stderr, r2.returncode


def normalize(s: str) -> str:
    return s.replace("\r\n", "\n").strip()


def check(name, actual, expected):
    return normalize(actual) == normalize(expected)


def run_tests(verbose=False, with_compiled=True):
    cases = find_test_cases()
    if not cases:
        print("No test cases found in tests/cases/")
        return 0

    passed = failed = skipped = 0

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)

        for fray_path, expected_path, expected_rc in cases:
            name = fray_path.stem
            expected = expected_path.read_text()

            engines = {"oracle": None, "compiled": None}
            try:
                out, err, rc = run_oracle(fray_path)
                engines["oracle"] = (out, err, rc)
            except subprocess.TimeoutExpired:
                engines["oracle"] = ("", "timeout", 1)
            except Exception as e:
                engines["oracle"] = ("", str(e), 1)

            if with_compiled:
                try:
                    out, err, rc = run_compiled(fray_path, workdir)
                    engines["compiled"] = (out, err, rc)
                except subprocess.TimeoutExpired:
                    engines["compiled"] = ("", "timeout", 1)
                except Exception as e:
                    engines["compiled"] = ("", str(e), 1)

            case_ok = True
            for engine, res in engines.items():
                if res is None:
                    continue
                out, err, rc = res
                # A malformed .exit reads as None, which no status equals, so
                # the case fails instead of the problem being ignored.
                status_ok = rc == expected_rc
                if not status_ok or not check(name, out, expected):
                    case_ok = False
                    failed += 1
                    expected_note = ("" if expected_rc == 0
                                     else f", expected {expected_rc}")
                    print(f"  FAIL  {name} [{engine}] (exit code {rc}"
                          f"{expected_note})")
                    if verbose:
                        if err:
                            print(f"    stderr: {err[:300]}")
                        import difflib
                        diff = difflib.unified_diff(
                            normalize(expected).splitlines(keepends=True),
                            normalize(out).splitlines(keepends=True),
                            fromfile=f"{name}.expected",
                            tofile=f"{name}.{engine}.actual",
                        )
                        print("".join(diff))

            if case_ok:
                passed += 1
                if verbose:
                    ran = ", ".join(e for e, v in engines.items() if v)
                    print(f"  PASS  {name} ({ran})")

    print(f"\nResults: {passed} passed, {failed} failed, {skipped} skipped out of {len(cases)}")
    return 1 if failed > 0 else 0


def main():
    parser = argparse.ArgumentParser(description="fray test runner")
    parser.add_argument("--no-compiled", action="store_true",
                        help="only run the oracle engine")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show pass/fail details")
    args = parser.parse_args()

    sys.exit(run_tests(args.verbose, with_compiled=not args.no_compiled))


if __name__ == "__main__":
    main()
