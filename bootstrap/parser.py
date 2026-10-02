"""
fray parser — recursive descent with Pratt expression parsing.

Usage:
    from lexer import tokenize
    from parser import Parser
    tree = Parser(tokenize(source), "filename.fray").parse()
"""

from __future__ import annotations
from typing import Optional

from lexer import TT, Token
from ast_nodes import (
    AwaitExpr,
    Program, Assignment, AugmentedAssignment, ExprStatement,
    ReturnStatement, ConstStatement, ImportStatement, ExternFuncDecl,
    FunctionDef, IfStatement, ForLoop, WhileLoop, TryStatement,
    BreakStatement, ContinueStatement, DelStatement,
    IntLiteral, FloatLiteral, StringLiteral, BoolLiteral, ConstantLiteral,
    Identifier, BinaryOp, UnaryOp, Call, MemberAccess, Index, Slice, QuestionMark,
    ListLiteral, SetLiteral, TupleLiteral, MapLiteral,
    StructDef, StructField, StructLiteral, EnumDef, EnumCase,
    MatchCase, MatchExpression,
    Node,
)


class ParseError(Exception):
    def __init__(self, msg: str, token: Token, filename: str):
        self.token = token
        self.filename = filename
        super().__init__(f"{filename}:{token.line}:{token.col}: {msg}")


# Operator precedence: higher = tighter binding
PRECEDENCE = {
    "or": 1,
    "xor": 1,
    "xnor": 1,
    "and": 2,
    "not": 3,
    "==": 4, "!=": 4, "<": 4, ">": 4, "<=": 4, ">=": 4, "in": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "//": 6, "%": 6,
    "^": 7,  # right-associative
}


