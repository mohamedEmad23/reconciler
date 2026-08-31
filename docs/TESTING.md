# Reconciler — Step-by-Step Testing Guide

Everything to verify, component by component — old and new. Run from the repo root.

---

## Part 0 — Prerequisites (once per machine)

```bash
# 1. Log in as the project Owner (for gcloud + identity tokens)
gcloud auth login                        # mohammed.emad4884@gmail.com

# 2. Runtime service-account key for LOCAL smoke tests (never committed)
ls ~/keys/reconciler-sa.json             # must exist

# 3. Environment for every local command (smokes / eval / run_pipeline)
export GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json
export GOOGLE_GENAI_USE_VERTEXAI=1
export GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad
export GOOGLE_CLOUD_LOCATION=global      # ← Gemini 3.5 is served on the GLOBAL endpoint

# 4. Identity token (for the live service)
TOKEN=$(gcloud auth print-identity-token)

# 5. Live service
SERVICE_URL="https://reconciler-542923033636.us-central1.run.app"
```

> **Why `GOOGLE_CLOUD_LOCATION=global`?** Gemini 3.5 (`gemini-3.5-flash`) 404s on regional Vertex.
> It is only served on the global Agent-Platform endpoint. Infra (Firestore/PubSub/SecretManager)
> still uses `us-central1` via the separate `RECONCILER_GCP_REGION` env — the two are independent.

---

## Part 1 — The smoke suite (17 tests)

Each `smoke_*.py` is self-contained. Run them all; each prints `PASS`. Free ones
make **zero** Vertex calls; paid ones cost $0.01–$0.06 each.

```bash
# The whole suite (one-liner). Stop on first failure:
for s in smoke smoke_extraction smoke_verification smoke_firestore smoke_safety \
         smoke_categorization smoke_resilience smoke_resolution smoke_pipeline \
         smoke_duplicate smoke_approvals smoke_learning smoke_provenance \
         smoke_dashboard smoke_chat smoke_digest_email smoke_mismatch; do
  echo "=== $s ==="; uv run python scripts/$s.py || { echo "FAILED: $s"; break; }
done
```

| # | Smoke | What it proves | Cost |
|---|-------|----------------|------|
| 1 | `smoke.py` | Supervisor boots, routes to Vertex, returns the ack/plan JSON | ~$0.01 |
| 2 | `smoke_extraction.py` | PDF→JSON at `temp=0.0`; ground-truth vendor/number/date/total; **$1M decoy never extracted**; byte-determinism | ~$0.02 |
| 3 | `smoke_verification.py` | CoVe cross-check vs bank CSV; happy-path `matched` + mismatch-path `matched=false amount_mismatch`; determinism | ~$0.03 |
| 4 | `smoke_firestore.py` | Idempotency (dup start→None), **atomic create() fence under concurrency**, crash→resume at next stage, SharedMemory deep-merge + miss→None | ~$0.01 |
| 5 | `smoke_safety.py` | PII redaction **before** model sees it; HITL Tier-1 (low-conf flag) + Tier-2 (send pauses for approval) | free |
| 6 | `smoke_categorization.py` | Chart-of-accounts coding (5000/5010/6000), substance-over-keyword, vendor-mapping echo | ~$0.02 |
| 7 | `smoke_resilience.py` | Retry+backoff, circuit breaker (open/half-open/close), watchdog, DLQ publish | free |
| 8 | `smoke_resolution.py` | resolve/dispute/escalate decision table; **resolved only after independent re-verify**; drafts are inert (no send key) | ~$0.02 |
| 9 | `smoke_pipeline.py` | Full 7-specialist batch on fixtures; Firestore checkpoints; idempotent re-run (0 LLM) | ~$0.03 |
| 10 | `smoke_duplicate.py` | **The $2,400 money moment** — duplicate_payment → dispute → draft $2,400 → dollars_at_risk | ~$0.06 |
| 11 | `smoke_approvals.py` | Approve→resolved+email-sent+dollars; double-approve→409; reject→escalated (no send) | free |
| 12 | `smoke_learning.py` | Approve→positive facts, reject→negative facts, deep-merge extends lists | free |
| 13 | `smoke_provenance.py` | Audit chain: rule_fired, CoVe Q&A, recheck verdict, trace_id; malformed-entry safety | free |
| 14 | `smoke_dashboard.py` | Dashboard `/` markup: scoreboard, stages, HITL cards, runs, learned facts, chat widget | free |
| 15 | `smoke_chat.py` | Read-only query tools (list_runs/invoices/facts/disputes) + chat agent wiring | free |
| 16 | `smoke_digest_email.py` | Benign run-summary email composition + send (stubbed SMTP) | free |
| 17 | `smoke_mismatch.py` | amount/vendor/date mismatch invoices — CoVe catches all 3 types | ~$0.06 |

---

## Part 2 — The eval harness (reproducible metrics)

```bash
uv run scripts/eval.py        # ~$0.06, writes docs/eval-results.md
```

Expected numbers (already baked into `findings.md` §9):
- Extraction field accuracy **100%** (clean + duplicate)
- Hallucinated entities **0** ($1M decoy never extracted)
- Injected discrepancy recall **5/5** (amount / vendor / date / invoice-number / duplicate_payment)
- Verification false-positives **0**
- Resolution re-verify pass rate **1/1**
- Dollars at risk **$2,400.00**

