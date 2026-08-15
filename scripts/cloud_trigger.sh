#!/usr/bin/env bash
# scripts/cloud_trigger.sh — Phase 5 reproducible cloud-trigger proof.
#
# Proves the FULL decoupled pipeline end-to-end:
#   Cloud Scheduler ("reconciler-weekly" job)
#     -> Pub/Sub topic "reconciler.trigger"
#     -> push subscription "reconciler-trigger-push" (OIDC auth via
#        reconciler-trigger-sa, least-privilege run.invoker only)
#     -> Cloud Run ADK server POST /trigger/pubsub
#     -> Supervisor agent (root_agent)
#     -> Vertex AI Gemini 2.5 Flash
#     -> 200 OK
#
# Usage:  bash scripts/cloud_trigger.sh
# No local creds needed — uses gcloud (user Owner account).
#
# This script is ALSO the "schedule trigger" beat of the demo storyboard
# (build-prompt demo script, step 2: 30s).

set -euo pipefail

PROJECT="reconciler-mohammed-emad"
REGION="us-central1"
SERVICE="reconciler"
SCHED_JOB="reconciler-weekly"

echo "=== Phase 5 cloud-trigger proof ==="
echo "Pipeline: Cloud Scheduler -> Pub/Sub -> Cloud Run ADK -> Vertex Gemini"
echo ""

# 1. Trigger the Scheduler job (publishes {"job_type":"weekly_reconcile"} to reconciler.trigger)
echo "[1/3] Triggering Cloud Scheduler job '${SCHED_JOB}'..."
gcloud scheduler jobs run "${SCHED_JOB}" \
  --location="${REGION}" --project="${PROJECT}" --quiet 2>&1 | tail -1

# 2. Wait for Pub/Sub push delivery + cold start + agent run
echo "[2/3] Waiting 30s for push delivery => Cloud Run cold start => agent run..."
sleep 30

# 3. Fetch Cloud Run logs — look for the 3 proof lines
echo "[3/3] Fetching Cloud Run logs (last 5 min)..."
LOGS=$(gcloud logging read \
  "resource.labels.service_name=${SERVICE}" \
  --project="${PROJECT}" --limit=30 --format="value(textPayload)" \
  --freshness=5m 2>&1)

# Check for the three proof markers
PUBSUB_HIT=$(echo "${LOGS}" | grep -c "Pub/Sub trigger:" || true)
GEMINI_HIT=$(echo "${LOGS}" | grep -c "model: gemini-2.5-flash, backend: GoogleLLMVariant.VERTEX_AI" || true)
OK_HIT=$(echo "${LOGS}" | grep -c "trigger/pubsub HTTP/1.1\" 200" || true)

echo ""
echo "Proof markers:"
echo "  Pub/Sub trigger received : ${PUBSUB_HIT} hit(s)"
echo "  Vertex Gemini call       : ${GEMINI_HIT} hit(s)"
echo "  200 OK response          : ${OK_HIT} hit(s)"
echo ""

if [ "${PUBSUB_HIT}" -gt 0 ] && [ "${GEMINI_HIT}" -gt 0 ] && [ "${OK_HIT}" -gt 0 ]; then
  echo "cloud_trigger PASS"
  echo ""
  echo "Full pipeline verified: Scheduler -> Pub/Sub -> Cloud Run -> ADK -> Vertex Gemini -> 200 OK"
  exit 0
else
  echo "cloud_trigger FAIL — one or more markers missing (grep logs manually)"
  exit 1
fi