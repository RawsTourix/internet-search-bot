"""Admission compatibility for historical terminal session projections."""

from __future__ import annotations

from ._filesystem_identity import (
    FileSystemInputAdmissionRepository as _FencedAdmissionRepository,
)
from ._filesystem_session import TERMINAL_OR_IDLE
from .errors import InputRuntimeConflictError
from .models import SessionInputRuntimeState


class FileSystemInputAdmissionRepository(_FencedAdmissionRepository):
    """Allow a new cycle after legacy terminal state without admission history."""

    async def _repair_from_authoritative_admissions(
        self,
        state_path,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState:
        session_rows = await self.list_for_session(state.session_id)
        if not session_rows:
            if state.cycle_status in TERMINAL_OR_IDLE:
                return state
            raise InputRuntimeConflictError(
                "active session has no authoritative admissions"
            )
        return await super()._repair_from_authoritative_admissions(
            state_path,
            state,
        )
