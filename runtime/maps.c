/**
 * fray runtime — hash maps (Phase 8).
 *
 * Insertion-ordered hash maps.  Open-addressing with linear probing.
 * Keys: int, float, string, bool, tuple (any immutable). Values: any.
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ── Internal representation ── */

#define MAP_INITIAL_CAP 8
#define MAP_LOAD_FACTOR 75  /* percent */

typedef struct {
    FrayValue  key;
    FrayValue  value;
    bool       occupied;
    bool       deleted;
} MapEntry;

typedef struct {
    MapEntry *entries;
    size_t    cap;        /* power of 2 */
    size_t    len;        /* occupied (non-deleted) count */
    size_t    fill;       /* occupied + deleted count */
} MapState;

/* ── Hashing ── */

static uint64_t hash_value(FrayValue v) {
    if (!v) return 0;
    switch (v->tag) {
        case TAG_INT: {
            uint64_t x = (uint64_t)v->as.i;
            x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
            x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
            x ^= x >> 33;
            return x;
        }
        case TAG_FLOAT: {
            uint64_t bits;
            memcpy(&bits, &v->as.f, sizeof(bits));
            return bits;
        }
        case TAG_BOOL:
            return v->as.b ? 0x9e3779b97f4a7c15ULL : 0x6a09e667f3bcc908ULL;
        case TAG_STRING: {
            /* FNV-1a */
            uint64_t h = 0xcbf29ce484222325ULL;
            for (size_t i = 0; i < v->as.str.len; i++) {
                h ^= (uint8_t)v->as.str.data[i];
                h *= 0x100000001b3ULL;
            }
            return h;
        }
        case TAG_NONE:
            return 0xdeadbeefcafebabeULL;
        default:
            /* Unhashable: use pointer as identity hash */
            return (uint64_t)(uintptr_t)v;
    }
}

static bool values_equal(FrayValue a, FrayValue b) {
    if (a == b) return true;
    if (!a || !b) return false;
    if (a->tag != b->tag) return false;
    switch (a->tag) {
        case TAG_INT:    return a->as.i == b->as.i;
        case TAG_FLOAT:  return a->as.f == b->as.f;
        case TAG_BOOL:   return a->as.b == b->as.b;
        case TAG_NONE:   return true;
        case TAG_STRING:
            return a->as.str.len == b->as.str.len &&
                   memcmp(a->as.str.data, b->as.str.data, a->as.str.len) == 0;
        default:
            return a == b;  /* identity for unhashable types */
    }
}

/* ── Resize ── */

static void map_resize(MapState *m, size_t new_cap) {
    MapEntry *old = m->entries;
    size_t old_cap = m->cap;
    m->entries = calloc(new_cap, sizeof(MapEntry));
    m->cap = new_cap;
    m->fill = 0;
    m->len = 0;

    for (size_t i = 0; i < old_cap; i++) {
        if (old[i].occupied && !old[i].deleted) {
            uint64_t h = hash_value(old[i].key) & (new_cap - 1);
            while (m->entries[h].occupied) {
                h = (h + 1) & (new_cap - 1);
            }
            m->entries[h] = old[i];
            m->len++;
            m->fill++;
        }
    }
    free(old);
}

/* ── Type info ── */

static void map_trace(FrayObj *self) {
    MapState *m = (MapState *)self->as.mp.state;
    if (!m) return;
    for (size_t i = 0; i < m->cap; i++) {
        if (m->entries[i].occupied && !m->entries[i].deleted) {
            if (m->entries[i].key) fray_retain(m->entries[i].key);
            if (m->entries[i].value) fray_retain(m->entries[i].value);
        }
    }
}

static void map_finalize(FrayObj *self) {
    MapState *m = (MapState *)self->as.mp.state;
    if (!m) return;
    for (size_t i = 0; i < m->cap; i++) {
        if (m->entries[i].occupied && !m->entries[i].deleted) {
            if (m->entries[i].key) fray_release(m->entries[i].key);
            if (m->entries[i].value) fray_release(m->entries[i].value);
        }
    }
    free(m->entries);
    free(m);
}

const FrayTypeInfo fray_type_map = {
    .name = "<map>",
    .size = sizeof(FrayObj),
    .trace = map_trace,
    .finalize = map_finalize,
};

/* ── Constructors ── */

FrayValue fray_map_new(void) {
    FrayValue obj = (FrayValue)calloc(1, sizeof(FrayObj));
    obj->tag = TAG_MAP;
    obj->gc_flags = 0;
    obj->generation = 0;
    obj->owner = -1;
    atomic_init(&obj->refcount, 1);
    obj->type = &fray_type_map;

    MapState *m = calloc(1, sizeof(MapState));
    m->cap = MAP_INITIAL_CAP;
    m->entries = calloc(m->cap, sizeof(MapEntry));
    obj->as.mp.state = m;
    return obj;
}

FrayValue fray_map_new_boxed(FrayValue cap_val) {
    (void)cap_val;
    return fray_map_new();
}

/* ── Get / Set / Has / Del ── */

