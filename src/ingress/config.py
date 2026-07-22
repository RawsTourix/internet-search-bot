"""Configuration for durable client ingress and atomic input batches."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IngressConfigType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_events_per_batch: int = Field(default=64, ge=1)
    max_attachments_per_batch: int = Field(default=32, ge=0)
    max_batch_total_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    max_text_parts_per_batch: int = Field(default=64, ge=0)
    max_text_part_chars: int = Field(default=100_000, ge=1)

    @model_validator(mode="after")
    def validate_part_limits(self) -> "IngressConfigType":
        if self.max_attachments_per_batch == 0 and self.max_text_parts_per_batch == 0:
            raise ValueError("ingress must allow text parts or attachments")
        return self
