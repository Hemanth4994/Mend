"""What the agent is shown about a change, in three amounts.

    schema    the raw table's columns and types
    evidence  schema + schema signals, a per-load profile of every column, the failing dbt checks, and sample rows
    notes     evidence + the source team's release note

Every number here is computed from the raw table and the dbt run, never from the answer key or the drift catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import signals

CONFIGS = ("schema", "evidence", "notes")
NUMERIC = ("DOUBLE", "BIGINT", "INTEGER", "DECIMAL", "HUGEINT", "FLOAT")


@dataclass
class Evidence:
    columns: dict[str, str]
    signals: dict
    profile: list[str]
    failing: dict[str, int]
    samples: list[dict]
    loads: int


def _loads(con) -> list[str]:
    return [r[0] for r in con.execute("select distinct _dlt_load_id from raw.orders order by 1").fetchall()]


def _profile_column(con, col: str, dtype: str, loads: list[str]) -> str:
    q = f'"{col}"'
    filled, series = [], []
    for lid in loads:
        n, k = con.execute(f"select count(*), count({q}) from raw.orders where _dlt_load_id = ?", [lid]).fetchone()
        filled.append(round(100 * k / n) if n else 0)
    parts = [f"{col} {dtype}", "filled % by load: " + " ".join(str(f) for f in filled)]
    if dtype.startswith(NUMERIC):
        for lid in loads:
            m = con.execute(f"select median({q}) from raw.orders where _dlt_load_id = ?", [lid]).fetchone()[0]
            series.append("-" if m is None else f"{m:.4g}")
        parts.append("median by load: " + " ".join(series))
    elif dtype.startswith("TIMESTAMP"):
        for lid in loads:
            lo, hi = con.execute(f"select min({q}), max({q}) from raw.orders where _dlt_load_id = ?", [lid]).fetchone()
            series.append("-" if lo is None else f"{lo:%m-%d %H:%M}..{hi:%m-%d %H:%M}")
        parts.append("min..max by load: " + " | ".join(series))
    else:
        for label, lid in (("first load", loads[0]), ("latest load", loads[-1])):
            vals = [r[0] for r in con.execute(f"select {q}, count(*) c from raw.orders where _dlt_load_id = ? and {q} is not null "
                                              f"group by 1 order by c desc, 1 limit 9", [lid]).fetchall()]
            if vals:
                shown = vals if len(vals) <= 8 else vals[:3] + ["..."]
                parts.append(f"{label} values: " + ", ".join(str(v) for v in shown) + (" (many distinct)" if len(vals) > 8 else ""))
    return " | ".join(parts)


def build_evidence(db: str | Path, work: str | Path) -> Evidence:
    """Everything the agent may be shown. `work` is a scratch folder for the dbt run."""
    cols = {c: t for c, t in signals.raw_columns(db).items() if not c.startswith("_dlt_")}
    sig = signals.schema_signals(db)
    signals.dbt_build(db, work)
    failing = signals.dbt_failures(work)
    con = duckdb.connect(str(db), read_only=True)
    try:
        con.execute("SET TimeZone='UTC'")
        loads = _loads(con)
        profile = [_profile_column(con, c, t, loads) for c, t in cols.items()]
        latest = con.execute("select * exclude(_dlt_load_id, _dlt_id) from raw.orders where _dlt_load_id = ? limit 3", [loads[-1]])
        names = [d[0] for d in latest.description]
        samples = [{n: (f"{v:%Y-%m-%d %H:%M:%S}" if hasattr(v, "year") else v) for n, v in zip(names, row) if v is not None}
                   for row in latest.fetchall()]
    finally:
        con.close()
    return Evidence(cols, sig, profile, failing, samples, len(loads))


def render(ev: Evidence, config: str, notes: str = "") -> str:
    """The text the agent sees for a configuration. `schema` is the least, `notes` the most."""
    if config not in CONFIGS:
        raise ValueError(f"config must be one of {CONFIGS}")
    out = ["Raw table raw.orders (all loads so far, oldest first):\n" + "\n".join(f"  {c} {t}" for c, t in ev.columns.items())]
    if config in ("evidence", "notes"):
        s = ev.signals
        out.append("Schema signals for the latest load:\n"
                   f"  new columns: {', '.join(s['added']) or 'none'}\n"
                   f"  columns that were filled before and are empty now: {', '.join(s['went_null']) or 'none'}")
        out.append(f"Profile of each column across the {ev.loads} loads (oldest first):\n" + "\n".join("  " + p for p in ev.profile))
        failing = "\n".join(f"  {n}: {k} failing rows" for n, k in sorted(ev.failing.items())) or "  none"
        out.append("dbt checks on the current staging model:\n" + failing)
        out.append("Sample rows from the latest load (empty columns left out):\n" + "\n".join(f"  {r}" for r in ev.samples))
    if config == "notes":
        out.append(f"Release note published by the source team:\n  {notes or 'none was published'}")
    return "\n\n".join(out)
