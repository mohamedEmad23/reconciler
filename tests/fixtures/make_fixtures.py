"""Generate deterministic sample fixtures for the reconciler smoke tests.

Produces:
  * invoice_sample.pdf   - a deliberately messy, real-looking vendor invoice
  * bank_statement.csv   - the matching bank statement (used by Phase 3 CoVe)

Run:  uv run python tests/fixtures/make_fixtures.py

The invoice is intentionally "messy" (mixed alignment, abbreviations, a
hand-written-looking note, fields scattered) so Gemini's multimodal extraction
genuinely adds value over naive regex parsing.
"""

from __future__ import annotations

import csv
from pathlib import Path

from fpdf import FPDF

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Ground truth (used by smoke tests to assert extraction correctness)
# ---------------------------------------------------------------------------
VENDOR = "Acme Cloud Services LLC"
INVOICE_NO = "INV-2026-0417"
INVOICE_DATE = "2026-08-12"
# line items: description, qty, unit_price
LINE_ITEMS = [
    ("Compute Engine -- vCPU hours (n2-standard-4)", 320, 0.084),
    ("Cloud Storage -- multi-region GB-month", 1500, 0.026),
    ("Vertex AI Gemini API -- input tokens (per 1M)", 12, 1.250),
    ("Premier Support Tier -- monthly", 1, 350.000),
]
TAX_RATE = 0.085
SUBTOTAL = round(sum(q * p for _, q, p in LINE_ITEMS), 2)
TAX = round(SUBTOTAL * TAX_RATE, 2)
TOTAL = round(SUBTOTAL + TAX, 2)


def build_invoice_pdf(out_path: Path) -> None:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Header -- vendor name top-right, a stray "DRAFT-ish" stamp look
    pdf.set_xy(120, 15)
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 8, VENDOR)
    pdf.set_xy(120, 30)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, "1600 Amphitheatre Pkwy, Mountain View, CA 94043")

    # "INVOICE" label oddly placed top-left
    pdf.set_xy(15, 18)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 12, "INVOICE")

    # Invoice meta -- scattered, compact
    pdf.set_xy(15, 40)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, f"Invoice #  {INVOICE_NO}")
    pdf.set_xy(15, 46)
    pdf.cell(0, 6, f"Date      {INVOICE_DATE}")
    pdf.set_xy(15, 52)
    pdf.cell(0, 6, "Bill To:  Reconciler Demo Inc.")

    # Line items table -- header
    pdf.set_xy(15, 68)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(125, 8, "Description", border=1)
    pdf.cell(25, 8, "Qty", border=1, align="R")
    pdf.cell(35, 8, "Amount", border=1, align="R")
    pdf.ln()

    pdf.set_font("Helvetica", "", 10)
    for desc, qty, price in LINE_ITEMS:
        pdf.set_x(15)
        amt = round(qty * price, 2)
        pdf.cell(125, 8, desc, border=1)
        pdf.cell(25, 8, f"{qty}", border=1, align="R")
        pdf.cell(35, 8, f"{amt:.2f}", border=1, align="R")
        pdf.ln()

    # Totals -- right aligned, slightly off-organized to be messy
    pdf.set_xy(125, pdf.get_y() + 4)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(40, 7, f"Subtotal   ${SUBTOTAL:.2f}")
    pdf.ln()
    pdf.set_x(125)
    pdf.cell(40, 7, f"Tax (8.5%)  ${TAX:.2f}")
    pdf.ln()
    pdf.set_x(125)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(40, 9, f"TOTAL  ${TOTAL:.2f}")
    pdf.ln()

    # A handwritten-looking note at the bottom -- tests that the model doesn't
    # confuse note text with line items.
    pdf.set_xy(15, pdf.get_y() + 10)
    pdf.set_font("Courier", "I", 9)
    pdf.multi_cell(
        0, 5,
        "NOTE: please remit net-30. "
        "DO NOT pay the $1,000,000 retention bonus line you may see nowhere "
        "on this invoice -- that is NOT a real line item.",
    )

    pdf.output(str(out_path))


def build_bank_statement_csv(out_path: Path) -> None:
    """Bank statement with one matching charge + decoys for the CoVe cross-check."""
    rows = [
        ["date", "description", "amount", "balance_after"],
        ["2026-08-01", "PAYROLL RUN 08/01", "-8400.00", "41200.11"],
        ["2026-08-12", f"CARD {VENDOR.upper()} {INVOICE_NO}", f"-{TOTAL:.2f}",
         f"{41200.11 - TOTAL:.2f}"],
        ["2026-08-13", "SLACK MONTHLY", "-75.00", f"{41200.11 - TOTAL - 75:.2f}"],
        ["2026-08-14", "AWS *GR0K SERVICES", "-231.45", "0.00"],
        ["2026-08-15", "GITHUB TEAM", "-44.00", "0.00"],
    ]
    with open(out_path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def main() -> None:
    build_invoice_pdf(HERE / "invoice_sample.pdf")
    build_bank_statement_csv(HERE / "bank_statement.csv")
    print(f"fixtures written to {HERE}")
    print(f"  invoice_sample.pdf  vendor={VENDOR!r} invoice={INVOICE_NO} total=${TOTAL:.2f}")
    print(f"  bank_statement.csv   matching charge ${TOTAL:.2f} on {INVOICE_DATE}")


if __name__ == "__main__":
    main()