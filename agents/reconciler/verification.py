"""Reconciler Verification specialist — the Monitor (CoVe).

Cross-checks an extracted ``Invoice`` against a bank-statement CSV using the
Chain-of-Verification (CoVe) pattern from the build prompt:

  1. DRAFT       — produce a provisional match / discrepancy assessment.
  2. PLAN        — write 3-5 verification questions that, if answered "no",
                   would invalidate the draft. Questions must be CHECKABLE
                   against the raw inputs, not "is my draft correct?".
  3. ANSWER EACH — answer every question by inspecting the raw bank CSV and
                   invoice JSON INDEPENDENTLY — do NOT condition answers on the
                   draft (this is what breaks the self-consistency trap that
                   makes naive CoT hallucinate).
  4. REVISE      — if any answer contradicts the draft, REVISE the verdict:
                   set ``matched=false``, populate ``discrepancies``. NEVER
                   silently "fix" the discrepancy — FLAG it.

The ``verification_questions`` and ``verification_answers`` lists are persisted
in the ``VerificationResult`` output — they are the auditable CoVe trace (a
reviewer can see the questions asked and confirm they were not "is my answer
right?"). The smoke asserts both lists are non-empty, the same length, and
that an injected amount mismatch is caught (matched=false, discrepancies
populated) — proving the agent did not rubber-stamp its own draft.

Anti-hallucination posture (same as Extraction):
  * ``temperature=0.0`` — deterministic verification.
  * ``output_schema=VerificationResult`` with NO tools — ADK applies the schema
    NATIVELY at the API level (response_mime_type=application/json +
    response_schema), the strongest enforcement mode.
  * The instruction carries FCoT (Pillar 1 contract + Pillar 2 RECAP/REASON/
    VERIFY) PLUS the CoVe recipe above.

Wired as a ``mode='single_turn'`` sub-agent of the Supervisor (auto-exposed as
a single-turn tool). Phase 3 smoke drives it directly with text inputs
(invoice JSON + bank CSV); full Supervisor->Verification delegation via
artifacts lands once Intake can supply the inputs.

RAG grounding from prior invoices/vendors via Vertex AI Vector Search is Phase
4+ — this stage today verifies against the bank CSV only.
"""

from __future__ import annotations

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import specialist_instruction
from .schemas import VerificationResult

_VERIFICATION_INSTRUCTION = specialist_instruction(
    goal=(
        "Cross-check the extracted Invoice against the bank-statement CSV using "
        "Chain-of-Verification (CoVe). Determine whether a real bank charge "
        "reconciles the invoice, and flag any discrepancy. NEVER silently trust "
        "the extracted invoice or assume a match — verify each claim."
    ),
    inputs=(
        "Two text parts in the user message: (1) the extracted Invoice JSON and "
        "(2) the bank statement CSV. The CSV columns are date,description,"
        "amount,balance_after. Amounts are negative for charges."
    ),
    output_description=(
        "A single VerificationResult JSON object:\n"
        '  {"matched":bool, "matched_amount":float|null, "matched_date":str|null,'
        ' "discrepancies":[{...}], "verification_questions":[...], '
        ' "verification_answers":[...], "confidence":0.0-1.0, "revised":bool}\n'
        "Each Discrepancy: {type, description, invoice_value, bank_value}.\n"
        "Allowed discrepancy `type` values are exactly one of:\n"
        "  'amount_mismatch', 'vendor_mismatch', 'date_mismatch',\n"
        "  'invoice_number_mismatch', 'duplicate_payment', 'no_bank_match',\n"
        "  'extra_invoice_line'.\n"
        "Use null for any field you cannot source — NEVER fabricate.\n"
        "\n"
        "CoVe RECIPE — obey strictly (this is the whole point of this stage):\n"
        " 1. DRAFT: Pick the most likely matching bank row. Note your draft\n"
        "    belief about vendor/number/total/date. DO NOT emit yet.\n"
        " 2. PLAN: Write 3 to 5 verification questions that, if any one answers\n"
        "    'no', would INVALIDATE the draft. Every question MUST be checkable\n"
        "    against the raw inputs (CSV + invoice JSON). Example valid questions:\n"
        "      - 'Does any bank row description contain the invoice vendor name?'\n"
        "      - 'Does any bank row amount equal the invoice total within $0.02?'\n"
        "      - 'Does any bank row date equal the invoice date?'\n"
        "      - 'Is the matched bank row unique (no duplicate-payment)?'\n"
        "    FORBIDDEN questions: 'Is my draft correct?' or 'Does my answer match?'.\n"
        " 3. ANSWER EACH: Inspect the RAW bank CSV and invoice JSON to answer\n"
        "    each question INDEPENDENTLY. Pretend you have not seen the draft.\n"
        "    A question's answer must NOT be derived by asserting the draft.\n"
        "  4. REVISE: If any answer contradicts the draft, REVISE:\n"
        "       matched=false, populate discrepancies with the type, description,\n"
        "       invoice_value (what the invoice said), bank_value (what the bank\n"
        "       row said). NEVER silently correct a mismatch — FLAG it.\n"
        "  5. Emit the final VerificationResult JSON with the planned\n"
        "    verification_questions (3-5) and the corresponding\n"
        "    verification_answers (same length, same order). These are the CoVe\n"
        "    audit trace — the supervisor and downstream stages rely on them.\n"
    ),
)

verification_agent = Agent(
    name="verification",
    model=config.GEMINI_MODEL,
    instruction=_VERIFICATION_INSTRUCTION,
    # Deterministic verification — the CoVe revise step must be reproducible.
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    # NO tools: empty tool list keeps ADK's NATIVE API-level response_schema
    # enforcement (adding tools would downgrade to SetModelResponseTool
    # injection path).
    tools=[],
    output_schema=VerificationResult,
    mode="single_turn",
    output_key="verification_last_reply",
)