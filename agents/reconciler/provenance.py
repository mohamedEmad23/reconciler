"""Provenance read path + rendering (design doc §3, P12).

The WRITE side lives in ``pipeline._close_resolution``: every resolution
payload persists an auditable ``provenance`` dict (extraction hash, CoVe
questions/answers, memory keys consulted, rule fired with its real score,
rationale, re-verification result, Cloud Trace id).

This module is the READ side — the "why did Reconciler do this?" view:

* :func:`entries_from_state` — pull provenance out of a Firestore invoice
  state and VALIDATE it through the ``ProvenanceEntry`` Pydantic model
  (closes the Gate 9 FYI "the model is never instantiated": unvalidated
  dicts are audit theater; a model round-trip proves the stored payload
  still matches the declared schema).
* :func:`render_entry` / :func:`render_for_digest` — human-readable
  rendering for the digest email, the HITL approval surface (P13) and
  ``scripts/show_firestore.py``.
* :func:`attach_digest_provenance` — deterministic Python (never a prompt)
  that appends the rendered "why" blocks to the run digest for every
  invoice that recorded provenance, so flagged items carry their evidence.

Design rules honored here:
* missing provenance is an explicit signal (rendered as such), never an
  invented explanation (Instruction Contract rule 1 applies to tooling
  too);
* rendering is pure string formatting — no LLM calls, byte-stable output
  for the same state (same determinism posture as the rest of the spine).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .schemas import ProvenanceEntry

__all__ = [
    "entries_from_state",
    "render_entry",
    "render_for_digest",
    "attach_digest_provenance",
]

_NO_PROVENANCE_LINE = "(no provenance recorded for this invoice)"


def _raw_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the raw provenance payloads stored on an invoice state.

    ``_close_resolution`` stores a single dict under
    ``stages_data.resolution.provenance``. A list form is accepted too so
    the reader survives a future multi-discrepancy writer.
    """
    resolution = (state.get("stages_data") or {}).get("resolution") or {}
    raw = resolution.get("provenance")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def entries_from_state(
    state: dict[str, Any],
) -> tuple[list[ProvenanceEntry], list[str]]:
    """Validate the stored provenance payloads against the schema.

    Returns ``(validated_entries, errors)``. Malformed entries surface as
    error strings instead of aborting the read — an audit trail reader
    must never crash the demo on one bad row.
    """
    entries: list[ProvenanceEntry] = []
    errors: list[str] = []
    for i, raw in enumerate(_raw_entries(state)):
        try:
            entries.append(ProvenanceEntry.model_validate(raw))
        except ValidationError as exc:
            errors.append(f"provenance[{i}] failed validation: {exc.error_count()} error(s)")
    return entries, errors


def _qa_block(entry: ProvenanceEntry) -> str:
    qs, ans = entry.verification_questions, entry.verification_answers
    if not qs:
        return "verification : (no CoVe questions recorded)"
    lines = [f"verification : CoVe — {len(qs)} independent check(s)"]
    for i, q in enumerate(qs):
        a = ans[i] if i < len(ans) else "(answer missing)"
        lines.append(f"  Q{i + 1}: {q}")
        lines.append(f"  A{i + 1}: {a}")
    return "\n".join(lines)


def _recheck_line(entry: ProvenanceEntry) -> str:
    if entry.recheck_matched is True:
        return "recheck       : independent re-verification CONFIRMED the fix (loop closed)"
    if entry.recheck_matched is False:
        return "recheck       : independent re-verification did NOT confirm — escalated (never self-certified)"
    return "recheck       : (not run — no correction was applied)"


def render_entry(entry: ProvenanceEntry, *, invoice_id: str | None = None) -> str:
    """Render one entry as the judge-facing 'why' block."""
    head = invoice_id or entry.extraction_hash or "invoice"
    lines = [f"---- {head} ----"]
    lines.append(f"discrepancy   : {entry.discrepancy_type or '(unknown)'}")
    lines.append(f"lane          : {entry.lane or '(unknown)'}")
    if entry.rule_fired:
        lines.append(f"rule fired    : {entry.rule_fired}")
    if entry.memory_keys_consulted:
        lines.append(f"memory        : {', '.join(entry.memory_keys_consulted)}")
    if entry.resolution_rationale:
        lines.append(f"rationale     : {entry.resolution_rationale}")
    lines.append(_qa_block(entry))
    lines.append(_recheck_line(entry))
    if entry.human_decision:
        lines.append(f"human         : {entry.human_decision}")
    elif entry.lane == "dispute":
        lines.append("human         : pending approval")
    if entry.trace_id:
        lines.append(f"trace         : {entry.trace_id} (Cloud Trace waterfall)")
    if entry.timestamp:
        lines.append(f"recorded      : {entry.timestamp}")
    return "\n".join(lines)


def render_for_digest(state: dict[str, Any], *, invoice_id: str | None = None) -> str:
    """Render all provenance for one invoice, or the explicit missing-signal."""
    entries, errors = entries_from_state(state)
    if not entries and not errors:
        return _NO_PROVENANCE_LINE
    parts = [render_entry(e, invoice_id=invoice_id) for e in entries]
    parts.extend(f"(malformed entry skipped: {err})" for err in errors)
    return "\n".join(parts)


def attach_digest_provenance(
    digest: dict[str, Any], states: list[dict[str, Any]]
) -> dict[str, Any]:
    """Attach rendered 'why' blocks to the digest, deterministically.

    Only invoices that actually recorded provenance get a block — the
    clean-invoice skip path stores none, so they stay out naturally.
    Pure Python: the digest's evidence trail is never delegated to a
    prompt (same doctrine as the ``email_sent`` hard clamp).
    """
    rendered: dict[str, str] = {}
    for state in states:
        entries, _ = entries_from_state(state)
        if not entries:
            continue
        invoice_id = state.get("invoice_id") or "(unknown invoice)"
        rendered[invoice_id] = render_for_digest(state, invoice_id=invoice_id)
    if rendered:
        digest["provenance"] = rendered
    return digest
