import unittest
from types import SimpleNamespace

from src.mcp.artifact_history_isolation import ArtifactHistoryIsolationMixin
from src.mcp.artifact_request_context import (
    reset_artifact_request_input_batch,
    set_artifact_request_input_batch,
)


class _LegacyHandoffBase:
    def _activate_manager_context(
        self,
        *,
        active_cycle,
        state,
        session_id,
        progress_callback,
    ):
        del state, progress_callback
        active_cycle.artifact_refs.extend(
            artifact_id
            for artifact_id in ("art_historical", "art_historical_2")
            if artifact_id not in active_cycle.artifact_refs
        )
        return SimpleNamespace(
            session_id=session_id,
            active_cycle=active_cycle,
        )

    def _trace_event(self, trace, event_type, **data):
        trace.append({"type": event_type, **data})


class _IsolatedClient(ArtifactHistoryIsolationMixin, _LegacyHandoffBase):
    pass


class ArtifactHistoryIsolationTests(unittest.TestCase):
    def test_fresh_cycle_removes_implicit_previous_cycle_refs(self):
        client = _IsolatedClient()
        cycle = SimpleNamespace(
            artifact_refs=["art_current_input"],
            cycle_trace=[],
        )
        context = client._activate_manager_context(
            active_cycle=cycle,
            state=SimpleNamespace(),
            session_id="session-1",
            progress_callback=None,
        )

        self.assertEqual(context.active_cycle.artifact_refs, ["art_current_input"])
        self.assertEqual(
            context.active_cycle.cycle_trace[-1]["type"],
            "artifact_implicit_history_removed",
        )
        self.assertEqual(
            context.active_cycle.cycle_trace[-1]["artifact_ids"],
            ["art_historical", "art_historical_2"],
        )

    def test_current_input_and_explicit_references_remain_authorized(self):
        client = _IsolatedClient()
        cycle = SimpleNamespace(
            artifact_refs=["art_created_in_cycle"],
            cycle_trace=[],
        )
        input_batch = SimpleNamespace(
            artifact_refs=["art_input"],
            referenced_artifact_refs=["art_explicit_reference"],
        )
        token = set_artifact_request_input_batch(input_batch)
        try:
            context = client._activate_manager_context(
                active_cycle=cycle,
                state=SimpleNamespace(),
                session_id="session-1",
                progress_callback=None,
            )
        finally:
            reset_artifact_request_input_batch(token)

        self.assertEqual(
            context.active_cycle.artifact_refs,
            [
                "art_created_in_cycle",
                "art_input",
                "art_explicit_reference",
            ],
        )


if __name__ == "__main__":
    unittest.main()
