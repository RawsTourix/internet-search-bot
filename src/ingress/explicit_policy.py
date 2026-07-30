"""Shared compatibility policy for durable explicit input collection.

The current persisted ingress schema already reserves ``IMMEDIATE_TEXT`` as a
non-atomic, non-timed grouping slot and no runtime path uses it. During the
``v0.4-batch-workflows`` rollout it represents an explicit user-controlled
collection internally. Public/domain semantics remain ``InputAssemblyMode.EXPLICIT``
and ``InputCommitPolicy.EXPLICIT``; a later schema migration may rename the
persisted enum value without changing those contracts.
"""

from __future__ import annotations

from .models import InputBatchDraft, InputGroupingMode


EXPLICIT_COLLECTION_GROUPING_MODE = InputGroupingMode.IMMEDIATE_TEXT
EXPLICIT_COLLECTION_ROUTE_METADATA_KEY = "_server_input_collection_id"
EXPLICIT_COLLECTION_COMMIT_REASON = "explicit_collection_commit"


def is_explicit_collection_draft(draft: InputBatchDraft) -> bool:
    """Return whether a persisted draft belongs to explicit collection mode."""

    return draft.grouping_mode == EXPLICIT_COLLECTION_GROUPING_MODE
