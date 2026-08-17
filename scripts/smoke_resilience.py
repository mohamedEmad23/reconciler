#!/usr/bin/env python3
"""Phase P10 — resilience smoke (PURE PYTHON, no Vertex, no Firestore, free).

Proves the four reliability primitives in agents/reconciler/resilience.py:
  1. retry_with_backoff  — transient errors retried w/ exp backoff+jitter;
                            permanent errors fail fast; budget exhaustion raises.
  2. CircuitBreaker      — closed -> open (after N failures) -> half_open
                            (after cooldown) -> closed on probe success;
                            re-opens on probe failure.
  3. watchdog            — per-call deadline; on timeout the inner awaitable
                            is cancelled (verified via a side-effect flag).
  4. guard() composition — watchdog -> breaker -> retry; a dying dependency
                            trips the breaker and subsequent calls fail FAST
                            (CircuitOpen, zero inner invocations); after
                            cooldown the half-open probe recovers.
  5. publish_to_dlq      — mock publisher; envelope fields + never-raises
                            contract.

Run: uv run python scripts/smoke_resilience.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from reconciler.resilience import (  # noqa: E402
    CircuitBreaker,
    CircuitOpen,
    RetryBudgetExhausted,
    WatchdogTimeout,
    guard,
    is_transient,
    publish_to_dlq,
    retry_with_backoff,
    watchdog,
)

PASS = "\nsmoke_resilience PASS"
FAIL_EXIT = 2


def _ok(label: str) -> None:
    print(f"[ok] {label}")


async def t_retry() -> None:
    # transient fails twice then succeeds
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("connection reset by peer")  # transient marker
        return "healed"

    out = await retry_with_backoff(
        flaky, dependency="t-retry", max_attempts=5, base_s=0.01, jitter=0.0
    )
    assert out == "healed" and calls["n"] == 3, (out, calls)
    _ok("retry: transient x2 then success (3 calls, backoff honored)")

    # permanent error fails fast — no retry
    calls["n"] = 0

    async def broken() -> str:
        calls["n"] += 1
        raise ValueError("permanent schema violation")  # no transient marker

    try:
        await retry_with_backoff(
            broken, dependency="t-retry", max_attempts=5, base_s=0.01
        )
        raise AssertionError("permanent error must raise")
    except ValueError:
        assert calls["n"] == 1, calls
    _ok("retry: permanent error raises immediately (1 call, no retry)")

    # budget exhausted
    async def always_transient() -> str:
        raise ConnectionError("temporarily unavailable")

    try:
        await retry_with_backoff(
            always_transient, dependency="t-retry", max_attempts=3, base_s=0.01
        )
        raise AssertionError("must exhaust")
    except RetryBudgetExhausted as exc:
        assert exc.attempts == 3, exc.attempts
        assert isinstance(exc.last, ConnectionError)
    _ok("retry: budget exhausted raises RetryBudgetExhausted (attempts=3, last kept)")

    # jitter bounds — delays stay within [base*2^n*(1-j), base*2^n*(1+j)]
    timings: list[float] = []
    real_sleep = asyncio.sleep

    async def timed_sleep(delay: float) -> None:
        timings.append(delay)
        await real_sleep(0)

    import reconciler.resilience as res_mod

    orig = res_mod.asyncio.sleep
    res_mod.asyncio.sleep = timed_sleep  # type: ignore[assignment]
    try:
        try:
            await retry_with_backoff(
                always_transient, dependency="t-jitter", max_attempts=3,
                base_s=1.0, jitter=0.3,
            )
        except RetryBudgetExhausted:
            pass
    finally:
        res_mod.asyncio.sleep = orig  # type: ignore[assignment]
    assert len(timings) == 2, timings  # sleeps between 3 attempts
    for n, d in enumerate(timings):
        lo, hi = 1.0 * (2 ** n) * 0.7, 1.0 * (2 ** n) * 1.3
        assert lo <= d <= hi, (n, d, lo, hi)
    _ok(f"retry: exp backoff + jitter bounds hold (delays={['%.3f' % d for d in timings]})")

    # is_transient markers
    assert is_transient(WatchdogTimeout("deadline_exceeded after 30s"))
    assert is_transient(ConnectionError("connection refused"))
    assert not is_transient(ValueError("permanent"))
    _ok("retry: is_transient classification (timeout/deadline=transient, ValueError=not)")


async def t_breaker() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

    clk = FakeClock()
    br = CircuitBreaker(dependency="t-bank", failure_threshold=3, cooldown_s=20.0, clock=clk)

    async def fail() -> str:
        raise ConnectionError("unavailable")

    async def win() -> str:
        return "ok"

    # closed -> 3 failures -> open
    for i in range(3):
        try:
            await br.call(fail)
        except ConnectionError:
            pass
    assert br.state == "open", br.state
    # open -> calls fail FAST with CircuitOpen, inner NOT invoked
    inner_calls = {"n": 0}

    async def counted_fail() -> str:
        inner_calls["n"] += 1
        raise ConnectionError("unavailable")

    try:
        await br.call(counted_fail)
        raise AssertionError("must raise CircuitOpen")
    except CircuitOpen:
        assert inner_calls["n"] == 0, "breaker must short-circuit without invoking"
    _ok("breaker: 3 failures -> OPEN, short-circuits (0 inner invocations)")

    # cooldown not elapsed -> still open
    clk.t = 10.0
    try:
        await br.call(win)
        raise AssertionError("still open before cooldown")
    except CircuitOpen:
        pass

    # cooldown elapsed -> half_open probe success -> closed
    clk.t = 21.0
    out = await br.call(win)
    assert out == "ok" and br.state == "closed"
    _ok("breaker: cooldown elapsed -> HALF_OPEN probe success -> CLOSED")

    # probe failure while half-open -> re-opens
    for i in range(3):
        try:
            await br.call(fail)
        except ConnectionError:
            pass
    assert br.state == "open"
    clk.t = 42.0  # past cooldown again
    try:
        await br.call(fail)  # half-open probe fails
    except ConnectionError:
        pass
    assert br.state == "open", "failed probe must re-open"
    _ok("breaker: half-open probe failure -> re-OPEN")


async def t_watchdog() -> None:
    cancelled = {"flag": False}

    async def slow() -> str:
        try:
            await asyncio.sleep(5)
            return "late"
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise

    try:
        await watchdog(slow(), dependency="t-slow", timeout_s=0.1)
        raise AssertionError("must time out")
    except WatchdogTimeout:
        pass
    await asyncio.sleep(0)  # let cancellation propagate
    assert cancelled["flag"], "inner awaitable must be cancelled on timeout"
    _ok("watchdog: deadline enforced, inner awaitable cancelled (verified via flag)")


async def t_guard() -> None:
    br = CircuitBreaker(dependency="t-guard-dep", failure_threshold=2, cooldown_s=5.0)
    invocations = {"n": 0}

    async def dying() -> str:
        invocations["n"] += 1
        raise ConnectionError("connection refused")

    # guard retries then trips the breaker
    try:
        await guard(
            "t-guard-dep", breaker=br, timeout_s=5.0, max_attempts=2, base_s=0.01,
        )(lambda: dying())
        raise AssertionError("must raise")
    except RetryBudgetExhausted:
        pass
    assert br.state == "open", br.state
    before = invocations["n"]
    # subsequent guard call fails FAST — no inner invocations
    try:
        await guard(
            "t-guard-dep", breaker=br, timeout_s=5.0, max_attempts=2, base_s=0.01,
        )(lambda: dying())
        raise AssertionError("must CircuitOpen")
    except CircuitOpen:
        assert invocations["n"] == before, "fast-fail must not invoke dependency"
    _ok("guard: dying dep -> breaker OPEN -> next call fails FAST (0 invocations)")

    # recovery after cooldown (half-open probe succeeds)
    await asyncio.sleep(5.05)  # real clock; cooldown_s=5.0

    async def healed() -> str:
        invocations["n"] += 1
        return "recovered"

    out = await guard(
        "t-guard-dep", breaker=br, timeout_s=5.0, max_attempts=2, base_s=0.01,
    )(lambda: healed())
    assert out == "recovered" and br.state == "closed"
    _ok("guard: after cooldown, half-open probe succeeds -> circuit CLOSED, dep recovered")


async def t_dlq() -> None:
    published: list[tuple[str, bytes]] = []

    class FakeFuture:
        def result(self) -> None:  # pragma: no cover - trivial
            return None

    class FakePublisher:
        def topic_path(self, project: str, topic: str) -> str:
            return f"projects/{project}/topics/{topic}"

        def publish(self, topic: str, data: bytes) -> "FakeFuture":
            published.append((topic, data))
            return FakeFuture()

    mid = await publish_to_dlq(
        run_id="run_x", invoice_id="inv_y", error="boom" * 2000,
        stage="verification", publisher=FakePublisher(),
        project="proj-1", topic="projects/whatever/reconciler.dlq",
    )
    assert mid == "", "mock publisher has no message id; must still return '' not raise"
    assert len(published) == 1
    topic, data = published[0]
    assert topic == "projects/proj-1/topics/reconciler.dlq", topic
    env = json.loads(data.decode("utf-8"))
    assert env["run_id"] == "run_x" and env["invoice_id"] == "inv_y"
    assert env["stage"] == "verification"
    assert len(env["error"]) <= 2000, "error must be truncated"
    assert env["published_at"]
    _ok("publish_to_dlq: envelope fields + topic resolution + error truncation")

    # never-raises contract — broken publisher
    class BrokenPublisher:
        def topic_path(self, *a: str) -> str:  # noqa: ANN002
            raise RuntimeError("transport dead")

        def publish(self, *a: object, **k: object) -> None:  # pragma: no cover
            raise RuntimeError("unreachable")

    mid2 = await publish_to_dlq(
        run_id="r", invoice_id="i", error="e",
        publisher=BrokenPublisher(), project="p", topic="t",
    )
    assert mid2 == ""
    _ok("publish_to_dlq: broken transport -> swallowed, returns '' (Firestore dlq status is durable record)")


async def main() -> None:
    print("Phase P10 resilience smoke — retry / breaker / watchdog / guard / DLQ")
    await t_retry()
    await t_breaker()
    await t_watchdog()
    await t_guard()
    await t_dlq()
    print(PASS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"smoke_resilience FAIL: {exc}")
        sys.exit(FAIL_EXIT)
