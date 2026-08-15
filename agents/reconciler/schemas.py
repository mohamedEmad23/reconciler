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

# Discrepancy type strings allowed in the digest (mirrors DISCREPANCY_TYPES but
# represented as a Pydantic Literal for the reporting agent's output schema).
ReportDiscrepancyType = Literal[
    "amount_mismatch",
    "vendor_mismatch",
    "date_mismatch",
    "invoice_number_mismatch",
    "duplicate_payment",
    "no_bank_match",
    "extra_invoice_line",
]


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