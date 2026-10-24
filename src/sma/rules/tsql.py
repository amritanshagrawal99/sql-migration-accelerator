"""SQL Server (T-SQL) rules: session noise, procedural statements, and DML/MERGE shapes sqlglot gets wrong."""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from ..models import Issue, Severity
from . import TextResult

_SESSION = re.compile(r"^\s*(?:GO|SET\s+(?:NOCOUNT|ANSI_NULLS|ANSI_WARNINGS|ANSI_PADDING|QUOTED_IDENTIFIER|XACT_ABORT|ARITHABORT|"
                      r"CONCAT_NULL_YIELDS_NULL)\s+(?:ON|OFF)|PRINT\b.*)\s*$", re.I | re.S)
_DROP_IF = re.compile(r"^\s*IF\s+OBJECT_ID\s*\(\s*N?'[^']+'\s*(?:,\s*N?'U')?\s*\)\s+IS\s+NOT\s+NULL\s+DROP\s+TABLE\s+(\S+?)\s*;?\s*$",
                      re.I | re.S)
_EXEC = re.compile(r"^\s*EXEC(?:UTE)?\b", re.I)
_PROCEDURAL = re.compile(r"^\s*(?:DECLARE\s+@|SET\s+@|IF\s|WHILE\s|BEGIN\s+TRY|RAISERROR|THROW\b)", re.I)
_NOLOCK = re.compile(r"WITH\s*\(\s*NOLOCK\s*\)", re.I)

_NATIVE_BY_SOURCE = {"databricks", "spark", "bigquery", "tsql", "postgres"}


def text_rules(sql: str, target: str = "snowflake") -> TextResult:
    issues: list[Issue] = []
    if _SESSION.match(sql):
        return TextResult(sql, [Issue("TS010", Severity.INFO,
                                      "Dropped batch separator / session setting / PRINT; it has no effect on the target.")], drop=True)
    m = _DROP_IF.match(sql)
    if m:
        return TextResult(f"DROP TABLE IF EXISTS {m.group(1)}",
                          [Issue("TS011", Severity.INFO, "IF OBJECT_ID(...) IS NOT NULL DROP TABLE rewritten as DROP TABLE IF EXISTS.")])
    if _EXEC.match(sql):
        return TextResult(sql, [Issue("TS005", Severity.ERROR,
                                      "Stored procedure call. Port the procedure (Snowflake Scripting / Databricks SQL scripting) "
                                      "and replace EXEC with CALL.")], manual=True)
    if _PROCEDURAL.match(sql):
        return TextResult(sql, [Issue("TS006", Severity.ERROR,
                                      "Procedural T-SQL (variables, IF/WHILE, TRY/CATCH) runs only inside a stored procedure or "
                                      "scripting block on the target; port the surrounding batch as one block.")], manual=True)
    if _NOLOCK.search(sql):
        issues.append(Issue("TS007", Severity.INFO, "Removed WITH (NOLOCK) hints; the target reads committed snapshots."))
    return TextResult(sql, issues)


def ast_rules(tree: exp.Expression, target: str):
    issues: list[Issue] = []
    if isinstance(tree, exp.Create):
        issues += _filegroup(tree)
    if isinstance(tree, exp.Merge):
        issues += _merge_by_source(tree, target)
    if any(isinstance(i, exp.Identifier) and i.args.get("temporary") for i in tree.find_all(exp.Identifier)):
        issues.append(Issue("TS008", Severity.INFO, "#temp table becomes a session-scoped temporary table without the # prefix."))
    if isinstance(tree, exp.Update):
        new, more = _update_join(tree, target)
        return issues + more, new
    if isinstance(tree, exp.Delete):
        new, more = _delete_join(tree, target)
        return issues + more, new
    return issues


def _filegroup(tree: exp.Create) -> list[Issue]:
    props = tree.args.get("properties")
    if not props or not any(isinstance(p, exp.OnProperty) for p in props.expressions):
        return []
    keep = [p for p in props.expressions if not isinstance(p, exp.OnProperty)]
    tree.set("properties", exp.Properties(expressions=keep) if keep else None)
    return [Issue("TS004", Severity.INFO, "Removed filegroup placement (ON [filegroup]).")]


