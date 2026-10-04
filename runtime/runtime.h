/**
 * fray runtime header — value boxing, type descriptors, memory management.
 *
 * Every fray value is a tagged pointer to a heap-allocated object.
 *
 * Phase 5 object header v2 (offsets are part of the stable ABI):
 *   0   uint8   tag            type tag (FrayTag)
 *   1   uint8   gc_flags       FRAY_GC_* mask (cycle-heap, colors, weak)
 *   2   uint16  generation     GC generation (0 = young, 1 = old)
 *   4   int32   weak_refs      external weak-reference count
 *   8   int64   refcount       atomic strong reference count
 *  16   const FrayTypeInfo *type
 *  24   struct FrayObj *gc_prev, *gc_next   (generation list links)
 *  40   union as              payload
 *
 * Strong references from within the GC heap (a list holding an element) are
 * *not* counted in `refcount`; those edges are traced by the cycle collector.
 * Strong references from roots (stack, globals, out-of-heap) *are* counted.
 * See runtime/cycles.c for the invariants and collector.
 *
 * Phase 6 concurrency model (see threads.c):
 *  - Every fray thread has its own GC space (generation lists, root buffer,
 *    counters, scratch) and its own root stack + exception state.
 *  - A collection takes the world lock plus the shard locks and every space
 *    lock: mutators are excluded by locks, not paused, so fray_gc_safepoint
 *    stays a no-op kept for the generated-code ABI.
 *  - Each space trials its own candidate roots (objects seen losing a strong
 *    reference while still positive), one space at a time.
 *  - Shared objects (refcount > 1 or owner != self) get thin striped locks
 *    around structural mutation; refcounts stay lock-free atomics.
 *  - A container the kill phase condemned is detached from its generation
 *    list and handed to the pending-free queue; freeing it as a live object
 *    would release its elements a second time. See cycles.c for the queue and
 *    fray_gc_free_condemned. Non-containers still free immediately.
 */

#ifndef FRAY_RUNTIME_H
#define FRAY_RUNTIME_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <setjmp.h>
#include <stdatomic.h>
#include <stdio.h>

/* Threading primitives. Some MinGW toolchains lack C11 <threads.h>, so
 * we use pthreads directly on Windows; POSIX systems may use either. */
#include <pthread.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ── Exception support ── */
extern jmp_buf fray_jmp_buf;
extern int fray_in_try;
extern int fray_exception_type; /* 0=none, 1=TypeError, 2=ValueError, 3=IndexError */

/* Phase 6: exception state is per-thread. The externs above remain for the
 * main thread's ABI stability; generated code must use the accessors. */
void     fray_exc_state_bind(void);        /* attach TLS state (idempotent) */
jmp_buf *fray_exc_jmp_buf(void);
int     *fray_exc_in_try(void);
int     *fray_exc_type(void);

/* Exception type constants */
#define FRAY_EXC_NONE      0
#define FRAY_EXC_TYPE      1
#define FRAY_EXC_VALUE     2
#define FRAY_EXC_INDEX     3
#define FRAY_EXC_NAME      4
#define FRAY_EXC_ZERO_DIV  5
#define FRAY_EXC_RUNTIME   6
#define FRAY_EXC_KEY       7
#define FRAY_EXC_OVERFLOW  8

/* ── GC object flags (gc_flags) ── */
#define FRAY_GC_HEAP       0x01u /* allocated in the GC heap (tracked)        */
#define FRAY_GC_COLOR_MASK 0x06u
#define FRAY_GC_WHITE      0x00u /* unmarked this cycle                       */
#define FRAY_GC_GRAY       0x02u /* candidate / on the scan worklist          */
#define FRAY_GC_BLACK      0x04u /* externally alive this cycle               */
#define FRAY_GC_DEAD       0x06u /* scanned garbage, awaiting the kill phase  */
#define FRAY_GC_WEAKABLE   0x08u /* weak references may point at this object  */
#define FRAY_GC_LINKED     0x10u /* object is on a generation list            */
#define FRAY_GC_ATOMIC     0x20u /* atomic box: lock payload accesses         */
#define FRAY_GC_QUEUED     0x40u /* container waits in the deferred-free queue */
#define FRAY_GC_CANDIDATE  0x80u /* listed in its space's candidate buffer     */

/* ── Type tags ── */

