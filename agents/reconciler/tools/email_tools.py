"""Email send tool — the ONLY send capability in the system.

Proxy-agent pattern (closed-loop design §5): the resolution agent *drafts*
disputes; this module, called exclusively by the HITL approval surface
(``approvals.approve``), is the only component that can actually send mail.
No agent ever holds this module's handle.

Transport is Gmail **SMTP with an app password** (not OAuth), so the sender is a
dedicated credential that is portable to any operator — the human's personal
OAuth grant is never involved. The app password lives in Secret Manager
(``reconciler-smtp-config``) alongside the sender address and an optional
``redirect_to`` override (used to point demo sends at a visible inbox).
Nothing is written to disk.
"""

from __future__ import annotations

import json
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

from .. import config

# Lazy cache of the decoded SMTP secret (sender / password / redirect_to).
_smtp: dict[str, str] | None = None


def _smtp_config() -> dict[str, str]:
    """Read the SMTP config JSON from Secret Manager (lazy, cached).

    The secret ``reconciler-smtp-config`` holds
    ``{"sender": str, "password": str, "redirect_to": str | None}``.
    """
    global _smtp
    if _smtp is not None:
        return _smtp
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{config.GCP_PROJECT}/secrets/{config.SECRET_SMTP_CONFIG}/versions/latest"
    payload = client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")
    cfg: dict[str, str] = json.loads(payload)
    _smtp = cfg
    return cfg


def send_email(recipient: str, subject: str, body: str) -> dict[str, Any]:
    """Send a plain-text email via Gmail SMTP.

    Returns ``{"sent": bool, "message_id": str | None, "error": str | None}``.
    Never raises — failures are captured so the approval flow can record the
    send status without losing the human decision.
    """
    try:
        cfg = _smtp_config()
        sender = cfg["sender"]
        password = cfg["password"]
        # Demo redirect: if configured, deliver to a visible inbox instead of
        # the vendor's (often fictional) billing address, and note the original
        # recipient in the body so the send is still honest.
        redirect_to = cfg.get("redirect_to") or ""
        final_recipient = redirect_to or recipient

        msg = MIMEText(body or "")
        msg["From"] = sender
        msg["To"] = final_recipient
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        if redirect_to:
            msg["X-Original-Recipient"] = recipient

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.send_message(msg)
        return {"sent": True, "message_id": None, "error": None}
    except Exception as exc:  # noqa: BLE001 — surfacing, not crashing
        return {"sent": False, "message_id": None, "error": f"{type(exc).__name__}: {exc}"}


def send_run_summary(
    *,
    run_id: str,
    job_type: str,
    invoices_total: int,
    invoices_completed: int,
    invoices_failed: int,
    flagged_count: int,
    dollars_at_risk: float,
    dollars_recovered: float,
) -> dict[str, Any]:
    """Send a benign run-summary email to the operator (no HITL gate).

    This is the "agent reports its results" beat of the autonomous loop: after
    a cron run the agent emails the operator a plain summary of what it did.
    It never touches money — dispute escalation stays behind the HITL approval
    surface (``approvals.approve``). Failures are captured, never raised, so a
    mail outage can never break (or force a redelivery of) a run.
    """
    try:
        cfg = _smtp_config()
        recipient = cfg.get("redirect_to") or cfg["sender"]
        subject = f"Reconciler {job_type} complete — {flagged_count} flagged"
        matched = max(0, invoices_completed - flagged_count)
        lines = [
            f"Reconciler autonomous {job_type} run complete.",
            f"  run id              : {run_id}",
            f"  invoices processed  : {invoices_completed}/{invoices_total}",
            f"  matched (no action) : {matched}",
            f"  flagged for review  : {flagged_count}",
            f"  failed              : {invoices_failed}",
            f"  dollars at risk     : ${dollars_at_risk:,.2f}",
            f"  dollars recovered   : ${dollars_recovered:,.2f}",
            "",
            "Review flagged items and approve / dispute / escalate on the dashboard:",
            f"  {config.SERVICE_URL}",
            "",
            "(Automated message from the Reconciler agent. It never acts on money",
            " without your explicit approval.)",
        ]
        body = "\n".join(lines)
        return send_email(recipient, subject, body)
    except Exception as exc:  # noqa: BLE001 — surfacing, not crashing
        return {"sent": False, "message_id": None, "error": f"{type(exc).__name__}: {exc}"}
