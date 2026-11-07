from collections import Counter

from mend.cases import CASES_FILE, GROUPS, generate, load_cases


def test_the_frozen_file_matches_the_generator():
    assert load_cases() == generate()


def test_sixty_cases_split_evenly_with_every_group_in_both_splits():
    cases = load_cases()
    assert len(cases) == 60 and len({c.id for c in cases}) == 60
    assert Counter(c.split for c in cases) == {"dev": 30, "test": 30}
    for group in GROUPS:
        assert Counter(c.split for c in cases if c.group == group).most_common(2)[0][1] == len([c for c in cases if c.group == group]) // 2


def test_expected_response_follows_the_group():
    expect = {"structural": "repair", "value": "repair", "compound": "repair", "control": "noop", "unfixable": "escalate"}
    assert all(c.expected == expect[c.group] for c in load_cases())


def test_cases_file_is_plain_utf8_json():
    assert CASES_FILE.read_text(encoding="utf-8").startswith("[")
