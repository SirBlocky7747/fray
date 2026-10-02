# Fray — Syntax Layout

> **Status:** Working syntax layout / language-design reference
>
> **Language:** Fray
>
> **File extension:** `.fray`
>
> This document maps out the syntax of Fray from the smallest lexical pieces to full programs. It is intentionally broader than the original `fray.txt`: syntax that is already established is marked **LOCKED**, while syntax that was not yet finalized in the project notes is marked **PROPOSED**.

---

## 1. Syntax philosophy

Fray is intended to feel familiar to Python users while being a compiled, dynamically typed, GIL-free language with native parallelism and first-class mathematical / ML capabilities.

Core syntax principles:

- Significant indentation.
- Four spaces per indentation level.
- No braces required for normal control-flow blocks.
- `#` begins a comment and comments may appear on their own line or after code.
- Variables are dynamically typed by default.
- Static type information may be added where useful, especially for compiler specialization.
- Functions use `def`.
- Containers use Python-like literal syntax.
- The language keeps several deliberately non-Python choices, such as `^` for exponentiation and named boolean operators such as `xor` and `xnor`.

---

## 2. Source-file layout

A normal Fray source file can contain:

```text
comments
imports
constants / globals
function definitions
other top-level statements
```

Example:

```fray
# main.fray
import random

const answer = 42


def greet(name):
    print("Hello, " + name)


greet("Fray")
```

Top-level executable statements are allowed in the core language.

---

# 3. Comments

## 3.1 Line comments — LOCKED

```fray
# this is a comment
x = 1
x = 2  # this is also a comment
```

A comment continues to the end of the physical line.

---

# 4. Whitespace and indentation

## 4.1 Indentation — LOCKED

Blocks are indentation-based.

```fray
if x == 1:
    print("one")
    x += 1
```

The current project specification uses **4 spaces per indentation level**.

Tabs should not be used for block indentation.

## 4.2 Blank lines

Blank lines are ignored except where they occur inside a lexical construct such as a future multiline string literal, if one is added.

---

# 5. Identifiers

## 5.1 Identifier shape — LOCKED / inferred

Identifiers are names for variables, functions, modules, and other language objects.

Recommended grammar:

```ebnf
identifier = letter , { letter | digit | "_" } ;
letter     = "A" … "Z" | "a" … "z" | "_" ;
digit      = "0" … "9" ;
```

Examples:

```fray
x
message
my_variable
value2
_tensor
```

Identifiers are case-sensitive.

Examples of distinct names:

```fray
x
X
hello
Hello
```

---

# 6. Keywords

The currently established keyword/operator vocabulary is:

```text
and
or
xor
xnor
not
band
bor
bxor
bnot
bxnor

def
return
if
elif
else
for
in
while
try
except
finally
const
import

True
False
```

Additional keywords may be introduced later for advanced features. Any future keyword should be reserved only when the corresponding feature is finalized.

---

# 7. Literals

## 7.1 Integer literals — LOCKED

```fray
0
1
42
-10
1000000
```

The planned numeric model uses 64-bit integers as the normal fast representation and promotes to a larger integer representation on overflow.

Negative numbers are syntactically an expression using unary negation rather than a fundamentally different integer-literal token.

## 7.2 Floating-point literals — LOCKED

```fray
1.0
3.14
0.5
10.25
```

## 7.3 Boolean literals — LOCKED

```fray
True
False
```

## 7.4 String literals — LOCKED

```fray
"Hello"
"Hello, world!"
"123"
```

Fray strings are UTF-8 runtime strings.

String concatenation uses `+`:

```fray
name = "Michael"
message = "Hello, " + name
```

> Multiline strings, escape-sequence rules, raw strings, and interpolation syntax were not finalized in the original syntax reference. Those are left **PROPOSED** below rather than silently becoming part of the locked language.

## 7.5 Proposed string escapes — PROPOSED

```fray
"line 1\nline 2"
"tab\tvalue"
"quote: \"hello\""
```

## 7.6 Proposed string interpolation — PROPOSED

Possible future syntax:

