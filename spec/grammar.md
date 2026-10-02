# fray — EBNF Grammar

Derived from `fray.txt` and confirmed design decisions. Uses standard EBNF notation:
`{ }` = zero or more, `[ ]` = optional, `( | )` = alternation, `"..."` = literal token.

---

## 1. Lexical structure

```
source_file     = { statement | NEWLINE } ;

comment         = "#" { any_char - NEWLINE } ;
NEWLINE         = "\n" ;
INDENT          = "    " { "    " } ;           (* 4-space indentation *)
DEDENT          = <implicit, matching prior indent level> ;

(* Identifiers *)
IDENT           = ( letter | "_" ) { letter | digit | "_" } ;
letter          = "a"-"z" | "A"-"Z" ;
digit           = "0"-"9" ;

(* Keywords — reserved, cannot be used as identifiers *)
KEYWORD         = "def" | "return" | "if" | "elif" | "else"
               | "for" | "in" | "while" | "try" | "except"
               | "finally" | "const" | "import" | "and"
               | "or" | "xor" | "xnor" | "not"
               | "True" | "False" ;

(* Literals *)
INT_LITERAL     = digit { digit } ;
FLOAT_LITERAL   = digit { digit } "." digit { digit } ;
STRING_LITERAL  = '"' { any_char - '"' - NEWLINE } '"' ;

(* Operators *)
ASSIGN          = "=" ;
AUG_ASSIGN      = "+=" | "-=" | "*=" | "/=" ;
EQ              = "==" ;
NEQ             = "!=" ;
LT              = "<" ;
GT              = ">" ;
LTE             = "<=" ;
GTE             = ">=" ;
PLUS            = "+" ;
MINUS           = "-" ;
STAR            = "*" ;
SLASH           = "/" ;
DOUBLE_SLASH    = "//" ;
PERCENT         = "%" ;
POWER           = "^" ;

(* Delimiters *)
LPAREN          = "(" ;
RPAREN          = ")" ;
LBRACKET        = "[" ;
RBRACKET        = "]" ;
LBRACE          = "{" ;
RBRACE          = "}" ;
COMMA           = "," ;
COLON           = ":" ;
DOT             = "." ;
```

---

## 2. Expressions

```
(* Precedence (lowest to highest):
   or, xor
   and
   not
   == != < > <= >=
   + -
   * / // %
   ^
   unary + - not
   primary
*)

expr            = logic_or ;

logic_or        = logic_xor { "or" logic_xor } ;
logic_xor       = logic_and { "xor" logic_and } ;
logic_and       = logic_not { "and" logic_not } ;
logic_not       = "not" logic_not | comparison ;

comparison      = addition { ( "==" | "!=" | "<" | ">" | "<=" | ">=" ) addition } ;

addition        = multiplication { ( "+" | "-" ) multiplication } ;
multiplication  = power { ( "*" | "/" | "//" | "%" ) power } ;
power           = unary { "^" unary } ;        (* right-associative *)

unary           = ( "+" | "-" | "not" ) unary | primary ;

primary         = literal
               | IDENT
               | call
               | member_access
               | indexed
               | "(" expr ")"
               | list_literal
               | set_literal
               | tuple_literal ;

literal         = INT_LITERAL | FLOAT_LITERAL | STRING_LITERAL
               | "True" | "False"
               | "pi" | "e" ;

call            = primary "(" [ arg_list ] ")" ;
arg_list        = expr { "," expr } ;

member_access   = primary "." IDENT ;
indexed         = primary "[" expr "]" ;

list_literal    = "[" [ expr { "," expr } [ "," ] ] "]" ;
set_literal     = "{" expr { "," expr } [ "," ] "}" ;
tuple_literal   = "(" expr "," [ expr { "," expr } [ "," ] ] ")" ;
                | "(" expr ")" ;                 (* single-element: not a tuple *)
                | "(" ")" ;                      (* empty tuple *)
```

---

## 3. Statements

