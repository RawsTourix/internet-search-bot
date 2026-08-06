"""Crash recovery helpers for global filesystem identity indexes."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, TypeVar

from pydantic import BaseModel

from ._filesystem_common import _RepositoryBase
from .errors import InputRuntimeConflictError
from .models import (
    ActiveCycleSnapshot,
    AgentEmission,
    CycleContextRevision,
    CycleFinalizationRecord,
    CycleInboxItem,
    InputAdmissionRecord,
    SessionControlCommand,
)
from .serialization import read_model


ModelT = TypeVar("ModelT", bound=BaseModel)


def clear_index(path: Path, *, identity_name: str) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise InputRuntimeConflictError(
            f"cannot clear dangling {identity_name} index"
        ) from error


def scan_models(
    paths: Iterable[Path],
    model_type: type[ModelT],
    *,
    identity_name: str,
) -> tuple[ModelT, ...]:
    try:
        return tuple(read_model(path, model_type) for path in sorted(paths))
    except Exception as error:
        raise InputRuntimeConflictError(
            f"cannot reconcile authoritative {identity_name} records"
        ) from error


def recover_indexed(
    repository: _RepositoryBase,
    index_path: Path,
    model_type: type[ModelT],
    *,
    identity_name: str,
    matches_identity: Callable[[ModelT], bool],
    scan: Callable[[], Iterable[ModelT]],
    restore: Callable[[ModelT], None],
) -> ModelT | None:
    """Resolve an index, rebuilding or clearing it from durable authority."""
    pointer = repository._read_pointer(index_path)
    if pointer is not None:
        record_path = repository._pointer_record_path(pointer)
        if record_path.exists():
            try:
                indexed = read_model(record_path, model_type)
            except Exception as error:
                raise InputRuntimeConflictError(
                    f"invalid indexed {identity_name} record"
                ) from error
            if matches_identity(indexed):
                return indexed

    authoritative = [item for item in scan() if matches_identity(item)]
    if len(authoritative) > 1:
        raise InputRuntimeConflictError(
            f"ambiguous authoritative {identity_name} identity"
        )
    if authoritative:
        restore(authoritative[0])
        return authoritative[0]

    if index_path.exists():
        clear_index(index_path, identity_name=identity_name)
    return None


def _cycle_local_sessions(
    repository: _RepositoryBase,
    cycle_id: str,
) -> set[str]:
    sessions: set[str] = set()
    snapshot_path = repository.layout.snapshot(cycle_id)
    if snapshot_path.exists():
        record = scan_models(
            (snapshot_path,),
            ActiveCycleSnapshot,
            identity_name="cycle authority",
        )[0]
        if record.cycle_id != cycle_id:
            raise InputRuntimeConflictError("cycle snapshot identity mismatch")
        sessions.add(record.session_id)

    collections = (
        (repository.layout.inbox(cycle_id).glob("*.json"), CycleInboxItem),
        (
            repository.layout.revisions(cycle_id).glob("*.json"),
            CycleContextRevision,
        ),
        (repository.layout.emissions(cycle_id).glob("*.json"), AgentEmission),
        (
            repository.layout.finalizations(cycle_id).glob("*.json"),
            CycleFinalizationRecord,
        ),
    )
    for paths, model_type in collections:
        for record in scan_models(
            paths,
            model_type,
            identity_name="cycle authority",
        ):
            if record.cycle_id != cycle_id:
                raise InputRuntimeConflictError("cycle record identity mismatch")
            sessions.add(record.session_id)
    return sessions


def _all_cycle_sessions(
    repository: _RepositoryBase,
    cycle_id: str,
) -> set[str]:
    sessions = _cycle_local_sessions(repository, cycle_id)
    for record in scan_models(
        repository.layout.root.glob("sessions/*/admissions/*.json"),
        InputAdmissionRecord,
        identity_name="cycle authority",
    ):
        if record.target_cycle_id == cycle_id:
            sessions.add(record.session_id)

    for record in scan_models(
        repository.layout.root.glob("sessions/*/controls/*.json"),
        SessionControlCommand,
        identity_name="cycle authority",
    ):
        if record.target_cycle_id == cycle_id:
            sessions.add(record.session_id)
    return sessions


def _write_cycle_authority(
    repository: _RepositoryBase,
    cycle_id: str,
    session_id: str,
) -> None:
    repository._write_pointer(
        repository.layout.cycle_authority(cycle_id),
        repository._pointer(
            "cycle",
            cycle_id,
            session_id,
            repository.layout.cycle_dir(cycle_id),
            cycle_id,
        ),
    )


def recover_cycle_authority(
    repository: _RepositoryBase,
    cycle_id: str,
    session_id: str,
) -> None:
    """Validate/rebuild cycle ownership without reserving an empty cycle."""
    path = repository.layout.cycle_authority(cycle_id)
    pointer = repository._read_pointer(path)

    local_sessions = _cycle_local_sessions(repository, cycle_id)
    if len(local_sessions) > 1:
        raise InputRuntimeConflictError(
            "ambiguous authoritative cycle ownership"
        )
    if local_sessions:
        authoritative = next(iter(local_sessions))
        if pointer is None or pointer.session_id != authoritative:
            _write_cycle_authority(repository, cycle_id, authoritative)
        if authoritative != session_id:
            raise InputRuntimeConflictError("cycle belongs to another session")
        return

    if pointer is not None and pointer.session_id == session_id:
        return

    authoritative_sessions = _all_cycle_sessions(repository, cycle_id)
    if len(authoritative_sessions) > 1:
        raise InputRuntimeConflictError(
            "ambiguous authoritative cycle ownership"
        )
    if authoritative_sessions:
        authoritative = next(iter(authoritative_sessions))
        _write_cycle_authority(repository, cycle_id, authoritative)
        if authoritative != session_id:
            raise InputRuntimeConflictError("cycle belongs to another session")
        return

    if path.exists():
        clear_index(path, identity_name="cycle authority")
