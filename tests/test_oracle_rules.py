import pytest

from sma import convert_statement
from sma.models import Status


def conv(sql, target="snowflake"):
    return convert_statement(sql, "oracle", target)


def flat(sql):
    return " ".join(sql.split())


def codes(r):
    return [i.code for i in r.issues]


@pytest.mark.parametrize("pred,cap", [("ROWNUM <= 10", 10), ("ROWNUM < 10", 9), ("ROWNUM = 1", 1), ("10 >= ROWNUM", 10)])
def test_rownum_top_n_becomes_limit(pred, cap):
    r = conv(f"SELECT * FROM emp WHERE {pred} AND dept = 3")
    assert flat(r.target_sql) == f"SELECT * FROM emp WHERE dept = 3 LIMIT {cap}"
    assert r.status is Status.CONVERTED and "OR001" in codes(r)


def test_rownum_with_order_by_warns_about_semantics():
    r = conv("SELECT * FROM emp WHERE ROWNUM = 1 ORDER BY salary DESC")
    assert flat(r.target_sql).endswith("ORDER BY salary DESC LIMIT 1")
    assert r.status is Status.CONVERTED_WITH_WARNINGS


def test_other_rownum_uses_need_review():
    assert conv("SELECT ROWNUM AS rn, name FROM emp").status is Status.MANUAL_REVIEW


def test_join_marks_native_on_snowflake_rewritten_elsewhere():
    q = "SELECT e.name, d.name FROM emp e, dept d WHERE e.dept_id = d.id(+)"
    assert "(+)" in conv(q).target_sql
    r = conv(q, "databricks")
    assert flat(r.target_sql) == "SELECT e.name, d.name FROM emp AS e LEFT JOIN dept AS d ON e.dept_id = d.id"
    assert "OR002" in codes(r)


def test_storage_clauses_removed():
    r = conv("CREATE TABLE t (id NUMBER(10), name VARCHAR2(100)) TABLESPACE users PCTFREE 10 NOLOGGING")
    assert flat(r.target_sql) == "CREATE TABLE t ( id DECIMAL(10), name VARCHAR(100) )"
    assert r.status is Status.CONVERTED


def test_connect_by_native_on_snowflake_review_elsewhere():
    q = "SELECT employee_id, LEVEL FROM emp START WITH manager_id IS NULL CONNECT BY PRIOR employee_id = manager_id"
    assert conv(q).status is Status.CONVERTED
    assert conv(q, "databricks").status is Status.MANUAL_REVIEW


def test_merge_delete_where_explained():
    r = conv("MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.v = s.v DELETE WHERE t.v IS NULL")
    assert r.status is Status.MANUAL_REVIEW and codes(r) == ["OR003"]


def test_sqlglot_handles_the_rest():
    out = flat(conv("SELECT NVL(x, 0), INSTR(name, 'a'), SYSDATE FROM dual").target_sql)
    assert out == "SELECT COALESCE(x, 0), CHARINDEX('a', name), SYSDATE() FROM dual"
