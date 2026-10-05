/**
 * fray runtime — coroutines & async I/O (Phase 7).
 *
 * Model (documented in runtime.h):
 *  - Stackful coroutines on platform fibers. The thread that runs async
 *    code converts itself to a fiber and executes an event loop; each
 *    async call gets its own fiber multiplexed onto that loop (M:N).
 *  - `await` = suspend my fiber, resume the loop. Completion of the
 *    awaited event (timer, channel, another coroutine) re-queues me.
 *  - Per-thread loops: no cross-thread stealing; the hot path is
 *    lock-free. Foreign threads may push onto a loop's ready queue under
 *    the loop's lock (channel wakeups from Phase 6 threads).
 *  - Refcounting makes suspended stacks GC-safe for free: locals hold
 *    strong refs, so a suspended coroutine's data is a plain root chain.
 *    No stack scanning anywhere.
 *
 * Coro lifetime: self-refcounted. The scheduler holds one reference until
 * the final switch; each TAG_COROUTINE handle holds one. The last unref
 * frees the fiber and state.
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <time.h>
#include <ucontext.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <unistd.h>
#endif

#include <pthread.h>

/* ── Small platform shims ── */

static void sleep_ms(double ms) {
    if (ms <= 0.0) return;
#ifdef _WIN32
    Sleep((DWORD)ms);
#else
    struct timespec ts;
    ts.tv_sec = (time_t)(ms / 1000.0);
    ts.tv_nsec = (long)((ms / 1000.0 - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
#endif
}

/* ── Fiber primitives ── */

typedef struct Fiber Fiber;

#define FRAY_STACK_SIZE (256 * 1024)

/* Result of the most recently finished async body on this thread. The
 * compiled async thunk stores its return value here before returning
 * (the thunk itself returns void); coro_trampoline moves it into the
 * Coro. Fibers of one loop never run concurrently, but a thread may run
 * several loops' fibers sequentially — TLS is the correct granularity. */
static _Thread_local FrayValue tls_async_result = NULL;

#ifdef _WIN32

static _Thread_local bool tls_thread_is_fiber = false;

static void fiber_main_init(void) {
    if (!tls_thread_is_fiber) {
        ConvertThreadToFiberEx(NULL, FIBER_FLAG_FLOAT_SWITCH);
        tls_thread_is_fiber = true;
    }
}

static Fiber *fiber_current(void) {
    return (Fiber *)GetCurrentFiber();
}

static Fiber *fiber_new(void (*entry)(void *), void *arg) {
    return (Fiber *)CreateFiberEx(FRAY_STACK_SIZE, FRAY_STACK_SIZE,
                                  FIBER_FLAG_FLOAT_SWITCH,
                                  (LPFIBER_START_ROUTINE)entry, arg);
}

static void fiber_switch_to(Fiber *to) {
    SwitchToFiber(to);
}

static void fiber_destroy(Fiber *f) {
    DeleteFiber(f);
}

#else

typedef struct Fiber {
    ucontext_t ctx;
    void     (*entry)(void *);
    void      *arg;
    char      *stack;
    bool       finished;
} Fiber;

static _Thread_local Fiber *tls_current_fiber = NULL;

static void fiber_main_init(void) {
    if (!tls_current_fiber) {
        /* The running thread's state becomes the "main" fiber. */
        tls_current_fiber = calloc(1, sizeof(Fiber));
    }
}

static void fiber_glue(void);

static Fiber *fiber_new(void (*entry)(void *), void *arg) {
    Fiber *f = calloc(1, sizeof(Fiber));
    f->stack = malloc(FRAY_STACK_SIZE);
    f->entry = entry;
    f->arg = arg;
    getcontext(&f->ctx);
    f->ctx.uc_stack.ss_sp = f->stack;
    f->ctx.uc_stack.ss_size = FRAY_STACK_SIZE;
    f->ctx.uc_link = NULL;
    makecontext(&f->ctx, fiber_glue, 0);
    return f;
}

static void fiber_glue(void) {
    Fiber *self = tls_current_fiber;
    self->entry(self->arg);
    self->finished = true;
    /* The loop always switches us off before treating us as done. */
}

static Fiber *fiber_current(void) { return tls_current_fiber; }

static void fiber_switch_to(Fiber *to) {
    Fiber *from = tls_current_fiber;
    tls_current_fiber = to;
    swapcontext(&from->ctx, &to->ctx);
}

static void fiber_destroy(Fiber *f) {
    free(f->stack);
    free(f);
}

#endif

/* ── Coroutine state ── */

typedef enum {
    CORO_READY,     /* in the ready queue                                    */
    CORO_RUNNING,   /* on the CPU right now                                  */
    CORO_WAITING,   /* parked on a timer/channel/coroutine                   */
    CORO_DONE,      /* finished; result available                            */
} CoroPhase;

typedef struct Coro Coro;
typedef struct EventLoop EventLoop;

typedef struct Waiter {
    Coro          *coro;
    struct Waiter *next;
} Waiter;

typedef struct WaitList {
    Waiter *head;
    Waiter *tail;
} WaitList;

struct Coro {
    Fiber            *fiber;
    CoroPhase         phase;
    FrayValue         fn;          /* retained async function object        */
    FrayValue         argv;        /* retained argument list; NULL if none  */
    FrayValue         result;      /* owned; valid when DONE                */
    Coro             *awaited_by;  /* coroutine to wake on completion       */
    bool              joined;      /* someone already awaits                */
    FrayValue         handle;      /* TAG_COROUTINE object owning me        */
    struct EventLoop *loop;        /* owning loop                           */
    Coro             *qnext;       /* ready-queue link                      */
    void             *io_done;     /* completed I/O job; NULL when none     */
    _Atomic int       refs;
};

/* ── Event loop (one per thread that runs async code) ── */

typedef struct Timer {
    double        when_ms;
    Coro         *coro;
    struct Timer *next;          /* sorted min list                         */
} Timer;

struct EventLoop {
    pthread_mutex_t  lock;       /* guards ready queue + timers + wake      */
    pthread_cond_t   cv;         /* signalled when a foreign thread posts  */
    bool             wake;       /* a post arrived; set under `lock`       */
    Coro            *ready_head, *ready_tail;
    Timer           *timers;
    Coro            *current;    /* coroutine running now                   */
    Fiber           *loop_fiber; /* the loop thread's own fiber context     */
    bool             is_fiber;
    _Atomic size_t   live_count; /* coroutines not yet DONE                 */
    struct EventLoop *next;      /* registry link                           */
};

static pthread_mutex_t g_loops_lock = PTHREAD_MUTEX_INITIALIZER;
static EventLoop *g_loops = NULL;
static _Atomic int64_t g_coro_live = 0;

int fray_coro_count(void) {
    return (int)atomic_load(&g_coro_live);
}

static double now_ms(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq = {0};
    static double freq_d = 0.0;
    LARGE_INTEGER c;
    if (!freq.QuadPart) {
        QueryPerformanceFrequency(&freq);
        freq_d = (double)freq.QuadPart;
    }
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart * 1000.0 / freq_d;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
#endif
}

/* Ready queue + timer ops. The loop lock must be held for pushes that can
 * originate on foreign threads; the loop thread uses it too. */

static void loop_push_ready(EventLoop *loop, Coro *c) {
    pthread_mutex_lock(&loop->lock);
    c->qnext = NULL;
    if (loop->ready_tail) loop->ready_tail->qnext = c;
    else loop->ready_head = c;
    loop->ready_tail = c;
    /* A post from a foreign thread (a channel wakeup, an I/O completion)
     * must not wait for the loop's next timeout: flag it and cut the wait
     * short. This is what turns the old 1 ms poll into a real wakeup. */
    loop->wake = true;
    pthread_cond_signal(&loop->cv);
    /* The phase write belongs inside the lock, and that is the whole point of
     * this function: linking `c` into the queue is what makes the loop able to
     * run it, so the moment the lock is dropped the loop thread may pick `c`
     * up, run it to completion and drop the last reference to it (an I/O
     * completion releases the coroutine's hold as soon as it has the job),
     * which frees it. Storing the phase after the unlock therefore wrote into
     * freed memory -- valgrind caught it as an invalid 4-byte write at the
     * phase field, offset 8 of a 96-byte Coro, in about 8 runs in 10 of
     * benchmarks/io_socket_coro.fray. With the store under the lock this is
     * the last thing any thread does to `c`, and everything that can free it
     * waits on the same lock. */
    c->phase = CORO_READY;
    pthread_mutex_unlock(&loop->lock);
}

static bool loop_pop_ready(EventLoop *loop, Coro **out) {
    pthread_mutex_lock(&loop->lock);
    Coro *c = loop->ready_head;
    if (c) {
        loop->ready_head = c->qnext;
        if (!loop->ready_head) loop->ready_tail = NULL;
        c->qnext = NULL;
    }
    pthread_mutex_unlock(&loop->lock);
    if (!c) return false;
    *out = c;
    return true;
}

static void loop_arm_timer(EventLoop *loop, double delay_ms, Coro *c) {
    Timer *t = malloc(sizeof(Timer));
    t->when_ms = now_ms() + delay_ms;
    t->coro = c;
    pthread_mutex_lock(&loop->lock);
    if (!loop->timers || loop->timers->when_ms > t->when_ms) {
        t->next = loop->timers;
        loop->timers = t;
        pthread_mutex_unlock(&loop->lock);
        return;
    }
    Timer *cur = loop->timers;
    while (cur->next && cur->next->when_ms <= t->when_ms) cur = cur->next;
    t->next = cur->next;
    cur->next = t;
    pthread_mutex_unlock(&loop->lock);
}

static void loop_fire_expired_timers(EventLoop *loop) {
    double now = now_ms();
    pthread_mutex_lock(&loop->lock);
    while (loop->timers && loop->timers->when_ms <= now) {
        Timer *t = loop->timers;
        loop->timers = t->next;
        pthread_mutex_unlock(&loop->lock);
        if (t->coro->phase == CORO_WAITING) {
            loop_push_ready(loop, t->coro);
        }
        free(t);
        pthread_mutex_lock(&loop->lock);
        now = now_ms();
    }
    pthread_mutex_unlock(&loop->lock);
}

static double loop_next_timer_delay(EventLoop *loop) {
    pthread_mutex_lock(&loop->lock);
    if (!loop->timers) {
        pthread_mutex_unlock(&loop->lock);
        return -1.0;
    }
    double d = loop->timers->when_ms - now_ms();
    pthread_mutex_unlock(&loop->lock);
    return d > 0.0 ? d : 0.0;
}

/* Block until the next timer, or until a foreign thread posts to this loop.
 * `wake` is set under `lock` by loop_push_ready, so a post that lands
 * between the ready-queue drain and this call is not missed: the flag is
 * already true and the wait is skipped. Returns as soon as work is posted,
 * which is the whole point — a 1 ms poll would cap the round trip of every
 * I/O completion at a millisecond. `delay_ms` < 0 means "no timer armed",
 * where we keep a short ceiling so a late post from another thread that
 * somehow missed the flag still cannot wedge the loop. */
static void loop_wait(EventLoop *loop, double delay_ms) {
    if (delay_ms < 0.0) delay_ms = 1.0;
    struct timeval tv;
    gettimeofday(&tv, NULL);
    long extra = (long)(delay_ms * 1000.0) * 1000L;
    struct timespec ts;
    ts.tv_sec  = (time_t)tv.tv_sec + extra / 1000000000L;
    ts.tv_nsec = extra % 1000000000L;
    pthread_mutex_lock(&loop->lock);
    if (!loop->wake) pthread_cond_timedwait(&loop->cv, &loop->lock, &ts);
    loop->wake = false;
    pthread_mutex_unlock(&loop->lock);
}

/* ── Coro refcounting ── */

static void coro_ref(Coro *c) {
    atomic_fetch_add(&c->refs, 1);
}

static void coro_unref(Coro *c) {
    if (atomic_fetch_sub(&c->refs, 1) == 1) {
        if (c->fiber) {
            fiber_destroy(c->fiber);
            c->fiber = NULL;
        }
        if (c->result) fray_release(c->result);
        if (c->argv) fray_release(c->argv);
        if (c->fn) fray_release(c->fn);
        /* c->handle is a BORROWED back-pointer, not an owned reference:
         * the handle object is owned by whoever received it from
         * fray_coro_start, and its finalizer (fray_coro_state_release)
         * drops the handle's coro_ref. Releasing it here too would be a
         * double free whenever the handle dies before the scheduler's
         * final unref. */
        free(c);
    }
}

/* ── Coroutine trampoline ── */

static void coro_trampoline(void *argp) {
    Coro *self = (Coro *)argp;

    /* Runs on the loop thread's GC space (fibers of a loop never execute
     * concurrently), so allocation is single-threaded per loop. */
    void (*fn_thunk)(FrayValue) = (void (*)(FrayValue))self->fn->as.func.code;
    tls_async_result = NULL;
    fn_thunk(self->fn);
    FrayValue result = tls_async_result
        ? tls_async_result : fray_none();
    tls_async_result = NULL;

    self->phase = CORO_DONE;
    self->result = result;                    /* ownership moves here      */
    atomic_fetch_sub(&g_coro_live, 1);
    atomic_fetch_sub(&self->loop->live_count, 1);

    /* Wake whoever awaits me. */
    if (self->awaited_by && self->awaited_by->phase == CORO_WAITING) {
        loop_push_ready(self->loop, self->awaited_by);
        self->awaited_by = NULL;
    }

    /* Hand control (and the scheduler's reference) back to the loop. */
    fiber_switch_to(self->loop->loop_fiber);
    /* NOTREACHED for Win32 fibers; POSIX glue marks finished. */
}

/* ── Creating coroutines ── */

static void loop_drain(EventLoop *loop);
static bool loop_drain_once(EventLoop *loop);

static Coro *coro_new(EventLoop *loop, FrayValue fn, FrayValue argv) {
    Coro *c = calloc(1, sizeof(Coro));
    atomic_init(&c->refs, 1);                 /* scheduler's reference     */
    c->fn = fn;                               /* ownership taken           */
    c->argv = argv;                           /* ownership taken; may be NULL */
    c->phase = CORO_READY;
    c->loop = loop;
    c->result = NULL;
    atomic_fetch_add(&g_coro_live, 1);
    atomic_fetch_add(&loop->live_count, 1);
    c->fiber = fiber_new(coro_trampoline, c);
    loop_push_ready(loop, c);
    return c;
}

/* ── The event loop ── */

/* Run one scheduler pass: fire expired timers, run every ready coroutine
 * once. Returns true if any coroutine ran. Never blocks. Safe to call
 * from a plain thread (not just the loop fiber): it converts the calling
 * thread to a fiber lazily so fiber_switch_to works from anywhere. */
static bool loop_drain_once(EventLoop *loop) {
    fiber_main_init();
    if (!loop->loop_fiber) {
        loop->loop_fiber = fiber_current();
        loop->is_fiber = true;
    }
    loop_fire_expired_timers(loop);

    bool ran = false;
    Coro *c;
    while (loop_pop_ready(loop, &c)) {
        if (c->phase == CORO_DONE) {
            /* Finished but not yet reaped (anonymous handle died). */
            continue;
        }
        loop->current = c;
        c->phase = CORO_RUNNING;
        fiber_switch_to(c->fiber);
        loop->current = NULL;
        ran = true;

        if (c->phase == CORO_DONE) {
            /* We just switched back off its fiber for the last time:
             * drop the scheduler's reference (frees fiber + state once
             * the handle — if any — is also gone). */
            coro_unref(c);
        }
    }
    return ran;
}

static void loop_drain(EventLoop *loop) {
    fiber_main_init();
    loop->loop_fiber = fiber_current();
    loop->is_fiber = true;

    while (atomic_load(&loop->live_count) > 0) {
        bool ran = loop_drain_once(loop);

        if (atomic_load(&loop->live_count) > 0 && !ran) {
            double delay = loop_next_timer_delay(loop);
            /* Sleep until the next timer, or until a cross-thread post
             * (channel wakeup, I/O completion) wakes us first. */
            loop_wait(loop, delay);
        }
    }
}

/* ── Thread-local loop ── */

static pthread_key_t g_loop_key;
static pthread_once_t g_loop_key_once = PTHREAD_ONCE_INIT;

static void make_loop_key(void) {
    pthread_key_create(&g_loop_key, NULL);
}

static EventLoop *thread_loop(void) {
    pthread_once(&g_loop_key_once, make_loop_key);
    EventLoop *loop = (EventLoop *)pthread_getspecific(g_loop_key);
    if (!loop) {
        loop = calloc(1, sizeof(EventLoop));
        pthread_mutex_init(&loop->lock, NULL);
        pthread_cond_init(&loop->cv, NULL);
        loop->wake = false;
        atomic_init(&loop->live_count, 0);
        pthread_setspecific(g_loop_key, loop);
        pthread_mutex_lock(&g_loops_lock);
        loop->next = g_loops;
        g_loops = loop;
        pthread_mutex_unlock(&g_loops_lock);
    }
    return loop;
}

/* ── Public API ── */

void fray_coro_run_until_complete(void) {
    EventLoop *loop = thread_loop();
    if (atomic_load(&loop->live_count) > 0) loop_drain(loop);
}

void fray_coro_yield(void) {
    EventLoop *loop = thread_loop();
    Coro *self = loop->current;
    if (!self) return;                        /* plain thread: no-op       */
    loop_push_ready(loop, self);
    fiber_switch_to(loop->loop_fiber);
}

void fray_sleep(double ms) {
    if (ms <= 0.0) return;
    EventLoop *loop = thread_loop();
    Coro *self = loop->current;
    if (self) {
        loop_arm_timer(loop, ms, self);
        self->phase = CORO_WAITING;
        fiber_switch_to(loop->loop_fiber);
    } else {
        sleep_ms(ms);
    }
}

FrayValue fray_coro_start(FrayValue fn) {
    if (!fn || fn->tag != TAG_FUNCTION) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: async call expects an async function");
        return fray_none();
    }
    EventLoop *loop = thread_loop();
    fray_retain(fn);
    Coro *c = coro_new(loop, fn, NULL);
    FrayValue handle = fray_coro_object(c);   /* refcount 1 (the handle)   */
    c->handle = handle;
    coro_ref(c);                              /* the handle's reference    */
    return handle;
}

/* Start a coroutine carrying an argument list. The arguments live on the
 * coroutine rather than the fn object, so two calls of the same async
 * function cannot see each other's arguments. Retains both arguments:
 * the caller keeps its own references. */
FrayValue fray_coro_start_argv(FrayValue fn, FrayValue argv) {
    if (!fn || fn->tag != TAG_FUNCTION) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: async call expects an async function");
        return fray_none();
    }
    EventLoop *loop = thread_loop();
    fray_retain(fn);
    if (argv) fray_retain(argv);              /* the coroutine owns its args */
    Coro *c = coro_new(loop, fn, argv);
    FrayValue handle = fray_coro_object(c);   /* refcount 1 (the handle)   */
    c->handle = handle;
    coro_ref(c);                              /* the handle's reference    */
    return handle;
}

