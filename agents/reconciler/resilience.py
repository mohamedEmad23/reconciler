"""Resilience primitives — adaptive retry, circuit breaker, watchdog, DLQ.

P10 of the closed-loop design (docs/reconciler-closed-loop-design.md §4).
Every external dependency call (bank-statement read, Gmail, vendor fetch,
Vertex model calls) runs through these wrappers so a single dead dependency
cannot stall a whole reconciliation run:

- ``retry_with_backoff`` — exponential backoff with jitter on TRANSIENT
  errors only (t = base_s * 2**n ± jitter). Permanent errors re-raise
  immediately; no retry budget is wasted on 4xx-style failures.
- ``CircuitBreaker`` — per-dependency closed→open→half_open state machine.
  After ``failure_threshold`` consecutive failures the circuit OPENS and
  short-circuits for ``cooldown_s``; the next call is a half-open probe.
  One dead dependency therefore fails fast instead of hogging the run.
- ``watchdog`` — hard deadline around any awaitable; on timeout the inner
  task is CANCELLED (never leaked) and ``WatchdogTimeout`` propagates so
  the caller can fall back or fail over.
- ``publish_to_dlq`` — publish a poisoned-invoice envelope to the
  ``reconciler.dlq`` topic (transport-level isolation; pairs with the
  existing Firestore ``mark_invoice_failed(dlq=True)`` status write).

All primitives are dependency-free (stdlib + google-cloud-pubsub for DLQ)
and unit-testable without Vertex.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from . import config

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Adaptive retry
# ---------------------------------------------------------------------------

# Error substrings that mark a TRANSIENT failure (safe to retry). Anything
# else re-raises immediately so we never retry a permanent 4xx.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "429",
    "resource_exhausted",
    "rate limit",
    "rate_limit",
    "unavailable",
    "deadline_exceeded",
    "internal",
    "connection reset",
    "connection refused",
    "temporarily",
    "overloaded",
)


def is_transient(exc: BaseException) -> bool:
    """True when the exception looks transient (retry-safe)."""
    markers = " ".join(
        filter(None, [type(exc).__name__.lower(), str(exc).lower()])
    )
    return any(m in markers for m in _TRANSIENT_MARKERS)


class RetryBudgetExhausted(RuntimeError):
    """Raised when every attempt failed; carries the last exception."""

    def __init__(self, attempts: int, last: BaseException):
        self.attempts = attempts
        self.last = last
        super().__init__(f"retry budget exhausted after {attempts} attempts: {last!r}")


async def retry_with_backoff(
    call: Callable[[], Awaitable[T]],
    *,
    dependency: str = "unknown",
    max_attempts: int = 5,
    base_s: float = 1.0,
    jitter: float = 0.3,
    cap_s: float = 30.0,
) -> T:
    """Await ``call()`` with exponential backoff + jitter on transient errors.

    Delay for attempt n (0-indexed failure) is ``min(cap_s, base_s * 2**n)``
    scaled by a random jitter in ``[1 - jitter, 1 + jitter]``.
    """
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await call()
        except BaseException as exc:  # noqa: BLE001 — classified below
            if not is_transient(exc):
                raise
            last = exc
            if attempt == max_attempts:
                break
            delay = min(cap_s, base_s * (2 ** (attempt - 1)))
            delay *= random.uniform(1.0 - jitter, 1.0 + jitter)
            logger.warning(
                "retrying %s (attempt %d/%d, backoff %.1fs) after %r",
                dependency, attempt + 1 - 1 + 1, max_attempts, delay, exc,
            )
            await asyncio.sleep(delay)
    raise RetryBudgetExhausted(max_attempts, last)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class CircuitOpen(RuntimeError):
    """Raised when the circuit is OPEN — fail fast, do not call the dependency."""

    def __init__(self, dependency: str, cooldown_remaining_s: float):
        super().__init__(
            f"circuit OPEN for {dependency} (cooldown {cooldown_remaining_s:.0f}s remaining)"
        )


class CircuitBreaker:
    """Per-dependency circuit breaker: closed → open → half_open.

    - CLOSED: calls pass through; consecutive failures counted.
    - OPEN (after ``failure_threshold`` consecutive failures): calls raise
      ``CircuitOpen`` immediately for ``cooldown_s``.
    - HALF_OPEN (after cooldown): exactly one probe call passes through —
      success closes the circuit, failure re-opens it for another cooldown.
    """

    def __init__(
        self,
        *,
        dependency: str,
        failure_threshold: int = 3,
        cooldown_s: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.dependency = dependency
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown_s:
            return "half_open"
        return "open"

    def allow(self) -> None:
        """Raise ``CircuitOpen`` if the circuit blocks the next call."""
        if self.state == "open":
            raise CircuitOpen(
                self.dependency, self.cooldown_s - (self._clock() - (self._opened_at or 0.0))
            )

    def record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("circuit CLOSED for %s (probe succeeded)", self.dependency)
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open":
            # A failed probe re-opens the circuit for a FULL new cooldown
            # (otherwise _opened_at would keep the stale timestamp and
            # `state` would remain half_open — every call would probe).
            self._opened_at = self._clock()
            logger.warning(
                "circuit re-OPENED for %s (half-open probe failed)", self.dependency
            )
            return
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = self._clock()
            logger.warning(
                "circuit OPEN for %s after %d consecutive failures",
                self.dependency, self._failures,
            )

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Guarded call: allow → run → record outcome."""
        self.allow()
        try:
            result = await fn()
        except BaseException:
            self.record_failure()
            raise
        self.record_success()
        return result


