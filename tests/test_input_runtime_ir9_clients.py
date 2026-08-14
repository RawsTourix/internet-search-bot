from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError
from telegram.ext import ApplicationHandlerStop

from src.input_runtime.diagnostics import (
    AddendumProjectionState,
    RuntimeAddendumProjection,
    RuntimeControlStatus,
    RuntimeEmissionCounts,
    RuntimeInputStatus,
    RuntimeRecoveryNotice,
    RuntimeStatusCounts,
    RuntimeStatusSnapshot,
)
from src.input_runtime.models import (
    ControlCommandType,
    ControlState,
    CycleStatus,
    FinalizationState,
)
from src.interaction.config import LocalizationConfigType
from src.interaction.runtime_diagnostics import (
    render_addendum_projection_telegram,
    render_recovery_notice_telegram,
    render_runtime_status_cli,
    render_runtime_status_telegram,
    runtime_status_to_web,
)
from src.localization.service import LocalizationService
from src.servers.telegram.runtime_projection_edits import (
    ProjectionEditDisposition,
    TelegramProjectionEditor,
)


CATALOG_DIR = Path(__file__).parents[1] / "src" / "localization" / "catalogs"


def localization() -> LocalizationService:
    return LocalizationService.from_directory(
        config=LocalizationConfigType(),
        directory=CATALOG_DIR,
    )


def snapshot(
    *,
    status: CycleStatus = CycleStatus.RUNNING,
    process: str = "ready",
    current_issue: str | None = None,
    replay: bool = True,
    recovery_notice: RuntimeRecoveryNotice | None = None,
) -> RuntimeStatusSnapshot:
    return RuntimeStatusSnapshot(
        process_readiness=process,
        session_exists=process == "ready",
        session_status=status if process == "ready" else None,
        generation=3 if process == "ready" else 0,
        active_cycle_id="cycle-a" if process == "ready" else None,
        accepted_session_sequence=7 if process == "ready" else 0,
        input=RuntimeInputStatus(
            accepted_sequence=6 if process == "ready" else 0,
            applied_sequence=4 if process == "ready" else 0,
            queued=2 if process == "ready" else 0,
            claimed=0,
            applying=0,
            oldest_queued_age_seconds=12 if process == "ready" else None,
        ),
        controls=RuntimeControlStatus(
            pending_sequence=3 if process == "ready" else 0,
            applied_sequence=3 if process == "ready" else 0,
            pending_count=0,
            terminal_count=3 if process == "ready" else 0,
            effective_command=ControlCommandType.PAUSE if process == "ready" else None,
            effective_state=ControlState.APPLIED if process == "ready" else None,
        ),
        counts=RuntimeStatusCounts(
            queued_additions=2 if process == "ready" else 0,
            terminal_controls=3 if process == "ready" else 0,
        ),
        emissions=RuntimeEmissionCounts(ready=1, unknown=1) if process == "ready" else RuntimeEmissionCounts(),
        finalization_state=FinalizationState.PREPARED if process == "ready" else None,
        waiting_for_user=status == CycleStatus.WAITING_USER and process == "ready",
        paused=status == CycleStatus.PAUSED_BY_USER and process == "ready",
        interrupted=status == CycleStatus.INTERRUPTED and process == "ready",
        terminal=status in {CycleStatus.DONE, CycleStatus.ERROR, CycleStatus.CANCELLED} and process == "ready",
        current_issue_code=current_issue,
        last_runtime_issue_code=current_issue,
        automatic_replay_enabled=replay,
        recovery_notice=recovery_notice,
    )


def addendum(state: AddendumProjectionState, *, acknowledgement: str = "queued_running") -> RuntimeAddendumProjection:
    from datetime import datetime, timezone

    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    return RuntimeAddendumProjection(
        input_batch_id="batch-a",
        admission_id="admission-a",
        cycle_id="cycle-a",
        cycle_sequence=2,
        generation=3,
        state=state,
        acknowledgement=acknowledgement,
        reason_code="reset_generation_advanced" if state == AddendumProjectionState.CANCELLED else None,
        admitted_at=now,
        applied_at=now if state == AddendumProjectionState.APPLIED else None,
        cancelled_at=now if state == AddendumProjectionState.CANCELLED else None,
    )


def test_ir9_web_projection_is_structured_dto_not_telegram_text():
    value = snapshot(status=CycleStatus.PAUSED_BY_USER)
    payload = runtime_status_to_web(value)
    assert payload["session_status"] == "paused_by_user"
    assert payload["input"]["queued"] == 2
    assert payload["controls"]["effective_command"] == "pause"
    assert "Состояние:" not in json.dumps(payload, ensure_ascii=False)


