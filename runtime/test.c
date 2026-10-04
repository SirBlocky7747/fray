/**
 * fray runtime test suite — Phase 5 memory management.
 *
 * Build & run:
 *   gcc -O1 -g -fsanitize=address -o test test.c objects.c cycles.c ops.c printing.c
 *   ./test
 *
 * Every test asserts observable refcount/GC behavior; running under ASan
 * (or valgrind) verifies there are no leaks or use-after-frees on any path.
 */

#include "runtime.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int tests_run = 0;

static void ok(const char *name) {
    /* After every test, the runtime must be clean: tests release every
     * reference they create. Auto-collections triggered mid-test count as
     * cleanup too, so run one explicitly. */
    fray_gc_collect(false);
    size_t heap = fray_gc_heap_size();
    if (heap != 0) {
        printf("FAIL  %s: %zu object(s) leaked on test exit\n", name, heap);
        exit(1);
    }
    tests_run++;
    printf("PASS  %s\n", name);
}

static void test_refcount_basic(void) {
    FrayValue a = fray_int(42);
    assert(fray_refcount(a) == 1);
    fray_retain(a);
    assert(fray_refcount(a) == 2);
    fray_release(a);
    assert(fray_refcount(a) == 1);
    fray_release(a); /* freed here */

    /* Strings own their buffer. */
    FrayValue s = fray_string_copy("hello");
    assert(fray_refcount(s) == 1);
    FrayValue n = fray_len(s);            /* returns an owned value */
    assert(n->as.i == 5);
    fray_release(n);
    fray_release(s);

    ok("refcount_basic");
}

static void test_container_ownership(void) {
    FrayValue l = fray_list();
    FrayValue x = fray_int(7);
    fray_list_append(l, x);          /* list holds a strong ref */
    assert(fray_refcount(x) == 2);
    fray_release(x);                 /* only the list holds it now */
    assert(fray_refcount(x) == 1);

    /* Indexing returns a retained reference. */
    FrayValue y = fray_list_index(l, 0);
    assert(fray_refcount(y) == 2);
    fray_release(y);

    /* Overwriting releases the old element. */
    FrayValue z = fray_int(9);
    fray_list_setindex(l, 0, z);
    assert(fray_refcount(z) == 2);   /* local + list */
    fray_release(z);
    fray_release(l);                 /* frees list + element (immediate) */

    assert(fray_gc_heap_size() == 0);
    ok("container_ownership");
}

static void test_acyclic_immediate_free(void) {
    /* A deep acyclic structure dies the moment the root is released:
     * no collector run should be needed. */
    FrayValue root = fray_list();
    for (int i = 0; i < 100; i++) {
        FrayValue inner = fray_list();
        FrayValue elem = fray_int(i);
        fray_list_append(inner, elem);   /* append retains */
        fray_release(elem);
        fray_list_append(root, inner);
        fray_release(inner); /* hand ownership to root */
    }
    size_t before = fray_gc_heap_size();
    assert(before == 101);
    fray_release(root);
    assert(fray_gc_heap_size() == 0);
    ok("acyclic_immediate_free");
}

static void test_two_object_cycle(void) {
    FrayValue a = fray_list();
    FrayValue b = fray_list();
    fray_list_append(a, b);          /* a -> b */
    fray_list_append(b, a);          /* b -> a  (cycle) */
    assert(fray_refcount(a) == 2);
    assert(fray_refcount(b) == 2);

    /* Drop the external references. */
    fray_release(a);
    fray_release(b);
    /* Both still alive, trapped in the cycle. */
    assert(fray_gc_heap_size() == 2);

    /* The collector must reclaim them. */
    fray_gc_collect(false);
    assert(fray_gc_heap_size() == 0);
    ok("two_object_cycle");
}

static void test_self_cycle(void) {
    FrayValue a = fray_list();
    fray_list_append(a, a);          /* a -> a */
    assert(fray_refcount(a) == 2);
    fray_release(a);
    assert(fray_gc_heap_size() == 1);
    fray_gc_collect(false);
    assert(fray_gc_heap_size() == 0);
    ok("self_cycle");
}

