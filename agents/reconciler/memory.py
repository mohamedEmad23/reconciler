"""Reconciler state, memory, and resilience layer (Firestore).

This is the *business-level* persistence layer sitting ABOVE the ADK session
service. It implements three concerns from the design doc:

- **RunsStore**: an immutable audit trail (one document per run, one per
  processed invoice) and per-invoice checkpointing so a crashed run resumes
  from the last completed stage instead of restarting from scratch.
- **Idempotency**: each processed invoice is keyed by ``{run_id}_{invoice_id}``
  and a duplicate start for the same key is answered with ``None`` (caller
  skips) — at-least-once Pub/Sub redelivery can never double-process.
- **SharedMemory**: a structured fact store (known vendors, account codes,
  prior-invoice summaries) that retrieval-augmented agents read for grounding
  instead of inventing values — the anti-hallucination armament.

Anti-hallucination posture: this module only **persists** what the upstream
agents produced; it never fabricates values. ``SharedMemory.set_fact`` is the
single controlled write path and is only called after a stage's output has
been verified by the Verification (CoVe) agent. Reading a missing fact returns
``None`` — agents are taught to treat ``None`` as "do not know" and emit null
per the Instruction Contract rule 1.

Credentials: on Cloud Run the runtime SA supplies ADC via the metadata server;
locally the smoke exports ``GOOGLE_APPLICATION_CREDENTIALS``. No key file is
ever loaded inside the agent package — ``get_firestore_client()`` uses ADC
exclusively. ``roles/datastore.user`` on the runtime SA is the only Firestore
permission required.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Optional

from google.api_core.exceptions import AlreadyExists  # noqa: E402
from google.cloud import firestore  # noqa: E402 (LSP noise — package is installed)

from . import config

logger = logging.getLogger("reconciler.memory")

# ---------------------------------------------------------------------------
# Collection names — all top-level so they are easy to find in the console.
# ---------------------------------------------------------------------------

RUNS_COLLECTION = "runs"
RUN_INVOICES_COLLECTION = "run_invoices"
MEMORY_COLLECTION = "memory"

# Ordered pipeline stages. ``next_pending_stage`` walks this list so a
# resumed run knows exactly where to pick up. Order MUST match the agent
# topology in ``agent.py`` (Supervisor → intake/extraction/verification/
# categorization/reconciliation/reporting). Phase 4 ships extraction +
# verification only; the remaining stages are reserved so future phases do
# not have to migrate the schema.
STAGE_ORDER: tuple[str, ...] = (
    "intake",
    "extraction",
    "verification",
    "resolution",
    "categorization",
    "reconciliation",
    "reporting",
)

# Per-invoice processing states (closed set — AI never sets this; only the
# orchestrator does, after each stage).
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_DLQ = "dlq"

# Sentinel for the Firestore server timestamp — keeps ordering consistent
# regardless of worker clock skew.
_SERVER_NOW = firestore.SERVER_TIMESTAMP


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_firestore_client() -> firestore.AsyncClient:
    """Return a Firestore ``AsyncClient`` using ADC.

    Honors ``GOOGLE_APPLICATION_CREDENTIALS`` locally and the Cloud Run SA
    metadata server in production. The database id is the single config
    constant ``config.FIRESTORE_DATABASE`` (``"(default)"``).
    """
    return firestore.AsyncClient(database=config.FIRESTORE_DATABASE)


def _now_utc() -> datetime:
    """Local-clock fallback for values where we explicitly want an actual
    datetime rather than the server-timestamp sentinel (e.g. tests that
    need to compare without reading back from Firestore)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# RunsStore — audit trail + idempotency + checkpointing
# ---------------------------------------------------------------------------


