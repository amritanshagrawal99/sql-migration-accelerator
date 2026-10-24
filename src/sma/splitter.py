"""Split a SQL script into statements without breaking on semicolons inside strings or comments."""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot.tokens import TokenType

_GO_LINE = re.compile(r"^([ 	]*)(GO)[ 	]*$", re.I | re.M)


@dataclass
class RawStatement:
    sql: str
    line: int  # 1-based line where the statement starts


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def split_statements(text: str, dialect: str) -> list[RawStatement]:
    """Split on top-level semicolons using the dialect tokenizer.

    Falls back to a naive split if the script cannot be tokenized (e.g. stray
    unterminated quotes), so one bad statement never hides the rest of a file.
    """
    if dialect.lower() == "tsql":
        # GO on its own line is a client-side batch separator, not SQL: treat it as a statement boundary.
        # Same length replacement keeps every offset (and so every line number) unchanged.
        text = _GO_LINE.sub(lambda m: m.group(1) + ";" + " " * (len(m.group(2)) - 1), text)
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(text)
        # Only top-level semicolons end a statement: a Teradata macro body is a parenthesised list of statements.
        cuts, depth = [], 0
        for t in tokens:
            if t.token_type == TokenType.L_PAREN:
                depth += 1
            elif t.token_type == TokenType.R_PAREN:
                depth = max(0, depth - 1)
            elif t.token_type == TokenType.SEMICOLON and depth == 0:
                cuts.append(t.start)
        if depth:
            # Unbalanced parentheses (a broken statement): don't let it swallow the rest of the file.
            cuts = [t.start for t in tokens if t.token_type == TokenType.SEMICOLON]
    except Exception:
        cuts = [i for i, ch in enumerate(text) if ch == ";"]

    out: list[RawStatement] = []
    start = 0
    for cut in [*cuts, len(text)]:
        chunk = text[start:cut]
        skip = _leading_comments_len(chunk)
        body = chunk[skip:].strip()
        if body:
            offset = start + skip + (len(chunk[skip:]) - len(chunk[skip:].lstrip()))
            out.append(RawStatement(body, _line_of(text, offset)))
        start = cut + 1
    return out


def _leading_comments_len(chunk: str) -> int:
    """Length of whitespace and -- / /* */ comments before the first real token."""
    i, n = 0, len(chunk)
    while i < n:
        if chunk[i].isspace():
            i += 1
        elif chunk.startswith("--", i):
            nl = chunk.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif chunk.startswith("/*", i):
            end = chunk.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            break
    return i
