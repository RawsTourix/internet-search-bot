import unittest
from types import SimpleNamespace

from src.mcp.waiting_user_batch_continuation import (
    WaitingUserBatchContinuationMixin,
)


class _BaseClient:
    def __init__(self):
        self.pending_cycle = None
        self.observed_batch_id = None
        self.trace = []

    def _get_or_create_session(self, session_id: str):
        return SimpleNamespace(pending_cycle=self.pending_cycle)

    async def process_query(self, *args, **kwargs):
        active_cycle = kwargs.pop("active_cycle")
        self._activate_manager_context(
            active_cycle=active_cycle,
            state=SimpleNamespace(),
            session_id=kwargs.get("session_id", "default"),
            progress_callback=None,
        )
        return active_cycle

    def _activate_manager_context(
        self,
        *,
        active_cycle,
        state,
        session_id,
        progress_callback,
    ):
        self.observed_batch_id = active_cycle.original_input_batch_id
        return SimpleNamespace(active_cycle=active_cycle)

    @staticmethod
    def _trace_event(trace, event_type: str, **data):
        trace.append({"type": event_type, **data})


class _Client(WaitingUserBatchContinuationMixin, _BaseClient):
    pass


class WaitingUserBatchContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_cycle_accepts_new_committed_batch(self):
        client = _Client()
        client.pending_cycle = SimpleNamespace(status="waiting_user")
        active_cycle = SimpleNamespace(
            original_input_batch_id="ibat-old",
            cycle_trace=client.trace,
        )
        input_batch = SimpleNamespace(
            input_batch_id="ibat-new",
            artifact_refs=["art-new"],
            text_parts=[SimpleNamespace()],
        )

        result = await client.process_query(
            "payload",
            session_id="session-1",
            input_batch=input_batch,
            active_cycle=active_cycle,
        )

        self.assertIs(result, active_cycle)
        self.assertEqual(client.observed_batch_id, "ibat-new")
        self.assertEqual(
            client.trace[-1],
            {
                "type": "waiting_user_input_batch_continued",
                "previous_input_batch_id": "ibat-old",
                "input_batch_id": "ibat-new",
                "artifact_count": 1,
                "text_part_count": 1,
            },
        )

    async def test_fresh_cycle_does_not_bypass_batch_guard(self):
        client = _Client()
        active_cycle = SimpleNamespace(
            original_input_batch_id="ibat-old",
            cycle_trace=client.trace,
        )
        input_batch = SimpleNamespace(
            input_batch_id="ibat-new",
            artifact_refs=[],
            text_parts=[],
        )

        await client.process_query(
            "payload",
            session_id="session-1",
            input_batch=input_batch,
            active_cycle=active_cycle,
        )

        self.assertEqual(client.observed_batch_id, "ibat-old")
        self.assertEqual(client.trace, [])


if __name__ == "__main__":
    unittest.main()
