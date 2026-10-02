#!/usr/bin/env python3
"""
fray runtime source-list checker.

libfrayrt.a is built from runtime/*.c twice over, and each build keeps its own
list: runtime/Makefile compiles the standalone library and the C test suite,
and bootstrap/codegen.py::RUNTIME_SOURCES compiles the same sources into every
generated binary and the JIT helper library. Two lists that describe one
library drift the moment somebody adds a translation unit to one of them — the
new file lands in some builds and not others, and the failure shows up as an
undefined symbol on whichever path was forgotten, well after the file was
added.

So: every runtime/*.c that is not a program must appear in both lists, and
every entry in either list must name a file that exists. A `.c` with a `main`
is a program rather than a library unit (test.c is the C suite, coro_drv.c is
the coroutine driver) and must not appear in either list — a program archived
into the library would define `main` in every binary that links it.

Programs are identified by compiling the file and looking for a defined `main`
symbol with `nm`, the same "ask the linker" approach as
tools/check_runtime_symbols.py: a `main` behind a preprocessor guard is still
seen, and a comment saying `main` is not a program.

Usage:
    python tools/check_runtime_sources.py [-v]

Skips with a warning (exit 0) when no C compiler or no `nm` is available.
"""

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / "runtime"
MAKEFILE = RUNTIME_DIR / "Makefile"
CODEGEN = REPO_ROOT / "bootstrap" / "codegen.py"
sys.path.insert(0, str(REPO_ROOT / "bootstrap"))

import target  # noqa: E402  (bootstrap/target.py)

MAKE_VAR_RE = re.compile(r"^RUNTIME_SOURCES\s*[:?]?=")
SOURCE_RE = re.compile(r"[A-Za-z0-9_.-]+\.c")
# `main` is a global text symbol; a local one would be a `static` function of
# that name, which is not a program entry point.
MAIN_KIND = "T"


def makefile_sources(path: Path):
    """Entries of the Makefile's RUNTIME_SOURCES, continuations joined.

    Returns None when the variable is not there in a shape this understands;
    the caller reports that rather than checking an empty list and passing.
    """
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if not MAKE_VAR_RE.match(line.strip()):
            continue
        text = line.split("=", 1)[1]
        while text.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            text = text.rstrip()[:-1] + " " + lines[i]
        return SOURCE_RE.findall(text)
    return None


def codegen_sources(path: Path):
    """Entries of codegen.RUNTIME_SOURCES, read without importing llvmlite.

    The list is a module-level literal, so the AST answers this without
    pulling in the code emitter (and without needing it installed).
    """
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "RUNTIME_SOURCES" not in targets:
            continue
        try:
            values = ast.literal_eval(node.value)
        except ValueError:
            return None
        return [str(v) for v in values]
    return None


def defines_main(cc: str, nm: str, src: Path, workdir: Path) -> bool:
    """Whether a runtime source is a program rather than a library unit."""
    obj = workdir / (src.stem + ".o")
    r = subprocess.run(
        [cc, "-c", "-std=gnu11", "-O0", "-pthread",
         "-I", str(RUNTIME_DIR), "-o", str(obj), str(src)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("cannot compile " + src.name + ": "
                           + r.stderr.strip().splitlines()[0][:200])
    listed = subprocess.run([nm, "-P", str(obj)], capture_output=True, text=True)
    for line in listed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lstrip("_") == "main" \
                and parts[1] == MAIN_KIND:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="check that both runtime source lists describe one library")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    for path in (MAKEFILE, CODEGEN):
        if not path.exists():
            print(f"check_runtime_sources: no {path.relative_to(REPO_ROOT)} — "
                  "skipping")
            return 0

    on_disk = sorted(p.name for p in RUNTIME_DIR.glob("*.c"))
    if not on_disk:
        print("check_runtime_sources: no runtime/*.c sources found — skipping")
        return 0

    lists = {}
    for label, entries in (
        ("runtime/Makefile RUNTIME_SOURCES", makefile_sources(MAKEFILE)),
        ("bootstrap/codegen.py RUNTIME_SOURCES", codegen_sources(CODEGEN)),
    ):
        if entries is None:
            print(f"check_runtime_sources: {label} is not a plain list this "
                  "tool can read (missing, or built some other way).")
            print("  Teach this tool the new shape rather than checking an "
                  "empty list and passing.")
            return 1
        lists[label] = entries

    cc = target.find_c_compiler()
    if not cc:
        print("check_runtime_sources: no C compiler found — skipping")
        return 0
    nm = shutil.which("nm")
    if not nm:
        print("check_runtime_sources: no nm found — skipping")
        return 0

    programs = set()
    try:
        with tempfile.TemporaryDirectory(prefix="fray-rt-sources-") as tmp:
            workdir = Path(tmp)
            for name in on_disk:
                if defines_main(cc, nm, RUNTIME_DIR / name, workdir):
                    programs.add(name)
                    if args.verbose:
                        print(f"  program  {name}")
                elif args.verbose:
                    print(f"  library  {name}")
    except RuntimeError as e:
        print(f"check_runtime_sources: {e}")
        return 1

    library = [name for name in on_disk if name not in programs]

    failures = 0
    for label, entries in lists.items():
        missing = [name for name in library if name not in entries]
        absent = [name for name in entries if name not in on_disk]
        programs_listed = [name for name in entries if name in programs]

        if missing:
            failures += len(missing)
            print(f"{len(missing)} runtime translation unit(s) are not in "
                  f"{label}:")
            for name in missing:
                print(f"  runtime/{name}")
        if absent:
            failures += len(absent)
            print(f"{len(absent)} entry/entries of {label} name a file that "
                  "does not exist:")
            for name in absent:
                print(f"  runtime/{name}")
        if programs_listed:
            failures += len(programs_listed)
            print(f"{len(programs_listed)} entry/entries of {label} are "
                  "programs, not library units (they define main):")
            for name in programs_listed:
                print(f"  runtime/{name}")

    if failures:
        print("\nBoth lists build the same library: add a new translation unit "
              "to both, or to neither, and keep the programs that have a "
              "`main` out of both.")
        return 1

    if args.verbose:
        for label, entries in lists.items():
            print(f"  {label}: {len(entries)} entries")
    print(f"check_runtime_sources: {len(library)} library translation unit(s) "
          f"({len(programs)} program(s) excluded: "
          f"{', '.join(sorted(programs)) or 'none'}) — both source lists "
          "agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
