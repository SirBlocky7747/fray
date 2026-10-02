#!/bin/sh
# check_cases.sh — run the golden cases in tests/cases through the native
# compile chain and diff each program's output against its .expected file.
#
#   tools/check_cases.sh [name-substring]
#
# Case format (see tests/cases):
#   foo.fray      the program
#   foo.expected  expected stdout
#   foo.exit      expected exit status (optional, default 0)
#
# This is the release's own test runner: sh, the compiler and a C compiler,
# with no Python anywhere in the loop.
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cases_dir="$repo_root/tests/cases"

chain=""
for candidate in "$repo_root/tools/frayc.sh" "$repo_root/bin/frayc.sh"; do
    if [ -f "$candidate" ]; then chain=$candidate; break; fi
done
[ -n "$chain" ] || { printf 'check_cases.sh: cannot find the frayc compile chain\n' >&2; exit 1; }

filter=${1:-}
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/fray_cases.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT INT TERM

passed=0
failed=0
failures=""

for src in "$cases_dir"/*.fray; do
    base=$(basename "$src" .fray)
    case "$base" in
        *"$filter"*) ;;
        *) continue ;;
    esac
    expected="$cases_dir/$base.expected"
    [ -f "$expected" ] || continue

    want_rc=0
    if [ -f "$cases_dir/$base.exit" ]; then
        want_rc=$(tr -d '[:space:]' < "$cases_dir/$base.exit")
    fi

    exe="$tmp_dir/$base"
    if ! "$chain" "$src" -o "$exe" > "$tmp_dir/$base.build.log" 2>&1; then
        failed=$((failed + 1))
        failures="$failures $base(compile)"
        printf '  FAIL  %s (compile)\n' "$base"
        sed 's/^/        /' "$tmp_dir/$base.build.log"
        continue
    fi

    rc=0
    "$exe" > "$tmp_dir/$base.out" 2> "$tmp_dir/$base.err" || rc=$?

    if [ "$rc" != "$want_rc" ]; then
        failed=$((failed + 1))
        failures="$failures $base(exit $rc, want $want_rc)"
        printf '  FAIL  %s (exit code %s, expected %s)\n' "$base" "$rc" "$want_rc"
        sed 's/^/        /' "$tmp_dir/$base.err"
        continue
    fi

    if [ "$(cat "$tmp_dir/$base.out")" != "$(cat "$expected")" ]; then
        failed=$((failed + 1))
        failures="$failures $base(output)"
        printf '  FAIL  %s (output)\n' "$base"
        diff -u "$expected" "$tmp_dir/$base.out" | sed 's/^/        /' || true
        continue
    fi

    passed=$((passed + 1))
done

printf '\nResults: %s passed, %s failed\n' "$passed" "$failed"
if [ "$failed" -gt 0 ]; then
    printf 'Failing cases:%s\n' "$failures"
    exit 1
fi
