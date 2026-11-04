"""One bounded asynchronous HTTP client and retry policy."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retryable status codes and bounded exponential backoff for one client."""

    max_retries: int = 0
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be zero or greater.")
        if not self.retry_statuses:
            raise ValueError("retry_statuses must contain at least one status.")
        if not isfinite(self.base_delay_seconds) or self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must be finite and zero or greater.")
        if not isfinite(self.max_delay_seconds) or self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must be finite and zero or greater.")


class HttpClient:
    """Async JSON HTTP client with explicit opt-in retries for idempotent requests."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        retry_policy: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero.")
        self._timeout_seconds = timeout_seconds
        self._retry_policy = retry_policy or RetryPolicy()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def request(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        retryable: bool = False,
        timeout_seconds: float | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        json: object | None = None,
        content: bytes | str | None = None,
    ) -> httpx.Response:
        """Send a request; retry only when explicitly marked safe by the caller."""

        if json is not None and content is not None:
            raise ValueError("Provide either json or content, not both.")
        timeout = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero.")
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json,
                    content=content,
                    timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError):
                if not retryable or attempt >= self._retry_policy.max_retries:
                    raise
                await asyncio.sleep(self._backoff_delay(attempt))
                attempt += 1
                continue

            if (
                not retryable
                or attempt >= self._retry_policy.max_retries
                or response.status_code not in self._retry_policy.retry_statuses
            ):
                return response

            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            if retry_after is None:
                delay = self._backoff_delay(attempt)
            elif retry_after > self._retry_policy.max_delay_seconds:
                # Keep the server's requested delay authoritative without exceeding
                # this client's bound: return the response instead of retrying early.
                return response
            else:
                delay = retry_after
            await asyncio.sleep(delay)
            attempt += 1

    async def aclose(self) -> None:
        """Close the underlying client when this instance created it."""

        if self._owns_client:
            await self._client.aclose()

    def _backoff_delay(self, retry_index: int) -> float:
        delay = self._retry_policy.base_delay_seconds * (2**retry_index)
        maximum = self._retry_policy.max_delay_seconds
        return maximum if delay > maximum else delay


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
        if not isfinite(seconds):
            return None
        return max(0.0, seconds)
    if not isfinite(seconds) or seconds < 0:
        return None
    return seconds


__all__ = ["HttpClient", "RetryPolicy"]
