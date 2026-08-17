# Reconciler — Closed-Loop Resolution Design

**Target:** `/home/mohammed-emad/VS-CODE/Hackathons/DevPost/reconciler`
**Status:** This document converts Reconciler from a *flag-and-escalate* pipeline into a *resolve-then-escalate* autonomous agent. It is the implementation spec for the 7 "killer features" that separate a winner from a finalist.

**Read order:** §0 (how this maps to your code) → §1 (the core closed loop) → §2–§8 (the remaining killer features) → §9 (composition + anti-gaming guards) → §10 (build-order delta) → §11 (file-change manifest).

---

## §0 — Codebase State (what exists, what this doc adds)

Your repo already has a working Supervisor + 3 specialists. Here is the ground truth this document builds on:

| File | What it does today |
|---|---|
| `agents/reconciler/agent.py` | `root_agent = Agent(name="supervisor", temp=0.0, sub_agents=[extraction, verification, reporting], ...)` — routes only, no safety callbacks |
| `agents/reconciler/config.py` | Single-source constants + frozen `RuntimeConfig` dataclass |
| `agents/reconciler/instruction_contract.py` | `INSTRUCTION_CONTRACT` (v0.2, 8 immutable rules), `SPECIALIST_PREAMBLE`, `specialist_instruction(goal, inputs, output_description)` |
| `agents/reconciler/extraction.py` | `extraction_agent` — PDF → `ExtractionResult`, temp=0.0, `output_schema` native enforcement |
| `agents/reconciler/verification.py` | `verification_agent` — CoVe vs bank CSV → `VerificationResult` |
| `agents/reconciler/reporting.py` | `reporting_agent` — `send_digest_email` tool, HITL Tier-2 gate |
| `agents/reconciler/middleware.py` | PII redaction + HITL Tier-1 flag (`CONFIDENCE_THRESHOLD=0.7`) + `with_safety_rails()` |
| `agents/reconciler/memory.py` | `RunsStore` (idempotency/checkpointing) + `SharedMemory` (deep-merge fact store) |
| `agents/reconciler/schemas.py` | Pydantic models, `DISCREPANCY_TYPES` (7 types) |

**The gap this document closes:** today, a discrepancy flows `verification → reporting` and is *only flagged*. There is no `resolution` step — no autonomous fix, no re-verification, no learning. That is the single highest-value thing you can add, because it is precisely the "autonomous, high-value action with little hand-holding" the 40% Innovation criterion rewards.

---

## §1 — The Closed-Loop Resolution Flow (Killer Feature #1)

This is the heart. It replaces *"flag everything"* with *"resolve what is resolvable, escalate only what is genuinely ambiguous."*

### 1.1 The principle

> **A capable agent resolves strategic ambiguity; a safe agent escalates tactical ambiguity.**

Three kinds of discrepancy exist:
1. **Resolvable** — the agent has the evidence and the low-risk action to fix it (e.g. OCR misread a digit; the vendor alias is in memory).
2. **Disputable** — the agent knows *something* is wrong and can *draft* the corrective action, but executing it is high-stakes (money moves, external email). → **draft + human approve**.
3. **Genuinely ambiguous** — the agent cannot resolve it with available evidence. → **escalate**.

The resolution flow routes every discrepancy into exactly one of these three lanes.

### 1.2 The resolve-vs-escalate decision engine

The decision is a **pure function** of four inputs (this is what makes it auditable — see §3):

```
decision = f(discrepancy_type, confidence, evidence_available, action_risk)
```

- `discrepancy_type` ∈ `DISCREPANCY_TYPES` (already defined in `schemas.py`)
- `confidence` = the verification agent's confidence (0–1), already flowing through `middleware.py`
- `evidence_available` = boolean, whether a resolution action can produce corroborating evidence (a higher-fidelity re-read, a memory lookup, a vendor portal fetch)
- `action_risk` = boolean, whether the resolution action has an external side effect (sends email, moves money, mutates shared data)

Decision table:

