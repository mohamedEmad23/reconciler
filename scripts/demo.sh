#!/usr/bin/env bash
# ============================================================================
# Reconciler — 4-minute live demo storyboard (DevPost "All Things Agentic")
#
# Prereqs: gcloud logged in as project Owner; this repo; uv installed;
#          service deployed (see README.md Quickstart); GOOGLE_APPLICATION_CREDENTIALS
#          pointing at the runtime SA key for local pipeline runs.
#
# Beats (each prints its own header; follow along + narrate):
#   1. (30s) The problem — messy PDF invoices vs the bank statement.
#   2. (30s) The trigger — Cloud Scheduler fires the weekly job on demand.
#   3. (90s) Live proof — Cloud Run logs + Cloud Trace waterfall.
#   4. (40s) The batch spine — six specialists run end-to-end; Firestore proof.
#   5. (30s) Idempotency — re-trigger the SAME run: 0 LLM calls.
#   6. (30s) The architecture — one diagram.
# ============================================================================
set -euo pipefail

PROJECT="reconciler-mohammed-emad"
REGION="us-central1"
SERVICE_URL="https://reconciler-542923033636.us-central1.run.app"
FIXTURES="$(dirname "$0")/../tests/fixtures"
export GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=$PROJECT GOOGLE_CLOUD_LOCATION=$REGION

banner() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$1"; }
wait_for() { read -r -p "▶ Press Enter to continue ($1)..." _; }

# ---------------------------------------------------------------------------
banner "BEAT 1 — The problem (30s)"
cat "$FIXTURES/bank_statement.csv"
echo
echo "Invoice: a messy scanned PDF ($FIXTURES/invoice_sample.pdf) with a decoy"
echo "NOTE designed to bait hallucinations. A human reconciles this at ~\$12/invoice."
wait_for "open the PDF"

# ---------------------------------------------------------------------------
banner "BEAT 2 — The trigger (30s)"
echo "Cloud Scheduler job 'reconciler-weekly' (cron: 0 8 * * 1) — fire it on demand:"
gcloud scheduler jobs run reconciler-weekly --project "$PROJECT" --location "$REGION"
echo "→ published {\"job_type\":\"weekly_reconcile\"} to Pub/Sub topic reconciler.trigger"
wait_for "or continue to logs"

# ---------------------------------------------------------------------------
banner "BEAT 3 — Live proof in GCP (90s)"
echo "— Pub/Sub push (OIDC, service-account-only invoker) → Cloud Run /trigger/pubsub:"
SINCE=$(date -u -d '-3 minutes' +%Y-%m-%dT%H:%M:%SZ)
gcloud logging read "resource.type=cloud_run_revision \
resource.labels.service_name=reconciler \
timestamp>=\"$SINCE\" \
textPayload=~\"Pub/Sub trigger|Sending out request|POST /apps\" " \
  --project "$PROJECT" --region "$REGION" --limit=15 \
  --format='table(timestamp,textPayload)' 2>/dev/null || \
gcloud logging read "resource.type=cloud_run_revision resource.labels.service_name=reconciler timestamp>=\"$SINCE\"" \
  --project "$PROJECT" --region "$REGION" --limit=15 --format='table(timestamp,textPayload)'

echo
echo "— ADK span waterfall in Cloud Trace (open the console):"
echo "  https://console.cloud.google.com/traces/list?project=$PROJECT"
echo "  Look for: /trigger/pubsub → invocation → invoke_agent supervisor → call_llm → generate_content gemini-2.5-flash"
python3 "$(dirname "$0")/check_traces.py" || true
wait_for "trace console"

# ---------------------------------------------------------------------------
banner "BEAT 4 — The batch spine: six specialists, one spine (40s)"
echo "Now the full pipeline (same agent code the Cloud Run service serves):"
GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/keys/reconciler-sa.json}" \
  uv run python "$(dirname "$0")/run_pipeline.py" demo_live
echo
echo "— Firestore state (runs · run_invoices · shared memory):"
GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/keys/reconciler-sa.json}" \
  uv run python "$(dirname "$0")/show_firestore.py" 3
echo "Console: https://console.cloud.google.com/firestore/databases/-default-/data?project=$PROJECT"
wait_for "firestore console"

# ---------------------------------------------------------------------------
banner "BEAT 5 — Idempotency: re-trigger the SAME run (30s)"
echo "At-least-once delivery means redelivery WILL happen. Re-run the same run_id:"
GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/keys/reconciler-sa.json}" \
  uv run python "$(dirname "$0")/run_pipeline.py" demo_live
echo "→ skipped_idempotent=true: every invoice fence + digest reuse = 0 LLM calls."
echo "Email was NOT sent: Reporting's send_digest_email is behind the HITL Tier-2"
echo "approval gate (request_confirmation) — no human, no send."
wait_for "wrap-up"

# ---------------------------------------------------------------------------
banner "BEAT 6 — The architecture (30s)"
echo "architecture.png (also architecture.excalidraw for the full-res source):"
echo "Scheduler → Pub/Sub → Cloud Run(ADK) → Vertex AI · Firestore · Secret Manager"
echo "Supervisor + 6 single-turn specialists · PII-redaction + HITL middleware"
echo "· checkpoints/idempotency/DLQ · OpenTelemetry → Trace/Logging/Monitoring"
echo
echo "Cost of this demo: a few cents of Gemini tokens. Everything else: free tier."
echo "DONE — ~4 minutes, zero hand-holding."
