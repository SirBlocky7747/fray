# Phase 8 — Compiler-required language features

These features are prerequisites for writing `frayc.fray`, the self-hosted compiler.
They extend the language with the data-structuring, interop, and text-manipulation
capabilities a compiler needs, while staying true to fray's Python-flavoured,
indentation-based, dynamically-typed design.

---

## 1. Structs

Structs are lightweight, mutable, named-field record types. They replace classes
for data modelling (tokens, AST nodes, compiler state).

### Syntax

```
struct Token:
    kind
    value
    line
    col
```

Fields are bare identifiers — no type annotations (fray is dynamically typed).
Default values are optional:

```
struct Token:
    kind = 0
    value = ""
    line = 1
    col = 0
```

### Construction

Positional or keyword arguments, matching the field declaration order:

```
t = Token(kind=5, value="hello", line=1, col=0)
t = Token(5, "hello", 1, 0)          # positional
t = Token(kind=5)                     # defaults: value="", line=1, col=0
```

### Field access

Dot notation for read and write:

```
print(t.kind)     # 5
t.kind = 10       # mutable
t.value = "world"
```

### Identity comparison

Two struct values are equal if they are the **same object** (identity, not deep
equality). Struct values are **not** compared by field contents.

```
a = Token(kind=1)
b = Token(kind=1)
print(a == b)    # False (different objects)
print(a == a)    # True  (same object)
```

### Runtime representation

Each struct instance is a heap-allocated object with a type descriptor that lists
field names and default values. Field access is O(n) by name lookup (adequate for
compiler-scale workloads; optimisable later).

New TAG: `TAG_STRUCT` in the runtime header. The struct object holds:
- Tag byte (TAG_STRUCT)
- GC flags, refcount, type descriptor pointer
- A contiguous array of `FrayValue` slots, one per field, in declaration order

The type descriptor is a new heap object that stores:
- Struct name (string)
- Ordered list of field names (strings)
- Default values (FrayValues)

### Grammar extension

```
(* New primary expressions *)
primary = ...
        | struct_literal
        | struct_def ;

struct_def      = "struct" IDENT ":" NEWLINE block ;
struct_literal  = IDENT "(" [ arg_list ] ")" ;

(* New target for assignments *)
target = ...
       | member_access ;
```

### Ownership

Struct values follow normal refcounting. When a struct is copied (assigned to
another variable), it is **aliased** (not deep-copied) — the same as lists.

---

## 2. Enums

Enums create sets of named integer constants. They are sugar over sequential
integer assignments.

### Syntax

```
enum TokenKind:
    IDENT
    INT_LIT
    FLOAT_LIT
    STRING_LIT
    PLUS
    MINUS
    STAR
    SLASH
    LPAREN
    RPAREN
    EOF
```

Auto-assigned values start at 0 and increment by 1.

Explicit values:

```
enum TokenKind:
    IDENT = 0
    INT_LIT = 1
    STRING_LIT = 2
    PLUS = 10
```

### Semantics

Each member becomes a **module-level constant** (integer). After the enum
declaration, `TokenKind.IDENT` evaluates to `0` (or the explicit value).

Enums do **not** create a new type — they are purely a declaration shorthand.
An enum value is just an `int` at runtime.

### Grammar extension

```
statement = ...
          | enum_def ;

enum_def = "enum" IDENT ":" NEWLINE block ;

(* Inside enum block — restricted form *)
enum_member = IDENT [ "=" INT_LITERAL ] ;
```

### Runtime

No new runtime support needed — the compiler lowers enum declarations to a
sequence of `const` assignments during semantic analysis.

---

## 3. Foreign Function Interface (FFI)

FFI allows fray code to call C functions from the runtime or system libraries.
This is essential for the self-hosted compiler, which needs process execution
(`system`, `popen`), fine-grained file I/O (`fopen`/`fread`/`fwrite`/`fseek`),
and memory operations.

### Syntax

