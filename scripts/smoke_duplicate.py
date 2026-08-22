"""P11 duplicate-payment smoke — THE MONEY MOMENT (live Firestore + Vertex).

Fixtures (tests/fixtures_duplicate/):
  invoice_sample.pdf            the clean invoice (verdict: matched)
  duplicate_invoice_sample.pdf  same vendor, INV-2026-0421, total $2,400.00
  bank_statement.csv            TWO matching debits for INV-2026-0421

Proves the closed-loop money path end-to-end:
  verification flags duplicate_payment
  -> resolution lane=dispute (HARD CLAMP: duplicate_payment never auto-resolves)
  -> DisputeDraft drafted (inert: recipient/subject/body/amount_at_risk ONLY)
  -> dollars_at_risk = $2,400.00 on the run
  -> dollars_recovered stays $0.00 (only APPROVED disputes count — P13 surface)
  -> idempotent re-run reuses everything (0 LLM calls)

Env (same as other smokes):
  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json
  GOOGLE_GENAI_USE_VERTEXAI=1
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad
  GOOGLE_CLOUD_LOCATION=us-central1

Cost: ~11 Vertex calls ≈ $0.06 (run 1). Run 2 reuses everything (0 LLM calls).
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

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures_duplicate"
VENDOR = "Acme Cloud Services LLC"
DUP_INVOICE_NO = "INV-2026-0421"
DUP_TOTAL = 2400.00
DRAFT_KEYS = {"recipient", "subject", "body", "amount_at_risk"}


async def main() -> None:
    missing = [e for e in REQUIRED_ENV if not os.environ.get(e)]
    assert not missing, f"missing env vars: {missing}"
    assert (FIXTURES / "invoice_sample.pdf").read_bytes()[:4] == b"%PDF"
    assert (FIXTURES / "duplicate_invoice_sample.pdf").read_bytes()[:4] == b"%PDF"
    bank_text = (FIXTURES / "bank_statement.csv").read_text()
    assert bank_text.count(DUP_INVOICE_NO) == 2, "bank CSV must carry the double charge"

    run_id = f"smokedup_{uuid.uuid4().hex[:8]}"
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

    # Protect any pre-existing PROD vendor fact (e.g. demo evidence): capture
    # now, restore exactly in cleanup instead of blind-deleting it.
    pre_fact = await memory.get_fact(namespace="vendor", key=VENDOR)

    try:
        # [1] run 1 — both invoices through the full closed loop
        t0 = time.monotonic()
        r1 = await pipe.run(run_id=run_id, job_type="smoke_duplicate")
        dt1 = time.monotonic() - t0
        print(
            f"[1] run-1: invoices={r1.invoices_total} completed={r1.invoices_completed} "
            f"failed={r1.invoices_failed} flagged={r1.flagged_count} "
            f"at_risk=${r1.dollars_at_risk:.2f} recovered=${r1.dollars_recovered:.2f} ({dt1:.0f}s)"
        )
        assert r1.invoices_total == 2, "expected clean + duplicate invoices"
        assert r1.invoices_completed == 2, f"both must complete: {r1}"
        assert r1.invoices_failed == 0, "unexpected failure"
        assert r1.flagged_count == 1, "exactly the duplicate should be flagged"
        assert abs(r1.dollars_at_risk - DUP_TOTAL) < 0.02, (
            f"dollars_at_risk must be ${DUP_TOTAL:.2f}, got {r1.dollars_at_risk}"
        )
        assert r1.dollars_recovered == 0.0, "recovered stays 0 until a human approves"

        # [2] the clean invoice still matches
        clean = await store.get_invoice_state(
            run_id=run_id, invoice_id="invoice_sample"
        )
        assert clean is not None and clean["status"] == "completed"
        clean_rec = clean["stages_data"]["reconciliation"]
        print(f"[2] clean invoice: verdict={clean_rec['verdict']}")
        assert clean_rec["verdict"] == "matched", clean_rec["verdict"]

        # [3] the duplicate: flagged duplicate_payment -> dispute lane -> draft
        dup = await store.get_invoice_state(
            run_id=run_id, invoice_id="duplicate_invoice_sample"
        )
        assert dup is not None and dup["status"] == "completed"
        assert "resolution" in dup["stages_done"], "resolution stage must run"
        ver = dup["stages_data"]["verification"]
        dup_types = [d.get("type") for d in ver.get("discrepancies", [])]
        print(f"[3] duplicate verification discrepancies: {dup_types}")
        assert "duplicate_payment" in dup_types, "CoVe must catch the double charge"

        res = dup["stages_data"]["resolution"]
        decision = res.get("decision", {})
        draft = res.get("dispute_draft") or {}
        print(
            f"[3] resolution: lane={decision.get('lane')} outcome={res.get('outcome')} "
            f"amount_at_risk=${draft.get('amount_at_risk', 0):.2f}"
        )
        assert decision.get("lane") == "dispute", "hard clamp: duplicate_payment => dispute"
        assert res.get("outcome") == "disputed", "dispute is the pending-approval terminal"
        assert abs((draft.get("amount_at_risk") or 0) - DUP_TOTAL) < 0.02, draft
        assert set(draft.keys()) <= DRAFT_KEYS, f"draft must be inert, got {draft.keys()}"
        assert res.get("provenance"), "provenance must be recorded for the audit trail"

        # [4] run doc carries the money summary (approved-only recovered)
        run_doc = await store.get_run(run_id=run_id)
        summary = run_doc.get("summary") or {}
        print(
            f"[4] run doc: status={run_doc['status']} "
            f"at_risk=${summary.get('dollars_at_risk', 0):.2f} "
            f"recovered=${summary.get('dollars_recovered', 0):.2f}"
        )
        assert run_doc["status"] in ("completed", "completed_with_errors")
        assert abs((summary.get("dollars_at_risk") or 0) - DUP_TOTAL) < 0.02
        assert (summary.get("dollars_recovered") or 0) == 0.0

        # [5] idempotent re-run — everything reused, zero LLM calls
        t1 = time.monotonic()
        r2 = await pipe.run(run_id=run_id, job_type="smoke_duplicate")
        dt2 = time.monotonic() - t1
        print(f"[5] run-2 (idempotent): skipped={r2.skipped} ({dt2:.0f}s — no LLM)")
        assert r2.skipped is True
        assert abs(r2.dollars_at_risk - DUP_TOTAL) < 0.02

        # [6] digest composed, email still blocked (batch HITL posture)
        digest = r1.digest or {}
        assert digest.get("digest_composed") is True
        assert digest.get("email_sent") is False
        assert digest.get("email_blocked_by_hitl") is True

        print("smoke_duplicate PASS")
    finally:
        # hermetic cleanup: both invoices + run; vendor fact RESTORED (not
        # blind-deleted) so pre-existing PROD demo data survives the smoke.
        try:
            await store.delete_invoice(run_id=run_id, invoice_id="invoice_sample")
            await store.delete_invoice(
                run_id=run_id, invoice_id="duplicate_invoice_sample"
            )
            await store.delete_run(run_id=run_id)
            if pre_fact is None:
                await memory.delete_fact(namespace="vendor", key=VENDOR)
            else:
                await memory.set_fact(
                    namespace="vendor", key=VENDOR, value=pre_fact, merge=False
                )
            print(
                "[cleanup] smoke run + invoices deleted; vendor fact "
                f"{'restored' if pre_fact is not None else 'absent (deleted)'}"
            )
        except Exception as exc:  # pragma: no cover
            print(f"[cleanup] WARNING: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
