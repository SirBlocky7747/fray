/**
 * fray runtime — object model v2 (Phase 5).
 *
 * Split out of the old single-file runtime: object allocation, atomic
 * reference counting, weak references, and the type descriptors.
 *
 * Memory-management invariants (see also cycles.c):
 *  - `refcount` counts EVERY strong reference: the machine stack, globals,
 *    embedders, and each container -> element edge. Storing an element in a
 *    container retains it and the container's teardown releases it, so zero
 *    means "nothing can reach this object".
 *  - A heap object whose strong count drops to zero is freed immediately,
 *    together with the heap-internal subgraph only it references.
 *  - A cycle keeps its members' counts positive after the last external
 *    reference is gone. The collector finds those by trial deletion over the
 *    candidate roots each space accumulated (cycles.c); it must never free
 *    anything reachable from a live one.
 *  - Objects that are members of a tracked heap object are allocated in the
 *    GC heap too, so the collector can always trace complete subgraphs.
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Phase 6: thread identity. 0 = main thread, >0 = registered fray thread,
 * -1 = detached/unknown. Defined here; declared in runtime.h. */
_Thread_local int fray_tls_thread_id = 0;

/* ── Exception state ──
 * Phase 6: per-thread. Main thread keeps the static ABI symbols; every
 * thread (main included, lazily) gets TLS storage on first use. */
static _Thread_local jmp_buf *tls_jmp = NULL;
static _Thread_local int tls_in_try = 0;
static _Thread_local int tls_exc_type = 0;
static _Thread_local bool tls_exc_ready = false;
/* The pending exception's message, owned: a re-raise after a finally needs it
 * and the thrower may be long gone. Replaced by the next throw, freed here and
 * at thread exit. */
static _Thread_local char *tls_exc_msg = NULL;

/* Main-thread ABI symbols (legacy consumers read these directly). */
jmp_buf fray_jmp_buf;
int fray_in_try = 0;
int fray_exception_type = 0;

void fray_exc_state_bind(void) {
    if (tls_exc_ready) return;
    tls_jmp = (jmp_buf *)malloc(sizeof(jmp_buf));
    if (!tls_jmp) { fprintf(stderr, "fray: oom\n"); exit(1); }
    tls_exc_ready = true;
}

jmp_buf *fray_exc_jmp_buf(void) {
    if (!tls_exc_ready) {
        /* Legacy main-thread path: hand out the ABI static. */
        return &fray_jmp_buf;
    }
    return tls_jmp;
}

int *fray_exc_in_try(void) {
    return tls_exc_ready ? &tls_in_try : &fray_in_try;
}

int *fray_exc_type(void) {
    return tls_exc_ready ? &tls_exc_type : &fray_exception_type;
}

void fray_try_begin(void) {
    (*fray_exc_in_try())++;
    *fray_exc_type() = 0;
}

/* Leave the try context. The type is left pending on purpose: generated code
 * then either marks it handled (fray_exc_clear, in a matching handler) or hands
 * it on (fray_exc_rethrow, after the finally). Clearing here would lose the
 * exception of a try whose clauses did not match. */
void fray_try_end(void) {
    (*fray_exc_in_try())--;
}

void fray_exc_clear(void) {
    *fray_exc_type() = 0;
    free(tls_exc_msg);
    tls_exc_msg = NULL;
}

void fray_exc_rethrow(void) {
    int type = *fray_exc_type();
    if (!type) return;              /* handled, or nothing was thrown */
    if (*fray_exc_in_try()) return; /* an enclosing try polls for it */
    /* Nothing handles it: this is where the oracle stops too. */
    fprintf(stderr, "%s\n", tls_exc_msg ? tls_exc_msg : "uncaught exception");
    exit(1);
}

void fray_throw(int exc_type, const char *msg) {
    int *in_try = fray_exc_in_try();
    if (*in_try) {
        /* Own the message: the caller may have built it on its own stack
         * (fray_call does), and a re-raise after a finally happens later. */
        if (msg != tls_exc_msg) {
            free(tls_exc_msg);
            tls_exc_msg = msg ? strdup(msg) : NULL;
        }
        *fray_exc_type() = exc_type;
        return;
    }
    /* No handler: print and abort */
    fprintf(stderr, "%s\n", msg);
    exit(1);
}

