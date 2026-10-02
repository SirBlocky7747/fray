/**
 * fray runtime — operators, comparisons, container operations, builtins.
 *
 * Every function here follows the same memory discipline:
 *  - take no ownership of arguments (never release them),
 *  - return one owned reference,
 *  - before any allocation that could trigger a collection, root-push
 *    every value that is only reachable from a local (see cycles.c).
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

/* ── Helpers ── */

static double to_float(FrayValue v) {
    if (v->tag == TAG_FLOAT) return v->as.f;
    if (v->tag == TAG_INT) return (double)v->as.i;
    if (v->tag == TAG_BOOL) return v->as.b ? 1.0 : 0.0;
    return 0.0;
}

static int64_t to_int(FrayValue v) {
    if (v->tag == TAG_INT) return v->as.i;
    if (v->tag == TAG_FLOAT) return (int64_t)v->as.f;
    if (v->tag == TAG_BOOL) return v->as.b ? 1 : 0;
    return 0;
}

static bool is_numeric(FrayValue v) {
    return v && (v->tag == TAG_INT || v->tag == TAG_FLOAT || v->tag == TAG_BOOL);
}

/* ── Arithmetic ── */

FrayValue fray_add_fast(FrayValue a, FrayValue b) {
    /* String concatenation */
    if (a->tag == TAG_STRING && b->tag == TAG_STRING) {
        size_t len = a->as.str.len + b->as.str.len;
        char *buf = (char *)malloc(len + 1);
        if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
        memcpy(buf, a->as.str.data, a->as.str.len);
        memcpy(buf + a->as.str.len, b->as.str.data, b->as.str.len);
        buf[len] = '\0';
        FrayValue result = fray_string(buf, len);
        free(buf);
        return result;
    }
    if (a->tag == TAG_FLOAT || b->tag == TAG_FLOAT)
        return fray_float(to_float(a) + to_float(b));
    return fray_int(to_int(a) + to_int(b));
}

FrayValue fray_add(FrayValue a, FrayValue b) {
    if (!a || !b) return NULL;

    /* List concatenation */
    if (a->tag == TAG_LIST && b->tag == TAG_LIST) {
        fray_root_push(a);
        fray_root_push(b);
        size_t new_len = a->as.list.len + b->as.list.len;
        FrayValue result = fray_list();
        if (new_len) {
            result->as.list.elems = (FrayValue *)calloc(new_len, sizeof(FrayValue));
            result->as.list.len = new_len;
            result->as.list.cap = new_len;
            for (size_t i = 0; i < a->as.list.len; i++) {
                result->as.list.elems[i] = a->as.list.elems[i];
                fray_retain(result->as.list.elems[i]);
            }
            for (size_t i = 0; i < b->as.list.len; i++) {
                result->as.list.elems[a->as.list.len + i] = b->as.list.elems[i];
                /* Retain the element just stored. Retaining
                 * result->as.list.elems[i] here instead reads back into a's
                 * half: a's elements get retained twice and b's elements are
                 * stored without a reference at all, so a list concatenation
                 * whose right operand is a temporary leaves the result
                 * pointing at freed boxes (a use-after-free, and a double
                 * free once the result itself dies). */
                fray_retain(b->as.list.elems[i]);
            }
        }
        fray_root_pop();
        fray_root_pop();
        return result;
    }

    if (is_numeric(a) && is_numeric(b))
        return fray_add_fast(a, b);

    /* String concatenation */
    if (a->tag == TAG_STRING && b->tag == TAG_STRING)
        return fray_add_fast(a, b);

    fray_throw(FRAY_EXC_TYPE, "TypeError: unsupported operand type(s) for +");
    return NULL;
}

FrayValue fray_sub(FrayValue a, FrayValue b) {
    if (!a || !b || !is_numeric(a) || !is_numeric(b)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: unsupported operand type(s) for -");
        return NULL;
    }
    if (a->tag == TAG_FLOAT || b->tag == TAG_FLOAT)
        return fray_float(to_float(a) - to_float(b));
    return fray_int(to_int(a) - to_int(b));
}

FrayValue fray_mul(FrayValue a, FrayValue b) {
    if (!a || !b) return NULL;

    /* String repetition */
    if (a->tag == TAG_STRING && b->tag == TAG_INT) {
        int64_t n = b->as.i;
        if (n <= 0) return fray_string("", 0);
        size_t total = a->as.str.len * (size_t)n;
        char *buf = (char *)malloc(total + 1);
        if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
        for (int64_t i = 0; i < n; i++)
            memcpy(buf + i * a->as.str.len, a->as.str.data, a->as.str.len);
        buf[total] = '\0';
        FrayValue result = fray_string(buf, total);
        free(buf);
        return result;
    }
    if (a->tag == TAG_INT && b->tag == TAG_STRING)
        return fray_mul(b, a);

    if (is_numeric(a) && is_numeric(b)) {
        if (a->tag == TAG_FLOAT || b->tag == TAG_FLOAT)
            return fray_float(to_float(a) * to_float(b));
        return fray_int(to_int(a) * to_int(b));
    }
    fray_throw(FRAY_EXC_TYPE, "TypeError: unsupported operand type(s) for *");
    return NULL;
}