/* The running coroutine's argument list, for the compiled async trampoline
 * to hand to its body. Borrowed: the coroutine owns it, so the caller must
 * not release it. Returns none when there are no arguments. */
FrayValue fray_coro_argv(void) {
    Coro *self = thread_loop()->current;
    if (!self || !self->argv) return fray_none();
    return self->argv;
}

void fray_coro_spawn(FrayValue fn) {
    if (!fn || fn->tag != TAG_FUNCTION) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: coroutine spawn expects a function");
        return;
    }
    EventLoop *loop = thread_loop();
    fray_retain(fn);
    Coro *c = coro_new(loop, fn, NULL);
    c->handle = NULL;                         /* anonymous                 */
    (void)c;
}

FrayValue fray_coro_await(FrayValue handle) {
    /* await None → None (matches the oracle: builtin calls like sleep()
     * return None, and `await sleep(N)` is a no-op on the result). */
    if (!handle || handle->tag == TAG_NONE) {
        return fray_none();
    }
    if (handle->tag != TAG_COROUTINE) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: await expects a coroutine");
        return fray_none();
    }
    Coro *target = (Coro *)handle->as.coro.state;
    EventLoop *loop = thread_loop();
    Coro *self = loop->current;

    if (target->phase == CORO_DONE) {
        return target->result ? fray_retained(target->result) : fray_none();
    }
    if (target->joined) {
        fray_throw(FRAY_EXC_TYPE, "ValueError: coroutine already awaited");
        return fray_none();
    }
    target->joined = true;

    if (!self || self->loop != target->loop) {
        /* Plain thread (e.g. module-level await): pump the target's loop
         * while waiting — otherwise nothing schedules the coroutine and
         * the poll never terminates. Cross-loop is the same shape. */
        while (target->phase != CORO_DONE) {
            loop_drain_once(target->loop);
            if (target->phase != CORO_DONE) sleep_ms(1.0);
        }
        return target->result ? fray_retained(target->result) : fray_none();
    }

    /* Same loop: suspend until the target completes. */
    target->awaited_by = self;
    self->phase = CORO_WAITING;
    fiber_switch_to(loop->loop_fiber);
    /* Resumed after coro_finish set result; the target Coro is kept
     * alive by our handle reference. */
    return target->result ? fray_retained(target->result) : fray_none();
}

