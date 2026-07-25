"""Catalog loading, validation, locale resolution and safe rendering."""

from __future__ import annotations

import json
import logging
import string
from pathlib import Path
from typing import Any, Callable

from ..interaction.config import LocalizationConfigType
from ..interaction.errors import InteractionValidationError
from .models import LocalizationCatalog, LocalizationEntry, LocalizationMessage


logger = logging.getLogger("Localization")
DiagnosticCallback = Callable[[str, dict[str, Any]], None]


class _SafeParams(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class LocalizationService:
    def __init__(
        self,
        *,
        config: LocalizationConfigType,
        catalogs: dict[str, LocalizationCatalog],
        diagnostic_callback: DiagnosticCallback | None = None,
    ) -> None:
        self.config = config
        self.catalogs = dict(catalogs)
        self.diagnostic_callback = diagnostic_callback
        self.validate_catalogs()

    @classmethod
    def from_directory(
        cls,
        *,
        config: LocalizationConfigType,
        directory: Path | None = None,
        diagnostic_callback: DiagnosticCallback | None = None,
    ) -> "LocalizationService":
        root = directory or Path(__file__).with_name("catalogs")
        catalogs = {
            locale: load_catalog(root / f"{locale}.json", locale=locale)
            for locale in config.supported_locales
        }
        return cls(
            config=config,
            catalogs=catalogs,
            diagnostic_callback=diagnostic_callback,
        )

    def normalize_locale(self, value: str | None) -> str:
        if value:
            normalized = value.strip().lower().replace("_", "-").split("-", 1)[0]
            if normalized in self.config.supported_locales:
                return normalized
        return self.config.default_locale

    def resolve_locale(
        self,
        *,
        explicit_locale: str | None = None,
        binding_locale: str | None = None,
        transport_locale: str | None = None,
    ) -> str:
        for value in (explicit_locale, binding_locale, transport_locale):
            if value:
                normalized = value.strip().lower().replace("_", "-").split("-", 1)[0]
                if normalized in self.config.supported_locales:
                    return normalized
        return self.config.default_locale

    def render(
        self,
        message: LocalizationMessage,
        *,
        locale: str | None = None,
    ) -> str:
        self._validate_params(message.params)
        requested = self.normalize_locale(locale)
        entry = self._entry(requested, message.message_key)
        selected_locale = requested
        if entry is None:
            selected_locale = self.config.fallback_locale
            entry = self._entry(selected_locale, message.message_key)
            self._diagnostic(
                "localization_fallback_used",
                {
                    "message_key": message.message_key,
                    "requested_locale": requested,
                    "fallback_locale": selected_locale,
                },
            )
        if entry is None:
            self._diagnostic(
                "localization_missing_key",
                {"message_key": message.message_key, "locale": requested},
            )
            if self.config.fail_on_missing_required_key:
                raise InteractionValidationError(
                    f"Missing required localization key: {message.message_key}"
                )
            return message.message_key

        template = self._select_template(entry, message.params, selected_locale)
        try:
            rendered = template.format_map(_SafeParams(message.params))
        except (ValueError, TypeError):
            self._diagnostic(
                "localization_format_failed",
                {"message_key": message.message_key, "locale": selected_locale},
            )
            rendered = template
        rendered = rendered.strip()
        if not rendered:
            return message.message_key
        self._diagnostic(
            "localization_rendered",
            {"message_key": message.message_key, "locale": selected_locale},
        )
        return rendered

    def validate_catalogs(self) -> None:
        missing = [
            locale
            for locale in self.config.supported_locales
            if locale not in self.catalogs
        ]
        if missing:
            raise InteractionValidationError(
                f"Missing localization catalogs: {missing}"
            )
        fallback_entries = self.catalogs[self.config.fallback_locale].entries
        expected_keys = set(fallback_entries)
        for locale, catalog in self.catalogs.items():
            if set(catalog.entries) != expected_keys:
                raise InteractionValidationError(
                    f"Localization catalog keys differ for {locale}"
                )
            for key, reference in fallback_entries.items():
                current = catalog.entries[key]
                if _entry_placeholders(current) != _entry_placeholders(reference):
                    raise InteractionValidationError(
                        f"Localization placeholders differ for {key} ({locale})"
                    )

    def _entry(self, locale: str, key: str) -> LocalizationEntry | None:
        catalog = self.catalogs.get(locale)
        return catalog.entries.get(key) if catalog is not None else None

    @staticmethod
    def _select_template(
        entry: LocalizationEntry,
        params: dict[str, Any],
        locale: str,
    ) -> str:
        if not entry.forms:
            return entry.text or ""
        raw_count = params.get(entry.plural_arg or "")
        category = _plural_category(raw_count, locale)
        return (
            entry.forms.get(category)
            or entry.forms.get("other")
            or entry.text
            or next(iter(entry.forms.values()))
        )

    def _validate_params(self, params: dict[str, Any]) -> None:
        if len(params) > self.config.max_params:
            raise InteractionValidationError("Too many localization params")
        try:
            encoded = json.dumps(params, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise InteractionValidationError(
                "Localization params must be JSON-safe"
            ) from error
        if len(encoded) > self.config.max_params * self.config.max_param_chars:
            raise InteractionValidationError("Localization params are too large")
        lowered_keys = " ".join(params).lower()
        if any(
            marker in lowered_keys
            for marker in ("token", "api_key", "password", "local_path", "bytes")
        ):
            raise InteractionValidationError(
                "Localization params contain a forbidden field"
            )

    def _diagnostic(self, event_type: str, data: dict[str, Any]) -> None:
        if self.diagnostic_callback is not None:
            self.diagnostic_callback(event_type, data)
        logger.debug("%s %s", event_type, data)


def load_catalog(path: Path, *, locale: str) -> LocalizationCatalog:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InteractionValidationError(
            f"Failed to read localization catalog {locale}"
        ) from error
    if not isinstance(raw, dict):
        raise InteractionValidationError("Localization catalog must be an object")
    entries: dict[str, LocalizationEntry] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            entries[key] = LocalizationEntry(text=value)
        elif isinstance(value, dict):
            entries[key] = LocalizationEntry.model_validate(value)
        else:
            raise InteractionValidationError(
                f"Invalid localization entry {key}"
            )
    return LocalizationCatalog(locale=locale, entries=entries)


def _entry_placeholders(entry: LocalizationEntry) -> set[str]:
    values = ([entry.text] if entry.text is not None else []) + list(
        entry.forms.values()
    )
    result: set[str] = set()
    formatter = string.Formatter()
    for value in values:
        try:
            for _, field_name, _, _ in formatter.parse(value):
                if field_name:
                    result.add(field_name.split(".", 1)[0].split("[", 1)[0])
        except ValueError as error:
            raise InteractionValidationError(
                "Invalid localization format string"
            ) from error
    return result


def _plural_category(value: Any, locale: str) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return "other"
    if locale == "ru":
        if count % 10 == 1 and count % 100 != 11:
            return "one"
        if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
            return "few"
        return "many"
    return "one" if count == 1 else "other"