FrayValue fray_div(FrayValue a, FrayValue b) {
    if (!a || !b || !is_numeric(a) || !is_numeric(b)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: unsupported operand type(s) for /");
        return NULL;
    }
    double divisor = to_float(b);
    if (divisor == 0.0) {
        fray_throw(FRAY_EXC_ZERO_DIV, "ZeroDivisionError: division by zero");
        return fray_float(0.0);
    }
    return fray_float(to_float(a) / divisor);
}

FrayValue fray_floordiv(FrayValue a, FrayValue b) {
    if (!a || !b || !is_numeric(a) || !is_numeric(b)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: unsupported operand type(s) for //");
        return NULL;
    }
    if (a->tag == TAG_FLOAT || b->tag == TAG_FLOAT) {
        double divisor = to_float(b);
        if (divisor == 0.0) {
            fray_throw(FRAY_EXC_ZERO_DIV, "ZeroDivisionError: division by zero");
            return fray_float(0.0);
        }
        return fray_float(floor(to_float(a) / divisor));
    }
    int64_t divisor = to_int(b);
    if (divisor == 0) {
        fray_throw(FRAY_EXC_ZERO_DIV, "ZeroDivisionError: division by zero");
        return fray_int(0);
    }
    int64_t dividend = to_int(a);
    int64_t result = dividend / divisor;
    /* Floor division: round toward negative infinity */
    if ((dividend % divisor != 0) && ((dividend < 0) != (divisor < 0)))
        result--;
    return fray_int(result);
}

FrayValue fray_mod(FrayValue a, FrayValue b) {
    if (!a || !b || !is_numeric(a) || !is_numeric(b)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: unsupported operand type(s) for %");
        return NULL;
    }
    if (a->tag == TAG_FLOAT || b->tag == TAG_FLOAT) {
        double divisor = to_float(b);
        if (divisor == 0.0) {
            fray_throw(FRAY_EXC_ZERO_DIV, "ZeroDivisionError: division by zero");
            return fray_float(0.0);
        }
        double r = fmod(to_float(a), divisor);
        if (r != 0.0 && ((r < 0.0) != (divisor < 0.0))) r += divisor;
        return fray_float(r);
    }
    int64_t divisor = to_int(b);
    if (divisor == 0) {
        fray_throw(FRAY_EXC_ZERO_DIV, "ZeroDivisionError: division by zero");
        return fray_int(0);
    }
    int64_t result = to_int(a) % divisor;
    /* Euclidean modulo: result always has the divisor's sign */
    if (result != 0 && ((result < 0) != (divisor < 0))) result += divisor;
    return fray_int(result);
}

FrayValue fray_pow(FrayValue a, FrayValue b) {
    if (!a || !b || !is_numeric(a) || !is_numeric(b)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: unsupported operand type(s) for ^");
        return NULL;
    }
    if (a->tag == TAG_FLOAT || b->tag == TAG_FLOAT)
        return fray_float(pow(to_float(a), to_float(b)));
    int64_t base = to_int(a);
    int64_t exp = to_int(b);
    int64_t result = 1;
    while (exp > 0) {
        if (exp & 1) result *= base;
        exp >>= 1;
        if (exp) base *= base;
    }
    return fray_int(result);
}

FrayValue fray_neg(FrayValue a) {
    if (!a) return NULL;
    if (a->tag == TAG_FLOAT) return fray_float(-a->as.f);
    if (a->tag == TAG_INT) return fray_int(-a->as.i);
    if (a->tag == TAG_BOOL) return fray_int(a->as.b ? -1 : 0);
    fray_throw(FRAY_EXC_TYPE, "TypeError: bad operand type for unary -");
    return NULL;
}

/* ── Comparison ── */

FrayValue fray_eq(FrayValue a, FrayValue b) {
    if (a == NULL && b == NULL) return fray_bool(true);
    if (a == NULL || b == NULL) return fray_bool(false);

    if (a->tag != b->tag) {
        if (is_numeric(a) && is_numeric(b))
            return fray_bool(to_float(a) == to_float(b));
        return fray_bool(false);
    }

    switch (a->tag) {
        case TAG_INT:    return fray_bool(a->as.i == b->as.i);
        case TAG_FLOAT:  return fray_bool(a->as.f == b->as.f);
        case TAG_BOOL:   return fray_bool(a->as.b == b->as.b);
        case TAG_STRING: return fray_bool(a->as.str.len == b->as.str.len &&
                                          memcmp(a->as.str.data, b->as.str.data,
                                                 a->as.str.len) == 0);
        default:         return fray_bool(a == b);
    }
}

