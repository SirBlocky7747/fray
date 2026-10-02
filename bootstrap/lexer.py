"""
fray lexer — tokenizer with INDENT/DEDENT support.

Usage:
    from lexer import Lexer
    tokens = Lexer(source, "filename.fray").tokenize()
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator


class TT(Enum):
    """Token types."""
    # Literals
    INT = auto()
    FLOAT = auto()
    STRING = auto()
    IDENT = auto()

    # Keywords
    DEF = auto()
    RETURN = auto()
    IF = auto()
    ELIF = auto()
    ELSE = auto()
    FOR = auto()
    IN = auto()
    WHILE = auto()
    TRY = auto()
    EXCEPT = auto()
    FINALLY = auto()
    CONST = auto()
    IMPORT = auto()
    FROM = auto()
    EXTERN = auto()
    ASYNC = auto()
    AWAIT = auto()
    STRUCT = auto()
    ENUM = auto()
    CASE = auto()
    MATCH = auto()
    DEL = auto()
    BREAK = auto()
    CONTINUE = auto()
    AND = auto()
    OR = auto()
    XOR = auto()
    XNOR = auto()
    NOT = auto()
    TRUE = auto()
    FALSE = auto()
    NULL = auto()

    # Operators
    ASSIGN = auto()
    PLUS_ASSIGN = auto()
    MINUS_ASSIGN = auto()
    STAR_ASSIGN = auto()
    SLASH_ASSIGN = auto()
    PERCENT_ASSIGN = auto()
    POWER_ASSIGN = auto()
    EQ = auto()
    NEQ = auto()
    LT = auto()
    GT = auto()
    LTE = auto()
    GTE = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    DOUBLE_SLASH = auto()
    DOUBLE_SLASH_ASSIGN = auto()
    PERCENT = auto()
    POWER = auto()

    # Delimiters
    LPAREN = auto()
    RPAREN = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    LBRACE = auto()
    RBRACE = auto()
    COMMA = auto()
    COLON = auto()
    DOT = auto()
    QUESTION = auto()

    # Structural
    INDENT = auto()
    DEDENT = auto()
    NEWLINE = auto()
    EOF = auto()

    # Special
    COMMENT = auto()


KEYWORDS: dict[str, TT] = {
    "def": TT.DEF,
    "return": TT.RETURN,
    "if": TT.IF,
    "elif": TT.ELIF,
    "else": TT.ELSE,
    "for": TT.FOR,
    "in": TT.IN,
    "while": TT.WHILE,
    "try": TT.TRY,
    "except": TT.EXCEPT,
    "finally": TT.FINALLY,
    "const": TT.CONST,
    "import": TT.IMPORT,
    "from": TT.FROM,
    "extern": TT.EXTERN,
    "async": TT.ASYNC,
    "await": TT.AWAIT,
    "and": TT.AND,
    "or": TT.OR,
    "xor": TT.XOR,
    "xnor": TT.XNOR,
    "not": TT.NOT,
    "True": TT.TRUE,
    "False": TT.FALSE,
    "null": TT.NULL,
    "break": TT.BREAK,
    "continue": TT.CONTINUE,
    "struct": TT.STRUCT,
    "enum": TT.ENUM,
    "case": TT.CASE,
    "match": TT.MATCH,
    "del": TT.DEL,
}

SINGLE_CHARS = {
    "(": TT.LPAREN,
    ")": TT.RPAREN,
    "[": TT.LBRACKET,
    "]": TT.RBRACKET,
    "{": TT.LBRACE,
    "}": TT.RBRACE,
    ",": TT.COMMA,
    ":": TT.COLON,
    ".": TT.DOT,
    "?": TT.QUESTION,
}


@dataclass
class Token:
    type: TT
    value: str
    line: int
    col: int
    indent: int = 0  # for INDENT tokens

    def __repr__(self) -> str:
        if self.type == TT.EOF:
            return "Token(EOF)"
        if self.type in (TT.INDENT, TT.DEDENT, TT.NEWLINE):
            return f"Token({self.type.name})"
        return f"Token({self.type.name}, {self.value!r}, L{self.line}:{self.col})"


class LexerError(Exception):
    def __init__(self, msg: str, line: int, col: int, filename: str):
        self.line = line
        self.col = col
        self.filename = filename
        super().__init__(f"{filename}:{line}:{col}: {msg}")


class Lexer:
    def __init__(self, source: str, filename: str = "<string>"):
        self.source = source
        self.filename = filename
        self.pos = 0
        self.line = 1
        self.col = 1
        self.indent_stack: list[int] = [0]
        self.tokens: list[Token] = []
        self._pending_dedents = 0

    def tokenize(self) -> list[Token]:
        """Tokenize the entire source and return a list of tokens ending with EOF."""
        # Initial newline to trigger any indentation at start of file
        self._emit(TT.NEWLINE, "\\n", self.line, self.col)

        while self.pos < len(self.source):
            ch = self.source[self.pos]

            if ch == "#":
                self._skip_comment()
            elif ch == "\n":
                self._advance()
                self.line += 1
                self.col = 1
                self._handle_newline()
            elif ch == " " or ch == "\t":
                self._skip_whitespace()
            elif ch == '"':
                self._read_string()
            elif ch.isdigit():
                self._read_number()
            elif ch.isalpha() or ch == "_":
                self._read_identifier()
            elif ch in SINGLE_CHARS:
                self._advance()
                self._emit(SINGLE_CHARS[ch], ch, self.line, self.col - 1)
            elif ch == "=":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.EQ, "==", self.line, self.col - 2)
                else:
                    self._emit(TT.ASSIGN, "=", self.line, self.col - 1)
            elif ch == "+":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.PLUS_ASSIGN, "+=", self.line, self.col - 2)
                else:
                    self._emit(TT.PLUS, "+", self.line, self.col - 1)
            elif ch == "-":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.MINUS_ASSIGN, "-=", self.line, self.col - 2)
                else:
                    self._emit(TT.MINUS, "-", self.line, self.col - 1)
            elif ch == "*":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.STAR_ASSIGN, "*=", self.line, self.col - 2)
                else:
                    self._emit(TT.STAR, "*", self.line, self.col - 1)
            elif ch == "/":
                self._advance()
                if self._peek() == "/":
                    self._advance()
                    if self._peek() == "=":
                        self._advance()
                        self._emit(TT.DOUBLE_SLASH_ASSIGN, "//=", self.line, self.col - 3)
                    else:
                        self._emit(TT.DOUBLE_SLASH, "//", self.line, self.col - 2)
                elif self._peek() == "=":
                    self._advance()
                    self._emit(TT.SLASH_ASSIGN, "/=", self.line, self.col - 2)
                else:
                    self._emit(TT.SLASH, "/", self.line, self.col - 1)
            elif ch == "%":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.PERCENT_ASSIGN, "%=", self.line, self.col - 2)
                else:
                    self._emit(TT.PERCENT, "%", self.line, self.col - 1)
            elif ch == "^":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.POWER_ASSIGN, "^=", self.line, self.col - 2)
                else:
                    self._emit(TT.POWER, "^", self.line, self.col - 1)
            elif ch == "!":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.NEQ, "!=", self.line, self.col - 2)
                else:
                    raise LexerError(f"unexpected character '!'", self.line, self.col - 1, self.filename)
            elif ch == "<":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.LTE, "<=", self.line, self.col - 2)
                else:
                    self._emit(TT.LT, "<", self.line, self.col - 1)
            elif ch == ">":
                self._advance()
                if self._peek() == "=":
                    self._advance()
                    self._emit(TT.GTE, ">=", self.line, self.col - 2)
                else:
                    self._emit(TT.GT, ">", self.line, self.col - 1)
            else:
                raise LexerError(f"unexpected character {ch!r}", self.line, self.col, self.filename)

        # End of file: emit DEDENTs for any remaining indentation, then EOF
        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            self._emit(TT.DEDENT, "", self.line, self.col)

        self._emit(TT.EOF, "", self.line, self.col)
        return self.tokens

    # ── internal helpers ──

    def _advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        self.col += 1
        return ch

    def _peek(self, offset: int = 0) -> str | None:
        idx = self.pos + offset
        return self.source[idx] if idx < len(self.source) else None

    def _emit(self, type: TT, value: str, line: int, col: int):
        self.tokens.append(Token(type, value, line, col))

    def _skip_whitespace(self):
        while self.pos < len(self.source) and self.source[self.pos] in (" ", "\t"):
            self._advance()

    def _skip_comment(self):
        """Skip a # comment to end of line."""
        start_col = self.col
        while self.pos < len(self.source) and self.source[self.pos] != "\n":
            self._advance()
        # Don't emit comment tokens — they're discarded

    def _handle_newline(self):
        """After a newline, count indentation and emit INDENT/DEDENT as needed."""
        indent = 0

        # Skip blank lines and comment-only lines
        while self.pos < len(self.source):
            # Count indentation
            indent = 0
            while self.pos < len(self.source) and self.source[self.pos] == " ":
                indent += 1
                self.pos += 1
                self.col += 1

            # If blank line or comment line, skip entirely
            if self.pos >= len(self.source) or self.source[self.pos] in ("\n", "#"):
                if self.pos < len(self.source) and self.source[self.pos] == "\n":
                    self._advance()
                    self.line += 1
                    self.col = 1
                elif self.pos < len(self.source) and self.source[self.pos] == "#":
                    self._skip_comment()
                    if self.pos < len(self.source) and self.source[self.pos] == "\n":
                        self._advance()
                        self.line += 1
                        self.col = 1
                else:
                    break
            else:
                break

        # Now handle indent level change
        current_indent = self.indent_stack[-1]
        if indent > current_indent:
            self.indent_stack.append(indent)
            self._emit(TT.INDENT, "", self.line, self.col)
        elif indent < current_indent:
            while self.indent_stack[-1] > indent:
                self.indent_stack.pop()
                self._emit(TT.DEDENT, "", self.line, self.col)
            if self.indent_stack[-1] != indent:
                raise LexerError(
                    f"indentation does not match any outer level (expected one of {self.indent_stack})",
                    self.line, self.col, self.filename,
                )

        self._emit(TT.NEWLINE, "\\n", self.line, self.col)

    def _read_string(self):
        """Read a double-quoted string literal."""
        line, col = self.line, self.col
        self._advance()  # skip opening "
        parts = []
        while self.pos < len(self.source):
            ch = self.source[self.pos]
            if ch == '"':
                self._advance()
                self._emit(TT.STRING, "".join(parts), line, col)
                return
            elif ch == "\\":
                self._advance()
                esc = self._advance()
                escape_map = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "0": "\0"}
                parts.append(escape_map.get(esc, esc))
            elif ch == "\n":
                raise LexerError("unterminated string literal", line, col, self.filename)
            else:
                parts.append(ch)
                self._advance()
        raise LexerError("unterminated string literal", line, col, self.filename)

    def _read_number(self):
        """Read an integer or float literal."""
        line, col = self.line, self.col
        start = self.pos
        is_float = False

        while self.pos < len(self.source) and self.source[self.pos].isdigit():
            self._advance()

        if self.pos < len(self.source) and self.source[self.pos] == ".":
            # Check if this is actually `.` (member access) or float decimal point
            # If followed by a digit, it's a float
            if self.pos + 1 < len(self.source) and self.source[self.pos + 1].isdigit():
                is_float = True
                self._advance()  # skip .
                while self.pos < len(self.source) and self.source[self.pos].isdigit():
                    self._advance()

        value = self.source[start:self.pos]
        self._emit(TT.FLOAT if is_float else TT.INT, value, line, col)

    def _read_identifier(self):
        """Read an identifier or keyword."""
        line, col = self.line, self.col
        start = self.pos
        while self.pos < len(self.source) and (self.source[self.pos].isalnum() or self.source[self.pos] == "_"):
            self._advance()
        word = self.source[start:self.pos]
        tt = KEYWORDS.get(word, TT.IDENT)
        self._emit(tt, word, line, col)


# ── Convenience ──

def tokenize(source: str, filename: str = "<string>") -> list[Token]:
    return Lexer(source, filename).tokenize()