def test_ir9_cli_projection_consumes_same_structured_status():
    text = render_runtime_status_cli(snapshot())
    assert "session_status=running" in text
    assert "input=4/6 queued=2" in text
    assert "control=pause:applied" in text
    assert "emissions=ready:1" in text


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_ir9_telegram_status_renders_compact_localized_projection(locale):
    text = render_runtime_status_telegram(snapshot(), localization(), locale=locale)
    if locale == "ru":
        assert "Состояние: выполняется" in text
        assert "Дополнения:" in text
        assert "пауза — применено" in text
    else:
        assert "State: running" in text
        assert "Additions:" in text
        assert "pause — applied" in text
    assert "cycle-a" not in text


@pytest.mark.parametrize(
    ("process", "needle_ru", "needle_en"),
    [
        ("recovering", "восстанавливается", "recovery is in progress"),
        ("stopping", "завершает работу", "stopping"),
        ("stopped", "остановлен", "stopped"),
        ("failed", "недоступен", "unavailable"),
    ],
)
def test_ir9_telegram_process_readiness_is_separate_from_session_state(process, needle_ru, needle_en):
    issue = "startup_failure" if process == "failed" else None
    ru = render_runtime_status_telegram(snapshot(process=process, current_issue=issue), localization(), locale="ru")
    en = render_runtime_status_telegram(snapshot(process=process, current_issue=issue), localization(), locale="en")
    assert needle_ru in ru
    assert needle_en in en


@pytest.mark.parametrize(
    ("projection_state", "acknowledgement", "ru_text", "en_text"),
    [
        (AddendumProjectionState.ADMITTED, "queued_running", "добавлено в очередь текущей задачи", "queued for the current task"),
        (AddendumProjectionState.ADMITTED, "queued_paused", "остаётся на паузе", "remains paused"),
        (AddendumProjectionState.ADMITTED, "resume_waiting", "Ответ принят", "reply was accepted"),
        (AddendumProjectionState.APPLYING, "queued_running", "Применяю дополнение", "Applying the addition"),
        (AddendumProjectionState.APPLIED, "queued_running", "Дополнение применено", "addition was applied"),
        (AddendumProjectionState.CANCELLED, "queued_running", "Дополнение отменено", "addition was cancelled"),
    ],
)
def test_ir9_addendum_lifecycle_localization(projection_state, acknowledgement, ru_text, en_text):
    projection = addendum(projection_state, acknowledgement=acknowledgement)
    service = localization()
    assert ru_text in render_addendum_projection_telegram(projection, service, locale="ru")
    assert en_text in render_addendum_projection_telegram(projection, service, locale="en")


def test_ir9_ambiguous_recovery_notice_is_conservative_in_both_locales():
    notice = RuntimeRecoveryNotice(
        disposition="ambiguous",
        reason_code="runtime_handoff_ambiguous",
        automatic_replay_enabled=False,
    )
    service = localization()
    ru = render_recovery_notice_telegram(notice, service, locale="ru")
    en = render_recovery_notice_telegram(notice, service, locale="en")
    assert "могло частично выполниться" in ru
    assert "автоматический повтор не запущен" in ru
    assert "may have partially completed" in en
    assert "automatic replay was not started" in en
    assert "точно не" not in ru.lower()
    assert "definitely" not in en.lower()


def test_ir9_interrupted_recovery_notice_is_not_ambiguous_notice():
    notice = RuntimeRecoveryNotice(
        disposition="interrupted",
        reason_code="runtime_interrupted",
    )
    service = localization()
    ru = render_recovery_notice_telegram(notice, service, locale="ru")
    en = render_recovery_notice_telegram(notice, service, locale="en")
    assert ru == "Работа была прервана."
    assert en == "The task was interrupted."


def test_ir9_localization_catalogs_have_exact_key_and_placeholder_parity():
    service = localization()
    ru_keys = set(service.catalogs["ru"].entries)
    en_keys = set(service.catalogs["en"].entries)
    assert ru_keys == en_keys
    required = {
        "input_runtime.addendum.queued_running",
        "input_runtime.addendum.queued_paused",
        "input_runtime.addendum.resume_waiting",
        "input_runtime.addendum.applying",
        "input_runtime.addendum.applied",
        "input_runtime.addendum.cancelled",
        "input_runtime.addendum.failed",
        "input_runtime.control.pause.accepted",
        "input_runtime.control.pause.paused",
        "input_runtime.control.continue.resume_accepted",
        "input_runtime.control.continue.still_waiting_for_input",
        "input_runtime.control.reset.applied",
        "input_runtime.recovery.interrupted",
        "input_runtime.recovery.ambiguous",
        "input_runtime.status.compact",
        "input_runtime.status.process.recovering",
        "input_runtime.status.state.waiting_user",
        "input_runtime.status.state.paused_by_user",
        "input_runtime.status.state.interrupted",
        "input_runtime.status.finalization.terminal_committed",
    }
    assert required <= ru_keys
    # LocalizationService construction validates placeholder parity as well.


