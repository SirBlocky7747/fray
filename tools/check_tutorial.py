#!/usr/bin/env python3
"""
Audit docs/fray_by_example.md: every snippet in the tutorial must compile, run
and print what the oracle prints.

This is the same engine as tools/check_fray_txt.py, which audits the syntax
reference. The two documents make different promises and so carry different
records, but the snippet model, the record kinds, the rules and the report are
shared — imported, not copied, so a snippet means the same thing in both.

Why the tutorial needs its own audit rather than a mention in the reference
audit: it is the document a newcomer reads first, and it is written as whole
programs ("Every feature is demonstrated with runnable code"), so its snippets
must *run*, not merely parse. A tutorial that teaches a construct the compiler
rejects is worse than no tutorial: the reader's first program does not work and
nothing says why.

A snippet that cannot work is recorded here with the behaviour it has — the
audit then checks the record on every run, so a snippet that starts working (or
stops) is reported rather than silently tolerated.

    python tools/check_tutorial.py                  # audit
    python tools/check_tutorial.py --list           # structure only
    python tools/check_tutorial.py -v               # one line per snippet
    python tools/check_tutorial.py --driver PATH    # a different driver
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_fray_txt as ref

REPO_ROOT = ref.REPO_ROOT
DEFAULT_DOC = REPO_ROOT / "docs" / "fray_by_example.md"

# The tutorial has no PROPOSED sections: it documents what the language does,
# so nothing in it is allowed to be rejected unless it is recorded below.
NOT_YET = {}


def parse_tutorial(text):
    """Snippets of the tutorial, by heading, plus every fence the audit could
    not classify.

    `parse_markdown` reads the tagged ```fray blocks and ignores everything
    else, which is right for the reference (whose untagged blocks are shell
    transcripts) and wrong here: an untagged fence in the tutorial is either a
    program the audit silently never ran, or expected output that should say
    so. Both are reported instead, so the convention is enforced rather than
    assumed.
    """
    untagged = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            language = line[3:].strip()
            body, j = [], i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                body.append(lines[j])
                j += 1
            if language not in ("fray", "text", "output") and \
                    "".join(body).strip():
                untagged.append((i + 1, line.strip() or "(bare fence)"))
            i = j + 1
            continue
        i += 1

    snippets = ref.parse_markdown(text)
    return snippets, untagged


# --------------------------------------------------------------------------
# the records
# --------------------------------------------------------------------------

def section(reason, prelude=None, stdin=None, records=None):
    return ref.section(reason, prelude=prelude, stdin=stdin, records=records)


# The module the "Modules and imports" section teaches, written beside the
# program that imports it. The section is two snippets because it is two
# files; the second cannot stand up without the first, which is what this is.
MATH_LIB = '''def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
'''

TUTORIAL = {
    "Lists": section(
        "a list, indexed, sliced, appended to, and with its last element "
        "removed"),
    "Maps (dictionaries)": section(
        "a map keyed by strings, with insert, lookup, delete and length",
        records={
            # Map methods are native-only: the oracle has no `keys` or `has`,
            # so this section cannot be diffed against it the way every other
            # one is. It is compiled, run and pinned here instead — the probe
            # is what keeps `keys()` from quietly changing what it returns.
            "1": dict(
                kind="recorded",
                reason="the oracle has no map methods, so `keys()` and `has()` "
                       "cannot be diffed against it; the probe pins what they "
                       "do natively",
                exit_code=0,
                output="",
                probe=('m = {"a": 1, "b": 2}\n'
                       'print(m.keys())\n'
                       'print(m.has("a"))\n'
                       'print(m.has("z"))\n',
                       '["a", "b"]\nTrue\nFalse'),
            ),
        }),
    "Loops: for": section(
        "the iterable forms the section shows"),
    "Scope": section(
        "what a function may and may not rebind"),
    "Modules and imports": section(
        "two files: the module, then the program that imports it",
        records={
            "2": dict(files={"math_lib.fray": MATH_LIB}),
        }),
    "Threading": section(
        "two threads started from one function, joined together"),
    "Input": section(
        "a prompt and a reply read from stdin",
        stdin="fray\n"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--driver", default=None,
                        help="native compiler binary (default: build/frayc_driver, "
                             "then bin/frayc_driver)")
    parser.add_argument("--doc", default=str(DEFAULT_DOC),
                        help="the tutorial (default: docs/fray_by_example.md)")
    parser.add_argument("--list", action="store_true",
                        help="print the parsed structure and the records only")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="one line per snippet")
    args = parser.parse_args()

    doc = Path(args.doc)
    if not doc.exists():
        print(f"no tutorial at {doc}")
        return 1
    driver = Path(args.driver) if args.driver else ref.default_driver()
    if not args.list and not driver.exists():
        print(f"no compiler at {driver} — build it first "
              "(see plan.md's Stage 2 section)")
        return 1

    untagged_failures = []

    def parse(path):
        """What `ref.check` calls to read the document: its snippets and its
        headings. The untagged fences are a finding, not part of that pair, so
        they are reported through the failures list `check` is handed."""
        text = path.read_text()
        snippets, untagged = parse_tutorial(text)
        del untagged_failures[:]
        untagged_failures.extend(
            f"{path.name}:{line}: untagged fence — a snippet the audit never "
            f"ran, or output that should say ```text ({fence})"
            for line, fence in untagged)
        return snippets, ref.document_headings(text)

    if args.list:
        snippets, headings = parse(doc)
        for heading in dict.fromkeys(s.heading for s in snippets):
            print(f"\n{heading}")
            for snippet in snippets:
                if snippet.heading == heading:
                    print(f"    {snippet.kind:<11}{snippet.lineno:>5}  "
                          f"{snippet.label}")
        print(f"\n{len(headings)} headings, {len(snippets)} snippets")
        for failure in untagged_failures:
            print(failure)
        return 1 if untagged_failures else 0

    return ref.check(driver, args.verbose, False, doc, parse=parse,
                     rules=TUTORIAL, not_yet=NOT_YET,
                     failures=untagged_failures)


if __name__ == "__main__":
    sys.exit(main())
