"""fray type inference — Phase 5 v1.

A flow-insensitive, bottom-up abstract interpreter over the AST. It
propagates a small lattice of ground types (INT, FLOAT, BOOL, STRING,
LIST, TUPLE, SET, NONE) plus UNKNOWN ("could be anything") and VOID
("no information yet"; never used for specialization).

Design notes:

  * Flow-insensitive on purpose: each variable keeps the join of every
    type ever assigned to it anywhere in its scope. Monomorphic
    specialization must be *sound under all orders*, and this is the
    simplest such discipline. It costs some precision (a variable used
    as an int in one place and a string in another becomes UNKNOWN)
    and buys guaranteed correctness without a dataflow solver.

  * Function results are inferred per call site: the callee body is
    re-run in a scratch namespace with the argument types bound as the
    parameters' initial types, and the result is memoized on the
    (callee, argument-type-tuple) key. A site that always passes ints
    sees an int-typed result even if another site passes strings —
    without any bidirectional machinery.

  * Self-recursion is inferred optimistically and then *checked*
    (``_try_self_recursive``). The recursive call itself carries no
    information, so the body is first walked with recursive self-calls
    replaced by a wildcard that propagates instead of widening to
    UNKNOWN; the join of the remaining contributions is the return-type
    *hypothesis*. The body is then walked again with recursive calls
    answering the hypothesis, and the hypothesis is accepted only when
    that walk reproduces it exactly — a genuine fixpoint. A body whose
    recursive branch contradicts the hypothesis (say a ``/`` that makes
    the recursive result FLOAT while the base case is INT) fails the
    check and keeps the old widening, so a wrong claim can never stick.
    Mutual recursion and calls with mismatched argument types stay
    conservative: they widen to UNKNOWN.

  * Everything here is advisory: codegen falls back to the dynamic
    (boxed) path whenever a type is not a ground type. Wrong advice
    costs a missed optimization, never a wrong answer.
"""

from __future__ import annotations

from ast_nodes import (
    Node, Program, Assignment, AugmentedAssignment, ExprStatement,
    ReturnStatement, ConstStatement,
    FunctionDef, IfStatement, ForLoop, WhileLoop, TryStatement,
    IntLiteral, FloatLiteral, StringLiteral, BoolLiteral, ConstantLiteral,
    Identifier, BinaryOp, UnaryOp, Call, MemberAccess, Index,
    ListLiteral, SetLiteral, TupleLiteral,
)

from own_lattice import INT, FLOAT, BOOL, STRING, LIST, TUPLE, SET, NONE, UNKNOWN, VOID, TypeNode


_CMP_OPS = {"==", "!=", "<", ">", "<=", ">="}
_BOOL_OPS = {"and", "or", "xor", "xnor"}
_ARITH_NUM = {"+", "-", "*"}

# A private lattice element used only while computing a self-recursion
# hypothesis: "this value is whatever the recursive result turns out to
# be". It behaves like VOID (no information) when joined and propagates
# through arithmetic instead of collapsing the whole expression to
# UNKNOWN, which is what makes a recursive body's base cases visible.
# It never escapes an inference walk: the hypothesis is discarded unless
# the fixpoint check accepts it, and `type_of` maps it to VOID.
_WILD = TypeNode("WILD")


