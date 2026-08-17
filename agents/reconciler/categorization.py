"""Categorization specialist — maps invoice line items to the chart of accounts.

Anti-hallucination posture (identical to Extraction/Verification):
  - temperature=0.0 (deterministic assignment)
  - tools=[] + output_schema → NATIVE API-level response_schema enforcement
  - account codes restricted to the closed ``AccountCode`` Literal — the model
    CANNOT emit an invented code; it physically fails schema validation
  - SharedMemory grounding: prior vendor→code mappings are injected as text
    (the orchestrator composes them into the user turn); the agent echoes what
    it used into ``known_vendor_mappings`` for the audit trail
  - never guesses: an unjustifiable line item stays null / 9000 (Uncategorized)
    and lands in ``unassigned_count`` for human review
"""

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import specialist_instruction
from .schemas import CHART_OF_ACCOUNTS, CategorizationResult

_COA_TEXT = "\n".join(f"  {code} — {name}" for code, name in sorted(CHART_OF_ACCOUNTS.items()))

categorization_agent = Agent(
    name="categorization",
    model=config.GEMINI_MODEL,
    instruction=specialist_instruction(
        goal=(
            "assign each invoice line item an account code from the CLOSED "
            "chart of accounts below"
        ),
        inputs=(
            "an extracted Invoice JSON (line_items with descriptions, amounts) "
            "plus optional known vendor→code mappings from shared memory"
        ),
        output_description=(
            "CategorizationResult JSON. CHART OF ACCOUNTS (the ONLY legal codes — "
            "a code outside this list violates the output schema and is rejected):\n"
            f"{_COA_TEXT}\n"
            "Rules: (1) if a known vendor mapping covers the vendor, follow it "
            "and echo it in known_vendor_mappings; (2) assign the code whose "
            "definition matches the line item's substance, not keyword overlap — "
            "'Premier Support' is Professional Services (6000) not support "
            "software; (3) if you cannot justify a code, leave account_code "
            "null or '9000' and increment unassigned_count — NEVER guess; "
            "(4) rationale is one short line citing the COA definition."
        ),
    ),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[],
    output_schema=CategorizationResult,
    mode="single_turn",
    output_key="categorization_last_reply",
)
