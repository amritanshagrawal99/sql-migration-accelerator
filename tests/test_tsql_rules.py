import pytest

from sma import convert_statement
from sma.models import Status


def conv(sql, target="snowflake"):
    return convert_statement(sql, "tsql", target)


def flat(sql):
    return " ".join(sql.split())


@pytest.mark.parametrize("sql", ["GO", "SET NOCOUNT ON", "SET XACT_ABORT ON", "PRINT 'loading orders'"])
def test_session_noise_dropped(sql):
    assert conv(sql).status is Status.DROPPED


def test_drop_if_exists():
    r = conv("IF OBJECT_ID('tempdb..#tmp') IS NOT NULL DROP TABLE #tmp")
    assert flat(r.target_sql) == "DROP TABLE IF EXISTS tmp"


@pytest.mark.parametrize("sql", ["EXEC dbo.usp_Load @p = 1", "DECLARE @d DATE = GETDATE()", "SET @d = 1",
                                 "IF @x > 1 SELECT 1", "WHILE @i < 10 SET @i = @i + 1"])
def test_procedural_needs_review(sql):
    assert conv(sql).status is Status.MANUAL_REVIEW


def test_update_from_join_snowflake():
    r = conv("UPDATE o SET o.Total = s.Total FROM dbo.Orders o JOIN #tmp s ON s.Id = o.Id WHERE o.Status = 'Open'")
    assert flat(r.target_sql) == ("UPDATE dbo.Orders AS o SET Total = s.Total FROM tmp AS s "
                                  "WHERE s.Id = o.Id AND o.Status = 'Open'")
    assert r.status is Status.CONVERTED


def test_update_from_join_databricks_becomes_merge():
    r = conv("UPDATE o SET o.Total = s.Total FROM dbo.Orders o JOIN dbo.S s ON s.Id = o.Id", "databricks")
    assert flat(r.target_sql) == ("MERGE INTO dbo.Orders AS o USING dbo.S AS s ON s.Id = o.Id "
                                  "WHEN MATCHED THEN UPDATE SET Total = s.Total")
    assert r.status is Status.CONVERTED_WITH_WARNINGS  # MERGE rejects multiple matches; SQL Server didn't


@pytest.mark.parametrize("target", ["snowflake", "databricks"])
def test_delete_from_join_becomes_exists(target):
    r = conv("DELETE o FROM dbo.Orders o JOIN dbo.Bad b ON b.Id = o.Id WHERE b.Reason = 'fraud'", target)
    assert flat(r.target_sql) == ("DELETE FROM dbo.Orders AS o WHERE EXISTS( SELECT 1 FROM dbo.Bad AS b "
                                  "WHERE b.Id = o.Id AND b.Reason = 'fraud' )")


def test_outer_join_update_needs_review():
    assert conv("UPDATE o SET o.x = 1 FROM dbo.Orders o LEFT JOIN dbo.S s ON s.Id = o.Id").status is Status.MANUAL_REVIEW


MERGE_BY_SOURCE = ("MERGE dbo.T AS tgt USING dbo.S AS src ON tgt.Id = src.Id WHEN MATCHED THEN UPDATE SET Name = src.Name "
                   "WHEN NOT MATCHED BY SOURCE THEN DELETE")


def test_merge_by_source_never_emits_the_wrong_delete_on_snowflake():
    r = conv(MERGE_BY_SOURCE)
    assert r.status is Status.MANUAL_REVIEW
    assert "DELETE" not in r.target_sql.upper()  # sqlglot alone would emit WHEN NOT MATCHED THEN DELETE


def test_merge_by_source_native_on_databricks():
    r = conv(MERGE_BY_SOURCE, "databricks")
    assert "WHEN NOT MATCHED BY SOURCE THEN DELETE" in flat(r.target_sql) and r.status is Status.CONVERTED


def test_filegroup_and_nolock_removed():
    r = conv("CREATE TABLE dbo.T (Id INT IDENTITY(1,1) PRIMARY KEY) ON [PRIMARY]")
    assert "PRIMARY\"" not in r.target_sql and "AUTOINCREMENT" in r.target_sql
    r = conv("SELECT TOP 5 * FROM dbo.Orders WITH (NOLOCK)")
    assert flat(r.target_sql) == "SELECT * FROM dbo.Orders LIMIT 5" and "TS007" in [i.code for i in r.issues]


def test_go_lines_split_batches():
    from sma.splitter import split_statements
    stmts = split_statements("SELECT 1\nGO\nSELECT 2\n  go  \nSELECT 'GO'", "tsql")
    assert [s.sql for s in stmts] == ["SELECT 1", "SELECT 2", "SELECT 'GO'"]
    assert [s.line for s in stmts] == [1, 3, 5]
