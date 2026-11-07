"""The production gate: can a proposed repair be trusted without a person looking? It never uses the answer key.

A real pipeline has no answer key, so the gate can only use what a pipeline has:

    1. static   one SELECT, reads only raw.orders, no SELECT *, no output column that is a constant, the contract's columns
    2. runs     the model executes and produces the contract's columns and types
    3. history  rows loaded before the change come out exactly as the current model produced them
    4. filled   every column is filled in the latest loads about as often as it was before
    5. dbt      every dbt check passes on the patched model

The evaluation then measures how often a patch that passes all five is still wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from . import sandbox, signals
from .sandbox import CONTRACT, SOURCE_REF, StageError


@dataclass
class Validation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


def static_problems(model_sql: str) -> list[str]:
    try:
        rendered = sandbox.render(model_sql)
    except StageError as e:
        return [str(e)]
    try:
        trees = [t for t in sqlglot.parse(rendered, read="duckdb") if t is not None]
    except sqlglot.errors.SqlglotError as e:
        return [f"the SQL does not parse: {str(e).splitlines()[0]}"]
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        return ["the model must be exactly one SELECT statement"]
    tree = trees[0]
    problems = []
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if t.name in ctes:
            continue
        if (t.db, t.name) != ("raw", "orders"):
            problems.append(f"the model may only read {{{{ source('raw', 'orders') }}}}, not {t.sql()}")
    if any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in tree.expressions):
        problems.append("SELECT * is not allowed; list the contract columns")
    names = []
    for p in tree.expressions:
        names.append(p.alias_or_name)
        if not isinstance(p, exp.Star) and not list(p.find_all(exp.Column)):
            problems.append(f"output column {p.alias_or_name} is a constant; a repair may not invent values")
    if sorted(names) != sorted(CONTRACT):
        problems.append(f"the model must output exactly these columns: {', '.join(CONTRACT)}")
    return problems


def _fill(rows: list[tuple]) -> list[float]:
    n = len(rows) or 1
    return [sum(r[i] is not None for r in rows) / n for i in range(len(CONTRACT))]


def validate_patch(db: str | Path, model_sql: str, baseline_sql: str, work: str | Path, history_loads: int) -> Validation:
    v = Validation(ok=False)
    v.problems += static_problems(model_sql)
    v.checks["static"] = not v.problems
    if v.problems:
        return v
    loads = sandbox.load_ids(db)
    old, new = loads[:history_loads], loads[history_loads:]
    try:
        rows_old = sandbox.stage(db, model_sql, old)
        rows_new = sandbox.stage(db, model_sql, new)
    except StageError as e:
        v.problems.append(f"the model does not run: {e}")
        v.checks["runs"] = False
        return v
    v.checks["runs"] = True

    v.checks["history"] = sandbox.same_rows(sandbox.stage(db, baseline_sql, old), rows_old)
    if not v.checks["history"]:
        v.problems.append("rows loaded before the change no longer come out the same as with the current model")

    before, after = _fill(rows_old), _fill(rows_new)
    empty = [c for c, b, a in zip(CONTRACT, before, after) if a < b - 0.02]
    v.checks["filled"] = not empty
    if empty:
        v.problems.append("these columns are filled much less often in the latest loads than before: " + ", ".join(empty))

    results = signals.dbt_build(db, Path(work) / "gate", model_sql)
    failing = signals.dbt_failures(Path(work) / "gate") if signals.failed_checks(results) else {}
    v.checks["dbt"] = not failing and results.get("stg_orders") == "success"
    if not v.checks["dbt"]:
        v.problems.append("dbt checks fail on the patched model: " + ", ".join(f"{n} ({k} failing rows)" for n, k in sorted(failing.items()))
                          if failing else "dbt could not build the patched model")
    v.ok = all(v.checks.values())
    return v


def validate_no_change(failing: dict[str, int], went_null: list[str]) -> Validation:
    """'No change needed' is believable only if nothing fails and no column went empty."""
    v = Validation(ok=not failing and not went_null, checks={"dbt": not failing, "filled": not went_null})
    if failing:
        v.problems.append("dbt checks fail on the current model: " + ", ".join(f"{n} ({k} failing rows)" for n, k in sorted(failing.items())))
    if went_null:
        v.problems.append("these columns are empty in the latest load: " + ", ".join(went_null))
    return v
