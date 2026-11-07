"""Build a case's raw data with dlt, run a staging model over it, and compare the result with the answer key.

Nothing here calls a model. The agent (later) only proposes SQL; this module is what judges it.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import feed
from .cases import Case
from .drifts import apply_drifts
from .pipeline import load_batch, make_pipeline

ROOT = Path(__file__).resolve().parents[2]
STAGING_MODEL = ROOT / "dbt" / "mend_dbt" / "models" / "staging" / "stg_orders.sql"
SOURCE_REF = re.compile(r"\{\{\s*source\(\s*['\"]raw['\"]\s*,\s*['\"]orders['\"]\s*\)\s*\}\}")
CONTRACT = ["order_id", "customer_id", "placed_at", "amount", "currency", "status", "city", "state", "channel", "item_count"]


class StageError(Exception):
    """The staging SQL did not run, or did not produce the staging contract."""


def baseline_sql() -> str:
    return STAGING_MODEL.read_text(encoding="utf-8")


def render(model_sql: str) -> str:
    """Replace the dbt source() reference with the real table, the way dbt would."""
    if not SOURCE_REF.search(model_sql):
        raise StageError("the model must read from {{ source('raw', 'orders') }}")
    return SOURCE_REF.sub("raw.orders", model_sql)


def load_ids(db_path: str | Path) -> list[str]:
    """The dlt load ids in the raw table, oldest first. One load is one daily batch."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return [r[0] for r in con.execute("select distinct _dlt_load_id from raw.orders order by 1").fetchall()]
    finally:
        con.close()


def _open(db_path: str | Path, loads: list[str] | None):
    """A connection that sees raw.orders, or only the rows of the given loads."""
    if loads is None:
        return duckdb.connect(str(db_path), read_only=True)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db_path}' AS src (READ_ONLY)")
    con.execute("CREATE SCHEMA raw")
    ids = ", ".join("'" + load.replace("'", "") + "'" for load in loads)
    con.execute(f"CREATE VIEW raw.orders AS SELECT * FROM src.raw.orders WHERE _dlt_load_id IN ({ids})")
    return con


def stage(db_path: str | Path, model_sql: str, loads: list[str] | None = None) -> list[tuple]:
    """Run a staging model over a raw database and return its rows in a comparable form. With `loads`, only those loads' rows are seen."""
    body = render(model_sql).strip().rstrip(";")
    con = _open(db_path, loads)
    try:
        con.execute("SET TimeZone='UTC'")
        sql = (f"select order_id, customer_id, cast(epoch(placed_at) as bigint) as placed_epoch, "
               f"cast(cast(amount as decimal(12,2)) as varchar) as amount, currency, status, city, state, channel, "
               f"cast(item_count as bigint) as item_count from ({body}) as m")
        try:
            return [tuple(r) for r in con.execute(sql).fetchall()]
        except Exception as e:  # DuckDB raises several error types; all mean the same thing here
            raise StageError(f"{type(e).__name__}: {e}") from e
    finally:
        con.close()


def _key(row: tuple) -> tuple:
    return tuple("" if v is None else str(v) for v in row)


def same_rows(gold: list[tuple], got: list[tuple]) -> bool:
    return sorted(gold, key=_key) == sorted(got, key=_key)


def diff_summary(gold: list[tuple], got: list[tuple], limit: int = 3) -> dict:
    """How the result differs from the answer key, for failure analysis. Not used for scoring."""
    g, r = sorted(gold, key=_key), sorted(got, key=_key)
    bad_cols = [c for i, c in enumerate(CONTRACT) if any(a[i] != b[i] for a, b in zip(g, r))] if len(g) == len(r) else ["row_count"]
    examples = [{"want": a, "got": b} for a, b in zip(g, r) if a != b][:limit]
    return {"rows_gold": len(g), "rows_got": len(r), "columns_differ": bad_cols, "examples": examples}


# ---- building the raw data ---------------------------------------------------------------------------------------------

def build_history(cache: Path) -> Path:
    """Days 0 .. DRIFT_DAY-1 in the canonical shape. Built once and copied for every case."""
    hist = cache / "history"
    if (hist / "mend.duckdb").exists():
        return hist
    if hist.exists():
        shutil.rmtree(hist)
    hist.mkdir(parents=True)
    pipe = make_pipeline(hist / "mend.duckdb", hist / "dlt")
    for d in range(feed.DRIFT_DAY):
        load_batch(pipe, feed.day_batch(d))
    return hist


def build_case(case: Case, cache: Path) -> Path:
    """The raw database the pipeline sees on the day the source changes: history plus the drifted days."""
    out = cache / "cases" / case.id
    db = out / "mend.duckdb"
    if db.exists():
        return db
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(build_history(cache), out)
    pipe = make_pipeline(db, out / "dlt")
    for d in range(feed.DRIFT_DAY, feed.DAYS):
        load_batch(pipe, apply_drifts(case.drifts, feed.day_batch(d)))
    return db


def build_gold(cache: Path) -> list[tuple]:
    """The answer key: the baseline staging model over the undrifted feed, all days."""
    path = cache / "gold" / "gold.json"
    if path.exists():
        return [tuple(r) for r in json.loads(path.read_text())]
    out = cache / "gold"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(build_history(cache), out)
    pipe = make_pipeline(out / "mend.duckdb", out / "dlt")
    for d in range(feed.DRIFT_DAY, feed.DAYS):
        load_batch(pipe, feed.day_batch(d))
    rows = stage(out / "mend.duckdb", baseline_sql())
    path.write_text(json.dumps(rows))
    return rows


@dataclass
class Check:
    ok: bool
    error: str | None
    rows: int
    diff: dict | None


def check_model(db: Path, model_sql: str, gold: list[tuple]) -> Check:
    """Run a model over a case database and compare it with the answer key."""
    try:
        rows = stage(db, model_sql)
    except StageError as e:
        return Check(False, str(e), 0, None)
    if same_rows(gold, rows):
        return Check(True, None, len(rows), None)
    return Check(False, None, len(rows), diff_summary(gold, rows))
