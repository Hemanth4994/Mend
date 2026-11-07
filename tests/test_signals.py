import pytest

from mend import sandbox, signals
from mend.cases import load_cases

BY_ID = {c.id: c for c in load_cases()}


def test_schema_signals_report_new_and_emptied_columns(cache):
    s = signals.schema_signals(sandbox.build_case(BY_ID["s01"], cache))      # amount renamed total_amount
    assert s["added"] == ["total_amount"] and s["went_null"] == ["amount"]
    assert signals.schema_signals(sandbox.build_case(BY_ID["c01"], cache)) == {"added": ["coupon_code"], "went_null": []}


def test_a_value_change_inside_a_column_leaves_no_schema_signal(cache):
    assert signals.schema_signals(sandbox.build_case(BY_ID["v03"], cache)) == {"added": [], "went_null": []}     # paise


def test_dbt_passes_on_a_harmless_change_and_fails_on_a_value_change(cache):
    ok = signals.dbt_build(sandbox.build_case(BY_ID["c01"], cache), cache / "dbt" / "c01")
    assert signals.failed_checks(ok) == [] and ok["stg_orders"] == "success"
    bad = signals.dbt_build(sandbox.build_case(BY_ID["v03"], cache), cache / "dbt" / "v03")
    assert signals.failed_checks(bad) == ["value_between_stg_orders_amount__100000__1"]