typedef enum {
    TAG_NONE = 0,
    TAG_INT,
    TAG_FLOAT,
    TAG_BOOL,
    TAG_STRING,
    TAG_LIST,
    TAG_TUPLE,
    TAG_SET,
    TAG_FUNCTION,
    TAG_COROUTINE,
    TAG_CHANNEL,
    TAG_STRUCT,
    TAG_MAP,
    TAG_OPTION,
    TAG_RESULT,
} FrayTag;

typedef struct FrayObj FrayObj;
typedef FrayObj *FrayValue;

/* ── Type descriptor ──
 * The header is laid out so a tracing GC can replace the refcount field
 * without breaking the ABI: `type` names the object and exposes the hooks a
 * future tracing GC needs (trace + finalize). v1 leaves them null.        */

typedef struct FrayTypeInfo {
    const char *name;
    size_t      size;      /* sizeof(FrayObj) + payload extra bytes       */
    void      (*trace)(FrayObj *self);    /* visit outgoing strong edges */
    void      (*finalize)(FrayObj *self); /* free payload, not the object */
} FrayTypeInfo;

/* ── Value representation ── */

struct FrayObj {
    /* header (see layout comment above) */
    uint8_t                 tag;
    uint8_t                 gc_flags;
    uint16_t                generation;
    int32_t                 owner;       /* GC space / thread id, -1 = none */
    _Atomic int64_t         refcount;
    const FrayTypeInfo     *type;
    struct FrayObj         *gc_prev;   /* generation list links          */
    struct FrayObj         *gc_next;

    /* payload */
    union {
        int64_t    i;
        double     f;
        bool       b;
        struct {
            char  *data;
            size_t len;
        } str;
        struct {
            FrayValue *elems;
            size_t len;
            size_t cap;
        } list;
        struct {
            FrayValue *elems;
            size_t len;
        } tuple;
        struct {
            FrayValue *elems;
            size_t len;
            size_t cap;
        } set;
        struct {
            char *name;
            void *code;  /* function pointer */
            int   arity;
        } func;
        struct {
            void *fiber;      /* platform fiber handle                    */
            void *state;      /* CoroState (coroutine.c)                  */
        } coro;
        struct {
            void *chan;       /* ChannelState (coroutine.c)               */
        } chan;
        struct {
            void *state;      /* StructState (structs.c)                  */
        } st;
        struct {
            void *state;      /* MapState (maps.c)                       */
        } mp;
        struct {
            uint8_t is_some;  /* 1 = Some(val), 0 = None */
            FrayValue val;    /* the wrapped value (NULL for None) */
        } opt;
        struct {
            uint8_t is_ok;    /* 1 = Ok(val), 0 = Err(err) */
            FrayValue val;    /* the wrapped value */
            FrayValue err;    /* the error value (NULL for Ok) */
        } res;
    } as;
};

/* Header accessors (stable ABI layout — see comment at top). */
#define FRAY_OFF_TAG        0
#define FRAY_OFF_FLAGS      1
#define FRAY_OFF_GENERATION 2
#define FRAY_OFF_OWNER      4
#define FRAY_OFF_REFCOUNT   8
#define FRAY_OFF_TYPE       16
#define FRAY_PAYLOAD_OFF    40
#define FRAY_HEADER_SIZE    FRAY_PAYLOAD_OFF

/* ── Constructors ── */

FrayValue fray_none(void);
FrayValue fray_int(int64_t v);
FrayValue fray_float(double v);
FrayValue fray_bool(bool v);
FrayValue fray_string(const char *s, size_t len);
FrayValue fray_string_copy(const char *s);
FrayValue fray_list(void);
FrayValue fray_tuple(size_t len);
FrayValue fray_set(void);
FrayValue fray_function(const char *name, void *code, int arity);

/* ── Calling a value (Phase 8) ──
 *
 * A function object's code pointer is always `void (*)(FrayValue self)`,
 * whichever entry point invokes it: the thread trampoline, the coroutine
 * trampoline and a first-class call all agree on that shape. What differs is
 * how the arguments reach the callee and how the result comes back, so a call
 * in progress parks both in thread-local slots — the same arrangement the
 * coroutine entry points use (fray_coro_argv / fray_coro_result_store).
 *
 * fray_call checks that the value is callable and that the argument count
 * matches the object's declared arity, throwing TypeError exactly as the
 * oracle does. It takes no ownership of fn or args; the result is the
 * caller's. Nested calls save and restore the slots, so a call in progress is
 * always the innermost one. */
