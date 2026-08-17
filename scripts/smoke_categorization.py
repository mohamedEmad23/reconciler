"""Phase 3.5a smoke — Categorization agent maps line items to the COA.

Anti-hallucination proof:
  - every returned account_code ∈ closed chart of accounts (schema-enforced)
  - substance-over-keyword: 'Premier Support Tier' → 6000 Professional Services
  - vendor memory grounding: injected 'Acme…=5000' mapping is followed + echoed
  - determinism at temperature=0.0 across 2 runs

Uses the ground-truth fixture invoice (same as tests/fixtures/make_fixtures.py)
so no extraction call is needed — this smoke isolates the Categorization stage.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from google.adk import Agent  # noqa: F401
from google.adk.runners import InMemoryRunner
from google.genai import types

from reconciler import config
from reconciler.categorization import categorization_agent

GROUND_TRUTH_ITEMS = [
    "Compute Engine -- vCPU hours (n2-standard-4)",
    "Cloud Storage -- multi-region GB-month",
    "Vertex AI Gemini API -- input tokens (per 1M)",
    "Premier Support Tier -- monthly",
]
EXPECTED_CODES = {"5000", "5010", "6000"}


def _check_env() -> None:
    for var in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    ):
        if not os.environ.get(var):
            raise SystemExit(f"missing env {var} — see module docstring")
    assert config.GEMINI_MODEL == "gemini-2.5-flash", config.GEMINI_MODEL


async def _categorize_once(runner: InMemoryRunner, session_id: str) -> dict:
    await runner.session_service.create_session(
        app_name="smoke_categorization", user_id="smoke", session_id=session_id
    )
    invoice_json = {
        "vendor": "Acme Cloud Services LLC",
        "invoice_number": "INV-2026-0417",
        "line_items": [{"description": d} for d in GROUND_TRUTH_ITEMS],
        "subtotal": 430.88,
        "tax": 36.62,
        "total": 467.50,
    }
    prompt = (
        "Categorize this invoice. Shared memory says this vendor historically "
        "maps to: Acme Cloud Services LLC=5000 (cloud infrastructure).\n\n"
        f"INVOICE JSON:\n{json.dumps(invoice_json, indent=1)}"
    )
    final = None
    async for ev in runner.run_async(
        user_id="smoke",
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
    ):
        if ev.is_final_response() and final is None and ev.content and ev.content.parts:
            final = ev.content.parts[0].text
    assert final, "no final response"
    return json.loads(final)


async def main() -> None:
    _check_env()

    # single_turn cannot run as root — clone to mode='chat' (same native
    # output_schema enforcement branch; validated in Gate 2).
    root = categorization_agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=root, app_name="smoke_categorization")

    r1 = await _categorize_once(runner, "s1")
    r2 = await _categorize_once(runner, "s2")

    codes = [i.get("account_code") for i in r1["items"]]
    assert len(r1["items"]) == 4, r1["items"]
    assert all(c in {"5000", "5010", "5100", "5200", "6000", "6100", "6200", "6300", "7000", "9000"} for c in codes), codes
    assert EXPECTED_CODES.issubset(set(codes)), f"expected 5000/5010/6000 coverage, got {codes}"
    # substance-over-keyword: Premier Support is a service, not software
    support = next(i for i in r1["items"] if "Support" in (i.get("description") or ""))
    assert support["account_code"] == "6000", support
    # vendor memory grounding echoed
    assert any("5000" in m for m in r1.get("known_vendor_mappings", [])), r1.get("known_vendor_mappings")

    det = json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    assert det, "non-deterministic across runs"

    print("run-1 codes:", {i['description'][:28]: i['account_code'] for i in r1['items']})
    print("vendor mappings echoed:", r1["known_vendor_mappings"])
    print("determinism:", det)
    print("smoke_categorization PASS")


if __name__ == "__main__":
    asyncio.run(main())
