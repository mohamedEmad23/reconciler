"""P13 smoke — HITL approval surface (FREE: Firestore yes, LLM no).

Proves the money-moment mechanics end to end at the route level:
seed a fake disputed invoice → /approvals lists it with the draft + provenance
→ approve sends via a STUB (never a real email), flips disputed→resolved,
increments run.dollars_recovered by amount_at_risk, and is 409-idempotent on a
second click → reject (with reason) flips disputed→escalated and stores the
reason for the P14 negative-fact loop.

Run:
  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
  GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
  GOOGLE_CLOUD_LOCATION=global uv run python scripts/smoke_approvals.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

for var in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT"):
    if not os.environ.get(var):
        sys.exit(f"missing env {var}")

from fastapi.testclient import TestClient  # noqa: E402

from reconciler import approvals, server  # noqa: E402
from reconciler.memory import RUN_INVOICES_COLLECTION, RUNS_COLLECTION, SharedMemory, get_firestore_client  # noqa: E402

RUN_A = "smokeapprovals_a"
RUN_B = "smokeapprovals_b"
INV_1 = "invoice_one"
INV_2 = "invoice_two"
AMOUNT = 123.45
# Unique (non-prod) identifiers so the P14 learning writes never pollute the
# real vendor:Acme Cloud Services LLC fact used by demo evidence.
SMOKE_VENDOR = "Smoke Approvals Vendor LLC"
SMOKE_INV_1 = "INV-SMOKE-1"
SMOKE_INV_2 = "INV-SMOKE-2"

_sent: list[tuple[str, str, str]] = []


def _stub_send(recipient: str, subject: str, body: str) -> dict:
    _sent.append((recipient, subject, body))
    return {"sent": True, "message_id": "stub-msg-1", "error": None}


async def _seed(run_id: str, invoice_id: str, amount: float, invoice_number: str) -> None:
    client = get_firestore_client()
    await client.collection(RUNS_COLLECTION).document(run_id).set(
        {"run_id": run_id, "job_type": "smoke", "status": "in_progress", "invoice_count": 1,
         "completed_count": 0, "failed_count": 0, "dollars_recovered": 0.0}
    )
    await client.collection(RUN_INVOICES_COLLECTION).document(f"{run_id}_{invoice_id}").set(
        {
            "run_id": run_id,
            "invoice_id": invoice_id,
            "status": "in_progress",
            "stages_done": ["extraction", "verification", "resolution"],
            "stages_data": {
                "extraction": {"invoice": {"vendor": SMOKE_VENDOR,
                                           "invoice_number": invoice_number, "total": amount}},
                "verification": {"matched": False, "discrepancies": [{"type": "duplicate_payment"}]},
                "resolution": {
                    "decision": {"discrepancy_type": "duplicate_payment", "lane": "dispute",
                                 "confidence": 0.95, "rationale": "two bank rows, one invoice"},
                    "dispute_draft": {"recipient": "ap@acme.example", "subject": "Duplicate charge",
                                      "body": "please refund", "amount_at_risk": amount},
                    "outcome": "disputed",
                    "provenance": {"discrepancy_type": "duplicate_payment", "lane": "dispute",
                                   "rule_fired": "fuzzy_match(vendor, bank_row) @ 0.95",
                                   "resolution_rationale": "two bank rows, one invoice"},
                },
            },
        }
    )


async def _doc(run_id: str, invoice_id: str) -> dict:
    client = get_firestore_client()
    snap = await client.collection(RUN_INVOICES_COLLECTION).document(f"{run_id}_{invoice_id}").get()
    return snap.to_dict() or {}


async def _run_doc(run_id: str) -> dict:
    client = get_firestore_client()
    snap = await client.collection(RUNS_COLLECTION).document(run_id).get()
    return snap.to_dict() or {}


async def _cleanup() -> None:
    client = get_firestore_client()
    memory = SharedMemory(client)
    for run_id, inv in ((RUN_A, INV_1), (RUN_B, INV_2)):
        await client.collection(RUN_INVOICES_COLLECTION).document(f"{run_id}_{inv}").delete()
        await client.collection(RUNS_COLLECTION).document(run_id).delete()
    # P14 learning writes facts on approve/reject — remove them too.
    await memory.delete_fact(namespace="vendor", key=SMOKE_VENDOR)
    for n in (SMOKE_INV_1, SMOKE_INV_2):
        await memory.delete_fact(namespace="prior_invoice", key=n)


async def main() -> None:
    await _cleanup()
    await _seed(RUN_A, INV_1, AMOUNT, SMOKE_INV_1)
    await _seed(RUN_B, INV_2, 50.0, SMOKE_INV_2)

    real_send = approvals.email_tools.send_email
    approvals.email_tools.send_email = _stub_send
    try:
        with TestClient(server.app) as client:
            # [1] health + index
            assert client.get("/health").json() == {"status": "ok"}, "health"
            assert client.get("/").status_code == 200, "index"
            print("[1] /health + / OK")

            # [2] /approvals lists both disputes with draft + provenance
            page = client.get("/approvals")
            assert page.status_code == 200 and "Approve" in page.text, "approvals page"
            assert INV_1 in page.text and INV_2 in page.text, "lists both"
            assert "123.45" in page.text and "fuzzy_match" in page.text, "amount + provenance"
            print("[2] /approvals lists disputes w/ draft + provenance OK")

            # [3] approve → resolved + stub send + dollars_recovered
            resp = client.post(
                f"/approvals/{RUN_A}/{INV_1}/decision?format=json", data={"action": "approve"}
            )
            body = resp.json()
            assert resp.status_code == 200 and body["status"] == "approved", body
            assert abs(body["amount"] - AMOUNT) < 0.01, body
            assert len(_sent) == 1 and _sent[0][0] == "ap@acme.example" and _sent[0][1] == "Duplicate charge"
            doc = await _doc(RUN_A, INV_1)
            res = doc["stages_data"]["resolution"]
            assert res["outcome"] == "resolved" and res["human_decision"] == "approved", res
            assert res["dispute_send"]["sent"] is True, res
            assert abs((await _run_doc(RUN_A)).get("dollars_recovered", 0.0) - AMOUNT) < 0.01, "run dollars"
            print(f"[3] approve OK — sent via stub, dollars_recovered=${AMOUNT:.2f}")

            # [4] double-approve → 409, no second send
            resp = client.post(
                f"/approvals/{RUN_A}/{INV_1}/decision?format=json", data={"action": "approve"}
            )
            assert resp.status_code == 409 and resp.json()["status"] == "already_decided", resp.text
            assert len(_sent) == 1, "no double send"
            print("[4] double-approve 409 idempotent OK")

            # [5] reject with reason → escalated + reason stored
            resp = client.post(
                f"/approvals/{RUN_B}/{INV_2}/decision?format=json",
                data={"action": "reject", "reason": "bank already reconciled this one"},
            )
            body = resp.json()
            assert resp.status_code == 200 and body["status"] == "rejected", body
            doc = await _doc(RUN_B, INV_2)
            res = doc["stages_data"]["resolution"]
            assert res["outcome"] == "escalated" and res["human_decision"] == "rejected"
            assert res["human_rejection_reason"] == "bank already reconciled this one"
            assert len(_sent) == 1, "reject never sends"
            assert (await _run_doc(RUN_B)).get("dollars_recovered", 0.0) == 0.0, "no dollars on reject"
            print("[5] reject w/ reason OK — escalated, no send, no dollars")

            # [6] queue drained
            page = client.get("/approvals").text
            assert INV_1 not in page and INV_2 not in page, "queue drained"
            print("[6] approvals queue drained OK")
    finally:
        approvals.email_tools.send_email = real_send
        await _cleanup()
    print("smoke_approvals PASS")


if __name__ == "__main__":
    asyncio.run(main())
