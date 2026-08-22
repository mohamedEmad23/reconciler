"""P14 closed-loop learning smoke — FREE (no LLM calls; Firestore yes).

Proves the human-in-the-loop fact writes and that the pipeline's fact reads
would surface them (the consumption path already exists in
``pipeline._evidence_packet`` via ``SharedMemory.get_fact``).

Checks:
[1] record_approval_facts writes prior_invoice + vendor facts (positive).
[2] merge extends (not replaces) — approve twice, assert lists dedupe.
[3] record_rejection_fact writes NOT_resolved + rejected_invoices (negative).
[4] get_fact returns the alias/code facts — the exact reads _evidence_packet
    performs, proving run-2 consumption.
[5] hermetic cleanup.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from reconciler.learning import record_approval_facts, record_rejection_fact
from reconciler.memory import SharedMemory

VENDOR = "Learning Test Co LLC"
INVOICE_NO = "LRN-2026-0001"
TOTAL = 1234.56
CODES = ["5000", "5010"]


async def main() -> None:
    memory = SharedMemory()
    client = memory.client

    # [1] approve → positive facts
    r1 = await record_approval_facts(
        client=client,
        vendor=VENDOR,
        invoice_number=INVOICE_NO,
        total=TOTAL,
        discrepancy_types=["amount_mismatch"],
        account_codes=CODES,
    )
    assert r1["status"] == "recorded", r1
    pi = await memory.get_fact(namespace="prior_invoice", key=INVOICE_NO)
    assert pi and pi.get("resolved") is True and pi.get("total") == TOTAL, pi
    v = await memory.get_fact(namespace="vendor", key=VENDOR)
    assert v and set(v.get("account_codes", [])) >= set(CODES), v
    assert INVOICE_NO in v.get("approved_invoices", []), v
    print("[1] approve writes prior_invoice + vendor facts (positive) PASS")

    # [2] merge extends, dedupe
    await record_approval_facts(
        client=client,
        vendor=VENDOR,
        invoice_number="LRN-2026-0002",
        total=99.0,
        discrepancy_types=["amount_mismatch"],
        account_codes=["6000"],
    )
    v2 = await memory.get_fact(namespace="vendor", key=VENDOR)
    assert set(v2.get("account_codes", [])) >= {"5000", "5010", "6000"}, v2
    assert v2.get("approved_invoices") == ["LRN-2026-0001", "LRN-2026-0002"], v2
    print("[2] merge extends account_codes + approved_invoices (dedupe) PASS")

    # [3] reject → negative facts
    r3 = await record_rejection_fact(
        client=client, vendor=VENDOR, invoice_number="LRN-2026-0003", reason="duplicate already refunded"
    )
    assert r3["status"] == "recorded", r3
    pi3 = await memory.get_fact(namespace="prior_invoice", key="LRN-2026-0003")
    assert pi3 and pi3.get("NOT_resolved") is True and "refunded" in pi3.get("reason", ""), pi3
    v3 = await memory.get_fact(namespace="vendor", key=VENDOR)
    assert "LRN-2026-0003" in v3.get("rejected_invoices", []), v3
    print("[3] reject writes NOT_resolved + rejected_invoices (negative) PASS")

    # [4] consumption read — the exact call _evidence_packet makes
    alias_fact = await memory.get_fact(namespace="vendor", key=VENDOR)
    prior_fact = await memory.get_fact(namespace="prior_invoice", key=INVOICE_NO)
    assert alias_fact is not None and "account_codes" in alias_fact
    assert prior_fact is not None and prior_fact.get("resolved") is True
    print("[4] get_fact surfaces facts for run-2 resolution consumption PASS")

    # [5] cleanup
    await memory.delete_fact(namespace="vendor", key=VENDOR)
    for k in (INVOICE_NO, "LRN-2026-0002", "LRN-2026-0003"):
        await memory.delete_fact(namespace="prior_invoice", key=k)
    assert await memory.get_fact(namespace="vendor", key=VENDOR) is None
    print("[5] hermetic cleanup PASS")

    print("smoke_learning PASS")


if __name__ == "__main__":
    asyncio.run(main())