FrayValue fray_neq(FrayValue a, FrayValue b) {
    FrayValue eq = fray_eq(a, b);
    FrayValue result = fray_bool(!eq->as.b);
    fray_release(eq);
    return result;
}

static int value_cmp(FrayValue a, FrayValue b) {
    /* -1, 0, 1 for numeric and string comparisons; -2 if incomparable. */
    if (is_numeric(a) && is_numeric(b)) {
        double x = to_float(a), y = to_float(b);
        return x < y ? -1 : x > y ? 1 : 0;
    }
    if (a->tag == TAG_STRING && b->tag == TAG_STRING) {
        size_t min_len = a->as.str.len < b->as.str.len ? a->as.str.len : b->as.str.len;
        int c = memcmp(a->as.str.data, b->as.str.data, min_len);
        if (c) return c < 0 ? -1 : 1;
        return a->as.str.len < b->as.str.len ? -1
             : a->as.str.len > b->as.str.len ? 1 : 0;
    }
    return -2;
}

FrayValue fray_lt(FrayValue a, FrayValue b) {
    int c = (a && b) ? value_cmp(a, b) : -2;
    return fray_bool(c == -1);
}

FrayValue fray_gt(FrayValue a, FrayValue b) {
    int c = (a && b) ? value_cmp(a, b) : -2;
    return fray_bool(c == 1);
}

FrayValue fray_lte(FrayValue a, FrayValue b) {
    int c = (a && b) ? value_cmp(a, b) : -2;
    return fray_bool(c == -1 || c == 0);
}

FrayValue fray_gte(FrayValue a, FrayValue b) {
    int c = (a && b) ? value_cmp(a, b) : -2;
    return fray_bool(c == 1 || c == 0);
}

/* ── Boolean ── */

bool fray_is_truthy(FrayValue v) {
    if (!v) return false;
    switch (v->tag) {
        case TAG_BOOL:   return v->as.b;
        case TAG_INT:    return v->as.i != 0;
        case TAG_FLOAT:  return v->as.f != 0.0;
        case TAG_STRING: return v->as.str.len > 0;
        case TAG_LIST:   return v->as.list.len > 0;
        case TAG_TUPLE:  return v->as.tuple.len > 0;
        case TAG_SET:    return v->as.set.len > 0;
        default:         return true;
    }
}

FrayValue fray_and(FrayValue a, FrayValue b) {
    return fray_bool(fray_is_truthy(a) && fray_is_truthy(b));
}

FrayValue fray_or(FrayValue a, FrayValue b) {
    return fray_bool(fray_is_truthy(a) || fray_is_truthy(b));
}

FrayValue fray_xor(FrayValue a, FrayValue b) {
    return fray_bool(fray_is_truthy(a) ^ fray_is_truthy(b));
}

FrayValue fray_xnor(FrayValue a, FrayValue b) {
    return fray_bool(!(fray_is_truthy(a) ^ fray_is_truthy(b)));
}

FrayValue fray_not(FrayValue a) {
    return fray_bool(!fray_is_truthy(a));
}

/* ── Built-in functions ── */

/* Raw length: the same answer as fray_len without allocating a box.
 * Generated code re-checks a for-loop's bound every iteration, so this is
 * on the per-element path; fray_len keeps the boxed builtin ABI. */
int64_t fray_len_raw(FrayValue v) {
    if (!v) return 0;
    switch (v->tag) {
        case TAG_STRING: return (int64_t)v->as.str.len;
        case TAG_LIST:   return (int64_t)v->as.list.len;
        case TAG_TUPLE:  return (int64_t)v->as.tuple.len;
        case TAG_SET:    return (int64_t)v->as.set.len;
        case TAG_MAP: {
            FrayValue n = fray_map_len(v);   /* owned box */
            int64_t len = n ? n->as.i : 0;
            fray_release(n);
            return len;
        }
        default:         return 0;
    }
}

FrayValue fray_len(FrayValue v) {
    return fray_int(fray_len_raw(v));
}

/* ── Membership test: needle in haystack ── */

static bool _string_contains(const char *h, size_t hlen, const char *n, size_t nlen) {
    if (nlen == 0) return true;
    if (nlen > hlen) return false;
    for (size_t i = 0; i <= hlen - nlen; i++) {
        if (memcmp(h + i, n, nlen) == 0) return true;
    }
    return false;
}

