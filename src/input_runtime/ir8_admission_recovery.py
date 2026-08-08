"""Strict IR-8 admission/cycle-authority startup repair."""

from __future__ import annotations

from ._filesystem_identity_recovery_common import (
    _all_cycle_sessions,
    recover_cycle_authority,
)
from .errors import InputRuntimeConflictError
from .ir8_filesystem import FileSystemInputAdmissionRepository as _IR8AdmissionRepository


class FileSystemInputAdmissionRepository(_IR8AdmissionRepository):
    """Repair derived cycle pointers only after all durable sources agree."""

    async def recover_session_authority(self, session_id: str):
        state = await super().recover_session_authority(session_id)
        if state is None:
            return None
        rows = await self.list_for_session(session_id)
        for row in rows:
            # Startup is allowed one whole-repository consistency pass. Unlike
            # the normal hot-path helper, IR-8 must compare cycle-local records
            # with admissions/controls from every session before rebuilding a
            # derived pointer. Contradictory immutable history is fatal.
            durable_sessions = _all_cycle_sessions(self, row.target_cycle_id)
            if len(durable_sessions) > 1:
                raise InputRuntimeConflictError(
                    "cycle authority points to multiple sessions"
                )
            if durable_sessions and durable_sessions != {row.session_id}:
                raise InputRuntimeConflictError(
                    "cycle authority points to multiple sessions"
                )

            recover_cycle_authority(
                self,
                row.target_cycle_id,
                row.session_id,
            )
            path = self.layout.cycle_authority(row.target_cycle_id)
            pointer = self._read_pointer(path)
            expected_relative_path = str(
                self.layout.cycle_dir(row.target_cycle_id).relative_to(
                    self.layout.root
                )
            )
            if (
                pointer is None
                or pointer.record_type != "cycle"
                or pointer.record_id != row.target_cycle_id
                or pointer.cycle_id != row.target_cycle_id
                or pointer.session_id != row.session_id
                or pointer.relative_path != expected_relative_path
            ):
                self._write_pointer(
                    path,
                    self._pointer(
                        "cycle",
                        row.target_cycle_id,
                        row.session_id,
                        self.layout.cycle_dir(row.target_cycle_id),
                        row.target_cycle_id,
                    ),
                )
        return state
