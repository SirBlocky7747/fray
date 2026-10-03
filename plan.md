# fray — Build Plan

**fray** is a Python-flavored, GIL-free, AOT-compiled language with first-class math and
machine learning built in. Syntax reference: `fray.txt`.

---

## 1. Locked-in decisions

| Decision | Choice |
|---|---|
| Bootstrap | The whole toolchain starts in **Python** — temporary scaffolding only |
| Self-hosting | **Mandatory before the first downloadable release**: the shipped binary is compiled *by fray, from fray source* |
| Execution | **LLVM / AOT compiler** → native executables (no bytecode runtime, no JIT in v1) |
| Concurrency | **Hybrid**: real shared-memory threads (GIL-free) + lightweight coroutines for I/O-bound work |
| Math/ML | **Tensor core + reverse-mode autodiff, CPU-first** (SIMD); GPU backend in a later phase |
| Runtime library | C99 — the one place Python can't go, since AOT binaries need a *native* runtime; progressively rewritten in fray after self-hosting *(default I chose — change if you disagree)* |

### The bootstrap chain (why Python doesn't stick around)

```
Stage 0   frayc-py   (Python + llvmlite)     compiles .fray → native
Stage 1   frayc.fray   (written in fray)    compiled by Stage 0 → native binary
Stage 2   frayc-v1   recompiles frayc.fray with itself → frayc-v2
Release   ship Stage 2  after verifying v2 compiles the compiler to a bit-identical v3 (fixed point)
```

From the first release onward, all compiler development happens in fray; the Python
toolchain is archived and kept only as an emergency rebuild path.

---

## 2. Open questions found in fray.txt

Resolved with proposed defaults — confirm or override any of them:

| # | Question | Proposed default |
|---|---|---|
| 1 | `x.depend` after `x.append(1)` on lists/sets — what is it? | Appears to mean "remove last element" (like `pop()`); keeping the name `depend` until you say otherwise |
| 2 | `med(x)` "middle number in general" vs `mid(x)` "middle number IN LIST" | `med` = statistical median (sorts the values), `mid` = positional middle element of the list as-is |
| 3 | `x^(2)` means power (Python uses `**`, and `^` means XOR) | `^` = power; `xor`/`xnor` are keywords — matches your spec |
| 4 | Sets use `x.append(1)` (Python uses `add`) | Keep `append` for all growable containers — consistent with your spec |
| 5 | `{1,2,3,4}` is labeled "random:" | Treated as a **set** (unordered, deduplicated) — matches the braces |
| 6 | Integer model not specified | **64-bit default, auto-promote to bignum on overflow** — fast path for small ints, no surprise errors |
| 7 | No dicts/maps, string methods, or classes in the spec | Planned for later phases (see Phase 4 and Phase 13); syntax to be decided |
| 8 | No async syntax in the spec | Propose `async def` / `await`, Python-style, in Phase 7 |
| 9 | `print(x)` where `x` is a function prints the function itself in your example | Assume you meant `print(x())` — normal call semantics |

---

## 3. Architecture

### Compiler pipeline

```
source .fray → Lexer → Tokens → Parser → AST → Semantic analysis
           → fray IR (SSA) → type inference & specialization
           → LLVM IR (llvmlite in Stage 0, .ll text when self-hosted)
           → object code → link with libfrayrt → native executable
```

- Own IR between AST and LLVM, so the self-hosted compiler never needs LLVM's C++ internals —
  it just emits `.ll` text.
- **Dynamic typing with static specialization**: dynamic by default like Python; a
  whole-program type-inference pass compiles monomorphic call sites to native code, with boxed
  fallback paths where types can't be proven. This is the core performance strategy for a
  dynamic AOT language.

### Runtime (`libfrayrt`, C99)

- Object model: boxed values, type descriptors, **atomic reference counts + cycle collector**
  (object header laid out so a tracing GC can replace it later without breaking ABI)
- Data structures: list, tuple, set, string (UTF-8), dict (later), tensor
- Concurrency: OS threads, per-object thin locks (biased locking in codegen), coroutine
  scheduler with per-thread event loops
- Math/ML: SIMD kernels (scalar → SSE/AVX/NEON), broadcasting, autodiff tape

### Repo layout

```
fray/
├── spec/           language spec: grammar, semantics, decisions
├── bootstrap/      Stage 0 Python toolchain (lexer, parser, sema, IR, llvmlite codegen)
├── runtime/        libfrayrt (C99): object model, containers, threads, tensor kernels
├── compiler/       frayc.fray — the self-hosted compiler (Phase 8+)
├── stdlib/         .fray standard library modules
├── tests/          golden, differential, bootstrap fixed-point, gradcheck, stress
├── examples/
└── tools/          test runner, bootstrap scripts, release packaging
```

---

## 4. Phases

### Phase 0 — Foundations
- [ ] Create the repo layout above; README
- [ ] Write `spec/grammar.md`: full EBNF grammar derived from `fray.txt`
- [ ] Write `spec/semantics.md`: scoping, `const` rules, truthiness, numeric model, error types
- [ ] Set up CI that runs the test suite on every commit
- [ ] Build the test harness: `.fray` test files with expected stdout + golden-test runner

**Done when:** grammar + semantics docs exist and CI runs an (empty) suite green.

### Phase 1 — Frontend (Python)
- [ ] Lexer: all tokens from the spec — `#` comments (full-line and inline), `= += -= *= /=`,
      `== != > < >= <=`, `+ - * / // % ^`, `and or xor xnor not`, `def return if elif else for
      in while try except finally const import`, `True False`, identifiers, int/float/string
      literals, `( ) [ ] { } , : .`
- [ ] Parser → AST: indentation-based blocks (4 spaces, per spec), every statement form in `fray.txt`
- [ ] AST round-trip printer (parse → print → parse must be identical) as the first correctness test
- [ ] Semantic analysis: name resolution, `const` enforcement, `return`/`break` validity, exception-type checking in `except TypeError`
- [ ] Error reporting with line/column for every failure

**Done when:** every snippet in `fray.txt` parses cleanly and round-trips.

### Phase 2 — Reference evaluator (test oracle only)
- [ ] Tree-walking evaluator in Python — **not the product** (you chose AOT); it exists only as
      the semantics referee for differential testing
- [ ] Implements 100% of the spec: containers, `xor`/`xnor`, `^`, `//`, exceptions, stats builtins
- [ ] Differential mode: run the same `.fray` file through oracle and compiled binary, diff stdout

**Done when:** the oracle passes the full golden suite.

### Phase 3 — Codegen v0: "Hello, fray!" as a native binary
- [ ] fray IR design (SSA, basic blocks, explicit types at the box level)
- [ ] llvmlite backend: ints, floats, strings, arithmetic (`+ - * / // % ^`), comparisons,
      `if/elif/else`, `for`/`while`, `def`/calls/`return`, `print`, `const`
- [ ] Runtime skeleton in C: value boxing, `print`, string repr, startup/teardown
- [ ] Link native executables (clang/lld)
- [ ] First end-to-end test: compile → run → diff against oracle output

**Done when:** `frayc-py hello.fray -o hello && ./hello` prints correct output on CI (Linux/macOS/Windows).

### Phase 4 — Full core language AOT (everything in fray.txt)
- [ ] Lists, tuples, sets: `append`, `depend`, `len`, indexing, index assignment, nesting (`x[0].append(1)`)
- [ ] Full builtin set: `input inputInt inputStr inputFloat len min max sum abs sqrt isqrt
      round int float str mean med mode mid`, plus constants `pi` and `e`
- [ ] `import` + module system (single-directory first, packages later)
- [x] `random` module (`randomInt` first) — `stdlib/random.fray`: a pure-fray 31-bit LCG
      with `seed`, `randomInt`, `randomFloat`, `choice` and `shuffle`. Every import root
      now ends with `stdlib/`, so `import random` means the same thing from any directory,
      and `tests/cases/random_module` pins the generator's sequence. The two reference
      sections `tools/check_fray_txt.py` used to record as unimplemented (§22 and §23)
      are compiled, run and diffed like any other now.
      *Exact-integer arithmetic, not a runtime RNG:* the recurrence is C's `rand()`
      constants, and every product stays inside a 64-bit int — `1103515245 * 2^31` is
      about 2.4e18 — so the oracle's unbounded integers and the compiler's boxes agree
      digit for digit, which is what lets a program using the module be diffed between
      the two engines at all. There is no clock or OS-random builtin in the runtime yet,
      so the default seed is a constant and a run is reproducible unless it calls
      `seed(n)`.
- [ ] Exceptions end-to-end: `try/except <Type>/finally`, builtin exception types, tracebacks
- [ ] String type in runtime: UTF-8, indexing, concatenation, `str()` conversions
- [ ] Dictionaries/maps — *not in fray.txt; proposed syntax `x = {"a": 1}` — decide in review*
- [ ] Differential-test the entire golden suite: compiled output == oracle output

**Done when:** every `fray.txt` example compiles and behaves identically to the oracle.

### Phase 5 — Memory management & performance floor — **COMPLETE**
- [x] Atomic refcounting + cycle collector; object header format finalized
  - Runtime split into 5 translation units: `objects.c`, `cycles.c`, `ops.c`,
    `printing.c`, `builtins.c` (all warning-clean under `-Wall -Wextra -Werror`).
  - Object header v2: tag, GC flags (white/gray/black/dead), generation,
    weak-ref count, atomic refcount, type descriptor, generation links.
  - Refcounts count **all** strong references (internal + external): rc==0
    frees immediately (acyclic garbage costs one decrement), containers that
    lose a reference but stay positive become possible cycle roots feeding
    the collector's candidate buffer.
  - Trial-deletion (Bacon–Rajan) cycle collector with young/old generations:
    minors trial all buffered roots with full subgraph expansion; generation
    gates candidate enumeration and promotion only. 13/13 C tests, zero
    leaked objects, including 2000-node ring and shared-cycle cases.
  - Weak references with a registry that survives object death.
  - Allocation fast paths: the root stack is a per-thread array (was two
    `malloc`/`free` pairs per push — the dominant cost of appending to a
    container), and a released non-heap leaf box goes to a per-thread pool
    instead of `free`. Both are thread-local scratch, so **every thread hands
    its pool, root array and scratch buffers back on exit** (spawn trampoline,
    plus a process-exit handler for the main thread) — without that, a program
    spawning short-lived threads retains a pool's worth of dead boxes each.
    Verified under ASan: a 50-thread spawn/allocate/exit churn drops from
    22.6 KB to 9.4 KB leaked, the remainder being GC spaces the registry parks
    for reuse (bounded by `MAX_SPACES`).
  - Program exit releases what the module scope still owns: `main` drops
    every module global and boxed local before returning (skipped while
    spawned threads are live). Function frames needed no equivalent — their
    return path already releases locals — so the module scope was the one
    place where the last binding of every variable outlived its program.
  - `tools/check_memory.py` makes all of this a gate instead of an occasional
    manual inspection: it builds the benchmarks (and, with `--all`, every
    golden case) against an ASan runtime and fails on any leak or memory
    error. **All 35 programs are clean** — one 64-byte box per benchmark was
    the starting point. It immediately paid for itself by finding three
    ownership bugs that no test covered: `fray_string_split` never dropped its
    reference to the pieces it appended (`append` retains), `fray_map_set`
    discarded the key callers transfer when updating an existing entry, and
    `fray_struct_state_release` was a no-op stub deferring to a `finalize`
    hook nothing ever called — so every struct or enum instance leaked its
    field array, field names and values. `fray_range` leaked the two bound
    boxes it builds for `range(n)`.
  - `--driver BIN` runs the same check with a native compiler driver instead of
    the Python emitter — the memory gate for the self-hosted codegen, whose
    ownership rules (a callee owns its parameters, results are owned by the
    caller, an argument list owns what it carries) are exactly what drifts
    between the two emitters. It found four in the self-hosted emitter, all of them
    invisible to the golden suite because a leak never changes what a short
    program prints: a first-class call never dropped the argument references
    `fray_list_index` hands it (one box per argument — and a spawned thread
    runs through that same wrapper, so each thread leaked too), a frame never
    released its own locals (a `return` left the last value of every boxed
    local behind), a match arm's fresh binding slots — the only owner of the
    references `fray_struct_field_get` returns — were never released, and the
    module-scope teardown the bootstrap emitter has had since Phase 6 did not
    exist here at all. **38/38 golden cases are clean under it now**
  - The Python emitter has the equivalent of the last two gaps and they are
    deliberately *not* fixed there: its `_emit_func_return` releases the
    frame's parameters and nothing else, so a boxed local is still live at
    return (`def f(): s = "abc"` then `return len(s)` loses a string and its
    buffer, 68 bytes — the self-hosted compiler is clean on the same program),
    and a match arm bound inside a function leaks its binding the same way.
    The bootstrap is frozen: it rebuilds v1 and serves as the oracle and test
    harness, so its own emitted code is not the release artifact, and the
    `--driver` gate above is what covers the emitter that is.
  - `make -C runtime asan` runs the C suite under AddressSanitizer and is a
    hard CI gate: the runtime owns every object a fray program allocates, so a
    leak in the suite is a leak in every compiled program. It reports
    **16/16 tests, zero leaks**. Getting there also required fixing the test
    program's own ownership discipline (a discarded `fray_atomic_add` result —
    632 boxes, one per add across eight threads; a fresh element appended
    without dropping the caller's reference; a discarded `fray_len` result),
    and taught a raw pthread to hand its runtime scratch back on exit —
    `fray_thread_spawn` does that in its trampoline, but a thread the test
    created itself has to ask. That contract now sits next to the declaration
    in `runtime.h`.
