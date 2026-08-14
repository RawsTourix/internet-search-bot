"""IR-8 startup-only emission reconciliation over IR-6 delivery authority."""

from __future__ import annotations

from ._filesystem_common import validated_copy
from .ir6_delivery_authority import FileSystemAgentEmissionRepository as _IR6Repository
from .models import EmissionState
from .serialization import atomic_write_model


class FileSystemAgentEmissionRepository(_IR6Repository):
    async def reconcile_terminal_ready_for_recovery(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        reason_code: str = "terminal_cycle_startup_fence",
    ):
        """Cancel only unsent READY intent after terminal authority wins.

        DELIVERING/UNKNOWN are never rewritten to READY and therefore retain
        their conservative IR-6 ambiguity semantics.
        """
        cancelled = []
        async with self.locks.hold(self.root, session_id):
            for stale in self._scan():
                if (
                    stale.session_id != session_id
                    or stale.cycle_id != cycle_id
                    or stale.generation != generation
                    or stale.state != EmissionState.READY
                ):
                    continue
                current = await self._find(stale.emission_id)
                if current.state != EmissionState.READY:
                    continue
                updated = validated_copy(
                    current,
                    state=EmissionState.CANCELLED,
                    cancellation_reason_code=reason_code,
                )
                atomic_write_model(
                    self.layout.emission(current.cycle_id, current.emission_id),
                    updated,
                )
                cancelled.append(updated)
        return tuple(cancelled)