FrayValue fray_call(FrayValue fn, FrayValue args);
FrayValue fray_call_args(void);                /* borrowed, during a call   */
void      fray_call_result_store(FrayValue v); /* hands the result back     */

/* ── Reference counting ──
 *
 * Strong counts are atomic. Internal (heap-internal) strong edges are not
 * counted; roots and out-of-heap references are. fray_retain/fray_release
 * are the *external* API used by generated code and embedders.
 */

void fray_retain(FrayValue v);
void fray_release(FrayValue v);
FrayValue fray_retained(FrayValue v);

/* Fast paths used by the specialized codegen. */
static inline void fray_retain_fast(FrayValue v) {
    if (v) atomic_fetch_add_explicit(&v->refcount, 1, memory_order_relaxed);
}
static inline void fray_release_fast(FrayValue v) {
    if (v && atomic_fetch_sub_explicit(&v->refcount, 1, memory_order_acq_rel) == 1)
        fray_release(v);
}
static inline int64_t fray_refcount(FrayValue v) {
    return v ? atomic_load_explicit(&v->refcount, memory_order_relaxed) : 0;
}

/* Root registration: keep a value alive across a potential collection while
 * it is only reachable from a local variable. */
void fray_root_push(FrayValue v);
void fray_root_pop(void);

/* Weak references: id-based registry. A weak handle stays valid after the
 * target dies — lock simply returns NULL. Handles must be dropped with
 * fray_weak_del or they linger (harmless, but leak registry slots). */
int64_t   fray_weak_new(FrayValue v);  /* returns handle id                 */
FrayValue fray_weak_lock(int64_t id);  /* retained value, or NULL if dead   */
void      fray_weak_del(int64_t id);   /* drop the handle                   */

/* ── Cycle collector ──
 *
 * Trial-deletion collector over GC-heap objects only. Young objects are
 * collected every FRAY_GC_YOUNG_INTERVAL allocations; old objects every
 * FRAY_GC_MAJOR_INTERVAL allocations. An object with a zero strong count
 * is freed immediately by fray_release — the collector only ever sees
 * objects trapped in cycles.
 *
 * Phase 6: each thread has its own GC space (lists, counters, buffers);
 * collections are stop-the-world over all registered threads.
 */
#define FRAY_GC_YOUNG_INTERVAL 1024
#define FRAY_GC_MAJOR_INTERVAL 1000000

void        fray_gc_collect(bool major);
void        fray_gc_notify_alloc(FrayValue v); /* called by constructors  */
const char *fray_gc_stats(void);               /* static counters string  */
size_t      fray_gc_heap_size(void);           /* tracked object count    */
size_t      fray_gc_pending_count(void);       /* deferred container frees */

/* Safepoints: poll points where a thread may pause for stop-the-world GC.
 * Allocation and decrement-notify route through these automatically. */
void fray_gc_safepoint(void);

/* ── Threads (Phase 6) ──
 *
 * spawn takes a first-class function object; the runtime runs it on a new
 * C thread with an empty local environment and joins nothing. join blocks
 * until the spawned thread finishes and reclaims its resources. All
 * coordination beyond that is up to the program (atomics, shared lists). */
int64_t fray_thread_spawn(FrayValue fn);       /* fn is a TAG_FUNCTION object  */
void    fray_thread_join(int64_t id);
void    fray_thread_join_all(void);            /* join every outstanding spawn */
int     fray_thread_count(void);               /* live spawned threads          */

/* ── Atomics (Phase 6) ──
 *
 * A boxed object (int/float/bool today) whose payload updates are made
 * atomic by a dedicated object lock. get/set/add are race-free across
 * threads; the object itself is managed by the normal GC. */
FrayValue fray_atomic_new(FrayValue initial);
FrayValue fray_atomic_get(FrayValue box);
void      fray_atomic_set(FrayValue box, FrayValue v);
FrayValue fray_atomic_add(FrayValue box, FrayValue delta); /* returns new value */

