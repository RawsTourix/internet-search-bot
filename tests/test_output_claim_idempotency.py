import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.errors import OutputBatchConflictError
from src.interaction.ids import (
    new_output_claim_request_id,
    new_output_part_id,
)
from src.interaction.output_claim import IdempotentOutputClaimService
from src.interaction.output_models import (
    OutputBatchKind,
    OutputBatchState,
    TextOutputPart,
)
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)


UTC = timezone.utc


class OutputClaimIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FileSystemOutputBatchStore(Path(self.temporary.name))
        self.service = IdempotentOutputClaimService(self.store)
        self.batch = await self._commit("cycle-1")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_same_request_replays_original_attempt(self):
        request_id = new_output_claim_request_id()
        first, first_attempt = await self.service.claim(
            self.batch.output_batch_id,
            claim_request_id=request_id,
        )
        replayed, replayed_attempt = await self.service.claim(
            self.batch.output_batch_id,
            claim_request_id=request_id,
        )
        self.assertEqual(first.state, OutputBatchState.DELIVERING)
        self.assertEqual(replayed.state, OutputBatchState.DELIVERING)
        self.assertEqual(replayed_attempt, first_attempt)

    async def test_different_request_cannot_join_active_claim(self):
        await self.service.claim(
            self.batch.output_batch_id,
            claim_request_id=new_output_claim_request_id(),
        )
        with self.assertRaises(OutputBatchConflictError):
            await self.service.claim(
                self.batch.output_batch_id,
                claim_request_id=new_output_claim_request_id(),
            )

    async def test_request_id_cannot_be_reused_for_another_batch(self):
        other = await self._commit("cycle-2")
        request_id = new_output_claim_request_id()
        await self.service.claim(
            self.batch.output_batch_id,
            claim_request_id=request_id,
        )
        with self.assertRaises(OutputBatchConflictError):
            await self.service.claim(
                other.output_batch_id,
                claim_request_id=request_id,
            )
        self.assertEqual(
            (await self.store.get(other.output_batch_id)).state,
            OutputBatchState.READY,
        )

    async def test_request_index_failure_rolls_back_batch_state(self):
        original_write = self.store._write

        def fail_request_index(path, payload):
            if path.parent == self.service.requests:
                raise OSError("synthetic claim-index failure")
            return original_write(path, payload)

        self.store._write = fail_request_index
        request_id = new_output_claim_request_id()
        try:
            with self.assertRaises(OSError):
                await self.service.claim(
                    self.batch.output_batch_id,
                    claim_request_id=request_id,
                    now=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
                )
        finally:
            self.store._write = original_write

        current = await self.store.get(self.batch.output_batch_id)
        self.assertEqual(current.state, OutputBatchState.READY)
        self.assertFalse((self.service.requests / f"{request_id}.json").exists())

    async def _commit(self, cycle_id: str):
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id=cycle_id,
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
            ),
            locale="en",
            capability_snapshot=snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text=cycle_id,
                ),
            ),
        )
        return (await self.store.commit(batch))[0]


if __name__ == "__main__":
    unittest.main()
