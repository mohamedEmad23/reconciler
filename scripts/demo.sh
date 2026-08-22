#!/usr/bin/env bash
# ============================================================================
# Reconciler — 4-minute live demo storyboard, rebuilt around the CLOSED LOOP.
#
# The two uncuttable beats (per docs/reconciler-closed-loop-design.md §8):
#   * the $2,400 approval — the "money moment"
#   * the fault-injection recovery — proving "not brittle"
#
# Prereqs: gcloud logged in as project Owner; service deployed (README Quickstart);
#          GOOGLE_APPLICATION_CREDENTIALS → runtime SA key for LOCAL pipeline beats.
#
# Beats:
#   1. (30s) Stakes — 4 hrs/wk, 30 messy PDFs, a $2,400 duplicate buried on page 3.
#   2. (30s) Trigger — Pub/Sub push → Cloud Run pipeline. "No human touched it."
#   3. (60s) The resolution loop — extract → verify → detect duplicate → draft
#            dispute → re-verify, shown live + the /approvals web surface.
#   4. (30s) The money moment — Approve → dollars_recovered ticks to $2,400.
#   5. (30s) Fault injection — kill the bank source mid-run, watch it recover.
#   6. (30s) Learning + idempotency — facts written; re-run = 0 LLM calls.
#   7. (30s) Proof + close — Cloud Trace waterfall + architecture + ROI.
# ============================================================================
set -euo pipefail

PROJECT="reconciler-mohammed-emad"
REGION="us-central1"
SERVICE_URL="https://reconciler-542923033636.us-central1.run.app"
TOPIC="reconciler.trigger"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT="$PROJECT" GOOGLE_CLOUD_LOCATION="$REGION"

banner() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$1"; }
wait_for() { read -r -p "▶ Press Enter to continue ($1)..." _; }
TOKEN() { gcloud auth print-identity-token 2>/dev/null; }

RUN_ID="demo_$(date +%s)"

# ---------------------------------------------------------------------------
banner "BEAT 1 — The stakes (30s)"
echo "Every Monday: 30 messy PDF invoices vs the bank statement. 4 hrs of a"
echo "human's week. One $2,400 duplicate charge buried on page 3 of a $2,400"
echo "invoice that's already been paid. Reconciler is the worker that finds it."
wait_for "open tests/fixtures_duplicate/duplicate_invoice_sample.pdf"

# ---------------------------------------------------------------------------
banner "BEAT 2 — The trigger (30s)"
echo "Publish the weekly job straight to Pub/Sub (same message Cloud Scheduler"
echo "fires on cron '0 8 * * 1'). Push → Cloud Run → the six-stage pipeline."
gcloud pubsub topics publish "$TOPIC" --project "$PROJECT" \
  --attribute="run_id=$RUN_ID,directory=tests/fixtures_duplicate" \
  --message='{"job_type":"weekly_reconcile"}'
echo "→ published to $TOPIC (run_id=$RUN_ID, directory=tests/fixtures_duplicate)"
echo "No human touched it. The container cold-starts, runs, and idles back to 0."
wait_for "pipeline to finish (watch /health if you like)"

# ---------------------------------------------------------------------------
banner "BEAT 3 — The resolution loop (60s)"
echo "— What the pipeline just did (extract → verify → detect → draft → re-verify):"
SINCE=$(date -u -d '-4 minutes' +%Y-%m-%dT%H:%M:%SZ)
gcloud logging read "resource.type=cloud_run_revision resource.labels.service_name=reconciler timestamp>=\"$SINCE\"" \
  --project "$PROJECT" --region "$REGION" --limit=25 --format='table(timestamp,textPayload)' \
  2>/dev/null | grep -iE "trigger|stage|duplicate|dispute|resolv|verif|extract|checkpoint|POST /" || true

echo
echo "— The HITL approval surface (the agent DRAFTED a dispute, it did NOT send):"
curl -s -H "Authorization: Bearer $(TOKEN)" "$SERVICE_URL/approvals?format=json&cb=$RANDOM" \
  | python3 -m json.tool
echo "Open in a browser for the visual cards + provenance:"
echo "  $SERVICE_URL/approvals  (auth: your identity token / demo allow-unauthenticated)"
wait_for "the /approvals page"

# ---------------------------------------------------------------------------
banner "BEAT 4 — The money moment (30s)"
echo "The duplicate_payment discrepancy → lane=dispute → draft for $2,400.00."
echo "The resolution agent holds NO send capability. A human approves:"
APPROVE=$(curl -s -X POST \
  -H "Authorization: Bearer $(TOKEN)" \
  -d "action=approve&reason=duplicate charge confirmed against two bank debits" \
  "$SERVICE_URL/approvals/$RUN_ID/duplicate_invoice_sample/decision?format=json")
echo "$APPROVE" | python3 -m json.tool
echo
echo "→ dollars_recovered just ticked to \$2,400.00 (approved disputes only —"
echo "  a draft or a flag never counts)."
wait_for "the \$2,400 tick"

# ---------------------------------------------------------------------------
banner "BEAT 5 — Fault injection (30s)"
echo "Kill the bank-statement source mid-run, watch the agent recover (local,"
echo "same agent code the cloud service serves):"
bash "$SCRIPT_DIR/fault_inject.sh"
wait_for "recovery logs"

# ---------------------------------------------------------------------------
banner "BEAT 6 — Learning + idempotency (30s)"
echo "— The approved dispute wrote confirmed facts to Shared Epistemic Memory:"
GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/keys/reconciler-sa.json}" \
  uv run python "$SCRIPT_DIR/show_firestore.py" 3
echo
echo "— Idempotency: re-deliver the SAME run_id → skipped, 0 LLM calls:"
gcloud pubsub topics publish "$TOPIC" --project "$PROJECT" \
  --attribute="run_id=$RUN_ID,directory=tests/fixtures_duplicate" \
  --message='{"job_type":"weekly_reconcile"}' >/dev/null
echo "  (at-least-once delivery → the create() fence + digest reuse make it a no-op)"
wait_for "wrap-up"

# ---------------------------------------------------------------------------
banner "BEAT 7 — Proof + close (30s)"
echo "— Cloud Trace waterfall (spans: /trigger/pubsub → pipeline → call_llm → generate_content):"
echo "  https://console.cloud.google.com/traces/list?project=$PROJECT"
python3 "$SCRIPT_DIR/check_traces.py" || true
echo
echo "— Reproducible metrics (uv run scripts/eval.py):"
echo "  extraction 100% · hallucinated entities 0 · discrepancy recall 5/5"
echo "  · false-positives 0 · re-verify pass rate 1/1 · \$2,400 at risk"
echo
echo "— Architecture: architecture.png (editable architecture.excalidraw alongside)."
echo
echo "ROI: a full weekly run costs < \$0.50 of Gemini tokens vs ~\$12/invoice human."
echo "DONE — resolve → verify → audit → recover → learn. Zero hand-holding."
