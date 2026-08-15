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
  Phase 2 — Extraction  (PDF -> structured invoice JSON, temp=0.0, output_schema)
  Phase 3 — Verification (CoVe cross-check against bank-statement CSV)
  Phase 6 — Reporting   (weekly digest email, FINAL HITL Tier-2 gate before send)
  Phase 3.5+ — Categorization, Reconciliation, Intake (deferred)
"""

from __future__ import annotations

from google.adk import Agent
from google.genai import types

from . import config
from .extraction import extraction_agent
from .instruction_contract import INSTRUCTION_CONTRACT
from .middleware import with_safety_rails
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
# reporting_agent keeps its own before_tool_callback (HITL Tier 2) and also
# gets PII redaction + HITL Tier-1 overlaid by with_safety_rails.
_reporting_wrapped = with_safety_rails(reporting_agent)

_SUPERVISOR_INSTRUCTION = (
    INSTRUCTION_CONTRACT
    + "\n\n"
    + "You are the Reconciler Supervisor orchestrating a reconciliation run.\n"
    "A run has just been triggered. You have access to single-turn specialist\n"
    "tools (auto-exposed from your sub_agents):\n"
    "  - extraction  : extract a vendor invoice PDF into structured invoice JSON.\n"
    "  - verification: cross-check extracted invoice against bank-statement CSV\n"
    "                  using Chain-of-Verification (CoVe) — flags discrepancies,\n"
    "                  never silently trusts the extraction draft.\n"
    "  - reporting   : compose a weekly digest escalating only flagged items;\n"
    "                  a FINAL HITL gate pauses before any email is sent.\n"
    "Per pipeline stage you delegate to the right specialist and forward its\n"
    "output to the next stage. Do NOT do extraction, verification, or reporting\n"
    "yourself — delegate them.\n"
    "\n"
    "When responding to a run trigger where no invoices have been ingested yet,\n"
    "respond with a single JSON object and nothing else, of shape:\n"
    '  {"status": "ack", "run_id": "<short hex>", "plan": [<stage>, ...]}\n'
    "where `plan` is the ordered list of pipeline stages you WILL execute\n"
    "(intake, extraction, verification, categorization, reconciliation, reporting).\n"
    "Do NOT emit free-form prose. Do NOT invent invoice data — none exists yet."
)

# Specialists are single-turn sub-agents (auto-wrapped as single-turn tools).
# Grows per phase; kept explicit so the wiring point is grep-able.
_SUB_AGENTS = [  # noqa: N806
    _extraction_wrapped,
    _verification_wrapped,
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