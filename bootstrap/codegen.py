"""
fray LLVM code generator — compiles AST to native code via llvmlite.

Handles all fray.txt features: arithmetic, booleans, strings, lists, tuples,
sets, functions, control flow, builtins, exceptions, and constants.

Phase 5: full ownership discipline + type-inference-driven specialization.

  * Every FrayValue produced by an expression is an OWNED reference
    (constructors, ops, loads, indexing, user calls, min/max all hand back
    a +1 reference). The generator releases every temporary exactly once:
    statement results, binary-op operands, call arguments, branch
    conditions, loop iterables/elements — all flow through `_release`.

  * When the inference pass proves a ground type at a site, the generator
    emits a monomorphic fast path: unboxed i64/f64/i1 arithmetic and
    comparisons, range loops over unboxed indices, and list loops that
    keep the loop variable unboxed in a register. Boxing happens only at
    the edges (stores into variables/containers, calls, prints).

  * Wrong inference is impossible by construction: fast paths are emitted
    only for sites the lattice proved ground, and every fast path still
    handles the same semantics as the boxed one (exceptions included).
"""

from __future__ import annotations
import ctypes
import os
import subprocess
import tempfile

from typing import Optional, Any

from llvmlite import ir, binding

import target


from ast_nodes import (
    Program, Assignment, AugmentedAssignment, ExprStatement,
    ReturnStatement, ConstStatement, ImportStatement, ExternFuncDecl,
    FunctionDef, IfStatement, ForLoop, WhileLoop, TryStatement,
    BreakStatement, ContinueStatement,
    IntLiteral, FloatLiteral, StringLiteral, BoolLiteral, ConstantLiteral,
    Identifier, BinaryOp, UnaryOp, Call, MemberAccess, Index, Slice,
    AwaitExpr, QuestionMark, EnumDef, EnumCase,
    ListLiteral, SetLiteral, TupleLiteral,
    StructDef, MapLiteral, DelStatement,
    MatchExpression, MatchCase,
    Node,
)
from lexer import tokenize
from parser import parse
import modules
# NO_TYPE is the lattice's "no information (yet)" element. It is imported
# under a distinct name because VOID below is llvmlite's void *type*, and
# shadowing it silently broke every `is VOID` test on inferred types
# (notably the raw-parameter decision in _param_kind).
from own_lattice import (INT, FLOAT, BOOL, STRING, LIST, TUPLE, SET, NONE,
                         UNKNOWN, VOID as NO_TYPE, TypeNode)
from inference import Inference


# ── Initialize LLVM ──
binding.initialize_all_targets()
binding.initialize_all_asmprinters()


# ── Pointer type shorthand ──
PTR = ir.PointerType(ir.IntType(8), 0)
I64 = ir.IntType(64)
I32 = ir.IntType(32)
I1 = ir.IntType(1)
I8 = ir.IntType(8)
DOUBLE = ir.DoubleType()
VOID = ir.VoidType()


# ── Runtime function declarations ──

RUNTIME_FUNCS = {
    # Constructors
    "fray_int":           ir.FunctionType(PTR, [I64], False),
    "fray_float":         ir.FunctionType(PTR, [DOUBLE], False),
    "fray_bool":          ir.FunctionType(PTR, [I1], False),
    "fray_string_copy":   ir.FunctionType(PTR, [PTR], False),
    "fray_none":          ir.FunctionType(PTR, [], False),
    "fray_list":          ir.FunctionType(PTR, [], False),
    "fray_tuple":         ir.FunctionType(PTR, [I64], False),
    "fray_set":           ir.FunctionType(PTR, [], False),
    # Arithmetic
    "fray_add":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_sub":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_mul":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_div":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_floordiv":      ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_mod":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_pow":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_neg":           ir.FunctionType(PTR, [PTR], False),
    # Comparison
    "fray_eq":            ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_neq":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_lt":            ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_gt":            ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_lte":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_gte":           ir.FunctionType(PTR, [PTR, PTR], False),
    # Boolean
    "fray_and":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_or":            ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_xor":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_xnor":          ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_not":           ir.FunctionType(PTR, [PTR], False),
    "fray_is_truthy":     ir.FunctionType(I1, [PTR], False),
    # Builtins
    "fray_print":         ir.FunctionType(VOID, [PTR], False),
    "fray_print_sep":     ir.FunctionType(VOID, [PTR], False),
    "fray_print_end":     ir.FunctionType(VOID, [], False),
    "fray_len":           ir.FunctionType(PTR, [PTR], False),
    "fray_len_raw":       ir.FunctionType(I64, [PTR], False),
    "fray_thread_count":  ir.FunctionType(I32, [], False),
    "fray_abs":           ir.FunctionType(PTR, [PTR], False),
    "fray_sqrt":          ir.FunctionType(PTR, [PTR], False),
    "fray_isqrt":         ir.FunctionType(PTR, [PTR], False),
    "fray_round_val":     ir.FunctionType(PTR, [PTR], False),
    "fray_int_val":       ir.FunctionType(PTR, [PTR], False),
    "fray_float_val":     ir.FunctionType(PTR, [PTR], False),
    "fray_str_val":       ir.FunctionType(PTR, [PTR], False),
    "fray_range":         ir.FunctionType(PTR, [PTR], False),
    "fray_range3":        ir.FunctionType(PTR, [PTR, PTR, PTR], False),
    "fray_sum":           ir.FunctionType(PTR, [PTR], False),
    # List ops
    "fray_list_append":   ir.FunctionType(VOID, [PTR, PTR], False),
    "fray_list_depend":   ir.FunctionType(PTR, [PTR], False),
    # `.append` / `.depend` on either container: the tag decides the arm, so
    # the emitters no longer branch on it themselves (host and self-hosted
    # codegen used to carry their own copies of that branch).
    "fray_append":        ir.FunctionType(VOID, [PTR, PTR], False),
    "fray_depend":        ir.FunctionType(PTR, [PTR], False),
    "fray_list_index":    ir.FunctionType(PTR, [PTR, I64], False),
    "fray_list_setindex": ir.FunctionType(VOID, [PTR, I64, PTR], False),
    # Tuple ops
    "fray_tuple_index":   ir.FunctionType(PTR, [PTR, I64], False),
    "fray_tuple_setindex": ir.FunctionType(VOID, [PTR, I64, PTR], False),
    # Set ops
    "fray_set_append":    ir.FunctionType(VOID, [PTR, PTR], False),
    "fray_set_depend":    ir.FunctionType(PTR, [PTR], False),
    # String ops
    "fray_string_concat": ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_string_repeat": ir.FunctionType(PTR, [PTR, I64], False),
    "fray_string_index":  ir.FunctionType(PTR, [PTR, I64], False),
    # String methods
    "fray_string_upper":      ir.FunctionType(PTR, [PTR], False),
    "fray_string_lower":      ir.FunctionType(PTR, [PTR], False),
    "fray_string_trim":       ir.FunctionType(PTR, [PTR], False),
    "fray_string_find":       ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_string_replace":    ir.FunctionType(PTR, [PTR, PTR, PTR], False),
    "fray_string_split":      ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_string_startswith": ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_string_endswith":   ir.FunctionType(PTR, [PTR, PTR], False),
    # Memory
    "fray_release":       ir.FunctionType(VOID, [PTR], False),
    "fray_retain":        ir.FunctionType(VOID, [PTR], False),
    # Type helpers
    "fray_as_int":        ir.FunctionType(I64, [PTR], False),
    "fray_as_float":      ir.FunctionType(DOUBLE, [PTR], False),
    "fray_as_string":     ir.FunctionType(PTR, [PTR], False),
    # Statistics
    "fray_mean":          ir.FunctionType(PTR, [PTR], False),
    "fray_med":           ir.FunctionType(PTR, [PTR], False),
    "fray_mid":           ir.FunctionType(PTR, [PTR], False),
    "fray_mode":          ir.FunctionType(PTR, [PTR], False),
    "fray_min":           ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_max":           ir.FunctionType(PTR, [PTR, PTR], False),
    # Try/except
    "fray_try_begin":     ir.FunctionType(VOID, [], False),
    "fray_try_end":       ir.FunctionType(VOID, [], False),
    "fray_exc_type":      ir.FunctionType(PTR, [], False),
    "fray_exc_clear":     ir.FunctionType(VOID, [], False),
    "fray_exc_rethrow":   ir.FunctionType(VOID, [], False),
    # Exceptions (returns normally when a try block is active)
    "fray_throw":         ir.FunctionType(VOID, [I32, PTR], False),
    # Threads & atomics (Phase 6)
    "fray_function":      ir.FunctionType(PTR, [PTR, PTR, I32], False),
    # First-class calls (Phase 8): a value holding a function is called
    # through the runtime, which validates that it is callable and that the
    # argument count matches, and carries the arguments in a list.
    "fray_call":              ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_call_args":         ir.FunctionType(PTR, [], False),
    "fray_call_result_store": ir.FunctionType(VOID, [PTR], False),
    "fray_thread_spawn":  ir.FunctionType(I64, [PTR], False),
    "fray_thread_join":   ir.FunctionType(VOID, [I64], False),
    "fray_thread_join_all": ir.FunctionType(VOID, [], False),
    "fray_atomic_new":    ir.FunctionType(PTR, [PTR], False),
    "fray_atomic_get":    ir.FunctionType(PTR, [PTR], False),
    "fray_atomic_set":    ir.FunctionType(VOID, [PTR, PTR], False),
    "fray_atomic_add":    ir.FunctionType(PTR, [PTR, PTR], False),
    # Structs (Phase 8)
    "fray_struct_new":    ir.FunctionType(PTR, [PTR, I64, PTR], False),
    "fray_struct_field_get": ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_struct_field_set": ir.FunctionType(VOID, [PTR, PTR, PTR], False),
    # Maps (Phase 8)
    "fray_map_new":       ir.FunctionType(PTR, [], False),
    "fray_map_get":       ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_map_set":       ir.FunctionType(VOID, [PTR, PTR, PTR], False),
    "fray_map_has":       ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_map_del":       ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_map_len":       ir.FunctionType(PTR, [PTR], False),
    "fray_map_keys":      ir.FunctionType(PTR, [PTR], False),
    "fray_contains":      ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_ord":            ir.FunctionType(PTR, [PTR], False),
    "fray_chr":            ir.FunctionType(PTR, [PTR], False),
    # The input readers. Each has a `_str` twin that writes a prompt first, so
    # the prompt form of every one of them is reachable (`fray_input_str` is
    # both inputStr's plain reader and the prompt form of input).
    "fray_input":             ir.FunctionType(PTR, [], False),
    "fray_input_str":         ir.FunctionType(PTR, [PTR], False),
    "fray_input_int":         ir.FunctionType(PTR, [], False),
    "fray_input_int_str":     ir.FunctionType(PTR, [PTR], False),
    "fray_input_float":       ir.FunctionType(PTR, [], False),
    "fray_input_float_str":   ir.FunctionType(PTR, [PTR], False),
    "fray_slice":          ir.FunctionType(PTR, [PTR, PTR, PTR, PTR], False),
    "fray_some":           ir.FunctionType(PTR, [PTR], False),
    "fray_ok":             ir.FunctionType(PTR, [PTR], False),
    "fray_err":            ir.FunctionType(PTR, [PTR], False),
    "fray_unwrap":         ir.FunctionType(PTR, [PTR], False),
    "fray_is_some":        ir.FunctionType(PTR, [PTR], False),
    "fray_is_none_val":    ir.FunctionType(PTR, [PTR], False),
    "fray_is_ok":          ir.FunctionType(PTR, [PTR], False),
    "fray_is_err":         ir.FunctionType(PTR, [PTR], False),
    # Coroutines & channels (Phase 7). Boxed shims adapt the runtime's
    # void/int entry points to the boxed calling convention.
    "fray_coro_start":        ir.FunctionType(PTR, [PTR], False),
    "fray_coro_start_argv":   ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_coro_await":        ir.FunctionType(PTR, [PTR], False),
    "fray_coro_argv":         ir.FunctionType(PTR, [], False),
    "fray_init_args":         ir.FunctionType(VOID, [I32, ir.PointerType(PTR, 0)], False),
    "fray_prog_name":         ir.FunctionType(PTR, [], False),
    "fray_argv":              ir.FunctionType(PTR, [], False),
    "fray_file_read":         ir.FunctionType(PTR, [PTR], False),
    "fray_file_exists":       ir.FunctionType(PTR, [PTR], False),
    "fray_file_write":        ir.FunctionType(VOID, [PTR, PTR], False),
    "fray_list_dir":          ir.FunctionType(PTR, [PTR], False),
    "fray_is_dir":            ir.FunctionType(PTR, [PTR], False),
    "fray_exit_val":          ir.FunctionType(VOID, [PTR], False),
    "fray_coro_result_store": ir.FunctionType(VOID, [PTR], False),
    "fray_channel_new":       ir.FunctionType(PTR, [PTR], False),
    "fray_channel_send":      ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_channel_recv":      ir.FunctionType(PTR, [PTR], False),
    "fray_channel_close_boxed": ir.FunctionType(PTR, [PTR], False),
    # Non-blocking file & socket I/O (io.c). All boxed, like the rest.
    "fray_io_read_file_boxed":   ir.FunctionType(PTR, [PTR], False),
    "fray_io_write_file_boxed":  ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_io_read_fd_boxed":     ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_io_write_fd_boxed":    ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_io_accept_boxed":      ir.FunctionType(PTR, [PTR], False),
    "fray_io_connect_boxed":     ir.FunctionType(PTR, [PTR, PTR], False),
    "fray_io_listen_boxed":      ir.FunctionType(PTR, [PTR], False),
    "fray_io_close_fd_boxed":    ir.FunctionType(PTR, [PTR], False),
    "fray_io_port_boxed":        ir.FunctionType(PTR, [PTR], False),
    "fray_sleep_boxed":       ir.FunctionType(PTR, [PTR], False),
    "fray_coro_yield_boxed":  ir.FunctionType(PTR, [], False),
    "fray_coro_count_boxed":  ir.FunctionType(PTR, [], False),
    "fray_coro_run_until_complete_shim": ir.FunctionType(PTR, [], False),
}

# The reader each input builtin uses when it is given a prompt. The plain
# forms come from BUILTIN_MAP; these are the `_str` (prompt) entry points.
INPUT_PROMPT_FNS = {
    "input": "fray_input_str",
    "inputStr": "fray_input_str",
    "inputInt": "fray_input_int_str",
    "inputFloat": "fray_input_float_str",
}

# Built-in function name mapping: (runtime_name, min_args, max_args, 0=unlimited)
BUILTIN_MAP = {
    "print":  ("fray_print", 0, 0),
    "len":    ("fray_len", 1, 1),
    "abs":    ("fray_abs", 1, 1),
    "sqrt":   ("fray_sqrt", 1, 1),
    "isqrt":  ("fray_isqrt", 1, 1),
    "round":  ("fray_round_val", 1, 1),
    "int":    ("fray_int_val", 1, 1),
    "float":  ("fray_float_val", 1, 1),
    "str":    ("fray_str_val", 1, 1),
    "range":  ("fray_range", 1, 3),
    "sum":    ("fray_sum", 1, 1),
    "min":    ("fray_min", 2, 10),
    "max":    ("fray_max", 2, 10),
    "mean":   ("fray_mean", 1, 1),
    "med":    ("fray_med", 1, 1),
    "mid":    ("fray_mid", 1, 1),
    "mode":   ("fray_mode", 1, 1),
    "atomic": ("fray_atomic_new", 1, 1),
    # Coroutines & channels (Phase 7)
    "channel":         ("fray_channel_new", 0, 1),
    "send":            ("fray_channel_send", 2, 2),
    "recv":            ("fray_channel_recv", 1, 1),
    "close":           ("fray_channel_close_boxed", 1, 1),
    "sleep":           ("fray_sleep_boxed", 1, 1),
    "yieldNow":        ("fray_coro_yield_boxed", 0, 0),
    # Non-blocking file & socket I/O
    "readFileAsync":   ("fray_io_read_file_boxed", 1, 1),
    "writeFileAsync":  ("fray_io_write_file_boxed", 2, 2),
    "readAsync":       ("fray_io_read_fd_boxed", 2, 2),
    "writeAsync":      ("fray_io_write_fd_boxed", 2, 2),
    "tcpListen":       ("fray_io_listen_boxed", 1, 1),
    "tcpAccept":       ("fray_io_accept_boxed", 1, 1),
    "tcpConnect":      ("fray_io_connect_boxed", 2, 2),
    "closeSocket":     ("fray_io_close_fd_boxed", 1, 1),
    "tcpPort":         ("fray_io_port_boxed", 1, 1),
    "coroCount":       ("fray_coro_count_boxed", 0, 0),
    "runUntilComplete": ("fray_coro_run_until_complete_shim", 0, 0),
    # String operations
    "ord":              ("fray_ord", 1, 1),
    "chr":              ("fray_chr", 1, 1),
    # Input. All four take an optional prompt (the oracle writes it and then
    # reads), so the prompt form is handled in `_gen_call`'s builtin path.
    "input":            ("fray_input", 0, 1),
    "inputStr":         ("fray_input", 0, 1),
    "inputInt":         ("fray_input_int", 0, 1),
    "inputFloat":       ("fray_input_float", 0, 1),
    # Option / Result
    "Some":             ("fray_some", 1, 1),
    "Ok":               ("fray_ok", 1, 1),
    "Err":              ("fray_err", 1, 1),
    "unwrap":           ("fray_unwrap", 1, 1),
    "isSome":           ("fray_is_some", 1, 1),
    "isNone":           ("fray_is_none_val", 1, 1),
    "isOk":             ("fray_is_ok", 1, 1),
    "isErr":            ("fray_is_err", 1, 1),
    # Program arguments / files (fray_init_args is emitted into main directly)
    "progName":         ("fray_prog_name", 0, 1),
    "programArgs":      ("fray_argv", 0, 1),
    "fileRead":         ("fray_file_read", 1, 1),
    "fileExists":      ("fray_file_exists", 1, 1),
    "fileWrite":        ("fray_file_write", 1, 2),
    "listDir":          ("fray_list_dir", 1, 1),
    "isDir":            ("fray_is_dir", 1, 1),
    "exit":             ("fray_exit_val", 0, 1),
}

# Builtins whose runtime function returns void: the call is emitted for its
# side effect and the expression's value is None (the print precedent).
VOID_BUILTINS = {"print", "fileWrite", "exit"}

# Constants
CONSTANT_MAP = {
    "pi": 3.141592653589793,
    "e": 2.718281828459045,
}

# Exception type ids (must match the C runtime)
EXC_TYPE_MAP = {
    "TypeError": 1, "ValueError": 2, "IndexError": 3,
    "NameError": 4, "ZeroDivisionError": 5,
}
# Clause names that catch every exception (the oracle's rule: `except:`,
# `except Exception:` and `except RuntimeError:` all take anything).
CATCH_ALL_CLAUSES = {"Exception", "RuntimeError"}

# FrayObj header offsets (must match runtime.h)
OFF_TAG = 0
OFF_PAYLOAD = 40
# FrayTag values (must match runtime.h)
TAG_NONE, TAG_INT, TAG_FLOAT, TAG_BOOL, TAG_STRING, TAG_LIST, TAG_TUPLE, TAG_SET, TAG_FUNCTION, TAG_COROUTINE, TAG_CHANNEL, TAG_STRUCT, TAG_MAP = range(13)


class CodegenError(Exception):
    pass


