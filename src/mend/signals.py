"""Drift signals read from the raw table, and the result of a dbt build: the evidence that something changed.

Schema signals come from the raw table itself (no model, no tests), the way a data engineer would first notice a feed change:
a column that did not exist before, or a column that stopped being filled.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import duckdb

from . import feed

ROOT = Path(__file__).resolve().parents[2]
DBT_DIR = ROOT / "dbt" / "mend_dbt"


def raw_columns(db: str | Path) -> dict[str, str]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("select column_name, data_type from information_schema.columns "
                           "where table_schema='raw' and table_name='orders' order by ordinal_position").fetchall()
    finally:
        con.close()
    return {n: t for n, t in rows}


def schema_signals(db: str | Path) -> dict:
    """added: columns the contract does not know. went_null: contract columns that were filled before the latest load and are not now."""
    cols = raw_columns(db)
    known = set(feed.COLUMNS)
    added = [c for c in cols if c not in known and not c.startswith("_dlt_")]
    con = duckdb.connect(str(db), read_only=True)
    try:
        latest = con.execute("select max(_dlt_load_id) from raw.orders").fetchone()[0]
        went_null = []
        for c in feed.COLUMNS:
            if c not in cols:
                continue
            before = con.execute(f'select avg(("{c}" is not null)::int) from raw.orders where _dlt_load_id <> ?', [latest]).fetchone()[0]
            now = con.execute(f'select avg(("{c}" is not null)::int) from raw.orders where _dlt_load_id = ?', [latest]).fetchone()[0]
            if (before or 0) > 0.5 and (now or 0) < 0.05:
                went_null.append(c)
    finally:
        con.close()
    return {"added": added, "went_null": went_null}


def dbt_build(db: str | Path, work: str | Path, model_sql: str | None = None) -> dict[str, str]:
    """Run `dbt build` (models and tests) on a copy of the database. Returns {node name: status}; 'fail' and 'error' mean a test or model failed.

    With model_sql, the project is copied first and stg_orders.sql is replaced, so a proposed repair is tested by exactly the same
    dbt tests as the real pipeline, without touching the real project."""
    from dbt.cli.main import dbtRunner

    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    copy = work / "mend.duckdb"
    shutil.copy(db, copy)
    project = DBT_DIR
    if model_sql is not None:
        project = work / "project"
        shutil.copytree(DBT_DIR, project, ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"))
        (project / "models" / "staging" / "stg_orders.sql").write_text(model_sql, encoding="utf-8")
    os.environ["MEND_DB"] = str(copy)
    os.environ["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    res = dbtRunner().invoke(["--quiet", "build", "--project-dir", str(project), "--profiles-dir", str(project),
                              "--target-path", str(work / "target"), "--log-path", str(work / "logs")])
    if res.exception is not None and res.result is None:
        raise RuntimeError(f"dbt could not run: {res.exception}")
    return {r.node.name: str(r.status).split(".")[-1].lower() for r in res.result.results}


def failed_checks(results: dict[str, str]) -> list[str]:
    return sorted(n for n, s in results.items() if s in ("fail", "error"))


def dbt_failures(work: str | Path) -> dict[str, int]:
    """{check name: number of failing rows} for the checks that failed in the last dbt_build run in `work`."""
    import json
    data = json.loads((Path(work) / "target" / "run_results.json").read_text(encoding="utf-8"))
    return {r["unique_id"].split(".")[2]: int(r.get("failures") or 0) for r in data["results"] if r["status"] in ("fail", "error")}
