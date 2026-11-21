"""List how the schema of the NYC TLC yellow taxi files changes from month to month, reading only the Parquet footers.

    python scripts/nyc_schema_timeline.py                                 # 2009-01 .. 2026-09, every month
    python scripts/nyc_schema_timeline.py --start 2018-01 --end 2020-12   # a part of it
    python scripts/nyc_schema_timeline.py --every 6                       # a quicker look: every sixth month

This is exploration for the real-data run, not part of the evaluated code and not part of the test suite. It reads no trip rows: for each month
DuckDB fetches the footer of the file (a few KB) to get the column names, types and row count, so it needs the internet (the files are on
CloudFront) but almost no bandwidth. It writes results/nyc/yellow_schema_timeline.json and prints one line per month where the schema changed.
A month that cannot be read (not published yet, no network) is recorded as an error and skipped when comparing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_BASE = "https://d37ci6vzurychx.cloudfront.net/trip-data"
OUT = Path(__file__).resolve().parent.parent / "results" / "nyc" / "yellow_schema_timeline.json"


def month_range(start: str, end: str, every: int = 1) -> list[str]:
    """'2018-11' .. '2019-02' -> ['2018-11', '2018-12', '2019-01', '2019-02'], taking every `every`-th month."""
    (y0, m0), (y1, m1) = (tuple(int(p) for p in s.split("-")) for s in (start, end))
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out[::every]


def describe(con, base: str, month: str) -> dict:
    """The columns and row count of one month, or {'error': ...} (short text, no trip data)."""
    url = f"{base}/yellow_tripdata_{month}.parquet"
    try:
        cols = con.execute("select column_name, column_type from (describe select * from read_parquet(?))", [url]).fetchall()
        rows = con.execute("select count(*) from read_parquet(?)", [url]).fetchone()[0]
    except Exception as exc:                                              # noqa: BLE001 - one bad month must not stop the scan
        return {"error": str(exc).splitlines()[0][:200]}
    return {"rows": rows, "columns": [[name, typ] for name, typ in cols]}


def changes(prev: dict, cur: dict) -> dict:
    """What differs between two months' column lists: added, removed, retyped, and renames that differ only in case (Airport_fee / airport_fee)."""
    a, b = dict(prev["columns"]), dict(cur["columns"])
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    lower_added = {c.lower(): c for c in added}
    case_renamed = [[c, lower_added[c.lower()]] for c in removed if c.lower() in lower_added]
    renamed_from = {old for old, _ in case_renamed}
    renamed_to = {new for _, new in case_renamed}
    return {
        "added": [c for c in added if c not in renamed_to],
        "removed": [c for c in removed if c not in renamed_from],
        "case_renamed": case_renamed,
        "retyped": [[c, a[c], b[c]] for c in sorted(set(a) & set(b)) if a[c] != b[c]],
    }


def timeline(con, base: str, months: list[str]) -> dict:
    described = {m: describe(con, base, m) for m in months}
    log, last = [], None
    for m in months:
        d = described[m]
        if "error" in d:
            continue
        if last is not None:
            diff = changes(described[last], d)
            if any(diff.values()):
                log.append({"from": last, "to": m, **diff})
        last = m
    return {"base": base, "months": described, "changes": log}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default="2009-01")
    ap.add_argument("--end", default="2026-09")
    ap.add_argument("--every", type=int, default=1, help="look at every Nth month")
    ap.add_argument("--base", default=DEFAULT_BASE, help="where yellow_tripdata_YYYY-MM.parquet lives (a URL or a folder)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    import duckdb
    con = duckdb.connect()
    if args.base.startswith("http"):
        con.execute("install httpfs")
        con.execute("load httpfs")
    months = month_range(args.start, args.end, args.every)
    print(f"reading the footers of {len(months)} files from {args.base} ...", flush=True)
    result = timeline(con, args.base, months)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    errors = sorted(m for m, d in result["months"].items() if "error" in d)
    print(f"wrote {out}: {len(months) - len(errors)} months read, {len(errors)} unreadable" + (f" ({', '.join(errors[:5])}{' ...' if len(errors) > 5 else ''})" if errors else ""))
    for c in result["changes"]:
        parts = ([f"+{x}" for x in c["added"]] + [f"-{x}" for x in c["removed"]] + [f"{o}->{n} (case)" for o, n in c["case_renamed"]]
                 + [f"{col}: {old}->{new}" for col, old, new in c["retyped"]])
        print(f"{c['from']} -> {c['to']}: " + ", ".join(parts))
    if not result["changes"]:
        print("no schema changes between the months that could be read")
    return 0 if len(errors) < len(months) else 1


if __name__ == "__main__":
    sys.exit(main())
