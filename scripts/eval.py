#!/usr/bin/env python3
"""Eval harness — reproducible numbers for the reconciler (closed-loop §7).

Runs the real agent pipeline over the labeled fixture set and prints the
metrics a judge can reproduce from the repo:

  1. Extraction field accuracy  — % of ground-truth fields matched per invoice
  2. Hallucinated-entity rate   — invented fields (the $1,000,000 decoy canary)
  3. Verification recall/FP     — 5 injected discrepancies caught, 0 false positives
  4. Resolution re-verify rate  — auto-resolves only stamped after clean recheck
  5. Dollars at risk            — the $2,400 duplicate-payment money moment

Metrics target (closed-loop §7): extraction 100% clean / >95% messy,
hallucinated entities 0, injected discrepancy recall 5/5, resolution re-verify
pass rate 100%.

Cost: ~10 Vertex calls (2 extraction + 6 verification + 2 resolution) at
temp=0.0 ≈ $0.06-0.08. Single runs (not double-run determinism — the smokes
already prove temp=0.0 determinism).

Run:
  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
  GOOGLE_GENAI_USE_VERTEXAI=1 \
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
  GOOGLE_CLOUD_LOCATION=us-central1 \
  uv run python scripts/eval.py

Exit 0 == eval completed (prints the metrics table; also writes
docs/eval-results.md).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import make_fixtures as fx  # noqa: E402  (ground truth, single source of truth)

FIXTURES = ROOT / "tests" / "fixtures"
FIXTURES_DUP = ROOT / "tests" / "fixtures_duplicate"

OK = "\033[32m"
FAIL = "\033[31m"
RESET = "\033[0m"


def _check_env() -> None:
    missing = [
        v for v in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
        ) if not os.environ.get(v)
    ]
    if missing:
        print(f"{FAIL}missing env: {', '.join(missing)}{RESET}", file=sys.stderr)
        raise SystemExit(2)


def _parse_json_response(text: str) -> dict:
    text = text.strip().removeprefix("```json").removeprefix("```").strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return json.loads(text)


async def _run_agent(agent, parts, *, app_name: str, user_id: str) -> dict:
    """Drive a specialist as a root-legal mode='chat' clone; return parsed JSON."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    clone = agent.model_copy(update={"mode": "chat"})
    runner = InMemoryRunner(agent=clone, app_name=app_name)
    session_id = f"{app_name}_{os.urandom(4).hex()}"
    await runner.session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    final_text: str | None = None
    async for ev in runner.run_async(
        user_id=user_id, session_id=session_id,
        new_message=types.Content(role="user", parts=parts),
    ):
        if ev.is_final_response() and final_text is None and ev.content and ev.content.parts:
            final_text = ev.content.parts[0].text
    if final_text is None:
        raise RuntimeError(f"{app_name}: no final response")
    return _parse_json_response(final_text)


async def _extract(pdf_path: Path) -> dict:
    from google.genai import types
    from reconciler.extraction import extraction_agent

    pdf_bytes = pdf_path.read_bytes()
    assert pdf_bytes[:4] == b"%PDF"
    return await _run_agent(
        extraction_agent,
        [
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            types.Part.from_text(
                text=(
                    "Extract this invoice into the ExtractionResult JSON. "
                    "Use null for any field not present. The 'NOTE' at the "
                    "bottom is not a line item."
                )
            ),
        ],
        app_name="eval-extraction",
        user_id="eval",
    )


async def _verify(invoice_dict: dict, csv_text: str) -> dict:
    from google.genai import types
    from reconciler.verification import verification_agent

    payload = (
        "INVOICE (extraction result JSON):\n"
        f"{json.dumps(invoice_dict, indent=2)}\n\n"
        "BANK STATEMENT CSV:\n"
        f"{csv_text}\n\n"
        "Verify the invoice against the bank statement using CoVe. "
        "Emit the VerificationResult JSON."
    )
    return await _run_agent(
        verification_agent,
        [types.Part.from_text(text=payload)],
        app_name="eval-verification",
        user_id="eval",
    )


