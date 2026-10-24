import duckdb
import pytest

from sma.cli import main
from sma.diff import CHANGED, DUPLICATE_KEY, EXTRA_IN_TARGET, MISSING_IN_TARGET, describe, diff_tables


@pytest.fixture
def pair():
    src, tgt = duckdb.connect(), duckdb.connect()
    src.execute("CREATE TABLE t AS SELECT range AS id, 'name_' || range::VARCHAR AS name, range * 1.5 AS amt FROM range(10000)")
    # Target: same data, different types and column order.
    tgt.execute("CREATE TABLE t AS SELECT amt::DOUBLE AS amt, name, id::BIGINT AS id FROM (SELECT range AS id, "
                "'name_' || range::VARCHAR AS name, range * 1.5 AS amt FROM range(10000))")
    return src, tgt


def run(pair, **kw):
    src, tgt = pair
    return diff_tables(src, "t", tgt, "t", key=["id"], **kw)


def test_identical_tables(pair):
    res = run(pair)
    assert res.match and res.mismatched_buckets == 0 and res.source_rows == 10000


def test_finds_exact_rows_and_columns(pair):
    _, tgt = pair
    tgt.execute("UPDATE t SET amt = amt + 1 WHERE id = 42")
    tgt.execute("UPDATE t SET name = 'x', amt = 0 WHERE id = 7")
    tgt.execute("DELETE FROM t WHERE id = 9999")
    tgt.execute("INSERT INTO t VALUES (1.0, 'new', 10001)")
    res = run(pair)
    by_kind = {(d.kind, d.key) for d in res.diffs}
    assert by_kind == {(CHANGED, ("42",)), (CHANGED, ("7",)), (MISSING_IN_TARGET, ("9999",)), (EXTRA_IN_TARGET, ("10001",))}
    changed = {d.key: d.columns for d in res.diffs if d.kind == CHANGED}
    assert changed[("42",)] == {"amt": ("63", "64")}
    assert set(changed[("7",)]) == {"name", "amt"}
    assert res.mismatched_buckets <= 4  # only the buckets holding bad keys were re-read


def test_duplicate_keys(pair):
    _, tgt = pair
    tgt.execute("INSERT INTO t SELECT * FROM t WHERE id = 5")
    res = run(pair)
    assert [(d.kind, d.key) for d in res.diffs] == [(DUPLICATE_KEY, ("5",))]


def test_composite_key():
    src, tgt = duckdb.connect(), duckdb.connect()
    for c in (src, tgt):
        c.execute("CREATE TABLE s AS SELECT range % 10 AS store, range // 10 AS day, range AS sales FROM range(100)")
    tgt.execute("UPDATE s SET sales = -1 WHERE store = 3 AND day = 4")
    res = diff_tables(src, "s", tgt, "s", key=["store", "day"])
    assert [(d.key, d.columns) for d in res.diffs] == [(("3", "4"), {"sales": ("43", "-1")})]


def test_truncation_and_describe(pair):
    _, tgt = pair
    tgt.execute("UPDATE t SET amt = 0 WHERE id < 50")
    res = run(pair, max_diffs=10)
    assert res.truncated and len(res.diffs) == 10
    text = describe(res, limit=3)
    assert text.startswith("DIFF") and "(truncated)" in text and "... 7 more" in text


def test_bad_key():
    src, tgt = duckdb.connect(), duckdb.connect()
    for c in (src, tgt):
        c.execute("CREATE TABLE t AS SELECT 1 AS a")
    with pytest.raises(ValueError, match="key column"):
        diff_tables(src, "t", tgt, "t", key=["nope"], columns=["a"])


def test_cli_diff(tmp_path, capsys):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for path in (a, b):
        con = duckdb.connect(str(path))
        con.execute("CREATE TABLE t AS SELECT range AS id, range * 2 AS v FROM range(100)")
        con.close()
    con = duckdb.connect(str(b))
    con.execute("UPDATE t SET v = 0 WHERE id = 3")
    con.close()
    rc = main(["diff", "--source-url", f"duckdb:///{a}", "--source-table", "t", "--target-url", f"duckdb:///{b}", "--key", "id"])
    assert rc == 1
    assert "~ (3)  v: '6' -> '0'" in capsys.readouterr().out