| confidence | evidence | risk | Lane | Action |
|---|---|---|---|---|
| ≥ 0.90 | — | low | **resolve** | apply correction + log provenance |
| ≥ 0.90 | — | high | **dispute** | draft corrective action → HITL approve |
| 0.70–0.90 | yes | low | **resolve (conditional)** | gather evidence → if re-verify confirms, apply |
| 0.70–0.90 | yes | high | **dispute** | gather evidence → draft action → HITL approve |
| 0.70–0.90 | no | — | **escalate** | flag with explicit "missing evidence" reason |
| < 0.70 | — | — | **escalate** | already flagged by Tier-1 HITL middleware |

The `< 0.70` row is already handled by your `make_after_model_callback_hitl` — do not duplicate it. The resolution engine only *adds* behavior for `confidence ≥ 0.70`.

### 1.3 Discrepancy taxonomy → resolution mapping

For each of the 7 `DISCREPANCY_TYPES`, define the canonical resolution action. This table is the spec for the `ResolutionAgent`'s instruction contract:

| Type | Resolvable? | Resolution action | Risk | Re-verify step |
|---|---|---|---|---|
| `amount_mismatch` | often | Re-read PDF at higher fidelity; if bank value is a transposition/fuzzy match of extracted value, correct amount | low (no side effect) | re-run Verification on corrected JSON |
| `vendor_mismatch` | often | Entity-resolution against `vendor` namespace in `SharedMemory` (aliases); if alias matches, canonicalize vendor | low | re-run match on canonical vendor |
| `date_mismatch` | often | Distinguish invoice date vs due date vs bank posting date; resolve if within a tolerance window (bank posts 1–3 days after invoice) | low | re-check date window |
| `invoice_number_mismatch` | sometimes | Fuzzy string match (OCR digit transposition: `0`↔`O`, `1`↔`l`); try alternate reading | low | re-run match on corrected number |
| `duplicate_payment` | **high value** | Confirm via `prior_invoice` memory + bank statement (same invoice number appears twice); if confirmed, **draft a dispute/correction email** | **high** | human approve → send → mark disputed |
| `no_bank_match` | sometimes | Fetch missing statement from vendor portal / Drive (MCP); if found, reconcile; if not, escalate as "unmatched" | low→high | re-run Verification on fetched statement |
| `extra_invoice_line` | often | Re-read line region; if clearly a stray/mis-bounded line, drop it with rationale | low | re-run line-item count |

`duplicate_payment` is the *money moment* (see §2): it is the discrepancy type that produces "dollars recovered," so it gets the full dispute + HITL treatment.

### 1.4 Confidence thresholds (single source of truth)

Reuse the existing constant. Add two more next to it in `middleware.py`:

```python
CONFIDENCE_THRESHOLD = 0.7   # already exists — below this = escalate (Tier-1)
RESOLVE_THRESHOLD   = 0.90  # NEW — at/above this = auto-resolve low-risk
DISPUTE_THRESHOLD   = 0.70  # NEW — at/above this (and < RESOLVE) = draft+approve
```

Keep all three in `middleware.py` (or move to `config.py` next to the other frozen constants) so there is exactly one place a judge can see your thresholds. A judge who finds `RESOLVE_THRESHOLD` with a comment explaining *why* 0.90 is the auto-resolve bar is a judge who marks "architectural discipline."

### 1.5 Resolution state machine (per discrepancy)

Extend the invoice checkpoint (already in `RunsStore`) with a resolution sub-state. A discrepancy transitions:

```
detected ──► analyzing ──► resolved          (auto or post-dispute)
                    │
                    ├──► disputed ──► approved ──► resolved
                    │                   └──► rejected ──► escalated
                    │
                    └──► escalated        (human review, no resolution)
```

`analyzing` is transient (the resolution agent is mid-decision). Persist only the terminal states (`resolved`, `disputed`, `approved`, `rejected`, `escalated`) plus the rationale — this is your audit trail (§3).

### 1.6 Re-verification: the loop that *closes*

The word "closed-loop" means: **after a resolution action, re-run verification, and only mark `resolved` if the discrepancy is actually gone.** Never self-certify.

