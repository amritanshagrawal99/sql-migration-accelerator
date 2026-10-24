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

## Engine-level

| Code | Severity | Meaning |
|---|---|---|
| SMA001 | error | Statement could not be parsed |
| SMA002 | error | Statement parsed only as an opaque command (unsupported syntax) |
| SMA003 | error | Target dialect can't express the statement |