#ifdef _WIN32
/* MinGW stack probe stub for LLVM-generated code with large stack frames */
void __chkstk(void) {}
#endif

/* ── Allocation counters for out-of-heap objects ── */

static size_t g_total_objects = 0; /* live, out-of-heap + in-heap */
static size_t g_total_frees = 0;

/* ── Type descriptors ──
 * v1: tracing hooks are null; trace/finalize live as static functions here.
 * A tracing GC (post-v1) fills these in without touching the ABI.        */static void list_trace(FrayObj *self)   { (void)self; }
static void tuple_trace(FrayObj *self)  { (void)self; }
static void set_trace(FrayObj *self)    { (void)self; }
static void option_trace(FrayObj *self) { (void)self; }
static void result_trace(FrayObj *self) { (void)self; }
const FrayTypeInfo fray_type_none     = { "none",     sizeof(FrayObj), NULL,        NULL };
const FrayTypeInfo fray_type_int      = { "int",      sizeof(FrayObj), NULL,        NULL };
const FrayTypeInfo fray_type_float    = { "float",    sizeof(FrayObj), NULL,        NULL };
const FrayTypeInfo fray_type_bool     = { "bool",     sizeof(FrayObj), NULL,        NULL };
const FrayTypeInfo fray_type_string   = { "string",   sizeof(FrayObj), NULL,        NULL };
const FrayTypeInfo fray_type_list     = { "list",     sizeof(FrayObj), list_trace,  NULL };
const FrayTypeInfo fray_type_tuple    = { "tuple",    sizeof(FrayObj), tuple_trace, NULL };
const FrayTypeInfo fray_type_set      = { "set",      sizeof(FrayObj), set_trace,   NULL };
const FrayTypeInfo fray_type_function = { "function", sizeof(FrayObj), NULL,        NULL };
const FrayTypeInfo fray_type_coroutine = { "coroutine", sizeof(FrayObj), NULL,       NULL };
const FrayTypeInfo fray_type_channel = { "channel", sizeof(FrayObj), NULL,         NULL };
const FrayTypeInfo fray_type_option  = { "option",  sizeof(FrayObj), option_trace, NULL };
const FrayTypeInfo fray_type_result  = { "result",  sizeof(FrayObj), result_trace, NULL };

/* ── Allocation ── */

static FrayObj *alloc_obj(FrayTag tag, const FrayTypeInfo *type, size_t extra) {
    FrayObj *obj = (FrayObj *)calloc(1, sizeof(FrayObj) + extra);
    if (!obj) {
        fprintf(stderr, "fray: out of memory\n");
        exit(1);
    }
    obj->tag = (uint8_t)tag;
    obj->type = type;
    atomic_init(&obj->refcount, 1);
    g_total_objects++;
    return obj;
}

static FrayObj *alloc_tracked(FrayTag tag, const FrayTypeInfo *type) {
    FrayObj *obj = alloc_obj(tag, type, 0);
    obj->gc_flags = FRAY_GC_HEAP;
    fray_gc_notify_alloc(obj);
    /* Allocation is the collection trigger point. The freshly-linked
     * object has one strong reference (ours) and no internal edges, so a
     * collection here is always safe. */
    fray_gc_maybe_collect();
    return obj;
}

/* ── Leaf-value pool ──
 *
 * Ints, floats and bools are allocated and freed far more often than
 * everything else put together: every arithmetic result, every list
 * element, every loop counter. They share one object size, own no extra
 * storage, and have nothing to finalize, so a dead one is a perfectly
 * good box for the next result. Recycling turns the box/unbox traffic of
 * a hot loop into a handful of field writes.
 *
 * Safety notes:
 *  - An object only reaches the pool once its strong count hit zero, so
 *    it is provably unreachable: no live reference, no container entry
 *    and no root can still name it.
 *  - Weakly-referenced objects are never pooled. The weak registry is
 *    keyed by address and expects to see death through the normal free
 *    path, where the entry is dropped.
 *  - Per-thread, so no locking and no cross-thread hand-off (an object
 *    goes back to the pool of whichever thread freed it).
 */
#define LEAF_POOL_CAP 1024