```
for each discrepancy:
    lane = decide(...)
    if lane == resolve:
        action_output = resolution_agent.apply(...)
        recheck = verification_agent.verify(corrected_data)   # re-open the loop
        if recheck.matched and discrepancy_absent(recheck):
            mark resolved, record provenance
        else:
            mark escalated (resolution failed — do not force it)
    elif lane == dispute:
        draft = resolution_agent.draft(...)     # no side effect yet
        approval = hitl_surface.await(draft)    # §5
        if approval.approved:
            send(draft); recheck; mark resolved-or-escalated
        else:
            record human rejection → learn (§7); mark escalated
    else:
        mark escalated
```

This is the single most important correctness property in the whole design: **an action is only ever "resolved" if an independent verification pass confirms the discrepancy is gone.** It directly prevents the agent from gaming its own metric (see §9).

### 1.7 Wiring into the existing code

1. **New file** `agents/reconciler/resolution.py` — the `ResolutionAgent`.
2. **New tools** registered on it (see §1.8).
3. **Extend `STAGE_ORDER`** in `memory.py` from
   `("intake","extraction","verification","categorization","reconciliation","reporting")`
   to insert `"resolution"` after `"verification"` (or fold resolution into `"reconciliation"` — pick one; the doc assumes a distinct `"resolution"` stage for a cleaner trace, but folding it into `reconciliation` is acceptable if you want fewer moving parts).
4. **Register** `resolution_wrapped` in `agent.py` `sub_agents` list (wrapped via `with_safety_rails()`, same as the others).
5. **Add schemas** to `schemas.py` (§1.8).

### 1.8 Schemas + pseudocode

New schemas in `schemas.py`:

```python
class ResolutionDecision(BaseModel):
    discrepancy_type: Literal[tuple(DISCREPANCY_TYPES)] = None
    lane: Literal["resolve", "dispute", "escalate"] = None
    confidence: float = None
    evidence_refs: list[str] = None      # memory keys / source hashes consulted
    rationale: str = None                # REQUIRED — the "why" (provenance §3)

class ResolutionAction(BaseModel):
    decision: ResolutionDecision = None
    corrected_invoice: Invoice | None = None   # only for lane=resolve
    dispute_draft: DisputeDraft | None = None  # only for lane=dispute
    recheck_matched: bool | None = None        # from the re-verification pass
    outcome: Literal["resolved", "disputed", "escalated"] = None

class DisputeDraft(BaseModel):
    recipient: str = None
    subject: str = None
    body: str = None
    amount_at_risk: float = None          # feeds the $ recovered metric (§2)
```

Resolution agent declaration (mirrors your existing specialist pattern):

```python
resolution_agent = Agent(
    name="resolution",
    model=config.GEMINI_MODEL,
    temp=0.0,
    tools=[re_read_invoice, lookup_prior, fetch_vendor_statement,
           apply_correction, draft_dispute],
    output_schema=ResolutionAction,
    mode="single_turn",
    output_key="resolution_last_reply",
)
resolution_wrapped = with_safety_rails(resolution_agent)
```

Tool contracts (pseudocode — signatures only, implementation in `tools.py` or inline):

```python
def re_read_invoice(source_ref: str, region: str | None) -> dict:
    """Re-extract a specific region of the source PDF at higher fidelity.
    Returns {field, old_value, new_value, confidence}. No side effect."""

def lookup_prior(namespace: str, key: str) -> dict | None:
    """Read SharedMemory.get_fact(namespace, key). No side effect."""

def fetch_vendor_statement(vendor: str) -> dict | None:
    """Fetch a missing statement from vendor portal/Drive via MCP.
    Side effect: external read. Timeout + retry required (§5)."""

def apply_correction(invoice_id: str, field: str, old: float|str, new: float|str,
                     rationale: str, evidence_refs: list[str]) -> dict:
    """Write the corrected field back with provenance. LOW-RISK, no external effect.
    Must be gated: only callable when lane == resolve."""

def draft_dispute(recipient: str, subject: str, body: str,
                  amount_at_risk: float) -> DisputeDraft:
    """Compose a correction email WITHOUT sending. HIGH-RISK.
    Register under make_before_tool_callback_hitl() so send is gated."""
```

