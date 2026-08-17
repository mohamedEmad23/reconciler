"""Intake specialist — discovers invoice documents (Gmail OAuth via Secret
Manager, or a local directory for the reproducible demo path).

The heavy lifting lives in ``tools/intake_tools.py`` (credential isolation:
the agent layer never touches raw OAuth material). This agent exposes those
tools to the Supervisor for interactive use; the batch pipeline calls the
tool functions directly — deterministic work should not burn model tokens.
"""

from google.adk import Agent
from google.genai import types

from . import config
from .instruction_contract import specialist_instruction
from .schemas import IntakeResult
from .tools.intake_tools import fetch_invoice_pdf, list_invoice_emails, list_local_invoices

intake_agent = Agent(
    name="intake",
    model=config.GEMINI_MODEL,
    instruction=specialist_instruction(
        goal="discover invoice documents and fetch their PDF bytes",
        inputs=(
            "a request naming the source: 'gmail' (list_invoice_emails then "
            "fetch_invoice_pdf per message) or 'local_dir' (list_local_invoices)"
        ),
        output_description=(
            "IntakeResult JSON — invoices found this run with source, ids, "
            "filenames, sha256 hashes; fetch failures go to errors[] and NEVER "
            "crash the run"
        ),
    ),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[list_invoice_emails, fetch_invoice_pdf, list_local_invoices],
    mode="single_turn",
    output_key="intake_last_reply",
)
