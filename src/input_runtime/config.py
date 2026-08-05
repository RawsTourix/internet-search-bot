"""Validated configuration for input runtime contracts."""
from __future__ import annotations
import json, math
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from .errors import InputRuntimeConfigValidationError

class InputRuntimeConfigType(BaseModel):
    model_config=ConfigDict(extra="forbid")
    enabled:bool=True
    max_queued_batches_per_session:int=Field(default=64,ge=1)
    max_queued_bytes_per_session:int=Field(default=268435456,ge=1)
    max_batches_per_checkpoint:int=Field(default=8,ge=1)
    max_batch_bytes_per_checkpoint:int=Field(default=67108864,ge=1)
    claim_lease_seconds:float=Field(default=300,gt=0)
    max_intermediate_messages_per_cycle:int=Field(default=16,ge=1)
    min_intermediate_message_interval_seconds:float=Field(default=15.0,gt=0)
    max_intermediate_message_chars:int=Field(default=3500,ge=1)
    @field_validator("claim_lease_seconds","min_intermediate_message_interval_seconds")
    @classmethod
    def finite(cls,v):
        if not math.isfinite(v) or v<=0: raise ValueError("time values must be finite and positive")
        return v
    @model_validator(mode="after")
    def limits(self):
        if self.max_batches_per_checkpoint>self.max_queued_batches_per_session: raise ValueError("max_batches_per_checkpoint must not exceed queue limit")
        if self.max_batch_bytes_per_checkpoint>self.max_queued_bytes_per_session: raise ValueError("max_batch_bytes_per_checkpoint must not exceed queue byte limit")
        return self

def load_input_runtime_config(config_path:str)->InputRuntimeConfigType:
    try: payload=json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError,UnicodeDecodeError) as error: raise InputRuntimeConfigValidationError("Failed to read input runtime configuration",code="input_runtime_config_read_failed") from error
    if not isinstance(payload,dict): raise InputRuntimeConfigValidationError("Configuration root must be an object",code="input_runtime_config_root_invalid")
    section=payload.get("input_runtime",{})
    if section is None: section={}
    if not isinstance(section,dict): raise InputRuntimeConfigValidationError("Input runtime configuration must be an object",code="input_runtime_config_section_invalid")
    try: return InputRuntimeConfigType.model_validate(section)
    except ValidationError as error: raise InputRuntimeConfigValidationError("Invalid input runtime configuration",code="input_runtime_config_invalid") from error
