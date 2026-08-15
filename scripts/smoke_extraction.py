"""Phase 2 extraction smoke — the anti-hallucination proof.

Drives the Extraction agent DIRECTLY with the sample invoice PDF (multimodal:
PDF inline as Part.from_bytes + a text trigger) and asserts:

  1. output_schema enforced: reply is JSON conforming to ExtractionResult.
  2. anti-fabrication: every non-null value was actually in the PDF
     (ground-truth asserts on vendor, invoice_number, invoice_date, total).
  3. decoy rejection: the NOTE's fake "$1,000,000 retention bonus" is NOT
     extracted as a line item (the contract + FCoT VERIFY step must catch this).
  4. monetary consistency (Contract rule 3): subtotal + tax == total.
  5. determinism at temp=0.0: running twice yields identical structured JSON.

Run (same Vertex env as scripts/smoke.py):

  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
  GOOGLE_GENAI_USE_VERTEXAI=1 \
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
  GOOGLE_CLOUD_LOCATION=us-central1 \
  uv run python scripts/smoke_extraction.py

Exit code 0 == Phase 2 smoke passed. Real Vertex calls (~$0.01 total for two
runs); deterministic so the numbers are assertable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

FAIL = "\033[31m"
OK = "\033[32m"
RESET = "\033[0m"

# Ground truth, mirrored from tests/fixtures/make_fixtures.py (DTT source).
GT_VENDOR = "Acme Cloud Services LLC"
GT_INVOICE_NO = "INV-2026-0417"
GT_INVOICE_DATE = "2026-08-12"
GT_TOTAL = 467.50
GT_SUBTOTAL = 430.88
GT_TAX = 36.62
GT_LINE_ITEMS = 4

FIXTURE_PDF = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "invoice_sample.pdf"
)


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
    """Run the Extraction agent once on the sample PDF; return parsed dict."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "agents"))
    from reconciler.extraction import extraction_agent  # type: ignore
    from reconciler import config  # type: ignore

    assert extraction_agent.generate_content_config.temperature == 0.0, (
        "extraction temperature must be 0.0"
    )
    assert extraction_agent.output_schema is not None, "output_schema must be set"
    assert not extraction_agent.tools, (
        "extraction must have NO tools for native schema enforcement"
    )

    pdf_bytes = FIXTURE_PDF.read_bytes()
    assert pdf_bytes[:4] == b"%PDF", f"fixture not a PDF: {FIXTURE_PDF}"

    # The shipped extraction_agent uses mode='single_turn' so the Supervisor
    # auto-wraps it as an inline tool. ADK forbids 'single_turn' as a ROOT agent
    # driven directly by a Runner ("must have mode='chat' or 'task'"). For this
    # smoke only, clone with mode='chat' — identical model/instruction/schema/
    # temp config. We must NOT use mode='task' because ADK's output_schema
    # native enforcement is SKIPPED for task mode (collected via finish_task
    # tool instead). 'chat' keeps output_schema enforcement AND is root-legal.
    # A single message -> single response works fine under 'chat'.
    root_extraction = extraction_agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=root_extraction, app_name=config.APP_NAME)
    session = await runner.session_service.create_session(
        app_name=config.APP_NAME, user_id="smoke-extraction"
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
        user_id="smoke-extraction",
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
        print(f"{FAIL}no final response{RESET}", file=sys.stderr)
        raise SystemExit(1)

    text = final_text.strip().removeprefix("```json").removeprefix("```").strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"{FAIL}reply not JSON: {exc}\n---\n{final_text}{RESET}", file=sys.stderr)
        raise SystemExit(1)
    return parsed


def _assert_extraction(name: str, parsed: dict) -> None:
    inv = parsed.get("invoice")
    assert isinstance(inv, dict), f"{name}: missing invoice object: {parsed!r}"

    # 1. Ground truth (anti-fabrication).
    assert inv.get("vendor") == GT_VENDOR, (
        f"{name}: vendor={inv.get('vendor')!r} expected {GT_VENDOR!r}"
    )
    assert inv.get("invoice_number") == GT_INVOICE_NO, (
        f"{name}: invoice_number={inv.get('invoice_number')!r} expected {GT_INVOICE_NO!r}"
    )
    assert inv.get("invoice_date") == GT_INVOICE_DATE, (
        f"{name}: invoice_date={inv.get('invoice_date')!r} expected {GT_INVOICE_DATE!r}"
    )
    assert inv.get("total") is not None, f"{name}: total is null"
    assert abs(inv["total"] - GT_TOTAL) < 0.01, (
        f"{name}: total={inv['total']!r} expected {GT_TOTAL}"
    )

    # 2. Line item count + decoy rejection (the heart of the FCoT VERIFY step).
    items = inv.get("line_items") or []
    assert len(items) == GT_LINE_ITEMS, (
        f"{name}: {len(items)} line_items, expected {GT_LINE_ITEMS} (decoy leaked?)"
    )
    for it in items:
        amt = it.get("amount")
        assert amt is None or amt < 100000.0, (
            f"{name}: decoy $1M line item leaked: {it!r}"
        )
        desc = (it.get("description") or "").lower()
        assert "retention" not in desc, (
            f"{name}: decoy 'retention bonus' leaked into line_items: {it!r}"
        )

    # 3. Monetary consistency (Contract rule 3): subtotal + tax == total.
    sub, tax, tot = inv.get("subtotal"), inv.get("tax"), inv.get("total")
    if sub is not None and tax is not None and tot is not None:
        assert abs((sub + tax) - tot) < 0.02, (
            f"{name}: monetary inconsistency: {sub}+{tax}={sub+tax} != {tot}"
        )

    print(
        f"{OK}{name} PASS: vendor={inv['vendor']!r} inv={inv['invoice_number']!r} "
        f"date={inv['invoice_date']!r} total={inv['total']} "
        f"lines={len(items)} conf={parsed.get('confidence')} "
        f"missing={parsed.get('missing_fields')}{RESET}"
    )


async def _run() -> str:
    print(f"{OK}Phase 2 extraction smoke — {FIXTURE_PDF.name}{RESET}")
    first = await _extract_once()
    _assert_extraction("run-1", first)

    second = await _extract_once()
    _assert_extraction("run-2", second)

    # Determinism at temp=0.0: identical structured JSON both runs.
    # Compare the full ExtractionResult (invoice + confidence + missing_fields),
    # not just the invoice, for a strictly stronger determinism proof.
    first_full = json.dumps(first, sort_keys=True)
    second_full = json.dumps(second, sort_keys=True)
    if first_full != second_full:
        print(f"{FAIL}NON-DETERMINISTIC at temp=0.0:{RESET}", file=sys.stderr)
        print(f"  run-1: {first_full}", file=sys.stderr)
        print(f"  run-2: {second_full}", file=sys.stderr)
        raise SystemExit(1)
    print(f"{OK}determinism PASS: identical structured output across 2 runs{RESET}")
    print(f"{OK}smoke_extraction PASS{RESET}")
    return "ok"


def main() -> None:
    _check_env()
    asyncio.run(_run())


if __name__ == "__main__":
    main()