static _Thread_local FrayObj *leaf_pool[LEAF_POOL_CAP];
static _Thread_local size_t leaf_pool_len = 0;

static void thread_locals_atexit_once(void);

static FrayObj *alloc_leaf(FrayTag tag, const FrayTypeInfo *type) {
    thread_locals_atexit_once();
    if (leaf_pool_len) {
        FrayObj *obj = leaf_pool[--leaf_pool_len];
        /* Restore every field a fresh calloc'd object would have. */
        obj->tag = tag;
        obj->gc_flags = 0;
        obj->generation = 0;
        obj->owner = 0;
        obj->type = type;
        obj->gc_prev = NULL;
        obj->gc_next = NULL;
        obj->as.i = 0;   /* clears the whole payload union */
        atomic_store_explicit(&obj->refcount, 1, memory_order_relaxed);
        g_total_objects++;
        return obj;
    }
    return alloc_obj(tag, type, 0);
}

/* ── Constructors ── */

FrayValue fray_none(void) {
    return NULL;
}

FrayValue fray_int(int64_t v) {
    FrayObj *obj = alloc_leaf(TAG_INT, &fray_type_int);
    obj->as.i = v;
    return obj;
}

FrayValue fray_float(double v) {
    FrayObj *obj = alloc_leaf(TAG_FLOAT, &fray_type_float);
    obj->as.f = v;
    return obj;
}

FrayValue fray_bool(bool v) {
    FrayObj *obj = alloc_leaf(TAG_BOOL, &fray_type_bool);
    obj->as.b = v;
    return obj;
}

FrayValue fray_string(const char *s, size_t len) {
    FrayObj *obj = alloc_obj(TAG_STRING, &fray_type_string, 0);
    obj->as.str.data = (char *)malloc(len + 1);
    if (!obj->as.str.data) {
        fprintf(stderr, "fray: out of memory\n");
        exit(1);
    }
    if (len) memcpy(obj->as.str.data, s, len);
    obj->as.str.data[len] = '\0';
    obj->as.str.len = len;
    return obj;
}

FrayValue fray_string_copy(const char *s) {
    return fray_string(s, strlen(s));
}

/* Adopt a buffer the caller already owns instead of copying it.
 *
 * The string finalizer frees `data` exactly as it does for a string built by
 * fray_string, so the result is indistinguishable from one afterwards; the
 * only difference is that this one must be given a malloc'd buffer with room
 * for a terminating NUL at `len`, which the caller then must not free. Bulk
 * reads are the reason this exists: fray_file_read used to allocate a second
 * buffer the size of the file and memcpy into it, and for multi-megabyte
 * files the extra transient allocation and the copy cost more than the read.
 * Passing NULL makes it behave exactly like fray_string. */
FrayValue fray_string_take(char *owned, size_t len) {
    FrayObj *obj = alloc_obj(TAG_STRING, &fray_type_string, 0);
    if (owned) {
        obj->as.str.data = owned;
    } else {
        obj->as.str.data = (char *)malloc(len + 1);
        if (!obj->as.str.data) {
            fprintf(stderr, "fray: out of memory\n");
            exit(1);
        }
    }
    obj->as.str.data[len] = '\0';
    obj->as.str.len = len;
    return obj;
}

FrayValue fray_list(void) {
    FrayObj *obj = alloc_tracked(TAG_LIST, &fray_type_list);
    obj->as.list.elems = NULL;
    obj->as.list.len = 0;
    obj->as.list.cap = 0;
    return obj;
}

FrayValue fray_tuple(size_t len) {
    FrayObj *obj = alloc_tracked(TAG_TUPLE, &fray_type_tuple);
    obj->as.tuple.elems = (FrayValue *)calloc(len ? len : 1, sizeof(FrayValue));
    if (!obj->as.tuple.elems) {
        fprintf(stderr, "fray: out of memory\n");
        exit(1);
    }
    obj->as.tuple.len = len;
    return obj;
}

FrayValue fray_set(void) {
    FrayObj *obj = alloc_tracked(TAG_SET, &fray_type_set);
    obj->as.set.elems = NULL;
    obj->as.set.len = 0;
    obj->as.set.cap = 0;
    return obj;
}