class CodeGen:
    def __init__(self, module_name: str = "main"):
        self.module = ir.Module(module_name)
        # Emit for the host target. On Windows we pin the MSVC triple
        # (objects stay compatible with MinGW gcc linking; the MinGW
        # triple generates PIC/GOT code that causes access violations).
        # On Linux/macOS the native triple links directly with gcc/clang.
        self.module.triple = target.host_triple()
        layout = target.host_data_layout()
        if layout:
            self.module.data_layout = layout
        self.builder: Optional[ir.IRBuilder] = None
        self.func: Optional[ir.Function] = None
        self.named_values: dict[str, ir.AllocaInstr] = {}
        self._str_counter = 0
        self._block_counter = 0
        # Every boxed slot the current frame owns: its parameters and its
        # boxed locals, in creation order. A return releases all of them (see
        # _emit_func_return) — the slot holds a +1 because assignment releases
        # the old value and stores the new one, so a return that dropped only
        # the parameters left one box behind per local.
        self._frame_slots: list[ir.AllocaInstr] = []
        self._module_globals: set[str] = set()   # names stored as LLVM globals
        self.slot_kinds: dict[str, str] = {}     # raw unboxed slots: 'int'/'float'
        self.async_fns: set[str] = set()         # async function names (Phase 7)
        self._in_async: int = 0                  # >0 while generating an async body
        self.struct_defs: dict[str, list[str]] = {}  # name -> field names (Phase 8)
        self.extern_decls: dict[str, tuple] = {}    # name -> (LLVM func, ExternFuncDecl)
        self.enum_defs: dict[str, dict] = {}       # name -> {cases: [(name, [params]), ...], fields: [field_names]}
        self._imported_modules: set[str] = set()    # already-inlined module names
        self._module_asts: dict[str, object] = {}   # module name -> parsed AST
        self._source_file: str = ""
        self._root: str = ""        # every module path resolves under one root
        self._package: str = ""     # the compiling module's package ("" for main)
        # Unboxed (raw) ABI: 'int'/'float' while generating a raw body,
        # else None. `_raw_generated` marks functions whose raw body has
        # been emitted (or is being emitted right now, which is also the
        # recursion guard for on-demand generation).
        self._raw_ret: Optional[str] = None
        self._raw_generated: set[str] = set()
        # The try a `return` in the statement being generated belongs to, if
        # any: `(dispatch, finally_block, park_alloca, flag_alloca)`. A return
        # inside a try body has to answer a pending exception before it
        # returns, so it parks its value in `park_alloca`, marks the flag, and
        # leaves through the finally — see _gen_deferred_return.
        self._try_ctx: list = []
        # Inference results (attached by compile_to_ir)
        self.infer: Optional[Inference] = None
        self._declare_runtime()
        self._declare_constants()

    def _declare_runtime(self):
        for name, ftype in RUNTIME_FUNCS.items():
            ir.Function(self.module, ftype, name)

    def _gen_deferred_return(self, val: ir.Value):
        """A `return` inside a try body.

        The statement that produced `val` may have left an exception pending
        (`try: return a / b`), and under the oracle the matching clause takes
        that case instead of the return happening. So the pending flag is read
        here, before anything is returned:

          * an exception is pending — the return is abandoned, its value
            dropped (it was ours and nobody will own it now), and control joins
            the try's clause dispatch.
          * nothing is pending — the value waits in the try's park slot while
            the finally body runs, and the try's tail returns it. Running the
            finally first is what the oracle does: `try: return x` with a
            finally prints the finally's output before the caller sees
            anything.
        """
        ctx = self._try_ctx[-1]
        has_exc = self.builder.icmp_signed(
            "!=", self._load_exc_type(), ir.Constant(I32, 0))
        throw_block = self._new_block("try_ret_throw")
        park_block = self._new_block("try_ret_park")
        self.builder.cbranch(has_exc, throw_block, park_block)
        self.builder.position_at_end(throw_block)
        self._release(val)
        self.builder.branch(ctx[0])
        self.builder.position_at_end(park_block)
        self.builder.store(val, ctx[2])
        self.builder.store(ir.Constant(I1, 1), ctx[3])
        self.builder.branch(ctx[1])

    def _gen_deferred_raw_return(self, node: ReturnStatement):
        """The unboxed twin of _gen_deferred_return: `return` inside the try
        body of a raw (unboxed) body, where the parked value is the raw result
        itself, so the abandoned path has nothing to release."""
        if node.value is None:
            val = (ir.Constant(I64, 0) if self._raw_ret == "int"
                   else ir.Constant(DOUBLE, 0.0))
        elif self._raw_ret == "int":
            val = self._unbox_int(node.value)
        else:
            val = self._unbox_float(node.value)
        ctx = self._try_ctx[-1]
        has_exc = self.builder.icmp_signed(
            "!=", self._load_exc_type(), ir.Constant(I32, 0))
        throw_block = self._new_block("try_ret_throw")
        park_block = self._new_block("try_ret_park")
        self.builder.cbranch(has_exc, throw_block, park_block)
        self.builder.position_at_end(throw_block)
        self.builder.branch(ctx[0])
        self.builder.position_at_end(park_block)
        self.builder.store(val, ctx[2])
        self.builder.store(ir.Constant(I1, 1), ctx[3])
        self.builder.branch(ctx[1])

    def _exc_match_test(self, exc_name: Optional[str]) -> Optional[ir.Value]:
        """IR test for `except <name>`, built in the current block.

        None when the clause can never match. The rule is the oracle's: a
        named clause matches its own type, and `except:` / `except Exception:`
        / `except RuntimeError:` match the types the oracle can name — which is
        why a bare `except:` tests set membership rather than "any exception".
        A KeyError or an ImportError passes a bare `except:` in the oracle too
        (see plan.md), so it does here."""
        if exc_name is None or exc_name in CATCH_ALL_CLAUSES:
            types = sorted(EXC_TYPE_MAP.values())
        else:
            expected = EXC_TYPE_MAP.get(exc_name)
            if expected is None:
                return None
            types = [expected]
        exc_val = self._load_exc_type()
        test = None
        for exc_id in types:
            match = self.builder.icmp_signed(
                "==", exc_val, ir.Constant(I32, exc_id))
            test = match if test is None else self.builder.or_(test, match)
        return test

    def _load_exc_type(self) -> ir.Value:
        """The thread's pending exception type, 0 when there is none.

        Read through the accessor rather than the `fray_exception_type`
        global: exception state is per-thread (runtime.h), and the fixed
        global is only the main thread's copy, so a try inside a spawned
        thread would poll the wrong thread's flag."""
        ptr = self.builder.bitcast(self._call("fray_exc_type", []),
                                   ir.PointerType(I32, 0))
        return self.builder.load(ptr)

    def _declare_constants(self):
        pi_global = ir.GlobalVariable(self.module, DOUBLE, "fray_pi")
        pi_global.initializer = ir.Constant(DOUBLE, CONSTANT_MAP["pi"])
        pi_global.global_constant = True
        e_global = ir.GlobalVariable(self.module, DOUBLE, "fray_e")
        e_global.initializer = ir.Constant(DOUBLE, CONSTANT_MAP["e"])
        e_global.global_constant = True

    def _get_rt(self, name: str) -> ir.Function:
        return self.module.get_global(name)

    def _call(self, name: str, args: list[ir.Value]) -> ir.Value:
        fn = self._get_rt(name)
        return self.builder.call(fn, args)

    # ── Ownership helpers ──

    def _release(self, val: ir.Value):
        """Release an owned reference (no-op for NULL, which the runtime
        also tolerates)."""
        if val is not None:
            self._call("fray_release", [val])

    def _release_all(self, vals):
        for v in vals:
            if v is not None:
                self._call("fray_release", [v])

    def _retained(self, val: ir.Value) -> ir.Value:
        """+1 an owned reference so it can be kept in two places."""
        if val is None:
            return None
        self._call("fray_retain", [val])
        return val

    # ── Boxing helpers ──

    def _box_int(self, val: ir.Value) -> ir.Value:
        return self._call("fray_int", [val])

    def _box_float(self, val: ir.Value) -> ir.Value:
        return self._call("fray_float", [val])

    def _box_bool(self, val: ir.Value) -> ir.Value:
        return self._call("fray_bool", [val])

    def _box_string(self, val: ir.Value) -> ir.Value:
        return self._call("fray_string_copy", [val])

    def _box_none(self) -> ir.Value:
        return self._call("fray_none", [])

    # ── Fast-path helpers (unboxed operations) ──

    def _ty(self, node) -> TypeNode:
        """Inferred lattice type of an expression node (NO_TYPE if unknown)."""
        if self.infer is None or node is None:
            return NO_TYPE
        if isinstance(node, IntLiteral):
            return INT
        if isinstance(node, FloatLiteral):
            return FLOAT
        if isinstance(node, BoolLiteral):
            return BOOL
        if isinstance(node, StringLiteral):
            return STRING
        if isinstance(node, Identifier):
            # A name in a raw i64/f64 slot carries a *proven* type, which
            # is stronger evidence than the flow-insensitive join: the
            # slot decision already required every assignment and every
            # call site to pass that exact type. Without this the fast
            # paths below would treat raw slots as unknown and re-box
            # them on every use.
            kind = self._slot_kind(node.name)
            if kind == "int":
                return INT
            if kind == "float":
                return FLOAT
            return self.infer.type_of(node.name, self._current_ns)
        if isinstance(node, BinaryOp):
            lt = self._ty(node.left)
            rt = self._ty(node.right)
            return self._binop_ty(node.op, lt, rt)
        if isinstance(node, UnaryOp):
            ty = self._ty(node.operand)
            if node.op == "not":
                return BOOL
            if node.op == "-":
                if ty in (INT, BOOL):
                    return INT
                if ty is FLOAT:
                    return FLOAT
            return UNKNOWN
        if isinstance(node, Call):
            return self._call_ty(node)
        return UNKNOWN

    def _binop_ty(self, op: str, lt: TypeNode, rt: TypeNode) -> TypeNode:
        if op in ("==", "!=", "<", ">", "<=", ">=",
                  "and", "or", "xor", "xnor"):
            return BOOL
        if lt is INT and rt is INT:
            return FLOAT if op == "/" else INT
        if lt in (INT, FLOAT) and rt in (INT, FLOAT):
            if op == "//":
                return UNKNOWN  # result type depends on runtime tags
            return FLOAT if (lt is FLOAT or rt is FLOAT) else INT
        if op == "+":
            if lt is STRING and rt is STRING:
                return STRING
            if lt is LIST and rt is LIST:
                return LIST
        if op == "*" and ((lt is STRING and rt is INT)
                          or (lt is INT and rt is STRING)):
            return STRING
        return UNKNOWN

    def _call_ty(self, node: Call) -> TypeNode:
        fname = node.func.name if isinstance(node.func, Identifier) else None
        if fname is None:
            return UNKNOWN
        if fname == "range":
            return LIST
        if fname == "str":
            return STRING
        if fname in ("int", "isqrt", "round", "len"):
            return INT
        if fname in ("float", "sqrt", "mean", "med"):
            return FLOAT
        if fname in ("input", "inputStr"):
            return STRING
        if fname == "inputInt":
            return INT
        if fname == "inputFloat":
            return FLOAT
        # Async calls yield a TAG_COROUTINE handle (never the body type).
        if fname in self.async_fns:
            return UNKNOWN
        if fname in self.infer._funcs:
            return self.infer._call_result(
                fname,
                [self._ty(a) for a in node.args],
                node.args)
        return UNKNOWN

    def _int_fast(self, node) -> bool:
        return self._ty(node) is INT

    def _float_fast(self, node) -> bool:
        return self._ty(node) is FLOAT

    def _bool_fast(self, node) -> bool:
        return self._ty(node) is BOOL

    # ── Variable pre-declaration ──

    # ── Variable pre-declaration ──

    def _collect_nested_vars(self, stmts: list, _nested: bool = False) -> set:
        """Walk *stmts* and return variable names that are first assigned
        inside *nested* control-flow (loops, if-branches, match arms, etc.).
        Top-level assignments are excluded because they are already compiled
        with the builder positioned in the entry block — the domination
        problem only affects variables first written inside a deeper block.
        """
        names = set()
        for s in stmts:
            if isinstance(s, Assignment):
                if _nested and isinstance(s.target, Identifier):
                    names.add(s.target.name)
            elif isinstance(s, AugmentedAssignment):
                if _nested and isinstance(s.target, Identifier):
                    names.add(s.target.name)
            elif isinstance(s, ForLoop):
                # For-loop variables are always 'nested' — the for-loop
                # body is a separate basic block, so its alloca needs to
                # be in the entry block to dominate uses after the loop.
                names.add(s.var)
                names |= self._collect_nested_vars(s.body, _nested=True)
                if hasattr(s, 'else_body') and s.else_body:
                    names |= self._collect_nested_vars(s.else_body, _nested=True)
            elif isinstance(s, WhileLoop):
                names |= self._collect_nested_vars(s.body, _nested=True)
            elif isinstance(s, IfStatement):
                for _cond, body in s.branches:
                    names |= self._collect_nested_vars(body, _nested=True)
                if s.else_body:
                    names |= self._collect_nested_vars(s.else_body, _nested=True)
            elif isinstance(s, TryStatement):
                names |= self._collect_nested_vars(s.body, _nested=True)
                for _exc_type, body in s.except_clauses:
                    names |= self._collect_nested_vars(body, _nested=True)
                if s.finally_body:
                    names |= self._collect_nested_vars(s.finally_body, _nested=True)
            elif isinstance(s, MatchExpression):
                for arm in s.cases:
                    names |= self._collect_nested_vars(arm.body, _nested=True)
                    names.update(arm.params)
        return names

    def _predeclare_boxed_vars(self, stmts: list):
        """Create PTR allocas in the entry block for every local variable
        that will be assigned inside *nested* control-flow in *stmts*.
        The stores are NOT emitted here — only the allocas — so the IR
        remains valid regardless of which control-flow path the variable
        is first written on.
        """
        entry = self.func.entry_basic_block
        saved_block = self.builder.block
        self.builder.position_at_end(entry)
        for name in self._collect_nested_vars(stmts):
            if name not in self.named_values and name not in self._module_globals:
                alloca = self.builder.alloca(PTR, name=name)
                self.builder.store(ir.Constant(PTR, None), alloca)
                self.named_values[name] = alloca
                self._frame_slots.append(alloca)
        self.builder.position_at_end(saved_block)

    def _ensure_alloca(self, name: str) -> ir.AllocaInstr:
        """Boxed PTR slot for `name`, created in the entry block and
        null-initialized (NULL = fray none). Every boxed slot is created
        that way — a loop whose body never runs leaves its variable
        unwritten, and the module-exit teardown loads every slot, so both
        rely on a defined NULL rather than an uninitialized alloca."""
        if name in self.named_values:
            return self.named_values[name]
        entry = self.func.entry_basic_block
        saved_block = self.builder.block
        self.builder.position_at_end(entry)
        alloca = self.builder.alloca(PTR, name=name)
        store = self.builder.store(ir.Constant(PTR, None), alloca)
        # Slide the pair to the front of the entry block. position_at_end
        # appends after whatever is already there, and by the time a variable
        # first used inside a loop body needs its slot the entry block already
        # ends in its `br` — an instruction after a terminator is not valid
        # IR, which is what made this emitter fail with "expected instruction
        # opcode" on a program that assigns a variable inside a `while`.
        # Allocas and a NULL init are side-effect free here: entry blocks
        # carry no phis, and nothing has been stored yet (the same argument
        # _raw_slot makes when it inserts at index 0).
        if len(entry.instructions) >= 2:
            entry.instructions.pop()
            entry.instructions.pop()
        entry.instructions.insert(0, alloca)
        entry.instructions.insert(1, store)
        self.builder.position_at_end(saved_block)
        self.named_values[name] = alloca
        self._frame_slots.append(alloca)
        return alloca

    # ── Unboxed local slots (Phase 6 perf) ──
    # Function locals/params that inference proves are always INT (or
    # FLOAT) live in raw i64/f64 allocas: no retain/release traffic and
    # no boxing inside loops. Inference's join widens a slot to UNKNOWN
    # on any conflicting assignment, so a raw slot only ever holds one
    # type; as a belt-and-braces guard every boxed->raw transition goes
    # through the tag-checked fray_as_int / fray_as_float.

    def _is_param(self, name: str) -> bool:
        """True when `name` is a parameter of the function being generated."""
        if self.infer is None or not self._current_ns:
            return False
        fn = self.infer._funcs.get(self._current_ns)
        return fn is not None and name in fn.params

    def _slot_kind(self, name: str):
        """Slot representation for `name` in the current scope: 'int',
        'float', or None (boxed). Deterministic per (function, name);
        decided once and cached."""
        if self._is_module_scope() or name in self._module_globals:
            return None
        kind = self.slot_kinds.get(name)
        if kind is not None:
            # Re-verify against globals: _module_globals grows as module
            # codegen proceeds, and a cached decision from before a global
            # with this name was created must not survive.
            if name in self._module_globals:
                del self.slot_kinds[name]
                return None
            return kind
        if self._is_param(name):
            # A parameter's representation is decided by its call sites
            # and by the entry code that unboxes it — never by an
            # assignment inside the body, which is all the inferred slot
            # type can see. Using the same rule as the entry keeps the
            # slot format consistent with what is actually stored there.
            kind = self._param_kind(self._current_ns, name)
            if kind is None:
                return None
            self.slot_kinds[name] = kind
            return kind
        ty = self.infer.type_of(name, self._current_ns) if self.infer else NO_TYPE
        if ty is INT:
            kind = "int"
        elif ty is FLOAT:
            kind = "float"
        else:
            return None
        self.slot_kinds[name] = kind
        return kind

    def _declared_arity(self, fname: str):
        """Number of parameters a user-defined function declares, or None
        when the name is not one this program's inference pass saw."""
        if self.infer is None:
            return None
        fn = self.infer._funcs.get(fname)
        if fn is None:
            return None
        return len(fn.params)

    def _check_arity(self, fname: str, expected: int, given: int,
                     line: int, col: int):
        """Reject a call whose argument count disagrees with the callee.

        Without this the mismatch reaches llvmlite's `builder.call`, which
        fails as a bare `IndexError` from inside the library — a message
        that names neither the function nor the argument count."""
        if expected == given:
            return
        raise CodegenError(
            f"{self._where(line, col)}'{fname}' takes {expected} argument(s) "
            f"but {given} given")

    def _where(self, line: int, col: int) -> str:
        """`file:line:col: ` prefix for a diagnostic, when a position is
        known."""
        if not line or not self._source_file:
            return ""
        return f"{self._source_file}:{line}:{col}: "

    def _param_kind(self, fname: str, pname: str):
        """Entry representation of a parameter: raw only when every call
        site passes that type AND the body never rebinds it to another.

        Async functions keep boxed parameters: their arguments travel
        through the coroutine's argument list, so a raw slot could never be
        filled by the entry code that unpacks them."""
        if self.infer is None:
            return None
        fn = self.infer._funcs.get(fname)
        if fn is not None and getattr(fn, "is_async", False):
            return None
        sig = self.infer.sig(fname)
        if sig is None:
            return None
        ty = sig.params.get(pname, NO_TYPE)
        body_ty = self.infer.type_of(pname, fname)
        if ty is INT and body_ty in (INT, NO_TYPE):
            return "int"
        if ty is FLOAT and body_ty in (FLOAT, NO_TYPE):
            return "float"
        return None

    def _raw_slot(self, name: str, kind: str) -> ir.AllocaInstr:
        """Alloca for a raw slot, relocated to the top of the entry block
        (allocas are side-effect free; our entry blocks never contain
        phis, so index 0 is always legal)."""
        slot = self.named_values.get(name)
        if slot is not None:
            return slot
        ty = I64 if kind == "int" else DOUBLE
        slot = self.builder.alloca(ty, name=f"{name}.raw")
        blk = self.func.entry_basic_block
        if slot.parent is blk and slot in blk.instructions:
            blk.instructions.remove(slot)
            blk.instructions.insert(0, slot)
        self.named_values[name] = slot
        return slot

    def _load_raw(self, name: str, kind: str) -> ir.Value:
        return self.builder.load(self._raw_slot(name, kind))

    # ── Module-level variables as globals (Phase 6) ──
    # Spawned threads run thunks that see NO caller stack, so any variable
    # a thread touches must live in the module's global section. Main-thread
    # locals stay in allocas; globals are used whenever a name is not bound
    # in the current function's scope.

    def _global_slot(self, name: str) -> ir.GlobalVariable:
        gv = self.module.globals.get(name)
        if gv is None:
            gv = ir.GlobalVariable(self.module, PTR, name)
            gv.initializer = ir.Constant(PTR, None)  # NULL = fray none
            self._module_globals.add(name)
        return gv

    def _is_module_scope(self) -> bool:
        return self.func.name == "main"

    # ── Module-scope teardown ──
    # Reaching the end of the program does not end ownership: the module's
    # slots still hold a +1 on whatever they were last bound to, and nothing
    # will ever drop those counts. A running process doesn't care (it is
    # exiting), but a leak checker does, and "one leaked box per module-scope
    # variable" is exactly the noise that hides a real leak. So main releases
    # every slot it owns on the way out; the boxes go to the runtime's leaf
    # pool, which the runtime's exit path hands back to the allocator.
    #
    # Function frames need no equivalent here: their parameters and boxed
    # locals are released by _emit_func_return, so only the module scope ever
    # outlives its last rebind.

    def _module_slots(self):
        """Module-scope slots that own a reference: the boxed locals of
        main plus every module global."""
        slots = [(self.builder.load(slot), slot)
                 for name, slot in list(self.named_values.items())
                 if name not in self.slot_kinds and slot.type.pointee == PTR]
        for name in sorted(self._module_globals):
            gv = self._global_slot(name)
            slots.append((self.builder.load(gv), gv))
        return slots

    def _emit_module_teardown(self):
        slots = self._module_slots()
        if not slots:
            return
        # Skipped while spawned threads are still running. Today those
        # threads keep the module globals reachable through their own
        # function object, so dropping main's references is safe — but
        # that is an implementation detail of the spawn path, and the
        # process is ending anyway, so this doesn't lean on it.
        live = self._call("fray_thread_count", [])
        idle = self.builder.icmp_signed("==", live, ir.Constant(I32, 0))
        release = self._new_block("module_release")
        done = self._new_block("module_release_done")
        self.builder.cbranch(idle, release, done)
        self.builder.position_at_end(release)
        for value, slot in slots:
            self._release(value)
            self.builder.store(ir.Constant(PTR, None), slot)
        self.builder.branch(done)
        self.builder.position_at_end(done)

    def _store_releasing(self, name: str, val: ir.Value):
        """Rebind a variable: the slot owns its current value, so the old
        one must be released. First binding initializes to NULL.
        Function-local variables use allocas; module-level variables use
        globals so spawned threads share them (Phase 6). Proven-int/float
        locals live in raw unboxed slots (Phase 6 perf)."""
        kind = self._slot_kind(name)
        if kind is not None:
            slot = self._raw_slot(name, kind)
            if kind == "int":
                v = self._extract_int(val)
            else:
                v = self._call("fray_as_float", [val])
            self.builder.store(v, slot)
            self._release(val)
            return
        if name in self.named_values:
            slot = self.named_values[name]
            old = self.builder.load(slot)
            self.builder.store(val, slot)
            self._release(old)
        elif not self._is_module_scope():
            # Inside a function: check the module globals, else alloca. The
            # slot comes from _ensure_alloca so it lands in the entry block
            # (a first assignment inside a loop would otherwise allocate in a
            # block that does not dominate every use) and is registered as a
            # frame slot for the return path to drop.
            if name in self._module_globals:
                slot = self._global_slot(name)
                old = self.builder.load(slot)
                self.builder.store(val, slot)
                self._release(old)
            else:
                alloca = self._ensure_alloca(name)
                self.builder.store(val, alloca)
        else:
            gv = self._global_slot(name)
            old = self.builder.load(gv)
            self.builder.store(val, gv)
            self._release(old)

    def _load(self, name: str) -> ir.Value:
        if name in self.slot_kinds:
            raise CodegenError(
                f"internal: boxed load of raw slot '{name}' — route through "
                "_load_raw/_unbox_int/_gen_expr")
        if name in self.named_values:
            alloca = self.named_values[name]
            return self.builder.load(alloca)
        if name in self._module_globals:
            return self.builder.load(self._global_slot(name))
        return self._box_none()

    def _new_block(self, prefix: str) -> ir.Block:
        self._block_counter += 1
        return self.func.append_basic_block(f"{prefix}_{self._block_counter}")

    def _string_const(self, s: str) -> ir.Value:
        """Create a global string constant and return a pointer to it."""
        data = s.encode("utf-8") + b"\0"
        arr_type = ir.ArrayType(I8, len(data))
        elems = [ir.Constant(I8, b) for b in data]
        const = ir.Constant(arr_type, elems)
        self._str_counter += 1
        global_var = ir.GlobalVariable(self.module, arr_type, f".str.{self._str_counter}")
        global_var.initializer = const
        global_var.global_constant = True
        return self.builder.bitcast(global_var, PTR)

    def _extract_int(self, val: ir.Value) -> ir.Value:
        """Extract int64 from a FrayValue via runtime helper."""
        return self._call("fray_as_int", [val])

    # ── Main compile entry ──

    def compile(self, program: Program) -> ir.Module:
        # C main signature so a compiled program receives its command line:
        # progName()/args() read what fray_init_args stores here at startup.
        main_type = ir.FunctionType(I32, [I32, ir.PointerType(PTR, 0)], False)
        self.func = ir.Function(self.module, main_type, "main")
        self.builder = ir.IRBuilder(self.func.append_basic_block("entry"))
        self._call("fray_init_args", [self.func.args[0], self.func.args[1]])
        self._current_ns = ""
        # Names of async functions: calling one must start a coroutine
        # and return a TAG_COROUTINE handle instead of calling directly.
        self.async_fns = {s.name for s in program.body
                          if isinstance(s, FunctionDef) and s.is_async}
        # Async function bodies may contain `await`, which is legal only
        # while generating inside one of these.
        self._in_async = 0

        # Pre-declare all boxed local variables in the entry block so
        # every alloca dominates all uses (fixes LLVM domination errors
        # when a variable is first assigned inside a while-loop body).
        self._predeclare_boxed_vars(program.body)

        # Pass 1: Pre-declare all top-level function definitions so that
        # forward references between functions resolve correctly.
        # (ExternFuncDecl is handled in pass 2 — it has specific types.)
        for stmt in program.body:
            if isinstance(stmt, FunctionDef):
                n_params = len(stmt.params)
                ft = ir.FunctionType(PTR, [PTR] * n_params, False)
                if stmt.name not in self.module.globals:
                    ir.Function(self.module, ft, stmt.name)

        # Pass 2: Compile everything
        for stmt in program.body:
            self._gen_stmt(stmt)

        if not self.builder.block.is_terminated:
            self._emit_module_teardown()
            self.builder.ret(ir.Constant(I32, 0))
        return self.module

    # ── Statement generation ──

    def _gen_stmt(self, node: Node):
        if isinstance(node, Assignment):
            if isinstance(node.target, Index):
                # x[i] = v : container subscript store (list or map)
                obj = self._gen_expr(node.target.obj)
                idx = self._gen_expr(node.target.index_expr)
                val = self._gen_expr(node.value)
                # Runtime tag dispatch: check if object is a map
                tag_ptr = self.builder.bitcast(obj, ir.PointerType(I8, 0))
                tag = self.builder.load(tag_ptr)
                is_map = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_MAP))
                do_map = self._new_block("set_map")
                do_list = self._new_block("set_list")
                done = self._new_block("set_done")
                self.builder.cbranch(is_map, do_map, do_list)
                # Map path: fray_map_set(obj, key, val) — takes ownership of key and val
                self.builder.position_at_end(do_map)
                self._call("fray_map_set", [obj, idx, val])
                # map_set takes ownership of idx and val — no releases needed
                self.builder.branch(done)
                # List path: fray_list_setindex(obj, idx_int, val) — does NOT take ownership
                self.builder.position_at_end(do_list)
                idx_int = self._extract_int(idx)
                self._call("fray_list_setindex", [obj, idx_int, val])
                self._release(val)  # list_setindex doesn't consume val
                self._release(idx)  # list path: idx not consumed
                self.builder.branch(done)
                self.builder.position_at_end(done)
                self._release(obj)
                return
            if isinstance(node.target, Identifier):
                kind = self._slot_kind(node.target.name)
                if kind == "int" and self._ty(node.value) is INT:
                    # Zero-alloc raw store (Phase 6 perf).
                    raw = self._unbox_int(node.value)
                    self.builder.store(raw, self._raw_slot(node.target.name, "int"))
                    return
                if kind == "float" and self._ty(node.value) is FLOAT:
                    raw = self._unbox_float(node.value)
                    self.builder.store(raw, self._raw_slot(node.target.name, "float"))
                    return
            val = self._gen_expr(node.value)
            if isinstance(node.target, Identifier):
                self._store_releasing(node.target.name, val)
            elif isinstance(node.target, MemberAccess):
                # t.field = val : struct field set
                # field_set takes ownership of val; string constant is raw
                # bytes (not a FrayValue) so must not be released.
                obj = self._gen_expr(node.target.obj)
                field_str = self._string_const(node.target.attr)
                self._call("fray_struct_field_set", [obj, field_str, val])
                self._release(obj)
            else:
                self._release(val)
        elif isinstance(node, AugmentedAssignment):
            base_op = node.op[:-1]
            op_map = {
                "+": "fray_add", "-": "fray_sub",
                "*": "fray_mul", "/": "fray_div",
                "//": "fray_floordiv", "%": "fray_mod",
                "^": "fray_pow",
            }
            if isinstance(node.target, Identifier) and base_op in op_map:
                if self._slot_kind(node.target.name) == "int":
                    # Raw int slot: stay raw end-to-end — every fast path
                    # (inline %, floordiv, pow) applies, no boxing. A raw
                    # *float* slot must fall through: _raw_binop_i64 is an
                    # integer op, and storing its i64 into a double* is a
                    # type error (`acc = 0.0; acc += i` inside a counted loop).
                    # The boxed path below re-boxes and _store_releasing
                    # unboxes back into the raw slot correctly.
                    binop = BinaryOp(op=base_op,
                                     left=Identifier(name=node.target.name),
                                     right=node.value)
                    raw = self._raw_binop_i64(binop)
                    slot = self._raw_slot(node.target.name,
                                          self.slot_kinds[node.target.name])
                    self.builder.store(raw, slot)
                else:
                    kind = self._slot_kind(node.target.name)
                    if kind is None:
                        cur = self._load(node.target.name)
                        self._call("fray_retain", [cur])
                    else:
                        # Raw float slot: there is no reference to load, so
                        # _load refuses it. Read the double straight out of
                        # the slot and box it — fray_float returns a fresh box
                        # we already own, hence no retain here. The result
                        # goes back through _store_releasing, which unboxes
                        # into the raw slot again.
                        cur = self._box_float(self.builder.load(
                            self._raw_slot(node.target.name, kind)))
                    val = self._gen_expr(node.value)
                    result = self._call(op_map[base_op], [cur, val])
                    self._release(cur)
                    self._release(val)
                    self._store_releasing(node.target.name, result)
            elif isinstance(node.target, Index) and base_op in op_map:
                obj = self._gen_expr(node.target.obj)
                idx = self._gen_expr(node.target.index_expr)
                # Tag dispatch: map (fray_map_set takes ownership of val)
                # vs list (setindex does not).
                tag_ptr = self.builder.bitcast(obj, ir.PointerType(I8, 0))
                tag = self.builder.load(tag_ptr)

                do_map = self._new_block("aug_map")
                do_list = self._new_block("aug_list")
                done = self._new_block("aug_done")

                is_map = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_MAP))
                self.builder.cbranch(is_map, do_map, do_list)

                self.builder.position_at_end(do_map)
                cur_m = self._call("fray_map_get", [obj, idx])  # returns +1
                val_m = self._gen_expr(node.value)
                res_m = self._call(op_map[base_op], [cur_m, val_m])
                self._release(cur_m)
                self._release(val_m)
                self._call("fray_map_set", [obj, idx, res_m])  # takes ownership of res_m
                self.builder.branch(done)

                self.builder.position_at_end(do_list)
                idx_int = self._extract_int(idx)
                cur_l = self._call("fray_list_index", [obj, idx_int])
                val_l = self._gen_expr(node.value)
                res_l = self._call(op_map[base_op], [cur_l, val_l])
                self._release(cur_l)
                self._release(val_l)
                self._call("fray_list_setindex", [obj, idx_int, res_l])
                self._release(res_l)
                self._release(idx)
                self.builder.branch(done)

                self.builder.position_at_end(done)
                self._release(obj)
            elif isinstance(node.target, MemberAccess) and base_op in op_map:
                obj = self._gen_expr(node.target.obj)
                field_str = self._string_const(node.target.attr)
                cur = self._call("fray_struct_field_get", [obj, field_str])  # returns +1
                val = self._gen_expr(node.value)
                result = self._call(op_map[base_op], [cur, val])
                self._release(cur)
                self._release(val)
                # field_set takes ownership of result — no release of result.
                self._call("fray_struct_field_set", [obj, field_str, result])
                self._release(obj)
            else:
                raise CodegenError(f"unsupported augmented assignment '{node.op}'")
        elif isinstance(node, ExprStatement):
            val = self._gen_expr(node.expr)
            self._release(val)
        elif isinstance(node, ReturnStatement):
            if self._raw_ret is not None:
                # Unboxed body: return a raw value.
                if self._try_ctx:
                    self._gen_deferred_raw_return(node)
                else:
                    self._emit_raw_return(node)
                return
            val = self._gen_expr(node.value) if node.value else self._box_none()
            if self.func.return_value.type == PTR:
                if self._try_ctx:
                    # Inside a try body a return is not final: the expression
                    # that produced this value may have thrown, and the
                    # matching clause has to win (see _gen_deferred_return).
                    self._gen_deferred_return(val)
                else:
                    self._emit_func_return(val)
            else:
                # Module level: main() returns i32.
                self._release(val)
                if not self.builder.block.is_terminated:
                    self._emit_module_teardown()
                    self.builder.ret(ir.Constant(I32, 0))
        elif isinstance(node, ConstStatement):
            val = self._gen_expr(node.value)
            self._store_releasing(node.name, val)
        elif isinstance(node, FunctionDef):
            self._gen_function_def(node)
        elif isinstance(node, StructDef):
            # Register the struct definition — no runtime code emitted.
            self.struct_defs[node.name] = [f.name for f in node.fields]
        elif isinstance(node, EnumDef):
            # Register the enum definition — treat each variant as a struct
            # with a _variant tag field + flattened params from all variants.
            cases = [(c.name, c.params) for c in node.cases]
            # Collect all unique param names across all variants. Besides the
            # _variant tag, every enum instance carries the enum's own name in
            # _enum: a qualified match arm ("case Shape.Circle(r):") is checked
            # against it, so two enums that share a variant name stay distinct
            # (the struct has no other per-instance type identity).
            all_fields = ["_variant", "_enum"]
            for vname, vparams in cases:
                for p in vparams:
                    if p not in all_fields:
                        all_fields.append(p)
            self.struct_defs[node.name] = all_fields
            self.enum_defs[node.name] = {"cases": cases, "fields": all_fields}
        elif isinstance(node, IfStatement):
            self._gen_if(node)
        elif isinstance(node, ForLoop):
            self._gen_for(node)
        elif isinstance(node, WhileLoop):
            self._gen_while(node)
        elif isinstance(node, TryStatement):
            self._gen_try(node)
        elif isinstance(node, ExternFuncDecl):
            self._gen_extern_decl(node)
        elif isinstance(node, ImportStatement):
            self._gen_import(node)
        elif isinstance(node, BreakStatement):
            if hasattr(self, '_loop_break'):
                if not self.builder.block.is_terminated:
                    self.builder.branch(self._loop_break)
        elif isinstance(node, ContinueStatement):
            # `continue` branches to the loop's own continuation point: the
            # increment block of a for loop, the condition preheader of a
            # while loop, the counter preheader of a counted range loop.
            # Without one there is no loop to continue.
            target = getattr(self, "_loop_continue", None)
            if target is not None:
                if not self.builder.block.is_terminated:
                    self.builder.branch(target)
        elif isinstance(node, DelStatement):
            self._gen_del(node)
        elif isinstance(node, MatchExpression):
            self._gen_match(node)
    def _gen_match(self, node: MatchExpression):
        """Codegen for match expression — tag dispatch on enum variants.
        Returns the arm's value (a trailing expression statement is the
        arm result, matching the oracle's MatchExpression evaluation)."""
        subject = self._gen_expr(node.subject)
        variant_str = self._string_const("_variant")
        variant_val = self._call("fray_struct_field_get", [subject, variant_str])
        # Result slot: created at the current position (dominates merge)
        result_ptr = self.builder.alloca(PTR, name="match_result")
        self.builder.store(ir.Constant(PTR, None), result_ptr)
        merge = self._new_block("match_merge")
        for i, arm in enumerate(node.cases):
            # Look up the field names the arm's params bind to. A qualified arm
            # ("case Shape.Circle(r):") names its enum, so it takes that enum's
            # variant fields — and an enum it does not name is one the arm can
            # never match. An unqualified arm has only the variant name to go
            # on, which the oracle resolves against the subject at run time;
            # one of the enums declaring the variant is the best static lookup.
            actual_field_names = []
            if arm.enum_name:
                einfo = self.enum_defs.get(arm.enum_name)
                if einfo is not None:
                    for vn, vp in einfo["cases"]:
                        if vn == arm.variant_name:
                            actual_field_names = vp
                            break
            else:
                # Unqualified arm: the variant name is all there is. The oracle
                # binds against the subject's own enum at run time; statically,
                # the last enum declaring the variant wins — the same lookup
                # the self-hosted codegen does.
                for ename, einfo in self.enum_defs.items():
                    for vn, vp in einfo["cases"]:
                        if vn == arm.variant_name:
                            actual_field_names = vp
            if arm.variant_name == "_":
                # Wildcard — fall through from previous case or straight in
                if not self.builder.block.is_terminated:
                    pass  # already positioned correctly
                # Bind params
                for pi, pname in enumerate(arm.params):
                    if pi < len(actual_field_names):
                        field_name = actual_field_names[pi]
                    else:
                        field_name = pname
                    field_str = self._string_const(field_name)
                    pval = self._call("fray_struct_field_get", [subject, field_str])
                    self._store_releasing(pname, pval)
                for si, stmt in enumerate(arm.body):
                    if si == len(arm.body) - 1 and isinstance(stmt, ExprStatement):
                        val = self._gen_expr(stmt.expr)
                        self.builder.store(val, result_ptr)
                    else:
                        self._gen_stmt(stmt)
                if not self.builder.block.is_terminated:
                    self.builder.branch(merge)
                continue
            # Compare variant string
            case_label = self._new_block(f"match_case_{arm.variant_name}")
            next_case = self._new_block(f"match_next_{i}")
            case_str = self._call("fray_string_copy", [self._string_const(arm.variant_name)])
            cmp = self._call("fray_eq", [variant_val, case_str])
            cmp_bool = self._call("fray_is_truthy", [cmp])
            self._release(cmp)
            self._release(case_str)
            if arm.enum_name:
                # A qualified arm also has to be an instance of the enum it
                # names: the variant name alone would let a same-named variant
                # of another enum match. The subject carries its enum name in
                # _enum (see _gen_enum_new).
                subject_enum = self._call(
                    "fray_struct_field_get",
                    [subject, self._string_const("_enum")])
                qual = self._call(
                    "fray_string_copy", [self._string_const(arm.enum_name)])
                enum_cmp = self._call("fray_eq", [subject_enum, qual])
                enum_bool = self._call("fray_is_truthy", [enum_cmp])
                self._release(enum_cmp)
                self._release(qual)
                self._release(subject_enum)
                cmp_bool = self.builder.and_(cmp_bool, enum_bool)
            self.builder.cbranch(cmp_bool, case_label, next_case)
            # Case body
            self.builder.position_at_end(case_label)
            for pi, pname in enumerate(arm.params):
                if pi < len(actual_field_names):
                    field_name = actual_field_names[pi]
                else:
                    field_name = pname
                field_str = self._string_const(field_name)
                pval = self._call("fray_struct_field_get", [subject, field_str])
                self._store_releasing(pname, pval)
            for si, stmt in enumerate(arm.body):
                if si == len(arm.body) - 1 and isinstance(stmt, ExprStatement):
                    val = self._gen_expr(stmt.expr)
                    self.builder.store(val, result_ptr)
                else:
                    self._gen_stmt(stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(merge)
            self.builder.position_at_end(next_case)
        # Terminate the final fallthrough block
        if not self.builder.block.is_terminated:
            self.builder.branch(merge)
        self.builder.position_at_end(merge)
        self._release(variant_val)
        self._release(subject)
        return self.builder.load(result_ptr)

    def _gen_extern_decl(self, node: ExternFuncDecl):
        """Declare an external C function in the LLVM module."""
        # Type mapping: fray type → LLVM type
        TYPE_MAP = {
            "int": I64, "i64": I64, "i32": I64, "i16": I64, "i8": I8,
            "u8": I8, "u16": I64, "u32": I64, "u64": I64,
            "float": DOUBLE, "f64": DOUBLE, "f32": DOUBLE,
            "bool": ir.IntType(1),
            "string": PTR, "char": I8, "ptr": PTR, "void": None,
        }
        # Return type
        ret = TYPE_MAP.get(node.return_type)
        if ret is None and node.return_type != "void":
            ret = I64  # default to i64 for unknown types
        if node.return_type == "void":
            ret = None
        # Param types
        params = [TYPE_MAP.get(t, I64) for t in node.param_types]
        # Create LLVM function type
        if ret is None:
            ft = ir.FunctionType(ir.VoidType(), params)
        else:
            ft = ir.FunctionType(ret, params)
        # Declare as external (no body)
        func = ir.Function(self.module, ft, node.name)
        # Track in extern_decls for the call handler
        self.extern_decls[node.name] = (func, node)

    def _gen_enum_new(self, enum_name: str, enum_info: dict, variant_name: str) -> ir.Value:
        """Create an enum variant instance as a struct with _variant tag + payload fields."""
        all_fields = enum_info["fields"]
        # Build global field name array (reuse struct creation pattern)
        arr_name = f".enum_fields.{enum_name}"
        if arr_name not in self.module.globals:
            str_globals = []
            for fn in all_fields:
                data = fn.encode("utf-8") + b"\0"
                arr_type = ir.ArrayType(I8, len(data))
                const = ir.Constant(arr_type, [ir.Constant(I8, b) for b in data])
                gv = ir.GlobalVariable(self.module, arr_type, f".enum_field.{enum_name}.{fn}")
                gv.initializer = const
                gv.global_constant = True
                gv.linkage = "internal"
                str_globals.append(gv)
            fields_arr_type = ir.ArrayType(PTR, len(all_fields))
            arr = ir.GlobalVariable(self.module, fields_arr_type, arr_name)
            arr.initializer = ir.Constant(fields_arr_type, str_globals)
            arr.global_constant = True
            arr.linkage = "internal"
        name_str = self._string_const(enum_name)
        nfields = ir.Constant(I64, len(all_fields))
        arr_ptr = self.builder.bitcast(self.module.get_global(arr_name), PTR)
        sv = self._call("fray_struct_new", [name_str, nfields, arr_ptr])
        # Set _variant field — field name is raw string, value must be a FrayValue
        variant_val = self._call("fray_string_copy", [self._string_const(variant_name)])
        self._call("fray_struct_field_set",
                    [sv, self._string_const("_variant"), variant_val])
        # Set _enum the same way, so a qualified match arm can tell a Color
        # instance from a Shape one that happens to share a variant name.
        enum_val = self._call("fray_string_copy", [self._string_const(enum_name)])
        self._call("fray_struct_field_set",
                    [sv, self._string_const("_enum"), enum_val])
        return sv

    def _make_module_alias(self, alias_name: str, src_name: str):
        """Create `alias_name` as a thin wrapper over the compiled `src_name`,
        so a qualified call `module.f()` resolves at the call site."""
        if alias_name in self.module.globals or src_name not in self.module.globals:
            return
        src_fn = self.module.globals[src_name]
        if not isinstance(src_fn, ir.Function):
            return
        ft = src_fn.function_type
        alias = ir.Function(self.module, ft, alias_name)
        block = alias.append_basic_block("entry")
        old_block = self.builder.block
        old_func = self.func
        self.builder.position_at_end(block)
        self.func = alias
        result = self.builder.call(src_fn, list(alias.args))
        if ft.return_type == ir.VoidType():
            self.builder.ret_void()
        else:
            self.builder.ret(result)
        self.builder.position_at_end(old_block)
        self.func = old_func

    def _gen_import(self, node: ImportStatement):
        """Generate code for import statements — inline module definitions with namespaced names.

        A module path resolves under the program root either as a plain module
        file or as a package initializer (`pkg/__init__.fray`), and leading
        dots make it relative to the importing module's package. That is what
        lets a package be split across files: the initializer imports its
        siblings, and the names it imports are what `from pkg import name`
        (and a qualified `pkg.name()`) see.
        """
        from ast_nodes import FunctionDef as _FD, ExternFuncDecl as _EFD

        target = modules.resolve_relative(node.module, node.level, self._package)
        if target is None:
            if node.level:
                raise CodegenError(
                    f"attempted relative import with no known parent package: "
                    f"{'.' * node.level}{node.module}")
            raise CodegenError("import needs a module path")

        module_path, is_package = modules.find_module_file(self._root, target)
        if module_path is None:
            looked = os.path.normpath(os.path.join(
                self._root, target.replace(".", os.sep) + ".fray"))
            raise CodegenError(f"module '{target}' not found (looked for {looked})")

        if target not in self._imported_modules:
            # Read, parse, and compile the module
            with open(module_path, encoding="utf-8") as f:
                source = f.read()

            tokens = tokenize(source, module_path)
            mod_ast = parse(tokens, module_path)
            self._module_asts[target] = mod_ast
            # Mark it inlined *before* generating it: a package initializer
            # that imports one of its own submodules (`from . import util`)
            # reaches the package again while it is still being generated.
            self._imported_modules.add(target)

            # Save and restore state
            saved_block = self.builder.block
            saved_func = self.func
            saved_ns = self._current_ns
            saved_in_async = self._in_async
            saved_pkg = self._package
            saved_src = self._source_file
            # Diagnostics from this module name this file, not the program.
            self._source_file = module_path
            self._in_async = 0
            # A module's own imports resolve against its package: an
            # initializer is its own package, a plain module's package is the
            # directory holding it.
            self._package = modules.package_of(target, is_package)

            # --- Pass 1: Pre-declare all functions so forward references
            #     within the module resolve correctly ---
            for stmt in mod_ast.body:
                if isinstance(stmt, _FD):
                    n_params = len(stmt.params)
                    ft = ir.FunctionType(PTR, [PTR] * n_params, False)
                    if stmt.name not in self.module.globals:
                        ir.Function(self.module, ft, stmt.name)
                elif isinstance(stmt, _EFD):
                    n_params = len(stmt.param_types)
                    ft = ir.FunctionType(PTR, [PTR] * n_params, False)
                    if stmt.name not in self.module.globals:
                        ir.Function(self.module, ft, stmt.name)
                elif isinstance(stmt, StructDef):
                    self.struct_defs[stmt.name] = [f.name for f in stmt.fields]
                elif isinstance(stmt, EnumDef):
                    cases = [(c.name, c.params) for c in stmt.cases]
                    all_fields = ["_variant", "_enum"]
                    for vname, vparams in cases:
                        for p in vparams:
                            if p not in all_fields:
                                all_fields.append(p)
                    self.struct_defs[stmt.name] = all_fields
                    self.enum_defs[stmt.name] = {"cases": cases, "fields": all_fields}
                elif isinstance(stmt, ImportStatement):
                    self._gen_import(stmt)

            # The module's own top-level bindings are globals, and their
            # slots have to exist before the function bodies below are
            # compiled: a function that reads one must find the module's slot
            # rather than invent a local for itself. (A name with no slot
            # anywhere reads back as none, which is what an imported module's
            # state used to do.)
            for stmt in mod_ast.body:
                if isinstance(stmt, ConstStatement):
                    self._global_slot(stmt.name)
                elif (isinstance(stmt, Assignment)
                      and isinstance(stmt.target, Identifier)):
                    self._global_slot(stmt.target.name)

            # --- Pass 2: Compile function bodies ---
            for stmt in mod_ast.body:
                if isinstance(stmt, _FD):
                    self._gen_function_def(stmt)
                elif isinstance(stmt, _EFD):
                    self._gen_extern_decl(stmt)

            # --- Pass 3: The module's own top-level statements. A module
            #     body is what sets up its state, and it runs here, in the
            #     order the file writes it, with those bindings in module
            #     scope: the same statements the native linker merges in ahead
            #     of the program. The function bodies above were compiled
            #     first, so they read the slots this fills in.
            if any(not isinstance(stmt, (_FD, _EFD, StructDef, EnumDef,
                                         ImportStatement))
                   for stmt in mod_ast.body):
                if not self._is_module_scope():
                    raise CodegenError(
                        f"module '{target}' is imported inside a function: an "
                        "import belongs at the top level, where its module "
                        "body has a module scope to run in")
                for stmt in mod_ast.body:
                    if isinstance(stmt, (_FD, _EFD, StructDef, EnumDef,
                                         ImportStatement)):
                        continue
                    self._gen_stmt(stmt)

            self.builder.position_at_end(saved_block)
            self.func = saved_func
            self._current_ns = saved_ns
            self._in_async = saved_in_async
            self._package = saved_pkg
            self._source_file = saved_src

        mod_ast = self._module_asts.get(target)

        # Bind names into the current scope
        if node.from_import:
            # With original-name compilation, functions are already available
            # by their real names. Verify they exist but no wrapper needed.
            # A name may also be a submodule of a package (`from . import util`).
            for name in node.names:
                if name in self.module.globals:
                    continue
                sub_name = f"{target}.{name}"
                sub_path, _sub_pkg = modules.find_module_file(self._root, sub_name)
                if sub_path is not None:
                    self._gen_import(ImportStatement(module=sub_name, level=0,
                                                     line=node.line, col=node.col))
                    # `from . import util` binds `util`, so `util.f()` needs
                    # aliases under the local name, not the full path.
                    sub_ast = self._module_asts.get(sub_name)
                    if sub_ast is not None:
                        for stmt in sub_ast.body:
                            if isinstance(stmt, _FD):
                                self._make_module_alias(f"{name}.{stmt.name}", stmt.name)
                    continue
                raise CodegenError(f"'{target}' has no exported '{name}'")
        else:
            # `import foo` — create alias functions: foo.bar → bar
            # so that foo.bar() calls resolve at call-site. A package's
            # re-exported names (`from .util import twice`) get aliases too.
            for stmt in mod_ast.body:
                if isinstance(stmt, _FD):
                    self._make_module_alias(f"{target}.{stmt.name}", stmt.name)
            for stmt in mod_ast.body:
                if isinstance(stmt, ImportStatement) and stmt.from_import:
                    for name in stmt.names:
                        self._make_module_alias(f"{target}.{name}", name)

    def _gen_del(self, node: DelStatement):
        """Generate code for: del target"""
        target = node.target
        if isinstance(target, Index):
            obj = self._gen_expr(target.obj)
            key = self._gen_expr(target.index_expr)
            self._call("fray_map_del", [obj, key])
            self._release(obj)
            self._release(key)
        elif isinstance(target, Identifier):
            # del variable — just release it
            if self._slot_kind(target.name) is not None:
                # Raw slot: there is no reference to release, so rebind it
                # the way any other store would (the value becomes none).
                self._store_releasing(target.name, self._box_none())
                return
            val = self._load(target.name)
            if val is not None:
                self._call("fray_retain", [val])  # load already retained
                self._store_releasing(target.name, self._box_none())
        else:
            raise CodegenError(f"invalid del target")

    # ── Control flow ──

    def _gen_cond(self, node) -> ir.Value:
        """Generate an i1 condition. Fast paths read the operand unboxed;
        every path releases any temporaries it generates."""
        ty = self._ty(node)
        if ty is BOOL and isinstance(node, BinaryOp) and node.op in (
                "==", "!=", "<", ">", "<=", ">="):
            # Numeric comparison as condition: produce the i1 directly,
            # skipping the box -> truthy round-trip (loop-critical).
            lt, rt = self._ty(node.left), self._ty(node.right)
            if lt in (INT, BOOL) and rt in (INT, BOOL):
                return self._icmp(node.op,
                                  self._unbox_int(node.left),
                                  self._unbox_int(node.right))
            if lt is FLOAT and rt is FLOAT:
                return self._fcmp(node.op,
                                  self._unbox_float(node.left),
                                  self._unbox_float(node.right))
        if ty is INT:
            iv = self._unbox_int(node)
            return self.builder.icmp_signed("!=", iv, ir.Constant(I64, 0))
        if ty is FLOAT:
            fv = self._unbox_float(node)
            return self.builder.fcmp_one(fv, ir.Constant(DOUBLE, 0.0))
        if ty is BOOL and isinstance(node, Identifier):
            if self._slot_kind(node.name) == "int":
                return self.builder.icmp_signed(
                    "!=", self._load_raw(node.name, "int"), ir.Constant(I64, 0))
            v = self._load(node.name)
            self._call("fray_retain", [v])
            p = self.builder.bitcast(v, ir.PointerType(I8, 0))
            raw = self.builder.load(self.builder.gep(
                p, [ir.Constant(I32, OFF_PAYLOAD)]))
            self._release(v)
            return self.builder.icmp_unsigned("!=", raw, ir.Constant(I8, 0))
        cond_val = self._gen_expr(node)
        truthy = self._call("fray_is_truthy", [cond_val])
        self._release(cond_val)
        return truthy

    def _gen_if(self, node: IfStatement):
        end_block = self._new_block("if_end")
        next_block = self._new_block("if_next")

        for i, (cond, body) in enumerate(node.branches):
            then_block = self._new_block("if_then")

            truthy = self._gen_cond(cond)
            self.builder.cbranch(truthy, then_block, next_block)

            self.builder.position_at_end(then_block)
            for stmt in body:
                self._gen_stmt(stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(end_block)

            self.builder.position_at_end(next_block)
            if i < len(node.branches) - 1:
                next_block = self._new_block("if_next")

        if node.else_body:
            for stmt in node.else_body:
                self._gen_stmt(stmt)

        if not self.builder.block.is_terminated:
            self.builder.branch(end_block)
        self.builder.position_at_end(end_block)

    def _gen_for(self, node: ForLoop):
        """For loops. Specializes `for i in range(a, b, s)` to an unboxed
        integer loop when the loop variable is proven int-typed; otherwise
        uses the generic boxed iteration."""
        if (isinstance(node.iterable, Call)
                and isinstance(node.iterable.func, Identifier)
                and node.iterable.func.name == "range"
                and self._int_fast(Identifier(name=node.var))
                and self._range_step_static(node.iterable)):
            self._gen_for_range_fast(node)
            return

        iterable_val = self._gen_expr(node.iterable)

        # Tag-dispatch: if iterable is a map, convert to keys list first
        tag_ptr = self.builder.bitcast(iterable_val, ir.PointerType(I8, 0))
        tag = self.builder.load(tag_ptr)
        is_map = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_MAP))
        do_map_iter = self._new_block("for_map_iter")
        do_normal_iter = self._new_block("for_normal_iter")
        iter_merge = self._new_block("for_iter_merge")
        self.builder.cbranch(is_map, do_map_iter, do_normal_iter)

        self.builder.position_at_end(do_map_iter)
        keys_list = self._call("fray_map_keys", [iterable_val])
        self._release(iterable_val)
        self.builder.branch(iter_merge)

        self.builder.position_at_end(do_normal_iter)
        self.builder.branch(iter_merge)

        self.builder.position_at_end(iter_merge)
        phi_iter = self.builder.phi(PTR)
        phi_iter.add_incoming(keys_list, do_map_iter)
        phi_iter.add_incoming(iterable_val, do_normal_iter)
        iterable_val = phi_iter

        preheader = self._new_block("for_preheader")
        incr_block = self._new_block("for_incr")
        loop_body = self._new_block("for_body")
        loop_exit = self._new_block("for_exit")
        break_block = self._new_block("for_break")

        # Store loop_exit for break statements
        self._loop_exit = loop_exit
        self._loop_break = break_block
        # `continue` lands on the increment, not on the preheader: the
        # preheader seeds the index at 0, so branching back to it would
        # restart the loop from the beginning forever.
        old_continue = getattr(self, "_loop_continue", None)
        self._loop_continue = incr_block

        self.builder.branch(preheader)
        self.builder.position_at_end(preheader)

        # Get length. The raw helper avoids a box per loop entry — and the
        # loop re-checks the bound every iteration (the iterable may be
        # mutated by the body), so a boxed length would allocate there too.
        len_int = self._call("fray_len_raw", [iterable_val])

        # Init index
        idx_alloca = self.builder.alloca(I64, name=f"{node.var}_idx")
        self.builder.store(ir.Constant(I64, 0), idx_alloca)

        # Pre-create the loop variable alloca BEFORE the loop body
        # so it's available after the loop exits
        loop_var_alloca = self._ensure_alloca(node.var)

        # Check condition
        idx_val = self.builder.load(idx_alloca)
        cond = self.builder.icmp_signed("<", idx_val, len_int)
        self.builder.cbranch(cond, loop_body, loop_exit)

        # Loop body
        self.builder.position_at_end(loop_body)
        idx_val = self.builder.load(idx_alloca)
        elem = self._call("fray_list_index", [iterable_val, idx_val])
        self._store_releasing(node.var, elem)

        for stmt in node.body:
            self._gen_stmt(stmt)

        # Increment. The body's fall-through and any `continue` in it both
        # arrive here; a body that ended in break/return leaves the block
        # already terminated, so the branch is conditional.
        if not self.builder.block.is_terminated:
            self.builder.branch(incr_block)
        self.builder.position_at_end(incr_block)
        idx_val = self.builder.load(idx_alloca)
        new_idx = self.builder.add(idx_val, ir.Constant(I64, 1))
        self.builder.store(new_idx, idx_alloca)

        # Re-check
        len_int2 = self._call("fray_len_raw", [iterable_val])
        idx_val = self.builder.load(idx_alloca)
        cond = self.builder.icmp_signed("<", idx_val, len_int2)
        self.builder.cbranch(cond, loop_body, loop_exit)

        # Break block branches to exit
        self.builder.position_at_end(break_block)
        self.builder.branch(loop_exit)

        self.builder.position_at_end(loop_exit)
        # The iterable is owned by the loop and dies with it. The loop
        # variable's final binding stays owned by its alloca (released on
        # rebind or at scope teardown).
        self._release(iterable_val)
        self._loop_continue = old_continue

    @staticmethod
    def _static_step_value(node) -> int:
        """The int value of a range() step argument when it is written as a
        constant, else 0. A unary minus over an int literal counts as a
        constant: what the loop needs is the step's *sign*, and `range(4, 0,
        -1)` spells it out just as plainly as `range(0, 4, 1)`."""
        if isinstance(node, IntLiteral):
            return node.value
        if (isinstance(node, UnaryOp) and node.op == "-"
                and isinstance(node.operand, IntLiteral)):
            return -node.operand.value
        return 0

    def _range_step_static(self, call: Call) -> bool:
        """The unboxed range loop picks its comparison predicate from the
        step's sign, so the step must be a non-zero constant (or absent). A
        literal zero step stays on the generic path, where the runtime raises
        the catchable ValueError the oracle's programs can trap — the same
        choice the self-hosted emitter makes."""
        if len(call.args) < 3:
            return True
        return self._static_step_value(call.args[2]) != 0

    def _gen_for_range_fast(self, node: ForLoop):
        """Unboxed `for i in range(...)` loop: i lives in an i64 SSA value
        across the loop; boxing happens only if the body stores it."""
        call = node.iterable
        args = call.args
        if len(args) == 1:
            start, stop, step = None, args[0], None
        elif len(args) == 2:
            start, stop, step = args[0], args[1], None
        else:
            start, stop, step = args[0], args[1], args[2]

        # Evaluate bounds once (owned boxed values, then unbox + release)
        def bound_val(arg, default):
            if arg is None:
                return ir.Constant(I64, default)
            v = self._gen_expr(arg)
            iv = self._extract_int(v)
            self._release(v)
            return iv

        start_v = bound_val(start, 0)
        stop_v = bound_val(stop, None) if stop is not None else ir.Constant(I64, 0)
        step_v = bound_val(step, 1)
        if step is None:
            step_v = ir.Constant(I64, 1)

        neg_step = step is not None and self._static_step_value(step) < 0
        # Zero step: match the oracle (runtime raises ValueError).
        zero_check = self.builder.icmp_signed("==", step_v, ir.Constant(I64, 0))
        zero_block = self._new_block("range_zero")
        check_ok = self._new_block("range_ok")
        self.builder.cbranch(zero_check, zero_block, check_ok)
        self.builder.position_at_end(zero_block)
        self._throw_const(2, "ValueError: range() step must not be zero")
        self._call("fray_list", [])  # C fallback value: empty list
        self.builder.unreachable()

        self.builder.position_at_end(check_ok)
        loop_preheader = self._new_block("range_preheader")
        loop_body = self._new_block("for_body")
        loop_exit = self._new_block("for_exit")
        break_block = self._new_block("for_break")
        self._loop_exit = loop_exit
        self._loop_break = break_block
        # The increment lives in the preheader, so `continue` needs a block
        # of its own only in the sense that it targets this one: the step is
        # still applied on the way round.
        old_continue = getattr(self, "_loop_continue", None)
        self._loop_continue = loop_preheader

        # Loop variable: raw slot when proven int (no per-iteration box);
        # otherwise a boxed alloca the body reads as a value.
        if self._slot_kind(node.var) is None:
            is_new_var = node.var not in self.named_values
            loop_var_alloca = self._ensure_alloca(node.var)
            if is_new_var:
                self.builder.store(ir.Constant(PTR, None), loop_var_alloca)
        idx_alloca = self.builder.alloca(I64, name=f"{node.var}_idx")
        # The counter holds the *previous* index: the preheader adds the step
        # to produce the current one, so seeding it with start - step makes
        # the first pass compute start.
        self.builder.store(self.builder.sub(start_v, step_v), idx_alloca)

        self.builder.branch(loop_preheader)
        self.builder.position_at_end(loop_preheader)
        # Increment + condition: idx < stop (step > 0) or idx > stop (step < 0)
        cur = self.builder.load(idx_alloca)
        new_idx = self.builder.add(cur, step_v)
        self.builder.store(new_idx, idx_alloca)

        cur = self.builder.load(idx_alloca)
        if neg_step:
            cond = self.builder.icmp_signed(">", cur, stop_v)
        else:
            cond = self.builder.icmp_signed("<", cur, stop_v)
        self.builder.cbranch(cond, loop_body, loop_exit)

        self.builder.position_at_end(loop_body)
        cur = self.builder.load(idx_alloca)
        # Bind the loop variable: raw slot takes the machine int directly;
        # a boxed slot takes ownership of a fresh box.
        if self._slot_kind(node.var) == "int":
            self.builder.store(cur, self._raw_slot(node.var, "int"))
        else:
            boxed = self._box_int(cur)
            self._store_releasing(node.var, boxed)

        for stmt in node.body:
            self._gen_stmt(stmt)

        if not self.builder.block.is_terminated:
            self.builder.branch(loop_preheader)

        self.builder.position_at_end(break_block)
        self.builder.branch(loop_exit)

        self.builder.position_at_end(loop_exit)
        self._loop_continue = old_continue

    def _emit_func_return(self, val: ir.Value):
        """Return from a user function. The callee owns its parameters *and*
        its boxed locals — every slot in _frame_slots holds a reference of its
        own — so drop them all before handing back the (owned) return value.

        The returned value is unaffected: reading a slot retains, so `return x`
        hands back a reference of its own that outlives the slot (the same
        reason the self-hosted emitter's gen_slot_release is safe)."""
        for alloca in self._frame_slots:
            p = self.builder.load(alloca)
            self._release(p)
        self.builder.ret(val)

    def _gen_spawn_thunk(self, thunk_name: str, llfn: ir.Function):
        """Emit `void thunk(FrayValue self_fn)` that calls a 0-arg user
        function. The runtime invokes this on the spawned C thread; the
        function object keeps globals reachable for the thread's lifetime."""
        thunk_type = ir.FunctionType(VOID, [PTR], False)
        thunk = ir.Function(self.module, thunk_type, thunk_name)
        entry = thunk.append_basic_block("entry")
        old_func, old_builder = self.func, self.builder
        old_named = self.named_values.copy()
        old_params = self._frame_slots
        old_slots = self.slot_kinds
        old_break, old_exit = getattr(self, "_loop_break", None), getattr(self, "_loop_exit", None)
        old_continue = getattr(self, "_loop_continue", None)
        old_raw_ret = self._raw_ret
        old_try_ctx = self._try_ctx
        self.func = thunk
        self.builder = ir.IRBuilder(entry)
        self.named_values = {}
        self._frame_slots = []
        self.slot_kinds = {}
        self._raw_ret = None   # the thunk itself is boxed-ABI, void result
        self._try_ctx = []     # a function body does not inherit the try around it
        result = self.builder.call(llfn, [])
        self._release(result)
        self.builder.ret_void()
        self.func, self.builder = old_func, old_builder
        self.named_values = old_named
        self._frame_slots = old_params
        self.slot_kinds = old_slots
        self._loop_break, self._loop_exit = old_break, old_exit
        self._loop_continue = old_continue
        self._raw_ret = old_raw_ret
        self._try_ctx = old_try_ctx

    def _gen_value_wrapper(self, wrapper_name: str, llfn: ir.Function):
        """Emit `void fray_val.<name>(FrayValue self_fn)` — the body a
        first-class function object runs when fray_call invokes it.

        The shape is the one every entry point uses (`void (FrayValue)`), so
        the runtime invokes a thread thunk, a coroutine trampoline and this
        identically. What is specific to a first-class call is that the
        arguments arrive as a list through fray_call_args and the result
        leaves through fray_call_result_store; unpacking them into the declared
        parameters happens here, where their types are still known.
        """
        wtype = ir.FunctionType(VOID, [PTR], False)
        wrapper = ir.Function(self.module, wtype, wrapper_name)
        entry = wrapper.append_basic_block("entry")
        old_func, old_builder = self.func, self.builder
        old_named = self.named_values.copy()
        old_params = self._frame_slots
        old_slots = self.slot_kinds
        old_break, old_exit = getattr(self, "_loop_break", None), getattr(self, "_loop_exit", None)
        old_continue = getattr(self, "_loop_continue", None)
        old_raw_ret = self._raw_ret
        old_try_ctx = self._try_ctx
        self.func = wrapper
        self.builder = ir.IRBuilder(entry)
        self.named_values = {}
        self._frame_slots = []
        self.slot_kinds = {}
        self._raw_ret = None   # the wrapper is boxed-ABI with a void result
        self._try_ctx = []     # generated body, not the try around the definition

        argv = self._call("fray_call_args", [])
        args, extracted = [], []
        for i, ptype in enumerate(llfn.function_type.args):
            elem = self._call("fray_list_index", [argv, ir.Constant(I64, i)])
            # fray_list_index hands back an owned reference: a boxed parameter
            # takes it directly (the callee releases its parameters on the way
            # out), while a raw one is read here and released after the call.
            if ptype == PTR:
                args.append(elem)
            elif ptype in (I64, DOUBLE, I1):
                extracted.append(elem)
                if ptype == I64:
                    args.append(self._extract_int(elem))
                elif ptype == DOUBLE:
                    args.append(self._call("fray_as_float", [elem]))
                else:
                    args.append(self._call("fray_is_truthy", [elem]))
            else:
                raise CodegenError(
                    f"cannot call a function whose parameter {i} is {ptype}: "
                    "not a value representation")
        result = self.builder.call(llfn, args)
        for elem in extracted:
            self._release(elem)
        rtype = llfn.function_type.return_type
        if isinstance(rtype, ir.VoidType):
            boxed = self._box_none()
        elif rtype == PTR:
            boxed = result
        elif rtype == I64:
            boxed = self._box_int(result)
        elif rtype == DOUBLE:
            boxed = self._box_float(result)
        else:
            raise CodegenError(f"cannot call a function returning {rtype}: "
                               "not a value representation")
        self._call("fray_call_result_store", [boxed])
        self.builder.ret_void()

        self.func, self.builder = old_func, old_builder
        self.named_values = old_named
        self._frame_slots = old_params
        self.slot_kinds = old_slots
        self._loop_break, self._loop_exit = old_break, old_exit
        self._loop_continue = old_continue
        self._raw_ret = old_raw_ret
        self._try_ctx = old_try_ctx

    def _gen_function_value(self, name: str) -> ir.Value:
        """A bare function name in value position is a function object.

        Without this the name loaded the (null) module slot of the same name
        and silently became `None` — `Holder(h)` stored nothing callable. The
        object wraps `void fray_val.<name>(FrayValue self_fn)`, so a call site
        that cannot resolve its callee statically reaches it through
        fray_call.
        """
        llfn = self.module.globals[name]
        wrapper_name = f"fray_val.{name}"
        if not isinstance(self.module.globals.get(wrapper_name), ir.Function):
            self._gen_value_wrapper(wrapper_name, llfn)
        wrapper = self.module.globals[wrapper_name]
        return self._call("fray_function", [
            self._string_const(name),
            self.builder.bitcast(wrapper, PTR),
            ir.Constant(I32, len(llfn.function_type.args)),
        ])

    # ── Unboxed (raw) ABI ──
    #
    # A function whose every call site passes the same numeric type, and
    # whose parameters are therefore already raw i64/f64 slots, also gets
    # a second body `@<name>.raw` with unboxed parameters and result.
    # Raw call sites reach it directly: no boxing of the arguments, no
    # unboxing at entry, and no allocation at all for the recursive call.
    # The boxed body is still emitted and still used by every dynamic
    # site (spawn, imports, mismatched argument types), so behaviour is
    # unchanged wherever the raw ABI does not apply.
    #
    # The two bodies compute the same thing: inference only exposes the
    # raw form for a site whose argument types are exactly the parameter
    # kinds and whose own inferred result matches the body's raw result
    # kind, and only for bodies that return a value on every path (there
    # is no unboxed representation of `none`).

    @staticmethod
    def _kind_llvm_type(kind: str):
        return I64 if kind == "int" else DOUBLE

    def _raw_call(self, node: Call, want: str):
        """Emit a call to the unboxed body of a user function, or return
        None *without emitting anything* when the call cannot be unboxed.
        `want` is the kind of value the caller needs."""
        if self.infer is None or not isinstance(node.func, Identifier):
            return None
        fname = node.func.name
        if fname in self.async_fns or fname in self.extern_decls \
                or fname in self.struct_defs:
            return None
        # Only functions defined in this module: imported ones are
        # inlined under an alias and keep the boxed ABI.
        if not isinstance(self.module.globals.get(fname), ir.Function):
            return None
        callee = self.infer._funcs.get(fname)
        if callee is None:
            return None
        arg_tys = [self._ty(a) for a in node.args]
        spec = self.infer.raw_specialized(fname, arg_tys)
        if spec is None:
            return None
        p_kinds, ret_kind = spec
        if ret_kind != want or len(p_kinds) != len(node.args):
            return None
        # The unboxed body stores its arguments straight into raw slots,
        # so it may only be generated when the normal entry decision for
        # every parameter agrees with the kinds the spec promises.
        if any(self._param_kind(fname, p) != k
               for p, k in zip(callee.params, p_kinds)):
            return None
        fn = self._ensure_raw_function(fname, spec)
        if fn is None:
            return None
        expected = [self._kind_llvm_type(k) for k in p_kinds]
        if (list(fn.function_type.args) != expected
                or fn.function_type.return_type != self._kind_llvm_type(ret_kind)):
            # A body generated earlier for different kinds: leave this
            # site on the boxed path.
            return None
        args = [self._unbox_int(a) if k == "int" else self._unbox_float(a)
                for a, k in zip(node.args, p_kinds)]
        return self.builder.call(fn, args)

    def _ensure_raw_function(self, fname: str, spec):
        """Return the `@<name>.raw` body, generating it on first use."""
        if fname in self._raw_generated:
            got = self.module.globals.get(fname + ".raw")
            return got if isinstance(got, ir.Function) else None
        node = self.infer._funcs.get(fname)
        if node is None:
            return None
        # Mark first: the body's own recursive calls must find the
        # declaration that is currently being emitted.
        self._raw_generated.add(fname)
        return self._gen_raw_function_def(node, spec)

    def _gen_raw_function_def(self, node: FunctionDef, spec) -> ir.Function:
        """Generate the unboxed body `@<name>.raw` for a function whose
        parameters and result are all raw values."""
        p_kinds, ret_kind = spec
        ret_type = self._kind_llvm_type(ret_kind)
        param_types = [self._kind_llvm_type(k) for k in p_kinds]
        fname = node.name + ".raw"
        existing = self.module.globals.get(fname)
        if isinstance(existing, ir.Function):
            func = existing
        else:
            func = ir.Function(
                self.module, ir.FunctionType(ret_type, param_types, False), fname)
        old_func, old_builder = self.func, self.builder
        old_named = self.named_values.copy()
        old_ns = getattr(self, "_current_ns", "")
        old_params = self._frame_slots
        old_slots = self.slot_kinds
        old_break = getattr(self, "_loop_break", None)
        old_exit = getattr(self, "_loop_exit", None)
        old_continue = getattr(self, "_loop_continue", None)
        old_raw_ret = self._raw_ret
        old_try_ctx = self._try_ctx
        self.func = func
        self.builder = ir.IRBuilder(func.append_basic_block("entry"))
        self.named_values = {}
        self._current_ns = node.name
        self._frame_slots = []
        self.slot_kinds = {}
        self._raw_ret = ret_kind
        self._try_ctx = []     # a raw body parks nothing: see _gen_try
        self._predeclare_raw_slots(node)
        # Parameters arrive already unboxed, so they go straight into
        # their raw slots — no conversion, no ownership to release.
        for i, pname in enumerate(node.params):
            self.builder.store(func.args[i], self._raw_slot(pname, p_kinds[i]))
        self._predeclare_boxed_vars(node.body)
        for stmt in node.body:
            self._gen_stmt(stmt)
        if not self.builder.block.is_terminated:
            # Unreachable: inference only exposes the raw ABI for bodies
            # that return on every path. Emitted so the IR stays valid.
            fallback = (ir.Constant(I64, 0) if ret_kind == "int"
                        else ir.Constant(DOUBLE, 0.0))
            self.builder.ret(fallback)
        self.func, self.builder = old_func, old_builder
        self.named_values = old_named
        self._current_ns = old_ns
        self._frame_slots = old_params
        self.slot_kinds = old_slots
        self._loop_break = old_break
        self._loop_exit = old_exit
        self._loop_continue = old_continue
        self._raw_ret = old_raw_ret
        self._try_ctx = old_try_ctx
        return func

    def _emit_raw_return(self, node: ReturnStatement):
        """`return expr` inside an unboxed body: convert the expression to
        the body's raw result kind, the same conversion the boxed caller
        would have applied to the boxed result."""
        if node.value is None:
            fallback = (ir.Constant(I64, 0) if self._raw_ret == "int"
                        else ir.Constant(DOUBLE, 0.0))
            self.builder.ret(fallback)
            return
        if self._raw_ret == "int":
            self.builder.ret(self._unbox_int(node.value))
        else:
            self.builder.ret(self._unbox_float(node.value))

    def _gen_function_def(self, node: FunctionDef):
        """Generate LLVM IR for a user-defined function."""
        if node.is_async:
            self._gen_async_function_def(node)
            return
        n_params = len(node.params)
        func_type = ir.FunctionType(PTR, [PTR] * n_params, False)
        # Reuse existing declaration if the function was pre-declared
        # (e.g. during a two-pass module import).
        existing = self.module.globals.get(node.name)
        if isinstance(existing, ir.Function):
            func = existing
        else:
            func = ir.Function(self.module, func_type, node.name)
        # Save current state
        old_func = self.func
        old_builder = self.builder
        old_named = self.named_values.copy()
        old_ns = getattr(self, "_current_ns", "")
        old_params = self._frame_slots
        old_slots = self.slot_kinds
        old_break = getattr(self, "_loop_break", None)
        old_exit = getattr(self, "_loop_exit", None)
        old_continue = getattr(self, "_loop_continue", None)
        old_raw_ret = self._raw_ret
        old_try_ctx = self._try_ctx
        # Create entry block
        entry = func.append_basic_block("entry")
        builder = ir.IRBuilder(entry)
        self.func = func
        self.builder = builder
        self.named_values = {}
        self._current_ns = node.name
        self._frame_slots = []
        self.slot_kinds = {}
        self._raw_ret = None   # boxed body
        self._try_ctx = []     # a function body does not inherit the try around it
        self._predeclare_raw_slots(node)
        # Store params in allocas (the function owns its arguments)
        for i, param_name in enumerate(node.params):
            kind = self._param_kind(node.name, param_name)
            if kind is not None:
                # Unbox at entry: argument arrives boxed; store the raw
                # payload. The boxed arg is owned — release after unbox.
                raw = self._raw_slot(param_name, kind)
                boxed = func.args[i]
                if kind == "int":
                    builder.store(self._extract_int(boxed), raw)
                else:
                    builder.store(self._call("fray_as_float", [boxed]), raw)
                self._release(boxed)
            else:
                alloca = builder.alloca(PTR, name=param_name)
                builder.store(func.args[i], alloca)
                self.named_values[param_name] = alloca
                self._frame_slots.append(alloca)
        # Pre-declare boxed local variables in entry block
        self._predeclare_boxed_vars(node.body)
        # Generate body
        for stmt in node.body:
            self._gen_stmt(stmt)
        # Implicit return None if no explicit return
        if not self.builder.block.is_terminated:
            self._emit_func_return(self._box_none())
        # Restore state
        self.func = old_func
        self.builder = old_builder
        self.named_values = old_named
        self._current_ns = old_ns
        self._frame_slots = old_params
        self.slot_kinds = old_slots
        self._loop_break = old_break
        self._loop_exit = old_exit
        self._loop_continue = old_continue
        self._raw_ret = old_raw_ret
        self._try_ctx = old_try_ctx

    def _gen_arg_list(self, nodes) -> ir.Value:
        """Build the runtime list the call ABIs carry arguments in.

        Each argument is appended (which retains it) and the emitter's own
        reference released, so the list holds the only one and the callee's
        unpacking retains what it takes. Async calls use this because their
        arguments travel with the coroutine; a first-class call hands the list
        to fray_call the same way.
        """
        argv = self._call("fray_list", [])
        for node in nodes:
            arg = self._gen_expr(node)
            self._call("fray_list_append", [argv, arg])
            self._release(arg)
        return argv

    def _gen_value_call(self, callee_node, arg_nodes) -> ir.Value:
        """Call a value that is not a statically known function.

        The callee is evaluated — a parameter, a variable, a struct field — and
        the arguments are carried in a list; the runtime then decides whether
        the value is callable and whether the count matches the declaration.
        Its TypeErrors are the oracle's (`'5' is not callable`, `'h' takes 1
        argument(s) but 2 given`), which is the only place they can be raised:
        the callee is not known until the program runs.
        """
        fn_val = self._gen_expr(callee_node)
        args = self._gen_arg_list(arg_nodes)
        result = self._call("fray_call", [fn_val, args])
        self._release(fn_val)
        self._release(args)
        return result

    def _gen_async_function_def(self, node: FunctionDef):
        """Async functions (Phase 7).

        The user-visible call name resolves to the coroutine trampoline
        `void fray_async.<name>(FrayValue self_fn)` — the exact shape the
        runtime's coro_trampoline invokes through the function object's
        code pointer. The trampoline reads the coroutine's argument list
        with fray_coro_argv and calls
        `fray_async_body.<name>(FrayValue argv)` (compiled with awaits
        allowed — each await suspends the fiber), handing the result to
        fray_coro_result_store. The body unpacks its parameters from that
        list, which is how an async call passes arguments through the
        coroutine ABI. A call site builds a function object around the
        trampoline plus the argument list, and fray_coro_start_argv
        returns a TAG_COROUTINE handle immediately. (The trampoline can't
        take the user's bare name: it would collide with `main`.)"""
        tramp_type = ir.FunctionType(VOID, [PTR], False)
        tramp = ir.Function(self.module, tramp_type, f"fray_async.{node.name}")
        body_type = ir.FunctionType(PTR, [PTR], False)
        body = ir.Function(self.module, body_type, f"fray_async_body.{node.name}")

        old_func, old_builder = self.func, self.builder
        old_named = self.named_values.copy()
        old_ns = getattr(self, "_current_ns", "")
        old_params = self._frame_slots
        old_slots = self.slot_kinds
        old_break = getattr(self, "_loop_break", None)
        old_exit = getattr(self, "_loop_exit", None)
        old_continue = getattr(self, "_loop_continue", None)
        old_in_async = self._in_async
        old_raw_ret = self._raw_ret
        old_try_ctx = self._try_ctx

        # Body helper: awaits here lower to fray_coro_await (fiber suspend).
        self.func = body
        self.builder = ir.IRBuilder(body.append_basic_block("entry"))
        self.named_values = {}
        self._current_ns = node.name
        self._frame_slots = []
        self.slot_kinds = {}
        self._raw_ret = None   # async results always travel boxed
        self._try_ctx = []     # a function body does not inherit the try around it
        self._in_async += 1
        # Bind parameters from the coroutine's argument list. Each element
        # arrives as an owned reference (fray_list_index retains) and the
        # function owns its parameters, so they land in _frame_slots and are
        # released on the return path — exactly the boxed entry a plain
        # function uses. The list itself is borrowed (the coroutine owns it).
        for i, param_name in enumerate(node.params):
            alloca = self.builder.alloca(PTR, name=param_name)
            elem = self._call("fray_list_index",
                              [body.args[0], ir.Constant(I64, i)])
            self.builder.store(elem, alloca)
            self.named_values[param_name] = alloca
            self._frame_slots.append(alloca)
        for stmt in node.body:
            self._gen_stmt(stmt)
        if not self.builder.block.is_terminated:
            self._emit_func_return(self._box_none())

        # Trampoline: run the body, deliver the result, return void.
        self._in_async = old_in_async
        self.func = tramp
        self.builder = ir.IRBuilder(tramp.append_basic_block("entry"))
        self.named_values = {}
        self._frame_slots = []
        self.slot_kinds = {}
        argv = self._call("fray_coro_argv", [])
        result = self.builder.call(body, [argv])
        self._call("fray_coro_result_store", [result])
        self.builder.ret_void()

        self.func, self.builder = old_func, old_builder
        self.named_values = old_named
        self._current_ns = old_ns
        self._frame_slots = old_params
        self.slot_kinds = old_slots
        self._loop_break = old_break
        self._loop_exit = old_exit
        self._loop_continue = old_continue
        self._raw_ret = old_raw_ret
        self._try_ctx = old_try_ctx

    def _predeclare_raw_slots(self, node: FunctionDef):
        """Decide raw vs boxed representation for every name in the
        function up front and allocate the raw allocas at entry. Reads
        the inference join for locals and the widened signatures for
        params (INT only when every call site passes INT and the body
        never rebinds it)."""
        if self.infer is None or self._is_module_scope():
            return
        for (scope, vname), ty in self.infer.var_types.items():
            if scope != node.name or vname in self.slot_kinds:
                continue
            if vname in self._module_globals:
                continue  # resolves to the module global, not a local slot
            if vname in node.params:
                continue  # parameters are decided from their call sites below
            kind = None
            if ty is INT:
                kind = "int"
            elif ty is FLOAT:
                kind = "float"
            if kind is not None:
                self.slot_kinds[vname] = kind
                self._raw_slot(vname, kind)
        for pname in node.params:
            if pname in self.slot_kinds:
                continue
            kind = self._param_kind(node.name, pname)
            if kind is not None:
                self.slot_kinds[pname] = kind
                self._raw_slot(pname, kind)

    def _gen_while(self, node: WhileLoop):
        preheader = self._new_block("while_preheader")
        loop_body = self._new_block("while_body")
        loop_exit = self._new_block("while_exit")

        self._loop_exit = loop_exit
        self._loop_break = loop_exit
        # `continue` goes back to the condition, which is the whole point:
        # it re-tests without running the rest of the body.
        old_continue = getattr(self, "_loop_continue", None)
        self._loop_continue = preheader

        self.builder.branch(preheader)
        self.builder.position_at_end(preheader)

        truthy = self._gen_cond(node.condition)
        self.builder.cbranch(truthy, loop_body, loop_exit)

        self.builder.position_at_end(loop_body)
        for stmt in node.body:
            self._gen_stmt(stmt)

        truthy = self._gen_cond(node.condition)
        self.builder.cbranch(truthy, loop_body, loop_exit)

        self.builder.position_at_end(loop_exit)
        self._loop_continue = old_continue

    def _gen_try(self, node: TryStatement):
        """try/except/finally.

        The runtime keeps one pending exception per thread, so this is a poll
        after each statement plus a dispatch chain: when the slot is set, the
        first clause that matches runs. A bare `except:`, `except Exception:`
        and `except RuntimeError:` match anything, as in the oracle. A clause
        that matches clears the slot (fray_exc_clear); one that does not leaves
        it pending, and the finally re-raises it — to the enclosing try, or out
        of the program when there is none, which is what the oracle does with
        an exception nothing handles.

        A `return` in the body polls the flag too before returning
        (_gen_deferred_return): the statement that built the return value may
        have left an exception behind, and the oracle lets the matching clause
        take that case rather than return. A return that survives the poll
        parks its value here and leaves through the finally, and the tail below
        is what returns it.
        """
        dispatch = self._new_block("try_dispatch")
        finally_block = self._new_block("try_finally")
        end_block = self._new_block("try_end")

        # Enter try context
        self._call("fray_try_begin", [])

        # Where a `return` in the body parks its value and whether one did —
        # cleared on entry, because a try inside a loop is entered more than
        # once. The slot's type follows the body's ABI: a boxed reference, or
        # the raw result itself in an unboxed body.
        ctx = None
        raw_kind = self._raw_ret
        if raw_kind == "int":
            park_type = I64
        elif raw_kind == "float":
            park_type = DOUBLE
        elif raw_kind is None and self.func.return_value.type == PTR:
            park_type = PTR
        else:
            # The module-level body (main returns i32) parks nothing; its
            # returns are not the ones this exists for.
            park_type = None
        if park_type is not None:
            park = self.builder.alloca(park_type, name="try_park")
            flag = self.builder.alloca(I1, name="try_ret_flag")
            self.builder.store(ir.Constant(I1, 0), flag)
            ctx = (dispatch, finally_block, park, flag, raw_kind)
            self._try_ctx.append(ctx)

        for stmt in node.body:
            self._gen_stmt(stmt)
            if self.builder.block.is_terminated:
                break
            # Did that statement leave an exception behind?
            has_exc = self.builder.icmp_signed(
                "!=", self._load_exc_type(), ir.Constant(I32, 0))
            still_body = self._new_block("try_body_next")
            self.builder.cbranch(has_exc, dispatch, still_body)
            self.builder.position_at_end(still_body)

        # The body is done. A `return` in an except clause does not end the try
        # either: this try's finally still has to run, and the caller still has
        # to see the value the clause produced, so the clause bodies keep this
        # try's park/flag/finally — with the finally as their dispatch, because
        # an exception raised inside a handler belongs to the *enclosing* try,
        # never to the clauses of the try that is already handling it. The
        # finally body below runs outside the context: a `return` there would
        # otherwise park its value and branch back into its own finally.
        if ctx is not None:
            self._try_ctx.pop()
            self._try_ctx.append((finally_block, finally_block, ctx[2], ctx[3],
                                  ctx[4]))

        if not self.builder.block.is_terminated:
            self.builder.branch(finally_block)

        # Dispatch: the first clause that matches wins. Every test gets a block
        # of its own so a failed test falls to the *next test*, never into the
        # body of the clause checked before it.
        chain = dispatch
        for exc_name, exc_body in node.except_clauses:
            self.builder.position_at_end(chain)
            match = self._exc_match_test(exc_name)
            if match is None:
                # A clause that cannot match in the oracle either: its body is
                # unreachable, so there is nothing to branch to.
                continue
            handler_block = self._new_block("exc_handler")
            next_test = self._new_block("exc_next")
            self.builder.cbranch(match, handler_block, next_test)

            self.builder.position_at_end(handler_block)
            self._call("fray_exc_clear", [])
            for h_stmt in exc_body:
                self._gen_stmt(h_stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(finally_block)

            chain = next_test

        if ctx is not None:
            self._try_ctx.pop()

        # Nothing matched — or there was no clause: the exception is still
        # pending, and the finally hands it on instead of dropping it.
        self.builder.position_at_end(chain)
        self.builder.branch(finally_block)

        # Finally block
        self.builder.position_at_end(finally_block)
        self._call("fray_try_end", [])
        if node.finally_body:
            for stmt in node.finally_body:
                self._gen_stmt(stmt)
        if not self.builder.block.is_terminated:
            # No-op when a clause handled the exception — and a no-op again for
            # a return that is on its way out of this try.
            self._call("fray_exc_rethrow", [])
            if ctx is None:
                self.builder.branch(end_block)
            else:
                return_block = self._new_block("try_return")
                self.builder.cbranch(self.builder.load(ctx[3]),
                                     return_block, end_block)
                self.builder.position_at_end(return_block)
                value = self.builder.load(ctx[2])
                outer = self._try_ctx[-1] if self._try_ctx else None
                if outer is not None:
                    # Enclosing finally bodies still have to run before the
                    # return takes effect: hand the parked value to the try
                    # around this one and let its tail decide when the function
                    # really returns. The frame's slots stay live until then,
                    # and every try on this stack shares this body's ABI, so the
                    # hand-off needs no conversion.
                    self.builder.store(value, outer[2])
                    self.builder.store(ir.Constant(I1, 1), outer[3])
                    self.builder.branch(outer[1])
                elif ctx[4] is not None:
                    # Unboxed body: the parked value *is* the raw result.
                    self.builder.ret(value)
                else:
                    self._emit_func_return(value)

        self.builder.position_at_end(end_block)

    # ── Expression generation ──

    def _gen_expr(self, node: Optional[Node]) -> ir.Value:
        """Generate an expression; the result is an OWNED reference the
        caller must release (or store)."""
        if node is None:
            return self._box_none()

        if isinstance(node, IntLiteral):
            return self._box_int(ir.Constant(I64, node.value))

        if isinstance(node, FloatLiteral):
            return self._box_float(ir.Constant(DOUBLE, node.value))

        if isinstance(node, StringLiteral):
            return self._box_string(self._string_const(node.value))

        if isinstance(node, BoolLiteral):
            return self._box_bool(ir.Constant(I1, 1 if node.value else 0))

        if isinstance(node, ConstantLiteral):
            if node.name == "null":
                return self._box_none()
            if node.name == "pi":
                ptr = self.builder.bitcast(self.module.get_global("fray_pi"), ir.PointerType(DOUBLE, 0))
                return self._box_float(self.builder.load(ptr))
            elif node.name == "e":
                ptr = self.builder.bitcast(self.module.get_global("fray_e"), ir.PointerType(DOUBLE, 0))
                return self._box_float(self.builder.load(ptr))
            return self._box_none()

        if isinstance(node, Identifier):
            kind = self._slot_kind(node.name)
            if kind is not None:
                raw = self._load_raw(node.name, kind)
                return self._box_int(raw) if kind == "int" else self._box_float(raw)
            # A name that is not a variable but does name a function is a
            # first-class function object, not the null slot _load would
            # hand back: `Holder(h)` has to store something callable.
            if (node.name not in self.named_values
                    and node.name not in self._module_globals
                    and isinstance(self.module.globals.get(node.name),
                                   ir.Function)):
                return self._gen_function_value(node.name)
            # Loading a variable yields an owned reference (retain).
            v = self._load(node.name)
            self._call("fray_retain", [v])
            return v

        if isinstance(node, AwaitExpr):
            # await coro — suspends the calling fiber until the target
            # coroutine finishes and yields its result. Must appear
            # inside an async function. Result type is UNKNOWN (whatever
            # the awaited body returned) so it never hits a raw slot.
            if self._in_async == 0:
                raise CodegenError("await outside an async function")
            if node.value is None:
                return self._box_none()
            target = self._gen_expr(node.value)
            result = self._call("fray_coro_await", [target])
            self._release(target)
            return result

        if isinstance(node, QuestionMark):
            # expr? — unwrap Option/Result, propagate None/Err upward
            # Strategy: try unwrap, catch the throw, and return early.
            val = self._gen_expr(node.value)
            result = self._call("fray_unwrap", [val])
            self._release(val)
            return result

        if isinstance(node, BinaryOp):
            return self._gen_binop(node)

        if isinstance(node, UnaryOp):
            operand = self._gen_expr(node.operand)
            if node.op == "not":
                result = self._call("fray_not", [operand])
                self._release(operand)
                return result
            if node.op == "-":
                result = self._call("fray_neg", [operand])
                self._release(operand)
                return result
            self._release(operand)
            return operand

        if isinstance(node, Call):
            return self._gen_call(node)

        if isinstance(node, MemberAccess):
            # Enum variant access: Color.Red → return a string-tag struct
            if (isinstance(node.obj, Identifier) and
                    node.obj.name in self.enum_defs):
                enum_info = self.enum_defs[node.obj.name]
                for vname, vparams in enum_info["cases"]:
                    if vname == node.attr:
                        if not vparams:
                            # Simple variant: create struct with _variant tag
                            sv = self._gen_enum_new(node.obj.name, enum_info, vname)
                            return sv
                        # A bare parameterized variant is not a value: the
                        # oracle turns it into an unusable placeholder, and
                        # this engine used to do the same — the None box
                        # passed for the variant until the first field read
                        # died at run time with "TypeError: not a struct".
                        # Refuse it at compile time instead, like the
                        # self-hosted codegen does for the same shape.
                        raise CodegenError(
                            f"bare enum-variant references of a parameterized "
                            f"variant like '{node.obj.name}.{node.attr}' are "
                            f"not supported — call it as "
                            f"'{node.obj.name}.{node.attr}(args)'")
                raise CodegenError(f"'{node.obj.name}' has no variant '{node.attr}'")
            obj = self._gen_expr(node.obj)
            if node.attr == "append" and isinstance(node.obj, Identifier):
                # Bare x.append is a method reference in the oracle; the
                # expression value is discarded by every statement that
                # can legally appear here, so pass the container through.
                return obj
            if node.attr == "depend":
                # Bare x.depend is a method reference too — it must NOT
                # pop. Hand back an owned placeholder.
                self._release(obj)
                return self._box_none()
            # Struct field access: t.field
            # field_get returns a retained value; string constant is a raw
            # byte array (not a FrayValue) so must not be released.
            field_str = self._string_const(node.attr)
            result = self._call("fray_struct_field_get", [obj, field_str])
            self._release(obj)
            return result

        if isinstance(node, Index):
            obj = self._gen_expr(node.obj)
            # Slice syntax: obj[start:stop:step]
            if isinstance(node.index_expr, Slice):
                sl = node.index_expr
                start_val = self._gen_expr(sl.start) if sl.start else self._box_none()
                stop_val = self._gen_expr(sl.stop) if sl.stop else self._box_none()
                step_val = self._gen_expr(sl.step) if sl.step else self._box_none()
                result = self._call("fray_slice", [obj, start_val, stop_val, step_val])
                self._release(step_val)
                self._release(stop_val)
                self._release(start_val)
                self._release(obj)
                return result
            # Regular index: obj[idx]
            idx = self._gen_expr(node.index_expr)
            # Tag dispatch: map path vs container paths
            tag_ptr = self.builder.bitcast(obj, ir.PointerType(I8, 0))
            tag = self.builder.load(tag_ptr)

            do_map = self._new_block("idx_map")
            do_container = self._new_block("idx_container")
            done = self._new_block("idx_done")

            is_map = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_MAP))
            self.builder.cbranch(is_map, do_map, do_container)

            # Map path: fray_map_get(obj, key) — idx stays as-is
            self.builder.position_at_end(do_map)
            r_map = self._call("fray_map_get", [obj, idx])
            self.builder.branch(done)

            # Container path: inline dispatch (tuple → string → list)
            self.builder.position_at_end(do_container)
            idx_int = self._extract_int(idx)
            is_tuple = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_TUPLE))
            do_tuple = self._new_block("idx_tuple")
            check_string = self._new_block("idx_chk_str")
            do_string = self._new_block("idx_string")
            do_list = self._new_block("idx_list")
            self.builder.cbranch(is_tuple, do_tuple, check_string)

            self.builder.position_at_end(do_tuple)
            r_tuple = self._call("fray_tuple_index", [obj, idx_int])
            self.builder.branch(done)

            self.builder.position_at_end(check_string)
            is_string = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_STRING))
            self.builder.cbranch(is_string, do_string, do_list)

            self.builder.position_at_end(do_string)
            r_string = self._call("fray_string_index", [obj, idx_int])
            self.builder.branch(done)

            self.builder.position_at_end(do_list)
            r_list = self._call("fray_list_index", [obj, idx_int])
            self.builder.branch(done)

            # Merge: map + tuple + string + list (all 4 leaf blocks)
            self.builder.position_at_end(done)
            phi = self.builder.phi(PTR)
            phi.add_incoming(r_map, do_map)
            phi.add_incoming(r_tuple, do_tuple)
            phi.add_incoming(r_string, do_string)
            phi.add_incoming(r_list, do_list)
            self._release(idx)
            self._release(obj)
            return phi

        if isinstance(node, ListLiteral):
            list_val = self._call("fray_list", [])
            for elem in node.elements:
                elem_val = self._gen_expr(elem)
                self._call("fray_list_append", [list_val, elem_val])
                self._release(elem_val)
            return list_val

        if isinstance(node, SetLiteral):
            set_val = self._call("fray_set", [])
            for elem in node.elements:
                elem_val = self._gen_expr(elem)
                self._call("fray_set_append", [set_val, elem_val])
                self._release(elem_val)
            return set_val

        if isinstance(node, TupleLiteral):
            n = len(node.elements)
            tuple_val = self._call("fray_tuple", [ir.Constant(I64, n)])
            for i, elem in enumerate(node.elements):
                elem_val = self._gen_expr(elem)
                self._call("fray_tuple_setindex", [tuple_val, ir.Constant(I64, i), elem_val])
                self._release(elem_val)
            return tuple_val

        if isinstance(node, MapLiteral):
            map_val = self._call("fray_map_new", [])
            for k_node, v_node in zip(node.keys, node.values):
                k = self._gen_expr(k_node)
                v = self._gen_expr(v_node)
                self._call("fray_map_set", [map_val, k, v])
                # map_set takes ownership of k and v
            return map_val

        if isinstance(node, MatchExpression):
            return self._gen_match(node)

        raise CodegenError(f"unknown node type {type(node).__name__}")

    def _gen_container_index(self, obj: ir.Value, idx_int: ir.Value) -> ir.Value:
        """Index a list/tuple/string by runtime tag dispatch."""
        tag_ptr = self.builder.bitcast(obj, ir.PointerType(I8, 0))
        tag = self.builder.load(tag_ptr)
        is_tuple = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_TUPLE))
        do_tuple = self._new_block("idx_tuple")
        check_string = self._new_block("idx_string")
        do_string = self._new_block("idx_str")
        do_list = self._new_block("idx_list")
        done = self._new_block("idx_done")
        self.builder.cbranch(is_tuple, do_tuple, check_string)

        self.builder.position_at_end(do_tuple)
        r_tuple = self._call("fray_tuple_index", [obj, idx_int])
        self.builder.branch(done)

        self.builder.position_at_end(check_string)
        is_string = self.builder.icmp_unsigned("==", tag, ir.Constant(I8, TAG_STRING))
        self.builder.cbranch(is_string, do_string, do_list)

        self.builder.position_at_end(do_string)
        r_string = self._call("fray_string_index", [obj, idx_int])
        self.builder.branch(done)

        self.builder.position_at_end(do_list)
        r_list = self._call("fray_list_index", [obj, idx_int])
        self.builder.branch(done)

        self.builder.position_at_end(done)
        phi = self.builder.phi(PTR)
        phi.add_incoming(r_tuple, do_tuple)
        phi.add_incoming(r_string, do_string)
        phi.add_incoming(r_list, do_list)
        return phi

    def _int_arith(self, op: str, l: ir.Value, r: ir.Value) -> ir.Value:
        """Raw i64 arithmetic (l, r, result are unboxed i64s)."""
        if op == "+":
            return self.builder.add(l, r)
        if op == "-":
            return self.builder.sub(l, r)
        if op == "*":
            return self.builder.mul(l, r)
        if op == "^":
            return self._gen_int_pow(l, r)
        raise CodegenError(f"unsupported raw int op '{op}'")

    def _raw_binop_i64(self, node: BinaryOp) -> ir.Value:
        """Raw i64 result of a proven int+int arithmetic node (no boxing
        anywhere in the chain). Used by raw-slot augmented assignment."""
        op = node.op
        l = self._unbox_int(node.left)
        r = self._unbox_int(node.right)
        if op in ("+", "-", "*", "^"):
            return self._int_arith(op, l, r)
        if op in ("//", "%"):
            # Zero check + floor semantics, same observable behavior as
            # the runtime's fray_floordiv / fray_mod.
            zero = self.builder.icmp_signed("==", r, ir.Constant(I64, 0))
            ok = self._new_block("rawdiv_ok")
            slow = self._new_block("rawdiv_slow")
            merge = self._new_block("rawdiv_merge")
            self.builder.cbranch(zero, slow, ok)
            self.builder.position_at_end(slow)
            self._throw_const(5, "ZeroDivisionError: division by zero")
            slow_v = ir.Constant(I64, 0)  # C fallback value
            self.builder.branch(merge)
            self.builder.position_at_end(ok)
            if op == "//":
                q = self.builder.sdiv(l, r)
                rem = self.builder.srem(l, r)
                rem_nz = self.builder.icmp_signed("!=", rem, ir.Constant(I64, 0))
                signs = self.builder.icmp_signed("<", self.builder.xor(l, r), ir.Constant(I64, 0))
                needs_fix = self.builder.and_(rem_nz, signs)
                fix_block = self._new_block("rawdiv_fix")
                self.builder.cbranch(needs_fix, fix_block, merge)
                self.builder.position_at_end(fix_block)
                q2 = self.builder.sub(q, ir.Constant(I64, 1))
                self.builder.branch(merge)
                self.builder.position_at_end(merge)
                phi = self.builder.phi(I64)
                phi.add_incoming(slow_v, slow)
                phi.add_incoming(q, ok)
                phi.add_incoming(q2, fix_block)
                return phi
            rm = self.builder.srem(l, r)
            nz = self.builder.icmp_signed("!=", rm, ir.Constant(I64, 0))
            sign_mismatch = self.builder.icmp_signed(
                "<", self.builder.xor(rm, r), ir.Constant(I64, 0))
            needs_fix = self.builder.and_(nz, sign_mismatch)
            fix_block = self._new_block("rawdiv_fix")
            self.builder.cbranch(needs_fix, fix_block, merge)
            self.builder.position_at_end(fix_block)
            rm2 = self.builder.add(rm, r)
            self.builder.branch(merge)
            self.builder.position_at_end(merge)
            phi = self.builder.phi(I64)
            phi.add_incoming(slow_v, slow)
            phi.add_incoming(rm, ok)
            phi.add_incoming(rm2, fix_block)
            return phi
        raise CodegenError(f"unsupported raw binop '{op}'")

    def _gen_binop(self, node: BinaryOp) -> ir.Value:
        """Binary operations with monomorphic fast paths."""
        op = node.op

        # Comparisons / boolean ops always produce bools; fast path when
        # both sides are proven numeric.
        if op in ("==", "!=", "<", ">", "<=", ">="):
            lt, rt = self._ty(node.left), self._ty(node.right)
            if lt in (INT, BOOL) and rt in (INT, BOOL):
                l = self._unbox_int(node.left)
                r = self._unbox_int(node.right)
                cond = self._icmp(op, l, r)
                return self._box_bool(cond)
            if lt is FLOAT and rt is FLOAT:
                l = self._unbox_float(node.left)
                r = self._unbox_float(node.right)
                cond = self._fcmp(op, l, r)
                return self._box_bool(cond)
            # Generic
            left = self._gen_expr(node.left)
            right = self._gen_expr(node.right)
            rt_name = {"==": "fray_eq", "!=": "fray_neq",
                       "<": "fray_lt", ">": "fray_gt",
                       "<=": "fray_lte", ">=": "fray_gte"}[op]
            result = self._call(rt_name, [left, right])
            self._release(left)
            self._release(right)
            return result

        if op == "in":
            left = self._gen_expr(node.left)
            right = self._gen_expr(node.right)
            # fray_contains(haystack, needle) dispatches by tag at runtime
            result = self._call("fray_contains", [right, left])
            self._release(left)
            self._release(right)
            return result

        if op in ("and", "or", "xor", "xnor"):
            # Short-circuit for and/or: only evaluate right side if needed
            if op == "and":
                left = self._gen_expr(node.left)
                left_truthy = self._call("fray_is_truthy", [left])
                left_block = self.builder.block
                right_block = self._new_block("and_right")
                done_block = self._new_block("and_done")
                self.builder.cbranch(left_truthy, right_block, done_block)
                # Right side
                self.builder.position_at_end(right_block)
                right = self._gen_expr(node.right)
                right_block = self.builder.block
                self._release(left)
                self.builder.branch(done_block)
                # Merge
                self.builder.position_at_end(done_block)
                result = self.builder.phi(PTR)
                result.add_incoming(left, left_block)
                result.add_incoming(right, right_block)
                return result
            if op == "or":
                left = self._gen_expr(node.left)
                left_truthy = self._call("fray_is_truthy", [left])
                left_block = self.builder.block
                right_block = self._new_block("or_right")
                done_block = self._new_block("or_done")
                self.builder.cbranch(left_truthy, done_block, right_block)
                # Right side
                self.builder.position_at_end(right_block)
                right = self._gen_expr(node.right)
                right_block = self.builder.block
                self._release(left)
                self.builder.branch(done_block)
                # Merge
                self.builder.position_at_end(done_block)
                result = self.builder.phi(PTR)
                result.add_incoming(left, left_block)
                result.add_incoming(right, right_block)
                return result
            # xor/xnor: both sides always evaluated
            left = self._gen_expr(node.left)
            right = self._gen_expr(node.right)
            lt_name = {"xor": "fray_xor", "xnor": "fray_xnor"}[op]
            result = self._call(lt_name, [left, right])
            self._release(left)
            self._release(right)
            return result

        # Arithmetic
        lt, rt = self._ty(node.left), self._ty(node.right)

        # int op int (no '/' — that always produces float)
        if lt is INT and rt is INT and op in ("+", "-", "*", "//", "%", "^"):
            v = self._raw_binop_i64(node)
            return self._box_int(v)

        # float op float (or int/float mixed): float arithmetic
        if lt in (INT, FLOAT) and rt in (INT, FLOAT) and op in ("+", "-", "*", "/"):
            l = self._unbox_float(node.left)
            r = self._unbox_float(node.right)
            if op == "+":
                v = self.builder.fadd(l, r)
            elif op == "-":
                v = self.builder.fsub(l, r)
            elif op == "*":
                v = self.builder.fmul(l, r)
            elif op == "/":
                zero = self.builder.fcmp_ordered("==", r, ir.Constant(DOUBLE, 0.0))
                div_ok = self._new_block("fdivf_ok")
                div_slow = self._new_block("fdivf_slow")
                fdiv_merge = self._new_block("fdivf_merge")
                self.builder.cbranch(zero, div_slow, div_ok)
                self.builder.position_at_end(div_slow)
                self._throw_const(5, "ZeroDivisionError: division by zero")
                slow_v = ir.Constant(DOUBLE, 0.0)  # C fallback value
                self.builder.branch(fdiv_merge)
                self.builder.position_at_end(div_ok)
                fast_v = self.builder.fdiv(l, r)
                self.builder.branch(fdiv_merge)
                self.builder.position_at_end(fdiv_merge)
                phi = self.builder.phi(DOUBLE)
                phi.add_incoming(slow_v, div_slow)
                phi.add_incoming(fast_v, div_ok)
                v = phi
            return self._box_float(v)

        # String concatenation / repetition
        if op == "+" and lt is STRING and rt is STRING:
            l = self._gen_expr(node.left)
            r = self._gen_expr(node.right)
            result = self._call("fray_string_concat", [l, r])
            self._release(l)
            self._release(r)
            return result
        if op == "*" and lt is STRING and rt is INT:
            l = self._gen_expr(node.left)
            r = self._unbox_int(node.right)
            result = self._call("fray_string_repeat", [l, r])
            self._release(l)
            return result
        if op == "*" and lt is INT and rt is STRING:
            l = self._unbox_int(node.left)
            r = self._gen_expr(node.right)
            result = self._call("fray_string_repeat", [r, l])
            self._release(r)
            return result

        # List concatenation
        if op == "+" and lt is LIST and rt is LIST:
            left = self._gen_expr(node.left)
            right = self._gen_expr(node.right)
            result = self._call("fray_add", [left, right])
            self._release(left)
            self._release(right)
            return result

        # Generic boxed path
        left = self._gen_expr(node.left)
        right = self._gen_expr(node.right)
        op_map = {
            "+": "fray_add", "-": "fray_sub",
            "*": "fray_mul", "/": "fray_div",
            "//": "fray_floordiv", "%": "fray_mod",
            "^": "fray_pow",
        }
        if op in op_map:
            result = self._call(op_map[op], [left, right])
            self._release(left)
            self._release(right)
            return result
        raise CodegenError(f"unknown binary operator '{op}'")

    def _icmp(self, op: str, l: ir.Value, r: ir.Value) -> ir.Value:
        m = {"==": "==", "!=": "!=", "<": "<", ">": ">",
             "<=": "<=", ">=": ">="}
        return self.builder.icmp_signed(m[op], l, r)

    def _fcmp(self, op: str, l: ir.Value, r: ir.Value) -> ir.Value:
        m = {"==": "==", "!=": "!=", "<": "<", ">": ">",
             "<=": "<=", ">=": ">="}
        return self.builder.fcmp_ordered(m[op], l, r)

    def _unbox_int(self, node) -> ir.Value:
        """Generate an int64 from a proven-int expression (fast path)."""
        if isinstance(node, IntLiteral):
            return ir.Constant(I64, node.value)
        if isinstance(node, BoolLiteral):
            return ir.Constant(I64, 1 if node.value else 0)
        if isinstance(node, Identifier):
            # Raw slot: plain load, no tag check needed (slot type is
            # inference-proven and the value never left the machine int).
            kind = self._slot_kind(node.name)
            if kind == "int":
                return self._load_raw(node.name, "int")
            if kind == "float":
                # Raw float slot: box it and let fray_as_int do the
                # conversion, exactly as the boxed path would (an
                # out-of-range fptosi would be undefined behaviour).
                boxed = self._box_float(self._load_raw(node.name, "float"))
                iv = self._extract_int(boxed)
                self._release(boxed)
                return iv
            v = self._load(node.name)
            self._call("fray_retain", [v])  # load is borrowed; keep alive
            iv = self._extract_int(v)
            self._release(v)
            return iv
        if isinstance(node, BinaryOp) and node.op in (
                "+", "-", "*", "//", "%", "^"):
            lt, rt = self._ty(node.left), self._ty(node.right)
            if lt is INT and rt is INT:
                return self._raw_binop_i64(node)
        if isinstance(node, Call):
            # Unboxed call: the callee's raw body returns an i64 already,
            # so there is no box to allocate and tear down.
            raw = self._raw_call(node, "int")
            if raw is not None:
                return raw
        # Fall back to generic generation + unbox
        v = self._gen_expr(node)
        iv = self._extract_int(v)
        self._release(v)
        return iv

    def _unbox_float(self, node) -> ir.Value:
        """Generate a double from a proven-float expression."""
        if isinstance(node, FloatLiteral):
            return ir.Constant(DOUBLE, node.value)
        if isinstance(node, (IntLiteral, BoolLiteral)):
            base = self._unbox_int(node)
            return self.builder.sitofp(base, DOUBLE)
        if isinstance(node, Identifier):
            kind = self._slot_kind(node.name)
            if kind == "float":
                return self._load_raw(node.name, "float")
            if kind == "int":
                # Raw int slot: fray_as_float on a boxed int is C's
                # (double) cast, which sitofp reproduces exactly.
                return self.builder.sitofp(
                    self._load_raw(node.name, "int"), DOUBLE)
            v = self._load(node.name)
            self._call("fray_retain", [v])  # load is borrowed; keep alive
            fv = self._call("fray_as_float", [v])
            self._release(v)
            return fv
        if isinstance(node, Call):
            raw = self._raw_call(node, "float")
            if raw is not None:
                return raw
        v = self._gen_expr(node)
        fv = self._call("fray_as_float", [v])
        self._release(v)
        return fv

    def _throw_const(self, exc_id: int, msg: str):
        """Call fray_throw. Inside a try block it returns normally; the
        caller must then produce the runtime's fallback value, exactly as
        the C ops do (e.g. fray_div returns fray_float(0.0) after a
        zero-divisor throw)."""
        self._call("fray_throw", [ir.Constant(I32, exc_id),
                                  self._string_const(msg)])

    def _gen_int_pow(self, base: ir.Value, exp: ir.Value) -> ir.Value:
        """Exponentiation by squaring on unboxed i64s (exp >= 0)."""
        result_a = self.builder.alloca(I64, name="pow_result")
        base_a = self.builder.alloca(I64, name="pow_base")
        exp_a = self.builder.alloca(I64, name="pow_exp")
        self.builder.store(ir.Constant(I64, 1), result_a)
        self.builder.store(base, base_a)
        self.builder.store(exp, exp_a)
        loop = self._new_block("pow_loop")
        body = self._new_block("pow_body")
        done = self._new_block("pow_done")
        self.builder.branch(loop)
        self.builder.position_at_end(loop)
        e = self.builder.load(exp_a)
        cont = self.builder.icmp_signed(">", e, ir.Constant(I64, 0))
        self.builder.cbranch(cont, body, done)
        self.builder.position_at_end(body)
        e = self.builder.load(exp_a)
        b = self.builder.load(base_a)
        odd = self.builder.icmp_signed("!=", self.builder.and_(e, ir.Constant(I64, 1)), ir.Constant(I64, 0))
        mul_block = self._new_block("pow_mul")
        skip_block = self._new_block("pow_skip")
        self.builder.cbranch(odd, mul_block, skip_block)
        self.builder.position_at_end(mul_block)
        r = self.builder.load(result_a)
        self.builder.store(self.builder.mul(r, b), result_a)
        self.builder.branch(skip_block)
        self.builder.position_at_end(skip_block)
        e2 = self.builder.load(exp_a)
        self.builder.store(self.builder.lshr(e2, ir.Constant(I64, 1)), exp_a)
        b2 = self.builder.load(base_a)
        self.builder.store(self.builder.mul(b2, b2), base_a)
        self.builder.branch(loop)
        self.builder.position_at_end(done)
        return self.builder.load(result_a)

    def _gen_call(self, node: Call) -> ir.Value:
        func_name = None
        if isinstance(node.func, Identifier):
            func_name = node.func.name

        # Handle extern C function calls
        if func_name and func_name in self.extern_decls:
            llvm_func, extern_node = self.extern_decls[func_name]
            args = [self._gen_expr(a) for a in node.args]
            # Box/unbox: wrap raw args as fray values for C calling convention
            c_args = []
            for arg_val, ctype in zip(args, extern_node.param_types):
                if ctype in ("string",):
                    c_args.append(self._call("fray_as_string", [arg_val]))
                elif ctype in ("int", "i64", "i32", "i16", "i8", "u8", "u16", "u32", "u64"):
                    c_args.append(self._extract_int(arg_val))
                elif ctype in ("float", "f64", "f32"):
                    c_args.append(self._unbox_float(arg_val))
                elif ctype in ("bool",):
                    c_args.append(self._extract_int(arg_val))
                elif ctype in ("ptr",):
                    c_args.append(arg_val)  # pass as raw pointer
                else:
                    c_args.append(self._extract_int(arg_val))  # default: unbox int
            # Call the extern function
            result = self.builder.call(llvm_func, c_args)
            # Box the result back to a fray value
            ret = extern_node.return_type
            if ret == "void":
                self._release_all(args)
                return self._box_none()
            if ret == "string":
                # fray_string_copy takes a char*, returns owned FrayValue
                result_ptr = self.builder.inttoptr(result, PTR)
                boxed = self._call("fray_string_copy", [result_ptr])
                self._release_all(args)
                return boxed
            if ret in ("int", "i64", "i32", "i16", "i8", "u8", "u16", "u32", "u64"):
                # Truncate/sign-extend to i64, then box
                if result.type != I64:
                    result = self.builder.sext(result, I64) if result.type.width < 64 else self.builder.trunc(result, I64)
                boxed = self._call("fray_int", [result])
                self._release_all(args)
                return boxed
            if ret in ("float", "f64", "f32"):
                # The declaration for every float width is a double (TYPE_MAP),
                # so this is a no-op for f64/f32 and the identity for f32's
                # promoted declaration — comparing against the wrong type used
                # to be a NameError for any float-returning extern.
                if result.type != DOUBLE:
                    result = self.builder.fpext(result, DOUBLE)
                boxed = self._call("fray_float", [result])
                self._release_all(args)
                return boxed
            if ret == "bool":
                # The declared return type is i1 already; fray_bool takes it
                # as-is (a zext to the same width is not valid IR).
                boxed = self._box_bool(result)
                self._release_all(args)
                return boxed
            if ret == "ptr":
                boxed = self._call("fray_int", [self.builder.ptrtoint(result, I64)])
                self._release_all(args)
                return boxed
            self._release_all(args)
            return self._box_none()  # fallback

        # Handle variadic builtins
        if func_name in BUILTIN_MAP:
            rt_name, min_args, max_args = BUILTIN_MAP[func_name]
            args = [self._gen_expr(a) for a in node.args]
            if len(args) < min_args:
                raise CodegenError(f"{func_name}() requires at least {min_args} argument(s)")
            if func_name == "range":
                # range(stop) is the unary entry point; range(start, stop) and
                # range(start, stop, step) take fray_range3 (fray_range would
                # silently ignore the extra bounds).
                if len(args) > 3:
                    raise CodegenError(
                        f"range() takes at most 3 argument(s) ({len(args)} given)")
                if len(args) == 1:
                    result = self._call("fray_range", [args[0]])
                else:
                    step = (args[2] if len(args) == 3
                            else self._box_int(ir.Constant(I64, 1)))
                    result = self._call("fray_range3", [args[0], args[1], step])
                    self._release(args[1])
                    self._release(step)
                self._release(args[0])
                return result
            if func_name in ("min", "max"):
                # fray_min/fray_max return a retained alias of one operand
                # (owned result), so every input can be dropped.
                result = args[0]
                for a in args[1:]:
                    nxt = self._call(rt_name, [result, a])
                    self._release(result)
                    self._release(a)
                    result = nxt
                return result
            if func_name in INPUT_PROMPT_FNS:
                # inputInt("n: ") reads through the runtime's prompt variant:
                # the prompt is a C string there, so the box is unwrapped.
                if len(args) > 1:
                    raise CodegenError(
                        f"{func_name}() takes at most 1 argument(s) "
                        f"({len(args)} given)")
                if len(args) == 1:
                    prompt = self._call("fray_as_string", [args[0]])
                    result = self._call(INPUT_PROMPT_FNS[func_name], [prompt])
                    self._release_all(args)
                    return result
                return self._call(rt_name, [])
            if func_name == "print":
                # print() is variadic. Every argument but the last is written
                # with a trailing space and the last one closes the line, so
                # print(x) stays a single fray_print call. With no arguments
                # at all it is a bare newline — what the oracle does, and what
                # bootstrap/evaluator.py's _builtin_print already did.
                for a in args[:-1]:
                    self._call("fray_print_sep", [a])
                if args:
                    self._call(rt_name, args[-1:])
                else:
                    self._call("fray_print_end", [])
                self._release_all(args)
                return self._box_none()
            if func_name in VOID_BUILTINS:
                self._call(rt_name, args)
                self._release_all(args)
                return self._box_none()
            result = self._call(rt_name, args)
            self._release_all(args)
            return result

        # String methods: s.upper(), s.find(x), etc.
        if isinstance(node.func, MemberAccess):
            attr = node.func.attr
            STRING_METHODS_1 = {"upper", "lower", "trim"}
            STRING_METHODS_2 = {"find", "startswith", "endswith", "split"}
            STRING_METHODS_3 = {"replace"}
            if attr in STRING_METHODS_1:
                obj = self._gen_expr(node.func.obj)
                args = [self._gen_expr(a) for a in node.args]
                if len(args) != 0:
                    raise CodegenError(f"{attr}() takes no arguments")
                result = self._call(f"fray_string_{attr}", [obj])
                self._release_all(args)
                self._release(obj)
                return result
            if attr in STRING_METHODS_2:
                obj = self._gen_expr(node.func.obj)
                args = [self._gen_expr(a) for a in node.args]
                rt_name = f"fray_string_{attr}"
                result = self._call(rt_name, [obj] + args)
                self._release_all(args)
                self._release(obj)
                return result
            if attr in STRING_METHODS_3:
                obj = self._gen_expr(node.func.obj)
                args = [self._gen_expr(a) for a in node.args]
                rt_name = f"fray_string_{attr}"
                result = self._call(rt_name, [obj] + args)
                self._release_all(args)
                self._release(obj)
                return result

        # Namespaced function calls: foo.bar() where foo.bar is a compiled global
        if (isinstance(node.func, MemberAccess) and isinstance(node.func.obj, Identifier)):
            qualified_name = f"{node.func.obj.name}.{node.func.attr}"
            if qualified_name in self.module.globals and isinstance(self.module.globals[qualified_name], ir.Function):
                llvm_fn = self.module.globals[qualified_name]
                args = [self._gen_expr(a) for a in node.args]
                result = self.builder.call(llvm_fn, args)
                # Don't release args — the function's own retain/release cycle handles them
                if llvm_fn.function_type.return_type == ir.VoidType():
                    return self._box_none()
                if llvm_fn.function_type.return_type == I64:
                    return self._call("fray_int", [result])
                if llvm_fn.function_type.return_type == DOUBLE:
                    return self._call("fray_float", [result])
                if llvm_fn.function_type.return_type == PTR:
                    return result
                return self._box_none()

        # Enum variant construction: Color.Red(val1, val2) via MemberAccess
        if isinstance(node.func, MemberAccess) and isinstance(node.func.obj, Identifier):
            enum_name = node.func.obj.name
            if enum_name in self.enum_defs:
                enum_info = self.enum_defs[enum_name]
                variant_name = node.func.attr
                for vname, vparams in enum_info["cases"]:
                    if vname == variant_name:
                        args = [self._gen_expr(a) for a in node.args]
                        if len(args) != len(vparams):
                            raise CodegenError(
                                f"{enum_name}.{variant_name}() takes {len(vparams)} arg(s) ({len(args)} given)")
                        sv = self._gen_enum_new(enum_name, enum_info, variant_name)
                        # Set each parameter field — field_set takes ownership of val
                        for i, pname in enumerate(vparams):
                            self._call("fray_struct_field_set",
                                        [sv, self._string_const(pname), args[i]])
                        # Don't release args — fray_struct_field_set took ownership
                        return sv
                raise CodegenError(f"'{enum_name}' has no variant '{variant_name}'")

        # Struct construction: Foo(val1, val2, ...) where Foo is a struct type
        if func_name and func_name in self.struct_defs:
            field_names = self.struct_defs[func_name]
            args = [self._gen_expr(a) for a in node.args]
            if len(args) > len(field_names):
                raise CodegenError(f"{func_name}() takes at most {len(field_names)} arguments ({len(args)} given)")
            # Create struct instance: fray_struct_new(name, nfields, field_names_ptr)
            name_str = self._string_const(func_name)
            nfields = ir.Constant(I64, len(field_names))
            # Build array of field name pointers as global constants
            arr_name = f".struct_fields.{func_name}"
            if arr_name not in self.module.globals:
                str_globals = []
                for i, fn in enumerate(field_names):
                    data = fn.encode("utf-8") + b"\0"
                    arr_type = ir.ArrayType(I8, len(data))
                    const = ir.Constant(arr_type, [ir.Constant(I8, b) for b in data])
                    gv = ir.GlobalVariable(self.module, arr_type, f".struct_field.{func_name}.{i}")
                    gv.initializer = const
                    gv.global_constant = True
                    gv.linkage = "internal"
                    str_globals.append(gv)
                fields_arr_type = ir.ArrayType(PTR, len(field_names))
                arr = ir.GlobalVariable(self.module, fields_arr_type, arr_name)
                arr.initializer = ir.Constant(fields_arr_type, str_globals)
                arr.global_constant = True
                arr.linkage = "internal"
            arr_ptr = self.builder.bitcast(self.module.get_global(arr_name), PTR)
            obj = self._call("fray_struct_new", [name_str, nfields, arr_ptr])
            # Set each field: fray_struct_field_set(obj, field_name, value)
            # field_set takes ownership of val; string constants are raw
            # byte arrays (not FrayValues) so they must not be released.
            for fn, arg in zip(field_names, args):
                fn_str = self._string_const(fn)
                self._call("fray_struct_field_set", [obj, fn_str, arg])
            # Remaining fields default to None (set by fray_struct_new)
            return obj

        # Thread spawn: spawn(user_fn) wraps the compiled LLVM function in
        # a first-class function object and hands it to the runtime.
        if func_name == "spawn":
            if len(node.args) != 1:
                raise CodegenError("spawn() takes exactly one function")
            target = node.args[0]
            if not isinstance(target, Identifier):
                raise CodegenError("spawn() target must be a function name")
            if target.name not in self.module.globals or \
               not isinstance(self.module.globals[target.name], ir.Function):
                raise CodegenError(f"spawn() target '{target.name}' is not a function")
            callee_fn = self.infer._funcs.get(target.name) if self.infer else None
            if callee_fn is not None and callee_fn.params:
                # The runtime invokes a spawned function with no arguments;
                # the oracle rejects this too, so report it instead of
                # emitting a call that cannot be built.
                raise CodegenError(
                    f"spawn() target '{target.name}' must take no parameters")
            llfn = self.module.globals[target.name]
            thunk_name = f"fray_thunk.{target.name}"
            if thunk_name not in self.module.globals:
                self._gen_spawn_thunk(thunk_name, llfn)
            thunk = self.module.globals[thunk_name]
            thunk_obj = self._call("fray_function", [
                self._string_const(target.name),
                self.builder.bitcast(thunk, PTR),
                ir.Constant(I32, 0),
            ])
            tid = self._call("fray_thread_spawn", [thunk_obj])
            self._release(thunk_obj)
            return self._box_int(tid)

        if func_name == "join":
            if len(node.args) != 1:
                raise CodegenError("join() takes exactly one argument")
            tid_boxed = self._gen_expr(node.args[0])
            tid = self._extract_int(tid_boxed)
            self._release(tid_boxed)
            self._call("fray_thread_join", [tid])
            return self._box_none()

        if func_name == "joinAll":
            self._call("fray_thread_join_all", [])
            return self._box_none()

        # Handle member access calls: x.append(v), x.depend, atomic methods
        if isinstance(node.func, MemberAccess):
            obj = self._gen_expr(node.func.obj)
            # Atomic methods: a.get() / a.set(v) / a.add(v). The runtime
            # validates the FRAY_GC_ATOMIC flag and throws TypeError for
            # non-atomic objects, matching the oracle.
            if node.func.attr in ("get", "set", "add"):
                if node.func.attr == "get":
                    result = self._call("fray_atomic_get", [obj])
                    self._release(obj)
                    return result
                if node.func.attr == "set":
                    if not node.args:
                        raise CodegenError("set() takes exactly one argument")
                    arg = self._gen_expr(node.args[0])
                    self._call("fray_atomic_set", [obj, arg])
                    self._release(arg)
                    self._release(obj)
                    return self._box_none()
                # add
                if not node.args:
                    raise CodegenError("add() takes exactly one argument")
                arg = self._gen_expr(node.args[0])
                result = self._call("fray_atomic_add", [obj, arg])
                self._release(arg)
                self._release(obj)
                return result
            if node.func.attr == "append":
                if node.args:
                    elem = self._gen_expr(node.args[0])
                    # fray_append dispatches on the tag (set append dedupes,
                    # list append appends) and keeps the retain-owning
                    # convention: the reference this call holds is dropped
                    # here.
                    self._call("fray_append", [obj, elem])
                    self._release(elem)
                self._release(obj)
                return self._box_none()
            if node.func.attr == "depend":
                result = self._call("fray_depend", [obj])
                self._release(obj)
                return result
            # A callee that is neither a known function nor a known method: a
            # field holding a function, or a nested qualified path. The field
            # is read and the value is called through the runtime, which
            # decides whether it is callable at all and whether the argument
            # count matches its declaration.
            self._release(obj)
            return self._gen_value_call(node.func, node.args)

        # Async calls (Phase 7): resolved BEFORE the plain globals lookup
        # because the trampoline lives under fray_async.<name>, not <name>.
        if func_name in self.async_fns:
            tramp = self.module.globals.get("fray_async." + func_name)
            if not isinstance(tramp, ir.Function):
                raise CodegenError(f"unknown async function '{func_name}'")
            # The trampoline's own type is always one argument (the coroutine
            # handle is not it), so the arity comes from the declaration.
            nargs = len(node.args)
            arity = self._declared_arity(func_name)
            if arity is not None:
                self._check_arity(func_name, arity, nargs, node.line, node.col)
            # Arguments travel with the coroutine as a list, so the body can
            # unpack its parameters without the runtime knowing the arity.
            argv = self._gen_arg_list(node.args)
            fn_obj = self._call("fray_function", [
                self._string_const(func_name),
                self.builder.bitcast(tramp, PTR),
                ir.Constant(I32, nargs),
            ])
            handle = self._call("fray_coro_start_argv", [fn_obj, argv])
            self._release(fn_obj)
            self._release(argv)
            return handle

        # A local holding a value — `g = add`, or a parameter, which is how a
        # function is passed on and called back — resolves first: the innermost
        # binding wins, as in the oracle, even when a function of that name
        # exists.
        if (isinstance(node.func, Identifier)
                and node.func.name in self.named_values):
            return self._gen_value_call(node.func, node.args)

        # Handle user-defined functions
        if func_name and func_name in self.module.globals:
            fn = self.module.globals[func_name]
            if isinstance(fn, ir.Function):
                # Unboxed call: skips boxing the arguments and unboxing
                # them at entry. The result comes back raw and is boxed
                # here, so the value this returns is still owned.
                for kind in ("int", "float"):
                    raw = self._raw_call(node, kind)
                    if raw is not None:
                        return (self._box_int(raw) if kind == "int"
                                else self._box_float(raw))
                # The LLVM function's signature is the declared arity, for
                # imported functions too (they are pre-declared on import).
                self._check_arity(func_name, len(fn.function_type.args),
                                  len(node.args), node.line, node.col)
                args = [self._gen_expr(a) for a in node.args]
                if not self.builder.block.is_terminated:
                    return self.builder.call(fn, args)
                self._release_all(args)
                return self._box_none()

        # A module global holding a value: `g = add` at module scope, then
        # `g(1, 2)`. A name that is neither a variable nor a function is still a
        # compile-time error — a typo should not wait until the program runs.
        if (isinstance(node.func, Identifier)
                and node.func.name in self._module_globals):
            return self._gen_value_call(node.func, node.args)

        # Any other callee is an expression that produces a value: an index
        # (`xs[0](1, 2)`) or another call's result. Same runtime call, so a value
        # that turns out not to be callable is reported here as it is there.
        if not isinstance(node.func, Identifier):
            return self._gen_value_call(node.func, node.args)

        raise CodegenError(f"unknown function '{func_name}'")