```
statement       = simple_stmt NEWLINE
               | compound_stmt ;

simple_stmt     = assignment
               | augmented_assignment
               | expression_stmt
               | return_stmt
               | import_stmt
               | const_stmt ;

compound_stmt   = function_def
               | if_stmt
               | for_stmt
               | while_stmt
               | try_stmt ;

(* --- Simple statements --- *)

assignment      = target "=" expr ;
augmented_assignment = target aug_op expr ;
aug_op          = "+=" | "-=" | "*=" | "/=" ;

target          = IDENT
               | indexed
               | member_access ;

expression_stmt = expr ;

return_stmt     = "return" [ expr ] ;

import_stmt     = "import" [ relative_dots ] module_path
                | "from" [ relative_dots ] [ module_path ] "import" IDENT { "," IDENT } ;
module_path     = IDENT { "." IDENT } ;
(* Leading dots make an import relative to the importing module's package
   (`.util`, `..other`). In the from-form the path may be omitted entirely
   (`from . import util`), in which case the dots must be present. *)
relative_dots   = "." { "." } ;

const_stmt      = "const" IDENT "=" expr ;

(* --- Compound statements --- *)

function_def    = "def" IDENT "(" [ param_list ] ")" ":" NEWLINE block ;
param_list      = IDENT { "," IDENT } ;

if_stmt         = "if" expr ":" NEWLINE block
                  { "elif" expr ":" NEWLINE block }
                  [ "else" ":" NEWLINE block ] ;

for_stmt        = "for" IDENT "in" expr ":" NEWLINE block ;

while_stmt      = "while" expr ":" NEWLINE block ;

try_stmt        = "try" ":" NEWLINE block
                  { "except" [ IDENT ] ":" NEWLINE block }
                  [ "finally" ":" NEWLINE block ] ;

block           = INDENT { statement NEWLINE } DEDENT ;
```

---

## 4. Built-in functions and constants

These are resolved by the compiler/runtime, not the grammar — they parse as ordinary
function calls (`IDENT "(" arg_list ")"`) and identifiers (`IDENT`).

| Name | Signature | Notes |
|---|---|---|
| `print` | `print(expr)` | Writes to stdout |
| `input` | `input()` | Reads a string from stdin |
| `inputStr` | `inputStr([prompt])` | Reads a string; optional prompt |
| `inputInt` | `inputInt()` | Reads an integer from stdin |
| `inputFloat` | `inputFloat()` | Reads a float from stdin |
| `len` | `len(collection)` | Number of elements |
| `min` | `min(a, b, ...)` | Minimum of arguments |
| `max` | `max(a, b, ...)` | Maximum of arguments |
| `sum` | `sum(list)` | Sum of list elements |
| `abs` | `abs(x)` | Absolute value |
| `sqrt` | `sqrt(x)` | Square root (float) |
| `isqrt` | `isqrt(x)` | Integer square root (floor) |
| `round` | `round(x)` | Round to nearest integer |
| `int` | `int(x)` | Convert to integer |
| `float` | `float(x)` | Convert to float |
| `str` | `str(x)` | Convert to string |
| `range` | `range(n)` / `range(start, stop)` | Integer range for `for` loops |
| `mean` | `mean(list)` | Arithmetic mean |
| `med` | `med(list)` | Statistical median (sorts values) |
| `mid` | `mid(list)` | Positional middle element of the list |
| `mode` | `mode(list)` | Most frequent value |
| `pi` | constant | ~3.14159 |
| `e` | constant | ~2.71828 |

---

## 5. Container operations (runtime dispatch)

| Operation | Syntax | Notes |
|---|---|---|
| List literal | `[1, 2, 3]` | Mutable, ordered, indexed from 0 |
| Set literal | `{1, 2, 3}` | Mutable, unordered, deduplicated |
| Tuple literal | `(1, 2, 3)` | Immutable |
| Index | `x[i]` | Returns element at position `i` |
| Index assign | `x[i] = v` | Lists only (tuples are immutable) |
| Append | `x.append(v)` | Adds element to end (list/set) |
| Depend | `x.depend` | Removes last element (like `pop()`) (list/set) |
| Length | `len(x)` | Number of elements |
| Nested access | `x[i].append(v)` | Chained operations on nested containers |

---

## 6. Notes on syntax decisions

1. **Indentation**: 4 spaces. Tabs are not permitted. Block structure is determined by
   INDENT/DEDENT, not by braces.

2. **Comments**: `#` to end of line. No block comments. Can appear on their own line or
   inline after a statement.

3. **`^` is power, not XOR**: Unlike Python, `x ^ 2` means "x squared". Bitwise XOR is
   expressed with the `xor` keyword.

4. **`//` is floor division**: `7 // 2` evaluates to `3`.

5. **`/` is true division**: Returns a float when the result is not a whole number.
   `1 / 1` returns `1.0` (not `1`).

6. **Sets use `append`**: Unlike Python's `add`, fray unifies the API across all
   growable containers.

7. **`depend` removes the last element**: The inverse of `append`. On a set, removes an
   arbitrary element (implementation-defined order).

8. **`med` vs `mid`**: `med([3,1,2])` → `2.0` (statistical median, sorts first).
   `mid([3,1,2])` → `1` (element at middle index, no sorting).

9. **Integer auto-promotion**: 64-bit signed integers by default; automatically promote to
   arbitrary-precision bignum on overflow.

10. **No braces for blocks**: The grammar uses indentation, not `{}`. Braces are only for
    set literals and dict literals (future).
