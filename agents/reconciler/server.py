"""Reconciler production surface — a pure FastAPI app.

Replaces ``adk api_server`` as the container entrypoint (P13). The deployed
service exposes EXACTLY these routes — no ADK /run, /run_sse chat surface:

* ``GET  /health``                       — liveness.
* ``POST /trigger/pubsub``               — Pub/Sub push envelope → the batch
  Pipeline (the demo-clarity fix: the scheduled trigger now runs the real
  six-stage pipeline instead of a Supervisor ack). Idempotent: ``run_id``
  derives from ``attributes.run_id`` or the Pub/Sub ``messageId``, so an
  at-least-once redelivery resumes/reuses instead of double-running.
* ``GET  /approvals``                    — the HITL Tier-2 face: pending
  disputes with rendered provenance + approve/reject forms.
* ``POST /approvals/{run_id}/{invoice_id}/decision`` — approve (send + count
  dollars) or reject (record reason). ``?format=json`` for curl/CI.
* ``GET  /``                             — tiny index page.

Auth: every route sits behind Cloud Run IAM (``--no-allow-unauthenticated``);
the only invoker is the dedicated least-privilege ``reconciler-trigger-sa``.
OTel: exporters are initialised in the lifespan (non-fatal on failure) so
Cloud Trace keeps receiving spans from the pipeline's ADK calls.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google.cloud import firestore

from . import approvals, chat, config
from .tools import email_tools
from .memory import (
    MEMORY_COLLECTION,
    RUN_INVOICES_COLLECTION,
    RUNS_COLLECTION,
    RunsStore,
    SharedMemory,
    get_firestore_client,
)
from .pipeline import Pipeline
from .provenance import entries_from_state, render_entry

logger = logging.getLogger("reconciler.server")

# One pipeline run at a time per instance — Cloud Run concurrency handles the
# queueing; the lock keeps a second push from interleaving Firestore writes.
_RUN_LOCK = asyncio.Lock()

_PAGE_CSS = """
:root{--bg:#f6f7f9;--card:#ffffff;--border:#e6e8ec;--text:#111827;--muted:#6b7280;
      --green:#047857;--green-bg:#ecfdf5;--amber:#b45309;--amber-bg:#fffbeb;
      --red:#b91c1c;--red-bg:#fef2f2;--blue:#1d4ed8}
*{box-sizing:border-box}
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
     margin:0;color:var(--text);background:var(--bg);line-height:1.5}
.hero{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 55%,#065f46 100%);
      color:#fff;padding:2.25rem 2rem 1.6rem;position:relative;overflow:hidden}
.hero::after{content:"";position:absolute;inset:0;pointer-events:none;
      background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,.07) 48%,transparent 64%);
      background-size:220% 100%;animation:sheen 7s ease-in-out infinite}
