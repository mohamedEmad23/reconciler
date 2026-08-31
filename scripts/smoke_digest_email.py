"""Smoke: the digest email — benign run-summary composition + send (no LLM, no network).

Proves the "agent reports its results" beat without sending real mail: we stub
``send_email`` and the SMTP secret cache, call ``send_run_summary`` with sample
run stats, and assert the composed email carries every fact a human needs and
that it routes to the redirect target (demo inbox). No Vertex calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from reconciler.tools import email_tools  # noqa: E402

calls: list[dict] = []


def _stub_send(recipient: str, subject: str, body: str) -> dict:
    calls.append({"recipient": recipient, "subject": subject, "body": body})
    return {"sent": True, "message_id": None, "error": None}


def main() -> None:
    # Patch the SMTP secret cache so _smtp_config() never hits Secret Manager,
    # and patch send_email so nothing hits the network.
    email_tools._smtp = {
        "sender": "operator@example.com",
        "password": "not-a-real-password",
        "redirect_to": "demo-inbox@example.com",
    }
    email_tools.send_email = _stub_send  # type: ignore[assignment]

    result = email_tools.send_run_summary(
        run_id="run_abc123",
        job_type="weekly_reconcile",
        invoices_total=2,
        invoices_completed=2,
        invoices_failed=0,
        flagged_count=1,
        dollars_at_risk=2400.0,
        dollars_recovered=0.0,
    )

    assert result == {"sent": True, "message_id": None, "error": None}, result
    assert len(calls) == 1, calls
    c = calls[0]

    # Routes to the demo redirect target (not the operator).
    assert c["recipient"] == "demo-inbox@example.com", c["recipient"]
    assert "weekly_reconcile" in c["subject"], c["subject"]
    assert "1 flagged" in c["subject"], c["subject"]

    # Every fact a human needs is present.
    assert "run_abc123" in c["body"]
    assert "invoices processed  : 2/2" in c["body"]
    assert "matched (no action) : 1" in c["body"]
    assert "flagged for review  : 1" in c["body"]
    assert "$2,400.00" in c["body"]  # dollars_at_risk
    assert "$0.00" in c["body"]  # dollars_recovered
    assert "Reconciler autonomous weekly_reconcile run complete." in c["body"]
    # Never acts on money without approval — the disclaimer is present.
    assert "never acts on money" in c["body"] or "explicit approval" in c["body"]

    # Error path: a failing send is captured, never raised.
    def _boom(_r: str, _s: str, _b: str) -> dict:
        raise RuntimeError("smtp down")

    email_tools.send_email = _boom  # type: ignore[assignment]
    failed = email_tools.send_run_summary(
        run_id="r2", job_type="weekly_reconcile", invoices_total=0,
        invoices_completed=0, invoices_failed=0, flagged_count=0,
        dollars_at_risk=0.0, dollars_recovered=0.0,
    )
    assert failed["sent"] is False, failed
    assert "RuntimeError" in failed["error"], failed

    print("smoke_digest_email PASS")


if __name__ == "__main__":
    main()
