"""The production gate. It must reject bad patches using only what a pipeline has (no answer key) and accept a good one."""
import pytest

from mend import feed, gold as gold_mod, sandbox, validate
from mend.cases import load_cases

BY_ID = {c.id: c for c in load_cases()}
BASE = sandbox.baseline_sql()


# ---- static checks (no database) ------------------------------------------------------------------------------------------

def test_the_current_model_passes_the_static_checks():
    assert validate.static_problems(BASE) == []


def test_a_cte_wrapped_model_is_allowed():
    cols = ", ".join(sandbox.CONTRACT)
    sql = "with r as (select * from {{ source('raw', 'orders') }}) select " + cols + " from r"
    assert validate.static_problems(sql) == []


@pytest.mark.parametrize("sql, expect", [
    ("select 1", "source"),
    ("drop table {{ source('raw', 'orders') }}", "exactly one SELECT"),
    (BASE.rstrip() + ";\nselect 2", "exactly one SELECT"),
    ("select * from {{ source('raw', 'orders') }}", "SELECT *"),
    (BASE.replace("    city,", "    'Delhi' as city,"), "constant"),
    (BASE.rstrip() + " join main.other on true", "may only read"),
    ("select order_id from {{ source('raw', 'orders') }}", "exactly these columns"),
    ("select ((( from {{ source('raw', 'orders') }}", "does not parse"),
])
def test_static_problems(sql, expect):
    problems = validate.static_problems(sql)
    assert problems and any(expect in p for p in problems), problems


# ---- the whole gate on real data ------------------------------------------------------------------------------------------

def gate(cache, case_id, sql):
    db = sandbox.build_case(BY_ID[case_id], cache)
    return validate.validate_patch(db, sql, BASE, cache / "gate" / case_id, feed.DRIFT_DAY)


def test_a_correct_repair_passes_every_check(cache):
    v = gate(cache, "s01", gold_mod.repair_sql("rename_amount"))
    assert v.ok and all(v.checks.values()) and v.problems == []


def test_leaving_the_model_alone_after_a_rename_fails_the_gate(cache):
    v = gate(cache, "s01", BASE)
    assert not v.ok and not v.checks["filled"] and not v.checks["dbt"]


def test_a_patch_that_changes_old_rows_is_caught_by_the_history_check(cache):
    wrong = BASE.replace("cast(amount as decimal(12, 2))", "cast(amount / 100.0 as decimal(12, 2))")
    v = gate(cache, "v03", wrong)                          # divides every row, including the rows loaded before the change
    assert not v.ok and not v.checks["history"]


def test_a_patch_that_does_not_run_is_reported_without_running_dbt(cache):
    v = gate(cache, "s01", BASE.replace("    channel,", "    no_such_column as channel,"))
    assert not v.ok and v.checks.get("runs") is False and "does not run" in v.problems[0]


# ---- 'no change needed' ---------------------------------------------------------------------------------------------------

def test_no_change_is_believable_only_when_nothing_fails_and_nothing_went_empty():
    assert validate.validate_no_change({}, []).ok
    bad = validate.validate_no_change({"not_null_stg_orders_amount": 3}, [])
    assert not bad.ok and "not_null_stg_orders_amount (3 failing rows)" in bad.problems[0]
    gone = validate.validate_no_change({}, ["city"])
    assert not gone.ok and "city" in gone.problems[0]