```fray
name = "Fray"
print(f"Hello, {name}!")
```

This is a proposal, not a locked language feature.

---

# 8. Variables

## 8.1 Assignment — LOCKED

```fray
x = 1
x = 2
x = "Hello"
x = 1.1
```

A variable may change type because Fray is dynamically typed by default.

```fray
x = 1
x = "now a string"
x = [1, 2, 3]
```

## 8.2 Constants — LOCKED

```fray
const x = 1
const name = "Fray"
```

A `const` binding cannot be reassigned after initialization.

The exact interaction between `const` and mutation of a referenced mutable object should be defined semantically as part of the type/ownership specification.

---

# 9. Assignment operators

The currently established assignment operators are:

```fray
=
+=
-=
*=
/=
```

Examples:

```fray
x = 10
x += 2
x -= 3
x *= 4
x /= 2
```

Additional compound operators may be added later.

---

# 10. Expressions

Fray expressions can contain:

- literals
- variables
- function calls
- indexing
- attribute/module access
- arithmetic
- comparisons
- boolean logic
- bitwise logic
- grouping with parentheses

Examples:

```fray
x
1 + 2
x * 4
x == 10
sqrt(x)
random.randomInt(0, 10)
x[0]
x[0].append(1)
```

---

# 11. Parentheses and calls

## 11.1 Grouping

```fray
x = (1 + 2) * 3
```

## 11.2 Function calls — LOCKED

```fray
print(1)
print("Hello")
x = int("42")
```

Arguments are comma-separated:

```fray
foo(a, b, c)
```

An empty argument list is valid:

```fray
foo()
```

## 11.3 Nested calls

```fray
print(str(int("42")))
```

---

# 12. Attribute / member access

The `.` operator is already used for module access and container methods.

Examples:

```fray
random.randomInt(0, 1)
x.append(1)
x.depend
```

General form:

```ebnf
member-access = expression , "." , identifier ;
```

Whether arbitrary user-defined object attributes exist in the same way is dependent on the future object/class system.

---

# 13. Indexing

## 13.1 Indexing — LOCKED

```fray
x = [1, 2, 3, 4]
print(x[0])
```

## 13.2 Index assignment — LOCKED

```fray
x = [1, 2, 3, 4]
x[0] = 99
```

## 13.3 Nested indexing / method calls — LOCKED

```fray
x = [[1, 2], [3, 4]]
x[0].append(5)
```

General form:

```ebnf
indexing = expression , "[" , expression , "]" ;
```

Slices were not defined in the original syntax reference and remain **PROPOSED**.

---

# 14. Operators

## 14.1 Arithmetic operators — LOCKED

```text
+
-
*
/
//
%
^
```

Examples:

```fray
1 + 1
1 - 1
2 * 3
10 / 2
10 // 3
10 % 3
2 ^ 3
```

### Fray-specific power operator

`^` means exponentiation / power.

```fray
x ^ (2)
```

This deliberately differs from Python, where `^` is bitwise XOR.

## 14.2 Comparison operators — LOCKED

```text
==
!=
>
<
>=
<=
```

Examples:

```fray
x == 1
x != 1
x > 1
x < 1
x >= 1
x <= 1
```

Comparison expressions evaluate to booleans.

## 14.3 Logical operators — LOCKED

```text
and
or
xor
xnor
not
```

Examples:

```fray
x = True
y = False

a = x and y
a = x or y
a = x xor y
a = x xnor y
a = not x
```

The original syntax reference defines the expected truth-table behavior for these operators.

## 14.4 Bitwise operators — LOCKED in the reference

```text
band
bor
bxor
bnot
bxnor
```

Examples:

```fray
a = x band y
a = x bor y
a = x bxor y
a = bnot x
a = x bxnor y
```

The exact width / signedness behavior should be pinned down in the numeric-semantics specification.

---

# 15. Suggested operator precedence

The exact precedence table should be treated as a compiler-spec item. A sensible layout consistent with the existing syntax is:

```text
highest

member access / indexing / call
unary operators
^
* / // %
+ -
comparisons
bitwise operators
not
and
xor / xnor
or
assignment

lowest
```

