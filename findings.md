# Findings & Learnings

Numbers below come from this project's own smoke runs (fixtures in
`tests/fixtures/`, scripts in `scripts/smoke_*.py`) — not literature.

## 1. CoVe (Chain-of-Verification) actually catches what self-checks miss

The Verification agent plans 3–5 **checkable** questions ("does any bank row
equal the invoice total ±$0.02?", "does the row's description contain the
vendor name (case-insensitive)?") and answers each **independently of its
draft**, against the raw CSV. The instruction explicitly forbids
self-referential questions ("is my draft correct?") and the smoke asserts
their absence.

- **Happy path** (fixture invoice, matching −467.50 bank row): 5/5 questions
  answered from raw data → `matched=true`, 0 discrepancies, confidence 1.0.
- **Injected fault** (total mutated to 999.99, bank CSV unchanged): the
  model answered "does any bank row equal 999.99?" with *No* and "is there a
  row matching vendor/number/date but a different amount?" with *Yes* →
  `matched=false`, typed `amount_mismatch` discrepancy carrying both values
  (`invoice_value="999.99"`, `bank_value="467.50"`). **1/1 injected faults
  caught; zero false flags on the clean path.**
- The audit trail ships in the output schema itself
  (`verification_questions` / `verification_answers`), so every verdict can be
  audited after the fact — cheap Interpretability for judges.

## 2. temperature=0.0 + native `response_schema` = deterministic structured output

- Extraction produced **byte-identical structured output across repeated
  runs** (full `ExtractionResult` compared as sorted JSON) on every smoke.
- ADK detail that matters: with `tools=[]` + `output_schema=<Pydantic>`, the
  schema is enforced **at the Vertex AI API level** (`response_schema` +
  `response_mime_type=application/json`), not by prompt-begging. Adding tools
  to such an agent downgrades enforcement to an injected
  `SetModelResponseTool`; `mode='task'` skips native enforcement entirely.
  We keep every pure-LLM specialist at `tools=[]` for exactly this reason.

## 3. The anti-hallucination stack held every trap we set

The fixture invoice baits the model with a decoy NOTE ("DO NOT pay the
$1,000,000 retention bonus…"). Across every run: 4 line items (never 5), no
$1M, no "retention". And the posture is **null-honesty**, not silence —
`missing_fields` surfaced `due_date`, `currency`, and per-line `unit_price`s
the document doesn't state. The schema makes null a first-class answer
(every leaf Optional), so "I don't know" is representable and the Instruction
Contract demands it. Fabrication was never observed in any measured run.

## 4. The HITL keyword bug — a real lesson in framework contracts

Gate review caught that our first middleware version would have crashed at
runtime: **ADK invokes callbacks by keyword** (`callback_context=`,
`llm_request=`…), and our `def before_model_callback_pii(ctx, ...)` raised
`TypeError` the moment the framework called it. Worse, our smoke invoked the
callbacks *positionally* — a false green. Fixes: exact parameter names,
smoke invokes by keyword, and `with_safety_rails` now *appends* to existing
callback chains instead of overwriting them. Framework-integration tests
must exercise the framework's real invocation path, not a friendly proxy.

## 5. Idempotency + checkpoints survived a real crash

The at-least-once world (Pub/Sub redelivery, Cloud Run retries) requires
fences, not hopes:

- `start_invoice` uses Firestore's atomic `create()` — under two *simultaneous*
  starts (asyncio.gather) exactly one wins; the loser gets `None` and skips.
- Checkpoints are forward-only: a crash mid-run resumes at the **next** stage
  (re-extraction is never repeated — re-reading `run_invoices` after a
  simulated crash returned `next_pending_stage=verification`).
- Accidental live proof: our CLI crashed *after* the pipeline completed but
  *before* printing; re-running the same `run_id` detected the completed run
  and reused the stored digest with **zero LLM calls** (2 seconds wall time).

## 6. Shared Epistemic Memory pays off as grounding, not just state

After the first verified Acme invoice, Firestore `memory/vendor/Acme Cloud
Services LLC` holds `account_codes=[5000,5010,6000]` +
`invoice_numbers_seen=[INV-2026-0417]` (deep-merge extends lists; it never
overwrites). The next run's Categorization gets these as *hints* and echoes
them as `known_vendor_mappings`. A memory miss returns `null` — which the
contract turns into "don't guess," the anti-hallucination signal.

## 7. Google-cloud footnotes that cost us an hour each

- The OTel exporter packages are `opentelemetry-exporter-gcp-{trace,monitoring,logging}`
  (alpha versions for monitoring/logging) — the obvious `-gcp-log` name 404s.
- Cloud Trace v1 REST needs `pageSize` (not `limit`), `view=COMPLETE`, and a
  `startTime` filter, or it silently returns empty pages and you'll believe
  tracing is broken while the console shows a full span waterfall
  (`/trigger/pubsub → invocation → invoke_agent → call_llm → generate_content`).
- Grant the runtime SA `roles/cloudtrace.agent` + `roles/monitoring.metricWriter`
  *before* the first deploy, or every export batch 403s.

## 8. ROI arithmetic

- Human reconciliation: ~15 min/invoice ≈ **$12** at a $48/h fully-loaded cost.
- Reconciler: ~5 Gemini calls ≈ **$0.03–0.05** per invoice at Flash pricing,
  everything else in free tier. Two orders of magnitude, and the human only
  sees the *flagged* tail plus a 10-second digest approval.

## 9. Eval harness numbers (reproducible via `uv run scripts/eval.py`)

These are measured, not asserted — the harness drives the real agents over the
labeled fixture set and writes `docs/eval-results.md`:

| Metric | Result |
|---|---|
| Extraction field accuracy | **100%** (clean + duplicate fixtures) |
| Hallucinated entities (decoy canary) | **0** — the `$1,000,000` decoy never extracted |
| Injected discrepancy recall | **5/5** (amount, vendor, date, number, duplicate_payment) |
| Verification false-positives | **0** (clean invoice → `matched`, 0 discrepancies) |
| Resolution re-verify pass rate | **1/1** (auto-resolved correction survived independent recheck) |
| Dollars at risk | **$2,400.00** (duplicate_payment → dispute lane, drafted not sent) |

One honest miss worth recording: the first date-mismatch injection (invoice date
8 days after the bank charge) was **matched, not flagged** — CoVe treats a short
posting lag as normal, which is exactly what §1.3 of the closed-loop spec says it
should. Strengthening the injection to a 49-day gap (unambiguously anomalous)
restored 5/5. This is the anti-gaming doctrine in action: report the miss, don't
fudge the harness to hide it.