static void test_dead_candidate_root_is_dropped(void) {
    /* Every release that leaves a positive count records the object in the
     * candidate-root buffer. All of those entries must leave with the object:
     * an entry left behind is a dangling root, and the collector walks roots
     * before it can know anything about them — reading whatever memory has
     * been recycled into a live root it then kills. */
    FrayValue a = fray_list();
    FrayValue h1 = fray_list();
    FrayValue h2 = fray_list();
    fray_list_append(h1, a);          /* a <- h1 */
    fray_list_append(h2, a);          /* a <- h2 */
    fray_retain(a);                   /* second external reference */
    assert(fray_refcount(a) == 4);
    fray_release(a);                  /* rc 4 -> 3: candidate entry */
    fray_release(a);                  /* rc 3 -> 2: the same object again */
    assert(fray_refcount(a) == 2);
    fray_release(h1);                 /* dies, dropping its edge: entry again */
    assert(fray_refcount(a) == 1);
    fray_release(h2);                 /* a's last reference dies with it    */
    assert(fray_gc_heap_size() == 0);
    /* A collection here used to follow freed memory as a candidate root. */
    fray_gc_collect(false);
    assert(fray_gc_pending_count() == 0);
    ok("dead_candidate_root_is_dropped");
}

static void test_long_ring(void) {
    /* 1000 lists in a ring: stresses the kill phase. */
    size_t n = 1000;
    FrayValue *ring = (FrayValue *)malloc(n * sizeof(FrayValue));
    for (size_t i = 0; i < n; i++) ring[i] = fray_list();
    for (size_t i = 0; i < n; i++)
        fray_list_append(ring[i], ring[(i + 1) % n]);
    for (size_t i = 0; i < n; i++) fray_release(ring[i]);
    assert(fray_gc_heap_size() == n);
    fray_gc_collect(false);
    assert(fray_gc_heap_size() == 0);
    /* Every condemned container must reach the deferred-free queue and leave
     * it again: a dropped entry is memory nobody owns any more, and freeing
     * it later through the normal path drops its elements a second time. */
    assert(fray_gc_pending_count() == 0);
    free(ring);
    ok("long_ring");
}

static void test_pending_queue_keeps_every_condemned_container(void) {
    /* More containers condemned in ONE collection than any fixed-size queue
     * could hold. Every condemned container must reach the deferred-free
     * queue: one that is dropped is detached from its generation list and
     * claimed by nobody, so it leaks — or, worse, is later torn down as a
     * live object and releases its elements a second time.
     *
     * The pairs are isolated from each other on purpose. A ring would let one
     * stale pointer keep the whole dropped set reachable and hide the leak
     * from LeakSanitizer; separate 2-cycles cannot do that. */
    size_t pairs = 400;
    FrayValue *a = (FrayValue *)malloc(pairs * sizeof(FrayValue));
    FrayValue *b = (FrayValue *)malloc(pairs * sizeof(FrayValue));
    for (size_t i = 0; i < pairs; i++) {
        a[i] = fray_list();
        b[i] = fray_list();
    }
    for (size_t i = 0; i < pairs; i++) {   /* a <-> b, nothing else */
        fray_list_append(a[i], b[i]);
        fray_list_append(b[i], a[i]);
    }
    for (size_t i = 0; i < pairs; i++) {   /* external references go away */
        fray_release(a[i]);
        fray_release(b[i]);
    }
    assert(fray_gc_heap_size() == 2 * pairs);
    fray_gc_collect(false);                /* condemns all of them at once */
    assert(fray_gc_heap_size() == 0);
    assert(fray_gc_pending_count() == 0);  /* queue drained, none forgotten */
    free(a);
    free(b);
    ok("pending_queue_keeps_every_condemned_container");
}