```
extern def fopen(path: string, mode: string) -> pointer
extern def fclose(fp: pointer) -> int
extern def fread(buf: pointer, size: int, count: int, fp: pointer) -> int
extern def fwrite(buf: pointer, size: int, count: int, fp: pointer) -> int
extern def fseek(fp: pointer, offset: int, origin: int) -> int
extern def ftell(fp: pointer) -> int
extern def system(cmd: string) -> int
extern def popen(cmd: string, mode: string) -> pointer
extern def pclose(fp: pointer) -> int
extern def fgets(buf: pointer, size: int, fp: pointer) -> pointer
extern def malloc(size: int) -> pointer
extern def free(ptr: pointer)
extern def realloc(ptr: pointer, size: int) -> pointer
extern def strlen(s: pointer) -> int
extern def memcpy(dst: pointer, src: pointer, n: int) -> pointer
```

### Semantics

- `extern def` declares a C function. The compiler generates a declaration in
  the LLVM IR (`declare`), and the linker resolves it at link time.
- The `-> type` return annotation is optional; if omitted, the function returns
  `None` (void).
- Parameter types are **informational only** (for documentation and future type
  checking) — at runtime, all values are boxed `FrayValue`s passed as-is.
- The runtime must pre-register the supported C functions so that the compiler
  can emit calls to them. For Phase 8, the set of supported extern functions is
  **fixed** (not user-extensible).

### Grammar extension

```
statement = ...
          | extern_def ;

extern_def = "extern" "def" IDENT "(" [ extern_param_list ] ")" [ "->" IDENT ] ;

extern_param_list = extern_param { "," extern_param } ;
extern_param      = IDENT [ ":" IDENT ] ;
```

### Codegen

The codegen emits LLVM `declare` directives for each `extern def`, then generates
normal `call` instructions at usage sites. The linker resolves the symbols.

For Phase 8, only the following C functions are supported (all available on
every platform fray targets; v0.1.0 is released and gated for Linux x86-64):

| Function | C signature | fray usage |
|---|---|---|
| `fopen` | `FILE*(const char*, const char*)` | Open a file |
| `fclose` | `int(FILE*)` | Close a file |
| `fread` | `size_t(void*, size_t, size_t, FILE*)` | Read bytes |
| `fwrite` | `size_t(const void*, size, size_t, FILE*)` | Write bytes |
| `fseek` | `int(FILE*, long, int)` | Seek in file |
| `ftell` | `long(FILE*)` | Get position |
| `fgets` | `char*(char*, int, FILE*)` | Read a line |
| `system` | `int(const char*)` | Run shell command |
| `malloc` | `void*(size_t)` | Allocate memory |
| `free` | `void(void*)` | Free memory |
| `realloc` | `void*(void*, size_t)` | Reallocate memory |
| `strlen` | `size_t(const char*)` | String length |
| `memcpy` | `void*(void*, const void*, size_t)` | Copy memory |
| `fprintf` | `int(FILE*, const char*, ...)` | Printf to file |
| `sprintf` | `int(char*, const char*, ...)` | Printf to buffer |

### Runtime changes

A new `ffi.c` translation unit provides:
- `fray_ffi_register(name, fn_ptr)` — called during startup to register
  supported functions
- `fray_ffi_call(name, argc, argv)` — dispatches to the registered function

The registration table is populated in `runtime.c` init with the functions
listed above. The codegen emits calls through a `fray_ffi_call` shim, or
(directly) emits LLVM `declare` + `call` for known functions.

**Direct emit** (preferred for Phase 8): the codegen knows the C signatures
and emits LLVM IR `declare`/`call` directly. No runtime FFI dispatch needed.

---

## 4. Richer string operations

The compiler needs character-level and substring operations for lexing.

### New builtins

| Builtin | Signature | Notes |
|---|---|---|
| `ord(c)` | `string → int` | Unicode code point of single character |
| `chr(n)` | `int → string` | Single character from code point |
| `substring(s, start, end)` | `string, int, int → string` | Extract `s[start:end]` |
| `s.find(sub)` | `string → int` | Index of first occurrence, or `-1` |
| `s.startswith(prefix)` | `string → bool` | Test prefix |
| `s.endswith(suffix)` | `string → bool` | Test suffix |
| `s.split(delim)` | `string → list` | Split by delimiter |
| `s.strip()` | `string → string` | Remove leading/trailing whitespace |
| `s.replace(old, new)` | `string, string, string → string` | Replace all occurrences |

### String slicing (alternative to substring)