/* ── Coroutines & async I/O (Phase 7) ──
 *
 * Stackful model: the thread that runs async code converts to a fiber and
 * executes an event loop; each `async fn` call runs on its own fiber that
 * multiplexes onto that loop (M:N). `await` suspends the coroutine fiber
 * and resumes the loop; I/O completion or timer expiry re-queues it. Cheap
 * stacks: fiber stacks are virtual, allocated once per coroutine.
 *
 * Interop rule with Phase 6 threads: blocking on a channel or timer inside
 * a coroutine suspends only that fiber; inside a plain (spawn) thread it
 * blocks the whole OS thread. Channels are the bridge between the two.
 *
 * All APIs below are safe to call from any thread: each event loop serves
 * the coroutines created on its own thread. */

/* Start a coroutine running now (used by the async-call lowering): the
 * body runs on a new fiber scheduled on the caller's event loop, and a
 * TAG_COROUTINE handle comes back immediately. */
FrayValue fray_coro_start(FrayValue fn);

/* Start a coroutine with an argument list. The arguments travel on the
 * coroutine (not on the fn object), so each async call gets its own copy;
 * `argv` is a list, or NULL for none. Retains both `fn` and `argv` — the
 * caller keeps its own references. */
FrayValue fray_coro_start_argv(FrayValue fn, FrayValue argv);

/* The running coroutine's argument list, for the compiled async trampoline
 * to hand to its body. Borrowed: the coroutine owns it, so the caller must
 * not release it. Returns none when the coroutine was started without
 * arguments. */
FrayValue fray_coro_argv(void);

/* Await a coroutine: suspends the calling fiber until the target finishes
 * and returns its result value. Await from a non-coroutine context falls
 * back to blocking the calling thread. */
FrayValue fray_coro_await(FrayValue handle);

/* Store an async body's return value (called from the compiled async
 * trampoline just before it returns). Takes ownership. */
void     fray_coro_result_store(FrayValue v);

/* Yield: reschedule the current coroutine without suspending on an event. */
void     fray_coro_yield(void);

/* Sleep: suspend the calling coroutine for ms milliseconds (fiber stack
 * when called from a coroutine; OS sleep from a plain thread). */
void     fray_sleep(double ms);

/* Spawn-and-forget: run an async function on the caller's loop without
 * keeping a handle. */
void     fray_coro_spawn(FrayValue fn);

/* Run the calling thread's event loop until every coroutine it owns has
 * finished (used at program/module level after spawning coroutines). */
void     fray_coro_run_until_complete(void);

int      fray_coro_count(void);   /* live coroutines across all loops    */

/* ── Parking a coroutine on a blocking I/O worker (coroutine.c + io.c) ──
 *
 * io.c hands a coroutine to a worker thread, parks the fiber, and the worker
 * posts the completion back. The loop only ever stores and returns the opaque
 * job pointer, and a worker never allocates a FrayValue — the coroutine turns
 * the result into one on its own GC space after it resumes. */
void    *fray_coro_current_token(void);   /* running Coro, or NULL       */
void     fray_coro_hold(void *token);     /* ref for the flight          */
void     fray_coro_release_token(void *token);  /* unref, loop thread only */
void     fray_coro_park(void *token);
void     fray_coro_post_io(void *token, void *job);
void    *fray_coro_take_io(void *token);  /* NULL if resumed otherwise  */

/* Adopt a malloc'd buffer as a string instead of copying it; the caller
 * must not free it afterwards. Passing NULL is exactly fray_string. */
FrayValue fray_string_take(char *owned, size_t len);

/* Read an open stream whole into a fresh buffer sized from the file when
 * the OS can tell us (builtins.c). The caller frees the buffer and closes
 * the stream; the function returns only on success, exiting on OOM. */
char    *fray_slurp_stream(FILE *f, size_t *out_len);

/* ── Non-blocking file & socket I/O (io.c) ──
 *
 * A regular file cannot be watched by epoll/kqueue (epoll_ctl returns EPERM
 * for anything without poll semantics), so file operations are offloaded to a
 * worker pool just like socket operations. Every entry point returns
 * immediately when called from a coroutine — the fiber parks and the loop
 * resumes it when the worker posts — and runs the syscall inline from a plain
 * thread, so the same call works at module level. Sockets and connections are
 * plain integers (file descriptors) in both engines. */