static void test_shared_cycle_member_survives(void) {
    /* C -> A, A -> B, B -> A. Dropping A and B leaves the cycle pinned
     * by C: the collector must free nothing. Dropping C frees all. */
    FrayValue a = fray_list();
    FrayValue b = fray_list();
    FrayValue c = fray_list();
    fray_list_append(a, b);
    fray_list_append(b, a);
    fray_list_append(c, a);
    fray_release(a);
    fray_release(b);
    assert(fray_gc_heap_size() == 3);
    fray_gc_collect(false);
    assert(fray_gc_heap_size() == 3); /* C still roots A */
    fray_release(c);                  /* C dies immediately */
    assert(fray_gc_heap_size() == 2); /* A<->B now unreachable garbage */
    fray_gc_collect(false);           /* ...reclaimed at the next collection */
    assert(fray_gc_heap_size() == 0);
    ok("shared_cycle_member_survives");
}

static void test_cycle_with_shared_string(void) {
    /* Cycle members also share a non-container child. */
    FrayValue a = fray_list();
    FrayValue b = fray_list();
    FrayValue s = fray_string_copy("shared");
    fray_list_append(a, b);
    fray_list_append(b, a);
    fray_list_append(a, s);
    fray_list_append(b, s);
    assert(fray_refcount(s) == 3);   /* a + b + local */
    fray_release(s);                  /* a + b hold it */
    fray_release(a);
    fray_release(b);
    assert(fray_gc_heap_size() == 2);
    fray_gc_collect(false);           /* string refcount hits 0 in kill phase */
    assert(fray_gc_heap_size() == 0);
    ok("cycle_with_shared_string");
}

static void test_weak_refs(void) {
    FrayValue s = fray_string_copy("weak target");
    int64_t h = fray_weak_new(s);
    FrayValue got = fray_weak_lock(h);
    assert(got == s);                 /* lock retains */
    assert(fray_refcount(s) == 2);
    fray_release(got);

    fray_release(s);                  /* target dies */
    assert(fray_weak_lock(h) == NULL); /* and no crash */
    fray_weak_del(h);
    ok("weak_refs");
}

static void test_root_stack(void) {
    size_t len = 0;
    FrayValue a = fray_list();
    fray_root_push(a);
    FrayValue *roots = fray_root_stack(&len);
    assert(len == 1 && roots[0] == a);
    FrayValue b = fray_int(1);
    fray_root_push(b);
    roots = fray_root_stack(&len);
    assert(len == 2 && roots[0] == b && roots[1] == a);
    fray_root_pop();
    fray_root_pop();
    roots = fray_root_stack(&len);
    assert(len == 0);                 /* and the stack is empty */
    (void)roots;
    fray_release(a);                  /* push/pop are ownership-neutral */
    fray_release(b);
    ok("root_stack");
}

static void test_gc_stats_smoke(void) {
    const char *stats = fray_gc_stats();
    assert(stats && strstr(stats, "heap:"));
    ok("gc_stats_smoke");
}

static void test_builders_and_arith_smoke(void) {
    FrayValue a = fray_int(20);
    FrayValue b = fray_int(22);
    FrayValue sum = fray_add(a, b);
    assert(sum->as.i == 42);
    fray_release(sum);
    fray_release(a);
    fray_release(b);

    FrayValue s1 = fray_string_copy("foo");
    FrayValue s2 = fray_string_copy("bar");
    FrayValue cat = fray_add(s1, s2);
    assert(cat->as.str.len == 6);
    assert(memcmp(cat->as.str.data, "foobar", 6) == 0);
    fray_release(cat);
    fray_release(s1);
    fray_release(s2);

    FrayValue one = fray_int(1);
    FrayValue two = fray_int(2);
    FrayValue three = fray_int(3);
    FrayValue l = fray_list();
    fray_list_append(l, one);
    fray_list_append(l, two);
    fray_list_append(l, three);
    FrayValue total = fray_sum(l);
    assert(total->as.i == 6);
    FrayValue ftotal = fray_sum_fast(l);
    assert(ftotal->as.i == 6);
    fray_release(ftotal);
    fray_release(total);
    fray_release(l);
    fray_release(one);
    fray_release(two);
    fray_release(three);
    ok("builders_and_arith_smoke");
}

