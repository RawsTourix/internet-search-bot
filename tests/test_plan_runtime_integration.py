import json
import tempfile
import unittest
from pathlib import Path

from src.agent.protocol import AgentAction
from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.mcp.planning_client import PlanningMCPClient
from src.mcp.planning_runtime import FinalizingPlanningMCPClient
from src.planning import PlanningConfigType, create_planning_services
from src.planning.runtime_context import set_manager_context
from src.planning.tools import PLAN_TOOL_NAMES
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class PlanRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        planning_config = PlanningConfigType()
        self.storage_services = create_storage_services(storage_config)
        self.planning_services = create_planning_services(
            storage_config=storage_config,
            planning_config=planning_config,
        )
        self.client = PlanningMCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                api_key="test",
                model="test-model",
                max_tokens=256,
                context_window_tokens=4096,
            ),
            storage_services=self.storage_services,
            planning_services=self.planning_services,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Complex task",
            messages_for_llm=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "request"},
            ],
            cycle_trace=[],
            original_user_message_index=1,
        )
        self.state = SessionState()
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=self.state,
        )
        set_manager_context(self.context)

    async def asyncTearDown(self):
        set_manager_context(None)
        await self.client.cleanup()
        self.temporary.cleanup()

    async def _create_plan(self):
        outcome = await self.client.plan_tool_controller.execute(
            "agent_plan_create",
            {
                "goal": "Collect then validate",
                "nodes": [
                    {
                        "client_key": "collect",
                        "title": "Collect",
                        "objective": "Collect data",
                        "kind": "collect",
                        "depends_on": [],
                        "success_criteria": [],
                    }
                ],
            },
            self.context,
        )
        return outcome.payload

    def test_plan_tools_are_registered(self):
        self.assertTrue(PLAN_TOOL_NAMES.issubset(self.client.manager_tools))
        formatted_names = {
            item["function"]["name"]
            for item in self.client._format_tools_for_llm()
        }
        self.assertTrue(PLAN_TOOL_NAMES.issubset(formatted_names))

    def test_planning_tool_schemas_have_no_local_refs(self):
        for item in self.client._format_tools_for_llm():
            if item["function"]["name"] not in PLAN_TOOL_NAMES:
                continue
            serialized = json.dumps(item["function"]["parameters"])
            self.assertNotIn('"$ref"', serialized)
            self.assertNotIn('"$defs"', serialized)

    async def test_planning_disabled_omits_plan_tools(self):
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(
            root_dir=str(root / "disabled-storage")
        )
        storage_services = create_storage_services(storage_config)
        services = create_planning_services(
            storage_config=storage_config,
            planning_config=PlanningConfigType(enabled=False),
        )
        client = PlanningMCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                api_key="test",
                model="test-model",
                max_tokens=256,
                context_window_tokens=4096,
            ),
            storage_services=storage_services,
            planning_services=services,
        )
        try:
            self.assertTrue(PLAN_TOOL_NAMES.isdisjoint(client.manager_tools))
            formatted_names = {
                item["function"]["name"]
                for item in client._format_tools_for_llm()
            }
            self.assertTrue(PLAN_TOOL_NAMES.isdisjoint(formatted_names))
        finally:
            await client.cleanup()

    async def test_runtime_message_contains_bounded_active_plan_state(self):
        await self._create_plan()
        self.cycle.activity = self.client._activity_for_state(
            self.cycle.active_plan_state
        )
        payload = self.client._iteration_runtime_payload(self.state)
        self.assertEqual(payload["activity"], "planning")
        self.assertEqual(
            payload["active_plan_state"]["plan_id"],
            self.cycle.active_plan_id,
        )

    async def test_mcp_call_is_blocked_without_in_progress_node(self):
        await self._create_plan()
        result = await self.client._call_registered_tool(
            "mcp_call_tool",
            {"tool_name": "remote_tool", "arguments": {}},
        )
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["type"], "plan_node_required")

    async def test_final_answer_is_reconciled_while_plan_is_active(self):
        await self._create_plan()
        response = {
            "content": AgentAction(
                status="done",
                action="answer",
                final_answer="Premature answer",
            ).model_dump_json(),
            "tool_calls": [],
        }
        guarded = await self.client._apply_plan_action_guard(
            response=response,
            context=self.context,
        )
        action = AgentAction.model_validate_json(guarded["content"])
        self.assertEqual(action.status, "running")
        self.assertEqual(action.action, "continue")
        self.assertTrue(any(
            "plan_reconciliation_required" in str(message.get("content"))
            for message in self.cycle.messages_for_llm
        ))

    async def test_reconciliation_limit_returns_resumable_user_handoff(self):
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "final-storage"))
        storage_services = create_storage_services(storage_config)
        services = create_planning_services(
            storage_config=storage_config,
            planning_config=PlanningConfigType(max_reconciliation_attempts=1),
        )
        client = FinalizingPlanningMCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                api_key="test",
                model="test-model",
                max_tokens=256,
                context_window_tokens=4096,
            ),
            storage_services=storage_services,
            planning_services=services,
        )
        cycle = ActiveAgentCycle(
            cycle_id="cycle-final",
            session_id="session-final",
            original_user_request="Complex task",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        state = SessionState()
        context = ManagerToolContext(
            session_id="session-final",
            cycle_id="cycle-final",
            active_cycle=cycle,
            session_state=state,
        )
        set_manager_context(context)
        try:
            created = await client.plan_tool_controller.execute(
                "agent_plan_create",
                {
                    "goal": "Unfinished work",
                    "nodes": [
                        {
                            "client_key": "work",
                            "title": "Work",
                            "objective": "Do the work",
                            "kind": "execute",
                            "depends_on": [],
                            "success_criteria": [],
                        }
                    ],
                },
                context,
            )
            self.assertEqual(created.payload["type"], "plan_created")
            response = {
                "content": AgentAction(
                    status="done",
                    action="answer",
                    final_answer="Premature",
                ).model_dump_json(),
                "tool_calls": [],
            }
            first = await client._apply_plan_action_guard(
                response=response,
                context=context,
            )
            self.assertEqual(
                AgentAction.model_validate_json(first["content"]).status,
                "running",
            )
            second = await client._apply_plan_action_guard(
                response=response,
                context=context,
            )
            handoff = AgentAction.model_validate_json(second["content"])
            self.assertEqual(handoff.status, "waiting_user")
            self.assertEqual(handoff.action, "ask_user")
            self.assertIn("Продолжить выполнение", handoff.question_to_user)
        finally:
            set_manager_context(self.context)
            await client.cleanup()

    async def test_waiting_user_requires_blocked_node(self):
        created = await self._create_plan()
        node_id = created["node_id_map"]["collect"]
        await self.client.plan_tool_controller.execute(
            "agent_plan_transition_node",
            {
                "plan_id": created["plan_id"],
                "expected_revision": 1,
                "node_id": node_id,
                "transition": "start",
                "result_refs": [],
                "artifact_refs": [],
            },
            self.context,
        )
        response = {
            "content": AgentAction(
                status="waiting_user",
                action="ask_user",
                question_to_user="Need more data",
            ).model_dump_json(),
            "tool_calls": [],
        }
        guarded = await self.client._apply_plan_action_guard(
            response=response,
            context=self.context,
        )
        action = AgentAction.model_validate_json(guarded["content"])
        self.assertEqual(action.status, "running")
        self.assertTrue(any(
            "plan_waiting_user_reconciliation_required"
            in str(message.get("content"))
            for message in self.cycle.messages_for_llm
        ))