Crucial safety property: `draft_dispute` **never sends** — it returns a `DisputeDraft`. Sending happens only after the human approves via the HITL surface (§5). This is the "Agent Calls Proxy Agent" pattern: the resolution agent drafts, the approval surface is the only component that can commit an external side effect. The resolution agent never holds a send capability.

---

## §2 — Real Dollar Number (Killer Feature #2)

**Purpose:** give judges a concrete, repeatable number — "Reconciler recovered $2,400."

**Mechanism:** seed a fixture where `duplicate_payment` fires on a large amount. The `DisputeDraft.amount_at_risk` field (from §1.8) is aggregated per run into a `dollars_recovered` counter on the run document.

**Schema (add to `memory.py` run summary):**

```
run doc:
  dollars_recovered: float          # sum of approved dispute amounts + corrected overcharges
  discrepancies_resolved: int       # count of lane=resolve + approved disputes
  discrepancies_escalated: int      # count of lane=escalate
```

`RunsStore.increment_counts` already exists — extend it with `dollars_recovered` (use `Firestore.Increment`, same as the others).

**The seed fixture:** add a second PDF to `tests/fixtures/` — a legitimate-looking invoice from a vendor already paid this month, so the bank statement shows *two* matching debits (a duplicate charge). The agent must:
1. Detect `duplicate_payment` (Verification sees two bank rows for one invoice number).
2. Draft a dispute for the duplicate amount (~$2,400).
3. After human approve, `dollars_recovered += 2400.00`.

**Demo beat:** in the demo, after the approval click, show the `dollars_recovered` scoreboard ticking from $0 → $2,400. This is the number judges repeat to each other.

**Anti-gaming guard:** `dollars_recovered` is only incremented on **approved** disputes and **re-verified** corrections (§1.6) — never on a self-certified flag. See §9 for the full guard rail.

---

## §3 — Decision Provenance / Audit Trail (Killer Feature #3)

**Purpose:** answer "why did Reconciler do this?" for every action. This is what turns a black-box agent into a production system a judge can trust.

**Mechanism:** a `provenance` subdocument appended to every resolved/disputed/escalated discrepancy, chaining: extraction evidence → verification evidence → resolution decision → re-verification result.

**Schema (store in Firestore under the invoice doc, not in the model output):**

```python
class ProvenanceEntry(BaseModel):
    discrepancy_type: str = None
    lane: str = None                      # resolve/dispute/escalate
    extraction_hash: str = None           # source_hash from ExtractionResult
    verification_questions: list[str] = None   # CoVe questions
    verification_answers: list[str] = None     # CoVe answers (the independent pass)
    memory_keys_consulted: list[str] = None    # vendor/prior_invoice lookups
    rule_fired: str = None                # e.g. "fuzzy_match(vendor, alias) @ 0.94"
    resolution_rationale: str = None      # from ResolutionDecision.rationale
    recheck_matched: bool | None = None   # the closing-the-loop evidence
    human_decision: Literal["approved","rejected",None] = None   # if disputed
    timestamp: str = None
```

**Wiring:** `RunsStore.checkpoint(stage, data)` already persists per-stage data. Store `provenance` entries in the `"resolution"` stage's `stages_data`. Then add a read path so the HITL surface (§5) and the digest email can render "why."

**OpenTelemetry tie-in:** each `ProvenanceEntry` gets the current `trace_id`. This links the human-readable "why" to the Cloud Trace waterfall — a judge can click a decision and see the exact span. This is the "structured logs + distributed tracing" combination that reads as production-grade.

---

## §4 — Live Fault-Injection Resilience (Killer Feature #4)

**Purpose:** the single most memorable 30 seconds of the demo — kill the bank-statement source mid-run and watch the agent recover, then finish. Proves "not brittle."

**What to build (all in a new `agents/reconciler/resilience.py`):**

1. **Adaptive retry with exponential backoff + jitter** — decorate every external call (`fetch_vendor_statement`, the bank-statement read, Gmail API):

```python
def retry_with_backoff(max_attempts=5, base_s=1.0, jitter=0.3):
    # t = base_s * 2**n  (± jitter); on transient errors only
```

2. **Circuit breaker** — per-dependency (bank statement API, vendor portal, Gmail). After N consecutive failures, open the circuit and short-circuit subsequent calls for a cool-down window, so one dead dependency can't stall the whole run:

