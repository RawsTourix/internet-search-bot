"""Conservative recovery for structurally empty OpenAI-compatible responses."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from ..core.errors import LLMTransportError


logger = logging.getLogger("mcp_client")


class LLMResponseRecoveryMixin:
    """Retry one side-effect-free LLM request when HTTP 200 has no action.

    Some OpenAI-compatible providers can consume the whole completion budget in
    hidden reasoning and return an assistant message with neither visible
    content nor tool calls. The response is transport-successful but unusable by
    the agent protocol. Retrying the LLM request is safe because no tool call was
    emitted and therefore no external side effect has started.
    """

    async def _call_llm_with_retries(
        self,
        messages,
        tools,
        *,
        context: str = "LLM call",
        state=None,
        session_id: str | None = None,
        cycle_id: str | None = None,
        progress_callback=None,
        cycle_trace=None,
        max_tokens_override: int | None = None,
        temperature_override: float | None = None,
        top_p_override: float | None = None,
        redact_error_details: bool = True,
        defer_context_overflow_error: bool = False,
    ):
        call_kwargs = {
            "context": context,
            "state": state,
            "session_id": session_id,
            "cycle_id": cycle_id,
            "progress_callback": progress_callback,
            "cycle_trace": cycle_trace,
            "max_tokens_override": max_tokens_override,
            "temperature_override": temperature_override,
            "top_p_override": top_p_override,
            "redact_error_details": redact_error_details,
            "defer_context_overflow_error": defer_context_overflow_error,
        }
        response = await super()._call_llm_with_retries(
            messages,
            tools,
            **call_kwargs,
        )
        response = self._normalize_legacy_function_call(response)
        if self._has_actionable_llm_output(response):
            return response

        first_diagnostics = self._empty_response_diagnostics(response)
        retry_max_tokens = self._empty_response_retry_max_tokens(
            tools=tools,
            max_tokens_override=max_tokens_override,
        )
        logger.warning(
            "%s: empty LLM response recovered with one bounded retry; "
            "cycle_id=%s finish_reason=%s completion_tokens=%s "
            "reasoning_chars=%s retry_max_tokens=%s",
            context,
            cycle_id,
            first_diagnostics["finish_reason"],
            first_diagnostics["completion_tokens"],
            first_diagnostics["reasoning_chars"],
            retry_max_tokens,
        )
        self._record_empty_response_event(
            cycle_trace,
            "llm_empty_response_retry",
            context=context,
            diagnostics=first_diagnostics,
            retry_max_tokens=retry_max_tokens,
        )

        retry_kwargs = dict(call_kwargs)
        retry_kwargs["max_tokens_override"] = retry_max_tokens
        retry_kwargs["temperature_override"] = 0.0
        retry_response = await super()._call_llm_with_retries(
            messages,
            tools,
            **retry_kwargs,
        )
        retry_response = self._normalize_legacy_function_call(retry_response)
        if self._has_actionable_llm_output(retry_response):
            self._record_empty_response_event(
                cycle_trace,
                "llm_empty_response_recovered",
                context=context,
                diagnostics=self._empty_response_diagnostics(retry_response),
                retry_max_tokens=retry_max_tokens,
            )
            return retry_response

        final_diagnostics = self._empty_response_diagnostics(retry_response)
        self._record_empty_response_event(
            cycle_trace,
            "llm_empty_response_exhausted",
            context=context,
            diagnostics=final_diagnostics,
            retry_max_tokens=retry_max_tokens,
        )
        logger.error(
            "%s: LLM returned no actionable output after bounded retry; "
            "cycle_id=%s finish_reason=%s completion_tokens=%s "
            "reasoning_chars=%s",
            context,
            cycle_id,
            final_diagnostics["finish_reason"],
            final_diagnostics["completion_tokens"],
            final_diagnostics["reasoning_chars"],
        )
        raise LLMTransportError(
            "LLM returned an empty assistant response after one safe retry"
        )

    def _empty_response_retry_max_tokens(
        self,
        *,
        tools,
        max_tokens_override: int | None,
    ) -> int | None:
        configured = (
            int(max_tokens_override)
            if max_tokens_override is not None
            else int(self.llm_config.max_tokens)
        )
        if not tools or max_tokens_override is not None:
            return configured
        reserved = int(
            self.llm_config.reserved_output_tokens or configured
        )
        return max(configured, min(reserved, configured * 2))

    @classmethod
    def _has_actionable_llm_output(cls, response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        content = response.get("content")
        if isinstance(content, str) and content.strip():
            return True
        tool_calls = response.get("tool_calls")
        return isinstance(tool_calls, list) and bool(tool_calls)

    @staticmethod
    def _normalize_legacy_function_call(response: Any):
        if not isinstance(response, dict):
            return response
        if response.get("tool_calls"):
            return response
        function_call = response.get("function_call")
        if not isinstance(function_call, dict):
            return response
        name = str(function_call.get("name") or "").strip()
        if not name:
            return response
        normalized = dict(response)
        normalized["tool_calls"] = [
            {
                "id": f"legacy-function-{uuid4().hex}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": function_call.get("arguments") or "{}",
                },
            }
        ]
        return normalized

    def _empty_response_diagnostics(self, response: Any) -> dict[str, Any]:
        payload = response if isinstance(response, dict) else {}
        metadata = payload.get(self.LLM_RUNTIME_METADATA_KEY)
        metadata = metadata if isinstance(metadata, dict) else {}
        reasoning = payload.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = payload.get("reasoning")
        return {
            "finish_reason": metadata.get("finish_reason"),
            "prompt_tokens": metadata.get("prompt_tokens"),
            "completion_tokens": metadata.get("completion_tokens"),
            "total_tokens": metadata.get("total_tokens"),
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
            "response_keys": sorted(str(key) for key in payload),
        }

    def _record_empty_response_event(
        self,
        cycle_trace,
        event_type: str,
        *,
        context: str,
        diagnostics: dict[str, Any],
        retry_max_tokens: int | None,
    ) -> None:
        if cycle_trace is None:
            return
        self._trace_event(
            cycle_trace,
            event_type,
            context=context,
            retry_max_tokens=retry_max_tokens,
            **diagnostics,
        )
