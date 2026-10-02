/**
 * fray runtime — builtin functions: print, input, statistics, string ops.
 *
 * Split out of ops.c (Phase 5). Semantics mirror bootstrap/evaluator.py
 * exactly — these are what the golden tests compare against.
 *
 * Memory contract: arguments are borrowed; results are owned by the
 * caller. Constructors never fail (exit on OOM), so no result path
 * leaks. Where an exception is thrown a throwaway value is returned
 * for ABI convenience; fray_throw is never observed to return when no
 * try context is active.
 */

#include "runtime.h"
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#include <unistd.h>

/* ── Printing ── */

void fray_print(FrayValue v) {
    fray_fprint_repr(stdout, v);
    fputc('\n', stdout);
}

/* print() is variadic: everything but the last argument is written with a
 * trailing space (fray_print_sep) and the last one closes the line
 * (fray_print). Splitting it that way keeps every call monomorphic — no C
 * varargs, no argument array — and leaves the single-argument case, which is
 * overwhelmingly the common one, as exactly the call it has always been.
 * print() with no arguments at all is fray_print_end: a bare newline, which
 * is what the oracle and bootstrap/evaluator.py already did. */
void fray_print_sep(FrayValue v) {
    fray_fprint_repr(stdout, v);
    fputc(' ', stdout);
}

void fray_print_end(void) {
    fputc('\n', stdout);
}

/* ── Input ── */

static FrayValue read_line(void) {
    size_t cap = 64, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
    int c;
    while ((c = fgetc(stdin)) != EOF && c != '\n') {
        if (len + 1 >= cap) {
            cap *= 2;
            char *p = (char *)realloc(buf, cap);
            if (!p) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
            buf = p;
        }
        buf[len++] = (char)c;
    }
    FrayValue s = fray_string(buf, len);
    free(buf);
    return s;
}

FrayValue fray_input(void) {
    return read_line();
}

FrayValue fray_input_str(const char *prompt) {
    if (prompt) fputs(prompt, stdout);
    fflush(stdout);
    return read_line();
}

FrayValue fray_input_int(void) {
    FrayValue line = read_line();
    if (!line || line->tag != TAG_STRING) return fray_int(0);
    const char *p = line->as.str.data;
    char *end = NULL;
    errno = 0;
    long long val = strtoll(p, &end, 10);
    while (end && *end && isspace((unsigned char)*end)) end++;
    if (end == p || (end && *end != '\0')) {
        fray_release(line);
        fray_throw(FRAY_EXC_VALUE, "ValueError: invalid literal for int()");
        return fray_int(0);
    }
    fray_release(line);
    return fray_int((int64_t)val);
}

FrayValue fray_input_float(void) {
    FrayValue line = read_line();
    if (!line || line->tag != TAG_STRING) return fray_float(0.0);
    const char *p = line->as.str.data;
    char *end = NULL;
    double val = strtod(p, &end);
    while (end && *end && isspace((unsigned char)*end)) end++;
    if (end == p || (end && *end != '\0')) {
        fray_release(line);
        fray_throw(FRAY_EXC_VALUE, "ValueError: could not convert to float");
        return fray_float(0.0);
    }
    fray_release(line);
    return fray_float(val);
}

/* The prompt forms of the numeric readers: the oracle accepts a prompt on
 * every input builtin (`_builtin_input_int` writes it before reading), so
 * these keep the compiled engines from having to reject `inputInt("n: ")`.
 * Same read, prompt written and flushed first. */
FrayValue fray_input_int_str(const char *prompt) {
    if (prompt) { fputs(prompt, stdout); fflush(stdout); }
    return fray_input_int();
}

FrayValue fray_input_float_str(const char *prompt) {
    if (prompt) { fputs(prompt, stdout); fflush(stdout); }
    return fray_input_float();
}