> **PROPOSED:** The final precedence order must be explicitly frozen in `spec/grammar.md` rather than inferred from implementation behavior.

---

# 16. Collections

Fray's original syntax reference contains three core collection forms.

## 16.1 Lists — LOCKED

Mutable, ordered collection.

```fray
x = [1, 2, 1, 4]
```

Access:

```fray
x[0]
```

Mutation:

```fray
x[0] = 2
```

Append:

```fray
x.append(1)
```

Remove the last element using the existing `depend` spelling:

```fray
x.depend
```

Nested mutation:

```fray
x[0].append(1)
x[0].depend
```

> `depend` is currently documented as the project's spelling for removing the last element. The name is unusual but is preserved here because it is part of the existing reference syntax.

Two lists concatenate with `+`, producing a new list and leaving both operands
untouched. The result keeps its own reference to every element, so the operands
may be dropped immediately afterwards:

```fray
a = [1, 2]
b = [3, 4]
c = a + b
a = []
b = []
print(c)
```

An accumulator grows with the same operator. The right operand is usually a
freshly built list, and it may be discarded as soon as the store happens:

```fray
xs = []
for i in range(5):
    xs = xs + [i * i]
print(xs)
```

## 16.2 Sets — LOCKED

The original `fray.txt` uses braces for sets:

```fray
x = {1, 2, 3, 4}
```

Sets are unordered and deduplicated.

Mutation uses the same container method vocabulary:

```fray
x.append(5)
x.depend
```

## 16.3 Tuples — LOCKED

Immutable ordered collection:

```fray
x = (1, 2, 1, 4)
```

Length:

```fray
len(x)
```

Indexing follows the same general indexing syntax:

```fray
x[0]
```

## 16.4 Dictionaries / maps — PROPOSED

The project plan suggested, but did not lock, Python-like map syntax:

```fray
x = {
    "name": "Fray",
    "version": 1
}
```

Lookup:

```fray
x["name"]
```

Assignment:

```fray
x["version"] = 2
```

This must remain separate from set literals through parser rules.

## 16.5 Collection nesting

Collections may be nested:

```fray
x = [[1, 2], [3, 4]]
```

```fray
x = [{1, 2}, {3, 4}]
```

```fray
x = ([1, 2], [3, 4])
```

---

# 17. Built-in functions

The current core builtin set from the project plan is:

```text
input
inputInt
inputStr
inputFloat
len
min
max
sum
abs
sqrt
isqrt
round
int
float
str
mean
med
mode
mid
print
```

## 17.1 Input

```fray
input()
inputInt()
inputStr()
inputFloat()
```

Prompt text can be passed where supported:

```fray
inputStr("Insert your name: ")
```

## 17.2 Printing

```fray
print(1)
print("Hello")
print(x)
print(x[0])
```

`print` takes any number of arguments and writes them separated by a single
space, followed by a newline. `print()` with no arguments is a bare newline.

```fray
print("Hello,", "Ada")     # Hello, Ada
print()                    # a blank line
```

## 17.3 Collection / numeric helpers

```fray
len(x)
min(1, 2, 3)
max(1, 2, 3)
sum([1, 2, 3])
abs(-1)
```

## 17.4 Mathematical helpers

```fray
sqrt(4)
isqrt(4)
round(1.1)
```

## 17.5 Type conversion

```fray
int("1")
float(1)
float("1")
float("1.0")
str(1)
str(1.0)
```

## 17.6 Statistics

```fray
mean(x)
med(x)
mode(x)
mid(x)
```

Current project definitions:

- `mean(x)` — arithmetic mean.
- `med(x)` — statistical median.
- `mode(x)` — most frequent value(s), semantics to be made explicit in the standard library specification.
- `mid(x)` — positional middle element of a list as-is.

## 17.7 Mathematical constants

```fray
pi
e
```

`pi` is π and `e` is Euler's number.

---

# 18. Functions

## 18.1 Definition — LOCKED

```fray
def x():
    y = 1 + 1
    return y
```

## 18.2 Parameters — LOCKED

