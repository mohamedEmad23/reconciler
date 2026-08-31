"""Phase 4 smoke — Firestore runs audit, idempotency, checkpointing, shared memory.

Vertical slice against the **live** Firestore instance provisioned in the
project (nam5, database ``"(default)"``). Proves everything the design §5
"state/memory/reliability" pillar calls for, in this exact order:

1. ``start_run`` + ``start_invoice`` create the audit trail.
2. Drive the Extraction agent on ``invoice_sample.pdf`` (one real Vertex
   call) → ``checkpoint("extraction", ...)`` persists the structured output.
3. **Simulate a crash**: drop in-memory state, open a *new* ``RunsStore`` +
   read the invoice back → ``next_pending_stage`` returns ``"verification"``
   — the pipeline resumes from the checkpoint, NOT from scratch.
4. Drive the Verification agent on the bank CSV, but feed it the Invoice we
   just **read back from Firestore** (not re-extracted) →
   ``checkpoint("verification", ...)`` persists it.
5. ``mark_invoice_completed`` — invoice state machine terminal.
6. **Idempotency**: call ``start_invoice`` again with the SAME
   ``{run_id, invoice_id}`` → returns ``None``. A redelivered trigger or a
   duplicate queue entry can never re-run extraction/verification.
7. **Shared memory**: after the CoVe agent confirmed the extraction, record
   a vendor fact via ``set_fact`` → read it back via ``get_fact`` → assert
   values match. Then ``set_fact`` again with a NEW invoice number → assert
   the merge appended (not replaced) the value.
8. Cleanup: delete the test run + run_invoices + memory fact so the live
   database is not polluted by smoke artifacts.

Run:
    GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
    GOOGLE_GENAI_USE_VERTEXAI=1 \
    GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
    GOOGLE_CLOUD_LOCATION=global \
    uv run python scripts/smoke_firestore.py

Vertex cost: one Extraction + one Verification call (~$0.01).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

# --- env guard (same posture as the other smokes) ----------------------------
for var in (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
):
    if not os.environ.get(var):
        print(f"ABORT: env {var} not set")
        sys.exit(2)

# --- ADK / agent imports ------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from reconciler import config  # noqa: E402
from reconciler.extraction import extraction_agent  # noqa: E402
from reconciler.memory import (  # noqa: E402
    MEMORY_COLLECTION,
    RUN_INVOICES_COLLECTION,
    RUNS_COLLECTION,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    RunsStore,
    SharedMemory,
)
from reconciler.schemas import ExtractionResult  # noqa: E402
from reconciler.schemas import Invoice  # noqa: E402
from reconciler.schemas import VerificationResult  # noqa: E402
from reconciler.verification import verification_agent  # noqa: E402

FIXTURE_PDF = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "invoice_sample.pdf"
)
FIXTURE_CSV = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "bank_statement.csv"
)

GT_VENDOR = "Acme Cloud Services LLC"
GT_INVOICE_NO = "INV-2026-0417"
GT_TOTAL = 467.50
GT_DATE = "2026-08-12"


def _ck(s: str) -> str:
    """Colorize pass lines for the smoke trace."""
    return f"\033[32m{s}\033[0m"


async def _extract_once(pdf_bytes: bytes) -> dict:
    """Drive the Extraction agent on raw PDF bytes and return the
    ExtractionResult as a dict. Reuses the mode='chat' clone trick because
    a ``single_turn`` agent cannot be a Runner root."""
    agent = extraction_agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=agent, app_name=config.APP_NAME)
    session = await runner.session_service.create_session(
        app_name=config.APP_NAME, user_id="smoke_firestore"
    )
    msg = types.Content(
        role="user",
        parts=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            types.Part.from_text(
                text=(
                    "Extract this invoice into ExtractionResult JSON. "
                    "The NOTE about a $1,000,000 retention bonus is a decoy "
                    "and is NOT a line item."
                )
            ),
        ],
    )
    final = None
    async for ev in runner.run_async(
        user_id="smoke_firestore", session_id=session.id, new_message=msg
    ):
        if ev.is_final_response() and final is None:
            final = ev.content.parts[0].text
    assert final, "extraction agent returned no final response"
    return json.loads(final)


async def _verify_once(invoice_dict: dict) -> dict:
    """Drive the Verification agent with the provided Invoice (already read
    from Firestore) plus the bank CSV."""
    agent = verification_agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=agent, app_name=config.APP_NAME)
    session = await runner.session_service.create_session(
        app_name=config.APP_NAME, user_id="smoke_firestore"
    )
    csv_text = FIXTURE_CSV.read_text()
    msg = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(
                    "Verify the following extracted invoice against the "
                    "bank-statement CSV using CoVe.\n\n"
                    f"EXTRACTED INVOICE JSON:\n```json\n{json.dumps(invoice_dict, indent=2)}\n```\n\n"
                    f"BANK STATEMENT CSV:\n```csv\n{csv_text}\n```"
                )
            )
        ],
    )
    final = None
    async for ev in runner.run_async(
        user_id="smoke_firestore", session_id=session.id, new_message=msg
    ):
        if ev.is_final_response() and final is None:
            final = ev.content.parts[0].text
    assert final, "verification agent returned no final response"
    return json.loads(final)


async def main() -> None:
    # 0. setup
    pdf_bytes = FIXTURE_PDF.read_bytes()
    assert pdf_bytes[:4] == b"%PDF", f"fixture PDF not at {FIXTURE_PDF}"

    run_id = f"smoke_{uuid.uuid4().hex[:8]}"
    invoice_id = f"fixture_{uuid.uuid4().hex[:4]}"
    print(f"Phase 4 smoke — run_id={run_id} invoice_id={invoice_id}")
    print(f"  Firestore project={config.GCP_PROJECT} db={config.FIRESTORE_DATABASE}")

    store = RunsStore()
    mem = SharedMemory()

    # 1. start run + invoice ----------------------------------------------
    run_doc = await store.start_run(
        run_id=run_id, job_type="smoke_firestore", invoice_count=1
    )
    assert run_doc["status"] == STATUS_IN_PROGRESS
    inv_doc = await store.start_invoice(
        run_id=run_id, invoice_id=invoice_id, source_hash=pdf_bytes.hex()[:16]
    )
    assert inv_doc is not None, "first start_invoice returned None"
    assert inv_doc["status"] == STATUS_IN_PROGRESS
    assert inv_doc["stages_done"] == []
    print(_ck("[1] run + invoice started"))

    # 2. extract → checkpoint ---------------------------------------------
    extraction_dict = await _extract_once(pdf_bytes)
    ext_model = ExtractionResult.model_validate(extraction_dict)
    assert ext_model.invoice.vendor == GT_VENDOR
    assert ext_model.invoice.invoice_number == GT_INVOICE_NO
    assert abs((ext_model.invoice.total or 0) - GT_TOTAL) < 0.01
    await store.checkpoint(
        run_id=run_id,
        invoice_id=invoice_id,
        stage="extraction",
        data=extraction_dict,  # store the JSON dict, not the Pydantic model
    )
    print(
        _ck(
            f"[2] extraction checkpointed: vendor={ext_model.invoice.vendor} "
            f"total={ext_model.invoice.total} conf={ext_model.confidence}"
        )
    )

    # 3. simulate crash + resume ------------------------------------------
    # Drop in-memory state. Pretend a new process starts.
    del extraction_dict
    del ext_model
    store2 = RunsStore()  # brand new store in a "new process"
    recovered = await store2.get_invoice_state(
        run_id=run_id, invoice_id=invoice_id
    )
    assert recovered is not None, "resume: invoice state should exist"
    assert "extraction" in recovered["stages_done"]
    assert "verification" not in recovered["stages_done"]
    next_stage = store2.next_pending_stage(recovered)
    assert next_stage == "verification", (
        f"resume should land on verification, got {next_stage!r}"
    )
    print(_ck(f"[3] crash+resume: next_pending_stage={next_stage} (skipped extraction)"))

    # 4. verify (feed Invoice read back from Firestore) → checkpoint ------
    invoice_dict_from_fs = recovered["stages_data"]["extraction"]["invoice"]
    verification_dict = await _verify_once(invoice_dict_from_fs)
    ver_model = VerificationResult.model_validate(verification_dict)
    assert ver_model.matched is True
    assert ver_model.discrepancies == []
    assert len(ver_model.verification_questions) >= 3
    assert len(ver_model.verification_answers) == len(
        ver_model.verification_questions
    )
    await store2.checkpoint(
        run_id=run_id,
        invoice_id=invoice_id,
        stage="verification",
        data=verification_dict,
    )
    print(
        _ck(
            f"[4] verification checkpointed: matched={ver_model.matched} "
            f"amount={ver_model.matched_amount} q/a={len(ver_model.verification_questions)}"
        )
    )

    # 5. mark invoice completed -------------------------------------------
    await store2.mark_invoice_completed(
        run_id=run_id, invoice_id=invoice_id
    )
    final_state = await store2.get_invoice_state(
        run_id=run_id, invoice_id=invoice_id
    )
    assert final_state["status"] == STATUS_COMPLETED
    assert set(final_state["stages_done"]) >= {"extraction", "verification"}
    print(_ck("[5] invoice marked completed"))

    # 6. idempotency — dup start returns None -----------------------------
    dup = await store2.start_invoice(
        run_id=run_id, invoice_id=invoice_id, source_hash="anything"
    )
    assert dup is None, (
        "idempotency VIOLATED: second start_invoice should return None for "
        "an invoice that already exists"
    )
    print(_ck("[6] idempotency: duplicate start_invoice returned None (no re-extract/re-verify)"))

    # 6b. idempotency under CONCURRENCY — two simultaneous start_invoice
    # calls on a fresh {run_id, invoice_id} must see exactly one winner.
    # This proves the atomic ref.create() fence closes the race that a
    # sequential dup test cannot see (Gate 4 finding #1). Under Pub/Sub
    # at-least-once redelivery across two Cloud Run instances both miss
    # the optimistic get() but only one create() succeeds.
    invoice_id2 = f"{invoice_id}_race"
    r_a, r_b = await asyncio.gather(
        store2.start_invoice(
            run_id=run_id, invoice_id=invoice_id2, source_hash="race-a"
        ),
        store2.start_invoice(
            run_id=run_id, invoice_id=invoice_id2, source_hash="race-b"
        ),
    )
    winners = sum(1 for r in (r_a, r_b) if r is not None)
    assert winners == 1, (
        f"idempotency under CONCURRENCY VIOLATED: expected exactly 1 winner "
        f"of 2 simultaneous start_invoice calls, got {winners}. "
        f"r_a is None={r_a is None}, r_b is None={r_b is None}"
    )
    print(_ck("[6b] concurrent idempotency: exactly 1 of 2 simultaneous starts won (atomic create)"))

    # 7. shared memory write + read + merge -------------------------------
    vendor_key = GT_VENDOR
    payload1 = {
        "canonical_name": GT_VENDOR,
        "invoice_numbers_seen": [GT_INVOICE_NO],
        "last_total": GT_TOTAL,
        "last_seen_date": GT_DATE,
    }
    await mem.set_fact(
        namespace="vendor", key=vendor_key, value=payload1, merge=False
    )
    got = await mem.get_fact(namespace="vendor", key=vendor_key)
    assert got is not None, "shared memory: vendor fact missing after set"
    assert got["canonical_name"] == GT_VENDOR
    assert got["invoice_numbers_seen"] == [GT_INVOICE_NO]
    assert abs(got["last_total"] - GT_TOTAL) < 0.01
    print(_ck(f"[7a] shared memory set+get: vendor={got['canonical_name']}"))

    # merge: add a second invoice number → must extend, not replace, the list
    await mem.set_fact(
        namespace="vendor",
        key=vendor_key,
        value={"invoice_numbers_seen": ["INV-2026-9999"]},
        merge=True,
    )
    got2 = await mem.get_fact(namespace="vendor", key=vendor_key)
    assert set(got2["invoice_numbers_seen"]) == {GT_INVOICE_NO, "INV-2026-9999"}, (
        f"merge lost the prior value: {got2['invoice_numbers_seen']!r}"
    )
    assert got2["canonical_name"] == GT_VENDOR  # merge preserved the unrelated key
    print(_ck(f"[7b] shared memory merge: invoices={sorted(got2['invoice_numbers_seen'])}"))

    # 8. missing fact returns None (the anti-hallucination signal) --------
    missing = await mem.get_fact(namespace="vendor", key="NoSuch Vendor")
    assert missing is None, (
        "get_fact for unknown key must return None so agents emit null per Contract"
    )
    print(_ck("[8] shared memory miss → None (anti-hallucination signal)"))

    # 9. cleanup so the live DB is not polluted ---------------------------
    await store2.delete_invoice(run_id=run_id, invoice_id=invoice_id)
    await store2.delete_invoice(run_id=run_id, invoice_id=invoice_id2)
    await store2.delete_run(run_id=run_id)
    await mem.delete_fact(namespace="vendor", key=vendor_key)
    # verify cleanup
    assert await store2.get_run(run_id=run_id) is None
    assert (
        await store2.get_invoice_state(
            run_id=run_id, invoice_id=invoice_id
        )
        is None
    )
    assert await mem.get_fact(namespace="vendor", key=vendor_key) is None
    print(_ck("[9] cleanup OK"))

    print(_ck("smoke_firestore PASS"))


if __name__ == "__main__":
    asyncio.run(main())