FrayValue fray_contains(FrayValue haystack, FrayValue needle) {
    if (!haystack) return fray_bool(false);
    switch (haystack->tag) {
        case TAG_LIST: {
            for (size_t i = 0; i < haystack->as.list.len; i++) {
                FrayValue eq = fray_eq(haystack->as.list.elems[i], needle);
                bool r = eq->as.b;
                fray_release(eq);
                if (r) return fray_bool(true);
            }
            return fray_bool(false);
        }
        case TAG_TUPLE: {
            for (size_t i = 0; i < haystack->as.tuple.len; i++) {
                FrayValue eq = fray_eq(haystack->as.tuple.elems[i], needle);
                bool r = eq->as.b;
                fray_release(eq);
                if (r) return fray_bool(true);
            }
            return fray_bool(false);
        }
        case TAG_SET: {
            for (size_t i = 0; i < haystack->as.set.len; i++) {
                FrayValue eq = fray_eq(haystack->as.set.elems[i], needle);
                bool r = eq->as.b;
                fray_release(eq);
                if (r) return fray_bool(true);
            }
            return fray_bool(false);
        }
        case TAG_STRING: {
            if (!needle || needle->tag != TAG_STRING) return fray_bool(false);
            bool found = _string_contains(
                haystack->as.str.data, haystack->as.str.len,
                needle->as.str.data, needle->as.str.len);
            return fray_bool(found);
        }
        case TAG_MAP:
            return fray_map_has(haystack, needle);
        default:
            return fray_bool(false);
    }
}

/* ── ord / chr ── */

FrayValue fray_ord(FrayValue c) {
    if (!c || c->tag != TAG_STRING || c->as.str.len != 1) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: ord() expected a single character");
        return fray_none();
    }
    return fray_int((int64_t)(unsigned char)c->as.str.data[0]);
}

FrayValue fray_chr(FrayValue n) {
    if (!n || n->tag != TAG_INT) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: chr() expected an integer");
        return fray_none();
    }
    int64_t code = n->as.i;
    if (code < 0 || code > 127) {
        fray_throw(FRAY_EXC_RUNTIME, "ValueError: chr() arg out of range");
        return fray_none();
    }
    char buf[2] = { (char)(unsigned char)code, 0 };
    return fray_string_copy(buf);
}

/* ── String / list / tuple slicing ── */

FrayValue fray_slice(FrayValue seq, FrayValue start_v, FrayValue stop_v, FrayValue step_v) {
    if (!seq) return fray_none();

    /* For strings: produce a new string */
    if (seq->tag == TAG_STRING) {
        const char *data = seq->as.str.data;
        int64_t len = (int64_t)seq->as.str.len;
        int64_t step = 1;
        int64_t start = 0;
        int64_t stop = len;

        if (step_v && step_v->tag == TAG_INT) step = step_v->as.i;
        if (start_v && start_v->tag == TAG_INT) start = start_v->as.i;
        if (stop_v && stop_v->tag == TAG_INT) stop = stop_v->as.i;

        if (step == 0) {
            fray_throw(FRAY_EXC_RUNTIME, "ValueError: slice step cannot be zero");
            return fray_none();
        }

        /* Clamp */
        if (start < 0) start += len;
        if (start < 0) start = (step < 0) ? 0 : 0;
        if (start > len) start = (step < 0) ? len : len;
        if (stop < 0) stop += len;
        if (stop < 0) stop = (step < 0) ? 0 : 0;
        if (stop > len) stop = (step < 0) ? len : len;

        /* Count characters */
        int64_t count = 0;
        if (step > 0) {
            if (start < stop)
                count = (stop - start + step - 1) / step;
        } else {
            if (start > stop)
                count = (start - stop - step - 1) / (-step);
        }
        if (count < 0) count = 0;

        char *buf = (char *)malloc((size_t)count + 1);
        int64_t idx = start;
        for (int64_t i = 0; i < count; i++) {
            buf[i] = data[idx];
            idx += step;
        }
        buf[count] = 0;
        FrayValue result = fray_string_copy(buf);
        free(buf);
        return result;
    }

    /* For lists: produce a new list */
    if (seq->tag == TAG_LIST || seq->tag == TAG_TUPLE) {
        FrayValue *elems = (seq->tag == TAG_LIST) ? seq->as.list.elems : seq->as.tuple.elems;
        int64_t len = (int64_t)((seq->tag == TAG_LIST) ? seq->as.list.len : seq->as.tuple.len);
        int64_t step = 1;
        int64_t start = 0;
        int64_t stop = len;

        if (step_v && step_v->tag == TAG_INT) step = step_v->as.i;
        if (start_v && start_v->tag == TAG_INT) start = start_v->as.i;
        if (stop_v && stop_v->tag == TAG_INT) stop = stop_v->as.i;

        if (step == 0) {
            fray_throw(FRAY_EXC_RUNTIME, "ValueError: slice step cannot be zero");
            return fray_none();
        }

        /* Clamp */
        if (start < 0) start += len;
        if (start < 0) start = (step < 0) ? 0 : 0;
        if (start > len) start = (step < 0) ? len : len;
        if (stop < 0) stop += len;
        if (stop < 0) stop = (step < 0) ? 0 : 0;
        if (stop > len) stop = (step < 0) ? len : len;

        FrayValue result = fray_list();
        if (step > 0) {
            for (int64_t i = start; i < stop; i += step) {
                fray_list_append(result, elems[i]);
            }
        } else {
            for (int64_t i = start; i > stop; i += step) {
                fray_list_append(result, elems[i]);
            }
        }
        return result;
    }

    fray_throw(FRAY_EXC_TYPE, "TypeError: object does not support slicing");
    return fray_none();
}

