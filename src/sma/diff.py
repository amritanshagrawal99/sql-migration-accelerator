"""Locate the exact rows and columns that differ between a source table and its migrated copy.

Two passes keep memory bounded on large tables:
  1. stream both sides once, hashing every row into one of N buckets by its key;
  2. stream again, keeping only rows whose bucket fingerprints disagree, and compare them key by key.
With 256 buckets and a handful of bad rows, pass 2 holds well under 1% of the table in memory.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .reconcile import _MASK, NormaliseOptions, _columns, _row_hash, normalise

MISSING_IN_TARGET = "missing_in_target"
EXTRA_IN_TARGET = "extra_in_target"
CHANGED = "changed"
DUPLICATE_KEY = "duplicate_key"


@dataclass
class RowDiff:
    key: tuple[str, ...]
    kind: str
    columns: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)  # column -> (source, target)


@dataclass
class DiffResult:
    key: list[str]
    columns: list[str]
    buckets: int
    mismatched_buckets: int
    source_rows: int
    target_rows: int
    diffs: list[RowDiff]
    truncated: bool

    @property
    def match(self) -> bool:
        return not self.diffs

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys((MISSING_IN_TARGET, EXTRA_IN_TARGET, CHANGED, DUPLICATE_KEY), 0)
        for d in self.diffs:
            out[d.kind] += 1
        return out


def _bucket(key: tuple[str, ...], buckets: int) -> int:
    return int.from_bytes(hashlib.blake2b("\x1f".join(key).encode(), digest_size=4).digest(), "big") % buckets


def _stream(conn, table: str, columns: list[str], key_idx: list[int], opts: NormaliseOptions, batch_size: int):
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(columns)} FROM {table}")
    try:
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                vals = [normalise(v, opts) for v in row]
                yield tuple(vals[i] for i in key_idx), vals
    finally:
        cur.close()


def _bucket_fingerprints(stream, buckets: int) -> tuple[list[tuple[int, int]], int]:
    fp = [(0, 0)] * buckets
    n = 0
    for key, vals in stream:
        b = _bucket(key, buckets)
        h, c = fp[b]
        fp[b] = ((h + _row_hash(vals)) & _MASK, c + 1)
        n += 1
    return fp, n


def _collect(stream, wanted: set[int], buckets: int) -> tuple[dict[tuple, list[str]], set[tuple]]:
    rows: dict[tuple, list[str]] = {}
    dupes: set[tuple] = set()
    for key, vals in stream:
        if _bucket(key, buckets) in wanted:
            if key in rows:
                dupes.add(key)
            rows[key] = vals
    return rows, dupes


def diff_tables(src_conn, src_table: str, tgt_conn, tgt_table: str, key: list[str], columns: list[str] | None = None,
                opts: NormaliseOptions | None = None, buckets: int = 256, max_diffs: int = 1000,
                batch_size: int = 50_000) -> DiffResult:
    opts = opts or NormaliseOptions()
    key = [k.lower() for k in key]
    shared = set(_columns(src_conn, src_table)) & set(_columns(tgt_conn, tgt_table))
    missing = [k for k in key if k not in shared]
    if missing:
        raise ValueError(f"key column(s) {missing} not found in both tables")
    columns = sorted(shared) if columns is None else [c.lower() for c in columns]
    columns = key + [c for c in columns if c not in key]
    key_idx = list(range(len(key)))

    def src():
        return _stream(src_conn, src_table, columns, key_idx, opts, batch_size)

    def tgt():
        return _stream(tgt_conn, tgt_table, columns, key_idx, opts, batch_size)

    src_fp, n_src = _bucket_fingerprints(src(), buckets)
    tgt_fp, n_tgt = _bucket_fingerprints(tgt(), buckets)
    bad = {b for b in range(buckets) if src_fp[b] != tgt_fp[b]}

    diffs: list[RowDiff] = []
    if bad:
        s_rows, s_dupes = _collect(src(), bad, buckets)
        t_rows, t_dupes = _collect(tgt(), bad, buckets)
        for k in sorted(s_dupes | t_dupes):
            diffs.append(RowDiff(k, DUPLICATE_KEY))
        for k in sorted(set(s_rows) | set(t_rows)):
            if k in s_dupes or k in t_dupes:
                continue
            if k not in t_rows:
                diffs.append(RowDiff(k, MISSING_IN_TARGET))
            elif k not in s_rows:
                diffs.append(RowDiff(k, EXTRA_IN_TARGET))
            elif s_rows[k] != t_rows[k]:
                changed = {c: (a, b) for c, a, b in zip(columns, s_rows[k], t_rows[k], strict=True) if a != b}
                diffs.append(RowDiff(k, CHANGED, changed))
    return DiffResult(key, columns, buckets, len(bad), n_src, n_tgt, diffs[:max_diffs], len(diffs) > max_diffs)


def describe(res: DiffResult, limit: int = 20) -> str:
    if res.match:
        return f"MATCH  {res.source_rows} rows, key ({', '.join(res.key)})"
    c = res.counts()
    lines = [f"DIFF   {res.source_rows} source rows vs {res.target_rows} target rows; "
             f"{res.mismatched_buckets}/{res.buckets} buckets differ",
             "       " + ", ".join(f"{k}={v}" for k, v in c.items() if v) + (" (truncated)" if res.truncated else "")]
    for d in res.diffs[:limit]:
        k = ", ".join(d.key)
        if d.kind == CHANGED:
            cols = "; ".join(f"{col}: {a!r} -> {b!r}" for col, (a, b) in d.columns.items())
            lines.append(f"  ~ ({k})  {cols}")
        else:
            lines.append(f"  {'-' if d.kind == MISSING_IN_TARGET else '+' if d.kind == EXTRA_IN_TARGET else '!'} ({k})  {d.kind}")
    if len(res.diffs) > limit:
        lines.append(f"  ... {len(res.diffs) - limit} more")
    return "\n".join(lines)
