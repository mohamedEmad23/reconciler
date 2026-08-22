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
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google.cloud import firestore

from . import approvals, config
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
body{font-family:ui-sans-serif,system-ui,sans-serif;margin:2rem auto;max-width:60rem;
     color:#1f2937;background:#f9fafb} h1{font-size:1.4rem}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:.6rem;padding:1rem 1.25rem;
      margin:1rem 0;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.amt{font-size:1.6rem;font-weight:700;color:#047857}
.muted{color:#6b7280;font-size:.85rem}
pre{background:#f3f4f6;padding:.75rem;border-radius:.4rem;white-space:pre-wrap;
    font-size:.78rem;overflow-x:auto}
form{display:inline-block;margin-right:.5rem}
button{border:0;border-radius:.4rem;padding:.45rem 1rem;font-weight:600;cursor:pointer}
.approve{background:#047857;color:#fff}.reject{background:#b91c1c;color:#fff}
input[name=reason]{width:16rem;padding:.35rem;border:1px solid #d1d5db;border-radius:.4rem}
.tag{display:inline-block;background:#fef3c7;border:1px solid #fcd34d;border-radius:999px;
     padding:.1rem .6rem;font-size:.75rem;font-weight:600;margin-right:.35rem}
.score{display:flex;gap:1rem;flex-wrap:wrap}
.score .metric{background:#fff;border:1px solid #e5e7eb;border-radius:.6rem;padding:1rem 1.25rem;
     min-width:9rem}
.score .metric .num{font-size:1.7rem;font-weight:800;color:#047857}
.score .metric .lbl{color:#6b7280;font-size:.8rem}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #e5e7eb}
th{color:#6b7280;font-weight:600}
.status{font-weight:700}.status.completed{color:#047857}.status.failed{color:#b91c1c}
.status.in_progress{color:#b45309}
.fact{font-family:ui-monospace,monospace;font-size:.78rem;background:#f3f4f6;border-radius:.4rem;
     padding:.4rem .6rem;margin:.3rem 0;overflow-x:auto}
"""


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        from google.adk.telemetry.google_cloud import get_gcp_exporters
        from google.adk.telemetry.setup import maybe_set_otel_providers
        from opentelemetry.sdk.resources import Resource

        hooks = get_gcp_exporters(
            enable_cloud_tracing=True, enable_cloud_metrics=True, enable_cloud_logging=False
        )
        # Google's OTLP endpoint rejects spans/metrics with a 400 unless the
        # resource carries ``service.name``. maybe_set_otel_providers falls back
        # to the env-based OTELResourceDetector, which leaves it unset in the
        # container → the "Failed to export ... 400" loop seen in P13. Pin it
        # explicitly (K_REVISION = Cloud Run revision id).
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
    logger.info("reconciler server up — model=%s project=%s", config.GEMINI_MODEL, config.GCP_PROJECT)
    yield


app = FastAPI(title="reconciler", lifespan=_lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def index() -> HTMLResponse:
    runs = await _read_recent_runs(limit=20)
    board = await _scoreboard(runs)
    disputes = await approvals.list_pending_disputes()
    facts = await _read_memory_facts(limit=30)

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

    html_body = (
        "<h1>Reconciler</h1>"
        "<p class='muted'>Autonomous invoice reconciliation worker — not a chatbot.</p>"
        "<div class='score'>"
        f"<div class='metric'><div class='num'>${board['recovered']:,.2f}</div><div class='lbl'>dollars recovered</div></div>"
        f"<div class='metric'><div class='num'>${board['at_risk']:,.2f}</div><div class='lbl'>dollars at risk</div></div>"
        f"<div class='metric'><div class='num'>{board['completed']}</div><div class='lbl'>invoices cleared</div></div>"
        f"<div class='metric'><div class='num'>{board['runs']}</div><div class='lbl'>runs</div></div>"
        "</div>"
        "<h2>Pending approvals <span class='muted'>(HITL Tier-2)</span></h2>"
        + _dispute_cards(disputes, next_page="/")
        + "<h2>Recent runs</h2>"
        + runs_table
        + "<h2>Learned memory <span class='muted'>(Shared Epistemic Memory)</span></h2>"
        + facts_html
    )
    return HTMLResponse(
        "<html><head><style>" + _PAGE_CSS + "</style></head><body>" + html_body + "</body></html>"
    )


def _pipeline_for(directory: str | None) -> Pipeline:
    client = get_firestore_client()
    directory = directory or "tests/fixtures"
    bank_csv = os.path.join(directory, "bank_statement.csv")
    return Pipeline(
        store=RunsStore(client=client),
        memory=SharedMemory(client=client),
        source="local_dir",
        directory=directory,
        # Pair the bank statement with the chosen fixture set (e.g. the
        # duplicates set carries the two INV-2026-0421 debits).
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


async def _scoreboard(runs: list[dict[str, Any]]) -> dict[str, Any]:
    recovered = sum(float(r.get("dollars_recovered") or 0.0) for r in runs)
    at_risk = 0.0
    completed = 0
    failed = 0
    for r in runs:
        summary = r.get("summary") or {}
        at_risk += float(summary.get("dollars_at_risk") or 0.0)
        completed += int(r.get("completed_count") or 0)
        failed += int(r.get("failed_count") or 0)
    return {
        "recovered": recovered,
        "at_risk": at_risk,
        "completed": completed,
        "failed": failed,
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
            f"<div class='card'><h2>{html.escape(str(d.get('vendor') or d.get('invoice_id')))}"
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
    logger.info("trigger: subscription=%s messageId=%s run_id=%s job=%s dir=%s",
                envelope.get("subscription", ""), message_id, run_id, job_type, directory)
    try:
        async with _RUN_LOCK:
            result = await _pipeline_for(directory).run(run_id=run_id, job_type=job_type)
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


def main() -> None:  # pragma: no cover — container entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
