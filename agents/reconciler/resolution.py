"""Resolution agent — the closed-loop core (design doc §1).

Resolve-then-escalate, never flag-and-stop. For every discrepancy the
Verification (CoVe) agent flags, this specialist decides ONE lane:

  * resolve  — the agent has the evidence + a low-risk correction to apply.
               The pipeline applies ``corrected_invoice`` and then RE-RUNS
               verification; the outcome is only "resolved" if the
               independent re-verification pass confirms the discrepancy is
               GONE (design doc §1.6 — never self-certify).
  * dispute  — the agent knows something is wrong and can DRAFT the
               corrective action (email text + amount at risk), but executing
               is high-stakes (money moves, external email). The draft is
               persisted for the HITL approval surface; the agent itself has
               NO send capability.
  * escalate — genuinely ambiguous / missing evidence. Abstention is a
               FIRST-CLASS successful terminal state (anti-gaming guard §9),
               never a failure to be forced into a resolution.

Decision is a pure function of four inputs — f(discrepancy_type, confidence,
evidence_available, action_risk) — with thresholds from ``middleware.py``
(DISPUTE_THRESHOLD=0.70, RESOLVE_THRESHOLD=0.90): one visible place a judge
can see exactly where the agent draws its autonomy lines.

Deterministic evidence (vendor-alias fuzzy match, OCR-transposition check,
date-window check, prior-invoice memory facts) is computed in Python by the
pipeline and supplied in the prompt; the agent DECIDES + DRAFTS, the
pipeline EXECUTES + RE-VERIFIES. The agent never mutates shared data or
sends anything — by construction, not by prompt.
"""

from __future__ import annotations

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import specialist_instruction
from .middleware import DISPUTE_THRESHOLD, RESOLVE_THRESHOLD
from .schemas import ResolutionAction

_DECISION_TABLE = f"""
RESOLUTION DECISION TABLE (pure function: f(type, confidence, evidence, risk)):

| confidence | evidence | risk  | lane    | action                                   |
|------------|----------|-------|---------|------------------------------------------|
| >= {RESOLVE_THRESHOLD:.2f}   | --       | low   | resolve | corrected_invoice + rationale            |
| >= {RESOLVE_THRESHOLD:.2f}   | --       | high  | dispute | dispute_draft -> HITL approve            |
| {DISPUTE_THRESHOLD:.2f}-{RESOLVE_THRESHOLD:.2f}  | yes      | low   | resolve | corrected_invoice (conditional) + rationale |
| {DISPUTE_THRESHOLD:.2f}-{RESOLVE_THRESHOLD:.2f}  | yes      | high  | dispute | dispute_draft -> HITL approve            |
| {DISPUTE_THRESHOLD:.2f}-{RESOLVE_THRESHOLD:.2f}  | no       | --    | escalate | rationale says what evidence is missing  |
| < {DISPUTE_THRESHOLD:.2f}    | --       | --    | escalate | (Tier-1 HITL already flagged it)          |

DISCREPANCY TYPE -> CANONICAL ACTION (spec §1.3):
  amount_mismatch        — often resolvable: bank value may be a transposition/
                           fuzzy match of the extracted value; evidence packet
                           includes the fuzzy-match result. If evidence
                           corroborates the bank reading, correct the amount.
  vendor_mismatch        — often resolvable: entity resolution against vendor
                           aliases in SharedMemory (evidence packet includes
                           alias facts). Canonicalize vendor if an alias matches.
  date_mismatch          — often resolvable: invoice date vs bank posting date
                           within a 1-3 day window is normal ACH/card latency
                           (evidence packet includes the day delta).
  invoice_number_mismatch— sometimes: OCR digit transposition (0<->O, 1<->l);
                           evidence packet includes the fuzzy string match.
  duplicate_payment      — HIGH VALUE, ALWAYS HIGH RISK: draft a dispute email
                           for the duplicate amount. NEVER lane=resolve. This
                           is the discrepancy that recovers dollars.
  no_bank_match          — sometimes: if no fetched statement evidence exists
                           in the packet, escalate as unmatched.
  extra_invoice_line     — often: if the evidence packet shows the line is a
                           stray/mis-bounded artifact, drop it with rationale.

SAFETY (non-negotiable):
  - You NEVER send email. You only DRAFT (recipient/subject/body/amount_at_risk).
    Sending is executed exclusively by the human approval surface after an
    explicit Approve click.
  - You NEVER claim outcome=resolved. The pipeline sets it only after an
    independent re-verification pass confirms the discrepancy is gone.
    Your job: decide the lane + produce the action artifact + rationale.
  - rationale is REQUIRED for every decision — a lane without a reason is
    unauditable. Cite the evidence (fuzzy scores, memory facts, day deltas).
  - Evidence that is NOT in your prompt does not exist. Do not invent vendors,
    amounts, or prior invoices. Missing evidence -> escalate.
"""

resolution_agent = Agent(
    name="resolution",
    model=config.GEMINI_MODEL,
    instruction=specialist_instruction(
        goal=(
            "decide the resolution lane for each flagged invoice discrepancy "
            "and produce the action artifact (corrected invoice OR dispute "
            "draft) with an auditable rationale"
        ),
        inputs=(
            "one flagged discrepancy with: its type, the verification "
            "confidence, the CoVe question/answer trace, the extracted "
            "invoice JSON, the relevant bank-statement rows, and a "
            "deterministic EVIDENCE PACKET computed in Python (vendor-alias "
            "fuzzy match scores, prior-invoice facts from shared memory, "
            "date-window deltas, OCR-transposition checks)"
        ),
        output_description=(
            "ResolutionAction JSON. decision.lane is exactly one of "
            "resolve|dispute|escalate per the decision table. "
            "lane=resolve -> corrected_invoice holds the FULL corrected "
            "invoice (only the corroborated fields changed). lane=dispute -> "
            "dispute_draft holds a ready-to-review email (recipient, subject, "
            "body citing the evidence, amount_at_risk). lane=escalate -> "
            "rationale names the missing evidence. decision.rationale always "
            "cites concrete evidence from the packet."
        ),
    )
    + "\n"
    + _DECISION_TABLE,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[],
    output_schema=ResolutionAction,
    mode="single_turn",
    output_key="resolution_last_reply",
)
