from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.core.models import AgentStatus
from src.input_runtime import (
    CheckpointName,
    ControlState,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.mcp.input_runtime_checkpoint_hardening import InputRuntimeCheckpointHardeningMixin
from src.mcp.input_runtime_checkpoints import InputRuntimeCheckpointMixin
from src.mcp.input_runtime_controls import InputRuntimeControlMixin
from src.mcp.mcp_client import MCPClient, ResultHandling
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 10
    text_parts: list[object] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "a" * 32,)
    content_fingerprint: str = "sha256:" + "b" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, batch: Batch):
        self.batch = batch

    async def get_committed(self, input_batch_id: str):
        assert input_batch_id == self.batch.input_batch_id
        return self.batch


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


class ProductionToolSupport:
    """Replace external seams while keeping MCPClient.process_query tool loop."""

    def __init__(self, *, service, active_cycle):
        self.service = service
        self._input_runtime_checkpoint_service = service.checkpoint_service
        self._input_runtime_seen_cycles = set()
        self._session = SimpleNamespace(pending_cycle=active_cycle)
        self._state = SimpleNamespace(
            status=AgentStatus.IDLE,
            last_seen=0.0,
            iterations=0,
            tools_used=[],
            last_error=None,
            awaiting_user_input=False,
            progress_events=[],
            active_tool=None,
            progress_locale="ru",
        )
        self.max_iterations = 2
        self.tool_call_timeout = 30.0
        self.llm_calls = 0
        self.tool_calls: list[str] = []
        self.second_tool_entered = asyncio.Event()
        self.second_tool_release = asyncio.Event()

    def _checkpoint_service(self):
        return self.service.checkpoint_service

    def _get_or_create_state(self, _session_id):
        return self._state

    def _get_or_create_session(self, _session_id):
        return self._session

    @staticmethod
    def _normalize_progress_locale(value):
        return value or "ru"

    @staticmethod
    def _trace_event(trace, event_type, **data):
        trace.append({"type": event_type, **data})

    async def _emit_progress_event(self, *args, **kwargs):
        return None

    @staticmethod
    def _format_tools_for_llm():
        return []

    @staticmethod
    def _estimate_main_request_tokens(**kwargs):
        return 1

    async def _compact_context_if_needed(self, *, active_cycle, **kwargs):
        return SimpleNamespace(messages_for_llm=active_cycle.messages_for_llm)

    async def _call_main_llm_with_context_recovery(self, **kwargs):
        self.llm_calls += 1
        assert self.llm_calls == 1, "pause must prevent a second LLM request"
        return (
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "tool_one", "arguments": "{}"},
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {"name": "tool_two", "arguments": "{}"},
                    },
                ],
            },
            kwargs["active_cycle"].messages_for_llm,
        )

    @staticmethod
    def _observe_main_llm_usage(**kwargs):
        return None

    @staticmethod
    def _resolve_effective_tool_context(tool_name, arguments):
        return tool_name, arguments, ResultHandling.AUTO

    @staticmethod
    def _resolve_progress_tool_names(tool_name, arguments):
        return tool_name, None

    @staticmethod
    def _record_tool_used(state, tool_name, arguments):
        if tool_name not in state.tools_used:
            state.tools_used.append(tool_name)

    @staticmethod
    def _tool_start_message(tool_name, arguments, *, progress_locale):
        return tool_name

    @staticmethod
    def _resolve_progress_server_name(_target_tool_name):
        return None

    async def _call_registered_tool(self, tool_name, arguments):
        self.tool_calls.append(tool_name)
        if tool_name == "tool_two":
            self.second_tool_entered.set()
            await self.second_tool_release.wait()
        return SimpleNamespace(content=[SimpleNamespace(text=f"{tool_name}-result")])

    @staticmethod
    def _format_tool_result(content):
        return "\n".join(str(getattr(item, "text", item)) for item in content)

    @staticmethod
    def _tool_result_payload(tool_name, tool_result):
        return {"type": "tool_result", "tool_name": tool_name, "result": tool_result}

    async def _process_tool_result_for_context(
        self,
        *,
        tool_call_id,
        tool_payload,
        messages_for_llm,
        **kwargs,
    ):
        messages_for_llm.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(tool_payload),
            }
        )
        return SimpleNamespace(
            stored_result_ref=None,
            persistence_failed=False,
            visible_payload=tool_payload,
        )


class ProductionToolLoopHarness(
    InputRuntimeControlMixin,
    InputRuntimeCheckpointHardeningMixin,
    InputRuntimeCheckpointMixin,
    ProductionToolSupport,
    MCPClient,
):
    def __init__(self, *, service, active_cycle):
        ProductionToolSupport.__init__(self, service=service, active_cycle=active_cycle)


@pytest.mark.asyncio
async def test_pause_during_production_multitool_loop_finishes_block_before_checkpoint(
    tmp_path, monkeypatch
):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(max_batches_per_checkpoint=1),
        repositories=repositories,
        committed_batches=Reader(Batch("initial")),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    admission = await service.admit_committed_batch("initial", session_id="session")
    assert admission.target_cycle_id == "cycle-a"
    active = ActiveAgentCycle(
        cycle_id="cycle-a",
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": json.dumps({"type": "user_request", "user_request": "initial"})},
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    # process_query resumes this exact process-local active cycle; the empty
    # compatibility resume message is dropped before the durable checkpoint.
    active.status = CycleStatus.WAITING_USER.value
    active.waiting_question = "runtime test resume"

    binding = SimpleNamespace(
        config=SimpleNamespace(enabled=True),
        repositories=repositories,
        checkpoint_service=service.checkpoint_service,
    )
    monkeypatch.setattr(
        "src.mcp.input_runtime_checkpoints.get_input_runtime_binding",
        lambda: binding,
    )
    monkeypatch.setattr(
        "src.mcp.input_runtime_checkpoint_hardening.get_input_runtime_binding",
        lambda: binding,
    )
    monkeypatch.setattr(
        "src.mcp.input_runtime_controls.get_input_runtime_binding",
        lambda: binding,
    )

    harness = ProductionToolLoopHarness(service=service, active_cycle=active)
    task = asyncio.create_task(harness.process_query("", session_id="session"))
    await harness.second_tool_entered.wait()

    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-production-tool-loop",
        source_client_type="test",
        source_message_ref={"message_id": 700},
    )
    assert pause.command.state == ControlState.ACKNOWLEDGED
    assert harness.tool_calls == ["tool_one", "tool_two"]
    assert not task.done()

    harness.second_tool_release.set()
    result = await task

    assert harness.llm_calls == 1
    assert harness.tool_calls == ["tool_one", "tool_two"]
    assert result.cycle_id == "cycle-a"
    snapshot = await repositories.snapshots.get("cycle-a")
    assert snapshot.status == CycleStatus.PAUSED_BY_USER
    assistant = next(
        message
        for message in reversed(snapshot.messages_for_llm)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    issued = {call["id"] for call in assistant["tool_calls"]}
    tool_results = [
        message
        for message in snapshot.messages_for_llm
        if message.get("role") == "tool" and message.get("tool_call_id") in issued
    ]
    assert {message["tool_call_id"] for message in tool_results} == issued
    assert len(tool_results) == len(issued) == 2
    assert InputRuntimeCheckpointMixin._last_block_is_complete_tool_block(
        snapshot.messages_for_llm
    )
