import unittest
from types import SimpleNamespace

from src.mcp.artifact_request_context import (
    reset_artifact_request_input_batch,
    set_artifact_request_input_batch,
)
from src.mcp.waiting_user_batch_continuation import (
    WaitingUserBatchContinuationMixin,
)


class _GuardedBaseClient:
    def __init__(self):
        self.pending_cycle = None
        self.observed_batch_id = None
        self.trace = []

    def _get_or_create_session(self, session_id: str):
        return SimpleNamespace(pending_cycle=self.pending_cycle)

    async def process_query(self, *args, **kwargs):
        active_cycle = kwargs.pop("active_cycle")
        input_batch = kwargs.pop("input_batch", None)
        token = set_artifact_request_input_batch(input_batch)
        try:
            self._activate_manager_context(
                active_cycle=active_cycle,
                state=SimpleNamespace(),
                session_id=kwargs.get("session_id", "default"),
                progress_callback=None,
            )
        finally:
            reset_artifact_request_input_batch(token)
        return active_cycle

    def _activate_manager_context(
        self,
        *,
        active_cycle,
        state,
        session_id,
        progress_callback,
    ):
        from src.mcp.artifact_request_context import (
            get_artifact_request_input_batch,
        )

        input_batch = get_artifact_request_input_batch()
        if input_batch is not None:
            existing = active_cycle.original_input_batch_id
            if existing is not None and existing != input_batch.input_batch_id:
                raise RuntimeError(
                    "Additional committed batches require CycleInbox runtime"
                )
            active_cycle.original_input_batch_id = input_batch.input_batch_id
            for artifact_id in input_batch.artifact_refs:
                if artifact_id not in active_cycle.artifact_refs:
                    active_cycle.artifact_refs.append(artifact_id)
        self.observed_batch_id = active_cycle.original_input_batch_id
        return SimpleNamespace(active_cycle=active_cycle)

    @staticmethod
    def _trace_event(trace, event_type: str, **data):
        trace.append({"type": event_type, **data})


class _Client(WaitingUserBatchContinuationMixin, _GuardedBaseClient):
    pass


class WaitingUserBatchContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_cycle_preserves_initial_identity_for_fifo_runtime(self):
        client = _Client()
        client.pending_cycle = SimpleNamespace(status="waiting_user")
        active_cycle = SimpleNamespace(
            original_input_batch_id="ibat-old",
            artifact_refs=["art-old"],
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
        self.assertEqual(client.observed_batch_id, "ibat-old")
        self.assertEqual(active_cycle.original_input_batch_id, "ibat-old")
        self.assertEqual(active_cycle.artifact_refs, ["art-old"])
        self.assertEqual(client.trace, [])

    async def test_interrupted_cycle_preserves_initial_identity_for_fifo_runtime(
        self,
    ):
        client = _Client()
        client.pending_cycle = SimpleNamespace(status="interrupted")
        active_cycle = SimpleNamespace(
            original_input_batch_id="ibat-before-timeout",
            artifact_refs=["art-uploaded-file"],
            cycle_trace=client.trace,
        )
        input_batch = SimpleNamespace(
            input_batch_id="ibat-resume-message",
            artifact_refs=[],
            text_parts=[SimpleNamespace()],
        )

        result = await client.process_query(
            "resume",
            session_id="session-1",
            input_batch=input_batch,
            active_cycle=active_cycle,
        )

        self.assertIs(result, active_cycle)
        self.assertEqual(client.observed_batch_id, "ibat-before-timeout")
        self.assertEqual(
            active_cycle.original_input_batch_id,
            "ibat-before-timeout",
        )
        self.assertEqual(active_cycle.artifact_refs, ["art-uploaded-file"])
        self.assertEqual(client.trace, [])

    async def test_fresh_cycle_does_not_bypass_batch_guard(self):
        client = _Client()
        active_cycle = SimpleNamespace(
            original_input_batch_id="ibat-old",
            artifact_refs=[],
            cycle_trace=client.trace,
        )
        input_batch = SimpleNamespace(
            input_batch_id="ibat-new",
            artifact_refs=[],
            text_parts=[],
        )

        with self.assertRaisesRegex(RuntimeError, "CycleInbox runtime"):
            await client.process_query(
                "payload",
                session_id="session-1",
                input_batch=input_batch,
                active_cycle=active_cycle,
            )

        self.assertEqual(active_cycle.original_input_batch_id, "ibat-old")
        self.assertEqual(client.trace, [])


if __name__ == "__main__":
    unittest.main()
