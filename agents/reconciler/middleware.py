"""ADK middleware — PII redaction + Human-In-The-Loop (HITL) safety rails.

These callbacks are wired onto every specialist agent's callback chain so they
run on EVERY model call and EVERY tool call — the agent cannot opt out. This is
the 'ADK middleware (callbacks/interceptors) for PII redaction / HITL' layer the
build prompt mandates.

Pillar 1 — PII redaction (before_model_callback):
    Redacts email / phone / SSN / card-number / bank-account from
    ``llm_request.contents`` *and* ``llm_request.config.system_instruction``
    BEFORE the model reads them. The model never receives raw PII. The pure
    function ``redact_pii`` is exported for standalone testing (no ADK runtime
    needed).

Pillar 2 — HITL Tier 1: low-confidence flag & continue (after_model_callback):
    Parses the ``confidence`` field from the specialist's structured JSON
    response. If below ``CONFIDENCE_THRESHOLD`` → annotates ``ctx.state`` with
    a ``hitl_flag_<agent_name>`` dict → the Supervisor sees the flag and routes
    the item to the digest for human review. The agent does NOT pause — it
    'flags and continues' autonomously, matching the design doc's two-tier HITL.

Pillar 3 — HITL Tier 2: high-stakes pause & require approval
    (before_tool_callback on the Reporting agent):
    On the ``send_digest_email`` tool call, calls
    ``tool_context.request_confirmation(...)`` → the ADK framework pauses the
    node (state WAITING) → external resume with confirmed=True/False is required.
    This is the FINAL gate before any email leaves the system.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pillar 1 — PII redaction
# ---------------------------------------------------------------------------

# Ordered (type-label, compiled-pattern) pairs. Order matters: email first
# (won't accidentally eat phone digits), then SSN, phone, card, bank-account.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone", re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("bank_account", re.compile(r"\b\d{10,17}\b")),
]


def redact_pii(text: str | None) -> str | None:
    """Redact PII from text. Pure function — testable without ADK runtime.

    Returns a copy of ``text`` with every PII match replaced by
    ``[REDACTED:<type>]``. ``None`` passes through unchanged.
    """
    if text is None:
        return None
    for pii_type, pattern in _PII_PATTERNS:
        text = pattern.sub(f"[REDACTED:{pii_type}]", text)
    return text


def before_model_callback_pii(
    callback_context: Any,  # CallbackContext — unused (PII redaction is stateless)
    llm_request: LlmRequest,
) -> LlmResponse | None:
    """ADK ``before_model_callback`` — redact PII from the request before the model sees it.

    Mutates ``llm_request`` **in place**:
      - ``llm_request.contents[i].parts[j].text``
      - ``llm_request.config.system_instruction`` (str or Content)

    Returns ``None`` so the model call proceeds (with redacted content).
    """
    # --- redact user/model turns --------------------------------------------------
    for content in llm_request.contents:
        for part in content.parts or []:
            if part.text:
                part.text = redact_pii(part.text)

    # --- redact system instruction ------------------------------------------------
    si = llm_request.config.system_instruction
    if isinstance(si, str):
        llm_request.config.system_instruction = redact_pii(si)
    elif si is not None and hasattr(si, "parts"):
        for part in si.parts or []:
            if part.text:
                part.text = redact_pii(part.text)

    logger.debug("before_model_callback: PII redaction applied")
    return None  # proceed to model call with redacted content


# ---------------------------------------------------------------------------
# Pillar 2 — HITL Tier 1: low-confidence flag & continue
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD = 0.7  # below this → flag for human review (no pause)

# Keys written into ctx.state by the Tier-1 callback. The Supervisor and the
# Reporting agent read these to decide which items to escalate in the digest.
HITL_FLAG_PREFIX = "hitl_flag_"


def _extract_confidence(text: str | None) -> float | None:
    """Parse ``confidence`` from a specialist's JSON response text.

    Handles markdown code fences (```json ... ```) and bare JSON. Returns
    ``None`` on any parse failure (safest — no false flag on malformed output).
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    c = obj.get("confidence")
    if isinstance(c, bool):  # bool is subclass of int — exclude explicitly
        return None
    if isinstance(c, (int, float)):
        return float(c)
    return None


