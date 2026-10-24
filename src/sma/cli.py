"""Command line entry point: `sma convert ...` and `sma reconcile ...`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .converter import convert_path
from .models import Status
from .reconcile import NormaliseOptions, reconcile
from .report import summary, to_json, to_markdown
from .rules import supported_sources


def _connect(url: str):
    """duckdb:///path.db, duckdb:///:memory:, sqlite:///path.db"""
    scheme, _, path = url.partition(":///")
    if scheme == "duckdb":
        import duckdb
        return duckdb.connect(path or ":memory:")
    if scheme == "sqlite":
        import sqlite3
        return sqlite3.connect(path)
    raise SystemExit(f"Unsupported connection URL {url!r}. Use duckdb:///file.db or sqlite:///file.db, "
                     "or call sma.reconcile.reconcile() with any DB-API connection (Snowflake, Databricks, ...).")


def cmd_convert(a: argparse.Namespace) -> int:
    src = Path(a.path)
    if not src.exists():
        raise SystemExit(f"{src} does not exist")
    out = Path(a.out)
    results = convert_path(src, out, a.source, a.target)
    out.mkdir(parents=True, exist_ok=True)
    (out / "migration_report.md").write_text(to_markdown(results, a.source, a.target), encoding="utf-8")
    (out / "migration_report.json").write_text(to_json(results), encoding="utf-8")
    s = summary(results)
    print(f"{s['statements']} statements in {s['files']} files -> {out}")
    for k, v in s["by_status"].items():
        print(f"  {k:<24}{v}")
    print(f"  automation rate         {s['automation_rate']:.0%}")
    print(f"Report: {out / 'migration_report.md'}")
    if a.strict and s["by_status"][Status.MANUAL_REVIEW.value]:
        return 1
    return 0


def cmd_reconcile(a: argparse.Namespace) -> int:
    opts = NormaliseOptions(float_digits=a.float_digits, rstrip_strings=not a.keep_trailing_spaces)
    res = reconcile(_connect(a.source_url), a.source_table, _connect(a.target_url), a.target_table or a.source_table,
                    columns=a.columns.split(",") if a.columns else None, opts=opts)
    print(res.describe())
    return 0 if res.match else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sma", description="SQL migration accelerator: convert legacy SQL and reconcile data.")
    p.add_argument("--version", action="version", version=f"sma {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="convert a .sql file or a directory of .sql files")
    c.add_argument("path")
    c.add_argument("-o", "--out", default="converted")
    c.add_argument("-s", "--source", default="teradata", help=f"source dialect (rules: {', '.join(supported_sources())})")
    c.add_argument("-t", "--target", default="snowflake", help="target dialect, e.g. snowflake, databricks, bigquery")
    c.add_argument("--strict", action="store_true", help="exit 1 if any statement needs manual review (for CI)")
    c.set_defaults(func=cmd_convert)

    r = sub.add_parser("reconcile", help="compare a source table with its migrated copy")
    r.add_argument("--source-url", required=True)
    r.add_argument("--source-table", required=True)
    r.add_argument("--target-url", required=True)
    r.add_argument("--target-table")
    r.add_argument("--columns", help="comma-separated column list (default: shared columns)")
    r.add_argument("--float-digits", type=int, default=6)
    r.add_argument("--keep-trailing-spaces", action="store_true")
    r.set_defaults(func=cmd_reconcile)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