Extend the `[]` operator on strings to accept slices:

```
s = "hello"
s[1:3]      # "el"
s[:3]       # "hel"
s[2:]       # "llo"
s[::2]      # "hlo"  (step — future)
```

**Implementation note**: String slicing creates a **new** string (strings are
immutable). The runtime allocates a new UTF-8 buffer and copies the substring.

### Grammar extension for slicing

```
indexed = primary "[" expr [ ":" [ expr ] [ ":" expr ] ] "]" ;
       | primary "[" expr "]" ;
```

The slice form `a[i:j]` is syntactic sugar for `substring(a, i, j)` when
applied to a string. For lists, it returns a new list (future).

---

## 5. Hash maps (dictionaries)

The compiler's semantic analysis needs hash maps for symbol tables. Maps are
also generally useful and were already noted as a planned feature.

### Syntax

```
# Empty map
m = {}

# Map literal
m = {"name": "fray", "version": 1}

# Access
print(m["name"])       # "fray"
m["author"] = "team"   # insert/update
del m["author"]        # delete (future)

# Membership
"name" in m            # True

# Length
len(m)                 # 1
```

### Semantics

- Keys can be any immutable type: `int`, `float`, `string`, `bool`, `tuple`.
- Values can be any type.
- `m[key]` raises `KeyError` if key is not found.
- `m[key] = value` inserts or updates.
- `del m[key]` removes the entry; raises `KeyError` if not found.
- Iteration: `for k in m:` iterates over keys (order is insertion order, like
  Python 3.7+).

### Runtime representation

New TAG: `TAG_MAP`. Internal structure:
- Open-addressing hash table with linear probing
- Power-of-2 capacity, 75% load factor trigger for resize
- Key-value pairs stored as `FrayValue` pairs
- Insertion-order maintained via a parallel key-order list

### Grammar extension

```
primary = ...
        | map_literal ;

map_literal = "{" [ map_entry { "," map_entry } [ "," ] ] "}" ;
map_entry   = expr ":" expr ;
```

### New operations

| Operation | Syntax | Notes |
|---|---|---|
| Map literal | `{"a": 1}` | Creates a map |
| Key access | `m[key]` | Get value or KeyError |
| Key assign | `m[key] = val` | Insert or update |
| Key delete | `del m[key]` | Remove entry |
| Key test | `key in m` | Membership |
| Length | `len(m)` | Number of entries |
| Keys iteration | `for k in m:` | Iterate keys |
| `.keys()` | `m.keys()` | List of keys (future) |
| `.values()` | `m.values()` | List of values (future) |
| `.items()` | `m.items()` | List of (key, value) tuples (future) |

---

## 6. Process execution (convenience builtins)

For the compiler driver, we need to invoke `gcc`/`clang` as subprocesses.
While `system()` via FFI covers simple cases, we need richer process control.

### New builtins

| Builtin | Signature | Notes |
|---|---|---|
| `exec(cmd)` | `string → string` | Run command, return stdout |
| `exec(cmd, stdin)` | `string, string → string` | Run with stdin input |
| `exit(code)` | `int → never` | Exit with code |

`exec()` is implemented as `popen()` + `fgets()` loop in the runtime. It runs
a shell command, captures stdout, and returns it as a string.

---

## 7. Implementation order

Features should be implemented in dependency order:

| # | Feature | Depends on | Priority |
|---|---|---|---|
| 1 | String slicing + `ord`/`chr` | nothing | 🔴 critical |
| 2 | Structs | nothing | 🔴 critical |
| 3 | Enums | nothing | 🔴 critical |
| 4 | Hash maps | nothing | 🔴 critical |
| 5 | FFI (extern def) | nothing | 🔴 critical |
| 6 | `exec()` / `exit()` builtins | FFI | 🟡 high |
| 7 | `find` / `split` / `replace` etc. | string slicing | 🟡 high |
| 8 | `del` statement | maps | 🟢 medium |
| 9 | `in` operator for maps | maps | 🟢 medium |

Features 1–5 are **hard blockers** for writing the compiler in fray.
Features 6–9 make the compiler more ergonomic but could be deferred.

---

## 8. Example: lexer skeleton in fray (post-Phase 8 features)

This sketch shows how the new features compose to write a lexer:

