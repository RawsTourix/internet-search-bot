from __future__ import annotations

import base64
import lzma
from pathlib import Path


PAYLOAD_PATH = Path("scripts/.v0_4_streaming_content_storage.payload")


def main() -> None:
    encoded = PAYLOAD_PATH.read_text(encoding="ascii").strip()
    source = lzma.decompress(base64.b85decode(encoded.encode("ascii")))
    code = compile(source, "<v0.4-streaming-content-storage>", "exec")
    namespace = {"__name__": "__main__"}
    exec(code, namespace, namespace)


if __name__ == "__main__":
    main()