/* ── Parking a coroutine on an I/O completion ──
 *
 * The loop treats a completion record as opaque: it stores the pointer,
 * makes the coroutine runnable again, and hands it back. Only io.c knows
 * what is inside. A worker thread never touches a FrayValue, so this hand-off
 * is the only thing that crosses the thread boundary.
 */

/* The running coroutine as an opaque token, or NULL on a plain thread (a
 * module-level `readFileAsync`, say) — io.c runs the job inline there. */
void *fray_coro_current_token(void) {
    EventLoop *loop = thread_loop();
    return (void *)loop->current;
}

/* Keep a parked Coro alive for as long as a job referencing it is in flight.
 * The matching release runs on the loop thread, so the final unref — and any
 * fray_release it performs — never happens on a worker thread whose GC space
 * is not this loop's. */
void fray_coro_hold(void *token) { coro_ref((Coro *)token); }
void fray_coro_release_token(void *token) { coro_unref((Coro *)token); }

/* Park the running coroutine. It resumes only via fray_coro_post_io. */
void fray_coro_park(void *token) {
    Coro *self = (Coro *)token;
    EventLoop *loop = self->loop;
    self->phase = CORO_WAITING;
    fiber_switch_to(loop->loop_fiber);
}

/* Called from a worker thread. Stores the completion and makes the coroutine
 * runnable again; loop_push_ready signals the loop's condvar, so this is the
 * wakeup the loop is blocked on. */