class Parser:
    def __init__(self, tokens: list[Token], filename: str = "<string>"):
        self.tokens = tokens
        self.pos = 0
        self.filename = filename

    def parse(self) -> Program:
        """Parse the full token stream into a Program AST."""
        program = Program(line=1, col=1)
        self._skip_newlines()

        while not self._at(TT.EOF):
            stmt = self._parse_statement()
            if stmt is not None:
                program.body.append(stmt)
            self._skip_newlines()

        return program

    # ── Helpers ──

    def _current(self) -> Token:
        return self.tokens[self.pos]

    def _at(self, *types: TT) -> bool:
        return self._current().type in types

    def _eat(self, tt: TT) -> Token:
        """Consume a token of the expected type, or raise ParseError."""
        tok = self._current()
        if tok.type != tt:
            raise ParseError(f"expected {tt.name}, got {tok.type.name} ({tok.value!r})", tok, self.filename)
        self.pos += 1
        return tok

    def _advance(self) -> Token:
        tok = self._current()
        self.pos += 1
        return tok

    def _skip_newlines(self):
        while self._at(TT.NEWLINE):
            self._advance()

    def _skip_newlines_and_dedents(self):
        while self._at(TT.NEWLINE, TT.DEDENT):
            self._advance()

    def _expect_newline_or_eof(self):
        if not self._at(TT.NEWLINE, TT.EOF, TT.DEDENT):
            tok = self._current()
            raise ParseError(f"expected newline, got {tok.type.name} ({tok.value!r})", tok, self.filename)

    # ── Statements ──

    def _parse_statement(self) -> Optional[Node]:
        tok = self._current()

        if tok.type == TT.NEWLINE:
            self._advance()
            return None
        if tok.type == TT.DEDENT:
            return None
        if tok.type == TT.EOF:
            return None

        # Compound statements
        if tok.type == TT.ASYNC:
            return self._parse_function_def(is_async=True)
        if tok.type == TT.DEF:
            return self._parse_function_def()
        if tok.type == TT.IF:
            return self._parse_if()
        if tok.type == TT.FOR:
            return self._parse_for()
        if tok.type == TT.WHILE:
            return self._parse_while()
        if tok.type == TT.STRUCT:
            return self._parse_struct_def()
        if tok.type == TT.ENUM:
            return self._parse_enum_def()
        if tok.type == TT.MATCH:
            return self._parse_match()
        if tok.type == TT.TRY:
            return self._parse_try()
        if tok.type == TT.RETURN:
            return self._parse_return()
        if tok.type == TT.CONST:
            return self._parse_const()
        if tok.type == TT.IMPORT or tok.type == TT.FROM:
            return self._parse_import()
        if tok.type == TT.EXTERN:
            return self._parse_extern()
        if tok.type == TT.DEL:
            return self._parse_del()
        if tok.type == TT.BREAK:
            self._advance()
            self._expect_newline_or_eof()
            return BreakStatement(line=tok.line, col=tok.col)
        if tok.type == TT.CONTINUE:
            self._advance()
            self._expect_newline_or_eof()
            return ContinueStatement(line=tok.line, col=tok.col)

        # Expression statement or assignment / augmented assignment
        return self._parse_expr_or_assignment()

    def _parse_function_def(self, is_async: bool = False) -> FunctionDef:
        tok = self._eat(TT.ASYNC if is_async else TT.DEF)
        if is_async:
            self._eat(TT.DEF)
        name_tok = self._eat(TT.IDENT)
        self._eat(TT.LPAREN)

        params: list[str] = []
        if not self._at(TT.RPAREN):
            params.append(self._eat(TT.IDENT).value)
            while self._at(TT.COMMA):
                self._advance()
                params.append(self._eat(TT.IDENT).value)
        self._eat(TT.RPAREN)
        self._eat(TT.COLON)
        body = self._parse_block()
        return FunctionDef(name=name_tok.value, params=params, body=body,
                           is_async=is_async, line=tok.line, col=tok.col)

    def _parse_struct_def(self) -> StructDef:
        """Parse: struct Name:\n    field1\n    field2 = default"""
        tok = self._eat(TT.STRUCT)
        name = self._eat(TT.IDENT).value
        self._eat(TT.COLON)
        body = self._parse_block()

        # The block should contain only field declarations.
        # Each field is: IDENT ["=" expr]
        fields: list[StructField] = []
        for stmt in body:
            # Field: bare identifier (no default)
            if isinstance(stmt, ExprStatement) and isinstance(stmt.expr, Identifier):
                fields.append(StructField(name=stmt.expr.name, line=stmt.expr.line, col=stmt.expr.col))
            # Field: name = default value
            elif isinstance(stmt, Assignment) and isinstance(stmt.target, Identifier):
                fields.append(StructField(name=stmt.target.name,
                                          default_value=stmt.value,
                                          line=stmt.target.line, col=stmt.target.col))
            else:
                raise ParseError("expected 'field' or 'field = default' in struct",
                                 self._current(), self.filename)

        return StructDef(name=name, fields=fields, line=tok.line, col=tok.col)

    def _parse_enum_def(self) -> EnumDef:
        """Parse: enum Name:\n    case Variant\n    case Variant(a, b)"""
        tok = self._eat(TT.ENUM)
        name = self._eat(TT.IDENT).value
        self._eat(TT.COLON)
        self._skip_newlines()
        self._eat(TT.INDENT)
        cases: list[EnumCase] = []
        while not self._at(TT.DEDENT, TT.EOF):
            self._skip_newlines()
            if self._at(TT.DEDENT, TT.EOF):
                break
            self._eat(TT.CASE)
            case_name = self._eat(TT.IDENT).value
            params: list[str] = []
            if self._at(TT.LPAREN):
                self._advance()
                if not self._at(TT.RPAREN):
                    params.append(self._eat(TT.IDENT).value)
                    while self._at(TT.COMMA):
                        self._advance()
                        params.append(self._eat(TT.IDENT).value)
                self._eat(TT.RPAREN)
            cases.append(EnumCase(name=case_name, params=params, line=tok.line, col=tok.col))
            self._skip_newlines()
        self._eat(TT.DEDENT)
        return EnumDef(name=name, cases=cases, line=tok.line, col=tok.col)

    def _parse_match(self) -> MatchExpression:
        """Parse: match expr:\n    case Pattern:\n        body"""
        tok = self._eat(TT.MATCH)
        subject = self._parse_expression()
        self._eat(TT.COLON)
        self._skip_newlines()
        self._eat(TT.INDENT)
        arms: list[MatchCase] = []
        while not self._at(TT.DEDENT, TT.EOF):
            self._skip_newlines()
            if self._at(TT.DEDENT, TT.EOF):
                break
            self._eat(TT.CASE)
            enum_name = ""
            variant_name = ""
            params: list[str] = []
            if self._at(TT.IDENT) and self._current().value == "_":
                self._advance()
                variant_name = "_"
            else:
                variant_name = self._eat(TT.IDENT).value
                if self._at(TT.DOT):
                    # Enum.Variant pattern
                    enum_name = variant_name
                    self._advance()
                    variant_name = self._eat(TT.IDENT).value
                if self._at(TT.LPAREN):
                    self._advance()
                    if not self._at(TT.RPAREN):
                        params.append(self._eat(TT.IDENT).value)
                        while self._at(TT.COMMA):
                            self._advance()
                            params.append(self._eat(TT.IDENT).value)
                    self._eat(TT.RPAREN)
            self._eat(TT.COLON)
            body = self._parse_block()
            arms.append(MatchCase(enum_name=enum_name, variant_name=variant_name,
                                  params=params, body=body,
                                  line=tok.line, col=tok.col))
            self._skip_newlines()
        self._eat(TT.DEDENT)
        return MatchExpression(subject=subject, cases=arms, line=tok.line, col=tok.col)

    def _parse_if(self) -> IfStatement:
        tok = self._eat(TT.IF)
        branches: list[tuple[Optional[Node], list[Node]]] = []
        cond = self._parse_expression()
        self._eat(TT.COLON)
        body = self._parse_block()
        branches.append((cond, body))

        self._skip_newlines()
        while self._at(TT.ELIF):
            self._advance()
            cond = self._parse_expression()
            self._eat(TT.COLON)
            body = self._parse_block()
            branches.append((cond, body))
            self._skip_newlines()

        else_body: Optional[list[Node]] = None
        if self._at(TT.ELSE):
            self._advance()
            self._eat(TT.COLON)
            else_body = self._parse_block()

        return IfStatement(branches=branches, else_body=else_body, line=tok.line, col=tok.col)

    def _parse_for(self) -> ForLoop:
        tok = self._eat(TT.FOR)
        var = self._eat(TT.IDENT).value
        self._eat(TT.IN)
        iterable = self._parse_expression()
        self._eat(TT.COLON)
        body = self._parse_block()
        return ForLoop(var=var, iterable=iterable, body=body, line=tok.line, col=tok.col)

    def _parse_while(self) -> WhileLoop:
        tok = self._eat(TT.WHILE)
        condition = self._parse_expression()
        self._eat(TT.COLON)
        body = self._parse_block()
        return WhileLoop(condition=condition, body=body, line=tok.line, col=tok.col)

    def _parse_try(self) -> TryStatement:
        tok = self._eat(TT.TRY)
        self._eat(TT.COLON)
        body = self._parse_block()
        except_clauses: list[tuple[Optional[str], list[Node]]] = []
        finally_body: Optional[list[Node]] = None

        self._skip_newlines()
        while self._at(TT.EXCEPT):
            self._advance()
            exc_type: Optional[str] = None
            if self._at(TT.IDENT):
                exc_type = self._advance().value
            self._eat(TT.COLON)
            exc_body = self._parse_block()
            except_clauses.append((exc_type, exc_body))
            self._skip_newlines()

        if self._at(TT.FINALLY):
            self._advance()
            self._eat(TT.COLON)
            finally_body = self._parse_block()

        return TryStatement(
            body=body, except_clauses=except_clauses,
            finally_body=finally_body, line=tok.line, col=tok.col,
        )

    def _parse_return(self) -> ReturnStatement:
        tok = self._eat(TT.RETURN)
        value: Optional[Node] = None
        if not self._at(TT.NEWLINE, TT.EOF, TT.DEDENT):
            value = self._parse_expression()
        self._expect_newline_or_eof()
        return ReturnStatement(value=value, line=tok.line, col=tok.col)

    def _parse_const(self) -> ConstStatement:
        tok = self._eat(TT.CONST)
        name = self._eat(TT.IDENT).value
        self._eat(TT.ASSIGN)
        value = self._parse_expression()
        self._expect_newline_or_eof()
        return ConstStatement(name=name, value=value, line=tok.line, col=tok.col)

    def _parse_import_path(self) -> tuple[int, str]:
        """Leading dots (the relative level) plus a dotted module path.

        `.sibling` is level 1 with the path `sibling`; `..pkg.util` is level 2
        with the path `pkg.util`; `from . import util` has a level and an empty
        path. The path is assembled from IDENT tokens because this is the one
        place in the grammar where a dot is not member access.
        """
        level = 0
        while self._at(TT.DOT):
            self._advance()
            level += 1
        module = ""
        if self._at(TT.IDENT):
            module = self._eat(TT.IDENT).value
            while self._at(TT.DOT):
                self._advance()
                module = f"{module}.{self._eat(TT.IDENT).value}"
        return level, module

    def _parse_import(self) -> ImportStatement:
        # `from X import a, b` form
        if self._at(TT.FROM):
            tok = self._eat(TT.FROM)
            level, module = self._parse_import_path()
            if module == "" and level == 0:
                self._eat(TT.IDENT)  # raises: `from` needs a module path
            self._eat(TT.IMPORT)
            names: list[str] = [self._eat(TT.IDENT).value]
            while self._at(TT.COMMA):
                self._advance()
                names.append(self._eat(TT.IDENT).value)
            self._expect_newline_or_eof()
            return ImportStatement(module=module, level=level,
                                  from_import=True, names=names,
                                  line=tok.line, col=tok.col)
        # `import X`, `import X.Y` or relative `import .sibling` form
        tok = self._eat(TT.IMPORT)
        level, module = self._parse_import_path()
        if module == "" and level == 0:
            self._eat(TT.IDENT)  # raises: `import` needs a module path
        self._expect_newline_or_eof()
        return ImportStatement(module=module, level=level, line=tok.line, col=tok.col)

    def _parse_block(self) -> list[Node]:
        """Parse an INDENT-delimited block of statements."""
        self._eat(TT.INDENT)
        stmts: list[Node] = []
        self._skip_newlines()

        while not self._at(TT.DEDENT, TT.EOF):
            stmt = self._parse_statement()
            if stmt is not None:
                stmts.append(stmt)
            self._skip_newlines()

        if self._at(TT.DEDENT):
            self._advance()

        return stmts

    def _parse_expr_or_assignment(self) -> Node:
        """Parse an expression statement, assignment, or augmented assignment."""
        expr = self._parse_expression()

        # Check for assignment
        if self._at(TT.ASSIGN):
            self._advance()
            value = self._parse_expression()
            self._expect_newline_or_eof()
            return Assignment(target=expr, value=value, line=expr.line, col=expr.col)

        # Check for augmented assignment
        if self._current().type in (
            TT.PLUS_ASSIGN, TT.MINUS_ASSIGN, TT.STAR_ASSIGN, TT.SLASH_ASSIGN,
            TT.PERCENT_ASSIGN, TT.POWER_ASSIGN, TT.DOUBLE_SLASH_ASSIGN,
        ):
            tok = self._advance()
            op_map = {
                TT.PLUS_ASSIGN: "+=", TT.MINUS_ASSIGN: "-=",
                TT.STAR_ASSIGN: "*=", TT.SLASH_ASSIGN: "/=",
                TT.PERCENT_ASSIGN: "%=", TT.POWER_ASSIGN: "^=",
                TT.DOUBLE_SLASH_ASSIGN: "//=",
            }
            value = self._parse_expression()
            self._expect_newline_or_eof()
            return AugmentedAssignment(
                target=expr, op=op_map[tok.type], value=value,
                line=expr.line, col=expr.col,
            )

        # Plain expression statement
        self._expect_newline_or_eof()
        return ExprStatement(expr=expr, line=expr.line, col=expr.col)

    # ── Expressions (Pratt parser) ──

    def _parse_expression(self) -> Node:
        return self._parse_precedence(0)

    def _parse_precedence(self, min_prec: int) -> Node:
        left = self._parse_unary()

        while True:
            tok = self._current()
            op = self._token_to_op(tok)
            if op is None:
                break
            prec = PRECEDENCE.get(op, 0)
            if prec < min_prec:
                break

            # Right-associative for ^
            if op == "^":
                self._advance()
                right = self._parse_precedence(prec)  # same prec for right-assoc
            else:
                self._advance()
                right = self._parse_precedence(prec + 1)

            left = BinaryOp(op=op, left=left, right=right, line=left.line, col=left.col)

        return left

    def _token_to_op(self, tok: Token) -> Optional[str]:
        mapping = {
            TT.OR: "or", TT.XOR: "xor", TT.XNOR: "xnor", TT.AND: "and",
            TT.EQ: "==", TT.NEQ: "!=", TT.LT: "<", TT.GT: ">",
            TT.LTE: "<=", TT.GTE: ">=", TT.IN: "in",
            TT.PLUS: "+", TT.MINUS: "-",
            TT.STAR: "*", TT.SLASH: "/", TT.DOUBLE_SLASH: "//", TT.PERCENT: "%",
            TT.POWER: "^",
        }
        return mapping.get(tok.type)

    def _parse_unary(self) -> Node:
        tok = self._current()
        if tok.type == TT.AWAIT:
            self._advance()
            operand = self._parse_unary()
            return AwaitExpr(value=operand, line=tok.line, col=tok.col)
        if tok.type == TT.NOT:
            self._advance()
            operand = self._parse_unary()
            return UnaryOp(op="not", operand=operand, line=tok.line, col=tok.col)
        if tok.type == TT.MINUS:
            self._advance()
            operand = self._parse_unary()
            return UnaryOp(op="-", operand=operand, line=tok.line, col=tok.col)
        if tok.type == TT.PLUS:
            self._advance()
            operand = self._parse_unary()
            return UnaryOp(op="+", operand=operand, line=tok.line, col=tok.col)
        return self._parse_primary()

    def _parse_primary(self) -> Node:
        tok = self._current()

        # Literals
        if tok.type == TT.INT:
            self._advance()
            return IntLiteral(value=int(tok.value), line=tok.line, col=tok.col)
        if tok.type == TT.FLOAT:
            self._advance()
            return FloatLiteral(value=float(tok.value), line=tok.line, col=tok.col)
        if tok.type == TT.STRING:
            self._advance()
            return StringLiteral(value=tok.value, line=tok.line, col=tok.col)
        if tok.type == TT.TRUE:
            self._advance()
            return BoolLiteral(value=True, line=tok.line, col=tok.col)
        if tok.type == TT.FALSE:
            self._advance()
            return BoolLiteral(value=False, line=tok.line, col=tok.col)
        if tok.type == TT.NULL:
            self._advance()
            return ConstantLiteral(name="null", line=tok.line, col=tok.col)
        if tok.type == TT.IDENT and tok.value in ("pi", "e"):
            self._advance()
            return ConstantLiteral(name=tok.value, line=tok.line, col=tok.col)

        # Identifier
        if tok.type == TT.IDENT:
            self._advance()
            node: Node = Identifier(name=tok.value, line=tok.line, col=tok.col)
            # Check for call or member access or index
            node = self._parse_postfix(node)
            return node

        # Parenthesized expression or tuple
        if tok.type == TT.LPAREN:
            return self._parse_paren_or_tuple()

        # List literal
        if tok.type == TT.LBRACKET:
            return self._parse_list_literal()

        # Set or map literal (distinguished by colons)
        if tok.type == TT.LBRACE:
            return self._parse_set_or_map_literal()

        # Match expression
        if tok.type == TT.MATCH:
            return self._parse_match()

        raise ParseError(f"unexpected token {tok.type.name} ({tok.value!r})", tok, self.filename)

    def _parse_postfix(self, node: Node) -> Node:
        """Parse call, member access, and index postfix operations."""
        while True:
            if self._at(TT.LPAREN):
                # Function call
                self._advance()
                args: list[Node] = []
                if not self._at(TT.RPAREN):
                    args.append(self._parse_expression())
                    while self._at(TT.COMMA):
                        self._advance()
                        args.append(self._parse_expression())
                self._eat(TT.RPAREN)
                node = Call(func=node, args=args, line=node.line, col=node.col)
            elif self._at(TT.DOT):
                self._advance()
                attr = self._eat(TT.IDENT).value
                node = MemberAccess(obj=node, attr=attr, line=node.line, col=node.col)
            elif self._at(TT.LBRACKET):
                self._advance()
                # Check for slice syntax: [: , expr: , expr:expr , etc.
                if self._at(TT.COLON):
                    # [:...] — start is None
                    self._advance()
                    stop = None
                    step = None
                    if not self._at(TT.RBRACKET) and not self._at(TT.COLON):
                        stop = self._parse_expression()
                    if self._at(TT.COLON):
                        self._advance()
                        if not self._at(TT.RBRACKET):
                            step = self._parse_expression()
                    self._eat(TT.RBRACKET)
                    sl = Slice(start=None, stop=stop, step=step, line=node.line, col=node.col)
                    node = Index(obj=node, index_expr=sl, line=node.line, col=node.col)
                else:
                    first = self._parse_expression()
                    if self._at(TT.COLON):
                        # [start:...] — slice
                        self._advance()
                        stop = None
                        step = None
                        if not self._at(TT.RBRACKET) and not self._at(TT.COLON):
                            stop = self._parse_expression()
                        if self._at(TT.COLON):
                            self._advance()
                            if not self._at(TT.RBRACKET):
                                step = self._parse_expression()
                        self._eat(TT.RBRACKET)
                        sl = Slice(start=first, stop=stop, step=step, line=node.line, col=node.col)
                        node = Index(obj=node, index_expr=sl, line=node.line, col=node.col)
                    else:
                        self._eat(TT.RBRACKET)
                        node = Index(obj=node, index_expr=first, line=node.line, col=node.col)
            elif self._at(TT.QUESTION):
                self._advance()
                node = QuestionMark(value=node, line=node.line, col=node.col)
            else:
                break
        return node

    def _parse_paren_or_tuple(self) -> Node:
        """Parse (...) — either a parenthesized expression or a tuple."""
        tok = self._eat(TT.LPAREN)

        # Empty tuple
        if self._at(TT.RPAREN):
            self._advance()
            return TupleLiteral(elements=[], line=tok.line, col=tok.col)

        first = self._parse_expression()

        # Single element: (expr) is parenthesized, (expr,) is tuple
        if self._at(TT.RPAREN):
            self._advance()
            return first  # parenthesized expression

        # At least two elements: tuple
        elements = [first]
        self._eat(TT.COMMA)
        if not self._at(TT.RPAREN):
            elements.append(self._parse_expression())
            while self._at(TT.COMMA):
                self._advance()
                if self._at(TT.RPAREN):
                    break
                elements.append(self._parse_expression())
        self._eat(TT.RPAREN)
        return TupleLiteral(elements=elements, line=tok.line, col=tok.col)

    def _parse_list_literal(self) -> ListLiteral:
        tok = self._eat(TT.LBRACKET)
        elements: list[Node] = []
        if not self._at(TT.RBRACKET):
            elements.append(self._parse_expression())
            while self._at(TT.COMMA):
                self._advance()
                if self._at(TT.RBRACKET):
                    break
                elements.append(self._parse_expression())
        self._eat(TT.RBRACKET)
        return ListLiteral(elements=elements, line=tok.line, col=tok.col)

    def _parse_extern(self) -> ExternFuncDecl:
        """Parse: extern ret_type name(param_type param_name, ...)"""
        tok = self._eat(TT.EXTERN)
        # Return type (int, float, string, bool, void, ptr, i64, etc.)
        ret_type = self._eat(TT.IDENT).value
        # Function name
        name = self._eat(TT.IDENT).value
        # Parameters
        self._eat(TT.LPAREN)
        param_types: list[str] = []
        param_names: list[str] = []
        if not self._at(TT.RPAREN):
            ptype = self._eat(TT.IDENT).value
            pname = self._eat(TT.IDENT).value
            param_types.append(ptype)
            param_names.append(pname)
            while self._at(TT.COMMA):
                self._advance()
                ptype = self._eat(TT.IDENT).value
                pname = self._eat(TT.IDENT).value
                param_types.append(ptype)
                param_names.append(pname)
        self._eat(TT.RPAREN)
        self._expect_newline_or_eof()
        return ExternFuncDecl(name=name, return_type=ret_type,
                             param_types=param_types, param_names=param_names,
                             line=tok.line, col=tok.col)

    def _parse_del(self) -> DelStatement:
        """Parse: del target"""
        tok = self._eat(TT.DEL)
        target = self._parse_primary()
        self._expect_newline_or_eof()
        return DelStatement(target=target, line=tok.line, col=tok.col)

    def _parse_set_or_map_literal(self):
        """Parse { } — set or map literal, distinguished by colons."""
        tok = self._eat(TT.LBRACE)

        # Empty set/map
        if self._at(TT.RBRACE):
            self._advance()
            # {} is an empty map (more useful than empty set)
            return MapLiteral(keys=[], values=[], line=tok.line, col=tok.col)

        first = self._parse_expression()

        # Check if this is a map (key: value) or set ({a, b})
        if self._at(TT.COLON):
            # Map literal: {key: val, ...}
            self._advance()
            first_val = self._parse_expression()
            keys = [first]
            values = [first_val]
            while self._at(TT.COMMA):
                self._advance()
                if self._at(TT.RBRACE):
                    break
                k = self._parse_expression()
                self._eat(TT.COLON)
                v = self._parse_expression()
                keys.append(k)
                values.append(v)
            self._eat(TT.RBRACE)
            return MapLiteral(keys=keys, values=values, line=tok.line, col=tok.col)
        else:
            # Set literal: {a, b, ...}
            elements = [first]
            while self._at(TT.COMMA):
                self._advance()
                if self._at(TT.RBRACE):
                    break
                elements.append(self._parse_expression())
            self._eat(TT.RBRACE)
            return SetLiteral(elements=elements, line=tok.line, col=tok.col)


# ── Convenience ──

def parse(tokens: list[Token], filename: str = "<string>") -> Program:
    return Parser(tokens, filename).parse()
