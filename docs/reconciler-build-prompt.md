# Reconciler — Build Prompt (Autonomous Agent Guide)

You are building **"Reconciler"** — a production-grade autonomous AI agent for the
DevPost hackathon **"All Things Agentic"**. You are the sole engineer. Work in
focused vertical slices and verify each slice works before moving to the next.
Do not leave stubs or TODOs; every agent you write must actually run.

This prompt is your complete guide. If a fuller design document
(`reconciler-taskmaster-design.md`) exists in your project directory, you may use
it for extra detail, but this prompt is self-contained and sufficient on its own.

---

## 1. The Project (what kind of project this is)

"Reconciler" is an **autonomous invoice/expense reconciliation agent**. It is NOT
a chatbot. The entire pitch: a background worker that wakes itself on a schedule,
does a messy multi-step accounting chore with no hand-holding, and only emails a
human when something is wrong.

Concrete behavior it must demonstrate:

1. Wakes on a cron schedule (no human trigger).
2. Pulls messy PDF invoices from a Gmail inbox / Google Drive.
3. Uses Gemini multimodal vision to extract line items, totals, vendor, date.
4. Cross-checks every invoice against a bank-statement CSV.
5. Categorizes each line item against a chart of accounts.
6. Flags discrepancies (missing invoice, amount mismatch, duplicate).
7. Persists everything to a database (Firestore).
8. Emails a weekly digest, escalating only the flagged items to a human.

Narrative in one line: **"not a chatbot — a background worker with an Instruction
Contract and a verification loop."**

---

## 2. Hackathon Context (what you are scored on — optimize for this)

**Category:** Taskmaster ("complete a workflow, not a chatbot")

**Judging criteria (100 total):**

1. **Innovation & Operational Utility — 40%**
   "How much real-world friction does the agent remove on its own?"
   Reward autonomous, high-value ACTION over chat. The agent must make decisions
   and COMPLETE tasks with little to no hand-holding.

2. **Architectural Discipline & Tech Stack — 30%**
   Sound engineering choices: decouple systems, manage state and memory, secure
   credentials, handle failures. "Production-minded, not brittle scripts." This is
   where most teams lose — don't be brittle.

3. **Demo & Production Readiness — 30%**
   Live UNEDITED demo, clean architecture diagram, reproducible setup, VISIBLE
   proof it runs on Google Cloud.

---

## 3. Mandatory Stack (rubric hard requirements — all required)

1. **Gemini 3.5 Flash (or newer) via Vertex AI.**
2. **At least one Google Agent Framework:** use **Google ADK (Python)** — a
   deliberate differentiator over the LangChain default most competitors use.
3. **At least one Google Cloud infra service.** Use these:
   - **Cloud Run** — runtime + your `.run.app` URL
   - **Pub/Sub** — async event bus
   - **Firestore** — state / memory
   - **Cloud Scheduler** — cron trigger
   - **Secret Manager** — credentials
   - **Vertex AI Vector Search** — RAG grounding (optional but impressive)
   - **Cloud Logging + Cloud Trace + Cloud Monitoring** — OpenTelemetry observability

---

## 4. Architecture (the core differentiator — get this shape right)

Async, event-driven, decoupled:

```
  Cloud Scheduler (cron)
        │
        ▼
  Pub/Sub topic "reconciler.trigger"
        │
        ▼
  Cloud Run (ADK runtime: Supervisor + specialists)
        │
        ├──► Firestore (state + Shared Epistemic Memory)
        ├──► Vertex AI Vector Search (RAG)
        ├──► Cloud Logging / Trace / Monitoring (OpenTelemetry)
        └──► Pub/Sub "reconciler.dlq"  (dead-letter queue for poisoned invoices)
```

**Decoupling rules:**

- Trigger ≠ execution ≠ persistence. Each is its own subsystem.
- A failing invoice must never crash the whole run; it goes to the DLQ.
- Every run is idempotent (same input → same result, no double-processing).

---

## 5. Agent Topology (Supervisor + 6 specialists — one file per agent)

| Agent | Responsibility |
|---|---|
| **Supervisor** | Orchestrator. Holds the Instruction Contract. Routes work, decides HITL escalation. |
| **Intake** | Reads Gmail/Drive (MCP-connected). |
| **Extraction** | Gemini multimodal: PDF → structured JSON. `temp=0.0`. |
| **Verification** | CoVe (Chain-of-Verification) + RAG grounding. The Monitor. |
| **Categorization** | Maps line items → chart of accounts. `temp=0.0`. |
| **Reconciliation** | Match invoice vs bank statement; flag discrepancies; enforce invariants (amounts, dates, duplicates). |
| **Reporting** | Weekly digest email; the FINAL HITL gate before sending. |

---

## 6. ADK Primitives to Use (exact API — don't reinvent)

```python
from google.adk.agents import LlmAgent
    LlmAgent(..., sub_agents=[...], mode='single_turn')
    # sub_agents are auto-exposed as tools to the parent

from google.adk.tools.agent_tool import AgentTool
    AgentTool(agent=specialist)
    # coordinator → managed-specialist delegation

from google.adk import Agent, Workflow
    Workflow(edges=[('START', a, b)])
    # graph-based orchestration for the happy path

InMemoryRunner / session_service.create_session / get_session / session.state
    # managed, persisted state across invocations (critical for long-running runs)

from google.adk.tools.mcp_tool import to_mcp_server
    # expose an entire agent AS an MCP server (one tool)
```

