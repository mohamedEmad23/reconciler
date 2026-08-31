"""Read-only Firestore query tools for the chat assistant.

These are the tools a human-facing chat agent uses to answer questions about
what the autonomous reconciliation agent did. They read ONLY structured,
provenanced memory — runs, per-invoice state, learned facts, pending disputes —
never a vector store. That is the "agentic memory vs chatbot recall" contrast:
answers are grounded in the auditable record, not a retrieval-augmented guess.

All functions are async (they await the AsyncClient) and read-only. They never
mutate Firestore and never raise — a query error surfaces as an ``error`` field
so the chat agent can report it honestly instead of hallucinating a number.
"""

from __future__ import annotations

from typing import Any

from google.cloud import firestore

from ..memory import (
    MEMORY_COLLECTION,
    RUN_INVOICES_COLLECTION,
    RUNS_COLLECTION,
    get_firestore_client,
)


async def list_runs(limit: int = 10) -> dict[str, Any]:
    """List the most recent reconciliation runs (newest first).

    Each run reports how many invoices were processed, how many failed, the
    dollars recovered (approved disputes) and dollars at risk (pending).
    """
    try:
        client = get_firestore_client()
        runs: list[dict[str, Any]] = []
        async for snap in (
            client.collection(RUNS_COLLECTION)
            .order_by("started_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        ):
            d = snap.to_dict() or {}
            summary = d.get("summary") or {}
            runs.append(
                {
                    "run_id": d.get("run_id") or snap.id,
                    "job_type": d.get("job_type"),
                    "status": d.get("status"),
                    "completed_count": d.get("completed_count"),
                    "invoice_count": d.get("invoice_count"),
                    "failed_count": d.get("failed_count"),
                    "dollars_recovered": d.get("dollars_recovered"),
                    "flagged_count": summary.get("flagged_count"),
                    "dollars_at_risk": summary.get("dollars_at_risk"),
                }
            )
        return {"runs": runs}
    except Exception as exc:  # noqa: BLE001
        return {"runs": [], "error": f"{type(exc).__name__}: {exc}"}


async def list_invoices(run_id: str) -> dict[str, Any]:
    """List the invoices processed in one run, with vendor, number, total, and verdict."""
    try:
        client = get_firestore_client()
        invoices: list[dict[str, Any]] = []
        async for snap in (
            client.collection(RUN_INVOICES_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", run_id))
            .stream()
        ):
            d = snap.to_dict() or {}
            stages = d.get("stages_data") or {}
            invoice = (stages.get("extraction") or {}).get("invoice") or {}
            recon = stages.get("reconciliation") or {}
            resolution = stages.get("resolution") or {}
            invoices.append(
                {
                    "invoice_id": d.get("invoice_id"),
                    "vendor": invoice.get("vendor"),
                    "invoice_number": invoice.get("invoice_number"),
                    "total": invoice.get("total"),
                    "verdict": recon.get("verdict"),
                    "resolution_outcome": resolution.get("outcome"),
                    "human_decision": resolution.get("human_decision"),
                    "status": d.get("status"),
                }
            )
        return {"invoices": invoices}
    except Exception as exc:  # noqa: BLE001
        return {"invoices": [], "error": f"{type(exc).__name__}: {exc}"}


async def list_facts() -> dict[str, Any]:
    """List the learned memory facts (Shared Epistemic Memory) the agent has accumulated."""
    try:
        client = get_firestore_client()
        facts: list[dict[str, Any]] = []
        async for snap in client.collection(MEMORY_COLLECTION).stream():
            d = snap.to_dict() or {}
            facts.append(
                {
                    "namespace": d.get("namespace"),
                    "key": d.get("key"),
                    "value": d.get("value"),
                }
            )
        return {"facts": facts}
    except Exception as exc:  # noqa: BLE001
        return {"facts": [], "error": f"{type(exc).__name__}: {exc}"}


async def list_disputes() -> dict[str, Any]:
    """List pending disputes awaiting human approval (HITL Tier-2)."""
    try:
        client = get_firestore_client()
        disputes: list[dict[str, Any]] = []
        async for snap in (
            client.collection(RUN_INVOICES_COLLECTION)
            .where(
                filter=firestore.FieldFilter(
                    "stages_data.resolution.outcome", "==", "disputed"
                )
            )
            .stream()
        ):
            d = snap.to_dict() or {}
            stages = d.get("stages_data") or {}
            invoice = (stages.get("extraction") or {}).get("invoice") or {}
            resolution = stages.get("resolution") or {}
            draft = resolution.get("dispute_draft") or {}
            disputes.append(
                {
                    "run_id": d.get("run_id"),
                    "invoice_id": d.get("invoice_id"),
                    "vendor": invoice.get("vendor"),
                    "invoice_number": invoice.get("invoice_number"),
                    "total": invoice.get("total"),
                    "amount_at_risk": draft.get("amount_at_risk"),
                }
            )
        return {"disputes": disputes}
    except Exception as exc:  # noqa: BLE001
        return {"disputes": [], "error": f"{type(exc).__name__}: {exc}"}
