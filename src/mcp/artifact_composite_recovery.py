"""Recovery guidance for non-inline artifact composite representations."""

from __future__ import annotations

from typing import Any


class ArtifactCompositeRecoveryMixin:
    """Tell the agent how to recover without repeating an identical batch."""

    def _prepare_structured_tool_result_representation(
        self,
        *,
        effective_tool_name: str,
        tool_payload: dict[str, Any],
        stored_result_ref,
        summary,
        decision,
        result_metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        visible = super()._prepare_structured_tool_result_representation(
            effective_tool_name=effective_tool_name,
            tool_payload=tool_payload,
            stored_result_ref=stored_result_ref,
            summary=summary,
            decision=decision,
            result_metadata=result_metadata,
        )
        if not isinstance(visible, dict):
            return visible
        if not visible.get("needs_retrieval"):
            return visible
        if visible.get("representation") == "summarized":
            return visible

        requested_ids: list[str] = []
        for item in visible.get("items") or []:
            if not isinstance(item, dict) or item.get("status") != "ok":
                continue
            artifact_id = item.get("requested_artifact_id")
            if isinstance(artifact_id, str) and artifact_id not in requested_ids:
                requested_ids.append(artifact_id)

        if len(requested_ids) > 1:
            chunk_size = min(4, max(1, (len(requested_ids) + 2) // 3))
            visible.update({
                "recommended_action": "split_artifact_batch",
                "do_not_repeat_same_batch": True,
                "suggested_batches": [
                    requested_ids[index:index + chunk_size]
                    for index in range(0, len(requested_ids), chunk_size)
                ],
                "recovery_note": (
                    "The exact immutable artifacts will return the same result for "
                    "an identical batch. Read the suggested smaller batches instead."
                ),
            })
        else:
            visible.update({
                "recommended_action": "report_retrieval_limit",
                "do_not_repeat_same_batch": True,
                "suggested_batches": [],
                "recovery_note": (
                    "This single exact artifact cannot be represented completely "
                    "within the current v0.4 context budget. Do not repeat the same "
                    "read; explain the limitation or narrow the operation."
                ),
            })
        return visible