FrayValue fray_function(const char *name, void *code, int arity) {
    FrayObj *obj = alloc_obj(TAG_FUNCTION, &fray_type_function, 0);
    obj->as.func.name = strdup(name ? name : "<anon>");
    obj->as.func.code = code;
    obj->as.func.arity = arity;
    return obj;
}

/* ── Phase 7: coroutine & channel objects ── */

FrayValue fray_coro_object(void *state) {
    FrayObj *obj = alloc_obj(TAG_COROUTINE, &fray_type_coroutine, 0);
    obj->as.coro.fiber = NULL;
    obj->as.coro.state = state;
    return obj;
}

FrayValue fray_channel_object(void *state) {
    FrayObj *obj = alloc_obj(TAG_CHANNEL, &fray_type_channel, 0);
    obj->as.chan.chan = state;
    return obj;
}

/* ── Freeing (finalization) ── */

void fray_free_object(FrayObj *v) {
    /* The collector may have condemned this container already: the kill
     * cascade dropped every reference it held, and the deferred-free queue
     * still owns it. Tearing it down as a live object would release those
     * elements a second time — freeing objects live code still points at —
     * and would leave the queue holding recycled memory for a later free. */
    if (fray_gc_pending_take(v)) {
        fray_gc_free_condemned(v);
        return;
    }
    /* Drop weak handles first so no lock() can ever see a dying object. */
    fray_weak_clear_for(v);
    switch (v->tag) {
        case TAG_STRING:
            free(v->as.str.data);
            break;
        case TAG_LIST:
            for (size_t i = 0; i < v->as.list.len; i++)
                fray_release(v->as.list.elems[i]);
            free(v->as.list.elems);
            break;
        case TAG_TUPLE:
            for (size_t i = 0; i < v->as.tuple.len; i++)
                fray_release(v->as.tuple.elems[i]);
            free(v->as.tuple.elems);
            break;
        case TAG_SET:
            for (size_t i = 0; i < v->as.set.len; i++)
                fray_release(v->as.set.elems[i]);
            free(v->as.set.elems);
            break;
        case TAG_FUNCTION:
            free(v->as.func.name);
            break;
        case TAG_COROUTINE:
            /* Drop the handle's reference on the Coro (coroutine.c). */
            if (v->as.coro.state) {
                extern void fray_coro_state_release(void *state);
                fray_coro_state_release(v->as.coro.state);
            }
            break;
        case TAG_CHANNEL:
            if (v->as.chan.chan) {
                extern void fray_channel_state_release(void *state);
                fray_channel_state_release(v->as.chan.chan);
            }
            break;
        case TAG_STRUCT:
            if (v->as.st.state) {
                extern void fray_struct_state_release(void *state);
                fray_struct_state_release(v->as.st.state);
            }
            break;
        case TAG_MAP:
            if (v->as.mp.state) {
                extern void fray_map_state_release(void *state);
                fray_map_state_release(v->as.mp.state);
            }
            break;
        case TAG_OPTION:
            if (v->as.opt.val) fray_release(v->as.opt.val);
            break;
        case TAG_RESULT:
            if (v->as.res.val) fray_release(v->as.res.val);
            if (v->as.res.err) fray_release(v->as.res.err);
            break;
        default:
            break;
    }
    g_total_objects--;
    g_total_frees++;
    free(v);
}

/* ── Reference counting ── */

void fray_retain(FrayValue v) {
    if (v) atomic_fetch_add_explicit(&v->refcount, 1, memory_order_relaxed);
}

/* An untracked object whose strong count reached zero: recycle it when it
 * is a leaf value, otherwise finalize it. Bookkeeping matches a real free
 * (the object is dead either way; the pool just keeps its memory). */
static void release_untracked(FrayObj *v) {
    if (leaf_pool_len < LEAF_POOL_CAP && !(v->gc_flags & FRAY_GC_WEAKABLE)) {
        switch (v->tag) {
            case TAG_INT:
            case TAG_FLOAT:
            case TAG_BOOL:
                leaf_pool[leaf_pool_len++] = v;
                g_total_objects--;
                g_total_frees++;
                return;
            default:
                break;
        }
    }
    fray_free_object(v);
}

