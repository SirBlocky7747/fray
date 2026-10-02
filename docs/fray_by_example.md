# fray by example

A hands-on guide to the fray programming language.
Every feature is demonstrated with runnable code.

---

## Hello, world!

```fray
print("Hello, fray!")
```

## Variables and assignment

```fray
x = 42
ratio = 3.14
greeting = "hello"
is_active = True
nothing = null

# A name the language already owns is not yours to rebind: `pi`, `e` and the
# builtins are rejected with "invalid assignment target" rather than shadowed.

# Augmented assignment
x += 10    # x is now 52
x -= 5     # x is now 47
x *= 2     # x is now 94
```

## Constants

```fray
const MAX_SIZE = 1024
const PI = 3.14159265358979

# MAX_SIZE = 2048  # Error: cannot reassign constant
```

## Arithmetic

```fray
a = 10 + 3       # 13
b = 10 - 3       # 7
c = 10 * 3       # 30
d = 10 / 3       # 3.333...
fdiv = 10 // 3   # 3  (floor division)
mod = 10 % 3     # 1  (modulo)
g = 2 ^ 10       # 1024 (power)
```

## Boolean logic

```fray
a = True and False   # False
b = True or False    # True
c = not True         # False
d = True xor False   # True
f = True xnor True   # True

# Short-circuit evaluation:
# and: if left is false, right is never evaluated
# or:  if left is true, right is never evaluated
```

## Comparison

```fray
1 == 1     # True
1 != 2     # True
1 < 2      # True
2 > 1      # True
1 <= 1     # True
2 >= 3     # False
```

## Strings

```fray
s = "hello"
s = "hello" + " world"    # "hello world"
s = "ha" * 3              # "hahaha"

# Indexing and slicing read a name, not a literal: bind the string first.
word = "hello"
word[0]                   # "h"
word[1:3]                 # "el"

# String methods — on a name, not on a literal: `"HELLO".lower()` is a
# parse error, so bind the receiver first.
word = "HELLO"
word.lower()             # "hello"
word.upper()             # "HELLO"

spaced = "  hi  "
spaced.trim()            # "hi"

phrase = "hello world"
phrase.find("world")     # 6
phrase.find("xyz")       # -1
phrase.replace("world", "fray")  # "hello fray"
phrase.startswith("hel") # True
phrase.endswith("llo")   # True

csv = "a,b,c"
csv.split(",")         # ["a", "b", "c"]
```

## String conversions

```fray
n = 42
s = str(n)         # "42"
n = int("42")      # 42
f = float("3.14")  # 3.14
c = chr(65)        # "A"
n = ord("A")       # 65
```

## Lists

```fray
xs = [1, 2, 3, 4, 5]
xs.append(6)       # [1, 2, 3, 4, 5, 6]
len(xs)            # 6
xs[0]              # 1
xs[1:3]            # [2, 3]
xs[1] = 99         # [1, 99, 3, 4, 5, 6]
xs.depend()        # removes and returns last element
```

## Tuples

```fray
t = (1, "hello", 3.14)
t[0]               # 1
t[2]               # 3.14
len(t)             # 3
```

## Sets

```fray
s = {1, 2, 3}
s.append(4)        # {1, 2, 3, 4}
s.append(2)        # {1, 2, 3, 4} (no duplicate)
len(s)             # 4
```

## Maps (dictionaries)

```fray
m = {"name": "fray", "version": 1}
m["name"]          # "fray"
m["lang"] = "compiled"
len(m)             # 3
"name" in m        # True
del m["lang"]
m.keys()           # ["lang", "name", "version"] in hash order, not insertion order

# A map has three methods: keys(), has() and len(). There is no values() —
# read a value with m[key] instead.
m["version"]       # 1
```

## Control flow: if / elif / else

```fray
x = 42
if x > 100:
    print("big")
elif x > 10:
    print("medium")
else:
    print("small")
# prints: medium
```

## Loops: for

```fray
for i in range(5):
    print(i)
# prints 0, 1, 2, 3, 4

xs = [10, 20, 30]
for x in xs:
    print(x)
# prints 10, 20, 30
```

Iterating a *string* is not implemented yet — `for c in "hello"` is rejected by
the compiler with a named diagnostic rather than silently doing nothing. Iterate
the characters you need explicitly (`for i in range(len(s))`) until it lands.

## Loops: while

```fray
i = 0
while i < 5:
    print(i)
    i += 1
```

## break and continue

```fray
for i in range(10):
    if i == 3:
        continue    # skip 3
    if i == 7:
        break       # stop at 7
    print(i)
# prints: 0, 1, 2, 4, 5, 6
```

## Functions

```fray
def add(a, b):
    return a + b

print(add(2, 3))  # 5

def greet(name):
    print("Hello, " + name + "!")

greet("world")
```

## Optional parameters (there are no default arguments)

fray has no default arguments, and a call must pass exactly as many arguments as
the `def` declares. The idiom for an optional value is an explicit sentinel the
caller supplies:

