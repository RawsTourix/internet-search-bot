"""Atomic filesystem persistence for one presentation per input/client binding."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..localization.models import LocalizationMessage
from .anchors import ClientResponseAnchor
from .errors import PresentationConflictError, PresentationNotFoundError
from .ids import is_interaction_id
from .presentation import (
    InputBatchPresentationRef,
    PresentationState,
    hash_presentation_token,
    utc_now,
)


def _key(input_batch_id: str, client_binding_id: str) -> str:
    import hashlib

    raw = f"{input_batch_id}\0{client_binding_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class FileSystemInputPresentationStore:
    """Persists the reservation before any transport message is created."""

    def __init__(self, root: Path, *, atomic_writes: bool = True) -> None:
        self.root = Path(root)
        self.atomic_writes = atomic_writes
        self._lock = threading.RLock()
        self.records = self.root / "input_presentations" / "records"
        self.index = self.root / "input_presentations" / "by_binding"
        self.records.mkdir(parents=True, exist_ok=True)
        self.index.mkdir(parents=True, exist_ok=True)

    async def reserve(
        self,
        *,
        input_batch_id: str,
        client_binding_id: str,
        token: str,
        message: LocalizationMessage,
        locale: str,
        file_count: int = 0,
        text_part_count: int = 0,
        response_anchor: ClientResponseAnchor | None = None,
        now: datetime | None = None,
    ) -> tuple[InputBatchPresentationRef, bool]:
        return await asyncio.to_thread(
            self._reserve_sync,
            input_batch_id,
            client_binding_id,
            token,
            message,
            locale,
            file_count,
            text_part_count,
            response_anchor,
            now,
        )

    def _reserve_sync(
        self,
        input_batch_id: str,
        client_binding_id: str,
        token: str,
        message: LocalizationMessage,
        locale: str,
        file_count: int,
        text_part_count: int,
        response_anchor: ClientResponseAnchor | None,
        now: datetime | None,
    ) -> tuple[InputBatchPresentationRef, bool]:
        with self._lock:
            index_path = self.index / f"{_key(input_batch_id, client_binding_id)}.json"
            if index_path.exists():
                pointer = self._read(index_path)
                record = self._load(pointer["presentation_id"])
                return record, False
            record = InputBatchPresentationRef.reserve(
                input_batch_id=input_batch_id,
                client_binding_id=client_binding_id,
                token=token,
                message=message,
                locale=locale,
                file_count=file_count,
                text_part_count=text_part_count,
                response_anchor=response_anchor,
                now=now,
            )
            self._write(
                self.records / f"{record.presentation_id}.json",
                record.model_dump(mode="json"),
            )
            self._write(
                index_path,
                {
                    "input_batch_id": input_batch_id,
                    "client_binding_id": client_binding_id,
                    "presentation_id": record.presentation_id,
                },
            )
            return record, True

    async def get(self, presentation_id: str) -> InputBatchPresentationRef:
        return await asyncio.to_thread(self._load, presentation_id)

    async def verify_token(self, presentation_id: str, token: str) -> bool:
        record = await self.get(presentation_id)
        import hmac

        return hmac.compare_digest(record.token_hash, hash_presentation_token(token))

    async def bind(
        self,
        presentation_id: str,
        *,
        client_message_id: str,
        token: str,
        now: datetime | None = None,
    ) -> InputBatchPresentationRef:
        if not await self.verify_token(presentation_id, token):
            raise PresentationConflictError("presentation token does not match")
        return await self._mutate(
            presentation_id,
            state=PresentationState.BOUND,
            client_message_id=client_message_id,
            now=now,
        )

    async def update(
        self,
        presentation_id: str,
        *,
        message: LocalizationMessage,
        file_count: int,
        text_part_count: int,
        response_anchor: ClientResponseAnchor | None = None,
        now: datetime | None = None,
    ) -> InputBatchPresentationRef:
        return await self._mutate(
            presentation_id,
            message=message,
            file_count=file_count,
            text_part_count=text_part_count,
            increment_update_count=True,
            response_anchor=response_anchor,
            now=now,
        )

    async def close(
        self,
        presentation_id: str,
        *,
        state: PresentationState = PresentationState.CLOSED,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> InputBatchPresentationRef:
        if state not in {
            PresentationState.CLOSED,
            PresentationState.FAILED,
            PresentationState.EXPIRED,
        }:
            raise ValueError("close requires a terminal presentation state")
        return await self._mutate(
            presentation_id,
            state=state,
            error_code=error_code,
            closed_at=now or utc_now(),
            now=now,
        )

    async def list_recoverable(self) -> list[InputBatchPresentationRef]:
        return await asyncio.to_thread(self._list_recoverable_sync)

    async def expire_stale_reservations(
        self,
        *,
        timeout_seconds: int,
        now: datetime | None = None,
    ) -> list[InputBatchPresentationRef]:
        current_time = now or utc_now()
        recoverable = await self.list_recoverable()
        expired: list[InputBatchPresentationRef] = []
        for record in recoverable:
            if (
                record.state == PresentationState.RESERVED
                and current_time - record.updated_at
                >= timedelta(seconds=timeout_seconds)
            ):
                expired.append(
                    await self.close(
                        record.presentation_id,
                        state=PresentationState.EXPIRED,
                        error_code="reservation_timeout",
                        now=current_time,
                    )
                )
        return expired

    def _list_recoverable_sync(self) -> list[InputBatchPresentationRef]:
        result: list[InputBatchPresentationRef] = []
        for path in sorted(self.records.glob("iprs_*.json")):
            record = InputBatchPresentationRef.model_validate(self._read(path))
            if record.state in {PresentationState.RESERVED, PresentationState.BOUND}:
                result.append(record)
        return result

    async def _mutate(self, presentation_id: str, **changes) -> InputBatchPresentationRef:
        return await asyncio.to_thread(self._mutate_sync, presentation_id, changes)

    def _mutate_sync(
        self, presentation_id: str, changes: dict
    ) -> InputBatchPresentationRef:
        with self._lock:
            current = self._load(presentation_id)
            if current.state in {
                PresentationState.CLOSED,
                PresentationState.FAILED,
                PresentationState.EXPIRED,
            }:
                return current
            timestamp = changes.pop("now", None) or utc_now()
            increment_update_count = changes.pop(
                "increment_update_count",
                False,
            )
            if increment_update_count:
                changes["update_count"] = current.update_count + 1
            payload = current.model_dump()
            payload.update(changes)
            payload["updated_at"] = timestamp
            updated = InputBatchPresentationRef.model_validate(payload)
            self._write(
                self.records / f"{presentation_id}.json",
                updated.model_dump(mode="json"),
            )
            return updated

    def _load(self, presentation_id: str) -> InputBatchPresentationRef:
        if not is_interaction_id(presentation_id, prefix="iprs"):
            raise PresentationNotFoundError("invalid presentation ID")
        path = self.records / f"{presentation_id}.json"
        if not path.exists():
            raise PresentationNotFoundError("presentation does not exist")
        return InputBatchPresentationRef.model_validate(self._read(path))

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            if path.is_symlink():
                raise PresentationConflictError("symlink metadata is not allowed")
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PresentationConflictError("invalid presentation metadata") from error

    def _write(self, path: Path, payload: dict) -> None:
        data = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self.atomic_writes:
            path.write_bytes(data)
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
