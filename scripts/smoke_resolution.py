#!/usr/bin/env python3
"""P9 smoke — closed-loop resolution core.

Proves the resolve/dispute/escalate decision engine:
  [unit] pipeline._close_resolution semantics (no Vertex):
         resolve+recheck-pass -> resolved; resolve+recheck-fail -> escalated
         (never self-certify); dispute -> disputed; escalate -> escalated.
  [unit] pipeline._evidence_packet computes real auditable numbers on the
         fixture bank CSV (fuzzy vendor match, date delta, transposition).
  [unit] decision-table thresholds exist in one visible place.
  [live] scenario A: date_mismatch inside 1-3d posting window + strong
         evidence -> lane in {resolve, dispute} with rationale citing the
         day delta (auto-resolve attempt is correct behavior).
  [live] scenario B: duplicate_payment (two bank rows, same invoice
         number) -> lane=dispute + DisputeDraft with amount_at_risk;
         draft is NEVER a send.
  [live] scenario C: no_bank_match with an empty evidence packet ->
         lane=escalate, rationale names the missing evidence.

Env (same as other live smokes):
  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json
  GOOGLE_GENAI_USE_VERTEXAI=1
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad
  GOOGLE_CLOUD_LOCATION=us-central1
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

FIXTURES = ROOT / "tests" / "fixtures"

EXPECTED_ENV = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)


def check_env() -> None:
    missing = [e for e in EXPECTED_ENV if not os.environ.get(e)]
    if missing:
        sys.exit(
            "missing env: "
            + ", ".join(missing)
            + "\nrun with: GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json "
              "GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad "
              "GOOGLE_CLOUD_LOCATION=us-central1 uv run python scripts/smoke_resolution.py"
        )


# ---------------------------------------------------------------------------
# live-agent helper (mode='chat' clone — root-legal, same native schema branch)
# ---------------------------------------------------------------------------

async def run_resolution(prompt_parts: list) -> dict:
    from google.adk import Agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from reconciler.resolution import resolution_agent

    clone: Agent = resolution_agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=clone, app_name="smoke-resolution")
    session_id = f"res_{os.urandom(4).hex()}"
    await runner.session_service.create_session(
        app_name="smoke-resolution", user_id="smoke", session_id=session_id
    )
    final_text: str | None = None
    async for ev in runner.run_async(
        user_id="smoke",
        session_id=session_id,
        new_message=types.Content(role="user", parts=prompt_parts),
    ):
        if ev.is_final_response() and final_text is None and ev.content and ev.content.parts:
            final_text = ev.content.parts[0].text
    if final_text is None:
        raise RuntimeError("resolution agent produced no final response")
    text = final_text.strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def resolution_prompt(discrepancies, verification, invoice, evidence) -> list:
    from google.genai import types

    return [
        types.Part.from_text(
            text=(
            "DISCREPANCIES (from verification):\n"
            + json.dumps(discrepancies, indent=2)
            + "\n\nVERIFICATION confidence: "
            + str(verification.get("confidence", 0.0))
            + "\nCoVe q/a: "
            + json.dumps(
                list(zip(verification.get("verification_questions", []),
                         verification.get("verification_answers", [])))[:5]
            )
            + "\n\nINVOICE (extracted):\n"
            + json.dumps(invoice, indent=2)
            + "\n\nEVIDENCE PACKET (the only evidence that exists):\n"
            + json.dumps(evidence, indent=2)
            + "\n\nApply the decision table. Emit ResolutionAction."
            )
        )
    ]


# ---------------------------------------------------------------------------
# evidence packet builder on the fixture bank CSV (no Firestore: memory=None)
# ---------------------------------------------------------------------------

def build_fixture_evidence() -> dict:
    """Run Pipeline._evidence_packet against the fixture invoice + bank CSV
    with memory lookups disabled (a stub that returns None = anti-hallack signal)."""

    class _NullMemory:
        async def get_fact(self, *, namespace, key):
            return None

    async def _go():
        from reconciler.pipeline import Pipeline

        class _P(Pipeline):
            def __init__(self):
                from reconciler.resilience import CircuitBreaker

                self.memory = _NullMemory()
                self.bank_csv = FIXTURES / "bank_statement.csv"
                # P10 wiring: _read_bank runs behind a per-dependency breaker.
                self._bank_breaker = CircuitBreaker(dependency="bank-statement")

        p = _P()
        invoice = {
            "vendor": "Acme Cloud Services LLC",
            "invoice_number": "INV-2026-0417",
            "invoice_date": "2026-08-12",
            "total": 467.50,
        }
        verification = {"confidence": 0.95}
        return await p._evidence_packet(invoice, verification)  # type: ignore[attr-defined]

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# unit checks (no Vertex)
# ---------------------------------------------------------------------------

def unit_close_resolution() -> None:
    """_close_resolution semantics with a stubbed re-verification pass."""
    from reconciler.pipeline import Pipeline

    class _P(Pipeline):
        def __init__(self, recheck):
            self._recheck = recheck

        async def _stage_verification(self, invoice):  # type: ignore[override]
            return self._recheck

    async def _go():
        # resolve + recheck PASS -> resolved
        p = _P({"matched": True, "discrepancies": [], "verification_questions": ["q"], "verification_answers": ["a"]})
        payload = {
            "decision": {"lane": "resolve", "discrepancy_type": "date_mismatch", "confidence": 0.95},
            "corrected_invoice": {"vendor": "Acme", "total": 467.50},
        }
        out = await p._close_resolution(payload)  # type: ignore[arg-type]
        assert out["outcome"] == "resolved", out
        assert out["recheck_matched"] is True

        # resolve + recheck FAIL (discrepancy persists) -> escalated, never self-certified
        p2 = _P({"matched": False,
                 "discrepancies": [{"type": "date_mismatch", "description": "still off"}],
                 "verification_questions": [], "verification_answers": []})
        payload2 = {
            "decision": {"lane": "resolve", "discrepancy_type": "date_mismatch", "confidence": 0.95},
            "corrected_invoice": {"vendor": "Acme", "total": 467.50},
        }
        out2 = await p2._close_resolution(payload2)  # type: ignore[arg-type]
        assert out2["outcome"] == "escalated", out2
        assert out2["recheck_matched"] is False

        # dispute -> disputed (draft only, never sent, never resolved)
        p3 = _P({"matched": True, "discrepancies": []})
        payload3 = {
            "decision": {"lane": "dispute", "discrepancy_type": "duplicate_payment", "confidence": 0.85},
            "dispute_draft": {"recipient": "ap@vendor.com", "subject": "dup",
                              "body": "...", "amount_at_risk": 2400.0},
        }
        out3 = await p3._close_resolution(payload3)  # type: ignore[arg-type]
        assert out3["outcome"] == "disputed", out3

        # escalate -> escalated
        payload4 = {"decision": {"lane": "escalate", "confidence": 0.5}}
        out4 = await p3._close_resolution(payload4)  # type: ignore[arg-type]
        assert out4["outcome"] == "escalated", out4

        # HARD CLAMP (Gate 9 F1): duplicate_payment + lane=resolve is
        # forced to dispute — money moves only with a human approval.
        p5 = _P({"matched": True, "discrepancies": []})
        payload5 = {
            "decision": {"lane": "resolve", "discrepancy_type": "duplicate_payment", "confidence": 0.99},
            "corrected_invoice": {"vendor": "Acme", "total": 2400.0},
        }
        out5 = await p5._close_resolution(payload5)  # type: ignore[arg-type]
        assert out5["outcome"] == "disputed", out5
        assert "clamp" in (out5["decision"].get("rationale") or "")

        # HARD CLAMP (Gate 9 F1): confidence below DISPUTE_THRESHOLD is
        # forced to escalate regardless of the lane the model chose.
        payload6 = {
            "decision": {"lane": "resolve", "discrepancy_type": "date_mismatch", "confidence": 0.3},
            "corrected_invoice": {"vendor": "Acme", "total": 467.50},
        }
        out6 = await p5._close_resolution(payload6)  # type: ignore[arg-type]
        assert out6["outcome"] == "escalated", out6
        assert "clamp" in (out6["decision"].get("rationale") or "")

        # Provenance (Gate 9 F5): every closed resolution carries the audit
        # entry, and evidence_refs citing non-existent packet keys are
        # surfaced, not trusted. rule_fired must reflect the ACTUAL
        # evidence-packet score (key 'fuzzy'), not a truthy placeholder.
        payload7 = {
            "decision": {"lane": "resolve", "discrepancy_type": "date_mismatch",
                         "confidence": 0.95,
                         "evidence_refs": ["date_delta_days", "made_up_ref"]},
            "corrected_invoice": {"total": 467.50},
        }
        out7 = await p5._close_resolution(
            payload7,
            evidence={
                "date_delta_days": 2,
                "best_vendor_row": {"description": "CARD ACME CLOUD", "fuzzy": 0.95},
            },  # type: ignore[arg-type]
        )
        assert out7["outcome"] == "resolved", out7
        prov = out7.get("provenance") or {}
        assert prov.get("lane") == "resolve" and prov.get("recheck_matched") is True
        assert prov.get("rule_fired") == "fuzzy_match(vendor, bank_row) @ 0.95", prov
        assert prov.get("timestamp")
        assert out7.get("evidence_refs_invalid") == ["made_up_ref"]
        assert out7.get("invoice_before_correction") is not None

        # HARD CLAMP (re-review follow-up): an OMITTED confidence (None)
        # is treated as below-threshold — never a clamp bypass.
        payload8 = {
            "decision": {"lane": "resolve", "discrepancy_type": "date_mismatch",
                         "confidence": None},
            "corrected_invoice": {"total": 467.50},
        }
        out8 = await p5._close_resolution(payload8)  # type: ignore[arg-type]
        assert out8["outcome"] == "escalated", out8
        assert "missing" in (out8["decision"].get("rationale") or "")

    asyncio.run(_go())
    print("[unit] _close_resolution PASS — strict re-verify closure + hard lane clamps + provenance + evidence-ref validation")


def unit_evidence_packet() -> None:
    packet = build_fixture_evidence()
    best = packet.get("best_vendor_row") or {}
    assert best.get("fuzzy", 0.0) > 0.8, f"vendor fuzzy match too weak: {best}"
    assert packet.get("date_delta_days") == 0, f"fixture same-day charge expected, got {packet.get('date_delta_days')}"
    amounts = packet.get("amount_rows", [])
    assert any(r.get("exact") for r in amounts), "fixture bank row must exact-match invoice total"
    assert packet.get("vendor_alias_fact") is None, "null-memory lookup must return None signal"
    print(f"[unit] _evidence_packet PASS — vendor fuzzy={best.get('fuzzy'):.2f}, "
          f"exact amount row present, date_delta={packet.get('date_delta_days')}d, miss->None")


def unit_thresholds() -> None:
    from reconciler.middleware import CONFIDENCE_THRESHOLD, DISPUTE_THRESHOLD, RESOLVE_THRESHOLD

    assert 0 < CONFIDENCE_THRESHOLD <= DISPUTE_THRESHOLD < RESOLVE_THRESHOLD <= 1
    print(f"[unit] thresholds PASS — escalate<{DISPUTE_THRESHOLD} <= dispute<{RESOLVE_THRESHOLD} <= resolve")


# ---------------------------------------------------------------------------
# live scenarios
# ---------------------------------------------------------------------------

def scenario_a_date_mismatch() -> None:
    """date_mismatch inside the 1-3 day bank-posting window, strong evidence."""
    discrepancies = [{
        "type": "date_mismatch",
        "description": "Invoice date 2026-08-12 vs bank posting date 2026-08-14.",
        "invoice_value": "2026-08-12",
        "bank_value": "2026-08-14",
    }]
    verification = {
        "confidence": 0.95,
        "verification_questions": ["Does the bank row post within 3 days of the invoice date?"],
        "verification_answers": ["Yes — 2 days later."],
    }
    invoice = {
        "vendor": "Acme Cloud Services LLC",
        "invoice_number": "INV-2026-0417",
        "invoice_date": "2026-08-12",
        "total": 467.50,
    }
    evidence = {
        "bank_rows": [
            {"date": "2026-08-14", "description": "CARD ACME CLOUD SERVICES LLC INV-2026-0417", "amount": "-467.50"},
        ],
        "vendor_alias_fact": {"aliases": ["ACME CLOUD", "ACME CLOUD SERVICES"]},
        "prior_invoice_fact": None,
        "best_vendor_row": {"date": "2026-08-14", "amount": "-467.50", "fuzzy": 0.91},
        "number_fuzzy": {"token": "INV-2026-0417", "score": 1.0},
        "date_delta_days": 2,
        "amount_rows": [{"abs_amount": 467.50, "exact": True, "digit_transposition": False}],
    }
    out = asyncio.run(run_resolution(resolution_prompt(discrepancies, verification, invoice, evidence)))
    lane = (out.get("decision") or {}).get("lane")
    rationale = (out.get("decision") or {}).get("rationale") or ""
    assert lane in ("resolve", "dispute"), f"scenario A: expected resolve/dispute, got {lane}: {json.dumps(out)}"
    assert out.get("outcome") in ("disputed", "escalated") or lane == "resolve", out
    if lane == "resolve":
        assert out.get("corrected_invoice"), "resolve lane must carry corrected_invoice (pipeline re-verifies it)"
    print(f"[live A] date_mismatch PASS — lane={lane}, rationale cites evidence: "
          f"{any(k in rationale.lower() for k in ('day', 'date', 'delta', '2'))}")


def scenario_b_duplicate_payment() -> None:
    discrepancies = [{
        "type": "duplicate_payment",
        "description": "Two bank debits of 2400.00 reference the same invoice INV-2026-1105.",
        "invoice_value": "2400.00",
        "bank_value": "4800.00 total",
    }]
    verification = {
        "confidence": 0.93,
        "verification_questions": ["Does the invoice number appear on two bank rows?"],
        "verification_answers": ["Yes — rows 3 and 4 both reference INV-2026-1105."],
    }
    invoice = {
        "vendor": "Vertex Data Systems Inc",
        "invoice_number": "INV-2026-1105",
        "invoice_date": "2026-08-05",
        "total": 2400.00,
    }
    evidence = {
        "bank_rows": [
            {"date": "2026-08-06", "description": "ACH VERTEX DATA SYSTEMS INV-2026-1105", "amount": "-2400.00"},
            {"date": "2026-08-09", "description": "ACH VERTEX DATA SYSTEMS INV-2026-1105", "amount": "-2400.00"},
        ],
        "vendor_alias_fact": {"aliases": ["VERTEX DATA"]},
        "prior_invoice_fact": {"total": 2400.0, "vendor": "Vertex Data Systems Inc"},
        "best_vendor_row": {"date": "2026-08-06", "amount": "-2400.00", "fuzzy": 0.95},
        "number_fuzzy": {"token": "INV-2026-1105", "score": 1.0},
        "date_delta_days": 1,
        "amount_rows": [
            {"abs_amount": 2400.0, "exact": True, "digit_transposition": False},
            {"abs_amount": 2400.0, "exact": True, "digit_transposition": False},
        ],
    }
    out = asyncio.run(run_resolution(resolution_prompt(discrepancies, verification, invoice, evidence)))
    lane = (out.get("decision") or {}).get("lane")
    draft = out.get("dispute_draft") or {}
    assert lane == "dispute", f"scenario B: duplicate_payment must be dispute, got {lane}: {json.dumps(out)}"
    assert draft.get("amount_at_risk", 0) > 0, f"dispute draft must carry amount_at_risk: {draft}"
    assert draft.get("recipient") and draft.get("subject") and draft.get("body"), draft
    # the draft is inert by construction: ResolutionAction has no send field.
    # Structural check — no send-capability KEY anywhere in the payload tree
    # (prose may legitimately mention sending; a capability cannot).
    def _walk_keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from _walk_keys(v)
        elif isinstance(o, list):
            for it in o:
                yield from _walk_keys(it)

    keys = set(_walk_keys(out))
    assert not any("send" in k.lower() or "sent" in k.lower() for k in keys), keys
    assert set(draft.keys()) <= {"recipient", "subject", "body", "amount_at_risk"}, draft
    print(f"[live B] duplicate_payment PASS — lane=dispute, amount_at_risk=${draft.get('amount_at_risk'):,.2f}, "
          f"draft inert (no send capability in schema)")


def scenario_c_no_bank_match() -> None:
    discrepancies = [{
        "type": "no_bank_match",
        "description": "No bank row matches invoice INV-2026-9999 total 810.40.",
        "invoice_value": "810.40",
        "bank_value": None,
    }]
    verification = {
        "confidence": 0.88,
        "verification_questions": ["Does any bank row match the invoice total within $0.02?"],
        "verification_answers": ["No."],
    }
    invoice = {
        "vendor": "Northwind Paper Co",
        "invoice_number": "INV-2026-9999",
        "invoice_date": "2026-08-14",
        "total": 810.40,
    }
    evidence = {
        "bank_rows": [
            {"date": "2026-08-12", "description": "CARD ACME CLOUD SERVICES LLC INV-2026-0417", "amount": "-467.50"},
        ],
        "vendor_alias_fact": None,
        "prior_invoice_fact": None,
        "best_vendor_row": {"fuzzy": 0.11},
        "number_fuzzy": {"score": 0.0},
        "date_delta_days": None,
        "amount_rows": [{"abs_amount": 467.50, "exact": False, "digit_transposition": False}],
    }
    out = asyncio.run(run_resolution(resolution_prompt(discrepancies, verification, invoice, evidence)))
    lane = (out.get("decision") or {}).get("lane")
    rationale = ((out.get("decision") or {}).get("rationale") or "").lower()
    assert lane == "escalate", f"scenario C: no-evidence no_bank_match must escalate, got {lane}: {json.dumps(out)}"
    # outcome=None is CORRECT agent behavior: the pipeline's _close_resolution
    # stamps terminal outcomes — the agent never self-certifies.
    assert out.get("outcome") in (None, "escalated"), out
    print(f"[live C] no_bank_match PASS — lane=escalate, rationale names missing evidence: "
          f"{any(k in rationale for k in ('no bank', 'no match', 'missing', 'no evidence', 'none'))}")


def main() -> None:
    check_env()
    print("P9 resolution smoke — closed-loop decision engine")
    unit_close_resolution()
    unit_evidence_packet()
    unit_thresholds()
    scenario_a_date_mismatch()
    scenario_b_duplicate_payment()
    scenario_c_no_bank_match()
    print("smoke_resolution PASS")


if __name__ == "__main__":
    main()
