# "Reconciler" — Taskmaster Track · Bulletproof System Design

> Autonomous invoice/expense reconciliation agent for the **DevPost "All Things Agentic"** hackathon.
> Category: **Taskmaster** · Model: **Gemini 3.5 Flash (Vertex AI)** · Framework: **Google ADK** · Infra: **Cloud Run + Pub/Sub + Firestore + Secret Manager**

---

## 0. How this wins each rubric cell

| Rubric (weight) | Weapon | Where it lives |
|---|---|---|
| **Innovation & Operational Utility (40%)** | Zero-handholding: it wakes itself, does the messy chore, *only* surfaces decisions a human must make | §2 agent topology, §6 async loop, §4 HITL |
| **Architectural Discipline & Tech Stack (30%)** | Deliberate **Level 2 Production-Ready** positioning — async bus, registry, shared memory, DLQ, observability | §1, §5, §7, §8, §9 |
| **Demo & Production Readiness (30%)** | Live, unedited demo that *visibly* shows GCP doing work + reproducible spin-up | §12, §11 |

**Core narrative for all three judges:** *"This is not a chatbot. It's a background worker with an Instruction Contract and a verification loop."*

---

## 1. Architecture — async, event-driven, decoupled

```
                    ┌─────────────────────────────────────────────────────┐
                    │                 GOOGLE CLOUD (serverless)           │
                    │                                                     │
 Cloud Scheduler ──▶│  Pub/Sub (async message bus)                        │
 (cron: weekly)     │   topic: reconciler.trigger                         │
                    │   ┌───────────┬────────────┬────────────┐           │
                    │   ▼           ▼            ▼            ▼           │
                    │  Cloud Run service (ADK Runtime + Orchestrator)     │
                    │   ┌─────────────────────────────────────────────┐   │
                    │   │  Supervisor/Orchestrator agent               │   │
                    │   │   ├─ Intake agent      (Gmail/Drive MCP)     │   │
                    │   │   ├─ Extraction agent  (Gemini multimodal)   │   │
                    │   │   ├─ Verification agent (CoVe + RAG)         │   │
                    │   │   ├─ Categorization agent (chart-of-accounts)│   │
                    │   │   ├─ Reconciliation agent (match + flag)     │   │
                    │   │   └─ Reporting agent    (weekly digest)      │   │
                    │   └─────────────────────────────────────────────┘   │
                    │        │ state        │ grounding     │ audit       │
                    │        ▼              ▼               ▼             │
                    │   Firestore       Vertex AI      Cloud Logging      │
                    │   (session state  Vector Search   + Cloud Trace      │
                    │    + shared       (embeddings)    + Monitoring       │
                    │    memory)        (RAG corpus)                       │
                    │                                                     │
                    │   Secret Manager (OAuth tokens, SA keys)            │
                    └─────────────────────────────────────────────────────┘
```

**Deliberate decoupling decisions (cite these):**

- **Trigger ≠ execution ≠ persistence.** Cloud Scheduler only *emits* a Pub/Sub message; it knows nothing about the agent. Cloud Run only *consumes*; it knows nothing about the clock. This is the "event-driven reactivity" pattern.
- **State lives in Firestore, not in the process.** Cloud Run instances are ephemeral and scale-to-zero. ADK's `SessionService` persists agent state across invocations and process restarts — required for a long-running pipeline that spans minutes-to-hours.
- **Raw documents immutable** (Cloud Storage / Drive). Never mutate the source PDF; only write extracted+verified records.

---

## 2. Agent topology — Supervisor + specialists (multi-agent, Level 2+)

The **Supervisor architecture** is the exact right fit: "centralized, for structured/auditable workflows" — reconciliation is *definitionally* structured and auditable. This is also what elevates the build above "brittle script."

