from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError

from src.input_runtime.models import (
    CheckpointAction,
    CheckpointName,
    CheckpointOutcome,
)
from src.mcp import ir9_projection_checkpoints as projection_checkpoints
from src.servers.telegram.collection_bridge import (
    ExplicitCollectionTelegramGatewayClient,
)
from src.servers.telegram.run_progress_bridge import (
    RunScopedProgressTelegramGatewayClient,
)
from src.servers.telegram.runtime_projection_edits import (
    ProjectionEditDisposition,
    TelegramProjectionEditor,
    apply_applied_addendum_projection,
)


class FakeAdmissions:
    def __init__(self, records):
        self.records = dict(records)

    async def get_by_input_batch_id(self, input_batch_id: str):
        return self.records.get(input_batch_id)


@pytest.mark.asyncio
async def test_ir9_checkpoint_emits_structured_projection_only_after_durable_applied(monkeypatch):
    emitted: list[dict] = []
    callback = object()
    state = SimpleNamespace(progress_locale="ru")
    active_cycle = SimpleNamespace(
        session_id="session-a",
        cycle_id="cycle-a",
        input_runtime_generation=2,
        cycle_trace=[],
    )
    admission = SimpleNamespace(
        session_id="session-a",
        target_cycle_id="cycle-a",
        admitted_generation=2,
        cycle_sequence=3,
    )
    binding = SimpleNamespace(
        repositories=SimpleNamespace(
            admissions=FakeAdmissions({"batch-add": admission})
        )
    )
    monkeypatch.setattr(
        projection_checkpoints.checkpoint_module,
        "get_input_runtime_binding",
        lambda: binding,
    )
    projection_checkpoints._projection_progress_state.set(state)
    projection_checkpoints._projection_progress_callback.set(callback)

    class Owner:
        async def _emit_progress_event(self, **kwargs):
            emitted.append(kwargs)

    outcome = CheckpointOutcome(
        checkpoint=CheckpointName.BEFORE_LLM,
        action=CheckpointAction.INPUT_APPLIED,
        context_revision_id="ctxrev_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        applied_through_cycle_sequence=3,
        applied_input_batch_ids=("batch-add",),
    )
    await projection_checkpoints._emit_applied_projection(
        Owner(),
        active_cycle=active_cycle,
        outcome=outcome,
    )

    assert len(emitted) == 1
    event = emitted[0]
    assert event["event_type"] == "input_addendum_applied"
    assert event["session_id"] == "session-a"
    assert event["cycle_id"] == "cycle-a"
    assert event["message"] == "input_addendum_applied"
    assert event["data"] == {
        "input_batch_id": "batch-add",
        "cycle_sequence": 3,
        "generation": 2,
        "locale": "ru",
    }
    assert "text" not in str(event["data"]).lower()


@pytest.mark.asyncio
async def test_ir9_checkpoint_suppresses_initial_cycle_and_non_applied_outcomes(monkeypatch):
    emitted: list[dict] = []
    active_cycle = SimpleNamespace(
        session_id="session-a",
        cycle_id="cycle-a",
        input_runtime_generation=1,
        cycle_trace=[],
    )
    initial = SimpleNamespace(
        session_id="session-a",
        target_cycle_id="cycle-a",
        admitted_generation=1,
        cycle_sequence=0,
    )
    binding = SimpleNamespace(
        repositories=SimpleNamespace(
            admissions=FakeAdmissions({"batch-initial": initial})
        )
    )
    monkeypatch.setattr(
        projection_checkpoints.checkpoint_module,
        "get_input_runtime_binding",
        lambda: binding,
    )
    projection_checkpoints._projection_progress_state.set(
        SimpleNamespace(progress_locale="en")
    )
    projection_checkpoints._projection_progress_callback.set(object())

    class Owner:
        async def _emit_progress_event(self, **kwargs):
            emitted.append(kwargs)

    await projection_checkpoints._emit_applied_projection(
        Owner(),
        active_cycle=active_cycle,
        outcome=CheckpointOutcome(
            checkpoint=CheckpointName.BEFORE_LLM,
            action=CheckpointAction.INPUT_APPLIED,
            context_revision_id="ctxrev_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            applied_input_batch_ids=("batch-initial",),
        ),
    )
    await projection_checkpoints._emit_applied_projection(
        Owner(),
        active_cycle=active_cycle,
        outcome=CheckpointOutcome(
            checkpoint=CheckpointName.BEFORE_LLM,
            action=CheckpointAction.CONTINUE,
        ),
    )
    assert emitted == []


@pytest.mark.asyncio
async def test_ir9_run_gateway_remembers_auto_addendum_handle_boundedly(monkeypatch):
    async def noop_super(self, submission, *, client_message_id):
        return None

    monkeypatch.setattr(
        ExplicitCollectionTelegramGatewayClient,
        "remember_input_presentation_handle",
        noop_super,
    )
    gateway = object.__new__(RunScopedProgressTelegramGatewayClient)
    gateway._maximum_pending_run_presentations = 2
    gateway._run_presentation_lock = asyncio.Lock()
    gateway._runtime_input_presentations = OrderedDict()

    for index in range(3):
        await gateway.remember_input_presentation_handle(
            {
                "input_batch_id": f"batch-{index}",
                "presentation_ref": {
                    "presentation_id": f"presentation-{index}",
                    "presentation_generation": 1,
                },
            },
            client_message_id=str(100 + index),
        )

    assert await gateway.runtime_input_presentation("batch-0") is None
    assert await gateway.runtime_input_presentation("batch-1") == {
        "presentation_id": "presentation-1",
        "message_id": "101",
        "presentation_generation": 1,
    }
    assert (await gateway.runtime_input_presentation("batch-2"))["message_id"] == "102"


