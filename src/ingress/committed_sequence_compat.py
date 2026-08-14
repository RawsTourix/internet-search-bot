"""Backward-compatible committed InputBatch sequence reconciliation.

The historical schema-v1 -> schema-v2 transition briefly used a current-model-
only allocator. Supported v1 committed records were therefore invisible while
allocating new v2 records, allowing a later v2 suffix to restart at sequence 1.

This module keeps one schema-aware committed loader for ordinary loads,
sequence allocation and the narrow reconciliation of that known defect. It does
not turn generic committed ordering corruption into an auto-repair policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.artifacts.errors import ArtifactIntegrityError
from src.ingress.models import CommittedInputBatch
from src.ingress.upgrades import upgrade_committed_input_batch


logger = logging.getLogger("API.Ingress.CommittedSequenceCompatibility")


@dataclass(frozen=True)
class _PersistedCommitted:
    path: Path
    raw: dict[str, Any]
    persisted_schema_version: int
    batch: CommittedInputBatch


class SchemaAwareCommittedSequenceMixin:
    """Unify committed reads/allocation and reconcile the known v1/v2 overlap."""

    _legacy_committed_sequence_reconciliation_checked = False

    def _load_committed_path_schema_aware_sync(
        self,
        path: Path,
    ) -> CommittedInputBatch:
        try:
            return CommittedInputBatch.model_validate(
                upgrade_committed_input_batch(self._read_json(path))
            )
        except (ValidationError, ValueError) as error:
            raise ArtifactIntegrityError("Invalid committed input batch") from error

    def _load_committed_sync(self, input_batch_id: str) -> CommittedInputBatch:
        self._ensure_legacy_committed_sequence_reconciled_sync()
        return self._load_committed_path_schema_aware_sync(
            self.root / input_batch_id / "committed.json"
        )

    def _next_sequence_sync(self, session_id: str) -> int:
        self._ensure_legacy_committed_sequence_reconciled_sync()
        maximum = 0
        for path in sorted(self.root.glob("ibat_*/committed.json")):
            item = self._load_committed_path_schema_aware_sync(path)
            if item.session_id == session_id:
                maximum = max(maximum, item.sequence_number)
        return maximum + 1

    def _ensure_legacy_committed_sequence_reconciled_sync(self) -> None:
        if getattr(
            self,
            "_legacy_committed_sequence_reconciliation_checked",
            False,
        ):
            return
        affected_sessions, migrated_batches = (
            self.reconcile_legacy_committed_sequence_overlaps_sync()
        )
        # Set the process-local convergence marker only after all durable writes
        # have completed. A crash/exception leaves the next store free to retry.
        self._legacy_committed_sequence_reconciliation_checked = True
        if migrated_batches:
            logger.warning(
                "legacy_committed_sequence_migration_complete "
                "affected_sessions=%s migrated_batches=%s",
                affected_sessions,
                migrated_batches,
            )

    def reconcile_legacy_committed_sequence_overlaps_sync(self) -> tuple[int, int]:
        """Repair only an unambiguous historical v1-prefix/v2-suffix restart.

        A recognised current-schema suffix is ordered by strictly increasing
        committed_at and every rank may be in exactly one of two states:
        the historical restarted rank (1, 2, ...) or its reconstructed target
        immediately after the v1 prefix. Already-migrated records must form a
        prefix, which is exactly what oldest-first atomic writes can leave after
        a crash. Any other shape is left untouched for strict recovery to reject.
        """

        with self._lock:
            by_session: dict[str, list[_PersistedCommitted]] = {}
            for path in sorted(self.root.glob("ibat_*/committed.json")):
                raw = self._read_json(path)
                try:
                    persisted_schema_version = int(raw.get("schema_version", 1))
                    batch = CommittedInputBatch.model_validate(
                        upgrade_committed_input_batch(raw)
                    )
                except (TypeError, ValidationError, ValueError) as error:
                    raise ArtifactIntegrityError(
                        "Invalid committed input batch"
                    ) from error
                by_session.setdefault(batch.session_id, []).append(
                    _PersistedCommitted(
                        path=path,
                        raw=raw,
                        persisted_schema_version=persisted_schema_version,
                        batch=batch,
                    )
                )

            affected_sessions = 0
            migrated_batches = 0
            for records in by_session.values():
                migrated = self._reconcile_one_session_sync(records)
                if migrated:
                    affected_sessions += 1
                    migrated_batches += migrated
            return affected_sessions, migrated_batches

    def _reconcile_one_session_sync(
        self,
        records: list[_PersistedCommitted],
    ) -> int:
        legacy = [item for item in records if item.persisted_schema_version == 1]
        current = [item for item in records if item.persisted_schema_version == 2]
        if not legacy or not current or len(legacy) + len(current) != len(records):
            return 0

        legacy_by_sequence = sorted(
            legacy,
            key=lambda item: (item.batch.sequence_number, item.batch.input_batch_id),
        )
        legacy_sequences = [item.batch.sequence_number for item in legacy_by_sequence]
        legacy_count = len(legacy_by_sequence)
        if legacy_sequences != list(range(1, legacy_count + 1)):
            return 0
        legacy_times = [item.batch.committed_at for item in legacy_by_sequence]
        if any(left >= right for left, right in zip(legacy_times, legacy_times[1:])):
            return 0

        current_by_time = sorted(
            current,
            key=lambda item: (item.batch.committed_at, item.batch.input_batch_id),
        )
        current_times = [item.batch.committed_at for item in current_by_time]
        if any(left >= right for left, right in zip(current_times, current_times[1:])):
            return 0
        if legacy_times[-1] >= current_times[0]:
            return 0

        migrated_state: list[bool] = []
        targets: list[int] = []
        for rank, item in enumerate(current_by_time, start=1):
            target = legacy_count + rank
            targets.append(target)
            sequence = item.batch.sequence_number
            if sequence == target:
                migrated_state.append(True)
            elif sequence == rank:
                migrated_state.append(False)
            else:
                return 0

        # Crash convergence proof: writes happen oldest-first, therefore an
        # already-renumbered record may never appear after an unrenumbered one.
        seen_unmigrated = False
        for is_migrated in migrated_state:
            if not is_migrated:
                seen_unmigrated = True
            elif seen_unmigrated:
                return 0

        migrated = 0
        for item, target, is_migrated in zip(
            current_by_time,
            targets,
            migrated_state,
        ):
            if is_migrated:
                continue
            rewritten = dict(item.raw)
            rewritten["sequence_number"] = target
            try:
                CommittedInputBatch.model_validate(
                    upgrade_committed_input_batch(rewritten)
                )
            except (ValidationError, ValueError) as error:
                raise ArtifactIntegrityError(
                    "Invalid migrated committed input batch"
                ) from error
            # _write_json publishes one file atomically under the store's
            # re-entrant lock. A crash after any write leaves a recognised
            # migrated prefix that converges on the next startup.
            self._write_json(item.path, rewritten)
            migrated += 1
        return migrated
