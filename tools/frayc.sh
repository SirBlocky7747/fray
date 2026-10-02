#!/bin/sh
# frayc.sh — compile a fray program with the native (Stage 2) compiler.
#
#   tools/frayc.sh program.fray [-o program] [--root DIR] [--driver PATH]
#
# Nothing in this chain is Python:
#
#   build/frayc_driver    the compiler itself: a native binary compiled by fray
#                         from compiler/frayc.fray (tools/check_frontend.py
#                         --stage2 builds and verifies the fixed point)
#   llc                   LLVM's own object emitter, fed the driver's IR
#   cc                    links the object against libfrayrt
#
# The driver walks the source's directory (plus --root) itself to resolve
# imports, so there is no host-side module catalog here either.
#
# The Python driver (tools/frayc_selfhosted.py) still exists for Stage 0/1: it
# is what builds the first native compiler and what the test gates orchestrate.
# This script is the compile loop the release ships, with Python out of it.
set -eu

usage() {
    cat <<'USAGE'
usage: tools/frayc.sh program.fray [-o program] [--root DIR] [--driver PATH]

  -o OUT        output binary (default: the source without its extension)
  --root DIR    extra directory to resolve imports from (the driver also
                searches the source's own directory)
  --driver PATH the frayc driver binary (default: build/frayc_driver,
                then bin/frayc_driver, or $FRAYC_DRIVER)
USAGE
}

die() { printf 'frayc.sh: %s\n' "$1" >&2; exit 1; }

source_file=""
output=""
extra_root=""
driver=""
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# The standard library: a search root of its own, after the program's directory
# and any --root, so `import random` resolves from anywhere. The layout is
# stdlib/ at the repository or package root; $FRAY_STDLIB overrides it. A
# checkout without one simply adds no modules.
stdlib_root=${FRAY_STDLIB:-$repo_root/stdlib}

while [ $# -gt 0 ]; do
    case "$1" in
        -o) [ $# -ge 2 ] || die "-o needs a path"; output=$2; shift 2 ;;
        --root) [ $# -ge 2 ] || die "--root needs a directory"; extra_root=$2; shift 2 ;;
        --driver) [ $# -ge 2 ] || die "--driver needs a path"; driver=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option '$1'" ;;
        *) [ -n "$source_file" ] && die "only one source file is supported"; source_file=$1; shift ;;
    esac
done

[ -n "$source_file" ] || { usage; exit 2; }
[ -f "$source_file" ] || die "no such file: $source_file"

# Locate the driver: --driver, then $FRAYC_DRIVER, then the development build
# output, then the layout a release package ships (bin/frayc_driver, so the
# package carries its compiler next to the `fray` front end that drives it).
[ -n "$driver" ] || driver=${FRAYC_DRIVER:-}
if [ -z "$driver" ]; then
    for candidate in "$repo_root/build/frayc_driver" "$repo_root/bin/frayc_driver"; do
        if [ -x "$candidate" ]; then driver=$candidate; break; fi
    done
fi
[ -n "$driver" ] || die "no frayc driver found under $repo_root — pass --driver PATH or
set \$FRAYC_DRIVER, or build one with:
  python tools/frayc_selfhosted.py build compiler/frayc.fray -o build/frayc_driver"
[ -x "$driver" ] || die "no frayc driver at $driver"

# Locate LLVM's object emitter (llc, or a versioned one).
llc=""
for candidate in llc llc-18 llc-17 llc-16 llc-15 llc-14; do
    if command -v "$candidate" >/dev/null 2>&1; then llc=$candidate; break; fi
done
[ -n "$llc" ] || die "llc not found — install LLVM (the object emitter for the native chain)"

cc=${CC:-}
if [ -z "$cc" ]; then
    for candidate in cc gcc clang; do
        if command -v "$candidate" >/dev/null 2>&1; then cc=$candidate; break; fi
    done
fi
[ -n "$cc" ] || die "no C compiler found (need cc, gcc or clang to link libfrayrt)"

[ -n "$output" ] || output=${source_file%.fray}

# 1. The driver compiles the program and prints LLVM IR between markers. Its
#    diagnostics share stdout, so a failed compile is reported verbatim.
tmp_ir=$(mktemp "${TMPDIR:-/tmp}/frayc.XXXXXX.ll")
tmp_obj=$(mktemp "${TMPDIR:-/tmp}/frayc.XXXXXX.o")
trap 'rm -f "$tmp_ir" "$tmp_obj"' EXIT INT TERM

# The driver's import search roots: the program's own directory (always),
# then --root, then the standard library. The driver also looks for stdlib/
# beside itself, so naming it here is what keeps this chain working when the
# driver binary has been moved (--driver / $FRAYC_DRIVER).
set -- "$driver" "$source_file"
[ -n "$extra_root" ] && set -- "$@" "$extra_root"
[ -d "$stdlib_root" ] && set -- "$@" "$stdlib_root"
driver_out=$("$@") || {
    printf '%s\n' "$driver_out"; die "compilation failed"
}

printf '%s\n' "$driver_out" \
    | sed -n '/^===IR_START===/,/^===IR_END===/p' | sed '1d;$d' > "$tmp_ir"
[ -s "$tmp_ir" ] || die "the driver produced no IR"

# 2. IR → object, with LLVM's own emitter.
"$llc" -filetype=obj "$tmp_ir" -o "$tmp_obj"

# 3. Object → binary, against the runtime library. A release package ships
#    lib/libfrayrt.a ready to link, which is also the case that keeps the
#    package directory read-only (only $TMPDIR gets written). A source checkout
#    has no lib/, so the archive is rebuilt from runtime/ when make is around.
libfrayrt=""
if [ -f "$repo_root/lib/libfrayrt.a" ]; then
    libfrayrt="$repo_root/lib/libfrayrt.a"
elif [ -f "$repo_root/runtime/Makefile" ] && command -v make >/dev/null 2>&1; then
    make -s -C "$repo_root/runtime" libfrayrt.a
    libfrayrt="$repo_root/runtime/libfrayrt.a"
elif [ -f "$repo_root/runtime/libfrayrt.a" ]; then
    libfrayrt="$repo_root/runtime/libfrayrt.a"
else
    die "no runtime library found under $repo_root (expected lib/libfrayrt.a or runtime/libfrayrt.a)"
fi
case "$(uname -s)" in
    Linux)  "$cc" -o "$output" "$tmp_obj" "$libfrayrt" -no-pie -lm -lpthread ;;
    Darwin) "$cc" -o "$output" "$tmp_obj" "$libfrayrt" -lm -lpthread ;;
    *)      "$cc" -o "$output" "$tmp_obj" "$libfrayrt" -lm ;;
esac
printf '%s\n' "$output"
