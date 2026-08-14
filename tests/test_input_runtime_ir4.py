from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    InputRuntimeRepositories,
    create_filesystem_input_runtime_repositories,
)
from src.mcp.input_runtime_checkpoints import InputRuntimeCheckpointMixin
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeTextPart:
    part_id: str
    kind: str
    text: str
    attachment_slot_ids: list[str] = field(default_factory=list)


@dataclass
class FakeBatch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 10
    text_parts: list[FakeTextPart] = field(default_factory=list)
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
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: FakeBatch):
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


def repos(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def cycle(cycle_id: str, *, artifacts=()):
    return ActiveAgentCycle(
        cycle_id=cycle_id,
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": json.dumps(
                    {"type": "user_request", "user_request": "initial"}
                ),
            },
        ],
        cycle_trace=[],
        original_user_message_index=1,
        artifact_refs=list(artifacts),
        active_plan_id="plan-1",
        active_plan_revision=3,
        active_plan_node_id="node-2",
    )


def service(tmp_path, batches, *, config=None):
    repository_bundle = repos(tmp_path)
    reader = Reader(*batches)
    admission = InputAdmissionService(
        config=config or InputRuntimeConfigType(),
        repositories=repository_bundle,
        committed_batches=reader,
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    applier = CycleInputApplier(
        config=admission.config,
        repositories=repository_bundle,
        committed_batches=reader,
        clock=lambda: NOW,
    )
    return admission, applier, repository_bundle


def test_composition_requires_explicit_handoff_port(tmp_path):
    base = repos(tmp_path)
    with pytest.raises(TypeError):
        InputRuntimeRepositories(
            sessions=base.sessions,
            admissions=base.admissions,
            inbox=base.inbox,
            controls=base.controls,
            snapshots=base.snapshots,
            context_revisions=base.context_revisions,
            emissions=base.emissions,
            finalizations=base.finalizations,
        )
    assert not hasattr(base.sessions, "runtime_handoffs")
    assert base.handoffs is not None


@pytest.mark.asyncio
async def test_initial_r1_and_fifo_update_are_durable_and_linear(tmp_path):
    batches = [
        FakeBatch("initial", artifact_refs=["artifact-a"]),
        FakeBatch(
            "addition-1",
            text_parts=[FakeTextPart("p1", "message_text", "first")],
            artifact_refs=["artifact-b"],
        ),
        FakeBatch(
            "addition-2",
            text_parts=[FakeTextPart("p2", "caption", "second")],
        ),
    ]
    admission, applier, repository_bundle = service(tmp_path, batches)
    initial = await admission.admit_committed_batch("initial", session_id="session")
    active = cycle(initial.target_cycle_id, artifacts=["artifact-a"])
    active.original_input_batch_id = "initial"
    active.input_runtime_generation = 0

    first = await applier.ensure_initial_context(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        input_batch_id="initial",
    )
    duplicate = await applier.ensure_initial_context(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        input_batch_id="initial",
    )
    revisions = await repository_bundle.context_revisions.list_for_cycle(
        initial.target_cycle_id
    )
    assert first.context_revision_id == duplicate.context_revision_id
    assert len(revisions) == 1
    assert revisions[0].revision_number == 1
    snapshot = await repository_bundle.snapshots.get(initial.target_cycle_id)
    assert snapshot.applied_through_cycle_sequence == 0
    assert snapshot.applied_input_batch_ids == ["initial"]

    await admission.admit_committed_batch("addition-1", session_id="session")
    await admission.admit_committed_batch("addition-2", session_id="session")
    outcome = await applier.apply_pending_input(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
    )
    assert outcome.action == CheckpointAction.INPUT_APPLIED
    assert outcome.applied_input_batch_ids == ("addition-1", "addition-2")
    payload = json.loads(active.messages_for_llm[-1]["content"])
    assert payload["type"] == "input_batch_update"
    assert [item["cycle_sequence"] for item in payload["batches"]] == [1, 2]
    assert [item["input_batch_id"] for item in payload["batches"]] == [
        "addition-1",
        "addition-2",
    ]
    assert active.artifact_refs == ["artifact-a", "artifact-b"]
    assert active.active_plan_id == "plan-1"
    assert active.active_plan_revision == 3
    assert active.active_plan_node_id == "node-2"

    revisions = await repository_bundle.context_revisions.list_for_cycle(
        initial.target_cycle_id
    )
    assert [item.revision_number for item in revisions] == [1, 2]
    assert revisions[1].parent_revision_ids == [revisions[0].context_revision_id]
    no_change = await applier.apply_pending_input(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
    )
    assert no_change.action == CheckpointAction.CONTINUE
    assert len(active.messages_for_llm) == 3
    assert len(await repository_bundle.context_revisions.list_for_cycle(
        initial.target_cycle_id
    )) == 2


@pytest.mark.asyncio
async def test_waiting_reply_is_deferred_to_common_fifo_applier(tmp_path):
    batches = [FakeBatch("initial"), FakeBatch("earlier"), FakeBatch("reply")]
    admission, applier, repository_bundle = service(tmp_path, batches)
    initial = await admission.admit_committed_batch("initial", session_id="session")
    active = cycle(initial.target_cycle_id)
    active.original_input_batch_id = "initial"
    active.input_runtime_generation = 0
    await applier.ensure_initial_context(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        input_batch_id="initial",
    )
    await admission.admit_committed_batch("earlier", session_id="session")
    state = await repository_bundle.sessions.get("session")
    waiting = state.model_copy(update={
        "cycle_status": CycleStatus.WAITING_USER,
        "revision": state.revision + 1,
        "updated_at": NOW,
    })
    await repository_bundle.sessions.compare_and_swap(state.revision, waiting)
    reply = await admission.admit_committed_batch("reply", session_id="session")
    deferred = await admission.begin_waiting_compatibility_apply(reply.admission)
    assert deferred.input_batch_id == "reply"
    items = await repository_bundle.inbox.list_for_cycle(initial.target_cycle_id)
    assert [item.state.value for item in items] == ["queued", "queued"]

    outcome = await applier.apply_pending_input(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
    )
    assert outcome.applied_input_batch_ids == ("earlier", "reply")
    await admission.complete_waiting_compatibility_apply(deferred)


def test_tool_block_checkpoint_requires_every_matching_result():
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {}},
                {"id": "call-2", "type": "function", "function": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "one"},
    ]
    assert InputRuntimeCheckpointMixin._last_block_is_complete_tool_block(
        messages
    ) is False
    messages.append(
        {"role": "tool", "tool_call_id": "call-2", "content": "two"}
    )
    assert InputRuntimeCheckpointMixin._last_block_is_complete_tool_block(
        messages
    ) is True