class RunsStore:
    """Immutable run/invoice audit trail with per-stage checkpointing.

    Design (design doc §5):

    - ``runs`` collection: one doc per run — ``{run_id, job_type, started_at,
      ended_at, status, invoice_count, completed_count, failed_count}``.
      Append-only from the orchestrator's perspective; the only updates are
      the ``ended_at`` / ``status`` / counts at the close of the run.
    - ``run_invoices`` collection: one doc per processed invoice, keyed by
      the idempotency key ``{run_id}_{invoice_id}``. It carries the stage
      state machine — ``stages_done`` (ordered) + ``stages_data`` (per-stage
      JSON output) + ``status`` (``in_progress`` / ``completed`` / ``failed``
      / ``dlq``).
    """

    def __init__(self, client: Optional[firestore.AsyncClient] = None) -> None:
        self.client = client or get_firestore_client()

    # -- run-level --------------------------------------------------------

    async def start_run(
        self,
        *,
        run_id: str,
        job_type: str,
        invoice_count: int = 0,
    ) -> dict[str, Any]:
        """Create a run record. Idempotent on ``run_id`` — re-calling with
        the same id does NOT overwrite (returns existing doc) so a
        redelivered trigger cannot reset the run.

        Atomicity: uses ``ref.create()`` (409 on collision) rather than
        ``ref.set()``, so two concurrent workers that both miss the
        optimistic ``get()`` check cannot both win — exactly one ``create``
        succeeds and the loser re-reads and returns the existing doc."""
        ref = self.client.collection(RUNS_COLLECTION).document(run_id)
        snap = await ref.get()
        if snap.exists:
            return snap.to_dict()  # type: ignore[return-value]
        data = {
            "run_id": run_id,
            "job_type": job_type,
            "started_at": _SERVER_NOW,
            "ended_at": None,
            "status": STATUS_IN_PROGRESS,
            "invoice_count": invoice_count,
            "completed_count": 0,
            "failed_count": 0,
        }
        try:
            await ref.create(data)  # atomic — 409 if a concurrent worker won
            return data
        except AlreadyExists:
            # A concurrent trigger beat us; return its doc, never reset it.
            snap = await ref.get()
            return snap.to_dict()  # type: ignore[return-value]

    async def end_run(
        self,
        *,
        run_id: str,
        status: str,
        summary: Optional[dict[str, Any]] = None,
    ) -> None:
        """Close a run. ``status`` should be ``completed`` or ``failed``."""
        update: dict[str, Any] = {
            "ended_at": _SERVER_NOW,
            "status": status,
        }
        if summary is not None:
            update["summary"] = summary
        await self.client.collection(RUNS_COLLECTION).document(run_id).update(
            update
        )

    async def increment_counts(
        self,
        *,
        run_id: str,
        completed: int = 0,
        failed: int = 0,
        dollars_recovered: float = 0.0,
    ) -> None:
        """Atomically bump the run's counters.

        ``dollars_recovered`` follows the anti-gaming rule (design doc §2/§9):
        it is ONLY ever incremented for APPROVED disputes and RE-VERIFIED
        corrections — never for a draft and never for a self-certified flag.
        """
        ref = self.client.collection(RUNS_COLLECTION).document(run_id)
        if completed:
            await ref.update(
                {"completed_count": firestore.Increment(completed)}
            )
        if failed:
            await ref.update({"failed_count": firestore.Increment(failed)})
        if dollars_recovered:
            await ref.update(
                {"dollars_recovered": firestore.Increment(dollars_recovered)}
            )

    async def get_run(self, *, run_id: str) -> Optional[dict[str, Any]]:
        snap = await self.client.collection(RUNS_COLLECTION).document(
            run_id
        ).get()
        return snap.to_dict() if snap.exists else None

    # -- invoice-level ----------------------------------------------------

    @staticmethod
    def invoice_doc_id(run_id: str, invoice_id: str) -> str:
        """Idempotency key — the document id in ``run_invoices``."""
        return f"{run_id}_{invoice_id}"

    async def start_invoice(
        self,
        *,
        run_id: str,
        invoice_id: str,
        source_hash: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Begin processing an invoice.

        Returns:
            - the fresh state dict if this is a new invoice (caller proceeds
              with stage 1);
            - ``None`` if a doc already exists for ``{run_id}_{invoice_id}``
              (caller treats this as "already processed or in progress —
              skip; do not double-process").

        This is the idempotency gate: redelivered Pub/Sub messages, a
        Scheduler re-fire, or a duplicate queue entry all hit the second
        branch and never re-run extraction/verification for the same
        invoice in the same run.

        Atomicity: uses ``ref.create()`` (409 on collision) rather than
        ``ref.set()`` — under at-least-once Pub/Sub redelivery two Cloud
        Run instances can both miss the optimistic ``get()`` check, but
        only one ``create()`` succeeds. The loser catches ``AlreadyExists``
        and returns ``None`` so the caller skips. This is the fence that
        the sequenced smoke (step 6) cannot otherwise prove."""
        doc_id = self.invoice_doc_id(run_id, invoice_id)
        ref = self.client.collection(RUN_INVOICES_COLLECTION).document(doc_id)
        snap = await ref.get()
        if snap.exists:
            return None
        data = {
            "run_id": run_id,
            "invoice_id": invoice_id,
            "idempotency_key": doc_id,
            "status": STATUS_IN_PROGRESS,
            "stages_done": [],
            "stages_data": {},
            "source_hash": source_hash,
            "started_at": _SERVER_NOW,
            "updated_at": _SERVER_NOW,
            "error": None,
        }
        try:
            await ref.create(data)  # atomic — 409 if a concurrent worker won
            return data
        except AlreadyExists:
            # A redelivered message beat us; treat as already-processed.
            return None

    async def get_invoice_state(
        self,
        *,
        run_id: str,
        invoice_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return the full state dict for an invoice, or ``None`` if no
        doc exists yet. The dict mirrors exactly what was persisted so a
        resumed worker can branch on ``stages_done`` and feed
        ``stages_data["extraction"]`` straight into the Verification agent."""
        doc_id = self.invoice_doc_id(run_id, invoice_id)
        snap = await self.client.collection(RUN_INVOICES_COLLECTION).document(
            doc_id
        ).get()
        return snap.to_dict() if snap.exists else None

    async def checkpoint(
        self,
        *,
        run_id: str,
        invoice_id: str,
        stage: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a stage's output and append the stage to ``stages_done``.

        This is the **incremental checkpointing** primitive — call it after
        every stage completes so a crash restarts at the next stage, not
        at the beginning. ``stage`` must be one of ``STAGE_ORDER``.
        """
        if stage not in STAGE_ORDER:
            raise ValueError(
                f"unknown stage {stage!r}; must be one of {STAGE_ORDER}"
            )
        doc_id = self.invoice_doc_id(run_id, invoice_id)
        ref = self.client.collection(RUN_INVOICES_COLLECTION).document(doc_id)
        snap = await ref.get()
        if not snap.exists:
            raise KeyError(
                f"invoice {doc_id} not started — call start_invoice first"
            )
        existing = snap.to_dict() or {}
        stages_done = list(existing.get("stages_done", []))
        if stage not in stages_done:
            stages_done.append(stage)
        merged_data = {**existing.get("stages_data", {}), stage: data}
        update = {
            "stages_done": stages_done,
            "stages_data": merged_data,
            "updated_at": _SERVER_NOW,
        }
        await ref.update(update)
        return {**existing, **update}

    async def mark_invoice_completed(
        self, *, run_id: str, invoice_id: str
    ) -> None:
        await self._set_invoice_status(
            run_id=run_id, invoice_id=invoice_id, status=STATUS_COMPLETED
        )

    async def mark_invoice_failed(
        self,
        *,
        run_id: str,
        invoice_id: str,
        error: str,
        dlq: bool = False,
    ) -> None:
        await self._set_invoice_status(
            run_id=run_id,
            invoice_id=invoice_id,
            status=STATUS_DLQ if dlq else STATUS_FAILED,
            error=error,
        )

    async def _set_invoice_status(
        self,
        *,
        run_id: str,
        invoice_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        doc_id = self.invoice_doc_id(run_id, invoice_id)
        update: dict[str, Any] = {
            "status": status,
            "updated_at": _SERVER_NOW,
        }
        if error is not None:
            update["error"] = error
        await self.client.collection(RUN_INVOICES_COLLECTION).document(
            doc_id
        ).update(update)

    @staticmethod
    def next_pending_stage(state: dict[str, Any]) -> Optional[str]:
        """Given a persisted invoice state, return the next stage to run.

        Stages are **forward-only and ordered**: the next stage is the one
        immediately *after* the highest-indexed completed stage in
        ``STAGE_ORDER``. A skipped earlier stage (e.g. ``intake`` when only
        ``extraction`` was checkpointed) is never re-queued — that is the
        crash-resume guarantee: the pipeline picks up where it left off, it
        does not restart. Returns ``None`` when every stage is done.
        """
        done = set(state.get("stages_done", []))
        if not done:
            return STAGE_ORDER[0]
        max_idx = -1
        for i, stage in enumerate(STAGE_ORDER):
            if stage in done:
                max_idx = i
        if max_idx + 1 < len(STAGE_ORDER):
            return STAGE_ORDER[max_idx + 1]
        return None

    # -- cleanup (used by tests; never called in prod) --------------------

    async def delete_invoice(self, *, run_id: str, invoice_id: str) -> None:
        doc_id = self.invoice_doc_id(run_id, invoice_id)
        await self.client.collection(RUN_INVOICES_COLLECTION).document(
            doc_id
        ).delete()

    async def delete_run(self, *, run_id: str) -> None:
        await self.client.collection(RUNS_COLLECTION).document(run_id).delete()


# ---------------------------------------------------------------------------
# SharedMemory — structured grounding facts (the anti-hallucination store)
# ---------------------------------------------------------------------------


class SharedMemory:
    """Structured fact store for retrieval-augmented grounding.

    The design doc's "Shared Epistemic Memory" — a single shared knowledge
    pool in Firestore. Agents read from it (``get_fact``) to ground vendor
    names, account codes, and prior-totals rather than inventing them; the
    orchestrator writes to it (``set_fact``) only AFTER a stage output has
    been verified by the CoVe agent.

The key is hashed (namespace + ':' + key) so vendor names containing
slashes/spaces don't collide with Firestore doc id rules. Namespace is
a closed vocabulary (e.g. ``"vendor"``, ``"account_code"``,
``"prior_invoice"``); ``key`` is the natural lookup string.
    """

    def __init__(self, client: Optional[firestore.AsyncClient] = None) -> None:
        self.client = client or get_firestore_client()

    @staticmethod
    def _doc_id(namespace: str, key: str) -> str:
        digest = hashlib.sha256(f"{namespace}:{key}".encode("utf-8")).hexdigest()
        return f"{namespace}_{digest[:16]}"

    @staticmethod
    def _deep_merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        """Deep-merge ``new`` onto ``old``. dict⊕dict recurses per-key,
        list⊕list extends preserving order and dedupes, scalar⊕scalar lets
        ``new`` win. Returns a fresh dict — neither input is mutated."""
        result = dict(old)
        for k, nv in new.items():
            if k in result:
                ov = result[k]
                if isinstance(ov, list) and isinstance(nv, list):
                    merged_list = list(ov)
                    for item in nv:
                        if item not in merged_list:
                            merged_list.append(item)
                    result[k] = merged_list
                elif isinstance(ov, dict) and isinstance(nv, dict):
                    result[k] = SharedMemory._deep_merge(ov, nv)
                else:
                    result[k] = nv
            else:
                result[k] = nv
        return result

    async def get_fact(
        self, *, namespace: str, key: str
    ) -> Optional[dict[str, Any]]:
        """Return the ``value`` payload for a fact, or ``None`` if no fact
        has been recorded yet. ``None`` is the explicit signal to the
        agent that it does NOT know and must emit null per Contract rule 1
        — never invent a value to fill the gap."""
        ref = self.client.collection(MEMORY_COLLECTION).document(
            self._doc_id(namespace, key)
        )
        snap = await ref.get()
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        return doc.get("value")

    async def set_fact(
        self,
        *,
        namespace: str,
        key: str,
        value: dict[str, Any],
        merge: bool = True,
    ) -> dict[str, Any]:
        """Persist a fact. With ``merge=True`` (default) new keys in
        ``value`` are merged onto the existing value, so remembering a
        vendor that we've seen before extends (not replaces) its record
        — e.g. appending a new invoice number to ``invoice_numbers_seen``.
        With ``merge=False`` the value is overwritten wholesale.

        Merge semantics (``_deep_merge``):
        - dict ⊕ dict → recurse (per-key);
        - list ⊕ list → extend preserving order and dedupe;
        - scalar ⊕ scalar → new wins.
        Only the orchestrator should call this, and only after the CoVe
        Verification agent has confirmed the source stage output is
        trustworthy — otherwise we'd persist an unverified (possibly
        hallucinated) fact into the grounding pool."""
        ref = self.client.collection(MEMORY_COLLECTION).document(
            self._doc_id(namespace, key)
        )
        snap = await ref.get()
        existing = snap.to_dict() if snap.exists else None
        if existing and merge:
            merged_value = self._deep_merge(
                existing.get("value", {}), value
            )
            doc = {
                "namespace": existing.get("namespace", namespace),
                "key": existing.get("key", key),
                "value": merged_value,
                "created_at": existing.get("created_at", _SERVER_NOW),
                "updated_at": _SERVER_NOW,
            }
        else:
            doc = {
                "namespace": namespace,
                "key": key,
                "value": value,
                "created_at": _SERVER_NOW,
                "updated_at": _SERVER_NOW,
            }
        await ref.set(doc)
        return doc

    async def delete_fact(self, *, namespace: str, key: str) -> None:
        ref = self.client.collection(MEMORY_COLLECTION).document(
            self._doc_id(namespace, key)
        )
        await ref.delete()