/* Returning an aliased operand (min/max) still hands the caller an owned
 * reference, so bump the count here. */
FrayValue fray_retained(FrayValue v) {
    if (v) atomic_fetch_add_explicit(&v->refcount, 1, memory_order_relaxed);
    return v;
}

FrayValue fray_min(FrayValue a, FrayValue b) {
    int c = (a && b) ? value_cmp(a, b) : -2;
    if (c == -2) return fray_retained(a ? a : b);
    return fray_retained(c <= 0 ? a : b);
}

FrayValue fray_max(FrayValue a, FrayValue b) {
    int c = (a && b) ? value_cmp(a, b) : -2;
    if (c == -2) return fray_retained(a ? a : b);
    return fray_retained(c >= 0 ? a : b);
}

FrayValue fray_sum(FrayValue v) {
    if (!v || v->tag != TAG_LIST) return fray_int(0);
    int64_t isum = 0;
    double fsum = 0.0;
    bool any_float = false;
    for (size_t i = 0; i < v->as.list.len; i++) {
        FrayValue elem = v->as.list.elems[i];
        if (!elem) continue;
        if (elem->tag == TAG_FLOAT) {
            if (!any_float) { fsum = (double)isum; any_float = true; }
            fsum += elem->as.f;
        } else if (elem->tag == TAG_INT) {
            if (any_float) fsum += (double)elem->as.i;
            else           isum += elem->as.i;
        } else if (elem->tag == TAG_BOOL) {
            if (any_float) fsum += elem->as.b ? 1.0 : 0.0;
            else           isum += elem->as.b ? 1 : 0;
        }
    }
    return any_float ? fray_float(fsum) : fray_int(isum);
}

/* Specialized: every element is known-int. */
FrayValue fray_sum_fast(FrayValue v) {
    if (!v || v->tag != TAG_LIST) return fray_int(0);
    int64_t sum = 0;
    for (size_t i = 0; i < v->as.list.len; i++) {
        FrayValue elem = v->as.list.elems[i];
        if (elem) {
            if (elem->tag == TAG_INT) sum += elem->as.i;
            else if (elem->tag == TAG_BOOL && elem->as.b) sum += 1;
        }
    }
    return fray_int(sum);
}

FrayValue fray_abs(FrayValue v) {
    if (!v) return fray_int(0);
    if (v->tag == TAG_INT) return fray_int(v->as.i < 0 ? -v->as.i : v->as.i);
    if (v->tag == TAG_FLOAT) return fray_float(fabs(v->as.f));
    if (v->tag == TAG_BOOL) return fray_int(v->as.b ? 1 : 0);
    return fray_int(0);
}

FrayValue fray_sqrt(FrayValue v) {
    if (!v) return fray_float(0.0);
    return fray_float(sqrt(to_float(v)));
}

FrayValue fray_isqrt(FrayValue v) {
    if (!v) return fray_int(0);
    int64_t n = to_int(v);
    if (n < 0) return fray_int(0);
    int64_t x = n;
    int64_t y = (x + 1) / 2;
    while (y < x) {
        x = y;
        y = (x + n / x) / 2;
    }
    return fray_int(x);
}

/* round/int/float pass a value of the target type straight through. The
 * generated code releases the argument after every unary builtin call (the
 * runtime only borrows it), so a pass-through must hand back a *retained*
 * alias rather than the bare pointer — otherwise `int(42)`, `float(1.5)` and
 * `round(5)` freed the box they had just returned (double free in tcache). */
FrayValue fray_round_val(FrayValue v) {
    if (!v) return fray_int(0);
    if (v->tag == TAG_FLOAT) return fray_int((int64_t)llround(v->as.f));
    if (v->tag == TAG_BOOL) return fray_int(v->as.b ? 1 : 0);
    return fray_retained(v);
}

