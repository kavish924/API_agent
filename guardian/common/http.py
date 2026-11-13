"""HTTP requests with retry and backoff (spec 00 F0.9).

Used for GitHub, PyPI and provider pages. Honours `Retry-After`, GitHub's primary
rate limit (`x-ratelimit-reset`) and its secondary rate limit (a 403 that mentions it).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from guardian.common.log import get_logger

log = get_logger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 120.0


def is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if "retry-after" in response.headers:
        return True
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "rate limit" in response.text.lower()


def should_retry(response: httpx.Response) -> bool:
    return response.status_code in RETRY_STATUS or is_rate_limited(response)


def retry_delay(
    response: httpx.Response | None,
    attempt: int,
    policy: RetryPolicy,
    *,
    now: Callable[[], float] = time.time,
    rng: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before the next attempt. `attempt` counts from 1."""
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), policy.max_delay)
            except ValueError:
                pass
        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None and response.headers.get("x-ratelimit-remaining") == "0":
            try:
                return min(max(float(reset) - now(), 0.0), policy.max_delay)
            except ValueError:
                pass
    backoff = min(policy.base_delay * 2.0 ** (attempt - 1), policy.max_delay)
    return backoff * (0.5 + rng() / 2)  # jitter: 50% to 100% of the backoff


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    rng: Callable[[], float] = random.random,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying on rate limits, 5xx and transport errors.

    Returns the last response when attempts run out, so the caller sees the real
    status. A transport error on the last attempt is raised.
    """
    policy = policy or RetryPolicy()
    attempt = 1
    while True:
        response: httpx.Response | None = None
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= policy.max_attempts:
                raise
            reason = type(exc).__name__
        else:
            if not should_retry(response) or attempt >= policy.max_attempts:
                return response
            reason = f"status {response.status_code}"
        delay = retry_delay(response, attempt, policy, now=now, rng=rng)
        log.warning(
            "retrying request",
            extra={"fields": {"url": url, "attempt": attempt, "reason": reason, "delay": delay}},
        )
        sleep(delay)
        attempt += 1
