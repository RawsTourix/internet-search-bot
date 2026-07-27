import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.ingress.models import new_input_batch_id
from src.interaction.errors import PresentationConflictError
from src.interaction.presentation import PresentationState
from src.interaction.presentation_service import InputPresentationCoordinator
from src.interaction.presentation_store import FileSystemInputPresentationStore


class InputPresentationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FileSystemInputPresentationStore(
            Path(self.temporary.name)
        )
        self.coordinator = InputPresentationCoordinator(self.store)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_atomic_commit_reserves_then_late_bind_closes(self):
        batch_id = new_input_batch_id()
        _, _, public = await self.coordinator.present(
            input_batch_id=batch_id,
            client_binding_id="telegram:bot:chat",
            locale="ru",
            state="committed",
            file_count=1,
            text_part_count=0,
            response_anchor=None,
        )
        reserved = await self.store.get(public.presentation_id)
        self.assertEqual(reserved.state, PresentationState.RESERVED)
        self.assertEqual(
            reserved.pending_terminal_state,
            PresentationState.CLOSED,
        )

        bound = await self.store.bind(
            public.presentation_id,
            client_message_id="501",
            token=public.presentation_token,
        )
        self.assertEqual(bound.state, PresentationState.CLOSED)
        self.assertEqual(bound.client_message_id, "501")
        self.assertIsNotNone(bound.closed_at)

    async def test_terminal_bind_is_an_explicit_conflict(self):
        _, _, public = await self.coordinator.present(
            input_batch_id=new_input_batch_id(),
            client_binding_id="telegram:bot:chat",
            locale="ru",
            state="committed",
            file_count=1,
            text_part_count=0,
            response_anchor=None,
        )
        await self.store.bind(
            public.presentation_id,
            client_message_id="501",
            token=public.presentation_token,
        )
        with self.assertRaises(PresentationConflictError):
            await self.store.bind(
                public.presentation_id,
                client_message_id="502",
                token=public.presentation_token,
            )

    async def test_grouped_bound_presentation_closes_on_commit(self):
        batch_id = new_input_batch_id()
        _, _, public = await self.coordinator.present(
            input_batch_id=batch_id,
            client_binding_id="telegram:bot:album",
            locale="ru",
            state="collecting",
            file_count=10,
            text_part_count=0,
            response_anchor=None,
        )
        bound = await self.store.bind(
            public.presentation_id,
            client_message_id="700",
            token=public.presentation_token,
        )
        self.assertEqual(bound.state, PresentationState.BOUND)

        finalized = await self.coordinator.finalize_batch(
            input_batch_id=batch_id,
            state="committed",
            file_count=10,
            text_part_count=1,
            response_anchor=None,
        )
        self.assertIsNotNone(finalized)
        closed = await self.store.get(public.presentation_id)
        self.assertEqual(closed.state, PresentationState.CLOSED)
        self.assertEqual(closed.client_message_id, "700")

    async def test_expired_reservation_is_reclaimed_with_new_handle(self):
        batch_id = new_input_batch_id()
        _, _, first = await self.coordinator.present(
            input_batch_id=batch_id,
            client_binding_id="telegram:bot:chat",
            locale="ru",
            state="collecting",
            file_count=1,
            text_part_count=0,
            response_anchor=None,
        )
        await self.store.close(
            first.presentation_id,
            state=PresentationState.EXPIRED,
            error_code="reservation_timeout",
        )
        _, _, second = await self.coordinator.present(
            input_batch_id=batch_id,
            client_binding_id="telegram:bot:chat",
            locale="ru",
            state="collecting",
            file_count=1,
            text_part_count=0,
            response_anchor=None,
        )
        self.assertNotEqual(first.presentation_id, second.presentation_id)
        self.assertIsNotNone(second.presentation_token)

    async def test_stale_deferred_terminal_reservation_expires_cleanly(self):
        _, _, public = await self.coordinator.present(
            input_batch_id=new_input_batch_id(),
            client_binding_id="telegram:bot:startup-recovery",
            locale="ru",
            state="committed",
            file_count=1,
            text_part_count=0,
            response_anchor=None,
        )
        reserved = await self.store.get(public.presentation_id)
        self.assertEqual(reserved.state, PresentationState.RESERVED)
        self.assertEqual(
            reserved.pending_terminal_state,
            PresentationState.CLOSED,
        )

        recovery_time = reserved.updated_at + timedelta(seconds=31)
        expired = await self.store.expire_stale_reservations(
            timeout_seconds=30,
            now=recovery_time,
        )

        self.assertEqual(len(expired), 1)
        recovered = expired[0]
        self.assertEqual(recovered.state, PresentationState.EXPIRED)
        self.assertIsNone(recovered.pending_terminal_state)
        self.assertEqual(recovered.closed_at, recovery_time)
        self.assertEqual(recovered.error_code, "reservation_timeout")


if __name__ == "__main__":
    unittest.main()
