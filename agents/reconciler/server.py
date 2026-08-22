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

from . import approvals, config
from .memory import RunsStore, SharedMemory, get_firestore_client
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
"""


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        from google.adk.telemetry.google_cloud import get_gcp_exporters
        from google.adk.telemetry.setup import maybe_set_otel_providers

        hooks = get_gcp_exporters(
            enable_cloud_tracing=True, enable_cloud_metrics=True, enable_cloud_logging=False
        )
        maybe_set_otel_providers(otel_hooks_to_setup=[hooks])
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
    return HTMLResponse(
        "<html><head><style>" + _PAGE_CSS + "</style></head><body>"
        "<h1>Reconciler</h1>"
        "<p class='muted'>Autonomous invoice reconciliation worker — not a chatbot.</p>"
        "<ul><li><a href='/approvals'>Pending approvals</a> (HITL Tier-2 surface)</li>"
        "<li><code>POST /trigger/pubsub</code> — Pub/Sub push → batch pipeline</li>"
        "<li><code>GET /health</code></li></ul></body></html>"
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
    if not disputes:
        body = "<p>No pending disputes — the agent found nothing that needs you.</p>"
    else:
        cards = []
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
            types = "".join(f"<span class='tag'>{html.escape(str(t))}</span>" for t in (d.get("discrepancies") or []) or ["dispute"])
            cards.append(
                f"<div class='card'><h2>{html.escape(str(d.get('vendor') or d.get('invoice_id')))}"
                f" <span class='muted'>{html.escape(str(d.get('invoice_number') or ''))}</span></h2>"
                f"<div>{types}</div>"
                f"<p>Dispute draft: <b>{html.escape(str(draft.get('subject') or ''))}</b>"
                f" → <code>{html.escape(str(draft.get('recipient') or ''))}</code></p>"
                f"<div class='amt'>${amount:,.2f} at risk</div>" if isinstance(amount, (int, float)) else "<div></div>"
            )
            cards[-1] += (
                f"<pre>{prov}</pre>"
                f"<form method='post' action='/approvals/{d['run_id']}/{d['invoice_id']}/decision?format=json'>"
                f"<input type='hidden' name='action' value='approve'>"
                f"<button class='approve' type='submit'>Approve &amp; send</button></form>"
                f"<form method='post' action='/approvals/{d['run_id']}/{d['invoice_id']}/decision?format=json'>"
                f"<input type='hidden' name='action' value='reject'>"
                f"<input name='reason' placeholder='reason (required to reject)'>"
                f"<button class='reject' type='submit'>Reject</button></form>"
                f"<p class='muted'>run {html.escape(str(d['run_id']))} · invoice {html.escape(str(d['invoice_id']))}</p></div>"
            )
        body = "".join(cards)
    return HTMLResponse(
        "<html><head><style>" + _PAGE_CSS + "</style></head><body>"
        "<h1>Pending approvals — Reconciler HITL Tier-2</h1>" + body + "</body></html>"
    )


@app.post("/approvals/{run_id}/{invoice_id}/decision")
async def decision(
    run_id: str, invoice_id: str, action: str = Form(...), reason: str = Form(""), format: str = "html"
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
        return RedirectResponse("/approvals", status_code=303)
    return JSONResponse(result, status_code=status)


def main() -> None:  # pragma: no cover — container entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