FrayValue fray_io_read_file(FrayValue path);
FrayValue fray_io_write_file(FrayValue path, FrayValue text);
FrayValue fray_io_read_fd(FrayValue fd, FrayValue count);
FrayValue fray_io_write_fd(FrayValue fd, FrayValue text);
FrayValue fray_io_accept(FrayValue server_fd);
FrayValue fray_io_connect(FrayValue host, FrayValue port);
FrayValue fray_io_listen(FrayValue port);
void      fray_io_close_fd(FrayValue fd);
/* The port a listener bound — `tcpListen(0)` takes an ephemeral one. */
FrayValue fray_io_port(FrayValue fd);

/* Boxed shims, the convention every other builtin follows. */
FrayValue fray_io_read_file_boxed(FrayValue path);
FrayValue fray_io_write_file_boxed(FrayValue path, FrayValue text);
FrayValue fray_io_read_fd_boxed(FrayValue fd, FrayValue count);
FrayValue fray_io_write_fd_boxed(FrayValue fd, FrayValue text);
FrayValue fray_io_accept_boxed(FrayValue server_fd);
FrayValue fray_io_connect_boxed(FrayValue host, FrayValue port);
FrayValue fray_io_listen_boxed(FrayValue port);
FrayValue fray_io_close_fd_boxed(FrayValue fd);
FrayValue fray_io_port_boxed(FrayValue fd);

/* Channels: rendezvous buffers connecting coroutines and threads. send
 * blocks (suspends a fiber) while the buffer is full; recv while empty. */
FrayValue fray_channel_new(FrayValue capacity);  /* int arg or none */
FrayValue fray_channel_send(FrayValue chan, FrayValue v);   /* returns none  */
FrayValue fray_channel_recv(FrayValue chan);  /* returns value or none when closed+drained */
void      fray_channel_close(FrayValue chan);
FrayValue fray_channel_close_boxed(FrayValue chan);  /* boxed convention */
FrayValue fray_sleep_boxed(FrayValue ms);            /* boxed convention */
FrayValue fray_coro_yield_boxed(void);
FrayValue fray_coro_count_boxed(void);
FrayValue fray_coro_run_until_complete_shim(void);

/* Object constructors for Phase 7 payloads (objects.c + coroutine.c). */
FrayValue fray_coro_object(void *state);       /* TAG_COROUTINE wrapper     */
FrayValue fray_channel_object(void *state);    /* TAG_CHANNEL wrapper       */
void fray_coro_state_release(void *state);     /* handle finalizer hook     */
void fray_channel_state_release(void *state);  /* channel finalizer hook    */

/* ── Type checks ── */

static inline FrayTag fray_tag(FrayValue v) { return v ? v->tag : TAG_NONE; }
static inline bool fray_is_none(FrayValue v) { return v == NULL || v->tag == TAG_NONE; }
static inline bool fray_is_int(FrayValue v) { return v && v->tag == TAG_INT; }
static inline bool fray_is_float(FrayValue v) { return v && v->tag == TAG_FLOAT; }
static inline bool fray_is_string(FrayValue v) { return v && v->tag == TAG_STRING; }
static inline bool fray_is_list(FrayValue v) { return v && v->tag == TAG_LIST; }
static inline bool fray_is_tuple(FrayValue v) { return v && v->tag == TAG_TUPLE; }
static inline bool fray_is_set(FrayValue v) { return v && v->tag == TAG_SET; }
static inline bool fray_is_coroutine(FrayValue v) { return v && v->tag == TAG_COROUTINE; }
static inline bool fray_is_channel(FrayValue v) { return v && v->tag == TAG_CHANNEL; }
static inline bool fray_is_struct(FrayValue v) { return v && v->tag == TAG_STRUCT; }
static inline bool fray_is_map(FrayValue v) { return v && v->tag == TAG_MAP; }

/* ── Arithmetic ── */

FrayValue fray_add(FrayValue a, FrayValue b);
FrayValue fray_sub(FrayValue a, FrayValue b);
FrayValue fray_mul(FrayValue a, FrayValue b);
FrayValue fray_div(FrayValue a, FrayValue b);
FrayValue fray_floordiv(FrayValue a, FrayValue b);
FrayValue fray_mod(FrayValue a, FrayValue b);
FrayValue fray_pow(FrayValue a, FrayValue b);
FrayValue fray_neg(FrayValue a);

/* Fast paths — assume both operands are already type-checked.
 * fray_add_fast: string+string concat, else numeric (i64 path, fp promote). */
FrayValue fray_add_fast(FrayValue a, FrayValue b);
FrayValue fray_sum_fast(FrayValue v); /* v is a list of ints */

/* ── Comparison ── */

