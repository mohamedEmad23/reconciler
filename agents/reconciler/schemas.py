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
    # SHAKE-free content hash of the source PDF, used for idempotency (Phase 4)
    # and dedupe. Computed by the Intake stage, echoed through here.
    source_hash: str | None = None