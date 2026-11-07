"""The orders feed as the source system emits it before any drift: the ground truth.

Every case in the evaluation starts from these same canonical batches. A drift changes how the feed is *written*
(a renamed field, a different unit) but never what the orders *are*, so the correct staging table for a drifted batch
is always the staging table of the canonical batch. That is how the evaluation gets its answer key without hand-written gold SQL.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

START = date(2026, 3, 1)
DAYS = 8                 # batches in every case
DRIFT_DAY = 5            # days 0-4 arrive in the canonical shape, days 5-7 arrive drifted
PER_DAY = 150
SEED = 7

COLUMNS = ["order_id", "customer_id", "placed_at", "amount", "currency", "status", "city", "state", "channel", "item_count"]
STATUSES = ["placed", "shipped", "delivered", "cancelled"]
_WEIGHTS = [0.20, 0.25, 0.45, 0.10]
CHANNELS = ["web", "app", "store"]
CITIES = [("Pune", "Maharashtra"), ("Mumbai", "Maharashtra"), ("Bengaluru", "Karnataka"), ("Mysuru", "Karnataka"),
          ("Chennai", "Tamil Nadu"), ("Coimbatore", "Tamil Nadu"), ("Hyderabad", "Telangana"), ("Delhi", "Delhi"),
          ("Kolkata", "West Bengal"), ("Ahmedabad", "Gujarat")]


def day_date(day: int) -> date:
    return START + timedelta(days=day)


def day_batch(day: int, seed: int = SEED, per_day: int = PER_DAY) -> list[dict]:
    """One day of orders. Deterministic: the same (day, seed) always gives the same rows."""
    rng = random.Random(f"{seed}-{day}")
    midnight = datetime.combine(day_date(day), datetime.min.time(), tzinfo=timezone.utc)
    rows = []
    for k in range(per_day):
        city, state = rng.choice(CITIES)
        placed = midnight + timedelta(seconds=rng.randrange(86400))
        amount = round(min(max(rng.lognormvariate(7.0, 0.8), 99.0), 49999.0), 2)
        rows.append({
            "order_id": f"ORD-{day * 1000 + k + 1:06d}",
            "customer_id": f"C{rng.randrange(1, 401):04d}",
            "placed_at": placed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "amount": amount,
            "currency": "INR",
            "status": rng.choices(STATUSES, _WEIGHTS)[0],
            "city": city,
            "state": state,
            "channel": rng.choice(CHANNELS),
            "item_count": rng.randint(1, 6),
        })
    return rows


def canonical_days(days: int = DAYS) -> list[list[dict]]:
    return [day_batch(d) for d in range(days)]
