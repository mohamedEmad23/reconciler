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
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ---- Model — the one config constant ---------------------------------------
# ``gemini-2.5-flash`` is the LIVE callable model on us-central1 via Vertex AI
# (probed 2025-08-15 with the runtime SA). It qualifies under the rubric's
# "Gemini 3.5 Flash (or newer)" / "any Gemini 3.x" fallback. Swap here only.
GEMINI_MODEL: str = os.environ.get("RECONCILER_GEMINI_MODEL", "gemini-2.5-flash")

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