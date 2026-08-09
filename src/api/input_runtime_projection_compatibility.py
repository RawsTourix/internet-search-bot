"""IR-9 compatibility projection hooks for the existing MessageProcessor.

The existing Gateway command framework remains in place. These hooks replace
only client-facing representation; durable semantic authority stays in IR-1--IR-8.
"""

from __future__ import annotations

from typing import Any

from ..core.models import ClientType
from ..input_runtime import ControlState, CycleStatus, InputAdmissionAction
from ..input_runtime.diagnostics import RuntimeDiagnosticsError
from ..interaction.runtime_diagnostics import (
    render_runtime_status_cli,
    render_runtime_status_telegram,
)
from ..localization.models import LocalizationMessage


_INSTALL_MARKER = "_ir9_projection_compatibility_installed"


def install_input_runtime_projection_compatibility(api: Any) -> None:
    if getattr(api, _INSTALL_MARKER, False):
        return

    # Import only after src.api.api has completed construction. This preserves
    # the package's existing composition order and avoids a domain->API import.
    from ..core import message_processor as module
    from .session_reset import reset_runtime_session

    processor = module.MessageProcessor
    original_handle_command = processor._handle_command

    def localized(self, key: str, *, locale: str, **params: Any) -> str:
        return self._render_message(
            LocalizationMessage(message_key=key, params=params),
            locale=locale,
        )

    def render_admission_outcome(self, outcome, *, locale: str) -> str:
        sequence = outcome.cycle_sequence or 0
        key = {
            InputAdmissionAction.QUEUED_RUNNING:
                "input_runtime.addendum.queued_running",
            InputAdmissionAction.RESUME_WAITING:
                "input_runtime.addendum.resume_waiting",
            InputAdmissionAction.QUEUED_PAUSED:
                "input_runtime.addendum.queued_paused",
            InputAdmissionAction.RESUME_INTERRUPTED:
                "input_runtime.addendum.resume_interrupted",
            InputAdmissionAction.CAPACITY_BLOCKED:
                "input_runtime.addendum.capacity_blocked",
            InputAdmissionAction.DUPLICATE:
                "input_runtime.addendum.duplicate",
            InputAdmissionAction.START_CYCLE:
                "input_runtime.addendum.start_accepted",
        }.get(outcome.action, "input_runtime.addendum.start_accepted")
        return localized(
            self,
            key,
            locale=locale,
            sequence=sequence,
        )

    def render_control_outcome(self, outcome, *, locale: str) -> str:
        command = outcome.command
        if command.state == ControlState.REJECTED:
            known = {
                "already_paused",
                "no_active_cycle",
                "already_running",
                "nothing_to_continue",
                "still_waiting_for_input",
            }
            reason = command.rejection_code or "rejected"
            if reason in known:
                key = f"input_runtime.control.{command.command.value}.{reason}"
                return localized(self, key, locale=locale)
            return localized(
                self,
                "input_runtime.control.rejected",
                locale=locale,
                reason=reason,
            )
        if command.command.value == "pause":
            key = (
                "input_runtime.control.pause.paused"
                if command.state == ControlState.APPLIED
                else "input_runtime.control.pause.pause_pending"
            )
            return localized(self, key, locale=locale)
        if command.command.value == "continue":
            if outcome.effective_cycle_status == CycleStatus.WAITING_USER:
                key = "input_runtime.control.continue.still_waiting_for_input"
            elif command.state == ControlState.APPLIED:
                key = "input_runtime.control.continue.resumed"
            else:
                key = "input_runtime.control.continue.resume_accepted"
            return localized(self, key, locale=locale)
        return localized(
            self,
            "input_runtime.control.reset.applied",
            locale=locale,
        )

    async def get_status_text(self, message) -> str:
        service = getattr(api, "input_runtime_diagnostics", None)
        locale = self._control_locale(message)
        if service is None:
            return localized(
                self,
                "input_runtime.status.unavailable",
                locale=locale,
                reason="diagnostics_unavailable",
            )
        try:
            snapshot = await service.status(self._build_session_id(message))
        except RuntimeDiagnosticsError as error:
            return localized(
                self,
                "input_runtime.status.unavailable",
                locale=locale,
                reason=error.reason_code,
            )
        localization_service = getattr(
            getattr(api, "ingress_services", None),
            "localization_service",
            None,
        )
        if message.client_type == ClientType.TELEGRAM:
            if localization_service is None:
                return "input_runtime.status.unavailable"
            return render_runtime_status_telegram(
                snapshot,
                localization_service,
                locale=locale,
            )
        return render_runtime_status_cli(snapshot)

    async def handle_command(
        self,
        message,
        *,
        progress_callback=None,
    ) -> str:
        command = message.content.strip()
        if command != "/reset":
            return await original_handle_command(
                self,
                message,
                progress_callback=progress_callback,
            )

        session_id = self._build_session_id(message)
        source_ref = self._control_source_ref(message)
        locale = self._control_locale(message)
        try:
            result = await reset_runtime_session(
                api,
                session_id,
                idempotency_key=self._control_idempotency_key(
                    message,
                    command=command,
                ),
                source_client_type=message.client_type.value,
                source_message_ref=source_ref,
            )
        except Exception as error:
            module.logger.warning(
                "runtime_reset_projection_failed session_id=%s error_type=%s",
                session_id,
                type(error).__name__,
            )
            return localized(
                self,
                "input_runtime.control.reset.failed",
                locale=locale,
            )
        if result.cancelled_input_batch_count:
            return localized(
                self,
                "input_runtime.control.reset.applied_with_cancelled",
                locale=locale,
                count=result.cancelled_input_batch_count,
            )
        return localized(
            self,
            "input_runtime.control.reset.applied",
            locale=locale,
        )

    processor._render_admission_outcome = render_admission_outcome
    processor._render_control_outcome = render_control_outcome
    processor._get_status_text = get_status_text
    processor._handle_command = handle_command
    setattr(api, _INSTALL_MARKER, True)
