from __future__ import annotations

import os

os.environ["AGENT_CONFIG_PATH"] = "src/api/mcp.config.example"

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.api import Api
from src.core.models import AgentResult, AgentStatus, ClientType
from src.input_runtime import (
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    client_type: ClientType = ClientType.CLI
    capability_snapshot: object = object()
    locale: str = "ru"
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW

    def model_dump_json(self):
        return '{"batch":true}'


class Reader:
    def __init__(self, batches):
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id):
        return self.batches[input_batch_id]


class BarrierMCP:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.call_count = 0
        self.cycle_ids = []

    async def process_query(self, message, **kwargs):
        self.call_count += 1
        self.cycle_ids.append(kwargs["cycle_id_override"])
        self.entered.set()
        await self.release.wait()
        return AgentResult(
            content="done",
            status=AgentStatus.DONE,
            session_id=kwargs["session_id"],
            cycle_id=kwargs["cycle_id_override"],
        )


def make_api(tmp_path, batches):
    api = object.__new__(Api)
    api.execution_coordinator = SessionExecutionCoordinator()
    api.input_runtime_config = InputRuntimeConfigType()
    api.input_runtime_repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    reader = Reader(batches)
    api.input_admission_service = InputAdmissionService(
        config=api.input_runtime_config,
        repositories=api.input_runtime_repositories,
        committed_batches=reader,
        wake_coordinator=api.execution_coordinator,
        cycle_id_factory=lambda: "one-cycle",
        clock=lambda: NOW,
        payload_size_resolver=lambda _batch: 10,
    )
    api.ingress_services = SimpleNamespace(batch_store=reader)
    api.mcp_client = BarrierMCP()
    api.interaction_config = SimpleNamespace(
        output_runtime=SimpleNamespace(enabled=False),
        telegram_output=SimpleNamespace(
            prefer_document_groups=False,
            status_message_editing=False,
        ),
    )
    api.output_assembler = SimpleNamespace()
    return api


@pytest.mark.asyncio
async def test_running_additions_never_start_parallel_agent_cycle(tmp_path):
    batches = [Batch("initial"), Batch("addition-1"), Batch("addition-2"), Batch("addition-3")]
    api = make_api(tmp_path, batches)

    initial = await api.admit_committed_batch("initial", session_id="session")
    runner = asyncio.create_task(api.start_admitted_cycle(initial))
    await api.mcp_client.entered.wait()

    additions = [
        await api.admit_committed_batch(batch_id, session_id="session")
        for batch_id in ("addition-1", "addition-2", "addition-3")
    ]
    assert [item.action for item in additions] == [
        InputAdmissionAction.QUEUED_RUNNING,
        InputAdmissionAction.QUEUED_RUNNING,
        InputAdmissionAction.QUEUED_RUNNING,
    ]
    assert [item.cycle_sequence for item in additions] == [1, 2, 3]
    assert {item.target_cycle_id for item in additions} == {"one-cycle"}
    assert api.mcp_client.call_count == 1

    duplicate = await api.admit_committed_batch("initial", session_id="session")
    duplicate_runner = await api.start_admitted_cycle(duplicate)
    assert duplicate_runner is None
    assert api.mcp_client.call_count == 1

    inbox = await api.input_runtime_repositories.inbox.list_for_cycle("one-cycle")
    assert [item.input_batch_id for item in inbox] == [
        "addition-1",
        "addition-2",
        "addition-3",
    ]

    api.mcp_client.release.set()
    result = await runner
    assert result is not None
    assert api.mcp_client.call_count == 1
    assert api.mcp_client.cycle_ids == ["one-cycle"]
