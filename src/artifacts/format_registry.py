"""Extensible artifact format registry and bounded content detection."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePath

from pydantic import BaseModel, ConfigDict, Field

from .errors import ArtifactCapabilityError, ArtifactValidationError
from .models import (
    ArtifactCapability,
    ArtifactContentKind,
    ArtifactFormatSpec,
)


class ArtifactFormatDetection(BaseModel):
    """One normalized format decision with non-authoritative evidence."""

    model_config = ConfigDict(extra="forbid")

    format_id: str
    detected_mime_type: str
    content_kind: ArtifactContentKind
    capabilities: list[ArtifactCapability] = Field(default_factory=list)
    confidence: str
    evidence: list[str] = Field(default_factory=list)
    requires_external_processor: bool = False


class ArtifactFormatRegistry:
    """Open registry. Runtime may add formats without changing a closed enum."""

    def __init__(self, specs: Iterable[ArtifactFormatSpec]) -> None:
        self._specs: dict[str, ArtifactFormatSpec] = {}
        self._extensions: dict[str, str] = {}
        self._mimes: dict[str, str] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ArtifactFormatSpec) -> None:
        if spec.format_id in self._specs:
            raise ArtifactValidationError(
                "duplicate_artifact_format",
                f"Artifact format {spec.format_id!r} is already registered.",
                retryable=False,
            )
        for extension in spec.extensions:
            owner = self._extensions.get(extension)
            if owner is not None and owner != spec.format_id:
                raise ArtifactValidationError(
                    "duplicate_artifact_extension",
                    f"Extension {extension!r} is already owned by {owner!r}.",
                    retryable=False,
                )
        mime = spec.canonical_mime_type.lower()
        self._specs[spec.format_id] = spec
        self._mimes.setdefault(mime, spec.format_id)
        for extension in spec.extensions:
            self._extensions[extension] = spec.format_id

    def get(self, format_id: str) -> ArtifactFormatSpec:
        normalized = format_id.strip().lower()
        try:
            return self._specs[normalized]
        except KeyError as error:
            raise ArtifactCapabilityError(
                f"Unsupported artifact format {normalized!r}"
            ) from error

    def list_specs(self) -> list[ArtifactFormatSpec]:
        return [self._specs[key] for key in sorted(self._specs)]

    def detect(
        self,
        *,
        filename: str,
        declared_mime_type: str | None,
        prefix: bytes,
        container_entries: Iterable[str] = (),
    ) -> ArtifactFormatDetection:
        """Detect by signature/container first, then MIME, extension, fallback."""

        evidence: list[str] = []
        signature_format = _detect_signature(prefix, container_entries)
        if signature_format is not None:
            evidence.append(f"signature:{signature_format}")
            return self._detection(signature_format, "high", evidence)

        mime = (declared_mime_type or "").split(";", 1)[0].strip().lower()
        if mime and mime != "application/octet-stream":
            mime_format = self._mimes.get(mime)
            if mime_format is not None:
                evidence.append(f"declared_mime:{mime}")
                return self._detection(mime_format, "medium", evidence)

        extension = PurePath(filename).suffix.lower().lstrip(".")
        if extension:
            extension_format = self._extensions.get(extension)
            if extension_format is not None:
                evidence.append(f"extension:{extension}")
                return self._detection(extension_format, "low", evidence)

        evidence.append("fallback:opaque_binary")
        return self._detection("opaque_binary", "low", evidence)

    def _detection(
        self,
        format_id: str,
        confidence: str,
        evidence: list[str],
    ) -> ArtifactFormatDetection:
        spec = self.get(format_id)
        return ArtifactFormatDetection(
            format_id=spec.format_id,
            detected_mime_type=spec.canonical_mime_type,
            content_kind=spec.content_kind,
            capabilities=sorted(spec.capabilities, key=lambda item: item.value),
            confidence=confidence,
            evidence=evidence,
            requires_external_processor=spec.requires_external_processor,
        )


def _detect_signature(prefix: bytes, container_entries: Iterable[str]) -> str | None:
    if prefix.startswith(b"%PDF-"):
        return "pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if (
        len(prefix) >= 12
        and prefix[:4] == b"RIFF"
        and prefix[8:12] == b"WEBP"
    ):
        return "webp"
    if prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        normalized = {entry.replace("\\", "/").lstrip("/") for entry in container_entries}
        if "word/document.xml" in normalized:
            return "docx"
        if "xl/workbook.xml" in normalized:
            return "xlsx"
        if "ppt/presentation.xml" in normalized:
            return "pptx"
        return "zip"
    return None


def build_default_format_registry() -> ArtifactFormatRegistry:
    read = ArtifactCapability.READ_TEXT
    search = ArtifactCapability.SEARCH_TEXT
    replace = ArtifactCapability.REPLACE_TEXT
    patch = ArtifactCapability.PATCH_TEXT
    process = ArtifactCapability.PROCESS_EXTERNALLY
    deliver = ArtifactCapability.DELIVER

    native_caps = {read, search, replace, patch, deliver}
    binary_caps = {process, deliver}

    specs = [
        ArtifactFormatSpec(
            format_id="txt",
            canonical_mime_type="text/plain",
            extensions=("txt",),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="markdown",
            canonical_mime_type="text/markdown",
            extensions=("md", "markdown"),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="json",
            canonical_mime_type="application/json",
            extensions=("json",),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="yaml",
            canonical_mime_type="application/yaml",
            extensions=("yaml", "yml"),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="csv",
            canonical_mime_type="text/csv",
            extensions=("csv",),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="python",
            canonical_mime_type="text/x-python",
            extensions=("py",),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="javascript",
            canonical_mime_type="text/javascript",
            extensions=("js", "mjs", "cjs"),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="typescript",
            canonical_mime_type="text/typescript",
            extensions=("ts", "tsx"),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities=native_caps,
            default_encoding="utf-8",
        ),
        ArtifactFormatSpec(
            format_id="pdf",
            canonical_mime_type="application/pdf",
            extensions=("pdf",),
            content_kind=ArtifactContentKind.BINARY_DOCUMENT,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="docx",
            canonical_mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            extensions=("docx",),
            content_kind=ArtifactContentKind.BINARY_DOCUMENT,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="xlsx",
            canonical_mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            extensions=("xlsx",),
            content_kind=ArtifactContentKind.BINARY_DOCUMENT,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="pptx",
            canonical_mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
            extensions=("pptx",),
            content_kind=ArtifactContentKind.BINARY_DOCUMENT,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="png",
            canonical_mime_type="image/png",
            extensions=("png",),
            content_kind=ArtifactContentKind.IMAGE,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="jpeg",
            canonical_mime_type="image/jpeg",
            extensions=("jpg", "jpeg"),
            content_kind=ArtifactContentKind.IMAGE,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="webp",
            canonical_mime_type="image/webp",
            extensions=("webp",),
            content_kind=ArtifactContentKind.IMAGE,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="zip",
            canonical_mime_type="application/zip",
            extensions=("zip",),
            content_kind=ArtifactContentKind.ARCHIVE,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
        ArtifactFormatSpec(
            format_id="opaque_binary",
            canonical_mime_type="application/octet-stream",
            extensions=(),
            content_kind=ArtifactContentKind.OPAQUE_BINARY,
            capabilities=binary_caps,
            requires_external_processor=True,
        ),
    ]
    return ArtifactFormatRegistry(specs)
