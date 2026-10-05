# fray

A Python-flavored, GIL-free, AOT-compiled programming language with first-class math and machine learning.

**fray** aims to have the ease-of-use of Python with true parallelism (no GIL) and a built-in tensor core + autodiff engine for ML workloads — all compiled to native code via LLVM.

## Status

**Self-hosted, and at a fixed point.** The compiler is `compiler/frayc.fray`: a native
binary that compiles a program read from argv — lexing, parsing, linking, semantic
analysis and codegen all happen inside it — and prints LLVM IR text for an object
emitter and a C compiler to finish. v2 rebuilds the compiler to a bit-identical v3,and it compiles all 49 golden cases. Python's remaining role is the Stage 0/1 bootstrap: it
builds the first native compiler and hosts the oracle the tests diff against.

`stdlib/` is a search root of its own, after the program's own directory, so
`import random` resolves from any working directory — `stdlib/random.fray` is a pure-fray
31-bit LCG with `seed`, `randomInt`, `randomFloat`, `choice` and `shuffle`.

`tools/package_release.sh` turns that into a downloadable Linux x86-64 archive —
the compiler binary, the runtime, the `fray` front end, the examples and the docs,
with nothing in the compile loop but `frayc_driver` → `llc` → `cc`.

Two things are measured and not yet good enough, both stated here rather than
papered over: the coroutine file path reaches **0.12x** of a thread-per-file on
cold reads (`benchmarks/run_io_benchmarks.py --strict` reports it), and
AddressSanitizer alone cannot see cross-thread lifetime bugs in this runtime,
which is why the memory gate also memchecks the concurrent programs.

See `plan.md` for the full build plan and `fray-layout.md` for the syntax reference.

### Verifying a release

Each release publishes six files: the archive, `SHA256SUMS`, `SHA256SUMS.asc`,
the signing key as `fray-release-key.asc`, and a SLSA v0.2 provenance
attestation as `PROVENANCE.json` and `PROVENANCE.md`.

```bash
sha256sum -c SHA256SUMS                                 # the digest matches
gpg --verify SHA256SUMS.asc SHA256SUMS                  # the signature is genuine
```

The signature is made by a 4096-bit RSA key under the uid
`The fray authors <fray-authors@users.noreply.github.com>` — the same identity
this repository's commits and tag signatures use:

```
29A4 4B56 DA1D ABA0 208E  9FFC D616 E3CC 417B F393
```

`gpg --verify` proves the signature matches *a* key with that fingerprint. It
does not prove the key is the one above, so compare the fingerprint against
this file — which is versioned in the repository alongside the tag — rather than
against whatever the download page happens to say.

The key is deliberately **not** published to a public keyserver, because doing
so emails the uid to every subscriber of that keyserver. Fetch the public key
from this repository instead:

```bash
curl -sLO https://github.com/SirBlocky7747/fray/releases/download/v0.1.0/fray-release-key.asc
gpg --import fray-release-key.asc
```

The archive is bit-reproducible: the member order, timestamps, ownership and
gzip header are all pinned, and the timestamp is the commit being packaged, so
rebuilding from the same commit produces the same digest. You can recompute it
rather than trusting the published one:

```bash
git checkout v0.1.0 && bash tools/package_release.sh && sha256sum dist/*.tar.gz
```

## Quick start

```bash
# 1. Build the compiler itself (once) with the Python Stage 0/1 toolchain
.venv/bin/python tools/frayc_selfhosted.py build compiler/frayc.fray -o build/frayc_driver

# 2. Compile and run a program: driver → llc → cc, no Python in the loop
./tools/fray.sh run examples/hello.fray       # compile and run in one step
./tools/fray.sh build program.fray -o program  # or keep the binary
./program
./tools/fray.sh ir program.fray               # or just look at the LLVM IR

# 3. Package a release (compiler + runtime + examples + docs)
bash tools/package_release.sh
#   dist/fray-0.1.0-linux-x86_64.tar.gz — extract it and:
#   ./bin/fray run examples/hello.fray

# Or run through the Stage 0/1 pipeline when debugging the frontend
.venv/bin/python tools/frayc_selfhosted.py run program.fray
```

## Project layout

```
fray/
├── spec/           Language spec: grammar (EBNF), semantics, design decisions
├── bootstrap/      Stage 0 Python toolchain (temporary — archived after self-hosting)
├── runtime/        libfrayrt (C99): object model, containers, threads, tensor kernels
├── compiler/       frayc.fray — the self-hosted compiler (Phase 8+)
├── stdlib/         .fray standard library modules (random.fray), an import root
                  of its own — the program's own directory still wins a clash
├── tests/          Golden tests, differential tests, stress tests
├── examples/       Example fray programs
└── tools/          Test runner, bootstrap scripts, release packaging
```