def _walk_nodes(node):
    """Yield *node* and every AST node nested inside it (bodies, branch
    tuples, argument lists — anything reachable through node fields)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        if isinstance(current, (list, tuple)):
            stack.extend(current)
            continue
        if not isinstance(current, Node):
            continue
        yield current
        for value in vars(current).values():
            if isinstance(value, (list, tuple)):
                stack.extend(value)
            elif isinstance(value, Node):
                stack.append(value)


def _all_paths_return(stmts: list) -> bool:
    """True when every path through *stmts* ends in ``return <value>``.

    Only used to guard the raw (unboxed) ABI: such a body must never fall
    off the end, because there is no unboxed representation of ``none``.
    Statement lists are checked by their last statement, which is where
    every surviving path ends up.
    """
    def stmt_returns(stmt) -> bool:
        if isinstance(stmt, ReturnStatement):
            return stmt.value is not None
        if isinstance(stmt, IfStatement):
            if not stmt.else_body:
                return False
            return (all(_all_paths_return(body)
                        for _cond, body in stmt.branches)
                    and _all_paths_return(stmt.else_body))
        if isinstance(stmt, TryStatement):
            return (_all_paths_return(stmt.body)
                    and all(_all_paths_return(body)
                            for _name, body in stmt.except_clauses)
                    and _all_paths_return(stmt.finally_body or []))
        return False

    return bool(stmts) and stmt_returns(stmts[-1])


def _has_bare_return(stmts: list) -> bool:
    """True when any ``return`` in *stmts* (at any nesting depth) has no
    value — such a path has no unboxed representation."""
    for stmt in stmts:
        for node in _walk_nodes(stmt):
            if isinstance(node, ReturnStatement) and node.value is None:
                return True
    return False


class Signature:
    """Inferred parameter types of one function (for tests/debugging)."""

    __slots__ = ("params", "ret")

    def __init__(self, params: dict[str, TypeNode], ret: TypeNode):
        self.params = params
        self.ret = ret

    def __repr__(self) -> str:
        return f"Signature({self.params} -> {self.ret.tag})"


class Inference:
    def __init__(self):
        # (scope, name) -> TypeNode. Module scope is ""; function scopes
        # are named. Memo tables below are scratch namespaces.
        self.var_types: dict[tuple[str, str], TypeNode] = {}
        # function name -> Signature (widened parameter view; tests)
        self.signatures: dict[str, Signature] = {}
        self._funcs: dict[str, FunctionDef] = {}
        # (callee, (arg tags...)) -> return TypeNode
        self._memo: dict[tuple[str, tuple[str, ...]], TypeNode] = {}
        self._widen: dict[str, TypeNode] = {}      # callee -> widened ret
        self._widen_params: dict[str, dict[str, TypeNode]] = {}
        # recursion guard: stack of callee names currently being inferred
        self._in_progress: set[str] = set()
        # Self-recursion hypothesis in force: callee -> (site key or None
        # for "any site", assumed return type).
        self._assume: dict[str, tuple[tuple[str, ...] | None, TypeNode]] = {}
        # While a hypothesis is being trialled, memo writes land here so a
        # rejected trial cannot poison the real memo table.
        self._pending: dict | None = None
        # Site results proven by the self-recursion fixpoint, keyed like
        # _memo. Only these may back a raw (unboxed) ABI: the legacy
        # top-level-only measure can under-claim a return type, and an
        # unboxed body must not be built from an under-claim.
        self._proved: dict[tuple[str, tuple[str, ...]], TypeNode] = {}
        # Cache for the pure-AST predicates (they never change).
        self._self_call_cache: dict[str, bool] = {}
        self._raw_body_cache: dict[str, bool] = {}

    # ── Entry ──

    def infer(self, program: Program) -> "Inference":
        for stmt in program.body:
            if isinstance(stmt, FunctionDef):
                self._funcs[stmt.name] = stmt
        # Body pass over every function first, so widen signatures exist
        # for self/forward references made from other functions.
        for fn in self._funcs.values():
            self._function_widen(fn)
        # Module pass: infers module variables and memoizes per-site
        # function results.
        for stmt in program.body:
            self._stmt(stmt)
        self._resolve_call_sites(program)
        return self

    def _params_snapshot(self) -> dict:
        return {name: tuple(sorted((p, t.tag) for p, t in params.items()))
                for name, params in self._widen_params.items()}

    def _resolve_call_sites(self, program: Program, rounds: int = 4):
        """Iterate call-site discovery until parameter types settle.

        Parameters are the one thing inference learns lazily: a function's
        parameter types are the join over its call sites, and a site inside
        another body is only resolved when that body is walked. Codegen
        decides a parameter's slot representation the moment it emits the
        body, so an unresolved site that is only discovered later — or
        discovered with a different argument type — would be invisible to
        that decision and the value would be coerced at entry. Re-walking
        every body with the current parameter guesses (and re-walking
        module-level code) closes that gap. Joins only ever widen, so the
        result is never less conservative than a single pass.
        """
        for _ in range(rounds):
            before = self._params_snapshot()
            for fn in self._funcs.values():
                guessed = self._widen_params.get(fn.name, {})
                arg_tys = [guessed.get(p, VOID) for p in fn.params]
                self._walk_call(fn, arg_tys)
            for stmt in program.body:
                if not isinstance(stmt, FunctionDef):
                    self._stmt(stmt, ns="")
            if self._params_snapshot() == before:
                break

    def sig(self, name: str) -> Signature | None:
        ws = self._widen_params.get(name)
        if ws is None:
            return None
        return Signature(dict(ws), self._widen.get(name, UNKNOWN))

    def type_of(self, name: str, scope: str = "") -> TypeNode:
        ty = self.var_types.get((scope, name), VOID)
        # A hypothesis wildcard is not a real type: it must never reach a
        # consumer that would try to specialize on it.
        return VOID if ty is _WILD else ty

    # ── Slots ──

    def _join(self, ns: str, name: str, ty: TypeNode):
        """Widen a slot. UNKNOWN is a no-op: an unrecorded slot is VOID,
        which behaves like UNKNOWN downstream. Conflicting ground types
        widen to UNKNOWN."""
        if ty is VOID or ty is UNKNOWN:
            return
        old = self.var_types.get((ns, name), VOID)
        if ty is _WILD:
            # A wildcard slot stays a wildcard until real information
            # arrives, and never widens one that already has some.
            if old is VOID:
                self.var_types[(ns, name)] = _WILD
            return
        if old is _WILD:
            self.var_types[(ns, name)] = ty
            return
        self.var_types[(ns, name)] = UNKNOWN if old not in (VOID, ty) else ty

    # ── Function analysis ──

    def _function_widen(self, node: FunctionDef):
        """Whole-function pass under UNKNOWN parameters: collects the
        widened return type and parameter joins from the body."""
        if node.name in self._widen:
            return
        # Pre-register _widen_params BEFORE analyzing the body so that
        # forward/cross-references from other functions don't KeyError.
        self._widen_params[node.name] = {p: VOID for p in node.params}
        self._in_progress.add(node.name)
        body_ret = VOID
        for stmt in node.body:
            self._stmt(stmt, ns=node.name)
            if isinstance(stmt, ReturnStatement) and stmt.value is not None:
                body_ret = _join(body_ret, self._expr(stmt.value, ns=node.name))
        self._in_progress.discard(node.name)
        ret = body_ret if body_ret is not VOID else NONE
        self._widen[node.name] = ret
        params = {p: self.var_types.get((node.name, p), VOID) for p in node.params}
        self._widen_params[node.name] = params

    def _store_memo(self, key, value: TypeNode):
        """Record a site result — in the trial overlay while a recursion
        hypothesis is being tested, otherwise in the real memo table."""
        if self._pending is not None:
            self._pending[key] = value
        else:
            self._memo[key] = value

    def _walk_call(self, fn: FunctionDef, arg_tys: list[TypeNode],
                   all_returns: bool = False) -> TypeNode:
        """One call frame: bind the argument types as the parameters'
        initial types and walk the body. Returns the join of the return
        statements (NONE for a body that returns nothing).

        `all_returns` switches from the legacy top-level-only measure to a
        join over every return at any nesting depth. A specialization
        claim must cover *all* the ways a body can return, so the
        self-recursion fixpoint uses it.
        """
        for pname, aty in zip(fn.params, arg_tys):
            if aty is not VOID and aty is not UNKNOWN:
                self.var_types[(fn.name, pname)] = aty
        body_ret = VOID
        for stmt in fn.body:
            self._stmt(stmt, ns=fn.name)
            if not all_returns and isinstance(stmt, ReturnStatement) \
                    and stmt.value is not None:
                body_ret = _join(body_ret, self._expr(stmt.value, ns=fn.name))
        if all_returns:
            body_ret = self._return_join(fn.body, fn.name)
        return body_ret if body_ret is not VOID else NONE

    def _return_join(self, stmts: list, ns: str) -> TypeNode:
        """Join of the types of every ``return <value>`` reachable in
        *stmts*, however deeply nested."""
        total = VOID
        for stmt in stmts:
            if isinstance(stmt, ReturnStatement):
                if stmt.value is not None:
                    total = _join(total, self._expr(stmt.value, ns=ns))
            elif isinstance(stmt, IfStatement):
                for cond, body in stmt.branches:
                    self._expr(cond, ns=ns)
                    total = _join(total, self._return_join(body, ns))
                total = _join(total, self._return_join(stmt.else_body or [], ns))
            elif isinstance(stmt, (ForLoop, WhileLoop)):
                total = _join(total, self._return_join(stmt.body, ns))
            elif isinstance(stmt, TryStatement):
                total = _join(total, self._return_join(stmt.body, ns))
                for _name, body in stmt.except_clauses:
                    total = _join(total, self._return_join(body, ns))
                total = _join(total, self._return_join(stmt.finally_body or [], ns))
        return total

    def _publish_params(self, fname: str, fn: FunctionDef,
                        arg_tys: list[TypeNode], result: TypeNode):
        """Widen the callee's public parameter types (and ret) with this
        site's argument types."""
        if fname not in self._widen_params:
            self._widen_params[fname] = {p: VOID for p in fn.params}
        for pname, aty in zip(fn.params, arg_tys):
            old = self._widen_params[fname].get(pname, VOID)
            self._widen_params[fname][pname] = (
                UNKNOWN if old not in (VOID, aty) else aty)
        w = self._widen.get(fname, VOID)
        if w is not VOID and w is not result and result is not VOID:
            self._widen[fname] = UNKNOWN

    def _calls_itself(self, fname: str) -> bool:
        """True when `fname`'s body contains a direct call to `fname`.
        Only such functions can use a recursion hypothesis."""
        cached = self._self_call_cache.get(fname)
        if cached is None:
            fn = self._funcs.get(fname)
            cached = False
            if fn is not None:
                cached = any(
                    isinstance(node, Call)
                    and isinstance(node.func, Identifier)
                    and node.func.name == fname
                    for stmt in fn.body for node in _walk_nodes(stmt))
            self._self_call_cache[fname] = cached
        return cached

    def _hypothesis(self, fname: str, arg_tys: list[TypeNode]):
        """Return-type guess for a self-recursive function: walk the body
        with recursive self-calls answering a wildcard, so the join of the
        remaining return contributions is visible. None when the body has
        nothing to guess from."""
        fn = self._funcs.get(fname)
        if fn is None:
            return None
        saved_types = self.var_types
        saved_widen = dict(self._widen)
        saved_params = {k: dict(v) for k, v in self._widen_params.items()}
        saved_pending = self._pending
        outer = self._assume.get(fname)
        self.var_types = dict(saved_types)
        self._pending = {}          # trial results are never kept
        self._assume[fname] = (None, _WILD)
        self._in_progress.add(fname)
        try:
            hyp = self._walk_call(fn, arg_tys, all_returns=True)
        finally:
            self._in_progress.discard(fname)
            if outer is None:
                self._assume.pop(fname, None)
            else:
                self._assume[fname] = outer
            self._pending = saved_pending
            self.var_types = saved_types
            self._widen = saved_widen
            self._widen_params = saved_params
        if hyp is _WILD or hyp in (VOID, NONE, UNKNOWN):
            return None
        return hyp

    def _try_self_recursive(self, fname: str, key, arg_tys: list[TypeNode],
                            fn: FunctionDef):
        """Prove a fixpoint return type for a directly self-recursive
        function, or return None to leave the legacy widening in place.

        The hypothesis is derived with recursive calls contributing
        nothing, then the body is re-walked with recursive calls answering
        the hypothesis. Accepting only an exact reproduction means the
        assumption is self-consistent for this site's argument types, so
        it cannot make a false claim stick.
        """
        hyp = self._hypothesis(fname, arg_tys)
        if hyp is None:
            return None
        saved_types = self.var_types
        saved_widen = dict(self._widen)
        saved_params = {k: dict(v) for k, v in self._widen_params.items()}
        trial: dict = {}
        self.var_types = dict(saved_types)
        self._pending = trial
        self._assume[fname] = (key, hyp)
        self._in_progress.add(fname)
        try:
            result = self._walk_call(fn, arg_tys, all_returns=True)
        finally:
            self._in_progress.discard(fname)
            self._assume.pop(fname, None)
            self._pending = None
            self.var_types = saved_types
        if result is not hyp:
            # Not a fixpoint (e.g. '/' turning the recursive result into a
            # float): drop the trial, including every signature the walk
            # published while it was assuming the hypothesis.
            self._widen = saved_widen
            self._widen_params = saved_params
            return None
        self._memo.update(trial)
        self._memo[key] = hyp
        self._proved[key] = hyp
        self._publish_params(fname, fn, arg_tys, hyp)
        if self._widen.get(fname, VOID) in (VOID, UNKNOWN):
            self._widen[fname] = hyp
        return hyp

    def _call_result(self, fname: str, arg_tys: list[TypeNode], argv: list) -> TypeNode:
        """Return type of calling `fname` with these argument types,
        memoized per (callee, argument-type tuple)."""
        for aty in arg_tys:
            if aty is _WILD:
                return _WILD
        key = (fname, tuple(t.tag for t in arg_tys))
        if self._pending is not None and key in self._pending:
            return self._pending[key]
        if key in self._memo:
            return self._memo[key]
        fn = self._funcs.get(fname)
        if fn is None:
            return UNKNOWN
        if fname in self._in_progress:
            # A call from inside this callee's own inference: use the
            # hypothesis when it was made for exactly this argument-type
            # tuple, otherwise widen as before.
            assumed = self._assume.get(fname)
            if assumed is not None and (assumed[0] is None or assumed[0] == key):
                return assumed[1]
            return self._widen.get(fname, UNKNOWN)
        if self._pending is None and self._calls_itself(fname):
            proved = self._try_self_recursive(fname, key, arg_tys, fn)
            if proved is not None:
                return proved
        self._in_progress.add(fname)
        self._store_memo(key, UNKNOWN)  # provisional: breaks mutual recursion
        saved = self.var_types
        self.var_types = dict(saved)  # scratch namespace for the call frame
        try:
            result = self._walk_call(fn, arg_tys)
        finally:
            self.var_types = saved
            self._in_progress.discard(fname)
        self._store_memo(key, result)
        self._publish_params(fname, fn, arg_tys, result)
        return result

    # ── Raw (unboxed) ABI ──

    def raw_body_ok(self, fname: str) -> bool:
        """True when `fname`'s body is shaped so an unboxed body can have
        the same observable behaviour: every path returns a value, so
        there is never an implicit ``none`` to represent."""
        cached = self._raw_body_cache.get(fname)
        if cached is None:
            fn = self._funcs.get(fname)
            cached = bool(
                fn is not None
                and not getattr(fn, "is_async", False)
                and _all_paths_return(fn.body)
                and not _has_bare_return(fn.body))
            self._raw_body_cache[fname] = cached
        return cached

    def raw_param_kinds(self, fname: str):
        """Raw representation of every parameter, or None when the
        function is not uniformly raw-specializable. Non-raw parameters
        keep the function on the boxed ABI only."""
        if not self.raw_body_ok(fname):
            return None
        fn = self._funcs.get(fname)
        widened = self._widen_params.get(fname)
        if widened is None:
            return None
        kinds = []
        for pname in fn.params:
            want = widened.get(pname, VOID)
            body_ty = self.var_types.get((fname, pname), VOID)
            if want is INT and body_ty in (INT, VOID):
                kinds.append("int")
            elif want is FLOAT and body_ty in (FLOAT, VOID):
                kinds.append("float")
            else:
                return None
        return tuple(kinds)

    def raw_specialized(self, fname: str, arg_tys: list[TypeNode]):
        """(parameter kinds, return kind) when a call to `fname` whose
        arguments have exactly `arg_tys` may use the raw ABI.

        The site's own inferred result doubles as the return kind, so a
        site can only reach an unboxed body that really does return that
        kind of value for those arguments. Anything else stays on the
        boxed path (a missed optimization, never a wrong answer).
        """
        kinds = self.raw_param_kinds(fname)
        if kinds is None:
            return None
        if len(arg_tys) != len(kinds):
            return None
        for aty, kind in zip(arg_tys, kinds):
            if (kind == "int" and aty is not INT) or (kind == "float" and aty is not FLOAT):
                return None
        result = self._call_result(fname, list(arg_tys), [])
        # The unboxed body is only safe when the return type is *proven*
        # (a fixpoint over every return in the body), never merely the
        # legacy join of the top-level returns.
        if self._proved.get((fname, tuple(t.tag for t in arg_tys))) is not result:
            return None
        if result is INT:
            return kinds, "int"
        if result is FLOAT:
            return kinds, "float"
        return None

    # ── Statements ──

    def _stmt(self, node, ns: str = ""):
        if isinstance(node, (Assignment, ConstStatement)):
            ty = self._expr(node.value, ns=ns)
            if isinstance(node, Assignment) and isinstance(node.target, Identifier):
                self._join(ns, node.target.name, ty)
            elif isinstance(node, ConstStatement):
                self._join(ns, node.name, ty)
        elif isinstance(node, AugmentedAssignment):
            val_ty = self._expr(node.value, ns=ns)
            if isinstance(node.target, Identifier):
                base = node.op[:-1]
                cur = self.var_types.get((ns, node.target.name), VOID)
                self._join(ns, node.target.name,
                           _binop_result(base, cur, val_ty))
        elif isinstance(node, ExprStatement):
            self._expr(node.expr, ns=ns)
        elif isinstance(node, ReturnStatement):
            if node.value is not None:
                self._expr(node.value, ns=ns)
        elif isinstance(node, IfStatement):
            for cond, body in node.branches:
                self._expr(cond, ns=ns)
                for s in body:
                    self._stmt(s, ns=ns)
            for s in node.else_body or []:
                self._stmt(s, ns=ns)
        elif isinstance(node, ForLoop):
            iter_ty = self._expr(node.iterable, ns=ns)
            # Element types are untracked in v1, except range(): a range
            # is always a list of ints.
            is_range = (isinstance(node.iterable, Call)
                        and isinstance(node.iterable.func, Identifier)
                        and node.iterable.func.name == "range")
            elem = INT if is_range else UNKNOWN
            self._join(ns, node.var, elem)
            for s in node.body:
                self._stmt(s, ns=ns)
        elif isinstance(node, WhileLoop):
            self._expr(node.condition, ns=ns)
            for s in node.body:
                self._stmt(s, ns=ns)
        elif isinstance(node, TryStatement):
            for s in node.body:
                self._stmt(s, ns=ns)
            for _name, body in node.except_clauses:
                for s in body:
                    self._stmt(s, ns=ns)
            for s in node.finally_body or []:
                self._stmt(s, ns=ns)

    # ── Expressions ──

    def _expr(self, node, ns: str = "") -> TypeNode:
        if node is None:
            return NONE
        if isinstance(node, IntLiteral):
            return INT
        if isinstance(node, FloatLiteral):
            return FLOAT
        if isinstance(node, BoolLiteral):
            return BOOL
        if isinstance(node, StringLiteral):
            return STRING
        if isinstance(node, ConstantLiteral):
            if node.name == "null":
                return VOID  # none-typed; joins widen any slot it touches
            return FLOAT
        if isinstance(node, Identifier):
            return self.var_types.get((ns, node.name), VOID)
        if isinstance(node, BinaryOp):
            lt = self._expr(node.left, ns=ns)
            rt = self._expr(node.right, ns=ns)
            return _binop_result(node.op, lt, rt)
        if isinstance(node, UnaryOp):
            ty = self._expr(node.operand, ns=ns)
            if node.op == "not":
                return BOOL
            if ty is _WILD:
                return _WILD
            if node.op == "-":
                if ty in (INT, BOOL):
                    return INT
                if ty is FLOAT:
                    return FLOAT
            return UNKNOWN
        if isinstance(node, (ListLiteral, SetLiteral, TupleLiteral)):
            for e in node.elements:
                self._expr(e, ns=ns)
            if isinstance(node, ListLiteral):
                return LIST
            if isinstance(node, SetLiteral):
                return SET
            return TUPLE
        if isinstance(node, Index):
            self._expr(node.obj, ns=ns)
            self._expr(node.index_expr, ns=ns)
            return UNKNOWN  # element types not tracked in v1
        if isinstance(node, MemberAccess):
            self._expr(node.obj, ns=ns)
            return UNKNOWN
        if isinstance(node, Call):
            return self._call(node, ns)
        return UNKNOWN

    def _call(self, node: Call, ns: str) -> TypeNode:
        arg_tys = [self._expr(a, ns=ns) for a in node.args]

        if isinstance(node.func, MemberAccess):
            return UNKNOWN

        fname = node.func.name if isinstance(node.func, Identifier) else None
        if fname is None:
            return UNKNOWN

        if fname == "range":
            return LIST
        if fname == "str":
            return STRING
        if fname in ("int", "isqrt", "round", "len"):
            return INT
        if fname in ("float", "sqrt"):
            return FLOAT
        if fname in ("mean", "med"):
            return FLOAT
        if fname in ("input", "inputStr"):
            return STRING
        if fname == "inputInt":
            return INT
        if fname == "inputFloat":
            return FLOAT
        if fname in ("min", "max", "sum", "abs", "mode", "mid"):
            return UNKNOWN

        # Async calls (Phase 7): the call expression evaluates to a
        # TAG_COROUTINE handle, never the body's return type (that value
        # arrives via `await`). UNKNOWN keeps slots boxed — boxing a
        # handle as a raw int would corrupt it.
        fn = self._funcs.get(fname)
        if fn is not None and getattr(fn, "is_async", False):
            return UNKNOWN

        if fname in self._funcs:
            return self._call_result(fname, arg_tys, node.args)

        return UNKNOWN


