"""Ingestion: dlt loads each daily batch into the raw.orders table of a DuckDB file.

dlt is run exactly as a daily job would run it (append, one load per day), so schema evolution happens the way it does in
production: a renamed field becomes a new column, a changed type becomes a side column such as amount__v_text, and nested
objects are flattened to parent__child columns.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

import dlt  # noqa: E402

PIPELINE = "mend"
DATASET = "raw"
TABLE = "orders"


def make_pipeline(db_path: str | Path, work_dir: str | Path) -> "dlt.Pipeline":
    return dlt.pipeline(PIPELINE, destination=dlt.destinations.duckdb(str(db_path)), dataset_name=DATASET, pipelines_dir=str(work_dir))


def load_batch(pipe: "dlt.Pipeline", rows: list[dict]) -> str:
    """Load one day's records. Returns the load id."""
    pipe.run(rows, table_name=TABLE, write_disposition="append")
    return pipe.last_trace.last_normalize_info.loads_ids[0]