void fray_coro_post_io(void *token, void *job) {
    Coro *self = (Coro *)token;
    self->io_done = job;
    loop_push_ready(self->loop, self);
}

/* The completion this coroutine was parked on, or NULL if it was resumed for
 * some other reason. Ownership passes to the caller. */
void *fray_coro_take_io(void *token) {
    Coro *self = (Coro *)token;
    void *job = self->io_done;
    self->io_done = NULL;
    return job;
}

/* State release hooks for objects.c finalizers. */

/* ── Boxed shims (compiled-code calling convention) ──
 * The compiler emits calls through these wrappers so every builtin has
 * the same boxed signature. Each consumes its arguments' references. */

FrayValue fray_sleep_boxed(FrayValue ms) {
    double d = 0.0;
    if (fray_is_int(ms))  d = (double)ms->as.i;
    else if (fray_is_float(ms)) d = fray_as_float(ms);
    /* Don't release ms here — the codegen's generic BUILTIN_MAP handler
     * releases all arguments after the call via _release_all. */
    fray_sleep(d);
    return fray_none();
}

FrayValue fray_coro_yield_boxed(void) {
    fray_coro_yield();
    return fray_none();
}

FrayValue fray_coro_count_boxed(void) {
    return fray_int(fray_coro_count());
}

