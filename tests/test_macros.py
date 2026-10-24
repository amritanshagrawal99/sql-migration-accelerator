from pathlib import Path

from sma.converter import convert_script, convert_statement
from sma.models import Status
from sma.splitter import split_statements

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "teradata_macros" / "daily_load.sql"
LOAD = """REPLACE MACRO edw.load_day (run_dt DATE, region VARCHAR(10) DEFAULT 'IN') AS (
  DEL FROM edw.daily WHERE sale_dt = :run_dt AND region = :region;
  INSERT INTO edw.daily SEL sale_dt, SUM(amt) FROM edw.sales WHERE sale_dt = :run_dt GROUP BY 1;
  COLLECT STATISTICS ON edw.daily COLUMN (sale_dt);
)"""


def flat(s):
    return " ".join(s.split())


def test_splitter_keeps_macro_body_together():
    stmts = split_statements(LOAD + ";\nSEL 1;", "teradata")
    assert len(stmts) == 2 and stmts[0].sql.endswith(")")


def test_macro_becomes_procedure():
    r = convert_statement(LOAD, "teradata", "snowflake")
    assert r.status is Status.CONVERTED
    sql = flat(r.target_sql)
    assert sql.startswith("CREATE OR REPLACE PROCEDURE edw.load_day(run_dt DATE, region VARCHAR(10) DEFAULT 'IN') "
                          "RETURNS VARCHAR LANGUAGE SQL EXECUTE AS CALLER AS $$ BEGIN")
    assert "DELETE FROM edw.daily WHERE sale_dt = :run_dt AND region = :region;" in sql
    assert "COLLECT" not in sql  # body statements go through the normal rules
    assert sql.endswith("RETURN 'ok'; END; $$")


def test_macro_returning_rows_returns_table():
    r = convert_statement("REPLACE MACRO m (n INTEGER) AS ( SEL TOP 5 a FROM t; )", "teradata", "snowflake")
    sql = flat(r.target_sql)
    assert "RETURNS TABLE()" in sql and "LET res RESULTSET := ( SELECT a FROM t LIMIT 5 ); RETURN TABLE(res);" in sql


def test_macro_with_unconvertible_statement_needs_review():
    r = convert_statement("REPLACE MACRO m AS ( SEL FROM WHERE (((; )", "teradata", "snowflake")
    assert r.status is Status.MANUAL_REVIEW and "TD021" in [i.code for i in r.issues]


def test_macro_on_databricks_needs_review():
    assert convert_statement(LOAD, "teradata", "databricks").status is Status.MANUAL_REVIEW


def test_exec_becomes_call():
    r = convert_statement("EXEC edw.load_day (DATE '2024-01-01', 'US')", "teradata", "snowflake")
    assert r.target_sql == "CALL edw.load_day(DATE '2024-01-01', 'US')" and r.status is Status.CONVERTED
    assert convert_statement("EXEC m", "teradata", "snowflake").target_sql == "CALL m()"


def test_example_converts_fully():
    res = convert_script(EXAMPLE.read_text(), "teradata", "snowflake", "daily_load.sql")
    assert [s.status for s in res.statements] == [Status.CONVERTED] * 4
