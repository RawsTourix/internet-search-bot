from __future__ import annotations

import base64
import io
import lzma
import tarfile
from pathlib import Path


PAYLOAD_PATH = Path("scripts/.v0_4_file_artifacts_models.payload")

ARTIFACT_CONFIG_BLOCK = r'''
    "artifacts": {
        "enabled": true,
        "max_artifacts_per_cycle": 32,
        "max_versions_per_lineage": 64,
        "max_artifact_size_bytes": 67108864,
        "max_inline_text_chars": 20000,
        "max_read_chars": 100000,
        "max_search_matches": 20,
        "max_patch_operations": 32,
        "max_patchable_text_bytes": 8388608,
        "max_patch_old_text_chars": 20000,
        "max_patch_new_text_chars": 50000,
        "max_runtime_artifact_summaries": 12,
        "allow_opaque_binary": true,
        "auto_select_deliverables": false,
        "max_container_entries_inspected": 2048,
        "max_workspace_bytes": 268435456,
        "workspace_ttl_seconds": 3600
    },
'''


def extract_payload() -> None:
    encoded = PAYLOAD_PATH.read_text(encoding="ascii").strip()
    archive_bytes = lzma.decompress(base64.b85decode(encoded.encode("ascii")))
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not member.isfile()
            ):
                raise RuntimeError(f"Unsafe payload member: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"Missing payload content: {member.name}")
            target = Path.cwd() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())


def update_example_config() -> None:
    path = Path("src/api/mcp.config.example")
    text = path.read_text(encoding="utf-8")
    if '"artifacts"' in text:
        return
    marker = '    "planning": {'
    if marker not in text:
        raise RuntimeError("Could not locate planning section in example config")
    path.write_text(
        text.replace(marker, ARTIFACT_CONFIG_BLOCK + marker, 1),
        encoding="utf-8",
    )


def main() -> None:
    extract_payload()
    update_example_config()


if __name__ == "__main__":
    main()
