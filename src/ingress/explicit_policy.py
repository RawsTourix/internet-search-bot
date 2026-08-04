"""Shared persisted policy for durable explicit input collection."""

from __future__ import annotations

from .models import InputBatchDraft, InputGroupingMode


EXPLICIT_COLLECTION_GROUPING_MODE = InputGroupingMode.EXPLICIT_COLLECTION
LEGACY_EXPLICIT_COLLECTION_GROUPING_MODE = InputGroupingMode.IMMEDIATE_TEXT
EXPLICIT_COLLECTION_ROUTE_METADATA_KEY = "_server_input_collection_id"
EXPLICIT_COLLECTION_COMMIT_REASON = "explicit_collection_commit"


def is_explicit_collection_draft(draft: InputBatchDraft) -> bool:
    """Return whether a draft uses canonical or rollout-era explicit mode."""

    return draft.grouping_mode in {
        EXPLICIT_COLLECTION_GROUPING_MODE,
        LEGACY_EXPLICIT_COLLECTION_GROUPING_MODE,
    }


def is_legacy_explicit_collection_draft(draft: InputBatchDraft) -> bool:
    """Return whether a draft still uses the rollout-era persisted marker."""

    return draft.grouping_mode == LEGACY_EXPLICIT_COLLECTION_GROUPING_MODE
