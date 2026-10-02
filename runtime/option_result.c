#include "runtime.h"
#include <stdlib.h>
#include <stdio.h>

/* Option type: Some(val) or None */

FrayValue fray_some(FrayValue val) {
    FrayValue v = (FrayValue)calloc(1, sizeof(FrayObj));
    if (!v) { fprintf(stderr, "out of memory\n"); abort(); }
    v->tag = TAG_OPTION;
    v->gc_flags = FRAY_GC_HEAP;
    atomic_init(&v->refcount, 1);
    v->type = &fray_type_option;
    v->as.opt.is_some = 1;
    v->as.opt.val = val;
    if (val) atomic_fetch_add_explicit(&val->refcount, 1, memory_order_relaxed);
    return v;
}

/* None is represented as a TAG_NONE object (the existing fray_none()) */

FrayValue fray_is_some(FrayValue v) {
    if (!v || v->tag != TAG_OPTION) return fray_bool(false);
    return fray_bool(v->as.opt.is_some == 1);
}

FrayValue fray_is_none_val(FrayValue v) {
    if (!v) return fray_bool(true);
    if (v->tag == TAG_NONE) return fray_bool(true);
    if (v->tag == TAG_OPTION) return fray_bool(v->as.opt.is_some == 0);
    return fray_bool(false);
}

FrayValue fray_unwrap(FrayValue v) {
    if (!v || v->tag == TAG_NONE) {
        fray_throw(FRAY_EXC_RUNTIME, "called unwrap() on None");
        return fray_none();
    }
    if (v->tag == TAG_OPTION) {
        if (!v->as.opt.is_some) {
            fray_throw(FRAY_EXC_RUNTIME, "called unwrap() on None");
            return fray_none();
        }
        /* Return owned reference */
        if (v->as.opt.val) atomic_fetch_add_explicit(&v->as.opt.val->refcount, 1, memory_order_relaxed);
        return v->as.opt.val;
    }
    if (v->tag == TAG_RESULT) {
        if (v->as.res.is_ok) {
            if (v->as.res.val) atomic_fetch_add_explicit(&v->as.res.val->refcount, 1, memory_order_relaxed);
            return v->as.res.val;
        }
        fray_throw(FRAY_EXC_RUNTIME, "called unwrap() on Err");
        return fray_none();
    }
    /* For other types, return as-is (unwrap is a no-op) — retain for caller ownership */
    if (v) atomic_fetch_add_explicit(&v->refcount, 1, memory_order_relaxed);
    return v;
}

void fray_option_state_release(void *state) {
    /* TAG_OPTION has no heap state — payload is inline */
    (void)state;
}

/* Result type: Ok(val) or Err(err) */

FrayValue fray_ok(FrayValue val) {
    FrayValue v = (FrayValue)calloc(1, sizeof(FrayObj));
    if (!v) { fprintf(stderr, "out of memory\n"); abort(); }
    v->tag = TAG_RESULT;
    v->gc_flags = FRAY_GC_HEAP;
    atomic_init(&v->refcount, 1);
    v->type = &fray_type_result;
    v->as.res.is_ok = 1;
    v->as.res.val = val;
    v->as.res.err = NULL;
    if (val) atomic_fetch_add_explicit(&val->refcount, 1, memory_order_relaxed);
    return v;
}

FrayValue fray_err(FrayValue err) {
    FrayValue v = (FrayValue)calloc(1, sizeof(FrayObj));
    if (!v) { fprintf(stderr, "out of memory\n"); abort(); }
    v->tag = TAG_RESULT;
    v->gc_flags = FRAY_GC_HEAP;
    atomic_init(&v->refcount, 1);
    v->type = &fray_type_result;
    v->as.res.is_ok = 0;
    v->as.res.val = NULL;
    v->as.res.err = err;
    if (err) atomic_fetch_add_explicit(&err->refcount, 1, memory_order_relaxed);
    return v;
}

FrayValue fray_is_ok(FrayValue v) {
    if (!v || v->tag != TAG_RESULT) return fray_bool(false);
    return fray_bool(v->as.res.is_ok == 1);
}

FrayValue fray_is_err(FrayValue v) {
    if (!v || v->tag != TAG_RESULT) return fray_bool(false);
    return fray_bool(v->as.res.is_ok == 0);
}

void fray_result_state_release(void *state) {
    /* TAG_RESULT has no heap state — payload is inline */
    (void)state;
}
