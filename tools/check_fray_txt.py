#!/usr/bin/env python3
"""Syntax-reference audit — compile and run every snippet the reference documents.

The release criterion is written against the syntax reference ("every example
works") and for a long time nothing compiled it. This tool does. It reads the
reference, takes every fenced ```fray snippet it documents, compiles it with
the native chain, runs it, and diffs the result against the bootstrap oracle.
The old flat reference (`fray.txt`, a `#heading` and the lines under it) is
still parsed, one statement per snippet, for a checkout that carries one.

A snippet that cannot simply work needs an explicit record here, and a record
is *checked*, never trusted:

  recorded   the snippet documents something the language does not do — an
             infinite loop, a method call on the wrong type, a line whose
             result the oracle and the driver disagree about. The record
             stores what the audit observed (does it compile, what status does
             running it give, what does it print), so the observation is made
             again on every run and the record cannot outlive its reason.
  missing    nothing implements the construct. The audit asserts the driver
             still rejects the snippet, naming its stage.
  value_bug  the reference annotates a value the language does not produce.
             The record stores both values and checks the language still
             prints the observed one.
  implemented the reference marks the construct as not-yet-final (PROPOSED)
             while the compiler implements it. The record says so and the
             snippet is compiled, run and diffed like any other — the audit
             reports the contradiction instead of picking a side.

The reference's own markers carry a rule, and the rule is verified rather than
believed: a snippet under a `LOCKED` heading must compile, run and match the
oracle; a snippet under a `PROPOSED` heading must still be *rejected*, and one
that starts compiling is reported so it can be promoted. `NOT_YET` holds the
headings whose whole feature is still unbuilt (the tensor core, classes, the
planned `break`): their snippets must keep being rejected, which is what stops
a document full of future syntax from looking like a passing suite.

Imports resolve the way every engine resolves them: the program's own
directory first, then the compiler's, then the standard library — `stdlib/`
beside the compiler (`build/` in a checkout, `bin/` in a release package), so
`import random` in a snippet means stdlib/random.fray here too.

Records are keyed by `(heading, n)` — the nth snippet under that heading — so
editing the reference either matches the record or leaves it unmatched, and the
audit fails and asks for the section to be re-read. Everything not recorded is
compiled, run and compared to the oracle on every run.

Usage:
    python tools/check_fray_txt.py                 # audit build/frayc_driver
    python tools/check_fray_txt.py --doc PATH      # a different reference
    python tools/check_fray_txt.py --list          # the parsed structure only
    python tools/check_fray_txt.py -v              # one line per snippet
"""

import argparse
import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
CASES_DIR = REPO_ROOT / "tests" / "cases"
SUPPORT_LIST = REPO_ROOT / "tests" / "selfhosted_supported.txt"
DEFAULT_DRIVER = None  # resolved by default_driver() at parse time
CHAIN = TOOLS_DIR / "frayc.sh"
ORACLE = TOOLS_DIR / "fray_oracle.py"
COMPILER_DIR = REPO_ROOT / "compiler"
STDLIB_DIR = REPO_ROOT / "stdlib"
# The reference, in the order the checkout is expected to carry it.
REFERENCE_NAMES = ("fray-layout.md", "fray.txt")


def default_driver():
    """The driver to audit against when none is named.

    A source checkout keeps it in `build/`; a release package ships it as
    `bin/frayc_driver`, which is why the fallback is here and not in the
    caller's argument: the package carries this tool so a user can re-run the
    audit, and an audit that only works in a checkout is not carried.
    """
    for candidate in (REPO_ROOT / "build" / "frayc_driver",
                      REPO_ROOT / "bin" / "frayc_driver"):
        if candidate.exists():
            return candidate
    return REPO_ROOT / "build" / "frayc_driver"