---

## Part 3 — The live service (Cloud Run, pure FastAPI)

**Auth note:** the service is `--no-allow-unauthenticated`, so a bare browser returns 403 *by design*
(only the trigger service-account may invoke it). To open it for judges during the demo:

```bash
gcloud run services update reconciler --region us-central1 \
  --project reconciler-mohammed-emad --allow-unauthenticated
# ... demo ...
gcloud run services update reconciler --region us-central1 \
  --project reconciler-mohammed-emad --no-allow-unauthenticated
```

Or use the identity token everywhere:

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/health"          # {"status":"ok"}
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/?cb=$RANDOM"      # dashboard HTML
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/approvals?cb=$RANDOM"
```

Verify on the dashboard `/` (open in browser with the token or after allow-unauthenticated):
- Scoreboard: **dollars recovered · all time**, **awaiting your approval**, **invoices processed**, **reconciliation runs**
- **Seven stages** pipeline + **Where it runs** (7 GCP services) + **Recent runs** + **Learned facts**
- **Ask the agent** chat widget (bottom) — type "which invoices were flagged this week?"

**Chat endpoint (P23):**
```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"What invoices were processed recently, and were any flagged?"}' \
  "$SERVICE_URL/chat"
```

---

## Part 4 — The autonomous cron (background worker)

```bash
gcloud scheduler jobs run reconciler-weekly --project reconciler-mohammed-emad --location us-central1
```

This is the **real autonomous path**: cron `0 8 * * 1` → Pub/Sub `reconciler.trigger` → Cloud Run
`/trigger/pubsub` → `source=gmail` → pulls invoices **from Gmail** → cross-checks vs the bank CSV →
the Nimbus `no_bank_match` invoice gets **escalated** (the agent abstains instead of hallucinating a match).

Watch it in real time:
```bash
gcloud logging read "resource.type=cloud_run_revision resource.labels.service_name=reconciler" \
  --project reconciler-mohammed-emad --limit=30 --format='value(textPayload)' \
  | grep -iE "trigger|stage|verif|resolv|dispute|escalat|POST /" 
```

Every run also **sends a benign digest email** (P22): "Reconciled N invoices — M matched, K flagged…".
Check your inbox (redirected to `mohammed.emad4884@gmail.com`).

---

## Part 5 — The money moment ($2,400 duplicate → approve)

This is the two-uncuttable-beats demo. Trigger the duplicate-payment fixture set:

```bash
RUN_ID="money_$(date +%s)"
gcloud pubsub topics publish reconciler.trigger --project reconciler-mohammed-emad \
  --attribute="run_id=$RUN_ID,directory=tests/fixtures_duplicate" \
  --message='{"job_type":"weekly_reconcile"}'
```

Wait ~60s, then confirm the dispute appears:
```bash
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/approvals?cb=$RANDOM"   # shows the $2,400 card
```

Approve it (the human decision → email sent + dollars recovered):
```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -d "action=approve&reason=duplicate charge confirmed against two bank debits" \
  "$SERVICE_URL/approvals/$RUN_ID/duplicate_invoice_sample/decision?format=json"
# → {"status":"approved","outcome":"resolved","amount":2400.0}
```

Re-approve to prove idempotency:
```bash
curl -s -X POST ... "$SERVICE_URL/approvals/$RUN_ID/duplicate_invoice_sample/decision?format=json"
# → {"status":"already_decided"}  (HTTP 409)
```

Refresh the dashboard — **dollars recovered** ticks +$2,400.00.

---

## Part 6 — Fault injection (not brittle)

```bash
bash scripts/fault_inject.sh
```
This renames the bank CSV mid-run → watch retry → circuit-breaker → DLQ → run reports
`completed_with_errors` (never crashes) → restore → clean run completes. Purely local, same agent code.

---

## Part 7 — The full 4-minute demo

```bash
bash scripts/demo.sh
```
Seven beats, interactive (press Enter between): stakes → **autonomous cron (Gmail) + $2,400
duplicate trigger** → resolution loop → **money moment (approve)** → fault injection → learning +
idempotency → proof (Cloud Trace + eval numbers + architecture + ROI).

---

## Quick reference — key invariants

- **Gemini 3.5 Flash** via Vertex AI, **global** endpoint — single constant `GEMINI_MODEL` in `agents/reconciler/config.py`.
- **All creds in Secret Manager** (`reconciler-oauth-config` Gmail OAuth, `reconciler-smtp-config` Gmail app password) — nothing in the image/env/repo.
- **Least-privilege IAM:** trigger SA `reconciler-trigger-sa` (run.invoker ONLY), runtime SA `reconciler-sa` (8 scoped roles).
- **Idempotent + crash-resumable:** atomic `create()` fence + per-stage Firestore checkpoints + forward-only resume.
- **Anti-hallucination:** `temp=0.0` + native `output_schema` + all-Optional leaves (null-not-guess) + CoVe + RAG memory.
- **Two-tier HITL:** low-confidence → flag & continue; money-touching send → pause & require approval.
