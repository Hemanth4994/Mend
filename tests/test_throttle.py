"""The throttle is pure logic, so it is tested with a fake clock and a fake sleep: no waiting, no network."""
import pytest

from mend.throttle import DailyQuotaExceeded, Throttle, is_daily_quota, is_rate_limit, retry_hint


class Fake:
    """A clock that only moves when the throttle sleeps."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def make(**kw):
    f = Fake()
    return f, Throttle(clock=f.clock, sleep=f.sleep, **kw)


def test_calls_are_spaced_by_the_minimum_interval():
    f, t = make(min_interval=4.5)
    t.call(lambda: 1)
    t.call(lambda: 2)
    assert f.sleeps == [4.5]                       # the first call does not wait, the second waits the whole interval


def test_a_per_minute_limit_waits_the_hinted_time_plus_a_second_and_retries():
    f, t = make(min_interval=0)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 9.3s.")
        return "ok"

    assert t.call(flaky) == "ok"
    assert f.sleeps == [10.3] and t.retries == 1


def test_without_a_hint_the_default_wait_is_used():
    f, t = make(min_interval=0, default_wait=15.0)
    calls = iter([RuntimeError("429 too many requests"), "done"])

    def fn():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    assert t.call(fn) == "done" and f.sleeps == [16.0]


def test_a_daily_quota_stops_the_run_instead_of_retrying():
    f, t = make(min_interval=0)

    def fn():
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota metric GenerateRequestsPerDayPerProjectPerModel exceeded")

    with pytest.raises(DailyQuotaExceeded):
        t.call(fn)
    assert f.sleeps == [] and t.retries == 0


def test_other_errors_are_raised_at_once():
    f, t = make(min_interval=0)

    def fn():
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        t.call(fn)
    assert f.sleeps == []


def test_retries_are_limited_and_the_last_error_is_raised():
    f, t = make(min_interval=0, max_retries=2)
    seen = []

    def fn():
        seen.append(1)
        raise RuntimeError("429 try again later")

    with pytest.raises(RuntimeError, match="429"):
        t.call(fn)
    assert len(seen) == 3 and t.retries == 2       # the first try plus two retries


def test_message_helpers():
    assert is_rate_limit("Error 429") and is_rate_limit("RESOURCE_EXHAUSTED") and not is_rate_limit("500 internal")
    assert is_daily_quota("... PerDay ...") and is_daily_quota("daily limit") and not is_daily_quota("per minute")
    assert retry_hint("Please retry in 9.3s") == 9.3
    assert retry_hint("{'retryDelay': '9s'}") == 9.0
    assert retry_hint("no hint here") is None