static void test_range_and_set(void) {
    FrayValue five = fray_int(5);
    FrayValue r = fray_range(five);
    fray_release(five);
    assert(r->as.list.len == 5);
    assert(r->as.list.elems[4]->as.i == 4);
    fray_release(r);

    FrayValue s = fray_set();
    FrayValue one = fray_int(1);
    FrayValue two = fray_int(2);
    FrayValue dup = fray_int(1);
    fray_set_append(s, one);
    fray_release(one);               /* set owns it now */
    fray_set_append(s, dup);         /* deduplicated, like the oracle */
    fray_release(dup);               /* freed: nobody kept it */
    fray_set_append(s, two);
    fray_release(two);
    assert(s->as.set.len == 2);
    FrayValue popped = fray_set_depend(s);
    fray_release(popped);
    assert(s->as.set.len == 1);
    fray_release(s);
    ok("range_and_set");
}

/* ── Phase 6: thread stress ──
 * The shared list and atomic are hammered by 8 fray threads (spawned via
 * the runtime's own thread registry, so each gets its own root stack and
 * GC space). Success = no crash/race and exact totals. */

#define STRESS_THREADS 8
#define STRESS_APPENDS 5000

static FrayValue g_shared_list = NULL;   /* owned by main, shared         */
static FrayValue g_shared_atomic = NULL; /* atomic int box                */

static size_t g_counts[STRESS_THREADS];

static void stress_body(size_t idx) {
    size_t n = 0;
    for (int i = 0; i < STRESS_APPENDS; i++) {
        FrayValue v = fray_int(i);
        fray_list_append(g_shared_list, v);
        fray_release(v);
        n++;
        if ((i & 63) == 0) {
            FrayValue one = fray_int(1);
            FrayValue added = fray_atomic_add(g_shared_atomic, one);
            fray_release(added);         /* atomic ops return the new value */
            fray_release(one);
        }
        if ((i & 255) == 0) fray_gc_safepoint();
    }
    g_counts[idx] = n;
}

static void worker_thunk_0(FrayValue self) { (void)self; stress_body(0); }
static void worker_thunk_1(FrayValue self) { (void)self; stress_body(1); }
static void worker_thunk_2(FrayValue self) { (void)self; stress_body(2); }
static void worker_thunk_3(FrayValue self) { (void)self; stress_body(3); }
static void worker_thunk_4(FrayValue self) { (void)self; stress_body(4); }
static void worker_thunk_5(FrayValue self) { (void)self; stress_body(5); }
static void worker_thunk_6(FrayValue self) { (void)self; stress_body(6); }
static void worker_thunk_7(FrayValue self) { (void)self; stress_body(7); }

static void (*const g_thunks[STRESS_THREADS])(FrayValue) = {
    worker_thunk_0, worker_thunk_1, worker_thunk_2, worker_thunk_3,
    worker_thunk_4, worker_thunk_5, worker_thunk_6, worker_thunk_7,
};

/* An exiting thread parks its GC space; the next thread to attach must get
 * that space back rather than a new one. The registry used to overwrite the
 * slot instead, dropping the last pointer to the retired space and leaking it
 * — 192 bytes per reuse, which is how this surfaced as an ASan failure in
 * `make -C runtime asan` (a racing test never ran the reuse path locally, so
 * it looked clean). */
static void reuse_thunk(FrayValue self) { (void)self; }

static void test_thread_space_reuse(void) {
    int before = fray_gc_space_count();
    for (int batch = 0; batch < 3; batch++) {
        int64_t ids[STRESS_THREADS];
        for (int i = 0; i < STRESS_THREADS; i++) {
            FrayValue fn = fray_function("reuse_worker", (void *)reuse_thunk, 0);
            ids[i] = fray_thread_spawn(fn);
            fray_release(fn);
            assert(ids[i] > 0);
        }
        for (int i = 0; i < STRESS_THREADS; i++) fray_thread_join(ids[i]);
    }
    int grown = fray_gc_space_count() - before;
    printf("  [reuse] 3 batches of %d threads grew the table by %d slot(s)\n",
           STRESS_THREADS, grown);
    /* Three batches of the same width cannot need more slots than one batch:
     * the parked spaces have to come back. */
    assert(grown <= STRESS_THREADS);
    ok("thread_space_reuse");
}

