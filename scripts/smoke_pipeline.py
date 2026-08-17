"""Phase 3.5 pipeline smoke — FULL CHAIN on fixtures (live Firestore + Vertex).

Proves the demo centerpiece end-to-end:
  intake → extraction → verification → categorization → reconciliation → reporting
with per-stage Firestore checkpoints, epistemic-memory writes after verified
output, an idempotent re-run (zero recomposition), and the HITL Tier-2 batch
posture (digest composed, email blocked awaiting human approval).

Env (same as other smokes):
  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json
  GOOGLE_GENAI_USE_VERTEXAI=1
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad
  GOOGLE_CLOUD_LOCATION=us-central1

Cost: ~5 Vertex calls ≈ $0.03 (run 1). Run 2 reuses everything (0 LLM calls).
"""

from __future__ import annotations

import asyncio
import json
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

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
VENDOR = "Acme Cloud Services LLC"


async def main() -> None:
    missing = [e for e in REQUIRED_ENV if not os.environ.get(e)]
    assert not missing, f"missing env vars: {missing}"
    assert (FIXTURES / "invoice_sample.pdf").read_bytes()[:4] == b"%PDF"

    run_id = f"smokepipe_{uuid.uuid4().hex[:8]}"
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

    try:
        # [1] run 1 — the full chain
        t0 = time.monotonic()
        r1 = await pipe.run(run_id=run_id, job_type="smoke_pipeline")
        dt1 = time.monotonic() - t0
        print(
            f"[1] run-1: invoices={r1.invoices_total} completed={r1.invoices_completed} "
            f"failed={r1.invoices_failed} flagged={r1.flagged_count} ({dt1:.0f}s)"
        )
        assert r1.invoices_total == 1, "expected exactly the fixture invoice"
        assert r1.invoices_completed == 1, f"invoice did not complete: {r1}"
        assert r1.invoices_failed == 0, "unexpected failure"

        # [2] per-stage Firestore checkpoints visible
        state = await store.get_invoice_state(
            run_id=run_id, invoice_id="invoice_sample"
        )
        assert state is not None and state["status"] == "completed"
        done = state["stages_done"]
        print(f"[2] firestore checkpoints: {done}")
        for stage in (
            "intake",
            "extraction",
            "verification",
            "categorization",
            "reconciliation",
        ):
            assert stage in done, f"missing checkpoint: {stage}"

        # [3] final verdict — matched with invariants recomputed
        rec = state["stages_data"]["reconciliation"]
        print(
            f"[3] verdict={rec['verdict']} codes={rec.get('account_codes_assigned')} "
            f"invariants={rec.get('invariants_checked')} passed={rec.get('invariants_passed')}"
        )
        assert rec["verdict"] == "matched", f"expected matched, got {rec['verdict']}"
        assert rec.get("invariants_passed") is True

        # [4] digest composed; email BLOCKED behind HITL Tier-2 (batch mode)
        digest = r1.digest or {}
        print(
            f"[4] digest: composed={digest.get('digest_composed')} "
            f"flagged={digest.get('flagged_count')} sent={digest.get('email_sent')} "
            f"blocked_by_hitl={digest.get('email_blocked_by_hitl')}"
        )
        assert digest.get("digest_composed") is True
        assert digest.get("email_sent") is False, "email must NOT send in batch"
        assert digest.get("email_blocked_by_hitl") is True

        # [5] epistemic memory written AFTER verified output
        fact = await memory.get_fact(namespace="vendor", key=VENDOR)
        print(f"[5] shared memory vendor fact: {json.dumps(fact)[:140] if fact else None}")
        assert fact is not None and "account_codes" in fact

        # [6] idempotent re-run — digest reused, zero recomposition
        t1 = time.monotonic()
        r2 = await pipe.run(run_id=run_id, job_type="smoke_pipeline")
        dt2 = time.monotonic() - t1
        print(
            f"[6] run-2 (idempotent): skipped={r2.skipped} completed={r2.invoices_completed} "
            f"({dt2:.0f}s — no LLM stage recomposed)"
        )
        assert r2.skipped is True, "re-run must reuse the completed run"
        assert r2.digest == r1.digest, "digest must be byte-identical on re-run"

        # [7] run doc closed with correct counts
        run_doc = await store.get_run(run_id=run_id)
        assert run_doc is not None
        print(
            f"[7] run doc: status={run_doc['status']} "
            f"counts={run_doc['completed_count']}/{run_doc['invoice_count']}"
        )
        assert run_doc["status"] in ("completed", "completed_with_errors")
        assert run_doc["completed_count"] == 1

        print("smoke_pipeline PASS")
    finally:
        # hermetic cleanup: run + invoice + memory fact (leave nothing behind)
        try:
            await store.delete_invoice(run_id=run_id, invoice_id="invoice_sample")
            await store.delete_run(run_id=run_id)
            await memory.delete_fact(namespace="vendor", key=VENDOR)
            print("[cleanup] smoke run + memory fact deleted")
        except Exception as exc:  # pragma: no cover
            print(f"[cleanup] WARNING: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
