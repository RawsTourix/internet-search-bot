import unittest
from types import SimpleNamespace

from src.mcp.fresh_task_boundary import FreshTaskBoundaryMixin


class _Client(FreshTaskBoundaryMixin):
    def __init__(self):
        self.session = SimpleNamespace(
            pending_cycle=None,
            last_cycle_trace=[],
        )

    def _get_or_create_session(self, session_id: str):
        self.last_session_id = session_id
        return self.session

    @staticmethod
    def _trace_event(trace, event_type: str, **data):
        trace.append({"type": event_type, **data})


class FreshTaskBoundaryTests(unittest.TestCase):
    def test_new_collection_abandons_only_pending_cycle(self):
        client = _Client()
        trace = [{"type": "waiting_user"}]
        client.session.pending_cycle = SimpleNamespace(
            cycle_id="cycle-old",
            cycle_trace=trace,
        )

        cycle_id = client.abandon_pending_cycle_for_new_task(
            "telegram:conversation:chat-1",
            reason="explicit_collection_started",
        )

        self.assertEqual(cycle_id, "cycle-old")
        self.assertEqual(client.last_session_id, "telegram:conversation:chat-1")
        self.assertIsNone(client.session.pending_cycle)
        self.assertEqual(client.session.last_cycle_trace, trace)
        self.assertEqual(
            trace[-1],
            {
                "type": "pending_cycle_abandoned",
                "cycle_id": "cycle-old",
                "reason": "explicit_collection_started",
            },
        )

    def test_no_pending_cycle_is_a_noop(self):
        client = _Client()

        cycle_id = client.abandon_pending_cycle_for_new_task(
            "telegram:conversation:chat-1",
            reason="explicit_collection_started",
        )

        self.assertIsNone(cycle_id)
        self.assertEqual(client.session.last_cycle_trace, [])


if __name__ == "__main__":
    unittest.main()
