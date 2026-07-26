"""Stable identifiers for the transport-independent interaction domain."""

from __future__ import annotations

import re
from uuid import uuid4


_PREFIXES = {
    "capability_snapshot": "cbs",
    "response_anchor": "anch",
    "input_presentation": "iprs",
    "output_batch": "obat",
    "output_part": "opart",
    "output_attempt": "odat",
    "output_claim_request": "oclm",
    "delivery_group": "odgrp",
}
_ID_RE = re.compile(
    r"^(?:cbs|anch|iprs|obat|opart|odat|oclm|odgrp)_[0-9a-f]{32}$"
)


def new_interaction_id(kind: str) -> str:
    try:
        prefix = _PREFIXES[kind]
    except KeyError as error:
        raise ValueError(f"Unknown interaction ID kind: {kind}") from error
    return f"{prefix}_{uuid4().hex}"


def is_interaction_id(value: str, *, prefix: str | None = None) -> bool:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        return False
    return prefix is None or value.startswith(prefix + "_")


def new_capability_snapshot_id() -> str:
    return new_interaction_id("capability_snapshot")


def new_response_anchor_id() -> str:
    return new_interaction_id("response_anchor")


def new_input_presentation_id() -> str:
    return new_interaction_id("input_presentation")


def new_presentation_id() -> str:
    """Readable alias used by the presentation domain."""
    return new_input_presentation_id()


def new_output_batch_id() -> str:
    return new_interaction_id("output_batch")


def new_output_part_id() -> str:
    return new_interaction_id("output_part")


def new_output_attempt_id() -> str:
    return new_interaction_id("output_attempt")


def new_output_claim_request_id() -> str:
    return new_interaction_id("output_claim_request")


def new_delivery_group_id() -> str:
    return new_interaction_id("delivery_group")


def new_output_delivery_group_id() -> str:
    """Readable alias used by the output delivery domain."""
    return new_delivery_group_id()