void fray_release(FrayValue v) {
    if (!v) return;
    if (atomic_fetch_sub_explicit(&v->refcount, 1, memory_order_acq_rel) > 1) {
        /* A container that loses a strong reference but stays positive is a
         * possible cycle root: only a cycle (or another root) can explain
         * the surviving count. The collector decides. */
        if (v->gc_flags & FRAY_GC_HEAP)
            fray_gc_decrement_notify(v);
        return;
    }
    /* Strong count hit zero: truly dead. Free immediately — the collector
     * never needs to see this object. */
    if (v->gc_flags & FRAY_GC_HEAP) {
        fray_gc_release_tracked(v);
        return;
    }
    release_untracked(v);
}

/* Root stack: see cycles.c header comment. Generated code does not touch
 * it, but every container mutation does, so pushes/pops sit on the hot
 * path of a build-and-fill loop. A growable array per thread keeps a push
 * to a bounds check and a store: the previous malloc-per-entry list cost
 * two malloc/free pairs for every element appended to a list. */

static _Thread_local FrayValue *roots_buf = NULL;
static _Thread_local size_t roots_len = 0;
static _Thread_local size_t roots_cap = 0;

static void roots_reserve(size_t extra) {
    if (roots_len + extra <= roots_cap) return;
    size_t cap = roots_cap ? roots_cap : 32;
    while (cap < roots_len + extra) cap *= 2;
    FrayValue *p = (FrayValue *)realloc(roots_buf, cap * sizeof(FrayValue));
    if (!p) {
        fprintf(stderr, "fray: out of memory\n");
        exit(1);
    }
    roots_buf = p;
    roots_cap = cap;
}

void fray_root_push(FrayValue v) {
    roots_reserve(1);
    roots_buf[roots_len++] = v;
}

void fray_root_pop(void) {
    if (roots_len) roots_len--;
}

static _Thread_local FrayValue *roots_scratch = NULL;  /* enumeration copy */
static _Thread_local size_t roots_scratch_cap = 0;

FrayValue *fray_root_stack(size_t *out_len) {
    if (roots_len > roots_scratch_cap) {
        free(roots_scratch);
        roots_scratch_cap = roots_len * 2;
        roots_scratch = (FrayValue *)malloc(roots_scratch_cap * sizeof(FrayValue));
        if (!roots_scratch) {
            *out_len = 0;
            return NULL;
        }
    }
    /* Newest first, matching the historical linked-list order. */
    for (size_t i = 0; i < roots_len; i++)
        roots_scratch[i] = roots_buf[roots_len - 1 - i];
    *out_len = roots_len;
    return roots_scratch;
}

/* ── Per-thread teardown ──
 *
 * The leaf pool and the root stack are thread-local scratch space. A
 * thread that exits must hand them back: a program that spawns many
 * short-lived threads would otherwise retain a pool's worth of dead boxes
 * for every one of them. Called from the spawn trampoline on thread exit
 * and from the exit handler for the main thread. */
void fray_thread_locals_free(void) {
    for (size_t i = 0; i < leaf_pool_len; i++)
        free(leaf_pool[i]);
    leaf_pool_len = 0;
    free(roots_buf);
    roots_buf = NULL;
    roots_len = 0;
    roots_cap = 0;
    free(roots_scratch);
    roots_scratch = NULL;
    roots_scratch_cap = 0;
    if (tls_exc_ready) {
        free(tls_jmp);
        tls_jmp = NULL;
        tls_exc_ready = false;
    }
    free(tls_exc_msg);
    tls_exc_msg = NULL;
}

/* Main-thread teardown at process exit (threads flush via the spawn
 * trampoline). Registering more than once is harmless. */
static void thread_locals_atexit_once(void) {
    static _Thread_local bool registered = false;
    if (registered) return;
    registered = true;
    atexit(fray_thread_locals_free);
}

/* ── Weak references (id registry) ──
 * Handles stay stable across target death: lock() returns NULL instead of
 * ever dereferencing freed memory. Registry compaction can come later. */

typedef struct WeakEntry {
    int64_t id;
    FrayValue target;
    struct WeakEntry *next;
} WeakEntry;

static void locks_init(void);

static pthread_mutex_t g_weak_lock;   /* guards the weak-registry list */

static WeakEntry *g_weak_head = NULL;
static int64_t g_weak_next_id = 1;

