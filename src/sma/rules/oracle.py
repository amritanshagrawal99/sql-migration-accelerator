"""Oracle-specific rules: the constructs sqlglot parses from Oracle but emits invalid or unsafe target SQL for."""
from __future__ import annotations

import re

from sqlglot import exp
from sqlglot.transforms import eliminate_join_marks

from ..models import Issue, Severity
from . import TextResult

# Targets that accept these Oracle-isms natively.
_JOIN_MARK_OK = {"snowflake", "oracle"}
_CONNECT_BY_OK = {"snowflake", "oracle"}

_MERGE_DELETE = re.compile(r"^\s*MERGE\b.*\bWHEN\s+MATCHED\s+THEN\s+UPDATE\b.*\bDELETE\s+WHERE\b", re.I | re.S)


def text_rules(sql: str) -> TextResult:
    if _MERGE_DELETE.match(sql):
        # sqlglot cannot parse it, and the semantics (delete is evaluated on the *updated* row) need a human.
        return TextResult(sql, [Issue("OR003", Severity.ERROR,
                                      "MERGE ... UPDATE ... DELETE WHERE has no direct equivalent: Oracle evaluates DELETE WHERE "
                                      "on the updated row. Rewrite as WHEN MATCHED AND <cond> THEN DELETE before the UPDATE "
                                      "clause, using the post-update expression in <cond>.")], manual=True)
    return TextResult(sql, [])


def ast_rules(tree: exp.Expression, target: str) -> list[Issue]:
    issues: list[Issue] = []
    if isinstance(tree, exp.Create):
        issues += _storage_props(tree)
    issues += _rownum(tree)
    issues += _join_marks(tree, target)
    if target not in _CONNECT_BY_OK and tree.find(exp.Connect):
        issues.append(Issue("OR005", Severity.ERROR,
                            f"CONNECT BY hierarchical query is not supported on {target}; rewrite as a recursive CTE."))
    return issues


def _storage_props(tree: exp.Create) -> list[Issue]:
    props = tree.args.get("properties")
    if not props:
        return []
    physical = (exp.TableSpaceProperty, exp.PhysicalProperty)
    removed = [p for p in props.expressions if isinstance(p, physical)]
    if not removed:
        return []
    keep = [p for p in props.expressions if not isinstance(p, physical)]
    tree.set("properties", exp.Properties(expressions=keep) if keep else None)
    names = ", ".join(p.sql("oracle") for p in removed)
    return [Issue("OR004", Severity.INFO, f"Removed Oracle storage options ({names}); the target manages storage itself.")]


def _rownum_cap(pred: exp.Expression) -> int | None:
    """`ROWNUM <= n` -> n, `ROWNUM < n` -> n - 1, `ROWNUM = 1` -> 1 (also with the operands flipped)."""
    def is_rownum(e):
        return isinstance(e, exp.Column) and not e.table and e.name.upper() == "ROWNUM"

    if not isinstance(pred, (exp.LTE, exp.LT, exp.EQ, exp.GTE, exp.GT)):
        return None
    left, right = pred.this, pred.expression
    op = type(pred)
    if is_rownum(right) and not is_rownum(left):  # 10 >= ROWNUM
        left, right = right, left
        op = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.EQ: exp.EQ, exp.LTE: exp.GTE, exp.LT: exp.GT}[op]
    if not is_rownum(left) or not (isinstance(right, exp.Literal) and right.is_int):
        return None
    n = int(right.this)
    if op is exp.LTE:
        return n
    if op is exp.LT:
        return n - 1
    if op is exp.EQ and n == 1:
        return 1
    return None


def _rownum(tree: exp.Expression) -> list[Issue]:
    issues: list[Issue] = []
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        if where is None:
            continue
        conjuncts = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
        caps = [(c, _rownum_cap(c)) for c in conjuncts]
        hits = [(c, n) for c, n in caps if n is not None]
        if not hits:
            continue
        cap = min(n for _, n in hits)
        rest = [c for c in conjuncts if all(c is not h for h, _ in hits)]
        select.set("where", exp.Where(this=exp.and_(*rest)) if rest else None)
        select.limit(cap, copy=False)
        if select.args.get("order") is not None:
            issues.append(Issue("OR001", Severity.WARNING,
                                f"ROWNUM <= {cap} converted to LIMIT {cap}. Oracle applies ROWNUM before ORDER BY (it returns "
                                f"{cap} arbitrary rows, then sorts them); LIMIT applies after ORDER BY. Results will differ "
                                "unless the original was wrapped in an ordered subquery."))
        else:
            issues.append(Issue("OR001", Severity.INFO, f"ROWNUM filter converted to LIMIT {cap}."))
    leftover = [c for c in tree.find_all(exp.Column) if not c.table and c.name.upper() == "ROWNUM"]
    if leftover:
        issues.append(Issue("OR006", Severity.ERROR,
                            "ROWNUM used outside a simple top-N filter; rewrite with ROW_NUMBER() OVER (...)."))
    return issues


def _join_marks(tree: exp.Expression, target: str) -> list[Issue]:
    if target in _JOIN_MARK_OK or not any(c.args.get("join_mark") for c in tree.find_all(exp.Column)):
        return []
    try:
        new = eliminate_join_marks(tree)
    except Exception as e:  # noqa: BLE001 - sqlglot raises on shapes it can't rewrite
        return [Issue("OR002", Severity.ERROR, f"Oracle (+) outer join could not be rewritten automatically: {e}")]
    if new is not tree:
        tree.replace(new)
    return [Issue("OR002", Severity.INFO, "Oracle (+) outer join rewritten as ANSI LEFT JOIN.")]
