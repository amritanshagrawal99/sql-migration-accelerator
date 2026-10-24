"""Reconciliation computed inside each database: only one row of aggregates crosses the network.

Every engine renders each value into the same canonical text, so fingerprints are comparable across engines
(e.g. a legacy warehouse vs Snowflake):

    numbers    -> CAST(ROUND(x AS DECIMAL(38, 6)) AS VARCHAR)   '10.500000' (ints and decimals compare equal)
    strings    -> RTRIM(x)                                      (CHAR padding ignored)
    dates      -> 'YYYY-MM-DD'
    timestamps -> 'YYYY-MM-DD HH:MM:SS.ffffff'
    booleans   -> '1' / '0'
    NULL       -> 'N', any value -> 'V' || canonical text        (NULL never equals a value)

A row is the canonical values joined by '|'; its hash is the first 15 hex digits of MD5(row) as an integer, and
the table fingerprint is (COUNT(*), SUM(hash)) plus per-column null counts. SUM is order-independent and, unlike
XOR, counts duplicate rows. `reference_fingerprint` computes the same thing in Python and is the specification.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

NUMBER, STRING, DATE, TIMESTAMP, BOOLEAN = "number", "string", "date", "timestamp", "boolean"

# Per-dialect SQL templates. {c} is a quoted column, {s} the row string expression.
_TEMPLATES = {
    "duckdb": {
        NUMBER: "CAST(ROUND(CAST({c} AS DECIMAL(38, 6)), 6) AS VARCHAR)",
        STRING: "RTRIM(CAST({c} AS VARCHAR))",
        DATE: "STRFTIME(CAST({c} AS DATE), '%Y-%m-%d')",
        TIMESTAMP: "STRFTIME(CAST({c} AS TIMESTAMP), '%Y-%m-%d %H:%M:%S.%f')",
        "hash": "CAST(('0x' || SUBSTR(MD5({s}), 1, 15)) AS BIGINT)",
    },
    "snowflake": {
        NUMBER: "TO_VARCHAR(CAST(ROUND(CAST({c} AS NUMBER(38, 6)), 6) AS NUMBER(38, 6)))",
        STRING: "RTRIM(CAST({c} AS VARCHAR))",
        DATE: "TO_CHAR(CAST({c} AS DATE), 'YYYY-MM-DD')",
        TIMESTAMP: "TO_CHAR(CAST({c} AS TIMESTAMP_NTZ), 'YYYY-MM-DD HH24:MI:SS.FF6')",
        "hash": "TO_NUMBER(SUBSTR(MD5({s}), 1, 15), 'XXXXXXXXXXXXXXX')",
    },
    "databricks": {
        NUMBER: "CAST(CAST(ROUND(CAST({c} AS DECIMAL(38, 6)), 6) AS DECIMAL(38, 6)) AS STRING)",
        STRING: "RTRIM(CAST({c} AS STRING))",
        DATE: "DATE_FORMAT(CAST({c} AS DATE), 'yyyy-MM-dd')",
        TIMESTAMP: "DATE_FORMAT(CAST({c} AS TIMESTAMP), 'yyyy-MM-dd HH:mm:ss.SSSSSS')",
        "hash": "CAST(CONV(SUBSTR(MD5({s}), 1, 15), 16, 10) AS BIGINT)",
    },
    "postgres": {
        NUMBER: "CAST(ROUND(CAST({c} AS NUMERIC(38, 6)), 6) AS VARCHAR)",
        STRING: "RTRIM(CAST({c} AS VARCHAR))",
        DATE: "TO_CHAR(CAST({c} AS DATE), 'YYYY-MM-DD')",
        TIMESTAMP: "TO_CHAR(CAST({c} AS TIMESTAMP), 'YYYY-MM-DD HH24:MI:SS.US')",
        "hash": "CAST(CAST(('x' || LPAD(SUBSTR(MD5({s}), 1, 15), 16, '0')) AS BIT(64)) AS BIGINT)",
    },
}
_TEMPLATES["spark"] = _TEMPLATES["databricks"]
_QUOTE = {"duckdb": '"{}"', "snowflake": '"{}"', "postgres": '"{}"', "databricks": "`{}`", "spark": "`{}`"}
SUPPORTED = sorted(_TEMPLATES)


def _canon_sql(col: str, kind: str, dialect: str) -> str:
    t = _TEMPLATES[dialect]
    c = _QUOTE[dialect].format(col)
    value = f"CASE WHEN {c} THEN '1' ELSE '0' END" if kind == BOOLEAN else t[kind].format(c=c)
    return f"CASE WHEN {c} IS NULL THEN 'N' ELSE 'V' || {value} END"


def fingerprint_sql(table: str, columns: list[tuple[str, str]], dialect: str) -> str:
    """One query returning COUNT(*), SUM(row hash), then a null count per column."""
    if dialect not in _TEMPLATES:
        raise ValueError(f"pushdown not supported for {dialect!r}; supported: {', '.join(SUPPORTED)}")
    row = " || '|' || ".join(_canon_sql(c, k, dialect) for c, k in columns)
    h = _TEMPLATES[dialect]["hash"].format(s=f"({row})")
    q = _QUOTE[dialect]
    nulls = "".join(f", SUM(CASE WHEN {q.format(c)} IS NULL THEN 1 ELSE 0 END)" for c, _ in columns)
    return f"SELECT COUNT(*), SUM(CAST({h} AS DECIMAL(38, 0))){nulls} FROM {table}"


# ------------------------------------------------------------------ Python reference (the specification)

def _canon_py(v, kind: str) -> str:
    if v is None:
        return "N"
    if kind == NUMBER:
        text = str(Decimal(str(v)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
    elif kind == STRING:
        text = str(v).rstrip()
    elif kind == DATE:
        text = (v.date() if isinstance(v, dt.datetime) else v).isoformat()
    elif kind == TIMESTAMP:
        text = v.strftime("%Y-%m-%d %H:%M:%S.%f")
    elif kind == BOOLEAN:
        text = "1" if v else "0"
    else:
        raise ValueError(kind)
    return "V" + text


def row_hash(values, kinds) -> int:
    s = "|".join(_canon_py(v, k) for v, k in zip(values, kinds, strict=True))
    return int(hashlib.md5(s.encode()).hexdigest()[:15], 16)


def reference_fingerprint(rows, kinds) -> tuple[int, int]:
    n = total = 0
    for r in rows:
        n += 1
        total += row_hash(r, kinds)
    return n, total


# ------------------------------------------------------------------ type inference and reconciliation

def infer_kinds(conn, table: str, sample: int = 100) -> list[tuple[str, str]]:
    """Column kinds from Python types of a sample (works with any DB-API driver). All-NULL columns count as strings."""
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} LIMIT {int(sample)}")
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    cur.close()
    kinds = []
    for i, name in enumerate(names):
        v = next((r[i] for r in rows if r[i] is not None), None)
        if isinstance(v, bool):
            k = BOOLEAN
        elif isinstance(v, (int, float, Decimal)):
            k = NUMBER
        elif isinstance(v, dt.datetime):
            k = TIMESTAMP
        elif isinstance(v, dt.date):
            k = DATE
        else:
            k = STRING
        kinds.append((name, k))
    return kinds


@dataclass
class PushdownResult:
    source: tuple
    target: tuple
    columns: list[tuple[str, str]]

    @property
    def match(self) -> bool:
        return self.source[:2] == self.target[:2] and self.source[2:] == self.target[2:]

    def describe(self) -> str:
        (sn, sh, *snulls), (tn, th, *tnulls) = self.source, self.target
        if self.match:
            return f"MATCH  {sn} rows (computed in-database)"
        bits = []
        if sn != tn:
            bits.append(f"row count {sn} vs {tn}")
        if sh != th:
            bits.append("content hash differs")
        diffs = {c: (a, b) for (c, _), a, b in zip(self.columns, snulls, tnulls, strict=True) if a != b}
        if diffs:
            bits.append(f"null counts differ: {diffs}")
        return "MISMATCH " + "; ".join(bits) + " (run `sma diff` to locate rows)"


def _run(conn, sql: str) -> tuple:
    cur = conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    cur.close()
    return tuple(int(x) if x is not None else 0 for x in row)


def reconcile_pushdown(src_conn, src_table: str, src_dialect: str, tgt_conn, tgt_table: str, tgt_dialect: str,
                       columns: list[tuple[str, str]] | None = None) -> PushdownResult:
    """Compare two tables with in-database fingerprints. Column kinds are inferred from the source if not given;
    the same column names (case-insensitive match is the caller's job) must exist on both sides."""
    columns = columns or infer_kinds(src_conn, src_table)
    return PushdownResult(_run(src_conn, fingerprint_sql(src_table, columns, src_dialect)),
                          _run(tgt_conn, fingerprint_sql(tgt_table, columns, tgt_dialect)), columns)