FrayValue fray_eq(FrayValue a, FrayValue b);
FrayValue fray_neq(FrayValue a, FrayValue b);
FrayValue fray_lt(FrayValue a, FrayValue b);
FrayValue fray_gt(FrayValue a, FrayValue b);
FrayValue fray_lte(FrayValue a, FrayValue b);
FrayValue fray_gte(FrayValue a, FrayValue b);

/* ── Boolean ── */

FrayValue fray_and(FrayValue a, FrayValue b);
FrayValue fray_or(FrayValue a, FrayValue b);
FrayValue fray_xor(FrayValue a, FrayValue b);
FrayValue fray_xnor(FrayValue a, FrayValue b);
FrayValue fray_not(FrayValue a);
bool fray_is_truthy(FrayValue v);

/* ── Built-in functions ── */

void fray_print(FrayValue v);
void fray_print_sep(FrayValue v);
void fray_print_end(void);
FrayValue fray_input(void);
FrayValue fray_input_str(const char *prompt);
FrayValue fray_input_int(void);
FrayValue fray_input_float(void);
FrayValue fray_input_int_str(const char *prompt);
FrayValue fray_input_float_str(const char *prompt);

/* ── Program arguments / files ──
 *
 * `progName` and `programArgs` give a compiled program its command line; the
 * generated `main` calls fray_init_args once at entry with the C argc/argv
 * (bootstrap/codegen.py and compiler/codegen.fray both do this), and these
 * accessors hand out retained aliases. Calling them without fray_init_args
 * (the JIT path) yields an empty argument list, not a crash. The parameter
 * list builtin is `programArgs`, not `args`: `args` is a common local name
 * (the self-hosted parser's own parse_postfix uses it), and with a builtin
 * of that name every block-level `args = ...` rebinds the global builtin
 * instead of creating a local — the oracle's assignment semantics walk up
 * the scope chain — which corrupted the compiler's own AST when it parsed
 * nested calls.
 *
 * exit(code) terminates the process; C's exit() flushes stdio, so buffered
 * print output is not lost. */

void fray_init_args(int argc, char **argv);
FrayValue fray_prog_name(void);
FrayValue fray_argv(void);
FrayValue fray_file_read(FrayValue path);
FrayValue fray_file_exists(FrayValue path);
void fray_file_write(FrayValue path, FrayValue text);
FrayValue fray_list_dir(FrayValue path);
FrayValue fray_is_dir(FrayValue path);
void fray_exit_val(FrayValue code);
FrayValue fray_len(FrayValue v);
/* Length without the box. Generated code re-checks a for-loop's bound on
 * every iteration, so the boxed builtin would allocate once per element. */
int64_t   fray_len_raw(FrayValue v);
FrayValue fray_contains(FrayValue haystack, FrayValue needle);
FrayValue fray_min(FrayValue a, FrayValue b);
FrayValue fray_max(FrayValue a, FrayValue b);
FrayValue fray_sum(FrayValue v);
FrayValue fray_abs(FrayValue v);
FrayValue fray_sqrt(FrayValue v);
FrayValue fray_isqrt(FrayValue v);
FrayValue fray_round_val(FrayValue v);
FrayValue fray_int_val(FrayValue v);
FrayValue fray_float_val(FrayValue v);
FrayValue fray_str_val(FrayValue v);
FrayValue fray_range3(FrayValue start, FrayValue stop, FrayValue step);
FrayValue fray_range(FrayValue v);

/* ── List operations ── */

void fray_list_append(FrayValue list, FrayValue elem);
/* `.append` / `.depend` dispatch on the container's tag at run time: the
 * reference documents both on lists and on sets, and the emitters cannot know
 * which one a variable holds. */
void fray_append(FrayValue container, FrayValue elem);
FrayValue fray_depend(FrayValue container);
FrayValue fray_list_depend(FrayValue list);
FrayValue fray_list_index(FrayValue list, int64_t idx);
void fray_list_setindex(FrayValue list, int64_t idx, FrayValue val);

/* ── Tuple operations ── */

FrayValue fray_tuple_index(FrayValue tuple, int64_t idx);
void fray_tuple_setindex(FrayValue tuple, int64_t idx, FrayValue val);

/* ── Set operations ── */

void fray_set_append(FrayValue set, FrayValue elem);
FrayValue fray_set_depend(FrayValue set);

