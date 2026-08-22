"""Closed-loop learning — human-in-the-loop fact writes (closed-loop design §6).

The autonomous half of learning already lives in ``pipeline.py``: after a
Verification pass confirms a stage output, the pipeline writes the vendor's
``account_codes`` and ``invoice_numbers_seen`` into SharedMemory. This module
is the *human* half: when a human approves or rejects a dispute (via
``approvals.py``), their decision is persisted as a fact so the next run
resolves the same shape of discrepancy with less friction.

Anti-gaming (§9) is structural, not advisory:

* A fact is a **hint**, never a bypass — the Resolution agent still runs its
  independent re-verification pass (§1.6) before anything is marked resolved.
* Only **approved** decisions write *positive* facts; only **rejected**
  decisions write *negative* facts. Neither ever writes a speculative guess.
* Learning is fire-and-forget here: it runs *after* the decision transaction
  commits and is wrapped non-fatally — a fact-write failure can never roll
  back or block a human decision.
"""

from __future__ import annotations

from typing import Any

from google.cloud import firestore

from .memory import RUN_INVOICES_COLLECTION, SharedMemory

# Namespaces (closed vocabulary, mirrors memory.py):
NS_VENDOR = "vendor"
NS_PRIOR_INVOICE = "prior_invoice"


def _invoice_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract the learning-relevant fields from a run_invoices Firestore doc."""
    stages = doc.get("stages_data") or {}
    extraction = stages.get("extraction") or {}
    invoice = extraction.get("invoice") or {}
    verification = stages.get("verification") or {}
    categorization = stages.get("categorization") or {}
    resolution = stages.get("resolution") or {}

    discrepancies = [
        d.get("type") for d in (resolution.get("recheck") or {}).get("discrepancies", [])
    ]
    if not discrepancies:
        discrepancies = [d.get("type") for d in verification.get("discrepancies", [])]

    account_codes = sorted(
        {i.get("account_code") for i in (categorization.get("items") or []) if i.get("account_code")}
    )

    return {
        "vendor": invoice.get("vendor"),
        "invoice_number": invoice.get("invoice_number"),
        "total": invoice.get("total"),
        "discrepancy_types": discrepancies,
        "account_codes": account_codes,
    }


async def _read_invoice_fields(
    client: firestore.AsyncClient, run_id: str, invoice_id: str
) -> dict[str, Any]:
    ref = client.collection(RUN_INVOICES_COLLECTION).document(f"{run_id}_{invoice_id}")
    snap = await ref.get()
    if not snap.exists:
        return {}
    return _invoice_fields(snap.to_dict() or {})


async def record_approval_facts(
    *,
    client: firestore.AsyncClient,
    vendor: str | None,
    invoice_number: str | None,
    total: float | None,
    discrepancy_types: list[str] | None,
    account_codes: list[str] | None,
) -> dict[str, Any]:
    """Persist the facts a human just confirmed by approving a dispute.

    Positive facts only (the human said "yes, this is correct"):

    * ``prior_invoice:{invoice_number}`` → ``{vendor, total, resolved: True}``
      — feeds duplicate_payment detection and prior-invoice lookups next run.
    * ``vendor:{vendor}`` → ``{account_codes, approved_invoices:[...]}``
      — reinforces the categorization memory and records the approval.

    Merge=True deep-merges so repeated approvals extend (never clobber) the
    record. Returns a summary; never raises.
    """
    memory = SharedMemory(client)
    facts: list[str] = []
    try:
        if invoice_number:
            await memory.set_fact(
                namespace=NS_PRIOR_INVOICE,
                key=invoice_number,
                value={"vendor": vendor, "total": total, "resolved": True},
                merge=True,
            )
            facts.append(f"{NS_PRIOR_INVOICE}:{invoice_number}")
        if vendor:
            value: dict[str, Any] = {}
            if account_codes:
                value["account_codes"] = account_codes
            if invoice_number:
                value["approved_invoices"] = [invoice_number]
            if value:
                await memory.set_fact(namespace=NS_VENDOR, key=vendor, value=value, merge=True)
                facts.append(f"{NS_VENDOR}:{vendor}")
        return {"status": "recorded", "facts": facts}
    except Exception as exc:  # learning is never fatal
        return {"status": "error", "error": str(exc), "facts": facts}


async def record_rejection_fact(
    *,
    client: firestore.AsyncClient,
    vendor: str | None,
    invoice_number: str | None,
    reason: str,
) -> dict[str, Any]:
    """Persist a negative fact after a human rejects a dispute.

    Negative facts only (the human said "no, this is wrong"):

    * ``prior_invoice:{invoice_number}`` → ``{NOT_resolved: True, reason}``
      — so the agent does not repeat the same resolution attempt.
    * ``vendor:{vendor}`` → ``{rejected_invoices:[...]}``
      — records that a dispute against this vendor was declined.

    Returns a summary; never raises.
    """
    memory = SharedMemory(client)
    facts: list[str] = []
    try:
        if invoice_number:
            await memory.set_fact(
                namespace=NS_PRIOR_INVOICE,
                key=invoice_number,
                value={"NOT_resolved": True, "reason": reason},
                merge=True,
            )
            facts.append(f"{NS_PRIOR_INVOICE}:{invoice_number}")
        if vendor and invoice_number:
            await memory.set_fact(
                namespace=NS_VENDOR,
                key=vendor,
                value={"rejected_invoices": [invoice_number]},
                merge=True,
            )
            facts.append(f"{NS_VENDOR}:{vendor}")
        return {"status": "recorded", "facts": facts}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "facts": facts}