def _join(a: TypeNode, b: TypeNode) -> TypeNode:
    """Lattice join: VOID means "no information"; differing ground types
    widen to UNKNOWN. The hypothesis wildcard is also a no-op, so a
    recursive contribution never drags the join down."""
    if a is VOID or a is _WILD:
        return b
    if b is VOID or b is _WILD:
        return a
    return a if a is b else UNKNOWN


def _binop_result(op: str, lt: TypeNode, rt: TypeNode) -> TypeNode:
    if op in _CMP_OPS or op in _BOOL_OPS:
        return BOOL
    if lt is _WILD or rt is _WILD:
        # Unknown, but consistent with whatever the hypothesis is.
        return _WILD
    if op in ("+", "-", "*", "/", "//", "%", "^"):
        if lt is INT and rt is INT:
            return FLOAT if op == "/" else INT
        if lt in (INT, FLOAT, BOOL) and rt in (INT, FLOAT, BOOL):
            if lt is FLOAT or rt is FLOAT:
                return FLOAT if op != "//" else UNKNOWN
            if op in _ARITH_NUM:
                return INT
        if op == "+" and lt is STRING and rt is STRING:
            return STRING
        if op == "+" and lt is LIST and rt is LIST:
            return LIST
        if op == "*" and (lt is STRING and rt is INT or lt is INT and rt is STRING):
            return STRING
        return UNKNOWN
    return UNKNOWN
