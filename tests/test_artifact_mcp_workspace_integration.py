import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.artifacts import (
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactValidationError,
    apply_local_workspace_server_policy,
    create_artifact_services,
)
from src.mcp.artifact_client import ArtifactMCPClient
from src.mcp.manager_context import ManagerToolContext
from src.mcp.manager_runtime_context import set_manager_context
from src.mcp.mcp_client import (
    LLMConfigType,
    MCPToolBinding,
    ServerConfigType,
    ServerConnectType,
    SessionState,
)
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


class ArtifactMCPWorkspaceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
        self.storage = create_storage_services(self.storage_config)
        self.artifact_config = ArtifactConfigType(
            max_artifact_size_bytes=1024 * 1024,
            max_patchable_text_bytes=1024 * 1024,
            max_workspace_bytes=2 * 1024 * 1024,
            local_workspace_server_names=["processor"],
        )
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=self.artifact_config,
            content_store=self.storage.content_store,
        )
        self.client = ArtifactMCPClient(
            llm_config(),
            storage_services=self.storage,
            artifact_services=self.artifacts,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Process the file",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.state = SessionState(progress_locale="en")
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=self.state,
        )
        set_manager_context(self.context)
        artifact = await self.artifacts.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="input.md",
            text="source text",
            format_id="markdown",
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="test_create",
            ),
            purpose=ArtifactPurpose.WORKING,
        )
        self.artifact = artifact
        self.cycle.artifact_refs.append(artifact.artifact_id)
        self.client.tool_registry["process_file"] = MCPToolBinding(
            public_name="process_file",
            server_name="processor",
            server_alias="processor",
            remote_name="process_file",
            description="Process one local file",
            input_schema={"type": "object"},
        )

    async def asyncTearDown(self):
        set_manager_context(None)
        await self.client.cleanup()
        self.temporary.cleanup()

    def _set_server(self, *, connect_type=ServerConnectType.EXECUTABLE, allow=True):
        server = ServerConfigType(
            name="processor",
            alias="processor",
            connect_type=connect_type,
            url=(
                "https://example.invalid/mcp/"
                if connect_type == ServerConnectType.STREAMABLE_HTTP
                else None
            ),
        )
        policy = self.artifact_config.model_copy(
            update={
                "local_workspace_server_names": ["processor"] if allow else []
            }
        )
        apply_local_workspace_server_policy([server], policy)
        self.client.server_configs_by_name = {"processor": server}
        return server

    async def test_local_processor_creates_candidate_and_hides_workspace_path(self):
        self._set_server()
        observed: dict[str, object] = {}

        async def process(tool_name, arguments):
            self.assertEqual(tool_name, "process_file")
            input_path = Path(arguments["input_file"])
            observed["path"] = input_path
            observed["text"] = input_path.read_text(encoding="utf-8")
            workspace_root = input_path.parent.parent
            output = workspace_root / "outputs" / "result.md"
            output.write_text("processed output", encoding="utf-8")
            return SimpleNamespace(
                content=[SimpleNamespace(text=f"processed {input_path}")]
            )

        self.client.server_manager = SimpleNamespace(
            call_tool=AsyncMock(side_effect=process)
        )
        payload = await self.client._manager_call_tool({
            "tool_name": "process_file",
            "arguments": {},
            "result_handling": "auto",
            "artifact_bindings": [
                {
                    "artifact_id": self.artifact.artifact_id,
                    "argument_pointer": "/input_file",
                    "representation": "local_file",
                }
            ],
            "artifact_outputs": [
                {
                    "relative_path": "result.md",
                    "suggested_filename": "result.md",
                }
            ],
        })

        self.assertEqual(observed["text"], "source text")
        input_path = observed["path"]
        self.assertIsInstance(input_path, Path)
        self.assertFalse(input_path.parent.parent.exists())
        self.assertEqual(payload["type"], "tool_result")
        self.assertEqual(len(payload["artifact_candidates"]), 1)
        candidate_ref = payload["artifact_candidates"][0]
        candidate_id = candidate_ref["candidate_id"]
        self.assertIn(candidate_id, self.cycle.artifact_candidate_refs)
        candidate = await self.artifacts.candidate_store.get(candidate_id)
        self.assertEqual(candidate.suggested_filename, "result.md")
        self.assertEqual(
            await self.storage.content_store.read_content(candidate.content_id),
            b"processed output",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(input_path.parent.parent), serialized)
        self.assertIn("[ARTIFACT_WORKSPACE]", serialized)
        event_types = [event.get("type") for event in self.state.progress_events]
        self.assertIn("artifact_tool_input_materialized", event_types)
        self.assertIn("artifact_candidate_saved", event_types)
        self.assertIn("artifact_tool_input_released", event_types)
        self.assertFalse(any(
            str(input_path.parent.parent) in json.dumps(event, ensure_ascii=False)
            for event in self.state.progress_events
        ))

    async def test_non_allowed_server_is_rejected_before_tool_call(self):
        self._set_server(allow=False)
        self.client.server_manager = SimpleNamespace(call_tool=AsyncMock())

        with self.assertRaises(ArtifactValidationError) as caught:
            await self.client._manager_call_tool({
                "tool_name": "process_file",
                "arguments": {},
                "artifact_bindings": [
                    {
                        "artifact_id": self.artifact.artifact_id,
                        "argument_pointer": "/input_file",
                    }
                ],
            })

        self.assertEqual(caught.exception.code, "artifact_transport_not_supported")
        self.client.server_manager.call_tool.assert_not_awaited()

    async def test_remote_server_is_rejected_by_policy(self):
        with self.assertRaises(Exception):
            self._set_server(connect_type=ServerConnectType.STREAMABLE_HTTP)
        self.assertEqual(list(self.artifacts.workspace_manager.root.iterdir()), [])

    async def test_plain_mcp_call_keeps_base_contract(self):
        self._set_server(allow=False)
        remote_result = SimpleNamespace(
            content=[SimpleNamespace(text="plain result")]
        )
        self.client.server_manager = SimpleNamespace(
            call_tool=AsyncMock(return_value=remote_result)
        )

        payload = await self.client._manager_call_tool({
            "tool_name": "process_file",
            "arguments": {"value": 1},
            "result_handling": "compact",
        })

        self.client.server_manager.call_tool.assert_awaited_once_with(
            "process_file",
            {"value": 1},
        )
        self.assertEqual(payload["content"], "plain result")
        self.assertNotIn("artifact_candidates", payload)
        self.assertEqual(list(self.artifacts.workspace_manager.root.iterdir()), [])

    def test_mcp_call_schema_has_no_local_refs(self):
        schema = next(
            item["function"]["parameters"]
            for item in self.client._format_tools_for_llm()
            if item["function"]["name"] == "mcp_call_tool"
        )
        serialized = json.dumps(schema)
        self.assertNotIn('"$ref"', serialized)
        self.assertNotIn('"$defs"', serialized)
        self.assertIn("artifact_bindings", schema["properties"])
        self.assertIn("artifact_outputs", schema["properties"])


if __name__ == "__main__":
    unittest.main()