FrayValue fray_map_get(FrayValue map, FrayValue key) {
    if (!map || map->tag != TAG_MAP || !map->as.mp.state) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a map");
        return fray_none();
    }
    MapState *m = (MapState *)map->as.mp.state;
    if (m->len == 0) {
        fray_throw(FRAY_EXC_RUNTIME, "KeyError: key not found");
        return fray_none();
    }
    uint64_t h = hash_value(key) & (m->cap - 1);
    for (size_t i = 0; i < m->cap; i++) {
        MapEntry *e = &m->entries[h];
        if (!e->occupied) break;  /* end of probe chain */
        if (!e->deleted && values_equal(e->key, key)) {
            return e->value ? fray_retained(e->value) : fray_none();
        }
        h = (h + 1) & (m->cap - 1);
    }
    fray_throw(FRAY_EXC_RUNTIME, "KeyError: key not found");
    return fray_none();
}

void fray_map_set(FrayValue map, FrayValue key, FrayValue val) {
    if (!map || map->tag != TAG_MAP || !map->as.mp.state) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a map");
        return;
    }
    MapState *m = (MapState *)map->as.mp.state;

    /* Resize if needed: load factor check */
    if (m->fill * 100 >= m->cap * MAP_LOAD_FACTOR) {
        map_resize(m, m->cap * 2);
    }

    uint64_t h = hash_value(key) & (m->cap - 1);
    for (size_t i = 0; i < m->cap; i++) {
        MapEntry *e = &m->entries[h];
        if (!e->occupied || e->deleted) {
            /* Found a slot — insert new entry */
            e->key = key;     /* ownership taken */
            e->value = val;   /* ownership taken */
            e->occupied = true;
            e->deleted = false;
            m->len++;
            m->fill++;
            return;
        }
        if (values_equal(e->key, key)) {
            /* Update existing entry. The map keeps the key it already holds
             * (equal by value, and its hash bucket is this one), so the
             * caller's transferred ownership of `key` is consumed here —
             * otherwise every update of an existing key leaks one key. */
            FrayValue old = e->value;
            e->value = val;  /* ownership transferred */
            if (old) fray_release(old);
            fray_release(key);
            return;
        }
        h = (h + 1) & (m->cap - 1);
    }
}

FrayValue fray_map_has(FrayValue map, FrayValue key) {
    if (!map || map->tag != TAG_MAP || !map->as.mp.state) {
        return fray_bool(false);
    }
    MapState *m = (MapState *)map->as.mp.state;
    if (m->len == 0) return fray_bool(false);
    uint64_t h = hash_value(key) & (m->cap - 1);
    for (size_t i = 0; i < m->cap; i++) {
        MapEntry *e = &m->entries[h];
        if (!e->occupied) return fray_bool(false);
        if (!e->deleted && values_equal(e->key, key)) {
            return fray_bool(true);
        }
        h = (h + 1) & (m->cap - 1);
    }
    return fray_bool(false);
}

FrayValue fray_map_del(FrayValue map, FrayValue key) {
    if (!map || map->tag != TAG_MAP || !map->as.mp.state) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a map");
        return fray_none();
    }
    MapState *m = (MapState *)map->as.mp.state;
    uint64_t h = hash_value(key) & (m->cap - 1);
    for (size_t i = 0; i < m->cap; i++) {
        MapEntry *e = &m->entries[h];
        if (!e->occupied) {
            fray_throw(FRAY_EXC_RUNTIME, "KeyError: key not found");
            return fray_none();
        }
        if (!e->deleted && values_equal(e->key, key)) {
            if (e->key) fray_release(e->key);
            if (e->value) fray_release(e->value);
            e->key = NULL;
            e->value = NULL;
            e->deleted = true;
            m->len--;
            return fray_none();
        }
        h = (h + 1) & (m->cap - 1);
    }
    fray_throw(FRAY_EXC_RUNTIME, "KeyError: key not found");
    return fray_none();
}

FrayValue fray_map_len(FrayValue map) {
    if (!map || map->tag != TAG_MAP || !map->as.mp.state) {
        return fray_int(0);
    }
    MapState *m = (MapState *)map->as.mp.state;
    return fray_int((int64_t)m->len);
}

FrayValue fray_map_keys(FrayValue map) {
    if (!map || map->tag != TAG_MAP || !map->as.mp.state) {
        return fray_list();
    }
    MapState *m = (MapState *)map->as.mp.state;
    FrayValue result = fray_list();
    for (size_t i = 0; i < m->cap; i++) {
        if (m->entries[i].occupied && !m->entries[i].deleted && m->entries[i].key) {
            fray_list_append(result, m->entries[i].key);
        }
    }
    return result;
}

void fray_map_state_release(void *state) {
    MapState *m = (MapState *)state;
    if (!m) return;
    for (size_t i = 0; i < m->cap; i++) {
        if (m->entries[i].occupied && !m->entries[i].deleted) {
            if (m->entries[i].key) fray_release(m->entries[i].key);
            if (m->entries[i].value) fray_release(m->entries[i].value);
        }
    }
    free(m->entries);
    free(m);
}