```fray
def add(a, b):
    y = a + b
    return y
```

## 18.3 Calling a function — LOCKED

```fray
print(add(1, 1))
```

## 18.4 Returning a value — LOCKED

```fray
def square(x):
    return x ^ 2
```

## 18.5 Early return

```fray
def check(x):
    if x == 0:
        return 0
    return 1
```

## 18.6 Recursion

Recursion is supported by the implementation plan.

```fray
def factorial(x):
    if x <= 1:
        return 1
    return x * factorial(x - 1)
```

## 18.7 Static type annotations — PROPOSED

The language roadmap describes optional static typing, but the original syntax reference does not lock its spelling.

A possible syntax is:

```fray
def add(a: int, b: int) -> int:
    return a + b
```

Possible variable annotations:

```fray
x: int = 10
name: str = "Fray"
```

These are proposals until the type-annotation grammar is finalized.

---

# 19. Conditional statements

## 19.1 `if` — LOCKED

```fray
if x == 1:
    x += 1
```

## 19.2 `elif` — LOCKED

```fray
if x == 1:
    x += 1
elif x == 2:
    x -= 1
```

## 19.3 `else` — LOCKED

```fray
if x == 1:
    x += 1
elif x == 2:
    x -= 1
else:
    x = 0
```

Nested conditions:

```fray
if x > 0:
    if x < 10:
        print("single digit positive")
```

---

# 20. Loops

## 20.1 `for` — LOCKED

```fray
for i in range(2):
    x += 1
```

The loop target is assigned each iteration.

## 20.2 `while` — LOCKED

```fray
while True:
    x += 1
```

## 20.3 Iteration over collections

The intended `in` syntax supports iteration over iterable values:

```fray
for value in x:
    print(value)
```

## 20.4 `break` — IMPLEMENTATION PLANNED

The plan explicitly mentions `break` validity in semantic analysis, so the intended syntax is:

```fray
while True:
    if done:
        break
```

## 20.5 `continue` — PROPOSED

```fray
for i in range(10):
    if i == 5:
        continue
    print(i)
```

The original project plan does not explicitly lock `continue` yet.

## 20.6 Range helper

Examples already establish:

```fray
range(2)
```

and therefore:

```fray
for i in range(2):
    print(i)
```

Additional range forms are **PROPOSED** until documented:

```fray
range(start, stop)
range(start, stop, step)
```

---

# 21. Exceptions

## 21.1 `try` / `except` / `finally` — LOCKED

```fray
try:
    x = 1 + "10"
except TypeError:
    print("Cannot add a string to a number!")
finally:
    print(x)
```

## 21.2 Exception type matching

The parser and semantic analyzer are expected to recognize exception types such as:

```fray
except TypeError:
    ...
```

## 21.3 Future exception raising syntax — PROPOSED

The original syntax reference does not define `raise`.

A natural future form is:

```fray
raise TypeError("invalid value")
```

This should not be considered part of the locked core until explicitly added to the grammar.

---

# 22. Imports and modules

## 22.1 Import — LOCKED

```fray
import random
```

## 22.2 Module members — LOCKED

```fray
random.randomInt(0, 1)
```

The initial module system is intended to work from a single directory, with package management added later.

## 22.3 Future import forms — PROPOSED

Possible future syntax:

```fray
import math
import nn
```

or:

```fray
import random as rng
```

or:

```fray
from math import sqrt
```

These are design proposals only.

---

# 23. The `random` module

The project plan explicitly calls for a `random` module.

Current known function:

```fray
random.randomInt(0, 1)
```

Potential future API:

```fray
random.randomFloat(0.0, 1.0)
random.choice(x)
random.shuffle(x)
```

These additional functions are **PROPOSED** unless implemented and documented elsewhere.

---

# 24. Boolean / truthiness model

Boolean literals:

```fray
True
False
```

Logical expressions:

```fray
x and y
x or y
x xor y
x xnor y
not x
```

Conditional statements consume truth-like expressions:

```fray
if x:
    print("true")
```

