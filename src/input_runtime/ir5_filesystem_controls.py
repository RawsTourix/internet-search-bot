"""IR-5 filesystem control adapter extensions.

The application layer uses command-oriented methods only; these audit reads are
also useful to deterministic tests and future diagnostics without exposing file
paths or layout.
"""
from __future__ import annotations

from ._filesystem_identity_recovery_common import recover_cycle_authority
from ._filesystem_identity_recovery_session import (
    FileSystemSessionControlRepository as _BaseControlRepository,
)
from .models import ControlState, SessionControlCommand


class FileSystemSessionControlRepository(_BaseControlRepository):
    async def get(self, control_id: str) -> SessionControlCommand | None:
        return self._recover_by_id(control_id)

    async def list_for_session(
        self,
        session_id: str,
    ) -> tuple[SessionControlCommand, ...]:
        return await self._all(session_id)

    def _restore_indexes(self, command: SessionControlCommand) -> None:
        # Rejected no-op pause/continue records may carry a synthetic target to
        # satisfy the IR-1 schema. They are audit evidence, never cycle authority.
        if (
            command.target_cycle_id is not None
            and command.state != ControlState.REJECTED
        ):
            recover_cycle_authority(
                self,
                command.target_cycle_id,
                command.session_id,
            )
        self._index(command)
        if (
            command.target_cycle_id is not None
            and command.state != ControlState.REJECTED
        ):
            self._ensure_cycle_authority(
                command.target_cycle_id,
                command.session_id,
            )