/* ── String operations ── */

FrayValue fray_string_concat(FrayValue a, FrayValue b);
FrayValue fray_string_repeat(FrayValue s, int64_t n);
FrayValue fray_string_index(FrayValue s, int64_t idx);
FrayValue fray_ord(FrayValue c);
FrayValue fray_chr(FrayValue n);
FrayValue fray_slice(FrayValue seq, FrayValue start, FrayValue stop, FrayValue step);
FrayValue fray_string_upper(FrayValue s);
FrayValue fray_string_lower(FrayValue s);
FrayValue fray_string_trim(FrayValue s);
FrayValue fray_string_find(FrayValue s, FrayValue sub);
FrayValue fray_string_replace(FrayValue s, FrayValue old, FrayValue new_);
FrayValue fray_string_split(FrayValue s, FrayValue delim);
FrayValue fray_string_startswith(FrayValue s, FrayValue prefix);
FrayValue fray_string_endswith(FrayValue s, FrayValue suffix);

/* ── Struct operations (Phase 8) ──
 *
 * Structs are named-field mutable record types. The type descriptor holds
 * the field names; instances hold a contiguous array of FrayValue slots. */
FrayValue fray_struct_new(const char *name, size_t nfields, const char **field_names);
FrayValue fray_struct_new_boxed(FrayValue name_val, FrayValue fields_list);
FrayValue fray_struct_field_get(FrayValue obj, const char *field);
void      fray_struct_field_set(FrayValue obj, const char *field, FrayValue val);
FrayValue fray_struct_field_get_boxed(FrayValue obj, FrayValue field_name);
void      fray_struct_field_set_boxed(FrayValue obj, FrayValue field_name, FrayValue val);
void      fray_struct_state_release(void *state);

/* ── Map operations (Phase 8) ──
 *
 * Insertion-ordered hash maps with arbitrary keys (int, float, string, bool, tuple). */
FrayValue fray_map_new(void);
FrayValue fray_map_new_boxed(FrayValue cap_val);
FrayValue fray_map_get(FrayValue map, FrayValue key);
void      fray_map_set(FrayValue map, FrayValue key, FrayValue val);
FrayValue fray_map_has(FrayValue map, FrayValue key);
FrayValue fray_map_del(FrayValue map, FrayValue key);
FrayValue fray_map_len(FrayValue map);
FrayValue fray_map_keys(FrayValue map);
void      fray_map_state_release(void *state);

/* ── Option / Result ── */
FrayValue fray_some(FrayValue val);
FrayValue fray_ok(FrayValue val);
FrayValue fray_err(FrayValue err);
FrayValue fray_unwrap(FrayValue v);
FrayValue fray_is_some(FrayValue v);
FrayValue fray_is_none_val(FrayValue v);
FrayValue fray_is_ok(FrayValue v);
FrayValue fray_is_err(FrayValue v);
void      fray_option_state_release(void *state);
void      fray_result_state_release(void *state);

/* ── Type conversion helpers ── */

int64_t fray_as_int(FrayValue v);
double fray_as_float(FrayValue v);
const char *fray_as_string(FrayValue v);

/* ── Statistics ── */

FrayValue fray_mean(FrayValue v);
FrayValue fray_med(FrayValue v);
FrayValue fray_mid(FrayValue v);
FrayValue fray_mode(FrayValue v);

/* ── Try/except support ──
 *
 * Generated code brackets each try with begin/end. The pending exception is
 * deliberately left set across fray_try_end: a clause that matches marks it
 * handled with fray_exc_clear, and one that does not leaves it pending so the
 * enclosing try can see it, or fray_exc_rethrow reports it when there is no
 * enclosing try. */
void fray_try_begin(void);
void fray_try_end(void);
void fray_exc_clear(void);   /* the pending exception is handled */
void fray_exc_rethrow(void); /* ...or hand it on now the try has left */

/* ── Utilities ── */

void fray_print_repr(FrayValue v);
size_t fray_print_string(FrayValue v);
void   fray_format_double(double f, char *out, size_t cap); /* shortest round-trip */

/* ── Concurrency: thin striped object locks (Phase 6) ──
 * Every structural mutation of a possibly-shared container routes through
 * these. Non-recursive: never nest two fray_obj_lock calls. */
