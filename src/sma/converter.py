"""Statement-by-statement conversion with a per-statement audit trail."""
from __future__ import annotations

from pathlib import Path

import sqlglot
from sqlglot.errors import ErrorLevel, ParseError, UnsupportedError

from .models import FileResult, Issue, Severity, StatementResult, Status
from .rules import rules_for
from .splitter import split_statements


def convert_statement(sql: str, source: str, target: str, index: int = 0, line: int = 1) -> StatementResult:
    text_rule, ast_rule = rules_for(source)
    issues: list[Issue] = []
    read = source

    if text_rule:
        tr = text_rule(sql)
        issues += tr.issues
        if tr.drop:
            return StatementResult(index, line, sql, None, Status.DROPPED, issues)
        if tr.manual:
            return StatementResult(index, line, sql, None, Status.MANUAL_REVIEW, issues)
        if tr.parse_with_target:
            read = target
        work = tr.sql
    else:
        work = sql

    try:
        tree = sqlglot.parse_one(work, read=read)
    except ParseError as e:
        issues.append(Issue("SMA001", Severity.ERROR, f"Could not parse statement: {_first_line(e)}"))
        return StatementResult(index, line, sql, None, Status.MANUAL_REVIEW, issues)

    if isinstance(tree, sqlglot.exp.Command):
        issues.append(Issue("SMA002", Severity.ERROR,
                            f"Unsupported {source} command '{tree.name}'; it was passed through unparsed."))
        return StatementResult(index, line, sql, None, Status.MANUAL_REVIEW, issues)

    if ast_rule:
        issues += ast_rule(tree, target)

    try:
        out = tree.sql(dialect=target, unsupported_level=ErrorLevel.RAISE, pretty=True)
    except UnsupportedError as e:
        out = tree.sql(dialect=target, unsupported_level=ErrorLevel.IGNORE, pretty=True)
        issues.append(Issue("SMA003", Severity.ERROR, f"Target does not support: {_first_line(e)}"))
        return StatementResult(index, line, sql, out, Status.MANUAL_REVIEW, issues)

    if any(i.severity is Severity.ERROR for i in issues):
        status = Status.MANUAL_REVIEW
    elif any(i.severity is Severity.WARNING for i in issues):
        status = Status.CONVERTED_WITH_WARNINGS
    else:
        status = Status.CONVERTED
    return StatementResult(index, line, sql, out, status, issues)


def convert_script(text: str, source: str, target: str, path: str = "<string>") -> FileResult:
    stmts = split_statements(text, source)
    return FileResult(path, [convert_statement(s.sql, source, target, i + 1, s.line) for i, s in enumerate(stmts)])


def render_script(result: FileResult, target: str) -> str:
    """Converted script with inline comments for anything dropped or needing review."""
    parts = [f"-- Converted by sql-migration-accelerator from {Path(result.path).name} (target: {target})"]
    for st in result.statements:
        notes = [f"-- [{i.code} {i.severity.value}] {i.message}" for i in st.issues if i.severity is not Severity.INFO]
        if st.status is Status.DROPPED:
            parts.append(f"-- [dropped, line {st.line}] " + " ".join(st.source_sql.split()))
            continue
        if st.status is Status.MANUAL_REVIEW:
            notes.insert(0, f"-- TODO(sma): manual review required (source line {st.line})")
            body = st.target_sql or "\n".join("-- " + ln for ln in st.source_sql.splitlines())
            parts.append("\n".join([*notes, body + (";" if st.target_sql else "")]))
            continue
        parts.append("\n".join([*notes, st.target_sql + ";"]))
    return "\n\n".join(parts) + "\n"


def convert_path(src: Path, out_dir: Path, source: str, target: str) -> list[FileResult]:
    files = sorted(src.rglob("*.sql")) if src.is_dir() else [src]
    results = []
    for f in files:
        rel = f.relative_to(src) if src.is_dir() else Path(f.name)
        res = convert_script(f.read_text(encoding="utf-8"), source, target, rel.as_posix())
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_script(res, target), encoding="utf-8")
        results.append(res)
    return results


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
