"""Trigger a reconciler run and keep the instance warm past the OTel batch flush."""
import subprocess
import time

BASE = "https://reconciler-542923033636.us-central1.run.app"

tok = subprocess.run(
    ["gcloud", "auth", "print-identity-token"], capture_output=True, text=True, check=True
).stdout.strip()

subprocess.run(
    ["gcloud", "scheduler", "jobs", "run", "reconciler-weekly",
     "--location", "us-central1", "--project", "reconciler-mohammed-emad"],
    capture_output=True, text=True, check=True,
)
print("scheduler job triggered; keeping instance warm 25s for OTel batch flush...")
for i in range(25):
    subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} ",
         "-H", f"Authorization: Bearer {tok}", f"{BASE}/health"],
        capture_output=True, text=True,
    )
    time.sleep(1)
print("warm window done")