# ── Public API ──

# Runtime sources compiled into every binary (and into the JIT helper lib).
RUNTIME_SOURCES = ["objects.c", "cycles.c", "ops.c", "printing.c",
                   "builtins.c", "threads.c", "atomics.c",
                   "coroutine.c", "io.c", "structs.c", "maps.c",
                   "option_result.c"]

_JIT_RUNTIME_HANDLE = None


def _ensure_runtime_for_jit():
    """Load libfrayrt into this process with RTLD_GLOBAL so MCJIT resolves
    the fray_* runtime symbols the generated IR references (via dlsym).
    Builds a shared library once and caches it in the temp dir."""
    global _JIT_RUNTIME_HANDLE
    if _JIT_RUNTIME_HANDLE is not None:
        return
    gcc = target.find_c_compiler()
    if not gcc:
        raise RuntimeError("C compiler required for JIT mode")
    runtime_dir = os.path.join(os.path.dirname(__file__), "..", "runtime")
    src_paths = [os.path.join(runtime_dir, s) for s in RUNTIME_SOURCES]
    lib_path = os.path.join(tempfile.gettempdir(),
                            f"frayrt-jit-{target.host_triple()}.so")
    newest_src = max(os.path.getmtime(p) for p in src_paths)
    if (not os.path.exists(lib_path)
            or os.path.getmtime(lib_path) < newest_src):
        subprocess.run(
            [gcc, "-shared", "-O2", "-fPIC", "-pthread",
             "-o", lib_path] + src_paths + ["-lm"],
            check=True, capture_output=True,
        )
    _JIT_RUNTIME_HANDLE = ctypes.CDLL(lib_path, mode=os.RTLD_GLOBAL)