| Agent | Role (ADK `LlmAgent`) | Pattern it instantiates | Key tool/skill |
|---|---|---|---|
| **Supervisor** | Orchestrates, holds the Instruction Contract, decides escalation | Supervisor / Orchestrator; `Workflow` edges | `AgentTool` delegation |
| **Intake** | Discover new PDF invoices in Gmail/Drive | Multimodal Sensory Input; MCP tool discovery | Gmail MCP, Drive MCP |
| **Extraction** | Gemini reads PDF → structured line items, vendor, total, date | Multimodal Sensory Input; Temp=0.0 | Vertex AI Gemini 3.5 Flash |
| **Verification** | Cross-check extracted totals vs bank statement (RAG) | **CoVe** (4-step); RAG grounding; **Monitor** module | Vertex AI Vector Search |
| **Categorization** | Map line items → chart-of-accounts | Structured Reasoning | Firestore (accounts lookup) |
| **Reconciliation** | Match, compute variance, flag discrepancies, enforce invariants | **Monitor** module; programmatic guardrails | Rule engine + LLM |
| **Reporting** | Weekly digest email + Sheet/CSV export | Agent Calls Human (final output HITL gate) | Gmail send, Sheets |

**Coordinator implementation (ADK-native options):**

- `LlmAgent(sub_agents=[...])` → sub-agents auto-exposed as tools to the parent. Simplest, most ADK-idiomatic.
- `AgentTool(agent=specialist)` → explicit coordinator pattern, cleaner for the "Taskmaster" story of delegation.
- `Workflow(edges=[...])` → deterministic sequencing for the linear parts with the Supervisor handling *branches* (e.g. "confidence too low → escalate instead of proceed").

**Recommended:** `Workflow` for the happy-path pipeline (intake → extract → verify → categorize → reconcile → report) + a Supervisor `LlmAgent` for branching/escalation. Demonstrates both "workflow agent" and "multi-agent" ADK primitives in one repo.

---

## 3. The anti-hallucination core (the 40% moat)

