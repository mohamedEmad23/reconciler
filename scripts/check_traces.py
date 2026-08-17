"""Phase 7 verification: confirm OTel spans landed in Cloud Trace.

Cloud Trace v1 REST API notes (learned the hard way):
- param is `pageSize` (NOT `limit` — INVALID_ARGUMENT on v1)
- `view=COMPLETE` is required to include spans (default view = summary)
- `startTime` filter (RFC3339) is required, else pages come back empty
- v1 spans carry `name` directly (NOT `displayName.value`, which is v2 shape)
- ADK/OTLP spans (invoke_agent, call_llm, generate_content) export under
  their OWN traceIds — the console Grouped view stitches the hierarchy.
"""
import datetime as dt
import json
import subprocess
import sys
from urllib.parse import quote

PROJECT = "reconciler-mohammed-emad"
WINDOW_HOURS = 6

TOKEN = subprocess.run(
    ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True
).stdout.strip()

since = (
    dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=WINDOW_HOURS)
).strftime("%Y-%m-%dT%H:%M:%SZ")

ADK_MARKERS = ("invoke_agent", "call_llm", "generate_content", "invocation")

def fetch(page_token: str | None = None) -> dict:
    url = (
        f"https://cloudtrace.googleapis.com/v1/projects/{PROJECT}/traces"
        f"?pageSize=50&view=COMPLETE&startTime={since}"
    )
    if page_token:
        url += f"&pageToken={quote(page_token, safe='')}"
    raw = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: Bearer {TOKEN}", url],
        capture_output=True, text=True, check=True,
    ).stdout
    d = json.loads(raw)
    if "error" in d:
        print(f"API error: {d['error'].get('message', d['error'])}")
        sys.exit(1)
    return d

traces: list[dict] = []
token: str | None = None
for _ in range(3):  # follow up to 3 pages
    d = fetch(token)
    traces.extend(d.get("traces", []))
    token = d.get("nextPageToken")
    if not token:
        break

print(f"traces found (last {WINDOW_HOURS}h): {len(traces)}")
span_names: list[str] = []
for t in traces:
    spans = t.get("spans", [])
    names = [s.get("name", "?") for s in spans]
    span_names.extend(names)
    started = spans[0].get("startTime", "?") if spans else "?"
    print(f"  traceId={t['traceId'][:20]}... spans={len(spans)} start={started}")
    print(f"    spans: {', '.join(names[:8])}")

if not traces:
    print("NO TRACES — telemetry exporter not delivering")
    sys.exit(1)

adk_hits = [n for n in span_names if any(m in (n or "") for m in ADK_MARKERS)]
print(f"\nADK/LLM spans in window: {len(adk_hits)} "
      f"({', '.join(sorted(set(adk_hits))[:5]) if adk_hits else 'none yet — trigger the scheduler job'})")
print("cloud trace verification:",
      "OK — ADK spans visible" if adk_hits else "partial — HTTP spans present, trigger job for LLM spans")