FrayValue fray_coro_run_until_complete_shim(void) {
    fray_coro_run_until_complete();
    return fray_none();
}

/* Boxed variant of fray_channel_close (boxed calling convention). */
FrayValue fray_channel_close_boxed(FrayValue chan) {
    fray_channel_close(chan);   /* consumes the argument's reference */
    return fray_none();
}

/* Store an async body's return value (called from the compiled async
 * trampoline just before it returns). Takes ownership. */
void fray_coro_result_store(FrayValue v) {
    if (tls_async_result) fray_release(tls_async_result);
    tls_async_result = v;
}

void fray_coro_state_release(void *state) {
    coro_unref((Coro *)state);
}

/* ── Channels ── */

typedef struct Channel {
    pthread_mutex_t lock;
    FrayValue      *buf;
    size_t          cap, head, tail, len;
    bool            closed;
    WaitList        send_waiters;
    WaitList        recv_waiters;
} Channel;

FrayValue fray_channel_new(FrayValue cap_val) {
    int64_t cap = 16;
    if (cap_val && cap_val->tag == TAG_INT) {
        cap = cap_val->as.i;
        if (cap < 0) cap = 0;
        if (cap > (int64_t)1 << 20) cap = 1 << 20;
    }
    Channel *ch = calloc(1, sizeof(Channel));
    pthread_mutex_init(&ch->lock, NULL);
    ch->cap = (size_t)cap;
    ch->buf = ch->cap ? malloc(sizeof(FrayValue) * ch->cap) : NULL;
    return fray_channel_object(ch);
}
FrayValue fray_channel_send(FrayValue chan, FrayValue v) {
    if (!chan || chan->tag != TAG_CHANNEL) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: send expects a channel");
        return fray_none();
    }
    Channel *ch = (Channel *)chan->as.chan.chan;
    EventLoop *loop = thread_loop();
    Coro *self = loop->current;

    pthread_mutex_lock(&ch->lock);
    while (!ch->closed && ch->len >= ch->cap) {
        if (self) {
            /* Suspend my fiber until a recv wakes me. */
            Waiter *w = malloc(sizeof(Waiter));
            w->coro = self;
            w->next = NULL;
            if (ch->send_waiters.tail) ch->send_waiters.tail->next = w;
            else ch->send_waiters.head = w;
            ch->send_waiters.tail = w;
            self->phase = CORO_WAITING;
            pthread_mutex_unlock(&ch->lock);
            fiber_switch_to(loop->loop_fiber);
            pthread_mutex_lock(&ch->lock);
        } else {
            pthread_mutex_unlock(&ch->lock);
            sleep_ms(1.0);                    /* plain thread: poll        */
            pthread_mutex_lock(&ch->lock);
        }
    }
    if (ch->closed) {
        pthread_mutex_unlock(&ch->lock);
        fray_throw(FRAY_EXC_TYPE, "ValueError: send on closed channel");
        return fray_none();
    }
    ch->buf[ch->tail] = fray_retained(v);
    ch->tail = (ch->tail + 1) % ch->cap;
    ch->len++;
    /* Wake one receiver. */
    if (ch->recv_waiters.head) {
        Waiter *w = ch->recv_waiters.head;
        ch->recv_waiters.head = w->next;
        if (!ch->recv_waiters.head) ch->recv_waiters.tail = NULL;
        loop_push_ready(w->coro->loop, w->coro);
        free(w);
    }
    pthread_mutex_unlock(&ch->lock);
    return fray_none();
}