sys.path.insert(0, str(TOOLS_DIR))
PYTHON = sys.executable
# A rejection must name its stage, never crash the compiler.
DIAG_RE = re.compile(r"^(LEX|PARSE|SEMA|CODEGEN|LINK) ERROR: ")
# Two representations the oracle and the driver cannot be expected to agree on
# character for character: a missing value (the oracle's sentinel object) and a
# function used as a value (each prints its own object's address).
NONE_REPR_RE = re.compile(r"<object object at 0x[0-9a-fA-F]+>")
FUNC_REPR_RE = re.compile(r"<function [^>]*>")
IDENT_RE = re.compile(r"[A-Za-z_]\w*")
# In the flat form: `const x=1` defines x; `x[0]=2` and `x+=1` read it.
ASSIGN_RE = re.compile(r"^(?:const\s+)?([A-Za-z_]\w*)\s*=(?!=)")
DEF_RE = re.compile(r"^def\s+([A-Za-z_]\w*)")
LITERAL_RE = re.compile(r"#\s*(\S+?)\s*$")
LITERAL_VALUES = re.compile(r"(True|False|-?\d+|-?\d+\.\d+)$")
BLOCK_CONT = re.compile(r"^(else|elif|except|finally)\b")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
STATUS_RE = re.compile(r"\b(LOCKED|PROPOSED)\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# the reference, as snippets
# --------------------------------------------------------------------------

class Snippet:
    """One unit of documented code, and what the audit does with it."""

    def __init__(self, heading, status, lineno, text):
        self.heading = heading      # the heading it sits under
        self.status = status        # LOCKED / PROPOSED / None, from the heading
        self.lineno = lineno
        self.text = text            # the snippet, verbatim
        self.prelude = ""           # names the snippet assumes
        self.reads = ()
        self.rule = None            # locked | proposed | not_yet, from the tool
        self.reason = None
        self.kind = "run"           # run | missing | recorded | value_bug | implemented
        self.compiles = True        # recorded: false when the driver rejects it
        self.exit_code = 0          # recorded: the status running it gives
        self.runs = True            # recorded: false for "cannot terminate"
        self.output = None          # recorded: the stdout it must still print
        self.documented = None      # value_bug: what the reference claims
        self.observed = None        # value_bug: what the language produces
        self.probe = None           # recorded: (source, stdout) to re-check
        self.stdin = ""
        self.files = {}             # companion sources: a section that spans
                                    # more than one file (a tutorial's "here is
                                    # the module, here is the program")

    @property
    def label(self):
        first = [l for l in self.text.split("\n") if l.strip() and
                 not l.strip().startswith("#")] or [""]
        return " ".join(first[0].split())[:46]

    @property
    def source(self):
        return (self.prelude + "\n" if self.prelude else "") + self.text + "\n"

    @property
    def value_source(self):
        """The snippet as a program that prints the value it documents — the
        right-hand side of an assignment, or the expression itself."""
        first = self.text.split("\n")[0]
        match = ASSIGN_RE.match(first)
        expression = first.split("=", 1)[1] if match else first
        return (self.prelude + "\n" if self.prelude else "") + \
            "print(" + LITERAL_RE.sub("", expression).strip() + ")\n"


def parse_markdown(text):
    """Snippets of `fray-layout.md`: the fenced ```fray blocks, by heading.

    A block is a demonstration, not a program: the one after `x = [1, 2, 3]`
    is `x[0]`, and it is the section's own sequence that makes it runnable. So
    every snippet is compiled with the definitions the snippets before it
    under the same heading supplied (see `carry_forward`) — the documented
    order is the context, and the audit supplies nothing the reference does
    not.
    """
    snippets = []
    heading, status = "(preamble)", None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = HEADING_RE.match(line)
        if m:
            title = m.group(2).strip()
            heading = title
            found = STATUS_RE.search(title.split("—")[-1])
            status = found.group(1).upper() if found else None
            i += 1
            continue
        if line.startswith("```"):
            language = line[3:].strip()
            body, j = [], i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                body.append(lines[j])
                j += 1
            if language == "fray" and "".join(body).strip():
                snippets.append(Snippet(heading, status, i + 2,
                                        "\n".join(body).rstrip("\n")))
            i = j + 1
            continue
        i += 1
    return carry_forward(snippets)


def parse_flat(text):
    """Snippets of the old flat reference: one statement per snippet, with the
    definitions it reads carried in front of it (`def f(): ...` then
    `print(f)` is two snippets, and the second cannot stand alone)."""
    snippets = []
    current, lines = None, text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.startswith("#") and len(raw) > 1 and raw[1].isalpha():
            current = raw.strip()
            i += 1
            continue
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        block = [raw.rstrip()]
        lineno = i + 1
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                break
            if not nxt[0].isspace() and not BLOCK_CONT.match(nxt):
                break
            block.append(nxt.rstrip())
            i += 1
        snippets.append(Snippet(current or "(preamble)", None, lineno,
                                "\n".join(block)))
    return carry_forward(snippets)


def statements(text):
    """The top-level statements of a block: a line at column 0, plus the lines
    indented under it, with comment-only and blank lines dropped."""
    out, current = [], None
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() and current is not None:
            current.append(line)
            continue
        if current:
            out.append("\n".join(current))
        current = [line]
    if current:
        out.append("\n".join(current))
    return out


def defined_names(text):
    """{name: the statement that defines it} for one block — its assignments
    and its `def`s, each with the lines that belong to it."""
    out = {}
    for statement in statements(text):
        match = ASSIGN_RE.match(statement) or DEF_RE.match(statement)
        if match:
            out[match.group(1)] = statement
    return out


def carry_forward(snippets):
    """Give every snippet the definitions of the names it reads.

    A documented block is often not a program on its own — the one after
    `x = [1, 2, 3]` is `x[0]` — and the section's own sequence is the context,
    one statement per name rather than a whole block (a documented block's
    prints are its own business). A snippet that only defines a name reads
    nothing, so `x = 1` gets no prelude and the `x += 1` after it gets `x = 1`.
    Headings that assume a name their own sequence never defines say so in
    `HEADINGS`, prelude and all, rather than inheriting it from a section whose
    `x` may be a list, a number or a function.
    """
    for heading in dict.fromkeys(s.heading for s in snippets):
        carried = {}
        for snippet in snippets:
            if snippet.heading != heading:
                continue
            own = defined_names(snippet.text)
            names = set(IDENT_RE.findall(snippet.text)) - set(own)
            snippet.reads = tuple(sorted(names))
            snippet.prelude = "\n".join(carried[name] for name in sorted(names)
                                        if name in carried)
            carried.update(own)
    return snippets


def document_headings(text):
    """Every heading the reference has, whether or not it documents a snippet:
    the prose-only sections are as much a part of the file's structure."""
    return {m.group(2).strip() for m in
            (HEADING_RE.match(line) for line in text.split("\n")) if m}


def parse_document(path):
    """The reference's snippets and headings, whichever form it is in."""
    text = path.read_text()
    if "```fray" in text:
        return parse_markdown(text), document_headings(text)
    return parse_flat(text), {s.heading for s in parse_flat(text)}


# --------------------------------------------------------------------------
# the records
# --------------------------------------------------------------------------

def run(**kw):
    """A snippet that is compiled, run and diffed: used to give one snippet a
    prelude or stdin of its own without touching its heading."""
    return dict(kind="run", **kw)


def recorded(reason, compiles=True, exit_code=1, runs=True, output=None,
             probe=None):
    """A snippet the language does not do as documented. `probe` is an extra
    (source, expected stdout) pair for a claim the snippet itself cannot show —
    a block of bare strings prints nothing, so pinning that `\n` is a literal
    backslash and an n needs a program that prints it."""
    return dict(kind="recorded", reason=reason, compiles=compiles,
                exit_code=exit_code, runs=runs, output=output, probe=probe)


def missing(reason):
    return dict(kind="missing", reason=reason)


def implemented(reason, probe=None):
    return dict(kind="implemented", reason=reason, probe=probe)


def value_bug(documented, observed, reason):
    return dict(kind="value_bug", reason=reason, documented=documented,
                observed=observed)


def section(reason, rule=None, prelude=None, stdin=None, records=None):
    """A heading's rule and its snippets' records, keyed by occurrence.

    `rule` overrides what the heading's own marker implies: "run" for a
    PROPOSED heading whose snippets compile anyway (the proposal is a semantic
    difference, not a syntax one), "proposed" is the default for a PROPOSED
    heading, and an unmarked heading must work like a LOCKED one.
    """
    return dict(reason=reason, rule=rule, prelude=prelude, stdin=stdin,
                records=records or {})


# What the reference does not have yet. Every snippet under one of these
# headings must still be rejected by the driver — which is what keeps a
# document full of future syntax from looking like a passing suite — and a
# snippet that starts compiling is reported so its heading can be promoted.
NOT_YET = {
    "20.4 `break` — IMPLEMENTATION PLANNED":
        "`break` is not in either parser; the heading says so",
    "20.5 `continue` — PROPOSED":
        "`continue` is not in either parser",
    "26.1 Primitive type names — PROPOSED / implementation-backed":
        "primitive type names are the runtime's, not a source-level construct",
    "26.2 Type annotations — PROPOSED":
        "no emitter reads a type annotation: the language is dynamically typed",
    "26.3 Function return annotations — PROPOSED":
        "no emitter reads a return annotation",
    "26.4 Generic types — FUTURE / PROPOSED":
        "generics are a FUTURE item: no parser, no checker, no codegen",
    "27.1 Tensor construction":
        "Phase 10: the tensor builtin does not exist yet",
    "27.2 Tensor arithmetic":
        "Phase 10",
    "27.3 Matrix multiplication — PROPOSED":
        "Phase 10",
    "27.4 Tensor metadata":
        "Phase 10",
    "27.5 Autodiff — PROPOSED":
        "Phase 10: no reverse-mode tape exists",
    "31.3 Unions — PROPOSED":
        "the language has no union type; the compiler uses an enum internally",
    "31.4 Pointers — PROPOSED / low-level feature":
        "no pointer syntax in either parser",
    "32.1 User-facing ownership syntax":
        "ownership is the runtime's business; the language has no syntax for it",
    "30.2 Constructors":
        "no constructors: classes are PROPOSED (§30.1)",
    "30.3 Instance members":
        "no instance members: classes are PROPOSED (§30.1)",
}

# Headings whose whole feature is implemented even though the reference calls
# it PROPOSED: recorded one by one, compiled like any other snippet, and
# reported as a contradiction to resolve. `SPEC` keeps the notes below short.
SPEC = "implemented even though the reference marks it PROPOSED"

# The module the reference uses that no engine has: `threads` (the language
# spawns with the `spawn`/`join`/`atomic` builtins instead). The other one it
# used to want, `random`, is now stdlib/random.fray — resolved by every engine
# from the standard library root, and pinned by tests/cases/random_module.
THREADS = ("the reference imports a `threads` module; the language has "
           "`spawn`, `join`, `joinAll`, `atomic`, `channel` and `send`/"
           "`recv` as builtins instead, and no module by that name exists "
           "(pin: tests/cases/threads*)")

HEADINGS = {
    "2. Source-file layout": section(
        "the example is a whole file: an import that resolves, a const, a def "
        "and a call"),
    "4.1 Indentation — LOCKED": section(
        "the fragment indents a body under an `if` whose x the tour gives",
        prelude="x = 1"),
    "5.1 Identifier shape — LOCKED / inferred": section(
        "the blocks list identifiers rather than a program: a bare name is "
        "not a statement that runs, which is what the record checks, and the "
        "forms themselves are pinned by functions and selfhosted_new_features",
        records={
            "1": recorded("a list of identifiers, not a program", compiles=False),
            "2": recorded("a list of identifiers, not a program", compiles=False),
        }),
    "7.5 Proposed string escapes — PROPOSED": section(
        "the escapes are in the lexer already: the block of bare strings "
        "prints nothing, so the record carries a probe that prints each one",
        rule="run",
        records={"1": implemented(
            'the layout proposes `\\n`, `\\t` and `\\"`, and both engines '
            "already interpret all three",
            probe=('print("line 1\\nline 2")\nprint("tab\\tvalue")\n'
                   'print("quote: \\"hello\\"")',
                   'line 1\nline 2\ntab\tvalue\nquote: "hello"'))}),
    "7.6 Proposed string interpolation — PROPOSED": section(
        "no interpolation: `$name` and `{name}` are literal text, and the "
        "snippet does not parse"),
    "9. Assignment operators": section(
        "the first block is the operator table rather than a program; the "
        "compound forms it lists are pinned by aug_assign",
        records={"1": recorded("an operator table, not a program",
                                compiles=False)}),
    "10. Expressions": section(
        "a list of expression forms, not a program: a bare expression is not a "
        "statement and x is never given a value (the forms themselves are "
        "pinned by the feature cases, and the module call by §22/§23)",
        records={"1": recorded(
            "a list of expression forms rather than a program — `x` has no "
            "value, and a bare `x` or `1 + 2` is not a statement either",
            compiles=False)}),
    "11.2 Function calls — LOCKED": section(
        "the second and third blocks call a placeholder `foo` with placeholder "
        "arguments to show the call syntax; the calls that run are pinned by "
        "functions and function_value",
        records={
            "2": recorded("`foo`, `a`, `b` and `c` are stand-ins for a callee "
                           "and its arguments", compiles=False),
            "3": recorded("`foo` is a stand-in for a callee", compiles=False),
        }),
    "12. Attribute / member access": section(
        "the block shows the `.` forms rather than a program: it reaches into "
        "the random module without importing it, and its `x` is never defined",
        records={"1": recorded(
            "`random` is used without `import random` (see §22.1), and `x` is "
            "never given a value, so neither `x.append(1)` nor `x.depend` has "
            "an object to work on",
            compiles=False)}),
    "Fray-specific power operator": section(
        "`^` is exponentiation; the fragment uses the tour's x",
        prelude="x = 4"),
    "14.2 Comparison operators — LOCKED": section(
        "the fragment compares the tour's x", prelude="x = 1"),
    "14.4 Bitwise operators — LOCKED in the reference": section(
        "the block's five operators are the ones the reference invented; the "
        "logical forms its heading shares a table with come from booleans",
        prelude="x = True\ny = False",
        records={"1": missing(
            "`band`, `bor`, `bxor`, `bnot` and `bxnor` — `and`, `or`, `not`, "
            "`xor` and `xnor` are the operators every engine implements, and "
            "these five appear in the reference and nowhere else: not in "
            "spec/grammar.md's table, not in either lexer, not in the oracle, "
            "not in the runtime")}),
    "16.1 Lists — LOCKED": section(
        "the demo of a nested list runs on a list of ints",
        records={"6": recorded(
            "the reference shows appending to a nested list, but the x of this "
            "section holds ints, so the documented line raises a TypeError in "
            "both engines (and used to be a segfault in the driver)",
            exit_code=1, output="")}),
    "17.1 Input": section(
        "the input family reads stdin; the snippets are run with a line each "
        "and diffed against the oracle on the same input",
        records={
            "1": run(stdin="ada\n41\nrest\n2.5\n"),
            "2": run(stdin="ada\n"),
        }),
    "17.2 Printing": section(
        "the fragment prints the x of §16.5, which is a tuple",
        prelude="x = ([1, 2], [3, 4])",
        records={"1": recorded(
            "the native front end has no tuple literal: `x = (…)` builds a "
            "list, so this prints [[1, 2], [3, 4]] where the oracle prints "
            "([1, 2], [3, 4]) (plan.md divergence 2)",
            exit_code=0, output="1\nHello\n[[1, 2], [3, 4]]\n[1, 2]")}),
    "17.3 Collection / numeric helpers": section(
        "the helpers are called on the tour's x", prelude="x = [1, 2, 3]"),
    "17.6 Statistics": section(
        "the statistics take a list, as mean/med/mode/mid always do here",
        prelude="x = [10, 20, 15, 12]"),
    "18.3 Calling a function — LOCKED": section(
        "the call uses the `add` that §18.2 defines",
        prelude="def add(a, b):\n    y = a + b\n    return y"),
    "19.1 `if` — LOCKED": section(
        "the branch tests the tour's x", prelude="x = 1"),
    "19.2 `elif` — LOCKED": section(
        "the branch tests the tour's x", prelude="x = 1"),
    "19.3 `else` — LOCKED": section(
        "the branches test the tour's x", prelude="x = 1"),
    "20.1 `for` — LOCKED": section(
        "the loop body increments the tour's x", prelude="x = 1"),
    "20.2 `while` — LOCKED": section(
        "a loop whose condition is the literal `True` cannot terminate",
        records={"1": recorded(
            "`while True` cannot terminate; the audit verifies that it compiles "
            "and does not run it", runs=False)}),
    "20.3 Iteration over collections": section(
        "the loop iterates the tour's x", prelude="x = [1, 2, 3]"),
    "20.5 `continue` — PROPOSED": section(
        "the layout calls `continue` PROPOSED because the project plan never "
        "locked it; both engines compile it",
        records={"1": implemented(
            "the layout calls `continue` PROPOSED, but both engines compile it "
            "and print 0..9 without 5")}),
    "20.6 Range helper": section(
        "`range` with one, two and three bounds; the third block names its "
        "bounds instead of numbering them, so the audit gives it numbers (the "
        "bounds themselves are pinned by range_bounds)",
        records={"3": run(prelude="start = 0\nstop = 3\nstep = 1")}),
    "21.1 `try` / `except` / `finally` — LOCKED": section(
        "the finally prints the tour's x: the assignment inside the try is the "
        "one that fails",
        prelude="x = 1",
        records={"1": recorded(
            "the failed assignment leaves None in x instead of keeping the "
            "value it had, so the documented finally prints None where the "
            "oracle prints the x from before the try",
            exit_code=0,
            output="Cannot add a string to a number!\nNone")}),
    "21.2 Exception type matching": section(
        "a clause on its own is not a program; the matching table it shows is "
        "pinned by error_handling and try_except",
        records={"1": recorded("an `except` clause without a `try`, not a "
                                "program", compiles=False)}),
    "22.2 Module members — LOCKED": section(
        "a member access on its own needs the module imported, as §22.1 shows",
        prelude="import random"),
    "23. The `random` module": section(
        "the module's whole documented surface: randomInt, and the three "
        "functions the section calls PROPOSED, which stdlib/random.fray now "
        "implements and tests/cases/random_module pins. The second block's `x` "
        "is a list, which is what choice and shuffle take.",
        prelude="import random\nx = [1, 2, 3]"),
    "24. Boolean / truthiness model": section(
        "the truthiness fragment uses the x and y of §14.3",
        prelude="x = True\ny = False"),
    "28.1 Spawn": section(THREADS, records={"1": missing(THREADS)}),
    "28.2 Join": section(THREADS, records={"1": missing(THREADS)}),
    "28.3 Join all": section(THREADS, records={"1": missing(THREADS)}),
    "28.4 Atomic values": section(THREADS, records={"1": missing(THREADS)}),
    "33. Error-producing expressions": section(
        "the fragment raises on its first line, in both engines, which is what "
        "the section is about",
        prelude="x = [1, 2, 3]",
        records={"1": recorded(
            "`1 + \"10\"` is the first line and it raises, so the block is a "
            "fatal error in both engines with no output before it",
            exit_code=1, output="")}),
    "4.2 Blank lines": section("prose only — no snippet"),
    "1. Syntax philosophy": section("prose only — no snippet"),
    "22.3 Future import forms — PROPOSED": section(
        "the proposed import forms (aliases, selective imports, stars) do not "
        "parse"),
    "Punctuation": section("prose only — no snippet"),
    "Assignment": section("prose only — no snippet"),
    "Comparison": section("prose only — no snippet"),
    "Arithmetic": section("prose only — no snippet"),
    "Logical / bitwise word operators": section("prose only — no snippet"),
    "Keywords": section("prose only — no snippet"),
    "Literals": section("prose only — no snippet"),
    "Identifier": section("prose only — no snippet"),
    "Final status": section("prose only — no snippet"),
    "Comment": section("prose only — no snippet"),
}


# --------------------------------------------------------------------------
# running things
# --------------------------------------------------------------------------

def _run(cmd, timeout, stdin=None):
    """Run a command, returning (stdout, stderr, returncode, timed_out)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           input=stdin)
        return r.stdout, r.stderr, r.returncode, False
    except subprocess.TimeoutExpired as e:
        def text(v):
            if v is None:
                return ""
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else v
        return text(e.stdout), text(e.stderr), -1, True


def normalize(s):
    s = NONE_REPR_RE.sub("<object>", s.replace("\r\n", "\n"))
    s = FUNC_REPR_RE.sub("<function>", s)
    return s.strip()


def read_support_list():
    if not SUPPORT_LIST.exists():
        return set()
    return {
        line.strip() for line in SUPPORT_LIST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def driver_rejects(driver, source, workdir, name):
    """(True, diagnostic) when the driver rejects this source, (False, ...) when
    it compiles it, (None, reason) when it failed without naming a stage."""
    path = workdir / f"{name}.fray"
    path.write_text(source)
    out, err, rc, timed = _run([str(driver), str(path), str(COMPILER_DIR),
                                str(STDLIB_DIR)], 120)
    if timed:
        return None, "the driver did not terminate"
    if rc != 0 or "===IR_START===" not in out:
        lines = [l.strip() for l in (out + "\n" + err).splitlines() if l.strip()]
        diags = [l for l in lines if DIAG_RE.match(l)]
        if diags:
            return True, diags[0][:120]
        return None, "rejected without naming a stage (internal error)"
    return False, "compiled"


def build_and_run(driver, source, workdir, name, stdin=""):
    """Compile `source` with the native chain and run it."""
    src = workdir / f"{name}.fray"
    exe = workdir / f"{name}.bin"
    src.write_text(source)
    out, err, rc, timed = _run(
        ["sh", str(CHAIN), "--driver", str(driver), str(src), "-o", str(exe)], 300)
    if timed:
        return False, "the compile chain did not terminate", "", "", -1
    if rc != 0 or not exe.exists():
        lines = [l.strip() for l in (out + "\n" + err).splitlines() if l.strip()]
        diags = [l for l in lines if DIAG_RE.match(l)]
        detail = (diags or lines[-1:])[:1]
        return False, "compile failed" + (f": {detail[0][:140]}" if detail
                                            else ": no output"), "", "", -1
    run_out, run_err, run_rc, timed = _run([str(exe)], 60, stdin=stdin)
    if timed:
        return False, "did not terminate", run_out, run_err, -1
    return True, "ran", run_out, run_err, run_rc


def oracle_run(source, workdir, name, stdin=""):
    src = workdir / f"{name}.fray"
    src.write_text(source)
    out, err, rc, timed = _run([PYTHON, str(ORACLE), str(src)], 120, stdin=stdin)
    if timed:
        return False, "the oracle did not terminate", "", "", -1
    return True, "ran", out, err, rc


def compare(oracle_out, oracle_rc, run_out, run_rc):
    """None when the native result matches the oracle, else the difference."""
    if run_rc != oracle_rc:
        return f"exit {run_rc}, the oracle exits {oracle_rc}"
    if normalize(run_out) != normalize(oracle_out):
        return "output differs from the oracle\n" + "".join(difflib.unified_diff(
            normalize(oracle_out).splitlines(keepends=True),
            normalize(run_out).splitlines(keepends=True),
            fromfile="oracle", tofile="driver"))
    return None


def check_snippet(driver, snippet, workdir, tag):
    """Verify one documented snippet. Returns (ok, evidence, detail)."""
    # Companion sources go in beside the program and come out again: the work
    # directory is shared by every snippet in the audit, so a module left
    # behind would let a later snippet's import resolve when it should not.
    written = []
    try:
        for name, text in snippet.files.items():
            (workdir / name).write_text(text)
            written.append(workdir / name)
        return _check_snippet(driver, snippet, workdir, tag)
    finally:
        for path in written:
            path.unlink(missing_ok=True)


def _check_snippet(driver, snippet, workdir, tag):
    source = snippet.source
    if snippet.rule == "proposed" and snippet.kind == "run":
        # The reference marks this construct as not-yet-final, so what it
        # claims is rejection — and a snippet that starts compiling is the
        # report, not the failure.
        rejected, why = driver_rejects(driver, source, workdir, tag)
        if rejected is None:
            return False, f"FAIL: {why}", why
        if not rejected:
            return False, ("FAIL: marked PROPOSED, but it compiles — promote "
                           "the heading or record it as implemented"), why
        return True, f"marked PROPOSED and still rejected ({why})", why
    if snippet.kind == "missing":
        rejected, why = driver_rejects(driver, source, workdir, tag)
        if rejected is None:
            return False, f"FAIL: {why}", why
        if not rejected:
            return False, ("FAIL: this compiles now — move it out of the "
                           "records and let the audit verify it"), why
        return True, f"unimplemented: {snippet.reason}", why

    if snippet.kind == "recorded" and not snippet.runs:
        # Compile-only: the record says running this cannot finish, so the
        # audit does not start it (a timeout per run is not a check).
        rejected, why = driver_rejects(driver, source, workdir, tag)
        if rejected is None:
            return False, f"FAIL: {why}", why
        if rejected:
            return False, ("FAIL: recorded as compiling, but the driver "
                           f"rejects it: {why}"), why
        return True, f"recorded: {snippet.reason} (compiles; not run)", why

    ok, detail, run_out, run_err, run_rc = build_and_run(
        driver, source, workdir, tag, snippet.stdin)

    if snippet.kind == "recorded":
        if not snippet.compiles:
            rejected, why = driver_rejects(driver, source, workdir, tag)
            if rejected:
                return True, f"recorded: {snippet.reason} ({why})", why
            return False, "FAIL: recorded as rejected, but " + why, why
        if not ok:
            return False, f"FAIL: recorded as compiling, but {detail}", detail
        if run_rc != snippet.exit_code:
            return False, (f"FAIL: recorded as exiting {snippet.exit_code}, "
                           f"but it exits {run_rc}"), detail
        if snippet.output is not None and normalize(run_out) != snippet.output.strip():
            return False, ("FAIL: recorded as printing "
                           f"{snippet.output.strip()!r}, it prints "
                           f"{normalize(run_out)!r} — the behaviour changed, "
                           "re-read the record"), detail
        first = (run_err.strip().splitlines() or [""])[0][:80]
        return True, (f"recorded: {snippet.reason} "
                      f"(exit {run_rc}{', ' + first if first else ''})"), detail

    if not ok:
        return False, f"FAIL: {detail}", detail
    oracle_ok, oracle_detail, oracle_out, oracle_err, oracle_rc = oracle_run(
        source, workdir, tag, snippet.stdin)
    if not oracle_ok:
        return False, f"FAIL: {oracle_detail}", oracle_detail
    if oracle_rc != 0:
        first = (oracle_err.strip().splitlines() or [""])[-1][:100]
        return False, (f"FAIL: the oracle cannot run this documented snippet "
                       f"(exit {oracle_rc}: {first}) — fix it or record it"), first
    difference = compare(oracle_out, oracle_rc, run_out, run_rc)
    if difference:
        return False, f"FAIL: {difference}", difference

    if snippet.kind == "value_bug":
        # The reference annotates a value the language does not produce: check
        # the language still produces the recorded one, so the reference stays
        # wrong for the reason recorded and not for a new one.
        value_ok, value_detail, got, _err, value_rc = build_and_run(
            driver, snippet.value_source, workdir, tag + "_value", snippet.stdin)
        first = normalize(got).splitlines()[0] if normalize(got) else "(no output)"
        if not value_ok or value_rc != 0:
            return False, f"FAIL: the value check did not run: {value_detail}", value_detail
        if first == snippet.documented:
            return False, ("FAIL: the reference's value is right now — drop "
                           "the value_bug record"), first
        if first != snippet.observed:
            return False, (f"FAIL: recorded as printing {snippet.observed}, "
                           f"it prints {first}"), first
        return True, (f"documented as {snippet.documented}, the language "
                      f"prints {snippet.observed}: {snippet.reason}"), first

    if snippet.probe:
        probe_source, expected = snippet.probe
        again, why, got, _err, rc = build_and_run(
            driver, probe_source, workdir, tag + "_probe", snippet.stdin)
        if not again or rc != 0 or normalize(got) != expected.strip():
            return False, ("FAIL: the record's probe no longer holds — it "
                           f"prints {normalize(got)!r}, the record says "
                           f"{expected.strip()!r}"), detail
    if snippet.kind == "implemented":
        return True, f"{SPEC}: {snippet.reason}", ""
    return True, "compiled, ran and matched the oracle", ""


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------

def apply_records(snippets, headings, failures, not_yet=None, rules=None):
    """Attach the tool's rules and records to the reference's snippets.

    `not_yet` and `rules` default to this document's tables. `check_tutorial.py`
    passes its own: the two documents share the engine and the snippet model,
    but a tutorial has no PROPOSED sections to police and its records are its
    own.
    """
    not_yet = NOT_YET if not_yet is None else not_yet
    rules = HEADINGS if rules is None else rules
    counts, used = {}, {}
    for snippet in snippets:
        counts[snippet.heading] = counts.get(snippet.heading, 0) + 1
        snippet.occurrence = counts[snippet.heading]

    for snippet in snippets:
        if snippet.heading in not_yet:
            # A whole unbuilt feature: every snippet in it must still be
            # rejected, and a record can still single one out.
            snippet.rule = "not_yet"
            snippet.reason = NOT_YET[snippet.heading]
            snippet.kind = "missing"
        elif snippet.status == "PROPOSED":
            snippet.rule = "proposed"

    for heading, rule in rules.items():
        if heading not in headings:
            failures.append(f"`{heading}` is gone from the reference — "
                            "re-audit this section")
            continue
        if rule["rule"]:
            for snippet in snippets:
                if snippet.heading == heading:
                    snippet.rule = rule["rule"]
        for snippet in snippets:
            if snippet.heading != heading:
                continue
            if rule["stdin"] and not snippet.stdin:
                snippet.stdin = rule["stdin"]
            if rule["prelude"]:
                snippet.prelude = (rule["prelude"] + "\n" + snippet.prelude).strip()
            key = str(snippet.occurrence)
            entry = rule["records"].get(key)
            if entry is None:
                continue
            used[(heading, key)] = True
            for field, value in entry.items():
                if field == "prelude":
                    snippet.prelude = (value + "\n" + snippet.prelude).strip()
                    continue
                setattr(snippet, field, value)
            snippet.rule = "recorded"
            snippet.reason = entry.get("reason") or rule["reason"]
        for key in rule["records"]:
            if (heading, key) not in used:
                failures.append(f"`{heading}`: the record for snippet {key} "
                                "matches nothing now — re-read the section")
    for heading in not_yet:
        if heading not in headings:
            failures.append(f"`{heading}` is gone from the reference — re-audit "
                            "this section")
    return counts


def check(driver, verbose, listing, doc, parse=None, rules=None, not_yet=None,
          failures=None):
    """Audit one document. `parse`, `rules` and `not_yet` default to this
    document's; `check_tutorial.py` passes its own, because a tutorial is
    audited by the same engine but holds different promises."""
    snippets, document = (parse or parse_document)(doc)
    failures = [] if failures is None else failures
    counts = {"run": 0, "recorded": 0, "missing": 0, "value_bug": 0,
              "implemented": 0}
    headings = apply_records(snippets, document, failures,
                             not_yet=not_yet, rules=rules)

    print(f"Auditing {doc.name} against {driver}"
          if not listing else f"{doc.name} as read by the audit")
    if listing:
        for heading in dict.fromkeys(s.heading for s in snippets):
            print(f"\n{heading}")
            for snippet in snippets:
                if snippet.heading == heading:
                    print(f"    {snippet.kind:<11}{snippet.lineno:>5}  "
                          f"{snippet.label}")
        print(f"\n{len(document)} headings, {len(snippets)} documented snippets")
        return 0
    results = []
    with tempfile.TemporaryDirectory(prefix="fray_ref_audit_") as td:
        workdir = Path(td)
        seen = {}
        for snippet in snippets:
            tag = re.sub(r"\W+", "_", snippet.heading)[:28] + f"_{snippet.lineno}"
            ok, evidence, detail = check_snippet(driver, snippet, workdir, tag)
            if ok and snippet.kind == "missing":
                evidence = f"unimplemented: {snippet.reason}"
            counts[snippet.kind] = counts.get(snippet.kind, 0) + 1
            seen.setdefault(snippet.heading, []).append((snippet, ok, evidence, detail))
            results.append((snippet, ok, evidence, detail))

    for heading, rows in seen.items():
        failed = [r for r in rows if not r[1]]
        status = "FAIL" if failed else "ok"
        print(f"  {status:<6}{heading[:52]:<54} {len(rows)} snippet"
              f"{'s' if len(rows) != 1 else ''}")
        for snippet, ok, evidence, _detail in rows:
            if verbose or not ok:
                print(f"    {'ok' if ok else 'FAIL':<5} {snippet.label:<48} {evidence}")

    for snippet, ok, _evidence, detail in results:
        if not ok:
            failures.append(f"{snippet.heading}: `{snippet.label}` — {detail}")

    total = len(snippets)
    proposed = sum(1 for s in snippets
                   if s.rule == "proposed" and s.kind == "run")
    diffed = counts['run'] - proposed + counts['value_bug']
    print(f"\n{doc.name}: {len(document)} headings ({len(headings)} of them "
          f"document snippets), {total} documented snippets "
          f"— {diffed} compiled, run and diffed against the oracle, "
          f"{counts['recorded'] + counts['implemented']} compiled and run with "
          f"their output pinned (the oracle cannot be the reference for them), "
          f"{proposed} marked PROPOSED and checked for rejection, "
          f"{counts['missing']} recorded as unimplemented")

    def report(title, rows):
        if not rows:
            return
        print(f"\n{title}")
        for heading, label, evidence in rows:
            print(f"  {heading}\n      `{label}` {evidence}")

    report("The reference calls these PROPOSED; the compiler implements them "
           "(checked every run):",
           [(s.heading, s.label, "") for s in snippets if s.kind == "implemented"])
    report("Recorded, deliberately: something the document shows that cannot "
           "be verified against the oracle:",
           [(s.heading, s.label, f"— {s.reason}")
            for s in snippets if s.kind == "recorded"])
    report("Documented but not implemented (still rejected, checked every run):",
           [(s.heading, s.label, f"— {s.reason}")
            for s in snippets if s.kind == "missing"])
    report("The reference is wrong about the language (checked every run):",
           [(s.heading, s.label,
             f"— documented as {s.documented}, the language prints {s.observed}")
            for s in snippets if s.kind == "value_bug"])

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("\nEvery documented snippet is accounted for: the ones that work are "
          "compiled, run and diffed against the oracle on every run, and the "
          "ones that cannot are recorded with the behaviour they have.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--driver", default=None,
                        help="native compiler binary (default: build/frayc_driver, "
                             "then bin/frayc_driver)")
    parser.add_argument("--doc", default=None,
                        help="the syntax reference (default: fray-layout.md, "
                             "then fray.txt)")
    parser.add_argument("--list", action="store_true",
                        help="print the parsed structure and the records only")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="one line per documented snippet")
    args = parser.parse_args()

    if args.doc:
        doc = Path(args.doc)
    else:
        doc = next((REPO_ROOT / name for name in REFERENCE_NAMES
                    if (REPO_ROOT / name).exists()), None)
    if doc is None or not doc.exists():
        print(f"no syntax reference found — expected one of "
              f"{', '.join(REFERENCE_NAMES)} in {REPO_ROOT}")
        return 1
    driver = Path(args.driver) if args.driver else default_driver()
    if not args.list and not driver.exists():
        print(f"no compiler at {driver} — build it first "
              "(see plan.md's Stage 2 section)")
        return 1
    return check(driver, args.verbose, args.list, doc)


if __name__ == "__main__":
    sys.exit(main())
