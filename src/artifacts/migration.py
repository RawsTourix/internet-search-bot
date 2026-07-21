"""Legacy payload-per-version artifact migration."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import NAMESPACE_URL, uuid5

from ..storage.interfaces import ContentStore
from ..storage.models import ArtifactRef as LegacyArtifactRef
from ..storage.serializers import deserialize_model
from .errors import ArtifactIntegrityError, ArtifactStorageError
from .file_store import FileSystemArtifactStore
from .models import (
    ArtifactLineage,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactVersion,
    utc_now,
)


@dataclass(slots=True)
class LegacyMigrationReport:
    dry_run: bool
    discovered_versions: int = 0
    discovered_lineages: int = 0
    migrated_lineages: int = 0
    skipped_lineages: int = 0
    migrated_versions: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "discovered_versions": self.discovered_versions,
            "discovered_lineages": self.discovered_lineages,
            "migrated_lineages": self.migrated_lineages,
            "skipped_lineages": self.skipped_lineages,
            "migrated_versions": self.migrated_versions,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


class LegacyArtifactMigrator:
    """Import old ``artifacts/art_*/file.bin`` objects without deleting them."""

    def __init__(
        self,
        *,
        artifact_store: FileSystemArtifactStore,
        content_store: ContentStore,
    ) -> None:
        self.artifact_store = artifact_store
        self.content_store = content_store

    async def migrate(self, *, dry_run: bool = True) -> LegacyMigrationReport:
        report = LegacyMigrationReport(dry_run=dry_run)
        legacy = self._load_legacy_versions(report)
        report.discovered_versions = len(legacy)
        chains = self._build_linear_chains(legacy, report)
        report.discovered_lineages = len(chains)

        existing_roots = {
            str(lineage.metadata.get("legacy_root_artifact_id")): lineage
            for lineage in await self.artifact_store._all_lineages()
            if lineage.metadata.get("legacy_root_artifact_id")
        }

        for chain in chains:
            root_id = chain[0].artifact_id
            if root_id in existing_roots:
                report.skipped_lineages += 1
                continue
            try:
                if dry_run:
                    self._validate_chain_payloads(chain)
                    continue
                await self._migrate_chain(chain)
                report.migrated_lineages += 1
                report.migrated_versions += len(chain)
            except Exception as error:
                report.errors.append(
                    f"{root_id}: {type(error).__name__}: {error}"
                )
        return report

    def _load_legacy_versions(
        self,
        report: LegacyMigrationReport,
    ) -> dict[str, LegacyArtifactRef]:
        result: dict[str, LegacyArtifactRef] = {}
        try:
            candidates = list(self.artifact_store.artifacts_dir.iterdir())
        except OSError as error:
            raise ArtifactStorageError(
                "Failed to list legacy artifact storage"
            ) from error

        for candidate in candidates:
            if not candidate.name.startswith("art_"):
                continue
            try:
                mode = candidate.lstat().st_mode
                if candidate.is_symlink() or not stat.S_ISDIR(mode):
                    raise ArtifactIntegrityError(
                        "invalid legacy artifact directory"
                    )
                metadata_path = candidate / "metadata.json"
                payload = metadata_path.read_bytes()
                legacy = deserialize_model(
                    payload,
                    LegacyArtifactRef,
                    object_type="legacy artifact",
                    object_id=candidate.name,
                )
                if legacy.artifact_id != candidate.name:
                    raise ArtifactIntegrityError(
                        "legacy artifact directory and metadata ID disagree"
                    )
                result[legacy.artifact_id] = legacy
            except Exception as error:
                report.errors.append(
                    f"{candidate.name}: {type(error).__name__}: {error}"
                )
        return result

    @staticmethod
    def _build_linear_chains(
        legacy: dict[str, LegacyArtifactRef],
        report: LegacyMigrationReport,
    ) -> list[list[LegacyArtifactRef]]:
        children: dict[str, list[str]] = {}
        roots: list[str] = []
        for artifact in legacy.values():
            parent = artifact.parent_artifact_id
            if parent is None:
                roots.append(artifact.artifact_id)
                continue
            if parent not in legacy:
                report.errors.append(
                    f"{artifact.artifact_id}: missing legacy parent {parent}"
                )
                continue
            children.setdefault(parent, []).append(artifact.artifact_id)

        chains: list[list[LegacyArtifactRef]] = []
        visited: set[str] = set()
        for root_id in sorted(roots):
            chain: list[LegacyArtifactRef] = []
            current_id = root_id
            while True:
                if current_id in visited:
                    report.errors.append(
                        f"{root_id}: cycle or duplicate legacy membership"
                    )
                    chain = []
                    break
                visited.add(current_id)
                chain.append(legacy[current_id])
                next_ids = children.get(current_id, [])
                if len(next_ids) > 1:
                    report.errors.append(
                        f"{root_id}: branching legacy history is unsupported"
                    )
                    chain = []
                    break
                if not next_ids:
                    break
                current_id = next_ids[0]
            if chain:
                expected_versions = list(range(1, len(chain) + 1))
                actual_versions = [item.version for item in chain]
                if actual_versions != expected_versions:
                    report.errors.append(
                        f"{root_id}: non-contiguous legacy version numbers"
                    )
                else:
                    chains.append(chain)

        for artifact_id in sorted(set(legacy) - visited):
            report.errors.append(
                f"{artifact_id}: legacy artifact is not reachable from a root"
            )
        return chains

    def _validate_chain_payloads(
        self,
        chain: list[LegacyArtifactRef],
    ) -> None:
        for legacy in chain:
            path = self._legacy_payload_path(legacy.artifact_id)
            size, content_hash = self._hash_regular_file(path)
            if size != legacy.size_bytes or content_hash != legacy.content_hash:
                raise ArtifactIntegrityError(
                    f"legacy payload integrity mismatch for {legacy.artifact_id}"
                )

    async def _migrate_chain(
        self,
        chain: list[LegacyArtifactRef],
    ) -> None:
        root = chain[0]
        lineage_id = self._lineage_id_for_root(root.artifact_id)
        versions: list[ArtifactVersion] = []

        for position, legacy in enumerate(chain, start=1):
            existing_path = (
                self.artifact_store.versions_dir
                / legacy.artifact_id
                / "metadata.json"
            )
            if existing_path.is_file():
                existing = self.artifact_store._load_version_metadata(
                    legacy.artifact_id
                )
                if (
                    existing.artifact_lineage_id != lineage_id
                    or existing.version != position
                    or existing.content_hash != legacy.content_hash
                ):
                    raise ArtifactIntegrityError(
                        "existing migrated version is incompatible"
                    )
                versions.append(existing)
                continue

            payload_path = self._legacy_payload_path(legacy.artifact_id)
            size, content_hash = self._hash_regular_file(payload_path)
            if size != legacy.size_bytes or content_hash != legacy.content_hash:
                raise ArtifactIntegrityError(
                    f"legacy payload integrity mismatch for {legacy.artifact_id}"
                )

            content_ref = await self.content_store.save_stream(
                self._iter_file(payload_path),
                source_type="legacy_artifact_migration",
                source_name=legacy.filename,
                mime_type=legacy.mime_type,
                cycle_id=legacy.cycle_id,
                metadata={
                    "legacy_artifact_id": legacy.artifact_id,
                    "legacy_source": legacy.source,
                },
                max_size_bytes=max(1, legacy.size_bytes),
            )
            if content_ref.content_hash != legacy.content_hash:
                raise ArtifactIntegrityError(
                    "migrated content hash differs from legacy payload"
                )

            parent_id = (
                chain[position - 2].artifact_id
                if position > 1
                else None
            )
            version = ArtifactVersion(
                artifact_id=legacy.artifact_id,
                artifact_lineage_id=lineage_id,
                version=position,
                parent_artifact_id=parent_id,
                content_id=content_ref.content_id,
                filename=legacy.filename,
                format_id=self._legacy_format_id(legacy.filename),
                encoding=(
                    "utf-8"
                    if legacy.mime_type.startswith("text/")
                    else None
                ),
                declared_mime_type=legacy.mime_type,
                detected_mime_type=legacy.mime_type,
                size_bytes=content_ref.size_bytes,
                content_hash=content_ref.content_hash,
                created_cycle_id=legacy.cycle_id,
                created_at=legacy.created_at,
                provenance=ArtifactProvenance(
                    origin="migration",
                    creator="runtime",
                    source_artifact_ids=(
                        [parent_id] if parent_id is not None else []
                    ),
                    operation="legacy_artifact_migration",
                ),
                metadata={
                    **legacy.metadata,
                    "legacy_source": legacy.source,
                    "legacy_delivery_targets": list(
                        legacy.delivery_targets
                    ),
                },
            )
            self.artifact_store._write_version_metadata(version)
            versions.append(version)

        now = utc_now()
        purpose = self._legacy_purpose(chain)
        lineage = ArtifactLineage(
            artifact_lineage_id=lineage_id,
            session_id=str(
                root.metadata.get("session_id")
                or f"legacy:{root.cycle_id}"
            ),
            created_cycle_id=root.cycle_id,
            current_artifact_id=versions[-1].artifact_id,
            current_version=len(versions),
            committed_artifact_ids=[
                version.artifact_id for version in versions
            ],
            purpose=purpose,
            title=root.filename,
            created_at=root.created_at,
            updated_at=max(
                versions[-1].created_at,
                now,
            ),
            metadata={
                "legacy_root_artifact_id": root.artifact_id,
                "legacy_migrated": True,
            },
        )
        lineage_path = self.artifact_store.lineages_dir / lineage_id
        if lineage_path.exists():
            existing = self.artifact_store._load_lineage_metadata(
                lineage_id
            )
            if existing != lineage:
                raise ArtifactIntegrityError(
                    "existing migrated lineage is incompatible"
                )
            return
        self.artifact_store._write_lineage_metadata(lineage)

    def _legacy_payload_path(self, artifact_id: str) -> Path:
        path = self.artifact_store.artifacts_dir / artifact_id / "file.bin"
        if not path.exists() and not path.is_symlink():
            raise ArtifactIntegrityError(
                f"missing legacy payload for {artifact_id}"
            )
        return path

    @staticmethod
    def _hash_regular_file(path: Path) -> tuple[int, str]:
        try:
            mode = path.lstat().st_mode
            if path.is_symlink() or not stat.S_ISREG(mode):
                raise ArtifactIntegrityError("invalid legacy payload file")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as source:
                while block := source.read(64 * 1024):
                    size += len(block)
                    digest.update(block)
            return size, f"sha256:{digest.hexdigest()}"
        except ArtifactIntegrityError:
            raise
        except OSError as error:
            raise ArtifactIntegrityError(
                "failed to read legacy payload"
            ) from error

    @staticmethod
    async def _iter_file(path: Path) -> AsyncIterator[bytes]:
        source = await asyncio.to_thread(path.open, "rb")
        try:
            while True:
                block = await asyncio.to_thread(source.read, 64 * 1024)
                if not block:
                    break
                yield block
        finally:
            await asyncio.to_thread(source.close)

    @staticmethod
    def _lineage_id_for_root(root_artifact_id: str) -> str:
        return "aln_" + uuid5(
            NAMESPACE_URL,
            f"internet-search-bot:legacy-artifact:{root_artifact_id}",
        ).hex

    @staticmethod
    def _legacy_format_id(filename: str) -> str:
        extension = Path(filename).suffix.lower().lstrip(".")
        if extension and extension.replace("_", "").replace("-", "").isalnum():
            if extension[0].isalpha():
                return extension[:64]
            return ("file_" + extension)[:64]
        return "opaque_binary"

    @staticmethod
    def _legacy_purpose(
        chain: list[LegacyArtifactRef],
    ) -> ArtifactPurpose:
        if any(item.delivery_targets for item in chain):
            return ArtifactPurpose.DELIVERABLE
        if chain[0].source == "user_upload":
            return ArtifactPurpose.INPUT
        return ArtifactPurpose.WORKING


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate legacy artifact payload directories."
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


async def _run_cli(args) -> int:
    from ..storage import StorageConfigType, create_storage_services
    from .config import ArtifactConfigType

    storage_config = StorageConfigType(root_dir=args.root)
    storage_services = create_storage_services(storage_config)
    artifact_store = FileSystemArtifactStore(
        storage_config=storage_config,
        artifact_config=ArtifactConfigType(),
        content_store=storage_services.content_store,
        allow_legacy_layout=True,
    )
    report = await LegacyArtifactMigrator(
        artifact_store=artifact_store,
        content_store=storage_services.content_store,
    ).migrate(dry_run=not args.apply)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 1 if report.errors else 0


def main() -> None:
    args = _build_argument_parser().parse_args()
    raise SystemExit(asyncio.run(_run_cli(args)))


if __name__ == "__main__":
    main()
