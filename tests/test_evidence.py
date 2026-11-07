"""What the agent is shown. Real dlt data and real dbt runs; no model. Built once per case and shared."""
import re

import pytest

from mend import evidence, sandbox
from mend.cases import load_cases

BY_ID = {c.id: c for c in load_cases()}


@pytest.fixture(scope="module")
def ev(cache):
    """Evidence for three kinds of change: a unit change inside a column, a removed column, and a harmless new column."""
    return {cid: evidence.build_evidence(sandbox.build_case(BY_ID[cid], cache), cache / "ev" / cid) for cid in ("v03", "u06", "c01")}


def test_schema_config_shows_only_the_raw_table(ev):
    text = evidence.render(ev["v03"], "schema")
    assert "raw.orders" in text and "amount" in text
    for section in ("Schema signals", "dbt checks", "Profile of each column", "Sample rows", "Release note"):
        assert section not in text


def test_evidence_config_adds_signals_profile_failing_checks_and_samples_but_not_the_note(ev):
    text = evidence.render(ev["v03"], "evidence", notes="THE NOTE")
    for section in ("Schema signals", "Profile of each column", "dbt checks on the current staging model", "Sample rows"):
        assert section in text
    assert "THE NOTE" not in text and "Release note" not in text
    assert any("value_between" in name and "amount" in name for name in ev["v03"].failing)


def test_notes_config_adds_the_release_note_or_says_there_is_none(ev):
    assert "Release note published by the source team:\n  THE NOTE" in evidence.render(ev["v03"], "notes", notes="THE NOTE")
    assert "none was published" in evidence.render(ev["v03"], "notes")


def test_the_profile_shows_a_unit_change_as_a_jump_in_the_median(ev):
    line = next(p for p in ev["v03"].profile if p.startswith("amount "))
    series = [float(x) for x in line.split("median by load: ")[1].split(" | ")[0].split()]
    assert len(series) == ev["v03"].loads and series[-1] / series[0] > 50


def test_a_removed_column_shows_up_as_emptied(ev):
    assert ev["u06"].signals["went_null"] == ["city"]
    assert "empty now: city" in evidence.render(ev["u06"], "evidence")


def test_a_harmless_change_has_no_failing_checks(ev):
    assert ev["c01"].failing == {} and ev["c01"].signals == {"added": ["coupon_code"], "went_null": []}


def test_samples_are_small_and_leave_out_empty_columns_and_dlt_bookkeeping(ev):
    for e in ev.values():
        assert 0 < len(e.samples) <= 3
        for row in e.samples:
            assert None not in row.values() and not any(k.startswith("_dlt") for k in row)


def test_the_agent_text_never_names_the_drift(ev):
    for config in ("schema", "evidence"):
        assert not re.search(r"paise|rename_|drop_", evidence.render(ev["v03"], config), re.I)


def test_unknown_config_is_rejected(ev):
    with pytest.raises(ValueError):
        evidence.render(ev["v03"], "everything")
