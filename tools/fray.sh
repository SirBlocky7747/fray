#!/bin/sh
# fray — front end for the native fray toolchain.
#
#   fray run PROGRAM.fray [args...]   compile and run in one step
#   fray build PROGRAM.fray [-o OUT]  compile to a native binary
#   fray ir PROGRAM.fray              print the LLVM IR the compiler emits
#   fray version                      print the toolchain version
#
# This is a thin shell over tools/frayc.sh (the compile chain: frayc_driver,
# then llc, then cc). Nothing here is Python.
set -eu

usage() {
    cat <<'USAGE'
usage: fray <command> [options]

commands:
  run PROGRAM.fray [args...]    compile PROGRAM.fray and run it
  build PROGRAM.fray [-o OUT]   compile PROGRAM.fray to a native binary
  ir PROGRAM.fray               print the generated LLVM IR
  version                       print the toolchain version

options (run and ir):
  --root DIR                    extra directory to resolve imports from
  --driver PATH                 use this frayc driver binary
build also accepts -o OUT, --root DIR and --driver PATH.
USAGE
}

die() { printf 'fray: %s\n' "$1" >&2; exit 1; }

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# The compile chain lives in tools/ in a source checkout and in bin/ in a
# release package.
chain=""
for candidate in "$repo_root/tools/frayc.sh" "$repo_root/bin/frayc" "$repo_root/bin/frayc.sh"; do
    if [ -f "$candidate" ]; then chain=$candidate; break; fi
done
[ -n "$chain" ] || die "cannot find the frayc compile chain under $repo_root"

# The compiler binary: a development build lives in build/, a release package
# ships it next to this script.
find_driver() {
    if [ -n "${driver:-}" ]; then printf '%s\n' "$driver"; return; fi
    if [ -n "${FRAYC_DRIVER:-}" ]; then printf '%s\n' "$FRAYC_DRIVER"; return; fi
    for candidate in "$repo_root/build/frayc_driver" "$repo_root/bin/frayc_driver" "$repo_root/bin/frayc"; do
        if [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return; fi
    done
    die "no frayc driver found under $repo_root — pass --driver PATH or set \$FRAYC_DRIVER"
}

read_version() {
    if [ -f "$repo_root/VERSION" ]; then
        tr -d '[:space:]' < "$repo_root/VERSION"
    else
        printf '0.0.0-dev\n'
    fi
}

# Split leading options from the source path and the program's own arguments.
driver=""
extra_root=""
cmd_source=""
rest=""
parse() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --driver) [ $# -ge 2 ] || die "--driver needs a path"; driver=$2; shift 2 ;;
            --root) [ $# -ge 2 ] || die "--root needs a directory"; extra_root=$2; shift 2 ;;
            --) shift; break ;;
            -*) die "unknown option '$1' for this command" ;;
            *) break ;;
        esac
    done
    [ $# -ge 1 ] || { usage >&2; exit 2; }
    cmd_source=$1; shift
    rest=$*
}

cmd_run() {
    parse "$@"
    [ -f "$cmd_source" ] || die "no such file: $cmd_source"
    tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/fray.XXXXXX")
    trap 'rm -rf "$tmp_dir"' EXIT INT TERM
    bin="$tmp_dir/$(basename "${cmd_source%.fray}")"
    # shellcheck disable=SC2086
    set -- "$chain" "$cmd_source" -o "$bin"
    [ -n "$driver" ] && set -- "$@" --driver "$driver"
    [ -n "$extra_root" ] && set -- "$@" --root "$extra_root"
    "$@" >/dev/null
    if [ -n "$rest" ]; then
        # shellcheck disable=SC2086
        "$bin" $rest
    else
        "$bin"
    fi
}

cmd_build() {
    parse "$@"
    # shellcheck disable=SC2086
    set -- "$chain" "$cmd_source"
    [ -n "$rest" ] && set -- "$@" $rest
    [ -n "$driver" ] && set -- "$@" --driver "$driver"
    [ -n "$extra_root" ] && set -- "$@" --root "$extra_root"
    "$@"
}

cmd_ir() {
    parse "$@"
    [ -f "$cmd_source" ] || die "no such file: $cmd_source"
    d=$(find_driver)
    if [ -n "$extra_root" ]; then
        out=$("$d" "$cmd_source" "$extra_root") || { printf '%s\n' "$out"; die "compilation failed"; }
    else
        out=$("$d" "$cmd_source") || { printf '%s\n' "$out"; die "compilation failed"; }
    fi
    ir=$(printf '%s\n' "$out" | sed -n '/^===IR_START===/,/^===IR_END===/p' | sed '1d;$d')
    [ -n "$ir" ] || die "the driver produced no IR"
    printf '%s\n' "$ir"
}

[ $# -ge 1 ] || { usage >&2; exit 2; }
command=$1
shift

case "$command" in
    run) cmd_run "$@" ;;
    build) cmd_build "$@" ;;
    ir) cmd_ir "$@" ;;
    version) printf 'fray %s\n' "$(read_version)" ;;
    -h|--help|help) usage ;;
    *) die "unknown command '$command' (try: fray help)" ;;
esac
