from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 9, tzinfo=timezone.utc)


@dataclass
class TextPart:
    part_id: str
    kind: str
    text: str
    attachment_slot_ids: list[str] = field(default_factory=list)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str
    sequence_number: int
    payload_size: int
    text_parts: list[TextPart] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: Batch) -> None:
        self.batches = {batch.input_batch_id: batch for batch in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_ir10_live_profile_30_artifacts_7_messages_and_repeated_addition(tmp_path):
    artifacts = [f"art_{index:032x}" for index in range(30)]
    text_parts = [
        TextPart(
            part_id=f"part-{index}",
            kind="message_text",
            text=(f"message-{index}-" + "payload " * 32).strip(),
            attachment_slot_ids=[f"slot-{index}"] if index < 7 else [],
        )
        for index in range(7)
    ]
    initial = Batch("initial", "session", 1, 1024)
    mixed = Batch(
        "mixed-live-shape",
        "session",
        2,
        64 * 1024,
        text_parts=text_parts,
        artifact_refs=artifacts,
    )
    follow_up = Batch(
        "follow-up",
        "session",
        3,
        8 * 1024,
        text_parts=[TextPart("follow", "message_text", "follow-up context")],
        artifact_refs=[artifacts[0], "art_ffffffffffffffffffffffffffffffff"],
    )
    reader = Reader(initial, mixed, follow_up)
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(
            max_queued_batches_per_session=8,
            max_queued_bytes_per_session=2 * 1024 * 1024,
            max_batches_per_checkpoint=8,
            max_batch_bytes_per_checkpoint=2 * 1024 * 1024,
            min_intermediate_message_interval_seconds=0,
        ),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-live-shape",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )

    admitted = await service.admit_committed_batch("initial", session_id="session")
    assert admitted.action == InputAdmissionAction.START_CYCLE
    active = ActiveAgentCycle(
        cycle_id=admitted.target_cycle_id,
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
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )

    for batch_id in ("mixed-live-shape", "follow-up"):
        outcome = await service.admit_committed_batch(batch_id, session_id="session")
        assert outcome.action == InputAdmissionAction.QUEUED_RUNNING
        assert outcome.target_cycle_id == "cycle-live-shape"

    applied = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert applied.action == CheckpointAction.INPUT_APPLIED
    assert applied.applied_input_batch_ids == ("mixed-live-shape", "follow-up")

    updates = []
    for message in active.messages_for_llm:
        if message.get("role") != "user":
            continue
        payload = json.loads(message["content"])
        if payload.get("type") == "input_batch_update":
            updates.append(payload)
    assert len(updates) == 1
    members = updates[0]["batches"]
    assert [member["input_batch_id"] for member in members] == [
        "mixed-live-shape",
        "follow-up",
    ]
    assert len(members[0]["text_parts"]) == 7
    assert len(members[0]["artifact_refs"]) == 30
    assert [item["artifact_id"] for item in members[0]["artifact_refs"]] == artifacts
    assert members[1]["artifact_refs"][0]["artifact_id"] == artifacts[0]

    expected_active_refs = [*artifacts, "art_ffffffffffffffffffffffffffffffff"]
    assert active.artifact_refs == expected_active_refs
    snapshot = await repositories.snapshots.get("cycle-live-shape")
    assert snapshot.artifact_refs == expected_active_refs
    revisions = await repositories.context_revisions.list_for_cycle("cycle-live-shape")
    assert revisions[-1].added_artifact_refs == [
        *artifacts,
        artifacts[0],
        "art_ffffffffffffffffffffffffffffffff",
    ]
