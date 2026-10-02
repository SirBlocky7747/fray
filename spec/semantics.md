# fray — Semantics

This document defines the runtime behavior of fray, complementing `grammar.md`.

---

## 1. Scoping

- **Function scope**: variables created inside a function are local to that function.
- **Block scope**: variables created inside `if`/`elif`/`else`/`for`/`while`/`try`/`except`/`finally` blocks are local to that block *unless* they were already defined in an enclosing scope.
- **Module scope**: top-level variables are visible to the rest of the module.
- **No block-level scope leaking**: a variable assigned inside a `for` loop does not leak to the enclosing function scope — unlike Python. This is intentional to avoid accidental shadowing bugs.

```
for i in range(5):
    x = i
print(x)   # Error: 'x' is not defined (did not leak from loop)
```

- **Name resolution**: inner scopes shadow outer scopes. Accessing an outer name from within a function requires no special syntax — just use it (closure-like semantics).

```
y = 10
def f():
    print(y)   # OK: reads outer 'y'
f()
```

- **Forward references**: a function may call another function defined later in the same module (name resolution happens at call time, not definition time).

---

## 2. `const` declarations

- `const x = expr` creates a **compile-time constant binding**.
- Reassignment to a `const` variable is a **compile-time error**: `const x = 1; x = 2` fails before the program runs.
- `const` is valid only at module scope or function scope — not inside loops or blocks.
- Constants are **not** deeply immutable. A `const` list binding prevents reassignment of the variable, but the list's contents can still be mutated via `append`/`depend`/index-assign.

```
const x = [1, 2, 3]
x = [4, 5]       # Error: cannot reassign const
x.append(4)       # OK: mutates the list, not the binding
```

---

## 3. Truthiness

Every value in fray has a boolean interpretation for use in `if`/`elif`/`while`:

| Value | Falsy | Truthy |
|---|---|---|
| `int` | `0` | Non-zero |
| `float` | `0.0` | Non-zero |
| `string` | `""` (empty) | Non-empty |
| `bool` | `False` | `True` |
| `list` | `[]` (empty) | Non-empty |
| `set` | `{}` (empty) | Non-empty |
| `tuple` | `()` (empty) | Non-empty |
| `None` | `None` | — |

- `and` / `or` / `not` operate on truthiness, not identity.
- `xor` / `xnor` operate on boolean values (both operands are coerced to bool first).

---

## 4. Numeric model

### Integers

- **Default**: 64-bit signed integer (`i64`).
- **Auto-promotion**: when an operation would overflow 64 bits, the value is automatically promoted to **arbitrary-precision bignum** (no precision loss, no wrap-around).
- Bignum promotion is transparent — the programmer sees no type change.
- `0` is both falsy and equal to `0.0`.

### Floats

- **IEEE 754 double-precision** (`f64`).
- `NaN` and `Infinity` are representable.
- `NaN != NaN` is `True` (IEEE 754 behavior).
- `0.0` is falsy; `-0.0` equals `0.0`.

### Mixed arithmetic

- `int + int → int` (may promote to bignum)
- `int + float → float`
- `float + float → float`

### Division

- `/` is **true division**: `1 / 2` → `0.5` (always returns float)
- `//` is **floor division**: `7 // 2` → `3` (truncates toward negative infinity)
- `%` is **modulo**: `7 % 2` → `1`, `-7 % 2` → `1` (Euclidean/modulo semantics)
- `^` is **power**: `2 ^ 3` → `8`

---

## 5. Strings

- **UTF-8 encoded**, immutable.
- `str[i]` returns a single-character string.
- `str + str` concatenates.
- `str * int` repeats.
- `len(str)` returns the number of characters (not bytes).
- String literals use double quotes: `"hello"`.
- Escape sequences: `\n`, `\t`, `\\`, `\"`, `\0` (NUL).

---

## 6. Containers

### Lists

- Mutable, ordered, indexed from 0.
- `[1, 2, 3]` — heterogeneous elements allowed.
- `x[i]` — index access (0-based).
- `x[i] = v` — index assignment.
- `x.append(v)` — add to end.
- `x.depend` — remove and return last element (like `pop()`).
- `len(x)` — number of elements.
- Negative indexing: `x[-1]` returns the last element.
- Slicing: `x[start:stop]` returns a new list (future — not in fray.txt but expected).

### Sets

