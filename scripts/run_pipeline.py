#!/usr/bin/env python
"""Run the full Reconciler batch pipeline against production Firestore.

This is the demo entrypoint for the six-specialist batch spine:
intake -> extraction -> verification (CoVe) -> categorization -> reconciliation
-> reporting digest, with per-stage Firestore checkpoints, idempotent re-runs,
and Shared Epistemic Memory writes.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=~/keys/reconciler-sa.json \
    GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
    GOOGLE_CLOUD_LOCATION=global \
    uv run python scripts/run_pipeline.py [run_id] [--directory DIR] [--source gmail]

Pass the same run_id twice to prove idempotency (second run: 0 LLM calls).
--directory selects the local_dir intake source (default tests/fixtures);
point it at tests/fixtures_duplicate for the $2,400 duplicate-payment demo.
--source gmail reads invoices from the linked Gmail inbox (OAuth from Secret
Manager) instead of the local fixture directory.
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    directory = None
    if "--directory" in sys.argv:
        try:
            directory = sys.argv[sys.argv.index("--directory") + 1]
        except IndexError:
            print("error: --directory requires a path", file=sys.stderr)
            return 2
    source = None
    if "--source" in sys.argv:
        try:
            source = sys.argv[sys.argv.index("--source") + 1]
        except IndexError:
            print("error: --source requires a value", file=sys.stderr)
            return 2
    run_id = args[0] if args else None
    client = get_firestore_client()
    store = RunsStore(client)
    memory = SharedMemory(client)
    kwargs = {"store": store, "memory": memory}
    if directory is not None:
        kwargs["directory"] = directory
    if source is not None:
        kwargs["source"] = source
    pipe = Pipeline(**kwargs)

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
