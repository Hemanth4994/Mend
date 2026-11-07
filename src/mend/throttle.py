"""Pacing and retry for a rate-limited model API (the Gemini free tier allows about 15 requests a minute).

Per-minute limits are waited out and retried. A daily quota cannot be waited out, so it stops the run with a clear error
instead of burning the remaining questions on failures.
"""
from __future__ import annotations

import re
import time


class DailyQuotaExceeded(RuntimeError):
    """The model API refused because the daily quota is used up. Stop and resume tomorrow."""


def is_rate_limit(message: str) -> bool:
    return "429" in message or "RESOURCE_EXHAUSTED" in message


def is_daily_quota(message: str) -> bool:
    low = message.lower()
    return "perday" in low or "per day" in low or "daily" in low


def retry_hint(message: str) -> float | None:
    """Seconds the server asked us to wait, from 'Please retry in 9.3s' or "retryDelay': '9s'"."""
    m = re.search(r"retry in ([\d.]+)\s*s", message, re.I) or re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+)s", message)
    return float(m.group(1)) if m else None


class Throttle:
    def __init__(self, min_interval: float = 4.5, max_retries: int = 5, default_wait: float = 15.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.min_interval, self.max_retries, self.default_wait = min_interval, max_retries, default_wait
        self._clock, self._sleep = clock, sleep
        self._last: float | None = None
        self.retries = 0                       # how many 429s were waited out, reported with the results

    def _pace(self) -> None:
        if self._last is not None:
            wait = self._last + self.min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def call(self, fn):
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                return fn()
            except Exception as exc:                                        # the SDKs raise their own error classes
                msg = str(exc)
                if not is_rate_limit(msg):
                    raise
                if is_daily_quota(msg):
                    raise DailyQuotaExceeded(msg.splitlines()[0][:300]) from exc
                if attempt == self.max_retries:
                    raise
                self.retries += 1
                hint = retry_hint(msg)
                self._sleep((hint if hint is not None else self.default_wait) + 1.0)
        raise AssertionError("unreachable")
