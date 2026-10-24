"""Teradata macros -> Snowflake Scripting stored procedures.

    REPLACE MACRO db.m (p1 DATE, p2 VARCHAR(10) DEFAULT 'IN') AS ( stmt1; stmt2; );
becomes
    CREATE OR REPLACE PROCEDURE db.m(p1 DATE, p2 VARCHAR(10) DEFAULT 'IN')
    RETURNS VARCHAR | TABLE()   -- TABLE() when the macro ends with a SELECT, like a Teradata macro returning rows
    LANGUAGE SQL EXECUTE AS CALLER AS $$ BEGIN ... END; $$

Every body statement goes through the normal converter, so all Teradata rules apply. Teradata references macro
parameters as :name, which is also how Snowflake Scripting binds variables inside SQL, so they carry over unchanged.
"""
from __future__ import annotations

import re

from sqlglot import exp

from .models import Issue, Severity, Status

MACRO = re.compile(r"^\s*(?:CREATE|REPLACE)\s+MACRO\s+(?P<name>[\w.\"]+)\s*"
                   r"(?P<params>\((?:[^()]|\([^()]*\))*\))?\s*AS\s*\((?P<body>.*)\)\s*$", re.I | re.S)
EXEC = re.compile(r"^\s*EXEC(?:UTE)?\s+(?P<name>[\w.\"]+)\s*(?P<args>\(.*\))?\s*$", re.I | re.S)


def _split_params(text: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _param(p: str) -> str:
    m = re.match(r"^(\w+)\s+(.+?)(\s+DEFAULT\s+.+)?$", p, re.I | re.S)
    if not m:
        raise ValueError(f"cannot read macro parameter {p!r}")
    name, typ, default = m.group(1), m.group(2), (m.group(3) or "")
    return f"{name} {exp.DataType.build(typ, dialect='teradata').sql('snowflake')}{default}"


def convert_macro(sql: str, target: str) -> tuple[str | None, list[Issue]] | None:
    """None if `sql` is not a macro; otherwise (procedure SQL or None, issues)."""
    m = MACRO.match(sql)
    if not m:
        return None
    if target != "snowflake":
        return None, [Issue("TD021", Severity.ERROR,
                            f"Teradata macros convert to Snowflake Scripting only; on {target}, port the macro to a SQL function, "
                            "a job/notebook, or a stored procedure by hand.")]
    from .converter import convert_statement  # local import: converter -> rules -> macros
    from .splitter import split_statements

    try:
        params = [_param(p) for p in _split_params(m.group("params")[1:-1])] if m.group("params") else []
    except ValueError as e:
        return None, [Issue("TD021", Severity.ERROR, str(e))]

    issues: list[Issue] = []
    body: list[str] = []
    stmts = split_statements(m.group("body"), "teradata")
    results = [convert_statement(s.sql, "teradata", "snowflake", i + 1, s.line) for i, s in enumerate(stmts)]
    for r in results:
        for i in r.issues:
            issues.append(Issue(i.code, i.severity, f"macro statement {r.index}: {i.message}"))
    if any(r.status is Status.MANUAL_REVIEW for r in results):
        issues.append(Issue("TD021", Severity.ERROR, "A statement in the macro body needs manual review, so the procedure was not built."))
        return None, issues
    live = [r for r in results if r.status is not Status.DROPPED]
    if not live:
        return None, issues + [Issue("TD021", Severity.ERROR, "Macro body has no statements to convert.")]

    returns_rows = live[-1].target_sql.lstrip().upper().startswith(("SELECT", "WITH"))
    for r in live[:-1] if returns_rows else live:
        body.append(_indent(r.target_sql) + ";")
    if returns_rows:
        body.append("  LET res RESULTSET := (\n" + _indent(live[-1].target_sql, 4) + "\n  );")
        body.append("  RETURN TABLE(res);")
    else:
        body.append("  RETURN 'ok';")
    proc = (f"CREATE OR REPLACE PROCEDURE {m.group('name')}({', '.join(params)})\n"
            f"RETURNS {'TABLE()' if returns_rows else 'VARCHAR'}\nLANGUAGE SQL\nEXECUTE AS CALLER\nAS\n$$\nBEGIN\n"
            + "\n".join(body) + "\nEND;\n$$")
    issues.append(Issue("TD020", Severity.INFO,
                        f"Macro converted to a Snowflake Scripting procedure ({len(live)} statement(s), "
                        f"returns {'a result set' if returns_rows else 'a status'})."))
    return proc, issues


def convert_exec(sql: str) -> str | None:
    m = EXEC.match(sql)
    if not m:
        return None
    return f"CALL {m.group('name')}{m.group('args') or '()'}"


def _indent(sql: str, n: int = 2) -> str:
    return "\n".join(" " * n + line for line in sql.splitlines())
