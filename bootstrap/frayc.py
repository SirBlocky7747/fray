#!/usr/bin/env python3
"""
frayc — fray compiler driver.

Usage:
    python frayc.py build program.fray -o program
    python frayc.py run program.fray
    python frayc.py oracle program.fray
"""

import argparse
import os
import sys
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from lexer import tokenize, LexerError
from parser import parse, ParseError
from sema import analyze, SemanticError
from codegen import compile_program, compile_to_ir, compile_to_object


def cmd_build(args):
    """Compile a .fray file to a native binary."""
    source_path = args.source
    output_path = args.output

    if not output_path:
        output_path = os.path.splitext(source_path)[0]

    with open(source_path, "r") as f:
        source = f.read()

    try:
        compile_program(source, output_path, source_path)
        print(f"Compiled {source_path} -> {output_path}")
    except (LexerError, ParseError, SemanticError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Compilation error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_run(args):
    """Compile and run a .fray file."""
    source_path = args.source

    with open(source_path, "r") as f:
        source = f.read()

    # Compile to temp binary
    with tempfile.NamedTemporaryFile(suffix=".out", delete=False) as tmp:
        binary_path = tmp.name

    try:
        compile_program(source, binary_path, source_path)
        result = subprocess.run([binary_path])
        sys.exit(result.returncode)
    finally:
        if os.path.exists(binary_path):
            os.remove(binary_path)


def cmd_oracle(args):
    """Run a .fray file through the oracle (tree-walking evaluator)."""
    source_path = args.source

    with open(source_path, "r") as f:
        source = f.read()

    from evaluator import run
    try:
        output = run(source, source_path)
        sys.stdout.write(output)
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_ir(args):
    """Compile a .fray file to LLVM IR (for debugging)."""
    source_path = args.source

    with open(source_path, "r") as f:
        source = f.read()

    try:
        from codegen import compile_to_ir
        ir_text = compile_to_ir(source, source_path)
        print(ir_text)
    except (LexerError, ParseError, SemanticError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="fray compiler")
    subparsers = parser.add_subparsers(dest="command")

    # build
    build_parser = subparsers.add_parser("build", help="Compile .fray to native binary")
    build_parser.add_argument("source", help="Source file (.fray)")
    build_parser.add_argument("-o", "--output", help="Output binary path")

    # run
    run_parser = subparsers.add_parser("run", help="Compile and run .fray")
    run_parser.add_argument("source", help="Source file (.fray)")

    # oracle
    oracle_parser = subparsers.add_parser("oracle", help="Run through oracle (evaluator)")
    oracle_parser.add_argument("source", help="Source file (.fray)")

    # ir
    ir_parser = subparsers.add_parser("ir", help="Show LLVM IR (debugging)")
    ir_parser.add_argument("source", help="Source file (.fray)")

    args = parser.parse_args()

    if args.command == "build":
        cmd_build(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "oracle":
        cmd_oracle(args)
    elif args.command == "ir":
        cmd_ir(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
