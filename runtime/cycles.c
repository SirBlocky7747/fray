/**
 * fray runtime — trial-deletion cycle collector (Phases 5 + 6).
 *
 * Model: `refcount` counts EVERY strong reference, including container ->
 * element edges inside the GC heap. Consequences:
 *
 *  - rc == 0  => the object is truly dead: freed immediately, no collector
 *    involvement. Acyclic garbage (the overwhelmingly common case) costs
 *    one atomic decrement and a free.
 *  - A decrement that leaves rc > 0 on a *container* (list/tuple/set) is a
 *    possible cycle root — only a cycle can explain a container surviving
 *    the loss of an external reference. The root enters a candidate buffer.
 *  - Collection is the classic three-phase trial deletion (Bacon–Rajan):
 *      1. SUBTRACT — from each candidate, walk the subgraph and move its
 *         internal edge counts into a scratch buffer.
 *      2. SCAN     — objects whose (rc + scratch) is still positive are
 *         externally alive: restore what they borrowed, transitively
 *         (mark-black). The rest are garbage.
 *      3. KILL     — collect_white over the garbage: every internal edge
 *         is decremented exactly once (colors dedup visits), memory stays
 *         allocated until every decrement is done, then payloads are
 *         freed in one final pass. Order can never touch freed memory.
 *
 * Phase 6 threading ("GC takes the container locks" model):
 *  - Each fray thread owns a GcSpace: its generation lists, allocation
 *    counters, candidate buffer and trial scratch, guarded by that space's
 *    (recursive) mutex. Objects record their owning space in `owner`.
 *  - Collections run under the world lock holding EVERY space lock and
 *    EVERY striped shard lock: generation-list mutations, candidate-buffer
 *    pushes and all container structural mutations are excluded for the
 *    (brief) collection. Pure arithmetic and atomic-box threads keep
 *    running — there is no GIL.
 *  - Lock order (always): world > spaces(all, index order) > shard.
 *    Release cascades take space locks one at a time in index order, and
 *    space mutexes are recursive, so same-thread reentry (kill phase
 *    freeing an object owned by the collecting space) is legal.
 *  - Cross-space hazard: a kill driven by thread A can free a B-owned
 *    container still sitting in a candidate buffer. Freed containers go
 *    to the pending queue; the final drain (all locks held) frees entries
 *    no buffer references.
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct ScratchEntry { FrayObj *obj; int64_t count; } ScratchEntry;

/* ── Per-thread GC space ── */

struct GcSpace {
    pthread_mutex_t lock;    /* recursive: same-thread reentry is legal   */

    FrayObj *young_head;
    FrayObj *old_head;
    size_t   young_count;
    size_t   old_count;
    size_t   alloc_count;      /* tracked (container) allocations          */
    size_t   young_gc_runs;
    size_t   major_gc_runs;
    size_t   collected_total;

    /* possible-cycle-root buffer */
    FrayObj **root_buf;
    size_t    root_buf_len;

    /* trial scratch (epoch-reset) */
    ScratchEntry *scratch;
    size_t   scratch_len, scratch_cap, scratch_floor;

    /* trial worklist */
    FrayObj **work;
    size_t   work_len, work_cap;
    size_t   scan_mark;

    int      owner;            /* index into g_spaces; -1 = parked         */
    bool     parked;           /* owning thread exited; space is empty     */
};

/* ── Global registry ──
 * g_spaces slots are STABLE once created: an exiting thread parks its
 * (emptied) space for reuse rather than removing it, so owner indices and
 * g_spaces[] reads on hot paths never dangle. */

#define MAX_SPACES 64

static _Thread_local GcSpace *tls_space = NULL;

static GcSpace *g_spaces[MAX_SPACES];
static int      g_space_count = 0;      /* slots ever created               */
static int      g_active_threads = 0;   /* threads currently attached       */

static pthread_mutex_t g_spaces_lock;
static pthread_mutex_t g_world_lock;

static void registry_init(void);
static void gen_unlink(FrayObj **head, size_t *count, FrayObj *obj);

void fray_world_lock(void)   { registry_init(); pthread_mutex_lock(&g_world_lock); }
void fray_world_unlock(void) { pthread_mutex_unlock(&g_world_lock); }

