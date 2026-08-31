"""Phase 3 verification smoke — CoVe cross-check against the bank-statement CSV.

The two-stage proof:

  SCENARIO A (happy path): drive Extraction on the sample PDF, then feed the
  extracted Invoice + the matching bank CSV to the Verification agent. The
  bank CSV contains the exact matching charge 'CARD ACME CLOUD SERVICES LLC
  INV-2026-0417' for -467.50 on 2026-08-12. Assert: matched=true, no
  discrepancies, CoVe questions and answers present and same length.

  SCENARIO B (injected discrepancy): take the SAME extracted invoice but MUTATE
  its total to $999.99 (a value the bank CSV does NOT contain). Feed to
  Verification. Assert: matched=false, at least one discrepancy (type in the
  closed DISCREPANCY_TYPES set), CoVe questions/answers still present. This
  proves the Verification agent did NOT rubber-stamp its own draft — CoVe's
  ANSWER-EACH-INDEPENDENTLY step caught the mismatch and REVISE set matched=false.

This is the anti-rubber-stamp proof: the only difference between A and B is the
invoice total. If the verification agent trusted its draft, B would also report
matched=true. Instead B must report a discrepancy (amount_mismatch or
no_bank_match), proving the CoVe loop broke the self-consistency trap.

Determinism: run each scenario twice at temp=0.0 and assert identical JSON.

Run (same Vertex env as scripts/smoke_extraction.py):

  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
  GOOGLE_GENAI_USE_VERTEXAI=1 \
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
  GOOGLE_CLOUD_LOCATION=global \
  uv run python scripts/smoke_verification.py

Exit 0 == Phase 3 smoke passed. ~$0.02-0.03 of Vertex tokens (4 extractions +
4 verifications at temp=0.0).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

FAIL = "\033[31m"
OK = "\033[32m"
RESET = "\033[0m"

# Ground truth mirrors tests/fixtures/make_fixtures.py.
GT_VENDOR = "Acme Cloud Services LLC"
GT_INVOICE_NO = "INV-2026-0417"
GT_INVOICE_DATE = "2026-08-12"
GT_TOTAL = 467.50
GT_LINE_ITEMS = 4

FIXTURE_PDF = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "invoice_sample.pdf"
)
FIXTURE_CSV = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bank_statement.csv"
)

# What we inject into Scenario B to prove CoVe catches a mismatch.
INJECTED_WRONG_TOTAL = 999.99


def _check_env() -> None:
    missing = [
        v
        for v in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
        )
        if not os.environ.get(v)
    ]
    if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
        missing.append("GOOGLE_GENAI_USE_VERTEXAI")
    if missing:
        print(f"{FAIL}missing env: {', '.join(missing)}{RESET}", file=sys.stderr)
        raise SystemExit(2)


async def _extract_once() -> dict:
    """Run Extraction once on the sample PDF; return parsed ExtractionResult."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "agents"))
    from reconciler import config  # type: ignore
    from reconciler.extraction import extraction_agent  # type: ignore

    # Same env-config asserts as the Phase 2 smoke.
    assert extraction_agent.generate_content_config.temperature == 0.0
    assert extraction_agent.output_schema is not None
    assert not extraction_agent.tools

    pdf_bytes = FIXTURE_PDF.read_bytes()
    assert pdf_bytes[:4] == b"%PDF", f"fixture not a PDF: {FIXTURE_PDF}"

    # Shipped agent is mode='single_turn' (forbidden as root). Clone to chat.
    root_extraction = extraction_agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=root_extraction, app_name=config.APP_NAME)
    session = await runner.session_service.create_session(
        app_name=config.APP_NAME, user_id="smoke-verification"
    )
    new_message = types.Content(
        role="user",
        parts=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            types.Part.from_text(
                text=(
                    "Extract this invoice into the ExtractionResult JSON. "
                    "Use null for any field not present. The 'NOTE' at the "
                    "bottom is not a line item."
                )
            ),
        ],
    )

    final_text: str | None = None
    async for event in runner.run_async(
        user_id="smoke-verification",
        session_id=session.id,
        new_message=new_message,
    ):
        if event.is_final_response():
            final_text = (
                event.content
                and event.content.parts
                and event.content.parts[0].text
            ) or None

    if not final_text:
        print(f"{FAIL}extraction: no final response{RESET}", file=sys.stderr)
        raise SystemExit(1)

    parsed = _parse_json_response(final_text)
    assert "invoice" in parsed, f"extraction reply missing invoice: {parsed!r}"
    return parsed