/* ── Program arguments ──
 *
 * The generated main() calls fray_init_args once with the C argc/argv before
 * any fray code runs; progName() and programArgs() hand out retained aliases.
 * The storage stays owned by this translation unit, so nothing else can free
 * it. (The accessor is programArgs rather than args: `args` is a common local
 * variable name — the self-hosted parser's own parse_postfix uses it — and a
 * builtin of that name would make every block-level `args = []` rebind the
 * global builtin instead of creating a local, corrupting the AST.)
 */

static char *g_prog_name = NULL;
static FrayValue g_argv_list = NULL;   /* fray_list of strings, or NULL */

void fray_init_args(int argc, char **argv) {
    if (g_argv_list) { fray_release(g_argv_list); g_argv_list = NULL; }
    g_prog_name = NULL;
    if (argc <= 0 || !argv) return;
    if (argv[0]) {
        size_t n = strlen(argv[0]);
        char *copy = (char *)malloc(n + 1);
        if (!copy) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
        memcpy(copy, argv[0], n + 1);
        g_prog_name = copy;
    }
    g_argv_list = fray_list();
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i] ? argv[i] : "";
        FrayValue s = fray_string(a, strlen(a));
        fray_list_append(g_argv_list, s);  /* append retains */
    }
}

FrayValue fray_prog_name(void) {
    if (!g_prog_name) return fray_string("", 0);
    return fray_string(g_prog_name, strlen(g_prog_name));
}

FrayValue fray_argv(void) {
    if (!g_argv_list) return fray_list();
    fray_retain(g_argv_list);
    return g_argv_list;
}

/* ── Files ──
 *
 * The self-hosted compiler reads the program it compiles through fileRead,
 * so this path is on the Stage-2 critical path. Error messages mirror the
 * oracle's implementations (bootstrap/evaluator.py) so differential tests
 * see the same diagnostics.
 */

static const char *file_path_arg(FrayValue v, const char *who) {
    if (!v || v->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: path must be a string");
        return NULL;
    }
    (void)who;
    return v->as.str.data;
}

/* How much to reserve for a file whose length the OS cannot tell us. */
#define FRAY_READ_FLOOR 65536

/* Largest length we will believe from a stat before falling back to
 * growing. A regular file on a sane filesystem reports its real size; one
 * that claims more than this is sparse or lying, and reserving for it up
 * front would be a denial of service on a path an attacker controls. */
#define FRAY_READ_TRUST  ((size_t)1 << 30)

/* The length of an open stream, when it is knowable in advance. Returns 0
 * for a pipe, a socket, a character device or /proc, all of which either
 * report no size or a meaningless one — those must grow. */
static size_t stream_length(FILE *f) {
#ifdef _WIN32
    struct _stat64 st;
    if (_fstat64(_fileno(f), &st) != 0) return 0;
    if ((st.st_mode & _S_IFMT) != _S_IFREG) return 0;
    if (st.st_size <= 0) return 0;
    return (size_t)st.st_size > FRAY_READ_TRUST ? 0 : (size_t)st.st_size;
#else
    struct stat st;
    if (fstat(fileno(f), &st) != 0) return 0;
    if (!S_ISREG(st.st_mode)) return 0;
    if (st.st_size <= 0) return 0;
    return (size_t)st.st_size > FRAY_READ_TRUST ? 0 : (size_t)st.st_size;
#endif
}

/* Read a whole stream into a fresh buffer. The buffer is sized from the
 * file when the OS knows it, which turns the common case into one
 * allocation and one read; the doubling loop is kept for everything else
 * and for a file that grew after the stat. Shared by fray_file_read and
 * io.c's slurp, which had the same doubling loop and the same cost.
 *
 * Returns a malloc'd buffer the caller must free and leaves `f` open; the
 * only path that does not return exits the process. */