> The exact truthiness of numbers, strings, containers, `None`, and user-defined values should be documented in `spec/semantics.md`.

---

# 25. No-value / null value

The current `fray.txt` does **not** establish a null literal such as Python's `None`.

The internal compiler/runtime plan mentions a `none` category in type inference, so a no-value runtime representation exists conceptually, but the source-level literal spelling is not locked.

## Proposed spelling — PROPOSED

```fray
None
```

Possible future use:

```fray
def do_something():
    return None
```

This must be explicitly added to the lexer/grammar before becoming core syntax.

---

# 26. Type system surface syntax

Fray's intended user-facing type model is:

```text
dynamic by default
optional static annotations
compiler type inference / specialization
boxed fallback for unknown dynamic values
```

The original syntax reference does not define the complete surface spelling for static types.

## 26.1 Primitive type names — PROPOSED / implementation-backed

```text
int
float
bool
str
```

Future numeric/tensor types can include:

```text
f32
f64
i32
i64
```

## 26.2 Type annotations — PROPOSED

```fray
x: int = 1
name: str = "Fray"
```

## 26.3 Function return annotations — PROPOSED

```fray
def square(x: int) -> int:
    return x ^ 2
```

## 26.4 Generic types — FUTURE / PROPOSED

The roadmap explicitly leaves generics for post-v1 development.

Possible syntax:

```fray
list[int]
list[T]
```

and:

```fray
fn max[T](a: T, b: T) -> T:
    ...
```

This is future syntax, not v1 core syntax.

---

# 27. Tensor / machine-learning syntax

Fray's roadmap includes a built-in tensor core and reverse-mode autodiff. The project notes establish the **capabilities**, but not a complete source syntax.

Therefore this section is deliberately **PROPOSED**.

## 27.1 Tensor construction

Possible syntax:

```fray
x = tensor([[1.0, 2.0], [3.0, 4.0]])
```

## 27.2 Tensor arithmetic

Potentially ordinary operators:

```fray
a = b + c
d = a * b
```

Elementwise vs matrix operations would need explicit semantic rules.

## 27.3 Matrix multiplication — PROPOSED

A possible dedicated operator:

```fray
y = a @ b
```

or an explicit function:

```fray
y = matmul(a, b)
```

No choice is locked yet.

## 27.4 Tensor metadata

Possible future access:

```fray
x.shape
x.dtype
x.ndim
```

## 27.5 Autodiff — PROPOSED

The roadmap establishes ideas such as:

```text
t.grad
zero_grad()
grad(f)
```

Possible surface usage:

```fray
y = model(x)
y.grad
```

The exact API belongs in the tensor/ML specification.

---

# 28. Threads and concurrency

The project has a real shared-memory threading model with no GIL.

The `threads` module is part of the implemented language roadmap and includes:

```text
spawn
join
joinAll
atomic
```

The exact source syntax for invoking these APIs was not fully captured in the original `fray.txt`, so the examples below are **PROPOSED API surface**, not locked grammar.

## 28.1 Spawn

Possible form:

```fray
import threads

thread = threads.spawn(worker, 10)
```

## 28.2 Join

Possible form:

```fray
threads.join(thread)
```

## 28.3 Join all

Possible form:

```fray
threads.joinAll(threads_list)
```

## 28.4 Atomic values

The runtime plan establishes an `atomic` type with:

```text
get
set
add
```

Possible syntax:

```fray
counter = threads.atomic(0)
counter.add(1)
print(counter.get())
```

## 28.5 Thread-shared globals

The compiler design treats module-level variables as thread-shared globals. The final language specification should define the synchronization semantics of reads/writes and when atomics are required.

---

# 29. Async / coroutines

The roadmap explicitly proposes:

```text
async def
await
```

for I/O-bound concurrency.

## 29.1 Proposed async function

```fray
async def fetch_data():
    ...
```

## 29.2 Proposed await

```fray
data = await fetch_data()
```

This is **PROPOSED** until the coroutine phase is finalized.

## 29.3 Non-blocking file I/O

