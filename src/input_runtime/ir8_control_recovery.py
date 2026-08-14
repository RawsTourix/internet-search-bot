"""Strict IR-8 startup validation for durable control frontiers."""

from __future__ import annotations

from .errors import InputRuntimeConflictError
from .ir8_filesystem import FileSystemSessionControlRepository as _IR8ControlRepository


class FileSystemSessionControlRepository(_IR8ControlRepository):
    async def recover_session_authority(self, session_id: str):
        state = await super().recover_session_authority(session_id)
        if state is None:
            return None
        rows = await self.list_for_session(session_id)
        durable_through = max((item.sequence_number for item in rows), default=0)
        if state.pending_control_sequence > durable_through:
            raise InputRuntimeConflictError(
                "control watermark exceeds authoritative records"
            )
        if state.applied_control_sequence > durable_through:
            raise InputRuntimeConflictError(
                "applied control watermark exceeds authoritative records"
            )
        return state
