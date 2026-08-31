"""Reconciler single-source-of-truth config.

All environment-specific values resolve to constants defined here. The Gemini
model is ONE constant; swapping it (or pointing at a newer Gemini) is a one-line
change in this file (or the ``RECONCILER_GEMINI_MODEL`` env var).

Vertex AI routing is driven by the runtime environment, not hard-coded creds:
  google-genai ``Client`` honors ``GOOGLE_GENAI_USE_VERTEXAI=1``,
  ``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION`` and uses Application
  Default Credentials. On Cloud Run the runtime service-account metadata server
  supplies those creds automatically (no key file in the image). Locally we set
  ``GOOGLE_APPLICATION_CREDENTIALS`` to the SA key. NONE of these credentials
  ever live in the repo or the Docker image.

IMPORTANT — Gemini 3.5 model location:
  The Gemini 3.5 family (gemini-3.5-flash / -lite / -pro) is NOT served on
  regional Vertex AI endpoints (all return 404 NOT_FOUND on us-central1 and
  every other region). It IS served on the GLOBAL Agent-Platform endpoint.
  Therefore ``GOOGLE_CLOUD_LOCATION`` MUST be ``global`` (not us-central1) for
  the model. This is model-routing ONLY — the infra region (GCP_REGION below)
  stays us-central1 for Firestore/Pub/Sub/Secret Manager, none of which read
  ``GOOGLE_CLOUD_LOCATION``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ---- Model — the one config constant ---------------------------------------
# ``gemini-3.5-flash`` is the rubric's exact target ("Gemini 3.5 Flash"). It is
# callable ONLY via the global endpoint (GOOGLE_CLOUD_LOCATION=global), verified
# 2026-08-31 with the runtime SA. ``gemini-3.5-flash-lite`` is the cheaper
# fallback (same global endpoint). Swap here only.
GEMINI_MODEL: str = os.environ.get("RECONCILER_GEMINI_MODEL", "gemini-3.5-flash")

# ---- GCP project / region ---------------------------------------------------
GCP_PROJECT: str = os.environ.get(
    "RECONCILER_GCP_PROJECT", "reconciler-mohammed-emad"
)
GCP_REGION: str = os.environ.get("RECONCILER_GCP_REGION", "us-central1")

# ---- Runtime service account (Cloud Run identity) ---------------------------
RUNTIME_SA: str = os.environ.get(
    "RECONCILER_RUNTIME_SA",
    "reconciler-sa@reconciler-mohammed-emad.iam.gserviceaccount.com",
)

# ---- Secret Manager ---------------------------------------------------------
# Holds the Gmail/Drive OAuth refresh-token JSON. Verified working in
# tests/test_gmail.py. Agents never read this directly — only the middleware
# layer touches the secret store (design §8 credential isolation).
SECRET_OAUTH_CONFIG: str = "reconciler-oauth-config"
# Gmail SMTP app-password config (sender / password / optional redirect_to).
# The resolution agent never touches this — only the approval surface sends.
SECRET_SMTP_CONFIG: str = "reconciler-smtp-config"

# ---- Pub/Sub topics (created in Phase 5) ------------------------------------
TOPIC_TRIGGER: str = "reconciler.trigger"   # Cloud Scheduler publishes here
TOPIC_DLQ: str = "reconciler.dlq"          # poisoned invoices (never crash run)

# ---- Cloud Scheduler (the autonomous cron trigger) ---------------------------
SCHEDULER_JOB: str = "reconciler-weekly"    # the cron job that wakes the agent
SCHEDULER_SCHEDULE: str = "0 8 * * 1"       # every Monday 08:00 UTC

# ---- Firestore (created in Phase 4) ----------------------------------------
FIRESTORE_DATABASE: str = "(default)"

# ---- Agent identity ---------------------------------------------------------
APP_NAME: str = "reconciler"

# ---- Live service URL -------------------------------------------------------
# The Cloud Run .run.app URL uses the project NUMBER (not name). Shown in the
# digest email + dashboard so a human knows where to review flagged items.
SERVICE_URL: str = os.environ.get(
    "RECONCILER_SERVICE_URL",
    "https://reconciler-542923033636.us-central1.run.app",
)


@dataclass(frozen=True)
class RuntimeConfig:
    """Frozen snapshot of the resolved config at boot — used by agents/middleware.

    Reading config through this object makes the wiring explicit and prevents
    accidental mutation of the single config constant mid-run.
    """

    model: str
    project: str
    region: str
    runtime_sa: str
    app_name: str


def runtime_config() -> RuntimeConfig:
    """Build an immutable snapshot of the current runtime configuration."""
    return RuntimeConfig(
        model=GEMINI_MODEL,
        project=GCP_PROJECT,
        region=GCP_REGION,
        runtime_sa=RUNTIME_SA,
        app_name=APP_NAME,
    )