@pytest.mark.asyncio
async def test_ir9_edit_deterministic_impossibility_falls_back_once():
    editor = TelegramProjectionEditor()
    sends = 0

    async def edit():
        raise BadRequest("Message to edit not found")

    async def send_new():
        nonlocal sends
        sends += 1
        return SimpleNamespace(message_id=77)

    result = await editor.update(
        session_id="session-a",
        presentation_id="presentation-a",
        session_generation=1,
        edit=edit,
        send_new=send_new,
    )
    assert result.disposition == ProjectionEditDisposition.FALLBACK_SENT
    assert result.message.message_id == 77
    assert sends == 1


@pytest.mark.asyncio
async def test_ir9_edit_ambiguous_network_outcome_does_not_blind_send_new():
    editor = TelegramProjectionEditor()
    sends = 0

    async def edit():
        raise NetworkError("timeout after possible server-side edit")

    async def send_new():
        nonlocal sends
        sends += 1
        return SimpleNamespace(message_id=88)

    result = await editor.update(
        session_id="session-a",
        presentation_id="presentation-a",
        session_generation=1,
        edit=edit,
        send_new=send_new,
    )
    assert result.disposition == ProjectionEditDisposition.UNKNOWN
    assert sends == 0


@pytest.mark.asyncio
async def test_ir9_ambiguous_fallback_send_is_not_retried_into_duplicate():
    editor = TelegramProjectionEditor()
    sends = 0

    async def edit():
        raise BadRequest("Message can't be edited")

    async def send_new():
        nonlocal sends
        sends += 1
        raise NetworkError("send outcome ambiguous")

    result = await editor.update(
        session_id="session-a",
        presentation_id="presentation-a",
        session_generation=1,
        edit=edit,
        send_new=send_new,
    )
    assert result.disposition == ProjectionEditDisposition.UNKNOWN
    assert sends == 1


@pytest.mark.asyncio
async def test_ir9_stale_queued_edit_cannot_finish_after_newer_applied_edit():
    editor = TelegramProjectionEditor()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    newer_reserved = asyncio.Event()
    visible: list[str] = []

    async def queued_edit():
        first_entered.set()
        await release_first.wait()
        visible.append("queued")
        return SimpleNamespace(message_id=1)

    async def applied_edit():
        visible.append("applied")
        return SimpleNamespace(message_id=1)

    async def never_send():
        raise AssertionError("fallback must not run")

    first = asyncio.create_task(
        editor.update(
            session_id="session-a",
            presentation_id="presentation-a",
            session_generation=1,
            revision=1,
            edit=queued_edit,
            send_new=never_send,
        )
    )
    await first_entered.wait()

    def current_for_newer() -> bool:
        newer_reserved.set()
        return True

    second = asyncio.create_task(
        editor.update(
            session_id="session-a",
            presentation_id="presentation-a",
            session_generation=1,
            revision=2,
            edit=applied_edit,
            send_new=never_send,
            generation_is_current=current_for_newer,
        )
    )
    await newer_reserved.wait()
    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert visible == ["queued", "applied"]
    assert first_result.disposition == ProjectionEditDisposition.SUPPRESSED_STALE
    assert second_result.disposition == ProjectionEditDisposition.EDITED