Inside a coroutine, `readFileAsync` and `writeFileAsync` hand the blocking
syscall to a worker and park the fiber; the event loop resumes it when the
worker posts, so the other coroutines keep running while this one waits on the
disk.

```fray
async def load(path):
    return readFileAsync(path)


async def main():
    n = writeFileAsync("/tmp/fray_layout_demo.txt", "written by a worker")
    text = await load("/tmp/fray_layout_demo.txt")
    print(n)
    print(text)


main()
runUntilComplete()
```

Called from a plain thread rather than a coroutine, the same two functions are
just the blocking call, so module-level code can use them without ceremony.

## 29.4 Sockets

Loopback TCP, for reaching a service without tying up a thread. A socket is a
plain integer, so handles can be passed around like any other value;
`tcpListen(0)` asks for an ephemeral port and `tcpPort` reports which one the
kernel chose, which is also how a program avoids hardcoding a port.

| Call | Meaning |
|---|---|
| `tcpListen(port)` | bind and listen on 127.0.0.1; returns a server handle |
| `tcpPort(handle)` | the port a listener bound |
| `tcpAccept(server)` | wait for a connection; returns its handle |
| `tcpConnect(host, port)` | connect; returns a handle |
| `readAsync(handle, n)` | read up to `n` bytes; returns a string (`""` at a clean close) |
| `writeAsync(handle, text)` | write a string; returns the byte count |
| `closeSocket(handle)` | close a socket or listener |

`tcpListen` binds the loopback address only — a program cannot open a listening
socket on an external interface. On a platform without sockets the calls raise
`OSError`.

---

# 30. Object-oriented syntax

Optional object-oriented programming is part of the broader Fray design, but the original syntax reference does not define classes.

## 30.1 Classes — PROPOSED / FUTURE

Possible syntax:

```fray
class Person:
    def __init__(self, name):
        self.name = name

    def greet(self):
        print("Hello, " + self.name)
```

This should be kept out of the locked core until the object model and method dispatch rules are finalized.

## 30.2 Constructors

Possible form:

```fray
person = Person("Fray")
```

## 30.3 Instance members

Possible form:

```fray
person.name
person.greet()
```

---

# 31. Structs, enums, unions, pointers, arrays, and FFI

The self-hosting plan explicitly identifies these as **compiler-required language features**, but their source grammar has not been locked.

They should therefore have dedicated syntax sections in the eventual formal specification.

## 31.1 Structs — PROPOSED

Possible syntax:

```fray
struct Point:
    x: int
    y: int
```

## 31.2 Enums / variants — PROPOSED

Possible syntax:

```fray
enum Result:
    Ok
    Error
```

The post-v1 roadmap also considers algebraic data types / sum types:

```fray
type Shape = Circle(radius) | Rect(width, height)
```

## 31.3 Unions — PROPOSED

Potential syntax:

```fray
union Number:
    int
    float
```

## 31.4 Pointers — PROPOSED / low-level feature

Potential syntax must be designed alongside Fray's memory/ownership model rather than copied directly from another language.

Example placeholder:

```fray
ptr = &x
value = *ptr
```

This is **not** currently valid Fray syntax.

## 31.5 C FFI — PROPOSED

The self-hosting plan requires the ability to call into the C runtime. Exact syntax is undecided.

Possible future form:

```fray
extern "C" def fray_runtime_function(...)
```

---

# 32. Memory-management surface

Fray is designed around automatic memory management rather than a manual `free()` model.

The runtime plan currently includes:

- atomic reference counting
- cycle collection
- ownership-aware compiler code generation
- weak references

This means normal user code should not need explicit deallocation syntax.

## 32.1 User-facing ownership syntax

No ownership syntax is currently locked.

Any future ownership annotations should be introduced only after the semantics are fully specified.

---

# 33. Error-producing expressions

Operations such as these may produce runtime exceptions:

```fray
1 + "10"
x[999]
10 / 0
10 // 0
```

The runtime/compiler is expected to preserve the intended exception semantics even when expressions are optimized into unboxed native operations.

---

# 34. Standard program examples

## 34.1 Hello world

```fray
print("Hello, world!")
```

## 34.2 Variables

