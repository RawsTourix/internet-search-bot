from __future__ import annotations

import base64
import io
import lzma
import tarfile
from pathlib import Path


PAYLOAD_PATH = Path("scripts/.v0_4_artifact_lineage_store.payload")


def main() -> None:
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


if __name__ == "__main__":
    main()
