from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.agent.protocol import AgentAction
from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.checkpoint_hardening import (
    DurableContextCheckpointService,
)
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from src.mcp.input_runtime_checkpoint_hardening import (
    InputRuntimeCheckpointHardeningMixin,
)
from src.mcp.input_runtime_checkpoints import InputRuntimeCheckpointMixin
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    text_parts: list[object] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(
        default_factory=lambda: SimpleNamespace(items=())
    )

    def model_dump_json(self) -> str:
        return '{"batch":true}'


class Reader:
    def __init__(self, *batches: Batch) -> None:
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


def active_cycle(cycle_id: str) -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id=cycle_id,
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"type":"user_request"}'},
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )


@pytest.mark.asyncio
async def test_closed_tool_block_is_persisted_before_fifo_update(tmp_path):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(max_batches_per_checkpoint=1),
        repositories=repositories,
        committed_batches=Reader(Batch("initial"), Batch("addition")),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda _batch: 10,
    )
    assert isinstance(
        service.checkpoint_service,
        DurableContextCheckpointService,
    )
    initial = await service.admit_committed_batch(
        "initial",
        session_id="session",
    )
    active = active_cycle(initial.target_cycle_id)
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    active.messages_for_llm.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tool-one",
                        "type": "function",
                        "function": {
                            "name": "demo",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tool-one",
                "content": '{"ok":true}',
            },
        ]
    )
    await service.admit_committed_batch(
        "addition",
        session_id="session",
    )
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.AFTER_TOOL_BLOCK,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action == CheckpointAction.INPUT_APPLIED
    assert [item["role"] for item in active.messages_for_llm[-3:]] == [
        "assistant",
        "tool",
        "user",
    ]
    update = json.loads(active.messages_for_llm[-1]["content"])
    assert update["type"] == "input_batch_update"
    snapshot = await repositories.snapshots.get("cycle-a")
    assert snapshot.messages_for_llm == active.messages_for_llm


def test_stale_candidate_before_runtime_update_is_removed():
    active = active_cycle("cycle-a")
    active.messages_for_llm.extend(
        [
            {
                "role": "assistant",
                "content": AgentAction(
                    status="done",
                    action="answer",
                    final_answer="stale",
                ).model_dump_json(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "type": "input_batch_update",
                        "runtime_generated": True,
                        "context_revision_id": "ctxrev_" + "1" * 32,
                        "batches": [],
                    }
                ),
            },
        ]
    )
    InputRuntimeCheckpointHardeningMixin._remove_stale_candidate(active)
    assert active.messages_for_llm[-1]["role"] == "user"
    assert all(
        "stale" not in str(item.get("content"))
        for item in active.messages_for_llm
    )


def test_production_mro_applies_hardening_before_checkpoint_mixin():
    mro = FinalizingArtifactDeliveryPlanningMCPClient.__mro__
    assert mro.index(InputRuntimeCheckpointHardeningMixin) < mro.index(
        InputRuntimeCheckpointMixin
    )