## Key design decisions

- **Bootstrap in Python, self-hosted before v0.1** — the first release binary is compiled by fray from fray source
- **LLVM/AOT** — no bytecode VM, no JIT; compile directly to native executables
- **Hybrid concurrency** — real shared-memory threads (GIL-free) + lightweight coroutines for I/O
- **Tensor core + autodiff** — CPU-first with SIMD; GPU backend in a later phase
- **Dynamic typing with static specialization** — fast paths compiled monomorphically, boxed fallback where types can't be proven

## Building

v0.1.0 ships for **Linux x86-64 only**. That is the platform it is developed,
gated and packaged on, and the only one whose archive has been built, extracted
and run end to end. macOS and Windows are not supported yet: `package_release.sh`
still has branches for both, and the source builds there, but no release has
been verified on either — the compiler build fails on the macOS SDK, and the
Windows runner cannot locate `llc` after installing it. Treat them as
unproven, not as working. The 0.1.0 artifact is
`fray-0.1.0-linux-x86_64.tar.gz`.

Developed on Linux Mint 22 / Ubuntu 24.04 (glibc, ext4).

### Requirements

- **LLVM — required.** `llc` is the project's only object emitter. The release
  chain, the Stage 0/1 bootstrap and the test gates all go through it, and it
  is also what decides whether emitted IR is valid. There is no second backend
  to fall back to, which is deliberate: a fallback was quietly re-checking
  frontend output against a second, more lenient LLVM and going green on IR the
  native chain could not build. Install `llvm-14` from your distribution, or a
  current release from [apt.llvm.org](https://apt.llvm.org/).
- **A C compiler and make** — to build and link `libfrayrt`.
- **Python 3.11+** — for the Stage 0/1 bootstrap and the gates. The release
  compile loop (`frayc_driver` → `llc` → `cc`) has no Python in it at all.
- **llvmlite — currently required, and only by one file.** `bootstrap/codegen.py`
  still builds LLVM IR through llvmlite's IR builder. Nothing else needs it: the
  self-hosted compiler already emits IR as text, and the driver hands that text
  straight to `llc`. That builder is mid-way through being replaced with text
  emission; once it is, this line disappears and Python alone builds the first
  compiler. Treat it as a dependency on its way out rather than a recommendation.

```bash
# 1. LLVM — required. Every object this project produces is emitted by LLVM's
#    own `llc`: the release chain, the Stage 0/1 bootstrap and the test gates all
#    go through it, and it is also what decides whether emitted IR is valid.
#    Ubuntu 24.04 ships 14 in universe; current releases come from apt.llvm.org.
sudo apt-get install -y llvm-14

# 2. Python 3.11+ (Mint/Ubuntu: may need `sudo apt install python3-venv`)
python3 -m venv .venv
.venv/bin/pip install llvmlite   # bootstrap/codegen.py only — see Requirements

# 3. Build the C runtime (gcc, make)
make -C runtime

# 4. Compile and run a program (Stage 0 Python toolchain)
.venv/bin/python bootstrap/frayc.py build program.fray -o program
./program

# Or the self-hosted compiler pipeline
.venv/bin/python tools/frayc_selfhosted.py build program.fray -o program

# The same thing natively, with no Python in the compile loop: the compiler
# binary (built above) emits LLVM IR, `llc` emits the object, `cc` links the
# runtime. tools/frayc.sh is that chain in one command, and tools/fray.sh
# wraps it (`fray run`, `fray build`, `fray ir`).
./tools/frayc.sh program.fray -o program
./tools/fray.sh run program.fray
```

## Testing

```bash
# The golden cases through the native chain, with no Python in it:
# every tests/cases/*.fray is compiled by the driver, run, and diffed against
# its .expected file (and its .exit status, when it has one)
./tools/check_cases.sh

# The syntax reference against the compiler: every ```fray snippet in
# fray-layout.md is compiled, run and diffed against the oracle, or recorded
.venv/bin/python tools/check_fray_txt.py

# C runtime tests (refcounting, cycle collector, threads, coroutines)
make -C runtime test

# runtime.h must agree with the runtime library in both directions: every
# non-inline fray_* function it declares is defined in one of the library's
# translation units, and every file-scope fray_* function those units define is
# declared in it. The units come from runtime/Makefile's RUNTIME_SOURCES — the
# same list the check below validates — so a translation unit that stops being
# built takes its symbols with it. Each is compiled and its symbols read with
# nm, so the check sees what the linker sees: a stale prototype is a link error
# waiting for its first caller, and an undeclared global is API that leaked out
# of a translation unit. The other end of the same contract: every runtime
# entry point the bootstrap and the self-hosted emitter call must exist in the
# library, and every literal call in the bootstrap must be one its own
# RUNTIME_FUNCS declares — a rename there is otherwise invisible until a
# program fails to link.
.venv/bin/python tools/check_runtime_symbols.py

# The library is described twice — runtime/Makefile's RUNTIME_SOURCES and the
# bootstrap's — so every runtime/*.c that is not a program (test.c and
# coro_drv.c define main) must be in both lists, and every entry in either
# must exist. A translation unit in one list and not the other links in some
# builds and not others.
.venv/bin/python tools/check_runtime_sources.py

# Full golden suite — every case runs through the oracle AND the
# compiled binary; both must match the expected output, and the status in
# the optional <case>.exit file (a case that ends in an unhandled exception
# has to stop both engines the same way, not just print the same words)
.venv/bin/python tools/run_tests.py -v

# Self-hosted frontend gate — the self-hosted compiler implements a subset
# of the language, and this pins the invariants that keep the subset honest:
# it always terminates, syntax it cannot compile is reported as a diagnostic
# (never a hang or an internal error), and what it does compile matches
# .expected. --probes runs a matrix of unsupported syntax.
.venv/bin/python tools/check_frontend.py --probes
.venv/bin/python tools/check_frontend.py --run

# Stage 1 of self-hosting: the self-hosted compiler compiles its own four
# modules to LLVM-verified IR, builds and runs them, and compares the compiled
# modules that have a driver against the oracle.
.venv/bin/python tools/check_frontend.py --stage1

# Stage 2 and the fixed point: v1 compiles frayc.fray -> IR1 -> v2 compiles it
# -> IR2, then IR2 -> v3 -> IR3. Requires IR1 == IR2 (Stage 2), IR2 == IR3 and
# bit-identical v2/v3 binaries (the release criterion), then runs the golden
# suite through v2. `--driver` reuses an existing v1 instead of building one,
# which turns a ~10 minute gate into a ~5 minute one.
.venv/bin/python tools/check_frontend.py --stage2 --driver build/frayc_driver

# The golden suite through a driver binary: the host's only job is to emit the
# object and link, so everything the gate measures was produced natively.
.venv/bin/python tools/check_frontend.py --native-gate build/frayc_driver

# The compiler modules as ONE binary: compiler/main.fray imports lexer,
# parser, sema, codegen and link, and the self-hosted compiler resolves those
# imports itself, renaming the names they share (`get_errors`, `clear_errors`,
# `node_type`, `node_get` and the private `errors` list) to `<module>_<name>` —
# no host-side text mangling. This builds that program, links it, runs it and
# diffs its output against the oracle.
.venv/bin/python tools/check_frontend.py --single-unit
# build the same program yourself:
.venv/bin/python tools/frayc_selfhosted.py build compiler/main.fray -o frayc

# Dotted module paths resolve into packages: `import a.b` is a/b.fray under
# the program's root, and a.b.f() / from a.b import c resolve through it. A
# directory with an initializer (pkg/__init__.fray) is a package whose
# initializer names it and can re-export names from sibling modules with
# relative imports (`from .util import twice`); `from . import util` binds a
# submodule, and `..util` climbs to the parent package.
.venv/bin/python tools/check_frontend.py --packages

# Compiler unit tests
.venv/bin/python bootstrap/tests/test_inference.py
.venv/bin/python bootstrap/test_parse.py
.venv/bin/python bootstrap/test_eval.py
.venv/bin/python bootstrap/test_codegen.py

# Benchmarks (regression gate vs benchmarks/baseline.json)
.venv/bin/python benchmarks/run_benchmarks.py --baseline benchmarks/baseline.json

# Same suite under AddressSanitizer — a gate, not a report
make -C runtime asan

# Leak + memory-error check for compiled programs: builds each one against
# an ASan runtime and fails if it leaks. `--all` adds every golden case;
# `--driver BIN` compiles them with the self-hosted compiler instead of the
# Python emitter, which is the same gate for the native codegen.
.venv/bin/python tools/check_memory.py --all
.venv/bin/python tools/check_memory.py --driver build/frayc_driver
```

Every compiled program is expected to run leak-free. The runtime owns a
program's memory, so a leak there scales with the workload — the check is the
reason `list_sum` sums a million boxed ints without leaving one behind. The
`--driver` form is the one that keeps the two emitters honest about the
owner-ship rules they share (a callee owns its parameters, a result is owned by
the caller, an argument list owns what it carries): it found four leaks in the
self-hosted emitter that the golden suite could not see, because a leak never
changes what a short program prints. All 49 golden cases are clean through the
native driver now.

## License

MIT — see [LICENSE](LICENSE).