FrayValue fray_int_val(FrayValue v) {
    if (!v) return fray_int(0);
    if (v->tag == TAG_STRING) return fray_int(strtoll(v->as.str.data, NULL, 10));
    if (v->tag == TAG_FLOAT) return fray_int((int64_t)v->as.f);
    if (v->tag == TAG_BOOL) return fray_int(v->as.b ? 1 : 0);
    return fray_retained(v);
}

FrayValue fray_float_val(FrayValue v) {
    if (!v) return fray_float(0.0);
    if (v->tag == TAG_STRING) return fray_float(strtod(v->as.str.data, NULL));
    if (v->tag == TAG_INT) return fray_float((double)v->as.i);
    if (v->tag == TAG_BOOL) return fray_float(v->as.b ? 1.0 : 0.0);
    return fray_retained(v);
}

FrayValue fray_str_val(FrayValue v) {
    if (!v) return fray_string_copy("None");
    switch (v->tag) {
        case TAG_INT: {
            char buf[32];
            snprintf(buf, sizeof(buf), "%lld", (long long)v->as.i);
            return fray_string_copy(buf);
        }
        case TAG_FLOAT: {
            /* Shortest round-trip repr, matching the printer. */
            char buf[64];
            fray_format_double(v->as.f, buf, sizeof(buf));
            return fray_string_copy(buf);
        }
        case TAG_BOOL: return fray_string_copy(v->as.b ? "True" : "False");
        case TAG_STRING: return fray_string(v->as.str.data, v->as.str.len);
        default: {
            /* Containers: build via the shared buffer repr printer. */
            size_t len = 0;
            char *out = fray_repr_alloc(v, &len);
            if (!out) return fray_string_copy("<object>");
            FrayValue result = fray_string(out, len);
            free(out);
            return result;
        }
    }
}

FrayValue fray_range3(FrayValue start, FrayValue stop, FrayValue step) {
    int64_t a = fray_as_int(start), b = fray_as_int(stop), s = fray_as_int(step);
    if (s == 0) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: range() step must not be zero");
        return fray_list();
    }
    int64_t count = (s > 0) ? ((b > a) ? (b - a + s - 1) / s : 0)
                            : ((b < a) ? (a - b - s - 1) / (-s) : 0);
    FrayValue list = fray_list();
    if (count > 0) {
        fray_root_push(list);
        list->as.list.elems = (FrayValue *)calloc((size_t)count, sizeof(FrayValue));
        list->as.list.len = (size_t)count;
        list->as.list.cap = (size_t)count;
        fray_root_pop();
        int64_t v = a;
        for (int64_t i = 0; i < count; i++) {
            list->as.list.elems[i] = fray_int(v);
            v += s;
        }
    }
    return list;
}

FrayValue fray_range(FrayValue v) {
    /* range(n) is range(0, n, 1). fray_range3 only *reads* its bounds, so
     * the two temporary boxes it is handed must be dropped here. */
    FrayValue start = fray_int(0);
    FrayValue step = fray_int(1);
    FrayValue result = fray_range3(start, v, step);
    fray_release(start);
    fray_release(step);
    return result;
}

/* ── List operations ── */

static void list_grow(FrayValue list) {
    if (list->as.list.len >= list->as.list.cap) {
        size_t new_cap = list->as.list.cap ? list->as.list.cap * 2 : 8;
        FrayValue *p = (FrayValue *)realloc(list->as.list.elems,
                                            new_cap * sizeof(FrayValue));
        if (!p) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
        list->as.list.elems = p;
        list->as.list.cap = new_cap;
    }
}

void fray_list_append(FrayValue list, FrayValue elem) {
    /* The tag check is not a formality: `as.list` and `as.int` share a union,
     * so appending to a boxed int reads the number's bits as a length and a
     * capacity and writes through the "elements" pointer it finds there.
     * `.append` is documented for lists and sets, and both spellings reach
     * fray_append below, which dispatches; this guard is what a direct call
     * (a list literal being built, or a set that took the list arm) gets.
     */
    if (!list || list->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a list");
        return;
    }
    /* Shared-object discipline: lock the shard, mutate, unlock. Root-push
     * guards the allocations inside (grow/eq) against stop-the-world GC.
     * Both the lock and the roots are on the per-element path of a
     * build-and-fill loop, so neither may allocate. */
    fray_root_push(list);
    fray_root_push(elem);
    fray_obj_lock(list);
    list_grow(list);
    list->as.list.elems[list->as.list.len++] = elem;
    fray_retain(elem);
    fray_obj_unlock(list);
    fray_root_pop();
    fray_root_pop();
}

