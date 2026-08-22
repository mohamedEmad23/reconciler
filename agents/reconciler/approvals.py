"""HITL approval operations on disputed drafts (closed-loop design §5).

The batch pipeline leaves ``stages_data.resolution.outcome == "disputed"``
together with an inert ``dispute_draft`` (never sent — the resolution agent
holds no send capability). This module is the human gate:

* ``list_pending_disputes()`` — what the /approvals page renders.
* ``approve()`` — transactional decision flip disputed → resolved, THEN the
  email send (via ``email_tools`` — the only send authority), THEN
  ``dollars_recovered`` increment. Anti-gaming (§9): dollars only ever move
  on an approved dispute, never on a draft or a flag.
* ``reject()`` — disputed → escalated with a required human reason; the
  reason is persisted for the P14 negative-fact learning loop.

Idempotency/race safety: both decisions re-read the doc inside a Firestore
transaction and require ``outcome == "disputed"``; a second click (or a
concurrent double-submit) gets ``{"status": "already_decided", ...}`` and
changes nothing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from google.cloud import firestore

from . import learning
from .memory import RUN_INVOICES_COLLECTION, RUNS_COLLECTION, get_firestore_client
from .tools import email_tools

SendFn = Callable[[str, str, str], dict[str, Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def list_pending_disputes(client: firestore.AsyncClient | None = None) -> list[dict[str, Any]]:
    """All invoices currently awaiting a human decision, newest first."""
    client = client or get_firestore_client()
    query = client.collection(RUN_INVOICES_COLLECTION).where(
        filter=firestore.FieldFilter("stages_data.resolution.outcome", "==", "disputed")
    )
    out: list[dict[str, Any]] = []
    async for snap in query.stream():
        doc = snap.to_dict() or {}
        resolution = (doc.get("stages_data") or {}).get("resolution") or {}
        extraction = (doc.get("stages_data") or {}).get("extraction") or {}
        invoice = extraction.get("invoice") or {}
        out.append(
            {
                "run_id": doc.get("run_id"),
                "invoice_id": doc.get("invoice_id"),
                "vendor": invoice.get("vendor"),
                "invoice_number": invoice.get("invoice_number"),
                "invoice_total": invoice.get("total"),
                "discrepancies": [
                    d.get("type") for d in (resolution.get("recheck") or {}).get("discrepancies", [])
                ]
                or [d.get("type") for d in _verification_discrepancies(doc)],
                "draft": resolution.get("dispute_draft") or {},
                "provenance": resolution.get("provenance"),
                "updated_at": doc.get("updated_at"),
            }
        )
    out.sort(key=lambda d: str(d.get("updated_at") or ""), reverse=True)
    return out


def _verification_discrepancies(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return ((doc.get("stages_data") or {}).get("verification") or {}).get("discrepancies") or []


@firestore.async_transactional
async def _approve_tx(
    transaction: firestore.AsyncTransaction,
    ref: firestore.AsyncDocumentReference,
    run_ref: firestore.AsyncDocumentReference,
    payload: dict[str, Any],
    approver: str,
    send_result: dict[str, Any],
) -> dict[str, Any]:
    snap = await ref.get(transaction=transaction)
    if not snap.exists:
        return {"status": "not_found"}
    doc = snap.to_dict() or {}
    resolution = (doc.get("stages_data") or {}).get("resolution") or {}
    if resolution.get("outcome") != "disputed":
        return {"status": "already_decided", "outcome": resolution.get("outcome")}
    draft = resolution.get("dispute_draft") or {}
    amount = draft.get("amount_at_risk") or 0.0
    resolution.update(
        {
            "human_decision": "approved",
            "human_decided_by": approver,
            "human_decided_at": _now_iso(),
            "outcome": "resolved",
            "dispute_send": send_result,
        }
    )
    transaction.update(ref, {"stages_data.resolution": resolution, "updated_at": firestore.SERVER_TIMESTAMP})
    transaction.update(run_ref, {"dollars_recovered": firestore.Increment(float(amount))})
    payload.update(
        {
            "status": "approved",
            "outcome": "resolved",
            "amount": float(amount),
            "send": send_result,
        }
    )
    return payload


async def approve(
    run_id: str,
    invoice_id: str,
    approver: str = "human",
    client: firestore.AsyncClient | None = None,
    send: SendFn | None = None,
) -> dict[str, Any]:
    """Approve a pending dispute: flip outcome, send the draft, count dollars.

    The email send happens BEFORE the transaction so its result is recorded
    atomically with the decision; a failed/absent send never blocks the human
    decision (send status is surfaced, not swallowed).
    """
    client = client or get_firestore_client()
    ref = client.collection(RUN_INVOICES_COLLECTION).document(f"{run_id}_{invoice_id}")
    run_ref = client.collection(RUNS_COLLECTION).document(run_id)

    # Read the draft outside the tx only to *compose* the email; the decision
    # re-validates inside the transaction.
    snap = await ref.get()
    if not snap.exists:
        return {"status": "not_found"}
    resolution = ((snap.to_dict() or {}).get("stages_data") or {}).get("resolution") or {}
    if resolution.get("outcome") != "disputed":
        return {"status": "already_decided", "outcome": resolution.get("outcome")}
    draft = resolution.get("dispute_draft") or {}
    send_fn: SendFn = send or email_tools.send_email
    send_result = await asyncio.to_thread(
        send_fn, draft.get("recipient", ""), draft.get("subject", ""), draft.get("body", "")
    )

    payload: dict[str, Any] = {"run_id": run_id, "invoice_id": invoice_id, "approver": approver}
    result = await _approve_tx(client.transaction(), ref, run_ref, payload, approver, send_result)

    # Post-decision learning (non-fatal): the human just confirmed this
    # dispute was correct, so persist positive facts for the next run.
    if result.get("status") == "approved":
        fields = await learning._read_invoice_fields(client, run_id, invoice_id)
        result["learning"] = await learning.record_approval_facts(
            client=client,
            vendor=fields.get("vendor"),
            invoice_number=fields.get("invoice_number"),
            total=fields.get("total"),
            discrepancy_types=fields.get("discrepancy_types"),
            account_codes=fields.get("account_codes"),
        )
    return result


@firestore.async_transactional
async def _reject_tx(
    transaction: firestore.AsyncTransaction,
    ref: firestore.AsyncDocumentReference,
    payload: dict[str, Any],
    approver: str,
    reason: str,
) -> dict[str, Any]:
    snap = await ref.get(transaction=transaction)
    if not snap.exists:
        return {"status": "not_found"}
    doc = snap.to_dict() or {}
    resolution = (doc.get("stages_data") or {}).get("resolution") or {}
    if resolution.get("outcome") != "disputed":
        return {"status": "already_decided", "outcome": resolution.get("outcome")}
    resolution.update(
        {
            "human_decision": "rejected",
            "human_decided_by": approver,
            "human_decided_at": _now_iso(),
            "human_rejection_reason": reason,
            "outcome": "escalated",
        }
    )
    transaction.update(ref, {"stages_data.resolution": resolution, "updated_at": firestore.SERVER_TIMESTAMP})
    payload.update({"status": "rejected", "outcome": "escalated", "reason": reason})
    return payload


async def reject(
    run_id: str,
    invoice_id: str,
    reason: str,
    approver: str = "human",
    client: firestore.AsyncClient | None = None,
) -> dict[str, Any]:
    """Reject a pending dispute; the reason feeds the P14 negative-fact loop."""
    client = client or get_firestore_client()
    ref = client.collection(RUN_INVOICES_COLLECTION).document(f"{run_id}_{invoice_id}")
    payload: dict[str, Any] = {"run_id": run_id, "invoice_id": invoice_id, "approver": approver}
    result = await _reject_tx(client.transaction(), ref, payload, approver, reason or "(no reason given)")

    # Post-decision learning (non-fatal): the human rejected this dispute,
    # so persist a negative fact so the agent does not repeat the attempt.
    if result.get("status") == "rejected":
        fields = await learning._read_invoice_fields(client, run_id, invoice_id)
        result["learning"] = await learning.record_rejection_fact(
            client=client,
            vendor=fields.get("vendor"),
            invoice_number=fields.get("invoice_number"),
            reason=reason or "(no reason given)",
        )
    return result
