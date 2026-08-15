#!/usr/bin/env python3
"""Phase 6 smoke — PII redaction + HITL two-tier + Reporting agent safety rails.

Tests the middleware as PURE FUNCTIONS (no Vertex / no ADK runner needed):
  1. redact_pii redacts email, phone, SSN, card, bank-account.
  2. redact_pii preserves non-PII text and passes None through.
  3. HITL Tier 1: low confidence (< 0.7) → flag; high confidence → no flag.
  4. HITL Tier 2: before_tool_callback recognizes send_digest_email →
     request_confirmation would fire (checked via mock context).
  5. Wiring: reporting_agent in supervisor sub_agents; every specialist has
     before_model_callback + after_model_callback attached; reporting has
     before_tool_callback (HITL Tier 2).
  6. ReportingResult schema parses a sample digest JSON.

NO Vertex calls, NO Firestore, NO Cloud calls — deterministic & free.

Run:  uv run python scripts/smoke_safety.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# --- import the agent package -------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from reconciler.middleware import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    HIGH_STAKES_TOOL,
    _extract_confidence,
    before_model_callback_pii,
    make_after_model_callback_hitl,
    make_before_tool_callback_hitl,
    redact_pii,
    with_safety_rails,
)
from reconciler.schemas import FlaggedItem, ReportingResult  # noqa: E402

# --- import the agents (NOT wrapped — agent.py attaches wrappers) -------------
# We import the raw agents from their modules, then apply with_safety_rails
# ourselves to verify the wiring path.
from reconciler.extraction import extraction_agent  # noqa: E402
from reconciler.verification import verification_agent  # noqa: E402
from reconciler.reporting import reporting_agent  # noqa: E402

# And import the supervisor to verify sub_agents wiring
from reconciler.agent import root_agent  # noqa: E402

# ============================================================================
# 1. PII redaction — pure function
# ============================================================================

def test_pii_redaction():
    """PII patterns are replaced; non-PII text is preserved; None passes through."""
    sample = (
        "Contact john.doe@example.com or call 555-123-4567. "
        "SSN 123-45-6789. Card 4111 1111 1111 1111. "
        "Bank account 987654321099. "  # 12 digits — distinct from 10-digit phone
        "This is a normal invoice line item: Compute Engine $26.88"
    )
    redacted = redact_pii(sample)

    assert "[REDACTED:email]" in redacted, "email not redacted"
    assert "[REDACTED:phone]" in redacted, "phone not redacted"
    assert "[REDACTED:ssn]" in redacted, "SSN not redacted"
    assert "[REDACTED:card]" in redacted, "card not redacted"
    assert "[REDACTED:bank_account]" in redacted, "bank account not redacted"

    # non-PII preserved
    assert "Compute Engine $26.88" in redacted, "non-PII text corrupted"

    # None passes through
    assert redact_pii(None) is None, "None should pass through"

    print("[1] PII redaction PASS — all PII types replaced, non-PII preserved")


# ============================================================================
# 2. before_model_callback_pii mutates llm_request in place
# ============================================================================

def test_before_model_callback_pii():
    """The callback mutates llm_request.contents + system_instruction in place."""
    from google.genai import types

    content = types.Content(
        role="user",
        parts=[types.Part.from_text(text="My email is alice@corp.com and SSN 999-88-7777")],
    )
    config_obj = types.GenerateContentConfig(
        system_instruction="You are an agent. Do not process emails like admin@system.io"
    )
    llm_request = MagicMock()
    llm_request.contents = [content]
    llm_request.config = config_obj

    result = before_model_callback_pii(None, llm_request)

    assert result is None, "before_model_callback must return None (let model proceed)"
    part_text = llm_request.contents[0].parts[0].text
    assert "[REDACTED:email]" in part_text, "PII in contents not redacted"
    assert "[REDACTED:ssn]" in part_text, "SSN in contents not redacted"
    assert "alice@corp.com" not in part_text, "raw email leaked into model request"
    si = llm_request.config.system_instruction
    assert "[REDACTED:email]" in si, "PII in system_instruction not redacted"
    assert "admin@system.io" not in si, "raw email leaked in system_instruction"

    print("[2] before_model_callback_pii PASS — llm_request mutated, PII redacted before model sees it")


# ============================================================================
# 3. HITL Tier 1 — low confidence flags, high confidence doesn't
# ============================================================================

def test_hitl_tier1_confidence():
    """_extract_confidence parses JSON; flag fires below threshold."""
    # low confidence → flag
    low_json = json.dumps({"invoice": {"vendor": "Acme"}, "confidence": 0.4})
    low_conf = _extract_confidence(low_json)
    assert low_conf == 0.4, f"expected 0.4, got {low_conf}"
    assert low_conf < CONFIDENCE_THRESHOLD, "0.4 should be below threshold"

    # high confidence → no flag
    high_json = json.dumps({"invoice": {"vendor": "Acme"}, "confidence": 0.95})
    high_conf = _extract_confidence(high_json)
    assert high_conf == 0.95
    assert high_conf >= CONFIDENCE_THRESHOLD, "0.95 should be above threshold"

    # markdown-fenced JSON
    fenced = "```json\n" + json.dumps({"matched": True, "confidence": 0.5}) + "\n```"
    fenced_conf = _extract_confidence(fenced)
    assert fenced_conf == 0.5, f"fenced JSON confidence extraction failed: {fenced_conf}"

    # None / unparseable
    assert _extract_confidence(None) is None
    assert _extract_confidence("not json") is None
    assert _extract_confidence('{"no_confidence": true}') is None

    # bool edge case (True is not a valid confidence)
    assert _extract_confidence('{"confidence": true}') is None

    print("[3] HITL Tier-1 confidence parser PASS — low flags, high passes, edge cases handled")


def test_hitl_tier1_callback_state_annotation():
    """The after_model_callback writes hitl_flag to ctx.state on low confidence."""
    callback = make_after_model_callback_hitl("extraction")

    # Mock ctx.state as a dict
    mock_ctx = MagicMock()
    mock_ctx.state = {}

    # Mock low-confidence LlmResponse
    mock_response = MagicMock()
    mock_content = MagicMock()
    mock_part = MagicMock()
    mock_part.text = json.dumps({"invoice": {}, "confidence": 0.3})
    mock_content.parts = [mock_part]
    mock_response.content = mock_content

    result = callback(mock_ctx, mock_response)

    assert result is None, "after_model_callback must return None (flag, don't block)"
    assert "hitl_flag_extraction" in mock_ctx.state, "low-confidence flag not set in ctx.state"
    flag = mock_ctx.state["hitl_flag_extraction"]
    assert flag["reason"] == "low_confidence"
    assert flag["confidence"] == 0.3
    assert flag["action"] == "flagged_for_human_review"

    # High confidence → no flag
    mock_ctx2 = MagicMock()
    mock_ctx2.state = {}
    mock_part2 = MagicMock()
    mock_part2.text = json.dumps({"invoice": {}, "confidence": 0.9})
    mock_content2 = MagicMock()
    mock_content2.parts = [mock_part2]
    mock_response2 = MagicMock()
    mock_response2.content = mock_content2

    callback(mock_ctx2, mock_response2)
    assert len(mock_ctx2.state) == 0, "high-confidence should NOT set a flag"

    print("[4] HITL Tier-1 callback PASS — low conf → ctx.state['hitl_flag_*'] set; high conf → no flag")


# ============================================================================
# 4. HITL Tier 2 — before_tool_callback calls request_confirmation
# ============================================================================

def test_hitl_tier2_request_confirmation():
    """The before_tool_callback calls request_confirmation for send_digest_email."""
    gate = make_before_tool_callback_hitl()

    # Mock tool named 'send_digest_email'
    mock_tool = MagicMock()
    mock_tool.name = HIGH_STAKES_TOOL

    mock_args = {"recipient": "team@example.com", "subject": "Weekly Digest", "flagged_count": 3}

    # Mock tool_context — request_confirmation requires function_call_id set
    mock_tc = MagicMock()
    mock_tc.function_call_id = "test_fc_id_123"

    result = gate(mock_tool, mock_args, mock_tc)

    assert result is None, "before_tool_callback must return None (let framework handle)"
    mock_tc.request_confirmation.assert_called_once(), "request_confirmation NOT called"
    call_kwargs = mock_tc.request_confirmation.call_args.kwargs
    assert "hint" in call_kwargs, "hint missing from request_confirmation"
    assert "payload" in call_kwargs, "payload missing from request_confirmation"
    assert call_kwargs["payload"]["recipient"] == "team@example.com"
    assert call_kwargs["payload"]["flagged_count"] == 3

    # Non-high-stakes tool → NO confirmation
    mock_tool_other = MagicMock()
    mock_tool_other.name = "retrieve_invoice"
    mock_tc2 = MagicMock()
    mock_tc2.function_call_id = "test_fc_id_456"
    gate(mock_tool_other, mock_args, mock_tc2)
    mock_tc2.request_confirmation.assert_not_called(), "non-high-stakes tool should NOT pause"

    print("[5] HITL Tier-2 gate PASS — send_digest_email → request_confirmation fires; other tools → no pause")


# ============================================================================
# 5. Wiring verification — agents + callbacks
# ============================================================================

def test_agent_wiring():
    """Every specialist has safety rails; reporting has HITL Tier-2; supervisor wires all."""
    # Apply safety rails to the raw agents (mirrors what agent.py does)
    ext_safe = with_safety_rails(extraction_agent)
    ver_safe = with_safety_rails(verification_agent)

    # each wrapped specialist gets PII + HITL Tier 1
    assert ext_safe.before_model_callback is not None, "extraction missing before_model_callback (PII)"
    assert ext_safe.after_model_callback is not None, "extraction missing after_model_callback (HITL T1)"
    assert ver_safe.before_model_callback is not None, "verification missing before_model_callback (PII)"
    assert ver_safe.after_model_callback is not None, "verification missing after_model_callback (HITL T1)"

    # reporting agent has before_tool_callback (HITL Tier 2) built-in
    assert reporting_agent.before_tool_callback is not None, "reporting missing before_tool_callback (HITL T2)"

    # after with_safety_rails, reporting ALSO gets PII + Tier 1
    rep_safe = with_safety_rails(reporting_agent)
    assert rep_safe.before_model_callback is not None, "reporting wrapped missing before_model_callback"
    assert rep_safe.after_model_callback is not None, "reporting wrapped missing after_model_callback"
    assert rep_safe.before_tool_callback is not None, "reporting wrapped LOST before_tool_callback!"

    # supervisor sub_agents: must include all 3 specialists
    sub_names = [a.name for a in root_agent.sub_agents]
    assert "extraction" in sub_names, "extraction not in supervisor sub_agents"
    assert "verification" in sub_names, "verification not in supervisor sub_agents"
    assert "reporting" in sub_names, "reporting not in supervisor sub_agents"

    # all sub_agents have before_model_callback (safety rails applied)
    for sub in root_agent.sub_agents:
        assert sub.before_model_callback is not None, f"{sub.name} missing before_model_callback in supervisor"
        assert sub.after_model_callback is not None, f"{sub.name} missing after_model_callback in supervisor"

    # reporting sub_agent specifically must have before_tool_callback
    reporting_sub = next(s for s in root_agent.sub_agents if s.name == "reporting")
    assert reporting_sub.before_tool_callback is not None, "reporting in supervisor lost HITL Tier-2"

    print("[6] Agent wiring PASS — 3 specialists in supervisor, all have PII+HITL-T1, reporting has HITL-T2")


# ============================================================================
# 6. ReportingResult schema parses a sample digest
# ============================================================================

def test_reporting_result_schema():
    """ReportingResult parses a realistic digest JSON."""
    sample = {
        "digest_composed": True,
        "flagged_items": [
            {
                "invoice_number": "INV-2026-0417",
                "vendor": "Acme Cloud Services LLC",
                "discrepancy_type": "amount_mismatch",
                "description": "Invoice total (999.99) does not match bank (467.50).",
                "invoice_value": "999.99",
                "bank_value": "467.50",
                "confidence": 0.4,
            }
        ],
        "total_invoices": 1,
        "flagged_count": 1,
        "email_sent": False,
        "email_blocked_by_hitl": True,
        "recipient": "reconciler-team@example.com",
        "subject": "Weekly Reconciliation Digest — 1 FLAGGED",
        "confidence": 1.0,
    }
    result = ReportingResult.model_validate(sample)
    assert result.digest_composed is True
    assert len(result.flagged_items) == 1
    assert result.flagged_items[0].discrepancy_type == "amount_mismatch"
    assert result.email_blocked_by_hitl is True
    assert result.email_sent is False
    assert result.flagged_count == 1
    assert result.confidence == 1.0

    # FlaggedItem standalone
    fi = FlaggedItem(
        invoice_number="INV-TEST",
        vendor="TestCo",
        discrepancy_type="duplicate_payment",
    )
    assert fi.discrepancy_type == "duplicate_payment"

    print("[7] ReportingResult schema PASS — parses sample digest JSON correctly")


# ============================================================================
# Main
# ============================================================================

def main():
    print("Phase 6 safety smoke — PII redaction + HITL two-tier + reporting\n")
    test_pii_redaction()
    test_before_model_callback_pii()
    test_hitl_tier1_confidence()
    test_hitl_tier1_callback_state_annotation()
    test_hitl_tier2_request_confirmation()
    test_agent_wiring()
    test_reporting_result_schema()
    print("\nsmoke_safety PASS")


if __name__ == "__main__":
    main()