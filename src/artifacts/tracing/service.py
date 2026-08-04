"""Best-effort artifact trace recording with bounded redaction."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from .interfaces import ArtifactTraceStore
from .models import (
    ArtifactTraceArtifact,
    ArtifactTraceCorrelation,
    ArtifactTraceError,
    ArtifactTraceEvent,
    ArtifactTraceTransport,
)


logger = logging.getLogger("Artifacts.Trace")

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "download_url",
    "file_path",
    "local_path",
    "password",
    "presigned_url",
    "secret",
    "set_cookie",
    "token",
    "workspace_path",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_cookie",
    "_password",
    "_path",
    "_secret",
    "_token",
    "_url",
)
_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|signature|sig)"
    r"\s*([:=])\s*([^\s,;&]+)"
)
_URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s]+")
_WINDOWS_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Z]:\\[^\r\n\t ]+"
)
_POSIX_PATH_PATTERN = re.compile(
    r"(?<![:A-Za-z0-9_])/(?:[^/\s]+/)+[^/\s]*"
)


class ArtifactTraceService:
    """Validate and append diagnostics without affecting domain transactions."""

    def __init__(
        self,
        store: ArtifactTraceStore,
        *,
        enabled: bool = True,
        max_string_chars: int = 2_000,
    ) -> None:
        if max_string_chars < 128:
            raise ValueError("artifact trace string limit is too small")
        self.store = store
        self.enabled = bool(enabled)
        self.max_string_chars = int(max_string_chars)

    async def record(
        self,
        *,
        session_id: str,
        event_type: str,
        stage: str,
        status: str,
        direction: str = "internal",
        occurred_at: datetime | None = None,
        cycle_id: str | None = None,
        operation_id: str | None = None,
        correlation: Mapping[str, Any] | None = None,
        transport: Mapping[str, Any] | None = None,
        artifact: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        error: BaseException | Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> ArtifactTraceEvent | None:
        if not self.enabled:
            return None
        try:
            event = ArtifactTraceEvent(
                occurred_at=(occurred_at or datetime.now(timezone.utc)),
                session_id=session_id,
                cycle_id=cycle_id,
                operation_id=operation_id,
                event_type=event_type,
                stage=stage,
                status=status,
                direction=direction,
                correlation=ArtifactTraceCorrelation.model_validate(
                    self._sanitize_mapping(correlation or {})
                ),
                transport=(
                    ArtifactTraceTransport.model_validate(
                        self._sanitize_mapping(transport)
                    )
                    if transport
                    else None
                ),
                artifact=(
                    ArtifactTraceArtifact.model_validate(
                        self._sanitize_mapping(artifact)
                    )
                    if artifact
                    else None
                ),
                metrics=self._sanitize_mapping(metrics or {}),
                error=self._build_error(error),
                data=self._sanitize_mapping(data or {}),
            )
            await self.store.append(event)
            return event
        except Exception as trace_error:
            logger.warning(
                "artifact_trace_write_failed session_id=%s event_type=%s "
                "error_type=%s",
                session_id,
                event_type,
                type(trace_error).__name__,
            )
            return None

    async def list_session(self, session_id: str) -> list[ArtifactTraceEvent]:
        return await self.store.list_session(session_id)

    def _build_error(
        self,
        value: BaseException | Mapping[str, Any] | None,
    ) -> ArtifactTraceError | None:
        if value is None:
            return None
        if isinstance(value, BaseException):
            return ArtifactTraceError(
                error_type=type(value).__name__,
                message=self._sanitize_string(str(value)),
            )
        sanitized = self._sanitize_mapping(value)
        error_type = str(
            sanitized.pop("error_type", None)
            or sanitized.pop("type", None)
            or "ArtifactTraceError"
        )
        return ArtifactTraceError(
            error_type=error_type,
            error_code=self._optional_string(
                sanitized.pop("error_code", None)
                or sanitized.pop("code", None)
            ),
            message=self._optional_string(sanitized.pop("message", None)),
            retryable=(
                bool(sanitized["retryable"])
                if sanitized.get("retryable") is not None
                else None
            ),
        )

    def _sanitize_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if self._is_sensitive_key(key):
                continue
            result[key] = self._sanitize_value(raw_value)
        return result

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        normalized = key.casefold().replace("-", "_").strip()
        return (
            normalized in _SENSITIVE_KEYS
            or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)
        )

    def _sanitize_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self._sanitize_mapping(value)
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._sanitize_value(item) for item in value]
        if isinstance(value, str):
            return self._sanitize_string(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if hasattr(value, "value"):
            return self._sanitize_value(value.value)
        return self._sanitize_string(str(value))

    def _sanitize_string(self, value: str) -> str:
        normalized = value.replace("\x00", "").strip()
        normalized = _BEARER_PATTERN.sub("Bearer [REDACTED]", normalized)
        normalized = _SECRET_ASSIGNMENT_PATTERN.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}[REDACTED]"
            ),
            normalized,
        )
        normalized = _URL_PATTERN.sub("[REDACTED_URL]", normalized)
        normalized = _WINDOWS_PATH_PATTERN.sub(
            "[REDACTED_PATH]",
            normalized,
        )
        normalized = _POSIX_PATH_PATTERN.sub(
            "[REDACTED_PATH]",
            normalized,
        )
        if len(normalized) > self.max_string_chars:
            return normalized[: self.max_string_chars - 1] + "…"
        return normalized

    def _optional_string(self, value: Any) -> str | None:
        if value is None:
            return None
        normalized = self._sanitize_string(str(value))
        return normalized or None
