"""Configuration for storage services."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class StorageConfigType(BaseModel):
    """Configuration shared by storage backend implementations."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["filesystem"] = "filesystem"
    root_dir: str = "storage"
    atomic_writes: bool = True
    verify_content_hash: bool = True
    max_in_memory_content_bytes: int = 64 * 1024 * 1024

    @field_validator("root_dir")
    @classmethod
    def validate_root_dir(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("root_dir must not be empty")
        return value

    @field_validator("max_in_memory_content_bytes")
    @classmethod
    def validate_memory_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_in_memory_content_bytes must be positive")
        return value