async def _verify_once(invoice_dict: dict, csv_text: str) -> dict:
    """Run the Verification agent on (invoice JSON, bank CSV); return parsed dict."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "agents"))
    from reconciler import config  # type: ignore
    from reconciler.verification import verification_agent  # type: ignore

    # Pre-assert: same anti-hallucination posture as Extraction.
    assert verification_agent.generate_content_config.temperature == 0.0, (
        "verification temperature must be 0.0"
    )
    assert verification_agent.output_schema is not None, (
        "verification output_schema must be set"
    )
    assert not verification_agent.tools, (
        "verification must have NO tools for native schema enforcement"
    )

    user_payload = (
        "INVOICE (extraction result JSON):\n"
        f"{json.dumps(invoice_dict, indent=2)}\n\n"
        "BANK STATEMENT CSV:\n"
        f"{csv_text}\n\n"
        "Verify the invoice against the bank statement using CoVe. "
        "Emit the VerificationResult JSON."
    )

    root_verification = verification_agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=root_verification, app_name=config.APP_NAME)
    session = await runner.session_service.create_session(
        app_name=config.APP_NAME, user_id="smoke-verification"
    )
    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=user_payload)],
    )

    final_text: str | None = None
    async for event in runner.run_async(
        user_id="smoke-verification",
        session_id=session.id,
        new_message=new_message,
    ):
        if event.is_final_response():
            final_text = (
                event.content
                and event.content.parts
                and event.content.parts[0].text
            ) or None

    if not final_text:
        print(f"{FAIL}verification: no final response{RESET}", file=sys.stderr)
        raise SystemExit(1)
    return _parse_json_response(final_text)


def _parse_json_response(text: str) -> dict:
    text = text.strip().removeprefix("```json").removeprefix("```").strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"{FAIL}reply not JSON: {exc}\n---\n{text}{RESET}", file=sys.stderr)
        raise SystemExit(1) from exc


def _validate_cove_trace(parsed: dict, scenario: str) -> None:
    """The CoVe proof: questions answered independently, not rubber-stamp.

    Assert the verification_questions list is non-empty, the answers list has
    the SAME length, and neither list contains forbidden self-referential
    questions ('is my draft right?').
    """
    qs = parsed.get("verification_questions") or []
    ans = parsed.get("verification_answers") or []

    assert isinstance(qs, list), f"{scenario}: verification_questions not list"
    assert isinstance(ans, list), f"{scenario}: verification_answers not list"
    assert len(qs) >= 1, f"{scenario}: CoVe must ask at least 1 question (got 0)"
    assert len(qs) == len(ans), (
        f"{scenario}: CoVe questions/answers length mismatch: "
        f"{len(qs)} vs {len(ans)}"
    )
    for q in qs:
        assert isinstance(q, str) and len(q) > 0, f"{scenario}: empty question"
        ql = q.lower()
        # Forbidden self-referential phrasing — CoVe questions must be CHECKABLE
        # against inputs, not 'is my draft right?'.
        forbidden = ("my draft", "is my answer", "my answer correct", "is my guess", "my answer")
        assert not any(p in ql for p in forbidden), (
            f"{scenario}: forbidden self-referential CoVe question: {q!r}"
        )
    for a in ans:
        assert isinstance(a, str) and len(a) > 0, f"{scenario}: empty answer"


def _assert_happy(parsed: dict) -> None:
    """Scenario A: the invoice matches a real bank charge -> matched=true."""
    assert parsed.get("matched") is True, (
        f"happy-path matched={parsed.get('matched')!r}, expected True "
        f"(invoice total {GT_TOTAL} is in the bank CSV)"
    )
    amt = parsed.get("matched_amount")
    assert amt is not None and abs(amt - GT_TOTAL) < 0.02, (
        f"happy-path matched_amount={amt!r}, expected ~{GT_TOTAL}"
    )
    assert parsed.get("matched_date") == GT_INVOICE_DATE, (
        f"happy-path matched_date={parsed.get('matched_date')!r}, "
        f"expected {GT_INVOICE_DATE!r}"
    )
    discrepancies = parsed.get("discrepancies") or []
    assert discrepancies == [], (
        f"happy-path must have NO discrepancies: {discrepancies!r}"
    )
    _validate_cove_trace(parsed, "happy-path")
    print(
        f"{OK}happy-path PASS: matched=True amount={parsed['matched_amount']} "
        f"date={parsed['matched_date']} discrepancies=0 "
        f"confidence={parsed.get('confidence')} "
        f"q/a={len(parsed.get('verification_questions') or [])}"
        f"{RESET}"
    )


def _assert_mismatch(parsed: dict) -> None:
    """Scenario B: invoice total mutated to $999.99 -> must be flagged.

    The verification agent must NOT rubber-stamp. matched=false, at least one
    discrepancy with a type from the closed set (amount_mismatch or
    no_bank_match expected for this fixture).
    """
    assert parsed.get("matched") is False, (
        f"mismatch-path matched={parsed.get('matched')!r}, expected False — "
        f"CoVe did NOT catch the injected amount mismatch"
    )
    discrepancies = parsed.get("discrepancies") or []
    assert len(discrepancies) >= 1, (
        "mismatch-path must flag at least one discrepancy — CoVe rubber-stamped"
    )

    # Import the closed set lazily so the module still loads under tooling.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "agents"))
    from reconciler.schemas import DISCREPANCY_TYPES  # type: ignore

    types_seen = set()
    for d in discrepancies:
        dt = d.get("type")
        assert dt in DISCREPANCY_TYPES, (
            f"mismatch-path: discrepancy type {dt!r} not in closed set "
            f"{sorted(DISCREPANCY_TYPES)}"
        )
        types_seen.add(dt)
    # For an injected amount of 999.99 that no bank row contains, the agent
    # should flag either an amount_mismatch (it found a candidate row but the
    # amounts disagree) or no_bank_match (it found no candidate). Both are valid
    # CoVe outcomes.
    assert types_seen & {"amount_mismatch", "no_bank_match"}, (
        f"mismatch-path: expected one of amount_mismatch/no_bank_match, got "
        f"{sorted(types_seen)}"
    )
    _validate_cove_trace(parsed, "mismatch-path")
    print(
        f"{OK}mismatch-path PASS: matched=False discrepancies={len(discrepancies)} "
        f"types={sorted(types_seen)} confidence={parsed.get('confidence')} "
        f"q/a={len(parsed.get('verification_questions') or [])}"
        f"{RESET}"
    )


async def _run() -> str:
    print(f"{OK}Phase 3 verification smoke — CoVe cross-check{RESET}")
    csv_text = FIXTURE_CSV.read_text()

    # --- Scenario A: happy path (extract -> verify) -----------------------
    print(f"{OK}[A] extracting sample invoice...{RESET}")
    extraction_a1 = await _extract_once()
    _assert_extraction_sane(extraction_a1)
    print(f"{OK}[A] verifying against bank CSV (run 1)...{RESET}")
    verdict_a1 = await _verify_once(extraction_a1["invoice"], csv_text)
    _assert_happy(verdict_a1)

    # --- Scenario B: injected amount mismatch -----------------------------
    mutated_invoice = copy.deepcopy(extraction_a1["invoice"])
    mutated_invoice["total"] = INJECTED_WRONG_TOTAL
    print(
        f"{OK}[B] verifying mutated invoice total={INJECTED_WRONG_TOTAL} "
        f"against SAME bank CSV (run 1)...{RESET}"
    )
    verdict_b1 = await _verify_once(mutated_invoice, csv_text)
    _assert_mismatch(verdict_b1)

    # --- Determinism at temp=0.0 ------------------------------------------
    print(f"{OK}[det] re-running both scenarios for determinism...{RESET}")
    extraction_a2 = await _extract_once()
    verdict_a2 = await _verify_once(extraction_a2["invoice"], csv_text)
    verdict_b2 = await _verify_once(mutated_invoice, csv_text)

    a1 = json.dumps(verdict_a1, sort_keys=True)
    a2 = json.dumps(verdict_a2, sort_keys=True)
    if a1 != a2:
        print(f"{FAIL}NON-DETERMINISTIC happy-path:{RESET}", file=sys.stderr)
        print(f"  run-1: {a1}", file=sys.stderr)
        print(f"  run-2: {a2}", file=sys.stderr)
        raise SystemExit(1)
    b1 = json.dumps(verdict_b1, sort_keys=True)
    b2 = json.dumps(verdict_b2, sort_keys=True)
    if b1 != b2:
        print(f"{FAIL}NON-DETERMINISTIC mismatch-path:{RESET}", file=sys.stderr)
        print(f"  run-1: {b1}", file=sys.stderr)
        print(f"  run-2: {b2}", file=sys.stderr)
        raise SystemExit(1)
    print(f"{OK}determinism PASS: identical structured output across 2 runs{RESET}")
    print(f"{OK}smoke_verification PASS{RESET}")
    return "ok"


def _assert_extraction_sane(extraction: dict) -> None:
    """Sanity-check the run-1 extraction before verification (cheap guard)."""
    inv = extraction["invoice"]
    assert inv.get("vendor") == GT_VENDOR, f"extracted vendor wrong: {inv.get('vendor')!r}"
    assert inv.get("invoice_number") == GT_INVOICE_NO
    assert inv.get("invoice_date") == GT_INVOICE_DATE
    assert abs(float(inv["total"]) - GT_TOTAL) < 0.01


def main() -> None:
    _check_env()
    asyncio.run(_run())


if __name__ == "__main__":
    main()