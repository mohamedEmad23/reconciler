"""Intake tools — invoice discovery from Gmail (OAuth via Secret Manager) or a
local directory (fixtures / reproducible demo path).

Credential isolation (design §8): the agent layer never holds raw OAuth
material — these tool functions are the ONLY code that touches Secret Manager
and mint google.oauth2 credentials. The agent/pipeline receives plain data.

Every public function is defensive: fetch failures are captured in an
``errors`` list and never crash the run (a failing invoice goes to errors /
later DLQ, the run continues).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .. import config

# --- Secret Manager → OAuth credentials (lazy, cached) -----------------------

_creds: Any = None
_gmail_service: Any = None


def _oauth_credentials() -> Any:
    """Build Gmail-scoped user credentials from the Secret Manager payload.

    The secret ``reconciler-oauth-config`` holds the minted token JSON
    (refresh_token + client_id + client_secret). Refresh happens inline when
    expired; nothing is ever written to disk.
    """
    global _creds
    if _creds is not None:
        return _creds
    from google.cloud import secretmanager
    from google.oauth2 import credentials as oauth_credentials

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{config.GCP_PROJECT}/secrets/{config.SECRET_OAUTH_CONFIG}/versions/latest"
    payload = client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")
    tok = json.loads(payload)
    _creds = oauth_credentials.Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        client_id=tok.get("client_id"),
        client_secret=tok.get("client_secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=tok.get("scopes"),
    )
    return _creds


def _gmail() -> Any:
    """Authenticated gmail.v1 resource (lazy)."""
    global _gmail_service
    if _gmail_service is None:
        from googleapiclient.discovery import build

        _gmail_service = build("gmail", "v1", credentials=_oauth_credentials(), cache_discovery=False)
    return _gmail_service


# --- Gmail source -------------------------------------------------------------


def list_invoice_emails(query: str = "has:attachment filename:pdf newer_than:7d") -> dict:
    """List recent Gmail messages that look like invoice PDFs.

    Returns ``{"messages": [{"message_id", "subject", "date"}], "errors": []}``.
    """
    try:
        resp = _gmail().users().messages().list(userId="me", q=query, maxResults=25).execute()
        out: list[dict[str, Any]] = []
        for m in resp.get("messages", []):
            meta = (
                _gmail().users()
                .messages()
                .get(userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
            out.append(
                {
                    "message_id": m["id"],
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                }
            )
        return {"messages": out, "errors": []}
    except Exception as exc:  # noqa: BLE001 — captured, run continues
        return {"messages": [], "errors": [f"gmail list failed: {exc!r}"]}


def fetch_invoice_pdf(message_id: str) -> dict:
    """Fetch the first PDF attachment of a Gmail message.

    Returns ``{"filename", "mime_type", "data_b64", "sha256", "errors": []}``.
    ``data_b64`` is the raw PDF bytes, base64-encoded for transport.
    """
    try:
        msg = _gmail().users().messages().get(userId="me", id=message_id).execute()
        for part in msg.get("payload", {}).get("parts", []):
            fn = part.get("filename", "")
            if fn.lower().endswith(".pdf") or part.get("mimeType") == "application/pdf":
                att_id = part["body"].get("attachmentId")
                if att_id:
                    att = (
                        _gmail().users()
                        .messages()
                        .attachments()
                        .get(userId="me", messageId=message_id, id=att_id)
                        .execute()
                    )
                    data = base64.urlsafe_b64decode(att["data"])
                else:
                    data = base64.urlsafe_b64decode(part["body"].get("data", ""))
                return {
                    "filename": fn or f"{message_id}.pdf",
                    "mime_type": "application/pdf",
                    "data_b64": base64.b64encode(data).decode("ascii"),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "errors": [],
                }
        return {"filename": None, "mime_type": None, "data_b64": None, "sha256": None,
                "errors": [f"no PDF attachment in {message_id}"]}
    except Exception as exc:  # noqa: BLE001
        return {"filename": None, "mime_type": None, "data_b64": None, "sha256": None,
                "errors": [f"gmail fetch {message_id} failed: {exc!r}"]}


# --- Local directory source (fixtures / reproducible demo) --------------------


def list_local_invoices(directory: str = "tests/fixtures") -> dict:
    """List invoice PDFs in a local directory (deterministic demo source).

    Returns the same shape as :func:`list_invoice_emails` plus ``pdfs`` with
    absolute paths, so the pipeline can read bytes without Gmail.
    """
    errors: list[str] = []
    pdfs: list[dict[str, Any]] = []
    root = Path(directory)
    if not root.is_absolute():
        root = Path(os.getcwd()) / root
    if not root.is_dir():
        return {"pdfs": [], "errors": [f"directory not found: {root}"]}
    for p in sorted(root.glob("*.pdf")):
        data = p.read_bytes()
        if not data.startswith(b"%PDF"):
            errors.append(f"skipped non-PDF: {p.name}")
            continue
        pdfs.append(
            {
                "filename": p.name,
                "mime_type": "application/pdf",
                "path": str(p),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return {"pdfs": pdfs, "errors": errors}


def read_local_pdf(path: str) -> bytes:
    """Read a local PDF's bytes (pipeline helper)."""
    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"not a PDF: {path}")
    return data
