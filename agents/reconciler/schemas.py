"""Reconciler structured-output schemas (Pydantic).

These models double as the ADK ``output_schema`` for specialist agents and as
the validation contract the smoke tests assert against. They are the wire format
between pipeline stages — every stage emits JSON shaped like one of these.

Design choice: every leaf value is Optional (``| None = None``). This is an
explicit anti-hallucination posture — the model is PRIVILEGED with the option to
return ``null`` for anything it cannot read, so the Instruction Contract's
"never fabricate; missing -> null" rule is enforceable at the schema level, not
just the prompt level.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    """One line on an invoice."""

    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    # `amount` is the line total = quantity * unit_price (or the stated amount).
    amount: float | None = None


class Invoice(BaseModel):
    """Structured representation of a vendor invoice, extracted from a PDF.

    This is the Extraction agent's ``output_schema``. Native Gemini
    ``response_schema`` enforcement (API-level) guarantees the reply is JSON of
    this shape; ``temperature=0.0`` makes it deterministic.
    """

    vendor: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None  # ISO YYYY-MM-DD when present
    due_date: str | None = None
    currency: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    notes: str | None = None


class ExtractionResult(BaseModel):
    """Envelope the Extraction agent returns to the Supervisor.

    Carries the parsed invoice plus extraction-level metadata the downstream
    stages (Verification, Categorization, Reconciliation) need: a confidence
    score and the list of fields that could not be read (so the CoVe
    Verification agent knows where to focus).
    """

    invoice: Invoice
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="self-assessed extraction confidence"
    )
    missing_fields: list[str] = Field(default_factory=list)
    # Content hash of the source PDF, used for idempotency (Phase 4) and dedupe.
    # Computed by the Intake stage, echoed through here.
    source_hash: str | None = None


# ---------------------------------------------------------------------------
# Phase 3 — Verification (CoVe) schemas
# ---------------------------------------------------------------------------

# Closed set of discrepancy types — kept as a frozenset for O(1) membership
# checks (smoke + downstream code) and used as the basis of the
# ``Discrepancy.type`` Literal so the *native API* response_schema ALSO rejects
# any invented type (third enforcement layer after the instruction list and the
# smoke assertions). With temp=0.0 + an explicit instruction listing these
# exact strings, case drift is unlikely; the Literal additionally closes the
# "model invents 'vendor_typo' / 'weird_charge'" hole at the API layer.
DISCREPANCY_TYPES = frozenset(
    {
        "amount_mismatch",
        "vendor_mismatch",
        "date_mismatch",
        "invoice_number_mismatch",
        "duplicate_payment",
        "no_bank_match",
        "extra_invoice_line",
    }
)

# Canonical Pydantic Literal form of DISCREPANCY_TYPES (single definition;
# ReportDiscrepancyType and the resolution schemas alias it).
DiscrepancyType = Literal[
    "amount_mismatch",
    "vendor_mismatch",
    "date_mismatch",
    "invoice_number_mismatch",
    "duplicate_payment",
    "no_bank_match",
    "extra_invoice_line",
]


class Discrepancy(BaseModel):
    """A single discrepancy the Verification agent flags.

    Anti-hallucination posture: every field is Optional so a flag can say "we
    saw a mismatch" without inventing values the agent could not source.
    ``type`` is a Literal bound to DISCREPANCY_TYPES so the native API
    response_schema rejects invented discrepancy kinds at the model layer;
    ``invoice_value`` / ``bank_value`` echo exactly what each side said (as
    strings to avoid float-repr drift).
    """

    type: (
        Literal[
            "amount_mismatch",
            "vendor_mismatch",
            "date_mismatch",
            "invoice_number_mismatch",
            "duplicate_payment",
            "no_bank_match",
            "extra_invoice_line",
        ]
        | None
    ) = None
    description: str | None = None
    invoice_value: str | None = None
    bank_value: str | None = None


class VerificationResult(BaseModel):
    """Envelope the Verification (CoVe) agent returns to the Supervisor.

    Cross-checks an extracted ``Invoice`` against a bank-statement CSV using the
    Chain-of-Verification pattern: draft a provisional match, plan verification
    questions, answer each INDEPENDENTLY (not conditioned on the draft), then
    revise. The ``verification_questions`` and ``verification_answers`` lists are
    the auditable CoVe trace — they prove the agent did not just rubber-stamp
    its own first draft.
    """

    matched: bool = False
    matched_amount: float | None = None
    matched_date: str | None = None  # ISO YYYY-MM-DD
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    # CoVe trace — the planned questions and their independently-derived
    # answers. Same length; zip() together in the audit.
    verification_questions: list[str] = Field(default_factory=list)
    verification_answers: list[str] = Field(default_factory=list)
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="self-assessed verification confidence"
    )
    revised: bool = False  # True if the draft was revised based on verification


# ---------------------------------------------------------------------------
# Phase 6 — Reporting + Safety schemas
# ---------------------------------------------------------------------------

# Discrepancy type strings allowed in the digest (aliases the canonical
# DiscrepancyType Literal; kept for Phase 6 import compatibility).
ReportDiscrepancyType = DiscrepancyType


class FlaggedItem(BaseModel):
    """One item escalated in the weekly digest for human review.

    A flag is set if: a discrepancy was found, verification confidence was below
    the HITL Tier-1 threshold, or a HITL flag was annotated in session state by
    the middleware's ``after_model_callback``.
    """

    invoice_number: str | None = None
    vendor: str | None = None
    discrepancy_type: ReportDiscrepancyType | None = None
    description: str | None = None
    invoice_value: str | None = None
    bank_value: str | None = None
    confidence: float | None = None


class ReportingResult(BaseModel):
    """Envelope the Reporting agent returns — the weekly digest composition +
    send status.

    ``email_sent`` is True only if the ``send_digest_email`` tool was called
    AND the human approved via HITL Tier-2. ``email_blocked_by_hitl`` is True
    if the human rejected the send (or the run ended without approval).
    """

    digest_composed: bool = False
    flagged_items: list[FlaggedItem] = Field(default_factory=list)
    total_invoices: int = 0
    flagged_count: int = 0
    email_sent: bool = False
    email_blocked_by_hitl: bool = False
    recipient: str | None = None
    subject: str | None = None
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="self-assessed reporting confidence"
    )

# ---------------------------------------------------------------------------
# Phase 3.5 — Categorization / Intake / Reconciliation schemas
# ---------------------------------------------------------------------------

# The closed chart of accounts. The Categorization agent MUST assign codes from
# this list only — a code outside it is a contract violation (anti-hallucination
# grounding: never invent account codes).
CHART_OF_ACCOUNTS: dict[str, str] = {
    "5000": "Cloud Infrastructure (compute, storage, network)",
    "5010": "AI & API Services (model tokens, SaaS APIs)",
    "5100": "Software Subscriptions (licenses, seats)",
    "5200": "Developer Tools & Hosting",
    "6000": "Professional Services (consulting, legal, support)",
    "6100": "Office Supplies & Equipment",
    "6200": "Travel & Entertainment",
    "6300": "Marketing & Advertising",
    "7000": "Payroll & Contractor Compensation",
    "9000": "Uncategorized (requires human review)",
}

AccountCode = Literal[
    "5000", "5010", "5100", "5200", "6000", "6100", "6200", "6300", "7000", "9000",
]


class CategorizedLineItem(BaseModel):
    """A single line item mapped to the chart of accounts."""

    description: str | None = None
    account_code: AccountCode | None = None
    account_name: str | None = None
    rationale: str | None = None  # one line: why this code
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class CategorizationResult(BaseModel):
    """Envelope the Categorization agent returns.

    ``known_vendor_mappings`` echo the SharedMemory-grounded vendor→code hints
    that were injected into the prompt (provenance for the audit trail).
    ``unassigned_count`` counts items left null / 9000 — null is first-class:
    the agent never guesses an account code it cannot justify.
    """

    items: list[CategorizedLineItem] = Field(default_factory=list)
    unassigned_count: int = 0
    known_vendor_mappings: list[str] = Field(default_factory=list)  # e.g. ["Acme Cloud Services LLC=5000"]
    confidence: float = Field(..., ge=0.0, le=1.0)


class InvoiceAttachment(BaseModel):
    """One invoice document discovered by the Intake stage."""

    source: Literal["gmail", "local_dir"] = "local_dir"
    message_id: str | None = None  # gmail message id; local: filename
    filename: str | None = None
    mime_type: str | None = None
    sha256: str | None = None  # idempotency / dedupe key material


class IntakeResult(BaseModel):
    """Envelope the Intake stage returns: invoices found this run."""

    invoices: list[InvoiceAttachment] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)  # fetch failures never crash the run


ReconciliationVerdict = Literal["matched", "discrepancy", "needs_review"]


class ReconciliationResult(BaseModel):
    """FINAL per-invoice verdict consolidating extraction + verification +
    categorization.

    Invariants (checked + reported, never assumed):
      INV-1 sum(line_items.amount) == subtotal (±$0.02)
      INV-2 subtotal + tax == total (±$0.02)
      INV-3 every line item has an account code or is explicitly 9000/null
      INV-4 verdict=matched ONLY if verification.matched and no discrepancies
    """

    invoice_number: str | None = None
    vendor: str | None = None
    verdict: ReconciliationVerdict | None = None
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    invoice_total: float | None = None
    bank_total: float | None = None
    account_codes_assigned: bool = False
    invariants_checked: list[str] = Field(default_factory=list)
    invariants_passed: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Closed-loop resolution (design doc §1) — resolve-then-escalate, never flag-and-stop
# ---------------------------------------------------------------------------

ResolutionLane = Literal["resolve", "dispute", "escalate"]
ResolutionOutcome = Literal["resolved", "disputed", "escalated"]
HumanDecision = Literal["approved", "rejected"]


class ResolutionDecision(BaseModel):
    """The auditable WHY of a resolution lane choice (design doc §1.2).

    Pure function inputs: f(discrepancy_type, confidence, evidence_available,
    action_risk) → lane. ``rationale`` is REQUIRED — a lane without a reason
    is unauditable.
    """

    discrepancy_type: DiscrepancyType | None = None
    lane: ResolutionLane | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)  # memory keys / source hashes consulted
    rationale: str | None = None  # REQUIRED — the "why" (provenance)


class DisputeDraft(BaseModel):
    """A corrective email DRAFTED but never sent. The resolution agent has no
    send capability — only the HITL approval surface (design doc §5) commits
    the external side effect after an explicit human click."""

    recipient: str | None = None
    subject: str | None = None
    body: str | None = None
    amount_at_risk: float | None = None  # feeds dollars_recovered (approved-only)


class ResolutionAction(BaseModel):
    """One discrepancy's journey through the closed loop.

    outcome=resolved is ONLY permitted when ``recheck_matched`` shows an
    independent re-verification pass confirmed the discrepancy is gone
    (design doc §1.6 — never self-certify).
    """

    decision: ResolutionDecision | None = None
    corrected_invoice: Invoice | None = None  # only for lane=resolve
    dispute_draft: DisputeDraft | None = None  # only for lane=dispute
    recheck_matched: bool | None = None  # from the independent re-verification pass
    outcome: ResolutionOutcome | None = None


class ProvenanceEntry(BaseModel):
    """Audit trail entry for a resolved/disputed/escalated discrepancy
    (design doc §3). Chains extraction evidence → CoVe verification →
    resolution decision → re-verification → (optional) human decision."""

    discrepancy_type: DiscrepancyType | None = None
    lane: ResolutionLane | None = None
    extraction_hash: str | None = None  # source_hash from ExtractionResult
    verification_questions: list[str] = Field(default_factory=list)  # CoVe questions
    verification_answers: list[str] = Field(default_factory=list)  # CoVe answers (independent pass)
    memory_keys_consulted: list[str] = Field(default_factory=list)  # vendor/prior_invoice lookups
    rule_fired: str | None = None  # e.g. "fuzzy_match(vendor, alias) @ 0.94"
    resolution_rationale: str | None = None  # from ResolutionDecision.rationale
    recheck_matched: bool | None = None  # the closing-the-loop evidence
    human_decision: HumanDecision | None = None  # only if disputed
    trace_id: str | None = None  # Cloud Trace linkage (click a decision → see the span)
    timestamp: str | None = None