FrayValue fray_list_depend(FrayValue list) {
    if (!list || list->tag != TAG_LIST || list->as.list.len == 0) {
        fray_throw(FRAY_EXC_INDEX, "IndexError: pop from empty list");
        return fray_none();
    }
    fray_obj_lock(list);
    if (list->as.list.len == 0) {  /* re-check under the lock */
        fray_obj_unlock(list);
        fray_throw(FRAY_EXC_INDEX, "IndexError: pop from empty list");
        return fray_none();
    }
    FrayValue elem = list->as.list.elems[--list->as.list.len];
    fray_obj_unlock(list);
    return elem; /* caller owns the reference */
}

FrayValue fray_list_index(FrayValue list, int64_t idx) {
    if (!list || list->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a list");
        return fray_none();
    }
    if (idx < 0) idx += (int64_t)list->as.list.len;
    if (idx < 0 || (size_t)idx >= list->as.list.len) {
        fray_throw(FRAY_EXC_INDEX, "IndexError: list index out of range");
        return fray_none();
    }
    FrayValue elem = list->as.list.elems[idx];
    fray_retain(elem);
    return elem;
}

void fray_list_setindex(FrayValue list, int64_t idx, FrayValue val) {
    if (!list || list->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a list");
        return;
    }
    if (idx < 0) idx += (int64_t)list->as.list.len;
    if (idx < 0 || (size_t)idx >= list->as.list.len) {
        fray_throw(FRAY_EXC_INDEX, "IndexError: list index out of range");
        return;
    }
    fray_retain(val);
    fray_obj_lock(list);
    FrayValue old = list->as.list.elems[idx];
    list->as.list.elems[idx] = val;
    fray_obj_unlock(list);
    fray_release(old);
}

/* ── Tuple operations ── */

void fray_tuple_setindex(FrayValue tuple, int64_t idx, FrayValue val) {
    /* Construction-time fill; tuples are otherwise immutable. */
    if (!tuple || tuple->tag != TAG_TUPLE) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a tuple");
        return;
    }
    if (idx < 0 || (size_t)idx >= tuple->as.tuple.len) {
        fray_throw(FRAY_EXC_INDEX, "IndexError: tuple index out of range");
        return;
    }
    fray_retain(val);
    FrayValue old = tuple->as.tuple.elems[idx];
    tuple->as.tuple.elems[idx] = val;
    if (old) fray_release(old);
}

FrayValue fray_tuple_index(FrayValue tuple, int64_t idx) {
    if (!tuple || tuple->tag != TAG_TUPLE) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a tuple");
        return fray_none();
    }
    if (idx < 0) idx += (int64_t)tuple->as.tuple.len;
    if (idx < 0 || (size_t)idx >= tuple->as.tuple.len) {
        fray_throw(FRAY_EXC_INDEX, "IndexError: tuple index out of range");
        return fray_none();
    }
    FrayValue elem = tuple->as.tuple.elems[idx];
    fray_retain(elem);
    return elem;
}

/* ── Set operations ── */

static bool set_contains(FrayValue set, FrayValue elem) {
    for (size_t i = 0; i < set->as.set.len; i++) {
        FrayValue eq = fray_eq(set->as.set.elems[i], elem);
        bool found = eq->as.b;
        fray_release(eq);
        if (found) return true;
    }
    return false;
}

void fray_set_append(FrayValue set, FrayValue elem) {
    if (!set || set->tag != TAG_SET) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a set");
        return;
    }
    fray_root_push(set);
    fray_root_push(elem);
    fray_obj_lock(set);
    if (set_contains(set, elem)) {
        fray_obj_unlock(set);
        fray_root_pop();
        fray_root_pop();
        return; /* deduplicated, like the oracle */
    }
    if (set->as.set.len >= set->as.set.cap) {
        size_t new_cap = set->as.set.cap ? set->as.set.cap * 2 : 8;
        FrayValue *p = (FrayValue *)realloc(set->as.set.elems,
                                            new_cap * sizeof(FrayValue));
        if (!p) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
        set->as.set.elems = p;
        set->as.set.cap = new_cap;
    }
    set->as.set.elems[set->as.set.len++] = elem;
    fray_retain(elem);
    fray_obj_unlock(set);
    fray_root_pop();
    fray_root_pop();
}

FrayValue fray_set_depend(FrayValue set) {
    if (!set || set->tag != TAG_SET || set->as.set.len == 0) {
        fray_throw(FRAY_EXC_INDEX, "IndexError: pop from empty set");
        return fray_none();
    }
    fray_obj_lock(set);
    if (set->as.set.len == 0) {  /* re-check under the lock */
        fray_obj_unlock(set);
        fray_throw(FRAY_EXC_INDEX, "IndexError: pop from empty set");
        return fray_none();
    }
    FrayValue elem = set->as.set.elems[--set->as.set.len];
    fray_obj_unlock(set);
    return elem;
}

