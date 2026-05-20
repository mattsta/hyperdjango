"""
HyperGuard .guard policy file parser — recursive descent, no regex.

Parses .guard files into PolicyAST structures that can be:
1. Validated at startup (undefined resources, circular refs, syntax errors)
2. Compiled to Zig bytecode for simple conditions
3. Flagged as needs_python for relation checks

Grammar (simplified):
    policy_file := resource_block*
    resource_block := "resource" IDENT "{" rule* "}"
    rule := ("allow" | "deny") IDENT "where" "{" condition+ "}"
    condition := field_condition | relation_condition | or_condition
    field_condition := dotted_name OP value
    relation_condition := "user" "is" IDENT "of" "resource"
    or_condition := "OR" condition
    dotted_name := IDENT "." IDENT
    OP := "=" | "!=" | ">" | ">=" | "<" | "<="
    value := "true" | "false" | INTEGER | QUOTED_STRING

Example:
    resource Forum {
        allow read where {
            resource.is_public = true
        }
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
        }
    }
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class TokenKind(Enum):
    """Token types produced by the lexer."""

    IDENT = "ident"
    INT = "int"
    STRING = "string"
    DOT = "dot"
    LBRACE = "lbrace"
    RBRACE = "rbrace"
    EQ = "eq"  # =
    NE = "ne"  # !=
    GT = "gt"  # >
    GE = "ge"  # >=
    LT = "lt"  # <
    LE = "le"  # <=
    EOF = "eof"


@dataclass(frozen=True)
class Token:
    """Single lexer token with position information."""

    kind: TokenKind
    value: str
    line: int
    col: int


class ParseError(Exception):
    """Raised when a .guard file has a syntax error."""

    def __init__(self, message: str, line: int, col: int):
        self.line = line
        self.col = col
        super().__init__(f"Line {line}, col {col}: {message}")


# ── AST nodes ────────────────────────────────────────────────────────────────


class RuleEffect(Enum):
    ALLOW = "allow"
    DENY = "deny"


class ConditionOp(Enum):
    EQ = "="
    NE = "!="
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="


@dataclass(frozen=True)
class FieldConditionAST:
    """resource.field op value OR user.field op value."""

    source: str  # "resource" or "user"
    field: str
    op: ConditionOp
    value: bool | int | str  # Literal value


@dataclass(frozen=True)
class CrossFieldConditionAST:
    """resource.field op user.field."""

    left_source: str  # "resource" or "user"
    left_field: str
    op: ConditionOp
    right_source: str  # "resource" or "user"
    right_field: str


@dataclass(frozen=True)
class RelationConditionAST:
    """user is <role> of resource — requires DB query at runtime."""

    role: str  # "member", "mod", "admin"


@dataclass(frozen=True)
class ResourceRefConditionAST:
    """resource.<relation> allows <action> — cross-resource policy check."""

    relation: str  # "forum"
    action: str  # "read", "write_post"


@dataclass(frozen=True)
class RuleAST:
    """Single allow/deny rule with conditions."""

    effect: RuleEffect
    action: str  # "read", "write_post", "edit", etc.
    conditions: tuple[
        FieldConditionAST
        | CrossFieldConditionAST
        | RelationConditionAST
        | ResourceRefConditionAST,
        ...,
    ]
    or_indices: frozenset[int]  # Indices of conditions prefixed with OR
    line: int  # Source line for error reporting


@dataclass(frozen=True)
class ResourceAST:
    """Parsed resource block with its rules."""

    name: str  # "Forum", "Post"
    rules: tuple[RuleAST, ...]
    line: int


@dataclass(frozen=True)
class PolicyAST:
    """Complete parsed .guard policy file."""

    resources: tuple[ResourceAST, ...]
    source_path: str  # File path for error reporting


# ── Lexer ────────────────────────────────────────────────────────────────────


def _lex(source: str, path: str = "<string>") -> list[Token]:
    """Tokenize a .guard file into a flat token list."""
    tokens: list[Token] = []
    i = 0
    line = 1
    col = 1
    n = len(source)

    while i < n:
        ch = source[i]

        # Skip whitespace
        if ch in " \t\r":
            i += 1
            col += 1
            continue

        # Newline
        if ch == "\n":
            i += 1
            line += 1
            col = 1
            continue

        # Comments: # to end of line
        if ch == "#":
            while i < n and source[i] != "\n":
                i += 1
            continue

        # Braces
        if ch == "{":
            tokens.append(Token(TokenKind.LBRACE, "{", line, col))
            i += 1
            col += 1
            continue
        if ch == "}":
            tokens.append(Token(TokenKind.RBRACE, "}", line, col))
            i += 1
            col += 1
            continue

        # Dot
        if ch == ".":
            tokens.append(Token(TokenKind.DOT, ".", line, col))
            i += 1
            col += 1
            continue

        # Operators
        if ch == "!" and i + 1 < n and source[i + 1] == "=":
            tokens.append(Token(TokenKind.NE, "!=", line, col))
            i += 2
            col += 2
            continue
        if ch == ">" and i + 1 < n and source[i + 1] == "=":
            tokens.append(Token(TokenKind.GE, ">=", line, col))
            i += 2
            col += 2
            continue
        if ch == "<" and i + 1 < n and source[i + 1] == "=":
            tokens.append(Token(TokenKind.LE, "<=", line, col))
            i += 2
            col += 2
            continue
        if ch == "=":
            tokens.append(Token(TokenKind.EQ, "=", line, col))
            i += 1
            col += 1
            continue
        if ch == ">":
            tokens.append(Token(TokenKind.GT, ">", line, col))
            i += 1
            col += 1
            continue
        if ch == "<":
            tokens.append(Token(TokenKind.LT, "<", line, col))
            i += 1
            col += 1
            continue

        # Quoted strings
        if ch == '"':
            start_col = col
            i += 1
            col += 1
            buf: list[str] = []
            while i < n and source[i] != '"':
                if source[i] == "\n":
                    raise ParseError("Unterminated string", line, start_col)
                buf.append(source[i])
                i += 1
                col += 1
            if i >= n:
                raise ParseError("Unterminated string", line, start_col)
            i += 1  # skip closing "
            col += 1
            tokens.append(Token(TokenKind.STRING, "".join(buf), line, start_col))
            continue

        # Integers (including negative)
        if ch.isdigit() or (ch == "-" and i + 1 < n and source[i + 1].isdigit()):
            start_col = col
            start_i = i
            if ch == "-":
                i += 1
                col += 1
            while i < n and source[i].isdigit():
                i += 1
                col += 1
            tokens.append(Token(TokenKind.INT, source[start_i:i], line, start_col))
            continue

        # Identifiers and keywords
        if ch.isalpha() or ch == "_":
            start_col = col
            start_i = i
            while i < n and (source[i].isalnum() or source[i] == "_"):
                i += 1
                col += 1
            word = source[start_i:i]
            tokens.append(Token(TokenKind.IDENT, word, line, start_col))
            continue

        raise ParseError(f"Unexpected character: {ch!r}", line, col)

    tokens.append(Token(TokenKind.EOF, "", line, col))
    return tokens


# ── Parser ───────────────────────────────────────────────────────────────────

_TOKEN_TO_OP: dict[TokenKind, ConditionOp] = {
    TokenKind.EQ: ConditionOp.EQ,
    TokenKind.NE: ConditionOp.NE,
    TokenKind.GT: ConditionOp.GT,
    TokenKind.GE: ConditionOp.GE,
    TokenKind.LT: ConditionOp.LT,
    TokenKind.LE: ConditionOp.LE,
}


class _Parser:
    """Recursive descent parser for .guard policy files."""

    def __init__(self, tokens: list[Token], path: str):
        self._tokens = tokens
        self._pos = 0
        self._path = path

    def _peek(self) -> Token:
        return self._tokens[self._pos]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, kind: TokenKind, value: str | None = None) -> Token:
        tok = self._advance()
        if tok.kind != kind:
            raise ParseError(
                f"Expected {kind.value}, got {tok.kind.value} ({tok.value!r})",
                tok.line,
                tok.col,
            )
        if value is not None and tok.value != value:
            raise ParseError(
                f"Expected {value!r}, got {tok.value!r}", tok.line, tok.col
            )
        return tok

    def _expect_ident(self, value: str) -> Token:
        return self._expect(TokenKind.IDENT, value)

    def _at_ident(self, value: str) -> bool:
        tok = self._peek()
        return tok.kind == TokenKind.IDENT and tok.value == value

    def parse(self) -> PolicyAST:
        """Parse the complete policy file."""
        resources: list[ResourceAST] = []
        while self._peek().kind != TokenKind.EOF:
            resources.append(self._parse_resource())
        return PolicyAST(resources=tuple(resources), source_path=self._path)

    def _parse_resource(self) -> ResourceAST:
        """Parse: resource <Name> { rule* }"""
        tok = self._expect_ident("resource")
        line = tok.line
        name_tok = self._expect(TokenKind.IDENT)
        self._expect(TokenKind.LBRACE)
        rules: list[RuleAST] = []
        while self._peek().kind != TokenKind.RBRACE:
            rules.append(self._parse_rule())
        self._expect(TokenKind.RBRACE)
        return ResourceAST(name=name_tok.value, rules=tuple(rules), line=line)

    def _parse_rule(self) -> RuleAST:
        """Parse: (allow|deny) <action> where { condition+ }"""
        tok = self._advance()
        if tok.kind != TokenKind.IDENT or tok.value not in ("allow", "deny"):
            raise ParseError(
                f"Expected 'allow' or 'deny', got {tok.value!r}", tok.line, tok.col
            )
        effect = RuleEffect.ALLOW if tok.value == "allow" else RuleEffect.DENY
        rule_line = tok.line

        action_tok = self._expect(TokenKind.IDENT)
        self._expect_ident("where")
        self._expect(TokenKind.LBRACE)

        conditions: list[
            FieldConditionAST
            | CrossFieldConditionAST
            | RelationConditionAST
            | ResourceRefConditionAST
        ] = []
        or_indices: set[int] = set()

        while self._peek().kind != TokenKind.RBRACE:
            # Check for OR prefix
            if self._at_ident("OR"):
                or_tok = self._advance()  # consume OR
                # OR is an infix joiner between conditions; it is meaningless on
                # the FIRST condition (nothing precedes it). Accepting it there
                # let a rule like `OR a; b` be read as "any OR count" and slip
                # past the mixed-AND/OR validator, silently compiling to all-OR
                # (over-allow). Reject it at parse time.
                if not conditions:
                    raise ParseError(
                        "OR cannot prefix the first condition of a rule",
                        or_tok.line,
                        or_tok.col,
                    )
                or_indices.add(len(conditions))

            conditions.append(self._parse_condition())

        self._expect(TokenKind.RBRACE)
        return RuleAST(
            effect=effect,
            action=action_tok.value,
            conditions=tuple(conditions),
            or_indices=frozenset(or_indices),
            line=rule_line,
        )

    def _parse_condition(
        self,
    ) -> (
        FieldConditionAST
        | CrossFieldConditionAST
        | RelationConditionAST
        | ResourceRefConditionAST
    ):
        """Parse a single condition."""
        tok = self._peek()

        # Relation: "user is <role> of resource"
        if tok.kind == TokenKind.IDENT and tok.value == "user":
            # Safe lookahead: check if next token is "is" (relation syntax)
            next_pos = self._pos + 1
            if next_pos < len(self._tokens):
                next_tok = self._tokens[next_pos]
                if next_tok.kind == TokenKind.IDENT and next_tok.value == "is":
                    return self._parse_relation_condition()

        # Must be a dotted name: source.field
        source_tok = self._expect(TokenKind.IDENT)
        self._expect(TokenKind.DOT)
        field_tok = self._expect(TokenKind.IDENT)

        # Check for "allows" (cross-resource ref): resource.forum allows read
        if self._at_ident("allows"):
            self._advance()  # consume "allows"
            action_tok = self._expect(TokenKind.IDENT)
            return ResourceRefConditionAST(
                relation=field_tok.value, action=action_tok.value
            )

        # Operator
        op_tok = self._advance()
        op = self._tok_to_op(op_tok)

        # RHS: value or dotted name (cross-field)
        rhs_tok = self._peek()
        if rhs_tok.kind == TokenKind.IDENT and rhs_tok.value in ("true", "false"):
            self._advance()
            val = rhs_tok.value == "true"
            return FieldConditionAST(
                source=source_tok.value, field=field_tok.value, op=op, value=val
            )

        if rhs_tok.kind == TokenKind.INT:
            self._advance()
            return FieldConditionAST(
                source=source_tok.value,
                field=field_tok.value,
                op=op,
                value=int(rhs_tok.value),
            )

        if rhs_tok.kind == TokenKind.STRING:
            self._advance()
            return FieldConditionAST(
                source=source_tok.value,
                field=field_tok.value,
                op=op,
                value=rhs_tok.value,
            )

        # Cross-field: user.id or resource.field
        if rhs_tok.kind == TokenKind.IDENT:
            rhs_source = self._advance()
            self._expect(TokenKind.DOT)
            rhs_field = self._expect(TokenKind.IDENT)
            return CrossFieldConditionAST(
                left_source=source_tok.value,
                left_field=field_tok.value,
                op=op,
                right_source=rhs_source.value,
                right_field=rhs_field.value,
            )

        raise ParseError(
            f"Expected value or field reference, got {rhs_tok.value!r}",
            rhs_tok.line,
            rhs_tok.col,
        )

    def _parse_relation_condition(self) -> RelationConditionAST:
        """Parse: user is <role> of resource"""
        self._expect_ident("user")
        self._expect_ident("is")
        role_tok = self._expect(TokenKind.IDENT)
        self._expect_ident("of")
        self._expect_ident("resource")
        return RelationConditionAST(role=role_tok.value)

    def _tok_to_op(self, tok: Token) -> ConditionOp:
        if tok.kind in _TOKEN_TO_OP:
            return _TOKEN_TO_OP[tok.kind]
        raise ParseError(
            f"Expected comparison operator, got {tok.value!r}", tok.line, tok.col
        )


# ── Public API ───────────────────────────────────────────────────────────────


def parse_policy(source: str, path: str = "<string>") -> PolicyAST:
    """Parse a .guard policy file into an AST.

    Args:
        source: The .guard file contents as a string.
        path: File path for error reporting.

    Returns:
        PolicyAST with parsed resource blocks and rules.

    Raises:
        ParseError: On syntax errors with line/col information.
    """
    tokens = _lex(source, path)
    parser = _Parser(tokens, path)
    return parser.parse()


def parse_policy_file(file_path: str) -> PolicyAST:
    """Parse a .guard file from disk.

    Args:
        file_path: Path to the .guard file.

    Returns:
        PolicyAST with parsed resource blocks and rules.

    Raises:
        ParseError: On syntax errors.
        FileNotFoundError: If file doesn't exist.
    """
    with Path(file_path).open() as f:
        source = f.read()
    return parse_policy(source, path=file_path)
