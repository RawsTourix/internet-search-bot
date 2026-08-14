"""Transport-neutral IR-6 durable semantic emission services."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import InputRuntimeConfigType
from .errors import InputRuntimeConflictError
from .models import AgentEmission, EmissionState, new_emission_id


Clock = Callable[[], datetime]
DeliveryWake = Callable[[str, str], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ManagerToolExecutionContext:
    """Runtime-owned authority for one native manager-tool call."""

    session_id: str
    cycle_id: str
    generation: int
    context_revision_id: str
    tool_call_id: str
    original_input_batch_id: str


@dataclass(frozen=True, slots=True)
class AgentEmissionAcceptance:
    accepted: bool
    emission: AgentEmission | None = None
    reason_code: str | None = None
    duplicate: bool = False


class AgentEmissionDeliveryReceipt(BaseModel):
    """Minimal durable client receipt used for audit and reply binding."""

    model_config = ConfigDict(extra="forbid")

    emission_id: str
    session_id: str
    cycle_id: str
    generation: int = Field(ge=0)
    claim_token: str
    attempt_number: int = Field(ge=1)
    client_type: str
    client_instance_id: str
    conversation_id: str
    thread_id: str | None = None
    external_message_id: str
    delivered_at: datetime

    @field_validator(
        "emission_id",
        "session_id",
        "cycle_id",
        "claim_token",
        "client_type",
        "client_instance_id",
        "conversation_id",
        "external_message_id",
    )
    @classmethod
    def non_empty(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        if info.field_name == "emission_id":
            import re

            if re.fullmatch(r"emit_[0-9a-f]{32}", normalized) is None:
                raise ValueError("invalid emission_id")
        return normalized

    @field_validator("client_type")
    @classmethod
    def normalized_client_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("client_type must not be empty")
        return normalized

    @field_validator("thread_id")
    @classmethod
    def optional_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("delivered_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("delivered_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class AgentEmissionCommandRepository(Protocol):
    async def accept_intermediate(
        self,
        candidate: AgentEmission,
        *,
        max_messages: int,
        min_interval_seconds: float,
    ) -> AgentEmissionAcceptance: ...

    async def get_by_idempotency_key(
        self,
        cycle_id: str,
        idempotency_key: str,
    ) -> AgentEmission | None: ...

    async def list_ready_for_client(
        self,
        *,
        client_type: str,
        client_instance_id: str,
        limit: int,
        now: datetime,
    ) -> tuple[AgentEmission, ...]: ...

    async def claim_for_client(
        self,
        emission_id: str,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
        claim_token: str,
        claimed_at: datetime,
        lease_seconds: int,
    ) -> AgentEmission: ...

    async def record_delivery_receipt(
        self,
        receipt: AgentEmissionDeliveryReceipt,
    ) -> AgentEmission: ...

    async def fail_for_client(
        self,
        emission_id: str,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
        claim_token: str,
        state: str,
        error_code: str,
    ) -> AgentEmission: ...

    async def fail_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        state: str,
        error_code: str,
    ) -> AgentEmission: ...

    async def recover_expired_delivery_claims(
        self,
        *,
        now: datetime,
    ) -> tuple[AgentEmission, ...]: ...

    async def resolve_delivered_reply(
        self,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
        conversation_id: str,
        thread_id: str | None,
        external_message_id: str,
    ) -> AgentEmission | None: ...


class AgentEmissionService:
    """Persist semantic intermediate intent without waiting for client delivery."""

    TOOL_NAME = "send_user_message"

    def __init__(
        self,
        *,
        config: InputRuntimeConfigType,
        repository: AgentEmissionCommandRepository,
        committed_batches: Any,
        clock: Clock | None = None,
        delivery_wake: DeliveryWake | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.committed_batches = committed_batches
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.delivery_wake = delivery_wake

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def idempotency_key(context: ManagerToolExecutionContext) -> str:
        material = (
            f"{AgentEmissionService.TOOL_NAME}\0{context.cycle_id}\0"
            f"{context.generation}\0{context.tool_call_id}"
        ).encode("utf-8")
        return "agent-emission:" + hashlib.sha256(material).hexdigest()

    @staticmethod
    def _semantic_message(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("message must be a string")
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    @staticmethod
    def _string(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip()

    async def _trusted_route(
        self,
        context: ManagerToolExecutionContext,
    ) -> dict[str, Any] | None:
        try:
            batch = await self.committed_batches.get_committed(
                context.original_input_batch_id
            )
        except Exception:
            return None
        if self._string(getattr(batch, "session_id", "")) != context.session_id:
            return None
        route = getattr(batch, "response_route", None)
        capability = getattr(batch, "capability_snapshot", None)
        if route is None or capability is None:
            return None
        client_type = self._string(getattr(capability, "client_type", "")).lower()
        client_instance_id = self._string(
            getattr(capability, "client_instance_id", "")
        )
        conversation_id = self._string(getattr(route, "conversation_id", ""))
        if not client_type or not client_instance_id or not conversation_id:
            return None
        route_type = self._string(getattr(route, "route_type", "")).lower()
        if route_type and route_type != client_type:
            return None
        thread_id = self._string(getattr(route, "thread_id", "")) or None
        anchor = getattr(batch, "response_anchor", None)
        reply_to = None
        anchor_id = None
        if anchor is not None:
            reply_to = self._string(getattr(anchor, "client_message_id", "")) or None
            anchor_id = self._string(getattr(anchor, "anchor_id", "")) or None
        if reply_to is None:
            reply_to = self._string(getattr(route, "reply_to_message_id", "")) or None
        capability_snapshot_id = self._string(
            getattr(capability, "capability_snapshot_id", "")
        )
        if not capability_snapshot_id:
            return None
        values = (
            client_type,
            client_instance_id,
            conversation_id,
            thread_id or "",
            reply_to or "",
            anchor_id or "",
            capability_snapshot_id,
        )
        if any(len(value) > 512 for value in values):
            return None
        return {
            "client_type": client_type,
            "client_instance_id": client_instance_id,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "reply_to_message_id": reply_to,
            "response_anchor_id": anchor_id,
            "capability_snapshot_id": capability_snapshot_id,
        }

    @staticmethod
    def _same_replay_semantics(
        emission: AgentEmission,
        *,
        context: ManagerToolExecutionContext,
        text: str,
        importance: str,
        idempotency_key: str,
    ) -> bool:
        return (
            emission.session_id == context.session_id
            and emission.cycle_id == context.cycle_id
            and emission.generation == context.generation
            and emission.context_revision_id == context.context_revision_id
            and emission.kind == "intermediate"
            and emission.text == text
            and emission.visibility == "user"
            and emission.importance == importance
            and emission.idempotency_key == idempotency_key
        )

    async def emit_intermediate(
        self,
        *,
        context: ManagerToolExecutionContext,
        message: Any,
        kind: Any = "intermediate",
        importance: Any = "normal",
    ) -> dict[str, Any]:
        if str(kind or "intermediate").strip().lower() != "intermediate":
            return self._rejected("unsupported_kind")
        normalized_importance = str(importance or "normal").strip().lower()
        if normalized_importance not in {"normal", "high"}:
            return self._rejected("invalid_importance")
        try:
            text = self._semantic_message(message)
        except ValueError:
            return self._rejected("invalid_message")
        if not text:
            return self._rejected("empty_message")
        if len(text) > self.config.max_intermediate_message_chars:
            return self._rejected("message_too_long")

        idempotency_key = self.idempotency_key(context)
        existing = await self.repository.get_by_idempotency_key(
            context.cycle_id,
            idempotency_key,
        )
        if existing is not None:
            if not self._same_replay_semantics(
                existing,
                context=context,
                text=text,
                importance=normalized_importance,
                idempotency_key=idempotency_key,
            ):
                return self._rejected("idempotency_conflict")
            return self._accepted(existing, duplicate=True)

        route = await self._trusted_route(context)
        if route is None:
            return self._rejected("route_unavailable")

        candidate = AgentEmission(
            emission_id=new_emission_id(),
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            generation=context.generation,
            context_revision_id=context.context_revision_id,
            kind="intermediate",
            text=text,
            visibility="user",
            importance=normalized_importance,
            response_route=route,
            state=EmissionState.READY,
            idempotency_key=idempotency_key,
            created_at=self._now(),
        )
        try:
            accepted = await self.repository.accept_intermediate(
                candidate,
                max_messages=self.config.max_intermediate_messages_per_cycle,
                min_interval_seconds=(
                    self.config.min_intermediate_message_interval_seconds
                ),
            )
        except InputRuntimeConflictError:
            return self._rejected("idempotency_conflict")
        if not accepted.accepted or accepted.emission is None:
            return self._rejected(accepted.reason_code or "policy_rejected")

        emission = accepted.emission
        if self.delivery_wake is not None:
            try:
                await self.delivery_wake(emission.session_id, emission.emission_id)
            except Exception:
                pass
        return self._accepted(emission, duplicate=accepted.duplicate)

    @staticmethod
    def _accepted(emission: AgentEmission, *, duplicate: bool) -> dict[str, Any]:
        return {
            "type": "agent_emission_result",
            "emission_id": emission.emission_id,
            "accepted": True,
            "persistence_state": emission.state.value,
            "delivery_required_for_cycle": False,
            "duplicate": duplicate,
        }

    @staticmethod
    def _rejected(reason_code: str) -> dict[str, Any]:
        return {
            "type": "agent_emission_result",
            "accepted": False,
            "reason_code": reason_code,
            "delivery_required_for_cycle": False,
        }


class AgentEmissionOutboxService:
    """Route-scoped worker facade over the durable emission repository."""

    MAX_LIMIT = 200

    def __init__(
        self,
        repository: AgentEmissionCommandRepository,
        *,
        clock: Clock | None = None,
        claim_lease_seconds: int = 300,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.claim_lease_seconds = max(1, int(claim_lease_seconds))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    async def list_ready(
        self,
        *,
        client_type: str,
        client_instance_id: str,
        limit: int = 50,
    ) -> tuple[AgentEmission, ...]:
        bounded = int(limit)
        if not 1 <= bounded <= self.MAX_LIMIT:
            raise ValueError("emission outbox limit is out of range")
        now = self._now()
        await self.repository.recover_expired_delivery_claims(now=now)
        return await self.repository.list_ready_for_client(
            client_type=client_type.strip().lower(),
            client_instance_id=client_instance_id.strip(),
            limit=bounded,
            now=now,
        )

    async def claim(
        self,
        emission_id: str,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
        claim_token: str,
    ) -> AgentEmission:
        return await self.repository.claim_for_client(
            emission_id,
            session_id=session_id,
            client_type=client_type.strip().lower(),
            client_instance_id=client_instance_id.strip(),
            claim_token=claim_token,
            claimed_at=self._now(),
            lease_seconds=self.claim_lease_seconds,
        )

    async def delivered(
        self,
        *,
        emission: AgentEmission,
        claim_token: str,
        external_message_id: str,
        delivered_at: datetime | None = None,
    ) -> AgentEmission:
        route = emission.response_route
        receipt = AgentEmissionDeliveryReceipt(
            emission_id=emission.emission_id,
            session_id=emission.session_id,
            cycle_id=emission.cycle_id,
            generation=emission.generation,
            claim_token=claim_token,
            attempt_number=emission.delivery_attempt_count,
            client_type=str(route["client_type"]),
            client_instance_id=str(route["client_instance_id"]),
            conversation_id=str(route["conversation_id"]),
            thread_id=(str(route["thread_id"]) if route.get("thread_id") is not None else None),
            external_message_id=str(external_message_id),
            delivered_at=delivered_at or self._now(),
        )
        return await self.repository.record_delivery_receipt(receipt)

    async def failed(
        self,
        emission_id: str,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
        claim_token: str,
        error_code: str,
        ambiguous: bool,
    ) -> AgentEmission:
        return await self.repository.fail_for_client(
            emission_id,
            session_id=session_id,
            client_type=client_type.strip().lower(),
            client_instance_id=client_instance_id.strip(),
            claim_token=claim_token,
            state=(EmissionState.UNKNOWN.value if ambiguous else EmissionState.FAILED.value),
            error_code=error_code,
        )


class _ReplyAwareBatch:
    __slots__ = ("_batch", "reply_to_emission")

    def __init__(self, batch: Any, reply_to_emission: dict[str, str] | None) -> None:
        self._batch = batch
        self.reply_to_emission = reply_to_emission

    def __getattr__(self, name: str) -> Any:
        return getattr(self._batch, name)


class ReplyAwareCommittedBatchReader:
    """Project optional trusted reply binding without mutating ingress records."""

    def __init__(self, committed_batches: Any, repository: AgentEmissionCommandRepository) -> None:
        self.committed_batches = committed_batches
        self.repository = repository

    async def get_committed(self, input_batch_id: str) -> Any:
        batch = await self.committed_batches.get_committed(input_batch_id)
        capability = getattr(batch, "capability_snapshot", None)
        route = getattr(batch, "response_route", None)
        if capability is None or route is None:
            return _ReplyAwareBatch(batch, None)
        reply_contexts = tuple(getattr(batch, "reply_contexts", ()) or ())
        external_id = None
        for context in reversed(reply_contexts):
            value = getattr(context, "replied_to_message_id", None)
            if value is not None and str(value).strip():
                external_id = str(value).strip()
                break
        if external_id is None:
            return _ReplyAwareBatch(batch, None)
        client_type_value = getattr(capability, "client_type", "")
        client_type = str(
            getattr(client_type_value, "value", client_type_value) or ""
        ).strip().lower()
        client_instance_id = str(
            getattr(capability, "client_instance_id", "") or ""
        ).strip()
        conversation_id = str(getattr(route, "conversation_id", "") or "").strip()
        thread_raw = getattr(route, "thread_id", None)
        thread_id = str(thread_raw).strip() if thread_raw is not None else None
        if thread_id == "":
            thread_id = None
        session_id = str(getattr(batch, "session_id", "") or "").strip()
        if not all((session_id, client_type, client_instance_id, conversation_id)):
            return _ReplyAwareBatch(batch, None)
        emission = await self.repository.resolve_delivered_reply(
            session_id=session_id,
            client_type=client_type,
            client_instance_id=client_instance_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            external_message_id=external_id,
        )
        relation = None
        if emission is not None and emission.kind == "intermediate":
            relation = {
                "emission_id": emission.emission_id,
                "kind": "intermediate",
            }
        return _ReplyAwareBatch(batch, relation)