FrayValue fray_channel_recv(FrayValue chan) {
    if (!chan || chan->tag != TAG_CHANNEL) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: recv expects a channel");
        return fray_none();
    }
    Channel *ch = (Channel *)chan->as.chan.chan;
    EventLoop *loop = thread_loop();
    Coro *self = loop->current;

    pthread_mutex_lock(&ch->lock);
    while (ch->len == 0 && !ch->closed) {
        if (self) {
            Waiter *w = malloc(sizeof(Waiter));
            w->coro = self;
            w->next = NULL;
            if (ch->recv_waiters.tail) ch->recv_waiters.tail->next = w;
            else ch->recv_waiters.head = w;
            ch->recv_waiters.tail = w;
            self->phase = CORO_WAITING;
            pthread_mutex_unlock(&ch->lock);
            fiber_switch_to(loop->loop_fiber);
            pthread_mutex_lock(&ch->lock);
        } else {
            pthread_mutex_unlock(&ch->lock);
            sleep_ms(1.0);
            pthread_mutex_lock(&ch->lock);
        }
    }
    if (ch->len == 0) {                       /* closed and drained        */
        pthread_mutex_unlock(&ch->lock);
        return fray_none();
    }
    FrayValue v = ch->buf[ch->head];
    ch->head = (ch->head + 1) % ch->cap;
    ch->len--;
    /* Wake one sender. */
    if (ch->send_waiters.head) {
        Waiter *w = ch->send_waiters.head;
        ch->send_waiters.head = w->next;
        if (!ch->send_waiters.head) ch->send_waiters.tail = NULL;
        loop_push_ready(w->coro->loop, w->coro);
        free(w);
    }
    pthread_mutex_unlock(&ch->lock);
    return v;                                 /* ownership to caller       */
}

