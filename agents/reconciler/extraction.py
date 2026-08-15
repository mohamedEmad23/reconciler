"""Reconciler Extraction specialist.

Reads a vendor invoice PDF (multimodal: the PDF is sent inline as a
``Part.from_bytes`` content part) and produces a structured ``Invoice`` JSON
enveloped in an ``ExtractionResult`` (with a self-assessed confidence and the
list of fields it could not read).

Anti-hallucination posture (this is the whole point of Phase 2):
  * ``temperature=0.0`` — deterministic extraction.
  * ``output_schema=ExtractionResult`` with NO tools — triggers ADK's NATIVE
    API-level ``response_schema`` enforcement (the Gemini API itself rejects
    non-conforming JSON; the model cannot emit free-form prose).
  * Every schema leaf is Optional — the model is PRIVILEGED with ``null`` as a
    first-class answer, so "missing -> null" is enforceable, not just hoped for.
  * The instruction carries the FCoT contract (Pillar 1 immutable rules +
    Pillar 2 RECAP/REASON/VERIFY loop).
  * Decoy text in the invoice (a "NOTE" about a fake $1,000,000 line item) must
    NOT be extracted as a line item — the smoke asserts this.

Wired as a ``mode='single_turn'`` sub-agent of the Supervisor (auto-exposed as
a single-turn tool). Phase 2 smoke drives it directly with an inline PDF;
Phase 3 wires full Supervisor->Extraction delegation via artifacts once the
Intake specialist can supply the PDF bytes.
"""

from __future__ import annotations

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import specialist_instruction
from .schemas import ExtractionResult

_EXTRACTION_INSTRUCTION = specialist_instruction(
    goal=(
        "Extract a vendor invoice from the supplied PDF into the ExtractionResult "
        "JSON schema. Read the PDF. Do not guess values that are not printed in it."
    ),
    inputs=(
        "A single vendor invoice rendered as a PDF document, sent inline as a "
        "content part with mime_type application/pdf, followed by a text part."
    ),
    output_description=(
        "A single ExtractionResult JSON object:\n"
        '  {"invoice": {...}, "confidence": 0.0-1.0, "missing_fields": [...]}\n'
        "`invoice` fields: vendor, invoice_number, invoice_date (ISO YYYY-MM-DD), "
        "due_date, currency, line_items[], subtotal, tax, total, notes.\n"
        "Each line_item: {description, quantity, unit_price, amount}.\n"
        "Rules:\n"
        " - Use null for any value not present in the PDF — NEVER fabricate.\n"
        " - `confidence` reflects how cleanly you could read the invoice.\n"
        " - `missing_fields` lists field names that are absent or unreadable.\n"
        " - Notes printed on the invoice are NOT line items; put them in `notes`."
    ),
)

extraction_agent = Agent(
    name="extraction",
    model=config.GEMINI_MODEL,
    instruction=_EXTRACTION_INSTRUCTION,
    # Deterministic extraction — the single most important anti-hallucination
    # knob for this stage.
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    # NO tools: an empty tools list lets ADK apply the output_schema NATIVELY at
    # the API level (response_mime_type=application/json + response_schema), the
    # strongest enforcement mode. Adding tools would downgrade to the
    # SetModelResponseTool injection path.
    tools=[],
    output_schema=ExtractionResult,
    mode="single_turn",
    output_key="extraction_last_reply",
)