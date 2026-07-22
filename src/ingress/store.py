"""Filesystem persistence for ingress events and atomic input batches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..artifacts.errors import ArtifactIntegrityError, ArtifactStorageError
from ..storage.config import StorageConfigType
from .models import (
    ClientIngressEvent,
    ClientInputEnvelope,
    CommittedInputBatch,
    InputAttachmentPart,
    InputAttachmentState,
    InputBatchDraft,
    InputBatchDraftState,
    InputGroupingMode,
    new_ingress_event_id,
    new_input_batch_id,
    utc_now,
)


class IngressConflictError(RuntimeError):
    """Idempotency key was reused with different semantic input."""


class IngressNotFoundError(RuntimeError):
    """Ingress event or input batch does not exist."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


class _AtomicJsonStore:
    def __init__(self, root: Path, *, atomic_writes: bool) -> None:
        self.root = root
        self.atomic_writes = atomic_writes
        self._lock = threading.RLock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactStorageError("Failed to initialize ingress storage") from error

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _canonical_json(payload)
        temporary: Path | None = None
        try:
            if self.atomic_writes:
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    dir=path.parent,
                )
                os.close(descriptor)
                temporary = Path(name)
                with temporary.open("wb") as output:
                    output.write(data)
                    output.flush()
                    try:
                        os.fsync(output.fileno())
                    except OSError:
                        pass
                os.replace(temporary, path)
                temporary = None
            else:
                path.write_bytes(data)
        except OSError as error:
            raise ArtifactStorageError("Failed to persist ingress metadata") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists() and not path.is_symlink():
            raise IngressNotFoundError(f"Unknown ingress object {path.name}")
        try:
            mode = path.lstat().st_mode
            if path.is_symlink() or not stat.S_ISREG(mode):
                raise ArtifactIntegrityError("Invalid ingress metadata file")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (IngressNotFoundError, ArtifactIntegrityError):
            raise
        except Exception as error:
            raise ArtifactStorageError("Failed to load ingress metadata") from error
        if not isinstance(payload, dict):
            raise ArtifactIntegrityError("Ingress metadata root must be an object")
        return payload


class FileSystemIngressEventStore(_AtomicJsonStore):
    """Durably deduplicate client transport events before acknowledgement."""

    def __init__(self, storage_config: StorageConfigType) -> None:
        configured = Path(storage_config.root_dir).expanduser()
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        base = configured.resolve(strict=False) / "ingress"
        super().__init__(base, atomic_writes=storage_config.atomic_writes)
        self.events_dir = self.root / "events"
        self.idempotency_dir = self.root / "idempotency"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.idempotency_dir.mkdir(parents=True, exist_ok=True)

    async def save_if_absent(
        self,
        envelope: ClientInputEnvelope,
    ) -> tuple[ClientIngressEvent, bool]:
        return await asyncio.to_thread(self._save_if_absent_sync, envelope)

    async def get(self, event_id: str) -> ClientIngressEvent:
        return await asyncio.to_thread(self._load_event_sync, event_id)

    def _save_if_absent_sync(
        self,
        envelope: ClientInputEnvelope,
    ) -> tuple[ClientIngressEvent, bool]:
        envelope_payload = envelope.model_dump(mode="json")
        semantic_fingerprint = _fingerprint(envelope_payload)
        key_digest = hashlib.sha256(
            envelope.idempotency_key.encode("utf-8")
        ).hexdigest()
        index_path = self.idempotency_dir / f"{key_digest}.json"

        with self._lock:
            if index_path.exists() or index_path.is_symlink():
                index = self._read_json(index_path)
                if index.get("fingerprint") != semantic_fingerprint:
                    raise IngressConflictError(
                        "Ingress idempotency key was reused with different content"
                    )
                return self._load_event_sync(str(index["event_id"])), True

            # Recover an event committed before an index write.
            for path in self.events_dir.glob("evt_*.json"):
                event = self._load_event_path_sync(path)
                if event.idempotency_key != envelope.idempotency_key:
                    continue
                if _fingerprint(self._envelope_projection(event)) != semantic_fingerprint:
                    raise IngressConflictError(
                        "Ingress idempotency key was reused with different content"
                    )
                self._write_json(index_path, {
                    "schema_version": 1,
                    "event_id": event.event_id,
                    "fingerprint": semantic_fingerprint,
                })
                return event, True

            event = ClientIngressEvent(
                event_id=new_ingress_event_id(),
                received_at=utc_now(),
                **envelope.model_dump(mode="python"),
            )
            self._write_json(
                self.events_dir / f"{event.event_id}.json",
                event.model_dump(mode="json"),
            )
            self._write_json(index_path, {
                "schema_version": 1,
                "event_id": event.event_id,
                "fingerprint": semantic_fingerprint,
            })
            return event, False

    def _load_event_sync(self, event_id: str) -> ClientIngressEvent:
        return self._load_event_path_sync(
            self.events_dir / f"{event_id}.json"
        )

    def _load_event_path_sync(self, path: Path) -> ClientIngressEvent:
        try:
            return ClientIngressEvent.model_validate(self._read_json(path))
        except ValidationError as error:
            raise ArtifactIntegrityError("Invalid ingress event metadata") from error

    @staticmethod
    def _envelope_projection(event: ClientIngressEvent) -> dict[str, Any]:
        payload = event.model_dump(mode="json")
        payload.pop("schema_version", None)
        payload.pop("event_id", None)
        payload.pop("received_at", None)
        return payload


