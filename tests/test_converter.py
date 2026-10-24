import json
from pathlib import Path

from sma.cli import main
from sma.converter import convert_script, render_script
from sma.models import Status
from sma.report import summary, to_json, to_markdown
from sma.splitter import split_statements

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "teradata"


def test_splitter_ignores_semicolons_in_strings_and_comments():
    stmts = split_statements("SEL 'a;b' FROM t;\n\n-- a comment; with a semicolon\nDEL FROM t;\n", "teradata")
    assert [s.sql for s in stmts] == ["SEL 'a;b' FROM t", "DEL FROM t"]
    assert [s.line for s in stmts] == [1, 4]


def test_splitter_strips_leading_comments_so_text_rules_match():
    res = convert_script("/* header */\n-- note\nLOCKING ROW FOR ACCESS SEL 1;", "teradata", "snowflake")
    assert res.statements[0].line == 3
    assert "TD001" in [i.code for i in res.statements[0].issues]


def test_unparseable_statement_is_isolated():
    res = convert_script("SEL a FROM t; SEL FROM WHERE (((; SEL b FROM t;", "teradata", "snowflake")
    assert [s.status for s in res.statements] == [Status.CONVERTED, Status.MANUAL_REVIEW, Status.CONVERTED]


def test_render_marks_dropped_and_review():
    res = convert_script("COLLECT STATISTICS ON t COLUMN (a); SEL FROM WHERE (((;", "teradata", "snowflake")
    out = render_script(res, "snowflake")
    assert "-- [dropped, line 1] COLLECT STATISTICS" in out
    assert "TODO(sma): manual review required" in out
    assert "-- SEL FROM WHERE" in out  # original kept, commented


def test_examples_convert_fully():
    results = [convert_script(f.read_text(), "teradata", "snowflake", str(f)) for f in sorted(EXAMPLES.glob("*.sql"))]
    s = summary(results)
    assert s["statements"] == 13
    assert s["by_status"]["manual_review"] == 0
    md = to_markdown(results, "teradata", "snowflake")
    assert "Automation rate: **100%**" in md and "TD005" in md
    assert json.loads(to_json(results))["summary"]["files"] == 2


def test_cli_convert_writes_outputs(tmp_path):
    assert main(["convert", str(EXAMPLES), "-o", str(tmp_path)]) == 0
    assert (tmp_path / "customer_load.sql").exists()
    assert (tmp_path / "migration_report.md").exists()
    assert "CREATE TEMPORARY TABLE" in (tmp_path / "customer_load.sql").read_text()


def test_cli_strict_fails_on_manual_review(tmp_path):
    bad = tmp_path / "bad.sql"
    bad.write_text("SEL FROM WHERE (((;")
    assert main(["convert", str(bad), "-o", str(tmp_path / "out"), "--strict"]) == 1
