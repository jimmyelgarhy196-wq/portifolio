"""Shared HTTP client for network-backed providers.

Handles the operational realities the brief calls out: rate limits, transient
failures, timeouts, and provider outages. A failure here always surfaces as a
:class:`ProviderError` — never as a partial or invented result.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any

import httpx

from backend.core.config import get_settings
from backend.core.logging_config import EVENT_PROVIDER_FAILURE, get_logger, log_event
from backend.data.providers.base import ProviderRateLimited, ProviderUnavailable

logger = get_logger(__name__)

USER_AGENT = "EGX-ALPHA/1.0 (personal research terminal)"


class RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, per_second: float) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


class HttpFetcher:
    """HTTP GET with retry, exponential backoff, jitter and rate limiting."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float | None = None,
        max_retries: int | None = None,
        rate_limit_per_second: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout if timeout is not None else settings.http_timeout_seconds
        self.max_retries = (
            max_retries if max_retries is not None else settings.http_max_retries
        )
        self._limiter = RateLimiter(
            rate_limit_per_second
            if rate_limit_per_second is not None
            else settings.http_rate_limit_per_second
        )
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout, headers=self._headers, follow_redirects=True
            )
        return self._client

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = self._url(path)
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                response = self.client.get(url, params=params)
            except httpx.RequestError as exc:
                last_error = exc
                log_event(
                    logger,
                    EVENT_PROVIDER_FAILURE,
                    f"HTTP request failed: {exc.__class__.__name__}",
                    url=url,
                    attempt=attempt + 1,
                )
            else:
                if response.status_code == 429:
                    last_error = ProviderRateLimited(f"429 from {url}")
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").isdigit() else None
                    self._sleep(attempt, delay)
                    continue
                if response.status_code >= 500:
                    last_error = ProviderUnavailable(
                        f"{response.status_code} from {url}"
                    )
                elif response.status_code >= 400:
                    # Client errors are not retried — they will not fix themselves.
                    raise ProviderUnavailable(
                        f"{response.status_code} from {url}: {response.text[:200]}"
                    )
                else:
                    return response

            if attempt < self.max_retries:
                self._sleep(attempt)

        raise ProviderUnavailable(f"GET {url} failed after {self.max_retries + 1} attempts: {last_error}")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.get(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderUnavailable(f"Malformed JSON from {self._url(path)}: {exc}") from exc

    @staticmethod
    def _sleep(attempt: int, override: float | None = None) -> None:
        delay = override if override is not None else (2.0**attempt) + random.uniform(0, 0.4)
        time.sleep(min(delay, 30.0))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
