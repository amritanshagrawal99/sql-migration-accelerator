import datetime as dt
from decimal import Decimal

import duckdb
import pytest

from sma.cli import main
from sma.reconcile import NormaliseOptions, normalise, reconcile


@pytest.fixture
def conns():
    src, tgt = duckdb.connect(), duckdb.connect()
    src.execute("CREATE TABLE c (id INTEGER, name CHAR(10), amt DECIMAL(18,2), ts TIMESTAMP)")
    src.execute("INSERT INTO c VALUES (1, 'ann', 10.50, '2024-01-01 10:00:00'), (2, 'bob', NULL, '2024-01-02 11:00:00'),"
                " (3, 'cy', 7.00, NULL)")
    # Target: different column order, VARCHAR instead of CHAR, DOUBLE instead of DECIMAL, rows in another order.
    tgt.execute("CREATE TABLE c (ts TIMESTAMP, amt DOUBLE, name VARCHAR, id BIGINT)")
    tgt.execute("INSERT INTO c VALUES (NULL, 7.0, 'cy', 3), ('2024-01-02 11:00:00', NULL, 'bob', 2),"
                " ('2024-01-01 10:00:00', 10.5, 'ann', 1)")
    return src, tgt


def test_equivalent_tables_match_despite_type_and_order_differences(conns):
    res = reconcile(*_pair(conns))
    assert res.match, res.describe()
    assert res.source.row_count == 3
    assert res.source.null_counts["amt"] == 1


def test_changed_value_is_detected(conns):
    src, tgt = conns
    tgt.execute("UPDATE c SET amt = 10.51 WHERE id = 1")
    res = reconcile(*_pair(conns))
    assert res.row_count_match and not res.hash_match and not res.match


def test_duplicate_row_is_detected(conns):
    src, tgt = conns
    tgt.execute("INSERT INTO c SELECT * FROM c WHERE id = 3")
    res = reconcile(*_pair(conns))
    assert not res.row_count_match and "row count 3 vs 4" in res.describe()


def test_missing_column_reported(conns):
    src, tgt = conns
    tgt.execute("ALTER TABLE c DROP COLUMN ts")
    res = reconcile(*_pair(conns))
    assert res.missing_in_target == ["ts"] and not res.match


def test_null_count_diff(conns):
    src, tgt = conns
    tgt.execute("UPDATE c SET name = NULL WHERE id = 2")
    assert reconcile(*_pair(conns)).null_count_diffs == {"name": (0, 1)}


@pytest.mark.parametrize("value,expected", [
    (None, "\x00"), (True, "1"), (Decimal("10.50"), "10.5"), (7.0, "7"), (1 / 3, "0.333333"),
    ("ab   ", "ab"), (dt.datetime(2024, 1, 1, 10), "2024-01-01 10:00:00.000000"), (dt.date(2024, 1, 1), "2024-01-01"),
])
def test_normalise(value, expected):
    assert normalise(value, NormaliseOptions()) == expected


def test_cli_reconcile(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for path in (a, b):
        con = duckdb.connect(str(path))
        con.execute("CREATE TABLE t AS SELECT range AS id, range * 2 AS v FROM range(1000)")
        con.close()
    assert main(["reconcile", "--source-url", f"duckdb:///{a}", "--source-table", "t", "--target-url", f"duckdb:///{b}"]) == 0


def _pair(conns):
    src, tgt = conns
    return src, "c", tgt, "c"