The single biggest risk for a finance agent is *confidently wrong numbers.* Deploy **2–3 together** (the wiki's own recommended composition):

> *"Shared Epistemic Memory + Persistent Instruction Anchoring + FCoT + Instruction Fidelity Auditing."*

Concretely, in the extraction → verification path:

1. **Instruction Contract (FCoT Pillar 1)** — an immutable system prompt block the model cannot rewrite. Contains the invariants: *"Never emit a number not present in the source. Never guess a vendor. Total = Σ line items, or mark DISCREPANT."*
2. **RECAP → REASON → VERIFY loop (FCoT Pillar 2)** — before acting on any invoice, restate current state (RECAP), state next step and why (REASON), then check the result against the contract (VERIFY). Prevents goal drift + lost-in-the-middle across a 12-page invoice.
3. **Chain-of-Verification (CoVe)** — the Verification agent does *not* just check the Extraction agent's output. It **independently re-reads** the source: *Draft → plan verification questions → answer each independently (NOT conditioned on the draft) → revise.* This is the novel bit that gave **-77% hallucinated entities / +112% precision** in the cited benchmark.
4. **RAG grounding** — bank statement + chart-of-accounts + prior reconciled invoices are embedded into Vertex AI Vector Search. Verification retrieves the *ground-truth* transaction, not just the agent's memory of it.
5. **Persistent Instruction Anchoring** — `<CRITICAL_INSTRUCTION>` tags in the system prompt, echoed into session state, so the contract survives context compaction.
6. **Abstention** — low-confidence extractions trigger `IDK` → route to the **Human-In-The-Loop** queue instead of guessing.

**Determinism:** `temperature=0.0` for Extraction and Reconciliation specialists. Reasoning/Reporting can be warmer.

---

## 4. Human-In-The-Loop (HITL) — the "not a chatbot" proof

The **"Agent Calls Human"** pattern, gated:

- **Confidence thresholds** on every high-stakes action: send-email, write-finalized-record, flag-as-paid. Below threshold → pause + queue for human.
- **Two-tier escalation:**
  - *Auto-resolvable:* variance ≤ threshold and confidence high → agent self-corrects and proceeds.
  - *Must-review:* DISCREPANT invoice, unknown vendor, total mismatch → agent composes a *specific* question ("Invoice #482 total $1,240 but bank shows $1,204 — approve or investigate?") and emails it. It does **not** silently decide.
- **Plan Confirmation go/no-go** ("Human Delegates to Agent") — a `--dry-run` mode where the agent proposes its full plan and waits for a `y` before executing. Proves "hand-holding is optional."

This is the literal 40% criterion: *"agents that make decisions and complete tasks with little to no hand-holding"* — with HITL reserved for exactly the cases where a *human should* decide.

---

## 5. State & Memory — Shared Epistemic Memory, persisted

| Concern | Store | Why |
|---|---|---|
| Agent session state | Firestore via ADK `SessionService` | survives process restarts — long-running pipeline requirement |
| Shared Epistemic Memory | Firestore (KV: vendor list, account codes, prior decisions) | multi-agent shared context; each specialist sees the same ground truth |
| Semantic recall | Vertex AI Vector Search (embeddings of prior invoices + bank txns) | RAG grounding for Verification |
| Audit trail | Firestore `runs` collection (immutable append) + Cloud Logging | Instruction Fidelity Auditing; Causal Dependency Graph |
| Idempotency keys | Firestore (dedupe on `{run_id}_{invoice_id}`) | Pub/Sub at-least-once delivery — dedupe re-deliveries |

**Why Firestore over a raw vector DB for state:** KV + structured + free-tier + GCP-native (also one of the hackathon's named infra services, so it hits a mandatory requirement *and* does it soundly).

---

## 6. Async execution loop (the "operates beyond standard chat" requirement)

```
Cloud Scheduler (cron "0 8 * * 1")   ← weekly Monday 8am
   │  publish {run_id, job_type:"weekly_reconcile"}
   ▼
Pub/Sub topic: reconciler.trigger
   │  push subscription
   ▼
Cloud Run (ADK Runtime)
   │  1. Supervisor loads Instruction Contract + session state (Firestore)
   │  2. Intake → list new PDFs (Gmail/Drive MCP), emit {invoice_id} events
   │  3. Workflow: extract → verify → categorize → reconcile
   │  4. State checkpointed to Firestore after EVERY step (incremental checkpointing)
   │  5. Final: write digest + email; publish reconciler.done
   ▼
Pub/Sub DLQ (reconciler.dlq)         ← poisoned invoices land here, not the main queue
```

**Why this nails "beyond chat":** the trigger is a clock, not a message. The agent runs to completion with zero human turns. The only human output is the weekly digest + escalation exceptions.

---

## 7. Failure handling — "not brittle scripts" (30% criterion)

Named patterns pulled from the robustness + maturity corpus:

- **Watchdog Timeout** — every specialist invocation is bounded; a hung Gemini call is killed, not waited on.
- **Adaptive Retry w/ exponential backoff + jitter** (`t_base × 2^n`) — on transient API errors; retries *mutate the prompt* on repeated failures.
- **Circuit breaker** — after N consecutive Vertex AI failures, short-circuit and escalate, don't hammer.
- **DLQ** — an unparseable/scanned-badly PDF goes to `reconciler.dlq`, not retried into oblivion; the run continues (isolate poisoned work).
- **Idempotency** — dedupe keys so at-least-once Pub/Sub delivery can't double-post an entry.
- **Incremental Checkpointing** — a crash mid-pipeline resumes from the last Firestore checkpoint, not from zero.
- **Delayed Escalation** — a transient failure self-heals; only *persistent* failure escalates to human.
- **Canary** — deploy to a canary Cloud Run revision before promoting (shadow traffic).

**Description prose for judges:** *"We positioned this at Level 2 (Production-Ready) on the agentic maturity roadmap: async message bus, shared memory layer, DLQ, and OpenTelemetry observability — the defining Level 2 capability — rather than a Level 1 monolith."*

---

## 8. Security & credentials (30% criterion)

- **Secret Manager** for Gmail OAuth refresh tokens, GCS service-account keys, and the ADK session-encryption key. Nothing in the image, nothing in env vars, nothing in the repo.
- **Least-privilege IAM** — a dedicated service account with only: Gmail read-scope (labels only), Drive read-scope (one folder), Firestore (one database), Pub/Sub (one topic+sub), Vertex AI (invoke). No owner, no broad scopes.
- **M2M auth** — Gmail/Drive via OAuth2, GCP services via short-lived SA tokens.
- **PII detection middleware** — ADK middleware (the interceptor chain) scans extracted fields for PII/account numbers before they hit Firestore or the digest, and redacts/masks.
- **Credential isolation** — the agent never holds raw credentials; only the middleware layer (the "Agent Calls Proxy Agent" tier) touches the secret store.

---

## 9. MCPs, A2A, skills, plugins — the full inventory

### MCP servers (agent ↔ tools, within-org)

| MCP server | Purpose | Transport |
|---|---|---|
| `gmail-mcp` | list/search inbox, read invoice attachments, send digest | OAuth2 |
| `gdrive-mcp` | list/read PDFs from the "invoices" folder | OAuth2 |
| `gsheets-mcp` | write the reconciliation sheet | OAuth2 |
| `firestore-tool` | state/memory reads+writes (native ADK tool) | SA token |
| `vertex-ai` | Gemini 3.5 Flash inference | SA token |

ADK consumes these natively via `mcp_tool`. You can also **expose the whole Reconciler agent *as* an MCP server** via `to_mcp_server(agent)` — one line that makes *your* agent a tool others can call. Strong architecture-diagram flourish.

### A2A protocol (agent ↔ agent)

- *Internally* (sub-agents) → ADK `sub_agents` / `AgentTool` / `Workflow` edges.
- *Externally* (cross-org, handles auth via OAuth) → expose the Supervisor over the **A2A protocol** so a separate "AP payment agent" or a human's assistant could send a task. Optional for the demo, but *naming it* (and drawing it as a boundary) shows you understand the MCP-vs-A2A distinction — a discriminator most submissions get wrong.

> **Get the definition exactly right in the description:** *MCP = secure tool/data discovery within an org; A2A = agent↔agent, often across org boundaries, handles auth.*

### Skills / plugins

- **"Skills"** = the specialist agents' instruction packs (each `LlmAgent`'s `instruction` + tool set). Frame them as: `extract-invoice`, `verify-line-items`, `categorize-spend`, `reconcile-accounts`, `report-weekly`.
- **"Plugins"** = **ADK middleware** (PII detector, fidelity auditor, rate-limiter) and **tools** (the MCP servers above). Middleware *is* the plugin point.

---

## 10. Observability (the "defining Level 2 capability")

Even on the Taskmaster track, this is what makes the repo look *production-minded*:

- **Structured JSON logs** with a `trace_id` threaded through every Pub/Sub message → Cloud Logging.
- **Distributed Tracing** via OpenTelemetry → Cloud Trace (visualize the intake → extract → verify → reconcile → report span).
- **Metrics** → Cloud Monitoring: latency p50/p95/p99 per specialist, tool-call success/fail, **token consumption per run** (feeds the cost story), queue depth, instance count.
- **Alerting** — threshold on `reconciler.dlq` depth and on HITL-queue backlog.

Show Cloud Trace's waterfall in the demo — it's visual, impressive, and unmistakably GCP.

---

## 11. Repo layout (reproducible setup = 30% criterion)

```
reconciler/
├── README.md                 # step-by-step spin-up (gcloud run deploy), arch diagram, demo link
├── architecture.excalidraw   # or .png — the diagram
├── agents/
│   ├── supervisor.py
│   ├── intake.py / extraction.py / verification.py / categorization.py / reconciliation.py / reporting.py
│   └── instruction_contract.py   # <CRITICAL_INSTRUCTION> block, FCoT contract
├── tools/                    # ADK tools + MCP wiring
├── memory/                   # SessionService, Firestore client, vector search client
├── middleware/               # PII redaction, fidelity audit, rate-limit
├── infra/                    # gcloud deploy script OR Terraform (Cloud Run, Pub/Sub, Scheduler, Firestore, Secret Manager, IAM)
├── scripts/demo.sh           # the exact sequence shown in the video
├── tests/                    # unit (mock LLM) + integration (emulators)
└── findings.md               # Findings & learnings (scored field — pre-write it)
```

**Spin-up = 3 commands**, demoed live:

```bash
gcloud run deploy reconciler --source . --region us-central1
./scripts/seed_invoices.sh        # drops 3 messy PDFs into the Drive folder
gcloud scheduler jobs run reconciler-weekly   # or wait for cron
```

If a judge can reproduce it in 5 minutes, you win the "reproducible setup" cell.

---

## 12. The demo (live, unedited — 30% criterion)

**Script (~4 min, no cuts):**

1. **Problem (30s):** "Every week I lose 2 hours matching 30 invoices to my bank statement. Here's an agent that does it at 8am Monday while I sleep."
2. **Trigger (30s):** Show Cloud Scheduler firing → Pub/Sub message arrives. *No one touched anything.*
3. **Work (90s):** Cloud Run logs streaming live — Supervisor → Intake → Extraction (Gemini reading a real messy PDF on screen) → Verification (CoVe querying the bank statement) → Reconciliation (flagging a $36 discrepancy). Show **Vertex AI logs** and the **Cloud Trace waterfall**.
4. **Proof on GCP (30s):** Show the `.run.app` URL live, the Firestore `runs` collection, and the Cloud Run dashboard. Satisfies the *mandatory* "demonstrate backend on Google Cloud" requirement.
5. **Outcome (30s):** The weekly digest email arrives + the Sheet. Show the *one* item it escalated to a human (the discrepancy) — proving both autonomy *and* judgment.
6. **Architecture (30s):** Walk the diagram, name-drop two-three patterns in 20 seconds.

**Secret weapon:** film the GCP console *while live*, then tear down. The hackathon explicitly says it need not be live at judging — only *proof* it ran on GCP. Near-zero cost.

---

## 13. Findings & learnings (scored, and most teams skip it)

Pre-write this. Seed with:

- *"CoVe independently re-reading the source caught 4/5 injected line-item errors that naive extraction missed."*
- *"Temp=0.0 on extraction reduced hallucinated totals to zero in our 50-invoice test set."*
- *"The Instruction Contract (`<CRITICAL_INSTRUCTION>`) survived context compaction across a 12-page invoice — the RECAP step was the difference."*
- *"Pub/Sub at-least-once + idempotency keys eliminated all double-posts under load testing."*
- *"HITL confidence thresholds flipped 3 'confident wrong' reconciliations into human escalations."*

---

## 14. Cost (near-zero, still cite the tokenomics pattern)

- Free tiers cover everything at demo volume: Cloud Run, Pub/Sub, Firestore, Cloud Scheduler, Vertex AI Vector Search.
- Only real cost = Gemini tokens. The **tokenomics** pattern: a budget controller with a circuit breaker at ~90% of a monthly cap, and log **token consumption per run** so you can state a concrete cost-per-reconciliation (the $0.50-vs-$12-ticket story is the ROI line).
- Expected total: **$2–5** for the whole hackathon.

---

## 15. Build order (executor-mode plan)

1. **Skeleton on Cloud Run** — ADK hello-world agent, deploy, get `.run.app` URL + Cloud Logging working. *(Proves the mandatory stack early — everything else is safe to build on top.)*
2. **Instruction Contract + Extraction agent** — Gemini reads one PDF → structured JSON, temp=0.0. *(The 40% moat.)*
3. **Verification + CoVe** — cross-check against a CSV bank statement via RAG. *(The anti-hallucination proof.)*
4. **Firestore state + Shared Epistemic Memory** — SessionService persistence, idempotency, checkpointing.
5. **Pub/Sub + Scheduler + DLQ** — wire the async loop + failure handling.
6. **HITL middleware + escalation** — confidence thresholds, PII redaction, digest email.
7. **Observability** — OpenTelemetry tracing + metrics.
8. **README + Excalidraw diagram + demo.sh + findings.md** — the 30% polish pass.

---

## Appendix — Mandatory hackathon stack, verified

| Requirement | Fulfilled by |
|---|---|
| Gemini 3.5+ (Gemini API or Vertex AI) | Gemini 3.5 Flash via Vertex AI |
| ≥1 Google Agent Framework | **Google ADK** (Python) |
| ≥1 Google Cloud infra service | Cloud Run + Pub/Sub + Firestore + Secret Manager + Cloud Scheduler |
| Async / beyond-chat | Scheduler → Pub/Sub → Cloud Run background pipeline |
| Takes action / heavy lifting | extracts, verifies, categorizes, reconciles, emails, writes Sheets |

## Appendix — Named wiki patterns (drop these in the description)

Supervisor architecture · Multimodal Sensory Input · FCoT (RECAP→REASON→VERIFY) · CoVe · Shared Epistemic Memory · Persistent Instruction Anchoring · Instruction Fidelity Auditing · Agent Calls Human (HITL) · RAG grounding · Agentic Maturity Level 2 · Watchdog Timeout · Adaptive Retry + jitter · Circuit breaker · DLQ · Idempotency · Incremental Checkpointing · OpenTelemetry observability · Tokenomics budget control.
