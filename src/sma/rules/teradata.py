"""Teradata-specific rules that close gaps in sqlglot's Teradata -> cloud warehouse transpilation.

Two kinds of rule:
  * text rules run before parsing, for syntax sqlglot cannot parse or that is meaningless on the target;
  * AST rules run on the parsed tree, for constructs sqlglot parses but would emit invalid target SQL for.
Every rule records an Issue so the report explains exactly what was changed and why.
"""
from __future__ import annotations

import re

from sqlglot import exp

from ..macros import convert_exec, convert_macro
from ..models import Issue, Severity
from . import TextResult

# ---------------------------------------------------------------- text rules

_LOCKING = re.compile(
    r"^\s*LOCKING\s+(?:ROW|(?:TABLE|DATABASE|VIEW)\s+[\w.\"]+)\s+FOR\s+(?:ACCESS|READ|WRITE|EXCLUSIVE)(?:\s+MODE)?\s+",
    re.I,
)
_NO_OP = re.compile(r"^\s*(?:COLLECT\s+(?:STATISTICS|STATS|STAT)|HELP\s+|DIAGNOSTIC\s+|BT\s*$|ET\s*$)", re.I)
_UPDATE_FROM = re.compile(
    r"^\s*UPD(?:ATE)?\s+(?P<target>[\w.\"]+)\s+FROM\s+(?P<sources>.+?)\s+SET\s+(?P<sets>.+?)(?P<where>\s+WHERE\s+.+)?$",
    re.I | re.S,
)


def text_rules(sql: str, target: str = "snowflake") -> TextResult:
    issues: list[Issue] = []

    macro = convert_macro(sql, target)
    if macro is not None:
        proc, macro_issues = macro
        if proc is None:
            return TextResult(sql, macro_issues, manual=True)
        return TextResult(sql, macro_issues, final_sql=proc)
    call = convert_exec(sql)
    if call is not None:
        if target != "snowflake":
            return TextResult(sql, [Issue("TD021", Severity.ERROR, f"EXEC of a Teradata macro: port the macro for {target} first.")],
                              manual=True)
        # sqlglot only parses CALL as an opaque command, and it is valid Snowflake as written.
        return TextResult(sql, [Issue("TD019", Severity.INFO, "EXEC macro rewritten as CALL of the converted procedure.")],
                          final_sql=call)

    m = _LOCKING.match(sql)
    if m:
        sql = sql[m.end():]
        issues.append(Issue("TD001", Severity.INFO,
                            "Removed LOCKING request modifier; the target uses MVCC, so dirty-read locking hints are unnecessary."))

    if _NO_OP.match(sql):
        issues.append(Issue("TD002", Severity.INFO,
                            "Statement has no equivalent on the target (statistics, session or transaction control) and was dropped."))
        return TextResult(sql, issues, drop=True)

    m = _UPDATE_FROM.match(sql)
    if m:
        sql = _rewrite_update_from(m)
        issues.append(Issue("TD003", Severity.INFO,
                            "Rewrote Teradata UPDATE ... FROM ... SET into UPDATE ... SET ... FROM form."))
        return TextResult(sql, issues, parse_with_target=True)

    return TextResult(sql, issues)


