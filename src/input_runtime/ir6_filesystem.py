"""IR-6 production hardening for durable AgentEmission delivery semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._filesystem_common import validated_copy
from ._filesystem_delivery import _same_emission_relation
from ._filesystem_identity_recovery_delivery import (
    FileSystemAgentEmissionRepository as _IR2EmissionRepository,
)
from .emissions import AgentEmissionAcceptance, AgentEmissionDeliveryReceipt
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import AgentEmission, CycleStatus, EmissionState, SessionInputRuntimeState
from .serialization import atomic_write_model, list_models, read_model, storage_key


_TERMINAL_CYCLE_STATES = {
    CycleStatus.DONE,
    CycleStatus.ERROR,
    CycleStatus.CANCELLED,
}


class FileSystemAgentEmissionRepository(_IR2EmissionRepository):
    """Linearizable local implementation of the IR-6 emission command port."""

    def _session_state(self, session_id: str) -> SessionInputRuntimeState | None:
        path = self.layout.state(session_id)
        if not path.exists():
            return None
        return read_model(path, SessionInputRuntimeState)

    def _cycle_rows(self, cycle_id: str) -> tuple[AgentEmission, ...]:
        return list_models(self.layout.emissions(cycle_id), AgentEmission)

    @staticmethod
    def _route_value(emission: AgentEmission, name: str) -> str | None:
        value = emission.response_route.get(name)
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    def _receipt_path(self, emission_id: str) -> Path:
        return (
            self.layout.root
            / "emission-receipts"
            / f"{storage_key(emission_id)}.json"
        )

    @staticmethod
    def _reply_scope(
        *,
        client_type: str,
        client_instance_id: str,
        conversation_id: str,
        thread_id: str | None,
        external_message_id: str,
    ) -> str:
        return "\0".join(
            (
                client_type.strip().lower(),
                client_instance_id.strip(),
                conversation_id.strip(),
                (thread_id or "").strip(),
                external_message_id.strip(),
            )
        )

    def _reply_index_path(self, receipt: AgentEmissionDeliveryReceipt) -> Path:
        scope = self._reply_scope(
            client_type=receipt.client_type,
            client_instance_id=receipt.client_instance_id,
            conversation_id=receipt.conversation_id,
            thread_id=receipt.thread_id,
            external_message_id=receipt.external_message_id,
        )
        return (
            self.layout.root
            / "indexes"
            / "emission-reply"
            / f"{storage_key(scope)}.json"
        )

    def _read_receipt(self, emission_id: str) -> AgentEmissionDeliveryReceipt | None:
        path = self._receipt_path(emission_id)
        if not path.exists():
            return None
        return read_model(path, AgentEmissionDeliveryReceipt)

    async def accept_intermediate(
        self,
        candidate: AgentEmission,
        *,
        max_messages: int,
        min_interval_seconds: float,
    ) -> AgentEmissionAcceptance:
        if candidate.kind != "intermediate" or candidate.state != EmissionState.READY:
            raise ValueError("accept_intermediate requires READY intermediate emission")
        if max_messages <= 0 or min_interval_seconds < 0:
            raise ValueError("invalid intermediate emission policy")

        async with self.locks.hold_identity_then_session(
            self.root,
            candidate.session_id,
        ):
            existing = await self.get_by_idempotency_key(
                candidate.cycle_id,
                candidate.idempotency_key,
            )
            if existing is not None:
                if not _same_emission_relation(existing, candidate):
                    raise InputRuntimeConflictError(
                        "emission idempotency relation changed"
                    )
                self._restore_indexes(existing)
                return AgentEmissionAcceptance(
                    accepted=True,
                    emission=existing,
                    duplicate=True,
                )

            state = self._session_state(candidate.session_id)
            if (
                state is None
                or state.active_cycle_id != candidate.cycle_id
                or state.generation != candidate.generation
            ):
                return AgentEmissionAcceptance(
                    accepted=False,
                    reason_code="cycle_authority_unavailable",
                )
            if state.cycle_status not in {
                CycleStatus.RUNNING,
                CycleStatus.PAUSE_REQUESTED,
            }:
                return AgentEmissionAcceptance(
                    accepted=False,
                    reason_code=(
                        "cycle_terminal"
                        if state.cycle_status in _TERMINAL_CYCLE_STATES
                        else "cycle_not_running"
                    ),
                )
            if state.active_context_revision_id != candidate.context_revision_id:
                return AgentEmissionAcceptance(
                    accepted=False,
                    reason_code="context_revision_stale",
                )

            rows = [
                item
                for item in self._cycle_rows(candidate.cycle_id)
                if item.generation == candidate.generation
                and item.kind == "intermediate"
                and item.visibility == "user"
            ]
            if len(rows) >= max_messages:
                return AgentEmissionAcceptance(
                    accepted=False,
                    reason_code="max_per_cycle",
                )
            if rows and min_interval_seconds > 0:
                latest = max(item.created_at for item in rows)
                if (
                    candidate.created_at - latest
                ).total_seconds() < min_interval_seconds:
                    return AgentEmissionAcceptance(
                        accepted=False,
                        reason_code="rate_limited",
                    )

            existing_path = self.layout.emission(
                candidate.cycle_id,
                candidate.emission_id,
            )
            if existing_path.exists():
                by_id = read_model(existing_path, AgentEmission)
                if by_id != candidate:
                    raise InputRuntimeConflictError("emission stable ID collision")
                self._restore_indexes(by_id)
                return AgentEmissionAcceptance(
                    accepted=True,
                    emission=by_id,
                    duplicate=True,
                )

            self._ensure_cycle_authority(candidate.cycle_id, candidate.session_id)
            # Record-first publication is intentional. If index publication fails,
            # exact-cycle idempotency lookup repairs it on retry.
            atomic_write_model(existing_path, candidate)
            self._index(candidate)
            return AgentEmissionAcceptance(accepted=True, emission=candidate)

    def _validate_worker_authority(
        self,
        emission: AgentEmission,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
    ) -> None:
        if emission.session_id != session_id:
            raise PermissionError("emission belongs to another session")
        if self._route_value(emission, "client_type") != client_type.strip().lower():
            raise PermissionError("emission belongs to another client type")
        if self._route_value(emission, "client_instance_id") != client_instance_id.strip():
            raise PermissionError("emission belongs to another client instance")

    async def list_ready_for_client(
        self,
        *,
        client_type: str,
        client_instance_id: str,
        limit: int,
        now: datetime,
    ) -> tuple[AgentEmission, ...]:
        if not 1 <= int(limit) <= 200:
            raise ValueError("invalid emission outbox limit")
        normalized_type = client_type.strip().lower()
        normalized_instance = client_instance_id.strip()
        if not normalized_type or not normalized_instance:
            raise ValueError("client authority must not be empty")
        rows = [
            item
            for item in self._scan_all()
            if item.state == EmissionState.READY
            and item.visibility == "user"
            and self._route_value(item, "client_type") == normalized_type
            and self._route_value(item, "client_instance_id") == normalized_instance
        ]
        rows.sort(key=lambda item: (item.created_at, item.emission_id))
        return tuple(rows[: int(limit)])

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
    ) -> AgentEmission:
        token = claim_token.strip()
        if not token or lease_seconds <= 0:
            raise ValueError("invalid delivery claim")
        stale = await self._find(emission_id)
        self._validate_worker_authority(
            stale,
            session_id=session_id,
            client_type=client_type,
            client_instance_id=client_instance_id,
        )
        async with self.locks.hold(self.root, stale.session_id):
            current = await self._find(emission_id)
            self._validate_worker_authority(
                current,
                session_id=session_id,
                client_type=client_type,
                client_instance_id=client_instance_id,
            )
            if current.state == EmissionState.DELIVERING:
                if current.delivery_claim_token != token:
                    raise InputRuntimeConflictError(
                        "emission already has another active delivery claim"
                    )
                if (
                    current.delivery_claim_expires_at is not None
                    and current.delivery_claim_expires_at <= claimed_at
                ):
                    unknown = validated_copy(
                        current,
                        state=EmissionState.UNKNOWN,
                        error_code="delivery_claim_expired",
                        delivery_claim_token=None,
                        delivery_claimed_at=None,
                        delivery_claim_expires_at=None,
                    )
                    atomic_write_model(
                        self.layout.emission(current.cycle_id, current.emission_id),
                        unknown,
                    )
                    raise InputRuntimeConflictError("delivery claim is expired")
                # Lost claim HTTP response: same token means the same durable
                # attempt, never a second transport attempt.
                return current
            if current.state != EmissionState.READY:
                raise InputRuntimeConflictError("emission is not ready")

            state = self._session_state(current.session_id)
            if (
                state is None
                or state.generation != current.generation
                or state.active_cycle_id != current.cycle_id
                or state.cycle_status in _TERMINAL_CYCLE_STATES
            ):
                cancelled = validated_copy(
                    current,
                    state=EmissionState.CANCELLED,
                    cancellation_reason_code="cycle_terminal_before_delivery",
                )
                atomic_write_model(
                    self.layout.emission(current.cycle_id, current.emission_id),
                    cancelled,
                )
                raise InputRuntimeConflictError(
                    "emission cannot start delivery after cycle terminalization"
                )

            claimed = validated_copy(
                current,
                state=EmissionState.DELIVERING,
                delivery_claim_token=token,
                delivery_claimed_at=claimed_at,
                delivery_claim_expires_at=(
                    claimed_at + timedelta(seconds=lease_seconds)
                ),
                delivery_attempt_count=current.delivery_attempt_count + 1,
            )
            atomic_write_model(
                self.layout.emission(current.cycle_id, current.emission_id),
                claimed,
            )
            return claimed

    async def claim_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        claimed_at: datetime | None = None,
        lease_seconds: int = 300,
    ) -> AgentEmission:
        emission = await self._find(emission_id)
        return await self.claim_for_client(
            emission_id,
            session_id=emission.session_id,
            client_type=self._route_value(emission, "client_type") or "",
            client_instance_id=(
                self._route_value(emission, "client_instance_id") or ""
            ),
            claim_token=claim_token,
            claimed_at=claimed_at or datetime.now(timezone.utc),
            lease_seconds=lease_seconds,
        )

    async def record_delivery_receipt(
        self,
        receipt: AgentEmissionDeliveryReceipt,
    ) -> AgentEmission:
        stale = await self._find(receipt.emission_id)
        async with self.locks.hold(self.root, stale.session_id):
            current = await self._find(receipt.emission_id)
            existing = self._read_receipt(receipt.emission_id)
            if existing is not None:
                if existing != receipt:
                    raise InputRuntimeConflictError(
                        "emission delivery receipt relation changed"
                    )
                if current.state == EmissionState.DELIVERED:
                    return current
                if (
                    current.state != EmissionState.DELIVERING
                    or current.delivery_claim_token != receipt.claim_token
                ):
                    raise InputRuntimeConflictError(
                        "durable receipt cannot repair stale delivery attempt"
                    )
                repaired = validated_copy(
                    current,
                    state=EmissionState.DELIVERED,
                    delivered_at=receipt.delivered_at,
                    error_code=None,
                    cancellation_reason_code=None,
                    delivery_claim_token=None,
                    delivery_claimed_at=None,
                    delivery_claim_expires_at=None,
                )
                atomic_write_model(
                    self.layout.emission(current.cycle_id, current.emission_id),
                    repaired,
                )
                return repaired

            if (
                current.state != EmissionState.DELIVERING
                or current.delivery_claim_token != receipt.claim_token
            ):
                raise InputRuntimeConflictError("stale emission delivery receipt")
            if (
                current.session_id != receipt.session_id
                or current.cycle_id != receipt.cycle_id
                or current.generation != receipt.generation
                or current.delivery_attempt_count != receipt.attempt_number
                or self._route_value(current, "client_type") != receipt.client_type
                or self._route_value(current, "client_instance_id")
                != receipt.client_instance_id
                or self._route_value(current, "conversation_id")
                != receipt.conversation_id
                or self._route_value(current, "thread_id") != receipt.thread_id
            ):
                raise InputRuntimeConflictError(
                    "emission receipt is outside exact delivery authority"
                )

            receipt_path = self._receipt_path(receipt.emission_id)
            reply_path = self._reply_index_path(receipt)
            if reply_path.exists():
                other = read_model(reply_path, AgentEmissionDeliveryReceipt)
                if other != receipt:
                    raise InputRuntimeConflictError(
                        "external delivery reference already belongs to another emission"
                    )
            # Receipt-first persistence is deliberate. If the process dies after
            # this write, retry of the same receipt repairs DELIVERED without a
            # second client send.
            atomic_write_model(receipt_path, receipt)
            atomic_write_model(reply_path, receipt)
            delivered = validated_copy(
                current,
                state=EmissionState.DELIVERED,
                delivered_at=receipt.delivered_at,
                error_code=None,
                cancellation_reason_code=None,
                delivery_claim_token=None,
                delivery_claimed_at=None,
                delivery_claim_expires_at=None,
            )
            atomic_write_model(
                self.layout.emission(current.cycle_id, current.emission_id),
                delivered,
            )
            return delivered

    async def complete_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        delivered_at: datetime,
    ) -> AgentEmission:
        emission = await self._find(emission_id)
        route = emission.response_route
        raise InputRuntimeConflictError(
            "IR-6 complete_delivery requires a durable external delivery receipt"
        )

    async def fail_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        state: str,
        error_code: str,
    ) -> AgentEmission:
        next_state = EmissionState(state)
        if next_state not in {EmissionState.FAILED, EmissionState.UNKNOWN}:
            raise ValueError("delivery failure state must be failed or unknown")
        stale = await self._find(emission_id)
        async with self.locks.hold(self.root, stale.session_id):
            current = await self._find(emission_id)
            if current.state == next_state and current.error_code == error_code:
                return current
            if (
                current.state != EmissionState.DELIVERING
                or current.delivery_claim_token != claim_token.strip()
            ):
                raise InputRuntimeConflictError("stale emission delivery claim")
            failed = validated_copy(
                current,
                state=next_state,
                error_code=error_code.strip() or "delivery_failed",
                delivery_claim_token=None,
                delivery_claimed_at=None,
                delivery_claim_expires_at=None,
            )
            atomic_write_model(
                self.layout.emission(current.cycle_id, current.emission_id),
                failed,
            )
            return failed

    async def recover_expired_delivery_claims(
        self,
        *,
        now: datetime,
    ) -> tuple[AgentEmission, ...]:
        # The dormant IR-2 implementation already classifies expiry as UNKNOWN,
        # never READY. Keep that conservative rule and durable evidence.
        return await super().recover_expired_delivery_claims(now=now)

    async def list_pending_delivery(self) -> tuple[AgentEmission, ...]:
        return tuple(
            item
            for item in self._scan_all()
            if item.state in {EmissionState.READY, EmissionState.DELIVERING}
        )

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
    ) -> tuple[AgentEmission, ...]:
        changed: list[AgentEmission] = []
        async with self.locks.hold(self.root, session_id):
            for stale in self._scan_all():
                if stale.session_id != session_id or stale.generation != generation:
                    continue
                current = await self._find(stale.emission_id)
                if current.state == EmissionState.READY:
                    updated = validated_copy(
                        current,
                        state=EmissionState.CANCELLED,
                        cancellation_reason_code=reason_code,
                    )
                elif current.state == EmissionState.DELIVERING:
                    # Reset cannot prove that an already-started transport attempt
                    # missed the client. Preserve ambiguity and fence its token.
                    updated = validated_copy(
                        current,
                        state=EmissionState.UNKNOWN,
                        error_code="reset_during_delivery",
                        delivery_claim_token=None,
                        delivery_claimed_at=None,
                        delivery_claim_expires_at=None,
                    )
                else:
                    continue
                atomic_write_model(
                    self.layout.emission(current.cycle_id, current.emission_id),
                    updated,
                )
                changed.append(updated)
        return tuple(changed)

    async def resolve_delivered_reply(
        self,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
        conversation_id: str,
        thread_id: str | None,
        external_message_id: str,
    ) -> AgentEmission | None:
        scope = self._reply_scope(
            client_type=client_type,
            client_instance_id=client_instance_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            external_message_id=external_message_id,
        )
        index_path = (
            self.layout.root
            / "indexes"
            / "emission-reply"
            / f"{storage_key(scope)}.json"
        )
        receipt = None
        if index_path.exists():
            receipt = read_model(index_path, AgentEmissionDeliveryReceipt)
        else:
            directory = self.layout.root / "emission-receipts"
            if directory.exists():
                matches = []
                for path in sorted(directory.glob("*.json")):
                    candidate = read_model(path, AgentEmissionDeliveryReceipt)
                    if self._reply_scope(
                        client_type=candidate.client_type,
                        client_instance_id=candidate.client_instance_id,
                        conversation_id=candidate.conversation_id,
                        thread_id=candidate.thread_id,
                        external_message_id=candidate.external_message_id,
                    ) == scope:
                        matches.append(candidate)
                if len(matches) > 1:
                    raise InputRuntimeConflictError(
                        "ambiguous external emission delivery reference"
                    )
                if matches:
                    receipt = matches[0]
                    atomic_write_model(index_path, receipt)
        if receipt is None or receipt.session_id != session_id:
            return None
        try:
            emission = await self._find(receipt.emission_id)
        except InputRuntimeNotFoundError:
            return None
        if emission.state != EmissionState.DELIVERED:
            return None
        if (
            emission.session_id != session_id
            or self._route_value(emission, "client_type") != client_type.strip().lower()
            or self._route_value(emission, "client_instance_id")
            != client_instance_id.strip()
            or self._route_value(emission, "conversation_id")
            != conversation_id.strip()
            or self._route_value(emission, "thread_id") != (thread_id or None)
        ):
            return None
        return emission