#include <pthread.h>
int  fray_obj_lock(FrayValue v);
void fray_obj_unlock(FrayValue v);
void fray_obj_locks_acquire_all(void);  /* collector use only */
void fray_obj_locks_release_all(void);

bool fray_is_bool_val(FrayValue v);   /* objects.c: inline helper below  */

/* Internal: the world lock serializes thread registration, GC initiation
 * and the pending-free queue. Defined in threads.c. */
void fray_world_lock(void);
void fray_world_unlock(void);

/* ── Internal: shared across runtime translation units ── */

/* Live fray threads a program may have at once. The GC space table is sized
 * from this (one space per live thread), so it must not be the tighter limit:
 * a space that cannot be registered is unreachable, which is a leak. */
#define FRAY_MAX_THREADS 256

/* Per-thread GC space struct — defined in cycles.c, used by threads.c. */
typedef struct ScratchEntry ScratchEntry;
typedef struct GcSpace GcSpace;
GcSpace *fray_gc_space_new(void);
void     fray_gc_space_retire(GcSpace *space);   /* release lists, park slot */
/* Registers a freshly allocated space and returns the space actually
 * registered, which is a parked one when a reuse is available. */
GcSpace *fray_gc_space_slot_grab(GcSpace *fresh);
void     fray_gc_active_threads_add(int delta);   /* spawn-side accounting   */
GcSpace *fray_gc_current_space(void);
void     fray_gc_current_space_set(GcSpace *space);
void     tls_space_bind(GcSpace *space);  /* bind a pre-created space to TLS */
FrayValue *edges_of_pub(FrayObj *o, size_t *n);  /* container edge array      */
void      fray_free_object(FrayValue v);        /* objects.c */
void      fray_gc_release_tracked(FrayValue v); /* cycles.c  */
FrayValue *fray_root_stack(size_t *out_len);    /* objects.c */
/* Release this thread's runtime scratch (leaf pool, root stack, exception
 * state). fray_thread_spawn's trampoline calls it automatically, and the
 * process-exit handler covers main. A thread the application creates itself
 * — a raw pthread — must call it before returning: the runtime has no way to
 * hook such a thread's exit, and anything it pooled would leak. */
void      fray_thread_locals_free(void);        /* objects.c: thread-exit */
void      fray_weak_clear_for(FrayValue v);     /* objects.c */
void      fray_gc_decrement_notify(FrayValue v);/* cycles.c  */
void      fray_gc_maybe_collect(void);          /* cycles.c  */
void      fray_throw(int exc_type, const char *msg); /* objects.c */
void      fray_fprint_repr(FILE *out, FrayValue v);  /* printing.c */
char     *fray_repr_alloc(FrayValue v, size_t *out_len); /* printing.c */

/* Thread identity + registry (threads.c, consumed by objects.c/cycles.c) */
extern _Thread_local int fray_tls_thread_id;   /* 0 = main, -1 = detached */

/* Type descriptors (defined in objects.c; atomics.c retags payloads). */
extern const FrayTypeInfo fray_type_none;
extern const FrayTypeInfo fray_type_int;
extern const FrayTypeInfo fray_type_float;
extern const FrayTypeInfo fray_type_bool;
extern const FrayTypeInfo fray_type_string;
extern const FrayTypeInfo fray_type_list;
extern const FrayTypeInfo fray_type_tuple;
extern const FrayTypeInfo fray_type_set;
extern const FrayTypeInfo fray_type_function;
extern const FrayTypeInfo fray_type_coroutine;
extern const FrayTypeInfo fray_type_channel;
extern const FrayTypeInfo fray_type_struct;
extern const FrayTypeInfo fray_type_map;
extern const FrayTypeInfo fray_type_option;
extern const FrayTypeInfo fray_type_result;
int      fray_threads_registered(void);
int      fray_gc_space_count(void);   /* slots ever created            */
void     fray_threads_foreach_space(void (*cb)(void *space, void *ctx), void *ctx);
void    *fray_thread_gc_space(int thread_id);  /* NULL if unregistered      */
void     fray_threads_pending_push(FrayValue v);   /* deferred container free */
bool     fray_gc_pending_take(FrayValue v);        /* off the queue; true if it was on it */
void     fray_gc_free_condemned(FrayValue v);      /* payload teardown; elements already dropped */

/* (GcSpace declarations live above with the other internals.) */

#ifdef __cplusplus
}
#endif

#endif /* FRAY_RUNTIME_H */
