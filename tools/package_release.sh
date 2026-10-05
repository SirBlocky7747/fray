#!/usr/bin/env bash
# package_release.sh — assemble a fray release archive.
#
# Usage:
#   bash tools/package_release.sh [version]
#
# Produces (under dist/):
#   fray-<version>-linux-<arch>.tar.gz     (Linux)
#   fray-<version>-macos-<arch>.tar.gz     (macOS)
#   fray-<version>-windows-x64.zip         (Windows)
#
# The archive ships the native (Stage 2) toolchain:
#
#   bin/fray             front end: `fray run`, `fray build`, `fray ir`
#   bin/frayc            compile chain: driver → llc → cc
#   bin/frayc_driver     the compiler itself, a native binary compiled by fray
#   lib/libfrayrt.a      the runtime library every program links against
#   runtime/             the C sources that archive was built from
#   compiler/            the compiler's own fray source
#   stdlib/              the standard library: `import random` resolves here
#   examples/ docs/ spec/ tests/cases/
#   bootstrap/ tools/    the rebuild path (Python) — see QUICKSTART.md
#
# Compiling a program needs LLVM's `llc` and a C compiler. It never needs
# Python: bin/ is self-contained. Python only appears in bootstrap/ and the
# tools that drive it, which exist so the compiler can be rebuilt from source
# when no matching binary is available.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    if [ -f "$ROOT/VERSION" ]; then
        VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
    else
        echo "package_release.sh: no version given and no VERSION file" >&2
        exit 1
    fi
fi
VERSION="$(printf '%s' "$VERSION" | tr -d '[:space:]')"

DIST_DIR="$ROOT/dist"

# Detect platform
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$OS" in
    linux)  PLATFORM="linux-$ARCH"; ARCHIVE_EXT="tar.gz" ;;
    darwin) PLATFORM="macos-$ARCH"; ARCHIVE_EXT="tar.gz" ;;
    mingw*|msys*|cygwin*|windows*) PLATFORM="windows-x64"; ARCHIVE_EXT="zip" ;;
    *)      echo "Warning: unknown OS $OS, packaging for linux-x64" >&2
            PLATFORM="linux-x64"; ARCHIVE_EXT="tar.gz" ;;
esac

PACKAGE_NAME="fray-$VERSION-$PLATFORM"
PACKAGE_DIR="$DIST_DIR/$PACKAGE_NAME"

echo "=== fray release packaging ==="
echo "version:  $VERSION"
echo "platform: $PLATFORM"
echo ""

# 1. The compiler binary. It is platform-specific (it is linked for this host),
#    so packaging fails loudly rather than shipping a Python fallback.
DRIVER="$ROOT/build/frayc_driver"
# Windows executables carry an .exe suffix. Looking only for the suffixless
# name meant the Windows branch could never find a compiler it had just built,
# and failed with "no native compiler" pointing at a path that cannot exist
# there.
if [ ! -x "$DRIVER" ] && [ -x "$DRIVER.exe" ]; then
    DRIVER="$DRIVER.exe"
fi
if [ ! -x "$DRIVER" ]; then
    cat >&2 <<EOF
package_release.sh: no native compiler at $DRIVER

Build it first — this is the one step that needs the Python bootstrap
(Python 3.8+ with llvmlite, plus llc and a C compiler):

  python tools/frayc_selfhosted.py build compiler/frayc.fray \\
         -o build/frayc_driver --backend llc
EOF
    exit 1
fi

# 2. The runtime archive, rebuilt from the shipped sources so the package is
#    consistent with them.
if command -v make >/dev/null 2>&1; then
    make -s -C "$ROOT/runtime" libfrayrt.a
fi
LIBRT="$ROOT/runtime/libfrayrt.a"
if [ ! -f "$LIBRT" ]; then
    echo "package_release.sh: no runtime library at $LIBRT" >&2
    exit 1
fi

# 3. Lay out the package.
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"/{bin,lib,runtime,compiler,stdlib,bootstrap,tools,examples,docs,spec,tests/cases}