```python
class CircuitBreaker:
    # states: closed -> open (after n_failures) -> half_open (after cooldown)
```

3. **Watchdog timeout** — every tool call runs inside a deadline; on timeout, cancel + fallback (per dependency).

4. **DLQ publish** — when an invoice *cannot* be resolved after retries, publish to `reconciler.dlq` (topic already configured in `config.py`), and `RunsStore.mark_invoice_failed(dlq=True)`.

**Demo beat:** a `fault_inject.sh` script (or a `?kill=bank` flag on the trigger) that:
1. Starts the run.
2. After the first invoice, revokes/denies the bank-statement source for ~10 seconds.
3. The agent logs `retrying (attempt 2/5, backoff 2.0s)` → `circuit OPEN for bank-api` → continues on cached/other data → circuit closes → run **completes** with a recovered log line.

Film this segment **live, uncut**. The recovery is the feature — a hiccup that self-heals reads as strength, not weakness.

---

## §5 — HITL Approval Surface (Killer Feature #5)

**Purpose:** replace the CLI/`request_confirmation` primitive with a minimal web approval page, so the "human approves the escalated item" beat is visual and credible.

**Mechanism:** the ADK `request_confirmation` primitive pauses the workflow, but it has no face. Add a thin web route on the same Cloud Run service that:
1. Lists pending `disputed` drafts (read from Firestore).
2. Renders each with the full provenance (§3) — the "why" the human reviews.
3. Offers **Approve** / **Reject** (reject requires a one-line reason).
4. On Approve: executes the `send` (the only component with send authority), then triggers the re-verification pass (§1.6).
5. On Reject: records the human decision + reason → feeds the learning loop (§7).

