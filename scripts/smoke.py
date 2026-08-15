"""Local Phase 1 smoke test.

Proves the Phase 1 contract end-to-end without deploying:
  1. the `reconciler` agent package imports cleanly,
  2. the Supervisor is a valid LlmAgent bound to `gemini-2.5-flash`,
  3. a run trigger yields a structured ack from Gemini (via Vertex AI using the
     runtime SA credentials), and
  4. the reply parses as JSON of the documented shape.

Run (note the Vertex-AI env vars + SA creds; the same env vars are set on the
Cloud Run service via `--set-env-vars`):

  GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/reconciler-sa.json \
  GOOGLE_GENAI_USE_VERTEXAI=1 \
  GOOGLE_CLOUD_PROJECT=reconciler-mohammed-emad \
  GOOGLE_CLOUD_LOCATION=us-central1 \
  uv run python scripts/smoke.py

Exit code 0 == smoke passed. This is a real call to Vertex AI; it costs a few
tokens (~$0.001).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid


FAIL = "\033[31m"
OK = "\033[32m"
RESET = "\033[0m"


def _check_env() -> None:
    missing = [
        v
        for v in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
        )
        if not os.environ.get(v)
    ]
    # google-genai reads GOOGLEGENAI_USE_VERTEXAI OR GOOGLE_GENAI_USE_VERTEXAI.
    # google-genai Client honors exactly this name (underline form), per
    # google/genai/_api_client.py:654. The wrong spelling silently falls through
    # to AI Studio, so we check the one that actually works.
    if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
        missing.append("GOOGLE_GENAI_USE_VERTEXAI")
    if missing:
        print(
            f"{FAIL}smoke: missing env: {', '.join(missing)}{RESET}\n"
            "See the docstring at the top of this file.",
            file=sys.stderr,
        )
        raise SystemExit(2)


async def _run() -> str:
    # Import after env validation so a misconfig fails fast with a clear msg.
    from google.adk import Agent  # noqa: F401  (asserts adk importable)
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    # Import the agent by package name (same way the ADK loader does it).
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "agents"))
    from reconciler import root_agent  # type: ignore
    from reconciler import config

    assert isinstance(root_agent, Agent), "root_agent is not an ADK Agent"
    assert root_agent.model == config.GEMINI_MODEL, (
        f"model mismatch: {root_agent.model!r} != {config.GEMINI_MODEL!r}"
    )
    print(f"{OK}agent ok: name={root_agent.name} model={root_agent.model}{RESET}")

    app_name = config.APP_NAME
    runner = InMemoryRunner(agent=root_agent, app_name=app_name)
    # InMemoryRunner owns its own InMemorySessionService — create the session on
    # THAT instance (a separate InMemorySessionService() would never be seen).
    user_id = "smoke"
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id
    )
    run_id = uuid.uuid4().hex[:8]
    new_message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(
                    f'Run trigger received. run_id="{run_id}". '
                    "Produce the structured ack/plan JSON now."
                )
            )
        ],
    )

    final_text: str | None = None
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=new_message,
    ):
        if event.is_final_response():
            final_text = (event.content and event.content.parts and
                          event.content.parts[0].text) or None

    if not final_text:
        print(f"{FAIL}smoke: no final response text{RESET}", file=sys.stderr)
        raise SystemExit(1)

    print(f"{OK}raw reply:{RESET}\n{final_text}")
    # The reply must be a single JSON object with status=ack and a plan list.
    text = final_text.strip().removeprefix("```json").removeprefix("```").strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            f"{FAIL}smoke: reply was not valid JSON ({exc}){RESET}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if parsed.get("status") != "ack" or not isinstance(parsed.get("plan"), list):
        print(
            f"{FAIL}smoke: reply shape unexpected: {parsed!r}{RESET}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(
        f"{OK}smoke PASS: status={parsed['status']!r} run_id={parsed.get('run_id')!r} "
        f"plan={parsed['plan']}{RESET}"
    )
    return "ok"


def main() -> None:
    _check_env()
    asyncio.run(_run())


if __name__ == "__main__":
    main()