def _split_top_level(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _rewrite_update_from(m: re.Match) -> str:
    target = m.group("target")
    sources = _split_top_level(m.group("sources"))
    # Teradata often lists the target in FROM with an alias: UPDATE t FROM tbl t, src s SET ...
    # The target table then moves to the UPDATE clause and only the other sources stay in FROM.
    target_clause = target
    rest = []
    for src in sources:
        words = src.split()
        alias = words[-1] if len(words) > 1 else None
        if alias and alias.lower() == target.lower() and target_clause == target:
            target_clause = src
        else:
            rest.append(src)
    out = f"UPDATE {target_clause} SET {m.group('sets').strip()}"
    if rest:
        out += f" FROM {', '.join(rest)}"
    if m.group("where"):
        out += m.group("where")
    return out


# ----------------------------------------------------------------- AST rules

_PHYSICAL_PROPS = tuple(
    getattr(exp, n) for n in (
        "FallbackProperty", "JournalProperty", "ChecksumProperty", "FreespaceProperty",
        "MergeBlockRatioProperty", "DataBlocksizeProperty", "BlockCompressionProperty",
        "IsolatedLoadingProperty", "LogProperty",
    ) if hasattr(exp, n)
)
_DATE_TYPES = {exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIME,
               exp.DataType.Type.DATETIME, exp.DataType.Type.TIMESTAMPTZ}


def ast_rules(tree: exp.Expression, target: str) -> list[Issue]:
    issues: list[Issue] = []
    if isinstance(tree, exp.Create):
        issues += _create_table(tree)
    issues += _index_function(tree)
    issues += _cast_format(tree)
    issues += _sample(tree)
    return issues


def _create_table(tree: exp.Create) -> list[Issue]:
    issues: list[Issue] = []
    issues += _copy_table(tree)
    props = tree.args.get("properties")
    if props:
        keep = []
        for p in props.expressions:
            if isinstance(p, exp.SetProperty):
                if p.args.get("multi"):
                    issues.append(Issue("TD004", Severity.INFO, "Removed MULTISET; target tables allow duplicate rows by default."))
                else:
                    issues.append(Issue("TD005", Severity.WARNING,
                                        "Removed SET table option. Teradata SET tables silently reject duplicate rows; the target "
                                        "will keep them. Add a dedup step or a uniqueness test if loads rely on this."))
            elif isinstance(p, exp.StabilityProperty) and p.name.upper() == "VOLATILE":
                keep.append(exp.TemporaryProperty())
                issues.append(Issue("TD006", Severity.INFO, "VOLATILE table converted to TEMPORARY (session-scoped on both platforms)."))
            elif isinstance(p, exp.OnCommitProperty):
                issues.append(Issue("TD007", Severity.INFO, "Removed ON COMMIT PRESERVE ROWS; temporary tables persist for the session."))
            elif isinstance(p, exp.WithDataProperty):
                if p.args.get("no"):
                    _make_empty_ctas(tree)
                    issues.append(Issue("TD008", Severity.INFO, "WITH NO DATA converted to a zero-row CTAS (LIMIT 0)."))
            elif _PHYSICAL_PROPS and isinstance(p, _PHYSICAL_PROPS):
                issues.append(Issue("TD009", Severity.INFO, f"Removed Teradata physical storage option {p.sql('teradata')!r}."))
            else:
                keep.append(p)
        if keep:
            props.set("expressions", keep)
        else:
            tree.set("properties", None)

    indexes = tree.args.get("indexes") or []
    if indexes:
        cols = []
        for idx in indexes:
            params = idx.args.get("params")
            if params:
                cols += [c.this.sql() if isinstance(c, exp.Ordered) else c.sql() for c in params.args.get("columns") or []]
            if idx.args.get("unique"):
                issues.append(Issue("TD010", Severity.WARNING,
                                    "UNIQUE PRIMARY INDEX removed: the target does not enforce uniqueness. Add a uniqueness test."))
        tree.set("indexes", None)
        issues.append(Issue("TD011", Severity.INFO,
                            f"Removed PRIMARY INDEX ({', '.join(cols)}). Consider it as a clustering key for large tables."))
    return issues


def _copy_table(tree: exp.Create) -> list[Issue]:
    """CREATE TABLE a AS b WITH [NO] DATA copies another table, not a query."""
    if not isinstance(tree.expression, exp.Table):
        return []
    src = tree.expression
    props = tree.args.get("properties")
    wd = next((p for p in props.expressions if isinstance(p, exp.WithDataProperty)), None) if props else None
    others = [p for p in props.expressions if p is not wd] if props else []
    tree.set("expression", None)
    if wd is not None and wd.args.get("no"):
        tree.set("properties", exp.Properties(expressions=[*others, exp.LikeProperty(this=src)]))
        return [Issue("TD016", Severity.INFO, f"Table copy WITH NO DATA converted to CREATE TABLE ... LIKE {src.sql()}.")]
    tree.set("properties", exp.Properties(expressions=others) if others else None)
    tree.set("expression", exp.select("*").from_(src))
    return [Issue("TD017", Severity.INFO, f"Table copy WITH DATA converted to CREATE TABLE ... AS SELECT * FROM {src.sql()}.")]


def _make_empty_ctas(tree: exp.Create) -> None:
    query = tree.expression
    if isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.Query):
        tree.set("expression", query.limit(0))


def _index_function(tree: exp.Expression) -> list[Issue]:
    found = False
    for fn in list(tree.find_all(exp.Anonymous)):
        if fn.name.upper() == "INDEX" and len(fn.expressions) == 2:
            haystack, needle = fn.expressions
            fn.replace(exp.StrPosition(this=haystack, substr=needle))
            found = True
    return [Issue("TD012", Severity.INFO, "INDEX(str, sub) converted to the target's position function.")] if found else []


def _cast_format(tree: exp.Expression) -> list[Issue]:
    issues = []
    for cast in tree.find_all(exp.Cast):
        fmt = cast.args.get("format")
        to = cast.args.get("to")
        if fmt is not None and to is not None and to.this not in _DATE_TYPES:
            cast.set("format", None)
            issues.append(Issue("TD013", Severity.WARNING,
                                f"Removed display FORMAT {fmt.sql()} from CAST to {to.sql()}; apply formatting in the BI layer if needed."))
    return issues


def _sample(tree: exp.Expression) -> list[Issue]:
    issues = []
    for sel in tree.find_all(exp.Select):
        sample = sel.args.get("sample")
        if sample is not None and sample.args.get("size") is not None:
            # Teradata SAMPLE n returns n random rows of the final result; it is not a table sample.
            n = sample.args["size"]
            sel.set("sample", None)
            if sel.args.get("order") is None:
                sel.order_by(exp.func("RANDOM"), copy=False)
            if sel.args.get("limit") is None:
                sel.limit(n.copy(), copy=False)
            issues.append(Issue("TD014", Severity.WARNING,
                                f"Result-level SAMPLE {n.sql()} converted to ORDER BY RANDOM() LIMIT {n.sql()}; "
                                "rows are random, so outputs will not match the source run."))
        elif sample is not None and sample.args.get("percent") is not None:
            # Fractional SAMPLE parsed at query level; the target only samples tables.
            frm = sel.args.get("from_")
            if frm is not None and isinstance(frm.this, exp.Table) and not sel.args.get("joins"):
                sel.set("sample", None)
                frm.this.set("sample", sample)
                issues.append(Issue("TD015", Severity.WARNING,
                                    "Teradata samples after filtering; the target TABLESAMPLE samples before WHERE/GROUP BY. "
                                    "Check the sample size is still what you need."))
            else:
                issues.append(Issue("TD018", Severity.ERROR,
                                    "Fractional SAMPLE over a join or subquery has no direct target equivalent; rewrite manually."))
        elif any(t.args.get("sample") is not None for t in sel.find_all(exp.Table)) and (
                sel.args.get("where") or sel.args.get("group")):
            issues.append(Issue("TD015", Severity.WARNING,
                                "Teradata samples after filtering; the target TABLESAMPLE samples before WHERE/GROUP BY. "
                                "Check the sample size is still what you need."))
    return issues