@keyframes sheen{0%{background-position:140% 0}100%{background-position:-40% 0}}
.hero .brand{font-size:1.9rem;font-weight:800;letter-spacing:-.02em}
.hero .tagline{margin-top:.4rem;font-size:1.05rem;color:#cbd5e1;max-width:54rem}
.hero .meta{margin-top:1.1rem;display:flex;gap:.5rem;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:.4rem;background:rgba(255,255,255,.12);
       border:1px solid rgba(255,255,255,.22);border-radius:999px;padding:.22rem .75rem;
       font-size:.78rem;font-weight:600}
.dot{width:.5rem;height:.5rem;border-radius:50%;background:#34d399;
     box-shadow:0 0 0 0 rgba(52,211,153,.55);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.55)}
  70%{box-shadow:0 0 0 7px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.wrap{max-width:64rem;margin:0 auto;padding:1.5rem 1rem 3rem;animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
h2{font-size:1.06rem;font-weight:700;margin:1.9rem 0 .6rem;letter-spacing:-.01em}
h2 .kicker{display:block;font-size:.68rem;font-weight:700;text-transform:uppercase;
           letter-spacing:.09em;color:var(--muted);margin-bottom:.15rem}
.score{display:grid;grid-template-columns:repeat(auto-fit,minmax(10.5rem,1fr));gap:.9rem}
.metric{background:var(--card);border:1px solid var(--border);border-radius:.75rem;
        padding:1.05rem 1.2rem;position:relative;overflow:hidden;
        transition:transform .18s ease,box-shadow .18s ease}
.metric:hover{transform:translateY(-3px);box-shadow:0 10px 26px rgba(15,23,42,.09)}
.metric::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:#94a3b8}
.metric.green::before{background:var(--green)}
.metric.amber::before{background:var(--amber)}
.metric.blue::before{background:var(--blue)}
.metric .num{font-size:1.75rem;font-weight:800;letter-spacing:-.02em}
.metric .lbl{color:var(--muted);font-size:.82rem;margin-top:.12rem}
.metric.green .num{color:var(--green)}
.metric.amber .num{color:var(--amber)}
.metric.blue .num{color:var(--blue)}
.metric.neutral .num{color:var(--text)}
.card{background:var(--card);border:1px solid var(--border);border-radius:.75rem;
      padding:1.1rem 1.25rem;margin:1rem 0;box-shadow:0 1px 2px rgba(0,0,0,.04);
      transition:box-shadow .18s ease,transform .18s ease}
.card:hover{box-shadow:0 8px 22px rgba(15,23,42,.07)}
.card.hitl{border-color:#fcd34d;background:linear-gradient(180deg,#fffbeb,#fff);
      border-left:4px solid var(--amber);animation:glow 2.6s ease-in-out infinite}
@keyframes glow{0%,100%{box-shadow:0 0 0 0 rgba(252,211,77,0)}
  50%{box-shadow:0 0 0 6px rgba(252,211,77,.16)}}
.card h2{font-size:1rem;margin:0 0 .4rem}
.amt{font-size:1.5rem;font-weight:800;color:var(--amber)}
.muted{color:var(--muted);font-size:.85rem}
pre{background:#0f172a;color:#e2e8f0;padding:.85rem;border-radius:.5rem;white-space:pre-wrap;
    font-size:.76rem;overflow-x:auto;line-height:1.45}
form{display:inline-block;margin-right:.5rem;margin-top:.4rem}
button{border:0;border-radius:.5rem;padding:.5rem 1.15rem;font-weight:700;cursor:pointer;
       font-size:.9rem;transition:filter .15s ease,transform .15s ease}
button:hover{filter:brightness(1.09);transform:translateY(-1px)}
button:active{transform:translateY(0)}
.approve{background:var(--green);color:#fff}
.reject{background:var(--red);color:#fff}
input[name=reason]{width:16rem;padding:.45rem;border:1px solid #d1d5db;border-radius:.5rem}
.tag{display:inline-block;background:var(--amber-bg);border:1px solid #fcd34d;border-radius:999px;
     padding:.12rem .6rem;font-size:.72rem;font-weight:700;margin-right:.35rem;color:#92400e}
.pipeline{display:flex;flex-wrap:wrap;gap:.5rem}
.stage{background:var(--card);border:1px solid var(--border);border-radius:.6rem;
       padding:.55rem .85rem;font-size:.8rem;flex:1 1 11rem;
       transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.stage:hover{transform:translateY(-2px);border-color:#cbd5e1;box-shadow:0 6px 16px rgba(15,23,42,.06)}
.stage b{display:block;font-size:.86rem}
.stage span{color:var(--muted);font-size:.74rem}
.feature{display:flex;gap:.9rem;align-items:flex-start;padding:.8rem 0;border-bottom:1px solid var(--border)}
.feature:last-child{border-bottom:0}
.feature .fname{font-weight:700;font-size:.92rem;min-width:11rem;flex-shrink:0}
.feature .fdesc{color:var(--muted);font-size:.85rem}
table{border-collapse:collapse;width:100%;font-size:.84rem;background:var(--card);
      border:1px solid var(--border);border-radius:.75rem;overflow:hidden}
th,td{text-align:left;padding:.5rem .7rem;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:700;background:#fafafa}
tbody tr{transition:background .15s ease}
tbody tr:hover{background:#f8fafc}
tr:last-child td{border-bottom:0}
.status{font-weight:700}
.status.completed{color:var(--green)}
.status.failed{color:var(--red)}
.status.in_progress{color:var(--amber)}
.status.completed_with_errors{color:var(--amber)}
.fact{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem;background:#f3f4f6;
      border:1px solid var(--border);border-radius:.5rem;padding:.45rem .7rem;margin:.35rem 0;
      overflow-x:auto}
.gcp{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:.7rem}
.gcp .svc{background:var(--card);border:1px solid var(--border);border-radius:.75rem;
          padding:.8rem 1rem;transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.gcp .svc:hover{transform:translateY(-2px);border-color:#cbd5e1;box-shadow:0 6px 16px rgba(15,23,42,.06)}
.gcp .svc b{font-size:.88rem}
.gcp .svc .what{color:var(--muted);font-size:.78rem;margin-top:.15rem}
"""

_CHAT_JS = """
<script>
(function(){
  var form=document.getElementById('chat-form');
  if(!form)return;
  var out=document.getElementById('chat-a');
  var q=document.getElementById('chat-q');
  var busy=document.getElementById('chat-busy');
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var text=(q.value||'').trim();
    if(!text)return;
    if(busy)busy.textContent='…';
    fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:text})})
      .then(function(r){return r.json();})
      .then(function(d){
        if(busy)busy.textContent='';
        out.textContent=d.answer||d.error||'no answer';
      })
      .catch(function(err){if(busy)busy.textContent='';out.textContent='error: '+err;});
  });
})();
</script>
"""


def _chat_widget_html() -> str:
    return (
        "<div class='card'>"
        "<p class='muted'>Ask about what the agent did — runs, invoices, flags, learned "
        "facts, or dollars at risk. Answers are read from the auditable Firestore record, "
        "not recalled from a vector store.</p>"
        "<form id='chat-form' onsubmit='return false;' style='display:flex;gap:.5rem;width:100%;margin-top:.4rem'>"
        "<input id='chat-q' type='text' placeholder='e.g. what invoices were processed and which were flagged?' "
        "style='flex:1;padding:.55rem;border:1px solid #d1d5db;border-radius:.5rem'>"
        "<button class='approve' type='submit'>Ask</button></form>"
        "<div id='chat-busy' style='color:var(--muted);font-size:.8rem;margin-top:.4rem;min-height:1rem'></div>"
        "<pre id='chat-a' style='margin-top:.5rem'>Answers appear here.</pre>"
        "</div>"
    )


# Static judge-facing copy (the agent's capabilities are a fixed property of the
# system — this is documentation rendered as UI, not runtime data).
_PIPELINE_STAGES = [
    ("Intake", "discovers invoice PDFs (Gmail / Drive / local)"),
    ("Extraction", "reads the PDF into structured JSON (temperature 0.0)"),
    ("Verification", "CoVe cross-checks every line against the bank statement"),
    ("Resolution", "decides resolve / dispute / escalate from real evidence"),
    ("Categorization", "maps line items to the chart of accounts"),
    ("Reconciliation", "final verdict + arithmetic invariants"),
    ("Reporting", "composes the weekly digest — blocked from sending"),
]

_GCP_SERVICES = [
    ("Cloud Scheduler", f"{config.SCHEDULER_JOB} — the cron that wakes the agent (no one asks it to run)"),
    ("Pub/Sub", f"{config.TOPIC_TRIGGER} event bus + {config.TOPIC_DLQ} dead-letter queue"),
    ("Cloud Run", "the runtime (FastAPI surface, service-account-only invoker)"),
    ("Vertex AI", f"{config.GEMINI_MODEL} — extraction, verification, resolution"),
    ("Firestore", "runs + run_invoices audit trail + shared epistemic memory"),
    ("Secret Manager", "credentials (OAuth + Gmail app password) — never in code"),
    ("Cloud Logging + Trace", "structured logs + distributed traces"),
]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Observability: Cloud Run auto-instruments HTTP requests into Cloud Trace,
    # and the pipeline emits structured logs + a Firestore audit trail. The ADK
    # OTLP cloud exporters are OPT-IN (RECONCILER_OTEL=1) because Google's OTLP
    # endpoint rejects the ADK-built resource with a 400 on every flush (the
    # "Failed to export metrics/span batch code: 400" loop), and that tight
    # error loop starved the 512Mi instance — stalling Gemini calls and tripping
    # the vertex-ai circuit breaker in live testing. Model-level spans are
    # therefore off by default; HTTP traces + logs + audit trail carry the
    # observability story.
    if os.environ.get("RECONCILER_OTEL") == "1":
        try:
            from google.adk.telemetry.google_cloud import get_gcp_exporters
            from google.adk.telemetry.setup import maybe_set_otel_providers
            from opentelemetry.sdk.resources import Resource

            hooks = get_gcp_exporters(
                enable_cloud_tracing=True, enable_cloud_metrics=True, enable_cloud_logging=False
            )
            otel_resource = Resource.create(
                {
                    "service.name": "reconciler",
                    "service.version": os.environ.get("K_REVISION", "dev"),
                }
            )
            maybe_set_otel_providers(
                otel_resource=otel_resource, otel_hooks_to_setup=[hooks]
            )
            logger.info(
                "otel exporters initialised (spans=%s metrics=%s)",
                bool(hooks.span_processors), bool(hooks.metric_readers),
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must never block serving
            logger.warning("otel init skipped: %s", exc)
    else:
        logger.info("otel cloud export disabled (RECONCILER_OTEL unset) — HTTP traces + logs + Firestore audit active")
    logger.info("reconciler server up — model=%s project=%s", config.GEMINI_MODEL, config.GCP_PROJECT)
    yield


app = FastAPI(title="reconciler", lifespan=_lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def index() -> HTMLResponse:
    runs = await _read_recent_runs(limit=20)
    disputes = await approvals.list_pending_disputes()
    facts = await _read_memory_facts(limit=30)
    board = await _scoreboard(runs, disputes)

    score_html = (
        f"<div class='metric green'><div class='num'>${board['recovered']:,.2f}</div>"
        f"<div class='lbl'>dollars recovered · all time</div></div>"
        f"<div class='metric amber'><div class='num'>${board['at_risk']:,.2f}</div>"
        f"<div class='lbl'>awaiting your approval · {board['pending']} dispute"
        f"{'s' if board['pending'] != 1 else ''}</div></div>"
        f"<div class='metric blue'><div class='num'>{board['completed']}</div>"
        f"<div class='lbl'>invoices processed</div></div>"
        f"<div class='metric neutral'><div class='num'>{board['runs']}</div>"
        f"<div class='lbl'>reconciliation runs</div></div>"
    )

    pipeline_html = "".join(
        f"<div class='stage'><b>{i + 1}. {html.escape(name)}</b>"
        f"<span>{html.escape(desc)}</span></div>"
        for i, (name, desc) in enumerate(_PIPELINE_STAGES)
    )

    gcp_html = "".join(
        f"<div class='svc'><b>{html.escape(name)}</b>"
        f"<div class='what'>{html.escape(what)}</div></div>"
        for name, what in _GCP_SERVICES
    )

    runs_rows: list[str] = []
    for r in runs:
        status = str(r.get("status") or "in_progress")
        summary = r.get("summary") or {}
        recovered = float(r.get("dollars_recovered") or 0.0)
        risk = float(summary.get("dollars_at_risk") or 0.0)
        flagged = summary.get("flagged_count")
        job = str(r.get("job_type") or "weekly_reconcile")
        runs_rows.append(
            f"<tr><td><code>{html.escape(str(r.get('run_id')))}</code></td>"
            f"<td>{html.escape(job)}</td>"
            f"<td><span class='status {html.escape(status)}'>{html.escape(status)}</span></td>"
            f"<td>{r.get('completed_count') or 0}/{r.get('invoice_count') or 0}</td>"
            f"<td>{'—' if flagged is None else html.escape(str(flagged))}</td>"
            f"<td>${recovered:,.2f}</td>"
            f"<td>${risk:,.2f}</td></tr>"
        )
    runs_table = (
        "<table><tr><th>run</th><th>job</th><th>status</th><th>done</th>"
        "<th>flagged</th><th>recovered</th><th>at risk</th></tr>"
        + "".join(runs_rows)
        + "</table>"
    )

    fact_rows: list[str] = []
    for f in facts:
        ns = str(f.get("namespace") or "?")
        key = str(f.get("key") or "?")
        value = json.dumps(f.get("value") or {}, sort_keys=True)
        fact_rows.append(
            f"<div class='fact'><b>{html.escape(ns)}:</b>{html.escape(key)} → {html.escape(value)}</div>"
        )
    facts_html = (
        "".join(fact_rows) if fact_rows else "<p class='muted'>No learned facts yet.</p>"
    )

    body = (
        "<div class='hero'>"
        "<div class='brand'>Reconciler</div>"
        "<div class='tagline'>Autonomous invoice reconciliation — it wakes on a schedule, "
        "does the work end-to-end, and stops to ask a human before touching money. Not a chatbot.</div>"
        "<div class='meta'>"
        f"<span class='badge'><span class='dot'></span>wakes {html.escape(config.SCHEDULER_SCHEDULE)} "
        f"via <code style='opacity:.85'>{html.escape(config.SCHEDULER_JOB)}</code></span>"
        f"<span class='badge'>Vertex AI · {html.escape(config.GEMINI_MODEL)}</span>"
        "<span class='badge'>7 autonomous specialists</span>"
        "</div>"
        "</div>"
        "<div class='wrap'>"
        "<div class='score'>" + score_html + "</div>"
        "<h2><span class='kicker'>How it works</span>Seven stages, zero hand-holding</h2>"
        "<div class='pipeline'>" + pipeline_html + "</div>"
        "<h2><span class='kicker'>Human-in-the-loop · Tier 2</span>Awaiting your approval</h2>"
        + _dispute_cards(disputes, next_page="/")
        + "<h2><span class='kicker'>Architecture</span>Where it runs — Google Cloud</h2>"
        "<div class='gcp'>" + gcp_html + "</div>"
        "<h2><span class='kicker'>Audit trail</span>Recent runs</h2>"
        + runs_table
        + "<h2><span class='kicker'>Memory</span>Learned facts (Shared Epistemic Memory)</h2>"
        + facts_html
        + "<h2><span class='kicker'>Ask the agent</span>Chat with Reconciler</h2>"
        + _chat_widget_html()
        + "</div>"
    )
    return HTMLResponse(
        "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>" + _PAGE_CSS + "</style></head><body>" + body + _CHAT_JS + "</body></html>"
    )


def _pipeline_for(directory: str | None, source: str | None) -> Pipeline:
    client = get_firestore_client()
    source = source or "local_dir"
    directory = directory or "tests/fixtures"
    bank_csv = os.path.join(directory, "bank_statement.csv")
    return Pipeline(
        store=RunsStore(client=client),
        memory=SharedMemory(client=client),
        source=source,
        directory=directory,
        # Pair the bank statement with the chosen fixture set (e.g. the
        # duplicates set carries the two INV-2026-0421 debits). For the gmail
        # source the directory is unused for intake — it only selects the bank
        # statement the invoices are cross-checked against.
        bank_csv=bank_csv if os.path.exists(bank_csv) else None,
    )


# ---------------------------------------------------------------------------
# Dashboard read-only views (P17) — no agent logic, no LLM calls. The home
# page is a single judge-facing surface over the same data the pipeline
# already persists, so "how does it work" and "what did it do" are one click.
# ---------------------------------------------------------------------------


async def _read_recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    client = get_firestore_client()
    query = (
        client.collection(RUNS_COLLECTION)
        .order_by("started_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    runs: list[dict[str, Any]] = []
    async for snap in query.stream():
        d = snap.to_dict() or {}
        d["run_id"] = d.get("run_id") or snap.id
        runs.append(d)
    return runs


async def _read_run_invoices(run_id: str) -> list[dict[str, Any]]:
    client = get_firestore_client()
    query = client.collection(RUN_INVOICES_COLLECTION).where(
        filter=firestore.FieldFilter("run_id", "==", run_id)
    )
    out: list[dict[str, Any]] = []
    async for snap in query.stream():
        d = snap.to_dict() or {}
        d["_id"] = snap.id
        out.append(d)
    out.sort(key=lambda d: str(d.get("invoice_id") or ""))
    return out


async def _read_memory_facts(limit: int = 30) -> list[dict[str, Any]]:
    client = get_firestore_client()
    query = client.collection(MEMORY_COLLECTION).limit(limit)
    facts: list[dict[str, Any]] = []
    async for snap in query.stream():
        d = snap.to_dict() or {}
        d["_id"] = snap.id
        facts.append(d)
    facts.sort(key=lambda d: (str(d.get("namespace") or ""), str(d.get("key") or "")))
    return facts


async def _scoreboard(
    runs: list[dict[str, Any]], disputes: list[dict[str, Any]]
) -> dict[str, Any]:
    recovered = sum(float(r.get("dollars_recovered") or 0.0) for r in runs)
    # "dollars at risk" = the LIVE total still awaiting human approval, not the
    # frozen number the pipeline stamped at run end. Approving a dispute moves
    # its amount from here into "recovered" immediately.
    at_risk = sum(
        float((d.get("draft") or {}).get("amount_at_risk") or 0.0) for d in disputes
    )
    completed = sum(int(r.get("completed_count") or 0) for r in runs)
    return {
        "recovered": recovered,
        "at_risk": at_risk,
        "completed": completed,
        "pending": len(disputes),
        "runs": len(runs),
    }


def _dispute_cards(disputes: list[dict[str, Any]], *, next_page: str = "/approvals") -> str:
    if not disputes:
        return "<p>No pending disputes — the agent found nothing that needs you.</p>"
    cards: list[str] = []
    for d in disputes:
        draft = d.get("draft") or {}
        amount = draft.get("amount_at_risk")
        prov = ""
        entries, errors = entries_from_state(
            {"stages_data": {"resolution": {"provenance": d.get("provenance")}}}
        )
        for problem in errors:
            prov += f"<p class='muted'>provenance parse problem: {html.escape(problem)}</p>"
        for entry in entries:
            prov += html.escape(render_entry(entry, invoice_id=d.get("invoice_id")))
        types = "".join(
            f"<span class='tag'>{html.escape(str(t))}</span>"
            for t in (d.get("discrepancies") or []) or ["dispute"]
        )
        amt_html = (
            f"<div class='amt'>${amount:,.2f} at risk</div>"
            if isinstance(amount, (int, float))
            else "<div></div>"
        )
        card = (
            f"<div class='card hitl'><h2>{html.escape(str(d.get('vendor') or d.get('invoice_id')))}"
            f" <span class='muted'>{html.escape(str(d.get('invoice_number') or ''))}</span></h2>"
            f"<div>{types}</div>"
            f"<p>Dispute draft: <b>{html.escape(str(draft.get('subject') or ''))}</b>"
            f" → <code>{html.escape(str(draft.get('recipient') or ''))}</code></p>"
            f"{amt_html}"
            f"<pre>{prov}</pre>"
            f"<form method='post' action='/approvals/{d['run_id']}/{d['invoice_id']}/decision?next={next_page}'>"
            f"<input type='hidden' name='action' value='approve'>"
            f"<button class='approve' type='submit'>Approve &amp; send</button></form>"
            f"<form method='post' action='/approvals/{d['run_id']}/{d['invoice_id']}/decision?next={next_page}'>"
            f"<input type='hidden' name='action' value='reject'>"
            f"<input name='reason' placeholder='reason (required to reject)'>"
            f"<button class='reject' type='submit'>Reject</button></form>"
            f"<p class='muted'>run {html.escape(str(d['run_id']))} · invoice {html.escape(str(d['invoice_id']))}</p></div>"
        )
        cards.append(card)
    return "".join(cards)


@app.post("/trigger/pubsub")
async def trigger_pubsub(envelope: dict[str, Any]) -> JSONResponse:
    """Pub/Sub push → one idempotent batch pipeline run."""
    message = envelope.get("message") or {}
    message_id = message.get("messageId") or "unknown"
    attributes = message.get("attributes") or {}
    try:
        payload = json.loads(base64.b64decode(message.get("data", "")).decode()) if message.get("data") else {}
    except Exception:  # noqa: BLE001
        payload = {}
    job_type = payload.get("job_type") or attributes.get("job_type") or "weekly_reconcile"
    run_id = attributes.get("run_id") or f"pubsub_{message_id}"
    directory = attributes.get("directory")
    source = attributes.get("source")
    logger.info("trigger: subscription=%s messageId=%s run_id=%s job=%s source=%s dir=%s",
                envelope.get("subscription", ""), message_id, run_id, job_type, source, directory)
    try:
        async with _RUN_LOCK:
            result = await _pipeline_for(directory, source).run(run_id=run_id, job_type=job_type)
        # "Agent reports its results" beat: send a benign run summary to the
        # operator (no HITL gate — it never touches money). Skipped on idempotent
        # redelivery (skipped=True) so a Pub/Sub retry can't double-send. Non-fatal:
        # a mail outage must never break (or force a redelivery of) the run.
        if not result.skipped:
            try:
                summary_send = await asyncio.to_thread(
                    email_tools.send_run_summary,
                    run_id=result.run_id,
                    job_type=result.job_type,
                    invoices_total=result.invoices_total,
                    invoices_completed=result.invoices_completed,
                    invoices_failed=result.invoices_failed,
                    flagged_count=result.flagged_count,
                    dollars_at_risk=result.dollars_at_risk,
                    dollars_recovered=result.dollars_recovered,
                )
                logger.info("run-summary email sent: %s", summary_send.get("sent"))
            except Exception as exc:  # noqa: BLE001 — mail must not break the run
                logger.warning("run-summary email failed: %s", exc)
        return JSONResponse(
            {
                "status": "ok",
                "run_id": result.run_id,
                "invoices_total": result.invoices_total,
                "invoices_completed": result.invoices_completed,
                "invoices_failed": result.invoices_failed,
                "flagged_count": result.flagged_count,
                "dollars_at_risk": result.dollars_at_risk,
                "dollars_recovered": result.dollars_recovered,
                "skipped_idempotent": result.skipped,
            }
        )
    except Exception as exc:  # noqa: BLE001 — 500 ⇒ Pub/Sub redelivers ⇒ idempotent resume
        logger.exception("pipeline run failed: %s", exc)
        return JSONResponse({"status": "error", "run_id": run_id, "error": str(exc)}, status_code=500)


@app.get("/approvals")
async def approvals_page() -> HTMLResponse:
    disputes = await approvals.list_pending_disputes()
    body = _dispute_cards(disputes)
    return HTMLResponse(
        "<html><head><style>" + _PAGE_CSS + "</style></head><body>"
        "<h1>Pending approvals — Reconciler HITL Tier-2</h1>" + body + "</body></html>"
    )


@app.post("/approvals/{run_id}/{invoice_id}/decision")
async def decision(
    run_id: str,
    invoice_id: str,
    action: str = Form(...),
    reason: str = Form(""),
    format: str = "html",
    next: str = "/approvals",
):
    if action == "approve":
        result = await approvals.approve(run_id=run_id, invoice_id=invoice_id)
    elif action == "reject":
        result = await approvals.reject(run_id=run_id, invoice_id=invoice_id, reason=reason)
    else:
        result = {"status": "error", "error": f"unknown action {action!r}"}
    status = 200 if result.get("status") in {"approved", "rejected"} else (
        409 if result.get("status") == "already_decided" else 404 if result.get("status") == "not_found" else 400
    )
    if format == "json":
        return JSONResponse(result, status_code=status)
    if status == 200:
        return RedirectResponse(next, status_code=303)
    return JSONResponse(result, status_code=status)


@app.post("/chat")
async def chat_route(payload: dict[str, Any]) -> JSONResponse:
    """Ask the agent a question about what it did.

    Accepts ``{"question": "..."}`` and returns ``{"answer": "..."}``. The
    answer is grounded in the structured Firestore record via the chat
    assistant's read-only tools — never vector recall. A fresh session per
    request keeps the Q&A stateless.
    """
    question = str((payload or {}).get("question") or "").strip()
    if not question:
        return JSONResponse({"answer": "", "error": "empty question"}, status_code=400)
    try:
        answer = await chat.ask_question(question, session_id=f"chat-{uuid.uuid4().hex[:12]}")
        return JSONResponse({"answer": answer})
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat failed: %s", exc)
        return JSONResponse(
            {"answer": "", "error": f"{type(exc).__name__}: {exc}"}, status_code=500
        )


def main() -> None:  # pragma: no cover — container entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
