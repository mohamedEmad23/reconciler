"""Generate deterministic sample fixtures for the reconciler smoke tests.

Produces:
  * invoice_sample.pdf   - a deliberately messy, real-looking vendor invoice
  * bank_statement.csv   - the matching bank statement (used by Phase 3 CoVe)
  * fixtures_duplicate/  - P11 money-moment set: the clean invoice PLUS a
    second invoice from the same vendor that the bank charged TWICE
    (duplicate_payment) -> resolution drafts a $2,400 dispute.

Run:  uv run python tests/fixtures/make_fixtures.py

The invoice is intentionally "messy" (mixed alignment, abbreviations, a
hand-written-looking note, fields scattered) so Gemini's multimodal extraction
genuinely adds value over naive regex parsing.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

HERE = Path(__file__).parent

# Pinned creation date so fixture regeneration is byte-idempotent (fpdf2
# otherwise stamps /CreationDate from the wall clock on every run).
FIXTURE_CREATED_AT = datetime(2026, 8, 11, 8, 0, 0)

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

# ---------------------------------------------------------------------------
# Duplicate-payment fixture (P11 "money moment"): the SAME vendor issues a
# second invoice, and the bank statement contains TWO matching debits for it
# (classic double-charge). Verification must flag duplicate_payment; the
# hard clamp in pipeline._close_resolution forces lane=dispute, and the
# resolution agent drafts a DisputeDraft with amount_at_risk = $2,400.00.
# ---------------------------------------------------------------------------
DUP_INVOICE_NO = "INV-2026-0421"
DUP_INVOICE_DATE = "2026-08-18"
DUP_LINE_ITEMS = [
    ("Managed Data Pipeline Retainer -- monthly", 1, 2400.000),
]
DUP_TAX_RATE = 0.0
DUP_SUBTOTAL = round(sum(q * p for _, q, p in DUP_LINE_ITEMS), 2)
DUP_TAX = round(DUP_SUBTOTAL * DUP_TAX_RATE, 2)
DUP_TOTAL = round(DUP_SUBTOTAL + DUP_TAX, 2)  # exactly 2400.00

# ---------------------------------------------------------------------------
# Mismatch fixtures (P24): three invoices, each cross-checked against a
# deliberately WRONG bank row, covering the three remaining discrepancy types
# that the local fixture set does not yet exercise end-to-end:
#   amount_mismatch   — bank charged the wrong amount (money at risk)
#   vendor_mismatch   — bank row has a vendor-name typo (entity resolution)
#   date_mismatch     — bank posted the charge 35 days after the invoice date
# Each invoice uses the same messy-layout family so extraction adds real value.
# ---------------------------------------------------------------------------

# amount_mismatch: invoice says $1,150.00; the bank charged $1,500.00.
AMT_VENDOR = "Stellar Analytics Ltd"
AMT_INVOICE_NO = "INV-2026-0531"
AMT_INVOICE_DATE = "2026-08-19"
AMT_LINE_ITEMS = [("Data warehouse compute -- 1000 node-hours", 1000, 1.15)]
AMT_TOTAL = round(sum(q * p for _, q, p in AMT_LINE_ITEMS), 2)  # 1150.00
AMT_BANK_AMOUNT = 1500.00  # deliberately WRONG — bank overcharged by $350

# vendor_mismatch: invoice number + amount match; the bank memo misspells the
# vendor (missing the 'u' in "Quantum") -> entity-resolution / alias learning.
VEND_VENDOR = "Quantum Robotics Corp"
VEND_INVOICE_NO = "INV-2026-0642"
VEND_INVOICE_DATE = "2026-08-20"
VEND_LINE_ITEMS = [("Industrial robot maintenance -- quarterly", 1, 3000.00)]
VEND_TOTAL = round(sum(q * p for _, q, p in VEND_LINE_ITEMS), 2)  # 3000.00
VEND_BANK_VENDOR = "QUANTOM ROBOTICS CORP"  # deliberate typo

# date_mismatch: vendor + invoice number + amount all match; only the bank
# posting date is anomalous — the bank charge predates the invoice by 37 days
# (a payment BEFORE the invoice was issued is impossible, so CoVe flags it;
# a charge AFTER the invoice would be treated as normal posting lag).
DATE_VENDOR = "Nebula Data Systems"
DATE_INVOICE_NO = "INV-2026-0753"
DATE_INVOICE_DATE = "2026-08-21"
DATE_LINE_ITEMS = [("Streaming analytics platform -- monthly", 1, 800.00)]
DATE_TOTAL = round(sum(q * p for _, q, p in DATE_LINE_ITEMS), 2)  # 800.00
DATE_BANK_DATE = "2026-07-15"  # 37 days BEFORE invoice date (impossible)


def build_invoice_pdf(out_path: Path) -> None:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXTURE_CREATED_AT)
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


def build_duplicate_invoice_pdf(out_path: Path) -> None:
    """Second invoice from the same vendor -- the one the bank charged twice.

    Same deliberately messy layout family as build_invoice_pdf (scattered
    meta, right-aligned totals) so extraction exercises the same skills.
    """
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXTURE_CREATED_AT)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Header -- vendor top-right (same vendor as the clean invoice)
    pdf.set_xy(120, 15)
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 8, VENDOR)
    pdf.set_xy(120, 30)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, "1600 Amphitheatre Pkwy, Mountain View, CA 94043")

    pdf.set_xy(15, 18)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 12, "INVOICE")

    pdf.set_xy(15, 40)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, f"Invoice #  {DUP_INVOICE_NO}")
    pdf.set_xy(15, 46)
    pdf.cell(0, 6, f"Date      {DUP_INVOICE_DATE}")
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
    for desc, qty, price in DUP_LINE_ITEMS:
        pdf.set_x(15)
        amt = round(qty * price, 2)
        pdf.cell(125, 8, desc, border=1)
        pdf.cell(25, 8, f"{qty}", border=1, align="R")
        pdf.cell(35, 8, f"{amt:.2f}", border=1, align="R")
        pdf.ln()

    # Totals -- right aligned
    pdf.set_xy(125, pdf.get_y() + 4)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(40, 7, f"Subtotal   ${DUP_SUBTOTAL:.2f}")
    pdf.ln()
    pdf.set_x(125)
    pdf.cell(40, 7, f"Tax (0.0%)  ${DUP_TAX:.2f}")
    pdf.ln()
    pdf.set_x(125)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(40, 9, f"TOTAL  ${DUP_TOTAL:.2f}")
    pdf.ln()

    # Benign note -- this fixture is the money moment, not the decoy canary
    pdf.set_xy(15, pdf.get_y() + 10)
    pdf.set_font("Courier", "I", 9)
    pdf.multi_cell(0, 5, "NOTE: second notice -- please remit net-30.")

    pdf.output(str(out_path))


def build_bank_statement_csv_with_duplicates(out_path: Path) -> None:
    """Bank statement containing the ORIGINAL rows plus TWO matching debits
    for the duplicate invoice number (double charge, two days apart)."""
    rows = [
        ["date", "description", "amount", "balance_after"],
        ["2026-08-01", "PAYROLL RUN 08/01", "-8400.00", "41200.11"],
        ["2026-08-12", f"CARD {VENDOR.upper()} {INVOICE_NO}", f"-{TOTAL:.2f}",
         f"{41200.11 - TOTAL:.2f}"],
        ["2026-08-13", "SLACK MONTHLY", "-75.00", f"{41200.11 - TOTAL - 75:.2f}"],
        ["2026-08-14", "AWS *GR0K SERVICES", "-231.45", "0.00"],
        ["2026-08-15", "GITHUB TEAM", "-44.00", "0.00"],
        # The double charge: same vendor, same invoice number, same amount,
        # two days apart. Verification's CoVe must catch TWO matching rows.
        ["2026-08-18", f"CARD {VENDOR.upper()} {DUP_INVOICE_NO}",
         f"-{DUP_TOTAL:.2f}", "0.00"],
        ["2026-08-20", f"CARD {VENDOR.upper()} {DUP_INVOICE_NO}",
         f"-{DUP_TOTAL:.2f}", "0.00"],
    ]
    with open(out_path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def _build_mismatch_invoice_pdf(
    out_path: Path,
    vendor: str,
    invoice_no: str,
    invoice_date: str,
    line_items: list,
    tax_rate: float,
    total: float,
    note_text: str,
) -> None:
    """Generic messy-layout invoice for the P24 mismatch fixtures.

    Shares the deliberately-scattered layout family of build_invoice_pdf
    (vendor top-right, "INVOICE" top-left, compact scattered meta, bordered
    line-item table, right-aligned totals, italic note) so extraction works
    identically. tax_rate/total are computed by the caller for exact totals.
    """
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXTURE_CREATED_AT)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_xy(120, 15)
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 8, vendor)
    pdf.set_xy(120, 30)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, "1600 Amphitheatre Pkwy, Mountain View, CA 94043")

    pdf.set_xy(15, 18)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 12, "INVOICE")

    pdf.set_xy(15, 40)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, f"Invoice #  {invoice_no}")
    pdf.set_xy(15, 46)
    pdf.cell(0, 6, f"Date      {invoice_date}")
    pdf.set_xy(15, 52)
    pdf.cell(0, 6, "Bill To:  Reconciler Demo Inc.")

    pdf.set_xy(15, 68)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(125, 8, "Description", border=1)
    pdf.cell(25, 8, "Qty", border=1, align="R")
    pdf.cell(35, 8, "Amount", border=1, align="R")
    pdf.ln()

    pdf.set_font("Helvetica", "", 10)
    for desc, qty, price in line_items:
        pdf.set_x(15)
        amt = round(qty * price, 2)
        pdf.cell(125, 8, desc, border=1)
        pdf.cell(25, 8, f"{qty}", border=1, align="R")
        pdf.cell(35, 8, f"{amt:.2f}", border=1, align="R")
        pdf.ln()

    pdf.set_xy(125, pdf.get_y() + 4)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(40, 7, f"Subtotal   ${total:.2f}")
    pdf.ln()
    pdf.set_x(125)
    pdf.cell(40, 7, f"Tax ({tax_rate * 100:.1f}%)  ${0.0:.2f}")
    pdf.ln()
    pdf.set_x(125)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(40, 9, f"TOTAL  ${total:.2f}")
    pdf.ln()

    pdf.set_xy(15, pdf.get_y() + 10)
    pdf.set_font("Courier", "I", 9)
    pdf.multi_cell(0, 5, note_text)

    pdf.output(str(out_path))


def build_bank_statement_csv_mismatches(out_path: Path) -> None:
    """Bank statement with three deliberately-wrong rows, one per mismatch
    invoice (amount / vendor typo / anomalous date) + a couple of decoys."""
    rows = [
        ["date", "description", "amount", "balance_after"],
        ["2026-08-01", "PAYROLL RUN 08/01", "-8400.00", "41200.11"],
        # amount_mismatch: same vendor + invoice number, WRONG amount.
        ["2026-08-19", f"CARD {AMT_VENDOR.upper()} {AMT_INVOICE_NO}",
         f"-{AMT_BANK_AMOUNT:.2f}", "0.00"],
        # vendor_mismatch: invoice number + amount match, vendor misspelled.
        ["2026-08-20", f"CARD {VEND_BANK_VENDOR} {VEND_INVOICE_NO}",
         f"-{VEND_TOTAL:.2f}", "0.00"],
        # date_mismatch: vendor + number + amount match, bank charge predates
        # the invoice (impossible — flagged as date_mismatch).
        [DATE_BANK_DATE, f"CARD {DATE_VENDOR.upper()} {DATE_INVOICE_NO}",
         f"-{DATE_TOTAL:.2f}", "0.00"],
        ["2026-08-22", "SLACK MONTHLY", "-75.00", "0.00"],
    ]
    with open(out_path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def main() -> None:
    # Original single-invoice fixtures -- byte-identical outputs, untouched.
    build_invoice_pdf(HERE / "invoice_sample.pdf")
    build_bank_statement_csv(HERE / "bank_statement.csv")
    print(f"fixtures written to {HERE}")
    print(f"  invoice_sample.pdf  vendor={VENDOR!r} invoice={INVOICE_NO} total=${TOTAL:.2f}")
    print(f"  bank_statement.csv   matching charge ${TOTAL:.2f} on {INVOICE_DATE}")

    # P11 duplicate-payment set: clean invoice + double-charged invoice +
    # bank statement carrying both matching debits for the duplicate.
    dup_dir = HERE.parent / "fixtures_duplicate"
    dup_dir.mkdir(exist_ok=True)
    build_invoice_pdf(dup_dir / "invoice_sample.pdf")
    build_duplicate_invoice_pdf(dup_dir / "duplicate_invoice_sample.pdf")
    build_bank_statement_csv_with_duplicates(dup_dir / "bank_statement.csv")
    print(f"fixtures written to {dup_dir}")
    print(f"  invoice_sample.pdf          (copy, deterministic rebuild)")
    print(f"  duplicate_invoice_sample.pdf invoice={DUP_INVOICE_NO} total=${DUP_TOTAL:.2f}")
    print(f"  bank_statement.csv           TWO matching debits ${DUP_TOTAL:.2f}")

    # P24 mismatch set: three invoices, three different deliberate wrong bank
    # rows (amount / vendor / date), one bank statement.
    mismatch_dir = HERE.parent / "fixtures_mismatch"
    mismatch_dir.mkdir(exist_ok=True)
    _build_mismatch_invoice_pdf(
        mismatch_dir / "amount_mismatch_invoice.pdf",
        AMT_VENDOR, AMT_INVOICE_NO, AMT_INVOICE_DATE,
        AMT_LINE_ITEMS, 0.0, AMT_TOTAL,
        "NOTE: please remit net-30.",
    )
    _build_mismatch_invoice_pdf(
        mismatch_dir / "vendor_mismatch_invoice.pdf",
        VEND_VENDOR, VEND_INVOICE_NO, VEND_INVOICE_DATE,
        VEND_LINE_ITEMS, 0.0, VEND_TOTAL,
        "NOTE: please remit net-30.",
    )
    _build_mismatch_invoice_pdf(
        mismatch_dir / "date_mismatch_invoice.pdf",
        DATE_VENDOR, DATE_INVOICE_NO, DATE_INVOICE_DATE,
        DATE_LINE_ITEMS, 0.0, DATE_TOTAL,
        "NOTE: please remit net-30.",
    )
    build_bank_statement_csv_mismatches(mismatch_dir / "bank_statement.csv")
    print(f"fixtures written to {mismatch_dir}")
    print(f"  amount_mismatch_invoice.pdf invoice={AMT_INVOICE_NO} total=${AMT_TOTAL:.2f} "
          f"(bank charged ${AMT_BANK_AMOUNT:.2f})")
    print(f"  vendor_mismatch_invoice.pdf invoice={VEND_INVOICE_NO} total=${VEND_TOTAL:.2f} "
          f"(bank memo {VEND_BANK_VENDOR!r})")
    print(f"  date_mismatch_invoice.pdf   invoice={DATE_INVOICE_NO} total=${DATE_TOTAL:.2f} "
          f"(bank date {DATE_BANK_DATE})")
    print(f"  bank_statement.csv           3 deliberately-wrong rows")


if __name__ == "__main__":
    main()