```
import C

# --- Enums ---
enum TokenKind:
    IDENT
    INT_LIT
    STRING_LIT
    PLUS
    MINUS
    STAR
    SLASH
    LPAREN
    RPAREN
    NEWLINE
    EOF

# --- Structs ---
struct Token:
    kind = 0
    value = ""
    line = 1
    col = 0

struct Lexer:
    source = ""
    pos = 0
    line = 1
    col = 1

# --- FFI ---
extern def fopen(path: string, mode: string) -> pointer
extern def fclose(fp: pointer) -> int
extern def fread(buf: pointer, size: int, count: int, fp: pointer) -> int
extern def fseek(fp: pointer, offset: int, origin: int) -> int
extern def ftell(fp: pointer) -> int
extern def malloc(size: int) -> pointer
extern def free(ptr: pointer)
extern def strlen(s: pointer) -> int

# --- Lexer functions ---
def read_file(path):
    fp = fopen(path, "rb")
    fseek(fp, 0, 2)
    size = ftell(fp)
    fseek(fp, 0, 0)
    buf = malloc(size + 1)
    fread(buf, 1, size, fp)
    # null-terminate (C string)
    buf_ptr = buf + size  # pointer arithmetic via FFI
    fclose(fp)
    return buf  # caller responsible for free

def lexer_peek(lex):
    if lex.pos >= len(lex.source):
        return ""
    return lex.source[lex.pos]

def lexer_advance(lex):
    ch = lex.source[lex.pos]
    lex.pos += 1
    if ch == "\n":
        lex.line += 1
        lex.col = 1
    else:
        lex.col += 1
    return ch

def lexer_next_token(lex):
    # skip whitespace
    while lexer_peek(lex) == " " or lexer_peek(lex) == "\t":
        lexer_advance(lex)

    ch = lexer_peek(lex)

    if ch == "":
        return Token(kind=TokenKind.EOF, line=lex.line, col=lex.col)

    if ch == "\n":
        lexer_advance(lex)
        return Token(kind=TokenKind.NEWLINE, value="\n", line=lex.line, col=lex.col)

    if ch.isdigit():
        start = lex.pos
        while lexer_peek(lex).isdigit():
            lexer_advance(lex)
        return Token(kind=TokenKind.INT_LIT, value=lex.source[start:lex.pos], line=lex.line, col=lex.col)

    # ... etc
    return Token(kind=TokenKind.IDENT, value=ch, line=lex.line, col=lex.col)

# --- Main ---
def main():
    path = "test.fray"
    source = read_file(path)
    lex = Lexer(source=source)
    while lexer_peek(lex) != "":
        tok = lexer_next_token(lex)
        print(tok.kind)
        print(tok.value)
    free(source)

main()
```

This sketch is intentionally rough — it demonstrates the feature composition,
not production-quality compiler code. The actual `frayc.fray` will be more
structured.

---

## 9. Grammar additions (complete)

### New keywords

```
"struct" | "enum" | "extern" | "del"
```

### New statement forms

```
statement = simple_stmt NEWLINE
          | compound_stmt
          | struct_def
          | enum_def
          | extern_def
          | del_stmt ;

struct_def    = "struct" IDENT ":" NEWLINE block ;
enum_def      = "enum" IDENT ":" NEWLINE block ;
extern_def    = "extern" "def" IDENT "(" [ extern_param_list ] ")" [ "->" IDENT ] ;
extern_param_list = extern_param { "," extern_param } ;
extern_param  = IDENT [ ":" IDENT ] ;
del_stmt      = "del" target ;
```

### Modified expressions

```
primary = literal
        | IDENT
        | call
        | member_access
        | indexed
        | struct_literal
        | "(" expr ")"
        | list_literal
        | set_literal
        | tuple_literal
        | map_literal ;

map_literal  = "{" [ map_entry { "," map_entry } [ "," ] ] "}" ;
map_entry    = expr ":" expr ;

struct_literal = IDENT "(" [ arg_list ] ")" ;

(* Slice syntax extends indexed *)
indexed = primary "[" expr [ ":" [ expr ] ] "]" ;
```

### Modified targets

```
target = IDENT
       | indexed
       | member_access ;

del_stmt = "del" target ;
```
