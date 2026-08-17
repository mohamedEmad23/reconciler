"""Reconciliation specialist — the FINAL per-invoice verdict.

Consolidates the three upstream envelopes (ExtractionResult,
VerificationResult, CategorizationResult) into one auditable row:
verdict (matched | discrepancy | needs_review), discrepancies, totals,
invariant checklist. Checks — never assumes — the monetary invariants:

  INV-1 sum(line_items.amount) == subtotal      (±$0.02)
  INV-2 subtotal + tax == total                 (±$0.02)
  INV-3 every line item coded, or explicitly null/9000
  INV-4 verdict=matched ONLY IF verification.matched AND no discrepancies
"""

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import specialist_instruction
from .schemas import ReconciliationResult

reconciliation_agent = Agent(
    name="reconciliation",
    model=config.GEMINI_MODEL,
    instruction=specialist_instruction(
        goal=(
            "emit the final per-invoice reconciliation verdict consolidating "
            "extraction + verification + categorization"
        ),
        inputs=(
            "three JSON envelopes: ExtractionResult (invoice fields, "
            "line_items with amounts), VerificationResult (matched, "
            "discrepancies, CoVe trace), CategorizationResult (per-item "
            "account codes)"
        ),
        output_description=(
            "ReconciliationResult JSON. CHECK the invariants by recomputing "
            "from the raw numbers given — do NOT assume upstream stages were "
            "right: INV-1 sum(line_items.amount)==subtotal (±0.02); INV-2 "
            "subtotal+tax==total (±0.02); INV-3 every line item has an "
            "account_code or is explicitly null/9000; INV-4 verdict may be "
            "'matched' ONLY if verification.matched is true AND "
            "discrepancies is empty — otherwise 'discrepancy' (discrepancies "
            "present) or 'needs_review' (invariant failure or missing data). "
            "List every invariant you checked in invariants_checked "
            "(['INV-1','INV-2','INV-3','INV-4']) and set invariants_passed "
            "only if all passed. Copy discrepancies verbatim from "
            "verification — never invent or silently fix one."
        ),
    ),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[],
    output_schema=ReconciliationResult,
    mode="single_turn",
    output_key="reconciliation_last_reply",
)
