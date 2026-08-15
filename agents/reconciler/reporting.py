"""Reporting agent — weekly digest email escalating only flagged items.

This is the LAST pipeline stage. It receives verification results (matched /
discrepancies / confidence) across all invoices in the run, composes a human-
readable digest, and — critically — the ``send_digest_email`` tool is gated by
HITL Tier 2: a ``before_tool_callback`` calls
``tool_context.request_confirmation(...)`` so the framework PAUSES before any
email leaves the system. A human must approve (resume with ``confirmed=True``)
or the email is blocked.

Design decisions:
  - The agent has BOTH ``output_schema`` (ReportingResult) and tools
    (send_digest_email). When an ADK agent has output_schema AND tools together
    and the model natively supports both, the framework still enforces the
    schema — otherwise it falls back to SetModelResponseTool injection (still
    enforced, just via a tool). Either way the structured output is guaranteed.
  - ``before_tool_callback`` is set to the HITL Tier 2 gate — it only fires on
    the ``send_digest_email`` tool, and calls ``request_confirmation`` to pause
    the node.
  - PII redaction + HITL Tier 1 are additionally applied via ``with_safety_rails``
    in ``agent.py`` (after import) so every model call and every response goes
    through the full middleware stack.
"""

from __future__ import annotations

import logging

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import specialist_instruction
from .middleware import make_before_tool_callback_hitl
from .schemas import ReportingResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# send_digest_email — the high-stakes tool gated by HITL Tier 2.
# In production this calls Gmail API via the OAuth token from Secret Manager.
# In the demo it logs + returns success (the HITL gate is the proven behaviour).
# ---------------------------------------------------------------------------

# Default digest recipient — configurable via env RECONCLER_DIGEST_RECIPIENT.
# Real prod would write from Secret Manager; for demo it's a constant.
_DEFAULT_RECIPIENT = "reconciler-team@example.com"


def send_digest_email(
    recipient: str,
    subject: str,
    body: str,
    flagged_count: int = 0,
) -> dict:
    """Send the weekly reconciliation digest email to ``recipient``.

    This tool is gated by HITL Tier 2: ``before_tool_callback`` calls
    ``request_confirmation`` → the framework PAUSES before this function
    executes. Only after a human approves (resume with confirmed=True) does
    ADK invoke this function.

    Production: Gmail API via OAuth refresh token from Secret Manager.
    Demo: logs the send + returns success.
    """
    logger.info(
        "DIGEST EMAIL SENT -> recipient=%s subject=%s flagged=%d",
        recipient,
        subject,
        flagged_count,
    )
    return {
        "sent": True,
        "recipient": recipient,
        "subject": subject,
        "flagged_count": flagged_count,
    }


# ---------------------------------------------------------------------------
# Reporting agent instruction
# ---------------------------------------------------------------------------

_REPORTING_GOAL = (
    "Compose a weekly reconciliation digest and send it via the "
    "send_digest_email tool. Escalate ONLY items that were flagged by earlier "
    "stages (low confidence, discrepancies, or HITL Tier-1 flags). Clean "
    "invoices (matched, no discrepancies, high confidence) get a one-line "
    "summary in the digest — they are NOT escalated."
)

_REPORTING_INPUTS = (
    "A list of per-invoice verification results. Each result is a JSON object "
    "with {matched, discrepancies[], confidence, revised}. You also receive the "
    "extracted invoice vendor/number/total for each item."
)

_REPORTING_OUTPUT = (
    "ReportingResult JSON.\n"
    "  - If there ARE flagged items (discrepancies or confidence < 0.7):\n"
    "      1. Compose the email body listing ONLY the flagged items with full "
    "detail (vendor, discrepancy type, invoice_value vs bank_value).\n"
    "      2. Call the send_digest_email tool with recipient, subject, body, "
    "flagged_count. The HITL gate will pause — if approved, set "
    "email_sent=true. If blocked, set email_blocked_by_hitl=true.\n"
    "  - If there are NO flagged items:\n"
    "      Do NOT call send_digest_email. Set email_sent=false, "
    "email_blocked_by_hitl=false, digest_composed=true with a summary.\n"
    "  - Always populate flagged_items[] with the items that need human "
    "attention and total_invoices + flagged_count."
)

_REPORTING_INSTRUCTION = specialist_instruction(
    goal=_REPORTING_GOAL,
    inputs=_REPORTING_INPUTS,
    output_description=_REPORTING_OUTPUT,
)


# ---------------------------------------------------------------------------
# Reporting agent
# ---------------------------------------------------------------------------

reporting_agent = Agent(
    name="reporting",
    model=config.GEMINI_MODEL,
    instruction=_REPORTING_INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    # Has ONE tool: send_digest_email — the high-stakes action gated by HITL.
    tools=[send_digest_email],
    # Before the tool executes, the HITL Tier-2 gate fires → request_confirmation
    # → framework pauses → human must approve before email is sent.
    before_tool_callback=[make_before_tool_callback_hitl()],
    # output_schema + tools together: ADK uses SetModelResponseTool injection
    # path (still enforced, just via the tool's args-schema). temp=0.0 makes
    # the composition deterministic.
    output_schema=ReportingResult,
    mode="single_turn",
    output_key="reporting_last_reply",
)