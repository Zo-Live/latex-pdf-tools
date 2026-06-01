"""Scheduling, retry, rate limiting, and progress helpers for LLM calls."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from typing import Callable, Iterator, Protocol, TypeVar

import httpx

from texbook.diagnostics import exception_metadata


T = TypeVar("T")


class ProgressReporter(Protocol):
    """Callable progress sink used by CLI, GUI, or tests."""

    def __call__(self, event: "ProgressEvent") -> None:
        """Receive one progress event."""


@dataclass(frozen=True)
class ProgressEvent:
    """Structured progress event emitted by the conversion core."""

    kind: str
    operation: str
    label: str = ""
    attempt: int | None = None
    max_attempts: int | None = None
    delay: float | None = None
    error: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RetryOptions:
    """Retry policy for one logical LLM request."""

    retries: int = 2
    initial_delay: float = 2.0
    max_delay: float = 30.0

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise ValueError("llm retries must be non-negative.")
        if self.initial_delay < 0:
            raise ValueError("llm retry initial delay must be non-negative.")
        if self.max_delay < 0:
            raise ValueError("llm retry max delay must be non-negative.")
        if self.max_delay < self.initial_delay:
            raise ValueError("llm retry max delay must be at least initial delay.")

    @property
    def max_attempts(self) -> int:
        return self.retries + 1

    def delay_for_retry(self, retry_index: int) -> float:
        """Return delay before retry number ``retry_index`` starts.

        ``retry_index`` is 1-based: the first retry happens after the first
        failed attempt.
        """
        if retry_index <= 0:
            raise ValueError("retry_index must be positive.")
        if self.initial_delay == 0:
            return 0.0
        return min(self.max_delay, self.initial_delay * (2 ** (retry_index - 1)))


class LLMRateLimiter:
    """Thread-safe limiter for concurrent LLM requests and request starts."""

    def __init__(
        self,
        *,
        max_concurrency: int = 1,
        min_request_interval: float = 0.0,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if max_concurrency <= 0:
            raise ValueError("llm max concurrency must be positive.")
        if min_request_interval < 0:
            raise ValueError("llm min request interval must be non-negative.")

        self.max_concurrency = max_concurrency
        self.min_request_interval = min_request_interval
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._semaphore = BoundedSemaphore(max_concurrency)
        self._start_lock = Lock()
        self._next_start_time = 0.0

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Acquire one LLM request slot and wait for the global start window."""
        self._semaphore.acquire()
        try:
            self._wait_for_start_window()
            yield
        finally:
            self._semaphore.release()

    def _wait_for_start_window(self) -> None:
        while True:
            with self._start_lock:
                now = self._monotonic()
                wait_seconds = self._next_start_time - now
                if wait_seconds <= 0:
                    self._next_start_time = now + self.min_request_interval
                    return
            self._sleep(wait_seconds)


class LLMScheduler:
    """Run LLM requests with bounded concurrency, retry, and progress events."""

    def __init__(
        self,
        *,
        retry_options: RetryOptions | None = None,
        rate_limiter: LLMRateLimiter | None = None,
        reporter: ProgressReporter | None = None,
        sleep: Callable[[float], None] | None = None,
        retryable: Callable[[Exception], bool] | None = None,
    ):
        self.retry_options = retry_options or RetryOptions()
        self.rate_limiter = rate_limiter or LLMRateLimiter()
        self.reporter = reporter
        self._sleep = sleep or time.sleep
        self._retryable = retryable or is_retryable_llm_error

    def run(
        self,
        *,
        operation: str,
        label: str,
        request: Callable[[], T],
        metadata: dict[str, object] | None = None,
    ) -> T:
        """Execute one LLM request according to the configured policy."""
        event_metadata = dict(metadata or {})
        max_attempts = self.retry_options.max_attempts
        attempt = 0
        while True:
            attempt += 1
            try:
                with self.rate_limiter.slot():
                    self.emit(
                        ProgressEvent(
                            kind="request_started",
                            operation=operation,
                            label=label,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            metadata=event_metadata,
                        )
                    )
                    result = request()
            except Exception as exc:
                error_metadata = dict(event_metadata)
                error_metadata.update(exception_metadata(exc))
                if attempt < max_attempts and self._retryable(exc):
                    delay = self.retry_options.delay_for_retry(attempt)
                    self.emit(
                        ProgressEvent(
                            kind="retry_scheduled",
                            operation=operation,
                            label=label,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            delay=delay,
                            error=_error_message(exc),
                            metadata=error_metadata,
                        )
                    )
                    if delay > 0:
                        self._sleep(delay)
                    continue
                self.emit(
                    ProgressEvent(
                        kind="request_failed",
                        operation=operation,
                        label=label,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error=_error_message(exc),
                        metadata=error_metadata,
                    )
                )
                raise

            self.emit(
                ProgressEvent(
                    kind="request_completed",
                    operation=operation,
                    label=label,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    metadata=event_metadata,
                )
            )
            return result

    def emit(self, event: ProgressEvent) -> None:
        report_progress(self.reporter, event)


def report_progress(
    reporter: ProgressReporter | None,
    event: ProgressEvent,
) -> None:
    """Send a progress event when a reporter is configured."""
    if reporter is not None:
        reporter(event)


def is_retryable_llm_error(exc: Exception) -> bool:
    """Return whether an LLM request error is safe to retry."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True

    status_code = _status_code(exc)
    if status_code is not None:
        return status_code in {408, 409, 429} or status_code >= 500

    class_name = exc.__class__.__name__.lower()
    return (
        "timeout" in class_name
        or "connection" in class_name
        or "ratelimit" in class_name
        or "rate_limit" in class_name
    )


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__