class FileSystemInputBatchStore(_AtomicJsonStore):
    """Persist mutable drafts and publish one immutable committed manifest."""

    def __init__(self, storage_config: StorageConfigType) -> None:
        configured = Path(storage_config.root_dir).expanduser()
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        super().__init__(
            configured.resolve(strict=False) / "input_batches",
            atomic_writes=storage_config.atomic_writes,
        )
        self.event_index_dir = self.root / "event_index"
        self.event_index_dir.mkdir(parents=True, exist_ok=True)

    async def create_for_event(
        self,
        event: ClientIngressEvent,
        *,
        session_id: str,
        grouping_mode: InputGroupingMode,
        grouping_key: str,
    ) -> tuple[InputBatchDraft, bool]:
        return await asyncio.to_thread(
            self._create_for_event_sync,
            event,
            session_id,
            grouping_mode,
            grouping_key,
        )

    async def get_draft(self, input_batch_id: str) -> InputBatchDraft:
        return await asyncio.to_thread(self._load_draft_sync, input_batch_id)

    async def get_committed(self, input_batch_id: str) -> CommittedInputBatch:
        return await asyncio.to_thread(self._load_committed_sync, input_batch_id)

    async def find_by_event(
        self,
        event_id: str,
    ) -> tuple[InputBatchDraft | None, CommittedInputBatch | None]:
        return await asyncio.to_thread(self._find_by_event_sync, event_id)

    async def begin_ingestion(self, input_batch_id: str) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._set_state_sync,
            input_batch_id,
            InputBatchDraftState.INGESTING,
            None,
        )

    async def mark_attachment_ingesting(
        self,
        input_batch_id: str,
        slot_id: str,
    ) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._update_attachment_sync,
            input_batch_id,
            slot_id,
            {"state": InputAttachmentState.INGESTING},
        )

    async def mark_attachment_stored(
        self,
        input_batch_id: str,
        slot_id: str,
        *,
        content_id: str,
        artifact_id: str,
        detected_format_id: str,
        detected_mime_type: str,
        size_bytes: int,
        content_hash: str,
    ) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._update_attachment_sync,
            input_batch_id,
            slot_id,
            {
                "state": InputAttachmentState.STORED,
                "content_id": content_id,
                "artifact_id": artifact_id,
                "detected_format_id": detected_format_id,
                "detected_mime_type": detected_mime_type,
                "size_bytes": size_bytes,
                "content_hash": content_hash,
                "error_code": None,
            },
        )

    async def fail(
        self,
        input_batch_id: str,
        *,
        code: str,
        slot_id: str | None = None,
    ) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._fail_sync,
            input_batch_id,
            code,
            slot_id,
        )

    async def commit(
        self,
        input_batch_id: str,
        *,
        reason: str,
    ) -> CommittedInputBatch:
        return await asyncio.to_thread(
            self._commit_sync,
            input_batch_id,
            reason,
        )

    def _create_for_event_sync(
        self,
        event: ClientIngressEvent,
        session_id: str,
        grouping_mode: InputGroupingMode,
        grouping_key: str,
    ) -> tuple[InputBatchDraft, bool]:
        with self._lock:
            existing_draft, existing_committed = self._find_by_event_sync(
                event.event_id
            )
            if existing_committed is not None:
                draft = self._load_draft_sync(existing_committed.input_batch_id)
                return draft, True
            if existing_draft is not None:
                return existing_draft, True

            now = utc_now()
            batch_id = new_input_batch_id()
            draft = InputBatchDraft(
                input_batch_id=batch_id,
                session_id=session_id,
                client_type=event.client_type,
                conversation=event.conversation,
                sender=event.sender,
                grouping_mode=grouping_mode,
                grouping_key=grouping_key,
                source_event_ids=[event.event_id],
                text_parts=list(event.text_parts),
                attachment_parts=[
                    InputAttachmentPart(
                        slot_id=slot.slot_id,
                        original_filename=slot.original_filename,
                        declared_mime_type=slot.declared_mime_type,
                        declared_size_bytes=slot.declared_size_bytes,
                    )
                    for slot in event.attachment_slots
                ],
                admission_mode=event.admission_mode,
                response_route=event.response_route,
                opened_at=now,
                last_event_at=event.occurred_at,
                updated_at=now,
            )
            batch_dir = self.root / batch_id
            batch_dir.mkdir(parents=True, exist_ok=False)
            self._write_json(
                batch_dir / "draft.json",
                draft.model_dump(mode="json"),
            )
            self._write_json(
                self.event_index_dir / f"{event.event_id}.json",
                {
                    "schema_version": 1,
                    "event_id": event.event_id,
                    "input_batch_id": batch_id,
                },
            )
            return draft, False

    def _find_by_event_sync(
        self,
        event_id: str,
    ) -> tuple[InputBatchDraft | None, CommittedInputBatch | None]:
        index_path = self.event_index_dir / f"{event_id}.json"
        if not index_path.exists() and not index_path.is_symlink():
            return None, None
        index = self._read_json(index_path)
        batch_id = str(index["input_batch_id"])
        batch_dir = self.root / batch_id
        committed_path = batch_dir / "committed.json"
        if committed_path.exists() or committed_path.is_symlink():
            return self._load_draft_sync(batch_id), self._load_committed_sync(batch_id)
        return self._load_draft_sync(batch_id), None

    def _load_draft_sync(self, input_batch_id: str) -> InputBatchDraft:
        try:
            return InputBatchDraft.model_validate(
                self._read_json(self.root / input_batch_id / "draft.json")
            )
        except ValidationError as error:
            raise ArtifactIntegrityError("Invalid input batch draft") from error

    def _load_committed_sync(self, input_batch_id: str) -> CommittedInputBatch:
        try:
            return CommittedInputBatch.model_validate(
                self._read_json(self.root / input_batch_id / "committed.json")
            )
        except ValidationError as error:
            raise ArtifactIntegrityError("Invalid committed input batch") from error

    def _set_state_sync(
        self,
        input_batch_id: str,
        state: InputBatchDraftState,
        failure_code: str | None,
    ) -> InputBatchDraft:
        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            if current.state == InputBatchDraftState.COMMITTED:
                return current
            updated = current.model_copy(update={
                "state": state,
                "failure_code": failure_code,
                "updated_at": utc_now(),
            })
            updated = InputBatchDraft.model_validate(
                updated.model_dump(mode="python")
            )
            self._write_json(
                self.root / input_batch_id / "draft.json",
                updated.model_dump(mode="json"),
            )
            return updated

    def _update_attachment_sync(
        self,
        input_batch_id: str,
        slot_id: str,
        changes: dict[str, Any],
    ) -> InputBatchDraft:
        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            if current.state in {
                InputBatchDraftState.COMMITTED,
                InputBatchDraftState.CANCELLED,
                InputBatchDraftState.ABANDONED,
            }:
                raise IngressConflictError("Input batch is no longer mutable")
            found = False
            attachments: list[InputAttachmentPart] = []
            for item in current.attachment_parts:
                if item.slot_id != slot_id:
                    attachments.append(item)
                    continue
                found = True
                candidate = item.model_copy(update=changes)
                attachments.append(InputAttachmentPart.model_validate(
                    candidate.model_dump(mode="python")
                ))
            if not found:
                raise IngressNotFoundError(f"Unknown attachment slot {slot_id}")
            updated = current.model_copy(update={
                "attachment_parts": attachments,
                "updated_at": utc_now(),
            })
            updated = InputBatchDraft.model_validate(
                updated.model_dump(mode="python")
            )
            self._write_json(
                self.root / input_batch_id / "draft.json",
                updated.model_dump(mode="json"),
            )
            return updated

    def _fail_sync(
        self,
        input_batch_id: str,
        code: str,
        slot_id: str | None,
    ) -> InputBatchDraft:
        with self._lock:
            current = self._load_draft_sync(input_batch_id)
            attachments = list(current.attachment_parts)
            if slot_id is not None:
                attachments = [
                    InputAttachmentPart.model_validate(
                        item.model_copy(update={
                            "state": InputAttachmentState.FAILED,
                            "error_code": code,
                        }).model_dump(mode="python")
                    )
                    if item.slot_id == slot_id
                    else item
                    for item in attachments
                ]
            updated = current.model_copy(update={
                "state": InputBatchDraftState.FAILED,
                "failure_code": code,
                "attachment_parts": attachments,
                "updated_at": utc_now(),
            })
            updated = InputBatchDraft.model_validate(
                updated.model_dump(mode="python")
            )
            self._write_json(
                self.root / input_batch_id / "draft.json",
                updated.model_dump(mode="json"),
            )
            return updated

    def _commit_sync(
        self,
        input_batch_id: str,
        reason: str,
    ) -> CommittedInputBatch:
        with self._lock:
            committed_path = self.root / input_batch_id / "committed.json"
            if committed_path.exists() or committed_path.is_symlink():
                return self._load_committed_sync(input_batch_id)

            draft = self._load_draft_sync(input_batch_id)
            if draft.state == InputBatchDraftState.FAILED:
                raise IngressConflictError("Failed input batch cannot be committed")
            if any(
                item.state != InputAttachmentState.STORED
                for item in draft.attachment_parts
            ):
                raise IngressConflictError(
                    "All attachment slots must be stored before commit"
                )

            artifact_refs = [
                item.artifact_id
                for item in draft.attachment_parts
                if item.artifact_id is not None
            ]
            sequence_number = self._next_sequence_sync(draft.session_id)
            committed_at = utc_now()
            fingerprint_payload = {
                "session_id": draft.session_id,
                "client_type": draft.client_type.value,
                "source_event_ids": draft.source_event_ids,
                "text_parts": [
                    item.model_dump(mode="json") for item in draft.text_parts
                ],
                "artifact_refs": artifact_refs,
                "admission_mode": draft.admission_mode.value,
                "response_route": draft.response_route.model_dump(mode="json"),
            }
            committed = CommittedInputBatch(
                input_batch_id=draft.input_batch_id,
                session_id=draft.session_id,
                client_type=draft.client_type,
                sequence_number=sequence_number,
                source_event_ids=list(draft.source_event_ids),
                text_parts=list(draft.text_parts),
                artifact_refs=artifact_refs,
                referenced_artifact_refs=[],
                admission_mode=draft.admission_mode,
                response_route=draft.response_route,
                committed_at=committed_at,
                commit_reason=reason,
                content_fingerprint=_fingerprint(fingerprint_payload),
            )
            self._write_json(
                committed_path,
                committed.model_dump(mode="json"),
            )
            final_draft = draft.model_copy(update={
                "state": InputBatchDraftState.COMMITTED,
                "updated_at": committed_at,
            })
            final_draft = InputBatchDraft.model_validate(
                final_draft.model_dump(mode="python")
            )
            self._write_json(
                self.root / input_batch_id / "draft.json",
                final_draft.model_dump(mode="json"),
            )
            return committed

    def _next_sequence_sync(self, session_id: str) -> int:
        maximum = 0
        for path in self.root.glob("ibat_*/committed.json"):
            try:
                item = CommittedInputBatch.model_validate(self._read_json(path))
            except Exception:
                continue
            if item.session_id == session_id:
                maximum = max(maximum, item.sequence_number)
        return maximum + 1