def _merge_by_source(tree: exp.Merge, target: str) -> list[Issue]:
    whens = tree.args.get("whens")
    by_source = [w for w in (whens.expressions if whens else []) if w.args.get("source")]
    if not by_source or target in _NATIVE_BY_SOURCE:
        return []
    # sqlglot silently drops BY SOURCE for some targets, turning "delete rows missing from the source" into
    # "delete when no target row matches", a different (and for Snowflake invalid) statement. Remove the clause so the
    # emitted MERGE is correct as far as it goes, and say what is missing.
    kept = [w for w in whens.expressions if not w.args.get("source")]
    whens.set("expressions", kept)
    return [Issue("TS001", Severity.ERROR,
                  f"MERGE ... WHEN NOT MATCHED BY SOURCE is not supported on {target}. Remove that clause and run a "
                  "separate DELETE FROM <target> WHERE NOT EXISTS (SELECT 1 FROM <source> WHERE <merge condition>).")]


def _joined_base(frm_table: exp.Expression, target_name: str) -> tuple[exp.Table, exp.Join] | None:
    """The base table and its single INNER join, if the statement's target is that base table (by alias or name)."""
    if not isinstance(frm_table, exp.Table):
        return None
    joins = frm_table.args.get("joins") or []
    if target_name.lower() not in (frm_table.alias_or_name.lower(), frm_table.name.lower()) or not joins:
        return None
    if len(joins) != 1:
        return None
    j = joins[0]
    if j.side or (j.kind or "").upper() not in ("", "INNER") or not j.args.get("on"):
        return None
    base = frm_table.copy()
    base.set("joins", None)
    return base, j


def _update_join(tree: exp.Update, target: str):
    frm = tree.args.get("from_")
    if frm is None or not isinstance(frm.this, exp.Table) or not frm.this.args.get("joins"):
        return tree, []
    hit = _joined_base(frm.this, tree.this.name)
    if hit is None:
        return tree, [Issue("TS002", Severity.ERROR,
                            "UPDATE ... FROM with several or outer joins: rewrite by hand (UPDATE ... FROM on Snowflake, MERGE on "
                            "Databricks).")]
    base, join = hit
    where = tree.args.get("where")
    cond = exp.and_(join.args["on"], where.this) if where else join.args["on"]
    sets = []
    for s in tree.expressions:
        s = s.copy()
        if isinstance(s.this, exp.Column):
            s.this.set("table", None)  # SET targets must be bare column names
        sets.append(s.sql(target))
    src, on = join.this.sql(target), cond.sql(target)
    if target in ("databricks", "spark"):
        sql = f"MERGE INTO {base.sql(target)} USING {src} ON {on} WHEN MATCHED THEN UPDATE SET {', '.join(sets)}"
        issue = Issue("TS002", Severity.WARNING,
                      "UPDATE ... FROM ... JOIN rewritten as MERGE. MERGE fails if a target row matches several source rows; "
                      "SQL Server silently picked one. Deduplicate the source if that can happen.")
    else:
        sql = f"UPDATE {base.sql(target)} SET {', '.join(sets)} FROM {src} WHERE {on}"
        issue = Issue("TS002", Severity.INFO, "UPDATE alias FROM ... JOIN rewritten as UPDATE table SET ... FROM source WHERE ...")
    return sqlglot.parse_one(sql, read=target), [issue]


def _delete_join(tree: exp.Delete, target: str):
    tbl = tree.this
    if not isinstance(tbl, exp.Table) or not tbl.args.get("joins"):
        return tree, []
    named = (tree.args.get("tables") or [tbl])[0]
    hit = _joined_base(tbl, named.name)
    if hit is None:
        return tree, [Issue("TS003", Severity.ERROR, "DELETE ... FROM with several or outer joins: rewrite as DELETE ... WHERE EXISTS.")]
    base, join = hit
    where = tree.args.get("where")
    cond = exp.and_(join.args["on"], where.this) if where else join.args["on"]
    sql = f"DELETE FROM {base.sql(target)} WHERE EXISTS (SELECT 1 FROM {join.this.sql(target)} WHERE {cond.sql(target)})"
    issue = Issue("TS003", Severity.INFO, "DELETE alias FROM ... JOIN rewritten as DELETE ... WHERE EXISTS.")
    return sqlglot.parse_one(sql, read=target), [issue]
