from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from src.ingress.committed_sequence_compat import SchemaAwareCommittedSequenceMixin
from src.ingress.recovery import FileSystemCommittedInputBatchRecoveryReader
from src.ingress.store import FileSystemInputBatchStore
from src.input_runtime import (
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import InputRuntimeReadinessGate, InputRuntimeRecoveryError
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType


BASE = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
SESSION = "telegram:conversation:1062062174"


class CompatStore(SchemaAwareCommittedSequenceMixin, FileSystemInputBatchStore):
    pass


def _batch_id(index: int) -> str:
    return f"ibat_{index:032x}"


def _event_id(index: int) -> str:
    return f"evt_{index:032x}"


def _current_payload(
    *,
    index: int,
    session_id: str,
    sequence_number: int,
    committed_at: datetime,
) -> dict:
    return {
        "schema_version": 2,
        "input_batch_id": _batch_id(index),
        "session_id": session_id,
        "client_type": "telegram",
        "sequence_number": sequence_number,
        "source_event_ids": [_event_id(index)],
        "text_parts": [],
        "semantic_parts": [],
        "artifact_refs": [],
        "referenced_artifact_refs": [],
        "admission_mode": "auto",
        "response_route": {
            "route_type": "telegram",
            "conversation_id": session_id,
            "thread_id": None,
            "reply_to_message_id": None,
            "metadata": {},
        },
        "response_anchor": None,
        "reply_contexts": [],
        "locale": "ru",
        "capability_snapshot": None,
        "artifact_manifest": {
            "items": [],
            "available_count": 0,
            "truncated": False,
        },
        "continuation_of_batch_id": None,
        "correction_of_batch_id": None,
        "committed_at": committed_at.isoformat(),
        "commit_reason": "ir10-regression",
        "content_fingerprint": f"sha256:{index:064x}",
        "legacy_derived": False,
    }


def _legacy_payload(**kwargs) -> dict:
    payload = _current_payload(**kwargs)
    payload["schema_version"] = 1
    for field in (
        "semantic_parts",
        "locale",
        "capability_snapshot",
        "response_anchor",
        "reply_contexts",
        "artifact_manifest",
        "legacy_derived",
    ):
        payload.pop(field)
    return payload


def _write_committed(root: Path, payload: dict) -> Path:
    path = root / "input_batches" / payload["input_batch_id"] / "committed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _write_real_pattern(root: Path, *, session_id: str = SESSION) -> dict[str, dict]:
    original: dict[str, dict] = {}
    for sequence in range(1, 8):
        payload = _legacy_payload(
            index=sequence,
            session_id=session_id,
            sequence_number=sequence,
            committed_at=BASE + timedelta(hours=sequence),
        )
        _write_committed(root, payload)
        original[payload["input_batch_id"]] = payload
    for rank in range(1, 8):
        index = 7 + rank
        payload = _current_payload(
            index=index,
            session_id=session_id,
            sequence_number=rank,
            committed_at=BASE + timedelta(days=5, hours=rank),
        )
        _write_committed(root, payload)
        original[payload["input_batch_id"]] = payload
    return original


def _store(root: Path) -> CompatStore:
    return CompatStore(StorageConfigType(root_dir=str(root)))


def _read_raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _session_batches(store: CompatStore, session_id: str = SESSION):
    batches = []
    for path in sorted(store.root.glob("ibat_*/committed.json")):
        item = store._load_committed_path_schema_aware_sync(path)
        if item.session_id == session_id:
            batches.append(item)
    return sorted(batches, key=lambda item: item.sequence_number)


def _ready_draft(*, index: int, session_id: str, now: datetime) -> dict:
    return {
        "schema_version": 2,
        "input_batch_id": _batch_id(index),
        "session_id": session_id,
        "client_type": "telegram",
        "conversation": {"conversation_id": session_id, "thread_id": None},
        "sender": {"principal_id": "ir10", "display_name": None},
        "grouping_mode": "atomic",
        "grouping_key": f"ir10-{index}",
        "state": "ready_to_commit",
        "source_event_ids": [_event_id(index)],
        "text_parts": [],
        "attachment_parts": [],
        "semantic_parts": [],
        "locale": "ru",
        "capability_snapshot": None,
        "response_anchor": None,
        "reply_contexts": [],
        "admission_mode": "auto",
        "response_route": {
            "route_type": "telegram",
            "conversation_id": session_id,
            "thread_id": None,
            "reply_to_message_id": None,
            "metadata": {},
        },
        "opened_at": now.isoformat(),
        "last_event_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "quiet_deadline": None,
        "sealing_deadline": None,
        "maximum_deadline": None,
        "failure_code": None,
        "legacy_derived": False,
    }


async def _recover(root: Path, store: CompatStore):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(root))
    )
    reader = FileSystemCommittedInputBatchRecoveryReader(store)
    execution = SessionExecutionCoordinator()
    ids = count(1)
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=execution,
        cycle_id_factory=lambda: f"cycle-ir10-{next(ids)}",
        clock=lambda: BASE + timedelta(days=10),
        payload_size_resolver=lambda batch: len(batch.model_dump_json()),
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=repositories,
        admission_service=service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=execution,
        clock=lambda: BASE + timedelta(days=10),
    )
    return repositories, gate, await recovery.recover()


