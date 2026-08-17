"""Reconciler root agent — the Supervisor.

The Supervisor is the ONLY agent exposed to the ADK API server
(``adk api_server agents/reconciler``). It holds the Instruction Contract and
routes work to single-turn specialists. Each specialist is added as a
``sub_agents=[...]`` entry with ``mode='single_turn'`` so the ADK framework
auto-exposes it as a tool callable by the Supervisor (``_SingleTurnAgentTool``
runs it inline in the Supervisor's session — shared state, no separate runner).

Safety rails (Phase 6):
  Every specialist agent is wrapped with ``with_safety_rails()`` which
  attaches PII-redaction (before_model_callback) and HITL Tier-1
  (after_model_callback low-confidence flag) — the agent cannot opt out.
  The Reporting agent additionally carries HITL Tier-2 (before_tool_callback
  on send_digest_email → request_confirmation → framework pause).

Specialists online:
  Phase 2  — Extraction     (PDF -> structured invoice JSON, temp=0.0, output_schema)
  Phase 3  — Verification   (CoVe cross-check against bank-statement CSV)
  Phase 6  — Reporting      (weekly digest email, FINAL HITL Tier-2 gate before send)
  Phase 3.5 — Intake        (Gmail OAuth via Secret Manager / local dir tools)
  Phase 3.5 — Categorization(chart of accounts, substance-over-keyword)
  Phase 3.5 — Reconciliation(final verdict, INV-1..INV-4 invariants)

The batch execution spine lives in ``pipeline.py`` (triggered by Pub/Sub);
the Supervisor exposes the same specialists as tools for interactive use.
"""

from __future__ import annotations

from google.adk import Agent
from google.genai import types

from . import config
from .categorization import categorization_agent
from .extraction import extraction_agent
from .instruction_contract import INSTRUCTION_CONTRACT
from .intake import intake_agent
from .middleware import with_safety_rails
from .reconciliation import reconciliation_agent
from .reporting import reporting_agent
from .verification import verification_agent

# ---------------------------------------------------------------------------
# Apply safety rails to every specialist (PII redaction + HITL Tier-1 flag).
# model_copy(update=...) preserves tools/output_schema/mode/output_key and only
# sets the two callback lists. model_post_init does NOT re-run on copies, so
# sub_agents wrapping in the originals is preserved.
# ---------------------------------------------------------------------------
_extraction_wrapped = with_safety_rails(extraction_agent)
_verification_wrapped = with_safety_rails(verification_agent)
_categorization_wrapped = with_safety_rails(categorization_agent)
_reconciliation_wrapped = with_safety_rails(reconciliation_agent)
# reporting_agent keeps its own before_tool_callback (HITL Tier 2) and also
# gets PII redaction + HITL Tier-1 overlaid by with_safety_rails.
_reporting_wrapped = with_safety_rails(reporting_agent)
# intake_agent is tool-driven (no output_schema): rails still apply so its
# model turns are PII-redacted and low-confidence flags fire.
_intake_wrapped = with_safety_rails(intake_agent)

_SUPERVISOR_INSTRUCTION = (
    INSTRUCTION_CONTRACT
    + "\n\n"
    + "You are the Reconciler Supervisor orchestrating a reconciliation run.\n"
    "A run has just been triggered. You have access to single-turn specialist\n"
    "tools (auto-exposed from your sub_agents):\n"
    "  - intake       : discover invoice PDFs (Gmail via Secret-Manager-isolated\n"
    "                   OAuth, or a local directory) and fetch their bytes.\n"
    "  - extraction   : extract a vendor invoice PDF into structured invoice JSON.\n"
    "  - verification : cross-check extracted invoice against bank-statement CSV\n"
    "                   using Chain-of-Verification (CoVe) — flags discrepancies,\n"
    "                   never silently trusts the extraction draft.\n"
    "  - categorization: assign every line item a chart-of-accounts code;\n"
    "                   never invents a code it cannot justify.\n"
    "  - reconciliation: final per-invoice verdict — recomputes the monetary\n"
    "                   invariants itself instead of trusting upstream stages.\n"
    "  - reporting    : compose a weekly digest escalating only flagged items;\n"
    "                   a FINAL HITL gate pauses before any email is sent.\n"
    "Per pipeline stage you delegate to the right specialist and forward its\n"
    "output to the next stage. Do NOT do intake, extraction, verification,\n"
    "categorization, reconciliation, or reporting yourself — delegate them.\n"
    "\n"
    "When responding to a run trigger where no invoices have been ingested yet,\n"
    "respond with a single JSON object and nothing else, of shape:\n"
    '  {"status": "ack", "run_id": "<short hex>", "plan": [<stage>, ...]}\n'
    "where `plan` is the ordered list of pipeline stages you WILL execute\n"
    "(intake, extraction, verification, categorization, reconciliation, reporting).\n"
    "Do NOT emit free-form prose. Do NOT invent invoice data — none exists yet."
)

# Specialists are single-turn sub-agents (auto-wrapped as single-turn tools).
# All six stages of the pipeline are wired; kept explicit so the wiring point
# is grep-able.
_SUB_AGENTS = [  # noqa: N806
    _intake_wrapped,
    _extraction_wrapped,
    _verification_wrapped,
    _categorization_wrapped,
    _reconciliation_wrapped,
    _reporting_wrapped,
]

root_agent = Agent(
    name="supervisor",
    model=config.GEMINI_MODEL,
    instruction=_SUPERVISOR_INSTRUCTION,
    # Deterministic orchestration skeleton: zero temperature so the ack/plan
    # shape is stable across runs (anti-hallucination posture, even here).
    # The Supervisor itself does NOT get PII/HITL callbacks — its only job is
    # to ack+route. The specialists hold the safety rails.
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    sub_agents=_SUB_AGENTS,
    output_key="supervisor_last_reply",
)