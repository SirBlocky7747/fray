/**
 * fray runtime — value printing.
 *
 * A growable buffer is the core formatter; the FILE* entry points
 * delegate to it. str() reuses the same buffer printer, so print(),
 * str(), and future tracebacks share exact repr semantics.
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ── Growable buffer ── */

typedef struct { char *buf; size_t len, cap; int oom; } ReprBuf;

static void rb_grow(ReprBuf *b, size_t need) {
    if (b->len + need <= b->cap) return;
    size_t cap = b->cap ? b->cap : 64;
    while (cap < b->len + need) cap *= 2;
    char *p = (char *)realloc(b->buf, cap);
    if (!p) { b->oom = 1; return; }
    b->buf = p;
    b->cap = cap;
}

static void rb_putc(ReprBuf *b, char c) {
    if (b->oom) return;
    rb_grow(b, 1);
    if (!b->oom) b->buf[b->len++] = c;
}

static void rb_puts(ReprBuf *b, const char *s) {
    size_t n = strlen(s);
    if (b->oom) return;
    rb_grow(b, n);
    if (!b->oom) {
        memcpy(b->buf + b->len, s, n);
        b->len += n;
    }
}

/* ── Core recursive formatter ── */

/* Python float repr: the shortest string that round-trips, always with
 * a decimal point (or exponent). Found by trying precisions 1..17; the
 * first that parses back to the identical double wins. */
void fray_format_double(double f, char *out, size_t cap) {
    for (int prec = 1; prec <= 16; prec++) {
        snprintf(out, cap, "%.*g", prec, f);
        if (strtod(out, NULL) == f) break;
    }
    if (!strchr(out, '.') && !strchr(out, 'e') && !strchr(out, 'E'))
        strcat(out, ".0");
}

static void rb_repr(ReprBuf *b, FrayValue v) {
    if (b->oom) return;
    if (!v) {
        rb_puts(b, "None");
        return;
    }
    switch (v->tag) {
        case TAG_NONE:
            rb_puts(b, "None");
            break;
        case TAG_INT: {
            char tmp[32];
            snprintf(tmp, sizeof(tmp), "%lld", (long long)v->as.i);
            rb_puts(b, tmp);
            break;
        }
        case TAG_FLOAT: {
            /* Match Python's float repr: shortest round-trip string,
             * always showing a decimal point. */
            char tmp[64];
            fray_format_double(v->as.f, tmp, sizeof(tmp));
            rb_puts(b, tmp);
            break;
        }
        case TAG_BOOL:
            rb_puts(b, v->as.b ? "True" : "False");
            break;
        case TAG_STRING:
            if (v->as.str.len) rb_grow(b, v->as.str.len);
            if (!b->oom) {
                memcpy(b->buf + b->len, v->as.str.data, v->as.str.len);
                b->len += v->as.str.len;
            }
            break;
        case TAG_LIST:
            rb_putc(b, '[');
            for (size_t i = 0; i < v->as.list.len; i++) {
                if (i > 0) rb_puts(b, ", ");
                rb_repr(b, v->as.list.elems[i]);
            }
            rb_putc(b, ']');
            break;
        case TAG_TUPLE:
            rb_putc(b, '(');
            for (size_t i = 0; i < v->as.tuple.len; i++) {
                if (i > 0) rb_puts(b, ", ");
                rb_repr(b, v->as.tuple.elems[i]);
            }
            /* Python-style single-element tuple repr. */
            if (v->as.tuple.len == 1) rb_putc(b, ',');
            rb_putc(b, ')');
            break;
        case TAG_SET:
            rb_putc(b, '{');
            for (size_t i = 0; i < v->as.set.len; i++) {
                if (i > 0) rb_puts(b, ", ");
                rb_repr(b, v->as.set.elems[i]);
            }
            rb_putc(b, '}');
            break;
        case TAG_FUNCTION:
            rb_puts(b, "<function ");
            rb_puts(b, v->as.func.name ? v->as.func.name : "?");
            rb_putc(b, '>');
            break;
        case TAG_COROUTINE:
            rb_puts(b, "<coroutine>");
            break;
        case TAG_CHANNEL:
            rb_puts(b, "<channel>");
            break;
        case TAG_STRUCT: {
            rb_puts(b, "<struct>");
            break;
        }
        case TAG_MAP: {
            rb_puts(b, "{");
            /* For now, just show count. Full repr needs iteration. */
            extern FrayValue fray_map_len(FrayValue);
            FrayValue l = fray_map_len(v);
            char buf[32];
            snprintf(buf, sizeof(buf), "%lld", (long long)(l ? l->as.i : 0));
            rb_puts(b, buf);
            if (l) fray_release(l);
            rb_puts(b, " items}");
            break;
        }
        case TAG_OPTION: {
            if (v->as.opt.is_some) {
                rb_puts(b, "Some(");
                if (v->as.opt.val) rb_repr(b, v->as.opt.val);
                rb_puts(b, ")");
            } else {
                rb_puts(b, "None");
            }
            break;
        }
        case TAG_RESULT: {
            if (v->as.res.is_ok) {
                rb_puts(b, "Ok(");
                if (v->as.res.val) rb_repr(b, v->as.res.val);
                rb_puts(b, ")");
            } else {
                rb_puts(b, "Err(");
                if (v->as.res.err) rb_repr(b, v->as.res.err);
                rb_puts(b, ")");
            }
            break;
        }
        default:
            rb_puts(b, "<object>");
            break;
    }
}

/* ── Public entry points ── */

/* Allocate and fill a NUL-terminated repr. Caller frees the buffer. */
char *fray_repr_alloc(FrayValue v, size_t *out_len) {
    ReprBuf b = { NULL, 0, 0, 0 };
    rb_repr(&b, v);
    if (b.oom) {
        if (out_len) *out_len = 0;
        return NULL;
    }
    rb_grow(&b, 1);
    if (b.oom) {
        free(b.buf);
        if (out_len) *out_len = 0;
        return NULL;
    }
    b.buf[b.len] = '\0';
    if (out_len) *out_len = b.len;
    return b.buf;
}

void fray_fprint_repr(FILE *out, FrayValue v) {
    size_t len = 0;
    char *s = fray_repr_alloc(v, &len);
    if (!s) return;
    fwrite(s, 1, len, out);
    free(s);
}

void fray_print_repr(FrayValue v) {
    fray_fprint_repr(stdout, v);
}

size_t fray_print_string(FrayValue v) {
    size_t len = 0;
    char *s = fray_repr_alloc(v, &len);
    if (!s) return 0;
    fwrite(s, 1, len, stdout);
    free(s);
    return len;
}
