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
      "google-adk[a2a]==2.7.0" \
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

# Copy ONLY the agent package. No .venv, no docs, no tests, no creds.
COPY agents/ /app/agents/

# Non-root runtime.
RUN useradd -m -u 1000 myuser && chown -R myuser:myuser /app
USER myuser

EXPOSE 8080
ENV PORT=8080 \
    HOST=0.0.0.0

# ADK API server.
#   --no_use_local_storage      -> in-memory session/artifact (stateless)
#   --trigger_sources=pubsub   -> registers POST /trigger/pubsub (Cloud
#                                 Scheduler -> Pub/Sub push subscription
#                                 delivers run triggers here)
#   --trace_to_cloud            -> exports OTel spans to Cloud Trace
#   --otel_to_cloud             -> exports OTel metrics/logs to Cloud Monitoring
CMD ["adk", "api_server", \
     "--host=0.0.0.0", "--port=8080", \
     "--no_use_local_storage", \
     "--trigger_sources=pubsub", \
     "--trace_to_cloud", \
     "--otel_to_cloud", \
     "--log_level=info", \
     "/app/agents/reconciler"]