@pytest.mark.asyncio
async def test_real_v1_v2_overlap_migrates_before_recovery_without_content_loss(tmp_path):
    original = _write_real_pattern(tmp_path)
    store = _store(tmp_path)

    repositories, _, plan = await _recover(tmp_path, store)
    batches = _session_batches(store)
    assert [item.sequence_number for item in batches] == list(range(1, 15))
    assert {item.input_batch_id for item in batches} == set(original)
    assert plan.report.committed_unadmitted_admitted == 14

    admissions = await repositories.admissions.list_for_session(SESSION)
    assert len(admissions) == 14
    assert len({item.input_batch_id for item in admissions}) == 14

    for batch_id, before in original.items():
        after = _read_raw(store.root / batch_id / "committed.json")
        expected = dict(before)
        if before["schema_version"] == 2:
            expected["sequence_number"] = 7 + before["sequence_number"]
        assert after == expected


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_next_real_commit_is_sequence_15(tmp_path):
    _write_real_pattern(tmp_path)
    first = _store(tmp_path)
    await _recover(tmp_path, first)

    second = _store(tmp_path)
    assert second.reconcile_legacy_committed_sequence_overlaps_sync() == (0, 0)
    assert [item.sequence_number for item in _session_batches(second)] == list(range(1, 15))

    now = BASE + timedelta(days=10, hours=1)
    draft = _ready_draft(index=100, session_id=SESSION, now=now)
    draft_path = second.root / draft["input_batch_id"] / "draft.json"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    second._write_json(draft_path, draft)
    committed = second._commit_sync(draft["input_batch_id"], "ir10-next")
    assert committed.sequence_number == 15

    third = _store(tmp_path)
    assert [item.sequence_number for item in _session_batches(third)] == list(range(1, 16))
    assert third._next_sequence_sync(SESSION) == 16


@pytest.mark.parametrize("crash_after", [0, 1, 4, 7])
def test_partial_migration_crash_converges_without_loss(tmp_path, crash_after, monkeypatch):
    original = _write_real_pattern(tmp_path)
    store = _store(tmp_path)
    real_write = store._write_json
    writes = 0

    def crashing_write(path, payload):
        nonlocal writes
        if path.name == "committed.json" and crash_after == 0 and writes == 0:
            raise RuntimeError("fault-before-first-migrated-write")
        real_write(path, payload)
        if path.name == "committed.json":
            writes += 1
            if writes == crash_after:
                raise RuntimeError("fault-after-migrated-write")

    monkeypatch.setattr(store, "_write_json", crashing_write)
    with pytest.raises(RuntimeError, match="fault-"):
        store._ensure_legacy_committed_sequence_reconciled_sync()

    fresh = _store(tmp_path)
    fresh._ensure_legacy_committed_sequence_reconciled_sync()
    batches = _session_batches(fresh)
    assert [item.sequence_number for item in batches] == list(range(1, 15))
    assert {item.input_batch_id for item in batches} == set(original)
    assert fresh._next_sequence_sync(SESSION) == 15


@pytest.mark.asyncio
async def test_modern_modern_duplicate_remains_fatal(tmp_path):
    _write_committed(
        tmp_path,
        _current_payload(
            index=20,
            session_id=SESSION,
            sequence_number=5,
            committed_at=BASE + timedelta(days=6),
        ),
    )
    _write_committed(
        tmp_path,
        _current_payload(
            index=21,
            session_id=SESSION,
            sequence_number=5,
            committed_at=BASE + timedelta(days=6, hours=1),
        ),
    )
    store = _store(tmp_path)
    assert store.reconcile_legacy_committed_sequence_overlaps_sync() == (0, 0)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await _recover(tmp_path, store)
    assert error.value.reason_code == "committed_batch_order_conflict"


@pytest.mark.asyncio
async def test_ambiguous_legacy_current_overlap_remains_fatal(tmp_path):
    _write_committed(
        tmp_path,
        _legacy_payload(
            index=30,
            session_id=SESSION,
            sequence_number=1,
            committed_at=BASE,
        ),
    )
    _write_committed(
        tmp_path,
        _legacy_payload(
            index=31,
            session_id=SESSION,
            sequence_number=2,
            committed_at=BASE + timedelta(hours=1),
        ),
    )
    _write_committed(
        tmp_path,
        _current_payload(
            index=32,
            session_id=SESSION,
            sequence_number=1,
            committed_at=BASE + timedelta(days=5),
        ),
    )
    _write_committed(
        tmp_path,
        _current_payload(
            index=33,
            session_id=SESSION,
            sequence_number=3,
            committed_at=BASE + timedelta(days=5, hours=1),
        ),
    )
    store = _store(tmp_path)
    assert store.reconcile_legacy_committed_sequence_overlaps_sync() == (0, 0)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await _recover(tmp_path, store)
    assert error.value.reason_code == "committed_batch_order_conflict"


def test_migration_is_exact_session_scoped_and_leaves_other_session_bytes_unchanged(tmp_path):
    _write_real_pattern(tmp_path)
    other = "web:conversation:other"
    other_paths = []
    for sequence in range(1, 4):
        other_paths.append(
            _write_committed(
                tmp_path,
                _current_payload(
                    index=40 + sequence,
                    session_id=other,
                    sequence_number=sequence,
                    committed_at=BASE + timedelta(days=7, hours=sequence),
                ),
            )
        )
    before = {path: path.read_bytes() for path in other_paths}

    store = _store(tmp_path)
    assert store.reconcile_legacy_committed_sequence_overlaps_sync() == (1, 7)
    assert [item.sequence_number for item in _session_batches(store)] == list(range(1, 15))
    assert {path: path.read_bytes() for path in other_paths} == before
    assert [item.sequence_number for item in _session_batches(store, other)] == [1, 2, 3]