static void test_thread_stress(void) {
    printf("  [stress] setup\n");
    g_shared_list = fray_list();
    FrayValue zero = fray_int(0);
    g_shared_atomic = fray_atomic_new(zero);
    fray_release(zero);

    int64_t ids[STRESS_THREADS];
    for (int i = 0; i < STRESS_THREADS; i++) {
        FrayValue fn = fray_function("stress_worker", (void *)g_thunks[i], 0);
        ids[i] = fray_thread_spawn(fn);
        fray_release(fn);
        assert(ids[i] > 0);
        printf("  [stress] spawned id %lld\n", (long long)ids[i]);
    }
    for (int i = 0; i < STRESS_THREADS; i++) {
        printf("  [stress] joining id %lld\n", (long long)ids[i]);
        fray_thread_join(ids[i]);
    }
    printf("  [stress] joined\n");

    size_t total = 0;
    for (int i = 0; i < STRESS_THREADS; i++) total += g_counts[i];
    assert(total == (size_t)STRESS_THREADS * STRESS_APPENDS);
    assert(g_shared_list->as.list.len == total);

    FrayValue got = fray_atomic_get(g_shared_atomic);
    int64_t expected = 0;
    for (int i = 0; i < STRESS_APPENDS; i += 64) expected++;  /* adds per thread */
    assert(got->as.i == (int64_t)STRESS_THREADS * expected);
    fray_release(got);

    /* Drain the list: every element releases cleanly. */
    while (g_shared_list->as.list.len > 0) {
        FrayValue e = fray_list_depend(g_shared_list);
        fray_release(e);
    }
    fray_release(g_shared_list);
    fray_release(g_shared_atomic);
    g_shared_list = NULL;
    g_shared_atomic = NULL;

    /* Force collections and make sure everything reconciles. */
    fray_gc_collect(false);
    fray_gc_collect(true);
    ok("thread_stress");
}

/* ── Phase 7: coroutine scheduler stress ── */

#include <pthread.h>

static _Atomic int64_t g_coro_completions = 0;
static FrayValue g_coro_chan = NULL;
static _Atomic int64_t g_chan_received = 0;

#define CORO_COUNT 100000

/* Coroutine body: yield a few times, send on a shared channel every
 * 10000th coroutine, then finish. Exercises: fiber switching, timer
 * wheel (via yield), channels from coroutines, 100k live fibers. */
static void coro_body(FrayValue self) {
    (void)self;
    for (int i = 0; i < 3; i++) {
        fray_coro_yield();
    }
    static _Atomic int64_t counter = 0;
    int64_t n = atomic_fetch_add(&counter, 1);
    if ((n % 10000) == 0 && g_coro_chan) {
        FrayValue msg = fray_int(n);
        fray_channel_send(g_coro_chan, msg);
        fray_release(msg);
    }
    atomic_fetch_add(&g_coro_completions, 1);
}

/* A receiver coroutine draining the channel. */
static void coro_receiver(FrayValue self) {
    (void)self;
    int64_t got = 0;
    for (;;) {
        FrayValue v = fray_channel_recv(g_coro_chan);
        if (!v || v->tag == TAG_NONE) {   /* closed + drained              */
            if (v) fray_release(v);
            break;
        }
        fray_release(v);
        got++;
    }
    atomic_fetch_add(&g_chan_received, got);
}

/* Closer coroutine: waits, then closes the channel. */
static void coro_closer(FrayValue self) {
    (void)self;
    fray_sleep(50.0);
    if (g_coro_chan) fray_channel_close(g_coro_chan);
}

