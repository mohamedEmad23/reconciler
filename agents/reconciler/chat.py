"""Chat assistant — answers human questions from structured memory.

This is the "ask the agent what it did" surface. Unlike a chatbot with vector
recall, this agent answers by CALLING read-only tools that query the auditable
Firestore record (runs, per-invoice state, learned facts, pending disputes).
Every answer is grounded in the provenanced record, not a retrieval guess —
which is the whole point of the "agentic memory vs chatbot recall" contrast.

The agent lives OUTSIDE the batch pipeline: it is a standalone, chat-mode
LlmAgent used only by the /chat route. It never writes to Firestore and never
touches money.
"""

from __future__ import annotations

from typing import Any

from google.adk import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from . import config
from .tools import query_tools

_CHAT_INSTRUCTION = (
    "You are the Reconciler assistant. A human is asking what the autonomous "
    "invoice-reconciliation agent did. Answer ONLY from the tools you are "
    "given — the structured, provenanced record in Firestore (runs, invoices, "
    "learned facts, pending disputes). Never invent numbers; if a tool returns "
    "nothing, or the data you need is absent, say so plainly instead of "
    "guessing.\n"
    "\n"
    "How to answer:\n"
    "- For 'what was processed this week / recently', call list_runs, then "
    "list_invoices for the run(s) you care about.\n"
    "- For 'what is awaiting approval / what is at risk', call list_disputes.\n"
    "- For 'what has the agent learned', call list_facts.\n"
    "\n"
    "Then summarize in plain prose for the human: how many invoices, how many "
    "matched, how many were flagged, which are awaiting approval, and any "
    "dollars at risk or recovered. Cite the specific vendors, invoice numbers, "
    "and amounts you found. Be concise and direct. Never speculate beyond the "
    "record, and never claim a dollar amount you did not read from a tool."
)

chat_agent = Agent(
    name="chat_assistant",
    model=config.GEMINI_MODEL,
    instruction=_CHAT_INSTRUCTION,
    tools=[
        query_tools.list_runs,
        query_tools.list_invoices,
        query_tools.list_facts,
        query_tools.list_disputes,
    ],
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    mode="chat",
)


async def ask_question(question: str, session_id: str = "chat-session") -> str:
    """Run the chat agent for one question; return its final text answer.

    Uses a fresh InMemoryRunner + session per call (stateless Q&A). The runner
    drives the full tool-use loop: the agent calls list_runs/list_invoices/...
    then composes its answer grounded in the returned JSON.
    """
    runner = InMemoryRunner(agent=chat_agent, app_name=config.APP_NAME)
    await runner.session_service.create_session(
        app_name=config.APP_NAME,
        user_id="chat",
        session_id=session_id,
    )
    final_text: str | None = None
    async for event in runner.run_async(
        user_id="chat",
        session_id=session_id,
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(text=question)],
        ),
    ):
        if event.is_final_response() and final_text is None:
            parts = event.content.parts if event.content else []
            final_text = "\n".join(p.text for p in parts if p.text) or None
    return final_text or "I couldn't find an answer in the recorded data."
