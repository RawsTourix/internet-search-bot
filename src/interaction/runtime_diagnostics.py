"""Client adapters for the shared IR-9 runtime diagnostics DTO."""

from __future__ import annotations

from typing import Any

from ..input_runtime.diagnostics import (
    RuntimeAddendumProjection,
    RuntimeStatusSnapshot,
)
from ..localization.models import LocalizationMessage


def runtime_status_to_web(snapshot: RuntimeStatusSnapshot) -> dict[str, Any]:
    """JSON-safe structured Web/API representation; no localized string parsing."""

    return snapshot.model_dump(mode="json")


def render_runtime_status_cli(snapshot: RuntimeStatusSnapshot) -> str:
    """Compact terminal renderer over the same structured projection."""

    if snapshot.process_readiness != "ready":
        issue = snapshot.current_issue_code or "none"
        return (
            f"process_readiness={snapshot.process_readiness}\n"
            f"issue={issue}"
        )
    state = (
        snapshot.session_status.value
        if snapshot.session_status is not None
        else "idle"
    )
    control = (
        f"{snapshot.controls.effective_command.value}:"
        f"{snapshot.controls.effective_state.value}"
        if snapshot.controls.effective_command is not None
        and snapshot.controls.effective_state is not None
        else "none"
    )
    finalization = (
        snapshot.finalization_state.value
        if snapshot.finalization_state is not None
        else "none"
    )
    return "\n".join(
        (
            f"session_status={state}",
            f"generation={snapshot.generation}",
            "input="
            f"{snapshot.input.applied_sequence}/"
            f"{snapshot.input.accepted_sequence}"
            f" queued={snapshot.input.queued}"
            f" claimed={snapshot.input.claimed}"
            f" applying={snapshot.input.applying}",
            f"control={control}",
            "emissions="
            f"ready:{snapshot.emissions.ready} "
            f"delivering:{snapshot.emissions.delivering} "
            f"unknown:{snapshot.emissions.unknown} "
            f"failed:{snapshot.emissions.failed}",
            f"finalization={finalization}",
            f"last_issue={snapshot.last_runtime_issue_code or 'none'}",
        )
    )


def _render(localization_service, key: str, *, locale: str, **params: Any) -> str:
    return localization_service.render(
        LocalizationMessage(message_key=key, params=params),
        locale=locale,
    )


def _state_text(snapshot: RuntimeStatusSnapshot, service, locale: str) -> str:
    state = (
        snapshot.session_status.value
        if snapshot.session_status is not None
        else "idle"
    )
    return _render(
        service,
        f"input_runtime.status.state.{state}",
        locale=locale,
    )


def render_runtime_status_telegram(
    snapshot: RuntimeStatusSnapshot,
    localization_service,
    *,
    locale: str,
) -> str:
    """Localized compact Telegram status without durable/internal content dumps."""

    readiness = snapshot.process_readiness
    if readiness == "recovering":
        return _render(
            localization_service,
            "input_runtime.status.process.recovering",
            locale=locale,
        )
    if readiness in {"failed", "stopping", "stopped"}:
        return _render(
            localization_service,
            f"input_runtime.status.process.{readiness}",
            locale=locale,
            reason=snapshot.current_issue_code or "none",
        )

    state = _state_text(snapshot, localization_service, locale)
    additions = _render(
        localization_service,
        "input_runtime.status.additions",
        locale=locale,
        queued=snapshot.input.queued,
        claimed=snapshot.input.claimed,
        applying=snapshot.input.applying,
    )
    applied = _render(
        localization_service,
        "input_runtime.status.applied",
        locale=locale,
        applied=snapshot.input.applied_sequence,
        accepted=snapshot.input.accepted_sequence,
    )
    if (
        snapshot.controls.effective_command is None
        or snapshot.controls.effective_state is None
    ):
        control = _render(
            localization_service,
            "input_runtime.status.none",
            locale=locale,
        )
    else:
        control = _render(
            localization_service,
            "input_runtime.status.control",
            locale=locale,
            command=snapshot.controls.effective_command.value,
            state=snapshot.controls.effective_state.value,
        )
    emissions = _render(
        localization_service,
        "input_runtime.status.emissions",
        locale=locale,
        ready=snapshot.emissions.ready,
        delivering=snapshot.emissions.delivering,
        unknown=snapshot.emissions.unknown,
        failed=snapshot.emissions.failed,
    )
    finalization = (
        snapshot.finalization_state.value
        if snapshot.finalization_state is not None
        else _render(
            localization_service,
            "input_runtime.status.none",
            locale=locale,
        )
    )
    issue = snapshot.current_issue_code or snapshot.last_runtime_issue_code
    issue_text = (
        issue
        if issue is not None
        else _render(
            localization_service,
            "input_runtime.status.none",
            locale=locale,
        )
    )
    replay_note = ""
    if not snapshot.automatic_replay_enabled:
        replay_note = "\n" + _render(
            localization_service,
            "input_runtime.status.automatic_replay_disabled",
            locale=locale,
        )
    return (
        _render(
            localization_service,
            "input_runtime.status.compact",
            locale=locale,
            state=state,
            additions=additions,
            applied=applied,
            control=control,
            emissions=emissions,
            finalization=finalization,
            issue=issue_text,
        )
        + replay_note
    )


def render_addendum_projection_telegram(
    projection: RuntimeAddendumProjection,
    localization_service,
    *,
    locale: str,
) -> str:
    """Render one addendum state without implying application before authority."""

    if projection.state.value == "input_addendum_admitted":
        key = f"input_runtime.addendum.{projection.acknowledgement}"
    else:
        key = (
            "input_runtime.addendum."
            f"{projection.state.value.removeprefix('input_addendum_')}"
        )
    return _render(
        localization_service,
        key,
        locale=locale,
        sequence=projection.cycle_sequence,
        reason=projection.reason_code or "none",
    )
