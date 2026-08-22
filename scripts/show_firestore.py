#!/usr/bin/env python
"""Read-only viewer for Reconciler Firestore state (demo beat 4).

Prints recent runs, their invoices with per-stage checkpoints, and Shared
Epistemic Memory facts. Read-only — never writes.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=~/keys/reconciler-sa.json \
    uv run python scripts/show_firestore.py [limit]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from google.cloud import firestore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from reconciler.memory import (  # noqa: E402
    RUN_INVOICES_COLLECTION,
    RUNS_COLLECTION,
    MEMORY_COLLECTION,
    get_firestore_client,
)
from reconciler.provenance import entries_from_state, render_entry  # noqa: E402


async def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    client = get_firestore_client()
    print(f"firestore project={client.project} db=(default)\n")

    runs = (
        await client.collection(RUNS_COLLECTION)
        .order_by("started_at", direction="DESCENDING")
        .limit(limit)
        .get()
    )
    if not runs:
        print("(no runs found)")
    for doc in runs:
        run = doc.to_dict() or {}
        print(f"run {doc.id}: status={run.get('status')} job={run.get('job_type')} "
              f"completed={run.get('completed_count')} failed={run.get('failed_count')}")
        invoices = (
            await client.collection(RUN_INVOICES_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", doc.id))
            .get()
        )
        for idoc in invoices:
            inv = idoc.to_dict() or {}
            stages = inv.get("stages_done") or []
            print(f"  invoice {inv.get('invoice_id')}: status={inv.get('status')} "
                  f"stages={stages}")
            data = inv.get("stages_data") or {}
            reco = data.get("reconciliation") or {}
            if reco:
                print(f"    verdict={reco.get('verdict')} total={reco.get('invoice_total')} "
                      f"invariants_passed={reco.get('invariants_passed')}")
            # P12: render the audit trail ("why did Reconciler do this?")
            entries, perr = entries_from_state(inv)
            for entry in entries:
                print("    provenance:")
                for line in render_entry(
                    entry, invoice_id=inv.get("invoice_id")
                ).splitlines():
                    print(f"      {line}")
            for err in perr:
                print(f"    provenance: (malformed: {err})")
        print()

    mems = await client.collection(MEMORY_COLLECTION).limit(10).get()
    if mems:
        print("shared epistemic memory:")
        for doc in mems:
            m = doc.to_dict() or {}
            print(f"  [{m.get('namespace')}] {m.get('key')}: {m.get('value')}")
    print("\nshow_firestore OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
