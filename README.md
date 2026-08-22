# Reconciler — an autonomous invoice reconciliation worker

**Not a chatbot.** Reconciler is a background worker that wakes on a schedule,
pulls messy PDF invoices, extracts them with Gemini multimodal, cross-checks
them against a bank statement with a Chain-of-Verification, categorizes them
against a chart of accounts, **resolves what it can and escalates only what it
must**, persists everything to Firestore with a full provenance trail, and
composes a weekly digest. High-risk corrections are *drafted* — never sent —
until a human clicks **Approve** on the web surface, which is the only component
that can commit an external side effect and tally **dollars recovered**.

Built for the DevPost **"All Things Agentic"** hackathon (Taskmaster track) on
a deliberately non-default stack:

| Layer | Choice |
|---|---|
| Agent framework | **Google ADK 2.7** (Python) — not LangChain |
| Model | **Gemini 2.5 Flash** via **Vertex AI** (one config constant, temp=0.0) |
| Runtime | **Cloud Run** (pure FastAPI surface, service-account-only invoker) |
| Trigger | **Cloud Scheduler** → **Pub/Sub** push (OIDC) → `/trigger/pubsub` |
| State/memory | **Firestore** — `runs`, `run_invoices`, `memory` collections |
| Secrets | **Secret Manager** (Gmail/Drive OAuth) — nothing in the image |
| Observability | **OpenTelemetry** → Cloud Trace / Logging / Monitoring |
| Failure handling | Dead-letter topic + idempotency fences + per-stage checkpoints |

![Architecture](architecture.png)

## What makes it agentic (and not a script)

- **Instruction Contract (FCoT)** — every specialist is wrapped in an
  immutable `<CRITICAL_INSTRUCTION>` block (never fabricate; missing → `null`,
  never guess; refuse jailbreaks) plus a RECAP → REASON → VERIFY loop that
  runs in reasoning before any output is emitted.
- **CoVe verification** — the Verification agent plans 3–5 *checkable*
  questions ("does any bank row equal the invoice total ±$0.02?") and answers
  each **independently of its draft**, inspecting the raw CSV. Injecting a
  mutated total (999.99) flips the verdict to `matched=false` with a typed
  `amount_mismatch` discrepancy — the anti-rubber-stamp proof.
- **Native structured output** — Pydantic `output_schema` enforced at the
  Vertex AI API level (`response_schema`), so even a jailbreak cannot emit
  off-schema prose. Temperature 0.0 everywhere.
- **Safety-rail middleware on every agent** (ADK callbacks): PII redaction
  *before the model sees the request*, HITL Tier-1 (low confidence → flag &
  continue), and HITL Tier-2 (email send → framework-level pause requiring
  human approval).
- **Shared Epistemic Memory** — verified vendor facts (account codes, invoice
  numbers seen) are written to Firestore *after* verification and read back as
  grounding hints. A memory miss returns `null`, which is the anti-hallucination
  signal — the model never invents a vendor or account code.