char *fray_slurp_stream(FILE *f, size_t *out_len) {
    size_t want = stream_length(f);
    /* One byte over the reported size, so the loop's final read is a
     * 1-byte probe that returns 0 at EOF and exits without a realloc. */
    size_t cap = want ? want + 1 : FRAY_READ_FLOOR;
    size_t len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
    for (;;) {
        if (len == cap) {
            cap *= 2;
            char *nb = (char *)realloc(buf, cap);
            if (!nb) { free(buf); fprintf(stderr, "fray: out of memory\n"); exit(1); }
            buf = nb;
        }
        size_t got = fread(buf + len, 1, cap - len, f);
        len += got;
        if (got == 0) break;   /* eof or error; either way we keep what we read */
    }
    *out_len = len;
    return buf;
}

FrayValue fray_file_read(FrayValue path) {
    const char *p = file_path_arg(path, "fileRead");
    if (!p) return fray_none();
    FILE *f = fopen(p, "rb");
    if (!f) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: no such file");
        return fray_none();
    }
    size_t len = 0;
    char *buf = fray_slurp_stream(f, &len);
    fclose(f);
    /* The string adopts the buffer, so a bulk read costs one allocation and
     * one read instead of two allocations and a copy. */
    return fray_string_take(buf, len);
}

FrayValue fray_file_exists(FrayValue path) {
    const char *p = file_path_arg(path, "fileExists");
    if (!p) return fray_none();
    return fray_bool(access(p, F_OK) == 0);
}

void fray_file_write(FrayValue path, FrayValue text) {
    const char *p = file_path_arg(path, "fileWrite");
    if (!p) return;
    if (!text || text->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: fileWrite() text must be a string");
        return;
    }
    FILE *f = fopen(p, "wb");
    if (!f) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: cannot open file for writing");
        return;
    }
    if (text->as.str.len > 0) {
        size_t wrote = fwrite(text->as.str.data, 1, text->as.str.len, f);
        if (wrote != text->as.str.len) {
            fclose(f);
            fray_throw(FRAY_EXC_RUNTIME, "RuntimeError: write failed");
            return;
        }
    }
    fclose(f);
}

FrayValue fray_list_dir(FrayValue path) {
    const char *p = file_path_arg(path, "listDir");
    if (!p) return fray_none();
    DIR *d = opendir(p);
    if (!d) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: cannot read directory");
        return fray_none();
    }
    /* Two passes: count, then fill a fixed buffer — no realloc churn. The
     * count of a directory can only shrink between the passes, so over-
     * allocating by the pass-1 count is always safe. */
    size_t n = 0;
    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) n++;
    FrayValue *names = (FrayValue *)malloc((n > 0 ? n : 1) * sizeof(FrayValue));
    if (!names) { closedir(d); fprintf(stderr, "fray: out of memory\n"); exit(1); }
    rewinddir(d);
    size_t m = 0;
    while ((ent = readdir(d)) != NULL) {
        const char *name = ent->d_name;
        if (name[0] == '.') continue;          /* hidden + . / .. */
        names[m] = fray_string(name, strlen(name));
        m++;
    }
    closedir(d);
    /* insertion sort: deterministic order for a given directory, so the
     * compiler's module discovery does not depend on readdir's order */
    for (size_t i = 1; i < m; i++) {
        FrayValue key = names[i];
        size_t j = i;
        while (j > 0 && strcmp(names[j - 1]->as.str.data, key->as.str.data) > 0) {
            names[j] = names[j - 1];
            j--;
        }
        names[j] = key;
    }
    FrayValue out = fray_list();
    for (size_t i = 0; i < m; i++) {
        fray_list_append(out, names[i]);  /* append retains */
    }
    free(names);
    return out;
}

FrayValue fray_is_dir(FrayValue path) {
    const char *p = file_path_arg(path, "isDir");
    if (!p) return fray_none();
    struct stat st;
    if (stat(p, &st) != 0) return fray_bool(false);
    return fray_bool(S_ISDIR(st.st_mode));
}