echo "--- compiler ---"
# Keep whatever suffix the compiler was built with: bin/fray and bin/frayc
# invoke it by name, so renaming frayc_driver.exe to frayc_driver would ship a
# Windows package that cannot run itself.
cp "$DRIVER" "$PACKAGE_DIR/bin/$(basename "$DRIVER")"
chmod +x "$PACKAGE_DIR/bin/$(basename "$DRIVER")"
cp "$ROOT/tools/frayc.sh" "$PACKAGE_DIR/bin/frayc"
cp "$ROOT/tools/fray.sh" "$PACKAGE_DIR/bin/fray"
chmod +x "$PACKAGE_DIR/bin/frayc" "$PACKAGE_DIR/bin/fray"
cp "$ROOT"/compiler/*.fray "$PACKAGE_DIR/compiler/"

# The standard library, at the package root: the driver looks for `stdlib/`
# beside its own directory (bin/), so a program in any working directory can
# `import random`. It is a search root of its own, after the program's own
# directory — a program's own random.fray still wins.
echo "--- standard library ---"
cp "$ROOT"/stdlib/*.fray "$PACKAGE_DIR/stdlib/"

echo "--- runtime ---"
# The archive the link step uses, and the sources it was built from. The
# sources live in runtime/ rather than next to the archive so `make -C runtime`
# in the package rebuilds them the way the repository does.
cp "$LIBRT" "$PACKAGE_DIR/lib/libfrayrt.a"
cp "$ROOT/runtime"/*.c "$ROOT/runtime"/*.h "$ROOT/runtime/Makefile" "$PACKAGE_DIR/runtime/"

echo "--- rebuild path (Python, not used to compile programs) ---"
cp "$ROOT"/bootstrap/*.py "$PACKAGE_DIR/bootstrap/"
cp "$ROOT/tools/frayc_selfhosted.py" "$ROOT/tools/fray_oracle.py" \
   "$ROOT/tools/run_tests.py" "$ROOT/tools/check_fray_txt.py" \
   "$ROOT/tools/check_tutorial.py" \
   "$PACKAGE_DIR/tools/"
cp "$ROOT/tools/check_cases.sh" "$PACKAGE_DIR/tools/"
cp "$ROOT/tools/frayc.sh" "$ROOT/tools/fray.sh" "$PACKAGE_DIR/tools/"
chmod +x "$PACKAGE_DIR/tools/check_cases.sh" "$PACKAGE_DIR/tools/frayc.sh" \
         "$PACKAGE_DIR/tools/fray.sh"

echo "--- examples, docs, spec, tests ---"
cp -R "$ROOT/examples/." "$PACKAGE_DIR/examples/"
cp "$ROOT/README.md" "$PACKAGE_DIR/" 2>/dev/null || true
cp "$ROOT/CHANGELOG.md" "$PACKAGE_DIR/" 2>/dev/null || true
# The syntax reference the release criterion is written against, and the audit
# that holds the compiler to it (it needs the reference, the case manifest the
# pins are recorded in, and the oracle it diffs against). The reference has
# been carried under more than one name, so ship whichever one is here.
for reference in fray-layout.md fray.txt; do
    [ -f "$ROOT/$reference" ] && cp "$ROOT/$reference" "$PACKAGE_DIR/" || true
done
[ -f "$ROOT/LICENSE" ] && cp "$ROOT/LICENSE" "$PACKAGE_DIR/" || \
    echo "  note: no LICENSE file in the repository"
cp "$ROOT"/docs/*.md "$PACKAGE_DIR/docs/" 2>/dev/null || true
cp "$ROOT"/spec/*.md "$PACKAGE_DIR/spec/" 2>/dev/null || true
cp -R "$ROOT/tests/cases/." "$PACKAGE_DIR/tests/cases/"
cp "$ROOT/tests/selfhosted_supported.txt" "$PACKAGE_DIR/tests/"
cp "$ROOT/tests/hello.fray" "$PACKAGE_DIR/tests/" 2>/dev/null || true
printf '%s\n' "$VERSION" > "$PACKAGE_DIR/VERSION"

# 4. Quick start, in the package's own terms.
cat > "$PACKAGE_DIR/QUICKSTART.md" << EOF
# fray $VERSION — quick start

fray compiles straight to a native binary: no interpreter, no runtime Python,
and no GIL.

This archive is for **Linux x86-64**, the platform fray $VERSION is released,
gated and packaged on. macOS and Windows are not supported yet.

## Requirements

* LLVM's \`llc\` object emitter — the chain looks for \`llc-22\` down to
  \`llc-14\`, or plain \`llc\`, and uses the first one it finds on \`PATH\`
* a C compiler (\`cc\`, \`gcc\` or \`clang\`) to link the runtime

Nothing else — compiling and running a program never invokes Python.

## Run a program

\`\`\`sh
./bin/fray run examples/hello.fray
./bin/fray build examples/fizzbuzz.fray -o fizzbuzz
./fizzbuzz
\`\`\`

\`fray run\` compiles to a temporary binary and runs it; \`fray build\` keeps the
binary. \`fray ir examples/hello.fray\` prints the LLVM IR instead.

## The examples

\`\`\`sh
for f in examples/*.fray; do ./bin/fray run "\$f"; done
\`\`\`

\`examples/\` covers arithmetic, control flow, strings, lists, maps, structs,
enums and \`match\`, exceptions, Option/Result, threads and atomics, coroutines
and modules.

## The compile chain

\`\`\`sh
./bin/frayc program.fray -o program        # bin/fray run, spelled out
./bin/frayc program.fray --root modules    # extra import search directory
\`\`\`

\`bin/frayc\` is a short shell script that runs \`bin/frayc_driver\` to emit IR,
\`llc\` to make an object file, and \`cc\` to link it against \`lib/libfrayrt.a\`.
\`runtime/\` holds the C sources that archive was built from (\`make -C runtime\`
rebuilds it; the shipped archive is what the chain links).

## Tests

\`\`\`sh
./tools/check_cases.sh        # compile and run tests/cases, diff .expected
\`\`\`

\`stdlib/\` holds the standard library — \`import random\` resolves there, from
any working directory (the compiler searches \`stdlib/\` beside itself).

\`fray-layout.md\` is the syntax reference. \`tools/check_fray_txt.py\` audits
the compiler against it: every \`\`\`fray snippet is compiled, run and diffed
against the oracle, the ones that cannot work are recorded with the behaviour
they have, and a snippet the reference marks PROPOSED must still be rejected:

\`\`\`sh
python tools/check_fray_txt.py
\`\`\`

## Documentation

* \`fray-layout.md\` — the syntax reference
* \`docs/fray_by_example.md\` — the tutorial: every language feature, by example
* \`spec/grammar.md\`, \`spec/semantics.md\`, \`spec/phase8-features.md\`

## Rebuilding the compiler

\`bin/frayc_driver\` is the self-hosted compiler: it was compiled by fray from
the sources in \`compiler/\`. To rebuild it from source you need Python 3.11+
with llvmlite — \`bootstrap/\` is the Python compiler that starts the chain:

\`\`\`sh
python tools/frayc_selfhosted.py build compiler/frayc.fray \\
       -o bin/frayc_driver --backend llc
\`\`\`

That path is for porting fray to a new platform or changing the compiler
itself. It is not part of compiling fray programs.
EOF

# 5. Archive.
echo ""
echo "--- archive ---"
cd "$DIST_DIR"
rm -f "$PACKAGE_NAME.tar.gz" "$PACKAGE_NAME.zip"
if [ "$ARCHIVE_EXT" = "tar.gz" ]; then
    tar -czf "$PACKAGE_NAME.tar.gz" "$PACKAGE_NAME"
    ARCHIVE="$DIST_DIR/$PACKAGE_NAME.tar.gz"
else
    if command -v zip >/dev/null 2>&1; then
        zip -qr "$PACKAGE_NAME.zip" "$PACKAGE_NAME"
    else
        # A Windows runner has `python`, not necessarily `python3`, and the
        # whole point of this branch is that packaging works there.
        ZIPPY=""
        for candidate in python3 python; do
            if command -v "$candidate" >/dev/null 2>&1; then ZIPPY="$candidate"; break; fi
        done
        if [ -z "$ZIPPY" ]; then
            echo "package_release.sh: neither zip nor python is available to build the archive" >&2
            exit 1
        fi
        "$ZIPPY" -c "
import os, zipfile
with zipfile.ZipFile('$PACKAGE_NAME.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, _, files in os.walk('$PACKAGE_NAME'):
        for name in files:
            zf.write(os.path.join(root, name))
"
    fi
    ARCHIVE="$DIST_DIR/$PACKAGE_NAME.zip"
fi

echo ""
echo "=== package ready ==="
echo "  directory: $PACKAGE_DIR"
echo "  archive:   $ARCHIVE"
echo ""
echo "Verify it compiles a program with no Python in the loop:"
echo "  tar -xzf $ARCHIVE -C /tmp && cd /tmp/$PACKAGE_NAME"
echo "  ./bin/fray run examples/hello.fray"
