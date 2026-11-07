from mend import feed


def test_batches_are_deterministic():
    assert feed.day_batch(3) == feed.day_batch(3)
    assert feed.day_batch(3) != feed.day_batch(4)


def test_canonical_shape():
    rows = feed.day_batch(0)
    assert len(rows) == feed.PER_DAY
    assert all(list(r) == feed.COLUMNS for r in rows)
    assert {r["status"] for r in feed.canonical_days()[0]} <= set(feed.STATUSES)


def test_order_ids_are_unique_across_days():
    ids = [r["order_id"] for day in feed.canonical_days() for r in day]
    assert len(ids) == len(set(ids)) == feed.DAYS * feed.PER_DAY


def test_amounts_stay_inside_the_range_the_dbt_test_allows():
    amounts = [r["amount"] for day in feed.canonical_days() for r in day]
    assert 1 <= min(amounts) and max(amounts) <= 100000
