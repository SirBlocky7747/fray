/**
 * fray runtime — threads (Phase 6).
 *
 * GIL-free model:
 *  - Each spawned thread runs a fray function object on its own C thread
 *    with a fresh GC space, root stack and exception state.
 *  - The function object is retained by the runtime for the thread's
 *    lifetime, then released at exit.
 *  - Collection is stop-the-world: threads pause at safepoints (allocation
 *    is the natural one; shared-container release polls too).
 *  - Shared-object mutation is guarded by the striped locks in objects.c;
 *    refcounts stay lock-free atomics throughout.
 *  - join() blocks on the C thread and reclaims the registry slot; the
 *    exited thread's GC space is emptied and parked for reuse.
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

/* Registry slot-grab, implemented here against cycles.c's static table via
 * the exported helper below (cycles.c owns g_spaces/g_space_count). */
extern void fray_gc_space_slot_grab(GcSpace *space);

/* ── Thread registry ── */

#define MAX_THREADS 256

typedef struct FrayThread {
    pthread_t      handle;
    int64_t        id;          /* user-visible id (>0)                    */
    bool           used;
    bool           joined;
    FrayValue      fn;          /* retained function object                */
} FrayThread;

static FrayThread g_threads[MAX_THREADS];
static int64_t    g_next_id = 1;     /* user ids start at 1 (0 = invalid) */

static pthread_mutex_t g_threads_lock;
static bool  g_threads_lock_ready = false;

static void threads_lock_init(void) {
    if (g_threads_lock_ready) return;
    pthread_mutex_init(&g_threads_lock, NULL);
    g_threads_lock_ready = true;
}

int fray_thread_count(void) {
    threads_lock_init();
    pthread_mutex_lock(&g_threads_lock);
    int n = 0;
    for (int i = 0; i < MAX_THREADS; i++)
        if (g_threads[i].used && !g_threads[i].joined) n++;
    pthread_mutex_unlock(&g_threads_lock);
    return n;
}

/* ── Trampoline ── */

typedef struct SpawnArgs {
    FrayValue fn;
    GcSpace  *space;    /* pre-attached space for this thread            */
} SpawnArgs;

static void *thread_trampoline(void *argp) {
    SpawnArgs *args = (SpawnArgs *)argp;
    GcSpace *space = args->space;
    FrayValue fn = args->fn;
    free(args);

    /* Bind this thread's identity: TLS id + GC space + exception state. */
    tls_space_bind(space);
    fray_exc_state_bind();

    void (*thunk)(FrayValue) = (void (*)(FrayValue))fn->as.func.code;
    thunk(fn);
    /* Drain any collection this thread's death may have made possible. */
    fray_gc_safepoint();

    /* Detach the space from TLS before retiring it. */
    fray_gc_current_space_set(NULL);
    fray_gc_space_retire(space);
    fray_tls_thread_id = -1;
    fray_release(fn);
    /* Hand back this thread's scratch (leaf pool, root stack, jmp_buf).
     * Must come last: retiring the space and releasing the fn can pool
     * the last few boxes this thread owns. */
    fray_thread_locals_free();
    return NULL;
}

/* ── Spawn / join ── */

int64_t fray_thread_spawn(FrayValue fn) {
    if (!fn || fn->tag != TAG_FUNCTION) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: spawn expects a function");
        return -1;
    }
    threads_lock_init();
    fray_world_lock();

    pthread_mutex_lock(&g_threads_lock);
    int slot = -1;
    for (int i = 0; i < MAX_THREADS; i++) {
        if (!g_threads[i].used || g_threads[i].joined) { slot = i; break; }
    }
    pthread_mutex_unlock(&g_threads_lock);

    if (slot < 0) {
        fray_world_unlock();
        fray_throw(FRAY_EXC_RUNTIME, "RuntimeError: too many live threads");
        return -1;
    }

    /* The spawned thread needs its own GC space registered BEFORE it runs.
     * We hold the world lock, so the slot grab is atomic with the start. */
    GcSpace *space = fray_gc_space_new();
    fray_gc_space_slot_grab(space);
    fray_gc_active_threads_add(1);
    fray_retain(fn);

    SpawnArgs *args = (SpawnArgs *)malloc(sizeof(SpawnArgs));
    if (!args) {
        fray_release(fn);
        fray_gc_space_retire(space);
        fray_world_unlock();
        fray_throw(FRAY_EXC_RUNTIME, "RuntimeError: spawn oom");
        return -1;
    }
    args->fn = fn;
    args->space = space;

    /* Reserve the slot with a sentinel id so a fast joiner can't race a
     * not-yet-registered handle. */
    int64_t id;
    pthread_mutex_lock(&g_threads_lock);
    id = g_next_id++;
    g_threads[slot].used = true;
    g_threads[slot].joined = false;
    g_threads[slot].id = id;
    g_threads[slot].fn = fn;
    pthread_mutex_unlock(&g_threads_lock);

    if (pthread_create(&g_threads[slot].handle, NULL, thread_trampoline, args) != 0) {
        pthread_mutex_lock(&g_threads_lock);
        g_threads[slot].used = false;
        g_threads[slot].fn = NULL;
        pthread_mutex_unlock(&g_threads_lock);
        free(args);
        fray_release(fn);
        fray_gc_space_retire(space);
        fray_world_unlock();
        fray_throw(FRAY_EXC_RUNTIME, "RuntimeError: thread creation failed");
        return -1;
    }

    fray_world_unlock();
    return id;
}

void fray_thread_join(int64_t id) {
    threads_lock_init();
    if (id <= 0) return;

    pthread_t handle;
    bool have = false;
    pthread_mutex_lock(&g_threads_lock);
    for (int i = 0; i < MAX_THREADS; i++) {
        if (g_threads[i].used && !g_threads[i].joined && g_threads[i].id == id) {
            handle = g_threads[i].handle;
            have = true;
            break;
        }
    }
    pthread_mutex_unlock(&g_threads_lock);
    if (!have) return;

    /* Joining from OUTSIDE the world lock; the dying thread may need to
     * take it during GC retire. */
    pthread_join(handle, NULL);

    pthread_mutex_lock(&g_threads_lock);
    for (int i = 0; i < MAX_THREADS; i++) {
        if (g_threads[i].used && !g_threads[i].joined && g_threads[i].id == id) {
            g_threads[i].joined = true;
            g_threads[i].fn = NULL;
            break;
        }
    }
    pthread_mutex_unlock(&g_threads_lock);
}

void fray_thread_join_all(void) {
    threads_lock_init();
    for (;;) {
        int64_t id = 0;
        pthread_mutex_lock(&g_threads_lock);
        for (int i = 0; i < MAX_THREADS; i++) {
            if (g_threads[i].used && !g_threads[i].joined) {
                id = g_threads[i].id;
                break;
            }
        }
        pthread_mutex_unlock(&g_threads_lock);
        if (id == 0) return;
        fray_thread_join(id);
    }
}