static void registry_init_impl(void) {
    pthread_mutexattr_t a;
    pthread_mutexattr_init(&a);
    pthread_mutexattr_settype(&a, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(&g_spaces_lock, &a);
    pthread_mutex_init(&g_world_lock, NULL); /* never re-entered */
    pthread_mutexattr_destroy(&a);
}

static void registry_init(void) {
    static pthread_once_t once = PTHREAD_ONCE_INIT;
    pthread_once(&once, registry_init_impl);
}

/* ── Pending-free queue ──
 *
 * Owns the containers the kill phase condemned: the cascade already dropped
 * every reference they held, so their payload must be torn down exactly once
 * and must never be released again on the way out (see
 * fray_gc_free_condemned). The queue has its own lock rather than riding on
 * the world lock: freeing a container here can cascade into fray_release and
 * back into this queue, and the world lock is not recursive.
 *
 * Lock order is world > spaces > shard > pending: every site takes the queue
 * lock last, and the drain drops it before freeing anything.
 */

/* Growable, not a fixed array: a collection routinely condemns more
 * containers than a small cap allows, and a dropped entry is memory whose
 * references the cascade already dropped — freeing it later through the
 * normal path would release its elements a second time. Nothing may be
 * silently forgotten here. */
static FrayObj **g_pending = NULL;
static size_t    g_pending_len = 0;
static size_t    g_pending_cap = 0;
static pthread_mutex_t g_pending_lock = PTHREAD_MUTEX_INITIALIZER;

void fray_threads_pending_push(FrayValue v) {
    if (v->gc_flags & FRAY_GC_QUEUED) return;   /* already queued once */
    pthread_mutex_lock(&g_pending_lock);
    if (g_pending_len == g_pending_cap) {
        size_t cap = g_pending_cap ? g_pending_cap * 2 : 256;
        FrayObj **grown = (FrayObj **)realloc(g_pending, cap * sizeof(FrayObj *));
        if (!grown) { fprintf(stderr, "fray: gc oom\n"); exit(1); }
        g_pending = grown;
        g_pending_cap = cap;
    }
    /* RMW: gc_flags also carries the collector's colors, and a mutator thread
     * may be setting FRAY_GC_CANDIDATE on the same byte. */
    __atomic_fetch_or(&v->gc_flags, FRAY_GC_QUEUED, __ATOMIC_RELAXED);
    g_pending[g_pending_len++] = v;
    pthread_mutex_unlock(&g_pending_lock);
}

size_t fray_gc_pending_count(void) { return g_pending_len; }

/* Take a container off the deferred-free queue. True when the collector had
 * condemned it: the caller then frees it as condemned, not as a live object
 * whose elements still hold counted references. */
bool fray_gc_pending_take(FrayValue v) {
    if (!v || !(v->gc_flags & FRAY_GC_QUEUED)) return false;
    pthread_mutex_lock(&g_pending_lock);
    for (size_t i = 0; i < g_pending_len; i++) {
        if (g_pending[i] == v) {
            g_pending[i] = g_pending[--g_pending_len];
            __atomic_fetch_and(&v->gc_flags, (uint8_t)~FRAY_GC_QUEUED,
                               __ATOMIC_RELAXED);
            pthread_mutex_unlock(&g_pending_lock);
            return true;
        }
    }
    /* Flag without an entry: repair it (a lost update would let a later free
     * tear this object down as if it were still a live one). */
    __atomic_fetch_and(&v->gc_flags, (uint8_t)~FRAY_GC_QUEUED, __ATOMIC_RELAXED);
    pthread_mutex_unlock(&g_pending_lock);
    return false;
}

/* Tear down a condemned container. The kill cascade already decremented
 * every reference it held, so its elements must NOT be released here — that
 * would drop a reference a second time and free a live object. */
void fray_gc_free_condemned(FrayValue o) {
    if (o->tag != TAG_LIST && o->tag != TAG_TUPLE && o->tag != TAG_SET) {
        /* Only containers are condemned today; anything else must still be
         * torn down normally rather than have its payload leaked. */
        fray_free_object(o);
        return;
    }
    if (o->gc_flags & FRAY_GC_LINKED) {
        GcSpace *osp = (o->owner >= 0 && o->owner < g_space_count)
                     ? g_spaces[o->owner] : NULL;
        if (osp) {
            if (o->generation == 0)
                gen_unlink(&osp->young_head, &osp->young_count, o);
            else
                gen_unlink(&osp->old_head,  &osp->old_count,  o);
        }
        o->gc_flags &= (uint8_t)~FRAY_GC_LINKED;
    }
    fray_weak_clear_for(o);
    __atomic_fetch_and(&o->gc_flags,
                       (uint8_t)~(FRAY_GC_QUEUED | FRAY_GC_CANDIDATE),
                       __ATOMIC_RELAXED);
    switch (o->tag) {
        case TAG_LIST:  free(o->as.list.elems);  break;
        case TAG_TUPLE: free(o->as.tuple.elems); break;
        case TAG_SET:   free(o->as.set.elems);   break;
        default: break;
    }
    free(o);
}

/* Requires world + ALL space locks. Frees pending containers that no space's
 * candidate buffer references; keeps the rest queued. The queue lock is
 * dropped before the frees: a teardown must never run while holding it (it
 * can take the shard locks and call into a finalizer), and the doomed objects
 * are already off the queue so no one else can see them. */
static void pending_drain_locked(void) {
    FrayObj **doomed = (FrayObj **)malloc(g_pending_len * sizeof(FrayObj *));
    if (!doomed) return;            /* keep everything queued; leak, not free */
    size_t out = 0, ndoomed = 0;

    pthread_mutex_lock(&g_pending_lock);
    for (size_t i = 0; i < g_pending_len; i++) {
        FrayObj *o = g_pending[i];
        bool referenced = false;
        for (int s = 0; s < g_space_count && !referenced; s++) {
            GcSpace *sp = g_spaces[s];
            if (!sp) continue;
            for (size_t r = 0; r < sp->root_buf_len; r++)
                if (sp->root_buf[r] == o) { referenced = true; break; }
        }
        if (!referenced) {
            __atomic_fetch_and(&o->gc_flags, (uint8_t)~FRAY_GC_QUEUED,
                               __ATOMIC_RELAXED);   /* leaving the queue */
            doomed[ndoomed++] = o;
        } else {
            g_pending[out++] = o;
        }
    }
    g_pending_len = out;
    pthread_mutex_unlock(&g_pending_lock);

    for (size_t i = 0; i < ndoomed; i++) fray_gc_free_condemned(doomed[i]);
    free(doomed);
}

/* Defined next to the candidate buffer; the spaces that own one are parked
 * and retired long before it. */
static void root_buf_clear(GcSpace *sp);

/* ── Safepoint hook ──
 * The GC model excludes mutators via locks instead of pausing threads, so
 * the safepoint is a cheap no-op kept for generated-code ABI and tests. */

void fray_gc_safepoint(void) {
}

/* ── Generation lists (doubly linked through the object header) ── */

static void gen_link(FrayObj **head, size_t *count, FrayObj *obj) {
    obj->gc_prev = NULL;
    obj->gc_next = *head;
    if (*head) (*head)->gc_prev = obj;
    *head = obj;
    (*count)++;
    obj->gc_flags |= FRAY_GC_LINKED;
}

static void gen_unlink(FrayObj **head, size_t *count, FrayObj *obj) {
    if (!(obj->gc_flags & FRAY_GC_LINKED)) return;
    if (obj->gc_prev) obj->gc_prev->gc_next = obj->gc_next;
    else *head = obj->gc_next;
    if (obj->gc_next) obj->gc_next->gc_prev = obj->gc_prev;
    obj->gc_prev = obj->gc_next = NULL;
    obj->gc_flags &= (uint8_t)~FRAY_GC_LINKED;
    (*count)--;
}

/* ── Space lifecycle ── */

GcSpace *fray_gc_space_new(void) {
    registry_init();
    GcSpace *sp = (GcSpace *)calloc(1, sizeof(GcSpace));
    if (!sp) { fprintf(stderr, "fray: gc oom\n"); exit(1); }
    pthread_mutexattr_t a;
    pthread_mutexattr_init(&a);
    pthread_mutexattr_settype(&a, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(&sp->lock, &a);
    pthread_mutexattr_destroy(&a);
    return sp;
}

FrayValue *edges_of_pub(FrayObj *o, size_t *n) {
    switch (o->tag) {
        case TAG_LIST:  *n = o->as.list.len;  return o->as.list.elems;
        case TAG_TUPLE: *n = o->as.tuple.len; return o->as.tuple.elems;
        case TAG_SET:   *n = o->as.set.len;   return o->as.set.elems;
        default:        *n = 0;               return NULL;
    }
}

/* Register a space into a stable slot (reusing parked ones). Used by
 * current_space() for lazy attach and exported for thread spawn, which
 * must register the space BEFORE the new thread starts running. */
void fray_gc_space_slot_grab(GcSpace *space) {
    registry_init();
    pthread_mutex_lock(&g_spaces_lock);
    int slot = -1;
    for (int s = 0; s < g_space_count; s++) {
        if (g_spaces[s] && g_spaces[s]->parked) { slot = s; break; }
    }
    if (slot < 0 && g_space_count < MAX_SPACES) slot = g_space_count++;
    if (slot >= 0) {
        g_spaces[slot] = space;
        space->owner = slot;
        space->parked = false;
    } else {
        space->owner = -1; /* registry full: space still usable, untracked */
    }
    pthread_mutex_unlock(&g_spaces_lock);
}

static GcSpace *current_space(void) {
    if (tls_space) return tls_space;
    registry_init();
    tls_space = fray_gc_space_new();
    fray_gc_space_slot_grab(tls_space);
    pthread_mutex_lock(&g_spaces_lock);
    g_active_threads++;
    pthread_mutex_unlock(&g_spaces_lock);
    fray_tls_thread_id = tls_space->owner;
    return tls_space;
}

GcSpace *fray_gc_current_space(void) { return current_space(); }
void fray_gc_current_space_set(GcSpace *space) { tls_space = space; }

/* threads.c: a pre-registered spawn space counts as active from birth. */
void fray_gc_active_threads_add(int delta) {
    pthread_mutex_lock(&g_spaces_lock);
    g_active_threads += delta;
    pthread_mutex_unlock(&g_spaces_lock);
}

/* threads.c: bind a pre-registered space as THIS thread's space. */
void tls_space_bind(GcSpace *space) {
    registry_init();
    tls_space = space;
    fray_tls_thread_id = space->owner;
}

void *fray_thread_gc_space(int thread_id) {
    registry_init();
    pthread_mutex_lock(&g_spaces_lock);
    GcSpace *sp = (thread_id >= 0 && thread_id < g_space_count)
                ? g_spaces[thread_id] : NULL;
    pthread_mutex_unlock(&g_spaces_lock);
    return sp;
}

void fray_threads_foreach_space(void (*cb)(void *space, void *ctx), void *ctx) {
    registry_init();
    pthread_mutex_lock(&g_spaces_lock);
    for (int s = 0; s < g_space_count; s++)
        if (g_spaces[s]) cb(g_spaces[s], ctx);
    pthread_mutex_unlock(&g_spaces_lock);
}

int fray_threads_registered(void) {
    registry_init();
    pthread_mutex_lock(&g_spaces_lock);
    int n = g_active_threads;
    pthread_mutex_unlock(&g_spaces_lock);
    return n;
}

/* Thread exit: free every container still on the space's lists, reset the
 * space, park it (slot stays valid for reuse). */
void fray_gc_space_retire(GcSpace *sp) {
    /* NOTE: called on thread exit; each spawned thread attached exactly one
     * space, so its exit decrements the active count. Spaces attached via
     * current_space() incremented on attach, so retire always balances. */
    pthread_mutex_lock(&sp->lock);
    for (int pass = 0; pass < 2; pass++) {
        FrayObj **head = pass == 0 ? &sp->young_head : &sp->old_head;
        size_t  *count = pass == 0 ? &sp->young_count : &sp->old_count;
        FrayObj *o = *head;
        while (o) {
            FrayObj *next = o->gc_next;
            gen_unlink(head, count, o);
            /* Elements hold counted references; releasing them may cascade
             * into other spaces (locks one at a time, index order). */
            size_t n;
            FrayValue *e = edges_of_pub(o, &n);
            for (size_t i = 0; i < n; i++) fray_release(e[i]);
            fray_free_object(o);
            o = next;
        }
    }
    root_buf_clear(sp);
    sp->scratch_len = sp->scratch_floor = 0;
    sp->work_len = 0;
    pthread_mutex_unlock(&sp->lock);

    pthread_mutex_lock(&g_spaces_lock);
    sp->parked = true;
    sp->owner = -1;
    g_active_threads--;
    pthread_mutex_unlock(&g_spaces_lock);
}

/* ── Allocation notification ── */

void fray_gc_notify_alloc(FrayValue v) {
    if (!v) return;
    GcSpace *sp = current_space();
    if (!sp) return;
    v->owner = sp->owner;
    pthread_mutex_lock(&sp->lock);
    gen_link(&sp->young_head, &sp->young_count, v);
    sp->alloc_count++;
    pthread_mutex_unlock(&sp->lock);
}

/* ── Possible-cycle-root buffer ──
 *
 * A SET of candidate roots, not a log: an object is listed at most once per
 * collection (FRAY_GC_CANDIDATE tracks that). Duplicates would not change any
 * trial-deletion count, but they would eat the fixed capacity — silently
 * dropping roots the collector needs — and they are what let a dead object
 * leave an entry behind (see fray_gc_release_tracked).
 *
 * The flag and the colors share one byte, so both sides use read-modify-write
 * atomics: a plain `gc_flags |= flag` next to a color update from another
 * thread would drop one of the two. */

#define ROOT_BUF_CAP 4096

void fray_gc_decrement_notify(FrayValue v) {
    if (!v || !(v->gc_flags & FRAY_GC_HEAP)) return;
    if (v->tag != TAG_LIST && v->tag != TAG_TUPLE && v->tag != TAG_SET) return;
    GcSpace *sp = current_space();
    if (!sp) return;
    /* Cross-space root: attribute it to the OWNING space's buffer (buffers
     * only ever hold owner-space objects; see the kill/pending protocol). */
    GcSpace *osp = sp;
    if (v->owner >= 0 && v->owner != sp->owner) {
        pthread_mutex_lock(&g_spaces_lock);
        if (v->owner < g_space_count && g_spaces[v->owner] &&
            !g_spaces[v->owner]->parked)
            osp = g_spaces[v->owner];
        pthread_mutex_unlock(&g_spaces_lock);
    }
    pthread_mutex_lock(&osp->lock);
    if (!osp->root_buf)
        osp->root_buf = (FrayObj **)malloc(ROOT_BUF_CAP * sizeof(FrayObj *));
    if (osp->root_buf && osp->root_buf_len < ROOT_BUF_CAP &&
        !(v->gc_flags & FRAY_GC_CANDIDATE)) {
        __atomic_fetch_or(&v->gc_flags, FRAY_GC_CANDIDATE, __ATOMIC_RELAXED);
        osp->root_buf[osp->root_buf_len++] = v;
    }
    pthread_mutex_unlock(&osp->lock);
}

/* Every entry leaves the buffer the same way: drop all of them and clear the
 * flag that kept them unique. Must be called with the buffer's space locked. */
static void root_buf_clear(GcSpace *sp) {
    for (size_t r = 0; r < sp->root_buf_len; r++) {
        FrayObj *o = sp->root_buf[r];
        if (o) __atomic_fetch_and(&o->gc_flags, (uint8_t)~FRAY_GC_CANDIDATE,
                                  __ATOMIC_RELAXED);
    }
    sp->root_buf_len = 0;
}

/* ── Container helpers ── */

static inline bool container_p(FrayObj *o) {
    return o->tag == TAG_LIST || o->tag == TAG_TUPLE || o->tag == TAG_SET;
}

/* ── Scratch counts (trial deletion) ── */

static void scratch_add(GcSpace *sp, FrayObj *obj, int64_t delta) {
    for (size_t i = sp->scratch_len; i-- > sp->scratch_floor;) {
        if (sp->scratch[i].obj == obj) {
            sp->scratch[i].count += delta;
            return;
        }
    }
    if (sp->scratch_len == sp->scratch_cap) {
        sp->scratch_cap = sp->scratch_cap ? sp->scratch_cap * 2 : 64;
        sp->scratch = (ScratchEntry *)realloc(sp->scratch,
                        sp->scratch_cap * sizeof(ScratchEntry));
        if (!sp->scratch) { fprintf(stderr, "fray: gc oom\n"); exit(1); }
    }
    sp->scratch[sp->scratch_len].obj = obj;
    sp->scratch[sp->scratch_len].count = delta;
    sp->scratch_len++;
}

static int64_t scratch_get(GcSpace *sp, FrayObj *obj) {
    for (size_t i = sp->scratch_len; i-- > sp->scratch_floor;) {
        if (sp->scratch[i].obj == obj) return sp->scratch[i].count;
    }
    return 0;
}

/* ── Worklist ── */

static void work_push(GcSpace *sp, FrayObj *obj) {
    if (sp->work_len == sp->work_cap) {
        sp->work_cap = sp->work_cap ? sp->work_cap * 2 : 64;
        sp->work = (FrayObj **)realloc(sp->work, sp->work_cap * sizeof(FrayObj *));
        if (!sp->work) { fprintf(stderr, "fray: gc oom\n"); exit(1); }
    }
    sp->work[sp->work_len++] = obj;
}

/* ── Colors ── */

static inline void obj_set_color(FrayObj *o, uint8_t c) {
    /* Read-modify-write so a concurrent flag update (FRAY_GC_CANDIDATE from
     * another thread's decrement notify) is never overwritten. */
    __atomic_fetch_and(&o->gc_flags, (uint8_t)~FRAY_GC_COLOR_MASK,
                       __ATOMIC_RELAXED);
    __atomic_fetch_or(&o->gc_flags, (uint8_t)(c & FRAY_GC_COLOR_MASK),
                      __ATOMIC_RELAXED);
}
static inline uint8_t obj_color_of(FrayObj *o) {
    return (uint8_t)(o->gc_flags & FRAY_GC_COLOR_MASK);
}

/* ── Promotion ── */

static void promote(GcSpace *sp, FrayObj *obj) {
    if (obj->generation == 0) {
        gen_unlink(&sp->young_head, &sp->young_count, obj);
        obj->generation = 1;
        gen_link(&sp->old_head, &sp->old_count, obj);
    }
}

/* ── Phase 2 restore (mark-black) ──
 *
 * Give back exactly what phase 1 borrowed: one count for every edge leaving a
 * node that turned out to be externally alive, and the same for everything
 * that keeps alive. The scan starts at the slot this call pushes its root
 * into, so each node is restored once — restarting from scan_mark would add
 * a count for the same edge on every call and hide real garbage forever. */

static void mark_black(GcSpace *sp, FrayObj *root) {
    obj_set_color(root, FRAY_GC_BLACK);
    size_t t = sp->work_len;
    work_push(sp, root);
    for (; t < sp->work_len; t++) {
        FrayObj *o = sp->work[t];
        size_t n;
        FrayValue *e = edges_of_pub(o, &n);
        for (size_t i = 0; i < n; i++) {
            FrayObj *child = e[i];
            if (!child || !container_p(child)) continue;
            scratch_add(sp, child, +1);
            if (obj_color_of(child) != FRAY_GC_BLACK) {
                obj_set_color(child, FRAY_GC_BLACK);
                work_push(sp, child);
            }
        }
    }
}

/* ── Phase 3 kill ── */

static void kill_phase(GcSpace *sp) {
    size_t free_start = sp->work_len;

    for (size_t t = 0; t < free_start; t++) {
        FrayObj *o = sp->work[t];
        if (obj_color_of(o) == FRAY_GC_DEAD) {
            obj_set_color(o, FRAY_GC_GRAY);
            work_push(sp, o);
        }
    }

    for (size_t t = free_start; t < sp->work_len; t++) {
        FrayObj *o = sp->work[t];
        size_t n;
        FrayValue *e = edges_of_pub(o, &n);
        for (size_t i = 0; i < n; i++) {
            FrayObj *child = e[i];
            if (!child) continue;
            if (atomic_fetch_sub_explicit(&child->refcount, 1,
                                          memory_order_relaxed) == 1) {
                if (container_p(child)) {
                    if (obj_color_of(child) != FRAY_GC_GRAY) {
                        obj_set_color(child, FRAY_GC_GRAY);
                        work_push(sp, child);
                    }
                } else {
                    /* Bare payloads (int/float/bool/string) have no
                     * container children; free inline. */
                    fray_free_object(child);
                }
            }
        }
    }

    /* Final pass: queue every dead container for deferred free (world lock
     * is held by the collector, which pending_push requires). */
    for (size_t t = free_start; t < sp->work_len; t++) {
        FrayObj *o = sp->work[t];
        GcSpace *osp = (o->owner >= 0 && o->owner < g_space_count)
                     ? g_spaces[o->owner] : sp;
        if (!osp) osp = sp;
        if (o->gc_flags & FRAY_GC_LINKED) {
            if (o->generation == 0) gen_unlink(&osp->young_head, &osp->young_count, o);
            else                    gen_unlink(&osp->old_head,  &osp->old_count,  o);
        }
        sp->collected_total++;
        fray_weak_clear_for(o);
        fray_threads_pending_push(o);
    }
    sp->work_len = free_start;
}

/* ── The collector core (requires the space's lock held) ── */

static void collect_from_candidates(GcSpace *sp) {
    sp->scratch_floor = sp->scratch_len;  /* new trial epoch */
    sp->work_len = 0;

    /* Phase 1 — trial delete over this space's candidates. */
    for (size_t r = 0; r < sp->root_buf_len; r++) {
        FrayObj *root = sp->root_buf[r];
        if (!root || !(root->gc_flags & FRAY_GC_LINKED)) continue;
        if (obj_color_of(root) != FRAY_GC_WHITE) continue;
        obj_set_color(root, FRAY_GC_GRAY);
        work_push(sp, root);
    }
    for (size_t t = 0; t < sp->work_len; t++) {
        FrayObj *o = sp->work[t];
        size_t n;
        FrayValue *e = edges_of_pub(o, &n);
        for (size_t i = 0; i < n; i++) {
            FrayObj *child = e[i];
            if (!child || !container_p(child)) continue;
            scratch_add(sp, child, -1);
            if (obj_color_of(child) == FRAY_GC_WHITE &&
                (child->gc_flags & FRAY_GC_LINKED)) {
                obj_set_color(child, FRAY_GC_GRAY);
                work_push(sp, child);
            }
        }
    }

    /* Phase 2 — scan. */
    sp->scan_mark = sp->work_len;
    for (size_t t = 0; t < sp->scan_mark; t++) {
        FrayObj *o = sp->work[t];
        if (obj_color_of(o) != FRAY_GC_GRAY) continue;
        int64_t live = atomic_load_explicit(&o->refcount, memory_order_relaxed)
                     + scratch_get(sp, o);
        if (live > 0) mark_black(sp, o);
        else          obj_set_color(o, FRAY_GC_DEAD);
    }

    /* Phase 3 — recolor + promote survivors BEFORE the kill. This covers the
     * restore closure mark_black pushed past scan_mark as well: leaving those
     * BLACK would exempt them from phase 1 in every later collection (they
     * would never be traversed again), so garbage behind one would be
     * immortal. */
    for (size_t t = 0; t < sp->work_len; t++) {
        FrayObj *o = sp->work[t];
        if (obj_color_of(o) == FRAY_GC_BLACK) {
            obj_set_color(o, FRAY_GC_WHITE);
            promote(sp, o);
        }
    }
    kill_phase(sp);
    sp->work_len = 0;

    sp->scratch_len = sp->scratch_floor;
    root_buf_clear(sp);      /* the candidates are consumed by this collection */
}

/* Collect one space. Caller holds the world + shard + g_spaces locks; we
 * take just this space's lock for the enumeration + trial. */
static void collect_space(GcSpace *sp, bool major) {
    pthread_mutex_lock(&sp->lock);
    if (major) {
        root_buf_clear(sp);
        if (!sp->root_buf)
            sp->root_buf = (FrayObj **)malloc(ROOT_BUF_CAP * sizeof(FrayObj *));
        if (sp->root_buf) {
            /* Major collection: every container in this space is a candidate. */
            for (FrayObj *o = sp->young_head; o && sp->root_buf_len < ROOT_BUF_CAP; o = o->gc_next)
                if (container_p(o)) {
                    __atomic_fetch_or(&o->gc_flags, FRAY_GC_CANDIDATE, __ATOMIC_RELAXED);
                    sp->root_buf[sp->root_buf_len++] = o;
                }
            for (FrayObj *o = sp->old_head; o && sp->root_buf_len < ROOT_BUF_CAP; o = o->gc_next)
                if (container_p(o)) {
                    __atomic_fetch_or(&o->gc_flags, FRAY_GC_CANDIDATE, __ATOMIC_RELAXED);
                    sp->root_buf[sp->root_buf_len++] = o;
                }
        }
        sp->major_gc_runs++;
    } else {
        sp->young_gc_runs++;
    }
    collect_from_candidates(sp);
    pthread_mutex_unlock(&sp->lock);
}

/* Bulk shard-lock plumbing lives in objects.c (owner of the striped table). */
void fray_obj_locks_acquire_all(void);
void fray_obj_locks_release_all(void);

void fray_gc_collect(bool major) {
    registry_init();
    fray_world_lock();

    /* Exclude everything the collector walks or mutates. Order:
     * shards (0..N-1) -> g_spaces -> each space. */
    fray_obj_locks_acquire_all();
    pthread_mutex_lock(&g_spaces_lock);

    for (int s = 0; s < g_space_count; s++)
        if (g_spaces[s] && !g_spaces[s]->parked)
            collect_space(g_spaces[s], major);

    /* Final drain with every space lock held. */
    for (int s = 0; s < g_space_count; s++)
        if (g_spaces[s]) pthread_mutex_lock(&g_spaces[s]->lock);
    pending_drain_locked();
    for (int s = g_space_count - 1; s >= 0; s--)
        if (g_spaces[s]) pthread_mutex_unlock(&g_spaces[s]->lock);

    pthread_mutex_unlock(&g_spaces_lock);
    fray_obj_locks_release_all();

    fray_world_unlock();
}

void fray_gc_maybe_collect(void) {
    GcSpace *sp = current_space();
    if (!sp) return;
    if (sp->root_buf_len > 0 && (sp->alloc_count % FRAY_GC_YOUNG_INTERVAL) == 0) {
        fray_gc_collect(false);
        return;
    }
    if (sp->alloc_count > 0 && (sp->alloc_count % FRAY_GC_MAJOR_INTERVAL) == 0)
        fray_gc_collect(true);
}

/* Strong count hit zero on a heap container. Called from fray_release —
 * possibly mid-cascade under another space's lock, hence recursive space
 * mutexes and one-at-a-time index-ordered acquisition. */
void fray_gc_release_tracked(FrayValue v) {
    registry_init();
    /* Remove EVERY entry from every space's candidate buffer (one lock at a
     * time): this object is about to be freed, and an entry left behind is a
     * dangling root the collector would later walk as if it were alive —
     * subtracting counts for whatever memory reused those bytes and killing
     * objects live code still references. */
    for (int s = 0; s < g_space_count; s++) {
        GcSpace *sp = g_spaces[s];
        if (!sp || !sp->root_buf || sp->root_buf_len == 0) continue;
        pthread_mutex_lock(&sp->lock);
        size_t w = 0;
        for (size_t r = 0; r < sp->root_buf_len; r++) {
            if (sp->root_buf[r] == v) continue;
            sp->root_buf[w++] = sp->root_buf[r];
        }
        sp->root_buf_len = w;
        pthread_mutex_unlock(&sp->lock);
    }
    __atomic_fetch_and(&v->gc_flags, (uint8_t)~FRAY_GC_CANDIDATE,
                       __ATOMIC_RELAXED);
    if (v->owner >= 0 && v->owner < g_space_count) {
        GcSpace *osp = g_spaces[v->owner];
        if (osp) {
            pthread_mutex_lock(&osp->lock);
            if (v->gc_flags & FRAY_GC_LINKED) {
                if (v->generation == 0)
                    gen_unlink(&osp->young_head, &osp->young_count, v);
                else
                    gen_unlink(&osp->old_head,  &osp->old_count,  v);
            }
            pthread_mutex_unlock(&osp->lock);
        }
    }
    fray_free_object(v);
}

/* ── Stats ── */

static char g_stats_buf[512];

const char *fray_gc_stats(void) {
    registry_init();
    size_t young = 0, old = 0, allocs = 0, ygc = 0, mgc = 0, collected = 0;
    pthread_mutex_lock(&g_spaces_lock);
    for (int s = 0; s < g_space_count; s++) {
        GcSpace *sp = g_spaces[s];
        if (!sp) continue;
        young += sp->young_count; old += sp->old_count;
        allocs += sp->alloc_count;
        ygc += sp->young_gc_runs; mgc += sp->major_gc_runs;
        collected += sp->collected_total;
    }
    int spaces = g_space_count, active = g_active_threads;
    pthread_mutex_unlock(&g_spaces_lock);
    snprintf(g_stats_buf, sizeof(g_stats_buf),
             "spaces: %d (active %d) | heap: %zu (young %zu, old %zu) | "
             "young GCs: %zu | major GCs: %zu | collected: %zu | allocs: %zu | "
             "pending: %zu",
             spaces, active, young + old, young, old, ygc, mgc,
             collected, allocs, g_pending_len);
    return g_stats_buf;
}

size_t fray_gc_heap_size(void) {
    registry_init();
    size_t total = 0;
    pthread_mutex_lock(&g_spaces_lock);
    for (int s = 0; s < g_space_count; s++) {
        GcSpace *sp = g_spaces[s];
        if (sp) total += sp->young_count + sp->old_count;
    }
    pthread_mutex_unlock(&g_spaces_lock);
    return total;
}
