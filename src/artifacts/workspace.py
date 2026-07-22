"""Isolated local workspaces for trusted executable MCP artifact processors."""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..storage.config import StorageConfigType
from ..storage.interfaces import ContentStore
from .candidate_store import ArtifactCandidateStore
from .config import ArtifactConfigType
from .errors import (
    ArtifactLimitError,
    ArtifactStorageError,
    ArtifactValidationError,
    ArtifactWorkspaceError,
)
from .format_registry import ArtifactFormatRegistry
from .models import (
    ArtifactAccessContext,
    ArtifactCandidate,
    new_artifact_candidate_id,
    is_artifact_id,
    utc_now,
)
from .service import ArtifactService


class _WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactInputBinding(_WorkspaceModel):
    artifact_id: str
    argument_pointer: str
    representation: Literal["local_file"] = "local_file"

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not is_artifact_id(value):
            raise ValueError("invalid artifact_id")
        return value

    @field_validator("argument_pointer")
    @classmethod
    def validate_pointer(cls, value: str) -> str:
        _decode_json_pointer(value)
        return value


class ArtifactOutputSpec(_WorkspaceModel):
    relative_path: str
    suggested_filename: str | None = None
    declared_mime_type: str | None = None

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return _normalize_relative_output_path(value)

    @field_validator("suggested_filename")
    @classmethod
    def normalize_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = Path(value.replace("\\", "/")).name.strip()
        if not normalized or normalized in {".", ".."}:
            raise ValueError("suggested_filename must not be empty")
        return normalized

    @field_validator("declared_mime_type")
    @classmethod
    def normalize_mime(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None


@dataclass(slots=True)
class ArtifactWorkspace:
    root: Path
    inputs_dir: Path
    outputs_dir: Path
    temp_dir: Path
    arguments: dict[str, Any]
    output_specs: tuple[ArtifactOutputSpec, ...]
    source_artifact_ids: tuple[str, ...]
    input_bytes: int


class ArtifactWorkspaceManager:
    """Materialize exact inputs and import only explicitly declared outputs."""

    def __init__(
        self,
        *,
        storage_config: StorageConfigType,
        artifact_config: ArtifactConfigType,
        artifact_service: ArtifactService,
        content_store: ContentStore,
        candidate_store: ArtifactCandidateStore,
        format_registry: ArtifactFormatRegistry,
    ) -> None:
        configured_root = Path(storage_config.root_dir).expanduser()
        if not configured_root.is_absolute():
            configured_root = Path.cwd() / configured_root
        self.root = configured_root.resolve(strict=False) / "workspaces"
        self.config = artifact_config
        self.artifact_service = artifact_service
        self.content_store = content_store
        self.candidate_store = candidate_store
        self.format_registry = format_registry
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to initialize artifact workspace storage"
            ) from error

    async def prepare(
        self,
        *,
        access: ArtifactAccessContext,
        tool_call_id: str,
        arguments: dict[str, Any],
        bindings: list[ArtifactInputBinding],
        outputs: list[ArtifactOutputSpec],
    ) -> ArtifactWorkspace:
        if not tool_call_id.strip():
            raise ArtifactValidationError(
                "invalid_tool_call_id",
                "Artifact workspace requires a tool call ID.",
                retryable=False,
            )
        if len(bindings) > self.config.max_artifacts_per_cycle:
            raise ArtifactLimitError("Too many artifact inputs for one tool call")
        if len(outputs) > self.config.max_artifacts_per_cycle:
            raise ArtifactLimitError("Too many artifact outputs for one tool call")
        if len({item.argument_pointer for item in bindings}) != len(bindings):
            raise ArtifactValidationError(
                "duplicate_artifact_argument_pointer",
                "Artifact bindings must use distinct argument pointers.",
            )
        if len({item.relative_path for item in outputs}) != len(outputs):
            raise ArtifactValidationError(
                "duplicate_artifact_output_path",
                "Artifact outputs must use distinct relative paths.",
            )

        workspace_root = Path(
            tempfile.mkdtemp(prefix="tool-", dir=self.root)
        ).resolve(strict=False)
        inputs_dir = workspace_root / "inputs"
        outputs_dir = workspace_root / "outputs"
        temp_dir = workspace_root / "temp"
        try:
            inputs_dir.mkdir()
            outputs_dir.mkdir()
            temp_dir.mkdir()
            internal_arguments = copy.deepcopy(arguments)
            source_ids: list[str] = []
            total_bytes = 0
            for index, binding in enumerate(bindings):
                artifact_ref = await self.artifact_service.get_artifact(
                    binding.artifact_id,
                    access=access,
                )
                version = await self.artifact_service.artifact_store.get_version(
                    binding.artifact_id
                )
                total_bytes += version.size_bytes
                if total_bytes > self.config.max_workspace_bytes:
                    raise ArtifactLimitError(
                        "Artifact workspace input budget would be exceeded"
                    )
                filename = (
                    f"{index:02d}-{binding.artifact_id[4:12]}-"
                    f"{artifact_ref.filename}"
                )
                destination = inputs_dir / filename
                await self._write_content_file(
                    version.content_id,
                    destination,
                    max_bytes=version.size_bytes,
                )
                try:
                    destination.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                except OSError:
                    # Read-only is best effort across supported platforms. The
                    # canonical ContentStore object remains immutable regardless.
                    pass
                _inject_json_pointer(
                    internal_arguments,
                    binding.argument_pointer,
                    str(destination),
                )
                source_ids.append(binding.artifact_id)

            manifest = {
                "schema_version": 1,
                "tool_call_id": tool_call_id,
                "source_artifact_ids": source_ids,
                "output_paths": [item.relative_path for item in outputs],
            }
            (workspace_root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            return ArtifactWorkspace(
                root=workspace_root,
                inputs_dir=inputs_dir,
                outputs_dir=outputs_dir,
                temp_dir=temp_dir,
                arguments=internal_arguments,
                output_specs=tuple(outputs),
                source_artifact_ids=tuple(source_ids),
                input_bytes=total_bytes,
            )
        except BaseException:
            await self.cleanup_path(workspace_root)
            raise

    async def collect_outputs(
        self,
        workspace: ArtifactWorkspace,
        *,
        session_id: str,
        cycle_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> list[ArtifactCandidate]:
        self._require_workspace(workspace.root)
        candidates: list[ArtifactCandidate] = []
        total_bytes = workspace.input_bytes
        for output in workspace.output_specs:
            path = self._resolve_output(workspace.outputs_dir, output.relative_path)
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ArtifactWorkspaceError(
                    "Failed to inspect declared artifact output"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ArtifactWorkspaceError(
                    "Declared artifact output must be a regular non-symlink file"
                )
            if metadata.st_size > self.config.max_artifact_size_bytes:
                raise ArtifactLimitError(
                    "Artifact tool output exceeds the per-artifact size budget"
                )
            total_bytes += metadata.st_size
            if total_bytes > self.config.max_workspace_bytes:
                raise ArtifactLimitError(
                    "Artifact workspace cumulative size budget was exceeded"
                )

            prefix = await _read_prefix(path, 4096)
            container_entries = _bounded_container_entries(
                path,
                limit=self.config.max_container_entries_inspected,
            )
            suggested_filename = output.suggested_filename or path.name
            detection = self.format_registry.detect(
                filename=suggested_filename,
                declared_mime_type=output.declared_mime_type,
                prefix=prefix,
                container_entries=container_entries,
            )
            content_ref = await self.content_store.save_stream(
                _iter_file(path),
                source_type="artifact_candidate",
                source_name=suggested_filename,
                mime_type=detection.detected_mime_type,
                cycle_id=cycle_id,
                tool_call_id=tool_call_id,
                metadata={
                    "artifact_format_id": detection.format_id,
                    "source_tool_name": tool_name,
                    "source_artifact_ids": list(workspace.source_artifact_ids),
                },
                max_size_bytes=self.config.max_artifact_size_bytes,
            )
            candidate = ArtifactCandidate(
                candidate_id=new_artifact_candidate_id(),
                session_id=session_id,
                cycle_id=cycle_id,
                content_id=content_ref.content_id,
                suggested_filename=suggested_filename,
                format_id=detection.format_id,
                mime_type=detection.detected_mime_type,
                size_bytes=content_ref.size_bytes,
                content_hash=content_ref.content_hash,
                source_tool_call_id=tool_call_id,
                source_tool_name=tool_name,
                source_artifact_ids=list(workspace.source_artifact_ids),
                created_at=utc_now(),
                metadata={
                    "detection_confidence": detection.confidence,
                    "detection_evidence": detection.evidence,
                    "declared_output_path": output.relative_path,
                },
            )
            candidates.append(await self.candidate_store.create(candidate))
        return candidates

    async def cleanup(self, workspace: ArtifactWorkspace) -> None:
        await self.cleanup_path(workspace.root)

    async def cleanup_path(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.root.resolve(strict=False))
        except ValueError as error:
            raise ArtifactWorkspaceError(
                "Refusing to clean a path outside artifact workspace storage"
            ) from error
        await _remove_tree(resolved)

    async def _write_content_file(
        self,
        content_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> None:
        written = 0
        try:
            with destination.open("xb") as stream:
                async for chunk in self.content_store.iter_content(content_id):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ArtifactWorkspaceError(
                            "Artifact payload exceeded its exact metadata size"
                        )
                    stream.write(chunk)
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
        except ArtifactWorkspaceError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise ArtifactWorkspaceError(
                "Failed to materialize artifact input"
            ) from error
        if written != max_bytes:
            destination.unlink(missing_ok=True)
            raise ArtifactWorkspaceError(
                "Artifact payload size disagrees with exact version metadata"
            )

    def _resolve_output(self, outputs_dir: Path, relative_path: str) -> Path:
        normalized = _normalize_relative_output_path(relative_path)
        resolved = (outputs_dir / PurePosixPath(normalized)).resolve(strict=False)
        try:
            resolved.relative_to(outputs_dir.resolve(strict=False))
        except ValueError as error:
            raise ArtifactWorkspaceError(
                "Declared artifact output escapes the workspace"
            ) from error
        return resolved

    def _require_workspace(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.root.resolve(strict=False))
        except ValueError as error:
            raise ArtifactWorkspaceError("Invalid artifact workspace root") from error
        if path.is_symlink() or not path.is_dir():
            raise ArtifactWorkspaceError("Artifact workspace root is unavailable")


def _decode_json_pointer(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("argument_pointer must be an absolute JSON Pointer")
    if pointer == "/":
        return [""]
    result: list[str] = []
    for raw in pointer[1:].split("/"):
        index = 0
        decoded: list[str] = []
        while index < len(raw):
            if raw[index] != "~":
                decoded.append(raw[index])
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                raise ValueError("argument_pointer contains invalid JSON Pointer escape")
            decoded.append("~" if raw[index + 1] == "0" else "/")
            index += 2
        result.append("".join(decoded))
    if len(result) > 32:
        raise ValueError("argument_pointer is too deep")
    return result


def _inject_json_pointer(document: dict[str, Any], pointer: str, value: str) -> None:
    tokens = _decode_json_pointer(pointer)
    current: Any = document
    for token in tokens[:-1]:
        if not isinstance(current, dict) or token not in current:
            raise ArtifactValidationError(
                "artifact_argument_parent_missing",
                "Artifact binding parent does not exist in tool arguments.",
            )
        current = current[token]
    if not isinstance(current, dict):
        raise ArtifactValidationError(
            "artifact_argument_parent_not_object",
            "Artifact binding parent must be an object.",
        )
    final = tokens[-1]
    if final in current:
        raise ArtifactValidationError(
            "artifact_argument_already_set",
            "Artifact binding must not overwrite an existing tool argument.",
        )
    current[final] = value


def _normalize_relative_output_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("relative_path must be a string")
    normalized = value.strip()
    if not normalized or "\\" in normalized or "\x00" in normalized:
        raise ValueError("relative_path must be a non-empty POSIX relative path")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("relative_path must stay inside outputs")
    if path.parts[0] == "outputs":
        raise ValueError("relative_path is relative to outputs and must omit that prefix")
    return path.as_posix()


async def _iter_file(path: Path, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk


async def _read_prefix(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(size)
    except OSError as error:
        raise ArtifactWorkspaceError("Failed to inspect artifact output") from error


def _bounded_container_entries(path: Path, *, limit: int) -> list[str]:
    try:
        with path.open("rb") as stream:
            signature = stream.read(4)
        if signature not in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}:
            return []
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > limit:
                raise ArtifactLimitError(
                    "Artifact container exceeds the inspection entry budget"
                )
            return names
    except ArtifactLimitError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return []


async def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ArtifactWorkspaceError("Failed to clean artifact workspace") from error