# ---------------------------------------------------------------------------
# Watchdog timeout
# ---------------------------------------------------------------------------

class WatchdogTimeout(RuntimeError):
    """The guarded awaitable exceeded its deadline and was cancelled."""


async def watchdog(
    awaitable: Awaitable[T],
    *,
    dependency: str,
    timeout_s: float,
) -> T:
    """Hard deadline around ``awaitable``.

    On timeout the inner task is CANCELLED (never leaked) and
    ``WatchdogTimeout`` propagates. The raised error is transient-marked
    (contains 'deadline_exceeded') so retry policy may back off and re-enter.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise WatchdogTimeout(
            f"watchdog deadline_exceeded for {dependency} ({timeout_s:.0f}s)"
        ) from exc


def guard(
    dependency: str,
    *,
    breaker: CircuitBreaker,
    timeout_s: float = 30.0,
    max_attempts: int = 4,
    base_s: float = 1.0,
) -> Callable[[Callable[[], Awaitable[T]]], Awaitable[T]]:
    """Compose watchdog + breaker + retry into one decorator-style guard.

    Order (innermost → outermost): watchdog(per call) → breaker(around each
    attempt) → retry(with backoff, transient-only). A timeout cancels the
    call, counts as a breaker failure, and is retryable; a CircuitOpen error
    is NOT transient — it fails fast out of retry so the run moves on.
    """

    async def _guarded(fn: Callable[[], Awaitable[T]]) -> T:
        async def _one_attempt() -> T:
            return await watchdog(breaker.call(fn), dependency=dependency, timeout_s=timeout_s)

        return await retry_with_backoff(
            _one_attempt, dependency=dependency, max_attempts=max_attempts, base_s=base_s
        )

    return _guarded


# ---------------------------------------------------------------------------
# DLQ publish
# ---------------------------------------------------------------------------

async def publish_to_dlq(
    *,
    run_id: str,
    invoice_id: str,
    error: str,
    stage: str | None = None,
    publisher: Any | None = None,
    project: str | None = None,
    topic: str | None = None,
) -> str:
    """Publish a poisoned-invoice envelope to the dead-letter topic.

    Application-level DLQ (distinct from the transport-level push-subscription
    dead-letter policy): used when an invoice cannot complete after retries.
    Returns the published message id. Failures to publish are logged and
    swallowed — the Firestore ``dlq`` status write is the durable record.
    """
    import json as _json

    from google.cloud import pubsub_v1

    try:
        client = publisher or pubsub_v1.PublisherClient()
        topic_path = client.topic_path(
            project or config.GCP_PROJECT, (topic or config.TOPIC_DLQ).split("/")[-1]
        )
        payload = _json.dumps(
            {
                "run_id": run_id,
                "invoice_id": invoice_id,
                "stage": stage,
                "error": error[:2000],
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ).encode()
        future = client.publish(topic_path, payload)
        # pubsub returns a concurrent.futures-style Future (NOT awaitable)
        # — resolve it on a worker thread so the event loop never blocks.
        message_id = await asyncio.to_thread(future.result, 15.0)
        logger.warning(
            "published to DLQ: run=%s invoice=%s stage=%s message_id=%s",
            run_id, invoice_id, stage, message_id,
        )
        return str(message_id)
    except Exception:  # noqa: BLE001 — DLQ publish must never crash the run
        logger.exception(
            "DLQ publish failed (Firestore status remains the durable record): "
            "run=%s invoice=%s", run_id, invoice_id,
        )
        return ""