/* Private to this file: the mutex is reached directly rather than through a
 * published symbol, so nothing outside objects.c can take the weak-registry
 * lock without going through a fray_weak_* entry point. */
static void weak_registry_lock(void)   { pthread_mutex_lock(&g_weak_lock); }
static void weak_registry_unlock(void) { pthread_mutex_unlock(&g_weak_lock); }

int64_t fray_weak_new(FrayValue v) {
    locks_init();
    weak_registry_lock();
    WeakEntry *e = (WeakEntry *)malloc(sizeof(WeakEntry));
    if (!e) { weak_registry_unlock(); return -1; }
    e->id = g_weak_next_id++;
    e->target = v;
    e->next = g_weak_head;
    g_weak_head = e;
    if (v) v->gc_flags |= FRAY_GC_WEAKABLE;
    weak_registry_unlock();
    return e->id;
}

FrayValue fray_weak_lock(int64_t id) {
    locks_init();
    weak_registry_lock();
    for (WeakEntry *e = g_weak_head; e; e = e->next) {
        if (e->id == id) {
            FrayValue t = e->target;
            /* Target is cleared under this same lock before its memory is
             * freed, so reading/copying it here is race-free. */
            weak_registry_unlock();
            if (t) fray_retain(t);
            return t;
        }
    }
    weak_registry_unlock();
    return NULL;
}

void fray_weak_del(int64_t id) {
    locks_init();
    weak_registry_lock();
    WeakEntry **p = &g_weak_head;
    while (*p) {
        if ((*p)->id == id) {
            WeakEntry *dead = *p;
            *p = dead->next;
            free(dead);
            weak_registry_unlock();
            return;
        }
        p = &(*p)->next;
    }
    weak_registry_unlock();
}

void fray_weak_clear_for(FrayValue v) {
    locks_init();
    weak_registry_lock();
    WeakEntry **p = &g_weak_head;
    while (*p) {
        if ((*p)->target == v) {
            WeakEntry *dead = *p;
            *p = dead->next;
            free(dead);
        } else {
            p = &(*p)->next;
        }
    }
    weak_registry_unlock();
}

/* ── Concurrency: thin striped object locks (Phase 6) ──
 *
 * 256-shard table of mutexes hashed by object address. Structural
 * mutation of a possibly-shared container (append/setindex/depend/atomic
 * payload writes) takes the shard; refcounts stay lock-free atomics.
 * The per-callsite discipline: lock, mutate, unlock — never nest. */

#define LOCK_SHARDS 256

static pthread_mutex_t g_obj_locks[LOCK_SHARDS];
static bool  g_locks_ready = false;

static bool g_weak_ready = false;

static void locks_init_impl(void) {
    for (int i = 0; i < LOCK_SHARDS; i++)
        pthread_mutex_init(&g_obj_locks[i], NULL);
    pthread_mutex_init(&g_weak_lock, NULL);
    g_locks_ready = true;
    g_weak_ready = true;
}

static void locks_init(void) {
    static pthread_once_t once = PTHREAD_ONCE_INIT;
    pthread_once(&once, locks_init_impl);
}

static inline size_t lock_shard(FrayValue v) {
    uintptr_t h = (uintptr_t)v;
    return (h >> 4) & (LOCK_SHARDS - 1);
}

int fray_obj_lock(FrayValue v) {
    if (!v) return 0;
    locks_init();
    pthread_mutex_lock(&g_obj_locks[lock_shard(v)]);
    return 0;
}

void fray_obj_unlock(FrayValue v) {
    if (!v) return;
    pthread_mutex_unlock(&g_obj_locks[lock_shard(v)]);
}

/* Bulk acquire/release for the collector: excludes all container
 * structural mutation while it walks edges. Fixed index order. */
void fray_obj_locks_acquire_all(void) {
    locks_init();
    for (int i = 0; i < LOCK_SHARDS; i++)
        pthread_mutex_lock(&g_obj_locks[i]);
}

void fray_obj_locks_release_all(void) {
    for (int i = LOCK_SHARDS - 1; i >= 0; i--)
        pthread_mutex_unlock(&g_obj_locks[i]);
}

bool fray_is_bool_val(FrayValue v) { return v && v->tag == TAG_BOOL; }