void fray_exit_val(FrayValue code) {
    int c = 0;
    if (code) {
        if (code->tag == TAG_INT) c = (int)code->as.i;
        else {
            fray_throw(FRAY_EXC_TYPE, "TypeError: exit() expects an int");
            return;
        }
    }
    exit(c);   /* C's exit flushes stdio, so buffered print output survives */
}

/* ── String operations ── */

FrayValue fray_string_concat(FrayValue a, FrayValue b) {
    if (!a || !b || a->tag != TAG_STRING || b->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: can only concatenate str to str");
        return fray_string_copy("");
    }
    size_t len = a->as.str.len + b->as.str.len;
    char *buf = (char *)malloc(len + 1);
    if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
    memcpy(buf, a->as.str.data, a->as.str.len);
    memcpy(buf + a->as.str.len, b->as.str.data, b->as.str.len);
    FrayValue result = fray_string(buf, len);
    free(buf);
    return result;
}

FrayValue fray_string_repeat(FrayValue s, int64_t n) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: can't multiply str");
        return fray_string_copy("");
    }
    if (n < 0) n = 0;
    size_t count = (size_t)n;
    if (s->as.str.len && count > (SIZE_MAX / s->as.str.len)) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: string too large");
        return fray_string_copy("");
    }
    size_t len = s->as.str.len * count;
    char *buf = (char *)malloc(len + 1);
    if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
    for (size_t i = 0; i < count; i++)
        memcpy(buf + i * s->as.str.len, s->as.str.data, s->as.str.len);
    FrayValue result = fray_string(buf, len);
    free(buf);
    return result;
}

FrayValue fray_string_index(FrayValue s, int64_t idx) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a string");
        return fray_string_copy("");
    }
    if (idx < 0) idx += (int64_t)s->as.str.len;
    if (idx < 0 || (size_t)idx >= s->as.str.len) {
        fray_throw(FRAY_EXC_INDEX, "IndexError: string index out of range");
        return fray_string_copy("");
    }
    return fray_string(s->as.str.data + idx, 1);
}

/* ── String methods ── */

FrayValue fray_string_upper(FrayValue s) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a string");
        return fray_string_copy("");
    }
    size_t len = s->as.str.len;
    char *buf = (char *)malloc(len + 1);
    if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
    for (size_t i = 0; i < len; i++)
        buf[i] = toupper((unsigned char)s->as.str.data[i]);
    buf[len] = '\0';
    FrayValue result = fray_string(buf, len);
    free(buf);
    return result;
}

FrayValue fray_string_lower(FrayValue s) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a string");
        return fray_string_copy("");
    }
    size_t len = s->as.str.len;
    char *buf = (char *)malloc(len + 1);
    if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
    for (size_t i = 0; i < len; i++)
        buf[i] = tolower((unsigned char)s->as.str.data[i]);
    buf[len] = '\0';
    FrayValue result = fray_string(buf, len);
    free(buf);
    return result;
}

FrayValue fray_string_trim(FrayValue s) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a string");
        return fray_string_copy("");
    }
    const char *d = s->as.str.data;
    size_t len = s->as.str.len;
    size_t start = 0;
    while (start < len && isspace((unsigned char)d[start])) start++;
    size_t end = len;
    while (end > start && isspace((unsigned char)d[end - 1])) end--;
    return fray_string(d + start, end - start);
}

FrayValue fray_string_find(FrayValue s, FrayValue sub) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a string");
        return fray_int(0);
    }
    if (!sub || sub->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: expected string");
        return fray_int(0);
    }
    if (sub->as.str.len == 0) return fray_int(0);
    if (s->as.str.len < sub->as.str.len) return fray_int(-1);
    for (size_t i = 0; i <= s->as.str.len - sub->as.str.len; i++) {
        if (memcmp(s->as.str.data + i, sub->as.str.data, sub->as.str.len) == 0)
            return fray_int((int64_t)i);
    }
    return fray_int(-1);
}

