/**
 * fray runtime — atomics (Phase 6).
 *
 * An atomic is a boxed object (int/float/bool payload) whose reads and
 * writes are serialized by the object's striped lock shard. Objects are
 * ordinary GC-managed FrayObjs — allocation, refcounting and collection
 * are unchanged — so an atomic is freely shareable across threads.
 *
 * Semantics match the evaluator oracle:
 *  - atomic_new(v)  -> boxed value marked as an atomic
 *  - atomic_get(a)  -> fresh (owned) copy of the payload
 *  - atomic_set(a,v)-> replaces payload; returns none
 *  - atomic_add(a,v)-> numeric add of v into the payload; returns the NEW
 *    value as a fresh object (owned)
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>

/* Atomic payload storage: like a normal value but the runtime knows to
 * lock around accesses. We reuse TAG_INT/FLOAT/BOOL with a dedicated
 * gc_flags bit so the printer/evaluator see plain ints. */
#define FRAY_GC_ATOMIC 0x20u

FrayValue fray_atomic_new(FrayValue initial) {
    FrayValue box;
    if (fray_is_int(initial)) {
        box = fray_int(initial->as.i);
    } else if (fray_is_float(initial)) {
        box = fray_float(initial->as.f);
    } else if (fray_is_bool_val(initial)) {
        box = fray_bool(initial->as.b);
    } else {
        fray_throw(FRAY_EXC_TYPE,
                   "TypeError: atomic expects int, float or bool");
        return fray_none();
    }
    box->gc_flags |= FRAY_GC_ATOMIC;
    return box;
}

static inline bool atomic_p(FrayValue v) {
    return v && (v->gc_flags & FRAY_GC_ATOMIC);
}

static bool check_atomic(FrayValue v, const char *who) {
    if (!atomic_p(v)) {
        (void)who;
        fray_throw(FRAY_EXC_TYPE,
                   "TypeError: expected an atomic object");
        return false;
    }
    return true;
}

FrayValue fray_atomic_get(FrayValue box) {
    if (!check_atomic(box, "get")) return fray_none();
    fray_obj_lock(box);
    FrayValue out;
    switch (box->tag) {
        case TAG_INT:   out = fray_int(box->as.i);   break;
        case TAG_FLOAT: out = fray_float(box->as.f); break;
        case TAG_BOOL:  out = fray_bool(box->as.b);  break;
        default:        out = fray_none();           break;
    }
    fray_obj_unlock(box);
    return out;
}

void fray_atomic_set(FrayValue box, FrayValue v) {
    if (!check_atomic(box, "set")) return;
    if (v->tag != box->tag) {
        /* Allow int -> float widening like the evaluator. */
        if (box->tag == TAG_FLOAT && v->tag == TAG_INT) {
            fray_obj_lock(box);
            box->as.f = (double)v->as.i;
            fray_obj_unlock(box);
            return;
        }
        fray_throw(FRAY_EXC_TYPE,
                   "TypeError: atomic set type mismatch");
        return;
    }
    fray_obj_lock(box);
    box->as.i = v->as.i; /* payload union: i/f/b share the first 8 bytes */
    fray_obj_unlock(box);
}

FrayValue fray_atomic_add(FrayValue box, FrayValue delta) {
    if (!check_atomic(box, "add")) return fray_none();
    fray_obj_lock(box);
    FrayValue out;
    if (box->tag == TAG_INT && delta->tag == TAG_INT) {
        box->as.i += delta->as.i;
        out = fray_int(box->as.i);
    } else if (box->tag == TAG_FLOAT ||
               (box->tag == TAG_INT && delta->tag == TAG_FLOAT)) {
        double cur = (box->tag == TAG_FLOAT) ? box->as.f : (double)box->as.i;
        double d = (delta->tag == TAG_FLOAT) ? delta->as.f
                 : (delta->tag == TAG_INT) ? (double)delta->as.i : 0.0;
        box->tag = TAG_FLOAT;
        box->type = &fray_type_float;
        box->as.f = cur + d;
        out = fray_float(box->as.f);
    } else {
        fray_obj_unlock(box);
        fray_throw(FRAY_EXC_TYPE,
                   "TypeError: atomic add expects numeric operands");
        return fray_none();
    }
    fray_obj_unlock(box);
    return out;
}
