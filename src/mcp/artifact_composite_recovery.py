"""Recovery guidance and repeat guards for artifact composite results."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

from mcp.types import TextContent

from .manager_runtime_context import get_manager_context


class ArtifactCompositeRecoveryMixin:
    """Recover from non-inline batches without repeating immutable results."""

    _RECOVERABLE_BATCH_TOOLS = frozenset({
        "artifact_read_text",
        "artifact_search_text",
    })

    async def _call_registered_tool(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
    ):
        signature = self._artifact_batch_signature(
            public_tool_name,
            arguments,
        )
        context = get_manager_context()
        blocked = (
            signature is not None
            and context is not None
            and signature
            in context.active_cycle.blocked_artifact_batch_signatures
        )
        if not blocked:
            return await super()._call_registered_tool(public_tool_name, arguments)

        artifact_ids = self._normalized_artifact_ids(arguments)
        payload = {
            "type": "artifact_batch_repetition_rejected",
            "status": "rejected",
            "tool_name": public_tool_name,
            "message": (
                "This exact immutable artifact batch already produced a "
                "stored-only/oversized representation in the current cycle. "
                "Repeating it would return the same content."
            ),
            "retryable": True,
            "recommended_action": "split_artifact_batch",
            "do_not_repeat_same_batch": True,
            "suggested_batches": self._suggested_batches(artifact_ids),
        }
        if hasattr(self, "_trace_event"):
            self._trace_event(
                context.active_cycle.cycle_trace,
                "artifact_batch_repetition_rejected",
                tool_name=public_tool_name,
                artifact_count=len(artifact_ids),
                signature=signature,
            )
        return SimpleNamespace(
            content=[TextContent(
                type="text",
                text=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )],
            execution_disposition="rejected",
            result_policy="inline_receipt",
        )

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
            visible.update({
                "recommended_action": "split_artifact_batch",
                "do_not_repeat_same_batch": True,
                "suggested_batches": self._suggested_batches(requested_ids),
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

        signature = self._artifact_batch_signature(
            effective_tool_name,
            {
                "artifact_ids": requested_ids,
                "query": visible.get("query"),
            },
        )
        context = get_manager_context()
        if signature is not None and context is not None:
            blocked_signatures = (
                context.active_cycle.blocked_artifact_batch_signatures
            )
            if signature not in blocked_signatures:
                blocked_signatures.append(signature)
        return visible

    @classmethod
    def _artifact_batch_signature(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        if tool_name not in cls._RECOVERABLE_BATCH_TOOLS:
            return None
        artifact_ids = cls._normalized_artifact_ids(arguments)
        if not artifact_ids:
            return None
        payload = {
            "tool_name": tool_name,
            "artifact_ids": sorted(set(artifact_ids)),
            "query": (
                str(arguments.get("query") or "").strip()
                if tool_name == "artifact_search_text"
                else None
            ),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _normalized_artifact_ids(arguments: dict[str, Any]) -> list[str]:
        values = arguments.get("artifact_ids") or []
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _suggested_batches(artifact_ids: list[str]) -> list[list[str]]:
        if len(artifact_ids) <= 1:
            return []
        chunk_size = min(4, max(1, (len(artifact_ids) + 2) // 3))
        return [
            artifact_ids[index:index + chunk_size]
            for index in range(0, len(artifact_ids), chunk_size)
        ]
