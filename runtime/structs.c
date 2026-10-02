/**
 * fray runtime — structs (Phase 8).
 *
 * Structs are named-field mutable record types.  The type descriptor stores
 * field names and their count; instances hold a contiguous FrayValue array
 * of slot values, one per field, in declaration order.
 */

#include "runtime.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ── Internal representation ── */

typedef struct {
    char      **field_names;  /* array of field name strings (owned)  */
    FrayValue  *fields;       /* owned array of field values          */
    size_t      nfields;      /* number of fields                     */
} StructState;

/* ── Type info ── */

static void struct_trace(FrayObj *self) {
    StructState *st = (StructState *)self->as.st.state;
    if (!st) return;
    for (size_t i = 0; i < st->nfields; i++) {
        if (st->fields[i]) {
            fray_retain(st->fields[i]);
        }
    }
}

static void struct_finalize(FrayObj *self) {
    fray_struct_state_release(self->as.st.state);
    self->as.st.state = NULL;
}

const FrayTypeInfo fray_type_struct = {
    .name = "<struct>",
    .size = sizeof(FrayObj),
    .trace = struct_trace,
    .finalize = struct_finalize,
};

/* ── Constructors ── */

FrayValue fray_struct_new(const char *name, size_t nfields, const char **field_names) {
    /* Struct names are not stored in v1: the identity a compiled program can
     * read back is the field set, and an enum instance carries its enum's name
     * in the _enum field the emitter sets (compiler/codegen.fray, gen_enum_new;
     * bootstrap/codegen.py, _gen_enum_new) — that is what a qualified match arm
     * compares against. */
    (void)name;
    FrayValue obj = (FrayValue)calloc(1, sizeof(FrayObj));
    obj->tag = TAG_STRUCT;
    obj->gc_flags = 0;
    obj->generation = 0;
    obj->owner = -1;
    atomic_init(&obj->refcount, 1);
    obj->type = &fray_type_struct;

    StructState *st = calloc(1, sizeof(StructState));
    st->nfields = nfields;
    st->field_names = calloc(nfields, sizeof(char *));
    st->fields = calloc(nfields, sizeof(FrayValue));

    for (size_t i = 0; i < nfields; i++) {
        st->field_names[i] = strdup(field_names[i]);
        st->fields[i] = fray_none();  /* default: None */
    }

    obj->as.st.state = st;
    return obj;
}

/* Boxed constructor: fray_struct_new_boxed(name_val, fields_list).
 * name_val: TAG_STRING with the struct type name.
 * fields_list: TAG_LIST of TAG_STRING field names.
 * Returns a new struct instance with all fields defaulted to None. */
FrayValue fray_struct_new_boxed(FrayValue name_val, FrayValue fields_list) {
    (void)name_val;
    if (!fields_list || fields_list->tag != TAG_LIST) {
        return fray_struct_new("unknown", 0, NULL);
    }
    size_t n = fields_list->as.list.len;
    const char **names = n ? calloc(n, sizeof(char *)) : NULL;
    for (size_t i = 0; i < n; i++) {
        FrayValue fn = fields_list->as.list.elems[i];
        if (fn && fn->tag == TAG_STRING) {
            names[i] = fn->as.str.data; /* borrow — strdup inside fray_struct_new */
        } else {
            names[i] = "";
        }
    }
    FrayValue result = fray_struct_new(
        name_val && name_val->tag == TAG_STRING ? name_val->as.str.data : "anon",
        n, names);
    free(names);
    return result;
}

/* ── Field access ── */

static int find_field(StructState *st, const char *field) {
    for (size_t i = 0; i < st->nfields; i++) {
        if (strcmp(st->field_names[i], field) == 0) return (int)i;
    }
    return -1;
}

FrayValue fray_struct_field_get(FrayValue obj, const char *field) {
    if (!obj || obj->tag != TAG_STRUCT || !obj->as.st.state) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a struct");
        return fray_none();
    }
    StructState *st = (StructState *)obj->as.st.state;
    int idx = find_field(st, field);
    if (idx < 0) {
        fray_throw(FRAY_EXC_RUNTIME, "AttributeError: struct has no such field");
        return fray_none();
    }
    return st->fields[idx] ? fray_retained(st->fields[idx]) : fray_none();
}

void fray_struct_field_set(FrayValue obj, const char *field, FrayValue val) {
    if (!obj || obj->tag != TAG_STRUCT || !obj->as.st.state) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: not a struct");
        return;
    }
    StructState *st = (StructState *)obj->as.st.state;
    int idx = find_field(st, field);
    if (idx < 0) {
        fray_throw(FRAY_EXC_RUNTIME, "AttributeError: struct has no such field");
        return;
    }
    FrayValue old = st->fields[idx];
    st->fields[idx] = val;  /* ownership transferred */
    if (old) fray_release(old);
}

/* Boxed wrappers for compiled-code calling convention. */
FrayValue fray_struct_field_get_boxed(FrayValue obj, FrayValue field_name) {
    const char *name = "";
    if (field_name && field_name->tag == TAG_STRING) {
        name = field_name->as.str.data;
    }
    FrayValue result = fray_struct_field_get(obj, name);
    fray_release(field_name);
    return result;
}

void fray_struct_field_set_boxed(FrayValue obj, FrayValue field_name, FrayValue val) {
    const char *name = "";
    if (field_name && field_name->tag == TAG_STRING) {
        name = field_name->as.str.data;
    }
    fray_struct_field_set(obj, name, val);
    fray_release(field_name);
    /* val ownership transferred to struct */
}

/* Called by objects.c when a struct object dies (fray_free_object), and by
 * the type's finalizer hook. The state owns everything the instance holds:
 * the field values, the strdup'd field names and both arrays. */
void fray_struct_state_release(void *state) {
    StructState *st = (StructState *)state;
    if (!st) return;
    for (size_t i = 0; i < st->nfields; i++) {
        if (st->fields[i]) fray_release(st->fields[i]);
    }
    for (size_t i = 0; i < st->nfields; i++) {
        free(st->field_names[i]);
    }
    free(st->field_names);
    free(st->fields);
    free(st);
}
