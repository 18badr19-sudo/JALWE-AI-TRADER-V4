"""Retry only Telegram requests explicitly rejected by a rate limit.

An ambiguous timeout may follow a delivered message, so it is never resent here.
"""
import json
import math
import time
from urllib.error import HTTPError


class TelegramRateLimit(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f'Telegram rate limited; retry after {retry_after:g} seconds')


def open_telegram(request, *, timeout, opener, sleep=None):
    """One retry after a confirmed 429, with at most ten seconds of waiting."""
    sleep = sleep or time.sleep
    for attempt in range(2):
        try:
            return opener(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code != 429:
                raise
            delay = 5.0
            try:
                body = json.loads(exc.read(65536))
                value = body.get('parameters', {}).get('retry_after')
                if type(value) in (int, float) and math.isfinite(value) and value > 0:
                    delay = float(value)
            except (ValueError, TypeError, AttributeError):
                pass
            finally:
                exc.close()
            if attempt or delay > 10:
                raise TelegramRateLimit(delay) from None
            sleep(delay)