Use **ADK middleware (callbacks/interceptors)** for: PII redaction, logging,
rate-limiting, HITL control. Wrap every agent invocation.

---

## 7. Patterns to Implement (deliberate architecture decisions — reference them in the submission)

**FCoT (Faithful Chain-of-Thought):**
- Pillar 1 = **Instruction Contract** — an immutable, non-promptable block of rules
  the agent must obey (never injected as negotiable prompt text).
- Pillar 2 = recursive loop: **RECAP → REASON → VERIFY**
  (RECAP current state, REASON next action, VERIFY against the contract).
- Purpose: prevents goal drift, "lost in the middle", and hallucination.

**CoVe (Chain-of-Verification)** — for the Verification agent:
1. Draft an answer.
2. Plan verification questions.
3. Answer those questions INDEPENDENTLY (NOT conditioned on the draft — this is
   the key step).
4. Revise the draft using only the verified facts.
- Claimed effect (cite in findings): −77% hallucinated entities, +112% precision.

**Shared Epistemic Memory** — Firestore is the single shared memory across agents.

**Persistent Instruction Anchoring** — wrap non-negotiables in
`<CRITICAL_INSTRUCTION> … </CRITICAL_INSTRUCTION>` tags.

**Instruction Fidelity Auditing** — a step that checks the output obeyed the contract.

**Agent Calls Human (HITL)** — two-tier escalation:
- low-confidence → flag & continue
- high-stakes (send email / DB write) → pause & require approval

**RAG grounding** — retrieve prior invoices/vendors from Vector Search to ground
extraction; never let the model invent vendor names or account codes.

**Reliability patterns:**
- **Watchdog Timeout** — every tool call has a deadline.
- **Adaptive Retry** — exponential backoff + jitter (`t_base × 2^n`) on transient errors.
- **Circuit Breaker** — stop hammering a failing dependency.
- **DLQ** — isolate poisoned invoices for later inspection.
- **Idempotency** — dedupe by invoice hash; re-running is safe.
- **Incremental Checkpointing** — persist state after each invoice so a crash
  resumes, not restarts.
- **Tokenomics budget control** — track token spend; hard circuit-breaker at ~90%
  of a per-run budget (cite as production cost control).

---

## 8. Security (this is 30% of your score — do it properly)

- ALL credentials in **Secret Manager**. Nothing in the Docker image, env vars, or repo.
- Least-privilege IAM service account, scoped to exactly the roles it needs.
- OAuth2 machine-to-machine for Gmail/Drive: refresh token stored in Secret
  Manager. Never committed.
- PII redaction middleware on the ADK callback chain.

---

## 9. Build Order (vertical slices — verify each before the next; commit after each)

1. Cloud Run ADK skeleton that responds and returns a `.run.app` URL. Prove deploy.
2. Instruction Contract + Extraction agent: PDF → JSON. `temp=0.0`.
3. Verification + CoVe, cross-checking a bank-statement CSV.
4. Firestore state + Shared Epistemic Memory + idempotency + checkpointing.
5. Pub/Sub + Cloud Scheduler + DLQ wiring.
6. HITL middleware + PII redaction + weekly digest email.
7. OpenTelemetry observability (structured logs, traces, metrics).
8. README + architecture diagram + demo script + findings.

---

## 10. Gemini 3.5 Flash Rules

- The model is ONE config constant — swap it in one line. Only the FINAL submitted
  app + demo video must run Gemini 3.5 Flash (or newer).
- Region availability matters most; `us-central1` serves first. If the default id
  errors ("model not found"), pull the exact current id from Model Garden (it may
  be suffixed, e.g. `gemini-3.5-flash-001`) and use that.
- Fallback: the rubric says "Gemini 3.5 Flash (or newer)" — any Gemini 3.x model
  qualifies, so the project is never blocked.

---

## 11. Submission Deliverables (produce all of these)

- **README.md** — step-by-step spin-up (`gcloud run deploy …`), so a judge can
  reproduce it in under 10 minutes.
- **Architecture diagram** — produce with the Excalidraw MCP tool (`create_diagram`),
  saved as `.excalidraw` AND exported to PNG/SVG.
- **Demo script** (`demo.sh` or a written storyboard) — ~4 minutes:
  problem (30s) → schedule trigger (30s) → live Cloud Run + Vertex AI logs + Cloud
  Trace waterfall (90s) → `.run.app` URL + Firestore rows + Cloud Run dashboard (30s)
  → digest email with the ONE escalated discrepancy (30s) → architecture diagram (30s).
- **findings.md** — the "Findings & Learnings" section (SCORED, often skipped):
  what you learned, the CoVe numbers, the FCoT contract, the ROI math
  (~$0.50-per-task vs $12 human).

---

## 12. Working Rules

- Verify every change (run the code / linter / emulator test) before moving on.
- Fix root causes, never mask them. No temporary hacks.
- Production-minded: config in one place, secrets out of the repo, every async
  handler idempotent, every tool call timed out.
- Report outcomes, not process. When a slice is done, show terminal output proving it.
- Cost target: the whole demo runs on GCP free tiers; only Gemini tokens are billed
  (~$2–5 total). Do not introduce anything requiring a paid tier.

---

Begin with the architecture skeleton (Slice 1), then report back what you
understand the architecture to be BEFORE writing code, so it can be confirmed.
