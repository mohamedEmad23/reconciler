#!/usr/bin/env bash
# seed_invoices.sh — seed the demo invoice set for a Reconciler run.
#
# Regenerates the deterministic fixtures (byte-idempotent) and shows what a
# run will consume:
#   tests/fixtures/            the clean single-invoice set (flag-and-match demo)
#   tests/fixtures_duplicate/  the P11 money-moment set (clean invoice + a
#                              $2,400 invoice the bank charged TWICE)
#
# For the Gmail intake source, seeding means the demo inbox holds the same
# PDFs as attachments; the local_dir source used here reads them straight
# from disk. Either way the pipeline input is identical.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Regenerating deterministic fixtures"
uv run python tests/fixtures/make_fixtures.py

echo
echo "==> Clean set (tests/fixtures/)"
ls -la tests/fixtures/*.pdf tests/fixtures/bank_statement.csv

echo
echo "==> Money-moment set (tests/fixtures_duplicate/)"
ls -la tests/fixtures_duplicate/*.pdf tests/fixtures_duplicate/bank_statement.csv

echo
echo "Seeded. To run the money moment:"
echo "  GOOGLE_APPLICATION_CREDENTIALS=\$HOME/keys/reconciler-sa.json \\"
echo "  GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \\"
echo "  GOOGLE_CLOUD_LOCATION=global \\"
echo "  uv run python scripts/run_pipeline.py duplicate_demo --directory tests/fixtures_duplicate"
