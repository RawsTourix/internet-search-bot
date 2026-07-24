"""Recover bounded exact previews after artifact composite process limiting."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from mcp.types import TextContent

from ..artifacts.models import ArtifactAccessContext
from .manager_runtime_context import get_manager_context


class ArtifactCompositePreviewMixin:
    """Enrich stored-only artifact items without exposing full exact contents."""

    _ARTIFACT_PREVIEW_TOOLS = frozenset({
        "artifact_read_text",
        "artifact_search_text",
    })

    async def _call_registered_tool(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
    ):
        result = await super()._call_registered_tool(public_tool_name, arguments)
        if public_tool_name not in self._ARTIFACT_PREVIEW_TOOLS:
            return result

        content = getattr(result, "content", None)
        if not isinstance(content, list) or not content:
            return result
        raw_text = getattr(content[0], "text", None)
        if not isinstance(raw_text, str):
            return result
        try:
            payload = json.loads(raw_text)
        except Exception:
            return result
        if not isinstance(payload, dict):
            return result

        items = payload.get("items")
        if not isinstance(items, list):
            return result
        targets = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("status") == "ok"
            and item.get("representation") == "stored_only"
            and not item.get("preview")
        ]
        if not targets:
            return result

        context = get_manager_context()
        artifact_services = getattr(self, "artifact_services", None)
        if context is None or artifact_services is None:
            return result
        access = ArtifactAccessContext(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            allowed_artifact_ids=context.active_cycle.artifact_refs,
        )
        total_preview_chars = max(
            1,
            int(getattr(
                getattr(self, "memory_config", None),
                "result_preview_max_chars",
                1000,
            )),
        )
        per_item_chars = max(1, total_preview_chars // len(targets))
        query = str(arguments.get("query") or "").strip()

        changed = False
        for item in targets:
            artifact = item.get("artifact")
            artifact_id = (
                artifact.get("artifact_id")
                if isinstance(artifact, dict)
                else None
            )
            if not isinstance(artifact_id, str):
                continue
            try:
                if public_tool_name == "artifact_read_text":
                    exact = await artifact_services.artifact_service.read_text(
                        artifact_id,
                        access=access,
                        offset_chars=0,
                        limit_chars=min(
                            per_item_chars,
                            artifact_services.config.max_read_chars,
                        ),
                    )
                    preview = exact.text
                elif query:
                    exact = await artifact_services.artifact_service.search_text(
                        artifact_id,
                        access=access,
                        query=query,
                        limit=1,
                    )
                    preview = json.dumps(
                        {"matches": [
                            match.model_dump(mode="json")
                            for match in exact.matches
                        ]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                else:
                    continue
            except Exception:
                continue
            if not preview:
                continue
            item["preview"] = self._bounded_artifact_preview(
                preview,
                max_chars=per_item_chars,
            )
            changed = True

        if not changed:
            return result
        return SimpleNamespace(
            content=[TextContent(
                type="text",
                text=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )],
            execution_disposition=getattr(
                result,
                "execution_disposition",
                "succeeded",
            ),
            result_policy=getattr(result, "result_policy", "default"),
        )

    @staticmethod
    def _bounded_artifact_preview(value: str, *, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        suffix = "…"
        if max_chars <= len(suffix):
            return suffix[:max_chars]
        return value[: max_chars - len(suffix)] + suffix

    def _artifact_composite_item_preview(
        self,
        item: dict[str, Any],
        *,
        max_chars: int,
    ) -> str | None:
        preview = item.get("preview")
        if isinstance(preview, str) and preview:
            return self._bounded_artifact_preview(preview, max_chars=max_chars)
        return super()._artifact_composite_item_preview(
            item,
            max_chars=max_chars,
        )