@pytest.mark.asyncio
async def test_ir9_old_generation_edit_cannot_finish_after_post_reset_projection():
    editor = TelegramProjectionEditor()
    current_generation = 1
    old_entered = asyncio.Event()
    release_old = asyncio.Event()
    new_reserved = asyncio.Event()
    visible: list[str] = []

    async def old_edit():
        old_entered.set()
        await release_old.wait()
        visible.append("old-generation")
        return SimpleNamespace(message_id=1)

    async def new_edit():
        visible.append("post-reset")
        return SimpleNamespace(message_id=1)

    async def never_send():
        raise AssertionError("fallback must not run")

    old = asyncio.create_task(
        editor.update(
            session_id="session-a",
            presentation_id="presentation-a",
            session_generation=1,
            revision=1,
            edit=old_edit,
            send_new=never_send,
            generation_is_current=lambda: current_generation == 1,
        )
    )
    await old_entered.wait()
    current_generation = 2

    def new_is_current() -> bool:
        new_reserved.set()
        return current_generation == 2

    new = asyncio.create_task(
        editor.update(
            session_id="session-a",
            presentation_id="presentation-a",
            session_generation=2,
            revision=1,
            edit=new_edit,
            send_new=never_send,
            generation_is_current=new_is_current,
        )
    )
    await new_reserved.wait()
    release_old.set()
    old_result, new_result = await asyncio.gather(old, new)
    assert visible == ["old-generation", "post-reset"]
    assert old_result.disposition == ProjectionEditDisposition.SUPPRESSED_STALE
    assert new_result.disposition == ProjectionEditDisposition.EDITED


@pytest.mark.asyncio
async def test_ir9_terminal_projection_wins_over_blocked_stale_progress_revision():
    editor = TelegramProjectionEditor()
    progress_entered = asyncio.Event()
    release_progress = asyncio.Event()
    terminal_reserved = asyncio.Event()
    visible: list[str] = []

    async def progress_edit():
        progress_entered.set()
        await release_progress.wait()
        visible.append("progress")
        return SimpleNamespace(message_id=1)

    async def terminal_edit():
        visible.append("terminal")
        return SimpleNamespace(message_id=1)

    async def never_send():
        raise AssertionError("fallback must not run")

    progress = asyncio.create_task(
        editor.update(
            session_id="session-a",
            presentation_id="status-a",
            session_generation=1,
            revision=10,
            edit=progress_edit,
            send_new=never_send,
        )
    )
    await progress_entered.wait()

    def terminal_current() -> bool:
        terminal_reserved.set()
        return True

    terminal = asyncio.create_task(
        editor.update(
            session_id="session-a",
            presentation_id="status-a",
            session_generation=1,
            revision=11,
            edit=terminal_edit,
            send_new=never_send,
            generation_is_current=terminal_current,
        )
    )
    await terminal_reserved.wait()
    release_progress.set()
    progress_result, terminal_result = await asyncio.gather(progress, terminal)
    assert visible[-1] == "terminal"
    assert progress_result.disposition == ProjectionEditDisposition.SUPPRESSED_STALE
    assert terminal_result.disposition == ProjectionEditDisposition.EDITED


@pytest.mark.asyncio
async def test_ir9_runtime_status_handler_uses_trusted_session_payload_and_stops_legacy_handler(monkeypatch):
    from src.servers.telegram import runtime_control_handlers

    sent_payloads: list[dict] = []
    replies: list[str] = []
    fake_server = SimpleNamespace(
        TELEGRAM_BOT_INSTANCE_ID="bot-a",
        _session_for_update=lambda _update: "telegram:bot-a:123:root",
        detect_progress_locale=lambda _update: "ru",
        send_to_gateway=None,
        telegram_reply_with_retries=None,
        _localized=lambda key, **params: f"{key}:{params.get('reason', '')}",
    )

    async def send_to_gateway(payload):
        sent_payloads.append(payload)
        return True, "Состояние: выполняется", {"ignored": "metadata"}

    async def reply(_update, text, **_kwargs):
        replies.append(text)
        return SimpleNamespace(message_id=9)

    fake_server.send_to_gateway = send_to_gateway
    fake_server.telegram_reply_with_retries = reply
    monkeypatch.setitem(sys.modules, "src.servers.telegram.telegram_server", fake_server)
    import src.servers.telegram as telegram_package
    monkeypatch.setattr(telegram_package, "telegram_server", fake_server, raising=False)

    update = SimpleNamespace(
        update_id=99,
        effective_user=SimpleNamespace(id=7, full_name="User"),
        effective_chat=SimpleNamespace(id=123),
        effective_message=SimpleNamespace(
            text="/status",
            message_id=55,
            message_thread_id=None,
        ),
    )
    with pytest.raises(ApplicationHandlerStop):
        await runtime_control_handlers.runtime_status_handler(update, SimpleNamespace())
    assert len(sent_payloads) == 1
    payload = sent_payloads[0]
    assert payload["message_type"] == "command"
    assert payload["content"] == "/status"
    assert payload["metadata"]["session_id"] == "telegram:bot-a:123:root"
    assert "progress_target" not in payload["metadata"]
    assert replies == ["Состояние: выполняется"]
