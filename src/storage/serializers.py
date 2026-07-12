"""Safe JSON serialization for storage metadata."""

import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from .errors import StorageSerializationError


ModelT = TypeVar("ModelT", bound=BaseModel)
SUPPORTED_SCHEMA_VERSION = 1


def serialize_model(model: BaseModel, *, object_type: str, object_id: str) -> bytes:
    """Serialize a Pydantic model as deterministic UTF-8 JSON."""
    try:
        payload = model.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (
        TypeError,
        ValueError,
        UnicodeError,
        PydanticSerializationError,
    ) as error:
        raise StorageSerializationError(
            f"Failed to serialize {object_type} {object_id} metadata"
        ) from error


def deserialize_model(
    data: bytes,
    model_type: type[ModelT],
    *,
    object_type: str,
    object_id: str,
) -> ModelT:
    """Load and validate one supported metadata document."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise StorageSerializationError(
            f"Failed to decode {object_type} {object_id} metadata"
        ) from error

    if not isinstance(payload, dict):
        raise StorageSerializationError(
            f"Invalid object root for {object_type} {object_id} metadata"
        )
    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise StorageSerializationError(
            f"Unsupported schema version for {object_type} {object_id}"
        )

    try:
        return model_type.model_validate(payload)
    except ValidationError as error:
        raise StorageSerializationError(
            f"Invalid {object_type} {object_id} metadata"
        ) from error
