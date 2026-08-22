"""Email send tool — the ONLY send capability in the system.

Proxy-agent pattern (closed-loop design §5): the resolution agent *drafts*
disputes; this module, called exclusively by the HITL approval surface
(``approvals.approve``), is the only component that can actually send mail.
Credentials come from Secret Manager via the same isolated loader the intake
tools use (``intake_tools._oauth_credentials``) — nothing is written to disk
and no agent ever holds this module's handle.

NOTE ON SCOPES: sending requires the ``https://mail.google.com/``-family
``gmail.send`` scope on the stored OAuth grant. ``tests/mint_token.py`` now
requests ``gmail.send`` alongside ``gmail.modify`` — re-mint and re-upload the
secret once (see README) and real sends work; until then a 403 surfaces in
``send["error"]`` and the approval decision itself is unaffected.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

from . import intake_tools


def send_email(recipient: str, subject: str, body: str) -> dict[str, Any]:
    """Send a plain-text email via the Gmail API.

    Returns ``{"sent": bool, "message_id": str | None, "error": str | None}``.
    Never raises — failures are captured so the approval flow can record the
    send status without losing the human decision.
    """
    try:
        message = MIMEText(body or "")
        message["to"] = recipient
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = (
            intake_tools._gmail()
            .users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return {"sent": True, "message_id": sent.get("id"), "error": None}
    except Exception as exc:  # noqa: BLE001 — surfacing, not crashing
        return {"sent": False, "message_id": None, "error": f"{type(exc).__name__}: {exc}"}
