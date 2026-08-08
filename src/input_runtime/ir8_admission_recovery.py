"""Strict IR-8 admission/cycle-authority startup repair."""

from __future__ import annotations

from ._filesystem_identity_recovery_common import recover_cycle_authority
from .ir8_filesystem import FileSystemInputAdmissionRepository as _IR8AdmissionRepository


class FileSystemInputAdmissionRepository(_IR8AdmissionRepository):
    """Repair only derived cycle-authority pointers after history agrees."""

    async def recover_session_authority(self, session_id: str):
        state = await super().recover_session_authority(session_id)
        if state is None:
            return None
        rows = await self.list_for_session(session_id)
        for row in rows:
            # First validate every authoritative durable source.  This rejects
            # cross-session contradictions before touching the derived pointer.
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
                # At this point recover_cycle_authority proved there is exactly
                # one compatible durable owner, so rebuilding the derived index
                # is deterministic rather than a semantic guess.
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
