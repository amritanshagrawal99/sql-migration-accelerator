import pytest

from sma import convert_statement
from sma.models import Status


def conv(sql, target="snowflake"):
    return convert_statement(sql, "teradata", target)


def flat(sql):
    return " ".join(sql.split())


def codes(res):
    return [i.code for i in res.issues]


def test_sel_and_del_abbreviations():
    assert flat(conv("SEL a FROM t").target_sql) == "SELECT a FROM t"
    assert flat(conv("DEL FROM t WHERE id = 1").target_sql) == "DELETE FROM t WHERE id = 1"


def test_locking_modifier_removed():
    r = conv("LOCKING ROW FOR ACCESS SEL * FROM t")
    assert r.status is Status.CONVERTED
    assert flat(r.target_sql) == "SELECT * FROM t"
    assert "TD001" in codes(r)


@pytest.mark.parametrize("sql", [
    "COLLECT STATISTICS ON t COLUMN (id)",
    "COLLECT STATS COLUMN (a) ON t",
    "HELP TABLE t",
    "BT",
])
def test_no_op_statements_dropped(sql):
    r = conv(sql)
    assert r.status is Status.DROPPED
    assert r.target_sql is None


def test_update_from_with_aliased_target():
    r = conv("UPDATE tgt FROM db.dim tgt, stage src SET name = src.name WHERE tgt.id = src.id")
    assert flat(r.target_sql) == "UPDATE db.dim AS tgt SET name = src.name FROM stage AS src WHERE tgt.id = src.id"
    assert "TD003" in codes(r)


def test_update_from_plain_target():
    r = conv("UPD db.dim FROM stage s SET name = s.name WHERE db.dim.id = s.id")
    assert flat(r.target_sql).startswith("UPDATE db.dim SET name = s.name FROM stage AS s")


def test_multiset_volatile_ctas():
    r = conv("CREATE MULTISET VOLATILE TABLE vt, NO LOG AS (SEL * FROM t) WITH DATA "
             "PRIMARY INDEX (id) ON COMMIT PRESERVE ROWS")
    out = flat(r.target_sql)
    assert out.startswith("CREATE TEMPORARY TABLE vt AS")
    for leftover in ("MULTISET", "PRIMARY INDEX", "WITH DATA", "ON COMMIT", "NO LOG"):
        assert leftover not in out
    assert r.status is Status.CONVERTED


def test_set_table_and_upi_raise_warnings():
    r = conv("CREATE SET TABLE t, FALLBACK (id INTEGER) UNIQUE PRIMARY INDEX (id)")
    assert flat(r.target_sql) == "CREATE TABLE t ( id INT )"
    assert r.status is Status.CONVERTED_WITH_WARNINGS
    assert {"TD005", "TD010", "TD011"} <= set(codes(r))


def test_with_no_data_ctas_is_zero_row():
    r = conv("CREATE TABLE m AS (SEL a FROM t) WITH NO DATA")
    assert flat(r.target_sql) == "CREATE TABLE m AS SELECT a FROM t LIMIT 0"


def test_table_copy_with_no_data_uses_like():
    assert flat(conv("CREATE TABLE b AS a WITH NO DATA").target_sql) == "CREATE TABLE b LIKE a"


def test_table_copy_with_data_uses_select():
    assert flat(conv("CREATE TABLE b AS a WITH DATA").target_sql) == "CREATE TABLE b AS SELECT * FROM a"


def test_index_function_becomes_position():
    out = flat(conv("SEL INDEX(name, 'x') FROM t").target_sql)
    assert out == "SELECT CHARINDEX('x', name) FROM t"
    assert "LOCATE" in conv("SEL INDEX(name, 'x') FROM t", target="databricks").target_sql.upper()


def test_numeric_cast_format_removed_but_date_format_kept():
    r = conv("SEL CAST(x AS DECIMAL(18,2) FORMAT 'ZZ9.99'), CAST(d AS DATE FORMAT 'YYYY-MM-DD') FROM t")
    out = flat(r.target_sql)
    assert "FORMAT" not in out
    assert "CAST(x AS DECIMAL(18, 2))" in out
    assert "DATE(d," in out
    assert "TD013" in codes(r)


def test_result_level_sample_count():
    r = conv("SEL a, COUNT(*) FROM t GROUP BY a SAMPLE 100")
    out = flat(r.target_sql)
    assert "TABLESAMPLE" not in out
    assert out.endswith("ORDER BY RANDOM() LIMIT 100")
    assert "TD014" in codes(r)


def test_fractional_sample_moves_before_where():
    r = conv("SEL a FROM t WHERE b > 1 SAMPLE 0.25")
    assert flat(r.target_sql) == "SELECT a FROM t TABLESAMPLE (25) WHERE b > 1"
    assert r.status is Status.CONVERTED_WITH_WARNINGS


def test_fractional_sample_over_join_needs_review():
    r = conv("SEL a FROM t JOIN s ON t.id = s.id WHERE b > 1 SAMPLE 0.25")
    assert r.status is Status.MANUAL_REVIEW


def test_qualify_and_top_survive():
    out = flat(conv("SEL TOP 5 * FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY ts DESC) = 1").target_sql)
    assert "QUALIFY" in out and out.endswith("LIMIT 5")


def test_databricks_target():
    r = conv("CREATE MULTISET TABLE t (id INTEGER) PRIMARY INDEX (id)", target="databricks")
    assert flat(r.target_sql) == "CREATE TABLE t ( id INT )"