**Implementation note:** keep this to a single small endpoint (FastAPI or the ADK server's existing HTTP surface). It must run in the same Cloud Run revision so there's one `.run.app` URL to show. The digest email (`reporting.py`) already demonstrates the send path; the approval surface reuses that same Gmail send, but only fires it after an explicit human click.

**Security:** the approval surface is the only component that can commit an external side effect. Route its auth through the same OAuth2 M2M credential in Secret Manager. This is the "proxy agent" tier from your architecture — credentials live in one place, and the resolution agent never touches them.

---

## §6 — Closed-Loop Learning (Killer Feature #6)

**Purpose:** the "agent that gets better" story — run 1 = 8/10 invoices correct, run 2 = 10/10 after learning vendor patterns.

**Mechanism (two write paths into `SharedMemory`, which already exists with deep-merge):**

1. **Autonomous learning (during a run):** every `resolve` decision that survives re-verification (§1.6) writes a fact to `SharedMemory`. Examples:
   - `vendor` namespace: `{"Acme Cloud Services LLC": {"aliases": ["ACME CLOUD", "ACME CLOUD SERVICES"]}}`
   - `prior_invoice` namespace: `{"INV-2026-0417": {"total": 467.50, "vendor": "Acme Cloud Services LLC"}}`
   - `account_code` namespace: `{"Acme Cloud Services LLC": "5500-Software"}`
   Only write facts that **passed re-verification** — never write a speculative guess (this is the "learn only from confirmed data" rule that prevents the vicious-cycle degradation).

2. **Human-in-the-loop learning (from §5):** on Approve, write the confirmed rule; on Reject, write a *negative* fact (e.g. `{"INV-2026-0417": {"NOT_vendor_alias": "..."}}`) so the agent doesn't repeat the mistake.

**Demo beat:** run the pipeline twice against the same fixture set. Run 1 flags/resolves 8/10. Between runs, the learned facts persist in Firestore. Run 2 uses the `vendor` aliases to resolve the last 2 automatically → 10/10. Show the delta on screen.

**Anti-gaming guard (critical):** the flywheel must balance *exploit* (use learned facts) with *explore* (still re-verify). Learned facts **shorten** the resolution path but never **skip** re-verification. A fact is a *hint*, not a *bypass*. This is what keeps "10/10" honest.

---

## §7 — Eval Harness with Real Numbers (Killer Feature #7)

**Purpose:** claim a number you actually ran, not a vibe. Judges trust a metric that is reproducible from the repo.

**Mechanism:** a `scripts/eval.py` that runs the full pipeline over the labeled `tests/fixtures/` set and prints:

- **Extraction accuracy** — % of invoices where every field (vendor, number, date, total, line items) matches ground truth.
- **Hallucinated-entity rate** — fields the model invented that aren't in the source (the decoy `$1,000,000` line in your existing fixture is the canary).
- **Verification precision/recall** — of the 7 discrepancy types, how many injected discrepancies were caught vs. false-positives.
- **Resolution success rate** — of auto-resolved discrepancies, how many survived re-verification.
- **Dollars recovered** — sum over the fixture set.

**Numbers to target (and write into `findings.md`):**

| Metric | Target |
|---|---|
| Extraction field accuracy | 100% on the clean fixture, >95% on the messy set |
| Hallucinated entities | 0 (temp=0.0 + native `output_schema` + contract) |
| Injected discrepancy recall | 5/5 caught (CoVe catches what naive verify misses) |
| Resolution re-verify pass rate | 100% (a resolve that fails re-check is marked escalated, not counted as success) |

**Wiring:** reuse `test_vertex.py` / `smoke_verification.py` patterns (they already assert determinism and anti-rubber-stamp behavior). `eval.py` is the script you point to in the README with the command `uv run scripts/eval.py`.

---

## §8 — The 4-Minute Demo, Re-Built Around Resolution

The old demo was "flag + digest." The new demo must center the *resolution* moment:

| Time | Beat | What the judge sees |
|---|---|---|
| 0:00–0:30 | Stakes | "4 hrs/wk, 30 messy PDFs, a $2,400 duplicate buried on page 3." |
| 0:30–1:00 | Trigger | Scheduler → Pub/Sub → Cloud Run spins up. "No human touched it." |
| 1:00–2:00 | **The resolution loop** | Live logs: extract → verify → **detect duplicate → draft dispute → re-verify**. Show the `.run.app` approval page. |
| 2:00–2:30 | **The money moment** | Click Approve → `dollars_recovered` ticks to $2,400 → correction email sent. |
| 2:30–3:00 | **Fault injection** | Kill bank-statement source → retry/backoff logs → circuit opens → run still completes. |
| 3:00–3:30 | **Learning** | Run 2 vs run 1: 8/10 → 10/10, learned vendor aliases on screen. |
| 3:30–4:00 | Proof + close | Cloud Trace waterfall → `.run.app` URL → architecture diagram → ROI ("$0.50/task vs $12 human"). |

The two dramatic beats are **the $2,400 approval** and **the fault-injection recovery**. If you must cut anything, cut breadth — never these two.

---

## §9 — Composition Notes + Anti-Gaming Guards

**How the 7 features compose (the "one system" argument):**

- **#1 (closed loop)** is the backbone. **#2 ($ recovered)** is its output metric. **#3 (provenance)** is the audit of #1. **#4 (fault injection)** is the resilience under #1. **#5 (HITL)** is the human gate on #1's high-risk lane. **#6 (learning)** feeds #1's `lookup_prior` with confirmed facts. **#7 (eval)** measures all of it.
- The through-line a judge should be able to state in one sentence: *"Reconciler resolves what it can, verifies every fix, audits every decision, recovers from failures, learns from confirmations, and only asks a human when it truly must."*

**The anti-gaming guard rail (non-negotiable):**

You are optimizing for `dollars_recovered` and `10/10 accuracy`. Both are *proxy metrics*, and an autonomous agent that optimizes a proxy directly will game it — e.g. marking a discrepancy "resolved" without evidence to hit a success rate, or inflating `dollars_recovered` by treating every flag as a dispute. Three structural guards prevent this:

1. **Independent re-verification (§1.6)** — a "resolve" only counts if a *separate* verification pass confirms the discrepancy is gone. The resolver cannot self-certify.
2. **`dollars_recovered` only increments on approved disputes and re-verified corrections (§2)** — never on a draft, never on a flag.
3. **Abstention is a first-class outcome** — `escalated` is a *successful* terminal state, not a failure. The contract already encodes this ("refuse out-of-contract"). An agent that abstains correctly is rewarded in the eval, not penalized. This kills the incentive to force a resolution.

These three guards are what make the "autonomous" claim *credible* rather than reckless — which is the exact line the 30% "production-minded, not brittle" criterion draws.

---

## §10 — Build-Order Delta (maps to your 15-day plan)

You are ~4 days in. Here is the revised order, dependency-first:

| Days | Work | Result |
|---|---|---|
| **Now–+2** | `resolution.py` + schemas + `STAGE_ORDER` extension + wire into `agent.py` | The closed loop works end-to-end on the clean fixture |
| **+2–+3** | `resilience.py` (retry/backoff/circuit breaker/watchdog) + wire into resolution tools | Fault injection is demo-able |
| **+3–+4** | `$2,400` duplicate fixture + `dollars_recovered` aggregation | The money moment works |
| **+4–+5** | Provenance subdocuments + OTel trace_id linkage | "Why did Reconciler do this?" view works |
| **+5–+7** | HITL approval surface (web route) + dispute approve/reject | The approval beat works |
| **+7–+8** | Learning writes to `SharedMemory` + negative facts on reject | 8/10 → 10/10 demo works |
| **+8–+9** | `scripts/eval.py` + `findings.md` numbers | Eval harness done |
| **+9–+10** | `demo.sh` (one-command) + Excalidraw architecture diagram (PNG/SVG export) | Demo is one command |
| **+10–+11** | README-as-landing-page + `seed_invoices.sh` + `fault_inject.sh` | Repo is judge-ready |
| **+11–+15** | Film (multiple takes of the two beats), edit, text description, **submit early** | Shipped |

**Iron rule:** if time runs short, cut the learning demo (#6) or the eval polish (#7) *before* cutting the two demo beats (#2's $2,400 and #4's fault injection). Those two are what the judges repeat.

---

## §11 — File-Change Manifest

**New files:**

| File | Contents |
|---|---|
| `agents/reconciler/resolution.py` | `ResolutionAgent` + `re_read_invoice`, `lookup_prior`, `fetch_vendor_statement`, `apply_correction`, `draft_dispute` tools |
| `agents/reconciler/resilience.py` | `retry_with_backoff`, `CircuitBreaker`, watchdog timeout, DLQ publish helper |
| `scripts/eval.py` | eval harness (§7) |
| `scripts/demo.sh` | one-command demo (§8) |
| `scripts/seed_invoices.sh` | seed the fixture set (incl. the $2,400 duplicate) |
| `scripts/fault_inject.sh` | kill the bank-statement source mid-run (§4) |
| `tests/fixtures/duplicate_invoice_sample.pdf` | the $2,400 duplicate fixture (§2) |
| `README.md` | landing-page README (currently empty) |
| `docs/findings.md` | findings & learnings (scored — do not skip) |
| `architecture.excalidraw` + `architecture.png` | architecture diagram (Excalidraw MCP) |

**Modified files:**

| File | Change |
|---|---|
| `agents/reconciler/schemas.py` | add `ResolutionDecision`, `ResolutionAction`, `DisputeDraft`, `ProvenanceEntry` |
| `agents/reconciler/memory.py` | extend `STAGE_ORDER` with `"resolution"`; extend `increment_counts` with `dollars_recovered`; add provenance read/write helpers |
| `agents/reconciler/middleware.py` | add `RESOLVE_THRESHOLD=0.90` and `DISPUTE_THRESHOLD=0.70` constants |
| `agents/reconciler/agent.py` | register `resolution_wrapped` in `sub_agents` |
| `agents/reconciler/config.py` | add any new frozen constants (thresholds, DLQ/retry defaults) |

**Do not touch:** `instruction_contract.py` (v0.2 is stable and correct — the resolution agent inherits the same `SPECIALIST_PREAMBLE`), `extraction.py`, `verification.py`, `reporting.py` (they are the re-verification and send authorities and must stay unchanged so the loop closes against a stable baseline).

---

*End of spec. The resolution flow (§1), the money moment (§2), and the fault-injection recovery (§4) are the three things that decide the podium. Build in that order.*
