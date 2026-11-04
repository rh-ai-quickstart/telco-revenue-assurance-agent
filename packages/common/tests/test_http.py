from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from common import http as common_http
from common.http import HttpClient, RetryPolicy, _retry_after_seconds


def test_retry_after_seconds_parses_numeric_delay() -> None:
    assert _retry_after_seconds("3") == 3.0


def test_retry_after_seconds_parses_http_date() -> None:
    retry_at = datetime.now(UTC) + timedelta(seconds=30)

    seconds = _retry_after_seconds(format_datetime(retry_at))

    assert seconds is not None
    assert 28.0 <= seconds <= 30.0


def test_retry_after_seconds_returns_zero_for_past_date() -> None:
    retry_at = datetime.now(UTC) - timedelta(seconds=30)

    assert _retry_after_seconds(format_datetime(retry_at)) == 0.0


@pytest.mark.parametrize("value", [None, "", "not-a-date", "-1", "NaN", "Infinity"])
def test_retry_after_seconds_returns_none_for_missing_or_invalid_values(
    value: str | None,
) -> None:
    assert _retry_after_seconds(value) is None


async def _request_with_retry_after(
    retry_after: str,
    policy: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[httpx.Response, list[float], int]:
    sleeps: list[float] = []
    request_count = 0

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200)

    monkeypatch.setattr(common_http, "asyncio", SimpleNamespace(sleep=record_sleep))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = HttpClient(client=transport, retry_policy=policy)
        response = await client.request("GET", "https://upstream.example", retryable=True)

    return response, sleeps, request_count


async def test_numeric_retry_after_within_cap_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, sleeps, request_count = await _request_with_retry_after(
        "0.25",
        RetryPolicy(max_retries=1, base_delay_seconds=0.1, max_delay_seconds=1.0),
        monkeypatch,
    )

    assert response.status_code == 200
    assert sleeps == [0.25]
    assert request_count == 2


async def test_past_date_retries_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    retry_after = format_datetime(datetime.now(UTC) - timedelta(seconds=30))

    response, sleeps, request_count = await _request_with_retry_after(
        retry_after,
        RetryPolicy(max_retries=1, max_delay_seconds=1.0),
        monkeypatch,
    )

    assert response.status_code == 200
    assert sleeps == [0.0]
    assert request_count == 2


@pytest.mark.parametrize("retry_after", ["invalid", "-1"])
async def test_invalid_retry_after_uses_bounded_backoff(
    retry_after: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, sleeps, request_count = await _request_with_retry_after(
        retry_after,
        RetryPolicy(max_retries=1, base_delay_seconds=2.0, max_delay_seconds=0.5),
        monkeypatch,
    )

    assert response.status_code == 200
    assert sleeps == [0.5]
    assert request_count == 2


@pytest.mark.parametrize(
    "retry_after",
    [
        "30",
        format_datetime(datetime.now(UTC) + timedelta(hours=1)),
    ],
)
async def test_retry_after_over_cap_returns_response_without_early_retry(
    retry_after: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, sleeps, request_count = await _request_with_retry_after(
        retry_after,
        RetryPolicy(max_retries=1, max_delay_seconds=1.0),
        monkeypatch,
    )

    assert response.status_code == 429
    assert sleeps == []
    assert request_count == 1
