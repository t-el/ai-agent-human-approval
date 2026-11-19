"""Retry with exponential backoff + jitter for transient failures (timeouts, 429, 5xx)."""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import httpx

log = logging.getLogger("agent.retry")
T = TypeVar("T")


class TransientError(Exception):
    pass


def check(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientError(f"{what} HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp


def with_retry(fn: Callable[[], T], *, step: str, attempts: int = 3, base_delay: float = 1.0,
               sleep: Callable[[float], None] = time.sleep) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (TransientError, httpx.TimeoutException, httpx.TransportError, ConnectionError) as exc:
            if attempt >= attempts:
                log.error("retries exhausted", extra={"step": step, "attempt": attempt, "error": str(exc)})
                raise
            delay = base_delay * 2 ** (attempt - 1) * (0.5 + random.random() / 2)
            log.warning("transient failure, retrying",
                        extra={"step": step, "attempt": attempt, "delay_s": round(delay, 2), "error": str(exc)})
            sleep(delay)
    raise AssertionError("unreachable")
