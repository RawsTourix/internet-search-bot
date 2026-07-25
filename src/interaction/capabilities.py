"""Versioned server-owned client capability contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import CapabilityValidationError
from .ids import is_interaction_id, new_capability_snapshot_id


_CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CapabilityValue = int | float | str | bool
LimitValueType = Literal["int", "float", "str", "bool"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_capability_id(value: str) -> str:
    normalized = value.strip()
    if not _CAPABILITY_ID_RE.fullmatch(normalized):
        raise ValueError("invalid canonical capability ID")
    return normalized


class FrozenDict(dict[str, CapabilityValue]):
    """Small JSON-compatible immutable mapping used inside frozen snapshots."""

    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("capability snapshot limits are immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked


class _CapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientCapabilitySpec(_CapabilityModel):
    capability_id: str
    description: str
    introduced_contract_version: int = Field(ge=1)
    deprecated_contract_version: int | None = Field(default=None, ge=1)
    transport_scope: str | None = None

    @field_validator("capability_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_capability_id(value)


class ClientLimitSpec(_CapabilityModel):
    limit_id: str
    value_type: LimitValueType
    minimum: int | float | None = None
    maximum: int | float | None = None
    allowed_values: tuple[CapabilityValue, ...] | None = None
    description: str
    introduced_contract_version: int = Field(ge=1)
    transport_scope: str | None = None

    @field_validator("limit_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_capability_id(value)


class ClientCapabilityDeclaration(_CapabilityModel):
    capability_contract_version: int = Field(ge=1)
    client_version: str | None = None
    features: tuple[str, ...] = ()
    limits: dict[str, CapabilityValue] = Field(default_factory=dict)

    @field_validator("features")
    @classmethod
    def validate_feature_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_capability_id(item) for item in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate capability feature ID")
        return normalized

    @field_validator("limits")
    @classmethod
    def validate_limit_ids(
        cls, values: dict[str, CapabilityValue]
    ) -> dict[str, CapabilityValue]:
        return {validate_capability_id(key): value for key, value in values.items()}


class ClientCapabilitySnapshot(_CapabilityModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    capability_snapshot_id: str
    capability_contract_version: int = Field(ge=1)
    client_type: str
    client_instance_id: str
    client_version: str | None = None
    features: tuple[str, ...] = ()
    limits: dict[str, CapabilityValue] = Field(default_factory=dict)
    fingerprint: str
    captured_at: datetime

    @field_validator("capability_snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="cbs"):
            raise ValueError("invalid capability_snapshot_id")
        return value

    @field_validator("client_type", "client_instance_id")
    @classmethod
    def validate_binding(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized

    @field_validator("features")
    @classmethod
    def validate_features(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(dict.fromkeys(validate_capability_id(v) for v in values)))
        if canonical != values:
            raise ValueError("snapshot features must be unique and canonically sorted")
        return canonical

    @field_validator("limits")
    @classmethod
    def freeze_limits(
        cls, values: dict[str, CapabilityValue]
    ) -> FrozenDict:
        canonical = {
            key: values[key]
            for key in sorted(validate_capability_id(item) for item in values)
        }
        return FrozenDict(canonical)

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("invalid capability fingerprint")
        return value

    @field_validator("captured_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    def to_ref(self) -> "ClientCapabilitySnapshotRef":
        return ClientCapabilitySnapshotRef.from_snapshot(self)


class ClientCapabilitySnapshotRef(_CapabilityModel):
    capability_snapshot_id: str
    capability_contract_version: int = Field(ge=1)
    fingerprint: str

    @field_validator("capability_snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="cbs"):
            raise ValueError("invalid capability_snapshot_id")
        return value

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not _FINGERPRINT_RE.fullmatch(value):
            raise ValueError("invalid capability fingerprint")
        return value

    @classmethod
    def from_snapshot(
        cls, snapshot: ClientCapabilitySnapshot
    ) -> "ClientCapabilitySnapshotRef":
        return cls(
            capability_snapshot_id=snapshot.capability_snapshot_id,
            capability_contract_version=snapshot.capability_contract_version,
            fingerprint=snapshot.fingerprint,
        )


class ClientCapabilityRegistry:
    """Canonical feature/limit registry with strict declaration validation."""

    def __init__(
        self,
        *,
        contract_version: int,
        features: tuple[ClientCapabilitySpec, ...],
        limits: tuple[ClientLimitSpec, ...],
    ) -> None:
        self.contract_version = contract_version
        self.features = {item.capability_id: item for item in features}
        self.limits = {item.limit_id: item for item in limits}
        if len(self.features) != len(features) or len(self.limits) != len(limits):
            raise ValueError("duplicate capability registry ID")

    def resolve(
        self,
        declaration: ClientCapabilityDeclaration,
        *,
        client_type: str,
        client_instance_id: str,
        reject_unknown: bool = True,
        max_feature_count: int = 64,
        max_limit_count: int = 64,
        captured_at: datetime | None = None,
    ) -> ClientCapabilitySnapshot:
        if declaration.capability_contract_version != self.contract_version:
            raise CapabilityValidationError("Unsupported capability contract version")
        if len(declaration.features) > max_feature_count:
            raise CapabilityValidationError("Capability feature count exceeds policy")
        if len(declaration.limits) > max_limit_count:
            raise CapabilityValidationError("Capability limit count exceeds policy")

        features: list[str] = []
        for capability_id in declaration.features:
            spec = self.features.get(capability_id)
            if spec is None:
                if reject_unknown:
                    raise CapabilityValidationError(
                        f"Unknown capability: {capability_id}"
                    )
                continue
            if spec.introduced_contract_version > self.contract_version:
                continue
            features.append(capability_id)

        limits: dict[str, CapabilityValue] = {}
        for limit_id, value in declaration.limits.items():
            spec = self.limits.get(limit_id)
            if spec is None:
                if reject_unknown:
                    raise CapabilityValidationError(f"Unknown limit: {limit_id}")
                continue
            self._validate_limit(spec, value)
            limits[limit_id] = value

        canonical_features = tuple(sorted(dict.fromkeys(features)))
        canonical_limits = {key: limits[key] for key in sorted(limits)}
        fingerprint = capability_fingerprint(
            contract_version=self.contract_version,
            client_type=client_type,
            client_instance_id=client_instance_id,
            client_version=declaration.client_version,
            features=canonical_features,
            limits=canonical_limits,
        )
        return ClientCapabilitySnapshot(
            capability_snapshot_id=new_capability_snapshot_id(),
            capability_contract_version=self.contract_version,
            client_type=client_type.strip(),
            client_instance_id=client_instance_id.strip(),
            client_version=declaration.client_version,
            features=canonical_features,
            limits=canonical_limits,
            fingerprint=fingerprint,
            captured_at=captured_at or utc_now(),
        )

    @staticmethod
    def _validate_limit(spec: ClientLimitSpec, value: CapabilityValue) -> None:
        expected = {
            "int": int,
            "float": (int, float),
            "str": str,
            "bool": bool,
        }[spec.value_type]
        if spec.value_type == "int":
            valid_type = type(value) is int
        elif spec.value_type == "float":
            valid_type = type(value) in {int, float}
        else:
            valid_type = isinstance(value, expected)
        if not valid_type:
            raise CapabilityValidationError(
                f"Limit {spec.limit_id} has invalid value type"
            )
        if spec.allowed_values is not None and value not in spec.allowed_values:
            raise CapabilityValidationError(
                f"Limit {spec.limit_id} has unsupported value"
            )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if spec.minimum is not None and value < spec.minimum:
                raise CapabilityValidationError(
                    f"Limit {spec.limit_id} is below minimum"
                )
            if spec.maximum is not None and value > spec.maximum:
                raise CapabilityValidationError(
                    f"Limit {spec.limit_id} exceeds maximum"
                )


def capability_fingerprint(
    *,
    contract_version: int,
    client_type: str,
    client_instance_id: str,
    client_version: str | None,
    features: tuple[str, ...],
    limits: dict[str, CapabilityValue],
) -> str:
    payload = {
        "capability_contract_version": contract_version,
        "client_type": client_type.strip(),
        "client_instance_id": client_instance_id.strip(),
        "client_version": client_version,
        "features": list(features),
        "limits": {key: limits[key] for key in sorted(limits)},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


_FEATURE_IDS = (
    "input.text",
    "input.artifact.document",
    "input.media.image",
    "input.media.audio",
    "input.media.voice",
    "input.media.video",
    "input.media.video_note",
    "input.media.animation",
    "input.media.sticker",
    "input.location",
    "input.contact",
    "input.forward_provenance",
    "output.text",
    "output.artifact.document",
    "output.media.image",
    "output.media.audio",
    "output.media.voice",
    "output.media.video",
    "output.media.video_note",
    "output.media.animation",
    "output.media.sticker",
    "output.location",
    "output.contact",
    "output.group.document",
    "output.group.image",
    "output.group.audio",
    "output.group.mixed_media",
    "presentation.reply_anchor",
    "presentation.message_edit",
    "presentation.status_updates",
    "presentation.intermediate_output",
    "presentation.interactive_output",
    "transport.streaming_upload",
    "transport.streaming_download",
)


def build_default_capability_registry(
    contract_version: int = 1,
) -> ClientCapabilityRegistry:
    features = tuple(
        ClientCapabilitySpec(
            capability_id=capability_id,
            description=f"Canonical client capability {capability_id}.",
            introduced_contract_version=1,
            transport_scope=(
                "telegram" if capability_id.startswith("transport.telegram.") else None
            ),
        )
        for capability_id in _FEATURE_IDS
    )
    limits = (
        ClientLimitSpec(
            limit_id="transport.telegram.output.document_group.max_items",
            value_type="int",
            minimum=2,
            maximum=10,
            description="Maximum compatible Telegram documents in one media group.",
            introduced_contract_version=1,
            transport_scope="telegram",
        ),
        ClientLimitSpec(
            limit_id="transport.telegram.output.caption.max_chars",
            value_type="int",
            minimum=1,
            maximum=4096,
            description="Maximum Telegram output caption length.",
            introduced_contract_version=1,
            transport_scope="telegram",
        ),
        ClientLimitSpec(
            limit_id="transport.telegram.output.text.max_chars",
            value_type="int",
            minimum=1,
            maximum=8192,
            description="Maximum Telegram text operation length.",
            introduced_contract_version=1,
            transport_scope="telegram",
        ),
        ClientLimitSpec(
            limit_id="transport.telegram.presentation.edit.max_chars",
            value_type="int",
            minimum=1,
            maximum=4096,
            description="Maximum Telegram status edit length.",
            introduced_contract_version=1,
            transport_scope="telegram",
        ),
    )
    return ClientCapabilityRegistry(
        contract_version=contract_version,
        features=features,
        limits=limits,
    )


def build_telegram_capability_declaration(
    *,
    client_version: str | None = None,
    document_grouping: bool = True,
    message_editing: bool = True,
) -> ClientCapabilityDeclaration:
    features = [
        "input.text",
        "input.artifact.document",
        "input.media.image",
        "input.media.audio",
        "input.media.voice",
        "input.media.video",
        "input.media.video_note",
        "input.media.animation",
        "input.media.sticker",
        "input.location",
        "input.contact",
        "input.forward_provenance",
        "output.text",
        "output.artifact.document",
        "presentation.reply_anchor",
        "presentation.status_updates",
        "transport.streaming_upload",
        "transport.streaming_download",
    ]
    if document_grouping:
        features.append("output.group.document")
    if message_editing:
        features.append("presentation.message_edit")
    return ClientCapabilityDeclaration(
        capability_contract_version=1,
        client_version=client_version,
        features=tuple(features),
        limits={
            "transport.telegram.output.document_group.max_items": 10,
            "transport.telegram.output.caption.max_chars": 1024,
            "transport.telegram.output.text.max_chars": 4096,
            "transport.telegram.presentation.edit.max_chars": 4096,
        },
    )


def build_web_capability_declaration(
    *, client_version: str | None = None
) -> ClientCapabilityDeclaration:
    return ClientCapabilityDeclaration(
        capability_contract_version=1,
        client_version=client_version,
        features=(
            "input.text",
            "input.artifact.document",
            "output.text",
            "output.artifact.document",
            "presentation.status_updates",
            "transport.streaming_upload",
            "transport.streaming_download",
        ),
    )


def build_cli_capability_declaration(
    *, client_version: str | None = None
) -> ClientCapabilityDeclaration:
    return ClientCapabilityDeclaration(
        capability_contract_version=1,
        client_version=client_version,
        features=("input.text", "output.text", "output.artifact.document"),
    )
