"""Configuration for semantic client interaction and output delivery."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import InteractionValidationError


class ClientCapabilitiesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: int = Field(default=1, ge=1)
    reject_unknown_features: bool = True
    max_feature_count: int = Field(default=64, ge=1)
    max_limit_count: int = Field(default=64, ge=1)


class LocalizationConfigType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_locale: str = "ru"
    supported_locales: tuple[str, ...] = ("ru", "en")
    fallback_locale: str = "ru"
    fail_on_missing_required_key: bool = False
    max_params: int = Field(default=32, ge=1)
    max_param_chars: int = Field(default=2_000, ge=1)

    @model_validator(mode="after")
    def validate_locales(self) -> "LocalizationConfigType":
        normalized = tuple(
            dict.fromkeys(item.strip().lower() for item in self.supported_locales)
        )
        if not normalized or any(not item for item in normalized):
            raise ValueError("supported_locales must not be empty")
        default = self.default_locale.strip().lower()
        fallback = self.fallback_locale.strip().lower()
        if default not in normalized or fallback not in normalized:
            raise ValueError("default and fallback locales must be supported")
        object.__setattr__(self, "supported_locales", normalized)
        object.__setattr__(self, "default_locale", default)
        object.__setattr__(self, "fallback_locale", fallback)
        return self


class InputPresentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    reservation_timeout_seconds: int = Field(default=30, ge=1)
    update_throttle_seconds: float = Field(default=1.0, ge=0)
    max_updates_per_batch: int = Field(default=64, ge=1)


class OutputRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_parts_per_batch: int = Field(default=64, ge=1)
    max_total_artifacts: int = Field(default=32, ge=0)
    max_delivery_groups: int = Field(default=16, ge=1)
    max_text_chars: int = Field(default=200_000, ge=1)
    max_metadata_bytes: int = Field(default=64 * 1024, ge=1)
    delivery_claim_timeout_seconds: int = Field(default=900, ge=1)

    @model_validator(mode="after")
    def validate_artifact_limit(self) -> "OutputRuntimeConfig":
        if self.max_total_artifacts > self.max_parts_per_batch:
            raise ValueError("max_total_artifacts must not exceed max_parts_per_batch")
        return self


class TelegramOutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefer_document_groups: bool = True
    status_message_editing: bool = True


class InteractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_capabilities: ClientCapabilitiesConfig = Field(
        default_factory=ClientCapabilitiesConfig
    )
    localization: LocalizationConfigType = Field(
        default_factory=LocalizationConfigType
    )
    input_presentation: InputPresentationConfig = Field(
        default_factory=InputPresentationConfig
    )
    output_runtime: OutputRuntimeConfig = Field(
        default_factory=OutputRuntimeConfig
    )
    telegram_output: TelegramOutputConfig = Field(
        default_factory=TelegramOutputConfig
    )


def load_interaction_config(config_path: str) -> InteractionConfig:
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InteractionValidationError(
            "Failed to read interaction configuration"
        ) from error
    if not isinstance(payload, dict):
        raise InteractionValidationError("Configuration root must be an object")
    projection = {
        "client_capabilities": payload.get("client_capabilities", {}),
        "localization": payload.get("localization", {}),
        "input_presentation": payload.get("input_presentation", {}),
        "output_runtime": payload.get("output_runtime", {}),
        "telegram_output": payload.get("telegram_output", {}),
    }
    try:
        return InteractionConfig.model_validate(projection)
    except ValidationError as error:
        raise InteractionValidationError(
            "Invalid interaction configuration"
        ) from error


def safe_interaction_config_summary(config: InteractionConfig) -> dict[str, object]:
    return {
        "client_capabilities": config.client_capabilities.model_dump(mode="json"),
        "localization": config.localization.model_dump(mode="json"),
        "input_presentation": config.input_presentation.model_dump(mode="json"),
        "output_runtime": config.output_runtime.model_dump(mode="json"),
        "telegram_output": config.telegram_output.model_dump(mode="json"),
    }
