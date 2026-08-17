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

import csv
import difflib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.adk import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from . import config
from .categorization import categorization_agent
from .extraction import extraction_agent
from .memory import RunsStore, SharedMemory
from .middleware import CONFIDENCE_THRESHOLD, with_safety_rails
from .reconciliation import reconciliation_agent
from .reporting import reporting_agent
from .resolution import resolution_agent
from .tools import intake_tools
from .verification import verification_agent

logger = logging.getLogger("reconciler.pipeline")

#: Stages executed per invoice, in order. ``reporting`` is run-level, not
#: per-invoice — the pipeline loop stops before it.
PER_INVOICE_STAGES = (
    "intake",
    "extraction",
    "verification",
    "resolution",
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
    # Closed-loop money metrics (anti-gaming §2/§9): dollars_recovered counts
    # ONLY approved+re-verified outcomes (stays 0 until the P13 approval
    # surface exists); dollars_at_risk is drafted-but-unapproved visibility.
    dollars_recovered: float = 0.0
    dollars_at_risk: float = 0.0


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
        self._resolution = with_safety_rails(resolution_agent)
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

    # ------------------------------------------------------------------
    # Closed-loop resolution (design doc §1) — deterministic evidence is
    # computed in PYTHON; the agent DECIDES + DRAFTS; the pipeline EXECUTES
    # + RE-VERIFIES. The agent never mutates data or sends anything.
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return None

    async def _evidence_packet(
        self, invoice: dict[str, Any], verification: dict[str, Any]
    ) -> dict[str, Any]:
        """Deterministic, auditable evidence for the resolution decision.

        Everything here is recomputed from raw inputs in Python (no LLM):
        fuzzy scores, memory facts, day deltas. These become the
        ``rule_fired`` strings in the provenance trail (e.g.
        'fuzzy_match(vendor, bank_row) @ 0.87').
        """
        bank_text = self.bank_csv.read_text()
        rows: list[dict[str, Any]] = []
        for line in bank_text.splitlines()[1:]:
            if not line.strip():
                continue
            try:
                date, desc, amount, *_ = next(csv.reader([line]))
            except (StopIteration, csv.Error):
                continue
            rows.append(
                {"date": date.strip(), "description": desc.strip(), "amount": amount.strip()}
            )

        packet: dict[str, Any] = {
            "bank_rows": rows,
            "vendor_alias_fact": None,
            "prior_invoice_fact": None,
            "best_vendor_row": None,
            "number_fuzzy": None,
            "date_delta_days": None,
            "amount_rows": [],
        }

        vendor = invoice.get("vendor")
        inv_no = invoice.get("invoice_number")
        inv_date = self._parse_date(invoice.get("invoice_date"))

        # Shared-memory facts (miss → None is the anti-hallucination signal).
        if vendor:
            packet["vendor_alias_fact"] = await self.memory.get_fact(
                namespace="vendor", key=vendor
            )
        if inv_no:
            packet["prior_invoice_fact"] = await self.memory.get_fact(
                namespace="prior_invoice", key=inv_no
            )

        # Best fuzzy vendor↔row match: max(difflib ratio, vendor-token overlap).
        # Token overlap = fraction of vendor name tokens present in the row —
        # the honest number when the vendor name is a substring of the memo.
        if vendor:
            vendor_tokens = [t for t in re.split(r"\W+", vendor.lower()) if len(t) > 2]
            best_row, best_score = None, 0.0
            for row in rows:
                ratio = difflib.SequenceMatcher(
                    None, vendor.lower(), row["description"].lower()
                ).ratio()
                row_tokens = set(re.split(r"\W+", row["description"].lower()))
                overlap = (
                    sum(1 for t in vendor_tokens if t in row_tokens) / len(vendor_tokens)
                    if vendor_tokens
                    else 0.0
                )
                score = max(ratio, overlap)
                if score > best_score:
                    best_row, best_score = row, score
            if best_row is not None:
                packet["best_vendor_row"] = {**best_row, "fuzzy": round(best_score, 2)}
                # Invoice-number fuzzy match inside that row (OCR 0↔O, 1↔l).
                if inv_no:
                    tokens = re.findall(r"[A-Za-z0-9-]{4,}", best_row["description"])
                    token_scores = {
                        t: round(
                            difflib.SequenceMatcher(
                                None, str(inv_no).lower(), t.lower()
                            ).ratio(),
                            2,
                        )
                        for t in tokens
                    }
                    top = max(token_scores.items(), key=lambda kv: kv[1]) if token_scores else None
                    if top:
                        packet["number_fuzzy"] = {"token": top[0], "score": top[1]}
                # Invoice-date vs bank-posting-date delta (1-3d is normal latency).
                row_date = self._parse_date(best_row.get("date"))
                if inv_date and row_date:
                    packet["date_delta_days"] = (row_date - inv_date).days
                # Amount rows near the invoice total (±$0.02 exact; transposition check).
                try:
                    inv_total = float(invoice.get("total") or 0)
                except (TypeError, ValueError):
                    inv_total = 0.0
                for row in rows:
                    try:
                        amt = abs(float(row["amount"]))
                    except ValueError:
                        continue
                    digits_a = sorted(re.sub(r"[^0-9]", "", f"{inv_total:.2f}"))
                    digits_b = sorted(re.sub(r"[^0-9]", "", f"{amt:.2f}"))
                    transposition = (
                        bool(inv_total)
                        and amt != inv_total
                        and digits_a == digits_b
                    )
                    packet["amount_rows"].append(
                        {
                            **row,
                            "abs_amount": amt,
                            "exact": abs(amt - inv_total) <= 0.02 if inv_total else None,
                            "digit_transposition": transposition,
                        }
                    )
        return packet

    async def _stage_resolution(
        self,
        extraction: dict[str, Any],
        verification: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            "A discrepancy was flagged during verification. Decide the "
            "resolution lane and produce the action artifact.\n\n"
            f"Discrepancies flagged by CoVe verification:\n"
            f"```json\n{json.dumps(verification.get('discrepancies', []), indent=2)}\n```\n"
            f"Verification confidence: {verification.get('confidence')}\n\n"
            f"CoVe trace (questions/answers):\n"
            f"{json.dumps(list(zip(verification.get('verification_questions', []), verification.get('verification_answers', []))), indent=2)}\n\n"
            f"Extracted invoice:\n```json\n{json.dumps(extraction.get('invoice', {}), indent=2)}\n```\n\n"
            f"DETERMINISTIC EVIDENCE PACKET (computed in Python from the raw "
            f"bank CSV + shared memory — the only evidence that exists):\n"
            f"```json\n{json.dumps(evidence, indent=2)}\n```\n\n"
            "Apply the decision table. Emit ResolutionAction."
        )
        return await self._run_agent(
            self._resolution,
            [types.Part.from_text(text=prompt)],
            hint="resolution",
        )

    async def _close_resolution(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute the decided lane and CLOSE THE LOOP by re-verification.

        Anti-gaming guard (design doc §1.6/§9): ``resolved`` is only ever
        set when an INDEPENDENT re-verification pass confirms the target
        discrepancy is gone. A failed re-check demotes to ``escalated`` —
        the resolver can never self-certify.
        """
        decision = payload.get("decision") or {}
        lane = decision.get("lane")
        target_type = decision.get("discrepancy_type")

        if lane == "resolve" and payload.get("corrected_invoice"):
            corrected = payload["corrected_invoice"]
            recheck = await self._stage_verification(corrected)
            payload["recheck"] = recheck
            payload["recheck_matched"] = bool(recheck.get("matched"))
            discrepancies = recheck.get("discrepancies") or []
            gone = all(d.get("type") != target_type for d in discrepancies)
            payload["outcome"] = (
                "resolved"
                if (recheck.get("matched") or gone)
                else "escalated"
            )
        elif lane == "dispute":
            # Draft only — the human approval surface (P13) is the ONLY
            # component that can commit the send. dollars_recovered is NOT
            # incremented here (anti-gaming §2: approved + re-verified only).
            payload["outcome"] = "disputed"
        else:
            payload["outcome"] = "escalated"
        return payload

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
        resolution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolution_block = (
            f"Resolution (closed loop — note the outcome; discrepancies the "
            f"resolver fixed are already applied to the invoice above):\n"
            f"```json\n{json.dumps(resolution, indent=2)}\n```\n\n"
            if resolution
            else ""
        )
        prompt = (
            "Final reconciliation. Recompute every invariant yourself — do "
            "NOT assume prior stages are right.\n\n"
            f"Extraction:\n```json\n{json.dumps(extraction, indent=2)}\n```\n\n"
            f"Verification:\n```json\n{json.dumps(verification, indent=2)}\n```\n\n"
            f"{resolution_block}"
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

    _LEASE_SECONDS = 600  # a worker holding an in_progress invoice for >10min
    #   without a checkpoint is presumed crashed — resume allowed.

    @staticmethod
    def _lease_expired(state: dict[str, Any]) -> bool:
        """True when an in_progress invoice is stale enough to resume.

        The lease is soft: ``updated_at`` is bumped by every checkpoint,
        so a live worker keeps it fresh; a crashed worker lets it go stale
        and the next delivery picks the invoice up mid-timeline.
        """
        updated = state.get("updated_at")
        if not isinstance(updated, datetime):
            return True  # unresolvable timestamp — prefer resumability
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age > Pipeline._LEASE_SECONDS

    @staticmethod
    def _python_recheck(
        rec: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        """Deterministic INV-1/INV-2 backstop — LLM arithmetic is not trusted.

        Recomputes sum(line_items) vs subtotal and subtotal+tax vs total in
        Python. If the arithmetic fails while the agent claimed ``matched``,
        the verdict is forced to ``needs_review`` (escalate, never trust).
        """
        inv = (data.get("extraction") or {}).get("invoice") or {}
        items = inv.get("line_items") or []
        sub, tax, tot = inv.get("subtotal"), inv.get("tax"), inv.get("total")
        if (
            not items
            or not isinstance(sub, (int, float))
            or not isinstance(tax, (int, float))
            or not isinstance(tot, (int, float))
        ):
            return rec
        s = sum(
            i.get("amount") or 0.0
            for i in items
            if isinstance(i.get("amount"), (int, float))
        )
        ok = abs(s - sub) <= 0.02 and abs(sub + tax - tot) <= 0.02
        if not ok and rec.get("verdict") == "matched":
            rec = dict(rec)
            rec["verdict"] = "needs_review"
            checked = list(rec.get("invariants_checked") or [])
            checked.append(
                "INV-PY-RECHECK: sum(line_items)/subtotal+tax mismatch in Python recompute"
            )
            rec["invariants_checked"] = checked
            rec["invariants_passed"] = False
        return rec

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
        if state is None:
            # Lost the create() fence — another worker owns this invoice.
            state = await self.store.get_invoice_state(
                run_id=run_id, invoice_id=invoice_id
            )
            if (
                state is not None
                and state.get("status") == "in_progress"
                and not self._lease_expired(state)
            ):
                # Concurrent redelivery: the winner is still actively
                # checkpointing (updated_at fresh). Skip instead of
                # double-executing stages (M1 race).
                logger.info(
                    "invoice %s in progress elsewhere (lease held) — skipping",
                    invoice_id,
                )
                return None
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
                elif stage == "resolution":
                    verification_payload = data["verification"]
                    if (
                        verification_payload.get("matched")
                        and not verification_payload.get("discrepancies")
                    ):
                        # Clean invoice — nothing to resolve. Abstention is a
                        # first-class outcome; record it for the audit trail.
                        payload = {
                            "decision": {
                                "lane": "escalate",
                                "rationale": (
                                    "no discrepancies flagged by CoVe "
                                    "verification — nothing to resolve"
                                ),
                                "confidence": verification_payload.get(
                                    "confidence"
                                ),
                            },
                            "outcome": "escalated",
                            "skipped_no_discrepancies": True,
                        }
                    else:
                        evidence = await self._evidence_packet(
                            data["extraction"]["invoice"],
                            verification_payload,
                        )
                        payload = await self._stage_resolution(
                            data["extraction"], verification_payload, evidence
                        )
                        payload = await self._close_resolution(payload)
                    if (
                        payload.get("outcome") == "resolved"
                        and payload.get("corrected_invoice")
                    ):
                        # Downstream stages (categorization, reconciliation)
                        # consume the CORRECTED invoice: re-checkpoint the
                        # extraction stage with the fix applied. checkpoint()
                        # merges stage data; the duplicate 'extraction' entry
                        # in stages_done is harmless (forward-only resume).
                        state = await self.store.checkpoint(
                            run_id=run_id,
                            invoice_id=invoice_id,
                            stage="extraction",
                            data={
                                **data["extraction"],
                                "invoice": payload["corrected_invoice"],
                            },
                        )
                        data = state.get("stages_data") or data
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
                        resolution=data.get("resolution"),
                    )
                else:  # pragma: no cover — guarded by PER_INVOICE_STAGES
                    break
                if stage == "reconciliation":
                    # Deterministic backstop BEFORE persisting the verdict.
                    payload = self._python_recheck(payload, data)
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

        invoice_results: list[dict[str, Any]] = []
        for s in completed:
            rec = (s.get("stages_data") or {}).get("reconciliation") or {}
            # Honor HITL Tier-1 in batch mode (M2): the per-agent callback
            # flags low confidence in its throwaway session; the pipeline
            # re-derives the same signal from the persisted stage payloads
            # so a low-confidence invoice escalates even when matched.
            low_conf = [
                {"stage": st, "confidence": p["confidence"]}
                for st, p in (s.get("stages_data") or {}).items()
                if isinstance(p, dict)
                and isinstance(p.get("confidence"), (int, float))
                and p["confidence"] < CONFIDENCE_THRESHOLD
            ]
            if low_conf:
                rec = dict(rec)
                rec["low_confidence_flags"] = low_conf
            # A pending dispute draft awaiting human approval is also a flag.
            resolution_payload = (s.get("stages_data") or {}).get("resolution") or {}
            if resolution_payload.get("outcome") == "disputed":
                rec = dict(rec)
                rec["pending_dispute"] = resolution_payload.get("dispute_draft")
            invoice_results.append(rec)
        result.flagged_count = sum(
            1
            for r in invoice_results
            if r.get("verdict") != "matched"
            or r.get("low_confidence_flags")
            or r.get("pending_dispute")
        )
        # Closed-loop money metrics. dollars_recovered remains 0.0 here by
        # design: only the HITL approval surface may increment it, and only
        # for approved disputes + re-verified corrections.
        at_risk = 0.0
        for s in completed:
            resolution_payload = (s.get("stages_data") or {}).get("resolution") or {}
            draft = resolution_payload.get("dispute_draft") or {}
            if resolution_payload.get("outcome") == "disputed":
                try:
                    at_risk += float(draft.get("amount_at_risk") or 0.0)
                except (TypeError, ValueError):
                    pass
        result.dollars_at_risk = round(at_risk, 2)

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
        # Hard clamp (never trust a prompt to keep a safety property):
        # batch mode has no human to approve the HITL Tier-2 pause, so the
        # digest is ALWAYS composed-not-sent.
        if isinstance(result.digest, dict):
            result.digest["email_sent"] = False
            result.digest["email_blocked_by_hitl"] = True

        status = "completed" if not failed else "completed_with_errors"
        await self.store.end_run(
            run_id=run_id,
            status=status,
            summary={
                "digest": result.digest,
                "flagged_count": result.flagged_count,
                "dollars_recovered": result.dollars_recovered,
                "dollars_at_risk": result.dollars_at_risk,
            },
        )
        logger.info(
            "run %s done: %d/%d completed, %d failed, %d flagged, "
            "$%.2f at risk (drafted), $%.2f recovered (approved-only)",
            run_id,
            result.invoices_completed,
            result.invoices_total,
            result.invoices_failed,
            result.flagged_count,
            result.dollars_at_risk,
            result.dollars_recovered,
        )
        return result