FrayValue fray_string_replace(FrayValue s, FrayValue old, FrayValue new_) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a string");
        return fray_string_copy("");
    }
    if (!old || old->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: expected string");
        return fray_string_copy("");
    }
    if (!new_ || new_->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: expected string");
        return fray_string_copy("");
    }
    if (old->as.str.len == 0) {
        /* Empty search string: return original */
        return fray_string(s->as.str.data, s->as.str.len);
    }
    /* Count occurrences */
    size_t count = 0;
    const char *d = s->as.str.data;
    size_t slen = s->as.str.len;
    size_t olen = old->as.str.len;
    for (size_t i = 0; i <= slen - olen; ) {
        if (memcmp(d + i, old->as.str.data, olen) == 0) {
            count++;
            i += olen;
        } else {
            i++;
        }
    }
    if (count == 0) return fray_string(s->as.str.data, s->as.str.len);
    /* Build result */
    size_t nlen = new_->as.str.len;
    size_t max_needed = slen + count * (nlen > olen ? nlen - olen : 0);
    char *buf = (char *)malloc(max_needed + 1);
    if (!buf) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
    size_t pos = 0;
    size_t i = 0;
    while (i < slen) {
        if (i <= slen - olen && memcmp(d + i, old->as.str.data, olen) == 0) {
            memcpy(buf + pos, new_->as.str.data, nlen);
            pos += nlen;
            i += olen;
        } else {
            buf[pos++] = d[i++];
        }
    }
    FrayValue result = fray_string(buf, pos);
    free(buf);
    return result;
}

FrayValue fray_string_split(FrayValue s, FrayValue delim) {
    if (!s || s->tag != TAG_STRING) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a string");
        return fray_list();
    }
    if (!delim || delim->tag != TAG_STRING || delim->as.str.len == 0) {
        fray_throw(FRAY_EXC_TYPE, "ValueError: empty delimiter");
        return fray_list();
    }
    FrayValue result = fray_list();
    const char *d = s->as.str.data;
    size_t slen = s->as.str.len;
    size_t dlen = delim->as.str.len;
    size_t start = 0;
    for (size_t i = 0; i <= slen - dlen; ) {
        if (memcmp(d + i, delim->as.str.data, dlen) == 0) {
            /* append is a *retaining* call, so the piece must be released
             * after it — the list owns one reference, we own the other. */
            FrayValue piece = fray_string(d + start, i - start);
            fray_list_append(result, piece);
            fray_release(piece);
            i += dlen;
            start = i;
        } else {
            i++;
        }
    }
    FrayValue last = fray_string(d + start, slen - start);
    fray_list_append(result, last);
    fray_release(last);
    return result;
}

FrayValue fray_string_startswith(FrayValue s, FrayValue prefix) {
    if (!s || s->tag != TAG_STRING || !prefix || prefix->tag != TAG_STRING)
        return fray_bool(false);
    if (prefix->as.str.len > s->as.str.len) return fray_bool(false);
    return fray_bool(memcmp(s->as.str.data, prefix->as.str.data, prefix->as.str.len) == 0);
}

FrayValue fray_string_endswith(FrayValue s, FrayValue suffix) {
    if (!s || s->tag != TAG_STRING || !suffix || suffix->tag != TAG_STRING)
        return fray_bool(false);
    if (suffix->as.str.len > s->as.str.len) return fray_bool(false);
    return fray_bool(memcmp(s->as.str.data + s->as.str.len - suffix->as.str.len,
                            suffix->as.str.data, suffix->as.str.len) == 0);
}

/* ── Statistics ── */

static bool is_num(FrayValue v) {
    return v && (v->tag == TAG_INT || v->tag == TAG_FLOAT || v->tag == TAG_BOOL);
}

static double num_of(FrayValue v) {
    if (!v) return 0.0;
    if (v->tag == TAG_FLOAT) return v->as.f;
    if (v->tag == TAG_INT)   return (double)v->as.i;
    if (v->tag == TAG_BOOL)  return v->as.b ? 1.0 : 0.0;
    return 0.0;
}

