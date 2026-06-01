"""Tests for LLM scheduling, retry, and rate limiting."""

import threading

import httpx
import pytest

from texbook.llm.scheduler import (
    LLMRateLimiter,
    LLMScheduler,
    RetryOptions,
    is_retryable_llm_error,
)


def test_scheduler_retries_recoverable_errors_and_reports_events():
    events = []
    sleeps = []
    attempts = 0

    def request():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.TimeoutException("timed out")
        return "ok"

    scheduler = LLMScheduler(
        retry_options=RetryOptions(retries=2, initial_delay=1.5, max_delay=10.0),
        reporter=events.append,
        sleep=sleeps.append,
    )

    assert scheduler.run(operation="chunk", label="chunk 1", request=request) == "ok"

    assert attempts == 2
    assert sleeps == [1.5]
    assert [event.kind for event in events] == [
        "request_started",
        "retry_scheduled",
        "request_started",
        "request_completed",
    ]
    assert events[1].attempt == 1
    assert events[1].max_attempts == 3


def test_scheduler_does_not_retry_non_retryable_errors():
    events = []
    attempts = 0

    def request():
        nonlocal attempts
        attempts += 1
        raise ValueError("bad response")

    scheduler = LLMScheduler(
        retry_options=RetryOptions(retries=3, initial_delay=0.0, max_delay=0.0),
        reporter=events.append,
    )

    with pytest.raises(ValueError, match="bad response"):
        scheduler.run(operation="chunk", label="chunk 1", request=request)

    assert attempts == 1
    assert [event.kind for event in events] == ["request_started", "request_failed"]


def test_scheduler_reports_failure_after_retries_are_exhausted():
    events = []

    def request():
        raise httpx.TransportError("connection lost")

    scheduler = LLMScheduler(
        retry_options=RetryOptions(retries=1, initial_delay=0.0, max_delay=0.0),
        reporter=events.append,
    )

    with pytest.raises(httpx.TransportError):
        scheduler.run(operation="structure", label="toc", request=request)

    assert [event.kind for event in events] == [
        "request_started",
        "retry_scheduled",
        "request_started",
        "request_failed",
    ]
    assert events[-1].attempt == 2


def test_scheduler_carries_raw_preview_from_request_failures():
    events = []

    class PreviewError(RuntimeError):
        def __init__(self):
            super().__init__("invalid response")
            self.raw_preview = "```tex\\n\\begin{frame}\\n\\end{frame}```"

    def request():
        raise PreviewError()

    scheduler = LLMScheduler(reporter=events.append)

    with pytest.raises(PreviewError):
        scheduler.run(operation="chunk", label="chunk 1", request=request)

    assert events[-1].metadata["raw_preview"] == "```tex\\n\\begin{frame}\\n\\end{frame}```"
    assert events[-1].metadata["exception_type"] == "PreviewError"


def test_rate_limiter_allows_only_configured_concurrency():
    limiter = LLMRateLimiter(max_concurrency=1)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first():
        with limiter.slot():
            entered.set()
            assert release.wait(timeout=1)

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=1)

    def second():
        with limiter.slot():
            second_entered.set()

    second_thread = threading.Thread(target=second)
    second_thread.start()
    assert not second_entered.wait(timeout=0.05)
    release.set()
    thread.join(timeout=1)
    second_thread.join(timeout=1)
    assert second_entered.is_set()


def test_rate_limiter_honors_start_interval_with_injected_clock():
    current = [0.0]
    sleeps = []

    def monotonic():
        return current[0]

    def sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    limiter = LLMRateLimiter(
        max_concurrency=1,
        min_request_interval=2.0,
        monotonic=monotonic,
        sleep=sleep,
    )

    with limiter.slot():
        pass
    with limiter.slot():
        pass

    assert sleeps == [2.0]


def test_retryable_error_detection_uses_status_codes():
    assert is_retryable_llm_error(type("Err", (), {"status_code": 429})())
    assert is_retryable_llm_error(type("Err", (), {"status_code": 500})())
    assert not is_retryable_llm_error(type("Err", (), {"status_code": 401})())
