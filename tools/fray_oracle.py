#!/usr/bin/env python3
"""
fray oracle — runs .fray files through the tree-walking evaluator.

Usage:
    python tools/fray_oracle.py program.fray
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bootstrap"))

from evaluator import run, set_oracle_argv


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file.fray> [args...]", file=sys.stderr)
        sys.exit(1)

    filename = sys.argv[1]
    with open(filename, "r") as f:
        source = f.read()

    # progName()/args() see the oracle's own command line, mirroring what
    # fray_init_args stores for a compiled binary.
    set_oracle_argv(filename, sys.argv[2:])

    try:
        output = run(source, filename)
        sys.stdout.write(output)
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
