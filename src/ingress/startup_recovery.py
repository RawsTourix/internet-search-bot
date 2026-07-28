"""Startup reconciliation for durable filesystem input drafts."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import CommittedInputBatch, InputBatchDraft, InputGroupingMode


logger = logging.getLogger("API.Ingress.StartupRecovery")


@dataclass(frozen=True, slots=True)
class IngressStartupRecoveryReport:
    committed_input_batch_ids: tuple[str, ...]
    abandoned_input_batch_ids: tuple[str, ...]

    @property
    def committed_count(self) -> int:
        return len(self.committed_input_batch_ids)

    @property
    def abandoned_count(self) -> int:
        return len(self.abandoned_input_batch_ids)


async def reconcile_ingress_after_restart(
    ingress_service,
    batch_store,
    *,
    abandonment_code: str = "process_restart_abandoned",
) -> IngressStartupRecoveryReport:
    """Commit already-ready drafts, then abandon every remaining open draft.

    After process restart no previous process-local upload stream, debounce task
    or commit owner can still finish an open draft. Fully ready drafts are
    published first without starting an agent cycle. All remaining drafts are
    preserved as terminal audit records but removed from active grouping.
    """

    committed: list[CommittedInputBatch] = []
    list_ready = getattr(batch_store, "list_ready_drafts", None)
    if list_ready is not None:
        try:
            ready_drafts = await list_ready()
        except Exception:
            logger.exception("ingress_startup_ready_scan_failed")
            ready_drafts = []
        for draft in ready_drafts:
            try:
                committed_batch = await ingress_service.commit_batch(
                    draft.input_batch_id,
                    session_id=draft.session_id,
                    reason=(
                        "media_group_restart_recovery"
                        if draft.grouping_mode == InputGroupingMode.MEDIA_GROUP
                        else "standalone_attachment_restart_recovery"
                    ),
                )
            except Exception:
                logger.exception(
                    "ingress_startup_ready_commit_failed "
                    "input_batch_id=%s session_id=%s",
                    draft.input_batch_id,
                    draft.session_id,
                )
            else:
                committed.append(committed_batch)

    abandon_open = getattr(batch_store, "abandon_open_drafts", None)
    if abandon_open is None:
        abandoned: list[InputBatchDraft] = []
    else:
        abandoned = await abandon_open(code=abandonment_code)

    coordinator = getattr(ingress_service, "presentation_coordinator", None)
    if coordinator is not None:
        for draft in abandoned:
            try:
                await coordinator.finalize_batch(
                    input_batch_id=draft.input_batch_id,
                    state="failed",
                    file_count=len(draft.attachment_parts),
                    text_part_count=len(draft.text_parts),
                    response_anchor=draft.response_anchor,
                )
            except Exception:
                logger.exception(
                    "ingress_startup_presentation_finalize_failed "
                    "input_batch_id=%s session_id=%s",
                    draft.input_batch_id,
                    draft.session_id,
                )

    report = IngressStartupRecoveryReport(
        committed_input_batch_ids=tuple(
            item.input_batch_id for item in committed
        ),
        abandoned_input_batch_ids=tuple(
            item.input_batch_id for item in abandoned
        ),
    )
    logger.info(
        "ingress_startup_reconciliation_completed committed=%s abandoned=%s",
        report.committed_count,
        report.abandoned_count,
    )
    return report
