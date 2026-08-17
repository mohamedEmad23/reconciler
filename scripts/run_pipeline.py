#!/usr/bin/env python
"""Run the full Reconciler batch pipeline against production Firestore.

This is the demo entrypoint for the six-specialist batch spine:
intake -> extraction -> verification (CoVe) -> categorization -> reconciliation
-> reporting digest, with per-stage Firestore checkpoints, idempotent re-runs,
and Shared Epistemic Memory writes.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=~/keys/reconciler-sa.json \
    GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
    GOOGLE_CLOUD_LOCATION=us-central1 \
    uv run python scripts/run_pipeline.py [run_id]

Pass the same run_id twice to prove idempotency (second run: 0 LLM calls).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from reconciler.memory import RunsStore, SharedMemory, get_firestore_client  # noqa: E402
from reconciler.pipeline import Pipeline  # noqa: E402


async def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    client = get_firestore_client()
    store = RunsStore(client)
    memory = SharedMemory(client)
    pipe = Pipeline(store=store, memory=memory)

    print(f"reconciler batch pipeline — project={store.client.project} db=(default)")
    result = await pipe.run(run_id=run_id)

    digest = result.digest or {}
    flagged = digest.get("flagged_items") or []
    summary = {
        "run_id": result.run_id,
        "job_type": result.job_type,
        "invoices_total": result.invoices_total,
        "invoices_completed": result.invoices_completed,
        "invoices_failed": result.invoices_failed,
        "flagged_count": result.flagged_count,
        "skipped_idempotent": result.skipped,
        "digest_composed": digest.get("digest_composed"),
        "email_sent": digest.get("email_sent"),
        "email_blocked_by_hitl": digest.get("email_blocked_by_hitl"),
        "flagged_items": [
            {
                "invoice": f.get("invoice_number"),
                "vendor": f.get("vendor"),
                "type": f.get("discrepancy_type"),
                "invoice_value": f.get("invoice_value"),
                "bank_value": f.get("bank_value"),
            }
            for f in flagged
        ],
    }
    print(json.dumps(summary, indent=2, default=str))
    print("run_pipeline OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