/* ── Container method dispatch ──
 *
 * fray.txt documents `.append` and `.depend` on lists and on sets alike (“the
 * mutable group” and “the random group” show the same two spellings), and a
 * variable's type is a runtime fact, so the emitters lower both methods to
 * these two helpers and the tag decides the arm. Both keep the conventions of
 * the operations they forward to: append retains the element, depend hands the
 * caller an owned reference.
 *
 * The type error is the point of having the helpers at all: calling either
 * through a union is a memory-safety bug, not a wrong answer, and the element
 * of a list is an int as often as it is a container.
 */
void fray_append(FrayValue container, FrayValue elem) {
    if (container && container->tag == TAG_SET) {
        fray_set_append(container, elem);
        return;
    }
    if (container && container->tag == TAG_LIST) {
        fray_list_append(container, elem);
        return;
    }
    fray_throw(FRAY_EXC_TYPE, "TypeError: object is not a list or a set");
}

FrayValue fray_depend(FrayValue container) {
    if (container && container->tag == TAG_SET) return fray_set_depend(container);
    if (container && container->tag == TAG_LIST) return fray_list_depend(container);
    fray_throw(FRAY_EXC_TYPE, "TypeError: object is not a list or a set");
    return fray_none();
}

/* ── Type conversion helpers ── */

int64_t fray_as_int(FrayValue v) {
    if (!v) return 0;
    if (v->tag == TAG_INT) return v->as.i;
    if (v->tag == TAG_FLOAT) return (int64_t)v->as.f;
    if (v->tag == TAG_BOOL) return v->as.b ? 1 : 0;
    return 0;
}

double fray_as_float(FrayValue v) {
    if (!v) return 0.0;
    if (v->tag == TAG_FLOAT) return v->as.f;
    if (v->tag == TAG_INT) return (double)v->as.i;
    if (v->tag == TAG_BOOL) return v->as.b ? 1.0 : 0.0;
    return 0.0;
}

const char *fray_as_string(FrayValue v) {
    if (!v) return "";
    if (v->tag == TAG_STRING) return v->as.str.data;
    return "";
}

/* ── Calling a value (Phase 8) ──
 *
 * Every function used as a value gets a wrapper from the emitter with the
 * shape all three entry points already invoke — `void fray_val.<name>
 * (FrayValue self)` — so the callee never learns which one called it. The
 * wrapper reads the arguments back out of fray_call_args, unpacks them into
 * the declared parameters, and hands its result to fray_call_result_store.
 *
 * The slots are saved and restored rather than stacked: a call in progress is
 * always the innermost one, so nesting costs two words and never allocates.
 * The argument list stays owned by fray_call's caller for the duration of the
 * call; its count keeps it alive through any collection the callee triggers.
 */

static _Thread_local FrayValue tls_call_args = NULL;   /* borrowed */
static _Thread_local FrayValue tls_call_result = NULL; /* owned    */

FrayValue fray_call_args(void) {
    /* A wrapper only ever runs inside fray_call. The fallback keeps one that
     * somehow ran on its own from dereferencing NULL: indexing an empty list
     * fails loudly instead. */
    return tls_call_args ? tls_call_args : fray_none();
}

void fray_call_result_store(FrayValue v) {
    if (tls_call_result) fray_release(tls_call_result);
    tls_call_result = v;
}

FrayValue fray_call(FrayValue fn, FrayValue args) {
    if (!fn || fn->tag != TAG_FUNCTION) {
        size_t len = 0;
        char *repr = fray_repr_alloc(fn, &len);
        char msg[224];
        snprintf(msg, sizeof(msg), "TypeError: '%.*s' is not callable",
                 (int)(len > 192 ? 192 : len), repr ? repr : "None");
        free(repr);
        fray_throw(FRAY_EXC_TYPE, msg);
        return fray_none();
    }
    if (!args || args->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE,
                   "TypeError: fray_call expects an argument list");
        return fray_none();
    }

    int64_t given = (int64_t)args->as.list.len;
    if (given != (int64_t)fn->as.func.arity) {
        char msg[224];
        snprintf(msg, sizeof(msg),
                 "TypeError: '%s' takes %d argument(s) but %lld given",
                 fn->as.func.name ? fn->as.func.name : "?",
                 fn->as.func.arity, (long long)given);
        fray_throw(FRAY_EXC_TYPE, msg);
        return fray_none();
    }

    FrayValue saved_args = tls_call_args;
    FrayValue saved_result = tls_call_result;
    tls_call_args = args;
    tls_call_result = NULL;

    ((void (*)(FrayValue))fn->as.func.code)(fn);

    FrayValue result = tls_call_result ? tls_call_result : fray_none();
    tls_call_result = NULL;
    tls_call_args = saved_args;
    tls_call_result = saved_result;
    return result;
}