```fray
x = 10
y = 20
print(x + y)
```

## 34.3 A function

```fray
def add(a, b):
    return a + b

print(add(10, 20))
```

## 34.4 A loop

```fray
for i in range(10):
    print(i)
```

## 34.5 A list

```fray
numbers = [1, 2, 3]
numbers.append(4)
print(numbers)
```

## 34.6 A conditional

```fray
x = 15

if x > 10:
    print("large")
elif x == 10:
    print("ten")
else:
    print("small")
```

## 34.7 Exceptions

```fray
try:
    x = 1 + "hello"
except TypeError:
    print("type error")
finally:
    print("done")
```

## 34.8 Imports

```fray
import random

x = random.randomInt(0, 100)
print(x)
```

---

# 35. Complete-example program

This example combines most of the currently locked core syntax in one file.

```fray
# Fray example program
import random

const greeting = "Hello from Fray!"


def average(values):
    return mean(values)


def describe(values):
    print("values:")
    print(values)
    print("length:")
    print(len(values))
    print("mean:")
    print(mean(values))
    print("median:")
    print(med(values))
    print("middle item:")
    print(mid(values))


numbers = [10, 20, 15, 12]

numbers.append(random.randomInt(0, 100))

if len(numbers) > 3:
    describe(numbers)
else:
    print("not enough values")

for i in range(3):
    print(i)

try:
    x = 1 + "10"
except TypeError:
    print("Cannot add a string to a number!")
finally:
    print(greeting)
```

---

# 36. Formal grammar skeleton

This is a high-level grammar map rather than the final parser grammar.

```ebnf
program         = { statement } ;

statement       = simple-statement
                | compound-statement ;

simple-statement = assignment
                 | expression-statement
                 | return-statement
                 | import-statement
                 | break-statement ;

compound-statement = if-statement
                   | for-statement
                   | while-statement
                   | try-statement
                   | function-definition ;

assignment      = [ "const" ] target assignment-op expression ;
assignment-op   = "=" | "+=" | "-=" | "*=" | "/=" ;

target          = identifier
                | indexing
                | member-access ;

return-statement = "return" [ expression ] ;

import-statement = "import" identifier ;

if-statement    = "if" expression ":" block
                  { "elif" expression ":" block }
                  [ "else" ":" block ] ;

for-statement   = "for" identifier "in" expression ":" block ;

while-statement = "while" expression ":" block ;

try-statement   = "try" ":" block
                  "except" identifier ":" block
                  [ "finally" ":" block ] ;

function-definition = "def" identifier "(" [ parameters ] ")" ":" block ;

block           = indented-statements ;
parameters      = identifier { "," identifier } ;

expression      = ... ;
```

The `expression = ...` portion intentionally leaves operator precedence and postfix-expression parsing to the formal grammar document.

---

# 37. Lexical token map

A lexer for the current core language needs, at minimum, to recognize:

## Punctuation

```text
(
)
[
]
{
}
,
:
.
```

## Assignment

```text
=
+=
-=
*=
/=
```

## Comparison

```text
==
!=
>
<
>=
<=
```

## Arithmetic

```text
+
-
*
/
//
%
^
```

## Logical / bitwise word operators

```text
and
or
xor
xnor
not
band
bor
bxor
bnot
bxnor
```

## Keywords

```text
def
return
if
elif
else
for
in
while
try
except
finally
const
import
```

## Literals

```text
True
False
integer
float
string
```

## Identifier

```text
identifier
```

## Comment

```text
# ...
```

---

# 38. Syntax intentionally NOT locked yet

The following areas are mentioned in the project roadmap but should not be treated as finalized syntax solely because they appear here:

```text
None / null syntax
string interpolation
multiline / raw strings
slice syntax
keyword arguments
default parameters
variadic parameters
continue
raise
static type annotations
classes / OOP syntax
dictionaries / maps
generics
pattern matching
macros / comptime
operator overloading
ADTs / sum types
traits / protocols
ownership annotations
pointers / references
structs
enums / variants
unions
C FFI
async / await
channels
tensor literals
matrix multiplication syntax
GPU/device syntax
package imports
```