- **Reliability spine** — atomic `create()` idempotency fences (at-least-once
  Pub/Sub redelivery can't double-process), forward-only stage checkpoints
  (crash → resume at the next stage, not from zero), fail-isolation per
  invoice, dead-letter topic for poison messages, plus **adaptive retry with
  exponential backoff + jitter, per-dependency circuit breakers, and a watchdog
  timeout on every tool call** (`resilience.py`) — kill the bank source mid-run
  and the run still completes.
- **Closed-loop resolution** — every discrepancy routes through a resolve /
  dispute / escalate decision engine (`resolution.py`) driven by *Python-computed
  evidence* (fuzzy vendor match, day deltas, exact/digit-transposed amounts), not
  just the model's say-so. A "resolve" only counts if an **independent
  re-verification pass confirms the discrepancy is gone** — never self-certified.
- **Real dollars** — a confirmed duplicate charge is drafted as a dispute with an
  `amount_at_risk`; `dollars_recovered` increments **only on human approval** of
  that dispute or a re-verified correction. The seeded demo finds **$2,400.00**.
- **Decision provenance** — every resolved/disputed/escalated action carries a
  chain of extraction hash, CoVe questions/answers, memory keys consulted, the
  rule that fired (with its real score), the recheck verdict, and the human
  decision — all rendered on the approval page and linked to the Cloud Trace
  waterfall.
- **Closed-loop learning** — human approvals write *confirmed* facts (vendor
  aliases, account codes, prior invoices); rejections write *negative* facts.
  Facts are hints that shorten resolution, never a bypass: re-verification still
  runs every time.

## Topology

Supervisor + 7 single-turn specialists, one file each under `agents/reconciler/`:
`intake`, `extraction`, `verification`, `resolution`, `categorization`,
`reconciliation`, `reporting`. The Supervisor delegates (its instruction forbids
doing the work itself); the batch spine (`pipeline.py`) drives the same
specialists for the scheduled run with checkpoints between every stage. A pure
FastAPI surface (`server.py`) exposes `/trigger/pubsub` (batch pipeline) and the
`/approvals` HITL web page; `resilience.py`, `learning.py`, `provenance.py` and
`approvals.py` wrap the runtime.

## Quickstart (reproducible in <10 minutes)

Prereqs: a GCP project with billing (free tiers cover everything except
Gemini tokens — expect **a few cents** for this demo), `gcloud` logged in as
Owner, Docker running (Cloud Build builds the image).

```bash
# 0) env
export PROJECT=your-project-id REGION=us-central1
gcloud config set project "$PROJECT"

# 1) enable APIs + create Firestore (skip any that exist)
gcloud services enable run.googleapis.com pubsub.googleapis.com \
  scheduler.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudtrace.googleapis.com monitoring.googleapis.com logging.googleapis.com
gcloud firestore databases create --location=nam5 2>/dev/null || true

# 2) runtime + trigger service accounts (least privilege)
gcloud iam service-accounts create reconciler-sa 2>/dev/null || true
gcloud iam service-accounts create reconciler-trigger-sa 2>/dev/null || true
for ROLE in roles/aiplatform.user roles/datastore.user roles/logging.logWriter \
            roles/pubsub.publisher roles/secretmanager.secretAccessor \
            roles/cloudtrace.agent roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:reconciler-sa@$PROJECT.iam.gserviceaccount.com" \
    --role "$ROLE" --condition=None -q
done

# 3) topics + push subscription + scheduler + DLQ
gcloud pubsub topics create reconciler.trigger
gcloud pubsub topics create reconciler.dlq
gcloud run deploy reconciler --source . --region "$REGION" \   # deploy first to get URL
  --service-account "reconciler-sa@$PROJECT.iam.gserviceaccount.com" \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=1,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION" \
  --no-allow-unauthenticated --min-instances=0 --max-instances=1 \
  --memory=512Mi --concurrency=4 --timeout=300 --port=8080 --quiet
URL="https://$(gcloud run services describe reconciler --region "$REGION" --format 'value(status.url)')"
gcloud pubsub subscriptions create reconciler-trigger-push \
  --topic=reconciler.trigger --push-endpoint="$URL/trigger/pubsub" \
  --push-auth-service-account="reconciler-trigger-sa@$PROJECT.iam.gserviceaccount.com" \
  --ack-deadline=60 --dead-letter-topic=reconciler.dlq --max-delivery-attempts=5
gcloud run services add-iam-policy-binding reconciler --region "$REGION" \
  --member "serviceAccount:reconciler-trigger-sa@$PROJECT.iam.gserviceaccount.com" \
  --role roles/run.invoker
gcloud pubsub topics add-iam-policy-binding reconciler.dlq \
  --member "serviceAccount:service-$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role roles/pubsub.publisher
gcloud scheduler jobs create pubsub reconciler-weekly --location "$REGION" \
  --schedule="0 8 * * 1" --time-zone=UTC --topic=reconciler.trigger \
  --message-body='{"job_type":"weekly_reconcile"}'
```

`403 Forbidden` when you open the URL in a browser is **by design**
(`--no-allow-unauthenticated` — only the trigger SA may invoke it):

```bash
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "https://reconciler-<hash>.$REGION.run.app/health"   # → {"status":"ok"}
```

### Run the demo

```bash
./scripts/demo.sh          # 4-minute guided storyboard (see beats inside)
```

Or manually:

```bash
# fire the weekly job now + watch the logs
gcloud scheduler jobs run reconciler-weekly --location us-central1
gcloud logging read 'resource.type=cloud_run_revision resource.labels.service_name=reconciler' --limit=10

# full seven-specialist batch spine (writes real Firestore state)
GOOGLE_APPLICATION_CREDENTIALS=~/keys/reconciler-sa.json \
GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=$PROJECT GOOGLE_CLOUD_LOCATION=$REGION \
uv run python scripts/run_pipeline.py demo_live

# re-run the SAME id → skipped, 0 LLM calls (idempotency proof)
uv run python scripts/run_pipeline.py demo_live

# approve a pending dispute (the only component with send authority)
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "https://reconciler-<hash>.$REGION.run.app/approvals/demo_live/duplicate_invoice_sample/decision?format=json" \
  -d "action=approve&reason=verified duplicate charge"

# see the state
uv run python scripts/show_firestore.py
```

Gmail/Drive intake (`agents/reconciler/tools/intake_tools.py`) reads its OAuth
refresh token from Secret Manager secret `reconciler-oauth-config` and never
writes credentials to disk. Without it, the pipeline runs on the bundled
fixture invoice (`tests/fixtures/invoice_sample.pdf` vs `bank_statement.csv`).

## Repo layout

```
agents/reconciler/
  config.py               # single source of truth (model constant, topic names…)
  instruction_contract.py # FCoT Pillar 1 (immutable rules) + Pillar 2 (RECAP→REASON→VERIFY)
  schemas.py              # Pydantic output schemas + chart of accounts + invariants
  agent.py                # Supervisor + all seven specialists wired
  intake.py tools/intake_tools.py
  extraction.py verification.py resolution.py categorization.py reconciliation.py reporting.py
  middleware.py           # PII redaction + HITL Tier-1/Tier-2 (ADK callbacks)
  memory.py               # RunsStore (checkpoints, idempotency) + SharedMemory
  pipeline.py             # batch spine: intake→…→reporting with checkpoints
  resilience.py           # retry/backoff, circuit breaker, watchdog, DLQ publish
  learning.py             # approve→positive facts, reject→negative facts
  provenance.py           # audit-trail read path + judge-facing rendering
  approvals.py            # transactional approve/reject + dollars_recovered
  server.py               # pure FastAPI surface (/trigger/pubsub, /approvals)
scripts/                  # demo.sh, eval.py, run_pipeline.py, show_firestore.py, smokes, seed/fault scripts
tests/fixtures/           # deterministic invoice PDF + bank CSV (with decoys + $2,400 duplicate)
tests/fixtures_duplicate/ # duplicate-payment money-moment fixture set
architecture.excalidraw   # editable diagram source (PNG export alongside)
findings.md               # what we learned, with numbers
docs/eval-results.md      # reproducible eval artifact (generated by scripts/eval.py)
```

## Verification

Every phase shipped with a smoke that asserts the claim it makes
(`uv run python scripts/smoke_*.py`; the ones calling Vertex cost ~$0.01–0.03):

| Smoke | Proves |
|---|---|
| `smoke_safety.py` | PII redaction + HITL tiers as pure functions (free) |
| `smoke_extraction.py` | ground-truth extraction, decoy rejection, determinism |
| `smoke_verification.py` | happy match + injected-mismatch CoVe catch |
| `smoke_categorization.py` | chart-of-accounts codes, substance-over-keyword |
| `smoke_firestore.py` | atomic idempotency fence, crash-resume, deep-merge memory |
| `smoke_pipeline.py` | full chain end-to-end + idempotent re-run |
| `smoke_resolution.py` | decision table, re-verification closure, draft inertness, abstention |
| `smoke_resilience.py` | retry/backoff, circuit breaker, watchdog, DLQ (free) |
| `smoke_duplicate.py` | duplicate-payment → dispute → $2,400 draft, dollars anti-gaming |
| `smoke_approvals.py` | approve→resolved+send+$ recovered; reject→escalated; 409 double-decide |
| `smoke_learning.py` | approve→positive facts, reject→negative facts, deep-merge |
| `smoke_provenance.py` | audit-trail read path + judge rendering (free) |

## Eval (reproducible numbers)

```bash
GOOGLE_APPLICATION_CREDENTIALS=~/keys/reconciler-sa.json \
GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=$PROJECT GOOGLE_CLOUD_LOCATION=$REGION \
uv run python scripts/eval.py
```

Runs the real agents over labeled fixtures and writes `docs/eval-results.md`:
extraction field accuracy **100%**, hallucinated entities **0**, injected
discrepancy recall **5/5** (amount / vendor / date / invoice-number /
duplicate-payment), false-positives **0**, resolution re-verify pass rate
**1/1**, dollars at risk **$2,400.00**. Full table in `findings.md` §9.

## Cost

At demo volume everything sits in GCP free tiers (Cloud Run, Pub/Sub,
Scheduler, Firestore, Trace); the only billed component is Gemini tokens —
the whole 4-minute demo costs **a few cents**, a full weekly run with a
handful of invoices well under $0.50, versus ~$12 human cost per invoice.