```fray
def power(base, exp):
    if exp == 0:     # 0 is this function's "not given" marker
        exp = 2
    result = 1
    for i in range(exp):
        result = result * base
    return result

print(power(3, 0))    # 9   — 0 means "use the default"
print(power(2, 10))   # 1024
```

Calling `power(3)` with one argument is an error, not a default: the compiler
rejects it. Pick a sentinel that cannot be a legitimate value (`0` cannot be an
exponent you want, but `None` usually can be a real argument).

## Scope

A name first assigned at the top level is a **module global**, and a function
that assigns it writes the global — it does *not* create a local of its own:

```fray
x = 10
def change():
    x = 20      # writes the module-level x
    print(x)    # 20
change()
print(x)         # 20 — the global was changed
```

For a local of its own, give the function a parameter, or a name that is
assigned nowhere else in the module.


## Data structures in functions

```fray
def sum_list(xs):
    total = 0
    for x in xs:
        total += x
    return total

print(sum_list([1, 2, 3, 4, 5]))  # 15
```

## Nested structures

```fray
matrix = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
print(matrix[1][2])  # 6

data = {"users": [{"name": "alice"}, {"name": "bob"}]}
print(data["users"][0]["name"])  # "alice"
```

## Enumerations

```fray
enum Direction:
    case North
    case South
    case East
    case West

d = Direction.North
print(d._variant)  # "North"
```

## Parameterized enums

```fray
enum Shape:
    case Circle(radius)
    case Rectangle(width, height)

c = Shape.Circle(5)
print(c._variant)    # "Circle"
print(c.radius)      # 5

r = Shape.Rectangle(10, 20)
print(r.width)       # 10
print(r.height)      # 20
```

## Match expressions

```fray
enum Color:
    case Red
    case Green
    case Blue

c = Color.Red
match c:
    case Color.Red:
        print("red")
    case Color.Green:
        print("green")
    case _:
        print("other")
# prints: red

# Parameterized matching
enum Shape:
    case Circle(radius)
    case Rectangle(width, height)

s = Shape.Circle(5)
match s:
    case Shape.Circle(r):
        print("circle with radius " + str(r))
    case Shape.Rectangle(w, h):
        print("rectangle " + str(w) + "x" + str(h))
# prints: circle with radius 5

# A pattern may name the enum it belongs to (the forms above) or leave the
# enum off (`case Circle(r):`). Qualification is what tells two enums that
# declare a variant with the same name apart: an arm is matched only against
# the enum it names, and an arm qualified with another enum falls through to
# the next one:
enum A:
    case V(x)

enum B:
    case V(y)

b = B.V(7)
match b:
    case A.V(q):
        print("A " + str(q))
    case B.V(q):
        print("B " + str(q))
# prints: B 7
```

## Structs

```fray
struct Point:
    x
    y

p = Point(3, 4)
print(p.x)    # 3
print(p.y)    # 4
p.x = 10
print(p.x)    # 10
```

## Option and Result types

```fray
x = Some(42)
print(isSome(x))    # True
print(isNone(x))    # False
print(unwrap(x))    # 42

r = Ok("success")
print(isOk(r))      # True
print(unwrap(r))    # "success"

err = Err("something went wrong")
print(isErr(err))   # True
```

## The unwrap operator (?)

```fray
x = Some(42)
value = x?          # unwraps to 42 (or throws if None)

r = Ok("done")
result = r?         # unwraps to "done" (or throws if Err)
```

## Deleting variables

```fray
x = 42
print(x)    # 42
del x
# x is now undefined
```

## Deleting from collections

```fray
m = {"a": 1, "b": 2}
del m["a"]  # removes the "a" entry

# `del xs[i]` on a *list* is not implemented — the subscript form of `del`
# works on maps only, and a list raises TypeError.
```

## Modules and imports

```fray
# math_lib.fray:
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
```

```fray
# main.fray:
import math_lib

print(math_lib.add(2, 3))        # 5
print(math_lib.multiply(4, 5))   # 20

# Or import specific names:
from math_lib import add
print(add(10, 20))  # 30
```

## External C functions (FFI)

```fray
extern int strlen(string s)
extern int labs(int x)

print(strlen("hello"))  # 5
print(labs(-42))        # 42
```

An `extern` names a C function the linker will resolve, so it cannot collide
with a name the language already has: `extern int abs(int x)` is rejected,
because `abs` is a builtin and there is nothing to link to.

## Error handling with try/except

```fray
xs = [1, 2, 3]

try:
    v = xs[9]              # the runtime raises IndexError
    print(v)
except IndexError:
    print("index out of range")
finally:
    print("cleanup")
# prints: index out of range, cleanup
```

A pending exception is checked between statements, so a value that may not have
been produced is read on the statement *after* the one that raised, never in
the same statement.
A clause may name the error type, a catch-all type (`Exception`) or nothing at
all (a bare `except:`, which catches whatever the earlier clauses left), and
`finally` always runs:

```fray
try:
    q = 1 / 0              # ZeroDivisionError, an Exception
    print(q)
except Exception:
    print("caught it")
# prints: caught it
```

A `return` is checked the same way, so a function can hand back what its clause
produced instead of the fallback the failed expression left behind, and a
`finally` still runs before the caller sees the value:

```fray
def safe_div(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        return -1
    finally:
        print("checked")

print(safe_div(6, 3))  # checked, then 2.0
print(safe_div(1, 0))  # checked, then -1
```

The error types a program can catch are the ones the runtime raises —
`IndexError`, `KeyError`, `TypeError`, `ValueError` and `Exception` as their
catch-all. A *function* that fails reports it as a value instead, with the
Result type: `Err("division by zero")` next to `Ok(a / b)` above.

## Built-in functions

```fray
# Math
print(abs(-5))          # 5
print(sqrt(16))         # 4.0
print(isqrt(16))        # 4
print(round(3.7))       # 4
print(min(3, 1, 4))     # 1
print(max(3, 1, 4))     # 4
print(sum([1, 2, 3]))   # 6

# Type conversions
print(int(3.7))         # 3
print(float(3))         # 3.0
print(str(42))          # "42"
print(chr(65))          # "A"
print(ord("A"))         # 65

# Collections
xs = [3, 1, 4, 1, 5]
print(len(xs))          # 5
print(sum(xs))          # 14
print(mean(xs))         # 2.8
print(med(xs))          # 3
print(mid(xs))          # 4 (middle element)
print(mode(xs))         # 1 (most common)

# Constants
print(pi)               # 3.14159265358979
print(e)                # 2.71828182845905
```

## Threading

```fray
# spawn takes a function, not a call: it starts that function on a new thread.
# A thread body takes no parameters (read what it needs from globals), and
# joinAll() waits for every thread it started.
def count_worker():
    total = 0
    for i in range(1000000):
        total += 1
    print("counted to " + str(total))

spawn(count_worker)
spawn(count_worker)
joinAll()
# Both counted in parallel — the order of the two lines is up to the OS
```

## Coroutines and async

```fray
async def fetch_data():
    await sleep(1)
    return "data"

async def main():
    result = await fetch_data()
    print(result)

# Calling an async function *starts* it (it returns a coroutine handle);
# runUntilComplete() drains the scheduler, so main() must be called here.
main()
runUntilComplete()
```

## Channels (communication between threads)

```fray
ch = channel(10)

# Producer thread
def producer():
    for i in range(5):
        send(ch, i)
    close(ch)

# Consumer: spawn takes the function itself, and joinAll() waits for the
# producer to finish before the main thread starts receiving.
spawn(producer)
joinAll()
while True:
    val = recv(ch)
    if val == null:
        break
    print(val)
```

## Atomics (shared state)

```fray
counter = atomic(0)

def increment():
    for i in range(1000):
        counter.add(1)

spawn(increment)
spawn(increment)
joinAll()
print(counter.get())  # 2000 (exact, no locks needed)
```

## Input

```fray
name = input("Enter your name: ")
print("Hello, " + name + "!")
```

## Complete example: FizzBuzz

```fray
for i in range(1, 101):
    if i % 15 == 0:
        print("FizzBuzz")
    elif i % 3 == 0:
        print("Fizz")
    elif i % 5 == 0:
        print("Buzz")
    else:
        print(str(i))
```

## Complete example: Fibonacci

```fray
def fib(n):
    if n <= 1:
        return n
    a = 0
    b = 1
    for i in range(2, n + 1):
        temp = b
        b = a + b
        a = temp
    return b

for i in range(20):
    print(str(fib(i)))
```

## Complete example: Sorting

```fray
def bubble_sort(xs):
    n = len(xs)
    for i in range(n):
        for j in range(0, n - i - 1):
            if xs[j] > xs[j + 1]:
                temp = xs[j]
                xs[j] = xs[j + 1]
                xs[j + 1] = temp
    return xs

xs = [64, 34, 25, 12, 22, 11, 90]
print(bubble_sort(xs))
```

## Complete example: Enum-based interpreter

```fray
enum Expr:
    case Num(value)
    case Add(left, right)
    case Mul(left, right)

def eval(expr):
    match expr:
        case Expr.Num(v):
            return v
        case Expr.Add(l, r):
            return eval(l) + eval(r)
        case Expr.Mul(l, r):
            return eval(l) * eval(r)
        case _:
            return 0

# (3 + 4) * 2
# A call's arguments must sit on one line — fray has no multi-line argument
# list yet, so build the tree one node at a time.
three = Expr.Num(3)
four = Expr.Num(4)
two = Expr.Num(2)
sum_node = Expr.Add(three, four)
product = Expr.Mul(sum_node, two)
print(eval(product))  # 14
```

---

This covers every feature in the fray language. For more details, see the full specification in `spec/`.
