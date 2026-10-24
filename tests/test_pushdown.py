import datetime as dt
from decimal import Decimal

import duckdb
import pytest
import sqlglot

from sma.pushdown import (
    BOOLEAN,
    DATE,
    NUMBER,
    STRING,
    SUPPORTED,
    TIMESTAMP,
    fingerprint_sql,
    infer_kinds,
    reconcile_pushdown,
    reference_fingerprint,
)

ROWS = [
    (1, Decimal("10.50"), "ann  ", dt.date(2024, 1, 1), dt.datetime(2024, 1, 1, 10, 0, 0, 123456), True),
    (2, None, "bob", dt.date(2024, 2, 29), dt.datetime(2024, 2, 29, 23, 59, 59), False),
    (3, Decimal("-0.33"), None, None, None, None),
    (3, Decimal("-0.33"), None, None, None, None),  # duplicate row must count twice
]
KINDS = [("id", NUMBER), ("amt", NUMBER), ("name", STRING), ("d", DATE), ("ts", TIMESTAMP), ("flag", BOOLEAN)]


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE t (id INTEGER, amt DECIMAL(18,2), name VARCHAR, d DATE, ts TIMESTAMP, flag BOOLEAN)")
    c.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    return c


def test_duckdb_sql_matches_python_reference(con):
    n, total, *nulls = con.execute(fingerprint_sql("t", KINDS, "duckdb")).fetchone()
    assert (n, int(total)) == reference_fingerprint(ROWS, [k for _, k in KINDS])
    assert nulls == [0, 1, 2, 2, 2, 2]


def test_types_that_differ_between_engines_still_match():
    a, b = duckdb.connect(), duckdb.connect()
    a.execute("CREATE TABLE t AS SELECT 5::INTEGER AS n, 'x'::CHAR(5) AS s, 1.5::DOUBLE AS f")
    b.execute("CREATE TABLE t AS SELECT 5::DECIMAL(38,0) AS n, 'x' AS s, 1.5::DECIMAL(10,4) AS f")
    res = reconcile_pushdown(a, "t", "duckdb", b, "t", "duckdb")
    assert res.match, res.describe()


def test_detects_change_duplicate_and_null(con):
    other = duckdb.connect()
    other.execute("CREATE TABLE t (id INTEGER, amt DECIMAL(18,2), name VARCHAR, d DATE, ts TIMESTAMP, flag BOOLEAN)")
    other.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    assert reconcile_pushdown(con, "t", "duckdb", other, "t", "duckdb", KINDS).match
    other.execute("UPDATE t SET amt = 10.51 WHERE id = 1")
    res = reconcile_pushdown(con, "t", "duckdb", other, "t", "duckdb", KINDS)
    assert not res.match and "content hash differs" in res.describe()
    other.execute("UPDATE t SET amt = 10.50 WHERE id = 1")
    other.execute("DELETE FROM t WHERE id = 3")
    other.execute("INSERT INTO t VALUES (3, -0.33, NULL, NULL, NULL, NULL)")  # one copy instead of two
    assert "row count 4 vs 3" in reconcile_pushdown(con, "t", "duckdb", other, "t", "duckdb", KINDS).describe()


def test_null_is_not_a_value():
    a, b = duckdb.connect(), duckdb.connect()
    a.execute("CREATE TABLE t AS SELECT NULL::VARCHAR AS s")
    b.execute("CREATE TABLE t AS SELECT 'N' AS s")
    assert not reconcile_pushdown(a, "t", "duckdb", b, "t", "duckdb", [("s", STRING)]).match


def test_infer_kinds(con):
    assert infer_kinds(con, "t") == KINDS


@pytest.mark.parametrize("dialect", SUPPORTED)
def test_generated_sql_parses_in_its_dialect(dialect):
    sql = fingerprint_sql("db.sch.t", KINDS, dialect)
    sqlglot.parse_one(sql, read=dialect)  # must be valid for that engine's grammar
    assert sql.startswith("SELECT COUNT(*), SUM(CAST(")


def test_snowflake_golden():
    sql = fingerprint_sql("t", [("amt", NUMBER), ("ts", TIMESTAMP)], "snowflake")
    assert "TO_NUMBER(SUBSTR(MD5(" in sql and "'YYYY-MM-DD HH24:MI:SS.FF6'" in sql


def test_unsupported_dialect():
    with pytest.raises(ValueError, match="pushdown not supported"):
        fingerprint_sql("t", KINDS, "teradata")


def test_cli_pushdown(tmp_path):
    from sma.cli import main
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for path in (a, b):
        c = duckdb.connect(str(path))
        c.execute("CREATE TABLE t AS SELECT range AS id, range * 0.5 AS v FROM range(1000)")
        c.close()
    args = ["reconcile", "--source-url", f"duckdb:///{a}", "--source-table", "t", "--target-url", f"duckdb:///{b}", "--pushdown"]
    assert main(args) == 0