- [x] Type-inference & specialization pass v1: monomorphic fast paths, boxed fallback
  - `bootstrap/inference.py`: flow-insensitive lattice (int/float/bool/
    string/list/tuple/set/none/unknown) with **per-call-site memoized return
    inference** — each call site is typed by re-running the callee with the
    site's argument types; polymorphism widens to unknown.
  - **Self-recursion is inferred to a checked fixpoint**, so a recursive call
    no longer poisons its own function's return type: the body is walked with
    recursive self-calls answering a wildcard (their contribution is "no
    information") to derive a hypothesis, then re-walked with recursive calls
    answering that hypothesis, and the hypothesis is kept only if the second
    walk reproduces it exactly over *every* return in the body. A body that can
    also return another type (`f(n-1)/2` beside an int base case) keeps the old
    widening; mutual recursion still widens to unknown. Parameter types are
    iterated to a fixpoint before codegen reads them, so no body is emitted
    with a slot representation decided from an incomplete set of call sites.
  - **Unboxed (raw) ABI for fully-specialized functions**: a function whose
    every call site passes the same numeric type also gets an `@name.raw` body
    with i64/f64 parameters and result. Raw call sites reach it directly — no
    argument boxing, no unboxing at entry, and recursive self-calls compile to
    a bare `call i64 @fib.raw` with zero allocations. The boxed body is still
    emitted for dynamic sites (spawn, imports, mismatched argument types). The
    raw form is only exposed for a site whose return type was *proven* by the
    fixpoint and whose body returns a value on every path, so it can never
    disagree with the boxed body's observable behaviour.
  - `bootstrap/codegen.py`: full ownership discipline (every expression value
    is an owned reference; releases on statement temps, rebinds, call args,
    conditions, loop iterables/elements; functions own their parameters).
    Fast paths: unboxed i64/f64 arithmetic and comparisons (with exact
    floor-div/mod/zero-div exception semantics), unboxed `for i in range()`
    with constant step, payload-direct bool truthiness. Codegen falls back
    to the boxed runtime call whenever inference says unknown.
- [~] Devirtualization / inline-cache-style dispatch for dynamic call sites
  - Covered in v1 by per-call-site specialization of user function calls
    (same effect for monomorphic sites); a dedicated inline-cache scheme is
    deferred until Phase 6 makes dynamic dispatch actually contended.
- [x] Benchmark suite + regression tracking in CI
  - `benchmarks/`: fib, list build+sum, string build, dense arithmetic;
    runner checks compiled output against the oracle, times compiled vs
    CPython on identical workloads, and diffs against `baseline.json`.
  - Results (Linux Mint, gcc -O2, best of 3; CPython 3.12): fib **14–19×**,
    serial 29×, parallel 76×, arith 4.5×, list build+sum **1.1–1.2×**,
    string build level. Compiled is 1.1–76× faster than CPython on every
    benchmark and ~40× faster than the Python evaluator.
  - CI: runtime C tests (plus the same suite under AddressSanitizer),
    inference unit tests, dual-engine golden suite (`tools/run_tests.py`:
    oracle AND compiled must both match .expected), a leak check over every
    compiled program, benchmark regression gate, and a compiled-beats-CPython
    gate. The benchmark gate needs both a relative and an absolute threshold:
    `fib` runs in ~1-2ms, where process startup alone varies by a millisecond,
    so a pure ratio flags unchanged code — and it now reaches the exit code,
    which is what makes it a gate rather than a printed warning.
  - Golden suite extended with `cycle_collection.fray` and
    `ownership.fray` (10/10 passing through both engines).

**Done when:** recursion, container, and string benchmarks beat CPython on the same tasks. ✅ on Linux Mint: fib 14–19×, list build+sum 1.1–1.2×, arith 4.5×, string build level with CPython, serial/parallel sums 29–76×.

### Phase 6 — GIL-free threading (hybrid, part 1: threads) — **COMPLETE**
- [x] `threads` module: `spawn`/`join`/`joinAll`, `atomic` type with `get`/`set`/`add` (locks land in Phase 7; atomics cover the shared-counter idiom)
- [x] True parallelism: no global lock — per-thread GC spaces, striped 256-shard thin locks on container mutation, stop-the-world collector at safepoints
- [x] Atomic refcounting safe under concurrency; deferred container reclamation through per-thread dead lists purged against all candidate buffers at drain time
- [x] Codegen: module-level variables as LLVM globals (thread-shared), spawn thunks, raw unboxed local slots for inference-proven int/float locals
- [x] **Unboxing ported into the shipping emitter** (this box used to be the single
      highest-value gap in the project). `compiler/codegen.fray` now runs its own
      inference pass — a flow-insensitive INT/FLOAT join over each function's body
      (`infer_program`, the port of `bootstrap/inference.py`) — and gives every name
      it proves a raw `i64`/`double` entry-block alloca instead of an `i8*` slot:
      unboxed reads, `add`/`sub`/`mul`/`srem`/`sdiv` for the arithmetic, an
      `icmp`/`fcmp` straight into a condition, and `fray_as_int`/`fray_as_float` as
      the only boxed->raw transition. `parallel_sum`'s `w1` went from 22 calls in the
      body to 8, none of them inside the loop. Same 8M-iteration benchmark, same
      driver: **683 ms -> 3.4 ms** (200x; the bootstrap emitter takes 4 ms), and
      `serial_sum` 276 ms -> 8.4 ms. The Phase 6 thread-scaling failure was the
      boxes, not the threads: 4 threads at 3.4 ms now beat 1 thread at 8.4 ms (2.5x)
      where the old driver was 2.4x *slower* with 2 threads than with 1, because
      every iteration went through one allocator.
- [x] The pass is deliberately stricter than the bootstrap's join, because a raw
      slot is not forgiving: an expression it cannot type widens the name to boxed
      (the bootstrap lets such a contribution through as "no information", which is
      safe there and not here — a raw slot fed a string reads as 0 through
      `fray_as_int`), a name bound by a match arm, an `except` clause or a `del` is
      never unboxed, a module global is never unboxed (it is shared with every
      spawned thread, not a frame slot), and an async body is left boxed entirely.
      Parameter types are the join over every call site, so a function called with
      both `3` and `"q"` keeps a boxed parameter.
- [ ] The unboxed *function ABI* is still the bootstrap's alone: a call still hands
      over boxes and still gets one back, so `x = f(n)` pays a box per call and a
      function's return is re-boxed. The loop this box was about no longer does; a
      recursive helper called per iteration still will. `bootstrap/codegen.py`'s
      `raw_specialized` / `_raw_call` machinery is the model — it needs the
      self-recursion fixpoint this port does not have (three widening rounds, no
      hypothesis), so a recursive function proves nothing here.
- [x] `for i in range(n)` is a counted loop: no list is built and the loop *head* is
      unboxed, not just the body. `compiler/codegen.fray` gates `for` on
      `is_range_call` with arity 1-3 and a statically non-zero step and then emits a
      counter in an entry-block `i64` slot (`gen_for_range`); everything else keeps
      the generic list/map path. The counter is seeded to `start - step` and the step
      is added in the preheader, so `continue` re-enters the preheader and takes its
      step and `break` still lands on the exit — there is no continuation block to
      get wrong. `range_step_value` reads the sign of a `UNARY_OP "-"` over an int
      literal, so `range(4, 0, -1)` counts where the bootstrap's `_range_step_static`
      fell back to the list; a literal zero step is deliberately *not* countable and
      keeps the generic path, where the runtime raises the catchable `ValueError`.
      `arith.fray` (3M counted iterations), same machine, best of 3: **331 ms ->
      112 ms (2.9x)** against the pre-change driver, output identical, and the
      `fray_range3` call is gone from the IR — the 3M-element list is never built.
      `list_sum.fray` 1.07x, `string_build.fray` 1.13x. Golden case `range_for.fray`
      pins all three arities, both directions, zero-trip loops each way, break and
      continue, a body that rebinds the loop variable, nesting, a parameter and a
      float-proven loop variable, a dynamic step, and once-only in-order bound
      evaluation.
- [x] `continue` is implemented in the bootstrap emitter, and the two engines agree
      on it. `ContinueStatement` was `pass  # TODO: continue support`, so a
      `continue` silently ran the rest of the body; `range_for.fray` was the first
      case in the corpus to need it. Each loop emitter now publishes a
      `_loop_continue` target — the index increment for a list loop, the counter
      preheader for a counted loop, the condition preheader for `while` — saved and
      restored at the same six points `_loop_break` is. Golden case `continue.fray`
      pins all seven shapes (list, counted, while, nested, body ending in the jump)
      through both engines.
- [x] Two block-scope bugs the new cases exposed, both the same shape: a name first
      assigned inside an `if` body and read after it. The evaluator runs every
      nested body in a child scope (`bootstrap/evaluator.py::_exec_block`) while the
      compiled emitter treats a function's names as function-scoped, so these
      compiled cleanly and died on the `--run` path with `FrayNameError`. `start`/
      `stop` in `gen_for_range` and `f` in `gen_store_raw` (reached by `acc += i`
      into a float slot with an int right-hand side) now bind before the branch, the
      way `parse_index_or_slice` already declares its three bounds.
      `tools/check_scope.py` audits for the pattern and reports **0** across
      `compiler/` and `stdlib/`; it is validated by the negative control of removing
      either fix, which makes it flag exactly that name.
- [x] A raw *float* slot can take an augmented assignment in the bootstrap: the fast
      path tested `_slot_kind(...) is not None` where `_raw_binop_i64` always returns
      `i64`, so `acc = 0.0; acc += i` stored an `i64` into a `double*`. It is gated
      on `"int"` now, and a raw float slot reads its double out of the slot and
      re-boxes it for the runtime operator.
- [ ] ThreadSanitizer CI job (deferred: TSAN unavailable in the MinGW toolchain; the 8-thread C stress test in `runtime/test.c` covers the race surface)
- [x] Stress suite: shared list/set mutation across 8 threads + atomic add, 10k iterations, zero corruption, leak-free

**Done when:** an N-thread CPU-bound fray program shows ~N× speedup where CPython shows none.
**Result:** the 4.9× first recorded here was measured on a *bootstrap-emitted* binary and did not reproduce on the shipping compiler (see the unboxing box above); with the pass ported, the driver emits the same unboxed loop and 4 threads on `parallel_sum` (8M-iteration int reduction) run in 3.4 ms against 8.4 ms for the serial twin — 2.5x on 12 cores. Golden suite 48/48 through both engines.

### Phase 7 — Coroutines (hybrid, part 2: async I/O)
- [x] Coroutine scheduler: per-thread event loops, M:N multiplexing onto OS threads —
      `runtime/coroutine.c` runs stackful coroutines on platform fibers: the thread that
      runs async code converts *itself* into a fiber and drives an `EventLoop`, and each
      async call gets its own fiber multiplexed onto that loop. The loops are per-thread
      — a loop's state is thread-local, fibers of one loop never run concurrently, and the
      hot path takes no lock; a foreign thread may push onto a ready queue under the loop's
      lock, which is the seam the Phase 6 channel wakeups use.
- [x] End-to-end async path: `async def`/`await`, `yieldNow`, `sleep` timers and
      `runUntilComplete` work in the oracle and the compiled binary, and
      `tests/cases/async_basic.fray` now pins the compiled path through both engines
      (three coroutines, a nested `await`, an argument-carrying call, a yield loop and
      a timer). The runtime's scheduler used to print `D:`/`U:`/`T:`/`F:` debug traces
      to **stdout**, which leaked into every program's output; those are removed.
      Async functions take parameters on both engines: a call site builds the
      argument list (`fray_list`/`fray_list_append`), `fray_coro_start_argv` carries
      it on the coroutine, and the compiled body unpacks its parameters from it
      (`fray_coro_argv`) — so arguments are per-call and cannot be seen by another
      invocation. A call with the wrong argument count is now a clear
      `file:line:col: 'f' takes N argument(s) but M given` diagnostic from the
      compiler and a `FrayTypeError` with the same wording from the oracle, instead
      of a bare `IndexError` from inside llvmlite (and, for a missing plain argument,
      instead of a later "name is not defined"). Non-blocking file/socket I/O is the
      separate item below and is still open. (One residual asymmetry: an async call with
      the wrong count *inside a coroutine* is only caught by the compiler — the oracle
      drops uncaught coroutine errors, so it prints nothing.)
- [x] `async def` / `await` syntax — implemented in both frontends
      (`compiler/lexer.fray` + `compiler/parser.fray` and `bootstrap/lexer.py` +
      `bootstrap/parser.py`) and pinned through both engines by
      `tests/cases/async_basic.fray`. The "proposed — confirm in review" caveat is
      retired: the syntax is what v0.1 ships, so the confirmation is a note for the next
      language revision, not an open question about this one.
- [x] `sleep` and timers in the runtime — timers arm on the owning loop's timer list and
      `await sleep(1)` runs in both engines under `async_basic.fray`
- [x] Non-blocking file & socket I/O in the runtime — `runtime/io.c`. The design is a
      worker pool rather than epoll, on purpose: `epoll_ctl(2)` returns EPERM for a
      regular file, so a purely readiness-driven loop could never make *file* I/O
      non-blocking even though it would have handled sockets. Files and sockets take the
      same path instead: a blocking syscall on one of four workers into a malloc'd
      buffer, posted back to the coroutine's own loop through the ready-queue push the
      loop already documents as its cross-thread seam. A worker never touches a FrayValue
      — the resumed coroutine converts the buffer on its own GC space, which is the only
      reason this is safe to run off the loop thread. The loop's idle wait is now a
      condvar (`loop_wait`) instead of a 1 ms poll, so a completion is picked up when it
      posts rather than up to a millisecond later.
      Surface: `readFileAsync`/`writeFileAsync`, `readAsync`/`writeAsync` over a
      descriptor, and `tcpListen`/`tcpAccept`/`tcpConnect`/`closeSocket`/`tcpPort`.
      `tcpListen` binds loopback only, and `tcpPort` exists so a test can ask for an
      ephemeral port (`tcpListen(0)`) instead of hardcoding one. From a plain thread the
      same calls run the syscall inline, which is what lets the oracle — one thread per
      coroutine — agree with the compiled engine instead of deadlocking behind it.
      `tests/cases/async_io.fray` and `tests/cases/async_net.fray` pin both through the
      two engines, and both are clean under ASan with the pool running.
- [~] Interop rule: coroutines (cheap, growable stacks) for I/O; threads for CPU; channels
      to connect them — the mechanism is complete end to end, and an earlier note here
      claimed the opposite. Channels are **not** found by grepping `runtime/builtins.c`
      because there is no builtin registry: `codegen.fray` lowers the names straight to
      `fray_channel_new`/`send`/`recv`/`close_boxed`, `sema.fray` lists them in `BUILTINS`,
      `bootstrap/codegen.py` has the same table, and the oracle has
      `_builtin_channel`/`_send`/`_recv`/`_close` — so a fray program *can* reach a
      channel. What is actually missing is proof: **no golden case constructs a channel**,
      and nothing exercises threads and coroutines in one pipeline. The gap is test
      coverage, not a missing language binding.
- [ ] Readiness-based socket I/O (epoll/kqueue) instead of a blocking worker pool —
      the pool now grows on demand to stay correct, but every in-flight operation still
      costs a thread, and the 256-thread cap is a cliff rather than a design. epoll
      cannot help *files*, so this is a split design: sockets on readiness, files on
      the pool.
- [~] Stress: 100k concurrent coroutines + mixed thread/coroutine pipeline — the C level
      already covers both shapes (`runtime/test.c`: `coroutine_stress` spawns 100000
      coroutines, `coro_thread_pipeline` runs thread → channel → coroutine), and both
      still pass with the I/O pool added. What is missing is fray-level coverage: no
      golden case drives a coroutine under a `spawn`ed thread, so the interop the C test
      proves is not yet proven through the language.

**Done when:** I/O-bound benchmark matches or beats async-Python while CPU work still scales across cores.

**Result (partial, and correcting an earlier reading of it).**
`benchmarks/run_io_benchmarks.py` measures the done-when. 48 files x 2 MB, 16 conns x
64 rounds, best of 3, process startup subtracted. **Against async-Python fray wins
both halves: 4.2x on files** (asyncio + `run_in_executor`, its only option for file
I/O) **and 5.6x on sockets** (asyncio's real readiness-based I/O, the strongest bar
available). That part of the claim holds.

**The coroutine-concurrency claim does not, and an earlier reading of it was wrong.**
The first run showed coroutines beating sequential blocking I/O 2.07x, and that was
not concurrency — it was `fray_file_read` being slow. Once the read was fixed (below),
a warm 2 MB read costs ~0.12 ms and the coroutine path's per-operation cost — enqueue,
park the fiber, worker posts, condvar wake, resume — exceeds the read itself. Coroutines
now run at **0.50x** of sequential blocking and **0.13x** of a thread per file. The
honest statement is that coroutine I/O pays off only when the operation is slow enough
to hide the handoff; on a warm page cache it is a net loss, and a real thread per file
wins outright. The benchmark's file workload is therefore not a useful concurrency
measurement at all, and one that would need cold caches (root, to drop them) or a slow
device to say anything.

The CPU clause is **not** met either, and the reason is not Phase 7's. `parallel_sum`
does not scale here: 1 thread 270 ms, 2 threads 647 ms, 4 threads 657 ms, 8 threads
723 ms, so a second thread costs 2.4x rather than saving anything. The cause is the
self-hosted emitter, not the runtime: it does **not** unbox local slots, so
`s += i % 7` allocates a box and calls `fray_int`/`fray_add`/`fray_retain` on every
iteration, and four threads allocating through one allocator serialise completely.
The same source emitted by the Python bootstrap runs 8M iterations in **4 ms** versus
**662 ms** from the driver (1 `fray_add` versus 8, hoisted out of the loop). The 4.9x
recorded under Phase 6 was therefore measured on a bootstrap-emitted binary, not on
the compiler that ships. See the Phase 6 item.

Writing the benchmark also caught a **pool-starvation deadlock** in `runtime/io.c`
as first written: a fixed 4-worker pool deadlocks once more than four operations are
outstanding *and* they depend on each other, because a worker parked in `read()`
waiting for a peer's `write()` never frees a slot for that write. Five concurrent
conversations hung; the oracle, being thread-per-coroutine, did not. The pool now
grows on demand up to a cap, and `benchmarks/io_socket_coro.fray` is the regression
that exercises it. This is the same cliff asyncio's default `ThreadPoolExecutor` has;
readiness-based socket I/O is the real fix and is still open.

It also caught a **compiler bug in the Python bootstrap emitter**, which is now fixed:
`_ensure_alloca` created a variable's slot with `position_at_end(entry_block)`, so a
variable first used *inside a loop body* got its `alloca` appended after the entry
block's `br` — an instruction after a terminator, which is not valid IR. The emitter
died with `expected instruction opcode` on any program assigning a variable inside a
`while`. The pair (alloca + NULL init) is now slid to the front of the entry block,
where allocas and stores are side-effect free, the same argument `_raw_slot` already
made. The self-hosted driver never had this bug.

### Phase 8 — Self-hosting ⭐ — **COMPLETE**
- [x] Write the compiler in fray: `compiler/` — lexer, parser, sema, IR, `.ll` emission
  - **Stage 1: the compiler compiles its own source.** All four modules go through its own
    lexer→parser→sema→codegen and LLVM verifies the result — `lexer` 232 KB, `parser`
    340 KB, `sema` 515 KB, `codegen` 1548 KB of IR (module plus its driver where one
    exists). Each module also builds and runs, and
    three are checked differentially: `tests/selfhost/{lexer,sema,codegen}_driver.fray`
    are appended to the module (with its `import` lines stripped) so the compiled binary
    can be *executed* and compared with the oracle — all three match exactly, including
    the lexer's token stream over two programs and its error collection.
    `tools/check_frontend.py --stage1` runs this in CI.
  - **Fixed-point status: reached — v2 compiles the compiler to a bit-identical v3.**
    `tools/check_frontend.py --stage2` runs the whole chain and holds it to the plan's
    release criterion. v1 (the Stage 1 binary) compiles `compiler/frayc.fray` → IR1;
    IR1 is emitted to an object and linked into v2; v2 compiles the same source → IR2;
    IR2 builds v3; v3 compiles it → IR3. The gate requires **IR1 == IR2** (Stage 2: the
    compiler compiled by itself emits exactly what the first native compiler emitted),
    **IR2 == IR3**, and the two binaries **byte-for-byte identical** — the `v2 ≡ v3`
    check, satisfied at the strongest level there is (`llc` writes the input file name
    into the object, so the gate builds both from the same `.ll` path to make that
    comparison meaningful). Then it runs the golden suite through v2, the binary a user
    would ship. Measured with the counted-range loop and the two block-scope fixes in
    the emitter (30 Sep): IR1/IR2/IR3 are the same 5.73 MB of text, v2 and v3 are the
    same 958 KB executable, and v2 compiles all 47 golden cases. IR is verified by
    LLVM at each step, and the objects come from `llc`/`cc` — nothing in the loop
    parses IR on the host side.
    That byte-identity is also the project's determinism check, because a fixed point is
    impossible unless the emitter is a pure function of its input. It is: nine
    independent self-compiles of `frayc.fray` (six of them concurrent) produced one
    byte-identical 5.65 MB IR, and each of the six compiler modules compiled to the same
    bytes twice running, all LLVM-valid. The inference pass decides slots from maps keyed
    by name, and `fray_map_keys` walks a hash table in slot order whose hashing is by
    value (`FNV-1a` for strings), so nothing in the emitter reads an address — a map keyed
    by an unhashable value would hash by pointer and iterate in heap order, which is the
    one way a hash map could leak run-to-run variation into the output.
    One `--stage2` run on 29 Sep did reject IR1 — `expected instruction opcode` at a
    function's entry label, which is LLVM reporting a block that opened and closed
    without an instruction — and it has not reproduced in the nine self-compiles above.
    The driver binary and every file under `compiler/` are unchanged since the build that
    run used, so what produced it is not explained, and it is written down here rather
    than forgotten. The shape it points at is an instruction landing in a function's
    implicit entry block, which is how `main` was broken once already (see the comment on
    `main_entry` in `gen_main`), and the emitter has no path that does it today.
    This is the completion of what started as IR-level Stage 1 — the
    compiler's own modules compile, build and run — and the four now also build as
    *one binary* through a real module system. `compiler/link.fray` resolves a program's
    imports against a host-supplied module catalog and namespaces each module: a
    top-level name more than one module defines (`get_errors`/`clear_errors` in three,
    `node_type`/`node_get` in two, the private `errors` list in all four) is renamed to
    `<module>_<name>`, while `import X`, `from X import a` and qualified references
    resolve to the defining module's symbol. A module path may be dotted to any depth —
    `import a.b` is `a/b.fray` under the program's root — and `a.b.f()`/`a.b.CONST`
    resolve through the longest imported module prefix, so packages work like Python's;
    the golden suite's `dotted_import` exercises the from-import form differentially, and
    `tools/check_frontend.py --packages` covers a cross-package name collision.
    A directory with an initializer (`pkg/__init__.fray`) is a package: the initializer
    provides the module `pkg`, and leading dots make an import relative to the importing
    module's package (`.util`, `..other`), so a package is split across files and
    re-exports names from them (`from .util import twice` makes `from pkg import twice`
    work, in the linker, the oracle and the bootstrap codegen alike). The golden suite's
    `package_init` covers it end to end through all three, and `--packages` gained a
    package-initializer scenario.
    `compiler/main.fray` imports the compiler
    modules and drives the pipeline, so building it links all of them into one
    executable — no host-side text mangling. `tools/check_frontend.py --single-unit`
    builds it, links it, runs it and diffs its output against the oracle, and the golden
    suite gained `import_test` (a program using both import forms) through the same
    path.
  - **Stage 2 is in: the compiler is a native binary that compiles programs from argv.**
    `compiler/frayc.fray` is the driver — `frayc <source.fray> [module_root]` — and it
    needs nothing from the host beyond the binary itself: it reads the source with
    `fileRead`, walks the source's directory and the extra root with `listDir`/`isDir` to
    build the module catalog the linker resolves imports against, runs lex → parse → link
    → sema → codegen inside itself, prints the IR between `===IR_START===`/
    `===IR_END===` markers, and reports a rejection as `<STAGE> ERROR: file:line:col: msg`
    with exit status 1. `tools/frayc.sh` is the compile loop a release ships: driver →
    `llc` → `cc` → binary, with no Python anywhere in it (the only host dependencies are
    LLVM's object emitter and a C compiler). Compiling `tests/cases/async_basic.fray`
    that way takes 0.14s end to end, and 74s to build the driver itself.
  - **The emitted module is host-agnostic.** It carries no target triple and no data
    layout: the Windows/MSVC triple used to be baked into the text, so the host rewrote
    the module before emitting an object. Every host's LLVM fills in its own defaults
    instead, and the same `.ll` builds on Linux, macOS or Windows. Writing it that way
    exposed a second, sharper problem: `llc` is strict about IR text where llvmlite was
    not — every function's unnamed temporaries must be numbered from `%0` and increase,
    and the emitter had been starting each function at `%1` (llvmlite accepted it, `llc`
    failed with `instruction expected to be numbered '%0'`). Fresh-variable numbering now
    restarts at 0 in every function, including the value wrappers, async bodies,
    trampolines and `main`.
  - **All 38 golden cases compile, link, run and match `.expected` through the native
    driver binary**, and through the oracle pipeline (`tools/check_frontend.py --run
    --update`; `tests/selfhosted_supported.txt` lists exactly the suite, so a case that
    regresses fails the gate). What closed the last eight cases: **`async def`/`await`
    with coroutines and channels** (the parser reads the keyword order right —
    `async def`, not the `def async` it expected — an async function lowers to a
    `void fray_async.<name>` trampoline plus an `i8* fray_async_body.<name>` that unpacks
    its parameters from the coroutine's argument list, `await` becomes `fray_coro_await`,
    and the boxed shims for `sleep`, `yieldNow`, `coroCount`, `runUntilComplete`,
    `channel`, `send`, `recv` and `close` are the bootstrap's); **FFI calls** (extern
    arguments and results unboxed and re-boxed around the C ABI); **Option/Result**
    (`Some`/`Ok`/`Err`, `unwrap`, `isSome`/`isNone`/`isOk`/`isErr`, `expr?`, and `null`
    lowering as the none value rather than `False`); **first-class function values** (a
    bare function name is a `fray_function` object and a call through a variable, a field
    or an element lowers to `fray_call`); and **threads/atomics** (`spawn`/`join`/
    `joinAll`, `atomic` and its `get`/`set`/`add` methods, shared containers across
    threads). `tests/cases/ffi_types.fray` was added for the FFI return conventions
    (string, int, double) so they are pinned rather than assumed.
  - Codegen defects that self-compilation exposed, all fixed: a local assigned only at a
    function body's top level was treated as a module global ("unknown name"); a struct
    or enum constructed inside a function body was an "undefined function" (functions are
    emitted before the module pass that registers type definitions); rebinding a
    parameter released a reference the caller owned, which double-freed it (parameters
    were borrowed then — the caller released each argument after the call); and `main`'s
    hardcoded `entry:` label collided with a local named `entry`, because LLVM's IR text
    keeps block labels and local values in one namespace.
  - Frontend runs the golden set end to end: **38/38 cases compile, link, run and match
    `.expected`** through `tools/frayc_selfhosted.py` (and through the native driver — see
    Stage 2 below). Enum variants are in: the value
    form (`Color.Red`, no call — how `match_basic` and `enum_basic` are written) lowers
    like the bootstrap engine's, the match dispatch on it works, and the broken shapes
    (calling a parameterless variant, a wrong argument count, a bare parameterized
    variant, a member that is no variant) are compile-time diagnostics instead of
    silently-built null fields — and **all three engines now reject them identically**:
    the oracle's `Enum.Variant` used to hand back a constructor closure even when read
    as a value (it printed as a value, passed for one, and only the first field read
    died with "object has no attribute …"); the evaluator dispatches `Enum.Variant(args)`
    on the enum definition itself now, so the bare form raises `FrayTypeError` at run
    time while the two compiled engines reject it at compile time. The one shape still
    reported by name is a statically-known string iterable (divergence 2 below).
    `import_test` (imports `math_lib` both qualified and through a from-import) and
    `dotted_import` (`from pkg.util import twice`) both pass: a program with imports now
    compiles to a single binary, and a dotted path resolves into a package directory.
  - **Qualified match arms are in, in both native engines** (the golden set is 38/38
    supported now). A pattern may name the enum it belongs to (`case Shape.Circle(r):`),
    and the oracle matches such an arm only against an instance of *that* enum: the
    variant name alone is ambiguous when two enums declare one, and the arm's params bind
    the named enum's variant fields. The self-hosted parser already read the dotted
    pattern; what was missing was the dispatch — both emitters compared the variant name
    and ignored the qualifier, so `case A.V(q):` matched a `B.V(7)` and then died reading
    a field B's struct does not have (`AttributeError: struct has no such field`) instead
    of falling through to the arm below it.
    An enum instance now records its enum in a hidden `_enum` field beside `_variant` (set
    where the instance is built — the struct has no other per-instance type identity, and
    `fray_struct_new` still discards the name it is handed), and a qualified arm is the
    variant test *and* a compare of `_enum` against the arm's qualifier. The qualifier is
    a reference to a top-level name, so `link` rewrites it like any other use of the
    enum; without that, a colliding enum name (`shapes_Kind`) or a from-import alias left
    the arm looking for the pre-link spelling. `tests/cases/match_qualified.fray` pins the
    dispatch, the field binding and the fall-through through all three engines, and
    `--packages` gained an enum-namespace scenario for the renamed case.
  - Honesty is now a gate (`tools/check_frontend.py`, in CI): the frontend must always
    terminate, must report syntax it cannot compile as a staged diagnostic
    (`LEX|PARSE|SEMA|CODEGEN ERROR: file:line:col`) instead of hanging or crashing
    internally, and every program it *accepts* must match the oracle. The gate's first
    run found three real problems; two are fixed here, the rest are recorded below.
  - The struct-field hang is fixed at the source: every parse loop consumes a token
    before looping again, and `expect()` reports instead of spinning. The driver's
    watchdog turns any future non-termination into a named failure, so a hang can never
    look like a slow compile.
  - `tests/selfhosted_supported.txt` records what compiles, so coverage cannot regress
    silently — a listed case that starts reporting a diagnostic fails the gate.
  - Fixed by this work: `for k in map` compiled and then died at run time with
    `TypeError: not a list`; `gen_for` now dispatches on the value's tag at loop entry
    (a map becomes its keys list via `fray_map_keys`, as the bootstrap's compiled path
    already did) and releases the sequence it iterated, which the loop previously
    leaked.
  - **Function values are first-class in the compiled path.** A bare function name in value
    position is now a `fray_function` object wrapping `void fray_val.<name>(FrayValue self)`
    — the same shape the thread and coroutine entry points invoke, so the runtime calls all
    three identically — instead of the null module slot of the same name, which had silently
    made every function value `None` (`Holder(h)` stored nothing callable). A call whose
    callee is not statically known (`g(1, 2)` for a variable or a parameter, `holder.fn(1)`
    through a field, `xs[0](1, 2)` through an element) lowers to `fray_call`, which validates
    the callee and the argument count
    and raises the oracle's `TypeError`s (`'h' takes 1 argument(s) but 2 given`, `'5' is not
    callable`). Function values also print as the oracle does (`<function add>`), where
    `None` used to appear. The self-hosted frontend reports the shape as unsupported
    (`CODEGEN ERROR: unknown name 'add'`) rather than mis-compiling it, and
    `tests/cases/function_value.fray` pins the rest under both engines and the ASan sweep.
    What found the gap was `tools/check_runtime_symbols.py`: the emitter had been emitting a
    call to `fray_call`, a runtime entry point that existed nowhere, so `Holder(h)`-then-call
    and `a.b.f()` died as a bare `KeyError` from inside the compiler.
  - Known divergences (recorded, not hidden):
    1. **Struct field defaults** — `spec/phase8-features.md` and the oracle fill an
       omitted field from its declared default; both native engines build `None`. The
       self-hosted sema now rejects a constructor that would need a default (the
       bootstrap's compiled path still silently builds `None` — open).
    2. **Iterating a string or tuple** — the oracle yields characters/elements; both
       native engines die with `TypeError: not a list` (`fray_list_index` requires a
       list). The self-hosted sema rejects a statically-known string iterable — open.
    3. **Map key order** — both native engines walk hash-table order, the oracle keeps
       insertion order. `maps.fray` passes because its two keys happen to agree; the
       gate tracks this as a known divergence so it cannot drift unnoticed — open.
    4. **A missing value on stdout** — the oracle prints `<object object at 0x…>`, the
       compiled engines print `None`, so the oracle cannot be the reference for such a
       program — open.
    5. **Local scope** — a local first assigned inside an `if` and used after it is fine
       in both compiled engines but raises `FrayNameError` in the oracle (block scope vs
       function scope). Captured as the `local_in_if_used_after` probe, so the day it is
       resolved the gate says so — open.
    6. **Dotted `import a.b`** — the self-hosted linker resolves the full path like
       Python (`a.b.f()`, `a.b.CONST`); the oracle binds the *leaf* module to the first
       segment, so `a.f()` works and `a.b.f()` raises `'a.b' has no exported 'b'`, and the
       bootstrap codegen's dotted alias is incomplete: `a.b.f()` binds the leaf module to
       the first segment, so the field read fails at run time with `TypeError: not a
       struct` (before function values existed the same program died inside the emitter).
       The from-import form
       (`from a.b import c`) agrees across all three engines and is the golden case; the
       qualified form is checked by `--packages` against an expected output — open.
       A bare relative `import .sibling` inherits the same split (the oracle and codegen
       bind the leaf under the written first segment, the linker uses the resolved full
       path); `from .sibling import name` and `from . import sub` agree across all three
       and are what `package_init` and `--packages` cover — open.
    7. **A `try` that does not handle what it caught** — *resolved.* The compiled engine
       used to drop the exception: the dispatch chain's no-match arm branched to the
       `finally` block, whose `fray_try_end` cleared the exception state, so the try body's
       fallback value stood and the program continued (`try: v = x / 0` with
       `except KeyError:` printed and carried on, where the oracle propagates
       `FrayZeroDivisionError` and stops). A bare `except:` was the sharper case — lowered
       as "exception type == 0", which a real throw never is, so it never matched. The
       runtime now keeps the pending exception across `fray_try_end` (and keeps its message
       alive for the re-raise), a matching clause calls the new `fray_exc_clear`, and
       `fray_exc_rethrow` in the `finally` hands anything still pending to the enclosing
       `try` or out of the program. Clauses are tested in order and the first match runs;
       a catch-all clause is a test too — membership in the five types the oracle can name
       (see divergence 10). Both emitters lower it this way, so the self-hosted compiler
       reads the same contract: `try_except`, `uncaught_exception` and `uncaught_keyerror`
       run identically in all three engines.
    8. **A fatal error loses the oracle's buffered output** — the oracle collects stdout and
       writes it only after the program finishes, so a program that dies from an unhandled
       exception prints nothing there, while both compiled engines have already printed
       whatever came before it. The golden cases for unhandled exceptions therefore assert
       an empty stdout plus a status (`.exit`) rather than text. Flushing the interpreter's
       buffer on the way out is the fix — open.
    9. **A partially evaluated statement still produces output** — compiled code polls for a
       pending exception after each *statement*, not inside an expression, so a throw in the
       middle of `print(xs[9])` still reaches the print and emits a placeholder (`None`;
       `print(10 / n)` with `n = 0` prints `0.0`) before the handler runs, where the oracle
       prints nothing from that statement at all. Assignments hide it (`v = xs[9]`), and
       that is how `try_except` is written. Closing it needs the throw site to unwind
       instead of returning — the runtime's `jmp_buf` machinery is in place but unused —
       open.
    10. **A bare `except:` does not take a `KeyError`** — in the oracle because its `try`
       catches only the five types it can name (`FrayKeyError`, `FrayImportError` and
       `FrayRuntimeError` pass every clause, bare or not), and both compiled engines now
       mirror it exactly: a catch-all clause tests membership in
       {TypeError, ValueError, IndexError, NameError, ZeroDivisionError} rather than "any
       exception", and the runtime's own `FRAY_EXC_KEY`/`FRAY_EXC_RUNTIME` fall outside it.
       `uncaught_keyerror` pins the agreement. Python would catch these; when the oracle
       learns to, both emitters must stop mirroring — open (mirrored deliberately).
    11. **A native Python error can escape the oracle** — `int("nope")` raises Python's
       `ValueError` inside the evaluator (`Error: ValueError: invalid literal for int()
       with base 10`), not `FrayValueError`, so no clause catches it there while both
       compiled engines raise the fray ValueError and catch it. Found while writing
       `try_except`, which avoids the construct — open.
    12. **An unqualified arm on an ambiguous variant name** — `case V(q):` where two
       enums declare `V` — binds the fields of one of them statically, where the oracle
       binds against the subject's own enum at run time, so the arm can read a field the
       subject's struct does not have (`AttributeError: struct has no such field`) where
       the oracle has the value. Qualifying the arm (`case A.V(q):`, the support added
       above) is what tells the two apart. The suite's unqualified arms
       (`selfhosted_new_features`, and `match_qualified`'s `case Solo(n):`) only ever
       name a variant one enum declares, so nothing there is in this hole — open.
    13. **An async function used as a value** — `g = f` where `f` is async, then `g(1)`:
       the oracle binds the function object and starts a coroutine when it is called (it
       prints the coroutine object itself). The self-hosted codegen rejects the value form
       at compile time (`async function 'f' used as a value is not supported …`), because
       an async function's only entry point is its coroutine trampoline — there is no
       wrapper to hand out, and calling the trampoline outside a coroutine would run the
       body against no argument list at all. That is a cleaner failure than the
       bootstrap's: it emits a `fray_val.f` wrapper that calls a function named `f`, which
       is not the trampoline's name, so the program dies at link time with an undefined
       symbol — open (rejected deliberately).
    14. **`await` outside an async function** — the oracle evaluates it, and a
       module-level `await f()` genuinely waits for the coroutine and returns its value.
       Both compiled engines reject it at compile time (`await outside an async function`,
       the bootstrap emitter's own wording): a top-level await would have to run the
       scheduler from `main`, which is a different execution model, not a lowering detail
       — open (mirrored deliberately with the bootstrap emitter).
    15. **A `return` in a `try` body never reached the handler** — *resolved* (see the
       try/return note below). The compiled engines polled for a pending exception *after*
       a statement, and a statement that returns ends the block, so the poll was skipped and
       the exception stayed pending: `try: return a / b` with
       `except ZeroDivisionError: return -1` returned the division's fallback `0.0` and the
       clause never ran (`try: return xs[9]` with `except IndexError: return -1` returned
       `None`), where the oracle returns `-1` for both. Divergence 9 describes the same poll
       model from the other side — the poll happens too *late* there; here it never happened
       at all, so a try body that threw on its way out of the function silently lost its
       handler. The guard now sits on the return path inside a try as well, in both emitters
    16. **A `return` in an `except` body skipped that try's `finally`** — found while
       closing 15, and *resolved* with it. The oracle runs the finally on the way out of a
       handler return (`try: 1/0` / `except ZeroDivisionError: return 5` /
       `finally: print("f")` prints `f`); both compiled engines returned straight out of
       the handler, because a handler body was generated outside its try's return context.
       It is generated *inside* it now — the same park/flag/finally, with the finally as
       the handler's dispatch rather than this try's clause chain, so an exception raised
       inside a handler still belongs to the enclosing try. The finally *body* stays
       outside the context: a `return` there would park its value and branch back into its
       own finally. Pinned by `try_return`'s `fallback`: a clause's return runs the finally,
       and the clause's value is what the caller sees
    17. **`print` only printed its first argument** — *resolved.* Both native emitters
       lowered it as the single-argument call it had always been and silently dropped the
       rest, and the self-hosted one leaked them besides: `print(a, b)` released only
       `args[0]`, so every extra argument leaked one box per execution. `fray-layout.md`
       §11.2 documents `foo(a, b, c)`, so this was a documented call the compiler silently
       truncated. The evaluator was already right (`_builtin_print` joins its arguments),
       which is what made it a divergence and not a missing feature. The runtime grew
       `fray_print_sep` (repr plus a trailing space) and `fray_print_end` (a bare newline),
       and both emitters now write every argument but the last with the separator and the
       last one with `fray_print` — so `print(x)` is still exactly the one call it was, and
       the whole sequence is monomorphic: no C varargs and no argument array.
       `print()` with no arguments is `fray_print_end`, which is what the oracle did and
       what the native engines used to drop on the floor. Pinned by `print_args`, which
       also pins the separator count, the missing trailing space, `print` used as an
       expression, and left-to-right once-only argument evaluation
    18. **A map or struct prints as a placeholder** — the runtime's `rb_repr` has no
       iteration for `TAG_MAP` and writes `{<n> items}` ("For now, just show count"), and
       writes `<struct>` for `TAG_STRUCT`, where the oracle prints the entries and the
       fields. Found by `print_args`, which had to leave both out: a golden case cannot
       pin two different renderings, and this is a repr gap rather than a `print` one.
       `fray_map_keys` already returns the keys in insertion order, so the map side is a
       small change; the struct side needs a field walk — open.
    19. **`bootstrap/frayc.py run` leaks a traceback on a semantic error** — `pi = 0` is an
       `invalid assignment target` that the oracle and the evaluator both report as a
       one-line `SemanticError`, while `cmd_build`/`cmd_run` let it escape uncaught (only
       `cmd_ir` catches it). It is a debug entry point rather than the shipped chain
       (`tools/frayc.sh` drives the native driver), so nothing depends on the exit
       status — open
    20. **String constants were never deduplicated** — *resolved.* `b.string_consts`
       was a list that `get_string_const` scanned for an already-interned string, and
       `string_const_ref` then **overwrote the entry it had just appended** with a
       `("pending", s, encoded, idx)` tuple. So by the next scan every entry was a
       tuple, `tuple == string` is false, and the lookup could never hit: each
       `string_const_ref` call appended a fresh constant. Compiling `frayc.fray` emitted
       **5451 `.str.N` definitions for 946 distinct strings** — 82% of the string
       constants in the module, and with them most of why a self-compile took five
       minutes and the IR was 5.7 MB. The encoding now lives in a parallel
       `b.string_encoded` list (a `null` entry meaning “interned, not yet emitted”,
       which is what the `"pending"` marker was for), so `b.string_consts` holds only
       the strings it is scanned for and the lookup works: **946 definitions for 946
       distinct strings**. LLVM and `llc` both accept the result
    21. **The driver's standard library is found relative to the binary, so a driver
       run from anywhere else silently loses it** — `register_roots` computes
       `dir_name(progName()) + "/../stdlib"`, so `build/frayc_driver` finds
       `stdlib/` and Stage 2's `v2`/`v3`, which live in a temp directory, do not. It is
       invisible today only because `compiler/frayc.fray` imports nothing from the
       standard library, so the two catalogs differ without the IR noticing. The day it
       imports one module, `v1` would carry a module `v2` cannot see and Stage 2 would
       stop being a fixed point for a reason that has nothing to do with the compiler.
       `check_native_driver` already passes `stdlib/` explicitly for the same reason;
       `compile_frayc` does not. Latent, not a live failure — open
    22. **A tuple used as a map key hashes by pointer, not by value** — `maps.c`'s
       header says keys may be “int, float, string, bool, tuple (any immutable)”, but
       `hash_value` has no `TAG_TUPLE` case, so a tuple falls through to `default:`
       (identity by address) and `values_equal` falls through with it. Two equal tuples
       are therefore two different keys, and since `fray_map_keys` walks slots in
       bucket order, a tuple-keyed map iterates in address order — which ASLR varies
       between runs. Latent: no map in `compiler/` or `stdlib/` has a non-string key
       today (checked — every map iteration in both is over a string-keyed map, so
       their order is FNV-1a bucket order and stable). The first tuple-keyed map would
       make a compiler's output depend on where the heap happened to land — open
    23. **List concatenation dropped every reference to the right operand's
       elements** — *resolved.* `fray_add`'s list path copies `b`'s elements into
       `result->as.list.elems[a->as.list.len + i]` and then retains
       `result->as.list.elems[i]` — a slot in *a*'s half, not the element it had just
       stored. So a's elements were retained twice and b's were stored with no
       reference at all. When the right operand is a temporary (`xs + [1]`,
       `xs = xs + [i]` in a loop) its elements die with the literal list and the
       result is left pointing at freed boxes: a use-after-free that becomes a double
       free once the result itself is released. The symptom was a `double free or
       corruption (fasttop)` abort at exit, or nothing at all — the leaf pool in
       `objects.c` recycles a freed int, so the dangling pointer kept pointing at
       plausible memory and the program printed the right answer. **49/49 golden
       cases, 8 gates, the ASan memory gate and 130 syntax-reference snippets were all
       green through it, because no case, example, benchmark or documented snippet
       ever concatenated two lists.** Found by writing a five-line smoke test that
       added a list to a list, not by any gate. Fixed to retain `b->as.list.elems[i]`,
       pinned by `tests/cases/list_concat.fray` (49th case: both operands temporary,
       in a nested block, accumulated in a loop, nested one deep, and an empty right
       operand). The lesson recorded rather than the bug: **the suite's gaps are in
       the operations nothing exercises**, and list `+` was a documented operator with
       zero coverage.       `fray-layout.md` still has no `list + list` snippet — that is
       open, and it is the same gap in the documentation.
    24. **Map methods exist only in the native engines** — `m.keys()` and `m.has()` are in
       sema's `MAP_METHODS`, the native driver implements them, and the oracle raises
       `FrayTypeError: object has no attribute 'keys'`. A map has exactly three methods
       (`keys`, `has`, `len`); `values()` and `items()` are documented in neither the
       reference nor the tutorial and do not exist anywhere. The direction of this one is
       the reverse of every other divergence recorded here: the *oracle* is behind, so a
       program using a map method cannot be differentially verified at all — the oracle is
       the reference the whole differential suite is written against, and it does not
       implement part of the language. `tests/cases/maps.fray` passes because it uses `in`,
       `len` and indexing rather than the methods. Recorded in
       `tools/check_tutorial.py` (its first `recorded` entry): compiled, run, and pinned
       by a probe. Open — either the oracle grows the two methods or the differential suite
       keeps a blind spot.

- [x] Structs, enums/variants and FFI to the C runtime: the self-hosted compiler uses all
      three (unions and pointers/arrays are still open — the language does not have them
      yet; `spec/phase8-features.md` records what was added)
- [x] `print` is variadic, in both native emitters and the runtime — the documented
      `print(a, b)` used to compile to a single-argument call that silently dropped the
      rest, and leaked every argument past the first. The runtime grew `fray_print_sep`
      and `fray_print_end`; both emitters write every argument but the last with a
      separator and the last one with the unchanged `fray_print`, so the single-argument
      case is untouched and no call needs an argument array. `print()` is a bare newline,
      as it is in the oracle and was already in the evaluator. Pinned by `print_args`
      (48th case) and by the new §17.2 of `fray-layout.md`, which `check_fray_txt.py`
      now compiles and diffs against the oracle
- [x] Stage 1: `frayc-py` compiles `frayc.fray` → `frayc-v1` — `tools/check_frontend.py
      --stage1` builds it, and each compiler module compiles, builds, runs and diffs
      against the oracle
- [x] Stage 2: `frayc-v1` compiles `frayc.fray` → `frayc-v2` — the driver reads a program
      from argv and builds its own module catalog, and `tools/frayc.sh` runs the whole
      chain (driver → `llc` → `cc`) with no Python in it
- [x] Fixed-point check: `frayc-v2` compiles `frayc.fray` → `frayc-v3`; **v2 ≡ v3 holds**,
      as identical IR and bit-identical binaries (`--stage2`). Re-run after the unboxing
      pass landed in the emitter: v1 ≡ v2 as well, so the first native compiler and the one
      it built agree on every byte
    - **Fixed point re-verified after the string-interning fix (divergence 20)**: v1 ≡ v2
      and v2 ≡ v3, IR and bit-identical binaries, and `--stage2` now takes **692 s**
      instead of 1121–1511 s — the O(n²) interning scan was most of the self-compile's
      five minutes. The driver itself shrank from 963 KB to 749 KB
    - **Open: `IR2 != IR3` has failed intermittently** — twice out of about a dozen
      `--stage2` runs, `v2` and `v3` were bit-identical yet emitted different IR for the
      same source, which cannot happen unless the result depends on something outside the
      input. What has been ruled out by measurement rather than by argument: the compiler
      is deterministic in isolation (`v2` run six times over, plus all 48 golden cases
      compiled 576 times, every run byte-identical); map iteration order (every map
      iterated in `compiler/` and `stdlib/` is string-keyed, so it is FNV-1a bucket
      order, stable — see divergence 22 for the pointer-keyed case that would *not* be);
      uninitialised `malloc` memory (`MALLOC_PERTURB_` of 1 and 85 changes nothing);
      the cycle collector (no randomness, no clock, no pointer ordering, and it fires on
      `alloc_count % INTERVAL`, so its timing is fixed); threads (the driver starts
      none); the host side (`subprocess.run` capture, and `frayc_selfhosted.py` sorts its
      directory walk); `cwd`, `argv[0]`, disk space and memory pressure. The compiler's
      own multi-module input is the only one that has ever flaked. `check_frontend.py`
      now classifies a difference before reporting it — same line count, “same lines in a
      different order (hash-table order?)” or “line contents differ” — keeps both IR
      texts in `build/` and prints the first hunk, because “different” alone does not
      name a cause and this one is worth being able to read. Not yet reproduced — open
- [x] Full golden suite passes when compiled by the self-hosted compiler: **48/48** through
      both the oracle pipeline (`--run`) and the native driver (`--native-gate`)
- [x] The self-hosted codegen is leak-clean: `tools/check_memory.py --driver build/frayc_driver`
      compiles the 48 supported golden cases with the native driver and runs them against an
      ASan runtime — **48/48 clean**. What it found and what closed it:
      a first-class call (`fray_call`'s wrapper) took a reference per argument from
      `fray_list_index` and dropped none of them, so every call through a value leaked one box
      per argument — and because a spawned thread's entry point *is* that wrapper, every thread
      leaked with it; a frame released nothing on the way out, so the last value of every boxed
      local leaked once per call (the module scope had a teardown; function scopes did not, and
      the bootstrap's compiled path never needed one because its proven locals live in raw
      unboxed slots); a match arm's binding slots own the references `fray_struct_field_get`
      returns and were released by nobody (`selfhosted_new_features` leaked its enum's field
      values on exactly this); and the self-hosted emitter had no module-scope teardown at all,
      which is what left `main`'s globals behind. The return paths must not be able to free the
      value they hand back — they cannot: a read of a slot retains, so the returned reference is
      the caller's own by the time the slots are dropped; and a rebind of a parameter stored an
      owned reference into a slot the frame never dropped, because parameter slots were borrowed
      (the caller released each argument after the call) — they are owned now, in both emitters,
      so a call hands its arguments over and the frame drops them on the way out, and
      `tests/cases/ownership.fray` pins the shapes (a plain rebind, an unused parameter, a
      first-class call)
- [x] Retire the Python bootstrap to a rebuild-only role; all future compiler work happens
      in `compiler/` — the box used to say "archive" while its own text said "kept
      deliberately", which is the contradiction this rewrites. "Archived" here means frozen
      into the emergency-rebuild role the bootstrap chain above describes, not deleted.
      What is actually true, and is what the tick claims: the bootstrap is **not on the
      release compile loop** — `tools/frayc.sh` is driver → `llc` → `cc` with no Python in
      it, and the Stage 2 gate proves the shipped compiler is a fixed point built by fray
      from fray source. It keeps three jobs: it builds v1, it *is* the oracle the gates
      diff against, and it is the test harness. It also still takes bug fixes, so the old
      absolute "no language or codegen work happens there anymore" is gone: the
      parameter-ownership fix landed in `bootstrap/codegen.py` for the same reason it
      landed in `compiler/codegen.fray`, because a rule wrong in both emitters has to be
      wrong in both. The line to hold is "not on the compile loop", not "never touched".

**Done when:** the shipped compiler is a native binary compiled by fray from fray source — Python is out of the loop.

**Done.** The compiler is `build/frayc_driver` — a native binary built from
`compiler/frayc.fray` — and it compiles a `.fray` program from argv, with its own file
I/O and module catalog (`tools/frayc.sh` drives it: driver → `llc` → `cc`). It is a fixed
point — v2 rebuilds it to a bit-identical v3, IR1 ≡ IR2 ≡ IR3 (4822 KB of IR), 830 KB of
compiler binary — and it compiles the full golden suite (43/43, leak-clean under ASan),
with `tools/check_fray_txt.py` holding it to the syntax reference section by section.
Python's remaining roles are outside the compile loop: building v1 the first time, the
oracle the gates diff against, and the test harness itself.

**Found while writing the release examples: a two-argument `range` was miscompiled.** The
driver lowered every `range(...)` to the *one-argument* runtime entry point, so
`for i in range(1, 6)` iterated `range(1)` and printed `0` — the second and third bounds
were dropped on the floor (and their boxes leaked). The special case now calls the
runtime's `fray_range3(start, stop, step)` for two and three bounds, synthesizes the
default step of `1` otherwise, and rejects a fourth argument as a codegen error. Nothing
in the suite used a two-argument `range`, which is why the gates were green; the new
`range_bounds` case pins the whole shape (a two-argument loop, a stepped loop, a negative
step, a bare `range(a, b)` as a value, `sum(range(1, 5))`) and the suite is 43 cases now.
The bootstrap emitter had the same gap — `range(a, b)` outside a `for` header reached the
one-argument entry point there too, because only the for-loop fast path understood extra
bounds — and is fixed the same way.

**Found closing divergence 15: a `return` in a `try` body did not reach the handler.** The
body polled for a pending exception after each statement, but a statement that returns ends
the block, so the poll before the return never ran and the exception stayed pending:
`try: return a / b` with `except ZeroDivisionError: return -1` returned the failed
division's `0.0` (and `try: return xs[9]` returned `None`) where the oracle returns `-1`.
Both emitters now read the pending flag on the return path itself: a return in a try body
drops its value and joins the clause dispatch when something is pending, and otherwise
parks the value in that try's slot and leaves through the finally — so the finally now runs
before the caller sees the value, in order for nested tries (the tail hands the parked
value to the enclosing try, whose own finally runs next). The bootstrap's unboxed (raw)
ABI parks its result the same way, and main (returning i32) is left out of the mechanism
entirely, which is what an earlier cut got wrong: it emitted the tail's `ret i8*` inside
`main`, and `llc` rejected four error-handling cases with "value doesn't match function
result type 'i32'". `tests/cases/try_return.fray` pins the shape: a division, an index
error, a return past a finally, one return unwinding through two nested finallys, an
exception the inner try has no clause for, a return inside an `if`, a clause's return past
the same finally, and a self-recursive function whose bootstrap build takes the unboxed
ABI.

**Audited the syntax reference against the compiler.** The reference is what the release
criterion is written against ("every example works") and nothing had ever compiled it.
`tools/check_fray_txt.py` now does: every ```fray snippet of `fray-layout.md`, and the
statements of the older flat `fray.txt` form if a checkout still carries one, is compiled
with the native chain, run, and diffed against the oracle. 128 snippets over 96 headings,
and a record has to hold: a snippet that is not recorded is compiled and diffed on every
run, a recorded one re-makes its observation (does it compile, what status, what does it
print), one recorded as unimplemented must still be *rejected* naming its stage, and one
the reference marks PROPOSED must still be rejected too — so a document full of future
syntax cannot look like a passing suite. On today's reference the split is 88 snippets
compiled, run and diffed against the oracle, 20 marked PROPOSED and checked for rejection,
and 20 recorded as unimplemented (the tensor core, classes, generics, pointers, unions,
type annotations, the `threads` module, the five bitwise spellings the reference invented). The reference's own marker is a claim the audit
checks rather than believes, which is how it reports the opposite case as well: the things
the layout calls PROPOSED that the compiler already does.

* **The input family was a codegen error.** `input`, `inputStr`, `inputInt` and
  `inputFloat`, prompts included (fray.txt's own example is `inputStr("Insert your
  name: ")`), are documented, implemented in the runtime (`fray_input`, `fray_input_str`,
  `fray_input_int`, `fray_input_float`) and implemented in the oracle — and *neither*
  emitter lowered them: `inputInt()` was "builtin 'inputInt' is not supported by the
  self-hosted codegen yet" in the native one and "unknown function 'inputInt'" in the
  bootstrap, so every documented input form was a compile error. Both lower them now, the
  runtime gained the two prompt variants the oracle already accepted
  (`fray_input_int_str`, `fray_input_float_str`), and the audit pins all four forms by
  compiling, running and diffing the program against the oracle on the *same* stdin — which
  is why it is a probe rather than a golden case: the golden runners feed no stdin, so a
  case that reads it would hang on a terminal.
* **`import random` / `random.randomInt(0, 1)` had no module to resolve to** — `stdlib/`
  was empty and the plan's own Phase 4 item for a random module was open, so §22 and §23 of
  the reference were recorded as unimplemented. That item is closed and the records are
  gone: those sections compile, run and match the oracle now.
* **`band`, `bor`, `bxor`, `bnot`, `bxnor`** appear in fray.txt and nowhere else: not in
  `spec/grammar.md`'s operator table, not in either lexer, not in the oracle, not in the
  runtime. Only the *logical* `xor`/`xnor` are implemented, so this is a spelling the
  reference invented and it needs a spec decision before it needs a compiler. Recorded.

* **The reference's own prose is checked too.** A snippet of bare strings prints nothing,
  so the claim in §7.5 that string escapes are PROPOSED cannot be settled by running the
  block: the record carries a *probe* — a program the audit compiles and runs — and the
  probe shows both engines already interpret `\n`, `\t` and `\"`. §7.5 and §20.5
  (`continue`) are the two headings whose PROPOSED marker the compiler has outgrown; the
  audit lists them on every run. The other way round, the 20 snippets that stay
  unimplemented are carried with *why* (the tensor core, classes, generics, pointers,
  unions, type annotations, the `threads` module, the five bitwise spellings), and a
  snippet that starts compiling fails the audit until its record is retired.
* **One module the reference imports does not exist.** `import threads` — the language
  spawns with the `spawn`, `join`, `joinAll`, `atomic`, `channel` and `send`/`recv`
  builtins instead, so the reference's `threads.x` spelling has no target. Recorded, with
  the bound `threads` snippets pointed at the cases that pin the builtin forms. (The other
  one, `random`, is `stdlib/random.fray` now — see the Phase 4 item and the section
  below.)

The earlier pass over the 16 sections of `fray.txt` had already found and fixed the input
family, and recorded `import random` and the five bitwise spellings:

* **The input family was a codegen error.** `input`, `inputStr`, `inputInt` and
  `inputFloat`, prompts included (the reference's own example is `inputStr("Insert your
  name: ")`), are documented, implemented in the runtime (`fray_input`, `fray_input_str`,
  `fray_input_int`, `fray_input_float`) and implemented in the oracle — and *neither*
  emitter lowered them: `inputInt()` was "builtin 'inputInt' is not supported by the
  self-hosted codegen yet" in the native one and "unknown function 'inputInt'" in the
  bootstrap, so every documented input form was a compile error. Both lower them now, the
  runtime gained the two prompt variants the oracle already accepted
  (`fray_input_int_str`, `fray_input_float_str`), and the audit pins all four forms by
  compiling, running and diffing the program against the oracle on the *same* stdin — which
  is why it is a probe rather than a golden case: the golden runners feed no stdin, so a
  case that reads it would hang on a terminal.
* **`import random` / `random.randomInt(0, 1)` had no module to resolve to** — `stdlib/`
  was empty; `stdlib/random.fray` closes it and the records are retired (see Phase 4).
* **`band`, `bor`, `bxor`, `bnot`, `bxnor`** appear in the reference and nowhere else: not in
  `spec/grammar.md`'s operator table, not in either lexer, not in the oracle, not in the
  runtime. Only the *logical* `xor`/`xnor` are implemented, so this is a spelling the
  reference invented and it needs a spec decision before it needs a compiler. Recorded.

**Found by compiling the reference line by line: the container methods.** Auditing section
by section hides the bugs *inside* a section, so the audit compiles the reference's
snippets one by one (with the definitions of the names a snippet reads carried in front of
it, the way the reference's own sequence supplies them) — which is how three defects in
`.append` and `.depend` surfaced at once:

* **`.depend` on a set threw.** The reference documents the same two method spellings on
  lists and on sets, but the native emitter lowered every `.depend` to `fray_list_depend`,
  whose tag check then reported "IndexError: pop from empty list" for a perfectly valid set
  (the bootstrap emitter already branched on the tag). The runtime now has `fray_depend`
  and `fray_append`, which dispatch to the list or set operation, and *both* emitters lower
  the methods to them — the bootstrap's inline tag branch went away with it.
* **`.append` on a non-container segfaulted.** `xs[0].append(4)` — the reference's own
  nested-list demonstration, on a list whose elements are ints — read the int's bits as a
  list header (the `as.*` union again), and the native binary exited 139. `fray_list_append`
  and `fray_set_append` now check their tag and raise a `TypeError`, so the same line is a
  clean fatal error in both engines. `tests/cases/append_type_error.fray` pins the status
  (1, not a crash's 139).
* **A set literal never deduplicated.** The native emitter built `{…}` with
  `fray_list_append`, which writes through the fields a set also uses and therefore worked —
  except for the membership check, so `{1, 1, 2}` was a three-element set here and a
  two-element one in the oracle. It emits `fray_set_append` now. `tests/cases/set_methods.fray`
  pins the set arms (dedup on append, pop on depend, and nothing that depends on iteration
  order).

One more native/oracle divergence came out of the same pass and is recorded rather than
fixed: the reference's `.depend` is written without parens in its list section, and a bare
attribute read must not pop — the native emitter popped where the oracle reads a method
reference, so the paren-less form is inert in both now (the audit compiles what it finds
and the *corrected* form, `x.depend()`, is what `ownership` pins).

**Found by answering the reference's `random` section: `int()`, `float()` and `round()`
double-freed a value that already had the target type.** The module the section asks for
wants `abs(int(n))`, and `print(abs(int(42)))` aborted with `free(): double free detected in
tcache 2` — exit 134, no output — in the native chain and the bootstrap alike. The
runtime's three pass-through conversions returned the argument *itself* when it was already
an int (`int(42)`, `round(5)`) or a float (`float(1.5)`), and both emitters release the
argument after every unary builtin call: the caller's own reference was handed back as the
result, freed as the argument, and freed again when the result was consumed. They return a
*retained* alias now — `fray_retained`, the same idiom `min`/`max` already used for an
aliased operand — and `tests/cases/math_builtins.fray` pins all three pass-through forms
alongside `abs(int(-42))`, the exact expression that surfaced it.

**Found by the memory gate: rebinding a parameter leaks one box per call.** `stdlib/random.fray`
originally swapped out-of-order bounds by assigning to its own parameters, and ASan reported
128 bytes in 2 allocations for `random_module` — as a plain program, `def f(lo, hi):\n    if hi < lo:\n        lo = hi\n    return lo\nprint(f(3, 1))` leaks exactly one box. Parameter slots were borrowed (the caller
released each argument after the call), so `gen_func_def` kept them out of `ret_slots` and a
rebind stored an owned reference into a slot nothing ever dropped. This is a *pre-existing*
compiler bug — no earlier case assigned to a parameter, which is why the gate was green — and
it is only a leak: the value is right, and the earlier "rebinding released the borrowed value"
fix is why it no longer double-frees. The module
recurses instead of swapping (`return randomInt(hi, lo)`, which consumes no generator state)
as a workaround, and the compiler fix landed separately: a parameter slot is now *owned* in
the native emitter too — `gen_func_def` binds each one into `ret_slots`, a direct call hands
its arguments over instead of releasing them, and the value wrapper does the same — so the
slot a rebind writes is dropped on the return path. `tests/cases/ownership.fray` pins the
shapes (a plain rebind, an unused parameter, a first-class call) and `check_memory.py
--driver` is clean on them; the pre-fix emitter leaked 192 bytes in 3 allocations for exactly
those functions.

**And in the Stage 0/1 emitter: a function frame never dropped its boxed locals.** The same
`random_module` run was clean through the native driver and 320 bytes dirty through the
Python emitter — `check_memory.py --all` (the emitter `tools/run_tests.py` compiles every
case with) is the form that saw it. `_emit_func_return` released `_frame_slots`, which held
only the *parameters*; a function's boxed locals were never dropped at all. Nothing reported
it before because a local has to be boxed *and* hold a leaf at the end of the function to
show: inference puts proven int/float locals in raw unboxed slots, and a container is
GC-tracked, so the case had to be a local holding an int that inference could not prove —
`i = len(xs) - 1`, `j = randomInt(0, i)` and `t = xs[i]` in `shuffle` are exactly that (3
leaks, one per local). The frame's slot list now includes every boxed local the frame
creates (`_ensure_alloca` and `_predeclare_boxed_vars` register them; the first-assignment
path allocates through `_ensure_alloca`, so it lands in the entry block and is registered
too), and `_emit_func_return` drops them all — the comment claiming "function frames need no
equivalent: their locals are released by the return path" was the belief, not the code. All
50 programs `check_memory.py --all` covers are clean again. Both emitters now own
their parameter slots (the Python one already did — its boxed parameters are `_frame_slots`
entries and `_store_releasing` drops the old value on a rebind), so a rebind leaks nothing on
either side; the convention is the same in both and each emitter's comments say so.

**A module-level name is a global, and a function that assigns it writes the global.** The
first attempt at giving the driver the standard library as a search root counted its argv
with a top-level `i = 1`. `collect_func_locals` documents the rule it broke — "a name that
is a module global is *not* a local: assigning it from a function writes the module
variable, which is what the oracle and the compiled bootstrap both do" — and `i` is the
loop counter of `build_catalog`, `report_errors` and most of `codegen.fray`. The driver
built and then hung (the catalog walk never terminated) or died with `IndexError: list
index out of range`, and the oracle hangs on the same shape, so this is the language
working as designed and the driver being wrong. The roots are registered by a function now,
whose counter is a local; the comment there says why.

The audit also reports the reference's *own* defects, and they stay in the reference and get
reported rather than quietly corrected — a reference that has silently drifted is how this
whole class of gap started. Two of them are recorded that way: `x.depend` in §16.1 is
missing the parens `x.append(1)` has on the line above it, so as an attribute read it is
inert in both engines (the call form, `x.depend()`, is what `ownership` pins); and two
snippets assume a name the section never defines (§17.2 prints the tuple of §16.5, §18.3
calls the `add` of §18.2), so the audit carries those definitions in front of them
explicitly instead of guessing them from a chapter whose `x` may be a list, a number or a
function.

**Audited the tutorial against the compiler.** `docs/fray_by_example.md` is the document a
newcomer reads first and the release criterion is written against it, and like the syntax
reference nothing had ever compiled it. `tools/check_tutorial.py` does, through the *same*
engine as `check_fray_txt.py`: `Snippet`, the record kinds, `apply_records`, `check_snippet`
and the report are imported rather than copied, and the two share a `files` mechanism so a
section that spans more than one file (the tutorial's "here is the module, here is the
program") can be audited at all. All 45 snippets of the 90 fences were untagged; they are
tagged ```fray now, and an untagged non-empty fence is a *failure* rather than something
the parser skips, so a snippet can never again be invisible to the audit.

It found **thirteen of forty-five snippets did not work** — and it found a compiler bug the
documentation was only the messenger for.

* **The compiler does not check call arity.** `def power(base, exp)` called as `power(3)`
  is an error in the oracle ("takes 2 argument(s) but 1 given") and in the self-hosted
  sema *nothing*: `scope_define` records a function as `"?"`, so there is no signature to
  check against, and the emitter writes a one-argument call to a two-parameter function.
  LLVM then rejects the module with `'@power' defined with type 'i8* (i8*, i8*)*' but
  expected 'i8* (i8*)*'` — a diagnostic that names no fray source, no line and no cause.
  The tutorial's "Default parameters (using if/else)" section was built on exactly this
  and never worked; it is rewritten, and **the arity check itself is now implemented** —
  see "The compiler checks call arity" below.
* **The tutorial taught shadowing a builtin as if it were ordinary.** Four sections assign
  `pi` or `e`, which are language constants, and the driver rejects them with
  `SemanticError: invalid assignment target` — as it should. Renamed, with a note saying why
  (`Arithmetic`'s `e = 10 - 3`, `Boolean logic`'s `e = True xnor True`, `Variables`'s
  `pi = 3.14`, `Option and Result`'s `e = Err(...)`).
* **The Scope section documented a rule the language does not have.** It said a function
  assigning a module-level name "creates a new local x" and that the global stays `10`. Both
  engines print `20 20`: a name first assigned at the top level is a module global and a
  function that assigns it writes the global — the rule `collect_func_locals` states and
  `plan.md` records. **The audit passed this one**: it compares engines, and they agree. A
  comment that is wrong about both is invisible to a differential check, which is an
  argument for the record-the-prose half of this work rather than only the diff half.
* **A method call needs a name, not a literal.** `"hello"[0]`, `"HELLO".lower()` and
  `"a,b,c".split(",")` are parse errors in both parsers (`expected newline, got LBRACKET`,
  `expected RPAREN, got DOT`). The Strings section binds each receiver first now.
* **`for c in "hello"`** is divergence 2 — iterating a string is unimplemented and sema
  rejects it with a named diagnostic. Dropped from the tutorial with a note saying it is not
  implemented yet, rather than left as a line a reader's first program dies on.
* **`m.values()` does not exist.** A map has three methods: `keys()`, `has()`, `len()`.
  The section teaches those and says what to use instead.
* **`del xs[1]` on a list raises `TypeError: not a map`.** `del` with a subscript works on
  maps only. The section says so.
* **`extern int abs(int x)` is rejected** — `abs` is a builtin and there is nothing for the
  linker to resolve. The example uses `labs` and explains the rule.
* **The module section was internally inconsistent**: the file was commented
  `# math_utils.fray:` and imported as `math_lib`. Fixed, and the second snippet now gets
  the first written beside it as a companion file.
* **A call's arguments must fit on one line**, so the enum-interpreter example's
  `Expr.Mul(\n ... \n)` was a parse error. Rebuilt one node per line, which reads better.
* **Map methods are native-only — a new divergence (24).** `m.keys()` and `m.has()` work
  in the driver and sema (`MAP_METHODS`) but the oracle raises `FrayTypeError: object has no
  attribute 'keys'`. The oracle is the differential reference, so a snippet using them can
  never be diffed — the Maps section is the audit's first `recorded` entry for a reason the
  reference audit never had: the *oracle* is behind, not the language. It is compiled, run
  and pinned by a probe.

Two engine gaps fell out of making the harness reusable and are now available to either
document: `Snippet.files` (companion sources in the work directory) and a summary line that
reports how many snippets were diffed against the oracle versus run with their output pinned
— the two are different claims and the report used to conflate them.

### The compiler checks call arity

The open item above is closed. `power(2)` for a two-parameter `power` is now
`SEMA ERROR: file:7:7: 'power' takes 2 argument(s) but 1 given` from the self-hosted
front end — the oracle's message and the oracle's `FrayTypeError` wording — instead of a
malformed LLVM module.

Two things had to be true for the check to be safe rather than merely present.

**Arities are collected before any body is checked.** A function may call one defined
*later* in the file; that is legal, and the compiler's own modules lean on it. Recording
each function's parameter count as its body was walked would leave the count missing for
exactly the calls a reader most wants checked, and a lookup that found nothing would
either skip silently or crash. `collect_arities` walks the top-level body once to fill
`st.func_arities`, and the call site reads from it. `forward_call_arity_ok` is a probe
precisely because the pre-pass is the part that could regress into a false rejection.

**A name that is not a top-level function is not checked.** `resolve_name` only reports
that a name resolves, so a local holding a function — `g = add`, then `g(1, 2)` — looks
identical to a direct call. It is not: that call's arity belongs to whatever the value is
at run time, and the runtime's `fray_call` already reports that. `resolves_to_function`
requires a module-level binding with no nearer one shadowing it.

The bootstrap had the same gap with a *worse* symptom, and it is worth recording why,
because it is not the failure the open item described. `codegen.py` already called
`_check_arity` — but it called it *after* the unboxed fast path, and `_raw_call` returns
first. `_raw_call` builds a wrapper that reads whatever arguments it is given out of a
list, so a wrong count never reached LLVM as a module error there: it bound fewer
parameters than were declared and **the program ran and printed a wrong answer**.
`power(2)` printed `1`, because the missing `exponent` left the loop empty. A wrong answer
is worse than a rejected module, and a rejected module is worse than a diagnostic; the
check now runs before the fast path, so all three agree.

`call_too_few_args`, `call_too_many_args` and `forward_call_arity_ok` are probes, so the
rejection, the rejection in the other direction, and the forward reference that must keep
working are all re-checked on every run.

#### What writing it exposed: a latent codegen bug

The first version of this change passed the oracle, the probes and 49/49 golden cases, and
**broke the self-build**. `stage2` failed with LLVM IR that had been corrupted in transit —
`bitcaSt` for `bitcast`, `cahl` for `call`, at a different offset each run. The diagnostic
names no source line and no cause, which is the signature of a use-after-free or a buffer
overwritten during emission rather than of anything in the check.

**The first conclusion drawn here was wrong, and the correction matters.** The obvious
suspect was the `--backend llvmlite` driver used for the self-build, so the matrix was run:

| source | object backend | stage2 |
|---|---|---|
| unmodified | `llc` (the shipped driver) | pass |
| unmodified | `llvmlite` | pass |
| with the check, inline in `check_expr` | `llvmlite` | fail, 2 of 2 |

That looked conclusive, so the check was hoisted into `check_call_arity`, on the theory that
nesting it inside `check_expr` — the compiler's largest function — was the trigger. **`stage2`
then passed, which was read as the fix. It was not.** Re-running the *inline* version
afterwards, unchanged, with the same driver and the same tree, it passes too. The
inline-versus-hoisted distinction was correlation drawn from three failures that the bug
happened to arrive with. The corruption is intermittent and its cause is still unidentified;
hoisting was a change that happened to land between failures, not a repair. It is kept
because a one-line call at the call site is the better shape anyway, not because it fixes
anything.

What *is* solid: a later diff of two drivers over identical source caught a concrete
instance — `br i1 %659, label %if_then_4217, nabel %if_else_4218`, one character of a string
literal wrong in the emitted text. Same shape as the original failures (a single character
substituted, no structural damage), which is why a parse error is the only symptom.

Ruled out along the way, each by constructing it directly and validating the IR or running
under AddressSanitizer: the unboxed fast path, list-element reads under allocation churn,
long concatenation chains with intermediates alive in a list, the O(n²) assembly loop on its
own, and an ASan build of the driver itself (clean, three runs). The one structural clue is
that `objects.c` pools and *recycles* freed leaf values — so a use-after-free here returns a
plausible object with the wrong contents instead of crashing, and a single substituted
character is exactly what a recycled slot looks like. That is a lead, not a diagnosis.

**The defect is still open.** It is the same class `check_frontend.py --probes` was built
for, and it is recorded rather than worked around: `stage2` validates every IR with LLVM and
caught this; nothing else in the suite would have.

#### The O(n²) IR assembly, fixed

Chasing this did turn up a real, provable defect next door. The module text was assembled by
concatenating into one accumulator:

    result = result + b.lines[i] + "\n"

Each iteration copies everything accumulated so far, so N lines cost O(N²) bytes copied.
`frayc.fray` emits ~5.5 MB of IR, and the driver took **253 seconds** to compile it. The
lines are now folded pairwise — a balanced merge tree, O(N log N) — and the same driver
compiles the same source in **2 seconds**, a 126x reduction, with byte-identical output.
(Verified by diffing the old and new drivers' IR over the same tree, since `stage2`'s fixed
point compares a compiler against itself and would not notice a changed output.)

That is a performance fix, not a cure: it removes most of the alloc/free churn this workload
creates, which is where the corruption is most likely to live, but it is not a root cause and
is not claimed to be one.

### Phase 9 — First downloadable release (v0.1)
- [x] Release packaging per OS: single executable + stdlib + docs (installer or tarball) —
      `tools/package_release.sh` builds `dist/fray-<version>-<platform>.{tar.gz,zip}` out of
      `build/frayc_driver`, `runtime/libfrayrt.a` and the chain around them: `bin/fray`
      (`fray run` / `build` / `ir` / `version`), `bin/frayc` (driver → `llc` → `cc`),
      `bin/frayc_driver`, `runtime/` (the archive plus the sources and Makefile it came
      from), `compiler/` (the compiler's own source), `examples/` (13 programs), `docs/`,
      `spec/`, `tests/cases/`, `VERSION` and `QUICKSTART.md`. `bootstrap/` and `tools/` ship
      too, as the *rebuild* path — the only Python in the archive, and never in the compile
      loop. A previous version of this script shipped the Python bootstrap as `bin/frayc`
      with a "install Python 3.8+" quick start; that is gone. Verified on this host by
      extracting the archive into a clean directory: `fray run`, `fray build`, `fray ir` and
      `tools/check_cases.sh` (43/43 at the time, re-verified at 49/49 after the
      `list_concat` fix) all work with `python`, `python3` and `python3.11`
      shadowed by stubs that exit 127, every `examples/*.fray` compiles and runs, and the
      only `python` string under `bin/` is inside a failure message. The macOS and Windows
      branches are scripted but unverified — there is no such host here. The archive carries
      `stdlib/` at its root — `bin/frayc_driver` looks for it beside its own directory, so
      `import random` works from any working directory inside an extracted package — and it
      carries no REPL.
- [x] Audit the syntax reference against the compiler — `tools/check_fray_txt.py` compiles
      every ```fray snippet of `fray-layout.md` (128 of them, over 96 headings) through the
      native chain, runs it, and diffs it against the oracle; the statements of the older
      flat `fray.txt` form are handled the same way if a checkout carries one. A snippet
      that cannot simply work carries a record here — unimplemented (and the driver must
      still reject it, naming its stage), observed-but-divergent (compiles, exits and
      prints exactly what the record says, probe included), or "the reference calls it
      PROPOSED and the compiler does it anyway". Snippets under a PROPOSED heading must
      stay rejected. It fails if a heading leaves the reference, if a pinning case leaves
      `tests/selfhosted_supported.txt`, or if any record stops matching what happens — so
      the reference cannot drift from the compiler in either direction again. What it
      found is the "Audited the syntax reference" note below
- [x] "fray by example" tutorial covering every `fray.txt` feature — the error-handling
      section taught two constructs no engine implements (`raise ValueError("…")` and
      `except ValueError as e`); `raise` is absent from `fray.txt`, both parsers and every
      test case, so that snippet was the only place it existed. The section now uses the
      real forms (`try: v = xs[9]` / `except IndexError:`, a Result value for a function
      that wants to report failure). The audit the item asks for is `tools/check_tutorial.py`:
      it compiles, runs and diffs every snippet in `docs/fray_by_example.md` against the
      oracle, through the same engine as the syntax-reference audit (the snippet model,
      the record kinds, `apply_records` and the report are imported from
      `check_fray_txt.py`, not copied). What it found is the "Audited the tutorial" note
      below
- [ ] REPL (AOT makes this harder — start with a fast compile-and-run snippet loop)
- [ ] Tag v0.1.0

**Done when:** a user downloads one file, runs `fray run program.fray`, and every `fray.txt` example works.

**Done when:** a user downloads one file, runs `fray run program.fray`, and every `fray.txt` example works.

### Phase 10 — Tensor core + autodiff (CPU-first) ⭐
- [ ] `tensor` builtin: n-dimensional, dtypes `f32 f64 i32 i64 bool`, row-major + strides, views/slicing
- [ ] Broadcasting (NumPy rules); elementwise ops ride normal call machinery (runtime dispatch —
      deliberately *no* special compiler support, so it survives self-hosting for free)
- [ ] Reductions: `sum mean max min argmax ...`; matmul: naive → blocked → SIMD (SSE/AVX/NEON)
- [ ] Reverse-mode autodiff: eager tape, `t.grad`, `zero_grad()`, `grad(f)` helper
- [ ] Gradcheck suite: every op vs numerical differentiation, every dtype
- [ ] Performance target: within 2× of NumPy on matmul and elementwise benchmarks

**Done when:** logistic regression on MNIST-style data trains in fray with correct gradients, faster than pure-Python NumPy loops.

### Phase 11 — ML standard library ("better at ML" beyond tensors)
- [ ] Optimizers: SGD, momentum, Adam/AdamW
- [ ] Losses: MSE, cross-entropy; weight init (Xavier, He)
- [ ] `nn` module: Linear, activations, sequential container, training-loop helper
- [ ] Data: CSV/JSON loading, shuffle, batching, train/test split; seeded `random` for reproducibility
- [ ] Metrics: accuracy, precision/recall, confusion matrix
- [ ] End-to-end example: feedforward net + small CNN trained entirely in fray

**Done when:** a newcomer trains a working classifier in <30 lines of fray with no imports beyond stdlib.

### Phase 12 — GPU backend (later)
- [ ] Backend abstraction in the tensor core (CPU reference implementation already done in Phase 10)
- [ ] Metal (macOS) first — no SDK lock-in; CUDA next
- [ ] Device memory manager, async copy/compute overlap
- [ ] Same autodiff tape works across devices

### Phase 13 — Ecosystem & polish
- [ ] Package manager + registry format (`import` resolves packages)
- [ ] LSP server (completion, hover, diagnostics) and a formatter
- [ ] PGO/LTO release builds; more SIMD (AVX-512); `f16`/`bf16` dtypes
- [ ] Rewrite remaining C runtime hot paths in fray

---

## 4b. Future expansion — heavy (post-v1.0)

Everything below is intentionally **not** in v1.0. It lives here so the roadmap can grow
without rewriting the plan. Prioritise freely once the core is stable.

### Language features
- [ ] **Generics / parametric polymorphism** — `list[T]`, `fn max[T](a: T, b: T) -> T`
- [ ] **Pattern matching** — `match x:` with destructuring, guards, exhaustiveness checking
- [ ] **Macros / compile-time code generation** — AST-level metaprogramming, `comptime` blocks
- [ ] **Operator overloading** — user-defined `+`, `*`, `==` via dunder-style methods
- [ ] **Algebraic data types / sum types** — `type Shape = Circle(radius) | Rect(w, h)`
- [ ] **Traits / protocols** — structural or nominal interfaces, static dispatch option
- [ ] **Null safety** — `Option[T]` / `Result[T, E]` replacing nullable by default
- [ ] **Contracts / preconditions** — `requires`, `ensures`, runtime-checked invariants
- [ ] **Gradual type annotations** — optional `x: int` hints, enforced at compile time when present
- [ ] **List comprehensions / generators** — `[x * 2 for x in range(10)]`, `yield`
- [ ] **Decorators** — `@cache`, `@override`, user-defined function wrappers
- [ ] **Closures with captured environment** — confirm or extend Phase 0 closure semantics
- [ ] **First-class modules / namespaces** — `use` syntax, re-export, sub-packages

### Performance & compilation
- [ ] **JIT tier** — hot-path recompilation (trace or method JIT) alongside AOT
- [ ] **Escape analysis** — stack-allocate short-lived objects, elide boxing where provable
- [ ] **Whole-program optimisation** — link-time codegen, cross-module inlining
- [ ] **Profile-guided optimisation (PGO)** — instrumented builds + feedback-driven recompile
- [ ] **WebAssembly target** — `fray build --target wasm32`
- [ ] **Cross-compilation** — single-host builds for Linux/macOS/Windows/ARM/RISC-V/WASM
- [ ] **Interpreter mode** — fast startup for scripts, no compile step (optional)

### Runtime & concurrency
- [ ] **Tracing GC** — replace refcounting for latency-sensitive workloads
- [ ] **Green threads** — user-space scheduled fibers (M:N), composable with OS threads
- [ ] **Software transactional memory (STM)** — composable concurrent state without locks
- [ ] **Persistent / immutable data structures** — structural sharing for cheap snapshots
- [ ] **Actor model** — optional isolation discipline on top of threads
- [ ] **Async streams / async iterators** — `for await x in stream:`

### ML, scientific computing & interop
- [ ] **Distributed training** — data-parallel and model-parallel across machines/GPUs
- [ ] **Sparse tensors** — CSR/CSC formats, sparse autodiff
- [ ] **Custom CUDA/Metal kernels** — embedded GPU shaders, launch from fray
- [ ] **Model serialisation** — save/load trained models (ONNX, custom binary)
- [ ] **NumPy / PyTorch / TensorFlow interop** — zero-copy via DLPack or buffer protocol
- [ ] **Automatic mixed precision (AMP)** — fp16/bf16 training with loss scaling
- [ ] **Quantisation** — INT8/INT4 inference support
- [ ] **RL / reinforcement learning primitives** — environments, agents, replay buffers

### Developer experience
- [ ] **Interactive REPL** — compile-and-run snippet loop (v0.1 starts this, expand later)
- [ ] **Debugger** — breakpoints, step-through, variable inspection (GDB/LLDB bridge or native)
- [ ] **Profiler** — CPU/memory/concurrency flame graphs, built into the compiler
- [ ] **Formatter** — opinionated code formatter (`fray fmt`)
- [ ] **Linter** — style and correctness warnings
- [ ] **Type checker** — incremental, gradual, powered by the inference pass
- [ ] **Documentation generator** — `fray doc`, Javadoc-style from source + comments
- [ ] **LSP deep integration** — go-to-definition, find-references, rename, quick-fixes

### Platform & deployment
- [ ] **Mobile targets** — iOS / Android via cross-compilation or embedded runtime
- [ ] **Embedded / RTOS** — bare-metal or freestanding subset, no GC, no threads
- [ ] **Kernel / OS modules** — `no_std`-style for systems programming
- [ ] **Package registry & hosting** — `fray pkg`, versioned, signed, mirrors
- [ ] **Lock files & reproducible builds** — hermetic dependency resolution

### Community & governance
- [ ] **RFC process** — formal proposal workflow for language changes
- [ ] **Governance model** — BDFL, steering committee, or foundation
- [ ] **Contributing guide + code of conduct**
- [ ] **Release cadence** — LTS policy, semver, rolling nightly channel
- [ ] **Education** — official tutorials, exercise platform, teaching license

> **Rule of thumb:** if a feature belongs in the language but isn't required for v1.0, add a
> bullet here with a checkbox. Nothing in this section blocks the milestones above.

---

## 5. Cross-cutting: testing & verification

- **Golden tests** — `.fray` files + expected stdout, run through the oracle AND the compiled binary; they must
  always agree. An optional `<case>.exit` file adds the expected status for cases whose point is that
  the program *stops* — an exception no clause handled is a fatal error in both engines, and the
  oracle's collected output is lost when it dies (divergence 8), so `uncaught_exception` and
  `uncaught_keyerror` assert an empty stdout plus status 1. A case whose exception is wrongly cleared
  prints the line after it and exits 0, which fails the case twice over
- **Runtime header check** — `tools/check_runtime_symbols.py` compiles the library's translation
  units — read out of `runtime/Makefile`'s `RUNTIME_SOURCES`, so both runtime gates hold one notion
  of what the library is, and a unit that stops being built stops answering for prototypes — and
  reads their symbols with `nm` (local `static` ones included), then fails in both directions:
  a non-inline `fray_*` function `runtime/runtime.h` declares that no translation unit defines —  a prototype no definition backs compiles fine and only surfaces as a link error at somebody else's
  first call; thirteen had accumulated before the gate existed — a file-scope `fray_*` function the
  sources define that the header never declares, which is API that leaked out of its file; and, in
  the other direction from the header entirely, every runtime entry point the bootstrap and the
  self-hosted emitter call. The last one found `self._call("fray_call", ...)` sitting in the
  bootstrap: a call site for a name that exists nowhere, which had made every program that reached
  that path die as a bare `KeyError` inside the compiler. That
  second direction immediately found two: `fray_weak_registry_lock_impl` and its unlock, mutex
  plumbing private to `objects.c` whose only callers were the wrappers beside them, now folded into
  those wrappers. Running the check the way the linker sees it, rather than pattern-matching C text,
  is deliberate: a call site looks like a definition to a regex, and that would hide the first bug
  the check is for
- **Runtime source lists** — `tools/check_runtime_sources.py` owns the other half of the same
  question: the library is described twice (`runtime/Makefile`'s `RUNTIME_SOURCES` and
  `bootstrap/codegen.py`'s), and a translation unit added to one list links in some builds and not
  others — an undefined symbol on whichever path was forgotten, long after the file was added.
  Every `runtime/*.c` that is not a program must be in both lists, every entry in either must name
  a file that exists, and a program (a `.c` defining `main`: `test.c`, `coro_drv.c`) must be in
  neither. Programs are identified by compiling the file and reading `main` out of its symbols
  rather than by grepping its source. The header check then reads the Makefile's list from this
  tool's parser instead of globbing the directory, which is what makes a dropped translation unit
  also stop hiding its symbols: with `maps.c` removed from the build, the symbol gate reports all
  nine `fray_map_*` prototypes as unbacked and points at the list
- **Self-hosted frontend gate** — `tools/check_frontend.py` pins the invariants that make a
  subset compiler honest: it terminates, unsupported syntax is a diagnostic (not a hang, not an
  internal error), and what it does compile matches `.expected`. Runs the golden set plus an
  inline matrix of unsupported syntax; `tests/selfhosted_supported.txt` catches coverage
  regressions. `--stage1` compiles the compiler's own modules with it, verifies the IR with
  LLVM, builds and runs them, and diffs the compiled modules that have a driver
  (`tests/selfhost/`) against the oracle
- **Bootstrap fixed-point test** — CI runs the three-stage compile and diffs Stage 2 vs Stage 3
- **Gradcheck** — autodiff vs numerical gradients for every op and dtype
- **Concurrency stress** — TSan job + randomized scheduling, run nightly
- **Fuzzing** — random program generator; the parser/round-trip/codegen must never crash the compiler
- **Benchmarks** — tracked in CI; regressions fail the build

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Dynamic typing + AOT = slow if everything stays boxed | Type inference & specialization is a first-class phase (5), not an afterthought |
| Self-hosting needs features the language doesn't have yet | Phase 8 lists "compiler-required" features (structs, unions, FFI) to add before the rewrite |
| A subset compiler can silently emit wrong code for what it cannot compile (a struct-field default built as `None`, a map handed to the list accessor) | **Gate added.** `tools/check_frontend.py` fails on hangs, internal errors, and any accepted program that disagrees with the oracle; `tests/selfhosted_supported.txt` fails on coverage regressions. Its first run found the struct-default and map-iteration divergences — both now diagnosed or fixed, the rest listed in Phase 8 |
| Self-hosting stalls on a codegen bug that only the compiler's own source exercises | **Measurable now.** `--stage1` compiles the compiler's own modules in CI (4/4: IR verified by LLVM, built, running, three differentially checked against the oracle), so a regression in what the compiler can compile about *itself* fails the build rather than being discovered later. It has already found four such bugs (Phase 8) |
| Built as separate modules, the compiler defines the same top-level names in several files (`get_errors`/`clear_errors`, `node_type`/`node_get`, one private `errors` list), so a flat build would silently share them | **Closed by a module system.** `compiler/link.fray` resolves imports and renames each module's colliding names to `<module>_<name>`; `--single-unit` builds `compiler/main.fray` (which imports the compiler modules) into one binary and diffs it against the oracle, and the golden `import_test` passes through the same linker |
| GIL-free shared memory is a correctness minefield | Per-object locks + biased refcounts follow the proven free-threaded-CPython (PEP 703) playbook; TSan in CI from day one |
| ML scope creep delays the language | Tensor core is runtime-side (C kernels) — it never blocks compiler milestones and survives self-hosting unchanged |
| LLVM bloats release binaries | Static-link with `lld`; consider a smaller backend later if needed |
| Boxed container elements cap throughput — `list_sum` trailed CPython (0.6×) on a million one-at-a-time boxes | **Closed.** Two allocation sources removed: the runtime's root stack is a per-thread array instead of two `malloc`/`free` pairs per push, released leaf boxes go to a per-thread pool (freed at thread exit), and the `for` loop takes an unboxed `fray_len_raw` bound instead of allocating a box on entry *and* every iteration to re-check the (possibly mutated) length. `list_sum` 0.145s → 0.072s (2.0×); ~17% of that is the runtime, ~40% the loop bound. Whole-list element storage is still one box per element — the next step if containers need more |

## 7. Milestone summary

| Milestone | Deliverable |
|---|---|
| M0–M1 | Spec + frontend that parses all of `fray.txt` |
| M2 | Reference oracle |
| M3 | Native "Hello, fray!" |
| M4 | Full core language, compiled, oracle-verified |
| M5 | Faster than CPython on benchmarks |
| M6 | True multithreaded parallelism (no GIL) |
| M7 | Coroutines + async I/O |
| M8 ⭐ | Self-hosted compiler (Python retired) |
| M9 | **v0.1 downloadable release — runs its own code** |
| M10 ⭐ | Tensor core + autodiff |
| M11 | ML stdlib |
| M12+ | GPU, packages, tooling |
| **M13+** | **See §4b — heavy future expansion (generics, JIT, WASM, distributed, mobile, …)** |

## 8. Immediate next steps

1. Confirm the open questions in §2 (one message is enough)
2. Write `spec/grammar.md` — EBNF derived from `fray.txt`
3. Start Phase 1: lexer + parser in Python
4. Seed the golden-test harness with the `fray.txt` examples as the first test files
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            