def compile_to_ir(source: str, filename: str = "<string>") -> str:
    from sema import analyze
    tokens = tokenize(source, filename)
    ast = parse(tokens, filename)
    analyze(ast, filename)
    inference = Inference().infer(ast)
    codegen = CodeGen()
    codegen.infer = inference
    codegen._source_file = filename
    # Every module path resolves under the program's directory, so a module's
    # own imports mean the same thing at any depth.
    codegen._root = (os.path.dirname(os.path.abspath(filename))
                     if filename and not filename.startswith("<") else os.getcwd())
    codegen._package = ""
    codegen.compile(ast)
    return str(codegen.module)


def compile_to_object(source: str, output_path: str, filename: str = "<string>"):
    ir_text = compile_to_ir(source, filename)
    mod = binding.parse_assembly(ir_text)
    mod.verify()

    # Host triple + small code model for object emission.
    # Small code model avoids _GLOBAL_OFFSET_TABLE_ references
    # that break MinGW gcc linking (and is the default on Linux).
    tgt = binding.Target.from_triple(target.host_triple())
    machine = tgt.create_target_machine(opt=2, codemodel='small')
    obj_data = machine.emit_object(mod)

    with open(output_path, 'wb') as f:
        f.write(obj_data)


def compile_program(source: str, output_path: str, filename: str = "<string>",
                    runtime_dir: str = None):
    if runtime_dir is None:
        runtime_dir = os.path.join(os.path.dirname(__file__), "..", "runtime")

    import shutil
    gcc = target.find_c_compiler()
    if not gcc:
        raise RuntimeError("No C compiler found")

    env = None
    if target.is_windows():
        # Windows only: make the MinGW DLLs next to gcc resolvable for
        # the produced binary; POSIX systems need no PATH munging.
        env = os.environ.copy()
        env["PATH"] = os.path.dirname(gcc) + ";" + env.get("PATH", "")

    obj_path = output_path + ".o"
    compile_to_object(source, obj_path, filename)

    # Phase 5: the runtime is split into focused translation units.
    # Phase 6 adds the thread registry and atomics; Phase 7 the
    # fiber coroutine scheduler and channels.
    runtime_sources = RUNTIME_SOURCES
    runtime_objs = []
    for name in runtime_sources:
        rc = os.path.join(runtime_dir, name)
        ro = output_path + ".rt." + name + ".o"
        subprocess.run(
            [gcc, "-c", "-O2", "-o", ro, rc],
            check=True, capture_output=True, env=env,
        )
        runtime_objs.append(ro)

    # Linux: gcc defaults to PIE executables, but the emitted objects use
    # small-code-model absolute relocations (non-PIC), so link non-PIE —
    # the same fixed-address model the Windows/MinGW link uses.
    link_flags = ["-lm", "-lpthread", "-lgcc"]
    if not target.is_windows() and not target.is_macos():
        link_flags.insert(0, "-no-pie")
    subprocess.run(
        [gcc, "-o", output_path, obj_path] + runtime_objs + link_flags,
        check=True, capture_output=True, env=env,
    )

    os.remove(obj_path)
    for ro in runtime_objs:
        if os.path.exists(ro):
            os.remove(ro)


def run_ir_jit(ir_text: str):
    _ensure_runtime_for_jit()
    mod = binding.parse_assembly(ir_text)
    mod.verify()
    tgt = binding.Target.from_default_triple()
    machine = tgt.create_target_machine()
    ee = binding.create_mcjit_compiler(mod, machine)
    ee.run_static_constructors()
    addr = ee.get_function_address("main")
    if addr == 0:
        raise RuntimeError("main() not found")
    # main is now the C signature (argc, argv); main passes them to
    # fray_init_args, so pass the process's argv and an empty list for argc.
    cfunc = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_void_p)(addr)
    result = cfunc(0, None)
    ee.run_static_destructors()
    return result
