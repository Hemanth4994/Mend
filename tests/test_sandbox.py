"""The answer key is only worth something if it is solvable and the scorer cannot be fooled. These tests build every case with real dlt."""
import pytest

from mend import gold as gold_mod
from mend import sandbox
from mend.cases import load_cases

CASES = load_cases()


def test_gold_has_every_order(gold):
    assert len(gold) == 8 * 150
    assert len({r[0] for r in gold}) == 1200


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_unrepaired_pipeline_and_gold_repair_behave_as_designed(case, cache, gold):
    db = sandbox.build_case(case, cache)
    baseline = sandbox.check_model(db, sandbox.baseline_sql(), gold)
    if case.expected == "noop":
        assert baseline.ok, f"{case.id}: dlt should absorb this change, but the baseline output differs"
        return
    assert not baseline.ok, f"{case.id}: this drift does not hurt the baseline, so there is nothing to repair"
    repair = gold_mod.repair_sql(case.drifts[0])
    if case.expected == "escalate":
        assert repair is None
    else:
        assert sandbox.check_model(db, repair, gold).ok, f"{case.id}: the gold repair does not reproduce the answer key"


def test_a_model_that_fills_in_constants_is_caught(cache, gold):
    case = next(c for c in CASES if c.id == "u06")           # city is gone
    db = sandbox.build_case(case, cache)
    invented = sandbox.baseline_sql().replace("    city,", "    'Delhi' as city,")
    assert not sandbox.check_model(db, invented, gold).ok


def test_sql_that_does_not_read_the_source_is_rejected(cache, gold):
    db = sandbox.build_case(CASES[0], cache)
    result = sandbox.check_model(db, "select 1 as order_id", gold)
    assert not result.ok and "source" in result.error


def test_sql_that_breaks_the_column_contract_is_an_error(cache, gold):
    db = sandbox.build_case(CASES[0], cache)
    broken = "select order_id from {{ source('raw', 'orders') }}"
    assert sandbox.check_model(db, broken, gold).error


def test_row_order_does_not_matter(gold):
    assert sandbox.same_rows(gold, list(reversed(gold)))
    assert not sandbox.same_rows(gold, gold[:-1])
