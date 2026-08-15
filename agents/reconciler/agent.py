"""Reconciler root agent — the Supervisor.

The Supervisor is the ONLY agent exposed to the ADK API server
(``adk api_server agents/reconciler``). It holds the Instruction Contract and
routes work to single-turn specialists. Phase 1 ships the Supervisor alone
(no sub_agents yet) so the Cloud Run skeleton can respond to a run trigger and
prove the Gemini-via-Vertex routing works end-to-end with the runtime SA.

Specialists come online per phase:
  Phase 2 — Extraction (PDF -> structured JSON, temp=0.0)
  Phase 3 — Verification (CoVe), Categorization, Reconciliation
  Phase 7 — Intake (Gmail/Drive), Reporting (weekly digest)
Each specialist is added as a ``sub_agents=[...]`` entry with ``mode='single_turn'``
(auto-wrapped as a single-turn tool callable by the Supervisor).
"""

from __future__ import annotations

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import INSTRUCTION_CONTRACT

_SUPERVISOR_INSTRUCTION = (
    INSTRUCTION_CONTRACT
    + "\n\n"
    + "You are the Reconciler Supervisor orchestrating a reconciliation run.\n"
    "A run has just been triggered (no invoices ingested yet in this skeleton phase).\n"
    "Respond with a single JSON object and nothing else, of shape:\n"
    '  {"status": "ack", "run_id": "<short hex>", "plan": [<stage>, ...]}\n'
    "where `plan` is the ordered list of pipeline stages you WILL execute\n"
    "(intake, extraction, verification, categorization, reconciliation, reporting).\n"
    "Do NOT emit free-form prose. Do NOT invent invoice data — none exists yet."
)

# Phase 1: skeleton. `sub_agents` starts empty; populated per phase above.
_SUB_AGENTS = []  # noqa: N806 — grows per phase; kept explicit so the wiring point is obvious

root_agent = Agent(
    name="supervisor",
    model=config.GEMINI_MODEL,
    instruction=_SUPERVISOR_INSTRUCTION,
    # Deterministic orchestration skeleton: zero temperature so the ack/plan
    # shape is stable across runs (anti-hallucination posture, even here).
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    sub_agents=_SUB_AGENTS,
    output_key="supervisor_last_reply",
)