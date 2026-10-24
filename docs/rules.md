# Rule catalogue

Severity: **info** = semantics preserved, **warning** = converted but behaviour may differ, **error** = manual rewrite needed.

## Teradata

| Code | Severity | Teradata construct | Converted to | Why |
|---|---|---|---|---|
| TD001 | info | `LOCKING ROW FOR ACCESS ...` | modifier removed | Snowflake/Databricks use MVCC; read locks don't apply |
| TD002 | info | `COLLECT STATISTICS`, `HELP`, `DIAGNOSTIC`, `BT`/`ET` | statement dropped | No target equivalent; stats are automatic |
| TD003 | info | `UPDATE t FROM tbl t, src s SET ... WHERE` | `UPDATE tbl t SET ... FROM src s WHERE` | sqlglot can't parse the Teradata form |
| TD004 | info | `MULTISET` | removed | Target tables allow duplicates by default |
| TD005 | warning | `SET` table | removed | SET tables silently drop duplicate rows; the target won't |
| TD006 | info | `VOLATILE` | `TEMPORARY` | Both are session-scoped |
| TD007 | info | `ON COMMIT PRESERVE ROWS` | removed | Temporary tables last for the session |
| TD008 | info | CTAS `WITH NO DATA` | CTAS with `LIMIT 0` | Creates the structure only |
| TD009 | info | `FALLBACK`, `JOURNAL`, `CHECKSUM`, `NO LOG`, ... | removed | Physical storage options with no target meaning |
| TD010 | warning | `UNIQUE PRIMARY INDEX` | removed | Uniqueness isn't enforced on the target; add a test |
| TD011 | info | `PRIMARY INDEX (cols)` | removed (cols reported) | Candidate clustering key |
| TD012 | info | `INDEX(str, sub)` | `CHARINDEX` / `LOCATE` | sqlglot passes `INDEX` through, which is invalid on the target |
| TD013 | warning | `CAST(x AS DECIMAL FORMAT '...')` | `CAST(x AS DECIMAL)` | Display formats are invalid on the target; date formats are kept |
| TD014 | warning | result-level `SAMPLE n` | `ORDER BY RANDOM() LIMIT n` | sqlglot emits `TABLESAMPLE` after `GROUP BY`, which is invalid |
| TD015 | warning | `SAMPLE 0.x` with WHERE/GROUP | `TABLESAMPLE (x)` on the table | Target samples before filtering; Teradata samples after |
| TD016 | info | `CREATE TABLE b AS a WITH NO DATA` | `CREATE TABLE b LIKE a` | sqlglot emits invalid `CREATE TABLE b AS a` |
| TD017 | info | `CREATE TABLE b AS a WITH DATA` | `CREATE TABLE b AS SELECT * FROM a` | Same as above |
| TD018 | error | fractional `SAMPLE` over a join | manual review | No direct equivalent |
| TD019 | info | `EXEC macro(args)` | `CALL macro(args)` | Snowflake target; other targets go to review |
| TD020 | info | `REPLACE MACRO m (params) AS ( stmts; )` | Snowflake Scripting `CREATE OR REPLACE PROCEDURE ... LANGUAGE SQL` | Every body statement is converted by the normal rules; `:param` bindings carry over; a macro ending in SELECT `RETURNS TABLE()` |
| TD021 | error | macro on a non-Snowflake target, or a body statement needing review | manual review | |

## Engine-level

| Code | Severity | Meaning |
|---|---|---|
| SMA001 | error | Statement could not be parsed |
| SMA002 | error | Statement parsed only as an opaque command (unsupported syntax) |
| SMA003 | error | Target dialect can't express the statement |

## Oracle

| Code | Severity | Oracle construct | Converted to | Why |
|---|---|---|---|---|
| OR001 | info / warning | `WHERE ROWNUM <= n` (also `< n`, `= 1`, `n >= ROWNUM`) | `LIMIT n` | sqlglot passes `ROWNUM` through. It's a warning when the query has `ORDER BY`, because Oracle applies `ROWNUM` *before* sorting |
| OR002 | info / error | `a.x = b.y(+)` outer join | ANSI `LEFT JOIN` | Kept as-is on Snowflake, which supports `(+)`; rewritten for other targets |
| OR003 | error | `MERGE ... UPDATE ... DELETE WHERE` | manual review, with rewrite guidance | sqlglot can't parse it, and `DELETE WHERE` is evaluated on the *updated* row |
| OR004 | info | `TABLESPACE`, `PCTFREE`, `NOLOGGING`, ... | removed | Physical storage options |
| OR005 | error | `CONNECT BY` on targets without it | manual review | Native on Snowflake; other targets need a recursive CTE |
| OR006 | error | `ROWNUM` outside a simple top-N filter | manual review | Needs `ROW_NUMBER() OVER (...)` |

sqlglot already handles `NVL`, `NVL2`, `DECODE`, `INSTR`, `SUBSTR`, `SYSDATE`, `TO_DATE`, `VARCHAR2` / `NUMBER` / `CLOB`, `LISTAGG`,
`FETCH FIRST` and multi-table `INSERT ALL`.

## SQL Server (T-SQL)

| Code | Severity | T-SQL construct | Converted to | Why |
|---|---|---|---|---|
| TS001 | error | `MERGE ... WHEN NOT MATCHED BY SOURCE` on Snowflake | MERGE without that clause, plus instructions for a separate `DELETE ... WHERE NOT EXISTS` | sqlglot silently turns it into `WHEN NOT MATCHED THEN DELETE`, a different and invalid statement. Kept natively for Databricks |
| TS002 | info / warning | `UPDATE alias SET ... FROM t alias JOIN s ON ...` | Snowflake: `UPDATE t AS alias SET ... FROM s WHERE ...`; Databricks: `MERGE INTO ... WHEN MATCHED THEN UPDATE` | It's a warning for MERGE, which fails on duplicate source matches where SQL Server picked one silently. Outer or multiple joins go to review |
| TS003 | info | `DELETE alias FROM t alias JOIN s ON ...` | `DELETE FROM t AS alias WHERE EXISTS (SELECT 1 FROM s WHERE ...)` | Valid on both targets |
| TS004 | info | `ON [PRIMARY]` filegroup | removed | It leaks into the output otherwise |
| TS005 | error | `EXEC proc` | manual review | Port the procedure and `CALL` it |
| TS006 | error | `DECLARE @v`, `SET @v`, `IF`, `WHILE`, `TRY/CATCH` | manual review | Needs a scripting block on the target |
| TS007 | info | `WITH (NOLOCK)` | removed (noted) | Snapshot reads on the target |
| TS008 | info | `#temp` tables | session temporary tables | The `#` prefix is dropped |
| TS010 | info | `GO`, `SET NOCOUNT ON` and other session settings, `PRINT` | dropped | `GO` lines also split batches, like semicolons |
| TS011 | info | `IF OBJECT_ID('x') IS NOT NULL DROP TABLE x` | `DROP TABLE IF EXISTS x` | |