- Mutable, unordered, deduplicated.
- `{1, 2, 3}` — duplicates are silently removed.
- `x.append(v)` — add element (like Python's `add`).
- `x.depend` — remove an arbitrary element (implementation-defined order).
- `len(x)` — number of unique elements.
- Membership test: `v in x` → bool.

### Tuples

- Immutable, ordered.
- `(1, 2, 3)` — once created, cannot be modified.
- `x[i]` — read-only index access.
- `x[i] = v` — **error** (immutable).
- `len(x)` — number of elements.
- Single-element tuple: `(1,)` — trailing comma required (same as Python).
- Empty tuple: `()`.

---

## 7. Error handling

### Exception hierarchy

```
Exception
├── TypeError          (mismatched types: int + "string")
├── ValueError         (e.g., int("abc"))
├── IndexError         (list index out of range)
├── KeyError           (dict key not found)
├── NameError          (undefined variable)
├── RuntimeError       (general runtime errors)
├── OverflowError      (should not happen — bignum auto-promotes)
├── ZeroDivisionError  (division by zero)
└── ImportError        (module not found)
```

### `try` / `except` / `finally`

- `except TypeError:` catches only `TypeError`. Clauses are tested in order and the
  first match runs; a later clause never sees an exception an earlier one took.
- `except:` (bare), `except Exception:` and `except RuntimeError:` all catch the same
  set: `TypeError`, `ValueError`, `IndexError`, `NameError`, `ZeroDivisionError`.
  Use them sparingly. `KeyError`, `ImportError` and the runtime errors around struct
  fields are outside that set in *both* engines — a `KeyError` passes a bare `except:`
  and stops the program (see plan.md, divergence 10).
- `except KeyError:` for an exception no engine raises is not an error: the clause
  simply never matches, and the clauses after it are still tried.
- `finally:` always runs, whether or not an exception occurred, and does not swallow
  one: an exception no clause matched is re-raised once the `finally` body has run.
- An exception no clause matches propagates out of the `try` — through its `finally` if
  it has one — and the enclosing `try` gets first refusal, so an inner `try` that cannot
  handle an exception leaves it to the outer one.
- An exception raised while an `except` or `finally` body is running replaces the
  pending one and propagates the same way.
- Unhandled exceptions print a message to stderr and exit with code 1.
- Compiled code polls for the pending exception after each statement, not inside an
  expression, so a throw in the middle of a statement that prints still prints for that
  statement before the handler runs (plan.md, divergence 9). Assigning the result first
  avoids the placeholder output; the oracle does not produce it at all.

---

## 8. Functions

- Defined with `def name(params):` followed by an indented block.
- `return expr` exits the function with a value.
- `return` without a value (or falling off the end) returns `None`.
- Functions are first-class values: a bare function name in value position is a function
  object — assignable to a variable, passable as an argument, storable in a struct field —
  and it prints as `<function name>`.
- A value that holds a function is called by naming it where a callee goes: `g(1, 2)` when
  `g` is a variable or parameter, `holder.fn(1)` through a field, `xs[0](1, 2)` through an
  element. Such a callee is only
  known at run time, so its two checks happen there: the value must be callable and the
  argument count must match the callee's declaration, otherwise `TypeError`
  (`'h' takes 1 argument(s) but 2 given`, `'5' is not callable`). The self-hosted frontend
  does not implement this yet: it reports `CODEGEN ERROR: unknown name 'add'` rather than
  mis-compiling the program.
- Default arguments and keyword arguments are **not** in v1 (future extension).

---

## 9. Control flow

### `for` loops

- `for i in expr:` iterates over the expression.
- `range(n)`: integers 0 to n-1.
- `range(start, stop)`: integers start to stop-1.
- Iterates over lists, sets, tuples, and strings.
- `break` exits the loop; `continue` skips to the next iteration.

### `while` loops

- `while expr:` repeats as long as the expression is truthy.
- `break` / `continue` supported.

### `if` / `elif` / `else`

- Evaluates conditions top-to-bottom; executes the first truthy branch.
- `else` is optional.
- `elif` chains can be arbitrarily long.

---

## 10. Imports

- `import module` loads a module from the standard library or current directory.
- `module.name` accesses module-level functions and constants.
- `import package.module` loads a module from a package subdirectory:
  `package/module.fray`. A module path may be dotted to any depth (`a.b.c` is
  `a/b/c.fray`), and qualified references use the full path (`a.b.f()`,
  `a.b.CONST`).
- `from module import name, other` binds those names directly instead of the
  module; the module path may be dotted (`from package.module import name`).

### Packages and initializers

A module path resolves to a file under the program's directory. A directory
that holds an initializer is a **package**, and the initializer provides the
package's own names:

| module | file |
|---|---|
| `a.b.c` | `a/b/c.fray`, or |
| `a.b.c` | `a/b/c/__init__.fray` |

A plain module file wins when both exist. The initializer is an ordinary
module — its definitions are the package's definitions — and it can **split
the package across files** and **re-export** names by importing them:

```
# pkg/__init__.fray
from .util import twice     # re-exported: `from pkg import twice` works
from . import shapes        # binds the sibling module under its local name

def describe(n):
    return str(twice(n)) + ":" + str(shapes.sides())
```

### Relative imports

A module path may begin with dots, which make it relative to the importing
module's **package**. One dot is the current package; each extra dot goes up
one level:

| in module | file | `.util` is | `..util` is |
|---|---|---|---|
| `pkg` | `pkg/__init__.fray` | `pkg.util` | `util` |
| `pkg.sub` | `pkg/sub.fray` | `pkg.util` | `util` |
| `pkg.sub` | `pkg/sub/__init__.fray` | `pkg.sub.util` | `pkg.util` |

The main program has no package, so a relative import there is an error, as is
one that climbs past the top level. `from . import name` binds either a name
the package exports or one of its submodules.

### Module search order

1. Standard library (`stdlib/`)
2. Current directory, and its subdirectories as dotted packages
3. Package registry (future)

---

## 11. Concurrency (Phase 6+)

- **Threads**: real OS threads, shared memory, no global interpreter lock.
- **Coroutines** (Phase 7): lightweight, scheduled onto OS threads; `async def` / `await` syntax.
- **Data races are errors**: ThreadSanitizer will catch them in CI.
- **Synchronization primitives**: locks, atomics, channels (details TBD).

---

## 12. Numeric constants

| Constant | Value | Notes |
|---|---|---|
| `pi` | 3.14159265358979... | `float` |
| `e` | 2.71828182845904... | `float` |

These are module-level builtins, not keywords — accessible without import.
