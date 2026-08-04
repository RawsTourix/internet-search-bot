"""Cardinality-aware progress projection for artifact delivery workflows."""

from __future__ import annotations

from typing import Any

from ..artifacts.progress import (
    artifact_delivery_event_message_key,
    artifact_delivery_message_projection,
    artifact_delivery_start_message_key,
)
from .manager_context import ManagerToolContext


class ArtifactDeliveryProgressMixin:
    """Project aggregate delivery outcomes into bounded user progress."""

    def _tool_start_message(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        progress_locale: str = "ru",
    ) -> str:
        if tool_name != "artifact_set_delivery":
            return super()._tool_start_message(
                tool_name,
                arguments,
                progress_locale=progress_locale,
            )

        artifact_ids = arguments.get("artifact_ids")
        count = len(artifact_ids) if isinstance(artifact_ids, list) else 0
        selected = arguments.get("selected", True) is not False
        return self._progress_text(
            artifact_delivery_start_message_key(selected=selected, count=count),
            locale_name=progress_locale,
            file_count=count,
        )

    async def _record_delivery_outcome(
        self,
        outcome,
        context: ManagerToolContext,
    ) -> None:
        if outcome.event_type is None:
            return

        safe_data: dict[str, Any] = {}
        deliveries = outcome.payload.get("items") or []
        for delivery in deliveries:
            if not isinstance(delivery, dict):
                continue
            for key in (
                "delivery_id",
                "artifact_id",
                "filename",
                "format_id",
                "size_bytes",
                "client_type",
                "state",
            ):
                if delivery.get(key) is not None:
                    safe_data.setdefault(f"{key}s", []).append(delivery[key])

        if deliveries:
            safe_data["requested_count"] = outcome.payload.get(
                "requested_count", len(deliveries)
            )
            safe_data["selected_count"] = outcome.payload.get("selected_count", 0)
            safe_data["cancelled_count"] = outcome.payload.get("cancelled_count", 0)

        projection = artifact_delivery_message_projection(
            safe_data.get("filenames") or []
        )
        safe_data.update({
            "filename_count": projection["file_count"],
            "filenames_preview": projection["filenames_preview"],
            "filenames_preview_count": projection["filenames_preview_count"],
            "filenames_omitted_count": projection["filenames_omitted_count"],
        })

        self._trace_event(
            context.active_cycle.cycle_trace,
            outcome.event_type,
            **safe_data,
        )
        await self._emit_progress_event(
            state=context.session_state,
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            progress_callback=context.progress_callback,
            cycle_trace=context.active_cycle.cycle_trace,
            event_type=outcome.event_type,
            message_key=artifact_delivery_event_message_key(
                outcome.event_type,
                count=projection["file_count"],
            ),
            severity=outcome.severity,
            visibility=outcome.visibility,
            data=safe_data,
            message_kwargs=projection,
        )
