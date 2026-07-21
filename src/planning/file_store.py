"""Revisioned filesystem implementation of :class:`PlanStore`."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from ..storage.config import StorageConfigType
from .config import PlanningConfigType
from .errors import (
    PlanNotFoundError,
    PlanRevisionConflictError,
    PlanStorageError,
    PlanValidationError,
)
from .interfaces import PlanStore
from .models import (
    AgentPlan,
    PlanRef,
    PlanStoreMetadata,
    is_plan_id,
)
from .validation import validate_plan


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("wb") as output:
        output.write(data)
        output.flush()
        try:
            os.fsync(output.fileno())
        except OSError:
            pass


def _serialize_model(model: Any) -> bytes:
    try:
        payload = model.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise PlanStorageError("Failed to serialize plan data") from error


def _deserialize_model(data: bytes, model_type, *, object_name: str):
    try:
        payload = json.loads(data.decode("utf-8"))
        return model_type.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        raise PlanStorageError(f"Invalid {object_name} document") from error


class FileSystemPlanStore(PlanStore):
    """Exact plan persistence with immutable JSON revisions."""

    def __init__(
        self,
        *,
        storage_config: StorageConfigType,
        planning_config: PlanningConfigType,
    ) -> None:
        configured_root = Path(storage_config.root_dir).expanduser()
        if not configured_root.is_absolute():
            configured_root = Path.cwd() / configured_root
        self.root = configured_root.resolve(strict=False) / "plans"
        self.atomic_writes = storage_config.atomic_writes
        self.planning_config = planning_config
        self._locks: dict[str, asyncio.Lock] = {}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PlanStorageError("Failed to initialize plan storage") from error

    async def create_plan(self, plan: AgentPlan) -> AgentPlan:
        validate_plan(plan, self.planning_config)
        return await asyncio.to_thread(self._create_plan_sync, plan)

    def _create_plan_sync(self, plan: AgentPlan) -> AgentPlan:
        plan_dir = self._plan_dir(plan.plan_id)
        if plan_dir.exists() or plan_dir.is_symlink():
            raise PlanValidationError(
                "plan_already_exists",
                "Plan identifier already exists.",
                retryable=False,
            )

        metadata = PlanStoreMetadata(
            plan_id=plan.plan_id,
            session_id=plan.session_id,
            cycle_id=plan.cycle_id,
            current_revision=plan.revision,
            status=plan.status,
            updated_at=plan.updated_at,
        )
        temporary_dir: Path | None = None
        try:
            if self.atomic_writes:
                temporary_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".tmp-{plan.plan_id}-",
                        dir=self.root,
                    )
                )
                target_dir = temporary_dir
            else:
                plan_dir.mkdir(parents=False, exist_ok=False)
                target_dir = plan_dir

            revisions_dir = target_dir / "revisions"
            revisions_dir.mkdir(parents=False, exist_ok=False)
            _write_bytes(
                revisions_dir / self._revision_filename(plan.revision),
                _serialize_model(plan),
            )
            _write_bytes(target_dir / "metadata.json", _serialize_model(metadata))

            if self.atomic_writes:
                os.replace(temporary_dir, plan_dir)
                temporary_dir = None
                _fsync_directory(self.root)
        except (PlanValidationError, PlanStorageError):
            raise
        except (OSError, ValueError) as error:
            raise PlanStorageError(f"Failed to create plan {plan.plan_id}") from error
        finally:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)

        return plan.model_copy(deep=True)

    async def get_plan(
        self,
        plan_id: str,
        *,
        revision: int | None = None,
    ) -> AgentPlan:
        return await asyncio.to_thread(self._get_plan_sync, plan_id, revision)

    def _get_plan_sync(
        self,
        plan_id: str,
        revision: int | None,
    ) -> AgentPlan:
        metadata = self._load_metadata(plan_id)
        selected_revision = revision or metadata.current_revision
        if selected_revision < 1:
            raise PlanValidationError(
                "invalid_revision",
                "Plan revision must be positive.",
            )
        if selected_revision > metadata.current_revision:
            raise PlanNotFoundError(
                f"Plan {plan_id} revision {selected_revision} is not committed"
            )
        revision_path = self._revisions_dir(plan_id) / self._revision_filename(
            selected_revision
        )
        self._require_regular_file(revision_path, plan_id, "revision")
        try:
            data = revision_path.read_bytes()
        except OSError as error:
            raise PlanStorageError(
                f"Failed to read plan {plan_id} revision {selected_revision}"
            ) from error
        plan = _deserialize_model(data, AgentPlan, object_name="plan revision")
        if plan.plan_id != plan_id or plan.revision != selected_revision:
            raise PlanStorageError(f"Plan revision identity mismatch for {plan_id}")
        validate_plan(plan, self.planning_config)
        return plan

    async def save_revision(
        self,
        plan: AgentPlan,
        *,
        expected_revision: int,
    ) -> AgentPlan:
        lock = self._locks.setdefault(plan.plan_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(
                self._save_revision_sync,
                plan,
                expected_revision,
            )

    def _save_revision_sync(
        self,
        plan: AgentPlan,
        expected_revision: int,
    ) -> AgentPlan:
        validate_plan(plan, self.planning_config)
        metadata = self._load_metadata(plan.plan_id)
        if metadata.current_revision != expected_revision:
            raise PlanRevisionConflictError(
                plan.plan_id,
                expected_revision=expected_revision,
                current_revision=metadata.current_revision,
            )
        if plan.revision != expected_revision + 1:
            raise PlanValidationError(
                "invalid_candidate_revision",
                "Candidate revision must equal expected_revision + 1.",
                retryable=False,
            )
        if plan.session_id != metadata.session_id or plan.cycle_id != metadata.cycle_id:
            raise PlanValidationError(
                "plan_identity_changed",
                "Plan session/cycle identity cannot change.",
                retryable=False,
            )

        revisions_dir = self._revisions_dir(plan.plan_id)
        revision_path = revisions_dir / self._revision_filename(plan.revision)
        temporary_revision = revisions_dir / (
            f".{self._revision_filename(plan.revision)}.tmp-{uuid4().hex}"
        )
        metadata_path = self._plan_dir(plan.plan_id) / "metadata.json"
        temporary_metadata = self._plan_dir(plan.plan_id) / (
            f"metadata.json.tmp-{uuid4().hex}"
        )
        next_metadata = PlanStoreMetadata(
            plan_id=plan.plan_id,
            session_id=plan.session_id,
            cycle_id=plan.cycle_id,
            current_revision=plan.revision,
            status=plan.status,
            updated_at=plan.updated_at,
        )

        try:
            if self.atomic_writes:
                _write_bytes(temporary_revision, _serialize_model(plan))
                os.replace(temporary_revision, revision_path)
                _fsync_directory(revisions_dir)
                _write_bytes(temporary_metadata, _serialize_model(next_metadata))
                os.replace(temporary_metadata, metadata_path)
                _fsync_directory(self._plan_dir(plan.plan_id))
            else:
                _write_bytes(revision_path, _serialize_model(plan))
                _write_bytes(metadata_path, _serialize_model(next_metadata))
        except (PlanStorageError, PlanValidationError):
            raise
        except OSError as error:
            raise PlanStorageError(
                f"Failed to save plan {plan.plan_id} revision {plan.revision}"
            ) from error
        finally:
            for path in (temporary_revision, temporary_metadata):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

        return plan.model_copy(deep=True)

    async def list_cycle_plans(self, cycle_id: str) -> list[PlanRef]:
        if not cycle_id.strip():
            raise PlanValidationError(
                "invalid_cycle_id",
                "cycle_id must not be empty.",
            )
        return await asyncio.to_thread(self._list_cycle_plans_sync, cycle_id)

    def _list_cycle_plans_sync(self, cycle_id: str) -> list[PlanRef]:
        result: list[PlanRef] = []
        try:
            entries = list(self.root.iterdir())
        except OSError as error:
            raise PlanStorageError("Failed to list plan storage") from error

        for entry in entries:
            if not entry.is_dir() or entry.is_symlink() or not is_plan_id(entry.name):
                continue
            try:
                metadata = self._load_metadata(entry.name)
            except PlanStorageError:
                raise
            except PlanNotFoundError:
                continue
            if metadata.cycle_id != cycle_id:
                continue
            plan = self._get_plan_sync(entry.name, metadata.current_revision)
            result.append(
                PlanRef(
                    plan_id=plan.plan_id,
                    cycle_id=plan.cycle_id,
                    goal=plan.goal,
                    status=plan.status,
                    revision=plan.revision,
                    node_count=len(plan.nodes),
                    updated_at=plan.updated_at,
                )
            )
        result.sort(key=lambda item: (item.updated_at, item.plan_id))
        return result

    def _load_metadata(self, plan_id: str) -> PlanStoreMetadata:
        plan_dir = self._require_plan_dir(plan_id)
        metadata_path = plan_dir / "metadata.json"
        self._require_regular_file(metadata_path, plan_id, "metadata")
        try:
            data = metadata_path.read_bytes()
        except OSError as error:
            raise PlanStorageError(f"Failed to read plan {plan_id} metadata") from error
        metadata = _deserialize_model(
            data,
            PlanStoreMetadata,
            object_name="plan metadata",
        )
        if metadata.plan_id != plan_id:
            raise PlanStorageError(f"Plan metadata identity mismatch for {plan_id}")
        return metadata

    def _plan_dir(self, plan_id: str) -> Path:
        if not is_plan_id(plan_id):
            raise PlanValidationError(
                "invalid_plan_id",
                "Invalid plan identifier.",
                retryable=False,
            )
        return self.root / plan_id

    def _require_plan_dir(self, plan_id: str) -> Path:
        plan_dir = self._plan_dir(plan_id)
        if not plan_dir.exists() and not plan_dir.is_symlink():
            raise PlanNotFoundError(f"Unknown plan {plan_id}")
        try:
            mode = plan_dir.lstat().st_mode
        except OSError as error:
            raise PlanStorageError(f"Failed to inspect plan {plan_id}") from error
        if plan_dir.is_symlink() or not stat.S_ISDIR(mode):
            raise PlanStorageError(f"Invalid plan directory for {plan_id}")
        return plan_dir

    def _revisions_dir(self, plan_id: str) -> Path:
        revisions_dir = self._require_plan_dir(plan_id) / "revisions"
        try:
            mode = revisions_dir.lstat().st_mode
        except FileNotFoundError as error:
            raise PlanStorageError(f"Missing revisions directory for {plan_id}") from error
        except OSError as error:
            raise PlanStorageError(f"Failed to inspect revisions for {plan_id}") from error
        if revisions_dir.is_symlink() or not stat.S_ISDIR(mode):
            raise PlanStorageError(f"Invalid revisions directory for {plan_id}")
        return revisions_dir

    @staticmethod
    def _require_regular_file(path: Path, plan_id: str, role: str) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as error:
            raise PlanNotFoundError(f"Missing {role} for plan {plan_id}") from error
        except OSError as error:
            raise PlanStorageError(f"Failed to inspect {role} for plan {plan_id}") from error
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise PlanStorageError(f"Invalid {role} for plan {plan_id}")

    @staticmethod
    def _revision_filename(revision: int) -> str:
        return f"{revision:06d}.json"
