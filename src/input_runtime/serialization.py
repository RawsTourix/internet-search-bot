"""Filesystem serialization primitives for the input runtime."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .errors import InputRuntimeError, InputRuntimeNotFoundError

ModelT = TypeVar("ModelT", bound=BaseModel)


def storage_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("storage key source must not be empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def read_model(path: Path, model_type: type[ModelT]) -> ModelT:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InputRuntimeNotFoundError(str(path)) from error
    except (OSError, json.JSONDecodeError) as error:
        raise InputRuntimeError(f"Cannot read durable input-runtime record {path}: {error}") from error
    try:
        return model_type.model_validate(payload)
    except Exception as error:
        raise InputRuntimeError(f"Invalid durable input-runtime record {path}: {error}") from error


def atomic_write_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = model.model_dump_json(indent=2)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as error:
        raise InputRuntimeError(f"Cannot persist durable input-runtime record {path}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def list_models(directory: Path, model_type: type[ModelT]) -> tuple[ModelT, ...]:
    if not directory.exists():
        return ()
    records = [read_model(path, model_type) for path in sorted(directory.glob("*.json"))]
    return tuple(records)