async def _resolve(discrepancies, verification, invoice, evidence) -> dict:
    from google.genai import types
    from reconciler.resolution import resolution_agent

    prompt = (
        "DISCREPANCIES (from verification):\n" + json.dumps(discrepancies, indent=2)
        + "\n\nVERIFICATION confidence: " + str(verification.get("confidence", 0.0))
        + "\nCoVe q/a: " + json.dumps(
            list(zip(verification.get("verification_questions", []),
                     verification.get("verification_answers", [])))[:5]
        )
        + "\n\nINVOICE (extracted):\n" + json.dumps(invoice, indent=2)
        + "\n\nEVIDENCE PACKET (the only evidence that exists):\n"
        + json.dumps(evidence, indent=2)
        + "\n\nApply the decision table. Emit ResolutionAction."
    )
    return await _run_agent(
        resolution_agent,
        [types.Part.from_text(text=prompt)],
        app_name="eval-resolution",
        user_id="eval",
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _field_accuracy(extraction: dict, expected: dict) -> tuple[list[str], list[str]]:
    """Return (correct_fields, wrong_fields) comparing extraction vs expected."""
    inv = extraction.get("invoice") or {}
    checks = {
        "vendor": (inv.get("vendor"), expected["vendor"]),
        "invoice_number": (inv.get("invoice_number"), expected["invoice_number"]),
        "invoice_date": (inv.get("invoice_date"), expected["invoice_date"]),
        "total": (round(float(inv.get("total") or 0), 2), expected["total"]),
        "line_item_count": (len(inv.get("line_items") or []), expected["line_items"]),
    }
    correct, wrong = [], []
    for field, (got, want) in checks.items():
        (correct if got == want else wrong).append(field)
    return correct, wrong


def _hallucinated_entities(extraction: dict, expected: dict) -> list[str]:
    """Detect invented line items / amounts not in the source fixture."""
    inv = extraction.get("invoice") or {}
    items = inv.get("line_items") or []
    expected_items = expected["line_items"]
    hallu = []
    for it in items:
        desc = (it.get("description") or "").lower()
        amt = it.get("amount")
        # The decoy canary: the $1,000,000 retention bonus must never appear.
        if "retention" in desc or "1,000,000" in desc or (amt is not None and amt >= 100000):
            hallu.append(f"decoy_canary:{it.get('description')!r}")
        elif len(items) > expected_items:
            hallu.append(f"extra_line_item:{it.get('description')!r}")
    if len(items) != expected_items:
        hallu.append(f"line_item_count_mismatch:{len(items)}!={expected_items}")
    return hallu


def _metric_extraction() -> tuple[dict, dict]:
    """Extract both labeled invoices; return per-invoice field-accuracy + hallucination."""
    from make_fixtures import (  # noqa: F401
        DUP_INVOICE_DATE, DUP_INVOICE_NO, DUP_TOTAL, INVOICE_DATE, INVOICE_NO,
        TOTAL, VENDOR,
    )

    expected_clean = {
        "vendor": VENDOR, "invoice_number": INVOICE_NO,
        "invoice_date": INVOICE_DATE, "total": round(TOTAL, 2), "line_items": 4,
    }
    expected_dup = {
        "vendor": VENDOR, "invoice_number": DUP_INVOICE_NO,
        "invoice_date": DUP_INVOICE_DATE, "total": round(DUP_TOTAL, 2), "line_items": 1,
    }
    clean_ext = asyncio.run(_extract(FIXTURES / "invoice_sample.pdf"))
    dup_ext = asyncio.run(_extract(FIXTURES_DUP / "duplicate_invoice_sample.pdf"))

    results = {}
    for label, ext, exp in (
        ("invoice_sample", clean_ext, expected_clean),
        ("duplicate_invoice_sample", dup_ext, expected_dup),
    ):
        correct, wrong = _field_accuracy(ext, exp)
        results[label] = {
            "correct": correct, "wrong": wrong,
            "accuracy": round(len(correct) / (len(correct) + len(wrong)) * 100, 1),
            "hallucinated": _hallucinated_entities(ext, exp),
        }
    return results, {"clean_ext": clean_ext, "dup_ext": dup_ext}


def _metric_verification(clean_ext: dict) -> dict:
    """Injected-discrepancy recall + false-positive rate via CoVe."""
    from reconciler.schemas import DISCREPANCY_TYPES

    csv_text = (FIXTURES / "bank_statement.csv").read_text()
    clean_invoice = clean_ext["invoice"]

    # 0) False-positive check: clean invoice + matching bank -> matched, 0 discrepancies.
    happy = asyncio.run(_verify(clean_invoice, csv_text))

    # Injections: (label, mutated_invoice, bank_csv_text, expected_type)
    def _mutate(**overrides):
        inv = copy.deepcopy(clean_invoice)
        inv.update(overrides)
        return inv

    amount_mismatch = _mutate(total=999.99)
    vendor_mismatch = _mutate(vendor="Totally Different Corp")
    date_mismatch = _mutate(invoice_date="2026-09-30")
    number_mismatch = _mutate(invoice_number="INV-2026-9999")

    # duplicate_payment: use the duplicate invoice + the double-debit bank CSV.
    dup_csv_text = (FIXTURES_DUP / "bank_statement.csv").read_text()
    dup_invoice = {
        "vendor": fx.VENDOR,
        "invoice_number": fx.DUP_INVOICE_NO,
        "invoice_date": fx.DUP_INVOICE_DATE,
        "total": round(fx.DUP_TOTAL, 2),
        "subtotal": round(fx.DUP_SUBTOTAL, 2),
        "tax": round(fx.DUP_TAX, 2),
        "line_items": [{"description": d, "quantity": q, "unit_price": p,
                        "amount": round(q * p, 2)}
                       for d, q, p in fx.DUP_LINE_ITEMS],
    }

    injections = [
        ("amount_mismatch", amount_mismatch, csv_text, {"amount_mismatch", "no_bank_match"}),
        ("vendor_mismatch", vendor_mismatch, csv_text, {"vendor_mismatch", "no_bank_match"}),
        ("date_mismatch", date_mismatch, csv_text, {"date_mismatch", "no_bank_match"}),
        ("invoice_number_mismatch", number_mismatch, csv_text, {"invoice_number_mismatch", "no_bank_match"}),
        ("duplicate_payment", dup_invoice, dup_csv_text, {"duplicate_payment"}),
    ]

    caught = 0
    rows = []
    for label, inv, bank, expected_types in injections:
        verdict = asyncio.run(_verify(inv, bank))
        disc_types = {d.get("type") for d in (verdict.get("discrepancies") or [])}
        hit = bool(disc_types & expected_types)
        caught += 1 if hit else 0
        rows.append({
            "injection": label,
            "matched": verdict.get("matched"),
            "detected_types": sorted(disc_types),
            "expected": sorted(expected_types),
            "caught": hit,
        })

    false_positives = 1 if (happy.get("matched") is False
                            or (happy.get("discrepancies") or [])) else 0

    return {
        "recall": f"{caught}/{len(injections)}",
        "recall_ratio": round(caught / len(injections), 2),
        "false_positives": false_positives,
        "rows": rows,
    }


def _metric_resolution(clean_ext: dict) -> dict:
    """Resolution re-verify closure + the duplicate-payment dispute clamp."""
    # Case R1: amount_mismatch with a corrected invoice -> resolve + recheck.
    discrepancies = [{
        "type": "amount_mismatch",
        "description": "Invoice total 999.99 vs bank row 467.50 (transposition/fuzzy).",
        "invoice_value": "999.99",
        "bank_value": "467.50",
    }]
    verification = {"confidence": 0.96,
                    "verification_questions": ["Does a bank row match within $0.02?"],
                    "verification_answers": ["No — but 467.50 is the closest row."]}
    invoice = copy.deepcopy(clean_ext["invoice"])
    invoice["total"] = 999.99
    evidence = {
        "bank_rows": [{"date": "2026-08-12",
                       "description": f"CARD {fx.VENDOR.upper()} {fx.INVOICE_NO}",
                       "amount": f"-{fx.TOTAL:.2f}"}],
        "vendor_alias_fact": None,
        "prior_invoice_fact": None,
        "best_vendor_row": {"fuzzy": 1.0, "amount": f"-{fx.TOTAL:.2f}"},
        "number_fuzzy": {"score": 1.0},
        "date_delta_days": 0,
        "amount_rows": [{"abs_amount": fx.TOTAL, "exact": True, "digit_transposition": False}],
    }
    r1 = asyncio.run(_resolve(discrepancies, verification, invoice, evidence))
    lane1 = (r1.get("decision") or {}).get("lane")
    corrected = r1.get("corrected_invoice")
    recheck_clean = None
    if lane1 == "resolve" and corrected:
        recheck = asyncio.run(_verify(corrected, (FIXTURES / "bank_statement.csv").read_text()))
        recheck_clean = bool(recheck.get("matched") and not (recheck.get("discrepancies") or []))

    # Case R2: duplicate_payment -> hard-clamped to dispute + draft amount_at_risk.
    dup_disc = [{
        "type": "duplicate_payment",
        "description": "Two bank debits reference the same invoice number.",
        "invoice_value": f"{fx.DUP_TOTAL:.2f}",
        "bank_value": f"{fx.DUP_TOTAL * 2:.2f}",
    }]
    dup_verification = {"confidence": 0.93,
                        "verification_questions": ["Does the invoice number appear twice?"],
                        "verification_answers": ["Yes — two matching debits."]}
    dup_invoice = {
        "vendor": fx.VENDOR, "invoice_number": fx.DUP_INVOICE_NO,
        "invoice_date": fx.DUP_INVOICE_DATE, "total": round(fx.DUP_TOTAL, 2),
    }
    dup_evidence = {
        "bank_rows": [
            {"date": "2026-08-18", "description": f"CARD {fx.VENDOR.upper()} {fx.DUP_INVOICE_NO}", "amount": f"-{fx.DUP_TOTAL:.2f}"},
            {"date": "2026-08-20", "description": f"CARD {fx.VENDOR.upper()} {fx.DUP_INVOICE_NO}", "amount": f"-{fx.DUP_TOTAL:.2f}"},
        ],
        "vendor_alias_fact": None,
        "prior_invoice_fact": None,
        "best_vendor_row": {"fuzzy": 1.0, "amount": f"-{fx.DUP_TOTAL:.2f}"},
        "number_fuzzy": {"score": 1.0},
        "date_delta_days": 0,
        "amount_rows": [
            {"abs_amount": fx.DUP_TOTAL, "exact": True, "digit_transposition": False},
            {"abs_amount": fx.DUP_TOTAL, "exact": True, "digit_transposition": False},
        ],
    }
    r2 = asyncio.run(_resolve(dup_disc, dup_verification, dup_invoice, dup_evidence))
    lane2 = (r2.get("decision") or {}).get("lane")
    draft = r2.get("dispute_draft") or {}

    return {
        "amount_mismatch_lane": lane1,
        "amount_mismatch_recheck_clean": recheck_clean,
        "duplicate_payment_lane": lane2,
        "duplicate_payment_amount_at_risk": draft.get("amount_at_risk"),
        "reverify_pass_rate": "1/1" if recheck_clean is True else ("0/1" if recheck_clean is False else "n/a"),
    }


def main() -> None:
    _check_env()
    print(f"{OK}reconciler eval — reproducible metrics{RESET}\n")

    print(f"{OK}[1] extraction accuracy + hallucination{RESET}")
    extraction_results, payloads = _metric_extraction()
    for label, r in extraction_results.items():
        print(f"  {label}: accuracy={r['accuracy']}% "
              f"wrong_fields={r['wrong']} hallucinated={r['hallucinated']}")

    print(f"\n{OK}[2] verification recall / false-positives{RESET}")
    v = _metric_verification(payloads["clean_ext"])
    for row in v["rows"]:
        mark = OK if row["caught"] else FAIL
        print(f"  {row['injection']}: matched={row['matched']} "
              f"detected={row['detected_types']} {mark}caught={row['caught']}{RESET}")
    print(f"  recall={v['recall']} false_positives={v['false_positives']}")

    print(f"\n{OK}[3] resolution re-verify closure + dispute clamp{RESET}")
    r = _metric_resolution(payloads["clean_ext"])
    print(f"  amount_mismatch: lane={r['amount_mismatch_lane']} "
          f"recheck_clean={r['amount_mismatch_recheck_clean']}")
    print(f"  duplicate_payment: lane={r['duplicate_payment_lane']} "
          f"amount_at_risk=${r['duplicate_payment_amount_at_risk']}")
    print(f"  re-verify pass rate={r['reverify_pass_rate']}")

    print(f"\n{OK}[4] dollars at risk (money moment){RESET}")
    print(f"  duplicate_payment amount_at_risk=${fx.DUP_TOTAL:,.2f} "
          f"(dollars_recovered only increments on APPROVED disputes + re-verified corrections)")

    # Write the machine-readable results.
    results = {
        "extraction": extraction_results,
        "verification": {"recall": v["recall"], "false_positives": v["false_positives"],
                         "rows": v["rows"]},
        "resolution": r,
        "dollars_at_risk": fx.DUP_TOTAL,
    }
    out_path = ROOT / "docs" / "eval-results.md"
    out_path.write_text(
        "# Eval results (generated by scripts/eval.py)\n\n"
        f"```json\n{json.dumps(results, indent=2, default=str)}\n```\n"
    )
    print(f"\n{OK}eval complete — results written to {out_path}{RESET}")


if __name__ == "__main__":
    main()
