"""latest_run — print the newest run id (and its disputed invoices) for demo.sh.

Fires zero LLM calls. Reads Firestore only. Used by scripts/demo.sh to discover
the run_id of the autonomously-triggered weekly run (whose id is the Pub/Sub
message id, not known in advance).

Run: GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
     GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
     uv run python scripts/latest_run.py

Output (one line of JSON):
  {"run_id": "...", "status": "...", "disputed_invoices": ["...", ...]}
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "agents")

from google.cloud import firestore

from reconciler.memory import (
    RUN_INVOICES_COLLECTION,
    RUNS_COLLECTION,
    get_firestore_client,
)


async def main() -> None:
    client = get_firestore_client()

    run_id: str | None = None
    status: str | None = None
    async for doc in (
        client.collection(RUNS_COLLECTION)
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    ):
        run_id = doc.get("run_id") or doc.id
        status = doc.get("status")

    disputed: list[str] = []
    if run_id:
        async for doc in (
            client.collection(RUN_INVOICES_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", run_id))
            .stream()
        ):
            stages = doc.get("stages_data") or {}
            outcome = (stages.get("resolution") or {}).get("outcome")
            if outcome == "disputed":
                disputed.append(doc.get("invoice_id") or doc.id)

    print(json.dumps({"run_id": run_id, "status": status, "disputed_invoices": disputed}))


if __name__ == "__main__":
    asyncio.run(main())
