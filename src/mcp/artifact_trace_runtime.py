"""Session-level tracing projection for native artifact tool outcomes."""

from __future__ import annotations

from typing import Any

from ..artifacts.tools import ToolExecutionDisposition


class ArtifactLifecycleTraceMixin:
    """Append safe artifact events after the existing runtime outcome hook."""

    async def _record_artifact_outcome(self, outcome, context) -> None:
        await super()._record_artifact_outcome(outcome, context)
        trace_service = getattr(self, "artifact_trace_service", None)
        if trace_service is None:
            return

        payload = dict(outcome.payload or {})
        event_type = outcome.event_type
        if event_type is None:
            # artifact_search_text historically has no user-progress event. Its
            # structured composite type is nevertheless an authoritative and
            # safe signal for the diagnostic lifecycle trace.
            if payload.get("type") == "artifact_batch_search":
                event_type = "artifact_search_completed"
            else:
                return

        artifact = payload.get("artifact")
        artifact_projection = (
            self._artifact_trace_projection(artifact)
            if isinstance(artifact, dict)
            else None
        )
        disposition = getattr(
            outcome.disposition,
            "value",
            outcome.disposition,
        )
        status = {
            ToolExecutionDisposition.SUCCEEDED.value: "succeeded",
            ToolExecutionDisposition.REJECTED.value: "failed",
            ToolExecutionDisposition.FAILED.value: "failed",
        }.get(str(disposition), "observed")

        data: dict[str, Any] = {}
        metrics: dict[str, int | float | str | bool | None] = {}
        correlation: dict[str, Any] = {}
        for key in (
            "artifact_lineage_id",
            "expected_current_artifact_id",
            "current_artifact_id",
            "source_candidate_id",
            "code",
        ):
            if payload.get(key) is not None:
                data[key] = payload[key]
        if payload.get("source_candidate_id") is not None:
            correlation["candidate_id"] = payload["source_candidate_id"]

        if event_type in {
            "artifact_read_completed",
            "artifact_search_completed",
        }:
            items = [
                item for item in (payload.get("items") or [])
                if isinstance(item, dict)
            ]
            artifact_ids = list(dict.fromkeys(
                str(
                    item.get("requested_artifact_id")
                    or (item.get("artifact") or {}).get("artifact_id")
                    or ""
                )
                for item in items
                if (
                    item.get("requested_artifact_id")
                    or (item.get("artifact") or {}).get("artifact_id")
                )
            ))
            data["artifact_ids"] = artifact_ids
            metrics.update({
                "requested_count": int(
                    payload.get("requested_count") or len(items)
                ),
                "successful_count": int(
                    payload.get("successful_count") or 0
                ),
                "failed_count": int(payload.get("failed_count") or 0),
            })
            if event_type == "artifact_read_completed":
                data["complete_artifact_ids"] = list(dict.fromkeys(
                    str(item.get("requested_artifact_id"))
                    for item in items
                    if item.get("status") == "ok"
                    and item.get("complete") is True
                    and item.get("requested_artifact_id")
                ))
                data["partial_artifact_ids"] = list(dict.fromkeys(
                    str(item.get("requested_artifact_id"))
                    for item in items
                    if item.get("status") == "ok"
                    and item.get("complete") is not True
                    and item.get("requested_artifact_id")
                ))

        error = None
        if status == "failed":
            error = {
                "error_type": "ArtifactToolOutcomeError",
                "error_code": payload.get("code"),
                "message": payload.get("message") or payload.get("error"),
                "retryable": payload.get("retryable"),
            }

        await trace_service.record(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            event_type=str(event_type),
            stage="artifact_runtime",
            status=status,
            direction="internal",
            correlation=correlation,
            artifact=artifact_projection,
            metrics=metrics,
            error=error,
            data=data,
        )

    @staticmethod
    def _artifact_trace_projection(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value[key]
            for key in (
                "artifact_id",
                "artifact_lineage_id",
                "content_id",
                "filename",
                "format_id",
                "mime_type",
                "size_bytes",
                "content_hash",
                "purpose",
                "version",
            )
            if value.get(key) is not None
        }
