import copy

import pytest

from mend import feed
from mend.cases import GROUPS
from mend.drifts import DRIFTS, apply_drifts, notes_for

ROWS = feed.day_batch(6)


def test_every_catalog_drift_is_used_by_exactly_one_case_group():
    used = [d for _, ids in GROUPS.values() for d in ids]
    assert sorted(used) == sorted(DRIFTS) and len(used) == len(set(used))


def test_drifts_do_not_change_their_input():
    before = copy.deepcopy(ROWS)
    for d in DRIFTS:
        apply_drifts([d], ROWS)
    assert ROWS == before


@pytest.mark.parametrize("drift", sorted(DRIFTS))
def test_each_drift_changes_the_records_and_keeps_the_row_count(drift):
    out = apply_drifts([drift], ROWS)
    assert len(out) == len(ROWS) and out != ROWS


def test_each_drift_has_a_release_note():
    assert all(notes_for([d]) for d in DRIFTS)


def test_paise_is_an_integer_hundred_times_the_amount():
    out = apply_drifts(["paise"], ROWS)
    assert all(isinstance(o["amount"], int) and o["amount"] == round(r["amount"] * 100) for o, r in zip(out, ROWS))


def test_ist_timestamps_are_five_and_a_half_hours_ahead():
    assert apply_drifts(["ts_ist_naive"], [{"placed_at": "2026-03-06T10:00:00Z"}])[0]["placed_at"] == "2026-03-06 15:30:00"


def test_drops_remove_exactly_the_named_columns():
    assert set(ROWS[0]) - set(apply_drifts(["drop_city_state"], ROWS)[0]) == {"city", "state"}