void fray_channel_close(FrayValue chan) {
    if (!chan || chan->tag != TAG_CHANNEL) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: close expects a channel");
        return;
    }
    Channel *ch = (Channel *)chan->as.chan.chan;
    pthread_mutex_lock(&ch->lock);
    ch->closed = true;
    WaitList *lists[2] = { &ch->send_waiters, &ch->recv_waiters };
    for (int i = 0; i < 2; i++) {
        Waiter *w = lists[i]->head;
        while (w) {
            Waiter *n = w->next;
            loop_push_ready(w->coro->loop, w->coro);
            free(w);
            w = n;
        }
        lists[i]->head = lists[i]->tail = NULL;
    }
    pthread_mutex_unlock(&ch->lock);
}

void fray_channel_state_release(void *state) {
    Channel *ch = (Channel *)state;
    for (size_t i = 0; i < ch->len; i++) {
        size_t idx = (ch->head + i) % ch->cap;
        fray_release(ch->buf[idx]);
    }
    free(ch->buf);
    pthread_mutex_destroy(&ch->lock);
    free(ch);
}

/* ── Async file I/O ──
 * Canonical whole-file read/write lives in builtins.c (fray_file_read /
 * fray_file_write / fray_file_exists); they run inline on the fiber — a
 * fiber blocks only its own coroutine relative to the loop scheduling of
 * others, and the semantic surface is the same. Overlapped I/O is the
 * follow-up; wall-clock overlap for big files lands with it. */