class FakeGenerations:
    def __init__(self, current_generation: int = 4) -> None:
        self.current_generation = current_generation

    def is_current(self, session_id: str, generation: int) -> bool:
        assert session_id == "telegram:bot-a:10:root"
        return generation == self.current_generation


class FakeGateway:
    def __init__(self) -> None:
        self.presentation = {
            "presentation_id": "presentation-add",
            "message_id": "222",
            "presentation_generation": 1,
        }
        self.lookups = 0
        self.replacements: list[tuple[str, str, int]] = []

    async def runtime_input_presentation(self, input_batch_id: str):
        self.lookups += 1
        assert input_batch_id == "batch-add"
        return dict(self.presentation)

    async def replace_runtime_input_presentation_message_id(
        self,
        input_batch_id: str,
        *,
        expected_presentation_id: str,
        message_id: int,
    ) -> bool:
        self.replacements.append(
            (input_batch_id, expected_presentation_id, int(message_id))
        )
        self.presentation["message_id"] = str(message_id)
        return True


def payload(*, generation: int = 4) -> dict:
    return {
        "client_type": "telegram",
        "event": {
            "type": "input_addendum_applied",
            "visibility": "user",
            "message": "input_addendum_applied",
            "data": {
                "input_batch_id": "batch-add",
                "cycle_sequence": 2,
                "generation": 9,
                "locale": "ru",
            },
        },
        "target": {
            "session_id": "telegram:bot-a:10:root",
            "session_generation": generation,
            "chat_id": 10,
            "message_id": 111,
        },
    }


def fake_server(bot, generations=None):
    return SimpleNamespace(
        session_generations=generations or FakeGenerations(),
        normalize_locale=lambda value: "en" if str(value).startswith("en") else "ru",
        _localized=lambda key, locale: (
            "Дополнение применено."
            if locale == "ru"
            else "The addition was applied."
        ),
        application=SimpleNamespace(bot=bot),
    )


@pytest.mark.asyncio
async def test_ir9_applied_projection_edits_addendum_handle_not_run_progress_target():
    edits: list[tuple[int, int, str]] = []

    class Bot:
        async def edit_message_text(self, *, chat_id, message_id, text):
            edits.append((chat_id, message_id, text))

        async def send_message(self, **_kwargs):
            raise AssertionError("fallback must not run")

    gateway = FakeGateway()
    result = await apply_applied_addendum_projection(
        server=fake_server(Bot()),
        gateway=gateway,
        payload=payload(),
        editor=TelegramProjectionEditor(),
    )
    assert result == {"status": "handled", "disposition": "edited"}
    assert edits == [(10, 222, "Дополнение применено.")]
    assert all(message_id != 111 for _, message_id, _ in edits)
    assert gateway.replacements == []


@pytest.mark.asyncio
async def test_ir9_applied_projection_deterministic_edit_failure_falls_back_and_rebinds_local_handle():
    sends: list[tuple[int, str]] = []

    class Bot:
        async def edit_message_text(self, **_kwargs):
            raise BadRequest("Message to edit not found")

        async def send_message(self, *, chat_id, text):
            sends.append((chat_id, text))
            return SimpleNamespace(message_id=333)

    gateway = FakeGateway()
    result = await apply_applied_addendum_projection(
        server=fake_server(Bot()),
        gateway=gateway,
        payload=payload(),
        editor=TelegramProjectionEditor(),
    )
    assert result["disposition"] == ProjectionEditDisposition.FALLBACK_SENT.value
    assert sends == [(10, "Дополнение применено.")]
    assert gateway.replacements == [("batch-add", "presentation-add", 333)]


@pytest.mark.asyncio
async def test_ir9_applied_projection_ambiguous_edit_never_sends_duplicate():
    sends = 0

    class Bot:
        async def edit_message_text(self, **_kwargs):
            raise NetworkError("timeout after possible server-side edit")

        async def send_message(self, **_kwargs):
            nonlocal sends
            sends += 1
            return SimpleNamespace(message_id=333)

    gateway = FakeGateway()
    result = await apply_applied_addendum_projection(
        server=fake_server(Bot()),
        gateway=gateway,
        payload=payload(),
        editor=TelegramProjectionEditor(),
    )
    assert result["disposition"] == ProjectionEditDisposition.UNKNOWN.value
    assert sends == 0
    assert gateway.replacements == []


@pytest.mark.asyncio
async def test_ir9_old_generation_applied_projection_is_suppressed_before_handle_lookup():
    class Bot:
        async def edit_message_text(self, **_kwargs):
            raise AssertionError("stale edit must not run")

        async def send_message(self, **_kwargs):
            raise AssertionError("stale fallback must not run")

    gateway = FakeGateway()
    result = await apply_applied_addendum_projection(
        server=fake_server(Bot(), FakeGenerations(current_generation=5)),
        gateway=gateway,
        payload=payload(generation=4),
        editor=TelegramProjectionEditor(),
    )
    assert result == {"status": "ignored", "reason": "stale session generation"}
    assert gateway.lookups == 0
