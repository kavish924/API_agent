"""Retry and backoff. Covers F0.9."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from guardian.common.http import RetryPolicy, is_rate_limited, request_with_retry, retry_delay

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _sequence(*responses: httpx.Response | Exception) -> tuple[Handler, list[httpx.Request]]:
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler, seen


def test_success_is_returned_without_sleeping() -> None:
    handler, seen = _sequence(httpx.Response(200, json={"ok": True}))
    slept: list[float] = []
    response = request_with_retry(_client(handler), "GET", "https://x.test/a", sleep=slept.append)
    assert response.status_code == 200
    assert len(seen) == 1 and slept == []


def test_retry_after_is_honoured() -> None:
    handler, seen = _sequence(
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)
    )
    slept: list[float] = []
    response = request_with_retry(_client(handler), "GET", "https://x.test/a", sleep=slept.append)
    assert response.status_code == 200
    assert slept == [7.0]
    assert len(seen) == 2


def test_github_secondary_rate_limit_is_retried() -> None:
    limited = httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."})
    handler, seen = _sequence(limited, httpx.Response(200))
    slept: list[float] = []
    response = request_with_retry(
        _client(handler), "GET", "https://api.github.test/x", sleep=slept.append, rng=lambda: 1.0
    )
    assert response.status_code == 200
    assert slept == [1.0]  # first backoff step, no jitter reduction


def test_primary_rate_limit_waits_until_reset() -> None:
    limited = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}
    )
    handler, _ = _sequence(limited, httpx.Response(200))
    slept: list[float] = []
    request_with_retry(
        _client(handler), "GET", "https://api.github.test/x", sleep=slept.append, now=lambda: 1000.0
    )
    assert slept == [30.0]


def test_plain_403_is_not_retried() -> None:
    handler, seen = _sequence(httpx.Response(403, json={"message": "Resource not accessible"}))
    response = request_with_retry(_client(handler), "GET", "https://api.github.test/x")
    assert response.status_code == 403
    assert len(seen) == 1


def test_server_errors_back_off_exponentially_then_give_up() -> None:
    handler, seen = _sequence(*[httpx.Response(503) for _ in range(3)])
    slept: list[float] = []
    response = request_with_retry(
        _client(handler),
        "GET",
        "https://x.test/a",
        policy=RetryPolicy(max_attempts=3, base_delay=2.0),
        sleep=slept.append,
        rng=lambda: 1.0,
    )
    assert response.status_code == 503  # the caller sees the real status
    assert len(seen) == 3
    assert slept == [2.0, 4.0]


def test_transport_errors_are_retried_then_raised() -> None:
    handler, seen = _sequence(httpx.ConnectError("down"), httpx.Response(200))
    response = request_with_retry(_client(handler), "GET", "https://x.test/a", sleep=lambda _: None)
    assert response.status_code == 200 and len(seen) == 2

    handler, _ = _sequence(httpx.ConnectError("down"), httpx.ConnectError("still down"))
    with pytest.raises(httpx.ConnectError):
        request_with_retry(
            _client(handler),
            "GET",
            "https://x.test/a",
            policy=RetryPolicy(max_attempts=2),
            sleep=lambda _: None,
        )


def test_delay_is_capped_and_jittered() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
    assert retry_delay(None, 10, policy, rng=lambda: 1.0) == 10.0
    assert retry_delay(None, 1, policy, rng=lambda: 0.0) == 0.5
    huge = httpx.Response(429, headers={"Retry-After": "9999"})
    assert retry_delay(huge, 1, policy) == 10.0


def test_request_kwargs_are_passed_through() -> None:
    handler, seen = _sequence(httpx.Response(304))
    request_with_retry(
        _client(handler), "GET", "https://x.test/a", headers={"If-None-Match": '"etag"'}
    )
    assert seen[0].headers["If-None-Match"] == '"etag"'


def test_rate_limit_detection() -> None:
    assert is_rate_limited(httpx.Response(429))
    assert is_rate_limited(httpx.Response(403, headers={"Retry-After": "1"}))
    assert not is_rate_limited(httpx.Response(403, text="forbidden"))
    assert not is_rate_limited(httpx.Response(404))
