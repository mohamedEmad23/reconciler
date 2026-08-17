#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Fault-injection demo beat (design doc §4): kill the bank-statement source
# mid-run and watch the agent recover — retry with backoff, circuit breaker,
# fail-isolation to the DLQ, run COMPLETES with errors; then restore the
# source and a fresh run completes clean. Film this segment uncut.
#
# Usage:  ./scripts/fault_inject.sh
# Cost:   ~$0.04 of Vertex tokens (one broken run + one clean run).
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

BANK=tests/fixtures/bank_statement.csv
ENV_VARS=(
  GOOGLE_APPLICATION_CREDENTIALS="$HOME/keys/reconciler-sa.json"
  GOOGLE_GENAI_USE_VERTEXAI=1
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad
  GOOGLE_CLOUD_LOCATION=us-central1
)

restore() {
  chmod 644 "$BANK" 2>/dev/null || true
  [ -f "$BANK.faultbak" ] && mv "$BANK.faultbak" "$BANK" 2>/dev/null || true
}
trap restore EXIT

echo "== B1: BREAK the bank-statement source (unreadable) =="
mv "$BANK" "$BANK.faultbak"

echo "== B2: trigger the pipeline — it must NOT hang =="
echo "    (watch for: retrying attempt n/4 backoff ... -> RetryBudgetExhausted"
echo "               -> invoice failed + published to reconciler.dlq"
echo "               -> run completes with errors, digest still composed)"
env "${ENV_VARS[@]}" timeout 300 uv run python scripts/run_pipeline.py fault_inject_demo

echo
echo "== B3: RESTORE the source =="
mv "$BANK.faultbak" "$BANK"

echo "== B4: fresh run — completes clean =="
env "${ENV_VARS[@]}" timeout 300 uv run python scripts/run_pipeline.py fault_inject_demo_clean

echo
echo "fault_inject beat OK — resilience demonstrated: retry -> breaker ->"
echo "DLQ isolation -> run survived; clean recovery on the next run."
