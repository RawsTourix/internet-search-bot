"""Bounded validation for structured semantic input."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..interaction.parts import ContactInputPart, InputPart, PollInputPart
from .config import IngressConfigType


class SemanticInputLimitError(ValueError):
    """Semantic input exceeds a configured admission invariant."""


def validate_semantic_parts(
    parts: Sequence[InputPart],
    config: IngressConfigType,
) -> None:
    if len(parts) > config.max_semantic_parts_per_batch:
        raise SemanticInputLimitError("semantic part count limit exceeded")
    part_ids = [item.part_id for item in parts]
    if len(part_ids) != len(set(part_ids)):
        raise SemanticInputLimitError("semantic part ID collision")

    total_bytes = 0
    for item in parts:
        payload = item.model_dump(mode="json")
        metadata = payload.get("metadata") or {}
        metadata_bytes = len(
            json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if metadata_bytes > config.max_semantic_metadata_bytes_per_part:
            raise SemanticInputLimitError(
                "semantic part metadata limit exceeded"
            )
        total_bytes += len(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if isinstance(item, ContactInputPart):
            if item.vcard is not None and len(item.vcard) > config.max_vcard_chars:
                raise SemanticInputLimitError("vCard character limit exceeded")
        if isinstance(item, PollInputPart):
            if len(item.options) > config.max_poll_options:
                raise SemanticInputLimitError("poll option count limit exceeded")
            if any(
                len(option) > config.max_poll_option_chars
                for option in item.options
            ):
                raise SemanticInputLimitError(
                    "poll option character limit exceeded"
                )
    if total_bytes > config.max_semantic_total_bytes:
        raise SemanticInputLimitError("semantic input byte limit exceeded")
