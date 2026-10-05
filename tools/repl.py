#!/usr/bin/env python3
"""
fray REPL — fast compile-and-run snippet loop.

Usage:
    python tools/repl.py           # interactive mode
    python tools/repl.py file.fray  # run a file, then drop into REPL

The REPL accumulates user input, wraps bare expressions in print(),
and recompiles the entire buffer each time. State persists between lines.
"""

import os
import sys
import tempfile
import subprocess
import signal

# Add bootstrap to path so we can import the compiler
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
BOOTSTRAP_DIR = os.path.join(PROJECT_ROOT, "bootstrap")
sys.path.insert(0, BOOTSTRAP_DIR)

from lexer import Lexer, LexerError
from parser import Parser, ParseError
from sema import Analyzer, SemanticError
from codegen import compile_program

import target  # platform detection (bootstrap/target.py)

BANNER = """\
Welcome to fray v0.1.0  (AOT-compiled, GIL-free)
Type expressions to evaluate them, statements to execute them.
Type 'help' for commands, 'exit' or Ctrl-D to quit.
"""

HELP_TEXT = """\
Commands:
  help       Show this help
  exit       Quit the REPL
  clear      Reset all accumulated code
  history    Show accumulated code
  run FILE   Run a file and add its contents to the session
  :ast       Show AST for the last input
  :ir        Show LLVM IR for the last input
  compile    Compile the session to a native binary

Anything else is fray code. Bare expressions are printed automatically.
Multi-line blocks (def, if, for, while, etc.) are entered normally —
press Enter on an empty line to finish the block.
"""

# ─── Helper: find the compiler executable or use Python fallback ───

def find_compiler():
    """Find the best available compiler."""
    # Check for compiled binary
    for name in ["frayc", "frayc.exe"]:
        path = os.path.join(PROJECT_ROOT, name)
        if os.path.isfile(path):
            return ("binary", path)
    # Check for Python bootstrap
    if os.path.isfile(os.path.join(BOOTSTRAP_DIR, "frayc.py")):
        return ("python", os.path.join(BOOTSTRAP_DIR, "frayc.py"))
    return (None, None)


