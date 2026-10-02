#!/usr/bin/env python3
"""
fray runtime header checker.

runtime.h is the contract between the runtime library and everything that links
against it: the bootstrap emitter, the self-hosted compiler, and the runtime's
own test.c. It is only a contract if it is exactly true, so this gate checks it
in both directions.

  * Every non-inline `fray_*` function declared in the header must be defined in
    one of the library's translation units. A prototype with no definition
    compiles — nothing in the build objects refers to it — so the linker only
    complains once some caller finally does, and by then the symbol is a mystery
    rather than an obvious deletion. Thirteen such prototypes had accumulated
    before this gate existed (`fray_coro_wrap_async`, `fray_split`,
    `fray_timer_new`, ...).

  * Every `fray_*` function the library defines at file scope must be declared
    in the header. The `fray_` prefix marks the runtime's interface; a global
    named that way which the header never mentions is either API somebody
    forgot to publish or an implementation detail that leaked out of its
    translation unit. Either way the header has stopped describing the runtime,
    and the two functions this direction first found were of the second kind
    (`fray_weak_registry_lock_impl` and its unlock, mutex plumbing private to
    objects.c).

  * Every runtime entry point the two emitters call must be one the library
    defines — and, in the bootstrap, one its own RUNTIME_FUNCS declares. The
    first is a rename the runtime made and the emitter did not follow: the
    compiler is happy, and the *program* fails to link. The second is worse,
    a call site for a name that exists nowhere: `self._call("fray_call", ...)`
    sat in the emitter looking plausible while every program that reached it
    died inside the compiler with a KeyError.

File-local (`static`) functions are private by construction and are exempt from
the second direction; the header's own `static inline` helpers are definitions
rather than declarations and are exempt from the first.

Which sources those are comes from the library itself, not from the directory:
the build's own RUNTIME_SOURCES is read out of runtime/Makefile by the same code
tools/check_runtime_sources.py uses, so both runtime gates hold one notion of
what the library is. Globbed from runtime/*.c instead, a translation unit that
stopped being built would keep answering for prototypes nothing links — the
symbols would outlive the build. (Whether the Makefile's list and the
bootstrap's agree is that gate's invariant, not re-checked here.)

The check then asks the linker's question rather than pattern-matching C text:
each of those translation units is compiled to an object (at -O0, so `static`
definitions are not optimized away and an unused one still counts) and its
symbols are read with `nm`, local ones included. Textual matching could not
tell a definition from a call site, and mistaking a call site for a definition
would mask exactly the bug the first direction is for.

Usage:
    python tools/check_runtime_symbols.py [-v]

Skips with a warning (exit 0) when no C compiler or no `nm` is available, so
the gate runs where the toolchain is present and never blocks a build
elsewhere.
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
TOOLS_DIR = REPO_ROOT / "tools"
RUNTIME_DIR = REPO_ROOT / "runtime"
MAKEFILE = RUNTIME_DIR / "Makefile"
HEADER = RUNTIME_DIR / "runtime.h"
BOOTSTRAP_CODEGEN = REPO_ROOT / "bootstrap" / "codegen.py"
SELFHOST_DIR = REPO_ROOT / "compiler"
# The bootstrap reaches runtime entry points through these two helpers, so a
# literal argument to one of them is a call the emitter will write out.
CALL_HELPERS = frozenset({"_call", "_get_rt"})
sys.path.insert(0, str(REPO_ROOT / "bootstrap"))
sys.path.insert(0, str(TOOLS_DIR))

import target  # noqa: E402  (bootstrap/target.py)
# Reading the library's source list the same way in both runtime gates is what
# keeps them agreeing on what the library is.
from check_runtime_sources import makefile_sources  # noqa: E402

# A declaration line begins (after indentation) with a return type and then the
# function name. `static inline` helpers match this too; they are skipped by
# the end-of-line test in declared_functions().
DECL_RE = re.compile(r"^(?P<prefix>.*?)\b(?P<name>fray_[a-z0-9_]+)\s*\(")

# Symbol types nm reports for defined code: global text, local text (a `static`
# function), weak definition. Undefined references are 'U' and are ignored — a
# reference is not a definition.
DEFINED_TYPES = frozenset("TtW")
# The two of those that are visible outside their translation unit. The
# lowercase 't' is a `static` function, which is nobody else's business.
GLOBAL_TYPES = frozenset("TW")


def strip_comments(text: str) -> str:
    """Blank out /* ... */ and // ... comments, keeping line structure.

    Comments must go before parsing: the header documents which functions are
    called by generated code, and a name inside prose is not a declaration.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def declared_functions(header: Path):
    """External fray_* prototypes as {name: line}, plus an unparsed line.

    Returns (declarations, bad_line) where bad_line is (lineno, text) for a
    line that looks like a declaration but is neither a `;`-terminated
    prototype nor a definition containing `{`. Reporting that instead of
    skipping it keeps the gate from quietly ignoring a declaration it does not
    understand.
    """
    decls = {}
    for lineno, raw in enumerate(strip_comments(header.read_text()).splitlines(), 1):
        line = raw.rstrip()
        m = DECL_RE.match(line)
        if not m:
            continue
        if "static" in m.group("prefix").split():
            continue  # static inline helper: a definition in the header
        if line.endswith(";"):
            decls[m.group("name")] = lineno
        elif "{" not in line:
            return None, (lineno, line)
    return decls, None