These should be promoted from **PROPOSED** to **LOCKED** only after the grammar and semantics are agreed upon.

---

# 39. Design consistency rules

When extending Fray syntax, prefer these rules:

1. **Python familiarity where it does not conflict with Fray's design.**
2. **Keep syntax visually lightweight.**
3. **Prefer one obvious syntax over several competing spellings.**
4. **Do not introduce a keyword when an existing expression form is enough.**
5. **Keep the core language small enough that the self-hosted compiler can realistically parse and compile it.**
6. **Keep runtime-heavy features from forcing complicated syntax when a normal function/module interface will do.**
7. **Any syntax that affects ownership, threading, or low-level memory semantics must have explicit semantic rules rather than relying on implementation behavior.**

---

# 40. Proposed future full-program direction

The long-term surface of Fray could plausibly grow toward something like this while preserving the original lightweight style:

```fray
import nn
import random
import threads

const learning_rate: float = 0.001


def train(model, data):
    for batch in data:
        prediction = model(batch.input)
        loss = model.loss(prediction, batch.target)

        loss.grad
        model.zero_grad()
        model.step(learning_rate)


def worker(values):
    total = 0
    for value in values:
        total += value
    return total


numbers = [1, 2, 3, 4]

thread = threads.spawn(worker, numbers)
result = threads.join(thread)

print(result)
```

This final example is intentionally illustrative rather than normative: some APIs shown above are still **PROPOSED**.

---

# 41. Versioning rule for this file

When a syntax feature becomes finalized:

1. Move it from **PROPOSED** to **LOCKED**.
2. Add its exact lexical grammar.
3. Add its semantic rules.
4. Add at least one golden test.
5. Add at least one invalid-input test where appropriate.
6. Update the parser and round-trip printer.
7. Update this document and the formal EBNF together.

The formal grammar should remain the final authority. This file is the human-friendly syntax map.

---

# 42. Core syntax cheat sheet

```text
# comment

x = 1
const x = 1
x += 1
x -= 1
x *= 1
x /= 1

print(x)

1 + 1
1 - 1
1 * 1
1 / 1
1 // 1
1 % 1
1 ^ 2

x == 1
x != 1
x > 1
x < 1
x >= 1
x <= 1

x and y
x or y
x xor y
x xnor y
not x
x band y
x bor y
x bxor y
bnot x
x bxnor y

x = [1, 2, 3]
x[0]
x[0] = 4
x.append(5)
x.depend

x = {1, 2, 3}
x.append(4)
x.depend

x = (1, 2, 3)

len(x)
min(1, 2, 3)
max(1, 2, 3)
sum(x)
abs(x)
sqrt(x)
isqrt(x)
round(x)
int(x)
float(x)
str(x)
mean(x)
med(x)
mode(x)
mid(x)

pi
e

input()
inputInt()
inputStr()
inputFloat()


def function(a, b):
    return a + b


if x == 1:
    ...
elif x == 2:
    ...
else:
    ...

for i in range(10):
    ...

while True:
    ...

try:
    ...
except TypeError:
    ...
finally:
    ...

import random
random.randomInt(0, 1)
```

---

## Final status

**Locked core:** variables, `const`, comments, indentation, functions, returns, calls, lists, sets, tuples, indexing, assignment operators, arithmetic, comparison, logical operators, bitwise operators from the reference, `if/elif/else`, `for/in`, `while`, `try/except/finally`, imports, core builtins, `random.randomInt`, `True` / `False`, `pi`, `e`.

**Planned but not fully surface-locked:** `break`, richer range forms, type annotations, dictionaries, async syntax, threads API spelling, tensor syntax, OOP, structs/enums/unions/pointers/FFI, and most post-v1 language expansion.

**Source of truth hierarchy:**

```text
formal grammar + formal semantics
        ↓
implemented lexer/parser/compiler behavior
        ↓
fray.txt reference examples
        ↓
this human-readable syntax layout
```

That hierarchy keeps this document useful as a design map without accidentally turning every example into a language commitment.
