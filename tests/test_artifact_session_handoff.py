import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from src.mcp.artifact_request_context import (
    reset_artifact_request_input_batch,
    set_artifact_request_input_batch,
)
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.planning import PlanningConfigType, create_planning_services
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


def llm_config() -> LLMConfigType:
    return LLMConfigType(
        api_url="https://example.invalid/v1/chat/completions",
        api_key="test",
        model="test-model",
        max_tokens=256,
        context_window_tokens=4096,
    )


def cycle(cycle_id: str, session_id: str) -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id=cycle_id,
        session_id=session_id,
        original_user_request="work with the file",
        messages_for_llm=[
            {"role": "user", "content": "work with the file"}
        ],
        cycle_trace=[],
        original_user_message_index=0,
    )


class ArtifactSessionHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        planning = create_planning_services(
            storage_config=storage_config,
            planning_config=PlanningConfigType(),
        )
        self.artifacts = artifacts
        self.client = FinalizingArtifactDeliveryPlanningMCPClient(
            llm_config(),
            storage_services=storage,
            artifact_services=artifacts,
            planning_services=planning,
        )

    async def asyncTearDown(self):
        await self.client.cleanup()
        self.temporary.cleanup()

    async def test_previous_cycle_artifact_is_available_in_same_session(self):
        first_cycle = cycle("cycle-1", "session-1")
        first_state = SessionState()
        self.client._activate_manager_context(
            active_cycle=first_cycle,
            state=first_state,
            session_id="session-1",
            progress_callback=None,
        )
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "source.md",
                "text": "STATUS: draft",
                "format_id": "markdown",
            },
        )
        artifact_id = json.loads(created.content[0].text)["artifact"]["artifact_id"]

        session = self.client._get_or_create_session("session-1")
        self.client._append_dialog_turn(
            session,
            user_request="read source.md",
            final_answer="done",
            state=first_state,
        )

        second_cycle = cycle("cycle-2", "session-1")
        second_state = SessionState()
        self.client._activate_manager_context(
            active_cycle=second_cycle,
            state=second_state,
            session_id="session-1",
            progress_callback=None,
        )

        self.assertIn(artifact_id, second_cycle.artifact_refs)
        self.assertEqual(
            second_cycle.cycle_trace[-1]["type"],
            "artifact_authority_inherited",
        )

        result = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["type"], "artifact_batch_read")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["items"][0]["artifact"]["artifact_id"],
            artifact_id,
        )
        self.assertEqual(payload["items"][0]["text"], "STATUS: draft")

    async def test_input_batch_ref_is_saved_applied_and_traced(self):
        source_cycle = cycle("cycle-input", "session-input")
        source_state = SessionState()
        self.client._activate_manager_context(
            active_cycle=source_cycle,
            state=source_state,
            session_id="session-input",
            progress_callback=None,
        )
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "summary_report.md",
                "text": "Launch status: CONDITIONAL GO",
                "format_id": "markdown",
            },
        )
        artifact_id = json.loads(created.content[0].text)["artifact"]["artifact_id"]

        # Reproduce the explicit-input edge: the committed batch is the direct
        # source of authority even if a caller has not retained a second copy in
        # the active-cycle list at dialog-turn persistence time.
        source_cycle.artifact_refs.clear()
        input_batch = SimpleNamespace(
            input_batch_id="ibat-input",
            session_id="session-input",
            artifact_refs=[artifact_id],
        )
        token = set_artifact_request_input_batch(input_batch)
        try:
            session = self.client._get_or_create_session("session-input")
            self.client._append_dialog_turn(
                session,
                user_request="analyse the uploaded report",
                final_answer="done",
                state=source_state,
            )
        finally:
            reset_artifact_request_input_batch(token)

        self.assertEqual(
            self.client._session_artifact_handoffs["session-input"],
            [artifact_id],
        )
        await self.client._trace_handoff_saved(
            session_id="session-input",
            cycle_id="cycle-input",
        )

        next_cycle = cycle("cycle-next", "session-input")
        next_state = SessionState()
        next_context = self.client._activate_manager_context(
            active_cycle=next_cycle,
            state=next_state,
            session_id="session-input",
            progress_callback=None,
        )
        await self.client._refresh_artifact_state(next_context)

        self.assertIn(artifact_id, next_cycle.artifact_refs)
        self.assertEqual(
            next_cycle.artifact_state.input_manifest.items[0].filename,
            "summary_report.md",
        )
        read = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        payload = json.loads(read.content[0].text)
        self.assertEqual(
            payload["items"][0]["text"],
            "Launch status: CONDITIONAL GO",
        )

        events = await self.artifacts.trace_service.list_session(
            "session-input"
        )
        event_types = [item.event_type for item in events]
        self.assertIn("artifact_handoff_saved", event_types)
        self.assertIn("artifact_handoff_applied", event_types)
        applied = next(
            item for item in events
            if item.event_type == "artifact_handoff_applied"
        )
        self.assertEqual(applied.data["artifact_ids"], [artifact_id])

    async def test_manager_tool_routing_returns_corrective_receipt(self):
        result = await self.client._manager_call_tool({
            "tool_name": "artifact_create_text",
            "arguments": {
                "filename": "wrong-route.md",
                "text": "payload",
                "format_id": "markdown",
            },
            "result_handling": "auto",
        })

        self.assertEqual(result["type"], "invalid_tool_routing")
        self.assertEqual(result["target_tool_name"], "artifact_create_text")
        self.assertTrue(result["retryable"])
        self.assertIn("directly", result["corrective_action"])

    async def test_history_does_not_cross_session_boundary(self):
        first_cycle = cycle("cycle-1", "session-1")
        first_state = SessionState()
        self.client._activate_manager_context(
            active_cycle=first_cycle,
            state=first_state,
            session_id="session-1",
            progress_callback=None,
        )
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "private.md",
                "text": "private",
                "format_id": "markdown",
            },
        )
        artifact_id = json.loads(created.content[0].text)["artifact"]["artifact_id"]
        session = self.client._get_or_create_session("session-1")
        self.client._append_dialog_turn(
            session,
            user_request="create private.md",
            final_answer="done",
            state=first_state,
        )

        other_cycle = cycle("cycle-2", "session-2")
        self.client._activate_manager_context(
            active_cycle=other_cycle,
            state=SessionState(),
            session_id="session-2",
            progress_callback=None,
        )
        self.assertNotIn(artifact_id, other_cycle.artifact_refs)
        catalog = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "session", "limit": 10},
        )
        payload = json.loads(catalog.content[0].text)
        self.assertEqual(payload["items"], [])

    async def test_clear_session_removes_handoff_and_preserves_other_session(self):
        first_cycle = cycle("cycle-1", "session-1")
        first_state = SessionState()
        self.client.session_states["session-1"] = first_state
        self.client._activate_manager_context(
            active_cycle=first_cycle,
            state=first_state,
            session_id="session-1",
            progress_callback=None,
        )
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "reset-me.md",
                "text": "must not survive reset authority",
                "format_id": "markdown",
            },
        )
        artifact_id = json.loads(created.content[0].text)["artifact"]["artifact_id"]
        first_session = self.client._get_or_create_session("session-1")
        self.client._append_dialog_turn(
            first_session,
            user_request="create reset-me.md",
            final_answer="done",
            state=first_state,
        )

        other_session = self.client._get_or_create_session("session-2")
        other_state = SessionState()
        self.client.session_states["session-2"] = other_state
        self.client._session_artifact_handoffs["session-2"] = ["artifact-other"]

        self.assertIn(artifact_id, self.client._session_artifact_handoffs["session-1"])
        self.client.clear_session("session-1")

        self.assertNotIn("session-1", self.client.sessions)
        self.assertNotIn("session-1", self.client.session_states)
        self.assertNotIn("session-1", self.client._session_artifact_handoffs)
        self.assertIs(self.client.sessions["session-2"], other_session)
        self.assertIs(self.client.session_states["session-2"], other_state)
        self.assertEqual(
            self.client._session_artifact_handoffs["session-2"],
            ["artifact-other"],
        )

        next_cycle = cycle("cycle-2", "session-1")
        self.client._activate_manager_context(
            active_cycle=next_cycle,
            state=SessionState(),
            session_id="session-1",
            progress_callback=None,
        )
        self.assertNotIn(artifact_id, next_cycle.artifact_refs)
        self.assertFalse(
            any(
                event.get("type") == "artifact_authority_inherited"
                for event in next_cycle.cycle_trace
            )
        )


if __name__ == "__main__":
    unittest.main()
