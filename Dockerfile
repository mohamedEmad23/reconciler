# Reconciler Cloud Run image.
# Hand-written (rather than the `adk deploy cloud_run` generated one) so we:
#   - control the Python base (CLI uses 3.11; we keep 3.12 for parity with dev),
#   - install the FULL pyproject dependency set now (so Firestore/Pub/Sub/Secret
#     Manager/Gmail/Drive clients are present when their phases land — no rebuild
#     churn), and
#   - match design §11 spin-up: `gcloud run deploy reconciler --source .`.
# Secrets NEVER live in this image — only the runtime SA (metadata server) +
# Secret Manager (accessed at request time) supply credentials.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Force stateless in-memory session/artifact services for Phase 1.
    # Phase 4 swaps to --session_service_uri=firestore://... in the CMD.
    ADK_DISABLE_LOCAL_STORAGE=1

WORKDIR /app

# Dependencies. `google-adk[a2a]` pulls google-genai transitively. The GCP
# clients below cover every later phase so the image is forward-compatible.
# `opentelemetry-exporter-otlp` is REQUIRED for --trace_to_cloud/--otel_to_cloud
# (without it ADK's GCP telemetry setup crashes on boot — verified in Phase 1).
# Keep versions aligned with pyproject.toml.
RUN pip install \
      "google-adk[a2a,otel-gcp]==2.7.0" \
      "google-api-python-client>=2.198.0" \
      "google-auth-httplib2>=0.4.1" \
      "google-auth-oauthlib>=1.4.0" \
      "google-cloud-aiplatform>=1.164.0" \
      "google-cloud-firestore>=2.28.1" \
      "google-cloud-pubsub>=2.39.1" \
      "google-cloud-scheduler>=2.20.0" \
      "google-cloud-secret-manager>=2.30.0" \
      "google-genai>=2.18.1" \
      "opentelemetry-exporter-otlp>=1.20.0" \
      "opentelemetry-exporter-gcp-logging>=1.9.0a0,<=1.12.0a0" \
      "opentelemetry-exporter-gcp-monitoring>=1.9.0a0,<2" \
      "opentelemetry-exporter-gcp-trace>=1.9,<2" \
      "opentelemetry-resourcedetector-gcp>=1.9.0a0,<2"

# Copy ONLY the agent package + the invoice/bank fixtures the batch pipeline
# reads (tests/fixtures = clean set, tests/fixtures_duplicate = the $2,400
# duplicate-payment money moment). No .venv, no docs, no scripts, no creds.
COPY agents/ /app/agents/
COPY tests/fixtures/ /app/tests/fixtures/
COPY tests/fixtures_duplicate/ /app/tests/fixtures_duplicate/

# Pure-FastAPI production surface (P13): the reconciler package must import.
ENV PYTHONPATH=/app/agents

# Non-root runtime.
RUN useradd -m -u 1000 myuser && chown -R myuser:myuser /app
USER myuser

EXPOSE 8080
ENV PORT=8080 \
    HOST=0.0.0.0

# Reconciler server — exactly five routes, NO ADK chat surface:
#   GET  /health, GET /, GET /approvals (HITL Tier-2 face),
#   POST /trigger/pubsub (Pub/Sub push -> batch Pipeline, idempotent),
#   POST /approvals/{run}/{invoice}/decision (approve & send | reject).
# OTel exporters initialise inside the app lifespan (Cloud Trace intact).
CMD ["python", "-m", "reconciler.server"]