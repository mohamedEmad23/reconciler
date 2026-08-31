"""P24 mismatch smoke — three discrepancy types end-to-end (live Firestore + Vertex).

Fixtures (tests/fixtures_mismatch/):
  amount_mismatch_invoice.pdf   Stellar Analytics Ltd, $1,150.00 (bank charged $1,500)
  vendor_mismatch_invoice.pdf   Quantum Robotics Corp, $3,000.00 (bank memo typo)
  date_mismatch_invoice.pdf     Nebula Data Systems, $800.00 (bank predates invoice)
  bank_statement.csv            one deliberately-wrong row per invoice

Proves the pipeline catches all three remaining discrepancy types the local
fixture set did not yet exercise (amount / vendor / date), routes them to the
resolution agent, and records a full audit trail — without ever auto-resolving
money or self-certifying.

The CORE (deterministic) assertion is that each invoice's Verification stage
catches the RIGHT discrepancy type. The resolution LANE (resolve/dispute/
escalate) is the agent's judgment and may legitimately differ between the
fixtures (e.g. a minor vendor typo auto-resolves via entity re-verification);
we only assert the lane is VALID, not which one.

Env (same as other smokes):
  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json
  GOOGLE_GENAI_USE_VERTEXAI=1
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad
  GOOGLE_CLOUD_LOCATION=global

Cost: ~3 invoices * (extract + verify + categorize + reconcile [+ resolution])
      ≈ ~15 Vertex calls ≈ $0.08 (run 1). Run 2 reuses everything (0 LLM).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, "agents")

from reconciler.memory import (  # noqa: E402
    RunsStore,
    SharedMemory,
    get_firestore_client,
)
from reconciler.pipeline import Pipeline  # noqa: E402

REQUIRED_ENV = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures_mismatch"

# Expected discrepancy type per invoice (filename stem -> discrepancy type).
EXPECTED = {
    "amount_mismatch_invoice": "amount_mismatch",
    "vendor_mismatch_invoice": "vendor_mismatch",
    "date_mismatch_invoice": "date_mismatch",
}
# Vendors whose shared-memory facts we must restore in cleanup.
VENDORS = (
    "Stellar Analytics Ltd",
    "Quantum Robotics Corp",
    "Nebula Data Systems",
)
VALID_LANES = {"resolve", "dispute", "escalate"}


async def _build_pipe(client) -> tuple[Pipeline, RunsStore, SharedMemory]:
    store = RunsStore(client=client)
    memory = SharedMemory(client=client)
    pipe = Pipeline(
        store=store,
        memory=memory,
        source="local_dir",
        directory=str(FIXTURES),
        bank_csv=str(FIXTURES / "bank_statement.csv"),
    )
    return pipe, store, memory


async def main() -> None:
    missing = [e for e in REQUIRED_ENV if not os.environ.get(e)]
    assert not missing, f"missing env vars: {missing}"
    for stem in EXPECTED:
        assert (FIXTURES / f"{stem}.pdf").read_bytes()[:4] == b"%PDF"
    bank_text = (FIXTURES / "bank_statement.csv").read_text()
    assert "STELLAR ANALYTICS" in bank_text.upper()
    assert "QUANTOM ROBOTICS" in bank_text.upper()
    assert "NEBULA DATA" in bank_text.upper()

    client = get_firestore_client()
    store = RunsStore(client=client)
    memory = SharedMemory(client=client)
    pipe = Pipeline(
        store=store,
        memory=memory,
        source="local_dir",
        directory=str(FIXTURES),
        bank_csv=str(FIXTURES / "bank_statement.csv"),
    )

    # Protect any pre-existing PROD vendor facts (demo evidence): capture now,
    # restore exactly in cleanup instead of blind-deleting.
    pre_facts = {v: await memory.get_fact(namespace="vendor", key=v) for v in VENDORS}

    run_ids: list[str] = []
    try:
        # [1] run — all three invoices through the full closed loop. The
        # sandbox network can intermittently drop DNS (NameResolutionError)
        # mid-run; retry ONCE with a fresh run_id if an invoice failed, so a
        # transient outage does not fail a genuine capability test.
        r1 = None
        for attempt in (1, 2):
            run_id = f"smokemis_{uuid.uuid4().hex[:8]}"
            run_ids.append(run_id)
            t0 = time.monotonic()
            r1 = await pipe.run(run_id=run_id, job_type="smoke_mismatch")
            dt = time.monotonic() - t0
            print(
                f"[1] run (attempt {attempt}): invoices={r1.invoices_total} "
                f"completed={r1.invoices_completed} failed={r1.invoices_failed} "
                f"flagged={r1.flagged_count} at_risk=${r1.dollars_at_risk:.2f} "
                f"({dt:.0f}s)"
            )
            assert r1.invoices_total == 3, "expected the 3 mismatch invoices"
            if r1.invoices_failed == 0:
                break
            print(f"    transient failure ({r1.invoices_failed} failed) — retrying")
        assert r1 is not None
        assert r1.invoices_completed == 3, f"all must complete: {r1}"
        assert r1.invoices_failed == 0, "unexpected failure after retry"
        run_id = run_ids[-1]

        # [2] CORE: each invoice's Verification (CoVe) stage caught the RIGHT
        # discrepancy type, and resolution routed it to a valid lane with a
        # full audit trail.
        for stem, want_type in EXPECTED.items():
            state = await store.get_invoice_state(run_id=run_id, invoice_id=stem)
            assert state is not None and state["status"] == "completed", stem
            assert "resolution" in state["stages_done"], f"{stem}: resolution must run"
            ver = state["stages_data"]["verification"]
            types = [d.get("type") for d in ver.get("discrepancies", [])]
            rec = state["stages_data"]["reconciliation"]
            verdict = rec.get("verdict")
            res = state["stages_data"]["resolution"]
            decision = res.get("decision", {})
            lane = decision.get("lane")
            outcome = res.get("outcome")
            print(
                f"[2] {stem}: verdict={verdict} discrepancies={types} "
                f"lane={lane} outcome={outcome}"
            )
            assert want_type in types, f"{stem}: expected {want_type}, got {types}"
            assert lane in VALID_LANES, f"{stem}: invalid lane {lane!r}"
            assert res.get("provenance"), f"{stem}: provenance must be recorded"

        # [3] idempotent re-run — everything reused, zero LLM calls.
        t1 = time.monotonic()
        r2 = await pipe.run(run_id=run_id, job_type="smoke_mismatch")
        dt2 = time.monotonic() - t1
        print(f"[3] run-2 (idempotent): skipped={r2.skipped} ({dt2:.0f}s — no LLM)")
        assert r2.skipped is True

        # [4] digest composed, email still blocked (batch HITL posture).
        digest = r1.digest or {}
        assert digest.get("digest_composed") is True
        assert digest.get("email_sent") is False
        assert digest.get("email_blocked_by_hitl") is True

        print("smoke_mismatch PASS")
    finally:
        # hermetic cleanup: invoices + run(s); vendor facts RESTORED so any
        # pre-existing PROD demo data survives the smoke.
        try:
            for rid in run_ids:
                for stem in EXPECTED:
                    await store.delete_invoice(run_id=rid, invoice_id=stem)
                await store.delete_run(run_id=rid)
            for v in VENDORS:
                if pre_facts[v] is None:
                    await memory.delete_fact(namespace="vendor", key=v)
                else:
                    await memory.set_fact(
                        namespace="vendor", key=v, value=pre_facts[v], merge=False
                    )
            print("[cleanup] smoke runs + invoices deleted; vendor facts restored")
        except Exception as exc:  # pragma: no cover
            print(f"[cleanup] WARNING: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
