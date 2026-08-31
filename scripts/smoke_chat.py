"""Smoke test for the chat assistant + its read-only Firestore query tools.

FREE (Firestore yes, LLM no). Seeds a fake run + invoice + fact + dispute, then
drives the query_tools functions directly and asserts the chat_agent wiring.
Does NOT call Vertex — the live ``ask_question`` path is exercised separately
(it is a single chat LLM call).

Run:
  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
  uv run python scripts/smoke_chat.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))

from google.cloud import firestore

from reconciler import config
from reconciler.chat import chat_agent
from reconciler.memory import (
    MEMORY_COLLECTION,
    RUN_INVOICES_COLLECTION,
    RUNS_COLLECTION,
    get_firestore_client,
)
from reconciler.tools import query_tools

RUN_ID = "smokechat_run1"
VENDOR = "Smoke Chat Vendor LLC"
INVOICE_NO = "INV-SMOKE-CHAT"
FACT_NS, FACT_KEY = "vendor", VENDOR
FACT_DOC_ID = f"{FACT_NS}_{FACT_KEY.replace(' ', '_')}"


def _check_env() -> None:
    for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"):
        assert os.environ.get(k), f"missing env {k}"


async def _seed(client) -> None:
    await client.collection(RUNS_COLLECTION).document(RUN_ID).set(
        {
            "run_id": RUN_ID,
            "job_type": "weekly_reconcile",
            "status": "completed",
            "started_at": firestore.SERVER_TIMESTAMP,
            "completed_count": 1,
            "invoice_count": 1,
            "failed_count": 0,
            "dollars_recovered": 0.0,
            "summary": {"flagged_count": 1, "dollars_at_risk": 2400.0},
        }
    )
    await client.collection(RUN_INVOICES_COLLECTION).document(f"{RUN_ID}_inv1").set(
        {
            "run_id": RUN_ID,
            "invoice_id": "inv1",
            "status": "completed",
            "stages_data": {
                "extraction": {
                    "invoice": {
                        "vendor": VENDOR,
                        "invoice_number": INVOICE_NO,
                        "total": 2400.0,
                    }
                },
                "reconciliation": {"verdict": "discrepancy"},
                "resolution": {
                    "outcome": "disputed",
                    "dispute_draft": {"amount_at_risk": 2400.0},
                },
            },
        }
    )
    await client.collection(MEMORY_COLLECTION).document(FACT_DOC_ID).set(
        {"namespace": FACT_NS, "key": FACT_KEY, "value": {"account_codes": ["5000"]}}
    )


async def _cleanup(client) -> None:
    await client.collection(RUNS_COLLECTION).document(RUN_ID).delete()
    await client.collection(RUN_INVOICES_COLLECTION).document(f"{RUN_ID}_inv1").delete()
    await client.collection(MEMORY_COLLECTION).document(FACT_DOC_ID).delete()


def _tool_name(t) -> str:
    return getattr(t, "name", None) or getattr(t, "__name__", "?")


async def main() -> None:
    _check_env()
    client = get_firestore_client()
    await _seed(client)
    try:
        runs = await query_tools.list_runs()
        run_ids = [r["run_id"] for r in runs.get("runs", [])]
        assert RUN_ID in run_ids, f"list_runs missing {RUN_ID}: {runs}"
        print("[1] list_runs OK")

        inv = await query_tools.list_invoices(RUN_ID)
        invs = inv.get("invoices", [])
        assert invs and invs[0]["vendor"] == VENDOR, inv
        assert invs[0]["invoice_number"] == INVOICE_NO, inv
        print("[2] list_invoices OK")

        facts = await query_tools.list_facts()
        fkeys = [f["key"] for f in facts.get("facts", [])]
        assert FACT_KEY in fkeys, facts
        print("[3] list_facts OK")

        disp = await query_tools.list_disputes()
        assert any(
            d.get("vendor") == VENDOR and d.get("amount_at_risk") == 2400.0
            for d in disp.get("disputes", [])
        ), disp
        print("[4] list_disputes OK")

        assert chat_agent.model == config.GEMINI_MODEL, chat_agent.model
        assert chat_agent.mode == "chat", chat_agent.mode
        tool_names = sorted(_tool_name(t) for t in chat_agent.tools)
        assert tool_names == ["list_disputes", "list_facts", "list_invoices", "list_runs"], tool_names
        print(f"[5] chat_agent wiring OK (model={chat_agent.model}, {len(chat_agent.tools)} tools)")

        print("smoke_chat PASS")
    finally:
        await _cleanup(client)


if __name__ == "__main__":
    asyncio.run(main())