def runtime_table(path: Path):
    """The runtime functions the bootstrap declares, from RUNTIME_FUNCS.

    Read out of the AST rather than imported: the values are llvmlite types,
    and a gate about C symbols should not need the code emitter installed to
    check itself. Returns None when the table is not a dict literal.
    """
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "RUNTIME_FUNCS"
                   for t in node.targets):
            continue
        return [k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return None


def literal_call_targets(path: Path) -> dict:
    """{literal name: line} for every self._call("name", ...) in an emitter.

    Only literals: a name built by an f-string or from a variable is not
    statically known here, and the table check below covers those (the table
    names every entry point the emitter may call).
    """
    targets = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in CALL_HELPERS:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            targets.setdefault(first.value, node.lineno)
    return targets


def strip_fray_comments(text: str) -> str:
    """Drop `#` comments from fray source, leaving quoted text alone.

    A name in a comment is prose; a name in a string literal is an instruction
    the self-hosted emitter writes into its IR.
    """
    out = []
    for line in text.splitlines():
        kept, quote = [], None
        for ch in line:
            if quote:
                kept.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
                kept.append(ch)
            elif ch == "#":
                break
            else:
                kept.append(ch)
        out.append("".join(kept))
    return "\n".join(out)


def selfhost_runtime_names(directory: Path) -> set:
    """Runtime entry points the self-hosted emitter writes into its IR text.

    That emitter builds IR as strings, so its call sites and its declarations
    both appear as string literals; reading the literals catches the two the
    same way. A name split across a concatenation would be missed, which is
    the price of having no structural hook there — the IR text is the hook.
    """
    names = set()
    for path in sorted(directory.glob("*.fray")):
        text = strip_fray_comments(path.read_text())
        for literal in re.findall(r'"([^"\n]*)"', text):
            names.update(re.findall(r"\bfray_[a-z0-9_]+\b", literal))
    return names


def check_emitters(symbols: dict, rel_bootstrap: str, rel_selfhost: str) -> int:
    """The runtime entry points the emitters call must exist in the library."""
    failures = 0

    if not BOOTSTRAP_CODEGEN.exists():
        print(f"check_runtime_symbols: no {rel_bootstrap} — skipping the "
              "bootstrap emitter")
    else:
        table = runtime_table(BOOTSTRAP_CODEGEN)
        if table is None:
            print(f"check_runtime_symbols: {rel_bootstrap} has no "
                  "RUNTIME_FUNCS dict literal, so the runtime entry points "
                  "the emitter declares are unknown.")
            print("  Teach this tool the new shape — a gate that checks "
                  "nothing is worse than no gate.")
            return 1
        undeclared = sorted(
            (name, line)
            for name, line in literal_call_targets(BOOTSTRAP_CODEGEN).items()
            if name not in table
        )
        if undeclared:
            failures += len(undeclared)
            print(f"{len(undeclared)} runtime call(s) in {rel_bootstrap} name a "
                  "function the emitter never declares:")
            for name, line in undeclared:
                print(f'  {rel_bootstrap}:{line}: self._call("{name}", ...)')
            print("The emitter has no declaration to call through, so this "
                  "cannot be emitted at all.\n")
        absent = sorted(name for name in table if name not in symbols)
        if absent:
            failures += len(absent)
            print(f"{len(absent)} runtime function(s) {rel_bootstrap} calls are "
                  "not defined in the library:")
            for name in absent:
                print(f"  {name}")
            print("The runtime moved and the emitter did not follow: the "
                  "compiler is happy and the program fails to link.\n")

    if not SELFHOST_DIR.exists():
        print(f"check_runtime_symbols: no {rel_selfhost}/ — skipping the "
              "self-hosted emitter")
    else:
        absent = sorted(name for name in selfhost_runtime_names(SELFHOST_DIR)
                        if name not in symbols)
        if absent:
            failures += len(absent)
            print(f"{len(absent)} runtime function(s) {rel_selfhost}/*.fray "
                  "calls are not defined in the library:")
            for name in absent:
                print(f"  {name}")
            print("Same failure as above, one engine over.\n")

    return failures


def read_symbols(nm: str, obj: Path) -> dict:
    """Defined code symbols in an object, as {name: nm type letter}.

    A Mach-O or MinGW symbol is reported under both spellings — its leading
    underscore is stripped as well — so the same code works on every host.
    """
    r = subprocess.run([nm, "-P", str(obj)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"nm failed on {obj.name}: {r.stderr.strip()[:200]}")
    symbols = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] not in DEFINED_TYPES:
            continue
        symbols.setdefault(parts[0], parts[1])
        symbols.setdefault(parts[0].lstrip("_"), parts[1])
    return symbols


def main() -> int:
    ap = argparse.ArgumentParser(
        description="check that runtime.h and the runtime C sources agree")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not HEADER.exists():
        print(f"check_runtime_symbols: no {HEADER.relative_to(REPO_ROOT)} — "
              "skipping")
        return 0

    decls, bad = declared_functions(HEADER)
    if decls is None:
        lineno, line = bad
        print(f"check_runtime_symbols: cannot parse "
              f"{HEADER.relative_to(REPO_ROOT)}:{lineno}: {line!r}")
        print("  Expected a prototype ending in ';' or a definition containing "
              "'{'. Teach this tool the shape rather than letting it skip a "
              "declaration it does not understand.")
        return 1

    if not MAKEFILE.exists():
        print(f"check_runtime_symbols: no {MAKEFILE.relative_to(REPO_ROOT)} — "
              "skipping")
        return 0
    names = makefile_sources(MAKEFILE)
    if names is None:
        print(f"check_runtime_symbols: {MAKEFILE.relative_to(REPO_ROOT)} does "
              "not declare RUNTIME_SOURCES in a shape this tool can read, so "
              "the library's translation units are unknown.")
        print("  Teach this tool the new shape — a gate that checks nothing is "
              "worse than no gate.")
        return 1
    sources = [RUNTIME_DIR / name for name in names]
    absent = [p for p in sources if not p.exists()]
    if absent:
        print("check_runtime_symbols: the library's source list names files "
              "that do not exist:")
        for path in absent:
            print(f"  {path.relative_to(REPO_ROOT)}")
        print("  Run tools/check_runtime_sources.py for the full picture.")
        return 1

    cc = target.find_c_compiler()
    if not cc:
        print("check_runtime_symbols: no C compiler found — skipping")
        return 0
    nm = shutil.which("nm")
    if not nm:
        print("check_runtime_symbols: no nm found — skipping")
        return 0

    symbols: dict = {}   # symbol -> nm type letter
    origin: dict = {}    # symbol -> the translation unit that defines it
    with tempfile.TemporaryDirectory(prefix="fray-rt-symbols-") as tmp:
        workdir = Path(tmp)
        for src in sources:
            obj = workdir / (src.stem + ".o")
            r = subprocess.run(
                [cc, "-c", "-std=gnu11", "-O0", "-pthread",
                 "-I", str(RUNTIME_DIR), "-o", str(obj), str(src)],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(f"check_runtime_symbols: cannot compile {src.name}:")
                print("  " + "\n  ".join(r.stderr.strip().splitlines()[:6]))
                return 1
            if args.verbose:
                print(f"  cc {src.name}")
            for name, kind in read_symbols(nm, obj).items():
                # Two objects defining one name is a duplicate symbol the
                # linker would reject; keep the externally visible spelling.
                if name not in symbols or (kind in GLOBAL_TYPES
                                           and symbols[name] not in GLOBAL_TYPES):
                    symbols[name] = kind
                    origin[name] = src.name

    rel_header = HEADER.relative_to(REPO_ROOT)
    rel_rt = RUNTIME_DIR.relative_to(REPO_ROOT)
    failures = 0

    missing = sorted(name for name in decls if name not in symbols)
    if missing:
        failures += len(missing)
        print(f"{len(missing)} fray_* function(s) declared in {rel_header} but "
              "defined in none of the "
              f"{len(sources)} translation units the library is built from:")
        for name in missing:
            print(f"  {rel_header}:{decls[name]}: {name}")
        print("Either define them or delete the prototypes: a declaration with "
              "no definition is a link error waiting for its first caller.")
        print("(If the definition exists but its file left the library's "
              "source list, that is the other runtime gate's finding — do not "
              "delete a declaration that is merely unbuilt.)\n")

    undeclared = sorted(
        name for name, kind in symbols.items()
        if kind in GLOBAL_TYPES and name.startswith("fray_") and name not in decls
    )
    if undeclared:
        failures += len(undeclared)
        print(f"{len(undeclared)} fray_* function(s) the library defines at "
              f"file scope but {rel_header} does not declare:")
        for name in undeclared:
            print(f"  {rel_rt}/{origin[name]}: {name}")
        print("Declare them in the header if they are part of the runtime's "
              "interface, or make them `static` if they are private to their "
              "translation unit — a global whose only callers are in the same "
              "file publishes a symbol nobody asked for.\n")

    # Third: what the emitters call must be what the library defines.
    failures += check_emitters(symbols, str(BOOTSTRAP_CODEGEN.relative_to(REPO_ROOT)),
                               str(SELFHOST_DIR.relative_to(REPO_ROOT)))

    if failures:
        return 1

    if args.verbose:
        print(f"  {len(decls)} declarations checked against {len(sources)} "
              "translation units")
    print(f"check_runtime_symbols: {rel_header} matches the library's "
          f"{len(sources)} translation units — {len(decls)} external fray_* "
          "declarations, all defined, every file-scope fray_* definition "
          "declared, and every runtime entry point the emitters call "
          "defined.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