static void test_coroutine_stress(void) {
    printf("  [coro] spawning %d coroutines\n", CORO_COUNT);
    g_coro_chan = fray_channel_new(fray_none());

    /* Spawn receiver + closer first, then the swarm. */
    FrayValue rcv = fray_function("receiver", (void *)coro_receiver, 0);
    fray_coro_spawn(rcv);
    FrayValue cls = fray_function("closer", (void *)coro_closer, 0);
    fray_coro_spawn(cls);

    for (int i = 0; i < CORO_COUNT; i++) {
        if (i == CORO_COUNT / 2) printf("  [coro] halfway spawned\n");
        FrayValue fn = fray_function("coro_body", (void *)coro_body, 0);
        fray_coro_spawn(fn);
        fray_release(fn);
    }
    fray_release(rcv);
    fray_release(cls);

    printf("  [coro] live before run: %d\n", fray_coro_count());
    fray_coro_run_until_complete();
    printf("  [coro] run complete; live after: %d\n", fray_coro_count());

    assert(atomic_load(&g_coro_completions) == CORO_COUNT);
    assert(fray_coro_count() == 0);
    printf("  [coro] channel messages received: %lld\n",
           (long long)atomic_load(&g_chan_received));
    assert(atomic_load(&g_chan_received) == CORO_COUNT / 10000 +
           (CORO_COUNT % 10000 ? 1 : 0) - 1 + 1);  /* sends at n=0,10000,... */

    fray_release(g_coro_chan);
    g_coro_chan = NULL;

    fray_gc_collect(false);
    fray_gc_collect(true);
    ok("coroutine_stress");
}

/* ── Phase 7: cross-world pipeline — thread produces, coroutine consumes ── */

static _Atomic bool g_pipeline_done = false;
static FrayValue g_pipeline_chan = NULL;

static void pipeline_consumer(FrayValue self) {
    (void)self;
    int64_t sum = 0;
    for (;;) {
        FrayValue v = fray_channel_recv(g_pipeline_chan);
        if (!v || v->tag == TAG_NONE) {
            if (v) fray_release(v);
            break;
        }
        sum += v->as.i;
        fray_release(v);
    }
    assert(sum == 100 * 101 / 2);
    g_pipeline_done = true;
}

static void *pipeline_producer(void *arg) {
    (void)arg;
    for (int i = 1; i <= 100; i++) {
        FrayValue v = fray_int(i);
        fray_channel_send(g_pipeline_chan, v);
        fray_release(v);
    }
    fray_channel_close(g_pipeline_chan);
    /* A raw pthread that touches the runtime owns per-thread scratch (the
     * leaf pool, root stack, exception state) that no one else will hand
     * back for it — fray_thread_spawn does this in its trampoline. */
    fray_thread_locals_free();
    return NULL;
}

static void test_coro_thread_pipeline(void) {
    printf("  [pipeline] thread -> channel -> coroutine\n");
    g_pipeline_chan = fray_channel_new(fray_none());

    FrayValue cons = fray_function("consumer", (void *)pipeline_consumer, 0);
    fray_coro_spawn(cons);
    fray_release(cons);

    pthread_t producer;
    pthread_create(&producer, NULL, pipeline_producer, NULL);

    /* Main thread runs the loop until the consumer finishes. */
    fray_coro_run_until_complete();
    pthread_join(producer, NULL);

    assert(g_pipeline_done);
    printf("  [pipeline] OK\n");

    fray_release(g_pipeline_chan);
    g_pipeline_chan = NULL;
    fray_gc_collect(false);
    fray_gc_collect(true);
    ok("coro_thread_pipeline");
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0); /* see progress even on a crash */
    test_refcount_basic();
    test_container_ownership();
    test_acyclic_immediate_free();
    test_two_object_cycle();
    test_self_cycle();
    test_dead_candidate_root_is_dropped();
    test_long_ring();
    test_pending_queue_keeps_every_condemned_container();
    test_shared_cycle_member_survives();
    test_cycle_with_shared_string();
    test_weak_refs();
    test_root_stack();
    test_gc_stats_smoke();
    test_builders_and_arith_smoke();
    test_range_and_set();
    test_thread_stress();
    test_thread_space_reuse();
    test_coroutine_stress();
    test_coro_thread_pipeline();

    printf("\n%d tests passed. GC: %s\n", tests_run, fray_gc_stats());
    return 0;
}