def make_after_model_callback_hitl(
    agent_name: str,
) -> Callable[[Any, LlmResponse], LlmResponse | None]:
    """Factory: build an ``after_model_callback`` that flags low-confidence
    results on ``ctx.state``.

    The ``agent_name`` is captured in the closure so the state key is unique per
    specialist (``hitl_flag_extraction``, ``hitl_flag_verification``, …).
    """

    def after_model_callback_hitl(
        callback_context: Any,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        # Extract the first text part from the response.
        text = None
        if llm_response and llm_response.content:
            for part in llm_response.content.parts or []:
                if part.text:
                    text = part.text
                    break

        confidence = _extract_confidence(text)
        if confidence is not None and confidence < CONFIDENCE_THRESHOLD:
            flag_key = f"{HITL_FLAG_PREFIX}{agent_name}"
            callback_context.state[flag_key] = {
                "stage": agent_name,
                "reason": "low_confidence",
                "confidence": confidence,
                "action": "flagged_for_human_review",
            }
            logger.warning(
                "HITL Tier-1 flag: %s confidence=%.3f < threshold %.2f — flagged",
                agent_name,
                confidence,
                CONFIDENCE_THRESHOLD,
            )
        return None  # always pass the response through (flag, don't block)

    return after_model_callback_hitl


# ---------------------------------------------------------------------------
# Pillar 3 — HITL Tier 2: high-stakes send → pause & require approval
# ---------------------------------------------------------------------------

# The tool name that triggers the Tier-2 gate. Kept in sync with the
# ``send_digest_email`` function defined in ``reporting.py``.
HIGH_STAKES_TOOL = "send_digest_email"


def make_before_tool_callback_hitl() -> Callable[[Any, dict, Any], dict | None]:
    """Factory: build a ``before_tool_callback`` that pauses on high-stakes
    tool calls via ``tool_context.request_confirmation(...)``.

    The ADK framework sees the pending confirmation, emits an interrupt Event,
    moves the node to WAITING, and waits for an external resume with
    ``confirmed=True`` (send) or ``confirmed=False`` (block).
    """

    def before_tool_callback_hitl(
        tool: Any,
        args: dict[str, Any],
        tool_context: Any,
    ) -> dict | None:
        tool_name = getattr(tool, "name", str(tool))
        if tool_name == HIGH_STAKES_TOOL:
            tool_context.request_confirmation(
                hint=(
                    "Approve sending the weekly reconciliation digest email? "
                    "This is a FINAL HITL gate — the email will be sent if approved."
                ),
                payload={
                    "recipient": args.get("recipient", ""),
                    "subject": args.get("subject", ""),
                    "flagged_count": args.get("flagged_count", 0),
                },
            )
            logger.info(
                "HITL Tier-2 gate: %s paused for human approval (recipient=%s)",
                tool_name,
                args.get("recipient", ""),
            )
        return None  # let the framework handle the confirmation flow

    return before_tool_callback_hitl


# ---------------------------------------------------------------------------
# Convenience: apply Pillar 1 + 2 to any specialist agent in one call
# ---------------------------------------------------------------------------

def with_safety_rails(agent: Any) -> Any:
    """Return a **copy** of ``agent`` with PII-redaction and HITL Tier-1
    callbacks attached.

    Uses ``model_copy(update=...)`` — a shallow Pydantic copy that preserves
    all existing fields (tools, output_schema, mode, output_key, …). It
    **prepends** the PII callback (so redaction runs before any pre-existing
    ``before_model_callback``) and **appends** the HITL callback (so flagging
    runs after any pre-existing ``after_model_callback``) — never replaces an
    existing callback chain. ``model_post_init`` does NOT re-run on a copy, so
    sub_agents wrapping is untouched.
    """
    existing_before = list(getattr(agent, "before_model_callback", None) or [])
    existing_after = list(getattr(agent, "after_model_callback", None) or [])
    return agent.model_copy(
        update={
            "before_model_callback": [before_model_callback_pii, *existing_before],
            "after_model_callback": [
                *existing_after,
                make_after_model_callback_hitl(agent.name),
            ],
        },
    )