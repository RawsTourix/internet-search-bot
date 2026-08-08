"""Startup-only committed InputBatch discovery for durable runtime recovery."""

from __future__ import annotations

from .models import CommittedInputBatch
from .store import FileSystemInputBatchStore


class FileSystemCommittedInputBatchRecoveryReader:
    """Adapter-local whole-store scan used only during process startup.

    Normal admission remains exact-ID and never inherits scan-all semantics.
    A future SQL adapter can implement the same application port with an indexed
    SELECT ordered by immutable commit metadata.
    """

    def __init__(self, store: FileSystemInputBatchStore) -> None:
        self.store = store

    async def get_committed(self, input_batch_id: str) -> CommittedInputBatch:
        return await self.store.get_committed(input_batch_id)

    async def list_committed_for_recovery(self) -> tuple[CommittedInputBatch, ...]:
        def scan() -> tuple[CommittedInputBatch, ...]:
            rows: list[CommittedInputBatch] = []
            batches_root = self.store.root / "input-batches"
            if not batches_root.exists():
                return ()
            for path in sorted(batches_root.glob("ibat_*/committed.json")):
                rows.append(self.store._read_committed(path))
            rows.sort(
                key=lambda item: (
                    item.session_id,
                    item.sequence_number,
                    item.committed_at,
                    item.input_batch_id,
                )
            )
            return tuple(rows)

        import asyncio

        return await asyncio.to_thread(scan)
