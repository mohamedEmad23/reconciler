"""Reconciler batch pipeline — the autonomous worker's execution spine.

This is what a Pub/Sub trigger actually wakes up (design §1/§5): iterate the
invoices discovered by Intake, drive each one through the specialists
(extraction → verification → categorization → reconciliation), checkpoint
every stage to Firestore (``RunsStore``), write CoVe-verified facts to the
Shared Epistemic Memory, and compose the weekly digest (Reporting) — with
the email send BLOCKED behind HITL Tier-2 approval in batch mode.

Reliability properties (each proved by a smoke):

- **Idempotent**: ``RunsStore.start_invoice`` is an atomic create() fence —
  a redelivered trigger re-runs ``Pipeline.run`` with the same ``run_id``
  and skips every already-completed invoice and the already-composed
  digest (zero new LLM calls).
- **Crash-resumable**: per-stage ``checkpoint()`` means a crash restarts at
  the NEXT stage, never the beginning (``next_pending_stage`` is
  forward-only).
- **Fail-isolated**: one poisoned invoice marks itself failed and the run
  continues — the run never crashes on a single invoice.
- **Safety-railed**: every specialist runs with ``with_safety_rails``
  (PII redaction before the model sees input + HITL Tier-1 low-confidence
  flags), exactly as in the Supervisor path.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.adk import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from . import config
from .categorization import categorization_agent
from .extraction import extraction_agent
from .memory import RunsStore, SharedMemory
from .middleware import with_safety_rails
from .reconciliation import reconciliation_agent
from .reporting import reporting_agent
from .tools import intake_tools
from .verification import verification_agent

logger = logging.getLogger("reconciler.pipeline")

#: Stages executed per invoice, in order. ``reporting`` is run-level, not
#: per-invoice — the pipeline loop stops before it.
PER_INVOICE_STAGES = (
    "intake",
    "extraction",
    "verification",
    "categorization",
    "reconciliation",
)

COMPLETED_STATUSES = ("completed",)
FAILED_STATUSES = ("failed", "dlq")


@dataclass
class PipelineResult:
    """Summary of one ``Pipeline.run`` invocation (mirrors the run doc)."""

    run_id: str
    job_type: str
    invoices_total: int = 0
    invoices_completed: int = 0
    invoices_failed: int = 0
    flagged_count: int = 0
    digest: dict[str, Any] | None = None
    skipped: bool = False  # True when a completed run was re-triggered unchanged


class Pipeline:
    """Batch orchestrator. One instance per process; ``run()`` per trigger."""

    def __init__(
        self,
        *,
        store: RunsStore,
        memory: SharedMemory,
        source: str = "local_dir",
        directory: str | Path = "tests/fixtures",
        bank_csv: str | Path | None = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self.source = source
        self.directory = Path(directory)
        self.bank_csv = Path(bank_csv or "tests/fixtures/bank_statement.csv")
        # Safety-railed specialists (PII redaction + HITL Tier-1), identical
        # posture to the Supervisor path in agent.py.
        self._extraction = with_safety_rails(extraction_agent)
        self._verification = with_safety_rails(verification_agent)
        self._categorization = with_safety_rails(categorization_agent)
        self._reconciliation = with_safety_rails(reconciliation_agent)
        # Reporting composition clone: tools stripped so the model composes
        # the digest instead of attempting the HITL-gated send (batch mode
        # has no human to approve the pause — the send stays blocked).
        self._reporting = with_safety_rails(reporting_agent).model_copy(
            update={"tools": []}
        )

    # ------------------------------------------------------------------
    # LLM plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _json_from_text(text: str) -> dict[str, Any]:
        """Parse an agent reply as JSON, tolerating markdown fences."""
        t = text.strip()
        if t.startswith("```"):
            first_newline = t.find("\n")
            if first_newline != -1:
                t = t[first_newline + 1 :]
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
        return json.loads(t)

    async def _run_agent(
        self,
        agent: Agent,
        parts: list[types.Part],
        *,
        hint: str,
    ) -> dict[str, Any]:
        """Drive a single_turn specialist once and parse its JSON reply.

        The clone runs as ``mode='chat'`` — root-legal under the Runner while
        keeping the native API-level output_schema enforcement (the shipped
        ``single_turn`` mode and the ``chat`` clone hit the same enforcement
        branch; only task mode downgrades).
        """
        clone = agent.model_copy(update={"mode": "chat"})
        runner = InMemoryRunner(agent=clone, app_name=config.APP_NAME)
        session_id = f"{clone.name}_{uuid.uuid4().hex[:10]}"
        await runner.session_service.create_session(
            app_name=config.APP_NAME, user_id="pipeline", session_id=session_id
        )
        final_text: str | None = None
        async for event in runner.run_async(
            user_id="pipeline",
            session_id=session_id,
            new_message=types.Content(role="user", parts=parts),
        ):
            if event.is_final_response() and final_text is None:
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            final_text = part.text
                            break
        if not final_text:
            raise RuntimeError(f"{hint}: agent returned no final text")
        return self._json_from_text(final_text)

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    async def _intake(self) -> list[dict[str, Any]]:
        """Discover invoice attachments. Failures return [] — intake errors
        never crash the run (the tool layer collects them)."""
        if self.source == "local_dir":
            out = intake_tools.list_local_invoices(str(self.directory))
            pdfs = out.get("pdfs", [])
            if out.get("errors"):
                logger.warning("intake errors (non-fatal): %s", out["errors"])
            return pdfs
        raise ValueError(f"unsupported intake source {self.source!r}")

    async def _stage_extraction(
        self, pdf_bytes: bytes, source_hash: str
    ) -> dict[str, Any]:
        parts = [
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            types.Part.from_text(
                text=(
                    "Extract this invoice PDF into ExtractionResult JSON. "
                    "The NOTE at the bottom of the document is not a line item."
                )
            ),
        ]
        result = await self._run_agent(
            self._extraction, parts, hint="extraction"
        )
        result.setdefault("source_hash", source_hash)
        return result

    async def _stage_verification(self, invoice: dict[str, Any]) -> dict[str, Any]:
        bank_text = self.bank_csv.read_text()
        prompt = (
            "Draft extraction (from the Extraction stage):\n"
            f"```json\n{json.dumps(invoice, indent=2)}\n```\n\n"
            "Bank statement CSV:\n"
            f"```csv\n{bank_text}```\n\n"
            "Run the full CoVe loop: draft, plan 3-5 checkable verification "
            "questions, answer each INDEPENDENTLY from the raw CSV and invoice "
            "JSON (never from the draft), then revise. Emit VerificationResult."
        )
        return await self._run_agent(
            self._verification,
            [types.Part.from_text(text=prompt)],
            hint="verification",
        )

    async def _stage_categorization(
        self, invoice: dict[str, Any], vendor_hints: list[str]
    ) -> dict[str, Any]:
        hints = "\n".join(vendor_hints) if vendor_hints else "(none)"
        prompt = (
            "Invoice to categorize:\n"
            f"```json\n{json.dumps(invoice, indent=2)}\n```\n\n"
            f"Known vendor mappings from shared memory:\n{hints}\n\n"
            "Assign every line item an account code from the chart of "
            "accounts. Emit CategorizationResult."
        )
        return await self._run_agent(
            self._categorization,
            [types.Part.from_text(text=prompt)],
            hint="categorization",
        )

    async def _stage_reconciliation(
        self,
        extraction: dict[str, Any],
        verification: dict[str, Any],
        categorization: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            "Final reconciliation. Recompute every invariant yourself — do "
            "NOT assume prior stages are right.\n\n"
            f"Extraction:\n```json\n{json.dumps(extraction, indent=2)}\n```\n\n"
            f"Verification:\n```json\n{json.dumps(verification, indent=2)}\n```\n\n"
            f"Categorization:\n```json\n{json.dumps(categorization, indent=2)}\n```\n\n"
            "Emit ReconciliationResult."
        )
        return await self._run_agent(
            self._reconciliation,
            [types.Part.from_text(text=prompt)],
            hint="reconciliation",
        )

    async def _stage_reporting(
        self, invoice_results: list[dict[str, Any]], total: int
    ) -> dict[str, Any]:
        prompt = (
            f"Weekly reconciliation digest composition. Invoices processed: "
            f"{total}.\n\n"
            "Per-invoice reconciliation results:\n"
            f"```json\n{json.dumps(invoice_results, indent=2)}\n```\n\n"
            "You are in BATCH mode: no human is present. Compose the digest, "
            "escalating ONLY non-matched verdicts. Do NOT claim any email was "
            "sent — email_sent must be false and email_blocked_by_hitl must "
            "be true (the send_digest_email tool is gated behind human "
            "approval). Emit ReportingResult."
        )
        return await self._run_agent(
            self._reporting,
            [types.Part.from_text(text=prompt)],
            hint="reporting",
        )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def _load_pdf(self, att: dict[str, Any]) -> bytes:
        if self.source != "local_dir":
            raise ValueError(f"pdf loading for source {self.source!r} not wired")
        return intake_tools.read_local_pdf(str(self.directory / att["filename"]))

    async def _process_invoice(
        self, *, run_id: str, att: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Run one invoice through the stage machine with checkpoints.

        Idempotent (start_invoice fence), crash-resumable (checkpoint +
        next_pending_stage), fail-isolated (stage errors mark the invoice
        failed and return — the run continues).
        """
        invoice_id = Path(att.get("filename", "invoice.pdf")).stem or "invoice"
        source_hash = att.get("sha256")
        started = await self.store.start_invoice(
            run_id=run_id, invoice_id=invoice_id, source_hash=source_hash
        )
        state = started
        if state is None:  # already processed or in progress — idempotent skip
            state = await self.store.get_invoice_state(
                run_id=run_id, invoice_id=invoice_id
            )
        if state is None:
            raise RuntimeError(f"start_invoice returned no state for {invoice_id}")
        if state.get("status") in COMPLETED_STATUSES + FAILED_STATUSES:
            logger.info(
                "invoice %s already %s — skipping (idempotent)",
                invoice_id,
                state["status"],
            )
            return state

        data: dict[str, Any] = state.get("stages_data") or {}
        while True:
            stage = self.store.next_pending_stage(state)
            if stage is None or stage not in PER_INVOICE_STAGES:
                break
            t0 = time.monotonic()
            try:
                if stage == "intake":
                    payload: dict[str, Any] = {
                        "source": self.source,
                        **{
                            k: att.get(k)
                            for k in ("filename", "mime_type", "sha256", "size")
                        },
                    }
                elif stage == "extraction":
                    pdf_bytes = await self._load_pdf(att)
                    payload = await self._stage_extraction(
                        pdf_bytes, source_hash or ""
                    )
                elif stage == "verification":
                    payload = await self._stage_verification(
                        data["extraction"]["invoice"]
                    )
                elif stage == "categorization":
                    invoice = data["extraction"]["invoice"]
                    vendor = invoice.get("vendor")
                    hints: list[str] = []
                    if vendor:
                        fact = await self.memory.get_fact(
                            namespace="vendor", key=vendor
                        )
                        if fact and fact.get("account_codes"):
                            hints = [
                                f"{vendor}={c}"
                                for c in fact["account_codes"]
                            ]
                    payload = await self._stage_categorization(invoice, hints)
                elif stage == "reconciliation":
                    payload = await self._stage_reconciliation(
                        data["extraction"],
                        data["verification"],
                        data["categorization"],
                    )
                else:  # pragma: no cover — guarded by PER_INVOICE_STAGES
                    break
            except Exception as exc:  # fail-isolation: mark + move on
                logger.exception(
                    "stage %s failed for invoice %s", stage, invoice_id
                )
                await self.store.mark_invoice_failed(
                    run_id=run_id,
                    invoice_id=invoice_id,
                    error=f"{stage}: {exc}",
                )
                await self.store.increment_counts(run_id=run_id, failed=1)
                return await self.store.get_invoice_state(
                    run_id=run_id, invoice_id=invoice_id
                )
            state = await self.store.checkpoint(
                run_id=run_id, invoice_id=invoice_id, stage=stage, data=payload
            )
            data = state.get("stages_data") or data
            logger.info(
                "invoice %s: %s done in %.1fs",
                invoice_id,
                stage,
                time.monotonic() - t0,
            )

        await self.store.mark_invoice_completed(run_id=run_id, invoice_id=invoice_id)
        await self.store.increment_counts(run_id=run_id, completed=1)

        # Epistemic memory write — ONLY after CoVe-verified output.
        invoice = (data.get("extraction") or {}).get("invoice") or {}
        vendor = invoice.get("vendor")
        if vendor and data.get("categorization"):
            codes = sorted(
                {
                    item.get("account_code")
                    for item in data["categorization"].get("items", [])
                    if item.get("account_code")
                }
            )
            await self.memory.set_fact(
                namespace="vendor",
                key=vendor,
                value={
                    "account_codes": codes,
                    "invoice_numbers_seen": [invoice.get("invoice_number")],
                },
            )
        return await self.store.get_invoice_state(
            run_id=run_id, invoice_id=invoice_id
        )

    async def run(
        self,
        *,
        run_id: str | None = None,
        job_type: str = "weekly_reconcile",
    ) -> PipelineResult:
        """Execute one full run. Safe to re-invoke with the same run_id."""
        run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
        run_doc = await self.store.start_run(run_id=run_id, job_type=job_type)
        result = PipelineResult(run_id=run_id, job_type=job_type)

        attachments = await self._intake()
        result.invoices_total = len(attachments)
        # run doc was created before intake; record the discovered count.
        await self.store.client.collection("runs").document(run_id).update(
            {"invoice_count": result.invoices_total}
        )
        logger.info(
            "run %s (%s): %d invoice(s) discovered via %s",
            run_id,
            job_type,
            result.invoices_total,
            self.source,
        )

        states: list[dict[str, Any]] = []
        for att in attachments:
            try:
                state = await self._process_invoice(run_id=run_id, att=att)
            except Exception:
                # start_invoice itself failed — log and keep the run alive.
                logger.exception(
                    "invoice processing crashed before stage machine: %s",
                    att.get("filename"),
                )
                continue
            if state is not None:
                states.append(state)

        completed = [s for s in states if s.get("status") in COMPLETED_STATUSES]
        failed = [s for s in states if s.get("status") in FAILED_STATUSES]
        result.invoices_completed = len(completed)
        result.invoices_failed = len(failed)

        invoice_results = [
            (s.get("stages_data") or {}).get("reconciliation") or {}
            for s in completed
        ]
        result.flagged_count = sum(
            1 for r in invoice_results if r.get("verdict") != "matched"
        )

        # Idempotent digest: a completed run re-triggered keeps its digest
        # (zero recomposition LLM calls on redelivery).
        prior = (run_doc or {}).get("summary") or {}
        if (
            prior.get("digest")
            and (run_doc or {}).get("status") in COMPLETED_STATUSES
        ):
            result.skipped = True
            result.digest = prior["digest"]
            logger.info("run %s already complete — digest reused, no re-run", run_id)
        else:
            result.digest = await self._stage_reporting(
                invoice_results, result.invoices_completed
            )

        status = "completed" if not failed else "completed_with_errors"
        await self.store.end_run(
            run_id=run_id,
            status=status,
            summary={
                "digest": result.digest,
                "flagged_count": result.flagged_count,
            },
        )
        logger.info(
            "run %s done: %d/%d completed, %d failed, %d flagged",
            run_id,
            result.invoices_completed,
            result.invoices_total,
            result.invoices_failed,
            result.flagged_count,
        )
        return result