static int cmp_values(const void *pa, const void *pb) {
    FrayValue a = *(FrayValue *)pa, b = *(FrayValue *)pb;
    if (a && b && is_num(a) && is_num(b)) {
        if (a->tag == TAG_INT && b->tag == TAG_INT)
            return (a->as.i > b->as.i) - (a->as.i < b->as.i);
        double da = num_of(a), db = num_of(b);
        return (da > db) - (da < db);
    }
    if (a && b && a->tag == TAG_STRING && b->tag == TAG_STRING) {
        size_t n = a->as.str.len < b->as.str.len ? a->as.str.len : b->as.str.len;
        int c = memcmp(a->as.str.data, b->as.str.data, n);
        if (c) return c;
        return (a->as.str.len > b->as.str.len) - (a->as.str.len < b->as.str.len);
    }
    return 0;
}

static FrayValue *sorted_copy(FrayValue v, size_t *out_n) {
    size_t n = v->as.list.len;
    FrayValue *arr = NULL;
    if (n) {
        arr = (FrayValue *)malloc(n * sizeof(FrayValue));
        if (!arr) { fprintf(stderr, "fray: out of memory\n"); exit(1); }
        memcpy(arr, v->as.list.elems, n * sizeof(FrayValue));
        qsort(arr, n, sizeof(FrayValue), cmp_values);
    }
    *out_n = n;
    return arr;
}

FrayValue fray_mean(FrayValue v) {
    if (!v || v->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: mean() takes a list");
        return fray_none();
    }
    size_t n = v->as.list.len;
    if (n == 0) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: mean() of empty list");
        return fray_none();
    }
    double total = 0.0;
    for (size_t i = 0; i < n; i++)
        if (is_num(v->as.list.elems[i])) total += num_of(v->as.list.elems[i]);
    return fray_float(total / (double)n);
}

FrayValue fray_med(FrayValue v) {
    if (!v || v->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: med() takes a list");
        return fray_none();
    }
    size_t n = 0;
    FrayValue *arr = sorted_copy(v, &n);
    if (n == 0) {
        free(arr);
        fray_throw(FRAY_EXC_VALUE, "ValueError: med() of empty list");
        return fray_none();
    }
    FrayValue result;
    if (n % 2 == 1) {
        result = arr[n / 2];
        fray_retain(result); /* borrowed element -> owned result */
    } else {
        /* Python: int/int is true division, so the even median is a float */
        result = fray_float((num_of(arr[n / 2 - 1]) + num_of(arr[n / 2])) / 2.0);
    }
    free(arr);
    return result;
}

FrayValue fray_mid(FrayValue v) {
    if (!v || v->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: mid() takes a list");
        return fray_none();
    }
    if (v->as.list.len == 0) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: mid() of empty list");
        return fray_none();
    }
    FrayValue result = v->as.list.elems[v->as.list.len / 2];
    fray_retain(result);
    return result;
}

FrayValue fray_mode(FrayValue v) {
    if (!v || v->tag != TAG_LIST) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: mode() takes a list");
        return fray_none();
    }
    size_t n = v->as.list.len;
    if (n == 0) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: mode() of empty list");
        return fray_none();
    }
    /* First-occurrence wins ties (Counter.most_common insertion order). */
    size_t best = 0, best_count = 0;
    for (size_t i = 0; i < n; i++) {
        size_t count = 0;
        for (size_t j = 0; j < n; j++) {
            FrayValue eq = fray_eq(v->as.list.elems[i], v->as.list.elems[j]);
            bool same = eq && eq->as.b;
            fray_release(eq);
            if (same) count++;
        }
        if (count > best_count) { best_count = count; best = i; }
    }
    FrayValue result = v->as.list.elems[best];
    fray_retain(result);
    return result;
}
