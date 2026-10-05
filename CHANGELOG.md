# Changelog

All notable changes to fray are recorded here.

## 0.1.0 — first release

**Linux x86-64 only.** This is the platform fray is developed, gated and
packaged on, and the only one whose archive has been built, extracted and run
end to end. `macOS` and `Windows` are unproven and unsupported for this
release: the compiler build fails on the macOS SDK, and the Windows runner
cannot locate `llc` after installing it. Both remain in CI as non-blocking legs
so the day either is fixed it says so.

### Added

- **Self-hosted compiler at a fixed point.** `compiler/frayc.fray` compiles a
  program end to end — lexing, parsing, linking, semantic analysis, codegen —
  and emits LLVM IR text. Rebuilding it produces a bit-identical v3, and it
  compiles all 49 golden cases.
- **Release archive with no Python in the compile loop.**
  `tools/package_release.sh` produces `fray-0.1.0-linux-x86_64.tar.gz`
  carrying the compiler, the runtime, the `fray` front end, examples and docs.
  The chain is `frayc_driver` → `llc` → `cc`.
- **Pure-fray standard library**, with `stdlib/` as a search root of its own so
  `import random` resolves from any directory.
- **`fray run` / `build` / `ir` / `version`** as the user-facing front end.
- **Memory gate over the concurrent programs.** valgrind memcheck now runs
  every program that uses threads, coroutines, channels or sockets, ten times
  each — the cross-thread lifetime errors ASan provably cannot see.
- **`--strict` on `benchmarks/run_io_benchmarks.py`**, which makes the five
  Phase 7 done-when criteria gate instead of being reported only.
- **A replayable language.** `fray run` compiles to a temporary binary and runs
  it; `fray build` keeps the binary; `fray ir` prints the IR.

### Fixed

- **Use-after-free in the coroutine scheduler.** `loop_push_ready` stored a
  coroutine's phase after releasing the loop lock it had just been queued
  under, so an I/O completion could write 4 bytes into a freed `Coro` while the
  loop thread was finishing it — 8 runs in 10 of `benchmarks/io_socket_coro.fray`
  under valgrind. Found only by running the packaged compiler under valgrind.
- **Leak in function-local rebinding.** A local rebound in a loop overwrote its
  slot without releasing the previous value, orphaning every value but the last.
- **A parked GC space was overwritten** instead of reused.
- **Memory gate scored dead programs clean.** `io_file_*` had no fixtures, so
  they died before producing any sanitizer output; and a wrong exit status was
  not treated as a failure.
- **Release archive could not be untarred.** `tar -C` does not create its target
  directory; `unzip -d` does.
- **A runtime source that failed to compile printed no diagnostic.**
- **File benchmarks measured memcpy, not I/O.** The fixture was written and read
  back immediately, so every read was a page-cache hit. Reads are now cold:
  `coroutines beat sequential blocking I/O` moved 0.62x → 2.01x.
- **The REPL swallowed 25 builtins.** `len(xs)`, `range(3)`, `Ok(5)` and the
  rest printed nothing at all.
- **No second LLVM fallback**, which was quietly re-checking frontend output
  against a more lenient LLVM and going green on IR the native chain could not
  build.

### Known issues

Stated plainly rather than tuned away. All are measured, not estimated.

- **Coroutines are 0.12x of a thread-per-file** on cold file reads
  (`benchmarks/run_io_benchmarks.py --strict` reports it). Pre-existing; the
  scheduler has not been tuned for it. Coroutines do beat sequential blocking
  I/O (2.01x) and beat async-Python on both file and socket workloads
  (3.3x, 5.5x).
- **macOS and Windows are unsupported.** The compiler fails to build on the
  macOS SDK; the Windows runner cannot find `llc`. Neither cause is known.
- **AddressSanitizer cannot detect cross-thread lifetime bugs in this
  runtime.** Measured: 0 reports in 240 runs across six `ASAN_OPTIONS` settings
  against a known-bad runtime, while a deterministic free-then-read control in
  the same binary was reported at once. The window is a couple of instructions
  wide and never interleaves at native speed. The valgrind leg covers the
  concurrent programs; it is probabilistic, at roughly one missed run in ten
  thousand for `io_socket_coro.fray`.
- **The benchmark runner's own `done-when` criteria are not enforced** by
  default. One of the five fails for a real reason (see above), so `--strict` is
  deliberately not wired into `gates.sh`.

### Verification at 0.1.0

- `bash build/gates.sh` — 9 gates, `GATES_EXIT=0`, 0 failures
- `tools/check_memory.py --all --keep-going` — 60/60 clean under ASan
- valgrind memcheck — 10 concurrent programs × 10 runs, clean
- `make -C runtime asan` — 19 tests
- `tools/check_cases.sh` — 49 passed / 0 failed, through the packaged front end
- all 12 examples run through the extracted archive
- `benchmarks/io_socket_coro.fray` built by the packaged compiler: 10/10 runs
  printing `32`, valgrind clean