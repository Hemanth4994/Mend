"""Save a few real rows from the NYC TLC yellow taxi files around the schema changes found by nyc_schema_timeline.py.

    python explore/nyc_sample.py                      # 12 months, 1000 rows each -> results/nyc/samples/
    python explore/nyc_sample.py --rows 500 --pause 3
    python explore/nyc_sample.py --months 2025-01 2024-12

The schema timeline reads only footers; to see what a change does to a pipeline we need real values (for example whether a text timestamp becomes a
real one). DuckDB reads just the start of each file (the first row group, a few to a few tens of MB), keeps the file's own column types, and writes
a small Parquet file, so later steps can run offline. This is exploration, not part of the evaluated code and not part of the test suite, and it
lives in explore/ because scripts/ is guarded by frozen-v1. A month whose sample already exists is skipped, so a run can be repeated safely; it
waits --pause seconds between downloads and stops after --give-up-after failures in a row (a server that refuses us is not helped by more requests).
The data is the public NYC Taxi and Limousine Commission trip record data.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DEFAULT_BASE = "https://d37ci6vzurychx.cloudfront.net/trip-data"
OUT = Path(__file__).resolve().parent.parent / "results" / "nyc" / "samples"
# The month before and the month after each real change found in the timeline (see results/nyc/yellow_schema_timeline.json).
DEFAULT_MONTHS = [
    "2009-12", "2010-01",   # snake_case rename wave (Trip_Pickup_DateTime -> pickup_datetime, Fare_Amt -> fare_amount, ...)
    "2010-12", "2011-01",   # tpep_ names, TIMESTAMP types, location IDs instead of coordinates
    "2018-06", "2018-07",   # passenger_count and RatecodeID BIGINT -> DOUBLE
    "2023-01", "2023-02",   # airport_fee -> Airport_fee, integer widths, passenger_count DOUBLE -> BIGINT
    "2024-12", "2025-01",   # cbd_congestion_fee added
    "2026-05", "2026-06",   # request_source added
]


def sample_path(out: Path, month: str) -> Path:
    return out / f"yellow_tripdata_{month}_sample.parquet"


def save_sample(con, base: str, month: str, rows: int, out: Path) -> dict:
    """Write the first `rows` rows of one month to `out`; return {'rows': n} or {'error': ...}."""
    src = f"{base}/yellow_tripdata_{month}.parquet".replace("'", "''")
    dest = sample_path(out, month)
    try:
        con.execute(f"copy (select * from read_parquet('{src}') limit {int(rows)}) to '{str(dest).replace(chr(39), chr(39) * 2)}' (format parquet)")
        n = con.execute("select count(*) from read_parquet(?)", [str(dest)]).fetchone()[0]
    except Exception as exc:                                              # noqa: BLE001 - report it, do not stop the whole run
        dest.unlink(missing_ok=True)
        return {"error": str(exc).splitlines()[0][:200]}
    return {"rows": n}


def run(con, base: str, months: list[str], rows: int, out: Path, pause: float = 0.0, give_up_after: int = 5) -> dict:
    """month -> {'rows': n} | {'skipped': 'exists'} | {'error': ...}. Stops after `give_up_after` failures in a row."""
    out.mkdir(parents=True, exist_ok=True)
    result, streak = {}, 0
    for m in months:
        if sample_path(out, m).exists():
            result[m] = {"skipped": "exists"}
            continue
        if give_up_after and streak >= give_up_after:
            result[m] = {"error": f"not tried: stopped after {give_up_after} months in a row failed"}
            continue
        result[m] = save_sample(con, base, m, rows, out)
        streak = streak + 1 if "error" in result[m] else 0
        if pause:
            time.sleep(pause)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--months", nargs="+", default=DEFAULT_MONTHS, help="YYYY-MM, one or more")
    ap.add_argument("--rows", type=int, default=1000)
    ap.add_argument("--base", default=DEFAULT_BASE, help="where yellow_tripdata_YYYY-MM.parquet lives (a URL or a folder)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--pause", type=float, default=2.0, help="seconds to wait after each download")
    ap.add_argument("--give-up-after", type=int, default=5, help="stop after this many months in a row fail (0: never stop)")
    args = ap.parse_args(argv)

    import duckdb
    con = duckdb.connect()
    if args.base.startswith("http"):
        con.execute("install httpfs")
        con.execute("load httpfs")
    print(f"saving up to {args.rows} rows for {len(args.months)} months from {args.base} ...", flush=True)
    result = run(con, args.base, args.months, args.rows, Path(args.out), args.pause, args.give_up_after)
    for m, r in result.items():
        print(f"  {m}: " + (f"{r['rows']} rows" if "rows" in r else r.get("skipped", r.get("error"))))
    failed = [m for m, r in result.items() if "error" in r]
    if failed:
        print("some months were not saved: wait a few minutes and run the same command again (months already saved are skipped)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
