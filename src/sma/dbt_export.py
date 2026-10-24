"""Turn converted CREATE TABLE/VIEW ... AS SELECT statements into a dbt project skeleton.

    CTAS / CREATE VIEW AS  -> models/<name>.sql with config(materialized=...)
    references between them -> {{ ref('name') }};   anything else -> {{ source('schema', 'table') }} + sources.yml
    VOLATILE / TEMPORARY    -> materialized='ephemeral'
    Teradata UNIQUE PRIMARY INDEX -> unique + not_null tests in schema.yml (the target will not enforce it)
DML and plain DDL are left in the converted scripts; the summary says how many.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from .models import FileResult, Status


@dataclass
class Model:
    name: str
    materialized: str
    query: exp.Query
    origin: str  # file:line
    unique_key: list[str] = field(default_factory=list)


@dataclass
class DbtExport:
    models: list[Model]
    sources: dict[str, set[str]]
    skipped: list[str]  # statements that stay as scripts, with a reason


def _unique_key(source_sql: str, source: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(source_sql, read=source)
    except Exception:  # noqa: BLE001 - best effort; the source may need text rules first
        return []
    for idx in (tree.args.get("indexes") or []) if isinstance(tree, exp.Create) else []:
        params = idx.args.get("params")
        if idx.args.get("unique") and params:
            return [(c.this if isinstance(c, exp.Ordered) else c).name.lower() for c in params.args.get("columns") or []]
    return []


def collect(results: list[FileResult], source: str, target: str) -> DbtExport:
    models: dict[str, Model] = {}
    skipped: list[str] = []
    for f in results:
        for st in f.statements:
            where = f"{f.path}:{st.line}"
            if st.status not in (Status.CONVERTED, Status.CONVERTED_WITH_WARNINGS) or not st.target_sql:
                continue
            tree = sqlglot.parse_one(st.target_sql, read=target)
            query = tree.expression.this if isinstance(tree, exp.Create) and isinstance(tree.expression, exp.Subquery) \
                else (tree.expression if isinstance(tree, exp.Create) else None)
            kind = (tree.args.get("kind") or "").upper() if isinstance(tree, exp.Create) else ""
            if not isinstance(query, exp.Query) or kind not in ("TABLE", "VIEW"):
                skipped.append(f"{where} {type(tree).__name__.upper()} stays a script")
                continue
            limit = query.args.get("limit")
            if limit is not None and limit.expression is not None and limit.expression.sql() == "0":
                skipped.append(f"{where} structure-only CTAS (LIMIT 0) stays a script")
                continue
            props = tree.args.get("properties")
            temporary = bool(props and any(isinstance(p, exp.TemporaryProperty) for p in props.expressions))
            name = tree.this.name.lower()
            models[name] = Model(name, "view" if kind == "VIEW" else "ephemeral" if temporary else "table",
                                 query, where, _unique_key(st.source_sql, source))
    sources: dict[str, set[str]] = {}
    for m in models.values():
        for t in m.query.find_all(exp.Table):
            if t.name.lower() not in models and t.name:
                sources.setdefault((t.db or "default").lower(), set()).add(t.name.lower())
    return DbtExport(list(models.values()), sources, skipped)


def render_model(m: Model, models: set[str], target: str) -> str:
    query = m.query.copy()
    placeholders: dict[str, str] = {}
    for i, t in enumerate(list(query.find_all(exp.Table))):
        if not t.name:
            continue
        key = f"__sma_rel_{i}__"
        if t.name.lower() in models:
            placeholders[key] = f"{{{{ ref('{t.name.lower()}') }}}}"
        else:
            placeholders[key] = f"{{{{ source('{(t.db or 'default').lower()}', '{t.name.lower()}') }}}}"
        alias = t.args.get("alias")
        t.replace(exp.Table(this=exp.to_identifier(key), alias=alias.copy() if alias else None))
    sql = query.sql(dialect=target, pretty=True)
    for key, jinja in placeholders.items():
        sql = sql.replace(key, jinja)
    return f"{{{{ config(materialized='{m.materialized}') }}}}\n\n-- migrated from {m.origin}\n{sql}\n"


def write(export: DbtExport, out_dir: Path, target: str, project: str = "migrated") -> None:
    models_dir = out_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    if not (out_dir / "dbt_project.yml").exists():
        (out_dir / "dbt_project.yml").write_text(
            f'name: {project}\nversion: "1.0.0"\nprofile: {project}\nmodel-paths: ["models"]\n', encoding="utf-8")
    names = {m.name for m in export.models}
    for m in export.models:
        (models_dir / f"{m.name}.sql").write_text(render_model(m, names, target), encoding="utf-8")
    lines = ["version: 2", "", "models:"]
    for m in export.models:
        lines.append(f"  - name: {m.name}")
        if m.unique_key:
            lines.append("    description: Unique primary index on the source; enforced here by tests.")
            lines.append("    columns:")
            for c in m.unique_key:
                lines += [f"      - name: {c}", "        data_tests: [unique, not_null]"]
    (models_dir / "schema.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    src = ["version: 2", "", "sources:"]
    for schema in sorted(export.sources):
        src += [f"  - name: {schema}", f"    schema: {schema}", "    tables:"]
        src += [f"      - name: {t}" for t in sorted(export.sources[schema])]
    (models_dir / "sources.yml").write_text("\n".join(src) + "\n", encoding="utf-8")