def compile_and_run(source, compiler_info):
    """Compile source to a temp file and run it. Returns (stdout, stderr, returncode)."""
    kind, compiler_path = compiler_info

    with tempfile.TemporaryDirectory() as td:
        src_path = os.path.join(td, "repl_session.fray")
        exe_path = os.path.join(td, "repl_session" + target.exe_suffix())
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(source)

        if kind == "binary":
            # Use the compiled binary
            r = subprocess.run(
                [compiler_path, "build", src_path, "-o", exe_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return ("", r.stderr, r.returncode)
            r2 = subprocess.run(
                [exe_path], capture_output=True, text=True, timeout=30,
                cwd=td,
            )
            return (r2.stdout, r2.stderr, r2.returncode)
        else:
            # Use Python fallback
            r = subprocess.run(
                [sys.executable, compiler_path, "oracle", src_path],
                capture_output=True, text=True, timeout=30,
            )
            return (r.stdout, r.stderr, r.returncode)


def compile_to_binary(source, compiler_info, output_path):
    """Compile source to a native binary."""
    kind, compiler_path = compiler_info

    with tempfile.TemporaryDirectory() as td:
        src_path = os.path.join(td, "repl_session.fray")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(source)

        if kind == "binary":
            r = subprocess.run(
                [compiler_path, "build", src_path, "-o", output_path],
                capture_output=True, text=True, timeout=60,
            )
        else:
            r = subprocess.run(
                [sys.executable, compiler_path, "build", src_path, "-o", output_path],
                capture_output=True, text=True, timeout=60,
            )
        return (r.stdout, r.stderr, r.returncode)


# ─── Input handling ───

def read_block(prompt_char=">>> "):
    """Read a block of fray code. Handles multi-line blocks."""
    try:
        line = input(prompt_char)
    except EOFError:
        return None

    if not line.strip():
        return ""

    # Check if this starts a block (lines that need matching dedent)
    block_keywords = ("def ", "if ", "elif ", "else:", "for ", "while ",
                      "try:", "except", "finally:", "match ", "case ",
                      "enum ", "struct ", "extern ")
    starts_block = any(line.strip().startswith(kw) for kw in block_keywords)

    if not starts_block:
        # Check if line ends with colon (simple block opener)
        if line.rstrip().endswith(":") and not line.strip().startswith("#"):
            starts_block = True

    if not starts_block:
        return line

    # Read indented lines until we get a line at indent 0 or empty line
    lines = [line]
    try:
        while True:
            continuation = input("... ")
            if continuation == "":
                break
            lines.append(continuation)
    except EOFError:
        pass

    return "\n".join(lines)


def is_expression(line):
    """Heuristic: is this line a bare expression that should be printed?"""
    stripped = line.strip()
    if not stripped:
        return False
    # Skip lines that are clearly statements
    statement_starts = (
        "def ", "if ", "elif ", "else:", "for ", "while ", "return ",
        "import ", "from ", "extern ", "const ", "struct ", "enum ",
        "case ", "match ", "del ", "break", "continue", "try:", "except",
        "finally:", "async ", "await ",
    )
    for s in statement_starts:
        if stripped.startswith(s):
            return False
    # Skip assignment lines
    if "=" in stripped and not stripped.startswith("="):
        # Simple check: if there's a = that's not == and not !=, <=, >=, +=
        parts = stripped.split("=")
        if len(parts) >= 2:
            before_eq = parts[0].rstrip()
            if before_eq and before_eq[-1] not in ("!", "<", ">", "+", "-", "*", "/", "%", "^"):
                return False
    # Only the calls whose result is not the point are statements. print
    # already writes its own output; exit and clear are REPL commands and
    # input prompts on stdin. Everything else -- len, range, str, abs, sum,
    # Ok, isNone and the rest of the builtins -- is a value the user asked to
    # see, and listing them here silently swallowed it: typing len(xs)
    # returned nothing at all, which reads as a broken REPL rather than as a
    # deliberate rule.
    if "(" in stripped and ")" in stripped:
        func_name = stripped.split("(")[0].strip()
        if func_name in ("print", "exit", "clear", "input"):
            return False
    return True


# ─── REPL commands ───

def repl_help():
    print(HELP_TEXT)


def repl_clear():
    return ""


def repl_history(code_buffer):
    if code_buffer.strip():
        print(code_buffer)
    else:
        print("(no code accumulated)")


def repl_run_file(filepath, code_buffer):
    """Run a file and add its contents to the session."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            contents = f.read()
        if code_buffer.strip():
            return code_buffer + "\n" + contents
        return contents
    except FileNotFoundError:
        print(f"Error: file not found: {filepath}")
        return code_buffer


# ─── Main REPL loop ───

def repl():
    """Run the interactive REPL."""
    compiler_info = find_compiler()
    if compiler_info[0] is None:
        print("Error: no compiler found. Need either a compiled binary or the Python bootstrap.")
        sys.exit(1)

    print(BANNER)
    print(f"Compiler: {compiler_info[1]}")
    print()

    code_buffer = ""
    pending_input = []  # For multi-line blocks

    while True:
        if pending_input:
            line = read_block("... ")
        else:
            line = read_block(">>> ")

        if line is None:
            # EOF
            print("\nGoodbye!")
            break

        line = line.rstrip()

        # Handle empty line: execute pending block
        if not line.strip() and pending_input:
            block = "\n".join(pending_input)
            pending_input = []

            if code_buffer.strip():
                full_source = code_buffer + "\n" + block + "\n"
            else:
                full_source = block + "\n"

            # Try to execute
            stdout, stderr, rc = compile_and_run(full_source, compiler_info)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
            if rc == 0:
                code_buffer = full_source
            continue

        if not line.strip():
            continue

        # Handle commands
        if line.strip() == "exit" or line.strip() == "quit":
            print("Goodbye!")
            break
        if line.strip() == "help":
            repl_help()
            continue
        if line.strip() == "clear":
            code_buffer = ""
            print("(session cleared)")
            continue
        if line.strip() == "history":
            repl_history(code_buffer)
            continue
        if line.strip().startswith("run "):
            filepath = line.strip()[4:].strip()
            code_buffer = repl_run_file(filepath, code_buffer)
            continue
        if line.strip() == ":ast":
            try:
                tokens = Lexer(code_buffer, "<repl>").tokenize()
                tree = Parser(tokens).parse()
                for stmt in tree.body:
                    print(stmt)
            except (LexerError, ParseError) as e:
                print(f"Parse error: {e}", file=sys.stderr)
            continue
        if line.strip() == "compile":
            output = input("Output path: ").strip() or "a.out"
            stdout, stderr, rc = compile_to_binary(code_buffer, compiler_info, output)
            if rc == 0:
                print(f"Compiled to {output}")
            else:
                print(f"Compilation failed:", file=sys.stderr)
                if stderr:
                    print(stderr, file=sys.stderr)
            continue

        # Check if this starts a block
        block_keywords = ("def ", "if ", "elif ", "else:", "for ", "while ",
                          "try:", "except", "finally:", "match ", "case ",
                          "enum ", "struct ", "extern ")
        starts_block = any(line.strip().startswith(kw) for kw in block_keywords)
        if not starts_block and line.strip().endswith(":") and not line.strip().startswith("#"):
            starts_block = True

        if starts_block:
            pending_input.append(line)
            continue

        # Regular line: try to execute immediately
        if code_buffer.strip():
            full_source = code_buffer + "\n" + line + "\n"
        else:
            full_source = line + "\n"

        # If it looks like a bare expression, wrap in print
        if is_expression(line):
            # Include definitions from buffer, but not previous statements
            print_source = code_buffer + "\nprint(" + line + ")\n" if code_buffer.strip() else "print(" + line + ")\n"

            stdout, stderr, rc = compile_and_run(print_source, compiler_info)
            if rc == 0:
                if stdout:
                    print(stdout, end="")
                continue

            # Failed — show error
            if stderr:
                print(stderr, end="", file=sys.stderr)
            continue

        # Statement: execute with definitions from buffer, but only the new line's side effects
        if code_buffer.strip():
            full_source = code_buffer + "\n" + line + "\n"
        else:
            full_source = line + "\n"
        stdout, stderr, rc = compile_and_run(full_source, compiler_info)
        if stdout:
            print(stdout, end="")
        if stderr:
            print(stderr, end="", file=sys.stderr)
        if rc == 0:
            # Keep definitions, assignments, and other state-changing statements
            stripped = line.strip()
            keep = any(stripped.startswith(kw) for kw in
                       ("def ", "enum ", "struct ", "extern ", "const ",
                        "import ", "from ", "del "))
            # Keep simple assignments (x = ..., x += ..., etc.)
            if not keep and "=" in stripped and not stripped.startswith("="):
                parts = stripped.split("=")
                if len(parts) >= 2:
                    lhs = parts[0].rstrip()
                    if lhs and lhs[-1] not in ("!", "<", ">"):
                        keep = True
            if keep:
                if code_buffer.strip():
                    code_buffer = code_buffer + "\n" + line + "\n"
                else:
                    code_buffer = line + "\n"


# ─── Non-interactive: run a file ───

def run_file(filepath, compiler_info):
    """Run a .fray file."""
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    stdout, stderr, rc = compile_and_run(source, compiler_info)
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    return rc


# ─── Entry point ───

if __name__ == "__main__":
    compiler_info = find_compiler()

    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        # Run a file, then optionally drop into REPL
        rc = run_file(arg, compiler_info)
        if len(sys.argv) > 2 and sys.argv[2] == "--repl":
            repl()
        sys.exit(rc)
    else:
        repl()
