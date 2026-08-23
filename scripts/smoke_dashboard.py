"""smoke_dashboard — free (Firestore yes, LLM no) proof of the P17 dashboard.

Seeds one fake run + one fake disputed invoice + one fake memory fact, then
hits the live FastAPI routes (via TestClient) and asserts the dashboard HTML
renders the scoreboard, the dispute card (with provenance + approve/reject),
the recent-runs row, and the learned-memory fact. Hermetic cleanup.

Run: GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
     GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
     uv run python scripts/smoke_dashboard.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "agents")

from google.cloud import firestore
from fastapi.testclient import TestClient

from reconciler import server
from reconciler.memory import (
    MEMORY_COLLECTION,
    RUN_INVOICES_COLLECTION,
    RUNS_COLLECTION,
    get_firestore_client,
)

RUN_ID = "smokedash_run1"
INV_ID = "dup_invoice"
VENDOR = "Smoke Dash Vendor LLC"
INVOICE_NO = "INV-SMOKE-DASH"
FACT_DOC = "smokedash_vendor_fact"


async def seed() -> None:
    client = get_firestore_client()
    await client.collection(RUNS_COLLECTION).document(RUN_ID).set(
        {
            "run_id": RUN_ID,
            "job_type": "weekly_reconcile",
            "status": "completed",
            "started_at": firestore.SERVER_TIMESTAMP,
            "ended_at": firestore.SERVER_TIMESTAMP,
            "completed_count": 1,
            "failed_count": 0,
            "dollars_recovered": 123.45,
            "summary": {"flagged_count": 1, "dollars_at_risk": 2400.0},
        }
    )
    await client.collection(RUN_INVOICES_COLLECTION).document(f"{RUN_ID}_{INV_ID}").set(
        {
            "run_id": RUN_ID,
            "invoice_id": INV_ID,
            "status": "in_progress",
            "stages_data": {
                "extraction": {
                    "invoice": {
                        "vendor": VENDOR,
                        "invoice_number": INVOICE_NO,
                        "total": 2400.0,
                    }
                },
                "resolution": {
                    "outcome": "disputed",
                    "dispute_draft": {
                        "recipient": "billing@smokedash.test",
                        "subject": "Duplicate payment dispute",
                        "body": "We were charged twice for this invoice.",
                        "amount_at_risk": 2400.0,
                    },
                    "recheck": {
                        "discrepancies": [
                            {
                                "type": "duplicate_payment",
                                "description": "two matching debits",
                                "invoice_value": "2400.00",
                                "bank_value": "2400.00",
                            }
                        ]
                    },
                    "provenance": {
                        "discrepancy_type": "duplicate_payment",
                        "lane": "dispute",
                        "rule_fired": "fuzzy_match(vendor, bank_row) @ 1.0",
                        "recheck_matched": None,
                        "resolution_rationale": "same invoice number charged twice",
                    },
                },
            },
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    await client.collection(MEMORY_COLLECTION).document(FACT_DOC).set(
        {
            "namespace": "vendor",
            "key": VENDOR,
            "value": {"account_codes": ["5000"]},
        }
    )


async def cleanup() -> None:
    client = get_firestore_client()
    for coll, doc_id in [
        (RUNS_COLLECTION, RUN_ID),
        (RUN_INVOICES_COLLECTION, f"{RUN_ID}_{INV_ID}"),
        (MEMORY_COLLECTION, FACT_DOC),
    ]:
        await client.collection(coll).document(doc_id).delete()


def main() -> None:
    asyncio.run(seed())
    try:
        with TestClient(server.app) as client:
            r = client.get("/")
            assert r.status_code == 200, f"GET / -> {r.status_code}"
            body = r.text
            needles = [
                "dollars recovered",
                "awaiting your approval",
                "invoices processed",
                "reconciliation runs",
                "Seven stages",
                "How it protects you",
                "Where it runs",
                "Recent runs",
                "Learned facts",
                RUN_ID,
                "123.45",
                VENDOR,
                "duplicate_payment",
                "Approve",
                "Reject",
            ]
            missing = [n for n in needles if n not in body]
            assert not missing, f"dashboard HTML missing: {missing}"

            r2 = client.get("/approvals")
            assert r2.status_code == 200, f"GET /approvals -> {r2.status_code}"
            assert RUN_ID in r2.text, "approvals page missing seeded run_id"

        print("smoke_dashboard PASS")
    finally:
        asyncio.run(cleanup())


if __name__ == "__main__":
    main()
