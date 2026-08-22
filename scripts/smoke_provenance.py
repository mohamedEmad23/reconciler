#!/usr/bin/env python
"""P12 smoke — provenance read path + rendering (pure-unit, no LLM calls).

Proves the "why did Reconciler do this?" view end-to-end at the unit level:
  [1] entries_from_state validates stored payloads through the Pydantic
      ProvenanceEntry model (closes the Gate 9 'never instantiated' FYI);
  [2] dict and list storage forms both read; absent provenance -> ([], []);
  [3] malformed entries surface as errors without aborting the read;
  [4] render_entry carries every judge-facing fact (discrepancy, lane, rule
      fired with real score, CoVe Q/A, rationale, recheck verdict, trace id);
  [5] render_for_digest emits the explicit missing-signal (never invents);
  [6] attach_digest_provenance attaches rendered blocks deterministically —
      only for invoices that recorded provenance, byte-stable across calls.

Free: no Vertex, no Firestore. Live wiring is covered by smoke_duplicate
(digest['provenance'] assert) and smoke_pipeline regression.

Usage:
    uv run python scripts/smoke_provenance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from reconciler.provenance import (  # noqa: E402
    _NO_PROVENANCE_LINE,
    attach_digest_provenance,
    entries_from_state,
    render_entry,
    render_for_digest,
)
from reconciler.schemas import ProvenanceEntry  # noqa: E402


def _entry_payload(**over: object) -> dict:
    payload = {
        "discrepancy_type": "duplicate_payment",
        "lane": "dispute",
        "extraction_hash": "3f9d2c771e0a4b66",
        "verification_questions": [
            "Does the bank statement contain more than one debit matching INV-2026-0421?",
            "Do both debits post to the same vendor within the statement window?",
        ],
        "verification_answers": ["Yes", "Yes"],
        "memory_keys_consulted": ["vendor:Acme Cloud Services LLC"],
        "rule_fired": "fuzzy_match(vendor, bank_row) @ 0.95",
        "resolution_rationale": "Two matching debits of $2,400.00 confirmed on 2026-08-18 and 2026-08-20.",
        "recheck_matched": None,
        "human_decision": None,
        "trace_id": "0af7651916cd43dd8d8fd0c0af781cc4",
        "timestamp": "2026-08-22T09:00:00+00:00",
    }
    payload.update(over)
    return payload


def _state(payload: object) -> dict:
    """Invoice state shaped exactly like a Firestore run_invoices doc."""
    return {
        "invoice_id": "duplicate_invoice_sample",
        "status": "completed",
        "stages_done": ["intake", "extraction", "verification", "resolution",
                        "categorization", "reconciliation"],
        "stages_data": {"resolution": {"provenance": payload}},
    }


def main() -> int:
    print("P12 provenance smoke — read path + rendering\n")

    # [1] validation through the model
    entries, errors = entries_from_state(_state(_entry_payload()))
    assert len(entries) == 1 and not errors, f"[1] parse failed: {errors}"
    e = entries[0]
    assert isinstance(e, ProvenanceEntry)
    assert e.discrepancy_type == "duplicate_payment"
    assert e.lane == "dispute"
    assert e.rule_fired == "fuzzy_match(vendor, bank_row) @ 0.95"
    assert len(e.verification_questions) == 2
    assert e.trace_id == "0af7651916cd43dd8d8fd0c0af781cc4"
    print("[1] entries_from_state validates via ProvenanceEntry model PASS")

    # [2] list form + absent provenance
    entries2, _ = entries_from_state(
        _state([_entry_payload(), _entry_payload(lane="escalate")])
    )
    assert len(entries2) == 2, f"[2] list form: expected 2, got {len(entries2)}"
    empty, no_errors = entries_from_state({"stages_data": {"resolution": {}}})
    assert empty == [] and not no_errors, "[2] absent provenance must be ([], [])"
    print("[2] dict + list forms read; absent -> ([], []) PASS")

    # [3] malformed entry surfaces as error, siblings still parse
    mixed, errs = entries_from_state(
        _state([_entry_payload(), _entry_payload(discrepancy_type="vendor_typo")])
    )
    assert len(mixed) == 1, "[3] valid sibling must still parse"
    assert len(errs) == 1 and "failed validation" in errs[0], f"[3] errs={errs}"
    print("[3] malformed entry -> error surfaced, sibling parsed PASS")

    # [4] renderer carries every judge-facing fact
    text = render_entry(e, invoice_id="duplicate_invoice_sample")
    for needle in (
        "duplicate_invoice_sample",
        "duplicate_payment",
        "dispute",
        "fuzzy_match(vendor, bank_row) @ 0.95",
        "vendor:Acme Cloud Services LLC",
        "Two matching debits",
        "Q1:",
        "A2: Yes",
        "0af7651916cd43dd8d8fd0c0af781cc4",
        "Cloud Trace",
    ):
        assert needle in text, f"[4] renderer missing {needle!r}"
    assert "pending approval" in text, "[4] dispute lane must show pending human"
    print("[4] render_entry carries discrepancy/lane/rule/QA/rationale/trace PASS")

    # recheck verdict lines (both branches)
    ok_text = render_entry(
        e.model_copy(update={"recheck_matched": True}), invoice_id="x"
    )
    bad_text = render_entry(
        e.model_copy(update={"recheck_matched": False}), invoice_id="x"
    )
    assert "CONFIRMED" in ok_text and "never self-certified" in bad_text
    print("[4b] recheck verdicts render (confirmed / not confirmed) PASS")

    # [5] missing-signal is explicit
    assert render_for_digest({"stages_data": {}}) == _NO_PROVENANCE_LINE
    mixed_render = render_for_digest(
        _state([_entry_payload(), _entry_payload(discrepancy_type="bogus")])
    )
    assert "(malformed entry skipped" in mixed_render, (
        "[5] mixed state must surface the malformed entry"
    )
    assert "duplicate_payment" in mixed_render, (
        "[5] mixed state must still render the valid entry"
    )
    print("[5] render_for_digest missing-signal explicit, never invented PASS")

    # [6] attach_digest_provenance — deterministic, selective, byte-stable
    digest: dict = {"digest_composed": True, "flagged_count": 1}
    clean_state = {"invoice_id": "invoice_sample", "stages_data": {}}
    out1 = attach_digest_provenance(digest, [clean_state, _state(_entry_payload())])
    assert set(out1["provenance"].keys()) == {"duplicate_invoice_sample"}, (
        f"[6] clean invoice must stay out: {list(out1['provenance'])}"
    )
    out2 = attach_digest_provenance({"digest_composed": True},
                                    [clean_state, _state(_entry_payload())])
    assert out1["provenance"] == out2["provenance"], "[6] not byte-stable"
    empty_out = attach_digest_provenance({}, [clean_state])
    assert "provenance" not in empty_out, "[6] no entries -> no key"
    print("[6] attach_digest_provenance selective + deterministic PASS")

    print("\nsmoke_provenance PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
