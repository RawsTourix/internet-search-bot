"""Typed, bounded localization contracts shared by client adapters."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MESSAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,191}$")
LocalizationSeverity = Literal["debug", "info", "success", "warning", "error"]
LocalizationVisibility = Literal["debug", "internal", "user"]


class _LocalizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalizationMessage(_LocalizationModel):
    message_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    severity: LocalizationSeverity = "info"
    visibility: LocalizationVisibility = "user"
    namespace: str | None = None

    @field_validator("message_key")
    @classmethod
    def validate_message_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _MESSAGE_KEY_RE.fullmatch(normalized):
            raise ValueError("invalid localization message_key")
        return normalized


class LocalizationEntry(_LocalizationModel):
    text: str | None = None
    plural_arg: str | None = None
    forms: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_representation(self) -> "LocalizationEntry":
        if self.text is None and not self.forms:
            raise ValueError("localization entry requires text or plural forms")
        if self.forms and not self.plural_arg:
            raise ValueError("plural forms require plural_arg")
        if self.text is not None and not self.text:
            raise ValueError("localization text must not be empty")
        if any(not value for value in self.forms.values()):
            raise ValueError("localization plural form must not be empty")
        return self


class LocalizationCatalog(_LocalizationModel):
    locale: str
    entries: dict[str, LocalizationEntry]

    @field_validator("locale")
    @classmethod
    def normalize_locale(cls, value: str) -> str:
        normalized = value.strip().lower().replace("_", "-").split("-", 1)[0]
        if not normalized:
            raise ValueError("catalog locale must not be empty")
        return normalized

    @field_validator("entries")
    @classmethod
    def validate_keys(
        cls, values: dict[str, LocalizationEntry]
    ) -> dict[str, LocalizationEntry]:
        result: dict[str, LocalizationEntry] = {}
        for key, entry in values.items():
            normalized = key.strip().lower()
            if not _MESSAGE_KEY_RE.fullmatch(normalized):
                raise ValueError("invalid localization catalog key")
            if normalized in result:
                raise ValueError("duplicate localization catalog key")
            result[normalized] = entry
        return result
