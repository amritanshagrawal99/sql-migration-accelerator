"""Source-vs-target data reconciliation over any DB-API 2.0 connection.

Each table is reduced to a fingerprint: row count, per-column null counts and an
order-independent, duplicate-sensitive hash of all rows (sum of per-row hashes mod 2**64).
Values are normalised first so that type differences between engines (Decimal vs float,
CHAR padding, timestamp precision) do not produce false mismatches.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

_MASK = (1 << 64) - 1
_NULL = "\x00"
_SEP = "\x1f"


@dataclass
class NormaliseOptions:
    float_digits: int = 6       # round floats/decimals to this many places
    rstrip_strings: bool = True  # Teradata CHAR(n) pads with spaces; target VARCHAR does not
    case_insensitive_strings: bool = False


@dataclass
class Fingerprint:
    table: str
    columns: list[str]
    row_count: int
    row_hash: int
    null_counts: dict[str, int]


@dataclass
class ReconResult:
    source: Fingerprint
    target: Fingerprint
    missing_in_target: list[str] = field(default_factory=list)
    missing_in_source: list[str] = field(default_factory=list)

    @property
    def row_count_match(self) -> bool:
        return self.source.row_count == self.target.row_count

    @property
    def hash_match(self) -> bool:
        return self.source.row_hash == self.target.row_hash

    @property
    def null_count_diffs(self) -> dict[str, tuple[int, int]]:
        return {c: (self.source.null_counts[c], self.target.null_counts[c])
                for c in self.source.columns
                if self.source.null_counts.get(c) != self.target.null_counts.get(c)}

    @property
    def match(self) -> bool:
        return (self.row_count_match and self.hash_match and not self.missing_in_target
                and not self.missing_in_source)

    def describe(self) -> str:
        if self.match:
            return f"MATCH  {self.source.table} == {self.target.table} ({self.source.row_count} rows)"
        bits = []
        if self.missing_in_target:
            bits.append(f"columns missing in target: {self.missing_in_target}")
        if self.missing_in_source:
            bits.append(f"columns missing in source: {self.missing_in_source}")
        if not self.row_count_match:
            bits.append(f"row count {self.source.row_count} vs {self.target.row_count}")
        if not self.hash_match:
            bits.append("content hash differs")
        if self.null_count_diffs:
            bits.append(f"null counts differ: {self.null_count_diffs}")
        return f"MISMATCH {self.source.table} vs {self.target.table}: " + "; ".join(bits)


def normalise(value: Any, opts: NormaliseOptions) -> str:
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, (float, Decimal)):
        q = round(float(value), opts.float_digits)
        if q == int(q):
            return str(int(q))
        return f"{q:.{opts.float_digits}f}".rstrip("0").rstrip(".")
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.isoformat(sep=" ", timespec="microseconds")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    s = str(value)
    if opts.rstrip_strings:
        s = s.rstrip()
    if opts.case_insensitive_strings:
        s = s.lower()
    return s


def _row_hash(values: Iterable[str]) -> int:
    return int.from_bytes(hashlib.sha256(_SEP.join(values).encode()).digest()[:8], "big")


def _columns(conn, table: str) -> list[str]:
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} WHERE 1 = 0")
    cols = [d[0].lower() for d in cur.description]
    cur.close()
    return cols


def fingerprint(conn, table: str, columns: list[str], opts: NormaliseOptions | None = None,
                batch_size: int = 50_000) -> Fingerprint:
    opts = opts or NormaliseOptions()
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(columns)} FROM {table}")
    count, total = 0, 0
    nulls = dict.fromkeys(columns, 0)
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            vals = []
            for col, v in zip(columns, row, strict=True):
                if v is None:
                    nulls[col] += 1
                vals.append(normalise(v, opts))
            total = (total + _row_hash(vals)) & _MASK
            count += 1
    cur.close()
    return Fingerprint(table, columns, count, total, nulls)


def reconcile(src_conn, src_table: str, tgt_conn, tgt_table: str, columns: list[str] | None = None,
              opts: NormaliseOptions | None = None) -> ReconResult:
    """Compare two tables. Without `columns`, compares the columns both sides share (sorted)."""
    src_cols, tgt_cols = _columns(src_conn, src_table), _columns(tgt_conn, tgt_table)
    if columns is None:
        columns = sorted(set(src_cols) & set(tgt_cols))
    columns = [c.lower() for c in columns]
    return ReconResult(
        fingerprint(src_conn, src_table, columns, opts),
        fingerprint(tgt_conn, tgt_table, columns, opts),
        missing_in_target=sorted(set(src_cols) - set(tgt_cols)),
        missing_in_source=sorted(set(tgt_cols) - set(src_cols)),
    )
